"""Run recorded sessions through several detectors and compare them.

The review asks for new algorithms to be measured in comparison mode
before they touch anything, and it is right to: the existing numbers came
from one room over a few sessions, and a change that improves one of them
can quietly ruin another. This replays material that already exists —
recordings and recorder imports — through a set of strategies at once and
reports what each would have decided.

It is read-only. It never touches a zone runtime, a device profile or the
live evaluator, so running it cannot change what the lights do.

Two honesty rules are built into the output rather than left to the
reader:

  * A strategy that learns its baseline from the same session it is then
    judged on is measuring itself. That is reported as `in_sample: true`,
    and `baseline_session` exists so it does not have to be.
  * Windows that could not be judged — too short, or a hole in the data —
    are counted and reported, not dropped. A success rate that hides its
    unknowns is not a success rate.
"""

import statistics

from app import presence_rate, zone_logic

#: Window lengths to compare, in seconds. Sixty is what ships; the others
#: are the multi-timescale candidates the review suggests looking at.
#: Short windows can react sooner, long ones steady a quiet room.
DEFAULT_WINDOWS = (15.0, 30.0, 60.0, 120.0)

#: Labels that mean somebody was in the room.
OCCUPIED_LABELS = ("still", "moving")


def _judge_rate(rows: list[dict], profile) -> dict:
    """Per-window verdicts for one label under one rate profile."""
    windows = presence_rate.split_windows(rows, profile.window_seconds)
    judged = occupied = unusable = 0
    for window in windows:
        verdict = presence_rate.evaluate(profile, window.samples, window=window)
        if not verdict["available"]:
            unusable += 1
            continue
        judged += 1
        occupied += bool(verdict["occupied"])
    return {"windows": judged, "occupied": occupied, "unusable": unusable}


def _judge_motion(rows: list[dict], window_seconds: float) -> dict:
    """The device's own motion boolean, windowed the same way.

    The reference every rate strategy has to beat, and the one that is
    already driving the lights.
    """
    windows = presence_rate.split_windows(rows, window_seconds)
    judged = occupied = unusable = 0
    for window in windows:
        seen = [row.get("motion") for row in window.samples if row.get("motion") is not None]
        if not seen:
            unusable += 1
            continue
        judged += 1
        occupied += any(seen)
    return {"windows": judged, "occupied": occupied, "unusable": unusable}


