# Changelog

## 0.12.0

**Builds were broken.** ESPectre restructured its repository in September
2026, and the `components/` folder Echolot pointed at no longer exists:

```
Could not find components folder for source.
source: github://francescopace/espectre@main
```

Everything below follows from catching up with that.

### Die Komponente

- New source. The shorthand `github://user/repo@ref` can only find a
  component folder at the repository root, so the long form with an
  explicit `path: src/cpp/frontend/esphome/components` is now the only one
  that resolves.
- The `espectre:` block was rewritten upstream. Field by field:
  `mvs`/`ml` became `lightweight`/`high_accuracy`;
  `traffic_generator_rate` became `csi_target_pps` and its range narrowed
  from 0–1000 to 1–500; `segmentation_threshold` is gone;
  `csi_traffic_mode`, `evaluation_interval_ms` and `direct_api` are new;
  `dns_tcp` joins `ping` and `dns`.
- The Calibrate **switch** is a Recalibrate **button** now — a one-shot
  action never was the right shape for a switch you have to turn off
  again. Echolot presses it accordingly.
- Entity names are no longer written into the generated YAML. They all
  have defaults upstream, and repeating them here would mean a rename
  upstream silently never reaching anyone.
- ESPectre's examples ask for ESPHome ≥ 2026.7.0, which does not exist on
  PyPI. Checked: the component itself does not enforce it and validates
  against 2026.6.5, so no ESPHome change was needed.

### BLE ist weg

ESPectre dropped its BLE telemetry channel with the restructure
("ESPHome images … no longer include BLE provisioning"). The dashboard's
**Live** button spoke that GATT service, so it and its client are removed
rather than left to fail against firmware that no longer answers. The
`esp32_ble_server` block is out of the generated firmware too, and with it
the `ble` flag in the board registry.

Its successor is ESPectre's Direct HTTP/SSE surface on port 62587, which
the reachability check now probes as a third port. Feeding the chart from
it would need proxying through the add-on to avoid a cross-origin request
— not built yet.

### Bestehende Geräte

Devices stored under the old schema are translated on load: profiles
renamed, the packet rate moved and clamped into the new range rather than
rejected, the dropped setting removed. Without that, updating would have
made every existing device unloadable.
`tests/test_espectre_migration.py` pins all of it.

- Fix: `overview.radio_load` still read the old rate field, so the
  overview's total would have crashed. The test suite caught it.

## 0.11.1

The Übersicht tab is rebuilt around what someone actually wants to know on
opening it.

- **The project-phase list is gone.** It told users which of *our* phases
  were finished, which is a fact about the repository, not about their
  house.
