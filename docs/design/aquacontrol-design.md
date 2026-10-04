# aquacontrol – Design

Stand: 2026-10-04 · Status: Entwurf zur Freigabe

## 1. Ziel

Die Windows-VM 100 „aquasuite“ auf dem Proxmox-Host `pve` wird überflüssig.
Ein kleiner Daemon `aquacontrol` läuft direkt auf dem Host und bietet per Web-Oberfläche:

- Live-Anzeige aller Werte des Aquacomputer QUADRO
- Bearbeiten der Lüfterregelung, die im QUADRO gespeichert ist: Modus Fest / Zieltemperatur / Kurve,
  Zieltemperatur, Kurvenpunkte, Min/Max
- Bearbeiten der LED-Einstellungen, die im QUADRO gespeichert sind (pro Gerät, „Farbe nach Temperatur“)
- **Zeitplan für die LEDs:** An/Aus und Helligkeit nach Uhrzeit, z. B. 01:00 aus und 09:00 an
- **Weitere Temperaturen nur zur Anzeige:** Host-Sensoren per hwmon (CPU k10temp, NVMe, RAM spd5118,
  iGPU amdgpu) und die beiden RTX 3090 aus VM 103, die ihre Werte selbst an aquacontrol schickt (§3.4b)
- Backups des Geräteprofils und Wiederherstellung

**Erfolgskriterium:**
- VM 100 ist dauerhaft aus und hat kein USB-Passthrough mehr.
- Lüfter und LEDs verhalten sich wie vorher.
- Regelung und LED-Schwellwerte lassen sich im Browser ändern, und die Änderung ist nach einem
  Stromlos-Zyklus des QUADRO noch da.
- Die LEDs gehen zu den eingestellten Zeiten aus und wieder an.

### Nicht-Ziele (v1)

- MQTT, Home Assistant, aquasuite-web-Cloudexport (laut Nutzer nicht mehr gebraucht)
- Lüfter- oder LED-Regelung im Sekundentakt auf dem Host: das Gerät regelt selbst, siehe §2. Die einzige
  Laufzeitaufgabe des Daemons ist der LED-Zeitplan mit wenigen Schreibvorgängen pro Tag.
- Software-Sensoren (Host- oder GPU-Temperaturen an den QUADRO senden, damit Kurven darauf regeln). Die
  Temperaturen werden in v1 nur angezeigt; die Lüfter regeln weiter nach der Wassertemperatur.
- Datenhaltung über einen Neustart hinaus (Langzeit-Verlauf, Datenbank)
- Firmware-Updates, Umbenennen von Sensoren im Gerät (Report 0x08)

## 2. Befunde, auf denen das Design beruht

- **Gerät:** QUADRO, USB 0c70:f00d, Firmware 1033. Es ist das einzige
  Aquacomputer-Gerät am Host.
- **Das Gerät arbeitet autonom.**
  - Alle 4 Lüfterkurven laufen in der Firmware und regeln nach Sensor 1 („Wasser Temp“).
  - Die LED-Farbe nach Temperatur läuft ebenfalls in der Firmware.
  - Nachweis am 2026-10-04: Unter GPU-Last wurden die LEDs gelb, danach wieder grün, und die Lüfter drehten
    hoch und wieder runter. Dabei war die VM abgestürzt bzw. pausiert, und usbmon zeigte null Pakete vom
    Host zum Gerät.
- **Was die Aquasuite zur Laufzeit tat:** nur MQTT-Ausgabe und Cloudexport, beides entfällt.
- **Kanalbelegung** (Namen aus Geräte-Report `0x08`, Rolle laut Nutzer):

  | Kanal | Name im Gerät | Gerät | Modus jetzt |
  | --- | --- | --- | --- |
  | 1 | Pumpe | Pumpe: PWM-Signal und Drehzahl; Leistung 0 W, also extern versorgt | Kurve, Min 28 %, Max 90,3 % |
  | 2 | 140mm Radiator | Radiator-Lüfter | Zieltemperatur 34 °C |
  | 3 | 420mm Radiator | Radiator-Lüfter | Zieltemperatur 35 °C |
  | 4 | Fan 4 | Gehäuselüfter | Zieltemperatur 37 °C |

  Jedes Gerät hat eigene LEDs am RGBpx-Strip. Die Zuordnung LED-Controller → Gerät wird in §5 ermittelt;
  die Controller heißen im Gerät nur „LED Controller 1–8“. Die Strip-Helligkeit steht derzeit auf 218/255.
