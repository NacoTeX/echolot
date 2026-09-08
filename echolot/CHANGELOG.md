# Changelog

## 0.13.2

Der Raten-Detektor aus 0.13.0/0.13.1 war bisher ein Auswertungswerkzeug:
er konnte eine fertige Aufnahme beurteilen, aber nichts im laufenden
Betrieb. Jetzt hängt er an der Zone.

- **Jedes Gerät hat ein dauerhaftes Abonnement.** Die Rate ist eine
  Aussage über die letzte Minute, und es gibt keine letzte Minute, wenn
  niemand zugehört hat. Bisher wurden Messwerte nur während einer
  Aufzeichnung gesammelt. Jedes gebaute Gerät bekommt jetzt ein eigenes
  Abonnement und ein rollendes Drei-Minuten-Fenster; alle 30 Sekunden
  wird die Geräteliste abgeglichen, damit neue Geräte eines bekommen und
  abgestürzte neu verbunden werden.
- **Eine Aufzeichnung öffnet kein zweites Abonnement mehr,** sondern
  hängt sich an das vorhandene. Zwei Abonnements auf dieselben drei
  Entities funktionieren zwar, aber das zweite bringt nichts, und das
  erste weiß ohnehin schon, was angekommen ist.
- **Das Fenster wird relativ zum neuesten Messwert beschnitten,** nicht
  zur Wanduhr — sonst leert ein Gerät, das gerade nichts meldet, sein
  eigenes Fenster, und genau dann fehlt der Verlauf, der die Frage
  beantworten soll.
- **`POST /api/calibrations/{id}/apply`** übernimmt den Leerwert einer
  Sitzung als Maßstab des Geräts.
- **Die Zone bekommt die Rate als zweite Quelle.** Sie kann nur
  hinzufügen, nie widersprechen: die Bewegungserkennung reagiert in einer
  Sekunde und ist das, was Licht einschaltet; die Rate braucht eine
  Minute und hält, solange jemand da ist. Ein Veto der langsamen Quelle
  über die schnelle würde das Licht ausschalten, während jemand im Raum
  steht.
- **Ohne Maßstab schweigt ein Gerät,** es stimmt nicht für „leer". Ein
  Zuhause, in dem niemand kalibriert hat, verhält sich exakt wie vorher.

Geprüft gegen die drei echten Aufnahmen: die Couch-Sitzung schaltet die
Zone über den Ratenpfad auf belegt, die Leer-Aufnahme nicht. Die letzte
Minute der Couch-Aufnahme liest sich weiterhin als leer — das ist die
bekannte Grenze aus 0.13.1 und steht als eigener Test da, statt umgangen
zu werden.

**Nebenbei, Kosmetik.** Das Icon war der generische WLAN-Fächer — er sagt
„funkt" und sonst nichts, und die halbe Seitenleiste sieht so aus. Neu ist
ein Sonarbild: Sender in der Mitte, Reichweitenringe, Peillinie, ein
Kontakt. Das ist, was „Echolot" heißt und was das Gerät tut. Der Kontakt
ist bernsteinfarben, weil Weiß mit Schein wie ein Mond aussieht; drei
Ringe statt vier, weil der vierte bei 44 px die Ecken zustellt; die
Peillinie bleibt, weil Ringe plus Punkt sonst eine Zielscheibe sind. Die
Geometrie steht als `tools/make_brand.py` im Repo, nicht nur als PNG.

Dazu die Beschreibungen: Add-on, Repository und README sagen jetzt zuerst,
was das Ding *tut*, statt gegen wen es antritt, und nennen die Grenzen —
Räume statt Positionen, Kalibrierung nötig, bislang ein Raum und ein
Gerät. Und vier Stellen zeigten noch auf den alten Repository-Namen
`claudeandI`; GitHub leitet zwar um, aber die URL, die man in Home
Assistant einträgt, sollte stimmen.

## 0.13.1

Eine zwanzigminütige Aufnahme — Raum leer, während jemand durch die übrige
Wohnung lief — hat zwei Fehler von 0.13.0 aufgedeckt und die wichtigste
offene Frage beantwortet.

**Das Signal geht durch Wände, aber nicht weit genug, um zu stören.**
Zwanzig Minuten Herumlaufen in der Wohnung erzeugten 0,027
Überschreitungen pro Sekunde, still auf der Couch sitzen 0,261 — Faktor
zehn. Raumbezogene Präsenz ist damit erreichbar. Das war das größte
verbliebene Risiko.

