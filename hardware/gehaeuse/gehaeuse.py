"""Echolot-Sensorgehäuse: HLK-LD2460 + Waveshare ESP32-C5-Zero.

Eine schlanke Kachel mit Squircle-Ecken und weichen Kanten. Vorn das
Radar hinter einem gleichmäßig dünnen Fenster, darunter im „Kinn“ der
eingeprägte Name, dahinter der ESP, unten das USB-C-Kabel. Der Rückdeckel rastet
ein und trägt eine Schwalbenschwanz-Nut; der Halter — an der Wand oder
in einer Raumecke — hat die passende Schiene und gibt die Neigung vor.
Der Sensor wird von oben aufgeschoben.

    pip install manifold3d numpy matplotlib
    python gehaeuse.py            # STL-Dateien nach stl/
    python gehaeuse.py --bilder   # dazu Vorschaubilder nach bilder/
    python pruefen.py             # Passung, Montage und Druckbarkeit prüfen

Alle Maße in Millimetern. Was oben unter MESSEN steht, bitte vor dem
Druck am eigenen Modul nachmessen: Hi-Links Maßzeichnung des LD2460 lag
beim Entwurf nicht vor, Händler nennen 50 × 35 mm und 49,5 × 32 mm. Die
Maße des ESP32-C5-Zero stammen aus Waveshares Zeichnung
(hardware/dimensions/esp32-c5-zero-Size.pdf und -2D.dxf im Waveshare-
Repository).
"""

import math
import struct
import sys
from pathlib import Path

import numpy as np
from manifold3d import (CrossSection, FillRule, JoinType, Manifold, OpType, set_min_circular_angle,
                        set_min_circular_edge_length)

set_min_circular_angle(3.0)
set_min_circular_edge_length(0.3)

# --- MESSEN: das eigene Radarmodul ---------------------------------------------

RADAR_W = 50.0      # HLK-LD2460, lange Seite (liegt waagerecht)
RADAR_H = 35.0      # kurze Seite
RADAR_PCB = 1.6     # Platinenstärke
RADAR_FRONT = 0.0   # höchstes Bauteil auf der Antennenseite (meist nichts)
RADAR_BACK = 5.0    # höchstes Bauteil auf der Rückseite, samt Lötstellen und Kabeln

# --- ESP32-C5-Zero (Waveshare-Zeichnung) ----------------------------------------

ESP_L = 28.0        # Länge (USB-C an einem Ende)
ESP_W = 18.0
ESP_PCB = 1.6
ESP_H = 4.85        # Platinenunterseite bis Oberkante USB-C
USB_W = 9.0         # USB-C-Buchse
USB_T = ESP_H - ESP_PCB
USB_OVERHANG = 1.24 # Buchse steht über die Platinenkante (Seitenansicht der DXF)
PLUG_W = 14.0       # Öffnung für den Stecker des Kabels (Umspritzung)
PLUG_H = 8.0

# --- Entwurf --------------------------------------------------------------------

FIT = 0.3           # Spiel ums Radar, je Seite
WALL = 1.6          # Seitenwände
FRONT = 1.0         # Radarfenster: gleichmäßig, ohne Muster, ohne Metall
GAP = 1.0           # Luft zwischen Fenster und Antennen
LID_T = 2.0         # Rückdeckel
SPINE_T = 2.4       # Rücken des Deckels: trägt die Nut und den ESP
R_OUT = 10.0        # Ecken außen: Squircle (Superellipse), so groß wie ein Kreisradius
SQUIRCLE = 5.0      # Exponent der Superellipse; 2 wäre ein Kreisbogen
EDGE_R = 3.0        # Rundung der Vorderkante, am Bett unter 45° angefast
BACK_R = 0.8        # Rundung der Hinterkante (mehr nähme der Rastlippe Material)
MARGIN = 3.7        # außen minus Radartasche, je Seite
CHIN = 8.0          # Kinn: so viel mehr Rand unter dem Fenster, für den Namen

