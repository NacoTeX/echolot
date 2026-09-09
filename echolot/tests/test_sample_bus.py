"""One canonical stream of readings per device, and it says where they came from.

From the external review (R8). Echolot could hear a device two ways —
through the Home Assistant entities it publishes over the ESPHome API,
and through ESPectre's own Direct HTTP event stream. Until 0.13.6 both
ran, both fed the calibration store, and the confidence fusion read only
the direct one. So:

  * a device without the Direct HTTP API contributed nothing to fusion,
    while Home Assistant was delivering its readings the whole time;
  * a device with it could have the same movement recorded twice, and the
    crossing rate is events per second — counting it twice does not add
    detail, it doubles the number;
  * nothing on a reading said which transport had measured it.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import samples  # noqa: E402
from app.telemetry import Sample  # noqa: E402


def reading(t=0.0, score=0.5):
    return Sample(t=t, movement_score=score, threshold=0.1, motion=score > 0.1)


@pytest.fixture
def bus(monkeypatch):
    monkeypatch.delenv("ECHOLOT_SAMPLE_SOURCE", raising=False)
    return samples.SampleBus()


# --- one source, declared ----------------------------------------------


def test_home_assistant_is_the_source_unless_told_otherwise(bus):
    """It is the one that always works — the direct transport refuses
    this add-on at the pinned ESPectre commit."""
    assert bus.source == samples.SOURCE_HOME_ASSISTANT


def test_the_source_is_a_setting_not_a_race(monkeypatch):
    """A rate profile is learned under one source. Switching underneath
    it because the other transport happened to deliver first would change
    the measurement without changing its definition."""
    monkeypatch.setenv("ECHOLOT_SAMPLE_SOURCE", "direct")
    bus = samples.SampleBus()
    assert bus.source == samples.SOURCE_DIRECT
    assert bus.publish("d", reading(), source=samples.SOURCE_DIRECT) is True
    assert bus.publish("d", reading(1.0), source=samples.SOURCE_HOME_ASSISTANT) is False


def test_an_unknown_setting_falls_back_rather_than_failing(monkeypatch):
    monkeypatch.setenv("ECHOLOT_SAMPLE_SOURCE", "irgendwas")
    assert samples.SampleBus().source == samples.SOURCE_HOME_ASSISTANT


def test_an_unknown_source_on_a_reading_is_a_programming_error(bus):
    with pytest.raises(ValueError):
        bus.publish("d", reading(), source="telepathie")


# --- no double counting -------------------------------------------------


def test_the_same_movement_over_two_transports_is_counted_once(bus):
    """The regression: both collectors fed the calibration store."""
    assert bus.publish("d", reading(1.0), source=samples.SOURCE_HOME_ASSISTANT) is True
    assert bus.publish("d", reading(1.0), source=samples.SOURCE_DIRECT) is False
    assert len(bus.window("d", 60.0, now=1.0)) == 1


def test_refusals_are_counted_rather_than_hidden(bus):
    """A collector that is running and contributing nothing should be
    visible, not mysterious."""
    for index in range(4):
        bus.publish("d", reading(float(index)), source=samples.SOURCE_DIRECT)
    assert bus.status()["refused_by_source"][samples.SOURCE_DIRECT] == 4
    assert bus.status()["devices"] == {}


def test_a_listener_hears_each_reading_exactly_once(bus):
    heard = []
    bus.add_listener(lambda device_id, sample: heard.append((device_id, sample)))
    bus.publish("d", reading(1.0), source=samples.SOURCE_HOME_ASSISTANT)
    bus.publish("d", reading(1.0), source=samples.SOURCE_DIRECT)
    assert len(heard) == 1


# --- source tagging -----------------------------------------------------


def test_a_reading_says_which_transport_measured_it(bus):
    bus.publish("d", reading(1.0), source=samples.SOURCE_HOME_ASSISTANT)
    assert bus.latest("d")["source"] == samples.SOURCE_HOME_ASSISTANT
    assert bus.window("d", 60.0, now=1.0)[0]["source"] == samples.SOURCE_HOME_ASSISTANT


def test_the_snapshot_names_the_source_and_the_refusals(bus):
    import time

    # snapshot() windows against the wall clock, the way the dashboard and
    # the fusion ask it to.
    now = time.time()
    bus.publish("d", reading(now), source=samples.SOURCE_HOME_ASSISTANT)
    bus.publish("d", reading(now), source=samples.SOURCE_DIRECT)
    snapshot = bus.snapshot("d", seconds=3600)
    assert snapshot["source"] == samples.SOURCE_HOME_ASSISTANT
    assert snapshot["available"] is True
    assert snapshot["sample_count"] == 1
    assert snapshot["refused_by_source"] == {samples.SOURCE_DIRECT: 1}


def test_a_device_nobody_has_heard_from_is_unavailable_not_empty(bus):
    snapshot = bus.snapshot("still", seconds=3600)
    assert snapshot["available"] is False
    assert snapshot["points"] == []


# --- the window ---------------------------------------------------------


def test_the_window_is_by_wall_clock(bus):
    for index in range(10):
        bus.publish("d", reading(float(index)), source=samples.SOURCE_HOME_ASSISTANT)
    assert len(bus.window("d", 3.0, now=9.0)) == 4       # t = 6, 7, 8, 9


def test_forgetting_a_device_leaves_nothing_behind(bus):
    bus.publish("d", reading(1.0), source=samples.SOURCE_HOME_ASSISTANT)
    bus.forget("d")
    assert bus.latest("d") is None
    assert bus.snapshot("d")["sample_count"] == 0


def test_a_listener_that_throws_does_not_stop_collection(bus):
    heard = []
    bus.add_listener(lambda *_: (_ for _ in ()).throw(RuntimeError("nope")))
    bus.add_listener(lambda device_id, sample: heard.append(device_id))
    assert bus.publish("d", reading(1.0), source=samples.SOURCE_HOME_ASSISTANT) is True
    assert heard == ["d"]


# --- watchers -----------------------------------------------------------


def test_a_watcher_gets_the_readings_as_they_arrive(bus):
    async def scenario():
        bus.bind(asyncio.get_running_loop())
        queue = bus.subscribe("d")
        bus.publish("d", reading(1.0), source=samples.SOURCE_HOME_ASSISTANT)
        await asyncio.sleep(0)
        return await asyncio.wait_for(queue.get(), timeout=1.0)

    assert asyncio.run(scenario()).t == 1.0


def test_a_reading_from_another_thread_reaches_the_watcher(bus):
    """Home Assistant's websocket delivers on its own thread, and the
    browser watching the trace is on the event loop."""
    import threading

    async def scenario():
        bus.bind(asyncio.get_running_loop())
        queue = bus.subscribe("d")
        threading.Thread(
            target=lambda: bus.publish(
                "d", reading(2.0), source=samples.SOURCE_HOME_ASSISTANT
            )
        ).start()
        return await asyncio.wait_for(queue.get(), timeout=2.0)

    assert asyncio.run(scenario()).t == 2.0


def test_a_slow_watcher_loses_the_oldest_rather_than_blocking(bus):
    async def scenario():
        bus.bind(asyncio.get_running_loop())
        queue = bus.subscribe("d")
        for index in range(150):
            bus.publish("d", reading(float(index)), source=samples.SOURCE_HOME_ASSISTANT)
        await asyncio.sleep(0)
        return queue.qsize(), (await queue.get()).t

    size, oldest = asyncio.run(scenario())
    assert size == 100
    assert oldest == 50.0


# --- the direct collector -----------------------------------------------


def test_the_direct_collector_is_off_by_default(monkeypatch):
    """Checked against the pinned ESPectre commit, not assumed: an
    ESPHome-built device allows only espectre.dev as an origin, and the
    Kconfig that would relax that is not declared in any file the ESPHome
    build reaches. So the collector could only ever collect 403s, and
    claiming to be espectre.dev is not something this add-on does."""
    monkeypatch.delenv("ECHOLOT_DIRECT_COLLECTOR", raising=False)
    monkeypatch.delenv("ECHOLOT_SAMPLE_SOURCE", raising=False)
    assert samples.direct_collector_enabled() is False
    assert "403" in samples.DIRECT_COLLECTOR_REASON


def test_it_can_be_switched_on_for_firmware_that_permits_it(monkeypatch):
    monkeypatch.setenv("ECHOLOT_DIRECT_COLLECTOR", "true")
    assert samples.direct_collector_enabled() is True


def test_choosing_the_direct_source_implies_running_its_collector(monkeypatch):
    monkeypatch.delenv("ECHOLOT_DIRECT_COLLECTOR", raising=False)
    monkeypatch.setenv("ECHOLOT_SAMPLE_SOURCE", "direct")
    assert samples.direct_collector_enabled() is True


# --- what a profile was learned under ----------------------------------


def test_a_profile_records_the_transport_it_was_learned_under():
    """The same room measured over a different transport is a different
    measurement, and nothing said which one a baseline described."""
    from app import presence_rate

    rows = [
        {"t": float(index), "movement_score": 0.0 if index % 20 else 0.5,
         "label": "empty", "source": samples.SOURCE_HOME_ASSISTANT}
        for index in range(2400)
    ]
    profile = presence_rate.learn_baseline(rows)
    assert profile is not None
    assert profile.source == samples.SOURCE_HOME_ASSISTANT
    assert profile.as_dict()["source"] == samples.SOURCE_HOME_ASSISTANT


def test_mixed_material_records_no_source_rather_than_picking_one():
    from app import presence_rate

    rows = []
    for index in range(2400):
        rows.append({
            "t": float(index), "movement_score": 0.0 if index % 20 else 0.5,
            "label": "empty",
            "source": samples.SOURCE_DIRECT if index < 1200 else samples.SOURCE_HOME_ASSISTANT,
        })
    profile = presence_rate.learn_baseline(rows)
    assert profile is not None and profile.source is None


def test_an_older_profile_still_loads_and_says_nothing_about_its_source():
    """Existing profiles are preserved, not invalidated: they are still a
    correct answer to the question they were learned under."""
    from app import presence_rate

    stored = {
        "crossing_threshold": 1e-3,
        "baseline_rate": 0.08,
        "baseline_spread": 0.01,
        "window_seconds": 60.0,
        "sample_count": 1200,
        "observed_seconds": 1200.0,
        "version": presence_rate.PROFILE_VERSION,
    }
    assert presence_rate.profile_status(stored) == presence_rate.USABLE
    assert presence_rate.profile_from_dict(stored).source is None


def test_a_profile_from_another_transport_is_reported(monkeypatch):
    """Not a crash and not something anybody would notice — which is
    exactly why it has to be said."""
    from app import overview
    from app.devices import BuildStatus, Device, DeviceCreate

    device = Device(
        id="probe", created_at=0, updated_at=0, status=BuildStatus.SUCCESS,
        config=DeviceCreate(name="probe", board="esp32c6", wifi_ssid="netz",
                            wifi_password="passwort123"),
    )
    device.presence_profile = {
        "crossing_threshold": 1e-3, "baseline_rate": 0.08, "baseline_spread": 0.01,
        "window_seconds": 60.0, "sample_count": 1200, "observed_seconds": 1200.0,
        "version": 2, "source": samples.SOURCE_DIRECT,
    }
    state = {"available": True, "motion": False, "movement_score": 0.1, "threshold": 0.5}

    monkeypatch.delenv("ECHOLOT_SAMPLE_SOURCE", raising=False)
    problems = overview.collect_problems(
        device_states=[(device, state)], zones_without_devices=[],
        mqtt_status={"enabled": False, "connected": False}, mqtt_wanted=False,
        esphome={"available": True, "version": "2026.6.5"},
    )
    kinds = [p.kind for p in problems]
    assert "profile_source_mismatch" in kinds

    # Matching source: nothing to say.
    device.presence_profile["source"] = samples.SOURCE_HOME_ASSISTANT
    problems = overview.collect_problems(
        device_states=[(device, state)], zones_without_devices=[],
        mqtt_status={"enabled": False, "connected": False}, mqtt_wanted=False,
        esphome={"available": True, "version": "2026.6.5"},
    )
    assert "profile_source_mismatch" not in [p.kind for p in problems]
