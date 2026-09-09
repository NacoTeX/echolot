"""The /api/fusion/zones routes, which nothing covered before.

The fusion module itself is tested as pure functions in test_fusion.py.
These tests cover the wiring: that the routes reach the canonical sample
stream and the calibration profiles, and that a zone with no data
degrades instead of failing.

Until 0.13.6 the wiring went to the direct telemetry hub specifically, so
a device without ESPectre's Direct HTTP API contributed nothing at all —
and that API is closed to this add-on at the pinned upstream commit (see
app/ha_sampler.py). Fusion now reads whichever transport is canonical.
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

from app import calibration, devices as dev_mod, samples, server, telemetry, zones  # noqa: E402
from app.telemetry import Sample  # noqa: E402
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
    # Both stores are process-wide singletons, so samples fed by one test
    # would otherwise still be in the window for the next one — and the
    # test that matters most here is the one about having no samples.
    telemetry.hub._samples.clear()
    telemetry.hub._status.clear()
    for device_id in ("a", "b"):
        samples.bus.forget(device_id)
    samples.bus._refused.clear()
    return TestClient(server.app)


def feed(device_id: str, score: float, threshold: float = 1.0) -> None:
    """One reading from Home Assistant, the canonical source."""
    samples.bus.publish(
        device_id,
        Sample(t=time.time(), movement_score=score, threshold=threshold,
               motion=score > threshold),
        source=samples.SOURCE_HOME_ASSISTANT,
    )


def feed_direct(device_id: str, score: float, threshold: float = 1.0) -> None:
    """Hand the hub a frame the way a device's SSE stream would.

    ingest() takes the raw JSON frame, not a parsed dict — the parsing is
    part of what it does — and it offers the result to the bus.
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


def test_home_assistant_data_alone_is_enough_for_fusion(api):
    """The regression: fusion read the direct hub only, so a device
    without the Direct HTTP API was permanently 'no_recent_sample' even
    while Home Assistant was delivering its readings."""
    feed("a", 5.0)
    feed("b", 4.0)
    body = api.get("/api/fusion/zones").json()[0]
    assert body["available"] is True
    assert body["source"] == samples.SOURCE_HOME_ASSISTANT
    assert all(m["basis"] != "no_recent_sample" for m in body["members"])


def test_the_direct_transport_does_not_add_a_second_vote(api):
    """Two readings of the same movement over two transports are one
    measurement delivered twice. The bus refuses the non-canonical one and
    counts the refusal rather than hiding it."""
    feed("a", 5.0)
    before = api.get("/api/fusion/zones").json()[0]

    for _ in range(5):
        feed_direct("a", 5.0)
    after = api.get("/api/fusion/zones").json()[0]

    assert after["confidence"] == before["confidence"]
    assert samples.bus.status()["refused_by_source"][samples.SOURCE_DIRECT] >= 5


def test_a_zone_without_any_data_degrades_rather_than_failing(api):
    # A zone whose devices never delivered must say so, not answer with a
    # confident vacancy.
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
