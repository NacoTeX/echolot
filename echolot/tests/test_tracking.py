"""Measurement definition 2: targets followed from report to report.

What it has to get right: a reflection that flashes up does not count, a
reflector learned in the empty room does not count, a person does — and
with the filters off the answers are exactly those of definition 1.
"""

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import geometry  # noqa: E402
from app.devices import Device, DeviceCreate  # noqa: E402
from app.radar_frame import parse_frame  # noqa: E402
from app.radar_link import LinkSnapshot  # noqa: E402
from app.room_engine import MEASUREMENT_VERSION, RoomEngine  # noqa: E402
from app.rooms import InterferenceSpot, Room  # noqa: E402
from app.tracking import Tracker  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Links:
    def __init__(self):
        self.snaps = {}

    def snapshot(self, device_id):
        return self.snaps.get(device_id)


def device():
    return Device(id="dev", created_at=0, updated_at=0,
                  config=DeviceCreate(name="radar", board="esp32c5", wifi_ssid="n"))


def room(calibration=None, **extra):
    data = {
        "id": "r1", "name": "Wohnzimmer", "width": 6, "height": 4,
        "sensor": {"device_id": "dev", "x": 3, "y": 0, "angle": 0},
        "zones": [
            {"id": "sofa", "name": "Sofa", "kind": "detect", "points": [[0, 2], [3, 2], [3, 4], [0, 4]], "hold_s": 0},
            {"id": "fan", "name": "Ventilator", "kind": "exclude", "points": [[5, 0], [6, 0], [6, 1], [5, 1]]},
        ],
        "hold_s": 0,
        "calibration": {"confirm_s": 1.0, "smoothing": "off", **(calibration or {})},
    }
    data.update(extra)
    return Room.model_validate(data)


class Rig:
    """An engine fed report by report, 0.2 s apart unless told otherwise."""

    def __init__(self, the_room=None):
        self.clock, self.links = Clock(), Links()
        self.engine = RoomEngine(self.links, clock=self.clock)
        self.engine.load([the_room or room()], [device()])
        self.seq = 0

    def report(self, targets="", step=0.2):
        self.clock.now += step
        self.seq += 1
        self.links.snaps["dev"] = LinkSnapshot(
            device_id="dev", connected=True,
            frame=parse_frame(f"1|R|{self.seq}|{targets}"), frame_at=self.clock.now,
        )
        return self.engine.evaluate()[0]

    def tick(self, step):
        """Time passes, no new report."""
        self.clock.now += step
        return self.engine.evaluate()[0]


def statuses(result):
    return [t["status"] for t in result["targets"]]


def test_a_person_counts_once_reported_for_the_confirmation_time():
    rig = Rig()
    # Sensor at (3, 0) looking down: (-15 dm, 30 dm) is (1.5, 3.0) on the sofa.
    first = rig.report("-15,30")
    assert statuses(first) == ["pending"] and first["count"] == 0 and not first["occupied"]
    for _ in range(4):
        assert rig.report("-15,30")["count"] == 0  # 0.2 … 0.8 s
    confirmed = rig.report("-15,30")  # 1.0 s
    assert statuses(confirmed) == ["counted"]
    assert confirmed["count"] == 1
    assert next(z for z in confirmed["zones"] if z["id"] == "sofa")["count"] == 1


def test_a_reflection_that_flashes_up_never_counts():
    rig = Rig()
    results = [rig.report("-15,30"), rig.report("-15,30"), rig.report(""), rig.report(""),
               rig.report("10,20"), rig.report(""), rig.report(""), rig.report(""), rig.report("")]
    assert all(r["count"] == 0 for r in results)
    assert not any(r["occupied"] for r in results)


def test_only_a_new_report_moves_the_confirmation_on():
    """The engine also runs on a timer; that must not confirm anybody."""
    rig = Rig()
    rig.report("-15,30")
    # 2.5 s later the report is still current (stale after 3 s), but it is
    # the same single report.
    later = rig.tick(2.5)
    assert later["available"] and statuses(later) == ["pending"]


def test_a_confirmed_person_survives_a_dropped_report_without_starting_over():
    rig = Rig()
    for _ in range(6):
        rig.report("-15,30")
    assert rig.report("-15,30")["count"] == 1
    gap = rig.report("")  # the module lost the person for one report
    assert gap["count"] == 0 and gap["targets"] == []
    back = rig.report("-14,31")
    assert statuses(back) == ["counted"] and back["count"] == 1


