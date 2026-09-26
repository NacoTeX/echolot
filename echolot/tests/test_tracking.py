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
        snap = self.links.snaps.setdefault("dev", LinkSnapshot(device_id="dev", connected=True))
        snap.record(parse_frame(f"1|R|{self.seq}|{targets}"), self.clock.now)
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
    # Definition 5: still counted, where last seen — the count does not
    # drop to zero and back. Definition 4 counted 0 here.
    assert statuses(gap) == ["held"] and gap["count"] == 1
    assert next(z for z in gap["zones"] if z["id"] == "sofa")["count"] == 1
    back = rig.report("-14,31")
    assert statuses(back) == ["counted"] and back["count"] == 1


def test_a_held_person_stops_counting_when_the_tracker_forgets_them():
    rig = Rig()
    for _ in range(7):
        rig.report("-15,30")
    counts = [rig.report("")["count"] for _ in range(9)]  # 0.2 s apart, 1.8 s in all
    # Remembered for 1.5 s after the last report that carried them.
    assert counts == [1, 1, 1, 1, 1, 1, 1, 0, 0]
    # Without new reports the engine's timer must not stretch it either.
    for _ in range(7):
        rig.report("-15,30")
    rig.report("")
    assert rig.tick(1.0)["count"] == 1
    assert rig.tick(0.6)["count"] == 0


def test_a_target_never_confirmed_is_not_held():
    rig = Rig()
    rig.report("-15,30")
    gap = rig.report("")
    assert gap["targets"] == [] and gap["count"] == 0


def test_a_held_person_outside_the_walls_or_excluded_is_not_counted():
    rig = Rig()
    for _ in range(7):
        rig.report("25,5")  # (5.5, 0.5): in the fan's exclusion zone
    gap = rig.report("")
    assert statuses(gap) == ["excluded"] and gap["count"] == 0


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
                                "entrances": 0, "assume_present_s": 1800.0,
                                "smoothing": "off", "interference_spots": 0, "range_scale": 1.0, "range_offset_m": 0.0, "azimuth_scale": 1.0, "slant": False}
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
    # The person is gone: held where last seen for the tracker's memory,
    # then not at all — and the reflector never counts.
    results = [rig.report("10,15") for _ in range(12)]
    assert all(t["status"] != "counted" for r in results for t in r["targets"])
    assert [t["id"] for t in results[0]["targets"] if t["status"] == "held"] == [person_id]
    assert all(r["count"] == 0 for r in results[8:])


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


# --- time base (review P0-04) ---------------------------------------------------


def test_a_target_not_reported_for_longer_than_the_ttl_is_a_new_one():
    """Reproduced in the review: reports at t=0 and t=10 at the same spot
    kept id and confirmation although the TTL is 1.5 s."""
    tracker = Tracker()
    tracker.update([(0.0, 2.0)], 0.0, confirm_s=0, alpha=1.0)
    (first,) = tracker.visible()
    assert first.confirmed
    tracker.update([(0.0, 2.0)], 10.0, confirm_s=1.0, alpha=1.0)
    (second,) = tracker.visible()
    assert second.id != first.id and not second.confirmed


def test_heartbeat_repeats_do_not_confirm_a_target():
    """One report, then the firmware repeating its line for 1.2 s: that is
    one report, not seven, and confirms nothing."""
    rig = Rig()
    assert statuses(rig.report("-15,30")) == ["pending"]
    for _ in range(6):
        rig.clock.now += 0.2
        rig.links.snaps["dev"].record(parse_frame(f"1|R|{rig.seq}|-15,30"), rig.clock.now)
        result = rig.engine.evaluate()[0]
    assert statuses(result) == ["pending"] and result["count"] == 0


def test_reports_arriving_between_two_evaluations_are_all_taken_at_their_times():
    """The engine evaluates at most ten times a second; a faster module's
    reports must not be overwritten unread."""
    rig = Rig()
    first = rig.report("-15,30")  # the engine follows the sensor
    assert statuses(first) == ["pending"]
    snap = rig.links.snaps["dev"]
    start = rig.clock.now
    # Somebody walks 1.5 m in a second, 30 cm per report — five reports
    # before the engine looks again.
    for i, x in enumerate((-12, -9, -6, -3, 0), start=1):
        rig.seq += 1
        snap.record(parse_frame(f"1|R|{rig.seq}|{x},30"), start + i * 0.2)
    rig.clock.now = start + 1.0
    result = rig.engine.evaluate()[0]
    # Taken one by one it is one target, followed and confirmed. Taking
    # only the latest would see a 1.5 m jump — past the gate, a new target.
    assert statuses(result) == ["counted"]
    assert result["targets"][0]["id"] == first["targets"][0]["id"]


