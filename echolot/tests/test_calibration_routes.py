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

from app import (  # noqa: E402
    calibration,
    devices as dev_mod,
    feature_api,
    ha_sampler,
    live_presence,
    samples,
    server,
)
from app.devices import BuildStatus, Device, DeviceCreate  # noqa: E402
from app.telemetry import Sample  # noqa: E402


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

    opened: list = []

    class FakeSubscription:
        def __init__(self, entity_ids, on_state, *, loop=None):
            self.entity_ids = list(entity_ids)
            self.on_state = on_state
            self.connected = False
            self.error = None
            opened.append(self)

        def start(self):
            self.connected = True

        def stop(self):
            self.connected = False

    # The live service owns the subscription; a recording attaches to it.
    live = live_presence.LivePresence(subscription_factory=FakeSubscription)
    monkeypatch.setattr(feature_api, "live", live)
    # The sampler is the liveness check; the readings themselves reach the
    # store through the canonical bus, once (app/samples.py).
    sampler = ha_sampler.HomeAssistantSampler(live)
    monkeypatch.setattr(feature_api, "sampler", sampler)
    samples.bus.forget("probe")

    device = make_device()
    monkeypatch.setattr(dev_mod, "get_device", lambda did: device if did == "probe" else None)
    monkeypatch.setattr(feature_api.devices, "get_device", dev_mod.get_device)
    # The lifespan reconciles the live streams from list_devices(), so this
    # has to answer too — otherwise startup closes the stream again.
    monkeypatch.setattr(dev_mod, "list_devices", lambda: [device])

    with TestClient(server.app) as client:
        yield client, sampler, store, opened


def test_starting_a_recording_through_the_api_actually_samples(api):
    client, sampler, store, opened = api
    response = client.post("/api/calibrations", json={"device_id": "probe"})
    assert response.status_code == 201
    # The regression: this used to raise "no running event loop" in the
    # worker thread and leave the session with nothing behind it.
    assert sampler.running_for("probe") is True
    assert opened, "es wurde kein Abonnement geöffnet"
    assert "sensor.probe_movement_score" in opened[-1].entity_ids

    # And a state change from that subscription reaches the session. Note
    # the subscription was opened by the live service at reconcile time,
    # before the recording started — which is the point of it.
    opened[-1].on_state(
        "sensor.probe_movement_score",
        {"state": "0.42", "last_updated": "2026-09-08T12:00:00+00:00"},
    )
    session = client.get("/api/calibrations").json()[0]
    assert session["sample_count"] == 1


def test_stopping_a_recording_stops_the_sampling(api):
    client, sampler, store, opened = api
    session = client.post("/api/calibrations", json={"device_id": "probe"}).json()
    assert sampler.running_for("probe") is True
    assert client.post(f"/api/calibrations/{session['id']}/stop").status_code == 200
    assert sampler.running_for("probe") is False


def test_deleting_the_last_recording_stops_the_sampling(api):
    client, sampler, store, opened = api
    session = client.post("/api/calibrations", json={"device_id": "probe"}).json()
    assert client.delete(f"/api/calibrations/{session['id']}").status_code == 204
    assert sampler.running_for("probe") is False


def test_a_device_without_entities_is_refused_with_the_way_out(api, monkeypatch):
    client, sampler, store, opened = api
    bare = make_device(with_entities=False)
    monkeypatch.setattr(feature_api.devices, "get_device", lambda did: bare)

    response = client.post("/api/calibrations", json={"device_id": "probe"})
    assert response.status_code == 409
    assert "Entities in Home Assistant suchen" in response.json()["detail"]
    assert sampler.running_for("probe") is False


def test_direct_api_is_no_longer_a_precondition(api, monkeypatch):
    """The Direct HTTP API is closed to third parties, so requiring it here
    only locked people out of a feature that no longer uses it."""
    client, sampler, store, opened = api
    device = make_device()
    device.config.direct_api = False
    monkeypatch.setattr(feature_api.devices, "get_device", lambda did: device)
    feature_api.live.reconcile([device])

    assert client.post("/api/calibrations", json={"device_id": "probe"}).status_code == 201


# --- importing history through the API ---------------------------------


