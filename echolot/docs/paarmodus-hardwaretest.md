# Paarmodus: Hardwaretestplan für zwei ESP32-C5

Dieser Plan gehört zu Stufe 2 und Stufe 4 des Paarmodus-Auftrags. Er
beschreibt, **wie nachgewiesen wird, dass ein gerichteter Funklink
zwischen zwei Sensoren überhaupt existiert** — und danach, ob er im Raum
besser misst als das, was Echolot heute tut.

Der Plan ist bewusst als Vorbereitung geschrieben. In Echolot 0.13.8
existiert vom Paarmodus nur der Datenvertrag: `sensing_mode` mit den
Werten `router` und `peer_link`, eine Fähigkeitsmeldung der Firmware, und
die Messdefinition am Profil. `peer_link` ist nicht wählbar, weil keine
Firmware `supports_peer_rx` meldet. **Kein Schritt hier darf als
erledigt gelten, weil er beschrieben ist.**

> **Stand 0.13.9:** Die Software-Hälfte von Stufe 2 ist gebaut und liegt
> in [`experiments/peer-link/`](../experiments/peer-link/README.md) — vier
> Befunde aus dem Quelltext, der Empfänger-Patch, der den Abschalttest
> überhaupt falsifizierbar macht, und `tools/peer_link_report.py`, das
> eine Aufnahme zum Urteil macht. Auf Hardware ist davon nichts belegt.

## Warum überhaupt ein Nachweis nötig ist

`csi_traffic_mode: external` ist am gepinnten ESPectre-Commit
`ce23b0b61b95` kein Funklink, sondern ein Weg, CSI-auslösenden Verkehr
von außen bereitzustellen — die Upstream-Dokumentation beschreibt dafür
ausdrücklich UDP-Pakete, die über den Access Point zugestellt werden.

Ein UDP-Paket von ESP A an die IP von ESP B läuft im normalen
Infrastruktur-WLAN also **A → AP → B**. Die IP-Absenderadresse sagt
nichts darüber, wer der unmittelbare 802.11-Sender war. Wer diese
Konstruktion „Paarmodus" nennt, misst weiterhin den Router und hat nur
den Namen geändert.

Espressifs `esp-csi`-Beispiele zeigen, dass ein Zwei-Chip-Link baubar
ist. Das ist ein Grund für einen Prototyp, keine Eigenschaft der
Firmware, die Echolot heute ausliefert.

## Hardware für diesen Plan

Zwei ESP32-C5, **beide auf 2,4 GHz festgenagelt** (`wifi_band: 2.4GHz`,
seit 0.13.8 die Vorgabe).

Das weicht bewusst von der Empfehlung des Reviews ab, mit zwei C6
anzufangen: verfügbar sind zwei C5. Der Grund, sie trotzdem auf 2,4 GHz
zu betreiben, ist der eigentliche Punkt der Empfehlung — **eine Variable
pro Versuch**. ESPectre bezeichnet die Erkennungsqualität auf 5 GHz
selbst als nicht charakterisiert. Wer den Funklink und das unbekannte
Band gleichzeitig einführt, kann ein schlechtes Ergebnis keinem von
beiden zuordnen.

5 GHz ist ein eigener, späterer Versuch — nach einem funktionierenden
Link auf dem vermessenen Band.

## Stufe 2: existiert der Link?

Ziel dieser Stufe ist **nicht** ein Anwesenheitssensor. Ziel ist ein
dokumentierter Funklink mit Messdaten.

### 2.1 Sender und Empfänger festlegen

Feste Rollen: A sendet, B empfängt und wertet aus. Kein automatischer
Rollenwechsel — er ändert die gerichtete Messstrecke und verlangt eine
neue Kalibrierung.

Zu protokollieren: Boardrevision, Antennenausrichtung, Standort beider
Geräte auf einer Skizze, Abstand, Wände dazwischen, Routerstandort.

### 2.2 Framformat, PHY, Kanal, Bandbreite nachweisen

Nicht annehmen, dass beliebige ESP-NOW- oder Broadcast-Voreinstellungen
dieselben CSI-Daten liefern wie der bisherige Empfangspfad. Festzuhalten
und zu belegen:

- welches 802.11-Frameformat der Sender erzeugt,
- PHY-Modus und Bandbreite (die Laufzeit fixiert 20 MHz),
- der Kanal, auf dem beide stehen, und wie er zum Uplink-Kanal steht.

### 2.3 Empfang am tatsächlichen Sender festmachen

Der Empfänger muss zwischen „Frame von A" und „Frame vom AP oder von
irgendeinem Nachbargerät" unterscheiden, **bevor** irgendetwas in den
Detektor läuft. Zuordnung über den echten 802.11-Sender, nicht über eine
IP.

### 2.4 Der Abschalttest

> Durchführung und Auswertung stehen jetzt ausformuliert in
> [`experiments/peer-link/README.md`](../experiments/peer-link/README.md).
> Kurz: das Beispiel verwirft alles außer Peer-Frames, deshalb ist Stille
> am Ende einer Aufnahme nicht von einem abgestürzten Empfänger zu
> unterscheiden. Der Patch dort lässt den Empfänger einmal pro Sekunde
> mitzählen, aus einer eigenen Task — sonst schweigt auch der Herzschlag,
> sobald es nichts zu hören gibt.

Der einzige Versuch, der den Link wirklich beweist:

> **Sender A abschalten. Der Peer-Link muss ausfallen — auch wenn der
> Router weiter Pakete an B schickt.**

Bleibt B „belegt" oder misst weiter Ereignisse, dann misst B den Router
und nicht A. Das Ergebnis ist dann negativ, egal wie plausibel die Kurven
aussehen.

