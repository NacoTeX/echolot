"""Live calibration: listening to one room's sensor for a while.

Three recordings. They run here, next to the radar link, because the
browser sees the room three times a second and the module reports more
often than that:

  empty   The room is empty, so everything the module reports is a
          reflection. The places it reports one again and again become
          interference spots (app/tracking.py says what they do).
  point   Somebody stands on a spot marked on the plan. The median of
          what the module reports is where it thinks that spot is;
          app/alignment.py compares the two.
  walk    Somebody walks from A to B, both drawn on the plan. Where the
          module saw the walk start and end tells which way its axes
          point (app/axes.py).

A recording starts after a delay — time to leave the room, or to walk to
the spot — and ends by the clock. It lives in memory: a restart ends it,
and nothing is lost that recording again would not find.

The empty-room run also answers a question about the hardware that the
manual leaves open: whether the module keeps sending empty reports in an
empty room, or falls silent. It counts both and says which it saw.
"""

import bisect
import math
import secrets
import statistics
import time
from dataclasses import dataclass, field

from app import geometry
from app.radar_frame import QUIET, RECEIVING
from app.rooms import InterferenceSpot, active_interference
from app.tracking import Tracker

#: kind -> (delay: min, max, default), (duration: min, max, default), seconds.
LIMITS = {
    "empty": ((0, 120, 20), (10, 300, 45)),
    "point": ((0, 30, 3), (2, 20, 5)),
    "walk": ((0, 30, 3), (3, 20, 6)),
}
#: A walk (axes.py) must cover at least this much, start to end.
MIN_WALK_M = 0.8
#: Start and end of a walk: the median over this long at either end.
WALK_END_S = 0.8
#: Tracking a walk (_walk_tracks). A target's next report is looked for
#: within WALK_GATE_M of its last — a step and the module's scatter, no
#: more: somebody standing half a metre from a reflection must not change
#: places with it. A target seen in WALK_SEEN reports picks first. Not
#: reported for WALK_LOST_S, it is lost.
WALK_GATE_M = 0.3
WALK_SEEN = 3
WALK_LOST_S = 1.0
#: Pieces of track are joined (_joined) where one starts within
#: WALK_JOIN_S after another ended, near where that one was heading
#: (_ahead: as it moved over its last WALK_HEADING_S, no faster than
#: WALK_SPEED_M_S): within WALK_GATE_M and WALK_TURN_M_S for every second
#: between them.
WALK_JOIN_S = 1.5
WALK_HEADING_S = 0.5
WALK_SPEED_M_S = 1.5
WALK_TURN_M_S = 1.0
#: The walk found must begin and end within this, all told, as far from
#: the sensor as A and B are drawn. Farther off, it is not the walk drawn
#: — a reflection of it in a wall, or the sensor stands elsewhere on the
#: plan than in the room.
WALK_RANGE_OFF_M = 1.5
#: Somebody walking is in most reports of their walk; standing still,
#: a module may lose them for a report or two. Scattered ghosts, strung
#: together now and then within reach of each other, are in a few in a
#: hundred.
WALK_DENSITY = 0.3
#: Points closer than this to a group's centre belong to it.
CLUSTER_M = 0.5
#: A reflection appearing in fewer of the empty room's reports than this
#: share (and never fewer than three reports) is left to the confirmation
#: time; it is not a fixed reflector.
MIN_SPOT_SHARE = 0.03
MIN_SPOT_REPORTS = 3
#: A target that travels this far during the empty-room run is somebody
#: walking — or the reflection of somebody walking — not a reflector.
MOVING_EXTENT_M = 1.2
#: Spot radius: the scatter of its reports plus this, within limits.
SPOT_MARGIN_M = 0.2
SPOT_RADIUS_M = (0.3, 1.2)
#: At a standpoint, the person must be in at least this share of reports.
MIN_POINT_SHARE = 0.4
#: Points kept for drawing while a recording runs.
LIVE_POINTS = 300


