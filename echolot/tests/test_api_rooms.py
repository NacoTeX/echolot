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


def keep(c, rid, points):
    """Hand standpoints in, as a page would have measured them."""
    view = None
    for point in points:
        r = c.post(f"/api/rooms/{rid}/alignment/points", json=point)
        assert r.status_code == 201, r.text
        view = r.json()
    return view


def solve(c, rid, **heights):
    r = c.post(f"/api/rooms/{rid}/alignment", json=heights)
    assert r.status_code == 200, r.text
    return r.json()


def apply(c, rid, proposal):
    return c.post(f"/api/rooms/{rid}/alignment/apply", json={"basis": proposal["basis"]})


def seen_from(qx, qy, sx=3.0, role="fit", scale=1.0):
    """What a module at (sx, 0) looking down the plan reports for (qx, qy)."""
    import math

    dx, dy = qx - sx, qy
    r, phi = math.hypot(dx, dy) * scale, math.atan2(dx, dy)
    return {"raw": [r * math.sin(phi), r * math.cos(phi)], "ref": [qx, qy], "role": role}


SPOTS = ((1.0, 1.5), (5.0, 1.5), (3.0, 1.2), (0.8, 3.6), (5.2, 3.6))


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
    keep(c, room["id"], [{"raw": [0.5, 2.0], "ref": [3.0, 2.0]}])
    body = solve(c, room["id"])
    assert body["mode"] == "direction" and body["turn_deg"] != 0
    assert c.get(f"/api/rooms/{room['id']}").json()["sensor"]["angle"] == 0  # nothing saved yet
    applied = apply(c, room["id"], body)
    assert applied.status_code == 200, applied.text
    saved = applied.json()
    assert saved["sensor"]["angle"] == body["angle"]
    assert saved["sensor"]["device_id"] == room["sensor"]["device_id"]
    cal = saved["calibration"]
    assert cal["alignment_points"] == 1
    # What it was computed from is kept with it; the drawing before it too.
    current, before = cal["alignment_history"]
    assert cal["alignment_id"] == current["id"] and current["origin"] == "alignment"
    assert current["standpoints"][0]["raw"] == [0.5, 2.0] and current["standpoints"][0]["source"] == "api"
    assert current["report"]["fit_rms_m"] == body["fit_rms_m"]
    assert before["origin"] == "before" and before["sensor"]["angle"] == 0


def test_an_alignment_cannot_move_the_sensor_off_the_plan_or_swap_it(client):
    c, _ = client
    _, room = room_with_sensor(c)
    # Values from a page are not taken: only what the server computes.
    off = c.put(f"/api/rooms/{room['id']}/calibration",
                json={"alignment": {"x": 9, "y": 0, "angle": 0, "mirror": False}, "filter": {"confirm_s": 2}})
    assert off.status_code == 422 and "alignment/apply" in off.json()["detail"]
    assert c.get(f"/api/rooms/{room['id']}").json()["calibration"]["confirm_s"] == 1.0
    assert c.post(f"/api/rooms/{room['id']}/alignment/apply", json={}).status_code == 422
    assert c.post(f"/api/rooms/{room['id']}/alignment", json={"points": []}).status_code == 422
    assert c.post(f"/api/rooms/{room['id']}/alignment", json={"points": [{"raw": [1]}]}).status_code == 422
    assert c.post(f"/api/rooms/{room['id']}/alignment", json={}).status_code == 422  # none kept
    bad = c.post(f"/api/rooms/{room['id']}/alignment/points", json={"raw": [0, 1], "ref": [1, "x"]})
    assert bad.status_code == 422


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
    assert live["filter"]["definition_version"] == 5


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
    keep(c, rid, [seen_from(qx, qy, scale=1.12) for qx, qy in SPOTS + ((3.0, 3.7),)])
    body = solve(c, rid, mount_height_m=2.0)
    assert body["range_scale"] == pytest.approx(1.12, abs=0.02) and body["model_changed"]
    assert body["cv_rms_m"] is not None and body["validation_status"] == "unvalidated"
    applied = apply(c, rid, body)
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
    assert c.put(f"/api/rooms/{rid}/calibration", json={"reset_model": True, "mounting": {"mount_height_m": None}}).status_code == 200


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
    assert c.post(f"/api/rooms/{rid}/alignment", json={"points": points}).json()["validation_status"] == "validated"
    assert c.post(f"/api/rooms/{rid}/alignment", json={"points": [spot(1, 1, "other")]}).status_code == 422
    assert c.post(f"/api/rooms/{rid}/alignment", json={"points": [spot(1, 1, "check")]}).status_code == 422
    keep(c, rid, points)
    body = solve(c, rid)
    assert body["points"] == 5 and body["validation_points"] == 2
    assert body["validation_status"] == "validated" and body["quality"] == "good"
    saved = apply(c, rid, body).json()
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

    # As 1.3 stored it: a date and a number, no record of what for.
    store.update_calibration(room["id"], calibration={"aligned_at": 1.0, "alignment_check_m": 0.03})
    assert c.get(f"/api/rooms/{room['id']}").json()["alignment_state"]["state"] == "unknown"


