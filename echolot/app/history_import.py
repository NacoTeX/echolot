"""Build calibration samples out of Home Assistant's recorder.

A recording used to be the only way to get labelled material: press start,
sit still or leave the room, press stop. That is a poor deal for the one
thing the rate detector actually needs, which is ten-plus minutes of the
room being empty — nobody wants to stand outside their own flat with a
timer, and the measurement everybody already has is sitting in the
recorder. Home Assistant keeps the movement score at full resolution
(measured on a real instance: 0.7 readings a second while the room is
quiet, 3.2 while it is not) for about ten days.

So this reads a past stretch instead of waiting for a future one. The
samples it produces are the same shape as recorded ones, because the
profiles have to be comparable: same `Sample`, same forward-filled
context, same labels.

The subtle part is that the three entities are three independent series.
The score changes constantly, `motion` flips a handful of times an hour,
and the threshold may not have changed all week. Reading them as parallel
rows would leave almost every sample without a threshold — the exact bug
0.13.1 fixed on the live path, arrived at from the other direction. So
the score drives the timeline and the other two are carried forward from
their last known value, which is what a subscription plus a cache does
anyway.
"""

from dataclasses import replace
from datetime import datetime

from app import ha_client
from app import samples as samples_module
from app.ha_sampler import _float, _stamp, build_sample
from app.telemetry import Sample

#: The recorder can hand back a great deal at once, and the whole period
#: is merged in memory before it is stored.
#:
#: Ninety minutes until 0.13.9, because the store then wrote every
#: recording as one JSON file and a long import made every later save
#: more expensive. That reason is gone — a recording is rows in a
#: database now — so the ceiling is what memory and patience allow
#: rather than what the file format did. Eight hours at the busiest
#: observed rate is roughly 90 000 readings, which the store holds and
#: `adopt` writes in one transaction.
#:
#: Still bounded, and deliberately: an unbounded range is a way to ask
#: Home Assistant's recorder for more than it can answer, and the
#: refusal should come from here with an explanation rather than from a
#: timeout somewhere else.
MAX_RANGE_SECONDS = 8 * 60 * 60

#: Below this there is nothing worth importing, and the caller has almost
#: certainly mistyped a date.
MIN_RANGE_SECONDS = 30


class RangeRejected(ValueError):
    """Refused before anything was fetched or stored."""


def check_range(start: datetime, end: datetime) -> float:
    """The span in seconds, or a refusal explaining which end is wrong."""
    span = (end - start).total_seconds()
    if span <= 0:
        raise RangeRejected("Das Ende liegt vor dem Anfang")
    if span < MIN_RANGE_SECONDS:
        raise RangeRejected(
            f"Der Zeitraum ist zu kurz — mindestens {MIN_RANGE_SECONDS} Sekunden"
        )
    if span > MAX_RANGE_SECONDS:
        raise RangeRejected(
            f"Der Zeitraum ist zu lang — höchstens {MAX_RANGE_SECONDS // 3600} Stunden "
            "auf einmal"
        )
    return span


def _ordered(states) -> list[dict]:
    """The states that carry a usable timestamp, oldest first."""
    stamped = [
        (stamp, state)
        for state in states or []
        if isinstance(state, dict) and (stamp := _stamp(state)) is not None
    ]
    stamped.sort(key=lambda pair: pair[0])
    return [state for _, state in stamped]


def merge(score_states, motion_states, threshold_states) -> list[Sample]:
    """One sample per movement-score reading, context carried forward.

    Home Assistant's history endpoint includes each entity's state as it
    stood at the start of the window, so the very first score reading
    already has a threshold to carry — there is no leading stretch of
    samples with the context missing.
    """
    scores = _ordered(score_states)
    motions = _ordered(motion_states)
    thresholds = _ordered(threshold_states)

    samples: list[Sample] = []
    motion_index = threshold_index = 0
    motion_now = None
    threshold_now = None

    for state in scores:
        stamp = _stamp(state)
        while motion_index < len(motions) and _stamp(motions[motion_index]) <= stamp:
            motion_now = motions[motion_index]
            motion_index += 1
        while (
            threshold_index < len(thresholds)
            and _stamp(thresholds[threshold_index]) <= stamp
        ):
            threshold_now = _float(thresholds[threshold_index])
            threshold_index += 1
        sample = build_sample(state, motion_now, threshold_now)
        if sample is not None:
            # The transport is Home Assistant, the same one the live path
            # takes — which is what makes an imported baseline comparable
            # with live readings at all. That it came from the recorder
            # rather than from the live subscription is a separate fact,
            # and the session carries it as `source: "history"`.
            samples.append(replace(sample, source=samples_module.SOURCE_HOME_ASSISTANT))
    return samples


async def fetch(device, start: datetime, end: datetime) -> list[Sample]:
    """Read the device's three entities over that range and merge them."""
    check_range(start, end)

    async def history(entity_id):
        if not entity_id:
            return []
        return await ha_client.get_history_range(entity_id, start, end)

    return merge(
        await history(device.entity_movement_score),
        await history(device.entity_motion),
        await history(device.entity_threshold),
    )
