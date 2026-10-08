"""A home for the learners to learn from: people, a radar, its ghosts.

Deterministic for a seed. Nothing here is a model of the LD2460's
physics; it is a model of what the module's reports look like to
Echolot, built from what the hardware test and the manual show:

  * Up to five positions per report, in the module's own metres (x
    across, y ahead), a decimetre of scatter, ten reports a second.
  * A person walking is reported nearly every time.
  * A person sitting or lying still is lost again and again — reported
    in bursts, with gaps of a second to tens of seconds between them.
  * Fixed reflectors (a radiator, a mirror) show up as targets in one
    place, in bursts, whether anybody is home or not.
  * Nobody home: Home Assistant says so, and the radar still sees the
    reflectors.

The truth comes along: where the sensor really hangs, where everybody
is, whether anybody is home — so a test can say whether what was
learned is right.
"""

import math
import random
from dataclasses import dataclass, field


@dataclass
class Placement:
    """Where the sensor hangs in the room, as geometry.to_room takes it."""

    x: float
    y: float
    angle: float
    mirror: bool = False

    def as_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "angle": self.angle, "mirror": self.mirror}


def to_sensor(px: float, py: float, p: Placement) -> tuple[float, float]:
    """Room point -> what the module reports (the inverse of to_room
    without a sensor model)."""
    a = math.radians(p.angle)
    dx, dy = px - p.x, py - p.y
    xm = dx * math.cos(a) + dy * math.sin(a)
    ym = -dx * math.sin(a) + dy * math.cos(a)
    return (-xm if p.mirror else xm), ym


@dataclass
class Ghost:
    """A fixed reflector, in the module's own metres."""

    x: float
    y: float
    #: Share of the time it is reported, and how long one burst lasts.
    share: float = 0.3
    burst_s: tuple[float, float] = (2.0, 20.0)
    scatter: float = 0.05


@dataclass
class Place:
    """Somewhere a person goes and stays, in room metres."""

    name: str
    x: float
    y: float
    #: How long somebody stays, seconds (min, max).
    stay_s: tuple[float, float] = (600.0, 2400.0)
    #: While still: bursts reported, and the gaps between them, seconds.
    burst_s: tuple[float, float] = (3.0, 40.0)
    gap_s: tuple[float, float] = (0.5, 25.0)
    weight: float = 1.0


@dataclass
class Home:
    width: float
    height: float
    outline: list | None
    door: tuple[float, float]
    places: list[Place]
    sensor: Placement
    ghosts: list[Ghost] = field(default_factory=list)
    #: Visits: (arrive at, leave at) in seconds from the start, one per
    #: person-stay in the house; outside of all of them nobody is home.
    home_periods: list[tuple[float, float]] = field(default_factory=list)
    #: People Home Assistant does not know about — a child asleep with a
    #: babysitter — and pets: there, but the house counts as empty.
    hidden_periods: list[tuple[float, float]] = field(default_factory=list)
    pet_periods: list[tuple[float, float]] = field(default_factory=list)
    #: Where the hidden go; None: the same places as everybody.
    hidden_places: list | None = None
    rate_hz: float = 10.0
    noise_m: float = 0.06
    walk_m_s: float = 1.0
    #: Reported while walking.
    walk_hit: float = 0.95
    #: The module's reach and field, in its own frame.
    range_m: float = 6.0
    fov_deg: float = 120.0


@dataclass
class Truth:
    home: bool
    people: list[tuple[float, float]]
    still: list[bool]


class _Person:
    def __init__(self, home: Home, rng: random.Random, start: float, end: float, places: list | None = None,
                 speed: float | None = None):
        self.home = home
        self.rng = rng
        self.start, self.end = start, end
        self.places = places or home.places
        self.speed = speed or home.walk_m_s
        # A plan: door -> place -> place ... -> door.
        self.legs: list[tuple[float, str, tuple]] = []
        t = start
        here = home.door
        while t < end:
            place = self._pick()
            target = (place.x + rng.uniform(-0.1, 0.1), place.y + rng.uniform(-0.1, 0.1))
            dist = math.hypot(target[0] - here[0], target[1] - here[1])
            walk = dist / self.speed
            self.legs.append((t, "walk", (here, target, walk)))
            t += walk
            stay = rng.uniform(*place.stay_s)
            stay = min(stay, max(0.0, end - t - 8.0))
            if stay > 5:
                self.legs.append((t, "still", (target, stay, place)))
                t += stay
            here = target
            if end - t < 60:
                break
        dist = math.hypot(home.door[0] - here[0], home.door[1] - here[1])
        self.legs.append((t, "walk", (here, home.door, dist / self.speed)))
        self.done = t + dist / self.speed
        self._burst_until = 0.0
        self._gap_until = 0.0

    def _pick(self) -> Place:
        weights = [p.weight for p in self.places]
        return self.rng.choices(self.places, weights=weights)[0]

    def at(self, t: float):
        """(x, y, still, place) or None when not in the room."""
        if t < self.start or t >= self.done:
            return None
        for i, (t0, kind, data) in enumerate(self.legs):
            t1 = self.legs[i + 1][0] if i + 1 < len(self.legs) else self.done
            if t0 <= t < t1:
                if kind == "walk":
                    (ax, ay), (bx, by), dur = data
                    f = 0.0 if dur <= 0 else min(1.0, (t - t0) / dur)
                    return ax + (bx - ax) * f, ay + (by - ay) * f, False, None
                (x, y), _stay, place = data
                return x, y, True, place
        return None

    def reported(self, t: float, still: bool, place: Place | None) -> bool:
        """Whether this report carries the person."""
        if not still:
            return self.rng.random() < self.home.walk_hit
        # Bursts and gaps, as a still person is lost and found again.
        if t < self._burst_until:
            return True
        if t < self._gap_until:
            return False
        self._burst_until = t + self.rng.uniform(*place.burst_s)
        self._gap_until = self._burst_until + self.rng.uniform(*place.gap_s)
        return True


