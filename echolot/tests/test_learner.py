"""Learning at work (app/learner.py, rooms.Learned, app/ha_presence.py).

What is taken over is kept apart from what was set by hand, belongs to
one sensor in one mounting, lands in the journal and can be taken back —
and what was taken back, or turned down, stays out.
"""

import asyncio
import copy
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import ha_presence, learner as learner_mod, learning, rooms  # noqa: E402
from app.room_engine import RoomEngine  # noqa: E402
from simhome import living_room  # noqa: E402
from test_learning import DESK, DOOR, HOUR, OUTLINE, SOFA, drive  # noqa: E402
from test_tracking import Clock, Links, Rig, device, room as engine_room  # noqa: E402


@pytest.fixture(scope="module")
def day():
    d = 8 * HOUR
    return drive(living_room(hours=8, home=[(0, 0.3 * d), (0.45 * d, 0.7 * d), (0.85 * d, d)]), 8)


@pytest.fixture(scope="module")
def busy():
    return drive(living_room(hours=3, home=[(0, 3 * HOUR)], busy=True), 3, seed=2)


@pytest.fixture(scope="module")
def both(day, busy):
    """Days with empty stretches and plenty of walks."""
    state = copy.deepcopy(day)
    for key in ("moves", "ends", "stays"):
        state[key] = busy[key] + day[key]
    for key in ("walks", "tracks"):
        state[key] += busy[key]
    return state


@pytest.fixture
def data(monkeypatch, tmp_path):
    monkeypatch.setattr(rooms, "DATA_DIR", tmp_path)
    return tmp_path


class Presence:
    """Home Assistant, as learning asks it."""

    def __init__(self, home=None, away_s=None):
        self.home, self._away, self.entity = home, away_s, None

    def away_for(self, _now=None):
        return self._away

    def view(self):
        return {"home": self.home}


def stored_room(angle=135.0, furniture=(), zones=(), device_id="dev") -> rooms.Room:
    room = rooms.create_room(rooms.RoomCreate(name="Wohnzimmer", width=3.9, height=5.0, device_id=device_id))
    data = room.model_dump()
    data.update(outline=OUTLINE, furniture=[f.model_dump() for f in furniture], zones=[z.model_dump() for z in zones])
    data["sensor"].update(x=3.9, y=5.0, angle=angle)
    return rooms.save_room(room.id, data)


def learner_with(room, state, mode="auto"):
    ln = learner_mod.Learner(Presence())
    ln.settings = learner_mod.Settings(mode=mode)
    ln.sync(rooms.list_rooms())
    lr = ln._for(room)
    lr.state.clear()
    lr.state.update(learning.empty_state(room.sensor.device_id, room.calibration.mounting_epoch, 0.0))
    lr.state.update({k: copy.deepcopy(v) for k, v in state.items() if k not in ("device_id", "epoch")})
    return ln, lr


def run(coro):
    return asyncio.run(coro)


SOFA_ZONE = rooms.Zone(id="zsofa", name="Sofa", points=[(2.3, 3.1), (3.9, 3.1), (3.9, 4.2), (2.3, 4.2)], hold_s=10)


# --- the room's own layer ----------------------------------------------------------


def test_learned_spots_and_holds_apply_to_their_sensor_only(data):
    room = stored_room(zones=[SOFA_ZONE])
    rooms.set_learned(room.id, {"spots": [{"x": 0.9, "y": 2.6, "r": 0.3, "share": 0.3}], "hold_s": 40,
                                "zone_hold_s": {"zsofa": 35, "gone": 50}}, device_id="dev", epoch=0)
    room = rooms.get_room(room.id)
    assert room.learned.zone_hold_s == {"zsofa": 35}
    assert [(s.x, s.y) for s in rooms.active_interference(room)] == [(0.9, 2.6)]
    assert rooms.effective_hold(room) == 40 and rooms.effective_hold(room, room.zones[0]) == 35
    # Set by hand longer than learned: the longer one.
    room.zones[0].hold_s = 60
    assert rooms.effective_hold(room, room.zones[0]) == 60
    # Another sensor: nothing learned applies.
    other = room.model_copy(deep=True)
    other.sensor.device_id = "other"
    assert rooms.active_interference(other) == [] and rooms.effective_hold(other) == other.hold_s


