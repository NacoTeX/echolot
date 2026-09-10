"""Echolot backend.

Phase 1 established the add-on skeleton (Ingress UI, ESPHome CLI check).
Phase 2 added device management and browser-based flashing: the backend
renders a per-device ESPHome/ESPectre YAML, compiles it, and serves the
result as an ESP Web Tools manifest + firmware image.
Phase 3 adds zones (grouping devices with OR-logic presence aggregation)
and pushing runtime parameters (detection threshold, recalibration) to
already-flashed devices via Home Assistant's Core API — ESPectre exposes
these as HA entities already, so no direct device protocol is needed.
"""

import asyncio
import logging
import math
import os
import subprocess
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from app import (
    builder,
    devices,
    entity_resolver,
    ha_client,
    health,
    mqtt_bridge,
    overview,
    presence_rate,
    presets,
    reachability,
    timeline,
    zone_logic,
    zones,
)
from app.board_registry import BOARDS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("echolot")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Run the MQTT zone publisher for the lifetime of the server.

    Zones are only useful outside this add-on once they exist as Home
    Assistant entities, and that mirroring has to keep running whether or
    not anyone has the dashboard open.
    """
    from app import feature_api

    # The one loop that decides what every zone is doing. Everything else
    # — the dashboard, the overview, the MQTT export — reads its
    # snapshots, so there is exactly one clock and one state machine.
    #
    # Waking it on live changes is what keeps the answer within half a
    # second of the event instead of within the idle timer. `_on_state`
    # runs on this loop (the subscription is scheduled onto it), so
    # setting the event straight from there is safe.
    feature_api.live.on_any_state(evaluator.wake)
    await evaluator.run(zones.list_zones)

    task = None
    if os.environ.get("ECHOLOT_MQTT_EXPORT", "true").lower() in ("0", "false", "no"):
        logger.info("MQTT export disabled by configuration")
    else:
        task = asyncio.create_task(_run_mqtt_export())

    try:
        yield
    finally:
        evaluator.stop()
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        mqtt_bridge.bridge.stop()
        await ha_client.close_client()


#: How long to wait before asking the Supervisor about MQTT again, in
#: seconds, doubling up to the last value. Starting the add-on before the
#: broker used to mean no export until the add-on itself was restarted —
#: a plausible order on a rebooting machine, and an invisible failure.
MQTT_RETRY_BACKOFF = (10, 30, 60, 300)


async def _run_mqtt_export() -> None:
    """Connect to MQTT and mirror zones, retrying until the broker exists."""
    from app import feature_api

    attempt = 0
    while True:
        try:
            await mqtt_bridge.bridge.start()
        except mqtt_bridge.MqttUnavailable as err:
            # Entirely normal without the Mosquitto add-on; everything else
            # keeps working, the zones just stay local to this UI. Worth
            # retrying anyway: the broker may simply not be up yet.
            delay = MQTT_RETRY_BACKOFF[min(attempt, len(MQTT_RETRY_BACKOFF) - 1)]
            attempt += 1
            logger.info("MQTT export inactive: %s — neuer Versuch in %ss", err, delay)
            mqtt_bridge.bridge.error = str(err)
            await asyncio.sleep(delay)
            continue

        # The export publishes what the evaluator worked out, when it
        # works it out. It used to run its own loop with its own clock and
        # its own calls into Home Assistant, which is how a reading
        # arriving three times a second became three rounds of requests.
        mqtt_bridge.attach(evaluator, forget_zone_runtime)
        return


app = FastAPI(title="Echolot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Builds run for minutes in a background task; asyncio only holds a weak
# reference to a task once created, so without keeping one here the task
# risks being garbage-collected mid-build. Discarded again once it's done.
_background_builds: set[asyncio.Task] = set()


def _validation_detail(err: ValidationError) -> list[dict]:
    # err.errors() can carry raw exception objects in "ctx" (e.g. from a
    # validator's `raise ValueError`), which json.dumps can't serialize.
    # Keep only the plain-text fields the UI actually uses.
    return [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in err.errors()]


def _safe_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # NaN and the infinities survive float() but poison every comparison
    # downstream: NaN >= threshold is False, so a broken sensor would read
    # as a quiet room rather than as a broken sensor.
    return number if math.isfinite(number) else None


#: What Home Assistant says when it has no reading. Both are states of the
#: transport, not measurements — `unavailable` means the integration lost
#: the device, `unknown` that it has never reported one.
NO_READING = ("unavailable", "unknown", "none", "")


def _binary_state(state) -> bool | None:
    """A binary_sensor's reading, or None when it has not got one.

    Only `on` and `off` are readings. Until 0.13.5 this was written as
    `state["state"] == "on"`, which quietly turned `unavailable` into
    "no motion" — so a device that had fallen off the network published a
    confidently empty room once the hold time ran out. Every other value
    is the absence of a measurement and has to be reported as such.
    """
    if not isinstance(state, dict):
        return None
    raw = state.get("state")
    if raw in ("on", "off"):
        return raw == "on"
    return None


def _parse_ts(value) -> float | None:
    """ISO-8601 timestamp -> epoch seconds. HA emits a trailing 'Z' that
    fromisoformat only learned to accept in 3.11+, so normalise it."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _persist_entities(device: devices.Device, found: dict) -> bool:
    """Store an already-resolved mapping, if it says anything new.

    Split out from _adopt_entities because the diagnosis resolves the same
    entities for its own reasons and would otherwise leave the record
    stale — which broke the "Neu kalibrieren" button it offers, on exactly
    the devices the diagnosis exists for: the ones whose entities were
    never adopted.
    """
    changed = {
        field: entity_id
        for field, entity_id in found.items()
        if getattr(device, field, None) != entity_id
    }
    if not changed:
        return False
    for field, entity_id in changed.items():
        setattr(device, field, entity_id)
    devices.save_device(device)
    logger.info("Entities für %s erkannt: %s", device.config.name, changed)
    return True


def _adopt_entities(device: devices.Device, states: list[dict]) -> bool:
    """Resolve the device's entities against a snapshot and persist the result."""
    return _persist_entities(device, entity_resolver.resolve(device, states))


async def _autodetect_entities(device: devices.Device) -> bool:
    """Fetch a snapshot, then adopt from it.

    Called when a single-device read finds its motion entity missing. A
    flashed device that Home Assistant adopted under a name we did not
    predict is the common case; without this the device sits at "nicht
    verfügbar" forever with a working sensor behind it.
    """
    try:
        states = await ha_client.list_states()
    except ha_client.HomeAssistantUnavailable:
        return False
    return _adopt_entities(device, states)


