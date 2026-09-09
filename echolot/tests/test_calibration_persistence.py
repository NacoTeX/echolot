"""Saving must not block the loop that receives the readings.

From the external review (P2, then R5 in the 0.13.5 round). Every save
serialises every session and replaces the file, so it costs O(the whole
history) — measured on this code with six sessions and 120 000 samples,
one save takes about 170 ms. It used to happen inline in `ingest`, which
runs on the asyncio path that also carries the websocket subscriptions.

0.13.5 moved it to a thread and stopped there. The writer held the same
RLock for the whole serialisation and file operation, and `ingest` needs
that lock, so a sample arriving during a write parked the event loop for
exactly as long as before: the bottleneck had moved, not gone. The lock
is now held only long enough to copy the structure.

`_write` is the seam these tests hook: it is the actual disk operation,
and it runs with no lock held.
"""

import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import calibration  # noqa: E402
from app.telemetry import Sample  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(tmp_path))
    instance = calibration.CalibrationStore()
    instance.COALESCE_SECONDS = 0.01
    instance.RETRY_SECONDS = 0.01
    yield instance
    instance.close()


def feed(store, device_id, count, start=0):
    for index in range(start, start + count):
        store.ingest(device_id, Sample(t=index * 0.25, movement_score=0.1,
                                       threshold=0.5, motion=False))


def test_ingesting_does_not_write_inline(store, tmp_path):
    """The regression: the hot path used to serialise the whole history
    every hundred samples, on the event loop.

    Asserted by which thread wrote, not by how many writes there were:
    the coalescing writer is allowed to fire while the feed is still
    running, and counting cannot tell that from writing inline.
    """
    import threading

    session = store.create("probe")
    threads = []
    original = store._write

    def noting(revision, sessions):
        threads.append(threading.current_thread())
        original(revision, sessions)

    store._write = noting
    caller = threading.current_thread()
    feed(store, "probe", 1000)
    assert caller not in threads, "ingest hat auf dem aufrufenden Thread geschrieben"

    before = len(threads)
    store.flush()
    # Not threads[-1]: the coalescing writer may append after flush did.
    assert caller in threads[before:], "flush muss auf dem aufrufenden Thread schreiben"
    assert len(store.samples(session["id"])) == 1000


def test_the_writer_does_eventually_write(store):
    """Without anyone calling flush().

    Waits on the writer itself rather than polling for the file: the file
    path comes from the environment at write time, and a store left over
    from another test can be writing to the same place, which made a
    file-existence check pass or fail depending on test order.
    """
    import threading

    wrote = threading.Event()
    original = store._write

    def signalling(revision, sessions):
        original(revision, sessions)
        wrote.set()

    store._write = signalling
    store.create("probe")
    feed(store, "probe", 200)

    assert wrote.wait(timeout=5.0), "der Writer hat nie geschrieben"
    assert calibration._data_path().exists()


def test_a_burst_becomes_one_write_not_one_per_hundred(store):
    store.create("probe")
    writes = {"n": 0}
    original = store._write

    def counting(revision, sessions):
        writes["n"] += 1
        original(revision, sessions)

    store._write = counting
    feed(store, "probe", 2000)          # twenty triggers under the old rule
    for _ in range(200):
        time.sleep(0.01)
        if writes["n"]:
            break
    assert 1 <= writes["n"] <= 3, f"{writes['n']} Schreibvorgänge für einen Schwall"


def test_stopping_persists_before_it_returns(store):
    """A finished recording is on disk before the caller is told it
    finished, whatever the writer thread happens to be doing."""
    session = store.create("probe")
    feed(store, "probe", 300)
    store.stop(session["id"])

    reloaded = calibration.CalibrationStore()
    stored = reloaded.samples(session["id"])
    assert stored is not None and len(stored) == 300


def test_a_failing_write_does_not_kill_the_writer(store):
    """One bad save must not silently end persistence for the session."""
    store.create("probe")
    calls = {"n": 0}
    original = store._write

    def flaky(revision, sessions):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("Platte voll")
        original(revision, sessions)

    store._write = flaky
    feed(store, "probe", 100)
    for _ in range(200):
        time.sleep(0.01)
        if calls["n"] >= 1:
            break
    feed(store, "probe", 100, start=100)
    for _ in range(200):
        time.sleep(0.01)
        if calls["n"] >= 2:
            break
    assert calls["n"] >= 2, "der Writer ist am ersten Fehler gestorben"


def test_a_failed_write_is_retried_without_a_new_sample(store):
    """The dirty flag used to be cleared before the save, so a write that
    failed left nothing to retry and the data waited for the next sample
    — which, at the end of a recording, never comes."""
    import threading

    session = store.create("probe")
    calls = {"n": 0}
    retried = threading.Event()
    original = store._write

    def flaky(revision, sessions):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("Platte voll")
        original(revision, sessions)
        # Set after the write, not on entry: otherwise the assertion
        # below can run while the file is still being replaced.
        retried.set()

    store._write = flaky
    feed(store, "probe", 100)          # the only samples this test feeds

    assert retried.wait(5.0), "der fehlgeschlagene Schreibvorgang wurde nie wiederholt"

    reloaded = calibration.CalibrationStore()
    assert len(reloaded.samples(session["id"]) or []) == 100


