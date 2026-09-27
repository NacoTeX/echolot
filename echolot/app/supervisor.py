"""Where the Supervisor is, and the token that lets this add-on ask it.

Home Assistant hands every add-on a token (SUPERVISOR_TOKEN); tests and
a development server set ECHOLOT_SUPERVISOR_TOKEN and _URL instead.
"""

import os


def access() -> tuple[str, str | None]:
    """(base URL, token); the token is None outside Home Assistant."""
    token = os.environ.get("ECHOLOT_SUPERVISOR_TOKEN") or os.environ.get("SUPERVISOR_TOKEN")
    return os.environ.get("ECHOLOT_SUPERVISOR_URL", "http://supervisor"), token or None
