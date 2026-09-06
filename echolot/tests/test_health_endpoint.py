"""End-to-end test of the device diagnosis against a fake Home Assistant.

The snapshot below is the entity set actually read off the two devices
this project first ran on: one complete, one missing its three sensing
entities, both with every CSI diagnostic at `unknown`.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import ha_client, main  # noqa: E402
from app.devices import BuildStatus, Device, DeviceCreate  # noqa: E402


def make_device(name: str) -> Device:
    return Device(
        id=name,
        created_at=1,
        updated_at=2,
        status=BuildStatus.SUCCESS,
        firmware_bin="firmware.factory.bin",
        chip_family="ESP32-C6",
        config=DeviceCreate(
            name=name,
            board="esp32c6",
            wifi_ssid="netz",
            wifi_password="passwort123",
            detection_algorithm="high_accuracy",
            csi_target_pps=100,
        ),
    )


def entity(entity_id: str, friendly: str, value: str) -> dict:
    return {"entity_id": entity_id, "state": value, "attributes": {"friendly_name": friendly}}


DIAGNOSTIC_ENTITIES = [
    ("sensor.test22_csi_accepted_rate", "test22 CSI Accepted Rate"),
    ("sensor.test22_csi_filtered_rate", "test22 CSI Filtered Rate"),
    ("sensor.test22_csi_stale_rate", "test22 CSI Stale Rate"),
    ("sensor.test22_csi_missing_slot_rate", "test22 CSI Missing Slot Rate"),
    ("sensor.test22_csi_out_of_order_rate", "test22 CSI Out-of-order Rate"),
    ("sensor.test22_traffic_tx_rate", "test22 Traffic TX Rate"),
]


def healthy_snapshot(threshold: str = "0.5", rate: str = "98") -> list[dict]:
    states = [
        entity("binary_sensor.test22_motion_detected", "test22 Motion Detected", "off"),
        entity("sensor.test22_movement_score", "test22 Movement Score", "0.31"),
        entity("number.test22_threshold", "test22 Threshold", threshold),
        entity("button.test22_recalibrate", "test22 Recalibrate", "unknown"),
        entity("button.test22_refresh_diagnostics", "test22 Refresh Diagnostics", "unknown"),
        entity("select.test22_detection_profile", "test22 Detection Profile", "high_accuracy"),
    ]
    for entity_id, friendly in DIAGNOSTIC_ENTITIES:
        states.append(entity(entity_id, friendly, rate))
    return states


@pytest.fixture
def client(monkeypatch):
    device = make_device("test22")
    monkeypatch.setattr(main.devices, "get_device", lambda _id: device)
    return TestClient(main.app), device


def install_ha(monkeypatch, states, history=None):
    async def fake_list_states():
        return states

    async def fake_get_history(entity_id, minutes):
        return history or []

    monkeypatch.setattr(ha_client, "list_states", fake_list_states)
    monkeypatch.setattr(ha_client, "get_history", fake_get_history)


def test_a_healthy_device_reports_nothing_wrong(client, monkeypatch):
    api, _ = client
    install_ha(monkeypatch, healthy_snapshot(), [{"state": "0.31"}])
    body = api.get("/api/devices/test22/health").json()
    assert body["findings"] == []
    assert body["profile"] == "high_accuracy"
    assert body["can_refresh"] is True


def test_the_real_threshold_mismatch_is_reported(client, monkeypatch):
    api, _ = client
    install_ha(monkeypatch, healthy_snapshot(threshold="0.6621854"), [{"state": "0.31"}])
    kinds = [f["kind"] for f in api.get("/api/devices/test22/health").json()["findings"]]
    assert "threshold_profile_mismatch" in kinds


def test_unread_diagnostics_are_reported_and_do_not_read_as_zero(client, monkeypatch):
    api, _ = client
    install_ha(monkeypatch, healthy_snapshot(rate="unknown"), [{"state": "0.31"}])
    findings = api.get("/api/devices/test22/health").json()["findings"]
    kinds = [f["kind"] for f in findings]
    assert "diagnostics_never_read" in kinds
    # A rate of `unknown` must not be mistaken for a starved radio.
    assert "csi_starved" not in kinds


def test_a_starved_radio_outranks_the_rest(client, monkeypatch):
    api, _ = client
    install_ha(monkeypatch, healthy_snapshot(rate="4"), [{"state": "0.31"}])
    findings = api.get("/api/devices/test22/health").json()["findings"]
    assert findings[0]["kind"] == "csi_starved"
    assert findings[0]["severity"] == "blocker"


def test_a_device_without_its_sensing_entities_is_caught(client, monkeypatch):
    # test11: control and diagnostic entities present, sensing ones gone.
    api, _ = client
    states = [
        entity("button.test22_recalibrate", "test22 Recalibrate", "unknown"),
        entity("select.test22_detection_profile", "test22 Detection Profile", "high_accuracy"),
    ]
    install_ha(monkeypatch, states)
    findings = api.get("/api/devices/test22/health").json()["findings"]
    assert findings[0]["kind"] == "core_entities_missing"
    assert findings[0]["action"] == "rebuild"


def test_a_device_home_assistant_never_saw_is_not_called_stale(client, monkeypatch):
    api, _ = client
    install_ha(monkeypatch, [])
    kinds = [f["kind"] for f in api.get("/api/devices/test22/health").json()["findings"]]
    assert "core_entities_missing" not in kinds


def test_the_score_history_is_measured_against_the_threshold(client, monkeypatch):
    api, _ = client
    history = [{"state": "0.0000012"}, {"state": "unknown"}, {"state": "0.0000092"}]
    install_ha(monkeypatch, healthy_snapshot(threshold="0.6621854"), history)
    body = api.get("/api/devices/test22/health").json()
    # The unparsable entry is skipped rather than counted as zero.
    assert body["samples"] == 2
    kinds = [f["kind"] for f in body["findings"]]
    assert "score_far_below_threshold" in kinds


def test_an_unbuilt_device_has_nothing_to_diagnose(monkeypatch):
    device = make_device("neu")
    device.status = BuildStatus.IDLE
    monkeypatch.setattr(main.devices, "get_device", lambda _id: device)
    assert TestClient(main.app).get("/api/devices/neu/health").status_code == 409


def test_home_assistant_being_down_is_a_503(client, monkeypatch):
    api, _ = client

    async def boom():
        raise ha_client.HomeAssistantUnavailable("kein Token")

    monkeypatch.setattr(ha_client, "list_states", boom)
    assert api.get("/api/devices/test22/health").status_code == 503


def test_refreshing_diagnostics_presses_the_device_button(client, monkeypatch):
    api, _ = client
    install_ha(monkeypatch, healthy_snapshot())
    pressed = []

    async def fake_call(domain, service, entity_id, **extra):
        pressed.append((domain, service, entity_id))

    monkeypatch.setattr(ha_client, "call_service", fake_call)
    body = api.post("/api/devices/test22/diagnostics/refresh").json()
    assert body["status"] == "ok"
    assert pressed == [("button", "press", "button.test22_refresh_diagnostics")]


def test_refreshing_without_the_button_is_a_404(client, monkeypatch):
    api, _ = client
    install_ha(monkeypatch, [entity("sensor.test22_movement_score", "test22 Movement Score", "0.1")])
    assert api.post("/api/devices/test22/diagnostics/refresh").status_code == 404
