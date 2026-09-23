"""Device registry: persisted metadata + build state for ESPectre devices.

Backed by a single JSON file under the add-on's persistent /data directory
(a plain file is plenty for the handful of devices a home setup has, and
keeps this phase free of a database dependency).
"""

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import unicodedata
import uuid
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.board_registry import Board, get_board
from app.firmware import capabilities_for

DATA_DIR = Path(os.environ.get("ECHOLOT_DATA_DIR", "/data"))
DEVICES_DIR = DATA_DIR / "devices"
INDEX_PATH = DATA_DIR / "devices.json"

_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")

#: The three answers to "which radio". Written the way ESPHome spells
#: them, bar case: `wifi.band_mode` takes 2.4GHZ, 5GHZ or AUTO, and the
#: template upper-cases on the way out, so there is one vocabulary here
#: rather than a translation table nobody remembers to update.
BAND_24 = "2.4GHz"
BAND_5 = "5GHz"
BAND_AUTO = "auto"

#: Which radio topology a device measures in.
#:
#: `router` is what every image Echolot has ever built does: CSI taken
#: from traffic between the access point and this device. `peer_link` is
#: the directed A→B link between two sensors — the topology TOMMY
#: describes — and no firmware Echolot ships provides it. It is named
#: here so that a baseline can record which of the two it was learned
#: under, and gated on a capability the firmware has to report, so it
#: cannot be selected by anybody who merely wants it.
MODE_ROUTER = "router"
MODE_PEER_LINK = "peer_link"

#: What a device senses with. `espectre` is Wi-Fi CSI through ESPectre,
#: which is every device stored before 0.14.0. `ld2460` is an HLK-LD2460
#: radar module on a UART, read by Echolot's own ESPHome component
#: (app/esphome_components/echolot_ld2460).
SENSOR_ESPECTRE = "espectre"
SENSOR_LD2460 = "ld2460"

#: Config fields that must never reach a manifest, a log or a hash.
_SECRET_CONFIG_FIELDS = ("wifi_password",)

#: Firmware fields nobody may change on an existing device.
#:
#: `name` is what Home Assistant builds every entity id from, and what
#: the OTA hostname resolves to; renaming here would orphan the entities
#: the zones point at without saying so. `board` is another chip, so
#: another image — and a baseline learned on the old one describes
#: different hardware. Both are reasons to make a new device, which keeps
#: the old one's recordings until somebody deletes it deliberately.
#:
#: `sensor` for the same reason as `board`, and one more: everything a
#: CSI device has learned — calibration, presence profile, entity
#: mapping — describes a Wi-Fi measurement, and none of it survives
#: becoming a radar. Converting a device while keeping its identity is a
#: separate, deliberate step; it is not a field edit.
IMMUTABLE_CONFIG_FIELDS = ("name", "board", "sensor")


_lock = threading.Lock()


class BuildStatus(StrEnum):
    IDLE = "idle"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


