"""Rooms: a floor plan, one radar placed on it, furniture, and zones.

Stored as one JSON file next to devices.json, with floor-plan images as
separate files — a plan can be a few megabytes, and the JSON is rewritten
on every save.

Every write carries the revision it was based on. Two tabs editing the
same room — the iPad on the sofa and the laptop on the desk — would
otherwise silently overwrite each other; the second save is refused
instead and the editor reloads.
"""

import hashlib
import json
import math
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.geometry import clip_to_plan, furniture_outline, polygon_area, self_intersects

DATA_DIR = Path(os.environ.get("ECHOLOT_DATA_DIR", "/data"))


def _index_path() -> Path:
    return DATA_DIR / "rooms.json"


def image_dir() -> Path:
    return DATA_DIR / "rooms"


MAX_ROOMS = 30
MAX_FURNITURE = 60
MAX_ZONES = 16
MAX_ZONE_POINTS = 16
MAX_OUTLINE_POINTS = 32
#: Floor plans are photos and exports; a few megabytes is plenty and
#: keeps a backup of /data from growing without bound.
MAX_IMAGE_BYTES = 4 * 1024 * 1024

ROOM_ICONS = ("living", "bedroom", "kitchen", "dining", "office", "bath", "hall", "kids", "generic")
FURNITURE_KINDS = (
    "sofa", "armchair", "bed", "table", "desk", "chair", "tv", "wardrobe",
    "plant", "door", "window", "kitchen", "bath", "other",
)
ZONE_KINDS = ("detect", "exclude", "entry")
SMOOTHING = ("off", "normal", "strong")
MAX_INTERFERENCE_SPOTS = 12

_ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
_lock = threading.Lock()


def new_id(prefix: str) -> str:
    return prefix + secrets.token_hex(4)


class SensorPlacement(BaseModel):
    #: The radar device standing in this room, or None while none is.
    device_id: str | None = None
    x: float
    y: float
    #: Degrees, clockwise on screen; 0 looks down the plan.
    angle: float = Field(default=0.0, ge=-360, le=360)
    #: Flip the module's x axis — see geometry.to_room.
    mirror: bool = False
    #: Drawn on the plan as the expected coverage. A planning aid, not a
    #: measurement: nothing reports the module's actual reach.
    range_m: float = Field(default=6.0, ge=1, le=12)
    fov_deg: float = Field(default=120.0, ge=30, le=180)
    #: How high the module hangs, and where on a person it sees them —
    #: needed only if it measures the slant line rather than along the
    #: floor. Given by the user; whether the slant applies is for the
    #: calibration to show.
    mount_height_m: float | None = Field(default=None, ge=0.2, le=4.0)
    target_height_m: float = Field(default=1.0, ge=0.2, le=2.0)
    #: The sensor model the calibration fitted (geometry.correct). The
    #: defaults take the module's positions as they are.
    slant: bool = False
    range_scale: float = Field(default=1.0, ge=0.7, le=1.4)
    range_offset_m: float = Field(default=0.0, ge=-0.6, le=0.6)
    azimuth_scale: float = Field(default=1.0, ge=0.6, le=1.5)

    @model_validator(mode="after")
    def _slant_needs_height(self) -> "SensorPlacement":
        if self.slant and self.mount_height_m is None:
            raise ValueError("Die Schrägkorrektur braucht die Montagehöhe des Sensors")
        return self


class Furniture(BaseModel):
    id: str
    kind: Literal[FURNITURE_KINDS]  # type: ignore[valid-type]
    name: str = Field(default="", max_length=40)
    #: Top-left corner before rotation; rotation is about the centre.
    x: float
    y: float
    w: float = Field(ge=0.1, le=30)
    h: float = Field(ge=0.1, le=30)
    angle: float = Field(default=0.0, ge=-360, le=360)


class Zone(BaseModel):
    id: str
    name: str = Field(min_length=1, max_length=40)
    #: `detect` counts the targets inside it and becomes entities in Home
    #: Assistant. `exclude` removes targets inside it from everything —
    #: the fan, the curtain in the draught, the aquarium. `entry` is a
    #: door or the edge where people leave the sensor's view: whoever was
    #: last seen there has left (see Room.assume_present_s).
    kind: Literal[ZONE_KINDS] = "detect"  # type: ignore[valid-type]
    points: list[tuple[float, float]]
    #: Absence delay: how long the zone stays occupied after its last
    #: target. The module loses people who sit very still; this is the
    #: honest knob for that, and it is per zone because a sofa and a
    #: hallway want very different ones.
    hold_s: float = Field(default=10.0, ge=0, le=600)
    color: int = Field(default=0, ge=0, le=7)
    #: A zone that stands for a furniture item — the sofa, the bed — and
    #: follows it: its corners are the item's, grown by `margin_m` and cut
    #: to the plan, worked out anew on every save (see Room._inside).
    #: None for a zone drawn by hand.
    furniture_id: str | None = None
    #: Room around the item that still counts: the radar places a person
    #: sitting on a sofa somewhere about it, not exactly on the cushions.
    margin_m: float = Field(default=0.2, ge=0, le=1)

    @field_validator("points")
    @classmethod
    def _points(cls, v):
        if not 3 <= len(v) <= MAX_ZONE_POINTS:
            raise ValueError(f"Eine Zone braucht 3 bis {MAX_ZONE_POINTS} Eckpunkte")
        if polygon_area(v) < 0.04:
            raise ValueError("Die Zone ist kleiner als 20 × 20 cm")
        return v


