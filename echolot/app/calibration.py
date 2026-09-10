"""Ground-truth recording and transparent threshold recommendations.

Calibration sessions pair direct ESPectre samples with labels supplied by
the person commissioning the room. Recommendations explain their inputs
instead of hiding them in an opaque model, and every recording exports as
CSV — the data stays inspectable whatever it is stored in.

Stored in SQLite since 0.13.9. It was one JSON file, which meant every
save rewrote every session; see CalibrationStore for what that cost and
what replaced it. The old file is migrated once and kept as a backup.
"""

import csv
import io
import json
import logging
import os
import sqlite3
import statistics
import threading
import time
import uuid
from pathlib import Path

from app.telemetry import Sample

logger = logging.getLogger("echolot.calibration")

LABELS = {"unlabelled", "empty", "moving", "still", "interference"}
MAX_SAMPLES_PER_SESSION = 100_000
PERSIST_EVERY_SAMPLES = 100

#: Non-numerical events kept alongside the readings — motion flips,
#: dropouts, source changes. Bounded separately and far lower: they are
#: sparse, and they must never grow with the sample rate.
#:
#: They are stored apart from `samples` so nothing that counts readings
#: can accidentally count them. The crossing rate is events per second of
#: observed time; a motion flip in that total would inflate the very
#: number these exist to explain.
MAX_EVENTS_PER_SESSION = 5_000


def _data_path() -> Path:
    root = Path(os.environ.get("ECHOLOT_DATA_DIR", "/data"))
    return root / "calibration.sqlite3"


def _legacy_path() -> Path:
    """Where recordings lived until 0.13.9. Migrated once, then renamed."""
    root = Path(os.environ.get("ECHOLOT_DATA_DIR", "/data"))
    return root / "calibration_sessions.json"


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile of empty data")
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def recommendation(samples: list[dict]) -> dict | None:
    """Derive robust, explainable enter/exit thresholds from labelled data."""
    empty = [row["movement_score"] for row in samples if row["label"] == "empty" and row["movement_score"] is not None]
    occupied = [
        row["movement_score"]
        for row in samples
        if row["label"] in {"moving", "still"} and row["movement_score"] is not None
    ]
    if len(empty) < 20 or len(occupied) < 20:
        return None

    baseline = statistics.median(empty)
    mad = statistics.median(abs(value - baseline) for value in empty)
    noise = max(mad * 1.4826, 0.01)
    empty_p99 = _percentile(empty, 0.99)
    occupied_p10 = _percentile(occupied, 0.10)
    noise_floor = max(empty_p99, baseline + 5 * noise)
    # If the classes separate, use the middle of the safe gap. If they overlap,
    # stay above empty-room noise and report the resulting false-negative rate
    # instead of buying sensitivity with an unexplained flood of false alarms.
    enter = (noise_floor + occupied_p10) / 2 if occupied_p10 > noise_floor else noise_floor
    exit_at = min(enter, max(baseline + 2.5 * noise, _percentile(empty, 0.95)))
    enter = round(max(0.0, min(10.0, enter)), 2)
    exit_at = round(max(0.0, min(enter, exit_at)), 2)

    false_positive = sum(value >= enter for value in empty) / len(empty)
    false_negative = sum(value < enter for value in occupied) / len(occupied)
    separation = (statistics.median(occupied) - baseline) / noise
    quality = "good" if separation >= 6 else "fair" if separation >= 3 else "poor"
    return {
        "enter_threshold": enter,
        "exit_threshold": exit_at,
        "baseline": round(baseline, 3),
        "noise": round(noise, 3),
        "separation": round(separation, 2),
        "quality": quality,
        "empty_samples": len(empty),
        "occupied_samples": len(occupied),
        "estimated_false_positive_rate": round(false_positive, 4),
        "estimated_false_negative_rate": round(false_negative, 4),
    }


