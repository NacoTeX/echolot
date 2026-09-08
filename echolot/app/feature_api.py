"""API routes for direct telemetry and ground-truth calibration.

Kept as a router so feature work does not continually collide with the core
device/zone application module.
"""

import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse

from app import (
    calibration,
    devices,
    fusion,
    ha_client,
    ha_sampler,
    history_import,
    live_presence,
    presence_rate,
    telemetry,
    zones,
)

router = APIRouter()


@router.get("/api/fusion/zones")
def fused_zones() -> list[dict]:
    profiles = calibration.store.latest_profiles()
    return [
        fusion.evaluate_zone(zone, devices.get_device, telemetry.hub, profiles)
        for zone in zones.list_zones()
    ]


@router.get("/api/fusion/zones/{zone_id}")
def fused_zone(zone_id: str) -> dict:
    zone = zones.get_zone(zone_id)
    if zone is None:
        raise HTTPException(status_code=404, detail="Zone nicht gefunden")
    return fusion.evaluate_zone(
        zone, devices.get_device, telemetry.hub, calibration.store.latest_profiles()
    )


@router.get("/api/devices/{device_id}/telemetry")
def device_telemetry(device_id: str, seconds: int = 1800) -> dict:
    if devices.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    return telemetry.hub.snapshot(device_id, seconds=seconds)


@router.get("/api/devices/{device_id}/telemetry/stream")
async def device_telemetry_stream(device_id: str) -> StreamingResponse:
    if devices.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")

    async def events():
        queue = telemetry.hub.subscribe(device_id)
        try:
            yield ": connected\n\n"
            while True:
                try:
                    sample = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(sample.as_dict(), separators=(',', ':'))}\n\n"
        finally:
            telemetry.hub.unsubscribe(device_id, queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


#: One subscription per device, running whether or not anyone is
#: recording: the crossing rate is measured over the last minute, and
#: there is no last minute unless something has been listening.
live = live_presence.LivePresence()

#: A recording attaches to that stream rather than opening its own. Samples
#: come from Home Assistant, not from the device's Direct API — see
#: app/ha_sampler.py for why that API is out of reach.
sampler = ha_sampler.HomeAssistantSampler(calibration.store.ingest, live)


@router.get("/api/calibrations")
def list_calibrations(device_id: str | None = None) -> list[dict]:
    return calibration.store.list(device_id=device_id)


@router.post("/api/calibrations", status_code=201)
def create_calibration(payload: dict) -> dict:
    device_id = str(payload.get("device_id") or "")
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    if device.status != devices.BuildStatus.SUCCESS:
        raise HTTPException(status_code=409, detail="Die Firmware wurde noch nicht gebaut")
    if not device.entity_movement_score and not device.entity_motion:
        raise HTTPException(
            status_code=409,
            detail=(
                "Für dieses Gerät kennt Echolot keine Entities in Home Assistant. "
                "Auf der Gerätekarte „Entities in Home Assistant suchen“ drücken."
            ),
        )
    name = str(payload.get("name") or "").strip()
    if len(name) > 100:
        raise HTTPException(status_code=422, detail="Der Name darf höchstens 100 Zeichen haben")
    try:
        session = calibration.store.create(device_id, name)
    except ValueError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err

    # A session with no sampler behind it looks live and collects nothing,
    # which is the exact failure a ground-truth recording exists to rule
    # out. Three sessions were once recorded and exported before anyone
    # noticed there had never been any data in them, so a start that did
    # not start is now a refusal rather than a green light.
    if not sampler.start(device):
        calibration.store.delete(session["id"])
        raise HTTPException(
            status_code=409,
            detail=(
                "Für dieses Gerät läuft gerade kein Live-Abonnement, die "
                "Aufzeichnung würde nichts aufnehmen. Ist das Gerät in Home "
                "Assistant verfügbar?"
            ),
        )
    return session


def _moment(payload: dict, key: str) -> datetime:
    """One end of the range, as an aware UTC datetime.

    A browser's `datetime-local` field has no zone, so a bare value is
    read as the add-on's local time — the same clock the person was
    reading when they decided the room was empty.
    """
    raw = str(payload.get(key) or "").strip()
    if not raw:
        raise HTTPException(status_code=422, detail=f"„{key}“ fehlt")
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            status_code=422, detail=f"„{key}“ ist kein gültiger Zeitpunkt"
        ) from None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone(timezone.utc)


