"""Solving the sensor's placement from standpoints.

Each case builds reports from a known "true" placement and asks the
solver to find it again from a wrong one — the situation after somebody
drew the sensor by eye.
"""

import math
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import alignment, geometry  # noqa: E402


def to_sensor(qx, qy, placement):
    """The inverse of geometry.to_room."""
    a = math.radians(placement["angle"])
    dx, dy = qx - placement["x"], qy - placement["y"]
    x = dx * math.cos(a) + dy * math.sin(a)
    y = -dx * math.sin(a) + dy * math.cos(a)
    return (-x if placement["mirror"] else x), y


def pairs_for(truth, spots, noise=0.0, seed=1):
    rng = random.Random(seed)
    out = []
    for qx, qy in spots:
        px, py = to_sensor(qx, qy, truth)
        out.append(((px + rng.uniform(-noise, noise), py + rng.uniform(-noise, noise)), (qx, qy)))
    return out


def test_the_inverse_used_here_is_right():
    truth = {"x": 2.0, "y": 0.3, "angle": 25.0, "mirror": True}
    px, py = to_sensor(1.2, 3.4, truth)
    assert geometry.to_room(px, py, truth) == pytest.approx((1.2, 3.4))


TRUTH = {"x": 2.6, "y": 0.1, "angle": 12.0, "mirror": False}
DRAWN = {"x": 3.0, "y": 0.0, "angle": 0.0, "mirror": False}
SPOTS3 = [(1.0, 2.0), (4.5, 2.5), (2.5, 3.6)]


def test_three_spots_find_position_and_direction():
    result = alignment.solve(pairs_for(TRUTH, SPOTS3), DRAWN, 6, 4)
    assert result["mode"] == "full"
    assert (result["x"], result["y"]) == pytest.approx((2.6, 0.1), abs=0.01)
    assert result["angle"] == pytest.approx(12.0, abs=0.2)
    assert result["mirror"] is False
    assert result["rms_m"] < 0.01 < result["rms_before_m"]
    assert all(after < before for after, before in zip(result["errors_after_m"], result["errors_before_m"]))


def test_three_spots_tell_a_mirrored_module_by_the_fit():
    truth = {**TRUTH, "mirror": True}
    result = alignment.solve(pairs_for(truth, SPOTS3), DRAWN, 6, 4)
    assert result["mirror"] is True and result["mirror_changed"] is True
    assert result["mirror_basis"] == "fit"
    assert result["angle"] == pytest.approx(12.0, abs=0.2)
    assert result["rms_m"] < 0.01


def test_scatter_of_a_real_module_still_lands_close():
    result = alignment.solve(pairs_for(TRUTH, SPOTS3 + [(5.0, 1.2)], noise=0.1, seed=3), DRAWN, 6, 4)
    assert math.hypot(result["x"] - 2.6, result["y"] - 0.1) < 0.15
    assert abs(result["angle"] - 12.0) < 3


def test_two_spots_decide_the_mirror_by_the_drawn_position():
    truth = {**TRUTH, "mirror": True}
    result = alignment.solve(pairs_for(truth, [(1.0, 2.0), (4.5, 2.5)]), DRAWN, 6, 4)
    assert result["mirror"] is True
    assert result["mirror_basis"] == "position"
    assert (result["x"], result["y"]) == pytest.approx((2.6, 0.1), abs=0.01)


def test_two_spots_in_line_with_the_sensor_leave_the_mirror_alone_and_say_so():
    # Both spots straight ahead: mirrored and not both put the sensor in
    # the same place.
    truth = {"x": 3.0, "y": 0.0, "angle": 0.0, "mirror": False}
    result = alignment.solve(pairs_for(truth, [(3.0, 1.5), (3.0, 3.5)]), DRAWN, 6, 4)
    assert result["mirror_basis"] == "kept" and result["mirror"] is False
    assert any("links und rechts" in w for w in result["warnings"])


def test_one_spot_turns_the_sensor_and_keeps_its_position():
    truth = {"x": 3.0, "y": 0.0, "angle": -20.0, "mirror": False}
    result = alignment.solve(pairs_for(truth, [(4.0, 3.0)]), DRAWN, 6, 4)
    assert result["mode"] == "direction"
    assert (result["x"], result["y"]) == (3.0, 0.0)
    assert result["angle"] == pytest.approx(-20.0, abs=0.1)
    assert result["warnings"] == []


