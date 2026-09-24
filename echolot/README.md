# Echolot

Radar room presence for Home Assistant, with the Hi-Link HLK-LD2460 on an
ESP32. Echolot builds the firmware, flashes it from the browser, puts the
sensor on a floor plan, lets you draw zones on it, and publishes every room
and zone to Home Assistant.

## What it does

**Sensors.** Pick a board, enter Wi-Fi credentials, press build. The first
flash runs over USB from Chrome or Edge; every update after that goes over
Wi-Fi from any browser. A button fills in the wiring of the Waveshare
ESP32-C5-Zero.

**Rooms.** Size, floor-plan image, furniture, and the sensor's position and
direction. The plan shows every target live, marks those behind a wall or in
an exclusion zone as not counting, and says why when a room has no current
measurement instead of calling it empty.

**Zones.** Rectangles or free shapes, drawn and reshaped on the plan.
Detection zones count people and keep an absence delay; exclusion zones hide
fans and curtains.

**Calibration.** On the live room: learn reflectors in the empty room,
align the sensor from standpoints marked on the plan, and set how long a new
target has to be reported before it counts.

**Home Assistant.** One device per room with *Anwesenheit* (occupancy) and
*Personen* (count), and another pair per detection zone, over MQTT
discovery.

## Installing

Add this repository in Home Assistant under **Settings → Add-ons → Add-on
Store → ⋮ → Repositories**:

```
https://github.com/NacoTeX/echolot
```

Then install **Echolot** and start it. The interface lives in the sidebar.

Requires a 64-bit Home Assistant OS or Supervised install (aarch64 or
amd64). The first firmware build downloads roughly 2 GB of ESP-IDF and cross
toolchain into `/data/platformio`, where it stays.

## Documentation

- [DOCS.md](DOCS.md) — wiring, the interface, counting, Home Assistant,
  troubleshooting, and what is still open
- [CHANGELOG.md](CHANGELOG.md) — what changed, and why

## Developing

```sh
pip install -r app/requirements.txt -r requirements-dev.txt
python -m pytest tests -q          # unit tests
python tools/validate_firmware.py  # every board through `esphome config`
python tools/check_metadata.py     # add-on manifest sanity
```

`tools/make_brand.py` regenerates `icon.png` and `logo.png`.

## Licence

GPL-3.0-or-later. Echolot is an independent project, not affiliated with
Hi-Link, Aqara or Home Assistant.