def test_a_fresh_start_takes_the_current_measurement_not_the_backlog():
    rig = Rig()
    snap = rig.links.snaps.setdefault("dev", LinkSnapshot(device_id="dev", connected=True))
    for i in range(6):
        rig.seq += 1
        snap.record(parse_frame(f"1|R|{rig.seq}|-15,30"), rig.clock.now + i * 0.2)
    rig.clock.now += 1.0
    assert statuses(rig.engine.evaluate()[0]) == ["pending"]


def test_a_new_connection_confirms_afresh():
    rig = Rig()
    for _ in range(7):
        rig.report("-15,30")
    assert rig.report("-15,30")["count"] == 1
    rig.links.snaps["dev"].new_session()
    rig.seq = 0
    assert statuses(rig.report("-15,30")) == ["pending"]


# --- entrances ------------------------------------------------------------------
#
# Sensor at (3, 0) looking down: sensor (x, y) dm is room (3 + x/10, y/10).
# The sofa is at room (1.5, 3.0) = sensor "-15,30"; the door on the right
# wall at room (5.5, 1.5) = sensor "25,15".

DOOR = {"id": "door", "name": "Tür", "kind": "entry", "points": [[5.2, 1.0], [6, 1.0], [6, 2.2], [5.2, 2.2]]}


def with_door(**extra):
    base = room()
    zones = [z.model_dump() for z in base.zones] + [DOOR]
    return room(zones=zones, **extra)


def seated(rig, at="-15,30", times=7):
    for _ in range(times):
        result = rig.report(at)
    assert result["count"] == at.count(";") + 1
    return result


def walk(rig, start, end, steps=8):
    """Half a metre or less per report, as a person walks."""
    (x0, y0), (x1, y1) = start, end
    for i in range(1, steps + 1):
        result = rig.report(f"{round(x0 + (x1 - x0) * i / steps)},{round(y0 + (y1 - y0) * i / steps)}")
    return result


def lost(rig, rounds=9):
    """The radar reports nothing for a while — 1.8 s, past the tracker's memory."""
    for _ in range(rounds):
        result = rig.report("")
    return result


def test_somebody_leaving_through_the_door_leaves_the_room_empty():
    rig = Rig(with_door())
    seated(rig)
    assert walk(rig, (-15, 30), (25, 15))["count"] == 1  # to the door, one target all the way
    result = lost(rig)
    assert result["count"] == 0 and result["occupied"] is False
    assert result["assumed_present"] is False and result["unaccounted"] == 0


def test_somebody_lost_on_the_sofa_is_assumed_to_be_still_there():
    rig = Rig(with_door(assume_present_s=600))
    seated(rig)
    result = lost(rig)
    # Counted as measured — nobody — but the room stays occupied.
    assert result["count"] == 0 and result["occupied"] is True
    assert result["assumed_present"] is True and result["unaccounted"] == 1
    assert 595 < result["assumed_remaining"] <= 600
    # Seen again on the sofa: found, and counted as before.
    back = seated(rig)
    assert back["assumed_present"] is False and back["unaccounted"] == 0 and back["occupied"] is True


def test_the_assumption_ends_after_its_time():
    rig = Rig(with_door(assume_present_s=60))
    seated(rig)
    lost(rig)  # 1.8 s
    # The module goes on reporting an empty room, once a second.
    for _ in range(40):
        result = rig.report("", step=1.0)
    assert result["occupied"] is True
    for _ in range(20):
        result = rig.report("", step=1.0)
    assert result["occupied"] is False and result["assumed_present"] is False and result["unaccounted"] == 0


def test_a_newcomer_at_the_door_does_not_find_the_one_lost_on_the_sofa():
    rig = Rig(with_door())
    seated(rig)
    lost(rig)
    arrived = seated(rig, at="25,15")  # confirmed at the door
    assert arrived["count"] == 1 and arrived["unaccounted"] == 1
    walk(rig, (25, 15), (15, 20), steps=3)  # a few steps in, and out again
    walk(rig, (15, 20), (25, 15), steps=3)
    gone = lost(rig)
    assert gone["occupied"] is True and gone["unaccounted"] == 1


