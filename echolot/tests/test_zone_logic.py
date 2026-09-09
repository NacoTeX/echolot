"""Tests for the zone presence state machine.

The machine is pure and time is an argument, so every case here is exact
— no sleeping, no tolerance windows. Run with pytest, or directly with
`python3 tests/test_zone_logic.py` if pytest isn't installed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.zone_logic import CLEAR, DETECTED, HOLDING, ZoneRuntime, evaluate  # noqa: E402


def step(runtime, now, *, motion=False, score=None, enter=None, exit_=None, hold=0.0):
    return evaluate(
        runtime,
        motion=motion,
        score=score,
        enter_threshold=enter,
        exit_threshold=exit_,
        hold_seconds=hold,
        now=now,
    ).as_dict()


def test_without_hold_it_is_plain_or_logic():
    """The pre-0.10 behaviour has to survive untouched as the default."""
    rt = ZoneRuntime()
    assert step(rt, 0, motion=True)["state"] == DETECTED
    assert step(rt, 1, motion=False)["state"] == CLEAR


def test_hold_time_keeps_the_zone_occupied_then_releases_it():
    rt = ZoneRuntime()
    step(rt, 100, motion=True, hold=30)

    holding = step(rt, 110, motion=False, hold=30)
    assert holding["state"] == HOLDING
    assert holding["occupied"] is True
    assert holding["hold_remaining"] == 20.0

    assert step(rt, 125, motion=False, hold=30)["hold_remaining"] == 5.0

    cleared = step(rt, 131, motion=False, hold=30)
    assert cleared["state"] == CLEAR
    assert cleared["occupied"] is False
    assert cleared["hold_remaining"] == 0.0


def test_motion_during_a_hold_restarts_the_countdown():
    rt = ZoneRuntime()
    step(rt, 0, motion=True, hold=30)
    step(rt, 10, motion=False, hold=30)  # holding until t=30
    step(rt, 20, motion=True, hold=30)  # re-detected, holding until t=50

    refreshed = step(rt, 40, motion=False, hold=30)
    assert refreshed["state"] == HOLDING
    assert refreshed["hold_remaining"] == 10.0


def test_hysteresis_ignores_the_band_between_the_thresholds():
    rt = ZoneRuntime()
    assert step(rt, 0, score=0.5, enter=2.0, exit_=1.0)["state"] == CLEAR
    assert step(rt, 1, score=2.5, enter=2.0, exit_=1.0)["state"] == DETECTED
    # Inside the band the previous decision stands — in both directions.
    assert step(rt, 2, score=1.5, enter=2.0, exit_=1.0)["state"] == DETECTED
    assert step(rt, 3, score=0.9, enter=2.0, exit_=1.0)["state"] == CLEAR
    assert step(rt, 4, score=1.5, enter=2.0, exit_=1.0)["state"] == CLEAR


def test_a_single_threshold_behaves_as_enter_equals_exit():
    rt = ZoneRuntime()
    assert step(rt, 0, score=2.0, enter=2.0)["state"] == DETECTED
    assert step(rt, 1, score=1.9, enter=2.0)["state"] == CLEAR


def test_a_configured_threshold_overrides_the_device_motion_flag():
    rt = ZoneRuntime()
    assert step(rt, 0, motion=True, score=0.1, enter=2.0)["state"] == CLEAR


def test_it_falls_back_to_the_motion_flag_when_no_score_is_available():
    """A device with no movement-score entity must still work in a tuned zone."""
    rt = ZoneRuntime()
    assert step(rt, 0, motion=True, score=None, enter=2.0)["state"] == DETECTED


def test_hysteresis_and_hold_compose():
    rt = ZoneRuntime()
    step(rt, 0, score=3.0, enter=2.0, exit_=1.0, hold=60)
    result = step(rt, 30, score=0.2, enter=2.0, exit_=1.0, hold=60)
    assert result["state"] == HOLDING
    assert result["raw_motion"] is False
    assert result["hold_remaining"] == 30.0


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
        except AssertionError:
            failures += 1
            import traceback

            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"ok   {name}")
    print("\n" + ("alle Tests bestanden" if not failures else f"{failures} fehlgeschlagen"))
    sys.exit(1 if failures else 0)


def test_a_negative_hold_time_cannot_disable_the_feature():
    """The zone schema bounds this, but the machine states its own
    invariant: a negative hold would expire in the past, silently turning
    a configured hold time into none at all."""
    rt = ZoneRuntime()
    step(rt, 0, motion=True, hold=-10)
    assert step(rt, 1, motion=False, hold=-10)["state"] == CLEAR


def test_the_result_is_a_typed_value_not_a_bag_of_keys():
    from app.zone_logic import ZoneEvaluation

    rt = ZoneRuntime()
    result = evaluate(
        rt, motion=True, score=None, enter_threshold=None,
        exit_threshold=None, hold_seconds=0, now=0,
    )
    assert isinstance(result, ZoneEvaluation)
    assert result.state == DETECTED
    # The JSON shape the API publishes has to stay the same.
    assert set(result.as_dict()) == {
        "state", "occupied", "hold_remaining", "raw_motion", "score",
        "rate_occupied", "trigger",
    }


# --- the second, slower opinion ---------------------------------------------


def test_the_crossing_rate_can_hold_a_zone_that_motion_has_lost():
    """The case the whole rate detector exists for: somebody sitting still.

    Motion says no, because they are not moving. The rate says yes,
    because the room is still crossing about ten times as often as it does
    empty. The zone stays occupied.
    """
    runtime = ZoneRuntime()
    result = evaluate(
        runtime, motion=False, score=None, enter_threshold=None,
        exit_threshold=None, hold_seconds=0.0, now=100.0, rate_occupied=True,
    )
    assert result.occupied is True
    assert result.state == DETECTED
    assert result.rate_occupied is True


def test_the_rate_only_ever_adds():
    """It cannot switch a zone off that motion has switched on. Motion
    reacts in a second and the rate needs a minute; letting the slow signal
    veto the fast one would make the zone drop out just as somebody walks
    in."""
    runtime = ZoneRuntime()
    result = evaluate(
        runtime, motion=True, score=None, enter_threshold=None,
        exit_threshold=None, hold_seconds=0.0, now=100.0, rate_occupied=False,
    )
    assert result.occupied is True


def test_without_a_baseline_the_zone_behaves_exactly_as_before():
    """A device with no learned profile contributes no rate opinion, and
    nothing about the existing behaviour may change for it."""
    runtime = ZoneRuntime()
    result = evaluate(
        runtime, motion=False, score=None, enter_threshold=None,
        exit_threshold=None, hold_seconds=0.0, now=100.0, rate_occupied=None,
    )
    assert result.occupied is False
    assert result.rate_occupied is None


def test_a_rate_that_falls_away_starts_the_hold_like_motion_would():
    runtime = ZoneRuntime()
    evaluate(
        runtime, motion=False, score=None, enter_threshold=None,
        exit_threshold=None, hold_seconds=30.0, now=100.0, rate_occupied=True,
    )
    result = evaluate(
        runtime, motion=False, score=None, enter_threshold=None,
        exit_threshold=None, hold_seconds=30.0, now=110.0, rate_occupied=False,
    )
    assert result.state == HOLDING
    assert result.occupied is True


# --- Regressions from the external review (P1 #3) ----------------------


def test_the_rate_does_not_latch_the_motion_hysteresis():
    """0.13.2 wrote the combined motion-or-rate result into `runtime.raw`.

    `runtime.raw` is the *motion* hysteresis memory: with a score between
    the exit and enter thresholds, `_raw_decision` returns whatever it
    said last time. So one minute of elevated rate latched the motion
    path on, and the zone stayed occupied for as long as the score sat in
    the band — long after the rate had dropped.
    """
    runtime = ZoneRuntime()
    common = dict(
        motion=False, score=0.5, enter_threshold=0.8, exit_threshold=0.2,
        hold_seconds=0.0,
    )
    first = evaluate(runtime, now=100.0, rate_occupied=True, **common)
    assert first.occupied is True

    second = evaluate(runtime, now=101.0, rate_occupied=False, **common)
    assert second.occupied is False, "die Rate hat die Bewegungs-Hysterese eingerastet"


def test_raw_motion_means_motion_and_not_the_rate():
    """Otherwise the state reports movement that never happened."""
    runtime = ZoneRuntime()
    result = evaluate(
        runtime, motion=False, score=None, enter_threshold=None,
        exit_threshold=None, hold_seconds=0.0, now=1.0, rate_occupied=True,
    )
    assert result.occupied is True
    assert result.raw_motion is False
    assert result.trigger == "rate"


def test_the_trigger_names_motion_when_motion_is_what_fired():
    runtime = ZoneRuntime()
    result = evaluate(
        runtime, motion=True, score=None, enter_threshold=None,
        exit_threshold=None, hold_seconds=0.0, now=1.0, rate_occupied=True,
    )
    assert result.trigger == "motion"


def test_nothing_holding_the_zone_has_no_trigger():
    runtime = ZoneRuntime()
    result = evaluate(
        runtime, motion=False, score=None, enter_threshold=None,
        exit_threshold=None, hold_seconds=0.0, now=1.0, rate_occupied=False,
    )
    assert result.trigger is None


def test_the_motion_hysteresis_still_works_on_its_own():
    """The band behaviour the memory exists for must survive the fix."""
    runtime = ZoneRuntime()
    common = dict(
        motion=False, enter_threshold=0.8, exit_threshold=0.2, hold_seconds=0.0,
    )
    assert evaluate(runtime, score=0.9, now=1.0, **common).occupied is True
    # In the band: unchanged, so still occupied.
    assert evaluate(runtime, score=0.5, now=2.0, **common).occupied is True
    # Below the exit threshold: off.
    assert evaluate(runtime, score=0.1, now=3.0, **common).occupied is False
    # Back into the band from below: stays off.
    assert evaluate(runtime, score=0.5, now=4.0, **common).occupied is False
