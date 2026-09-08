"""Importing a past stretch of Home Assistant history as a calibration.

The reason this exists is that the measurement the rate detector needs —
ten-plus minutes of the room being empty — is also the one nobody wants
to sit through, and the recorder already made it. The tests therefore
lean on a real recording out of a real Home Assistant instance rather
than on synthesised numbers: tests/data/recorder_room_empty_night.json
is thirteen minutes of a living room at four in the morning.
"""

import asyncio
import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import calibration, ha_client, history_import, presence_rate  # noqa: E402
from app.telemetry import Sample  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"


def state(stamp: str, value):
    return {"state": str(value), "last_changed": stamp}


def at(second: float, value) -> dict:
    base = datetime(2026, 9, 8, 4, 0, tzinfo=timezone.utc)
    return state((base + timedelta(seconds=second)).isoformat(), value)


# --- merging the three series ------------------------------------------


def test_the_score_drives_the_timeline():
    samples = history_import.merge(
        [at(0, 0.1), at(1, 0.2), at(2, 0.3)], [], []
    )
    assert [round(s.movement_score, 1) for s in samples] == [0.1, 0.2, 0.3]


def test_the_threshold_is_carried_forward_to_every_sample():
    """The bug 0.13.1 fixed on the live path, reached from the other side.

    A threshold that has not changed all week appears once in the history
    — at the start of the window. Read as parallel rows, every later
    sample would have no threshold at all.
    """
    samples = history_import.merge(
        [at(0, 0.1), at(30, 0.2), at(600, 0.3)], [], [at(0, 0.5)]
    )
    assert [s.threshold for s in samples] == [0.5, 0.5, 0.5]


def test_a_later_threshold_change_applies_only_from_then_on():
    samples = history_import.merge(
        [at(0, 0.1), at(10, 0.2), at(20, 0.3)], [], [at(0, 0.5), at(15, 0.66)]
    )
    assert [s.threshold for s in samples] == [0.5, 0.5, 0.66]


def test_motion_is_carried_forward_as_a_boolean():
    samples = history_import.merge(
        [at(0, 0.1), at(10, 0.2), at(20, 0.3)],
        [at(5, "on"), at(15, "off")],
        [],
    )
    assert [s.motion for s in samples] == [None, True, False]


def test_states_arriving_out_of_order_are_sorted_first():
    samples = history_import.merge([at(2, 0.3), at(0, 0.1), at(1, 0.2)], [], [])
    assert [s.t for s in samples] == sorted(s.t for s in samples)


def test_unusable_states_are_dropped_rather_than_becoming_zero():
    samples = history_import.merge(
        [at(0, "unavailable"), at(1, 0.2), at(2, "unknown")], [], []
    )
    assert [s.movement_score for s in samples] == [0.2]


def test_a_state_without_a_timestamp_is_ignored():
    samples = history_import.merge([{"state": "0.4"}, at(1, 0.2)], [], [])
    assert len(samples) == 1


# --- the range guard ----------------------------------------------------


def test_a_backwards_range_is_refused():
    now = datetime.now(timezone.utc)
    with pytest.raises(history_import.RangeRejected):
        history_import.check_range(now, now - timedelta(minutes=5))


def test_a_range_of_seconds_is_refused():
    now = datetime.now(timezone.utc)
    with pytest.raises(history_import.RangeRejected):
        history_import.check_range(now, now + timedelta(seconds=5))


def test_a_range_of_hours_is_refused_before_anything_is_fetched():
    now = datetime.now(timezone.utc)
    with pytest.raises(history_import.RangeRejected) as err:
        history_import.check_range(now, now + timedelta(hours=4))
    assert "90" in str(err.value)


def test_an_hour_is_accepted():
    now = datetime.now(timezone.utc)
    assert history_import.check_range(now, now + timedelta(hours=1)) == 3600


# --- the query Home Assistant actually receives -------------------------


