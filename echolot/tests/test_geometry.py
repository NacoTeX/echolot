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


BOWTIE = [[0, 0], [2, 2], [2, 0], [0, 2]]
# A 6 × 4 room with its bottom-left 2 × 1 m cut out.
NOTCHED = [[0, 0], [6, 0], [6, 4], [2, 4], [2, 3], [0, 3]]
WALL_PROBES = [(1, 1), (1, 3.2), (1, 3.6), (2.2, 3.5), (6.2, 2), (6.5, 2), (-0.25, -0.25), (1, 2.9)]


def test_distance_to_the_nearest_wall():
    assert geometry.distance_to_polygon(2, 2, SQUARE) == pytest.approx(1)
    assert geometry.distance_to_polygon(4, 2, SQUARE) == pytest.approx(1)
    assert geometry.distance_to_polygon(4, 4, SQUARE) == pytest.approx(math.sqrt(2))


def test_without_an_outline_the_walls_are_the_rectangle_as_before():
    for x, y in WALL_PROBES:
        assert geometry.within_walls(x, y, 6, 4, None, 0.3) == geometry.in_room(x, y, 6, 4, 0.3)


def test_with_an_outline_the_cut_out_corner_is_outside():
    assert geometry.within_walls(1, 1, 6, 4, NOTCHED, 0.3)
    assert not geometry.within_walls(1, 3.6, 6, 4, NOTCHED, 0.3)  # 0.6 m into the cut-out
    assert geometry.within_walls(1, 3.2, 6, 4, NOTCHED, 0.3)  # against the wall, within the margin
    assert geometry.within_walls(6.2, 2, 6, 4, NOTCHED, 0.3)
    assert not geometry.within_walls(6.5, 2, 6, 4, NOTCHED, 0.3)


def test_crossed_walls_are_recognised():
    assert geometry.self_intersects(BOWTIE)
    assert not geometry.self_intersects(SQUARE)
    assert not geometry.self_intersects(L_SHAPE)
    assert not geometry.self_intersects(NOTCHED)
    # A corner pulled onto another wall touches it.
    assert geometry.self_intersects([[0, 0], [4, 0], [4, 4], [2, 0], [0, 4]])


def test_a_furniture_outline_turns_about_the_centre_and_grows_by_the_margin():
    def flat(points):
        return [v for point in points for v in point]

    assert flat(geometry.furniture_outline(1, 1, 2, 1, 0, 0.2)) == pytest.approx(
        flat([(0.8, 0.8), (3.2, 0.8), (3.2, 2.2), (0.8, 2.2)]))
    turned = geometry.furniture_outline(1, 1, 2, 1, 90, 0)
    assert flat(turned) == pytest.approx(flat([(2.5, 0.5), (2.5, 2.5), (1.5, 2.5), (1.5, 0.5)]))


def test_clipping_keeps_the_part_on_the_plan():
    # A sofa against the top-left corner, grown past both walls.
    assert geometry.clip_to_plan(geometry.furniture_outline(0.1, 0.1, 2, 0.9, 0, 0.2), 6, 4) == [
        (0.0, 0.0), (2.3, 0.0), (2.3, 1.2), (0.0, 1.2)]
    # Turned 45° over the corner: the cut adds corners where it crosses.
    clipped = geometry.clip_to_plan(geometry.furniture_outline(-0.5, -0.5, 2, 1, 45, 0), 6, 4)
    assert all(0 <= x <= 6 and 0 <= y <= 4 for x, y in clipped)
    assert len(clipped) >= 3 and geometry.polygon_area(clipped) > 0
    assert geometry.clip_to_plan([(10, 10), (12, 10), (12, 12)], 6, 4) == []


def test_area():
    assert geometry.polygon_area(SQUARE) == pytest.approx(4)
    assert geometry.polygon_area(L_SHAPE) == pytest.approx(7)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_browser_computes_the_same_answers():
    cases = {
        "to_room": [[p, pl] for p in POINTS for pl in PLACEMENTS],
        "inside": [[pt, poly] for pt in PROBES for poly in (SQUARE, L_SHAPE)],
        "walls": [[pt, outline] for pt in WALL_PROBES for outline in (None, NOTCHED)],
        "crossed": [SQUARE, L_SHAPE, NOTCHED, BOWTIE, [[0, 0], [4, 0], [4, 4], [2, 0], [0, 4]]],
        "furniture": [[0.1, 0.1, 2, 0.9, 0, 0.2], [2, 2, 2, 1, 45, 0.2], [-0.5, -0.5, 2, 1, 45, 0],
                      [4.8, 3.2, 1.6, 2.0, 30, 0.35], [2.5, 1.5, 0.5, 0.5, -15, 0.0]],
    }
    script = f"""
      const g = require({json.dumps(str(JS))});
      const cases = {json.dumps(cases)};
      const out = {{
        to_room: cases.to_room.map(([[x, y], pl]) => {{ const r = g.toRoom(x, y, pl); return [r.x, r.y]; }}),
        inside: cases.inside.map(([[x, y], poly]) => g.pointInPolygon(x, y, poly)),
        area: [g.polygonArea({json.dumps(SQUARE)}), g.polygonArea({json.dumps(L_SHAPE)})],
        walls: cases.walls.map(([[x, y], outline]) => g.withinWalls(x, y, 6, 4, outline, 0.3)),
        distance: cases.walls.map(([[x, y]]) => g.distanceToPolygon(x, y, {json.dumps(NOTCHED)})),
        crossed: cases.crossed.map((poly) => g.selfIntersects(poly)),
        furniture: cases.furniture.map(([x, y, w, h, a, m]) => g.clipToPlan(g.furnitureOutline(x, y, w, h, a, m), 6, 4)),
      }};
      console.log(JSON.stringify(out));
    """
    result = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    for (point, placement), js in zip(cases["to_room"], result["to_room"]):
        assert js == pytest.approx(list(geometry.to_room(*point, placement)))
    for (point, poly), js in zip(cases["inside"], result["inside"]):
        assert js == geometry.point_in_polygon(*point, poly)
    assert result["area"] == pytest.approx([4, 7])
    for (point, outline), js in zip(cases["walls"], result["walls"]):
        assert js == geometry.within_walls(*point, 6, 4, outline, 0.3)
    for (point, _), js in zip(cases["walls"], result["distance"]):
        assert js == pytest.approx(geometry.distance_to_polygon(*point, NOTCHED))
    assert result["crossed"] == [geometry.self_intersects(p) for p in cases["crossed"]]
    for args, js in zip(cases["furniture"], result["furniture"]):
        py = geometry.clip_to_plan(geometry.furniture_outline(*args), 6, 4)
        assert len(js) == len(py), args
        for a, b in zip(js, py):
            assert a == pytest.approx(list(b), abs=0.0011)
