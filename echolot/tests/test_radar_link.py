"""The live link to a radar node, against a stand-in for aioesphomeapi."""

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import radar_link  # noqa: E402
from app.devices import Device, DeviceCreate  # noqa: E402


@dataclass
class Info:
    key: int
    name: str


@dataclass
class State:
    key: int
    state: object
    missing_state: bool = False


class FakeClient:
    instances = []
    fail_with = None
    entities = [Info(1, "Radar Frame"), Info(2, "Radar Status"), Info(3, "WiFi Signal"), Info(4, "Radar Firmware")]

    def __init__(self, address, port, key, name):
        self.address, self.port, self.key, self.name = address, port, key, name
        self.on_stop = None
        self.on_state = None
        self.disconnected = False
        FakeClient.instances.append(self)

    async def connect(self, on_stop=None, login=False):
        if FakeClient.fail_with:
            raise FakeClient.fail_with
        self.on_stop = on_stop

    async def list_entities_services(self):
        return list(FakeClient.entities), []

    def subscribe_states(self, callback):
        self.on_state = callback

    async def disconnect(self, force=False):
        self.disconnected = True


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    FakeClient.instances = []
    FakeClient.fail_with = None
    FakeClient.entities = [Info(1, "Radar Frame"), Info(2, "Radar Status"), Info(3, "WiFi Signal"),
                           Info(4, "Radar Firmware")]
    monkeypatch.setattr(radar_link, "BACKOFF", (0.01, 0.01))


KEY = "S0VZS0VZS0VZS0VZS0VZS0VZS0VZS0VZS0VZS0VZS0U="


def device(address=None, key=KEY):
    return Device(id="dev", created_at=0, updated_at=0, api_encryption_key=key,
                  config=DeviceCreate(name="wohnzimmer", board="esp32c5", wifi_ssid="n"), address=address)


async def settle(times=5):
    for _ in range(times):
        await asyncio.sleep(0.02)


def test_connects_with_the_devices_key_and_name():
    async def run():
        links = radar_link.RadarLinks(client_factory=FakeClient)
        await links.sync([device(address="192.168.1.9")])
        await settle()
        client = FakeClient.instances[0]
        assert (client.address, client.port, client.name) == ("192.168.1.9", 6053, "wohnzimmer")
        assert client.key == KEY
        assert links.snapshot("dev").connected
        await links.stop_all()
    asyncio.run(run())


def test_frames_are_parsed_and_listeners_told():
    async def run():
        seen = []
        links = radar_link.RadarLinks(client_factory=FakeClient)
        links.add_listener(seen.append)
        await links.sync([device()])
        await settle()
        client = FakeClient.instances[0]
        client.on_state(State(1, "1|R|7|15,23"))
        client.on_state(State(3, -61.0))
        client.on_state(State(4, "V1.2 (2025-06)"))
        client.on_state(State(99, "unrelated"))
        snap = links.snapshot("dev")
        assert snap.frame.targets_m == ((1.5, 2.3),)
        assert snap.fresh()
        assert (snap.wifi_signal, snap.firmware) == (-61.0, "V1.2 (2025-06)")
        assert seen == ["dev"]
        await links.stop_all()
    asyncio.run(run())


def test_a_malformed_frame_does_not_replace_a_good_one():
    async def run():
        links = radar_link.RadarLinks(client_factory=FakeClient)
        await links.sync([device()])
        await settle()
        client = FakeClient.instances[0]
        client.on_state(State(1, "1|R|7|15,23"))
        client.on_state(State(1, "2|R|8|1,1"))
        client.on_state(State(1, "1|R|9|"), )
        client.on_state(State(1, "garbage", missing_state=True))
        assert links.snapshot("dev").frame.seq == 9
        client.on_state(State(1, "1|X|10|"))
        assert links.snapshot("dev").frame.seq == 9
        await links.stop_all()
    asyncio.run(run())


def test_a_node_without_a_frame_entity_is_reported():
    async def run():
        FakeClient.entities = [Info(10, "Motion Detected"), Info(11, "Movement Score")]
        links = radar_link.RadarLinks(client_factory=FakeClient)
        await links.sync([device()])
        await settle()
        snap = links.snapshot("dev")
        assert snap.connected and snap.no_frame_entity
        assert "keine Echolot-Radar-Firmware" in snap.error
        await links.stop_all()
    asyncio.run(run())


def test_a_dropped_connection_reconnects():
    async def run():
        links = radar_link.RadarLinks(client_factory=FakeClient)
        await links.sync([device()])
        await settle()
        first = FakeClient.instances[0]
        await first.on_stop(False)
        await settle()
        assert len(FakeClient.instances) == 2
        assert first.disconnected
        assert links.snapshot("dev").connected
        await links.stop_all()
    asyncio.run(run())


