# aquacontrol – Klima-Automatik (Spec-Ergänzung)

Stand: 2026-10-04 · Status: vom Nutzer im Chat freigegeben („passt, mach so“)

## 1. Ziel

Läuft die Wasserkühlung dauerhaft am Limit, schaltet aquacontrol die Raum-Klimaanlage über Home Assistant ein.
Wenn das Wasser wieder deutlich kühler ist, schaltet es sie wieder aus. Sie darf dabei nicht pendeln. Handbetrieb hat
immer Vorrang.

## 2. Home Assistant (Ist-Stand, 2026-10-04 gelesen)

- **HA-Instanz:** `http://<ha-host>:8123`. Die REST-API mit Long-Lived-Token ist vom PVE-Host erreichbar.
- **Klima-Entity:** `climate.panasonic_ac_panasonic_ac`
  - hvac_modes: off, heat_cool, cool, heat, fan_only, dry
  - Temperatur 16–30 °C in Schritten von 0,5
  - fan_modes: Automatic, 1–5
  - preset_modes: Normal, Powerful, Quiet
  - swing_modes: off, both, vertical, horizontal (nicht verwendet)
- **Lamellen fest positionieren** (Select-Entities am selben Gerät):
  - `select.panasonic_ac_panasonic_ac_horizontal_swing_mode`: auto, left, left_center, center, right_center, right
  - `select.panasonic_ac_panasonic_ac_vertical_swing_mode`: swing, auto, up, up_center, center, down_center, down
- **Services:**
  - `climate.set_hvac_mode`, `climate.set_temperature`, `climate.set_preset_mode`, `climate.set_fan_mode`, `climate.turn_off`
  - `select.select_option`

## 3. Verhalten

### Eingaben
Kommen aus dem Monitor-Snapshot:
- Wassertemperatur: Sensor 1 des QUADRO
- Lüfterleistung %: Kanäle 2 und 3 (Radiatoren), konfigurierbar

### Einschalten
Alle Bedingungen müssen gleichzeitig gelten:
- Die Automatik ist aktiviert, und Token und Entity sind gesetzt.
- Der QUADRO ist online, die Wassertemperatur ist bekannt.
- Wasser ≥ `on.water_c` (Standard **40 °C**) **und** jeder Kanal aus `on.fan_channels` (Standard [2, 3]) ≥ `on.fan_percent`
  (Standard **85 %**). Beides gilt ununterbrochen seit `on.minutes` (Standard **5 min**).
- Die Klimaanlage ist laut HA **aus** (`state == "off"`). Läuft sie bereits, gilt Handbetrieb, und aquacontrol unternimmt nichts.
- Die Sperrzeit nach dem letzten eigenen Ausschalten ist vorbei (`min_off_minutes`, Standard **15 min**).
- In den letzten 60 Minuten gab es weniger als `max_switches_per_hour` eigene Schaltvorgänge (Standard **2**).

### Einschaltaktion
Reihenfolge:
1. `set_hvac_mode(cool)`
2. `set_temperature(20)`
3. `set_preset_mode(Quiet)`
4. `set_fan_mode(Automatic)`
5. `select_option(horizontal, left)`
6. `select_option(vertical, down_center)`

Alle Werte sind konfigurierbar. Danach liest aquacontrol den Zustand und merkt sich einen **Fingerabdruck** dessen, was es
gesetzt hat: hvac-Zustand, Zieltemperatur, Preset. Ab dann gilt **Besitz = aquacontrol**.

### Besitz abgeben
Solange aquacontrol im Besitz ist, wird bei jedem Zyklus geprüft:
- Weicht der HA-Zustand vom Fingerabdruck ab, hat jemand etwas geändert, auch durch Ausschalten. Dann gibt aquacontrol
  den Besitz ab, schaltet nichts mehr und protokolliert „Handbetrieb übernommen“.
- Lamellen-Selects zählen nicht zum Fingerabdruck. Die Cloud meldet sie teils verzögert oder anders zurück.

### Ausschalten
Nur im Besitz und wenn alle Bedingungen gelten:
- Wasser ≤ `off.water_c` (Standard **36 °C**), ununterbrochen seit `off.minutes` (Standard **10 min**).
- Die Laufzeit seit dem eigenen Einschalten ist ≥ `min_on_minutes` (Standard **30 min**).
- Das Schaltlimit pro Stunde ist nicht erreicht. Das Ausschalten zählt als Schaltvorgang, darf aber nie durch das Limit
  blockiert werden; das Limit gilt nur fürs Einschalten.

Aktion: `climate.turn_off`. Danach ist der Besitz beendet, und die Sperrzeit beginnt.

### Fehlende Daten und Fehler
- **QUADRO offline oder Wasser unbekannt:** Es wird nie eingeschaltet. Eine laufende eigene Klimaanlage bleibt an, und die
  Ausschalt-Uhr wird zurückgesetzt.
