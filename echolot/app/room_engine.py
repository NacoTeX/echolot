"""What every room and zone is doing, worked out from the radar frames.

One loop, one clock. It runs when a frame arrives (at most every
`MIN_INTERVAL`) and on a slow timer otherwise, so a hold time runs out and
a lost sensor turns unavailable without anybody watching. Everything else
— the live map, the Home Assistant entities — reads its results.

The rules, in order — measurement definition 5 (`MEASUREMENT_VERSION`):

  1. Every new report goes through the room's tracker (app/tracking.py),
     each at the time it arrived and in order — a repeat of the last line
     (the firmware's heartbeat) is no new report (radar_link):
     positions are followed from report to report and smoothed, and a
     new target is confirmed only once it has been reported for the
     room's confirmation time outside the learned interference spots.
  2. Each target of the latest report is corrected by the sensor model
     the calibration fitted — distance and angle scale, slant line — and
     turned into room coordinates (geometry.to_room).
  3. Outside the walls by more than the room's edge margin: shown on the
     map, counted nowhere. Radar sees through drywall. The walls are the
     room's outline when it has one (niches, L-shapes), else its
     width × depth rectangle.
  4. Inside an exclusion zone: shown, counted nowhere.
  5. Not confirmed yet: shown, counted nowhere.
  6. Otherwise it counts for the room and for every detection zone that
     contains it. Zones may overlap; a target in two counts in both.
     A confirmed target the latest report did not carry still counts,
     where it was last seen, for as long as the tracker remembers it
     (tracking.TRACK_TTL_S) — status "held". Not with a confirmation
     time of 0 s, which counts each report as it is.

Definition 4 (1.4) counted only the targets of the latest report: one
dropped report took a person out of the count and put them back.
Definition 3 (1.3) counted heartbeat repeats as reports, took only the
latest report per evaluation, at evaluation time, and could match a
target to a report after its memory had run out. Definition 2 (1.1–1.2)
was 3 without the sensor model.
Definition 1 (Echolot 1.0) was rules 2–4 and 6 on the raw report, with
the rectangle as the walls. For a room without an outline, with a
confirmation time of 0 s, smoothing off and no interference spots,
definition 2 gives the same answers.

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
from app.rooms import active_interference
from app.tracking import SMOOTHING_ALPHA, Tracker

logger = logging.getLogger("echolot.engine")

#: Which rules produced a count. Goes out with every result and as an
#: attribute of every Home Assistant entity, so a recorded history can be
#: read with the rules that made it. Raise it whenever the rules change.
MEASUREMENT_VERSION = 5

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


def filter_settings(room) -> dict:
    """What decides this room's counts besides its zones — for display
    and for Home Assistant."""
    cal = room.calibration
    return {
        "definition_version": MEASUREMENT_VERSION,
        "confirm_s": cal.confirm_s,
        "smoothing": cal.smoothing,
        "interference_spots": len(active_interference(room)),
        # The sensor model positions are corrected with (geometry.correct).
        "range_scale": room.sensor.range_scale,
        "range_offset_m": room.sensor.range_offset_m,
        "azimuth_scale": room.sensor.azimuth_scale,
        "slant": room.sensor.slant,
    }


@dataclass
class _Follow:
    """A room's tracker, what it was set up for, and the report it last took."""

    #: Sensor, confirmation time and spots. When any of them changes the
    #: targets are confirmed afresh: one confirmed before its spot was
    #: learned would otherwise keep counting on the reflector for good.
    setup: tuple
    tracker: Tracker
    #: The sensor connection the tracker follows, and the last of its
    #: queued measurements it took (radar_link.LinkSnapshot.queue).
    session: int = 0
    last_index: int = 0


