"""What Echolot learns about a room from what happens in it every day.

The radar gives positions and nothing else. What a room teaches over
days is in how those positions behave:

  * People walk. Their paths stay inside the walls and begin and end at
    the doors. A plan that puts them through a wall is wrong — and the
    paths say how it is wrong (`fit_placement`).
  * People sit. The module loses somebody sitting still and finds them
    again in the same place, again and again; the gaps in between say
    how long a zone has to wait before it may call itself empty
    (`hold_recommendations`). Where they sit is where a zone belongs
    (`zone_suggestions`).
  * Reflectors stay. While Home Assistant says nobody is home and
    nothing walks anywhere, whatever stands in the room again and again
    is a reflector (`find_interference`).

This module only watches and works things out. It keeps, per room, a
bounded record in the sensor's own metres — so it stays valid when the
sensor is moved or turned on the plan — and belongs to one sensor in
one mounting: another sensor or a remount starts it afresh, because the
module's coordinates mean something else then. What is done with the
results — proposed, or taken over — is app/learner.py's business.

Times are wall-clock seconds; the room engine's are monotonic and are
converted on the way in.
"""

import math
from collections import deque
from dataclasses import dataclass, field

from app import geometry
from app.tracking import TRACK_TTL_S

#: Format of the stored record. A different one is started afresh.
STATE_VERSION = 1

#: Grid of the maps, metres, in the sensor's own frame.
GRID_M = 0.25
#: A path is kept as one point per this many seconds.
SAMPLE_S = 0.5
#: A track, or the end of one, that stays within this of where it began
#: is somebody (or something) standing still...
STILL_M = 0.5
#: ... and a still end of a walk counts as arriving or leaving from this long.
STILL_MIN_S = 10.0
#: A still track shorter than this is a flash, not a piece of a stay.
MIN_PIECE_S = 2.0
#: A stay seen for less than this in total is not kept.
MIN_STAY_S = 10.0
#: A confirmed track whose positions span more than this walked.
MOVE_MIN_M = 1.0
#: Still pieces in one place, this close in time, are one stay: the
#: module losing somebody who sits and finding them again.
STAY_GAP_S = 900.0
STAY_SPOT_M = 0.5
#: A walk ending this close to a stay, this soon before it goes on, is
#: somebody arriving there; one starting this close, this soon after, is
#: somebody leaving.
ANCHOR_M = 0.5
ANCHOR_S = 30.0
#: Nobody-home time is kept in pieces of at most this long, and a piece
#: shorter than MIN_CHUNK_S is not kept.
AWAY_CHUNK_S = 4 * 3600.0
MIN_CHUNK_S = 300.0
#: Bounds of what is kept.
MAX_MOVES = 6000
MAX_ENDS = 800
MAX_STAYS = 600
MAX_GAPS_PER_STAY = 200
MAX_SAMPLES_PER_TRACK = 600
MAX_AWAY_CHUNKS = 40
MAX_GRID_CELLS = 4000
#: Reports further apart than this are a stall, not observation time.
MAX_STEP_S = 1.0


def cell_of(x: float, y: float) -> str:
    return f"{math.floor(x / GRID_M)},{math.floor(y / GRID_M)}"


def cell_centre(key: str) -> tuple[float, float]:
    ix, iy = (int(v) for v in key.split(","))
    return (ix + 0.5) * GRID_M, (iy + 0.5) * GRID_M


def empty_state(device_id: str | None, epoch: int, now: float) -> dict:
    return {
        "version": STATE_VERSION,
        "device_id": device_id,
        "epoch": epoch,
        "started_at": now,
        "updated_at": now,
        #: Seconds of reports seen at all.
        "observed_s": 0.0,
        #: Seconds a confirmed target was in each cell.
        "activity_grid": {},
        #: Points along confirmed walking tracks: [t, x, y].
        "moves": [],
        #: Where walking tracks appeared [t, x, y, 0] or vanished [t, x, y, 1]
        #: other than by standing still there.
        "ends": [],
        #: Closed stays: {"c": [x, y], "start", "end", "seen_s", "gaps": [s, ...],
        #: "arrivals", "departures", "away_s"}.
        "stays": [],
        #: Nobody home, nothing walking: {"start", "end", "s", "grid": {cell: s}}.
        "away_chunks": [],
        #: The one being collected, if any: as above plus "tainted".
        "away_open": None,
        #: Nobody-home pieces given up because something walked.
        "away_tainted": 0,
        #: Targets that walked while nobody was home: a pet, a robot vacuum —
        #: or somebody Home Assistant does not know about.
        "away_motion": {"count": 0, "last_at": None, "last": None},
        #: Confirmed tracks seen to the end.
        "tracks": 0,
        "walks": 0,
    }


def usable(state: dict | None, device_id: str | None, epoch: int) -> bool:
    return (
        isinstance(state, dict)
        and state.get("version") == STATE_VERSION
        and state.get("device_id") == device_id
        and state.get("epoch") == epoch
    )


def away_seconds(state: dict) -> float:
    return sum(c["s"] for c in state.get("away_chunks", []))


@dataclass
class _Live:
    """One track while it lives."""

    born_t: float
    last_t: float
    born: tuple[float, float]
    last: tuple[float, float]
    samples: list = field(default_factory=list)
    confirmed: bool = False
    away: bool = False
    #: Walked while nobody was home (counted once).
    away_moved: bool = False


@dataclass
class _Open:
    """A stay that may still go on."""

    #: Where it began — where somebody sat down. It does not move with
    #: later pieces: a reflector next to a seat would otherwise draw the
    #: stay over to itself, piece by piece.
    c: tuple[float, float]
    start: float
    end: float
    seen_s: float
    gaps: list = field(default_factory=list)
    arrivals: int = 0
    departures: int = 0
    away_s: float = 0.0
    pieces: int = 1


def _mean(samples) -> tuple[float, float]:
    n = len(samples)
    return sum(s[1] for s in samples) / n, sum(s[2] for s in samples) / n


