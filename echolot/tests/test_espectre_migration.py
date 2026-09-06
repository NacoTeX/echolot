"""Tests for surviving ESPectre's September 2026 restructure.

Upstream renamed the detection profiles, moved the packet rate, dropped
the segmentation threshold, turned the calibrate switch into a
recalibrate button, and narrowed the rate's valid range. A device stored
under the old schema must still load — otherwise updating the add-on
silently loses everything the user set up.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def store(monkeypatch, tmp_path):
    """A devices.json on disk, with the module pointed at it."""
    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(tmp_path))
    import importlib

    from app import devices as devices_module

    monkeypatch.setattr(devices_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(devices_module, "INDEX_PATH", tmp_path / "devices.json")
    importlib.reload  # keep the import meaningful to linters
    return devices_module


def legacy_device(**config_overrides):
    config = {
        "name": "flur",
        "friendly_name": "Flur unten",
        "board": "esp32c6",
        "wifi_ssid": "netz",
        "wifi_password": "passwort123",
        "detection_algorithm": "mvs",
        "traffic_generator_rate": 100,
        "traffic_generator_mode": "ping",
        "segmentation_threshold": "auto",
    }
    config.update(config_overrides)
    return {"id": "a", "created_at": 0, "updated_at": 0, "config": config}


def write(store, device):
    (store.INDEX_PATH).write_text(json.dumps({device["id"]: device}), encoding="utf-8")


def test_a_device_from_before_the_restructure_still_loads(store):
    write(store, legacy_device())
    device = store.get_device("a")
    assert device is not None, "an old device must not become unloadable"
    assert device.config.name == "flur"


def test_the_detection_profiles_are_translated(store):
    for old, new in (("mvs", "lightweight"), ("ml", "high_accuracy")):
        write(store, legacy_device(detection_algorithm=old))
        assert store.get_device("a").config.detection_algorithm == new


def test_the_packet_rate_moves_to_its_new_name(store):
    write(store, legacy_device(traffic_generator_rate=40))
    assert store.get_device("a").config.csi_target_pps == 40


def test_a_rate_outside_the_new_range_is_clamped_not_rejected(store):
    """The old field allowed 0-1000; ESPectre's range is 1-500. Refusing
    the device would be worse than adjusting the number."""
    write(store, legacy_device(traffic_generator_rate=1000))
    assert store.get_device("a").config.csi_target_pps == 500
    write(store, legacy_device(traffic_generator_rate=0))
    assert store.get_device("a").config.csi_target_pps == 1


def test_the_dropped_setting_is_removed(store):
    write(store, legacy_device())
    store.get_device("a")
    stored = json.loads(store.INDEX_PATH.read_text())["a"]["config"]
    assert "segmentation_threshold" not in stored
    assert "traffic_generator_rate" not in stored


def test_migration_is_written_back_once_and_is_stable(store):
    write(store, legacy_device())
    first = store.get_device("a").config.model_dump()
    second = store.get_device("a").config.model_dump()
    assert first == second


def test_new_defaults_are_filled_in(store):
    write(store, legacy_device())
    config = store.get_device("a").config
    assert config.csi_traffic_mode == "internal"
    assert config.evaluation_interval_ms == 250
    assert config.direct_api is True


def test_calibration_points_at_a_button_now(store):
    """ESPectre replaced the Calibrate switch with a Recalibrate button."""
    from app.devices import default_entity_ids
    from app.entity_resolver import ENTITY_SPECS

    assert default_entity_ids("flur")["entity_calibrate"] == "button.flur_recalibrate"
    assert ENTITY_SPECS["entity_calibrate"] == ("button", "Recalibrate")


def test_presets_only_use_fields_the_device_model_accepts():
    """A preset naming a field that no longer exists would fail silently
    in the browser, leaving the form on its defaults."""
    from app.devices import DeviceCreate
    from app.presets import PRESETS

    allowed = set(DeviceCreate.model_fields)
    for preset in PRESETS:
        for field in ("csi_target_pps", "detection_algorithm", "evaluation_interval_ms"):
            assert field in allowed
            value = getattr(preset, field)
            # Round-trips through the real validator, ranges included.
            DeviceCreate(
                name="probe", board="esp32c6", wifi_ssid="n", wifi_password="passwort123",
                **{field: value},
            )


def test_api_encryption_is_off_by_default_and_omits_the_block():
    """Not a preference: `encryption:` makes ESPHome pull in libsodium,
    whose C sources include a bare "utils.h" — and ESPectre publishes a
    C++ utils.h on the global include path, so the compile dies on
    `#include <cstdint>`. The firmware cannot be built with both."""
    import os
    import tempfile

    os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp())
    from app.builder import render_yaml
    from app.devices import Device, DeviceCreate

    def render(encryption):
        device = Device(
            id="x", created_at=0, updated_at=0,
            config=DeviceCreate(
                name="probe", board="esp32c6", wifi_ssid="netz",
                wifi_password="passwort123", api_encryption=encryption,
            ),
        )
        return render_yaml(device), device

    default, device = render(False)
    assert DeviceCreate(
        name="probe", board="esp32c6", wifi_ssid="n", wifi_password="passwort123"
    ).api_encryption is False
    assert "encryption:" not in default
    # The key is still generated and kept, so switching it on needs no new one.
    assert device.api_encryption_key

    enabled, device = render(True)
    assert "encryption:" in enabled
    assert device.api_encryption_key in enabled
