"""One loop decides what every zone is doing; nothing else evaluates.

From the external review (R1). Three separate failures had the same
cause — evaluation lived wherever somebody happened to ask for it.

Reproduction C: the per-device rate verdict was memoised on the newest
reading. A rolling window also moves at its *left* edge, so twelve high
readings that had aged out of the last minute kept the room occupied
until something new arrived. The reviewer's own numbers: direct
evaluation said False while the cached answer still said True.

Reproduction D: the freshness check was global while a round could cover
a subset of zones, so a zone created inside the throttle was skipped —
and then a GET on it started an evaluation of its own, which is how an
open dashboard came to be driving the state machine.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, presence_rate, zone_logic  # noqa: E402
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


class FakeStream:
    generation = 1

    def __init__(self, rows):
        self.rows = rows

    def window(self, seconds, *, now=None):
        return list(self.rows)


class FakeLive:
    def __init__(self, streams):
        self.streams = streams

    def stream(self, device_id):
        return self.streams.get(device_id)


def make_device(device_id="wohnzimmer"):
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
    device.presence_profile = dict(PROFILE)
    return device


def zone(zone_id, *device_ids):
    return Zone(
        id=zone_id, created_at=0, updated_at=0, name=zone_id.title(),
        device_ids=list(device_ids),
    )


# --- reproduction C ----------------------------------------------------


@pytest.fixture
def wired(monkeypatch):
    from app import feature_api

    registry: dict[str, Device] = {}
    streams: dict[str, FakeStream] = {}
    monkeypatch.setattr(main.devices, "get_device", lambda i: registry.get(i))
    monkeypatch.setattr(feature_api, "live", FakeLive(streams))
    main._rate_state.clear()
    main.evaluator._device_verdicts.clear()
    yield registry, streams
    main._rate_state.clear()
    main.evaluator._device_verdicts.clear()


def test_readings_falling_off_the_left_edge_change_the_answer(wired):
    """The reviewer's reproduction C, with their numbers.

    Twelve high readings at the start of a minute, then nothing. The
    newest reading never changes — so the verdict must not be keyed on
    it. Once the twelve have aged out, the window is empty of crossings
    and the room is not occupied any more.
    """
    registry, streams = wired
    device = make_device()
    registry[device.id] = device
    room = zone("z", device.id)

    full = [{"t": t, "movement_score": 0.01 if t < 12 else 0.0} for t in range(61)]
    streams[device.id] = FakeStream(full)
    assert main._zone_rate_verdict(room) is True

    main.evaluator._device_verdicts.clear()          # the next round
    aged_out = [row for row in full if row["t"] >= 12]
    streams[device.id] = FakeStream(aged_out)

    profile = presence_rate.profile_from_dict(PROFILE)
    direct = presence_rate.evaluate(profile, aged_out, occupied_now=True)
    assert direct["occupied"] is False
    assert main._zone_rate_verdict(room) == direct["occupied"], (
        "der zwischengespeicherte Wert weicht von der direkten Bewertung ab"
    )


# --- reproduction D ----------------------------------------------------


def evaluated(monkeypatch):
    """Record which zones each round actually computed."""
    seen: list[str] = []

    async def compute(zone_):
        seen.append(zone_.id)
        return {
            "available": True,
            "members": [],
            **zone_logic.ZoneEvaluation(
                state=zone_logic.CLEAR, occupied=False, hold_remaining=0.0,
                raw_motion=False, score=None,
            ).as_dict(),
        }

    monkeypatch.setattr(main, "compute_zone_state", compute)
    return seen


def test_a_round_always_covers_every_zone(monkeypatch):
    """A zone created between two rounds was skipped by the next one and
    then had no snapshot at all — which the overview turned into a 500."""
    seen = evaluated(monkeypatch)
    evaluator = main.ZoneEvaluator()
    a, b = zone("a"), zone("b")

    asyncio.run(evaluator.cycle([a]))
    assert seen == ["a"]

    asyncio.run(evaluator.cycle([a, b]))
    assert sorted(seen[1:]) == ["a", "b"]
    assert evaluator.snapshot("b") is not None


def test_reading_a_zones_state_never_evaluates_it(monkeypatch):
    """An open dashboard polls. Every poll used to start a round, so the
    hold timer and the Home Assistant request rate depended on who was
    looking."""
    seen = evaluated(monkeypatch)
    evaluator = main.ZoneEvaluator()
    a = zone("a")
    asyncio.run(evaluator.cycle([a]))

    for _ in range(20):
        evaluator.state_of(a)
    assert seen == ["a"]


def test_a_zone_with_no_round_behind_it_yet_reads_as_pending(monkeypatch):
    """Not an error, and not "clear" either: nothing has looked yet."""
    evaluator = main.ZoneEvaluator()
    state = evaluator.state_of(zone("frisch"))
    assert state["pending"] is True
    assert state["available"] is False
    # Every key the overview reads has to be there.
    for key in ("state", "occupied", "hold_remaining", "raw_motion", "score", "members"):
        assert key in state


def test_a_deleted_zone_leaves_no_snapshot_behind(monkeypatch):
    seen = evaluated(monkeypatch)
    evaluator = main.ZoneEvaluator()
    a, b = zone("a"), zone("b")
    asyncio.run(evaluator.cycle([a, b]))
    assert evaluator.snapshot("b") is not None

    asyncio.run(evaluator.cycle([a]))
    assert evaluator.snapshot("b") is None
    assert seen[-1] == "a"


# --- what the routes say about a zone nobody has looked at yet ---------


def test_a_brand_new_zone_answers_pending_over_the_api(monkeypatch, tmp_path):
    """The overview read five keys out of a two-key stand-in and returned
    an HTTP 500 for the crime of making a zone. It now says what is
    actually true: nothing has looked at this one yet."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ECHOLOT_MQTT_EXPORT", "false")
    from app import zones as zones_module

    monkeypatch.setattr(main, "evaluator", main.ZoneEvaluator())

    async def never_evaluated(z):                     # the loop never gets a turn
        raise AssertionError("die Zone wurde ausgewertet")

    monkeypatch.setattr(main, "compute_zone_state", never_evaluated)
    monkeypatch.setattr(main.evaluator, "run", lambda _list: asyncio.sleep(0))

    with TestClient(main.app) as client:
        created = client.post("/api/zones", json={"name": "Frisch", "device_ids": []})
        assert created.status_code == 201
        zone_id = created.json()["id"]

        state = client.get(f"/api/zones/{zone_id}/state")
        assert state.status_code == 200
        assert state.json()["pending"] is True

        overview = client.get("/api/overview")
        assert overview.status_code == 200
        entry = next(z for z in overview.json()["zones"] if z["id"] == zone_id)
        assert entry["pending"] is True
        assert entry["state"] == "clear" and entry["occupied"] is False

        assert client.delete(f"/api/zones/{zone_id}").status_code == 204
    zones_module.delete_zone(zone_id)