async def _read_device_state(device: devices.Device, allow_detect: bool = True) -> dict:
    """Current motion/score/threshold for one device.

    The three entities are read concurrently, so this costs one round-trip
    of latency rather than three, and a few hundred bytes rather than the
    whole state machine of the installation.

    An earlier version fetched /api/states once and served every device
    from that snapshot. It looked like a win — fifteen requests down to
    one for a five-device zone — but that one request carries *every*
    entity Home Assistant knows about, on a timer, forever. On a large
    installation that is megabytes every ten seconds to read a handful of
    numbers. Targeted reads, issued together, are the better trade;
    /api/states is kept for discovery, where it earns its size.
    """
    if not device.entity_motion:
        return {"available": False, "error": "Für dieses Gerät ist keine Bewegungs-Entity konfiguriert"}

    async def read(entity_id: str | None):
        return await ha_client.get_state(entity_id) if entity_id else None

    try:
        motion, score, threshold = await asyncio.gather(
            read(device.entity_motion),
            read(device.entity_movement_score),
            read(device.entity_threshold),
        )
        if motion is None and allow_detect and await _autodetect_entities(device):
            # Entities were just relearned — read once more before giving up.
            return await _read_device_state(device, allow_detect=False)
    except ha_client.HomeAssistantUnavailable as err:
        return {"available": False, "error": str(err)}
    if motion is None:
        return {
            "available": False,
            "error": (
                f"Entity {device.entity_motion} existiert in Home Assistant nicht. "
                "Wurde das Gerät dort schon hinzugefügt? Einstellungen → Geräte & "
                "Dienste → Integrationen, dort sollte ESPHome das Gerät zur "
                "Einrichtung anbieten."
            ),
        }
    detected = _binary_state(motion)
    if detected is None:
        # The entity exists but is not reporting. That is a transport
        # failure, and the honest answer is "no measurement" — not "no
        # motion". A movement score without a working motion sensor is
        # deliberately not treated as partial evidence: the zone machine
        # falls back to the motion boolean whenever no threshold is
        # configured, so admitting a half-available device would put that
        # fallback on a value that is not there.
        reported = motion.get("state") if isinstance(motion, dict) else None
        return {
            "available": False,
            "error": (
                f"Entity {device.entity_motion} meldet „{reported}“ statt on/off. "
                "Home Assistant hat für dieses Gerät gerade keinen Messwert — "
                "ist es im Netz erreichbar?"
            ),
        }

    return {
        "available": True,
        "motion": detected,
        "movement_score": _safe_float(score["state"]) if score else None,
        "threshold": _safe_float(threshold["state"]) if threshold else None,
    }


#: Per-zone state-machine memory. Zones are few and short-lived compared
#: to the process, and a forgotten entry only costs a dict slot, but a
#: deleted zone should not keep its hold running if the id is reused.
_zone_runtimes: dict[str, zone_logic.ZoneRuntime] = {}

#: The floor between two evaluation rounds, in seconds.
#:
#: Zone state has exactly one owner: `compute_zone_state` mutates the
#: zone's runtime — the motion hysteresis memory and the hold deadline —
#: and re-reads every member from Home Assistant. Half a second is below
#: what anyone notices in a room and far above the rate readings arrive
#: in bursts.
MIN_EVALUATION_INTERVAL = 0.5

#: How long the loop waits when nothing has happened. A zone counting
#: down a hold time needs the short one — the countdown has to end
#: without anybody watching — and an idle house does not.
IDLE_INTERVAL = 10.0
HOLDING_INTERVAL = 1.0

#: How long a device's rate hysteresis remembers across a data outage.
#: Defined next to the step that uses it, so the replay runner and the
#: live path cannot drift apart on it.
RATE_MEMORY_SECONDS = presence_rate.RATE_MEMORY_SECONDS


def _initial_zone_state() -> dict:
    """What a zone looks like before anything has evaluated it.

    A zone created a moment ago has no snapshot, and the overview used to
    fill that hole with two keys and then read five — a KeyError and an
    HTTP 500 for the crime of making a zone. Built from the state machine
    itself so it cannot drift from the real shape.
    """
    return {
        "available": False,
        "members": [],
        "pending": True,
        **zone_logic.ZoneEvaluation(
            state=zone_logic.CLEAR,
            occupied=False,
            hold_remaining=0.0,
            raw_motion=False,
            score=None,
        ).as_dict(),
    }


class ZoneEvaluator:
    """The one background loop that decides what every zone is doing.

    Everything else reads its snapshots: the dashboard, the overview, the
    MQTT export. Three things follow from that and none of them worked
    before this loop existed.

    A request never evaluates. `state_of` was still calling `refresh`, so
    a GET two seconds after the last round started another one — an open
    dashboard was quietly driving the state machine and the Home
    Assistant request rate.

    A round always covers every zone. The freshness check was global
    while a round could cover a subset, so `refresh([A])` followed by
    `refresh([A, B])` returned only A, and B fell through the overview's
    two-key fallback into a KeyError.

    A round always re-reads the devices. The per-device rate verdict was
    memoised on the newest reading, which does not change when old
    readings fall off the *other* end of a rolling window: twelve high
    readings that had aged out of the last minute kept the room occupied
    until something new arrived.
    """

    def __init__(self) -> None:
        self._snapshots: dict[str, dict] = {}
        #: The loop the run() task lives on, so `wake` can be called from
        #: anywhere. FastAPI runs a plain `def` route in a worker thread,
        #: and `asyncio.Event.set()` from another thread does not reliably
        #: wake the selector — the event would look set and the loop would
        #: sit in its timeout until it expired.
        self._loop = None
        #: Per-tick device verdicts. Cleared at the start of every round,
        #: so two zones sharing a device get one evaluation and the same
        #: answer, and the next round starts from the current window.
        self._device_verdicts: dict[str, bool | None] = {}
        self._lock = asyncio.Lock()
        self._wakeup = asyncio.Event()
        self._task = None
        self._listeners: list = []

    # --- what other components see ---------------------------------------

    def forget(self, zone_id: str) -> None:
        _zone_runtimes.pop(zone_id, None)
        self._snapshots.pop(zone_id, None)
        timeline.timeline.forget(zone_id)

    def snapshot(self, zone_id: str) -> dict | None:
        return self._snapshots.get(zone_id)

    def state_of(self, zone) -> dict:
        """One zone's state. A read, and only a read."""
        return self._snapshots.get(zone.id) or _initial_zone_state()

    def all_states(self, zone_list: list) -> list[tuple]:
        return [(zone, self.state_of(zone)) for zone in zone_list]

    def add_listener(self, callback) -> None:
        """Called with [(zone, state)] after every round. The MQTT export
        hangs on this rather than evaluating anything itself."""
        self._listeners.append(callback)

    def wake(self, *_args) -> None:
        """Something changed; evaluate as soon as the floor allows.

        Safe from any thread, and takes any arguments so it can be
        attached straight to a device stream, whose change listeners are
        called with the device id.
        """
        loop = self._loop
        if loop is None:
            self._wakeup.set()
            return
        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            self._wakeup.set()
            return
        try:
            loop.call_soon_threadsafe(self._wakeup.set)
        except RuntimeError:
            # The loop is shutting down; there is nothing left to wake.
            logger.debug("Wakeup nach Loop-Ende verworfen")

    # --- the loop ---------------------------------------------------------

    async def run(self, list_zones) -> None:
        if self._task is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._loop_body(list_zones))

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._loop = None

    async def _loop_body(self, list_zones) -> None:
        while True:
            try:
                await self.cycle(list_zones())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad round must not end the loop
                logger.exception("Zonenauswertung fehlgeschlagen")
            await self._wait_for_work()

    async def _wait_for_work(self) -> None:
        """Sleep until something happens, then honour the floor.

        The floor is applied *after* the wake rather than by refusing to
        evaluate: a change arriving inside the throttle used to publish
        the old snapshot and schedule nothing, so a short pulse could sit
        there until the next timer tick. Waiting, then sleeping the floor,
        then evaluating means every wake produces a round — late by at
        most the floor, never dropped.
        """
        holding = any(
            state.get("state") == zone_logic.HOLDING
            for state in self._snapshots.values()
        )
        try:
            await asyncio.wait_for(
                self._wakeup.wait(),
                timeout=HOLDING_INTERVAL if holding else IDLE_INTERVAL,
            )
        except asyncio.TimeoutError:
            pass
        self._wakeup.clear()
        await asyncio.sleep(MIN_EVALUATION_INTERVAL)

    async def cycle(self, zone_list: list) -> list[tuple]:
        """One round: every device once, then every zone.

        "Every device once" is the whole point and it used not to hold
        for the state read. `compute_zone_state` fetched Home Assistant
        per *membership* and consumed the motion latch per *membership*,
        so a device in two zones cost two reads and only the first zone
        saw a short pulse. The round is built here, once, and every zone
        reads the same snapshots.
        """
        async with self._lock:
            # The window has moved on even if nothing new arrived, so
            # last round's verdicts are not answers to this round.
            self._device_verdicts = {}

            snapshots = await read_round(
                [device_id for zone in zone_list for device_id in zone.device_ids]
            )
            states = await asyncio.gather(
                *(compute_zone_state(zone, snapshots) for zone in zone_list)
            )
            live = {zone.id for zone in zone_list}
            for gone in set(self._snapshots) - live:
                self._snapshots.pop(gone, None)
            # A device that has left every zone is not being measured any
            # more, so its hysteresis is not about anything.
            for forgotten in set(_rate_state) - set(snapshots):
                _rate_state.pop(forgotten, None)
            self._snapshots.update(
                {zone.id: state for zone, state in zip(zone_list, states)}
            )
            # The one place that sees every transition, so the one place
            # that can write down why it happened.
            for zone, state in zip(zone_list, states):
                timeline.timeline.record(zone.id, zone.name, state)

        result = list(zip(zone_list, states))
        for listener in tuple(self._listeners):
            try:
                listener(result)
            except Exception:  # noqa: BLE001 - one listener must not stop the rest
                logger.exception("Zonen-Listener fehlgeschlagen")
        return result