def history_states(count: int, *, start: float = 1_757_300_000.0, step: float = 1.0):
    """`count` movement-score states, as Home Assistant hands them back."""
    from datetime import datetime, timezone

    return [
        {
            "state": f"{0.001 * (index % 3):.6f}",
            "last_changed": datetime.fromtimestamp(
                start + index * step, timezone.utc
            ).isoformat(),
        }
        for index in range(count)
    ]


def test_importing_a_range_stores_a_finished_session(api, monkeypatch):
    from app import ha_client

    async def fake_history(entity_id, start, end):
        if entity_id == "sensor.probe_movement_score":
            return history_states(120)
        if entity_id == "number.probe_threshold":
            # As Home Assistant really answers: the state as it stood at
            # the start of the window, stamped with when it actually last
            # changed — which for a threshold nobody touches is days
            # earlier. Every sample in the window has to inherit it.
            return [{"state": "0.5", "last_changed": "2025-09-01T00:00:00+00:00"}]
        return []

    monkeypatch.setattr(ha_client, "get_history_range", fake_history)

    client, _sampler, _store, _opened = api
    response = client.post(
        "/api/calibrations/import",
        json={
            "device_id": "probe",
            "start": "2026-09-08T04:00:00+00:00",
            "end": "2026-09-08T04:10:00+00:00",
            "label": "empty",
        },
    )
    assert response.status_code == 201, response.text
    session = response.json()
    assert session["status"] == "complete"
    assert session["source"] == "history"
    assert session["label_counts"]["empty"] == 120
    # Carried forward, not left empty — the whole reason merge exists.
    csv = client.get(f"/api/calibrations/{session['id']}/export.csv").text
    rows = [line for line in csv.splitlines()[1:] if line]
    assert len(rows) == 120
    assert all(row.split(",")[2] == "0.5" for row in rows)


def test_a_range_that_is_too_long_is_refused_before_home_assistant_is_asked(api, monkeypatch):
    from app import ha_client

    asked = []

    async def fake_history(entity_id, start, end):
        asked.append(entity_id)
        return []

    monkeypatch.setattr(ha_client, "get_history_range", fake_history)

    client, _sampler, _store, _opened = api
    response = client.post(
        "/api/calibrations/import",
        json={
            "device_id": "probe",
            "start": "2026-09-08T00:00:00+00:00",
            "end": "2026-09-08T09:00:00+00:00",
        },
    )
    assert response.status_code == 422
    assert asked == [], "der Recorder wurde trotz Ablehnung abgefragt"


def test_an_unparseable_moment_is_reported_as_such(api):
    client, _sampler, _store, _opened = api
    response = client.post(
        "/api/calibrations/import",
        json={"device_id": "probe", "start": "gestern abend", "end": "2026-09-08T04:10:00+00:00"},
    )
    assert response.status_code == 422
    assert "start" in response.json()["detail"]


def test_a_recorder_outage_is_reported_as_a_gateway_problem(api, monkeypatch):
    from app import ha_client

    async def boom(entity_id, start, end):
        raise ha_client.HomeAssistantUnavailable("connection refused")

    monkeypatch.setattr(ha_client, "get_history_range", boom)

    client, _sampler, _store, _opened = api
    response = client.post(
        "/api/calibrations/import",
        json={
            "device_id": "probe",
            "start": "2026-09-08T04:00:00+00:00",
            "end": "2026-09-08T04:10:00+00:00",
        },
    )
    assert response.status_code == 502


def test_a_range_with_no_readings_is_refused_rather_than_stored_empty(api, monkeypatch):
    from app import ha_client

    async def nothing(entity_id, start, end):
        return []

    monkeypatch.setattr(ha_client, "get_history_range", nothing)

    client, _sampler, _store, _opened = api
    response = client.post(
        "/api/calibrations/import",
        json={
            "device_id": "probe",
            "start": "2026-09-08T04:00:00+00:00",
            "end": "2026-09-08T04:10:00+00:00",
        },
    )
    assert response.status_code == 409
    assert client.get("/api/calibrations").json() == []


def test_a_recording_that_cannot_sample_is_refused_not_started(api, monkeypatch):
    """From the external review (P1 #5).

    `sampler.start()` returns False when there is no live subscription to
    attach to, and the route ignored it: the session was created, the UI
    showed "recording", and nothing was ever collected. Three sessions
    were once recorded and exported before anyone noticed they were empty.
    """
    client, sampler, store, _opened = api
    monkeypatch.setattr(sampler, "start", lambda device: False)

    response = client.post("/api/calibrations", json={"device_id": "probe"})
    assert response.status_code == 409
    assert client.get("/api/calibrations").json() == [], "eine tote Sitzung blieb zurück"