# Name auf der Front: eingeprägt im Kinn, nicht im Radarfenster
NAME = "Echolot"    # "" für keinen
NAME_CAP = 4.5      # Höhe der Großbuchstaben
NAME_DEPTH = 0.4    # Prägetiefe (zwei Schichten à 0,2 mm)
NAME_FONT = "schrift/InterDisplay-SemiBold.ttf"   # Inter, SIL Open Font License
LID_FIT = 0.15      # Spiel des Deckels, je Seite
SNAP = 0.5          # Rastnase: Rautenprofil, halbe Diagonale (ragt 0,35 mm über)
SNAP_GROOVE = 0.6   # Rille dafür im Gehäuse; 45°-Flanken, ohne Stütze druckbar

# Schwalbenschwanz: Hals an der Haltefläche, Kopf in der Nut
DT_NECK = 12.0
DT_HEAD = 15.0
DT_H = 2.4
DT_PLAY = 0.2
DT_LEN = 24.0

# Halter
TILT = 30.0         # Neigung nach unten; Hi-Link empfiehlt an der Wand 25–40°.
                    # Im Modul dieselbe Neigung einstellen (Kalibrieren → Montage).
CORNER_YAW = 45.0   # Raumecke: Blick in die Winkelhalbierende
BRACKET_W = 26.0
MIN_T = 3.0         # Abstand der Unterkante der Haltefläche von der Wand
CLEAR = 2.0         # Luft zwischen Sensor und Wand
WING_T = 3.0        # Platte an der Wand, durch die geschraubt wird
WING_L = 27.0       # Eckhalter: Länge der Platte an jeder Wand
SCREW_R = 1.9       # Schrauben 3,5 mm, Linsen- oder Halbrundkopf
HEAD_R = 3.9        # Kopf bis 7,5 mm
DRIVER_R = 18.0     # Eckhalter: Platz für den Griff des Schraubendrehers neben der anderen Wand

# --- abgeleitet -----------------------------------------------------------------

IW, IH = RADAR_W + 2 * FIT, RADAR_H + 2 * FIT          # Radartasche
W, H = IW + 2 * MARGIN, IH + 2 * MARGIN + CHIN          # außen
RADAR_Y = CHIN / 2                                      # Mitte des Radars über der Mitte
NAME_Y = (-H / 2 + RADAR_Y - IH / 2) / 2                # Mitte des Kinns
Z_RADAR_FRONT = FRONT + GAP + RADAR_FRONT
Z_RADAR_BACK = Z_RADAR_FRONT + RADAR_PCB
Z_POCKET_END = Z_RADAR_BACK + 1.0                       # ab hier weiter Innenraum
Z_ESP_TOP = Z_RADAR_BACK + RADAR_BACK                   # Oberkante USB-C
Z_ESP_BOTTOM = Z_ESP_TOP + ESP_H                        # Platinenunterseite = Rücken
D = Z_ESP_BOTTOM + SPINE_T + LID_T                      # Tiefe
Z_LID_IN = D - LID_T                                    # Innenseite des Deckels
Z_USB = Z_ESP_BOTTOM - ESP_PCB - USB_T / 2              # Mitte der Buchse
CAV_W, CAV_H = W - 2 * WALL, H - 2 * WALL
STRAIGHT = W / 2 - R_OUT                                # bis hier sind Ober- und Unterkante gerade
ESP_Y0 = -CAV_H / 2 + USB_OVERHANG                      # Platinenkante am USB-Ende
SPINE_HALF = DT_HEAD / 2 + DT_PLAY + 2.0
RAIL_Y0 = -CAV_H / 2 + LID_FIT + 0.5                    # wo das untere Schienenende sitzt
# Halter, entlang der Schiene gemessen: Keil, Schrauben, Oberkante
Y_WEDGE = DT_LEN + 1.0
Y_SCREW = Y_WEDGE + HEAD_R + 1.0
Y_TOP = Y_SCREW + HEAD_R + 2.0


