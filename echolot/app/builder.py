"""Renders per-device ESPHome YAML and drives `esphome compile` for it."""

import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.board_registry import get_board
from app.devices import BuildStatus, Device, config_path, device_dir, save_device

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

_LOG_TAIL_CHARS = 20_000

# The line CMake prints when the cross compiler is missing from PATH. It is
# the visible end of a failure that starts much earlier and much quieter —
# see toolchain_compiler() below.
_MISSING_COMPILER_MARKER = "is not a full path and was not found in the PATH"


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


def render_yaml(device: Device) -> str:
    board = get_board(device.config.board)
    template = _env.get_template("espectre.yaml.j2")
    return template.render(
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
        api_encryption_key=device.api_encryption_key,
        ota_password=device.ota_password,
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


def _find_factory_bin(build_dir: Path) -> Path | None:
    if not build_dir.exists():
        return None
    candidates = sorted(
        build_dir.rglob("firmware.factory.bin"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


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


def run_ota(device: Device, address: str) -> None:
    """Push the already-built firmware to a running device over the network.

    The point of this is the USB cable: flashing a blank chip needs one,
    but every update after that does not. Browsers without Web Serial —
    everything on iPadOS — can therefore still keep a device current, since
    the upload happens here rather than in the browser.

    Runs synchronously; call via a worker thread like run_build.
    """
    ddir = device_dir(device.id)
    device.ota_status = BuildStatus.RUNNING
    device.ota_error = None
    device.ota_log = ""
    save_device(device)

    try:
        # Re-render first: the config carries the OTA password, and an
        # edited device must not be pushed with a stale one.
        config_path(device.id).write_text(render_yaml(device), encoding="utf-8")

        proc = subprocess.run(
            ["esphome", "upload", str(config_path(device.id)), "--device", address],
            cwd=ddir,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        log = (proc.stdout or "") + (proc.stderr or "")
        device.ota_log = log[-_LOG_TAIL_CHARS:]

        if proc.returncode != 0:
            device.ota_status = BuildStatus.ERROR
            device.ota_error = _explain_ota_failure(proc.returncode, log, address)
        else:
            device.ota_status = BuildStatus.SUCCESS
            device.ota_last_success = time.time()
        save_device(device)
    except subprocess.TimeoutExpired:
        device.ota_status = BuildStatus.ERROR
        device.ota_error = "Das OTA-Update hat nach 30 Minuten aufgegeben"
        save_device(device)
    except Exception as err:  # noqa: BLE001 - surface it instead of killing the worker
        logger.exception("OTA failed for device %s", device.id)
        device.ota_status = BuildStatus.ERROR
        device.ota_error = str(err)
        save_device(device)
    finally:
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

    device.status = BuildStatus.RUNNING
    device.build_error = None
    device.build_log = ""
    save_device(device)

    try:
        # Inside the try: an unknown board key raises, and out here that
        # would escape run_build entirely — leaving the device stuck on
        # RUNNING with the build lock never released.
        board = get_board(device.config.board)
        yaml_text = render_yaml(device)
        config_path(device.id).write_text(yaml_text, encoding="utf-8")

        proc = subprocess.run(
            ["esphome", "compile", str(config_path(device.id))],
            cwd=ddir,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        log = (proc.stdout or "") + (proc.stderr or "")
        device.build_log = log[-_LOG_TAIL_CHARS:]

        if proc.returncode != 0:
            device.status = BuildStatus.ERROR
            device.build_error = _explain_failure(proc.returncode, log, board)
            save_device(device)
            return

        firmware = _find_factory_bin(ddir / ".esphome" / "build" / device.config.name)
        if firmware is None:
            device.status = BuildStatus.ERROR
            device.build_error = "Compile succeeded but no firmware.factory.bin was found"
            save_device(device)
            return

        device.firmware_bin = str(firmware.relative_to(ddir))
        device.chip_family = board.chip_family
        device.status = BuildStatus.SUCCESS
        save_device(device)
    except subprocess.TimeoutExpired:
        device.status = BuildStatus.ERROR
        device.build_error = "Build timed out after 30 minutes"
        save_device(device)
    except Exception as err:  # noqa: BLE001 - surface any failure to the UI instead of crashing the worker
        logger.exception("Build failed for device %s", device.id)
        device.status = BuildStatus.ERROR
        device.build_error = str(err)
        save_device(device)
    finally:
        _finish_build(device.id)