- **Maßgebliche Quelle ist das Gerät.** Das live gelesene Profil weicht in 68 Bytes vom Aquasuite-XML von 2025
  ab, das XML ist veraltet. Das erste Backup ist deshalb das live gelesene Profil
  (`quadro_ctrl_live.bin`, 2026-10-04, CRC ok). Feature-Lesen per hidraw vom Host funktioniert.
- **Controller-Modi:** 0 = Fest, 1 = Zieltemperatur (PID), 2 = Kurve, 4 = einem anderen Controller folgen.
- **Host-Kernel 6.17:** Der Mainline-Treiber `aquacomputer_d5next` bindet das Gerät (hwmon). Parallel
  existiert `/dev/hidrawN` für Feature-Reports.
- **Protokoll** (Quellen: TimSC/quadroctl `quadro-interface-v2.md`, aleksamagicka re-docs, eigene
  Dekodierung). Alle Werte sind Big-Endian, Temperaturen und Prozent jeweils ×100.
  - Input-Report `0x01` (220 Byte): Live-Werte, ca. 1×/s.
  - Feature-Report `0x03` (961 Byte): Einstellungen. Am Ende steht eine CRC-16/USB über die Bytes 1..958.
  - Report `0x02` (11 Byte, fix `02 00 00 00 02 00 00 00 00 34 c6`): Übernehmen/Speichern. Der Kernel-Treiber
    sendet ihn als **Feature-Report** (SET_REPORT, Typ Feature).
  - Payload-Offsets der Einstellungen:
    - 9: Sensor-Offsets
    - 17: FanConfig[4] à 9 Byte
    - 53: ControllerConfig[4] à 85 Byte (Modus, Fest-%, Sensor, PID, Kurvenstart, 16 Temperaturen,
      16 Prozentwerte)
    - 393: Strip-Konfiguration
    - 396: LedControllerConfig[8] à 70 Byte
    - 956: Profilnummer
- **Referenzdaten unter `tests/fixtures/`:**
  - das live gelesene Profil
  - die 4 alten Aquasuite-Profile (aus `Settings_DeviceProfiles.xml`)
  - der Namens-Report `0x08`
  - Input-Reports aus einer usbmon-Aufzeichnung
  - Keine Geheimnisse, denn das Repo ist öffentlich.
- **Seriennummer nie ins Repo** (Vorgabe des Nutzers):
  - Profil- und Namens-Reports (`0x03`, `0x08`) enthalten sie nachweislich nicht.
  - Input-Reports `0x01` enthalten sie in Bytes 3–6. Das Skript `tools/make_fixture.py`, das Fixtures
    erzeugt, setzt diese Bytes auf 0, und ein Unit-Test prüft das für alle Input-Fixtures.
  - Backups liegen nur auf dem Host, nicht im Repo.
  - Die API liefert die Seriennummer nicht aus, und Logs schreiben sie nicht.

## 3. Architektur

Python 3.13 (Debian 13), **nur Standardbibliothek**: keine pip-Pakete auf dem Hypervisor.

```
aquacontrol/
  aquacontrol/
    protocol.py   # reines Byte-Format: CRC, decode/encode Settings + Status. Keine I/O.
    transport.py  # hidraw: Feature lesen/schreiben (ioctl), Commit (Feature, optional Output), Input lesen
    device.py     # Geräte-Logik: Snapshot lesen, Änderung anwenden (Backup→Write→Commit→Verify)
    validate.py   # Sicherheitsregeln für Änderungen
    monitor.py    # Hintergrund-Thread: Input-Reports → aktueller Zustand + Ringpuffer
    backups.py    # Backup-Dateien anlegen/listen/laden
    sensors.py    # Host-hwmon-Temperaturen + Speicher für extern gepushte Sensoren
    schedule.py   # LED-Zeitplan: Soll-Zustand zu einer Uhrzeit berechnen, Scheduler-Thread
    config.py     # daemon.json + config.json laden/prüfen/atomar schreiben
    web.py        # HTTPS-Server, Basic-Auth, JSON-API, statische Dateien
    __main__.py   # Start, Verdrahtung
  static/index.html, app.js, style.css   # Vanilla JS, SVG-Diagramme, kein Build-Schritt
  tests/          # unittest, Fixtures mit echten Reports
  deploy/         # systemd-Unit, udev-Regel, install.sh
  docs/
```