class DeviceCreate(BaseModel):
    name: str = Field(..., description="ESPHome node name (lowercase, digits, hyphens)")
    friendly_name: str | None = None
    board: str
    #: See SENSOR_ESPECTRE / SENSOR_LD2460. The default stays `espectre`
    #: while both exist, so an API client written for an earlier version
    #: keeps creating what it always created.
    sensor: Literal["espectre", "ld2460"] = SENSOR_ESPECTRE
    wifi_ssid: str = Field(..., min_length=1, max_length=32)
    wifi_password: str = Field(default="", max_length=64)
    wifi_bssid: str | None = None
    #: Which radio the device associates on — and therefore which one it
    #: measures on. Only the ESP32-C5 has the choice; see
    #: `board_registry.Board.dual_band`.
    #:
    #: 2.4 GHz by default, and not merely because it is the common
    #: denominator: ESPectre's own SETUP.md says of the other one
    #: "Detection quality on 5 GHz is not characterized yet". A default
    #: nobody chose should be the band somebody has measured.
    #:
    #: `auto` leaves the band to the router. It is offered because it is
    #: what a C5 built before 0.13.8 is running, not because it is a good
    #: idea: under `auto` a recording cannot say which radio it was made
    #: on, so it is a poor thing to learn a baseline from.
    wifi_band: Literal["2.4GHz", "5GHz", "auto"] = "2.4GHz"
    #: Which radio topology this device measures in — see MODE_ROUTER.
    #: `router` by default, which is also what every device stored before
    #: 0.13.8 is doing, so no migration is needed to say so.
    sensing_mode: Literal["router", "peer_link"] = "router"
    # Field names and ranges follow ESPectre's own schema
    # (src/cpp/runtime/runtime_sensing_schema.h), so a value that
    # validates here validates there.
    detection_algorithm: Literal["lightweight", "high_accuracy"] = "lightweight"
    csi_target_pps: int = Field(default=100, ge=1, le=500)
    csi_traffic_mode: Literal["internal", "external"] = "internal"
    traffic_generator_mode: Literal["ping", "dns", "dns_tcp"] = "ping"
    evaluation_interval_ms: int = Field(default=250, ge=10, le=10000)
    #: ESPectre's own HTTP/SSE surface on port 62587. Leaving it on gives
    #: the reachability check a second thing to probe.
    direct_api: bool = True
    #: Encrypt the Home Assistant API connection. Off by default only
    #: because it currently makes the firmware fail to compile — see the
    #: comment in templates/espectre.yaml.j2 — not because it is optional
    #: in principle. The key is generated and kept either way, so turning
    #: this on later needs no new key and no change in Home Assistant
    #: beyond entering it.
    api_encryption: bool = False
    #: Serve a status page on the device at http://<ip>/. Costs flash and a
    #: little RAM, and is the only way to check a device from a browser
    #: that has no Web Serial — everything on iPadOS, for instance.
    web_server: bool = True
    #: Signal strength, uptime, chip temperature, IP address, restart
    #: buttons. Signal strength in particular decides whether a spot is
    #: viable for CSI sensing at all.
    diagnostics: bool = True
    log_level: Literal["NONE", "ERROR", "WARN", "INFO", "DEBUG", "VERBOSE"] = "INFO"

    # --- radar (sensor == "ld2460") -----------------------------------
    # Inert for ESPectre devices, and left at None there.

    #: ESP pins wired to the module's Rx2 (pin 8) and Tx2 (pin 7). Left
    #: empty, the board's suggestion applies — `Board.radar_uart_pins`.
    radar_tx_pin: int | None = Field(default=None, ge=0, le=56)
    radar_rx_pin: int | None = Field(default=None, ge=0, le=56)
    #: A GPIO held low from boot, for boards that pick their antenna with
    #: a pin. On the Waveshare ESP32-C5-Zero that is GPIO26, and low means
    #: the on-board antenna. None on every board that has no such switch.
    antenna_select_pin: int | None = Field(default=None, ge=0, le=56)
    #: Whether a module that answers but reports nothing counts as an
    #: empty room. Off until somebody has watched a module in an empty
    #: room and knows — see `classify()` in ld2460_protocol.h. Changing it
    #: changes what "0 people" means, so it counts as a new measurement
    #: definition like the Wi-Fi band does for CSI.
    radar_quiet_means_empty: bool = False

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(
                "name must be lowercase alphanumeric with hyphens, "
                "start/end with a letter or digit (max 32 chars)"
            )
        return v

    @field_validator("board")
    @classmethod
    def _validate_board(cls, v: str) -> str:
        get_board(v)  # raises ValueError if unknown
        return v

    @field_validator("wifi_password")
    @classmethod
    def _validate_password(cls, v: str) -> str:
        if v and len(v) < 8:
            raise ValueError("wifi_password must be empty (open network) or at least 8 characters")
        return v

    @model_validator(mode="after")
    def _validate_sensing_mode(self) -> "DeviceCreate":
        """A topology no firmware provides is not an option.

        Checked against the commit the *next build* would use, because at
        create time there is no image yet — `available_sensing_modes`
        asks a built device's own manifest instead. Both read the same
        capability names, so the gate opens in one place when firmware
        ever reports one, and nowhere before.
        """
        if self.sensing_mode == MODE_PEER_LINK and not capabilities_for(None)[
            "supports_peer_rx"
        ]:
            raise ValueError(
                "sensing_mode 'peer_link' verlangt einen gerichteten Funklink "
                "zwischen zwei Sensoren. Die gepinnte ESPectre-Firmware meldet "
                "supports_peer_rx=False — sie misst ausschließlich den Verkehr "
                "zwischen Access Point und Gerät. Solange keine Firmware etwas "
                "anderes meldet, gibt es diesen Modus nicht."
            )
        return self

    @model_validator(mode="after")
    def _validate_band(self) -> "DeviceCreate":
        """A band the chip has no radio for is a mistake, not a preference.

        Caught here rather than at render time because ESPHome would
        reject the generated YAML anyway — `wifi.band_mode` is declared
        `only_on_variant(supported=[VARIANT_ESP32C5])` — and a build that
        fails after the toolchain has started is a much worse way to
        learn it.
        """
        if self.wifi_band != BAND_24 and not get_board(self.board).dual_band:
            raise ValueError(
                f"wifi_band '{self.wifi_band}' braucht zwei Funkbänder — "
                "davon hat nur der ESP32-C5 welche. Jedes andere Board misst "
                "auf 2,4 GHz."
            )
        return self

    @model_validator(mode="after")
    def _validate_radar(self) -> "DeviceCreate":
        """Fill in and check the radar wiring; leave it alone otherwise.

        Filled in here rather than at render time so the stored config
        says which pins the firmware uses. A default that only exists in
        the template is a default nobody can see, and one that changes
        with a later board table would silently rewire a device on its
        next build.
        """
        if self.sensor != SENSOR_LD2460:
            return self
        # Two ESPectre switches whose stored value would otherwise lie
        # about a radar node. There is no ESPectre HTTP/SSE surface to
        # probe or stream from, and the API is always encrypted — see
        # templates/ld2460.yaml.j2. Set rather than refused, because both
        # default to the ESPectre answer and a client that never heard of
        # radar should still be able to create one.
        self.direct_api = False
        self.api_encryption = True
        board = get_board(self.board)
        if self.radar_tx_pin is None or self.radar_rx_pin is None:
            if board.radar_uart_pins is None:
                raise ValueError(
                    f"Für {board.label} gibt es keinen Vorschlag für die Radar-Pins. "
                    "Bitte radar_tx_pin und radar_rx_pin angeben."
                )
            default_tx, default_rx = board.radar_uart_pins
            if self.radar_tx_pin is None:
                self.radar_tx_pin = default_tx
            if self.radar_rx_pin is None:
                self.radar_rx_pin = default_rx
        if self.radar_tx_pin == self.radar_rx_pin:
            raise ValueError("TX und RX des Radars brauchen zwei verschiedene Pins")
        if self.antenna_select_pin in (self.radar_tx_pin, self.radar_rx_pin):
            raise ValueError(
                f"GPIO{self.antenna_select_pin} ist schon eine Radar-Leitung und kann "
                "nicht zugleich die Antenne umschalten"
            )
        return self


