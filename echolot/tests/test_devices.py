"""The device registry after CSI: radar only, and nobody's device lost.

Three promises are tested here:

  * Changing a firmware option keeps the device — id, credentials, name.
  * A radar image built by 0.14 is not called stale by 1.0, although 1.0
    dropped the CSI fields its config hash used to include.
  * A Wi-Fi CSI device stored by an earlier version stays exactly as it
    was written until somebody converts it, and converting keeps its id,
    its node name and every credential.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import builder, devices  # noqa: E402
from app.devices import BuildStatus, Device, DeviceCreate  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(devices, "DATA_DIR", tmp_path)
    monkeypatch.setattr(devices, "INDEX_PATH", tmp_path / "devices.json")
    monkeypatch.setattr(devices, "DEVICES_DIR", tmp_path / "devices")
    return tmp_path


def make(board="esp32c6", **config) -> Device:
    return devices.create_device(
        DeviceCreate(name="flur", friendly_name="Flur", board=board, wifi_ssid="netz",
                     wifi_password="passwort123", **config)
    )


def built(device: Device) -> Device:
    device.status = BuildStatus.SUCCESS
    device.build_manifest = builder.build_manifest(device, None)
    devices.save_device(device)
    return device


# --- creating ---------------------------------------------------------------


def test_the_board_suggests_the_radar_pins():
    config = DeviceCreate(name="k", board="esp32c5", wifi_ssid="n")
    assert (config.radar_tx_pin, config.radar_rx_pin) == (11, 12)
    assert config.sensor == "ld2460"


def test_a_csi_device_can_no_longer_be_created():
    with pytest.raises(ValueError):
        DeviceCreate(name="k", board="esp32c5", wifi_ssid="n", sensor="espectre")


def test_the_csi_options_are_gone_from_the_model():
    for field in ("csi_target_pps", "detection_algorithm", "direct_api", "api_encryption", "sensing_mode"):
        assert field not in DeviceCreate.model_fields


def test_a_band_the_board_has_no_radio_for_is_refused():
    with pytest.raises(ValueError, match="zwei Funkbänder"):
        DeviceCreate(name="k", board="esp32c6", wifi_ssid="n", wifi_band="5GHz")


def test_only_the_c5_renders_a_band_mode(store):
    assert "band_mode: 5GHZ" in builder.render_yaml(make(board="esp32c5", wifi_band="5GHz"))
    other = Device(id="o", created_at=0, updated_at=0,
                   config=DeviceCreate(name="o", board="esp32c6", wifi_ssid="n"))
    assert "band_mode" not in builder.render_yaml(other)


# --- reconfiguring ----------------------------------------------------------


def test_changing_a_firmware_option_keeps_the_device(store):
    device = make()
    key, ota = device.api_encryption_key, device.ota_password
    changed = devices.reconfigure(device.id, {"radar_quiet_means_empty": True})
    assert changed.id == device.id
    assert changed.config.radar_quiet_means_empty is True
    assert (changed.api_encryption_key, changed.ota_password) == (key, ota)
    assert changed.config.wifi_password == "passwort123"


def test_the_node_name_board_and_sensor_are_not_settings(store):
    device = make()
    for field, value in (("name", "other"), ("board", "esp32"), ("sensor", "ld2460")):
        with pytest.raises(ValueError, match="nicht ändern"):
            devices.reconfigure(device.id, {field: value})


def test_an_unknown_or_removed_field_is_refused(store):
    device = make()
    with pytest.raises(ValueError, match="Unbekannte"):
        devices.reconfigure(device.id, {"csi_target_pps": 50})


def test_a_refused_change_leaves_the_stored_device_alone(store):
    device = make()
    with pytest.raises(ValueError):
        devices.reconfigure(device.id, {"radar_tx_pin": device.config.radar_rx_pin})
    assert devices.get_device(device.id).config.radar_tx_pin == device.config.radar_tx_pin


def test_behind_config_follows_the_hash(store):
    device = make()
    assert devices.get_device(device.id).firmware_behind_config is False  # never built
    built(device)
    assert devices.get_device(device.id).firmware_behind_config is False
    devices.reconfigure(device.id, {"diagnostics": False})
    assert devices.get_device(device.id).firmware_behind_config is True


# --- 0.14 images ------------------------------------------------------------

#: What 0.14.0's config_fingerprint returned for these two configs,
#: computed with the 0.14.0 source (git 5d43d34, app/devices.py). Golden
#: values rather than a re-derivation, so this test checks against what
#: 0.14 actually wrote and not against 1.0's idea of it.
HASH_0_14 = {
    "c5": "a0f985c349afba22",
    "esp32": "e82817088fd8b42c",
}


def device_like_0_14(key: str) -> Device:
    if key == "c5":
        config = DeviceCreate(name="wohnzimmer-radar", friendly_name="Wohnzimmer", board="esp32c5",
                              wifi_ssid="netz", wifi_password="passwort123", antenna_select_pin=26)
    else:
        config = DeviceCreate(name="flur", board="esp32", wifi_ssid="netz", wifi_password="",
                              radar_quiet_means_empty=True, diagnostics=False)
    return Device(id="x", created_at=0, updated_at=0, config=config,
                  build_manifest={"sensor": "ld2460", "config_hash": HASH_0_14[key]})


@pytest.mark.parametrize("key", sorted(HASH_0_14))
def test_a_radar_image_built_by_0_14_is_not_stale(key):
    device = device_like_0_14(key)
    # The current hash differs — 1.0 has fewer fields — which is exactly
    # why comparing only against it would flag every 0.14 build.
    assert devices.config_fingerprint(device) != HASH_0_14[key]
    assert device.firmware_behind_config is False


def test_a_real_change_on_a_0_14_image_is_still_a_change():
    device = device_like_0_14("c5")
    device.config.radar_quiet_means_empty = True
    assert device.firmware_behind_config is True


# --- CSI devices from before 1.0 ------------------------------------------

LEGACY = {
    "id": "legacy-1",
    "created_at": 1700000000.0,
    "updated_at": 1700000000.0,
    "config": {
        "name": "kueche", "friendly_name": "Küche", "board": "esp32c6", "sensor": "espectre",
        "wifi_ssid": "netz", "wifi_password": "passwort123", "wifi_bssid": None,
        "csi_target_pps": 100, "detection_algorithm": "lightweight", "direct_api": True,
        "web_server": True, "diagnostics": True, "log_level": "INFO",
    },
    "entity_motion": "binary_sensor.kuche_motion_detected",
    "presence_profile": {"baseline_rate": 0.05},
    "api_encryption_key": "S0VZS0VZS0VZS0VZS0VZS0VZS0VZS0VZS0VZS0VZS0U=",
    "ota_password": "ota-secret",
    "fallback_password": "fallback-secret",
    "address": "192.168.1.23",
    "build_manifest": {"espectre_ref": "ce23b0b6", "config_hash": "abc"},
    "status": "success",
}


def write_legacy(store, record=LEGACY, extra=None):
    index = {record["id"]: json.loads(json.dumps(record))}
    if extra:
        index.update(extra)
    (store / "devices.json").write_text(json.dumps(index))


def test_a_csi_record_is_listed_separately_and_not_rewritten(store):
    write_legacy(store)
    before = (store / "devices.json").read_text()
    assert devices.list_devices() == []
    assert [d["id"] for d in devices.list_legacy_devices()] == ["legacy-1"]
    assert devices.get_device("legacy-1") is None
    assert (store / "devices.json").read_text() == before


def test_a_record_without_a_sensor_field_is_a_csi_record(store):
    record = json.loads(json.dumps(LEGACY))
    del record["config"]["sensor"]
    write_legacy(store, record)
    assert [d["id"] for d in devices.list_legacy_devices()] == ["legacy-1"]


def test_the_legacy_listing_carries_no_secrets(store):
    write_legacy(store)
    encoded = json.dumps(devices.list_legacy_devices())
    for secret in ("passwort123", "ota-secret", "fallback-secret", LEGACY["api_encryption_key"]):
        assert secret not in encoded


def test_converting_keeps_identity_and_credentials(store):
    write_legacy(store)
    device = devices.convert_legacy("legacy-1")
    assert device.id == "legacy-1"
    assert device.config.name == "kueche"
    assert device.config.friendly_name == "Küche"
    assert device.config.sensor == "ld2460"
    assert device.config.wifi_password == "passwort123"
    assert device.api_encryption_key == LEGACY["api_encryption_key"]
    assert device.ota_password == "ota-secret"
    assert device.fallback_password == "fallback-secret"
    assert device.address == "192.168.1.23"
    assert device.created_at == LEGACY["created_at"]
    assert (device.config.radar_tx_pin, device.config.radar_rx_pin) == (16, 17)
    assert devices.list_legacy_devices() == []


def test_the_whole_csi_record_is_kept_on_disk(store):
    write_legacy(store)
    devices.convert_legacy("legacy-1")
    kept = json.loads(devices.legacy_backup_path("legacy-1").read_text())
    assert kept["presence_profile"] == {"baseline_rate": 0.05}
    assert kept["config"]["csi_target_pps"] == 100


def test_a_converted_device_reads_as_running_csi_until_rebuilt(store):
    write_legacy(store)
    device = devices.convert_legacy("legacy-1")
    assert device.runs_radar_firmware is False
    assert device.firmware_behind_config is True
    assert device.public()["runs_radar_firmware"] is False


def test_conversion_takes_radar_pins_and_checks_them(store):
    write_legacy(store)
    with pytest.raises(ValueError):
        devices.convert_legacy("legacy-1", {"radar_tx_pin": 5, "radar_rx_pin": 5})
    # Refused before anything was written.
    assert devices.list_legacy_devices()[0]["id"] == "legacy-1"
    assert not devices.legacy_backup_path("legacy-1").exists()
    device = devices.convert_legacy("legacy-1", {"radar_tx_pin": 4, "radar_rx_pin": 5})
    assert (device.config.radar_tx_pin, device.config.radar_rx_pin) == (4, 5)


def test_converting_a_radar_device_is_not_a_thing(store):
    device = make()
    assert devices.convert_legacy(device.id) is None


def test_radar_routes_do_not_touch_a_csi_record(store):
    write_legacy(store)
    assert devices.update_device("legacy-1", address="x") is None
    assert devices.reconfigure("legacy-1", {"diagnostics": False}) is None
    assert devices.delete_device("legacy-1") is False
    assert devices.list_legacy_devices()[0]["id"] == "legacy-1"


def test_removing_a_csi_record_keeps_a_copy(store):
    write_legacy(store)
    assert devices.delete_legacy_device("legacy-1") is True
    assert devices.list_legacy_devices() == []
    assert json.loads(devices.legacy_backup_path("legacy-1").read_text())["ota_password"] == "ota-secret"


def test_credentials_are_generated_once_for_old_records(store):
    record = json.loads(json.dumps(LEGACY))
    del record["api_encryption_key"]
    write_legacy(store, record)
    devices.list_devices()
    first = json.loads((store / "devices.json").read_text())["legacy-1"]["api_encryption_key"]
    devices.list_devices()
    assert json.loads((store / "devices.json").read_text())["legacy-1"]["api_encryption_key"] == first