- The four status cards are replaced by three sections in order of use:
  **Jetzt** (every zone's live state, with the hold countdown), **Braucht
  Aufmerksamkeit** (only when something is wrong, each entry worded as the
  thing to do about it, with a link to the tab that fixes it), and
  **System** (device count, total radio load, zone export, ESPHome
  version) styled to recede.
- "Backend: online" is gone with them. If the page rendered, the backend
  is up; the card could never say anything else.
- With no devices yet, the tab is a three-step setup path rather than a
  status report about an empty system.
- Work in progress is deliberately not listed as a problem. A build that
  is still running is not a fault, and listing it trains people to ignore
  the list.
- One `/api/overview` request replaces what would have been one call per
  zone plus three per device from the browser, and it only polls while the
  tab is actually visible.
- The ESPHome version lookup is cached — it spawned a subprocess per load
  and cannot change while the container runs.
- Three scripts each carried their own copy of `escapeHtml` and of the
  seconds formatter. They now share one definition in `app.js`, which
  loads first.

## 0.11.0

The firmware learns to answer for itself, updates stop needing a USB
cable, and the devices are no longer wide open on the network.

### Auf dem Gerät

- **Statusseite** (`web_server`, on by default). Reachable at
  `http://<ip>/` from any browser, including ones with no Web Serial —
  everything on iPadOS. The page is embedded in the firmware, so it works
  with the device cut off from the internet. This is the answer to "is
  this thing even alive?", which previously required a USB cable.
- **Diagnose-Entities** (on by default): signal strength, uptime, chip
  temperature, IP address, connected SSID, MAC address, and restart /
  safe-mode buttons. Signal strength is not housekeeping here — CSI
  sensing degrades with a weak link, so it says whether a spot is viable
  before anything gets mounted.
- **Per-device API encryption key and OTA password.** Until now the
  firmware had neither: anyone on the network could read the sensor, drive
  it, and reflash it. Home Assistant asks for the key when adopting a
  device, so the device card shows it with a copy button.
- The firmware log level is now a per-device setting.

### Im Add-on

- **Update über WLAN.** After the first USB flash, firmware goes to the
  device over the network — the upload runs in the add-on, so it works
  from any browser. One computer per device, once, and never again.
- **Erreichbarkeit prüfen** probes ports 6053 and 80 and distinguishes the
  four cases that all used to read as "nicht verfügbar": name won't
  resolve, nothing answers, only the status page answers, or the device is
  fine and Home Assistant simply hasn't adopted it.
- Reading a zone made three Home Assistant round-trips *per member device*.
  A zone of five devices cost fifteen requests, every ten seconds, forever.
  One snapshot now serves every zone in a cycle: measured at 15 requests
  down to 1. Single device cards keep their targeted reads, which stay
  cheaper than pulling every state in Home Assistant.

### Projekt

- **armv7 removed.** It was advertised in `config.yaml` and `build.yaml`,
  but Espressif ships no ESP-IDF toolchain for 32-bit hosts — the official
  ESPHome add-on lists aarch64 and amd64 only. Promising a platform that
  cannot work is worse than not offering it.
- **CI**, which did not exist. Every pull request now runs the unit tests,
  renders the firmware template for all six boards through `esphome
  config`, and checks the add-on manifest. `tools/check_metadata.py` found
  a real gap on its first run: `mqtt_export` had no translation.
- `README.md` for the add-on; `translations/en.yaml` completed.
- CI's first run caught its own bug: `pip install ... pytest`, unpinned,
  made the resolver backtrack through pytest releases until it reached a
  2013-era version that bootstraps `distribute` over plain HTTP, and the
  job died on `HTTP Error 403: SSL is required`. pytest is pinned in a
  separate `requirements-dev.txt`, verified to resolve.
- Fix: a device stored before this release has no encryption key, and the
  generating default would have produced a *different* one on every read —
  so the key shown would not have been the key in the firmware. Missing
  keys are now generated once and persisted.
- Fix: `.btn-secondary` was not a class at all but a hand-kept list of
  every secondary control by name, so each new button silently fell
  through to the browser's default styling. It is a real class now.
- `tests/test_state_reading.py` pins both halves of the batching change:
  that a zone reads from one snapshot, and that a failing `/states`
  degrades to individual reads instead of blacking out every device.

## 0.10.2

A successfully flashed device stayed "nicht verfügbar" forever. The
firmware and the flash were fine — Echolot was looking for entities under
names Home Assistant never uses.

- **Entity ids were derived from the ESPHome node name.** Home Assistant
  builds them from the *device* name plus the entity name, and the device
  name is the config's `friendly_name` when there is one. A node `flur`
  with friendly name "Flur unten" produces
  `binary_sensor.flur_unten_motion_detected`, while Echolot looked for
  `binary_sensor.flur_motion_detected`. Every device with a friendly name
  was affected — which the device form actively encourages.
- Echolot now **asks Home Assistant what the entities are called** instead
  of predicting them, matching on the `friendly_name` attribute Home
  Assistant sets. The lookup runs automatically when a configured entity
  turns out to be missing, so existing broken devices repair themselves on
  the next poll; **Entities in Home Assistant suchen** on the device card
  triggers it by hand.
- Matching on the stated name rather than a predicted id also survives
  Home Assistant's `_2` collision suffix and its umlaut transliteration
  ("Küche" → `kuche`), neither of which a prediction could get right
  reliably.
- The "nicht verfügbar" message now names the other likely cause: after
  flashing, the device still has to be confirmed once under Settings →
  Devices & Services before any of its entities exist.
- `tests/test_entity_resolver.py` covers the naming cases against Home
  Assistant's real `/api/states` shape, including that one device never
  claims another's `_motion_detected`.

## 0.10.1

Firmware builds could fail with a bare CMake error naming a compiler that
was never installed. Two causes, both fixed.

- The PlatformIO cache now lives in `/data/platformio` instead of
  `~/.platformio`. The default sits in the container's writable layer, so
  every add-on restart or update threw away ~2 GB of ESP-IDF and toolchain
  and re-downloaded it on the next build — and every one of those
  downloads was another chance to be interrupted. The official ESPHome
  add-on keeps its cache the same way.
- An interrupted download leaves the toolchain *directory* in place with
  no compiler inside. PlatformIO's own guard checks only that the
  directory exists before putting its `bin/` on `PATH`, so the build got
  as far as CMake before failing on `riscv32-esp-elf-gcc ... not found in
  the PATH`, with nothing pointing back at the cause. Echolot now checks
  for the compiler itself, distinguishes "not installed yet" from
  "installed but broken", and replaces the opaque error with what actually
  happened plus a **Toolchain zurücksetzen** button that clears the
  package for a clean re-fetch.
- Fix: `run_build` looked up the board outside its `try`, so an unknown
  board key escaped the handler — the device stayed on "running" forever
  and its build lock was never released.
- `tests/test_toolchain.py` builds all three on-disk shapes (absent,
  broken, ok) for real, and checks that repairing one architecture leaves
  the other's toolchain alone.

## 0.10.0

Zones learn to hold. Until now a zone was the plain OR of its members'
motion sensors: instant to react, but equally instant to drop out, which
makes it unusable for lighting when a CSI score dips for two seconds.

- **Haltezeit**: a zone stays occupied for a configurable span after the
  last movement, and reports a third state, `holding`, while that
  countdown runs. Zone cards and dashboard tiles show the remaining time,
  so a zone that is deliberately waiting doesn't look like a stuck
  sensor.
- **Hysterese**: separate enter/exit thresholds on the movement score. In
  the band between them a zone keeps whatever state it had, so it cannot
  flicker on the boundary. A single value still works — that is just
  enter == exit.
- Both are optional and default to off, so existing zones behave exactly
  as before.
- The state machine lives in `app/zone_logic.py` as a pure function of
  (readings, time) and is covered by `tests/test_zone_logic.py`; the API
  and the MQTT bridge share one code path, so Home Assistant can never
  see a different state than the dashboard.
- Fix: `Zone.apply_update` wrote fields with `setattr` and so skipped
  validation — a PATCH that lowered only the enter threshold could leave
  the exit threshold stranded above it. Updates now re-validate the
  merged zone and answer 422.
- Fix: the layout probe only ever measured collapsed `<details>`, and the
  new tuning section overflowed its card on phones because a grid item's
  automatic minimum size is its content width. Probe and layout both
  fixed; measured overflow is 0 px across five viewports and all four
  tabs.

## 0.9.3

Firmware configuration is now validated against real ESPHome rather than
assembled from prose docs, and modelled on ESPectre's own per-board
example configs.

- Fix: builds failed with `Platform not found: 'ota.esp32'`. No such
  platform exists — the classic OTA protocol is `platform: esphome`. The
  wrong value came straight from ESPectre's SETUP.md.
- Fix: the ESP32-C3 could never have built. `cpu_frequency: 240MHz` was
  applied to every board, but the C3 and C6 top out lower and ESPHome
  rejects it outright. The frequency now follows each board's official
  example (and is simply omitted for C3/C6).
- **The BLE "Live" view could never have worked.** ESPectre only enables
  its telemetry channel when the config declares an `esp32_ble_server`,
  and ours didn't — `ble_channel_enabled` resolved to `false`. The
  generated firmware now includes that server (with the UUIDs the
  browser client expects) on every BLE-capable board, and
  `ble_channel_enabled` comes out `true`.
- Board id and framework version are no longer pinned, which is what
  produced the "not the recommended one" warnings on every build.
- Generated firmware gains a fallback access point and keeps
  `improv_serial`, so a device whose Wi-Fi credentials stop working can
  be re-provisioned without a rebuild.
- Whether a board supports BLE now lives in the board registry alone,
  feeding both the firmware template and the dashboard's "Live" button;
  the frontend previously kept its own separate list.

All six supported boards are verified with `esphome config` against
ESPHome 2026.6.5 and the real ESPectre component: all valid, BLE server
present on every board except the S2, which has no Bluetooth.

## 0.9.2

- Fix: on phones the page was wider than the screen and scrolled
  sideways, cutting off content at the left edge. The tab strip was the
  sole cause — four German labels do not fit 360px, and as an
  `inline-flex` it silently widened the whole document by up to 58px. It
  now scrolls within itself, and below 400px the labels are trimmed
  horizontally so all four still fit from 360px up.
- Phones are now a proper layout rather than a shrunken desktop:
  tighter page padding, readings and controls that stack instead of
  being squeezed onto one line, entity-id fields with their labels above
  them, long device names that wrap without pushing their status badge
  away, and full-width action buttons — except Delete, which stays small.
- Measured rather than eyeballed: horizontal overflow is now 0 px across
  320/360/375/393/768/1280px on every tab.

## 0.9.1

- Fix: the add-on image would not build at all. `paho-mqtt>=2.1` conflicted
  with ESPHome, which pins `paho-mqtt==1.6.1` exactly, so pip failed with
  ResolutionImpossible before anything was installed. The requirement now
  admits 1.6.x, and the MQTT client works with both paho generations — 2.x
  is asked for its VERSION1 callback API so one set of signatures serves
  both.
- Fix: zone names with umlauts produced entity ids containing the umlaut.
  A zone called "Küche" now becomes `binary_sensor.echolot_kueche` while
  keeping "Küche" as its display name.

## 0.9.0

Phase 5 — the last planned phase.

- **Zones are published to Home Assistant as occupancy sensors** over MQTT
  discovery. Until now a zone existed only inside this add-on: visible in
  its dashboard, but unusable in an automation, on a Home Assistant
  dashboard, or in HomeKit. Each zone now appears as its own
  `binary_sensor` with device class *occupancy*. Deleting a zone retracts
  the discovery message so the entity disappears instead of lingering as
  unavailable, and an unreachable zone publishes nothing rather than a
  confident "clear". Broker credentials come from the Supervisor
  (`services: mqtt:want`), so nothing needs configuring alongside the
  Mosquitto add-on — and without a broker everything else still works.
  Opt out with `mqtt_export: false`.
- **Native Matter was deliberately not implemented.** Matter commissioning
  inside an add-on is a large, fragile undertaking, and once a zone is a
  Home Assistant entity, HA's own HomeKit and Matter bridges export it.
  Publishing entities is the smaller and more reliable path to the same
  goal.
- **Radio-load estimation.** Each device probes the air continuously, so
  its packet rate is a real cost. The device form estimates it per device
  and the overview sums it across all of them, using ESPectre's own
  figure of roughly 9 KB/s at 100 packets/s.
- **Presets** — *Ausgewogen*, *Sparsam*, *Empfindlich*, *Ohne
  Kalibrierung* — replace guessing at four interacting parameters.
  Editing any value drops back to custom.
- Fix: the MQTT publish task kept running after shutdown. Startup and
  shutdown now use FastAPI's `lifespan`, which cancels it properly.

## 0.8.0

- **Renamed from "ESPectre Hub" to "Echolot".** The old name was both dull
  and misleading: it read like an official product of the upstream
  ESPectre project, when this is an independent add-on that merely uses
  it. "Echolot" names the measuring principle instead — locating something
  by reading how waves come back disturbed.
- The add-on slug changed from `espectre_hub` to `echolot`, which
  Home Assistant treats as a **new add-on**: the old one stays installed
  until removed by hand, and its `/data` (devices, zones, built firmware)
  does not carry over.
- References to ESPectre itself are untouched — it is still the ESPHome
  component this builds on, and the generated firmware config still pulls
  `github://francescopace/espectre`.

## 0.7.0

- The web interface is now in German throughout — labels, buttons, status
  text, validation messages and the error details the backend returns to
  the UI. Code comments, commit messages and this changelog stay in
  English, as does the project documentation, since the repository is
  public.
- Added the add-on `icon.png` and `logo.png` that were missing, so the
  Supervisor store no longer shows a placeholder, plus a matching favicon
  for the web interface.
- The Overview tab gained an inventory card (how many devices, how many
  built, how many zones) instead of only reporting service health.
- Movement scores now render with the same precision everywhere.

## 0.6.0

- Dashboard rethought around the question it should answer: **is the
  threshold in the right place, and is the signal steady?** Each device
  tile now charts its movement score over time with the detection
  threshold drawn across it, replacing a status dot that showed less
  than the Devices tab did.
- Charts open pre-filled from Home Assistant's recorded history via a new
  `GET /api/devices/{id}/history` endpoint, so the view is useful
  immediately instead of building up from an empty buffer.
- One chart, two sources: connecting over BLE re-feeds the same chart at
  the device's native ~10-50ms rate instead of driving a separate bar,
  which is what makes the high resolution actually useful.
- Zone tiles now show which member device is tripping, not just a count.
- Board type dropped from dashboard tiles (configuration detail, not live
  state) and the Web Bluetooth notice is a quiet aside rather than a
  full-width banner.
- Dashboard polling now only runs while that tab is on screen.

## 0.5.0

- UI reworked into one consistent design system rather than a glass
  dashboard bolted onto flat management tabs:
  - Real light **and** dark themes. The stylesheet previously claimed
    `color-scheme: light dark` while hardcoding dark colours, so in light
    mode the browser rendered native controls light against dark panels.
  - An atmospheric background so the frosted-glass surfaces actually have
    something to refract — `backdrop-filter` over a flat fill blurred
    nothing.
  - One scale for radii, one set of glass elevations, three button roles
    (primary / secondary / ghost), a single input style, keyboard focus
    rings, hover states, and `prefers-reduced-motion` support.
  - Tabs are now a segmented control; live readings render as labelled
    stat blocks instead of one crowded line.
- Fix: in the zone form, device checkboxes stacked on top of their labels
  — `.zone-device-option` inherited `flex-direction: column` from
  `.device-form label` and lacked the specificity to override it.
- Fix: "Flash over USB" dropped onto its own row below the other actions;
  it now sits in the action bar, and once a device is built it becomes the
  primary action while "Rebuild" steps down to secondary.
- Dashboard tiles are grouped under Devices/Zones headings — the two were
  previously indistinguishable.
- Repository URLs updated after the repo was renamed.

## 0.4.1

- Fix: firmware builds failed immediately with
  `esphome: error: unrecognized arguments: --no-logs`. That flag only
  exists on ESPHome's `run` subcommand, not on `compile`.

## 0.4.0

- Phase 4: live dashboard and optional high-resolution BLE visualizer.
  - New "Dashboard" tab: a glass-tile view of every device and zone at a
    glance, status dots polled from Home Assistant (same as Devices/Zones).
  - Optional per-device "Connect live (BLE)" button on BLE-capable boards
    (ESP32, C3, C5, C6, S3 — not S2) opens a direct Web Bluetooth
    connection to the device itself (no backend involved) for movement/
    threshold telemetry at ESPectre's native ~10-50ms notify rate, using
    the GATT protocol documented in ESPectre's own browser game client
    (service `d33ff46b-…`, little-endian float32 telemetry, ASCII control
    commands) — see `echolot/DOCS.md`.
  - This BLE path is implemented strictly to that documented spec and its
    binary/text parsing is unit-verified, but the live device connection
    itself has not been exercised against real ESPectre hardware — no
    Bluetooth-capable browser or device was available to test with. The
    polling-based dashboard tiles remain the verified fallback.

## 0.3.0

- Phase 3: zones and runtime configuration.
  - New "Zones" tab: group devices, aggregated with OR-logic presence
    (`GET /api/zones/{id}/state` — occupied if any member currently sees
    motion). Backed by `POST/GET/PATCH/DELETE /api/zones[/{id}]`.
  - Each built device now shows live motion/movement-score state, a
    threshold control (`POST /api/devices/{id}/threshold`, backed by HA's
    `number.set_value`), and a "Recalibrate" button (`.../calibrate`,
    backed by `switch.turn_on`) — all read/written through Home
    Assistant's Core API (`homeassistant_api: true`), since ESPectre
    already exposes these as HA entities.
  - `detection_algorithm` (mvs/ml) is a compile-time YAML option, not a
    runtime entity ESPectre exposes — changing it still means a rebuild
    and reflash on the Devices tab, not a Zones-tab control.
  - Best-guess HA entity ids are computed per device from its name and
    are user-editable ("HA entity ids" panel), since the exact id HA
    assigns isn't guaranteed.

## 0.2.0

- Phase 2: device management and browser-based flashing.
  - `POST /api/devices` renders a per-device ESPHome + ESPectre YAML
    (name, Wi-Fi credentials, board, detection algorithm/threshold) from
    a Jinja2 template and validates it with Pydantic.
  - `POST /api/devices/{id}/build` compiles it via the bundled `esphome`
    CLI in a background thread; `GET /api/devices/{id}` polls status/log.
  - `GET /api/devices/{id}/manifest.json` + `.../firmware.bin` expose the
    compiled image as an ESP Web Tools manifest for one-click USB flashing
    from the browser (Web Serial API — needs HTTPS or localhost).
  - `esp-web-tools` is vendored locally (`app/static/vendor/esp-web-tools`,
    Apache-2.0) rather than loaded from a CDN.
  - New "Devices" tab in the Ingress UI for adding, building, and
    flashing devices.

## 0.1.0

- Phase 1: initial add-on skeleton — Ingress-enabled FastAPI backend,
  bundled ESPHome CLI dependency, basic health-check UI.