def test_learning_writes_no_new_revision_and_the_editor_cannot_undo_it(data):
    room = stored_room()
    revision = room.revision
    rooms.set_learned(room.id, {"spots": [], "hold_s": 30}, device_id="dev", epoch=0)
    assert rooms.get_room(room.id).revision == revision
    # An editor that loaded the room before learning saves over it.
    stale = room.model_dump()
    stale["learned"] = None
    stale["name"] = "Wohnen"
    saved = rooms.save_room(room.id, stale)
    assert saved.name == "Wohnen" and saved.learned.hold_s == 30


def test_learned_for_another_sensor_or_mounting_is_refused(data):
    room = stored_room()
    with pytest.raises(rooms.CalibrationConflict):
        rooms.set_learned(room.id, {"hold_s": 30}, device_id="other", epoch=0)
    with pytest.raises(rooms.CalibrationConflict):
        rooms.set_learned(room.id, {"hold_s": 30}, device_id="dev", epoch=1)


def test_a_remount_drops_what_was_learned(data):
    room = stored_room()
    rooms.set_learned(room.id, {"hold_s": 30}, device_id="dev", epoch=0)
    room = rooms.remount_sensor(room.id, revision=rooms.get_room(room.id).revision)
    assert room.learned is None and room.calibration.mounting_epoch == 1


def test_turning_the_sensor_keeps_the_state_before_in_the_history(data):
    room = stored_room(angle=-90)
    turned, before = rooms.turn_sensor(room.id, expect={"x": 3.9, "y": 5.0, "angle": -90.0, "mirror": False},
                                       angle=135.0, mirror=False)
    assert turned.sensor.angle == 135.0 and turned.revision == room.revision + 1
    record = next(r for r in turned.calibration.alignment_history if r.id == before)
    assert record.origin == "before" and record.sensor["angle"] == -90.0
    with pytest.raises(rooms.CalibrationConflict):
        rooms.turn_sensor(room.id, expect={"x": 3.9, "y": 5.0, "angle": -90.0, "mirror": False},
                          angle=0.0, mirror=False)


# --- the engine's side ---------------------------------------------------------------


class Watcher:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def observe(self, room, tracks, generation):
        self.calls.append(("observe", room.id, len(tracks), generation))
        if self.fail:
            raise RuntimeError("boom")

    def pause(self, room):
        self.calls.append(("pause", room.id))


def test_the_engine_hands_every_round_to_learning():
    watcher = Watcher()
    rig = Rig()
    rig.engine._learner = watcher
    rig.report("-15,30")
    rig.report("-15,30")
    assert [c[0] for c in watcher.calls] == ["observe", "observe"]
    assert watcher.calls[-1][2] == 1
    rig.tick(4.0)  # stale: the sensor is away
    assert watcher.calls[-1] == ("pause", "r1")


def test_learning_that_fails_costs_no_count():
    rig = Rig()
    rig.engine._learner = Watcher(fail=True)
    for _ in range(6):
        result = rig.report("-15,30")
    assert result["count"] == 1


def test_the_engine_counts_with_learned_spots_and_holds():
    learned = {"device_id": "dev", "epoch": 0, "spots": [{"x": -1.5, "y": 3.0, "r": 0.4, "share": 0.3}],
               "hold_s": 30, "zone_hold_s": {"sofa": 30}}
    rig = Rig(engine_room(learned=learned))
    for _ in range(10):
        result = rig.report("-15,30")
    # Appeared in a learned spot: never confirmed.
    assert result["count"] == 0 and result["filter"]["learned_spots"] == 1
    assert result["filter"]["hold_s"] == 30
    rig2 = Rig(engine_room(learned={**learned, "spots": []}))
    for _ in range(6):
        rig2.report("10,30")
    for _ in range(10):  # nobody reported for two seconds
        gone = rig2.report("")
    assert gone["count"] == 0 and gone["occupied"] and gone["hold_remaining"] > 25


# --- Home Assistant -----------------------------------------------------------------------


