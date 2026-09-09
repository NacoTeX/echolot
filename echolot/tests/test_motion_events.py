"""What happened between two readings, written down.

From the 0.13.6 follow-up review (finding D). The chronological
simulation was there and sensible, but live motion pulses were kept
outside the numerical samples: a plain on→off between two score readings
was seen by the live latch and recorded nowhere. The replay runner could
not reconstruct it. That both call the same decision function is not
enough for the same result — they were running it over different inputs.

The rule that makes the stream safe: an event is never a measurement.
The crossing rate is events per second of observed time, and a motion
flip in that count would inflate the very number these exist to explain.
"""

import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import calibration, live_presence, presence_rate, replay, samples  # noqa: E402
from app.devices import Device, DeviceCreate  # noqa: E402
from app.telemetry import Sample  # noqa: E402
from app.zones import Zone  # noqa: E402

PROFILE = presence_rate.RateProfile(
    crossing_threshold=1e-3,
    baseline_rate=0.05,
    baseline_spread=0.01,
    window_seconds=60.0,
    sample_count=1200,
    observed_seconds=1200.0,
)


class FakeSubscription:
    def __init__(self, entity_ids, on_state, *, loop=None):
        self.entity_ids = list(entity_ids)
        self.on_state = on_state
        self.connected = False
        self.error = None

    def start(self):
        self.connected = True

    def stop(self):
        self.connected = False


def make_device(device_id="probe"):
    return Device(
        id=device_id, created_at=0, updated_at=0,
        config=DeviceCreate(name=device_id, board="esp32c6", wifi_ssid="netz",
                            wifi_password="passwort123"),
        entity_motion=f"binary_sensor.{device_id}_motion",
        entity_movement_score=f"sensor.{device_id}_score",
    )


@pytest.fixture
def bus(monkeypatch):
    monkeypatch.delenv("ECHOLOT_SAMPLE_SOURCE", raising=False)
    samples.bus.forget("probe")
    yield samples.bus
    samples.bus.forget("probe")


# --- the stream itself -------------------------------------------------


def test_a_motion_flip_is_written_down(bus):
    live = live_presence.LivePresence(subscription_factory=FakeSubscription)
    device = make_device()
    live.reconcile([device])
    stream = live.stream(device.id)

    stream._subscription.on_state(
        device.entity_motion, {"state": "on", "last_updated": "2026-09-08T12:00:00+00:00"}
    )
    stream._subscription.on_state(
        device.entity_motion, {"state": "off", "last_updated": "2026-09-08T12:00:00.4+00:00"}
    )
    recorded = bus.events(device.id)
    assert [event["value"] for event in recorded] == [True, False]
    assert [event["kind"] for event in recorded] == ["motion", "motion"]
    assert recorded[0]["t"] < recorded[1]["t"]
    live.stop_all()


def test_an_event_is_never_a_measurement(bus):
    """The whole reason they are kept apart."""
    live = live_presence.LivePresence(subscription_factory=FakeSubscription)
    device = make_device()
    live.reconcile([device])

    for state in ("on", "off", "on", "off"):
        live.stream(device.id)._subscription.on_state(
            device.entity_motion,
            {"state": state, "last_updated": f"2026-09-08T12:00:0{state == 'on'}+00:00"},
        )
    assert bus.events(device.id)
    assert bus.window(device.id, 3600.0) == [], "ein Ereignis wurde als Messwert gezählt"
    assert bus.snapshot(device.id)["sample_count"] == 0
    live.stop_all()


def test_a_repeated_value_is_not_an_event(bus):
    live = live_presence.LivePresence(subscription_factory=FakeSubscription)
    device = make_device()
    live.reconcile([device])
    for stamp in ("12:00:00", "12:00:01", "12:00:02"):
        live.stream(device.id)._subscription.on_state(
            device.entity_motion,
            {"state": "on", "last_updated": f"2026-09-08T{stamp}+00:00"},
        )
    assert len(bus.events(device.id)) == 1
    live.stop_all()


def test_an_event_from_a_source_nobody_reads_is_refused(bus):
    assert bus.publish_event("probe", samples.EVENT_MOTION, True,
                             at=time.time(), source=samples.SOURCE_DIRECT) is False
    assert bus.events("probe") == []


def test_restarting_a_buffer_leaves_a_trace(bus):
    """What happened still happened. A reader should see the break, not
    find the history quietly shortened."""
    bus.publish(
        "probe", Sample(t=time.time(), movement_score=0.1, threshold=0.0, motion=False),
        source=samples.SOURCE_HOME_ASSISTANT,
    )
    bus.restart("probe")
    assert bus.window("probe", 3600.0) == []
    assert [event["kind"] for event in bus.events("probe")] == [samples.EVENT_SOURCE]


# --- the recording -----------------------------------------------------


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(tmp_path))
    instance = calibration.CalibrationStore()
    instance.COALESCE_SECONDS = 0.01
    yield instance
    instance.close()


