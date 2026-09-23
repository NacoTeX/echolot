"""What every room and zone is doing, worked out from the radar frames.

One loop, one clock. It runs when a frame arrives (at most every
`MIN_INTERVAL`) and on a slow timer otherwise, so a hold time runs out and
a lost sensor turns unavailable without anybody watching. Everything else
— the live map, the Home Assistant entities — reads its results.

The rules, in order, for each target of a current frame:

  1. Turn sensor coordinates into room coordinates (geometry.to_room).
  2. Outside the walls by more than the room's edge margin: shown on the
     map, counted nowhere. Radar sees through drywall.
  3. Inside an exclusion zone: shown, counted nowhere.
  4. Otherwise it counts for the room and for every detection zone that
     contains it. Zones may overlap; a target in two counts in both.

A room or zone is occupied while it has a target, and for its hold time
after the last one. It is *unavailable* — not empty — whenever there is
no current frame to judge by: no sensor placed, no connection, no frame
for three seconds, or a module that answers without reporting while
`quiet_means_empty` is off.
"""

import asyncio
import logging
import time
from dataclasses import dataclass

from app import geometry
from app.radar_frame import QUIET, RECEIVING

logger = logging.getLogger("echolot.engine")

#: Frames arrive up to ten times a second per sensor; evaluating more
#: often than that is work nobody sees.
MIN_INTERVAL = 0.1
#: Without frames: often enough for a hold time to end on time and for a
#: silent sensor to go unavailable within a second of going stale.
IDLE_INTERVAL = 0.5

REASONS = {
    "no_sensor": "Diesem Raum ist noch kein Sensor zugeordnet.",
    "device_missing": "Der zugeordnete Sensor existiert nicht mehr.",
    "offline": "Keine Verbindung zum Sensor.",
    "no_frame_entity": "Der Sensor liefert keine Positionen — seine Firmware ist keine Echolot-Radar-Firmware.",
    "stale": "Seit über drei Sekunden keine Meldung vom Sensor.",
    "radar_unknown": "Der Sensor ist online, aber das Radarmodul antwortet nicht. Verkabelung prüfen.",
    "radar_quiet": (
        "Das Radarmodul antwortet, meldet aber nichts. Ob das „leer“ heißt, ist "
        "beim Gerät einstellbar (Stille = leerer Raum)."
    ),
}


@dataclass
class _Hold:
    last_seen: float | None = None

    def update(self, present: bool, now: float, hold_s: float) -> tuple[bool, float]:
        """(occupied, seconds of hold left)."""
        if present:
            self.last_seen = now
            return True, 0.0
        if self.last_seen is None:
            return False, 0.0
        left = hold_s - (now - self.last_seen)
        if left > 0:
            return True, left
        self.last_seen = None
        return False, 0.0