class RecordingHandler(BaseHTTPRequestHandler):
    """Answers /history/period and remembers what it was asked."""

    seen: list = []

    def do_GET(self):
        parsed = urlparse(self.path)
        RecordingHandler.seen.append((parsed.path, parse_qs(parsed.query, keep_blank_values=True)))
        body = json.dumps([[{"state": "0.25", "last_changed": "2026-09-08T04:00:00+00:00"}]])
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def recorder(monkeypatch):
    RecordingHandler.seen = []
    server = HTTPServer(("127.0.0.1", 0), RecordingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SUPERVISOR_TOKEN", "token")
    monkeypatch.setattr(
        ha_client, "_base_url", lambda: f"http://127.0.0.1:{server.server_port}"
    )
    asyncio.get_event_loop_policy().new_event_loop()
    yield RecordingHandler
    asyncio.run(ha_client.close_client())
    server.shutdown()
    server.server_close()


def test_the_request_switches_off_significant_changes_only(recorder):
    """The one parameter that is not a presence flag.

    Home Assistant reads it as `query.get(name, "1") != "0"`, so the empty
    value used by `minimal_response` beside it would leave the filter on
    and quietly drop readings — and a rate per second of wall time would
    then measure whatever survived the filter.
    """
    start = datetime(2026, 9, 8, 4, 0, tzinfo=timezone.utc)
    asyncio.run(
        ha_client.get_history_range("sensor.s", start, start + timedelta(minutes=10))
    )
    path, query = recorder.seen[-1]
    assert path.endswith("/history/period/2026-09-08T04:00:00+00:00")
    assert query["significant_changes_only"] == ["0"]
    assert query["end_time"] == ["2026-09-08T04:10:00+00:00"]
    assert query["filter_entity_id"] == ["sensor.s"]


# --- storing the result -------------------------------------------------


def test_an_imported_session_is_complete_and_labelled():
    samples = [Sample(t=100.0 + i, movement_score=0.1, threshold=0.5, motion=False) for i in range(5)]
    session = calibration.store.adopt("dev-a", samples, label="empty")
    assert session["status"] == "complete"
    assert session["source"] == "history"
    assert session["label_counts"]["empty"] == 5
    assert session["started_at"] == 100.0
    assert session["ended_at"] == 104.0
    calibration.store.delete(session["id"])


def test_importing_does_not_collide_with_a_live_recording():
    """`create` refuses while another recording runs. Importing the past
    has no reason to wait for the present."""
    live = calibration.store.create("dev-b")
    try:
        session = calibration.store.adopt(
            "dev-b", [Sample(t=1.0, movement_score=0.1, threshold=0.5, motion=False)],
            label="empty",
        )
        calibration.store.delete(session["id"])
    finally:
        calibration.store.delete(live["id"])


def test_an_empty_range_is_refused_rather_than_stored():
    with pytest.raises(ValueError):
        calibration.store.adopt("dev-c", [], label="empty")


def test_an_unknown_label_is_refused():
    with pytest.raises(ValueError):
        calibration.store.adopt(
            "dev-d", [Sample(t=1.0, movement_score=0.1, threshold=0.5, motion=False)],
            label="erfunden",
        )


# --- the real recording -------------------------------------------------


def load_night() -> list:
    return json.loads((DATA / "recorder_room_empty_night.json").read_text())


def test_the_real_night_history_becomes_a_usable_baseline():
    """Thirteen minutes of a living room at 04:00, straight from a real
    recorder, through the real merge and the real learner."""
    samples = history_import.merge(load_night(), [], [{"state": "0.5", "last_changed": "2026-09-08T03:00:00+00:00"}])
    assert len(samples) > 500

    rows = [{**s.as_dict(), "label": "empty"} for s in samples]
    profile = presence_rate.learn_baseline(rows)
    assert profile is not None
    assert profile.window_count >= presence_rate.MIN_BASELINE_WINDOWS
    assert profile.suspect_windows == 0


def test_the_empty_room_at_night_never_crosses():
    """Not "rarely" — 22 of 22 windows at exactly zero when this was
    measured. The floor in MIN_BASELINE_RATE is what keeps that from
    becoming a baseline of nothing, and this is the recording that shows
    why the floor is needed."""
    samples = history_import.merge(load_night(), [], [])
    rows = [{**s.as_dict(), "label": "empty"} for s in samples]
    windows = presence_rate.split_windows(rows, presence_rate.DEFAULT_WINDOW_SECONDS)
    rates = [w.rate(presence_rate.DEFAULT_CROSSING_THRESHOLD) for w in windows]
    assert rates and max(rates) == 0.0

    profile = presence_rate.learn_baseline(rows)
    assert profile.baseline_rate == presence_rate.MIN_BASELINE_RATE