evaluator = ZoneEvaluator()


def forget_zone_runtime(zone_id: str) -> None:
    evaluator.forget(zone_id)


async def _read_devices(device_ids: list[str]) -> list[tuple[str, devices.Device | None, dict]]:
    """Read several devices at once.

    Concurrency is what makes targeted reads affordable: a zone of five
    devices is fifteen small requests, but they all go out together over
    one pooled connection, so it costs one round-trip of latency rather
    than fifteen.
    """
    resolved = [(device_id, devices.get_device(device_id)) for device_id in device_ids]
    states = await asyncio.gather(
        *(
            _read_device_state(device)
            if device is not None
            else _missing_device_state()
            for _, device in resolved
        )
    )
    return [(device_id, device, state) for (device_id, device), state in zip(resolved, states)]


async def _missing_device_state() -> dict:
    """A zone member whose device was deleted out from under it."""
    return {"available": False, "motion": None, "error": "Gerät existiert nicht mehr"}


#: The crossing rate's own memory, per device.
#:
#: `presence_rate.evaluate` has two levels — a higher one to switch on and
#: a lower one to stay on — and picks between them from `occupied_now`,
#: which is meant to be *that device's* previous verdict.
#:
#: What lives here is only the memory: the last verdict, when it was
#: reached, and what it was reached under. The verdict itself is worked
#: out afresh on every round, because a rolling window changes when old
#: readings fall off its left edge and not only when new ones arrive at
#: its right. Memoising on the newest reading held twelve high readings
#: that had aged out of the last minute as "occupied" until something new
#: happened to turn up.
_rate_state: dict[str, dict] = {}


#: Why the slow, window-based evidence has nothing to say. Kept apart
#: because they mean different things to the person reading the card:
#: one is a dropout, one is a setup mistake, one is simply "not
#: calibrated yet".
RATE_DISCONNECTED = "disconnected"
RATE_SOURCE_MISMATCH = "source_mismatch"
RATE_BAND_MISMATCH = "band_mismatch"
RATE_MODE_MISMATCH = "mode_mismatch"

#: One reason per condition a baseline was learned under, in the order
#: they are checked. The conditions themselves live in
#: `presence_rate.MEASUREMENT_FIELDS`; a fourth would be a row in both.
RATE_MISMATCH_REASONS = {
    "source": RATE_SOURCE_MISMATCH,
    "band": RATE_BAND_MISMATCH,
    "sensing_mode": RATE_MODE_MISMATCH,
}


def _measurement_now(device) -> dict[str, str]:
    """The conditions this device is measuring under right now.

    The counterpart to `presence_rate.measurement_definition`, and
    deliberately the same keys: what a baseline recorded is compared with
    what is true, field by field, rather than by three separate rules
    that drift apart.
    """
    from app import samples

    return {
        # Which transport the readings arrive over.
        "source": samples.bus.source,
        # Which radio they are measured on.
        "band": devices.effective_band(device.config),
        # Which radio path: the access point's traffic, or a directed
        # link between two sensors.
        "sensing_mode": device.config.sensing_mode,
    }


