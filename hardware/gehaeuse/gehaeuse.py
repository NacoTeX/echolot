"""Echolot-Sensorgehäuse: HLK-LD2460 + Waveshare ESP32-C5-Zero.

Zwei gedruckte Teile, vier Schrauben, ein Winkel für die Ecke:

* das **Gehäuse**: eine schlanke Kachel mit Squircle-Ecken und weichen
  Kanten. Vorn das Radar hinter einem gleichmäßig dünnen Fenster, darunter
  im „Kinn“ der eingeprägte Name. Ihre Rückseite liegt schräg: Das Gehäuse
  selbst ist der Neigungskeil;
* der **Deckel**: flach an der Wand, vom Gehäuse verdeckt. Er trägt den ESP
  und zwei Rippen mit Schraubdomen, auf die das Gehäuse geschoben wird;
* **vier Schrauben**: zwei Senkkopfschrauben halten den Deckel an der Wand,
  zwei Blechschrauben (2,9 mm) durch die Seitenwände halten das Gehäuse am
  Deckel;
* in der Raumecke ein **Eckkeil** zwischen Wand und Deckel.

    pip install manifold3d numpy matplotlib
    python gehaeuse.py            # STL-Dateien nach stl/
    python gehaeuse.py --bilder   # dazu Vorschaubilder nach bilder/
    python pruefen.py             # Passung, Montage und Druckbarkeit prüfen

Alle Maße in Millimetern. Quellen der Maße:

* **LD2460:** Hi-Link, *HLK-LD2460 Manual V1.1*, Abschnitt 3.1 (Maßzeichnung:
  Platine 49,5 × 32,0 mm, Stiftleisten im 2,0-mm-Raster) und Abschnitt 3.2
  (Anschlüsse). **Keine Bohrungen am Modul.** Nicht im Handbuch stehen die
  Platinenstärke und die Höhen der Bauteile: Sie sind unten als Annahme
  gekennzeichnet und vor dem Druck mit dem Messschieber zu prüfen.
* **ESP32-C5-Zero:** Waveshare, `hardware/dimensions/esp32-c5-zero-2D.dxf`
  (Platine 18,0 × 28,0 mm, 1,6 mm, USB-C bis 4,85 mm über der Rückseite,
  Lötaugen im Raster 2,54 mm an beiden Längskanten).

Koordinaten: Das **Gehäuse** liegt in seinem eigenen Rahmen, x quer, y nach
oben, z von der Front (z = 0, das Fenster) nach hinten. Der **Deckel** hat
einen eigenen Rahmen (a, b, h): a quer, b an der Wand nach oben, h aus der
Wand in den Raum; h = 0 ist die Fläche, auf der das Gehäuse aufliegt.
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

# --- LD2460: Hi-Link, Handbuch V1.1, Abschnitt 3 ---------------------------------

RADAR_W = 49.5      # lange Seite (liegt waagerecht)
RADAR_H = 32.0      # kurze Seite
RADAR_R = 1.8       # Eckenradius der Platine (aus der Zeichnung)
# Annahmen, nicht im Handbuch: vor dem Druck nachmessen
RADAR_PCB = 1.6     # Platinenstärke
RADAR_FRONT = 1.0   # höchstes Bauteil auf der Antennenseite: zwei kleine Chips (Foto), QFN etwa 1 mm
RADAR_BACK = 9.0    # Stiftleisten (2,0 mm Raster) samt aufgesteckter Buchse
RADAR_LOW = 4.0     # alles andere hinter dem Radar (MCU, Kleinteile)

# Lage der beiden Stiftleisten, aus der Maßzeichnung (Rückansicht, u von der
# linken, v von der unteren Kante des Hochformats). J1: 2 × 4 Pins, ab 12,22 mm
# von links; JP103: 2 × 2 Pins, ab 13,69 mm. Im Gehäuse liegt die lange Seite
# waagerecht, die Platine ist von hinten gesehen um 90° im Uhrzeigersinn gedreht.
J1_UV = ((10.5, 19.5), (43.5, 49.5))
JP103_UV = ((12.7, 16.7), (0.0, 6.0))

# --- ESP32-C5-Zero: Waveshare, DXF -------------------------------------------------

ESP_L = 28.0        # Länge (USB-C an einem Ende)
ESP_W = 18.0
ESP_PCB = 1.6
ESP_H = 4.85        # Platinenunterseite bis Oberkante USB-C
USB_W = 9.0         # USB-C-Buchse
USB_T = ESP_H - ESP_PCB
USB_OVERHANG = 1.24 # Buchse steht über die Platinenkante (Seitenansicht der DXF)
PAD_FIRST = 1.59    # erstes Lötauge, vom USB-Ende
PAD_PITCH = 2.54    # Raster der Lötaugen; neun je Längskante
UFL_A = -4.08       # U.FL-Buchse am anderen Ende, quer zur Mitte
PLUG_W = 14.0       # Öffnung für den Stecker des Kabels (Umspritzung)
PLUG_H = 8.0

# --- Entwurf: das Gehäuse ---------------------------------------------------------

FIT_R = 0.2         # Spiel ums Radar, je Seite; die Quetschrippen nehmen es wieder weg
CRUSH = 0.15        # Übermaß der Quetschrippen, je Seite
RIB_W = 1.2         # Breite einer Quetschrippe
WALL = 2.0          # Seitenwände
FRONT = 1.0         # Radarfenster: gleichmäßig, ohne Muster, ohne Metall
GAP = 0.6           # Luft zwischen Bauteilen auf der Antennenseite und dem Fenster
R_OUT = 10.0        # Ecken außen: Squircle (Superellipse), so groß wie ein Kreisradius
SQUIRCLE = 5.0      # Exponent der Superellipse; 2 wäre ein Kreisbogen
EDGE_R = 3.0        # Rundung der Vorderkante, am Bett unter 45° angefast
MARGIN = 3.5        # außen minus Radartasche, je Seite
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
Z_B0 = 14.0         # Tiefe des Gehäuses an seiner Unterkante; oben ist es um H·tan(TILT) tiefer

# --- Deckel ------------------------------------------------------------------------

PLATE_T = 3.5       # Stärke
WS_A = 17.0         # die zwei Wandschrauben: Abstand von der Mitte
WS_B = 20.0         # und Höhe auf dem Deckel
WS_R = 1.8          # Loch für Senkkopfschrauben 3,5 mm
CSK_R = 3.7         # Kopfradius (90°-Senkung)

# ESP: liegt in einer Tasche auf dem Deckel, USB-C unten
ESP_B0 = 15.0       # Platinenkante am USB-Ende, gemessen ab Unterkante des Deckels
ESP_SO = 1.5        # Abstand der Platine zum Deckel (Lötstellen der Lötaugen)
ESP_GAP = 0.9       # Abstand der Taschenwände zur Platinenkante (Lötstellen an den Lötaugen)
ESP_CRUSH = 0.15    # Übermaß der Quetschrippen
ESP_RIBS = (1, 3, 5, 7)   # zwischen welchen Lötaugen sie stehen
GUIDE_T = 1.6       # Wände der Tasche
STOP_L = 2.0        # Anschläge an den Enden
STOP_H = 1.0        # und wie hoch sie über die Unterseite der Platine reichen
ARM_L = 4.0         # Haltearme über den Ecken am Antennenende
ARM_OVER = 1.6      # so weit greifen sie über die Platine
ARM_GAP = 0.4       # Luft zwischen Arm und Platine; beim Einsetzen kippt die Platine um 3°
ARM_T = 0.9         # Stärke der Arme

# Seitenschrauben: Blechschrauben 2,9 mm durch die Seitenwand in einen Dom des Deckels
RIB_T = 2.0         # Rippe
RIB_GAP = 0.15      # Spiel zwischen Rippe und Innenwand
RIB_B0, RIB_B1 = 24.0, 36.0   # Länge der Rippe auf dem Deckel: innerhalb des geraden Stücks der Innenwand
RIB_H = 9.5         # Höhe über dem Deckel
SS_B = 32.0         # Mitte der Seitenschraube, ab Unterkante des Deckels
SS_H = 6.0          # und Höhe über dem Deckel
SS_R = 1.6          # Loch in der Wand (2,9 mit Spiel)
PILOT_R = 1.2       # Kernloch im Dom
PILOT_L = 8.0
CB_R = 3.0          # Senkung für den Kopf (Linsenkopf 5,5 mm)
CB_D = 1.0
DOM_B = 3.5         # halbe Breite des Doms
DOM_L = 9.5         # Dicke des Doms, von der Rippe nach innen

# Eckkeil
CORNER_D = 33.0     # Abstand der Deckelfläche von der Ecke
AW_R = 2.0          # Loch für die Schrauben in die Wand
AW_CB = 3.9         # Senkung für den Kopf
AW_X = 22.0         # Abstand der Schraube aus der Ecke
AW_Z = (14.0, 44.0) # Höhen
DRIVER_R = 18.0     # Platz für den Griff des Schraubendrehers neben der anderen Wand
CP_R = 1.4          # Kernloch im Keil für die Deckelschrauben
CP_L = 12.0

# --- abgeleitet -----------------------------------------------------------------

IW, IH = RADAR_W + 2 * FIT_R, RADAR_H + 2 * FIT_R       # Radartasche
W, H = IW + 2 * MARGIN, IH + 2 * MARGIN + CHIN          # außen
RADAR_Y = CHIN / 2                                      # Mitte des Radars über der Mitte
NAME_Y = (-H / 2 + EDGE_R + RADAR_Y - IH / 2) / 2         # Mitte des Kinns, bis wo die Front noch eben ist
Z_SEAT = FRONT + RADAR_FRONT + GAP                      # Auflage: Vorderseite der Platine
Z_RADAR_FRONT = Z_SEAT
Z_RADAR_BACK = Z_SEAT + RADAR_PCB
Z_POCKET_END = Z_RADAR_BACK + 1.0                       # ab hier weiter Innenraum
CAV_W, CAV_H = W - 2 * WALL, H - 2 * WALL
STRAIGHT = W / 2 - R_OUT
C, S = math.cos(math.radians(TILT)), math.sin(math.radians(TILT))
Z_MAX = Z_B0 + H * S / C + 12.0                         # weit hinter der Rückseite
PLATE_H = H / C                                         # Höhe des Deckels
NOTCH_H = PLUG_H + 0.8                                  # Aussparung für den Stecker, über dem Deckel
H_USB_MID = ESP_SO + ESP_PCB + USB_T / 2                # Mitte der Buchse über dem Deckel
D_TOP = Z_B0 + H * S / C                                # Tiefe des Gehäuses an der Oberkante
ESP_SIDE = ESP_W / 2 + ESP_GAP                          # Innenkante der Taschenwand


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


# --- Rahmen: Deckel <-> Gehäuse <-> Raum ---------------------------------------------
#
# Das Gehäuse liegt mit der Front unten (z nach hinten). Seine Rückseite ist die
# Ebene durch P0 mit der Normalen N (zeigt in den Raum). Der Deckel hat die
# Achsen A (quer), B (an der Wand nach oben), N (in den Raum); A × B = N.

N_PLATE = np.array([0.0, S, -C])        # in Gehäusekoordinaten
A_PLATE = np.array([-1.0, 0.0, 0.0])
B_PLATE = np.array([0.0, C, S])
P0 = np.array([0.0, -H / 2, Z_B0])      # unten, Mitte, auf der Rückseite


def pose(a, b, n, origin) -> np.ndarray:
    """4×4: Deckelkoordinaten -> der Rahmen mit den Achsen a, b, n."""
    m = np.eye(4)
    m[:3, :3] = np.column_stack([a, b, n])
    m[:3, 3] = origin
    return m


def apply(part: Manifold, m: np.ndarray) -> Manifold:
    return part.transform(m[:3].tolist())


def apply_point(m: np.ndarray, p) -> np.ndarray:
    return (m @ np.append(np.asarray(p, float), 1.0))[:3]


PLATE_TO_SHELL = pose(A_PLATE, B_PLATE, N_PLATE, P0)
SHELL_TO_PLATE = np.linalg.inv(PLATE_TO_SHELL)


def plate_pose(corner: bool) -> np.ndarray:
    """Deckelkoordinaten -> Raum. Wand: X entlang der Wand, Y in den Raum,
    Z nach oben; die Wand ist Y = 0 (Ecke: auch X = 0)."""
    up = np.array([0.0, 0.0, 1.0])
    yaw = math.radians(CORNER_YAW if corner else 0.0)
    n = np.array([math.sin(yaw), math.cos(yaw), 0.0])
    a = np.cross(up, n)
    dist = CORNER_D if corner else 0.0
    return pose(a, up, n, (dist + PLATE_T) * n)


def shell_to_room(corner: bool) -> np.ndarray:
    return plate_pose(corner) @ SHELL_TO_PLATE


# --- das Radar im Gehäuse ---------------------------------------------------------------


def radar_xy(u: float, v: float) -> tuple[float, float]:
    """Ein Punkt der Maßzeichnung (u von links, v von unten, Rückansicht,
    Hochformat) -> x, y im Gehäuse: von hinten um 90° im Uhrzeigersinn gedreht."""
    return v - RADAR_W / 2, RADAR_Y - (u - RADAR_H / 2)


def radar_box(w: float = RADAR_W, h: float = RADAR_H, pcb: float = RADAR_PCB) -> Manifold:
    """Die Platine, so groß man will, mittig in der Tasche."""
    return box(-w / 2, w / 2, RADAR_Y - h / 2, RADAR_Y + h / 2, Z_RADAR_FRONT, Z_RADAR_FRONT + pcb)


def radar_zones() -> dict[str, Manifold]:
    """Was hinter der Platine Platz braucht, in Gehäusekoordinaten: die
    Stiftleisten samt Buchsen hoch, der Rest niedrig."""
    tall = []
    for (u0, u1), (v0, v1) in (J1_UV, JP103_UV):
        xa, ya = radar_xy(u0, v0)
        xb, yb = radar_xy(u1, v1)
        x0, x1 = max(min(xa, xb) - 0.5, -RADAR_W / 2 + 0.2), min(max(xa, xb) + 0.5, RADAR_W / 2 - 0.2)
        tall.append(box(x0, x1, min(ya, yb) - 0.5, max(ya, yb) + 0.5, Z_RADAR_BACK, Z_RADAR_BACK + RADAR_BACK))
    low = box(-RADAR_W / 2 + 0.2, RADAR_W / 2 - 0.2, RADAR_Y - RADAR_H / 2 + 0.2, RADAR_Y + RADAR_H / 2 - 0.2,
              Z_RADAR_BACK, Z_RADAR_BACK + RADAR_LOW)
    return {"tall": union(tall), "low": low}


def crush_ribs() -> list[tuple[Manifold, str]]:
    """Quetschrippen an den vier Taschenwänden: Sie nehmen das Spiel und
    drücken die Platine mit CRUSH je Seite. Das Radar sitzt dadurch fest, ohne
    Bohrungen und ohne Kleber. Zwischen den Rippen und vorn liegt sie auf den
    Auflagen. Die Rippen stehen nicht an den Stiftleisten."""
    p = FIT_R + CRUSH
    z0, z1 = Z_RADAR_FRONT, Z_POCKET_END
    out = []

    def rib(axis: str, side: int, pos: float) -> Manifold:
        # Rippe an der Wand axis=side, mittig bei pos entlang der Wand; hinten abgeschrägt (45°)
        main = box(0, p, -RIB_W / 2, RIB_W / 2, z0, z1 - p)
        wedge = Manifold.batch_hull([box(0, p, -RIB_W / 2, RIB_W / 2, z1 - p - 0.01, z1 - p),
                                     box(0, 0.01, -RIB_W / 2, RIB_W / 2, z1 - 0.01, z1)])
        r = union([main, wedge])        # steht bei x = 0 an der Wand und ragt nach +x
        if axis == "x":                 # Wand bei x = ±IW/2
            return r.translate((-IW / 2, pos, 0)) if side < 0 else r.rotate((0, 0, 180)).translate((IW / 2, pos, 0))
        r = r.rotate((0, 0, 90 if side < 0 else -90))
        return r.translate((pos, RADAR_Y + side * IH / 2, 0))

    for sy in (-1, 1):                  # lange Kanten (oben, unten)
        for x in (-14.0, 0.0, 14.0):
            out.append((rib("y", sy, x), f"y{'+' if sy > 0 else '-'}{x:+.0f}"))
    for sx in (-1, 1):                  # kurze Kanten (links, rechts)
        for dy in (-12.0, 12.0):
            out.append((rib("x", sx, RADAR_Y + dy), f"x{'+' if sx > 0 else '-'}{dy:+.0f}"))
    return out


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
    body = union([Manifold.batch_hull(layers), slab(outline(), EDGE_R + 0.005, Z_MAX)])
    # hinten schräg abgeschnitten: dort liegt der Deckel an
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
    p = apply_point(PLATE_TO_SHELL, [0.0, SS_B, SS_H])
    for side in (-1, 1):
        start = np.array([side * (W / 2 + 1.0), p[1], p[2]])
        cut.append(drop_hole(SS_R, WALL + 2.0, start, [-side, 0, 0], [0, 0, 1]))
        cut.append(drop_hole(CB_R, CB_D + 1.0, start, [-side, 0, 0], [0, 0, 1]))
    if NAME:
        cut.append(Manifold.extrude(name_outline(), NAME_DEPTH + 1).translate((0, 0, -1)))
    body = body - union(cut)
    # Auflagen fürs Radar in den Ecken der Tasche, auf der Antennenseite frei von Bauteilen
    pads = []
    for sx in (-1, 1):
        for sy in (-1, 1):
            x0, x1 = sorted((sx * IW / 2, sx * (IW / 2 - 3.0)))
            y0, y1 = sorted((RADAR_Y + sy * IH / 2, RADAR_Y + sy * (IH / 2 - 3.0)))
            pads.append(box(x0, x1, y0, y1, FRONT - 0.01, Z_SEAT))
    return union([body, union(pads)] + [r for r, _ in crush_ribs()])


# --- der Deckel (gedruckt mit der Wandseite auf dem Bett) ----------------------------
#
# In Deckelkoordinaten; h = 0 ist die Fläche, auf der das Gehäuse aufliegt.


def footprint() -> CrossSection:
    """Der Umriss der Gehäuserückseite, auf den Deckel projiziert."""
    return outline().scale((1.0, 1.0 / C)).translate((0.0, H / 2 / C))


def esp_pads_b() -> list[float]:
    """Mitten zwischen den Lötaugen, vom USB-Ende der Platine gerechnet."""
    return [PAD_FIRST + PAD_PITCH / 2 + PAD_PITCH * k for k in ESP_RIBS]


def esp_arms() -> Manifold:
    """Die Arme über den Ecken am Antennenende, auf Wand und Anschlag abgestützt."""
    parts = []
    end = ESP_B0 + ESP_L
    ah0 = ESP_SO + ESP_PCB + ARM_GAP
    for sa in (-1, 1):
        a6, a7 = sorted((sa * (ESP_W / 2 - ARM_OVER), sa * (ESP_SIDE + 0.5)))
        parts.append(plate_box(a6, a7, end - ARM_L, end + 0.1 + STOP_L, ah0, ah0 + ARM_T))
        parts.append(plate_box(*sorted((sa * ESP_SIDE, sa * (ESP_SIDE + GUIDE_T))), end - ARM_L, end + 0.1,
                               -0.01, ah0 + ARM_T))
    return union(parts)


def esp_pocket(with_ribs: bool = True) -> Manifold:
    """Die Tasche für den ESP. Er liegt mit den Bauteilen nach oben, USB-C unten.
    - vier Auflagen unter den Enden, frei von den Lötaugen;
    - Wände an den Längskanten, 0,9 mm von der Kante (Lötstellen);
    - Quetschrippen zwischen den Lötaugen drücken die Platine mit CRUSH je Seite;
    - zwei Anschläge unten neben der Buchse, zwei oben, die Mitte bleibt frei;
    - zwei Arme über den Ecken am Antennenende halten die Platine nach unten:
      Sie wird schräg darunter geschoben und dann am USB-Ende abgesenkt."""
    parts = []
    top = ESP_SO + ESP_PCB + 1.2                       # Wände ragen über die Platine
    end = ESP_B0 + ESP_L
    # Auflagen unter den Enden der Platine, in der Mitte zwischen Kante und Buchse
    for sa in (-1, 1):
        for b0, b1 in ((ESP_B0 + 0.05, ESP_B0 + 0.95), (end - 3.0, end - 0.5)):
            a0, a1 = sorted((sa * 5.0, sa * 8.2))
            parts.append(plate_box(a0, a1, b0, b1, -0.01, ESP_SO))
    for sa in (-1, 1):
        a0, a1 = sorted((sa * ESP_SIDE, sa * (ESP_SIDE + GUIDE_T)))
        parts.append(plate_box(a0, a1, ESP_B0 - 0.1, end + 0.1, -0.01, top))
        # Quetschrippen: stehen von der Wand bis CRUSH in die Platinenbreite
        r0, r1 = sorted((sa * (ESP_W / 2 - ESP_CRUSH), sa * ESP_SIDE))
        for g in esp_pads_b() if with_ribs else []:
            parts.append(plate_box(r0, r1, ESP_B0 + g - 0.4, ESP_B0 + g + 0.4, -0.01, ESP_SO + ESP_PCB + 0.4))
        # Anschlag unten, neben der Buchse (der Stecker braucht 12,5 mm Breite)
        a2, a3 = sorted((sa * 6.9, sa * (ESP_SIDE + GUIDE_T)))
        parts.append(plate_box(a2, a3, ESP_B0 - 0.1 - STOP_L, ESP_B0 - 0.1, -0.01, ESP_SO + STOP_H))
        # Anschlag oben, die Mitte (U.FL) bleibt frei
        a4, a5 = sorted((sa * 5.5, sa * (ESP_SIDE + GUIDE_T)))
        parts.append(plate_box(a4, a5, end + 0.1, end + 0.1 + STOP_L, -0.01, top))
    parts.append(esp_arms())
    return union(parts)


def ribs() -> Manifold:
    """Zwei Rippen, auf die das Gehäuse geschoben wird, mit je einem Dom um
    das Kernloch für die Seitenschraube."""
    parts = []
    inner = CAV_W / 2 - RIB_GAP - RIB_T
    for sa in (-1, 1):
        a0, a1 = sorted((sa * inner, sa * (inner + RIB_T)))
        parts.append(plate_box(a0, a1, RIB_B0, RIB_B1, -0.01, RIB_H))
        p0, p1 = sorted((sa * (inner + RIB_T - DOM_L), sa * (inner + RIB_T)))
        parts.append(plate_box(p0, p1, SS_B - DOM_B, SS_B + DOM_B, -0.01, SS_H + DOM_B))
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
        # Kernloch für die Seitenschraube, von der Außenseite der Rippe nach innen
        outer = CAV_W / 2 - RIB_GAP
        start = np.array([sa * (outer + 0.5), SS_B, SS_H])
        cuts.append(drop_hole(PILOT_R, PILOT_L + 0.5, start, [-sa, 0, 0], [0, 0, 1]))
    return body - union(cuts)


def corner_prism() -> Manifold:
    """Der Eckkeil ohne Löcher, im Raumrahmen: ein Dreieckprisma zwischen den
    Wänden, mit dem Deckel auf seiner Schrägfläche."""
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
        cuts.append(drop_hole(AW_CB, 8.0 + 7.0, start, along, [0, 0, 1]))
    # Kernlöcher für die Deckelschrauben, senkrecht in die Schrägfläche
    m = plate_pose(True)
    for sa in (-1, 1):
        cuts.append(apply(Manifold.cylinder(CP_L + 1.0, CP_R).translate((sa * WS_A, WS_B, -PLATE_T - CP_L)), m))
    return prism - union(cuts)


def tolerance_strip() -> Manifold:
    """Ein kleiner Streifen, um das Spiel des eigenen Druckers zu finden,
    bevor die großen Teile gedruckt werden: fünf Schlitze (die Rippe des
    Deckels ist 2,0 mm dick), fünf Kernlöcher für die Blechschraube und eine
    Quetschrippe zum Messen."""
    deltas = (0.0, 0.1, 0.15, 0.2, 0.3)
    pitch = 11.0
    length = pitch * len(deltas) + 4.0
    strip = box(0, length, 0, 2 * 13.0 + 3.0, 0, 3.0)
    cuts = []
    for i, d in enumerate(deltas):
        x = 2.0 + i * pitch
        cuts.append(box(x, x + RIB_T + d, 3.0, 13.0, -1, 4))
        r = (2.2 + 0.1 * i) / 2
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
        "1_gehaeuse": to_bed(s),
        "2_deckel": to_bed(plate()),
        "3_eckkeil": to_bed(apply(corner_wedge(), wedge_print_frame())),
    }
    if NAME:
        # an derselben Stelle wie im Gehäuse: zusammen laden, zweite Farbe zuweisen
        parts["4_schriftzug_einlage"] = name_inlay().translate((-lo[0], -lo[1], -lo[2]))
    return parts


def main(argv) -> int:
    out = Path(__file__).parent / "stl"
    out.mkdir(exist_ok=True)
    for old in out.glob("*.stl"):
        old.unlink()
    for name, part in print_parts().items():
        write_stl(part, out / f"{name}.stl", f"echolot {name}")
        x0, y0, z0, x1, y1, z1 = part.bounding_box()
        print(f"{name:22s} {x1 - x0:5.1f} × {y1 - y0:5.1f} × {z1 - z0:5.1f} mm  {part.volume() / 1000:5.1f} cm³")
    print(f"Gehäuse außen {W:.1f} × {H:.1f} mm, unten {Z_B0:.1f} mm tief, oben {D_TOP:.1f} mm")
    if "--bilder" in argv:
        import vorschau
        vorschau.render_all(Path(__file__).parent / "bilder")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