def test_a_failed_connection_is_explained_and_retried():
    class InvalidEncryptionKeyAPIError(Exception):
        pass

    async def run():
        FakeClient.fail_with = InvalidEncryptionKeyAPIError("bad key")
        links = radar_link.RadarLinks(client_factory=FakeClient)
        await links.sync([device()])
        await settle()
        snap = links.snapshot("dev")
        assert not snap.connected
        assert "Schlüssel abgelehnt" in snap.error
        assert snap.attempts >= 2
        FakeClient.fail_with = None
        await settle(10)
        assert links.snapshot("dev").connected
        await links.stop_all()
    asyncio.run(run())


def test_changing_the_address_restarts_the_link_and_removing_stops_it():
    async def run():
        links = radar_link.RadarLinks(client_factory=FakeClient)
        await links.sync([device()])
        await settle()
        await links.sync([device()])
        await settle()
        assert len(FakeClient.instances) == 1  # unchanged: kept
        await links.sync([device(address="10.0.0.5")])
        await settle()
        assert FakeClient.instances[-1].address == "10.0.0.5"
        assert FakeClient.instances[0].disconnected
        await links.sync([device(address="10.0.0.5", key="T0VZ" + KEY[4:])])
        await settle()
        assert FakeClient.instances[-1].key != KEY  # a new key is a new connection
        await links.sync([])
        assert links.snapshot("dev") is None
    asyncio.run(run())


@pytest.mark.parametrize("name, words", [
    ("BadNameAPIError", "anderes ESPHome-Gerät"),
    ("RequiresEncryptionAPIError", "unverschlüsselt"),
    ("ResolveAPIError", "nicht auflösen"),
    ("TimeoutAPIError", "antwortet nicht"),
])
def test_errors_are_named(name, words):
    error = type(name, (Exception,), {})("x")
    assert words in radar_link.explain_error(error, "radar.local")


def test_a_stale_frame_is_not_fresh():
    snap = radar_link.LinkSnapshot(device_id="d", connected=True)
    from app.radar_frame import parse_frame
    snap.frame, snap.frame_at = parse_frame("1|R|1|"), 100.0
    assert snap.fresh(now=102.9)
    assert not snap.fresh(now=103.1)
    snap.connected = False
    assert not snap.fresh(now=100.5)


# --- the sequence rules (LinkSnapshot.record) ------------------------------

from app.radar_frame import parse_frame  # noqa: E402


def recorded(*lines, start=100.0, step=0.2):
    snap = radar_link.LinkSnapshot(device_id="d", connected=True)
    results = [snap.record(parse_frame(line), start + i * step) for i, line in enumerate(lines)]
    return snap, results


def test_a_repeated_line_keeps_the_link_alive_but_is_no_new_measurement():
    """The firmware republishes the last line every second as a heartbeat."""
    snap, results = recorded("1|R|7|15,23", "1|R|7|15,23", "1|R|7|15,23")
    assert results == [True, False, False]
    assert snap.duplicates == 2 and snap.frames_received == 1
    assert snap.frame_at == pytest.approx(100.4) and snap.measured_at == pytest.approx(100.0)
    assert [i for i, _, _ in snap.queue] == [1]


def test_a_state_change_under_the_same_number_is_taken():
    snap, results = recorded("1|R|7|15,23", "1|Q|7|")
    assert results == [True, True] and snap.frame.state == "quiet"


def test_numbers_that_skip_ahead_count_the_reports_in_between():
    snap, results = recorded("1|R|7|", "1|R|10|")
    assert results == [True, True] and snap.reports_skipped == 2


def test_an_older_line_is_ignored_and_counted():
    snap, results = recorded("1|R|10|15,23", "1|R|9|0,5")
    assert results == [True, False]
    assert snap.out_of_order == 1 and snap.frame.seq == 10


def test_the_counter_wrapping_after_four_billion_reports_is_just_the_next_one():
    snap, results = recorded(f"1|R|{2**32 - 1}|", "1|R|0|", "1|R|1|")
    assert results == [True, True, True]
    assert snap.out_of_order == 0 and snap.reports_skipped == 0


def test_a_new_connection_starts_the_sequence_over():
    """A rebooted device counts from zero again; it also drops the link."""
    snap, _ = recorded("1|R|5000|15,23")
    snap.new_session()
    assert snap.frame is None and not snap.fresh(now=100.1) and not snap.queue
    assert snap.record(parse_frame("1|R|1|"), 101.0) is True
    assert snap.session == 1


