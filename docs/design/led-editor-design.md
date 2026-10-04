# aquacontrol – LED-Editor (Spec-Ergänzung)

Stand: 2026-10-04 · Status: vom Nutzer beauftragt („dann den LED-Editor bauen“). Grundlage: `docs/led-layout.md`.

## 1. Ziel

Die LED-Effekte im QUADRO lassen sich in der Oberfläche bearbeiten, ohne Aquasuite:
- Farbschalter: Schwellen, Farben, Datenquelle, Schalter
- statische Farbe

Außerdem lassen sich Backups mit anderen LED-Daten wieder einspielen.

## 2. Was bearbeitbar ist (v1)

Nur bestehende Controller mit Modus `0x12` (Farbschalter) oder `0x01` (statische Farbe). Unbenutzte Controller (Modus 0),
LED-Bereiche und der Modus selbst bleiben unveränderlich.

### Modus 0x12, Farbschalter
- **Schwellen** `values[2 .. 1+n]`:
  - Anzahl n = `values[0]`, änderbar von 1 bis 5. Dazu gehören n+1 Farben (Palette mit 6 Plätzen).
  - Ganze Zahlen, streng steigend, innerhalb des Quellbereichs `binding1.x1 .. binding1.x2` (beim Nutzer 20–70 °C).
  - Ändert sich n, werden die Schwellen-Slots, die dann unbenutzt sind (nur Slots 2..6, nie `values[7]`), auf 100
    gesetzt, wie die unbenutzten Plätze im Gerät. Die Palette wird gekürzt bzw. mit der letzten Farbe aufgefüllt.
- **Farben** `palette[0 .. n]`: Farbton 0–1535, Sättigung und Helligkeit 0–255. Die API nimmt sie auch als `"#RRGGBB"`
  entgegen; die Umrechnung erfolgt im Server (Ton = Grad·1536/360, gerundet, mod 1536; S und V wie HSV·255).
- **Schalter:** Überblenden `0x0001`, Blinken `0x0002`, Helligkeit nach Datenquelle `0x4000`. Andere Flag-Bits bleiben
  unverändert.
- **Datenquelle:** nur Temperatursensoren 1–4 (Index 0–3). Dieselbe Einheit, deshalb bleiben die Bindings gültig. Flow,
  Software-Sensoren und Lüfter sind nicht wählbar; ihr Format ist nicht vollständig entschlüsselt. Ist schon so eine Quelle
  gesetzt (Flow, Software-Sensor, keine), kann sie auch nicht geändert werden: nur Anzeige, Änderung nur in der Aquasuite.

### Modus 0x01, statische Farbe
- `palette[0]` ist bearbeitbar.
- Schalter: nur Überblenden und Blinken. „Helligkeit nach Datenquelle“ nur, wenn das Bit schon gesetzt ist (dann nur
  ausschaltbar); der Server lehnt das Setzen ab.

### Nie geschrieben
- +0..+3 (Strip, Start, Anzahl, Modus)
- +8..+21 (Filter, Bindings)
- `values[1]` und `values[2+n ..]`, außer beim Auf-100-Setzen nach dem Ändern von n
- Palette-Plätze jenseits von n (außer beim Auffüllen)

## 3. Protokoll und Validierung
- `protocol.py`: `LedController` bekommt dekodierte Felder `flags`, `source`, `binding1 (x1, x2, y1, y2)`, `values[12]`,
  `palette[6] (h, s, v)`; `raw` bleibt erhalten.
- `encode_settings` patcht nur die oben genannten Felder. Alle übrigen Bytes kommen aus dem aktuellen Gerätebericht.
- `validate.check` erlaubt LED-Änderungen nur innerhalb dieser Regeln.
- `Device.restore` prüft beim Wiederherstellen eines Backups weiterhin vollständig Pumpe und Lüfter. LED-Bereich,
  Strip-Felder, Profilnummer und Sensor-Offsets dürfen sich unterscheiden: ein Backup ist ein bekannter Gerätestand.

## 4. API und Oberfläche
- `GET /api/settings` → `leds[]` mit `index`, `name`, `led_start`, `led_count`, `mode`, `mode_name`
  (`farbschalter | statisch | unbenutzt | raw-N`), `editable`, `source`, `source_name`, `range: [x1, x2]`,
  `thresholds`, `colors: ["#rrggbb", …]`, `flags: {fade, blink, brightness_by_source}`.
- `PUT /api/settings/led/{1-8}` mit den optionalen Feldern `thresholds`, `colors`, `fade`, `blink`,
  `brightness_by_source` und `source`. Mit Backup wie bei Lüfteränderungen. Fehler geben 400.
- **Tab LEDs:**
  - pro bearbeitbarem Controller eine Karte mit Name, LED-Bereich und einer Vorschau-Leiste: Farbsegmente über den
    Quellbereich mit Schwellenmarken und dem aktuellen Wert der Datenquelle
  - für jede Farbe ein `<input type=color>`
  - Zahlenfelder für die Schwellen, Buttons „+ Schwelle“ und „− Schwelle“
  - die drei Schalter, Auswahl der Quelle, „Speichern“
  - unbenutzte Controller werden nicht angezeigt

## 5. Nicht in v1
- Zeitplan-Regeln pro LED-Gruppe (`led:N`): Es gibt kein bekanntes Ein/Aus-Bit pro Controller. Ein Aus über die
  Helligkeit 0 der Palette würde gespeicherte Farben überschreiben. Die Strip-Regeln decken den Bedarf.
- Effekte anlegen oder löschen, Modus wechseln, LED-Bereiche verschieben.

## 6. Tests
- Decode der echten Fixture (`settings_live.bin`):
  - C1: Farbschalter, Quelle 0, Bereich 20–70, Schwellen [35, 45], Farben grün/gelb/rot
  - C2: statisch grün
- Encode ändert nur die erwarteten Bytes. Geprüft an den Diffs aus der Aufzeichnung:
  - Schwelle 35 → 38 ändert +27
  - Farbe grün → blau ändert +46
  - Überblenden ändert +5
  - Quelle bleibt bei Temperatur
- Validierung: Grenzen, Reihenfolge, Anzahl, unbekannte Quelle, unbenutzter Controller, Modus 0.
- Restore eines Backups mit anderen LED-Bytes klappt; ein Backup mit Pumpen-Minimum unter dem Grenzwert wird abgelehnt.
- API und Oberfläche wie bei den bestehenden Tests.
