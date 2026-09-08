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


def crossing_rate(samples: list[dict], threshold: float) -> float:
    """Crossings per second of wall time.

    Per second, not per reading. Home Assistant sends a message when a
    value *changes*, and the movement score sits at exactly zero for long
    stretches — a twenty-minute recording had gaps up to 18.7 s with no
    message at all. A share of readings therefore counts a quiet minute
    that produced four readings the same as a busy minute that produced
    two hundred, which flatters exactly the periods that should look
    quiet. Measured on the same recordings, the per-second form separates
    the far-away case from the next-door one by a factor of 112 where the
    per-reading form managed 38.
    """
    scored = [row for row in samples if row.get("movement_score") is not None]
    if len(scored) < 2:
        return 0.0
    stamps = [row.get("t") or 0.0 for row in scored]
    span = max(stamps) - min(stamps)
    crossings = sum(row["movement_score"] >= threshold for row in scored)
    if span <= 0:
        return 0.0
    return crossings / span


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
    gives a confidently wrong one.
    """
    empty = [
        row for row in samples
        if row.get("label") == "empty" and row.get("movement_score") is not None
    ]
    if len(empty) < 30:
        return None

    windows = _split_windows(empty, window_seconds)
    rates = [crossing_rate(window, crossing_threshold) for window in windows]
    if len(rates) < MIN_BASELINE_WINDOWS:
        return None

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
        # A window far above the rest is the signature of someone being in
        # the room while the "empty" recording ran. The median survives it,
        # but the user should be told rather than left with a baseline that
        # quietly describes a room they were standing in.
        suspect_windows=sum(
            1 for rate in rates if rate > max(centre * 3.0, centre + 0.05)
        ),
        window_count=len(rates),
    )


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


def _split_windows(samples: list[dict], window_seconds: float) -> list[list[dict]]:
    """Consecutive, non-overlapping windows with enough readings to count.

    Non-overlapping on purpose: overlapping windows share samples, and a
    spread computed across them understates the real variability.
    """
    ordered = sorted(samples, key=lambda row: row.get("t") or 0.0)
    windows: list[list[dict]] = []
    current: list[dict] = []
    start = None
    for row in ordered:
        stamp = row.get("t") or 0.0
        if start is None:
            start = stamp
        if stamp - start >= window_seconds:
            if len(current) >= 5:
                windows.append(current)
            current, start = [], stamp
        current.append(row)
    if len(current) >= 5:
        windows.append(current)
    return windows


def evaluate(
    profile: RateProfile,
    recent: list[dict],
    *,
    occupied_now: bool = False,
) -> dict:
    """Is the room occupied, judged by how often it is crossing right now.

    `occupied_now` is the previous verdict: the enter and exit levels
    differ, so the answer depends on which side it is coming from.
    """
    scored = [row for row in recent if row.get("movement_score") is not None]
    rate = crossing_rate(scored, profile.crossing_threshold)
    ratio = rate / profile.baseline_rate if profile.baseline_rate else 0.0

    if len(scored) < 5:
        # Too little to judge. Saying "vacant" here would report an empty
        # room every time the connection hiccups.
        return {
            "available": False,
            "occupied": None,
            "rate": round(rate, 5),
            "ratio": round(ratio, 2),
            "samples": len(scored),
            "reason": "zu wenige Messwerte im Fenster",
        }

    level = profile.exit_rate if occupied_now else profile.enter_rate
    occupied = rate >= level
    return {
        "available": True,
        "occupied": occupied,
        "rate": round(rate, 5),
        "ratio": round(ratio, 2),
        "samples": len(scored),
        "reason": (
            f"{rate:.1%} der Messwerte über {profile.crossing_threshold:g}, "
            f"leerer Raum {profile.baseline_rate:.1%} — "
            f"Faktor {ratio:.1f}"
        ),
    }
