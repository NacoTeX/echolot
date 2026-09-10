# Paarmodus, Stufe 2: existiert der Funklink überhaupt?

Nichts hier gehört zum Add-on. Der Dockerfile kopiert `app/` und
`rootfs/`, sonst nichts — dieses Verzeichnis ist ein Versuchsaufbau, kein
Produkt. Ziel von Stufe 2 ist **ein dokumentierter Funklink mit
Messdaten**, kein freigegebener Anwesenheitssensor.

## Was am gepinnten Stand nachgelesen wurde

Vier Befunde, alle aus dem Quelltext und nicht aus Prosa. Sie ändern den
Plan, deshalb stehen sie zuerst.

### 1. Ein echter A→B-Link ist ESP-NOW, nicht UDP über den Router

Espressifs eigenes Beispielpaar sendet ESP-NOW-Broadcast auf einem fest
eingestellten Kanal und assoziiert sich **nie** mit einem Access Point:

```c
esp_now_peer_info_t peer = { .channel = 11, .ifidx = WIFI_IF_STA,
                             .peer_addr = {0xff,0xff,0xff,0xff,0xff,0xff} };
```

— `esp-csi/examples/get-started/csi_send/main/app_main.c`. Kein
`esp_wifi_connect()`, dafür `esp_wifi_set_channel(11, …)` und beim
Empfänger `esp_wifi_set_promiscuous(true)`.

Das ist der Unterschied zu `csi_traffic_mode: external`, das die
ESPectre-Doku als UDP über den Access Point beschreibt. Hier gibt es
wirklich nur den Weg A→B.

### 2. ESPectre kann diese Frames nicht auswerten

Der Frame-Filter am gepinnten Commit `ce23b0b61b95` ist vollständig
IP-basiert. `csi_frame_identity.cpp` parst LLC/SNAP → IPv4 → ICMP/UDP/TCP
und vergleicht gegen lokale IP, Gateway-IP, Multicast-IP, UDP-Port und
ICMP-Identifier:

```cpp
bool parse_ipv4(const uint8_t *payload, size_t payload_len, ParsedIpv4 *out) {
  …
  if (std::memcmp(payload, kLlcSnapPrefix, sizeof(kLlcSnapPrefix)) != 0 ||
      read_be16(payload + 6U) != kEtherTypeIpv4) return false;
```

Ein ESP-NOW-Frame ist ein herstellerspezifisches 802.11-Action-Frame und
hat überhaupt keinen LLC/SNAP-IPv4-Kopf. `parse_bounded_payload` liefert
also `false`, und das Frame wird verworfen. **Der Paarmodus lässt sich
nicht auf ESPectres vorhandenen Filter aufsetzen.**

### 3. Es gibt genau einen CSI-Callback

`esp_wifi_set_csi_rx_cb(cb, ctx)` registriert *einen* Callback
(ESP-IDF 5.5.4, `components/esp_wifi/include/esp_wifi.h`). Die letzte
Registrierung gewinnt. Solange ESPectre ihn hält, kann daneben kein
zweiter Empfänger CSI bekommen.

Zusammen mit Befund 2 heißt das: **Routermessung und Paarlink können auf
demselben Chip nicht gleichzeitig laufen.** Ein Paargerät ist eine andere
Firmware, kein Betriebsmodus der vorhandenen — was Stufe 3 mit „Beim
Flashen Auswahl Standard/Experimentell" schon vermutet hatte und jetzt
belegt ist.

### 4. Fester Kanal und Uplink schließen sich nicht aus, aber vertragen sich schlecht

Eine Station folgt dem Kanal ihres Access Points. Das Beispiel umgeht das,
indem es sich gar nicht erst verbindet. Ein Paargerät, das gleichzeitig
mit Home Assistant sprechen soll, müsste den Link auf dem Kanal des
Routers betreiben — und verliert ihn, sobald der Router den Kanal
wechselt. Das ist Punkt 5 der Stufe-2-Liste und muss gemessen werden,
nicht angenommen.

### Nebenbefund: die Beispiel-MAC ist keine Identität

Beide Beispiele setzen dieselbe MAC:

```c
ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, CONFIG_CSI_SEND_MAC));
```

— auch der **Empfänger**, obwohl er auf genau diese Adresse filtert.
Für den Abschalttest ist das folgenlos, für alles danach nicht: die
gefilterte Adresse ist eine im Binary einkompilierte Konstante, kein
Nachweis, dass das Frame von *deinem* Gerät A kam. Gib den beiden Boards
verschiedene feste MACs, bevor du Aussagen über Zuordnung machst.

## Der Aufbau

Zwei ESP32-C5, beide auf **2,4 GHz**. Espressifs Beispiel pinnt auf dem
C5 selbst `WIFI_BAND_MODE_2G_ONLY`, und ESPectre nennt die
Erkennungsqualität auf 5 GHz nicht charakterisiert. Eine Variable pro
Versuch.

Aus dem Beispiel-README, und beides ernst nehmen:

- **Externe Antenne.** Die PCB-Antenne ist richtungsarm und wird vom
  Board gestört.
- **Mehr als ein Meter Abstand.**

## Der Empfänger-Patch

`csi_recv-liveness.patch` gegen `espressif/esp-csi` bei `8633d67`.
38 Zeilen, zwei Gründe:

**Der Abschalttest wäre sonst nicht falsifizierbar.** Das Beispiel
verwirft jedes Frame, dessen Sender nicht der Peer ist, und druckt sonst
nichts. Stille am Ende einer Aufnahme heißt dann: der Link ist tot —
oder das Board hat neu gestartet, oder das USB-Kabel ist rausgerutscht.
Alle drei Fälle sehen in der Datei gleich aus. Ein Versuch, dessen
Misserfolg wie sein Erfolg aussieht, ist kein Versuch.

Der Patch zählt deshalb Peer- und Fremd-Frames und gibt einmal pro
Sekunde eine Zeile aus:

```
CSI_STAT,<uptime_ms>,<peer_frames>,<other_frames>
```

**Aus einer eigenen Task, nicht aus dem CSI-Callback.** Wenn der Peer aus
ist *und* der Raum gerade still ist, läuft überhaupt kein Callback — und
genau dann braucht der Versuch den Beweis, dass der Empfänger noch da
ist. Ein Herzschlag, der aussetzt, sobald es nichts zu hören gibt, kann
einen toten Link nicht von einem toten Board unterscheiden. Nebenbei ist
das auch Punkt 7 der Stufe-2-Liste: im Callback nur begrenzte Arbeit.

```bash
git clone https://github.com/espressif/esp-csi
cd esp-csi && git checkout 8633d67
git apply /pfad/zu/echolot/experiments/peer-link/csi_recv-liveness.patch
```

## Durchführung

```bash
# Sender
cd esp-csi/examples/get-started/csi_send
idf.py set-target esp32c5
idf.py -p /dev/ttyACM0 flash

# Empfänger (gepatcht)
cd ../csi_recv
idf.py set-target esp32c5
idf.py -p /dev/ttyACM1 flash

# Aufnahme — ohne idf.py monitor, das will denselben Port
cat /dev/ttyACM1 | tee lauf1.log
```

Ablauf eines Durchgangs:

1. Beide an, **60 s** laufen lassen. Der Router muss in dieser Zeit
   normalen Verkehr machen — er ist die Kontrolle.
2. **Sender abschalten** (Strom ziehen, nicht nur das Programm beenden).
3. **30 s** warten.
4. **Sender wieder an**, weitere 30 s. Dieser Schritt ist der wichtigere
   Teil: kommt der Link zurück, ist ein zwischenzeitlich abgestürzter
   Empfänger ausgeschlossen.

Danach:

```bash
python echolot/tools/peer_link_report.py lauf1.log
```

Das Werkzeug urteilt vierwertig, nicht bestanden/durchgefallen:

| Ergebnis | Bedeutung |
| --- | --- |
| `BESTANDEN` | ≥ 5 s ohne Peer-Frame, Empfänger lief weiter und hörte in derselben Zeit fremden Verkehr |
| `NICHT DURCHGEFÜHRT` | der Peer hat nie aufgehört, oder die Stille war zu kurz |
| `UNENTSCHIEDEN` | Stille lang genug, aber der Empfänger hörte auch sonst nichts — toter Link und totes Funkmodul sehen von hier gleich aus |
| `NICHT AUSWERTBAR` | keine `CSI_STAT`-Zeile; der Patch fehlt |

## Was danach noch aussteht

Der Abschalttest ist die Eintrittskarte, nicht die Stufe. Offen bleiben
aus der Stufe-2-Liste:

- **Punkt 2** — Frameformat, PHY-Modus, Kanal und Bandbreite belegen
  statt annehmen. Die Beispiele setzen HT40/11N/MCS0_LGI; ob der
  Empfänger genau das sieht, sagt `rate` und `rx_format` in jeder Zeile.
- **Punkt 5** — Koexistenz mit dem Management-Uplink: Kanalwechsel am
  Router, Routerneustart, AP-Wechsel.
- **Punkt 6** — der Adapter, der aus Peer-CSI etwas macht, das ein
  Detektor auswerten kann. Befund 2 und 3 sagen, wo er hin muss.
- Abschattungsversuche: eine Kurve, die sich beim Abschatten der Strecke
  A→B ändert und beim Abschatten von AP→B nicht, ist der eigentliche
  Beleg dafür, dass die Geometrie eine andere ist.

Erst wenn das steht, wird Stufe 4 sinnvoll — und erst danach darf
irgendeine Oberfläche einen Paarmodus anbieten.

## Herkunft

`csi_send` und `csi_recv` stammen aus
[espressif/esp-csi](https://github.com/espressif/esp-csi) (Apache-2.0).
Sie sind hier **nicht** einkopiert, damit kein Fork entsteht, den niemand
pflegt — nur der Patch liegt hier, gegen einen benannten Commit.
