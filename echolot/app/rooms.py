"""Rooms: a floor plan, one radar placed on it, furniture, and zones.

Stored as one JSON file next to devices.json, with floor-plan images as
separate files — a plan can be a few megabytes, and the JSON is rewritten
on every save.

Every write carries the revision it was based on. Two tabs editing the
same room — the iPad on the sofa and the laptop on the desk — would
otherwise silently overwrite each other; the second save is refused
instead and the editor reloads.
"""

import json
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
ZONE_KINDS = ("detect", "exclude")
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
    #: the fan, the curtain in the draught, the aquarium.
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
    alignment_check_m: float | None = None


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
    Returns None when the room does not exist.
    """
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
                sensor["device_id"] = None
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
