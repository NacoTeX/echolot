"""Tests for the MQTT zone bridge.

Two properties that are invisible until they fail in someone's house:

  * After a reconnect the bridge re-announces its zones. Discovery is
    published retained, so it normally survives — but a broker restarted
    without persistence has forgotten everything while the bridge still
    believes it announced them, and the entities would stay missing until
    the add-on restarted.
  * A publish the broker rejected is not recorded as sent. Otherwise a
    dropped discovery message is never retried and the zone silently
    never appears.
"""

import os
import sys
import tempfile
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import mqtt_bridge  # noqa: E402


class FakeInfo:
    def __init__(self, rc):
        self.rc = rc


class FakeClient:
    """Records publishes and can be told to reject them."""

    def __init__(self, rc=mqtt.MQTT_ERR_SUCCESS):
        self.published: list[tuple[str, str, bool]] = []
        self.rc = rc

    def publish(self, topic, payload, retain=False):
        self.published.append((topic, payload, retain))
        return FakeInfo(self.rc)

    def topics(self):
        return [topic for topic, _, _ in self.published]


@pytest.fixture
def bridge():
    b = mqtt_bridge.ZoneBridge()
    b._client = FakeClient()
    b.connected = True
    return b


def discovery_count(client, zone_id):
    return client.topics().count(mqtt_bridge.zone_discovery_topic(zone_id))


def test_a_zone_is_announced_once_while_the_connection_holds(bridge):
    for _ in range(3):
        bridge.publish_zone("z1", "Küche", occupied=True, available=True)
    assert discovery_count(bridge._client, "z1") == 1
    # ...but its state goes out every time.
    assert bridge._client.topics().count(mqtt_bridge.zone_state_topic("z1")) == 3


def test_a_reconnect_makes_the_zone_be_announced_again(bridge):
    """The regression this file exists for."""
    bridge.publish_zone("z1", "Küche", occupied=True, available=True)
    assert discovery_count(bridge._client, "z1") == 1

    # What on_connect does after the broker comes back.
    with bridge._lock:
        bridge._announced.clear()

    bridge.publish_zone("z1", "Küche", occupied=True, available=True)
    assert discovery_count(bridge._client, "z1") == 2


def test_a_rejected_discovery_is_retried_next_cycle(bridge):
    """Recording a dropped message as sent would mean never retrying it."""
    bridge._client.rc = mqtt.MQTT_ERR_QUEUE_SIZE
    bridge.publish_zone("z1", "Küche", occupied=True, available=True)
    assert bridge._announced == {}
    # No state published either: announcing it is the precondition.
    assert mqtt_bridge.zone_state_topic("z1") not in bridge._client.topics()

    bridge._client.rc = mqtt.MQTT_ERR_SUCCESS
    bridge.publish_zone("z1", "Küche", occupied=True, available=True)
    assert bridge._announced == {"z1": "Küche"}
    assert mqtt_bridge.zone_state_topic("z1") in bridge._client.topics()


def test_an_unavailable_zone_publishes_no_state(bridge):
    """A sensor being offline is not the same as a room being empty."""
    bridge.publish_zone("z1", "Küche", occupied=False, available=False)
    assert mqtt_bridge.zone_state_topic("z1") not in bridge._client.topics()
    # It is still announced, so the entity exists and reads "unavailable".
    assert discovery_count(bridge._client, "z1") == 1


# --- Regressions from the external review (P1 #7) ----------------------


def test_an_unavailable_zone_says_so_instead_of_going_quiet(bridge):
    """Publishing nothing left Home Assistant holding the last ON/OFF.

    The add-on's own LWT stayed "online" the whole time, so nothing in
    Home Assistant could tell a stale value from a current one — and a
    stale "clear" is exactly the value an automation acts on.
    """
    bridge.publish_zone("z1", "Küche", occupied=False, available=False)
    assert (mqtt_bridge.zone_availability_topic("z1"), "offline", True) in bridge._client.published


def test_an_available_zone_is_marked_online(bridge):
    bridge.publish_zone("z1", "Küche", occupied=True, available=True)
    published = bridge._client.published
    assert (mqtt_bridge.zone_availability_topic("z1"), "online", True) in published
    assert (mqtt_bridge.zone_state_topic("z1"), "ON", True) in published