class InterferenceSpot(BaseModel):
    """A place where the radar reported a target while the room was empty.

    In the sensor's own coordinates (metres, before `mirror`), not the
    room's: the reflection belongs to the module and what stands around
    it, so it stays put when the sensor is moved or turned on the plan.
    """

    x: float = Field(ge=-20, le=20)
    y: float = Field(ge=-20, le=20)
    r: float = Field(ge=0.1, le=1.5)
    #: Share of the learning run's reports that had a target here.
    share: float = Field(default=0.0, ge=0, le=1)


#: Standpoints one alignment works with: twelve to fit, six to check.
MAX_FIT_STANDPOINTS = 12
MAX_CHECK_STANDPOINTS = 6
#: Applied alignments a room keeps, the current one included.
MAX_ALIGNMENT_HISTORY = 6


class Standpoint(BaseModel):
    """Somebody stood at `ref` on the plan; the module reported `raw`, in
    its own metres (the median of the recording)."""

    #: Stable across edits of the list, so that removing or measuring one
    #: again from a second tab cannot hit another.
    id: str = Field(default_factory=lambda: new_id("p"))
    ref: tuple[float, float]
    raw: tuple[float, float]
    #: "fit": the alignment is computed from it. "check": a control spot
    #: that takes no part in that and tests the result.
    role: Literal["fit", "check"] = "fit"
    #: How much the reports scattered (90th percentile, metres) and the
    #: share of reports that had the person in them.
    spread_m: float | None = Field(default=None, ge=0, le=20)
    share: float | None = Field(default=None, ge=0, le=1)
    measured_at: float | None = None
    #: "capture": recorded by Echolot. "api": handed in from outside,
    #: nothing Echolot measured.
    source: Literal["capture", "api"] = "api"
    warnings: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("ref", "raw")
    @classmethod
    def _finite(cls, v):
        if not all(math.isfinite(c) and abs(c) <= 50 for c in v):
            raise ValueError("Ein Standpunkt braucht endliche Koordinaten")
        return v


def _check_standpoint_counts(points: list[Standpoint]) -> None:
    fit = sum(p.role == "fit" for p in points)
    if fit > MAX_FIT_STANDPOINTS or len(points) - fit > MAX_CHECK_STANDPOINTS:
        raise ValueError(
            f"Höchstens {MAX_FIT_STANDPOINTS} Standpunkte zum Anpassen und {MAX_CHECK_STANDPOINTS} Kontrollpunkte"
        )


class AlignmentDraft(BaseModel):
    """Standpoints measured and not applied yet, kept so that a reload or
    a second device does not lose them. They belong to the sensor and the
    mounting they were measured with (see invalidate_sensor)."""

    device_id: str | None = None
    points: list[Standpoint] = Field(default_factory=list)
    updated_at: float = 0.0

    @model_validator(mode="after")
    def _counts(self) -> "AlignmentDraft":
        _check_standpoint_counts(self.points)
        return self


class AlignmentRecord(BaseModel):
    """One placement and sensor model the room had, and what it rests on."""

    id: str
    #: "alignment": computed from standpoints and applied. "before": the
    #: sensor as it stood when an alignment replaced it — drawn by hand,
    #: or aligned by 1.3, which kept no record — so it can be taken back.
    origin: Literal["alignment", "before"] = "alignment"
    created_at: float
    device_id: str | None = None
    #: Which mounting of the sensor this was (Calibration.mounting_epoch).
    epoch: int = 0
    #: alignment.VERSION it was computed with; None for "before".
    algorithm: int | None = None
    #: The SENSOR_CALIBRATED fields it set.
    sensor: dict
    report: dict | None = None
    basis: dict | None = None
    standpoints: list[Standpoint] = Field(default_factory=list)
    restored_at: float | None = None

    @model_validator(mode="after")
    def _counts(self) -> "AlignmentRecord":
        _check_standpoint_counts(self.standpoints)
        return self


