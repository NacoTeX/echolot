"""A test track for the tracker: people walking, stopping and crossing, as
the LD2460 reports them, with the truth alongside.

Deterministic for a seed. What the reports look like is modelled from
what tests/simhome.py and the hardware test show — up to five positions
in the module's metres, rounded to its decimetres, with scatter; a person
walking reported nearly every time, one standing still now and then —
not from the module's physics. It measures, for a tracker, how far the
targets it shows are from where people really are, how far they trail a
walker, and how often a person's target changes its identity.

    python -m tests.trackbench      # compares the tracker with itself
"""

import math
import random
from dataclasses import dataclass


@dataclass
class Walker:
    """A path of (x, y) waypoints in sensor metres, walked at `speed`,
    with `pause_s` standing still at each waypoint but the first."""

    path: list[tuple[float, float]]
    speed: float = 1.2
    pause_s: float = 0.0
    start_s: float = 0.0

    def where(self, t: float):
        """(x, y, moving) at time t, or None before the start / after the end."""
        t -= self.start_s
        if t < 0:
            return None
        here = self.path[0]
        for target in self.path[1:]:
            leg = math.dist(here, target) / self.speed
            if t <= leg:
                f = t / leg if leg else 1.0
                return (here[0] + f * (target[0] - here[0]), here[1] + f * (target[1] - here[1]), True)
            t -= leg
            if t <= self.pause_s:
                return (target[0], target[1], False)
            t -= self.pause_s
            here = target
        return None


@dataclass
class Scenario:
    name: str
    walkers: list[Walker]
    duration_s: float
    rate_hz: float = 10.0
    noise_m: float = 0.12
    walk_hit: float = 0.95
    still_hit: float = 0.5


SCENARIOS = [
    Scenario("Gang mit Kehren", [Walker([(-2.0, 1.5), (2.0, 1.5), (2.0, 4.5), (-1.5, 4.5), (0.0, 2.5)],
                                        pause_s=2.0)], 22.0),
    Scenario("Gang, 4 Meldungen/s", [Walker([(-2.0, 1.5), (2.0, 1.5), (2.0, 4.5), (-1.5, 4.5)], pause_s=2.0)],
             16.0, rate_hz=4.0),
    Scenario("Zwei kreuzen sich", [Walker([(-2.5, 2.6), (2.5, 2.6)]), Walker([(2.5, 3.2), (-2.5, 3.2)])], 5.5),
    Scenario("Zwei kreuzen sich schräg", [Walker([(-2.0, 1.5), (2.0, 4.5)], speed=1.3),
                                          Walker([(2.0, 1.5), (-2.0, 4.5)], speed=1.1)], 5.0),
    Scenario("Langsam schlendern", [Walker([(-1.5, 2.0), (1.5, 2.0), (1.5, 4.0)], speed=0.5, pause_s=3.0)], 14.0),
    Scenario("Still sitzen", [Walker([(0.5, 3.0), (0.5, 3.0)], pause_s=20.0)], 20.0, still_hit=0.35),
]


def reports(scenario: Scenario, seed: int):
    """(time, [(x, y)...], truth [(x, y, moving)...]) per report."""
    rng = random.Random(seed)
    dt = 1.0 / scenario.rate_hz
    t = 0.0
    while t <= scenario.duration_s:
        truth = [p for w in scenario.walkers if (p := w.where(t)) is not None]
        points = []
        for x, y, moving in truth:
            if rng.random() < (scenario.walk_hit if moving else scenario.still_hit):
                mx = round((x + rng.gauss(0, scenario.noise_m)) * 10) / 10
                my = round((y + rng.gauss(0, scenario.noise_m)) * 10) / 10
                points.append((mx, my))
        rng.shuffle(points)
        # Jitter in when the report arrives, as over Wi-Fi.
        yield t + rng.uniform(0, 0.02), points[:5], truth
        t += dt


#: How long a person must stay nearest to another target for it to count
#: as a switch of identity.
SETTLE_S = 1.0


@dataclass
class Score:
    rms_m: float
    walk_rms_m: float
    switches: int
    covered: float

    def __str__(self) -> str:
        return (f"Fehler {self.rms_m * 100:4.1f} cm (gehend {self.walk_rms_m * 100:4.1f} cm), "
                f"Wechsel {self.switches}, abgedeckt {self.covered * 100:3.0f} %")


def run(make_tracker, update, scenario: Scenario, seeds=range(20)) -> Score:
    """Feed every report to a fresh tracker per seed. `update(tracker,
    points, now)` takes a report; the tracker's visible() and held(now)
    targets are what it shows."""
    errors, walk_errors, switches, covered, total = [], [], 0, 0, 0
    for seed in seeds:
        tracker = make_tracker()
        # A person's target, and a different one they have been nearest
        # to since when: a switch counts once it has lasted SETTLE_S —
        # two targets passing through the same point swap places for a
        # report or two without anybody losing their own.
        last_id: dict[int, int] = {}
        other: dict[int, tuple[int, float]] = {}
        for now, points, truth in reports(scenario, seed):
            update(tracker, points, now)
            shown = [t for t in tracker.visible() + tracker.held(now) if t.confirmed]
            # Each person with their nearest shown target within a metre.
            pairs = sorted((math.dist((p[0], p[1]), (t.x, t.y)), pi, t.id, t)
                           for pi, p in enumerate(truth) for t in shown)
            used_p, used_t = set(), set()
            for d, pi, tid, _t in pairs:
                if pi in used_p or tid in used_t or d > 1.0:
                    continue
                used_p.add(pi)
                used_t.add(tid)
                errors.append(d)
                if truth[pi][2]:
                    walk_errors.append(d)
                if pi not in last_id or last_id[pi] == tid:
                    last_id[pi] = tid
                    other.pop(pi, None)
                elif other.get(pi, (None,))[0] != tid:
                    other[pi] = (tid, now)
                elif now - other[pi][1] >= SETTLE_S:
                    switches += 1
                    last_id[pi] = tid
                    other.pop(pi)
            total += len(truth)
            covered += len(used_p)
    rms = math.sqrt(sum(e * e for e in errors) / len(errors)) if errors else float("nan")
    walk = math.sqrt(sum(e * e for e in walk_errors) / len(walk_errors)) if walk_errors else float("nan")
    return Score(rms, walk, switches, covered / total if total else 0.0)


if __name__ == "__main__":
    from app.tracking import SMOOTHING_NOISE_M, Tracker

    for sc in SCENARIOS:
        score = run(Tracker, lambda tr, pts, now: tr.update(pts, now, confirm_s=0.5,
                                                            noise=SMOOTHING_NOISE_M["normal"]), sc)
        print(f"{sc.name:28s} {score}")
