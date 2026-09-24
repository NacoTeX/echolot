<p align="center">
  <img src="echolot/logo.png" alt="Echolot" width="440">
</p>

<p align="center">
  Radar room presence for Home Assistant —<br>
  build the firmware, flash it from the browser, draw your rooms and zones.
</p>

---

Echolot is a Home Assistant add-on for room presence with the Hi-Link
**HLK-LD2460**, a 24 GHz radar that reports up to five targets with their
position. Put one on an inexpensive ESP32, and Echolot does the rest:
per-device firmware builds, flashing over USB from the browser, updates
over Wi-Fi, a floor plan with live targets, zones you draw with your finger,
and every room and zone as an entity in Home Assistant.

## Installation

In Home Assistant, open **Settings → Add-ons → Add-on Store → ⋮ →
Repositories** and add:

```
https://github.com/NacoTeX/echolot
```

Then install **Echolot** and start it; the interface appears in the sidebar.

Requires 64-bit Home Assistant OS or Supervised (`aarch64` or `amd64`). The
first firmware build downloads roughly 2 GB of ESP-IDF and cross toolchain
into `/data/platformio`, where it stays.

## What it does

- **Firmware from the browser.** Choose a board, enter Wi-Fi credentials,
  press build. Echolot compiles an ESPHome firmware with its own
  `echolot_ld2460` component and serves it to [ESP Web Tools][ewt], so the
  first flash runs over USB from Chrome or Edge. Later updates go over
  Wi-Fi, from any browser — iPad included.
- **Rooms on a floor plan.** Room size, an optional floor-plan image,
  furniture, and where the sensor hangs and which way it looks. Targets
  appear live on the plan, with a short trail.
- **Zones like on the mmWave apps.** Draw rectangles or free shapes; drag
  corners, add and remove them. Detection zones count people; exclusion
  zones hide the fan and the curtain. Each zone has its own absence delay.
- **Live calibration.** Learn where the radar reports targets in the empty
  room (radiators, mirrors, metal) so targets appearing there stop
  counting; align the sensor by standing on points marked on the plan,
  which corrects its position, direction and left/right; and a
  confirmation time that keeps reflections flashing up for a report or
  two from counting.
- **Home Assistant.** Per room an occupancy sensor and a person count, and
  another pair per zone, over MQTT discovery — and from there on to
  HomeKit, Matter, Google or Alexa through Home Assistant's own bridges.

## Scope and limits

- **Tested with simulated sensors, not yet in a real room.** The firmware
  links on real toolchains and reads real frames in a host build; the room
  map, zones and MQTT export are tested against a simulated node.
- **The module has no target IDs.** Echolot follows targets from report to
  report by position. That tells a lingering person from a flash of
  reflection; it does not tell two people apart who cross paths.
- **Three hardware questions are open** — whether the LD2460 goes silent in
  an empty room, the sign of its X axis, and the layout of two
  acknowledgement frames. Each is a setting or a visible diagnostic rather
  than an assumption. See [DOCS.md][docs].

Up to 0.14 Echolot also built Wi-Fi CSI sensors through ESPectre. 1.0 is
radar only; stored CSI devices keep their identity and credentials and can
be converted in place. See [DOCS.md][docs].

## Documentation

- [`echolot/DOCS.md`][docs] — wiring, the interface, how counting works,
  Home Assistant, troubleshooting, what is open
- [`echolot/CHANGELOG.md`](echolot/CHANGELOG.md) — what changed, and why

## Development

```sh
cd echolot
pip install -r app/requirements.txt -r requirements-dev.txt
python -m pytest tests -q          # unit tests
python tools/validate_firmware.py  # every board through `esphome config`
python tools/check_metadata.py     # add-on manifest sanity
```

All three run in CI on every pull request; real firmware images are linked
on pushes to `main` and on pull requests labelled `firmware`.

## Licence and affiliation

GPL-3.0-or-later. See [`LICENSE`](LICENSE).

Echolot is an independent project, not affiliated with Hi-Link, Aqara,
Home Assistant, or ESPectre's maintainers.

[ewt]: https://esphome.github.io/esp-web-tools/
[docs]: echolot/DOCS.md
