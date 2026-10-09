"""Vorschaubilder aus genau der Geometrie, die als STL gedruckt wird.

    python gehaeuse.py --bilder     # oder: python vorschau.py

Braucht numpy und matplotlib. Die 3D-Ansichten rechnet ein kleiner
Rasterizer mit Tiefenpuffer (matplotlibs eigenes 3D sortiert Flächen nur
ungefähr und zeichnet verdeckte Teile darüber). Bilder nach bilder/.
"""

import math
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import Patch, PathPatch  # noqa: E402
from matplotlib.path import Path as MplPath  # noqa: E402

import gehaeuse as g  # noqa: E402

WHITE = (0.92, 0.92, 0.91)
LID = (0.78, 0.79, 0.80)
BRACKET = (0.86, 0.86, 0.84)
RADAR = (0.22, 0.47, 0.33)
ESP = (0.16, 0.18, 0.24)
USB = (0.72, 0.72, 0.74)
WALL = (0.96, 0.95, 0.93)
INK = np.array([0.18, 0.19, 0.21])


# --- Rasterizer -----------------------------------------------------------------------


def mesh_of(part, color, cull=True):
    m = part.to_mesh()
    return np.asarray(m.vert_properties)[:, :3].astype(float), np.asarray(m.tri_verts), color, cull


def quad(a, b, c, d, color):
    """Eine ebene Fläche (Wand), von beiden Seiten sichtbar."""
    return np.array([a, b, c, d], dtype=float), np.array([[0, 1, 2], [0, 2, 3]]), color, False


def camera(azim: float, elev: float):
    """Blickrichtung wie bei matplotlib: Kamera bei Azimut/Höhe, Blick zur Mitte."""
    a, e = math.radians(azim), math.radians(elev)
    forward = -np.array([math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e)])
    right = np.cross(forward, [0.0, 0.0, 1.0])
    right /= np.linalg.norm(right)
    up = np.cross(right, forward)
    return right, up, forward


def render(items, azim: float, elev: float, width=1200, height=900, ss=2, margin=0.07) -> np.ndarray:
    right, up, forward = camera(azim, elev)
    key = -0.55 * forward + 0.65 * up - 0.5 * right
    fill = -0.6 * forward - 0.2 * up + 0.75 * right
    key, fill = key / np.linalg.norm(key), fill / np.linalg.norm(fill)
    pts = np.concatenate([v for v, *_ in items])
    sx, sy = pts @ right, pts @ up
    w, h = width * ss, height * ss
    scale = min(w * (1 - 2 * margin) / np.ptp(sx), h * (1 - 2 * margin) / np.ptp(sy))
    ox = w / 2 - scale * (sx.min() + sx.max()) / 2
    oy = h / 2 + scale * (sy.min() + sy.max()) / 2
    zbuf = np.full((h, w), np.inf)
    color = np.ones((h, w, 3))
    normal = np.zeros((h, w, 3))
    ident = np.full((h, w), -1)
    for k, (v, tris, rgb, cull) in enumerate(items):
        px = np.column_stack([ox + scale * (v @ right), oy - scale * (v @ up), v @ forward])
        tri = v[tris]
        n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
        facing = n @ forward
        if not cull:
            n = np.where(facing[:, None] > 0, -n, n)
        shade = 0.34 + 0.52 * np.clip(n @ key, 0, 1) + 0.18 * np.clip(n @ fill, 0, 1)
        shaded = np.clip(np.asarray(rgb)[None, :] * shade[:, None], 0, 1)
        for i, (a, b, c) in enumerate(tris):
            if cull and facing[i] > 0:
                continue
            p0, p1, p2 = px[a], px[b], px[c]
            x0 = max(int(math.floor(min(p0[0], p1[0], p2[0]))), 0)
            x1 = min(int(math.ceil(max(p0[0], p1[0], p2[0]))), w - 1)
            y0 = max(int(math.floor(min(p0[1], p1[1], p2[1]))), 0)
            y1 = min(int(math.ceil(max(p0[1], p1[1], p2[1]))), h - 1)
            if x1 < x0 or y1 < y0:
                continue
            area = (p1[0] - p0[0]) * (p2[1] - p0[1]) - (p2[0] - p0[0]) * (p1[1] - p0[1])
            if abs(area) < 1e-9:
                continue
            X, Y = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
            w0 = ((p1[0] - X) * (p2[1] - Y) - (p2[0] - X) * (p1[1] - Y)) / area
            w1 = ((p2[0] - X) * (p0[1] - Y) - (p0[0] - X) * (p2[1] - Y)) / area
            w2 = 1.0 - w0 - w1
            inside = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
            z = w0 * p0[2] + w1 * p1[2] + w2 * p2[2]
            win = (slice(y0, y1 + 1), slice(x0, x1 + 1))
            closer = inside & (z < zbuf[win])
            if not closer.any():
                continue
            zbuf[win][closer] = z[closer]
            color[win][closer] = shaded[i]
            normal[win][closer] = n[i]
            ident[win][closer] = k
    # Umrisse und Kanten: wo das Teil wechselt, die Tiefe springt oder die
    # Fläche knickt — wie eine Linienzeichnung über dem Bild.
    edge = np.zeros((h, w), bool)
    finite = np.where(np.isfinite(zbuf), zbuf, 1e9)
    for dy, dx in ((0, 1), (1, 0)):
        a = (slice(0, h - dy), slice(0, w - dx))
        b = (slice(dy, h), slice(dx, w))
        jump = np.abs(finite[a] - finite[b]) > 0.8
        bend = np.sum(normal[a] * normal[b], axis=2) < math.cos(math.radians(32))
        part = ident[a] != ident[b]
        e = (jump | bend | part) & ((ident[a] >= 0) | (ident[b] >= 0))
        edge[a] |= e
    color[edge] = color[edge] * 0.35 + INK * 0.65
    img = color.reshape(height, ss, width, ss, 3).mean(axis=(1, 3))
    return img


