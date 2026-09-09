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

from app import presence_rate

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