- **Die Schwelle fehlte in jeder Zeile der Aufnahme.** Ein Abonnement
  meldet *Änderungen*; die Schwelle ändert sich nie, wurde also nie
  gemeldet und stand in keinem Sample. Der Sampler liest die drei Entities
  jetzt einmal beim Start und füllt den Zwischenspeicher vor. Eine
  Meldung, die vorher eintrifft, gewinnt gegen den Startwert.
- **Gezählt wird pro Sekunde statt pro Messwert.** Home Assistant meldet
  nur Änderungen, und der Bewegungswert steht lange exakt auf null — die
  Aufnahme hatte Lücken bis 18,7 s. Ein Anteil an den Messwerten wertet
  damit ausgerechnet die stillen Zeiträume auf. Die Trennung zwischen
  „nebenan" und „weit weg" verbessert sich dadurch von Faktor 38 auf 112.
- **Der Leerwert ist das 90er-Perzentil der Fenster, nicht ihr Median.**
  Vierzehn von zwanzig Fenstern kreuzten exakt null Mal; der Median ist
  0,0 und lässt einen willkürlichen Mindestwert die Arbeit machen — 6 von
  20 Leer-Fenstern galten dann als belegt. Maßgeblich ist nicht, was der
  leere Raum üblicherweise tut, sondern was er schlimmstenfalls tut.
- **Kurze Leer-Aufnahmen werden abgelehnt.** Bei drei Fenstern ist das
  90er-Perzentil das größte davon: die alte Zweieinhalb-Minuten-Aufnahme
  ergab einen „Leerwert" von 0,393 — das kontaminierte Fenster selbst. Zu
  kurz gemessen gibt keine schwache, sondern eine zuversichtlich falsche
  Antwort. Mindestens zehn volle Fenster.

Am neuen Profil: **3 von 4** Couch-Fenstern erkannt, **1 von 1** vor der
Tür, **1 von 20** Leer-Fenstern falsch — und genau dieses eine meldet das
Profil selbst als verdächtig.

Bestätigt nebenbei: das Abonnement aus 0.13.0 arbeitet. Der häufigste
Abstand zwischen Messwerten ist 0,25 s statt des früheren Polling-Takts
von 0,5 s. Die Gesamtrate bleibt bei 1,6/s, weil der Wert lange
unverändert bleibt und dann nichts zu melden ist — meine frühere Schätzung
von 6/s stammte aus einer Phase direkt nach dem Boot und war falsch.

Geprüft: 229 Tests. Die dritte echte Aufnahme liegt als
`tests/data/flat_occupied_room_empty.csv` bei.

## 0.13.0

Aus zwei echten Aufnahmen entstanden — fünf Minuten still auf der Couch,
zweieinhalb Minuten derselbe Raum leer. Beide liegen jetzt als
`tests/data/*.csv` im Projekt und sind die Prüfsteine für alles Folgende.

**Der Sampler abonniert, statt zu fragen.** Das Gerät veröffentlicht rund
sechs Werte pro Sekunde an Home Assistant; das Polling holte 1,5 mit
Lücken bis 13 s — etwa ein Viertel. Über Home Assistants Websocket-API
(`subscribe_trigger`, serverseitig auf unsere drei Entities gefiltert)
kommt jede Änderung an, und die Wiederholungsfilterung entfällt, weil
Home Assistant nur bei Änderungen sendet.

**Präsenz aus der Überschreitungsrate statt aus Einzelwerten.** Die Daten
haben drei Dinge gezeigt:

- Ein einzelner Messwert trennt „Person sitzt still" und „Raum leer" kaum
  — AUC 0,616. Unsere eigene `recommendation()` sagte das korrekt mit
  `quality: poor` und 98 % Falsch-Negativ-Rate.
- Der leere Raum überschreitet ebenfalls, in 3,0 % der Messwerte. Damit
  ist die Haltezeit, die ich zuvor empfohlen hatte, unbrauchbar: 60 s
  Haltezeit ergaben 95 % belegt mit Person — und 40 % belegt ohne.
- Über ein 60-Sekunden-Fenster trennt die Rate: AUC 0,931.

`app/presence_rate.py` lernt die Leerlaufrate eines Raums und meldet
Präsenz, wenn die Rate der letzten Minute deutlich darüber liegt. Preis:
rund eine Minute Latenz. Das ersetzt die Bewegungserkennung nicht, es
beantwortet die Frage, die sie nicht beantworten kann.