class Calibration(BaseModel):
    """How a room's reports are filtered before they count.

    Written by the calibration routes only, like the floor plan by its
    upload route: an editor holding an older copy must not undo a
    calibration that finished in the meantime.
    """

    #: A new target counts only once it has been reported for this long.
    #: Reflections that flash up for a report or two never get there.
    #: 0 counts every reported position at once, as 1.0 did.
    confirm_s: float = Field(default=1.0, ge=0, le=5)
    #: How strongly a target's position is averaged over its reports.
    smoothing: Literal[SMOOTHING] = "normal"  # type: ignore[valid-type]
    interference: list[InterferenceSpot] = Field(default_factory=list, max_length=MAX_INTERFERENCE_SPOTS)
    #: The sensor the spots were learned with. They describe that module
    #: where it hung then; another sensor in this room ignores them.
    interference_device_id: str | None = None
    interference_learned_at: float | None = None
    aligned_at: float | None = None
    #: What was left of the error after the last alignment, in metres,
    #: and over how many standpoints.
    alignment_rms_m: float | None = None
    alignment_points: int | None = None
    #: Which sensor model the last alignment settled on (alignment.MODELS),
    #: and its accuracy checked on spots left out, in metres.
    alignment_model: str | None = None
    #: 1.3 only: a leave-one-out with the model chosen from all spots —
    #: not an independent check. Kept readable, no longer written.
    alignment_check_m: float | None = None
    #: What the last alignment reported (REPORT_KEYS), and what it was
    #: computed for (alignment_basis): when any of that changes, the
    #: report no longer describes the room (alignment_state).
    alignment_report: dict | None = None
    alignment_basis: dict | None = None
    #: The record in alignment_history the sensor stands on now; None when
    #: none does (never aligned, aligned by 1.3, or invalidated since).
    alignment_id: str | None = None
    #: Applied alignments, newest first, at most MAX_ALIGNMENT_HISTORY.
    alignment_history: list[AlignmentRecord] = Field(default_factory=list)
    alignment_draft: AlignmentDraft | None = None
    #: Goes up whenever the sensor is swapped or remounted: what was
    #: measured with an earlier mounting no longer describes this one.
    mounting_epoch: int = 0
    invalidated_at: float | None = None
    invalidated_reason: str | None = None
    #: How the module itself said it is mounted — {mode, height_m,
    #: angle_deg} as it read them back — the last time Echolot heard. The
    #: module uses these to compute the positions it reports, so when they
    #: change, whatever was measured before describes other positions.
    module_mounting: dict | None = None


class Room(BaseModel):
    id: str
    name: str = Field(min_length=1, max_length=40)
    icon: Literal[ROOM_ICONS] = "generic"  # type: ignore[valid-type]
    width: float = Field(ge=1, le=30)
    height: float = Field(ge=1, le=30)
    #: The walls, as a polygon on the width × depth plan — for niches,
    #: L-shaped rooms, a chimney breast. None is the plain rectangle.
    #: Targets outside the walls (beyond the edge margin) count nowhere.
    outline: list[tuple[float, float]] | None = None
    sensor: SensorPlacement
    furniture: list[Furniture] = Field(default_factory=list)
    zones: list[Zone] = Field(default_factory=list)
    #: How long the room stays occupied after its last target.
    hold_s: float = Field(default=10.0, ge=0, le=600)
    #: With entrance zones: somebody who vanished anywhere but at an
    #: entrance is assumed to be still there — sitting still, lost by the
    #: radar — for at most this long, or until seen again. 0 turns it off.
    assume_present_s: float = Field(default=1800.0, ge=0, le=43200)
    #: Targets this far outside the walls still count. The module measures
    #: to a decimetre at best, and a person against the wall shows up on
    #: either side of it.
    edge_margin_m: float = Field(default=0.3, ge=0, le=1.5)
    #: {"file": ..., "content_type": ..., "opacity": 0..1} or None.
    image: dict | None = None
    calibration: Calibration = Field(default_factory=Calibration)
    revision: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0

    @model_validator(mode="after")
    def _inside(self) -> "Room":
        """Everything must lie on the plan; ids must be unique and plain."""
        slack = 0.01

        def on_plan(x: float, y: float, what: str) -> None:
            if not (-slack <= x <= self.width + slack and -slack <= y <= self.height + slack):
                raise ValueError(f"{what} liegt außerhalb des Raums ({x:.2f} m, {y:.2f} m)")

        on_plan(self.sensor.x, self.sensor.y, "Der Sensor")
        if self.outline is not None:
            if not 3 <= len(self.outline) <= MAX_OUTLINE_POINTS:
                raise ValueError(f"Die Wände brauchen 3 bis {MAX_OUTLINE_POINTS} Ecken")
            for x, y in self.outline:
                on_plan(x, y, "Eine Wandecke")
            # Crossing first: a figure-eight's area cancels out to nothing.
            if self_intersects(self.outline):
                raise ValueError("Die Wände kreuzen sich — eine Ecke liegt auf der falschen Seite")
            if polygon_area(self.outline) < 1.0:
                raise ValueError("Der Raum innerhalb der Wände ist kleiner als 1 m²")
        if len(self.furniture) > MAX_FURNITURE:
            raise ValueError(f"Höchstens {MAX_FURNITURE} Möbel je Raum")
        if len(self.zones) > MAX_ZONES:
            raise ValueError(f"Höchstens {MAX_ZONES} Zonen je Raum")
        seen: set[str] = set()
        for item in [*self.furniture, *self.zones]:
            if not _ID_RE.match(item.id) or item.id in seen:
                raise ValueError(f"Ungültige oder doppelte Kennung „{item.id}“")
            seen.add(item.id)
        for item in self.furniture:
            on_plan(item.x + item.w / 2, item.y + item.h / 2, f"„{item.name or item.kind}“")
        furniture = {item.id: item for item in self.furniture}
        linked: set[str] = set()
        for zone in self.zones:
            if zone.furniture_id is None:
                continue
            item = furniture.get(zone.furniture_id)
            if item is None:
                raise ValueError(f"Die Zone „{zone.name}“ gehört zu einem Möbelstück, das es nicht mehr gibt")
            if zone.furniture_id in linked:
                raise ValueError(f"„{item.name or item.kind}“ hat schon eine Zone")
            linked.add(zone.furniture_id)
            # The server's corners, not the editor's: what Home Assistant
            # counts must follow the item, whatever a client sent.
            zone.points = clip_to_plan(
                furniture_outline(item.x, item.y, item.w, item.h, item.angle, zone.margin_m),
                self.width, self.height,
            )
            if len(zone.points) < 3 or polygon_area(zone.points) < 0.04:
                raise ValueError(f"Die Zone „{zone.name}“ liegt fast ganz außerhalb des Plans")
        for zone in self.zones:
            for x, y in zone.points:
                on_plan(x, y, f"Ein Eckpunkt der Zone „{zone.name}“")
        names = [z.name.strip().lower() for z in self.zones]
        if len(names) != len(set(names)):
            raise ValueError("Zwei Zonen im selben Raum brauchen verschiedene Namen")
        if self.image is not None:
            opacity = self.image.get("opacity", 0.5)
            if not isinstance(opacity, (int, float)) or not 0 <= opacity <= 1:
                raise ValueError("Deckkraft des Grundrisses muss zwischen 0 und 1 liegen")
        return self