#: Fields added after config hashes started being recorded, with the
#: value that means "this field did not exist yet". A field holding that
#: value is left out of the hash.
#:
#: Without this, adding a field changes the hash of every stored device —
#: its dump grows a key — and every device built by the previous version
#: reads as `firmware_behind_config` the moment the add-on updates,
#: although nothing about what it would build has changed. A field that
#: moves off its "absent" value is hashed like any other, so a real change
#: is still a change.
_FINGERPRINT_ABSENT_VALUES = {
    "sensor": SENSOR_ESPECTRE,
    "radar_tx_pin": None,
    "radar_rx_pin": None,
    "antenna_select_pin": None,
    "radar_quiet_means_empty": False,
}


def config_fingerprint(device) -> str:
    """A short hash of what would be built, with the secrets left out."""
    payload = device.config.model_dump()
    for field in _SECRET_CONFIG_FIELDS:
        payload.pop(field, None)
    for field, absent in _FINGERPRINT_ABSENT_VALUES.items():
        if field in payload and payload[field] == absent:
            del payload[field]
    encoded = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def get_board_safely(key) -> Board:
    """The board, or a stand-in — for code that must not raise.

    The migration runs before validation, on whatever is in the file. A
    record naming a board this version no longer knows must still load;
    it fails later, at validation, with a message about the board rather
    than a KeyError out of a migration step.
    """
    try:
        return get_board(str(key))
    except ValueError:
        return Board(key=str(key), label=str(key), variant=None, chip_family="")


