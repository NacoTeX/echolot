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


def test_espectre_switches_cannot_lie_about_a_radar_node():
    """No ESPectre HTTP surface to probe, and the API always encrypted —
    whatever a client sent."""
    config = radar_config(direct_api=True, api_encryption=False)
    assert config.direct_api is False
    assert config.api_encryption is True


def test_a_csi_device_is_left_exactly_as_it_was():
    config = DeviceCreate(name="flur", board="esp32c5", wifi_ssid="netz", wifi_password="passwort123")
    assert config.sensor == "espectre"
    assert config.radar_tx_pin is None and config.radar_rx_pin is None
    assert config.direct_api is True


def test_a_radar_node_gets_no_guessed_csi_entities(store):
    device = devices.create_device(radar_config())
    assert device.entity_motion is None
    assert device.entity_movement_score is None


# --- what was stored before -------------------------------------------


def test_a_device_stored_before_0_14_is_a_csi_device(store):
    raw = {
        "id": "alt", "created_at": 1.0, "updated_at": 1.0,
        "config": {"name": "flur", "board": "esp32c6", "wifi_ssid": "netz",
                   "wifi_password": "passwort123"},
        "api_encryption_key": "k" * 44, "ota_password": "o" * 32,
    }
    devices.INDEX_PATH.write_text(json.dumps({"alt": raw}), encoding="utf-8")
    device = devices.get_device("alt")
    assert device.config.sensor == "espectre"
    # Written back, so the day the default changes it cannot move.
    stored = json.loads(devices.INDEX_PATH.read_text())["alt"]["config"]
    assert stored["sensor"] == "espectre"


def _hash_as_0_13_9_did(device: Device) -> str:
    """config_fingerprint as it was before the radar fields existed."""
    payload = device.config.model_dump()
    for field in ("wifi_password", "sensor", "radar_tx_pin", "radar_rx_pin",
                  "antenna_select_pin", "radar_quiet_means_empty"):
        payload.pop(field, None)
    encoded = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def test_a_device_built_by_0_13_9_is_not_suddenly_stale(store):
    """The regression this guards against: adding fields changes every
    stored config's dump, so every device built by the previous version
    read as "umkonfiguriert, aber noch nicht neu gebaut" the moment the
    add-on updated — with nothing changed."""
    device = devices.create_device(
        DeviceCreate(name="flur", board="esp32c6", wifi_ssid="netz", wifi_password="passwort123")
    )
    device.status = BuildStatus.SUCCESS
    device.build_manifest = {"config_hash": _hash_as_0_13_9_did(device)}
    devices.save_device(device)

    assert devices.config_fingerprint(device) == _hash_as_0_13_9_did(device)
    assert devices.get_device(device.id).firmware_behind_config is False


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


def test_a_csi_manifest_is_unchanged():
    device = as_device(DeviceCreate(name="flur", board="esp32c6", wifi_ssid="netz",
                                    wifi_password="passwort123"))
    manifest = builder.build_manifest(device, None)
    assert manifest["espectre_ref"] == builder.ESPECTRE_REF
    assert manifest["firmware_capabilities"]["supports_router"] is True
    assert "radar_component" not in manifest


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


def test_a_radar_node_cannot_join_a_csi_zone(store, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main.devices, "INDEX_PATH", devices.INDEX_PATH)
    radar = devices.create_device(radar_config())
    client = TestClient(main.app)
    refused = client.post("/api/zones", json={"name": "Wohnzimmer", "device_ids": [radar.id]})
    assert refused.status_code == 422
    assert "Radar" in refused.json()["detail"]


def test_a_built_radar_node_is_not_reported_as_silent_in_home_assistant(store, monkeypatch):
    """The overview asks Home Assistant for CSI entities. A radar node has
    none, and being told it "liefert keine Werte" would be a false alarm.
    Through the route, because the route is where the reading happens."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main.devices, "INDEX_PATH", devices.INDEX_PATH)
    monkeypatch.setenv("ECHOLOT_MQTT_EXPORT", "false")
    device = devices.create_device(radar_config())
    device.status = BuildStatus.SUCCESS
    device.build_manifest = builder.build_manifest(device, None)
    devices.save_device(device)

    body = TestClient(main.app).get("/api/overview").json()
    about_it = [p["kind"] for p in body["problems"] if p.get("device_id") == device.id]
    assert about_it == []


def test_the_card_shows_the_real_fallback_network(store):
    device = devices.create_device(radar_config())
    assert device.public()["fallback_ssid"] == "wohnzimmer-radar Fallback"


def test_a_radar_node_is_not_told_about_espectres_port(store, monkeypatch):
    """62587 is ESPectre's HTTP surface. Its silence on a radar node is
    not a finding, and the sentence about `direct_api` would send
    somebody looking for a switch that node never had."""
    from fastapi.testclient import TestClient

    async def answered(host, timeout=0):
        return {"host": host, "resolved": "192.168.178.30", "verdict": "ok", "api": True,
                "web": True, "direct": False, "reachable": True}

    monkeypatch.setattr(main.devices, "INDEX_PATH", devices.INDEX_PATH)
    monkeypatch.setattr(main.reachability, "check", answered)
    radar = devices.create_device(radar_config())
    csi = devices.create_device(DeviceCreate(name="flur", board="esp32c6", wifi_ssid="netz",
                                             wifi_password="passwort123"))
    client = TestClient(main.app)
    assert client.get(f"/api/devices/{radar.id}/reachability?host=x").json()["direct_message"] is None
    assert "62587" in client.get(f"/api/devices/{csi.id}/reachability?host=x").json()["direct_message"]


def test_hidden_means_hidden_in_the_device_form():
    """`.device-form label` sets display, which beats the hidden
    attribute. Without an explicit rule the band field stood in the form
    for single-band boards since 0.13.8, and the fields of the other
    sensor type would stand there too. Checked in a browser once; this
    keeps the rule from being tidied away."""
    import re

    css = (Path(__file__).resolve().parents[1] / "app" / "static" / "style.css").read_text()
    rule = re.search(r"\.device-form \[hidden\]\s*\{([^}]*)\}", css)
    assert rule and re.search(r"display:\s*none\s*!important", rule.group(1))
    html = (Path(__file__).resolve().parents[1] / "app" / "static" / "index.html").read_text()
    assert 'data-sensor="ld2460"' in html and 'data-sensor="espectre"' in html