def active_interference(room: Room) -> list[InterferenceSpot]:
    """The learned spots that apply to the sensor standing in the room now."""
    cal = room.calibration
    if not room.sensor.device_id or cal.interference_device_id != room.sensor.device_id:
        return []
    return list(cal.interference)


class RoomCreate(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    icon: Literal[ROOM_ICONS] = "generic"  # type: ignore[valid-type]
    width: float = Field(default=5.0, ge=1, le=30)
    height: float = Field(default=4.0, ge=1, le=30)
    device_id: str | None = None


class RevisionConflict(Exception):
    """The room changed since the editor loaded it."""

    def __init__(self, current: "Room"):
        super().__init__("Der Raum wurde inzwischen an anderer Stelle geändert")
        self.current = current


def _read() -> list[dict]:
    try:
        data = json.loads(_index_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    return list(data.get("rooms") or [])


def _write(rooms: list[dict]) -> None:
    path = _index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"version": 1, "rooms": rooms}, indent=2), encoding="utf-8")
    tmp.replace(path)


def list_rooms() -> list[Room]:
    with _lock:
        return [Room.model_validate(r) for r in _read()]


def get_room(room_id: str) -> Room | None:
    return next((r for r in list_rooms() if r.id == room_id), None)


def room_for_device(device_id: str) -> Room | None:
    return next((r for r in list_rooms() if r.sensor.device_id == device_id), None)


def _check_device_free(rooms: list[dict], room_id: str, device_id: str | None) -> None:
    if device_id is None:
        return
    for other in rooms:
        if other["id"] != room_id and (other.get("sensor") or {}).get("device_id") == device_id:
            raise ValueError(
                f"Dieser Sensor steht schon im Raum „{other['name']}“. Ein Radar "
                "sieht nur einen Raum — nimm ihn dort erst heraus."
            )


def create_room(payload: RoomCreate) -> Room:
    now = time.time()
    room = Room(
        id=new_id("r"),
        name=payload.name.strip(),
        icon=payload.icon,
        width=payload.width,
        height=payload.height,
        # On the top wall, looking into the room: where most people
        # mount one first.
        sensor=SensorPlacement(device_id=payload.device_id, x=round(payload.width / 2, 2), y=0.0),
        revision=1,
        created_at=now,
        updated_at=now,
    )
    with _lock:
        rooms = _read()
        if len(rooms) >= MAX_ROOMS:
            raise ValueError(f"Höchstens {MAX_ROOMS} Räume")
        _check_device_free(rooms, room.id, payload.device_id)
        rooms.append(room.model_dump())
        _write(rooms)
    return room


def save_room(room_id: str, payload: dict) -> Room | None:
    """Replace a room, if nobody else changed it since `payload["revision"]`.

    The image is not taken from the payload: it is uploaded on its own
    route, and an editor holding an older copy must not undo an upload.
    Only its opacity is. The calibration likewise has its own route.

    Another device in the room, or `payload["remounted"]` for the same
    one hung up anew, invalidates what was measured with the old one
    (invalidate_sensor). Moving or turning the sensor on the plan alone is
    a correction of the drawing: the measurements stay, and the alignment
    report is marked stale (alignment_state).
    Returns None when the room does not exist.
    """
    payload = dict(payload)
    remounted = bool(payload.pop("remounted", False))
    with _lock:
        rooms = _read()
        index = next((i for i, r in enumerate(rooms) if r["id"] == room_id), None)
        if index is None:
            return None
        stored = Room.model_validate(rooms[index])
        if payload.get("revision") != stored.revision:
            raise RevisionConflict(stored)
        merged = {
            **payload,
            "id": room_id,
            "image": stored.image,
            "calibration": stored.calibration.model_dump(),
            "created_at": stored.created_at,
        }
        opacity = (payload.get("image") or {}).get("opacity")
        if stored.image is not None and opacity is not None:
            merged["image"] = {**stored.image, "opacity": opacity}
        room = Room.model_validate(merged)
        _check_device_free(rooms, room_id, room.sensor.device_id)
        if room.sensor.device_id != stored.sensor.device_id:
            invalidate_sensor(room, "Sensor gewechselt")
        elif remounted:
            invalidate_sensor(room, "Sensor neu montiert")
        room.revision = stored.revision + 1
        room.updated_at = time.time()
        rooms[index] = room.model_dump()
        _write(rooms)
        return room


