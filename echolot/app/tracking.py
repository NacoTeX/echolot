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

This is also where positions are smoothed — since measurement
definition 9 with a Kalman filter that knows people move: each target
carries a velocity, its position is predicted for the next report and the
report corrects the prediction, by as much as the two uncertainties say.
A walker is followed without the lag a plain average has (it trails by
the speed times its time constant); someone standing still is averaged
as calmly as before; and a report is matched to the target whose
*predicted* position it is nearest to, all reports of a frame at once
(the assignment with the least total distance), so two people crossing
keep their own targets. The smoothing setting is how much scatter a
single report is assumed to carry (`SMOOTHING_NOISE_M`): "off" takes each
report as it is.
"""

import math
from dataclasses import dataclass

#: Scatter of one report, metres, per smoothing setting. 0: positions are
#: taken as reported (velocities are still estimated, for the matching).
SMOOTHING_NOISE_M = {"off": 0.0, "normal": 0.2, "strong": 0.3}
#: Scatter assumed for the matching when positions are not smoothed.
MATCH_NOISE_M = 0.15
#: How sharply a person changes speed or direction: the standard
#: deviation of their acceleration, m/s². Walking, turning, stopping.
ACCEL_M_S2 = 1.0
#: Speed uncertainty of a new target: anything up to a brisk walk.
NEW_SPEED_M_S = 1.0
#: Fastest a person is taken to walk. A velocity estimate above it is
#: scatter, not speed.
MAX_SPEED_M_S = 2.5
#: A target keeps its velocity across a gap this long; over a longer one
#: it moves this much worth and the velocity fades: people the module
#: loses have mostly stopped. What is shown for a target not reported is
#: where it was last seen; the velocity only steers where its next report
#: is looked for.
VELOCITY_MEMORY_S = 0.5
#: Resting: a target the filter finds slower than REST_SPEED_M_S is
#: averaged more calmly, with a time constant of REST_TAU_PER_M × the
#: report scatter (normal 0.2 m: 1.2 s; strong 0.3 m: 1.8 s) — until a
#: report lands more than REST_LEAVE_SIGMAS × the scatter from the mean,
#: the filter's own position drifts more than REST_DRIFT_SIGMAS × the
#: scatter away from it
#: (somebody strolling off slowly), or the filter finds it faster than
#: REST_LEAVE_SPEED_M_S. The gap between the two speeds keeps the scatter
#: of the speed estimate itself from ending a rest.
REST_SPEED_M_S = 0.25
REST_LEAVE_SPEED_M_S = 0.6
REST_TAU_PER_M = 6.0
REST_LEAVE_SIGMAS = 3.0
REST_DRIFT_SIGMAS = 1.5
#: A report belongs to a target if it is within this many standard
#: deviations of the prediction (chi² of 2 degrees of freedom at 99.5 %:
#: 10.6) — never farther than MAX_GATE_M, always as far as MIN_GATE_M.
GATE_CHI2 = 10.6
MIN_GATE_M = 0.6
MAX_GATE_M = 1.6

#: A target standing in a spot takes a report away from the spot's centre
#: over one at the centre — where the reflector is — as if the one at
#: the centre were up to this much farther away. Somebody leaving a
#: reflector's spot is followed, the reflector is not.
SPOT_PENALTY_M = 0.6
#: How long a confirmed target may stay in a spot and still count.
SPOT_TRUST_S = 20.0
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
    #: Where the target was first reported, and where last, as reported
    #: (unsmoothed). Where somebody came from and where they went
    #: (room_engine's entrances) must not lag behind them.
    origin: tuple[float, float] = (0.0, 0.0)
    reported: tuple[float, float] = (0.0, 0.0)
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
    #: The filter: position and velocity (m, m/s, sensor coordinates) and
    #: its uncertainty, the same for x and y — position variance,
    #: covariance, velocity variance. `x`/`y` above are what is shown:
    #: the filter's position, or while the target rests the calmer mean
    #: of its reports, or with smoothing off the report itself.
    kx: float = 0.0
    ky: float = 0.0
    vx: float = 0.0
    vy: float = 0.0
    p_pos: float = 0.0
    p_cross: float = 0.0
    p_vel: float = NEW_SPEED_M_S ** 2
    #: Resting: since when the target has been slower than REST_SPEED_M_S,
    #: and the mean of its reports since then; None while it moves.
    rest_since: float | None = None
    rest_x: float = 0.0
    rest_y: float = 0.0

    def __post_init__(self) -> None:
        self.kx, self.ky = self.x, self.y

    def predicted(self, now: float) -> tuple[float, float, float]:
        """Where the target is expected at `now`, and the variance of that
        guess (per axis), before a report says."""
        dt = max(0.0, now - self.last_seen)
        reach = min(dt, VELOCITY_MEMORY_S)
        pp = self.p_pos + 2 * dt * self.p_cross + dt * dt * self.p_vel + ACCEL_M_S2 ** 2 * dt ** 3 / 3
        return self.kx + reach * self.vx, self.ky + reach * self.vy, pp

    def correct(self, px: float, py: float, now: float, noise: float) -> None:
        """Take a report at `now`: predict, then move towards the report
        by the Kalman gain; and keep the resting mean."""
        dt = max(0.0, now - self.last_seen)
        q = ACCEL_M_S2 ** 2
        # Constant velocity across an ordinary gap; across a longer one the
        # target moves VELOCITY_MEMORY_S worth and its velocity fades.
        reach = min(dt, VELOCITY_MEMORY_S)
        fade = math.exp(-(dt - VELOCITY_MEMORY_S) / VELOCITY_MEMORY_S) if dt > VELOCITY_MEMORY_S else 1.0
        x, y = self.kx + reach * self.vx, self.ky + reach * self.vy
        vx, vy = self.vx * fade, self.vy * fade
        a = self.p_pos + 2 * dt * self.p_cross + dt * dt * self.p_vel + q * dt ** 3 / 3
        b = self.p_cross + dt * self.p_vel + q * dt * dt / 2
        c = self.p_vel + q * dt
        r = (noise or MATCH_NOISE_M) ** 2
        k_pos, k_vel = a / (a + r), b / (a + r)
        ix, iy = px - x, py - y
        vx, vy = vx + k_vel * ix, vy + k_vel * iy
        speed = math.hypot(vx, vy)
        if speed > MAX_SPEED_M_S:
            vx, vy = vx * MAX_SPEED_M_S / speed, vy * MAX_SPEED_M_S / speed
            speed = MAX_SPEED_M_S
        self.kx, self.ky = x + k_pos * ix, y + k_pos * iy
        self.vx, self.vy = vx, vy
        self.p_pos, self.p_cross, self.p_vel = (1 - k_pos) * a, (1 - k_pos) * b, c - k_vel * b
        if noise <= 0:
            self.x, self.y = px, py
            return
        # Resting: a report far from the resting mean, or the filter seeing
        # speed, ends it at once — the filter's position takes over.
        resting = (speed < REST_LEAVE_SPEED_M_S and self.rest_since is not None
                   and math.hypot(px - self.rest_x, py - self.rest_y) <= REST_LEAVE_SIGMAS * noise
                   and math.hypot(self.kx - self.rest_x, self.ky - self.rest_y) <= REST_DRIFT_SIGMAS * noise)
        if speed < REST_SPEED_M_S and not resting:
            self.rest_since, self.rest_x, self.rest_y = now, self.kx, self.ky
        elif resting:
            share = 1.0 - math.exp(-max(dt, 0.05) / (REST_TAU_PER_M * noise))
            self.rest_x += share * (px - self.rest_x)
            self.rest_y += share * (py - self.rest_y)
        else:
            self.rest_since = None
        if self.rest_since is not None:
            self.x, self.y = self.rest_x, self.rest_y
        else:
            self.x, self.y = self.kx, self.ky


def _in_spot(x: float, y: float, spots) -> bool:
    return any(math.hypot(x - s.x, y - s.y) <= s.r for s in spots)


def _centrality(x: float, y: float, spots) -> float:
    """1 at a spot's centre, 0 at its edge and outside."""
    return max((1.0 - math.hypot(x - s.x, y - s.y) / s.r for s in spots), default=0.0) if spots else 0.0


