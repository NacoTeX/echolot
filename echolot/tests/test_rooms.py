"""Rooms on disk: what is accepted, and what two editors cannot do to each other."""

import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import rooms  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(rooms, "DATA_DIR", tmp_path)
    return tmp_path


def create(name="Wohnzimmer", device_id=None, **extra):
    return rooms.create_room(rooms.RoomCreate(name=name, device_id=device_id, **extra))


def payload(room, **changes):
    data = room.model_dump()
    data.update(changes)
    return data


ZONE = {"id": "zsofa", "name": "Sofa", "kind": "detect", "points": [[1, 2], [3, 2], [3, 3.5], [1, 3.5]]}


def test_a_new_room_has_its_sensor_on_the_top_wall():
    room = create(width=6, height=4)
    assert (room.sensor.x, room.sensor.y, room.sensor.angle) == (3, 0, 0)
    assert room.revision == 1


def test_saving_bumps_the_revision_and_keeps_zones():
    room = create()
    saved = rooms.save_room(room.id, payload(room, zones=[ZONE]))
    assert saved.revision == 2
    assert rooms.get_room(room.id).zones[0].name == "Sofa"


def test_a_save_based_on_an_old_revision_is_refused():
    room = create()
    rooms.save_room(room.id, payload(room, name="Erste"))
    with pytest.raises(rooms.RevisionConflict) as conflict:
        rooms.save_room(room.id, payload(room, name="Zweite"))
    assert conflict.value.current.name == "Erste"
    assert rooms.get_room(room.id).name == "Erste"


@pytest.mark.parametrize("zone, message", [
    ({**ZONE, "points": [[1, 1], [2, 2]]}, "3 bis"),
    ({**ZONE, "points": [[1, 1], [1.1, 1], [1.1, 1.1]]}, "20 × 20"),
    ({**ZONE, "points": [[1, 1], [9, 1], [9, 3]]}, "außerhalb"),
    ({**ZONE, "kind": "somewhere"}, "kind"),
])
def test_bad_zones_are_refused(zone, message):
    room = create(width=5, height=4)
    with pytest.raises(ValueError, match=message):
        rooms.save_room(room.id, payload(room, zones=[zone]))


def test_two_zones_need_two_names():
    room = create()
    with pytest.raises(ValueError, match="verschiedene Namen"):
        rooms.save_room(room.id, payload(room, zones=[ZONE, {**ZONE, "id": "z2", "name": "sofa"}]))


def test_ids_are_unique_across_zones_and_furniture():
    room = create()
    furniture = {"id": "zsofa", "kind": "sofa", "x": 1, "y": 1, "w": 2, "h": 1}
    with pytest.raises(ValueError, match="doppelte"):
        rooms.save_room(room.id, payload(room, zones=[ZONE], furniture=[furniture]))


def test_a_sensor_stands_in_one_room_only():
    create("A", device_id="dev-1")
    with pytest.raises(ValueError, match="schon im Raum"):
        create("B", device_id="dev-1")
    other = create("C")
    with pytest.raises(ValueError, match="schon im Raum"):
        rooms.save_room(other.id, payload(other, sensor={**other.sensor.model_dump(), "device_id": "dev-1"}))


def test_a_deleted_device_leaves_its_room():
    room = create(device_id="dev-1")
    rooms.release_device("dev-1")
    after = rooms.get_room(room.id)
    assert after.sensor.device_id is None
    assert after.revision == room.revision + 1


def test_an_editor_cannot_undo_an_upload_it_did_not_see():
    room = create()
    stale = payload(room)
    uploaded = rooms.set_image(room.id, PNG, "image/png")
    stale["revision"] = uploaded.revision  # the editor picked up the new revision…
    stale["image"] = None                  # …but still holds no image
    saved = rooms.save_room(room.id, stale)
    assert saved.image is not None and saved.image["file"].endswith(".png")


def test_only_the_opacity_comes_from_the_editor():
    room = create()
    uploaded = rooms.set_image(room.id, PNG, "image/png")
    saved = rooms.save_room(room.id, payload(uploaded, image={"opacity": 0.3, "file": "../../etc/passwd"}))
    assert saved.image["opacity"] == 0.3
    assert saved.image["file"] == f"{room.id}.png"


def test_images_are_checked_by_content():
    room = create()
    with pytest.raises(ValueError, match="kein Bild"):
        rooms.set_image(room.id, b"GIF89a....", "image/png")
    with pytest.raises(ValueError, match="PNG, JPEG oder WebP"):
        rooms.set_image(room.id, PNG, "image/svg+xml")
    with pytest.raises(ValueError, match="höchstens"):
        rooms.set_image(room.id, PNG + b"\0" * rooms.MAX_IMAGE_BYTES, "image/png")


def test_replacing_and_removing_the_image_cleans_up(store):
    room = create()
    rooms.set_image(room.id, PNG, "image/png")
    rooms.set_image(room.id, b"\xff\xd8\xff" + b"\0" * 10, "image/jpeg")
    assert sorted(p.name for p in rooms.image_dir().iterdir()) == [f"{room.id}.jpg"]
    rooms.clear_image(room.id)
    assert list(rooms.image_dir().iterdir()) == []


