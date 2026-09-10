#!/usr/bin/env python3
"""Turn a peer-link capture into the one verdict Stufe 2 is about.

Reads what an ESP32 running the `csi_recv` experiment prints on its
serial console and answers three questions:

  * Was there a directed A→B link at all, and how good was it — frames a
    second, sequence loss, signal range.
  * **Did it die when the sender was switched off, while the router kept
    sending?** That is the experiment the whole peer mode rests on. A
    presence signal that survives its own transmitter being unplugged is
    measuring the access point, which is what Echolot already does.
  * If it cannot tell, which of the several reasons applies.

Run from anywhere:

    python echolot/tools/peer_link_report.py capture.log
    cat /dev/ttyACM0 | python echolot/tools/peer_link_report.py -

See experiments/peer-link/README.md for how to record one, including
the receiver patch that makes the shutdown test falsifiable at all.

**Why a patch is needed.** Espressif's example drops every frame whose
transmitter is not the peer, so its capture holds peer frames and nothing
else. Silence at the end of such a file means the link died — or the
board rebooted, or the cable came loose, and all three look identical. So
the receiver is asked for one `CSI_STAT` line a second carrying how many
peer and non-peer frames it saw, and silence only counts as a dead link
when that line kept coming and kept counting somebody else's traffic.

Line formats, both printed by the receiver:

    CSI_DATA,<seq>,<mac>,<rssi>,<rate>,<noise_floor>,<fft_gain>,
             <agc_gain>,<channel>,<local_timestamp_us>,<sig_len>,
             <rx_format>,<len>,<first_word>,"[<csi ...>]"
    CSI_STAT,<uptime_ms>,<peer_frames>,<other_frames>

The CSI_DATA layout is Espressif's own for the ESP32-C5/C6 — see
esp-csi/examples/get-started. Only the first ten fields are read here;
the subcarrier array is left alone, because nothing in this verdict
depends on it.
"""

import argparse
import statistics
import sys
from dataclasses import dataclass, field

#: How long the peer has to stay silent before it counts as switched off
#: rather than as a scheduling hiccup. Each CSI_STAT line covers one
#: second, so this is a number of lines.
DEFAULT_QUIET_SECONDS = 5

#: The MAC Espressif's example compiles into both binaries. Overridable,
#: and worth overriding — see the runbook.
DEFAULT_PEER_MAC = "1a:00:00:00:00:00"

#: The four things a capture can say about the shutdown test.
LINK_DIED = "link_died"
NEVER_QUIET = "never_quiet"
TOO_SHORT = "too_short"
NO_CONTROL = "no_control"
NO_LIVENESS = "no_liveness"


@dataclass(frozen=True)
class Frame:
    """One CSI callback the receiver decided to print."""

    seq: int
    mac: str
    rssi: int
    at: float  # seconds on the receiver's own clock


@dataclass(frozen=True)
class Stat:
    """One second of "I am alive, and this is what I heard"."""

    at: float
    peer: int
    other: int


@dataclass
class Capture:
    frames: list = field(default_factory=list)
    stats: list = field(default_factory=list)
    #: Lines that were not one of the two formats. Counted rather than
    #: dropped silently: a capture that is 90 % boot log and 10 % data
    #: should be visible as such.
    skipped: int = 0

    @property
    def duration_seconds(self) -> float:
        """First to last event, across both streams."""
        moments = [f.at for f in self.frames] + [s.at for s in self.stats]
        if not moments:
            return 0.0
        return round(max(moments) - min(moments), 3)

    @property
    def senders(self) -> dict:
        """Every transmitter address seen, and how often.

        More than one means the capture was taken without the firmware's
        own filter, or with the wrong address compiled into it. Either
        way the reader should know before believing a frame count.
        """
        counts: dict = {}
        for frame in self.frames:
            counts[frame.mac] = counts.get(frame.mac, 0) + 1
        return counts

    def peer_frames(self, peer: str) -> int:
        return sum(1 for frame in self.frames if frame.mac == peer)


def read_capture(lines) -> Capture:
    """Parse a serial capture, skipping everything that is not data.

    Never raises. A capture starts mid-line, carries boot noise and log
    output, and ends wherever the operator pressed Ctrl-C — so a strict
    reader would refuse most real recordings.
    """
    capture = Capture()
    for line in lines:
        text = line.strip()
        if text.startswith("CSI_DATA,"):
            frame = _parse_frame(text)
            if frame is None:
                capture.skipped += 1
            else:
                capture.frames.append(frame)
        elif text.startswith("CSI_STAT,"):
            stat = _parse_stat(text)
            if stat is None:
                capture.skipped += 1
            else:
                capture.stats.append(stat)
        elif text:
            # Blank lines are not noise worth counting — a serial capture
            # is full of them, and inflating the number would hide the
            # thing it is for: how much of the file was unparsable data.
            capture.skipped += 1
    return capture


