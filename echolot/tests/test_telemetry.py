"""Direct ESPectre telemetry parsing, buffering, and fan-out."""

import json
import socket
import sys

import httpx
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import telemetry  # noqa: E402
from app.telemetry import MAX_POINTS, TelemetryHub, parse_payload  # noqa: E402


def test_current_and_compatible_payload_names_are_accepted():
    current = parse_payload(
        '{"movement_score":2.5,"threshold":1.25,"motion":true,"timestamp":1700000000}'
    )
    compatible = parse_payload(
        '{"data":{"movement":"3.5","detection_threshold":"2","detected":"on"}}',
        now=12,
    )

    # `source` is filled in by the bus, not by the parser: a payload says
    # what was measured, not which transport carried it.
    assert current.as_dict() == {
        "t": 1_700_000_000.0,
        "movement_score": 2.5,
        "threshold": 1.25,
        "motion": True,
        "source": None,
    }
    assert compatible.as_dict() == {
        "t": 12,
        "movement_score": 3.5,
        "threshold": 2.0,
        "motion": True,
        "source": None,
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


def test_recording_listeners_receive_each_valid_sample_only():
    hub = TelemetryHub()
    received = []
    listener = lambda device_id, sample: received.append((device_id, sample.movement_score))
    hub.add_listener(listener)

    hub.ingest("probe", '{"score":1.5}', now=100)
    hub.ingest("probe", '{"event":"heartbeat"}', now=101)
    hub.remove_listener(listener)
    hub.ingest("probe", '{"score":2.5}', now=102)

    assert received == [("probe", 1.5)]


# --- ESPectre's real wire format ------------------------------------------
#
# Everything below is taken from the firmware sources rather than imagined,
# after three Calibration Lab recordings came back with a header row and no
# samples. The adapter had been written against a guess.


def test_espectres_own_events_endpoint_is_tried_first():
    """`runtime/direct_http_protocol.h`:

        ESPECTRE_DIRECT_HTTP_EVENTS_ENDPOINT = "/espectre/v1/events"

    None of the four paths this module shipped with matched it, so the
    collector took a 404 on every one and recorded nothing at all.
    """
    assert telemetry.DEFAULT_PATHS[0] == "/espectre/v1/events"


def test_the_motion_event_is_understood():
    """espectre_motion_payload(), espectre_protocol.cpp:

        {"timestamp_ms":%u,"state":"%s","score":%.6g}
    """
    sample = telemetry.parse_payload(
        '{"timestamp_ms":1234567,"state":"motion","score":0.0305698}', now=1000.0
    )
    assert sample is not None
    assert sample.movement_score == 0.0305698
    assert sample.motion is True


def test_the_idle_state_is_not_mistaken_for_missing():
    sample = telemetry.parse_payload('{"timestamp_ms":9,"state":"idle","score":1e-07}')
    assert sample.motion is False
    assert sample.movement_score == 1e-07


def test_a_device_uptime_stamp_does_not_become_a_1970_timestamp():
    """timestamp_ms is milliseconds since boot, not since the epoch. Taken
    literally it would sort every sample before every other device's."""
    sample = telemetry.parse_payload(
        '{"timestamp_ms":1234567,"state":"idle","score":0.1}', now=1_700_000_000.0
    )
    assert sample.t == 1_700_000_000.0


def test_the_threshold_from_the_sensing_event_reaches_the_motion_samples():
    """ESPectre splits the pair across two events: the motion event has the
    score and no threshold, the sensing event the threshold and no score."""
    hub = telemetry.TelemetryHub()
    hub.ingest("probe", '{"enabled":true,"detector":"high_accuracy","threshold":0.5}')
    sample = hub.ingest("probe", '{"timestamp_ms":42,"state":"motion","score":0.9}')
    assert sample.threshold == 0.5
    assert sample.movement_score == 0.9


def test_a_later_threshold_change_is_picked_up():
    hub = telemetry.TelemetryHub()
    hub.ingest("probe", '{"threshold":0.66}')
    assert hub.ingest("probe", '{"state":"idle","score":0.1}').threshold == 0.66
    hub.ingest("probe", '{"threshold":0.5}')
    assert hub.ingest("probe", '{"state":"idle","score":0.1}').threshold == 0.5


def test_one_devices_threshold_does_not_leak_into_another():
    hub = telemetry.TelemetryHub()
    hub.ingest("a", '{"threshold":0.5}')
    assert hub.ingest("b", '{"state":"idle","score":0.1}').threshold is None


def test_events_that_carry_neither_are_still_ignored():
    """The collector feeds every event type through here — health, wifi and
    the rest must not become empty samples."""
    assert telemetry.parse_payload('{"free_memory_kb":120.5,"uptime":42}') is None


def test_a_threshold_only_event_updates_state_without_becoming_a_point():
    hub = telemetry.TelemetryHub()
    assert hub.ingest("probe", '{"threshold":0.5}') is None
    assert hub.snapshot("probe")["points"] == []
    # It still took effect: the next real measurement carries it.
    assert hub.ingest("probe", '{"state":"idle","score":0.1}').threshold == 0.5


# --- Why a connection failed -----------------------------------------------
#
# Three failures used to share one sentence ("Kein ESPectre-Telemetrie-
# Endpunkt erreichbar"), which is what a user saw after a recording came
# back empty. They need opposite responses, so they must read differently.


def test_a_device_answering_on_unknown_paths_is_a_version_mismatch():
    message = telemetry.explain_failure("192.168.2.188", [404, 404], [], [])
    assert "404" in message
    assert "ESPectre-Version" in message
    assert "auflösen" not in message


def test_an_unresolvable_name_points_at_mdns_and_the_ip_field():
    message = telemetry.explain_failure("test22.local", [], ["dns"], [])
    assert "auflösen" in message
    assert "IP-Adresse" in message


def test_a_refused_connection_points_at_direct_api():
    message = telemetry.explain_failure("192.168.2.188", [], ["refused"], [])
    assert "direct_api" in message
    assert "62587" in message


def test_a_timeout_points_at_the_network_not_the_firmware():
    message = telemetry.explain_failure("192.168.2.188", [], ["timeout"], [])
    assert "Client-Isolation" in message
    assert "direct_api" not in message


def test_an_unrecognised_failure_still_names_the_target_and_the_detail():
    message = telemetry.explain_failure("host", [], ["other"], ["OSError: something odd"])
    assert "host:62587" in message
    assert "something odd" in message


def test_no_information_at_all_still_produces_a_sentence():
    assert telemetry.explain_failure("host", [], [], []).startswith("Keine Telemetrieverbindung")


def test_an_http_status_outranks_a_connection_error():
    """One path may refuse while another answers 404; the answer is the
    stronger signal, because it proves something is listening."""
    assert "404" in telemetry.explain_failure("host", [404], ["refused"], [])


# The classifier reads the exception chain, because httpx hides the real
# cause behind "All connection attempts failed". These build the chains the
# way httpx really does — an earlier version matched on the message text
# and was wrong about every one of them.


def test_a_refused_connection_is_recognised_through_httpxs_wrapper():
    refused = ConnectionRefusedError(111, "Connect call failed")
    wrapped = httpx.ConnectError("All connection attempts failed")
    wrapped.__cause__ = OSError("All connection attempts failed")
    wrapped.__cause__.__cause__ = refused
    assert telemetry.failure_kind(wrapped) == "refused"


def test_an_unresolvable_name_is_recognised_through_the_wrapper():
    wrapped = httpx.ConnectError("[Errno -2] Name or service not known")
    wrapped.__cause__ = socket.gaierror(-2, "Name or service not known")
    assert telemetry.failure_kind(wrapped) == "dns"


def test_a_timeout_is_recognised():
    assert telemetry.failure_kind(httpx.ConnectTimeout("timed out")) == "timeout"


def test_an_unknown_failure_is_not_forced_into_a_category():
    assert telemetry.failure_kind(httpx.ProtocolError("odd")) == "other"


def test_the_chain_walk_terminates_on_a_cycle():
    a = httpx.ConnectError("a")
    b = httpx.ConnectError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert telemetry.failure_kind(a) == "other"
