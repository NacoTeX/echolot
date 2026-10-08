"""What Echolot learns from everyday life (app/learning.py).

Most of these drive the simulated home (simhome.py) through the
production tracker and observer, and check what was learned against
what is really there: the reflectors, the way the sensor looks, how long
the module loses somebody sitting. The rest pin the rules that keep
learning from doing harm — a person is never learned as a reflector, a
plan is never "corrected" into a wrong one that merely fits.
"""

import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import geometry, learning  # noqa: E402
from app.rooms import Furniture, Room, SensorPlacement, Zone  # noqa: E402
from app.tracking import SMOOTHING_TAU, Tracker  # noqa: E402
from simhome import Ghost, Placement, Simulation, bedroom, living_room, to_sensor  # noqa: E402

HOUR = 3600.0
#: The living room's walls: 3.9 × 5 m with a strip cut off on the left.
OUTLINE = [[0, 0], [3.9, 0], [3.9, 5], [0.35, 5], [0.35, 1.1], [0, 1.1]]
SOFA = Furniture(id="f1", kind="sofa", x=2.4, y=3.2, w=1.4, h=0.9)
DESK = Furniture(id="f2", kind="desk", x=2.2, y=0.3, w=1.2, h=0.6)
DOOR = Furniture(id="f3", kind="door", x=0.2, y=-0.05, w=0.8, h=0.1)


def drive(home, hours, seed=1, grace=600.0):
    """The simulation through the tracker and an observer, as the engine
    and learner run them; the record at the end."""
    sim = Simulation(home, seed)
    tracker = Tracker()
    state = learning.empty_state("dev", 0, 0.0)
    observer = learning.Observer(state)
    away_since = None
    for t, points, truth in sim.run(hours * HOUR):
        tracker.update(points, t, confirm_s=1.0, tau=SMOOTHING_TAU["normal"])
        if truth.home:
            away_since = None
        elif away_since is None:
            away_since = t
        observer.observe(tracker.tracks, t, away_since is not None and t - away_since >= grace)
    observer.flush(hours * HOUR)
    return state


def plan(sensor: dict, furniture=(), zones=(), **kw) -> Room:
    return Room(id="r1", name="Wohnzimmer", width=3.9, height=5.0, outline=OUTLINE,
                sensor=SensorPlacement(device_id="dev", **sensor), furniture=list(furniture), zones=list(zones), **kw)


TRUE = {"x": 3.9, "y": 5.0, "angle": 135.0}


@pytest.fixture(scope="module")
def day():
    """Eight hours: home, out for 1.2 h, home, out again, home."""
    d = 8 * HOUR
    return drive(living_room(hours=8, home=[(0, 0.3 * d), (0.45 * d, 0.7 * d), (0.85 * d, d)]), 8)


@pytest.fixture(scope="module")
def busy():
    """Three busy hours at home: many walks, as days would bring."""
    return drive(living_room(hours=3, home=[(0, 3 * HOUR)], busy=True), 3, seed=2)


def room_of(spot: dict, placement=TRUE) -> tuple[float, float]:
    return geometry.to_room(spot["x"], spot["y"], {**placement, "mirror": False})


# --- reflectors ---------------------------------------------------------------


def test_both_reflectors_are_found_while_nobody_is_home(day):
    spots = learning.find_interference(day, plan(TRUE))
    assert len(spots) == 2
    # Where the simulated reflectors are, in the sensor's metres, and as
    # often as they are there: 35 % and 15 % of the time.
    strong, weak = spots
    assert abs(strong["x"] - 0.9) < 0.15 and abs(strong["y"] - 2.6) < 0.15
    assert abs(weak["x"] + 1.4) < 0.15 and abs(weak["y"] - 3.8) < 0.15
    assert 0.28 < strong["share"] < 0.42 and 0.10 < weak["share"] < 0.20
    assert strong["auto"] and weak["auto"]
    assert strong["chunks"] == weak["chunks"] == 2


