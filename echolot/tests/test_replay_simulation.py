"""Replay has to run the detector, not something that resembles it.

From the external review (R6). `replay.compare` groups by label first
and scores each label's windows on their own, which is a useful summary
and not a simulation:

  * grouping by label destroys the order of the session, so a window can
    span two separate stretches that happened to carry the same label,
    with everything in between removed;
  * every window was judged with `occupied_now=False`, so the exit level
    — the whole reason there are two levels — was never used;
  * `motion_only` was `any(motion)` per minute, not the product's actual
    interplay of motion, rate and hold time;
  * `split_windows` drops a trailing part-window before anything counts
    it, so a ten-second recording reported zero judged and zero unusable
    windows and the time simply was not in the report.

`replay.simulate` runs the recording forward through the same two
functions the live path takes — `presence_rate.advance` and
`zone_logic.evaluate` — with the clock injected. The first test here is
the one that matters: live and replay, same events, same transitions.
"""

import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import live_presence, main, presence_rate, replay, samples  # noqa: E402
from app.telemetry import Sample  # noqa: E402
from app.devices import Device, DeviceCreate  # noqa: E402
from app.zones import Zone  # noqa: E402

PROFILE = presence_rate.RateProfile(
    crossing_threshold=1e-3,
    baseline_rate=0.05,
    baseline_spread=0.01,
    window_seconds=60.0,
    sample_count=1200,
    observed_seconds=1200.0,
)


def series() -> list[dict]:
    """Four minutes: quiet, busy, quiet-but-not-silent, quiet again.

    The profile's levels are 0.10/s to switch on and 0.06/s to stay on.
    The third stretch sits at 0.083/s — between them, which is the case
    the two levels exist for: a detector that only ever asks "would this
    switch on" reads it differently from one that remembers it was
    already on.
    """
    rows = []
    for index in range(960):                      # 240 s at 4 Hz
        t = index * 0.25
        if t < 60:
            score = 0.5 if index % 80 == 0 else 0.0        # 0.05/s, at baseline
        elif t < 120:
            score = 0.5 if index % 4 == 0 else 0.0         # 1.0/s, busy
        elif t < 180:
            score = 0.5 if index % 48 == 0 else 0.0        # 0.083/s, between
        else:
            score = 0.0                                    # silent
        rows.append({
            "t": t,
            "movement_score": score,
            "motion": 60 <= t < 90,               # the device sees movement here
            "label": "empty" if t < 60 or t >= 180 else "still",
        })
    return rows


# --- live and replay on the same event sequence ------------------------


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def time(self):
        return self.now


class ReplayStream:
    """A live subscription that is up.

    The readings themselves reach the rate through the canonical bus, the
    same channel calibration and replay read — that is the point of the
    parity test. Only the connection state comes from here, and the
    motion latch, which this recording never uses: its motion is a level
    on every row, not a pulse between rounds.
    """

    connected = True
    error = None

    def motion_pulsed(self):
        return live_presence.MotionPulse(happened=False)


