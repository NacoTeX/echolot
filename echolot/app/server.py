"""The ASGI entry point the add-on starts (`uvicorn app.server:app`).

Kept as its own module so the run script does not have to change when
the application is reorganised behind it.
"""

from app.main import app

__all__ = ["app"]
