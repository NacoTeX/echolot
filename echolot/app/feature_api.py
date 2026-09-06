"""API routes for direct telemetry and ground-truth calibration.

Kept as a router so feature work does not continually collide with the core
device/zone application module.
"""

import asyncio
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse

from app import calibration, devices, telemetry

router = APIRouter()


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


@router.get("/api/calibrations")
def list_calibrations(device_id: str | None = None) -> list[dict]:
    return calibration.store.list(device_id=device_id)


@router.post("/api/calibrations", status_code=201)
def create_calibration(payload: dict) -> dict:
    device_id = str(payload.get("device_id") or "")
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    if device.status != devices.BuildStatus.SUCCESS or not device.config.direct_api:
        raise HTTPException(
            status_code=409,
            detail="Das Gerät muss gebaut sein und Direkt-Telemetrie aktiviert haben",
        )
    name = str(payload.get("name") or "").strip()
    if len(name) > 100:
        raise HTTPException(status_code=422, detail="Der Name darf höchstens 100 Zeichen haben")
    try:
        return calibration.store.create(device_id, name)
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
        return calibration.store.stop(session_id)
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err


@router.delete("/api/calibrations/{session_id}", status_code=204)
def delete_calibration(session_id: str) -> None:
    if not calibration.store.delete(session_id):
        raise HTTPException(status_code=404, detail="Kalibrierung nicht gefunden")


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