@dataclass
class Capture:
    id: str
    room_id: str
    device_id: str
    kind: str
    created: float
    delay_s: float
    duration_s: float
    #: Interference spots in force when it started (standpoint only).
    spots: list = field(default_factory=list)
    #: (time, targets in sensor metres) per receiving report.
    reports: list = field(default_factory=list)
    quiet: int = 0
    unknown: int = 0
    cancelled: bool = False
    result: dict | None = None
    #: For a standpoint: {"ref", "role", "replace"} — where the person
    #: stands, so the result is kept even if the page that started it is
    #: gone by then — and whether it has been kept.
    standpoint: dict | None = None
    kept: bool = False
    keep_error: str | None = None
    #: The room's mounting when it started (Calibration.mounting_epoch):
    #: a result from before a remount is not kept for the new mounting.
    epoch: int = 0
    #: For a walk: {"leg": "away"|"across", "from": [x, y], "to": [x, y]},
    #: the way drawn on the plan (axes.py), and the sensor as it stood on
    #: the plan then — how far from it A and B are.
    walk: dict | None = None
    placement: dict | None = None

    @property
    def starts_at(self) -> float:
        return self.created + self.delay_s

    @property
    def ends_at(self) -> float:
        return self.starts_at + self.duration_s

    def phase(self, now: float) -> str:
        if self.cancelled:
            return "cancelled"
        if now < self.starts_at:
            return "waiting"
        if now < self.ends_at:
            return "recording"
        return "done"


def _p90(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(math.ceil(0.9 * len(ordered))) - 1)]


def _cluster(points: list[tuple[int, float, float]], radius: float) -> list[dict]:
    """Greedy grouping: (report index, x, y) -> groups with centre and members."""
    groups: list[dict] = []
    for index, x, y in points:
        best, best_d = None, radius
        for g in groups:
            d = math.hypot(g["cx"] - x, g["cy"] - y)
            if d <= best_d:
                best, best_d = g, d
        if best is None:
            best = {"members": [], "cx": x, "cy": y}
            groups.append(best)
        best["members"].append((index, x, y))
        n = len(best["members"])
        best["cx"] += (x - best["cx"]) / n
        best["cy"] += (y - best["cy"]) / n
    for g in groups:
        g["reports"] = len({m[0] for m in g["members"]})
        g["scatter"] = _p90([math.hypot(m[1] - g["cx"], m[2] - g["cy"]) for m in g["members"]])
    return groups


def analyze_empty(capture: Capture) -> dict:
    receiving = len(capture.reports)
    total = receiving + capture.quiet
    base = {
        "ok": False,
        "kind": "empty",
        "device_id": capture.device_id,
        "reports": total,
        "receiving": receiving,
        "quiet": capture.quiet,
        "unknown": capture.unknown,
        "empty_reports": sum(1 for _, targets in capture.reports if not targets),
    }
    if total < 5:
        return {**base, "error": (
            f"In {capture.duration_s:.0f} s kamen nur {total} Meldungen vom Sensor. "
            "Ist er verbunden? Die Aufnahme zeigt nichts Verlässliches."
        )}

    # Follow the reports once, the way the room does, to find what moved.
    tracker = Tracker()
    paths: dict[int, list] = {}
    for index, (at, targets) in enumerate(capture.reports):
        tracker.update(list(targets), at, confirm_s=0, tau=0.0)
        for track in tracker.visible():
            paths.setdefault(track.id, []).append((index, track.x, track.y))
    still: list[tuple[int, float, float]] = []
    moved = 0
    for path in paths.values():
        xs, ys = [p[1] for p in path], [p[2] for p in path]
        if math.hypot(max(xs) - min(xs), max(ys) - min(ys)) > MOVING_EXTENT_M:
            moved += 1
        else:
            still.extend(path)

    spots = []
    for g in _cluster(still, CLUSTER_M):
        if g["reports"] < max(MIN_SPOT_REPORTS, MIN_SPOT_SHARE * total):
            continue
        radius = min(max(g["scatter"] + SPOT_MARGIN_M, SPOT_RADIUS_M[0]), SPOT_RADIUS_M[1])
        spots.append(InterferenceSpot(
            x=round(g["cx"], 2), y=round(g["cy"], 2), r=round(radius, 2),
            share=round(min(1.0, g["reports"] / total), 3),
        ))
    spots.sort(key=lambda s: s.share, reverse=True)
    dropped = max(0, len(spots) - 12)
    spots = spots[:12]

    warnings = []
    if moved:
        warnings.append(
            "Während der Aufnahme hat sich etwas bewegt — war der Raum leer? Was sich bewegt "
            "hat, wurde nicht als Störquelle übernommen. Bei Zweifeln noch einmal aufnehmen."
        )
    if dropped:
        warnings.append(f"{dropped} schwächere Stellen wurden weggelassen; mehr als 12 hält ein Raum nicht.")
    if capture.unknown:
        warnings.append("Das Radarmodul hat zwischendurch nicht geantwortet.")
    return {**base, "ok": True, "spots": [s.model_dump() for s in spots], "moved": moved, "warnings": warnings}


