"""Zone state has one owner, and everything else reads its answer.

From the external review (recommended step 6, then R1 in the 0.13.5
round). `compute_zone_state` mutates the zone's runtime — the motion
hysteresis memory and the hold deadline — and re-reads every member
device from Home Assistant. It was called from three places: the
dashboard's poll, the overview, and the MQTT publisher.

Making the publisher event-driven in 0.13.5 made that worse rather than
better: a reading arriving three times a second became three full rounds
of Home Assistant requests a second, on top of whatever an open
dashboard was already asking for.

0.13.5 answered that with a throttle on `refresh()`, and the throttle was
the wrong shape. A change arriving inside it published the *old* snapshot
and scheduled nothing, so a short pulse could sit unseen until the next
timer tick — and a request could still start a round of its own. The
floor now sits inside one background loop: wait for something to happen,
sleep the floor, then evaluate. Every wake produces a round, late by at
most the floor and never dropped.

This file is about that discipline. Which zones a round covers, and what
a request may do, is tests/test_evaluator_rounds.py.
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

    async def fake_compute(z, _snapshots=None):
        calls["n"] += 1
        return {"available": True, "members": [], "state": "clear", "occupied": False}

    monkeypatch.setattr(main, "compute_zone_state", fake_compute)
    monkeypatch.setattr(main, "evaluator", main.ZoneEvaluator())
    return calls


async def run_for(evaluator, zones, seconds):
    """Let the loop run for a stretch of real time, then stop it."""
    await evaluator.run(lambda: zones)
    await asyncio.sleep(seconds)
    evaluator.stop()
    await asyncio.sleep(0)


def test_a_burst_of_readings_does_not_become_a_burst_of_requests(counted, monkeypatch):
    """Twenty wake-ups inside one floor cost one extra round, not twenty."""
    monkeypatch.setattr(main, "MIN_EVALUATION_INTERVAL", 0.2)
    monkeypatch.setattr(main, "IDLE_INTERVAL", 30.0)

    async def scenario():
        zones = [zone()]
        await main.evaluator.run(lambda: zones)
        await asyncio.sleep(0.02)                 # the first round
        for _ in range(20):
            main.evaluator.wake()
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.25)
        main.evaluator.stop()

    asyncio.run(scenario())
    assert counted["n"] == 2, f"{counted['n']} Auswertungen für einen Schwall"


def test_a_change_inside_the_floor_is_delayed_and_not_dropped(counted, monkeypatch):
    """The 0.13.5 throttle answered such a change with the old snapshot
    and scheduled nothing, so a short pulse could sit there until the
    idle timer came round — up to ten seconds."""
    monkeypatch.setattr(main, "MIN_EVALUATION_INTERVAL", 0.1)
    monkeypatch.setattr(main, "IDLE_INTERVAL", 30.0)

    async def scenario():
        zones = [zone()]
        await main.evaluator.run(lambda: zones)
        await asyncio.sleep(0.02)
        assert counted["n"] == 1
        main.evaluator.wake()                     # well inside the floor
        await asyncio.sleep(0.05)
        assert counted["n"] == 1, "die Drosselung muss noch greifen"
        await asyncio.sleep(0.15)
        main.evaluator.stop()

    asyncio.run(scenario())
    assert counted["n"] == 2, "das Ereignis wurde verschluckt statt verzögert"


def test_a_wake_up_from_a_worker_thread_arrives_at_once(monkeypatch):
    """FastAPI runs a plain `def` route in a worker thread, and that is
    where creating or deleting a zone happens.

    `asyncio.Event.set()` from another thread only queues a callback; it
    does not interrupt the selector the idle loop is blocked in. So the
    round lands whenever the loop next wakes for its own reasons — in the
    add-on, the ten-second idle interval.

    The poking thread waits first, on purpose: called before the loop has
    settled into `select()`, even the broken version looks fine, and the
    test would prove nothing. Measured with the loop genuinely idle, the
    naive `set()` produces no round at all inside a second and a half.
    """
    import threading
    import time as clock

    monkeypatch.setattr(main, "MIN_EVALUATION_INTERVAL", 0.001)
    monkeypatch.setattr(main, "IDLE_INTERVAL", 30.0)
    monkeypatch.setattr(main, "evaluator", main.ZoneEvaluator())

    rounds = []

    async def stamped(z, _snapshots=None):
        rounds.append(clock.monotonic())
        return {"available": True, "members": [], "state": "clear", "occupied": False}

    monkeypatch.setattr(main, "compute_zone_state", stamped)

    async def scenario():
        await main.evaluator.run(lambda: [zone()])
        await asyncio.sleep(0.05)
        assert len(rounds) == 1
        poked = []

        def poke():
            clock.sleep(0.2)             # let the loop settle into select()
            poked.append(clock.monotonic())
            main.evaluator.wake()

        threading.Thread(target=poke).start()
        # One long sleep: the loop has no other reason to wake before it.
        await asyncio.sleep(1.0)
        main.evaluator.stop()
        return poked[0]

    poked = asyncio.run(scenario())
    assert len(rounds) == 2, "der Wakeup aus dem Thread kam gar nicht an"
    assert rounds[1] - poked < 0.5, (
        f"der Wakeup wurde erst nach {rounds[1] - poked:.2f}s bearbeitet"
    )


def test_the_loop_keeps_going_without_any_event(counted, monkeypatch):
    """Hold times expire on nobody's event."""
    monkeypatch.setattr(main, "MIN_EVALUATION_INTERVAL", 0.001)
    monkeypatch.setattr(main, "IDLE_INTERVAL", 0.02)

    asyncio.run(run_for(main.evaluator, [zone()], 0.2))
    assert counted["n"] >= 3


