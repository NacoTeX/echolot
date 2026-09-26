"""The radar component, compiled by ESPHome and run against a fake module.

tests/test_ld2460_protocol.py covers the protocol core. This covers the
component around it — the part that reads the UART, sends the probe,
decides what silence means and publishes the frame line — by running the
real thing: ESPHome generates a firmware for its `host` platform, the
host compiler builds it, and it talks to a pseudo-terminal that this test
plays the LD2460 on.

What that proves and what it does not: the component's own logic, its
use of the ESPHome API at the pinned version, and the bytes it puts on
the wire. Not the ESP-IDF UART driver, not timing on a real chip, and not
the module — for those there is CI's cross-compile and, in the end, a
module on a desk.

Two component instances on two ports, so both answers to "is a quiet
module an empty room" are exercised by one build.

Skipped where ESPHome or a C++ compiler is missing. CI's unit-test job
installs the add-on's requirements, which include ESPHome, so it runs
there.
"""

import os
import re
import select
import shutil
import subprocess
import sys
import time
import tty
from pathlib import Path

import pytest

COMPONENTS = Path(__file__).resolve().parents[1] / "app" / "esphome_components"

PROBE = bytes.fromhex("fdfcfbfa060c000104030201")
VERSION_QUERY = bytes.fromhex("fdfcfbfa0b0c000104030201")
ACK_REPORTING_ON = bytes.fromhex("fdfcfbfa060c001104030201")
ACK_VERSION = bytes.fromhex("fdfcfbfa0b10000119060102" + "04030201")
REPORT_ONE = bytes.fromhex("f4f3f2f1040f000f001700f8f7f6f5")  # (1.5 m, 2.3 m)
REPORT_TWO = bytes.fromhex("f4f3f2f10413000f001700ffff2700f8f7f6f5")  # + (-0.1 m, 3.9 m)
QUERY_MODE = bytes.fromhex("fdfcfbfa0a0c000104030201")
QUERY_MOUNTING = bytes.fromhex("fdfcfbfa080c000104030201")
ACK_MODE_SIDE = bytes.fromhex("fdfcfbfa0a0c000104030201")
ACK_MOUNTING_260_25 = bytes.fromhex("fdfcfbfa080f000401c40904030201")  # 2.60 m, 25.00 deg
ACK_MOUNTING_240_30 = bytes.fromhex("fdfcfbfa080f00f000b80b04030201")  # 2.40 m, 30.00 deg
SET_MOUNTING_240_25 = bytes.fromhex("fdfcfbfa070f00f000c40904030201")
SET_MOUNTING_240_30 = bytes.fromhex("fdfcfbfa070f00f000b80b04030201")
ACK_SET_MOUNTING_OK = bytes.fromhex("fdfcfbfa070c000104030201")

YAML = """
esphome:
  name: ld2460-hosttest
  on_boot:
    # Once the module's height is known, somebody asks for 2.40 m and
    # 30 degrees, one right after the other — as Echolot does.
    then:
      - wait_until:
          condition:
            lambda: 'return id(height_a).has_state();'
      - delay: 1s
      - number.set:
          id: height_a
          value: 2.4
      - number.set:
          id: angle_a
          value: 30
host:
logger:
  level: DEBUG
uart:
  - id: uart_a
    port: {port_a}
    baud_rate: 115200
  - id: uart_b
    port: {port_b}
    baud_rate: 115200
external_components:
  - source:
      type: local
      path: {components}
    components: [echolot_ld2460]
echolot_ld2460:
  - id: radar_a
    uart_id: uart_a
    stale_after: 1s
    probe_interval: 2s
    frame:
      name: "Frame A"
      on_value:
        - logger.log: {{format: "FRAME_A %s", args: [x.c_str()]}}
    target_count:
      name: "Targets A"
      on_value:
        - logger.log: {{format: "COUNT_A %.0f", args: [x]}}
    radar_firmware:
      name: "Firmware A"
      on_value:
        - logger.log: {{format: "FIRMWARE_A %s", args: [x.c_str()]}}
    mount_mode:
      name: "Mode A"
      on_value:
        - logger.log: {{format: "MODE_A %s", args: [x.c_str()]}}
    mount_height:
      id: height_a
      name: "Height A"
      on_value:
        - logger.log: {{format: "HEIGHT_A %.2f", args: [x]}}
    mount_angle:
      id: angle_a
      name: "Angle A"
      on_value:
        - logger.log: {{format: "ANGLE_A %.2f", args: [x]}}
  - id: radar_b
    uart_id: uart_b
    stale_after: 1s
    probe_interval: 2s
    quiet_means_empty: true
    target_count:
      name: "Targets B"
      on_value:
        - logger.log: {{format: "COUNT_B %.0f", args: [x]}}
"""

