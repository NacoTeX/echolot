"""Presence from the rate of threshold crossings, not from single readings.

Measured on real hardware, one person sitting still on a couch against the
same room empty:

  * A single reading barely separates the two. AUC 0.616 — a coin flip
    with a lean. The Calibration Lab's own recommendation() said as much
    and reported a 98 % false-negative rate rather than inventing a
    threshold.
  * The empty room is not quiet. At threshold 0.5 it crosses on 3.0 % of
    readings against 8.7 % with someone sitting there. So a hold time is
    the wrong instrument: sixty seconds of hold reported the room occupied
    95 % of the time with a person in it — and 40 % of the time with
    nobody in it.
  * The rate of crossings over a window does separate them. Over sixty
    seconds: AUC 0.931, with 17.7 % of readings above 1e-3 while occupied
    against 3.8 % empty.

So presence is "the crossing rate over the last minute is well above the
rate this room shows when empty", and the empty-room recording is not
preparation for the calibration — it *is* the calibration.

The cost is honest and unavoidable: about a minute of latency. This does
not replace the existing motion path, which reacts in a second and is
what you want for turning on a light. It answers the other question, the
one motion cannot: is somebody still there.

Caveat carried from the data it was derived from: the 0.931 rests on
twelve windows against six, from one room, one device, one session. The
mechanism is sound; the numbers want more evidence.

Second caveat, added in 0.13.5: every number in this docstring was
computed under the *old* window rules, which divided by the span between
the first and last reading rather than by observed time, and accepted
windows that had never elapsed. They are kept because they are what the
design was derived from, not because they are current — the levels a
recording produces today are lower (the same baseline recording gives
0.067 rather than 0.084), and that is why a stored profile carries a
version.
"""

from dataclasses import dataclass

#: How far back the rate is measured. Sixty seconds is where separation
#: became convincing (AUC 0.705 at 15 s, 0.799 at 30 s, 0.931 at 60 s);
#: longer would separate better still and lag more.
DEFAULT_WINDOW_SECONDS = 60.0

#: A crossing counts below the device's own motion threshold, because the
#: rate carries the signal and a lower bar collects more of it: 0.261
#: crossings a second against 0.130 while occupied, while the empty room
#: rises only from 0.004 to 0.027.
DEFAULT_CROSSING_THRESHOLD = 1e-3

#: The baseline is the *worst* an empty room gets, not its typical value,
#: so this percentile of its window rates rather than the median. A clean
#: twenty-minute baseline had fourteen of twenty windows at exactly zero:
#: the median is 0.0 and says nothing, while the ninetieth percentile
#: (0.084) is the level the room reached on the few windows where
#: something happened. It also survives one contaminated window in twenty,
#: which a maximum would not.
BASELINE_PERCENTILE = 0.90

#: A high percentile needs enough windows to have anything to exclude. On
#: three windows the ninetieth percentile *is* the maximum, so a
#: two-and-a-half-minute baseline whose last minute was occupied produced
#: a "baseline" of 0.393 — the contaminated window itself. Ten windows is
#: the point at which the top decile is something the statistic can leave
#: out, and it is also an honest statement about the measurement: a room
#: cannot be characterised in three minutes.
MIN_BASELINE_WINDOWS = 10

#: How far above that the window must sit to count as occupied, and how
#: far it must fall to be released. The gap is hysteresis: without it a
#: rate at the line chatters. At the measured baseline of 0.084 this puts
#: the entry at 0.168 — above nineteen of twenty empty windows and below
#: three of four with someone sitting on the couch.
DEFAULT_ENTER_RATIO = 2.0
DEFAULT_EXIT_RATIO = 1.2

#: An absolute margin as well as a ratio, for rooms so quiet that a ratio
#: of a very small number is still a very small number.
ENTER_MARGIN = 0.05

#: An empty room that never crosses would make every ratio infinite, and
#: one stray crossing would then read as presence. This floor is the
#: smallest baseline rate taken seriously, in crossings a second. A clean
#: twenty-minute baseline — the room empty while someone moved about the
#: rest of the flat — measured 0.027.
#: Roughly one crossing a minute. Below that a "baseline" is indis-
#: tinguishable from silence, and a single stray crossing would read as
#: presence.
MIN_BASELINE_RATE = 0.02

#: The longest gap between two readings that still counts as the room
#: being quiet rather than as nobody listening. A quiet room still
#: reports — a living room at four in the morning produced 0.7 readings a
#: second — and the longest gap in any real recording here was 18.7 s.
#: Anything beyond this is a hole in the data, and time inside it is not
#: counted as observed.
MAX_SAMPLE_GAP = 30.0

