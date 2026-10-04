# QUADRO LED controller layout (firmware 1033)

How the layout was found: aquasuite X.84 ran in a VM with the QUADRO passed through. It was driven over QMP
(screenshots and mouse input), and the host recorded the USB traffic with usbmon. Each SET_REPORT 0x03 that aquasuite
wrote was diffed against the previous one with `tools/usbmon_reports.py`, one UI change per step.
Field names follow medevil84/FanControl.AquacomputerDevices (`DataStructs/Quadro.cs`). Fields marked **confirmed**
were observed changing in a capture.

## Strip (settings payload offsets)

| Payload offset | Type | Meaning | Status |
| ---: | --- | --- | --- |
| 393 | u8 | Strip brightness 0–255 (aquasuite shows 218 as 85 %) | confirmed |
| 394–395 | u16be | Strip flags, bit `0x0002` = strips off (aquasuite "Strips" toggle) | confirmed |

## LED controller entry

There are 8 entries of 70 bytes, the first at payload offset 396: `base(i) = 396 + 70*i`. Add 1 to get the absolute
report offset.

| Entry offset | Type | Meaning | Status |
| ---: | --- | --- | --- |
| +0 | u8 | Strip index (always 0 on the QUADRO) | |
| +1 | u8 | First LED (0-based) | high (UI shows 1–30 for 0/30) |
| +2 | u8 | LED count | high |
| +3 | u8 | Effect mode: `0x00` unused, `0x01` static colour, `0x12` **Farbschalter** (colour switch by data source) | confirmed for 0x12 and 0x01 |
| +4–5 | u16be | Flags: `0x0001` Überblenden (fade), `0x0002` Blinken (blink), `0x4000` Helligkeit nach Datenquelle | confirmed |
| +6–7 | s16be | Data source: 0 = temp sensor 1 ("Wasser Temp"), 1–3 = temp sensors 2–4, 4 = flow, presumably 5–20 = software sensors 1–16 and then fan rpm/% pairs; `0xFFFF` = none | confirmed for 0, 4, 0xFFFF |
| +8–9 | u8[2] | Source filter / damping ("Dämpfung"), `0a 0f` | unknown, keep as-is |
| +10–15 | s16be x1, s16be x2, u8 y1, u8 y2 | Binding 1: source range x1..x2 (temperature in whole °C, 20..70; flow 0..300 when the source is flow) mapped to y1..y2 | x1/x2 confirmed (changed with source) |
| +16–21 | same | Binding 2, identical values in all captures | |
| +22–45 | s16be[12] | Effect values. For Farbschalter: `[0]` = number of thresholds, `[2]` = threshold 1 (°C), `[3]` = threshold 2 (°C), … ; `[7]` = 70 (scale end shown in UI) | thresholds confirmed (+26/27, +28/29) |
| +46–69 | 6 × {u16be h, u8 s, u8 v} | Palette: hue 0..1535 (red 0, yellow 255, green 511, blue 1023), saturation and value 0..255. Farbschalter uses `thresholds + 1` colours, from coolest to hottest | hue confirmed (green 0x01ff → blue 0x03ff) |

## The user's configuration (2026-10-04)

- **C1, LEDs 1–30:** Farbschalter on "Wasser Temp". Green below 35 °C, yellow from 35 to 45 °C, red from 45 °C.
- **C2, LEDs 31–45:** static colour, green (h=511).
- **C3–C8:** unused. C7/C8 hold leftover blink-like values but are not shown in aquasuite.

## Commit

aquasuite sends output report 0x02 (the 11-byte commit) about 4 s after each settings write, and it saves only through
that commit. aquacontrol sends the same bytes as a feature report, as the kernel driver does. Both persist: a
power-cycle test confirmed it (power-up counter 220 → 221, settings kept).

## Safe-editing rules derived from this

- Editable in a UI:
  - mode 1 colour (palette entry 0)
  - Farbschalter thresholds (values[2..1+n], strictly increasing, inside the binding range)
  - the n+1 palette colours
  - flags 0x0001, 0x0002 and 0x4000
  - the data source among the known indices
- Do not touch: +8..+21 (filter, bindings), values other than the thresholds, and unused entries.
