"""The export publishes when something happens, not when a timer says so.

From the external review (P1 #7). On a pure ten-second timer a movement
that arrives just after a tick waits most of an interval before Home
Assistant hears about it, and a short pulse between two ticks can be
missed entirely — the device reacts in a second and the export then adds
ten. The live subscriptions already receive state changes as they happen.

The timer stays, because two things have no event behind them: a hold
time expiring, and a zone being added or removed.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import live_presence, mqtt_bridge  # noqa: E402
from app.devices import Device, DeviceCreate  # noqa: E402


class Zone:
    def __init__(self, zone_id="z1", name="Küche"):
        self.id = zone_id
        self.name = name


async def drive(wakeup, *, cycles, interval=30.0):
    """Run the loop until it has published `cycles` times, then stop it."""
    seen = []

    async def compute(zones):
        seen.append(len(seen))
        return [(zone, {"occupied": True, "available": True}) for zone in zones]

    published = []
    original = mqtt_bridge.bridge.publish_zone
    mqtt_bridge.bridge.publish_zone = lambda *a, **k: published.append(a)
    task = asyncio.create_task(
        mqtt_bridge.publish_loop(compute, lambda: [Zone()], interval=interval, wakeup=wakeup)
    )
    try:
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(seen) >= cycles:
                break
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        mqtt_bridge.bridge.publish_zone = original
    return len(seen)


def test_a_reading_publishes_without_waiting_for_the_timer():
    async def scenario():
        wakeup = asyncio.Event()
        # An interval far longer than the test: only the event can produce
        # a second cycle.
        async def poke():
            await asyncio.sleep(0.05)
            wakeup.set()

        asyncio.create_task(poke())
        return await drive(wakeup, cycles=2, interval=300.0)

    assert asyncio.run(scenario()) >= 2


def test_without_an_event_the_timer_is_still_the_floor():
    """Hold times expire on nobody's event."""
    async def scenario():
        wakeup = asyncio.Event()
        return await drive(wakeup, cycles=2, interval=0.02)

    assert asyncio.run(scenario()) >= 2


def test_a_change_during_a_cycle_is_not_lost():
    """The event is cleared after the wait, not before the next cycle, so
    something arriving mid-cycle still triggers the following one."""
    async def scenario():
        wakeup = asyncio.Event()
        wakeup.set()          # already pending before the loop ever waits
        return await drive(wakeup, cycles=2, interval=300.0)

    assert asyncio.run(scenario()) >= 2


# --- the hook the export hangs on --------------------------------------


class FakeSubscription:
    def __init__(self, entity_ids, on_state, *, loop=None):
        self.entity_ids = list(entity_ids)
        self.on_state = on_state
        self.connected = False
        self.error = None

    def start(self):
        self.connected = True

    def stop(self):
        self.connected = False


def make_device(device_id="wohnzimmer"):
    return Device(
        id=device_id,
        created_at=0,
        updated_at=0,
        config=DeviceCreate(
            name=device_id, board="esp32c6", wifi_ssid="netz", wifi_password="passwort123"
        ),
        entity_motion=f"binary_sensor.{device_id}_motion",
        entity_movement_score=f"sensor.{device_id}_score",
    )


def test_a_reading_reaches_the_global_hook():
    live = live_presence.LivePresence(subscription_factory=FakeSubscription)
    fired = []
    live.on_any_change(lambda device_id, sample: fired.append(device_id))
    device = make_device()
    live.reconcile([device])

    stream = live.stream(device.id)
    stream._subscription.on_state(
        device.entity_movement_score,
        {"state": "0.42", "last_updated": "2026-09-08T12:00:00+00:00"},
    )
    assert fired == [device.id]
    live.stop_all()


def test_devices_added_later_are_hooked_up_too():
    """Otherwise a device built after start-up would export on the timer
    only, and nobody would notice which one."""
    live = live_presence.LivePresence(subscription_factory=FakeSubscription)
    fired = []
    live.on_any_change(lambda device_id, sample: fired.append(device_id))
    live.reconcile([])

    later = make_device("flur")
    live.reconcile([later])
    stream = live.stream(later.id)
    stream._subscription.on_state(
        later.entity_movement_score,
        {"state": "0.1", "last_updated": "2026-09-08T12:00:01+00:00"},
    )
    assert fired == [later.id]
    live.stop_all()


# --- the broker showing up late (P1 #7) --------------------------------


def test_the_export_keeps_trying_until_the_broker_exists(monkeypatch):
    """Starting the add-on before Mosquitto used to mean no export at all
    until the add-on itself was restarted — a plausible order on a
    rebooting machine, and an invisible failure."""
    from app import main

    attempts = {"n": 0}

    async def flaky_start():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise mqtt_bridge.MqttUnavailable("kein Broker")

    async def stub_loop(*args, **kwargs):
        return None

    monkeypatch.setattr(mqtt_bridge.bridge, "start", flaky_start)
    monkeypatch.setattr(mqtt_bridge, "publish_loop", stub_loop)
    monkeypatch.setattr(main, "MQTT_RETRY_BACKOFF", (0.001,))
    asyncio.run(main._run_mqtt_export())

    assert attempts["n"] == 3


def test_a_failed_start_is_visible_while_it_retries(monkeypatch):
    from app import main

    async def always_fails():
        raise mqtt_bridge.MqttUnavailable("kein Broker")

    monkeypatch.setattr(mqtt_bridge.bridge, "start", always_fails)
    monkeypatch.setattr(main, "MQTT_RETRY_BACKOFF", (0.001,))

    async def scenario():
        task = asyncio.create_task(main._run_mqtt_export())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert "kein Broker" in (mqtt_bridge.bridge.error or "")
