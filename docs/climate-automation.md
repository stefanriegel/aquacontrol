# Climate automation (Home Assistant)

Optional. aquacontrol can switch a room air conditioner on through Home Assistant when the water cooling runs at
its limit for a while, and switch it off again once the water is clearly cooler. It is configured in the **Klima**
tab and is **disabled by default**.

The design document (German) is [design/climate-design.md](design/climate-design.md).

## Behaviour

| | Rule (all values configurable) |
| --- | --- |
| **Switch on** | Water (sensor 1) ≥ 40 °C **and** every selected fan channel (default: the two radiators) ≥ 85 %, uninterrupted for 5 min, **and** Home Assistant reports the AC as `off`. aquacontrol then sets HVAC mode, target temperature, preset, fan mode and both louvre positions. |
| **Switch off** | Only an AC that aquacontrol switched on itself and still "owns": water ≤ 36 °C for 10 min **and** at least 30 min runtime. |
| **Anti-hunting** | ≥ 2 °C gap between on and off temperature, 30 min minimum runtime, 15 min lockout after switching off, at most 2 switch-ons per hour. The limit never blocks switching off. |

### Manual operation always wins

- **Running AC:** if the AC is already running, aquacontrol does nothing.
- **Hand-over:** if someone changes state, target temperature or preset of an AC aquacontrol owns, aquacontrol hands
  over and stops switching. It hands over after two consecutive differing reads (±0.25 °C, preset case-insensitive).
- **Manual off:** if someone switches the AC off, aquacontrol pauses until the water has cooled down to the off
  temperature, at most for `manual_off_pause_minutes` (default 120).
- **Restart:** after a daemon restart, a running AC counts as foreign.

### Confirmation and errors

- **Confirmation:** after switching on, aquacontrol waits up to 10 s for Home Assistant to show the requested values,
  because cloud integrations lag. If they don't show up, the switch-on is marked "unconfirmed" and re-checked on the
  next cycles.
- **QUADRO offline or water temperature unknown:** aquacontrol never switches on. An AC it owns stays on.
- **Home Assistant unreachable or a service call fails:** aquacontrol retries after 1, 5 and then 15 minutes. The last
  20 events are shown in the tab.

## Setup

1. Create a **Long-Lived Access Token** in your Home Assistant profile.
2. In the Klima tab, enter the Home Assistant URL and the token, then save.
3. Press **Auswahl neu laden**. The entity and option drop-downs are filled from Home Assistant: climate entities,
   swing select entities, HVAC modes, presets, fan modes and louvre positions.
4. Pick the entities and values, press **Verbindung testen** (read-only, it never switches anything) and enable the
   automation.

## Token and URL handling

- **Storage:** the token is stored in `secrets.json` next to `config.json` (mode 0600). It is never logged and never
  returned by the API, which only reports `token_set`.
- **Changing the URL without a new token deletes the stored token**, so the token is never sent to a different host.
- **URL format:** use `http://`, or `https://` with a publicly trusted certificate. A self-signed Home Assistant
  certificate is rejected. Credentials in the URL are rejected.
- **Plain http:** the token travels unencrypted, so use it only on a trusted network.

## API

- `GET /api/climate`: configuration (without the token), `token_set`, status and events.
- `PUT /api/climate`: partial update. `"token"` sets the token and `""` clears it.
- `POST /api/climate/test`: reads the entity and both selects. It never switches anything.
- `GET /api/climate/options`: what Home Assistant offers for the drop-downs, with built-in fallbacks.
