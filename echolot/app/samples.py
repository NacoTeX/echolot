"""One canonical stream of readings per device, and it says where they came from.

Echolot can hear a device two ways: through the Home Assistant entities it
publishes over the ESPHome API, and through ESPectre's own Direct HTTP
event stream on the device itself. Until now both ran, both fed the
calibration store, and the confidence fusion read only the direct one —
so a device without the Direct API contributed nothing to fusion, and a
device with it could have the same movement counted twice.

Two readings of the same movement from two transports are one
measurement delivered twice. The crossing rate is *events per second*, so
counting it twice does not add detail, it doubles the number.

So there is one canonical source per device, it is named on every reading
it produces, and a reading from any other source is refused rather than
quietly mixed in. The refusals are counted, because a collector that is
running and contributing nothing should be visible rather than
mysterious.

Which source is canonical is a declared setting, not a race between
whichever transport happens to deliver first: a rate profile is learned
under one source, and switching underneath it silently would change the
measurement without changing its definition. `ECHOLOT_SAMPLE_SOURCE`
picks; Home Assistant is the default because it is the one that always
works — see app/ha_sampler.py for why the direct transport does not.
"""

import asyncio
import logging
import os
import threading
import time
from collections import deque

logger = logging.getLogger("echolot.samples")

SOURCE_HOME_ASSISTANT = "home_assistant"
SOURCE_DIRECT = "direct"
SOURCES = (SOURCE_HOME_ASSISTANT, SOURCE_DIRECT)
DEFAULT_SOURCE = SOURCE_HOME_ASSISTANT

#: Roughly an hour at four readings a second, per device.
MAX_POINTS = 3_600

#: Why the direct collector does not run unless somebody asks for it.
#:
#: Checked against the pinned ESPectre commit rather than assumed. At
#: `ce23b0b6` an ESPHome-built device configures its Direct HTTP service
#: with `DirectHttpServiceConfig::for_first_party_portals()`, which
#: allows exactly three origins — https://espectre.dev and its two
#: siblings — and the ESPHome frontend passes `allow_missing_origin =
#: false` (`espectre.cpp`, the RuntimeDirectHttpBridgeConfig literal).
#: Loopback origins are compiled out unless
#: `CONFIG_ESPECTRE_DIRECT_DEV_ORIGINS_ENABLED` is set, and the Kconfig
#: the ESPHome build reaches (`src/cpp/Kconfig.projbuild` and the
#: espectre_config one it sources) does not declare that symbol at all —
#: only the native frontend and the micro firmware do.
#:
#: So every request from this add-on gets 403 "Origin rejected" unless it
#: claims to be espectre.dev, and claiming to be somebody else is not
#: something this add-on will do. The collector stays available for
#: firmware that permits it, off by default, and says why.
DIRECT_COLLECTOR_REASON = (
    "ESPectre lässt am gepinnten Commit nur espectre.dev als Origin zu und "
    "der ESPHome-Build kann das nicht ändern. Ohne eine vorgetäuschte "
    "Herkunft antwortet das Gerät mit 403."
)


def direct_collector_enabled() -> bool:
    """Whether to run the direct collector at all.

    Off unless asked, because at the pinned upstream it can only collect
    403s — see DIRECT_COLLECTOR_REASON. `ECHOLOT_DIRECT_COLLECTOR=true`
    turns it on for firmware that accepts this add-on.
    """
    if configured_source() == SOURCE_DIRECT:
        return True
    wanted = (os.environ.get("ECHOLOT_DIRECT_COLLECTOR") or "").strip().lower()
    return wanted in ("1", "true", "yes", "on")


def configured_source() -> str:
    """The canonical source, as configured. Anything unknown is ignored."""
    wanted = (os.environ.get("ECHOLOT_SAMPLE_SOURCE") or "").strip().lower()
    if wanted in SOURCES:
        return wanted
    if wanted:
        logger.warning(
            "Unbekannte Datenquelle %r — es gilt %s", wanted, DEFAULT_SOURCE
        )
    return DEFAULT_SOURCE


