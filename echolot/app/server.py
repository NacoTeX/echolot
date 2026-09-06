"""Composes the stable core application with optional feature routers."""

from contextlib import asynccontextmanager

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
