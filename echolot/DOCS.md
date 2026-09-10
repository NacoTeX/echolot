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
   into that device's compiled firmware. On an ESP32-C5 the form also asks
   which **Wi-Fi band** to use; 2.4 GHz is the default and the only one
   ESPectre has characterised — see „Auf welchem Funkband gemessen wird".
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

### Die Oberfläche

Zwei Dinge, die nach dem Ausprobieren mit echten Geräten geändert wurden.

**Eine Gerätekarte ist eine Liste, keine Wand.** Vorher war jede Karte rund
500 px hoch, ob man den Inhalt wollte oder nicht — zwei Geräte füllten den
Bildschirm, und eine Wohnung mit fünf Räumen wäre unbenutzbar gewesen. Die
Karte ist jetzt ein `<details>`: die zugeklappte Zeile trägt Name, Board,
Erkennungsprofil, den Live-Zustand und den Build-Zustand, und das ist genau
das, wofür man eine Liste überfliegt. Alles andere — Netzwerk, Diagnose,
Verschlüsselungscode, Entity-IDs, Build-Protokoll — steckt in eigenen
Untergruppen dahinter.

Native `<details>` und nicht ein selbstgebautes Akkordeon, damit Tastatur
und die Seitensuche des Browsers weiter funktionieren. Der Zustand
überlebt das Neuzeichnen: die Liste wird beim Bauen alle paar Sekunden neu
gerendert, und ohne das würde jede offene Karte unter der lesenden Person
zuklappen. Eine Karte öffnet sich von selbst, wenn sie die einzige ist,
wenn gerade gebaut wird, oder wenn ein Build fehlschlägt — Letzteres nur
beim Übergang, sonst spränge sie nach jedem Zuklappen wieder auf.

Das Anlegen-Formular klappt genauso weg, sobald es Geräte gibt. Ob jemand
es selbst geöffnet hat, wird am Klick auf die Zusammenfassung erkannt und
nicht am `toggle`-Ereignis: `toggle` kann eine Person nicht von der Zeile
unterscheiden, die das Formular bei leerer Liste aufklappt, und es feuert
asynchron.

**Der Systemzustand steht in der Kopfzeile.** Vorher war er nur auf der
Übersicht zu sehen, also unsichtbar, während man irgendwo anders arbeitete
— ein fehlender `SUPERVISOR_TOKEN` fiel erst auf, wenn eine Gerätekarte
„nicht verfügbar" meldete. Rechts oben steht jetzt auf jedem Tab ein Chip
mit der Anzahl offener Hinweise; er öffnet dieselbe Liste, und ein Klick
darin springt auf den zuständigen Tab. Die Zahl steht im Text, nicht nur
in der Farbe des Punkts.

Daneben zwei Schalter, die das Lesen betreffen und nicht das System:
**Aktualisieren aussetzen** hält jeden Poller auf der Seite an, damit ein
Log oder eine Zahl stehen bleibt, und **Gerätekarten offen anzeigen** für
alle, die lieber alles sehen. Beide liegen im `localStorage`, weil sie
Gewohnheiten des Browsers sind und keine Konfiguration, die das Add-on
mitschleppen sollte. Die eigentliche Konfiguration — Log-Level,
MQTT-Export — gehört in die Add-on-Optionen von Home Assistant und steht
bewusst nicht doppelt hier.

**Der Web-Serial-Hinweis erscheint nur, wo er zutrifft.** Er stand auf dem
Geräte-Tab bei jedem Besuch, auch über HTTPS, wo Flashen ohnehin geht.
Jetzt hängt er an `window.isSecureContext` — dauerhafte Warnungen sind der
Grund, warum Warnungen nicht gelesen werden.

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

> **Die API-Verschlüsselung ist zurzeit standardmäßig aus.** Not by
> preference: `api: encryption:` makes ESPHome pull in noise-c/libsodium,
> whose C sources include a bare `"utils.h"`. ESPectre registers its own
> `src/cpp/core` as a *public* ESP-IDF include directory and there is a
> C++ `utils.h` in it, so the C compiler picks that one and dies on
> `#include <cstdint>`. The firmware cannot be compiled with both present.
> This is a bug in ESPectre's build definition — a component should not
> publish a generically named header on the global include path.
>
> The key is still generated and kept per device, so the option can be
> switched back on (per device, in the firmware options) the moment
> upstream fixes it, without generating anything new.
>
> With encryption off, anyone on your network can read the sensor and
> drive the device. On a normal home network behind a router that is the
> same exposure as most ESPHome devices; on a shared or guest network it
> is not acceptable, and there the device does not belong.

Every device gets its own API encryption key and OTA password, generated
when you create it. The OTA password is always active; the API key only
when API encryption is switched on. Without them anyone on the
network could read the sensor, drive it, and overwrite its firmware.

When encryption is on, **Home Assistant asks for the key** as it adopts
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

Die Prüfung testet drei Ports: **6053** (ESPHome-API, die Home Assistant
braucht), **80** (die Statusseite des Geräts) und **62587** (ESPectres
direkte HTTP/SSE-Schnittstelle). Über 6053 entscheidet sich das Urteil —
nur dieser Port beantwortet „kann Home Assistant das Gerät erreichen".

Port 62587 bekommt eine eigene Zeile, weil er eine andere Frage
beantwortet: ob **Calibration Lab und Live-Dashboard** Daten bekommen.
Beide holen ihre Samples direkt vom Gerät, nicht über Home Assistant. Ein
Gerät kann in Home Assistant tadellos laufen und für das Calibration Lab
trotzdem stumm sein. Antwortet der Port nicht, wurde die Firmware ohne
`direct_api` gebaut — die Voreinstellung ist an, ein Neubau schaltet es
also ein.

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

### Wenn „Preparing installation“ nicht weitergeht

That message covers two separate steps, which is why it is unhelpful on
its own. Reading ESP Web Tools' own code: the dialog shows it while the
install state is either `initializing` — talking to the chip over serial
— or `preparing`, downloading the firmware. Neither has a timeout, so
either can sit there indefinitely.

**The device card now names the step.** Underneath the flash button,
Echolot shows which of the two is running, and after about twenty seconds
without progress it adds the advice that belongs to that step. The
Network-tab check below is the same answer from the other side, and works
even on an older add-on version.

**Which one it is, in five seconds:** open the browser's developer tools,
Network tab, and look for `firmware.bin`.

| What you see | Where it is stuck |
| --- | --- |
| `firmware.bin` pending, bytes not climbing | the download from this add-on |
| `firmware.bin` finished, or never requested | the serial handshake with the chip |

#### An existing firmware is never the reason

A chip that already carries a firmware is not harder to flash than a blank
one, and erasing it first is not a prerequisite. Writing goes through the
ROM bootloader, which sits in mask ROM: no firmware can overwrite it, and
`esptool` never asks the running application for permission. Erasing
happens *through* that same bootloader — so if an install will not start,
an erase will not either. They fail for one shared reason, and it is the
next section.

#### If it is the chip: download mode by hand

Auto-reset over DTR/RTS does not work on every board, and on chips with
native USB the running firmware owns the USB port until it lets go. Put
the chip into download mode yourself:

1. Hold **BOOT** down. That is `GPIO0` on the ESP32, ESP32-S2 and
   ESP32-S3, and `GPIO9` on the C3, C5 and C6.
2. Tap **RESET** (also labelled **EN**) — or, on a board without a reset
   button, plug the USB cable in while still holding BOOT.
3. Release BOOT.

**Then pick the port again.** In download mode a native-USB chip
re-enumerates as a *different* USB device, so the port the browser was
already granted no longer exists. Start the install fresh and choose the
port from the dialog.

Also close anything else holding the port — an open ESPHome log viewer,
the Arduino IDE, a terminal — and rule out a charge-only USB cable, which
has no data lines and cannot be told apart from a broken board.

#### If it is the download, or nothing else worked

