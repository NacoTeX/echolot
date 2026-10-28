"""Prüft das Gehäuse, bevor jemand Filament verbraucht.

    python pruefen.py

- Jedes Teil ist ein geschlossener, zusammenhängender Körper.
- Zusammengebaut: Radar, ESP und USB-Stecker stoßen nirgends an, der
  Stecker hat seine Aussparung, die Schale sitzt auf der Platte, und die
  Löcher für die Seitenschrauben treffen die Rippen.
- Montiert: Die Schale lässt sich über die ganze Strecke auf die Rippen
  schieben; die Wandschrauben sind vor der Schale frei erreichbar; in der
  Ecke berührt nichts die Wände, und die Schrauben haben Platz für den
  Schraubendreher.
- Druck: in der Lage, in der gedruckt wird, nichts steiler als 50°
  überhängend, Brücken höchstens 20 mm weit.

Endet mit Code 1, wenn etwas nicht stimmt.
"""

import math
import sys

import numpy as np
from manifold3d import Manifold

import gehaeuse as g

OVERHANG_MAX = 50.0   # Grad gegen die Senkrechte
BRIDGE_MAX = 20.0     # mm

problems: list[str] = []


def check(ok: bool, text: str) -> None:
    print(("ok    " if ok else "FEHLT ") + text)
    if not ok:
        problems.append(text)


def overlap(a: Manifold, b: Manifold) -> float:
    return (a ^ b).volume()


def vertices(part: Manifold) -> np.ndarray:
    return np.asarray(part.to_mesh().vert_properties)[:, :3]


def parts_valid() -> None:
    for name, part in g.print_parts().items():
        pieces = len(part.decompose())
        single = pieces == 1 or name == "4_schriftzug_einlage"   # einzelne Buchstaben
        check(part.status().name == "NoError" and part.volume() > 0 and single,
              f"{name}: geschlossen, {pieces} Körper, {part.volume() / 1000:.1f} cm³")


def look() -> None:
    """Name und Wände: der Name im Kinn, nicht im Radarfenster; die Einlage
    füllt die Prägung genau; nirgends zu dünne Wände."""
    shell = g.shell()
    outer = g.shell_outer()
    # seitlich gemessen: ohne die Front (das Fenster ist gewollt 1 mm)
    outside = g.box(-100, 100, -100, 100, 0.0, g.Z_B0 - 0.01) - outer
    pocket = g.box(-g.IW / 2, g.IW / 2, g.RADAR_Y - g.IH / 2, g.RADAR_Y + g.IH / 2, g.FRONT, g.Z_POCKET_END)
    wall = pocket.min_gap(outside, 5.0)
    check(wall >= 1.2, f"Wand um die Radartasche mindestens {wall:.2f} mm")
    cavity = g.slab(g.outline(g.WALL), g.Z_POCKET_END + 0.5, g.Z_B0 - 0.5)
    rear = cavity.min_gap(outside, 5.0)
    check(rear >= g.WALL - 0.05, f"Wand um den hinteren Raum mindestens {rear:.2f} mm")
    window = shell.trim_by_plane((0, 0, 1), 0.0).trim_by_plane((0, 0, -1), -g.FRONT)
    pocket_win = g.box(-g.IW / 2, g.IW / 2, g.RADAR_Y - g.IH / 2, g.RADAR_Y + g.IH / 2, 0, g.FRONT)
    check(abs(overlap(window, pocket_win) - pocket_win.volume()) < 1.0,
          f"Radarfenster überall {g.FRONT:.1f} mm massiv")
    if not g.NAME:
        return
    inlay = g.name_inlay()
    x0, y0, _, x1, y1, _ = inlay.bounding_box()
    window_y = g.RADAR_Y - g.IH / 2
    check(y1 <= window_y - 1.0, f"Name liegt im Kinn, {window_y - y1:.1f} mm unter dem Radarfenster")
    flat = g.outline(g.EDGE_R)                       # wo die Front noch eben ist
    fx0, fy0, fx1, fy1 = flat.bounds()
    check(x0 >= fx0 + 1 and x1 <= fx1 - 1 and y0 >= fy0 + 0.5,
          f"Name auf der ebenen Front ({x1 - x0:.1f} × {y1 - y0:.1f} mm)")
    check(overlap(inlay, shell) < 1e-3 and abs((g.shell_outer() ^ inlay).volume() - inlay.volume()) < 1e-3,
          f"Einlage füllt die Prägung genau ({inlay.volume():.1f} mm³)")


