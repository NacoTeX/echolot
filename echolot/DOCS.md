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

Ab 2.0 fragt das Add-on Home Assistant, ob jemand zu Hause ist
(`homeassistant_api`, nur lesend: die Zustände der `person`-Entitäten oder
einer genannten Entität) — siehe „Echolot lernt“.

Echolot ist nur über die Seitenleiste von Home Assistant erreichbar
(Ingress): Home Assistant meldet dich an, und das Add-on antwortet nur
dessen Ingress-Gateway (`172.30.32.2`). Andere Add-ons im internen Netz
bekommen keine Antwort — die Oberfläche kann Geräteschlüssel zeigen und
Firmware aufspielen.

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

Meldet das Modul Ziele, beantwortet aber 90 s lang keine einzige Frage der
Firmware, sagen Sensor- und Raumseite **„Modul hört den Sensor nicht“**:
Die Meldungen kommen an, die Befehle nicht — meist sitzt die Leitung vom
TX des ESP zu Rx2 (Pin 8) nicht. Ohne sie lassen sich Montage und
Erfassungsbereich weder lesen noch einstellen.

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
  Gestrichelt hohl: Meldungen *ohne Ort*, die das Sensormodell nirgends im
  Raum unterbringen kann (siehe „Wie gezählt wird“).
- **Orange Kreise mit Kreuz** — gelernte Störquellen.
- **Zonen** — leuchten auf, sobald jemand darin ist, mit der Zahl in der
  Ecke. Rechts stehen sie mit Zustand und Abwesenheits-Countdown.
- **Eingänge** — grau gepunktet umrandet (siehe „Eingänge“).
- **Sensor und Sichtbereich** — der gestrichelte Fächer zeigt, wohin der
  Sensor laut Plan blickt, aus Reichweite und Öffnungswinkel, an den
  Wänden abgeschnitten; eine Planungshilfe, keine Messung. Wie weit das
  Modul wirklich sieht, zeigen die Punkte. Blickt der Sensor laut Plan aus
  dem Raum hinaus, verschwindet der Fächer hinter der Wand — dann steht
  über der Karte, dass die Meldungen so an falschen Stellen landen, mit
  *Ausrichten* direkt zum Sensor im Raumeditor.

Ist jemand verschwunden, ohne durch einen Eingang zu gehen, steht rechts
**vermutlich noch da** mit der Zeit, die das höchstens noch gilt, und
**Raum ist leer** — für den Fall, dass du es besser weißt.

Hat ein Raum keine aktuelle Messung, sagt die Karte warum — kein Sensor
zugeordnet, keine Verbindung, Radar antwortet nicht — statt einen leeren
Raum zu zeigen.

**Aufzeichnen.** *Raum → Aufzeichnen* hält fest, was der Sensor meldet —
jede Zeile, wie sie ankam, und jeden Verbindungswechsel —, zwischen 10 s
und 60 min. Keine Schlüssel, kein WLAN in der Datei. Während der Aufnahme
lassen sich Marken setzen: wie viele Personen im Raum sind, ob jemand in
einer Zone ist, wo jemand steht (Punkt auf dem Plan antippen, mit
Genauigkeit) und freie Notizen. Eine Aufzeichnung lässt sich danach

- **auswerten** — dieselben Meldungen laufen noch einmal durch die
  Auswertung, auf einer virtuellen Uhr und damit Schritt für Schritt
  gleich. Mit Marken vergleicht der Bericht Personen, Zonen und
  Standpunkte mit dem, was markiert war; ohne Marken zeigt er nur, was
  gezählt wurde.
- **vergleichen** — dieselbe Aufzeichnung mit anderen Filtern (etwa
  längerer Bestätigungszeit) oder mit dem Raum, wie er jetzt ist.
- **abspielen** — auf der Karte, bis 16-fach, mit Sprung zu jeder Marke.
- **exportieren, importieren, löschen.** Insgesamt bis 100 MB und 50
  Aufzeichnungen.

So lässt sich eine Einstellung an echten Meldungen prüfen, bevor sie gilt.

**Raum einrichten.** Werkzeugleiste über der Karte, Inspektor rechts:

