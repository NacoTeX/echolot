"""Echolot, learning by itself.

Every room's tracker is watched (app/learning.Observer) from the moment
its sensor reports, and every ten minutes what was seen is worked out
in the background. What comes of it:

  * Reflectors, found while Home Assistant says nobody is home and
    nothing walks anywhere (app/ha_presence.py), become interference
    spots — unless people sit there, then they are proposed.
  * Hold times grow to what the module's lapses with somebody sitting
    call for: a zone no longer empties under somebody on the sofa.
  * The plan is checked against the walks every day brings. A sensor
    that looks another way than drawn is turned — when nothing else
    could explain the walks; anything less certain is proposed.
  * Places people stay at without a zone become proposals for one.

What is taken over is the room's `learned` layer (rooms.Learned) —
never what was set by hand — and a turn of the sensor goes through the
alignment history like any change of it. Everything lands in the room's
journal, and everything taken over can be taken back there. Taking
something back, or turning a proposal down, is remembered: it is not
proposed again.

Settings: mode "auto" (take over what is safe, propose the rest),
"suggest" (propose everything) or "off" (watch nothing, take nothing
over — what was taken over is dropped); and which Home Assistant entity
says whether anybody is home (default: every person).

Kept in /data/learning: settings.json, and per room <room_id>.json —
the record (bounded, see app/learning.py), the journal and what was
turned down. A room's record belongs to one sensor in one mounting.
"""

import asyncio
import copy
import json
import logging
import math
import secrets
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app import geometry, learning, rooms

logger = logging.getLogger("echolot.learner")

FILE_VERSION = 1
#: Nobody home for this long before what stands in a room counts as no
#: person: whoever left last has gone by then, and Home Assistant has
#: caught up with the phones.
AWAY_GRACE_S = 600.0
ANALYSE_EVERY_S = 600.0
SAVE_EVERY_S = 300.0
#: The placement search at most this often, and only on this many new
#: walking points since the last.
FIT_EVERY_S = 3600.0
FIT_NEW_POINTS = 100
MAX_JOURNAL = 60
#: A learned hold time is changed by at least this much, or not at all.
HOLD_STEP_S = 5.0
#: A learned spot that moved less than this is the same one.
SPOT_SAME_M = 0.2
#: Notes on things walking while nobody is home, at most this often.
MOTION_NOTE_S = 6 * 3600.0

FURNITURE_LABELS = {
    "sofa": "Sofa", "armchair": "Sessel", "bed": "Bett", "table": "Tisch", "desk": "Schreibtisch",
    "chair": "Stuhl", "tv": "Fernseher", "wardrobe": "Schrank", "plant": "Pflanze", "door": "Tür",
    "window": "Fenster", "kitchen": "Küchenzeile", "bath": "Wanne", "other": "Objekt",
}

FEMININE = ("door", "plant", "kitchen", "bath")


def _dir() -> Path:
    return rooms.DATA_DIR / "learning"


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


class Settings(BaseModel):
    mode: Literal["auto", "suggest", "off"] = "auto"
    #: The Home Assistant entity that says whether anybody is home; None:
    #: every person.
    presence_entity: str | None = Field(default=None, max_length=255, pattern=r"^[a-z_]+\.[a-z0-9_]+$")


def load_settings() -> Settings:
    try:
        return Settings.model_validate(json.loads((_dir() / "settings.json").read_text(encoding="utf-8")))
    except (FileNotFoundError, ValueError):
        return Settings()


def _metres(v: float) -> str:
    return f"{v:.1f}".replace(".", ",") + " m"


def _seconds(v: float) -> str:
    v = round(v)
    return f"{v // 60} min {v % 60} s" if v >= 120 and v % 60 else (f"{v // 60} min" if v >= 120 else f"{v} s")


def where(room: rooms.Room, x: float, y: float) -> str:
    """A place in the room, in words: by the nearest piece of furniture,
    or in metres from the top-left corner of the plan."""
    best, best_d = None, math.inf
    for f in room.furniture:
        d = geometry.distance_to_polygon(x, y, geometry.furniture_outline(f.x, f.y, f.w, f.h, f.angle))
        if geometry.point_in_polygon(x, y, geometry.furniture_outline(f.x, f.y, f.w, f.h, f.angle)):
            d = 0.0
        if d < best_d:
            best, best_d = f, d
    if best is not None and best_d <= 0.8:
        if best.name:
            return f"bei „{best.name}“"
        label = FURNITURE_LABELS.get(best.kind, "Objekt")
        return f"an der {label}" if best.kind in FEMININE else f"am {label}"
    return f"bei {_metres(x)} / {_metres(y)}"


def _drawn(room: rooms.Room) -> tuple:
    s = room.sensor
    return (round(s.x, 3), round(s.y, 3), round(s.angle, 2), bool(s.mirror))


