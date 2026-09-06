"""Tests for how device state is read from Home Assistant.

Two properties matter here and neither is obvious from the code:

  * Reading state uses targeted entity lookups, never /api/states. That
    endpoint returns every entity in the installation — fine once, for
    discovery; ruinous on a ten-second timer in a house with thousands of
    entities. An earlier version did exactly that, because one request
    looked cheaper than fifteen. It is not, once you count the bytes.
  * Those targeted reads go out concurrently, which is what makes them
    affordable: a five-device zone costs one round-trip of latency, not
    fifteen.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import ha_client, main  # noqa: E402
from app.devices import Device, DeviceCreate  # noqa: E402
from app.zones import Zone  # noqa: E402


def make_device(name="flur"):
    device = Device(
        id=name,
        created_at=0,
        updated_at=0,
        config=DeviceCreate(
            name=name, board="esp32c6", wifi_ssid="netz", wifi_password="passwort123"
        ),
        entity_motion=f"binary_sensor.{name}_motion_detected",
        entity_movement_score=f"sensor.{name}_movement_score",
        entity_threshold=f"number.{name}_threshold",
    )
    return device


def states_for(device, motion="on", score="4.2"):
    return [
        {"entity_id": device.entity_motion, "state": motion,
         "attributes": {"friendly_name": f"{device.config.name} Motion Detected"}},
        {"entity_id": device.entity_movement_score, "state": score,
         "attributes": {"friendly_name": f"{device.config.name} Movement Score"}},
        {"entity_id": device.entity_threshold, "state": "2.0",
         "attributes": {"friendly_name": f"{device.config.name} Threshold"}},
    ]


@pytest.fixture
def no_persistence(monkeypatch):
    """Reading state must never need to write; keep the fixtures in memory."""
    monkeypatch.setattr("app.devices.save_device", lambda device: None)
    monkeypatch.setattr("app.main.devices.save_device", lambda device: None)


def test_reading_a_zone_never_pulls_every_entity(monkeypatch, no_persistence):
    """/api/states is for discovery. On a timer it is megabytes per tick."""
    devices_ = [make_device(f"dev{i}") for i in range(5)]
    all_states = [s for d in devices_ for s in states_for(d)]
    calls = {"list": 0, "single": 0}

    async def fake_list():
        calls["list"] += 1
        return all_states

    async def fake_get(entity_id):
        calls["single"] += 1
        return next((s for s in all_states if s["entity_id"] == entity_id), None)

    monkeypatch.setattr(ha_client, "list_states", fake_list)
    monkeypatch.setattr(ha_client, "get_state", fake_get)
    monkeypatch.setattr(main.devices, "get_device", lambda i: next(d for d in devices_ if d.id == i))

    zone = Zone(id="z", created_at=0, updated_at=0, name="Zone",
                device_ids=[d.id for d in devices_])
    result = asyncio.run(main.compute_zone_state(zone))

    assert calls["list"] == 0, "steady-state reads must not call /api/states"
    assert calls["single"] == 15, "three targeted reads per device"
    assert result["occupied"] is True
    assert len(result["members"]) == 5


def test_a_zones_reads_are_issued_concurrently(monkeypatch, no_persistence):
    """Fifteen sequential round-trips would make a five-device zone slow
    enough to matter on every tick."""
    devices_ = [make_device(f"dev{i}") for i in range(5)]
    all_states = {s["entity_id"]: s for d in devices_ for s in states_for(d)}
    in_flight = 0
    peak = 0

    async def fake_get(entity_id):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0)  # yield, so overlap is observable
            return all_states.get(entity_id)
        finally:
            in_flight -= 1

    monkeypatch.setattr(ha_client, "get_state", fake_get)
    monkeypatch.setattr(main.devices, "get_device", lambda i: next(d for d in devices_ if d.id == i))

    zone = Zone(id="z", created_at=0, updated_at=0, name="Zone",
                device_ids=[d.id for d in devices_])
    asyncio.run(main.compute_zone_state(zone))

    assert peak > 1, "reads ran one after another"


def test_a_deleted_device_does_not_break_its_zone(monkeypatch, no_persistence):
    """A zone can outlive one of its members."""
    monkeypatch.setattr(main.devices, "get_device", lambda i: None)
    zone = Zone(id="z", created_at=0, updated_at=0, name="Zone", device_ids=["weg"])
    result = asyncio.run(main.compute_zone_state(zone))
    assert result["available"] is False
    assert result["members"][0]["available"] is False


def test_a_single_device_card_does_not_pull_every_state(monkeypatch, no_persistence):
    """The same rule as for zones, at the smallest scale."""
    device = make_device()
    entities = {s["entity_id"]: s for s in states_for(device)}
    calls = {"list": 0, "single": 0}

    async def fake_list():
        calls["list"] += 1
        return list(entities.values())

    async def fake_get(entity_id):
        calls["single"] += 1
        return entities.get(entity_id)

    monkeypatch.setattr(ha_client, "list_states", fake_list)
    monkeypatch.setattr(ha_client, "get_state", fake_get)

    result = asyncio.run(main._read_device_state(device))

    assert calls["list"] == 0
    assert calls["single"] == 3
    assert result["available"] is True
    assert result["movement_score"] == 4.2


def test_a_wrong_entity_id_is_still_relearned(monkeypatch, no_persistence):
    """Self-healing from 0.10.2 must survive every change to how state is
    read — and this is the one place /api/states is still the right call."""
    device = make_device()
    real = states_for(device)
    device.entity_motion = "binary_sensor.falscher_name"
    by_id = {s["entity_id"]: s for s in real}

    async def fake_list():
        return real

    async def fake_get(entity_id):
        return by_id.get(entity_id)

    monkeypatch.setattr(ha_client, "list_states", fake_list)
    monkeypatch.setattr(ha_client, "get_state", fake_get)
    monkeypatch.setattr(main.devices, "get_device", lambda i: device)

    zone = Zone(id="z", created_at=0, updated_at=0, name="Zone", device_ids=[device.id])
    result = asyncio.run(main.compute_zone_state(zone))

    assert device.entity_motion == "binary_sensor.flur_motion_detected"
    assert result["occupied"] is True
