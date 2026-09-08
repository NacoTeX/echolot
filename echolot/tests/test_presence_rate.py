"""Presence from the crossing rate, checked against the recordings it came from.

tests/data holds two real sessions from an ESP32-C5 in a living room: five
minutes with one person sitting still on the couch, and two and a half
minutes of the same room empty. Every threshold and ratio in
app/presence_rate.py was derived from them, so they are the fixtures — an
invented distribution would only confirm the arithmetic.
"""

import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import presence_rate  # noqa: E402

DATA = Path(__file__).parent / "data"


def load(name: str) -> list[dict]:
    with open(DATA / name) as fh:
        return [
            {
                "t": float(row["t"]),
                "movement_score": float(row["movement_score"]),
                "label": row["label"],
            }
            for row in csv.DictReader(fh)
            if row["movement_score"]
        ]


@pytest.fixture
def empty():
    return load("room_empty.csv")


@pytest.fixture
def still():
    return load("couch_still.csv")


@pytest.fixture
def profile(empty):
    learned = presence_rate.learn_baseline(empty)
    assert learned is not None
    return learned


# --- what the empty room teaches -------------------------------------------


def test_the_baseline_matches_what_was_measured(profile):
    """About 4 % of readings cross 1e-3 in an empty room. That number is
    the whole point: it is what presence has to beat."""
    assert 0.01 < profile.baseline_rate < 0.12
    assert profile.crossing_threshold == 1e-3
    assert profile.sample_count == 199


def test_the_enter_level_sits_above_the_empty_rooms_own_variation(profile):
    assert profile.enter_rate > profile.baseline_rate
    assert profile.exit_rate <= profile.enter_rate


def test_too_little_data_yields_no_profile():
    """A handful of readings cannot say how much an empty room varies, and
    a profile invented from them would be worse than none."""
    assert presence_rate.learn_baseline([]) is None
    assert presence_rate.learn_baseline(
        [{"t": i, "movement_score": 0.0, "label": "empty"} for i in range(10)]
    ) is None


def test_samples_of_other_labels_are_not_baseline(still):
    assert presence_rate.learn_baseline(still) is None


def test_a_silent_room_does_not_get_an_infinite_ratio():
    """Without a floor, one stray crossing in a never-crossing room reads
    as certain presence."""
    quiet = [
        {"t": i * 0.5, "movement_score": 0.0, "label": "empty"} for i in range(400)
    ]
    learned = presence_rate.learn_baseline(quiet)
    assert learned is not None
    assert learned.baseline_rate >= presence_rate.MIN_BASELINE_RATE


# --- and what it decides ---------------------------------------------------


def windows(samples, seconds=presence_rate.DEFAULT_WINDOW_SECONDS):
    return presence_rate._split_windows(samples, seconds)


def test_the_couch_reads_as_occupied(profile, still):
    verdicts = [presence_rate.evaluate(profile, window)["occupied"] for window in windows(still)]
    assert verdicts, "keine vollen Fenster in der Aufnahme"
    # Not every window: the person sat still, and stillness is exactly the
    # hard case. A clear majority is the honest bar here.
    assert sum(bool(v) for v in verdicts) > len(verdicts) / 2


def test_the_empty_room_reads_as_empty_where_it_was_empty(profile, empty):
    """The last window of this recording is not empty.

    Its three windows cross at 0.014, 0.038 and 0.217 — the last as often
    as the couch does. The spikes begin about a hundred seconds in and run
    to the end: somebody walked back into the room while the "empty"
    recording was still going. The detector calling that window occupied
    is the detector being right, so the test says so rather than demanding
    the wrong answer.
    """
    verdicts = [presence_rate.evaluate(profile, window)["occupied"] for window in windows(empty)]
    assert verdicts[:2] == [False, False]
    assert verdicts[2] is True


def test_a_contaminated_baseline_is_flagged_rather_than_silently_used(profile):
    """One occupied window in three moved the mean rate above every honest
    window. The median survives it; the user still needs telling."""
    assert profile.suspect_windows == 1
    assert profile.window_count == 3
    assert "jemand im Raum" in (profile.warning or "")


def test_a_clean_baseline_carries_no_warning():
    clean = [
        {"t": i * 0.5, "movement_score": 0.02 if i % 30 == 0 else 0.0, "label": "empty"}
        for i in range(600)
    ]
    learned = presence_rate.learn_baseline(clean)
    assert learned.suspect_windows == 0
    assert learned.warning is None


