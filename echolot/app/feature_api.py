"""API routes for direct telemetry and ground-truth calibration.

Kept as a router so feature work does not continually collide with the core
device/zone application module.
"""

import asyncio
import json
from dataclasses import replace
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
    replay,
    samples,
    telemetry,
    zones,
)

router = APIRouter()


@router.get("/api/fusion/zones")
def fused_zones() -> list[dict]:
    profiles = calibration.store.latest_profiles()
    return [
        fusion.evaluate_zone(zone, devices.get_device, samples.bus, profiles)
        for zone in zones.list_zones()
    ]


@router.get("/api/fusion/zones/{zone_id}")
def fused_zone(zone_id: str) -> dict:
    zone = zones.get_zone(zone_id)
    if zone is None:
        raise HTTPException(status_code=404, detail="Zone nicht gefunden")
    return fusion.evaluate_zone(
        zone, devices.get_device, samples.bus, calibration.store.latest_profiles()
    )


@router.get("/api/devices/{device_id}/telemetry")
def device_telemetry(device_id: str, seconds: int = 1800) -> dict:
    """The canonical readings, whichever transport produced them.

    It used to be the direct collector's own buffer, so a device without
    ESPectre's Direct HTTP API showed an empty trace and an error — while
    Home Assistant was delivering its readings the whole time.
    """
    if devices.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    return {
        **samples.bus.snapshot(device_id, seconds=seconds),
        # The direct collector's connection state, whether or not it is
        # the source: "why is there no direct data" is a real question.
        "direct": telemetry.hub.status(device_id),
    }


@router.get("/api/devices/{device_id}/telemetry/stream")
async def device_telemetry_stream(device_id: str) -> StreamingResponse:
    if devices.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")

    async def events():
        queue = samples.bus.subscribe(device_id)
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
            samples.bus.unsubscribe(device_id, queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


#: One subscription per device, running whether or not anyone is
#: recording: the crossing rate is measured over the last minute, and
#: there is no last minute unless something has been listening.
live = live_presence.LivePresence()

#: Recordings are fed from the canonical sample bus (app/samples.py), not
#: from here. This is what answers "is there anything to record from" —
#: see app/ha_sampler.py, and for why the device's own Direct API is out
#: of reach, the module docstring there.
sampler = ha_sampler.HomeAssistantSampler(live)


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
        windows = presence_rate.split_windows(rows, profile.window_seconds)
        verdicts = [presence_rate.evaluate(profile, w.samples, window=w) for w in windows]
        judged = [v for v in verdicts if v["available"]]
        labels[label] = {
            "windows": len(judged),
            # Windows that elapsed but had no source behind them, or never
            # elapsed at all. Reported rather than dropped: a label whose
            # windows are mostly unusable is a fact about the recording.
            "unusable_windows": len(verdicts) - len(judged),
            "occupied_windows": sum(1 for v in judged if v["occupied"]),
            "detail": judged,
        }

    return {"profile": profile.as_dict(), "labels": labels}


@router.get("/api/calibrations/{session_id}/replay")
def replay_calibration(
    session_id: str,
    baseline: str | None = None,
    hold_seconds: float = 0.0,
    transfer: bool = False,
) -> dict:
    """Replay a session and report what the add-on would have decided.

    Read-only: it touches no zone runtime, no device profile and not the
    live evaluator, so asking cannot change what the lights do. That is
    the point — the review asks for new algorithms to be measured before
    they are wired to anything.

    Two runs over the same material. `window_comparison` groups by label
    and scores each label's windows on their own; `simulation` runs the
    recording forward through the same functions the live path takes,
    with hysteresis and hold time in place.

    `baseline` names another session to learn the empty-room rate from.
    Without it the rate is learned from the material being judged, which
    measures itself; so does a baseline that is this session, or one whose
    recording overlaps it in time. The response says which. A baseline
    from another device is refused unless `transfer=true` says the
    comparison is meant to be a transfer test.
    """
    session = calibration.store.get(session_id)
    samples = calibration.store.samples(session_id)
    if samples is None or session is None:
        raise HTTPException(status_code=404, detail="Kalibrierung nicht gefunden")

    baseline_session = baseline_samples = None
    if baseline:
        baseline_session = calibration.store.get(baseline)
        baseline_samples = calibration.store.samples(baseline)
        if baseline_samples is None or baseline_session is None:
            raise HTTPException(
                status_code=404, detail="Die Maßstab-Sitzung gibt es nicht"
            )
    try:
        return replay.report(
            session,
            samples,
            baseline=baseline_session,
            baseline_samples=baseline_samples,
            transfer=transfer,
            hold_seconds=max(0.0, hold_seconds),
            events=calibration.store.events(session_id) or [],
        )
    except replay.BaselineRefused as err:
        raise HTTPException(status_code=409, detail=str(err)) from err


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
    # Which radio this was measured on. It comes from the device rather
    # than from the readings, because a reading does not carry its band —
    # a weaker claim than `source`, and the reason `auto` is recorded as
    # its own answer instead of being resolved to a band. Without it a
    # baseline learned on 2.4 GHz would keep judging a device somebody
    # later moved to 5 GHz, which is the transport mismatch of 0.13.7 one
    # layer down.
    profile = replace(
        profile,
        band=devices.effective_band(device.config),
        # And the radio path it was measured along. Only `router` exists
        # today, so this records a fact rather than distinguishing two —
        # which is the point: when a peer link does exist, a baseline
        # learned against the access point will not silently become its
        # scale.
        sensing_mode=device.config.sensing_mode,
    )
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
