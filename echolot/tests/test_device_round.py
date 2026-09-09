"""One device, read once per round, and every zone sees the same thing.

From the 0.13.6 follow-up review (findings A, B and C).

**A.** `compute_zone_state` read Home Assistant per *membership* and
consumed the motion latch per *membership*. A device in two zones
therefore cost two reads, and only the zone evaluated first saw a short
pulse — the reviewer reproduced `[True, False]` for the same device at
the same instant.

**B.** The rate ignored `stream.connected`, so a subscription that had
dropped kept answering from whatever was left in the buffer. And the
window ended at the newest reading rather than at the tick, so a source
that had stopped delivering was judged on its own last minute for as
long as anything remained.

**C.** The production rate read the Home Assistant subscription's own
buffer while calibration, fusion and replay read the canonical bus — two
different sources of readings for the same question. A profile learned
over one transport was applied to another with only a warning.
"""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import live_presence, main, presence_rate, samples  # noqa: E402
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


def make_device(device_id="probe", profile=None):
    device = Device(
        id=device_id, created_at=0, updated_at=0,
        config=DeviceCreate(name=device_id, board="esp32c6", wifi_ssid="netz",
                            wifi_password="passwort123"),
        entity_motion=f"binary_sensor.{device_id}_motion",
        entity_movement_score=f"sensor.{device_id}_score",
    )
    device.presence_profile = profile
    return device


class FakeStream:
    """A subscription, and whether it is up. Readings come from the bus."""

    def __init__(self, connected=True, pulse_at=None):
        self.connected = connected
        self._pulse_at = pulse_at

    def motion_pulsed(self):
        at, self._pulse_at = self._pulse_at, None
        return live_presence.MotionPulse(happened=at is not None, at=at)


def busy(device_id, *, crossings=40, count=60, span=60.0):
    """A stretch of readings on the canonical bus, ending now."""
    end = time.time()
    samples.bus.forget(device_id)
    for index in range(count):
        samples.bus.publish(
            device_id,
            Sample(t=end - span + span * index / (count - 1),
                   movement_score=0.5 if index < crossings else 0.0,
                   threshold=0.0, motion=False),
            source=samples.SOURCE_HOME_ASSISTANT,
        )


@pytest.fixture
def wired(monkeypatch):
    from app import feature_api

    registry: dict[str, Device] = {}
    streams: dict[str, FakeStream] = {}
    reads: list[str] = []

    async def read_state(device, allow_detect=True):
        reads.append(device.id)
        return {"available": True, "motion": False,
                "movement_score": 0.0, "threshold": 0.5}

    monkeypatch.delenv("ECHOLOT_SAMPLE_SOURCE", raising=False)
    monkeypatch.setattr(main.devices, "get_device", registry.get)
    monkeypatch.setattr(main, "_read_device_state", read_state)
    monkeypatch.setattr(feature_api, "live",
                        type("Live", (), {"stream": staticmethod(streams.get)})())
    monkeypatch.setattr(main, "evaluator", main.ZoneEvaluator())
    main._rate_state.clear()
    yield registry, streams, reads
    for device_id in list(streams):
        samples.bus.forget(device_id)
    main._rate_state.clear()


def zone(zone_id, *device_ids):
    return Zone(id=zone_id, created_at=0, updated_at=0, name=zone_id.title(),
                device_ids=list(device_ids))


# --- A: one round, one device -----------------------------------------


def test_two_zones_sharing_a_device_see_the_same_pulse(wired):
    """The regression, in the reviewer's own shape: the round returned
    `[True, False]` for one device at one instant."""
    registry, streams, _reads = wired
    device = make_device()
    registry[device.id] = device
    streams[device.id] = FakeStream(pulse_at=time.time())

    result = asyncio.run(main.evaluator.cycle([zone("a", device.id), zone("b", device.id)]))
    occupied = [state["occupied"] for _zone, state in result]
    assert occupied == [True, True], f"nur eine Zone sah den Impuls: {occupied}"