Jede Einheit hat eine Aufgabe und lässt sich ohne Hardware testen. Die einzige Ausnahme ist
`transport.py`, das hinter einem kleinen Interface liegt und in Tests durch ein Fake-Gerät ersetzt wird.

### 3.1 protocol.py

- `crc16_usb(data) -> int`
- `decode_settings(report: bytes) -> Settings` liefert Dataclasses. Unbekannte Bereiche bleiben als Rohbytes
  erhalten.
- `encode_settings(settings, base: bytes) -> bytes` patcht **nur die bekannten Felder** in eine Kopie von
  `base` und berechnet die CRC neu. Unbekannte Bytes bleiben unverändert.
- `decode_status(report: bytes) -> Status` liefert Temperaturen 1–4, Software-Sensoren, Durchfluss, je Lüfter
  Drehzahl, Ausgang %, Spannung, Strom, Leistung sowie das aktive Profil.
- Invariante, abgesichert durch Tests: `encode(decode(r), r) == r` Byte für Byte für alle Fixture-Profile.

### 3.2 transport.py

- `HidrawTransport(path)` findet das Gerät über sysfs: `/sys/class/hidraw/*/device/uevent` mit
  `HID_ID=0003:00000C70:0000F00D`.
- Feature-Reports laufen über `fcntl.ioctl` (`HIDIOCGFEATURE` / `HIDIOCSFEATURE`). Lesen geschieht mit
  einem 1013-Byte-Puffer.
- Der Commit `0x02` wird wie im Kernel-Treiber (`aqc_send_ctrl_data`) als Feature-Report per `HIDIOCSFEATURE`
  gesendet (`send_commit`). Der Konstruktor-Schalter `commit_as="output"` sendet ihn stattdessen per `os.write`
  als Output-Report, wie TimSC/quadroctl es tut; damit lässt sich umschalten, falls der Hardware-Test es
  verlangt. Beim Hardware-Test (§7) prüfen wir zusätzlich, ob die Änderung ohne Aquasuite wirksam wird; das ist der
  bekannte Bug bei Firmware 1033.
- Input-Reports werden per blockierendem `os.read` mit Timeout (`select`) gelesen.
- Ein Prozess-Lock serialisiert alle Steuer-Operationen, mit 200 ms Abstand zwischen ihnen, wie im
  Kernel-Treiber.

### 3.3 device.py – Schreibablauf

`apply(mutator)`:
1. Feature `0x03` frisch lesen und die CRC prüfen. Bei falscher CRC abbrechen.
2. Den gelesenen Report als Backup speichern (`backups.py`).
3. `mutator(settings)` aufrufen, dann `validate.check(alt, neu)` (§4). Bei einem Verstoß abbrechen, ohne
   etwas zu schreiben.
4. Mit `encode_settings` den neuen Report bauen. Wenn er identisch zum alten ist, nichts tun.
5. Feature `0x03` schreiben, danach den Commit `0x02` (als Feature-Report, siehe §3.2).
6. Feature `0x03` erneut lesen und Byte für Byte mit dem Soll vergleichen. Erlaubt sind nur Abweichungen in
   Feldern, die das Gerät selbst pflegt; die Liste dieser Felder wird beim Hardware-Test ermittelt.
7. Wenn der Vergleich fehlschlägt, das Backup zurückschreiben und den Fehler melden.

Geschrieben wird **nur** auf ausdrückliche Nutzeraktion („Speichern“, „Wiederherstellen“) oder bei einem
Zeitplan-Wechsel (§3.4a), nie periodisch. Das schont den Flash.

### 3.4b Weitere Temperaturen (sensors.py) – nur Anzeige

- **Host:** `sensors.py` liest alle `/sys/class/hwmon/hwmon*/temp*_input` außer `name == quadro`, dessen
  Daten aus hidraw kommen.
  - Kennung ist `"<name>/<label>"`, z. B. `k10temp/Tctl` oder `nvme/Composite`. Gibt es kein Label, gilt
    `temp1` usw.
  - Lesefehler (`ENODATA`) überspringt er.
  - Die Config kann Sensoren umbenennen und ausblenden: `host_sensors: {"k10temp/Tctl": "CPU",
    "spd5118/temp1": null}`, wobei `null` ausblendet.
  - Gelesen wird im Monitor-Takt; die Werte kommen mit in den Verlauf.