#: What a calibration may set on the sensor: where it is, and its model.
#: Which device stands there is the editor's business.
SENSOR_CALIBRATED = {
    "x", "y", "angle", "mirror", "mount_height_m", "target_height_m",
    "slant", "range_scale", "range_offset_m", "azimuth_scale",
}


#: What an alignment report keeps: the three accuracy numbers apart, and
#: what they rest on.
REPORT_KEYS = (
    "model", "points", "fit_rms_m", "cv_rms_m", "validation_rms_m", "validation_points",
    "validation_status", "quality", "layout_ok", "rms_before_m",
)


def alignment_basis(room: "Room") -> dict:
    """What an alignment was computed for."""
    s = room.sensor
    return {
        "device_id": s.device_id,
        "x": s.x, "y": s.y, "angle": s.angle, "mirror": s.mirror,
        "mount_height_m": s.mount_height_m, "target_height_m": s.target_height_m,
        "slant": s.slant, "range_scale": s.range_scale, "range_offset_m": s.range_offset_m,
        "azimuth_scale": s.azimuth_scale,
        "width": room.width, "height": room.height,
    }


BASIS_LABELS = {
    "device_id": "anderer Sensor",
    "x": "Sensor verschoben", "y": "Sensor verschoben", "angle": "Sensor gedreht",
    "mirror": "Links/Rechts geändert",
    "mount_height_m": "Montagehöhe geändert", "target_height_m": "Messhaltung geändert",
    "slant": "Sensormodell geändert", "range_scale": "Sensormodell geändert",
    "range_offset_m": "Sensormodell geändert", "azimuth_scale": "Sensormodell geändert",
    "width": "Raummaße geändert", "height": "Raummaße geändert",
}


def alignment_state(room: "Room") -> dict:
    """Whether the last alignment's report still describes this room.

    "none"     never aligned;
    "unknown"  aligned by 1.3, which kept no record of what for;
    "current"  nothing it rests on has changed;
    "stale"    something has — `changed` says what.
    """
    cal = room.calibration
    if not cal.aligned_at:
        return {"state": "none", "changed": []}
    if not cal.alignment_basis:
        return {"state": "unknown", "changed": []}
    now = alignment_basis(room)
    changed = []
    for key, value in now.items():
        before = cal.alignment_basis.get(key)
        same = before == value if not isinstance(value, float) or before is None else abs(before - value) < 1e-6
        if not same and BASIS_LABELS[key] not in changed:
            changed.append(BASIS_LABELS[key])
    return {"state": "stale" if changed else "current", "changed": changed}


def update_calibration(room_id: str, *, sensor: dict | None = None, calibration: dict | None = None) -> Room | None:
    """Merge a calibration result into the stored room.

    `sensor` may set what an alignment finds (SENSOR_CALIBRATED) and
    nothing else: which device stands there is the editor's business.
    `calibration` replaces the fields it names. The revision goes up, so
    an editor open elsewhere reloads instead of saving over the result.
    Returns None when the room does not exist.
    """
    with _lock:
        rooms = _read()
        index = next((i for i, r in enumerate(rooms) if r["id"] == room_id), None)
        if index is None:
            return None
        stored = Room.model_validate(rooms[index])
        data = stored.model_dump()
        if sensor:
            unknown = set(sensor) - SENSOR_CALIBRATED
            if unknown:
                raise ValueError(f"Nicht einstellbar: {', '.join(sorted(unknown))}")
            data["sensor"].update(sensor)
        if calibration:
            data["calibration"].update(calibration)
        room = Room.model_validate(data)
        room.revision = stored.revision + 1
        room.updated_at = time.time()
        rooms[index] = room.model_dump()
        _write(rooms)
        return room


NEUTRAL_MODEL = {"slant": False, "range_scale": 1.0, "range_offset_m": 0.0, "azimuth_scale": 1.0}


def invalidate_sensor(room: Room, reason: str) -> None:
    """Another sensor, or the same one hung up anew: everything measured
    with the old mounting goes — the learned reflections, the alignment
    and its report, the sensor model, the standpoints not applied yet.
    The placement drawn on the plan and the heights stay; the history
    stays too, but its records belong to the old mounting (`epoch`) and
    are not restored onto the new one.

    Changes `room` in place; the caller saves it.
    """
    for key, value in NEUTRAL_MODEL.items():
        setattr(room.sensor, key, value)
    cal = room.calibration
    cal.interference = []
    cal.interference_device_id = None
    cal.interference_learned_at = None
    cal.aligned_at = None
    cal.alignment_rms_m = None
    cal.alignment_points = None
    cal.alignment_model = None
    cal.alignment_check_m = None
    cal.alignment_report = None
    cal.alignment_basis = None
    cal.alignment_id = None
    cal.alignment_draft = None
    cal.mounting_epoch += 1
    # Whatever the next module says is the first word on the new mounting.
    cal.module_mounting = None
    cal.invalidated_at = time.time()
    cal.invalidated_reason = reason