# --- P0-02: a proposal is bound to what it was computed for ---------------------


def aligned_room(c):
    device, room = room_with_sensor(c)
    keep(c, room["id"], [seen_from(qx, qy, sx=2.7) for qx, qy in SPOTS])
    return device, room


def test_a_stale_proposal_from_a_second_tab_is_refused(client):
    c, _ = client
    _, room = aligned_room(c)
    rid = room["id"]
    tab_a = solve(c, rid)
    # Tab B changes the room in the meantime.
    assert c.put(f"/api/rooms/{rid}/calibration", json={"filter": {"confirm_s": 2}}).status_code == 200
    r = apply(c, rid, tab_a)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["changed"] == ["Raum geändert"]
    assert "neu berechnet" in r.json()["detail"]["message"]
    assert c.get(f"/api/rooms/{rid}").json()["sensor"]["x"] == 3.0  # nothing saved
    # Computed again, it goes through.
    fresh = solve(c, rid)
    assert apply(c, rid, fresh).status_code == 200
    assert c.get(f"/api/rooms/{rid}").json()["sensor"]["x"] == pytest.approx(2.7, abs=0.02)


def test_changed_standpoints_or_heights_after_solving_are_refused(client):
    c, _ = client
    _, room = aligned_room(c)
    rid = room["id"]
    proposal = solve(c, rid)
    keep(c, rid, [seen_from(3.0, 3.7, sx=2.7)])
    r = apply(c, rid, proposal)
    assert r.status_code == 409 and r.json()["detail"]["changed"] == ["Standpunkte geändert"]
    proposal = solve(c, rid)
    c.put(f"/api/rooms/{rid}/calibration", json={"mounting": {"mount_height_m": 2.2}})
    assert apply(c, rid, proposal).status_code == 409


def test_a_sensor_swap_between_solving_and_applying_is_refused_and_invalidates(client):
    c, _ = client
    device, room = aligned_room(c)
    rid = room["id"]
    spots = [{"x": 1.0, "y": 1.5, "r": 0.4, "share": 0.6}]
    c.put(f"/api/rooms/{rid}/calibration", json={"interference": {"spots": spots, "device_id": device["id"]}})
    assert apply(c, rid, solve(c, rid)).status_code == 200
    proposal = solve(c, rid)
    other = new_device(c, "radar-2")
    current = c.get(f"/api/rooms/{rid}").json()
    current["sensor"]["device_id"] = other["id"]
    swapped = c.put(f"/api/rooms/{rid}", json=current)
    assert swapped.status_code == 200, swapped.text
    r = apply(c, rid, proposal)
    assert r.status_code == 409
    assert "anderer Sensor" in r.json()["detail"]["changed"]
    after = c.get(f"/api/rooms/{rid}").json()
    cal = after["calibration"]
    # What was measured with the old one is gone; the drawing stays.
    assert cal["interference"] == [] and cal["alignment_draft"] is None
    assert cal["aligned_at"] is None and cal["alignment_report"] is None and cal["alignment_id"] is None
    assert cal["invalidated_reason"] == "Sensor gewechselt" and cal["mounting_epoch"] == 1
    assert after["sensor"]["x"] == pytest.approx(2.7, abs=0.02)
    assert after["alignment_state"]["state"] == "none"
    # The old sensor's alignments are kept for the record, not restored.
    record = cal["alignment_history"][0]
    r = c.post(f"/api/rooms/{rid}/alignment/restore", json={"id": record["id"], "revision": after["revision"]})
    assert r.status_code == 409