def squircle(w: float, h: float, r: float, n: float = SQUIRCLE, steps: int = 40) -> CrossSection:
    """Rechteck mit Superellipsen-Ecken: Die Krümmung setzt sanft ein statt
    mit einem Sprung wie beim Kreisbogen."""
    pts = []
    for cx, cy, a0 in ((1, 1, 0), (-1, 1, 90), (-1, -1, 180), (1, -1, 270)):
        ox, oy = cx * (w / 2 - r), cy * (h / 2 - r)
        for k in range(steps + 1):
            t = math.radians(a0 + 90 * k / steps)
            c, s = math.cos(t), math.sin(t)
            pts.append((ox + r * math.copysign(abs(c) ** (2 / n), c), oy + r * math.copysign(abs(s) ** (2 / n), s)))
    return CrossSection([pts])


def outline(inset: float = 0.0) -> CrossSection:
    """Der Umriss des Gehäuses, um `inset` nach innen versetzt."""
    cs = squircle(W, H, R_OUT)
    return cs.offset(-inset, JoinType.Round) if inset > 0 else cs


def name_outline() -> CrossSection:
    """Der Name als Fläche, wie er von vorn zu lesen ist, mittig im Kinn.
    In Gehäusekoordinaten blickt man von -z: rechts ist dort -x, deshalb
    gespiegelt."""
    if not NAME:
        return CrossSection()
    from matplotlib.font_manager import FontProperties
    from matplotlib.textpath import TextPath

    font = FontProperties(fname=str(Path(__file__).parent / NAME_FONT))
    cap = TextPath((0, 0), "H", size=100, prop=font).get_extents().height
    path = TextPath((0, 0), NAME, size=100 * NAME_CAP / cap, prop=font)
    rings = [np.asarray(ring)[:-1] for ring in path.to_polygons(closed_only=True)]
    cs = CrossSection([r.tolist() for r in rings if len(r) >= 3], FillRule.EvenOdd)
    x0, y0, x1, y1 = cs.bounds()
    cap_mid = NAME_CAP / 2
    return cs.translate((-(x0 + x1) / 2, -cap_mid)).scale((-1, 1)).translate((0, NAME_Y))


def name_inlay() -> Manifold:
    """Der eingeprägte Name als eigener Körper: für den Druck in zweiter
    Farbe, genau in die Prägung."""
    return Manifold.extrude(name_outline(), NAME_DEPTH) if NAME else Manifold()


def slab(cs: CrossSection, z0: float, z1: float) -> Manifold:
    return Manifold.extrude(cs, z1 - z0).translate((0, 0, z0))


def box(x0, x1, y0, y1, z0, z1) -> Manifold:
    return Manifold.cube((x1 - x0, y1 - y0, z1 - z0)).translate((x0, y0, z0))


def ridge_x(x0, x1, y, z, s) -> Manifold:
    """Ein Prisma entlang x mit Rautenprofil (halbe Diagonale s): 45°-Flanken."""
    diamond = CrossSection.square((s * math.sqrt(2), s * math.sqrt(2)), center=True).rotate(45)
    return Manifold.extrude(diamond, x1 - x0).rotate((0, 90, 0)).translate((x0, y, z))


def union(parts) -> Manifold:
    parts = [p for p in parts if not p.is_empty()]
    return Manifold.batch_boolean(parts, OpType.Add) if parts else Manifold()


# --- das Gehäuse (gedruckt mit dem Fenster auf dem Bett) ---------------------------


def shell_outer() -> Manifold:
    # Vorn eine Rundung, deren unteres Stück eine 45°-Fase ist: Die Front
    # liegt auf dem Bett, und eine volle Rundung hinge dort frei in der Luft.
    flat = EDGE_R * (2 - math.sqrt(2))
    layers = [slab(outline(flat), 0.0, 0.01)]
    steps = 10
    for k in range(steps + 1):
        a = math.pi / 4 + math.pi / 4 * k / steps
        inset = EDGE_R * (1 - math.sin(a))
        z = EDGE_R * (1 - math.cos(a))
        layers.append(slab(outline(inset), z, z + 0.01))
    for k in range(steps + 1):
        a = math.pi / 2 * k / steps
        inset = BACK_R * (1 - math.cos(a))
        z = D - BACK_R + BACK_R * math.sin(a)
        layers.append(slab(outline(inset), z - 0.01, z))
    return Manifold.batch_hull(layers)


