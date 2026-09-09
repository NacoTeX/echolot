"""Renders per-device ESPHome YAML and drives `esphome compile` for it."""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.board_registry import get_board
from app.devices import BuildStatus, Device, config_path, device_dir, update_device

logger = logging.getLogger("echolot.builder")

TEMPLATES_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(disabled_extensions=("j2",), default=False),
    trim_blocks=True,
    lstrip_blocks=True,
)

# Guards against triggering two concurrent compiles for the same device.
_building: set[str] = set()
_building_lock = threading.Lock()

#: How many ESPHome compiles may run at once, across all devices.
#:
#: The per-device guard above stops the same device building twice, but
#: said nothing about four devices building at once — and an ESPHome
#: compile is a full C++ toolchain run. Four of those on the kind of
#: hardware Home Assistant usually lives on (a Pi, an old NUC) will swap,
#: thrash, or be killed by the OOM reaper mid-build.
#:
#: One at a time. Later builds wait rather than compete, which is both
#: faster overall and far less likely to fail.
MAX_CONCURRENT_BUILDS = 1
_build_slots = threading.Semaphore(MAX_CONCURRENT_BUILDS)

_LOG_TAIL_CHARS = 20_000

# The line CMake prints when the cross compiler is missing from PATH. It is
# the visible end of a failure that starts much earlier and much quieter —
# see toolchain_compiler() below.
_MISSING_COMPILER_MARKER = "is not a full path and was not found in the PATH"

# ESPHome external-component checkouts can contain no tags.  ESPectre's CMake
# build normally derives its SDK version with ``git describe``, but such a
# checkout of ``main`` contains none of the numeric tags it searches for.
# Upstream explicitly supports this environment-variable fallback for source
# archives and other unstamped checkouts.  Zero is intentionally an
# "unknown development checkout" version rather than pretending that the
# moving main branch is one of ESPectre's releases.  A caller may still provide
# the exact version and wins through setdefault() below.
ESPECTRE_FALLBACK_VERSION = "0.0.0"


def platformio_core_dir() -> Path:
    """Where PlatformIO keeps its downloaded packages.

    The add-on pins this to /data so the ~2 GB of ESP-IDF and toolchain
    survives a restart; outside the add-on PlatformIO's own default
    applies.
    """
    configured = os.environ.get("PLATFORMIO_CORE_DIR")
    return Path(configured) if configured else Path.home() / ".platformio"


def toolchain_compiler(board) -> Path:
    """Path the cross compiler for this board must occupy."""
    return platformio_core_dir() / "packages" / board.toolchain_package / "bin" / board.compiler_binary


def toolchain_state(board) -> dict:
    """Describe the toolchain install, distinguishing absent from broken.

    PlatformIO's builder guards only `isdir(TOOLCHAIN_DIR)` before putting
    that directory's bin/ on PATH. A download interrupted partway leaves
    the directory in place with no compiler inside, so the guard passes and
    the build dies later inside CMake with nothing pointing back at the
    real cause. Telling the two apart is the whole point: "absent" is
    normal before the first build, "broken" needs the package thrown away.
    """
    compiler = toolchain_compiler(board)
    package = compiler.parent.parent
    if compiler.is_file():
        return {"state": "ok", "package": str(package), "compiler": str(compiler)}
    if package.exists():
        return {"state": "broken", "package": str(package), "compiler": str(compiler)}
    return {"state": "absent", "package": str(package), "compiler": str(compiler)}


def reset_toolchain(board) -> bool:
    """Delete this board's toolchain package so the next build re-downloads it.

    Returns False when there was nothing to remove.
    """
    package = toolchain_compiler(board).parent.parent
    if not package.exists():
        return False
    shutil.rmtree(package, ignore_errors=True)
    logger.info("Removed toolchain package %s", package)
    return True


#: The ESPectre commit this Echolot release builds against.
#:
#: Pinned rather than tracking `main`, so a given Echolot version always
#: produces the same firmware base. Moving it is a deliberate act: bump
#: this, rebuild a device, and check it still senses.
#:
#: Since 0.13.6 CI links a real image for one board per instruction set
#: against exactly this commit (tools/compile_firmware.py), so "pinned"
#: now means verified as well as reproducible — for those two boards, on
#: CI's Linux runner, and not on hardware. See DOCS.md.
ESPECTRE_REF = "ce23b0b61b95b87a75f12681a0e576d8f3df5d1b"

#: Fields that must never reach a manifest or a log.
_SECRET_CONFIG_FIELDS = ("wifi_password",)


def esphome_version() -> str:
    try:
        from importlib.metadata import version

        return version("esphome")
    except Exception:  # noqa: BLE001 - a missing version is not a build failure
        return "unbekannt"


