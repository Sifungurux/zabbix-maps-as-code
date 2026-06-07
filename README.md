Zabbix Maps as Code — Test Scaffold
===================================

A self-contained Ansible test harness for the [Zabbix Maps as Code](https://github.com/Sifungurux/tech-blog) blog series. It spins up a Lima VM, installs Zabbix 7.4, and exercises both approaches to managing Zabbix network maps from the posts:

- **Part 1** — [`community.zabbix.zabbix_map`](https://docs.ansible.com/ansible/latest/collections/community/zabbix/zabbix_map_module.html), driven from a structured `map_hosts.yaml` rendered to DOT via a Jinja2 template
- **Part 2** — a custom module, [`zabbix_map_from_yaml`](library/zabbix_map_from_yaml.py), that talks to the Zabbix API directly from a plain YAML map definition

Everything here was run against a live Zabbix 7.4.11 install; the findings (and the corrections they drove in the blog posts) are written up in [Testing Zabbix Maps as Code — What the Blog Post Got Wrong](https://github.com/Sifungurux/tech-blog/blob/main/src/content/posts/05-06-2026-testing-zabbix-maps-as-code.md).

---

Requirements
------------

- macOS with [Lima](https://github.com/lima-vm/lima) (`limactl`) and [Homebrew](https://brew.sh)
- `ansible-playbook` / `ansible-galaxy` (the script installs `ansible-core >= 2.16` for you — it requires Python 3.10+, so the script locates a Homebrew Python and uses that instead of macOS's system Python 3.9)
- `curl`

`./run.sh` installs the rest on first run: the `community.zabbix` collection, `pydotplus` / `webcolors` / `Pillow` (needed to render DOT maps to images), and `graphviz` via Homebrew.

---

Running the tests
-----------------

```bash
./run.sh            # full run: start the VM, provision Zabbix, register test hosts, create maps (Part 1)
./run.sh maps-only  # skip provisioning — just (re)run the Part 1 maps playbook against an already-running VM
```

A cold run takes around ten minutes, most of it spent installing and configuring the Zabbix server package. `run.sh` writes `inventory.ini` and `group_vars/all/zabbix.yml` with the VM's actual IP each time it runs, so both files are generated artifacts rather than fixed config.

To exercise the **Part 2** custom module against the same VM:

```bash
ansible-playbook -i inventory.ini create_zabbix_maps_custom.yml
ansible-playbook -i inventory.ini create_zabbix_maps_custom.yml -e update=true   # overwrite an existing map
ansible-playbook -i inventory.ini create_zabbix_maps_custom.yml -e map_name="Zabbix Infrastructure (Custom)"
```

Verify the result at `http://<VM_IP>/zabbix` under **Monitoring → Maps** (default credentials `Admin` / `zabbix`).

---

Layout
------

| Path | Purpose |
|------|---------|
| `lima/zabbix-maps-server.yaml` | Lima VM definition (Ubuntu 24.04 ARM64, 4 GiB RAM) |
| `provision_zabbix.yml` | Installs Zabbix server + MariaDB via the [`ansible-zabbix`](https://github.com/Sifungurux/ansible-zabbix) role |
| `setup_test_hosts.yml` | Registers a handful of test hosts in Zabbix via the API, so the maps have something to point at |
| `map_hosts.yaml` / `templates/map_dot.j2` | Part 1: structured topology rendered to DOT |
| `create_zabbix_maps.yml` | Part 1: builds the map with `community.zabbix.zabbix_map` |
| `maps_custom.yaml` | Part 2: map definition in the custom module's plain YAML format |
| `create_zabbix_maps_custom.yml` | Part 2: builds the map with `zabbix_map_from_yaml` |
| `library/zabbix_map_from_yaml.py` | The custom module itself |
| `run.sh` | Orchestrates the whole Part 1 flow end to end |

---

Findings from testing against live Zabbix 7.4
----------------------------------------------

Both posts were written from documentation first and corrected after running against a real install. The headline issues this harness surfaced:

- `community.zabbix` 4.x dropped legacy `server_url`/`login_user` auth — it's `httpapi`-only now, which means a dedicated `ZABBIX_API` inventory group with `ansible_connection=httpapi`
- `zbx_map` silently creates a plain image element instead of a drill-down sub-map; the correct DOT attribute is `zbx_sysmap`
- `zbx_url_name` / `zbx_url` node attributes make DOT-format map creation fail outright in 7.4
- The `Database_(64)` / `Desktop_(48)` icons from older Zabbix versions don't exist in the 7.4 default image set
- `{URL.HOST}` is not a real Zabbix macro — it's stored and displayed literally rather than expanded; `{HOST.HOST}` and `{HOST.ID}` work
- The `map.create` / `map.update` API endpoints reject non-empty `linktriggers` arrays in 7.4 (`Invalid parameter: should be empty`), so trigger-based link styling isn't usable there
- `/var/lib/zabbix` must be owned by `zabbix:zabbix` for ICMP ping checks to work

Full detail and context on each of these is in the [testing post](https://github.com/Sifungurux/tech-blog/blob/main/src/content/posts/05-06-2026-testing-zabbix-maps-as-code.md).
