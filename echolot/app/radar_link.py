"""Live link to every radar node, over ESPHome's native API.

Home Assistant's state machine is the wrong pipe for positions: the frame
text sensor is disabled there on purpose (the recorder would store every
report), and even enabled it would add a hop and a rate limit. So Echolot
holds its own API connection to each node, next to Home Assistant's —
ESPHome serves several clients at once — with the same encryption key.

Each node gets one long-lived task that connects, finds the entities it
needs by name, subscribes, and reconnects with a backoff when the
connection goes. What it learns is kept as a snapshot and handed to
listeners (the room engine) on every frame.
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

from app.radar_frame import RadarFrame, parse_frame

logger = logging.getLogger("echolot.radar")

API_PORT = 6053
CONNECT_TIMEOUT = 15.0
LIST_TIMEOUT = 10.0
#: Seconds between attempts, the last one repeating.
BACKOFF = (2, 5, 10, 20, 30)
#: The component refreshes its frame line at least once a second
#: (FRAME_HEARTBEAT_MS). Three without one and the frame on hand is no
#: longer a description of the room.
FRAME_STALE_S = 3.0

#: New reports kept for the room engine between two evaluations. The
#: engine runs at most ten times a second; a module reporting faster
#: would otherwise have reports overwritten before anybody read them.
QUEUE_LENGTH = 64
_SEQ_MOD = 2**32

#: Entity name in the firmware template -> role here. Names, not
#: object_ids: ESPHome is phasing object_id out of the API, and the names
#: are what templates/ld2460.yaml.j2 writes.
ENTITY_ROLES = {
    "radar frame": "frame",
    "radar status": "status",
    "radar firmware": "firmware",
    "wifi signal": "wifi_signal",
    "ip address": "ip",
    "radar mount mode": "mount_mode",
    "radar mount height": "mount_height",
    "radar mount angle": "mount_angle",
}
MOUNT_ROLES = ("mount_mode", "mount_height", "mount_angle")
#: The module's own names for how it hangs (ld2460_protocol.h).
MOUNT_MODES = ("side", "top")
#: Mounting states arrive one entity at a time; listeners hear about a
#: change once they have stopped arriving for this long.
MOUNTING_SETTLE_S = 1.0


class MountingError(Exception):
    """The mounting cannot be written: no connection, or a firmware that
    does not have the entities."""


@dataclass
class LinkSnapshot:
    device_id: str
    address: str = ""
    connected: bool = False
    #: Why there is no connection, in words somebody can act on.
    error: str | None = None
    #: The node answers but carries no frame entity — a CSI image, or a
    #: radar image from before the frame line existed.
    no_frame_entity: bool = False
    frame: RadarFrame | None = None
    #: time.monotonic() when the last frame line arrived, repeats
    #: included: the link and the module are alive. None before the first.
    frame_at: float | None = None
    #: time.monotonic() when the last *new* measurement arrived. Both are
    #: receive times: frame format 1 carries no time of its own, and the
    #: delay between the module's report and its arrival here is unknown.
    measured_at: float | None = None
    #: Which connection the frames belong to. A new one — reconnect, or
    #: the device rebooted — starts the sequence afresh.
    session: int = 0
    #: New measurements not yet taken by the room engine:
    #: (index, received_at, frame), oldest first.
    queue: deque = field(default_factory=lambda: deque(maxlen=QUEUE_LENGTH))
    index: int = 0
    #: Counters, per connection: repeats of the last line (the firmware
    #: republishes it as a heartbeat), lines older than the last one,
    #: reports the module made that never arrived here (skipped by the
    #: firmware's rate limit or lost — the two cannot be told apart), and
    #: new measurements pushed out of the queue unread.
    duplicates: int = 0
    out_of_order: int = 0
    reports_skipped: int = 0
    dropped: int = 0
    status: str | None = None
    firmware: str | None = None
    wifi_signal: float | None = None
    ip: str | None = None
    connected_since: float | None = None
    frames_received: int = 0
    attempts: int = 0
    #: How the module says it is mounted — what it read back, never what
    #: was asked for. None until it has said so on this connection.
    #: `mounting_entities`: the firmware has the three entities at all.
    mounting_entities: bool = False
    mount_mode: str | None = None
    mount_height_m: float | None = None
    mount_angle_deg: float | None = None

    def mounting(self) -> dict | None:
        """The module's mounting, once all three are known."""
        if self.mount_mode is None or self.mount_height_m is None or self.mount_angle_deg is None:
            return None
        return {"mode": self.mount_mode, "height_m": self.mount_height_m, "angle_deg": self.mount_angle_deg}

    def record(self, frame: RadarFrame, now: float) -> bool:
        """Take one frame line; True when it is a new measurement.

        Sequence rules, within one connection (see `new_session`):

          same number, same content   a repeat: proves the link is alive,
                                      is no new measurement
          same number, other content  the module's state changed without
                                      a new report (receiving -> quiet):
                                      new, for the state
          ahead by less than 2**31    new; the numbers in between are
                                      reports that never arrived
          otherwise                   older than the last: ignored

        Comparison is modulo 2**32, so the wrap of the device's counter
        after four billion reports is just the next number.
        """
        self.frame_at = now
        last = self.frame
        if last is not None:
            ahead = (frame.seq - last.seq) % _SEQ_MOD
            if ahead == 0 and frame == last:
                self.duplicates += 1
                return False
            if ahead >= _SEQ_MOD // 2:
                self.out_of_order += 1
                return False
            if ahead > 1:
                self.reports_skipped += ahead - 1
        self.frame = frame
        self.measured_at = now
        self.frames_received += 1
        self.index += 1
        if len(self.queue) == self.queue.maxlen:
            self.dropped += 1
        self.queue.append((self.index, now, frame))
        return True

    def new_session(self) -> None:
        """A new connection: the sequence starts over, nothing old is current."""
        self.session += 1
        self.frame = None
        self.frame_at = None
        self.measured_at = None
        self.queue.clear()
        self.duplicates = self.out_of_order = self.reports_skipped = self.dropped = 0
        self.mount_mode = self.mount_height_m = self.mount_angle_deg = None

    def pending(self, after_index: int) -> list:
        """New measurements after `after_index`, oldest first."""
        return [entry for entry in self.queue if entry[0] > after_index]

    def fresh(self, now: float | None = None) -> bool:
        if not self.connected or self.frame is None or self.frame_at is None:
            return False
        return (now if now is not None else time.monotonic()) - self.frame_at < FRAME_STALE_S

    def as_dict(self, now: float | None = None) -> dict:
        now = now if now is not None else time.monotonic()
        frame = self.frame
        return {
            "device_id": self.device_id,
            "address": self.address,
            "connected": self.connected,
            "error": self.error,
            "no_frame_entity": self.no_frame_entity,
            "fresh": self.fresh(now),
            "link_state": frame.state if frame else None,
            "frame_age_s": round(now - self.frame_at, 2) if self.frame_at is not None else None,
            "status": self.status,
            "firmware": self.firmware,
            "wifi_signal": self.wifi_signal,
            "ip": self.ip,
            "frames_received": self.frames_received,
            "measurement_age_s": round(now - self.measured_at, 2) if self.measured_at is not None else None,
            "duplicates": self.duplicates,
            "out_of_order": self.out_of_order,
            "reports_skipped": self.reports_skipped,
            "dropped": self.dropped,
            "mounting_entities": self.mounting_entities,
            "mounting": self.mounting(),
        }