def _still_run(samples: list, from_end: bool) -> int:
    """How many samples at one end of a path stay within STILL_M of the
    point there."""
    if not samples:
        return 0
    ordered = samples[::-1] if from_end else samples
    anchor = ordered[0]
    n = 0
    for _t, x, y in ordered:
        if math.hypot(x - anchor[1], y - anchor[2]) > STILL_M:
            break
        n += 1
    return n


def _add(grid: dict, key: str, seconds: float) -> None:
    if key in grid or len(grid) < MAX_GRID_CELLS:
        grid[key] = grid.get(key, 0.0) + seconds


class Observer:
    """Watches one room's tracker and keeps its record (`state`).

    Fed once per evaluation with the tracker's tracks; cheap per call.
    The record changes in place and is the caller's to store.
    """

    def __init__(self, state: dict):
        self.state = state
        self._live: dict[int, _Live] = {}
        self._open: list[_Open] = []
        self._last_t: float | None = None
        #: Recent walking ends (t, x, y), to tell arrivals.
        self._walk_deaths: deque = deque(maxlen=50)

    # --- feeding ----------------------------------------------------------

    def observe(self, tracks, now: float, away: bool | None) -> bool:
        """`tracks`: the tracker's current tracks (sensor metres); `away`:
        True while nobody is home (Home Assistant says so, for long
        enough), None if that is not known.

        Returns True if a confirmed target started walking while nobody
        was home — the caller taints the other rooms too.
        """
        st = self.state
        step = 0.0 if self._last_t is None else min(max(now - self._last_t, 0.0), MAX_STEP_S)
        self._last_t = now
        st["observed_s"] += step
        chunk = self._chunk(now, away)
        moved_away = False
        alive = set()
        for t in tracks:
            alive.add(t.id)
            live = self._live.get(t.id)
            if live is None:
                live = self._live[t.id] = _Live(born_t=now, last_t=now, born=(t.x, t.y), last=(t.x, t.y),
                                                 samples=[(now, t.x, t.y)])
            if not t.seen:
                continue
            live.last_t = now
            live.last = (t.x, t.y)
            live.confirmed = live.confirmed or t.confirmed
            if now - live.samples[-1][0] >= SAMPLE_S and len(live.samples) < MAX_SAMPLES_PER_TRACK:
                live.samples.append((now, t.x, t.y))
            key = cell_of(t.x, t.y)
            if away:
                live.away = True
                if (not live.away_moved and live.confirmed
                        and math.hypot(t.x - live.born[0], t.y - live.born[1]) > MOVE_MIN_M):
                    live.away_moved = True
                    moved_away = True
                    motion = st["away_motion"]
                    motion["count"] += 1
                    motion["last_at"] = now
                    motion["last"] = [round(t.x, 2), round(t.y, 2)]
            if step:
                if chunk is not None and not chunk["tainted"]:
                    _add(chunk["grid"], key, step)
                if t.confirmed:
                    _add(st["activity_grid"], key, step)
        if chunk is not None and step and not chunk["tainted"]:
            chunk["s"] += step
        if moved_away:
            self.taint()
        for track_id in [k for k in self._live if k not in alive]:
            self._finish(self._live.pop(track_id), now, away)
        self._close_stays(now)
        st["updated_at"] = now
        return moved_away

    def taint(self) -> None:
        """Something walked while nobody was home: what stood in the room
        meanwhile may have been somebody. The rest of this nobody-home
        stretch teaches nothing about reflectors."""
        chunk = self.state.get("away_open")
        if chunk is not None:
            chunk["tainted"] = True

    def pause(self, now: float) -> None:
        """The sensor went away: whatever it followed is finished, and the
        time until it is back is no observation."""
        for live in self._live.values():
            self._finish(live, now, None)
        self._live.clear()
        self._last_t = None

    def flush(self, now: float) -> None:
        """Finish everything still open — before storing for good."""
        self.pause(now)
        self._close_stays(now, force=True)
        self._close_chunk(now)

    # --- nobody home --------------------------------------------------------

    def _chunk(self, now: float, away: bool | None) -> dict | None:
        st = self.state
        chunk = st.get("away_open")
        if not away:
            if chunk is not None:
                self._close_chunk(now)
            return None
        if chunk is not None and chunk["s"] >= AWAY_CHUNK_S:
            tainted = chunk["tainted"]
            self._close_chunk(now)
            chunk = None
            if tainted:
                st["away_open"] = chunk = {"start": now, "s": 0.0, "grid": {}, "tainted": True}
        if chunk is None:
            st["away_open"] = chunk = {"start": now, "s": 0.0, "grid": {}, "tainted": False}
        return chunk

    def _close_chunk(self, now: float) -> None:
        st = self.state
        chunk = st.get("away_open")
        st["away_open"] = None
        if chunk is None:
            return
        if chunk["tainted"]:
            st["away_tainted"] += 1
            return
        if chunk["s"] < MIN_CHUNK_S:
            return
        st["away_chunks"].append({
            "start": round(chunk["start"]), "end": round(now), "s": round(chunk["s"], 1),
            "grid": {k: round(v, 1) for k, v in chunk["grid"].items() if v >= 0.5},
        })
        if len(st["away_chunks"]) > MAX_AWAY_CHUNKS:
            del st["away_chunks"][: len(st["away_chunks"]) - MAX_AWAY_CHUNKS]

    # --- what a finished track was ------------------------------------------

    def _finish(self, live: _Live, now: float, away: bool | None) -> None:
        st = self.state
        samples = live.samples
        if samples[-1][0] < live.last_t:
            samples = samples + [(live.last_t, *live.last)]
        span = max(math.hypot(x - live.born[0], y - live.born[1]) for _t, x, y in samples)
        duration = live.last_t - live.born_t
        if live.confirmed:
            st["tracks"] += 1
        if span <= STILL_M:
            if duration >= MIN_PIECE_S:
                self._piece(_mean(samples), live.born_t, live.last_t, arrived=False, departed=False,
                            away=bool(live.away))
            return
        # It moved. The still ends, if long enough, are where somebody sat;
        # the rest is a walk.
        tail = _still_run(samples, from_end=True)
        head = _still_run(samples, from_end=False)
        tail_s = samples[-1][0] - samples[-tail][0] if tail else 0.0
        head_s = samples[head - 1][0] - samples[0][0] if head else 0.0
        arrived = tail_s >= STILL_MIN_S
        departed = head_s >= STILL_MIN_S
        if departed:
            self._piece(_mean(samples[:head]), samples[0][0], samples[head - 1][0], arrived=False, departed=True,
                        away=bool(live.away))
        if arrived:
            self._piece(_mean(samples[-tail:]), samples[-tail][0], samples[-1][0], arrived=True, departed=False,
                        away=bool(live.away))
        if not live.confirmed or span < MOVE_MIN_M:
            return
        st["walks"] += 1
        walk = samples[head if departed else 0: len(samples) - (tail if arrived else 0)]
        moves = st["moves"]
        for t, x, y in walk:
            moves.append([round(t), round(x, 2), round(y, 2)])
        if len(moves) > MAX_MOVES:
            del moves[: len(moves) - MAX_MOVES]
        ends = st["ends"]
        if not departed:
            ends.append([round(live.born_t), round(live.born[0], 2), round(live.born[1], 2), 0])
            self._left_from(live.born_t, live.born)
        if not arrived:
            ends.append([round(live.last_t), round(live.last[0], 2), round(live.last[1], 2), 1])
            self._walk_deaths.append((live.last_t, live.last[0], live.last[1]))
        if len(ends) > MAX_ENDS:
            del ends[: len(ends) - MAX_ENDS]

    def _left_from(self, t: float, at: tuple) -> None:
        """A walk starting next to a stay that just went quiet: somebody
        got up, and the stay is over."""
        keep = []
        for stay in self._open:
            if 0 <= t - stay.end <= ANCHOR_S and math.hypot(at[0] - stay.c[0], at[1] - stay.c[1]) <= ANCHOR_M:
                stay.departures += 1
                self._store_stay(stay)
            else:
                keep.append(stay)
        self._open = keep

    def _piece(self, c, start, end, *, arrived, departed, away=False) -> None:
        arrived = arrived or any(
            0 <= start - t <= ANCHOR_S and math.hypot(x - c[0], y - c[1]) <= ANCHOR_M
            for t, x, y in self._walk_deaths
        )
        seen = max(0.0, end - start)
        near = [s for s in self._open
                if math.hypot(c[0] - s.c[0], c[1] - s.c[1]) <= STAY_SPOT_M and start - s.end <= STAY_GAP_S]
        stay = min(near, key=lambda s: math.hypot(c[0] - s.c[0], c[1] - s.c[1])) if near else None
        if stay is not None and arrived and not stay.arrivals:
            # Somebody walks up to where only something unaccounted for has
            # stood: theirs is a stay of its own.
            stay = None
        if stay is not None:
            # A gap the module made — not one somebody came back after,
            # unseen on the way out.
            if start > stay.end and not arrived and len(stay.gaps) < MAX_GAPS_PER_STAY:
                stay.gaps.append(round(start - stay.end, 1))
            stay.pieces += 1
            stay.end = max(stay.end, end)
            stay.seen_s += seen
            stay.arrivals += int(arrived)
            stay.departures += int(departed)
            stay.away_s += seen if away else 0.0
        else:
            stay = _Open(c=c, start=start, end=end, seen_s=seen, arrivals=int(arrived),
                         departures=int(departed), away_s=seen if away else 0.0)
            self._open.append(stay)
        if departed:
            # Somebody got up and walked off: the stay is over.
            self._open.remove(stay)
            self._store_stay(stay)

    def _close_stays(self, now: float, force: bool = False) -> None:
        keep = []
        for stay in self._open:
            if force or now - stay.end > STAY_GAP_S:
                self._store_stay(stay)
            else:
                keep.append(stay)
        self._open = keep

    def _store_stay(self, stay: _Open) -> None:
        if stay.seen_s < MIN_STAY_S:
            return
        stays = self.state["stays"]
        stays.append({
            "c": [round(stay.c[0], 2), round(stay.c[1], 2)],
            "start": round(stay.start), "end": round(stay.end),
            "seen_s": round(stay.seen_s, 1), "gaps": stay.gaps,
            "arrivals": stay.arrivals, "departures": stay.departures,
            "away_s": round(stay.away_s, 1),
        })
        if len(stays) > MAX_STAYS:
            del stays[: len(stays) - MAX_STAYS]


