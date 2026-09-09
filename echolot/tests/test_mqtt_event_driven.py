"""Evaluation happens when something happens, not when a timer says so.

From the external review (P1 #7, and R1/R3 in the 0.13.5 round). On a
pure ten-second timer a movement that arrives just after a tick waits
most of an interval before Home Assistant hears about it, and a short
pulse between two ticks can be missed entirely — the device reacts in a
second and the export then adds ten. The live subscriptions already
receive state changes as they happen.

The timer stays, because two things have no event behind them: a hold
time expiring, and a zone being added or removed.

The loop moved in 0.13.6. The export used to run its own, with its own
clock and its own calls into Home Assistant; now there is one evaluator
and the export is a listener on it. So these are tests of
`main.ZoneEvaluator` — the thing that actually decides when to look.
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


async def drive(*, cycles, idle, poke=None, published=None):
    """Run the evaluator until it has done `cycles` rounds, then stop it.

    `idle` stands in for the ten-second timer, so a test can say whether
    it is measuring the timer or the wake-up.
    """
    from app import main

    seen = []

    async def compute(zone):
        seen.append(zone.id)
        return {"occupied": True, "available": True, "state": "occupied"}

    evaluator = main.ZoneEvaluator()
    if published is not None:
        evaluator.add_listener(published.append)

    original = (main.compute_zone_state, main.IDLE_INTERVAL, main.MIN_EVALUATION_INTERVAL)
    main.compute_zone_state = compute
    main.IDLE_INTERVAL = idle
    main.MIN_EVALUATION_INTERVAL = 0.001
    try:
        await evaluator.run(lambda: [Zone()])
        if poke is not None:
            asyncio.create_task(poke(evaluator))
        for _ in range(400):
            await asyncio.sleep(0.005)
            if len(seen) >= cycles:
                break
    finally:
        evaluator.stop()
        (
            main.compute_zone_state,
            main.IDLE_INTERVAL,
            main.MIN_EVALUATION_INTERVAL,
        ) = original
    return len(seen)


def test_a_reading_evaluates_without_waiting_for_the_timer():
    """An idle interval far longer than the test: only the wake-up can
    produce a second round."""
    async def poke(evaluator):
        await asyncio.sleep(0.05)
        evaluator.wake()

    assert asyncio.run(drive(cycles=2, idle=300.0, poke=poke)) >= 2


def test_without_an_event_the_timer_is_still_the_floor():
    """Hold times expire on nobody's event."""
    assert asyncio.run(drive(cycles=2, idle=0.02)) >= 2


def test_a_change_during_a_cycle_is_not_lost():
    """The event is cleared after the wait, not before the next round, so
    something arriving mid-round still triggers the following one."""
    async def poke(evaluator):
        evaluator.wake()      # pending before the loop ever waits

    assert asyncio.run(drive(cycles=2, idle=300.0, poke=poke)) >= 2


def test_the_export_hangs_on_the_evaluator_rather_than_its_own_clock():
    """One loop, one clock. Two meant a reading arriving three times a
    second became three rounds of requests into Home Assistant."""
    published = []
    assert asyncio.run(drive(cycles=2, idle=0.02, published=published)) >= 2
    assert published, "der Export bekam keine Runde zu sehen"
    zone, state = published[0][0]
    assert zone.id == "z1" and state["occupied"] is True


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


# --- motion is news too (R3) -------------------------------------------


def test_motion_alone_wakes_the_evaluation():
    """Reproduction E: motion=on changed the cache and notified nobody.

    A motion flip is not a measurement — it produces no sample, on
    purpose, because counting it as one inflated a rate measured per
    second. But it is exactly the kind of change the evaluation should
    not wait ten seconds to hear about.
    """
    live = live_presence.LivePresence(subscription_factory=FakeSubscription)
    woken = []
    live.on_any_state(woken.append)
    device = make_device()
    live.reconcile([device])

    live.stream(device.id)._subscription.on_state(
        device.entity_motion, {"state": "on", "last_updated": "2026-09-08T12:00:00+00:00"}
    )
    assert woken == [device.id]

    live.stream(device.id)._subscription.on_state(
        device.entity_motion, {"state": "off", "last_updated": "2026-09-08T12:00:05+00:00"}
    )
    assert woken == [device.id, device.id]
    live.stop_all()


def test_the_same_motion_value_again_is_not_news():
    """Home Assistant re-sends a state on an attribute change. Waking on
    that is work with nothing behind it."""
    live = live_presence.LivePresence(subscription_factory=FakeSubscription)
    woken = []
    live.on_any_state(woken.append)
    device = make_device()
    live.reconcile([device])

    for stamp in ("12:00:00", "12:00:01", "12:00:02"):
        live.stream(device.id)._subscription.on_state(
            device.entity_motion,
            {"state": "on", "last_updated": f"2026-09-08T{stamp}+00:00"},
        )
    assert woken == [device.id]
    live.stop_all()


def test_a_reading_wakes_the_evaluation_but_a_repeat_of_it_does_not():
    """A duplicate score is dropped from the window, so there is nothing
    new to look at."""
    live = live_presence.LivePresence(subscription_factory=FakeSubscription)
    woken = []
    live.on_any_state(woken.append)
    device = make_device()
    live.reconcile([device])

    same = {"state": "0.42", "last_updated": "2026-09-08T12:00:00+00:00"}
    live.stream(device.id)._subscription.on_state(device.entity_movement_score, same)
    live.stream(device.id)._subscription.on_state(device.entity_movement_score, dict(same))
    assert woken == [device.id]
    live.stop_all()