class CalibrationStore:
    """Sessions and their readings, kept in SQLite.

    **Why it moved.** Until 0.13.9 everything lived in one JSON file, and
    every save serialised every session and replaced it — so the cost of
    writing grew with everything ever recorded. Measured on this code
    with six sessions and 120 000 samples: 171 ms per save, on an x86
    development machine, and `json.dumps` does not release the GIL, so
    that is 171 ms in which the interpreter runs nothing else.

    0.13.5 got that off the hot path — the writer took a snapshot under
    the lock and serialised outside it, so nothing waited on the write.
    That was the right fix for "the event loop is parked" and no fix at
    all for "a write costs the whole history". It also forced a bound on
    the history, and a 90-minute ceiling on importing from the recorder,
    both for the same reason.

    Now a reading is an INSERT: O(one row), and `sqlite3` releases the
    GIL while it executes. Writing costs what was added, not what was
    ever recorded.

    **What is still in memory.** Session metadata — a hundred rows at
    most — and the readings of the *recording* session, of which there is
    at most one by construction (`create` refuses a second). Those are
    the rows something asks about every few seconds while the recording
    runs; reading them back out of the database each time would trade one
    cost for another. Everything else is loaded on demand.

    **What is still coalesced.** Rows are appended to the database in
    batches rather than one statement per reading, so a hundred readings
    a second do not become a hundred transactions a second on an SD card.
    A crash therefore still costs the last COALESCE_SECONDS of a
    recording, as before — and everything a person does (create, label,
    stop, delete, import) is on disk before the request returns.
    """

    #: How long the writer waits before saving, so a burst of readings
    #: becomes one transaction rather than one per reading.
    COALESCE_SECONDS = 2.0

    #: How long to wait after a failed write before trying again.
    RETRY_SECONDS = 5.0

    #: A bound on the whole history, not just on one session.
    #:
    #: The old bound existed because writing cost O(the whole history);
    #: that reason is gone. What remains is the disk, which on a Home
    #: Assistant box is often an SD card — so there is still a ceiling,
    #: just a much higher one. Reaching it refuses new material rather
    #: than dropping old material: a recording is somebody's afternoon,
    #: and silently deleting one to make room for another is not a trade
    #: this add-on gets to make.
    MAX_TOTAL_SAMPLES = 5_000_000
    MAX_SESSIONS = 500

    #: Columns of a stored reading, in the order they are bound.
    _SAMPLE_COLUMNS = ("t", "movement_score", "threshold", "motion", "source", "label")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        #: Session metadata only. The readings live in the database, bar
        #: the recording session's, which are in `_active_samples`.
        self._sessions: dict[str, dict] = {}
        self._active_id: str | None = None
        self._active_samples: list[dict] = []
        self._active_events: list[dict] = []
        #: How many of those are already in the database. The writer
        #: inserts the tail; nothing is copied to hand it over.
        self._flushed_samples = 0
        self._flushed_events = 0
        self._total_samples = 0
        self._dirty = threading.Event()
        self._stopping = threading.Event()
        self._writer_lock = threading.Lock()
        #: Guards the connection. Held only for the statement itself —
        #: sqlite3 releases the GIL while it runs, so other threads keep
        #: going.
        self._db_lock = threading.Lock()
        self._writer = None
        self._db: "sqlite3.Connection | None" = None
        self._load()

    # --- the database -----------------------------------------------------

    def _conn(self) -> "sqlite3.Connection":
        """The connection, opened if it is not. Call under `_db_lock`.

        Re-openable on purpose. `close()` is called from the add-on's
        shutdown, and the store is a module-level singleton — anything
        that touches it afterwards used to keep working, and a closed
        connection that stays closed would turn that into a crash at
        exactly the least useful moment.
        """
        if self._db is None:
            self._db = self._connect()
        return self._db

    def _connect(self) -> "sqlite3.Connection":
        path = _data_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # One connection shared across threads, guarded by `_db_lock`.
        # Per-thread connections would make a transaction that spans a
        # request impossible to reason about, and the concurrency here is
        # a handful of operations a second.
        db = sqlite3.connect(path, check_same_thread=False)
        db.row_factory = sqlite3.Row
        # WAL so a reader never blocks on the writer; NORMAL so a commit
        # does not fsync. What that costs is the last transactions on a
        # power cut, which is the same thing the coalescing already costs.
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id            TEXT PRIMARY KEY,
                device_id     TEXT NOT NULL,
                name          TEXT NOT NULL,
                status        TEXT NOT NULL,
                label         TEXT NOT NULL,
                started_at    REAL NOT NULL,
                ended_at      REAL,
                source        TEXT,
                segments      TEXT NOT NULL,
                recommendation TEXT,
                sample_count  INTEGER NOT NULL DEFAULT 0,
                event_count   INTEGER NOT NULL DEFAULT 0,
                label_counts  TEXT NOT NULL DEFAULT '{}',
                sample_limit_reached INTEGER NOT NULL DEFAULT 0,
                event_limit_reached  INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS samples (
                session_id     TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                seq            INTEGER NOT NULL,
                t              REAL NOT NULL,
                movement_score REAL,
                threshold      REAL,
                motion         INTEGER,
                source         TEXT,
                label          TEXT NOT NULL,
                PRIMARY KEY (session_id, seq)
            );
            CREATE TABLE IF NOT EXISTS events (
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                seq        INTEGER NOT NULL,
                payload    TEXT NOT NULL,
                PRIMARY KEY (session_id, seq)
            );
            """
        )
        db.commit()
        return db

    def _load(self) -> None:
        with self._db_lock:
            self._conn()
        self._migrate_legacy_json()

        with self._db_lock:
            rows = self._conn().execute("SELECT * FROM sessions").fetchall()
            total = self._conn().execute("SELECT COALESCE(SUM(sample_count), 0) FROM sessions").fetchone()[0]
        self._sessions = {row["id"]: self._session_from_row(row) for row in rows}
        self._total_samples = int(total or 0)

        # A process restart ends a recording; never silently keep
        # assigning a stale ground-truth label to new samples.
        interrupted = [s for s in self._sessions.values() if s["status"] == "recording"]
        for session in interrupted:
            session["status"] = "interrupted"
            session["ended_at"] = time.time()
        if interrupted:
            with self._db_lock:
                for session in interrupted:
                    self._conn().execute(
                        "UPDATE sessions SET status = ?, ended_at = ? WHERE id = ?",
                        (session["status"], session["ended_at"], session["id"]),
                    )
                self._conn().commit()

    @staticmethod
    def _session_from_row(row) -> dict:
        return {
            "id": row["id"],
            "device_id": row["device_id"],
            "name": row["name"],
            "status": row["status"],
            "label": row["label"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            **({"source": row["source"]} if row["source"] else {}),
            "segments": json.loads(row["segments"]),
            "recommendation": json.loads(row["recommendation"]) if row["recommendation"] else None,
            "sample_count": row["sample_count"],
            "event_count": row["event_count"],
            "label_counts": json.loads(row["label_counts"]),
            **({"sample_limit_reached": True} if row["sample_limit_reached"] else {}),
            **({"event_limit_reached": True} if row["event_limit_reached"] else {}),
        }

    def _insert_session(self, session: dict) -> None:
        """Write one session's metadata. `_db_lock` NOT held."""
        with self._db_lock:
            # An upsert, and emphatically not INSERT OR REPLACE: REPLACE
            # deletes the conflicting row before inserting the new one,
            # which fires ON DELETE CASCADE and takes every reading of the
            # session with it. Found by running it — stopping a recording
            # wrote the session's metadata and emptied its own data in the
            # same statement.
            self._conn().execute(
                """INSERT INTO sessions
                   (id, device_id, name, status, label, started_at, ended_at, source,
                    segments, recommendation, sample_count, event_count, label_counts,
                    sample_limit_reached, event_limit_reached)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     device_id = excluded.device_id,
                     name = excluded.name,
                     status = excluded.status,
                     label = excluded.label,
                     started_at = excluded.started_at,
                     ended_at = excluded.ended_at,
                     source = excluded.source,
                     segments = excluded.segments,
                     recommendation = excluded.recommendation,
                     sample_count = excluded.sample_count,
                     event_count = excluded.event_count,
                     label_counts = excluded.label_counts,
                     sample_limit_reached = excluded.sample_limit_reached,
                     event_limit_reached = excluded.event_limit_reached""",
                (
                    session["id"], session["device_id"], session["name"],
                    session["status"], session["label"], session["started_at"],
                    session["ended_at"], session.get("source"),
                    json.dumps(session["segments"]),
                    json.dumps(session["recommendation"]) if session.get("recommendation") else None,
                    session.get("sample_count", 0), session.get("event_count", 0),
                    json.dumps(session.get("label_counts", {})),
                    1 if session.get("sample_limit_reached") else 0,
                    1 if session.get("event_limit_reached") else 0,
                ),
            )
            self._conn().commit()

    # --- migration --------------------------------------------------------

    def _migrate_legacy_json(self) -> None:
        """Bring a 0.13.8 JSON history into the database, once.

        The JSON is renamed rather than deleted. It is somebody's
        recordings; if anything about this goes wrong, the file that had
        them is still there, and a migration that destroys its own input
        is not one worth trusting.
        """
        legacy = _legacy_path()
        if not legacy.exists():
            return
        with self._db_lock:
            if self._conn().execute("SELECT 1 FROM sessions LIMIT 1").fetchone():
                return  # Already migrated, or the database was here first.
        try:
            raw = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("Alte Kalibrierungsdatei ließ sich nicht lesen")
            return
        if not isinstance(raw, dict):
            return

        migrated = 0
        for session_id, old in raw.items():
            if not isinstance(old, dict):
                continue
            rows = old.get("samples") or []
            events = old.get("events") or []
            counts = {label: 0 for label in LABELS}
            for row in rows:
                counts[row.get("label", "unlabelled")] = counts.get(row.get("label", "unlabelled"), 0) + 1
            session = {
                "id": session_id,
                "device_id": old.get("device_id", ""),
                "name": old.get("name", ""),
                "status": old.get("status", "complete"),
                "label": old.get("label", "unlabelled"),
                "started_at": old.get("started_at") or 0.0,
                "ended_at": old.get("ended_at"),
                "segments": old.get("segments") or [],
                # Computed here rather than left absent: the old store
                # recomputed it on demand from the sample list, and that
                # list is no longer at hand every time somebody asks.
                "recommendation": old.get("recommendation") or recommendation(rows),
                "sample_count": len(rows),
                "event_count": len(events),
                "label_counts": counts,
            }
            if old.get("source"):
                session["source"] = old["source"]
            if old.get("sample_limit_reached"):
                session["sample_limit_reached"] = True
            if old.get("event_limit_reached"):
                session["event_limit_reached"] = True
            self._insert_session(session)
            self._append_rows(session_id, rows, 0)
            self._append_events(session_id, events, 0)
            migrated += 1

        backup = legacy.with_suffix(".json.migrated")
        try:
            legacy.replace(backup)
        except OSError:
            logger.exception("Alte Kalibrierungsdatei ließ sich nicht umbenennen")
        logger.info("%d Kalibrierungen nach SQLite übernommen; alte Datei liegt als %s",
                    migrated, backup.name)

    # --- appending --------------------------------------------------------

    def _append_rows(self, session_id: str, rows: list, first_seq: int) -> None:
        """Insert readings. One transaction, `_db_lock` NOT held."""
        if not rows:
            return
        with self._db_lock:
            self._conn().executemany(
                "INSERT OR REPLACE INTO samples "
                "(session_id, seq, t, movement_score, threshold, motion, source, label) "
                "VALUES (?,?,?,?,?,?,?,?)",
                [
                    (session_id, first_seq + offset, row.get("t"), row.get("movement_score"),
                     row.get("threshold"),
                     None if row.get("motion") is None else int(bool(row.get("motion"))),
                     row.get("source"), row.get("label", "unlabelled"))
                    for offset, row in enumerate(rows)
                ],
            )
            self._conn().commit()

    def _append_events(self, session_id: str, rows: list, first_seq: int) -> None:
        if not rows:
            return
        with self._db_lock:
            self._conn().executemany(
                "INSERT OR REPLACE INTO events (session_id, seq, payload) VALUES (?,?,?)",
                [(session_id, first_seq + offset, json.dumps(row))
                 for offset, row in enumerate(rows)],
            )
            self._conn().commit()

    # --- the writer -------------------------------------------------------

    def _schedule_save(self) -> None:
        self._dirty.set()
        with self._writer_lock:
            if self._stopping.is_set():
                return
            if self._writer is None or not self._writer.is_alive():
                self._writer = threading.Thread(
                    target=self._writer_loop, name="echolot-calibration-writer", daemon=True
                )
                self._writer.start()

    def _writer_loop(self) -> None:
        while not self._stopping.is_set():
            self._dirty.wait()
            if self._stopping.is_set():
                return
            # Coalesce a burst of readings into one transaction — but
            # wake at once when the store is closing, so a shutdown does
            # not sit out the interval before writing what it has.
            self._stopping.wait(self.COALESCE_SECONDS)
            if self._stopping.is_set():
                self._dirty.clear()
                self._write(*self._pending())
                return
            # Cleared *before* the batch is taken, so a reading arriving
            # during the write marks the store dirty again and goes out
            # next round rather than waiting for the reading after it.
            self._dirty.clear()
            try:
                self._write(*self._pending())
            except Exception:  # noqa: BLE001 - a failed write must not kill the writer
                logger.exception("Kalibrierung konnte nicht gespeichert werden")
                self._dirty.set()
                self._stopping.wait(self.RETRY_SECONDS)

    def _pending(self) -> tuple:
        """What is not in the database yet. Lock held briefly.

        The rows are shared rather than copied: a reading is written once
        when it arrives and never touched again, so copying them would
        cost more than the insert it is protecting.
        """
        with self._lock:
            if self._active_id is None:
                return None, ()
            return self._active_id, (
                self._active_samples[self._flushed_samples:],
                self._flushed_samples,
                self._active_events[self._flushed_events:],
                self._flushed_events,
            )

    def _write(self, session_id, batch) -> None:
        """Append one batch to the database. No `_lock` held."""
        if session_id is None or not batch:
            return
        samples, first_sample, events, first_event = batch
        self._append_rows(session_id, samples, first_sample)
        self._append_events(session_id, events, first_event)
        with self._lock:
            # Only if the session is still the active one: a stop or a
            # delete may have landed while this was in flight, and moving
            # the mark then would be a claim about a session that no
            # longer exists.
            if self._active_id == session_id:
                self._flushed_samples = max(self._flushed_samples, first_sample + len(samples))
                self._flushed_events = max(self._flushed_events, first_event + len(events))

    def flush(self) -> None:
        """Write now and wait for it — for shutdown and for stop()."""
        self._dirty.clear()
        self._write(*self._pending())

    def close(self) -> None:
        """Stop the writer and wait for it. For shutdown."""
        self._stopping.set()
        self._dirty.set()          # wake it so it can see the flag
        with self._writer_lock:
            writer = self._writer
            self._writer = None
        if writer is not None and writer.is_alive():
            writer.join(timeout=self.COALESCE_SECONDS + 2.0)
            if writer.is_alive():
                logger.warning("Kalibrierungs-Writer hat sich nicht beendet")
        self.flush()
        self._stopping.clear()
        self._dirty.clear()
        with self._db_lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    # --- retention --------------------------------------------------------

    def total_samples(self) -> int:
        with self._lock:
            return self._total_samples

    def _check_room(self) -> None:
        """Refuse a new session when the history is already at its bound.

        Raises ValueError, which every caller already turns into a 409
        with the message shown to the person.
        """
        with self._lock:
            if len(self._sessions) >= self.MAX_SESSIONS:
                raise ValueError(
                    f"Es sind bereits {self.MAX_SESSIONS} Aufzeichnungen gespeichert. "
                    "Lösche eine alte, bevor du eine neue anlegst — Echolot wirft "
                    "keine Aufzeichnung von selbst weg."
                )
            total = self._total_samples
            if total >= self.MAX_TOTAL_SAMPLES:
                raise ValueError(
                    f"Die gespeicherten Aufzeichnungen umfassen bereits {total} Messwerte "
                    f"(Grenze {self.MAX_TOTAL_SAMPLES}). Lösche oder exportiere eine alte, "
                    "bevor du eine neue anlegst."
                )

    # --- sessions ---------------------------------------------------------

    def create(self, device_id: str, name: str = "") -> dict:
        self._check_room()
        with self._lock:
            if any(s["status"] == "recording" for s in self._sessions.values()):
                raise ValueError("Es läuft bereits eine Kalibrierungsaufzeichnung")
            now = time.time()
            session_id = uuid.uuid4().hex
            session = {
                "id": session_id,
                "device_id": device_id,
                "name": name.strip() or time.strftime("Kalibrierung %Y-%m-%d %H:%M"),
                "status": "recording",
                "label": "unlabelled",
                "started_at": now,
                "ended_at": None,
                "segments": [{"label": "unlabelled", "started_at": now, "ended_at": None}],
                "recommendation": None,
                "sample_count": 0,
                "event_count": 0,
                "label_counts": {label: 0 for label in LABELS},
            }
            self._sessions[session_id] = session
            self._active_id = session_id
            self._active_samples = []
            self._active_events = []
            self._flushed_samples = 0
            self._flushed_events = 0
            result = self.public(session)
        self._insert_session(session)
        return result

    def set_label(self, session_id: str, label: str) -> dict:
        if label not in LABELS - {"unlabelled"}:
            raise ValueError(f"Unbekanntes Label '{label}'")
        with self._lock:
            session = self._require(session_id)
            if session["status"] != "recording":
                raise ValueError("Die Aufzeichnung ist bereits beendet")
            now = time.time()
            session["segments"][-1]["ended_at"] = now
            session["segments"].append({"label": label, "started_at": now, "ended_at": None})
            session["label"] = label
            result = self.public(session)
        self._insert_session(session)
        return result

    def stop(self, session_id: str) -> dict:
        # Before the metadata write, so the recommendation is computed
        # over everything and the rows are on disk when the caller is
        # told the recording finished.
        self.flush()
        with self._lock:
            session = self._require(session_id)
            changed = session["status"] == "recording"
            if changed:
                now = time.time()
                session["status"] = "complete"
                session["ended_at"] = now
                session["segments"][-1]["ended_at"] = now
                session["recommendation"] = recommendation(self._active_samples)
                if self._active_id == session_id:
                    # The readings are on disk; keeping a second copy in
                    # memory for a session nobody is recording any more
                    # is what the old store did with all of them.
                    self._active_id = None
                    self._active_samples = []
                    self._active_events = []
                    self._flushed_samples = 0
                    self._flushed_events = 0
            result = self.public(session)
        if changed:
            self._insert_session(session)
        return result

    def adopt(self, device_id: str, samples: list, *, label: str, name: str = "") -> dict:
        """Store a finished session built from history rather than recorded.

        Not `create` + `ingest` + `stop`: those model a recording, and a
        recording refuses to start while another one runs. Importing the
        past has no reason to wait for the present, and the samples already
        carry their own timestamps, so the session is written complete.
        """
        if label not in LABELS:
            raise ValueError(f"Unbekanntes Label '{label}'")
        if not samples:
            raise ValueError("Der Zeitraum enthält keine Messwerte")
        self._check_room()

        rows = [{**sample.as_dict(), "label": label} for sample in samples[:MAX_SAMPLES_PER_SESSION]]
        started_at = min(row["t"] for row in rows)
        ended_at = max(row["t"] for row in rows)
        counts = {name_: 0 for name_ in LABELS}
        counts[label] = len(rows)
        session_id = uuid.uuid4().hex
        session = {
            "id": session_id,
            "device_id": device_id,
            "name": name.strip() or time.strftime(
                "Verlauf %Y-%m-%d %H:%M", time.localtime(started_at)
            ),
            "status": "complete",
            "label": label,
            "started_at": started_at,
            "ended_at": ended_at,
            "segments": [{"label": label, "started_at": started_at, "ended_at": ended_at}],
            "recommendation": recommendation(rows),
            "sample_count": len(rows),
            # An import has no event stream: Home Assistant's recorder
            # gives back the score series, not what happened between two
            # of its points. Zero rather than absent, so a reader can tell
            # "nothing happened" from "not recorded".
            "event_count": 0,
            "label_counts": counts,
            #: So the UI can say where this came from, and so a later
            #: reader does not mistake it for something somebody sat
            #: through.
            "source": "history",
        }
        self._insert_session(session)
        self._append_rows(session_id, rows, 0)
        with self._lock:
            self._sessions[session_id] = session
            self._total_samples += len(rows)
            return self.public(session)

    def delete(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return False
            self._total_samples = max(0, self._total_samples - session.get("sample_count", 0))
            if self._active_id == session_id:
                # Drop the pending rows with it. Inserting them after the
                # DELETE would resurrect the session — the failure the
                # old store's revision counter existed to prevent.
                self._active_id = None
                self._active_samples = []
                self._active_events = []
                self._flushed_samples = 0
                self._flushed_events = 0
        with self._db_lock:
            # The rows go with it: `ON DELETE CASCADE` plus explicit
            # deletes, because a database created before foreign keys
            # were enforced would otherwise leave them behind.
            self._conn().execute("DELETE FROM samples WHERE session_id = ?", (session_id,))
            self._conn().execute("DELETE FROM events WHERE session_id = ?", (session_id,))
            self._conn().execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self._conn().commit()
        return True

    # --- recording --------------------------------------------------------

    def ingest(self, device_id: str, sample) -> None:
        save = False
        with self._lock:
            active = self._active(device_id)
            if active is None:
                return
            if len(self._active_samples) >= MAX_SAMPLES_PER_SESSION:
                # Readings were dropped here in silence: the session kept
                # its live indicator on, the counter stopped moving, and
                # nothing said the recording had stopped recording. Now it
                # is marked once, so the UI can say so and a later reader
                # of the data knows it was cut off rather than ended.
                if not active.get("sample_limit_reached"):
                    active["sample_limit_reached"] = True
                    logger.warning(
                        "Kalibrierung %s hat das Limit von %d Messwerten erreicht",
                        active["id"], MAX_SAMPLES_PER_SESSION,
                    )
                    save = True
            else:
                row = {**sample.as_dict(), "label": active["label"]}
                self._active_samples.append(row)
                active["sample_count"] = len(self._active_samples)
                active["label_counts"][row["label"]] = active["label_counts"].get(row["label"], 0) + 1
                self._total_samples += 1
                save = len(self._active_samples) % PERSIST_EVERY_SAMPLES == 0
        # Outside the lock, and off this thread: this runs on the asyncio
        # path that also carries the websocket subscriptions.
        if save:
            self._schedule_save()

    def ingest_event(self, device_id: str, event: dict) -> None:
        """Record one non-numerical event against the running session.

        Same shape as `ingest`, and deliberately separate from it: this
        never touches the sample list, so the recorded crossing count is
        exactly what it was before events existed.
        """
        save = False
        with self._lock:
            active = self._active(device_id)
            if active is None:
                return
            if len(self._active_events) >= MAX_EVENTS_PER_SESSION:
                if not active.get("event_limit_reached"):
                    active["event_limit_reached"] = True
                    logger.warning(
                        "Kalibrierung %s hat das Ereignislimit von %d erreicht",
                        active["id"], MAX_EVENTS_PER_SESSION,
                    )
                    save = True
            else:
                self._active_events.append({**event, "label": active["label"]})
                active["event_count"] = len(self._active_events)
                save = True
        if save:
            self._schedule_save()

    def _active(self, device_id: str):
        """The recording session for this device, or None. Lock held."""
        if self._active_id is None:
            return None
        session = self._sessions.get(self._active_id)
        if session is None or session["device_id"] != device_id or session["status"] != "recording":
            return None
        return session

    # --- reading ----------------------------------------------------------

    def events(self, session_id: str) -> "list[dict] | None":
        """The non-numerical events of one session, oldest first."""
        with self._lock:
            if session_id not in self._sessions:
                return None
            if self._active_id == session_id:
                return list(self._active_events)
        with self._db_lock:
            rows = self._conn().execute(
                "SELECT payload FROM events WHERE session_id = ? ORDER BY seq", (session_id,)
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def list(self, device_id: str | None = None) -> list:
        with self._lock:
            sessions = self._sessions.values()
            if device_id is not None:
                sessions = (session for session in sessions if session["device_id"] == device_id)
            return sorted((self.public(session) for session in sessions),
                          key=lambda s: s["started_at"], reverse=True)

    def get(self, session_id: str) -> dict | None:
        with self._lock:
            session = self._sessions.get(session_id)
            return self.public(session) if session else None

    def _require(self, session_id: str) -> dict:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise KeyError("Kalibrierung nicht gefunden") from None

    def public(self, session: dict) -> dict:
        """The session as the UI sees it — metadata and counts, no rows.

        The counts are carried rather than recomputed: they used to come
        from `len(session["samples"])`, and those rows are no longer in
        memory for anything but the recording session.
        """
        result = {
            key: value for key, value in session.items()
            if key not in ("sample_count", "event_count", "label_counts")
        }
        if session["status"] == "recording" and self._active_id == session["id"]:
            # A live recording's recommendation moves as it grows, so it
            # is derived rather than stored until the recording stops.
            result["recommendation"] = recommendation(self._active_samples)
        return result | {
            "sample_count": session.get("sample_count", 0),
            "event_count": session.get("event_count", 0),
            "label_counts": dict(session.get("label_counts") or {}),
        }

    def latest_profiles(self) -> dict:
        """Newest completed recommendation per device, calculated once."""
        with self._lock:
            profiles = {}
            ordered = sorted(
                self._sessions.values(), key=lambda session: session["started_at"], reverse=True
            )
            for session in ordered:
                if session["device_id"] in profiles or session["status"] == "recording":
                    continue
                profile = session.get("recommendation")
                if profile:
                    profiles[session["device_id"]] = profile
            return profiles

    # The annotation is quoted because this class defines a method called
    # `list`, which shadows the builtin for every annotation below it.
    def samples(self, session_id: str) -> "list[dict] | None":
        """The raw labelled readings, for analysis rather than display.

        Kept out of public() on purpose: a session holds up to tens of
        thousands of rows, and the session list would carry all of them.
        """
        with self._lock:
            if session_id not in self._sessions:
                return None
            if self._active_id == session_id:
                # In memory and possibly ahead of the database — the
                # answer has to include what has not been flushed yet.
                return list(self._active_samples)
        with self._db_lock:
            rows = self._conn().execute(
                f"SELECT {', '.join(self._SAMPLE_COLUMNS)} FROM samples "
                "WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        return [
            {
                "t": row["t"],
                "movement_score": row["movement_score"],
                "threshold": row["threshold"],
                "motion": None if row["motion"] is None else bool(row["motion"]),
                "source": row["source"],
                "label": row["label"],
            }
            for row in rows
        ]

    def csv(self, session_id: str) -> str:
        rows = self.samples(session_id)
        if rows is None:
            raise KeyError("Kalibrierung nicht gefunden")
        output = io.StringIO()
        # `source` is part of the export, not an accident of it: a
        # reading means something different depending on which transport
        # measured it, and a CSV that does not say is a CSV somebody will
        # merge with another one. Rows recorded before 0.13.6 have no
        # source and export as blank.
        writer = csv.DictWriter(
            output,
            fieldnames=("t", "movement_score", "threshold", "motion", "source", "label"),
            restval="",
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)
        return output.getvalue()


store = CalibrationStore()