def test_a_plan_correction_keeps_the_measurements_and_a_remount_does_not(client):
    c, _ = client
    device, room = aligned_room(c)
    rid = room["id"]
    spots = [{"x": 1.0, "y": 1.5, "r": 0.4, "share": 0.6}]
    c.put(f"/api/rooms/{rid}/calibration", json={"interference": {"spots": spots, "device_id": device["id"]}})
    keep(c, rid, [seen_from(3.0, 3.7, sx=2.7, scale=1.0)])
    assert apply(c, rid, solve(c, rid, mount_height_m=2.0)).status_code == 200
    # The drawing is corrected: the sensor moved on the plan only.
    current = c.get(f"/api/rooms/{rid}").json()
    current["sensor"]["x"] = 2.5
    corrected = c.put(f"/api/rooms/{rid}", json=current).json()
    cal = corrected["calibration"]
    assert len(cal["interference"]) == 1 and len(cal["alignment_draft"]["points"]) == 6
    assert cal["alignment_report"] is not None and cal["mounting_epoch"] == 0
    assert corrected["alignment_state"] == {"state": "stale", "changed": ["Sensor verschoben"]}
    earlier = solve(c, rid)
    # Hung up anew: the same device, but nothing measured before applies.
    corrected["remounted"] = True
    remounted = c.put(f"/api/rooms/{rid}", json=corrected).json()
    cal = remounted["calibration"]
    assert cal["interference"] == [] and cal["alignment_draft"] is None and cal["alignment_report"] is None
    assert cal["invalidated_reason"] == "Sensor neu montiert" and cal["mounting_epoch"] == 1
    assert remounted["sensor"]["device_id"] == device["id"] and remounted["sensor"]["x"] == 2.5
    assert remounted["sensor"]["mount_height_m"] == 2.0 and remounted["sensor"]["range_scale"] == 1.0
    stale = apply(c, rid, earlier)
    assert stale.status_code == 409 and "Sensor neu montiert" in stale.json()["detail"]["changed"]
    # Spots learned before the remount are refused.
    old = c.put(f"/api/rooms/{rid}/calibration",
                json={"interference": {"spots": spots, "device_id": device["id"], "epoch": 0}})
    assert old.status_code == 409


def test_the_remount_route_checks_the_revision(client):
    c, _ = client
    _, room = aligned_room(c)
    rid = room["id"]
    assert c.post(f"/api/rooms/{rid}/remount", json={"revision": room["revision"] - 1}).status_code == 409
    r = c.post(f"/api/rooms/{rid}/remount", json={"revision": room["revision"]})
    assert r.status_code == 200 and r.json()["calibration"]["mounting_epoch"] == 1
    assert r.json()["calibration"]["alignment_draft"] is None


