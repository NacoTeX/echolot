"""Which way the module's axes point, from two walks drawn on the plan.

A module is simulated as it really hangs (TRUTH); the plan may say
otherwise. The walks are what it would report for somebody walking the
drawn way.
"""

import math
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import axes, calibration  # noqa: E402


def to_sensor(qx, qy, truth):
    """What a module hanging as `truth` reports for a person at (qx, qy)."""
    a = math.radians(truth["angle"])
    dx, dy = qx - truth["x"], qy - truth["y"]
    x = dx * math.cos(a) + dy * math.sin(a)
    y = -dx * math.sin(a) + dy * math.cos(a)
    return (-x if truth.get("mirror") else x), y


def leg(start, end, truth):
    return {"from": list(start), "to": list(end),
            "start_raw": list(to_sensor(*start, truth)), "end_raw": list(to_sensor(*end, truth))}


# The sensor on the top wall of a 6 x 4 room, looking down the plan.
PLAN = {"x": 3.0, "y": 0.0, "angle": 0.0, "mirror": False}
AWAY = ((3.0, 1.0), (3.0, 3.0))
ACROSS = ((2.0, 2.0), (4.0, 2.0))


def walks(truth, plan=PLAN):
    return axes.verdict(plan, away=leg(*AWAY, truth), across=leg(*ACROSS, truth))


def test_a_sensor_set_up_right_is_left_as_it_is():
    result = walks(PLAN)
    assert result["ok"] and result["mirror"] is False and result["angle"] is None and result["reason"] is None
    assert result["module_angle_deg"] == pytest.approx(0.0) and result["plan_error_deg"] == pytest.approx(0.0)
    assert result["messages"] == ["Links und rechts stimmen, so wie eingestellt."]


def test_a_module_that_counts_x_the_other_way_is_mirrored():
    result = walks({**PLAN, "mirror": True})
    assert result["ok"] and result["mirror"] is True and result["angle"] is None
    assert "gehört eingeschaltet" in result["messages"][0]
    # And the other way round.
    back = axes.verdict({**PLAN, "mirror": True}, away=leg(*AWAY, PLAN), across=leg(*ACROSS, PLAN))
    assert back["mirror"] is False and "gehört aber aus" in back["messages"][0]


@pytest.mark.parametrize("true_angle, mirrored", [(30.0, False), (-35.0, True), (50.0, True), (-15.0, False)])
def test_a_sensor_turned_on_the_plan_is_turned_back_and_then_mirrored_right(true_angle, mirrored):
    """Turned on the plan, the walk away has a sideways part whose sign is
    the mirror's: both are found together."""
    truth = {**PLAN, "angle": true_angle, "mirror": mirrored}
    result = walks(truth)
    assert result["ok"] and result["mirror"] is mirrored
    assert result["plan_error_deg"] == pytest.approx(-true_angle, abs=0.5)
    if abs(true_angle) > axes.PLAN_OFF_DEG:
        assert result["angle"] == round(true_angle / 5) * 5
    else:
        assert result["angle"] is None


def test_a_walk_away_that_comes_closer_proposes_nothing():
    """The sensor hangs on the opposite wall to where the plan has it: the
    walk drawn away from it goes towards it."""
    truth = {"x": 3.0, "y": 4.0, "angle": 180.0, "mirror": False}
    result = walks(truth)
    assert not result["ok"] and result["mirror"] is None and result["angle"] is None and result["reason"] == "away"
    assert abs(result["module_angle_deg"]) == pytest.approx(180.0)
    assert "kleiner geworden" in result["messages"][0] and "an der richtigen Wand" in result["messages"][0]


def test_the_walk_across_alone_decides_the_mirror_when_the_plan_is_right():
    result = axes.verdict(PLAN, across=leg(*ACROSS, {**PLAN, "mirror": True}))
    assert result["mirror"] is True and result["module_angle_deg"] is None


