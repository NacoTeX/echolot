"""Publishes zones to Home Assistant as occupancy sensors over MQTT.

Zones otherwise live only inside this add-on: you can see them here, but
you can't use them in an automation, put them on a Home Assistant
dashboard, or export them to HomeKit/Matter. Publishing them via MQTT
discovery turns each one into a real `binary_sensor` entity, and from
there Home Assistant's own bridges handle the rest — which is a far
better answer than implementing Matter commissioning in here.

Credentials come from the Supervisor (`services: mqtt:want` in
config.yaml), so nothing needs configuring when the Mosquitto add-on is
installed. Without a broker the bridge simply stays dormant.
"""

import json
import logging
import os
import re
import threading
import unicodedata
from pathlib import Path

import httpx
import paho.mqtt.client as mqtt

logger = logging.getLogger("echolot.mqtt")

DISCOVERY_PREFIX = "homeassistant"
BASE_TOPIC = "echolot"
AVAILABILITY_TOPIC = f"{BASE_TOPIC}/status"

# Identifies this add-on as one device that owns all the zone entities.
DEVICE_INFO = {
    "identifiers": ["echolot"],
    "name": "Echolot",
    "manufacturer": "Echolot",
    "model": "Wi-Fi CSI presence",
}


#: What Home Assistant has been told about, and what still has to be
#: taken back, kept across restarts.
#:
#: The announced set alone was not a deletion queue. `forget_zone()`
#: returns immediately when the broker is disconnected, and the loop then
#: set `known = current` anyway — so on the next pass the zone to remove
#: was not in `known` any more and nobody ever retried. Its retained
#: discovery message stayed on the broker and the entity haunted Home
#: Assistant. Failed publishes were ignored the same way.
#:
#: So a zone that should go gets a tombstone written down *before* the
#: first attempt, and the tombstone is only dropped once every retained
#: topic has actually been taken. Announcements and tombstones are stored
#: together, because writing one without the other loses the other.
def _state_path() -> Path:
    return Path(os.environ.get("ECHOLOT_DATA_DIR", "/data")) / "mqtt_announced.json"


def load_state() -> tuple[set[str], set[str]]:
    """(announced, tombstones), tolerant of anything on disk."""
    try:
        stored = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set(), set()
    if isinstance(stored, list):
        # The 0.13.5 shape: a bare list of announced ids.
        return set(stored), set()
    if not isinstance(stored, dict):
        return set(), set()
    return set(stored.get("announced") or []), set(stored.get("tombstones") or [])


def remember_state(announced: set[str], tombstones: set[str]) -> None:
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps({"announced": sorted(announced), "tombstones": sorted(tombstones)}),
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError:
        # Losing this costs a ghost entity, not a broken export.
        logger.warning("MQTT-Zustand konnte nicht gespeichert werden")


def load_announced() -> set[str]:
    return load_state()[0]


class MqttUnavailable(Exception):
    """No broker configured, or the Supervisor wouldn't tell us about one."""


def _new_client(client_id: str) -> mqtt.Client:
    """Build a client that works on both paho generations.

    ESPHome pins paho-mqtt==1.6.1, so the container gets 1.x while a
    development machine may well have 2.x. Asking 2.x for the VERSION1
    callback API means one set of callback signatures serves both.
    """
    if hasattr(mqtt, "CallbackAPIVersion"):
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id)
    return mqtt.Client(client_id=client_id)


def zone_state_topic(zone_id: str) -> str:
    return f"{BASE_TOPIC}/zone/{zone_id}/state"


def zone_discovery_topic(zone_id: str) -> str:
    return f"{DISCOVERY_PREFIX}/binary_sensor/{BASE_TOPIC}/zone_{zone_id}/config"


def zone_availability_topic(zone_id: str) -> str:
    """Whether *this zone* currently has a measurement behind it.

    Separate from the add-on's own LWT, because the two failures are
    different: the add-on being gone, and the add-on running fine while
    one room's sensor has dropped off the network.
    """
    return f"{BASE_TOPIC}/zone/{zone_id}/availability"


