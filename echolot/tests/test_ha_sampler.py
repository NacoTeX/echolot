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


# --- the polling loop -------------------------------------------------------


class FakeHomeAssistant:
    """Serves a scripted sequence, repeating the last entry forever."""

    def __init__(self, readings):
        self.readings = list(readings)
        self.index = 0
        self.threshold_reads = 0

    async def read(self, entity_id):
        if entity_id.startswith("number."):
            self.threshold_reads += 1
            return state("0.5")
        current = self.readings[min(self.index, len(self.readings) - 1)]
        if entity_id.startswith("sensor."):
            self.index += 1
            return state(current[0], current[1])
        return state("on" if current[0] > 0.5 else "off", current[1])


def run_sampler(fake, *, ticks: int) -> list:
    captured = []
    sampler = ha_sampler.HomeAssistantSampler(
        lambda device_id, sample: captured.append((device_id, sample)), fake.read
    )

    async def run():
        assert sampler.start(make_device()) is True
        await asyncio.sleep(ha_sampler.POLL_SECONDS * ticks)
        sampler.stop("probe")

    asyncio.run(run())
    return captured


def test_distinct_readings_are_recorded(monkeypatch):
    monkeypatch.setattr(ha_sampler, "POLL_SECONDS", 0.01)
    fake = FakeHomeAssistant([
        (0.1, "2026-09-08T12:00:00+00:00"),
        (0.9, "2026-09-08T12:00:01+00:00"),
        (0.2, "2026-09-08T12:00:02+00:00"),
    ])
    captured = run_sampler(fake, ticks=8)
    scores = [sample.movement_score for _, sample in captured]
    assert scores[:3] == [0.1, 0.9, 0.2]
    assert all(device_id == "probe" for device_id, _ in captured)


def test_a_repeated_reading_is_not_counted_twice(monkeypatch):
    """Polling faster than Home Assistant updates must not pile up copies —
    that would skew the distribution the recommendation is computed from."""
    monkeypatch.setattr(ha_sampler, "POLL_SECONDS", 0.01)
    fake = FakeHomeAssistant([(0.42, "2026-09-08T12:00:00+00:00")])
    captured = run_sampler(fake, ticks=10)
    assert len(captured) == 1


def test_the_threshold_is_not_re_read_on_every_tick(monkeypatch):
    monkeypatch.setattr(ha_sampler, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(ha_sampler, "THRESHOLD_EVERY", 5)
    fake = FakeHomeAssistant([
        (index / 10, f"2026-09-08T12:00:{index:02d}+00:00") for index in range(20)
    ])
    run_sampler(fake, ticks=12)
    assert 1 <= fake.threshold_reads <= 4


def test_a_failing_read_does_not_end_the_recording(monkeypatch):
    monkeypatch.setattr(ha_sampler, "POLL_SECONDS", 0.01)
    calls = {"n": 0}

    async def flaky(entity_id):
        calls["n"] += 1
        if calls["n"] < 4:
            raise RuntimeError("Home Assistant hustet")
        return state(0.7, f"2026-09-08T12:00:{calls['n']:02d}+00:00")

    captured = []
    sampler = ha_sampler.HomeAssistantSampler(
        lambda device_id, sample: captured.append(sample), flaky
    )

    async def run():
        sampler.start(make_device())
        await asyncio.sleep(0.2)
        sampler.stop("probe")

    asyncio.run(run())
    assert captured, "nach dem Fehler muss weiter abgetastet werden"


def test_starting_twice_does_not_double_the_sample_rate(monkeypatch):
    monkeypatch.setattr(ha_sampler, "POLL_SECONDS", 0.01)
    sampler = ha_sampler.HomeAssistantSampler(lambda *_: None, FakeHomeAssistant([(0.1, "x")]).read)

    async def run():
        device = make_device()
        first = sampler.start(device)
        second = sampler.start(device)
        sampler.stop_all()
        return first, second

    assert asyncio.run(run()) == (True, False)


def test_a_device_without_entities_cannot_be_sampled():
    sampler = ha_sampler.HomeAssistantSampler(lambda *_: None, None)
    device = make_device()
    device.entity_movement_score = None
    device.entity_motion = None
    assert sampler.start(device) is False


# --- through to the export --------------------------------------------------


def test_a_recording_from_home_assistant_reaches_the_csv(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ha_sampler, "POLL_SECONDS", 0.01)
    store = calibration.CalibrationStore()
    fake = FakeHomeAssistant([
        (score, f"2026-09-08T12:00:{index:02d}+00:00")
        for index, score in enumerate([0.05, 0.06, 3.1, 3.4, 0.05, 0.07])
    ])
    sampler = ha_sampler.HomeAssistantSampler(store.ingest, fake.read)

    async def run():
        session = store.create("probe", name="Gehtest")
        store.set_label(session["id"], "empty")
        sampler.start(make_device())
        await asyncio.sleep(0.25)
        sampler.stop("probe")
        return session

    session = asyncio.run(run())
    store.stop(session["id"])

    rows = store.csv(session["id"]).strip().splitlines()
    assert rows[0] == "t,movement_score,threshold,motion,label"
    assert len(rows) > 1, "die Aufzeichnung darf nicht leer bleiben"
    body = [row.split(",") for row in rows[1:]]
    assert all(row[1] for row in body), "jede Zeile braucht einen Bewegungswert"
    assert {row[2] for row in body} == {"0.5"}, "die Schwelle muss mitgeschrieben werden"
    assert all(row[4] == "empty" for row in body)