class CalibrationConflict(Exception):
    """What a request was computed for is no longer what the room is."""

    def __init__(self, message: str, changed: list[str], current: Room):
        super().__init__(message)
        self.changed = changed
        self.current = current


def standpoints_key(points) -> str:
    """A fingerprint of standpoints: which ones, in which order, in which
    role. Two lists with the same key give the same alignment."""
    canon = [
        [p["role"] if isinstance(p, dict) else p.role,
         *[round(float(v), 4) for v in (p["raw"] if isinstance(p, dict) else p.raw)],
         *[round(float(v), 4) for v in (p["ref"] if isinstance(p, dict) else p.ref)]]
        for p in points
    ]
    return hashlib.sha256(json.dumps(canon).encode()).hexdigest()[:16]


def draft_points(room: Room) -> list[Standpoint]:
    """The standpoints measured with the sensor standing in the room now."""
    draft = room.calibration.alignment_draft
    if draft is None or draft.device_id != room.sensor.device_id:
        return []
    return list(draft.points)


def binding_conflicts(room: Room, basis: dict, algorithm: int) -> list[str]:
    """Why a proposal computed for `basis` does not fit the room as it is."""
    changed = []
    if basis.get("device_id") != room.sensor.device_id:
        changed.append("anderer Sensor")
    if basis.get("epoch") != room.calibration.mounting_epoch:
        changed.append("Sensor neu montiert")
    if basis.get("algorithm") != algorithm:
        changed.append("Rechenverfahren geändert")
    if basis.get("standpoints") != standpoints_key(draft_points(room)):
        changed.append("Standpunkte geändert")
    if basis.get("revision") != room.revision and not changed:
        changed.append("Raum geändert")
    return changed


def stale_proposal(room: Room, changed: list[str]) -> CalibrationConflict:
    return CalibrationConflict(
        "Der Vorschlag passt nicht mehr zum Raum (" + ", ".join(changed) + ") — er wird neu berechnet.",
        changed, room,
    )


def _find_room(rooms: list[dict], room_id: str) -> int | None:
    return next((i for i, r in enumerate(rooms) if r["id"] == room_id), None)


def _save_draft(room_id: str, change) -> Room | None:
    """Change the draft without a new revision: the standpoints are the
    calibration page's working state, not the room, and an editor open
    elsewhere must be able to save over them. What a proposal was
    computed from is pinned by standpoints_key instead."""
    with _lock:
        rooms = _read()
        index = _find_room(rooms, room_id)
        if index is None:
            return None
        room = Room.model_validate(rooms[index])
        points = draft_points(room)
        points = change(room, points)
        _check_standpoint_counts(points)
        room.calibration.alignment_draft = (
            AlignmentDraft(device_id=room.sensor.device_id, points=points, updated_at=time.time()) if points else None
        )
        rooms[index] = room.model_dump()
        _write(rooms)
        return room


class GoneError(Exception):
    """What a request refers to is no longer there."""


def add_standpoint(room_id: str, point: Standpoint, *, device_id: str | None, epoch: int,
                   replace_id: str | None = None) -> Room | None:
    """Keep a measured standpoint, or put it in place of `replace_id`.

    `device_id` and `epoch` are the sensor and mounting it was measured
    with; if either is no longer the room's, the measurement is refused
    rather than mixed in with the new one's."""

    def change(room: Room, points: list[Standpoint]) -> list[Standpoint]:
        if device_id != room.sensor.device_id:
            raise CalibrationConflict(
                "Gemessen mit einem anderen Sensor als dem, der jetzt im Raum steht", ["anderer Sensor"], room
            )
        if epoch != room.calibration.mounting_epoch:
            raise CalibrationConflict(
                "Gemessen, bevor der Sensor neu montiert wurde", ["Sensor neu montiert"], room
            )
        if replace_id is None:
            return points + [point]
        index = next((i for i, p in enumerate(points) if p.id == replace_id), None)
        if index is None:
            raise GoneError("Dieser Standpunkt wurde inzwischen entfernt")
        return points[:index] + [point] + points[index + 1:]

    return _save_draft(room_id, change)


def drop_standpoint(room_id: str, point_id: str | None) -> Room | None:
    """Remove one standpoint, or all of them with `point_id` None."""

    def change(_room: Room, points: list[Standpoint]) -> list[Standpoint]:
        if point_id is None:
            return []
        if not any(p.id == point_id for p in points):
            raise GoneError("Dieser Standpunkt wurde inzwischen entfernt")
        return [p for p in points if p.id != point_id]

    return _save_draft(room_id, change)


def _sensor_fields(room: Room) -> dict:
    s = room.sensor.model_dump()
    return {k: s[k] for k in sorted(SENSOR_CALIBRATED)}


def _same_sensor(a: dict, b: dict) -> bool:
    for key in SENSOR_CALIBRATED:
        x, y = a.get(key), b.get(key)
        numbers = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (x, y))
        if numbers:
            if abs(x - y) > 1e-6:
                return False
        elif x != y:
            return False
    return True


