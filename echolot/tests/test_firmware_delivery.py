"""Tests for how a built firmware image is handed out.

The in-page flasher is not the only way anyone will ever install this
image, and when it stalls there has to be a way around it. So the same
file is a plain download, and the endpoint answers the questions other
tools ask before fetching.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ECHOLOT_MQTT_EXPORT", "false")
    from app import devices as devices_module

    monkeypatch.setattr(devices_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(devices_module, "DEVICES_DIR", tmp_path / "devices")
    monkeypatch.setattr(devices_module, "INDEX_PATH", tmp_path / "devices.json")

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client, tmp_path


def built_device(client_and_path, size=1024):
    client, root = client_and_path
    response = client.post("/api/devices", json={
        "name": "test1", "friendly_name": "Test", "board": "esp32c6",
        "wifi_ssid": "netz", "wifi_password": "passwort123",
    })
    assert response.status_code == 201
    device_id = response.json()["id"]

    relative = ".esphome/build/test1/.pioenvs/test1/firmware.factory.bin"
    binary = root / "devices" / device_id / relative
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"\xff" * size)

    index = json.loads((root / "devices.json").read_text())
    index[device_id].update(
        status="success", chip_family="ESP32-C6", firmware_bin=relative
    )
    (root / "devices.json").write_text(json.dumps(index))
    return device_id


def test_the_manifest_names_the_chip_and_a_relative_part(client):
    device_id = built_device(client)
    manifest = client[0].get(f"/api/devices/{device_id}/manifest.json").json()
    build = manifest["builds"][0]
    assert build["chipFamily"] == "ESP32-C6"
    # Relative, so it resolves under Home Assistant's Ingress token prefix.
    assert build["parts"] == [{"path": "firmware.bin", "offset": 0}]


def test_the_image_downloads_with_a_length(client):
    device_id = built_device(client, size=4096)
    response = client[0].get(f"/api/devices/{device_id}/firmware.bin")
    assert response.status_code == 200
    assert response.content == b"\xff" * 4096
    assert response.headers["content-length"] == "4096"
    assert response.headers["content-type"] == "application/octet-stream"


def test_head_is_answered_too(client):
    """Flashers other than the built-in one ask for the size first, and a
    405 there reads to them as a broken link."""
    device_id = built_device(client, size=4096)
    response = client[0].head(f"/api/devices/{device_id}/firmware.bin")
    assert response.status_code == 200
    assert response.headers["content-length"] == "4096"


def test_the_size_is_visible_without_downloading(client):
    """So a slow flash can be told apart from a large one."""
    device_id = built_device(client, size=2048)
    listed = client[0].get("/api/devices").json()[0]
    assert listed["id"] == device_id
    assert listed["firmware_size"] == 2048


def test_an_unbuilt_device_offers_neither(client):
    response = client[0].post("/api/devices", json={
        "name": "test2", "board": "esp32c6",
        "wifi_ssid": "netz", "wifi_password": "passwort123",
    })
    device_id = response.json()["id"]
    assert response.json()["firmware_size"] is None
    assert client[0].get(f"/api/devices/{device_id}/manifest.json").status_code == 409
    assert client[0].get(f"/api/devices/{device_id}/firmware.bin").status_code == 409


def test_a_missing_file_is_reported_rather_than_served_empty(client):
    device_id = built_device(client)
    index = json.loads((client[1] / "devices.json").read_text())
    index[device_id]["firmware_bin"] = "weg/firmware.factory.bin"
    (client[1] / "devices.json").write_text(json.dumps(index))
    assert client[0].get(f"/api/devices/{device_id}/firmware.bin").status_code == 404
