#!/usr/bin/env python3
"""Compile the firmware for real, for one Xtensa and one RISC-V board.

`esphome config` catches a YAML mistake in seconds and catches nothing
else. It does not fetch ESPectre, it does not run a compiler, and it
cannot tell you that the pinned ESPHome and the pinned ESPectre commit
disagree about a header — which is the failure that reaches a user as a
twenty-minute build that ends in a C++ error they did not write.

So this links a real image. Two boards rather than six, because the two
instruction sets are what actually differ: the Xtensa and the RISC-V
toolchains are separate downloads, separate compilers and separate sets
of chip headers, while ESP32-C3/C5/C6 differ from each other in ways
`esphome config` already covers.

Run from anywhere:

    python echolot/tools/compile_firmware.py            # esp32 and esp32c6
    python echolot/tools/compile_firmware.py esp32c6    # just one

It is slow on a cold cache — ESP-IDF and a toolchain are around 2 GB per
instruction set — and that is the price of knowing.
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

#: One per instruction set. `chip_family` in the board registry says which.
DEFAULT_BOARDS = ("esp32", "esp32c6")

#: A cold build downloads a toolchain and compiles ESP-IDF from scratch.
TIMEOUT_SECONDS = 3600


def compile_board(board: str, workdir: Path) -> tuple[bool, str]:
    from app.builder import render_yaml
    from app.devices import Device, DeviceCreate

    name = f"smoke-{board}"
    device = Device(
        id=name,
        created_at=0,
        updated_at=0,
        config=DeviceCreate(
            name=name,
            friendly_name=f"Smoke {board}",
            board=board,
            wifi_ssid="testnetz",
            wifi_password="passwort123",
            # The options that add code rather than change a constant:
            # each one pulls in components the plain build never links.
            web_server=True,
            diagnostics=True,
            api_encryption=True,
            direct_api=True,
        ),
    )
    path = workdir / f"{name}.yaml"
    path.write_text(render_yaml(device), encoding="utf-8")

    started = time.monotonic()
    result = subprocess.run(
        ["esphome", "compile", str(path)],
        capture_output=True,
        text=True,
        cwd=workdir,
        timeout=TIMEOUT_SECONDS,
    )
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
    print(f"  ok   {board:8s} {size / 1024:7.0f} KiB in {took / 60:.1f} min")
    return True, log


def main(argv: list[str]) -> int:
    boards = tuple(argv[1:]) or DEFAULT_BOARDS
    workdir = Path(os.environ.get("ECHOLOT_SMOKE_DIR") or tempfile.mkdtemp(prefix="echolot-smoke-"))
    workdir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("ECHOLOT_DATA_DIR", str(workdir / "data"))

    from app.builder import ESPECTRE_REF, esphome_version

    print(f"ESPHome {esphome_version()} gegen ESPectre {ESPECTRE_REF[:12]}")
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
