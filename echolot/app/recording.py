"""Recordings: what a radar node said, kept to be played back.

A recording holds what the room engine was given — every frame line as it
arrived and when, and when the connection came and went — with the room
as it was set up, and what the person recording said was really going on
(marks). Played back through the same engine on a virtual clock
(app/replay.py), the same file with the same settings gives the same
results every time; with other settings, the difference is what the
settings make.

Small on purpose: nothing records until somebody starts it, every
recording stops by itself (`limit_s`, at most MAX_LIMIT_S), and all of
them together stay under MAX_TOTAL_BYTES. A file carries no credentials:
the device's id, name and board, no keys, no Wi-Fi.

File format 1, one JSON object per line:

  {"type": "header", "format": 1, ...}              the first line
  {"t": 1.234, "type": "line", "text": "1|R|..."}   a frame line, exactly as
                                                    it arrived, t seconds
                                                    after the start
  {"t": ..., "type": "link", "connected": true, "no_frame_entity": false}
  {"t": ..., "type": "mark", "kind": ..., ...}      see Mark
"""

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from app import rooms

logger = logging.getLogger("echolot.recording")

FORMAT = 1
DEFAULT_LIMIT_S = 600
MAX_LIMIT_S = 3600
#: All recordings together. A module reporting ten times a second fills
#: about 1.5 MB an hour.
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_RECORDINGS = 50
MAX_IMPORT_BYTES = 20 * 1024 * 1024
#: A recording starts only with this much room left: the header alone is
#: a few kilobytes, and a file without it is no recording.
MIN_FREE_BYTES = 64 * 1024
#: Frame lines are short; anything much longer is not one.
MAX_LINE_CHARS = 512


class RecordingError(Exception):
    """A recording cannot be started, marked or read — in words for the UI."""


class Mark(BaseModel):
    """What was really going on, said while recording.

    standpoint      somebody stands still at (x, y) from now on, the spot
                    marked on the plan to within `uncertainty_m`
    standpoint_end  they have left it
    people          this many people are in the room from now on
    zone            somebody went into (inside) or out of a detection zone
    note            anything else, as text
    """

    kind: Literal["standpoint", "standpoint_end", "people", "zone", "note"]
    x: float | None = None
    y: float | None = None
    uncertainty_m: float = Field(default=0.25, ge=0.05, le=2.0)
    count: int | None = Field(default=None, ge=0, le=20)
    zone_id: str | None = Field(default=None, max_length=64)
    inside: bool | None = None
    text: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _complete(self):
        if self.kind == "standpoint" and (self.x is None or self.y is None):
            raise ValueError("Ein Standpunkt braucht x und y")
        if self.kind == "people" and self.count is None:
            raise ValueError("Wie viele Personen?")
        if self.kind == "zone" and (self.zone_id is None or self.inside is None):
            raise ValueError("Welche Zone, hinein oder hinaus?")
        if self.kind == "note" and not (self.text or "").strip():
            raise ValueError("Die Notiz ist leer")
        return self

    def as_event(self) -> dict:
        keep = {
            "standpoint": ("x", "y", "uncertainty_m"),
            "standpoint_end": (),
            "people": ("count",),
            "zone": ("zone_id", "inside"),
            "note": ("text",),
        }[self.kind]
        return {"type": "mark", "kind": self.kind, **{k: getattr(self, k) for k in keep}}


def _dir() -> Path:
    return rooms.DATA_DIR / "recordings"


def _index_path() -> Path:
    return _dir() / "index.json"


def path_of(recording_id: str) -> Path:
    if not recording_id.isalnum():
        raise RecordingError("Unbekannte Aufzeichnung")
    return _dir() / f"{recording_id}.jsonl"


_lock = threading.Lock()


def _read_index() -> list[dict]:
    try:
        return json.loads(_index_path().read_text(encoding="utf-8")).get("recordings", [])
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        logger.exception("Aufzeichnungsverzeichnis unlesbar")
        return []


def _write_index(entries: list[dict]) -> None:
    path = _index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"version": 1, "recordings": entries}, indent=2), encoding="utf-8")
    tmp.replace(path)


def _update_entry(recording_id: str, **fields) -> dict | None:
    with _lock:
        entries = _read_index()
        for entry in entries:
            if entry["id"] == recording_id:
                entry.update(fields)
                _write_index(entries)
                return dict(entry)
    return None


def list_recordings() -> list[dict]:
    with _lock:
        return sorted(_read_index(), key=lambda e: e["started_at"], reverse=True)


def get(recording_id: str) -> dict | None:
    return next((e for e in list_recordings() if e["id"] == recording_id), None)


def usage() -> dict:
    entries = list_recordings()
    return {
        "bytes_used": sum(int(e.get("bytes") or 0) for e in entries),
        "bytes_max": MAX_TOTAL_BYTES,
        "count": len(entries),
        "count_max": MAX_RECORDINGS,
    }


