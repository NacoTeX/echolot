"""Direct ESPectre telemetry parsing, buffering, and fan-out."""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.telemetry import MAX_POINTS, TelemetryHub, parse_payload  # noqa: E402


def test_current_and_compatible_payload_names_are_accepted():
    current = parse_payload(
        '{"movement_score":2.5,"threshold":1.25,"motion":true,"timestamp":1700000000}'
    )
    compatible = parse_payload(
        '{"data":{"movement":"3.5","detection_threshold":"2","detected":"on"}}',
        now=12,
    )

    assert current.as_dict() == {
        "t": 1_700_000_000.0,
        "movement_score": 2.5,
        "threshold": 1.25,
        "motion": True,
    }
    assert compatible.as_dict() == {
        "t": 12,
        "movement_score": 3.5,
        "threshold": 2.0,
        "motion": True,
    }


def test_heartbeats_and_invalid_json_do_not_become_clear_samples():
    assert parse_payload(": keepalive") is None
    assert parse_payload('{"event":"connected"}') is None
    assert parse_payload("[]") is None


def test_millisecond_timestamps_are_normalised():
    sample = parse_payload('{"score":1,"t":1700000000000}')
    assert sample.t == 1_700_000_000


def test_device_uptime_is_replaced_with_comparable_arrival_time():
    sample = parse_payload('{"score":1,"timestamp":42}', now=1_700_000_001)
    assert sample.t == 1_700_000_001


def test_history_is_bounded_and_old_samples_can_be_filtered():
    hub = TelemetryHub()
    started = time.time()
    for number in range(MAX_POINTS + 5):
        hub.ingest("probe", json.dumps({"score": number}), now=started + number / 1000)

    all_points = hub.snapshot("probe", seconds=86_400)["points"]
    assert len(all_points) == MAX_POINTS
    assert all_points[0]["movement_score"] == 5


def test_subscribers_receive_samples_without_blocking_collection():
    hub = TelemetryHub()
    queue = hub.subscribe("probe")
    sample = hub.ingest("probe", '{"score":4.2,"motion":true}', now=100)

    assert queue.get_nowait() == sample
    hub.unsubscribe("probe", queue)
    assert "probe" not in hub._subscribers


def test_a_slow_subscriber_gets_recent_data_instead_of_an_unbounded_queue():
    hub = TelemetryHub()
    queue = hub.subscribe("probe")
    for number in range(150):
        hub.ingest("probe", json.dumps({"score": number}), now=number + 1)

    assert queue.qsize() == 100
    newest = None
    while not queue.empty():
        newest = queue.get_nowait()
    assert newest.movement_score == 149
