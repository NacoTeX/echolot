"""Prüft das Gehäuse, bevor jemand Filament verbraucht.

    python pruefen.py

- Jedes Teil ist ein geschlossener, zusammenhängender Körper.
- Zusammengebaut: Radar, ESP und USB-Stecker stoßen nirgends an, die
  Stützen liegen auf dem Radar auf, der Deckel sitzt ohne Spannung und
  rastet, die Schiene läuft mit Spiel in der Nut.
- Montiert: Der Sensor lässt sich über die ganze Strecke aufschieben, hält
  Abstand zu den Wänden, berührt den Halter nur über die Schiene; der
  Halter ist von vorn verdeckt, und an die Schrauben kommt ein
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
        check(part.status().name == "NoError" and part.volume() > 0 and pieces == 1,
              f"{name}: geschlossen, {pieces} Körper, {part.volume() / 1000:.1f} cm³")


def dummies() -> dict[str, Manifold]:
    """Was ins Gehäuse kommt, in Gehäusekoordinaten."""
    return {
        "radar": g.box(-g.RADAR_W / 2, g.RADAR_W / 2, -g.RADAR_H / 2, g.RADAR_H / 2, g.Z_RADAR_FRONT, g.Z_RADAR_BACK),
        # Bauteile, Lötstellen und Kabel hinter dem Radar, innerhalb der Stützen
        "behind": g.box(-g.RADAR_W / 2 + 3.2, g.RADAR_W / 2 - 3.2, -g.RADAR_H / 2 + 3.2, g.RADAR_H / 2 - 3.2,
                        g.Z_RADAR_BACK, g.Z_RADAR_BACK + g.RADAR_BACK),
        "esp": g.box(-g.ESP_W / 2, g.ESP_W / 2, g.ESP_Y0, g.ESP_Y0 + g.ESP_L, g.Z_ESP_BOTTOM - g.ESP_PCB, g.Z_ESP_BOTTOM),
        "usb": g.box(-g.USB_W / 2, g.USB_W / 2, g.ESP_Y0 - g.USB_OVERHANG, g.ESP_Y0 + 7.3,
                     g.Z_ESP_BOTTOM - g.ESP_H, g.Z_ESP_BOTTOM - g.ESP_PCB),
        # ein üblicher Stecker: Umspritzung 12,5 × 6,5 mm, bis an die Buchse
        "plug": g.box(-6.25, 6.25, -g.CAV_H / 2 - 30, -g.CAV_H / 2 - 0.05, g.Z_USB - 3.25, g.Z_USB + 3.25),
    }


def assembly() -> None:
    shell, lid, d = g.shell(), g.lid(), dummies()
    radar = d["radar"]
    check(overlap(radar, shell) < 1e-3, "Radar passt in die Tasche")
    side = radar.trim_by_plane((0, 0, 1), g.Z_RADAR_FRONT + 0.5)   # über den Auflagen: nur seitlich
    play = side.min_gap(shell, 1.0)
    check(play > g.FIT - 0.05, f"Spiel ums Radar {play:.2f} mm")
    check(overlap(radar, lid) < 1e-3, "Stützen drücken nicht ins Radar")
    check(radar.min_gap(lid, 1.0) < 0.02, "Stützen liegen auf dem Radar auf")
    pads = shell.trim_by_plane((0, 0, 1), g.FRONT + 0.01).trim_by_plane((0, 0, -1), -g.Z_RADAR_FRONT + 0.01)
    check(radar.min_gap(pads, 1.0) < 0.02, "Auflagen halten das Radar mit Abstand zum Fenster")
    check(overlap(d["behind"], lid) < 1e-3 and overlap(d["behind"], shell) < 1e-3, "Platz hinter dem Radar frei")
    for name in ("esp", "usb"):
        check(overlap(d[name], lid) < 1e-3 and overlap(d[name], shell) < 1e-3, f"ESP ({name}) stößt nirgends an")
    check(g.Z_ESP_BOTTOM - g.ESP_H >= g.Z_RADAR_BACK + g.RADAR_BACK - 1e-6, "ESP liegt hinter dem Platz fürs Radar")
    check(abs((g.ESP_Y0 - g.USB_OVERHANG) - (-g.CAV_H / 2)) < 1e-6, "USB-Buchse sitzt an der Innenwand")
    check(overlap(d["plug"], shell) < 1e-3 and overlap(d["plug"], lid) < 1e-3, "USB-Stecker passt durch die Öffnung")
    check(overlap(lid, shell) < 1e-3, "Deckel sitzt ohne Spannung")
    gap = lid.min_gap(shell, 1.0)
    check(gap > 0.1, f"Deckel hat rundum Spiel ({gap:.2f} mm)")
    hold = overlap(lid.translate((0, 0, 1.0)), shell)
    check(0 < hold < 20, f"Rastnasen halten den Deckel ({hold:.1f} mm³ zu überwinden)")


def sensor_world(part: Manifold, lay: dict) -> Manifold:
    return g.place(g.sensor_to_rail(part), lay["ex"], lay["ey"], lay["f"], lay["origin"])


def to_local(part: Manifold, lay: dict) -> Manifold:
    """Raum -> Schienenrahmen."""
    turn = np.vstack([lay["ex"], lay["ey"], lay["f"]])
    return part.transform(np.column_stack([turn, -turn @ lay["origin"]]).tolist())


def mount() -> None:
    lid_r = g.sensor_to_rail(g.lid())
    sensor_r = g.union([lid_r, g.sensor_to_rail(g.shell())])
    rail = g.rail()
    check(overlap(rail, sensor_r) < 1e-3, "Schiene läuft durchs Gehäuse in die Nut")
    play = rail.min_gap(lid_r.translate((0, 1.0, 0)), 1.0)
    check(0.15 < play < 0.3, f"Spiel der Schiene in der Nut {play:.2f} mm")
    check(overlap(rail.translate((0, 0.3, 0)), lid_r) > 0, "Nut endet oben als Anschlag")
    for corner in (False, True):
        name = "Eckhalter" if corner else "Wandhalter"
        body, lay = g.bracket(corner)
        local = to_local(body, lay)
        # aufschieben: von ganz oben bis auf den Anschlag
        steps = np.arange(0.0, g.SENSOR_Y1 + 2.0, 0.5)
        hits = [dy for dy in steps if overlap(local, sensor_r.translate((0, dy, 0))) > 1e-3]
        check(not hits, f"{name}: Sensor lässt sich aufschieben, berührt den Halter nur über die Schiene"
              + (f" (stößt bei {hits[0]:.1f} mm)" if hits else ""))
        placed = g.place(rail, lay["ex"], lay["ey"], lay["f"], lay["origin"])
        check((placed - body).volume() < 1e-3, f"{name}: Schiene vollständig")
        x0, y0, z0, x1, y1, z1 = body.bounding_box()
        check(y0 > -1e-6 and (not corner or x0 > -1e-6), f"{name}: bleibt vor der Wand")
        sv = vertices(sensor_world(g.union([g.shell(), g.lid()]), lay))
        near = min(sv[:, axis].min() for axis in lay["walls"])
        check(near >= g.CLEAR - 0.05, f"{name}: Sensor {near:.1f} mm von der Wand")
        lv = vertices(local)
        hidden = (np.abs(lv[:, 0]).max() <= g.W / 2 and lv[:, 1].min() >= g.SENSOR_Y0 - 1e-6
                  and lv[:, 1].max() <= g.SENSOR_Y1)
        check(hidden, f"{name}: von vorn hinter dem Sensor verborgen")
        tilt = math.degrees(math.asin(-lay["f"][2]))
        check(abs(tilt - g.TILT) < 1e-6, f"{name}: neigt den Sensor um {tilt:.0f}° nach unten")
        plug = sensor_world(dummies()["plug"], lay)
        check(overlap(plug, body) < 1e-3, f"{name}: USB-Stecker frei")
        sensor_w = sensor_world(g.union([g.shell(), g.lid()]), lay)
        for k, (p, axis) in enumerate(lay["screws"], 1):
            a = np.zeros(3)
            a[axis] = 1.0
            face = np.asarray(p) + g.WING_T * a
            # Kopf und Klinge des Schraubendrehers: frei bis 80 mm vor die Wand
            path = along(face, a, g.HEAD_R + 0.3, 80.0)
            check(overlap(path, body) < 1e-3, f"{name}: Schraube {k} frei erreichbar")
            head = along(face, a, g.HEAD_R, 3.2)
            check(overlap(head, sensor_w) < 1e-3, f"{name}: Schraubenkopf {k} stößt nicht an den Sensor")
            plate = along(np.asarray(p), a, g.SCREW_R + 1.5, g.WING_T) - along(np.asarray(p) - a, a, g.SCREW_R + 0.6, 5)
            check(overlap(plate, body) > 0.95 * plate.volume(), f"{name}: Schraube {k} hat ringsum Platte")
            if corner:
                other = 1 - axis
                check(p[other] >= g.DRIVER_R, f"{name}: Griff des Schraubendrehers {k} passt neben die Wand "
                      f"({p[other]:.0f} mm)")


def along(p, a, r: float, length: float) -> Manifold:
    """Zylinder mit Radius r von p aus entlang a (Einheitsvektor einer Achse)."""
    c = Manifold.cylinder(length, r)
    if a[0] == 1.0:
        c = c.rotate((0, 90, 0))
    elif a[1] == 1.0:
        c = c.rotate((-90, 0, 0))
    return c.translate(tuple(p))


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
    print(f"Gehäuse {g.W:.1f} × {g.H:.1f} × {g.D:.1f} mm, Radar {g.RADAR_W} × {g.RADAR_H} mm, "
          f"Neigung {g.TILT:.0f}°")
    parts_valid()
    assembly()
    mount()
    printability()
    print()
    print("Alles passt." if not problems else f"{len(problems)} Problem(e).")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