The device card shows the image's size next to **Firmware
herunterladen**. That link is the way around the built-in flasher
entirely: download the `.bin` and install it with
[web.esphome.io](https://web.esphome.io) or `esptool`, writing it at
offset `0` — it is a full factory image, bootloader and partition table
included.

```sh
# optional, and only ever useful for leftover state such as stored
# Wi-Fi credentials — never as a precondition for writing
esptool --chip esp32c6 --port /dev/ttyACM0 erase-flash

esptool --chip esp32c6 --port /dev/ttyACM0 write-flash 0x0 firmware.bin
```

On esptool 4 and earlier the subcommands are spelled `erase_flash` and
`write_flash`; on Windows the port is `COM3` or similar. `--port` can be
left out when exactly one board is attached.

### Diagnose stellen

Ein Gerät kann online sein, jeden Bau bestanden haben, in Home Assistant
sauber auftauchen — und trotzdem nichts erkennen. Der Knopf **Diagnose
stellen** auf der Gerätekarte prüft genau das. Er ist ein Knopf und läuft
nicht im Hintergrund, weil er den vollständigen Zustandsschnappschuss von
Home Assistant liest; auf einer großen Installation ist das die teuerste
Abfrage, die dieses Add-on kennt.

Er meldet vier Dinge, alle aus echten Geräten heraus entstanden:

**Die Schwelle gehört zum anderen Profil.** ESPectre hat pro
Erkennungsprofil eine eigene Vorgabe — `0.6621854538596202` für
`lightweight`, `0.5` für `high_accuracy` (`src/cpp/core/detector_types.h`).
Die Firmware übernimmt die passende aber nur, wenn das Profil **zur
Laufzeit** umgestellt und im NVS gespeichert wurde. Ein im YAML gesetztes
Profil — also jedes Gerät, das Echolot baut — behält den Schema-Default,
und der ist fest auf den Lightweight-Wert verdrahtet
(`runtime/esp_idf/esp_idf_runtime.cpp`, `runtime_sensing_schema.h`). Ein
`high_accuracy`-Gerät läuft dann mit einer um 32 % zu hohen Latte.
**Neu kalibrieren** setzt sie richtig. Dieser Befund steht auch auf der
Übersicht, weil er jedes gebaute Gerät betrifft und die Schwelle dort
ohnehin gelesen wird.

**Die Sensing-Entities fehlen.** Ein Gerät, das sich in Home Assistant
meldet, aber ohne „Motion Detected“, „Movement Score“ und „Threshold“.
Die aktuelle ESPectre-Version legt diese immer an, dort liefe also ältere
Firmware, und nichts sonst fiele darauf auf — Uptime, Temperatur und WLAN
sähen normal aus. Dann hilft nur neu bauen und flashen.

Bevor du das tust: **prüfe, ob die Entities nur woanders liegen.** Ein in
Home Assistant umbenanntes Gerät behält den alten Slug auf allen bereits
angelegten Entities und bekommt für neu angelegte den neuen. Ein einziges
Gerät antwortet dann unter zwei Präfixen gleichzeitig — beobachtet mit
`sensor.test1_movement_score` neben `sensor.zuhause_test1_uptime`, beide
mit dem Anzeigenamen „test11“. Eine Suche nach dem einen Präfix findet die
Entities des anderen nicht und lässt das Gerät halb verschwunden aussehen.

Echolot fällt darauf nicht herein: der Resolver vergleicht das Attribut
`friendly_name`, das Home Assistant unabhängig vom Slug auf
„&lt;Gerätename&gt; &lt;Entity-Name&gt;“ setzt. Wenn die Diagnose die
Entities dennoch nicht findet, ist **Entities in Home Assistant suchen**
auf der Gerätekarte der erste Griff — nicht der Neuflash.

**Die CSI-Diagnosen wurden nie abgerufen.** `CSI Accepted Rate` und die
übrigen Raten veröffentlichen nur auf Anforderung. Unberührt stehen sie
für immer auf `unknown`, und damit lässt sich nicht sehen, ob überhaupt
verwertbare CSI-Pakete ankommen. **Diagnosewerte abrufen** drückt den
`Refresh Diagnostics`-Knopf des Geräts.

**Zu wenig CSI.** Sobald die Raten Werte liefern: kommt weniger als ein
Viertel der eingestellten `csi_target_pps` an, bekommt der Detektor zu
wenig Material und die Erkennung wird Zufall. Meist liegt es am Abstand
zum Access Point oder an einem überlasteten Kanal.

Dazu ein Hinweis ohne Knopf: lag der höchste Bewegungswert der letzten
fünfzehn Minuten um mehr als das Hundertfache unter der Schwelle, sagt
die Diagnose das — aber ausdrücklich als Beobachtung, nicht als Urteil.
Eine leere Wohnung erzeugt aus gutem Grund niedrige Werte, und das Add-on
kann den Fall nicht von einer unerreichbaren Schwelle unterscheiden. Das
trennt nur ein Gehtest: eine Minute durch den Raum laufen, dann die
Diagnose noch einmal stellen.

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

### Was CI von der Firmware prüft

Zwei getrennte Fragen, zwei Jobs.

**Konfiguration** (`tools/validate_firmware.py`): rendert die Vorlage für
*jedes* Board plus die Varianten, deren Zweige sonst nie durchlaufen
werden, und lässt `esphome config` darüber laufen. Drei Fehler kamen so in
Releases, weil die erzeugte YAML nur gelesen und nie geprüft wurde:
`ota: platform: esp32` (gibt es nicht), `cpu_frequency: 240MHz` auf Chips,
die niedriger enden, und ein fehlender `esp32_ble_server`, der den
BLE-Kanal dauerhaft abschaltete. Das dauert Sekunden und läuft bei jeder
Änderung.

**Kompilierung** (`tools/compile_firmware.py`, seit 0.13.6): linkt ein
echtes Image. `esphome config` holt ESPectre nicht, startet keinen
Compiler und kann nicht sagen, dass der gepinnte ESPHome-Stand und der
gepinnte ESPectre-Commit sich über einen Header uneinig sind — genau der
Fehler, der bei einem Nutzer als Zwanzig-Minuten-Build mit einem
C++-Fehler ankommt, den er nicht geschrieben hat.

Drei Boards — Xtensa `esp32`, RISC-V `esp32c6` und `esp32c5` — in der
Konfiguration, die auch ausgeliefert wird: Weboberfläche, Diagnose und
Direct-API an, API-Verschlüsselung aus. Letzteres nicht aus Bequemlichkeit
— sie *validiert*, aber sie *linkt nicht* (siehe „Der
Verschlüsselungscode"), und ein Job, der scheitern muss, ist kein Job.
Genau diese Lücke hat der Compile-Job bei seinem ersten echten Lauf
gezeigt: die Konfigurationsprüfung war für die verschlüsselte Variante
die ganze Zeit grün. Drei statt sechs, weil die beiden Befehlssätze das
meiste davon sind, was sich wirklich unterscheidet: getrennte
Toolchain-Downloads, getrennte Compiler, getrennte Chip-Header. C3 und C6
unterscheiden sich untereinander auf eine Weise, die `esphome config`
bereits abdeckt.

Der C5 ist die Ausnahme, seit 0.13.8. Er ist der einzige Zweiband-Chip
hier, also der einzige, dessen erzeugter `wifi:`-Block überhaupt ein
`band_mode:` trägt, und der jüngste von ihnen in ESP-IDF. Dass eine
Konfiguration validiert, sagt über diese Variante nichts. Der Cache läuft
seither pro Board statt pro Befehlssatz — zwei RISC-V-Läufe mit
demselben Schlüssel würden sich um dieselbe Cache-Ablage streiten, und
der Verlierer bekäme fortan einen Cache mit den Headern des anderen
Chips.

Der Job läuft bei Pushes auf `main` und bei Pull Requests mit dem Label
`firmware`, nicht bei jedem Commit: ein kalter Build lädt rund 2 GB je
Befehlssatz. Das Label anzuhängen startet einen Lauf — dafür horcht der
Workflow neben den Standardereignissen auch auf `labeled`, sonst wäre das
Label ein Mechanismus, der nie auslöst. Der PlatformIO-Cache wird auf beide Pins verschlüsselt —
ESPHome-Anforderung und ESPectre-Commit —, sodass ein Bump von einem der
beiden von vorn baut statt gegen ein altes Framework zu linken.

Lokal genauso aufrufbar:

```bash
python echolot/tools/compile_firmware.py            # alle drei
python echolot/tools/compile_firmware.py esp32c5    # nur eines
```

### Dashboard

The **Dashboard** tab shows every device and zone as a tile. Each device
tile plots its **movement score over time with the detection threshold
drawn across it** — that is the view that tells you whether the threshold
sits in a sensible place and whether the signal is steady or flickering,
which a bare on/off indicator cannot.

The chart opens pre-filled from Echolot's bounded buffer of canonical
readings (see „Eine kanonische Datenquelle" below). If none have arrived
yet, it falls back to Home Assistant's recorded history (the last 30
minutes, if the recorder keeps that entity), so it is useful immediately
rather than starting blank. Below it sit the current score
and threshold; zone tiles highlight *which* member device is currently
tripping.

Device tiles used to offer a **Live** button that opened a Web Bluetooth
connection straight to the device for a ~10–50 ms stream. ESPectre removed
that GATT service when it restructured in September 2026, so the button is
gone rather than left to fail against firmware that no longer answers.

Its successor is ESPectre's Direct HTTP/SSE surface on port 62587, enabled
by the `direct_api` option (on by default). Echolot can keep one connection
to that local stream per built device and fan it out through its own
same-origin SSE endpoint, so the browser never connects to a device directly
and this also works through Home Assistant Ingress.

**Der Kollektor läuft aber nicht von selbst** — siehe „Eine kanonische
Datenquelle" gleich unten. Am gepinnten ESPectre-Commit weist ein
ESPHome-gebautes Gerät jede Anfrage dieses Add-ons mit 403 ab, und die
einzige Umgehung wäre eine vorgetäuschte Herkunft.

ESPectre has used more than one route shape during development. Echolot probes
the known event routes automatically. For an upstream build with a different
route, set `ESPECTRE_DIRECT_PATHS` in the container environment to a
comma-separated list such as `/events,/api/events`.

### Eine kanonische Datenquelle

Echolot kann ein Gerät auf zwei Wegen hören: über die
Home-Assistant-Entities, die es per ESPHome-API veröffentlicht, und über
ESPectres eigenen Direct-HTTP-Stream auf dem Gerät. Bis 0.13.5 liefen
beide, **beide** speisten den Kalibrierungsspeicher, und die
Konfidenz-Fusion las ausschließlich den direkten. Also:

- ein Gerät ohne Direct-API steuerte zur Fusion **nichts** bei, während
  Home Assistant seine Werte die ganze Zeit lieferte;
- ein Gerät mit Direct-API konnte dieselbe Bewegung **doppelt**
  aufzeichnen — und die Ereignisrate zählt pro Sekunde, doppelt zählen
  fügt also keine Auflösung hinzu, es verdoppelt die Zahl;
- an keinem Messwert stand, welcher Transport ihn gemessen hatte.

Seit 0.13.6 gibt es genau **eine kanonische Quelle je Gerät**
(`app/samples.py`). Jeder Messwert trägt sie, ein Messwert aus einer
anderen Quelle wird abgelehnt statt untergemischt, und die Ablehnungen
werden gezählt — ein Kollektor, der läuft und nichts beiträgt, soll
sichtbar sein und nicht rätselhaft. Kalibrierung, Fusion, Live-Kurve und
CSV-Export lesen diesen einen Strom.

Welche Quelle kanonisch ist, ist eine **Einstellung** und kein Rennen
zwischen den Transporten: ein Ratenprofil wird unter einer Quelle
gelernt, und still darunter zu wechseln würde die Messung ändern, ohne
ihre Definition zu ändern. `ECHOLOT_SAMPLE_SOURCE` wählt
(`home_assistant`, Voreinstellung, oder `direct`).

**Warum Home Assistant die Voreinstellung ist.** Nicht aus Bequemlichkeit,
sondern weil der andere Weg am gepinnten Upstream nicht offensteht. Am
Commit `ce23b0b6` konfiguriert ein ESPHome-gebautes Gerät seinen
Direct-HTTP-Dienst mit `DirectHttpServiceConfig::for_first_party_portals()`
— erlaubt sind genau `https://espectre.dev` und zwei Geschwister-Domains
—, und das ESPHome-Frontend übergibt `allow_missing_origin = false`
(`espectre.cpp`, das `RuntimeDirectHttpBridgeConfig`-Literal).
Loopback-Origins sind wegkompiliert, solange
`CONFIG_ESPECTRE_DIRECT_DEV_ORIGINS_ENABLED` nicht gesetzt ist, und die
Kconfig, die der ESPHome-Build erreicht (`src/cpp/Kconfig.projbuild` samt
der von dort eingebundenen `espectre_config`), **deklariert dieses Symbol
gar nicht** — nur das Native-Frontend und die Micro-Firmware tun das.

Jede Anfrage dieses Add-ons bekommt also **403 „Origin rejected"**, außer
sie behauptet, espectre.dev zu sein. Das tut Echolot nicht. Der direkte
Kollektor bleibt für Firmware verfügbar, die ihn zulässt, läuft aber nur
auf ausdrückliche Anforderung (`ECHOLOT_DIRECT_COLLECTOR=true`) und nennt
im Log den Grund, wenn er ausbleibt. Sein Verbindungszustand steht
weiterhin unter `/api/devices/{id}/telemetry` neben den kanonischen
Messwerten — „warum kommen keine Direktdaten" ist eine echte Frage.

### Was auf der Direkt-Telemetrie liegt

Der Hub verbindet sich pro Gerät auf **`/espectre/v1/events`**
(`runtime/direct_http_protocol.h`, `ESPECTRE_DIRECT_HTTP_EVENTS_ENDPOINT`).
Die übrigen Pfade in `DEFAULT_PATHS` sind nur Rückfallebenen.

ESPectre teilt die Werte auf mehrere Ereignisse auf, und das ist beim
Lesen der Aufzeichnungen wichtig:

| Event | Nutzdaten |
| --- | --- |
| `motion` | `{"timestamp_ms":…,"state":"motion"\|"idle","score":…}` |
| `sensing` | Profil, `threshold`, `motion_on_hits`, Verkehrsmodus |
| `health`, `wifi`, `diagnostics` | Laufzeitwerte ohne Messgrößen |

Der Bewegungswert heißt also `score`, der Bewegungszustand ist ein
Textzustand unter `state` — kein Boolean —, und die Schwelle steht
**nie** im selben Ereignis wie ein Messwert. Echolot merkt sich deshalb
die zuletzt gesehene Schwelle je Gerät und schreibt sie an die
nachfolgenden Messpunkte; ein Ereignis, das nur die Schwelle meldet, wird
selbst nicht als Messpunkt aufgezeichnet, sonst stünde in jeder CSV eine
leere Zeile.

`timestamp_ms` zählt ab dem Start des Geräts, nicht ab 1970. Wörtlich
genommen läge jeder Messpunkt im Jahr 1970 und vor denen aller anderen
Geräte; Echolot nimmt deshalb die Ankunftszeit.

### Woher das Calibration Lab seine Werte nimmt

**Aus Home Assistant, nicht vom Gerät.** ESPectres Direct-HTTP-API ist
absichtlich für Fremdzugriffe gesperrt: der Dienst wird mit
`for_first_party_portals()` konfiguriert und akzeptiert nur
`https://espectre.dev` und zwei Geschwister-Domains; eine Anfrage ohne
`Origin`-Header wird mit **403 „Origin required"** abgewiesen
(`runtime/direct_http_service.h`, `direct_http_service_esp_idf.cpp`).

Die Ausnahme, die upstream vorsieht —
`CONFIG_ESPECTRE_DIRECT_DEV_ORIGINS_ENABLED`, das HTTP-Loopback-Origins
zulässt — ist in **keiner Kconfig deklariert, die der ESPHome-Build
erreicht**; sie existiert nur für die Micro-Python-Firmware und das
Native-Frontend. Über `sdkconfig_options` gesetzt würde sie als
unbekanntes Symbol verworfen und stillschweigend nichts bewirken.

Das Add-on nimmt deshalb dieselben drei Werte dort, wo sie ohnehin
liegen: `movement_score`, `motion_detected` und `threshold` als
Home-Assistant-Entities. Ein Gerät braucht dafür **kein** `direct_api`,
sondern erkannte Entities — bei Bedarf „Entities in Home Assistant
suchen" auf der Gerätekarte.

Abgetastet wird zweimal pro Sekunde, aber nur **neue** Werte werden
aufgezeichnet: Home Assistant stempelt jeden Zustand mit `last_updated`,
und eine Abfrage, die denselben Wert wie zuvor liefert, ist eine
Wiederholung und keine Messung. Sie mitzuzählen würde identische Zahlen
aufhäufen und genau die Verteilung verzerren, aus der die Empfehlung
berechnet wird. Die Schwelle wird seltener gelesen als der Messwert, weil
sie sich nur beim Kalibrieren ändert.

Das kostet einen Umweg über Home Assistant und etwas Auflösung gegenüber
dem direkten Strom. Dafür braucht es nichts vom Gerät, was es Home
Assistant nicht ohnehin schon gibt.

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

### Warum die Firmware das Socket-Budget anhebt

ESPHome berechnet `CONFIG_LWIP_MAX_SOCKETS` aus den Komponenten, die sich
während der Validierung dafür anmelden, und schreibt das Ergebnis ins
Build-Log:

```
Setting CONFIG_LWIP_MAX_SOCKETS to 17
  (TCP=11 [api=3, captive_portal=3, web_server=5], UDP=3, TCP_LISTEN=3)
```

ESPectre taucht darin nicht auf — es ist eine externe Komponente und
nimmt an dieser Zählung nicht teil. Sein Direct-HTTP/SSE-Server auf Port
62587 fordert aber aus demselben Pool bis zu sieben weitere Sockets
(`max_open_sockets = max_event_clients + 5`, `max_event_clients` ist auf 2
begrenzt). Es ist **ein** Pool für TCP, UDP und Listener zusammen.

Die Folge wäre keine Fehlermeldung beim Bauen, sondern Socket-Knappheit
unter Last — und als Erstes stirbt der SSE-Strom, von dem Calibration Lab
und Live-Dashboard leben. Das sähe aus wie eine Aufzeichnung, die einfach
aufhört.

Echolot setzt deshalb `CONFIG_LWIP_MAX_SOCKETS: "24"`, sobald `direct_api`
an ist: ESPHomes bisher beobachteter Höchstwert plus ESPectres sieben. Ein
selbst gesetzter Wert hat Vorrang vor ESPHomes Rechnung; sollte ESPHome
jemals mehr brauchen, sagt es das beim Bauen mit der genauen Zahl. Ohne
`direct_api` bleibt die Rechnung unangetastet.

### Präsenz aus der Überschreitungsrate

Gemessen an echter Hardware, in drei Aufnahmen aus einem Wohnzimmer:

| Situation | Ereignisse/s |
| --- | --- |
| Raum leer, jemand bewegt sich in der übrigen Wohnung (20 min) | **0,027** |
| Person sitzt still auf der Couch | **0,261** |

Eine dritte Zeile stand hier bis 0.13.5: „Person direkt vor der Tür,
3,03". Die kam aus **neun Sekunden** Aufnahme, geteilt durch den Abstand
zwischen erstem und letztem Messwert. Neun Sekunden sind kein Fenster,
und die Rate war ein kurzer Ausschlag, auf eine Sekunde hochgerechnet.
Seit die Fenster auf einem festen Raster liegen, ergibt diese Aufnahme
gar kein Fenster mehr — die ehrliche Antwort ist, dass sie nichts sagt.

Die wichtigste Zahl ist die erste: zwanzig Minuten durch die Wohnung
laufen erzeugt **zehnmal weniger** als still im Raum sitzen. Das Signal
geht zwar durch Wände, aber nicht stark genug, um raumbezogene Präsenz
unmöglich zu machen.

**Gezählt wird pro Sekunde, nicht pro Messwert.** Home Assistant meldet
nur Änderungen, und der Bewegungswert steht lange exakt auf null — eine
Zwanzig-Minuten-Aufnahme hatte Lücken bis 18,7 s ohne jede Meldung. Ein
Anteil an den Messwerten würde eine stille Minute mit vier Meldungen so
schwer wiegen wie eine geschäftige mit zweihundert, also ausgerechnet
die Zeiträume aufwerten, die still aussehen sollten.

**Der Maßstab ist das 90er-Perzentil der Leer-Fenster, nicht ihr Median.**
Vierzehn von zwanzig Fenstern kreuzten exakt null Mal; der Median ist 0,0
und sagt nichts. Die Frage ist nicht, was der leere Raum üblicherweise
tut, sondern was er **schlimmstenfalls** tut — und das ist ein hohes
Perzentil. Es übersteht außerdem ein kontaminiertes Fenster unter zwanzig,
was ein Maximum nicht täte.

**Eine kurze Leer-Aufnahme wird abgelehnt.** Bei drei Fenstern *ist* das
90er-Perzentil das größte davon: eine zweieinhalbminütige Aufnahme, deren
letzte Minute belegt war, ergab einen „Leerwert" von 0,393 — das
kontaminierte Fenster selbst. Zu kurz gemessen liefert nicht eine
schwache, sondern eine zuversichtlich falsche Antwort. Es braucht daher
mindestens zehn volle Fenster, also gut zehn Minuten.

Am so gelernten Profil (Leerwert 0,067, Einschaltschwelle 0,133):

| | Fenster als belegt erkannt |
| --- | --- |
| Person sitzt still auf der Couch | 3 von 3 |
| Raum leer, Wohnung belegt | **1 von 20** |

Bis 0.13.5 stand hier „3 von 4". Das vierte Couch-Fenster war der
Restbrocken am Ende der Aufnahme — kein volles Fenster, und eine Rate aus
einer halben Minute ist mit einer aus einer ganzen nicht vergleichbar. Es
zählt jetzt gar nicht mehr mit. **Die Erkennung ist dadurch nicht besser
geworden; das Bruchstück ist verschwunden.**

Das eine falsch erkannte Leer-Fenster liegt auf Couch-Niveau, vermutlich
jemand direkt vor der Tür. Genau dieses Fenster meldet das Profil
zusätzlich als verdächtig.

Der Leerwert ist von 0,084 auf 0,067 gesunken, weil jetzt durch die
beobachtete Zeit geteilt wird und nicht durch den Abstand zwischen erstem
und letztem Messwert eines Fensters. Beides sind Zahlen über dasselbe
Zimmer, aber nicht über dieselbe Messgröße — deshalb tragen Profile eine
Version, und alte werden nicht weiterverwendet.

`GET /api/calibrations/{id}/presence-rate` lernt die Rate aus den mit
„Raum leer" markierten Messwerten einer Sitzung und bewertet die übrigen
Label daran.

**Der Preis ist rund eine Minute Latenz.** Das ersetzt die vorhandene
Bewegungserkennung nicht, die in einer Sekunde reagiert und die man zum
Lichteinschalten will. Es beantwortet die andere Frage: ist noch jemand
da.

**Was nicht behauptet wird:** ein Raum, ein Gerät, drei Sitzungen.

### Was ein Fenster ist

Ein Fenster ist eine bekannte Strecke Wanduhr, und erst danach das, was
hineinfiel. Bis 0.13.5 war es „so viele Messwerte, wie zufällig
nebeneinander lagen", und das ließ drei verschiedene Fragen wie eine
Antwort aussehen: wie lang die Strecke war, wie viel davon überhaupt
jemand zugehört hat, und wie viele Ereignisse darin lagen.

Was daraus folgte, in Zahlen: zehn Bursts aus je fünf Messwerten, jeder
Burst 0,4 s lang und im Abstand von 60 s — insgesamt **vier Sekunden**
Beobachtung — wurden als zehn Ein-Minuten-Fenster akzeptiert und als
zehn Minuten Leerwert verbucht. Und fünf hohe Werte in 0,4 s ergaben
`available: True` mit 12,5 Ereignissen/s: eine zuversichtliche Antwort
aus einer Fünftelsekunde.

Jetzt:

- **Festes Raster.** Fenster laufen vom ersten Messwert an in festen
  Schritten. Der Rest am Ende ist kein Fenster.
- **Geteilt wird durch die beobachtete Zeit**, nicht durch den Abstand
  zwischen erstem und letztem Messwert.
- **Eine Lücke ist keine Ruhe.** Ein stiller Raum meldet trotzdem — das
  Wohnzimmer um vier Uhr morgens lieferte 0,7 Messwerte pro Sekunde, und
  die längste Lücke in irgendeiner echten Aufnahme war 18,7 s. Zeit
  innerhalb einer Lücke über 30 s zählt deshalb nicht als beobachtet,
  eine gewöhnliche stille Strecke schon.
- **`warming_up` statt einer Antwort**, solange das Fenster noch nicht
  vergangen ist, und `gap`, wenn es vergangen ist, aber überwiegend ohne
  Datenquelle.

**Die Messgröße heißt jetzt, was sie ist.** Gezählt werden Messwerte
*über* der Schwelle, nicht Übergänge von darunter nach darüber. Das ist
eine Ereignisrate, keine Übertrittsrate. Die Funktion heißt
`event_rate`, und die Anzeige sagt „Ereignisse/s". Auf echte Übertritte
umzustellen wäre eine Algorithmusänderung mit neuer Profilversion und
Neukalibrierung — das steht hier bewusst nicht.

**Profile tragen eine Version.** Ein Profil aus einer älteren Auswertung
beschreibt eine andere Messgröße und wird nicht stillschweigend
weiterverwendet: das Gerät trägt dann nichts zur ratenbasierten Präsenz
bei, und die Übersicht sagt, dass eine Leer-Aufnahme neu übernommen
werden muss.

### Ein Abonnement pro Gerät, dauerhaft

Die Rate ist eine Aussage über die letzte Minute, und es gibt keine letzte
Minute, wenn niemand zugehört hat. Bis 0.13.1 wurden Messwerte nur
während einer laufenden Aufzeichnung gesammelt — für eine Kalibrierung
genug, für Live-Präsenz nicht.

Jedes gebaute Gerät hat deshalb jetzt ein eigenes, dauerhaftes Abonnement
auf seine drei Entities und ein rollendes Fenster von drei Minuten
(`app/live_presence.py`). Eine Aufzeichnung öffnet kein zweites
Abonnement mehr, sondern hängt sich als Zuhörer an das vorhandene: zwei
Abonnements auf dieselben drei Entities funktionieren zwar, aber das
zweite bringt nichts, und das erste weiß ohnehin schon, was angekommen
ist.

Drei Details, die aus echten Aufnahmen stammen:

- **Der Zwischenspeicher wird beim Start gefüllt.** Ein Abonnement meldet
  *Änderungen*; die Schwelle ändert sich nie und stünde sonst in keinem
  einzigen Sample.
- **Das Fenster wird nach Zeit begrenzt, nicht nach Anzahl.** Der
  Bewegungswert ändert sich nur, wenn er sich ändert — eine
  Zwanzig-Minuten-Aufnahme hatte Lücken bis 18,7 s. Eine feste Anzahl
  Messwerte überspannt damit völlig unterschiedlich lange Zeiträume, und
  die Rate ist pro Sekunde.
- **Beschnitten wird relativ zum neuesten Messwert**, nicht zur Wanduhr.
  Sonst leert ein Gerät, das gerade nichts meldet, sein eigenes Fenster.

Alle 30 Sekunden wird die Geräteliste abgeglichen: neue Geräte bekommen
ein Abonnement, gelöschte verlieren es, abgestürzte werden neu
verbunden.

### Das Profil an der Zone

`POST /api/calibrations/{id}/apply` übernimmt den Leerwert einer Sitzung
als Maßstab des Geräts (`Device.presence_profile`). Ohne diesen Maßstab
trägt ein Gerät zur ratenbasierten Präsenz **nichts** bei — es stimmt
nicht etwa für „leer", es schweigt. Ein Zuhause, in dem niemand
kalibriert hat, verhält sich damit exakt wie vorher.

In der Zone kommt die Rate als zweite Quelle neben die Bewegung:

    if rate_occupied:
        raw = True

Sie kann nur hinzufügen, nie widersprechen. Das ist Absicht — die
Bewegungserkennung reagiert in einer Sekunde und ist das, was man zum
Lichteinschalten will; die Rate braucht eine Minute und beantwortet die
andere Frage: sitzt da noch jemand. Ein Veto der langsamen Quelle über
die schnelle würde das Licht ausschalten, während jemand im Raum steht.

Über die Mitglieder einer Zone wird ODER gebildet, wie bei der Bewegung
auch. Das Ergebnis steht als `rate_occupied` im Zonenstatus: `true`,
`false`, oder `null`, wenn kein Mitglied einen Maßstab hat.

### Kalibrierung aus dem Verlauf

Der Raten-Detektor braucht mindestens zehn Minuten aus dem leeren Raum.
Das ist ausgerechnet die Messung, die niemand machen will — man muss die
Wohnung verlassen und einen Timer stellen. Sie liegt aber längst vor:
Home Assistant speichert den Bewegungswert in voller Auflösung, rund zehn
Tage lang. An einer echten Instanz gemessen: 0,7 Messwerte pro Sekunde,
wenn im Raum nichts passiert, 3,2 wenn doch.

`POST /api/calibrations/import` liest einen vergangenen Zeitraum aus dem
Recorder und legt daraus eine fertige Sitzung an — gleiche Form, gleiche
Label, gleiche Auswertung wie eine aufgezeichnete. Im Calibration Lab
gibt es dafür ein Formular mit Von/Bis und einem Label.

Drei Dinge, die dabei nicht offensichtlich sind:

**Der Bewegungswert gibt die Zeitachse vor, die anderen beiden werden
mitgeführt.** Die drei Entities sind drei unabhängige Reihen: der Wert
ändert sich dauernd, `motion` ein paarmal pro Stunde, die Schwelle
vielleicht seit Tagen nicht. Als parallele Zeilen gelesen hätte fast
jeder Messwert keine Schwelle — derselbe Fehler, den 0.13.1 auf dem
Live-Pfad behoben hat, nur von der anderen Seite. Der Verlauf liefert zu
jeder Entity ihren Zustand zu Beginn des Fensters, gestempelt mit dem
Zeitpunkt der letzten *echten* Änderung; deshalb hat schon der erste
Messwert eine Schwelle.

**`significant_changes_only=0` muss ausdrücklich gesetzt werden.** Home
Assistant liest den Parameter als `query.get(name, "1") != "0"` — anders
als die Nachbarn `minimal_response` und `no_attributes`, die reine
Anwesenheitsflaggen sind. Ein leerer Wert lässt den Filter also *an*, der
Recorder lässt Messwerte weg, und eine Rate pro Sekunde Wanduhr würde
danach messen, was den Filter überlebt hat.

**Ein Import ist keine Aufzeichnung.** Er wartet nicht darauf, dass eine
laufende Aufzeichnung endet, und die Sitzung wird fertig geschrieben
statt gestartet und gestoppt. Sie trägt `source: "history"`, damit später
niemand sie für etwas hält, das jemand durchgesessen hat.

Grenzen: höchstens 90 Minuten pro Import (ein Zeitraum wird vollständig
im Speicher zusammengeführt und als JSON abgelegt), mindestens 30
Sekunden, und der Recorder muss den Zeitraum noch haben.

**Was sich damit messen ließ.** Dreizehn Minuten Wohnzimmer um vier Uhr
morgens, durch denselben Detektor: **22 von 22 Fenstern exakt null
Überschreitungen**. Diese Aufnahme liegt als
`tests/data/recorder_room_empty_night.json` im Repo und ist der Grund,
warum `MIN_BASELINE_RATE` existiert — ohne den Mindestwert wäre der
Maßstab dieses Raums null, und eine einzige Überschreitung würde als
Präsenz gelten.

Damit sind es drei Ruhestufen statt zwei:

| | Überschreitungen/s |
| --- | --- |
| Wohnung leer (nachts) | **0,000** |
| Raum leer, Wohnung belegt | 0,027 |
| Person sitzt still im Raum | 0,261 |

Die mittlere Zeile ist der richtige Maßstab für den Alltag, nicht die
obere: mit 0,000 als Leerwert würde der Melder losgehen, sobald jemand
im Flur vorbeiläuft. Der Unterschied zwischen 0,000 und 0,027 ist
zugleich der Beleg, dass das Signal wirklich durch Wände geht.

### Wann ein Messwert keiner ist

Home Assistant kennt zwei Zustände, die wie ein Wert aussehen und keiner
sind: `unavailable`, wenn die Integration das Gerät verloren hat, und
`unknown`, wenn es nie einen geliefert hat. Bis 0.13.5 stand im Code
`motion["state"] == "on"` — beide wurden damit zu „keine Bewegung" auf
einem als verfügbar gemeldeten Gerät. Ein Gerät, das aus dem Netz
gefallen war, veröffentlichte nach Ablauf der Haltezeit einen
zuversichtlich leeren Raum.

Gültig sind jetzt nur `on` und `off`. Alles andere ist das Fehlen einer
Messung, und die Zone meldet dann `available: false` statt „frei".

**Ein Bewegungswert ohne funktionierenden Bewegungssensor zählt bewusst
nicht als halber Beleg.** Die Zonenlogik fällt ohne konfigurierten
Schwellwert genau auf den Bewegungs-Boolean zurück; ein halb verfügbares
Gerät würde diesen Rückfallpfad auf einen Wert stellen, den es nicht
gibt.

`NaN` und `inf` überleben `float()`, und jeder spätere Vergleich mit
ihnen ist False — ein kaputter Messwert läse sich als ruhiger Raum.
Zahlen müssen `math.isfinite` erfüllen.

### Zwei Hysteresen, zwei Gedächtnisse

Es gibt zwei unabhängige Hysteresen, und 0.13.2 hat sie sich teilen
lassen. Beides waren Fehler.

**Die Rate braucht ihr eigenes pro Gerät.** `evaluate` wählt über
`occupied_now` zwischen Einschalt- und Ausschaltschwelle, und gemeint ist
der Vorzustand *dieses Geräts*. Übergeben wurde das laufende
ODER-Ergebnis der Zonenschleife: das erste Gerät jeder Zone galt damit
immer als gerade noch leer, die Ausschaltschwelle kam nie zum Einsatz.
Der Zustand liegt jetzt in `main._rate_state`, gemerkt am neuesten
Messwert des Fensters — ohne neuen Messwert kann sich die Antwort nicht
geändert haben, also wird pro Datenpunkt genau einmal ausgewertet, egal
wie viele Zonen das Gerät enthalten. Ein Profilwechsel verwirft die
Historie, ein gelöschtes Gerät ebenso. Eine Datenlücke meldet
„unbekannt", setzt die Erinnerung aber nicht zurück.

**Nur Bewegung schreibt das Bewegungs-Gedächtnis.** `runtime.raw` ist das
Gedächtnis der Bewegungs-Hysterese: zwischen den beiden Schwellen liefert
`_raw_decision` das, was es zuletzt gesagt hat. Das kombinierte Ergebnis
dort hineinzuschreiben rastete den Bewegungspfad ein — lag der
Bewegungswert im Zwischenband, blieb die Zone belegt, lange nachdem die
Rate gefallen war. Die Rate wird jetzt erst *nach* dem Gedächtnis
hinzu-geodert.

`raw_motion` bezeichnet damit wieder Bewegung. Welche Quelle die Zone
gerade hält, steht in `trigger`: `motion`, `rate` oder nichts.

### Wo geschrieben wird, und wo nicht

Kalibrierungsdaten liegen als eine JSON-Datei. Jeder Schreibvorgang
serialisiert die **gesamte** Historie und ersetzt die Datei — an diesem
Code gemessen, sechs Sitzungen mit zusammen 120.000 Messwerten: 171 ms.
Das ist für eine Gerätekartei völlig in Ordnung und für eine wachsende
Messreihe nicht.

Bis 0.13.5 passierte das alle hundert Messwerte direkt in `ingest`, und
`ingest` läuft auf demselben Event-Loop wie die Websocket-Abonnements.
Jetzt markiert der heiße Pfad den Speicher nur als geändert, ein einzelner
Writer-Thread schreibt, und ein Schwall wird zu einem Schreibvorgang
zusammengefasst: **1673 ms → 7 ms** für tausend Messwerte im aufrufenden
Pfad.

Alles andere schreibt weiterhin synchron — anlegen, markieren, beenden,
löschen, importieren sind Benutzeraktionen, deren Ergebnis auf der Platte
sein soll, wenn die Antwort zurückgeht.

**Was das nicht löst:** `json.dumps` gibt den GIL nicht frei. Ein
Schreibvorgang hält den Interpreter weiterhin rund 171 ms an — nur eben
einmal statt zehnmal, und niemand wartet mehr darauf. Eine
append-orientierte Aufzeichnung oder SQLite wäre die eigentliche Antwort;
das steht aus, samt Migration mit Sicherung. Und ein Absturz kostet jetzt
die letzten zwei Sekunden statt der letzten hundert Messwerte.

### Was zwischen zwei Messwerten passiert

Zwei Ströme je Gerät, absichtlich getrennt.

**Messwerte** beantworten „wie oft hat dieser Raum in der letzten Minute
überschritten". **Ereignisse** beantworten „was ist passiert": eine
Bewegungsflanke, ein Quellenwechsel, ein neu begonnener Puffer.

Der Grund für die Trennung ist arithmetisch. Die Überschreitungsrate
zählt Ereignisse pro *beobachteter Sekunde*. Eine Bewegungsflanke in
diese Summe zu legen würde genau die Zahl aufblähen, die sie erklären
soll — und der Messwertzähler einer Aufnahme, den man zwischen Aufnahmen
vergleicht, würde sich verschieben, weil eine neue Zeilenart daneben
eingeführt wurde.

Der Grund für den Ereignisstrom ist eine Lücke: ein reines on→off der
Bewegung zwischen zwei Score-Ereignissen sah bis 0.13.6 nur der
Live-Zwischenspeicher. In der Aufzeichnung stand nichts davon, das
Replay konnte den Impuls nicht rekonstruieren, und dass Live und Replay
dieselbe Entscheidungsfunktion aufrufen, hieß dann nur, dass sie sie über
verschiedene Eingaben laufen ließen.

Im Replay plant ein Bewegungsereignis einen eigenen Blickzeitpunkt — die
Live-Schleife wacht dafür ja auch auf. Ein Bericht nennt außerdem, **ob**
Ereignisse aufgezeichnet wurden: eine Aufnahme von vor 0.13.7 hat keine,
und daraus soll niemand schließen müssen, dass nichts passiert ist.

### Eine Runde, ein Gerät

Die Auswertungsschleife baut je Runde ein unveränderliches Snapshot pro
Gerät: Zustand, Messwerte, seit der letzten Runde eingegangene Impulse,
Rate-Evidenz, Verfügbarkeit und Quelle. Jede Zone liest daraus.

Vorher las `compute_zone_state` Home Assistant **pro Mitgliedschaft** und
holte den Bewegungsimpuls **pro Mitgliedschaft** aus dem
Zwischenspeicher. Ein Gerät in zwei Zonen kostete zwei Abfragen, und weil
der Impuls beim Lesen verbraucht wird, sah nur die zuerst ausgewertete
Zone ihn. Zehn Zonen mit demselben Gerät kosten jetzt eine Abfrage.

Das Snapshot ist eingefroren: eine Zone, die es ändern könnte, würde
ändern, was die anderen sehen.

**Die langsame Evidenz kann drei Gründe haben zu schweigen**, und sie
stehen getrennt auf der Karte:

| | |
| --- | --- |
| kein Profil | nicht kalibriert — verhält sich wie vor der Rate |
| `disconnected` | das Abonnement ist abgerissen |
| `source_mismatch` | das Profil wurde über einen anderen Transport gelernt |
| `band_mismatch` | das Profil wurde auf einem anderen Funkband gelernt |
| `mode_mismatch` | das Profil wurde in einer anderen Funktopologie gelernt |

Bei den letzten dreien wird nicht mehr nur gewarnt: was ein Raum leer tut, ist
eine Eigenschaft dieses Raums über *einen* Messweg. Der langsame Pfad
schweigt, bis neu kalibriert wird. Der schnelle Bewegungspfad läuft
weiter — eine Zone soll ihre Bewegungserkennung nicht verlieren, weil ein
Maßstab über den falschen Weg gelernt wurde.

### Auf welchem Funkband gemessen wird

Nur der ESP32-C5 hat zwei Funkbänder. Alle anderen hier unterstützten
Chips funken auf 2,4 GHz, und ESPectres ESPHome-Komponente gibt für jede
andere Variante ein festes `2g` zurück.

Beim C5 leitet sie ihre `wifi_band_policy` aus ESPHomes eigenem
`wifi.band_mode` ab:

```python
def _runtime_wifi_band_policy():
    if get_esp32_variant() != esp32_const.VARIANT_ESP32C5:
        return "2g"
    band_mode = str(CORE.config[CONF_WIFI].get(CONF_BAND_MODE, "AUTO"))
    return _WIFI_BAND_POLICY_BY_MODE[band_mode]
```

Bis 0.13.7 setzte Echolots Template diesen Schlüssel nicht. Das ist kein
„keine Meinung": ESPHomes Vorgabe für den C5 ist `AUTO`, ein von Echolot
gebauter C5 assoziierte also dort, wo der Router ihn hinschickte — und
konnte auf einem Band messen, über das ESPectres SETUP.md selbst sagt:
*„Detection quality on 5 GHz is not characterized yet."*

Jetzt ist das Band eine Wahl mit 2,4 GHz als Vorgabe. Ein Feld, das nur
beim C5 erscheint: ESPHome nimmt `band_mode` ausschließlich für diese
Variante an (`only_on_variant(supported=[VARIANT_ESP32C5])`), überall
sonst wäre die Zeile kein wirkungsloser Schalter, sondern ein
Konfigurationsfehler. `esphome config` prüft in CI alle drei Werte.

**Das Band gehört zur Messdefinition.** 2,4 GHz und 5 GHz sind zwei
Messungen desselben Raums; was der leere Raum auf dem einen tut, sagt
nichts über das andere. Ein Profil trägt deshalb das Band, auf dem es
gelernt wurde, und ein Profil vom anderen Band macht die langsame Evidenz
stumm — dieselbe Mechanik wie beim Transportwechsel, eine Ebene tiefer.

Die Angabe ist schwächer als die Quellenangabe daneben, und das ist
absichtlich so notiert: ein Messwert trägt sein Funkband nicht mit sich,
das Band wird beim Übernehmen vom Gerät gestempelt. Genau deshalb zählt
`auto` als eigene Antwort und nicht als Platzhalter — unter `auto` weiß
auch die Aufnahme nicht, auf welchem Band sie entstanden ist, und ist
damit für keines der beiden ein Maßstab.

Profile ohne Bandangabe — alles vor 0.13.8 — zählen weiter wie bisher.
Sie sind eine richtige Antwort auf die Frage, unter der sie gelernt
wurden; `PROFILE_VERSION` zu erhöhen hieße, jedes von ihnen für ein
Merkmal wegzuwerfen, das keines je gemessen hat.

**Was das Band nicht ist: nachträglich änderbar.** Es wird in ein Image
gebacken, gehört also zu derselben Klasse wie Board und SSID, und Echolot
kennt keinen Zustand „die Konfiguration ist der geflashten Firmware
davongelaufen". `DeviceUpdate` trägt deshalb überhaupt keine
Firmware-Felder, und dieses auch nicht. Praktisch heißt das: die
Band-Prüfung oben greift heute bei wiederhergestellten oder von Hand
bearbeiteten Daten, und das Band am Profil wird notiert, damit eine
*spätere* Bandänderung keinen alten Maßstab stillschweigend weiterbenutzt.

**Was schon geflasht ist, bleibt.** Ein vor 0.13.8 angelegter C5 läuft
auf `AUTO`, und die Migration schreibt genau das in seine Konfiguration,
statt die neue Vorgabe anzuwenden. Ihn auf 2,4 GHz zu setzen ist ein
Neubau samt Flashen — eine Entscheidung, die niemand einer Migration
überlassen sollte.

### Router oder Gerätepaar

Ein Sensor misst heute die Funkstrecke **Access Point → Gerät**. TOMMY
beschreibt etwas anderes: mindestens zwei Geräte je Zone, zwischen denen
gezielt Pakete ausgetauscht werden, also eine gerichtete Strecke
**A → B**. Beide Enden der Messstrecke lassen sich dann platzieren, statt
nur eines.

| Variante | Tatsächliche Messstrecken |
|---|---|
| Echolot, ein Sensor | AP → Sensor |
| Echolot, zwei Sensoren | AP → A und AP → B — zwei Routermessungen, kombiniert |
| Echter Paarmodus | A → B |

Zwei Sensoren in einer Zone ergeben **nicht** von selbst einen A→B-Link.
Und `csi_traffic_mode: external` ist am gepinnten Commit keiner: die
Upstream-Dokumentation beschreibt dafür UDP-Pakete, die über den Access
Point zugestellt werden. Ein Paket von A an die IP von B läuft also
A → AP → B, und eine IP-Absenderadresse ist kein Nachweis des
unmittelbaren 802.11-Senders.

**Was 0.13.8 davon hat: den Datenvertrag, sonst nichts.**

- `sensing_mode` am Gerät, `router` als Vorgabe — was jedes je von
  Echolot gebaute Image tut, weshalb dafür keine Migration nötig ist.
- `peer_link` als benannter zweiter Wert, der **nicht wählbar ist**. Er
  hängt an einer Fähigkeit, die die Firmware melden muss:
  `firmware_capabilities` im Build-Manifest, mit `supports_router`,
  `supports_peer_tx`, `supports_peer_rx` und `peer_protocol_version`. Die
  gepinnte Firmware meldet nur das erste. Die Fähigkeiten stehen **im
  Manifest**, nicht in einer Tabelle im Add-on: ein vor einem Jahr
  geflashtes Gerät läuft mit der Firmware von vor einem Jahr, und was
  das Add-on heute kann, ist über sie keine Auskunft.
- Das Profil trägt den Modus mit. Ein Routerprofil wird an einem
  Peer-Link nicht stillschweigend übernommen.

Der Weg von hier zu einem echten Link steht in
[docs/paarmodus-hardwaretest.md](docs/paarmodus-hardwaretest.md), mit dem
Versuch, an dem alles hängt: **Sender abschalten — der Peer-Link muss
ausfallen, auch wenn der Router weiter Pakete schickt.** Solange das
nicht belegt ist, gibt es keinen Schalter dafür.

Ausdrücklich nicht gebaut: `LinkConfig`, `LinkSample`, ein Paar-Assistent
oder irgendeine Oberfläche. Speicher und Bedienung für eine Funkstrecke
zu bauen, die kein Gerät herstellen kann, erweckt genau den Eindruck, den
das Ganze vermeiden soll.

### Firmware-Optionen ändern, ohne das Gerät wegzuwerfen

Bis 0.13.9 war jedes Firmware-Feld bei der Geräteanlage endgültig. Eine
andere Paketrate hieß: Gerät löschen, neu anlegen — und damit seine ID,
seine Home-Assistant-Entity-IDs, sein gelerntes Profil, seine Aufnahmen
und seine Zugangsdaten verlieren, um eine Zahl zu ändern.

`PATCH /api/devices/{id}/config` ändert sie an Ort und Stelle. Der Patch
wird in die gespeicherte Konfiguration gemischt, und das Ergebnis läuft
durch **`DeviceCreate`** — das ganze Modell, keine zweite Abschrift
einzelner Regeln. Ein Band, für das das Board kein Funkmodul hat, ein zu
kurzes WPA-Passwort und eine Paketrate außerhalb von ESPectres Bereich
werden hier also aus demselben Grund und mit derselben Meldung
abgelehnt wie beim Anlegen. Wird etwas abgelehnt, bleibt das gespeicherte
Gerät unangetastet, weil die Mischung auf einer Kopie passiert.

**Zwei Felder bleiben unveränderlich.** Der Knotenname trägt jede
Entity-ID in Home Assistant und den OTA-Hostnamen; ihn hier zu ändern
würde die Entities verwaisen lassen, auf die Zonen zeigen. Und ein
anderes Board ist andere Hardware, über die ein gelerntes Profil nichts
sagt. Für beides ist ein neues Gerät der ehrliche Weg — das alte behält
seine Aufnahmen, bis jemand es absichtlich löscht.

**Die Konfiguration kann der Firmware jetzt davonlaufen**, also sagt das
jemand. Das Build-Manifest trägt seit 0.13.5 einen Fingerabdruck der
Konfiguration, aus der das Image gebaut wurde; stimmt er nicht mehr mit
dem aktuellen überein, steht `firmware_behind_config` am Gerät, auf der
Karte und in der Übersicht.

Nur für gebaute Geräte: ein ungebautes hat nichts, wovon die
Konfiguration vorauseilen könnte, und ein Manifest von vor dem
Fingerabdruck sagt ebenfalls nichts — eine Warnung, die niemand
wegbekommt, ist schlimmer als keine.

Verglichen wird der Fingerabdruck, also ignoriert der Vergleich genau
das, was der Fingerabdruck ignoriert: das WLAN-Passwort, sonst nichts.
**Auch der Anzeigename zählt** — das Template rendert `friendly_name:` in
die ESPHome-Konfiguration, und Home Assistant leitet daraus den
Gerätenamen und jede Entity-ID ab. Eine Umbenennung, die nie geflasht
wird, ist eine Umbenennung, die nie passiert. Das war eine Annahme von
mir, die beim Nachsehen im Template nicht standhielt.

Nebenwirkung, und eine erwünschte: die Messdefinition am Profil (siehe
„Auf welchem Funkband gemessen wird") war bis hierhin eine Absicherung
für wiederhergestellte Daten, weil sich kein Band ändern ließ. Jetzt
lässt es sich ändern, und die Prüfung trägt.

### Wer den Zonenzustand besitzt

Genau eine Komponente. `compute_zone_state` verändert den Zonen-Runtime —
das Gedächtnis der Bewegungs-Hysterese und die Haltefrist — und liest bei
jedem Aufruf alle Mitglieder aus Home Assistant. Bis 0.13.5 riefen das
drei Stellen auf: der Dashboard-Poll alle zwei Sekunden, die Übersicht
alle zehn, und der MQTT-Publisher. Eine Statusanzeige veränderte damit
den Automaten, über den sie berichtet.

Der ereignisgesteuerte Export in derselben Version machte es schlimmer:
ein Messwert dreimal pro Sekunde wurde zu drei vollen Runden
Home-Assistant-Abfragen pro Sekunde.

`ZoneEvaluator` wertet jetzt aus — mit einer Untergrenze von einer halben
Sekunde zwischen zwei Runden, weit unter dem, was man in einem Raum
bemerkt, und weit über der Rate, mit der Messwerte in Schwällen
eintreffen. Alles andere liest den Snapshot:

- Ein offenes Dashboard kostet **keine** Abfragen mehr.
- Die Übersicht liest, statt auszuwerten.
- MQTT bekommt weiterhin sofort mit, dass etwas passiert ist — nur eben
  aus derselben Auswertung wie alle anderen.

Nur eine Zone, die noch nie ausgewertet wurde, zahlt einmal dafür.

### Warum die Zone tut, was sie tut

Die Frage, die ein Präsenzsystem tatsächlich gestellt bekommt, ist nie
„ist der Raum belegt" — das beantwortet der Punkt auf der Kachel. Sie
lautet „warum blieb das Licht noch eine Minute an" oder „warum ging es
aus, während ich dasaß".

Der Evaluator ist die einzige Stelle, die jeden Übergang sieht, also
schreibt er sie mit: Zeitpunkt, Zustand vorher und nachher, die
auslösende Quelle (`motion`, `rate`, oder eine ablaufende Haltezeit) und
was jedes Mitgliedsgerät in dem Moment meldete.

**Ein Verlust der Messung zählt als Übergang.** Vergliche man nur den
Zustandsnamen, wäre er unsichtbar — dabei ist „das Gerät war weg" die
häufigste Erklärung für etwas, das jemand hinterher nicht versteht.

Im Zonen-Tab unter „Verlauf", oder über `GET /api/zones/{id}/timeline`
und `GET /api/timeline`.

**Bewusst im Arbeitsspeicher und bewusst begrenzt** — 200 Übergänge je
Zone. Das ist zum Nachsehen, nachdem einen etwas überrascht hat, und kein
Prüfprotokoll; eine Historie auf der Platte wäre ein zweites
Speicherproblem obendrauf (siehe oben), mit weniger Grund. Nach einem
Neustart des Add-ons ist der Verlauf leer, und die Oberfläche sagt das.

### Verfahren vergleichen, ohne etwas anzufassen

Die vorhandenen Zahlen kommen aus einem Raum und wenigen Sitzungen. Eine
Änderung, die eine davon verbessert, kann eine andere ruinieren — also
wird sie zuerst gemessen und nicht verdrahtet.

`GET /api/calibrations/{id}/replay` liefert **zwei Durchläufe** über
dasselbe Material, weil sie Verschiedenes beantworten.

**Chronologischer Durchlauf** (`simulation`) schickt die Aufnahme
vorwärts durch genau die Funktionen, die auch der Livebetrieb nimmt:
`presence_rate.advance` für den Gerätebefund samt seinem
Hysterese-Gedächtnis und `zone_logic.evaluate` für die Zone. Die Uhr wird
injiziert — es sind die Zeitstempel der Aufnahme selbst — und Labels
werden erst hinterher gelesen, um zu bewerten, nie um die Eingabe
umzusortieren. Daneben läuft dieselbe Maschine mit abgeschalteter Rate:
Bewegung, Hysterese und Haltezeit, also das, was das Add-on vor der Rate
getan hat und was sie schlagen muss.

Je Durchlauf:

| | |
| --- | --- |
| **belegt** | wie lange die Zone belegt war |
| **fälschlich belegt** | belegt, während das Label „Raum leer" sagt |
| **fälschlich leer** | leer, während das Label „belegt" sagt |
| **Eintritt / Freigabe** | wie lange es dauerte, bis der Wechsel kam (Median und schlechtester Fall) |

Dazu eine **vollständige Zeitbilanz**: belegt, leer, Aufwärmen, Lücke,
unbekannt und Rest. Die Teile summieren sich auf die Gesamtdauer. Vorher
verschwand Zeit lautlos aus dem Bericht — `split_windows` wirft ein
angefangenes Restfenster weg, bevor irgendetwas es zählt, also meldete
eine zehnsekündige Aufnahme *null* bewertete und *null* unbrauchbare
Fenster. Jetzt steht sie als zehn Sekunden „Aufwärmen" da.

**Fenstervergleich** (`window_comparison`) ist der ältere Bericht und
bleibt: er gruppiert nach Label und bewertet jedes Label für sich, über
die Ereignisrate bei 15, 30, 60 und 120 Sekunden plus den
Bewegungs-Boolean. Als Überblick nützlich, als Simulation nicht — die
Gruppierung zerstört die Reihenfolge der Sitzung, und jedes Fenster wird
aus dem Stand bewertet, sodass die Ausschaltschwelle dort nie zum Einsatz
kommt.

Je Verfahren:

| | |
| --- | --- |
| **Fehlalarm** | Fenster mit Label „Raum leer", die als belegt gelten |
| **Verpasst** | Fenster mit „belegt", die als leer gelten |
| **Nicht beurteilbar** | zu kurz oder Lücke — getrennt ausgewiesen |

Die letzte Spalte ist Absicht: eine Erfolgsquote, die ihre Unbekannten
versteckt, ist keine.

**Schreibfrei.** Kein Zonen-Runtime, kein Geräteprofil, nicht der
Live-Evaluator. Messen darf nicht das Licht bewegen.

**Herkunft steht im Bericht** (`identity`), nicht in der Ansicht. Sitzung
und Maßstab mit ID, Gerät, Quelle und Zeitraum; dazu `parameters` mit
Fensterlänge, Schwelle, Haltezeit, Takt, Mindestabdeckung und
Gedächtnisdauer. Eine Zahl ohne die Einstellungen, aus denen sie stammt,
ist keine Messung.

**Es sagt, wenn die Zahlen sich selbst messen.** `in_sample` ist nicht
„wurde ein Maßstab angegeben". Eine Sitzung, die als ihr eigener Maßstab
genannt wird, ist in-sample, egal wie sie übergeben wurde — und eine, die
sich zeitlich mit dem Bewerteten überschneidet, auch: dieselben Minuten
können nicht zugleich Maßstab und Gemessenes sein. `?baseline=<andere
Sitzung>` lernt den Leerwert aus einer anderen Sitzung und macht daraus
einen echten Vergleich.

**Ein fremdes Gerät wird abgelehnt.** Was ein Raum leer tut, ist eine
Eigenschaft dieses Raums und dieser Antenne. Ein Maßstab von einem
anderen Gerät ist ein Übertragungsexperiment und muss so genannt werden:
`?transfer=true`, sonst antwortet die Route mit 409.

`?hold_seconds=<n>` simuliert eine Nachlaufzeit, die es in der Aufnahme
nicht gab — nützlich, um zu sehen, was eine Zoneneinstellung geändert
hätte, bevor man sie setzt.

Im Calibration Lab unter „Replay" an jeder Messung.

### Confidence fusion

For zones whose devices stream direct telemetry, Echolot computes a second
opinion alongside the classic binary zone state. Each member contributes:

- a presence probability, taken from its newest completed calibration
  profile, otherwise from its current device threshold, otherwise from the
  motion boolean alone;
- a reliability value from calibration quality and sample age;
- the basis it used (`calibrated_score`, `device_threshold`, `motion_only`).

Samples start losing weight after five seconds and expire after twenty.
An expired device therefore makes the fused result *unavailable* rather
than voting for an empty room. Reliable probabilities are combined as a
weighted mean, so adding many weak sensors cannot force a zone to
occupied. The result is **occupied**, **vacant**, or explicitly
**uncertain** — the last one when devices disagree, instead of hiding the
disagreement behind OR logic.

**It is a second opinion, not the verdict.** The dot and the state text on
a zone tile still come from the zone state machine, and that machine —
with its hold time — is what drives Home Assistant and the MQTT export.
The fusion gets its own line underneath, with the per-device breakdown in
its tooltip. Two things writing one element would flip the tile between
two answers, and would overwrite the very value the fusion is worth
comparing against. Seeing "frei" above "Direkt-Fusion: belegt · 78 %" is
the point: that disagreement is information, and it is what a walk test
resolves.

Endpoints: `GET /api/fusion/zones` for all zones, `GET
/api/fusion/zones/{id}` for one. The dashboard polls the first every two
seconds while its tab is on screen.

## Support

This is a personal/community project, not affiliated with TOMMY or
ESPectre's upstream maintainers. File issues against this repository.
