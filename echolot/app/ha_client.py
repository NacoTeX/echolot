"""Minimal client for Home Assistant's Core REST API, reached through the
Supervisor proxy (http://supervisor/core/api) using SUPERVISOR_TOKEN.

See https://developers.home-assistant.io/docs/add-ons/communication/ —
`homeassistant_api: true` in config.yaml is what makes this proxy reachable
and populates SUPERVISOR_TOKEN.

Used to read the binary_sensor/sensor entities ESPectre exposes and to call
number.set_value / switch.turn_on for runtime parameter pushes, rather than
re-implementing the ESPHome native API's device encryption ourselves.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone

import httpx

DEFAULT_BASE_URL = "http://supervisor/core/api"

#: One client for the process, so repeated polling reuses connections
#: instead of completing a TCP (and, off-Supervisor, TLS) handshake for
#: every entity read. Created lazily because the base URL and token are
#: only known once the environment is set up.
_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        async with _client_lock:
            if _client is None or _client.is_closed:
                _client = httpx.AsyncClient(
                    base_url=_base_url(),
                    timeout=httpx.Timeout(10.0, connect=5.0),
                    # A zone with several devices reads them at once; the
                    # pool has to be able to hold those open together.
                    limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
                )
    return _client


async def close_client() -> None:
    """Release the pooled connections on shutdown."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


class HomeAssistantUnavailable(Exception):
    """Raised whenever the Core API can't be reached or answers with an error."""


def _base_url() -> str:
    return os.environ.get("ECHOLOT_HA_BASE_URL", DEFAULT_BASE_URL)


def _headers() -> dict:
    token = os.environ.get("ECHOLOT_HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise HomeAssistantUnavailable("No SUPERVISOR_TOKEN available (homeassistant_api not granted?)")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def get_state(entity_id: str) -> dict | None:
    """Returns the entity's state object, or None if it doesn't exist (yet)."""
    headers = _headers()
    try:
        client = await get_client()
        resp = await client.get(f"/states/{entity_id}", headers=headers)
    except httpx.HTTPError as err:
        raise HomeAssistantUnavailable(str(err)) from err
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise HomeAssistantUnavailable(f"GET /states/{entity_id} -> {resp.status_code}")
    return resp.json()


async def list_states() -> list[dict]:
    """Every entity Home Assistant currently knows about.

    **For discovery only.** This is one response carrying every entity in
    the installation — a few hundred kilobytes on a small setup, several
    megabytes on a large one. It is the right call when the question is
    "what did Home Assistant name this device's entities?", which is asked
    once per device and then remembered.

    It is the wrong call for reading state on a timer: three targeted
    reads issued concurrently are a fraction of the bytes and no more
    round-trips. See main._read_device_state.
    """
    headers = _headers()
    try:
        client = await get_client()
        resp = await client.get("/states", headers=headers, timeout=30.0)
    except httpx.HTTPError as err:
        raise HomeAssistantUnavailable(str(err)) from err
    if resp.status_code != 200:
        raise HomeAssistantUnavailable(f"GET /states -> {resp.status_code}")
    payload = resp.json()
    return payload if isinstance(payload, list) else []


async def get_history(entity_id: str, minutes: int) -> list[dict]:
    """Past states for one entity over the last `minutes`, oldest first."""
    end = datetime.now(timezone.utc)
    return await get_history_range(entity_id, end - timedelta(minutes=minutes), end)


async def get_history_range(entity_id: str, start: datetime, end: datetime) -> list[dict]:
    """Past states for one entity between two moments, oldest first.

    /api/history/period/<start> answers with a list of per-entity lists;
    `minimal_response` trims the intermediate entries down to state +
    last_changed, which is all either caller needs.

    `significant_changes_only=0` matters and is easy to get wrong. Home
    Assistant reads that parameter as `query.get(name, "1") != "0"`, so —
    unlike `minimal_response` and `no_attributes` next to it, which are
    presence flags — an empty value leaves the filter *on*. With it on,
    the recorder drops readings, and a rate measured per second of wall
    time would then be a rate of whatever survived the filter.
    """
    headers = _headers()
    params = {
        "filter_entity_id": entity_id,
        "minimal_response": "",
        "no_attributes": "",
        "significant_changes_only": "0",
        "end_time": end.isoformat(),
    }
    start = start.isoformat()
    try:
        client = await get_client()
        resp = await client.get(f"/history/period/{start}", headers=headers, params=params)
    except httpx.HTTPError as err:
        raise HomeAssistantUnavailable(str(err)) from err
    if resp.status_code != 200:
        raise HomeAssistantUnavailable(f"GET /history/period -> {resp.status_code}")

    payload = resp.json()
    if not isinstance(payload, list) or not payload:
        return []
    series = payload[0]
    return series if isinstance(series, list) else []


async def call_service(domain: str, service: str, entity_id: str, **extra) -> None:
    headers = _headers()
    payload = {"entity_id": entity_id, **extra}
    try:
        client = await get_client()
        resp = await client.post(f"/services/{domain}/{service}", headers=headers, json=payload)
    except httpx.HTTPError as err:
        raise HomeAssistantUnavailable(str(err)) from err
    if resp.status_code not in (200, 201):
        raise HomeAssistantUnavailable(f"POST /services/{domain}/{service} -> {resp.status_code}: {resp.text[:200]}")
