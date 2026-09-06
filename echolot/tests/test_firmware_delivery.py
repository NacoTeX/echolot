"""Regression tests for firmware delivery through Home Assistant Ingress."""

import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main  # noqa: E402
from app.devices import BuildStatus, Device, DeviceCreate  # noqa: E402


def built_device(tmp_path: Path) -> Device:
    firmware = tmp_path / "firmware.factory.bin"
    firmware.write_bytes(b"factory-image")
    return Device(
        id="probe",
        created_at=1,
        updated_at=2,
        status=BuildStatus.SUCCESS,
        firmware_bin=firmware.name,
        chip_family="ESP32-C6",
        config=DeviceCreate(
            name="probe",
            board="esp32c6",
            wifi_ssid="netz",
            wifi_password="passwort123",
        ),
    )


def test_manifest_and_firmware_are_not_cached(monkeypatch, tmp_path):
    device = built_device(tmp_path)
    monkeypatch.setattr(main.devices, "get_device", lambda _id: device)
    monkeypatch.setattr(main.devices, "device_dir", lambda _id: tmp_path)

    manifest = main.api_device_manifest(device.id)
    firmware = main.api_device_firmware(device.id)

    assert manifest.headers["cache-control"] == "no-store"
    assert firmware.headers["cache-control"] == "no-store"
    assert firmware.headers["content-length"] == str(len(b"factory-image"))
    assert firmware.headers["content-disposition"] == 'inline; filename="firmware.bin"'
    assert firmware.body == b"factory-image"


def test_unreadable_firmware_becomes_an_http_error(monkeypatch, tmp_path):
    device = built_device(tmp_path)
    monkeypatch.setattr(main.devices, "get_device", lambda _id: device)
    monkeypatch.setattr(main.devices, "device_dir", lambda _id: tmp_path)
    monkeypatch.setattr(Path, "read_bytes", lambda _path: (_ for _ in ()).throw(OSError("kaputt")))

    with pytest.raises(main.HTTPException) as caught:
        main.api_device_firmware(device.id)

    assert caught.value.status_code == 500
    assert "gelesen" in caught.value.detail