def test_a_zone_counting_down_a_hold_is_looked_at_more_often(counted, monkeypatch):
    """A hold time running out has no event behind it, and ten seconds
    of a light staying on after the room emptied is the visible part."""
    monkeypatch.setattr(main, "MIN_EVALUATION_INTERVAL", 0.001)
    monkeypatch.setattr(main, "IDLE_INTERVAL", 10.0)
    monkeypatch.setattr(main, "HOLDING_INTERVAL", 0.02)

    async def holding(z, _snapshots=None):
        counted["n"] += 1
        return {"available": True, "members": [], "state": "holding", "occupied": True}

    monkeypatch.setattr(main, "compute_zone_state", holding)
    asyncio.run(run_for(main.evaluator, [zone()], 0.2))
    assert counted["n"] >= 3, "eine laufende Nachlaufzeit darf nicht am Leerlauftakt hängen"


def test_the_dashboard_reads_the_snapshot(counted):
    """An open dashboard polling every two seconds costs no Home
    Assistant requests: it reads what the evaluator already worked out."""
    target = zone()
    asyncio.run(main.evaluator.cycle([target]))
    assert counted["n"] == 1

    for _ in range(5):
        assert main.evaluator.state_of(target)["state"] == "clear"
    assert counted["n"] == 1


def test_a_bad_round_does_not_end_the_loop(counted, monkeypatch):
    """One zone whose device read blew up must not stop every other zone
    from ever being evaluated again."""
    monkeypatch.setattr(main, "MIN_EVALUATION_INTERVAL", 0.001)
    monkeypatch.setattr(main, "IDLE_INTERVAL", 0.02)

    async def explodes(z, _snapshots=None):
        counted["n"] += 1
        raise RuntimeError("Home Assistant sagt nein")

    monkeypatch.setattr(main, "compute_zone_state", explodes)
    asyncio.run(run_for(main.evaluator, [zone()], 0.15))
    assert counted["n"] >= 3


def test_deleting_a_zone_drops_its_snapshot_and_its_runtime(counted):
    target = zone()
    asyncio.run(main.evaluator.cycle([target]))
    main._zone_runtimes[target.id] = object()

    main.forget_zone_runtime(target.id)
    assert main.evaluator.snapshot(target.id) is None
    assert target.id not in main._zone_runtimes


def test_rounds_do_not_overlap(counted, monkeypatch):
    """Two rounds at once would each advance the same hold deadline."""
    running = {"now": 0, "most": 0}

    async def slow(z, _snapshots=None):
        running["now"] += 1
        running["most"] = max(running["most"], running["now"])
        await asyncio.sleep(0.01)
        running["now"] -= 1
        counted["n"] += 1
        return {"available": True, "members": [], "state": "clear", "occupied": False}

    monkeypatch.setattr(main, "compute_zone_state", slow)

    async def scenario():
        zones = [zone()]
        await asyncio.gather(*(main.evaluator.cycle(zones) for _ in range(5)))

    asyncio.run(scenario())
    assert counted["n"] == 5
    assert running["most"] == 1, "zwei Runden liefen gleichzeitig"


# --- the timeline the evaluator feeds (review step 8) ------------------


def test_the_evaluator_writes_down_its_transitions(counted, monkeypatch):
    """It is the one place that sees every change, so it is the one place
    that can record why."""
    from app import timeline as timeline_module

    log = timeline_module.Timeline()
    monkeypatch.setattr(timeline_module, "timeline", log)

    states = iter([
        {"available": True, "members": [], "state": "clear", "occupied": False},
        {"available": True, "members": [], "state": "detected", "occupied": True,
         "trigger": "motion"},
    ])

    async def changing(z, _snapshots=None):
        return next(states)

    monkeypatch.setattr(main, "compute_zone_state", changing)

    async def scenario():
        target = zone()
        await main.evaluator.cycle([target])
        await main.evaluator.cycle([target])

    asyncio.run(scenario())
    events = log.events("z1")
    assert len(events) == 1
    assert events[0]["to_state"] == "detected"
    assert events[0]["trigger"] == "motion"


def test_deleting_a_zone_takes_its_timeline_too(counted, monkeypatch):
    from app import timeline as timeline_module

    log = timeline_module.Timeline()
    monkeypatch.setattr(timeline_module, "timeline", log)
    log.record("z1", "Küche", {"state": "clear", "available": True, "members": []})
    log.record("z1", "Küche", {"state": "detected", "available": True, "members": []})

    main.forget_zone_runtime("z1")
    assert log.events("z1") == []
