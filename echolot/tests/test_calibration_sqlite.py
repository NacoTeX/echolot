"""What moving the recordings into SQLite had to get right.

The old store kept everything in one JSON file, so every save serialised
every session: 171 ms with six sessions and 120 000 samples, and
`json.dumps` does not release the GIL, so that is 171 ms in which the
interpreter runs nothing else. 0.13.5 got it off the hot path; nothing
made it cheap.

Three things this had to not break, and one it very nearly did:

  * A history somebody already has must arrive intact, and the file that
    held it must survive the move.
  * A save must now cost what was added, not what was ever recorded.
  * `ON DELETE CASCADE` plus `INSERT OR REPLACE` is a trap. REPLACE
    deletes the conflicting row before inserting the new one, so writing
    a session's metadata fired the cascade and emptied its own data.
    Found by running it, not by reading it.
"""

import json
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
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(tmp_path))
    return tmp_path


def feed(store, device_id, count, *, label="empty", start=0.0):
    for index in range(count):
        store.ingest(device_id, Sample(t=start + index * 0.25, movement_score=0.1 * (index % 5),
                                       threshold=0.5, motion=bool(index % 2)))


# --- the trap -----------------------------------------------------------


def test_writing_a_sessions_metadata_does_not_delete_its_readings(home):
    """The bug, in its own test. `INSERT OR REPLACE` on the parent row
    fires `ON DELETE CASCADE` on the children — so stopping a recording
    wrote the session and emptied it in one statement, and the CSV export
    came back as a header line."""
    store = calibration.CalibrationStore()
    session = store.create("probe")
    feed(store, "probe", 20)
    store.flush()

    # Every metadata write there is, each one an upsert on an existing row.
    store.set_label(session["id"], "moving")
    store.stop(session["id"])

    assert len(store.samples(session["id"])) == 20
    assert len(store.csv(session["id"]).strip().splitlines()) == 21
    store.close()


def test_deleting_a_session_does_take_its_readings(home):
    """The other direction, which is what the cascade is for."""
    store = calibration.CalibrationStore()
    first = store.create("probe")
    feed(store, "probe", 30)
    store.stop(first["id"])
    second = store.create("probe")
    feed(store, "probe", 10)
    store.stop(second["id"])

    assert store.delete(first["id"]) is True

    with store._db_lock:  # noqa: SLF001 - the point is what is left on disk
        left = store._conn().execute(
            "SELECT count(*) FROM samples WHERE session_id = ?", (first["id"],)
        ).fetchone()[0]
    assert left == 0
    assert len(store.samples(second["id"])) == 10
    store.close()


# --- the cost -----------------------------------------------------------


def test_a_save_costs_what_was_added_not_what_was_recorded(home):
    """The whole reason for the move, asserted as a ratio rather than a
    wall-clock number — the absolute value is a fact about the machine,
    the independence from history is a fact about the design.

    Under the old store this ratio was the thing that grew: writing 100
    new readings serialised every session that had ever been recorded.
    """
    store = calibration.CalibrationStore()
    store.COALESCE_SECONDS = 3600      # keep the writer out of the timing

    def one_save(device):
        feed(store, device, 100)
        started = time.perf_counter()
        store.flush()
        return time.perf_counter() - started

    session = store.create("empty-history")
    small = min(one_save("empty-history") for _ in range(5))
    store.stop(session["id"])

    for index in range(4):
        old = store.create(f"old{index}")
        feed(store, f"old{index}", 10_000)
        store.flush()
        store.stop(old["id"])
    assert store.total_samples() > 40_000

    session = store.create("full-history")
    large = min(one_save("full-history") for _ in range(5))
    store.stop(session["id"])

    assert large < small * 5 + 0.01, (
        f"Speichern kostet mit {store.total_samples()} Messwerten im Bestand "
        f"{large * 1000:.1f} ms gegen {small * 1000:.1f} ms ohne Bestand"
    )
    store.close()


def test_reading_back_a_session_does_not_need_the_others(home):
    store = calibration.CalibrationStore()
    for index in range(3):
        session = store.create(f"dev{index}")
        feed(store, f"dev{index}", 200 * (index + 1))
        store.stop(session["id"])

    for index, session in enumerate(sorted(store.list(), key=lambda s: s["started_at"])):
        assert len(store.samples(session["id"])) == 200 * (index + 1)
    store.close()


# --- the migration ------------------------------------------------------


