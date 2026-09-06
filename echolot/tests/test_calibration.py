"""Ground-truth recording and explainable calibration recommendations."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import calibration  # noqa: E402
from app.telemetry import Sample  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(tmp_path))
    return calibration.CalibrationStore()


def sample(score: float, stamp: float = 100) -> Sample:
    return Sample(t=stamp, movement_score=score, threshold=2.0, motion=score >= 2)


def test_a_session_records_the_ground_truth_active_at_sample_time(store):
    session = store.create("probe", "Wohnzimmer")
    store.set_label(session["id"], "empty")
    store.ingest("probe", sample(0.2))
    store.set_label(session["id"], "still")
    store.ingest("probe", sample(3.0, 101))
    finished = store.stop(session["id"])

    assert finished["status"] == "complete"
    assert finished["label_counts"]["empty"] == 1
    assert finished["label_counts"]["still"] == 1
    assert finished["sample_count"] == 2


def test_samples_from_another_device_are_not_mixed_into_the_session(store):
    session = store.create("probe")
    store.set_label(session["id"], "empty")
    store.ingest("other", sample(9))
    assert store.get(session["id"])["sample_count"] == 0


def test_recommendation_requires_both_empty_and_occupied_evidence():
    assert calibration.recommendation(
        [{"label": "empty", "movement_score": 0.2}] * 100
    ) is None


def test_recommendation_separates_a_clean_room_dataset():
    rows = (
        [{"label": "empty", "movement_score": 0.2 + (i % 4) * 0.01} for i in range(100)]
        + [{"label": "still", "movement_score": 2.0 + (i % 5) * 0.05} for i in range(60)]
        + [{"label": "moving", "movement_score": 4.0 + (i % 5) * 0.1} for i in range(60)]
        + [{"label": "interference", "movement_score": 9.0} for _ in range(30)]
    )
    result = calibration.recommendation(rows)

    assert result["quality"] == "good"
    assert 0.2 < result["exit_threshold"] <= result["enter_threshold"] < 2.1
    assert result["estimated_false_positive_rate"] == 0
    assert result["estimated_false_negative_rate"] < 0.1
    assert result["empty_samples"] == 100
    assert result["occupied_samples"] == 120


def test_only_one_recording_can_run_at_a_time(store):
    store.create("one")
    with pytest.raises(ValueError, match="bereits"):
        store.create("two")


def test_a_restart_marks_an_open_recording_as_interrupted(store, tmp_path):
    session = store.create("probe")
    reloaded = calibration.CalibrationStore()
    assert reloaded.get(session["id"])["status"] == "interrupted"

    persisted = json.loads((tmp_path / "calibration_sessions.json").read_text())
    assert persisted[session["id"]]["ended_at"] is not None


def test_csv_export_contains_labels_and_measurements(store):
    session = store.create("probe")
    store.set_label(session["id"], "moving")
    store.ingest("probe", sample(4.2))
    exported = store.csv(session["id"])

    assert exported.startswith("t,movement_score,threshold,motion,label")
    assert "100,4.2,2.0,True,moving" in exported


def test_delete_removes_a_session(store):
    session = store.create("probe")
    assert store.delete(session["id"]) is True
    assert store.get(session["id"]) is None
    assert store.delete(session["id"]) is False
