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
