"""Where a radar target is in the room, and which zone it is in.

Plain functions, no state. static/geometry.js is the same arithmetic for
the browser; tests/test_geometry.py holds both to the same cases, so the
map and the Home Assistant entities cannot disagree about a point.

Coordinates:

  Room      metres, origin at the top-left corner of the floor plan, x to
            the right, y down — the way the plan is drawn on screen.
  Sensor    what the LD2460 reports, converted to metres: y is the
            distance straight ahead of the module, x the offset across.
            The sign of x is not documented; see `mirror`.

A sensor placed with angle 0 looks down the plan (+y). Positive angles
turn it clockwise on screen, which is the direction a rotation handle
dragged to the right turns it.
"""

import math


#: The sensor model's neutral values: with these, what the module reports
#: is taken as it is (measurement definition 2 and earlier).
MODEL_DEFAULTS = {"range_scale": 1.0, "range_offset_m": 0.0, "azimuth_scale": 1.0, "slant": False}


def is_neutral(placement: dict) -> bool:
    return all(placement.get(k, v) == v for k, v in MODEL_DEFAULTS.items())


def correct(x_m: float, y_m: float, placement: dict) -> tuple[float, float]:
    """What the module reported -> where the target is, still seen from
    the sensor.

    The module's positions are not the room's. Their errors are the ones
    that grow with distance, which moving and turning the sensor on the
    plan cannot take out, so the calibration fits a model of them:

      range_scale, range_offset_m  it reports a distance of
                                   range_scale × true + range_offset_m;
      azimuth_scale                it reports an angle off its axis of
                                   azimuth_scale × the true one;
      slant                        the distance is the slant line from
                                   the module, mounted at mount_height_m,
                                   to a body at target_height_m, not the
                                   distance along the floor.

    Neutral values (MODEL_DEFAULTS) change nothing, to the last bit.
    """
    if is_neutral(placement):
        return x_m, y_m
    r = math.hypot(x_m, y_m)
    phi = math.atan2(x_m, y_m)
    k = float(placement.get("range_scale", 1.0))
    b = float(placement.get("range_offset_m", 0.0))
    d = max(0.0, (r - b) / k)
    if placement.get("slant") and placement.get("mount_height_m") is not None:
        dh = float(placement["mount_height_m"]) - float(placement.get("target_height_m", 1.0))
        d = math.sqrt(max(d * d - dh * dh, 0.0))
    phi /= float(placement.get("azimuth_scale", 1.0))
    return d * math.sin(phi), d * math.cos(phi)


def to_room(x_m: float, y_m: float, placement: dict) -> tuple[float, float]:
    """Sensor coordinates -> room coordinates.

    `placement` is the sensor entry of a room: x, y (metres), angle
    (degrees), mirror (bool), and the sensor model (see `correct`).
    `mirror` flips the sensor's x axis. It exists because the manual does
    not say which side is positive, and the answer is found by walking
    past the module once, not by guessing.
    """
    x_m, y_m = correct(x_m, y_m, placement)
    if placement.get("mirror"):
        x_m = -x_m
    angle = math.radians(float(placement.get("angle", 0.0)))
    c, s = math.cos(angle), math.sin(angle)
    return (
        float(placement["x"]) + x_m * c - y_m * s,
        float(placement["y"]) + x_m * s + y_m * c,
    )


def point_in_polygon(x: float, y: float, points: list) -> bool:
    """Even-odd rule. A point exactly on an edge may land either side.

    That ambiguity costs nothing here: the radar's own resolution is a
    decimetre, far coarser than the difference.
    """
    inside = False
    n = len(points)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = points[i]
        xj, yj = points[j]
        if (yi > y) != (yj > y):
            crossing = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < crossing:
                inside = not inside
        j = i
    return inside


def in_room(x: float, y: float, width: float, height: float, margin: float = 0.0) -> bool:
    return -margin <= x <= width + margin and -margin <= y <= height + margin


def _segment_distance(x: float, y: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length))
    return math.hypot(x - (ax + t * dx), y - (ay + t * dy))


