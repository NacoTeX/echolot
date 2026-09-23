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


def to_room(x_m: float, y_m: float, placement: dict) -> tuple[float, float]:
    """Sensor coordinates -> room coordinates.

    `placement` is the sensor entry of a room: x, y (metres), angle
    (degrees), mirror (bool). `mirror` flips the sensor's x axis. It
    exists because the manual does not say which side is positive, and
    the answer is found by walking past the module once, not by guessing.
    """
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


def polygon_area(points: list) -> float:
    """Unsigned area (shoelace). Used to refuse degenerate zones."""
    total = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0