def _default_client_factory(address: str, port: int, noise_psk: str, expected_name: str):
    # Imported here so the rest of the add-on — and its tests — do not
    # need the library loaded to start.
    from aioesphomeapi import APIClient

    return APIClient(
        address,
        port,
        None,
        noise_psk=noise_psk,
        client_info="Echolot",
        expected_name=expected_name,
    )


def explain_error(err: BaseException, address: str) -> str:
    """The connection failures somebody can do something about, named."""
    name = type(err).__name__
    if name in ("InvalidEncryptionKeyAPIError", "EncryptionHelloAPIError", "EncryptionPlaintextAPIError"):
        return (
            "Der Sensor hat den Schlüssel abgelehnt. Er läuft mit einer Firmware, "
            "die nicht aus diesem Gerät gebaut wurde — neu bauen und flashen."
        )
    if name == "RequiresEncryptionAPIError":
        return "Der Sensor spricht unverschlüsselt — er läuft nicht mit einer Echolot-Radar-Firmware."
    if name == "BadNameAPIError":
        return (
            f"Unter {address} antwortet ein anderes ESPHome-Gerät. Die Adresse "
            "stimmt nicht mehr — meist hat der Router die IP neu vergeben."
        )
    if name == "ResolveAPIError" or isinstance(err, OSError) and "resolve" in str(err).lower():
        return f"{address} lässt sich nicht auflösen. Trag beim Gerät die IP-Adresse ein."
    if isinstance(err, (asyncio.TimeoutError, TimeoutError)) or name == "TimeoutAPIError":
        return f"{address} antwortet nicht. Ist der Sensor eingeschaltet und im WLAN?"
    return f"Keine Verbindung zu {address}: {err}"


