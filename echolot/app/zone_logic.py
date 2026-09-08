"""Presence state machine for a zone.

Echolot used to publish a zone as the plain OR of its members' motion
sensors. That reacts instantly but also drops out instantly: CSI movement
scores are noisy, and a person sitting still for two seconds turns the
zone off and back on. Two mechanisms fix that, and both are standard
signal-processing practice rather than anything specific to one product:

  * **Hysterese** — separate enter and exit thresholds on the movement
    score. Above `enter` the zone switches on, below `exit` it switches
    off, and in between it keeps whatever it had. One threshold value
    (the simple case) is just enter == exit.

  * **Haltezeit** — once motion stops, the zone stays occupied for a
    configured span before it clears. This is what makes a zone usable
    for lighting automations.

  * **Überschreitungsrate** — a second, slower opinion from
    app/presence_rate.py: how often the room crossed in the last minute
    against what it does empty. It cannot react in a second, so it never
    replaces the motion path; it keeps a zone occupied while somebody sits
    there without moving, which is the case motion alone cannot see.

That gives three externally visible states — `clear`, `detected`,
`holding` — where `holding` still counts as occupied but tells the UI
that a countdown is running.

The machine is deliberately pure: it takes readings plus a timestamp and
returns the new state. `main.py` owns the per-zone memory, so the logic
here can be tested without Home Assistant, MQTT, or a clock.
"""

from dataclasses import asdict, dataclass

CLEAR = "clear"
DETECTED = "detected"
HOLDING = "holding"


@dataclass(frozen=True)
class ZoneEvaluation:
    """What one evaluation concluded.

    A dataclass rather than a dict so callers get a contract instead of
    four string keys to misspell. `as_dict()` keeps the JSON shape the API
    already publishes.
    """

    state: str
    occupied: bool
    hold_remaining: float
    raw_motion: bool
    score: float | None
    #: True when the crossing rate said so, False when it said otherwise,
    #: None when no device in the zone has a baseline to judge against.
    rate_occupied: bool | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ZoneRuntime:
    """Mutable per-zone memory carried between evaluations."""

    state: str = CLEAR
    #: monotonic deadline at which a `holding` zone falls back to `clear`
    hold_until: float = 0.0
    #: last raw (pre-hold) decision, so hysteresis has something to hold on to
    raw: bool = False


def _raw_decision(
    runtime: ZoneRuntime,
    motion: bool,
    score: float | None,
    enter_threshold: float | None,
    exit_threshold: float | None,
) -> bool:
    """Is anything moving right now, before hold time is applied?

    Falls back to the devices' own motion booleans whenever no score is
    available or no thresholds are configured — an ESPectre node already
    applies its own threshold on-device, so that path stays useful.
    """
    if score is None or enter_threshold is None:
        return motion

    exit_at = exit_threshold if exit_threshold is not None else enter_threshold
    if score >= enter_threshold:
        return True
    if score <= exit_at:
        return False
    # Between the two thresholds nothing changes — that is the whole point.
    return runtime.raw


def evaluate(
    runtime: ZoneRuntime,
    *,
    motion: bool,
    score: float | None,
    enter_threshold: float | None,
    exit_threshold: float | None,
    hold_seconds: float,
    now: float,
    rate_occupied: bool | None = None,
) -> ZoneEvaluation:
    """Advance the machine and describe the result.

    `runtime` is mutated in place.
    """
    # The zone schema already bounds this, but the machine should not
    # depend on its caller for an invariant it can state itself: a
    # negative hold would expire in the past and quietly disable the
    # feature rather than fail.
    hold_seconds = max(0.0, hold_seconds)

    raw = _raw_decision(runtime, motion, score, enter_threshold, exit_threshold)
    # A second, slower opinion: how often this room crossed in the last
    # minute compared with what it does empty (see app/presence_rate.py).
    # Measured on real hardware, someone sitting still crosses on the order
    # of ten times as often as an empty room does — but only over a window,
    # never in a single reading, which is why it cannot replace `motion`
    # and only ever adds to it. Motion switches on in a second; the rate
    # keeps the zone on while somebody sits there not moving.
    if rate_occupied:
        raw = True
    runtime.raw = raw

    if raw:
        runtime.state = DETECTED
        runtime.hold_until = now + hold_seconds
    elif runtime.state in (DETECTED, HOLDING):
        # Motion just stopped, or a hold started on an earlier tick. Either
        # way `hold_until` was refreshed on every tick the zone was
        # DETECTED, so it already points at the right deadline. A hold time
        # of 0 makes that deadline `now`, which clears immediately — the
        # old pure-OR behaviour, preserved by construction.
        runtime.state = HOLDING if now < runtime.hold_until else CLEAR
    else:
        runtime.state = CLEAR

    remaining = max(0.0, runtime.hold_until - now) if runtime.state == HOLDING else 0.0
    return ZoneEvaluation(
        state=runtime.state,
        occupied=runtime.state in (DETECTED, HOLDING),
        hold_remaining=round(remaining, 1),
        raw_motion=raw,
        score=score,
        rate_occupied=rate_occupied,
    )