def _in_spot(x: float, y: float, spots) -> bool:
    return any(math.hypot(x - s.x, y - s.y) <= s.r for s in spots)


def analyze_point(capture: Capture) -> dict:
    total = len(capture.reports)
    base = {"ok": False, "kind": "point", "reports": total, "quiet": capture.quiet}
    if total < 3:
        return {**base, "error": (
            "Der Sensor hat in dieser Zeit kaum gemeldet. Ist er verbunden? Dann noch einmal messen."
        )}
    points = [
        (index, x, y)
        for index, (_, targets) in enumerate(capture.reports)
        for x, y in targets
        if not _in_spot(x, y, capture.spots)
    ]
    groups = sorted(_cluster(points, 0.6), key=lambda g: g["reports"], reverse=True)
    if not groups:
        return {**base, "error": (
            "Das Radar hat an diesem Standpunkt niemanden gesehen. Wer ganz still steht, geht "
            "dem Modul manchmal verloren — leicht hin und her wiegen und noch einmal messen."
        )}
    best = groups[0]
    share = best["reports"] / total
    if share < MIN_POINT_SHARE:
        return {**base, "error": (
            f"Kein stabiles Ziel: nur in {share * 100:.0f} % der Meldungen. Noch einmal messen, "
            "am besten mit leichter Bewegung auf der Stelle."
        )}
    x = statistics.median(m[1] for m in best["members"])
    y = statistics.median(m[2] for m in best["members"])
    spread = _p90([math.hypot(m[1] - x, m[2] - y) for m in best["members"]])
    warnings = []
    if spread > 0.35:
        warnings.append(f"Die Position schwankte um etwa {spread * 100:.0f} cm. Ruhiger stehen macht sie genauer.")
    if len(groups) > 1 and groups[1]["reports"] / total >= 0.3:
        warnings.append("Außer dir war noch ein Ziel zu sehen. Ist noch jemand im Raum?")
    return {
        **base,
        "ok": True,
        "raw": [round(x, 3), round(y, 3)],
        "spread_m": round(spread, 3),
        "share": round(share, 3),
        "warnings": warnings,
    }


def _walk_tracks(reports: list) -> list[list[tuple[float, float, float]]]:
    """Every target through the reports, each as [(t, x, y)] where it was
    reported.

    Every target keeps its own track, as in the room's tracker
    (app/tracking.py): a reflection keeps its own while the module loses
    the walker for a moment beside it. Nearest pairs first, and a target
    seen in WALK_SEEN reports before one that has only just shown up: a
    reflection flashing up just ahead of the walker would otherwise take
    their track over. None reaches farther than WALK_GATE_M; a track that
    has lost its target does not go collecting what else turns up, and
    the pieces a walk falls into then are joined again (_joined).

    Looking for a track's next report where it was heading, rather than
    where it was, loses the walker less often, but tried on simulated
    walks it put more of them in the wrong place — standing beside a
    reflection, mostly. Only joining pieces goes by the heading.
    """
    live: list[list] = []
    pieces: list[list] = []
    for at, points in reports:
        pieces.extend(track for track in live if at - track[-1][0] > WALK_LOST_S)
        live = [track for track in live if at - track[-1][0] <= WALK_LOST_S]
        free = set(range(len(points)))
        for seen in (True, False):
            pairs = []
            for k, track in enumerate(live):
                if (len(track) >= WALK_SEEN) != seen:
                    continue
                _, x, y = track[-1]
                for j in free:
                    d = math.hypot(points[j][0] - x, points[j][1] - y)
                    if d <= WALK_GATE_M:
                        pairs.append((d, k, j))
            matched: set[int] = set()
            for _, k, j in sorted(pairs):
                if k in matched or j not in free:
                    continue
                matched.add(k)
                free.discard(j)
                live[k].append((at, *points[j]))
        live.extend([(at, *points[j])] for j in sorted(free))
    # A point alone is nothing to join.
    return _joined([track for track in pieces + live if len(track) >= 2])


