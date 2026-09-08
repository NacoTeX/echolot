"""Presence from the crossing rate, checked against the recordings it came from.

tests/data holds three real sessions from an ESP32-C5 in a living room:
five minutes with one person sitting still on the couch, two and a half
minutes of the same room empty (its last minute occupied, which is how
the contamination check came about), and twenty minutes of the room empty
while somebody moved about the rest of the flat. Every threshold and
ratio in app/presence_rate.py was derived from them, so they are the
fixtures — an invented distribution would only confirm the arithmetic.
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
    """The short, partly occupied recording."""
    return load("room_empty.csv")


@pytest.fixture
def clean_baseline():
    """Twenty minutes, room empty, somebody moving about the flat.

    The operational baseline: what matters is not an empty building but a
    room nobody is in while life goes on elsewhere.
    """
    return load("flat_occupied_room_empty.csv")


@pytest.fixture
def next_door():
    return [row for row in load("flat_occupied_room_empty.csv")
            if row["label"] == "interference"]


@pytest.fixture
def still():
    return load("couch_still.csv")


@pytest.fixture
def profile(clean_baseline):
    learned = presence_rate.learn_baseline(clean_baseline)
    assert learned is not None
    return learned


# --- what the empty room teaches -------------------------------------------


def test_the_baseline_matches_what_was_measured(profile):
    """The rate the room has to be beaten by, in crossings a second."""
    assert 0.05 < profile.baseline_rate < 0.15
    assert profile.crossing_threshold == 1e-3
    assert profile.window_count == 20


def test_the_percentile_survives_a_baseline_that_is_mostly_silent(clean_baseline):
    """Fourteen of twenty windows crossed exactly zero times. The median of
    that is 0.0 and says nothing at all, so the level would fall back to
    the floor and one stray crossing would read as presence. The
    ninetieth percentile is 0.084 — what the room reached on the windows
    where something did happen."""
    learned = presence_rate.learn_baseline(clean_baseline)
    assert learned.baseline_rate > presence_rate.MIN_BASELINE_RATE * 2
    assert 0.05 < learned.baseline_rate < 0.15


def test_the_clean_baseline_separates_the_couch_from_the_empty_room(
    clean_baseline, still, next_door
):
    """The operating point, on the recordings it was chosen from."""
    learned = presence_rate.learn_baseline(clean_baseline)
    empty_rows = [row for row in clean_baseline if row["label"] == "empty"]

    def occupied(rows):
        windows = presence_rate.split_windows(rows, learned.window_seconds)
        return (
            sum(1 for w in windows if presence_rate.evaluate(learned, w.samples, window=w)["occupied"]),
            len(windows),
        )

    # Three of three, not three of four. The fourth "window" was the
    # trailing fragment of the recording and is no longer a window: a rate
    # from part of a minute is not comparable with a rate from a whole
    # one. The known miss from 0.13.1 is gone because the fragment is
    # gone, not because anything detects better.
    assert occupied(still) == (3, 3), "eine Person auf der Couch"
    assert occupied(empty_rows) == (1, 20), "leerer Raum, Wohnung belegt"


def test_walking_around_the_flat_is_far_quieter_than_sitting_in_the_room(
    clean_baseline, still
):
    """The question the whole per-room idea depends on: does the signal come
    through the wall? Twenty minutes of moving about the rest of the flat
    crossed at 0.027 a second against 0.261 sitting still in the room."""
    empty_rows = [row for row in clean_baseline if row["label"] == "empty"]
    away = presence_rate.event_rate(empty_rows, presence_rate.DEFAULT_CROSSING_THRESHOLD)
    inside = presence_rate.event_rate(still, presence_rate.DEFAULT_CROSSING_THRESHOLD)
    assert inside > away * 5


def test_the_enter_level_clears_the_baseline_by_ratio_and_by_margin(profile):
    assert profile.enter_rate >= profile.baseline_rate * presence_rate.DEFAULT_ENTER_RATIO
    assert profile.enter_rate >= profile.baseline_rate + presence_rate.ENTER_MARGIN
    assert profile.exit_rate <= profile.enter_rate


def test_a_very_quiet_room_is_not_tripped_by_two_stray_crossings():
    """Twice a very small number is still a very small number, which is
    what the absolute margin is for."""
    # Ten full windows needs a little over ten minutes: the grid drops the
    # trailing fragment, so exactly 600 s would give nine.
    silent = [{"t": i * 0.25, "movement_score": 0.0, "label": "empty"} for i in range(2500)]
    learned = presence_rate.learn_baseline(silent)
    two_crossings = [
        {"t": i * 0.25, "movement_score": 1.0 if i < 2 else 0.0} for i in range(240)
    ]
    assert presence_rate.evaluate(learned, two_crossings)["occupied"] is False


def test_too_little_data_yields_no_profile():
    assert presence_rate.learn_baseline([]) is None
    assert presence_rate.learn_baseline(
        [{"t": i, "movement_score": 0.0, "label": "empty"} for i in range(10)]
    ) is None


def test_a_short_baseline_is_refused_rather_than_answered_wrongly(empty):
    """Two and a half minutes is three windows, and the ninetieth
    percentile of three values is the largest of them. This recording's
    last minute was occupied, so a profile from it would have taken 0.393
    — the contaminated window — as "what the empty room does", and gone
    deaf. Refusing is the only safe answer."""
    assert presence_rate.learn_baseline(empty) is None


def test_samples_of_other_labels_are_not_baseline(still):
    assert presence_rate.learn_baseline(still) is None


def test_a_silent_room_does_not_get_an_infinite_ratio():
    """Without a floor, one stray crossing in a never-crossing room reads
    as certain presence."""
    quiet = [
        {"t": i * 0.25, "movement_score": 0.0, "label": "empty"} for i in range(6000)
    ]
    learned = presence_rate.learn_baseline(quiet)
    assert learned is not None
    assert learned.baseline_rate >= presence_rate.MIN_BASELINE_RATE


# --- and what it decides ---------------------------------------------------


def windows(samples, seconds=presence_rate.DEFAULT_WINDOW_SECONDS):
    return presence_rate.split_windows(samples, seconds)


def test_the_couch_reads_as_occupied(profile, still):
    verdicts = [presence_rate.evaluate(profile, w.samples, window=w)["occupied"] for w in windows(still)]
    assert verdicts, "keine vollen Fenster in der Aufnahme"
    # All three full windows. The fourth used to be counted and used to be
    # the miss; it was the trailing fragment of the recording, which is no
    # longer a window at all.
    assert sum(bool(v) for v in verdicts) == 3
    assert len(verdicts) == 3


def test_the_empty_room_stays_empty_while_the_flat_is_used(profile, clean_baseline):
    """One window in twenty. It crossed at 0.286 a second — couch
    territory, and almost certainly somebody walking past the door, which
    is why the profile flags it as suspect too."""
    empty_rows = [row for row in clean_baseline if row["label"] == "empty"]
    verdicts = [presence_rate.evaluate(profile, w.samples, window=w)["occupied"] for w in windows(empty_rows)]
    assert len(verdicts) == 20
    assert sum(bool(v) for v in verdicts) == 1


def test_someone_at_the_door_cannot_be_judged_from_nine_seconds(profile, next_door):
    """A correction to this project's own headline numbers.

    "1 von 1 Fenstern erkannt, 3,03 Überschreitungen/s" came from nine
    seconds of recording divided by the span between its first and last
    reading. Nine seconds is not a minute, and the rate was a burst
    extrapolated to a second. Under a fixed grid there is no window here
    at all, and the honest answer is that this recording does not say.
    """
    span = next_door[-1]["t"] - next_door[0]["t"]
    assert span < 15, "die Aufnahme ist kürzer als ein Fenster"
    assert windows(next_door) == []


def test_a_contaminated_baseline_is_flagged(profile):
    assert profile.suspect_windows == 1
    assert profile.window_count == 20
    assert "jemand im Raum" in (profile.warning or "")


def test_a_clean_baseline_carries_no_warning():
    clean = [
        {"t": i * 0.25, "movement_score": 0.02 if i % 400 == 0 else 0.0, "label": "empty"}
        for i in range(6000)
    ]
    learned = presence_rate.learn_baseline(clean)
    assert learned.suspect_windows == 0
    assert learned.warning is None


def test_the_verdict_explains_itself(profile, still):
    """In events per second, not as a share of the readings.

    The old wording said "% der Messwerte" for a per-second rate, so a
    burst at 12.5/s was reported as "1250 % der Messwerte" — the right
    number under a sentence that was not true of it.
    """
    first = windows(still)[0]
    result = presence_rate.evaluate(profile, first.samples, window=first)
    assert "Ereignisse/s" in result["reason"]
    assert "%" not in result["reason"]
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
    assert result["state"] == "warming_up"


def test_a_burst_is_not_a_window(profile):
    """From the external review (P1 #4).

    Five readings inside four tenths of a second were accepted and
    reported 12.5 events a second — a confident answer from a fifth of a
    second of evidence, because the divisor was the span between the
    first and last reading rather than the window.
    """
    burst = [{"t": i * 0.1, "movement_score": 1.0} for i in range(5)]
    result = presence_rate.evaluate(profile, burst)
    assert result["available"] is False
    assert result["state"] == "warming_up"
    assert result["occupied"] is None


def test_a_hole_in_the_data_is_not_a_quiet_minute(profile):
    """A window that elapsed with nothing behind it for most of it is a
    gap, and reporting it as a low rate would be reporting an empty room
    every time the connection drops."""
    rows = [{"t": i * 0.25, "movement_score": 0.0} for i in range(40)]   # 10 s
    rows += [{"t": 100.0 + i * 0.25, "movement_score": 0.0} for i in range(40)]
    result = presence_rate.evaluate(profile, rows)
    assert result["available"] is False
    assert result["state"] == "gap"


def test_a_genuinely_quiet_minute_is_still_a_measurement(profile):
    """The other half of the same rule: a room that reports steadily and
    never crosses is quiet, not missing."""
    rows = [{"t": i * 0.25, "movement_score": 0.0} for i in range(240)]
    result = presence_rate.evaluate(profile, rows)
    assert result["available"] is True
    assert result["occupied"] is False
    assert result["state"] == "ok"


def _window_with_rate(rate: float, threshold: float) -> list[dict]:
    """A sixty-second window crossing at the given rate per second."""
    total = 240                      # 4 Hz, the device's actual cadence
    crossings = round(rate * 60.0)
    return [
        {"t": i * 0.25, "movement_score": threshold * 2 if i < crossings else 0.0}
        for i in range(total)
    ]


def test_the_rate_is_per_second_not_per_reading():
    """Home Assistant sends a message when a value changes, and the score
    sits at zero for long stretches — one twenty-minute recording had gaps
    up to 18.7 s. Counting per reading makes a quiet minute that produced
    four readings weigh as much as a busy one that produced two hundred,
    which flatters exactly the periods that should look quiet.
    """
    busy = [{"t": i * 0.25, "movement_score": 1.0} for i in range(240)]
    quiet = [{"t": i * 6.0, "movement_score": 1.0} for i in range(10)]
    assert presence_rate.event_rate(busy, 0.5) == pytest.approx(4.0, rel=0.02)
    assert presence_rate.event_rate(quiet, 0.5) == pytest.approx(0.185, rel=0.05)


def test_a_single_reading_cannot_establish_a_rate():
    assert presence_rate.event_rate([{"t": 1.0, "movement_score": 1.0}], 0.5) == 0.0
    assert presence_rate.event_rate([], 0.5) == 0.0


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
            for row in load("flat_occupied_room_empty.csv") + load("couch_still.csv")
        ]

    client = TestClient(server.app)
    body = client.get(f"/api/calibrations/{session['id']}/presence-rate").json()

    assert 0.05 < body["profile"]["baseline_rate"] < 0.15
    assert body["profile"]["suspect_windows"] == 1
    assert body["labels"]["still"]["occupied_windows"] == 3
    assert body["labels"]["empty"]["occupied_windows"] == 1
    # Nine seconds of "somebody at the door" no longer produces a window
    # at all, and the route says so rather than reporting a rate from a
    # burst. The count of unusable windows is part of the answer.
    assert body["labels"]["interference"]["windows"] == 0
    assert body["labels"]["interference"]["occupied_windows"] == 0


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