def shell() -> Manifold:
    body = shell_outer()
    cut = [
        box(-IW / 2, IW / 2, RADAR_Y - IH / 2, RADAR_Y + IH / 2, FRONT, Z_POCKET_END + 0.01),
        slab(outline(WALL), Z_POCKET_END, D + 1),
        # USB-C: von hinten offener Schlitz in der unteren Wand
        box(-PLUG_W / 2, PLUG_W / 2, -H / 2 - 1, -CAV_H / 2 + 0.01, Z_USB - PLUG_H / 2, D + 1),
        # Einfahrt der Schiene von unten
        box(-(DT_HEAD / 2 + DT_PLAY + 0.4), DT_HEAD / 2 + DT_PLAY + 0.4, -H / 2 - 1, -CAV_H / 2 + 0.01,
            D - DT_H - DT_PLAY - 0.4, D + 1),
        # Hebelkerbe oben, um den Deckel zu lösen
        box(-3, 3, CAV_H / 2 - 0.01, CAV_H / 2 + 0.8, D - 1.2, D + 1),
    ]
    # Lüftung: Schlitze nur unten (Zuluft) und im Deckel (Abluft) — Front
    # und Oberseite bleiben glatt.
    z0, z1 = Z_POCKET_END + 1.0, Z_LID_IN - 1.2
    for x in (-18, -12, 12, 18):
        cut.append(box(x - 0.9, x + 0.9, -H / 2 - 1, -CAV_H / 2 + 0.01, z0, z1))
    # Rillen für die Rastnasen des Deckels
    zs = D - LID_T / 2
    for x0, x1 in ((-STRAIGHT + 1, -8), (8, STRAIGHT - 1)):
        for y in (CAV_H / 2, -CAV_H / 2):
            cut.append(ridge_x(x0, x1, y, zs, SNAP_GROOVE))
    if NAME:
        cut.append(Manifold.extrude(name_outline(), NAME_DEPTH + 1).translate((0, 0, -1)))
    body = body - union(cut)
    # Auflagen fürs Radar in den Ecken der Tasche, außerhalb der Antennen
    pads = []
    for sx in (-1, 1):
        for sy in (-1, 1):
            x0, x1 = sorted((sx * IW / 2, sx * (IW / 2 - 3.0)))
            y0, y1 = sorted((RADAR_Y + sy * IH / 2, RADAR_Y + sy * (IH / 2 - 3.0)))
            pads.append(box(x0, x1, y0, y1, FRONT - 0.01, Z_RADAR_FRONT))
    return union([body] + pads) if Z_RADAR_FRONT > FRONT + 0.05 else body


# --- der Rückdeckel (gedruckt mit der Außenseite auf dem Bett) --------------------------
#
# Modelliert in Gehäusekoordinaten und für den Druck umgedreht.


def rail_profile() -> CrossSection:
    """Querschnitt der Schiene: x quer, y aus der Haltefläche heraus (Hals
    ab -1 in der Fläche verankert, Kopf bei DT_H)."""
    neck, head = DT_NECK / 2, DT_HEAD / 2
    return CrossSection([[(-neck, -1.0), (neck, -1.0), (neck, 0), (head, DT_H), (-head, DT_H), (-neck, 0)]])


def groove() -> Manifold:
    """Die Nut, von der Außenseite her: unten offen, oben ein Anschlag.
    Ihr Profil ist das der Schiene, rundum DT_PLAY größer — auch an den
    schrägen Flanken, nicht nur in der Breite."""
    profile = rail_profile().offset(DT_PLAY, JoinType.Miter)
    y0 = -CAV_H / 2 - 1
    y1 = RAIL_Y0 + DT_LEN
    # Profil in x / Tiefe ab Außenseite; gedreht liegt die Tiefe auf -z und
    # die Länge auf +y. In Gehäusekoordinaten: z = D - Tiefe.
    return Manifold.extrude(profile, y1 - y0).rotate((-90, 0, 0)).translate((0, y0, D))