#: What ESPHome's generated platformio.ini asks for on the host platform.
CXXFLAGS = [
    "-DESPHOME_LOG_LEVEL=ESPHOME_LOG_LEVEL_DEBUG",
    "-DUSE_HOST",
    "-Wno-sign-compare",
    "-Wno-unused-but-set-variable",
    "-Wno-unused-variable",
    "-fno-exceptions",
    "-std=gnu++20",
]


def _esphome():
    try:
        import esphome  # noqa: F401
    except ImportError:
        return None
    return shutil.which("esphome")


def _pty(tmp_path, name):
    master, slave = os.openpty()
    tty.setraw(master)
    tty.setraw(slave)
    # ESPHome's host UART only accepts a path of exactly two levels
    # (/dev/ttyX); a pseudo-terminal lives at /dev/pts/N.
    link = Path("/tmp") / f"echolot-{name}-{os.getpid()}-{tmp_path.name}"
    if link.is_symlink():
        link.unlink()
    link.symlink_to(os.ttyname(slave))
    return master, slave, link


class Firmware:
    def __init__(self, binary, masters):
        self.proc = subprocess.Popen(
            ["stdbuf", "-o0", "-e0", str(binary)] if shutil.which("stdbuf") else [str(binary)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        os.set_blocking(self.proc.stdout.fileno(), False)
        self.masters = masters
        self.lines: list[str] = []
        self.wire = {name: b"" for name in masters}
        self._pending = ""

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            readable, _, _ = select.select([self.proc.stdout, *self.masters.values()], [], [], 0.05)
            if self.proc.stdout in readable:
                chunk = self.proc.stdout.read() or b""
                text = self._pending + re.sub(r"\x1b\[[0-9;]*m", "", chunk.decode(errors="replace"))
                *complete, self._pending = text.split("\n")
                self.lines.extend(complete)
            for name, fd in self.masters.items():
                if fd in readable:
                    self.wire[name] += os.read(fd, 1024)

    def send(self, name, data):
        os.write(self.masters[name], data)

    def values(self, tag):
        """Every value logged under `tag`, in order."""
        found = []
        for line in self.lines:
            match = re.search(rf"\b{tag} (.*)$", line)
            if match:
                found.append(match.group(1).strip())
        return found

    def stop(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)


@pytest.fixture(scope="module")
def firmware(tmp_path_factory):
    esphome = _esphome()
    compiler = shutil.which("g++")
    if esphome is None or compiler is None:
        pytest.skip("ESPHome oder g++ fehlt")
    tmp = tmp_path_factory.mktemp("ld2460-host")
    master_a, _slave_a, port_a = _pty(tmp, "a")
    master_b, _slave_b, port_b = _pty(tmp, "b")
    (tmp / "hosttest.yaml").write_text(
        YAML.format(port_a=port_a, port_b=port_b, components=COMPONENTS), encoding="utf-8"
    )
    subprocess.run(
        [esphome, "compile", "--only-generate", "hosttest.yaml"],
        cwd=tmp, check=True, capture_output=True,
    )
    build = tmp / ".esphome" / "build" / "ld2460-hosttest"
    sources = sorted((build / "src").rglob("*.cpp"))
    objects = []
    jobs = []
    for source in sources:
        obj = build / "obj" / (source.relative_to(build).as_posix().replace("/", "_") + ".o")
        obj.parent.mkdir(parents=True, exist_ok=True)
        objects.append(obj)
        # Warnings are errors for our own sources: that is the code this
        # test exists for. ESPHome's headers come in as system headers for
        # those files, so their warnings — not ours to fix — stay quiet,
        # while our own headers, found next to the .cpp by the quoted
        # include, stay strict.
        if "echolot_ld2460" in source.as_posix():
            flags = ["-Wall", "-Wextra", "-Werror", f"-isystem{build / 'src'}"]
        else:
            flags = [f"-I{build / 'src'}"]
        jobs.append(subprocess.Popen(
            [compiler, "-c", *CXXFLAGS, *flags, "-o", str(obj), str(source)],
            stderr=subprocess.PIPE, text=True,
        ))
    failed = [(src, job.communicate()[1]) for src, job in zip(sources, jobs) if job.wait() != 0]
    assert not failed, "\n".join(f"{src}:\n{err}" for src, err in failed)
    binary = build / "hosttest"
    subprocess.run([compiler, "-o", str(binary), *map(str, objects), "-lpthread"], check=True)

    fw = Firmware(binary, {"a": master_a, "b": master_b})
    yield fw
    fw.stop()
    for link in (port_a, port_b):
        link.unlink(missing_ok=True)


def test_the_whole_conversation(firmware):
    """One sequence, because each step depends on the state the last one
    left behind — exactly as on a device."""
    fw = firmware

    # Nothing from the module yet: unknown, and asking.
    fw.pump(2.6)
    assert fw.values("FRAME_A")[:1] == ["1|U|0|"]
    assert fw.values("COUNT_A")[:1] == ["nan"]
    assert fw.wire["a"].startswith(PROBE)
    assert VERSION_QUERY in fw.wire["a"], "no version query after two seconds"
    # Probed again after probe_interval, not on every loop.
    assert 2 <= fw.wire["a"].count(PROBE) <= 3

    # It answers: reporting is on. Alive, with nothing to say.
    fw.send("a", ACK_REPORTING_ON)
    fw.send("b", ACK_REPORTING_ON)
    fw.pump(0.6)
    assert fw.values("FRAME_A")[-1] == "1|Q|0|"
    assert fw.values("COUNT_A")[-1] == "nan", "quiet must not read as empty unless configured"
    assert fw.values("COUNT_B")[-1] == "0", "quiet_means_empty did not make quiet read as empty"

    fw.send("a", ACK_VERSION)
    fw.pump(0.4)
    assert fw.values("FIRMWARE_A") == ["V1.2 (2025-06)"]

    # A report, then a second one with a negative X.
    fw.send("a", REPORT_ONE)
    fw.pump(0.4)
    assert "1|R|1|15,23" in fw.values("FRAME_A")
    assert fw.values("COUNT_A")[-1] == "1"
    fw.send("a", REPORT_TWO)
    fw.pump(0.4)
    assert fw.values("FRAME_A")[-1] == "1|R|2|15,23;-1,39"
    assert fw.values("COUNT_A")[-1] == "2"

    # The module goes quiet. Within the acknowledgement's validity the
    # link is quiet — without coordinates, since the last report is no
    # longer a measurement — and after it, unknown.
    probes_before = fw.wire["a"].count(PROBE)
    fw.pump(4.5)
    frames = fw.values("FRAME_A")
    after_reports = frames[frames.index("1|R|2|15,23;-1,39") + 1:]
    assert after_reports[0] == "1|Q|2|"
    assert after_reports[-1] == "1|U|2|"
    assert all(not f.startswith("1|R|") for f in after_reports)
    assert fw.values("COUNT_A")[-1] == "nan"
    assert fw.wire["a"].count(PROBE) > probes_before, "silence did not trigger a probe"


def test_the_add_on_can_read_every_line_the_component_wrote(firmware):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.radar_frame import parse_frame

    firmware.pump(1.2)  # at least one heartbeat, if this runs on its own
    lines = firmware.values("FRAME_A")
    assert lines
    for line in lines:
        parse_frame(line)


def test_the_mounting_is_read_from_the_module_and_only_its_answer_counts(firmware):
    """Runs after the conversation above. The mounting was asked for; the
    entities show nothing until the module answers, and a change shows
    only once the module reads it back."""
    fw = firmware
    assert QUERY_MODE in fw.wire["a"] and QUERY_MOUNTING in fw.wire["a"]
    # The version answer already named the mode.
    assert fw.values("MODE_A") == ["side"]
    assert fw.values("HEIGHT_A") == []

    fw.send("a", ACK_MODE_SIDE + ACK_MOUNTING_260_25)
    fw.pump(0.5)
    assert fw.values("HEIGHT_A") == ["2.60"] and fw.values("ANGLE_A") == ["25.00"]

    # Somebody asks for 2.40 m, then 30 degrees (on_boot above). The
    # first command carries the angle the module holds; the second the
    # new height as well — not the old one read back before. Nothing is
    # shown yet.
    before = len(fw.wire["a"])
    fw.pump(1.8)
    sent = fw.wire["a"][before:]
    assert SET_MOUNTING_240_25 in sent and SET_MOUNTING_240_30 in sent
    assert sent.index(SET_MOUNTING_240_25) < sent.index(SET_MOUNTING_240_30)
    assert fw.values("HEIGHT_A") == ["2.60"], "a value was shown before the module confirmed it"
    assert fw.values("ANGLE_A") == ["25.00"]

    # Accepted: the component reads back, and the new values are what it read.
    before = len(fw.wire["a"])
    fw.send("a", ACK_SET_MOUNTING_OK + ACK_SET_MOUNTING_OK)
    fw.pump(0.8)
    assert QUERY_MOUNTING in fw.wire["a"][before:]
    fw.send("a", ACK_MOUNTING_240_30)
    fw.pump(0.4)
    assert fw.values("HEIGHT_A")[-1] == "2.40" and fw.values("ANGLE_A")[-1] == "30.00"
