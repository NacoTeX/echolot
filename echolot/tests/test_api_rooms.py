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
    from app.main import ASSET_PREFIX

    c, _ = client
    html = c.get("/").text
    files = re.findall(r'(?:src|href)="' + re.escape(ASSET_PREFIX) + r'/([^"?]+)', html)
    assert "style.css" in files and "app.js" in files
    for src in files:
        assert (STATIC / src).exists(), src
        assert c.get(f"/{ASSET_PREFIX}/{src}").status_code == 200, src
    for gone in ("calibration.js", "dashboard.js", "zones.js", "dashboard_live.js"):
        assert gone not in html


def test_every_file_comes_from_a_path_that_names_the_version(client):
    """Home Assistant's service worker answered `static/style.css?v=1.1.1`
    with the stylesheet of 0.x: it ignores the query for some paths. The
    version therefore sits in the path, and no file loads from
    `static/` any more."""
    from app import builder

    c, _ = client
    r = c.get("/")
    assert r.headers["cache-control"] == "no-cache"
    version = builder.addon_version()
    refs = re.findall(r'(?:src|href)="([^"#:]+\.(?:css|js|svg))"', r.text)
    assert refs and all(ref.startswith(f"assets/{version}/") for ref in refs), refs
    assert '="static/' not in r.text
    # Every stylesheet and script reports a failed load to the diagnosis.
    code = [ref for ref in refs if ref.endswith((".css", ".js"))]
    assert r.text.count('onerror="echolotAssetFailed(this)"') == len(code)
    served = c.get(f"/assets/{version}/style.css")
    assert served.status_code == 200 and served.headers["content-type"].startswith("text/css")
    assert "--echolot-css: 1" in served.text


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


# --- calibration -------------------------------------------------------------


def room_with_sensor(c):
    device = new_device(c)
    room = c.post("/api/rooms", json={"name": "Wohnzimmer", "width": 6, "height": 4,
                                       "device_id": device["id"]}).json()
    return device, room


def test_a_new_room_filters_by_default(client):
    c, _ = client
    _, room = room_with_sensor(c)
    assert room["calibration"]["confirm_s"] == 1.0
    assert room["calibration"]["smoothing"] == "normal"
    assert room["calibration"]["interference"] == []


def test_filter_settings_are_saved_and_move_the_revision_on(client):
    c, _ = client
    _, room = room_with_sensor(c)
    r = c.put(f"/api/rooms/{room['id']}/calibration", json={"filter": {"confirm_s": 2.5, "smoothing": "strong"}})
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["calibration"]["confirm_s"] == 2.5 and saved["calibration"]["smoothing"] == "strong"
    assert saved["revision"] == room["revision"] + 1
    # An editor still holding the old copy is refused, not allowed to
    # save the old settings back.
    assert c.put(f"/api/rooms/{room['id']}", json=room).status_code == 409
    bad = c.put(f"/api/rooms/{room['id']}/calibration", json={"filter": {"smoothing": "max"}})
    assert bad.status_code == 422


def test_the_editor_cannot_overwrite_a_calibration(client):
    c, _ = client
    _, room = room_with_sensor(c)
    room = c.put(f"/api/rooms/{room['id']}/calibration", json={"filter": {"confirm_s": 3}}).json()
    room["calibration"]["confirm_s"] = 0
    room["name"] = "Stube"
    saved = c.put(f"/api/rooms/{room['id']}", json=room).json()
    assert saved["name"] == "Stube" and saved["calibration"]["confirm_s"] == 3


