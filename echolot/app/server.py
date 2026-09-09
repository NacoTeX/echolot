"""Composes the stable core application with optional feature routers."""

import asyncio
import logging
from contextlib import asynccontextmanager

from app import calibration, devices, feature_api, samples, telemetry
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
    samples.bus.bind(asyncio.get_running_loop())
    await feature_api.live.run(devices.list_devices)

    # One registration, on the canonical stream. Calibration used to be
    # attached to both collectors at once, so a device with the Direct
    # HTTP API enabled recorded the same movement twice — and the
    # crossing rate is events per second, so counting it twice does not
    # add detail, it doubles the number.
    samples.bus.add_listener(calibration.store.ingest)
    # Alongside the numbers, what happened between them. A recording that
    # holds only the score series cannot reproduce a motion flip, and
    # replay then runs the same decision function over different inputs.
    samples.bus.add_event_listener(calibration.store.ingest_event)
    if samples.direct_collector_enabled():
        await telemetry.hub.start(devices.list_devices)
    else:
        logger.info(
            "Direkter ESPectre-Kollektor aus: %s", samples.DIRECT_COLLECTOR_REASON
        )
    _resume_recordings()
    try:
        async with _core_lifespan(application):
            yield
    finally:
        feature_api.sampler.stop_all()
        # The writer thread is a daemon; a shutdown must not drop the last
        # couple of seconds of a recording because of it. close() stops it
        # and waits, then writes what is left — so shutdown is a defined
        # point rather than a race with a background thread.
        calibration.store.close()
        feature_api.live.stop_all()
        samples.bus.remove_listener(calibration.store.ingest)
        samples.bus.remove_event_listener(calibration.store.ingest_event)
        await telemetry.hub.stop()


app.router.lifespan_context = lifespan
app.include_router(feature_router)