def test_the_replay_route_compares_strategies(api, monkeypatch):
    """From the external review (step 8). Read-only, and it says whether
    the numbers measured themselves."""
    from app import ha_client

    async def fake_history(entity_id, start, end):
        if entity_id == "sensor.probe_movement_score":
            return history_states(2600, step=0.25)
        return []

    monkeypatch.setattr(ha_client, "get_history_range", fake_history)
    client, _sampler, _store, _opened = api

    created = client.post(
        "/api/calibrations/import",
        json={
            "device_id": "probe",
            "start": "2026-09-08T04:00:00+00:00",
            "end": "2026-09-08T04:12:00+00:00",
            "label": "empty",
        },
    )
    assert created.status_code == 201, created.text

    response = client.get(f"/api/calibrations/{created.json()['id']}/replay")
    assert response.status_code == 200
    body = response.json()

    # Provenance first: without a baseline the numbers measure themselves,
    # and the response has to say so where a reader will see it.
    assert body["identity"]["in_sample"] is True
    assert body["identity"]["session"]["source"] == "history"
    assert body["identity"]["baseline"] is None

    assert body["window_comparison"]["kind"] == "window_comparison"
    assert [s["name"] for s in body["window_comparison"]["strategies"]][:1] == ["rate_15s"]

    # And the chronological run, with a time budget that adds up.
    simulation = body["simulation"]["with_rate"]
    budget = simulation["budget"]
    parts = sum(value for key, value in budget.items() if key != "total_seconds")
    assert abs(parts - budget["total_seconds"]) < 1.0
    assert "motion_only" in body["simulation"]


def test_a_baseline_from_another_device_is_refused(api, monkeypatch):
    """What a room does empty is a fact about that room and that antenna.
    Accepting a foreign device silently would make a transfer experiment
    look like an ordinary out-of-sample comparison."""
    from app import ha_client

    async def fake_history(entity_id, start, end):
        return history_states(300) if entity_id.startswith("sensor.") else []

    monkeypatch.setattr(ha_client, "get_history_range", fake_history)
    client, _sampler, store, _opened = api

    created = client.post(
        "/api/calibrations/import",
        json={"device_id": "probe", "start": "2026-09-08T04:00:00+00:00",
              "end": "2026-09-08T04:05:00+00:00", "label": "empty"},
    ).json()
    # A second session on a different device, built directly: the import
    # route would refuse a device that does not exist.
    other = store.adopt(
        "anderes-geraet",
        [Sample(t=float(i), movement_score=0.1, threshold=0.5, motion=False)
         for i in range(300)],
        label="empty",
    )

    refused = client.get(
        f"/api/calibrations/{created['id']}/replay?baseline={other['id']}"
    )
    assert refused.status_code == 409
    assert "anderen Gerät" in refused.json()["detail"]

    allowed = client.get(
        f"/api/calibrations/{created['id']}/replay?baseline={other['id']}&transfer=true"
    )
    assert allowed.status_code == 200
    assert allowed.json()["identity"]["cross_device"] is True


def test_replaying_an_unknown_session_is_a_404(api):
    client, _sampler, _store, _opened = api
    assert client.get("/api/calibrations/gibtsnicht/replay").status_code == 404


def test_an_unknown_baseline_session_is_a_404(api, monkeypatch):
    from app import ha_client

    async def fake_history(entity_id, start, end):
        if entity_id == "sensor.probe_movement_score":
            return history_states(300)
        return []

    monkeypatch.setattr(ha_client, "get_history_range", fake_history)
    client, _sampler, _store, _opened = api
    created = client.post(
        "/api/calibrations/import",
        json={
            "device_id": "probe",
            "start": "2026-09-08T04:00:00+00:00",
            "end": "2026-09-08T04:05:00+00:00",
            "label": "empty",
        },
    )
    response = client.get(
        f"/api/calibrations/{created.json()['id']}/replay?baseline=gibtsnicht"
    )
    assert response.status_code == 404