# --- the room, as the learners see it ------------------------------------------

#: Furniture somebody sits or lies on.
SEATS = ("sofa", "armchair", "bed", "chair", "desk", "table")
#: Furniture and zones a person comes in through.
DOORS = ("door",)


def walls_of(room) -> list:
    return [tuple(p) for p in room.outline] if room.outline else [
        (0.0, 0.0), (room.width, 0.0), (room.width, room.height), (0.0, room.height)]


def _seat_outlines(room, margin: float) -> list:
    return [geometry.furniture_outline(f.x, f.y, f.w, f.h, f.angle, margin) for f in room.furniture if f.kind in SEATS]


def _door_outlines(room, margin: float) -> list:
    shapes = [geometry.furniture_outline(f.x, f.y, f.w, f.h, f.angle, margin) for f in room.furniture if f.kind in DOORS]
    shapes += [list(z.points) for z in room.zones if z.kind == "entry"]
    return shapes


def _near_any(x: float, y: float, shapes: list, margin: float) -> bool:
    return any(geometry.point_in_polygon(x, y, s) or geometry.distance_to_polygon(x, y, s) <= margin for s in shapes)


def _spot_xyr(spot) -> tuple[float, float, float]:
    if isinstance(spot, dict):
        return float(spot["x"]), float(spot["y"]), float(spot["r"])
    return float(spot.x), float(spot.y), float(spot.r)


