"""Tests for what the first screen reports.

The overview's job is to name what is wrong in words that say what to do,
and to stay silent when nothing is. Both halves matter: a page that cries
wolf about a device still building is as useless as one that hides a
failed build.
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import overview  # noqa: E402
from app.devices import BuildStatus, Device, DeviceCreate  # noqa: E402
from app.zones import Zone  # noqa: E402

ESPHOME_OK = {"available": True, "version": "ESPHome 2026.6.5"}
MQTT_OK = {"connected": True}


def device(name="flur", status=BuildStatus.SUCCESS, ota_error=None):
    return Device(
        id=name,
        created_at=0,
        updated_at=0,
        status=status,
        ota_error=ota_error,
        config=DeviceCreate(
            name=name, friendly_name=f"Gerät {name}", board="esp32c6",
            wifi_ssid="netz", wifi_password="passwort123",
        ),
    )


def collect(device_states=(), empty_zones=(), mqtt=None, mqtt_wanted=True, esphome=None):
    return overview.collect_problems(
        device_states=list(device_states),
        zones_without_devices=list(empty_zones),
        mqtt_status=mqtt if mqtt is not None else MQTT_OK,
        mqtt_wanted=mqtt_wanted,
        esphome=esphome if esphome is not None else ESPHOME_OK,
    )


def kinds(problems):
    return [p.kind for p in problems]


def test_a_healthy_system_reports_nothing():
    """Silence is the correct output when everything works."""
    problems = collect(device_states=[(device(), {"available": True})])
    assert problems == []


def test_a_failed_build_is_reported():
    problems = collect(device_states=[(device(status=BuildStatus.ERROR), None)])
    assert kinds(problems) == ["build_failed"]
    assert "Gerät flur" in problems[0].message


def test_a_device_that_was_never_built_is_reported():
    problems = collect(device_states=[(device(status=BuildStatus.IDLE), None)])
    assert kinds(problems) == ["not_built"]


def test_a_build_in_progress_is_not_a_problem():
    """Work in flight is not a fault, and reporting it trains people to
    ignore the list."""
    for status in (BuildStatus.QUEUED, BuildStatus.RUNNING):
        assert collect(device_states=[(device(status=status), None)]) == []


def test_a_built_but_unreadable_device_is_reported():
    problems = collect(device_states=[(device(), {"available": False, "error": "..."})])
    assert kinds(problems) == ["device_unavailable"]
    assert "geflasht" in problems[0].message


def test_a_failed_ota_is_reported_alongside_a_working_device():
    """The firmware on the device is fine; the update was not. Both facts
    are true at once."""
    problems = collect(
        device_states=[(device(ota_error="Passwort abgelehnt"), {"available": True})]
    )
    assert kinds(problems) == ["ota_failed"]


def test_an_empty_zone_is_reported():
    zone = Zone(id="z", created_at=0, updated_at=0, name="Küche", device_ids=[])
    problems = collect(empty_zones=[zone])
    assert kinds(problems) == ["zone_empty"]
    assert "Küche" in problems[0].message
    assert problems[0].tab == "zones"


def test_mqtt_being_down_is_only_a_problem_when_it_was_wanted():
    down = {"connected": False, "error": "kein Broker"}
    assert kinds(collect(mqtt=down, mqtt_wanted=True)) == ["mqtt_down"]
    # Switched off deliberately in the add-on options — not a fault.
    assert collect(mqtt=down, mqtt_wanted=False) == []


def test_a_missing_esphome_is_reported_first():
    """Nothing else can be fixed while firmware cannot be built."""
    problems = collect(
        device_states=[(device(status=BuildStatus.ERROR), None)],
        esphome={"available": False, "error": "not found"},
    )
    assert kinds(problems)[0] == "esphome_missing"


def test_every_problem_names_a_tab_that_exists():
    zone = Zone(id="z", created_at=0, updated_at=0, name="Z", device_ids=[])
    problems = collect(
        device_states=[
            (device("a", status=BuildStatus.ERROR), None),
            (device("b", status=BuildStatus.IDLE), None),
            (device("c", ota_error="x"), {"available": False}),
        ],
        empty_zones=[zone],
        mqtt={"connected": False},
        esphome={"available": False},
    )
    assert problems, "fixture should produce problems"
    for problem in problems:
        assert problem.tab in {"overview", "dashboard", "devices", "zones"}
        assert problem.message.strip()


def test_radio_load_sums_the_whole_fleet():
    """One device's rate is not the number that matters to the household."""
    devices_ = [device("a"), device("b")]
    for d in devices_:
        d.config.csi_target_pps = 100
    assert overview.radio_load(devices_, 0.09) == 18.0
    assert overview.radio_load([], 0.09) == 0


def _high_accuracy(threshold):
    dev = device("flur")
    dev.config.detection_algorithm = "high_accuracy"
    state = {
        "available": True,
        "motion": False,
        "movement_score": 1e-6,
        "threshold": threshold,
    }
    return [(dev, state)]


def test_a_threshold_from_the_other_profile_reaches_the_overview():
    """The finding that affects every device Echolot builds.

    It rides along on the threshold already read for the live view, so it
    costs no extra request — which is why it belongs here and not only in
    the per-device diagnosis.
    """
    problems = collect(_high_accuracy(0.6621854))
    assert "threshold_profile_mismatch" in kinds(problems)
    message = next(p.message for p in problems if p.kind == "threshold_profile_mismatch")
    assert "flur" in message
    assert "lightweight" in message


def test_a_matching_threshold_stays_off_the_overview():
    assert kinds(collect(_high_accuracy(0.5))) == []


def test_a_deliberately_chosen_threshold_is_not_second_guessed():
    assert kinds(collect(_high_accuracy(0.31))) == []


def test_a_device_that_never_reported_a_threshold_is_not_judged():
    assert "threshold_profile_mismatch" not in kinds(collect(_high_accuracy(None)))
