"""Direct, local ESPectre telemetry collection.

Home Assistant entities are the durable integration surface, but polling them
every five seconds throws away nearly all of ESPectre's live signal.  This
module maintains one SSE connection per eligible device, keeps a bounded local
history, and fans samples out to dashboard clients without exposing devices to
the browser or crossing the Home Assistant Ingress origin.
"""

import asyncio
import json
import logging
import os
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace

import httpx

logger = logging.getLogger("echolot.telemetry")

DIRECT_PORT = 62587
RECONCILE_SECONDS = 10
RECONNECT_MAX_SECONDS = 30
MAX_POINTS = 3_600
#: The first entry is ESPectre's own, from `runtime/direct_http_protocol.h`:
#: ESPECTRE_DIRECT_HTTP_EVENTS_ENDPOINT = "/espectre/v1/events". The rest
#: were guesses made before that was checked, and none of them matched —
#: the collector connected, took a 404 on every path, and recorded nothing.
#: They stay only as fallbacks for firmware that serves somewhere else.
DEFAULT_PATHS = (
    "/espectre/v1/events",
    "/events",
    "/api/events",
    "/stream",
    "/api/v1/events",
)


@dataclass(frozen=True)
class Sample:
    t: float
    movement_score: float | None
    threshold: float | None
    motion: bool | None

    def as_dict(self) -> dict:
        return asdict(self)


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _boolean(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "on", "detected", "occupied", "motion"}:
            return True
        if lowered in {"0", "false", "off", "clear", "vacant", "idle"}:
            return False
    return None


def _first(data: dict, keys: tuple[str, ...], converter):
    for key in keys:
        if key in data:
            converted = converter(data[key])
            if converted is not None:
                return converted
    return None


def parse_payload(payload: str, *, now: float | None = None) -> Sample | None:
    """Accept ESPectre JSON while tolerating small API naming changes.

    SSE framing is handled by the collector; this function deliberately only
    accepts JSON objects.  Unknown messages (heartbeats, version notices) are
    ignored rather than manufacturing zero-valued presence readings.
    """
    try:
        data = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    for envelope in ("data", "payload", "telemetry"):
        if isinstance(data.get(envelope), dict):
            data = data[envelope]
            break

    score = _first(data, ("movement_score", "movementScore", "movement", "score"), _number)
    threshold = _first(
        data, ("threshold", "detection_threshold", "detectionThreshold"), _number
    )
    # ESPectre's motion event is {"timestamp_ms":…,"state":"motion"|"idle",
    # "score":…} — the state lives under `state`, and _boolean already
    # maps those two words.
    motion = _first(
        data, ("motion", "state", "detected", "presence", "occupied"), _boolean
    )
    if score is None and threshold is None and motion is None:
        return None
    received_at = now or time.time()
    stamp = (
        _number(data.get("timestamp"))
        or _number(data.get("timestamp_ms"))
        or _number(data.get("t"))
        or received_at
    )
    # Millisecond epoch values are common in browser-oriented APIs.
    if stamp > 10_000_000_000:
        stamp /= 1000
    # A smaller value is device uptime, not wall time. Arrival time makes
    # samples from several devices comparable and keeps history filtering sane.
    if stamp < 946_684_800:  # 2000-01-01
        stamp = received_at
    return Sample(t=stamp, movement_score=score, threshold=threshold, motion=motion)