def _device_rate_evidence(
    device, *, now: float | None = None, connected: bool = True
) -> tuple[bool | None, str | None]:
    """The slow evidence for one device, and why it is silent when it is.

    Evaluated once per round and shared, so two zones holding the same
    device get one evaluation and the same answer, and member order
    cannot change it.

    Three kinds of thing make it unknown rather than "vacant":

    * **No profile at all.** Silent, as before — a zone where nobody has
      calibrated behaves exactly as it did before the rate existed.

    * **A profile learned under other conditions.** What a room does
      empty is a fact about that room measured over one transport, on
      one radio band, along one radio path. Change any of them and the
      recording describes a different measurement, so it is no longer a
      scale for this one. The overview warned about the first of those
      and the evaluation went ahead anyway; now the slow path goes quiet
      until it is recalibrated, with the condition named, and the fast
      motion path is untouched.

    * **No transport.** The caller ignored `stream.connected`, so a
      subscription that had dropped kept answering from whatever was
      still in the buffer. An open socket does not prove the ESP is
      measuring — but a closed one does prove it is not, and that is a
      sound one-way inference. The hysteresis memory is kept: a dropout
      is not a recalibration, and somebody sitting still through one
      should not have to move again to be seen.

    The step itself is `presence_rate.advance`, which the replay runner
    also takes, so a replay measures what the add-on actually does.

    Imported lazily because app.feature_api imports app.main.
    """
    from app import presence_rate, samples

    cached = evaluator._device_verdicts
    if device.id in cached:
        return cached[device.id]

    now = time.monotonic() if now is None else now
    profile = presence_rate.profile_from_dict(device.presence_profile)
    if profile is None:
        _rate_state.pop(device.id, None)
        cached[device.id] = (None, None)
        return None, None

    # What a room does empty is a fact about that room *under
    # conditions*: over one transport, on one radio band, along one radio
    # path. Change any of them and the recording describes a different
    # measurement, so the baseline is no longer a scale for it. Each was
    # once a separate rule; they are one comparison now, because they are
    # one argument.
    #
    # Not a warning any more, and the memory goes with it: it was built
    # from verdicts this profile was not entitled to have made. A
    # condition the profile does not record is not a mismatch — see
    # `measurement_definition`.
    recorded = presence_rate.measurement_definition(profile)
    current = _measurement_now(device)
    for field, reason in RATE_MISMATCH_REASONS.items():
        if recorded[field] and recorded[field] != current[field]:
            _rate_state.pop(device.id, None)
            cached[device.id] = (None, reason)
            return None, reason

    if not connected:
        cached[device.id] = (None, RATE_DISCONNECTED)
        return None, RATE_DISCONNECTED

    # The canonical stream — the one calibration, fusion and replay read.
    # It used to read the Home Assistant subscription's own buffer
    # directly, so the production rate and everything that judges it were
    # two different sources of readings.
    verdict, memory = presence_rate.advance(
        profile,
        samples.bus.window(device.id, profile.window_seconds),
        _rate_state.get(device.id),
        now=now,
        # The bus numbers its own generations, so a rebuilt subscription
        # or a source change cannot pass its memory to the new one.
        source=samples.bus.generation(device.id),
        memory_seconds=RATE_MEMORY_SECONDS,
        # Two clocks, on purpose. The memory ages on the monotonic one,
        # because a duration must not jump when the system clock is set.
        # The window ends on the wall clock, because that is what Home
        # Assistant stamps a reading with. Passing the monotonic tick as
        # the window end would compare a machine uptime against an epoch.
        window_end=time.time(),
    )
    if memory is None:
        _rate_state.pop(device.id, None)
    else:
        _rate_state[device.id] = memory
    cached[device.id] = (verdict, None)
    return verdict, None


def _device_rate_verdict(device, *, now: float | None = None) -> bool | None:
    """Just the verdict. The reason is for the card, not the state machine."""
    return _device_rate_evidence(device, now=now)[0]


def _zone_rate_verdict(zone) -> bool | None:
    """What the crossing rate says about this zone, or None if it cannot say.

    OR across the members: one device seeing an elevated rate is enough,
    and a device that cannot say contributes nothing — not a "vacant"
    vote. The round does this from its snapshots; this is the standalone
    path, for a caller holding only a zone.
    """
    from app import feature_api

    verdict = None
    for device_id in zone.device_ids:
        device = devices.get_device(device_id)
        if device is None or not device.presence_profile:
            _rate_state.pop(device_id, None)
            continue
        stream = feature_api.live.stream(device_id)
        one, _reason = _device_rate_evidence(
            device, connected=bool(stream is not None and getattr(stream, "connected", False))
        )
        if one is None:
            continue
        verdict = bool(verdict) or one
    return verdict


@dataclass(frozen=True)
class DeviceRound:
    """One device, as a single evaluation round sees it.

    Built once per round and read by every zone that holds the device.
    Before this, `compute_zone_state` read Home Assistant per *membership*
    and consumed the motion latch per *membership*, so a device in two
    zones cost two reads and — worse — only the zone that happened to be
    evaluated first saw a short pulse. The second zone got False for the
    same instant of the same device. Reported as A in the 0.13.6 review,
    reproduced as `[True, False]`.

    Frozen, because a round is a fact about a moment. A zone that could
    edit it would be editing what the other zones see.
    """

    device_id: str
    name: str | None
    available: bool
    motion: bool
    #: Motion went on at least once since the previous round, even if it
    #: is off again now. Carries `motion_since` so a reader can tell a
    #: pulse from a level — and so it cannot silently stand in for a
    #: fresh measurement.
    motion_pulse: bool
    motion_since: float | None
    movement_score: float | None
    threshold: float | None
    #: The slow, window-based evidence. None when the rate cannot say —
    #: and `rate_reason` says which of the several reasons it is.
    rate_occupied: bool | None
    rate_reason: str | None
    #: Which transport this device is being measured over, and whether it
    #: is currently receiving.
    source: str | None
    connected: bool
    error: str | None

    def as_member(self) -> dict:
        """The row the API and the dashboard read."""
        row = {
            "device_id": self.device_id,
            "name": self.name,
            "available": self.available,
            "motion": self.motion,
            "movement_score": self.movement_score,
            "threshold": self.threshold,
            "source": self.source,
            "connected": self.connected,
        }
        if self.motion_pulse:
            row["motion_pulse"] = True
            row["motion_since"] = self.motion_since
        if self.rate_reason:
            row["rate_reason"] = self.rate_reason
        if self.error:
            row["error"] = self.error
        return row


def _missing_round(device_id: str, state: dict) -> DeviceRound:
    """A device the registry does not know any more."""
    return DeviceRound(
        device_id=device_id,
        name=None,
        available=False,
        motion=False,
        motion_pulse=False,
        motion_since=None,
        movement_score=None,
        threshold=None,
        rate_occupied=None,
        rate_reason=None,
        source=None,
        connected=False,
        error=state.get("error"),
    )


async def read_round(device_ids, *, now: float | None = None) -> dict[str, DeviceRound]:
    """Read every distinct device once, and settle what this round sees.

    Ten zones holding one device cost one Home Assistant read and one
    pulse, not ten of each.
    """
    from app import feature_api, samples

    wanted = list(dict.fromkeys(device_ids))
    now = time.monotonic() if now is None else now
    snapshots: dict[str, DeviceRound] = {}

    for device_id, device, state in await _read_devices(wanted):
        if device is None:
            snapshots[device_id] = _missing_round(device_id, state)
            continue

        stream = feature_api.live.stream(device_id)
        connected = bool(stream is not None and getattr(stream, "connected", False))

        # Consumed exactly once, here. Every zone then reads the same
        # answer out of this snapshot.
        pulse = stream.motion_pulsed() if stream is not None else None
        motion = bool(state.get("motion"))
        available = bool(state.get("available"))
        if pulse is not None:
            motion = motion or pulse.happened
            # A pulse proves the source was alive when it arrived, so it
            # makes the device available for this round even if the REST
            # read came back empty a moment later. It does not stand in
            # for a *measurement*: score and threshold stay as read.
            available = available or pulse.happened

        verdict, reason = _device_rate_evidence(device, now=now, connected=connected)
        snapshots[device_id] = DeviceRound(
            device_id=device_id,
            name=device.config.friendly_name or device.config.name,
            available=available,
            motion=motion,
            motion_pulse=bool(pulse and pulse.happened),
            motion_since=pulse.at if pulse and pulse.happened else None,
            movement_score=state.get("movement_score"),
            threshold=state.get("threshold"),
            rate_occupied=verdict,
            rate_reason=reason,
            source=samples.bus.source if connected else None,
            connected=connected,
            error=state.get("error"),
        )
    return snapshots


