"""A finished build must not undo what happened while it ran.

From the external review (P1 #8). run_build and run_ota hold a Device
object for the length of a compile — minutes — and used to write that
whole object back with save_device when they finished. Anything changed
in between was replaced by the copy the job had started with, and a
device deleted during a build came back when the build ended.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import devices  # noqa: E402
from app.devices import BuildStatus, Device, DeviceCreate  # noqa: E402


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(devices, "DATA_DIR", tmp_path)
    monkeypatch.setattr(devices, "INDEX_PATH", tmp_path / "devices.json")
    return tmp_path


def make(device_id="probe", **kwargs) -> Device:
    device = Device(
        id=device_id,
        created_at=1.0,
        updated_at=1.0,
        config=DeviceCreate(
            name=device_id, board="esp32c6", wifi_ssid="netz", wifi_password="passwort123"
        ),
        **kwargs,
    )
    devices.save_device(device)
    return device


def test_a_field_update_leaves_everything_else_alone(registry):
    """The regression, in one line: the build's stale copy of the device
    must not decide what the entity ids are."""
    stale = make()
    # ... meanwhile, someone corrects the entity id and applies a profile
    live = devices.get_device("probe")
    live.entity_movement_score = "sensor.wohnzimmer_movement_score"
    live.presence_profile = {"baseline_rate": 0.084}
    devices.save_device(live)

    # ... and only now does the build finish, holding `stale`.
    updated = devices.update_device(stale.id, status=BuildStatus.SUCCESS,
                                    firmware_bin="firmware.factory.bin")

    assert updated.status == BuildStatus.SUCCESS
    assert updated.entity_movement_score == "sensor.wohnzimmer_movement_score"
    assert updated.presence_profile == {"baseline_rate": 0.084}


def test_saving_the_whole_object_is_what_loses_the_change(registry):
    """States the failure the fix avoids, so the reason for update_device
    does not have to be taken on trust."""
    stale = make()
    live = devices.get_device("probe")
    live.entity_movement_score = "sensor.wohnzimmer_movement_score"
    devices.save_device(live)

    stale.status = BuildStatus.SUCCESS
    devices.save_device(stale)
    assert devices.get_device("probe").entity_movement_score is None


def test_a_deleted_device_is_not_recreated_by_a_finishing_build(registry):
    device = make()
    assert devices.delete_device(device.id) is True
    assert devices.update_device(device.id, status=BuildStatus.SUCCESS) is None
    assert devices.get_device(device.id) is None


def test_an_unknown_field_is_refused_rather_than_dropped(registry):
    """Pydantic ignores unknown keys, so a typo would write nothing and
    report success — the quietest possible way to lose a build result."""
    make()
    with pytest.raises(ValueError, match="Unbekannte Gerätefelder"):
        devices.update_device("probe", stauts=BuildStatus.SUCCESS)
    assert devices.get_device("probe").status == BuildStatus.IDLE


# --- restarts (P1 #8) --------------------------------------------------


@pytest.mark.parametrize("status", [BuildStatus.QUEUED, BuildStatus.RUNNING])
def test_a_job_left_running_by_a_restart_is_failed(registry, status):
    """The task is gone; the record used to say "wird gebaut…" forever,
    with the button disabled and nothing saying why."""
    make(status=status)
    assert devices.mark_interrupted_jobs() == ["probe"]

    device = devices.get_device("probe")
    assert device.status == BuildStatus.ERROR
    assert "Neustart" in device.build_error


def test_an_interrupted_ota_is_failed_too(registry):
    make(ota_status=BuildStatus.RUNNING)
    devices.mark_interrupted_jobs()
    device = devices.get_device("probe")
    assert device.ota_status == BuildStatus.ERROR
    assert "Neustart" in device.ota_error


def test_a_finished_device_is_left_alone(registry):
    make(status=BuildStatus.SUCCESS)
    assert devices.mark_interrupted_jobs() == []
    assert devices.get_device("probe").status == BuildStatus.SUCCESS