def effective_band(config: DeviceCreate) -> str:
    """Which band firmware built from this config actually measures on.

    Not the same question as `config.wifi_band`: on a single-band chip the
    stored value is inert, because ESPectre's `_runtime_wifi_band_policy`
    returns a flat "2g" for every variant but the C5 and no `band_mode:`
    is rendered at all. Asking the config directly would let a stray
    stored value claim a radio the board does not have.
    """
    return config.wifi_band if get_board(config.board).dual_band else BAND_24


#: What 802.11 allows for an SSID, and what ESPHome enforces.
_SSID_MAX = 32
_FALLBACK_SUFFIX = " Fallback"


def fallback_ssid(device_name: str) -> str:
    """The fallback AP's SSID: the device name, shortened to fit.

    Device names may be 32 characters, and "<name> Fallback" is then 41 —
    which ESPHome refuses ("SSID can't be longer than 32 characters"), so
    every device with a name longer than 23 characters failed to build.
    The name is cut rather than the suffix, because the suffix is what
    tells somebody scanning for networks what this one is.
    """
    return device_name[: _SSID_MAX - len(_FALLBACK_SUFFIX)] + _FALLBACK_SUFFIX


def is_radar(device_or_config) -> bool:
    """Whether this device senses with an LD2460 rather than CSI.

    Takes a Device or its config, because both halves of the code base
    ask: the builder has a config in hand, everything that looks at a
    device's live state has the device.
    """
    config = getattr(device_or_config, "config", device_or_config)
    return getattr(config, "sensor", SENSOR_ESPECTRE) == SENSOR_LD2460


def available_sensing_modes(device) -> list[str]:
    """The topologies this device's own image can actually measure in.

    Read from its build manifest, because that is what is on the chip;
    an unbuilt device is judged by the pinned commit, which is what its
    first build would give it. Returns a list rather than a set so the
    order is stable wherever it is shown.
    """
    capabilities = capabilities_for(getattr(device, "build_manifest", None))
    modes = [MODE_ROUTER] if capabilities["supports_router"] else []
    if capabilities["supports_peer_rx"]:
        modes.append(MODE_PEER_LINK)
    return modes


def _entity_slug(text: str) -> str:
    """Home Assistant's entity-id slug, near enough for a first guess."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    stripped = decomposed.encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", stripped).strip("_")


def default_entity_ids(device_name: str, friendly_name: str | None = None) -> dict[str, str]:
    """Opening guess at the entity ids Home Assistant will create.

    Home Assistant builds an ESPHome entity id from the *device* name plus
    the entity name, and the device name is the config's `friendly_name`
    when there is one — not the node name. Guessing from the node name
    left every device with a friendly name looking permanently
    unavailable.

    This is only a starting value written at create time, before the
    device has even been flashed. Once it is running,
    app/entity_resolver.py replaces these with what Home Assistant
    actually named the entities.
    """
    slug = _entity_slug(friendly_name or device_name)
    return {
        "entity_motion": f"binary_sensor.{slug}_motion_detected",
        "entity_movement_score": f"sensor.{slug}_movement_score",
        "entity_threshold": f"number.{slug}_threshold",
        # A button since ESPectre's restructure, not a switch.
        "entity_calibrate": f"button.{slug}_recalibrate",
    }


class DeviceUpdate(BaseModel):
    """Partial update for fields that don't require a rebuild/reflash."""

    friendly_name: str | None = None
    entity_motion: str | None = None
    entity_movement_score: str | None = None
    entity_threshold: str | None = None
    entity_calibrate: str | None = None
    #: Where to reach the device on the network, for OTA updates and the
    #: reachability check. Learned from Home Assistant where possible,
    #: overridable because mDNS does not survive every network.
    address: str | None = None


