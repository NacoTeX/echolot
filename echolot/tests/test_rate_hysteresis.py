"""The crossing rate's hysteresis needs a memory of its own.

From the external review (P1 #2). `presence_rate.evaluate` has two levels
— a higher one to switch on, a lower one to stay on — and picks between
them from `occupied_now`, which is meant to be *that device's* previous
verdict. 0.13.2 passed the running OR of the zone loop instead, so the
first device in every zone was always judged as if it had just been
vacant. The exit level was therefore never used, and the one case the two
levels exist for — a room that has gone quiet but not silent — dropped
out immediately.

The numbers below are the review's: baseline 0.084/s gives an enter level
of 0.168/s and an exit level of 0.1008/s, and a window at 8 crossings in
60 s sits at 0.1333/s — between the two.
"""

import os
import sys
import tempfile
from pathlib import Path

import time

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, presence_rate, samples  # noqa: E402
from app.devices import Device, DeviceCreate  # noqa: E402
from app.telemetry import Sample  # noqa: E402
from app.zones import Zone  # noqa: E402

PROFILE = {
    "crossing_threshold": 1e-3,
    "baseline_rate": 0.084,
    "baseline_spread": 0.01,
    "window_seconds": 60.0,
    "sample_count": 1200,
    "observed_seconds": 1200.0,
    "version": presence_rate.PROFILE_VERSION,
}


def window(crossings: int, *, span: float = 60.0, count: int = 40, ago: float = 0.0):
    """`crossings` readings above the threshold, spread over `span` seconds.

    Stamped against the wall clock, because that is what the canonical
    bus windows against and what Home Assistant stamps a reading with.
    `ago` pushes the whole stretch that many seconds into the past.
    """
    end = time.time() - ago
    rows = []
    for index in range(count):
        rows.append({
            "t": end - span + span * index / (count - 1),
            "movement_score": 0.5 if index < crossings else 0.0,
        })
    return rows


def feed(device_id, rows):
    """Put readings on the canonical bus, the way the live stream does.

    The production rate reads this and nothing else since 0.13.7 — it
    used to read the Home Assistant subscription's own buffer, so the
    rate and everything that judges it were two different sources of
    readings.

    The buffer is emptied first so each call is a whole window rather
    than an accumulation, but the *generation* is left alone: this is new
    data on the same subscription, which is what a live stream produces.
    `restart()` means the opposite — the readings from here are not a
    continuation — and one test below uses it for exactly that.
    """
    samples.bus._samples.pop(device_id, None)
    for row in rows:
        samples.bus.publish(
            device_id,
            Sample(t=row["t"], movement_score=row["movement_score"],
                   threshold=0.0, motion=False),
            source=samples.SOURCE_HOME_ASSISTANT,
        )


class FakeStream:
    """A stand-in for one live subscription.

    Only its connection state matters to the rate now; the readings come
    from the bus. A stream that is not connected is proof the source is
    not delivering, and the rate must say unknown rather than answer from
    what is left in the buffer.
    """

    def __init__(self, connected=True):
        self.connected = connected


class FakeLive:
    def __init__(self, streams):
        self.streams = streams

    def stream(self, device_id):
        return self.streams.get(device_id)


def make_device(device_id="wohnzimmer", profile=PROFILE):
    device = Device(
        id=device_id,
        created_at=0,
        updated_at=0,
        config=DeviceCreate(
            name=device_id, board="esp32c6", wifi_ssid="netz", wifi_password="passwort123"
        ),
        entity_motion=f"binary_sensor.{device_id}_motion",
        entity_movement_score=f"sensor.{device_id}_score",
    )
    device.presence_profile = dict(profile) if profile else None
    return device


def new_round():
    """Start a fresh evaluation round.

    The evaluator clears its per-tick device cache at the top of every
    cycle, so a device is evaluated once per round however many zones
    hold it. A test that means "later, with new data" has to say so —
    otherwise it is asking the same round twice and gets the cached
    answer, which is the intended behaviour, not a bug.
    """
    main.evaluator._device_verdicts.clear()


@pytest.fixture
def wired(monkeypatch):
    """Point main at fake devices and fake live streams, and start clean."""
    from app import feature_api

    registry: dict[str, Device] = {}
    streams: dict[str, FakeStream] = {}
    monkeypatch.setattr(main.devices, "get_device", lambda i: registry.get(i))
    monkeypatch.setattr(feature_api, "live", FakeLive(streams))
    monkeypatch.delenv("ECHOLOT_SAMPLE_SOURCE", raising=False)
    main._rate_state.clear()
    new_round()
    yield registry, streams
    for device_id in list(streams):
        samples.bus.forget(device_id)
    main._rate_state.clear()
    new_round()


