"""A delete that fails has to survive the failure.

From the external review (R4). The announced set was not a deletion
queue: `forget_zone()` returned immediately while the broker was
disconnected, and the publish loop then set `known = current` anyway. On
the next pass the zone was no longer in `known`, so nobody ever tried
again — its retained discovery message stayed on the broker and the
entity haunted Home Assistant for good.

So a zone that should go gets a tombstone written to disk *before* the
first attempt, and the tombstone is only dropped once the broker has
actually taken every retained topic.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import mqtt_bridge  # noqa: E402


class Zone:
    def __init__(self, zone_id, name=None):
        self.id = zone_id
        self.name = name or zone_id.title()


class FakeBridge:
    """Enough of ZoneBridge to be a broker that can be switched off."""

    def __init__(self):
        self.connected = True
        self.reject = False
        self.announced: dict[str, str] = {}
        self.deleted: list[str] = []

    def announced_ids(self):
        return set(self.announced)

    def publish_zone(self, zone_id, zone_name, occupied, available):
        if not self.connected or self.reject:
            return
        self.announced[zone_id] = zone_name

    def forget_zone(self, zone_id):
        if not self.connected or self.reject:
            return False
        self.announced.pop(zone_id, None)
        self.deleted.append(zone_id)
        return True


@pytest.fixture
def broker(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(tmp_path))
    fake = FakeBridge()
    monkeypatch.setattr(mqtt_bridge, "bridge", fake)
    return fake


def round_of(publisher, *zone_ids):
    publisher.publish([(Zone(z), {"occupied": False, "available": True}) for z in zone_ids])


def test_an_announced_zone_is_written_down(broker):
    publisher = mqtt_bridge.ZonePublisher()
    round_of(publisher, "kueche")
    assert mqtt_bridge.load_state() == ({"kueche"}, set())


def test_a_delete_that_could_not_be_sent_is_retried_until_it_lands(broker):
    """The regression: several rounds went by, the zone was forgotten
    locally, and the retained topics were never taken."""
    publisher = mqtt_bridge.ZonePublisher()
    round_of(publisher, "kueche", "flur")

    broker.connected = False
    for _ in range(5):                       # the zone is gone, the broker is not there
        round_of(publisher, "flur")
    assert broker.deleted == []
    assert mqtt_bridge.load_state()[1] == {"kueche"}, "die Löschung muss vorgemerkt bleiben"

    broker.connected = True
    round_of(publisher, "flur")
    assert broker.deleted == ["kueche"]
    assert mqtt_bridge.load_state() == ({"flur"}, set())


def test_a_rejected_delete_stays_queued(broker):
    """A full queue or a publish the broker refused is not a delete."""
    publisher = mqtt_bridge.ZonePublisher()
    round_of(publisher, "kueche", "flur")

    broker.reject = True
    round_of(publisher, "flur")
    assert broker.deleted == []
    assert publisher.tombstones == {"kueche"}

    broker.reject = False
    round_of(publisher, "flur")
    assert broker.deleted == ["kueche"]
    assert publisher.tombstones == set()


def test_a_restart_does_not_lose_a_pending_delete(broker):
    """The add-on being updated is the most likely moment for this."""
    publisher = mqtt_bridge.ZonePublisher()
    round_of(publisher, "kueche", "flur")

    broker.connected = False
    round_of(publisher, "flur")
    assert broker.deleted == []

    # Restart: new process, new publisher, broker back.
    broker.connected = True
    broker.announced.clear()               # retained discovery is still on the broker
    revived = mqtt_bridge.ZonePublisher()
    assert revived.tombstones == {"kueche"}
    round_of(revived, "flur")
    assert broker.deleted == ["kueche"]


def test_a_zone_deleted_while_the_add_on_was_stopped_is_un_announced(broker):
    """Nothing in memory remembers a zone that was deleted while the
    process was not running. The store on disk does."""
    mqtt_bridge.remember_state({"weg", "bleibt"}, set())

    publisher = mqtt_bridge.ZonePublisher()
    round_of(publisher, "bleibt")
    assert broker.deleted == ["weg"]
    assert mqtt_bridge.load_state() == ({"bleibt"}, set())


def test_a_new_announcement_does_not_erase_a_pending_delete(broker):
    """Announcements and tombstones share one file: writing one without
    the other loses the other."""
    publisher = mqtt_bridge.ZonePublisher()
    round_of(publisher, "kueche")

    broker.connected = False
    round_of(publisher, "flur")            # kueche gone, flur new, broker away
    broker.connected = True
    round_of(publisher, "flur", "bad")     # a third zone appears

    announced, tombstones = mqtt_bridge.load_state()
    assert announced == {"flur", "bad"}
    assert tombstones == set()
    assert broker.deleted == ["kueche"]


def test_a_zone_that_comes_back_is_not_deleted(broker):
    """A queued delete for a zone that exists again would take a live
    entity away."""
    publisher = mqtt_bridge.ZonePublisher()
    round_of(publisher, "kueche")

    broker.connected = False
    round_of(publisher)                    # kueche gone
    assert publisher.tombstones == {"kueche"}

    broker.connected = True
    round_of(publisher, "kueche")          # ...and back before the delete landed
    assert broker.deleted == []
    assert mqtt_bridge.load_state() == ({"kueche"}, set())


def test_the_runtime_is_only_dropped_once_the_zone_is_really_gone(broker):
    """`on_zone_gone` clears the zone's hold timer and its snapshot. Doing
    that while the delete is still queued would forget the zone locally
    and leave the entity in Home Assistant — the original bug."""
    gone = []
    publisher = mqtt_bridge.ZonePublisher(gone.append)
    round_of(publisher, "kueche", "flur")

    broker.connected = False
    round_of(publisher, "flur")
    assert gone == []

    broker.connected = True
    round_of(publisher, "flur")
    assert gone == ["kueche"]


def test_the_old_single_list_store_is_still_read(broker, tmp_path):
    """0.13.5 wrote a bare list of announced ids. An update must not
    forget what it had announced — that is a ghost entity per zone."""
    (tmp_path / "mqtt_announced.json").write_text(json.dumps(["alt"]), encoding="utf-8")
    assert mqtt_bridge.load_state() == ({"alt"}, set())

    publisher = mqtt_bridge.ZonePublisher()
    round_of(publisher, "neu")
    assert broker.deleted == ["alt"]


def test_a_corrupt_store_is_not_a_broken_export(broker, tmp_path):
    (tmp_path / "mqtt_announced.json").write_text("{kaputt", encoding="utf-8")
    assert mqtt_bridge.load_state() == (set(), set())


def test_a_failing_round_does_not_kill_the_listener(broker):
    """The publisher is called from the evaluator's loop; raising there
    would stop every zone from being evaluated, not just exported."""
    publisher = mqtt_bridge.ZonePublisher()

    def explode(*_args, **_kwargs):
        raise RuntimeError("Broker mag nicht")

    broker.publish_zone = explode
    publisher([(Zone("kueche"), {"occupied": True, "available": True})])
