#!/usr/bin/env python3
"""Compile the firmware for real, for one Xtensa and one RISC-V board.

`esphome config` catches a YAML mistake in seconds and catches nothing
else. It does not fetch ESPectre, it does not run a compiler, and it
cannot tell you that the pinned ESPHome and the pinned ESPectre commit
disagree about a header — which is the failure that reaches a user as a
twenty-minute build that ends in a C++ error they did not write.

So this links real images. Three boards rather than six: the two
instruction sets are most of what differs — the Xtensa and the RISC-V
toolchains are separate downloads, separate compilers and separate sets
of chip headers — and the C3 and C6 differ from each other in ways
`esphome config` already covers.

The C5 is the exception, added in 0.13.8. It is the only dual-band chip
here, so it is the only one whose generated `wifi:` block carries a
`band_mode:` at all, and it is the newest of them in ESP-IDF. A config
that validates says nothing about whether that variant links.

Radar nodes (0.14.0) are a different firmware on the same chips: no
ESPectre, Echolot's own `echolot_ld2460` component, and API encryption
switched on — which is exactly what an ESPectre build cannot link. So a
radar target is named separately, as `<board>:ld2460`.

Run from anywhere:

    python echolot/tools/compile_firmware.py                    # the three ESPectre boards
    python echolot/tools/compile_firmware.py esp32c5            # just one
    python echolot/tools/compile_firmware.py esp32c5:ld2460     # a radar node

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

#: One per instruction set, plus the C5 for the reason in the module
#: docstring. `chip_family` in the board registry says which is which.
DEFAULT_BOARDS = ("esp32", "esp32c6", "esp32c5")

#: A cold build downloads a toolchain and compiles ESP-IDF from scratch.
TIMEOUT_SECONDS = 3600


def _config(board: str, sensor: str, name: str) -> dict:
    common = {
        "name": name,
        "friendly_name": f"Smoke {board}",
        "board": board,
        "wifi_ssid": "testnetz",
        "wifi_password": "passwort123",
        # The options that add code rather than change a constant:
        # each one pulls in components the plain build never links.
        "web_server": True,
        "diagnostics": True,
    }
    if sensor == "ld2460":
        return {
            **common,
            "sensor": "ld2460",
            # The C5 is the board the radar actually runs on, a Waveshare
            # ESP32-C5-Zero with its antenna on GPIO26 — so that is the
            # branch linked there. Encryption needs no switch: it is always
            # on for radar nodes, and linking it is half of why this
            # target exists.
            **({"antenna_select_pin": 26} if board == "esp32c5" else {}),
        }
    return {
        **common,
        "direct_api": True,
        # Deliberately off, which is also how it ships. `api:
        # encryption:` pulls in noise-c/libsodium, whose C sources
        # include a bare "utils.h" — and ESPectre publishes its own
        # C++ `core/utils.h` on the global include path, so the C
        # compiler takes that one and dies on `#include <cstdint>`.
        # An upstream build-definition bug (see DOCS.md, "Der
        # Verschlüsselungscode"), not something this repository can
        # fix, and a CI job that must fail is not a CI job.
        "api_encryption": False,
    }


def compile_board(target: str, workdir: Path) -> tuple[bool, str]:
    from app.builder import render_yaml
    from app.devices import Device, DeviceCreate

    board, _, sensor = target.partition(":")
    sensor = sensor or "espectre"
    name = f"smoke-{board}" + ("-radar" if sensor == "ld2460" else "")
    device = Device(
        id=name,
        created_at=0,
        updated_at=0,
        config=DeviceCreate(**_config(board, sensor, name)),
    )
    path = workdir / f"{name}.yaml"
    path.write_text(render_yaml(device), encoding="utf-8")

    # Through the add-on's own runner, not a bare subprocess. It sets
    # ESPECTRE_GIT_VERSION, and without it CMake stops before compiling
    # anything: ESPHome checks out an external component shallow at the
    # pinned commit with no tags, so ESPectre's `git describe` finds
    # nothing to describe. A smoke test that builds in a different
    # environment from the add-on is testing something else.
    from app.builder import _run_esphome

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
    print(f"  ok   {target:15s} {size / 1024:7.0f} KiB in {took / 60:.1f} min")
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