async def compute_zone_state(zone: zones.Zone, snapshots: dict | None = None) -> dict:
    """Aggregate a zone's members and run its presence state machine.

    Shared by the API route and the MQTT publisher, so what Home Assistant
    receives can never drift from what the dashboard shows. The raw
    aggregation is still OR over the members — any device seeing movement
    means the zone sees movement — but hysteresis and hold time now sit
    between that and the published `occupied` flag (see zone_logic).
    """
    if snapshots is None:
        # Standalone — a route or a test asking about one zone. The
        # evaluator always hands one in, and that is the path that must
        # not read a device twice.
        snapshots = await read_round(zone.device_ids)

    members = []
    raw_motion = False
    any_available = False
    best_score: float | None = None
    rate_occupied: bool | None = None
    for device_id in zone.device_ids:
        seen = snapshots.get(device_id)
        if seen is None:
            members.append({"device_id": device_id, "name": None, "available": False})
            continue
        members.append(seen.as_member())
        if seen.available:
            any_available = True
            if seen.motion:
                raw_motion = True
            if seen.movement_score is not None and (
                best_score is None or seen.movement_score > best_score
            ):
                best_score = seen.movement_score
        # OR across the members, like the motion aggregation: one device
        # seeing an elevated rate is enough, and a device that cannot say
        # contributes nothing — not a "vacant" vote.
        if seen.rate_occupied is not None:
            rate_occupied = bool(rate_occupied) or seen.rate_occupied

    runtime = _zone_runtimes.setdefault(zone.id, zone_logic.ZoneRuntime())
    verdict = zone_logic.evaluate(
        runtime,
        motion=raw_motion,
        score=best_score,
        enter_threshold=zone.enter_threshold,
        exit_threshold=zone.exit_threshold,
        hold_seconds=zone.hold_seconds,
        now=time.monotonic(),
        rate_occupied=rate_occupied,
    )
    return {"available": any_available, "members": members, **verdict.as_dict()}


def all_zone_states(zone_list: list) -> list[tuple]:
    """Every zone's latest state, read from the evaluator's snapshots."""
    return evaluator.all_states(zone_list)


@app.get("/api/health")
def api_liveness() -> dict:
    """Is the add-on's own process up. Not to be confused with
    /api/devices/{id}/health, which asks whether a device is sensing."""
    return {"status": "ok"}


@app.get("/api/overview")
async def api_overview() -> dict:
    """Everything the first screen needs, in one request.

    Assembled here rather than in the browser: the alternative is one
    request per zone plus three per device from a page whose whole job is
    to load fast. Devices and zones are read concurrently, so the page
    costs roughly one round-trip regardless of how many there are.
    """
    device_list = devices.list_devices()
    zone_list = zones.list_zones()

    built = [d for d in device_list if str(d.status) == "success"]
    built_states = dict(
        zip(
            (d.id for d in built),
            await asyncio.gather(*(_read_device_state(d) for d in built)),
        )
    )
    device_states = [(d, built_states.get(d.id)) for d in device_list]

    # Reads the snapshot rather than evaluating: the overview is a status
    # report, and a status report should not be advancing the state
    # machine it is reporting on.
    zone_verdicts = {
        zone.id: state for zone, state in evaluator.all_states(zone_list)
    }

    zone_views = []
    for zone in zone_list:
        # No fallback: all_states answers for every zone, including one
        # created a moment ago. The two-key stand-in that used to be here
        # was read for five keys directly below — a KeyError and an HTTP
        # 500 for the crime of making a zone.
        verdict = zone_verdicts[zone.id]
        zone_views.append(
            {
                "id": zone.id,
                "name": zone.name,
                "state": verdict["state"],
                "occupied": verdict["occupied"],
                "hold_remaining": verdict["hold_remaining"],
                "available": verdict["available"],
                # Nothing has looked at this zone yet, which is not the
                # same as having looked and found nothing.
                "pending": bool(verdict.get("pending")),
                "device_count": len(zone.device_ids),
            }
        )

    mqtt_status = mqtt_bridge.bridge.status()
    mqtt_wanted = os.environ.get("ECHOLOT_MQTT_EXPORT", "true").lower() not in ("0", "false", "no")
    esphome = _esphome_version()

    problems = overview.collect_problems(
        device_states=device_states,
        zones_without_devices=[z for z in zone_list if not z.device_ids],
        mqtt_status=mqtt_status,
        mqtt_wanted=mqtt_wanted,
        esphome=esphome,
    )

    return {
        "zones": zone_views,
        "problems": [p.as_dict() for p in problems],
        "devices": {
            "total": len(device_list),
            "built": sum(1 for d in device_list if str(d.status) == "success"),
        },
        "esphome": esphome,
        "mqtt": {**mqtt_status, "wanted": mqtt_wanted},
        "radio_load_kb_per_second": overview.radio_load(device_list, presets.KB_PER_SECOND_PER_PPS),
    }


@app.get("/api/mqtt/status")
def api_mqtt_status() -> dict:
    return mqtt_bridge.bridge.status()


@app.get("/api/presets")
def api_presets() -> dict:
    return {
        "presets": presets.as_dicts(),
        "kb_per_second_per_pps": presets.KB_PER_SECOND_PER_PPS,
    }


_esphome_version_cache: dict | None = None