def test_nobody_home_is_kept_in_clean_pieces(day):
    chunks = day["away_chunks"]
    assert [round(c["s"] / 60) for c in chunks] == [62, 62]
    assert day["away_tainted"] == 0
    assert day["away_open"] is None


def test_one_empty_stretch_is_not_enough_to_take_a_reflector_over():
    d = 4 * HOUR
    state = drive(living_room(hours=4, home=[(0, 0.3 * d), (0.7 * d, d)]), 4, seed=3)
    spots = learning.find_interference(state, plan(TRUE))
    assert spots and all(not s["auto"] and s["reason"] == "few_chunks" for s in spots)


def test_a_pet_walking_while_nobody_is_home_teaches_nothing_about_reflectors():
    d = 8 * HOUR
    home = living_room(hours=8, home=[(0, 0.3 * d), (0.45 * d, 0.7 * d), (0.85 * d, d)])
    home.pet_periods = [(0.3 * d, 0.45 * d), (0.7 * d, 0.85 * d)]
    state = drive(home, 8)
    assert state["away_motion"]["count"] > 0
    assert state["away_tainted"] >= 2
    assert state["away_chunks"] == []
    assert learning.find_interference(state, plan(TRUE)) == []


def test_somebody_asleep_while_the_house_counts_as_empty_is_no_reflector():
    """The child in bed while the parents are out, three evenings running:
    the bed is where somebody walked in and stayed, every time. The
    parents are back before the child gets up — nothing walks while the
    house counts as empty."""
    night = 3.2 * HOUR
    parents = [(0, night)]
    for n in (1, 2):
        base = n * night
        parents += [(base, base + 900), (base + 2.6 * HOUR, base + night)]
    home = bedroom(home=parents, hidden=[(n * night + 600, n * night + 3.0 * HOUR) for n in range(3)])
    state = drive(home, 3 * night / HOUR, seed=5)
    assert state["away_tainted"] == 0 and len(state["away_chunks"]) == 2
    room = Room(id="r2", name="Kinderzimmer", width=3.5, height=4.0,
                sensor=SensorPlacement(device_id="dev", x=1.75, y=4.0, angle=180.0))
    spots = learning.find_interference(state, room)
    bed = [s for s in spots if s["chunks"] >= 2]
    assert bed, "the bed is seen in both empty stretches"
    assert all(not s["auto"] and s["reason"] == "people_sit" for s in bed)


def test_a_reflector_on_a_seat_is_proposed_not_taken_over(day):
    # The strong reflector lies 0.6 m from where the TV is watched;
    # a chair drawn right over it.
    x, y = room_of({"x": 0.9, "y": 2.6})
    chair = Furniture(id="f9", kind="armchair", x=x - 0.4, y=y - 0.4, w=0.8, h=0.8)
    spots = learning.find_interference(day, plan(TRUE, furniture=[chair]))
    on_seat = [s for s in spots if abs(s["x"] - 0.9) < 0.2]
    assert on_seat and not on_seat[0]["auto"] and on_seat[0]["reason"] == "on_seat"


# --- stays and how long the module loses somebody ---------------------------------


def test_people_who_walked_in_are_told_from_reflectors(day):
    people = learning.person_stays(day)
    assert people
    sofa = [s for s in people if geometry.point_in_polygon(*room_of({"x": s["c"][0], "y": s["c"][1]}),
                                                            [(2.6, 3.2), (3.9, 3.2), (3.9, 4.1), (2.6, 4.1)])]
    assert len(sofa) >= 3
    # A reflector never walks in.
    strong = [s for s in day["stays"] if abs(s["c"][0] - 0.9) < 0.2 and abs(s["c"][1] - 2.6) < 0.2]
    assert strong and sum(s["seen_s"] for s in strong if s["arrivals"]) < 0.1 * sum(s["seen_s"] for s in strong)