def test_the_order_of_the_zones_cannot_change_it(wired):
    registry, streams, _reads = wired
    device = make_device()
    registry[device.id] = device

    for order in (["a", "b"], ["b", "a"]):
        streams[device.id] = FakeStream(pulse_at=time.time())
        main._zone_runtimes.clear()
        result = asyncio.run(
            main.evaluator.cycle([zone(name, device.id) for name in order])
        )
        assert all(state["occupied"] for _zone, state in result)


def test_ten_zones_with_one_device_cost_one_read(wired):
    """It was one read per membership, so a device in ten zones was ten
    round trips into Home Assistant every round, forever."""
    registry, streams, reads = wired
    device = make_device()
    registry[device.id] = device
    streams[device.id] = FakeStream()

    asyncio.run(main.evaluator.cycle([zone(f"z{index}", device.id) for index in range(10)]))
    assert reads.count(device.id) == 1, f"{reads.count(device.id)} Abfragen für ein Gerät"


def test_a_pulse_carries_when_it_happened(wired):
    """A bare boolean could not tell a doorway crossing a moment ago from
    one that had been sitting in the latch since the connection dropped —
    so its mere existence marked a dead source as healthy."""
    registry, streams, _reads = wired
    device = make_device()
    registry[device.id] = device
    moment = time.time() - 3.0
    streams[device.id] = FakeStream(pulse_at=moment)

    snapshots = asyncio.run(main.read_round([device.id]))
    seen = snapshots[device.id]
    assert seen.motion_pulse is True
    assert seen.motion_since == pytest.approx(moment)
    assert seen.as_member()["motion_since"] == pytest.approx(moment)


def test_a_round_is_a_fact_and_cannot_be_edited(wired):
    """A zone that could edit it would be editing what the others see."""
    registry, streams, _reads = wired
    device = make_device()
    registry[device.id] = device
    streams[device.id] = FakeStream()

    snapshots = asyncio.run(main.read_round([device.id]))
    with pytest.raises(Exception):
        snapshots[device.id].motion = True


# --- B: a dropped transport is not evidence ---------------------------


def test_a_disconnected_subscription_makes_the_rate_unknown(wired):
    """An open socket does not prove the ESP is measuring, but a closed
    one proves it is not — and that is a sound one-way inference."""
    registry, streams, _reads = wired
    device = make_device(profile=PROFILE.as_dict())
    registry[device.id] = device
    busy(device.id)

    streams[device.id] = FakeStream(connected=True)
    verdict, reason = main._device_rate_evidence(device, connected=True)
    assert verdict is True and reason is None

    main.evaluator._device_verdicts.clear()
    streams[device.id] = FakeStream(connected=False)
    verdict, reason = main._device_rate_evidence(device, connected=False)
    assert verdict is None
    assert reason == main.RATE_DISCONNECTED


def test_a_dropout_keeps_the_memory(wired):
    """Somebody sitting still through one should not have to move again
    to be seen. A dropout is not a recalibration."""
    registry, streams, _reads = wired
    device = make_device(profile=PROFILE.as_dict())
    registry[device.id] = device
    busy(device.id)
    streams[device.id] = FakeStream()
    assert main._device_rate_evidence(device, connected=True)[0] is True

    main.evaluator._device_verdicts.clear()
    main._device_rate_evidence(device, connected=False)
    assert device.id in main._rate_state, "das Gedächtnis wurde beim Aussetzer verworfen"


def test_the_window_ends_at_the_tick_not_at_the_newest_reading():
    """A source that stopped delivering was judged on its own last minute
    for as long as anything remained in the buffer: the window slid with
    the data instead of with the clock."""
    end = 1000.0
    rows = [{"t": end - 60 + index, "movement_score": 0.5 if index < 40 else 0.0}
            for index in range(60)]

    # Judged at the moment the readings end: a real answer.
    assert presence_rate.evaluate(PROFILE, rows, window_end=end)["available"] is True
    # Judged five minutes later: the window is empty, and saying "vacant"
    # would be inventing evidence.
    stale = presence_rate.evaluate(PROFILE, rows, window_end=end + 300)
    assert stale["available"] is False
    assert stale["occupied"] is None