#: How much of a window must have had a source behind it, and how many
#: readings it must hold, before it is a measurement rather than a
#: fragment.
MIN_COVERAGE = 0.8
MIN_SAMPLES_PER_WINDOW = 5

#: What a stored profile means. Version 1 divided by the span between the
#: first and last reading rather than by observed time, and accepted
#: windows that had never elapsed — so its baseline numbers describe a
#: different measurement and are not comparable with these. A profile
#: from before the change is refused rather than quietly reused; the
#: device stops contributing to rate-based presence until it is
#: recalibrated, which is the honest outcome.
PROFILE_VERSION = 2


@dataclass(frozen=True)
class RateProfile:
    """What an empty room does, learned from a labelled recording."""

    crossing_threshold: float
    baseline_rate: float
    #: How much the empty room's own rate varied between windows, so the
    #: enter level can sit above the noise rather than above the mean.
    baseline_spread: float
    window_seconds: float
    sample_count: int
    #: Seconds of the recording that actually had a source behind them.
    #: Absent from profiles written before 0.13.5, which is exactly why
    #: those cannot be trusted: they never measured it.
    observed_seconds: float = 0.0
    #: Windows that look occupied inside an "empty" recording.
    suspect_windows: int = 0
    window_count: int = 0

    def as_dict(self) -> dict:
        return {
            "crossing_threshold": self.crossing_threshold,
            "baseline_rate": round(self.baseline_rate, 5),
            "baseline_spread": round(self.baseline_spread, 5),
            "window_seconds": self.window_seconds,
            "sample_count": self.sample_count,
            "observed_seconds": round(self.observed_seconds, 1),
            "version": PROFILE_VERSION,
            "enter_rate": round(self.enter_rate, 5),
            "exit_rate": round(self.exit_rate, 5),
            "suspect_windows": self.suspect_windows,
            "window_count": self.window_count,
            "warning": self.warning,
        }

    @property
    def warning(self) -> str | None:
        if not self.suspect_windows:
            return None
        return (
            f"{self.suspect_windows} von {self.window_count} Fenstern dieser "
            "Leer-Aufnahme sehen belegt aus. War während der Aufzeichnung "
            "jemand im Raum? Der Mittelwert wäre dadurch unbrauchbar; der "
            "Median hält stand, aber eine saubere Aufnahme ist besser."
        )

    @property
    def enter_rate(self) -> float:
        """Clear of the empty room by a ratio *and* by an absolute margin.

        The ratio scales with a noisy room; the margin keeps a very quiet
        one from tripping on two stray crossings, where twice a small
        number is still a small number.
        """
        return max(
            self.baseline_rate * DEFAULT_ENTER_RATIO,
            self.baseline_rate + ENTER_MARGIN,
        )

    @property
    def exit_rate(self) -> float:
        return min(self.enter_rate, self.baseline_rate * DEFAULT_EXIT_RATIO)


def profile_outdated(data) -> bool:
    """True for a stored profile written under an older definition."""
    return isinstance(data, dict) and int(data.get("version") or 1) != PROFILE_VERSION