def test_deleting_a_room_deletes_its_image():
    room = create()
    rooms.set_image(room.id, PNG, "image/png")
    assert rooms.delete_room(room.id)
    assert list(rooms.image_dir().iterdir()) == []
    assert rooms.get_room(room.id) is None


def test_a_room_saved_by_1_0_loads_with_the_filter_defaults_and_its_ids(store):
    import json

    stored = {"version": 1, "rooms": [{
        "id": "r1a2b3c4", "name": "Wohnzimmer", "icon": "living", "width": 5, "height": 4,
        "sensor": {"device_id": "dev-1", "x": 2.5, "y": 0, "angle": 0, "mirror": False},
        "zones": [ZONE], "furniture": [], "hold_s": 10, "edge_margin_m": 0.3,
        "image": None, "revision": 7, "created_at": 1.0, "updated_at": 2.0,
    }]}
    (store / "rooms.json").write_text(json.dumps(stored))
    (room,) = rooms.list_rooms()
    assert room.id == "r1a2b3c4" and room.zones[0].id == "zsofa" and room.revision == 7
    assert room.calibration.confirm_s == 1.0 and room.calibration.smoothing == "normal"
    assert rooms.active_interference(room) == []


def test_a_calibration_update_keeps_the_rest_of_the_room(store):
    base = create(device_id="dev")
    room = rooms.save_room(base.id, payload(base, zones=[ZONE]))
    updated = rooms.update_calibration(room.id, sensor={"x": 1.0, "angle": 12.5},
                                       calibration={"confirm_s": 2.0})
    assert updated.revision == room.revision + 1
    assert (updated.sensor.x, updated.sensor.angle, updated.sensor.device_id) == (1.0, 12.5, "dev")
    assert updated.zones[0].id == "zsofa" and updated.calibration.confirm_s == 2.0
    with pytest.raises(ValueError):
        rooms.update_calibration(room.id, sensor={"device_id": "other"})
    assert rooms.update_calibration("nope", calibration={"confirm_s": 1}) is None


NOTCHED = [[0, 0], [6, 0], [6, 4], [2, 4], [2, 3], [0, 3]]


def test_walls_can_follow_the_room_and_are_kept(store):
    base = create(width=6, height=4)
    saved = rooms.save_room(base.id, payload(base, outline=NOTCHED))
    assert saved.outline == [tuple(p) for p in NOTCHED]
    assert rooms.get_room(base.id).outline == saved.outline
    back = rooms.save_room(base.id, payload(saved, outline=None))
    assert back.outline is None


@pytest.mark.parametrize("outline, message", [
    ([[0, 0], [6, 0]], "3 bis"),
    ([[0, 0], [7, 0], [6, 4], [0, 4]], "außerhalb"),
    ([[0, 0], [2, 2], [2, 0], [0, 2]], "kreuzen"),
    ([[0, 0], [0.5, 0], [0.5, 0.5], [0, 0.5]], "1 m²"),
])
def test_bad_walls_are_refused(store, outline, message):
    base = create(width=6, height=4)
    with pytest.raises(ValueError, match=message):
        rooms.save_room(base.id, payload(base, outline=outline))


SOFA = {"id": "fsofa", "kind": "sofa", "name": "Sofa", "x": 1, "y": 2.5, "w": 2.2, "h": 0.9, "angle": 0}


def sofa_zone(**extra):
    return {"id": "zsofa", "name": "Sofa", "kind": "detect", "furniture_id": "fsofa", "margin_m": 0.2,
            "points": [[0, 0], [1, 0], [1, 1]], **extra}


def test_a_furniture_zone_takes_its_corners_from_the_item_not_the_payload(store):
    base = create(width=6, height=4)
    saved = rooms.save_room(base.id, payload(base, furniture=[SOFA], zones=[sofa_zone()]))
    assert saved.zones[0].points == [(0.8, 2.3), (3.4, 2.3), (3.4, 3.6), (0.8, 3.6)]
    # The sofa moves and turns; the zone follows, keeping its id.
    moved = rooms.save_room(base.id, payload(saved, furniture=[{**SOFA, "x": 3, "angle": 90}], zones=[sofa_zone()]))
    zone = moved.zones[0]
    assert zone.id == "zsofa" and zone.points != saved.zones[0].points
    assert min(x for x, _ in zone.points) > 3


def test_a_furniture_zone_against_the_wall_is_cut_to_the_plan(store):
    base = create(width=6, height=4)
    saved = rooms.save_room(base.id, payload(base, furniture=[{**SOFA, "x": 0, "y": 3.1}], zones=[sofa_zone()]))
    xs = [x for x, _ in saved.zones[0].points]
    ys = [y for _, y in saved.zones[0].points]
    assert min(xs) == 0 and max(ys) == 4


def test_a_furniture_zone_needs_its_furniture_and_only_one_per_item(store):
    base = create(width=6, height=4)
    with pytest.raises(ValueError, match="nicht mehr gibt"):
        rooms.save_room(base.id, payload(base, zones=[sofa_zone()]))
    with pytest.raises(ValueError, match="schon eine Zone"):
        rooms.save_room(base.id, payload(base, furniture=[SOFA],
                                         zones=[sofa_zone(), sofa_zone(id="z2", name="Sofa 2")]))