def mark_interrupted() -> list[str]:
    """At start-up: a recording still marked as running was cut off when
    the add-on stopped. What it has is kept."""
    with _lock:
        entries = _read_index()
        cut = [e for e in entries if e.get("state") == "recording"]
        for entry in cut:
            entry["state"] = "interrupted"
            entry["stop_reason"] = "Add-on neu gestartet"
            try:
                entry["bytes"] = path_of(entry["id"]).stat().st_size
            except (OSError, RecordingError):
                pass
        if cut:
            _write_index(entries)
    return [e["id"] for e in cut]


def delete(recording_id: str) -> bool:
    with _lock:
        entries = _read_index()
        kept = [e for e in entries if e["id"] != recording_id]
        if len(kept) == len(entries):
            return False
        _write_index(kept)
    try:
        path_of(recording_id).unlink()
    except FileNotFoundError:
        pass
    return True


def load(recording_id: str) -> tuple[dict, list[dict]]:
    """(header, events) of a stored recording."""
    try:
        text = path_of(recording_id).read_text(encoding="utf-8")
    except FileNotFoundError as err:
        raise RecordingError("Die Aufzeichnung gibt es nicht mehr") from err
    return parse(text)


def parse(text: str) -> tuple[dict, list[dict]]:
    """Read a recording, checking every line. A file cut off in the middle
    of its last line — the add-on stopped while writing — loses that line."""
    lines = text.splitlines()
    if not lines:
        raise RecordingError("Die Datei ist leer")
    try:
        header = json.loads(lines[0])
    except ValueError as err:
        raise RecordingError("Keine Echolot-Aufzeichnung: die erste Zeile ist kein JSON") from err
    if not isinstance(header, dict) or header.get("type") != "header":
        raise RecordingError("Keine Echolot-Aufzeichnung: der Kopf fehlt")
    if header.get("format") != FORMAT:
        raise RecordingError(f"Aufzeichnungsformat {header.get('format')} kennt diese Version nicht")
    try:
        rooms.Room.model_validate(header.get("room"))
    except ValidationError as err:
        raise RecordingError("Der Raum im Kopf der Aufzeichnung ist ungültig") from err
    device = header.get("device")
    if not isinstance(device, dict) or not isinstance(device.get("id"), str):
        raise RecordingError("Das Gerät im Kopf der Aufzeichnung fehlt")
    events = []
    last_t = 0.0
    for number, raw in enumerate(lines[1:], start=2):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except ValueError as err:
            if number == len(lines):
                break  # cut off while being written
            raise RecordingError(f"Zeile {number} ist kein JSON") from err
        events.append(_checked(event, number, last_t))
        last_t = events[-1]["t"]
    return header, events


def _checked(event, number: int, last_t: float) -> dict:
    if not isinstance(event, dict):
        raise RecordingError(f"Zeile {number} ist kein Ereignis")
    t = event.get("t")
    if not isinstance(t, (int, float)) or t != t or t < last_t:
        raise RecordingError(f"Zeile {number}: die Zeit fehlt oder läuft rückwärts")
    kind = event.get("type")
    if kind == "line":
        text = event.get("text")
        if not isinstance(text, str) or len(text) > MAX_LINE_CHARS:
            raise RecordingError(f"Zeile {number}: keine Meldungszeile")
        return {"t": float(t), "type": "line", "text": text}
    if kind == "link":
        if not isinstance(event.get("connected"), bool):
            raise RecordingError(f"Zeile {number}: Verbindung ohne Zustand")
        return {"t": float(t), "type": "link", "connected": event["connected"],
                "no_frame_entity": bool(event.get("no_frame_entity", False))}
    if kind == "mark":
        try:
            mark = Mark.model_validate({k: v for k, v in event.items() if k not in ("t", "type")})
        except ValidationError as err:
            raise RecordingError(f"Zeile {number}: ungültige Markierung") from err
        return {"t": float(t), **mark.as_event()}
    raise RecordingError(f"Zeile {number}: unbekanntes Ereignis {kind!r}")


