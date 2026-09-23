"""The line an Echolot radar publishes per report, read back.

The device side is `encode_frame()` in
app/esphome_components/echolot_ld2460/ld2460_protocol.h; the two are held
together by tests/test_ld2460_protocol.py, which runs the C++ encoder
and feeds its output through this parser.

Format version 1::

    1|<state>|<seq>|<targets>

Everything that does not match is refused rather than repaired. A line
from a newer firmware is not "probably fine" — it is a format this add-on
does not know, and reading positions out of it would be guessing.
"""

from dataclasses import dataclass

RECEIVING = "receiving"
QUIET = "quiet"
UNKNOWN = "unknown"

_STATES = {"R": RECEIVING, "Q": QUIET, "U": UNKNOWN}

FORMAT_VERSION = "1"
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


def _int(text: str, low: int, high: int, what: str) -> int:
    body = text[1:] if text.startswith("-") else text
    if not body.isdigit() or not body.isascii():
        raise ValueError(f"{what} ist keine ganze Zahl: {text!r}")
    value = int(text)
    if not low <= value <= high:
        raise ValueError(f"{what} liegt außerhalb von {low}…{high}: {value}")
    return value


def parse_frame(text: str) -> RadarFrame:
    """Parse one frame line; raises ValueError for anything off-format."""
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
    return RadarFrame(state=state, seq=seq, targets_dm=tuple(targets))
