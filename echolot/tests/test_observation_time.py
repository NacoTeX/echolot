"""Observed time has to be measured, not inferred from the gaps.

From the follow-up review (R2, reproductions A and B). A reading proves
the source was alive at that instant and says nothing about any other
instant, so what it stands for either side of itself has to be bounded —
otherwise the arithmetic invents evidence.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import presence_rate  # noqa: E402

REACH = presence_rate.MAX_SAMPLE_GAP / 2.0


def rows(stamps, label="empty", score=0.0):
    return [{"t": t, "movement_score": score, "label": label} for t in stamps]


# --- reproduction A ----------------------------------------------------


def test_a_burst_in_the_middle_does_not_buy_a_whole_minute():
    """The review's reproduction: five readings spanning 0.4 s at t=29.8
    were reported as sixty seconds of observation."""
    burst = rows([29.8 + index * 0.1 for index in range(5)])
    observed = presence_rate.observed_seconds(burst, 0, 60)
    assert observed < 60
    assert observed == pytest.approx(0.4 + 2 * REACH, abs=0.01)


def test_the_same_burst_at_the_edge_and_in_the_middle_are_judged_alike():
    """They were not: the edge burst failed the coverage check and the
    middle one passed it, on the same evidence."""
    middle = presence_rate.observed_seconds(rows([29.8 + i * 0.1 for i in range(5)]), 0, 60)
    edge = presence_rate.observed_seconds(rows([0.0 + i * 0.1 for i in range(5)]), 0, 60)
    assert middle / 60 < presence_rate.MIN_COVERAGE
    assert edge / 60 < presence_rate.MIN_COVERAGE


def test_a_steadily_reporting_room_covers_its_window():
    """0.7 readings a second is what a real living room produced at four
    in the morning; that has to come out as fully observed."""
    steady = rows([index * 1.4 for index in range(43)])
    assert presence_rate.observed_seconds(steady, 0, 60) == 60


def test_the_longest_real_gap_still_counts_as_observed():
    """18.7 s was the largest gap in any real recording here. A quiet room
    is quiet, not missing."""
    quiet = rows([5.0, 23.7, 42.0, 55.0])
    assert presence_rate.observed_seconds(quiet, 0, 60) == 60


def test_a_real_hole_opens_a_real_hole():
    holed = rows([1.0, 2.0, 58.0, 59.0])
    observed = presence_rate.observed_seconds(holed, 0, 60)
    assert observed < 60 * presence_rate.MIN_COVERAGE


def test_no_readings_is_no_observation():
    assert presence_rate.observed_seconds([], 0, 60) == 0.0


def test_unusable_timestamps_are_ignored_rather_than_counted():
    junk = [
        {"t": float("nan"), "movement_score": 0.0},
        {"t": float("inf"), "movement_score": 0.0},
        {"t": None, "movement_score": 0.0},
        {"movement_score": 0.0},
    ]
    assert presence_rate.observed_seconds(junk, 0, 60) == 0.0


def test_observation_never_exceeds_the_window():
    dense = rows([index * 0.1 for index in range(1200)])
    assert presence_rate.observed_seconds(dense, 0, 60) == 60


# --- reproduction B ----------------------------------------------------


def test_occupied_minutes_are_not_counted_as_empty_observation():
    """The review's reproduction: a 600 s recording labelled still for
    twenty seconds of every minute reported 600 s of empty-room
    observation. Two hundred of those seconds had somebody in the room.
    """
    mixed = [
        {
            "t": float(t),
            "movement_score": 0.0,
            "label": "still" if 20 <= t % 60 < 40 else "empty",
        }
        for t in range(601)
    ]
    profile = presence_rate.learn_baseline(mixed)
    # Forty-second empty runs cannot fill a sixty-second window at all, so
    # the honest answer is that this recording does not establish a
    # baseline — not that it establishes one from ten minutes.
    assert profile is None


def test_a_genuinely_continuous_empty_recording_still_learns():
    """The fix must not refuse honest material."""
    steady = rows([index * 0.5 for index in range(2400)])
    profile = presence_rate.learn_baseline(steady)
    assert profile is not None
    assert profile.observed_seconds >= profile.window_count * 60 * presence_rate.MIN_COVERAGE


def test_windows_are_not_built_across_a_label_boundary():
    segments = presence_rate.label_segments(
        rows([0.0, 1.0], label="empty") + rows([2.0, 3.0], label="still")
        + rows([4.0, 5.0], label="empty")
    )
    assert [label for label, _ in segments] == ["empty", "still", "empty"]


def test_segments_come_back_in_time_order_however_they_arrive():
    shuffled = rows([5.0], "empty") + rows([1.0], "empty") + rows([3.0], "still")
    segments = presence_rate.label_segments(shuffled)
    assert [label for label, _ in segments] == ["empty", "still", "empty"]


# --- the live path uses the same contract ------------------------------


def test_the_live_path_states_its_window_rather_than_inferring_it():
    """Taking the span between the first and last reading made the window
    whatever the data happened to fill: four seconds of readings were
    judged as a four-second window and passed."""
    profile = presence_rate.profile_from_dict(
        {
            "crossing_threshold": 1e-3,
            "baseline_rate": 0.084,
            "window_seconds": 60.0,
            "version": presence_rate.PROFILE_VERSION,
        }
    )
    burst = [{"t": index * 0.1, "movement_score": 1.0} for index in range(40)]
    verdict = presence_rate.evaluate(profile, burst)
    assert verdict["available"] is False
    assert verdict["state"] == "warming_up"


def test_live_and_replay_agree_on_the_same_readings():
    """Same evidence, same rate, whichever path asks."""
    profile = presence_rate.profile_from_dict(
        {
            "crossing_threshold": 1e-3,
            "baseline_rate": 0.084,
            "window_seconds": 60.0,
            "version": presence_rate.PROFILE_VERSION,
        }
    )
    readings = [
        {"t": index * 0.5, "movement_score": 0.5 if index % 4 == 0 else 0.0}
        for index in range(121)
    ]
    live = presence_rate.evaluate(profile, readings)
    window = presence_rate.split_windows(readings, 60.0)[0]
    replayed = presence_rate.evaluate(profile, window.samples, window=window)
    assert live["available"] and replayed["available"]
    assert live["rate"] == pytest.approx(replayed["rate"], rel=0.05)