def test_a_restore_is_traceable_and_keeps_every_id(client):
    c, _ = client
    _, room = aligned_room(c)
    rid = room["id"]
    room = c.get(f"/api/rooms/{rid}").json()
    room["zones"] = [{"id": "zsofa", "name": "Sofa", "kind": "detect", "points": [[1, 2], [3, 2], [3, 3], [1, 3]]}]
    room = c.put(f"/api/rooms/{rid}", json=room).json()
    first = apply(c, rid, solve(c, rid)).json()
    first_id = first["calibration"]["alignment_id"]
    # Measured again with a different result: a second alignment.
    c.delete(f"/api/rooms/{rid}/alignment/points")
    keep(c, rid, [seen_from(qx, qy, sx=2.9) for qx, qy in SPOTS])
    second = apply(c, rid, solve(c, rid)).json()
    assert second["sensor"]["x"] == pytest.approx(2.9, abs=0.02)
    history = second["calibration"]["alignment_history"]
    assert [r["origin"] for r in history] == ["alignment", "alignment", "before"]
    # Back to the first one.
    stale = c.post(f"/api/rooms/{rid}/alignment/restore", json={"id": first_id, "revision": second["revision"] - 1})
    assert stale.status_code == 409
    assert c.post(f"/api/rooms/{rid}/alignment/restore", json={"id": "anope", "revision": second["revision"]}).status_code == 404
    back = c.post(f"/api/rooms/{rid}/alignment/restore", json={"id": first_id, "revision": second["revision"]})
    assert back.status_code == 200, back.text
    back = back.json()
    assert back["sensor"]["x"] == first["sensor"]["x"] and back["sensor"]["angle"] == first["sensor"]["angle"]
    cal = back["calibration"]
    assert cal["alignment_id"] == first_id
    record = next(r for r in cal["alignment_history"] if r["id"] == first_id)
    assert record["restored_at"] is not None and cal["alignment_report"] == record["report"]
    assert back["alignment_state"]["state"] == "current"
    # Nothing else has a new identity; the second alignment is still there.
    assert back["id"] == rid and [z["id"] for z in back["zones"]] == ["zsofa"]
    assert back["sensor"]["device_id"] == room["sensor"]["device_id"]
    assert len(cal["alignment_history"]) == 3
    # And the drawing from before any of it.
    before = next(r for r in cal["alignment_history"] if r["origin"] == "before")
    drawn = c.post(f"/api/rooms/{rid}/alignment/restore", json={"id": before["id"], "revision": back["revision"]}).json()
    assert drawn["sensor"]["x"] == 3.0 and drawn["calibration"]["aligned_at"] is None
    assert drawn["alignment_state"]["state"] == "none"


def test_the_history_is_bounded_and_never_drops_the_current_one(client):
    from app import rooms as store

    c, _ = client
    _, room = aligned_room(c)
    rid = room["id"]
    for i in range(store.MAX_ALIGNMENT_HISTORY + 2):
        c.delete(f"/api/rooms/{rid}/alignment/points")
        keep(c, rid, [seen_from(qx, qy, sx=2.5 + 0.1 * i) for qx, qy in SPOTS])
        assert apply(c, rid, solve(c, rid)).status_code == 200
    cal = c.get(f"/api/rooms/{rid}").json()["calibration"]
    assert len(cal["alignment_history"]) == store.MAX_ALIGNMENT_HISTORY
    assert cal["alignment_history"][0]["id"] == cal["alignment_id"]


def test_standpoints_can_be_dropped_by_id_and_a_gone_one_is_named(client):
    c, _ = client
    _, room = aligned_room(c)
    rid = room["id"]
    points = c.get(f"/api/rooms/{rid}").json()["calibration"]["alignment_draft"]["points"]
    r = c.delete(f"/api/rooms/{rid}/alignment/points/{points[1]['id']}")
    assert r.status_code == 200
    assert [p["id"] for p in r.json()["calibration"]["alignment_draft"]["points"]] == [p["id"] for p in points if p is not points[1]]
    assert c.delete(f"/api/rooms/{rid}/alignment/points/{points[1]['id']}").status_code == 404
    again = c.post(f"/api/rooms/{rid}/alignment/points", json={**seen_from(1, 1), "replace_id": points[1]["id"]})
    assert again.status_code == 404
    # Adding points does not bump the revision: an editor open elsewhere
    # can still save.
    assert r.json()["revision"] == room["revision"]


