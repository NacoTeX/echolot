"""Targets followed from one report to the next.

The LD2460 hands over up to five positions per report and nothing more:
no identities, no speeds, no confidence. Two kinds of false target come
with that, and both look exactly like a person in a single report:

  * A reflection that flashes up for a report or two somewhere in the
    room. Following each position from report to report is what gives it
    away — it does not stay. A target counts only once it has been
    reported for the room's confirmation time (`confirm_s`).
  * A fixed reflector — a radiator, a mirror, the metal frame of a sofa —
    that makes the module report a target in the same place again and
    again. Staying is no giveaway for that one; knowing the place is.
    The calibration learns such places in the empty room, and a target
    that *appears* in one is never confirmed there. A person confirmed
    elsewhere who walks into one keeps counting — and when they walk out
    again, their target goes with them rather than staying behind on the
    reflector, which keeps reporting where they were. Telling the two
    apart from positions alone is a guess, though, so the benefit of the
    doubt runs out: a confirmed target that stays in a spot for
    `SPOT_TRUST_S` stops counting. Better to lose somebody standing right
    on a radiator (the hold time bridges a lot) than to count the
    radiator for good.

Positions are kept in sensor coordinates, so a sensor moved or turned on
the plan does not tear every target away from its history.

This is also where positions are smoothed: each report moves a target
part of the way towards the newly reported position, `alpha` of it.
"""

import math
from dataclasses import dataclass

#: Share of the way each report moves a target towards its new position.
#: 1.0 is no smoothing. At five reports a second "normal" lags a walking
#: person by about a report; "strong" by three.
SMOOTHING_ALPHA = {"off": 1.0, "normal": 0.5, "strong": 0.25}
#: A target standing in a spot takes a report away from the spot's centre
#: over one at the centre — where the reflector is — as if the one at
#: the centre were up to this much farther away. Somebody leaving a
#: reflector's spot is followed, the reflector is not.
SPOT_PENALTY_M = 0.6
#: How long a confirmed target may stay in a spot and still count.
SPOT_TRUST_S = 20.0
#: Farthest a target can be from where it was and still be the same one.
#: A person walking briskly covers 0.3 m between two reports at 5 Hz; the
#: module's own scatter adds a few decimetres.
GATE_M = 0.9
#: How long a target that was not reported is remembered. Long enough to
#: bridge the reports the module drops for a person standing still; it
#: is never counted in that time.
TRACK_TTL_S = 1.5
#: While a target is being confirmed, at least this share of the reports
#: must have carried it. One that shows up every third report is not
#: somebody standing there.
MIN_HIT_RATIO = 0.5
MAX_TRACKS = 16
#: Report times are sums of float seconds; "1.0 s later" must not miss
#: by a rounding error.
_EPSILON = 1e-6


@dataclass
class Track:
    id: int
    #: Sensor coordinates, metres, smoothed.
    x: float
    y: float
    first_seen: float
    last_seen: float
    #: Start of the current confirmation run; None while there is none.
    confirm_since: float | None = None
    #: Reports during the current confirmation run, and those that had
    #: this target in them.
    reports: int = 0
    hits: int = 0
    confirmed: bool = False
    #: Reported in the latest report.
    seen: bool = True
    #: Inside a learned interference spot at the latest report, and since
    #: when without a break.
    in_spot: bool = False
    spot_since: float | None = None


def _in_spot(x: float, y: float, spots) -> bool:
    return any(math.hypot(x - s.x, y - s.y) <= s.r for s in spots)


def _centrality(x: float, y: float, spots) -> float:
    """1 at a spot's centre, 0 at its edge and outside."""
    return max((1.0 - math.hypot(x - s.x, y - s.y) / s.r for s in spots), default=0.0) if spots else 0.0


class Tracker:
    """Every target of one sensor. Fed once per report."""

    def __init__(self) -> None:
        self.tracks: list[Track] = []
        self._next_id = 1

    def reset(self) -> None:
        self.tracks = []

    def visible(self) -> list[Track]:
        """The targets of the latest report, in a stable order."""
        return sorted((t for t in self.tracks if t.seen), key=lambda t: t.id)

    def held(self, now: float) -> list[Track]:
        """Confirmed targets the latest report did not carry, still within
        their memory (TRACK_TTL_S): somebody the module lost for a report
        or two. They keep counting where they were last seen, so a count
        does not drop to zero and back on every dropped report."""
        return sorted(
            (t for t in self.tracks if not t.seen and t.confirmed and now - t.last_seen <= TRACK_TTL_S + _EPSILON),
            key=lambda t: t.id,
        )

    def update(self, points, now: float, *, confirm_s: float, alpha: float, spots=()) -> None:
        """Take one report: `points` in sensor coordinates (metres), `now`
        the time it arrived."""
        # Forget what has not been reported for longer than the TTL before
        # anything is matched: a target last seen ten seconds ago is not
        # the same one as a report at the same spot now, and must not come
        # back already confirmed.
        self.tracks = [t for t in self.tracks if now - t.last_seen <= TRACK_TTL_S]
        # Nearest pairs first, each target and each point used once.
        centrality = [max(0.0, _centrality(px, py, spots)) for px, py in points]
        pairs = []
        for ti, t in enumerate(self.tracks):
            for pi, (px, py) in enumerate(points):
                distance = math.hypot(t.x - px, t.y - py)
                if distance > GATE_M:
                    continue
                cost = distance + (SPOT_PENALTY_M * centrality[pi] if t.in_spot else 0.0)
                pairs.append((cost, ti, pi))
        pairs.sort()
        matched_tracks: set[int] = set()
        matched_points: set[int] = set()
        for _cost, ti, pi in pairs:
            if ti in matched_tracks or pi in matched_points:
                continue
            matched_tracks.add(ti)
            matched_points.add(pi)
            track = self.tracks[ti]
            px, py = points[pi]
            track.x += alpha * (px - track.x)
            track.y += alpha * (py - track.y)
            track.last_seen = now
            track.seen = True
        for ti, track in enumerate(self.tracks):
            if ti not in matched_tracks:
                track.seen = False
        for pi, (px, py) in enumerate(points):
            if pi not in matched_points:
                self.tracks.append(Track(id=self._next_id, x=px, y=py, first_seen=now, last_seen=now))
                self._next_id += 1

        self.tracks = [t for t in self.tracks if t.seen or now - t.last_seen <= TRACK_TTL_S]
        if len(self.tracks) > MAX_TRACKS:
            self.tracks.sort(key=lambda t: (t.seen, t.last_seen), reverse=True)
            del self.tracks[MAX_TRACKS:]

        for track in self.tracks:
            if track.seen:
                track.in_spot = _in_spot(track.x, track.y, spots)
                if not track.in_spot:
                    track.spot_since = None
                elif track.spot_since is None:
                    track.spot_since = now
            if track.confirmed:
                if track.in_spot and now - track.spot_since > SPOT_TRUST_S:
                    track.confirmed = False
                    track.confirm_since = None
                continue
            if track.confirm_since is None:
                if not track.seen or track.in_spot:
                    continue
                track.confirm_since = now
                track.reports = track.hits = 0
            if track.seen and track.in_spot:
                # Back in the reflector's spot before it was confirmed:
                # start over once it is out again.
                track.confirm_since = None
                continue
            track.reports += 1
            track.hits += int(track.seen)
            if track.hits < MIN_HIT_RATIO * track.reports:
                track.confirm_since = None
                continue
            if track.seen and now - track.confirm_since >= confirm_s - _EPSILON:
                track.confirmed = True