def test_one_spot_whose_distance_does_not_fit_says_so():
    truth = {"x": 1.0, "y": 0.0, "angle": 0.0, "mirror": False}
    result = alignment.solve(pairs_for(truth, [(4.0, 3.0)]), DRAWN, 6, 4)
    assert any("Abstand" in w for w in result["warnings"])


def test_a_solution_far_outside_the_room_is_not_taken():
    truth = {"x": 3.0, "y": -2.0, "angle": 0.0, "mirror": False}
    result = alignment.solve(pairs_for(truth, SPOTS3), DRAWN, 6, 4)
    assert result["mode"] == "direction"
    assert (result["x"], result["y"]) == (3.0, 0.0)
    assert any("außerhalb" in w for w in result["warnings"])


def test_a_solution_just_behind_the_wall_is_put_on_it():
    truth = {"x": 2.6, "y": -0.2, "angle": 12.0, "mirror": False}
    result = alignment.solve(pairs_for(truth, SPOTS3), DRAWN, 6, 4)
    assert result["mode"] == "full"
    assert result["y"] == 0.0 and 0 <= result["x"] <= 6


def test_spots_close_together_are_flagged():
    result = alignment.solve(pairs_for(TRUTH, [(2.0, 2.0), (2.5, 2.2)]), DRAWN, 6, 4)
    assert any("keinen Meter" in w for w in result["warnings"])


def test_no_spots_is_an_error():
    with pytest.raises(ValueError):
        alignment.solve([], DRAWN, 6, 4)


# --- the sensor model -----------------------------------------------------------

from app.rooms import Room  # noqa: E402


def module_pairs(truth, spots, noise=0.0, seed=1):
    """Reports of a module with a sensor model: the forward direction of
    geometry.correct, then the placement undone."""
    rng = random.Random(seed)
    out = []
    for qx, qy in spots:
        a = math.radians(truth["angle"])
        dx, dy = qx - truth["x"], qy - truth["y"]
        x = dx * math.cos(a) + dy * math.sin(a)
        y = -dx * math.sin(a) + dy * math.cos(a)
        x = -x if truth.get("mirror") else x
        d, phi = math.hypot(x, y), math.atan2(x, y)
        phi *= truth.get("azimuth_scale", 1.0)
        r = d
        if truth.get("slant"):
            r = math.hypot(d, truth["mount_height_m"] - truth.get("target_height_m", 1.0))
        r = r * truth.get("range_scale", 1.0) + truth.get("range_offset_m", 0.0)
        out.append(((r * math.sin(phi) + rng.gauss(0, noise), r * math.cos(phi) + rng.gauss(0, noise)), (qx, qy)))
    return out


SPREAD = [(1.0, 1.5), (5.0, 1.5), (3.0, 1.2), (0.8, 3.6), (5.2, 3.6), (3.0, 3.7), (2.0, 2.6), (4.2, 2.4)]
DRAWN2 = {"x": 3.0, "y": 0.0, "angle": 0.0, "mirror": False}


def test_a_distance_scale_is_found_where_moving_and_turning_cannot_help():
    """The report from the field: the farther away, the farther off."""
    truth = {"x": 3.0, "y": 0.0, "angle": 4.0, "mirror": False, "range_scale": 1.12}
    pairs = module_pairs(truth, SPREAD, noise=0.03, seed=2)
    result = alignment.solve(pairs, DRAWN2, 6, 4)
    assert result["model"] in ("range", "range_offset", "range_azimuth")
    assert result["range_scale"] == pytest.approx(1.12, abs=0.03)
    assert result["rms_m"] < 0.06
    # The rigid fit alone leaves errors that grow with distance.
    rigid = alignment.fit_free(pairs, False)
    assert rigid[3] > 2 * result["rms_m"]


def test_the_slant_line_is_recognised_when_the_height_is_given():
    truth = {"x": 3.0, "y": 0.0, "angle": 0.0, "mirror": False, "slant": True,
             "mount_height_m": 2.2, "target_height_m": 1.0}
    pairs = module_pairs(truth, SPREAD, noise=0.02, seed=3)
    with_height = alignment.solve(pairs, {**DRAWN2, "mount_height_m": 2.2, "target_height_m": 1.0}, 6, 4)
    assert with_height["slant"] is True
    assert with_height["rms_m"] < 0.05
    # Without the height the slant cannot be taken out; something else is
    # fitted in its place, and not as well.
    without = alignment.solve(pairs, DRAWN2, 6, 4)
    assert without["slant"] is False and without["rms_m"] > with_height["rms_m"]


