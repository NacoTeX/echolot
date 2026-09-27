"""Recordings and their playback (review P0-03).

A recording holds what the engine was given; played back through the same
engine on a virtual clock it gives the same results every time, and the
same results the live engine gave at the same instants. Errors are only
reported where marks say what was really going on.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import recording, replay, rooms  # noqa: E402
from app.devices import Device, DeviceCreate  # noqa: E402
from app.radar_frame import parse_frame  # noqa: E402
from app.radar_link import LinkSnapshot  # noqa: E402
from app.room_engine import RoomEngine  # noqa: E402
from app.rooms import Room  # noqa: E402

SECRETS = {"api_encryption_key": "S0VZS0VZS0VZS0VZS0VZS0VZS0VZS0VZS0VZS0VZS0U=", "ota_password": "ota-geheim-123"}


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(rooms, "DATA_DIR", tmp_path)
    return tmp_path


class Clock:
    def __init__(self):
        self.now = 500.0

    def __call__(self):
        return self.now


class Links:
    def __init__(self):
        self.snaps = {}

    def snapshot(self, device_id):
        return self.snaps.get(device_id)


def device(**config):
    return Device(id="dev", created_at=0, updated_at=0, **SECRETS,
                  config=DeviceCreate(name="radar", board="esp32c5", wifi_ssid="MeinWLAN",
                                      wifi_password="wlan-geheim-456", **config))


def room(**extra):
    data = {
        "id": "r1", "name": "Wohnzimmer", "width": 6, "height": 4,
        "sensor": {"device_id": "dev", "x": 3, "y": 0, "angle": 0},
        "zones": [{"id": "sofa", "name": "Sofa", "kind": "detect", "points": [[0, 2], [3, 2], [3, 4], [0, 4]], "hold_s": 2}],
        "hold_s": 5,
        "calibration": {"confirm_s": 1.0, "smoothing": "off"},
    }
    data.update(extra)
    return Room.model_validate(data)


class Rig:
    """A link, a live engine and a recorder, fed line by line — the way the
    add-on feeds them, with the engine evaluated after every line."""

    def __init__(self, the_room=None, the_device=None):
        self.clock = Clock()
        self.links = Links()
        self.snap = LinkSnapshot(device_id="dev")
        self.links.snaps["dev"] = self.snap
        self.room = the_room or room()
        self.device = the_device or device()
        self.engine = RoomEngine(self.links, clock=self.clock)
        self.recorder = recording.Recorder(self.links, clock=self.clock, wall=lambda: 1_700_000_000.0)
        self.live = []
        self.seq = 0
        self.connect()
        self.engine.load([self.room], [self.device])

    def connect(self):
        self.snap.new_session()
        self.snap.connected = True
        self.recorder.on_link("dev", True, False, self.clock.now)

    def start(self, **kw):
        #: The recording's zero on this clock.
        self.t0 = self.clock.now
        return self.recorder.start(self.room, self.device, definition=7, alignment=3, addon="test", **kw)

    def line(self, text, step=0.2):
        self.clock.now += step
        self.recorder.on_line("dev", text, self.clock.now)
        try:
            self.snap.record(parse_frame(text), self.clock.now)
        except ValueError:
            return None
        result = self.engine.evaluate()[0]
        result.pop("evaluated_at", None)
        self.live.append((self.clock.now, result))
        return result

    def report(self, targets="", step=0.2):
        self.seq += 1
        return self.line(f"1|R|{self.seq}|{targets}", step)

    def repeat(self, step=0.2):
        """The firmware's heartbeat: the last line again."""
        return self.line(self.snap.frame.as_line(), step)

    def mark(self, **mark):
        return self.recorder.mark("r1", recording.Mark.model_validate(mark))


def recorded(rig):
    entry = rig.recorder.stop("r1", "Beendet")
    return entry, *recording.load(entry["id"])


# --- recording --------------------------------------------------------------------


def test_nothing_is_recorded_until_somebody_starts_it(data_dir):
    rig = Rig()
    rig.report("-15,30")
    assert not (data_dir / "recordings").exists()
    assert recording.list_recordings() == []