**Median statt Mittelwert, und eine Warnung.** Die erste Leer-Aufnahme
enthielt in ihrer letzten Minute jemanden, der zurück in den Raum kam:
Fenster bei 0,014, 0,038 und 0,217. Der Mittelwert (0,090) liegt über
jedem sauberen Fenster und hätte die Einschaltschwelle unerreichbar
gemacht; der Median (0,038) hält stand. Ein Fenster weit über den übrigen
wird zusätzlich gemeldet, statt eine halb belegte Leer-Aufnahme
stillschweigend als Maßstab zu verwenden.

`GET /api/calibrations/{id}/presence-rate` macht die Auswertung im
Produkt verfügbar, die zuvor von Hand lief.

Geprüft: 220 Tests, darunter die Websocket-Anmeldung gegen einen echten
Server (auth_required → auth → auth_ok → subscribe_trigger, samt
Wiederverbindung nach Abbruch) und der Detektor gegen die beiden echten
Aufnahmen. Sechs von sieben Fenstern werden richtig eingeordnet; die
beiden Ausreißer sind vermutlich der Detektor, der recht hat.

**Nicht behauptet:** ein Raum, ein Gerät, eine Sitzung. Die 0,931 stehen
auf zwölf Fenstern gegen sechs.

## 0.12.13

Die verbesserte Fehlermeldung aus 0.12.12 hat geliefert, wofür sie gebaut
wurde: **HTTP 403**. Kein Netzwerkproblem, sondern eine Designentscheidung
von ESPectre.

Der Direct-HTTP-Dienst wird mit `for_first_party_portals()` konfiguriert
und akzeptiert nur `https://espectre.dev` und zwei Geschwister-Domains;
ohne `Origin`-Header antwortet er „403 Origin required". Die vorgesehene
Ausnahme `CONFIG_ESPECTRE_DIRECT_DEV_ORIGINS_ENABLED` ist in keiner
Kconfig deklariert, die der ESPHome-Build erreicht — über
`sdkconfig_options` gesetzt würde sie als unbekanntes Symbol verworfen.
Für ein per ESPHome gebautes Gerät gibt es also keinen vorgesehenen Weg,
einen anderen Client zuzulassen.

- **Das Calibration Lab liest jetzt aus Home Assistant.** Dieselben drei
  Werte — `movement_score`, `motion_detected`, `threshold` — liegen dort
  ohnehin. Das kostet einen Umweg und etwas Auflösung, verlangt vom Gerät
  aber nichts, was es Home Assistant nicht schon gibt.
- **`direct_api` ist keine Voraussetzung mehr.** Es wurde verlangt und
  hat Leute von einer Funktion ausgesperrt, die es nicht mehr benutzt.
  Verlangt werden jetzt erkannte Entities, und die Fehlermeldung nennt
  den Knopf, der sie beschafft.
- Aufgezeichnet werden nur **neue** Messwerte. Home Assistant stempelt
  jeden Zustand mit `last_updated`; eine Abfrage mit unverändertem Wert
  ist eine Wiederholung. Sie mitzuzählen würde identische Zahlen aufhäufen
  und die Verteilung verzerren, aus der die Empfehlung entsteht.
- Nach einem Neustart des Add-ons — meist ein Update — nehmen laufende
  Aufzeichnungen ihre Abtastung wieder auf, statt mit leuchtender
  Live-Anzeige und nichts dahinter dazustehen.

Ein Fehler dabei ist erwähnenswert, weil ihn nur der Browser fand: der
Sampler startete mit `asyncio.create_task` in `create_calibration`. Diese
Route ist ein einfaches `def`, FastAPI führt sie im Worker-Thread ohne
Event-Loop aus, und der Aufruf scheiterte — **nachdem** die Sitzung
angelegt war. Ergebnis: eine Aufzeichnung, die live aussah und nichts
sammelte, also genau das, was diese Funktion verhindern soll. Die
Unit-Tests liefen in `asyncio.run` und sahen es nicht.
`tests/test_calibration_routes.py` geht jetzt durch die App.

Geprüft: 195 Tests, und im Browser eine vollständige Aufzeichnung von der
Geräteauswahl bis zur CSV mit Zeilen darin.

## 0.12.12

Die Warnung aus 0.12.11 sagte „Kein ESPectre-Telemetrie-Endpunkt
erreichbar" — und damit zu wenig. Der Collector hat den konkreten Fehler
verworfen und alle Ursachen auf denselben Satz abgebildet.