def person(entity, state, changed="2026-10-08T10:00:00+00:00"):
    return {"entity_id": f"person.{entity}", "state": state, "last_changed": changed}


def test_nobody_home_once_everybody_is_somewhere_else():
    states = [person("a", "not_home", "2026-10-08T10:00:00+00:00"), person("b", "Arbeit", "2026-10-08T11:00:00+00:00"),
              {"entity_id": "light.x", "state": "on"}]
    found = ha_presence.whereabouts(states)
    assert found["home"] is False and found["persons"] == 2
    # Since the last of them left.
    assert found["since"] == pytest.approx(1791457200.0)  # 2026-10-08 11:00 UTC


@pytest.mark.parametrize("states, home", [
    ([person("a", "home"), person("b", "not_home")], True),
    ([person("a", "unknown"), person("b", "not_home")], None),
    ([], None),
])
def test_somebody_home_or_not_known(states, home):
    assert ha_presence.whereabouts(states)["home"] is home


@pytest.mark.parametrize("state, home", [("on", True), ("off", False), ("2", True), ("0", False), ("unavailable", None)])
def test_one_entity_can_say_it_instead(state, home):
    states = [person("a", "home"), {"entity_id": "zone.home", "state": state, "last_changed": None}]
    assert ha_presence.whereabouts(states, "zone.home")["home"] is home


def test_the_house_counts_as_empty_only_after_a_while():
    clock = Clock()
    presence = ha_presence.HomePresence(clock=clock)
    presence.take([person("a", "not_home", None)])  # empty from now
    ln = learner_mod.Learner(presence, clock=clock)
    clock.now = 1000.0 + learner_mod.AWAY_GRACE_S - 1
    assert ln.away() is False
    clock.now = 1000.0 + learner_mod.AWAY_GRACE_S
    assert ln.away() is True
    presence.take([person("a", "home")])
    assert ln.away() is False
    presence.lost("weg")
    assert ln.away() is None


def test_an_empty_house_without_a_time_counts_from_when_it_was_seen():
    clock = Clock()
    presence = ha_presence.HomePresence(clock=clock)
    presence.take([person("a", "not_home", None)])
    clock.now += 100
    assert presence.away_for() == pytest.approx(100)


# --- the learner ------------------------------------------------------------------------------


def test_reflectors_and_hold_times_are_taken_over_and_journalled(data, day):
    room = stored_room(zones=[SOFA_ZONE])
    ln, lr = learner_with(room, day)
    run(ln.analyse_room(room.id))
    room = rooms.get_room(room.id)
    assert len(room.learned.spots) == 2
    assert room.learned.zone_hold_s == {"zsofa": 35.0} and room.learned.hold_s == 35.0
    kinds = [e["kind"] for e in lr.journal if e["state"] == "applied"]
    assert kinds.count("spots") == 1 and kinds.count("hold") == 2
    spots = next(e for e in lr.journal if e["kind"] == "spots")
    assert spots["title"] == "2 Störquellen erkannt: bei 1,4 m / 3,8 m, bei 2,1 m / 1,3 m"
    # A second round changes nothing and writes nothing new.
    before = len(lr.journal)
    run(ln.analyse_room(room.id))
    assert len(lr.journal) == before


def test_what_is_taken_back_stays_out(data, day):
    room = stored_room(zones=[SOFA_ZONE])
    ln, lr = learner_with(room, day)
    run(ln.analyse_room(room.id))
    spots = next(e for e in lr.journal if e["kind"] == "spots")
    run(ln.act(room.id, spots["id"], "undo"))
    assert rooms.get_room(room.id).learned.spots == []
    hold = next(e for e in lr.journal if e["kind"] == "hold" and e["data"]["target"] == "zsofa")
    run(ln.act(room.id, hold["id"], "undo"))
    assert rooms.get_room(room.id).learned.zone_hold_s == {}
    run(ln.analyse_room(room.id))
    room = rooms.get_room(room.id)
    assert room.learned.spots == [] and room.learned.zone_hold_s == {}
    assert room.learned.hold_s == 35.0
    with pytest.raises(ValueError):
        run(ln.act(room.id, spots["id"], "undo"))