def test_hold_times_cover_the_lapses_with_somebody_sitting(day):
    sofa_zone = Zone(id="z1", name="Sofa", points=[(2.3, 3.1), (3.9, 3.1), (3.9, 4.2), (2.3, 4.2)], hold_s=10)
    room = plan(TRUE, zones=[sofa_zone])
    spots = [s for s in learning.find_interference(day, room) if s["auto"]]
    holds = learning.hold_recommendations(day, room, spots=spots)
    # The sofa's simulated lapses last up to 30 s.
    zone = holds["zones"]["z1"]
    assert 29 <= zone["q99_s"] <= 30.5 and zone["longest_s"] <= 30.5
    assert zone["hold_s"] == 35.0
    assert holds["room"]["hold_s"] == 35.0


def test_gaps_somebody_came_back_after_are_no_lapses():
    st = learning.empty_state("dev", 0, 0.0)
    ob = learning.Observer(st)
    track = SimpleNamespace(id=1, x=1.0, y=2.0, seen=True, confirmed=True)
    # Sits for 60 s, then walks off.
    t = 0.0
    while t < 60:
        ob.observe([track], t, None)
        t += 0.1
    while t < 66:
        track.y += 0.03
        ob.observe([track], t, None)
        t += 0.1
    ob.observe([], t, None)
    # Back five minutes later, at the same place: a new stay.
    track = SimpleNamespace(id=2, x=1.0, y=2.0, seen=True, confirmed=True)
    t = 370.0
    while t < 400:
        ob.observe([track], t, None)
        t += 0.1
    ob.observe([], t, None)
    ob.flush(t)
    stays = st["stays"]
    assert len(stays) == 2
    assert stays[0]["departures"] == 1 and stays[0]["gaps"] == []
    assert all(g < 300 for s in stays for g in s["gaps"])


def _still(ob, track_id, x, y, t0, t1, *, then_gone=True):
    track = SimpleNamespace(id=track_id, x=x, y=y, seen=True, confirmed=True)
    t = t0
    while t < t1:
        ob.observe([track], t, None)
        t += 0.5
    if then_gone:
        ob.observe([], t, None)
    return t


def test_a_stay_stays_where_somebody_sat_down():
    """A reflector next to the seat is found again and again while
    somebody sits there: the stay must not wander over to it."""
    st = learning.empty_state("dev", 0, 0.0)
    ob = learning.Observer(st)
    walker = SimpleNamespace(id=1, x=0.0, y=3.0, seen=True, confirmed=True)
    t = 0.0
    while walker.y > 0.0:  # walks in and sits down at (0, 0)
        ob.observe([walker], t, None)
        walker.y = max(0.0, walker.y - 0.05)
        t += 0.1
    t = _still(ob, 1, 0.0, 0.0, t, t + 20)
    for i in range(30):  # the reflector, 0.45 m off, and the sitter, by turns
        t = _still(ob, 100 + 2 * i, 0.45, 0.0, t + 1, t + 6)
        t = _still(ob, 101 + 2 * i, 0.0, 0.0, t + 1, t + 6)
    ob.flush(t)
    sitter = [s for s in st["stays"] if s["arrivals"]]
    assert len(sitter) == 1 and abs(sitter[0]["c"][0]) < 0.05


def test_somebody_sitting_down_by_a_reflector_has_a_stay_of_their_own():
    st = learning.empty_state("dev", 0, 0.0)
    ob = learning.Observer(st)
    t = _still(ob, 1, 0.0, 0.0, 0.0, 30.0)  # the reflector, nobody walked in
    walker = SimpleNamespace(id=2, x=0.3, y=3.0, seen=True, confirmed=True)
    while walker.y > 0.0:  # somebody walks up and sits 0.3 m from it
        ob.observe([walker], t, None)
        walker.y = max(0.0, walker.y - 0.05)
        t += 0.1
    t = _still(ob, 2, 0.3, 0.0, t, t + 60)
    t = _still(ob, 3, 0.0, 0.0, t + 2, t + 30)
    ob.flush(t)
    reflector = [s for s in st["stays"] if abs(s["c"][0]) < 0.05]
    person = [s for s in st["stays"] if abs(s["c"][0] - 0.3) < 0.05]
    assert len(reflector) == 1 and reflector[0]["arrivals"] == 0
    assert len(person) == 1 and person[0]["arrivals"] == 1