- **GPUs aus VM 103 (Push):**
  - Die RTX 3090 hängen per Passthrough an `vfio-pci` und sind auf dem Host unsichtbar.
  - In VM 103 läuft ein systemd-Timer (`deploy/push-gpu/`) alle 5 s: `nvidia-smi --query-gpu=index,
    temperature.gpu,power.draw,utilization.gpu --format=csv,noheader,nounits` → `POST /api/external`.
  - Authentifiziert wird mit einem eigenen Bearer-Token je Quelle. In `daemon.json` steht nur der
    SHA-256-Hash unter `push_tokens: {"llm-vm": "<hash>"}`.
  - Dem TLS-Zertifikat vertraut die VM über die PVE-CA (`/etc/pve/pve-root-ca.pem`, `curl --cacert`).
  - Body: `{"source": "llm-vm", "sensors": [{"id": "gpu0", "label": "GPU 0", "value": 45.0, "unit": "°C"},
    …]}`, höchstens 32 Sensoren. Erlaubte Einheiten: °C, W und %.
  - Die Werte liegen nur im Speicher. Älter als 30 s gelten sie als „keine Daten“, z. B. wenn VM 103 aus ist.
  - Temperaturen kommen mit in den Verlauf.
  - Ein Token darf nur unter seiner eigenen `source` schreiben.

### 3.4a schedule.py – LED-Zeitplan

- **Regeln** stehen in der Config: Liste von `{ "time": "HH:MM", "days": [0..6] (optional, Standard täglich),
  "target": "strip" | "led:N", "on": bool, "brightness": 0..255 (optional) }`.
- **Soll-Zustand je Ziel:** die letzte Regel, deren Zeitpunkt vor jetzt liegt. Die Berechnung greift über
  Mitternacht und Wochentage zurück und ist eine reine Funktion `desired_state(rules, now)`, die sich ohne
  Gerät testen lässt.
- **Scheduler-Thread:** prüft jede Minute und beim Start. Wenn der Soll-Zustand vom Gerät abweicht,
  folgt genau ein `device.apply` mit allen fälligen Änderungen. Ein verpasster Wechsel, etwa weil der
  Daemon um 01:00 aus war, wird beim Start nachgeholt.
- **Manuelle Übersteuerung:** In der Oberfläche lassen sich „LEDs jetzt an/aus“ bis zur nächsten Regel
  setzen. Die Übersteuerung liegt nur im Speicher und verfällt mit der nächsten Regel.
- **Zeitzone:** die des Hosts (Europe/Berlin).
- **Ziel `strip`:** nutzt die bekannten Felder Strip-Helligkeit (Payload 393) und Strip-Flags (394, Bit
  `0x0002` = aus). Das funktioniert sofort.
- **Ziel `led:N`:** Helligkeit oder An/Aus je LED-Controller, also je Gerät. Verfügbar nur, wenn §5 die
  Felder dafür eindeutig entschlüsselt. Bis dahin lehnt die Config solche Regeln ab.
- **Schreiblast:** typisch 2 Schreibvorgänge pro Tag, vernachlässigbar für den Flash.
- **Backups:** Zeitplan-Schreibvorgänge legen kein Backup an, da sie nur bekannte LED-Felder ändern. So
  werden Nutzer-Backups nicht verdrängt. Prüfung per Zurücklesen und Rollback gilt trotzdem.

### 3.4 monitor.py

- Liest Input-Reports in einem eigenen Thread.
- Hält den letzten Zustand und einen Ringpuffer: 6 h mit 10-s-Mittelwerten, rund 2200 Punkte, nur im
  Speicher.
- Fehlt das Gerät (abgesteckt, von der VM belegt), meldet der Zustand `offline` und der Thread versucht es
  alle 5 s erneut.

### 3.5 web.py

- `ThreadingHTTPServer` mit `ssl`. Zertifikat und Schlüssel sind die von PVE (`/etc/pve/local/pve-ssl.*`)
  und werden per systemd `LoadCredential=` bereitgestellt, sodass der Dienst-User keinen Zugriff auf
  `/etc/pve` braucht.
- HTTP Basic Auth. Der Passwort-Hash (PBKDF2-SHA256) steht in `daemon.json`. Ein CLI-Befehl
  `python3 -m aquacontrol set-password` erzeugt ihn.