def levels():
    profile = presence_rate.profile_from_dict(PROFILE)
    return profile.enter_rate, profile.exit_rate


def test_the_reviews_numbers_still_describe_this_profile():
    """If the levels move, the rest of this file is measuring something else."""
    enter, exit_ = levels()
    assert round(enter, 4) == 0.168
    assert round(exit_, 4) == 0.1008
    rate = presence_rate.event_rate(window(8), 1e-3)
    assert exit_ < rate < enter


def test_a_device_that_was_active_stays_active_between_the_levels(wired):
    """The regression: this used to read True, then False."""
    registry, streams = wired
    device = make_device()
    registry[device.id] = device
    zone = Zone(id="z", created_at=0, updated_at=0, name="Wohnzimmer", device_ids=[device.id])

    streams[device.id] = FakeStream()
    feed(device.id, window(30))          # well above enter
    assert main._zone_rate_verdict(zone) is True

    new_round()
    streams[device.id] = FakeStream()
    feed(device.id, window(8, ago=0.0))  # between the levels
    assert main._zone_rate_verdict(zone) is True, "die Ausschaltschwelle wurde nicht benutzt"


def test_a_device_that_was_idle_does_not_switch_on_between_the_levels(wired):
    registry, streams = wired
    device = make_device()
    registry[device.id] = device
    zone = Zone(id="z", created_at=0, updated_at=0, name="Wohnzimmer", device_ids=[device.id])

    streams[device.id] = FakeStream()
    feed(device.id, window(0))
    assert main._zone_rate_verdict(zone) is False

    new_round()
    streams[device.id] = FakeStream()
    feed(device.id, window(8, ago=0.0))
    assert main._zone_rate_verdict(zone) is False


def test_member_order_does_not_change_a_devices_answer(wired):
    """Each device carries its own history, so shuffling a zone's members
    cannot make one of them read differently — neither inside a round,
    where the shared cache answers, nor in the round after it."""
    registry, streams = wired
    quiet, active = make_device("flur"), make_device("wohnzimmer")
    registry.update({quiet.id: quiet, active.id: active})
    streams[quiet.id] = FakeStream()
    feed(quiet.id, window(0))
    streams[active.id] = FakeStream()
    feed(active.id, window(30))

    forwards = Zone(id="a", created_at=0, updated_at=0, name="A",
                    device_ids=[quiet.id, active.id])
    assert main._zone_rate_verdict(forwards) is True
    quiet_state = dict(main._rate_state[quiet.id])

    new_round()
    backwards = Zone(id="b", created_at=0, updated_at=0, name="B",
                     device_ids=[active.id, quiet.id])
    assert main._zone_rate_verdict(backwards) is True
    assert main._rate_state[quiet.id]["remembered"] == quiet_state["remembered"]


def test_a_device_in_two_zones_is_evaluated_once_per_reading(wired):
    """Otherwise the second zone advances the hysteresis a second time and
    the two zones can disagree about the same device."""
    registry, streams = wired
    device = make_device()
    registry[device.id] = device
    streams[device.id] = FakeStream()
    feed(device.id, window(30))

    calls = {"n": 0}
    real = presence_rate.evaluate

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    presence_rate.evaluate = counting
    try:
        one = Zone(id="a", created_at=0, updated_at=0, name="A", device_ids=[device.id])
        two = Zone(id="b", created_at=0, updated_at=0, name="B", device_ids=[device.id])
        assert main._zone_rate_verdict(one) is True
        assert main._zone_rate_verdict(two) is True
    finally:
        presence_rate.evaluate = real
    assert calls["n"] == 1


def test_recalibrating_drops_the_old_history(wired):
    """A new profile moves the levels, so a verdict from the old one is
    an answer to a different question."""
    registry, streams = wired
    device = make_device()
    registry[device.id] = device
    zone = Zone(id="z", created_at=0, updated_at=0, name="Z", device_ids=[device.id])

    streams[device.id] = FakeStream()
    feed(device.id, window(30))
    assert main._zone_rate_verdict(zone) is True

    new_round()
    device.presence_profile = dict(PROFILE, baseline_rate=0.5)   # far higher bar
    streams[device.id] = FakeStream()
    feed(device.id, window(8, ago=0.0))
    assert main._zone_rate_verdict(zone) is False


