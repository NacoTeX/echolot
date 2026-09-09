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


# --- the check that a recording has something behind it ----------------
#
# The sampler no longer owns a subscription, and since 0.13.6 it no longer
# feeds one either: readings reach the calibration store through the
# canonical bus (app/samples.py), once. Attaching here as well meant a
# device with the Direct HTTP API enabled recorded the same movement
# twice. What remains is the question `create_calibration` asks before it
# says yes — is anything listening to this device at all.


class FakeStream:
    def __init__(self, device_id="probe"):
        self.device_id = device_id
        self.connected = True
        self.error = None


class FakeLive:
    def __init__(self, streams=None):
        self.streams = streams if streams is not None else {"probe": FakeStream()}

    def stream(self, device_id):
        return self.streams.get(device_id)


@pytest.fixture
def wired():
    live = FakeLive()
    sampler = ha_sampler.HomeAssistantSampler(live)
    assert sampler.start(make_device()) is True
    return sampler, live.streams["probe"]


def test_a_recording_is_marked_as_running(wired):
    sampler, _ = wired
    assert sampler.running_for("probe") is True


def test_the_sampler_does_not_feed_anything_itself(wired):
    """The regression this file's role changed for: two feeds into one
    store is one movement recorded twice."""
    sampler, stream = wired
    assert not hasattr(sampler, "_sink")
    assert not hasattr(stream, "listeners"), "der Sampler hängt sich nicht mehr an"


def test_stopping_ends_the_recording(wired):
    sampler, _ = wired
    sampler.stop("probe")
    assert sampler.running_for("probe") is False


def test_starting_twice_is_refused(wired):
    sampler, _ = wired
    assert sampler.start(make_device()) is False


def test_a_device_with_no_live_stream_cannot_be_recorded():
    """Nothing is listening to it — usually because Echolot has not
    resolved its entities. Three sessions were once recorded and exported
    before anyone noticed there had never been any data in them."""
    sampler = ha_sampler.HomeAssistantSampler(FakeLive(streams={}))
    assert sampler.start(make_device()) is False


def test_the_connection_state_comes_from_the_stream(wired):
    sampler, stream = wired
    assert sampler.status("probe") == {"connected": True, "error": None}
    stream.connected = False
    stream.error = "getrennt"
    assert sampler.status("probe") == {"connected": False, "error": "getrennt"}
    assert sampler.status("gibtsnicht") == {"connected": False, "error": None}


def test_stop_all_ends_every_recording():
    live = FakeLive({"a": FakeStream("a"), "b": FakeStream("b")})
    sampler = ha_sampler.HomeAssistantSampler(live)
    for device_id in ("a", "b"):
        device = make_device()
        device.id = device_id
        sampler.start(device)
    sampler.stop_all()
    assert not sampler.running_for("a") and not sampler.running_for("b")
