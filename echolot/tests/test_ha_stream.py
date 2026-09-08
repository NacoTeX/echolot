"""The Home Assistant websocket subscription, against a real server.

Polling captured about a quarter of the data — six updates a second
published, one and a half sampled, with gaps up to thirteen seconds. The
rate over a sixty-second window is what the presence decision now turns
on, so that quarter is the difference between ninety samples behind a
rate estimate and three hundred and sixty.

The fake below speaks Home Assistant's actual handshake — auth_required,
auth, auth_ok, subscribe_trigger — over a real websocket, because the
protocol is the part that can be got wrong.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import ha_stream  # noqa: E402


def trigger_event(entity_id: str, value: str, stamp: str = "2026-09-08T12:00:00+00:00") -> str:
    return json.dumps({
        "id": 1,
        "type": "event",
        "event": {"variables": {"trigger": {
            "entity_id": entity_id,
            "to_state": {"state": value, "last_updated": stamp},
        }}},
    })


class FakeHomeAssistant:
    def __init__(self, *, auth_ok=True, subscribe_ok=True, events=()):
        self.auth_ok = auth_ok
        self.subscribe_ok = subscribe_ok
        self.events = list(events)
        self.subscribed_to: list[str] | None = None
        self.tokens: list[str] = []

    async def handler(self, socket):
        await socket.send(json.dumps({"type": "auth_required", "ha_version": "2026.9.0"}))
        auth = json.loads(await socket.recv())
        self.tokens.append(auth.get("access_token"))
        if not self.auth_ok:
            await socket.send(json.dumps({"type": "auth_invalid", "message": "ungültig"}))
            return
        await socket.send(json.dumps({"type": "auth_ok"}))

        request = json.loads(await socket.recv())
        self.subscribed_to = request["trigger"]["entity_id"]
        if not self.subscribe_ok:
            await socket.send(json.dumps(
                {"id": 1, "type": "result", "success": False,
                 "error": {"message": "kein solcher Trigger"}}))
            return
        await socket.send(json.dumps({"id": 1, "type": "result", "success": True}))
        for event in self.events:
            await socket.send(event)
        await asyncio.sleep(5)


async def run_against(fake, entity_ids, *, seconds=0.6):
    received = []
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        os.environ["ECHOLOT_HA_WS_URL"] = f"ws://127.0.0.1:{port}"
        os.environ["ECHOLOT_HA_TOKEN"] = "geheim"
        subscription = ha_stream.StateSubscription(
            entity_ids, lambda entity_id, state: received.append((entity_id, state))
        )
        subscription.start()
        await asyncio.sleep(seconds)
        subscription.stop()
    del os.environ["ECHOLOT_HA_WS_URL"]
    return subscription, received


def test_the_handshake_authenticates_and_filters_by_entity():
    fake = FakeHomeAssistant(events=[
        trigger_event("sensor.probe_movement_score", "0.42"),
        trigger_event("binary_sensor.probe_motion_detected", "on"),
    ])
    subscription, received = asyncio.run(
        run_against(fake, ["sensor.probe_movement_score", "binary_sensor.probe_motion_detected"])
    )
    assert fake.tokens == ["geheim"]
    # Server-side filtering: the alternative is every state change in the
    # installation delivered to read two of them.
    assert fake.subscribed_to == [
        "sensor.probe_movement_score",
        "binary_sensor.probe_motion_detected",
    ]
    assert [entity for entity, _ in received] == [
        "sensor.probe_movement_score",
        "binary_sensor.probe_motion_detected",
    ]
    assert received[0][1]["state"] == "0.42"
    assert subscription.error is None


def test_a_rejected_token_is_reported_and_not_hammered():
    fake = FakeHomeAssistant(auth_ok=False)
    subscription, received = asyncio.run(run_against(fake, ["sensor.x"]))
    assert received == []
    assert subscription.connected is False
    assert "ungültig" in (subscription.error or "")


def test_a_rejected_subscription_is_reported():
    fake = FakeHomeAssistant(subscribe_ok=False)
    subscription, received = asyncio.run(run_against(fake, ["sensor.x"]))
    assert received == []
    assert "kein solcher Trigger" in (subscription.error or "")


def test_a_dropped_connection_reconnects():
    """The server hangs up after one event; the subscription must come back
    rather than leaving a recording silently dead."""
    class Flaky(FakeHomeAssistant):
        def __init__(self):
            super().__init__()
            self.connections = 0

        async def handler(self, socket):
            self.connections += 1
            await socket.send(json.dumps({"type": "auth_required"}))
            await socket.recv()
            await socket.send(json.dumps({"type": "auth_ok"}))
            await socket.recv()
            await socket.send(json.dumps({"id": 1, "type": "result", "success": True}))
            await socket.send(trigger_event("sensor.x", str(self.connections)))
            await socket.close()

    fake = Flaky()
    _, received = asyncio.run(run_against(fake, ["sensor.x"], seconds=2.5))
    assert fake.connections >= 2, "nach dem Abbruch wurde nicht neu verbunden"
    assert [state["state"] for _, state in received][:2] == ["1", "2"]


# --- message parsing --------------------------------------------------------


def test_only_trigger_events_become_states():
    assert ha_stream.state_from_message({"type": "result", "success": True}) is None
    assert ha_stream.state_from_message({"type": "event", "event": {}}) is None
    assert ha_stream.state_from_message({}) is None


def test_a_state_that_became_none_is_ignored():
    """Home Assistant sends to_state: null when an entity is removed."""
    message = json.loads(trigger_event("sensor.x", "1"))
    message["event"]["variables"]["trigger"]["to_state"] = None
    assert ha_stream.state_from_message(message) is None