| Werkzeug | Taste | |
| --- | --- | --- |
| Auswahl | V | Antippen wählt, Ziehen verschiebt. Bei Zonen: Eckpunkte ziehen verformt, die kleinen Punkte zwischen den Ecken ziehen fügt eine Ecke hinzu, Doppelklick auf eine Ecke entfernt sie. Möbel: Griff oben dreht, Ecke unten rechts ändert die Größe; X und Y im Inspektor sind die linke obere Ecke dessen, was auf dem Plan steht, auch gedreht — zwei Möbel mit gleichem X stehen an derselben Linie, und Breite oder Tiefe ändern hält diese Ecke fest. Sensor: der Griff vor ihm dreht ihn. |
| Rechteck | R | Zone aufziehen. |
| Freiform | P | Ecken antippen; auf die erste tippen oder Enter schließt, Esc bricht ab. |
| Möbel | | Sofa, Bett, Tisch, Schrank, Tür, Fenster … als Orientierung. Unter *Als Zone* wird ein Möbelstück zur Erkennungs-, Ausschluss- oder Eingangszone, die ihm folgt (siehe unten). |
| Wände | | Der Umriss des Raums, für Nischen, L-Formen, Vorsprünge. Ecken ziehen, über die Punkte dazwischen neue einfügen, Doppelklick entfernt eine. *Neu nachzeichnen* zieht die Wände Ecke für Ecke, etwa über dem Grundrissbild. |
| Einrasten | | 5-cm-Raster, Drehungen in 15°-Schritten (Sensor 5°). |
| Rückgängig / Wiederholen | ⌘Z / ⇧⌘Z | |

**Sensor.** Zieh ihn dorthin, wo er hängt, und dreh ihn so, wie er in
den Raum blickt: 0° blickt auf dem Plan nach unten, positive Winkel drehen
im Uhrzeigersinn — der Inspektor sagt es auch in Worten („nach links
oben“). Er zeigt, wie viel des Raums der Sensor laut Plan sieht, und
warnt, wenn ein großer Teil seines Blicks hinter der Wand liegt oder er
ganz aus dem Raum hinaus blickt. *Zum Raum drehen* dreht ihn senkrecht von
der Wand weg, an der er hängt, in einer Ecke schräg in den Raum und, frei
im Raum, zu dessen Mitte; hängt er anders, mit den Pfeilen nachstellen.
Meldet das Modul seinen Erfassungsbereich (siehe „Kalibrieren“, *Filter*)
und zeichnet der Plan etwas anderes, sagt der Inspektor das und bietet bei
einem gleichmäßigen Sektor *Vom Modul übernehmen* an. Einen ungleichen
zeichnet der Plan gleichmäßig: Auf welcher Seite er beginnt, steht nicht
im Handbuch.

**Möbel als Zone.** Wähle ein Möbelstück und stell *Als Zone* auf
*Erkennung* — für Sofa, Bett, Schreibtisch, Esstisch —, auf
*Ausschluss* — für Pflanze, Ventilator, Aquarium — oder, bei einer Tür, auf
*Eingang* (siehe unten). Die Zone trägt den Namen
des Möbelstücks und folgt ihm, wenn du es verschiebst, drehst, in der Größe
änderst oder umbenennst; löschst du es, geht die Zone mit, auch aus Home
Assistant. Der **Rand** (Standard 20 cm) legt fest, wie weit um das Möbel
herum noch mitgezählt wird: Das Radar verortet jemanden, der sitzt oder
liegt, nicht genau auf den Polstern. An einer Wand endet die Zone an der
Wand. Sitz- und Liegemöbel bekommen 30 s Abwesenheitsverzögerung statt
10 s. Ein Tipp auf die Zone wählt das Möbelstück; einzeln verformen lässt
sich eine Möbelzone nicht — dafür eine Zone von Hand zeichnen.

**Eingänge.** Das Radar verliert Menschen, die still sitzen oder liegen,
auch für länger als jede Abwesenheitsverzögerung. Eine Zone der Art
*Eingang* — über die Tür, einen Durchgang oder den Rand des Sichtfelds,
an dem man aus dem Blick geht — sagt Echolot, wo man den Raum verlässt.
Verschwindet ein gezähltes Ziel **woanders**, gilt der Raum weiter als
belegt: bis jemand abseits der Eingänge wieder erkannt wird, bis die Zeit
unter *Anwesenheit annehmen* abläuft (beim Raum, Standard 30 min, 0 schaltet
es ab) oder bis du *Raum ist leer* tippst. Die Personenzahl bleibt, was das
Radar misst. Wer an einem Eingang zum ersten Mal gemeldet wird, ist jemand
Neues und hebt die Annahme nicht auf. Ein Eingang zählt mit wie der übrige
Raum und wird keine eigene Entität. Zeichne ihn großzügig: Das Radar
verliert Gehende oft kurz vor der Tür. Ohne Eingang gibt es keine Annahme.

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
Belegt ist das erst am eigenen Modul: *Kalibrieren → Achsen* prüft es mit
zwei kurzen Gängen (siehe „Kalibrieren“) und sagt, ob „Links und rechts
tauschen“ an gehört.

