"""Subscribe to Home Assistant state changes over its websocket API.

Polling was costing three quarters of the data. A device publishes about
six movement-score updates a second; a sampler polling twice a second and
discarding repeats captured roughly a quarter of them, with gaps up to
thirteen seconds. That is the difference between a rate estimate over a
sixty-second window resting on ninety samples or on three hundred and
sixty, and the rate is what the presence decision now turns on.

A subscription also removes the repeat problem by construction: Home
Assistant sends a message when a value *changes*, so there is nothing to
deduplicate.

`subscribe_trigger` is used rather than `subscribe_events`, so the server
filters by entity. The alternative delivers every state change in the
installation to read three of them — the same trade the state reader in
main.py already makes for the same reason.
"""

import asyncio
import json
import logging
import os

from websockets.asyncio.client import connect

logger = logging.getLogger("echolot.ha_stream")

DEFAULT_WS_URL = "ws://supervisor/core/websocket"
RECONNECT_MAX_SECONDS = 30


def ws_url() -> str:
    return os.environ.get("ECHOLOT_HA_WS_URL", DEFAULT_WS_URL)


def _token() -> str | None:
    return os.environ.get("ECHOLOT_HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN")


class AuthenticationFailed(Exception):
    """Home Assistant rejected the token, so retrying will not help."""


async def _handshake(socket, entity_ids: list[str]) -> None:
    """Authenticate, then ask for state changes on these entities only."""
    hello = json.loads(await socket.recv())
    if hello.get("type") != "auth_required":
        raise AuthenticationFailed(f"Unerwartete Begrüßung: {hello.get('type')!r}")

    token = _token()
    if not token:
        raise AuthenticationFailed("Kein SUPERVISOR_TOKEN verfügbar")
    await socket.send(json.dumps({"type": "auth", "access_token": token}))

    result = json.loads(await socket.recv())
    if result.get("type") != "auth_ok":
        raise AuthenticationFailed(result.get("message") or "Anmeldung abgelehnt")

    await socket.send(json.dumps({
        "id": 1,
        "type": "subscribe_trigger",
        "trigger": {"platform": "state", "entity_id": entity_ids},
    }))
    confirmation = json.loads(await socket.recv())
    if not confirmation.get("success", False):
        raise AuthenticationFailed(
            (confirmation.get("error") or {}).get("message") or "Abonnement abgelehnt"
        )


def state_from_message(message: dict) -> tuple[str, dict] | None:
    """The entity and its new state, or None for anything else on the wire."""
    if message.get("type") != "event":
        return None
    trigger = ((message.get("event") or {}).get("variables") or {}).get("trigger") or {}
    to_state = trigger.get("to_state")
    entity_id = trigger.get("entity_id")
    if not isinstance(to_state, dict) or not isinstance(entity_id, str):
        return None
    return entity_id, to_state


class StateSubscription:
    """One websocket, reconnecting, delivering state changes to a callback."""

    def __init__(self, entity_ids, on_state, *, connector=connect, loop=None) -> None:
        self._entity_ids = [e for e in entity_ids if e]
        self._on_state = on_state
        self._connector = connector
        #: FastAPI runs a plain `def` route in a worker thread, where
        #: asyncio.create_task raises. A recording is started from such a
        #: route, so the loop has to be handed in rather than discovered.
        self._loop = loop
        self._task = None
        #: Why the last attempt failed, for the UI to show.
        self.error: str | None = None
        self.connected = False

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if self._loop is not None:
            self._task = asyncio.run_coroutine_threadsafe(self._run(), self._loop)
        else:
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        self.connected = False

    async def _run(self) -> None:
        delay = 1
        while True:
            try:
                async with self._connector(ws_url()) as socket:
                    await _handshake(socket, self._entity_ids)
                    self.connected = True
                    self.error = None
                    delay = 1
                    async for raw in socket:
                        found = state_from_message(json.loads(raw))
                        if found is not None:
                            self._on_state(*found)
            except asyncio.CancelledError:
                raise
            except AuthenticationFailed as err:
                # A bad token does not get better by trying again, but the
                # add-on should not die over it either: report and back off
                # to the maximum instead of hammering.
                self.connected = False
                self.error = str(err)
                logger.error("Home-Assistant-Abonnement abgelehnt: %s", err)
                delay = RECONNECT_MAX_SECONDS
            except Exception as err:  # noqa: BLE001 - any transport fault reconnects
                self.connected = False
                self.error = f"{type(err).__name__}: {err}"
                logger.warning("Home-Assistant-Abonnement getrennt: %s", self.error)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_SECONDS)
