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
import itertools
import logging
import time
from collections import deque

from app import ha_client, ha_stream
from app.ha_sampler import build_sample, _float

logger = logging.getLogger("echolot.live_presence")

#: How much history to keep. Comfortably more than the sixty seconds the
#: rate is measured over, so a window is never short of its own span.
WINDOW_SECONDS = 180.0


#: Every stream ever opened gets its own number.
#:
#: A stream is rebuilt when a device's entity ids change: the buffer
#: starts empty and the readings may come from different entities
#: entirely, so anything remembered about the old one is an answer to a
#: different question. Callers need to be able to tell one from the
#: other, and `id()` cannot do it — CPython reuses an address as soon as
#: the old object is collected, which is exactly when the replacement is
#: allocated.
_generations = itertools.count(1)


class DeviceStream:
    """The live state of one device: its subscription, cache and window."""

    def __init__(self, device, *, subscription_factory=None, reader=None, loop=None):
        self.device_id = device.id
        self.generation = next(_generations)
        self._entities = {
            "score": device.entity_movement_score,
            "motion": device.entity_motion,
            "threshold": device.entity_threshold,
        }
        #: What this stream is subscribed to, so `reconcile` can notice
        #: when the device's entity ids have been corrected underneath it.
        self.entity_fingerprint = tuple(
            self._entities[role] for role in ("score", "motion", "threshold")
        )
        self._cache: dict[str, dict] = {}
        self._samples: deque = deque()
        self._listeners: set = set()
        #: Told that *something* changed, without a measurement attached.
        #: See `_notify_change`.
        self._change_listeners: set = set()
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
        self._change_listeners.clear()

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
        previous = self._cache.get(entity_id)
        self._cache[entity_id] = state

        # Only the movement score is a measurement. The threshold and the
        # motion boolean are context: they say what a reading means, not
        # that there is a new one.
        #
        # Until 0.13.5 a motion event also produced a sample — built from
        # the *cached* score, and carrying that score's own older
        # timestamp, because build_sample takes the score's stamp when it
        # has one. So one score reading plus a motion flip put the same
        # number into the window twice, at the same instant: the crossing
        # count went up while the observed span did not, which inflates a
        # rate measured per second. Worse, it made the live path and the
        # recorder import disagree — the import reads the score series
        # alone — so a profile learned from history judged data that had
        # been counted differently.
        if entity_id != self._entities["score"]:
            # Not a measurement — but still news. Until 0.13.5 this
            # return was the end of it, so motion flipping on notified
            # nobody: the zone kept whatever the evaluator last worked
            # out until the idle timer came round, up to ten seconds
            # after the device had already decided. Reported as R3.
            if previous is None or previous.get("state") != state.get("state"):
                self._notify_change()
            return

        sample = build_sample(
            self._cache.get(self._entities["score"]),
            self._cache.get(self._entities["motion"]),
            _float(self._cache.get(self._entities["threshold"])),
        )
        if sample is None:
            return

        # A reading is identified by the instant Home Assistant stamped it
        # with. The same instant arriving twice is one measurement
        # delivered twice, not two measurements — while the same *value*
        # at a new instant is a real second reading and is kept. An older
        # stamp than the newest would also break the ordering `_trim` and
        # `window` rely on.
        if self._samples and sample.t <= self._samples[-1].t:
            logger.debug(
                "Messwert für %s verworfen: Zeitstempel %s nicht neuer als %s",
                self.device_id, sample.t, self._samples[-1].t,
            )
            return

        self._samples.append(sample)
        self._trim()
        for listener in tuple(self._listeners):
            try:
                listener(self.device_id, sample)
            except Exception:  # noqa: BLE001 - one bad listener must not stop the rest
                logger.exception("Listener für %s fehlgeschlagen", self.device_id)
        self._notify_change()

    def _notify_change(self) -> None:
        """Something about this device changed; no measurement implied.

        Separate from the sample listeners on purpose. A sample listener
        is handed a reading and is entitled to assume there is one — the
        calibration recorder writes it down. A change listener is only
        being told to look again, which is what the evaluator needs and
        what motion and threshold events can honestly offer.

        A duplicate score is deliberately not a change: it is dropped
        from the window above, so there is nothing new to look at.
        """
        for listener in tuple(self._change_listeners):
            try:
                listener(self.device_id)
            except Exception:  # noqa: BLE001 - one bad listener must not stop the rest
                logger.exception("Change-Listener für %s fehlgeschlagen", self.device_id)

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

    @property
    def listeners(self) -> tuple:
        """Whoever is attached, so a rebuilt stream can take them over."""
        return tuple(self._listeners)

    @property
    def change_listeners(self) -> tuple:
        return tuple(self._change_listeners)

    def add_listener(self, listener) -> None:
        self._listeners.add(listener)

    def remove_listener(self, listener) -> None:
        self._listeners.discard(listener)

    def add_change_listener(self, listener) -> None:
        self._change_listeners.add(listener)

    def remove_change_listener(self, listener) -> None:
        self._change_listeners.discard(listener)


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
        #: Called as `(device_id, sample)` after any device's reading
        #: changes. The MQTT export uses it to publish when something
        #: happens rather than when a timer next comes round.
        self._on_any_change = None
        #: Called as `(device_id)` whenever anything about a device
        #: changed, measurement or not. The evaluator wakes on this.
        self._on_any_state = None

    def bind(self, loop) -> None:
        self._loop = loop

    def stream(self, device_id: str) -> DeviceStream | None:
        return self._streams.get(device_id)

    def on_any_change(self, callback) -> None:
        """Register one `(device_id, sample)` callback for any device.

        Attached to every stream, including ones opened later, so the
        caller does not have to track devices coming and going itself.
        """
        self._on_any_change = callback
        for stream in self._streams.values():
            stream.add_listener(callback)

    def on_any_state(self, callback) -> None:
        """Register one `(device_id)` callback for any change at all.

        Motion turning on is not a measurement and never reaches
        `on_any_change`, but it is exactly the kind of thing the
        evaluation should not wait ten seconds to hear about.
        """
        self._on_any_state = callback
        for stream in self._streams.values():
            stream.add_change_listener(callback)

    def statuses(self) -> dict:
        return {
            device_id: {"connected": stream.connected, "error": stream.error}
            for device_id, stream in self._streams.items()
        }

    @staticmethod
    def _fingerprint(device) -> tuple:
        return (
            device.entity_movement_score,
            device.entity_motion,
            device.entity_threshold,
        )

    def reconcile(self, devices) -> None:
        """Open a stream for every device that can supply readings, close the rest.

        A device is matched on its id *and* on the entities it wants. Only
        the id was checked until 0.13.5, so correcting an entity id — by
        hand or through the automatic lookup — repaired the stored value
        while the running subscription stayed on the old entity: the
        device page said it was fixed and no readings ever arrived. The
        automatic lookup made this worse, because it fires exactly when
        the ids are wrong.
        """
        wanted = {
            device.id: device
            for device in devices
            if (device.entity_movement_score or device.entity_motion)
        }
        carry_over: dict[str, tuple] = {}
        carry_over_changes: dict[str, tuple] = {}
        for device_id in list(self._streams):
            if device_id not in wanted:
                self._streams.pop(device_id).stop()
                continue
            existing = self._streams[device_id]
            if existing.entity_fingerprint != self._fingerprint(wanted[device_id]):
                logger.info(
                    "Entities für %s geändert — Abonnement wird neu aufgebaut", device_id
                )
                # The cached readings describe the old entities and are
                # dropped with the stream. The listeners are not: one of
                # them may be a calibration recording in progress, and
                # silently detaching it would leave a session that looks
                # live and collects nothing — the exact failure the
                # recording exists to rule out.
                carry_over[device_id] = tuple(existing.listeners)
                carry_over_changes[device_id] = tuple(existing.change_listeners)
                self._streams.pop(device_id).stop()
        for device_id, device in wanted.items():
            if device_id not in self._streams:
                stream = DeviceStream(
                    device,
                    subscription_factory=self._factory,
                    reader=self._reader,
                    loop=self._loop,
                )
                if self._on_any_change is not None:
                    stream.add_listener(self._on_any_change)
                if self._on_any_state is not None:
                    stream.add_change_listener(self._on_any_state)
                for listener in carry_over.get(device_id, ()):
                    stream.add_listener(listener)
                for listener in carry_over_changes.get(device_id, ()):
                    stream.add_change_listener(listener)
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
