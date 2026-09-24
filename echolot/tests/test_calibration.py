"""Recording a room: the empty-room run and the standpoint."""

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.calibration import Capture, Captures, analyze_empty, analyze_point  # noqa: E402
from app.radar_frame import parse_frame  # noqa: E402
from app.radar_link import LinkSnapshot  # noqa: E402
from app.rooms import InterferenceSpot, Room  # noqa: E402


def capture(kind="empty", reports=(), quiet=0, spots=()):
    c = Capture(id="c", room_id="r1", device_id="dev", kind=kind, created=0, delay_s=0, duration_s=45,
                spots=list(spots))
    c.reports = [(i * 0.2, tuple(targets)) for i, targets in enumerate(reports)]
    c.quiet = quiet
    return c


def test_a_reflector_becomes_a_spot_and_a_flash_does_not():
    rng = random.Random(4)
    reports = []
    for i in range(200):
        targets = []
        if rng.random() < 0.6:  # the radiator, jittering
            targets.append((1.0 + rng.uniform(-0.1, 0.1), 1.5 + rng.uniform(-0.1, 0.1)))
        if i in (17, 90):  # two flashes somewhere else
            targets.append((-2.0, 3.0 + i / 100))
        reports.append(targets)
    result = analyze_empty(capture(reports=reports))
    assert result["ok"] and result["moved"] == 0
    (spot,) = result["spots"]
    assert spot["x"] == pytest.approx(1.0, abs=0.05) and spot["y"] == pytest.approx(1.5, abs=0.05)
    assert 0.3 <= spot["r"] <= 0.5
    assert spot["share"] == pytest.approx(0.6, abs=0.08)


def test_somebody_walking_through_is_not_learned_and_is_mentioned():
    reports = [[(-2.0 + i * 0.1, 2.0)] for i in range(40)] + [[] for _ in range(100)]
    result = analyze_empty(capture(reports=reports))
    assert result["ok"] and result["spots"] == []
    assert result["moved"] == 1
    assert any("bewegt" in w for w in result["warnings"])


def test_a_module_that_falls_silent_in_the_empty_room_is_reported_as_such():
    result = analyze_empty(capture(reports=[], quiet=40))
    assert result["ok"] and result["spots"] == []
    assert (result["receiving"], result["quiet"], result["empty_reports"]) == (0, 40, 0)


def test_empty_reports_are_counted_apart_from_silence():
    result = analyze_empty(capture(reports=[[]] * 30, quiet=0))
    assert (result["receiving"], result["quiet"], result["empty_reports"]) == (30, 0, 30)


def test_too_few_reports_is_no_result():
    result = analyze_empty(capture(reports=[[], []]))
    assert not result["ok"] and "Meldungen" in result["error"]


def test_a_standpoint_is_the_median_of_the_reports():
    reports = [[(0.5 + d, 2.0 - d)] for d in (-0.05, 0.0, 0.02, 0.0, 0.4, -0.02, 0.01)]
    result = analyze_point(capture("point", reports=reports))
    assert result["ok"]
    assert result["raw"] == pytest.approx([0.5, 2.0], abs=0.02)


def test_a_standpoint_ignores_learned_reflectors():
    spot = InterferenceSpot(x=-1.0, y=1.0, r=0.4)
    reports = [[(-1.0, 1.0), (0.5, 2.0)] for _ in range(10)]
    result = analyze_point(capture("point", reports=reports, spots=[spot]))
    assert result["raw"] == pytest.approx([0.5, 2.0])
    assert result["warnings"] == []


def test_a_second_person_is_mentioned():
    reports = [[(0.5, 2.0), (-1.5, 3.0)] for _ in range(10)]
    result = analyze_point(capture("point", reports=reports))
    assert result["ok"] and any("noch jemand" in w for w in result["warnings"])


def test_nobody_at_the_standpoint_is_an_error():
    result = analyze_point(capture("point", reports=[[]] * 10))
    assert not result["ok"] and "niemanden" in result["error"]


def test_somebody_seen_now_and_then_is_not_a_standpoint():
    reports = [[(0.5, 2.0)] if i % 4 == 0 else [] for i in range(20)]
    result = analyze_point(capture("point", reports=reports))
    assert not result["ok"] and "stabil" in result["error"]


# --- the live part -----------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Links:
    def __init__(self):
        self.snaps = {}

    def snapshot(self, device_id):
        return self.snaps.get(device_id)


def room(device_id="dev"):
    return Room.model_validate({"id": "r1", "name": "Wohnzimmer", "width": 6, "height": 4,
                                "sensor": {"device_id": device_id, "x": 3, "y": 0}})


@pytest.fixture
def rig():
    clock, links = Clock(), Links()
    caps = Captures(links, clock=clock)

    def report(text, device_id="dev"):
        links.snaps[device_id] = LinkSnapshot(device_id=device_id, connected=True,
                                              frame=parse_frame(text), frame_at=clock.now)
        caps.on_frame(device_id)

    return caps, clock, report


def test_a_capture_records_only_between_its_delay_and_its_end(rig):
    caps, clock, report = rig
    cap = caps.start(room(), "point", delay_s=3, duration_s=5)
    report("1|R|1|5,20")
    assert caps.view(cap)["phase"] == "waiting" and cap.reports == []
    clock.now += 3
    for i in range(10):
        report(f"1|R|{i + 2}|5,20")
        clock.now += 0.4
    report("1|R|99|5,20", device_id="other")  # another sensor's report
    view = caps.view(cap)
    assert view["phase"] == "recording" and len(cap.reports) == 10
    clock.now += 1.1
    report("1|R|100|5,20")  # after the end
    view = caps.view(cap)
    assert view["phase"] == "done" and len(cap.reports) == 10
    assert view["result"]["ok"] and view["result"]["raw"] == pytest.approx([0.5, 2.0])


def test_quiet_reports_are_counted_not_recorded(rig):
    caps, clock, report = rig
    cap = caps.start(room(), "empty", delay_s=0, duration_s=10)
    report("1|Q|1|")
    report("1|R|2|")
    assert cap.quiet == 1 and len(cap.reports) == 1


def test_a_new_capture_replaces_the_running_one_and_cancel_ends_it(rig):
    caps, _, _ = rig
    first = caps.start(room(), "empty")
    second = caps.start(room(), "point")
    assert caps.get("r1") is second and first is not second
    assert caps.cancel("r1") and caps.get("r1") is None
    assert not caps.cancel("r1")


def test_a_room_without_a_sensor_cannot_be_recorded(rig):
    caps, _, _ = rig
    with pytest.raises(ValueError):
        caps.start(room(device_id=None), "empty")


def test_limits_are_enforced(rig):
    caps, _, _ = rig
    with pytest.raises(ValueError):
        caps.start(room(), "empty", duration_s=1)
    with pytest.raises(ValueError):
        caps.start(room(), "nonsense")
