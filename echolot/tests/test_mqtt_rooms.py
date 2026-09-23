"""Rooms and zones as Home Assistant entities, and the ones that must go.

A fake paho client records what would reach the broker. The bridge is
exercised as it runs: discovery first, then state, then availability,
only what changed — and deletions queued on disk until acknowledged.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import mqtt_bridge  # noqa: E402
from app.rooms import Room  # noqa: E402


class Info:
    """Like paho's MQTTMessageInfo: acknowledged whenever the broker says."""

    def __init__(self, client):
        self.rc = 0
        self.client = client

    def is_published(self):
        return self.client.ack


class FakeClient:
    def __init__(self):
        self.messages = []
        self.ack = True

    def publish(self, topic, payload, retain=False, qos=0):
        self.messages.append((topic, payload, retain, qos))
        return Info(self)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(tmp_path))
    bridge = mqtt_bridge.Bridge()
    client = FakeClient()
    bridge._client = client
    bridge.connected = True
    return bridge, client, tmp_path


def make_room(zones=None, room_id="r1", name="Wohnzimmer"):
    return Room.model_validate({
        "id": room_id, "name": name, "width": 5, "height": 4,
        "sensor": {"device_id": "dev", "x": 2.5, "y": 0},
        "zones": zones if zones is not None else [
            {"id": "zsofa", "name": "Sofa", "kind": "detect", "points": [[0, 2], [2, 2], [2, 4], [0, 4]]},
            {"id": "zfan", "name": "Fan", "kind": "exclude", "points": [[4, 0], [5, 0], [5, 1]]},
        ],
    })


def result(available=True, count=1, occupied=True, zone_count=1, zone_occupied=True, room_id="r1"):
    return {"room_id": room_id, "available": available, "count": count if available else None,
            "occupied": occupied if available else None,
            "zones": [{"id": "zsofa", "count": zone_count, "occupied": zone_occupied}] if available else
                     [{"id": "zsofa", "count": None, "occupied": None}]}


def topics(client):
    return [m[0] for m in client.messages]


def payload_of(client, topic):
    return [m[1] for m in client.messages if m[0] == topic][-1]


def test_a_room_becomes_two_entities_and_each_detect_zone_two_more(env):
    bridge, client, _ = env
    mqtt_bridge.RoomPublisher(bridge).publish([make_room()], [result()])
    discovery = [t for t in topics(client) if t.endswith("/config")]
    assert discovery == [
        "homeassistant/binary_sensor/echolot/room_r1_occupancy/config",
        "homeassistant/sensor/echolot/room_r1_count/config",
        "homeassistant/binary_sensor/echolot/room_r1_zone_zsofa_occupancy/config",
        "homeassistant/sensor/echolot/room_r1_zone_zsofa_count/config",
    ]
    config = json.loads(payload_of(client, discovery[0]))
    assert config["device_class"] == "occupancy"
    assert config["device"]["identifiers"] == ["echolot_room_r1"]
    assert config["device"]["suggested_area"] == "Wohnzimmer"
    assert config["availability_mode"] == "all"
    assert json.loads(payload_of(client, discovery[3]))["name"] == "Sofa Personen"


def test_states_then_availability(env):
    bridge, client, _ = env
    mqtt_bridge.RoomPublisher(bridge).publish([make_room()], [result()])
    assert payload_of(client, "echolot/room_r1_occupancy/state") == "ON"
    assert payload_of(client, "echolot/room_r1_count/state") == "1"
    assert payload_of(client, "echolot/room_r1_zone_zsofa_occupancy/state") == "ON"
    assert payload_of(client, "echolot/room_r1_zone_zsofa_count/state") == "1"
    assert topics(client)[-1] == "echolot/room/r1/availability"
    assert payload_of(client, "echolot/room/r1/availability") == "online"


def test_an_unavailable_room_says_offline_and_publishes_no_state(env):
    bridge, client, _ = env
    mqtt_bridge.RoomPublisher(bridge).publish([make_room()], [result(available=False)])
    assert payload_of(client, "echolot/room/r1/availability") == "offline"
    assert not any(t.endswith("/state") for t in topics(client))


