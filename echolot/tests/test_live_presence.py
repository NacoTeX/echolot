"""The always-on subscription and its rolling window.

Presence from the crossing rate asks how often a room crossed in the last
minute. There is no last minute unless something has been listening, so
this runs whether or not anybody is recording — and the calibration
sampler attaches to it rather than opening a second subscription to the
same three entities.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import live_presence  # noqa: E402
from app.devices import BuildStatus, Device, DeviceCreate  # noqa: E402

SCORE = "sensor.probe_movement_score"
MOTION = "binary_sensor.probe_motion_detected"
THRESHOLD = "number.probe_threshold"


def make_device(device_id: str = "probe", *, entities: bool = True) -> Device:
    device = Device(
        id=device_id,
        created_at=1,
        updated_at=2,
        status=BuildStatus.SUCCESS,
        config=DeviceCreate(
            name=device_id, board="esp32c6", wifi_ssid="netz", wifi_password="passwort123"
        ),
    )
    if entities:
        device.entity_movement_score = SCORE
        device.entity_motion = MOTION
        device.entity_threshold = THRESHOLD
    return device


def state(value, stamp="2026-09-08T12:00:00+00:00"):
    return {"state": str(value), "last_updated": stamp}


def at(seconds: float) -> str:
    """A Home Assistant timestamp `seconds` after a fixed origin."""
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(seconds=seconds)).isoformat()


ORIGIN = 1788868800.0  # 2026-09-08T12:00:00Z


class FakeSubscription:
    instances: list["FakeSubscription"] = []

    def __init__(self, entity_ids, on_state, *, loop=None):
        self.entity_ids = list(entity_ids)
        self.on_state = on_state
        self.started = False
        self.stopped = False
        self.connected = False
        self.error = None
        FakeSubscription.instances.append(self)

    def start(self):
        self.started = True
        self.connected = True

    def stop(self):
        self.stopped = True
        self.connected = False


@pytest.fixture
def stream():
    FakeSubscription.instances.clear()
    device_stream = live_presence.DeviceStream(
        make_device(), subscription_factory=FakeSubscription
    )
    device_stream.start()
    return device_stream, FakeSubscription.instances[-1]


# --- collecting -------------------------------------------------------------


def test_all_three_entities_are_subscribed(stream):
    _, subscription = stream
    assert subscription.entity_ids == [SCORE, MOTION, THRESHOLD]
    assert subscription.started is True


def test_readings_land_in_the_window(stream):
    device_stream, subscription = stream
    subscription.on_state(SCORE, state("0.4", at(0)))
    subscription.on_state(SCORE, state("0.9", at(1)))
    window = device_stream.window(60.0, now=ORIGIN + 2)
    assert [row["movement_score"] for row in window] == [0.4, 0.9]


def test_the_threshold_is_context_not_a_reading(stream):
    device_stream, subscription = stream
    subscription.on_state(THRESHOLD, state("0.5", at(0)))
    assert device_stream.window(60.0, now=ORIGIN + 1) == []

    subscription.on_state(SCORE, state("0.4", at(1)))
    assert device_stream.window(60.0, now=ORIGIN + 2)[0]["movement_score"] == 0.4


def test_the_seed_supplies_entities_that_never_change(stream):
    """A subscription reports changes; a threshold that has been 0.5 all
    along is never reported and would be missing from every sample."""
    device_stream, subscription = stream
    device_stream.seed({THRESHOLD: state("0.5", at(0))})
    subscription.on_state(SCORE, state("0.4", at(1)))
    assert device_stream._samples[-1].threshold == 0.5  # noqa: SLF001


def test_a_live_message_beats_the_seed(stream):
    device_stream, subscription = stream
    subscription.on_state(THRESHOLD, state("0.66", at(1)))
    device_stream.seed({THRESHOLD: state("0.5", at(0))})
    subscription.on_state(SCORE, state("0.4", at(2)))
    assert device_stream._samples[-1].threshold == 0.66  # noqa: SLF001


# --- the window -------------------------------------------------------------


def test_the_window_is_bounded_by_time_not_by_count(stream):
    """The score only changes when it changes — one recording had gaps up
    to 18.7 s — so a fixed number of readings would span a wildly varying
    stretch of clock, and the rate is per second."""
    device_stream, subscription = stream
    for index in range(10):
        subscription.on_state(SCORE, state("0.4", at(index * 20)))
    recent = device_stream.window(60.0, now=ORIGIN + 180)
    assert len(recent) == 4  # 120, 140, 160, 180 seconds in


def test_old_readings_are_dropped_from_memory(stream):
    device_stream, subscription = stream
    subscription.on_state(SCORE, state("0.4", at(0)))
    subscription.on_state(SCORE, state("0.4", at(live_presence.WINDOW_SECONDS + 60)))
    assert len(device_stream._samples) == 1  # noqa: SLF001


# --- listeners --------------------------------------------------------------


def test_listeners_receive_every_reading(stream):
    device_stream, subscription = stream
    seen = []
    device_stream.add_listener(lambda device_id, sample: seen.append((device_id, sample)))
    subscription.on_state(SCORE, state("0.4", at(0)))
    assert len(seen) == 1 and seen[0][0] == "probe"

    device_stream.remove_listener(next(iter(device_stream._listeners)))  # noqa: SLF001
    subscription.on_state(SCORE, state("0.9", at(1)))
    assert len(seen) == 1


def test_one_failing_listener_does_not_stop_the_others(stream):
    device_stream, subscription = stream
    seen = []

    def broken(device_id, sample):
        raise RuntimeError("kaputt")

    device_stream.add_listener(broken)
    device_stream.add_listener(lambda device_id, sample: seen.append(sample))
    subscription.on_state(SCORE, state("0.4", at(0)))
    assert seen, "der intakte Listener muss trotzdem bedient werden"
    # And the reading is still in the window.
    assert device_stream.window(60.0, now=ORIGIN + 1)


# --- keeping in step with the device list -----------------------------------


def test_a_stream_opens_for_every_device_that_can_supply_readings():
    FakeSubscription.instances.clear()
    live = live_presence.LivePresence(subscription_factory=FakeSubscription)
    live.reconcile([make_device("a"), make_device("b"), make_device("c", entities=False)])
    assert set(live.statuses()) == {"a", "b"}
    assert live.stream("c") is None


def test_a_removed_device_has_its_stream_closed():
    FakeSubscription.instances.clear()
    live = live_presence.LivePresence(subscription_factory=FakeSubscription)
    live.reconcile([make_device("a"), make_device("b")])
    live.reconcile([make_device("a")])
    assert set(live.statuses()) == {"a"}
    assert FakeSubscription.instances[1].stopped is True


def test_reconciling_again_does_not_reopen_a_working_stream():
    FakeSubscription.instances.clear()
    live = live_presence.LivePresence(subscription_factory=FakeSubscription)
    live.reconcile([make_device("a")])
    live.reconcile([make_device("a")])
    assert len(FakeSubscription.instances) == 1


def test_stopping_everything_closes_every_stream():
    FakeSubscription.instances.clear()
    live = live_presence.LivePresence(subscription_factory=FakeSubscription)
    live.reconcile([make_device("a"), make_device("b")])
    live.stop_all()
    assert live.statuses() == {}
    assert all(instance.stopped for instance in FakeSubscription.instances)