class RoomEngine:
    def __init__(self, links, clock=time.monotonic) -> None:
        self._links = links
        self._clock = clock
        self._rooms: list = []
        self._devices: dict = {}
        self._holds: dict[str, _Hold] = {}
        self._follow: dict[str, _Follow] = {}
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
        for room_id in list(self._follow):
            if room_id not in live_keys:
                del self._follow[room_id]
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
        # A target is not the same one after an outage: whoever is there
        # afterwards is confirmed afresh.
        self._follow.pop(room.id, None)

    def _points(self, room, now: float) -> tuple[list | None, str | None, dict | None, object]:
        """(sensor points or None, reason when None, sensor view, link snapshot)."""
        device_id = room.sensor.device_id
        if not device_id:
            return None, "no_sensor", None, None
        device = self._devices.get(device_id)
        if device is None:
            return None, "device_missing", None, None
        snap = self._links.snapshot(device_id)
        view = snap.as_dict(now) if snap else None
        if snap is None or not snap.connected:
            return None, "offline", view, None
        if snap.no_frame_entity:
            return None, "no_frame_entity", view, None
        if not snap.fresh(now):
            return None, "stale", view, None
        frame = snap.frame
        if frame.state == RECEIVING:
            return list(frame.targets_m), None, view, snap
        if frame.state == QUIET:
            if device.config.radar_quiet_means_empty:
                return [], None, view, snap
            return None, "radar_quiet", view, None
        return None, "radar_unknown", view, None

    def _tracker(self, room, snap) -> Tracker:
        """The room's tracker, fed with every measurement it has not had yet.

        The engine evaluates on a timer as well as on reports, and at most
        ten times a second: it takes the new measurements queued since the
        last round, in order, each at the time it arrived. A repeat of the
        last line is no new measurement (radar_link.LinkSnapshot.record)
        and moves, confirms or forgets nothing.
        """
        spots = active_interference(room)
        setup = (
            room.sensor.device_id,
            room.calibration.confirm_s,
            tuple((s.x, s.y, s.r) for s in spots),
        )
        follow = self._follow.get(room.id)
        if follow is None or follow.setup != setup or follow.session != snap.session:
            # A fresh start takes the current measurement, not whatever is
            # still queued from before.
            follow = self._follow[room.id] = _Follow(setup, Tracker(), snap.session, max(0, snap.index - 1))
        quiet_is_empty = self._devices[room.sensor.device_id].config.radar_quiet_means_empty
        for index, received_at, frame in snap.pending(follow.last_index):
            follow.last_index = index
            if frame.state == RECEIVING:
                points = list(frame.targets_m)
            elif frame.state == QUIET and quiet_is_empty:
                points = []
            else:
                continue
            follow.tracker.update(
                points, received_at,
                confirm_s=room.calibration.confirm_s,
                alpha=SMOOTHING_ALPHA[room.calibration.smoothing],
                spots=spots,
            )
        return follow.tracker

    def evaluate_room(self, room, now: float) -> dict:
        points, reason, sensor_view, snap = self._points(room, now)
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
                "filter": filter_settings(room),
            }

        targets = []
        tracker = self._tracker(room, snap)
        # With a confirmation time of 0 s the room counts what each report
        # says and nothing else, as definition 1 did: nothing is held.
        held = tracker.held(now) if room.calibration.confirm_s > 0 else []
        for track in sorted(tracker.visible() + held, key=lambda t: t.id):
            x, y = geometry.to_room(track.x, track.y, placement)
            zone_ids: list[str] = []
            if not geometry.within_walls(x, y, room.width, room.height, room.outline, room.edge_margin_m):
                status = "outside"
            elif any(geometry.point_in_polygon(x, y, z.points) for z in exclude):
                status = "excluded"
            elif not track.confirmed:
                status = "interference" if track.in_spot else "pending"
            else:
                status = "counted" if track.seen else "held"
                zone_ids = [z.id for z in detect if geometry.point_in_polygon(x, y, z.points)]
            targets.append({
                "id": track.id,
                "x": round(x, 3), "y": round(y, 3),
                # Sensor coordinates of the same (smoothed) position.
                "raw_x": round(track.x, 3), "raw_y": round(track.y, 3),
                "status": status, "zones": zone_ids,
            })

        count = sum(1 for t in targets if t["status"] in ("counted", "held"))
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
            "filter": filter_settings(room),
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