class Simulation:
    """Reports, as (time, [(x, y) in module metres]), and the truth."""

    def __init__(self, home: Home, seed: int = 1):
        self.home = home
        self.rng = random.Random(seed)
        self.people = [_Person(home, random.Random(seed * 1000 + i), a, b)
                       for i, (a, b) in enumerate(home.home_periods)]
        self.hidden = [_Person(home, random.Random(seed * 2000 + i), a, b, places=home.hidden_places)
                       for i, (a, b) in enumerate(home.hidden_periods)]
        # A pet: wanders from spot to spot, never for long.
        rng = random.Random(seed * 3000)
        spots = [Place(f"p{i}", rng.uniform(0.5, home.width - 0.5), rng.uniform(0.5, home.height - 0.5),
                       stay_s=(5.0, 60.0), burst_s=(2.0, 10.0), gap_s=(0.5, 5.0)) for i in range(6)]
        self.pets = [_Person(home, random.Random(seed * 4000 + i), a, b, places=spots, speed=0.6)
                     for i, (a, b) in enumerate(home.pet_periods)]
        self._ghost_until = [0.0] * len(home.ghosts)
        self._ghost_on = [False] * len(home.ghosts)

    def _visible(self, xm: float, ym: float) -> bool:
        if ym <= 0:
            return False
        if math.hypot(xm, ym) > self.home.range_m:
            return False
        return abs(math.degrees(math.atan2(xm, ym))) <= self.home.fov_deg / 2

    def is_home(self, t: float) -> bool:
        return any(p.start <= t < p.done for p in self.people)

    def run(self, duration_s: float, start: float = 0.0):
        dt = 1.0 / self.home.rate_hz
        t = start
        noise = self.home.noise_m
        while t < start + duration_s:
            points = []
            positions, stills = [], []
            for person in self.people + self.hidden + self.pets:
                where = person.at(t)
                if where is None:
                    continue
                x, y, still, place = where
                positions.append((x, y))
                stills.append(still)
                if person.reported(t, still, place):
                    xm, ym = to_sensor(x, y, self.home.sensor)
                    if self._visible(xm, ym):
                        points.append((xm + self.rng.gauss(0, noise), ym + self.rng.gauss(0, noise)))
            for i, g in enumerate(self.home.ghosts):
                if t >= self._ghost_until[i]:
                    # On and off by turns, each off spell as long as the
                    # share asks for: on for `share` of the time.
                    on = not self._ghost_on[i]
                    self._ghost_on[i] = on
                    span = self.rng.uniform(*g.burst_s)
                    self._ghost_until[i] = t + (span if on else span * (1 - g.share) / max(g.share, 1e-6))
                if self._ghost_on[i]:
                    points.append((g.x + self.rng.gauss(0, g.scatter), g.y + self.rng.gauss(0, g.scatter)))
            yield t, points[:5], Truth(self.is_home(t), positions, stills)
            t += dt


def living_room(sensor: Placement | None = None, *, hours: float = 6.0, home: list | None = None,
                ghosts: list | None = None, busy: bool = False) -> Home:
    """The room of the hardware test: 3.9 × 5 m, a strip cut off on the
    left, a door top-left, sofa bottom-right, desk top-right.

    `home`: (from, to) seconds somebody Home Assistant knows is home; by
    default two long stretches with an empty house in between. `busy`:
    short stays, so a few hours hold as many walks as days would."""
    outline = [[0, 0], [3.9, 0], [3.9, 5], [0.35, 5], [0.35, 1.1], [0, 1.1]]
    short = (30.0, 120.0) if busy else None
    places = [
        Place("sofa", 3.2, 3.6, stay_s=short or (900, 3600), burst_s=(3, 30), gap_s=(0.5, 30), weight=3),
        Place("desk", 2.7, 1.0, stay_s=short or (600, 2400), burst_s=(5, 40), gap_s=(0.5, 12), weight=2),
        Place("tv", 1.0, 3.4, stay_s=short or (60, 300), burst_s=(5, 20), gap_s=(0.5, 5), weight=1),
    ]
    sensor = sensor or Placement(3.9, 5.0, 135.0)
    # Home in two long stretches by default, away in between.
    periods = [(0.0, hours * 3600 * 0.4), (hours * 3600 * 0.6, hours * 3600)] if home is None else home
    if ghosts is None:
        ghosts = [Ghost(0.9, 2.6, share=0.35), Ghost(-1.4, 3.8, share=0.15)]
    return Home(width=3.9, height=5.0, outline=outline, door=(0.6, 0.3), places=places, sensor=sensor,
                ghosts=ghosts, home_periods=periods)


def bedroom(*, home: list, hidden: list = (), pets: list = (), ghosts: list | None = None) -> Home:
    """A 3.5 × 4 m child's room: bed along the right wall, door top-left,
    the sensor on the bottom wall looking up the room. Whoever is `hidden`
    goes to bed and stays there for hours; whoever is home sits on the
    chair by the desk for a while."""
    chair = Place("chair", 0.9, 2.6, stay_s=(300, 600), burst_s=(3, 20), gap_s=(1, 10))
    bed = Place("bed", 2.9, 1.8, stay_s=(9000, 10800), burst_s=(3, 30), gap_s=(1, 40))
    sensor = Placement(1.75, 4.0, 180.0)
    return Home(width=3.5, height=4.0, outline=None, door=(0.5, 0.2), places=[chair], sensor=sensor,
                ghosts=list(ghosts or []), home_periods=list(home), hidden_periods=list(hidden),
                pet_periods=list(pets), hidden_places=[bed])