def _by_label(samples: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in samples:
        grouped.setdefault(row.get("label") or "unlabelled", []).append(row)
    return grouped


def _score(labels: dict) -> dict:
    """What the per-label counts mean, in the terms the review asks for.

    False alarms per empty window and misses per occupied window, with
    the unjudgeable windows reported beside them rather than folded in.
    """
    empty = labels.get("empty", {})
    false_alarms = empty.get("occupied", 0)
    empty_windows = empty.get("windows", 0)

    occupied_windows = misses = 0
    for label in OCCUPIED_LABELS:
        stats = labels.get(label)
        if not stats:
            continue
        occupied_windows += stats["windows"]
        misses += stats["windows"] - stats["occupied"]

    unusable = sum(stats.get("unusable", 0) for stats in labels.values())
    return {
        "empty_windows": empty_windows,
        "false_alarms": false_alarms,
        "false_alarm_rate": round(false_alarms / empty_windows, 3) if empty_windows else None,
        "occupied_windows": occupied_windows,
        "misses": misses,
        "miss_rate": round(misses / occupied_windows, 3) if occupied_windows else None,
        "unusable_windows": unusable,
    }


def compare(
    samples: list[dict],
    *,
    baseline_samples: list[dict] | None = None,
    windows: tuple = DEFAULT_WINDOWS,
) -> dict:
    """Replay `samples` through every strategy and report each verdict.

    `baseline_samples` is the session the empty-room rate is learned from.
    Leaving it out means learning from the material being judged, which
    is reported rather than hidden.
    """
    in_sample = baseline_samples is None
    learn_from = samples if in_sample else baseline_samples
    grouped = _by_label(samples)

    strategies = []
    for window_seconds in windows:
        profile = presence_rate.learn_baseline(learn_from, window_seconds=window_seconds)
        entry = {
            "name": f"rate_{window_seconds:g}s",
            "kind": "rate",
            "window_seconds": window_seconds,
            "reference": window_seconds == presence_rate.DEFAULT_WINDOW_SECONDS,
        }
        if profile is None:
            entry.update(
                available=False,
                reason=(
                    "Zu wenig mit „Raum leer“ markiertes Material für ein Fenster "
                    f"von {window_seconds:g} s"
                ),
            )
            strategies.append(entry)
            continue
        labels = {label: _judge_rate(rows, profile) for label, rows in grouped.items()}
        entry.update(
            available=True,
            profile={
                "baseline_rate": round(profile.baseline_rate, 5),
                "enter_rate": round(profile.enter_rate, 5),
                "exit_rate": round(profile.exit_rate, 5),
                "window_count": profile.window_count,
                "observed_seconds": round(profile.observed_seconds, 1),
            },
            labels=labels,
            score=_score(labels),
        )
        strategies.append(entry)

    motion_labels = {
        label: _judge_motion(rows, presence_rate.DEFAULT_WINDOW_SECONDS)
        for label, rows in grouped.items()
    }
    strategies.append(
        {
            "name": "motion_only",
            "kind": "motion",
            "window_seconds": presence_rate.DEFAULT_WINDOW_SECONDS,
            "reference": False,
            "available": True,
            "labels": motion_labels,
            "score": _score(motion_labels),
        }
    )

    return {
        # Named, because it is not a simulation: it groups by label first
        # and scores each label's windows on their own. `simulate` below
        # is the chronological run through the productive path.
        "kind": "window_comparison",
        "in_sample": in_sample,
        "caveat": (
            "Maßstab und Bewertung stammen aus derselben Aufnahme — die Zahlen "
            "messen sich selbst und sind kein Nachweis. Für einen echten "
            "Vergleich eine zweite Sitzung als Maßstab angeben."
            if in_sample
            else "Maßstab aus einer anderen Sitzung als die Bewertung."
        ),
        "sample_count": len(samples),
        "labels_present": sorted(grouped),
        "strategies": strategies,
    }


# ----------------------------------------------------------------------
# The chronological runner (review R6)
# ----------------------------------------------------------------------
#
# `compare()` above is a *window comparison*: it groups by label first and
# then scores each label's windows on their own. That is a useful summary
# and it stays, but it is not a simulation of what the add-on does. Three
# things are different, and all three matter:
#
#   * Grouping by label first destroys the order of the session. A window
#     could span two separate stretches that happened to carry the same
#     label, with everything in between removed.
#   * Every window was judged with `occupied_now=False`, so the exit
#     level — the whole reason there are two levels — was never used.
#   * `motion_only` was `any(motion)` per minute, not the product's
#     actual interplay of motion, rate and hold time.
#
# So this runs the recording forward through the *same* functions the
# live path takes: `presence_rate.advance` for the device's verdict and
# its hysteresis memory, `zone_logic.evaluate` for the zone. The clock is
# injected — it is the recording's own timestamps — and labels are read
# afterwards, to score what happened, never to reorder the input.

#: How often the runner looks when nothing has arrived, in seconds.
#: Matches `main.HOLDING_INTERVAL`: a hold time running out has no event
#: behind it, so the live loop looks on a timer too.
DEFAULT_TICK_SECONDS = 1.0


def _rows_until(samples: list[dict], index: int, now: float, window_seconds: float) -> list[dict]:
    """The rolling window as `live_presence.DeviceStream.window` builds it."""
    cutoff = now - window_seconds
    start = index
    while start > 0 and samples[start - 1]["t"] >= cutoff:
        start -= 1
    return samples[start:index]


def _steps(samples: list[dict], tick_seconds: float) -> list[float]:
    """Every moment the live path would have looked at, in order.

    A reading is an event and wakes the evaluation; between events the
    loop still ticks, because a hold time expires on nobody's event.
    """
    stamps = sorted({row["t"] for row in samples})
    if not stamps:
        return []
    moments = []
    previous = None
    for stamp in stamps:
        if previous is not None and tick_seconds > 0:
            filler = previous + tick_seconds
            while filler < stamp:
                moments.append(filler)
                filler += tick_seconds
        moments.append(stamp)
        previous = stamp
    return moments


def _label_at(segments: list[tuple], moment: float) -> str | None:
    for label, start, end in segments:
        if start <= moment <= end:
            return label
    return None


def simulate(
    samples: list[dict],
    profile,
    *,
    hold_seconds: float = 0.0,
    enter_threshold: float | None = None,
    exit_threshold: float | None = None,
    tick_seconds: float = DEFAULT_TICK_SECONDS,
    use_rate: bool = True,
    include_timeline: bool = False,
) -> dict:
    """Replay the recording chronologically through the productive path.

    `use_rate=False` runs the same machine with the crossing rate
    switched off — the honest motion reference, with hysteresis and hold
    time in place, rather than `any(motion)` over a minute.

    Returns the state transitions, a complete time budget, and the
    metrics the review asks for: time wrongly occupied, time wrongly
    empty, and how long entry and release took. `include_timeline` adds
    the per-step record — thousands of rows for a long recording, so it
    is off in the API response and on when something wants to look at
    individual moments.
    """
    ordered = sorted(
        (row for row in samples if isinstance(row.get("t"), (int, float))),
        key=lambda row: row["t"],
    )
    if not ordered:
        return {
            "available": False,
            "reason": "Die Aufnahme enthält keine Messwerte mit Zeitstempel",
        }

    first, last = ordered[0]["t"], ordered[-1]["t"]
    total = last - first
    runtime = zone_logic.ZoneRuntime()
    memory = None
    index = 0
    timeline: list[dict] = []
    transitions: list[dict] = []
    previous_state = None

    for moment in _steps(ordered, tick_seconds):
        while index < len(ordered) and ordered[index]["t"] <= moment:
            index += 1
        window_rows = _rows_until(ordered, index, moment, profile.window_seconds)

        rate_state = "off"
        verdict = None
        if use_rate:
            verdict, memory = presence_rate.advance(
                profile, window_rows, memory, now=moment, source="replay",
                # The same window contract as the live path: the window
                # ends at the tick, not at the newest reading in it.
                # Without this the two drifted apart the moment the live
                # path started ending its window at the clock — replay
                # judged a stretch as soon as the data filled it, live
                # waited for the time to pass.
                window_end=moment,
            )
            rate_state = presence_rate.evaluate(
                profile, window_rows, occupied_now=bool(verdict), window_end=moment
            )["state"]

        current = ordered[index - 1] if index else None
        result = zone_logic.evaluate(
            runtime,
            motion=bool(current and current.get("motion")),
            score=current.get("movement_score") if current else None,
            enter_threshold=enter_threshold,
            exit_threshold=exit_threshold,
            hold_seconds=hold_seconds,
            now=moment,
            rate_occupied=verdict,
        )
        timeline.append(
            {"t": moment, "state": result.state, "occupied": result.occupied,
             "rate_state": rate_state, "rate_occupied": verdict}
        )
        if result.state != previous_state:
            transitions.append(
                {"t": moment, "from": previous_state, "to": result.state,
                 "trigger": result.trigger}
            )
            previous_state = result.state

    budget = _time_budget(timeline, first, last)
    # (label, start, end) rather than (label, rows): the runner scores by
    # the moment it looked, not by which reading it looked at.
    segments = [
        (label, rows[0]["t"], rows[-1]["t"])
        for label, rows in presence_rate.label_segments(ordered)
        if rows
    ]
    return {
        "available": True,
        "labels_present": sorted({label for label, _, _ in segments if label}),
        "started_at": first,
        "ended_at": last,
        "total_seconds": round(total, 1),
        "tick_seconds": tick_seconds,
        "hold_seconds": hold_seconds,
        "enter_threshold": enter_threshold,
        "exit_threshold": exit_threshold,
        "uses_rate": use_rate,
        "budget": budget,
        "transitions": transitions,
        "accuracy": _accuracy(timeline, segments, first, last),
        **({"timeline": timeline} if include_timeline else {}),
    }


def _time_budget(timeline: list[dict], first: float, last: float) -> dict:
    """Where every second of the recording went. The parts must sum.

    `split_windows` drops a trailing part-window before anything counts
    it, so a ten-second recording used to report zero judged windows and
    zero unusable ones — the time simply was not in the report. Here it
    is: whatever the runner never covered is `remainder`, named and
    added up with the rest.
    """
    buckets = {"occupied": 0.0, "empty": 0.0, "warming_up": 0.0,
               "gap": 0.0, "unknown": 0.0, "remainder": 0.0}
    if not timeline:
        buckets["remainder"] = round(max(0.0, last - first), 1)
        return {**buckets, "total_seconds": round(max(0.0, last - first), 1)}

    buckets["remainder"] += timeline[0]["t"] - first
    for entry, following in zip(timeline, timeline[1:] + [None]):
        end = following["t"] if following else last
        span = max(0.0, end - entry["t"])
        if entry["occupied"]:
            buckets["occupied"] += span
        elif entry["rate_state"] in ("ok", "off"):
            buckets["empty"] += span
        elif entry["rate_state"] in ("warming_up", "gap"):
            buckets[entry["rate_state"]] += span
        else:
            buckets["unknown"] += span
    return {
        **{key: round(value, 1) for key, value in buckets.items()},
        "total_seconds": round(max(0.0, last - first), 1),
    }


def _accuracy(timeline: list[dict], segments: list[tuple], first: float, last: float) -> dict:
    """What it got wrong, in seconds, and how long it took to be right.

    Counted against the labels the person supplied, which is the only
    ground truth this project has. Unlabelled stretches are excluded from
    both totals rather than counted as either.
    """
    occupied_labels = set(OCCUPIED_LABELS)
    wrong_occupied = wrong_empty = labelled_empty = labelled_occupied = 0.0
    for entry, following in zip(timeline, timeline[1:] + [None]):
        end = following["t"] if following else last
        span = max(0.0, end - entry["t"])
        label = _label_at(segments, entry["t"])
        if label == "empty":
            labelled_empty += span
            if entry["occupied"]:
                wrong_occupied += span
        elif label in occupied_labels:
            labelled_occupied += span
            if not entry["occupied"]:
                wrong_empty += span

    entry_latencies = []
    release_latencies = []
    for label, start, end in segments:
        if label in occupied_labels:
            became = next(
                (row["t"] for row in timeline if row["t"] >= start and row["occupied"]), None
            )
            if became is not None and became <= end:
                entry_latencies.append(became - start)
        elif label == "empty":
            cleared = next(
                (row["t"] for row in timeline if row["t"] >= start and not row["occupied"]), None
            )
            if cleared is not None and cleared <= end:
                release_latencies.append(cleared - start)

    def summary(values):
        if not values:
            return None
        return {"count": len(values), "median": round(statistics.median(values), 1),
                "worst": round(max(values), 1)}

    return {
        "labelled_empty_seconds": round(labelled_empty, 1),
        "labelled_occupied_seconds": round(labelled_occupied, 1),
        "false_occupied_seconds": round(wrong_occupied, 1),
        "false_empty_seconds": round(wrong_empty, 1),
        "false_occupied_share": (
            round(wrong_occupied / labelled_empty, 3) if labelled_empty else None
        ),
        "false_empty_share": (
            round(wrong_empty / labelled_occupied, 3) if labelled_occupied else None
        ),
        "entry_latency": summary(entry_latencies),
        "release_latency": summary(release_latencies),
    }


class BaselineRefused(Exception):
    """The proposed baseline cannot stand as an independent measure."""


def _overlaps(one: dict, other: dict) -> bool:
    a_start, a_end = one.get("started_at"), one.get("ended_at")
    b_start, b_end = other.get("started_at"), other.get("ended_at")
    if None in (a_start, a_end, b_start, b_end):
        return False
    return a_start <= b_end and b_start <= a_end


def report(
    session: dict,
    samples: list[dict],
    *,
    baseline: dict | None = None,
    baseline_samples: list[dict] | None = None,
    transfer: bool = False,
    hold_seconds: float = 0.0,
    windows: tuple = DEFAULT_WINDOWS,
) -> dict:
    """Everything replay can say about one session, with its provenance.

    Two runs over the same material: the window comparison that was here
    before, and the chronological simulation. Plus the part that decides
    whether any of it counts as evidence.

    `in_sample` is not "was a baseline given". A session named as its own
    baseline is in-sample however it was passed, and so is one whose
    recording overlaps in time with the material being judged — the same
    minutes cannot be both the measure and the measured. A baseline from
    a *different device* is refused outright unless the caller asks for a
    transfer comparison, because what a room does empty is a fact about
    that room and that antenna.
    """
    if baseline is not None and baseline.get("device_id") != session.get("device_id"):
        if not transfer:
            raise BaselineRefused(
                "Die Maßstab-Sitzung stammt von einem anderen Gerät. Was ein Raum "
                "leer tut, ist eine Eigenschaft dieses Raums und dieser Antenne — "
                "für einen Übertragungsvergleich ausdrücklich „transfer“ angeben."
            )

    same_session = baseline is not None and baseline.get("id") == session.get("id")
    overlapping = baseline is not None and _overlaps(session, baseline)
    in_sample = baseline is None or same_session or overlapping

    # A session named as its own baseline is the material itself; anything
    # else the caller offered is still used, and `identity.in_sample`
    # is the one place that says whether it counts as independent.
    measure = None if (same_session or baseline_samples is None) else baseline_samples
    profile = presence_rate.learn_baseline(measure if measure is not None else samples)

    if in_sample:
        why = (
            "dieselbe Sitzung" if same_session
            else "überlappender Zeitraum" if overlapping
            else "kein eigener Maßstab angegeben"
        )
        caveat = (
            f"Maßstab und Bewertung stammen aus derselben Messung ({why}) — die "
            "Zahlen messen sich selbst und sind kein Nachweis."
        )
    else:
        caveat = "Maßstab aus einer anderen, zeitlich getrennten Sitzung."

    identity = {
        "session": {
            "id": session.get("id"),
            "device_id": session.get("device_id"),
            "source": session.get("source") or "recording",
            "started_at": session.get("started_at"),
            "ended_at": session.get("ended_at"),
            "sample_count": len(samples),
        },
        "baseline": None if baseline is None else {
            "id": baseline.get("id"),
            "device_id": baseline.get("device_id"),
            "source": baseline.get("source") or "recording",
            "started_at": baseline.get("started_at"),
            "ended_at": baseline.get("ended_at"),
            "sample_count": len(baseline_samples or []),
        },
        "same_session": same_session,
        "time_overlap": overlapping,
        "cross_device": (
            baseline is not None
            and baseline.get("device_id") != session.get("device_id")
        ),
        "in_sample": in_sample,
        "caveat": caveat,
    }

    # The settings the run used — or would have used, when no baseline
    # could be learned. A number without them is not a measurement, and
    # "null" tells a reader nothing about what was tried.
    parameters = {
        "profile_learned": profile is not None,
        "window_seconds": (
            profile.window_seconds if profile else presence_rate.DEFAULT_WINDOW_SECONDS
        ),
        "crossing_threshold": (
            profile.crossing_threshold if profile
            else presence_rate.DEFAULT_CROSSING_THRESHOLD
        ),
        "hold_seconds": hold_seconds,
        "tick_seconds": DEFAULT_TICK_SECONDS,
        "min_coverage": presence_rate.MIN_COVERAGE,
        "max_sample_gap": presence_rate.MAX_SAMPLE_GAP,
        "rate_memory_seconds": presence_rate.RATE_MEMORY_SECONDS,
    }

    result = {
        "identity": identity,
        "parameters": parameters,
        # `compare` reports an `in_sample` of its own — whether it was
        # given separate material. `identity.in_sample` above is the
        # stronger question and the one to read.
        "window_comparison": compare(samples, baseline_samples=measure, windows=windows),
    }
    if profile is None:
        result["simulation"] = {
            "available": False,
            "reason": (
                "Zu wenig mit \u201eRaum leer\u201c markiertes Material, um einen "
                "Ma\u00dfstab zu lernen \u2014 ohne ihn gibt es keine Rate zu simulieren."
            ),
        }
        return result

    result["simulation"] = {
        "profile": profile.as_dict(),
        "with_rate": simulate(samples, profile, hold_seconds=hold_seconds),
        # The same machine with the rate switched off: motion, hysteresis
        # and hold time, which is what the add-on did before the rate
        # existed and what it has to beat.
        "motion_only": simulate(samples, profile, hold_seconds=hold_seconds, use_rate=False),
    }
    return result