def test_an_angle_scale_is_found_with_spots_to_the_sides():
    truth = {"x": 3.0, "y": 0.0, "angle": 0.0, "mirror": False, "azimuth_scale": 0.82}
    pairs = module_pairs(truth, SPREAD, noise=0.02, seed=4)
    result = alignment.solve(pairs, DRAWN2, 6, 4)
    assert "azimuth" in result["model"] or result["model"] == "full"
    assert result["azimuth_scale"] == pytest.approx(0.82, abs=0.04)


def test_noise_alone_does_not_buy_a_richer_model():
    """Five spots from a module that errs only by where it hangs: the
    extra terms would fit the noise, and are not taken."""
    truth = {"x": 2.7, "y": 0.0, "angle": 6.0, "mirror": False}
    for seed in range(5):
        result = alignment.solve(module_pairs(truth, SPREAD[:5], noise=0.05, seed=seed), DRAWN2, 6, 4)
        assert result["model"] == "placement", (seed, result["candidates"][:2])
        assert result["range_scale"] == 1.0 and result["slant"] is False


def test_the_accuracy_is_checked_on_spots_left_out():
    truth = {"x": 3.0, "y": 0.0, "angle": 3.0, "mirror": False, "range_scale": 1.08}
    result = alignment.solve(module_pairs(truth, SPREAD, noise=0.05, seed=5), DRAWN2, 6, 4)
    assert result["check_m"] is not None
    # A fair estimate: above the in-sample residual, near the noise.
    assert result["rms_m"] <= result["check_m"] < 0.2
    assert result["quality"] == "good"


@pytest.mark.parametrize("index, offset", [
    (3, 1.2),  # a metre off: both ways see it
    (0, 0.6),  # swallowed by a richer model in the fit to all; left out, it shows
    (1, 0.8),  # left out, it falls where the others say little; the fit shows it
])
def test_a_spot_marked_in_the_wrong_place_is_named(index, offset):
    truth = {"x": 3.0, "y": 0.0, "angle": 0.0, "mirror": False}
    pairs = module_pairs(truth, SPREAD[:6], noise=0.02, seed=6)
    (raw, (qx, qy)) = pairs[index]
    pairs[index] = (raw, (qx + offset, qy))
    result = alignment.solve(pairs, DRAWN2, 6, 4)
    assert any(f"Standpunkt {index + 1} " in w for w in result["warnings"]), result["warnings"]
    others = [w for w in result["warnings"] if w.startswith("Standpunkt") and f"Standpunkt {index + 1} " not in w]
    assert others == []


def test_neutral_model_values_are_reported_for_the_placement_model():
    result = alignment.solve(pairs_for(TRUTH, SPOTS3), DRAWN, 6, 4)
    assert result["model"] == "placement" and result["model_changed"] is False
    assert (result["range_scale"], result["range_offset_m"], result["azimuth_scale"]) == (1.0, 0.0, 1.0)


def test_suggested_spots_are_in_view_inside_the_walls_and_off_the_furniture():
    room = Room.model_validate({
        "id": "r1", "name": "Wohnzimmer", "width": 6, "height": 4,
        "outline": [[0, 0], [6, 0], [6, 4], [2, 4], [2, 3], [0, 3]],
        "sensor": {"x": 3, "y": 0, "angle": 0, "fov_deg": 120, "range_m": 6},
        "furniture": [{"id": "fs", "kind": "sofa", "x": 3.5, "y": 2.6, "w": 2.2, "h": 0.9}],
    })
    spots = alignment.suggest_points(room, 5)
    assert len(spots) == 5
    for x, y in spots:
        assert geometry.point_in_polygon(x, y, room.outline)
        assert math.hypot(x - 3, y) >= 1.0
        assert math.degrees(abs(math.atan2(x - 3, y))) <= 60
        assert not geometry.point_in_polygon(x, y, geometry.furniture_outline(3.5, 2.6, 2.2, 0.9, 0, 0))
    xs = [x for x, _ in spots]
    ds = [math.hypot(x - 3, y) for x, y in spots]
    assert max(xs) - min(xs) > 2.0 and max(ds) - min(ds) > 1.0  # left and right, near and far
