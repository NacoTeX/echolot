"""One live subscription per device, and a rolling window of what it sent.

Until now readings were only collected while a calibration recording ran.
Presence from the crossing rate needs them all the time: the verdict is
"how often did this room cross in the last minute", and there is no last
minute unless somebody has been listening.

So this owns the subscription, and everything else attaches to it. The
calibration sampler used to open its own; two subscriptions to the same
three entities would work, but the second one buys nothing and the first
one is already the thing that knows what arrived.

The window is kept by time, not by count. The score only changes when it
changes — a twenty-minute recording had gaps up to 18.7 s with no message
at all — so a fixed number of readings would span a wildly varying
stretch of wall clock, and the rate is per second.
"""

import asyncio
import logging
import time
from collections import deque

from app import ha_client, ha_stream
from app.ha_sampler import build_sample, _float

logger = logging.getLogger("echolot.live_presence")

#: How much history to keep. Comfortably more than the sixty seconds the
#: rate is measured over, so a window is never short of its own span.
WINDOW_SECONDS = 180.0


class DeviceStream:
    """The live state of one device: its subscription, cache and window."""

    def __init__(self, device, *, subscription_factory=None, reader=None, loop=None):
        self.device_id = device.id
        self._entities = {
            "score": device.entity_movement_score,
            "motion": device.entity_motion,
            "threshold": device.entity_threshold,
        }
        self._cache: dict[str, dict] = {}
        self._samples: deque = deque()
        self._listeners: set = set()
        self._reader = reader
        self._loop = loop
        factory = subscription_factory or ha_stream.StateSubscription
        self._subscription = factory(
            [e for e in self._entities.values() if e], self._on_state, loop=loop
        )

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._subscription.start()

    def stop(self) -> None:
        self._subscription.stop()
        self._listeners.clear()

    @property
    def entity_ids(self) -> list[str]:
        return [e for e in self._entities.values() if e]

    @property
    def connected(self) -> bool:
        return bool(getattr(self._subscription, "connected", False))

    @property
    def error(self):
        return getattr(self._subscription, "error", None)

    def seed(self, states: dict) -> None:
        """Prime the cache with current values, for entities that never change.

        A subscription reports changes only, so a threshold that has been
        0.5 since the device booted is never announced and would be absent
        from every sample.
        """
        for entity_id, state in states.items():
            if state is not None and entity_id not in self._cache:
                self._cache[entity_id] = state

    # --- data --------------------------------------------------------------

    def _on_state(self, entity_id: str, state: dict) -> None:
        self._cache[entity_id] = state
        if entity_id == self._entities["threshold"]:
            # Context, not a measurement.
            return
        sample = build_sample(
            self._cache.get(self._entities["score"]),
            self._cache.get(self._entities["motion"]),
            _float(self._cache.get(self._entities["threshold"])),
        )
        if sample is None:
            return
        self._samples.append(sample)
        self._trim()
        for listener in tuple(self._listeners):
            try:
                listener(self.device_id, sample)
            except Exception:  # noqa: BLE001 - one bad listener must not stop the rest
                logger.exception("Listener für %s fehlgeschlagen", self.device_id)

    def _trim(self) -> None:
        """Bound the buffer relative to the newest reading, not the clock.

        Home Assistant stamps a reading with its own `last_updated`, and
        those can lag the wall clock — a device catching up after a
        reconnect delivers a burst of older stamps. Trimming against
        time.time() would discard them on arrival. Evaluation still uses
        the wall clock: window() filters by it.
        """
        if not self._samples:
            return
        cutoff = self._samples[-1].t - WINDOW_SECONDS
        while self._samples and self._samples[0].t < cutoff:
            self._samples.popleft()

    def window(self, seconds: float, *, now: float | None = None) -> list[dict]:
        """The readings from the last `seconds`, as plain rows."""
        cutoff = (now if now is not None else time.time()) - seconds
        return [
            {"t": sample.t, "movement_score": sample.movement_score, "motion": sample.motion}
            for sample in self._samples
            if sample.t >= cutoff
        ]

    def add_listener(self, listener) -> None:
        self._listeners.add(listener)

    def remove_listener(self, listener) -> None:
        self._listeners.discard(listener)


#: How often the device list is re-read. Devices are created, built and
#: renamed from the UI; polling for that beats threading a callback
#: through every route that could change one.
RECONCILE_SECONDS = 30


class LivePresence:
    """A stream per device, kept in step with the device list."""

    def __init__(self, *, subscription_factory=None, reader=None) -> None:
        self._streams: dict[str, DeviceStream] = {}
        self._factory = subscription_factory
        self._reader = reader or ha_client.get_state
        self._loop = None
        self._supervisor = None

    def bind(self, loop) -> None:
        self._loop = loop

    def stream(self, device_id: str) -> DeviceStream | None:
        return self._streams.get(device_id)

    def statuses(self) -> dict:
        return {
            device_id: {"connected": stream.connected, "error": stream.error}
            for device_id, stream in self._streams.items()
        }

    def reconcile(self, devices) -> None:
        """Open a stream for every device that can supply readings, close the rest."""
        wanted = {
            device.id: device
            for device in devices
            if (device.entity_movement_score or device.entity_motion)
        }
        for device_id in list(self._streams):
            if device_id not in wanted:
                self._streams.pop(device_id).stop()
        for device_id, device in wanted.items():
            if device_id not in self._streams:
                stream = DeviceStream(
                    device,
                    subscription_factory=self._factory,
                    reader=self._reader,
                    loop=self._loop,
                )
                self._streams[device_id] = stream
                stream.start()
                self._seed(stream)

    def _seed(self, stream: DeviceStream) -> None:
        """Read each entity once, so unchanging ones are known from the start."""
        async def read_all():
            states = {}
            for entity_id in stream.entity_ids:
                try:
                    states[entity_id] = await self._reader(entity_id)
                except Exception:  # noqa: BLE001 - a missing seed is not fatal
                    logger.warning("Startwert für %s nicht lesbar", entity_id)
            stream.seed(states)

        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(read_all(), self._loop)
            return
        try:
            asyncio.get_running_loop().create_task(read_all())
        except RuntimeError:
            logger.debug("Kein Event-Loop zum Vorbelegen der Startwerte")

    async def run(self, provider) -> None:
        """Keep the streams in step with what `provider()` returns."""
        if self._supervisor is not None:
            return
        self.reconcile(provider())
        self._supervisor = asyncio.create_task(self._supervise(provider))

    async def _supervise(self, provider) -> None:
        while True:
            await asyncio.sleep(RECONCILE_SECONDS)
            try:
                self.reconcile(provider())
            except Exception:  # noqa: BLE001 - a bad pass must not end the loop
                logger.exception("Abgleich der Live-Ströme fehlgeschlagen")

    def stop_all(self) -> None:
        if self._supervisor is not None and not self._supervisor.done():
            self._supervisor.cancel()
        self._supervisor = None
        for device_id in list(self._streams):
            self._streams.pop(device_id).stop()