## Kalibrieren

*Raum → Kalibrieren*, vier Reiter. Alles geschieht am lebenden Raum; die
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

**Achsen.** Welche Seite des Moduls links ist, steht in keinem Handbuch,
und ein Sensor, der auf dem Plan in die falsche Richtung blickt, fällt oft
erst bei der Ausrichtung auf. Echolot zeichnet dafür zwei kurze Wege auf den
Plan, beide vom Sensor aus gedacht: **1 — vom Sensor weg**, **2 — quer vor
ihm vorbei**, jeweils von A nach B, gut 2 m lang und mindestens 30 cm von
den Wänden. Führt ein Weg durch ein Möbelstück, sagt die Seite es; ein Tipp
auf den Plan legt beide Wege an eine andere Stelle (nicht zu nah am Sensor,
nicht außerhalb seines Blickfelds). Allein im Raum auf A stellen, *Gehen*
tippen, nach dem Countdown kurz stehen bleiben, zügig und geradeaus nach B
gehen und dort stehen bleiben, bis die Zeit um ist (8 s).

Aus dem, was das Modul dabei gesehen hat, folgt:

- **Links und rechts.** Mit dem Weg quer vor dem Sensor dreht nur eine der
  beiden Einstellungen die gemessene Bewegung in die gezeichnete Richtung.
  Vorgeschlagen wird sie nur, wenn sie höchstens 45° daneben liegt und die
  andere mindestens 90°.
- **Die Blickrichtung auf dem Plan.** Der Weg vom Sensor weg zeigt, wie weit
  der Sensor auf dem Plan anders blickt als im Raum; ab 20° schlägt Echolot
  eine neue Richtung vor, auf 5° gerundet. Links/Rechts und Richtung werden
  zusammen bestimmt, weil bei gedrehtem Plan auch der Weg vom Sensor weg
  seitlich läuft.
- **Nichts**, wenn der Weg vom Sensor weg nicht als Entfernen gesehen wurde
  — der Sensor steht dann auf dem Plan an einer anderen Wand oder blickt
  ganz woandershin — oder der Weg quer zu keiner Einstellung eindeutig
  passt. Die Seite sagt, was zu prüfen ist.

Auf dem Plan erscheinen die gezeichneten Wege, rot gestrichelt, wo das Modul
den Gang mit der jetzigen Einstellung sieht, und orange, wo es ihn nach
*Übernehmen* sähe. *Übernehmen* setzt „Links und rechts tauschen“ und die
Richtung; stimmt schon alles, heißt der Knopf *Als geprüft vermerken*. Der
Server rechnet dabei aus den Gängen neu und nur für den Raum, wie er beim
Auswerten war; eine bestehende Ausrichtung gilt danach als veraltet.
Gänge mit einem anderen Sensor oder vor einer Neumontage gelten nicht.

Den Gang findet Echolot unter allem, was das Modul meldet, mit einer
eigenen Zielverfolgung: Jedes Ziel behält seine Spur, länger gesehene Ziele
werden zuerst zugeordnet, keine Spur greift weiter als 30 cm, und Stücke
einer Spur, die das Modul kurz verloren hatte, werden dort angesetzt, wohin
die Person unterwegs war. Der Gang ist die Spur, die so weit vom Sensor
begann und endete, wie A und B auf dem Plan liegen — das gilt, wie auch
immer die Achsen stehen, und schließt ein Spiegelbild in Wand oder Scheibe
aus, das weiter weg erscheint. Weicht das mehr als 1,5 m ab, gibt es kein
Ergebnis. Liegt ein Modul auf der Seite, misst es quer die Höhe und meldet
beim Quergang kaum Bewegung; auch das sagt die Seite.

**Filter.**

- **Bestätigungszeit** (0–5 s, Standard 1 s): Ein neues Ziel zählt erst,
  wenn das Radar es so lange meldet, und zwar in mindestens der Hälfte der
  Meldungen. 0 s zählt jede Meldung sofort.
- **Glättung** (aus / normal / stark): Jede Meldung zieht ein Ziel ein
  Stück zur neuen Position, umso mehr, je länger die letzte zurückliegt —
  mit einer Zeitkonstante von 0,29 s (normal) oder 0,70 s (stark). So
  dauert die Glättung gleich lang, ob das Modul zwei- oder zehnmal in der
  Sekunde meldet; bei fünf Meldungen je Sekunde ist das die Hälfte
  (normal) oder ein Viertel (stark) des Wegs, wie vor 1.7. Ruhigere Punkte
  an Zonengrenzen, dafür folgt der Punkt einer gehenden Person etwas
  später.