def in_spots(x: float, y: float, spots) -> bool:
    """Within a reflector's spot ({x, y, r} in sensor metres, or
    rooms.InterferenceSpot)."""
    return any(math.hypot(x - sx, y - sy) <= r for sx, sy, r in map(_spot_xyr, spots))


def person_stays(state: dict, min_seen_s: float = 60.0, spots=()) -> list[dict]:
    """Stays somebody walked into: a person's, not a reflector's — and,
    once a reflector is known, none in its spot: something walking past
    one now and then looks like somebody arriving there."""
    return [s for s in state.get("stays", [])
            if s.get("arrivals", 0) >= 1 and s["seen_s"] >= min_seen_s and not in_spots(s["c"][0], s["c"][1], spots)]


# --- reflectors -------------------------------------------------------------------

#: Nobody home for this long in total before anything is called a reflector...
MIN_AWAY_S = 1800.0
#: ... in at least this many separate stretches...
MIN_AWAY_CHUNKS = 2
#: ... and a place must have had a target in at least this share of the
#: time — the empty-room recording's threshold (calibration.MIN_SPOT_SHARE)
#: — overall and in each of those stretches.
MIN_AWAY_SHARE = 0.03
SPOT_RADIUS_M = (0.3, 1.0)
#: People sitting at a place this long in total, in this many stays, make
#: it a person's place: a reflector there is proposed, never taken over.
PERSON_PLACE_S = 1200.0
PERSON_PLACE_STAYS = 3


def find_interference(state: dict, room=None, placement: dict | None = None) -> list[dict]:
    """Reflectors, from what stood in the room while nobody was home and
    nothing walked: [{x, y, r, share, chunks, auto, reason}] in the
    sensor's metres, strongest first.

    `auto` says whether one may be taken over without asking: seen in at
    least MIN_AWAY_CHUNKS separate stretches, and not where people sit —
    neither on a seat drawn on the plan (`room`, `placement`) nor where
    people were seen to walk in and stay. `reason` says why not.
    """
    chunks = [c for c in state.get("away_chunks", []) if c["s"] >= MIN_CHUNK_S]
    away_s = sum(c["s"] for c in chunks)
    if away_s < MIN_AWAY_S:
        return []
    grid: dict[str, float] = {}
    for c in chunks:
        for k, v in c["grid"].items():
            grid[k] = grid.get(k, 0.0) + v
    cells = {k: v for k, v in grid.items() if v / away_s >= MIN_AWAY_SHARE / 4}
    seen: set[str] = set()
    seats = _seat_outlines(room, 0.0) if room is not None else []
    if room is not None and placement is None:
        placement = room.sensor.model_dump()
    people = person_stays(state)
    spots = []
    for key in sorted(cells, key=cells.get, reverse=True):
        if key in seen:
            continue
        # The cell and its neighbours, as far as they go.
        group, todo = [], [key]
        seen.add(key)
        while todo:
            k = todo.pop()
            group.append(k)
            ix, iy = (int(v) for v in k.split(","))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    n = f"{ix + dx},{iy + dy}"
                    if n in cells and n not in seen:
                        seen.add(n)
                        todo.append(n)
        total = sum(cells[k] for k in group)
        if total / away_s < MIN_AWAY_SHARE:
            continue
        cx = sum(cell_centre(k)[0] * cells[k] for k in group) / total
        cy = sum(cell_centre(k)[1] * cells[k] for k in group) / total
        spread = math.sqrt(sum(((cell_centre(k)[0] - cx) ** 2 + (cell_centre(k)[1] - cy) ** 2) * cells[k]
                               for k in group) / total)
        r = min(max(2 * spread + GRID_M / 2 + 0.1, SPOT_RADIUS_M[0]), SPOT_RADIUS_M[1])
        in_chunks = sum(
            1 for c in chunks
            if sum(c["grid"].get(k, 0.0) for k in group) / c["s"] >= MIN_AWAY_SHARE
        )
        reason = None
        if in_chunks < MIN_AWAY_CHUNKS:
            reason = "few_chunks"
        else:
            sat = [s for s in people if math.hypot(s["c"][0] - cx, s["c"][1] - cy) <= r + 0.2]
            if len(sat) >= PERSON_PLACE_STAYS and sum(s["seen_s"] for s in sat) >= PERSON_PLACE_S:
                reason = "people_sit"
            elif seats and placement is not None:
                rx, ry = geometry.to_room(cx, cy, placement)
                if _near_any(rx, ry, seats, r):
                    reason = "on_seat"
        spots.append({
            "x": round(cx, 2), "y": round(cy, 2), "r": round(r, 2),
            "share": round(min(1.0, total / away_s), 3), "chunks": in_chunks,
            "auto": reason is None, "reason": reason,
        })
    spots.sort(key=lambda s: s["share"], reverse=True)
    return spots


# --- does the plan fit what walks? ---------------------------------------------

#: Recent walking points the checks look at.
PLAUSIBILITY_POINTS = 2000
#: At least this many, from this many walks, before anything is said.
MIN_PLAUSIBILITY_POINTS = 200
MIN_WALKS = 8


