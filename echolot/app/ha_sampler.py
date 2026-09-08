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

Only genuinely new readings are recorded. Home Assistant stamps every
state with `last_updated`, so a poll that returns the value it returned
last time is a repeat, not a measurement — counting those would pile up
identical numbers and skew the very distribution the recommendation is
computed from.
"""

import asyncio
import logging
from datetime import datetime

from app.telemetry import Sample

logger = logging.getLogger("echolot.ha_sampler")

#: Home Assistant carries roughly six movement-score updates a second from
#: an ESPectre node. Polling twice a second therefore never invents a
#: sample, and the recommendation needs only twenty per label.
POLL_SECONDS = 0.5

#: The threshold changes when someone recalibrates, not continuously, so
#: it is read once every this many ticks rather than on each one.
THRESHOLD_EVERY = 20


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
    """Feeds one recording device's readings into a sink."""

    def __init__(self, sink, reader, loop: asyncio.AbstractEventLoop | None = None) -> None:
        #: sink(device_id, Sample) — CalibrationStore.ingest in production.
        self._sink = sink
        #: reader(entity_id) -> state dict | None
        self._reader = reader
        #: The application's event loop. FastAPI runs a plain `def` route in
        #: a worker thread, where asyncio.create_task raises "no running
        #: event loop" — so starting a recording from the API needs the loop
        #: handed in rather than discovered. Left unset, start() falls back
        #: to the running loop, which is what tests have.
        self._loop = loop
        self._tasks: dict[str, object] = {}

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def _spawn(self, coro):
        """Schedule the poll loop from whichever thread we happen to be on."""
        if self._loop is not None:
            return asyncio.run_coroutine_threadsafe(coro, self._loop)
        return asyncio.create_task(coro)

    def running_for(self, device_id: str) -> bool:
        task = self._tasks.get(device_id)
        return task is not None and not task.done()

    def start(self, device) -> bool:
        """Begin sampling, unless this device is already being sampled."""
        if self.running_for(device.id):
            return False
        if not device.entity_movement_score and not device.entity_motion:
            return False
        self._tasks[device.id] = self._spawn(self._poll(device))
        return True

    def stop(self, device_id: str) -> None:
        task = self._tasks.pop(device_id, None)
        if task is not None and not task.done():
            task.cancel()

    def stop_all(self) -> None:
        for device_id in list(self._tasks):
            self.stop(device_id)

    async def _poll(self, device) -> None:
        threshold: float | None = None
        last_stamp: float | None = None
        tick = 0
        while True:
            try:
                if tick % THRESHOLD_EVERY == 0 and device.entity_threshold:
                    threshold = _float(await self._reader(device.entity_threshold))
                score_state, motion_state = await asyncio.gather(
                    self._read(device.entity_movement_score),
                    self._read(device.entity_motion),
                )
                sample = build_sample(score_state, motion_state, threshold)
                # A reading Home Assistant has not refreshed is the same
                # reading, not a second one.
                if sample is not None and sample.t != last_stamp:
                    last_stamp = sample.t
                    self._sink(device.id, sample)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad poll must not end the recording
                logger.exception("Abtastung für %s fehlgeschlagen", device.id)
            tick += 1
            await asyncio.sleep(POLL_SECONDS)

    async def _read(self, entity_id: str | None):
        return await self._reader(entity_id) if entity_id else None