- Lauscht standardmäßig auf `0.0.0.0:8443`.
- API (JSON):
  - `GET /api/status`: aktueller Zustand (QUADRO, Host-Sensoren, externe Sensoren)
  - `POST /api/external`: Push externer Sensoren, mit Bearer-Token statt Basic-Auth (§3.4b)
  - `GET /api/history?minutes=N`: Verlauf
  - `GET /api/settings`: dekodierte Einstellungen (Lüfter, Kurven, LEDs, Strip) samt Namen aus der Config
  - `PUT /api/settings/fan/{1-4}`: Modus, Fest-%, Zieltemperatur, Sensor, 16 Kurvenpunkte, Min/Max
  - `PUT /api/settings/led/{1-8}`: die LED-Felder, die nach §5 bekannt sind
  - `PUT /api/settings/strip`: Helligkeit, An/Aus
  - `GET/PUT /api/schedule`: Zeitplan-Regeln (werden in die Config geschrieben)
  - `POST /api/schedule/override`: LEDs jetzt an/aus bis zur nächsten Regel
  - `GET /api/backups`, `POST /api/backups/{id}/restore`
- Schreib-Endpunkte geben das Ergebnis von `device.apply` zurück: ok oder Fehler, sowie den Backup-Namen.

### 3.6 Oberfläche

Eine Seite mit drei Bereichen:
- **Übersicht:** Wassertemperatur, Durchfluss, je Lüfter Drehzahl und %. Dazu CPU, NVMe, RAM und GPU 0/1
  (mit „keine Daten“, wenn veraltet). Verlauf als SVG-Liniendiagramm mit wählbaren Kurven.
- **Lüfter:** je Kanal der Modus (Fest / Zieltemperatur / Kurve). Bei Zieltemperatur ein Eingabefeld für
  °C, bei Kurve ein Kurveneditor (16 ziehbare Punkte in SVG plus Tabelle). Min/Max sind editierbar, der
  aktuelle Arbeitspunkt wird angezeigt. PID-Parameter (P/I/D) und Fallback werden nur angezeigt.
- **LEDs:** je belegtem Controller mit Gerätename (z. B. „Radiator 420“), LED-Bereich, Modus und den
  bekannten Feldern (Farben, Schwellwerte, Sensor) mit Vorschau. Dazu Helligkeit und An/Aus des Strips.
- **Zeitplan:** Tabelle der Regeln (Uhrzeit, Tage, Ziel, An/Aus, Helligkeit) zum Hinzufügen und Löschen.
  Angezeigt werden außerdem der aktuelle Soll-Zustand und der nächste Wechsel, dazu Buttons
  „jetzt an/aus“.
- Backups: Liste mit Zeitstempel und Button „Wiederherstellen“ (mit Bestätigung).

Funktioniert im Desktop-Browser. Auf dem Handy ist es lesbar, aber nicht optimiert.

### 3.7 Konfiguration

Zwei Dateien mit klar getrennten Zuständigkeiten:

**`/etc/aquacontrol/daemon.json`** (0640 root:aquacontrol, nur vom Admin gepflegt, vom Daemon nur gelesen):

```json
{ "listen": "0.0.0.0", "port": 8443, "password_hash": "pbkdf2_sha256$...",
  "push_tokens": { "llm-vm": "sha256-hex-des-tokens" } }
```

**`/var/lib/aquacontrol/config.json`** (Eigentümer aquacontrol; der Daemon schreibt sie bei Zeitplan-Änderungen
über die API, atomar per temporärer Datei und `rename`):

```json
{
  "fans": {"1": {"name": "Pumpe", "min_percent": 25}, "2": {"name": "140mm Radiator"},
           "3": {"name": "420mm Radiator"}, "4": {"name": "Gehäuselüfter"}},
  "sensors": {"1": "Wasser Temp"},
  "host_sensors": {"k10temp/Tctl": "CPU", "nvme/Composite": "NVMe"},
  "leds": {"1": "…", "2": "…"},
  "schedule": [
    {"time": "01:00", "target": "strip", "on": false},
    {"time": "09:00", "target": "strip", "on": true, "brightness": 218}
  ],
  "backup_dir": "/var/lib/aquacontrol/backups", "backup_keep": 50
}
```

Die Namen in `leds` kommen aus §5. Fehlt die Datei, legt `install.sh` sie mit den Standardwerten an. Die
Beispielregeln im Zeitplan oben sind **nicht** vorinstalliert: der Standard-Zeitplan ist leer (`"schedule": []`),
damit ein frisch installierter Dienst nie von sich aus auf das Gerät schreibt, bevor jemand Regeln anlegt.