class _Plan:
    """The room as lookup grids — the floor (edge margin included), the
    floor proper, the seats, the doors — fast enough to try a placement
    for every degree."""

    def __init__(self, room):
        walls = walls_of(room)
        self.walls = walls
        margin = room.edge_margin_m
        xs = [p[0] for p in walls]
        ys = [p[1] for p in walls]
        span = max(max(xs) - min(xs), max(ys) - min(ys))
        self.cell = cell = max(0.05, span / 200.0)
        pad = max(margin, DOOR_REACH_M) + cell
        self.x0, self.y0 = min(xs) - pad, min(ys) - pad
        self.nx = int((max(xs) + pad - self.x0) / cell) + 1
        self.ny = int((max(ys) + pad - self.y0) / cell) + 1
        self.inv = 1.0 / cell
        self.strict = self._layer(lambda x, y: geometry.point_in_polygon(x, y, walls))
        self.inside = self._layer(lambda x, y: geometry.point_in_polygon(x, y, walls)
                                  or geometry.distance_to_polygon(x, y, walls) <= margin)
        seats = _seat_outlines(room, 0.0)
        doors = _door_outlines(room, 0.0)
        self.seat = self._layer(lambda x, y: _near_any(x, y, seats, SEAT_REACH_M)) if seats else None
        self.door = self._layer(lambda x, y: _near_any(x, y, doors, DOOR_REACH_M)) if doors else None

    def _layer(self, test) -> bytearray:
        grid = bytearray(self.nx * self.ny)
        for iy in range(self.ny):
            y = self.y0 + (iy + 0.5) * self.cell
            row = iy * self.nx
            for ix in range(self.nx):
                if test(self.x0 + (ix + 0.5) * self.cell, y):
                    grid[row + ix] = 1
        return grid

    def has(self, x: float, y: float, strict: bool = False) -> bool:
        ix = int((x - self.x0) * self.inv)
        iy = int((y - self.y0) * self.inv)
        if 0 <= ix < self.nx and 0 <= iy < self.ny:
            return bool((self.strict if strict else self.inside)[iy * self.nx + ix])
        return False

    def share(self, layer: bytearray, points, x: float, y: float, angle: float, mirror: bool) -> float:
        """Share of `points` (sensor metres, corrected) that a placement
        puts on `layer`."""
        if not points:
            return 0.0
        a = math.radians(angle)
        c, s = math.cos(a), math.sin(a)
        m = -1.0 if mirror else 1.0
        x0, y0, inv, nx, ny = self.x0, self.y0, self.inv, self.nx, self.ny
        n = 0
        for px, py in points:
            px *= m
            ix = int((x + px * c - py * s - x0) * inv)
            iy = int((y + px * s + py * c - y0) * inv)
            if 0 <= ix < nx and 0 <= iy < ny and layer[iy * nx + ix]:
                n += 1
        return n / len(points)


def _corrected(points, placement: dict) -> list[tuple[float, float]]:
    """Module positions through the room's sensor model, before mirror and
    turn — what the placement search moves around."""
    return [geometry.correct(x, y, placement) for x, y in points]


def _place(px: float, py: float, x: float, y: float, angle: float, mirror: bool) -> tuple[float, float]:
    a = math.radians(angle)
    c, s = math.cos(a), math.sin(a)
    if mirror:
        px = -px
    return x + px * c - py * s, y + px * s + py * c


def _recent_moves(state: dict) -> list:
    return state.get("moves", [])[-PLAUSIBILITY_POINTS:]


def _enough(state: dict) -> bool:
    return len(_recent_moves(state)) >= MIN_PLAUSIBILITY_POINTS and state.get("walks", 0) >= MIN_WALKS


def plausibility(state: dict, room, placement: dict | None = None) -> dict:
    """How many recent walking points the plan puts inside the walls.

    {points, walks, inside_share} — inside_share None while there is too
    little to say. Only walking counts: somebody sitting is one place, a
    walk crosses the room.
    """
    moves = _recent_moves(state)
    placement = placement if placement is not None else room.sensor.model_dump()
    walks = state.get("walks", 0)
    if not _enough(state):
        return {"points": len(moves), "inside_share": None, "walks": walks}
    plan = _Plan(room)
    points = _corrected([(m[1], m[2]) for m in moves], placement)
    share = plan.share(plan.inside, points, float(placement["x"]), float(placement["y"]),
                       float(placement.get("angle", 0.0)), bool(placement.get("mirror")))
    return {"points": len(moves), "inside_share": round(share, 3), "walks": walks}


#: How near a seat a stay, and a door the end of a walk, counts as there.
SEAT_REACH_M = 0.4
DOOR_REACH_M = 0.8
#: Weights of what besides the walls tells placements apart.
SEAT_WEIGHT = 0.25
DOOR_WEIGHT = 0.15
#: Scores this close are a tie: a few stays, two walking points in a
#: hundred. Closer than that is noise — one stay more or less on a seat
#: must not move the answer by ten degrees.
TIE = 0.02
FIT_POINTS = 600


class _Evidence:
    """What a placement is judged by, in the sensor's metres through the
    room's sensor model: the recent walks, the stays somebody walked into,
    the ends of walks."""

    def __init__(self, state: dict, room):
        current = room.sensor.model_dump()
        self.plan = _Plan(room)
        self.points = _even(_corrected([(m[1], m[2]) for m in _recent_moves(state)], current), FIT_POINTS)
        stays = [geometry.correct(s["c"][0], s["c"][1], current) for s in person_stays(state)][-200:]
        ends = [geometry.correct(e[1], e[2], current) for e in state.get("ends", [])][-400:]
        self.stays = stays if self.plan.seat is not None and len(stays) >= 3 else None
        self.ends = ends if self.plan.door is not None and len(ends) >= 10 else None

    def judge(self, x: float, y: float, angle: float, mirror: bool) -> dict:
        plan = self.plan
        inside = plan.share(plan.inside, self.points, x, y, angle, mirror)
        seat = plan.share(plan.seat, self.stays, x, y, angle, mirror) if self.stays else None
        door = plan.share(plan.door, self.ends, x, y, angle, mirror) if self.ends else None
        score = inside + SEAT_WEIGHT * (seat or 0.0) + DOOR_WEIGHT * (door or 0.0)
        return {"x": round(x, 2), "y": round(y, 2), "angle": round(_normal_angle(angle), 1), "mirror": mirror,
                "inside": round(inside, 4), "seat": None if seat is None else round(seat, 3),
                "door": None if door is None else round(door, 3), "score": round(score, 4)}


def _normal_angle(angle: float) -> float:
    """-180 < angle <= 180, as the plan shows it."""
    angle = angle % 360.0
    return angle - 360.0 if angle > 180.0 else angle


