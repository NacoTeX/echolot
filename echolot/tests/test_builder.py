"""Tests for build orchestration.

Two failures these guard against, both of which only show up on the kind
of hardware Home Assistant actually runs on:

  * Four devices building at once means four C++ toolchain runs at once.
    On a Pi that swaps, thrashes, or gets one of them killed mid-build.
  * A compile that fails after an earlier one succeeded leaves the old
    firmware image on disk. Handing that back as the build's output means
    flashing firmware that silently predates the change.
"""

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import builder  # noqa: E402


class Completed:
    returncode = 0
    stdout = ""
    stderr = ""


def test_only_one_esphome_runs_at_a_time(monkeypatch):
    """The per-device guard says nothing about four different devices."""
    state = {"running": 0, "peak": 0}
    lock = threading.Lock()

    def fake_run(argv, cwd, capture_output, text, timeout, env):
        with lock:
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
        time.sleep(0.15)
        with lock:
            state["running"] -= 1
        return Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)

    threads = [
        threading.Thread(target=builder._run_esphome, args=(["esphome"], ".", 10))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert state["peak"] == 1, f"{state['peak']} compiles ran at once"


def test_all_queued_builds_still_run(monkeypatch):
    """Queued, not refused: pressing build on four devices means four."""
    calls = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: (calls.append(1), Completed())[1],
    )
    threads = [
        threading.Thread(target=builder._run_esphome, args=(["esphome"], ".", 10))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(calls) == 4


def test_espectre_version_is_stamped_for_shallow_external_component(monkeypatch):
    """An ESPHome checkout may have no tags for ESPectre's git describe."""
    seen = {}

    def fake_run(*args, **kwargs):
        seen.update(kwargs["env"])
        return Completed()

    monkeypatch.delenv("ESPECTRE_GIT_VERSION", raising=False)
    monkeypatch.setattr(subprocess, "run", fake_run)
    builder._run_esphome(["esphome", "compile", "probe.yaml"], Path("."), 10)

    assert seen["ESPECTRE_GIT_VERSION"] == builder.ESPECTRE_FALLBACK_VERSION


def test_an_explicit_espectre_version_is_not_overridden(monkeypatch):
    seen = {}

    def fake_run(*args, **kwargs):
        seen.update(kwargs["env"])
        return Completed()

    monkeypatch.setenv("ESPECTRE_GIT_VERSION", "1.2.3")
    monkeypatch.setattr(subprocess, "run", fake_run)
    builder._run_esphome(["esphome", "compile", "probe.yaml"], Path("."), 10)

    assert seen["ESPECTRE_GIT_VERSION"] == "1.2.3"


def test_a_stale_firmware_image_is_not_reported_as_this_builds_output(tmp_path):
    """The dangerous case: an old image left by a previous successful
    build, picked up after a compile that produced nothing."""
    build_dir = tmp_path / ".pioenvs" / "probe"
    build_dir.mkdir(parents=True)
    stale = build_dir / "firmware.factory.bin"
    stale.write_bytes(b"old")
    os.utime(stale, (1000, 1000))

    assert builder._find_factory_bin(tmp_path, not_before=2000) is None
    assert builder._find_factory_bin(tmp_path, not_before=500) == stale


def test_a_fresh_firmware_image_is_found(tmp_path):
    build_dir = tmp_path / ".pioenvs" / "probe"
    build_dir.mkdir(parents=True)
    fresh = build_dir / "firmware.factory.bin"
    fresh.write_bytes(b"new")
    assert builder._find_factory_bin(tmp_path, not_before=time.time() - 60) == fresh


def test_a_missing_build_directory_is_not_an_error(tmp_path):
    assert builder._find_factory_bin(tmp_path / "gibtsnicht", not_before=0) is None


def test_the_rendered_config_is_removed_and_was_owner_only(monkeypatch, tmp_path):
    """It carries the Wi-Fi password, API key and OTA password in clear
    text, and is regenerated on every build — so it has no business
    outliving the compile."""
    monkeypatch.setattr(builder, "config_path", lambda device_id: tmp_path / f"{device_id}.yaml")

    builder._write_rendered_config("dev", "esphome:\n  name: probe\n")
    path = tmp_path / "dev.yaml"
    assert path.exists()
    assert path.stat().st_mode & 0o077 == 0, "readable beyond the owner"

    builder._discard_rendered_config("dev")
    assert not path.exists()
    # Removing it twice must not raise; both jobs clean up in `finally`.
    builder._discard_rendered_config("dev")
