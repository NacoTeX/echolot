"""Ground-truth recording and transparent threshold recommendations.

Calibration sessions pair direct ESPectre samples with labels supplied by the
person commissioning the room.  The stored data is deliberately plain JSON:
it remains inspectable and exportable, and recommendations can explain their
inputs instead of hiding them in an opaque model.
"""

import csv
import io
import json
import os
import statistics
import threading
import time
import uuid
from pathlib import Path

from app.telemetry import Sample

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
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, dict] = {}
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
            if active is None or len(active["samples"]) >= MAX_SAMPLES_PER_SESSION:
                return
            active["samples"].append({**sample.as_dict(), "label": active["label"]})
            if len(active["samples"]) % PERSIST_EVERY_SAMPLES == 0:
                self._save()

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