def lid() -> Manifold:
    plate = slab(outline(WALL + LID_FIT), Z_LID_IN, D)
    spine = box(-SPINE_HALF, SPINE_HALF, -CAV_H / 2 + LID_FIT, CAV_H / 2 - LID_FIT, Z_ESP_BOTTOM, Z_LID_IN + 0.01)
    parts = [plate, spine]
    # Rastnasen
    for x0, x1 in ((-STRAIGHT + 2, -9), (9, STRAIGHT - 2)):
        for y in (CAV_H / 2 - LID_FIT, -(CAV_H / 2 - LID_FIT)):
            parts.append(ridge_x(x0, x1, y, D - LID_T / 2, SNAP))
    # Stützen, die das Radar in den Ecken gegen die Auflagen drücken
    for sx in (-1, 1):
        for sy in (-1, 1):
            cx, cy = sx * (IW / 2 - 1.6), RADAR_Y + sy * (IH / 2 - 1.6)
            parts.append(box(cx - 1.4, cx + 1.4, cy - 1.4, cy + 1.4, Z_RADAR_BACK, Z_LID_IN + 0.01))
    # Führung für den ESP am Antennenende: Anschlag und zwei Seitenstücke.
    # Dort liegen die ersten Lötpads 6 mm vom Rand (Zeichnung) — die
    # Führung bleibt 4 mm kurz. Am USB-Ende hält ihn die Wand, quer ein
    # Streifen doppelseitiges Klebeband auf dem Rücken.
    ex, y1 = ESP_W / 2 + 0.2, ESP_Y0 + ESP_L + 0.2
    z0, z1 = Z_ESP_BOTTOM - 1.2, Z_ESP_BOTTOM + 0.01
    parts.append(box(-ex - 1.2, ex + 1.2, y1, y1 + 1.2, z0, z1))
    for sx in (-1, 1):
        xa, xb = sorted((sx * ex, sx * (ex + 1.2)))
        parts.append(box(xa, xb, y1 - 4.0, y1 + 0.01, z0, z1))
    body = union(parts) - groove()
    # Lüftung im Deckel, neben dem Rücken: lange Schlitze, oben Abluft
    vents = [box(x - 0.9, x + 0.9, -CAV_H / 2 + 6, CAV_H / 2 - 7, Z_LID_IN - 1, D + 1) for x in (-21, -17, 17, 21)]
    return body - union(vents)


# --- die Halter --------------------------------------------------------------------------
#
# Raum: X an der Wand entlang, Y von der Wand in den Raum, Z nach oben; die
# Wand ist Y = 0, beim Eckhalter auch X = 0. Rahmen der Schiene: x' quer
# (vom Raum aus gesehen nach rechts), y' an der Schiene entlang nach oben,
# z' aus der Haltefläche in den Raum; y' = 0 ist das untere Schienenende.
# Alles am Halter bleibt innerhalb des Sensorumrisses: von vorn verdeckt.

SENSOR_Y0 = -H / 2 - RAIL_Y0                            # Sensor im Schienenrahmen
SENSOR_Y1 = H / 2 - RAIL_Y0


def frame(yaw: float, tilt: float):
    """Achsen des Sensorrückens im Raum: ex quer, ey nach oben entlang des
    Rückens, f vom Sensor in den Raum."""
    t, a = math.radians(tilt), math.radians(yaw)
    f = np.array([math.cos(t) * math.sin(a), math.cos(t) * math.cos(a), -math.sin(t)])
    ey = np.array([math.sin(t) * math.sin(a), math.sin(t) * math.cos(a), math.cos(t)])
    ex = np.cross(ey, f)
    return ex, ey, f


def place(part: Manifold, ex, ey, f, origin) -> Manifold:
    """Schienenrahmen -> Raum."""
    return part.transform(np.column_stack([ex, ey, f, origin]).tolist())


def sensor_to_rail(part: Manifold) -> Manifold:
    """Gehäuse -> Schienenrahmen: um y gedreht, damit die Front nach +z'
    zeigt; der Rücken bei z' = 0, das untere Schienenende bei y' = 0."""
    return part.transform([[-1, 0, 0, 0], [0, 1, 0, -RAIL_Y0], [0, 0, -1, D]])


