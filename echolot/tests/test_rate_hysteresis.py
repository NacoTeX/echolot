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

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, presence_rate  # noqa: E402
from app.devices import Device, DeviceCreate  # noqa: E402
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


def window(crossings: int, *, span: float = 60.0, samples: int = 40, start: float = 0.0):
    """`crossings` readings above the threshold, spread over `span` seconds."""
    rows = []
    for index in range(samples):
        rows.append({
            "t": start + span * index / (samples - 1),
            "movement_score": 0.5 if index < crossings else 0.0,
        })
    return rows


class FakeStream:
    """A stand-in for one live subscription.

    `generation` is the stream's own number: replacing the object here
    stands for new data arriving on the same subscription, so the default
    keeps it. A *rebuilt* subscription — the device's entity ids changed
    — gets a different one, and that is a different source.
    """

    def __init__(self, rows, generation=1):
        self.rows = rows
        self.generation = generation

    def window(self, seconds, *, now=None):
        return list(self.rows)


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
    main._rate_state.clear()
    new_round()
    yield registry, streams
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

    streams[device.id] = FakeStream(window(30))          # well above enter
    assert main._zone_rate_verdict(zone) is True

    new_round()
    streams[device.id] = FakeStream(window(8, start=100.0))  # between the levels
    assert main._zone_rate_verdict(zone) is True, "die Ausschaltschwelle wurde nicht benutzt"


def test_a_device_that_was_idle_does_not_switch_on_between_the_levels(wired):
    registry, streams = wired
    device = make_device()
    registry[device.id] = device
    zone = Zone(id="z", created_at=0, updated_at=0, name="Wohnzimmer", device_ids=[device.id])

    streams[device.id] = FakeStream(window(0))
    assert main._zone_rate_verdict(zone) is False

    new_round()
    streams[device.id] = FakeStream(window(8, start=100.0))
    assert main._zone_rate_verdict(zone) is False


def test_member_order_does_not_change_a_devices_answer(wired):
    """Each device carries its own history, so shuffling a zone's members
    cannot make one of them read differently — neither inside a round,
    where the shared cache answers, nor in the round after it."""
    registry, streams = wired
    quiet, active = make_device("flur"), make_device("wohnzimmer")
    registry.update({quiet.id: quiet, active.id: active})
    streams[quiet.id] = FakeStream(window(0))
    streams[active.id] = FakeStream(window(30))

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
    streams[device.id] = FakeStream(window(30))

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

    streams[device.id] = FakeStream(window(30))
    assert main._zone_rate_verdict(zone) is True

    new_round()
    device.presence_profile = dict(PROFILE, baseline_rate=0.5)   # far higher bar
    streams[device.id] = FakeStream(window(8, start=100.0))
    assert main._zone_rate_verdict(zone) is False


def test_a_device_without_a_profile_says_nothing(wired):
    registry, streams = wired
    device = make_device(profile=None)
    registry[device.id] = device
    streams[device.id] = FakeStream(window(30))
    zone = Zone(id="z", created_at=0, updated_at=0, name="Z", device_ids=[device.id])
    assert main._zone_rate_verdict(zone) is None


def test_too_little_data_is_unknown_and_not_vacant(wired):
    registry, streams = wired
    device = make_device()
    registry[device.id] = device
    streams[device.id] = FakeStream([])
    zone = Zone(id="z", created_at=0, updated_at=0, name="Z", device_ids=[device.id])
    assert main._zone_rate_verdict(zone) is None


def test_a_gap_in_the_data_does_not_re_arm_the_enter_level(wired):
    """Someone sitting still through a dropout should not have to move
    again to be seen."""
    registry, streams = wired
    device = make_device()
    registry[device.id] = device
    zone = Zone(id="z", created_at=0, updated_at=0, name="Z", device_ids=[device.id])

    streams[device.id] = FakeStream(window(30))
    assert main._zone_rate_verdict(zone) is True

    new_round()
    streams[device.id] = FakeStream([])                       # dropout
    assert main._zone_rate_verdict(zone) is None

    new_round()
    streams[device.id] = FakeStream(window(8, start=200.0))   # back, still quiet
    assert main._zone_rate_verdict(zone) is True


def test_a_rebuilt_stream_does_not_inherit_the_old_memory(wired):
    """Correcting a device's entity ids rebuilds its subscription: empty
    buffer, possibly different entities. Carrying the hysteresis across
    would answer a question about the old source with the new one."""
    registry, streams = wired
    device = make_device()
    registry[device.id] = device
    zone = Zone(id="z", created_at=0, updated_at=0, name="Z", device_ids=[device.id])

    streams[device.id] = FakeStream(window(30), generation=1)
    assert main._zone_rate_verdict(zone) is True

    new_round()
    # Same data between the levels, but a new subscription behind it.
    streams[device.id] = FakeStream(window(8, start=100.0), generation=2)
    assert main._zone_rate_verdict(zone) is False


def test_every_live_stream_gets_its_own_number(wired):
    """`id()` used to serve as the generation, and CPython gives the
    freed address of the old stream straight to its replacement."""
    from app import live_presence

    seen = set()
    for _ in range(50):
        stream = live_presence.DeviceStream(
            make_device(), subscription_factory=lambda *a, **k: object()
        )
        seen.add(stream.generation)
    assert len(seen) == 50


def test_a_deleted_device_leaves_no_state_behind(wired):
    registry, streams = wired
    device = make_device()
    registry[device.id] = device
    streams[device.id] = FakeStream(window(30))
    zone = Zone(id="z", created_at=0, updated_at=0, name="Z", device_ids=[device.id])
    assert main._zone_rate_verdict(zone) is True

    new_round()
    registry.pop(device.id)
    assert main._zone_rate_verdict(zone) is None
    assert device.id not in main._rate_state