def _esphome_version() -> dict:
    """Whether the bundled ESPHome CLI is usable, cached.

    Spawning a subprocess is cheap once and wasteful on every overview
    load — and the answer cannot change while this container runs.
    """
    global _esphome_version_cache
    if _esphome_version_cache is not None:
        return _esphome_version_cache
    try:
        result = subprocess.run(
            ["esphome", "version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        _esphome_version_cache = {"available": True, "version": result.stdout.strip()}
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as err:
        logger.warning("esphome CLI check failed: %s", err)
        # Not cached: a transient failure should not be permanent.
        return {"available": False, "error": str(err)}
    return _esphome_version_cache


@app.get("/api/esphome/version")
def esphome_version() -> dict:
    """Confirm the bundled ESPHome CLI is usable (needed for firmware builds)."""
    return _esphome_version()


@app.get("/api/boards")
def list_boards() -> list[dict]:
    return [
        {
            "key": b.key,
            "label": b.label,
            "chip_family": b.chip_family,
            "experimental": b.experimental,
            # Lets the form offer the band only where there is one to
            # choose. Everywhere else the field would be a control with
            # no radio behind it.
            "dual_band": b.dual_band,
        }
        for b in BOARDS.values()
    ]


@app.get("/api/devices")
def api_list_devices() -> list[dict]:
    return [d.public() for d in devices.list_devices()]


@app.post("/api/devices", status_code=201)
def api_create_device(payload: dict) -> dict:
    try:
        config = devices.DeviceCreate.model_validate(payload)
    except ValidationError as err:
        raise HTTPException(status_code=422, detail=_validation_detail(err)) from err
    device = devices.create_device(config)
    return device.public()


@app.get("/api/devices/{device_id}")
def api_get_device(device_id: str) -> dict:
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    return device.public()


@app.patch("/api/devices/{device_id}")
def api_update_device(device_id: str, payload: dict) -> dict:
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    try:
        patch = devices.DeviceUpdate.model_validate(payload)
    except ValidationError as err:
        raise HTTPException(status_code=422, detail=_validation_detail(err)) from err
    device.apply_update(patch)
    devices.save_device(device)
    return device.public()


@app.patch("/api/devices/{device_id}/config")
def api_reconfigure_device(device_id: str, payload: dict) -> dict:
    """Change firmware options on an existing device.

    Separate from PATCH on the device itself, because the consequence is
    different: those fields take effect at once, these need a rebuild and
    a flash before the chip knows about them. `firmware_behind_config`
    in the response says whether that is now outstanding.
    """
    try:
        device = devices.reconfigure(device_id, payload)
    except ValidationError as err:
        raise HTTPException(status_code=422, detail=_validation_detail(err)) from err
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    return device.public()


@app.delete("/api/devices/{device_id}", status_code=204)
def api_delete_device(device_id: str) -> None:
    if not devices.delete_device(device_id):
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")


@app.get("/api/devices/{device_id}/state")
async def api_device_state(device_id: str) -> dict:
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    return await _read_device_state(device)


@app.get("/api/devices/{device_id}/history")
async def api_device_history(device_id: str, minutes: int = 30) -> dict:
    """Movement-score history, so the dashboard sparkline starts populated
    instead of building up from nothing on every page load."""
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    if not device.entity_movement_score:
        return {"available": False, "error": "Keine Bewegungswert-Entity konfiguriert", "points": []}

    minutes = max(1, min(minutes, 1440))
    try:
        raw = await ha_client.get_history(device.entity_movement_score, minutes)
    except ha_client.HomeAssistantUnavailable as err:
        return {"available": False, "error": str(err), "points": []}

    points = []
    for entry in raw:
        value = _safe_float(entry.get("state"))
        if value is None:  # skips "unavailable" / "unknown"
            continue
        stamp = entry.get("last_changed") or entry.get("last_updated")
        parsed = _parse_ts(stamp)
        if parsed is None:
            continue
        points.append({"t": parsed, "v": value})
    return {"available": True, "points": points}


@app.post("/api/devices/{device_id}/threshold")
async def api_set_threshold(device_id: str, payload: dict) -> dict:
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    if not device.entity_threshold:
        raise HTTPException(status_code=409, detail="Für dieses Gerät ist keine Schwellen-Entity konfiguriert")
    value = _safe_float(payload.get("value"))
    if value is None or not (0.0 <= value <= 10.0):
        raise HTTPException(status_code=422, detail='Erwartet wird {"value": <Zahl 0.0-10.0>}')
    try:
        await ha_client.call_service("number", "set_value", device.entity_threshold, value=value)
    except ha_client.HomeAssistantUnavailable as err:
        raise HTTPException(status_code=502, detail=f"Home Assistant nicht erreichbar: {err}") from err
    return {"status": "ok"}


@app.post("/api/devices/{device_id}/calibrate")
async def api_calibrate_device(device_id: str) -> dict:
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    if not device.entity_calibrate:
        raise HTTPException(status_code=409, detail="Für dieses Gerät ist keine Kalibrierungs-Entity konfiguriert")
    try:
        # A button press, not a switch: recalibration is a one-shot action
        # upstream now, with nothing to switch back off.
        await ha_client.call_service("button", "press", device.entity_calibrate)
    except ha_client.HomeAssistantUnavailable as err:
        raise HTTPException(status_code=502, detail=f"Home Assistant nicht erreichbar: {err}") from err
    return {"status": "ok"}


#: Looked up on demand rather than stored per device: nothing polls these,
#: and persisting them would mean migrating every device record for a
#: diagnosis that is opened by hand.
_DIAGNOSTIC_SPECS = {
    field: ("sensor", label) for field, label in health.DIAGNOSTIC_LABELS.items()
}
_DIAGNOSTIC_SPECS["diag_profile"] = ("select", "Detection Profile")
_DIAGNOSTIC_SPECS["diag_refresh_button"] = ("button", "Refresh Diagnostics")

#: How far back the score history goes when judging whether anything has
#: come near the threshold. Long enough to contain a walk through the
#: room, short enough that yesterday's furniture does not count.
HEALTH_WINDOW_MINUTES = 15


def _states_by_id(states: list[dict]) -> dict:
    return {s.get("entity_id"): s for s in states if isinstance(s, dict)}


@app.get("/api/devices/{device_id}/health")
async def api_device_health(device_id: str) -> dict:
    """What is wrong with this device, judged from Home Assistant.

    Reads the full state snapshot rather than the three targeted entities
    the live view uses. That is the expensive call, and deliberate: the
    question here is which entities *exist*, and a targeted read cannot
    tell a missing entity from a wrongly guessed id — which is exactly the
    failure this is meant to catch. It runs when someone opens the
    diagnosis, not on a timer.
    """
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    if device.status != devices.BuildStatus.SUCCESS:
        raise HTTPException(status_code=409, detail="Die Firmware wurde noch nicht gebaut")

    try:
        states = await ha_client.list_states()
    except ha_client.HomeAssistantUnavailable as err:
        raise HTTPException(
            status_code=503, detail=f"Home Assistant nicht erreichbar: {err}"
        ) from err

    core = entity_resolver.resolve(device, states)
    # The snapshot is already in hand, so repair the stored ids from it.
    # Without this the diagnosis can name an entity in its finding and
    # then offer a button that fails for want of that same entity.
    _persist_entities(device, core)
    diagnostics = entity_resolver.resolve(device, states, _DIAGNOSTIC_SPECS)
    by_id = _states_by_id(states)

    diagnostic_states = {
        field: by_id.get(entity_id)
        for field, entity_id in diagnostics.items()
        if field.startswith("diag_csi") or field == "diag_traffic_tx_rate"
    }

    # The profile the device is actually running, which a runtime change
    # can have moved away from what was compiled in.
    running_profile = device.config.detection_algorithm
    profile_state = by_id.get(diagnostics.get("diag_profile", ""))
    if isinstance(profile_state, dict) and profile_state.get("state") in health.PROFILE_DEFAULT_THRESHOLD:
        running_profile = profile_state["state"]

    scores: list[float] = []
    score_entity = core.get("entity_movement_score") or device.entity_movement_score
    if score_entity:
        try:
            history = await ha_client.get_history(score_entity, HEALTH_WINDOW_MINUTES)
        except ha_client.HomeAssistantUnavailable:
            history = []
        for entry in history:
            value = health._number(entry if isinstance(entry, dict) else None)
            if value is not None:
                scores.append(value)

    findings = health.inspect(
        profile=running_profile,
        target_pps=device.config.csi_target_pps,
        present_fields=set(core),
        reachable=bool(core or diagnostics),
        threshold_state=by_id.get(core.get("entity_threshold", "")),
        diagnostic_states=diagnostic_states,
        observed_scores=scores,
        window_minutes=HEALTH_WINDOW_MINUTES,
    )

    return {
        "checked_at": time.time(),
        "profile": running_profile,
        "window_minutes": HEALTH_WINDOW_MINUTES,
        "samples": len(scores),
        "findings": [f.as_dict() for f in findings],
        "diagnostics": {
            health.DIAGNOSTIC_LABELS[field]: (state or {}).get("state")
            for field, state in diagnostic_states.items()
            if field in health.DIAGNOSTIC_LABELS
        },
        "can_refresh": "diag_refresh_button" in diagnostics,
    }


@app.post("/api/devices/{device_id}/diagnostics/refresh")
async def api_refresh_diagnostics(device_id: str) -> dict:
    """Press the device's own "Refresh Diagnostics" button.

    ESPectre publishes the CSI rate sensors only when asked. Left alone
    they read `unknown` forever, which is why nobody could see whether
    usable CSI was arriving.
    """
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    try:
        states = await ha_client.list_states()
        found = entity_resolver.resolve(device, states, _DIAGNOSTIC_SPECS)
        button = found.get("diag_refresh_button")
        if not button:
            raise HTTPException(
                status_code=404,
                detail="Home Assistant kennt für dieses Gerät keinen „Refresh Diagnostics“-Knopf",
            )
        await ha_client.call_service("button", "press", button)
    except ha_client.HomeAssistantUnavailable as err:
        raise HTTPException(
            status_code=502, detail=f"Home Assistant nicht erreichbar: {err}"
        ) from err
    return {"status": "ok", "pressed": button}


@app.post("/api/devices/{device_id}/entities/detect")
async def api_detect_entities(device_id: str) -> dict:
    """Re-learn this device's entity ids from Home Assistant.

    The same lookup the state reader does on its own, exposed so a device
    that was flashed before this existed can be repaired without deleting
    and recreating it.
    """
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    try:
        states = await ha_client.list_states()
    except ha_client.HomeAssistantUnavailable as err:
        raise HTTPException(status_code=503, detail=f"Home Assistant nicht erreichbar: {err}") from err

    found = entity_resolver.resolve(device, states)
    if not found:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Home Assistant kennt keine Entities für „{entity_resolver.device_display_name(device)}“. "
                "Prüfe unter Einstellungen → Geräte & Dienste, ob das Gerät als "
                "ESPHome-Integration hinzugefügt wurde — nach dem Flashen muss es "
                "dort einmalig bestätigt werden."
            ),
        )
    for field, entity_id in found.items():
        setattr(device, field, entity_id)
    devices.save_device(device)
    return {"detected": found}


