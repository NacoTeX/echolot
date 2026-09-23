"""Device registry: persisted metadata and build state for radar nodes.

Backed by a single JSON file under the add-on's persistent /data directory
(a plain file is plenty for the handful of devices a home setup has).

Since 1.0 every device Echolot builds is an ESP32 with an HLK-LD2460. The
same file may still hold Wi-Fi CSI devices from before 1.0; those records
are left exactly as they were written — id, credentials, learned profile —
and are listed separately (`list_legacy_devices`). `convert_legacy` turns
one into a radar node under the same id and with the same credentials,
after keeping a full copy of the old record next to its build directory.
"""

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import uuid
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.board_registry import Board, get_board

DATA_DIR = Path(os.environ.get("ECHOLOT_DATA_DIR", "/data"))
DEVICES_DIR = DATA_DIR / "devices"
INDEX_PATH = DATA_DIR / "devices.json"

_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")

#: The three answers to "which radio". Written the way ESPHome spells
#: them, bar case: `wifi.band_mode` takes 2.4GHZ, 5GHZ or AUTO, and the
#: template upper-cases on the way out.
BAND_24 = "2.4GHz"
BAND_5 = "5GHz"
BAND_AUTO = "auto"

#: What a device senses with. `ld2460` is the only kind Echolot builds.
#: `espectre` (Wi-Fi CSI) is what every record written before 0.14.0 is,
#: and what a record without the field means.
SENSOR_ESPECTRE = "espectre"
SENSOR_LD2460 = "ld2460"

#: Config fields that must never reach a manifest, a log or a hash.
_SECRET_CONFIG_FIELDS = ("wifi_password",)

#: Firmware fields nobody may change on an existing device.
#:
#: `name` is what Home Assistant builds every entity id from, and what
#: the OTA hostname resolves to. `board` is another chip, so another
#: image. `sensor` is fixed since 1.0 anyway.
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
    #: Only one value since 1.0. Kept as a field so a stored record and a
    #: build manifest still say what they describe.
    sensor: Literal["ld2460"] = SENSOR_LD2460
    wifi_ssid: str = Field(..., min_length=1, max_length=32)
    wifi_password: str = Field(default="", max_length=64)
    wifi_bssid: str | None = None
    #: Which radio carries the data. Only the ESP32-C5 has the choice;
    #: see `board_registry.Board.dual_band`. Radar does not measure on the
    #: Wi-Fi band, so this is a network decision, not a sensing one.
    wifi_band: Literal["2.4GHz", "5GHz", "auto"] = "2.4GHz"
    #: Serve a status page on the device at http://<ip>/.
    web_server: bool = True
    #: Signal strength, uptime, chip temperature, IP address, restart
    #: buttons, and the radar's byte and frame counters.
    diagnostics: bool = True
    log_level: Literal["NONE", "ERROR", "WARN", "INFO", "DEBUG", "VERBOSE"] = "INFO"

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
    #: changes what "0 people" means.
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
    def _validate_band(self) -> "DeviceCreate":
        """A band the chip has no radio for is a mistake, not a preference.

        ESPHome would reject the YAML anyway — `wifi.band_mode` only exists
        on the C5 — and a build that fails after the toolchain has started
        is a much worse way to learn it.
        """
        if self.wifi_band != BAND_24 and not get_board(self.board).dual_band:
            raise ValueError(
                f"wifi_band '{self.wifi_band}' braucht zwei Funkbänder — "
                "davon hat nur der ESP32-C5 welche."
            )
        return self

    @model_validator(mode="after")
    def _validate_radar(self) -> "DeviceCreate":
        """Fill in and check the radar wiring.

        Filled in here rather than at render time so the stored config
        says which pins the firmware uses. A default that only exists in
        the template is a default nobody can see, and one that changes
        with a later board table would silently rewire a device on its
        next build.
        """
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


#: Fields a 0.14.x radar config carried and 1.0 does not, with the value
#: 0.14 forced on every radar node. A radar image built by 0.14 hashed
#: them; hashing without them would call every such image stale although
#: nothing it builds has changed. See `_fingerprints`.
_REMOVED_RADAR_FIELDS = {
    "sensing_mode": "router",
    "detection_algorithm": "lightweight",
    "csi_target_pps": 100,
    "csi_traffic_mode": "internal",
    "traffic_generator_mode": "ping",
    "evaluation_interval_ms": 250,
    "direct_api": False,
    "api_encryption": True,
}

