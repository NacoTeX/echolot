"""Why the zone is in the state it is in.

The question a presence system gets asked is never "is the room occupied"
— the dot on the tile answers that. It is "why did the light stay on for
another minute", or "why did it go out while I was sitting here", and
until now nothing in Echolot could answer either. The state was a
verdict without a record.

So the evaluator writes down every transition: when it happened, what it
changed from and to, which source caused it (fast motion, the slow
crossing rate, or a hold time running out), and what each member device
was reporting at that moment.

Deliberately in memory and deliberately bounded. This is for looking at
the last hour after something surprised you, not an audit log — and a
disk-backed history of every zone transition would be a second storage
problem on top of the calibration one, for less reason.
"""

import threading
import time
from collections import deque

#: Per zone. At the rate a room actually changes state — a handful of
#: times an hour — this is a day or two of history, and a pathological
#: flapping zone still cannot grow without bound.
MAX_EVENTS_PER_ZONE = 200


def _member_view(members: list[dict]) -> list[dict]:
    """What each device was saying, small enough to keep 200 of."""
    view = []
    for member in members or []:
        view.append(
            {
                "device_id": member.get("device_id"),
                "name": member.get("name"),
                "available": bool(member.get("available")),
                "motion": member.get("motion"),
                "movement_score": member.get("movement_score"),
            }
        )
    return view


class Timeline:
    """Bounded per-zone history of state transitions."""

    def __init__(self, max_events: int = MAX_EVENTS_PER_ZONE) -> None:
        self._events: dict[str, deque] = {}
        self._previous: dict[str, tuple] = {}
        self._max = max_events
        self._lock = threading.Lock()

    def forget(self, zone_id: str) -> None:
        with self._lock:
            self._events.pop(zone_id, None)
            self._previous.pop(zone_id, None)

    def record(self, zone_id: str, zone_name: str, state: dict) -> dict | None:
        """Note a transition, or return None when nothing changed.

        Availability counts as part of the state: a zone going unavailable
        is exactly the kind of thing someone is trying to explain later,
        and it is invisible if only `state` is compared.
        """
        signature = (state.get("state"), bool(state.get("available")))
        with self._lock:
            previous = self._previous.get(zone_id)
            if previous == signature:
                return None
            self._previous[zone_id] = signature

            # The first sighting of a zone is not a transition. Recording
            # it would put a "became clear" in every restart's history
            # that nothing in the room caused.
            if previous is None:
                return None

            event = {
                "t": time.time(),
                "zone_id": zone_id,
                "zone_name": zone_name,
                "from_state": previous[0],
                "to_state": state.get("state"),
                "available": bool(state.get("available")),
                "was_available": previous[1],
                "occupied": bool(state.get("occupied")),
                # Which source is holding it: "motion", "rate", or None
                # when nothing is — see zone_logic.evaluate.
                "trigger": state.get("trigger"),
                "rate_occupied": state.get("rate_occupied"),
                "hold_remaining": state.get("hold_remaining"),
                "score": state.get("score"),
                "members": _member_view(state.get("members")),
            }
            self._events.setdefault(zone_id, deque(maxlen=self._max)).append(event)
            return event

    def events(self, zone_id: str, limit: int = 50) -> list[dict]:
        """Newest first, because that is the end anyone reads from."""
        with self._lock:
            stored = self._events.get(zone_id)
            return list(reversed(stored))[:limit] if stored else []

    def all_events(self, limit: int = 50) -> list[dict]:
        with self._lock:
            everything = [event for stored in self._events.values() for event in stored]
        everything.sort(key=lambda event: event["t"], reverse=True)
        return everything[:limit]


timeline = Timeline()


def explain(event: dict) -> str:
    """One sentence a person can read, in the UI's language."""
    if not event.get("available"):
        return "Keine Messung mehr — Gerät nicht erreichbar"
    if not event.get("was_available"):
        return "Messung wieder da"

    to_state = event.get("to_state")
    if to_state == "detected":
        if event.get("trigger") == "rate":
            return "Belegt über die Rate — jemand sitzt vermutlich still da"
        return "Bewegung erkannt"
    if to_state == "holding":
        return f"Bewegung vorbei, Haltezeit läuft ({event.get('hold_remaining')} s)"
    if to_state == "clear":
        return "Haltezeit abgelaufen, niemand mehr erkannt"
    return f"{event.get('from_state')} → {to_state}"