def test_too_little_to_go_by_says_nothing():
    st = learning.empty_state("dev", 0, 0.0)
    room = plan(TRUE)
    assert learning.hold_recommendations(st, room) == {"room": None, "zones": {}}
    assert learning.find_interference(st, room) == []
    assert learning.plausibility(st, room)["inside_share"] is None
    assert learning.fit_turn(st, room) is None
    assert learning.placement_verdict(None)["verdict"] == "unknown"


def test_a_hold_time_never_grows_past_three_minutes():
    stays = [{"c": [0, 1], "seen_s": 900, "arrivals": 1, "gaps": [400.0] * 2 + [250.0] * 30}] * 3
    rec = learning._hold_for(stays, 1.0)
    assert rec["hold_s"] == 180.0 and rec["short"]


# --- zones and the map ------------------------------------------------------------


def test_a_zone_is_proposed_where_people_sit_without_one(day):
    sofa_zone = Zone(id="z1", name="Sofa", points=[(2.3, 3.1), (3.9, 3.1), (3.9, 4.2), (2.3, 4.2)])
    room = plan(TRUE, furniture=[SOFA, DESK], zones=[sofa_zone])
    spots = [s for s in learning.find_interference(day, room) if s["auto"]]
    proposals = learning.zone_suggestions(day, room, spots=spots)
    assert [p["furniture_id"] for p in proposals] == ["f2"]
    assert proposals[0]["seen_s"] >= 1800


def test_the_map_leaves_the_reflectors_out(day):
    room = plan(TRUE)
    with_them = learning.activity_map(day, room)
    spots = [s for s in learning.find_interference(day, room) if s["auto"]]
    without = learning.activity_map(day, room, spots=spots)

    def hottest(m):
        return max(m["cells"], key=lambda c: c[2])[:2]

    ghost = room_of({"x": 0.9, "y": 2.6})
    assert math.dist(ghost, hottest(with_them)) < 0.3
    assert min(math.dist(ghost, c[:2]) for c in without["cells"]) > 0.2


# --- the plan --------------------------------------------------------------------------


def test_the_walks_fit_the_true_plan_and_not_a_wrong_one(busy):
    assert learning.plausibility(busy, plan(TRUE))["inside_share"] >= 0.99
    assert learning.plausibility(busy, plan({**TRUE, "angle": -90}))["inside_share"] < 0.1


@pytest.mark.parametrize("drawn", [-90.0, 95.0, 180.0])
def test_a_sensor_drawn_looking_the_wrong_way_is_turned(busy, drawn):
    room = plan({**TRUE, "angle": drawn}, furniture=[SOFA, DESK, DOOR])
    turn = learning.fit_turn(busy, room)
    verdict = learning.placement_verdict(turn, learning.fit_anywhere(busy, room))
    assert verdict["verdict"] == "turn" and verdict["auto"]
    assert learning._angle_diff(verdict["placement"]["angle"], 135.0) <= 5
    assert verdict["placement"]["mirror"] is False


def test_the_true_plan_is_left_alone(busy):
    room = plan(TRUE, furniture=[SOFA, DESK, DOOR])
    verdict = learning.placement_verdict(learning.fit_turn(busy, room), learning.fit_anywhere(busy, room))
    assert verdict["verdict"] == "fits"


def test_without_seats_or_doors_a_turn_is_only_proposed(busy):
    room = plan({**TRUE, "angle": -90})
    verdict = learning.placement_verdict(learning.fit_turn(busy, room), None, mirror_known=True)
    assert verdict["verdict"] == "turn" and not verdict["auto"]


def test_without_knowing_left_from_right_a_corner_stays_open(busy):
    """A corner sensor fits the same walks mirrored and turned: without
    the walk test, nothing is turned."""
    room = plan({**TRUE, "angle": -90})
    verdict = learning.placement_verdict(learning.fit_turn(busy, room), None, mirror_known=False)
    assert verdict["verdict"] == "ambiguous" and not verdict["auto"]