#: Fields whose value "absent" is left out of the hash — see 0.14.0.
_FINGERPRINT_ABSENT_VALUES = {
    "radar_tx_pin": None,
    "radar_rx_pin": None,
    "antenna_select_pin": None,
    "radar_quiet_means_empty": False,
}


def _hash(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def _fingerprint_payload(device) -> dict:
    payload = device.config.model_dump()
    for field in _SECRET_CONFIG_FIELDS:
        payload.pop(field, None)
    for field, absent in _FINGERPRINT_ABSENT_VALUES.items():
        if field in payload and payload[field] == absent:
            del payload[field]
    return payload


def config_fingerprint(device) -> str:
    """A short hash of what would be built, with the secrets left out."""
    return _hash(_fingerprint_payload(device))


def _fingerprints(device) -> set[str]:
    """Every hash an image built from this config may have recorded.

    The current one, and the one 0.14.x wrote for the same config: its
    radar configs carried the ESPectre fields at fixed values, which 1.0
    no longer has. Same firmware, different bookkeeping.
    """
    current = _fingerprint_payload(device)
    return {_hash(current), _hash({**current, **_REMOVED_RADAR_FIELDS})}


def get_board_safely(key) -> Board:
    """The board, or a stand-in — for code that must not raise."""
    try:
        return get_board(str(key))
    except ValueError:
        return Board(key=str(key), label=str(key), variant=None, chip_family="")


def effective_band(config: DeviceCreate) -> str:
    """Which band firmware built from this config actually uses."""
    return config.wifi_band if get_board(config.board).dual_band else BAND_24


#: What 802.11 allows for an SSID, and what ESPHome enforces.
_SSID_MAX = 32
_FALLBACK_SUFFIX = " Fallback"


def fallback_ssid(device_name: str) -> str:
    """The fallback AP's SSID: the device name, shortened to fit.

    "<name> Fallback" for a 32-character name is 41 characters, which
    ESPHome refuses. The name is cut rather than the suffix, because the
    suffix is what tells somebody scanning for networks what this one is.
    """
    return device_name[: _SSID_MAX - len(_FALLBACK_SUFFIX)] + _FALLBACK_SUFFIX


class DeviceUpdate(BaseModel):
    """Partial update for fields that don't require a rebuild/reflash."""

    friendly_name: str | None = None
    #: Where to reach the device on the network, for OTA updates, the
    #: live link and the reachability check. Empty means "<name>.local".
    address: str | None = None


def new_api_key() -> str:
    """A fresh 32-byte key, base64-encoded the way ESPHome expects it."""
    return base64.b64encode(secrets.token_bytes(32)).decode()


def new_ota_password() -> str:
    return secrets.token_hex(16)


def new_fallback_password() -> str:
    """The access point a device opens when it cannot reach the Wi-Fi.

    It carries a captive portal that takes Wi-Fi credentials, so it is
    never open. ESPHome wants at least eight characters.
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
    #: Guards the fallback access point.
    fallback_password: str = Field(default_factory=new_fallback_password)
    #: What the last successful build was made of. For a device converted
    #: from CSI this still describes the CSI image until the first radar
    #: build — which is what is on the chip.
    build_manifest: dict | None = None
    #: Baked into the firmware, and needed by Home Assistant and by
    #: Echolot's own live link — so it has to be readable here.
    api_encryption_key: str = Field(default_factory=new_api_key)
    ota_password: str = Field(default_factory=new_ota_password)
    #: Tracked separately from the build: a failed OTA must not make a
    #: perfectly good firmware image look unbuilt.
    ota_status: BuildStatus = BuildStatus.IDLE
    ota_log: str = ""
    ota_error: str | None = None
    ota_last_success: float | None = None
    #: Hostname or IP. Empty means "<node name>.local".
    address: str | None = None
    #: Set when this record was converted from a CSI device.
    converted_from_csi_at: float | None = None

    def firmware_size(self) -> int | None:
        if not self.firmware_bin:
            return None
        path = device_dir(self.id) / self.firmware_bin
        try:
            return path.stat().st_size
        except OSError:
            return None

    def ota_address(self) -> str:
        return self.address or f"{self.config.name}.local"

    def display_name(self) -> str:
        return self.config.friendly_name or self.config.name

    #: Never leave the process except through credentials().
    _SECRET_FIELDS = ("api_encryption_key", "ota_password")

    @property
    def runs_radar_firmware(self) -> bool | None:
        """Whether the last image built for this device reads the radar.

        None when nothing has been built. False for a converted CSI device
        until its first radar build — its chip still runs ESPectre.
        """
        if not self.build_manifest:
            return None
        return self.build_manifest.get("sensor") == SENSOR_LD2460

    @property
    def firmware_behind_config(self) -> bool:
        """Whether the flashed image was built from a different config."""
        recorded = (self.build_manifest or {}).get("config_hash")
        if not recorded:
            return False
        if self.runs_radar_firmware is False:
            return True
        return recorded not in _fingerprints(self)

    def public(self) -> dict:
        """Serialize without anything secret."""
        data = self.model_dump()
        data["config"]["wifi_password"] = "********" if self.config.wifi_password else ""
        for field in self._SECRET_FIELDS:
            data.pop(field, None)
        data["has_credentials"] = bool(self.api_encryption_key)
        data["firmware_behind_config"] = self.firmware_behind_config
        data["runs_radar_firmware"] = self.runs_radar_firmware
        data["firmware_size"] = self.firmware_size()
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

    The patch is merged into the stored config and the result goes
    through `DeviceCreate`, so everything refused at creation is refused
    here for the same reason. Returns None when the device is gone.
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
            "OTA-Hostnamen, und ein anderes Board ist andere Hardware."
        )

    with _lock:
        index = _read_index()
        stored = index.get(device_id)
        if stored is None or is_legacy_record(stored):
            return None
        record = dict(stored)
        raw = dict(record.get("config") or {})
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


def is_legacy_record(record: dict) -> bool:
    """A stored Wi-Fi CSI device: no `sensor`, or `sensor: espectre`."""
    config = record.get("config") if isinstance(record, dict) else None
    if not isinstance(config, dict):
        return False
    return config.get("sensor", SENSOR_ESPECTRE) != SENSOR_LD2460


def _migrate(index: dict[str, dict]) -> bool:
    """Generate credentials once for records that predate them.

    `api_encryption_key` and `ota_password` have generating defaults, so a
    record stored before they existed would otherwise get a *different*
    value on every load. Applied to CSI records too: their credentials
    are what a conversion carries over.
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
    return changed


def list_devices() -> list[Device]:
    with _lock:
        index = _read_index()
        if _migrate(index):
            _write_index(index)
        return [Device.model_validate(v) for v in index.values() if not is_legacy_record(v)]


def get_device(device_id: str) -> Device | None:
    with _lock:
        index = _read_index()
        record = index.get(device_id)
        if record is None or is_legacy_record(record):
            return None
        if _migrate(index):
            _write_index(index)
        return Device.model_validate(index[device_id])


def legacy_summary(device_id: str, record: dict) -> dict:
    config = record.get("config") or {}
    board = get_board_safely(config.get("board"))
    return {
        "id": device_id,
        "name": config.get("name"),
        "friendly_name": config.get("friendly_name"),
        "board": config.get("board"),
        "board_label": board.label,
        "created_at": record.get("created_at"),
        # Whether a conversion would find radar pins without asking.
        "convertible": board.radar_uart_pins is not None,
    }


def list_legacy_devices() -> list[dict]:
    """The CSI devices still on file, without anything secret."""
    with _lock:
        index = _read_index()
    return [legacy_summary(key, raw) for key, raw in index.items() if is_legacy_record(raw)]


#: Config fields a CSI record shares with a radar config and that carry
#: over as they are. Everything else was about CSI.
_CARRIED_CONFIG_FIELDS = (
    "name", "friendly_name", "board", "wifi_ssid", "wifi_password", "wifi_bssid",
    "web_server", "diagnostics", "log_level", "wifi_band",
)


def legacy_backup_path(device_id: str) -> Path:
    return device_dir(device_id) / "csi_record.json"


def convert_legacy(device_id: str, radar: dict | None = None) -> Device | None:
    """Turn a stored CSI device into a radar node, keeping who it is.

    Kept: the id, the node name (and with it every Home Assistant entity
    id derived from it), the Wi-Fi settings, the API key, the OTA
    password, the fallback password, the address and the last build
    manifest — which still describes the CSI image that is on the chip,
    so the device reads as "needs a new build" until it gets one.

    Not carried: everything that described a CSI measurement. It is not
    thrown away either: the complete record goes to
    devices/<id>/csi_record.json before anything is written.

    Returns None when there is no such CSI record. Raises ValueError when
    the radar settings are not acceptable; nothing is written then.
    """
    radar = dict(radar or {})
    unknown = set(radar) - {"radar_tx_pin", "radar_rx_pin", "antenna_select_pin",
                            "radar_quiet_means_empty"}
    if unknown:
        raise ValueError(f"Unbekannte Radar-Felder: {', '.join(sorted(unknown))}")
    with _lock:
        index = _read_index()
        stored = index.get(device_id)
        if stored is None or not is_legacy_record(stored):
            return None
        _migrate(index)
        stored = index[device_id]
        old_config = stored.get("config") or {}
        config = {k: old_config[k] for k in _CARRIED_CONFIG_FIELDS if k in old_config}
        # A C5 stored before 0.13.8 has no band and ran on AUTO; its new
        # firmware is a new decision anyway, so the default applies.
        config.update(radar)
        config["sensor"] = SENSOR_LD2460
        new_config = DeviceCreate.model_validate(config)

        backup = legacy_backup_path(device_id)
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(json.dumps(stored, indent=2), encoding="utf-8")

        now = time.time()
        record = {
            "id": device_id,
            "created_at": stored.get("created_at") or now,
            "updated_at": now,
            "config": new_config.model_dump(),
            "fallback_password": stored.get("fallback_password") or new_fallback_password(),
            "api_encryption_key": stored["api_encryption_key"],
            "ota_password": stored["ota_password"],
            "address": stored.get("address"),
            "build_manifest": stored.get("build_manifest"),
            "chip_family": stored.get("chip_family"),
            "ota_last_success": stored.get("ota_last_success"),
            "converted_from_csi_at": now,
        }
        device = Device.model_validate(record)
        index[device_id] = device.model_dump()
        _write_index(index)
        return device


def delete_legacy_device(device_id: str) -> bool:
    """Remove a CSI record, after keeping a copy of it."""
    with _lock:
        index = _read_index()
        stored = index.get(device_id)
        if stored is None or not is_legacy_record(stored):
            return False
        backup = legacy_backup_path(device_id)
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(json.dumps(stored, indent=2), encoding="utf-8")
        del index[device_id]
        _write_index(index)
        return True


def create_device(payload: DeviceCreate) -> Device:
    now = time.time()
    device = Device(id=str(uuid.uuid4()), created_at=now, updated_at=now, config=payload)
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
    Device object for minutes: anything changed meanwhile was silently
    replaced by the copy the job started with, and a device deleted
    during a build came back when the build finished.

    Returns None when the device is gone.
    """
    unknown = set(fields) - set(Device.model_fields)
    if unknown:
        raise ValueError(f"Unbekannte Gerätefelder: {sorted(unknown)}")

    with _lock:
        index = _read_index()
        stored = index.get(device_id)
        if stored is None or is_legacy_record(stored):
            return None
        record = dict(stored)
        record.update(fields)
        record["updated_at"] = time.time()
        device = Device.model_validate(record)
        index[device_id] = device.model_dump()
        _write_index(index)
        return device


INTERRUPTED_MESSAGE = (
    "Der Vorgang wurde durch einen Neustart des Add-ons unterbrochen. "
    "Er lief in einem Prozess, den es nicht mehr gibt — einfach neu starten."
)


def mark_interrupted_jobs() -> list[str]:
    """Fail anything still queued or running at start-up."""
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
        if device_id not in index or is_legacy_record(index[device_id]):
            return False
        del index[device_id]
        _write_index(index)
    return True


def device_dir(device_id: str) -> Path:
    return DEVICES_DIR / device_id


def config_path(device_id: str) -> Path:
    return device_dir(device_id) / "config.yaml"