def live_transitions(rows, *, hold_seconds, monkeypatch):
    """Drive the real evaluator over the recording and note every change."""
    from app import feature_api

    clock = Clock()
    device = Device(
        id="probe", created_at=0, updated_at=0,
        config=DeviceCreate(name="probe", board="esp32c6", wifi_ssid="netz",
                            wifi_password="passwort123"),
        entity_motion="binary_sensor.probe_motion",
        entity_movement_score="sensor.probe_score",
    )
    device.presence_profile = PROFILE.as_dict()
    stream = ReplayStream()

    # The bus windows against the wall clock, so it gets the same virtual
    # one. Readings are published as the clock reaches them: a live
    # buffer never holds the future, and `window()` has no upper bound
    # because in production it does not need one.
    monkeypatch.setattr(samples, "time", clock)
    samples.bus.forget("probe")
    pending = iter(sorted(rows, key=lambda row: row["t"]))
    upcoming = next(pending, None)

    def deliver_until(moment):
        nonlocal upcoming
        while upcoming is not None and upcoming["t"] <= moment:
            samples.bus.publish(
                "probe",
                Sample(t=upcoming["t"], movement_score=upcoming["movement_score"],
                       threshold=None, motion=upcoming["motion"]),
                source=samples.SOURCE_HOME_ASSISTANT,
            )
            upcoming = next(pending, None)

    monkeypatch.setattr(main, "time", clock)
    monkeypatch.setattr(main.devices, "get_device", lambda i: device if i == "probe" else None)
    monkeypatch.setattr(feature_api, "live", types.SimpleNamespace(
        stream=lambda device_id: stream if device_id == "probe" else None
    ))
    main._rate_state.clear()

    evaluator = main.ZoneEvaluator()
    monkeypatch.setattr(main, "evaluator", evaluator)
    zone = Zone(id="z", created_at=0, updated_at=0, name="Z", device_ids=["probe"],
                hold_seconds=hold_seconds)

    # The zone's own member read comes from Home Assistant in production;
    # here it comes from the same recording, at the same instant.
    async def members(zone_, _snapshots=None):
        current = [r for r in rows if r["t"] <= clock.now]
        newest = current[-1] if current else None
        runtime = main._zone_runtimes.setdefault(zone_.id, main.zone_logic.ZoneRuntime())
        verdict = main.zone_logic.evaluate(
            runtime,
            motion=bool(newest and newest["motion"]),
            score=newest["movement_score"] if newest else None,
            enter_threshold=zone_.enter_threshold,
            exit_threshold=zone_.exit_threshold,
            hold_seconds=zone_.hold_seconds,
            now=clock.monotonic(),
            rate_occupied=main._zone_rate_verdict(zone_),
        )
        return {"available": True, "members": [], **verdict.as_dict()}

    monkeypatch.setattr(main, "compute_zone_state", members)

    # The round still runs — it is what prunes the hysteresis of devices
    # that have left every zone, so a round that sees no devices wipes
    # the memory this test is about. Only the Home Assistant read is
    # replaced; `members` takes the motion and score from the recording.
    async def read_state(_device, allow_detect=True):
        return {"available": True, "motion": False,
                "movement_score": None, "threshold": None}

    monkeypatch.setattr(main, "_read_device_state", read_state)
    main._zone_runtimes.pop("z", None)

    changes = []
    previous = None
    for moment in replay._steps(rows, replay.DEFAULT_TICK_SECONDS):
        clock.now = moment
        deliver_until(moment)
        asyncio.run(evaluator.cycle([zone]))
        state = evaluator.snapshot("z")["state"]
        if state != previous:
            changes.append((round(moment, 3), state))
            previous = state
    main._rate_state.clear()
    main._zone_runtimes.pop("z", None)
    samples.bus.forget("probe")
    return changes


@pytest.mark.parametrize("hold_seconds", [0.0, 30.0])
def test_live_and_replay_agree_on_the_same_events(monkeypatch, hold_seconds):
    """The acceptance criterion for R6, and the reason `advance` is one
    function rather than two copies."""
    rows = series()
    live = live_transitions(rows, hold_seconds=hold_seconds, monkeypatch=monkeypatch)

    result = replay.simulate(rows, PROFILE, hold_seconds=hold_seconds)
    replayed = [(round(t["t"], 3), t["to"]) for t in result["transitions"]]

    assert replayed == live, f"Replay {replayed}\nLive     {live}"
    assert len(live) > 2, "das Testmaterial erzeugt kaum Zustandswechsel"


# --- hysteresis and hold time actually matter --------------------------


def test_the_exit_level_is_used_because_the_run_is_chronological():
    """`compare` judged every window with `occupied_now=False`, so the
    stretch between the two levels always read as empty. Running forward
    keeps the device's own previous verdict, which is what the exit level
    is for."""
    rows = series()
    result = replay.simulate(rows, PROFILE, hold_seconds=0.0, include_timeline=True)

    # 120..180 s sits between the exit level (0.06/s) and the enter level
    # (0.10/s): a detector that only ever asks "would this switch on"
    # reads it as empty.
    between = [row for row in result["timeline"] if 130 <= row["t"] < 175]
    assert between and all(row["rate_occupied"] for row in between), (
        "die Ausschaltschwelle wurde nicht benutzt"
    )

    # And the same stretch judged the way `compare` judges it — from a
    # standing start, `occupied_now=False` — says the opposite.
    stretch = [r for r in rows if 120 <= r["t"] <= 180]
    cold = presence_rate.evaluate(PROFILE, stretch, occupied_now=False)
    assert cold["available"] is True and cold["occupied"] is False


def test_the_hold_time_changes_the_answer():
    """Otherwise the runner is not simulating the thing that ships."""
    rows = series()
    without = replay.simulate(rows, PROFILE, hold_seconds=0.0)
    with_hold = replay.simulate(rows, PROFILE, hold_seconds=45.0)

    assert with_hold["budget"]["occupied"] > without["budget"]["occupied"]
    assert any(t["to"] == "holding" for t in with_hold["transitions"])
    assert not any(t["to"] == "holding" for t in without["transitions"])


def test_the_motion_reference_is_the_product_not_any_motion():
    """`any(motion)` per minute is not what the add-on does. The motion
    reference runs the same machine with the rate switched off."""
    rows = series()
    with_rate = replay.simulate(rows, PROFILE, hold_seconds=0.0)
    motion_only = replay.simulate(rows, PROFILE, hold_seconds=0.0, use_rate=False)

    assert motion_only["uses_rate"] is False
    # Motion is on for 30 s of the four minutes; the rate holds the room
    # for the stretch where somebody sat still.
    assert motion_only["budget"]["occupied"] < with_rate["budget"]["occupied"]
    assert motion_only["budget"]["warming_up"] == 0.0