def slugify(name: str) -> str:
    """Zone name -> safe entity id suffix.

    German zone names are the normal case here ("Küche", "Büro"), and an
    umlaut left in an object_id makes the resulting entity id whatever
    Home Assistant decides to do with it. Transliterate first, then keep
    only characters that are valid in an entity id.
    """
    lowered = name.lower()
    for source, target in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        lowered = lowered.replace(source, target)
    slug = re.sub(r"[^a-z0-9]+", "_", unicodedata.normalize("NFKD", lowered))
    slug = slug.encode("ascii", "ignore").decode().strip("_")
    return slug or "zone"


def zone_discovery_payload(zone_id: str, zone_name: str) -> dict:
    """The config message that makes Home Assistant create the entity."""
    return {
        "name": zone_name,
        "unique_id": f"echolot_zone_{zone_id}",
        "object_id": f"echolot_{slugify(zone_name)}",
        "state_topic": zone_state_topic(zone_id),
        "device_class": "occupancy",
        "payload_on": "ON",
        "payload_off": "OFF",
        # Two availability sources, both of which must say online.
        #
        # With only the add-on's LWT, a zone whose devices had fallen off
        # the network published nothing at all — so Home Assistant kept
        # the last ON/OFF it had seen, indefinitely, while the add-on
        # itself stayed cheerfully "online". A stale "clear" is worse than
        # no answer: an automation cannot tell it from a real one.
        "availability": [
            {
                "topic": AVAILABILITY_TOPIC,
                "payload_available": "online",
                "payload_not_available": "offline",
            },
            {
                "topic": zone_availability_topic(zone_id),
                "payload_available": "online",
                "payload_not_available": "offline",
            },
        ],
        "availability_mode": "all",
        "device": DEVICE_INFO,
    }


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


class ZoneBridge:
    """Keeps one MQTT connection and mirrors zone state onto it."""

    def __init__(self) -> None:
        self._client: mqtt.Client | None = None
        self._lock = threading.Lock()
        #: zone id -> the name last announced under it. A set was not
        #: enough: renaming a zone changes the discovery payload, and
        #: without noticing, Home Assistant kept the old name forever.
        self._announced: dict[str, str] = {}
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

        # VERSION1 signatures: (client, userdata, flags, rc).
        def on_connect(_client, _userdata, _flags, rc):
            self.connected = rc == 0
            if self.connected:
                self.error = None
                _client.publish(AVAILABILITY_TOPIC, "online", retain=True)
                # Forget what we believe the broker knows. Discovery is
                # published retained, so it normally survives — but a broker
                # that was restarted without persistence, or had its topics
                # cleared, has forgotten every zone while this set still
                # says they were announced. Nothing would then re-announce
                # them and the entities would stay gone until the add-on
                # restarted. Clearing here costs one repeat publish per
                # zone on reconnect and makes the bridge self-healing.
                with self._lock:
                    self._announced.clear()
                logger.info("MQTT connected to %s", config["host"])
            else:
                self.error = f"Verbindung abgelehnt (Code {rc})"

        def on_disconnect(_client, _userdata, *_args):
            self.connected = False

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect

        try:
            client.connect_async(config["host"], int(config.get("port") or 1883), keepalive=60)
            client.loop_start()  # reconnects on its own thread
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

    def _publish(self, topic: str, payload: str, *, retain: bool = True) -> bool:
        """Publish and say whether the broker accepted it.

        paho returns an MQTTMessageInfo whose rc tells you the message was
        dropped — queue full, or not connected after all. Discarding that
        turns a silent failure into a zone that quietly stops updating in
        Home Assistant, with nothing anywhere saying why.
        """
        info = self._client.publish(topic, payload, retain=retain)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.warning("MQTT publish to %s rejected (rc=%s)", topic, info.rc)
            return False
        return True

    def announced_ids(self) -> set[str]:
        with self._lock:
            return set(self._announced)

    def publish_zone(self, zone_id: str, zone_name: str, occupied: bool, available: bool) -> None:
        if not self._client or not self.connected:
            return

        with self._lock:
            # Renaming counts as new: the name and object_id live in the
            # discovery payload, so an unchanged one leaves Home Assistant
            # showing the old name for the rest of time.
            needs_discovery = self._announced.get(zone_id) != zone_name

        if needs_discovery:
            # Only remember the announcement once the broker has taken it;
            # a rejected discovery message that we recorded as sent would
            # never be retried.
            if not self._publish(
                zone_discovery_topic(zone_id),
                json.dumps(zone_discovery_payload(zone_id, zone_name)),
            ):
                return
            with self._lock:
                self._announced[zone_id] = zone_name

        # Say whether this zone has a measurement at all, then — only if
        # it does — what that measurement is. An unavailable zone stops
        # publishing state on purpose: Home Assistant marks the entity
        # unavailable from the topic above rather than holding the last
        # value as though it were current.
        self._publish(zone_availability_topic(zone_id), "online" if available else "offline")
        if available:
            self._publish(zone_state_topic(zone_id), "ON" if occupied else "OFF")

    def forget_zone(self, zone_id: str) -> bool:
        """Clear every retained topic. True only when all of them took.

        The caller keeps a tombstone until this says True: a disconnected
        broker, a full queue or a single rejected publish all leave the
        entity in Home Assistant, and reporting success would forget the
        zone before it was gone.
        """
        if not self._client or not self.connected:
            return False
        taken = all(
            [
                self._publish(zone_discovery_topic(zone_id), ""),
                self._publish(zone_state_topic(zone_id), ""),
                self._publish(zone_availability_topic(zone_id), ""),
            ]
        )
        if taken:
            with self._lock:
                self._announced.pop(zone_id, None)
        return taken

    def status(self) -> dict:
        if self.connected:
            return {"enabled": True, "connected": True}
        return {"enabled": self._client is not None, "connected": False, "error": self.error}


