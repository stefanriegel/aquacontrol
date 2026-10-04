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

Die Cloud-Integration zeigt das Ergebnis eines Service-Aufrufs erst nach etwa 0,5 s. Deshalb fragt aquacontrol den Zustand
alle 0,5 s ab, höchstens 10 s lang, bis er die eingestellte Betriebsart (`ac.hvac_mode`) mit der eingestellten Temperatur (±0,25 °C) und dem
Preset (ohne Groß-/Kleinschreibung) zeigt, und nimmt den Fingerabdruck aus dieser Abfrage. Zeigt HA bis dahin einen
laufenden Zustand mit anderen Werten, gilt der Besitz mit dem letzten Stand als „unbestätigt“; sobald die eingestellten
Werte erscheinen, ist er bestätigt. Zeigt HA noch `off`, ist die Anlage nicht im Besitz, wird aber in den folgenden Zyklen
(10 min lang) erneut gelesen: zeigt sie die eingestellten Werte, wird der Besitz übernommen.

### Besitz abgeben
Solange aquacontrol im Besitz ist, wird bei jedem Zyklus geprüft:
- Weicht der HA-Zustand vom Fingerabdruck ab (Temperatur mit ±0,25 °C Toleranz, Preset ohne Groß-/Kleinschreibung), hat
  jemand etwas geändert, auch durch Ausschalten. Nach **zwei aufeinanderfolgenden** abweichenden Abfragen gibt aquacontrol
  den Besitz ab, schaltet nichts mehr und protokolliert „Handbetrieb übernommen“. `unavailable`/`unknown` zählt nicht als Abweichung.
- Zeigt HA nach einem fehlgeschlagenen `turn_off` später `off`, gilt das als eigenes Ausschalten (zählt als Schaltvorgang,
  Sperrzeit), nicht als Handbetrieb.

### Von Hand ausgeschaltet
Beobachtet aquacontrol einen Wechsel auf `off`, den es nicht selbst verursacht hat, schaltet es **nicht wieder ein**, solange
die Einschalt-Bedingung ununterbrochen gilt. Das gilt für eine eigene Anlage (Besitzabgabe mit Zustand `off`) und für eine
fremde, die aquacontrol laufen sah und die jetzt `off` ist (`unavailable`/`unknown` davor zählt nicht). Status `cooldown`,
Grund „Von Hand ausgeschaltet – Automatik pausiert bis das Wasser wieder kühl ist (≤ 36.0 °C, spätestens HH:MM)“.
- Die Pause endet erst, wenn das Wasser wieder kühl ist: Wassertemperatur ≤ `off.water_c` (Abkühlschwelle, Standard 36 °C)
  in einem Durchlauf mit gültigen Daten. Eine bloße Unterbrechung der Einschalt-Bedingung (Wasser zwischen 36 und 40 °C,
  ein Lüfter langsamer, Wasser unbekannt, QUADRO offline) beendet sie nicht. Danach gilt das normale Einschalten mit voller
  `on.minutes`.
- Obergrenze: `manual_off_pause_minutes` (Standard 120, 10–480).
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
  - Erneute Versuche frühestens nach 1, dann 5, dann 15 Minuten (Backoff). Der Backoff wird erst zurückgesetzt, wenn ein
    ganzer Durchlauf ohne Fehler beendet wurde (eine gelungene Statusabfrage vor einem fehlschlagenden Service-Aufruf zählt nicht).
  - Ein halb ausgeführtes Einschalten zählt trotzdem als Schaltvorgang. Besitz wird nur übernommen, wenn danach `state != off` ist.
- **Daemon-Neustart:** Besitz und Zeiten liegen nur im Speicher und gehen verloren. Nach einem Neustart ist eine laufende
  Klimaanlage damit „fremd“ und wird nicht automatisch ausgeschaltet. Die sichere Seite ist Kühlen statt Pendeln.

### Takt
Die Zustandsmaschine läuft alle 30 s in einem eigenen Thread mit injizierbarer Uhr. Die HA-Abfrage geschieht nur, wenn sie
für eine Entscheidung gebraucht wird:
- im Besitz in jedem Zyklus
- sonst nur, wenn die Einschaltbedingungen erfüllt sind: einmal sofort, wenn das Warten beginnt (damit eine von Hand laufende
  Anlage bekannt ist und ihr Ausschalten während des Wartens, der Sperrzeit oder des Schaltlimits auffällt), dann wieder nach
  Ablauf von `on.minutes`

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
  "manual_off_pause_minutes": 120,
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
- `manual_off_pause_minutes` 10–480
- `ha_url` mit http oder https, ohne Benutzer/Passwort (`@` im Host abgelehnt). https nur mit öffentlich vertrautem Zertifikat.
- `ac.preset`, `ac.fan_mode`, `ac.horizontal`, `ac.vertical`: nur die Form (1–64 Zeichen, keine Steuerzeichen). Die gültigen Werte
  liefert das Gerät über HA (siehe `GET /api/climate/options`).
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
  Ändern der Konfiguration setzt Besitz und Zeiten **nicht** zurück. Ändert sich `ha_url` ohne neues `token` in derselben
  Anfrage, wird das gespeicherte Token gelöscht (Antwort: `token_set: false` und ein deutscher `notice`). Das Token wird
  gelöscht, bevor die neue Adresse gespeichert wird.
- `POST /api/climate/test`: liest die Entity und beide Lamellen-Selects über HA. Antwort (flach):
  `{"ok", "state", "temperature", "preset", "fan_mode", "horizontal", "vertical"}` oder ein Fehler. Dabei wird nichts geschaltet.
- `GET /api/climate/options`: liest `/api/states` und liefert `hvac_modes` (nur cool/dry/fan_only), `preset_modes`, `fan_modes`,
  `horizontal`, `vertical` (aus den `options` der beiden Selects), `climate_entities`, `select_entities` (nur Ids mit „swing“,
  sonst alle `select.*`), außerdem `fallback` (eingebaute Listen), `source` (`ha` | `fallback`) und `error`. Optional
  `?entity_id=&horizontal_select=&vertical_select=` für noch nicht gespeicherte Entities.

### Tab „Klima“
- An/Aus-Schalter und Felder für alle Werte. Wo nur feste Werte gültig sind (Entities, Betriebsart, Preset, Lüfterstufe,
  Lamellen), gibt es Auswahlfelder aus `GET /api/climate/options`, der gespeicherte Wert bleibt wählbar („(gespeichert)“).
  Lüfterkanäle sind Checkboxen mit den Lüfternamen. Frei getippt werden nur Adresse, Zahlen und Token.
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
