"""The learning routes: settings, a room's view, acting on the journal."""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_api_rooms import NeverConnects, new_device  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ECHOLOT_MQTT_EXPORT", "false")
    from app import devices, learner, radar_link, rooms

    monkeypatch.setattr(devices, "DATA_DIR", tmp_path)
    monkeypatch.setattr(devices, "DEVICES_DIR", tmp_path / "devices")
    monkeypatch.setattr(devices, "INDEX_PATH", tmp_path / "devices.json")
    monkeypatch.setattr(rooms, "DATA_DIR", tmp_path)
    monkeypatch.setattr(radar_link.links, "client_factory", NeverConnects)

    from app import main

    # The add-on's one learner: each test starts it afresh.
    monkeypatch.setattr(main.learner, "settings", learner.Settings())
    monkeypatch.setattr(main.learner, "_rooms", {})
    monkeypatch.setattr(main.presence, "entity", None)
    with TestClient(main.app) as test_client:
        yield test_client, main


def room_with_sensor(c):
    device = new_device(c)
    return c.post("/api/rooms", json={"name": "Wohnzimmer", "width": 4, "height": 5, "device_id": device["id"]}).json()


def test_settings_round_trip(client):
    c, main = client
    r = c.get("/api/learning")
    assert r.status_code == 200
    assert r.json()["settings"] == {"mode": "auto", "presence_entity": None}
    r = c.put("/api/learning/settings", json={"mode": "suggest", "presence_entity": "zone.home"})
    assert r.status_code == 200 and r.json()["settings"] == {"mode": "suggest", "presence_entity": "zone.home"}
    # Outside Home Assistant nobody can say who is home.
    assert r.json()["presence"]["home"] is None and r.json()["presence"]["error"]
    assert main.presence.entity == "zone.home"
    assert c.put("/api/learning/settings", json={"mode": "maybe"}).status_code == 422
    assert c.put("/api/learning/settings", json={"presence_entity": "Nicht eine Entität"}).status_code == 422


def test_a_room_with_a_sensor_is_learned(client):
    c, _ = client
    room = room_with_sensor(c)
    view = c.get(f"/api/rooms/{room['id']}/learning").json()
    assert view["active"] and view["mode"] == "auto"
    assert view["journal"][0]["title"] == "Echolot lernt diesen Raum kennen"
    assert view["activity"] == {"cell": 0.25, "cells": []}
    summary = c.get(f"/api/rooms/{room['id']}").json()["learning"]
    assert summary["active"] and summary["walks"] == 0
    assert c.get("/api/learning").json()["rooms"][room["id"]]["active"]


def test_a_room_without_a_sensor_is_not(client):
    c, _ = client
    room = c.post("/api/rooms", json={"name": "Flur"}).json()
    assert c.get(f"/api/rooms/{room['id']}/learning").json()["active"] is False
    assert c.get("/api/rooms/nope/learning").status_code == 404


def test_acting_on_what_is_not_there(client):
    c, _ = client
    room = room_with_sensor(c)
    info = c.get(f"/api/rooms/{room['id']}/learning").json()["journal"][0]
    assert c.post(f"/api/rooms/{room['id']}/learning/nope/accept").status_code == 404
    assert c.post(f"/api/rooms/{room['id']}/learning/{info['id']}/frobnicate").status_code == 404
    # A note is no proposal.
    r = c.post(f"/api/rooms/{room['id']}/learning/{info['id']}/accept")
    assert r.status_code == 409
    r = c.post(f"/api/rooms/{room['id']}/learning/{info['id']}/undo")
    assert r.status_code == 409


def test_analysing_now_and_starting_over(client):
    c, _ = client
    room = room_with_sensor(c)
    r = c.post(f"/api/rooms/{room['id']}/learning/analyse")
    assert r.status_code == 200 and r.json()["analysed_at"]
    assert r.json()["plausibility"]["inside_share"] is None
    assert c.delete(f"/api/rooms/{room['id']}/learning").status_code == 204
    view = c.get(f"/api/rooms/{room['id']}/learning").json()
    assert [e["title"] for e in view["journal"]] == ["Neu begonnen"]


def test_a_deleted_room_takes_its_record_along(client):
    c, main = client
    room = room_with_sensor(c)
    c.get(f"/api/rooms/{room['id']}/learning")
    main.learner.save(force=True)
    path = Path(main.rooms.DATA_DIR) / "learning" / f"{room['id']}.json"
    assert path.exists()
    assert c.delete(f"/api/rooms/{room['id']}").status_code == 204
    assert not path.exists()
