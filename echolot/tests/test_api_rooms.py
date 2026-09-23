"""The HTTP surface the room pages use, through the real stack."""

import json
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


class NeverConnects:
    """The live link without a network: every attempt fails quietly."""

    def __init__(self, *args):
        pass

    async def connect(self, **kwargs):
        raise OSError("no network in tests")

    async def disconnect(self, force=False):
        pass


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ECHOLOT_MQTT_EXPORT", "false")
    from app import devices, radar_link, rooms

    monkeypatch.setattr(devices, "DATA_DIR", tmp_path)
    monkeypatch.setattr(devices, "DEVICES_DIR", tmp_path / "devices")
    monkeypatch.setattr(devices, "INDEX_PATH", tmp_path / "devices.json")
    monkeypatch.setattr(rooms, "DATA_DIR", tmp_path)
    monkeypatch.setattr(radar_link.links, "client_factory", NeverConnects)

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client, tmp_path


def new_device(c, name="radar"):
    r = c.post("/api/devices", json={"name": name, "board": "esp32c5", "wifi_ssid": "netz"})
    assert r.status_code == 201, r.text
    return r.json()


def test_a_room_round_trip(client):
    c, _ = client
    device = new_device(c)
    room = c.post("/api/rooms", json={"name": "Wohnzimmer", "icon": "living", "width": 6, "height": 4,
                                       "device_id": device["id"]}).json()
    assert room["sensor"]["device_id"] == device["id"]
    room["zones"] = [{"id": "zsofa", "name": "Sofa", "kind": "detect", "points": [[1, 2], [3, 2], [3, 3], [1, 3]]}]
    saved = c.put(f"/api/rooms/{room['id']}", json=room)
    assert saved.status_code == 200 and saved.json()["revision"] == room["revision"] + 1
    assert [r["name"] for r in c.get("/api/rooms").json()] == ["Wohnzimmer"]
    assert c.get("/api/devices").json()[0]["room"] == {"id": room["id"], "name": "Wohnzimmer"}


def test_a_stale_save_gets_409_with_the_current_room(client):
    c, _ = client
    room = c.post("/api/rooms", json={"name": "Küche"}).json()
    assert c.put(f"/api/rooms/{room['id']}", json={**room, "name": "Küche 2"}).status_code == 200
    conflict = c.put(f"/api/rooms/{room['id']}", json={**room, "name": "Küche 3"})
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["room"]["name"] == "Küche 2"


def test_an_unknown_sensor_is_refused(client):
    c, _ = client
    assert c.post("/api/rooms", json={"name": "A", "device_id": "nope"}).status_code == 422
    room = c.post("/api/rooms", json={"name": "B"}).json()
    room["sensor"]["device_id"] = "nope"
    assert c.put(f"/api/rooms/{room['id']}", json=room).status_code == 422


def test_the_floor_plan_is_uploaded_raw_and_served_back(client):
    c, _ = client
    room = c.post("/api/rooms", json={"name": "Bad"}).json()
    r = c.put(f"/api/rooms/{room['id']}/image", content=PNG, headers={"Content-Type": "image/png"})
    assert r.status_code == 200
    url = r.json()["image"]["url"]
    assert url.startswith(f"api/rooms/{room['id']}/image?v=")
    served = c.get("/" + url)
    assert served.status_code == 200 and served.content == PNG
    assert served.headers["content-type"] == "image/png"
    assert c.put(f"/api/rooms/{room['id']}/image", content=b"<svg/>", headers={"Content-Type": "image/svg+xml"}).status_code == 422
    assert c.delete(f"/api/rooms/{room['id']}/image").json()["image"] is None
    assert c.get("/" + url).status_code == 404


def test_deleting_a_device_empties_its_room(client):
    c, _ = client
    device = new_device(c)
    room = c.post("/api/rooms", json={"name": "Flur", "device_id": device["id"]}).json()
    assert c.delete(f"/api/devices/{device['id']}").status_code == 204
    assert c.get(f"/api/rooms/{room['id']}").json()["sensor"]["device_id"] is None


def test_live_says_why_a_room_has_no_answer(client):
    c, _ = client
    device = new_device(c)
    c.post("/api/rooms", json={"name": "Mit", "device_id": device["id"]})
    c.post("/api/rooms", json={"name": "Ohne"})
    live = {r["reason"] for r in c.get("/api/live").json()["rooms"]}
    assert live == {"offline", "no_sensor"}


def test_a_csi_device_is_converted_through_the_route(client):
    c, root = client
    (root / "devices.json").write_text(json.dumps({"old": {
        "id": "old", "created_at": 1.0, "updated_at": 1.0, "api_encryption_key": "k", "ota_password": "p",
        "config": {"name": "bad", "board": "esp32c3", "wifi_ssid": "netz", "csi_target_pps": 80},
    }}))
    assert [d["id"] for d in c.get("/api/legacy-devices").json()] == ["old"]
    assert c.get("/api/devices").json() == []
    converted = c.post("/api/legacy-devices/old/convert", json={})
    assert converted.status_code == 200
    body = converted.json()
    assert body["id"] == "old" and body["config"]["name"] == "bad"
    assert "api_encryption_key" not in body and "ota_password" not in body
    assert c.get("/api/devices/old/credentials").json()["ota_password"] == "p"
    assert c.post("/api/legacy-devices/old/convert", json={}).status_code == 404


def test_the_page_loads_only_scripts_that_exist(client):
    c, _ = client
    html = c.get("/").text
    for src in re.findall(r'src="static/([^"]+)"', html):
        assert (STATIC / src).exists(), src
    for gone in ("calibration.js", "dashboard.js", "zones.js", "dashboard_live.js"):
        assert gone not in html


def test_every_icon_the_scripts_ask_for_is_defined():
    html = (STATIC / "index.html").read_text()
    defined = set(re.findall(r'<symbol id="i-([a-z0-9-]+)"', html))
    used = set()
    for script in STATIC.glob("*.js"):
        text = script.read_text()
        used |= set(re.findall(r'icon\("([a-z0-9-]+)"', text))
        used |= set(re.findall(r'icon: "([a-z0-9-]+)"', text))
    from app.rooms import ROOM_ICONS
    used |= set(ROOM_ICONS)
    assert used <= defined, used - defined