def test_a_rebuilt_stream_keeps_waking_the_evaluation():
    """Correcting a device's entity ids rebuilds its subscription. If the
    wake-up did not come with it, that device would silently fall back to
    the ten-second timer — and only that one device."""
    live = live_presence.LivePresence(subscription_factory=FakeSubscription)
    woken = []
    live.on_any_state(woken.append)
    device = make_device()
    live.reconcile([device])

    device.entity_movement_score = "sensor.korrigiert"
    live.reconcile([device])
    live.stream(device.id)._subscription.on_state(
        "sensor.korrigiert", {"state": "0.9", "last_updated": "2026-09-08T12:00:00+00:00"}
    )
    assert woken == [device.id]
    live.stop_all()


def test_a_wake_up_listener_that_throws_does_not_stop_the_stream():
    live = live_presence.LivePresence(subscription_factory=FakeSubscription)
    device = make_device()
    live.reconcile([device])
    stream = live.stream(device.id)

    seen = []
    stream.add_change_listener(lambda _id: (_ for _ in ()).throw(RuntimeError("nope")))
    stream.add_change_listener(seen.append)
    stream._subscription.on_state(
        device.entity_motion, {"state": "on", "last_updated": "2026-09-08T12:00:00+00:00"}
    )
    assert seen == [device.id]
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

    attached = []
    monkeypatch.setattr(mqtt_bridge.bridge, "start", flaky_start)
    monkeypatch.setattr(mqtt_bridge, "attach", lambda *a, **k: attached.append(a))
    monkeypatch.setattr(main, "MQTT_RETRY_BACKOFF", (0.001,))
    asyncio.run(main._run_mqtt_export())

    assert attempts["n"] == 3
    assert attached, "nach dem geglückten Start muss der Export angehängt werden"


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


# --- a pulse shorter than the floor (R3) -------------------------------


def test_a_short_motion_pulse_is_latched_until_the_round_reads_it():
    """The evaluation runs on its own loop and reads the *current* state
    when it does. Somebody crossing a doorway is on and off again before
    the round, so the round saw nothing and the decision history had no
    trace of it at all."""
    live = live_presence.LivePresence(subscription_factory=FakeSubscription)
    device = make_device()
    live.reconcile([device])
    stream = live.stream(device.id)

    stream._subscription.on_state(
        device.entity_motion, {"state": "on", "last_updated": "2026-09-08T12:00:00+00:00"}
    )
    stream._subscription.on_state(
        device.entity_motion, {"state": "off", "last_updated": "2026-09-08T12:00:00.2+00:00"}
    )
    assert stream.motion_pulsed() is True
    # And exactly once: two consumers would mean the one that decides
    # loses the pulse to the one that only looks.
    assert stream.motion_pulsed() is False
    live.stop_all()


def test_no_pulse_is_reported_when_nothing_moved():
    live = live_presence.LivePresence(subscription_factory=FakeSubscription)
    device = make_device()
    live.reconcile([device])
    stream = live.stream(device.id)
    stream._subscription.on_state(
        device.entity_motion, {"state": "off", "last_updated": "2026-09-08T12:00:00+00:00"}
    )
    assert stream.motion_pulsed() is False
    live.stop_all()


def test_the_zone_evaluation_sees_the_pulse(monkeypatch):
    """End to end: the pulse the round would otherwise have missed puts
    the zone into `detected`, which is what the timeline records."""
    from app import feature_api, main, zone_logic
    from app.zones import Zone

    live = live_presence.LivePresence(subscription_factory=FakeSubscription)
    device = make_device()
    live.reconcile([device])
    monkeypatch.setattr(feature_api, "live", live)
    monkeypatch.setattr(main.devices, "get_device", lambda i: device if i == device.id else None)

    async def read(device_ids):
        # What Home Assistant answers *now*: nothing is moving any more.
        return [(device.id, device, {"available": True, "motion": False,
                                     "movement_score": 0.0, "threshold": 0.5})]

    monkeypatch.setattr(main, "_read_devices", read)
    monkeypatch.setattr(main, "_zone_rate_verdict", lambda _zone: None)
    main._zone_runtimes.pop("z", None)
    zone = Zone(id="z", created_at=0, updated_at=0, name="Z", device_ids=[device.id])

    # Without a pulse the zone is clear.
    state = asyncio.run(main.compute_zone_state(zone))
    assert state["state"] == zone_logic.CLEAR

    live.stream(device.id)._subscription.on_state(
        device.entity_motion, {"state": "on", "last_updated": "2026-09-08T12:00:00+00:00"}
    )
    live.stream(device.id)._subscription.on_state(
        device.entity_motion, {"state": "off", "last_updated": "2026-09-08T12:00:00.2+00:00"}
    )
    state = asyncio.run(main.compute_zone_state(zone))
    assert state["state"] == zone_logic.DETECTED
    assert state["members"][0]["motion_pulse"] is True

    # And it is consumed: the next round is clear again.
    assert asyncio.run(main.compute_zone_state(zone))["state"] == zone_logic.CLEAR
    main._zone_runtimes.pop("z", None)
    live.stop_all()