def test_nothing_is_sent_twice(env):
    bridge, client, _ = env
    publisher = mqtt_bridge.RoomPublisher(bridge)
    publisher.publish([make_room()], [result()])
    before = len(client.messages)
    publisher.publish([make_room()], [result()])
    assert len(client.messages) == before
    publisher.publish([make_room()], [result(count=2)])
    assert client.messages[before:] == [("echolot/room_r1_count/state", "2", True, 0)]


def test_a_reconnect_republishes_everything(env):
    bridge, client, _ = env
    publisher = mqtt_bridge.RoomPublisher(bridge)
    publisher.publish([make_room()], [result()])
    bridge._sent.clear()  # what on_connect does
    before = len(client.messages)
    publisher.publish([make_room()], [result()])
    assert len(client.messages) - before == 9


def test_a_removed_zone_is_deleted_and_stays_queued_until_acked(env):
    bridge, client, tmp_path = env
    publisher = mqtt_bridge.RoomPublisher(bridge)
    publisher.publish([make_room()], [result()])
    client.ack = False
    publisher.publish([make_room(zones=[])], [result()])
    deletes = [m for m in client.messages if m[1] == "" and m[3] == 1]
    assert {m[0] for m in deletes} >= {
        "homeassistant/binary_sensor/echolot/room_r1_zone_zsofa_occupancy/config",
        "echolot/room_r1_zone_zsofa_occupancy/state",
    }
    stored = json.loads((tmp_path / "mqtt_entities.json").read_text())
    assert "room_r1_zone_zsofa_occupancy" in stored["tombstones"]
    # A restart does not lose the queue.
    again = mqtt_bridge.RoomPublisher(mqtt_bridge.Bridge())
    assert "room_r1_zone_zsofa_count" in again.tombstones
    client.ack = True
    publisher.publish([make_room(zones=[])], [result()])
    assert publisher.tombstones == {}
    assert "room_r1_zone_zsofa_occupancy" not in json.loads((tmp_path / "mqtt_entities.json").read_text())["announced"]


def test_a_deleted_room_takes_its_availability_topic_along(env):
    bridge, client, _ = env
    publisher = mqtt_bridge.RoomPublisher(bridge)
    publisher.publish([make_room()], [result()])
    publisher.publish([], [])
    cleared = {m[0] for m in client.messages if m[1] == ""}
    assert "echolot/room/r1/availability" in cleared


def test_a_zone_that_comes_back_is_not_deleted(env):
    bridge, client, _ = env
    publisher = mqtt_bridge.RoomPublisher(bridge)
    publisher.publish([make_room()], [result()])
    client.ack = False
    publisher.publish([make_room(zones=[])], [result()])
    client.ack = True
    publisher.publish([make_room()], [result()])
    assert publisher.tombstones == {}
    assert "room_r1_zone_zsofa_occupancy" in publisher.announced


def test_the_csi_zones_of_0_14_are_queued_for_deletion(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(tmp_path))
    (tmp_path / "mqtt_announced.json").write_text(json.dumps({"announced": ["abc"], "tombstones": ["old"]}))
    announced, tombstones = mqtt_bridge.load_state()
    assert announced == {}
    assert tombstones == {
        "csi_zone:abc": mqtt_bridge.legacy_zone_topics("abc"),
        "csi_zone:old": mqtt_bridge.legacy_zone_topics("old"),
    }
    assert "homeassistant/binary_sensor/echolot/zone_abc/config" in tombstones["csi_zone:abc"]
    # Written down in the new file, so the old one is read only once.
    assert (tmp_path / "mqtt_entities.json").exists()


def test_the_csi_zones_actually_leave_the_broker(env):
    bridge, client, tmp_path = env
    (tmp_path / "mqtt_announced.json").write_text(json.dumps(["abc"]))
    publisher = mqtt_bridge.RoomPublisher(bridge)
    publisher.publish([], [])
    cleared = {m[0] for m in client.messages if m[1] == "" and m[3] == 1}
    assert cleared == set(mqtt_bridge.legacy_zone_topics("abc"))
    assert publisher.tombstones == {}


def test_nothing_is_attempted_without_a_connection(env):
    bridge, client, _ = env
    bridge.connected = False
    mqtt_bridge.RoomPublisher(bridge).publish([make_room()], [result()])
    assert client.messages == []
