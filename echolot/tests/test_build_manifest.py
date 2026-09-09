"""What a firmware was built from, recorded with the firmware.

From the external review (P2). The template used `ref: main` and the
requirement was `esphome>=2024.9` with no ceiling, so the same Echolot
release built a different firmware base next month and "it worked
yesterday" stopped being a usable sentence.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import builder  # noqa: E402
from app.devices import Device, DeviceCreate  # noqa: E402


def make(**config):
    return Device(
        id="probe",
        created_at=0,
        updated_at=0,
        config=DeviceCreate(
            name="probe", board="esp32c6", wifi_ssid="netz",
            wifi_password="passwort123", **config
        ),
    )


def test_the_rendered_config_names_a_commit_not_a_branch():
    yaml_text = builder.render_yaml(make())
    assert f"ref: {builder.ESPECTRE_REF}" in yaml_text
    assert "ref: main" not in yaml_text
    assert len(builder.ESPECTRE_REF) == 40, "ein Branchname ist kein Commit"


def test_the_manifest_says_what_it_was_built_from():
    manifest = builder.build_manifest(make(), None)
    assert manifest["espectre_ref"] == builder.ESPECTRE_REF
    assert manifest["board"] == "esp32c6"
    assert manifest["esphome_version"]
    assert manifest["config_hash"]


def test_the_manifest_carries_no_secrets():
    """It ends up in the API response and in the UI."""
    device = make()
    manifest = builder.build_manifest(device, None)
    encoded = json.dumps(manifest)
    assert device.config.wifi_password not in encoded
    assert device.api_encryption_key not in encoded
    assert device.ota_password not in encoded


def test_the_fingerprint_changes_with_the_configuration():
    quiet = builder.config_fingerprint(make(csi_target_pps=50))
    busy = builder.config_fingerprint(make(csi_target_pps=100))
    assert quiet != busy


def test_the_fingerprint_ignores_the_wifi_password():
    """Two devices differing only in a secret built the same firmware
    logic; the hash is for comparing configurations, not credentials."""
    one = make()
    other = make()
    other.config.wifi_password = "einanderespasswort"
    assert builder.config_fingerprint(one) == builder.config_fingerprint(other)


def test_the_firmware_checksum_is_recorded(tmp_path):
    firmware = tmp_path / "firmware.factory.bin"
    firmware.write_bytes(b"nicht wirklich firmware")
    manifest = builder.build_manifest(make(), firmware)
    assert len(manifest["firmware_sha256"]) == 64
    assert manifest["firmware_bytes"] == len(b"nicht wirklich firmware")


# --- the fallback access point (P2) ------------------------------------


def test_the_fallback_access_point_has_a_password():
    """It carries a captive portal that takes Wi-Fi credentials, and it
    comes up exactly when something is already wrong."""
    import yaml

    rendered = yaml.safe_load(builder.render_yaml(make()))
    access_point = rendered["wifi"]["ap"]
    assert access_point["password"], "der Notfall-AP ist offen"
    assert len(access_point["password"]) >= 8, "ESPHome verlangt mindestens acht Zeichen"


def test_each_device_gets_its_own_fallback_password():
    assert make().fallback_password != make().fallback_password


def test_the_fallback_password_is_revealed_with_the_other_credentials():
    device = make()
    assert device.credentials()["fallback_password"] == device.fallback_password


def test_a_build_records_that_the_access_point_is_closed():
    """So a device flashed before 0.13.5 can be told from one flashed
    after it — the password only reaches hardware through a rebuild."""
    assert builder.build_manifest(make(), None)["fallback_ap_secured"] is True