def _keep_current(room: Room, now: float) -> None:
    """Before the sensor changes: make sure the state it leaves is in the
    history, so that it can be taken back."""
    cal = room.calibration
    current = next((r for r in cal.alignment_history if r.id == cal.alignment_id), None)
    if current is not None and _same_sensor(current.sensor, _sensor_fields(room)):
        return
    cal.alignment_history.insert(0, AlignmentRecord(
        id=new_id("a"), origin="before", created_at=now, device_id=room.sensor.device_id,
        epoch=cal.mounting_epoch, sensor=_sensor_fields(room), report=cal.alignment_report,
        basis=cal.alignment_basis,
    ))


def _trim_history(cal: Calibration) -> None:
    while len(cal.alignment_history) > MAX_ALIGNMENT_HISTORY:
        oldest = next(i for i in range(len(cal.alignment_history) - 1, -1, -1)
                      if cal.alignment_history[i].id != cal.alignment_id)
        del cal.alignment_history[oldest]


def _apply_report(cal: Calibration, record: AlignmentRecord) -> None:
    report = record.report or {}
    cal.aligned_at = record.created_at if record.origin == "alignment" else None
    cal.alignment_rms_m = report.get("fit_rms_m")
    cal.alignment_points = report.get("points")
    cal.alignment_model = report.get("model")
    cal.alignment_check_m = None
    cal.alignment_report = {k: report.get(k) for k in REPORT_KEYS} if record.report else None
    cal.alignment_id = record.id


def apply_alignment(room_id: str, *, basis: dict, algorithm: int, sensor: dict, report: dict,
                    standpoints: list[Standpoint]) -> Room | None:
    """Keep an alignment, if the room is still what it was computed for.

    The check and the write happen under one lock: a second tab, a
    changed height or another sensor between computing and applying
    raises CalibrationConflict instead of saving a proposal for a room
    that no longer exists. The state it replaces goes into the history.
    """
    with _lock:
        rooms = _read()
        index = _find_room(rooms, room_id)
        if index is None:
            return None
        room = Room.model_validate(rooms[index])
        changed = binding_conflicts(room, basis, algorithm)
        if changed:
            raise stale_proposal(room, changed)
        now = time.time()
        _keep_current(room, now)
        unknown = set(sensor) - SENSOR_CALIBRATED
        if unknown:
            raise ValueError(f"Nicht einstellbar: {', '.join(sorted(unknown))}")
        data = room.model_dump()
        data["sensor"].update(sensor)
        room = Room.model_validate(data)
        record = AlignmentRecord(
            id=new_id("a"), origin="alignment", created_at=now, device_id=room.sensor.device_id,
            epoch=room.calibration.mounting_epoch, algorithm=algorithm, sensor=_sensor_fields(room),
            report={k: report.get(k) for k in REPORT_KEYS}, basis=alignment_basis(room),
            standpoints=standpoints,
        )
        cal = room.calibration
        cal.alignment_history.insert(0, record)
        _apply_report(cal, record)
        cal.alignment_basis = record.basis
        _trim_history(cal)
        room.revision += 1
        room.updated_at = now
        rooms[index] = room.model_dump()
        _write(rooms)
        return room


def restore_alignment(room_id: str, record_id: str, *, revision: int | None) -> Room | None:
    """Put the sensor back as a record in the history had it.

    Only for the same sensor and mounting: a record from before a swap or
    a remount describes another mounting. The record keeps its id; the
    state it replaces is kept as well, so a restore can be taken back.
    """
    with _lock:
        rooms = _read()
        index = _find_room(rooms, room_id)
        if index is None:
            return None
        room = Room.model_validate(rooms[index])
        if revision != room.revision:
            raise CalibrationConflict("Der Raum wurde inzwischen geändert — die Liste ist neu geladen.",
                                      ["Raum geändert"], room)
        cal = room.calibration
        record = next((r for r in cal.alignment_history if r.id == record_id), None)
        if record is None:
            raise GoneError("Diese Ausrichtung gibt es nicht mehr")
        if record.device_id != room.sensor.device_id or record.epoch != cal.mounting_epoch:
            raise CalibrationConflict(
                "Diese Ausrichtung gehört zu einem anderen Sensor oder einer früheren Montage.",
                ["anderer Sensor" if record.device_id != room.sensor.device_id else "Sensor neu montiert"], room,
            )
        now = time.time()
        _keep_current(room, now)
        data = room.model_dump()
        data["sensor"].update({k: v for k, v in record.sensor.items() if k in SENSOR_CALIBRATED})
        room = Room.model_validate(data)
        cal = room.calibration
        record = next(r for r in cal.alignment_history if r.id == record_id)
        record.restored_at = now
        _apply_report(cal, record)
        # What the record was computed for, not the room now: if the room
        # has changed since, the report is stale and says so.
        cal.alignment_basis = record.basis
        _trim_history(cal)
        room.revision += 1
        room.updated_at = now
        rooms[index] = room.model_dump()
        _write(rooms)
        return room


def remount_sensor(room_id: str, *, revision: int | None) -> Room | None:
    """The sensor was taken down and hung up again: see invalidate_sensor."""
    with _lock:
        rooms = _read()
        index = _find_room(rooms, room_id)
        if index is None:
            return None
        room = Room.model_validate(rooms[index])
        if revision != room.revision:
            raise CalibrationConflict("Der Raum wurde inzwischen geändert.", ["Raum geändert"], room)
        invalidate_sensor(room, "Sensor neu montiert")
        room.revision += 1
        room.updated_at = time.time()
        rooms[index] = room.model_dump()
        _write(rooms)
        return room


