"""The add-on answers Home Assistant's ingress gateway and nobody else."""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main  # noqa: E402


@pytest.fixture
def plain(monkeypatch, tmp_path):
    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ECHOLOT_MQTT_EXPORT", "false")
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("ECHOLOT_INGRESS_ONLY", raising=False)
    return monkeypatch


def get(peer, path="/api/health"):
    with TestClient(main.app, client=(peer, 50000)) as c:
        return c.get(path)


@pytest.mark.parametrize("peer", ["172.30.32.1", "172.30.33.7", "192.168.1.20", "10.0.0.5"])
def test_other_peers_are_refused_as_an_add_on(plain, peer):
    plain.setenv("ECHOLOT_INGRESS_ONLY", "true")
    r = get(peer, "/api/devices")
    assert r.status_code == 403 and "Ingress" in r.text


@pytest.mark.parametrize("peer", ["172.30.32.2", "::ffff:172.30.32.2", "127.0.0.1", "::1"])
def test_the_ingress_gateway_and_the_container_itself_are_answered(plain, peer):
    plain.setenv("ECHOLOT_INGRESS_ONLY", "true")
    assert get(peer).status_code == 200


def test_the_supervisor_token_switches_it_on_without_the_flag(plain):
    plain.setenv("SUPERVISOR_TOKEN", "x")
    assert get("172.30.33.7").status_code == 403
    plain.setenv("ECHOLOT_INGRESS_ONLY", "false")
    assert get("172.30.33.7").status_code == 200


def test_outside_home_assistant_nothing_is_restricted(plain):
    assert get("192.168.1.20").status_code == 200


def test_the_run_script_turns_it_on():
    run = Path(__file__).resolve().parents[1] / "rootfs" / "etc" / "s6-overlay" / "s6-rc.d" / "echolot" / "run"
    assert 'export ECHOLOT_INGRESS_ONLY="true"' in run.read_text()