def test_the_walk_away_alone_says_what_is_missing():
    result = axes.verdict(PLAN, away=leg(*AWAY, PLAN))
    assert not result["ok"] and result["reason"] == "across" and result["module_angle_deg"] == pytest.approx(0.0)
    assert result["mirror"] is None and result["angle"] is None


def test_a_walk_that_fits_no_setting_decides_nothing():
    """Walked along the view axis instead of across it: both mirror
    settings put it about as far off."""
    wrong = leg((3.0, 1.0), (3.0, 3.0), PLAN)
    wrong["from"], wrong["to"] = list(ACROSS[0]), list(ACROSS[1])
    result = axes.verdict(PLAN, across=wrong)
    assert result["mirror"] is None and not result["ok"] and result["reason"] == "undecided"
    assert "keiner Einstellung eindeutig" in result["messages"][0]


# --- one walk, as the module reports it ----------------------------------------------


def walk_capture(start, end, seconds=6.0, rate=5.0, noise=0.04, reflector=(1.0, 1.5), seed=1):
    rng = random.Random(seed)
    capture = calibration.Capture(id="c", room_id="r", device_id="d", kind="walk", created=0.0,
                                  delay_s=0.0, duration_s=seconds, walk={"leg": "away", "from": [0, 0], "to": [0, 0]})
    n = int(seconds * rate)
    for i in range(n + 1):
        f = min(1.0, max(0.0, (i / rate - 1.0) / (seconds - 2.0)))  # stands 1 s, walks, stands 1 s
        x = start[0] + (end[0] - start[0]) * f + rng.gauss(0, noise)
        y = start[1] + (end[1] - start[1]) * f + rng.gauss(0, noise)
        targets = [(x, y)] + ([reflector] if reflector and i % 2 == 0 else [])
        capture.reports.append((i / rate, tuple(targets)))
    return capture


def test_a_walk_is_found_among_reflections_and_measured_at_both_ends():
    result = calibration.analyze_walk(walk_capture((0.0, 1.0), (0.2, 3.0)))
    assert result["ok"], result
    assert result["start_raw"] == pytest.approx([0.0, 1.0], abs=0.05)
    assert result["end_raw"] == pytest.approx([0.2, 3.0], abs=0.05)
    assert result["moved_m"] == pytest.approx(math.hypot(0.2, 2.0), abs=0.08)
    # The reflection stands still: nobody else is about.
    assert result["warnings"] == []


def test_a_reflection_flashing_up_just_ahead_does_not_cut_the_walk_in_two():
    """A flash a little ahead of the walker is nearer to where the walker
    steps next than the walker's last position: shared out among targets,
    the walker's reports go to the flash from there on, and the walk falls
    in pieces shorter than a walk."""
    capture = walk_capture((0.0, 1.0), (0.0, 3.0), rate=10.0, noise=0.0, reflector=None)
    flashes = 0
    for k, (at, targets) in enumerate(capture.reports):
        x, y = targets[0]
        if k in (25, 42):  # a third and two thirds of the way
            capture.reports[k] = (at, (targets[0], (x, y + 0.15)))
            flashes += 1
    assert flashes == 2
    result = calibration.analyze_walk(capture)
    assert result["ok"], result
    assert result["start_raw"] == pytest.approx([0.0, 1.0], abs=0.05)
    assert result["end_raw"] == pytest.approx([0.0, 3.0], abs=0.05)
    assert result["moved_m"] == pytest.approx(2.0, abs=0.05)


def test_a_walker_lost_for_a_moment_beside_a_reflection_is_not_taken_for_it():
    """Radar modules lose somebody standing still now and then. Right then
    the reflection half a metre away is the nearest thing to where the
    walker was — it is still not the walker."""
    capture = walk_capture((0.0, 1.0), (0.0, 3.0), rate=5.0, noise=0.0, reflector=None)
    for k, (at, targets) in enumerate(capture.reports):
        walker = () if k in (2, 3, 4) else targets  # lost while standing on A
        capture.reports[k] = (at, walker + ((0.45, 1.0),))
    result = calibration.analyze_walk(capture)
    assert result["ok"], result
    assert result["start_raw"] == pytest.approx([0.0, 1.0], abs=0.05)
    assert result["end_raw"] == pytest.approx([0.0, 3.0], abs=0.05)
    assert result["warnings"] == []