def test_the_median_ignores_a_minority_of_occupied_windows(empty):
    """The number that matters: 0.038, what the room does when empty —
    not 0.090, the average of two empty windows and one occupied one."""
    profile = presence_rate.learn_baseline(empty)
    assert profile.baseline_rate < 0.06


def test_the_verdict_explains_itself(profile, still):
    result = presence_rate.evaluate(profile, windows(still)[0])
    assert "%" in result["reason"]
    assert "Faktor" in result["reason"]
    assert result["samples"] > 5


def test_hysteresis_makes_leaving_harder_than_it_looks(profile):
    """A rate between the exit and enter levels keeps whichever verdict it
    already had, so a rate hovering at the line does not chatter."""
    between = (profile.exit_rate + profile.enter_rate) / 2
    borderline = _window_with_rate(between, profile.crossing_threshold)

    assert presence_rate.evaluate(profile, borderline, occupied_now=True)["occupied"] is True
    assert presence_rate.evaluate(profile, borderline, occupied_now=False)["occupied"] is False


def test_a_window_with_too_little_data_says_so_rather_than_empty(profile):
    """A dropped connection must not be reported as an empty room."""
    result = presence_rate.evaluate(profile, [{"t": 1, "movement_score": 1.0}])
    assert result["available"] is False
    assert result["occupied"] is None
    assert "zu wenige" in result["reason"]


def _window_with_rate(rate: float, threshold: float) -> list[dict]:
    total = 100
    crossings = round(rate * total)
    return [
        {"t": i * 0.5, "movement_score": threshold * 2 if i < crossings else 0.0}
        for i in range(total)
    ]


def test_the_crossing_rate_is_a_share_not_a_count():
    """Windows hold different numbers of readings; a count would make a
    busy window look occupied for the wrong reason."""
    dense = [{"t": i * 0.1, "movement_score": 1.0} for i in range(100)]
    sparse = [{"t": i * 1.0, "movement_score": 1.0} for i in range(10)]
    assert presence_rate.crossing_rate(dense, 0.5) == presence_rate.crossing_rate(sparse, 0.5) == 1.0


# --- through the API --------------------------------------------------------


def test_the_route_reproduces_the_analysis(tmp_path, monkeypatch):
    """The hand analysis that produced this module, done by the product.

    Feeds both real recordings into a session and asks the route what it
    makes of them: an empty-room rate, a warning that the baseline is
    partly occupied, and the couch scoring above it.
    """
    import os
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(tmp_path))
    from app import calibration, feature_api, server

    store = calibration.CalibrationStore()
    monkeypatch.setattr(calibration, "store", store)
    monkeypatch.setattr(feature_api.calibration, "store", store)

    session = store.create("probe", name="Analyse")
    with store._lock:  # noqa: SLF001 - loading a recorded session, not a live one
        store._sessions[session["id"]]["samples"] = [
            {**row, "threshold": 0.5, "motion": False}
            for row in load("room_empty.csv") + load("couch_still.csv")
        ]

    client = TestClient(server.app)
    body = client.get(f"/api/calibrations/{session['id']}/presence-rate").json()

    assert body["profile"]["baseline_rate"] < 0.06
    assert body["profile"]["suspect_windows"] == 1
    assert body["labels"]["still"]["occupied_windows"] >= 3
    assert body["labels"]["empty"]["occupied_windows"] == 1


def test_a_session_without_an_empty_label_says_what_is_missing(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(tmp_path))
    from app import calibration, feature_api, server

    store = calibration.CalibrationStore()
    monkeypatch.setattr(calibration, "store", store)
    monkeypatch.setattr(feature_api.calibration, "store", store)
    session = store.create("probe")
    with store._lock:  # noqa: SLF001
        store._sessions[session["id"]]["samples"] = [
            {**row, "threshold": 0.5, "motion": False} for row in load("couch_still.csv")
        ]

    response = TestClient(server.app).get(
        f"/api/calibrations/{session['id']}/presence-rate"
    )
    assert response.status_code == 409
    assert "Raum leer" in response.json()["detail"]


def test_an_unknown_session_is_a_404(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(tmp_path))
    from app import server

    assert TestClient(server.app).get(
        "/api/calibrations/gibtsnicht/presence-rate"
    ).status_code == 404