def _mirror_known(room: rooms.Room) -> bool:
    """Whether which side is the sensor's left was settled for the plan as
    drawn: by the walk test, or by an alignment from standpoints."""
    cal = room.calibration
    if cal.axes_check and cal.axes_check.get("mirror") == room.sensor.mirror:
        return True
    current = next((r for r in cal.alignment_history if r.id == cal.alignment_id), None)
    return bool(current is not None and current.origin == "alignment"
                and current.sensor.get("mirror") == room.sensor.mirror)


def analyse(state: dict, room: rooms.Room, *, fit: bool) -> dict:
    """Everything a record says about a room — pure, for a worker thread."""
    placement = room.sensor.model_dump()
    found = learning.find_interference(state, room, placement)
    recorded = []
    cal = room.calibration
    if room.sensor.device_id and cal.interference_device_id == room.sensor.device_id:
        recorded = [s.model_dump() for s in cal.interference]
    spots = recorded + [s for s in found if s["auto"]]
    out = {
        "at": time.time(),
        "interference": found,
        "plausibility": learning.plausibility(state, room, placement),
        "holds": learning.hold_recommendations(state, room, placement, spots=spots),
        "zones": learning.zone_suggestions(state, room, placement, spots=spots),
        "turn": None, "anywhere": None, "verdict": None,
    }
    inside = out["plausibility"]["inside_share"]
    if inside is not None and inside >= learning.FITS:
        out["verdict"] = {"verdict": "fits", "inside": inside, "placement": None, "auto": False}
    elif inside is not None and fit:
        mode = (cal.module_mounting or {}).get("mode")
        out["turn"] = learning.fit_turn(state, room)
        out["anywhere"] = learning.fit_anywhere(state, room, mode)
        out["verdict"] = learning.placement_verdict(out["turn"], out["anywhere"], _mirror_known(room))
    return out


class _Room:
    """One room's record, journal and what was turned down."""

    def __init__(self, room_id: str, device_id: str | None, epoch: int, now: float, saved: dict | None = None):
        self.room_id = room_id
        saved = saved or {}
        state = saved.get("state")
        fresh = not learning.usable(state, device_id, epoch)
        self.state = learning.empty_state(device_id, epoch, now) if fresh else state
        self.journal: list[dict] = list(saved.get("journal") or [])
        self.declined: dict = {"spots": [], "holds": [], "keys": [], **(saved.get("declined") or {})}
        self.analysis: dict | None = None if fresh else saved.get("analysis")
        self.fit_at = 0.0 if fresh else float(saved.get("fit_at") or 0.0)
        self.fit_moves = 0 if fresh else int(saved.get("fit_moves") or 0)
        #: The sensor as drawn when the placement was last searched.
        self.fit_for = None if fresh else (tuple(saved["fit_for"]) if saved.get("fit_for") else None)
        self.motion_noted = float(saved.get("motion_noted") or 0.0)
        if fresh:
            self.declined = {"spots": [], "holds": [], "keys": []}
        self.observer = learning.Observer(self.state)
        self.generation = None
        self.dirty = fresh
        self.saved_at = now

    def dump(self) -> dict:
        return {"version": FILE_VERSION, "room_id": self.room_id, "state": self.state, "journal": self.journal,
                "declined": self.declined, "analysis": self.analysis, "fit_at": self.fit_at,
                "fit_moves": self.fit_moves, "fit_for": self.fit_for, "motion_noted": self.motion_noted}

    # --- the journal --------------------------------------------------------

    def note(self, kind: str, title: str, detail: str = "", *, state: str = "info", key: str | None = None,
             data: dict | None = None, auto: bool = False, now: float | None = None) -> dict:
        entry = {"id": "j" + secrets.token_hex(4), "at": now or time.time(), "kind": kind, "state": state,
                 "key": key, "title": title, "detail": detail, "data": data or {}, "auto": auto}
        self.journal.insert(0, entry)
        # Open proposals stay; the oldest of the rest go.
        while len(self.journal) > MAX_JOURNAL:
            index = next((i for i in range(len(self.journal) - 1, -1, -1) if self.journal[i]["state"] != "open"), None)
            if index is None:
                break
            del self.journal[index]
        self.dirty = True
        return entry

    def entry(self, entry_id: str) -> dict | None:
        return next((e for e in self.journal if e["id"] == entry_id), None)

    def open_entry(self, key: str) -> dict | None:
        return next((e for e in self.journal if e["state"] == "open" and e["key"] == key), None)

    def propose(self, kind: str, key: str, title: str, detail: str, data: dict, now: float) -> None:
        """An open proposal under `key`, made or brought up to date."""
        if key in self.declined["keys"]:
            return
        entry = self.open_entry(key)
        if entry is None:
            self.note(kind, title, detail, state="open", key=key, data=data, now=now)
        elif entry["title"] != title or entry["detail"] != detail or entry["data"] != data:
            entry.update(title=title, detail=detail, data=data, at=now)
            self.dirty = True

    def withdraw(self, prefix: str, keep: set) -> None:
        """Open proposals under `prefix` that no longer hold."""
        before = len(self.journal)
        self.journal = [e for e in self.journal
                        if not (e["state"] == "open" and (e["key"] or "").startswith(prefix) and e["key"] not in keep)]
        self.dirty = self.dirty or len(self.journal) != before


