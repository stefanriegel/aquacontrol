#!/bin/bash
# Install or update aquacontrol on the Proxmox host. Run as root from the repo checkout.
set -euo pipefail
[ "$EUID" -eq 0 ] || { echo "install.sh must run as root" >&2; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"

TMPFILES=()
cleanup() { [ "${#TMPFILES[@]}" -eq 0 ] || rm -f -- "${TMPFILES[@]}"; }
trap cleanup EXIT

id aquacontrol >/dev/null 2>&1 || \
  useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin aquacontrol

install -d -m 0755 /opt/aquacontrol
rsync -a --delete --chown=root:root --chmod=D755,F644 --exclude __pycache__ \
  "$SRC/aquacontrol" "$SRC/static" /opt/aquacontrol/

install -m 0644 "$SRC/deploy/70-aquacontrol.rules" /etc/udev/rules.d/70-aquacontrol.rules
udevadm control --reload
udevadm trigger --action=change --subsystem-match=hidraw

# Missing or empty counts as "not there". Files are generated into a temp file in the
# target directory and moved into place atomically, so a failure never leaves a partial file.
install -d -m 0750 -o root -g aquacontrol /etc/aquacontrol
if [ ! -s /etc/aquacontrol/daemon.json ]; then
  tmp=$(mktemp /etc/aquacontrol/.daemon.json.XXXXXX); TMPFILES+=("$tmp")
  printf '{\n  "listen": "0.0.0.0",\n  "port": 8443,\n  "password_hash": "",\n  "push_tokens": {}\n}\n' > "$tmp"
  chown root:aquacontrol "$tmp"
  chmod 0640 "$tmp"
  mv -f -- "$tmp" /etc/aquacontrol/daemon.json
fi

install -d -m 0750 -o aquacontrol -g aquacontrol /var/lib/aquacontrol /var/lib/aquacontrol/backups
if [ ! -s /var/lib/aquacontrol/config.json ]; then
  tmp=$(mktemp /var/lib/aquacontrol/.config.json.XXXXXX); TMPFILES+=("$tmp")
  (cd /opt/aquacontrol && python3 -B -c 'import json; from aquacontrol.config import DEFAULT_APP_CONFIG; print(json.dumps(DEFAULT_APP_CONFIG, indent=2, ensure_ascii=False))') > "$tmp"
  chown aquacontrol:aquacontrol "$tmp"
  chmod 0644 "$tmp"
  mv -f -- "$tmp" /var/lib/aquacontrol/config.json
fi

# Prefer a custom/ACME certificate for the web UI if both files are installed.
CERT=/etc/pve/local/pve-ssl.pem; KEY=/etc/pve/local/pve-ssl.key
if [ -f /etc/pve/local/pveproxy-ssl.pem ] && [ -f /etc/pve/local/pveproxy-ssl.key ]; then
  CERT=/etc/pve/local/pveproxy-ssl.pem; KEY=/etc/pve/local/pveproxy-ssl.key
fi
install -m 0644 "$SRC/deploy/aquacontrol.service" /etc/systemd/system/aquacontrol.service
install -d /etc/systemd/system/aquacontrol.service.d
printf '[Service]\nLoadCredential=cert:%s\nLoadCredential=key:%s\n' "$CERT" "$KEY" \
  > /etc/systemd/system/aquacontrol.service.d/cert.conf

systemctl daemon-reload
systemctl enable aquacontrol >/dev/null
# Exit status 0 only if daemon.json parses and has a non-empty password hash.
if (cd /opt/aquacontrol && python3 -B -c 'import sys; from aquacontrol.config import load_daemon_config as l; sys.exit(0 if l("/etc/aquacontrol/daemon.json").password_hash else 1)') 2>/dev/null; then
  systemctl restart aquacontrol
  systemctl --no-pager --lines=5 status aquacontrol || true
else
  echo "Noch kein Passwort: cd /opt/aquacontrol && python3 -m aquacontrol set-password && systemctl restart aquacontrol"
fi
