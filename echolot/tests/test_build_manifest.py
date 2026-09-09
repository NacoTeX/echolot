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


# --- full traceability (review R8) -------------------------------------


def test_every_build_is_nameable_on_its_own():
    """Two images from identical sources are still two builds, and a bug
    report needs to say which one."""
    one = builder.build_manifest(make(), None)
    two = builder.build_manifest(make(), None)
    assert one["build_id"] != two["build_id"]
    assert len(one["build_id"]) == 32


def test_the_manifest_names_the_echolot_build_that_produced_it():
    """The pinned sources say what went in; they do not say which
    revision of this add-on assembled it."""
    manifest = builder.build_manifest(make(), None)
    assert manifest["echolot_version"] == builder.addon_version()
    assert manifest["echolot_version"] != "unbekannt"
    # A source checkout has a commit; an image has it stamped in. Only a
    # plain source copy has neither, and then it says so rather than
    # guessing.
    assert "echolot_revision" in manifest


def test_a_stamped_image_reports_its_revision_without_git(monkeypatch):
    """The container has no checkout to ask, which is why the Dockerfile
    passes BUILD_REF in."""
    monkeypatch.setenv("ECHOLOT_BUILD_REVISION", "0123456789abcdef" * 2 + "01234567")
    monkeypatch.setenv("ECHOLOT_VERSION", "9.9.9")
    manifest = builder.build_manifest(make(), None)
    assert manifest["echolot_revision"].startswith("0123456789abcdef")
    assert manifest["echolot_version"] == "9.9.9"


def test_the_dockerfile_passes_the_build_stamps_through():
    """Otherwise the fields above are always unknown in the shipped image,
    which is the only place they matter."""
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    for name in ("BUILD_VERSION", "BUILD_REF", "BUILD_ARCH"):
        assert f"ARG {name}" in dockerfile, f"{name} wird nicht entgegengenommen"
    assert "ECHOLOT_BUILD_REVISION=${BUILD_REF}" in dockerfile
    assert "ECHOLOT_VERSION=${BUILD_VERSION}" in dockerfile


def test_the_manifest_names_the_allowed_esphome_range_not_just_the_installed_one():
    """An allowed patch series is not a resolved dependency: the installed
    version does not say what the next build could have picked."""
    manifest = builder.build_manifest(make(), None)
    assert manifest["esphome_pin"].startswith("esphome")
    assert "<" in manifest["esphome_pin"], "eine Obergrenze fehlt"
    assert manifest["esphome_pin"] == builder.esphome_pin()


def test_the_pin_is_read_from_the_requirements_that_ship():
    """Not from a second copy that can drift from it."""
    requirements = (Path(__file__).resolve().parents[1] / "app" / "requirements.txt").read_text()
    assert builder.esphome_pin() in requirements


def test_the_manifest_records_the_framework_that_was_installed():
    """`type: esp-idf` with no version is whatever ESPHome recommended
    that week. The image contains one particular ESP-IDF."""
    manifest = builder.build_manifest(make(), None)
    framework = manifest["framework"]
    assert framework["type"] == "esp-idf"
    # Absent on a machine that has never built firmware — reported as
    # unknown rather than invented.
    assert "framework_version" in framework
    assert isinstance(framework["packages"], dict)


def test_the_framework_version_comes_from_the_installed_package(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORMIO_CORE_DIR", str(tmp_path))
    package = tmp_path / "packages" / "framework-espidf"
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps({"name": "framework-espidf", "version": "5.3.2"}), encoding="utf-8"
    )
    toolchain = tmp_path / "packages" / "toolchain-riscv32-esp"
    toolchain.mkdir(parents=True)
    (toolchain / "package.json").write_text(
        json.dumps({"name": "toolchain-riscv32-esp", "version": "14.2.0"}), encoding="utf-8"
    )

    base = builder.framework_base()
    assert base["framework_version"] == "5.3.2"
    assert base["packages"]["toolchain-riscv32-esp"] == "14.2.0"


def test_a_broken_package_manifest_does_not_break_the_build(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORMIO_CORE_DIR", str(tmp_path))
    package = tmp_path / "packages" / "kaputt"
    package.mkdir(parents=True)
    (package / "package.json").write_text("{nicht json", encoding="utf-8")
    assert builder.framework_base()["packages"] == {}


def test_the_new_fields_carry_no_secrets_either(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOLOT_BUILD_REVISION", "abc123")
    device = make()
    encoded = json.dumps(builder.build_manifest(device, None))
    for secret in (device.config.wifi_password, device.api_encryption_key,
                   device.ota_password, device.fallback_password):
        assert secret not in encoded


# --- the environment a build actually runs in ---------------------------


def test_a_build_names_the_espectre_version_for_cmake(monkeypatch):
    """ESPHome checks out an external component shallow at the pinned
    commit, with no tags, so ESPectre's `git describe` finds nothing and
    its CMake stops before a compiler ever runs. The add-on passes the
    version in; anything that builds firmware has to do the same."""
    seen = {}

    def fake_run(argv, cwd=None, capture_output=None, text=None, timeout=None, env=None):
        seen.update(env or {})

        class Result:
            returncode = 0
            stdout = stderr = ""

        return Result()

    monkeypatch.setattr(builder.subprocess, "run", fake_run)
    builder._run_esphome(["esphome", "compile", "x.yaml"], Path("."), 10)
    assert seen.get("ESPECTRE_GIT_VERSION") == builder.ESPECTRE_FALLBACK_VERSION


def test_the_compile_smoke_uses_the_same_runner():
    """It shelled out on its own at first, and CI failed in CMake before
    compiling a single file. A smoke test that builds in a different
    environment from the add-on is testing something else."""
    source = (Path(__file__).resolve().parents[1] / "tools" / "compile_firmware.py").read_text()
    assert "_run_esphome" in source
    assert "subprocess.run" not in source, "der Smoke-Test baut wieder an der Umgebung vorbei"