def new_api_key() -> str:
    """A fresh 32-byte key, base64-encoded the way ESPHome expects it."""
    return base64.b64encode(secrets.token_bytes(32)).decode()


def new_ota_password() -> str:
    return secrets.token_hex(16)


def new_fallback_password() -> str:
    """The access point a device opens when it cannot reach the Wi-Fi.

    That AP used to have no password at all. It carries a captive portal
    that takes Wi-Fi credentials, and it comes up exactly when something
    is already wrong — a router swap, a changed passphrase — so it is
    most likely to be open at the least convenient moment. ESPHome wants
    at least eight characters; sixteen hex is comfortably past that and
    still typable off a screen.
    """
    return secrets.token_hex(8)


class Device(BaseModel):
    id: str
    created_at: float
    updated_at: float
    config: DeviceCreate
    status: BuildStatus = BuildStatus.IDLE
    build_log: str = ""
    build_error: str | None = None
    firmware_bin: str | None = None  # path relative to the device dir, once built
    chip_family: str | None = None
    # Best-guess Home Assistant entity ids for ESPectre's exposed entities
    # (see default_entity_ids); user-editable in case the guess is wrong.
    entity_motion: str | None = None
    entity_movement_score: str | None = None
    entity_threshold: str | None = None
    entity_calibrate: str | None = None
    #: What this room does when empty, learned from a calibration recording
    #: (see app/presence_rate.py). Presence from the crossing rate is
    #: meaningless without it, so a device without one simply does not
    #: contribute that signal.
    presence_profile: dict | None = None
    #: Guards the fallback access point. Generated per device, and shown
    #: alongside the other credentials so it can be typed in when the
    #: portal actually comes up.
    fallback_password: str = Field(default_factory=new_fallback_password)
    #: What the last successful build was made of: the ESPectre commit,
    #: the ESPHome version, the board, a hash of the configuration with
    #: the secrets left out, and the firmware's own checksum. Written so
    #: "which firmware is on this device" has an answer later.
    build_manifest: dict | None = None
    #: Baked into the firmware, and needed again when Home Assistant adopts
    #: the device — so it has to be readable here, not just generated.
    api_encryption_key: str = Field(default_factory=new_api_key)
    ota_password: str = Field(default_factory=new_ota_password)
    #: Tracked separately from the build: a failed OTA must not make a
    #: perfectly good firmware image look unbuilt.
    ota_status: BuildStatus = BuildStatus.IDLE
    ota_log: str = ""
    ota_error: str | None = None
    ota_last_success: float | None = None
    #: Hostname or IP for OTA and reachability checks. Empty means "use
    #: <node name>.local", which is right whenever mDNS works.
    address: str | None = None

    def firmware_size(self) -> int | None:
        """Bytes of the built image, or None when there is none.

        Shown next to the download link so a slow flash can be told apart
        from a large one — the built-in flasher gives no size feedback
        while it downloads.
        """
        if not self.firmware_bin:
            return None
        path = device_dir(self.id) / self.firmware_bin
        try:
            return path.stat().st_size
        except OSError:
            return None

    def ota_address(self) -> str:
        return self.address or f"{self.config.name}.local"

    #: Never leave the process except through credentials(), which is
    #: reached one device at a time and on purpose.
    _SECRET_FIELDS = ("api_encryption_key", "ota_password")

    @property
    def firmware_behind_config(self) -> bool:
        """Whether the flashed image was built from a different config.

        Only answerable for a device that has been built: an unbuilt one
        has nothing for the config to be ahead of, and calling it stale
        would put a warning on every new device. A manifest from before
        the hash existed says nothing either, and a warning nobody can
        clear is worse than none.

        The comparison is the fingerprint, so it ignores exactly what the
        fingerprint ignores: the Wi-Fi password, and nothing else. Even
        the display name counts — the template renders `friendly_name:`
        into the ESPHome config, and Home Assistant derives the device
        name, and with it every entity id, from what the firmware
        reports. A rename that is never flashed is a rename that never
        happens.
        """
        recorded = (self.build_manifest or {}).get("config_hash")
        return bool(recorded) and recorded != config_fingerprint(self)

    def public(self) -> dict:
        """Serialize without anything secret.

        The API key is only needed once, when Home Assistant adopts the
        device — so it does not belong in the payload that draws the
        device list. Handing every key out on every page load makes the
        blast radius of one leaked response the whole fleet instead of
        one device.
        """
        data = self.model_dump()
        data["config"]["wifi_password"] = "********" if self.config.wifi_password else ""
        for field in self._SECRET_FIELDS:
            data.pop(field, None)
        # Enough for the UI to know there is something to show, without
        # showing it.
        data["has_credentials"] = bool(self.api_encryption_key)
        data["firmware_behind_config"] = self.firmware_behind_config
        data["firmware_size"] = self.firmware_size()
        # Computed here so the card shows the network the firmware really
        # opens, not a longer name nobody can find.
        data["fallback_ssid"] = fallback_ssid(self.config.name)
        return data

    def credentials(self) -> dict:
        """The secrets, for the one endpoint whose job is to reveal them."""
        return {
            "api_encryption_key": self.api_encryption_key,
            "ota_password": self.ota_password,
            "fallback_password": self.fallback_password,
        }

    def apply_update(self, patch: DeviceUpdate) -> None:
        for field, value in patch.model_dump(exclude_unset=True).items():
            if field == "friendly_name":
                self.config.friendly_name = value
            else:
                setattr(self, field, value)


