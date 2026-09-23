#!/usr/bin/env python3
"""Render the firmware template for every board and validate it with ESPHome.

Bugs have reached a release because the generated YAML was only ever read,
never validated — `ota: platform: esp32` (no such platform), and
`cpu_frequency: 240MHz` on chips that top out lower. `esphome config`
catches that kind in seconds, so CI runs it over every board on every
change.

Run from anywhere: `python echolot/tools/validate_firmware.py`
"""

import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="echolot-cfgcheck-"))
    # devices.py resolves its data directory at import time.
    import os

    os.environ.setdefault("ECHOLOT_DATA_DIR", str(workdir / "data"))

    from app.board_registry import BOARDS
    from app.builder import render_yaml
    from app.devices import Device, DeviceCreate

    # Every board on its suggested pins, then the branches only some
    # configs take: the Waveshare C5-Zero's antenna pin, the optional
    # blocks off with the "quiet means empty" rule on, and the C5's other
    # two bands — ESPHome accepts `band_mode` only on that variant, and
    # the rendered value has to be one of its three spellings. The
    # external component is validated along with the YAML, so a schema
    # error in echolot_ld2460/__init__.py fails here, not at compile time.
    cases = [(key, True, True, None, {}) for key in BOARDS]
    cases.append(("esp32c5", True, True, None, {"antenna_select_pin": 26}))
    cases.append(("esp32c5", False, False, None, {"radar_quiet_means_empty": True}))
    cases += [("esp32c5", True, True, band, {}) for band in ("5GHz", "auto")]

    failures = []
    for board, web, diag, band, extra in cases:
        name = f"probe-{board}"
        if extra.get("antenna_select_pin") is not None:
            name += "-antenna"
        if not (web and diag):
            name += "-minimal"
        if band:
            name += "-" + band.replace(".", "").replace("GHz", "g").lower()
        device = Device(
            id=name,
            created_at=0,
            updated_at=0,
            config=DeviceCreate(
                name=name,
                friendly_name=f"Probe {board}",
                board=board,
                wifi_ssid="testnetz",
                wifi_password="passwort123",
                web_server=web,
                diagnostics=diag,
                **({"wifi_band": band} if band else {}),
                **extra,
            ),
        )
        path = workdir / f"{name}.yaml"
        path.write_text(render_yaml(device), encoding="utf-8")

        result = subprocess.run(
            ["esphome", "config", str(path)],
            capture_output=True,
            text=True,
            cwd=workdir,
        )
        label = (
            f"{board:9s} web_server={int(web)} diagnostics={int(diag)} band={band or '-'}"
            + (f" antenna=GPIO{extra['antenna_select_pin']}" if "antenna_select_pin" in extra else "")
        )
        if result.returncode == 0:
            print(f"  ok   {label}")
        else:
            failures.append(label)
            print(f"  FAIL {label}")
            # ESPHome prints "Failed config" and the offending block to
            # stdout, and only its INFO lines to stderr.
            tail = ((result.stdout or "") + (result.stderr or "")).strip().splitlines()[-20:]
            print("\n".join(f"       {line}" for line in tail))

    print()
    if failures:
        print(f"{len(failures)} of {len(cases)} configurations failed to validate")
        return 1
    print(f"All {len(cases)} configurations validate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