def _parse_frame(text: str):
    parts = text.split(",")
    if len(parts) < 10:
        return None
    try:
        return Frame(
            seq=int(parts[1]),
            mac=parts[2].strip().lower(),
            rssi=int(parts[3]),
            # Microseconds on the receiver's clock. Not wall time and not
            # comparable between two boards — which is fine, because
            # every question here is about one receiver's own timeline.
            at=int(parts[9]) / 1_000_000,
        )
    except (TypeError, ValueError):
        return None


def _parse_stat(text: str):
    parts = text.split(",")
    if len(parts) < 4:
        return None
    try:
        return Stat(at=int(parts[1]) / 1000, peer=int(parts[2]), other=int(parts[3]))
    except (TypeError, ValueError):
        return None


# --- what the link looked like while it was up ---------------------------


@dataclass(frozen=True)
class LinkQuality:
    received: int
    sent: int
    #: Seconds in which the peer was heard at all — not the length of
    #: the recording, which on a shutdown run is deliberately longer.
    seconds_observed: float
    frames_per_second: float
    loss_ratio: float | None
    restarts: int
    rssi_min: int | None
    rssi_max: int | None
    rssi_median: float | None


def link_quality(capture: Capture, *, peer: str = DEFAULT_PEER_MAC) -> LinkQuality:
    """How well the link carried, measured over time it was observed.

    Observed time comes from the liveness lines rather than from the
    frames: one a second, and they are the only evidence the receiver was
    listening at all. Dividing by the span between the first and last
    frame would turn a capture that stopped early into a flattering rate
    — the same mistake the presence window made before 0.13.5.

    And only the seconds the link was *up*. A shutdown capture contains
    half a minute of deliberate silence; counting it would answer "frames
    per second of recording", which is a fact about the experiment rather
    than about the link. The first synthetic run through this tool
    reported 73.6/s for a link that carried 98.
    """
    frames = [f for f in capture.frames if f.mac == peer]
    seconds = float(sum(1 for stat in capture.stats if stat.peer > 0))
    sent, restarts = _expected_frames([f.seq for f in frames])
    rssis = [f.rssi for f in frames]
    return LinkQuality(
        received=len(frames),
        sent=sent,
        seconds_observed=seconds,
        frames_per_second=round(len(frames) / seconds, 2) if seconds else 0.0,
        loss_ratio=(max(0.0, (sent - len(frames)) / sent) if sent else None),
        restarts=restarts,
        rssi_min=min(rssis) if rssis else None,
        rssi_max=max(rssis) if rssis else None,
        rssi_median=statistics.median(rssis) if rssis else None,
    )


def _expected_frames(sequence: list) -> tuple:
    """How many frames the sender emitted, and how often it restarted.

    The sender numbers every frame it sends, so the holes are the link's
    loss — invisible if you only count what arrived. A counter that goes
    backwards is a reset, not a billion lost frames, so the span is taken
    per run rather than from the lowest to the highest number ever seen.
    """
    if not sequence:
        return 0, 0
    total = 0
    restarts = 0
    start = previous = sequence[0]
    for value in sequence[1:]:
        if value < previous:
            total += previous - start + 1
            restarts += 1
            start = value
        previous = value
    return total + previous - start + 1, restarts


# --- the shutdown test ---------------------------------------------------


@dataclass(frozen=True)
class ShutdownVerdict:
    outcome: str
    reason: str
    quiet_seconds: int
    #: Whether peer frames came back after the silence. A link that
    #: returns rules out the receiver having simply died, which is the
    #: strongest form of this experiment.
    recovered: bool


