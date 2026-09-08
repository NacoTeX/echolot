"""Comparing detectors on material that already exists.

From the external review (recommended step 8, and the prerequisite for
step 9: new algorithms are to be measured in comparison mode before they
touch anything). The recordings in tests/data are the real ones this
project was built from.
"""

import csv
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import presence_rate, replay  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"


def load(name: str, *, motion=None) -> list[dict]:
    with open(DATA / name) as fh:
        return [
            {
                "t": float(row["t"]),
                "movement_score": float(row["movement_score"]),
                "label": row["label"],
                "motion": motion,
            }
            for row in csv.DictReader(fh)
            if row["movement_score"]
        ]


@pytest.fixture
def session():
    return load("flat_occupied_room_empty.csv") + load("couch_still.csv")


def named(result, name):
    return next(s for s in result["strategies"] if s["name"] == name)


def test_every_window_length_is_reported(session):
    result = replay.compare(session)
    names = [strategy["name"] for strategy in result["strategies"]]
    assert names == ["rate_15s", "rate_30s", "rate_60s", "rate_120s", "motion_only"]


def test_the_shipping_detector_is_marked_as_the_reference(session):
    result = replay.compare(session)
    references = [s["name"] for s in result["strategies"] if s.get("reference")]
    assert references == [f"rate_{presence_rate.DEFAULT_WINDOW_SECONDS:g}s"]


def test_learning_and_judging_on_the_same_session_says_so(session):
    """Otherwise the numbers measure themselves and read like a result."""
    result = replay.compare(session)
    assert result["in_sample"] is True
    assert "messen sich selbst" in result["caveat"]


def test_a_separate_baseline_is_not_in_sample(session):
    empty = [row for row in session if row["label"] == "empty"]
    result = replay.compare(session, baseline_samples=empty)
    assert result["in_sample"] is False
    assert "andere" in result["caveat"]


def test_unjudgeable_windows_are_counted_not_dropped(session):
    """A success rate that hides its unknowns is not a success rate."""
    result = replay.compare(session)
    for strategy in result["strategies"]:
        if strategy.get("available"):
            assert "unusable_windows" in strategy["score"]

    # The recordings carry no motion column, so the motion strategy has
    # nothing to judge — and says so instead of scoring a perfect zero.
    motion = named(result, "motion_only")
    assert motion["score"]["unusable_windows"] > 0
    assert motion["score"]["empty_windows"] == 0


def test_the_reference_detector_reproduces_its_known_numbers(session):
    """One false alarm in twenty empty windows, no misses on the couch —
    the same numbers DOCS.md reports, arrived at through the replay."""
    result = replay.compare(session)
    reference = named(result, "rate_60s")
    assert reference["labels"]["empty"] == {"windows": 20, "occupied": 1, "unusable": 0}
    assert reference["labels"]["still"] == {"windows": 3, "occupied": 3, "unusable": 0}
    assert reference["score"]["false_alarms"] == 1
    assert reference["score"]["misses"] == 0


def test_a_window_too_long_for_the_material_is_refused_not_guessed(session):
    """learn_baseline needs ten full windows; a twenty-minute recording
    cannot supply ten ten-minute ones."""
    result = replay.compare(session, windows=(600.0,))
    strategy = named(result, "rate_600s")
    assert strategy["available"] is False
    assert "600" in strategy["reason"]


def test_the_rates_are_shares_not_counts(session):
    result = replay.compare(session)
    score = named(result, "rate_60s")["score"]
    assert score["false_alarm_rate"] == round(1 / 20, 3)
    assert score["miss_rate"] == 0.0


def test_motion_is_judged_when_the_recording_has_it():
    """With a motion column the strategy becomes comparable."""
    rows = [
        {"t": index * 0.25, "movement_score": 0.0, "label": "empty", "motion": False}
        for index in range(2500)
    ]
    rows += [
        {"t": 700.0 + index * 0.25, "movement_score": 1.0, "label": "moving", "motion": True}
        for index in range(500)
    ]
    result = replay.compare(rows)
    motion = named(result, "motion_only")
    assert motion["score"]["empty_windows"] > 0
    assert motion["score"]["false_alarms"] == 0
    assert motion["score"]["misses"] == 0


def test_replaying_changes_nothing(session):
    """Read-only is the whole premise: measuring must not move the
    lights."""
    from app import main

    before = dict(main._rate_state)
    snapshot = dict(main.evaluator._snapshots)
    replay.compare(session)
    assert main._rate_state == before
    assert main.evaluator._snapshots == snapshot
