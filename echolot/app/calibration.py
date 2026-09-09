"""Ground-truth recording and transparent threshold recommendations.

Calibration sessions pair direct ESPectre samples with labels supplied by the
person commissioning the room.  The stored data is deliberately plain JSON:
it remains inspectable and exportable, and recommendations can explain their
inputs instead of hiding them in an opaque model.
"""

import csv
import io
import json
import logging
import os
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


def _data_path() -> Path:
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
    #: How long the writer waits before saving, so a burst of readings
    #: becomes one write rather than one write per hundred samples.
    COALESCE_SECONDS = 2.0

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, dict] = {}
        self._dirty = threading.Event()
        self._writer_lock = threading.Lock()
        self._writer = None
        self._load()

    def _load(self) -> None:
        path = _data_path()
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._sessions = raw
        except (OSError, json.JSONDecodeError):
            self._sessions = {}
        # A process restart ends a recording; never silently keep assigning a
        # stale ground-truth label to new samples.
        changed = False
        for session in self._sessions.values():
            if session.get("status") == "recording":
                session["status"] = "interrupted"
                session["ended_at"] = time.time()
                changed = True
        if changed:
            self._save()

    # --- persistence ------------------------------------------------------
    #
    # Writing is O(the whole history): every save serialises every session
    # and replaces the file. Measured on this code — six sessions, 120 000
    # samples — one save takes 171 ms, and it used to happen inline in
    # `ingest`, which runs on the asyncio path that also carries the
    # websocket subscriptions. A sixth of a second of blocked event loop
    # every hundred samples, growing with the history, for a write nobody
    # is waiting on.
    #
    # So the hot path marks the store dirty and a single writer thread
    # does the work. Everything else — creating, labelling, stopping,
    # deleting, importing — still writes synchronously: those are user
    # actions whose result should be on disk when the request returns,
    # and they happen once, not per reading.
    #
    # The cost is that a crash can lose the last COALESCE_SECONDS of
    # samples rather than the last hundred. Comparable, and a recording
    # that survives a crash was never the point of this file.

    def _schedule_save(self) -> None:
        self._dirty.set()
        with self._writer_lock:
            if self._writer is None or not self._writer.is_alive():
                self._writer = threading.Thread(
                    target=self._writer_loop, name="echolot-calibration-writer", daemon=True
                )
                self._writer.start()

    def _writer_loop(self) -> None:
        while True:
            self._dirty.wait()
            # Coalesce a burst of readings into one write.
            time.sleep(self.COALESCE_SECONDS)
            self._dirty.clear()
            try:
                with self._lock:
                    self._save()
            except Exception:  # noqa: BLE001 - a failed write must not kill the writer
                logger.exception("Kalibrierung konnte nicht gespeichert werden")

    def flush(self) -> None:
        """Write now and wait for it — for shutdown and for stop()."""
        self._dirty.clear()
        with self._lock:
            self._save()

    def _save(self) -> None:
        path = _data_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self._sessions, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def create(self, device_id: str, name: str = "") -> dict:
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
                "samples": [],
                "recommendation": None,
            }
            self._sessions[session_id] = session
            self._save()
            return self.public(session)

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
            self._save()
            return self.public(session)

    def stop(self, session_id: str) -> dict:
        with self._lock:
            session = self._require(session_id)
            if session["status"] == "recording":
                now = time.time()
                session["status"] = "complete"
                session["ended_at"] = now
                session["segments"][-1]["ended_at"] = now
                session["recommendation"] = recommendation(session["samples"])
                self._save()
            # A finished recording is on disk before the caller is told it
            # finished, whatever the writer thread is doing.
            self._dirty.clear()
            return self.public(session)

    def adopt(self, device_id: str, samples: list[Sample], *, label: str, name: str = "") -> dict:
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

        rows = [{**sample.as_dict(), "label": label} for sample in samples[:MAX_SAMPLES_PER_SESSION]]
        started_at = min(row["t"] for row in rows)
        ended_at = max(row["t"] for row in rows)
        with self._lock:
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
                "samples": rows,
                "recommendation": recommendation(rows),
                #: So the UI can say where this came from, and so a later
                #: reader does not mistake it for something somebody sat
                #: through.
                "source": "history",
            }
            self._sessions[session_id] = session
            self._save()
            return self.public(session)

    def delete(self, session_id: str) -> bool:
        with self._lock:
            removed = self._sessions.pop(session_id, None) is not None
            if removed:
                self._save()
            return removed

    def ingest(self, device_id: str, sample: Sample) -> None:
        with self._lock:
            active = next(
                (s for s in self._sessions.values() if s["device_id"] == device_id and s["status"] == "recording"),
                None,
            )
            if active is None:
                return
            if len(active["samples"]) >= MAX_SAMPLES_PER_SESSION:
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
                    self._schedule_save()
                return
            active["samples"].append({**sample.as_dict(), "label": active["label"]})
        # Outside the lock: the writer thread needs it to do the work.
        if len(active["samples"]) % PERSIST_EVERY_SAMPLES == 0:
            self._schedule_save()

    def list(self, device_id: str | None = None) -> list[dict]:
        with self._lock:
            sessions = self._sessions.values()
            if device_id is not None:
                sessions = (session for session in sessions if session["device_id"] == device_id)
            return sorted((self.public(session) for session in sessions), key=lambda s: s["started_at"], reverse=True)

    def get(self, session_id: str) -> dict | None:
        with self._lock:
            session = self._sessions.get(session_id)
            return self.public(session) if session else None

    def _require(self, session_id: str) -> dict:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise KeyError("Kalibrierung nicht gefunden") from None

    @staticmethod
    def public(session: dict) -> dict:
        counts = {label: 0 for label in LABELS}
        for row in session["samples"]:
            counts[row["label"]] = counts.get(row["label"], 0) + 1
        result = {
            key: value for key, value in session.items() if key != "samples"
        }
        if session["status"] == "recording" or "recommendation" not in session:
            result["recommendation"] = recommendation(session["samples"])
        return result | {
            "sample_count": len(session["samples"]),
            "label_counts": counts,
        }

    def latest_profiles(self) -> dict[str, dict]:
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
                if profile is None and "recommendation" not in session:
                    profile = recommendation(session["samples"])
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
            session = self._sessions.get(session_id)
            return list(session["samples"]) if session else None

    def csv(self, session_id: str) -> str:
        with self._lock:
            session = self._require(session_id)
            output = io.StringIO()
            writer = csv.DictWriter(
                output, fieldnames=("t", "movement_score", "threshold", "motion", "label")
            )
            writer.writeheader()
            writer.writerows(session["samples"])
            return output.getvalue()


store = CalibrationStore()
