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

import math
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

#: How long a device's rate hysteresis remembers across a data outage.
#:
#: Somebody sitting still through a short dropout should not have to move
#: again to be seen; somebody who left an hour ago should not still be
#: holding the room on evidence nobody has confirmed since. Two window
#: lengths is the compromise, and it is a decision rather than a side
#: effect of a cache key.
RATE_MEMORY_SECONDS = 120.0


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
    #: Which transport measured the material this was learned from — see
    #: app/samples.py. None on a profile learned before 0.13.6, and on
    #: material that carries no source; those are all Home Assistant
    #: readings, because that is the only path the live evaluation has
    #: ever taken. Recorded rather than versioned: an existing profile is
    #: still a correct answer to the question it was learned under, and
    #: bumping the version would throw every one of them away. What it
    #: buys is the mismatch check — the same room measured over a
    #: different transport is a different measurement.
    source: str | None = None

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
            "source": self.source,
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


#: Why a stored profile is not being used. Three different situations,
#: kept apart because they need three different things from the person:
#: recalibrate, fix the data, or nothing at all.
MISSING = "missing"
OUTDATED = "outdated"
MALFORMED = "malformed"
USABLE = "usable"


def _finite(value) -> float:
    """A float, or a refusal. NaN and the infinities are not numbers here.

    They survive float() and then make every comparison False, so a
    profile carrying one would read as a room that is never occupied.
    """
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{value!r} ist keine endliche Zahl")
    return number


def profile_status(data) -> str:
    """Whether a stored profile can be used, and if not, why.

    Every branch returns; nothing raises. This is read on the shared
    evaluation pass, and one device with a damaged profile must not take
    the other rooms down with it.
    """
    if not isinstance(data, dict) or not data:
        return MISSING
    try:
        version = int(data.get("version") or 1)
    except (TypeError, ValueError):
        # A version that is not a number says nothing about which
        # definition wrote this, so the profile cannot be trusted at all.
        return MALFORMED
    if version != PROFILE_VERSION:
        return OUTDATED
    return USABLE if _parse_profile(data) is not None else MALFORMED


def profile_outdated(data) -> bool:
    """True for a stored profile that is present but cannot be used."""
    return profile_status(data) in (OUTDATED, MALFORMED)


def _parse_profile(data: dict) -> RateProfile | None:
    """The strict half: shapes and ranges, or None."""
    try:
        window_seconds = _finite(data["window_seconds"])
        baseline_rate = _finite(data["baseline_rate"])
        crossing_threshold = _finite(data["crossing_threshold"])
    except (KeyError, TypeError, ValueError):
        return None

    # Ranges, not just types. A window of zero divides by zero downstream;
    # a negative baseline makes every ratio meaningless and every room
    # occupied.
    if window_seconds <= 0 or baseline_rate < 0 or crossing_threshold < 0:
        return None

    try:
        return RateProfile(
            crossing_threshold=crossing_threshold,
            baseline_rate=baseline_rate,
            baseline_spread=max(0.0, _finite(data.get("baseline_spread") or 0.0)),
            window_seconds=window_seconds,
            sample_count=max(0, int(data.get("sample_count") or 0)),
            observed_seconds=max(0.0, _finite(data.get("observed_seconds") or 0.0)),
            suspect_windows=max(0, int(data.get("suspect_windows") or 0)),
            window_count=max(0, int(data.get("window_count") or 0)),
            source=(str(data["source"]) if data.get("source") else None),
        )
    except (TypeError, ValueError):
        return None


def profile_from_dict(data) -> RateProfile | None:
    """Rebuild a profile from what as_dict() produced, or None if unusable.

    Never raises. Until 0.13.6 the version was converted outside the
    try block, so a stored `{"version": "broken"}` — a hand-edited file,
    a half-written record — raised ValueError out of the shared
    evaluation pass and took every zone with it.

    A profile from an older version is unusable on purpose. Version 1
    divided by the span between readings rather than by observed time, so
    its baseline is a number about a different measurement; carrying it
    forward would silently judge new data against an old definition.
    """
    if profile_status(data) != USABLE:
        return None
    return _parse_profile(data)


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
    """How much of [start, end) a live source can be shown to have covered.

    A reading proves the source was alive at that instant and says nothing
    about any other instant. What it is allowed to stand for either side
    of itself has to be bounded, or the arithmetic invents evidence: until
    0.13.6 this subtracted gaps over thirty seconds and counted everything
    else — including the time before the first reading and after the last
    — so five readings spanning four tenths of a second in the middle of a
    minute were reported as sixty seconds observed, and the same burst at
    the edge of the window was rejected. The same evidence, two answers,
    decided by where it happened to fall.

    So each reading covers `MAX_SAMPLE_GAP / 2` either side of itself, and
    the observed time is the union of those intervals clipped to the
    window. Two readings closer together than MAX_SAMPLE_GAP therefore
    join up — an ordinary quiet stretch (the longest measured in a real
    recording was 18.7 s) stays continuous — while a real hole opens a
    real hole, and a burst covers only what a burst can cover.

    A quiet room still reports: the movement score is derived from CSI,
    and even a living room at four in the morning produced 0.7 readings a
    second. A minute with no reading at all is not a quiet minute, it is a
    minute nobody was listening.
    """
    span = end - start
    if span <= 0:
        return 0.0
    stamps = sorted(
        stamp
        for stamp in (row.get("t") for row in samples)
        if isinstance(stamp, (int, float)) and math.isfinite(stamp)
    )
    if not stamps:
        return 0.0

    reach = MAX_SAMPLE_GAP / 2.0
    covered = 0.0
    open_from = open_to = None
    for stamp in stamps:
        low = max(start, stamp - reach)
        high = min(end, stamp + reach)
        if high <= low:
            continue
        if open_to is not None and low <= open_to:
            open_to = max(open_to, high)
            continue
        if open_to is not None:
            covered += open_to - open_from
        open_from, open_to = low, high
    if open_to is not None:
        covered += open_to - open_from
    return min(span, covered)


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