def test_in_suggest_mode_everything_waits_for_a_yes(data, day):
    room = stored_room(zones=[SOFA_ZONE])
    ln, lr = learner_with(room, day, mode="suggest")
    run(ln.analyse_room(room.id))
    assert rooms.get_room(room.id).learned is None
    proposals = {e["kind"]: e for e in lr.journal if e["state"] == "open"}
    assert {"spots", "hold"} <= set(proposals)
    run(ln.act(room.id, proposals["spots"]["id"], "accept"))
    assert len(rooms.get_room(room.id).learned.spots) == 2
    run(ln.act(room.id, proposals["hold"]["id"], "decline"))
    run(ln.analyse_room(room.id))
    assert not any(e["state"] == "open" and e["key"] == proposals["hold"]["key"] for e in lr.journal)


def test_a_wrongly_turned_sensor_is_turned_and_can_be_turned_back(data, busy):
    room = stored_room(angle=-90.0, furniture=[SOFA, DESK, DOOR])
    ln, lr = learner_with(room, busy)
    run(ln.analyse_room(room.id))
    turned = rooms.get_room(room.id)
    assert learning._angle_diff(turned.sensor.angle, 135.0) <= 5
    entry = next(e for e in lr.journal if e["kind"] == "placement")
    assert entry["state"] == "applied" and entry["auto"]
    run(ln.act(room.id, entry["id"], "undo"))
    assert rooms.get_room(room.id).sensor.angle == -90.0
    # Taken back: not turned again.
    run(ln.analyse_room(room.id, force_fit=True))
    assert rooms.get_room(room.id).sensor.angle == -90.0


def test_after_a_turn_everything_else_is_worked_out_for_the_turned_plan(data, both):
    """Reflectors, hold times and zones depend on where the plan puts the
    module's positions: worked out for the plan before the turn, they
    would describe places outside the room."""
    room = stored_room(angle=-90.0, furniture=[SOFA, DESK, DOOR], zones=[SOFA_ZONE])
    ln, lr = learner_with(room, both)
    run(ln.analyse_room(room.id))
    room = rooms.get_room(room.id)
    assert learning._angle_diff(room.sensor.angle, 135.0) <= 5
    assert room.learned.zone_hold_s == {"zsofa": 35.0}
    spots = next(e for e in lr.journal if e["kind"] == "spots")
    places = [tuple(float(v.replace(",", ".")) for v in m) for m in re.findall(r"bei (\d+,\d) m / (\d+,\d) m", spots["title"])]
    assert places and all(0 <= x <= 3.9 and 0 <= y <= 5.0 for x, y in places)
    assert lr.analysis["verdict"]["verdict"] == "fits"


def test_without_seats_a_turn_is_proposed_not_made(data, busy):
    room = stored_room(angle=-90.0)
    ln, lr = learner_with(room, busy)
    run(ln.analyse_room(room.id))
    assert rooms.get_room(room.id).sensor.angle == -90.0
    proposal = next(e for e in lr.journal if e["kind"] == "placement")
    assert proposal["state"] == "open" and proposal["data"]["verdict"] in ("ambiguous", "turn")


def test_a_zone_proposal_becomes_a_zone(data, day):
    room = stored_room(furniture=[SOFA, DESK], zones=[SOFA_ZONE])
    ln, lr = learner_with(room, day)
    run(ln.analyse_room(room.id))
    proposal = next(e for e in lr.journal if e["kind"] == "zone" and e["state"] == "open")
    assert proposal["title"] == "Zone vorschlagen: Schreibtisch"
    run(ln.act(room.id, proposal["id"], "accept"))
    room = rooms.get_room(room.id)
    zone = next(z for z in room.zones if z.furniture_id == DESK.id)
    assert zone.name == "Schreibtisch" and zone.kind == "detect"
    run(ln.analyse_room(room.id))
    assert not any(e["kind"] == "zone" and e["state"] == "open" for e in lr.journal)