def test_one_zone_going_unavailable_leaves_the_others_alone(bridge):
    bridge.publish_zone("z1", "Küche", occupied=True, available=True)
    bridge.publish_zone("z2", "Flur", occupied=False, available=False)
    published = bridge._client.published
    assert (mqtt_bridge.zone_availability_topic("z1"), "online", True) in published
    assert (mqtt_bridge.zone_availability_topic("z2"), "offline", True) in published


def test_discovery_requires_both_availability_sources():
    """`availability_mode: all` — the add-on has to be up *and* the zone
    has to have a measurement. Either one failing marks it unavailable."""
    payload = mqtt_bridge.zone_discovery_payload("z1", "Küche")
    assert payload["availability_mode"] == "all"
    topics = [entry["topic"] for entry in payload["availability"]]
    assert mqtt_bridge.AVAILABILITY_TOPIC in topics
    assert mqtt_bridge.zone_availability_topic("z1") in topics
    # The old single-topic form must be gone, or HA would use it instead.
    assert "availability_topic" not in payload


def test_the_unique_id_is_unchanged_by_the_availability_rework():
    """Existing installations must keep their entity, not gain a second."""
    assert mqtt_bridge.zone_discovery_payload("z1", "Küche")["unique_id"] == "echolot_zone_z1"


def test_forgetting_a_zone_clears_every_retained_topic(bridge):
    bridge.publish_zone("z1", "Küche", occupied=True, available=True)
    bridge._client.published.clear()
    bridge.forget_zone("z1")
    assert bridge._client.published == [
        (mqtt_bridge.zone_discovery_topic("z1"), "", True),
        (mqtt_bridge.zone_state_topic("z1"), "", True),
        (mqtt_bridge.zone_availability_topic("z1"), "", True),
    ]
    assert bridge._announced == {}


def test_nothing_is_published_while_disconnected(bridge):
    bridge.connected = False
    bridge.publish_zone("z1", "Küche", occupied=True, available=True)
    bridge.forget_zone("z1")
    assert bridge._client.published == []


def test_umlauts_become_a_predictable_object_id():
    """Home Assistant would otherwise decide, and it decides differently."""
    assert mqtt_bridge.slugify("Küche") == "kueche"
    assert mqtt_bridge.slugify("Büro/Süd") == "buero_sued"
    assert mqtt_bridge.slugify("") == "zone"


# --- the two halves of P1 #7 that were missed the first time -----------


def test_renaming_a_zone_republishes_its_discovery(bridge):
    """The name and object_id live in the discovery payload, and it was
    only ever sent once — so Home Assistant kept the old name forever."""
    bridge.publish_zone("z1", "Küche", occupied=False, available=True)
    assert discovery_count(bridge._client, "z1") == 1

    bridge.publish_zone("z1", "Küche neu", occupied=False, available=True)
    assert discovery_count(bridge._client, "z1") == 2

    last = [p for p in bridge._client.published
            if p[0] == mqtt_bridge.zone_discovery_topic("z1")][-1]
    assert '"name": "K\\u00fcche neu"' in last[1] or "Küche neu" in last[1]


def test_an_unchanged_name_is_still_announced_only_once(bridge):
    for _ in range(5):
        bridge.publish_zone("z1", "Küche", occupied=True, available=True)
    assert discovery_count(bridge._client, "z1") == 1


def test_announced_zones_survive_a_restart(tmp_path, monkeypatch, bridge):
    """A zone deleted while the add-on was stopped was never
    un-announced: its retained discovery message stayed on the broker and
    the entity haunted Home Assistant. Nothing in memory remembered it had
    ever been announced."""
    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(tmp_path))
    bridge.publish_zone("z1", "Küche", occupied=True, available=True)
    assert mqtt_bridge.load_announced() == {"z1"}

    bridge.forget_zone("z1")
    assert mqtt_bridge.load_announced() == set()


def test_a_missing_store_is_not_a_broken_export(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(tmp_path / "gibtsnicht"))
    assert mqtt_bridge.load_announced() == set()
