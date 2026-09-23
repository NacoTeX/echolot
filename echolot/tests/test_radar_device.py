"""A radar node as Echolot stores, builds and shows it.

The component and its protocol are tested elsewhere
(test_ld2460_protocol.py, test_ld2460_component.py). This is the add-on
side: what a radar device's config says, what firmware is rendered from
it, what its build manifest records — and, just as much, what does *not*
change for the CSI devices that were there first.
"""

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import builder, devices, main  # noqa: E402
from app.board_registry import BOARDS  # noqa: E402
from app.devices import BuildStatus, Device, DeviceCreate  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(devices, "INDEX_PATH", tmp_path / "devices.json")
    monkeypatch.setattr(devices, "DEVICES_DIR", tmp_path / "devices")
    return tmp_path


def radar_config(board="esp32c5", **extra) -> DeviceCreate:
    return DeviceCreate(name="wohnzimmer-radar", friendly_name="Wohnzimmer", board=board,
                        wifi_ssid="netz", wifi_password="passwort123", sensor="ld2460", **extra)


def as_device(config: DeviceCreate) -> Device:
    return Device(id="probe", created_at=0, updated_at=0, config=config)


# --- the config ---------------------------------------------------------


def test_the_pins_come_from_the_board_and_are_written_down():
    config = radar_config()
    assert (config.radar_tx_pin, config.radar_rx_pin) == (11, 12)
    # Stored, not implied: a later board table must not rewire it.
    dumped = config.model_dump()
    assert dumped["radar_tx_pin"] == 11 and dumped["radar_rx_pin"] == 12


def test_every_board_has_a_suggestion():
    for key, board in BOARDS.items():
        config = radar_config(board=key)
        assert (config.radar_tx_pin, config.radar_rx_pin) == board.radar_uart_pins, key


def test_pins_given_are_pins_kept():
    config = radar_config(radar_tx_pin=4, radar_rx_pin=5)
    assert (config.radar_tx_pin, config.radar_rx_pin) == (4, 5)


@pytest.mark.parametrize(
    "extra, fragment",
    [
        ({"radar_tx_pin": 7, "radar_rx_pin": 7}, "zwei verschiedene Pins"),
        ({"antenna_select_pin": 11}, "Radar-Leitung"),
        ({"radar_tx_pin": 99}, ""),
    ],
)
def test_impossible_wiring_is_refused(extra, fragment):
    with pytest.raises(ValidationError) as err:
        radar_config(**extra)
    assert fragment in str(err.value)


# --- what was stored before -------------------------------------------


def _hash_as_0_13_9_did(device: Device) -> str:
    """config_fingerprint as it was before the radar fields existed."""
    payload = device.config.model_dump()
    for field in ("wifi_password", "sensor", "radar_tx_pin", "radar_rx_pin",
                  "antenna_select_pin", "radar_quiet_means_empty"):
        payload.pop(field, None)
    encoded = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def test_moving_a_new_field_off_its_old_value_is_still_a_change():
    before = as_device(radar_config())
    after = as_device(radar_config(radar_quiet_means_empty=True))
    assert devices.config_fingerprint(before) != devices.config_fingerprint(after)
    moved = as_device(radar_config(radar_tx_pin=4, radar_rx_pin=5))
    assert devices.config_fingerprint(before) != devices.config_fingerprint(moved)


# --- the firmware -------------------------------------------------------


def test_the_rendered_radar_config():
    device = as_device(radar_config(antenna_select_pin=26))
    yaml = builder.render_yaml(device)
    assert "espectre" not in yaml
    assert "tx_pin: GPIO11" in yaml and "rx_pin: GPIO12" in yaml
    assert "baud_rate: 115200" in yaml
    assert f"key: {json.dumps(device.api_encryption_key)}" in yaml
    assert "components: [echolot_ld2460]" in yaml
    assert json.dumps(str(builder.COMPONENTS_DIR)) in yaml
    assert "pin: GPIO26" in yaml and "output.turn_off: antenna_select" in yaml
    assert "band_mode: 2.4GHZ" in yaml
    assert "disabled_by_default: true" in yaml
    assert "quiet_means_empty: false" in yaml


def test_no_antenna_pin_no_antenna_switch():
    yaml = builder.render_yaml(as_device(radar_config()))
    assert "antenna_select" not in yaml
    assert "on_boot" not in yaml


def test_a_single_band_radar_board_gets_no_band_mode():
    yaml = builder.render_yaml(as_device(radar_config(board="esp32c6")))
    assert "band_mode" not in yaml


def test_the_radar_manifest_names_the_component_not_espectre():
    device = as_device(radar_config())
    manifest = builder.build_manifest(device, None)
    assert manifest["sensor"] == "ld2460"
    assert manifest["radar_component"]["name"] == "echolot_ld2460"
    assert manifest["radar_component"]["frame_format"] == 1
    assert len(manifest["radar_component"]["sha256"]) == 64
    assert manifest["radar_quiet_means_empty"] is False
    assert "espectre_ref" not in manifest
    assert "firmware_capabilities" not in manifest
    assert "wifi_password" not in json.dumps(manifest)


def test_the_component_digest_follows_its_sources(tmp_path, monkeypatch):
    component = tmp_path / "echolot_ld2460"
    component.mkdir()
    (component / "a.h").write_text("one")
    monkeypatch.setattr(builder, "COMPONENTS_DIR", tmp_path)
    first = builder.radar_component_digest()
    (component / "a.h").write_text("two")
    assert builder.radar_component_digest() != first


# --- the fallback AP, for every device ---------------------------------


def test_a_long_name_still_gets_a_valid_fallback_ssid():
    """Found while adding radar nodes, and older than them: a name of 24
    or more characters gave "<name> Fallback" beyond the 32 characters
    ESPHome allows, and the build failed."""
    name = "a" * 32
    ssid = devices.fallback_ssid(name)
    assert len(ssid) == 32
    assert ssid.endswith(" Fallback")
    for config in (
        DeviceCreate(name=name, board="esp32c6", wifi_ssid="netz", wifi_password="passwort123"),
        DeviceCreate(name=name, board="esp32c6", wifi_ssid="netz", wifi_password="passwort123",
                     sensor="ld2460"),
    ):
        yaml = builder.render_yaml(as_device(config))
        assert f"ssid: {json.dumps(ssid)}" in yaml
        assert f"{name} Fallback" not in yaml


def test_a_short_name_keeps_the_ssid_it_always_had():
    assert devices.fallback_ssid("flur") == "flur Fallback"


# --- the rest of the add-on --------------------------------------------


def test_the_board_list_carries_the_pin_suggestion():
    listed = {b["key"]: b for b in main.list_boards()}
    assert listed["esp32c5"]["radar_uart_pins"] == [11, 12]


def test_the_sensor_cannot_be_swapped_on_an_existing_device(store):
    device = devices.create_device(radar_config())
    with pytest.raises(ValueError, match="sensor"):
        devices.reconfigure(device.id, {"sensor": "espectre"})


def test_the_wiring_can_be_changed_later(store):
    device = devices.create_device(radar_config())
    changed = devices.reconfigure(device.id, {"radar_tx_pin": 4, "radar_rx_pin": 5,
                                              "antenna_select_pin": None})
    assert (changed.config.radar_tx_pin, changed.config.radar_rx_pin) == (4, 5)


def test_the_card_shows_the_real_fallback_network(store):
    device = devices.create_device(radar_config())
    assert device.public()["fallback_ssid"] == "wohnzimmer-radar Fallback"