def _ahead(track: list, at: float) -> tuple[float, float]:
    """Where a track's target is to be looked for at `at`: where it was
    last, moved on as it moved over the last WALK_HEADING_S, no faster
    than WALK_SPEED_M_S."""
    t1, x1, y1 = track[-1]
    earlier = next((p for p in reversed(track) if t1 - p[0] >= WALK_HEADING_S), None)
    if earlier is None:
        return x1, y1
    t0, x0, y0 = earlier
    vx, vy = (x1 - x0) / (t1 - t0), (y1 - y0) / (t1 - t0)
    speed = math.hypot(vx, vy)
    if speed > WALK_SPEED_M_S:
        vx, vy = vx * WALK_SPEED_M_S / speed, vy * WALK_SPEED_M_S / speed
    return x1 + vx * (at - t1), y1 + vy * (at - t1)


def _joined(pieces: list[list]) -> list[list]:
    """Pieces of track put together where one takes up after another left
    off: starting within WALK_JOIN_S after it ended, near where it was
    heading. Somebody the module loses for a few reports while walking is
    farther on than WALK_GATE_M when it finds them again. The nearest
    first; each piece goes on in one other at most."""
    pieces = sorted(pieces, key=lambda p: p[0][0])
    starts = [p[0][0] for p in pieces]
    links = []
    for a, first in enumerate(pieces):
        t1 = first[-1][0]
        for b in range(bisect.bisect_right(starts, t1), bisect.bisect_right(starts, t1 + WALK_JOIN_S)):
            t0, x0, y0 = pieces[b][0]
            gap = t0 - t1
            x, y = _ahead(first, t0)
            d = math.hypot(x0 - x, y0 - y)
            if d <= WALK_GATE_M + WALK_TURN_M_S * gap:
                links.append((d + WALK_TURN_M_S * gap, a, b))
    after: dict[int, int] = {}
    before: set[int] = set()
    for _, a, b in sorted(links):
        if a in after or b in before:
            continue
        after[a] = b
        before.add(b)
    joined = []
    for head in range(len(pieces)):
        if head in before:
            continue
        path, k = [], head
        while k is not None:
            path.extend(pieces[k])
            k = after.get(k)
        joined.append(path)
    return joined


def _dense(path: list, report_times: list[float]) -> list:
    """A track without its thin ends: from either end, points go while in
    the WALK_END_S next to them it was in fewer than WALK_DENSITY of the
    reports. What is left is where it was really there."""
    times = [p[0] for p in path]

    def dense(lo: float, hi: float) -> bool:
        seen = bisect.bisect_right(times, hi) - bisect.bisect_left(times, lo)
        sent = bisect.bisect_right(report_times, hi) - bisect.bisect_left(report_times, lo)
        return seen >= WALK_DENSITY * sent

    first, last = 0, len(path) - 1
    while first < last and not dense(times[first], times[first] + WALK_END_S):
        first += 1
    while last > first and not dense(times[last] - WALK_END_S, times[last]):
        last -= 1
    return path[first:last + 1]