def test_the_queue_is_bounded_and_says_what_it_dropped():
    lines = [f"1|R|{i}|" for i in range(1, radar_link.QUEUE_LENGTH + 11)]
    snap, _ = recorded(*lines, step=0.01)
    assert len(snap.queue) == radar_link.QUEUE_LENGTH and snap.dropped == 10
    assert [i for i, _, _ in snap.pending(snap.index - 2)] == [snap.index - 1, snap.index]


def test_listeners_hear_of_new_measurements_only():
    async def run():
        seen = []
        links = radar_link.RadarLinks(client_factory=FakeClient)
        links.add_listener(seen.append)
        await links.sync([device()])
        await settle()
        client = FakeClient.instances[0]
        for line in ("1|R|7|15,23", "1|R|7|15,23", "1|R|6|0,5", "1|R|8|15,24"):
            client.on_state(State(1, line))
        assert seen == ["dev", "dev"]
        assert links.snapshot("dev").duplicates == 1
        await links.stop_all()
    asyncio.run(run())


# --- the module's own mounting ----------------------------------------------

MOUNT_ENTITIES = [Info(1, "Radar Frame"), Info(5, "Radar Mount Mode"), Info(6, "Radar Mount Height"),
                  Info(7, "Radar Mount Angle")]


class CommandClient(FakeClient):
    def __init__(self, *args):
        super().__init__(*args)
        self.commands = []

    def select_command(self, key, state):
        self.commands.append(("select", key, state))

    def number_command(self, key, state):
        self.commands.append(("number", key, state))


def test_the_mounting_is_what_the_module_read_back_and_listeners_hear_once(monkeypatch):
    monkeypatch.setattr(radar_link, "MOUNTING_SETTLE_S", 0.05)

    async def run():
        FakeClient.entities = MOUNT_ENTITIES
        heard = []
        links = radar_link.RadarLinks(client_factory=CommandClient)
        links.add_mounting_listener(heard.append)
        await links.sync([device()])
        await settle()
        snap = links.snapshot("dev")
        assert snap.mounting_entities and snap.mounting() is None
        client = FakeClient.instances[0]
        client.on_state(State(5, "side"))
        client.on_state(State(6, 2.6000001))
        assert snap.mounting() is None  # the angle is still missing
        client.on_state(State(7, 25.0))
        assert snap.mounting() == {"mode": "side", "height_m": 2.6, "angle_deg": 25.0}
        await asyncio.sleep(0.15)
        assert heard == ["dev"]
        # Height and angle arrive one by one after a change: heard once.
        client.on_state(State(6, 2.4))
        client.on_state(State(7, 30.0))
        await asyncio.sleep(0.15)
        assert heard == ["dev", "dev"]
        # A repeat of the same values is nothing new.
        client.on_state(State(7, 30.0))
        await asyncio.sleep(0.15)
        assert heard == ["dev", "dev"]
        # An unknown mode or a missing number is not a mounting.
        client.on_state(State(5, "diagonal"))
        assert snap.mounting() is None
        await links.stop_all()
    asyncio.run(run())


def test_writing_sends_only_what_differs_and_needs_the_entities():
    async def run():
        FakeClient.entities = MOUNT_ENTITIES
        links = radar_link.RadarLinks(client_factory=CommandClient)
        await links.sync([device()])
        await settle()
        client = FakeClient.instances[0]
        for state in (State(5, "side"), State(6, 2.6), State(7, 25.0)):
            client.on_state(state)
        links.link("dev").write_mounting("side", 2.4, 25.0)
        assert client.commands == [("number", 6, 2.4)]
        client.commands.clear()
        links.link("dev").write_mounting("top", 2.4, 30.0)
        assert client.commands == [("select", 5, "top"), ("number", 6, 2.4), ("number", 7, 30.0)]
        await links.stop_all()
        # Not connected any more.
        with pytest.raises(radar_link.MountingError):
            links_off = radar_link.RadarLink(device(), CommandClient, None)
            links_off.write_mounting("side", 2.6, 25.0)

        # A firmware without the entities.
        FakeClient.entities = [Info(1, "Radar Frame")]
        old = radar_link.RadarLinks(client_factory=CommandClient)
        await old.sync([device()])
        await settle()
        with pytest.raises(radar_link.MountingError, match="neu bauen"):
            old.link("dev").write_mounting("side", 2.6, 25.0)
        await old.stop_all()
    asyncio.run(run())


def test_a_new_connection_forgets_the_mounting_until_the_module_says_it_again():
    snap = radar_link.LinkSnapshot(device_id="dev")
    snap.mount_mode, snap.mount_height_m, snap.mount_angle_deg = "side", 2.6, 25.0
    snap.new_session()
    assert snap.mounting() is None
