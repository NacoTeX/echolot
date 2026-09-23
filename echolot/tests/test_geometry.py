"""Room geometry, in Python and in the browser, held to the same cases.

The map draws a target inside a zone with static/geometry.js; Home
Assistant is told it is inside with app/geometry.py. If the two disagreed
the map would show somebody on the sofa while the sofa entity said
empty. So every case runs through both.
"""

import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import geometry  # noqa: E402

JS = Path(__file__).resolve().parents[1] / "app" / "static" / "geometry.js"

PLACEMENTS = [
    {"x": 3.0, "y": 0.0, "angle": 0, "mirror": False},
    {"x": 3.0, "y": 0.0, "angle": 0, "mirror": True},
    {"x": 0.0, "y": 2.0, "angle": -90, "mirror": False},
    {"x": 5.0, "y": 4.0, "angle": 135, "mirror": False},
    {"x": 1.2, "y": 3.3, "angle": 37.5, "mirror": True},
]
POINTS = [(0.0, 0.0), (1.5, 2.3), (-0.1, 3.9), (-2.4, 1.0), (0.3, 6.5)]

SQUARE = [[1, 1], [3, 1], [3, 3], [1, 3]]
L_SHAPE = [[0, 0], [4, 0], [4, 1], [1, 1], [1, 4], [0, 4]]
PROBES = [(2, 2), (0.5, 0.5), (3.5, 0.5), (0.5, 3.5), (2, 2.5), (5, 5), (-1, 2)]


def test_a_sensor_looking_down_the_plan_adds_its_position():
    assert geometry.to_room(1.5, 2.3, {"x": 3, "y": 0, "angle": 0}) == pytest.approx((4.5, 2.3))


def test_mirror_flips_the_modules_x_axis_only():
    assert geometry.to_room(1.5, 2.3, {"x": 3, "y": 0, "angle": 0, "mirror": True}) == pytest.approx((1.5, 2.3))


def test_a_sensor_turned_to_face_right_maps_ahead_to_plus_x():
    # Mounted on the left wall, facing into the room.
    x, y = geometry.to_room(0.0, 2.0, {"x": 0, "y": 1.5, "angle": -90})
    assert (x, y) == pytest.approx((2.0, 1.5))


def test_straight_ahead_is_the_handle_direction_used_by_the_editor():
    # plan.js places the rotate handle at (x - sin a, y + cos a).
    for angle in (0, 30, -50, 90, 180):
        x, y = geometry.to_room(0.0, 1.0, {"x": 0, "y": 0, "angle": angle})
        a = math.radians(angle)
        assert (x, y) == pytest.approx((-math.sin(a), math.cos(a)))


def test_polygons_including_concave_ones():
    assert geometry.point_in_polygon(2, 2, SQUARE)
    assert not geometry.point_in_polygon(0.5, 0.5, SQUARE)
    assert geometry.point_in_polygon(0.5, 3.5, L_SHAPE)
    assert not geometry.point_in_polygon(2, 2, L_SHAPE)  # the notch
    assert not geometry.point_in_polygon(1, 1, [[0, 0], [1, 1]])


def test_room_bounds_with_margin():
    assert geometry.in_room(-0.2, 1, 4, 3, margin=0.3)
    assert not geometry.in_room(-0.4, 1, 4, 3, margin=0.3)


def test_area():
    assert geometry.polygon_area(SQUARE) == pytest.approx(4)
    assert geometry.polygon_area(L_SHAPE) == pytest.approx(7)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_browser_computes_the_same_answers():
    cases = {
        "to_room": [[p, pl] for p in POINTS for pl in PLACEMENTS],
        "inside": [[pt, poly] for pt in PROBES for poly in (SQUARE, L_SHAPE)],
    }
    script = f"""
      const g = require({json.dumps(str(JS))});
      const cases = {json.dumps(cases)};
      const out = {{
        to_room: cases.to_room.map(([[x, y], pl]) => {{ const r = g.toRoom(x, y, pl); return [r.x, r.y]; }}),
        inside: cases.inside.map(([[x, y], poly]) => g.pointInPolygon(x, y, poly)),
        area: [g.polygonArea({json.dumps(SQUARE)}), g.polygonArea({json.dumps(L_SHAPE)})],
      }};
      console.log(JSON.stringify(out));
    """
    result = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    for (point, placement), js in zip(cases["to_room"], result["to_room"]):
        assert js == pytest.approx(list(geometry.to_room(*point, placement)))
    for (point, poly), js in zip(cases["inside"], result["inside"]):
        assert js == geometry.point_in_polygon(*point, poly)
    assert result["area"] == pytest.approx([4, 7])
