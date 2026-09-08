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
#: rate carries the signal and a lower bar collects more of it: 17.4 % of
#: readings against 8.7 % while occupied, and the empty-room rate rises
#: only from 3.0 % to 7.0 %.
DEFAULT_CROSSING_THRESHOLD = 1e-3

#: How many times the empty-room rate the window must reach before the
#: room counts as occupied, and how far it must fall to be released. The
#: gap between them is hysteresis: without it a rate hovering at the line
#: chatters. Measured ratio was about 4.7.
DEFAULT_ENTER_RATIO = 2.5
DEFAULT_EXIT_RATIO = 1.5

#: An empty room that never crosses would make every ratio infinite, and
#: one stray crossing would then read as presence. This floor is the
#: smallest baseline rate taken seriously.
MIN_BASELINE_RATE = 0.005


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
        """Above the empty room's mean *and* above its spread.

        Whichever is higher: a room whose empty rate is steady is judged
        by the ratio, a twitchy one by its own variability.
        """
        return max(
            self.baseline_rate * DEFAULT_ENTER_RATIO,
            self.baseline_rate + 2.0 * self.baseline_spread,
        )

    @property
    def exit_rate(self) -> float:
        return min(self.enter_rate, self.baseline_rate * DEFAULT_EXIT_RATIO)


def crossing_rate(samples: list[dict], threshold: float) -> float:
    """The share of readings at or above the threshold."""
    scores = [
        row["movement_score"]
        for row in samples
        if row.get("movement_score") is not None
    ]
    if not scores:
        return 0.0
    return sum(score >= threshold for score in scores) / len(scores)


def learn_baseline(
    samples: list[dict],
    *,
    crossing_threshold: float = DEFAULT_CROSSING_THRESHOLD,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
) -> RateProfile | None:
    """Derive a room's empty-state rate from its labelled `empty` samples.

    Returns None when there is not enough to be worth trusting: a single
    window says nothing about how much the rate varies, and the spread is
    half of what makes the enter level defensible.
    """
    empty = [
        row for row in samples
        if row.get("label") == "empty" and row.get("movement_score") is not None
    ]
    if len(empty) < 30:
        return None

    windows = _split_windows(empty, window_seconds)
    rates = [crossing_rate(window, crossing_threshold) for window in windows]
    if len(rates) < 2:
        return None

    # Median and MAD rather than mean and standard deviation. The first
    # real baseline recording had someone walk back into the room for its
    # last minute: three windows read 0.014, 0.038 and 0.217. The mean of
    # those is 0.090 and the standard deviation 0.111 — one contaminated
    # window in three moved the "empty" rate above every honest one and
    # pushed the enter level out of reach. The median is 0.038, which is
    # what the room actually does.
    centre = _median(rates)
    spread = _median([abs(rate - centre) for rate in rates]) * 1.4826
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
