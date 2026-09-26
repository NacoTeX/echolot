"""The rules that turn radar frames into room and zone states.

The one that matters most: a room without a current measurement is
*unavailable*, never *empty*. An automation that turns the lights off on
"empty" must not do it because a sensor fell off the Wi-Fi.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.devices import Device, DeviceCreate  # noqa: E402
from app.radar_frame import parse_frame  # noqa: E402
from app.radar_link import LinkSnapshot  # noqa: E402
from app.room_engine import RoomEngine  # noqa: E402
from app.rooms import Room  # noqa: E402


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


def device(quiet_means_empty=False):
    return Device(id="dev", created_at=0, updated_at=0, config=DeviceCreate(
        name="radar", board="esp32c5", wifi_ssid="n", radar_quiet_means_empty=quiet_means_empty))


def room(**extra):
    data = {
        "id": "r1", "name": "Wohnzimmer", "width": 6, "height": 4,
        "sensor": {"device_id": "dev", "x": 3, "y": 0, "angle": 0},
        "zones": [
            {"id": "sofa", "name": "Sofa", "kind": "detect", "points": [[0, 2], [3, 2], [3, 4], [0, 4]], "hold_s": 5},
            {"id": "left", "name": "Links", "kind": "detect", "points": [[0, 0], [3, 0], [3, 4], [0, 4]], "hold_s": 0},
            {"id": "fan", "name": "Ventilator", "kind": "exclude", "points": [[5, 0], [6, 0], [6, 1], [5, 1]]},
        ],
        "hold_s": 10,
        # The zone and availability rules, on raw reports: definition 1's
        # behaviour, which definition 2 keeps with its filters off.
        # tests/test_tracking.py covers the filters.
        "calibration": {"confirm_s": 0, "smoothing": "off"},
    }
    data.update(extra)
    return Room.model_validate(data)


def feed(links, text, at, connected=True, device_id="dev"):
    """A frame line through the same door the live link uses."""
    snap = links.snaps.get(device_id) or LinkSnapshot(device_id=device_id)
    snap.connected = connected
    snap.record(parse_frame(text), at)
    links.snaps[device_id] = snap


@pytest.fixture
def rig():
    clock, links = Clock(), Links()
    engine = RoomEngine(links, clock=clock)
    engine.load([room()], [device()])

    def frame(text, connected=True, age=0.0):
        feed(links, text, clock.now - age, connected=connected)
        return engine.evaluate()[0]

    return engine, clock, links, frame


def zone(result, zone_id):
    return next(z for z in result["zones"] if z["id"] == zone_id)


def test_a_target_counts_for_the_room_and_every_zone_it_is_in(rig):
    _, _, _, frame = rig
    # Sensor at (3, 0) looking down: (-15 dm, 30 dm) -> room (1.5, 3.0).
    result = frame("1|R|1|-15,30")
    assert result["available"] and result["count"] == 1 and result["occupied"]
    assert zone(result, "sofa")["count"] == 1
    assert zone(result, "left")["count"] == 1  # zones may overlap
    assert result["targets"][0]["status"] == "counted"
    assert result["targets"][0]["x"] == pytest.approx(1.5)


def test_targets_behind_the_wall_or_in_an_exclusion_zone_count_nowhere(rig):
    _, _, _, frame = rig
    # (7.0, 1.0) is outside the 6 m room; (5.5, 0.5) is on the fan.
    result = frame("1|R|1|40,10;25,5")
    assert [t["status"] for t in result["targets"]] == ["outside", "excluded"]
    assert result["count"] == 0
    assert all(z["count"] == 0 for z in result["zones"])


def test_the_edge_margin_forgives_a_person_against_the_wall(rig):
    _, _, _, frame = rig
    result = frame("1|R|1|32,10")  # x = 6.2, margin 0.3
    assert result["targets"][0]["status"] == "counted"


def test_exclusion_zones_do_not_become_entities(rig):
    _, _, _, frame = rig
    assert [z["id"] for z in frame("1|R|1|")["zones"]] == ["sofa", "left"]


def test_hold_keeps_a_zone_occupied_and_then_lets_go(rig):
    _, clock, _, frame = rig
    frame("1|R|1|-15,30")
    clock.now += 3
    held = frame("1|R|2|")
    assert zone(held, "sofa")["occupied"] and zone(held, "sofa")["count"] == 0
    assert zone(held, "sofa")["hold_remaining"] == pytest.approx(2.0)
    assert not zone(held, "left")["occupied"]  # hold 0
    assert held["occupied"]  # the room holds for 10 s
    clock.now += 2.5
    released = frame("1|R|3|")
    assert not zone(released, "sofa")["occupied"]


@pytest.mark.parametrize("setup, reason", [
    (dict(connected=False), "offline"),
    (dict(age=3.5), "stale"),
])
def test_no_current_frame_is_unavailable_not_empty(rig, setup, reason):
    _, _, _, frame = rig
    result = frame("1|R|1|", **setup)
    assert result["available"] is False
    assert result["reason"] == reason
    assert result["count"] is None and result["occupied"] is None
    assert all(z["occupied"] is None for z in result["zones"])


def test_an_unknown_radar_is_unavailable(rig):
    _, _, _, frame = rig
    assert frame("1|U|1|")["reason"] == "radar_unknown"


def test_a_quiet_radar_is_unavailable_unless_configured_otherwise(rig):
    engine, _, _, frame = rig
    assert frame("1|Q|1|")["reason"] == "radar_quiet"
    engine.load([room()], [device(quiet_means_empty=True)])
    result = frame("1|Q|1|")
    assert result["available"] and result["count"] == 0 and result["occupied"] is False


def test_an_outage_ends_the_hold_instead_of_carrying_it_across(rig):
    _, clock, _, frame = rig
    frame("1|R|1|-15,30")
    frame("1|R|2|", connected=False)
    clock.now += 1
    back = frame("1|R|3|")
    assert back["occupied"] is False and not zone(back, "sofa")["occupied"]


def test_a_room_without_a_sensor_says_so():
    engine = RoomEngine(Links(), clock=Clock())
    engine.load([room(sensor={"device_id": None, "x": 3, "y": 0})], [])
    assert engine.evaluate()[0]["reason"] == "no_sensor"
    engine.load([room()], [])
    assert engine.evaluate()[0]["reason"] == "device_missing"


def test_a_node_without_a_frame_entity_is_named(rig):
    _, _, links, frame = rig
    frame("1|R|1|")
    links.snaps["dev"].no_frame_entity = True
    engine = rig[0]
    assert engine.evaluate()[0]["reason"] == "no_frame_entity"


def test_mirror_and_rotation_are_applied(rig):
    engine, _, _, frame = rig
    engine.load([room(sensor={"device_id": "dev", "x": 3, "y": 0, "angle": 0, "mirror": True})], [device()])
    result = frame("1|R|1|-15,30")
    assert result["targets"][0]["x"] == pytest.approx(4.5)
    assert zone(result, "sofa")["count"] == 0


def test_listeners_get_every_round(rig):
    engine, _, _, frame = rig
    seen = []
    engine.add_listener(lambda rooms, results: seen.append(results[0]["count"]))
    frame("1|R|1|-15,30")
    frame("1|R|2|")
    assert seen == [1, 0]


def test_the_latest_results_are_kept_for_the_live_map(rig):
    engine, _, _, frame = rig
    frame("1|R|1|-15,30")
    assert engine.latest["r1"]["count"] == 1



def test_with_walls_the_cut_out_corner_does_not_count():
    """A 6 × 4 room with its bottom-left 2 × 1 m missing — the hallway
    behind the wall is on the plan, but not in the room."""
    clock, links = Clock(), Links()
    engine = RoomEngine(links, clock=clock)
    walls = [[0, 0], [6, 0], [6, 4], [2, 4], [2, 3], [0, 3]]
    engine.load([room(outline=walls)], [device()])
    # Sensor at (3, 0) looking down: (-20 dm, 36 dm) -> (1.0, 3.6), 0.6 m
    # into the cut-out; (-20, 32) -> (1.0, 3.2), against its wall.
    feed(links, "1|R|1|-20,36;-20,32", clock.now)
    result = engine.evaluate()[0]
    assert [t["status"] for t in result["targets"]] == ["outside", "counted"]
    assert result["count"] == 1
    # The same report in the plain rectangle counts both.
    engine.load([room()], [device()])
    feed(links, "1|R|2|-20,36;-20,32", clock.now)
    assert engine.evaluate()[0]["count"] == 2



def test_somebody_beside_the_sofa_counts_in_its_zone_within_the_margin():
    """The radar puts a person on the sofa somewhere about it; the margin
    is what lets the zone catch that."""
    clock, links = Clock(), Links()
    engine = RoomEngine(links, clock=clock)
    sofa = {"id": "fsofa", "kind": "sofa", "name": "Sofa", "x": 1, "y": 2.5, "w": 2.2, "h": 0.9, "angle": 0}
    zone = {"id": "zs", "name": "Sofa", "kind": "detect", "furniture_id": "fsofa", "margin_m": 0.3,
            "points": [[0, 0], [1, 0], [1, 1]], "hold_s": 0}
    engine.load([room(furniture=[sofa], zones=[zone])], [device()])
    # Sensor at (3, 0) looking down: (-7 dm, 36 dm) -> (2.3, 3.6), 0.2 m
    # past the sofa's back edge at 3.4: inside a 0.3 m margin, outside none.
    feed(links, "1|R|1|-7,36", clock.now)
    result = engine.evaluate()[0]
    assert result["zones"][0]["count"] == 1
    engine.load([room(furniture=[sofa], zones=[{**zone, "margin_m": 0.0}])], [device()])
    feed(links, "1|R|2|-7,36", clock.now)
    assert engine.evaluate()[0]["zones"][0]["count"] == 0