## 4. Sicherheitsregeln (validate.py)

- Kurven-Temperaturen streng aufsteigend, Bereich 0–100 °C.
- Prozentwerte 0–100.
- Lüfter mit `min_percent` (die Pumpe, 25 %): Das Geräte-Minimum (FanConfig `min`) darf nicht darunter
  gesetzt werden. Ebenso wenig darf ein Fest-Wert darunter liegen. Kurvenpunkte sind frei wählbar:
  Das Gerät bildet die Kurvenprozente linear auf Minimum–Maximum ab (0 % = Minimum, 100 % = Maximum; auf der
  Hardware bestätigt: Kurve 4,31 % bei Min 28,02 % / Max 90,3 % ergibt 30,7 % Ausgang). Die Ausgabe liegt
  damit immer im Bereich [Minimum, Maximum]. Die Oberfläche zeigt dafür einen Hinweis.
- Min < Max, beides 0–100.
- Zieltemperatur 20–60 °C.
- Modus nur aus {Fest, Zieltemperatur, Kurve}. „Folgen“ (4) bleibt unverändert, wenn es schon gesetzt ist,
  ist aber nicht neu wählbar.
- Strip-Helligkeit 0–255.
- Kurvensensor nur aus den physischen Sensoren 1–4 (Index 0–3). Den Index für den Durchfluss haben wir
  nicht verifiziert, er wird abgelehnt, sofern er nicht schon gesetzt ist.
- Änderungen an Bytes außerhalb der bekannten Felder sind unmöglich, weil `encode` sie aus `base` kopiert.

## 5. LED-Reverse-Engineering (Teil der Umsetzung, vor dem LED-Editor)

Das Layout von `LedControllerConfig` ist nur teilweise bekannt (Bereich, Modus, Flags; der Rest liegt roh
vor). Vorgehen:
1. VM 100 einmalig mit `vga: std` starten, die QXL-Bluescreens sind damit behoben. Die Aquasuite sieht
   den QUADRO per Passthrough.
2. Der Nutzer ändert in der Aquasuite jeweils **eine** LED-Einstellung, z. B. Schwelle Gelb, Farbe Grün,
   Sensor oder Helligkeit. Zuerst wird die Zuordnung Controller → Gerät geklärt: einen Controller
   abschalten und schauen, welches Gerät dunkel wird. Danach folgen Helligkeit und An/Aus je Controller,
   die der Zeitplan mit Ziel `led:N` braucht.
3. Während der Sitzung läuft auf dem Host ein usbmon-Mitschnitt (`tcpdump -i usbmon3`). Jeder
   Speichervorgang der Aquasuite erscheint darin als SET_REPORT `0x03` mit dem kompletten neuen Profil. Ein
   Skript extrahiert alle geschriebenen Profile und vergleicht aufeinanderfolgende. Das XML ist dafür
   ungeeignet, weil es veraltet sein kann (§2). Nebenbei sehen wir, mit welchem Report die Aquasuite das
   Speichern bestätigt (Feature oder Output); das klärt das Risiko bei Firmware 1033 aus §9.
4. Die Ergebnisse kommen nach `docs/led-layout.md` und als benannte Felder in `protocol.py`.
5. Der LED-Editor bietet nur Felder an, die so eindeutig entschlüsselt sind. Alles andere bleibt roh und
   unveränderlich.

## 6. Deployment

- Code unter `/opt/aquacontrol`, Konfiguration wie in §3.7, Backups unter `/var/lib/aquacontrol/backups`.
- System-User `aquacontrol`.
- udev-Regel `/etc/udev/rules.d/70-aquacontrol.rules`:
  `SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0c70", ATTRS{idProduct}=="f00d", GROUP="aquacontrol", MODE="0660"`
- systemd-Unit `aquacontrol.service`:
  - `User=aquacontrol`, `StateDirectory=aquacontrol`
  - `LoadCredential=` für Zertifikat und Schlüssel
  - Härtung: `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`, `NoNewPrivileges=yes`,
    `DeviceAllow=char-hidraw rw`, `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`
  - `Restart=on-failure`
- `deploy/install.sh` ist idempotent: rsync nach `/opt/aquacontrol`, User, udev, Unit, Restart.
- Entwicklung lokal in `~/Documents/coding/proxmox/aquacontrol` (Git), Deploy per rsync über SSH.

## 7. Tests