- **Erfassungsbereich des Moduls** (ab Firmware 1.8): wie weit und über
  welchen Winkel das LD2460 überhaupt Ziele meldet — 0° ist geradeaus.
  Das Modul hält ihn selbst, je Montageart einen; ab Werk an der Wand
  6 m und −60° bis +60°, an der Decke 4 m rundum. Was die HLK-App dort
  zuletzt eingestellt hat, entscheidet, was ankommt; die Seite zeigt, was
  das Modul zurückliest, und *Ins Modul schreiben* ändert es wie die
  Montage. Enger stellen hält etwa den Flur hinter einer offenen Tür
  heraus, bevor er Zielplätze im Modul belegt. Ausrichtung und
  Störquellen bleiben: Der Bereich entscheidet, *welche* Ziele das Modul
  meldet, nicht *wo*.

## Echolot lernt

Ab 2.0 beobachtet Echolot jeden Raum, sobald sein Sensor meldet, und lernt
aus dem Alltag darin — ohne dass jemand dafür still stehen, Punkte
abschreiten oder den Raum räumen muss. Was es weiß und tut, steht auf der
Raumseite in der Karte **Echolot lernt**: wie lange es beobachtet hat, wie
viele Wege und Lieblingsplätze es kennt, ob der Plan zu den Wegen passt,
offene Vorschläge und ein Tagebuch.

**Was es aus den Spuren macht.** Jede Spur der Zielverfolgung wird zerlegt
in Gehen, Ankommen, Sitzen und Weggehen. Ein *Aufenthalt* ist, wo jemand
still war — samt der Lücken, in denen das Modul ihn verlor und wiederfand.
Ein Aufenthalt, zu dem ein Weg hinführte, ist der eines Menschen; einer,
zu dem nie jemand ging, ist verdächtig. Gespeichert wird begrenzt (höchstens
6000 Wegpunkte, 600 Aufenthalte, 40 Abwesenheiten) in den Koordinaten des
Sensors, also unabhängig davon, wie der Sensor auf dem Plan liegt — und je
Sensor und Montage: ein anderer Sensor oder *Neu montiert* beginnt von
vorn.

**Störquellen, während niemand zu Hause ist.** Home Assistant weiß, ob
jemand zu Hause ist (alle `person`-Entitäten, oder eine Entität, die du
unter *System → Selbstlernen* nennst, etwa `zone.home`). Ist seit 10
Minuten niemand da, sammelt Echolot, was im Raum steht. Bewegt sich dabei
irgendwo im Haus etwas — ein Haustier, ein Saugroboter, jemand ohne
Telefon —, zählt diese ganze Abwesenheit nicht. Eine Stelle wird Störquelle,
wenn dort in **zwei getrennten Abwesenheiten** von zusammen mindestens 30
Minuten in mindestens 3 % der Zeit ein Ziel stand (die Schwelle der
Aufnahme im leeren Raum). Nie von selbst dort, wo Menschen sitzen: Liegt
die Stelle auf einem Sitzmöbel des Plans oder dort, wo mindestens dreimal
jemand hinging und zusammen 20 Minuten blieb, wird sie nur vorgeschlagen
— sonst würde etwa ein Kind, das schläft, während die Eltern fort sind, im
eigenen Bett als Störquelle gelernt. Gelernte Störquellen zählen wie
aufgenommene (gestrichelt auf dem Plan).

**Haltezeiten aus den Aussetzern.** Aus den Lücken, in denen das Modul
Sitzende verlor, folgt, wie lange Raum und Zone warten müssen, bevor sie
sich leer melden: das 99. Perzentil der Lücken, abzüglich der 1,5 s, die die
Zielverfolgung ein verlorenes Ziel noch zählt, plus Bestätigungszeit und 2 s,
auf 5 s gerundet, höchstens 3 Minuten. Gilt nur, wenn es länger ist als
eingestellt — Echolot verkürzt nie, was du gesetzt hast. Ab 3 Aufenthalten
mit zusammen 20 Lücken; Lücken nach einem Weggehen zählen nicht.

