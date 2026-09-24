"""Publishes rooms and zones to Home Assistant over MQTT discovery.

Per room one Home Assistant device with an occupancy binary_sensor and a
person-count sensor; per detection zone another pair. From there Home
Assistant's own bridges (HomeKit, Matter, Google, Alexa) take over.

Credentials come from the Supervisor (`services: mqtt:want` in
config.yaml), so nothing needs configuring when the Mosquitto add-on is
installed. Without a broker the bridge stays dormant.

Deletions are the hard part and get a queue that survives restarts: an
entity that should go gets a tombstone naming its retained topics
*before* the first attempt, and the tombstone is dropped only once the
broker acknowledged every one of them. The Wi-Fi CSI zones of earlier
versions leave through the same queue.
"""

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import paho.mqtt.client as mqtt

from app.room_engine import filter_settings

logger = logging.getLogger("echolot.mqtt")

DISCOVERY_PREFIX = "homeassistant"
BASE_TOPIC = "echolot"
AVAILABILITY_TOPIC = f"{BASE_TOPIC}/status"


def _data_dir() -> Path:
    return Path(os.environ.get("ECHOLOT_DATA_DIR", "/data"))


def _state_path() -> Path:
    return _data_dir() / "mqtt_entities.json"


def _legacy_state_path() -> Path:
    """Where 0.13/0.14 kept the announced CSI zone ids."""
    return _data_dir() / "mqtt_announced.json"


def legacy_zone_topics(zone_id: str) -> list[str]:
    """Every retained topic a CSI zone of 0.13/0.14 was published under."""
    return [
        f"{DISCOVERY_PREFIX}/binary_sensor/{BASE_TOPIC}/zone_{zone_id}/config",
        f"{BASE_TOPIC}/zone/{zone_id}/state",
        f"{BASE_TOPIC}/zone/{zone_id}/availability",
    ]