# --- C: one canonical channel -----------------------------------------


def test_the_rate_reads_the_same_channel_as_calibration(wired):
    """It read the Home Assistant subscription's own buffer, so the
    production rate and everything that judges it were two different
    sources of readings."""
    registry, streams, _reads = wired
    device = make_device(profile=PROFILE.as_dict())
    registry[device.id] = device
    streams[device.id] = FakeStream()

    busy(device.id)
    assert main._device_rate_evidence(device, connected=True)[0] is True

    # Emptying the canonical bus is enough to silence the rate. If it
    # were reading anything else, this would not be true.
    main.evaluator._device_verdicts.clear()
    samples.bus.forget(device.id)
    assert main._device_rate_evidence(device, connected=True)[0] is None


def test_a_profile_from_another_transport_stops_the_slow_path(wired):
    """The overview warned and the evaluation went ahead anyway. What a
    room does empty is a fact about that room measured over one path."""
    registry, streams, _reads = wired
    device = make_device(profile={**PROFILE.as_dict(), "source": samples.SOURCE_DIRECT})
    registry[device.id] = device
    streams[device.id] = FakeStream()
    busy(device.id)

    verdict, reason = main._device_rate_evidence(device, connected=True)
    assert verdict is None
    assert reason == main.RATE_SOURCE_MISMATCH


def test_the_fast_motion_path_is_untouched_by_a_mismatch(wired):
    """Only the slow evidence goes quiet. A zone must not lose its motion
    detection because a baseline was learned over the wrong transport."""
    registry, streams, _reads = wired
    device = make_device(profile={**PROFILE.as_dict(), "source": samples.SOURCE_DIRECT})
    registry[device.id] = device
    streams[device.id] = FakeStream(pulse_at=time.time())

    result = asyncio.run(main.evaluator.cycle([zone("a", device.id)]))
    _zone, state = result[0]
    assert state["occupied"] is True
    assert state["members"][0]["rate_reason"] == main.RATE_SOURCE_MISMATCH


def test_a_matching_profile_is_judged_normally(wired):
    registry, streams, _reads = wired
    device = make_device(
        profile={**PROFILE.as_dict(), "source": samples.SOURCE_HOME_ASSISTANT}
    )
    registry[device.id] = device
    streams[device.id] = FakeStream()
    busy(device.id)
    assert main._device_rate_evidence(device, connected=True) == (True, None)


def test_a_mismatch_drops_the_memory_it_was_not_entitled_to(wired):
    registry, streams, _reads = wired
    device = make_device(profile=PROFILE.as_dict())
    registry[device.id] = device
    streams[device.id] = FakeStream()
    busy(device.id)
    assert main._device_rate_evidence(device, connected=True)[0] is True
    assert device.id in main._rate_state

    device.presence_profile = {**PROFILE.as_dict(), "source": samples.SOURCE_DIRECT}
    main.evaluator._device_verdicts.clear()
    main._device_rate_evidence(device, connected=True)
    assert device.id not in main._rate_state


def test_a_source_change_restarts_every_buffer(wired, monkeypatch):
    """Readings from before the change were measured the other way, and a
    window that straddles it is two measurements added together."""
    _registry, streams, _reads = wired
    streams["probe"] = FakeStream()
    busy("probe")
    before = samples.bus.generation("probe")
    assert samples.bus.window("probe", 120.0)

    monkeypatch.setenv("ECHOLOT_SAMPLE_SOURCE", "direct")
    samples.bus.publish(
        "probe",
        Sample(t=time.time(), movement_score=0.1, threshold=0.0, motion=False),
        source=samples.SOURCE_DIRECT,
    )
    assert samples.bus.generation("probe") != before
    assert len(samples.bus.window("probe", 120.0)) == 1