def rail() -> Manifold:
    """Die Schiene im Schienenrahmen, y' von 0 bis DT_LEN."""
    # extrudiert entlang z; um x gedreht: Profil-y -> z', Länge -> -y'
    return Manifold.extrude(rail_profile(), DT_LEN).rotate((90, 0, 0)).translate((0, DT_LEN, 0))


def in_slab(m: Manifold, n, lo: float, hi: float, o) -> Manifold:
    """Der Teil von m mit lo ≤ n·(p - o) ≤ hi."""
    return m.trim_by_plane(tuple(n), float(n @ o + lo)).trim_by_plane(tuple(-n), float(-(n @ o) - hi))


def bracket_layout(corner: bool) -> dict:
    """Wo der Halter sitzt: Rahmen, Ursprung (unteres Schienenende in der
    Haltefläche) und die Schraubenpunkte auf der Wand."""
    yaw = CORNER_YAW if corner else 0.0
    ex, ey, f = frame(yaw, TILT)
    walls = (0, 1) if corner else (1,)
    direction = np.array([math.sin(math.radians(yaw)), math.cos(math.radians(yaw)), 0.0])
    need = 0.0
    # So weit von Wand oder Ecke, dass die Haltefläche unten MIN_T vor der
    # Wand endet und der Sensor mit allen Ecken CLEAR Luft hat.
    spots = [(xs * ex, MIN_T) for xs in (-BRACKET_W / 2, BRACKET_W / 2)]
    spots += [(xs * ex + ys * ey, CLEAR) for xs in (-W / 2, W / 2) for ys in (SENSOR_Y0, SENSOR_Y1)]
    for p, gap in spots:
        for axis in walls:
            if direction[axis] > 1e-9:
                need = max(need, (gap - p[axis]) / direction[axis])
    origin = need * direction
    screws = []
    if corner:
        # je Wand eine, so weit aus der Ecke, dass der Griff neben die andere Wand passt
        along = WING_L - HEAD_R - 2.5
        screws = [(np.array([along, 0.0, 0.0]), 1), (np.array([0.0, along, 0.0]), 0)]
    else:
        screws = [(np.array([xs * (BRACKET_W / 2 - HEAD_R - 1.5), 0.0, 0.0]), 1) for xs in (-1, 1)]
    for p, _ in screws:
        # über dem Keil: Mitte bei y' = Y_SCREW
        p[2] = (Y_SCREW + ey @ origin - ey[0] * p[0] - ey[1] * p[1]) / ey[2]
    return {"yaw": yaw, "ex": ex, "ey": ey, "f": f, "origin": origin, "walls": walls, "screws": screws}


def bracket(corner: bool) -> tuple[Manifold, dict]:
    """Ein Keil, der die Schiene trägt, und eine Platte an jeder Wand, die
    über den Keil hinausreicht: Dort sitzen die Schrauben, frei für den
    Schraubendreher und hinter dem Sensor verborgen."""
    lay = bracket_layout(corner)
    ex, ey, f, o = lay["ex"], lay["ey"], lay["f"], lay["origin"]
    big = Manifold.cube((400, 400, 400), center=True).translate(tuple(o))
    big = big.trim_by_plane((0, 1, 0), 0.0)
    if corner:
        big = big.trim_by_plane((1, 0, 0), 0.0)
    wedge = in_slab(in_slab(big, ex, -BRACKET_W / 2, BRACKET_W / 2, o), ey, 0.0, Y_WEDGE, o)
    if corner:
        plates = [box(0, WING_L, 0, WING_T, -200, 200), box(0, WING_T, 0, WING_L, -200, 200)]
    else:
        plates = [box(-BRACKET_W / 2, BRACKET_W / 2, 0, WING_T, -200, 200)]
    plates = in_slab(union(plates), ey, 0.0, Y_TOP, o)
    body = union([wedge, plates]).trim_by_plane(tuple(-f), float(-f @ o))
    body = union([body, place(rail(), ex, ey, f, o)])
    up = print_up(corner)
    return body - union([screw_hole(p, axis, up) for p, axis in lay["screws"]]), lay


def print_up(corner: bool):
    """Die Richtung im Raum, die beim Druck nach oben zeigt."""
    return frame(CORNER_YAW, TILT)[1] if corner else np.array([0.0, 1.0, 0.0])


