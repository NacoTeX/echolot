# Echolot

Raumpräsenz für Home Assistant mit dem **Hi-Link HLK-LD2460**: einem
24-GHz-Radar, das bis zu fünf Ziele mit Position meldet. Echolot baut die
Firmware für einen ESP32 mit diesem Modul, flasht sie aus dem Browser, legt
den Sensor auf einen Grundriss, lässt dich Zonen darauf zeichnen und macht
Räume und Zonen zu Entitäten in Home Assistant.

Bis 0.14 konnte Echolot außerdem WLAN-CSI-Sensoren über ESPectre bauen.
Das ist mit 1.0 entfernt; was mit solchen Geräten passiert, steht unter
[„Geräte aus früheren Versionen“](#geräte-aus-früheren-versionen).

## Installation

1. Dieses Repository im Add-on-Store eintragen (**Einstellungen → Add-ons →
   Add-on-Store → ⋮ → Repositories**): `https://github.com/NacoTeX/echolot`
2. **Echolot** installieren und starten. Die Oberfläche erscheint in der
   Seitenleiste.

64-Bit-Home-Assistant (`aarch64` oder `amd64`). Der erste Firmware-Build
lädt etwa 2 GB ESP-IDF und Cross-Toolchain nach `/data/platformio`; dort
bleiben sie für alle weiteren Builds.

| Option | Bedeutung |
| --- | --- |
| `log_level` | Ausführlichkeit des Add-on-Logs. |
| `mqtt_export` | Räume und Zonen per MQTT-Discovery an Home Assistant geben (Standard `true`). Braucht einen Broker, etwa das Mosquitto-Add-on; ohne läuft alles andere normal. |

## In drei Schritten

1. **Sensor anlegen** — unter *Sensoren*: Name, Board, WLAN. Für den
   Waveshare ESP32-C5-Zero trägt ein Knopf die Belegung ein.
2. **Firmware aufspielen** — *Firmware bauen*, dann *Über USB flashen*
   (Chrome oder Edge am Computer). Danach gehen Updates *über WLAN*, aus
   jedem Browser, auch vom iPad.
3. **Raum einrichten** — *Raum hinzufügen*, Maße eintragen, Sensor
   zuordnen, im Editor Sensor platzieren und Zonen zeichnen.

## Verkabelung

Das Modul hat zwei UARTs; Ziele und Befehle laufen über UART2 (Pins 7/8).

| ESP | LD2460 |
| --- | --- |
| 5 V | Pin 1 · 5V |
| GND | Pin 2 oder 6 · GND |
| TX (Feld „TX-Pin“) | Pin 8 · Rx2 |
| RX (Feld „RX-Pin“) | Pin 7 · Tx2 |

Pin 5 (VDD33) bleibt frei. Vorgeschlagen werden je Chip Pins, die bei
ESPHomes Standard-Logger frei sind. **Mit echter Verkabelung geprüft ist nur
der ESP32-C5** (GPIO11/12, Waveshare ESP32-C5-Zero); dort hält GPIO26 auf
LOW die Antenne auf der Platine aktiv.

## Die Oberfläche

Gebaut wie die Raum-Apps von mmWave-Sensoren: der Grundriss ist die Seite.

**Übersicht.** Eine Kachel je Raum mit Mini-Karte, Live-Punkten, der Zahl
erkannter Personen und den belegten Zonen. Die Seitenleiste zeigt dieselbe
Zahl je Raum; auf dem Handy wird sie zur Leiste unten.

**Raum.** Die Karte in Metern, Raster 0,5 m. Darauf:

- **Punkte** — gezählte Ziele, mit einer kurzen Spur der letzten Meldungen.
  Das Modul vergibt keine Personen-IDs; Echolot folgt jedem Ziel von
  Meldung zu Meldung selbst (siehe „Wie gezählt wird“).
- **Gestrichelte Punkte** — neue Ziele, noch nicht bestätigt. Sie zählen,
  sobald das Radar sie die Bestätigungszeit lang gemeldet hat.
- **Hohle Punkte** — Ziele, die nicht zählen: hinter der Wand, in einer
  Ausschlusszone oder (orange umrandet) an einer gelernten Störquelle.
- **Orange Kreise mit Kreuz** — gelernte Störquellen.
- **Zonen** — leuchten auf, sobald jemand darin ist, mit der Zahl in der
  Ecke. Rechts stehen sie mit Zustand und Abwesenheits-Countdown.
- **Sensor und Sichtbereich** — der gestrichelte Fächer ist eine
  Planungshilfe aus Reichweite und Öffnungswinkel, keine Messung. Wie weit
  das Modul wirklich sieht, zeigen die Punkte.

Hat ein Raum keine aktuelle Messung, sagt die Karte warum — kein Sensor
zugeordnet, keine Verbindung, Radar antwortet nicht — statt einen leeren
Raum zu zeigen.

**Raum einrichten.** Werkzeugleiste über der Karte, Inspektor rechts:

| Werkzeug | Taste | |
| --- | --- | --- |
| Auswahl | V | Antippen wählt, Ziehen verschiebt. Bei Zonen: Eckpunkte ziehen verformt, die kleinen Punkte zwischen den Ecken ziehen fügt eine Ecke hinzu, Doppelklick auf eine Ecke entfernt sie. Möbel: Griff oben dreht, Ecke unten rechts ändert die Größe. Sensor: der Griff vor ihm dreht ihn. |
| Rechteck | R | Zone aufziehen. |
| Freiform | P | Ecken antippen; auf die erste tippen oder Enter schließt, Esc bricht ab. |
| Möbel | | Sofa, Bett, Tisch, Schrank, Tür, Fenster … als Orientierung. Unter *Als Zone* wird ein Möbelstück zur Erkennungs- oder Ausschlusszone, die ihm folgt (siehe unten). |
| Wände | | Der Umriss des Raums, für Nischen, L-Formen, Vorsprünge. Ecken ziehen, über die Punkte dazwischen neue einfügen, Doppelklick entfernt eine. *Neu nachzeichnen* zieht die Wände Ecke für Ecke, etwa über dem Grundrissbild. |
| Einrasten | | 5-cm-Raster, Drehungen in 15°-Schritten (Sensor 5°). |
| Rückgängig / Wiederholen | ⌘Z / ⇧⌘Z | |

**Möbel als Zone.** Wähle ein Möbelstück und stell *Als Zone* auf
*Erkennung* — für Sofa, Bett, Schreibtisch, Esstisch — oder auf
*Ausschluss* — für Pflanze, Ventilator, Aquarium. Die Zone trägt den Namen
des Möbelstücks und folgt ihm, wenn du es verschiebst, drehst, in der Größe
änderst oder umbenennst; löschst du es, geht die Zone mit, auch aus Home
Assistant. Der **Rand** (Standard 20 cm) legt fest, wie weit um das Möbel
herum noch mitgezählt wird: Das Radar verortet jemanden, der sitzt oder
liegt, nicht genau auf den Polstern. An einer Wand endet die Zone an der
Wand. Sitz- und Liegemöbel bekommen 30 s Abwesenheitsverzögerung statt
10 s. Ein Tipp auf die Zone wählt das Möbelstück; einzeln verformen lässt
sich eine Möbelzone nicht — dafür eine Zone von Hand zeichnen.

**Wände.** Ein neuer Raum ist ein Rechteck aus Breite × Tiefe. Folgt der
Raum nicht einem Rechteck — eine L-förmige Wohnküche, ein Erker, eine
Nische für den Schrank —, schalte in den Raumeinstellungen *Form* auf
*Freiform* oder tippe *Wände*. Breite × Tiefe bleiben dann der Plan, auf
dem die Wände liegen; was außerhalb der Wände liegt, ist schraffiert und
zählt nicht. Die Fläche innerhalb steht über der Karte. Kreuzen sich zwei
Wände, werden sie rot und der Raum lässt sich so nicht speichern; *Zurück
zum Rechteck* verwirft den Umriss (rückgängig machbar).

Nichts ausgewählt zeigt die Raumeinstellungen: Name, Art, Maße, Form, Sensor,
Abwesenheitsverzögerung, Randtoleranz, Grundrissbild (PNG, JPEG oder WebP
bis 4 MB, auf Breite × Tiefe gestreckt) und Deckkraft. Die Live-Punkte sind
auch im Editor zu sehen — zum Einzeichnen einer Zone einfach in die Ecke
stellen.

Gespeichert wird mit *Speichern*. Hat jemand den Raum inzwischen anderswo
gespeichert — ein zweiter Tab, das iPad —, wird das zweite Speichern
abgelehnt, und du entscheidest, ob neu geladen wird. Ein Raum lässt sich
nicht so verkleinern, dass Zonen oder Möbel außerhalb lägen.

**Links und rechts.** Laut der Einbauanleitung des UltimateSensor V2, die
sich auf Hi-Links Handbuch beruft, zeigt bei Wandmontage das Antennenende
nach vorn in den Raum (+Y), und +X liegt rechts, vom Radar aus gesehen.
Prüfen lässt sich das mit zwei Gängen: geradeaus vom Sensor weg wächst Y,
von links nach rechts wächst X. Läuft der Punkt quer in die Gegenrichtung,
beim Sensor „Links und rechts tauschen“ einschalten. Ändert sich X beim
Seitwärtsgehen kaum, ist das Modul um 90° gedreht eingebaut — dann
ausschalten und das Modul richtig drehen.

## Kalibrieren

*Raum → Kalibrieren*, drei Reiter. Alles geschieht am lebenden Raum; die
Kalibrierung ändert sich erst, wenn du *Übernehmen* tippst. Gemessene
Standpunkte hebt Echolot vorher schon auf, damit ein Neuladen oder der
Wechsel vom iPad zum Laptop nichts verliert.

**Störquellen lernen.** Das Radar meldet an manchen Stellen Ziele, wo
niemand ist: Heizkörper, Spiegel, Metallgestelle, ein Ventilator. Wähle,
wie lange du zum Hinausgehen brauchst und wie lange Echolot zuhören soll,
tippe *Aufnahme starten* und verlass den Raum. Währenddessen erscheint
jede Meldung als grauer Punkt auf der Karte. Danach zeigt Echolot die
Stellen, an denen das Radar immer wieder etwas gemeldet hat (in mindestens
3 % der Meldungen), mit Anteil und Radius. *Übernehmen* ersetzt die
bisherigen. Was sich während der Aufnahme bewegt hat — jemand kam doch
herein —, wird nicht gelernt, und der Hinweis sagt es.

Was eine Störquelle bewirkt: Ein Ziel, das dort *entsteht*, wird nie
bestätigt. Wer aus dem Raum hineingeht, zählt weiter; wer herausgeht,
nimmt sein Ziel mit, statt es beim Heizkörper zurückzulassen. Bleibt ein
bestätigtes Ziel länger als **20 s** auf einer Störquelle, zählt es nicht
mehr. Liegt eine Stelle dort, wo jemand länger sitzt — ein Sofa mit
Metallgestell —, nimm sie einzeln heraus.

Die Stellen stehen in Koordinaten des Sensors, nicht des Raums. Sie
bleiben also richtig, wenn der Sensor auf dem Plan verschoben oder gedreht
wird, gelten aber nur für genau diesen Sensor in genau dieser Montage.
Hängt er physisch anders, verwirft *Sensor neu montiert* sie (siehe unten).

Die Aufnahme sagt nebenbei, ob das Modul im leeren Raum **leere Berichte**
schickt oder **verstummt**. Verstummt es, schlägt sie vor, beim Sensor
„Stille = leerer Raum“ einzuschalten.

**Sensor ausrichten.** Hier lernt Echolot, wo der Sensor wirklich hängt
und wie sein Modul misst. Das Radar sieht dich nie genau dort, wo du
stehst: Die Platzierung auf dem Plan ist nach Augenmaß gezeichnet, und das
Modul selbst verzerrt — Fehler, die mit der Entfernung wachsen. Beides
rechnet die Ausrichtung aus Standpunkten heraus.

1. **Montage:** Das LD2460 kennt selbst, wie es hängt — an der Wand
   („side“) oder an der Decke („top“) —, und speichert Höhe über dem Boden
   und Neigung nach unten. Es rechnet sie in die Positionen ein, die es
   meldet. Die Kalibrierseite zeigt, was das Modul meldet, und schreibt
   Änderungen hinein: *Ins Modul schreiben* schickt die Werte, wartet, bis
   das Modul sie zurückliest, und sagt, ob es sie übernommen hat. Hi-Link
   empfiehlt an der Wand 2,2–2,7 m Höhe und 25–40° Neigung (Beispiel:
   2,6 m, 30°). Weil das Modul danach andere Positionen meldet, verwirft
   eine Änderung Ausrichtung, Sensormodell, Störquellen und Standpunkte —
   auch wenn sie in Home Assistant geändert wurde (Entitäten „Radar Mount
   Mode/Height/Angle“). Dazu, in welcher Haltung gemessen wird (stehend,
   sitzend). Ob die schräge Linie zu dir dann noch eine Rolle spielt, prüft
   die Kalibrierung mit der Höhe des Moduls und nimmt, was die Messung
   zeigt. Mit einer Firmware vor 1.5 steht hier nur ein Feld für die Höhe.
2. **Standpunkte:** *Standpunkte vorschlagen* legt fünf Punkte in den
   Sichtbereich — nah und fern, links und rechts, nicht auf Möbeln — und
   hält zwei weitere als **Kontrollpunkte** zurück. Stell dich auf den
   markierten Punkt, tippe *Messen*; nach 3 s hört Echolot 5 s zu und nimmt
   den Median der gemeldeten Positionen. Danach ist der nächste Punkt dran.
   Eigene Punkte gehen mit einem Tipp auf den Plan — gut sind Stellen, die
   du genau wiederfindest. Jeder Punkt lässt sich neu messen oder
   entfernen. Gespeichert werden Sollpunkt, gemeldete Position, Streuung,
   Anteil der Meldungen und Zeitpunkt; ein Punkt, dessen Messung über einen
   Sensorwechsel oder eine Neumontage lief, wird nicht aufgenommen.
3. **Bericht:** Echolot rechnet mehrere Modelle durch — nur Position und
   Richtung; dazu ein Entfernungsmaßstab; dazu ein Entfernungsversatz oder
   ein Winkelmaßstab; alles zusammen; jeweils mit und ohne Schräge — und
   nimmt das einfachste, das die Messung trägt. Mehr Korrekturen passen auf
   ein paar Punkte immer besser, auch wenn sie nur das Rauschen nachzeichnen;
   deshalb kostet jede zusätzliche Korrektur etwas (Bayes'sches
   Informationskriterium mit einer Rauschgrenze von 6 cm). Der Bericht
   nennt drei Zahlen, bewusst getrennt:
   - **Anpassung:** RMS an den Standpunkten, aus denen gerechnet wurde.
     Sagt wenig — mehr Korrekturen passen immer besser.
   - **Kreuzprüfungs-RMS:** Jeder Standpunkt wird einmal weggelassen, die
     ganze Wahl (Modell, Links/Rechts, Schräge) aus den übrigen neu
     getroffen und der Punkt vorhergesagt. Ehrlich über das Verfahren,
     aber aus derselben Sitzung und denselben Punkten.
   - **Kontrollpunkte-RMS:** Punkte, die an keiner Rechnung teilhaben.
     Nur das ist eine unabhängige Prüfung. Erst mit **zwei**
     Kontrollpunkten heißt das Ergebnis *geprüft* und bekommt eine Stufe
     — *gut* bis 15 cm, *brauchbar* bis 30 cm, sonst *ungenau*. Ohne sie
     steht da *nicht unabhängig geprüft*, auch bei einem Punkt, der
     perfekt passt.

   Die Zahlen sind RMS-Werte dieser Punkte, keine Fehlergrenze und kein
   Konfidenzintervall. Liegen die Standpunkte zu eng, auf einer Linie, in
   derselben Richtung oder gleich weit vom Sensor, sagt der Bericht,
   welche Korrektur sich daraus nicht bestimmen lässt.

Links und rechts entscheidet ab drei Punkten die Passung selbst; ist sie
nicht eindeutig, der eingezeichnete Platz; sonst bleibt die Einstellung,
und der Bericht sagt es. Passt ein Punkt nicht zu den anderen — falsch
markiert, bewegt —, nennt ihn der Bericht: neu messen oder entfernen, er
verzerrt sonst das Ergebnis. Eine Lösung mehr als 50 cm außerhalb des
Raums wird nicht übernommen, knapp hinter der Wand wird der Sensor auf die
Wand gesetzt. Mit einem Punkt wird nur die Richtung korrigiert, mit zwei
Position und Richtung; die Korrekturen des Moduls brauchen mindestens drei,
belastbar fünf.

**Übernehmen** rechnet auf dem Server noch einmal aus den gespeicherten
Standpunkten und speichert Platzierung, Sensormodell, alle Standpunkte und
den Bericht — aber nur, wenn der Raum noch der ist, für den der Vorschlag
berechnet wurde: derselbe Sensor, dieselbe Montage, dieselben Standpunkte,
dieselbe Höhe, dasselbe Rechenverfahren, keine andere Änderung am Raum
dazwischen. Sonst lehnt Echolot ab, nennt, was sich geändert hat, und
rechnet den Vorschlag neu; ein zweiter Tab kann so keinen veralteten
Vorschlag übernehmen. Die Standpunkte bleiben stehen und zeigen, wie gut
die neue Einstellung zu ihnen passt. *Korrekturen zurücksetzen* nimmt die
Korrekturen des Moduls wieder weg.

Ändert sich später etwas, worauf der Bericht beruht — Höhe, Sensormodell,
Raummaße, der Sensor auf dem Plan —, steht die Ausrichtung als
**veraltet** da, mit dem Grund. Ausrichtungen aus 1.3 haben keinen solchen
Nachweis und heißen deshalb nicht „aktuell“.

**Verlauf.** Jede übernommene Ausrichtung bleibt mit Standpunkten und
Bericht im Verlauf, dazu der Stand davor — die letzten sechs. *Vorschau*
zeichnet den Sensor, wie er damals stand, gestrichelt auf den Plan;
*Zurück* stellt ihn so wieder her. Der Eintrag behält dabei seine
Kennung, der jetzige Stand bleibt im Verlauf, Raum, Zonen und
Home-Assistant-Entitäten bleiben, wie sie sind. Ein wiederhergestellter
Bericht wird am Raum von heute gemessen: Wurde der Raum seither größer,
steht er als veraltet da.

**Sensor neu montiert oder gewechselt.** Wird ein anderer Sensor
zugeordnet, gelöscht, oder tippst du *Sensor neu montiert …* (abgenommen
und wieder aufgehängt, anders gedreht), gilt nichts mehr, was mit der
alten Montage gemessen wurde: Störquellen, Ausrichtung, Sensormodell und
Standpunkte werden verworfen, die Zeichnung auf dem Plan und die Höhe
bleiben. Einträge im Verlauf aus der alten Montage lassen sich ansehen,
aber nicht wiederherstellen. Verschiebst du den Sensor dagegen im
Raumeditor, fragt Echolot beim Speichern: *umgehängt* verwirft wie oben,
*nur Zeichnung korrigiert* behält alle Messungen — die gemeldeten
Positionen sind in Sensorkoordinaten gespeichert und passen zur neuen
Zeichnung; die Ausrichtung gilt dann als veraltet.

**Filter.**

- **Bestätigungszeit** (0–5 s, Standard 1 s): Ein neues Ziel zählt erst,
  wenn das Radar es so lange meldet, und zwar in mindestens der Hälfte der
  Meldungen. 0 s zählt jede Meldung sofort.
- **Glättung** (aus / normal / stark): Jede Meldung zieht ein Ziel die
  Hälfte (normal) oder ein Viertel (stark) des Wegs zur neuen Position.
  Ruhigere Punkte an Zonengrenzen, dafür folgt der Punkt einer gehenden
  Person etwas später.

## Wie gezählt wird

Messdefinition 4 (seit 1.4). Was eine **neue Meldung** ist, entscheidet
die Folgenummer jeder Zeile: Dieselbe Nummer mit demselben Inhalt ist die
Wiederholung, die die Firmware jede Sekunde als Lebenszeichen schickt —
sie hält die Verbindung frisch, ist aber keine Messung. Eine Nummer, die
höchstens 2³¹ voraus liegt, ist neu (auch über den Überlauf bei 2³²
hinweg; übersprungene Nummern werden gezählt), eine ältere wird
verworfen. Nach jeder neuen Verbindung beginnt die Zählung neu. Bis zu 64
Meldungen warten in einer Schlange; die Auswertung nimmt alle der Reihe
nach, jede zu ihrer Empfangszeit — die Zeile trägt keine Gerätezeit, die
Empfangszeit ist also die Zeit der Messung plus Übertragung.

Jede neue Meldung geht zuerst durch die **Zielverfolgung**: Ziele, die
länger als 1,5 s nicht gemeldet wurden, sind vergessen, bevor die neue
Meldung zugeordnet wird. Jede gemeldete Position wird dann dem nächsten
bekannten Ziel zugeordnet (bis 0,9 m), sonst entsteht ein neues. Ein Ziel,
das eine Meldung lang fehlt, bleibt so lange gemerkt, zählt in der Zeit
aber nicht. Ein neues Ziel wird bestätigt, sobald das Radar es die
**Bestätigungszeit** lang gemeldet hat, außerhalb gelernter Störquellen.

Dann für jedes Ziel der aktuellen Meldung, in dieser Reihenfolge:

1. Umrechnung vom Sensor in den Raum: erst das Sensormodell aus der
   Ausrichtung — Entfernungsmaßstab und -versatz, Winkelmaßstab,
   Schrägstrecke —, dann Position, Blickrichtung, Spiegelung.
2. Liegt es weiter als die **Randtoleranz** (Standard 30 cm) außerhalb der
   Wände, zählt es nicht. Radar sieht durch Trockenbau. Die Wände sind der
   Umriss des Raums, wenn er einen hat, sonst sein Rechteck; bei einem
   Umriss gilt die Toleranz als Abstand zur nächsten Wand.
3. Liegt es in einer **Ausschlusszone**, zählt es nicht — für Ventilator,
   Vorhang im Luftzug, Aquarium.
4. Ist es noch nicht bestätigt, zählt es nicht.
5. Sonst zählt es für den Raum und für jede **Erkennungszone**, in der es
   liegt. Zonen dürfen sich überlappen.

Die Ziele stehen in Koordinaten des Sensors. Verschieben oder Drehen auf
dem Plan lässt sie bestätigt; ein anderer Sensor, eine andere
Bestätigungszeit oder neue Störquellen beginnen die Bestätigung neu, ebenso
jeder Ausfall.

Definition 3 (1.3) zählte die Lebenszeichen-Wiederholungen als Meldungen
— ein Ziel wurde so schneller bestätigt, als das Radar es tatsächlich
gemeldet hatte —, nahm je Auswertung nur die letzte Meldung, zur Zeit der
Auswertung, und konnte ein Ziel nach Ablauf seines Gedächtnisses wieder
aufnehmen. Definition 2 (1.1–1.2) war Definition 3 ohne Sensormodell;
Definition 1 (Echolot 1.0) waren die Schritte 1–3 und 5 auf jede Meldung
einzeln, mit dem Rechteck als Wänden.

Raum und Zone gelten als belegt, solange ein Ziel darin ist, und danach
noch für ihre **Abwesenheitsverzögerung** (Raum 10 s, Zone einstellbar).
Das ist der ehrliche Regler dafür, dass das Modul still sitzende Menschen
zeitweise verliert.

**Nicht verfügbar ist nicht leer.** Ohne aktuelle Meldung — keine
Verbindung, drei Sekunden keine Zeile, Modul antwortet nicht — sind Raum und
Zonen *nicht verfügbar*, in Echolot wie in Home Assistant. Eine Automation,
die bei „leer“ das Licht ausschaltet, tut das nicht, weil ein Sensor aus dem
WLAN gefallen ist. Nach einem Ausfall beginnt die Abwesenheitsverzögerung
neu, statt über die Lücke hinweg weiterzulaufen.

## In Home Assistant

Zwei Wege, unabhängig voneinander:

**Der Sensor selbst** über die ESPHome-Integration. Home Assistant findet
ihn und fragt beim Hinzufügen nach dem Schlüssel; er steht beim Sensor unter
*Zugangsdaten*. Entitäten: „Targets“ (Anzahl; unbekannt statt 0, solange
keine aktuelle Meldung da ist), „Radar Status“, „Radar Firmware“, mit
Diagnose außerdem WLAN-Signal, Laufzeit, Temperatur, Byte- und
Meldungszähler. „Radar Frame“ ist deaktiviert, damit der Recorder nicht
zehn Zeilen pro Sekunde schreibt.

**Räume und Zonen** über MQTT-Discovery. Je Raum ein Gerät (mit dem Raum als
vorgeschlagenem Bereich) mit

- `binary_sensor` *Anwesenheit* (Geräteklasse `occupancy`)
- `sensor` *Personen*

und je Erkennungszone ein weiteres Paar *\<Zone\>* und *\<Zone\> Personen*.
Ausschlusszonen werden keine Entitäten. Jede Entität trägt als Attribute
die Regeln, nach denen ihr Wert entstand: `definition_version` (jetzt 4),
`confirm_s`, `smoothing`, `interference_spots` und das Sensormodell
(`range_scale`, `range_offset_m`, `azimuth_scale`, `slant`). So lässt sich ein
Verlauf auch nach einer Kalibrierung richtig lesen. Alle hängen an zwei
Verfügbarkeiten: dem Add-on und dem Raum. Gelöschte Zonen und Räume
verschwinden aus Home Assistant; die Löschung steht in einer Warteschlange
auf der Platte, bis der Broker sie bestätigt hat, und übersteht auch einen
Neustart. Von dort exportieren Home Assistants eigene Brücken — HomeKit,
Matter, Google, Alexa — die Entitäten weiter.

## Die Verbindung zu den Sensoren

Echolot hält zu jedem Sensor eine eigene Verbindung über die native
ESPHome-API (Port 6053, verschlüsselt mit dem Schlüssel des Geräts), neben
der von Home Assistant. Adresse ist `<knotenname>.local` oder, falls
eingetragen, die IP beim Sensor unter *Über WLAN*. Bricht die Verbindung ab,
versucht Echolot es nach 2, 5, 10, 20 und dann alle 30 Sekunden erneut.
Warum es nicht klappt, steht beim Sensor in Worten: Schlüssel abgelehnt
(Firmware nicht aus diesem Gerät gebaut), anderes ESPHome-Gerät unter der
Adresse (IP neu vergeben), Name nicht auflösbar, keine Antwort.

## Was die Firmware tut

Ein eigener ESPHome-Baustein, `echolot_ld2460`. Er liest die Zielmeldungen
des Moduls (Kopf `F4 F3 F2 F1`, Funktion `04`, bis zu fünf Ziele in
Dezimetern) und veröffentlicht jede Meldung als *eine* Zeile:

```
1|R|42|15,23;-1,39
```

Format 1, Zustand, laufende Nummer, Ziele. Eine Zeile pro Meldung, damit X
und Y eines Ziels aus derselben Meldung stammen — das Modul liefert keine
Ziel-IDs. Der Zustand:

| | Bedeutung |
| --- | --- |
| `R` receiving | Meldung innerhalb der letzten 3 s. Die Ziele sind aktuell. |
| `Q` quiet | Keine Meldung, aber das Modul hat „Meldungen an“ kürzlich quittiert. Es ist da und hat nichts zu sagen. |
| `U` unknown | Weder noch — Verkabelung, Strom, ein Modul, das noch startet. |

Bleiben Meldungen aus, schickt die Firmware alle 5 s „Meldungen
einschalten“ (Funktion `06`). Das ist der Werkszustand und ändert an einem
laufenden Modul nichts; die Quittung ist der einzige Weg, ein stilles Modul
von einem fehlenden zu unterscheiden.

Nach dem Start fragt sie die **Montage** des Moduls ab — Montageart
(Funktion `0A`), Höhe und Neigung (`08`) —, dreimal im Abstand von 5 s,
danach einmal pro Minute, solange es nicht geantwortet hat. Sie stehen als
Auswahl „Radar Mount Mode“ und Zahlen „Radar Mount Height“ (m) und „Radar
Mount Angle“ (°) bereit. Deren Wert ist immer, was das Modul zurückgelesen
hat: Eine Änderung geht als Befehl hinaus (`09` für die Montageart, `07`
für Höhe in cm und Neigung in 1/100° zusammen), das Modul quittiert, die
Firmware fragt nach, und erst die Antwort erscheint. Werte außerhalb von
0,5–5 m und 0–90° schreibt sie nicht. Beim Start schreibt sie nichts.

## Was offen ist

Nur mit echter Hardware zu klären, und deshalb Einstellungen statt
Annahmen:

- **Ob das LD2460 im leeren Raum leere Meldungen schickt oder verstummt.**
  Verstummt es, sieht ein leerer Raum aus wie `Q`. Bis das beobachtet ist,
  gilt `Q` als „nicht verfügbar“. Wer es beobachtet hat, schaltet beim
  Sensor **„Stille = leerer Raum“** ein. Die Diagnose-Entität „Radar Empty
  Reports“ zählt leere Meldungen — steigt sie im leeren Raum, ist die Frage
  beantwortet, und der Schalter bleibt aus.
- **Das Vorzeichen der X-Achse** — jetzt beschrieben (siehe „Links und
  rechts“), aber an einem Modul nur mit den zwei Gängen zu bestätigen.
- **Der Aufbau der Quittungen für `06` und `0B` und der Montage-Befehle
  `07`–`0A`.** Er stammt aus dem MIT-lizenzierten
  [smarthomeshop/ld2460](https://github.com/smarthomeshop/ld2460) und
  dessen Einbauanleitung, weil das Handbuch selbst nicht vorlag.
- **Was das Modul mit Montageart, Höhe und Neigung genau rechnet.** Dass es
  sie nutzt, sagt die Anleitung; wie, nicht. Ob die Schrägkorrektur der
  Kalibrierung danach noch nötig ist, entscheidet die Messung im Raum.
- **Reichweite und Öffnungswinkel** des Sichtbereichs auf der Karte sind
  Planungswerte (6 m, 120°), keine gemessenen.
- Raumkarte, Zonen und MQTT-Export sind mit simulierten Sensoren getestet,
  **noch nicht mit einem echten Modul im Raum**.
- Die Kenngrößen der Zielverfolgung — Zuordnung bis 0,9 m, 1,5 s Gedächtnis,
  50 % Trefferquote, 20 s Vertrauen in Störquellen, Störquellen ab 3 % der
  Meldungen — sind an simulierten Meldungen gewählt. Wie gut sie zu einem
  echten LD2460 passen, zeigt erst der Raum.
- **Die Kalibrierung ist nur an simulierten Modulen geprüft.** Die
  Rauschgrenze (6 cm) und die Stufen der Kontrollpunkte (15/30 cm) sind
  gewählt, nicht an echten Messungen abgeleitet. Ob ein Fehler, der mit
  der Entfernung wächst, von der Montage kommt (5° Richtungsfehler sind
  bei 5 m schon etwa 44 cm), vom Modul oder von Reflexionen, lässt sich
  nur mit Messungen im Raum trennen — mit Kontrollpunkten, die nicht in
  die Rechnung eingehen.

## Geräte aus früheren Versionen

WLAN-CSI-Geräte, die mit Echolot bis 0.14 angelegt wurden, bleiben in
`/data/devices.json` genau so stehen, wie sie geschrieben wurden — Kennung,
Zugangsdaten, gelerntes Profil. Echolot 1.0 baut keine CSI-Firmware mehr;
unter *Sensoren → Aus früheren Versionen* stehen sie mit zwei Knöpfen:

- **Auf Radar umstellen** macht daraus einen Radar-Sensor mit derselben
  Kennung, demselben Knotennamen (und damit denselben Entity-IDs in Home
  Assistant), demselben WLAN, API-Schlüssel, OTA- und Notfall-WLAN-Passwort.
  Der vollständige alte Eintrag wird vorher als
  `devices/<id>/csi_record.json` gesichert. Danach LD2460 anschließen,
  Firmware bauen und aufspielen — **per WLAN** geht, das OTA-Passwort ist
  dasselbe. Bis dahin zeigt der Sensor „noch CSI-Firmware“.
- **Entfernen** nimmt den Eintrag aus der Liste; die Sicherungskopie bleibt.

Die CSI-Zonen der alten Versionen werden beim ersten Start mit MQTT aus Home
Assistant entfernt: das Programm, das sie gefüllt hat, gibt es nicht mehr,
und ein Belegt-Sensor, der nie wieder einen Wert bekommt, wäre schlimmer als
keiner. Ihr Verlauf im Recorder bleibt. Aufnahmen aus dem Calibration Lab
(`/data/calibration.sqlite3` und Vorgänger) werden nicht gelöscht, nur nicht
mehr gelesen.

Radar-Sensoren aus 0.14 laufen unverändert weiter und gelten nicht als
veraltet: ihr Konfigurations-Fingerabdruck enthielt noch die CSI-Felder,
Echolot erkennt beide Formen.

## Firmware aufspielen

**Über USB (erstes Mal).** *Über USB flashen* nutzt
[ESP Web Tools](https://esphome.github.io/esp-web-tools/) und Web Serial —
Chrome oder Edge am Computer, nur auf HTTPS-Seiten oder `localhost`. Ist Home
Assistant nur per HTTP erreichbar, das Add-on direkt unter
`http://<host>:8099` öffnen, vom Rechner, an dem der ESP hängt.

**Über WLAN (danach).** *Aufspielen* unter *Über WLAN* schickt die gebaute
Firmware vom Add-on aus an den Sensor — funktioniert aus jedem Browser,
auch vom iPad. Adresse leer lassen heißt `<knotenname>.local`; wo mDNS nicht
durchkommt, die IP eintragen. Lehnt der Sensor das OTA-Passwort ab, ist
seine Firmware älter als dieses Passwort: einmal per USB flashen.

**Einstellungen ändern.** Alles außer dem Namen steckt in der Firmware.
Nach dem Speichern zeigt der Sensor „Einstellungen nicht geflasht“, bis neu
gebaut und aufgespielt ist. Knotenname und Board sind fest — sie tragen die
Entity-IDs und die Hardware.

**Erreichbarkeit prüfen** testet Port 6053 (ESPHome-API) und 80
(Statusseite):

| Ergebnis | Bedeutung |
| --- | --- |
| Name nicht auflösbar | mDNS erreicht das Add-on nicht — IP eintragen |
| Nichts antwortet | Gerät aus, anderes Netz, oder das WLAN isoliert seine Clients |
| Nur Statusseite | Gerät lebt; `http://<ip>/` sagt, woran es hakt |
| API antwortet | Netz in Ordnung |

### Wenn die Oberfläche ohne Aussehen lädt

Große schwarze Symbole, keine Räume, nichts lässt sich anklicken: Die Seite
kam an, ihr Stylesheet oder ihre Skripte nicht. Echolot zeigt dann einen
Kasten mit den betroffenen Dateien und wie sie ankamen — Status,
Content-Type, Kompression, Länge. Steht dort Status 200 und beginnt die Datei
mit Text, der nicht zur installierten Version passt, hat ein Cache
geantwortet — bis 1.1.1 konnte das über HTTPS der Service Worker von Home
Assistant sein; seit 1.1.2 kommen die Dateien aus einem Pfad mit der
Version darin (`assets/<version>/`) und sind davor sicher. Andere Ursachen
sind Proxys vor Home Assistant (NGINX, Cloudflare, ein Reverse Proxy für
HTTPS), die CSS- oder JavaScript-Antworten verändern: falscher
Content-Type, doppelt oder falsch komprimiert, abgeschnitten. Zum Flashen über USB braucht der Browser HTTPS
**und** die Skripte; ohne sie gibt es keinen Flash-Knopf. Den Kasten
abfotografieren und mitschicken, wenn du ein Problem meldest.

### Wenn „Preparing installation“ nicht weitergeht

Hinter der Meldung stecken zwei Schritte: der serielle Kontakt zum Chip und
der Download der Firmware. Beide haben keine Zeitgrenze. Unter dem
Flash-Knopf nennt Echolot den Schritt und nach etwa 20 Sekunden ohne
Fortschritt den passenden Rat.

**Hängt der Chip:** Download-Modus von Hand. **BOOT** halten (GPIO0 bei
ESP32, S2, S3; GPIO9 bei C3, C5, C6), **RESET/EN** kurz drücken, BOOT
loslassen — dann den Port **neu wählen**, denn im Download-Modus meldet sich
ein Chip mit nativem USB als anderes Gerät. Andere Programme am Port
schließen, Ladekabel ohne Datenadern ausschließen. Eine vorhandene Firmware
ist nie der Grund: geschrieben wird über den ROM-Bootloader.

**Hängt der Download, oder hilft nichts:** *Datei* lädt dasselbe Image; es
ist ein vollständiges Factory-Image für Offset `0`:

```sh
esptool --chip esp32c5 --port /dev/ttyACM0 write-flash 0x0 firmware.bin
```

### Wenn ein Build am Compiler scheitert

Endet ein Build mit „`riscv32-esp-elf-gcc` … was not found in the PATH“, ist
fast immer ein abgebrochener Toolchain-Download schuld: PlatformIO prüft nur,
ob das Verzeichnis existiert, nicht ob ein Compiler darin liegt. Echolot
erkennt das und bietet **Toolchain zurücksetzen** an, das das Paket löscht,
damit der nächste Build es neu lädt.

## Was CI prüft

- **Unit-Tests** bei jedem Pull Request: Gerätemodell und Umstellung alter
  Geräte, Räume, Geometrie (Python und die Browser-Fassung gegen dieselben
  Fälle), Zonenauswertung, MQTT-Export samt Löschwarteschlange, die
  Live-Verbindung gegen eine nachgebaute API, die HTTP-Routen, der
  Protokollkern des Firmware-Bausteins mit dem Host-Compiler und ein
  ESPHome-`host`-Build, der gegen ein Pseudo-Terminal läuft, auf dem der Test
  das Modul spielt.
- **`esphome config`** über jedes Board und die Sonderfälle (Antennen-Pin,
  minimale Firmware, 5 GHz und AUTO am C5).
- **Echte Images** für ESP32 (Xtensa) und ESP32-C5 (RISC-V) — bei jedem
  Push auf `main` und bei Pull Requests mit dem Label `firmware`, weil ein
  kalter Build 2 GB lädt.

Die Seitenskripte werden auf Syntax geprüft; ein Unit-Test sieht zu, dass
keine Ansicht ihre eigenen Methoden überschreibt.

Grün in CI heißt: baut und linkt. Ob ein Modul an einem Board richtig misst,
sagt erst die Hardware.

## Support

Fehler und Wünsche: [github.com/NacoTeX/echolot/issues](https://github.com/NacoTeX/echolot/issues).
