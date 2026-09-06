"""Device registry: persisted metadata + build state for ESPectre devices.

Backed by a single JSON file under the add-on's persistent /data directory
(a plain file is plenty for the handful of devices a home setup has, and
keeps this phase free of a database dependency).
"""

import base64
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

from pydantic import BaseModel, Field, field_validator

from app.board_registry import get_board

DATA_DIR = Path(os.environ.get("ECHOLOT_DATA_DIR", "/data"))
DEVICES_DIR = DATA_DIR / "devices"
INDEX_PATH = DATA_DIR / "devices.json"

_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")

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
    wifi_ssid: str = Field(..., min_length=1, max_length=32)
    wifi_password: str = Field(default="", max_length=64)
    wifi_bssid: str | None = None
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

    def ota_address(self) -> str:
        return self.address or f"{self.config.name}.local"

    #: Never leave the process except through credentials(), which is
    #: reached one device at a time and on purpose.
    _SECRET_FIELDS = ("api_encryption_key", "ota_password")

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
        return data

    def credentials(self) -> dict:
        """The secrets, for the one endpoint whose job is to reveal them."""
        return {
            "api_encryption_key": self.api_encryption_key,
            "ota_password": self.ota_password,
        }

    def apply_update(self, patch: DeviceUpdate) -> None:
        for field, value in patch.model_dump(exclude_unset=True).items():
            if field == "friendly_name":
                self.config.friendly_name = value
            else:
                setattr(self, field, value)


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
        **default_entity_ids(payload.name, payload.friendly_name),
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