- **Der Grund steht jetzt in der Meldung.** Ein nicht auflösbarer Name,
  eine abgewiesene Verbindung, ein 404 auf allen Pfaden und ein Timeout
  sind vier verschiedene Dinge mit vier verschiedenen Antworten. Der
  häufigste ist der erste: ohne eingetragene Adresse benutzt Echolot
  `<gerätename>.local`, und mDNS kommt oft nicht bis in den
  Add-on-Container, obwohl dasselbe Gerät in Home Assistant einwandfrei
  läuft.
- Antwortet der Port mit HTTP-Status, gewinnt das über jeden
  Verbindungsfehler: es beweist, dass etwas lauscht, und verschiebt die
  Diagnose von „Netz" zu „Version".
- **Klassifiziert wird über den Ausnahmetyp, nicht über den Fehlertext.**
  httpx verbirgt die Ursache hinter „All connection attempts failed";
  darunter liegt ein `ConnectionRefusedError` oder ein `socket.gaierror`.
  Ein erster Anlauf hat auf den Text geprüft und lag bei jedem einzelnen
  Fall falsch — aufgefallen, weil der Test gegen einen echten
  geschlossenen Port lief statt gegen eine erfundene Meldung.

Geprüft: 178 Tests. Die Klassifizierung gegen die Ausnahmeketten, die
httpx tatsächlich baut, und die beiden wichtigsten Fälle gegen echte
Sockets — ein Server, der auf allen Pfaden 404 liefert, und ein
geschlossener Port.

## 0.12.11

Drei Aufzeichnungen im Calibration Lab kamen als CSV mit Kopfzeile und
**null Zeilen** zurück. Der Grund war kein Messproblem: der
Telemetrie-Adapter war gegen eine Vermutung geschrieben, nicht gegen die
Firmware.

- **Der Endpunkt stimmte nicht.** ESPectre bedient
  `/espectre/v1/events` (`runtime/direct_http_protocol.h`). Keiner der
  vier Pfade, mit denen dieses Modul ausgeliefert wurde — `/events`,
  `/api/events`, `/stream`, `/api/v1/events` — traf das. Der Collector
  verband sich, kassierte auf jedem Pfad ein 404 und zeichnete nichts
  auf. Das allein erklärt die leeren Dateien.
- **Der Bewegungszustand wurde nicht gelesen.** Er steht unter `state`
  mit den Werten `"motion"` und `"idle"`, nicht unter `motion` als
  Boolean.
- **Die Schwelle stand nie in derselben Nachricht wie ein Messwert.**
  ESPectre teilt sie auf: `motion` trägt `score`, `sensing` trägt
  `threshold`. Der Hub merkt sie sich jetzt je Gerät und schreibt sie an
  die folgenden Messpunkte. Ein Ereignis, das nur die Schwelle meldet,
  wird selbst nicht als Messpunkt geführt — es ist ein
  Konfigurations-Echo, keine Messung, und stünde sonst als leere Zeile in
  jeder CSV.
- **`timestamp_ms` zählt ab Gerätestart**, nicht ab 1970, und wurde gar
  nicht gelesen. Wörtlich genommen läge jeder Messpunkt vor denen aller
  anderen Geräte.

