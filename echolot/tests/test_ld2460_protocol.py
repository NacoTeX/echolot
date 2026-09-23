"""The LD2460 protocol code the radar firmware runs, exercised on a PC.

ld2460_protocol.h is plain C++ for exactly this: the harness in
tests/firmware/ compiles it with the host compiler and these tests talk
to it line by line. What passes here is the code that goes onto the
device, not a Python re-implementation of it.

The parser cases are those of the wohnzimmer-radar prototype's on-device
self-test (partial header, junk, invalid length, damaged tail, five
targets with a negative X, every split point, back-to-back reports),
plus the second frame family this version reads: acknowledgements.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.radar_frame import QUIET, RECEIVING, UNKNOWN, parse_frame  # noqa: E402

HARNESS_SOURCE = Path(__file__).parent / "firmware" / "ld2460_harness.cpp"

# The manufacturer's worked example: one target at (1.5 m, 2.3 m).
EXAMPLE = "f4f3f2f1040f000f001700f8f7f6f5"
EMPTY = "f4f3f2f1040b00f8f7f6f5"


def _five_targets() -> str:
    body = "".join("f1ff1700" for _ in range(5))  # x = -15 dm, y = 23 dm
    return "f4f3f2f1041f00" + body + "f8f7f6f5"


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    compiler = shutil.which("g++") or shutil.which("c++")
    if compiler is None:
        pytest.skip("kein C++-Compiler vorhanden")
    binary = tmp_path_factory.mktemp("ld2460") / "harness"
    subprocess.run(
        [compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-o", str(binary), str(HARNESS_SOURCE)],
        check=True,
    )
    proc = subprocess.Popen([str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def ask(line: str) -> list[str]:
        proc.stdin.write(line + "\n")
        proc.stdin.flush()
        out = []
        while True:
            reply = proc.stdout.readline().rstrip("\n")
            out.append(reply)
            if not line.startswith("feed") or reply.startswith("end "):
                return out

    yield ask
    proc.stdin.close()
    proc.wait(timeout=5)


def feed(ask, hexbytes: str):
    """Events and the rejected counter for one chunk of bytes."""
    lines = ask(f"feed {hexbytes}")
    return lines[:-1], int(lines[-1].split("=")[1])


# --- reports ------------------------------------------------------------


def test_the_manufacturers_example_reads_as_one_target_at_1_5_by_2_3(harness):
    harness("reset")
    events, rejected = feed(harness, EXAMPLE)
    assert events == ["report 1 15,23"]
    assert rejected == 0


def test_an_empty_report_is_a_report_with_no_targets(harness):
    harness("reset")
    assert feed(harness, EMPTY)[0] == ["report 0 "]


def test_five_targets_and_a_negative_x(harness):
    harness("reset")
    events, _ = feed(harness, _five_targets())
    assert events == ["report 5 " + ";".join(["-15,23"] * 5)]


def test_noise_bad_length_and_a_damaged_tail_are_rejected_and_the_next_frame_still_reads(harness):
    harness("reset")
    # Partial header, junk, a header announcing 0xFFFF bytes.
    events, rejected = feed(harness, "f4f30" + "0f4f3f2f104ffff")
    assert events == [] and rejected == 1
    damaged = EXAMPLE[:-2] + "00"
    events, rejected = feed(harness, damaged)
    assert events == [] and rejected == 2
    events, _ = feed(harness, EXAMPLE)
    assert events == ["report 1 15,23"]


def test_every_split_point(harness):
    raw = EXAMPLE
    for split in range(0, len(raw) + 1, 2):
        harness("reset")
        first, _ = feed(harness, raw[:split])
        second, _ = feed(harness, raw[split:] + raw)
        assert first + second == ["report 1 15,23", "report 1 15,23"], split


def test_a_report_with_another_function_code_is_not_a_report(harness):
    harness("reset")
    other = "f4f3f2f1050f000f001700f8f7f6f5"
    events, rejected = feed(harness, other + EXAMPLE)
    assert events == ["report 1 15,23"]
    assert rejected == 1


def test_a_length_for_six_targets_is_refused(harness):
    """The manual promises at most five. A sixth would need 35 bytes and
    is far more likely a corrupted length field than a sixth person."""
    harness("reset")
    six = "f4f3f2f1042300" + "0f001700" * 6 + "f8f7f6f5"
    events, rejected = feed(harness, six + EXAMPLE)
    assert events == ["report 1 15,23"]
    assert rejected >= 1


# --- acknowledgements ---------------------------------------------------


def test_the_reporting_acknowledgement_is_read(harness):
    harness("reset")
    ack = "fdfcfbfa060c0011" + "04030201"
    events, rejected = feed(harness, ack)
    assert events == ["ack 06 11"]
    assert rejected == 0


def test_the_version_acknowledgement_is_read_between_reports(harness):
    harness("reset")
    version = "fdfcfbfa0b1000" + "0119060102" + "04030201"
    events, _ = feed(harness, EXAMPLE + version + EMPTY)
    assert events == ["report 1 15,23", "ack 0b 0119060102", "report 0 "]


def test_an_acknowledgement_with_an_absurd_length_is_refused(harness):
    harness("reset")
    events, rejected = feed(harness, "fdfcfbfa06ff00" + EXAMPLE)
    assert events == ["report 1 15,23"]
    assert rejected == 1


def test_a_report_tail_does_not_close_an_acknowledgement(harness):
    """The two families have different tails. Accepting either would let
    a byte-shifted report pass for an acknowledgement."""
    harness("reset")
    events, rejected = feed(harness, "fdfcfbfa060c0011f8f7f6f5" + EXAMPLE)
    assert events == ["report 1 15,23"]
    assert rejected == 1


# --- the line Echolot reads ---------------------------------------------


@pytest.mark.parametrize(
    "command, expected",
    [
        ("encode R 7 15,23;-1,39", "1|R|7|15,23;-1,39"),
        ("encode R 0 -", "1|R|0|"),
        ("encode R 4294967295 -32768,32767", "1|R|4294967295|-32768,32767"),
        # Coordinates of a report that is no longer current are not sent.
        ("encode Q 9 15,23", "1|Q|9|"),
        ("encode U 9 15,23", "1|U|9|"),
    ],
)
def test_encoding(harness, command, expected):
    assert harness(command) == [expected]


def test_what_the_firmware_writes_is_what_the_add_on_reads(harness):
    [line] = harness("encode R 12 15,23;-15,-7;0,0")
    frame = parse_frame(line)
    assert frame.state == RECEIVING
    assert frame.seq == 12
    assert frame.targets_dm == ((15, 23), (-15, -7), (0, 0))
    assert frame.targets_m == ((1.5, 2.3), (-1.5, -0.7), (0.0, 0.0))
    assert frame.count == 3

    [line] = harness("encode Q 12 15,23")
    assert parse_frame(line).state == QUIET
    assert parse_frame(line).count is None

    [line] = harness("encode U 0 -")
    assert parse_frame(line).state == UNKNOWN


def test_the_longest_line_fits(harness):
    targets = ";".join(["-32768,-32768"] * 5)
    [line] = harness(f"encode R 4294967295 {targets}")
    assert line != "<empty>"
    assert len(parse_frame(line).targets_dm) == 5


# --- what silence means -------------------------------------------------


@pytest.mark.parametrize(
    "args, expected",
    [
        # have_report last_report have_ack last_ack enabled now stale ack_valid
        ("0 0 0 0 0 100 3000 8000", "U"),        # nothing yet
        ("1 1000 0 0 0 3999 3000 8000", "R"),    # report 2999 ms ago
        ("1 1000 0 0 0 4000 3000 8000", "U"),    # 3000 ms: stale, no ack
        ("1 1000 1 3000 1 4000 3000 8000", "Q"),  # stale, but answered probe
        ("1 1000 1 3000 0 4000 3000 8000", "U"),  # answered, reporting off
        ("1 1000 1 3000 1 11000 3000 8000", "U"),  # the answer is too old
        ("1 4294967000 0 0 0 200 3000 8000", "R"),  # across the millis() wrap
    ],
)
def test_classify(harness, args, expected):
    assert harness(f"classify {args}") == [expected]


# --- the reader refuses what it does not know ---------------------------


@pytest.mark.parametrize(
    "line",
    [
        "",
        "2|R|1|",                  # a newer format
        "1|X|1|",                  # unknown state
        "1|R|-1|",                 # negative sequence
        "1|R|4294967296|",         # beyond uint32
        "1|R|1|15",                # not a pair
        "1|R|1|15,23,4",
        "1|R|1|1,1;2,2;3,3;4,4;5,5;6,6",
        "1|R|1|40000,0",           # beyond int16
        "1|Q|1|15,23",             # coordinates without a current report
        "1|R|1|15,23|",            # extra field
        "1|R|１|",                 # non-ASCII digit
    ],
)
def test_malformed_lines_are_refused(line):
    with pytest.raises(ValueError):
        parse_frame(line)