def test_a_device_without_a_profile_says_nothing(wired):
    registry, streams = wired
    device = make_device(profile=None)
    registry[device.id] = device
    streams[device.id] = FakeStream()
    feed(device.id, window(30))
    zone = Zone(id="z", created_at=0, updated_at=0, name="Z", device_ids=[device.id])
    assert main._zone_rate_verdict(zone) is None


def test_too_little_data_is_unknown_and_not_vacant(wired):
    registry, streams = wired
    device = make_device()
    registry[device.id] = device
    streams[device.id] = FakeStream()
    feed(device.id, [])
    zone = Zone(id="z", created_at=0, updated_at=0, name="Z", device_ids=[device.id])
    assert main._zone_rate_verdict(zone) is None


def test_a_gap_in_the_data_does_not_re_arm_the_enter_level(wired):
    """Someone sitting still through a dropout should not have to move
    again to be seen."""
    registry, streams = wired
    device = make_device()
    registry[device.id] = device
    zone = Zone(id="z", created_at=0, updated_at=0, name="Z", device_ids=[device.id])

    streams[device.id] = FakeStream()
    feed(device.id, window(30))
    assert main._zone_rate_verdict(zone) is True

    new_round()
    streams[device.id] = FakeStream()
    feed(device.id, [])                       # dropout
    assert main._zone_rate_verdict(zone) is None

    new_round()
    streams[device.id] = FakeStream()
    feed(device.id, window(8, ago=0.0))   # back, still quiet
    assert main._zone_rate_verdict(zone) is True


def test_a_rebuilt_stream_does_not_inherit_the_old_memory(wired):
    """Correcting a device's entity ids rebuilds its subscription: empty
    buffer, possibly different entities. Carrying the hysteresis across
    would answer a question about the old source with the new one."""
    registry, streams = wired
    device = make_device()
    registry[device.id] = device
    zone = Zone(id="z", created_at=0, updated_at=0, name="Z", device_ids=[device.id])

    streams[device.id] = FakeStream()
    feed(device.id, window(30))
    assert main._zone_rate_verdict(zone) is True

    new_round()
    # Same data between the levels, but a rebuilt subscription behind it.
    # `restart` is what LivePresence.reconcile calls when a device's
    # entity ids change: the buffer holds readings from the old entities,
    # and the memory was built from verdicts about them.
    streams[device.id] = FakeStream()
    samples.bus.restart(device.id)
    feed(device.id, window(8, ago=0.0))
    assert main._zone_rate_verdict(zone) is False


def test_every_run_of_readings_gets_its_own_number(wired):
    """`id()` used to serve as the generation, and CPython gives the
    freed address of the old stream straight to its replacement."""
    seen = set()
    for _ in range(50):
        seen.add(samples.bus.restart("wohnzimmer"))
    assert len(seen) == 50


def test_a_rebuilt_subscription_drops_the_canonical_buffer_too(wired):
    """The stream was replaced and this buffer was not, so readings from
    the old entities stayed in the window and the hysteresis carried
    straight across."""
    from app import live_presence

    class FakeSubscription:
        def __init__(self, entity_ids, on_state, *, loop=None):
            self.entity_ids = list(entity_ids)
            self.connected = False
            self.error = None

        def start(self):
            self.connected = True

        def stop(self):
            self.connected = False

    live = live_presence.LivePresence(subscription_factory=FakeSubscription)
    device = make_device("flur")
    live.reconcile([device])
    feed(device.id, window(30))
    before = samples.bus.generation(device.id)
    assert samples.bus.window(device.id, 120.0)

    device.entity_movement_score = "sensor.korrigiert"
    live.reconcile([device])
    assert samples.bus.window(device.id, 120.0) == []
    assert samples.bus.generation(device.id) != before
    live.stop_all()
    samples.bus.forget(device.id)


def test_a_deleted_device_leaves_no_state_behind(wired):
    registry, streams = wired
    device = make_device()
    registry[device.id] = device
    streams[device.id] = FakeStream()
    feed(device.id, window(30))
    zone = Zone(id="z", created_at=0, updated_at=0, name="Z", device_ids=[device.id])
    assert main._zone_rate_verdict(zone) is True

    new_round()
    registry.pop(device.id)
    assert main._zone_rate_verdict(zone) is None
    assert device.id not in main._rate_state