def assign(cost) -> list[tuple[int, int]]:
    """The pairs (row, column) with the least total cost, each row and
    each column at most once; None in `cost` is a pair that cannot be made.

    Unmatched is allowed — a report no target takes starts a new target, a
    target with no report is not seen — but every pair that can be made is
    worth making: the most pairs first, the least cost among those
    (Hungarian method on a square matrix padded with a prohibitive cost).
    """
    rows = len(cost)
    cols = len(cost[0]) if rows else 0
    finite = [c for row in cost for c in row if c is not None]
    if not finite:
        return []
    # Leaving a pair unmade costs more than any set of real pairs.
    big = (max(finite) + 1.0) * (min(rows, cols) + 1)
    n = max(rows, cols)
    a = [[cost[i][j] if i < rows and j < cols and cost[i][j] is not None else big for j in range(n)]
         for i in range(n)]
    inf = float("inf")
    u, v = [0.0] * (n + 1), [0.0] * (n + 1)
    match, way = [0] * (n + 1), [0] * (n + 1)
    for i in range(1, n + 1):
        match[0] = i
        j0 = 0
        minv = [inf] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = match[j0], inf, 0
            for j in range(1, n + 1):
                if not used[j]:
                    cur = a[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(n + 1):
                if used[j]:
                    u[match[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if match[j0] == 0:
                break
        while True:
            j1 = way[j0]
            match[j0] = match[j1]
            j0 = j1
            if j0 == 0:
                break
    return sorted((match[j] - 1, j - 1) for j in range(1, n + 1)
                  if match[j] - 1 < rows and j - 1 < cols and cost[match[j] - 1][j - 1] is not None)


class Tracker:
    """Every target of one sensor. Fed once per report."""

    def __init__(self) -> None:
        self.tracks: list[Track] = []
        self._next_id = 1

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

    def update(self, points, now: float, *, confirm_s: float, noise: float, spots=()) -> None:
        """Take one report: `points` in sensor coordinates (metres), `now`
        the time it was measured, `noise` the scatter of one report
        (SMOOTHING_NOISE_M; 0 takes positions as reported)."""
        # Forget what has not been reported for longer than the TTL before
        # anything is matched: a target last seen ten seconds ago is not
        # the same one as a report at the same spot now, and must not come
        # back already confirmed.
        self.tracks = [t for t in self.tracks if now - t.last_seen <= TRACK_TTL_S]
        # Every report against every target's predicted position; the
        # assignment with the least total cost, each target and each report
        # used once. A pair outside the gate cannot be made.
        centrality = [max(0.0, _centrality(px, py, spots)) for px, py in points]
        cost = []
        for t in self.tracks:
            ex, ey, var = t.predicted(now)
            s2 = var + (noise or MATCH_NOISE_M) ** 2
            gate = min(MAX_GATE_M, max(MIN_GATE_M, math.sqrt(GATE_CHI2 * s2)))
            row = []
            for pi, (px, py) in enumerate(points):
                distance = math.hypot(ex - px, ey - py)
                if distance > gate:
                    row.append(None)
                    continue
                row.append(distance + (SPOT_PENALTY_M * centrality[pi] if t.in_spot else 0.0))
            cost.append(row)
        matched_tracks: set[int] = set()
        matched_points: set[int] = set()
        for ti, pi in assign(cost):
            matched_tracks.add(ti)
            matched_points.add(pi)
            track = self.tracks[ti]
            px, py = points[pi]
            track.correct(px, py, now, noise)
            track.reported = (px, py)
            track.last_seen = now
            track.seen = True
        for ti, track in enumerate(self.tracks):
            if ti not in matched_tracks:
                track.seen = False
        for pi, (px, py) in enumerate(points):
            if pi not in matched_points:
                self.tracks.append(Track(
                    id=self._next_id, x=px, y=py, first_seen=now, last_seen=now,
                    origin=(px, py), reported=(px, py), p_pos=(noise or MATCH_NOISE_M) ** 2,
                ))
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
