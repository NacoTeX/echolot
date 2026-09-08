"""A zone that stays occupied because the room keeps crossing.

The point of the whole rate detector: motion drops out the moment
somebody stops moving, and the crossing rate over the last minute does
not. This drives the real path — live stream, learned profile, zone state
machine — with the readings recorded from one person sitting still on a
couch and from the same room empty.
"""

import csv
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import devices as dev_mod, feature_api, live_presence, main, presence_rate  # noqa: E402
from app.devices import BuildStatus, Device, DeviceCreate  # noqa: E402
from app.telemetry import Sample  # noqa: E402

DATA = Path(__file__).parent / "data"


def load(name: str, label: str | None = None) -> list[dict]:
    with open(DATA / name) as fh:
        return [
            {"t": float(r["t"]), "movement_score": float(r["movement_score"]), "label": r["label"]}
            for r in csv.DictReader(fh)
            if r["movement_score"] and (label is None or r["label"] == label)
        ]


class Zone:
    def __init__(self, device_ids):
        self.id = "wohnzimmer"
        self.name = "Wohnzimmer"
        self.device_ids = device_ids
        self.enter_threshold = None
        self.exit_threshold = None
        self.hold_seconds = 0.0


def make_device(profile: dict | None) -> Device:
    device = Device(
        id="probe",
        created_at=1,
        updated_at=2,
        status=BuildStatus.SUCCESS,
        config=DeviceCreate(
            name="probe", board="esp32c6", wifi_ssid="netz", wifi_password="passwort123"
        ),
    )
    device.entity_movement_score = "sensor.probe_movement_score"
    device.entity_motion = "binary_sensor.probe_motion_detected"
    device.presence_profile = profile
    return device


class LoadedStream:
    """A live stream already holding a stretch of recorded readings.

    `ends_at` picks which moment of the recording is "now", because a
    recording is not uniform: the last minute of the couch session reads
    as empty, and that is a real limit worth testing rather than avoiding.
    """

    def __init__(self, rows, ends_at=None):
        self._rows = rows
        self._end = ends_at if ends_at is not None else max(r["t"] for r in rows)
        self.connected = True
        self.error = None

    def window(self, seconds, now=None):
        return [row for row in self._rows if self._end - seconds <= row["t"] <= self._end]


@pytest.fixture
def profile():
    learned = presence_rate.learn_baseline(load("flat_occupied_room_empty.csv"))
    assert learned is not None
    return learned


def wire(monkeypatch, device, rows, ends_at=None):
    monkeypatch.setattr(dev_mod, "get_device", lambda did: device if did == device.id else None)
    monkeypatch.setattr(main.devices, "get_device", dev_mod.get_device)

    class Live:
        def stream(self, device_id):
            return LoadedStream(rows, ends_at) if device_id == device.id else None

    monkeypatch.setattr(feature_api, "live", Live())


def minutes_in(rows, minutes: float) -> float:
    return min(row["t"] for row in rows) + minutes * 60.0


def test_a_still_person_keeps_the_zone_occupied(monkeypatch, profile):
    """Motion says nothing is happening. The rate says the room is
    crossing about ten times as often as it does empty. The zone is
    occupied — which is the case that motion alone cannot see."""
    device = make_device(profile.as_dict())
    couch = load("couch_still.csv")
    wire(monkeypatch, device, couch, ends_at=minutes_in(couch, 1.0))

    assert main._zone_rate_verdict(Zone([device.id])) is True


def test_the_last_minute_of_that_recording_does_not_read_as_occupied(
    monkeypatch, profile
):
    """An honest limit, not a bug hidden by a kind window.

    The fourth window of the couch session crossed at 0.041 a second —
    below what the empty room reaches at its worst. Whether the person had
    already got up cannot be told from the data. A zone with a hold time
    would ride over it; with none, it clears.
    """
    device = make_device(profile.as_dict())
    couch = load("couch_still.csv")
    wire(monkeypatch, device, couch)

    assert main._zone_rate_verdict(Zone([device.id])) is False


def test_an_empty_room_does_not_hold_the_zone(monkeypatch, profile):
    device = make_device(profile.as_dict())
    empty = load("flat_occupied_room_empty.csv", "empty")
    # The first ten minutes, away from the window where somebody walked past.
    start = min(row["t"] for row in empty)
    wire(monkeypatch, device, [r for r in empty if r["t"] <= start + 600])

    assert main._zone_rate_verdict(Zone([device.id])) is False


def test_a_device_without_a_baseline_stays_out_of_the_vote(monkeypatch):
    """Not a "vacant" vote — silence. A zone where nobody has calibrated
    must behave exactly as it did before the rate detector existed."""
    device = make_device(None)
    wire(monkeypatch, device, load("couch_still.csv"))

    assert main._zone_rate_verdict(Zone([device.id])) is None


def test_a_corrupt_stored_profile_is_ignored_rather_than_crashing(monkeypatch):
    device = make_device({"baseline_rate": "nonsense"})
    wire(monkeypatch, device, load("couch_still.csv"))

    assert main._zone_rate_verdict(Zone([device.id])) is None


def test_one_calibrated_member_is_enough(monkeypatch, profile):
    """OR across the members, like the motion aggregation beside it."""
    calibrated = make_device(profile.as_dict())
    bare = make_device(None)
    bare.id = "zweites"

    lookup = {calibrated.id: calibrated, bare.id: bare}
    monkeypatch.setattr(dev_mod, "get_device", lookup.get)
    monkeypatch.setattr(main.devices, "get_device", lookup.get)

    rows = load("couch_still.csv")
    end = minutes_in(rows, 1.0)

    class Live:
        def stream(self, device_id):
            return LoadedStream(rows, end)

    monkeypatch.setattr(feature_api, "live", Live())
    assert main._zone_rate_verdict(Zone([bare.id, calibrated.id])) is True
