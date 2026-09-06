"""The /api/fusion/zones routes, which nothing covered before.

The fusion module itself is tested as pure functions in test_fusion.py.
These tests cover the wiring: that the routes reach the telemetry hub and
the calibration profiles, and that a zone with no direct data degrades
instead of failing.
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import calibration, devices as dev_mod, server, telemetry, zones  # noqa: E402
from app.devices import BuildStatus, Device, DeviceCreate  # noqa: E402


def make_device(device_id: str) -> Device:
    return Device(
        id=device_id,
        created_at=1,
        updated_at=2,
        status=BuildStatus.SUCCESS,
        firmware_bin="firmware.factory.bin",
        chip_family="ESP32-C6",
        config=DeviceCreate(
            name=device_id,
            board="esp32c6",
            wifi_ssid="netz",
            wifi_password="passwort123",
        ),
    )


@pytest.fixture
def api(monkeypatch):
    zone = type("Zone", (), {"id": "wohnzimmer", "name": "Wohnzimmer", "device_ids": ["a", "b"]})()
    monkeypatch.setattr(zones, "list_zones", lambda: [zone])
    monkeypatch.setattr(zones, "get_zone", lambda zid: zone if zid == zone.id else None)
    monkeypatch.setattr(dev_mod, "get_device", lambda did: make_device(did))
    monkeypatch.setattr(calibration.store, "latest_profiles", lambda: {})
    # The hub is a process-wide singleton, so samples fed by one test would
    # otherwise still be in the window for the next one — and the test that
    # matters most here is the one about having no samples at all.
    telemetry.hub._samples.clear()
    telemetry.hub._status.clear()
    return TestClient(server.app)


def feed(device_id: str, score: float, threshold: float = 1.0) -> None:
    """Hand the hub a sample the way a device's SSE stream would.

    ingest() takes the raw JSON frame, not a parsed dict — the parsing is
    part of what it does.
    """
    telemetry.hub.ingest(
        device_id,
        json.dumps(
            {
                "timestamp": time.time(),
                "movement_score": score,
                "threshold": threshold,
                "motion": score > threshold,
            }
        ),
    )


def test_two_agreeing_devices_make_a_confident_zone(api):
    feed("a", 5.0)
    feed("b", 4.0)
    body = api.get("/api/fusion/zones").json()
    assert len(body) == 1
    assert body[0]["zone_id"] == "wohnzimmer"
    assert body[0]["state"] == "occupied"
    assert body[0]["agreement"] > 0.8


def test_a_zone_without_direct_data_degrades_rather_than_failing(api):
    # The fusion needs the device's own SSE stream; a zone whose devices
    # never connected must say so, not answer with a confident vacancy.
    body = api.get("/api/fusion/zones").json()
    assert body[0]["available"] is False
    assert body[0]["occupied"] is None
    # Every member is still named, so the dashboard can say which one is quiet.
    assert {m["device_id"] for m in body[0]["members"]} == {"a", "b"}


def test_a_single_zone_can_be_asked_for(api):
    feed("a", 5.0)
    body = api.get("/api/fusion/zones/wohnzimmer").json()
    assert body["zone_id"] == "wohnzimmer"


def test_an_unknown_zone_is_a_404(api):
    assert api.get("/api/fusion/zones/gibtsnicht").status_code == 404


def test_disagreement_is_reported_rather_than_resolved(api):
    feed("a", 9.0)
    feed("b", 0.01)
    body = api.get("/api/fusion/zones").json()[0]
    assert body["state"] == "uncertain"
    assert body["occupied"] is None
    assert body["agreement"] < 0.5