def esphome_pin() -> str:
    """The requirement this add-on ships, as written.

    The installed version alone does not say what was *allowed*: a patch
    series is a range, and "which build produced this image" needs both
    ends of that question answered.
    """
    try:
        for line in (Path(__file__).parent / "requirements.txt").read_text().splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("esphome"):
                return stripped
    except OSError:
        pass
    return "unbekannt"


def addon_version() -> str:
    """This add-on's own version.

    Stamped into the image at build time; config.yaml is the fallback for
    a source checkout, where it is the same file Home Assistant reads.
    """
    stamped = os.environ.get("ECHOLOT_VERSION")
    if stamped:
        return stamped.strip()
    try:
        for line in (Path(__file__).parent.parent / "config.yaml").read_text().splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip().strip('"\'')
    except OSError:
        pass
    return "unbekannt"


def addon_revision() -> str | None:
    """The commit this add-on was built from, when it can be known.

    Present in a git checkout and in an image built with the label; absent
    in a plain source copy, where it is reported as unknown rather than
    guessed.
    """
    stamped = os.environ.get("ECHOLOT_BUILD_REVISION")
    if stamped:
        return stamped.strip()
    root = Path(__file__).resolve().parents[2]
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    revision = proc.stdout.strip()
    return revision if proc.returncode == 0 and revision else None


def framework_base(build_dir: Path | None = None) -> dict:
    """Which ESP-IDF and toolchain packages actually went into the image.

    The template asks for `type: esp-idf` and no version, so ESPHome picks
    its recommended framework — which changes with ESPHome. Reading it
    back from what PlatformIO installed is the difference between "we
    asked for esp-idf" and "this image contains esp-idf 5.x.y".
    """
    base: dict = {"type": "esp-idf", "framework_version": None, "packages": {}}
    packages_dir = platformio_core_dir() / "packages"
    if not packages_dir.is_dir():
        return base
    for package in sorted(packages_dir.iterdir()):
        manifest = package / "package.json"
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        name, version_string = data.get("name"), data.get("version")
        if not name or not version_string:
            continue
        base["packages"][str(name)] = str(version_string)
        if str(name) == "framework-espidf":
            base["framework_version"] = str(version_string)
    return base


def config_fingerprint(device: Device) -> str:
    """A short hash of what was built, with the secrets left out."""
    payload = device.config.model_dump()
    for field in _SECRET_CONFIG_FIELDS:
        payload.pop(field, None)
    encoded = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def build_manifest(device: Device, firmware: Path | None) -> dict:
    """What this artefact was made of, so a later question has an answer.

    "Which build produced this image" needs more than the sources: an
    allowed patch series is not a resolved dependency, and `type: esp-idf`
    with no version is whatever ESPHome recommended that week. So the
    manifest names the add-on revision it was built by, carries its own
    id, and records the framework and toolchain packages that were
    actually installed when the image was linked.

    Credentials never appear here — see `_SECRET_CONFIG_FIELDS` and the
    fingerprint below, which hashes the config with them removed.
    """
    manifest = {
        # A single build, nameable in a bug report. Two images built from
        # identical sources are still two builds.
        "build_id": uuid.uuid4().hex,
        "echolot_version": addon_version(),
        "echolot_revision": addon_revision(),
        "espectre_ref": ESPECTRE_REF,
        "esphome_version": esphome_version(),
        # The requirement as written: the installed version alone does not
        # say what the next build would have been allowed to pick.
        "esphome_pin": esphome_pin(),
        "framework": framework_base(),
        "board": device.config.board,
        # Where the compile ran, not what it produced.
        "built_on_arch": os.environ.get("ECHOLOT_BUILD_ARCH") or None,
        "config_hash": config_fingerprint(device),
        # Lets a later reader tell a device flashed with a closed fallback
        # AP from one flashed before 0.13.5, when that AP had no password.
        "fallback_ap_secured": True,
        "built_at": time.time(),
    }
    if firmware is not None and firmware.exists():
        digest = hashlib.sha256()
        with firmware.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        manifest["firmware_sha256"] = digest.hexdigest()
        manifest["firmware_bytes"] = firmware.stat().st_size
    return manifest