def dummies() -> dict[str, Manifold]:
    """Was ins Gehäuse kommt: das Radar in Schalenkoordinaten, ESP und
    Stecker in Plattenkoordinaten."""
    esp_top = g.ESP_SO + g.ESP_PCB
    return {
        "radar": g.box(-g.RADAR_W / 2, g.RADAR_W / 2, g.RADAR_Y - g.RADAR_H / 2, g.RADAR_Y + g.RADAR_H / 2,
                       g.Z_RADAR_FRONT, g.Z_RADAR_BACK),
        # Bauteile, Lötstellen und Kabel hinter dem Radar
        "behind": g.box(-g.RADAR_W / 2, g.RADAR_W / 2, g.RADAR_Y - g.RADAR_H / 2, g.RADAR_Y + g.RADAR_H / 2,
                        g.Z_RADAR_BACK, g.Z_BEHIND),
        "esp": g.box(-g.ESP_W / 2, g.ESP_W / 2, g.ESP_B0, g.ESP_B0 + g.ESP_L, g.ESP_SO, esp_top),
        "usb": g.box(-g.USB_W / 2, g.USB_W / 2, g.ESP_B0 - g.USB_OVERHANG, g.ESP_B0 + 7.3, esp_top, g.ESP_SO + g.ESP_H),
        # ein üblicher Stecker: Umspritzung 12,5 × 6,5 mm, bis an die Buchse
        "plug": g.box(-6.25, 6.25, -40.0, g.ESP_B0 - g.USB_OVERHANG - 0.05,
                      g.H_USB_MID - 3.25, g.H_USB_MID + 3.25),
    }


def in_shell(part: Manifold) -> Manifold:
    """Plattenkoordinaten -> Schalenkoordinaten."""
    return g.apply(part, g.PLATE_TO_SHELL)


