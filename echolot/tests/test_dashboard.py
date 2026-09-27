"""The dashboard card: its page in the add-on, and what the room page
hands out to put it on a dashboard."""

import asyncio
import re
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import dashboard  # noqa: E402

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ECHOLOT_MQTT_EXPORT", "false")
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("ECHOLOT_SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setattr(dashboard, "_slug", None)
    from app import devices, rooms

    monkeypatch.setattr(devices, "DATA_DIR", tmp_path)
    monkeypatch.setattr(devices, "DEVICES_DIR", tmp_path / "devices")
    monkeypatch.setattr(devices, "INDEX_PATH", tmp_path / "devices.json")
    monkeypatch.setattr(rooms, "DATA_DIR", tmp_path)
    from app.main import ASSET_PREFIX, app

    with TestClient(app) as test_client:
        yield test_client, ASSET_PREFIX


def test_the_card_file_and_the_add_on_agree_on_its_version():
    source = (STATIC / dashboard.CARD_FILE).read_text(encoding="utf-8")
    assert re.search(r"const ECHOLOT_CARD_VERSION = (\d+);", source).group(1) == str(dashboard.CARD_VERSION)
    # The card talks to Home Assistant the way its add-on panel does.
    for call in ('endpoint: "/ingress/session"', 'endpoint: "/ingress/validate_session"', "ingress_session=",
                 "path=${INGRESS_PREFIX}", "is_admin"):
        assert call in source, call


def test_the_card_page_loads_this_release_s_files_and_is_never_cached(client):
    c, prefix = client
    page = c.get("/embed?room=abc")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-cache"
    html = page.text
    for name in ("style.css", "embed.js", "geometry.js", "plan.js"):
        assert f'"{prefix}/{name}"' in html, name
        assert c.get(f"/{prefix}/{name}").status_code == 200
    # Beside index.html, so relative URLs resolve alike: no second level.
    assert 'src="static/' not in html and 'href="static/' not in html
    # The plan takes escapeHtml from Echolot; embed.js provides it first.
    assert html.index("embed.js") < html.index("plan.js")


def test_the_room_page_hands_out_the_card_and_where_it_goes(client):
    c, prefix = client
    info = c.get("/api/dashboard-card").json()
    assert info == {"slug": None, "file": "echolot-room-card.js", "resource": "/local/echolot-room-card.js",
                    "card_version": dashboard.CARD_VERSION}
    card = c.get(f"/{prefix}/echolot-room-card.js")
    assert card.status_code == 200 and "javascript" in card.headers["content-type"]


def test_the_slug_comes_from_the_supervisor_and_is_remembered(monkeypatch):
    monkeypatch.setattr(dashboard, "_slug", None)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t0ken")
    asked = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append((str(request.url), request.headers.get("authorization")))
        return httpx.Response(200, json={"result": "ok", "data": {"slug": "a1b2c3d4_echolot", "name": "Echolot"}})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(answer), **kw))
    assert asyncio.run(dashboard.own_slug()) == "a1b2c3d4_echolot"
    assert asyncio.run(dashboard.own_slug()) == "a1b2c3d4_echolot"
    assert asked == [("http://supervisor/addons/self/info", "Bearer t0ken")]


@pytest.mark.parametrize("status, body", [(401, {}), (200, {"data": {}}), (200, {"data": {"slug": ""}})])
def test_no_slug_when_the_supervisor_does_not_say(monkeypatch, status, body):
    monkeypatch.setattr(dashboard, "_slug", None)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t0ken")
    real = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    assert asyncio.run(dashboard.own_slug()) is None


def test_no_slug_when_the_supervisor_is_out_of_reach(monkeypatch):
    monkeypatch.setattr(dashboard, "_slug", None)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t0ken")

    def fail(request):
        raise httpx.ConnectError("no route", request=request)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(fail), **kw))
    assert asyncio.run(dashboard.own_slug()) is None
