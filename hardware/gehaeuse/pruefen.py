"""Prüft das Gehäuse, bevor jemand Filament verbraucht.

    python pruefen.py

- Jedes Teil ist ein geschlossener, zusammenhängender Körper.
- Festen Sitz: Das Radar wird von Quetschrippen gehalten, auch wenn die
  Platine am unteren oder oberen Ende ihrer Toleranz liegt; der ESP von
  Quetschrippen, Anschlägen und Haltearmen. Beide passen noch mit der
  größten Platine, ohne zu klemmen.
- Zusammengebaut: Radar, ESP und USB-Stecker stoßen nirgends an, der
  Stecker hat seine Aussparung, das Gehäuse sitzt auf dem Deckel, und die
  Löcher für die Seitenschrauben treffen die Kernlöcher in den Domen.
- Montiert: Das Gehäuse lässt sich über die ganze Strecke auf die Rippen
  schieben; die Wandschrauben sind vor dem Gehäuse frei erreichbar; in der
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
TOL = 0.2             # Fertigungstoleranz der Platinen, je Maß (Annahme für beide)

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


def fit_radar() -> None:
    """Das Radar sitzt fest: bei jeder Platinengröße innerhalb der Toleranz
    berühren die Rippen, und die größte Platine passt noch."""
    shell = g.shell()
    ribs = g.crush_ribs()
    all_ribs = g.union([r for r, _ in ribs])
    walls = shell - all_ribs
    nominal = g.radar_box()
    small = g.radar_box(g.RADAR_W - TOL, g.RADAR_H - TOL)
    large = g.radar_box(g.RADAR_W + TOL, g.RADAR_H + TOL, g.RADAR_PCB + 0.2)
    check(overlap(nominal, walls) < 1e-3, "Radar passt in die Tasche")
    check(overlap(large, walls) < 1e-3, f"auch die größte Platine ({g.RADAR_W + TOL:.1f} × {g.RADAR_H + TOL:.1f} mm) passt")
    v = overlap(nominal, all_ribs)
    check(0.5 < v < 25.0, f"Quetschrippen drücken die Platine mit {g.CRUSH} mm je Seite ({v:.1f} mm³ zu verformen)")
    vl = overlap(large, all_ribs)
    check(vl < 60.0, f"größte Platine: {vl:.0f} mm³ zu verformen, kein Klemmen")
    for label, sel in (("links", lambda n: n.startswith("x-")), ("rechts", lambda n: n.startswith("x+")),
                       ("unten", lambda n: n.startswith("y-")), ("oben", lambda n: n.startswith("y+"))):
        side = g.union([r for r, n in ribs if sel(n)])
        gap = small.min_gap(side, 1.0)
        check(gap < 0.01, f"kleinste Platine ({g.RADAR_W - TOL:.1f} × {g.RADAR_H - TOL:.1f} mm) berührt die Rippen {label}")
    zones = g.radar_zones()
    check(all_ribs.min_gap(zones["tall"], 20.0) >= 0.5, "Quetschrippen stehen nicht an den Stiftleisten")
    pads = shell.trim_by_plane((0, 0, 1), g.FRONT + 0.01).trim_by_plane((0, 0, -1), -g.Z_SEAT + 0.01)
    check(nominal.min_gap(pads, 1.0) < 0.02, f"Auflagen halten das Radar mit {g.Z_SEAT - g.FRONT:.1f} mm Abstand zum Fenster")
    check(nominal.min_gap(shell.trim_by_plane((0, 0, -1), -g.FRONT), 1.0) > g.GAP - 0.01,
          f"Chips auf der Antennenseite ({g.RADAR_FRONT:.1f} mm) haben {g.GAP:.1f} mm Luft zum Fenster")


def esp_parts() -> dict[str, Manifold]:
    """ESP in Deckelkoordinaten: Platine, Buchse und ein üblicher Stecker."""
    top = g.ESP_SO + g.ESP_PCB
    out = {
        "esp": g.box(-g.ESP_W / 2, g.ESP_W / 2, g.ESP_B0, g.ESP_B0 + g.ESP_L, g.ESP_SO, top),
        "usb": g.box(-g.USB_W / 2, g.USB_W / 2, g.ESP_B0 - g.USB_OVERHANG, g.ESP_B0 + 7.3, top, g.ESP_SO + g.ESP_H),
        # ein üblicher Stecker: Umspritzung 12,5 × 6,5 mm, bis an die Buchse
        "plug": g.box(-6.25, 6.25, -40.0, g.ESP_B0 - g.USB_OVERHANG - 0.05,
                      g.H_USB_MID - 3.25, g.H_USB_MID + 3.25),
    }
    return out


def fit_esp() -> None:
    """Der ESP sitzt fest: Quetschrippen, Anschläge, Arme; die größte Platine
    passt noch; er lässt sich einsetzen."""
    p = esp_parts()
    top = g.ESP_SO + g.ESP_PCB
    pocket = g.esp_pocket()
    bare = g.esp_pocket(with_ribs=False)
    ribs_only = pocket - bare
    nominal = p["esp"]
    big = g.box(-(g.ESP_W + TOL) / 2, (g.ESP_W + TOL) / 2, g.ESP_B0 - TOL / 2, g.ESP_B0 + g.ESP_L + TOL / 2, g.ESP_SO, top)
    small = g.box(-(g.ESP_W - TOL) / 2, (g.ESP_W - TOL) / 2, g.ESP_B0 + TOL / 2, g.ESP_B0 + g.ESP_L - TOL / 2, g.ESP_SO, top)
    check(overlap(nominal, bare) < 1e-3 and overlap(p["usb"], bare) < 1e-3, "ESP und Buchse stoßen an keine Wand")
    check(overlap(big, bare) < 1e-3, f"auch die größte Platine ({g.ESP_W + TOL:.1f} × {g.ESP_L + TOL:.1f} mm) passt")
    v = overlap(nominal, ribs_only)
    check(0.1 < v < 10.0, f"Quetschrippen drücken den ESP mit {g.ESP_CRUSH} mm je Seite ({v:.2f} mm³ zu verformen)")
    for sa, label in ((-1, "links"), (1, "rechts")):
        half = g.plate_box(0, sa * 30.0, 0, 80.0, -1, 20) if sa > 0 else g.plate_box(-30.0, 0, 0, 80.0, -1, 20)
        side = ribs_only ^ half
        check(small.min_gap(side, 1.0) < 0.01, f"kleinste Platine ({g.ESP_W - TOL:.1f} mm breit) berührt die Rippen {label}")
    rest = nominal.translate((0, 0, -0.05))
    check(overlap(rest, bare) > 0, "ESP liegt auf den Auflagen")
    # Haltearme: über der Platine, mit Luft, und greifen über die Ecken
    end0 = g.ESP_B0 + g.ESP_L
    # was die Tasche wirklich über den Ecken der Platine hat (nicht die Funktion, die sie baut)
    arms = bare ^ g.box(-g.ESP_W / 2, g.ESP_W / 2, end0 - g.ARM_L, end0, top + 0.01, top + 3.0)
    check(arms.volume() > 2.0, f"Tasche hat Haltearme über den Ecken am Antennenende ({arms.volume():.1f} mm³ über der Platine)")
    gap = nominal.min_gap(arms, 2.0)
    check(abs(gap - g.ARM_GAP) < 0.02, f"Haltearme über der Platine, {gap:.2f} mm Luft")
    lift = nominal.translate((0, 0, g.ARM_GAP + 0.2))
    check(overlap(lift, arms) > 1.0, "Platine kann nicht hochkippen: Arme greifen über die Ecken")
    # Stecker: schiebt die Platine nach oben, an den Anschlag; zieht man ihn, hält der untere Anschlag
    check(overlap(nominal.translate((0, 0.12, 0)), bare) > 0, "oberer Anschlag hält die Platine beim Stecken")
    check(overlap(nominal.translate((0, -0.12, 0)), bare) > 0, "unterer Anschlag hält die Platine beim Ziehen")
    # Einsetzen: um 3° gekippt, das Antennenende voran, unter die Arme schieben; dann absenken
    end = g.ESP_B0 + g.ESP_L
    hits = []
    for s in np.arange(-8.0, 0.01, 0.5):
        if overlap(tilt_about_end(nominal, 3.0, end - 0.1 + s), bare) > 1e-3:
            hits.append(f"{s:+.1f} mm")
    for tilt in (3.0, 2.0, 1.0, 0.0):
        if overlap(tilt_about_end(nominal, tilt, end - 0.1), bare) > 1e-3:
            hits.append(f"{tilt:.0f}°")
    check(not hits, "ESP lässt sich gekippt unter die Arme schieben und am USB-Ende absenken"
          + (f" (stößt bei {hits[0]})" if hits else ""))


def tilt_about_end(board: Manifold, deg: float, pivot_b: float) -> Manifold:
    """Die Platine um die Unterkante ihres Antennenendes gekippt: das USB-Ende
    steht höher, das Antennenende liegt bei pivot_b."""
    end = g.ESP_B0 + g.ESP_L
    moved = board.translate((0, pivot_b - end, 0))
    return moved.translate((0, -pivot_b, -g.ESP_SO)).rotate((-deg, 0, 0)).translate((0, pivot_b, g.ESP_SO))


def dummies() -> dict[str, Manifold]:
    zones = g.radar_zones()
    return {"radar": g.radar_box(), **zones}


def in_shell(part: Manifold) -> Manifold:
    """Deckelkoordinaten -> Gehäusekoordinaten."""
    return g.apply(part, g.PLATE_TO_SHELL)


def assembly() -> None:
    shell = g.shell()
    plate = in_shell(g.plate())
    d = dummies()
    small_esp = g.box(-(g.ESP_W - TOL) / 2, (g.ESP_W - TOL) / 2, g.ESP_B0 + TOL / 2, g.ESP_B0 + g.ESP_L - TOL / 2,
                      g.ESP_SO, g.ESP_SO + g.ESP_PCB)
    esp, usb, plug = in_shell(small_esp), in_shell(esp_parts()["usb"]), in_shell(esp_parts()["plug"])
    check(overlap(d["tall"], shell) < 1e-3 and overlap(d["tall"], plate) < 1e-3,
          f"Platz für Stiftleisten samt Buchsen ({g.RADAR_BACK:.0f} mm) frei")
    check(overlap(d["low"], shell) < 1e-3 and overlap(d["low"], plate) < 1e-3,
          f"Platz für die übrigen Bauteile hinter dem Radar ({g.RADAR_LOW:.0f} mm) frei")
    base = in_shell(g.Manifold.extrude(g.footprint(), g.PLATE_T).translate((0, 0, -g.PLATE_T)))
    gap = d["tall"].min_gap(g.union([base, in_shell(g.esp_pocket())]), 100.0)
    check(gap >= 4.0, f"Stiftleisten halten {gap:.0f} mm Abstand zum Deckel: Platz für die Leitungen")
    check(esp.min_gap(d["low"], 20.0) >= 1.0 and usb.min_gap(d["low"], 20.0) >= 1.0,
          f"ESP hält Abstand zum Radar ({min(esp.min_gap(d['low'], 20.0), usb.min_gap(d['low'], 20.0)):.1f} mm)")
    esp_ribs = in_shell(g.esp_pocket() - g.esp_pocket(with_ribs=False))
    bare_plate = plate - esp_ribs                            # ohne Quetschrippen: sie sollen ja drücken
    check(overlap(esp, bare_plate) < 1e-3 and overlap(esp, shell) < 1e-3, "ESP stößt nirgends an")
    check(overlap(usb, plate) < 1e-3 and overlap(usb, shell) < 1e-3, "USB-Buchse stößt nirgends an")
    check(overlap(plug, shell) < 1e-3 and overlap(plug, plate) < 1e-3, "USB-Stecker passt durch die Aussparung")
    check(overlap(shell, plate) < 1e-3, "Gehäuse sitzt auf dem Deckel, ohne Spannung")
    seat = shell.min_gap(plate, 1.0)
    check(seat < 0.02, f"Gehäuse liegt flach auf dem Deckel auf ({seat:.3f} mm)")
    ribs = in_shell(g.ribs())
    walls = ribs.min_gap(shell, 2.0)
    check(walls >= g.RIB_GAP - 0.05, f"Rippen haben {walls:.2f} mm Spiel im Gehäuse")
    # Seitenschraube: eine Sonde vom Außenrand durch das Loch bis ans Ende des Kernlochs
    p = g.apply_point(g.PLATE_TO_SHELL, [0.0, g.SS_B, g.SS_H])
    outer = g.CAV_W / 2 - g.RIB_GAP
    for side_ in (-1, 1):
        r = 1.0                                              # die Schraube (2,9 mm) mit Gewinde
        x_end = side_ * (outer - g.PILOT_L + 0.1)
        start = side_ * 40.0
        probe = g.Manifold.cylinder(abs(start - x_end), r).rotate((0, 90, 0)).translate((min(start, x_end), p[1], p[2]))
        check(overlap(probe, shell) < 1e-3 and overlap(probe, plate) < 1e-3,
              f"Seitenschraube {'links' if side_ < 0 else 'rechts'}: Loch und Kernloch fluchten")
    behind = g.DOM_L - g.PILOT_L
    check(behind >= 1.0, f"Seitenschraube: {behind:.1f} mm Dom hinter dem Kernloch")
    screw = 9.5                                              # Blechschraube 2,9 × 9,5
    reach = screw - (g.WALL - g.CB_D) - g.RIB_GAP
    check(reach <= g.PILOT_L + 0.5 and reach >= g.PILOT_L - 1.0,
          f"Seitenschraube 2,9 × 9,5: {reach:.1f} mm im Dom, Kernloch {g.PILOT_L:.0f} mm")
    check(g.WALL - g.CB_D >= 1.0, f"Seitenwand unter dem Schraubenkopf {g.WALL - g.CB_D:.1f} mm")
    # Wandschrauben: vor dem Aufsetzen des Gehäuses frei erreichbar
    plate_p = g.plate()
    for k, sa in enumerate((-1, 1), 1):
        x, y = sa * g.WS_A, g.WS_B
        driver = Manifold.cylinder(80.0, g.CSK_R + 0.3).translate((x, y, 0.2))
        check(overlap(driver, plate_p) < 1e-3, f"Wandschraube {k}: frei erreichbar, Kopf bis {g.CSK_R:.1f} mm Radius")
    # das Gehäuse lässt sich entlang der Rippen aufschieben
    n = g.N_PLATE
    hits = [k for k in np.arange(0.0, 45.0, 1.0) if overlap(shell.translate(tuple(k * n)), plate) > 1e-3]
    check(not hits, "Gehäuse lässt sich entlang der Rippen aufschieben"
          + (f" (stößt bei {hits[0]:.0f} mm)" if hits else ""))
    # und die Rippen stehen nicht an den Stiftleisten
    check(ribs.min_gap(d["tall"], 20.0) >= 2.0, f"Rippen und Dome halten {ribs.min_gap(d['tall'], 20.0):.1f} mm Abstand zu den Stiftleisten")


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
    print(f"Gehäuse {g.W:.1f} × {g.H:.1f} mm, unten {g.Z_B0:.1f} und oben {g.D_TOP:.1f} mm tief, "
          f"Radar {g.RADAR_W} × {g.RADAR_H} mm, Neigung {g.TILT:.0f}°")
    parts_valid()
    look()
    fit_radar()
    fit_esp()
    assembly()
    mount()
    printability()
    print()
    print("Alles passt." if not problems else f"{len(problems)} Problem(e).")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