class Learner:
    def __init__(self, presence, clock=time.time) -> None:
        self.presence = presence
        self._clock = clock
        self.settings = load_settings()
        presence.entity = self.settings.presence_entity
        self._rooms: dict[str, _Room] = {}
        #: The rooms as stored, by id — as the engine last had them.
        self._known: dict[str, rooms.Room] = {}
        self._refresh = None
        self._task: asyncio.Task | None = None
        self._analysed_at = 0.0
        #: Rooms worked out right now, so two rounds never overlap.
        self._busy: set[str] = set()

    # --- the engine's side ----------------------------------------------------

    def away(self) -> bool | None:
        """Whether nobody is home, and has not been for long enough."""
        away_for = self.presence.away_for(self._clock())
        if away_for is None:
            return None if self.presence.home is None else False
        return away_for >= AWAY_GRACE_S

    def _for(self, room: rooms.Room) -> _Room | None:
        device_id = room.sensor.device_id
        if not device_id:
            return None
        epoch = room.calibration.mounting_epoch
        now = self._clock()
        lr = self._rooms.get(room.id)
        if lr is not None and learning.usable(lr.state, device_id, epoch):
            return lr
        if lr is None:
            saved = None
            try:
                saved = json.loads((_dir() / f"{room.id}.json").read_text(encoding="utf-8"))
            except FileNotFoundError:
                pass
            except ValueError:
                logger.warning("Lerndaten von %s unlesbar, beginne neu", room.id)
            lr = self._rooms[room.id] = _Room(room.id, device_id, epoch, now, saved)
            if saved is None:
                lr.note("info", "Echolot lernt diesen Raum kennen",
                        "Ab jetzt wird beobachtet, wie Menschen sich hier bewegen und wo sie sitzen.", now=now)
            elif lr.dirty:
                lr.note("info", "Neuer Sensor oder neu montiert — Lernen beginnt von vorn",
                        "Was mit dem bisherigen Sensor gelernt wurde, gilt für diesen nicht.", now=now)
            return lr
        # Another sensor, or the same one hung up anew, while running.
        lr.observer.pause(now)
        fresh = _Room(room.id, device_id, epoch, now, {"journal": lr.journal})
        fresh.note("info", "Neuer Sensor oder neu montiert — Lernen beginnt von vorn",
                   "Was mit dem bisherigen Sensor gelernt wurde, gilt für diesen nicht.", now=now)
        self._rooms[room.id] = fresh
        return fresh

    def observe(self, room: rooms.Room, tracks, generation: int) -> None:
        if self.settings.mode == "off":
            return
        lr = self._for(room)
        if lr is None:
            return
        now = self._clock()
        if lr.generation != generation:
            # A new tracker: its ids are not the old one's.
            if lr.generation is not None:
                lr.observer.pause(now)
            lr.generation = generation
        if lr.observer.observe(tracks, now, self.away()):
            # Something walked while nobody was home: in every room, what
            # stands still now may be somebody.
            for other in self._rooms.values():
                other.observer.taint()
            if now - lr.motion_noted >= MOTION_NOTE_S:
                lr.motion_noted = now
                lr.note("motion", "Bewegung, während niemand zu Hause war",
                        "Ein Haustier, ein Saugroboter — oder jemand, den Home Assistant nicht kennt. Diese "
                        "Zeit zählt nicht fürs Erkennen von Störquellen.", now=now)
        lr.dirty = True

    def pause(self, room: rooms.Room) -> None:
        lr = self._rooms.get(room.id)
        if lr is not None:
            lr.observer.pause(self._clock())
            lr.generation = None

    # --- keeping it -------------------------------------------------------------

    def sync(self, room_list) -> None:
        """The rooms as stored now. A deleted room's record goes with it."""
        self._known = {r.id: r for r in room_list}
        for room_id in [k for k in self._rooms if k not in self._known]:
            del self._rooms[room_id]
            (_dir() / f"{room_id}.json").unlink(missing_ok=True)

    def forget_room(self, room_id: str) -> None:
        self._rooms.pop(room_id, None)
        (_dir() / f"{room_id}.json").unlink(missing_ok=True)

    def _due(self, room_id: str | None, force: bool) -> list[tuple[Path, str]]:
        """What is to be written: serialised here, on the loop that changes
        the records, so that a worker thread never reads one half-changed."""
        now = self._clock()
        out = []
        for lr in list(self._rooms.values()):
            if room_id is not None and lr.room_id != room_id:
                continue
            if lr.dirty and (force or now - lr.saved_at >= SAVE_EVERY_S):
                out.append((_dir() / f"{lr.room_id}.json", json.dumps(lr.dump(), separators=(",", ":"))))
                lr.dirty = False
                lr.saved_at = now
        return out

    @staticmethod
    def _write(due: list[tuple[Path, str]]) -> None:
        for path, text in due:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)

    def save(self, room_id: str | None = None, *, force: bool = False) -> None:
        self._write(self._due(room_id, force))

    def flush(self) -> None:
        """Before the add-on stops: everything open is finished and kept."""
        now = self._clock()
        for lr in self._rooms.values():
            lr.observer.flush(now)
            lr.dirty = True
        self.save(force=True)

    def set_settings(self, wanted: dict) -> Settings:
        settings = Settings.model_validate({**self.settings.model_dump(), **wanted})
        _write_json(_dir() / "settings.json", settings.model_dump())
        was = self.settings.mode
        self.settings = settings
        self.presence.entity = settings.presence_entity
        if settings.mode == "off" and was != "off":
            for room in rooms.list_rooms():
                if room.learned is not None:
                    self._set_learned(room, None)
                lr = self._rooms.get(room.id)
                if lr is not None:
                    lr.observer.pause(self._clock())
                    lr.note("info", "Lernen ausgeschaltet",
                            "Gelernte Störquellen und Haltezeiten gelten nicht mehr. Beobachtet wird nichts.")
        return settings

    # --- working it out -----------------------------------------------------------

    async def analyse_room(self, room_id: str, *, force_fit: bool = False) -> dict | None:
        room = rooms.get_room(room_id)
        if room is None or self.settings.mode == "off" or room_id in self._busy:
            return None
        lr = self._for(room)
        if lr is None:
            return None
        now = self._clock()
        moves = len(lr.state.get("moves", []))
        drawn = _drawn(room)
        fit = (force_fit or drawn != lr.fit_for
               or (now - lr.fit_at >= FIT_EVERY_S and abs(moves - lr.fit_moves) >= FIT_NEW_POINTS))
        state = copy.deepcopy(lr.state)
        self._busy.add(room_id)
        try:
            result = await asyncio.to_thread(analyse, state, room, fit=fit)
        finally:
            self._busy.discard(room_id)
        if result["turn"] is not None:
            lr.fit_at, lr.fit_moves, lr.fit_for = now, moves, drawn
        elif result["verdict"] is None and lr.analysis and lr.fit_for == drawn:
            # No new search this round: the last one, for the plan as it
            # is, still stands.
            for key in ("turn", "anywhere", "verdict"):
                result[key] = lr.analysis.get(key)
        lr.analysis = result
        lr.dirty = True
        # The room may have changed while that ran.
        room = rooms.get_room(room_id)
        if room is not None and self._for(room) is lr and await self._take(room, lr, result):
            # Turned: everything else again, for the plan as it is now.
            room = rooms.get_room(room_id)
            result = await asyncio.to_thread(analyse, state, room, fit=False)
            lr.analysis = result
            if self._for(room) is lr:
                await self._take(room, lr, result)
        return result

    async def analyse_all(self) -> None:
        for room_id in list(self._known):
            try:
                await self.analyse_room(room_id)
            except Exception:  # noqa: BLE001 - one room must not stop the others
                logger.exception("Auswertung von Raum %s fehlgeschlagen", room_id)
        self._analysed_at = self._clock()

    def _set_learned(self, room: rooms.Room, learned: dict | None) -> rooms.Room | None:
        try:
            return rooms.set_learned(room.id, learned, device_id=room.sensor.device_id,
                                     epoch=room.calibration.mounting_epoch)
        except rooms.CalibrationConflict:
            return None

    @staticmethod
    def _layer(room: rooms.Room) -> dict:
        if rooms.learned_applies(room):
            return room.learned.model_dump(include={"spots", "hold_s", "zone_hold_s"})
        return {"spots": [], "hold_s": None, "zone_hold_s": {}}

    async def _take(self, room: rooms.Room, lr: _Room, result: dict) -> bool:
        """Take over what is safe, propose the rest. True if the sensor was
        turned: then nothing else is, as it was worked out for the plan
        before."""
        auto = self.settings.mode == "auto"
        now = self._clock()
        layer = self._layer(room)
        changed = False
        placement = room.sensor.model_dump()

        # The plan.
        verdict = result.get("verdict") or {}
        kind = verdict.get("verdict")
        if kind == "turn" and verdict.get("auto") and auto and not self._declined_turn(lr, verdict["placement"]):
            target = verdict["placement"]
            try:
                turned = rooms.turn_sensor(room.id, expect=placement, angle=target["angle"],
                                           mirror=target["mirror"])
            except rooms.CalibrationConflict:
                turned = None
            if turned is not None:
                room, record_id = turned
                lr.withdraw("placement", set())
                lr.note("placement", f"Sensor-Blickrichtung korrigiert: {round(placement['angle'])}° → "
                        f"{round(target['angle'])}°",
                        f"Nur {round(verdict['inside'] * 100)} % der Wege lagen innerhalb der Wände; so sind es "
                        f"{round(target['inside'] * 100)} %, und die Plätze, an denen Menschen sitzen, liegen auf "
                        "den Sitzmöbeln. Grob korrigiert — zentimetergenau wird es mit Standpunkten.",
                        state="applied", key="placement",
                        data={"before": {k: placement[k] for k in ("x", "y", "angle", "mirror")},
                              "after": {k: target[k] for k in ("x", "y", "angle", "mirror")}, "record_id": record_id},
                        auto=True, now=now)
                if self._refresh is not None:
                    await self._refresh()
                # Everything else was worked out for the plan as it was.
                return True
        elif kind in ("turn", "mirror", "ambiguous", "elsewhere", "unclear"):
            text = _placement_text(kind, verdict, placement)
            if text and not (verdict.get("placement") and self._declined_turn(lr, verdict["placement"])):
                lr.propose("placement", "placement", text[0], text[1],
                           {"verdict": kind, "placement": verdict.get("placement"),
                            "before": {k: placement[k] for k in ("x", "y", "angle", "mirror")}}, now)
        elif kind == "fits":
            lr.withdraw("placement", set())

        # Reflectors.
        declined = [tuple(p) for p in lr.declined["spots"]]
        cal = room.calibration
        recorded = list(cal.interference) if cal.interference_device_id == room.sensor.device_id else []

        def wanted_spot(s: dict) -> bool:
            if any(math.hypot(s["x"] - x, s["y"] - y) <= max(s["r"], 0.3) for x, y in declined):
                return False
            return not any(math.hypot(s["x"] - r.x, s["y"] - r.y) <= r.r + s["r"] for r in recorded)

        found = [s for s in result["interference"] if wanted_spot(s)]
        wanted = [{k: s[k] for k in ("x", "y", "r", "share")} for s in found if s["auto"]][: rooms.MAX_INTERFERENCE_SPOTS]
        before = layer["spots"]
        if _spots_differ(before, wanted):
            added = [s for s in wanted if not any(_same_spot(s, b) for b in before)]
            removed = [b for b in before if not any(_same_spot(b, s) for s in wanted)]
            def place(s: dict) -> str:
                return where(room, *geometry.to_room(s["x"], s["y"], placement))

            if added:
                title = (("Störquelle erkannt: " if len(added) == 1 else f"{len(added)} Störquellen erkannt: ")
                         + ", ".join(place(s) for s in added))
                detail = ("Dort meldete das Radar ein Ziel, während niemand zu Hause war: "
                          + "; ".join(f"{place(s)} ({round(s['share'] * 100)} % der Zeit)" for s in added)
                          + ". Ein neues Ziel dort zählt erst, wenn es von woanders kommt.")
            else:
                title = "Störquelle nicht mehr gesehen: " + ", ".join(place(s) for s in removed)
                detail = "Was dort stand, ist nicht mehr zu sehen; die Stelle zählt wieder wie jede andere."
            if added and removed:
                detail += " Nicht mehr gesehen: " + ", ".join(place(s) for s in removed) + "."
            data = {"before": before, "after": wanted}
            if auto:
                layer["spots"] = wanted
                changed = True
                lr.withdraw("spots", set())
                lr.note("spots", title, detail, state="applied", key="spots", data=data, auto=True, now=now)
            else:
                lr.propose("spots", "spots", title, detail, data, now)
        else:
            lr.withdraw("spots", set())
        doubtful = [s for s in found if not s["auto"] and s["reason"] in ("on_seat", "people_sit")]
        keep = set()
        for s in doubtful:
            key = f"spot:{s['x']:.1f},{s['y']:.1f}"
            keep.add(key)
            place = where(room, *geometry.to_room(s["x"], s["y"], placement))
            lr.propose("spot", key, f"Mögliche Störquelle {place}",
                       "Während niemand zu Hause war, meldete das Radar dort ein Ziel — "
                       f"{round(s['share'] * 100)} % der Zeit. Weil dort auch Menschen sitzen, wird das nicht "
                       "selbst übernommen: Als Störquelle zählt dort jemand, der lange still sitzt, nicht mehr.",
                       {"spot": {k: s[k] for k in ("x", "y", "r", "share")}}, now)
        lr.withdraw("spot:", keep)

        # Hold times.
        holds = result["holds"]
        zones = {z.id: z for z in room.zones if z.kind == "detect"}
        targets = [("room", None, holds.get("room"))] + [(zid, zones[zid], rec) for zid, rec in holds["zones"].items()
                                                           if zid in zones]
        for target, zone, rec in targets:
            if target in lr.declined["holds"]:
                continue
            base = zone.hold_s if zone is not None else room.hold_s
            old = layer["zone_hold_s"].get(target) if zone is not None else layer["hold_s"]
            new = rec["hold_s"] if rec is not None and rec["hold_s"] > base else None
            if (old is None) == (new is None) and (new is None or abs(new - old) < HOLD_STEP_S):
                continue
            if rec is None:
                # Too little left to go by: what was learned stays.
                continue
            name = f"Zone „{zone.name}“" if zone is not None else "Raum"
            title = (f"Abwesenheitsverzögerung {name}: {_seconds(max(base, old or 0))} → {_seconds(max(base, new or 0))}")
            detail = (f"Das Radar verlor Sitzende dort bis zu {_seconds(rec['q99_s'])} lang "
                      f"({rec['gaps']} Aussetzer in {rec['stays']} Aufenthalten). So lange wartet "
                      f"{'die Zone' if zone is not None else 'der Raum'} jetzt, bevor {'sie' if zone is not None else 'er'} "
                      "sich leer meldet.")
            if rec.get("short"):
                detail += " Längere Aussetzer kommen vor — mehr als 3 Minuten wartet Echolot nie von selbst."
            data = {"target": target, "before": old, "after": new}
            key = f"hold:{target}"
            if auto:
                if zone is not None:
                    if new is None:
                        layer["zone_hold_s"].pop(target, None)
                    else:
                        layer["zone_hold_s"][target] = new
                else:
                    layer["hold_s"] = new
                changed = True
                lr.withdraw(key, set())
                lr.note("hold", title, detail, state="applied", key=key, data=data, auto=True, now=now)
            else:
                lr.propose("hold", key, title, detail, data, now)

        if changed:
            self._set_learned(room, layer)

        # Zones.
        keep = set()
        for z in result["zones"]:
            key = f"zone:{z['key']}"
            keep.add(key)
            label = (z["furniture_name"] or FURNITURE_LABELS.get(z["furniture_kind"] or "", "")) or None
            lr.propose("zone", key, f"Zone vorschlagen: {label or where(room, z['x'], z['y'])}",
                       f"Hier hielten sich Menschen {_seconds(z['seen_s'])} lang auf, in {z['stays']} Aufenthalten. "
                       "Eine Zone dort meldet Home Assistant, ob jemand da sitzt.",
                       {"zone": z, "name": label or "Lieblingsplatz"}, now)
        lr.withdraw("zone:", keep)

        if changed and self._refresh is not None:
            await self._refresh()
        return False

    @staticmethod
    def _declined_turn(lr: _Room, placement: dict) -> bool:
        return any(learning._angle_diff(placement["angle"], a) <= 20 and placement["mirror"] == m
                   for a, m in lr.declined.get("turns", []))

    # --- what the user says -------------------------------------------------------

    async def act(self, room_id: str, entry_id: str, action: str) -> dict:
        """`accept` a proposal, `decline` it, or `undo` what was taken over."""
        room = rooms.get_room(room_id)
        if room is None:
            raise LookupError("Raum nicht gefunden")
        lr = self._for(room)
        entry = lr.entry(entry_id) if lr is not None else None
        if entry is None:
            raise LookupError("Diesen Eintrag gibt es nicht mehr")
        kind = entry["kind"]
        if action == "decline":
            if entry["state"] != "open":
                raise ValueError("Nur ein Vorschlag lässt sich ablehnen")
            self._remember_declined(lr, entry)
            entry["state"] = "declined"
        elif action == "accept":
            if entry["state"] != "open":
                raise ValueError("Dieser Vorschlag ist schon erledigt")
            done = await self._accept(room, entry)
            entry["state"] = "applied" if done else "done"
        elif action == "undo":
            if entry["state"] != "applied" or kind not in ("spots", "hold", "placement"):
                raise ValueError("Das lässt sich nicht zurücknehmen")
            await self._undo(room, entry)
            self._remember_declined(lr, entry)
            entry["state"] = "undone"
        else:
            raise ValueError("Unbekannte Aktion")
        entry["acted_at"] = time.time()
        lr.dirty = True
        self.save(room_id, force=True)
        if self._refresh is not None:
            await self._refresh()
        return entry

    def _remember_declined(self, lr: _Room, entry: dict) -> None:
        data, kind = entry["data"], entry["kind"]
        if kind == "spots":
            # What would have been added stays out.
            for s in data.get("after") or []:
                if not any(_same_spot(s, b) for b in data.get("before") or []):
                    lr.declined["spots"].append([s["x"], s["y"]])
        elif kind == "spot":
            lr.declined["spots"].append([data["spot"]["x"], data["spot"]["y"]])
        elif kind == "hold":
            lr.declined["holds"].append(data["target"])
        elif kind == "placement":
            target = data.get("after") or data.get("placement")
            if target:
                lr.declined.setdefault("turns", []).append([target["angle"], target["mirror"]])
        elif entry["key"]:
            lr.declined["keys"].append(entry["key"])

    async def _accept(self, room: rooms.Room, entry: dict) -> bool:
        """Take a proposal over. False when there is nothing to take over —
        a hint, done by the user."""
        data, kind = entry["data"], entry["kind"]
        layer = self._layer(room)
        if kind == "spots":
            layer["spots"] = data["after"]
            return self._set_learned(room, layer) is not None
        if kind == "spot":
            if len(layer["spots"]) >= rooms.MAX_INTERFERENCE_SPOTS:
                raise ValueError("Schon so viele Störquellen, wie ein Raum haben kann")
            data["before"] = list(layer["spots"])
            layer["spots"] = layer["spots"] + [data["spot"]]
            data["after"] = layer["spots"]
            entry["kind"] = "spots"
            return self._set_learned(room, layer) is not None
        if kind == "hold":
            if data["target"] == "room":
                layer["hold_s"] = data["after"]
            elif data["after"] is None:
                layer["zone_hold_s"].pop(data["target"], None)
            else:
                layer["zone_hold_s"][data["target"]] = data["after"]
            return self._set_learned(room, layer) is not None
        if kind == "placement":
            if data.get("verdict") not in ("turn", "mirror", "ambiguous") or not data.get("placement"):
                return False
            target = data["placement"]
            try:
                turned = rooms.turn_sensor(room.id, expect=data["before"], angle=target["angle"],
                                           mirror=target["mirror"])
            except rooms.CalibrationConflict as err:
                raise ValueError("Der Sensor wurde inzwischen anders eingezeichnet — der Vorschlag gilt nicht "
                                 "mehr") from err
            data["after"] = {k: target[k] for k in ("x", "y", "angle", "mirror")}
            data["record_id"] = turned[1] if turned else None
            return turned is not None
        if kind == "zone":
            z = data["zone"]
            taken = {zone.name.strip().lower() for zone in room.zones}
            name = data.get("name") or "Lieblingsplatz"
            base, n = name, 2
            while name.strip().lower() in taken:
                name, n = f"{base} {n}", n + 1
            zone = {"id": rooms.new_id("z"), "name": name[:40], "kind": "detect", "points": z["points"],
                    "hold_s": 30.0, "color": len(room.zones) % 8}
            if z.get("furniture_id"):
                zone.update(furniture_id=z["furniture_id"], margin_m=0.2)
            if rooms.add_zone(room.id, zone) is None:
                raise LookupError("Raum nicht gefunden")
            return False
        return False

    async def _undo(self, room: rooms.Room, entry: dict) -> None:
        data, kind = entry["data"], entry["kind"]
        layer = self._layer(room)
        if kind == "spots":
            layer["spots"] = data["before"]
            self._set_learned(room, layer)
        elif kind == "hold":
            if data["target"] == "room":
                layer["hold_s"] = data["before"]
            elif data["before"] is None:
                layer["zone_hold_s"].pop(data["target"], None)
            else:
                layer["zone_hold_s"][data["target"]] = data["before"]
            self._set_learned(room, layer)
        elif kind == "placement":
            before, after = data["before"], data["after"]
            try:
                rooms.turn_sensor(room.id, expect=after, angle=before["angle"], mirror=before["mirror"])
            except rooms.CalibrationConflict as err:
                raise ValueError("Der Sensor wurde seitdem anders eingezeichnet — zurück geht es über den "
                                 "Verlauf der Ausrichtung") from err

    async def reset(self, room_id: str) -> None:
        """Forget everything learned about a room, and what was taken over."""
        room = rooms.get_room(room_id)
        if room is None:
            raise LookupError("Raum nicht gefunden")
        if room.learned is not None:
            self._set_learned(room, None)
        self._rooms.pop(room_id, None)
        (_dir() / f"{room_id}.json").unlink(missing_ok=True)
        lr = self._for(room)
        if lr is not None:
            lr.journal = []
            lr.note("info", "Neu begonnen", "Alles Gelernte ist vergessen; Echolot lernt den Raum neu kennen.")
            self.save(room_id, force=True)
        if self._refresh is not None:
            await self._refresh()

    # --- what the pages show ---------------------------------------------------------

    def summary(self, room: rooms.Room) -> dict:
        lr = self._rooms.get(room.id)
        if lr is None or not learning.usable(lr.state, room.sensor.device_id, room.calibration.mounting_epoch):
            return {"active": False}
        st = lr.state
        verdict = (lr.analysis or {}).get("verdict") or {}
        return {
            "active": self.settings.mode != "off",
            "since": st["started_at"],
            "observed_s": round(st["observed_s"]),
            "walks": st["walks"],
            "stays": len(learning.person_stays(st)),
            "proposals": sum(1 for e in lr.journal if e["state"] == "open"),
            "verdict": verdict.get("verdict"),
            "inside": verdict.get("inside"),
        }

    def view(self, room: rooms.Room) -> dict:
        lr = self._rooms.get(room.id)
        if lr is None and room.sensor.device_id:
            lr = self._for(room)
        base = {"mode": self.settings.mode, "presence": self.presence.view()}
        if lr is None:
            return {**base, "active": False, "reason": "no_sensor"}
        st = lr.state
        analysis = lr.analysis or {}
        placement = room.sensor.model_dump()
        spots = [s.model_dump() for s in rooms.active_interference(room)]
        layer = self._layer(room)
        moves = len(st.get("moves", []))
        chunks = st.get("away_chunks", [])
        open_chunk = st.get("away_open") or {}
        learned_spots = [{**s, "room": [round(v, 2) for v in geometry.to_room(s["x"], s["y"], placement)],
                          "where": where(room, *geometry.to_room(s["x"], s["y"], placement))}
                         for s in layer["spots"]]
        return {
            **base,
            "active": self.settings.mode != "off",
            "since": st["started_at"],
            "updated_at": st["updated_at"],
            "observed_s": round(st["observed_s"]),
            "walks": st["walks"],
            "tracks": st["tracks"],
            "stays": len(learning.person_stays(st, spots=spots)),
            "away": {"seconds": round(sum(c["s"] for c in chunks)), "chunks": len(chunks),
                     "collecting": bool(open_chunk) and not open_chunk.get("tainted"),
                     "collecting_s": round(open_chunk.get("s", 0)) if open_chunk else 0,
                     "tainted": st.get("away_tainted", 0), "motion": st.get("away_motion")},
            "needs": {"walks": learning.MIN_WALKS, "points": learning.MIN_PLAUSIBILITY_POINTS, "points_now": moves,
                      "away_s": learning.MIN_AWAY_S, "chunks": learning.MIN_AWAY_CHUNKS},
            "analysed_at": analysis.get("at"),
            "plausibility": analysis.get("plausibility"),
            "verdict": analysis.get("verdict"),
            "holds": analysis.get("holds"),
            "learned": {"spots": learned_spots, "hold_s": layer["hold_s"], "zone_hold_s": layer["zone_hold_s"]},
            "proposals": [e for e in lr.journal if e["state"] == "open"],
            "journal": [e for e in lr.journal if e["state"] != "open"],
            "activity": learning.activity_map(st, room, placement, spots=spots),
            "places": learning.favourite_places(st, room, placement, spots=spots),
        }

    # --- running -------------------------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                if self._clock() - self._analysed_at >= ANALYSE_EVERY_S:
                    await self.analyse_all()
                await asyncio.to_thread(self._write, self._due(None, False))
            except Exception:  # noqa: BLE001 - learning must never stop the add-on
                logger.exception("Lernrunde fehlgeschlagen")

    def start(self, refresh) -> None:
        self._refresh = refresh
        if self._task is None:
            self._analysed_at = self._clock()
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.flush()