def test_a_recording_holds_every_line_as_it_arrived_and_no_credentials(data_dir):
    rig = Rig()
    rig.report("-15,30")
    status = rig.start(limit_s=60, note="Sofa-Test")
    assert status["state"] == "recording" and status["lines"] == 0
    rig.report("-15,30")
    rig.repeat()  # a heartbeat repeat is recorded too: it keeps the link fresh
    rig.line("kaputt")  # and a line nobody could read
    rig.mark(kind="people", count=1)
    entry, header, events = recorded(rig)
    assert entry["state"] == "done" and entry["lines"] == 3 and entry["marks"] == 1
    assert header["format"] == recording.FORMAT and header["synthetic"] is False
    assert header["room"]["id"] == "r1" and header["device"]["id"] == "dev"
    assert header["last_line"] == {"text": "1|R|1|-15,30", "age_s": 0.0}
    assert [e["type"] for e in events] == ["line", "line", "line", "mark"]
    assert [e["t"] for e in events] == pytest.approx([0.2, 0.4, 0.6, 0.6])
    assert events[1]["text"] == events[0]["text"] and events[2]["text"] == "kaputt"
    text = recording.path_of(entry["id"]).read_text()
    for secret in (*SECRETS.values(), "wlan-geheim-456", "MeinWLAN"):
        assert secret not in text


def test_connections_coming_and_going_are_recorded():
    rig = Rig()
    rig.start(limit_s=60)
    rig.recorder.on_link("dev", False, False, rig.clock.now + 1)
    rig.recorder.on_link("dev", True, False, rig.clock.now + 3)
    _, _, events = recorded(rig)
    assert [(e["type"], e["connected"], e["t"]) for e in events] == [("link", False, 1.0), ("link", True, 3.0)]


def test_a_recording_stops_on_time_even_while_nothing_arrives():
    rig = Rig()
    rig.start(limit_s=10)
    rig.clock.now += 9.9
    rig.recorder.tick()
    assert rig.recorder.status("r1") is not None
    rig.clock.now += 0.1
    rig.recorder.tick()
    assert rig.recorder.status("r1") is None
    (entry,) = recording.list_recordings()
    assert entry["state"] == "done" and entry["stop_reason"] == "Zeit erreicht"


def test_a_line_after_the_time_is_not_recorded():
    rig = Rig()
    rig.start(limit_s=10)
    rig.report("-15,30", step=10.5)
    (entry,) = recording.list_recordings()
    assert entry["lines"] == 0 and entry["stop_reason"] == "Zeit erreicht"


def test_the_storage_limit_is_kept(monkeypatch):
    monkeypatch.setattr(recording, "MIN_FREE_BYTES", 1000)
    monkeypatch.setattr(recording, "MAX_TOTAL_BYTES", 6000)
    rig = Rig()
    rig.start(limit_s=600)
    for _ in range(200):
        rig.report("-15,30")
    (entry,) = recording.list_recordings()
    assert entry["stop_reason"] == "Speicher voll" and entry["bytes"] <= 6000
    assert recording.path_of(entry["id"]).stat().st_size == entry["bytes"]
    with pytest.raises(recording.RecordingError, match="Speicher"):
        rig.start(limit_s=60)


def test_only_one_recording_per_room_and_limits_are_checked():
    rig = Rig()
    with pytest.raises(recording.RecordingError):
        rig.start(limit_s=5)
    rig.start(limit_s=60)
    with pytest.raises(recording.RecordingError, match="schon"):
        rig.start(limit_s=60)


def test_a_recording_ends_with_its_room_or_its_sensor():
    rig = Rig()
    first = rig.start(limit_s=60)["id"]
    rig.recorder.sync([room(sensor={"device_id": "other", "x": 3, "y": 0})])
    assert recording.get(first)["stop_reason"] == "Anderer Sensor zugeordnet"
    second = rig.start(limit_s=60)["id"]
    rig.recorder.sync([rig.room])  # nothing changed: it goes on
    assert rig.recorder.status("r1") is not None
    rig.recorder.sync([])
    assert recording.get(second)["stop_reason"] == "Raum gelöscht"


