<p align="center">
  <img src="echolot/logo.png" alt="Echolot" width="440">
</p>

<p align="center">
  Wi-Fi CSI presence detection for Home Assistant —<br>
  build the firmware, flash it from the browser, group the sensors into zones.
</p>

---

Echolot is a Home Assistant add-on that turns inexpensive ESP32 boards into
presence sensors. It reads Channel State Information: the way a body moving
through a room disturbs the Wi-Fi signal between the board and the router.
Detection therefore needs no wearable, no phone, no camera, and no line of
sight — the sensor can sit behind furniture.

The sensing itself is [ESPectre][espectre], an ESPHome component. Echolot is
everything around it: per-device firmware builds, browser-based flashing,
over-the-air updates, zones, calibration, a live dashboard, and MQTT
discovery so each zone arrives in Home Assistant as an occupancy sensor.

## Installation

In Home Assistant, open **Settings → Add-ons → Add-on Store → ⋮ →
Repositories** and add:

```
https://github.com/NacoTeX/echolot
```

Then install **Echolot** and start it; the interface appears in the sidebar.

Requires 64-bit Home Assistant OS or Supervised (`aarch64` or `amd64`).
Espressif ships no ESP-IDF toolchain for 32-bit hosts, so `armv7` is not
supported. The first firmware build downloads roughly 2 GB of ESP-IDF and
cross toolchain into `/data/platformio`, where it stays.

## What it does

- **Firmware from the browser.** Choose a board, enter Wi-Fi credentials,
  press build. Echolot renders an ESPHome configuration, compiles it, and
  serves the result as an [ESP Web Tools][ewt] manifest, so flashing runs
  over USB from Chrome or Edge with no toolchain on your machine. Later
  updates go over the network.
- **Zones.** A zone groups devices into one presence state, with a hold time
  and optional enter/exit thresholds, and is published to Home Assistant over
  MQTT discovery.
- **Calibration.** Record a session from a device, label what was happening,
  and let Echolot learn what the room does when it is empty.
- **Presence from the crossing rate.** Motion detection reacts in a second
  and switches lights on; it cannot see somebody sitting still. Measured over
  a minute, a still-occupied room crosses its threshold about ten times as
  often as an empty one, and Echolot uses that as a second, slower signal
  that keeps a zone occupied. See [DOCS.md][docs].
- **Dashboard.** Movement score against detection threshold per device,
  pre-filled from Home Assistant's recorder, plus a fused second opinion per
  zone that reports disagreement instead of hiding it.

## Scope and limits

Stated plainly, because CSI sensing is easy to oversell:

- **Rooms, not positions.** At 20 MHz channel bandwidth the range resolution
  is about 15 m, and a single antenna gives no direction. Echolot answers
  "is this room occupied", not "where in the room".
- **The slow signal needs calibration.** Rate-based presence requires a
  recording of at least ten minutes of the empty room. Without one, a device
  contributes nothing to it rather than guessing.
- **Measured in one room, on one device, so far.** The numbers in
  [DOCS.md][docs] come from real recordings on real hardware, and that is
  the whole sample.
- **The direct BLE telemetry path is untested on hardware.** It is
  implemented against ESPectre's documented protocol; no BLE-capable browser
  was available to exercise it.
- **No native Matter.** Once a zone is a Home Assistant entity, Home
  Assistant's own HomeKit and Matter bridges export it — a smaller and more
  reliable path than commissioning Matter in here.

## Documentation

- [`echolot/DOCS.md`][docs] — configuration, zones, calibration, dashboard,
  troubleshooting, and why things work the way they do
- [`echolot/CHANGELOG.md`](echolot/CHANGELOG.md) — what changed, and why

## Development

```sh
cd echolot
pip install -r app/requirements.txt -r requirements-dev.txt
python -m pytest tests -q          # unit tests
python tools/validate_firmware.py  # every board through `esphome config`
python tools/check_metadata.py     # add-on manifest sanity
```

All three run in CI on every pull request. `tools/make_brand.py` regenerates
the icon and logo.

## Licence and affiliation

GPL-3.0-or-later, matching [ESPectre][espectre], whose component this builds
on. See [`LICENSE`](LICENSE).

Echolot is an independent project, not affiliated with ESPectre's
maintainers, with Home Assistant, or with [TOMMY][tommy] — the commercial
CSI presence sensor whose closed firmware and per-device licence prompted
this one. No claim of feature parity is made.

[espectre]: https://github.com/francescopace/espectre
[ewt]: https://esphome.github.io/esp-web-tools/
[docs]: echolot/DOCS.md
[tommy]: https://www.tommysense.com