def distance_to_polygon(x: float, y: float, points: list) -> float:
    """Distance to the nearest edge — the nearest wall, for an outline."""
    n = len(points)
    return min(
        _segment_distance(x, y, *points[i], *points[(i + 1) % n]) for i in range(n)
    ) if n >= 2 else math.inf


def within_walls(x: float, y: float, width: float, height: float, outline, margin: float = 0.0) -> bool:
    """Inside the room, or no farther than `margin` outside its walls.

    Without an outline the room is its width × depth rectangle, and the
    margin is added to each side, as it always was. With one, the walls
    are the outline: a niche is inside, the corner cut out of an L-shaped
    room is not.
    """
    if not outline:
        return in_room(x, y, width, height, margin)
    return point_in_polygon(x, y, outline) or distance_to_polygon(x, y, outline) <= margin


def _cross(ax, ay, bx, by, cx, cy) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _segments_cross(p1, p2, p3, p4) -> bool:
    """Proper crossing or touching of two segments."""
    d1 = _cross(*p3, *p4, *p1)
    d2 = _cross(*p3, *p4, *p2)
    d3 = _cross(*p1, *p2, *p3)
    d4 = _cross(*p1, *p2, *p4)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 and d2 and d3 and d4:
        return True

    def on(a, b, c, d):
        return d == 0 and min(a[0], b[0]) <= c[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= c[1] <= max(a[1], b[1])

    return on(p3, p4, p1, d1) or on(p3, p4, p2, d2) or on(p1, p2, p3, d3) or on(p1, p2, p4, d4)


def self_intersects(points: list) -> bool:
    """Whether any two edges that are not neighbours cross or touch."""
    n = len(points)
    for i in range(n):
        a, b = points[i], points[(i + 1) % n]
        for j in range(i + 1, n):
            if j == i or (j + 1) % n == i or j == (i + 1) % n:
                continue
            if _segments_cross(a, b, points[j], points[(j + 1) % n]):
                return True
    return False


def polygon_area(points: list) -> float:
    """Unsigned area (shoelace). Used to refuse degenerate zones."""
    total = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def furniture_outline(x: float, y: float, w: float, h: float, angle: float, margin: float = 0.0) -> list:
    """The corners of a furniture item, turned about its centre and grown
    by `margin` on every side — the zone a sofa or a bed stands for.

    `x`, `y` is the top-left corner before turning, as the plan stores it.
    """
    cx, cy = x + w / 2, y + h / 2
    hw, hh = w / 2 + margin, h / 2 + margin
    a = math.radians(angle)
    c, s = math.cos(a), math.sin(a)
    return [
        (cx + dx * c - dy * s, cy + dx * s + dy * c)
        for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))
    ]


def clip_to_plan(points: list, width: float, height: float) -> list:
    """The part of a polygon on the width × depth plan (Sutherland–Hodgman),
    rounded to the millimetre. A sofa against the wall keeps its zone up to
    the wall instead of refusing to save."""

    def clip(pts, inside, cut):
        out = []
        for i, cur in enumerate(pts):
            prev = pts[i - 1]
            if inside(cur):
                if not inside(prev):
                    out.append(cut(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(cut(prev, cur))
        return out

    def at_x(xv):
        return lambda p, q: (xv, p[1] + (q[1] - p[1]) * (xv - p[0]) / (q[0] - p[0]))

    def at_y(yv):
        return lambda p, q: (p[0] + (q[0] - p[0]) * (yv - p[1]) / (q[1] - p[1]), yv)

    pts = [tuple(p) for p in points]
    for inside, cut in (
        (lambda p: p[0] >= 0, at_x(0.0)),
        (lambda p: p[0] <= width, at_x(float(width))),
        (lambda p: p[1] >= 0, at_y(0.0)),
        (lambda p: p[1] <= height, at_y(float(height))),
    ):
        if not pts:
            break
        pts = clip(pts, inside, cut)
    out: list = []
    for px, py in pts:
        q = (round(px, 3) + 0.0, round(py, 3) + 0.0)
        if not out or out[-1] != q:
            out.append(q)
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return out