LEGACY = {
    "alt1": {
        "id": "alt1",
        "device_id": "probe",
        "name": "Vom letzten Jahr",
        "status": "complete",
        "label": "empty",
        "started_at": 1000.0,
        "ended_at": 1100.0,
        "segments": [{"label": "empty", "started_at": 1000.0, "ended_at": 1100.0}],
        "samples": [
            {"t": 1000.0 + i, "movement_score": 0.1, "threshold": 0.5,
             "motion": False, "source": "home_assistant", "label": "empty"}
            for i in range(50)
        ],
        "events": [{"kind": "motion", "t": 1005.0, "label": "empty"}],
        "recommendation": None,
    },
    "alt2": {
        "id": "alt2",
        "device_id": "probe",
        "name": "Aus dem Verlauf",
        "status": "complete",
        "label": "moving",
        "started_at": 2000.0,
        "ended_at": 2100.0,
        "segments": [],
        "samples": [{"t": 2000.0, "movement_score": 1.0, "threshold": 0.5,
                     "motion": True, "source": None, "label": "moving"}],
        "events": [],
        "source": "history",
    },
}


def write_legacy(home, payload=None):
    path = home / "calibration_sessions.json"
    path.write_text(json.dumps(payload if payload is not None else LEGACY), encoding="utf-8")
    return path


def test_an_existing_history_arrives_intact(home):
    write_legacy(home)
    store = calibration.CalibrationStore()

    assert {s["id"] for s in store.list()} == {"alt1", "alt2"}
    first = store.get("alt1")
    assert first["name"] == "Vom letzten Jahr"
    assert first["sample_count"] == 50
    assert first["event_count"] == 1
    assert first["label_counts"]["empty"] == 50
    assert store.get("alt2")["source"] == "history"

    rows = store.samples("alt1")
    assert len(rows) == 50
    assert rows[0]["source"] == "home_assistant"
    assert rows[0]["motion"] is False
    assert store.events("alt1") == [{"kind": "motion", "t": 1005.0, "label": "empty"}]
    store.close()


def test_the_old_file_is_kept_rather_than_deleted(home):
    """It is somebody's recordings. A migration that destroys its own
    input is not one worth trusting."""
    legacy = write_legacy(home)
    store = calibration.CalibrationStore()

    assert not legacy.exists()
    backup = home / "calibration_sessions.json.migrated"
    assert backup.exists()
    assert set(json.loads(backup.read_text())) == {"alt1", "alt2"}
    store.close()


def test_the_migration_runs_once_and_leaves_later_work_alone(home):
    write_legacy(home)
    first = calibration.CalibrationStore()
    first.delete("alt1")
    first.close()

    # A second start must not resurrect what was deleted in between.
    second = calibration.CalibrationStore()
    assert {s["id"] for s in second.list()} == {"alt2"}
    second.close()


def test_a_recording_left_open_in_the_old_file_is_marked_interrupted(home):
    payload = {"alt3": {**LEGACY["alt1"], "id": "alt3", "status": "recording",
                        "ended_at": None}}
    write_legacy(home, payload)
    store = calibration.CalibrationStore()

    session = store.get("alt3")
    assert session["status"] == "interrupted"
    assert session["ended_at"] is not None
    assert len(store.samples("alt3")) == 50
    store.close()


def test_an_unreadable_old_file_does_not_stop_the_add_on(home):
    """A truncated file is a bad afternoon, not a boot loop."""
    (home / "calibration_sessions.json").write_text("{ this is not json", encoding="utf-8")
    store = calibration.CalibrationStore()

    assert store.list() == []
    assert store.create("probe")["status"] == "recording"
    store.close()


def test_a_recommendation_is_computed_for_rows_that_never_had_one(home):
    """The old store recomputed it from the sample list whenever asked.
    That list is no longer at hand every time, so the migration works it
    out once — otherwise a device would quietly lose its threshold
    suggestion in the move."""
    rows = (
        [{"t": float(i), "movement_score": 0.1, "threshold": 0.5, "motion": False,
          "label": "empty"} for i in range(60)]
        + [{"t": 100.0 + i, "movement_score": 3.0, "threshold": 0.5, "motion": True,
            "label": "moving"} for i in range(60)]
    )
    write_legacy(home, {"alt4": {**LEGACY["alt1"], "id": "alt4", "samples": rows,
                                "recommendation": None}})
    store = calibration.CalibrationStore()

    profile = store.get("alt4")["recommendation"]
    assert profile is not None and profile["enter_threshold"] > 0
    assert store.latest_profiles()["probe"] == profile
    store.close()


# --- staying usable after shutdown --------------------------------------


def test_the_store_still_works_after_it_was_closed(home):
    """`close()` runs at shutdown and the store is a module-level
    singleton. Anything touching it afterwards used to keep working; a
    connection that stayed closed would turn that into a crash at exactly
    the least useful moment."""
    store = calibration.CalibrationStore()
    session = store.create("probe")
    feed(store, "probe", 10)
    store.close()

    assert len(store.samples(session["id"])) == 10
    assert store.delete(session["id"]) is True
    store.close()