def test_a_recording_keeps_the_events_beside_the_readings(store):
    session = store.create("probe")
    store.ingest("probe", Sample(t=1.0, movement_score=0.1, threshold=0.5, motion=False))
    store.ingest_event("probe", {"t": 1.5, "kind": "motion", "value": True,
                                 "source": samples.SOURCE_HOME_ASSISTANT})
    store.ingest("probe", Sample(t=2.0, movement_score=0.1, threshold=0.5, motion=False))

    assert len(store.samples(session["id"])) == 2
    events = store.events(session["id"])
    assert len(events) == 1 and events[0]["value"] is True
    # The label the recording was carrying when it happened.
    assert events[0]["label"] == "unlabelled"


def test_the_sample_count_is_untouched_by_events(store):
    """A number people compare between recordings must not move because
    a new kind of row was added next to it."""
    session = store.create("probe")
    for index in range(5):
        store.ingest("probe", Sample(t=float(index), movement_score=0.1,
                                     threshold=0.5, motion=False))
    for index in range(20):
        store.ingest_event("probe", {"t": float(index), "kind": "motion",
                                     "value": bool(index % 2), "source": "home_assistant"})
    public = store.get(session["id"])
    assert public["sample_count"] == 5
    assert public["event_count"] == 20


def test_events_stop_at_their_own_limit(store, monkeypatch):
    monkeypatch.setattr(calibration, "MAX_EVENTS_PER_SESSION", 3)
    session = store.create("probe")
    for index in range(10):
        store.ingest_event("probe", {"t": float(index), "kind": "motion",
                                     "value": True, "source": "home_assistant"})
    assert len(store.events(session["id"])) == 3
    assert store.get(session["id"])["event_limit_reached"] is True


def test_an_import_says_it_has_no_events(store):
    """Home Assistant's recorder gives back the score series, not what
    happened between two of its points."""
    imported = store.adopt(
        "probe",
        [Sample(t=float(index), movement_score=0.1, threshold=0.5, motion=False)
         for index in range(30)],
        label="empty",
    )
    assert store.events(imported["id"]) == []


# --- live and replay on the same pulse ---------------------------------


def steady(score=0.0, span=240.0, count=960):
    """A recording where nothing the score can see ever changes."""
    return [{"t": span * index / (count - 1), "movement_score": score,
             "motion": False, "label": "empty"} for index in range(count)]


def test_a_motion_pulse_between_two_readings_reaches_replay():
    """The acceptance criterion: score constant, motion briefly on and
    off. Without the event stream the recording holds no trace of it and
    replay reports a room that never woke up."""
    rows = steady()
    events = [
        {"t": 100.0, "kind": "motion", "value": True, "source": "home_assistant"},
        {"t": 100.4, "kind": "motion", "value": False, "source": "home_assistant"},
    ]

    blind = replay.simulate(rows, PROFILE, hold_seconds=0.0)
    assert not any(step["to"] == "detected" for step in blind["transitions"])

    seeing = replay.simulate(rows, PROFILE, hold_seconds=0.0, events=events)
    detected = [step for step in seeing["transitions"] if step["to"] == "detected"]
    assert detected, "der Impuls fehlt im Replay"
    assert detected[0]["t"] == pytest.approx(100.0, abs=0.5)


def test_the_pulse_does_not_change_the_numerical_rate():
    """The events must not touch the crossing count."""
    rows = steady(score=0.5)
    events = [{"t": 100.0, "kind": "motion", "value": True, "source": "home_assistant"},
              {"t": 100.4, "kind": "motion", "value": False, "source": "home_assistant"}]

    without = replay.simulate(rows, PROFILE, hold_seconds=0.0, include_timeline=True)
    with_events = replay.simulate(rows, PROFILE, hold_seconds=0.0,
                                  events=events, include_timeline=True)
    rates_without = [step["rate_occupied"] for step in without["timeline"]
                     if 150 <= step["t"] <= 200]
    rates_with = [step["rate_occupied"] for step in with_events["timeline"]
                  if 150 <= step["t"] <= 200]
    assert rates_without == rates_with


def test_a_motion_event_schedules_a_look_of_its_own():
    """Live wakes on a motion change. A replay that only steps on score
    readings looks at different moments than the loop it reproduces."""
    rows = [{"t": 0.0, "movement_score": 0.0, "motion": False, "label": "empty"},
            {"t": 300.0, "movement_score": 0.0, "motion": False, "label": "empty"}]
    events = [{"t": 42.5, "kind": "motion", "value": True, "source": "home_assistant"}]
    assert 42.5 in replay._steps(rows, replay.DEFAULT_TICK_SECONDS, events)
    assert 42.5 not in replay._steps(rows, replay.DEFAULT_TICK_SECONDS)


def test_a_report_says_whether_events_were_recorded():
    """A recording made before 0.13.7 has none, and a reader should not
    have to assume nothing happened."""
    rows = steady()
    session = {"id": "s", "device_id": "probe", "started_at": 0.0, "ended_at": 240.0}
    report = replay.report(session, rows, events=[])
    assert report["simulation"]["events_recorded"] is True
    assert report["simulation"]["event_count"] == 0

    report = replay.report(session, rows)
    assert report["simulation"]["events_recorded"] is False