def screw_hole(p, axis: int, up) -> Manifold:
    """Loch senkrecht in die Wand `axis` (0: X = 0, 1: Y = 0) am Wandpunkt p,
    durch die Platte. Liegt es beim Druck quer, bekommt es eine 45°-Spitze
    nach oben (Tropfenform) und braucht keine Stütze."""
    a = np.zeros(3)
    a[axis] = 1.0
    t = up - (up @ a) * a
    if np.linalg.norm(t) < 0.3:
        cs = CrossSection.circle(SCREW_R)
        t = np.array([0.0, 0.0, 1.0])
    else:
        tip = CrossSection.square((0.01, 0.01), center=True).translate((0, SCREW_R * math.sqrt(2)))
        cs = CrossSection.batch_hull([CrossSection.circle(SCREW_R), tip])
    t = t / np.linalg.norm(t)
    e1 = np.cross(t, a)
    start = np.asarray(p, dtype=float) - a
    return Manifold.extrude(cs, WING_T + 2.0).transform(np.column_stack([e1, t, a, start]).tolist())


# --- Ausgabe -------------------------------------------------------------------------------


def write_stl(part: Manifold, path: Path, name: str) -> None:
    mesh = part.to_mesh()
    v = np.asarray(mesh.vert_properties)[:, :3]
    t = np.asarray(mesh.tri_verts)
    a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
    n = np.cross(b - a, c - a)
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    with open(path, "wb") as out:
        out.write(name.encode()[:80].ljust(80, b" "))
        out.write(struct.pack("<I", len(t)))
        data = np.zeros(len(t), dtype=[("n", "<f4", 3), ("a", "<f4", 3), ("b", "<f4", 3), ("c", "<f4", 3), ("x", "<u2")])
        data["n"], data["a"], data["b"], data["c"] = n, a, b, c
        out.write(data.tobytes())


def to_bed(part: Manifold) -> Manifold:
    lo = part.bounding_box()[:3]
    return part.translate((-lo[0], -lo[1], -lo[2]))


def print_parts() -> dict[str, Manifold]:
    """Jedes Teil in der Lage, in der es gedruckt wird, auf z = 0."""
    s = shell()
    lid_print = lid().rotate((180, 0, 0))                     # Außenseite unten
    wall, _ = bracket(corner=False)
    wall_print = wall.rotate((90, 0, 0))                       # Wandseite unten
    corner, lay = bracket(corner=True)
    # auf der Unterseite, die Schiene senkrecht: Zeilen f, ex, ey
    turn = np.vstack([lay["f"], lay["ex"], lay["ey"]])
    corner_print = corner.transform(np.column_stack([turn, np.zeros(3)]).tolist())
    probe = s.trim_by_plane((0, 0, -1), -(Z_POCKET_END + 0.6))
    lo = s.bounding_box()[:3]
    parts = {
        "1_gehaeuse": to_bed(s),
        "2_rueckdeckel": to_bed(lid_print),
        "3_wandhalter": to_bed(wall_print),
        "4_eckhalter": to_bed(corner_print),
        "0_passprobe_radar": to_bed(probe),
    }
    if NAME:
        # an derselben Stelle wie im Gehäuse: zusammen laden, zweite Farbe zuweisen
        parts["5_schriftzug_einlage"] = name_inlay().translate((-lo[0], -lo[1], -lo[2]))
    return parts


def main(argv) -> int:
    out = Path(__file__).parent / "stl"
    out.mkdir(exist_ok=True)
    for name, part in print_parts().items():
        write_stl(part, out / f"{name}.stl", f"echolot {name}")
        x0, y0, z0, x1, y1, z1 = part.bounding_box()
        print(f"{name:20s} {x1 - x0:5.1f} × {y1 - y0:5.1f} × {z1 - z0:5.1f} mm  {part.volume() / 1000:5.1f} cm³")
    print(f"Gehäuse außen {W:.1f} × {H:.1f} × {D:.1f} mm")
    if "--bilder" in argv:
        import vorschau
        vorschau.render_all(Path(__file__).parent / "bilder")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