def test_an_alignment_is_proposed_then_applied(client):
    c, _ = client
    _, room = room_with_sensor(c)
    # The sensor is drawn at (3, 0) looking down; someone at (3, 2) was
    # reported 2 m ahead and 0.5 m off to the side -> turn it.
    proposal = c.post(f"/api/rooms/{room['id']}/alignment",
                      json={"points": [{"raw": [0.5, 2.0], "ref": [3.0, 2.0]}]})
    assert proposal.status_code == 200, proposal.text
    body = proposal.json()
    assert body["mode"] == "direction" and body["turn_deg"] != 0
    assert c.get(f"/api/rooms/{room['id']}").json()["sensor"]["angle"] == 0  # nothing saved yet
    applied = c.put(f"/api/rooms/{room['id']}/calibration", json={"alignment": {
        "x": body["x"], "y": body["y"], "angle": body["angle"], "mirror": body["mirror"],
        "report": body}})
    assert applied.status_code == 200, applied.text
    saved = applied.json()
    assert saved["sensor"]["angle"] == body["angle"]
    assert saved["sensor"]["device_id"] == room["sensor"]["device_id"]
    assert saved["calibration"]["alignment_points"] == 1


def test_an_alignment_cannot_move_the_sensor_off_the_plan_or_swap_it(client):
    c, _ = client
    _, room = room_with_sensor(c)
    off = c.put(f"/api/rooms/{room['id']}/calibration",
                json={"alignment": {"x": 9, "y": 0, "angle": 0, "mirror": False}})
    assert off.status_code == 422
    assert c.post(f"/api/rooms/{room['id']}/alignment", json={"points": []}).status_code == 422
    assert c.post(f"/api/rooms/{room['id']}/alignment", json={"points": [{"raw": [1]}]}).status_code == 422


def test_interference_spots_belong_to_the_sensor_they_were_learned_with(client):
    c, _ = client
    device, room = room_with_sensor(c)
    spots = [{"x": 1.0, "y": 1.5, "r": 0.4, "share": 0.6}]
    wrong = c.put(f"/api/rooms/{room['id']}/calibration",
                  json={"interference": {"spots": spots, "device_id": "someone-else"}})
    assert wrong.status_code == 409
    ok = c.put(f"/api/rooms/{room['id']}/calibration",
               json={"interference": {"spots": spots, "device_id": device["id"]}})
    assert ok.status_code == 200, ok.text
    cal = ok.json()["calibration"]
    assert cal["interference"][0]["x"] == 1.0 and cal["interference_device_id"] == device["id"]
    live = next(r for r in c.get("/api/live").json()["rooms"] if r["room_id"] == room["id"])
    assert live["filter"]["interference_spots"] == 1
    cleared = c.put(f"/api/rooms/{room['id']}/calibration", json={"interference": None}).json()
    assert cleared["calibration"]["interference"] == []
    assert c.put(f"/api/rooms/{room['id']}/calibration", json={"interference": "x"}).status_code == 422
    assert c.put(f"/api/rooms/{room['id']}/calibration", json={}).status_code == 422


def test_a_capture_starts_reports_and_can_be_cancelled(client):
    c, _ = client
    _, room = room_with_sensor(c)
    r = c.post(f"/api/rooms/{room['id']}/capture", json={"kind": "empty", "delay_s": 20, "duration_s": 30})
    assert r.status_code == 201, r.text
    assert r.json()["phase"] == "waiting" and r.json()["sensor_fresh"] is False
    assert c.get(f"/api/rooms/{room['id']}/capture").json()["kind"] == "empty"
    assert c.delete(f"/api/rooms/{room['id']}/capture").status_code == 204
    assert c.get(f"/api/rooms/{room['id']}/capture").status_code == 204  # none running
    assert c.delete(f"/api/rooms/{room['id']}/capture").status_code == 404
    assert c.get("/api/rooms/nope/capture").status_code == 404
    assert c.post(f"/api/rooms/{room['id']}/capture", json={"kind": "empty", "duration_s": 1}).status_code == 422


def test_a_room_without_a_sensor_cannot_be_calibrated(client):
    c, _ = client
    room = c.post("/api/rooms", json={"name": "Flur"}).json()
    r = c.post(f"/api/rooms/{room['id']}/capture", json={"kind": "empty"})
    assert r.status_code == 422 and "kein Sensor" in r.json()["detail"]


def test_live_names_the_measurement_definition(client):
    c, _ = client
    _, room = room_with_sensor(c)
    live = next(r for r in c.get("/api/live").json()["rooms"] if r["room_id"] == room["id"])
    assert live["filter"]["definition_version"] == 4


