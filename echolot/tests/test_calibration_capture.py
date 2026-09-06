"""End-to-end: an SSE device, the hub, and a Calibration Lab CSV with rows in it.

Written after three recordings exported nothing but their header. The
adapter had been built against a guessed endpoint and a guessed payload;
this drives the real ones, so the same mistake cannot pass again.

The server below speaks exactly what the firmware speaks:
`/espectre/v1/events`, `event: motion` frames carrying
{"timestamp_ms":…,"state":"motion"|"idle","score":…}, and a sensing frame
that holds the threshold the motion frames omit.
"""

import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import telemetry  # noqa: E402
from app import calibration  # noqa: E402

EVENTS_PATH = "/espectre/v1/events"

#: A quiet room, then someone walking through it.
SCORES = [0.0004] * 25 + [3.1, 3.4, 2.9, 3.6, 3.2] * 5


class ESPectreHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path != EVENTS_PATH:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        # The threshold arrives on its own event, exactly as the firmware
        # sends it — never together with a score.
        self.wfile.write(b"event: sensing\ndata: " + json.dumps(
            {"enabled": True, "detector": "high_accuracy", "threshold": 0.5}
        ).encode() + b"\n\n")
        for index, score in enumerate(SCORES):
            frame = json.dumps({
                "timestamp_ms": 1000 + index * 250,
                "state": "motion" if score > 0.5 else "idle",
                "score": score,
            })
            self.wfile.write(b"event: motion\ndata: " + frame.encode() + b"\n\n")
            self.wfile.flush()
        self.wfile.write(b"event: health\ndata: " + json.dumps(
            {"free_memory_kb": 120.5, "uptime": 42}
        ).encode() + b"\n\n")
        self.wfile.flush()

    def log_message(self, *args):
        pass


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(tmp_path))
    return calibration.CalibrationStore()


@pytest.fixture
def device_server(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), ESPectreHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(telemetry, "DIRECT_PORT", server.server_address[1])
    yield "127.0.0.1"
    server.shutdown()
    server.server_close()


def collect(host: str, hub: telemetry.TelemetryHub, *, samples_wanted: int) -> None:
    async def run():
        task = asyncio.create_task(hub._collect("probe", host))
        for _ in range(200):
            await asyncio.sleep(0.05)
            if len(hub._samples.get("probe", ())) >= samples_wanted:
                break
        task.cancel()

    asyncio.run(run())


def test_a_recording_against_the_real_endpoint_is_not_empty(device_server, store):
    hub = telemetry.TelemetryHub()
    hub.add_listener(store.ingest)

    session = store.create("probe", name="Gehtest")
    store.set_label(session["id"], "empty")
    collect(device_server, hub, samples_wanted=len(SCORES))
    store.set_label(session["id"], "moving")
    store.stop(session["id"])

    csv = store.csv(session["id"])
    rows = [line for line in csv.strip().splitlines() if line]
    assert rows[0] == "t,movement_score,threshold,motion,label"
    assert len(rows) > 1, "das war der Fehler: nur die Kopfzeile"


def test_the_captured_rows_carry_score_threshold_and_motion(device_server, store):
    hub = telemetry.TelemetryHub()
    hub.add_listener(store.ingest)
    session = store.create("probe")
    store.set_label(session["id"], "empty")
    collect(device_server, hub, samples_wanted=len(SCORES))
    store.stop(session["id"])

    body = [line.split(",") for line in store.csv(session["id"]).strip().splitlines()[1:]]
    assert body, "keine Zeilen aufgezeichnet"
    scores = [float(row[1]) for row in body if row[1]]
    thresholds = {row[2] for row in body if row[2]}
    motions = {row[3] for row in body if row[3]}

    # The threshold-only sensing event updates the carried value but is not
    # itself a measurement, so it must not appear as a blank row.
    assert len(scores) == len(body), "jede Zeile braucht einen Bewegungswert"
    # The threshold never shares an event with a score; it is carried over.
    assert thresholds == {"0.5"}
    # Both states occur in the fixture and must survive the round trip.
    assert motions == {"True", "False"}


def test_the_wrong_endpoint_produces_exactly_what_the_user_saw(device_server, store):
    """The regression itself: a path the device does not serve yields a CSV
    with nothing but its header — no error, no warning, no data."""
    hub = telemetry.TelemetryHub()
    hub.add_listener(store.ingest)
    session = store.create("probe")
    store.set_label(session["id"], "empty")

    async def run():
        task = asyncio.create_task(hub._collect("probe", device_server))
        await asyncio.sleep(1.0)
        task.cancel()

    os.environ["ESPECTRE_DIRECT_PATHS"] = "/events,/api/events"
    try:
        asyncio.run(run())
    finally:
        del os.environ["ESPECTRE_DIRECT_PATHS"]

    store.stop(session["id"])
    assert store.csv(session["id"]).strip().splitlines() == [
        "t,movement_score,threshold,motion,label"
    ]
    assert hub.snapshot("probe")["available"] is False
