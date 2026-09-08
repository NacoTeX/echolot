"""Starting a recording through the API, which is where it broke.

The sampler was first started with asyncio.create_task inside
create_calibration. That route is a plain `def`, so FastAPI runs it in a
worker thread with no running event loop, and the call raised — after the
session had already been created. The result was a recording that looked
live and collected nothing, which is the exact failure this whole feature
exists to prevent. The unit tests missed it because they ran inside
asyncio.run; only a request through the app reproduces it.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import calibration, devices as dev_mod, feature_api, ha_sampler, server  # noqa: E402
from app.devices import BuildStatus, Device, DeviceCreate  # noqa: E402


def make_device(*, with_entities: bool = True) -> Device:
    device = Device(
        id="probe",
        created_at=1,
        updated_at=2,
        status=BuildStatus.SUCCESS,
        config=DeviceCreate(
            name="probe", board="esp32c6", wifi_ssid="netz", wifi_password="passwort123"
        ),
    )
    if with_entities:
        device.entity_movement_score = "sensor.probe_movement_score"
        device.entity_motion = "binary_sensor.probe_motion_detected"
        device.entity_threshold = "number.probe_threshold"
    return device


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(tmp_path))
    store = calibration.CalibrationStore()
    monkeypatch.setattr(calibration, "store", store)
    monkeypatch.setattr(feature_api.calibration, "store", store)

    reads: list[str] = []

    async def reader(entity_id):
        reads.append(entity_id)
        return {"state": "0.4", "last_updated": f"2026-09-08T12:00:{len(reads) % 60:02d}+00:00"}

    sampler = ha_sampler.HomeAssistantSampler(store.ingest, reader)
    monkeypatch.setattr(feature_api, "sampler", sampler)
    monkeypatch.setattr(ha_sampler, "POLL_SECONDS", 0.01)

    device = make_device()
    monkeypatch.setattr(dev_mod, "get_device", lambda did: device if did == "probe" else None)
    monkeypatch.setattr(feature_api.devices, "get_device", dev_mod.get_device)

    with TestClient(server.app) as client:
        yield client, sampler, store, reads


def test_starting_a_recording_through_the_api_actually_samples(api):
    client, sampler, store, reads = api
    response = client.post("/api/calibrations", json={"device_id": "probe"})
    assert response.status_code == 201
    # The regression: this used to raise "no running event loop" in the
    # worker thread and leave the session with nothing behind it.
    assert sampler.running_for("probe") is True

    deadline = 2.0
    while deadline > 0 and not reads:
        client.get("/api/calibrations")
        deadline -= 0.05
    assert reads, "die Aufzeichnung hat Home Assistant nie gelesen"


def test_stopping_a_recording_stops_the_sampling(api):
    client, sampler, store, reads = api
    session = client.post("/api/calibrations", json={"device_id": "probe"}).json()
    assert sampler.running_for("probe") is True
    assert client.post(f"/api/calibrations/{session['id']}/stop").status_code == 200
    assert sampler.running_for("probe") is False


def test_deleting_the_last_recording_stops_the_sampling(api):
    client, sampler, store, reads = api
    session = client.post("/api/calibrations", json={"device_id": "probe"}).json()
    assert client.delete(f"/api/calibrations/{session['id']}").status_code == 204
    assert sampler.running_for("probe") is False


def test_a_device_without_entities_is_refused_with_the_way_out(api, monkeypatch):
    client, sampler, store, reads = api
    bare = make_device(with_entities=False)
    monkeypatch.setattr(feature_api.devices, "get_device", lambda did: bare)

    response = client.post("/api/calibrations", json={"device_id": "probe"})
    assert response.status_code == 409
    assert "Entities in Home Assistant suchen" in response.json()["detail"]
    assert sampler.running_for("probe") is False


def test_direct_api_is_no_longer_a_precondition(api, monkeypatch):
    """The Direct HTTP API is closed to third parties, so requiring it here
    only locked people out of a feature that no longer uses it."""
    client, sampler, store, reads = api
    device = make_device()
    device.config.direct_api = False
    monkeypatch.setattr(feature_api.devices, "get_device", lambda did: device)

    assert client.post("/api/calibrations", json={"device_id": "probe"}).status_code == 201