def test_taking_single_spots_away_keeps_the_learning_date(client):
    c, _ = client
    device, room = room_with_sensor(c)
    spots = [{"x": 1.0, "y": 1.5, "r": 0.4, "share": 0.6}, {"x": -1.0, "y": 2.0, "r": 0.3, "share": 0.2}]
    first = c.put(f"/api/rooms/{room['id']}/calibration",
                  json={"interference": {"spots": spots, "device_id": device["id"]}}).json()
    learned_at = first["calibration"]["interference_learned_at"]
    edited = c.put(f"/api/rooms/{room['id']}/calibration",
                   json={"interference": {"spots": spots[:1], "device_id": device["id"]}}).json()
    assert len(edited["calibration"]["interference"]) == 1
    assert edited["calibration"]["interference_learned_at"] == learned_at
    relearned = c.put(f"/api/rooms/{room['id']}/calibration",
                      json={"interference": {"spots": [{"x": 0.0, "y": 3.0, "r": 0.3, "share": 0.1}],
                                             "device_id": device["id"]}}).json()
    assert relearned["calibration"]["interference_learned_at"] >= learned_at
    assert relearned["calibration"]["interference"][0]["y"] == 3.0



def test_walls_are_saved_through_the_editor_route(client):
    c, _ = client
    room = c.post("/api/rooms", json={"name": "Flur", "width": 6, "height": 4}).json()
    room["outline"] = [[0, 0], [6, 0], [6, 4], [2, 4], [2, 3], [0, 3]]
    saved = c.put(f"/api/rooms/{room['id']}", json=room)
    assert saved.status_code == 200, saved.text
    assert saved.json()["outline"][3] == [2, 4]
    crossed = saved.json()
    crossed["outline"] = [[0, 0], [2, 2], [2, 0], [0, 2]]
    r = c.put(f"/api/rooms/{room['id']}", json=crossed)
    assert r.status_code == 422 and "kreuzen" in r.text


def test_the_sensor_model_is_fitted_applied_and_reset_through_the_routes(client):
    import math

    c, _ = client
    _, room = room_with_sensor(c)
    rid = room["id"]
    # A module that reads distances 12 % long, seen from (3, 0) looking down.
    spots = [(1.0, 1.5), (5.0, 1.5), (3.0, 1.2), (0.8, 3.6), (5.2, 3.6), (3.0, 3.7)]
    points = []
    for qx, qy in spots:
        dx, dy = qx - 3.0, qy
        r, phi = math.hypot(dx, dy) * 1.12, math.atan2(dx, dy)
        points.append({"raw": [r * math.sin(phi), r * math.cos(phi)], "ref": [qx, qy]})
    proposal = c.post(f"/api/rooms/{rid}/alignment", json={"points": points, "mount_height_m": 2.0})
    assert proposal.status_code == 200, proposal.text
    body = proposal.json()
    assert body["range_scale"] == pytest.approx(1.12, abs=0.02) and body["model_changed"]
    assert body["cv_rms_m"] is not None and body["validation_status"] == "unvalidated"
    applied = c.put(f"/api/rooms/{rid}/calibration", json={"alignment": {
        **{k: body[k] for k in ("x", "y", "angle", "mirror", "slant", "range_scale", "range_offset_m", "azimuth_scale")},
        "mount_height_m": 2.0, "report": body}})
    assert applied.status_code == 200, applied.text
    saved = applied.json()
    assert saved["sensor"]["range_scale"] == pytest.approx(1.12, abs=0.02)
    assert saved["sensor"]["mount_height_m"] == 2.0
    assert saved["calibration"]["alignment_model"] == body["model"]
    live = next(r for r in c.get("/api/live").json()["rooms"] if r["room_id"] == rid)
    assert live["filter"]["range_scale"] == saved["sensor"]["range_scale"]
    reset = c.put(f"/api/rooms/{rid}/calibration", json={"reset_model": True}).json()
    assert reset["sensor"]["range_scale"] == 1.0 and reset["sensor"]["x"] == saved["sensor"]["x"]
    assert reset["calibration"]["alignment_model"] is None


