"""Composes the stable core application with optional feature routers."""

import asyncio
import logging
from contextlib import asynccontextmanager

from app import calibration, devices, feature_api, telemetry
from app.feature_api import router as feature_router
from app.main import app

logger = logging.getLogger("echolot.server")

_core_lifespan = app.router.lifespan_context


def _resume_recordings() -> None:
    """Re-attach a sampler to every session still marked as recording.

    An add-on restart — an update, most often — otherwise leaves the
    session in the UI with its live indicator on and nothing behind it.
    """
    for session in calibration.store.list():
        if session["status"] != "recording":
            continue
        device = devices.get_device(session["device_id"])
        if device is not None:
            feature_api.sampler.start(device)


@asynccontextmanager
async def lifespan(application):
    # Recordings are started from plain `def` routes, which FastAPI runs in
    # a worker thread; the sampler needs this loop to schedule onto.
    # Before anything reads the registry: a build or OTA run that was in
    # flight when the add-on stopped has no process behind it any more.
    interrupted = devices.mark_interrupted_jobs()
    if interrupted:
        logger.info("Unterbrochene Jobs zurückgesetzt: %s", ", ".join(interrupted))

    feature_api.live.bind(asyncio.get_running_loop())
    await feature_api.live.run(devices.list_devices)
    telemetry.hub.add_listener(calibration.store.ingest)
    await telemetry.hub.start(devices.list_devices)
    _resume_recordings()
    try:
        async with _core_lifespan(application):
            yield
    finally:
        feature_api.sampler.stop_all()
        feature_api.live.stop_all()
        telemetry.hub.remove_listener(calibration.store.ingest)
        await telemetry.hub.stop()


app.router.lifespan_context = lifespan
app.include_router(feature_router)