- **Unit (ohne Hardware), `python3 -m unittest`:**
  - CRC der 4 echten Profile stimmt.
  - Decode/Encode-Roundtrip ist Byte-identisch.
  - Bekannte Werte werden korrekt gelesen, z. B. Lüfter 1: Kurve von 22 °C/15 % bis 43 °C/90 %.
  - Input-Report-Fixtures werden dekodiert, z. B. Wassertemperatur 30,12 °C.
  - Validierung: jede Regel mit einem positiven und einem negativen Fall.
  - Zeitplan: `desired_state` über Mitternacht, mit Wochentagsfiltern, ohne Regeln, mit Übersteuerung und
    beim Nachholen nach Ausfall. Der Scheduler schreibt nur bei Abweichung (Fake-Gerät).
  - `device.apply` gegen ein Fake-Transport: Backup wird angelegt, Abbruch bei falscher CRC, Rollback bei
    gescheitertem Vergleich, kein Schreiben ohne Änderung.
  - Web-API gegen ein Fake-Gerät: Auth, Fehlerfälle.
- **Hardware, in dieser Reihenfolge, jeweils mit Freigabe:**
  1. Nur lesen: Dekodierte Live-Werte stimmen mit hwmon überein, die Einstellungen stimmen mit dem
     Aquasuite-Profil überein.
  2. Unverändertes Profil zurückschreiben (No-op-Write mit Commit): Der Vergleich passt, das Gerät läuft
     normal weiter.
  3. Kleine echte Änderung, z. B. den letzten Kurvenpunkt von Lüfter 4 um 1 % anheben: Sie wirkt sich auf
     die Drehzahl aus und überlebt das Ab- und Anstecken. Danach zurücksetzen.
  4. Wiederherstellung aus Backup.
  5. Zeitplan: Testregel für 2 Minuten in der Zukunft „Strip aus“, dann „an“. Die LEDs reagieren.

## 8. Umstellung

1. LED-Reverse-Engineering (§5) abschließen, solange die VM noch existiert.
2. VM 100: `usb0` entfernen, `onboot: 0`. Die VM bleibt gestoppt erhalten, um zurückwechseln zu können.
3. aquacontrol installieren, die Hardware-Tests aus §7 durchführen.
4. Host-Neustart-Test: Danach hat hwmon/aquacontrol das Gerät, die VM startet nicht.
5. Rückweg dokumentieren: `qm set 100 -usb0 host=0c70:f00d && qm start 100`.

## 9. Risiken

| Risiko | Gegenmaßnahme |
| --- | --- |
| Bug bei Firmware 1033: Schreibvorgänge werden ohne Aquasuite nicht wirksam (Issue #113) | Hardware-Test 2/3 deckt es früh auf. Fallback: Output-Report statt Feature-Report fürs Commit (`commit_as="output"`, wie TimSC), sonst Analyse per usbmon-Mitschnitt eines Aquasuite-Speichervorgangs. |
| Falsches Byte zerstört die Konfiguration | Nur bekannte Felder werden gepatcht, CRC-Prüfung, Backup vor jedem Schreiben, Vergleich mit Rollback, live gelesenes Profil vom 2026-10-04 als erstes Backup. |
| Pumpe auf 0 % | `min_percent`-Regel, durchgesetzt im Server. |
| Flash-Verschleiß | Schreiben nur auf Nutzeraktion oder Zeitplan-Wechsel (ca. 2/Tag), kein Schreiben ohne Änderung. |
| Strip-Flag `0x0002` bedeutet nicht „aus“ wie erwartet | Hardware-Test 5. Fallback: Helligkeit 0 als „aus“. |
| Push-Endpunkt wird missbraucht | Eigenes Token je Quelle (nur als Hash gespeichert), Größen- und Wertgrenzen, nur Anzeige ohne Wirkung aufs Gerät. |
| Daemon zur Schaltzeit nicht aktiv | Nachholen beim Start; der Zustand ist im Gerät gespeichert, LEDs bleiben bis dahin im letzten Zustand. |
| Web-Oberfläche auf dem Hypervisor | Härtung per systemd, eigener User ohne Root-Rechte, TLS und Passwort, nur Zugriff auf hidraw. |
| Konflikt zwischen hwmon-Treiber und hidraw | Der Daemon liest pwm nicht über sysfs, damit keine konkurrierenden Feature-Reads entstehen. Steuer-Operationen laufen im Daemon über ein Lock. |