def test_heights_are_saved_on_their_own_and_checked(client):
    c, _ = client
    _, room = room_with_sensor(c)
    rid = room["id"]
    saved = c.put(f"/api/rooms/{rid}/calibration", json={"mounting": {"mount_height_m": 2.1, "target_height_m": 0.9}})
    assert saved.status_code == 200 and saved.json()["sensor"]["mount_height_m"] == 2.1
    assert c.put(f"/api/rooms/{rid}/calibration", json={"mounting": {"mount_height_m": 9}}).status_code == 422
    bad = c.post(f"/api/rooms/{rid}/alignment", json={"points": [{"raw": [0, 2], "ref": [3, 2]}], "mount_height_m": -1})
    assert bad.status_code == 422
    # A slant without a height is refused.
    assert c.put(f"/api/rooms/{rid}/calibration", json={"reset_model": True, "mounting": {"mount_height_m": None}}).status_code == 200
    nope = c.put(f"/api/rooms/{rid}/calibration", json={"alignment": {"x": 3, "y": 0, "angle": 0, "mirror": False, "slant": True}})
    assert nope.status_code == 422


def test_standpoints_are_suggested(client):
    c, _ = client
    _, room = room_with_sensor(c)
    r = c.get(f"/api/rooms/{room['id']}/alignment/suggest?count=5")
    assert r.status_code == 200
    assert len(r.json()["points"]) == 5


def test_control_spots_go_through_the_route_and_the_report_is_kept_with_its_basis(client):
    import math

    c, _ = client
    _, room = room_with_sensor(c)
    rid = room["id"]

    def spot(qx, qy, role="fit"):
        dx, dy = qx - 3.0, qy
        return {"raw": [dx, dy], "ref": [qx, qy], "role": role}

    points = [spot(*q) for q in ((1.0, 1.5), (5.0, 1.5), (3.0, 1.2), (0.8, 3.6), (5.2, 3.6))]
    points += [spot(1.6, 2.2, "check"), spot(4.6, 3.1, "check")]
    body = c.post(f"/api/rooms/{rid}/alignment", json={"points": points}).json()
    assert body["points"] == 5 and body["validation_points"] == 2
    assert body["validation_status"] == "validated" and body["quality"] == "good"
    assert c.post(f"/api/rooms/{rid}/alignment", json={"points": [spot(1, 1, "other")]}).status_code == 422
    assert c.post(f"/api/rooms/{rid}/alignment", json={"points": [spot(1, 1, "check")]}).status_code == 422
    saved = c.put(f"/api/rooms/{rid}/calibration", json={"alignment": {
        **{k: body[k] for k in ("x", "y", "angle", "mirror")}, "report": body}}).json()
    report = saved["calibration"]["alignment_report"]
    assert report["validation_status"] == "validated" and report["cv_rms_m"] == body["cv_rms_m"]
    assert saved["alignment_state"] == {"state": "current", "changed": []}
    # A new mounting height: the report no longer describes the room.
    moved = c.put(f"/api/rooms/{rid}/calibration", json={"mounting": {"mount_height_m": 2.3}}).json()
    assert moved["alignment_state"]["state"] == "stale"
    assert "Montagehöhe geändert" in moved["alignment_state"]["changed"]
    assert math.isclose(moved["calibration"]["alignment_report"]["cv_rms_m"], body["cv_rms_m"])


def test_an_alignment_from_1_3_is_not_called_current(client):
    c, _ = client
    _, room = room_with_sensor(c)
    from app import rooms as store

    store.update_calibration(room["id"], calibration={"aligned_at": 1.0, "alignment_check_m": 0.03})
    # update_calibration stamps a basis; a stored 1.3 record has none.
    data = store._read()
    data[0]["calibration"]["alignment_basis"] = None
    store._write(data)
    assert c.get(f"/api/rooms/{room['id']}").json()["alignment_state"]["state"] == "unknown"