def test_hitting_the_sample_limit_is_visible(store, monkeypatch):
    """Readings were dropped in silence: the session kept its live
    indicator on, the counter stopped moving, and nothing said the
    recording had stopped recording."""
    monkeypatch.setattr(calibration, "MAX_SAMPLES_PER_SESSION", 10)
    session = store.create("probe")
    feed(store, "probe", 25)

    public = store.get(session["id"])
    assert public["sample_count"] == 10
    assert public["sample_limit_reached"] is True


def test_a_session_under_the_limit_says_nothing(store):
    session = store.create("probe")
    feed(store, "probe", 20)
    assert store.get(session["id"]).get("sample_limit_reached") is None


# --- R5: the lock the writer holds -------------------------------------


def test_a_slow_write_does_not_park_the_event_loop(store):
    """The acceptance criterion for R5, measured rather than asserted by
    construction: with a deliberately slow write in flight, an
    independent heartbeat on the event loop keeps ticking and samples are
    still taken in.

    0.13.5 held the ingest lock for the whole serialisation and file
    operation, so `ingest` — called from the asyncio path that carries
    the websocket subscriptions — waited the entire write out.
    """
    import asyncio
    import threading

    writing = threading.Event()
    release = threading.Event()
    original = store._write

    def slow(revision, sessions):
        writing.set()
        release.wait(2.0)
        original(revision, sessions)

    store._write = slow
    store.create("probe")
    feed(store, "probe", 100)                 # triggers the writer
    assert writing.wait(2.0), "der Writer hat nie angefangen"

    async def scenario():
        beats = []

        async def heartbeat():
            while True:
                beats.append(time.monotonic())
                await asyncio.sleep(0.01)

        pulse = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.02)
        started = time.monotonic()
        # This is the call that used to wait for the whole write.
        store.ingest("probe", Sample(t=999.0, movement_score=0.2,
                                     threshold=0.5, motion=True))
        blocked = time.monotonic() - started
        await asyncio.sleep(0.1)
        pulse.cancel()
        return blocked, len(beats)

    blocked, beats = asyncio.run(scenario())
    release.set()

    assert blocked < 0.05, f"ingest blockierte {blocked * 1000:.0f} ms"
    assert beats >= 5, f"nur {beats} Herzschläge während des Schreibens"
    assert store.samples(store.list()[0]["id"])[-1]["t"] == 999.0


def test_a_stale_write_cannot_bring_a_deleted_session_back(store):
    """A write already in flight carries an older revision, and a delete
    that has been reported to the caller must stay deleted."""
    import threading

    session = store.create("probe")
    feed(store, "probe", 100)

    holding = threading.Event()
    release = threading.Event()
    original = store._write

    def slow(revision, sessions):
        if not holding.is_set():
            holding.set()
            release.wait(2.0)
        original(revision, sessions)

    store._write = slow
    feed(store, "probe", 100, start=100)      # writer picks this up
    assert holding.wait(2.0)

    # The delete happens while that write is still parked.
    store._write = original
    assert store.delete(session["id"]) is True
    release.set()
    time.sleep(0.1)

    reloaded = calibration.CalibrationStore()
    assert reloaded.get(session["id"]) is None


def test_close_stops_the_writer_and_leaves_the_data_on_disk(store):
    session = store.create("probe")
    feed(store, "probe", 250)
    store.close()

    assert store._writer is None
    reloaded = calibration.CalibrationStore()
    assert len(reloaded.samples(session["id"]) or []) == 250

    # And it is idempotent, because shutdown paths run twice often enough.
    store.close()


# --- R5: a bound on the whole history ----------------------------------


def test_a_full_history_refuses_a_new_recording_rather_than_pruning(store):
    """Deliberately not a retention policy that deletes: a recording is
    somebody's afternoon. The bound stops growth and says what to do."""
    store.MAX_TOTAL_SAMPLES = 150
    first = store.create("probe")
    feed(store, "probe", 200)
    store.stop(first["id"])

    with pytest.raises(ValueError) as err:
        store.create("probe")
    assert "Lösche" in str(err.value)

    # Nothing was thrown away to make room.
    assert len(store.samples(first["id"])) == 200

    store.delete(first["id"])
    assert store.create("probe")["status"] == "recording"


def test_too_many_sessions_is_also_a_refusal(store):
    store.MAX_SESSIONS = 2
    for _ in range(2):
        session = store.create("probe")
        store.stop(session["id"])

    with pytest.raises(ValueError) as err:
        store.create("probe")
    assert "2 Aufzeichnungen" in str(err.value)


def test_an_import_is_bounded_by_the_same_history(store):
    store.MAX_TOTAL_SAMPLES = 150
    first = store.create("probe")
    feed(store, "probe", 200)
    store.stop(first["id"])

    with pytest.raises(ValueError):
        store.adopt(
            "probe",
            [Sample(t=1.0, movement_score=0.1, threshold=0.5, motion=False)],
            label="empty",
        )