def reconfigure(device_id: str, changes: dict) -> "Device | None":
    """Change firmware options on an existing device, keeping the device.

    Deleting and recreating used to be the only way to change one number,
    and it cost the device's id, its Home Assistant entity ids, its
    learned profile, its recordings and its credentials.

    The patch is merged into the stored config and the result goes
    through `DeviceCreate` — the whole model, not a second copy of some
    of its rules. So a band the board has no radio for, a password too
    short for WPA and a packet rate outside ESPectre's range are all
    refused here for the same reason and with the same message as at
    creation.

    Returns None when the device is gone. Raises ValueError for anything
    the config would not accept; the stored device is left alone in that
    case, because the merge happens on a copy.
    """
    unknown = set(changes) - set(DeviceCreate.model_fields)
    if unknown:
        raise ValueError(f"Unbekannte Konfigurationsfelder: {', '.join(sorted(unknown))}")

    frozen = set(changes) & set(IMMUTABLE_CONFIG_FIELDS)
    if frozen:
        raise ValueError(
            f"{', '.join(sorted(frozen))} lässt sich an einem bestehenden Gerät "
            "nicht ändern — dafür ist ein neues Gerät der ehrliche Weg. Der "
            "Knotenname trägt jede Entity-ID in Home Assistant und den "
            "OTA-Hostnamen, und ein anderes Board ist andere Hardware, über die "
            "ein gelerntes Profil nichts sagt."
        )

    with _lock:
        index = _read_index()
        stored = index.get(device_id)
        if stored is None:
            return None
        record = dict(stored)
        raw = dict(record.get("config") or {})
        _migrate_config(raw)
        # Validated before anything is written, so a refusal leaves the
        # stored device exactly as it was.
        record["config"] = DeviceCreate.model_validate({**raw, **changes}).model_dump()
        record["updated_at"] = time.time()
        device = Device.model_validate(record)
        index[device_id] = device.model_dump()
        _write_index(index)
        return device