def analyze_walk(capture: Capture) -> dict:
    """Where somebody walking from A to B started and ended, as the module
    reported it. Every target is tracked (_walk_tracks) and kept where it
    was in a fair share of the reports (_dense). Of those that moved at
    least MIN_WALK_M, the walker is the one that began and ended as far
    from the sensor as A and B are on the plan — which holds whichever way
    the module's axes point and however the sensor on the plan is turned;
    a reflection of the walk in a wall moves as much, but farther away.
    One more than WALK_RANGE_OFF_M off is not taken for the walk. Without
    the plan's distances, the one that moved farthest. Start and end are
    medians over WALK_END_S at either end of the walk."""
    total = len(capture.reports)
    base = {"ok": False, "kind": "walk", "reports": total, "quiet": capture.quiet, "walk": capture.walk}
    if total < 5:
        return {**base, "error": "Der Sensor hat in dieser Zeit kaum gemeldet. Ist er verbunden? Dann noch einmal gehen."}
    report_times = [at for at, _ in capture.reports]
    paths = []
    for track in _walk_tracks(capture.reports):
        path = _dense(track, report_times)
        span = bisect.bisect_right(report_times, path[-1][0]) - bisect.bisect_left(report_times, path[0][0])
        if len(path) >= 5 and len(path) >= WALK_DENSITY * span:
            paths.append(path)
    if not paths:
        return {**base, "error": "Niemand war lange genug zu sehen. Noch einmal gehen, zügig und allein im Raum."}

    def ends(path):
        first = [p for p in path if p[0] <= path[0][0] + WALK_END_S]
        last = [p for p in path if p[0] >= path[-1][0] - WALK_END_S]
        start = (statistics.median(p[1] for p in first), statistics.median(p[2] for p in first))
        end = (statistics.median(p[1] for p in last), statistics.median(p[2] for p in last))
        return start, end

    scored = []
    for path in paths:
        start, end = ends(path)
        scored.append((math.hypot(end[0] - start[0], end[1] - start[1]), path, start, end))
    walks = [s for s in scored if s[0] >= MIN_WALK_M]
    if not walks:
        moved = max(s[0] for s in scored)
        hint = (
            " Wer tatsächlich quer gegangen ist und das hier sieht: Ein Modul, das auf der Seite liegt, misst "
            "quer die Höhe statt der Seite und meldet kaum Bewegung — dann den Einbau prüfen."
            if (capture.walk or {}).get("leg") == "across" else ""
        )
        return {**base, "error": (
            f"Nur {moved * 100:.0f} cm Weg erkannt. Erst auf A stellen, dann nach dem Start zügig nach B gehen.{hint}"
        )}
    if capture.placement and capture.walk:
        sx, sy = capture.placement["x"], capture.placement["y"]
        drawn = [math.hypot(q[0] - sx, q[1] - sy) for q in (capture.walk["from"], capture.walk["to"])]

        def ranges(s) -> list[float]:
            return [math.hypot(*geometry.correct(*q, capture.placement)) for q in (s[2], s[3])]

        best = min(walks, key=lambda s: sum(abs(r - d) for r, d in zip(ranges(s), drawn)))
        seen = ranges(best)
        if sum(abs(r - d) for r, d in zip(seen, drawn)) > WALK_RANGE_OFF_M:
            m = lambda v: f"{v:.1f}".replace(".", ",")  # noqa: E731
            return {**base, "error": (
                f"Was sich bewegt hat, begann {m(seen[0])} m und endete {m(seen[1])} m vom Sensor entfernt; "
                f"A und B liegen auf dem Plan {m(drawn[0])} und {m(drawn[1])} m von ihm. Ging der Weg von A nach B, "
                "und steht der Sensor auf dem Plan an der richtigen Stelle? Dann noch einmal gehen, allein im Raum."
            )}
        moved, path, start, end = best
    else:
        moved, path, start, end = max(walks, key=lambda s: s[0])
    # Somebody else moving about: the walk may have been mixed up with
    # them. What stands still — a radiator, a mirror — does not matter.
    warnings = []
    if any(other_moved >= MIN_WALK_M / 2 and len(other) >= 0.3 * total
           for other_moved, other, _, _ in scored if other is not path):
        warnings.append(
            "Außer dir hat sich noch etwas bewegt: noch jemand im Raum, oder dein Spiegelbild in einer Wand oder "
            "Scheibe. Ist noch jemand da, allein noch einmal gehen."
        )
    return {
        **base,
        "ok": True,
        "start_raw": [round(start[0], 3), round(start[1], 3)],
        "end_raw": [round(end[0], 3), round(end[1], 3)],
        "moved_m": round(moved, 3),
        "share": round(len(path) / total, 3),
        "warnings": warnings,
    }