@router.post("/api/calibrations/import", status_code=201)
async def import_calibration(payload: dict) -> dict:
    """Turn a past stretch of Home Assistant history into a session.

    The room being empty is the measurement the rate detector actually
    needs, and it is also the one nobody wants to sit through. The
    recorder has already made it.
    """
    device = devices.get_device(str(payload.get("device_id") or ""))
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    if not device.entity_movement_score:
        raise HTTPException(
            status_code=409,
            detail=(
                "Für dieses Gerät kennt Echolot keinen Bewegungswert in Home "
                "Assistant. Auf der Gerätekarte „Entities in Home Assistant "
                "suchen“ drücken."
            ),
        )

    label = str(payload.get("label") or "empty")
    start = _moment(payload, "start")
    end = _moment(payload, "end")
    try:
        history_import.check_range(start, end)
    except history_import.RangeRejected as err:
        raise HTTPException(status_code=422, detail=str(err)) from err

    try:
        samples = await history_import.fetch(device, start, end)
    except ha_client.HomeAssistantUnavailable as err:
        raise HTTPException(
            status_code=502, detail=f"Home Assistant antwortet nicht: {err}"
        ) from err

    try:
        return calibration.store.adopt(
            device.id, samples, label=label, name=str(payload.get("name") or "")
        )
    except ValueError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err


@router.post("/api/calibrations/{session_id}/label")
def label_calibration(session_id: str, payload: dict) -> dict:
    try:
        return calibration.store.set_label(session_id, str(payload.get("label") or ""))
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    except ValueError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err


@router.post("/api/calibrations/{session_id}/stop")
def stop_calibration(session_id: str) -> dict:
    try:
        session = calibration.store.stop(session_id)
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    _stop_sampling_if_idle(session["device_id"])
    return session


@router.delete("/api/calibrations/{session_id}", status_code=204)
def delete_calibration(session_id: str) -> None:
    session = calibration.store.get(session_id)
    if not calibration.store.delete(session_id):
        raise HTTPException(status_code=404, detail="Kalibrierung nicht gefunden")
    if session is not None:
        _stop_sampling_if_idle(session["device_id"])


def _stop_sampling_if_idle(device_id: str) -> None:
    """Keep polling only while this device still has a recording running.

    Deleting one of two concurrent sessions must not silence the other.
    """
    still_recording = any(
        s["status"] == "recording" for s in calibration.store.list(device_id=device_id)
    )
    if not still_recording:
        sampler.stop(device_id)


@router.get("/api/calibrations/{session_id}/presence-rate")
def calibration_presence_rate(session_id: str) -> dict:
    """Learn this session's empty-room rate and score its other labels by it.

    The analysis that produced app/presence_rate.py, done by the product
    instead of by hand: what does this room do when empty, and does the
    crossing rate over a minute separate that from the labels where
    somebody was in it.
    """
    samples = calibration.store.samples(session_id)
    if samples is None:
        raise HTTPException(status_code=404, detail="Kalibrierung nicht gefunden")

    profile = presence_rate.learn_baseline(samples)
    if profile is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Zu wenig mit „Raum leer“ markiertes Material. Der Maßstab ist, "
                f"was der leere Raum schlimmstenfalls tut, und dafür braucht es "
                f"mindestens {presence_rate.MIN_BASELINE_WINDOWS} volle Fenster "
                f"à {int(presence_rate.DEFAULT_WINDOW_SECONDS)} s — also gut "
                f"{presence_rate.MIN_BASELINE_WINDOWS * int(presence_rate.DEFAULT_WINDOW_SECONDS) // 60} "
                "Minuten. Kürzer gemessen landet die Kennzahl auf dem lautesten "
                "Fenster statt neben ihm."
            ),
        )

    by_label: dict[str, list[dict]] = {}
    for row in samples:
        by_label.setdefault(row.get("label") or "unlabelled", []).append(row)

    labels = {}
    for label, rows in by_label.items():
        windows = presence_rate._split_windows(rows, profile.window_seconds)
        verdicts = [presence_rate.evaluate(profile, window) for window in windows]
        judged = [v for v in verdicts if v["available"]]
        labels[label] = {
            "windows": len(judged),
            "occupied_windows": sum(1 for v in judged if v["occupied"]),
            "detail": judged,
        }

    return {"profile": profile.as_dict(), "labels": labels}


@router.post("/api/calibrations/{session_id}/apply")
def apply_presence_rate(session_id: str) -> dict:
    """Adopt this session's empty-room rate as the device's baseline.

    Until a device has one, it contributes nothing to rate-based presence:
    without knowing what the room does empty there is no "well above" to
    measure against.
    """
    samples = calibration.store.samples(session_id)
    session = calibration.store.get(session_id)
    if samples is None or session is None:
        raise HTTPException(status_code=404, detail="Kalibrierung nicht gefunden")

    profile = presence_rate.learn_baseline(samples)
    if profile is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Zu wenig mit „Raum leer“ markiertes Material, um daraus einen "
                "Maßstab zu machen."
            ),
        )

    device = devices.get_device(session["device_id"])
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    device.presence_profile = profile.as_dict()
    devices.save_device(device)
    return {"status": "ok", "profile": device.presence_profile}


@router.get("/api/calibrations/{session_id}/export.csv")
def export_calibration(session_id: str) -> Response:
    try:
        content = calibration.store.csv(session_id)
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="echolot-{session_id}.csv"'},
    )
