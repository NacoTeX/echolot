"""Composes the stable core application with optional feature routers."""

from contextlib import asynccontextmanager

from fastapi import Request
from fastapi.responses import Response

from app import calibration, devices, telemetry
from app.feature_api import router as feature_router
from app.main import app

_core_lifespan = app.router.lifespan_context


@asynccontextmanager
async def lifespan(application):
    telemetry.hub.add_listener(calibration.store.ingest)
    await telemetry.hub.start(devices.list_devices)
    try:
        async with _core_lifespan(application):
            yield
    finally:
        telemetry.hub.remove_listener(calibration.store.ingest)
        await telemetry.hub.stop()


app.router.lifespan_context = lifespan
app.include_router(feature_router)


@app.middleware("http")
async def feature_assets(request: Request, call_next):
    """Add feature scripts without modifying the frequently edited core HTML."""
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if request.url.path != "/" or "text/html" not in content_type:
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])
    marker = b"</body>"
    if marker not in body:
        return Response(body, status_code=response.status_code, headers=dict(response.headers))
    assets = (
        b'<script src="static/dashboard_live.js"></script>'
        b'<script src="static/calibration.js"></script>'
    )
    headers = {key: value for key, value in response.headers.items() if key.lower() != "content-length"}
    return Response(
        body.replace(marker, assets + marker, 1),
        status_code=response.status_code,
        headers=headers,
    )