def _angle_diff(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _even(items: list, n: int) -> list:
    if len(items) <= n:
        return list(items)
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def _best_turn(rows: list[dict]) -> dict:
    """The best of 360 placements a degree apart: the middle of the
    stretch that scores best, and whether another stretch scores as well."""
    top = max(r["score"] for r in rows)
    good = [r["score"] >= top - TIE for r in rows]
    if all(good):
        return {**rows[0], "width": 360, "split": True}
    # Stretches of good degrees, around the circle.
    start = next(i for i in range(360) if not good[i])
    runs, run = [], []
    for k in range(1, 361):
        i = (start + k) % 360
        if good[i]:
            run.append(i)
        elif run:
            runs.append(run)
            run = []
    if run:
        runs.append(run)
    runs.sort(key=len, reverse=True)
    middle = runs[0][len(runs[0]) // 2]
    # Two stretches further apart than a slip of the hand: two answers.
    split = any(min(_angle_diff(i, j) for i in other for j in runs[0]) > 20 for other in runs[1:])
    return {**rows[middle], "width": len(runs[0]), "split": split}


def fit_turn(state: dict, room) -> dict | None:
    """Which way the sensor must look, from where the plan has it, for the
    everyday walks to stay inside the walls — and, where the plan has
    seats and doors, for the people who stay to land on the seats and the
    walks to end at the doors.

    Whoever hung the sensor knows which wall it is on; what is easily got
    wrong is which way it looks and which side is its left. Every degree,
    both mirror settings. Returns None while there is too little to go
    by, else {"current", "same", "flipped", "points"}: the placement as
    drawn, the best turn with the mirror setting as drawn and with it the
    other way — each {x, y, angle, mirror, inside, seat, door, score},
    the best two with `width` (degrees that score as well) and `split`
    (whether a second, different turn does).
    """
    if not _enough(state):
        return None
    ev = _Evidence(state, room)
    s = room.sensor
    x, y = float(s.x), float(s.y)
    out = {"current": ev.judge(x, y, float(s.angle), bool(s.mirror)), "points": len(ev.points)}
    for key, mirror in (("same", bool(s.mirror)), ("flipped", not s.mirror)):
        out[key] = _best_turn([ev.judge(x, y, a, mirror) for a in range(360)])
    return out


#: The search over every wall: positions this far apart (more for big
#: rooms), turns in these steps, both mirror settings.
FIT_STEP_M = 0.2
FIT_MAX_POSITIONS = 160
FIT_ANGLE_STEP = 5.0
FIT_COARSE_POINTS = 150
FIT_KEEP = 24
#: Two placements this far apart are different answers.
DIFFERENT_M = 0.6
DIFFERENT_DEG = 20.0


def _different(a: dict, b: dict) -> bool:
    return (a["mirror"] != b["mirror"] or math.hypot(a["x"] - b["x"], a["y"] - b["y"]) > DIFFERENT_M
            or _angle_diff(a["angle"], b["angle"]) > DIFFERENT_DEG)


def _wall_positions(walls: list) -> list[tuple[float, float]]:
    n = len(walls)
    perimeter = sum(math.hypot(walls[(i + 1) % n][0] - walls[i][0], walls[(i + 1) % n][1] - walls[i][1])
                    for i in range(n))
    step = max(FIT_STEP_M, perimeter / FIT_MAX_POSITIONS)
    out = []
    for i in range(n):
        (ax, ay), (bx, by) = walls[i], walls[(i + 1) % n]
        length = math.hypot(bx - ax, by - ay)
        k = max(1, int(length / step))
        for j in range(k):
            f = j / k
            out.append((ax + (bx - ax) * f, ay + (by - ay) * f))
    return out


def _ceiling_positions(plan: _Plan) -> list[tuple[float, float]]:
    xs = [p[0] for p in plan.walls]
    ys = [p[1] for p in plan.walls]
    step = max(0.5, max(max(xs) - min(xs), max(ys) - min(ys)) / 12)
    out = []
    y = min(ys) + step / 2
    while y < max(ys):
        x = min(xs) + step / 2
        while x < max(xs):
            if plan.has(x, y, strict=True):
                out.append((x, y))
            x += step
        y += step
    return out


def fit_anywhere(state: dict, room, mount_mode: str | None = None) -> dict | None:
    """The best placements anywhere on the walls (for a module mounted on
    top, anywhere on the ceiling): {"best", "runner_up", "points"} or None
    while there is too little to go by.

    Walks alone rarely tell places apart — a rectangle fits the same walks
    turned half round — so this is a hint that the sensor hangs somewhere
    else than drawn, never a correction.
    """
    if not _enough(state):
        return None
    ev = _Evidence(state, room)
    plan = ev.plan
    coarse = _even(ev.points, FIT_COARSE_POINTS)
    top = mount_mode == "top"
    positions = _ceiling_positions(plan) if top else _wall_positions(plan.walls)
    found = []
    for x, y in positions:
        for i in range(int(360 / FIT_ANGLE_STEP)):
            angle = i * FIT_ANGLE_STEP
            if not top:
                # Looking into the room: a metre ahead is on the floor.
                ax, ay = _place(0.0, 1.0, x, y, angle, False)
                if not plan.has(ax, ay, strict=True):
                    continue
            for mirror in (False, True):
                found.append((plan.share(plan.inside, coarse, x, y, angle, mirror), x, y, angle, mirror))
    if not found:
        return None
    found.sort(key=lambda c: c[0], reverse=True)
    picked: list[dict] = []
    for share, x, y, angle, mirror in found:
        cand = {"x": x, "y": y, "angle": angle, "mirror": mirror}
        if any(not _different(cand, p) for p in picked):
            continue
        picked.append(cand)
        if len(picked) >= FIT_KEEP or share < found[0][0] - 0.25:
            break
    judged = []
    for cand in picked:
        x, y, angle = _refine(cand, ev.points, plan)
        judged.append(ev.judge(x, y, angle, cand["mirror"]))
    judged.sort(key=lambda c: c["score"], reverse=True)
    best = judged[0]
    runner = next((c for c in judged[1:] if _different(c, best)), None)
    return {"best": best, "runner_up": runner, "points": len(ev.points)}


def _refine(cand: dict, points: list, plan: _Plan) -> tuple[float, float, float]:
    """Climb to the nearest best: small moves and turns while they help."""
    x, y, angle, mirror = cand["x"], cand["y"], cand["angle"], cand["mirror"]
    best = plan.share(plan.inside, points, x, y, angle, mirror)
    for step_m, step_deg in ((0.2, 4.0), (0.1, 2.0), (0.05, 1.0)):
        improved = True
        rounds = 0
        while improved and rounds < 20:
            improved = False
            rounds += 1
            for dx, dy, da in ((step_m, 0, 0), (-step_m, 0, 0), (0, step_m, 0), (0, -step_m, 0),
                               (0, 0, step_deg), (0, 0, -step_deg)):
                share = plan.share(plan.inside, points, x + dx, y + dy, angle + da, mirror)
                if share > best + 1e-9:
                    best, x, y, angle = share, x + dx, y + dy, angle + da
                    improved = True
    return x, y, angle


#: The plan fits when this share of the walks is inside the walls...
FITS = 0.9
#: ... is wrong when less is, and a turn that puts this much inside —
#: and scores this much better — is the answer.
WRONG = 0.8
FIXED = 0.95
BETTER = 0.15
#: A turn this much smaller is no correction worth making.
MIN_TURN_DEG = 10.0
#: Scores this much apart tell two answers apart.
CLEARLY = 0.05
#: A placement elsewhere this far away is another place.
ELSEWHERE_M = 1.0
#: What the seats and doors on the plan must say for a turn to be taken
#: over without asking: most stays on a seat, or many walks ending at a door.
SEATS_AGREE = 0.5
DOORS_AGREE = 0.3


def placement_verdict(turn: dict | None, anywhere: dict | None = None, mirror_known: bool = False) -> dict:
    """What the walks say about the plan: {"verdict", "inside",
    "placement", "auto"}.

    "unknown"    too little to go by yet;
    "fits"       the walks stay inside the walls;
    "turn"       the sensor looks another way than drawn; `placement` is
                 the way. `auto` when nothing else could explain the
                 walks: which side is left is settled (`mirror_known` —
                 the walk test said so — or the other way round clearly
                 fits worse) and the seats or doors on the plan agree.
                 A rectangle fits the same walks mirrored in a corner;
                 without seats or doors it is a proposal;
    "mirror"     the walks fit clearly better with left and right swapped
                 — proposed, to be confirmed by the walk test (app/axes.py):
                 a sensor drawn in the wrong corner can fit mirrored too;
    "ambiguous"  they fit as well either way round — the walk test tells;
    "elsewhere"  they fit a sensor somewhere else much better — the
                 position on the plan is to be checked;
    "unclear"    they leave the walls, and nothing above explains it:
                 walls the radar sees through, a range beyond the room.
    """
    if turn is None:
        return {"verdict": "unknown", "inside": None, "placement": None, "auto": False}
    cur, same, flipped = turn["current"], turn["same"], turn["flipped"]
    out = {"inside": cur["inside"], "placement": None, "auto": False}
    if cur["inside"] >= FITS:
        return {**out, "verdict": "fits"}
    here = same["score"] if mirror_known else max(same["score"], flipped["score"])
    if anywhere is not None:
        best = anywhere["best"]
        far = math.hypot(best["x"] - cur["x"], best["y"] - cur["y"]) > ELSEWHERE_M
        if far and best["score"] >= here + CLEARLY:
            return {**out, "verdict": "elsewhere", "placement": best}
    if cur["inside"] >= WRONG:
        return {**out, "verdict": "unclear"}
    if not mirror_known and flipped["inside"] >= FIXED and flipped["score"] >= same["score"] + CLEARLY:
        return {**out, "verdict": "mirror", "placement": flipped}
    good = (same["inside"] >= FIXED and not same["split"] and same["score"] >= cur["score"] + BETTER
            and _angle_diff(same["angle"], cur["angle"]) >= MIN_TURN_DEG)
    if not good:
        return {**out, "verdict": "unclear"}
    settled = mirror_known or flipped["score"] <= same["score"] - CLEARLY
    if not settled:
        return {**out, "verdict": "ambiguous", "placement": same}
    agree = ((same["seat"] is not None and same["seat"] >= SEATS_AGREE)
             or (same["door"] is not None and same["door"] >= DOORS_AGREE))
    return {**out, "verdict": "turn", "placement": same, "auto": agree}


# --- how long a zone must wait ------------------------------------------------------

#: A gap counts as the module losing somebody up to this long; longer,
#: they more likely left unseen.
MAX_DROPOUT_S = 300.0
#: Enough to go by: stays somebody walked into, and gaps within them.
MIN_HOLD_STAYS = 3
MIN_HOLD_GAPS = 20
#: Bounds of a learned hold time, seconds.
HOLD_BOUNDS = (5.0, 180.0)


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    i = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[i]


def _hold_for(stays: list[dict], confirm_s: float) -> dict | None:
    gaps = [g for s in stays for g in s.get("gaps", []) if g <= MAX_DROPOUT_S]
    if len(stays) < MIN_HOLD_STAYS or len(gaps) < MIN_HOLD_GAPS:
        return None
    q = _quantile(gaps, 0.99)
    # Bridging a gap of g takes a hold of g, less the time the tracker
    # still counts a lost target, plus the time it takes to count again.
    need = q - TRACK_TTL_S + confirm_s + 2.0
    hold = min(max(math.ceil(need / 5.0) * 5.0, HOLD_BOUNDS[0]), HOLD_BOUNDS[1])
    return {"hold_s": hold, "q99_s": round(q, 1), "longest_s": round(max(gaps), 1), "gaps": len(gaps),
            "stays": len(stays), "short": need > HOLD_BOUNDS[1]}


def hold_recommendations(state: dict, room, placement: dict | None = None, spots=()) -> dict:
    """How long the room and each detection zone must wait before calling
    themselves empty, so the module's lapses with somebody sitting there
    do not empty them: {"room": {...} | None, "zones": {zone_id: {...}}},
    each {hold_s, q99_s, longest_s, gaps, stays, short}. Stays in a
    reflector's spot (`spots`) do not count."""
    placement = placement if placement is not None else room.sensor.model_dump()
    confirm_s = room.calibration.confirm_s
    stays = []
    for s in person_stays(state, spots=spots):
        rx, ry = geometry.to_room(s["c"][0], s["c"][1], placement)
        if geometry.within_walls(rx, ry, room.width, room.height, room.outline, room.edge_margin_m):
            stays.append((rx, ry, s))
    out = {"room": _hold_for([s for _x, _y, s in stays], confirm_s), "zones": {}}
    for zone in room.zones:
        if zone.kind != "detect":
            continue
        inside = [s for x, y, s in stays if geometry.point_in_polygon(x, y, zone.points)]
        rec = _hold_for(inside, confirm_s)
        if rec is not None:
            out["zones"][zone.id] = rec
    return out


# --- where zones belong -------------------------------------------------------------

#: A place counts as somebody's from this much time in this many stays.
PLACE_S = 1800.0
PLACE_STAYS = 3
PLACE_M = 0.8


def favourite_places(state: dict, room, placement: dict | None = None, spots=()) -> list[dict]:
    """Where people stay, in room metres: [{x, y, seen_s, stays}], the
    most-used first. Stays somebody walked into, grouped by place."""
    placement = placement if placement is not None else room.sensor.model_dump()
    groups: list[dict] = []
    for s in sorted(person_stays(state, spots=spots), key=lambda s: s["seen_s"], reverse=True):
        x, y = geometry.to_room(s["c"][0], s["c"][1], placement)
        if not geometry.within_walls(x, y, room.width, room.height, room.outline, room.edge_margin_m):
            continue
        group = next((g for g in groups if math.hypot(g["x"] - x, g["y"] - y) <= PLACE_M), None)
        if group is None:
            groups.append({"x": x, "y": y, "seen_s": s["seen_s"], "stays": 1})
        else:
            w = group["seen_s"]
            group["x"] = (group["x"] * w + x * s["seen_s"]) / (w + s["seen_s"])
            group["y"] = (group["y"] * w + y * s["seen_s"]) / (w + s["seen_s"])
            group["seen_s"] += s["seen_s"]
            group["stays"] += 1
    places = [{"x": round(g["x"], 2), "y": round(g["y"], 2), "seen_s": round(g["seen_s"]), "stays": g["stays"]}
              for g in groups if g["seen_s"] >= PLACE_S and g["stays"] >= PLACE_STAYS]
    places.sort(key=lambda g: g["seen_s"], reverse=True)
    return places


def zone_suggestions(state: dict, room, placement: dict | None = None, spots=()) -> list[dict]:
    """Places people stay at that no detection zone covers yet:
    [{key, x, y, seen_s, stays, furniture_id, points}] — the zone a seat
    there stands for, or a square around the place."""
    out = []
    for place in favourite_places(state, room, placement, spots):
        x, y = place["x"], place["y"]
        if any(z.kind == "detect" and geometry.point_in_polygon(x, y, z.points) for z in room.zones):
            continue
        seat = next((f for f in room.furniture if f.kind in SEATS and _near_any(
            x, y, [geometry.furniture_outline(f.x, f.y, f.w, f.h, f.angle, 0.0)], 0.4)), None)
        if seat is not None:
            if any(z.furniture_id == seat.id for z in room.zones):
                continue
            points = geometry.furniture_outline(seat.x, seat.y, seat.w, seat.h, seat.angle, 0.2)
        else:
            half = 0.6
            points = [(x - half, y - half), (x + half, y - half), (x + half, y + half), (x - half, y + half)]
        points = geometry.clip_to_plan(points, room.width, room.height)
        if len(points) < 3 or geometry.polygon_area(points) < 0.04:
            continue
        out.append({
            "key": f"place:{round(x, 1)}:{round(y, 1)}" if seat is None else f"seat:{seat.id}",
            "x": x, "y": y, "seen_s": place["seen_s"], "stays": place["stays"],
            "furniture_id": seat.id if seat is not None else None,
            "furniture_kind": seat.kind if seat is not None else None,
            "furniture_name": (seat.name or None) if seat is not None else None,
            "points": [[round(px, 2), round(py, 2)] for px, py in points],
        })
    return out


# --- what the room is used for --------------------------------------------------------

MAX_MAP_CELLS = 1500


def activity_map(state: dict, room, placement: dict | None = None, spots=()) -> dict:
    """Where confirmed targets were, in room metres: {"cell": metres,
    "cells": [[x, y, weight 0..1], ...]} — only what lies within the
    walls, and nothing in a reflector's spot (`spots`): before it was
    known, its target was confirmed as well."""
    placement = placement if placement is not None else room.sensor.model_dump()
    grid = state.get("activity_grid", {})
    cells = []
    for key, seconds in grid.items():
        sx, sy = cell_centre(key)
        if in_spots(sx, sy, spots):
            continue
        x, y = geometry.to_room(sx, sy, placement)
        if geometry.within_walls(x, y, room.width, room.height, room.outline, room.edge_margin_m):
            cells.append((x, y, seconds))
    cells.sort(key=lambda c: c[2], reverse=True)
    cells = cells[:MAX_MAP_CELLS]
    top = cells[0][2] if cells else 0.0
    return {"cell": GRID_M, "cells": [[round(x, 2), round(y, 2), round(math.sqrt(s / top), 3)]
                                      for x, y, s in cells] if top else []}