class SampleBus:
    """The readings every consumer reads, tagged with where they came from."""

    def __init__(self, max_points: int = MAX_POINTS) -> None:
        self._lock = threading.RLock()
        self._samples: dict[str, deque] = {}
        self._accepted: dict[str, int] = {}
        #: source -> how many readings were refused because that source is
        #: not the canonical one. Not an error; a fact worth showing.
        self._refused: dict[str, int] = {}
        self._listeners: set = set()
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._loop = None

    # --- configuration ----------------------------------------------------

    @property
    def source(self) -> str:
        return configured_source()

    def bind(self, loop) -> None:
        """The loop asyncio subscribers live on.

        Readings arrive on a websocket thread and on the direct
        collector's task; a browser watching the live trace is neither.
        """
        self._loop = loop

    # --- producing --------------------------------------------------------

    def publish(self, device_id: str, sample, *, source: str) -> bool:
        """Take one reading. False when it is not from the canonical source.

        Returning False rather than raising: a collector that is running
        against a source nobody reads is a configuration state, not a
        fault, and it must not take the collector down.
        """
        if source not in SOURCES:
            raise ValueError(f"Unbekannte Quelle {source!r}")
        if source != self.source:
            with self._lock:
                self._refused[source] = self._refused.get(source, 0) + 1
            return False

        stamped = sample if getattr(sample, "source", None) == source else _tag(sample, source)
        with self._lock:
            self._samples.setdefault(device_id, deque(maxlen=MAX_POINTS)).append(stamped)
            self._accepted[device_id] = self._accepted.get(device_id, 0) + 1
            listeners = tuple(self._listeners)
            queues = tuple(self._subscribers.get(device_id, ()))

        for listener in listeners:
            try:
                listener(device_id, stamped)
            except Exception:  # noqa: BLE001 - one consumer must not stop collection
                logger.exception("Sample-Listener für %s fehlgeschlagen", device_id)
        for queue in queues:
            self._offer(queue, stamped)
        return True

    def _offer(self, queue: asyncio.Queue, sample) -> None:
        """Hand a reading to one watcher, dropping the oldest when full."""
        def deliver():
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(sample)

        loop = self._loop
        if loop is None:
            deliver()
            return
        try:
            if asyncio.get_running_loop() is loop:
                deliver()
                return
        except RuntimeError:
            pass
        try:
            loop.call_soon_threadsafe(deliver)
        except RuntimeError:
            logger.debug("Kein Loop mehr für Abonnenten von Messwerten")

    # --- consuming --------------------------------------------------------

    def window(self, device_id: str, seconds: float, *, now: float | None = None) -> list[dict]:
        cutoff = (time.time() if now is None else now) - seconds
        with self._lock:
            return [
                sample.as_dict()
                for sample in self._samples.get(device_id, ())
                if sample.t >= cutoff
            ]

    def latest(self, device_id: str) -> dict | None:
        with self._lock:
            samples = self._samples.get(device_id)
            return samples[-1].as_dict() if samples else None

    def snapshot(self, device_id: str, *, seconds: int = 1800) -> dict:
        """What the dashboard and the fusion read.

        Same shape the direct collector's own snapshot had, so a consumer
        does not have to know which transport is behind it — plus the
        source, so it can say.
        """
        points = self.window(device_id, max(1, min(int(seconds), 86_400)))
        with self._lock:
            accepted = self._accepted.get(device_id, 0)
            refused = dict(self._refused)
        return {
            "source": self.source,
            "available": bool(points),
            "sample_count": accepted,
            "refused_by_source": refused,
            "points": points,
        }

    def status(self) -> dict:
        with self._lock:
            return {
                "source": self.source,
                "devices": dict(self._accepted),
                "refused_by_source": dict(self._refused),
            }

    def forget(self, device_id: str) -> None:
        with self._lock:
            self._samples.pop(device_id, None)
            self._accepted.pop(device_id, None)
            self._subscribers.pop(device_id, None)

    # --- watchers ---------------------------------------------------------

    def add_listener(self, listener) -> None:
        with self._lock:
            self._listeners.add(listener)

    def remove_listener(self, listener) -> None:
        with self._lock:
            self._listeners.discard(listener)

    def subscribe(self, device_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        with self._lock:
            self._subscribers.setdefault(device_id, set()).add(queue)
        return queue

    def unsubscribe(self, device_id: str, queue: asyncio.Queue) -> None:
        with self._lock:
            watchers = self._subscribers.get(device_id)
            if watchers:
                watchers.discard(queue)
                if not watchers:
                    self._subscribers.pop(device_id, None)


def _tag(sample, source: str):
    """The same reading, saying where it came from."""
    from dataclasses import replace

    return replace(sample, source=source)


bus = SampleBus()
