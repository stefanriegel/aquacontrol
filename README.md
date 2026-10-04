# aquacontrol

Web control for an **Aquacomputer QUADRO** fan controller on a Linux/Proxmox host, replacing the
Windows-only aquasuite for day-to-day use.

The QUADRO runs its fan curves and LED effects on its own from settings stored in the device.
aquacontrol reads live values, edits those stored settings (fan mode, target temperature, curve,
min/max, LED strip on/off and brightness), switches the LEDs on a time schedule, and shows other
temperatures (host hwmon sensors, values pushed from other machines) for reference.

- Python 3.11+ standard library only, no dependencies
- Talks to the device via Linux `hidraw` (feature report 0x03 = settings, input report 0x01 = live data)
- No schedule rules are installed by default, so a fresh install never writes to the device on its own; add rules in the UI
- The commit report is sent as a HID feature report like the kernel driver (`commit_as="output"` on `HidrawTransport` switches to an output report)
- Every write: fresh read → CRC check → backup → write → commit → read-back verify → rollback on mismatch
- Backups: fan changes and restores create a backup first; LED strip and schedule changes do not (by design, they are frequent and harmless)

> Protocol knowledge builds on [TimSC/quadroctl](https://github.com/TimSC/quadroctl) and the
> [aquacomputer_d5next hwmon driver](https://github.com/aleksamagicka/aquacomputer_d5next-hwmon).
> Tested with QUADRO firmware 1033 only. Writing settings to hardware is at your own risk.

## Install (Proxmox host, as root)

```bash
rsync -a --exclude .git --exclude dev --exclude .superpowers --exclude __pycache__ \
    ./ root@pve:/root/aquacontrol-src/
ssh root@pve /root/aquacontrol-src/deploy/install.sh
ssh root@pve 'cd /opt/aquacontrol && python3 -m aquacontrol set-password && systemctl restart aquacontrol'
```

Web UI: `https://<host>:8443/` (user name is ignored, password as set).

The web UI uses the PVE node certificate (or `pveproxy-ssl.pem` if both it and its key exist). The
certificate is loaded when the service starts, so after PVE renews it run `systemctl restart aquacontrol`.

Before the first change, store a pinned backup of the device settings:

```bash
runuser -u aquacontrol -- sh -c 'cd /opt/aquacontrol && python3 -m aquacontrol backup --pinned --reason initial'
```

## Klima-Automatik

Optional: schaltet die Raum-Klimaanlage über **Home Assistant** (REST-API) ein, wenn die Wasserkühlung dauerhaft am
Limit läuft, und wieder aus, wenn das Wasser deutlich kühler ist. Konfiguration im Tab „Klima“; **standardmäßig
deaktiviert** (`enabled: false`, keine HA-Adresse). Details: `docs/superpowers/specs/2026-10-04-aquacontrol-climate-design.md`.

- **Ein:** Wasser (Sensor 1) ≥ 40 °C und Radiator-Lüfter (Kanäle 2 und 3) ≥ 85 % ununterbrochen seit 5 min, die
  Klimaanlage ist laut HA aus. Dann werden Betriebsart, Solltemperatur, Preset, Lüfterstufe und die beiden Lamellen
  gesetzt. Alle Werte sind einstellbar.
- **Aus:** nur wenn aquacontrol sie selbst eingeschaltet hat und noch „besitzt“: Wasser ≤ 36 °C seit 10 min und
  mindestens 30 min Laufzeit.
- **Handbetrieb gewinnt:** Läuft die Klimaanlage schon, passiert nichts. Ändert jemand Zustand, Solltemperatur oder
  Preset (zwei Abfragen hintereinander mit Abweichung; Temperatur ±0,25 °C, Preset ohne Groß-/Kleinschreibung), gibt
  aquacontrol den Besitz ab und schaltet nichts mehr. Nach einem Daemon-Neustart gilt eine laufende Klimaanlage als fremd.
- **Von Hand ausgeschaltet:** Schaltet jemand die Klimaanlage aus (eine eigene oder eine, die aquacontrol laufen sah),
  schaltet die Automatik **nicht** wieder ein, solange die Einschalt-Bedingung ununterbrochen gilt. Die Pause endet,
  sobald das Wasser unter die Einschalt-Temperatur fällt, ein Lüfter darunter liegt oder die Temperatur unbekannt ist,
  spätestens nach `manual_off_pause_minutes` (Standard 120, einstellbar 10 bis 480).
- **Einschalten bestätigen:** Nach dem Einschalten wartet aquacontrol bis zu 10 s, bis Home Assistant die gesetzten
  Werte zeigt (die Cloud ist verzögert). Zeigt sie sie nicht, gilt das Einschalten als „unbestätigt“ und wird in den
  nächsten Zyklen erneut geprüft.
- **Gegen Pendeln:** mindestens 2 °C Abstand zwischen Ein- und Ausschalt-Temperatur, Mindestlaufzeit (30 min),
  Sperrzeit nach dem Ausschalten (15 min) und höchstens 2 eigene Schaltvorgänge pro Stunde. Das Limit blockiert nur
  das Einschalten, nie das Ausschalten.
- **Fehler:** QUADRO offline oder Wasser unbekannt: nie einschalten, eine eigene Klimaanlage bleibt an. HA nicht
  erreichbar oder ein Service-Aufruf schlägt fehl: Wiederholung nach 1, 5, dann 15 min; die letzten 20 Ereignisse
  stehen im Tab.
- **Token:** Ein Long-Lived Access Token aus dem HA-Profil. Er liegt in `secrets.json` neben `config.json`
  (Modus 0600), wird nie geloggt und nie von der API zurückgegeben (nur `token_set`). Im Tab ist das Feld leer =
  unverändert; „Token löschen“ entfernt ihn. „Verbindung testen“ liest nur und schaltet nichts. **Ändert man die
  Adresse ohne neuen Token, wird der gespeicherte Token gelöscht**, damit er nie an eine andere Adresse geschickt wird.
- **Adresse und Zertifikat:** `http://` oder `https://` mit einem öffentlich vertrauten Zertifikat (z. B. Nabu Casa oder
  Let's Encrypt). Ein selbstsigniertes Zertifikat wird nicht akzeptiert. Über `http://` im LAN läuft der Token
  unverschlüsselt über das Netz: nur in einem vertrauenswürdigen Netz verwenden. Benutzer und Passwort in der Adresse
  (`http://user:pw@…`) werden abgelehnt.
- **Auswahllisten:** Betriebsart, Preset, Lüfterstufe, Lamellenpositionen und die Entities sind Auswahlfelder. Ihre
  Werte liest aquacontrol aus Home Assistant (eingebaute Listen, falls HA nicht erreichbar ist).
- API: `GET`/`PUT /api/climate`, `POST /api/climate/test`, `GET /api/climate/options`.

## LED-Editor

Der Tab „LEDs“ bearbeitet die LED-Effekte direkt im QUADRO, ohne Aquasuite:

- **Farbschalter** (Farbe nach Temperatur): bis zu fünf Schwellen (ganze °C, streng steigend, innerhalb des gespeicherten
  Bereichs), je Schwelle eine Farbe, Datenquelle (Temperatursensor 1–4) sowie Überblenden, Blinken und Helligkeit nach
  Datenquelle. Eine Vorschau-Leiste zeigt die Farbbereiche und den aktuellen Messwert.
- **Statische Farbe**: eine Farbe plus die drei Schalter.
- Nicht benutzte Controller werden nicht angezeigt. LED-Bereiche, Modus und andere Effektwerte bleiben unverändert; andere
  Datenquellen (Durchfluss, Software-Sensoren) werden nur angezeigt.
- Jede Änderung legt vorher ein Backup an. Beim Wiederherstellen eines Backups dürfen sich LED-Daten unterscheiden
  (Pumpe und Lüfter werden weiterhin vollständig geprüft).
- API: `GET /api/settings` (`leds[]`) und `PUT /api/settings/led/1..8` mit `thresholds`, `colors` (`"#rrggbb"`), `fade`,
  `blink`, `brightness_by_source`, `source`. Die Byte-Belegung steht in `docs/led-layout.md`.

## Pushing extra sensors (e.g. GPUs from a VM)

```bash
cd /opt/aquacontrol && python3 -m aquacontrol add-push-token llm-vm   # prints the token once
systemctl restart aquacontrol
```

On the sending machine (VM 103), as root, with the repo checked out there:

```bash
install -m 0755 deploy/push-gpu/aquacontrol-push-gpu.sh /usr/local/bin/
install -m 0644 deploy/push-gpu/aquacontrol-push-gpu.service deploy/push-gpu/aquacontrol-push-gpu.timer /etc/systemd/system/
scp root@<pve-host>:/etc/pve/pve-root-ca.pem /etc/aquacontrol-ca.pem
install -m 0600 /dev/null /etc/aquacontrol-push.env      # then fill it, see below
systemctl daemon-reload
systemctl enable --now aquacontrol-push-gpu.timer
```

`/etc/aquacontrol-push.env` (mode 0600):

```
AQUACONTROL_URL=https://<pve-host>:8443
AQUACONTROL_TOKEN=<token>
AQUACONTROL_CA=/etc/aquacontrol-ca.pem
```

The host in `AQUACONTROL_URL` must match a subject alternative name (SAN) of the PVE certificate.
If it does not (for example a bare IP), use the node's host name there and make it resolve in the VM
(DNS or an `/etc/hosts` entry).
Check with `systemctl status aquacontrol-push-gpu.service` and `journalctl -u aquacontrol-push-gpu`.

## Development

```bash
python3 -m unittest discover -s tests -t .
# UI against a simulated device:
python3 -m aquacontrol --daemon-config dev/daemon.json --app-config dev/config.json \
    run --fake tests/fixtures --no-tls --listen 127.0.0.1 --port 18443
```

`dev/` is git-ignored, so create the dev configs once after checkout:

```bash
mkdir -p dev/backups
python3 - <<'EOF'
import json
from aquacontrol.auth import hash_password
from aquacontrol.config import DEFAULT_APP_CONFIG
json.dump({"listen": "127.0.0.1", "port": 18443, "password_hash": hash_password("devpassword1"),
           "push_tokens": {}}, open("dev/daemon.json", "w"), indent=2)
json.dump({**DEFAULT_APP_CONFIG, "backup_dir": "dev/backups"}, open("dev/config.json", "w"), indent=2)
EOF
```

Without `dev/config.json` pointing `backup_dir` at a local path, writes try `/var/lib/aquacontrol/backups`.

Test fixtures are real device reports. Status reports must have the device serial zeroed
(`tools/make_fixture.py` does that); the settings and names reports contain no serial.

## Hardware verification (QUADRO firmware 1033, 2026-10-04)

- Read path: live values via hidraw match the kernel hwmon driver exactly (temperature, all fan RPMs, flow);
  settings decode matches the device; feature read returns 961 bytes.
- No-op write (`selftest-write`): settings written, committed as **feature** report 0x02, read back byte-identical.
  No device-volatile bytes (`VOLATILE_OFFSETS` stays empty).
- Real writes take effect immediately: channel 4 fixed 60 % → 499 → 1094 rpm; restoring the backup returned it to
  target-temperature mode and ~540 rpm. (The firmware-1033 "write has no effect" issue does not occur with this commit.)
- LED strip: brightness 60, off (flag 0x0002) and on again via the API were confirmed visually.
- Schedule: test rules switched the strip off and on at the configured minute.
- Curve semantics: the device maps curve percentages linearly into [min, max]
  (pump: curve 4.31 % with min 28.02 / max 90.3 → output 30.70 %, reported 30.7 %).
- Open: persistence across a QUADRO power cycle (pending), full host reboot test (when a reboot is planned anyway).