def test_a_zone_drawn_by_hand_settles_the_proposal(data, day):
    room = stored_room(furniture=[SOFA, DESK], zones=[SOFA_ZONE])
    ln, lr = learner_with(room, day)
    run(ln.analyse_room(room.id))
    assert any(e["kind"] == "zone" and e["state"] == "open" for e in lr.journal)
    rooms.add_zone(room.id, {"id": "zdesk", "name": "Arbeit", "kind": "detect",
                             "points": [(2.0, 0.1), (3.6, 0.1), (3.6, 1.2), (2.0, 1.2)]})
    run(ln.analyse_room(room.id))
    assert not any(e["kind"] == "zone" and e["state"] == "open" for e in lr.journal)


def test_switched_off_nothing_learned_applies_and_nothing_is_watched(data, day):
    room = stored_room(zones=[SOFA_ZONE])
    ln, lr = learner_with(room, day)
    run(ln.analyse_room(room.id))
    ln.set_settings({"mode": "off"})
    assert rooms.get_room(room.id).learned is None
    observed = lr.state["observed_s"]
    clock = Clock()
    ln._clock = clock
    for _ in range(5):
        ln.observe(rooms.get_room(room.id), [], 1)
        clock.now += 0.5
    assert lr.state["observed_s"] == observed


def test_another_sensor_starts_learning_afresh(data, day):
    room = stored_room()
    ln, lr = learner_with(room, day)
    room = rooms.remount_sensor(room.id, revision=room.revision)
    fresh = ln._for(room)
    assert fresh is not lr and fresh.state["epoch"] == 1 and fresh.state["walks"] == 0
    assert fresh.journal[0]["title"].startswith("Neuer Sensor")
    assert len(fresh.journal) > 1  # the journal goes on


def test_the_record_survives_a_restart(data, day):
    room = stored_room()
    ln, lr = learner_with(room, day)
    lr.dirty = True
    ln.flush()
    again = learner_mod.Learner(Presence())
    again.sync(rooms.list_rooms())
    lr2 = again._for(room)
    assert lr2.state["walks"] == day["walks"] and lr2.state["stays"] == lr.state["stays"]
    # A deleted room's record goes with it.
    rooms.delete_room(room.id)
    again.sync(rooms.list_rooms())
    assert not (data / "learning" / f"{room.id}.json").exists()


def test_walking_while_nobody_is_home_taints_every_room(data):
    a = stored_room(device_id="a")
    b = rooms.create_room(rooms.RoomCreate(name="Flur", width=3, height=3, device_id="b"))
    ln = learner_mod.Learner(Presence(home=False, away_s=3600))
    ln.sync(rooms.list_rooms())
    walker = SimpleNamespace(id=1, x=0.0, y=1.0, seen=True, confirmed=True)
    ln.observe(b, [], 1)
    for i in range(30):
        walker.y = 1.0 + i * 0.1
        ln.observe(a, [walker], 1)
    assert ln._rooms[b.id].state["away_open"]["tainted"]
    assert any(e["kind"] == "motion" for e in ln._rooms[a.id].journal)


def test_the_view_says_what_is_missing(data):
    room = stored_room()
    ln = learner_mod.Learner(Presence())
    ln.sync(rooms.list_rooms())
    view = ln.view(room)
    assert view["active"] and view["walks"] == 0
    assert view["needs"]["walks"] == learning.MIN_WALKS
    assert view["journal"][0]["title"] == "Echolot lernt diesen Raum kennen"
    assert ln.view(rooms.create_room(rooms.RoomCreate(name="Leer", width=3, height=3)))["active"] is False


def test_engine_and_learner_together(data):
    """The add-on's wiring: rounds reach the record."""
    room = rooms.Room.model_validate({**engine_room().model_dump(), "id": rooms.create_room(
        rooms.RoomCreate(name="x", width=6, height=4, device_id="dev")).id})
    ln = learner_mod.Learner(Presence())
    ln.sync([room])
    clock, links = Clock(), Links()
    engine = RoomEngine(links, clock=clock, learner=ln)
    engine.load([room], [device()])
    rig = Rig.__new__(Rig)
    rig.clock, rig.links, rig.engine, rig.seq = clock, links, engine, 0
    for _ in range(20):
        rig.report("-15,30")
    assert ln._rooms[room.id].state["observed_s"] > 0
