"""Zone state has one owner, and everything else reads its answer.

From the external review (recommended step 6). `compute_zone_state`
mutates the zone's runtime — the motion hysteresis memory and the hold
deadline — and re-reads every member device from Home Assistant. It was
called from three places: the dashboard's poll, the overview, and the
MQTT publisher.

Making the publisher event-driven earlier in 0.13.5 made that worse
rather than better: a reading arriving three times a second became three
full rounds of Home Assistant requests a second, on top of whatever an
open dashboard was already asking for.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main  # noqa: E402
from app.zones import Zone  # noqa: E402


def zone(zone_id="z1", name="Küche"):
    return Zone(id=zone_id, created_at=0, updated_at=0, name=name, device_ids=[])


@pytest.fixture
def counted(monkeypatch):
    """Count evaluations, and give the evaluator a clean slate."""
    calls = {"n": 0}

    async def fake_compute(z):
        calls["n"] += 1
        return {"available": True, "members": [], "state": "clear", "occupied": False}

    monkeypatch.setattr(main, "compute_zone_state", fake_compute)
    monkeypatch.setattr(main, "evaluator", main.ZoneEvaluator())
    return calls


def test_a_burst_of_readings_does_not_become_a_burst_of_requests(counted):
    """The regression this introduces a floor for: with the publisher
    woken by every reading, an unthrottled evaluator would re-read Home
    Assistant several times a second."""
    async def scenario():
        zones = [zone()]
        for _ in range(20):
            await main.evaluator.refresh(zones)

    asyncio.run(scenario())
    assert counted["n"] == 1, f"{counted['n']} Auswertungen für einen Schwall"


def test_the_first_call_does_evaluate(counted):
    asyncio.run(main.evaluator.refresh([zone()]))
    assert counted["n"] == 1


def test_the_dashboard_reads_the_snapshot(counted):
    """An open dashboard polling every two seconds costs no Home
    Assistant requests: it reads what the evaluator already worked out."""
    async def scenario():
        target = zone()
        await main.evaluator.refresh([target])
        before = counted["n"]
        for _ in range(5):
            state = await main.evaluator.state_of(target)
            assert state["state"] == "clear"
        return before

    before = asyncio.run(scenario())
    assert counted["n"] == before


def test_a_zone_nobody_has_evaluated_yet_is_evaluated(counted):
    """The one case that has nothing to fall back on."""
    async def scenario():
        await main.evaluator.refresh([zone("z1")])
        return await main.evaluator.state_of(zone("neu"))

    state = asyncio.run(scenario())
    assert state["state"] == "clear"
    assert counted["n"] == 2


def test_one_zones_refresh_does_not_erase_the_others(counted):
    """The snapshot store is updated, not replaced."""
    async def scenario():
        await main.evaluator.refresh([zone("a"), zone("b")])
        main.evaluator._last_run = 0.0        # force the next round
        await main.evaluator.refresh([zone("a")])
        return main.evaluator.snapshot("b")

    assert asyncio.run(scenario()) is not None


def test_the_floor_expires(counted):
    async def scenario():
        zones = [zone()]
        await main.evaluator.refresh(zones)
        main.evaluator._last_run -= main.MIN_EVALUATION_INTERVAL
        await main.evaluator.refresh(zones)

    asyncio.run(scenario())
    assert counted["n"] == 2


def test_deleting_a_zone_drops_its_snapshot_and_its_runtime(counted):
    async def scenario():
        target = zone()
        await main.evaluator.refresh([target])
        main._zone_runtimes[target.id] = object()
        main.forget_zone_runtime(target.id)
        return main.evaluator.snapshot(target.id), target.id in main._zone_runtimes

    snapshot, has_runtime = asyncio.run(scenario())
    assert snapshot is None
    assert has_runtime is False


def test_concurrent_callers_share_one_evaluation(counted):
    """The publisher and a dashboard poll arriving together must not each
    advance the state machine."""
    async def scenario():
        zones = [zone()]
        await asyncio.gather(*(main.evaluator.refresh(zones) for _ in range(5)))

    asyncio.run(scenario())
    assert counted["n"] == 1