@app.get("/api/devices/{device_id}/credentials")
def api_device_credentials(device_id: str) -> dict:
    """The device's API key and OTA password.

    Separated from the device payload so that drawing the device list
    does not hand out every key in the installation. This is reached one
    device at a time, when someone opens the credentials section.
    """
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    return device.credentials()


@app.get("/api/devices/{device_id}/reachability")
async def api_reachability(device_id: str, host: str | None = None) -> dict:
    """Probe the device on the network and say what answered.

    Settles the question a missing entity cannot: is the device silent, or
    is it answering and merely not adopted by Home Assistant?
    """
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    target = (host or device.ota_address()).strip()
    if not target:
        raise HTTPException(status_code=422, detail="Keine Adresse angegeben")
    result = await reachability.check(target)
    return {
        **result,
        "message": reachability.explain(result),
        "direct_message": reachability.explain_direct(result),
    }


@app.post("/api/devices/{device_id}/ota", status_code=202)
async def api_start_ota(device_id: str, payload: dict | None = None) -> dict:
    """Push the built firmware to the running device over the network."""
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    if device.status != devices.BuildStatus.SUCCESS or not device.firmware_bin:
        raise HTTPException(status_code=409, detail="Die Firmware wurde noch nicht gebaut")

    address = ((payload or {}).get("address") or device.ota_address()).strip()
    if not address:
        raise HTTPException(status_code=422, detail="Keine Adresse angegeben")

    # One lock covers builds and OTA alike: both write the same build
    # directory, and running them together corrupts it.
    if not builder.try_start_build(device_id):
        raise HTTPException(
            status_code=409, detail="Für dieses Gerät läuft bereits ein Build oder Update"
        )

    if address != device.address:
        device.address = address
    device.ota_status = devices.BuildStatus.QUEUED
    devices.save_device(device)

    task = asyncio.create_task(asyncio.to_thread(builder.run_ota, device, address))
    _background_builds.add(task)
    task.add_done_callback(_background_builds.discard)
    return {"status": "queued", "address": address}


@app.get("/api/devices/{device_id}/toolchain")
def api_toolchain_state(device_id: str) -> dict:
    """Whether this board's cross compiler is actually installed.

    PlatformIO downloads it on the first build, so "absent" is normal then;
    "broken" means a download was interrupted and the package has to go.
    """
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    board = BOARDS.get(device.config.board)
    if board is None:
        raise HTTPException(status_code=422, detail=f"Unbekanntes Board '{device.config.board}'")
    return {"board": board.key, "label": board.label, **builder.toolchain_state(board)}


@app.post("/api/devices/{device_id}/toolchain/reset")
def api_reset_toolchain(device_id: str) -> dict:
    """Throw away this board's toolchain package so the next build refetches it.

    Refused while a build is running: deleting the compiler out from under
    a live compile turns one clear failure into a confusing one.
    """
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    board = BOARDS.get(device.config.board)
    if board is None:
        raise HTTPException(status_code=422, detail=f"Unbekanntes Board '{device.config.board}'")
    if device.status in (devices.BuildStatus.QUEUED, devices.BuildStatus.RUNNING):
        raise HTTPException(
            status_code=409,
            detail="Für dieses Gerät läuft gerade ein Build — warte, bis er beendet ist",
        )
    removed = builder.reset_toolchain(board)
    return {"removed": removed, **builder.toolchain_state(board)}