def test_marks_are_checked_against_the_recorded_room():
    rig = Rig()
    rig.start(limit_s=60)
    with pytest.raises(recording.RecordingError, match="Zone"):
        rig.mark(kind="zone", zone_id="nope", inside=True)
    with pytest.raises(recording.RecordingError, match="Standpunkt"):
        rig.mark(kind="standpoint_end")
    with pytest.raises(ValueError):
        recording.Mark.model_validate({"kind": "standpoint", "x": 1.0})
    rig.mark(kind="standpoint", x=1.5, y=3.0)
    assert rig.recorder.status("r1")["standpoint"] == {"x": 1.5, "y": 3.0, "since": 0.0}
    rig.mark(kind="standpoint_end")
    assert rig.recorder.status("r1")["standpoint"] is None


def test_a_recording_cut_off_by_a_restart_is_kept_and_says_so():
    rig = Rig()
    rig.start(limit_s=60)
    rig.report("-15,30")
    assert recording.mark_interrupted() == [recording.list_recordings()[0]["id"]]
    entry = recording.list_recordings()[0]
    assert entry["state"] == "interrupted" and entry["bytes"] > 0


def test_a_file_is_read_line_by_line_and_a_cut_off_last_line_is_dropped():
    rig = Rig()
    rig.start(limit_s=60)
    rig.report("-15,30")
    entry, header, events = recorded(rig)
    text = recording.path_of(entry["id"]).read_text()
    header2, events2 = recording.parse(text + '{"t": 9, "type": "li')
    assert header2 == header and events2 == events
    with pytest.raises(recording.RecordingError, match="rückwärts"):
        recording.parse(text + '{"t": 0.1, "type": "line", "text": "x"}\n')
    with pytest.raises(recording.RecordingError, match="unbekanntes"):
        recording.parse(text + '{"t": 9, "type": "shell", "text": "x"}\n{"t": 10, "type": "line", "text": "y"}\n')
    with pytest.raises(recording.RecordingError, match="Kopf"):
        recording.parse('{"t": 0, "type": "line", "text": "x"}\n')


def test_an_imported_recording_keeps_what_it_says_it_is():
    rig = Rig()
    rig.start(limit_s=60)
    rig.report("-15,30")
    entry, header, _ = recorded(rig)
    lines = recording.path_of(entry["id"]).read_text().splitlines()
    lines[0] = json.dumps({**header, "synthetic": True})
    imported = recording.import_text("\n".join(lines))
    assert imported["synthetic"] is True and imported["imported"] is True and imported["lines"] == 1
    assert recording.load(imported["id"])[1] == recording.load(entry["id"])[1]


# --- playback -----------------------------------------------------------------------


def walk_and_sit(rig):
    """Somebody comes in, sits on the sofa for a while, gets up and leaves.

    Sensor at (3, 0) looking down: report (x, y) dm is room (3 + x/10, y/10).
    The sofa zone is room x 0…3, y 2…4. Every step stays within the
    tracker's gate, and the zone marks are made between the last report
    on one side of its edge and the first on the other.
    """
    rig.start(limit_s=600)
    rig.mark(kind="people", count=0)
    for _ in range(5):
        rig.report("")
    rig.mark(kind="people", count=1)  # comes in on the right, along y = 1.8
    for x in range(25, -16, -5):
        rig.report(f"{x},18")
    rig.mark(kind="zone", zone_id="sofa", inside=True)  # the next report is in the zone
    for y in (22, 26, 30):
        rig.report(f"-15,{y}")
    rig.mark(kind="standpoint", x=1.5, y=3.0, uncertainty_m=0.2)
    for i in range(40):
        rig.report(f"{-15 + (i % 3) - 1},30")  # ±10 cm across
        if i % 4 == 0:
            rig.repeat()
    rig.mark(kind="standpoint_end")
    for y in (26, 22):
        rig.report(f"-15,{y}")
    rig.mark(kind="zone", zone_id="sofa", inside=False)  # the next report is out of it
    for x in range(-15, 30, 5):
        rig.report(f"{x},18")
    rig.mark(kind="people", count=0)  # gone through the door
    for _ in range(40):
        rig.report("")
    return recorded(rig)


