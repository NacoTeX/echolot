"""The one experiment the peer mode rests on, made falsifiable.

Stufe 2 of the peer-mode work order asks for a directed A→B radio link
and one decisive test of it: switch the sender off, and the link must
fail *even while the router keeps sending*. Without that, "the numbers
move when somebody walks past" proves nothing — the access point's own
traffic moves them too, and that is what Echolot already measures.

The trap this module exists for: Espressif's `csi_recv` example drops
every frame whose transmitter is not the peer, so a capture contains
peer frames and nothing else. Silence at the end of such a capture is
therefore ambiguous — the link died, or the receiver crashed, or the USB
cable came loose, and the file looks identical in all three cases. A
test whose failure mode is indistinguishable from its success mode is
not a test.

So the receiver is asked for one extra line a second (`CSI_STAT`,
carrying how many peer and non-peer frames it saw), and the verdict here
is four-way rather than pass/fail: silence only counts as a dead link
when the receiver was demonstrably still alive and still hearing
somebody else.
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import peer_link_report as report  # noqa: E402

PEER = "1a:00:00:00:00:00"
STRANGER = "aa:bb:cc:dd:ee:ff"


def csi(seq, *, at, mac=PEER, rssi=-30):
    """One CSI_DATA row in the ESP32-C5/C6 layout the firmware prints.

    `at` is seconds; the firmware stamps microseconds from its own clock.
    """
    return (
        f"CSI_DATA,{seq},{mac},{rssi},11,-96,32,4,11,{int(at * 1_000_000)},"
        f'47,0,128,0,"[1,2,3,4]"'
    )


def stat(at, *, peer, other):
    return f"CSI_STAT,{int(at * 1000)},{peer},{other}"


def capture(lines):
    return report.read_capture(lines)


def running(seconds, *, rate=100, first_seq=0, mac=PEER, other=40):
    """A stretch with the peer sending, and the room otherwise normal."""
    lines = []
    for second in range(seconds):
        for tick in range(rate):
            lines.append(
                csi(first_seq + second * rate + tick, at=second + tick / rate, mac=mac)
            )
        lines.append(stat(second + 0.999, peer=rate, other=other))
    return lines


def silent(seconds, *, start, other=40):
    """The peer gone, the receiver alive, the room still talking."""
    return [stat(start + second + 0.999, peer=0, other=other) for second in range(seconds)]


# --- reading a capture ---------------------------------------------------


def test_a_capture_keeps_peer_frames_and_the_liveness_line_apart():
    reading = capture(running(3) + silent(3, start=3))

    assert len(reading.frames) == 300
    assert len(reading.stats) == 6
    # First frame at 0.0, last status line at 5.999 — the honest span,
    # not a rounded-up count of seconds.
    assert reading.duration_seconds == 5.999


def test_a_frame_from_somebody_else_is_not_a_peer_frame():
    """Defence in depth. The firmware filters by transmitter address, but
    a capture taken with that filter off, or with the wrong MAC compiled
    in, must not quietly become evidence for a link."""
    reading = capture([csi(1, at=0.0), csi(2, at=0.01, mac=STRANGER)])

    assert reading.peer_frames(PEER) == 1
    assert reading.senders == {PEER: 1, STRANGER: 1}


def test_a_capture_that_saw_two_senders_says_so():
    reading = capture(running(2) + [csi(9, at=0.5, mac=STRANGER)])

    assert len(reading.senders) == 2


def test_junk_lines_are_skipped_rather_than_fatal():
    """A serial capture starts mid-line, carries boot noise and log
    output, and ends wherever the operator pressed Ctrl-C."""
    lines = [
        "ets Jul 29 2019 12:21:46",
        "I (532) csi_recv: ================ CSI RECV ================",
        "type,seq,mac,rssi,rate,noise_floor,fft_gain,agc_gain,channel",
        "CSI_DATA,7,1a:00:00:00:00:00,-23,11",  # truncated
        csi(8, at=0.0),
        'CSI_DATA,nine,1a:00:00:00:00:00,-23,11,-96,32,4,11,0,47,0,128,0,"[1]"',
        "",
    ]
    reading = capture(lines)

    assert len(reading.frames) == 1
    assert reading.skipped == 5


# --- the shutdown test ---------------------------------------------------


def test_the_sender_going_quiet_while_the_room_does_not_is_a_dead_link():
    """The result the whole stage is for."""
    verdict = report.shutdown_verdict(
        capture(running(10) + silent(10, start=10)), peer=PEER
    )

    assert verdict.outcome == report.LINK_DIED
    assert verdict.quiet_seconds == 10
    assert "10" in verdict.reason


def test_a_peer_that_never_stopped_means_the_test_was_not_performed():
    """Not a failure of the link — a failure to run the experiment. Said
    differently, because the fix is different."""
    verdict = report.shutdown_verdict(capture(running(20)), peer=PEER)

    assert verdict.outcome == report.NEVER_QUIET


def test_a_receiver_that_stopped_reporting_proves_nothing():
    """Peer frames end and the liveness line ends with them: the link may
    have died, or the board rebooted, or the cable came out. Reporting
    this as a pass is exactly the mistake this module exists to prevent."""
    verdict = report.shutdown_verdict(capture(running(10)), peer=PEER)

    assert verdict.outcome == report.NEVER_QUIET

    # And with a real capture that simply stops: the last stat line is at
    # the same moment as the last frame, so there is no evidence of life
    # after the peer went silent.
    cut = capture(running(10) + [stat(10.5, peer=0, other=40)])
    assert report.shutdown_verdict(cut, peer=PEER).outcome == report.TOO_SHORT


def test_a_radio_that_heard_nothing_at_all_is_inconclusive():
    """The receiver kept reporting, but its `other` count was zero too.
    A dead link and a dead radio look the same from here, and the router
    was supposed to be the control."""
    verdict = report.shutdown_verdict(
        capture(running(10) + silent(10, start=10, other=0)), peer=PEER
    )

    assert verdict.outcome == report.NO_CONTROL
    assert "Router" in verdict.reason or "Verkehr" in verdict.reason


def test_the_quiet_stretch_has_to_be_long_enough_to_mean_anything():
    """Two seconds of silence is a scheduling hiccup, not a switched-off
    transmitter."""
    verdict = report.shutdown_verdict(
        capture(running(10) + silent(2, start=10)), peer=PEER, quiet_seconds=5
    )

    assert verdict.outcome == report.TOO_SHORT


def test_silence_in_the_middle_counts_as_well_as_silence_at_the_end():
    """Switch off, wait, switch on again — a stronger experiment than
    switching off and stopping the capture, because the link coming back
    rules out a receiver that simply died."""
    reading = capture(running(10) + silent(8, start=10) + running(5, first_seq=5000))
    verdict = report.shutdown_verdict(reading, peer=PEER)

    assert verdict.outcome == report.LINK_DIED
    assert verdict.recovered is True


def test_a_link_that_never_came_back_is_still_a_dead_link_but_says_so():
    verdict = report.shutdown_verdict(
        capture(running(10) + silent(10, start=10)), peer=PEER
    )

    assert verdict.outcome == report.LINK_DIED
    assert verdict.recovered is False


def test_a_capture_without_any_liveness_line_cannot_be_judged():
    """An unpatched receiver produces exactly this. The tool has to say
    so rather than guess, or the patch is optional and the evidence is
    worthless."""
    verdict = report.shutdown_verdict(capture([csi(i, at=i / 100) for i in range(500)]),
                                      peer=PEER)

    assert verdict.outcome == report.NO_LIVENESS
    assert "CSI_STAT" in verdict.reason


# --- what the link looked like while it was up ---------------------------


def test_the_frame_rate_is_measured_over_observed_time():
    """Same rule as the presence rate: events per second of time that
    something was actually listening."""
    stats = report.link_quality(capture(running(10)), peer=PEER)

    assert stats.frames_per_second == 100.0
    assert stats.seconds_observed == 10.0


def test_a_gap_in_the_sequence_is_loss_not_a_shorter_recording():
    """The sender numbers every frame it emits. A hole means the frame
    was sent and not received — which is the link's quality, and is
    invisible if you only count what arrived."""
    lines = [csi(seq, at=seq / 100) for seq in range(100) if seq % 10]
    lines.append(stat(0.999, peer=90, other=40))
    stats = report.link_quality(capture(lines), peer=PEER)

    assert stats.sent == 99  # first to last, inclusive
    assert stats.received == 90
    assert round(stats.loss_ratio, 3) == round(9 / 99, 3)


def test_a_counter_that_restarts_is_a_reboot_not_a_billion_lost_frames():
    """The sender counts from zero after a reset. Subtracting the old
    high water mark would report the whole range as loss."""
    lines = [csi(seq, at=seq / 100) for seq in range(500, 600)]
    lines += [csi(seq, at=6 + seq / 100) for seq in range(0, 100)]
    lines.append(stat(0.999, peer=100, other=40))
    stats = report.link_quality(capture(lines), peer=PEER)

    assert stats.restarts == 1
    assert stats.loss_ratio == 0.0


def test_signal_strength_is_reported_as_a_range_not_an_average():
    """A mean hides the thing worth seeing. Somebody standing in the path
    moves the extremes long before it moves the mean."""
    lines = [csi(i, at=i / 100, rssi=-30 if i % 2 else -60) for i in range(100)]
    lines.append(stat(0.999, peer=100, other=40))
    stats = report.link_quality(capture(lines), peer=PEER)

    assert stats.rssi_min == -60
    assert stats.rssi_max == -30
    assert stats.rssi_median == -45.0


def test_an_empty_capture_reports_nothing_rather_than_dividing_by_zero():
    stats = report.link_quality(capture([]), peer=PEER)

    assert stats.received == 0
    assert stats.frames_per_second == 0.0
    assert stats.loss_ratio is None


def test_the_rate_is_measured_over_the_seconds_the_link_was_up():
    """A shutdown capture contains half a minute of deliberate silence.
    Counting it answers "frames per second of recording", which is a fact
    about the experiment, not about the link — the first synthetic run
    through this tool reported 73.6/s for a link that carried 98."""
    stats = report.link_quality(
        capture(running(10) + silent(10, start=10)), peer=PEER
    )

    assert stats.seconds_observed == 10.0
    assert stats.frames_per_second == 100.0