**Der Plan gegen die Wege.** Gehende Menschen bleiben in den Wänden. Liegen
weniger als 90 % der letzten Wege darin, sucht Echolot, in welche Richtung
der Sensor schauen muss — von der Stelle aus, an der er eingezeichnet ist,
jeden Grad, beide Seiten. Übernommen wird eine neue Blickrichtung nur, wenn
alles stimmt: Weniger als 80 % der Wege lagen im Raum, mit ihr sind es
mindestens 95 %, sie ist eindeutig, links und rechts sind geklärt (durch
den Gang-Test unter *Kalibrieren → Achsen*, eine Ausrichtung aus
Standpunkten, oder weil die andere Seite klar schlechter passt) und die
Sitzmöbel oder eine Tür auf dem Plan bestätigen sie. Der Grund: In einer
Ecke passen dieselben Wege gespiegelt oft genauso — ohne Möbel wäre die
Korrektur ein Raten. Alles andere ist ein Vorschlag: *Seiten vertauscht?*
mit dem Gang-Test als Prüfung, *Position prüfen*, wenn die Wege zu einem
Sensor an ganz anderer Stelle viel besser passen, *Wege außerhalb*, wenn
das Radar durch eine Wand sieht (dann hilft, den Erfassungsbereich zu
begrenzen). Die Korrektur ist grob, auf wenige Grad; zentimetergenau wird
es mit Standpunkten. Die vorherige Lage steht im Verlauf der Ausrichtung.

**Zonen vorschlagen.** Wo Menschen zusammen mindestens 30 Minuten in
mindestens drei Aufenthalten waren und keine Erkennungszone liegt, schlägt
Echolot eine vor — für das Sitzmöbel dort, sonst ein Quadrat um den Platz.
*Vorschau* zeichnet sie auf den Plan.

**Aktivität zeigen** legt über den Plan, wo bestätigte Ziele waren — ohne
die Störquellen.

**Selbstständig, nur vorschlagen, aus** (*System → Selbstlernen*).
Selbstständig übernimmt Echolot, was die Regeln oben erlauben, und schlägt
den Rest vor; *Nur vorschlagen* wartet bei allem auf *Übernehmen*; *Aus*
beobachtet nichts, und Gelerntes gilt nicht mehr. Ausgewertet wird alle
10 Minuten im Hintergrund, die Suche nach der Blickrichtung höchstens
stündlich — oder sofort mit *Jetzt auswerten*.

**Alles lässt sich zurücknehmen.** Jede Übernahme steht im Tagebuch mit
ihrem Grund; *Rückgängig* nimmt sie zurück, und Echolot schlägt sie nicht
wieder vor, ebenso wenig wie Abgelehntes. *Neu beginnen* vergisst alles
Gelernte des Raums. Was du selbst eingestellt hast — Haltezeiten,
aufgenommene Störquellen, Zonen —, ändert das Lernen nie; es hat eine
eigene Ebene im Raum, die der Editor nicht überschreibt. Eine Drehung des
Sensors geht durch den Verlauf der Ausrichtung wie jede andere.

**Gespeichert** wird unter `/data/learning`: je Raum eine Datei mit
Wegpunkten, Aufenthalten, Abwesenheiten, Tagebuch und Abgelehntem, dazu
die Einstellungen. Nichts davon verlässt Home Assistant. Ohne
`homeassistant_api` (oder ohne eine einzige `person`) weiß Echolot nicht,
wer zu Hause ist: Dann lernt es alles außer den Störquellen.

## Wie gezählt wird

Messdefinition 8 (seit 2.0). Was eine **neue Meldung** ist, entscheidet
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
**Bestätigungszeit** lang gemeldet hat, außerhalb der Störquellen — der
im leeren Raum aufgenommenen und der gelernten (siehe „Echolot lernt“).

Dann für jedes Ziel der aktuellen Meldung, in dieser Reihenfolge:

1. Umrechnung vom Sensor in den Raum: erst das Sensormodell aus der
   Ausrichtung — Entfernungsmaßstab und -versatz, Winkelmaßstab,
   Schrägstrecke —, dann Position, Blickrichtung, Spiegelung. Kann das
   Modell eine Meldung nicht unterbringen — misst das Modul die Schräge,
   und die gemeldete Entfernung ist mehr als 20 cm kürzer als der
   Höhenunterschied zwischen Modul und Körper —, hat sie **keinen Ort**:
   Sie zählt nirgends, auch nicht am Fuß des Sensors, wo die Rechnung sie
   hinlegen würde.
2. Liegt es weiter als die **Randtoleranz** (Standard 30 cm) außerhalb der
   Wände, zählt es nicht. Radar sieht durch Trockenbau. Die Wände sind der
   Umriss des Raums, wenn er einen hat, sonst sein Rechteck; bei einem
   Umriss gilt die Toleranz als Abstand zur nächsten Wand.