def test_the_same_recording_plays_back_the_same_every_time():
    rig = Rig()
    _, header, events = walk_and_sit(rig)
    first = replay.run(header, events, replay.room_for(header))
    second = replay.run(header, events, replay.room_for(header))
    assert first == second and len(first) > 100


def test_playback_gives_what_the_live_engine_gave_at_the_same_instants():
    """Not a second implementation of the rules: the same engine, the same
    sequence rules, the same answers."""
    rig = Rig()
    _, header, events = walk_and_sit(rig)
    timeline = replay.run(header, events, replay.room_for(header))
    played = {round(e["t"], 4): e["result"] for e in timeline}
    compared = 0
    for now, live in rig.live:
        t = round(now - rig.t0, 4)
        if t <= 0 or t not in played:
            continue
        assert played[t]["count"] == live["count"] and played[t]["occupied"] == live["occupied"], t
        assert played[t]["zones"] == live["zones"], t
        compared += 1
    assert compared > 100


def test_without_marks_there_are_no_error_figures():
    rig = Rig()
    rig.start(limit_s=60)
    for _ in range(10):
        rig.report("-15,30")
    _, header, events = recorded(rig)
    report = replay.report(header, events, replay.run(header, events, replay.room_for(header)))
    assert report["reference"] is False
    assert set(report) == {"summary", "reference"}
    assert report["summary"]["lines"] == 10 and report["summary"]["reports_per_s"] == pytest.approx(5.0, rel=0.05)


def test_marks_give_errors_latencies_and_counts():
    rig = Rig()
    _, header, events = walk_and_sit(rig)
    report = replay.report(header, events, replay.run(header, events, replay.room_for(header)))
    spots = report["standpoints"]
    # Reports scatter 10 cm to either side of the spot, smoothing off: the
    # engine puts the target there, and the scatter shows as jitter.
    assert spots["median_error_m"] == pytest.approx(0.1) and spots["p95_error_m"] == pytest.approx(0.1)
    assert spots["jitter_m"] == pytest.approx(0.0816, abs=0.002)  # sqrt((0.1² + 0 + 0.1²) / 3)
    assert spots["detected_share"] == pytest.approx(1.0) and spots["id_changes"] == 0
    assert spots["reference_uncertainty_m"] == 0.2
    people = report["people"]
    # Coming in: nobody counted until confirmed, 1 s after the first
    # report 0.2 s after the mark.
    assert people["empty_while_occupied_s"] == pytest.approx(1.2, abs=0.01)
    # Leaving: the target is held while the tracker remembers it — the last
    # evaluation that counts it is 1.4 s after the mark — and the room's
    # hold time of 5 s runs from there.
    assert people["occupied_while_empty_s"] == pytest.approx(6.4, abs=0.01)
    assert people["count_right_share"] > 0.8
    sofa = report["zones"]["sofa"]
    assert sofa["reference_changes"] == 2 and sofa["measured_changes"] == 2 and sofa["extra_changes"] == 0
    assert sofa["enter_latency_s"] == [pytest.approx(0.2, abs=0.001)]  # the next report
    assert sofa["leave_latency_s"] == [pytest.approx(2.0, abs=0.001)]  # the zone's hold time


