#!/usr/bin/env bash
# Test runner for the Zabbix maps-as-code blog post (Part 1).
#
# Usage:
#   ./run.sh            — full run (deps, VM, provision Zabbix, setup hosts, maps)
#   ./run.sh maps-only  — skip everything, just run the maps playbook

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Locate Python 3.10+ and put its user bin dir first in PATH ─────────────────
# ansible-core >= 2.16 requires Python 3.10+; macOS system python3 is 3.9.
# This runs unconditionally so maps-only mode also picks up the right ansible.

PYTHON3_NEW=""
for py in python3.13 python3.12 python3.11 python3.10; do
  if command -v "$py" &>/dev/null; then
    PYTHON3_NEW="$py"
    break
  fi
done
if [ -z "$PYTHON3_NEW" ]; then
  echo "ERROR: Python 3.10+ not found. Run: brew install python@3.11"
  exit 1
fi
PYTHON3_NEW_BIN="$($PYTHON3_NEW -c 'import site,os; print(os.path.join(site.getuserbase(),"bin"))')"
export PATH="$PYTHON3_NEW_BIN:$PATH"

# ── Sanity checks ──────────────────────────────────────────────────────────────

for cmd in limactl ansible-playbook ansible-galaxy brew curl; do
  command -v "$cmd" >/dev/null || { echo "ERROR: $cmd not found"; exit 1; }
done

MAPS_ONLY="${1:-}"

# ── Install Mac-side dependencies ──────────────────────────────────────────────

if [ "$MAPS_ONLY" != "maps-only" ]; then
  echo "==> Using $PYTHON3_NEW ($(${PYTHON3_NEW} --version))"

  echo "==> Installing ansible-core >= 2.16..."
  "$PYTHON3_NEW" -m pip install --quiet --user --break-system-packages 'ansible-core>=2.16'

  echo "==> Installing Ansible collection..."
  ansible-galaxy collection install -r "$SCRIPT_DIR/requirements.yml"

  echo "==> Installing Python deps (pydotplus, webcolors, Pillow)..."
  "$PYTHON3_NEW" -m pip install --quiet --user --break-system-packages pydotplus webcolors Pillow

  echo "==> Checking graphviz..."
  brew list graphviz &>/dev/null || brew install graphviz
fi

# ── Lima VM ────────────────────────────────────────────────────────────────────

PREFERRED_VM="zabbix-server"
FALLBACK_VM="zabbix-maps-server"

if limactl list "$PREFERRED_VM" --format '{{.Status}}' 2>/dev/null | grep -q "Running"; then
  ZABBIX_VM="$PREFERRED_VM"
  echo "==> Reusing running Lima VM: $ZABBIX_VM"
elif limactl list "$FALLBACK_VM" --format '{{.Status}}' 2>/dev/null | grep -q "Running"; then
  ZABBIX_VM="$FALLBACK_VM"
  echo "==> Reusing running Lima VM: $ZABBIX_VM"
else
  ZABBIX_VM="$FALLBACK_VM"
  echo "==> Starting Lima VM: $ZABBIX_VM"
  limactl start --tty=false --name="$ZABBIX_VM" "$SCRIPT_DIR/lima/zabbix-maps-server.yaml"
fi

# ── Discover VM IP ─────────────────────────────────────────────────────────────

echo "==> Waiting for IP on shared network..."
ZABBIX_IP=""
for i in $(seq 1 30); do
  ZABBIX_IP=$(limactl shell "$ZABBIX_VM" -- \
    ip -4 route get 192.168.105.1 2>/dev/null \
    | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1); exit}' || true)
  [ -n "$ZABBIX_IP" ] && break
  sleep 2
done
[ -z "$ZABBIX_IP" ] && { echo "ERROR: could not get IP for $ZABBIX_VM after 60s"; exit 1; }
echo "    $ZABBIX_VM -> $ZABBIX_IP"

SSH_KEY="$HOME/.lima/_config/user"
SSH_USER="$(whoami)"

# ── Write inventory with real IP ───────────────────────────────────────────────

cat > "$SCRIPT_DIR/inventory.ini" <<EOF
[localhost]
localhost ansible_connection=local