def label_segments(samples: list[dict]) -> list[tuple]:
    """Contiguous runs of one label, in time order.

    Windows must not be built across a label boundary. `learn_baseline`
    used to drop every non-empty reading first and then window what was
    left, so a recording labelled empty/still/empty/still every twenty
    seconds reported the full ten minutes as empty-room observation —
    two hundred seconds of somebody sitting there bridged away by the
    gap rule. Segmenting first makes those gaps what they are: the end of
    one stretch and the start of another.
    """
    ordered = sorted(
        (row for row in samples if isinstance(row.get("t"), (int, float))
         and math.isfinite(row["t"])),
        key=lambda row: row["t"],
    )
    segments: list[tuple] = []
    current: list[dict] = []
    label = None
    for row in ordered:
        if row.get("label") != label:
            if current:
                segments.append((label, current))
            label, current = row.get("label"), []
        current.append(row)
    if current:
        segments.append((label, current))
    return segments


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
    scored = [row for row in samples if row.get("movement_score") is not None]
    empty = [row for row in scored if row.get("label") == "empty"]
    if len(empty) < 30:
        return None

    # Segment first, window inside each segment. Filtering to "empty" and
    # windowing the remainder joined separate empty stretches across the
    # occupied minutes between them, and counted those minutes as empty
    # observation.
    windows = []
    for label, rows in label_segments(scored):
        if label == "empty":
            windows.extend(usable_windows(split_windows(rows, window_seconds)))
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
        # Only when the material agrees with itself. Mixed sources mean
        # this baseline describes no single measurement, and saying so is
        # more useful than picking one.
        source=_single_source(empty),
    )


def _single_source(rows: list[dict]) -> str | None:
    """The one transport these readings came from, or None."""
    seen = {row.get("source") for row in rows if row.get("source")}
    return seen.pop() if len(seen) == 1 else None


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
    scored = [
        row for row in recent
        if row.get("movement_score") is not None
        and isinstance(row.get("t"), (int, float))
        and math.isfinite(row["t"])
        and math.isfinite(row["movement_score"])
    ]
    stamps = [row["t"] for row in scored]
    if window is not None:
        span = window.duration
        observed = window.observed
    else:
        # The rolling window, stated rather than inferred. Taking the span
        # between the first and last reading made the window whatever the
        # data happened to fill, so a buffer holding four seconds of
        # readings was judged as a four-second window and passed. The
        # window is `window_seconds` ending at the newest reading; how
        # much of it was observed is then the same question replay asks.
        if stamps:
            end = max(stamps)
            start = end - profile.window_seconds
            span = min(profile.window_seconds, end - min(stamps))
            observed = observed_seconds(scored, start, end)
        else:
            span = observed = 0.0
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
    # One release rule, written once: the window has to have elapsed to
    # MIN_COVERAGE of its length, and MIN_COVERAGE of it has to have been
    # observed. Two different questions — has enough time passed, and was
    # anything listening while it did — and both have to be answered
    # before there is a verdict.
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


def advance(
    profile: RateProfile,
    rows: list[dict],
    memory: dict | None,
    *,
    now: float,
    source=None,
    window: "Window | None" = None,
    memory_seconds: float = RATE_MEMORY_SECONDS,
) -> tuple[bool | None, dict | None]:
    """One device's verdict, carrying its own hysteresis between calls.

    Returns `(verdict, memory)`. The verdict is True, False, or None when
    the rate cannot say; `memory` is opaque and belongs to the caller —
    hand back what came out last time and nothing else.

    This is the step both the live path and the replay runner take, and
    it is one function on purpose. `evaluate` alone is not the whole
    answer: which of the two levels applies depends on what this device
    said last time, and that memory has rules of its own. It is dropped
    when the profile moves (recalibrating asks a different question), when
    the source is replaced (a rebuilt subscription is different data), and
    when it goes stale (`memory_seconds`). An outage keeps it: a person
    sitting still through a dropout should not have to move again to be
    seen.

    A replay that reimplemented any of that would be measuring something
    the add-on does not do.
    """
    key = (profile.crossing_threshold, profile.baseline_rate, profile.window_seconds)
    if memory is not None and (
        memory.get("key") != key
        or memory.get("source") != source
        or now - memory.get("at", now) > memory_seconds
    ):
        memory = None

    previous = bool(memory["remembered"]) if memory else False
    result = evaluate(profile, rows, occupied_now=previous, window=window)
    verdict = bool(result["occupied"]) if result["available"] else None
    if verdict is None:
        return None, memory
    return verdict, {"key": key, "source": source, "remembered": verdict, "at": now}


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