def test_a_measured_standpoint_is_kept_once_with_its_measurement_and_survives_a_reload(client):
    from app import main

    c, _ = client
    _, room = room_with_sensor(c)
    rid = room["id"]
    r = c.post(f"/api/rooms/{rid}/capture", json={"kind": "point", "delay_s": 0, "duration_s": 2,
                                                  "standpoint": {"ref": [3.0, 2.0], "role": "check"}})
    assert r.status_code == 201, r.text
    cap = main.captures.get(rid)
    # Ten reports of somebody 2 m ahead, and the recording is over.
    cap.reports = [(cap.created + 0.2 * i, ((0.02 * (i % 3), 2.0),)) for i in range(10)]
    cap.created -= 10
    view = c.get(f"/api/rooms/{rid}/capture").json()
    assert view["phase"] == "done" and view["kept"] and view["keep_error"] is None
    c.get(f"/api/rooms/{rid}/capture")  # a second tab polls as well
    # A reload finds it on the server, with what it was measured from.
    (point,) = c.get(f"/api/rooms/{rid}").json()["calibration"]["alignment_draft"]["points"]
    assert point["ref"] == [3.0, 2.0] and point["role"] == "check" and point["source"] == "capture"
    assert point["raw"][1] == pytest.approx(2.0) and point["share"] == 1.0
    assert point["spread_m"] is not None and point["measured_at"] is not None
    # Measured again in place: the same slot, a new measurement.
    c.post(f"/api/rooms/{rid}/capture", json={"kind": "point", "delay_s": 0, "duration_s": 2,
                                              "standpoint": {"ref": [3.0, 2.0], "role": "check",
                                                             "replace_id": point["id"]}})
    cap = main.captures.get(rid)
    cap.reports = [(cap.created, ((0.0, 2.2),))] * 8
    cap.created -= 10
    c.get(f"/api/rooms/{rid}/capture")
    (again,) = c.get(f"/api/rooms/{rid}").json()["calibration"]["alignment_draft"]["points"]
    assert again["raw"][1] == pytest.approx(2.2) and again["id"] != point["id"]


def test_a_standpoint_recorded_across_a_remount_is_not_kept(client):
    from app import main

    c, _ = client
    _, room = room_with_sensor(c)
    rid = room["id"]
    c.post(f"/api/rooms/{rid}/capture", json={"kind": "point", "delay_s": 0, "duration_s": 2,
                                              "standpoint": {"ref": [3.0, 2.0]}})
    cap = main.captures.get(rid)
    current = c.get(f"/api/rooms/{rid}").json()
    c.put(f"/api/rooms/{rid}", json={**current, "remounted": True})
    cap.reports = [(cap.created, ((0.0, 2.0),))] * 8
    cap.created -= 10
    main.captures._by_room[rid] = cap  # as if it had kept running
    view = c.get(f"/api/rooms/{rid}/capture").json()
    assert not view["kept"] and "neu montiert" in view["keep_error"]
    assert c.get(f"/api/rooms/{rid}").json()["calibration"]["alignment_draft"] is None


def test_a_proposal_from_another_version_of_the_procedure_is_refused(client):
    c, _ = client
    _, room = aligned_room(c)
    proposal = solve(c, room["id"])
    proposal["basis"]["algorithm"] = 1
    r = apply(c, room["id"], proposal)
    assert r.status_code == 409 and r.json()["detail"]["changed"] == ["Rechenverfahren geändert"]


def test_a_standpoint_recorded_across_a_sensor_swap_names_the_swap(client):
    from app import main

    c, _ = client
    _, room = room_with_sensor(c)
    rid = room["id"]
    c.post(f"/api/rooms/{rid}/capture", json={"kind": "point", "delay_s": 0, "duration_s": 2,
                                              "standpoint": {"ref": [3.0, 2.0]}})
    cap = main.captures.get(rid)
    other = new_device(c, "radar-2")
    current = c.get(f"/api/rooms/{rid}").json()
    current["sensor"]["device_id"] = other["id"]
    assert c.put(f"/api/rooms/{rid}", json=current).status_code == 200
    cap.reports = [(cap.created, ((0.0, 2.0),))] * 8
    cap.created -= 10
    view = c.get(f"/api/rooms/{rid}/capture").json()
    assert not view["kept"] and "anderen Sensor" in view["keep_error"]



# --- the module's own mounting ----------------------------------------------


class StubLink:
    """A connected module that reads a change back, or does not."""

    def __init__(self, reads_back=True):
        from app.radar_link import LinkSnapshot

        self.snapshot = LinkSnapshot(device_id="stub", connected=True, mounting_entities=True)
        self.snapshot.mount_mode, self.snapshot.mount_height_m, self.snapshot.mount_angle_deg = "side", 2.2, 30.0
        self.reads_back = reads_back
        self.asked = []

    def write_mounting(self, mode, height_m, angle_deg):
        self.asked.append((mode, height_m, angle_deg))
        if self.reads_back:
            snap = self.snapshot
            snap.mount_mode, snap.mount_height_m, snap.mount_angle_deg = mode, height_m, angle_deg