3. Liegt es in einer **Ausschlusszone**, zählt es nicht — für Ventilator,
   Vorhang im Luftzug, Aquarium.
4. Ist es noch nicht bestätigt, zählt es nicht.
5. Sonst zählt es für den Raum und für jede **Erkennungszone**, in der es
   liegt. Zonen dürfen sich überlappen.

Ein bestätigtes Ziel, das die letzte Meldung nicht enthielt — das Modul
verliert still stehende Menschen immer wieder für eine Meldung —, zählt
weiter, dort, wo es zuletzt war, solange die Zielverfolgung es noch kennt
(1,5 s). Auf der Karte ist es blass gezeichnet („kurz nicht gemeldet“). So
springt die Personenzahl nicht bei jeder ausgefallenen Meldung auf 0 und
zurück. Mit einer Bestätigungszeit von 0 s wird nichts gehalten: Dann
zählt genau, was jede Meldung sagt.

Die Ziele stehen in Koordinaten des Sensors. Verschieben oder Drehen auf
dem Plan lässt sie bestätigt; ein anderer Sensor, eine andere
Bestätigungszeit oder neue Störquellen beginnen die Bestätigung neu, ebenso
jeder Ausfall.

Definition 7 (1.7–1.8) kannte keine gelernten Störquellen und keine
gelernten Haltezeiten. Definition 6 (1.6) setzte solche Meldungen an den Fuß des Sensors und
zählte sie dort, und glättete je Meldung um einen festen Anteil statt mit
einer Zeitkonstante. Definition 5 (1.5) kannte keine Eingänge. Definition 4 (1.4) zählte nur die Ziele der letzten Meldung; eine
ausgefallene Meldung nahm eine Person aus der Zählung und gab sie mit der
nächsten zurück. Definition 3 (1.3) zählte die Lebenszeichen-Wiederholungen als Meldungen
— ein Ziel wurde so schneller bestätigt, als das Radar es tatsächlich
gemeldet hatte —, nahm je Auswertung nur die letzte Meldung, zur Zeit der
Auswertung, und konnte ein Ziel nach Ablauf seines Gedächtnisses wieder
aufnehmen. Definition 2 (1.1–1.2) war Definition 3 ohne Sensormodell;
Definition 1 (Echolot 1.0) waren die Schritte 1–3 und 5 auf jede Meldung
einzeln, mit dem Rechteck als Wänden.

Raum und Zone gelten als belegt, solange ein Ziel darin ist, und danach
noch für ihre **Abwesenheitsverzögerung** (Raum 10 s, Zone einstellbar) —
oder so lange, wie das Lernen aus den Aussetzern Sitzender als nötig
gefunden hat, wenn das länger ist. Das ist der ehrliche Regler dafür, dass
das Modul still sitzende Menschen zeitweise verliert.

Hat der Raum **Eingänge**, gilt er außerdem als belegt, solange jemand
nicht abgemeldet ist: Ein gezähltes Ziel, das aus der Zählung fällt, ohne
dass es zuletzt — geglättet oder wie gemeldet — in einem Eingang lag, ist
eine Person mehr, die nicht abgemeldet ist. Ein Ausfall des Sensors zählt
genauso. Jedes Ziel, das neu zu zählen beginnt und *zuerst* abseits der
Eingänge gemeldet wurde, ist eine davon weniger; eines, das in einem
Eingang zuerst gemeldet wurde, ist jemand Neues. Die Annahme endet
*Anwesenheit annehmen* nach dem letzten Verschwinden, oder mit *Raum ist
leer*. Die Personenzahl ändert sie nicht, nur die Belegung.

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
Ausschlusszonen und Eingänge werden keine Entitäten. *Anwesenheit* des
Raums schließt die Annahme an Eingängen ein, *Personen* nicht. Jede Entität
trägt als Attribute die Regeln, nach denen ihr Wert entstand:
`definition_version` (jetzt 8), `confirm_s`, `smoothing`, `entrances`,
`assume_present_s`, `interference_spots` (davon gelernt: `learned_spots`),
`hold_s` (die Abwesenheitsverzögerung des Raums, wie gezählt wird —
eingestellt oder gelernt) und das Sensormodell
(`range_scale`, `range_offset_m`, `azimuth_scale`, `slant`). So lässt sich ein
Verlauf auch nach einer Kalibrierung richtig lesen. Alle hängen an zwei
Verfügbarkeiten: dem Add-on und dem Raum. Gelöschte Zonen und Räume
verschwinden aus Home Assistant; die Löschung steht in einer Warteschlange
auf der Platte, bis der Broker sie bestätigt hat, und übersteht auch einen
Neustart. Von dort exportieren Home Assistants eigene Brücken — HomeKit,
Matter, Google, Alexa — die Entitäten weiter.

