"""The room card for Home Assistant dashboards.

static/echolot-room-card.js is a Lovelace card. Home Assistant lets the
browser reach an add-on only through Ingress, and Ingress only with a
session an administrator's browser asks the Supervisor for — the card
does that, as Home Assistant's own add-on panel does, and shows the
page `embed?room=<id>` of this add-on (static/embed.html) in a frame.
Everything the card shows is drawn by that page, so updating the add-on
updates the card; the file in the dashboard only opens the door.

To reach the add-on, the card needs its slug; the add-on asks the
Supervisor for it (own_slug) so the room page can hand out the card's
configuration ready to paste.
"""

import os

import httpx

CARD_FILE = "echolot-room-card.js"
#: Where Home Assistant serves a file put in /config/www.
CARD_RESOURCE = f"/local/{CARD_FILE}"
#: Goes up when the card file itself changes, not with every add-on
#: release (see the module docstring); the file carries the same number
#: (tests/test_dashboard.py holds them together).
CARD_VERSION = 1

_slug: str | None = None


async def own_slug() -> str | None:
    """This add-on's slug, as the Supervisor knows it ("local_echolot",
    or a repository prefix and "_echolot"). None outside Home Assistant
    or while the Supervisor does not answer; remembered once known."""
    global _slug
    if _slug:
        return _slug
    token = os.environ.get("ECHOLOT_SUPERVISOR_TOKEN") or os.environ.get("SUPERVISOR_TOKEN")
    base = os.environ.get("ECHOLOT_SUPERVISOR_URL", "http://supervisor")
    if not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base}/addons/self/info", headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    slug = (resp.json().get("data") or {}).get("slug")
    if isinstance(slug, str) and slug:
        _slug = slug
    return _slug