def assembly() -> None:
    shell = g.shell()
    plate = in_shell(g.plate())
    d = dummies()
    radar = d["radar"]
    esp, usb, plug = (in_shell(d[k]) for k in ("esp", "usb", "plug"))
    check(overlap(radar, shell) < 1e-3, "Radar passt in die Tasche")
    side = radar.trim_by_plane((0, 0, 1), g.Z_RADAR_FRONT + 0.5)   # über den Auflagen: nur seitlich
    play = side.min_gap(shell, 1.0)
    check(play > g.FIT - 0.05, f"Spiel ums Radar {play:.2f} mm")
    pads = shell.trim_by_plane((0, 0, 1), g.FRONT + 0.01).trim_by_plane((0, 0, -1), -g.Z_RADAR_FRONT + 0.01)
    check(radar.min_gap(pads, 1.0) < 0.02, "Auflagen halten das Radar mit Abstand zum Fenster")
    behind = d["behind"]
    check(overlap(behind, shell) < 1e-3 and overlap(behind, plate) < 1e-3,
          f"Platz hinter dem Radar ({g.RADAR_BACK:.0f} mm) frei")
    ribs = in_shell(g.ribs())
    rest = in_shell(g.union([g.esp_pocket(), g.Manifold.extrude(g.footprint(), g.PLATE_T).translate((0, 0, -g.PLATE_T))]))
    gap = behind.min_gap(rest, 100.0)
    check(gap >= 5.0, f"Platte liegt {gap:.0f} mm hinter dem Radar: Platz für die Kabel")
    gap = behind.min_gap(ribs, 100.0)
    check(gap >= 2.0, f"Rippen halten {gap:.1f} mm Abstand zum Bauraum hinter dem Radar")
    check(overlap(esp, plate) < 1e-3 and overlap(esp, shell) < 1e-3, "ESP stößt nirgends an")
    check(overlap(usb, plate) < 1e-3 and overlap(usb, shell) < 1e-3, "USB-Buchse stößt nirgends an")
    check(esp.min_gap(behind, 20.0) >= 1.0 and usb.min_gap(behind, 20.0) >= 1.0,
          f"ESP hält Abstand zum Radar ({min(esp.min_gap(behind, 20.0), usb.min_gap(behind, 20.0)):.1f} mm)")
    pocket = g.esp_pocket()
    esp_p = d["esp"]
    walls_only = pocket.trim_by_plane((0, 0, 1), g.ESP_SO + 0.05)       # ohne die Auflagen
    check(esp_p.min_gap(walls_only, 1.0) > g.ESP_FIT - 0.05, f"ESP hat in der Tasche {esp_p.min_gap(walls_only, 1.0):.2f} mm Spiel")
    rest = esp_p.translate((0, 0, -0.05))
    check(overlap(rest, pocket) > 0, "ESP liegt auf den Auflagen")
    check(overlap(plug, shell) < 1e-3 and overlap(plug, plate) < 1e-3, "USB-Stecker passt durch die Aussparung")
    check(overlap(shell, plate) < 1e-3, "Schale sitzt auf der Platte, ohne Spannung")
    seat = shell.min_gap(plate, 1.0)
    check(seat < 0.02, f"Schale liegt flach auf der Platte auf ({seat:.3f} mm)")
    walls = ribs.min_gap(shell, 2.0)
    check(walls >= g.RIB_GAP - 0.05, f"Rippen haben {walls:.2f} mm Spiel in der Schale")
    # Seitenschraube: eine Sonde vom Außenrand durch das Loch in die Kernbohrung
    p = (g.PLATE_TO_SHELL @ np.array([0.0, g.SS_B, g.SS_H, 1.0]))[:3]
    inner = g.CAV_W / 2 - g.RIB_GAP - g.RIB_T
    for side_ in (-1, 1):
        r = 0.8
        start = np.array([side_ * 40.0, p[1], p[2]])
        end_x = side_ * (inner + g.RIB_T - g.PILOT_L + 0.2)
        probe = g.Manifold.cylinder(abs(start[0] - end_x), r).rotate((0, 90, 0)).translate((0, p[1], p[2]))
        probe = probe.translate((min(start[0], end_x), 0, 0))
        check(overlap(probe, shell) < 1e-3 and overlap(probe, plate) < 1e-3,
              f"Seitenschraube {'links' if side_ < 0 else 'rechts'}: Loch und Kernbohrung fluchten")
    # Fleisch um die Kernbohrung
    for sa in (-1, 1):
        a_in = sa * (inner - 3.0)
        a_out = sa * (inner + g.RIB_T)
        thick = abs(a_out - a_in) - g.PILOT_L
        check(thick >= 0.5, f"Seitenschraube {'links' if sa < 0 else 'rechts'}: Pfeiler hinter der Kernbohrung {thick:.1f} mm")
    # Wandschrauben: vor dem Aufsetzen der Schale frei erreichbar
    plate_p = g.plate()
    for k, sa in enumerate((-1, 1), 1):
        x, y = sa * g.WS_A, g.WS_B
        driver = Manifold.cylinder(80.0, g.CSK_R + 0.3).translate((x, y, 0.2))
        check(overlap(driver, plate_p) < 1e-3, f"Wandschraube {k}: frei erreichbar, Kopf bis {g.CSK_R:.1f} mm Radius")
    # die Schale lässt sich entlang der Rippen aufschieben
    n = g.N_PLATE
    hits = [k for k in np.arange(0.0, 45.0, 1.0) if overlap(shell.translate(tuple(k * n)), plate) > 1e-3]
    check(not hits, "Schale lässt sich entlang der Rippen aufschieben"
          + (f" (stößt bei {hits[0]:.0f} mm)" if hits else ""))


