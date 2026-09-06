# Echolot

Self-hosted Wi-Fi CSI presence detection hub, built on
[ESPectre](https://github.com/francescopace/espectre) — an open-source,
license-free alternative to [TOMMY](https://www.tommysense.com).

## Installation

1. Add this repository to the Home Assistant Add-on Store
   (**Settings → Add-ons → Add-on Store → ⋮ → Repositories**).
2. Find **Echolot** in the store and click **Install**.
3. Start the add-on and open its web UI (Ingress panel in the sidebar).

## Configuration

| Option        | Description |
|---------------|-------------|
| `log_level`   | Verbosity of the add-on's own log output. One of `trace`, `debug`, `info`, `notice`, `warning`, `error`, `fatal`. |
| `mqtt_export` | Publish zones to Home Assistant as occupancy sensors over MQTT (default `true`). Needs an MQTT broker such as the Mosquitto add-on; without one the add-on runs normally and zones simply stay local to this interface. |

## Current phase

All five planned phases are implemented: add-on skeleton, device
flashing, zones and runtime configuration, the live dashboard, and the
Phase 5 polish (traffic estimation, presets, zone export).

### Flashing a device

1. Open the add-on's web UI and switch to the **Devices** tab.
2. Fill in the form: a device name, board, Wi-Fi network, and (optionally)
   the ESPectre detection algorithm/threshold. Wi-Fi credentials are baked
   into that device's compiled firmware.
3. Click **Build firmware**. This renders an ESPHome YAML for the device
   and runs `esphome compile` in the container — the first build per board
   downloads the ESP-IDF toolchain, so it can take several minutes;
   watch progress in the build log.
4. Once the card shows "ready to flash", connect the device over USB and
   click **Flash over USB**. This uses [ESP Web Tools](https://esphome.github.io/esp-web-tools/)
   and your browser's Web Serial API — supported in Chrome/Edge, and only
   on secure (HTTPS) pages or `localhost`. If Home Assistant is only
   reachable over plain HTTP on your LAN, open the add-on directly at
   `http://<host>:8099` from the same machine you're flashing from.

### Live state and runtime configuration

Once a device is built (it doesn't need to be reflashed for this — the
firmware from step 3 already includes it), its card also shows live
motion/movement-score state, a **threshold** control, and a
**Recalibrate** button. These are read and pushed through Home
Assistant's own Core API (this add-on requests `homeassistant_api: true`
for that), targeting the `binary_sensor`/`sensor`/`number`/`switch`
entities ESPectre's ESPHome component already exposes for each device —
so the device needs to actually be added to Home Assistant (normally
auto-discovered via the ESPHome integration once it's on your network)
for this to work; a freshly flashed device that HA hasn't picked up yet
will show as "unavailable".

The entity ids used are guessed from the device's name and shown (and
editable) under "HA entity ids" on its card — open that if a device
shows as unavailable and check the ids match what Home Assistant
actually assigned.

Note: `detection_algorithm` (`lightweight` / `high_accuracy`) sets only
the *initial* profile. ESPectre now also exposes a **Detection Profile**
select entity in Home Assistant, so the profile can be switched at
runtime without rebuilding; the YAML value is what a freshly flashed
device starts with.

### Übersicht

The first tab answers, in this order: what is being sensed right now, what
is stopping it from sensing correctly, and what the system is made of.

**Jetzt** shows every zone with its live state — occupied, free, holding
(with the countdown), or without devices. Clicking one opens the dashboard,
where you can see which member device is actually tripping.

**Braucht Aufmerksamkeit** appears only when something is wrong, and each
entry is worded as the thing to do about it: a build that failed, a device
never built, a device flashed but returning no values, a failed network
update, a zone with no devices, a zone export with no broker. A build in
progress is deliberately *not* listed — work in flight is not a fault, and
listing it teaches people to ignore the list.

**System** is reference rather than news: device count, total radio load
across the fleet, whether zones reach Home Assistant, and the ESPHome
version.

With nothing set up yet, the tab is a three-step setup path instead — a
status report about an empty system has nothing to report.

### Zones

The **Zones** tab groups devices and reports "occupied" when *any*
member device currently detects motion. Create a zone, tick which devices
belong to it, and its card polls live state the same way device cards do.

Raw OR-logic reacts instantly but also drops out instantly: CSI movement
scores are noisy, and someone sitting still for two seconds turns the
zone off and straight back on. Two settings under **Feinabstimmung** (in
the create form, or the *Abstimmen* button on an existing zone) fix that:

| Setting | Effect |
| --- | --- |
| **Haltezeit** | Seconds the zone stays occupied after the last movement. `0` disables it. 60–180 s is a sensible range for lighting. |
| **Einschalt-Schwellwert** | Movement score at which the zone switches on. Leave empty to keep trusting each device's own on-device threshold. |
| **Ausschalt-Schwellwert** | A *lower* second value creates hysteresis: between the two the zone keeps whatever state it had, so it cannot flicker on the boundary. Only meaningful together with an enter threshold. |

A zone therefore has three states rather than two. `detected` means a
device is seeing movement right now; `holding` means nothing is moving
but the hold time has not run out, so the zone is still occupied; `clear`
means neither. Both zone cards and dashboard tiles show the remaining
hold time as a countdown, so a well-tuned zone reads as deliberate rather
than as a stuck sensor. Only `detected` and `holding` publish as
"occupied" — to the API, to the dashboard, and to Home Assistant over
MQTT, all from the same code path.

Score-based detection uses the highest movement score across the zone's
members. A member with no movement-score entity falls back to its plain
motion sensor, so mixing tuned and untuned devices in one zone works.

### Der Verschlüsselungscode

Every device gets its own API encryption key and OTA password, generated
when you create it and baked into its firmware. Without them anyone on the
network could read the sensor, drive it, and overwrite its firmware.

The consequence is that **Home Assistant asks for the key** when it adopts
the device. It is on the device card under *Verschlüsselungscode für Home
Assistant*, with a copy button. The key is fetched only when that section
is opened — the device list itself carries no credentials, so one leaked
response is one device rather than the whole installation. (Ingress usually runs over plain HTTP,
which is not a secure context, so the clipboard API may be unavailable —
the button then selects the text instead.)

Keys are per device and never leave the add-on. Rebuilding a device keeps
its key; deleting and recreating it generates a new one, which means
removing and re-adding the device in Home Assistant too.

### Updates über das Netz statt über USB

Flashing a blank chip needs USB and a browser with Web Serial — Chrome or
Edge on a desktop. Every update *after* that does not: the device card has
a **Update über WLAN** button that pushes the built firmware over the
network. The upload happens in the add-on, so it works from any browser,
iPadOS included.

It needs an address to talk to, in *Netzwerkadresse* on the device card.
`<node name>.local` is the default and works wherever mDNS does; enter the
IP address where it doesn't.

If the device rejects the OTA password, its running firmware predates that
password — flash it over USB once and network updates work from then on.

### Erreichbarkeit prüfen

**Erreichbarkeit prüfen** on the device card probes TCP ports 6053 (the
ESPHome API) and 80 (the device's status page) and says which answered:

| Result | Meaning |
| --- | --- |
| Name nicht auflösbar | mDNS isn't reaching the add-on — use the IP address |
| Nichts antwortet | Device off, on another network, or the Wi-Fi isolates its clients |
| Nur Statusseite | Device is alive; open `http://<ip>/` to see what it says |
| API antwortet | Network is fine — what's missing is Home Assistant adopting the device |

That last row is the distinction the add-on could not previously make: a
device Home Assistant has not adopted and a device that never joined the
network both look like "nicht verfügbar" and need entirely different
fixes.

### Was auf dem Gerät selbst läuft

Two firmware options on the device form, both on by default:

**Statusseite** puts a web server on the device, reachable at
`http://<ip>/`. It shows every entity live and lets you drive them, which
makes it the fastest way to answer "is this thing working?" — and the only
way from a browser without Web Serial. The page is embedded in the
firmware, so it works with the device cut off from the internet.

**Diagnose-Entities** add signal strength, uptime, chip temperature, IP
address, connected SSID, MAC address, and restart buttons. Signal strength
is the one that matters most here: CSI sensing degrades with a weak link,
so it tells you whether a spot is viable for the device at all, before you
mount anything.

Both cost flash space. Turn them off if a build runs out of room.

### Wenn ein Gerät nach dem Flashen „nicht verfügbar" bleibt

Flashing puts the firmware on the chip; it does not put the device into
Home Assistant. Two things have to be true before Echolot can read it:

1. **Home Assistant must have adopted the device.** After the first boot
   it turns up under Settings → Devices & Services as a discovered ESPHome
   device and has to be confirmed once. Until then none of its entities
   exist and Echolot will say so, naming that step.
2. **Echolot must know the entity ids.** Home Assistant builds them from
   the *device* name plus the entity name — and the device name is the
   config's `friendly_name`, not the ESPHome node name. A node `flur` with
   friendly name "Flur unten" produces
   `binary_sensor.flur_unten_motion_detected`.

Echolot no longer predicts those ids and hopes. When the configured entity
turns out not to exist it asks Home Assistant what the device's entities
are actually called, matching on the `friendly_name` attribute Home
Assistant sets ("Flur unten Motion Detected"), and saves the result — so a
device usually repairs itself on the next state poll. **Entities in Home
Assistant suchen** on the device card triggers the same lookup by hand,
and the entity ids stay editable under **HA-Entity-IDs** for the cases
nothing can infer.

### Wenn ein Build am Compiler scheitert

A build that ends in

```
The CMAKE_C_COMPILER:
    riscv32-esp-elf-gcc
  is not a full path and was not found in the PATH.
```

is almost never a configuration problem. PlatformIO downloads roughly 2 GB
of ESP-IDF and cross toolchain before the first build, and its own guard
checks only that the toolchain *directory* exists before putting that
directory's `bin/` on `PATH` — never that a compiler is inside it. A
download interrupted partway therefore sails past that check and dies much
later, inside CMake, with nothing left pointing at the real cause.

Echolot recognises that failure and says so instead: the device card
reports that the toolchain is incomplete and offers **Toolchain
zurücksetzen**, which deletes the package so the next build fetches it
again. The button appears only after such a failure — it discards a
multi-gigabyte download, so it is not something to reach for casually.

The cache itself lives in `/data/platformio`, not in PlatformIO's default
`~/.platformio`. The default sits in the container's writable layer and is
discarded on every add-on restart or update, which would mean re-fetching
those 2 GB each time and getting a fresh chance at an interrupted
download. Budget the space accordingly on small installations; the
official ESPHome add-on stores its cache the same way.

### Dashboard

The **Dashboard** tab shows every device and zone as a tile. Each device
tile plots its **movement score over time with the detection threshold
drawn across it** — that is the view that tells you whether the threshold
sits in a sensible place and whether the signal is steady or flickering,
which a bare on/off indicator cannot.

The chart opens pre-filled from Echolot's bounded direct-data buffer. If no
direct samples are available yet, it falls back to Home Assistant's recorded
history (the last 30 minutes, if the recorder keeps that entity), so it is
useful immediately rather than starting blank. Below it sit the current score
and threshold; zone tiles highlight *which* member device is currently
tripping.

Device tiles used to offer a **Live** button that opened a Web Bluetooth
connection straight to the device for a ~10–50 ms stream. ESPectre removed
that GATT service when it restructured in September 2026, so the button is
gone rather than left to fail against firmware that no longer answers.

Its successor is ESPectre's Direct HTTP/SSE surface on port 62587, enabled
by the `direct_api` option (on by default). Echolot now keeps one connection
to that local stream per built device, retains a bounded in-memory history,
and fans it out through its own same-origin SSE endpoint. The browser never
connects to a device directly, so this also works through Home Assistant
Ingress. Device tiles mark the stream as **Direkt verbunden**; Home Assistant
history and polling remain the automatic fallback while a device is offline.

ESPectre has used more than one route shape during development. Echolot probes
the known event routes automatically. For an upstream build with a different
route, set `ESPECTRE_DIRECT_PATHS` in the container environment to a
comma-separated list such as `/events,/api/events`.

### Calibration Lab

The **Kalibrierung** tab turns live telemetry into a labelled room dataset:

1. Select a built device and start a recording.
2. Mark the real situation as **room empty**, **person moving**, **person
   sitting still**, or **interference active**. Every direct sample receives
   the label that was active when it arrived.
3. Collect at least 20 empty and 20 occupied samples, then stop the recording.

Echolot calculates the empty-room median, robust noise (MAD), class separation,
recommended enter/exit thresholds, and estimated false-positive and
false-negative rates. Interference samples remain in the export but do not
silently train the presence threshold. Recommendations are deliberately shown
for review rather than pushed onto the device automatically.

Sessions survive add-on restarts; an open recording is marked **interrupted**
after a restart so new measurements can never inherit an old label unnoticed.
Use **CSV exportieren** to inspect the timestamp, movement score, device
threshold, motion decision, and ground-truth label for every sample.

### Confidence fusion

For zones with direct data, Echolot supplements the classic binary state with
an explainable confidence model. Each member contributes:

- a presence probability derived from its newest completed calibration,
  otherwise from its current device threshold or motion boolean;
- a reliability value based on calibration quality and sample age;
- the basis used for that decision (`calibrated_score`, `device_threshold`, or
  `motion_only`).

Samples start losing weight after five seconds and expire after twenty seconds.
Expired devices therefore make the fused result unavailable rather than voting
for an empty room. Reliable member probabilities are combined as a weighted
mean, so adding many weak sensors cannot force a zone to occupied. The result
is **occupied**, **vacant**, or explicitly **uncertain**; the latter is used
when devices disagree instead of hiding the disagreement behind OR logic.

Dashboard zone tiles show confidence and agreement. Hover a member chip to see
that device's probability, reliability, and evidence basis. The existing Home
Assistant/MQTT occupancy state remains untouched for backwards compatibility
while this confidence path gathers real-world validation data.

### Zones in Home Assistant

Zones would otherwise exist only inside this add-on — visible here, but
unusable in an automation, on a Home Assistant dashboard, or in HomeKit.
With an MQTT broker available (the Mosquitto add-on is the usual one),
each zone is published via MQTT discovery and appears as its own
`binary_sensor` with device class *occupancy*, named after the zone.

From there Home Assistant's own HomeKit and Matter bridges can export it
further. That is deliberately how this works instead of speaking Matter
directly: implementing Matter commissioning inside an add-on is a large,
fragile undertaking, while Home Assistant already does it well for any
entity it knows about.

Deleting a zone retracts its discovery message, so the entity disappears
rather than lingering as "unavailable". If a zone's devices can't be
reached, no state is published at all — Home Assistant then keeps the
last known value instead of being told a confident "clear".

The **Übersicht** tab shows whether the export is active, and why not if
it isn't. Set `mqtt_export: false` in the add-on options to turn it off.

### Presets and radio load

Every device probes the air continuously, so its packet rate is a real
cost: at the default 100 packets/s a device generates roughly 9 KB/s of
Wi-Fi traffic (figure from ESPectre's own SETUP.md). The device form
estimates this per device, and the **Übersicht** tab sums it across all
of them — worth a glance before adding the fifth sensor.

The **Voreinstellung** picker offers four starting points instead of
four interacting numbers to guess at: *Ausgewogen* (ESPectre's
defaults), *Sparsam* (less than half the radio load), *Empfindlich*
(double the sampling and the lowest threshold), and *Ohne Kalibrierung*
(the neural-network algorithm, which needs no settling period). Editing
any of the values leaves the preset and switches to custom.

## Support

This is a personal/community project, not affiliated with TOMMY or
ESPectre's upstream maintainers. File issues against this repository.
