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


# --- driven by a subscription ----------------------------------------------


class FakeSubscription:
    """Stands in for the websocket, so a test can deliver states by hand."""

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
def wired():
    FakeSubscription.instances.clear()
    captured = []
    sampler = ha_sampler.HomeAssistantSampler(
        lambda device_id, sample: captured.append((device_id, sample)), FakeSubscription
    )
    assert sampler.start(make_device()) is True
    return sampler, captured, FakeSubscription.instances[-1]


def test_all_three_entities_are_subscribed(wired):
    _, _, subscription = wired
    assert subscription.entity_ids == [
        "sensor.probe_movement_score",
        "binary_sensor.probe_motion_detected",
        "number.probe_threshold",
    ]
    assert subscription.started is True


def test_a_score_change_becomes_a_sample(wired):
    _, captured, subscription = wired
    subscription.on_state("sensor.probe_movement_score", state("0.42"))
    assert len(captured) == 1
    device_id, sample = captured[0]
    assert device_id == "probe"
    assert sample.movement_score == 0.42


def test_the_threshold_is_context_not_a_reading(wired):
    """It changes only on recalibration. Emitting on it would put a row in
    the export with no measurement in it."""
    _, captured, subscription = wired
    subscription.on_state("number.probe_threshold", state("0.5"))
    assert captured == []

    subscription.on_state("sensor.probe_movement_score", state("0.42"))
    assert captured[0][1].threshold == 0.5


def test_motion_and_score_are_carried_across_messages(wired):
    """They arrive as separate messages; a sample needs the latest of each."""
    _, captured, subscription = wired
    subscription.on_state("binary_sensor.probe_motion_detected", state("on"))
    subscription.on_state("sensor.probe_movement_score", state("0.9"))
    sample = captured[-1][1]
    assert sample.motion is True
    assert sample.movement_score == 0.9


def test_stopping_ends_the_subscription(wired):
    sampler, _, subscription = wired
    sampler.stop("probe")
    assert subscription.stopped is True
    assert sampler.running_for("probe") is False


def test_starting_twice_does_not_open_a_second_subscription(wired):
    sampler, _, _ = wired
    before = len(FakeSubscription.instances)
    assert sampler.start(make_device()) is False
    assert len(FakeSubscription.instances) == before


def test_a_device_without_entities_cannot_be_sampled():
    sampler = ha_sampler.HomeAssistantSampler(lambda *_: None, FakeSubscription)
    device = make_device()
    device.entity_movement_score = None
    device.entity_motion = None
    assert sampler.start(device) is False


def test_the_connection_state_is_reportable(wired):
    sampler, _, subscription = wired
    assert sampler.status("probe") == {"connected": True, "error": None}
    subscription.connected = False
    subscription.error = "getrennt"
    assert sampler.status("probe") == {"connected": False, "error": "getrennt"}
    assert sampler.status("gibtsnicht") == {"connected": False, "error": None}


# --- through to the export --------------------------------------------------


def test_a_recording_from_home_assistant_reaches_the_csv(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(tmp_path))
    FakeSubscription.instances.clear()
    store = calibration.CalibrationStore()
    sampler = ha_sampler.HomeAssistantSampler(store.ingest, FakeSubscription)

    session = store.create("probe", name="Gehtest")
    store.set_label(session["id"], "empty")
    sampler.start(make_device())
    subscription = FakeSubscription.instances[-1]

    subscription.on_state("number.probe_threshold", state("0.5"))
    for index, score in enumerate([0.05, 0.06, 3.1, 3.4, 0.05, 0.07]):
        subscription.on_state(
            "sensor.probe_movement_score",
            state(score, f"2026-09-08T12:00:{index:02d}+00:00"),
        )
    store.stop(session["id"])

    rows = store.csv(session["id"]).strip().splitlines()
    assert rows[0] == "t,movement_score,threshold,motion,label"
    body = [row.split(",") for row in rows[1:]]
    assert len(body) == 6
    assert all(row[1] for row in body), "jede Zeile braucht einen Bewegungswert"
    assert {row[2] for row in body} == {"0.5"}, "die Schwelle muss mitgeschrieben werden"
    assert all(row[4] == "empty" for row in body)


def test_the_cache_is_seeded_so_unchanging_entities_are_known():
    """A subscription reports changes. The threshold does not change, so it
    is never reported — and the first twenty-minute recording came back
    with an empty threshold column throughout, because it had been 0.5 the
    whole time and there was nothing to announce.
    """
    FakeSubscription.instances.clear()
    captured = []
    read = []

    async def reader(entity_id):
        read.append(entity_id)
        return {"state": "0.5", "last_updated": "2026-09-08T12:00:00+00:00"}

    async def run():
        sampler = ha_sampler.HomeAssistantSampler(
            lambda device_id, sample: captured.append(sample), FakeSubscription, reader
        )
        sampler.start(make_device())
        await asyncio.sleep(0)  # let the seeding task run
        FakeSubscription.instances[-1].on_state(
            "sensor.probe_movement_score", state("0.42")
        )

    asyncio.run(run())
    assert "number.probe_threshold" in read
    assert captured and captured[-1].threshold == 0.5


def test_a_change_that_arrived_first_is_not_overwritten_by_the_seed():
    """The seed is a snapshot from before; a live message is newer."""
    FakeSubscription.instances.clear()
    captured = []
    gate = asyncio.Event()

    async def slow_reader(entity_id):
        await gate.wait()
        return {"state": "0.9", "last_updated": "2026-09-08T11:00:00+00:00"}

    async def run():
        sampler = ha_sampler.HomeAssistantSampler(
            lambda device_id, sample: captured.append(sample), FakeSubscription, slow_reader
        )
        sampler.start(make_device())
        subscription = FakeSubscription.instances[-1]
        subscription.on_state("number.probe_threshold", state("0.5"))
        gate.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        subscription.on_state("sensor.probe_movement_score", state("0.42"))

    asyncio.run(run())
    assert captured[-1].threshold == 0.5


def test_a_failing_seed_does_not_stop_the_recording():
    FakeSubscription.instances.clear()
    captured = []

    async def broken(entity_id):
        raise RuntimeError("Home Assistant hustet")

    async def run():
        sampler = ha_sampler.HomeAssistantSampler(
            lambda device_id, sample: captured.append(sample), FakeSubscription, broken
        )
        assert sampler.start(make_device()) is True
        await asyncio.sleep(0)
        FakeSubscription.instances[-1].on_state(
            "sensor.probe_movement_score", state("0.42")
        )

    asyncio.run(run())
    assert captured, "die Aufzeichnung muss auch ohne Startwerte laufen"