def import_text(text: str, name: str = "") -> dict:
    """Store a recording from elsewhere — another add-on, a test rig. It is
    checked line by line and kept as it came, synthetic or not."""
    if len(text.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise RecordingError(f"Größer als {MAX_IMPORT_BYTES // (1024 * 1024)} MB")
    header, events = parse(text)
    use = usage()
    size = len(text.encode("utf-8"))
    if use["count"] >= MAX_RECORDINGS or use["bytes_used"] + size > MAX_TOTAL_BYTES:
        raise RecordingError("Der Speicher für Aufzeichnungen ist voll. Ältere löschen, dann noch einmal.")
    recording_id = uuid.uuid4().hex
    room = header["room"]
    duration = events[-1]["t"] if events else 0.0
    entry = {
        "id": recording_id,
        "room_id": room.get("id"),
        "room_name": room.get("name"),
        "device_id": header["device"]["id"],
        "started_at": float(header.get("started_at") or time.time()),
        "limit_s": header.get("limit_s"),
        "duration_s": round(duration, 1),
        "lines": sum(1 for e in events if e["type"] == "line"),
        "marks": sum(1 for e in events if e["type"] == "mark"),
        "bytes": size,
        "synthetic": bool(header.get("synthetic", False)),
        "imported": True,
        "state": "done",
        "stop_reason": None,
        "note": str(header.get("note") or name or "")[:200],
        "definition": header.get("definition"),
        "format": FORMAT,
    }
    path = path_of(recording_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    with _lock:
        entries = _read_index()
        entries.append(entry)
        _write_index(entries)
    return entry


class _Active:
    def __init__(self, entry: dict, room, handle, t0: float, budget: int) -> None:
        self.entry = entry
        #: The room as recorded (the header's).
        self.room = room
        self.handle = handle
        self.t0 = t0
        #: What this recording may still write: what was free when it
        #: started, less what the others running then had written.
        self.budget = budget
        self.bytes = 0
        #: The latest event time written: a file's times never run back.
        self.last_t = 0.0
        self.lines = 0
        self.marks = 0
        #: The standpoint somebody is standing on, if any.
        self.standpoint: dict | None = None
        self.people: int | None = None


class Recorder:
    """The recordings being made, fed by the radar links."""

    def __init__(self, links, clock=time.monotonic, wall=time.time) -> None:
        self._links = links
        self._clock = clock
        self._wall = wall
        self._active: dict[str, _Active] = {}

    # --- starting and stopping ---------------------------------------------------

    def start(self, room, device, *, limit_s: float = DEFAULT_LIMIT_S, note: str = "",
              definition: int, alignment: int, addon: str) -> dict:
        if room.id in self._active:
            raise RecordingError("Für diesen Raum läuft schon eine Aufzeichnung.")
        if device is None or room.sensor.device_id != device.id:
            raise RecordingError("Dem Raum ist kein Sensor zugeordnet.")
        if not 10 <= limit_s <= MAX_LIMIT_S:
            raise RecordingError(f"Die Dauer muss zwischen 10 s und {MAX_LIMIT_S // 60} min liegen.")
        use = usage()
        budget = MAX_TOTAL_BYTES - use["bytes_used"] - sum(a.bytes for a in self._active.values())
        if use["count"] >= MAX_RECORDINGS or budget < MIN_FREE_BYTES:
            raise RecordingError("Der Speicher für Aufzeichnungen ist voll. Ältere löschen, dann noch einmal.")
        now = self._clock()
        snap = self._links.snapshot(device.id)
        last_line = None
        if snap is not None and snap.frame is not None and snap.frame_at is not None:
            last_line = {"text": snap.frame.as_line(), "age_s": round(now - snap.frame_at, 4)}
        recording_id = uuid.uuid4().hex
        header = {
            "type": "header",
            "format": FORMAT,
            "id": recording_id,
            # Made by the add-on from a live link. A recording made up by a
            # program says so here, and is shown as such.
            "synthetic": False,
            "started_at": self._wall(),
            "limit_s": limit_s,
            "note": note[:200],
            "definition": definition,
            "alignment": alignment,
            "addon": addon,
            "room": room.model_dump(mode="json"),
            # Who the lines came from; nothing that opens the device.
            "device": {
                "id": device.id,
                "name": device.config.name,
                "friendly_name": device.config.friendly_name,
                "board": device.config.board,
                "radar_quiet_means_empty": device.config.radar_quiet_means_empty,
            },
            "node": snap.node if snap is not None else None,
            "link": {
                "connected": bool(snap and snap.connected),
                "no_frame_entity": bool(snap and snap.no_frame_entity),
            },
            # The line on hand when the recording started: without it, the
            # first heartbeat repeat would read as a new report on playback.
            "last_line": last_line,
        }
        path = path_of(recording_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("w", encoding="utf-8")
        entry = {
            "id": recording_id,
            "room_id": room.id,
            "room_name": room.name,
            "device_id": device.id,
            "started_at": header["started_at"],
            "limit_s": limit_s,
            "duration_s": 0.0,
            "lines": 0,
            "marks": 0,
            "bytes": 0,
            "synthetic": False,
            "imported": False,
            "state": "recording",
            "stop_reason": None,
            "note": header["note"],
            "definition": definition,
            "format": FORMAT,
        }
        active = _Active(entry, room, handle, now, budget)
        self._write(active, header)
        with _lock:
            entries = _read_index()
            entries.append(dict(entry, bytes=active.bytes))
            _write_index(entries)
        self._active[room.id] = active
        return self.status(room.id)

    def stop(self, room_id: str, reason: str | None = None) -> dict | None:
        active = self._active.pop(room_id, None)
        if active is None:
            return None
        duration = self._clock() - active.t0
        try:
            active.handle.close()
        except OSError:
            logger.exception("Aufzeichnung %s nicht sauber geschlossen", active.entry["id"])
        return _update_entry(
            active.entry["id"], state="done", stop_reason=reason, duration_s=round(duration, 1),
            lines=active.lines, marks=active.marks, bytes=active.bytes,
        )

    def stop_all(self, reason: str) -> None:
        for room_id in list(self._active):
            self.stop(room_id, reason)

    def sync(self, room_list) -> None:
        """After every change to the rooms: a recording whose room is gone,
        or whose room now has another sensor, has nothing more to record."""
        by_id = {r.id: r for r in room_list}
        for room_id, active in list(self._active.items()):
            room = by_id.get(room_id)
            if room is None:
                self.stop(room_id, "Raum gelöscht")
            elif room.sensor.device_id != active.entry["device_id"]:
                self.stop(room_id, "Anderer Sensor zugeordnet")

    def tick(self) -> None:
        """Stop what has reached its time — also while no line arrives."""
        now = self._clock()
        for room_id, active in list(self._active.items()):
            if now - active.t0 >= active.entry["limit_s"]:
                self.stop(room_id, "Zeit erreicht")

    # --- what is recorded -------------------------------------------------------

    def on_line(self, device_id: str, text: str, now: float) -> None:
        for active in self._for_device(device_id, now):
            self._write(active, {"t": round(now - active.t0, 4), "type": "line", "text": text[:MAX_LINE_CHARS]})
            active.lines += 1

    def on_link(self, device_id: str, connected: bool, no_frame_entity: bool, now: float) -> None:
        for active in self._for_device(device_id, now):
            self._write(active, {"t": round(now - active.t0, 4), "type": "link", "connected": connected,
                                 "no_frame_entity": no_frame_entity})

    def mark(self, room_id: str, mark: Mark) -> dict:
        active = self._active.get(room_id)
        if active is None:
            raise RecordingError("Für diesen Raum läuft keine Aufzeichnung.")
        if mark.kind == "zone" and mark.zone_id not in {z.id for z in active.room.zones if z.kind == "detect"}:
            raise RecordingError("Diese Zone gibt es in der Aufzeichnung nicht.")
        if mark.kind == "standpoint_end" and active.standpoint is None:
            raise RecordingError("Es ist kein Standpunkt offen.")
        now = self._clock()
        event = {"t": round(now - active.t0, 4), **mark.as_event()}
        self._write(active, event)
        active.marks += 1
        if mark.kind == "standpoint":
            active.standpoint = {"x": mark.x, "y": mark.y, "since": event["t"]}
        elif mark.kind == "standpoint_end":
            active.standpoint = None
        elif mark.kind == "people":
            active.people = mark.count
        return event

    def active(self) -> dict[str, dict]:
        """Room id -> status of every recording running now."""
        return {room_id: self.status(room_id) for room_id in list(self._active)}

    def status(self, room_id: str) -> dict | None:
        active = self._active.get(room_id)
        if active is None:
            return None
        elapsed = self._clock() - active.t0
        return {
            **active.entry,
            "elapsed_s": round(elapsed, 1),
            "remaining_s": round(max(0.0, active.entry["limit_s"] - elapsed), 1),
            "lines": active.lines,
            "marks": active.marks,
            "bytes": active.bytes,
            "standpoint": active.standpoint,
            "people": active.people,
        }

    # --- inside --------------------------------------------------------------------

    def _for_device(self, device_id: str, now: float) -> list[_Active]:
        out = []
        for room_id, active in list(self._active.items()):
            if active.entry["device_id"] != device_id:
                continue
            if now - active.t0 >= active.entry["limit_s"]:
                self.stop(room_id, "Zeit erreicht")
                continue
            out.append(active)
        return out

    def _write(self, active: _Active, event: dict) -> None:
        if "t" in event:
            event["t"] = active.last_t = max(event["t"], active.last_t)
        line = json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n"
        size = len(line.encode("utf-8"))
        if active.bytes + size > active.budget:
            room_id = next((r for r, a in self._active.items() if a is active), None)
            if room_id is not None:
                self.stop(room_id, "Speicher voll")
            return
        try:
            active.handle.write(line)
            active.handle.flush()
        except OSError:
            logger.exception("Aufzeichnung %s: Schreiben fehlgeschlagen", active.entry["id"])
            room_id = next((r for r, a in self._active.items() if a is active), None)
            if room_id is not None:
                self.stop(room_id, "Schreiben fehlgeschlagen")
            return
        active.bytes += size