def _read_index() -> dict[str, dict]:
    if not INDEX_PATH.exists():
        return {}
    return json.loads(INDEX_PATH.read_text(encoding="utf-8"))


def _write_index(index: dict[str, dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, indent=2), encoding="utf-8")
    tmp.replace(INDEX_PATH)


#: Old field -> new field, for values that merely moved.
_RENAMED_CONFIG_FIELDS = {"traffic_generator_rate": "csi_target_pps"}

#: Old value -> new value, for ESPectre's renamed detection profiles.
_RENAMED_ALGORITHMS = {"mvs": "lightweight", "ml": "high_accuracy"}


def _migrate_config(config: dict) -> bool:
    """Bring one stored device config onto ESPectre's current schema.

    ESPectre restructured in September 2026 and renamed most of these.
    Without translation Pydantic refuses to load the device at all, which
    would lose every device someone had already set up.
    """
    changed = False

    # Before 0.14.0 every device was a CSI device and said nothing about
    # it. Written down explicitly, so that when the default changes the
    # stored devices do not change with it.
    if "sensor" not in config:
        config["sensor"] = SENSOR_ESPECTRE
        changed = True

    for old_field, new_field in _RENAMED_CONFIG_FIELDS.items():
        if old_field in config:
            config.setdefault(new_field, config.pop(old_field))
            changed = True

    algorithm = config.get("detection_algorithm")
    if algorithm in _RENAMED_ALGORITHMS:
        config["detection_algorithm"] = _RENAMED_ALGORITHMS[algorithm]
        changed = True

    # No equivalent upstream: segmentation is a window in milliseconds now,
    # not a threshold, so the old value cannot be carried over meaningfully.
    if config.pop("segmentation_threshold", None) is not None:
        changed = True

    # A dual-band device stored before 0.13.8 was built without a
    # `band_mode:`, and ESPHome's default for the C5 is AUTO — so that is
    # what is on the chip. Letting the new field's default apply would
    # write "2.4GHz" into a config describing an image that is running on
    # whatever the router handed it, and would silently reinterpret the
    # baseline learned under it. Record what was flashed; changing it is
    # a decision with a rebuild attached, which is the user's to make.
    if "wifi_band" not in config and get_board_safely(config.get("board")).dual_band:
        config["wifi_band"] = BAND_AUTO
        changed = True

    # The old field allowed 0-1000; ESPectre's range is 1-500. Clamp rather
    # than reject, so an out-of-range device still loads.
    pps = config.get("csi_target_pps")
    if isinstance(pps, int) and not (1 <= pps <= 500):
        config["csi_target_pps"] = min(500, max(1, pps))
        changed = True

    return changed


def _migrate(index: dict[str, dict]) -> bool:
    """Fill in fields added after a device was first written.

    `api_encryption_key` and `ota_password` have generating defaults, so a
    device stored before they existed would otherwise get a *different*
    value on every load — and the key shown in the UI would not be the one
    baked into the firmware. Generate once, here, and persist.

    Returns whether anything changed.
    """
    changed = False
    for raw in index.values():
        for field, factory in (
            ("api_encryption_key", new_api_key),
            ("ota_password", new_ota_password),
        ):
            if not raw.get(field):
                raw[field] = factory()
                changed = True
        if isinstance(raw.get("config"), dict) and _migrate_config(raw["config"]):
            changed = True
    return changed


def list_devices() -> list[Device]:
    with _lock:
        index = _read_index()
        if _migrate(index):
            _write_index(index)
        return [Device.model_validate(v) for v in index.values()]


def get_device(device_id: str) -> Device | None:
    with _lock:
        index = _read_index()
        if device_id not in index:
            return None
        if _migrate(index):
            _write_index(index)
        return Device.model_validate(index[device_id])