bridge = ZoneBridge()


class ZonePublisher:
    """Mirrors the evaluator's rounds onto MQTT, with a deletion queue.

    It does no evaluation of its own. The export used to run its own loop
    with its own clock and its own calls into Home Assistant, which is
    how a reading arriving three times a second became three rounds of
    requests; now it publishes what the one evaluator worked out, when it
    works it out.
    """

    def __init__(self, on_zone_gone=None) -> None:
        self.announced, self.tombstones = load_state()
        self._on_zone_gone = on_zone_gone

    def __call__(self, states: list[tuple]) -> None:
        try:
            self.publish(states)
        except Exception:  # noqa: BLE001 - a bad round must not kill the loop
            logger.exception("MQTT publish cycle failed")

    def publish(self, states: list[tuple]) -> None:
        desired = {zone.id for zone, _ in states}

        # A queued delete for a zone that exists again is not a delete any
        # more, and working it would take a live entity away. Dropped
        # before the queue is worked, not after.
        revived = self.tombstones & desired
        if revived:
            self.tombstones -= revived
            self._remember()

        # Anything announced that should not exist gets a tombstone, and
        # the tombstone is written down before the first attempt to
        # remove it — a delete that fails must survive the failure.
        vanished = (self.announced | bridge.announced_ids()) - desired - self.tombstones
        if vanished:
            self.tombstones |= vanished
            self._remember()

        self._retry_deletions()

        for zone, state in states:
            bridge.publish_zone(
                zone.id, zone.name, bool(state.get("occupied")), bool(state.get("available"))
            )
            if zone.id in bridge.announced_ids() and zone.id not in self.announced:
                self.announced.add(zone.id)
                self._remember()

    def _retry_deletions(self) -> None:
        """Work the queue. Whatever does not take stays queued."""
        done = set()
        for zone_id in sorted(self.tombstones):
            if bridge.forget_zone(zone_id):
                done.add(zone_id)
                if self._on_zone_gone is not None:
                    self._on_zone_gone(zone_id)
        if done:
            self.tombstones -= done
            self.announced -= done
            self._remember()

    def _remember(self) -> None:
        remember_state(self.announced, self.tombstones)


def attach(evaluator, on_zone_gone=None) -> ZonePublisher:
    """Publish on every evaluator round, for the life of the process."""
    publisher = ZonePublisher(on_zone_gone)
    evaluator.add_listener(publisher)
    return publisher
