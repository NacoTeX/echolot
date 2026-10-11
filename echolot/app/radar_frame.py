"""The line an Echolot radar publishes per report, read back.

The device side is `encode_frame()` in
app/esphome_components/echolot_ld2460/ld2460_protocol.h; the two are held
together by tests/test_ld2460_protocol.py, which runs the C++ encoder
and feeds its output through this parser.

Format version 1 (before Echolot 3.0)::

    1|<state>|<seq>|<targets>

Format version 2 (3.0): every report since the last line, each with the
device time (milliseconds since boot) it arrived at the ESP::

    2|<state>|<seq>@<ms>:<targets>[/<seq>@<ms>:<targets>...]

`parse_line` reads both, into one frame per report.

Everything that does not match is refused rather than repaired. A line
from a newer firmware is not "probably fine" — it is a format this add-on
does not know, and reading positions out of it would be guessing.
"""

from dataclasses import dataclass

RECEIVING = "receiving"
QUIET = "quiet"
UNKNOWN = "unknown"

_STATES = {"R": RECEIVING, "Q": QUIET, "U": UNKNOWN}
_LETTERS = {state: letter for letter, state in _STATES.items()}

FORMAT_VERSION = "1"
FORMAT_VERSION_2 = "2"
_MS_MAX = 2**32 - 1
MAX_TARGETS = 5
_SEQ_MAX = 2**32 - 1
_COORD_MIN, _COORD_MAX = -32768, 32767


@dataclass(frozen=True)
class RadarFrame:
    """One report as the device described it.

    `targets_dm` are the module's own numbers: decimetres, in the sensor's
    frame of reference, with the sign convention of its X axis not yet
    checked against a real room. Turning them into room coordinates is the
    room model's job, not this one's.
    """

    state: str
    seq: int
    targets_dm: tuple[tuple[int, int], ...]
    #: Format 2: the device's clock when the report arrived, ms since boot
    #: (wraps after 49 days). None from format 1.
    device_ms: int | None = None

    @property
    def targets_m(self) -> tuple[tuple[float, float], ...]:
        return tuple((x / 10, y / 10) for x, y in self.targets_dm)

    @property
    def count(self) -> int | None:
        """Targets in this report, or None when there is no current report.

        None rather than 0 on purpose: a device that cannot see is not a
        device that sees nobody.
        """
        return len(self.targets_dm) if self.state == RECEIVING else None

    def as_line(self) -> str:
        """The line this frame is read from, as the firmware writes it:
        parse_frame(frame.as_line()) == frame."""
        targets = ";".join(f"{x},{y}" for x, y in self.targets_dm)
        if self.device_ms is not None:
            return f"{FORMAT_VERSION_2}|{_LETTERS[self.state]}|{self.seq}@{self.device_ms}:{targets}"
        return f"{FORMAT_VERSION}|{_LETTERS[self.state]}|{self.seq}|{targets}"


def _int(text: str, low: int, high: int, what: str) -> int:
    body = text[1:] if text.startswith("-") else text
    if not body.isdigit() or not body.isascii():
        raise ValueError(f"{what} ist keine ganze Zahl: {text!r}")
    value = int(text)
    if not low <= value <= high:
        raise ValueError(f"{what} liegt außerhalb von {low}…{high}: {value}")
    return value


def parse_line(text: str) -> list[RadarFrame]:
    """One line of either format, as its reports, oldest first; raises
    ValueError for anything off-format."""
    if not isinstance(text, str):
        raise ValueError("Radar-Frame ist kein Text")
    if not text.startswith(FORMAT_VERSION_2 + "|"):
        return [parse_frame(text)]
    parts = text.split("|")
    if len(parts) != 3:
        raise ValueError(f"Radar-Frame hat {len(parts)} statt 3 Felder")
    state = _STATES.get(parts[1])
    if state is None:
        raise ValueError(f"Unbekannter Radarzustand {parts[1]!r}")
    frames = []
    for item in parts[2].split("/"):
        head, colon, targets_text = item.partition(":")
        seq_text, at, ms_text = head.partition("@")
        if not colon or not at:
            raise ValueError(f"Meldung {item!r} ist nicht <Nummer>@<ms>:<Ziele>")
        if seq_text.startswith("-") or ms_text.startswith("-"):
            raise ValueError("Nummer und Zeit dürfen nicht negativ sein")
        frames.append(RadarFrame(
            state=state,
            seq=_int(seq_text, 0, _SEQ_MAX, "Laufende Nummer"),
            targets_dm=_targets(targets_text, state),
            device_ms=_int(ms_text, 0, _MS_MAX, "Gerätezeit"),
        ))
    if state != RECEIVING and len(frames) != 1:
        raise ValueError("Ohne aktuelle Radarmeldung gibt es nur eine Meldung")
    for older, newer in zip(frames, frames[1:]):
        if not 0 < (newer.seq - older.seq) % 2**32 < 2**31:
            raise ValueError("Meldungen einer Zeile nicht in aufsteigender Folge")
    return frames


def _targets(targets_text: str, state: str) -> tuple[tuple[int, int], ...]:
    targets: list[tuple[int, int]] = []
    if targets_text:
        if state != RECEIVING:
            # The firmware never sends coordinates without a current
            # report. If a line claims otherwise, the line is wrong.
            raise ValueError("Koordinaten ohne aktuelle Radarmeldung")
        pairs = targets_text.split(";")
        if len(pairs) > MAX_TARGETS:
            raise ValueError(f"Mehr als {MAX_TARGETS} Ziele")
        for pair in pairs:
            coords = pair.split(",")
            if len(coords) != 2:
                raise ValueError(f"Ziel {pair!r} ist kein x,y-Paar")
            targets.append((
                _int(coords[0], _COORD_MIN, _COORD_MAX, "X"),
                _int(coords[1], _COORD_MIN, _COORD_MAX, "Y"),
            ))
    return tuple(targets)


def parse_frame(text: str) -> RadarFrame:
    """Parse one format-1 frame line; raises ValueError for anything
    off-format. (Lines of both formats: parse_line.)"""
    if not isinstance(text, str):
        raise ValueError("Radar-Frame ist kein Text")
    parts = text.split("|")
    if len(parts) != 4:
        raise ValueError(f"Radar-Frame hat {len(parts)} statt 4 Felder")
    version, letter, seq_text, targets_text = parts
    if version != FORMAT_VERSION:
        raise ValueError(f"Unbekanntes Radar-Frame-Format {version!r}")
    state = _STATES.get(letter)
    if state is None:
        raise ValueError(f"Unbekannter Radarzustand {letter!r}")
    if seq_text.startswith("-"):
        raise ValueError("Laufende Nummer darf nicht negativ sein")
    seq = _int(seq_text, 0, _SEQ_MAX, "Laufende Nummer")

    return RadarFrame(state=state, seq=seq, targets_dm=_targets(targets_text, state))