def with_stub(monkeypatch, device_id, stub):
    from app import main

    monkeypatch.setattr(main.links, "link", lambda d: stub if d == device_id else None)
    monkeypatch.setattr(main.links, "snapshot", lambda d: stub.snapshot if d == device_id else None)


def test_the_module_mounting_is_written_read_back_and_drops_what_was_measured(client, monkeypatch):
    c, _ = client
    device, room = aligned_room(c)
    rid = room["id"]
    spots = [{"x": 1.0, "y": 1.5, "r": 0.4, "share": 0.6}]
    c.put(f"/api/rooms/{rid}/calibration", json={"interference": {"spots": spots, "device_id": device["id"]}})
    assert apply(c, rid, solve(c, rid)).status_code == 200
    stub = StubLink()
    with_stub(monkeypatch, device["id"], stub)
    # The room page shows what the module says, live.
    assert c.get(f"/api/rooms/{rid}").json()["module"] == {
        "connected": True, "entities": True, "mounting": {"mode": "side", "height_m": 2.2, "angle_deg": 30.0}}
    r = c.put(f"/api/devices/{device['id']}/mounting", json={"mode": "side", "height_m": 2.6, "angle_deg": 25})
    assert r.status_code == 200, r.text
    assert r.json()["confirmed"] and r.json()["mounting"] == {"mode": "side", "height_m": 2.6, "angle_deg": 25.0}
    assert stub.asked == [("side", 2.6, 25.0)]
    after = c.get(f"/api/rooms/{rid}").json()
    cal = after["calibration"]
    # Measured with 2.2 m and 30°: none of it describes the room now.
    assert cal["invalidated_reason"] == "Montage im Modul geändert"
    assert cal["interference"] == [] and cal["alignment_id"] is None
    assert cal["module_mounting"] == {"mode": "side", "height_m": 2.6, "angle_deg": 25.0}
    assert after["sensor"]["mount_height_m"] == 2.6


def test_a_mounting_the_module_does_not_confirm_changes_nothing(client, monkeypatch):
    from app import main

    c, _ = client
    device, room = aligned_room(c)
    stub = StubLink(reads_back=False)
    with_stub(monkeypatch, device["id"], stub)
    monkeypatch.setattr(main, "MOUNTING_CONFIRM_S", 0.3)
    r = c.put(f"/api/devices/{device['id']}/mounting", json={"mode": "side", "height_m": 2.6, "angle_deg": 25})
    assert r.status_code == 200 and r.json()["confirmed"] is False
    assert r.json()["mounting"] == {"mode": "side", "height_m": 2.2, "angle_deg": 30.0}
    cal = c.get(f"/api/rooms/{room['id']}").json()["calibration"]
    assert cal["mounting_epoch"] == 0 and cal["module_mounting"]["height_m"] == 2.2


def test_the_mounting_route_checks_what_it_is_given(client, monkeypatch):
    from app.radar_link import MountingError

    c, _ = client
    device, _room = room_with_sensor(c)
    url = f"/api/devices/{device['id']}/mounting"
    assert c.put(url, json={"mode": "side", "height_m": 2.6, "angle_deg": 25}).status_code == 409  # no link
    stub = StubLink()
    with_stub(monkeypatch, device["id"], stub)
    for bad in ({"mode": "diagonal", "height_m": 2.6, "angle_deg": 25}, {"mode": "side", "height_m": 9, "angle_deg": 25},
                {"mode": "side", "height_m": 2.6, "angle_deg": 120}, {"mode": "side"}):
        assert c.put(url, json=bad).status_code == 422, bad

    def old_firmware(*_):
        raise MountingError("Die Firmware … neu bauen und flashen.")

    stub.write_mounting = old_firmware
    r = c.put(url, json={"mode": "side", "height_m": 2.6, "angle_deg": 25})
    assert r.status_code == 409 and "neu bauen" in r.json()["detail"]
    assert stub.asked == []