class Captures:
    """At most one recording per room."""

    def __init__(self, links, clock=time.monotonic) -> None:
        self._links = links
        self._clock = clock
        self._by_room: dict[str, Capture] = {}

    def start(self, room, kind: str, delay_s: float | None = None, duration_s: float | None = None,
              standpoint: dict | None = None, walk: dict | None = None) -> Capture:
        if kind not in LIMITS:
            raise ValueError("Unbekannte Aufnahmeart")
        if not room.sensor.device_id:
            raise ValueError("Diesem Raum ist kein Sensor zugeordnet")
        (d_lo, d_hi, d_default), (t_lo, t_hi, t_default) = LIMITS[kind]
        delay = d_default if delay_s is None else float(delay_s)
        duration = t_default if duration_s is None else float(duration_s)
        if not (d_lo <= delay <= d_hi and t_lo <= duration <= t_hi):
            raise ValueError(f"Wartezeit {d_lo}–{d_hi} s und Dauer {t_lo}–{t_hi} s")
        capture = Capture(
            id=secrets.token_hex(4),
            room_id=room.id,
            device_id=room.sensor.device_id,
            kind=kind,
            created=self._clock(),
            delay_s=delay,
            duration_s=duration,
            spots=active_interference(room) if kind == "point" else [],
            standpoint=standpoint if kind == "point" else None,
            epoch=room.calibration.mounting_epoch,
            walk=walk if kind == "walk" else None,
            placement=room.sensor.model_dump() if kind == "walk" else None,
        )
        # A new recording replaces one still running: there is one sensor
        # and one person with the iPad.
        self._by_room[room.id] = capture
        return capture

    def get(self, room_id: str) -> Capture | None:
        return self._by_room.get(room_id)

    def cancel(self, room_id: str) -> bool:
        capture = self._by_room.pop(room_id, None)
        if capture is None:
            return False
        capture.cancelled = True
        return True

    def forget_room(self, room_id: str) -> None:
        self._by_room.pop(room_id, None)

    def on_frame(self, device_id: str) -> None:
        """Radar-link listener: one call per report of any sensor."""
        now = self._clock()
        for capture in self._by_room.values():
            if capture.device_id != device_id or capture.phase(now) != "recording":
                continue
            snap = self._links.snapshot(device_id)
            frame = snap.frame if snap else None
            if frame is None:
                continue
            if frame.state == RECEIVING:
                capture.reports.append((now, frame.targets_m))
            elif frame.state == QUIET:
                capture.quiet += 1
            else:
                capture.unknown += 1

    def view(self, capture: Capture) -> dict:
        now = self._clock()
        phase = capture.phase(now)
        if phase == "done" and capture.result is None:
            analyze = {"empty": analyze_empty, "point": analyze_point, "walk": analyze_walk}[capture.kind]
            capture.result = analyze(capture)
        points = [p for _, targets in capture.reports for p in targets][-LIVE_POINTS:]
        snap = self._links.snapshot(capture.device_id)
        return {
            "id": capture.id,
            "kind": capture.kind,
            "phase": phase,
            "starts_in_s": round(max(0.0, capture.starts_at - now), 1),
            "elapsed_s": round(min(capture.duration_s, max(0.0, now - capture.starts_at)), 1),
            "duration_s": capture.duration_s,
            "delay_s": capture.delay_s,
            "reports": len(capture.reports) + capture.quiet,
            "points": [[round(x, 2), round(y, 2)] for x, y in points],
            "sensor_fresh": bool(snap and snap.fresh(now)),
            "result": capture.result,
            "device_id": capture.device_id,
            "epoch": capture.epoch,
            "ended_ago_s": round(max(0.0, now - capture.ends_at), 1),
            "standpoint": capture.standpoint,
            "walk": capture.walk,
            "kept": capture.kept,
            "keep_error": capture.keep_error,
        }