def create_device(payload: DeviceCreate) -> Device:
    now = time.time()
    device = Device(
        id=str(uuid.uuid4()),
        created_at=now,
        updated_at=now,
        config=payload,
        # A radar node has none of ESPectre's entities. Guessing their ids
        # anyway would have every CSI code path read entities that will
        # never exist and report the device as unavailable.
        **({} if is_radar(payload) else default_entity_ids(payload.name, payload.friendly_name)),
    )
    with _lock:
        index = _read_index()
        index[device.id] = device.model_dump()
        _write_index(index)
    device_dir(device.id).mkdir(parents=True, exist_ok=True)
    return device


def save_device(device: Device) -> None:
    device.updated_at = time.time()
    with _lock:
        index = _read_index()
        index[device.id] = device.model_dump()
        _write_index(index)


def update_device(device_id: str, **fields) -> Device | None:
    """Write only the named fields, under the registry lock.

    `save_device` replaces the whole record, which is right for a route
    that has just read it and wrong for a job that has been holding a
    Device object for minutes. A build or an OTA run does exactly that:
    anything changed meanwhile — corrected entity ids, an address, a
    freshly applied presence profile — was silently replaced by the copy
    the job started with, and a device deleted during a build came back
    when the build finished.

    Returns None when the device is gone, so a job can tell that its
    result has nowhere to go instead of re-creating it.
    """
    # Pydantic drops unknown keys silently, so a typo in a field name
    # would write nothing and report success — the quietest possible way
    # to lose a build result.
    unknown = set(fields) - set(Device.model_fields)
    if unknown:
        raise ValueError(f"Unbekannte Gerätefelder: {sorted(unknown)}")

    with _lock:
        index = _read_index()
        stored = index.get(device_id)
        if stored is None:
            return None
        record = dict(stored)
        # The same migration the read paths run, before the model gets to
        # apply its defaults. Without it this path *undid* migrations: an
        # old record has no `wifi_band`, so validation filled in the
        # current default and wrote it back — repinning a flashed C5 that
        # is running on AUTO, on any update at all, including the address
        # the entity resolver writes on its own. What a record means is
        # decided in one place; this is a writer, not a second opinion.
        if isinstance(record.get("config"), dict):
            record["config"] = dict(record["config"])
            _migrate_config(record["config"])
        record.update(fields)
        record["updated_at"] = time.time()
        # Round-trip through the model so a bad field fails here, next to
        # its caller, rather than at the next read.
        device = Device.model_validate(record)
        index[device_id] = device.model_dump()
        _write_index(index)
        return device


INTERRUPTED_MESSAGE = (
    "Der Vorgang wurde durch einen Neustart des Add-ons unterbrochen. "
    "Er lief in einem Prozess, den es nicht mehr gibt — einfach neu starten."
)


def mark_interrupted_jobs() -> list[str]:
    """Fail anything still queued or running at start-up.

    A build or an OTA run lives in a background task. After a restart —
    an add-on update, most often — that task is gone, but the record kept
    saying "wird gebaut…" forever: the button stayed disabled and nothing
    anywhere said why. Called once from the lifespan.
    """
    busy = {BuildStatus.QUEUED, BuildStatus.RUNNING}
    touched: list[str] = []
    with _lock:
        index = _read_index()
        for device_id, record in index.items():
            changed = False
            if record.get("status") in busy:
                record["status"] = BuildStatus.ERROR
                record["build_error"] = INTERRUPTED_MESSAGE
                changed = True
            if record.get("ota_status") in busy:
                record["ota_status"] = BuildStatus.ERROR
                record["ota_error"] = INTERRUPTED_MESSAGE
                changed = True
            if changed:
                record["updated_at"] = time.time()
                touched.append(device_id)
        if touched:
            _write_index(index)
    return touched


def delete_device(device_id: str) -> bool:
    with _lock:
        index = _read_index()
        if device_id not in index:
            return False
        del index[device_id]
        _write_index(index)
    return True


def device_dir(device_id: str) -> Path:
    return DEVICES_DIR / device_id


def config_path(device_id: str) -> Path:
    return device_dir(device_id) / "config.yaml"
