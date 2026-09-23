#!/usr/bin/env python3
"""Compile the radar firmware for real, for one Xtensa and one RISC-V board.

`esphome config` catches a YAML mistake in seconds and catches nothing
else. It does not run a compiler, and it cannot tell you that the pinned
ESPHome and Echolot's own `echolot_ld2460` component disagree about a
header or an API — the failure that reaches a user as a twenty-minute
build ending in a C++ error they did not write.

So this links real images. Two boards: the classic ESP32 for the Xtensa
toolchain, and the ESP32-C5 for RISC-V — it is the board the radar runs
on (a Waveshare ESP32-C5-Zero, antenna on GPIO26), the only dual-band
chip here, so the only one whose `wifi:` block carries a `band_mode:`,
and the newest of them in ESP-IDF.

Run from anywhere:

    python echolot/tools/compile_firmware.py              # esp32 and esp32c5
    python echolot/tools/compile_firmware.py esp32c6      # just one board

A `:ld2460` suffix, as 0.14 CI wrote it, is accepted and ignored — every
image is a radar image now.

It is slow on a cold cache — ESP-IDF and a toolchain are around 2 GB per
instruction set — and that is the price of knowing.
"""

import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_BOARDS = ("esp32", "esp32c5")

#: A cold build downloads a toolchain and compiles ESP-IDF from scratch.
TIMEOUT_SECONDS = 3600


def _config(board: str, name: str) -> dict:
    return {
        "name": name,
        "friendly_name": f"Smoke {board}",
        "board": board,
        "wifi_ssid": "testnetz",
        "wifi_password": "passwort123",
        # The options that add code rather than change a constant: each
        # one pulls in components the plain build never links.
        "web_server": True,
        "diagnostics": True,
        **({"antenna_select_pin": 26} if board == "esp32c5" else {}),
    }


def compile_board(target: str, workdir: Path) -> tuple[bool, str]:
    from app.builder import _run_esphome, render_yaml
    from app.devices import Device, DeviceCreate

    board = target.partition(":")[0]
    name = f"smoke-{board}"
    device = Device(id=name, created_at=0, updated_at=0, config=DeviceCreate(**_config(board, name)))
    path = workdir / f"{name}.yaml"
    path.write_text(render_yaml(device), encoding="utf-8")

    # Through the add-on's own runner, so the smoke test builds in the
    # environment the add-on builds in.
    started = time.monotonic()
    result = _run_esphome(["esphome", "compile", str(path)], workdir, TIMEOUT_SECONDS)
    took = time.monotonic() - started
    log = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        return False, log

    # A zero exit code is not an image. ESPHome has reported success
    # while producing nothing more than once.
    images = list((workdir / ".esphome" / "build" / name).rglob("firmware.factory.bin"))
    if not images:
        return False, log + "\nKein firmware.factory.bin entstanden.\n"
    size = images[0].stat().st_size
    print(f"  ok   {board:10s} {size / 1024:7.0f} KiB in {took / 60:.1f} min")
    return True, log


def main(argv: list[str]) -> int:
    boards = tuple(argv[1:]) or DEFAULT_BOARDS
    workdir = Path(os.environ.get("ECHOLOT_SMOKE_DIR") or tempfile.mkdtemp(prefix="echolot-smoke-"))
    workdir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("ECHOLOT_DATA_DIR", str(workdir / "data"))

    from app.builder import esphome_version, radar_component_digest

    print(f"ESPHome {esphome_version()}, echolot_ld2460 {radar_component_digest()[:12]}")
    print(f"Arbeitsverzeichnis: {workdir}\n")

    failures = []
    for board in boards:
        ok, log = compile_board(board, workdir)
        if not ok:
            failures.append(board)
            print(f"  FAIL {board}")
            print("\n".join(f"       {line}" for line in log.strip().splitlines()[-40:]))

    print()
    if failures:
        print(f"{len(failures)} von {len(boards)} Boards ließen sich nicht bauen: {', '.join(failures)}")
        return 1
    print(f"Alle {len(boards)} Boards gebaut")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
