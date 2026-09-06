"""Tests for the device network probe.

Real sockets on loopback, not mocks: the point of this module is to say
something true about the network, and a mocked socket cannot be wrong in
the ways a real one is.
"""

import asyncio
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import reachability  # noqa: E402


class Listener:
    """A real TCP server on a real port, for the duration of a test."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]

    def close(self):
        self.sock.close()


@pytest.fixture
def listener():
    lis = Listener()
    yield lis
    lis.close()


def test_it_sees_an_open_port(listener):
    assert asyncio.run(reachability._probe("127.0.0.1", listener.port, 2.0)) is True


def test_it_sees_a_closed_port(listener):
    port = listener.port
    listener.close()
    assert asyncio.run(reachability._probe("127.0.0.1", port, 2.0)) is False


def test_an_unresolvable_name_is_reported_as_such():
    """Distinct from 'silent': the fix is DNS, not the network."""
    result = asyncio.run(reachability.check("kein-solcher-host.invalid", timeout=1.0))
    assert result["verdict"] == "unresolved"
    assert result["reachable"] is False
    assert "IP-Adresse" in reachability.explain(result)


def test_a_resolvable_but_silent_host_is_reported_as_silent():
    result = asyncio.run(reachability.check("127.0.0.1", timeout=0.5))
    # Nothing of ours listens on 6053 or 80 here.
    assert result["verdict"] == "silent"
    assert result["resolved"] == "127.0.0.1"
    assert "Client-Isolation" in reachability.explain(result)


def test_an_answering_api_port_means_home_assistant_is_the_problem(monkeypatch, listener):
    """The whole reason this module exists: separating a dead device from
    a live one Home Assistant has simply not adopted."""
    monkeypatch.setattr(reachability, "API_PORT", listener.port)
    monkeypatch.setattr(reachability, "WEB_PORT", listener.port)
    result = asyncio.run(reachability.check("127.0.0.1", timeout=2.0))
    assert result["verdict"] == "ok"
    assert result["api"] is True
    assert "Geräte & Dienste" in reachability.explain(result)


def test_only_the_status_page_answering_points_at_the_device(monkeypatch, listener):
    monkeypatch.setattr(reachability, "WEB_PORT", listener.port)
    # API_PORT stays 6053, where nothing listens.
    result = asyncio.run(reachability.check("127.0.0.1", timeout=2.0))
    assert result["verdict"] == "web_only"
    assert result["web"] is True
    assert result["api"] is False


def test_every_verdict_has_a_message():
    """A verdict with no explanation would reach the UI as a bare word."""
    for verdict in ("unresolved", "silent", "web_only", "ok"):
        assert reachability.VERDICT_MESSAGES[verdict].strip()


def test_the_direct_port_is_reported_when_it_answers(monkeypatch, listener):
    """Port 62587 decides whether the Calibration Lab has any data.

    It was probed from the start but never made it into a message, so the
    add-on measured the answer and then discarded it.
    """
    monkeypatch.setattr(reachability, "API_PORT", listener.port)
    monkeypatch.setattr(reachability, "DIRECT_PORT", listener.port)
    result = asyncio.run(reachability.check("127.0.0.1", timeout=2.0))
    assert result["direct"] is True
    assert "62587" in reachability.explain_direct(result)
    assert "Calibration Lab" in reachability.explain_direct(result)


def test_a_device_answering_without_the_direct_port_is_told_what_that_costs():
    result = {"verdict": "ok", "direct": False, "host": "h", "resolved": "h"}
    message = reachability.explain_direct(result)
    assert "direct_api" in message
    assert "Neubau" in message


def test_the_direct_port_stays_quiet_when_nothing_answered_at_all():
    """On a silent or unresolvable device it would read as a second fault."""
    for verdict in ("unresolved", "silent"):
        result = {"verdict": verdict, "direct": False, "host": "h", "resolved": None}
        assert reachability.explain_direct(result) is None


def test_the_verdict_does_not_turn_on_the_direct_port(monkeypatch, listener):
    """Home Assistant needs 6053; a device serving only 62587 is not "ok"."""
    monkeypatch.setattr(reachability, "DIRECT_PORT", listener.port)
    result = asyncio.run(reachability.check("127.0.0.1", timeout=2.0))
    assert result["direct"] is True
    assert result["api"] is False
    assert result["verdict"] == "web_only"