**Ein Raum im Dashboard.** Auf der Raumseite zeigt *Im Dashboard*, wie der
Raum live in ein Home-Assistant-Dashboard kommt — Plan, Personen, Zonen,
wie in Echolot:

1. Die Karte `echolot-room-card.js` herunterladen und als
   `/config/www/echolot-room-card.js` ablegen (etwa mit dem File-editor-
   oder Samba-Add-on). Einmal für alle Räume.
2. Einstellungen → Dashboards → ⋮ → Ressourcen: `/local/echolot-room-card.js`
   als *JavaScript-Modul* eintragen, die Seite neu laden.
3. Im Dashboard eine Karte *Manuell* hinzufügen, mit dem YAML von der
   Raumseite:

   ```yaml
   type: custom:echolot-room-card
   addon: local_echolot   # das Kürzel des Add-ons, von der Raumseite
   room: r1a2b3c4d        # die Raum-ID, von der Raumseite
   title: Wohnzimmer      # optional
   ```

Home Assistant lässt den Browser nur über Ingress an ein Add-on, und nur mit
einer Sitzung, die ein **Administrator** beim Supervisor öffnen darf. Die
Karte tut, was Home Assistants eigene Add-on-Seite tut: Sie öffnet die
Sitzung, bestätigt sie jede Minute und zeigt die Seite `embed?room=…` des
Add-ons in einem Rahmen; mehrere Karten teilen sich eine Sitzung. Andere
Benutzer sehen einen Hinweis statt des Plans — für sie sind die
MQTT-Entitäten da. Was die Karte zeigt, zeichnet das Add-on; ein Update
des Add-ons aktualisiert sie also mit, die Datei in `/config/www` bleibt
dieselbe. Hell und dunkel folgen Home Assistant.

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

Die **Version** des Moduls fragt sie 2 s nach dem Start ab, dreimal im
Abstand von 5 s, danach einmal pro Minute, bis es antwortet (bis 1.8:
dreimal, dann nie wieder — ein Modul, das später anlief, blieb bis zum
nächsten Neustart ohne Version).

Nach dem Start fragt sie die **Montage** des Moduls ab — Montageart
(Funktion `0A`), Höhe und Neigung (`08`) —, dreimal im Abstand von 5 s,
danach einmal pro Minute, solange es nicht geantwortet hat. Sie stehen als
Auswahl „Radar Mount Mode“ und Zahlen „Radar Mount Height“ (m) und „Radar
Mount Angle“ (°) bereit. Deren Wert ist immer, was das Modul zurückgelesen
hat: Eine Änderung geht als Befehl hinaus (`09` für die Montageart, `07`
für Höhe in cm und Neigung in 1/100° zusammen), das Modul quittiert, die
Firmware fragt nach, und erst die Antwort erscheint. Werte außerhalb von
0,5–5 m und 0–90° schreibt sie nicht. Beim Start schreibt sie nichts.

Ab 1.8 genauso mit dem **Erfassungsbereich** (Funktion `12` liest, `11`
schreibt): Zahlen „Radar Range“ (m), „Radar Range Start“ und „Radar Range
End“ (°), gefragt mit der Montage, Werte immer vom Modul zurückgelesen. Die
drei gehen zusammen hinaus; nacheinander geänderte kommen alle an. Was die
Montageart des Moduls nicht erlaubt — an der Wand mehr als 6 m oder
jenseits von ±60°, an der Decke mehr als 4 m oder außerhalb von 0–360° —,
unter 0,5 m oder ein Sektor, der endet, bevor er beginnt, schreibt sie
nicht, und die Zahl springt auf den Wert des Moduls zurück. Wechselt die
Montageart, liest sie den Bereich der neuen.

Alle Befehle und Quittungen sind nach Hi-Links vollständigem
Schnittstellenprotokoll V1.0 gebaut, einschließlich seiner Beispiele Byte
für Byte; ein Modul mit Firmware V1.3 hat im Test von 1.7 Version,
Montageart, Höhe und Neigung so beantwortet.

## Was offen ist

Nur mit echter Hardware zu klären, und deshalb Einstellungen statt
Annahmen:

- **Das Lernen ist an einem simulierten Zuhause geprüft**, nicht an einem
  echten: ein Wohnzimmer mit zwei Reflektoren, Sitzenden, die das Modul
  immer wieder verliert, ein Haustier während der Abwesenheit, ein Kind,
  das schläft, während die Eltern fort sind — alles durch die echte
  Zielverfolgung. Die Schwellen (zwei Abwesenheiten, 3 %, 20 Minuten
  Sitzen, 99. Perzentil, 90/80/95 % der Wege) sind daran gewählt. Wie oft
  ein echtes LD2460 Sitzende verliert, wie seine Reflexionen aussehen und
  wie gut Home Assistants Anwesenheit stimmt, zeigt erst der Raum — das
  Tagebuch sagt jedes Mal, worauf eine Übernahme beruht.

- **Ob das LD2460 im leeren Raum leere Meldungen schickt oder verstummt.**
  Verstummt es, sieht ein leerer Raum aus wie `Q`. Bis das beobachtet ist,
  gilt `Q` als „nicht verfügbar“. Wer es beobachtet hat, schaltet beim
  Sensor **„Stille = leerer Raum“** ein. Die Diagnose-Entität „Radar Empty
  Reports“ zählt leere Meldungen — steigt sie im leeren Raum, ist die Frage
  beantwortet, und der Schalter bleibt aus.
- **Das Vorzeichen der X-Achse** — beschrieben (siehe „Links und
  rechts“), an einem echten Modul aber erst mit den zwei Gängen unter
  *Kalibrieren → Achsen* bestätigt. Die Auswertung der Gänge ist an
  simulierten Meldungen mit Reflexionen, Blitzen, Aussetzern und
  Spiegelbildern geprüft (0,3 m Zuordnung, 1 s Gedächtnis, 1,5 m
  Entfernungsabweichung); wie gut das zu einem echten LD2460 passt, zeigt
  erst der Raum.
- **Die Dashboard-Karte** ist gegen eine Nachbildung von Home Assistant
  geprüft, die Ingress-Sitzungen wie der Supervisor behandelt, noch nicht
  in einem echten Home Assistant.
- **Der Erfassungsbereich (`11`/`12`)** ist nach Hi-Links Protokoll
  gebaut und gegen ein nachgebildetes Modul geprüft, an einem echten noch
  nicht gelesen oder geschrieben. Wie das Modul einen ungleichen Sektor
  zu links und rechts legt, steht nicht im Protokoll.
- **Was das Modul mit Montageart, Höhe und Neigung genau rechnet.** Dass es
  sie nutzt, sagt die Anleitung; wie, nicht. Ob die Schrägkorrektur der
  Kalibrierung danach noch nötig ist, entscheidet die Messung im Raum.
- **Reichweite und Öffnungswinkel** des Sichtbereichs auf der Karte sind
  Planungswerte (6 m, 120°), keine gemessenen; meldet das Modul seinen
  Erfassungsbereich, bietet der Raumeditor an, den zu zeichnen. Die
  Warnung „blickt aus dem Raum hinaus“ prüft den Plan, nicht den Raum:
  Ob der Sensor wirklich so hängt, zeigen die Gänge unter *Achsen*.
- Raumkarte, Zonen und MQTT-Export sind mit simulierten Sensoren getestet,
  **noch nicht mit einem echten Modul im Raum**.
- Die Kenngrößen der Zielverfolgung — Zuordnung bis 0,9 m, 1,5 s Gedächtnis,
  50 % Trefferquote, 20 s Vertrauen in Störquellen, Störquellen ab 3 % der
  Meldungen — sind an simulierten Meldungen gewählt. Wie gut sie zu einem
  echten LD2460 passen, zeigt erst der Raum.
- **Eingänge sind an simulierten Meldungen geprüft.** Wie oft ein echtes
  Modul jemanden mitten im Raum verliert, der dann wirklich gegangen ist
  (etwa durch eine Tür, die nicht als Eingang eingezeichnet ist), und wie
  oft ein kurz bestätigter Reflex eine Annahme auslöst, zeigt erst der
  Raum. *Raum ist leer* und *Anwesenheit annehmen* sind die Regler dafür.
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

**Welche Firmware läuft.** Die Sensorseite zeigt, solange der Sensor
verbunden ist, was er selbst meldet: die *Echolot-Firmware* als Add-on-Version und Stand
(`1.7.0 · a1b2c3d4`, ein Fingerabdruck von Baustein und Vorlage), die
ESPHome-Version mit Bauzeit und die *Radarchip-Firmware* des LD2460.
Stimmt der Stand mit dem überein, was dieses Add-on baut, steht dort
*aktuell*, sonst *älterer Stand* — neu bauen und aufspielen. Meldet der
Sensor keinen Stand (vor 1.7 gebaut), steht dort *unbekannt*; eine fremde
Firmware zeigt ihren Projektnamen.

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