- **HA nicht erreichbar oder Service-Fehler:**
  - `last_error` wird gesetzt, und das Ereignis kommt ins Protokoll.
  - Erneute Versuche frühestens nach 1, dann 5, dann 15 Minuten (Backoff). Erfolg setzt den Backoff zurück.
  - Ein halb ausgeführtes Einschalten zählt trotzdem als Schaltvorgang. Besitz wird nur übernommen, wenn danach `state != off` ist.
- **Daemon-Neustart:** Besitz und Zeiten liegen nur im Speicher und gehen verloren. Nach einem Neustart ist eine laufende
  Klimaanlage damit „fremd“ und wird nicht automatisch ausgeschaltet. Die sichere Seite ist Kühlen statt Pendeln.

### Takt
Die Zustandsmaschine läuft alle 30 s in einem eigenen Thread mit injizierbarer Uhr. Die HA-Abfrage geschieht nur, wenn sie
für eine Entscheidung gebraucht wird:
- im Besitz in jedem Zyklus
- sonst nur, wenn die Einschaltbedingungen erfüllt sind

## 4. Konfiguration

### `/var/lib/aquacontrol/config.json` → `climate`

```json
{
  "enabled": false,
  "ha_url": "",
  "entity_id": "climate.panasonic_ac_panasonic_ac",
  "horizontal_select": "select.panasonic_ac_panasonic_ac_horizontal_swing_mode",
  "vertical_select": "select.panasonic_ac_panasonic_ac_vertical_swing_mode",
  "on":  {"water_c": 40.0, "fan_percent": 85.0, "fan_channels": [2, 3], "minutes": 5},
  "off": {"water_c": 36.0, "minutes": 10},
  "min_on_minutes": 30, "min_off_minutes": 15, "max_switches_per_hour": 2,
  "ac": {"hvac_mode": "cool", "temperature": 20.0, "preset": "Quiet", "fan_mode": "Automatic",
         "horizontal": "left", "vertical": "down_center"}
}
```

### Validierung
- `off.water_c < on.water_c`, mit mindestens 2 °C Abstand
- Minuten jeweils 1–240
- Prozent 0–100
- Kanäle 1–4
- Temperatur 16–30 in 0,5er-Schritten
- `hvac_mode` ∈ {cool, dry, fan_only}
- `ha_url` mit http oder https
- Entity-IDs: Format `domain.name`, Domain `climate` bzw. `select`

### Token
- Liegt in `/var/lib/aquacontrol/secrets.json` (0600, aquacontrol), Schlüssel `ha_token`.
- Wird atomar geschrieben, nie geloggt und nie von der API zurückgegeben. Die API meldet nur `token_set: bool`.

## 5. API und Oberfläche

### API
- `GET /api/climate`: Konfiguration (ohne Token, mit `token_set`) und Status:
  - Zustand: `idle | arming | owned | cooldown | disabled`
  - `reason` (deutsch)
  - Ablaufzeiten: `arming_since`, `owned_since`, `cooldown_until`, `off_condition_since`
  - `last_error`
  - die letzten 20 Ereignisse
- `PUT /api/climate`: Teil-Update der Konfiguration. Das optionale `"token"` wird in secrets geschrieben, `""` löscht es. Ein
  Ändern der Konfiguration setzt Besitz und Zeiten **nicht** zurück.
- `POST /api/climate/test`: liest die Entity über HA. Antwort: `{"ok", "state", "temperature", "preset", "swing": {...}}`
  oder ein Fehler. Dabei wird nichts geschaltet.

### Tab „Klima“
- An/Aus-Schalter und Felder für alle Werte
- Token-Feld (Passwort-Typ, leer = unverändert)
- Button „Verbindung testen“
- Statusbox und Ereignisliste

## 6. Nicht-Ziele
- Klimaanlage nach GPU- oder Host-Temperaturen steuern (nur Wasser + Lüfter)
- Mehrere Klimaanlagen
- Besitz über einen Daemon-Neustart hinaus behalten
- Lamellen „zurückstellen“ nach dem Ausschalten

## 7. Tests (ohne Netz)
- **Zustandsmaschine mit Fake-HA-Client und Fake-Uhr:**
  - Einschalten erst nach 5 min durchgehender Bedingung; eine Unterbrechung setzt die Uhr zurück.
  - Bei manuell laufender Klimaanlage passiert nichts.
  - Ausschalten erst nach 10 min ≤ 36 °C und 30 min Laufzeit.
  - Sperrzeit und Schaltlimit greifen.
  - Fremdänderung führt zur Besitzabgabe.
  - QUADRO offline: kein Einschalten, eigene Klimaanlage bleibt an.
  - HA-Fehler: Backoff greift, kein Dauerfeuer.
- **HA-Client:** gegen einen lokalen `http.server`. Geprüft werden Header, Pfade, Bodies, Timeout und Fehlermapping.
- **API:** Token wird nie zurückgegeben; Validierungsfehler geben 400; `test` schaltet nichts.