def test_a_newcomer_counts_as_new_even_once_past_the_door():
    """Confirmation takes a second; somebody walking in is well into the
    room by then. Where they were first reported decides."""
    rig = Rig(with_door())
    seated(rig)
    lost(rig)
    arrived = walk(rig, (25, 15), (5, 25), steps=7)
    assert arrived["count"] == 1 and arrived["targets"][0]["x"] < 5.2  # counted past the door
    assert arrived["unaccounted"] == 1 and arrived["occupied"] is True


def test_leaving_is_judged_where_last_reported_not_where_smoothing_lags():
    rig = Rig(with_door(calibration={"confirm_s": 1.0, "smoothing": "strong"}))
    seated(rig, times=12)
    out = walk(rig, (-15, 30), (25, 15), steps=24)
    assert out["count"] == 1 and out["targets"][0]["x"] < 5.2  # the smoothed target is short of the door
    result = lost(rig)
    assert result["occupied"] is False and result["unaccounted"] == 0


def test_without_an_entrance_or_with_the_time_at_0_nothing_is_assumed():
    for the_room in (room(), with_door(assume_present_s=0)):
        rig = Rig(the_room)
        seated(rig)
        result = lost(rig)
        assert result["occupied"] is False and result["assumed_present"] is False


def test_a_sensor_outage_counts_as_not_seen_leaving():
    rig = Rig(with_door())
    seated(rig)
    down = rig.tick(4)  # no frame for over three seconds
    assert down["available"] is False and down["unaccounted"] == 1
    up = rig.report("")
    assert up["available"] and up["occupied"] is True and up["assumed_present"] is True


def test_a_restarted_tracker_does_not_mistake_new_ids_for_old_ones():
    rig = Rig(with_door())
    seated(rig)
    # A new confirmation time starts the tracker afresh; its ids start over.
    rig.engine.load([with_door(calibration={"confirm_s": 0.5, "smoothing": "off"})], [device()])
    result = rig.report("-15,30")
    assert result["unaccounted"] == 1  # the one counted before, not seen leaving
    for _ in range(3):
        result = rig.report("-15,30")
    assert result["count"] == 1 and result["unaccounted"] == 0  # found again


def test_a_new_tracker_s_first_target_is_not_the_old_one_with_the_same_id():
    # Without a confirmation time a target counts with its first report.
    rig = Rig(with_door(calibration={"confirm_s": 0.0, "smoothing": "off"}))
    assert rig.report("-15,30")["targets"][0]["id"] == 1  # on the sofa
    # A new connection starts a new tracker, whose ids start over: its
    # first target is id 1 again — somebody at the door, not the one on
    # the sofa, who was not seen leaving.
    rig.links.snaps["dev"].new_session()
    rig.seq = 0
    result = rig.report("25,15")
    assert result["targets"][0]["id"] == 1 and result["count"] == 1
    assert result["unaccounted"] == 1 and result["occupied"] is True


def test_the_time_runs_from_the_latest_one_to_vanish():
    rig = Rig(with_door(assume_present_s=60))
    seated(rig, at="-15,30;5,25")
    lost_one = [rig.report("5,25") for _ in range(9)][-1]  # the sofa one is lost
    assert lost_one["count"] == 1 and lost_one["unaccounted"] == 1
    for _ in range(40):
        rig.report("5,25", step=1.0)
    lost(rig)  # the other one too, 40 s later
    for _ in range(30):
        result = rig.report("", step=1.0)
    assert result["occupied"] is True and result["unaccounted"] == 2
    for _ in range(30):
        result = rig.report("", step=1.0)
    assert result["occupied"] is False and result["unaccounted"] == 0


def test_somebody_can_say_the_room_is_empty():
    rig = Rig(with_door())
    seated(rig)
    lost(rig)
    assert rig.engine.clear_presence("r1") is True
    assert rig.tick(0.1)["occupied"] is False
    assert rig.engine.clear_presence("r1") is False  # nothing left to clear


def test_an_entrance_is_no_detection_zone():
    rig = Rig(with_door())
    at_door = seated(rig, at="25,15")
    assert at_door["targets"][0]["zones"] == [] and at_door["count"] == 1
    assert [z["id"] for z in at_door["zones"]] == ["sofa"]