class RoomEngine:
    def __init__(self, links, clock=time.monotonic) -> None:
        self._links = links
        self._clock = clock
        self._rooms: list = []
        self._devices: dict = {}
        self._holds: dict[str, _Hold] = {}
        self._listeners: list = []
        #: Created in start(), on the loop that runs the engine: an Event
        #: made at import belongs to whichever loop touches it first.
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self.latest: dict[str, dict] = {}

    # --- inputs -----------------------------------------------------------

    def load(self, room_list, device_list) -> None:
        """Replace the rooms and devices the engine works from."""
        self._rooms = list(room_list)
        self._devices = {d.id: d for d in device_list}
        live_keys = set()
        for room in self._rooms:
            live_keys.add(room.id)
            live_keys.update(f"{room.id}/{z.id}" for z in room.zones)
        for key in list(self._holds):
            if key not in live_keys:
                del self._holds[key]
        self.evaluate()

    def add_listener(self, callback) -> None:
        self._listeners.append(callback)

    def wake(self, _device_id: str | None = None) -> None:
        if self._wake is not None:
            self._wake.set()

    # --- the rules --------------------------------------------------------

    def _hold(self, key: str) -> _Hold:
        return self._holds.setdefault(key, _Hold())

    def _reset(self, room) -> None:
        self._holds.pop(room.id, None)
        for zone in room.zones:
            self._holds.pop(f"{room.id}/{zone.id}", None)

    def _points(self, room, now: float) -> tuple[list | None, str | None, dict | None]:
        """(sensor points or None, reason when None, sensor snapshot)."""
        device_id = room.sensor.device_id
        if not device_id:
            return None, "no_sensor", None
        device = self._devices.get(device_id)
        if device is None:
            return None, "device_missing", None
        snap = self._links.snapshot(device_id)
        view = snap.as_dict(now) if snap else None
        if snap is None or not snap.connected:
            return None, "offline", view
        if snap.no_frame_entity:
            return None, "no_frame_entity", view
        if not snap.fresh(now):
            return None, "stale", view
        frame = snap.frame
        if frame.state == RECEIVING:
            return list(frame.targets_m), None, view
        if frame.state == QUIET:
            if device.config.radar_quiet_means_empty:
                return [], None, view
            return None, "radar_quiet", view
        return None, "radar_unknown", view

    def evaluate_room(self, room, now: float) -> dict:
        points, reason, sensor_view = self._points(room, now)
        placement = room.sensor.model_dump()
        detect = [z for z in room.zones if z.kind == "detect"]
        exclude = [z for z in room.zones if z.kind == "exclude"]

        if points is None:
            self._reset(room)
            return {
                "room_id": room.id,
                "available": False,
                "reason": reason,
                "reason_text": REASONS[reason],
                "count": None,
                "occupied": None,
                "hold_remaining": 0.0,
                "targets": [],
                "zones": [
                    {"id": z.id, "name": z.name, "count": None, "occupied": None, "hold_remaining": 0.0}
                    for z in detect
                ],
                "sensor": sensor_view,
            }

        targets = []
        for raw_x, raw_y in points:
            x, y = geometry.to_room(raw_x, raw_y, placement)
            if not geometry.in_room(x, y, room.width, room.height, room.edge_margin_m):
                status, zone_ids = "outside", []
            elif any(geometry.point_in_polygon(x, y, z.points) for z in exclude):
                status, zone_ids = "excluded", []
            else:
                status = "counted"
                zone_ids = [z.id for z in detect if geometry.point_in_polygon(x, y, z.points)]
            targets.append({
                "x": round(x, 3), "y": round(y, 3),
                "raw_x": raw_x, "raw_y": raw_y,
                "status": status, "zones": zone_ids,
            })

        count = sum(1 for t in targets if t["status"] == "counted")
        occupied, left = self._hold(room.id).update(count > 0, now, room.hold_s)
        zone_views = []
        for zone in detect:
            zone_count = sum(1 for t in targets if zone.id in t["zones"])
            zone_occupied, zone_left = self._hold(f"{room.id}/{zone.id}").update(
                zone_count > 0, now, zone.hold_s
            )
            zone_views.append({
                "id": zone.id, "name": zone.name, "count": zone_count,
                "occupied": zone_occupied, "hold_remaining": round(zone_left, 1),
            })
        return {
            "room_id": room.id,
            "available": True,
            "reason": None,
            "reason_text": None,
            "count": count,
            "occupied": occupied,
            "hold_remaining": round(left, 1),
            "targets": targets,
            "zones": zone_views,
            "sensor": sensor_view,
        }

    def evaluate(self) -> list[dict]:
        now = self._clock()
        results = []
        for room in self._rooms:
            try:
                result = self.evaluate_room(room, now)
            except Exception:  # noqa: BLE001 - one broken room must not stop the others
                logger.exception("Raum %s konnte nicht ausgewertet werden", room.id)
                continue
            result["evaluated_at"] = time.time()
            results.append(result)
        self.latest = {r["room_id"]: r for r in results}
        for callback in list(self._listeners):
            try:
                callback(self._rooms, results)
            except Exception:  # noqa: BLE001
                logger.exception("Listener der Raumauswertung fehlgeschlagen")
        return results

    # --- the loop ---------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), IDLE_INTERVAL)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            self.evaluate()
            await asyncio.sleep(MIN_INTERVAL)

    def start(self) -> None:
        if self._task is None:
            self._wake = asyncio.Event()
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            self._wake = None