def test_a_device_that_left_every_zone_leaves_no_hysteresis_behind(monkeypatch, wired):
    """The memory is per device and survives rounds on purpose. A device
    nobody measures any more is not a short outage."""
    registry, streams = wired
    device = make_device()
    registry[device.id] = device
    streams[device.id] = FakeStream(
        [{"t": t, "movement_score": 0.5} for t in range(61)]
    )
    # Only the rate path, so this test does not depend on Home Assistant
    # being reachable to read the members.
    async def rate_only(z):
        return {
            "available": True, "members": [],
            "rate_occupied": main._zone_rate_verdict(z),
            "state": "clear", "occupied": False,
        }

    monkeypatch.setattr(main, "compute_zone_state", rate_only)
    evaluator = main.ZoneEvaluator()
    monkeypatch.setattr(main, "evaluator", evaluator)

    asyncio.run(evaluator.cycle([zone("z", device.id)]))
    assert device.id in main._rate_state

    asyncio.run(evaluator.cycle([zone("z")]))
    assert device.id not in main._rate_state


def test_a_listener_that_throws_does_not_stop_the_round(monkeypatch):
    evaluated(monkeypatch)
    evaluator = main.ZoneEvaluator()
    seen = []
    evaluator.add_listener(lambda _states: (_ for _ in ()).throw(RuntimeError("nope")))
    evaluator.add_listener(seen.append)
    asyncio.run(evaluator.cycle([zone("a")]))
    assert len(seen) == 1