def test_standing_beside_a_reflection_the_walker_does_not_change_places_with_it():
    """On B, half a metre from a radiator. One report misses the radiator
    and carries a flash beside the walker: the radiator's track must not
    reach over and take the walker, leaving them the radiator."""
    capture = walk_capture((-1.0, 2.0), (1.0, 2.0), rate=10.0, noise=0.0, reflector=None)
    for k, (at, targets) in enumerate(capture.reports):
        radiator = ((1.0, 1.5),)
        if k == 50:  # standing on B
            capture.reports[k] = (at, targets + ((0.93, 1.79),))
        else:
            capture.reports[k] = (at, targets + radiator)
    result = calibration.analyze_walk(capture)
    assert result["ok"], result
    assert result["start_raw"] == pytest.approx([-1.0, 2.0], abs=0.05)
    assert result["end_raw"] == pytest.approx([1.0, 2.0], abs=0.05)


def test_a_walker_lost_as_they_set_off_is_found_again_where_they_were_heading():
    """Lost for most of a second as they set off, the walker turns up
    farther on than a track looks; meanwhile the radiator beside the way
    turned up again. The walk goes on where it was heading, not there."""
    capture = walk_capture((0.0, 1.0), (0.0, 3.0), seconds=6.0, rate=5.0, noise=0.0, reflector=None)
    reports = []
    for k, (at, targets) in enumerate(capture.reports):
        walker = () if k in (10, 11, 12) else targets  # 2.0 to 2.4 s, walking
        radiator = ((0.6, 1.9),) if 11 <= k else ()  # first seen at 2.2 s
        reports.append((at, walker + radiator))
    capture.reports = reports
    result = calibration.analyze_walk(capture)
    assert result["ok"], result
    assert result["start_raw"] == pytest.approx([0.0, 1.0], abs=0.05)
    assert result["end_raw"] == pytest.approx([0.0, 3.0], abs=0.05)


def test_a_walk_lost_on_the_way_goes_on_where_it_was_heading():
    """Lost for most of a second while walking, the walker turns up well
    on. Meanwhile a reflection showed up nearer to where they were last
    seen — but off to the side and behind, not where they were going."""
    rate = 10
    reports = []
    for i in range(8 * rate):
        t = i / rate
        f = min(1.0, max(0.0, (t - 1.0) / 2.0))  # 1 m/s from 1 s to 3 s
        targets = [] if 1.5 <= t < 2.3 else [(0.0, 1.0 + 2.0 * f)]
        if t >= 1.6:
            targets.append((0.3, 1.1))
        reports.append((t, tuple(targets)))
    capture = walk_capture((0, 0), (0, 0))
    capture.reports = reports
    result = calibration.analyze_walk(capture)
    assert result["ok"], result
    assert result["start_raw"] == pytest.approx([0.0, 1.0], abs=0.05)
    assert result["end_raw"] == pytest.approx([0.0, 3.0], abs=0.05)


def test_what_moved_far_from_where_a_and_b_are_is_not_taken_for_the_walk():
    """The walker was not seen; their reflection in a side wall was. It
    moves as much, the other way round across — taken for the walk, it
    would swap left and right."""
    capture = walk_capture((-1.0, 2.0), (1.0, 2.0), reflector=None)
    capture.walk = {"leg": "across", "from": [2.0, 2.0], "to": [4.0, 2.0]}
    capture.placement = dict(PLAN)
    capture.reports = [(at, tuple((3.0 - x, 1.15 * y) for x, y in targets)) for at, targets in capture.reports]
    result = calibration.analyze_walk(capture)
    assert not result["ok"] and "vom Sensor entfernt" in result["error"]
    assert "2,2 m" in result["error"]  # where A and B are: sqrt(1 + 4)


