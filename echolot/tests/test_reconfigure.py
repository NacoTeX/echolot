"""Changing a firmware option without throwing the device away.

Until 0.13.9 every firmware field was settled when the device was
created. Wanting a different packet rate meant deleting the device and
making a new one — which loses its id, its Home Assistant entity ids, its
learned profile, its recordings and its credentials, all to change one
number that is baked into an image.

The other half of the same gap: once a field *can* change, the config and
the flashed image can disagree, and nothing said so. A device whose card
claims 2.4 GHz while the chip is still running the 5 GHz build it was
flashed with is worse than one that could not be edited at all. So the
build manifest's fingerprint is compared against the config's, and the
answer is shown.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import builder, devices, main, presence_rate, samples  # noqa: E402
from app.devices import BuildStatus, Device, DeviceCreate  # noqa: E402
from app.telemetry import Sample  # noqa: E402

PROFILE = presence_rate.RateProfile(
    crossing_threshold=1e-3, baseline_rate=0.05, baseline_spread=0.01,
    window_seconds=60.0, sample_count=1200, observed_seconds=1200.0,
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(devices, "INDEX_PATH", tmp_path / "devices.json")
    monkeypatch.setattr(devices, "DEVICES_DIR", tmp_path / "devices")
    return tmp_path


def make(board="esp32c6", **config) -> Device:
    return devices.create_device(
        DeviceCreate(name="flur", friendly_name="Flur", board=board, wifi_ssid="netz",
                     wifi_password="passwort123", **config)
    )


def built(device: Device) -> Device:
    """A device as it looks after a successful build of its current config."""
    device.status = BuildStatus.SUCCESS
    device.build_manifest = builder.build_manifest(device, None)
    devices.save_device(device)
    return device


# --- what survives ------------------------------------------------------


def test_changing_a_firmware_option_keeps_everything_that_is_not_it(store):
    """The whole reason this exists. Deleting and recreating was the only
    way to change one number, and it cost all of this."""
    device = make()
    device.entity_motion = "binary_sensor.flur_motion_detected"
    device.presence_profile = PROFILE.as_dict()
    devices.save_device(device)
    before = devices.get_device(device.id)

    after = devices.reconfigure(device.id, {"csi_target_pps": 50})

    assert after.config.csi_target_pps == 50
    assert after.id == before.id
    assert after.entity_motion == before.entity_motion
    assert after.presence_profile == before.presence_profile
    assert after.api_encryption_key == before.api_encryption_key
    assert after.ota_password == before.ota_password
    assert after.fallback_password == before.fallback_password
    assert after.created_at == before.created_at


def test_only_the_named_fields_move(store):
    device = make(csi_target_pps=200, web_server=False)
    after = devices.reconfigure(device.id, {"log_level": "DEBUG"})

    assert after.config.log_level == "DEBUG"
    assert after.config.csi_target_pps == 200
    assert after.config.web_server is False
    assert after.config.wifi_ssid == "netz"


def test_the_password_is_not_lost_by_leaving_it_out(store):
    """`public()` masks it, so a UI that round-trips the device would send
    the mask back. Leaving a field out has to mean "unchanged"."""
    device = make()
    after = devices.reconfigure(device.id, {"csi_target_pps": 50})

    assert after.config.wifi_password == "passwort123"


def test_a_changed_password_is_taken(store):
    after = devices.reconfigure(make().id, {"wifi_password": "neuespasswort"})

    assert after.config.wifi_password == "neuespasswort"


# --- what cannot change -------------------------------------------------


def test_the_node_name_is_not_a_setting(store):
    """Home Assistant builds every entity id from it, and so does the OTA
    hostname. Renaming here would silently orphan the entities the zones
    point at — a new device is the honest way to get a new name."""
    device = make()
    with pytest.raises(ValueError, match="name"):
        devices.reconfigure(device.id, {"name": "wohnzimmer"})


def test_the_board_is_not_a_setting(store):
    """Another chip is another device: different image, and a baseline
    learned on the old one describes different hardware."""
    with pytest.raises(ValueError, match="board"):
        devices.reconfigure(make().id, {"board": "esp32c5"})


def test_an_unknown_field_is_refused_rather_than_ignored(store):
    """Pydantic drops unknown keys, so a typo would report success and
    change nothing."""
    with pytest.raises(ValueError, match="csi_target_ppss"):
        devices.reconfigure(make().id, {"csi_target_ppss": 50})


def test_the_same_rules_apply_as_when_the_device_was_created(store):
    """Not a second copy of the validation — the merged config goes
    through `DeviceCreate`, so every cross-field rule runs once and in one
    place."""
    device = make(board="esp32c6")
    with pytest.raises(ValueError, match="ESP32-C5"):
        devices.reconfigure(device.id, {"wifi_band": devices.BAND_5})

    with pytest.raises(ValueError, match="8 characters"):
        devices.reconfigure(device.id, {"wifi_password": "kurz"})

    with pytest.raises(ValueError):
        devices.reconfigure(device.id, {"csi_target_pps": 9000})


def test_a_refused_change_leaves_the_stored_device_alone(store):
    device = make()
    with pytest.raises(ValueError):
        devices.reconfigure(device.id, {"csi_target_pps": 9000})

    assert devices.get_device(device.id).config.csi_target_pps == 100


def test_reconfiguring_a_device_that_is_gone_says_so(store):
    assert devices.reconfigure("weg", {"csi_target_pps": 50}) is None


# --- the image and the config can now disagree --------------------------


def test_an_unbuilt_device_is_not_behind_anything(store):
    """Nothing is flashed, so there is nothing for the config to be ahead
    of. Saying "behind" here would put a warning on every new device."""
    device = make()

    assert device.build_manifest is None
    assert device.firmware_behind_config is False


def test_a_freshly_built_device_matches_its_image(store):
    assert built(make()).firmware_behind_config is False


def test_a_changed_option_puts_the_image_behind_the_config(store):
    device = built(make())

    after = devices.reconfigure(device.id, {"csi_target_pps": 50})

    assert after.firmware_behind_config is True
    assert after.public()["firmware_behind_config"] is True


def test_rebuilding_catches_the_image_up(store):
    device = built(make())
    changed = devices.reconfigure(device.id, {"csi_target_pps": 50})
    assert changed.firmware_behind_config is True

    assert built(changed).firmware_behind_config is False


def test_a_manifest_from_before_the_hash_existed_is_not_called_behind(store):
    """An old record has no `config_hash`. Reporting every one of them as
    stale would be a warning nobody can clear."""
    device = make()
    device.build_manifest = {"espectre_ref": "0" * 40}
    devices.save_device(device)

    assert device.firmware_behind_config is False


def test_even_the_display_name_puts_the_image_behind(store):
    """Checked rather than assumed, and the assumption was wrong: the
    template renders `friendly_name:` into the ESPHome config, and Home
    Assistant builds the device name — and with it every entity id — from
    what the *firmware* reports. So the rename does not take effect until
    the device is reflashed, and saying otherwise would leave somebody
    waiting for a change that never arrives."""
    device = built(make())

    after = devices.reconfigure(device.id, {"friendly_name": "Diele"})

    assert after.config.friendly_name == "Diele"
    assert after.firmware_behind_config is True


def test_the_overview_asks_for_the_rebuild(store):
    from app import overview

    device = built(make())
    changed = devices.reconfigure(device.id, {"csi_target_pps": 50})

    problems = overview.collect_problems(
        device_states=[(changed, None)],
        zones_without_devices=[],
        mqtt_status={"connected": True},
        mqtt_wanted=True,
        esphome={"available": True, "version": "ESPHome 2026.6.5"},
    )

    stale = [p for p in problems if p.kind == "firmware_behind_config"]
    assert len(stale) == 1
    assert stale[0].device_id == changed.id


# --- and the 0.13.8 machinery stops being theoretical -------------------


def test_moving_a_c5_to_another_band_silences_its_baseline(store, monkeypatch):
    """0.13.8 recorded the band on a profile and said plainly that the
    check was a guard on restored data, because nothing could change a
    band. Now something can, and the guard is load-bearing."""
    monkeypatch.delenv("ECHOLOT_SAMPLE_SOURCE", raising=False)
    monkeypatch.setattr(main, "evaluator", main.ZoneEvaluator())
    main._rate_state.clear()

    device = make(board="esp32c5")
    device.presence_profile = {**PROFILE.as_dict(), "band": devices.BAND_24}
    devices.save_device(device)

    import time
    end = time.time()
    samples.bus.forget(device.id)
    for index in range(60):
        samples.bus.publish(
            device.id,
            Sample(t=end - 60 + index, movement_score=0.5 if index < 40 else 0.0,
                   threshold=0.0, motion=False),
            source=samples.SOURCE_HOME_ASSISTANT,
        )
    try:
        assert main._device_rate_evidence(device)[0] is True

        moved = devices.reconfigure(device.id, {"wifi_band": devices.BAND_5})
        main.evaluator._device_verdicts.clear()

        assert main._device_rate_evidence(moved) == (None, main.RATE_BAND_MISMATCH)
    finally:
        samples.bus.forget(device.id)
        main._rate_state.clear()


# --- through the API ----------------------------------------------------


def test_the_route_reports_what_was_refused(store, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main.devices, "INDEX_PATH", devices.INDEX_PATH)
    device = make()
    client = TestClient(main.app)

    ok = client.patch(f"/api/devices/{device.id}/config", json={"csi_target_pps": 50})
    assert ok.status_code == 200
    assert ok.json()["config"]["csi_target_pps"] == 50
    assert ok.json()["config"]["wifi_password"] == "********"

    for payload, fragment in (
        ({"board": "esp32c5"}, "board"),
        ({"nonsense": 1}, "nonsense"),
        ({"csi_target_pps": 9000}, ""),
    ):
        refused = client.patch(f"/api/devices/{device.id}/config", json=payload)
        assert refused.status_code == 422, payload
        assert fragment in str(refused.json()["detail"])

    assert client.patch("/api/devices/weg/config", json={}).status_code == 404
    assert devices.get_device(device.id).config.csi_target_pps == 50