Gegenprobe in dieselbe Richtung: A wieder an, ohne dass sich sonst etwas
ändert — der Link muss wiederkommen, und zwar erkennbar in Sequenz und
Empfangszeit, nicht nur in einem Gesamtzustand.

### 2.5 Belegen, dass die Daten vom geplanten Link stammen

Aufzuzeichnen und auszuwerten: Sequenznummern, Anzahl gültiger
CSI-Frames, Empfangslücken, Signaländerung bei bewusster Störung (Hand
vor die Antenne, Person zwischen die Geräte). Eine Kurve, die sich beim
Abschatten der Strecke A→B ändert und beim Abschatten der Strecke AP→B
nicht, ist der eigentliche Beleg.

### 2.6 Koexistenz mit dem Management-Uplink

Ein Funkchip kann nicht gleichzeitig beliebig getrennte Kanäle bedienen.
Zu testen:

- WLAN-Kanalwechsel am Router,
- Routerneustart,
- AP-Wechsel (Roaming zwischen zwei APs),
- was mit dem Home-Assistant-Uplink passiert, während der Link läuft.

Ein Link, der den Uplink verliert, ist kein brauchbarer Sensor, sondern
ein Gerät, das man nicht mehr erreicht.

### 2.7 Aufwand im CSI-Callback

Im Callback nur begrenzte Arbeit und Übergabe an eine Queue; die
Signalverarbeitung läuft getrennt. Das ist auch Espressifs eigene
Empfehlung, und es ist der Unterschied zwischen einem Link, der unter
Last funktioniert, und einem, der nur im Leerlauf misst.

### Abbruchkriterium

Gelingt kein stabiler Peer-Empfang, wird **kein** scheinbar
funktionierender UI-Modus ausgeliefert. Ein Schalter, der eine
Funkfunktion verspricht, die es nicht gibt, ist schlimmer als kein
Schalter.

## Stufe 4: misst er im Raum besser?

Erst nach einem bestandenen Abschalttest. Drei Varianten, mit denselben
zwei Geräten:

| | Aufbau |
|---|---|
| **A** | ein vorhandener Sensor gegen den Router |
| **B** | zwei vorhandene Sensoren, aktuelle Zonenaggregation |
| **C** | echter Peer-Link zwischen denselben zwei Geräten |

Nur so wird sichtbar, ob ein Vorteil aus der zusätzlichen Hardware, aus
der Aufstellung oder aus dem direkten Link kommt. Varianten wiederholt
und in wechselnder Reihenfolge messen, damit Tageszeit- und
WLAN-Effekte nicht einer Variante zugeschrieben werden.

### Szenarien

Leerer Raum · Durchqueren · still sitzen · liegen · Bewegung im
Nachbarraum · Haustier · Ventilator oder Vorhang · anderer WLAN-Verkehr ·
Verbindungsabbruch und Wiederanlauf.

Zu jedem Durchgang: Geräte- und Routerstandorte, Kanal,
Antennenausrichtung.

### Kennzahlen

- Fehlbelegungsdauer pro leerer Stunde
- Falsch-leer-Dauer bei ruhiger Anwesenheit
- Eintritts- und Freigabeverzögerung
- Anzahl Zustandswechsel
- `unavailable`-Zeit
- gültige CSI-Abdeckung
- Verkehr/Airtime, soweit messbar
- CPU und RAM auf dem Pi, Ressourcen auf dem ESP

**Trainings- und Testaufnahmen trennen.** Ein Profil, das auf denselben
Daten gelernt und geprüft wurde, sagt über den nächsten Tag nichts.

**Eine längere Haltezeit ist keine bessere Erkennung.** Wer die
Haltefrist erhöht, verschiebt Falsch-leer nach Fehlbelegung — das ist
eine Entscheidung, kein Fortschritt, und muss getrennt ausgewiesen
werden.

### Freigabe

Zuerst Vergleichsbetrieb ohne Einfluss auf Automationen, danach bewusst
aktivierbar. Keine Aussage „genauer als Standard" ohne Messergebnisse.
Fällt der Vergleich nicht klar aus, wird der Paarmodus als alternative
Aufstellungsoption beschrieben — nicht als bessere.

## Was Echolot dafür schon mitbringt

- Den Versuchsaufbau samt Auswertung:
  [`experiments/peer-link/`](../experiments/peer-link/README.md) und
  `tools/peer_link_report.py`. Das Urteil ist vierwertig — „unentschieden"
  und „durchgefallen" verlangen verschiedene Reparaturen.
- `sensing_mode` am Gerät, mit `router` als Vorgabe und `peer_link`
  hinter einer Fähigkeitsprüfung.
- `firmware_capabilities` im Build-Manifest: `supports_router`,
  `supports_peer_tx`, `supports_peer_rx`, `peer_protocol_version` — mit
  dem Image aufgezeichnet, damit ein vor einem Jahr geflashtes Gerät
  weiterhin sagen kann, was es kann.
- Die Messdefinition am Profil (`source`, `band`, `sensing_mode`): ein
  Routerprofil wird an einem Peer-Link nicht stillschweigend übernommen,
  sondern macht die langsame Evidenz stumm, mit Begründung.
- Das Replay, das dieselbe Entscheidungsfunktion über aufgezeichnete
  Daten laufen lässt — womit sich die Varianten A/B/C an denselben
  Aufnahmen vergleichen lassen, statt nur nacheinander im Raum.

## Was ausdrücklich fehlt

Kein `LinkConfig`, kein `LinkSample`, keine Paar-Oberfläche, kein
Assistent. Das ist Absicht: Speicher und Oberfläche für eine Funkstrecke
zu bauen, die kein Gerät herstellen kann, erzeugt genau den Eindruck, den
dieser Plan vermeiden soll.