def test_ghosts_all_over_the_room_are_not_taken_for_the_end_of_the_walk():
    """Two ghosts somewhere in every report: now and then one lands near
    another, or near the walker. Strung together onto the walk, they
    would carry its end far off."""
    rng = random.Random(0)
    capture = walk_capture((0.0, 1.0), (0.0, 3.0), reflector=None)
    capture.reports = []
    rate, seconds = 25, 16
    for i in range(rate * seconds):
        f = min(1.0, max(0.0, (i / rate - 1.0) / 6.0))  # stands 1 s on A, walks, stands on B
        targets = [(rng.gauss(0, 0.05), 1.0 + 2.0 * f + rng.gauss(0, 0.05))]
        targets += [(rng.uniform(-3, 3), rng.uniform(0.3, 6)) for _ in range(2)]
        capture.reports.append((i / rate, tuple(targets)))
    result = calibration.analyze_walk(capture)
    assert result["ok"], result
    assert result["start_raw"] == pytest.approx([0.0, 1.0], abs=0.05)
    assert result["end_raw"] == pytest.approx([0.0, 3.0], abs=0.05)
    assert result["warnings"] == []


def test_a_reflection_of_the_walk_in_a_wall_is_not_taken_for_it():
    """Walls and windows reflect: the walk's mirror image moves along, even
    a little farther — but it begins and ends farther from the sensor than
    A and B are on the plan, whichever way the axes point."""
    capture = walk_capture((0.0, 1.0), (0.0, 3.0), reflector=None)
    capture.walk = {"leg": "away", "from": [3.0, 1.0], "to": [3.0, 3.0]}
    capture.placement = {**PLAN, "mirror": True, "angle": 25.0}  # the plan is off: no matter
    for k, (at, targets) in enumerate(capture.reports):
        x, y = targets[0]
        capture.reports[k] = (at, targets + ((3.0 - x, 1.2 * y),))  # behind a wall 1.5 m to the side
    result = calibration.analyze_walk(capture)
    assert result["ok"], result
    assert result["start_raw"] == pytest.approx([0.0, 1.0], abs=0.05)
    assert result["end_raw"] == pytest.approx([0.0, 3.0], abs=0.05)
    assert "Spiegelbild" in result["warnings"][0]


# --- many walks in a lively room ----------------------------------------------------


LIVELY_LEGS = {
    "away": ((0.0, 1.0), (0.0, 3.0), [3.0, 1.0], [3.0, 3.0]),
    "across": ((-1.0, 2.0), (1.0, 2.0), [2.0, 2.0], [4.0, 2.0]),
}


def lively_walk(leg: str, rate: float, dropped: float, seed: int) -> calibration.Capture:
    """Somebody standing on A for a second, walking to B in 2.5 s and
    standing there, reported with 6 cm of scatter and missing from a share
    of the reports; a radiator half a metre from where the walk across
    ends, in 60 % of the reports; a flash somewhere in 5 %, and one just
    beside the walker in 5 %."""
    rng = random.Random(seed)
    (ax, ay), (bx, by), a, b = LIVELY_LEGS[leg]
    capture = calibration.Capture(id="c", room_id="r", device_id="d", kind="walk", created=0.0, delay_s=0.0,
                                  duration_s=8.0, walk={"leg": leg, "from": a, "to": b}, placement=dict(PLAN))
    for i in range(int(8 * rate)):
        t = i / rate
        f = min(1.0, max(0.0, (t - 1.0) / 2.5))
        x, y = ax + (bx - ax) * f + rng.gauss(0, 0.06), ay + (by - ay) * f + rng.gauss(0, 0.06)
        targets = [] if rng.random() < dropped else [(x, y)]
        if rng.random() < 0.6:
            targets.append((1.0 + rng.uniform(-0.08, 0.08), 1.5 + rng.uniform(-0.08, 0.08)))
        if rng.random() < 0.05:
            targets.append((rng.uniform(-2, 2), rng.uniform(0.5, 3.5)))
        if rng.random() < 0.05:
            targets.append((x + rng.uniform(-0.3, 0.3), y + rng.uniform(-0.3, 0.3)))
        rng.shuffle(targets)
        capture.reports.append((t, tuple(targets)))
    return capture


