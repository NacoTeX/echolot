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

Readings arrive from the live subscription in app/live_presence.py, which
listens whether or not a recording is running. Home Assistant sends a
message when a value changes, so there are no repeats to filter — which
polling did need, and which cost three quarters of the data anyway. Each
sample keeps Home Assistant's own `last_updated` as its timestamp.
"""

import logging
from datetime import datetime

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
    """Feeds a recording from the live stream that is already listening.

    It used to open its own subscription per recording. The live service
    (app/live_presence.py) now holds one per device permanently, because
    the crossing rate needs a window that exists before anybody presses
    record. A second subscription to the same three entities would buy
    nothing, so this attaches to the first.
    """

    def __init__(self, sink, live) -> None:
        #: sink(device_id, Sample) — CalibrationStore.ingest in production.
        self._sink = sink
        self._live = live
        self._attached: dict[str, object] = {}

    def running_for(self, device_id: str) -> bool:
        return device_id in self._attached

    def status(self, device_id: str) -> dict:
        stream = self._live.stream(device_id)
        if stream is None or device_id not in self._attached:
            return {"connected": False, "error": None}
        return {"connected": stream.connected, "error": stream.error}

    def start(self, device) -> bool:
        if self.running_for(device.id):
            return False
        stream = self._live.stream(device.id)
        if stream is None:
            return False

        def listener(device_id, sample):
            self._sink(device_id, sample)

        stream.add_listener(listener)
        self._attached[device.id] = listener
        return True

    def stop(self, device_id: str) -> None:
        listener = self._attached.pop(device_id, None)
        stream = self._live.stream(device_id)
        if listener is not None and stream is not None:
            stream.remove_listener(listener)

    def stop_all(self) -> None:
        for device_id in list(self._attached):
            self.stop(device_id)