def cylinder_along(start, direction, r: float, length: float) -> Manifold:
    """Ein Zylinder vom Punkt `start` in Richtung `direction` (Achse x, y oder z, mit Vorzeichen)."""
    d = np.asarray(direction, float)
    c = Manifold.cylinder(length, r)
    if abs(d[0]) == 1.0:
        c = c.rotate((0, 90 * d[0], 0))
    elif abs(d[1]) == 1.0:
        c = c.rotate((-90 * d[1], 0, 0))
    elif d[2] < 0:
        c = c.rotate((180, 0, 0))
    return c.translate(tuple(np.asarray(start, float)))


def room_part(part: Manifold, corner: bool) -> Manifold:
    return g.apply(part, g.shell_to_room(corner))


def mount() -> None:
    shell = g.shell()
    for corner in (False, True):
        name = "Ecke" if corner else "Wand"
        m = g.shell_to_room(corner)
        sensor_v = vertices(room_part(shell, corner))
        plate_room = g.apply(g.plate(), g.plate_pose(corner))
        pv = vertices(plate_room)
        f = -m[:3, :3] @ np.array([0.0, 0.0, 1.0])            # Blickrichtung der Front im Raum
        tilt = math.degrees(math.asin(-f[2]))
        check(abs(tilt - g.TILT) < 1e-6, f"{name}: neigt den Sensor um {tilt:.0f}° nach unten")
        yaw = math.degrees(math.atan2(f[0], f[1]))
        check(abs(yaw - (g.CORNER_YAW if corner else 0.0)) < 1e-6, f"{name}: blickt {yaw:.0f}° aus der Wandnormalen")
        walls = (0, 1) if corner else (1,)
        near = min(sensor_v[:, a].min() for a in walls)
        check(near >= g.PLATE_T - 0.01 or corner, f"{name}: Sensor mindestens {near:.1f} mm von der Wand")
        back = min(pv[:, a].min() for a in walls)
        check(back >= -0.01, f"{name}: Platte bleibt vor der Wand ({back:.2f} mm)")
        if not corner:
            check(abs(pv[:, 1].min()) < 0.01, "Wand: Plattenrückseite liegt genau an der Wand")
    wedge = g.corner_wedge()
    plate_room = g.apply(g.plate(), g.plate_pose(True))
    check(overlap(plate_room, wedge) < 1e-3, "Ecke: Platte und Eckkeil durchdringen sich nicht")
    # die Plattenrückseite liegt ganz auf der Schrägfläche des Keils
    face = g.apply(g.box(-g.W / 2, g.W / 2, 0, g.PLATE_H, -g.PLATE_T - 0.2, -g.PLATE_T + 0.0), g.plate_pose(True))
    check(face.min_gap(wedge, 1.0) < 0.01, "Ecke: Platte liegt auf dem Keil")
    base = g.apply(g.Manifold.extrude(g.footprint(), 0.5).translate((0, 0, -g.PLATE_T - 0.5)), g.plate_pose(True))
    outside = (base - g.corner_prism()).volume()
    check(outside < 1.0, f"Ecke: Keil trägt die ganze Platte ({outside:.1f} mm³ stehen über)")
    # Schrauben in die Wände: Kopf unter der Schrägfläche, Schraubendreher frei
    yaw = math.radians(g.CORNER_YAW)
    nr = np.array([math.sin(yaw), math.cos(yaw), 0.0])
    for wall, z in ((0, g.AW_Z[0]), (1, g.AW_Z[1])):
        along = np.zeros(3)
        along[wall] = -1.0                       # in die Wand hinein
        other = 1 - wall
        s_, c_ = (math.sin(yaw), math.cos(yaw)) if wall == 0 else (math.cos(yaw), math.sin(yaw))
        pos = np.zeros(3)                        # Punkt auf der Schrägfläche
        pos[wall] = g.AW_X
        pos[other] = (g.CORNER_D - g.AW_X * s_) / c_
        pos[2] = z
        label = f"Schraube in Wand {wall + 1}"
        # Kopf: 2,2 mm dick auf dem Grund der Senkung, die 7 mm hinter dem Punkt endet
        head = cylinder_along(pos + along * 7.0, -along, 3.75, 2.2)
        check(overlap(head, wedge) < 1e-3, f"Ecke: {label}: Kopf passt in die Senkung")
        top = vertices(head) @ nr
        check(top.max() <= g.CORNER_D, f"Ecke: {label}: Kopf liegt {g.CORNER_D - top.max():.1f} mm unter der Fläche")
        # Schraubendreher: Schaft von der Senkung bis weit vor die Fläche
        shank = cylinder_along(pos + along * 7.0, -along, 2.8, 70.0)
        check(overlap(shank, wedge) < 1e-3, f"Ecke: {label} frei erreichbar")
        check(pos[other] >= g.DRIVER_R, f"Ecke: {label}: Griff des Schraubendrehers passt neben die Wand ({pos[other]:.0f} mm)")
        # Fleisch bis zur Wand
        check(pos[wall] + 7.0 >= 20.0, f"Ecke: {label}: {pos[wall] + 7.0:.0f} mm Keil bis zur Wand")
    # Kernlöcher der Plattenschrauben haben Fleisch dahinter
    m = g.plate_pose(True)
    for sa in (-1, 1):
        p = m @ np.array([sa * g.WS_A, g.WS_B, -g.PLATE_T, 1.0])
        t = min(p[0], p[1]) / math.sin(yaw)       # bis zur nächsten Wand, entlang -n
        check(t >= g.CP_L + 2.0, f"Ecke: Plattenschraube {'links' if sa < 0 else 'rechts'}: {t:.0f} mm Keil, Kernloch {g.CP_L:.0f} mm")


