"""Live calibration: listening to one room's sensor for a while.

Two recordings. Both run here, next to the radar link, because the
browser sees the room three times a second and the module reports more
often than that:

  empty   The room is empty, so everything the module reports is a
          reflection. The places it reports one again and again become
          interference spots (app/tracking.py says what they do).
  point   Somebody stands on a spot marked on the plan. The median of
          what the module reports is where it thinks that spot is;
          app/alignment.py compares the two.

A recording starts after a delay — time to leave the room, or to walk to
the spot — and ends by the clock. It lives in memory: a restart ends it,
and nothing is lost that recording again would not find.

The empty-room run also answers a question about the hardware that the
manual leaves open: whether the module keeps sending empty reports in an
empty room, or falls silent. It counts both and says which it saw.
"""

import math
import secrets
import statistics
import time
from dataclasses import dataclass, field

from app.radar_frame import QUIET, RECEIVING
from app.rooms import InterferenceSpot, active_interference
from app.tracking import Tracker

#: kind -> (delay: min, max, default), (duration: min, max, default), seconds.
LIMITS = {
    "empty": ((0, 120, 20), (10, 300, 45)),
    "point": ((0, 30, 3), (2, 20, 5)),
}
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
        tracker.update(list(targets), at, confirm_s=0, alpha=1.0)
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


class Captures:
    """At most one recording per room."""

    def __init__(self, links, clock=time.monotonic) -> None:
        self._links = links
        self._clock = clock
        self._by_room: dict[str, Capture] = {}

    def start(self, room, kind: str, delay_s: float | None = None, duration_s: float | None = None,
              standpoint: dict | None = None) -> Capture:
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
            capture.result = analyze_empty(capture) if capture.kind == "empty" else analyze_point(capture)
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
            "kept": capture.kept,
            "keep_error": capture.keep_error,
        }