def profile_from_dict(data) -> RateProfile | None:
    """Rebuild a profile from what as_dict() produced, or None if unusable.

    A profile from an older version is unusable on purpose. Version 1
    divided by the span between readings rather than by observed time, so
    its baseline is a number about a different measurement; carrying it
    forward would silently judge new data against an old definition.
    """
    if not isinstance(data, dict):
        return None
    if profile_outdated(data):
        return None
    try:
        return RateProfile(
            crossing_threshold=float(data["crossing_threshold"]),
            baseline_rate=float(data["baseline_rate"]),
            baseline_spread=float(data.get("baseline_spread") or 0.0),
            window_seconds=float(data["window_seconds"]),
            sample_count=int(data.get("sample_count") or 0),
            observed_seconds=float(data.get("observed_seconds") or 0.0),
            suspect_windows=int(data.get("suspect_windows") or 0),
            window_count=int(data.get("window_count") or 0),
        )
    except (KeyError, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Window:
    """A fixed stretch of wall time, and what was actually observed in it.

    Windows used to be "however many samples happened to be adjacent",
    which made three separate questions look like one answer: how long
    the stretch was, how much of it anything was listening, and how many
    events fell inside. Ten bursts of five readings, each burst lasting
    0.4 s and sixty seconds apart, were accepted as ten one-minute
    baseline windows — 4 seconds of observation reported as ten minutes.
    """

    start: float
    end: float
    samples: list[dict]
    observed: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def coverage(self) -> float:
        """Fraction of the window that something was actually listening."""
        return self.observed / self.duration if self.duration > 0 else 0.0

    def rate(self, threshold: float) -> float:
        return event_rate(self.samples, threshold, observed=self.observed)


def observed_seconds(samples: list[dict], start: float, end: float) -> float:
    """How much of [start, end) had a live source behind it.

    A quiet room still reports: the movement score is a float derived from
    CSI, and even a living room at four in the morning produced 0.7
    readings a second. A minute with no reading at all is therefore not a
    quiet minute, it is a minute nobody was listening — so time inside a
    gap longer than MAX_SAMPLE_GAP is not counted as observed, while an
    ordinary quiet stretch (the longest measured was 18.7 s) still is.
    """
    span = end - start
    if span <= 0:
        return 0.0
    stamps = sorted(row.get("t") or 0.0 for row in samples)
    if not stamps:
        return 0.0

    missing = 0.0
    previous = start
    for stamp in stamps:
        gap = stamp - previous
        if gap > MAX_SAMPLE_GAP:
            missing += gap
        previous = stamp
    trailing = end - previous
    if trailing > MAX_SAMPLE_GAP:
        missing += trailing
    return max(0.0, span - missing)


def event_rate(samples: list[dict], threshold: float, *, observed: float | None = None) -> float:
    """Readings above `threshold` per second of *observed* wall time.

    Named for what it counts. It is not a count of threshold crossings —
    of transitions from below to above — it is a count of readings that
    were above it. Changing that would change what a profile means, so
    the measure is kept and called by its right name instead.

    Per second, not per reading. Home Assistant sends a message when a
    value *changes*, and the movement score sits at exactly zero for long
    stretches — a twenty-minute recording had gaps up to 18.7 s with no
    message at all. A share of readings therefore counts a quiet minute
    that produced four readings the same as a busy minute that produced
    two hundred, which flatters exactly the periods that should look
    quiet.

    `observed` is the divisor when given. Without it the span between the
    first and last reading is used, which is what this did until 0.13.5
    and is wrong for exactly the case that matters: five readings inside
    0.4 s divided by 0.4 s reported 12.5 events a second, from a fifth of
    a second of evidence.
    """
    scored = [row for row in samples if row.get("movement_score") is not None]
    if len(scored) < 2:
        return 0.0
    if observed is None:
        stamps = [row.get("t") or 0.0 for row in scored]
        observed = max(stamps) - min(stamps)
    if observed <= 0:
        return 0.0
    events = sum(row["movement_score"] >= threshold for row in scored)
    return events / observed


def split_windows(samples: list[dict], window_seconds: float) -> list[Window]:
    """Consecutive windows on a fixed grid from the first reading.

    A grid rather than "samples that happen to be adjacent": the point of
    a window is a known stretch of time, and only then what fell into it.
    A trailing part-window is dropped, because a rate from half a window
    is not comparable with a rate from a whole one.
    """
    stamped = sorted(
        (row for row in samples if row.get("t") is not None), key=lambda row: row["t"]
    )
    if len(stamped) < 2 or window_seconds <= 0:
        return []

    first, last = stamped[0]["t"], stamped[-1]["t"]
    windows: list[Window] = []
    start = first
    index = 0
    while start + window_seconds <= last:
        end = start + window_seconds
        inside = []
        while index < len(stamped) and stamped[index]["t"] < end:
            inside.append(stamped[index])
            index += 1
        windows.append(
            Window(start=start, end=end, samples=inside,
                   observed=observed_seconds(inside, start, end))
        )
        start = end
    return windows


def usable_windows(windows: list[Window]) -> list[Window]:
    """The windows with enough behind them to be a measurement."""
    return [
        window
        for window in windows
        if len(window.samples) >= MIN_SAMPLES_PER_WINDOW
        and window.coverage >= MIN_COVERAGE
    ]


def learn_baseline(
    samples: list[dict],
    *,
    crossing_threshold: float = DEFAULT_CROSSING_THRESHOLD,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
) -> RateProfile | None:
    """Derive a room's empty-state rate from its labelled `empty` samples.

    Returns None when there is not enough to be worth trusting. That is
    not a formality: with three windows the percentile lands on the worst
    of them, so a short baseline does not merely give a weak answer, it
    gives a confidently wrong one. Ten *observed* minutes, now, rather
    than ten groups of readings.
    """
    empty = [
        row for row in samples
        if row.get("label") == "empty" and row.get("movement_score") is not None
    ]
    if len(empty) < 30:
        return None

    windows = usable_windows(split_windows(empty, window_seconds))
    if len(windows) < MIN_BASELINE_WINDOWS:
        return None

    rates = [window.rate(crossing_threshold) for window in windows]

    # Median and MAD rather than mean and standard deviation. The first
    # real baseline recording had someone walk back into the room for its
    # last minute: three windows read 0.014, 0.038 and 0.217. The mean of
    # those is 0.090 and the standard deviation 0.111 — one contaminated
    # window in three moved the "empty" rate above every honest one and
    # pushed the enter level out of reach. The median is 0.038, which is
    # what the room actually does.
    centre = _percentile(rates, BASELINE_PERCENTILE)
    spread = _median([abs(rate - _median(rates)) for rate in rates]) * 1.4826
    return RateProfile(
        crossing_threshold=crossing_threshold,
        baseline_rate=max(centre, MIN_BASELINE_RATE),
        baseline_spread=spread,
        window_seconds=window_seconds,
        sample_count=len(empty),
        observed_seconds=sum(window.observed for window in windows),
        # A window far above the rest is the signature of someone being in
        # the room while the "empty" recording ran. The median survives it,
        # but the user should be told rather than left with a baseline that
        # quietly describes a room they were standing in.
        suspect_windows=sum(
            1 for rate in rates if rate > max(centre * 3.0, centre + 0.05)
        ),
        window_count=len(rates),
    )


def evaluate(
    profile: RateProfile,
    recent: list[dict],
    *,
    occupied_now: bool = False,
    window: "Window | None" = None,
) -> dict:
    """Is the room occupied, judged by how often it is reporting right now.

    `occupied_now` is the previous verdict: the enter and exit levels
    differ, so the answer depends on which side it is coming from.

    `window` is passed when the caller already knows the stretch of time
    this is about — analysing a recording, where the windows come off a
    fixed grid. Without it the span is taken from the readings, which is
    right for the live rolling buffer: it holds exactly what has arrived,
    so how far it reaches back *is* how long there has been anything to
    judge. Deriving the span from the readings in the fixed-grid case
    instead would reject a perfectly observed minute whose readings
    happen to start late in it.
    """
    scored = [row for row in recent if row.get("movement_score") is not None]
    stamps = [row.get("t") or 0.0 for row in scored]
    if window is not None:
        span = window.duration
        observed = window.observed
    else:
        span = (max(stamps) - min(stamps)) if len(stamps) >= 2 else 0.0
        observed = (
            observed_seconds(scored, min(stamps), max(stamps)) if len(stamps) >= 2 else 0.0
        )
    rate = event_rate(scored, profile.crossing_threshold, observed=observed)
    ratio = rate / profile.baseline_rate if profile.baseline_rate else 0.0

    base = {
        "rate": round(rate, 5),
        "ratio": round(ratio, 2),
        "samples": len(scored),
        "observed_seconds": round(observed, 1),
        "coverage": round(observed / profile.window_seconds, 2) if profile.window_seconds else 0.0,
    }

    # A burst is not a window. Five readings inside four tenths of a second
    # used to be accepted and reported 12.5 events a second — a confident
    # answer from a fifth of a second of evidence. The window has to have
    # actually elapsed, and something has to have been listening for most
    # of it, before there is anything to judge.
    if len(scored) < MIN_SAMPLES_PER_WINDOW or span < profile.window_seconds * MIN_COVERAGE:
        return {
            **base,
            "available": False,
            "occupied": None,
            "state": "warming_up",
            "reason": (
                f"Erst {span:.0f} s beobachtet, gebraucht werden "
                f"{profile.window_seconds:.0f} s"
            ),
        }
    if observed < profile.window_seconds * MIN_COVERAGE:
        return {
            **base,
            "available": False,
            "occupied": None,
            "state": "gap",
            "reason": (
                f"Nur {observed:.0f} s von {profile.window_seconds:.0f} s hatten "
                "eine Datenquelle — das ist eine Lücke, keine Ruhe"
            ),
        }

    level = profile.exit_rate if occupied_now else profile.enter_rate
    return {
        **base,
        "available": True,
        "occupied": rate >= level,
        "state": "ok",
        # Events per second of wall time, not a share of the readings —
        # the old wording said "% der Messwerte", which turned 12.5/s into
        # "1250 % der Messwerte". Same number, false sentence.
        "reason": (
            f"{rate:.3f} Ereignisse/s über {profile.crossing_threshold:g}, "
            f"leerer Raum {profile.baseline_rate:.3f}/s — "
            f"Faktor {ratio:.1f}"
        ),
    }


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2