def load_state() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """(announced, tombstones), each key -> its retained topics.

    On the first start after 0.14 the old file is read once: every CSI
    zone it names — announced or already queued for deletion — becomes a
    tombstone, because the thing that published it is gone.
    """
    try:
        stored = json.loads(_state_path().read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            announced = {k: list(v) for k, v in (stored.get("announced") or {}).items()}
            tombstones = {k: list(v) for k, v in (stored.get("tombstones") or {}).items()}
            return announced, tombstones
    except (OSError, ValueError, AttributeError):
        pass
    tombstones: dict[str, list[str]] = {}
    try:
        legacy = json.loads(_legacy_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        legacy = None
    ids: set[str] = set()
    if isinstance(legacy, list):
        ids = {str(i) for i in legacy}
    elif isinstance(legacy, dict):
        ids = {str(i) for i in (legacy.get("announced") or [])} | {
            str(i) for i in (legacy.get("tombstones") or [])
        }
    for zone_id in ids:
        tombstones[f"csi_zone:{zone_id}"] = legacy_zone_topics(zone_id)
    if tombstones:
        remember_state({}, tombstones)
    return {}, tombstones


def remember_state(announced: dict, tombstones: dict) -> None:
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps({"announced": announced, "tombstones": tombstones}, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError:
        # Losing this costs a ghost entity, not a broken export.
        logger.warning("MQTT-Zustand konnte nicht gespeichert werden")


class MqttUnavailable(Exception):
    """No broker configured, or the Supervisor wouldn't tell us about one."""


def _new_client(client_id: str) -> mqtt.Client:
    """A client that works on both paho generations (ESPHome pins 1.6.1)."""
    if hasattr(mqtt, "CallbackAPIVersion"):
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id)
    return mqtt.Client(client_id=client_id)


@dataclass
class Entity:
    """One Home Assistant entity and everything retained that it owns."""

    key: str
    component: str  # binary_sensor | sensor
    discovery: dict
    state_topic: str
    availability_topic: str
    #: The rules behind the value (room_engine.filter_settings), retained
    #: next to it, so a history can be read with the rules that made it.
    attributes_topic: str = ""
    #: "occupancy" or "count".
    measure: str = "occupancy"
    #: None for the room itself.
    zone_id: str | None = None
    #: Retained topics to clear when the entity goes. The shared room
    #: availability topic belongs to the room's occupancy entity only.
    owned: list[str] = field(default_factory=list)

    @property
    def discovery_topic(self) -> str:
        return f"{DISCOVERY_PREFIX}/{self.component}/{BASE_TOPIC}/{self.key}/config"

    def topics(self) -> list[str]:
        extra = [self.attributes_topic] if self.attributes_topic else []
        return [self.discovery_topic, self.state_topic, *extra, *self.owned]


def room_availability_topic(room_id: str) -> str:
    """Whether this room currently has a measurement behind it.

    Separate from the add-on's own LWT, because the two failures are
    different: the add-on being gone, and the add-on running fine while
    one room's sensor has dropped off the network. A stale "clear" is
    worse than no answer — an automation cannot tell it from a real one.
    """
    return f"{BASE_TOPIC}/room/{room_id}/availability"


def _availability(room_id: str) -> list[dict]:
    return [
        {"topic": AVAILABILITY_TOPIC, "payload_available": "online", "payload_not_available": "offline"},
        {"topic": room_availability_topic(room_id), "payload_available": "online",
         "payload_not_available": "offline"},
    ]


def room_entities(room) -> list[Entity]:
    """Everything one room puts into Home Assistant."""
    device = {
        "identifiers": [f"echolot_room_{room.id}"],
        "name": room.name,
        "manufacturer": "Echolot",
        "model": "Radar-Raum (HLK-LD2460)",
        "suggested_area": room.name,
    }
    availability_topic = room_availability_topic(room.id)
    out = []

    def pair(key: str, name: str, zone_id: str | None, occupancy_extra: list[str]):
        occupancy_key = f"room_{key}_occupancy"
        count_key = f"room_{key}_count"
        occupancy_state = f"{BASE_TOPIC}/{occupancy_key}/state"
        count_state = f"{BASE_TOPIC}/{count_key}/state"
        occupancy_attributes = f"{BASE_TOPIC}/{occupancy_key}/attributes"
        count_attributes = f"{BASE_TOPIC}/{count_key}/attributes"
        out.append(Entity(
            key=occupancy_key,
            component="binary_sensor",
            discovery={
                "name": name,
                "unique_id": f"echolot_{occupancy_key}",
                "state_topic": occupancy_state,
                "json_attributes_topic": occupancy_attributes,
                "device_class": "occupancy",
                "payload_on": "ON",
                "payload_off": "OFF",
                "availability": _availability(room.id),
                "availability_mode": "all",
                "device": device,
            },
            state_topic=occupancy_state,
            availability_topic=availability_topic,
            attributes_topic=occupancy_attributes,
            measure="occupancy",
            zone_id=zone_id,
            owned=occupancy_extra,
        ))
        out.append(Entity(
            key=count_key,
            component="sensor",
            discovery={
                "name": f"{name} Personen" if name != "Anwesenheit" else "Personen",
                "unique_id": f"echolot_{count_key}",
                "state_topic": count_state,
                "json_attributes_topic": count_attributes,
                "state_class": "measurement",
                "icon": "mdi:account-multiple",
                "availability": _availability(room.id),
                "availability_mode": "all",
                "device": device,
            },
            state_topic=count_state,
            availability_topic=availability_topic,
            attributes_topic=count_attributes,
            measure="count",
            zone_id=zone_id,
        ))

    pair(room.id, "Anwesenheit", None, [availability_topic])
    for zone in room.zones:
        if zone.kind == "detect":
            pair(f"{room.id}_zone_{zone.id}", zone.name, zone.id, [])
    return out


async def fetch_broker_config() -> dict:
    """Ask the Supervisor for the MQTT service it manages."""
    token = os.environ.get("ECHOLOT_SUPERVISOR_TOKEN") or os.environ.get("SUPERVISOR_TOKEN")
    base = os.environ.get("ECHOLOT_SUPERVISOR_URL", "http://supervisor")
    if not token:
        raise MqttUnavailable("Kein SUPERVISOR_TOKEN vorhanden")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{base}/services/mqtt",
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as err:
        raise MqttUnavailable(f"Supervisor nicht erreichbar: {err}") from err
    if resp.status_code != 200:
        raise MqttUnavailable(f"Supervisor meldet kein MQTT (HTTP {resp.status_code})")
    data = resp.json().get("data") or {}
    if not data.get("host"):
        raise MqttUnavailable("Supervisor lieferte keine Broker-Adresse")
    return data


class Bridge:
    """One MQTT connection; publishes only what changed."""

    def __init__(self) -> None:
        self._client: mqtt.Client | None = None
        self._lock = threading.Lock()
        #: topic -> payload last handed to the broker. Cleared on every
        #: (re)connect: a broker restarted without persistence has
        #: forgotten everything, and the bridge must heal on its own.
        self._sent: dict[str, str] = {}
        #: key -> delete publishes still awaiting a PUBACK.
        self._pending_deletes: dict[str, list] = {}
        self.connected = False
        self.error: str | None = None

    async def start(self) -> None:
        config = await fetch_broker_config()
        client = _new_client("echolot")
        if config.get("username"):
            client.username_pw_set(config["username"], config.get("password") or None)
        if config.get("ssl"):
            client.tls_set()
        client.will_set(AVAILABILITY_TOPIC, "offline", retain=True)

        def on_connect(_client, _userdata, _flags, rc):
            self.connected = rc == 0
            if self.connected:
                self.error = None
                _client.publish(AVAILABILITY_TOPIC, "online", retain=True)
                with self._lock:
                    self._sent.clear()
                    self._pending_deletes.clear()
                logger.info("MQTT verbunden mit %s", config["host"])
            else:
                self.error = f"Verbindung abgelehnt (Code {rc})"

        def on_disconnect(_client, _userdata, *_args):
            self.connected = False

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        try:
            client.connect_async(config["host"], int(config.get("port") or 1883), keepalive=60)
            client.loop_start()
        except OSError as err:
            raise MqttUnavailable(f"Verbindung fehlgeschlagen: {err}") from err
        self._client = client

    def stop(self) -> None:
        if not self._client:
            return
        try:
            self._client.publish(AVAILABILITY_TOPIC, "offline", retain=True)
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.debug("MQTT shutdown was not clean", exc_info=True)
        self._client = None
        self.connected = False

    def _send(self, topic: str, payload: str, *, retain: bool = True, qos: int = 0):
        """Hand one message to the client; None when it refused it."""
        info = self._client.publish(topic, payload, retain=retain, qos=qos)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.warning("MQTT publish to %s rejected (rc=%s)", topic, info.rc)
            return None
        return info

    def _publish_changed(self, topic: str, payload: str) -> bool:
        with self._lock:
            if self._sent.get(topic) == payload:
                return True
        if self._send(topic, payload) is None:
            return False
        with self._lock:
            self._sent[topic] = payload
        return True

    @property
    def ready(self) -> bool:
        return self._client is not None and self.connected

    def announce(self, entity: Entity) -> bool:
        if not self.ready:
            return False
        return self._publish_changed(entity.discovery_topic, json.dumps(entity.discovery, sort_keys=True))

    def publish(self, topic: str, payload: str) -> bool:
        return self.ready and self._publish_changed(topic, payload)

    def forget(self, key: str, topics: list[str]) -> bool:
        """Clear retained topics at QoS 1. True once the broker acked all.

        Checked, never waited for: this runs on the engine's loop. A
        delete therefore normally takes two rounds, which is what the
        tombstone is for.
        """
        if not self.ready:
            self._pending_deletes.pop(key, None)
            return False
        pending = self._pending_deletes.get(key)
        if pending is not None:
            if not all(info.is_published() for info in pending):
                return False
            self._pending_deletes.pop(key, None)
            with self._lock:
                for topic in topics:
                    self._sent.pop(topic, None)
            return True
        sent = [self._send(topic, "", qos=1) for topic in topics]
        if any(info is None for info in sent):
            return False
        self._pending_deletes[key] = sent
        return self.forget(key, topics)

    def status(self) -> dict:
        if self.connected:
            return {"enabled": True, "connected": True}
        return {"enabled": self._client is not None, "connected": False, "error": self.error}


bridge = Bridge()


class RoomPublisher:
    """Mirrors the engine's rounds onto MQTT, with a deletion queue."""

    def __init__(self, target: Bridge | None = None) -> None:
        self.bridge = target or bridge
        self.announced, self.tombstones = load_state()

    def __call__(self, rooms, results) -> None:
        try:
            self.publish(rooms, results)
        except Exception:  # noqa: BLE001 - a bad round must not kill the loop
            logger.exception("MQTT publish cycle failed")

    def publish(self, rooms, results) -> None:
        entities = {e.key: e for room in rooms for e in room_entities(room)}
        by_room = {r["room_id"]: r for r in results}

        # A queued delete for an entity that exists again is not a delete
        # any more, and working it would take a live entity away.
        revived = set(self.tombstones) & set(entities)
        changed = bool(revived)
        for key in revived:
            del self.tombstones[key]
        # Written down before the first attempt: a delete that fails must
        # survive the failure.
        for key in set(self.announced) - set(entities) - set(self.tombstones):
            self.tombstones[key] = self.announced[key]
            changed = True
        if changed:
            self._remember()
        self._retry_deletions()

        if not self.bridge.ready:
            return
        for room in rooms:
            result = by_room.get(room.id)
            available = bool(result and result["available"])
            zone_state = {z["id"]: z for z in (result or {}).get("zones", [])}
            attributes = json.dumps(filter_settings(room), sort_keys=True)
            for entity in room_entities(room):
                if not self.bridge.announce(entity):
                    continue
                if entity.key not in self.announced or self.announced[entity.key] != entity.topics():
                    self.announced[entity.key] = entity.topics()
                    self._remember()
                self.bridge.publish(entity.attributes_topic, attributes)
                if not available:
                    continue
                source = result if entity.zone_id is None else zone_state.get(entity.zone_id)
                if source is None:
                    continue
                if entity.measure == "occupancy":
                    self.bridge.publish(entity.state_topic, "ON" if source["occupied"] else "OFF")
                else:
                    self.bridge.publish(entity.state_topic, str(int(source["count"])))
            # Availability last, so Home Assistant never shows a room as
            # available with the state of its previous life.
            self.bridge.publish(room_availability_topic(room.id), "online" if available else "offline")

    def _retry_deletions(self) -> None:
        done = [key for key, topics in sorted(self.tombstones.items()) if self.bridge.forget(key, topics)]
        if done:
            for key in done:
                self.tombstones.pop(key, None)
                self.announced.pop(key, None)
            self._remember()

    def _remember(self) -> None:
        remember_state(self.announced, self.tombstones)