def ghostly_walk(leg: str, rate: float, dropped: float, seed: int) -> calibration.Capture:
    """As lively_walk, and two ghosts anywhere in the room in every report."""
    rng = random.Random(seed + 1000)
    capture = lively_walk(leg, rate, dropped, seed)
    capture.reports = [
        (t, targets + tuple((rng.uniform(-3, 3), rng.uniform(0.3, 6)) for _ in range(2)))
        for t, targets in capture.reports
    ]
    return capture


@pytest.mark.parametrize("room, leg, rate, dropped, may_refuse, may_stray", [
    (lively_walk, "away", 10, 0.1, 1, 0), (lively_walk, "across", 10, 0.1, 2, 0),
    (lively_walk, "across", 20, 0.1, 1, 0), (lively_walk, "away", 5, 0.3, 8, 0), (lively_walk, "across", 5, 0.3, 8, 0),
    # Ghosts all over the room in every report: one walk may come out up
    # to 20° off — still far from mistaking left for right.
    (ghostly_walk, "away", 20, 0.1, 3, 1), (ghostly_walk, "across", 20, 0.1, 3, 1),
])
def test_walks_in_a_lively_room_come_out_the_way_they_went(room, leg, rate, dropped, may_refuse, may_stray):
    """Sixty walks each. A walk may be refused now and then — walked again,
    it costs a few seconds — but one that is taken must have gone the way
    it went: taken the wrong way, it decides left and right wrongly.
    `may_stray` walks may be up to 20° off, the rest 10°."""
    (ax, ay), (bx, by), _, _ = LIVELY_LEGS[leg]
    want = math.atan2(bx - ax, by - ay)
    refused, off = 0, []
    for seed in range(60):
        result = calibration.analyze_walk(room(leg, rate, dropped, seed))
        if not result["ok"]:
            refused += 1
            continue
        (sx, sy), (ex, ey) = result["start_raw"], result["end_raw"]
        error = (math.atan2(ex - sx, ey - sy) - want + math.pi) % (2 * math.pi) - math.pi
        off.append(abs(math.degrees(error)))
    assert max(off) <= 20.0 and sum(e > 10.0 for e in off) <= may_stray, sorted(off)[-5:]
    assert refused <= may_refuse


def test_somebody_else_walking_about_is_warned_of():
    capture = walk_capture((0.0, 1.0), (0.0, 3.0), reflector=None)
    for k, (at, targets) in enumerate(capture.reports):
        capture.reports[k] = (at, targets + ((1.5 - 0.1 * k / 3, 2.5),))  # 1 m in the time
    result = calibration.analyze_walk(capture)
    assert result["ok"] and result["moved_m"] == pytest.approx(2.0, abs=0.1)
    assert result["warnings"] and "noch jemand im Raum" in result["warnings"][0]


def test_standing_still_is_no_walk():
    result = calibration.analyze_walk(walk_capture((0.0, 2.0), (0.3, 2.2)))
    assert not result["ok"] and "cm Weg" in result["error"]
    empty = walk_capture((0, 0), (0, 0))
    empty.reports = []
    assert "kaum gemeldet" in calibration.analyze_walk(empty)["error"]


def test_a_walk_across_with_next_to_no_movement_hints_at_the_mounting():
    """On its side the module measures height where it should measure the
    side; walked across, it barely moves."""
    capture = walk_capture((0.0, 2.0), (0.1, 2.05))
    capture.walk = {"leg": "across", "from": [2, 2], "to": [4, 2]}
    result = calibration.analyze_walk(capture)
    assert not result["ok"] and "auf der Seite liegt" in result["error"]