@app.post("/api/devices/{device_id}/build", status_code=202)
async def api_build_device(device_id: str) -> dict:
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    if not builder.try_start_build(device_id):
        raise HTTPException(status_code=409, detail="Für dieses Gerät läuft bereits ein Build")

    device.status = devices.BuildStatus.QUEUED
    devices.save_device(device)
    task = asyncio.create_task(asyncio.to_thread(builder.run_build, device))
    _background_builds.add(task)
    task.add_done_callback(_background_builds.discard)
    return {"status": "queued"}


@app.get("/api/devices/{device_id}/manifest.json")
def api_device_manifest(device_id: str) -> JSONResponse:
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    if device.status != devices.BuildStatus.SUCCESS or not device.firmware_bin:
        raise HTTPException(status_code=409, detail="Die Firmware wurde noch nicht gebaut")
    manifest = {
        "name": f"Echolot – {device.config.friendly_name or device.config.name}",
        "version": str(int(device.updated_at)),
        "new_install_prompt_erase": True,
        "builds": [
            {
                "chipFamily": device.chip_family,
                "parts": [{"path": "firmware.bin", "offset": 0}],
            }
        ],
    }
    # Neither the manifest nor its relative firmware URL may be reused after
    # a rebuild.  In particular, ESP Web Tools otherwise has no visible way
    # to distinguish a stale service-worker/browser response while it says
    # only "Preparing installation".
    return JSONResponse(manifest, headers={"Cache-Control": "no-store"})


# HEAD as well as GET: flashers other than the built-in one — web.esphome.io,
# esptool wrappers, plain download managers — ask for the size before
# fetching, and a 405 there looks to them like a broken link.
@app.api_route("/api/devices/{device_id}/firmware.bin", methods=["GET", "HEAD"])
def api_device_firmware(device_id: str) -> Response:
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    if device.status != devices.BuildStatus.SUCCESS or not device.firmware_bin:
        raise HTTPException(status_code=409, detail="Die Firmware wurde noch nicht gebaut")
    path = devices.device_dir(device_id) / device.firmware_bin
    if not path.exists():
        raise HTTPException(status_code=404, detail="Firmware-Datei fehlt auf der Festplatte")
    # Serve a finite response rather than FileResponse's streamed ASGI body.
    # The Home Assistant Ingress proxy has to relay this request to ESP Web
    # Tools' fetch(), and a stream left open by either hop leaves its dialog
    # indefinitely at "Preparing installation". Factory images are small
    # enough to read once here (normally about 1–2 MB), while Content-Length
    # lets every hop know exactly where the response ends.
    try:
        content = path.read_bytes()
    except OSError as err:
        logger.exception("Could not read firmware for device %s", device_id)
        raise HTTPException(status_code=500, detail="Firmware-Datei konnte nicht gelesen werden") from err
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'inline; filename="firmware.bin"',
        },
    )


@app.get("/api/zones")
def api_list_zones() -> list[dict]:
    return [z.model_dump() for z in zones.list_zones()]


@app.post("/api/zones", status_code=201)
def api_create_zone(payload: dict) -> dict:
    try:
        config = zones.ZoneCreate.model_validate(payload)
    except ValidationError as err:
        raise HTTPException(status_code=422, detail=_validation_detail(err)) from err
    unknown = [d for d in config.device_ids if devices.get_device(d) is None]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unbekannte Geräte-ID(s): {', '.join(unknown)}")
    zone = zones.create_zone(config)
    # A zone appearing has no event behind it, and evaluation no longer
    # happens on request: without this the new zone reads "wird
    # ausgewertet" for up to a whole idle interval.
    evaluator.wake()
    return zone.model_dump()


@app.get("/api/zones/{zone_id}")
def api_get_zone(zone_id: str) -> dict:
    zone = zones.get_zone(zone_id)
    if zone is None:
        raise HTTPException(status_code=404, detail="Zone nicht gefunden")
    return zone.model_dump()


@app.patch("/api/zones/{zone_id}")
def api_update_zone(zone_id: str, payload: dict) -> dict:
    zone = zones.get_zone(zone_id)
    if zone is None:
        raise HTTPException(status_code=404, detail="Zone nicht gefunden")
    try:
        patch = zones.ZoneUpdate.model_validate(payload)
    except ValidationError as err:
        raise HTTPException(status_code=422, detail=_validation_detail(err)) from err
    if patch.device_ids is not None:
        unknown = [d for d in patch.device_ids if devices.get_device(d) is None]
        if unknown:
            raise HTTPException(status_code=422, detail=f"Unbekannte Geräte-ID(s): {', '.join(unknown)}")
    try:
        # apply_update re-validates the merged zone, so a patch that only
        # moves one threshold can still break the enter/exit invariant.
        zone.apply_update(patch)
    except ValidationError as err:
        raise HTTPException(status_code=422, detail=_validation_detail(err)) from err
    zones.save_zone(zone)
    # Members and thresholds changed; the snapshot describes the old zone.
    evaluator.wake()
    return zone.model_dump()


@app.delete("/api/zones/{zone_id}", status_code=204)
def api_delete_zone(zone_id: str) -> None:
    if not zones.delete_zone(zone_id):
        raise HTTPException(status_code=404, detail="Zone nicht gefunden")
    # The retained discovery message is retracted by the export's deletion
    # queue on the next round rather than from here. Publishing it
    # straight away worked only while the broker happened to be up, and
    # reported success either way — which is how an entity could linger
    # in Home Assistant as "unavailable" for good.
    forget_zone_runtime(zone_id)
    evaluator.wake()


@app.get("/api/zones/{zone_id}/state")
def api_zone_state(zone_id: str) -> dict:
    zone = zones.get_zone(zone_id)
    if zone is None:
        raise HTTPException(status_code=404, detail="Zone nicht gefunden")
    return evaluator.state_of(zone)


@app.get("/api/zones/{zone_id}/timeline")
def api_zone_timeline(zone_id: str, limit: int = 50) -> dict:
    """Why this zone is where it is: its transitions, newest first.

    In memory and bounded — for looking at the last while after something
    surprised you, not for auditing. It is empty after a restart, and
    says so rather than pretending the room did nothing.
    """
    if zones.get_zone(zone_id) is None:
        raise HTTPException(status_code=404, detail="Zone nicht gefunden")
    events = timeline.timeline.events(zone_id, limit=max(1, min(limit, 200)))
    return {
        "events": [{**event, "explanation": timeline.explain(event)} for event in events]
    }


@app.get("/api/timeline")
def api_timeline(limit: int = 50) -> dict:
    """Every zone's transitions interleaved, newest first."""
    events = timeline.timeline.all_events(limit=max(1, min(limit, 200)))
    return {
        "events": [{**event, "explanation": timeline.explain(event)} for event in events]
    }


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    # Single-page app: Home Assistant Ingress serves this behind a per-session
    # token path prefix, and every asset/API call below uses relative (no
    # leading slash) URLs so they resolve under that prefix. A second route
    # like "/devices" would break that resolution (its relative fetches would
    # nest one level too deep), so device management is a tab on this page
    # instead of a separate path — see static/app.js.
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")
