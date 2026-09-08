"""Recording calibration samples from Home Assistant.

The device's own Direct HTTP API is closed to us: ESPectre configures it
`for_first_party_portals()` — only https://espectre.dev and two siblings —
and refuses a request without an Origin header. The one escape hatch,
CONFIG_ESPECTRE_DIRECT_DEV_ORIGINS_ENABLED, is not declared in any Kconfig
the ESPHome build reaches. So the samples come from the entities the
device already publishes to Home Assistant.
"""

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import calibration, ha_sampler  # noqa: E402
from app.devices import BuildStatus, Device, DeviceCreate  # noqa: E402


def state(value, stamp="2026-09-08T12:00:00+00:00"):
    return {"state": str(value), "last_updated": stamp}


def make_device() -> Device:
    device = Device(
        id="probe",
        created_at=1,
        updated_at=2,
        status=BuildStatus.SUCCESS,
        config=DeviceCreate(
            name="probe", board="esp32c6", wifi_ssid="netz", wifi_password="passwort123"
        ),
    )
    device.entity_movement_score = "sensor.probe_movement_score"
    device.entity_motion = "binary_sensor.probe_motion_detected"
    device.entity_threshold = "number.probe_threshold"
    return device


# --- building one reading ---------------------------------------------------


def test_a_reading_carries_score_motion_and_threshold():
    sample = ha_sampler.build_sample(state("0.42"), state("on"), 0.5)
    assert sample.movement_score == 0.42
    assert sample.motion is True
    assert sample.threshold == 0.5


def test_home_assistants_own_timestamp_is_used():
    """Not arrival time: the reading is as old as Home Assistant says, and
    that is what makes repeats detectable."""
    when = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)
    sample = ha_sampler.build_sample(
        state("0.42", when.isoformat()), state("off", when.isoformat()), None
    )
    assert sample.t == when.timestamp()


def test_unknown_and_unavailable_are_not_measurements():
    assert ha_sampler.build_sample(state("unknown"), state("unavailable"), 0.5) is None
    assert ha_sampler.build_sample(None, None, 0.5) is None


def test_a_motion_state_alone_is_still_a_reading():
    """A device may publish motion while its score entity is briefly
    unknown; dropping that would lose the transition."""
    sample = ha_sampler.build_sample(state("unknown"), state("on"), None)
    assert sample is not None
    assert sample.motion is True
    assert sample.movement_score is None


def test_an_unparsable_timestamp_does_not_lose_the_reading():
    sample = ha_sampler.build_sample({"state": "0.4", "last_updated": "gestern"}, None, None)
    assert sample is not None
    assert sample.movement_score == 0.4


# --- attached to the live stream --------------------------------------------
#
# The sampler no longer owns a subscription. app/live_presence.py holds one
# per device permanently, because the crossing rate needs a window that
# exists before anybody presses record; a recording attaches to it.


class FakeStream:
    def __init__(self, device_id="probe"):
        self.device_id = device_id
        self.listeners = set()
        self.connected = True
        self.error = None

    def add_listener(self, listener):
        self.listeners.add(listener)

    def remove_listener(self, listener):
        self.listeners.discard(listener)

    def deliver(self, sample):
        for listener in tuple(self.listeners):
            listener(self.device_id, sample)


class FakeLive:
    def __init__(self, streams=None):
        self.streams = streams if streams is not None else {"probe": FakeStream()}

    def stream(self, device_id):
        return self.streams.get(device_id)


@pytest.fixture
def wired():
    captured = []
    live = FakeLive()
    sampler = ha_sampler.HomeAssistantSampler(
        lambda device_id, sample: captured.append((device_id, sample)), live
    )
    assert sampler.start(make_device()) is True
    return sampler, captured, live.streams["probe"]


def test_a_recording_attaches_to_the_existing_stream(wired):
    _, _, stream = wired
    assert len(stream.listeners) == 1


def test_samples_from_the_stream_reach_the_session(wired):
    _, captured, stream = wired
    sample = ha_sampler.build_sample(state("0.42"), state("on"), 0.5)
    stream.deliver(sample)
    assert captured == [("probe", sample)]


def test_stopping_detaches(wired):
    sampler, captured, stream = wired
    sampler.stop("probe")
    assert stream.listeners == set()
    stream.deliver(ha_sampler.build_sample(state("0.42"), None, None))
    assert captured == []
    assert sampler.running_for("probe") is False


def test_starting_twice_attaches_once(wired):
    sampler, _, stream = wired
    assert sampler.start(make_device()) is False
    assert len(stream.listeners) == 1


def test_a_device_with_no_live_stream_cannot_be_recorded():
    """Nothing is listening to it — usually because Echolot has not
    resolved its entities."""
    sampler = ha_sampler.HomeAssistantSampler(lambda *_: None, FakeLive(streams={}))
    assert sampler.start(make_device()) is False


def test_the_connection_state_comes_from_the_stream(wired):
    sampler, _, stream = wired
    assert sampler.status("probe") == {"connected": True, "error": None}
    stream.connected = False
    stream.error = "getrennt"
    assert sampler.status("probe") == {"connected": False, "error": "getrennt"}
    assert sampler.status("gibtsnicht") == {"connected": False, "error": None}


def test_stop_all_detaches_every_recording():
    stream_a, stream_b = FakeStream("a"), FakeStream("b")
    live = FakeLive({"a": stream_a, "b": stream_b})
    sampler = ha_sampler.HomeAssistantSampler(lambda *_: None, live)
    for device_id in ("a", "b"):
        device = make_device()
        device.id = device_id
        sampler.start(device)
    sampler.stop_all()
    assert stream_a.listeners == set() and stream_b.listeners == set()
