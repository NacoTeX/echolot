"""Why the zone is in the state it is in.

From the external review's feature table: transitions, the devices
involved, the rate, motion, hold time and data failures, in a bounded
event log. The question a presence system is actually asked is never "is
the room occupied" — it is "why did the light stay on".
"""

import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import timeline as timeline_module  # noqa: E402


@pytest.fixture
def log():
    return timeline_module.Timeline(max_events=5)


def state(name="clear", *, available=True, occupied=False, trigger=None, **extra):
    return {
        "state": name,
        "available": available,
        "occupied": occupied,
        "trigger": trigger,
        "members": [],
        **extra,
    }


def test_the_first_sighting_is_not_a_transition(log):
    """Otherwise every restart writes a "became clear" nothing caused."""
    assert log.record("z1", "Küche", state("clear")) is None
    assert log.events("z1") == []


def test_a_change_is_recorded(log):
    log.record("z1", "Küche", state("clear"))
    event = log.record("z1", "Küche", state("detected", occupied=True, trigger="motion"))
    assert event is not None
    assert event["from_state"] == "clear"
    assert event["to_state"] == "detected"
    assert event["trigger"] == "motion"


def test_standing_still_records_nothing(log):
    log.record("z1", "Küche", state("clear"))
    for _ in range(10):
        assert log.record("z1", "Küche", state("clear")) is None
    assert log.events("z1") == []


def test_losing_the_measurement_is_a_transition(log):
    """Invisible if only the state name is compared, and exactly the kind
    of thing someone is trying to explain afterwards."""
    log.record("z1", "Küche", state("clear"))
    event = log.record("z1", "Küche", state("clear", available=False))
    assert event is not None
    assert event["available"] is False
    assert "nicht erreichbar" in timeline_module.explain(event)


def test_the_rate_and_motion_are_told_apart(log):
    log.record("z1", "Küche", state("clear"))
    event = log.record(
        "z1", "Küche", state("detected", occupied=True, trigger="rate", rate_occupied=True)
    )
    assert "still" in timeline_module.explain(event)

    log.record("z1", "Küche", state("clear"))
    event = log.record("z1", "Küche", state("detected", occupied=True, trigger="motion"))
    assert timeline_module.explain(event) == "Bewegung erkannt"


def test_a_hold_says_how_long_is_left(log):
    log.record("z1", "Küche", state("detected", occupied=True))
    event = log.record(
        "z1", "Küche", state("holding", occupied=True, hold_remaining=42.0)
    )
    assert "42" in timeline_module.explain(event)


def test_what_each_device_was_saying_is_kept(log):
    log.record("z1", "Küche", state("clear"))
    event = log.record(
        "z1",
        "Küche",
        state(
            "detected",
            occupied=True,
            members=[
                {"device_id": "d1", "name": "Wohnzimmer", "available": True,
                 "motion": True, "movement_score": 0.8, "error": None},
            ],
        ),
    )
    assert event["members"] == [
        {"device_id": "d1", "name": "Wohnzimmer", "available": True,
         "motion": True, "movement_score": 0.8}
    ]


def test_the_log_is_bounded(log):
    """A flapping zone must not grow without bound."""
    for index in range(50):
        log.record("z1", "Küche", state("detected" if index % 2 else "clear"))
    assert len(log.events("z1", limit=100)) == 5


def test_newest_first(log):
    log.record("z1", "Küche", state("clear"))
    log.record("z1", "Küche", state("detected"))
    log.record("z1", "Küche", state("holding"))
    events = log.events("z1")
    assert [event["to_state"] for event in events] == ["holding", "detected"]


def test_zones_are_kept_apart_but_can_be_read_together(log):
    log.record("a", "Küche", state("clear"))
    log.record("b", "Flur", state("clear"))
    log.record("a", "Küche", state("detected"))
    time.sleep(0.01)
    log.record("b", "Flur", state("detected"))

    assert len(log.events("a")) == 1
    assert [event["zone_name"] for event in log.all_events()] == ["Flur", "Küche"]


def test_deleting_a_zone_takes_its_history(log):
    log.record("z1", "Küche", state("clear"))
    log.record("z1", "Küche", state("detected"))
    log.forget("z1")
    assert log.events("z1") == []