# httpapi — used by setup_test_hosts.yml and create_zabbix_maps.yml
# Separate host alias so ZABBIX_SERVER:vars don't bleed in.
[ZABBIX_API]
zabbix-maps-api ansible_host=${ZABBIX_IP}

[ZABBIX_API:vars]
ansible_connection=httpapi
ansible_network_os=community.zabbix.zabbix
ansible_user=Admin
ansible_httpapi_port=80
ansible_httpapi_use_ssl=false
ansible_httpapi_validate_certs=false
ansible_password=zabbix

# SSH — used by provision_zabbix.yml only
# Different host alias so httpapi vars don't bleed in.
[ZABBIX_SERVER]
zabbix-maps-ssh ansible_host=${ZABBIX_IP}

[ZABBIX_SERVER:vars]
ansible_connection=ssh
ansible_user=${SSH_USER}
ansible_ssh_private_key_file=${SSH_KEY}
ansible_ssh_common_args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'
ansible_become=true
ansible_python_interpreter=/usr/bin/python3
EOF

# group_vars — Zabbix URL for the maps playbook (module_defaults / server_url pattern)
cat > "$SCRIPT_DIR/group_vars/all/zabbix.yml" <<EOF
# URL for community.zabbix.zabbix_map module_defaults (legacy auth pattern).
# Standard Debian/Ubuntu Zabbix install puts the API at /zabbix/api_jsonrpc.php.
zabbix_url:            "http://${ZABBIX_IP}/zabbix"
zabbix_user:           "Admin"
zabbix_password:       "zabbix"
zabbix_validate_certs: false
EOF

echo "==> Inventory and group_vars written"

# ── Preflight: verify Zabbix is up (always) ────────────────────────────────────

ZABBIX_API_URL="http://${ZABBIX_IP}/zabbix/api_jsonrpc.php"
zabbix_up() {
  curl -sf --connect-timeout 5 "$ZABBIX_API_URL" \
    -d '{"jsonrpc":"2.0","method":"apiinfo.version","id":1,"params":{}}' \
    -H "Content-Type: application/json" 2>/dev/null | grep -q "jsonrpc"
}

if [ "$MAPS_ONLY" = "maps-only" ]; then
  if ! zabbix_up; then
    echo "ERROR: Zabbix is not responding at $ZABBIX_API_URL"
    echo "       Run './run.sh' (without maps-only) to provision Zabbix first."
    exit 1
  fi
fi

# ── Provision Zabbix if not already running ────────────────────────────────────

if [ "$MAPS_ONLY" != "maps-only" ]; then
  echo "==> Checking if Zabbix API is up..."
  if zabbix_up; then
    echo "    Zabbix already responding — skipping provision."
  else
    echo "==> Provisioning Zabbix on $ZABBIX_VM..."
    echo "    Step 1: bootstrap Python3 via limactl (avoids SSH+become for raw bootstrap)"
    limactl shell "$ZABBIX_VM" -- sudo apt-get install -y python3

    echo "    Step 2: install Zabbix server + MySQL via ansible-zabbix role"
    # --skip-tags repo: the pre_tasks already installed the correct ubuntu24.04
    # repo package; skipping the role's repo task prevents it from overwriting
    # with the ubuntu26.04 package that tail -1 picks from the directory index.
    ANSIBLE_ROLES_PATH="$HOME/development" \
      ansible-playbook -i "$SCRIPT_DIR/inventory.ini" \
        --skip-tags repo \
        "$SCRIPT_DIR/provision_zabbix.yml"
  fi

  # ── Register test hosts in Zabbix ────────────────────────────────────────────

  echo "==> Registering test hosts in Zabbix..."
  ansible-playbook -i "$SCRIPT_DIR/inventory.ini" \
    -e "zabbix_server_ip=${ZABBIX_IP}" \
    "$SCRIPT_DIR/setup_test_hosts.yml"
fi

# ── Run the maps playbook ──────────────────────────────────────────────────────

echo "==> Running maps playbook..."
ansible-playbook -i "$SCRIPT_DIR/inventory.ini" \
  -v "$SCRIPT_DIR/create_zabbix_maps.yml"

echo ""
echo "Done. Verify at: http://${ZABBIX_IP}/zabbix/index.php?action=map.list"
