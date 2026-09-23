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
    }
    data.update(extra)
    return Room.model_validate(data)


@pytest.fixture
def rig():
    clock, links = Clock(), Links()
    engine = RoomEngine(links, clock=clock)
    engine.load([room()], [device()])

    def frame(text, connected=True, age=0.0):
        links.snaps["dev"] = LinkSnapshot(device_id="dev", connected=connected,
                                         frame=parse_frame(text), frame_at=clock.now - age)
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