class RadarLink:
    """One node: connect, subscribe, keep the snapshot, reconnect."""

    def __init__(self, device, client_factory, on_frame, on_mounting=None) -> None:
        self.device_id = device.id
        self.name = device.config.name
        self.address = device.ota_address()
        self.key = device.api_encryption_key
        self.snapshot = LinkSnapshot(device_id=device.id, address=self.address)
        self._factory = client_factory
        self._on_frame = on_frame
        self._on_mounting = on_mounting
        self._task: asyncio.Task | None = None
        self._roles: dict[int, str] = {}
        self._client = None
        self._settle: asyncio.TimerHandle | None = None

    def identity(self) -> tuple:
        """What forces a new connection when it changes."""
        return (self.address, self.key, self.name)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._settle is not None:
            self._settle.cancel()
            self._settle = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self.snapshot.connected = False

    async def _run(self) -> None:
        failures = 0
        while True:
            self.snapshot.attempts += 1
            try:
                await self._session()
                failures = 0
                delay = BACKOFF[0]
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - every failure means "try again later"
                self.snapshot.error = explain_error(err, self.address)
                delay = BACKOFF[min(failures, len(BACKOFF) - 1)]
                failures += 1
                logger.debug("Radar %s: %s", self.name, err)
            finally:
                self.snapshot.connected = False
            await asyncio.sleep(delay)

    async def _session(self) -> None:
        client = self._factory(self.address, API_PORT, self.key, self.name)
        stopped = asyncio.Event()

        async def on_stop(_expected: bool) -> None:
            stopped.set()

        try:
            await asyncio.wait_for(client.connect(on_stop=on_stop, login=True), CONNECT_TIMEOUT)
            entities, _services = await asyncio.wait_for(client.list_entities_services(), LIST_TIMEOUT)
            self._roles = {}
            for info in entities:
                role = ENTITY_ROLES.get(str(getattr(info, "name", "")).strip().lower())
                if role:
                    self._roles[info.key] = role
            self.snapshot.no_frame_entity = "frame" not in self._roles.values()
            self.snapshot.mounting_entities = all(r in self._roles.values() for r in MOUNT_ROLES)
            self.snapshot.new_session()
            self.snapshot.connected = True
            self.snapshot.connected_since = time.time()
            self.snapshot.error = (
                "Der Sensor ist erreichbar, liefert aber keine Radar-Positionen. Auf ihm "
                "läuft keine Echolot-Radar-Firmware — neu bauen und flashen."
                if self.snapshot.no_frame_entity
                else None
            )
            logger.info("Radar %s verbunden (%s)", self.name, self.address)
            client.subscribe_states(self._on_state)
            self._client = client
            await stopped.wait()
            logger.info("Radar %s getrennt", self.name)
        finally:
            self.snapshot.connected = False
            self._client = None
            try:
                await client.disconnect(force=True)
            except Exception:  # noqa: BLE001 - already gone is fine
                pass

    def _on_state(self, state) -> None:
        role = self._roles.get(getattr(state, "key", None))
        if role is None or getattr(state, "missing_state", False):
            return
        value = getattr(state, "state", None)
        snap = self.snapshot
        if role == "frame":
            try:
                frame = parse_frame(str(value))
            except ValueError as err:
                logger.debug("Radar %s: Zeile verworfen: %s", self.name, err)
                return
            if snap.record(frame, time.monotonic()) and self._on_frame is not None:
                self._on_frame(self.device_id)
        elif role == "wifi_signal":
            snap.wifi_signal = float(value) if value == value else None  # NaN -> None
        elif role in MOUNT_ROLES:
            before = snap.mounting()
            if role == "mount_mode":
                snap.mount_mode = str(value) if value in MOUNT_MODES else None
            else:
                number = float(value) if isinstance(value, (int, float)) and value == value else None
                if role == "mount_height":
                    snap.mount_height_m = round(number, 2) if number is not None else None
                else:
                    snap.mount_angle_deg = round(number, 2) if number is not None else None
            if snap.mounting() is not None and snap.mounting() != before:
                self._mounting_changed()
        else:
            setattr(snap, role, str(value) if value is not None else None)


    def _mounting_changed(self) -> None:
        if self._on_mounting is None:
            return
        if self._settle is not None:
            self._settle.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._on_mounting(self.device_id)
            return
        self._settle = loop.call_later(MOUNTING_SETTLE_S, self._on_mounting, self.device_id)

    def write_mounting(self, mode: str, height_m: float, angle_deg: float) -> None:
        """Ask the module for this mounting. What it holds afterwards shows
        in the snapshot once it has read it back."""
        client = self._client
        if client is None or not self.snapshot.connected:
            raise MountingError("Der Sensor ist gerade nicht verbunden.")
        if not self.snapshot.mounting_entities:
            raise MountingError(
                "Die Firmware auf dem Sensor kennt die Montage des Moduls noch nicht — neu bauen und flashen."
            )
        keys = {role: key for key, role in self._roles.items()}
        snap = self.snapshot
        if mode != snap.mount_mode:
            client.select_command(keys["mount_mode"], mode)
        # Height and angle travel to the module together; the firmware
        # keeps what was asked for until the module reads it back, so the
        # second of two commands does not undo the first.
        if snap.mount_height_m is None or abs(height_m - snap.mount_height_m) >= 0.005:
            client.number_command(keys["mount_height"], float(height_m))
        if snap.mount_angle_deg is None or abs(angle_deg - snap.mount_angle_deg) >= 0.005:
            client.number_command(keys["mount_angle"], float(angle_deg))


