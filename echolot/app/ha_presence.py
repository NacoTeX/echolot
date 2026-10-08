"""Whether anybody is home, as Home Assistant knows it.

Learning (app/learner.py) asks this one question: is the house empty?
While it is, and nothing walks anywhere, whatever stands in a room is no
person — a radiator, a mirror, a plant in the draught. Home Assistant
knows from its `person` entities (phones, routers, door locks) — or from
one entity the user names instead, a group or a helper that is on while
somebody is home.

Asked through the Supervisor's proxy to Home Assistant's API
(`homeassistant_api: true` in config.yaml). Without it, or without any
person, the answer is "not known", and nothing is learned that needs it.
"""

import asyncio
import logging
import time
from datetime import datetime

import httpx

from app import supervisor

logger = logging.getLogger("echolot.presence")

#: How often Home Assistant is asked.
POLL_S = 30.0
#: States of an entity that mean somebody is home, and nobody is.
HOME_STATES = {"home", "on", "true", "anwesend"}
AWAY_STATES = {"not_home", "off", "false", "away", "abwesend"}
#: States that say nothing.
UNKNOWN_STATES = {"unknown", "unavailable", "", "none"}


def _since(state: dict) -> float | None:
    try:
        return datetime.fromisoformat(str(state.get("last_changed"))).timestamp()
    except (TypeError, ValueError):
        return None


def whereabouts(states: list[dict], entity: str | None = None) -> dict:
    """What a list of Home Assistant states says: {"home": True | False |
    None, "since": when the house became empty (seconds) or None,
    "persons": n, "persons_home": n, "source": what was asked}.

    `entity` given: that entity alone — home while it is "home", "on" or a
    number above 0 (a zone's count of persons), empty while it is
    "not_home", "off" or 0. Else every `person`: home while anybody is,
    empty once everybody is somewhere else — and not known while anybody's
    whereabouts are.
    """
    if entity:
        state = next((s for s in states if s.get("entity_id") == entity), None)
        out = {"home": None, "since": None, "persons": None, "persons_home": None, "source": entity}
        if state is None:
            return {**out, "error": "missing"}
        value = str(state.get("state", "")).strip().lower()
        home = None
        if value in HOME_STATES:
            home = True
        elif value in AWAY_STATES:
            home = False
        else:
            try:
                home = float(value) > 0
            except ValueError:
                home = None
        return {**out, "home": home, "since": _since(state) if home is False else None}
    persons = [s for s in states if str(s.get("entity_id", "")).startswith("person.")]
    values = [str(s.get("state", "")).strip().lower() for s in persons]
    at_home = sum(1 for v in values if v == "home")
    out = {"persons": len(persons), "persons_home": at_home, "source": "person"}
    if not persons:
        return {**out, "home": None, "since": None}
    if at_home:
        return {**out, "home": True, "since": None}
    if any(v in UNKNOWN_STATES for v in values):
        return {**out, "home": None, "since": None}
    times = [t for t in (_since(s) for s in persons) if t is not None]
    return {**out, "home": False, "since": max(times) if times else None}


class HomePresence:
    """Polls Home Assistant; `home` and `away_for` are what learning reads."""

    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        #: The entity to ask instead of the persons; None for the persons.
        self.entity: str | None = None
        self.state: dict = {"home": None, "since": None, "persons": None, "persons_home": None, "source": None}
        self.error: str | None = None
        self.checked_at: float | None = None
        #: When this add-on first saw the house empty, for an empty house
        #: Home Assistant gives no time for.
        self._empty_seen: float | None = None
        self._task: asyncio.Task | None = None

    @property
    def home(self) -> bool | None:
        return self.state.get("home")

    def away_for(self, now: float | None = None) -> float | None:
        """Seconds the house has been empty; None while somebody is home or
        nobody knows."""
        if self.home is not False:
            return None
        now = self._clock() if now is None else now
        since = self.state.get("since") or self._empty_seen
        return max(0.0, now - since) if since is not None else None

    def take(self, states: list[dict]) -> None:
        """Take what Home Assistant said."""
        found = whereabouts(states, self.entity)
        now = self._clock()
        if found.get("home") is False:
            self._empty_seen = self._empty_seen or now
        else:
            self._empty_seen = None
        self.error = {"missing": f"„{self.entity}“ gibt es in Home Assistant nicht"}.get(found.pop("error", None))
        self.state = found
        self.checked_at = now

    def lost(self, reason: str) -> None:
        self.state = {**self.state, "home": None, "since": None}
        self._empty_seen = None
        self.error = reason

    def view(self) -> dict:
        away = self.away_for()
        return {**self.state, "away_s": None if away is None else round(away), "error": self.error,
                "checked_at": self.checked_at}

    async def poll(self) -> None:
        base, token = supervisor.access()
        if not token:
            self.lost("Nur als Home-Assistant-Add-on verfügbar")
            return
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{base}/core/api/states", headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as err:
            self.lost(f"Home Assistant nicht erreichbar: {err.__class__.__name__}")
            return
        if resp.status_code in (401, 403):
            self.lost("Echolot darf Home Assistant nicht fragen (homeassistant_api)")
            return
        if resp.status_code != 200:
            self.lost(f"Home Assistant antwortet mit HTTP {resp.status_code}")
            return
        try:
            states = resp.json()
        except ValueError:
            self.lost("Home Assistant lieferte keine lesbare Antwort")
            return
        self.take(states if isinstance(states, list) else [])

    async def _loop(self) -> None:
        while True:
            try:
                await self.poll()
            except Exception:  # noqa: BLE001 - presence must never stop the add-on
                logger.exception("Anwesenheit konnte nicht abgefragt werden")
                self.lost("Abfrage fehlgeschlagen")
            await asyncio.sleep(POLL_S)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