def faces(part: Manifold):
    mesh = part.to_mesh()
    v = np.asarray(mesh.vert_properties)[:, :3]
    t = np.asarray(mesh.tri_verts)
    a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
    n = np.cross(b - a, c - a)
    length = np.maximum(np.linalg.norm(n, axis=1), 1e-12)
    zmin = np.minimum(np.minimum(a[:, 2], b[:, 2]), c[:, 2])
    return v, t, length / 2, n[:, 2] / length, zmin > 0.05


def printability() -> None:
    limit = -math.sin(math.radians(OVERHANG_MAX))
    for name, part in g.print_parts().items():
        v, t, area, nz, floating = faces(part)
        steep = floating & (nz < limit) & (nz > -0.999)
        flat = floating & (nz <= -0.999)
        span = bridge_span(v, t, flat)
        check(area[steep].sum() < 1.0 and span <= BRIDGE_MAX,
              f"{name}: Überhänge über {OVERHANG_MAX:.0f}° {area[steep].sum():.1f} mm², "
              f"Brücken bis {span:.1f} mm")


def bridge_span(v: np.ndarray, t: np.ndarray, sel: np.ndarray) -> float:
    """Die weiteste Brücke: zusammenhängende waagerechte Unterseiten, je
    die kürzere Seite ihrer Ausdehnung."""
    tris = t[sel]
    if not len(tris):
        return 0.0
    parent = list(range(len(v)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, b, c in tris:
        for x, y in ((a, b), (b, c)):
            parent[root(x)] = root(y)
    groups: dict[int, list[int]] = {}
    for a, _, _ in tris:
        groups.setdefault(root(a), [])
    for tri in tris:
        groups[root(tri[0])].extend(tri)
    widest = 0.0
    for idx in groups.values():
        pts = v[np.unique(idx)]
        extent = pts.max(axis=0) - pts.min(axis=0)
        widest = max(widest, min(extent[0], extent[1]))
    return widest


def main() -> int:
    print(f"Schale {g.W:.1f} × {g.H:.1f} mm, unten {g.Z_B0:.1f} und oben {g.D_TOP:.1f} mm tief, "
          f"Radar {g.RADAR_W} × {g.RADAR_H} mm, Neigung {g.TILT:.0f}°")
    parts_valid()
    look()
    assembly()
    mount()
    printability()
    print()
    print("Alles passt." if not problems else f"{len(problems)} Problem(e).")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