def test_the_same_recording_with_other_settings_is_compared():
    """What a change of settings does, measured on the same reports."""
    rig = Rig()
    _, header, events = walk_and_sit(rig)
    slow = replay.room_for(header, {"confirm_s": 3.0, "hold_s": 0})
    a, b = replay.compare(header, events, [
        {"label": "Wie aufgezeichnet", "room": replay.room_for(header)},
        {"label": "Langsamer bestätigt, ohne Haltezeit", "room": slow},
    ])
    # The last evaluation that still counts the held target is 1.4 s after
    # the mark. With the room's hold time the room stays occupied 5 s from
    # there; without it, until the next evaluation, 0.2 s later.
    assert a["report"]["people"]["occupied_while_empty_s"] == pytest.approx(6.4, abs=0.01)
    assert b["report"]["people"]["occupied_while_empty_s"] == pytest.approx(1.6, abs=0.01)
    # Confirmed after 3 s instead of 1 s: 2 s longer empty after coming in.
    missed = b["report"]["people"]["empty_while_occupied_s"] - a["report"]["people"]["empty_while_occupied_s"]
    assert missed == pytest.approx(2.0, abs=0.01)


def test_the_viewer_gets_at_most_ten_evaluations_a_second():
    rig = Rig()
    _, header, events = walk_and_sit(rig)
    timeline = replay.run(header, events, replay.room_for(header))
    compact = replay.compact(timeline)
    assert all(b["t"] - a["t"] >= 0.1 - 1e-9 for a, b in zip(compact, compact[1:-1]))
    assert compact[-1]["t"] == timeline[-1]["t"]
    assert set(compact[0]) == {"t", "available", "reason", "count", "occupied", "assumed_present", "targets", "zones"}


def test_a_frame_reads_back_as_the_line_it_came_from():
    for text in ("1|R|7|15,23;-4,0", "1|Q|4294967295|", "1|U|0|", "1|R|12|"):
        assert parse_frame(text).as_line() == text


def test_the_times_in_a_file_never_run_back():
    """Whatever a caller hands in, a written file stays readable."""
    rig = Rig()
    rig.start(limit_s=60)
    rig.recorder.on_line("dev", "1|R|1|", rig.clock.now + 2.0)
    rig.mark(kind="note", text="früher")  # the recorder's clock is still at 0 s
    _, _, events = recorded(rig)
    assert [e["t"] for e in events] == [2.0, 2.0]


def test_playback_knows_when_the_sensor_was_gone_or_silent():
    """Unavailable, not empty: the connection dropped for 2 s, then no line
    for 5 s — stale after 3 s, found by the evaluations in between, as the
    live engine's timer finds it."""
    rig = Rig()
    rig.start(limit_s=60)
    for _ in range(10):
        rig.report("-15,30")
    rig.clock.now += 0.1
    rig.recorder.on_link("dev", False, False, rig.clock.now)
    rig.snap.connected = False
    rig.clock.now += 2.0
    rig.connect()
    for _ in range(5):
        rig.report("-15,30")
    rig.report("-15,30", step=5.0)  # silence, then one more
    _, header, events = recorded(rig)
    timeline = replay.run(header, events, replay.room_for(header))
    reasons = [e["result"]["reason"] for e in timeline if not e["result"]["available"]]
    assert "offline" in reasons and "stale" in reasons
    last_before_silence = timeline[-1]["t"] - 5.0
    stale = [e["t"] for e in timeline if e["result"]["reason"] == "stale" and e["t"] > last_before_silence]
    # Found by the idle evaluations, every 0.5 s: stale 3 s after the last line.
    assert stale[0] == pytest.approx(last_before_silence + 3.0, abs=1e-6)
    summary = replay.report(header, events, timeline)["summary"]
    assert 0.5 < summary["available_share"] < 0.8



def test_a_standpoint_is_judged_on_what_came_after_its_mark():
    """The button is pressed once standing; an evaluation at that very
    moment still rests on the report from the way there."""
    rig = Rig()
    rig.start(limit_s=60)
    for _ in range(7):
        rig.report("0,20")  # room (3.0, 2.0), confirmed
    rig.mark(kind="standpoint", x=3.0, y=2.5)
    for _ in range(10):
        rig.report("0,25")
    _, header, events = recorded(rig)
    spots = replay.report(header, events, replay.run(header, events, replay.room_for(header)))["standpoints"]
    assert spots["p95_error_m"] == pytest.approx(0.0) and spots["jitter_m"] == pytest.approx(0.0)
