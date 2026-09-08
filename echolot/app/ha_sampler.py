"""Record calibration samples from Home Assistant instead of from the device.

ESPectre's Direct HTTP API is deliberately closed to third parties: the
service is configured `for_first_party_portals()`, which allows only
`https://espectre.dev` and its two siblings, and refuses a request with no
`Origin` header outright (`runtime/direct_http_service.h`,
`direct_http_service_esp_idf.cpp`). The escape hatch upstream provides —
`CONFIG_ESPECTRE_DIRECT_DEV_ORIGINS_ENABLED` — is not declared in any
Kconfig the ESPHome build reaches, so it cannot be switched on from here;
setting it through `sdkconfig_options` would be dropped as an unknown
symbol and silently do nothing.

So the Calibration Lab takes the same three values from where they already
are: the Home Assistant entities the device publishes over the ESPHome
API. That costs a hop and some resolution, and it needs nothing from the
device that it is not already giving Home Assistant.

Readings arrive by subscription (see app/ha_stream.py). Home Assistant
sends a message when a value changes, so there are no repeats to filter —
which polling did need, and which cost three quarters of the data anyway.
Each sample keeps Home Assistant's own `last_updated` as its timestamp.
"""

import asyncio
import logging
from datetime import datetime

from app import ha_client, ha_stream
from app.telemetry import Sample

logger = logging.getLogger("echolot.ha_sampler")


def _float(state) -> float | None:
    if not isinstance(state, dict):
        return None
    raw = state.get("state")
    if raw in (None, "", "unknown", "unavailable"):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _stamp(state) -> float | None:
    """Home Assistant's own `last_updated`, as epoch seconds."""
    if not isinstance(state, dict):
        return None
    raw = state.get("last_updated") or state.get("last_changed")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def build_sample(score_state, motion_state, threshold: float | None) -> Sample | None:
    """One reading, or None when there is nothing measured to record."""
    score = _float(score_state)
    motion = None
    if isinstance(motion_state, dict) and motion_state.get("state") in ("on", "off"):
        motion = motion_state["state"] == "on"
    if score is None and motion is None:
        return None
    stamp = _stamp(score_state) or _stamp(motion_state)
    return Sample(
        t=stamp if stamp is not None else 0.0,
        movement_score=score,
        threshold=threshold,
        motion=motion,
    )


class HomeAssistantSampler:
    """Turns one device's Home Assistant state changes into calibration samples.

    Driven by a subscription rather than a poll, so every update Home
    Assistant receives becomes a sample. Home Assistant only sends a
    message when a value changes, so the repeat-suppression the polling
    version needed is gone with the polling.

    The three entities arrive independently. A sample is emitted when the
    movement score or the motion state changes; the threshold is kept as
    context, because it changes only on recalibration and is never a
    measurement of its own.
    """

    def __init__(self, sink, subscription_factory=None, reader=None) -> None:
        #: sink(device_id, Sample) — CalibrationStore.ingest in production.
        self._sink = sink
        self._factory = subscription_factory or ha_stream.StateSubscription
        #: reader(entity_id) -> state dict, used once per recording to seed
        #: the cache. A subscription only reports *changes*, so an entity
        #: that never changes is never seen: the first twenty-minute
        #: recording came back with an empty threshold column throughout,
        #: because the threshold had been 0.5 all along and there was
        #: nothing to report.
        self._reader = reader or ha_client.get_state
        self._subscriptions: dict[str, object] = {}
        self._latest: dict[str, dict] = {}
        self._loop = None

    def bind(self, loop) -> None:
        """Hand in the application's event loop.

        Recordings start from a plain `def` FastAPI route, which runs in a
        worker thread with no running loop of its own.
        """
        self._loop = loop

    def running_for(self, device_id: str) -> bool:
        return device_id in self._subscriptions

    def status(self, device_id: str) -> dict:
        subscription = self._subscriptions.get(device_id)
        if subscription is None:
            return {"connected": False, "error": None}
        return {"connected": subscription.connected, "error": subscription.error}

    def start(self, device) -> bool:
        if self.running_for(device.id):
            return False
        entity_ids = [
            device.entity_movement_score,
            device.entity_motion,
            device.entity_threshold,
        ]
        if not device.entity_movement_score and not device.entity_motion:
            return False

        cache: dict[str, dict] = {}
        self._latest[device.id] = cache
        self._seed(device, cache)

        def on_state(entity_id: str, state: dict) -> None:
            cache[entity_id] = state
            # The threshold alone is context, not a reading: emitting on it
            # would put a row in the export with no measurement in it.
            if entity_id == device.entity_threshold:
                return
            sample = build_sample(
                cache.get(device.entity_movement_score),
                cache.get(device.entity_motion),
                _float(cache.get(device.entity_threshold)),
            )
            if sample is not None:
                self._sink(device.id, sample)

        subscription = self._factory([e for e in entity_ids if e], on_state, loop=self._loop)
        subscription.start()
        self._subscriptions[device.id] = subscription
        return True

    def _seed(self, device, cache: dict) -> None:
        """Read the three entities once, so unchanging ones are known.

        Scheduled onto the loop rather than awaited: start() is called from
        a worker thread, and a recording must not wait on Home Assistant
        before it begins collecting.
        """
        async def read_all():
            for entity_id in (
                device.entity_threshold,
                device.entity_movement_score,
                device.entity_motion,
            ):
                if not entity_id:
                    continue
                try:
                    state = await self._reader(entity_id)
                except Exception:  # noqa: BLE001 - a missing seed is not fatal
                    logger.warning("Startwert für %s nicht lesbar", entity_id)
                    continue
                # A change that arrived first is newer than this read.
                if state is not None and entity_id not in cache:
                    cache[entity_id] = state

        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(read_all(), self._loop)
            return
        try:
            asyncio.get_running_loop().create_task(read_all())
        except RuntimeError:
            # No loop to schedule on. Seeding is an optimisation — the
            # subscription still delivers everything that changes — so a
            # recording must not fail for want of it.
            logger.debug("Kein Event-Loop zum Vorbelegen der Startwerte")

    def stop(self, device_id: str) -> None:
        subscription = self._subscriptions.pop(device_id, None)
        self._latest.pop(device_id, None)
        if subscription is not None:
            subscription.stop()

    def stop_all(self) -> None:
        for device_id in list(self._subscriptions):
            self.stop(device_id)