@dataclass
class RadarLinks:
    """All links, kept in step with the device registry."""

    client_factory: object = _default_client_factory
    links: dict[str, RadarLink] = field(default_factory=dict)
    _listeners: list = field(default_factory=list)
    _mounting_listeners: list = field(default_factory=list)

    def add_listener(self, callback) -> None:
        self._listeners.append(callback)

    def add_mounting_listener(self, callback) -> None:
        """Told, with the device id, when a module's mounting as read back
        has changed — including the first time it is known."""
        self._mounting_listeners.append(callback)

    def _mounting(self, device_id: str) -> None:
        for callback in list(self._mounting_listeners):
            try:
                callback(device_id)
            except Exception:  # noqa: BLE001 - a listener must not break the link
                logger.exception("Montage-Listener fehlgeschlagen")

    def link(self, device_id: str | None) -> "RadarLink | None":
        return self.links.get(device_id) if device_id else None

    def _frame(self, device_id: str) -> None:
        for callback in list(self._listeners):
            try:
                callback(device_id)
            except Exception:  # noqa: BLE001 - a listener must not break the link
                logger.exception("Radar-Listener fehlgeschlagen")

    async def sync(self, device_list) -> None:
        """Start links for new devices, restart changed ones, stop the rest."""
        wanted = {d.id: d for d in device_list}
        for device_id in list(self.links):
            if device_id not in wanted:
                await self.links.pop(device_id).stop()
        for device_id, device in wanted.items():
            link = self.links.get(device_id)
            fresh = RadarLink(device, self.client_factory, self._frame, self._mounting)
            if link is not None and link.identity() == fresh.identity():
                continue
            if link is not None:
                await link.stop()
            self.links[device_id] = fresh
            fresh.start()

    async def stop_all(self) -> None:
        for link in list(self.links.values()):
            await link.stop()
        self.links.clear()

    def snapshot(self, device_id: str | None) -> LinkSnapshot | None:
        link = self.links.get(device_id) if device_id else None
        return link.snapshot if link else None


links = RadarLinks()