def shutdown_verdict(
    capture: Capture,
    *,
    peer: str = DEFAULT_PEER_MAC,
    quiet_seconds: int = DEFAULT_QUIET_SECONDS,
) -> ShutdownVerdict:
    """Did the link die when its sender did, with the router still up?

    Judged from the liveness lines, not from the absence of frames: the
    absence of frames is what a crashed receiver produces too. The four
    ways this can come out are all reported, because "inconclusive" and
    "failed" call for different repairs.
    """
    if not capture.stats:
        return ShutdownVerdict(
            NO_LIVENESS,
            "Keine CSI_STAT-Zeile in der Aufnahme. Ohne sie ist Stille nicht "
            "von einem abgestürzten Empfänger zu unterscheiden — siehe den "
            "Empfänger-Patch in experiments/peer-link/.",
            0,
            False,
        )

    runs = _quiet_runs(capture.stats)
    if not runs:
        return ShutdownVerdict(
            NEVER_QUIET,
            "Der Peer hat in keiner einzigen Sekunde aufgehört zu senden. "
            "Entweder war der Sender nie aus, oder gemessen wurde etwas "
            "anderes als der Sender.",
            0,
            False,
        )

    longest = max(runs, key=len)
    length = len(longest)
    recovered = capture.stats.index(longest[-1]) < len(capture.stats) - 1 and any(
        stat.peer > 0 for stat in capture.stats[capture.stats.index(longest[-1]) + 1:]
    )

    if length < quiet_seconds:
        return ShutdownVerdict(
            TOO_SHORT,
            f"Längste Stille: {length} s, verlangt sind {quiet_seconds} s. "
            "So kurz ist das ein Aussetzer und kein abgeschalteter Sender.",
            length,
            recovered,
        )

    heard_others = sum(stat.other for stat in longest)
    if heard_others == 0:
        return ShutdownVerdict(
            NO_CONTROL,
            f"{length} s ohne Peer-Frames — aber der Empfänger hat in dieser "
            "Zeit auch sonst nichts gehört. Ein toter Link und ein totes "
            "Funkmodul sehen von hier gleich aus. Der Router ist die "
            "Kontrolle und muss während des Versuchs weiter Verkehr machen.",
            length,
            recovered,
        )

    return ShutdownVerdict(
        LINK_DIED,
        f"{length} s ohne ein einziges Peer-Frame, während der Empfänger "
        f"weiterlief und in derselben Zeit {heard_others} andere Frames hörte."
        + (
            " Danach kam der Link zurück — womit auch ein zwischenzeitlich "
            "abgestürzter Empfänger ausgeschlossen ist."
            if recovered
            else " Der Link kam nicht zurück; ein Wiedereinschalten des "
            "Senders im selben Durchgang wäre der stärkere Versuch."
        ),
        length,
        recovered,
    )


def _quiet_runs(stats: list) -> list:
    """Every contiguous stretch of seconds with no peer frame."""
    runs: list = []
    current: list = []
    for stat in stats:
        if stat.peer == 0:
            current.append(stat)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


# --- the command line ----------------------------------------------------


def render(capture: Capture, peer: str, quiet_seconds: int) -> str:
    quality = link_quality(capture, peer=peer)
    verdict = shutdown_verdict(capture, peer=peer, quiet_seconds=quiet_seconds)

    labels = {
        LINK_DIED: "BESTANDEN — der Link hängt am Sender",
        NEVER_QUIET: "NICHT DURCHGEFÜHRT",
        TOO_SHORT: "NICHT DURCHGEFÜHRT",
        NO_CONTROL: "UNENTSCHIEDEN",
        NO_LIVENESS: "NICHT AUSWERTBAR",
    }

    lines = [
        f"Aufnahme: {capture.duration_seconds:.1f} s, "
        f"{len(capture.frames)} CSI-Zeilen, {len(capture.stats)} Statuszeilen, "
        f"{capture.skipped} übersprungen",
        "",
        f"Peer {peer}",
        f"  empfangen        {quality.received}",
        f"  gesendet (Seq.)  {quality.sent}",
        f"  Link oben        {quality.seconds_observed:.0f} s von "
        f"{len(capture.stats)} s Aufnahme",
        f"  Rate             {quality.frames_per_second} Frames/s",
    ]
    if quality.loss_ratio is not None:
        lines.append(f"  Verlust          {quality.loss_ratio * 100:.1f} %")
    if quality.restarts:
        lines.append(f"  Sender-Neustarts {quality.restarts}")
    if quality.rssi_median is not None:
        lines.append(
            f"  RSSI             {quality.rssi_min} … {quality.rssi_max} dBm "
            f"(Median {quality.rssi_median})"
        )

    others = {mac: n for mac, n in capture.senders.items() if mac != peer}
    if others:
        lines += [
            "",
            "Fremde Sender in derselben Aufnahme — der Firmware-Filter war aus "
            "oder trägt die falsche Adresse:",
        ]
        lines += [f"  {mac}: {count}" for mac, count in sorted(others.items())]

    lines += [
        "",
        f"Abschalttest: {labels[verdict.outcome]}",
        f"  {verdict.reason}",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("capture", help="Datei mit der Aufnahme, oder - für stdin")
    parser.add_argument("--peer", default=DEFAULT_PEER_MAC,
                        help=f"MAC des Senders (Vorgabe {DEFAULT_PEER_MAC})")
    parser.add_argument("--quiet-seconds", type=int, default=DEFAULT_QUIET_SECONDS,
                        help="Wie lange der Peer schweigen muss, damit es zählt")
    args = parser.parse_args(argv)

    if args.capture == "-":
        capture = read_capture(sys.stdin)
    else:
        with open(args.capture, encoding="utf-8", errors="replace") as handle:
            capture = read_capture(handle)

    print(render(capture, args.peer.lower(), args.quiet_seconds))
    verdict = shutdown_verdict(capture, peer=args.peer.lower(),
                               quiet_seconds=args.quiet_seconds)
    return 0 if verdict.outcome == LINK_DIED else 1


if __name__ == "__main__":
    sys.exit(main())