def _same_mounting(a: dict, b: dict) -> bool:
    return (a.get("mode") == b.get("mode")
            and abs(float(a.get("height_m", 0)) - float(b.get("height_m", 0))) < 0.005
            and abs(float(a.get("angle_deg", 0)) - float(b.get("angle_deg", 0))) < 0.005)


def note_module_mounting(device_id: str, mounting: dict) -> Room | None:
    """The module standing in a room has said how it is mounted.

    The first time, that is only noted: it is what everything so far was
    measured with. It changes nothing the editor holds, so the revision
    stays — a module that answers while the editor is open must not turn
    the next save into a conflict. The pages take the height from the
    module directly.

    After that, a different answer means the module now computes other
    positions: everything measured with the old ones is dropped, as for a
    remount (invalidate_sensor), the room's mounting height follows a
    wall-mounted module, and the revision goes up.
    Returns the room if anything was stored, else None.
    """
    with _lock:
        rooms = _read()
        index = next((i for i, r in enumerate(rooms) if (r.get("sensor") or {}).get("device_id") == device_id), None)
        if index is None:
            return None
        room = Room.model_validate(rooms[index])
        known = room.calibration.module_mounting
        if known is not None and _same_mounting(known, mounting):
            return None
        height = round(float(mounting["height_m"]), 2)
        if known is not None:
            invalidate_sensor(room, "Montage im Modul geändert")
            if mounting.get("mode") == "side" and 0.2 <= height <= 4.0:
                room.sensor.mount_height_m = height
            room.revision += 1
            room.updated_at = time.time()
        room.calibration.module_mounting = {
            "mode": mounting.get("mode"), "height_m": height, "angle_deg": round(float(mounting["angle_deg"]), 2),
        }
        room = Room.model_validate(room.model_dump())
        rooms[index] = room.model_dump()
        _write(rooms)
        return room


def delete_room(room_id: str) -> bool:
    with _lock:
        rooms = _read()
        remaining = [r for r in rooms if r["id"] != room_id]
        if len(remaining) == len(rooms):
            return False
        _write(remaining)
    for path in image_dir().glob(f"{room_id}.*"):
        path.unlink(missing_ok=True)
    return True


def release_device(device_id: str) -> None:
    """Take a deleted device out of whichever room it stood in."""
    with _lock:
        rooms = _read()
        changed = False
        for room in rooms:
            sensor = room.get("sensor") or {}
            if sensor.get("device_id") == device_id:
                model = Room.model_validate(room)
                model.sensor.device_id = None
                invalidate_sensor(model, "Sensor gelöscht")
                room.clear()
                room.update(model.model_dump())
                room["revision"] = int(room.get("revision", 0)) + 1
                changed = True
        if changed:
            _write(rooms)


_IMAGE_TYPES = {
    "image/png": (b"\x89PNG\r\n\x1a\n", "png"),
    "image/jpeg": (b"\xff\xd8\xff", "jpg"),
    "image/webp": (b"RIFF", "webp"),
}


def set_image(room_id: str, content: bytes, content_type: str) -> Room | None:
    """Store a floor plan. It is stretched onto the room's width × depth."""
    kind = _IMAGE_TYPES.get(content_type)
    if kind is None:
        raise ValueError("Grundriss bitte als PNG, JPEG oder WebP")
    magic, extension = kind
    if not content.startswith(magic) or (extension == "webp" and content[8:12] != b"WEBP"):
        raise ValueError("Die Datei ist kein Bild des angegebenen Typs")
    if len(content) > MAX_IMAGE_BYTES:
        raise ValueError(f"Grundriss höchstens {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    with _lock:
        rooms = _read()
        index = next((i for i, r in enumerate(rooms) if r["id"] == room_id), None)
        if index is None:
            return None
        image_dir().mkdir(parents=True, exist_ok=True)
        for old in image_dir().glob(f"{room_id}.*"):
            old.unlink(missing_ok=True)
        name = f"{room_id}.{extension}"
        (image_dir() / name).write_bytes(content)
        previous = rooms[index].get("image") or {}
        rooms[index]["image"] = {
            "file": name,
            "content_type": content_type,
            "opacity": previous.get("opacity", 0.6),
            "version": int(time.time() * 1000),
        }
        rooms[index]["revision"] = int(rooms[index].get("revision", 0)) + 1
        rooms[index]["updated_at"] = time.time()
        _write(rooms)
        return Room.model_validate(rooms[index])


def clear_image(room_id: str) -> Room | None:
    with _lock:
        rooms = _read()
        index = next((i for i, r in enumerate(rooms) if r["id"] == room_id), None)
        if index is None:
            return None
        for old in image_dir().glob(f"{room_id}.*"):
            old.unlink(missing_ok=True)
        rooms[index]["image"] = None
        rooms[index]["revision"] = int(rooms[index].get("revision", 0)) + 1
        rooms[index]["updated_at"] = time.time()
        _write(rooms)
        return Room.model_validate(rooms[index])


def image_path(room: Room) -> Path | None:
    if not room.image:
        return None
    path = image_dir() / room.image["file"]
    return path if path.is_file() else None