class TelemetryHub:
    def __init__(self) -> None:
        self._provider: Callable[[], Iterable] | None = None
        self._supervisor: asyncio.Task | None = None
        self._workers: dict[str, tuple[str, asyncio.Task]] = {}
        self._samples: dict[str, deque[Sample]] = {}
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._listeners: set[Callable[[str, Sample], None]] = set()
        self._status: dict[str, dict] = {}
        #: Last threshold seen per device, to fill in on motion events.
        self._last_threshold: dict[str, float] = {}

    async def start(self, provider: Callable[[], Iterable]) -> None:
        if self._supervisor is not None:
            return
        self._provider = provider
        await self._reconcile()
        self._supervisor = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        tasks = [task for _, task in self._workers.values()]
        if self._supervisor:
            self._supervisor.cancel()
            tasks.append(self._supervisor)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
        self._supervisor = None

    async def _supervise(self) -> None:
        while True:
            await asyncio.sleep(RECONCILE_SECONDS)
            await self._reconcile()

    async def _reconcile(self) -> None:
        wanted = {}
        for device in self._provider() if self._provider else ():
            if device.config.direct_api and getattr(device.status, "value", device.status) == "success":
                wanted[device.id] = device.ota_address()

        for device_id, (host, task) in list(self._workers.items()):
            if wanted.get(device_id) != host or task.done():
                task.cancel()
                self._workers.pop(device_id, None)
        for device_id, host in wanted.items():
            if device_id not in self._workers:
                self._workers[device_id] = (host, asyncio.create_task(self._collect(device_id, host)))

    @staticmethod
    def _paths() -> tuple[str, ...]:
        configured = os.environ.get("ESPECTRE_DIRECT_PATHS")
        if not configured:
            return DEFAULT_PATHS
        paths = tuple(path.strip() for path in configured.split(",") if path.strip())
        return tuple(path if path.startswith("/") else f"/{path}" for path in paths)

    async def _collect(self, device_id: str, host: str) -> None:
        delay = 1
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=None)) as client:
            while True:
                connected = False
                for path in self._paths():
                    url = f"http://{host}:{DIRECT_PORT}{path}"
                    try:
                        async with client.stream("GET", url, headers={"Accept": "text/event-stream"}) as response:
                            if response.status_code != 200:
                                continue
                            connected = True
                            delay = 1
                            self._status[device_id] = {"connected": True, "url": url, "error": None}
                            data_lines = []
                            async for line in response.aiter_lines():
                                if line.startswith("data:"):
                                    data_lines.append(line[5:].lstrip())
                                elif not line and data_lines:
                                    self.ingest(device_id, "\n".join(data_lines))
                                    data_lines.clear()
                                elif line.startswith("{"):
                                    self.ingest(device_id, line)
                            if data_lines:
                                self.ingest(device_id, "\n".join(data_lines))
                    except (httpx.HTTPError, OSError) as err:
                        self._status[device_id] = {
                            "connected": False,
                            "url": url,
                            "error": str(err),
                        }
                    if connected:
                        self._status[device_id] = {
                            **self._status.get(device_id, {}),
                            "connected": False,
                            "error": "Telemetrieverbindung wurde beendet",
                        }
                        break
                if not connected:
                    self._status[device_id] = {
                        "connected": False,
                        "url": None,
                        "error": "Kein ESPectre-Telemetrie-Endpunkt erreichbar",
                    }
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX_SECONDS)

    def ingest(self, device_id: str, payload: str, *, now: float | None = None) -> Sample | None:
        sample = parse_payload(payload, now=now)
        if sample is None:
            return None
        # ESPectre splits the two values across two events: the motion event
        # carries `score` but no threshold, the sensing event carries the
        # threshold and no score. Remembering the last threshold lets a
        # motion sample say what it was measured against — which is what the
        # calibration export and the fusion's device_threshold basis need.
        if sample.threshold is not None:
            self._last_threshold[device_id] = sample.threshold
            if sample.movement_score is None and sample.motion is None:
                # A configuration echo, not a measurement. Recording it
                # would put a blank row in every calibration export and a
                # gap in the dashboard trace.
                return None
        else:
            carried = self._last_threshold.get(device_id)
            if carried is not None:
                sample = replace(sample, threshold=carried)
        self._samples.setdefault(device_id, deque(maxlen=MAX_POINTS)).append(sample)
        self._status[device_id] = {
            **self._status.get(device_id, {}),
            "connected": True,
            "last_sample": sample.t,
            "error": None,
        }
        for queue in self._subscribers.get(device_id, ()):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(sample)
        for listener in tuple(self._listeners):
            try:
                listener(device_id, sample)
            except Exception:  # noqa: BLE001 - one recorder must not kill collection
                logger.exception("Telemetry listener failed for %s", device_id)
        return sample

    def add_listener(self, listener: Callable[[str, Sample], None]) -> None:
        self._listeners.add(listener)

    def remove_listener(self, listener: Callable[[str, Sample], None]) -> None:
        self._listeners.discard(listener)

    def snapshot(self, device_id: str, *, seconds: int = 1800) -> dict:
        samples = self._samples.get(device_id, ())
        cutoff = time.time() - max(1, min(seconds, 86_400))
        points = [sample.as_dict() for sample in samples if sample.t >= cutoff]
        status = self._status.get(device_id, {"connected": False, "error": "Noch keine Direktdaten"})
        return {**status, "available": bool(points), "points": points}

    def subscribe(self, device_id: str) -> asyncio.Queue:
        queue = asyncio.Queue(maxsize=100)
        self._subscribers.setdefault(device_id, set()).add(queue)
        return queue

    def unsubscribe(self, device_id: str, queue: asyncio.Queue) -> None:
        subscribers = self._subscribers.get(device_id)
        if subscribers:
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(device_id, None)


hub = TelemetryHub()
