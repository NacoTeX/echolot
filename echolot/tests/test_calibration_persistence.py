"""Saving must not block the loop that receives the readings.

From the external review (P2). Every save serialises every session and
replaces the file, so it costs O(the whole history) — measured on this
code with six sessions and 120 000 samples, one save takes about 170 ms.
It used to happen inline in `ingest`, which runs on the asyncio path that
also carries the websocket subscriptions.
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
    return instance


def feed(store, device_id, count, start=0):
    for index in range(start, start + count):
        store.ingest(device_id, Sample(t=index * 0.25, movement_score=0.1,
                                       threshold=0.5, motion=False))


def test_ingesting_does_not_write_inline(store, tmp_path):
    """The regression: the hot path used to serialise the whole history
    every hundred samples, on the event loop."""
    session = store.create("probe")
    writes = {"n": 0}
    original = store._save

    def counting():
        writes["n"] += 1
        original()

    store._save = counting
    feed(store, "probe", 1000)
    assert writes["n"] == 0, "ingest hat synchron geschrieben"

    store.flush()
    assert writes["n"] == 1
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
    original = store._save

    def signalling():
        original()
        wrote.set()

    store._save = signalling
    store.create("probe")
    feed(store, "probe", 200)

    assert wrote.wait(timeout=5.0), "der Writer hat nie geschrieben"
    assert calibration._data_path().exists()


def test_a_burst_becomes_one_write_not_one_per_hundred(store):
    store.create("probe")
    writes = {"n": 0}
    original = store._save

    def counting():
        writes["n"] += 1
        original()

    store._save = counting
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
    original = store._save

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("Platte voll")
        original()

    store._save = flaky
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
