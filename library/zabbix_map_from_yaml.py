#!/usr/bin/python
# -*- coding: utf-8 -*-

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import json
import traceback

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.urls import fetch_url


# ── Zabbix API client ──────────────────────────────────────────────────────────

class ZabbixAPI:
    def __init__(self, module, url, token=None, user=None, password=None):
        self.module = module
        self.url    = url.rstrip("/") + "/api_jsonrpc.php"
        self._id    = 1
        self._auth  = None
        self._token = token

        if not token:
            if user and password:
                self._auth = self._login(user, password)
            else:
                module.fail_json(
                    msg="Provide either 'token' or 'login_user' + 'login_password'"
                )

    def _headers(self):
        h = {"Content-Type": "application/json"}
        # Zabbix 7.x removed the 'auth' body field — both API tokens and
        # session tokens obtained via user.login use Authorization: Bearer.
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        elif self._auth:
            h["Authorization"] = f"Bearer {self._auth}"
        return h

    def call(self, method, params):
        payload = {
            "jsonrpc": "2.0",
            "method":  method,
            "params":  params,
            "id":      self._id,
        }
        self._id += 1

        resp, info = fetch_url(
            self.module,
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        if info["status"] != 200:
            self.module.fail_json(
                msg=f"HTTP {info['status']} calling Zabbix API method '{method}'"
            )
        body = json.loads(resp.read())
        if "error" in body:
            err = body["error"]
            self.module.fail_json(
                msg=f"Zabbix API error [{method}]: ({err['code']}) {err['data']}"
            )
        return body["result"]

    def _login(self, user, password):
        return self.call("user.login", {"username": user, "password": password})

    def resolve_hosts(self, names):
        if not names:
            return {}
        result = self.call("host.get", {
            "filter": {"host": names}, "output": ["hostid", "host"]
        })
        mapping = {h["host"]: h["hostid"] for h in result}
        missing = set(names) - set(mapping)
        if missing:
            self.module.warn(f"Hosts not found in Zabbix: {sorted(missing)}")
        return mapping

    def resolve_maps(self, names):
        if not names:
            return {}
        result = self.call("map.get", {
            "filter": {"name": names}, "output": ["sysmapid", "name"]
        })
        mapping = {m["name"]: m["sysmapid"] for m in result}
        missing = set(names) - set(mapping)
        if missing:
            self.module.warn(f"Maps not found in Zabbix: {sorted(missing)}")
        return mapping

    def resolve_hostgroups(self, names):
        if not names:
            return {}
        result = self.call("hostgroup.get", {
            "filter": {"name": names}, "output": ["groupid", "name"]
        })
        mapping = {g["name"]: g["groupid"] for g in result}
        missing = set(names) - set(mapping)
        if missing:
            self.module.warn(f"Host groups not found in Zabbix: {sorted(missing)}")
        return mapping

    def resolve_icons(self, names):
        if not names:
            return {}
        result = self.call("image.get", {
            "filter": {"name": names}, "output": ["imageid", "name"]
        })
        mapping = {i["name"]: i["imageid"] for i in result}
        missing = set(names) - set(mapping)
        if missing:
            self.module.warn(f"Icons not found in Zabbix: {sorted(missing)}")
        return mapping

    def resolve_triggers(self, specs):
        mapping = {}
        for spec in specs:
            result = self.call("trigger.get", {
                "host":   spec["host"],
                "filter": {"description": spec["trigger"]},
                "output": ["triggerid", "description"],
            })
            if result:
                mapping[(spec["host"], spec["trigger"])] = result[0]["triggerid"]
            else:
                self.module.warn(
                    f"Trigger not found: host='{spec['host']}' "
                    f"description='{spec['trigger']}'"
                )
        return mapping

    def get_map_by_name(self, name):
        result = self.call("map.get", {
            "filter": {"name": name}, "output": ["sysmapid", "name"]
        })
        return result[0] if result else None

    def create_map(self, payload):
        result = self.call("map.create", payload)
        return result["sysmapids"][0]

    def update_map(self, sysmapid, payload):
        self.call("map.update", {**payload, "sysmapid": sysmapid})

    def delete_map(self, sysmapid):
        self.call("map.delete", [sysmapid])


# ── Constants ──────────────────────────────────────────────────────────────────

ELEMENT_TYPE = {"host": 0, "map": 1, "trigger": 2, "hostgroup": 3, "image": 4}
DRAWTYPE     = {"line": 0, "bold": 2, "dotted": 3, "dashed": 4}
LABEL_TYPE   = {"label": 0, "ip": 1, "name": 2, "status_only": 3, "nothing": 4}


# ── Payload builder ────────────────────────────────────────────────────────────

def _resolve(val, mapping, default=0):
    return mapping.get(val, default) if isinstance(val, str) else val


def _collect_resources(map_def):
    host_names, map_names, icon_names, trigger_specs, group_names = [], [], [], [], []

    for elem in map_def.get("elements", []):
        etype = elem.get("type", "host")
        if etype == "host"      and "host"  in elem: host_names.append(elem["host"])
        if etype == "map"       and "map"   in elem: map_names.append(elem["map"])
        if etype == "hostgroup" and "group" in elem: group_names.append(elem["group"])
        for key in ("default", "problem", "maintenance", "disabled"):
            name = elem.get("icon", {}).get(key)
            if name:
                icon_names.append(name)

    for link in map_def.get("links", []):
        for t in link.get("triggers", []):
            trigger_specs.append({"host": t["host"], "trigger": t["trigger"]})
            if t["host"] not in host_names:
                host_names.append(t["host"])

    bg = map_def.get("background")
    if bg:
        icon_names.append(bg)

    return (
        list(dict.fromkeys(host_names)),
        list(dict.fromkeys(map_names)),
        list(dict.fromkeys(icon_names)),
        trigger_specs,
        list(dict.fromkeys(group_names)),
    )


def build_map_payload(map_def, api):
    host_names, map_names, icon_names, trigger_specs, group_names = _collect_resources(map_def)

    host_map    = api.resolve_hosts(host_names)
    map_id_map  = api.resolve_maps(map_names)
    icon_map    = api.resolve_icons(icon_names)
    trigger_map = api.resolve_triggers(trigger_specs)
    group_map   = api.resolve_hostgroups(group_names)

    local_to_selid = {}
    selements = []

    for idx, elem in enumerate(map_def.get("elements", []), start=1):
        local_id  = elem["id"]
        selid     = str(idx)
        local_to_selid[local_id] = selid
        etype_int = _resolve(elem.get("type", "host"), ELEMENT_TYPE, 0)

        selement = {
            "selementid":  selid,
            "elementtype": etype_int,
            "label":       elem.get("label", local_id),
            "x":           elem.get("x", 0),
            "y":           elem.get("y", 0),
        }

        if etype_int == 0:
            hname = elem.get("host")
            selement["elements"] = (
                [{"hostid": host_map[hname]}] if hname and hname in host_map else []
            )
        elif etype_int == 1:
            mname = elem.get("map")
            selement["elements"] = (
                [{"sysmapid": map_id_map[mname]}] if mname and mname in map_id_map else []
            )
        elif etype_int == 3:
            gname = elem.get("group")
            selement["elements"] = (
                [{"groupid": group_map[gname]}] if gname and gname in group_map else []
            )
        else:
            selement["elements"] = []

        for yaml_key, api_key in {
            "default":     "iconid_off",
            "problem":     "iconid_on",
            "maintenance": "iconid_maintenance",
            "disabled":    "iconid_disabled",
        }.items():
            icon_name = elem.get("icon", {}).get(yaml_key)
            if icon_name and icon_name in icon_map:
                selement[api_key] = icon_map[icon_name]

        if "label_type" in elem:
            selement["label_type"] = _resolve(elem["label_type"], LABEL_TYPE, 0)

        if "urls" in elem:
            selement["urls"] = elem["urls"]

        selements.append(selement)

    links = []
    for link in map_def.get("links", []):
        from_selid = local_to_selid.get(link["from"])
        to_selid   = local_to_selid.get(link["to"])
        if not from_selid or not to_selid:
            api.module.warn(
                f"Link skipped — unknown element: "
                f"'{link.get('from')}' → '{link.get('to')}'"
            )
            continue

        link_obj = {
            "selementid1": from_selid,
            "selementid2": to_selid,
            "label":       link.get("label", ""),
            "color":       link.get("color", "000000"),
            "drawtype":    _resolve(link.get("drawtype", "line"), DRAWTYPE, 0),
        }

        linktriggers = []
        for t in link.get("triggers", []):
            key = (t["host"], t["trigger"])
            if key not in trigger_map:
                api.module.warn(
                    f"Linktrigger skipped — trigger not found: "
                    f"host='{t['host']}' description='{t['trigger']}'"
                )
                continue
            linktriggers.append({
                "triggerid": trigger_map[key],
                "color":     t.get("color", "FF0000"),
                "drawtype":  _resolve(t.get("drawtype", "bold"), DRAWTYPE, 2),
            })
        if linktriggers:
            link_obj["linktriggers"] = linktriggers

        links.append(link_obj)

    lt_raw  = map_def.get("label_type", 0)
    payload = {
        "name":       map_def["name"],
        "width":      map_def.get("width", 1200),
        "height":     map_def.get("height", 800),
        "label_type": _resolve(lt_raw, LABEL_TYPE, 0),
        "selements":  selements,
        "links":      links,
    }

    bg = map_def.get("background")
    if bg and bg in icon_map:
        payload["backgroundid"] = icon_map[bg]

    return payload


# ── Module entry point ─────────────────────────────────────────────────────────

def main():
    module = AnsibleModule(
        argument_spec=dict(
            url=dict(type="str", required=True),
            token=dict(type="str", no_log=True),
            login_user=dict(type="str"),
            login_password=dict(type="str", no_log=True),
            map_definition=dict(type="dict", required=True),
            state=dict(type="str", default="present",
                       choices=["present", "absent"]),
            update=dict(type="bool", default=False),
            validate_certs=dict(type="bool", default=True),
        ),
        mutually_exclusive=[
            ["token", "login_user"],
            ["token", "login_password"],
        ],
        required_together=[["login_user", "login_password"]],
        required_one_of=[["token", "login_user"]],
        supports_check_mode=True,
    )

    map_def   = module.params["map_definition"]
    state     = module.params["state"]
    do_update = module.params["update"]
    map_name  = map_def.get("name")

    if not map_name:
        module.fail_json(msg="map_definition must contain a 'name' key")

    try:
        api = ZabbixAPI(
            module,
            url=module.params["url"],
            token=module.params["token"],
            user=module.params["login_user"],
            password=module.params["login_password"],
        )

        existing = api.get_map_by_name(map_name)

        if state == "absent":
            if not existing:
                module.exit_json(changed=False, msg=f"Map '{map_name}' does not exist")
            if module.check_mode:
                module.exit_json(changed=True, msg=f"Would delete map '{map_name}'")
            api.delete_map(existing["sysmapid"])
            module.exit_json(
                changed=True,
                msg=f"Deleted map '{map_name}' (sysmapid={existing['sysmapid']})",
            )

        payload = build_map_payload(map_def, api)

        if existing:
            if not do_update:
                module.exit_json(
                    changed=False,
                    sysmapid=existing["sysmapid"],
                    msg=f"Map '{map_name}' already exists (set update=true to overwrite)",
                )
            if module.check_mode:
                module.exit_json(
                    changed=True,
                    sysmapid=existing["sysmapid"],
                    msg=f"Would update map '{map_name}'",
                )
            api.update_map(existing["sysmapid"], payload)
            module.exit_json(
                changed=True,
                sysmapid=existing["sysmapid"],
                msg=f"Updated map '{map_name}' (sysmapid={existing['sysmapid']})",
            )
        else:
            if module.check_mode:
                module.exit_json(changed=True, msg=f"Would create map '{map_name}'")
            sysmapid = api.create_map(payload)
            module.exit_json(
                changed=True,
                sysmapid=sysmapid,
                msg=f"Created map '{map_name}' (sysmapid={sysmapid})",
            )

    except Exception as exc:
        module.fail_json(msg=str(exc), exception=traceback.format_exc())


if __name__ == "__main__":
    main()
