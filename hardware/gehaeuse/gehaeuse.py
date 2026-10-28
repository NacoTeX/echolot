"""Echolot-Sensorgehäuse: HLK-LD2460 + Waveshare ESP32-C5-Zero.

Drei gedruckte Teile, zwei Schrauben:

* die **Schale**: eine schlanke Kachel mit Squircle-Ecken und weichen
  Kanten. Vorn das Radar hinter einem gleichmäßig dünnen Fenster, darunter
  im „Kinn“ der eingeprägte Name. Ihre Rückseite liegt schräg: Die Schale
  selbst ist der Neigungskeil, es gibt keine Schiene und keinen Halter;
* die **Wandplatte**: flach an der Wand, von der Schale verdeckt. Sie trägt
  den ESP in einer Tasche und zwei Rippen, auf die die Schale geschoben
  wird;
* zwei **Schrauben** (M2) durch die Seitenwände der Schale in diese Rippen.

In der Raumecke kommt ein **Eckkeil** zwischen Wand und Platte.

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

Koordinaten: Die **Schale** liegt in ihrem eigenen Rahmen, x quer, y nach
oben, z von der Front (z = 0, das Fenster) nach hinten. Die **Platte** hat
einen eigenen Rahmen (a, b, h): a quer, b an der Wand nach oben, h aus der
Wand in den Raum; h = 0 ist die Fläche, auf der die Schale aufliegt.
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

# --- Entwurf: die Schale --------------------------------------------------------

FIT = 0.3           # Spiel ums Radar, je Seite
WALL = 1.6          # Seitenwände
FRONT = 1.0         # Radarfenster: gleichmäßig, ohne Muster, ohne Metall
GAP = 1.0           # Luft zwischen Fenster und Antennen
R_OUT = 10.0        # Ecken außen: Squircle (Superellipse), so groß wie ein Kreisradius
SQUIRCLE = 5.0      # Exponent der Superellipse; 2 wäre ein Kreisbogen
EDGE_R = 3.0        # Rundung der Vorderkante, am Bett unter 45° angefast
MARGIN = 3.7        # außen minus Radartasche, je Seite
CHIN = 8.0          # Kinn: so viel mehr Rand unter dem Fenster, für den Namen

# Name auf der Front: eingeprägt im Kinn, nicht im Radarfenster
NAME = "Echolot"    # "" für keinen
NAME_CAP = 4.5      # Höhe der Großbuchstaben
NAME_DEPTH = 0.4    # Prägetiefe (zwei Schichten à 0,2 mm)
NAME_FONT = "schrift/InterDisplay-SemiBold.ttf"   # Inter, SIL Open Font License

# --- Neigung und Rückseite --------------------------------------------------------

TILT = 30.0         # Neigung nach unten; Hi-Link empfiehlt an der Wand 25–40°.
                    # Im Modul dieselbe Neigung einstellen (Kalibrieren → Montage).
CORNER_YAW = 45.0   # Raumecke: Blick in die Winkelhalbierende
Z_B0 = 12.0         # Tiefe der Schale an ihrer Unterkante; oben ist sie um H·tan(TILT) tiefer

# --- Wandplatte ------------------------------------------------------------------

PLATE_T = 3.5       # Plattenstärke
WS_A = 17.0         # die zwei Wandschrauben: Abstand von der Mitte
WS_B = 30.0         # und Höhe auf der Platte
WS_R = 1.8          # Loch für Senkkopfschrauben 3,5 mm
CSK_R = 3.7         # Kopfradius (90°-Senkung)

# ESP: liegt in einer Tasche auf der Platte, USB-C unten
ESP_B0 = 12.0       # Platinenkante am USB-Ende, gemessen ab Unterkante der Platte
ESP_SO = 1.2        # Abstand der Platine zur Platte
ESP_FIT = 0.2       # Spiel der Tasche
GUIDE_T = 1.6       # Wände der Tasche
STOP_L = 2.0        # Anschläge an den Enden

# Seitenschrauben: M2 durch die Seitenwand der Schale in eine Rippe der Platte
RIB_T = 1.65        # Rippe
RIB_GAP = 0.15      # Spiel zwischen Rippe und Innenwand
RIB_L = 18.0        # Länge entlang der Wand
RIB_H = 13.0        # Höhe über der Platte
SS_B = 30.0         # Mitte der Seitenschraube, ab Unterkante der Platte
SS_H = 9.0          # und Höhe über der Platte
SS_R = 1.2          # Loch in der Schale (M2 mit Spiel)
PILOT_R = 0.85      # Kernloch in der Rippe für die selbstschneidende M2
PILOT_L = 3.8
CB_R = 2.3          # Senkung für den Kopf (M2, Linsenkopf 3,8 mm)
CB_D = 0.8
PILLAR = 3.5        # halbe Kantenlänge des Pfeilers um die Schraube (verdickt die Rippe)

# Eckkeil
CORNER_D = 33.0     # Abstand der Plattenfläche von der Ecke
AW_R = 2.0          # Loch für die Schrauben in die Wand
AW_CB = 3.9         # Senkung für den Kopf
AW_X = 22.0         # Abstand der Schraube aus der Ecke
AW_Z = (14.0, 44.0) # Höhen
DRIVER_R = 18.0     # Platz für den Griff des Schraubendrehers neben der anderen Wand
CP_R = 1.4          # Kernloch im Keil für die Plattenschrauben
CP_L = 12.0

# --- abgeleitet -----------------------------------------------------------------

IW, IH = RADAR_W + 2 * FIT, RADAR_H + 2 * FIT          # Radartasche
W, H = IW + 2 * MARGIN, IH + 2 * MARGIN + CHIN          # außen
RADAR_Y = CHIN / 2                                      # Mitte des Radars über der Mitte
NAME_Y = (-H / 2 + RADAR_Y - IH / 2) / 2                # Mitte des Kinns
Z_RADAR_FRONT = FRONT + GAP + RADAR_FRONT
Z_RADAR_BACK = Z_RADAR_FRONT + RADAR_PCB
Z_POCKET_END = Z_RADAR_BACK + 1.0                       # ab hier weiter Innenraum
Z_BEHIND = Z_RADAR_BACK + RADAR_BACK                    # bis hier Bauteile und Kabel hinter dem Radar
CAV_W, CAV_H = W - 2 * WALL, H - 2 * WALL
STRAIGHT = W / 2 - R_OUT
C, S = math.cos(math.radians(TILT)), math.sin(math.radians(TILT))
Z_MAX = Z_B0 + H * S / C + 12.0                         # weit hinter der Rückseite
PLATE_H = H / C                                         # Höhe der Platte
ESP_Y0 = ESP_B0                                         # (in Plattenkoordinaten)
NOTCH_H = PLUG_H + 0.8                                  # Aussparung für den Stecker, über der Platte
H_USB_MID = ESP_SO + ESP_PCB + USB_T / 2                # Mitte der Buchse über der Platte
D_TOP = Z_B0 + H * S / C                                # Tiefe der Schale an der Oberkante


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
    In Schalenkoordinaten blickt man von -z: rechts ist dort -x, deshalb
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


def union(parts) -> Manifold:
    parts = [p for p in parts if not p.is_empty()]
    return Manifold.batch_boolean(parts, OpType.Add) if parts else Manifold()


def drop_hole(r: float, length: float, start, axis, up) -> Manifold:
    """Ein Loch vom Punkt `start` in Richtung `axis`. Liegt es beim Druck
    quer, bekommt es eine 45°-Spitze nach `up` (Tropfenform) und braucht
    keine Stütze; steht es senkrecht, bleibt es rund."""
    axis = np.asarray(axis, float)
    axis = axis / np.linalg.norm(axis)
    tip_dir = np.asarray(up, float) - (np.asarray(up, float) @ axis) * axis
    if np.linalg.norm(tip_dir) < 0.3:
        cs = CrossSection.circle(r)
        tip_dir = np.cross(axis, [1.0, 0.0, 0.0] if abs(axis[0]) < 0.9 else [0.0, 1.0, 0.0])
    else:
        tip = CrossSection.square((0.01, 0.01), center=True).translate((0, r * math.sqrt(2)))
        cs = CrossSection.batch_hull([CrossSection.circle(r), tip])
    tip_dir = tip_dir / np.linalg.norm(tip_dir)
    e1 = np.cross(tip_dir, axis)
    return Manifold.extrude(cs, length).transform(
        np.column_stack([e1, tip_dir, axis, np.asarray(start, float)]).tolist())


# --- Rahmen: Platte <-> Schale <-> Raum ----------------------------------------------
#
# Die Schale liegt mit der Front unten (z nach hinten). Ihre Rückseite ist die
# Ebene durch P0 mit der Normalen N (zeigt in den Raum). Die Platte hat die
# Achsen A (quer), B (an der Wand nach oben), N (in den Raum); A × B = N.

N_PLATE = np.array([0.0, S, -C])        # in Schalenkoordinaten
A_PLATE = np.array([-1.0, 0.0, 0.0])
B_PLATE = np.array([0.0, C, S])
P0 = np.array([0.0, -H / 2, Z_B0])      # unten, Mitte, auf der Rückseite


def pose(a, b, n, origin) -> np.ndarray:
    """4×4: Plattenkoordinaten -> der Rahmen mit den Achsen a, b, n."""
    m = np.eye(4)
    m[:3, :3] = np.column_stack([a, b, n])
    m[:3, 3] = origin
    return m


def apply(part: Manifold, m: np.ndarray) -> Manifold:
    return part.transform(m[:3].tolist())


PLATE_TO_SHELL = pose(A_PLATE, B_PLATE, N_PLATE, P0)
SHELL_TO_PLATE = np.linalg.inv(PLATE_TO_SHELL)


def plate_pose(corner: bool) -> np.ndarray:
    """Plattenkoordinaten -> Raum. Wand: X entlang der Wand, Y in den Raum,
    Z nach oben; die Wand ist Y = 0 (Ecke: auch X = 0)."""
    up = np.array([0.0, 0.0, 1.0])
    yaw = math.radians(CORNER_YAW if corner else 0.0)
    n = np.array([math.sin(yaw), math.cos(yaw), 0.0])
    a = np.cross(up, n)
    dist = CORNER_D if corner else 0.0
    return pose(a, up, n, (dist + PLATE_T) * n)


def apply_point(m: np.ndarray, p) -> np.ndarray:
    return (m @ np.append(np.asarray(p, float), 1.0))[:3]


def shell_to_room(corner: bool) -> np.ndarray:
    return plate_pose(corner) @ SHELL_TO_PLATE


# --- die Schale (gedruckt mit dem Fenster auf dem Bett) ---------------------------


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
    body = union([Manifold.batch_hull(layers), slab(outline(), EDGE_R + 0.005, Z_MAX)])
    # hinten schräg abgeschnitten: dort liegt die Platte an
    return body.trim_by_plane(tuple(N_PLATE), float(N_PLATE @ P0))


def plate_box(a0, a1, b0, b1, h0, h1) -> Manifold:
    return box(a0, a1, b0, b1, h0, h1)


def shell() -> Manifold:
    body = shell_outer()
    cut = [
        box(-IW / 2, IW / 2, RADAR_Y - IH / 2, RADAR_Y + IH / 2, FRONT, Z_POCKET_END + 0.01),
        slab(outline(WALL), Z_POCKET_END, Z_MAX + 1),
        # USB-C-Stecker: unten offen, von der Rückseite her
        apply(plate_box(-PLUG_W / 2, PLUG_W / 2, -20.0, 6.0, -1.0, NOTCH_H), PLATE_TO_SHELL),
    ]
    # Seitenschrauben: Loch durch die Seitenwand, außen eine Senkung für den Kopf
    p = (PLATE_TO_SHELL @ np.array([0.0, SS_B, SS_H, 1.0]))[:3]
    for side in (-1, 1):
        start = np.array([side * (W / 2 + 1.0), p[1], p[2]])
        cut.append(drop_hole(SS_R, 5.0, start, [-side, 0, 0], [0, 0, 1]))
        cut.append(drop_hole(CB_R, CB_D + 1.0, start, [-side, 0, 0], [0, 0, 1]))
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


# --- die Wandplatte (gedruckt mit der Wandseite auf dem Bett) ------------------------
#
# In Plattenkoordinaten; h = 0 ist die Fläche, auf der die Schale aufliegt.


def footprint() -> CrossSection:
    """Der Umriss der Schalenrückseite, auf die Platte projiziert."""
    return outline().scale((1.0, 1.0 / C)).translate((0.0, H / 2 / C))


def esp_pocket() -> Manifold:
    """Die Tasche für den ESP: vier Auflagen, vier Wandstücke, zwei Anschläge.
    Die Platine liegt mit den Bauteilen nach oben; USB-C zeigt nach unten."""
    parts = []
    top = ESP_SO + ESP_PCB + 0.8                      # Wände ragen etwas über die Platine
    half = ESP_W / 2 + ESP_FIT
    # Auflagen unter den Ecken der Platine
    for sa in (-1, 1):
        for b0, b1 in ((ESP_B0 + 0.6, ESP_B0 + 3.6), (ESP_B0 + ESP_L - 3.6, ESP_B0 + ESP_L - 0.6)):
            a0, a1 = sorted((sa * 5.5, sa * (ESP_W / 2 - 0.5)))
            parts.append(plate_box(a0, a1, b0, b1, -0.01, ESP_SO))
    # Wandstücke an den Längskanten: in der Mitte frei für Lötstellen und Kabel
    for sa in (-1, 1):
        a0, a1 = sorted((sa * half, sa * (half + GUIDE_T)))
        for b0, b1 in ((ESP_B0 + 2.0, ESP_B0 + 8.0), (ESP_B0 + ESP_L - 8.0, ESP_B0 + ESP_L - 2.0)):
            parts.append(plate_box(a0, a1, b0, b1, -0.01, top))
        # Anschlag unten, neben der Buchse (der Stecker braucht 12,5 mm Breite)
        a2, a3 = sorted((sa * 6.9, sa * (half + GUIDE_T)))
        parts.append(plate_box(a2, a3, ESP_B0 - ESP_FIT - STOP_L, ESP_B0 - ESP_FIT, -0.01, top))
        # Anschlag oben, die Mitte (Antenne) bleibt frei
        a4, a5 = sorted((sa * 5.5, sa * (half + GUIDE_T)))
        parts.append(plate_box(a4, a5, ESP_B0 + ESP_L + ESP_FIT, ESP_B0 + ESP_L + ESP_FIT + STOP_L, -0.01, top))
    return union(parts)


def ribs() -> Manifold:
    """Zwei Rippen, auf die die Schale geschoben wird, mit je einem Pfeiler
    um das Kernloch für die Seitenschraube."""
    parts = []
    inner = CAV_W / 2 - RIB_GAP - RIB_T
    for sa in (-1, 1):
        a0, a1 = sorted((sa * inner, sa * (inner + RIB_T)))
        parts.append(plate_box(a0, a1, SS_B - RIB_L / 2, SS_B + RIB_L / 2, -0.01, RIB_H))
        p0, p1 = sorted((sa * (inner - 3.0), sa * (inner + RIB_T)))
        parts.append(plate_box(p0, p1, SS_B - PILLAR, SS_B + PILLAR, -0.01, SS_H + PILLAR))
    return union(parts)


def plate() -> Manifold:
    base = Manifold.extrude(footprint(), PLATE_T).translate((0, 0, -PLATE_T))
    body = union([base, esp_pocket(), ribs()])
    cuts = []
    for sa in (-1, 1):
        # Senkloch für die Wandschraube
        x, y = sa * WS_A, WS_B
        cuts.append(Manifold.cylinder(PLATE_T + 2.0, WS_R).translate((x, y, -PLATE_T - 1.0)))
        cone = Manifold.cylinder(CSK_R - WS_R + 0.6, WS_R, CSK_R + 0.6).translate((x, y, -(CSK_R - WS_R)))
        cuts.append(cone)
        # Kernloch für die Seitenschraube, von der Innenseite der Rippe nach außen
        inner = CAV_W / 2 - RIB_GAP - RIB_T
        start = np.array([sa * (inner + RIB_T + 0.5), SS_B, SS_H])
        cuts.append(drop_hole(PILOT_R, PILOT_L + 0.5, start, [-sa, 0, 0], [0, 0, 1]))
    return body - union(cuts)


def corner_prism() -> Manifold:
    """Der Eckkeil ohne Löcher, im Raumrahmen: ein Dreieckprisma zwischen den
    Wänden, mit der Platte auf seiner Schrägfläche."""
    yaw = math.radians(CORNER_YAW)
    tri = CrossSection([[(0.0, 0.0), (CORNER_D / math.sin(yaw), 0.0), (0.0, CORNER_D / math.cos(yaw))]])
    return Manifold.extrude(tri, PLATE_H)


def corner_wedge() -> Manifold:
    """Der Eckkeil mit den Schraubenlöchern."""
    yaw = math.radians(CORNER_YAW)
    sy, cy = math.sin(yaw), math.cos(yaw)
    prism = corner_prism()
    cuts = []
    # Schrauben in die Wände: je eine, senkrecht zur Wand, Kopf unter der Schrägfläche
    for wall, z in ((0, AW_Z[0]), (1, AW_Z[1])):
        along = np.zeros(3)
        along[wall] = -1.0                       # in die Wand hinein
        other = 1 - wall
        pos = np.zeros(3)
        pos[wall] = AW_X
        # Punkt auf der Schrägfläche: x·sin + y·cos = D
        s_, c_ = (sy, cy) if wall == 0 else (cy, sy)
        pos[other] = (CORNER_D - AW_X * s_) / c_
        pos[2] = z
        start = pos + (-along) * 8.0              # außerhalb der Fläche beginnen
        cuts.append(drop_hole(AW_R, 8.0 + AW_X + 1.0, start, along, [0, 0, 1]))
        cs = pos + (-along) * 8.0
        cuts.append(drop_hole(AW_CB, 8.0 + 7.0, cs, along, [0, 0, 1]))
    # Kernlöcher für die Plattenschrauben, senkrecht in die Schrägfläche
    m = plate_pose(True)
    for sa in (-1, 1):
        cuts.append(apply(Manifold.cylinder(CP_L + 1.0, CP_R).translate((sa * WS_A, WS_B, -PLATE_T - CP_L)), m))
    return prism - union(cuts)


def tolerance_strip() -> Manifold:
    """Ein kleiner Streifen, um das Spiel des eigenen Druckers zu finden,
    bevor die großen Teile gedruckt werden: fünf Schlitze für eine Rippe und
    fünf Kernlöcher für die M2."""
    deltas = (0.0, 0.1, 0.15, 0.2, 0.3)
    pitch = 11.0
    length = pitch * len(deltas) + 4.0
    strip = box(0, length, 0, 2 * 13.0 + 3.0, 0, 3.0)
    cuts = []
    for i, d in enumerate(deltas):
        x = 2.0 + i * pitch
        cuts.append(box(x, x + RIB_T + d, 3.0, 13.0, -1, 4))
        r = (1.5 + 0.1 * i) / 2
        cuts.append(Manifold.cylinder(5.0, r).translate((x + 2.5, 21.0, -1)))
    return strip - union(cuts)


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


def wedge_print_frame() -> np.ndarray:
    """Raum -> Druckbett für den Eckkeil: die Schrägfläche liegt unten."""
    m = plate_pose(True)
    a, b, n = m[:3, 0], m[:3, 1], m[:3, 2]
    r = np.eye(4)
    r[:3, :3] = np.vstack([b, a, -n])
    return r


def print_parts() -> dict[str, Manifold]:
    """Jedes Teil in der Lage, in der es gedruckt wird, auf z = 0."""
    s = shell()
    probe = s.trim_by_plane((0, 0, -1), -(Z_POCKET_END + 0.6))
    lo = s.bounding_box()[:3]
    parts = {
        "0_passprobe_radar": to_bed(probe),
        "0_toleranzstreifen": to_bed(tolerance_strip()),
        "1_schale": to_bed(s),
        "2_wandplatte": to_bed(plate().translate((0, 0, 0))),
        "3_eckkeil": to_bed(apply(corner_wedge(), wedge_print_frame())),
    }
    if NAME:
        # an derselben Stelle wie in der Schale: zusammen laden, zweite Farbe zuweisen
        parts["4_schriftzug_einlage"] = name_inlay().translate((-lo[0], -lo[1], -lo[2]))
    return parts


def main(argv) -> int:
    out = Path(__file__).parent / "stl"
    out.mkdir(exist_ok=True)
    for name, part in print_parts().items():
        write_stl(part, out / f"{name}.stl", f"echolot {name}")
        x0, y0, z0, x1, y1, z1 = part.bounding_box()
        print(f"{name:22s} {x1 - x0:5.1f} × {y1 - y0:5.1f} × {z1 - z0:5.1f} mm  {part.volume() / 1000:5.1f} cm³")
    print(f"Schale außen {W:.1f} × {H:.1f} mm, unten {Z_B0:.1f} mm tief, oben {D_TOP:.1f} mm")
    if "--bilder" in argv:
        import vorschau
        vorschau.render_all(Path(__file__).parent / "bilder")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