def render_yaml(device: Device) -> str:
    board = get_board(device.config.board)
    template = _env.get_template("espectre.yaml.j2")
    return template.render(
        espectre_ref=ESPECTRE_REF,
        device_name=device.config.name,
        friendly_name=device.config.friendly_name or device.config.name,
        board=board,
        wifi_ssid=device.config.wifi_ssid,
        wifi_password=device.config.wifi_password,
        wifi_bssid=device.config.wifi_bssid,
        detection_algorithm=device.config.detection_algorithm,
        csi_target_pps=device.config.csi_target_pps,
        csi_traffic_mode=device.config.csi_traffic_mode,
        traffic_generator_mode=device.config.traffic_generator_mode,
        evaluation_interval_ms=device.config.evaluation_interval_ms,
        direct_api=device.config.direct_api,
        web_server=device.config.web_server,
        diagnostics=device.config.diagnostics,
        log_level=device.config.log_level,
        api_encryption=device.config.api_encryption,
        api_encryption_key=device.api_encryption_key,
        ota_password=device.ota_password,
        fallback_password=device.fallback_password,
    )


def try_start_build(device_id: str) -> bool:
    """Returns False if a build for this device is already running."""
    with _building_lock:
        if device_id in _building:
            return False
        _building.add(device_id)
        return True


def _finish_build(device_id: str) -> None:
    with _building_lock:
        _building.discard(device_id)


def _find_factory_bin(build_dir: Path, not_before: float) -> Path | None:
    """The firmware image this build produced.

    `not_before` is the timestamp the build started. Without it, a compile
    that failed after an earlier one succeeded would hand back the *old*
    image and report success — the user would then flash firmware that
    silently predates their changes.
    """
    if not build_dir.exists():
        return None
    candidates = [
        path
        for path in build_dir.rglob("firmware.factory.bin")
        if path.stat().st_mtime >= not_before
    ]
    return max(candidates, key=lambda p: p.stat().st_mtime, default=None)


def _discard_rendered_config(device_id: str) -> None:
    """Remove the generated YAML once ESPHome is done with it.

    It carries the Wi-Fi password, the API key and the OTA password in
    plain text. Keeping it afterwards buys nothing — it is regenerated
    from the stored device on every build and every OTA push.

    This does not make the add-on secret-free: /data/devices.json still
    holds the same values, because the add-on has to be able to rebuild a
    device and to show its key. /data is the trust boundary either way;
    this just stops the secrets from having a second home, one that also
    ends up in add-on backups.
    """
    try:
        config_path(device_id).unlink(missing_ok=True)
    except OSError as err:  # pragma: no cover - a failure here is not fatal
        logger.warning("Could not remove rendered config for %s: %s", device_id, err)


def _explain_failure(returncode: int, log: str, board) -> str:
    """Turn a compile failure into something the UI can act on.

    Only the missing-compiler case gets special treatment, because it is
    the one where the message ESPHome prints names a symptom
    ("riscv32-esp-elf-gcc ... not found in the PATH") rather than the
    cause, and where the fix is a button rather than a code change.
    """
    if _MISSING_COMPILER_MARKER in log:
        state = toolchain_state(board)
        if state["state"] == "broken":
            return (
                f"Die Toolchain für {board.label} ist unvollständig installiert: "
                f"das Paketverzeichnis existiert, aber {board.compiler_binary} fehlt darin. "
                "Meist bricht der rund 2 GB große Download ab. Setze die Toolchain "
                "zurück und starte den Build erneut."
            )
        if state["state"] == "absent":
            return (
                f"Die Toolchain für {board.label} konnte nicht installiert werden — "
                f"{state['package']} ist gar nicht erst angelegt worden. Prüfe die "
                "Internetverbindung und den freien Speicherplatz und starte den Build erneut."
            )
        return (
            "Der Compiler wurde nicht gefunden, obwohl "
            f"{state['compiler']} vorhanden ist. Sieh ins Build-Log."
        )
    return f"esphome compile exited with code {returncode}"