def test_a_mirrored_plan_is_proposed_for_the_walk_test(busy):
    room = plan({**TRUE, "mirror": True}, furniture=[SOFA, DESK, DOOR])
    verdict = learning.placement_verdict(learning.fit_turn(busy, room), learning.fit_anywhere(busy, room))
    assert verdict["verdict"] == "mirror" and not verdict["auto"]
    assert verdict["placement"]["mirror"] is False


@pytest.mark.parametrize("drawn", [{"x": 0.35, "y": 5.0, "angle": -135.0}, {"x": 2.0, "y": 0.0, "angle": 0.0}])
@pytest.mark.parametrize("seats", [True, False])
@pytest.mark.parametrize("known", [True, False])
def test_a_sensor_drawn_elsewhere_is_never_turned_by_itself(busy, drawn, seats, known):
    """Drawn in the wrong corner, or on the wrong wall: some turn there may
    fit the walks — but it is not where the sensor is."""
    room = plan(drawn, furniture=[SOFA, DESK, DOOR] if seats else [])
    verdict = learning.placement_verdict(learning.fit_turn(busy, room), learning.fit_anywhere(busy, room),
                                         mirror_known=known)
    assert not verdict["auto"]


def test_a_ceiling_module_is_searched_for_all_over_the_ceiling(busy):
    room = plan({"x": 2.0, "y": 2.5, "angle": 0.0})
    found = learning.fit_anywhere(busy, room, mount_mode="top")
    best = found["best"]
    assert geometry.point_in_polygon(best["x"], best["y"], OUTLINE)
    assert geometry.distance_to_polygon(best["x"], best["y"], OUTLINE) > 0.2
    assert best["inside"] >= 0.95


def test_the_turn_search_sees_through_the_sensor_model(busy):
    """A module that reports 35 % too far, and a room whose sensor model
    says so: the search places the corrected positions, as the engine
    does."""
    k = 1.35

    def scaled(rows, i):
        return [[*r[:i], r[i] * k, r[i + 1] * k, *r[i + 2:]] for r in rows]

    state = {**busy, "moves": scaled(busy["moves"], 1), "ends": scaled(busy["ends"], 1),
             "stays": [{**st, "c": [st["c"][0] * k, st["c"][1] * k]} for st in busy["stays"]]}
    room = plan({**TRUE, "angle": -90, "range_scale": k}, furniture=[SOFA, DESK, DOOR])
    assert learning.plausibility(state, plan({**TRUE, "range_scale": k}))["inside_share"] >= 0.99
    verdict = learning.placement_verdict(learning.fit_turn(state, room), None, mirror_known=True)
    assert verdict["verdict"] == "turn" and learning._angle_diff(verdict["placement"]["angle"], 135.0) <= 5
    assert verdict["placement"]["inside"] >= 0.99


def test_a_sensor_drawn_on_the_wrong_wall_is_reported(busy):
    room = plan({"x": 2.0, "y": 0.0, "angle": 0.0}, furniture=[SOFA, DESK, DOOR])
    verdict = learning.placement_verdict(learning.fit_turn(busy, room), learning.fit_anywhere(busy, room))
    assert verdict["verdict"] == "elsewhere" and not verdict["auto"]
    assert verdict["placement"]["inside"] >= 0.99


def test_a_sensor_drawn_in_the_wrong_corner_is_reported_once_left_and_right_are_known(busy):
    room = plan({"x": 0.35, "y": 5.0, "angle": -135.0}, furniture=[SOFA, DESK, DOOR])
    verdict = learning.placement_verdict(learning.fit_turn(busy, room), learning.fit_anywhere(busy, room),
                                         mirror_known=True)
    assert verdict["verdict"] == "elsewhere"


def test_the_simulator_inverts_the_plan():
    p = Placement(3.9, 5.0, 135.0, mirror=True)
    xm, ym = to_sensor(1.0, 2.0, p)
    assert geometry.to_room(xm, ym, p.as_dict()) == pytest.approx((1.0, 2.0))
    assert Ghost(0, 0).share == 0.3