def _same_spot(a: dict, b: dict) -> bool:
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"]) < SPOT_SAME_M and abs(a["r"] - b["r"]) < 0.15


def _spots_differ(before: list, after: list) -> bool:
    if len(before) != len(after):
        return True
    return not all(any(_same_spot(a, b) for b in before) for a in after)


def _placement_text(kind: str, verdict: dict, placement: dict) -> tuple[str, str] | None:
    inside = verdict.get("inside")
    share = f"{round(inside * 100)} %" if inside is not None else "ein Teil"
    target = verdict.get("placement") or {}
    if kind == "turn":
        return (f"Sensor schaut vermutlich nach {round(target['angle'])}° statt {round(placement['angle'])}°",
                f"Nur {share} der Wege liegen innerhalb der Wände; so wären es {round(target['inside'] * 100)} %. "
                "Selbst übernommen wird das nur, wenn Sitzmöbel oder eine Tür auf dem Plan es bestätigen — "
                "zeichne sie ein, oder übernimm es hier.")
    if kind == "mirror":
        return ("Links und rechts des Sensors scheinen vertauscht",
                f"Nur {share} der Wege liegen innerhalb der Wände; mit getauschten Seiten wären es "
                f"{round(target['inside'] * 100)} %. Ein Sensor, der in einer anderen Ecke hängt als eingezeichnet, "
                "kann genauso aussehen — der Gang-Test unter „Kalibrieren“ klärt es in einer Minute.")
    if kind == "ambiguous":
        return ("Blickrichtung des Sensors prüfen",
                f"Nur {share} der Wege liegen innerhalb der Wände. Sie passen zu einer anderen Blickrichtung — aber "
                "gespiegelt genauso gut. Der Gang-Test unter „Kalibrieren“ klärt, was stimmt; danach korrigiert "
                "Echolot die Richtung selbst.")
    if kind == "elsewhere":
        return ("Hängt der Sensor woanders?",
                f"Nur {share} der Wege liegen innerhalb der Wände. Sie passen viel besser zu einem Sensor bei "
                f"{_metres(target['x'])} / {_metres(target['y'])}, Blick {round(target['angle'])}°. Bitte die Position "
                "im Editor prüfen.")
    if kind == "unclear":
        return ("Viele Wege liegen außerhalb der Wände",
                f"Nur {share} der Wege liegen innerhalb der Wände, und keine andere Lage erklärt das. Oft sieht das "
                "Radar durch eine dünne Wand in den Nachbarraum — dann hilft, seine Reichweite unter „Kalibrieren → "
                "Filter“ auf den Raum zu begrenzen.")
    return None