def _write_rendered_config(device_id: str, yaml_text: str) -> None:
    """Write the generated YAML, readable only by this add-on."""
    path = config_path(device_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml_text, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - some filesystems refuse chmod
        pass


def _run_esphome(argv: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    """Run one ESPHome command, holding a global build slot for its duration.

    Queued rather than refused: someone who presses build on four devices
    means all four, just not all at once.
    """
    env = os.environ.copy()
    env.setdefault("ESPECTRE_GIT_VERSION", ESPECTRE_FALLBACK_VERSION)
    with _build_slots:
        return subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )


def run_ota(device: Device, address: str) -> None:
    """Push the already-built firmware to a running device over the network.

    The point of this is the USB cable: flashing a blank chip needs one,
    but every update after that does not. Browsers without Web Serial —
    everything on iPadOS — can therefore still keep a device current, since
    the upload happens here rather than in the browser.

    Runs synchronously; call via a worker thread like run_build.
    """
    ddir = device_dir(device.id)

    def record(**fields) -> bool:
        return update_device(device.id, **fields) is not None

    record(ota_status=BuildStatus.RUNNING, ota_error=None, ota_log="")

    try:
        # Re-render first: the config carries the OTA password, and an
        # edited device must not be pushed with a stale one.
        _write_rendered_config(device.id, render_yaml(device))

        proc = _run_esphome(
            ["esphome", "upload", str(config_path(device.id)), "--device", address],
            ddir,
            1800,
        )
        log = (proc.stdout or "") + (proc.stderr or "")
        ota_log = log[-_LOG_TAIL_CHARS:]

        if proc.returncode != 0:
            record(
                ota_status=BuildStatus.ERROR,
                ota_log=ota_log,
                ota_error=_explain_ota_failure(proc.returncode, log, address),
            )
        else:
            record(
                ota_status=BuildStatus.SUCCESS,
                ota_log=ota_log,
                ota_error=None,
                ota_last_success=time.time(),
            )
    except subprocess.TimeoutExpired:
        record(
            ota_status=BuildStatus.ERROR,
            ota_error="Das OTA-Update hat nach 30 Minuten aufgegeben",
        )
    except Exception as err:  # noqa: BLE001 - surface it instead of killing the worker
        logger.exception("OTA failed for device %s", device.id)
        record(ota_status=BuildStatus.ERROR, ota_error=str(err))
    finally:
        _discard_rendered_config(device.id)
        _finish_build(device.id)


def _explain_ota_failure(returncode: int, log: str, address: str) -> str:
    """Name the two failures that are not the user's fault to diagnose."""
    lowered = log.lower()
    if "bad magic" in lowered or "authentication" in lowered or "password" in lowered:
        return (
            "Das Gerät hat das OTA-Passwort abgelehnt. Das passiert, wenn die "
            "laufende Firmware älter ist als dieses Passwort — dann hilft nur "
            "einmal Flashen über USB."
        )
    if "resolve" in lowered or "not found" in lowered or "no route" in lowered:
        return (
            f"„{address}“ ist nicht erreichbar. Prüfe die Adresse — bei einem "
            ".local-Namen trag stattdessen die IP-Adresse ein."
        )
    return f"esphome upload endete mit Code {returncode}"


def run_build(device: Device) -> None:
    """Render config and compile firmware. Runs synchronously — call via a worker thread."""
    ddir = device_dir(device.id)
    ddir.mkdir(parents=True, exist_ok=True)

    # Only the fields this job produces are written, and only while the
    # device still exists — see devices.update_device. The Device object
    # here is a snapshot from minutes ago; writing it back whole would
    # undo anything changed meanwhile and resurrect a deleted device.
    def record(**fields) -> bool:
        return update_device(device.id, **fields) is not None

    record(status=BuildStatus.RUNNING, build_error=None, build_log="")

    # Anything the build produces has to be newer than this; see
    # _find_factory_bin.
    started_at = time.time()
    try:
        # Inside the try: an unknown board key raises, and out here that
        # would escape run_build entirely — leaving the device stuck on
        # RUNNING with the build lock never released.
        board = get_board(device.config.board)
        yaml_text = render_yaml(device)
        _write_rendered_config(device.id, yaml_text)

        proc = _run_esphome(["esphome", "compile", str(config_path(device.id))], ddir, 1800)
        log = (proc.stdout or "") + (proc.stderr or "")
        build_log = log[-_LOG_TAIL_CHARS:]

        if proc.returncode != 0:
            record(
                status=BuildStatus.ERROR,
                build_log=build_log,
                build_error=_explain_failure(proc.returncode, log, board),
            )
            return

        firmware = _find_factory_bin(ddir / ".esphome" / "build" / device.config.name, started_at)
        if firmware is None:
            record(
                status=BuildStatus.ERROR,
                build_log=build_log,
                build_error=(
                    "Der Build meldet Erfolg, aber es ist kein neues "
                    "firmware.factory.bin entstanden. Sieh ins Build-Protokoll."
                ),
            )
            return

        record(
            status=BuildStatus.SUCCESS,
            build_log=build_log,
            firmware_bin=str(firmware.relative_to(ddir)),
            chip_family=board.chip_family,
            build_manifest=build_manifest(device, firmware),
        )
    except subprocess.TimeoutExpired:
        record(status=BuildStatus.ERROR, build_error="Build timed out after 30 minutes")
    except Exception as err:  # noqa: BLE001 - surface any failure to the UI instead of crashing the worker
        logger.exception("Build failed for device %s", device.id)
        record(status=BuildStatus.ERROR, build_error=str(err))
    finally:
        _discard_rendered_config(device.id)
        _finish_build(device.id)