def save(img: np.ndarray, path: Path, caption: str = "") -> None:
    h, w = img.shape[:2]
    fig = plt.figure(figsize=(w / 150, h / 150 + (0.35 if caption else 0)), dpi=150)
    ax = fig.add_axes((0, 0.35 / (h / 150 + 0.35) if caption else 0, 1, 1 - (0.35 / (h / 150 + 0.35) if caption else 0)))
    ax.imshow(img)
    ax.set_axis_off()
    if caption:
        fig.text(0.5, 0.1 / (h / 150 + 0.35), caption, ha="center", fontsize=10, color="0.3")
    fig.savefig(path, facecolor="white")
    plt.close(fig)


# --- Szenen ----------------------------------------------------------------------------


def upright(part):
    """Gehäusekoordinaten -> aufgestellt: Front nach -y, oben nach +z (eine
    Drehung; von vorn gesehen ist rechts, wo es am Sensor rechts ist)."""
    return part.transform([[-1, 0, 0, 0], [0, 0, 1, 0], [0, 1, 0, 0]])


def parts():
    shell, lid = g.shell(), g.lid()
    radar = g.box(-g.RADAR_W / 2, g.RADAR_W / 2, -g.RADAR_H / 2, g.RADAR_H / 2, g.Z_RADAR_FRONT, g.Z_RADAR_BACK)
    esp = g.box(-g.ESP_W / 2, g.ESP_W / 2, g.ESP_Y0, g.ESP_Y0 + g.ESP_L, g.Z_ESP_BOTTOM - g.ESP_PCB, g.Z_ESP_BOTTOM)
    usb = g.box(-g.USB_W / 2, g.USB_W / 2, g.ESP_Y0 - g.USB_OVERHANG, g.ESP_Y0 + 7.3,
                g.Z_ESP_BOTTOM - g.ESP_H, g.Z_ESP_BOTTOM - g.ESP_PCB)
    return shell, lid, radar, esp, usb


def front_view(out: Path) -> None:
    shell, lid, *_ = parts()
    img = render([mesh_of(upright(shell), WHITE), mesh_of(upright(lid), LID)], azim=-58, elev=16)
    save(img, out / "ansicht.png", f"{g.W:.0f} × {g.H:.0f} × {g.D:.1f} mm · Front: gleichmäßig {g.FRONT:.1f} mm")


def back_view(out: Path) -> None:
    shell, lid, *_ = parts()
    img = render([mesh_of(upright(shell), WHITE), mesh_of(upright(lid), LID)], azim=118, elev=22)
    save(img, out / "rueckseite.png", "Rückseite: Nut für die Schiene (unten offen), Rastdeckel, USB-C unten")


def exploded(out: Path) -> None:
    shell, lid, radar, esp, usb = parts()
    step = 18.0
    items = [
        mesh_of(upright(shell), WHITE),
        mesh_of(upright(radar.translate((0, 0, step))), RADAR),
        mesh_of(upright(esp.translate((0, 0, 2 * step))), ESP),
        mesh_of(upright(usb.translate((0, 0, 2 * step))), USB),
        mesh_of(upright(lid.translate((0, 0, 3 * step))), LID),
    ]
    img = render(items, azim=150, elev=24, width=1400, height=900)
    save(img, out / "explosion.png", "Gehäuse · LD2460 · ESP32-C5-Zero (USB-C unten) · Rückdeckel")