- **Eine leere Aufzeichnung sagt das jetzt.** Sie war von einer
  funktionierenden nicht zu unterscheiden: der Zähler blieb bei 0, sonst
  nichts. Nach drei Abfragen ohne Sample nennt der Kalibrierungs-Tab den
  Grund, den der Hub ohnehin protokolliert („Kein
  ESPectre-Telemetrie-Endpunkt erreichbar"), sonst den Verweis auf die
  Erreichbarkeitsprüfung.

Geprüft: 165 Tests. Neu ist `tests/test_calibration_capture.py`, das
einen SSE-Server mit ESPectres echten Frames aufsetzt und die Kette bis
zur CSV durchmisst — inklusive eines Tests, der den falschen Pfad
absichtlich benutzt und damit exakt die Datei erzeugt, die den Anlass
gab. Die Warnung in echtem Chromium geprüft, in beide Richtungen.

## 0.12.10

Aus einem erfolgreichen Build-Log gelesen — dem ersten vollständigen
ESP-IDF-Compile, den dieses Projekt zu sehen bekommen hat (48 s, Flash
70,2 %, RAM 17,5 %).

- **Das Socket-Budget deckt jetzt ESPectres Direct-Server mit ab.** Im Log
  steht ESPHomes Rechnung offen da: `CONFIG_LWIP_MAX_SOCKETS to 17
  (TCP=11 [api=3, captive_portal=3, web_server=5], UDP=3, TCP_LISTEN=3)`.
  ESPectre kommt darin nicht vor — als externe Komponente meldet es keinen
  Bedarf an —, fordert aus demselben Pool aber bis zu sieben weitere
  Sockets für seinen HTTP/SSE-Server auf Port 62587. Ein Pool, zwei
  Rechnungen. Beim Bauen fällt das nicht auf; unter Last stirbt zuerst der
  SSE-Strom, von dem Calibration Lab und Live-Dashboard leben, und das
  sähe aus wie eine Aufzeichnung, die einfach aufhört. Die Firmware setzt
  den Wert jetzt selbst auf 24, sobald `direct_api` an ist. Ohne
  `direct_api` bleibt ESPHomes Rechnung unberührt.

Zur API-Verschlüsselung, die seit 0.12.3 abgeschaltet ist: der Grund
besteht upstream unverändert. `src/cpp/core` ist weiterhin öffentlicher
`INCLUDE_DIRS`-Eintrag und enthält weiterhin `utils.h`, das libsodiums
C-Dateien statt ihres eigenen finden. Neu ist aber, dass sich das jetzt
in einer knappen Minute überprüfen statt herleiten lässt — der Build
läuft.

Geprüft: 153 Tests. Alle acht Firmware-Varianten rendern gültiges YAML mit
der Option, und ohne `direct_api` erscheint sie nicht.

## 0.12.9

Vor dem Gehtest die Frage geklärt, ob die Geräte überhaupt Direkt-Telemetrie
liefern — und dabei gefunden, dass das Add-on die Antwort seit jeher misst
und wegwirft.

- **Die Erreichbarkeitsprüfung sagt jetzt, was Port 62587 geantwortet hat.**
  Sie hat ihn von Anfang an mitgeprüft, aber keine der vier Meldungen
  erwähnte ihn; das Ergebnis stand nur unbenutzt in der JSON-Antwort. Das
  ist genau die Frage, die vor dem Calibration Lab zu klären ist: beide
  neuen Ansichten holen ihre Samples direkt vom Gerät, nicht über Home
  Assistant, und ein Gerät kann in Home Assistant tadellos laufen und für
  sie trotzdem stumm sein.
- Der Port bekommt eine **eigene Zeile**, nicht ein verändertes Urteil. Über
  das Urteil entscheidet weiter Port 6053 — das ist der, den Home Assistant
  braucht. Die beiden Fragen zu vermischen hätte beide unschärfer gemacht.
- Antwortet er nicht, nennt die Meldung die Folge (Calibration Lab und
  Live-Dashboard ohne Daten) und den Grund (Firmware ohne `direct_api`,
  Voreinstellung ist an, Neubau genügt).

Geprüft: 15 Reachability-Tests, darunter der Fall, dass ein Gerät *nur* auf
62587 antwortet und trotzdem nicht „ok" heißen darf; die Karte in echtem
Chromium mit beiden Fällen.

## 0.12.8

Eine Korrektur. In 0.12.6 stand, ein Gerät habe seine drei Sensing-Entities
nicht gemeldet und laufe deshalb auf älterer Firmware. Das war falsch, und
der Fehler lag in der Untersuchung, nicht im Gerät: gesucht wurde nach dem
Entity-ID-Präfix `zuhause_test1`, und genau die drei fehlenden lagen unter
`test1` — `sensor.test1_movement_score`, `binary_sensor.test1_motion_detected`,
`number.test1_threshold`. Das Gerät hat 34 Entities, misst, und war nie
defekt. Ein Neuflash wäre die falsche Konsequenz gewesen.

- Der Befund `core_entities_missing` bleibt: er ist richtig gebaut, hatte
  nur nie den Anlass, den ihm 0.12.6 zuschrieb. Er greift weiterhin, wenn
  ein Gerät die Entities wirklich nicht hat.
- Was tatsächlich beobachtet wurde, ist interessanter und steht jetzt in
  DOCS.md: **ein Gerät antwortet unter zwei Entity-ID-Präfixen
  gleichzeitig.** Home Assistant behält beim Umbenennen den alten Slug auf
  bestehenden Entities und vergibt den neuen nur an später angelegte. Kein
  Präfix-Raten löst das auf.
- Dass Echolot darauf nicht hereinfällt, ist kein Zufall, sondern die
  Begründung für den `friendly_name`-Abgleich im Resolver — bisher aus der
  Dokumentation von Home Assistant hergeleitet, jetzt an echter Hardware
  belegt. `tests/test_entity_resolver.py` enthält den Fall mit den real
  beobachteten IDs.
- DOCS.md sagt beim Befund jetzt ausdrücklich: erst „Entities in Home
  Assistant suchen“, dann Neuflash — nicht umgekehrt.

## 0.12.7

Confidence fusion, lifted out of a pull request that could no longer be
merged. Its branch was cut before six other merges, and the same
telemetry and Calibration Lab work had meanwhile reached main from a
different branch — so seven files existed on both sides with no common
ancestor. Nothing about the fusion itself was in conflict; only its base.

- **`GET /api/fusion/zones`** combines each zone's devices into an
  explainable presence confidence: a probability per device from its
  calibration profile (falling back to the device threshold, then to the
  motion boolean), weighted by calibration quality and sample age. Samples
  lose weight after five seconds and expire after twenty, so a silent
  device makes the result *unavailable* rather than voting the room empty.
  Disagreement is reported as **uncertain** instead of being resolved by
  OR logic.
- The dashboard shows it as **its own line** on the zone tile. The dot and
  the state text stay with the zone state machine, which is what feeds
  Home Assistant and the MQTT export. In the original pull request both
  wrote the same elements on separate timers — five seconds against two —
  which would have flipped the tile between two verdicts and wiped the
  per-device tooltips. Seeing both at once is the useful part.
- Left behind deliberately: a middleware that injected two script tags
  into the page. It existed because that branch's `index.html` predated
  them; on main those tags are already there, so it would only have loaded
  both scripts twice. Its `style.css` was likewise the older file — taking
  it would have deleted the firmware download link, the flash-step display
  and the diagnosis styling.
- New: `tests/test_fusion_routes.py`. The fusion functions were tested,
  the routes that reach the telemetry hub and the calibration profiles
  were not.

Verified: 144 tests, and the tile driven in real Chromium with the two
verdicts deliberately opposed — after both timers had fired, each still
said its own thing.

## 0.12.6

Die erste Sitzung mit laufender Hardware — und drei Wege, auf denen ein
Gerät online, gesund und nutzlos sein kann, von denen Echolot bis hierhin
keinen gemeldet hätte.

- **Diagnose stellen** auf der Gerätekarte. Prüft, ob das Gerät wirklich
  misst, statt nur ob es erreichbar ist, und liefert zu jedem Befund den
  Knopf, der ihn behebt.
- **Die Schwelle passt nicht immer zum Erkennungsprofil.** ESPectre hat
  pro Profil eine eigene Vorgabe, übernimmt sie aber nur, wenn das Profil
  zur Laufzeit umgestellt wurde. Ein im YAML gesetztes Profil — jedes
  Gerät, das Echolot baut — behält den Schema-Default, und der ist fest
  auf den Lightweight-Wert verdrahtet. Ein `high_accuracy`-Gerät lief
  damit gegen eine um 32 % zu hohe Latte. Der Befund steht auch auf der
  Übersicht: er kostet dort keine zusätzliche Abfrage und betrifft jedes
  gebaute Gerät.
- **Fehlende Sensing-Entities werden erkannt.** Ein Gerät, das jede
  Diagnose- und Bedien-Entity meldet, aber keine der drei, um die es geht,
  liefe auf älterer Firmware — und Uptime, Temperatur und WLAN sähen dabei
  tadellos aus. (Der Anlassfall war keiner: siehe 0.12.8.)
- **Die CSI-Diagnosen werden abgerufen.** Sie veröffentlichen nur auf
  Anforderung und standen deshalb auf beiden Geräten dauerhaft auf
  `unknown`. Niemand konnte sehen, ob verwertbare Pakete ankommen. Liegt
  die Rate unter einem Viertel des eingestellten Ziels, ist das jetzt ein
  Blocker.
- Ein Hinweis, wenn der höchste Bewegungswert weit unter der Schwelle
  bleibt — als Beobachtung, nicht als Urteil. Eine leere Wohnung sieht
  genauso aus, und nur ein Gehtest trennt die beiden Fälle.
- Nebenbei behoben: die Diagnose speichert die Entity-IDs, die sie ohnehin
  auflöst. Ohne das nannte ein Befund eine Entity und bot dann einen Knopf
  an, der mangels derselben Entity fehlschlug — ausgerechnet bei den
  Geräten, für die die Diagnose da ist.

Geprüft gegen die tatsächlich gemessenen Werte zweier Geräte, die als
Regressionsfälle in den Tests stehen; die Oberfläche in echtem Chromium
gegen einen Server mit denselben Daten.

## 0.12.5

Prompted by a chip that would not take a new firmware and would not let
go of the old one — and by the reasonable but wrong conclusion that the
second fact explains the first.

- **The flash step is named while it runs.** ESP Web Tools shows the same
  sentence, "Preparing installation", for the serial handshake with the
  chip and for downloading the image, and puts a timeout on neither. The
  device card now shows which of the two is running, and after about
  twenty seconds without progress adds the advice belonging to that
  step — download mode for the one, the download link for the other.
  They need opposite responses, so telling them apart is the whole point.
- The percentage of a write resets that clock, so a slow flash is never
  called stalled; a frozen one is.
- DOCS.md says plainly that an existing firmware never blocks a new one.
  Writing goes through the ROM bootloader, which no firmware can
  overwrite, and erasing goes through that same bootloader — so an erase
  that will not start and an install that will not start have one cause,
  not two. The manual download-mode procedure (BOOT, RESET, release, and
  then *pick the port again*, because a native-USB chip re-enumerates as
  a different device) and the `esptool` erase and write commands are
  spelled out.

Verified in Chromium against a synthetic dialog: the right card, the
right step, the hint only once a step is genuinely stuck, the line
cleared when the dialog closes, and nothing rendered at all if a future
ESP Web Tools bundle renames the property this reads.

## 0.12.4

Prompted by a flash that sat on "Preparing installation" for ten minutes.
Measured first: serving a 3 MB image from this add-on takes ten
milliseconds, and the manifest is correct — so that message is not about
the download. Reading ESP Web Tools' own code, it covers *two* steps with
no timeout on either: the serial handshake with the chip
(`initializing`) and the firmware download (`preparing`). Nothing in the
dialog distinguishes them.

- **Firmware herunterladen** on the device card, with the image's size.
  The built-in flasher is no longer the only way in: the same `.bin` can
  be installed with `web.esphome.io` or `esptool` at offset 0. When the
  in-page flash stalls, that is a way forward rather than a dead end.
- The size also makes a large image distinguishable from a stalled one,
  which the flasher itself never says.
- `HEAD` on the firmware endpoint answered 405. Flashers other than the
  built-in one ask for the size before fetching, and a 405 reads to them
  as a broken link. It now answers with the length.
- DOCS.md gains the five-second test for which of the two steps is stuck
  (look for `firmware.bin` in the browser's Network tab) and what to do
  in each case.
- The buffered, `no-store` firmware response from 0.12.3 stays exactly as
  it is; the `HEAD` handler was folded into it rather than replacing it.
  The two changes address different halves of the same symptom, and only
  a flash on real hardware can say which half was actually stuck.

## 0.12.3

**Builds failed again**, this time deep into the compile:

```
external_components/…/src/cpp/core/utils.h:14:10:
  fatal error: cstdint: No such file or directory
   14 | #include <cstdint>
```

A *C* file — libsodium's `aead_chacha20poly1305.c` — was including
ESPectre's *C++* header. The chain, traced through the sources rather
than guessed:

1. `api: encryption:` makes ESPHome add `esphome/noise-c`, which compiles
   libsodium (`components/api/__init__.py`, `cg.add_library`).
2. libsodium's C sources include a bare `"utils.h"`.
3. ESPectre registers `src/cpp/core` as a **public** ESP-IDF
   `INCLUDE_DIRS` entry (`src/cpp/CMakeLists.txt`), and that directory
   contains `utils.h` — a C++ header.
4. The C compiler finds ESPectre's before libsodium's own, and `<cstdint>`
   does not exist in C.

This is a bug in ESPectre's build definition: a component should not
publish a generically named header on the global include path. Nothing in
the add-on can reorder those include paths from YAML.

- **API encryption is now a per-device option, default off.** Not a
  judgement that it is optional — with it on, the firmware does not
  compile at all. The key is still generated and stored, so it can be
  switched back on without generating anything new the moment upstream
  fixes the include. The device card and the firmware options say why.
- The OTA password is unaffected and stays on.
- `tools/validate_firmware.py` now covers eight configurations rather than
  seven: the encrypted variant is exercised too, since that is the branch
  that broke.

Honest limit: this environment cannot complete a full ESP-IDF compile (the
toolchain download is blocked), so the *fix* is verified by construction
and by config validation, not by a completed build. That the encrypted
variant fails to compile is established from the upstream sources quoted
above, not from a run here.

This is a *second*, unrelated build blocker: 0.12.2 fixed ESPectre's
CMake failing on a tagless checkout, which stopped the build before it
ever reached a compiler. This one stops it during compilation.

### Auslieferung der Firmware

- Firmware downloads are now sent as finite, non-cached responses through
  Home Assistant Ingress. This prevents ESP Web Tools from waiting forever at
  “Preparing installation”; a direct download remains available as a fallback.
- The dashboard no longer depends on the unrelated board-list request. It now
  validates API responses and shows a useful error with a retry button instead
  of becoming an empty panel when an endpoint fails or returns malformed data.

Both change sets carry the number 0.12.3: they were released one after the
other without a bump in between, and the merge that brought them together
had dropped the text of both.

## 0.12.2

- **Firmware builds work with ESPHome's shallow ESPectre checkout.** The
  upstream CMake build obtains its SDK version from numeric Git tags, but
  ESPHome does not fetch those tags for an external component. Echolot now
  supplies upstream's supported `ESPECTRE_GIT_VERSION` override as an unknown
  development version (`0.0.0`) instead of letting `git describe` abort the
  build. An explicitly configured version still takes precedence.

## 0.12.1

Hardening pass over the five core modules, from an external code review.
Findings were verified against the code before acting on them; two did
not survive that check and are noted at the end.

### Sicherheit

- **`GET /api/devices` no longer returns any device's API key or OTA
  password.** Drawing the device list handed out every credential in the
  installation; one leaked response was the whole fleet rather than one
  device. There is now a `GET /api/devices/{id}/credentials` endpoint,
  reached one device at a time when someone opens that section.
- **The generated ESPHome YAML is deleted after the build.** It carries
  the Wi-Fi password, API key and OTA password in clear text, is
  regenerated on every build and OTA push, and was previously kept
  forever under `/data/devices/<id>/`. While it exists it is now
  owner-only (0600). This does not make the add-on secret-free —
  `devices.json` holds the same values, because the add-on has to be able
  to rebuild a device and show its key — but the secrets no longer have a
  second home that also lands in add-on backups.

### Zuverlässigkeit

- **At most one ESPHome compile runs at a time.** The old guard stopped
  one device building twice but said nothing about four devices building
  at once — four C++ toolchain runs on the hardware Home Assistant
  usually lives on. Later builds queue instead of competing. Measured: 4
  started, 1 ran.
- **A stale firmware image can no longer be reported as a build's
  output.** `_find_factory_bin` took the newest matching file; a compile
  that failed after an earlier success would hand back the *old* image
  and report success, and the user would flash firmware predating their
  change. It now only accepts images newer than the build's start.
- **MQTT re-announces its zones after a reconnect.** Discovery is
  retained, so it normally survives — but a broker restarted without
  persistence has forgotten every zone while the bridge still believed it
  announced them, and the entities would have stayed missing until the
  add-on restarted.
- **Rejected MQTT publishes are noticed.** paho's return value was
  discarded, so a dropped discovery message was recorded as sent and
  never retried. A rejected announcement now stays un-announced and is
  retried next cycle.

### Leistung

- **One pooled HTTP client** instead of a fresh `AsyncClient` per request.
- **Reading state no longer calls `/api/states`.** 0.11.0 replaced fifteen
  targeted reads with one snapshot call, which looked like a clear win —
  but that one response carries *every* entity in the installation, on a
  ten-second timer, forever. On a large install that is megabytes to read
  a handful of numbers. Targeted reads are back, now issued concurrently,
  so a five-device zone costs one round-trip of latency rather than
  fifteen. `/api/states` is kept for entity discovery, where its size
  buys something.

### Typen

- `zone_logic.evaluate` returns a frozen `ZoneEvaluation` rather than a
  dict of four stringly-typed keys; `as_dict()` keeps the published JSON
  identical.
- It also clamps a negative hold time itself rather than trusting its
  caller — a negative hold would expire in the past and quietly turn a
  configured hold into none at all.

### Zwei Befunde, die nicht zutrafen

- *"`DeviceUpdate` mixes runtime and build configuration"* — it does not.
  It contains `friendly_name`, the four entity ids and `address`, all of
  which apply without a rebuild. Board, Wi-Fi and detection settings are
  not in it.
- *"`get_history` should validate `minutes`"* — already clamped to
  1–1440 at the route.

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