# --- the time budget adds up -------------------------------------------


def test_every_second_of_the_recording_is_in_the_budget():
    rows = series()
    budget = replay.simulate(rows, PROFILE, hold_seconds=0.0)["budget"]
    parts = sum(value for key, value in budget.items() if key != "total_seconds")
    assert abs(parts - budget["total_seconds"]) < 0.2
    assert budget["total_seconds"] == pytest.approx(239.75, abs=0.1)


def test_ten_seconds_of_material_appear_in_full():
    """Reproduction F: a ten-second recording produced zero judged and
    zero unusable windows, and the time was simply missing from the
    report."""
    rows = [{"t": float(t), "movement_score": 0.0, "motion": False, "label": "empty"}
            for t in range(11)]
    budget = replay.simulate(rows, PROFILE, hold_seconds=0.0)["budget"]

    assert budget["total_seconds"] == 10.0
    assert budget["warming_up"] == 10.0
    parts = sum(value for key, value in budget.items() if key != "total_seconds")
    assert parts == pytest.approx(10.0, abs=0.1)


def test_a_recording_without_timestamps_says_so_rather_than_dividing_by_zero():
    assert replay.simulate([], PROFILE)["available"] is False


# --- provenance --------------------------------------------------------


def session(session_id, device_id="probe", start=0.0, end=100.0, source="recording"):
    return {"id": session_id, "device_id": device_id, "started_at": start,
            "ended_at": end, "source": source}


def test_a_session_named_as_its_own_baseline_is_never_out_of_sample():
    """`baseline_samples is not None` was the whole test, so passing the
    same session explicitly made it look like independent evidence."""
    rows = series()
    one = session("s")
    result = replay.report(one, rows, baseline=one, baseline_samples=rows)
    assert result["identity"]["in_sample"] is True
    assert result["identity"]["same_session"] is True


def test_an_overlapping_baseline_is_not_independent_either():
    """Two imports over the same minutes are two names for one
    measurement."""
    rows = series()
    result = replay.report(
        session("s", start=0.0, end=200.0), rows,
        baseline=session("b", start=150.0, end=400.0), baseline_samples=rows,
    )
    assert result["identity"]["in_sample"] is True
    assert result["identity"]["time_overlap"] is True


def test_a_separate_session_on_the_same_device_is_out_of_sample():
    rows = series()
    result = replay.report(
        session("s", start=0.0, end=200.0), rows,
        baseline=session("b", start=500.0, end=900.0), baseline_samples=rows,
    )
    assert result["identity"]["in_sample"] is False
    assert result["identity"]["cross_device"] is False


def test_a_foreign_device_is_refused_unless_it_is_called_a_transfer_test():
    rows = series()
    with pytest.raises(replay.BaselineRefused):
        replay.report(
            session("s"), rows,
            baseline=session("b", device_id="anderes", start=500.0, end=900.0),
            baseline_samples=rows,
        )

    allowed = replay.report(
        session("s"), rows,
        baseline=session("b", device_id="anderes", start=500.0, end=900.0),
        baseline_samples=rows, transfer=True,
    )
    assert allowed["identity"]["cross_device"] is True


def test_the_report_names_its_parameters():
    """A number without the settings it came from is not a measurement."""
    result = replay.report(session("s"), series())
    parameters = result["parameters"]
    assert parameters["window_seconds"] == presence_rate.DEFAULT_WINDOW_SECONDS
    assert parameters["min_coverage"] == presence_rate.MIN_COVERAGE
    assert parameters["max_sample_gap"] == presence_rate.MAX_SAMPLE_GAP
    assert parameters["rate_memory_seconds"] == presence_rate.RATE_MEMORY_SECONDS


def test_the_window_comparison_is_still_there_and_says_what_it_is():
    result = replay.report(session("s"), series())
    assert result["window_comparison"]["kind"] == "window_comparison"
    assert result["window_comparison"]["strategies"]


# --- the metrics the review asks for -----------------------------------


def test_the_report_measures_wrong_time_and_latency():
    rows = series()
    accuracy = replay.simulate(rows, PROFILE, hold_seconds=0.0)["accuracy"]
    for key in ("false_occupied_seconds", "false_empty_seconds",
                "entry_latency", "release_latency"):
        assert key in accuracy
    assert accuracy["labelled_occupied_seconds"] > 0
    assert accuracy["labelled_empty_seconds"] > 0
    assert accuracy["entry_latency"]["count"] >= 1