def mounted_scene(corner: bool):
    shell, lid, *_ = parts()
    body, lay = g.bracket(corner)
    sensor = [g.place(g.sensor_to_rail(p), lay["ex"], lay["ey"], lay["f"], lay["origin"]) for p in (shell, lid)]
    items = [mesh_of(sensor[0], WHITE), mesh_of(sensor[1], LID), mesh_of(body, BRACKET)]
    lo = np.min([np.asarray(s.bounding_box()[:3]) for s in sensor + [body]], axis=0)
    hi = np.max([np.asarray(s.bounding_box()[3:]) for s in sensor + [body]], axis=0)
    z0, z1 = lo[2] - 10, hi[2] + 10
    if corner:
        far = max(hi[0], hi[1]) + 12
        items.append(quad((0, 0, z0), (far, 0, z0), (far, 0, z1), (0, 0, z1), WALL))
        items.append(quad((0, 0, z0), (0, far, z0), (0, far, z1), (0, 0, z1), WALL))
    else:
        items.append(quad((lo[0] - 14, 0, z0), (hi[0] + 14, 0, z0), (hi[0] + 14, 0, z1), (lo[0] - 14, 0, z1), WALL))
    return items


def mounted(out: Path) -> None:
    wall_img = render(mounted_scene(False), azim=32, elev=12, width=900, height=900)
    corner_img = render(mounted_scene(True), azim=8, elev=16, width=900, height=900)
    fig, axes = plt.subplots(1, 2, figsize=(12, 6.4), dpi=150)
    for ax, img, title in ((axes[0], wall_img, f"Wandhalter: {g.TILT:.0f}° nach unten"),
                           (axes[1], corner_img, f"Eckhalter: {g.CORNER_YAW:.0f}° in den Raum, {g.TILT:.0f}° nach unten")):
        ax.imshow(img)
        ax.set_axis_off()
        ax.set_title(title, fontsize=11, color="0.3")
    fig.tight_layout()
    fig.savefig(out / "montage.png", facecolor="white")
    plt.close(fig)


def section(out: Path) -> None:
    """Schnitt durch die Mitte (x = 0): Tiefe nach rechts, oben nach oben."""
    shell, lid, radar, esp, usb = parts()
    body, lay = g.bracket(False)
    # Halter in Gehäusekoordinaten: Raum -> Schienenrahmen -> Gehäuse
    turn = np.vstack([lay["ex"], lay["ey"], lay["f"]])
    local = body.transform(np.column_stack([turn, -turn @ lay["origin"]]).tolist())
    bracket = local.transform([[-1, 0, 0, 0], [0, 1, 0, g.RAIL_Y0], [0, 0, -1, g.D]])
    fig, ax = plt.subplots(figsize=(9, 6.5), dpi=150)
    legend = []
    for part, rgb, name in ((shell, WHITE, "Gehäuse"), (radar, RADAR, "LD2460"), (esp, ESP, "ESP32-C5-Zero"),
                            (usb, USB, "USB-C-Buchse"), (lid, LID, "Rückdeckel"), (bracket, BRACKET, "Wandhalter")):
        cut = part.rotate((0, 90, 0)).slice(0.0)       # nach der Drehung: x = Tiefe, y = oben
        verts, codes = [], []
        for ring in cut.to_polygons():
            ring = np.asarray(ring)
            verts.extend(ring.tolist() + [ring[0].tolist()])
            codes.extend([MplPath.MOVETO] + [MplPath.LINETO] * (len(ring) - 1) + [MplPath.CLOSEPOLY])
        if verts:
            ax.add_patch(PathPatch(MplPath(verts, codes), facecolor=rgb, edgecolor="0.25", linewidth=0.6))
            legend.append(Patch(facecolor=rgb, edgecolor="0.25", label=name))
    ax.set_aspect("equal")
    ax.autoscale_view()
    ax.set_xlabel("Tiefe ab Front (mm)")
    ax.set_ylabel("Höhe (mm)")
    ax.grid(True, linewidth=0.3, color="0.88")
    ax.legend(handles=legend, loc="upper left", bbox_to_anchor=(1.01, 1.0), fontsize=8, frameon=False)
    stack = (f"Fenster {g.FRONT:.1f} · Luft {g.GAP:.1f} · Radar {g.RADAR_PCB:.1f} · dahinter {g.RADAR_BACK:.1f} · "
             f"ESP {g.ESP_H:.2f} · Rücken {g.SPINE_T:.1f} + {g.LID_T:.1f} = {g.D:.1f} mm")
    ax.set_title(f"Schnitt in der Mitte, am Wandhalter ({g.TILT:.0f}°)\n{stack}", fontsize=9)
    fig.savefig(out / "schnitt.png", bbox_inches="tight", facecolor="white")
    plt.close(fig)


def render_all(out: Path) -> None:
    out.mkdir(exist_ok=True)
    for draw in (front_view, back_view, exploded, mounted, section):
        draw(out)
        print(f"Bild: {out.name}/{draw.__name__}")


if __name__ == "__main__":
    render_all(Path(__file__).parent / "bilder")
    sys.exit(0)