def test_a_target_seen_in_only_every_third_report_is_not_confirmed():
    rig = Rig()
    for _ in range(6):
        rig.report("-15,30")
        rig.report("")
        result = rig.report("")
        assert result["count"] == 0
    assert rig.report("-15,30")["count"] == 0


def learned(spots, device_id="dev"):
    return {"interference": [s.model_dump() for s in spots], "interference_device_id": device_id}


def test_a_target_appearing_in_a_learned_spot_never_counts_there():
    # A radiator reported at sensor (1.0 m, 1.5 m) -> room (2.0, 1.5).
    rig = Rig(room({**learned([InterferenceSpot(x=1.0, y=1.5, r=0.4)])}))
    for _ in range(15):
        result = rig.report("10,15")
        assert statuses(result) == ["interference"] and result["count"] == 0
    # Its jitter stays inside the spot and changes nothing.
    assert statuses(rig.report("12,17")) == ["interference"]


def test_a_person_who_walks_into_a_learned_spot_keeps_counting():
    rig = Rig(room({**learned([InterferenceSpot(x=1.0, y=1.5, r=0.4)])}))
    for _ in range(6):
        rig.report("10,25")  # a metre further in, outside the spot
    assert rig.report("10,25")["count"] == 1
    for y in (22, 19, 16, 15, 15):  # walking into the spot
        result = rig.report(f"10,{y}")
        assert statuses(result) == ["counted"] and result["count"] == 1


def test_a_target_that_leaves_the_spot_is_confirmed_from_then_on():
    rig = Rig(room({**learned([InterferenceSpot(x=1.0, y=1.5, r=0.4)])}))
    for _ in range(10):
        rig.report("10,15")
    rig.report("10,21")
    for _ in range(4):
        assert rig.report("10,22")["count"] == 0
    assert rig.report("10,22")["count"] == 1


def test_spots_learned_with_another_sensor_do_not_apply():
    rig = Rig(room({**learned([InterferenceSpot(x=1.0, y=1.5, r=0.4)], device_id="other")}))
    for _ in range(5):
        rig.report("10,15")
    result = rig.report("10,15")
    assert statuses(result) == ["counted"]
    assert result["filter"]["interference_spots"] == 0


def test_smoothing_moves_a_target_part_of_the_way():
    tracker = Tracker()
    tracker.update([(0.0, 2.0)], 0.0, confirm_s=0, alpha=0.5)
    tracker.update([(0.4, 2.0)], 0.2, confirm_s=0, alpha=0.5)
    (track,) = tracker.visible()
    assert track.x == pytest.approx(0.2)
    tracker.update([(0.4, 2.0)], 0.4, confirm_s=0, alpha=0.5)
    assert tracker.visible()[0].x == pytest.approx(0.3)


def test_two_people_keep_their_own_targets_when_the_module_swaps_the_order():
    tracker = Tracker()
    tracker.update([(-1.0, 2.0), (1.0, 2.0)], 0.0, confirm_s=0, alpha=1.0)
    ids = {round(t.x): t.id for t in tracker.visible()}
    tracker.update([(1.1, 2.0), (-1.1, 2.0)], 0.2, confirm_s=0, alpha=1.0)
    assert {round(t.x): t.id for t in tracker.visible()} == ids


def test_moving_the_sensor_on_the_plan_does_not_start_confirmation_over():
    """Targets live in sensor coordinates; the plan only draws them."""
    rig = Rig()
    for _ in range(6):
        rig.report("-15,30")
    assert rig.report("-15,30")["count"] == 1
    rig.engine.load([room(sensor={"device_id": "dev", "x": 3, "y": 0, "angle": 20})], [device()])
    assert rig.report("-15,30")["count"] == 1


def test_an_outage_starts_confirmation_over():
    rig = Rig()
    for _ in range(7):
        rig.report("-15,30")
    assert rig.tick(3.5)["available"] is False  # stale
    assert statuses(rig.report("-15,30")) == ["pending"]


def test_every_result_names_its_rules():
    rig = Rig()
    result = rig.report("")
    assert result["filter"] == {"definition_version": MEASUREMENT_VERSION, "confirm_s": 1.0,
                                "smoothing": "off", "interference_spots": 0}
    assert rig.tick(5)["filter"]["definition_version"] == MEASUREMENT_VERSION


