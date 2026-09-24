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
