#!/bin/bash
# Install or update aquacontrol on the Proxmox host. Run as root from the repo checkout.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"

id aquacontrol >/dev/null 2>&1 || \
  useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin aquacontrol

install -d -m 0755 /opt/aquacontrol
rsync -a --delete "$SRC/aquacontrol" "$SRC/static" /opt/aquacontrol/

install -m 0644 "$SRC/deploy/70-aquacontrol.rules" /etc/udev/rules.d/70-aquacontrol.rules
udevadm control --reload
udevadm trigger --action=change --subsystem-match=hidraw

install -d -m 0750 -o root -g aquacontrol /etc/aquacontrol
if [ ! -f /etc/aquacontrol/daemon.json ]; then
  printf '{\n  "listen": "0.0.0.0",\n  "port": 8443,\n  "password_hash": "",\n  "push_tokens": {}\n}\n' \
    > /etc/aquacontrol/daemon.json
  chown root:aquacontrol /etc/aquacontrol/daemon.json
  chmod 0640 /etc/aquacontrol/daemon.json
fi

install -d -m 0750 -o aquacontrol -g aquacontrol /var/lib/aquacontrol /var/lib/aquacontrol/backups
if [ ! -f /var/lib/aquacontrol/config.json ]; then
  (cd /opt/aquacontrol && python3 -c 'import json; from aquacontrol.config import DEFAULT_APP_CONFIG; print(json.dumps(DEFAULT_APP_CONFIG, indent=2, ensure_ascii=False))') \
    > /var/lib/aquacontrol/config.json
  chown aquacontrol:aquacontrol /var/lib/aquacontrol/config.json
fi

# Prefer a custom/ACME certificate for the web UI if one is installed.
CERT=/etc/pve/local/pve-ssl.pem; KEY=/etc/pve/local/pve-ssl.key
if [ -f /etc/pve/local/pveproxy-ssl.pem ]; then
  CERT=/etc/pve/local/pveproxy-ssl.pem; KEY=/etc/pve/local/pveproxy-ssl.key
fi
install -m 0644 "$SRC/deploy/aquacontrol.service" /etc/systemd/system/aquacontrol.service
install -d /etc/systemd/system/aquacontrol.service.d
printf '[Service]\nLoadCredential=cert:%s\nLoadCredential=key:%s\n' "$CERT" "$KEY" \
  > /etc/systemd/system/aquacontrol.service.d/cert.conf

systemctl daemon-reload
systemctl enable aquacontrol >/dev/null
if grep -q '"password_hash": ""' /etc/aquacontrol/daemon.json; then
  echo "Noch kein Passwort: cd /opt/aquacontrol && python3 -m aquacontrol set-password && systemctl restart aquacontrol"
else
  systemctl restart aquacontrol
  systemctl --no-pager --lines=5 status aquacontrol || true
fi