def reference_v1(the_room, points):
    """Definition 1, as Echolot 1.0 computed it on one raw report."""
    placement = the_room.sensor.model_dump()
    detect = [z for z in the_room.zones if z.kind == "detect"]
    exclude = [z for z in the_room.zones if z.kind == "exclude"]
    out = []
    for raw_x, raw_y in points:
        x, y = geometry.to_room(raw_x, raw_y, placement)
        if not geometry.in_room(x, y, the_room.width, the_room.height, the_room.edge_margin_m):
            out.append((round(x, 3), round(y, 3), "outside", ()))
        elif any(geometry.point_in_polygon(x, y, z.points) for z in exclude):
            out.append((round(x, 3), round(y, 3), "excluded", ()))
        else:
            out.append((round(x, 3), round(y, 3), "counted",
                        tuple(z.id for z in detect if geometry.point_in_polygon(x, y, z.points))))
    return sorted(out)


def test_with_the_filters_off_definition_2_answers_like_definition_1():
    the_room = room({"confirm_s": 0, "smoothing": "off"})
    rig = Rig(the_room)
    rng = random.Random(7)
    for _ in range(400):
        points = [(rng.randint(-45, 45), rng.randint(-5, 50)) for _ in range(rng.randint(0, 5))]
        result = rig.report(";".join(f"{x},{y}" for x, y in points))
        expected = reference_v1(the_room, [(x / 10, y / 10) for x, y in points])
        got = sorted((t["x"], t["y"], t["status"], tuple(t["zones"])) for t in result["targets"])
        assert got == expected
        assert result["count"] == sum(1 for e in expected if e[2] == "counted")


def test_a_reflector_confirmed_before_its_spot_was_learned_stops_counting():
    """Seen in the browser run: the radiator had been counting for minutes
    when the empty-room recording learned it — and went on counting."""
    rig = Rig()
    for _ in range(8):
        rig.report("10,15")
    assert rig.report("10,15")["count"] == 1  # the reflector, confirmed
    rig.engine.load([room({**learned([InterferenceSpot(x=1.0, y=1.5, r=0.4)])})], [device()])
    for _ in range(10):
        result = rig.report("10,15")
        assert statuses(result) == ["interference"] and result["count"] == 0


def test_somebody_leaving_a_spot_takes_their_target_along():
    """The reflector keeps reporting where the person stood; the person's
    confirmed target must follow the person, not stay on the reflector."""
    rig = Rig(room({**learned([InterferenceSpot(x=1.0, y=1.5, r=0.4)])}))
    for _ in range(7):
        rig.report("10,25")
    for y in (20, 16, 15):  # into the spot, where the reflector also is
        rig.report(f"10,{y}")
    person_id = next(t["id"] for t in rig.report("10,15")["targets"] if t["status"] == "counted")
    # Out again, while the reflector goes on at (1.0, 1.5).
    for y in (19, 23, 27, 30):
        result = rig.report(f"10,15;10,{y}")
        by_id = {t["id"]: t for t in result["targets"]}
        assert by_id[person_id]["raw_y"] == pytest.approx(y / 10)
        assert result["count"] == 1
    for _ in range(10):
        assert rig.report("10,15")["count"] == 0


def test_a_confirmed_target_that_stays_in_a_spot_stops_counting_after_a_while():
    """The bound on the benefit of the doubt: whatever sits on a reflector
    for good is counted for SPOT_TRUST_S at most."""
    from app.tracking import SPOT_TRUST_S

    rig = Rig(room({**learned([InterferenceSpot(x=1.0, y=1.5, r=0.4)])}))
    for _ in range(7):
        rig.report("10,25")
    for y in (20, 16):
        rig.report(f"10,{y}")
    steps = int(SPOT_TRUST_S / 0.2)
    counts = [rig.report("10,15")["count"] for _ in range(steps + 3)]
    assert counts[0] == 1 and counts[steps - 3] == 1
    assert counts[-1] == 0
    assert statuses(rig.report("10,15")) == ["interference"]


def test_a_reflector_that_jitters_out_of_its_spot_for_a_moment_is_not_confirmed():
    """Its confirmation run starts outside; back inside, it has to end."""
    rig = Rig(room({**learned([InterferenceSpot(x=1.0, y=1.5, r=0.4)])}))
    rig.report("10,20")
    rig.report("10,20")  # 0.5 m from the centre: just outside
    for _ in range(10):
        result = rig.report("10,15")
        assert statuses(result) == ["interference"] and result["count"] == 0
