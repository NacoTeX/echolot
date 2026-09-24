"""Echolot backend.

Builds radar firmware (ESP32 + HLK-LD2460) and serves it to the browser
flasher, holds a live link to every node, places each one in a room with
zones, and publishes rooms and zones to Home Assistant over MQTT.
"""

import asyncio
import logging
import math
import os
import re
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from app import alignment, builder, devices, mqtt_bridge, reachability, rooms
from app.board_registry import BOARDS
from app.calibration import Captures
from app.radar_link import links
from app.room_engine import RoomEngine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("echolot")

STATIC_DIR = Path(__file__).parent / "static"

engine = RoomEngine(links)
links.add_listener(engine.wake)
captures = Captures(links)
links.add_listener(captures.on_frame)


def mqtt_wanted() -> bool:
    return os.environ.get("ECHOLOT_MQTT_EXPORT", "true").lower() not in ("0", "false", "no")


async def refresh() -> None:
    """Bring links and engine in line with what is stored.

    Called after every write to devices or rooms. Cheap — two small JSON
    files — and it keeps one rule: what the engine evaluates is what is
    on disk, never a copy a route happened to hold.
    """
    device_list = devices.list_devices()
    await links.sync(device_list)
    engine.load(rooms.list_rooms(), device_list)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    interrupted = devices.mark_interrupted_jobs()
    if interrupted:
        logger.info("Unterbrochene Jobs zurückgesetzt: %s", ", ".join(interrupted))
    await refresh()
    engine.start()

    task = None
    if mqtt_wanted():
        task = asyncio.create_task(_run_mqtt_export())
    else:
        logger.info("MQTT export disabled by configuration")
    try:
        yield
    finally:
        await engine.stop()
        await links.stop_all()
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        mqtt_bridge.bridge.stop()


#: How long to wait before asking the Supervisor about MQTT again, in
#: seconds, the last value repeating. Starting before the broker is a
#: plausible order on a rebooting machine.
MQTT_RETRY_BACKOFF = (10, 30, 60, 300)


async def _run_mqtt_export() -> None:
    attempt = 0
    while True:
        try:
            await mqtt_bridge.bridge.start()
        except mqtt_bridge.MqttUnavailable as err:
            delay = MQTT_RETRY_BACKOFF[min(attempt, len(MQTT_RETRY_BACKOFF) - 1)]
            attempt += 1
            logger.info("MQTT export inactive: %s — neuer Versuch in %ss", err, delay)
            mqtt_bridge.bridge.error = str(err)
            await asyncio.sleep(delay)
            continue
        engine.add_listener(mqtt_bridge.RoomPublisher())
        return


app = FastAPI(title="Echolot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Builds run for minutes in a background task; asyncio only keeps a weak
# reference to a task, so without this one could be collected mid-build.
_background_builds: set[asyncio.Task] = set()


def _validation_detail(err: ValidationError) -> list[dict]:
    # err.errors() can carry raw exception objects in "ctx", which
    # json.dumps can't serialize. Keep only what the UI uses.
    return [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in err.errors()]


def _device_or_404(device_id: str) -> devices.Device:
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    return device


# --- add-on ----------------------------------------------------------------


@app.get("/api/health")
def api_liveness() -> dict:
    return {"status": "ok"}


_esphome_version_cache: dict | None = None


def _esphome_version() -> dict:
    """Whether the bundled ESPHome CLI is usable, cached once it is."""
    global _esphome_version_cache
    if _esphome_version_cache is not None:
        return _esphome_version_cache
    try:
        result = subprocess.run(
            ["esphome", "version"], capture_output=True, text=True, timeout=15, check=True
        )
        _esphome_version_cache = {"available": True, "version": result.stdout.strip()}
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as err:
        logger.warning("esphome CLI check failed: %s", err)
        return {"available": False, "error": str(err)}
    return _esphome_version_cache


@app.get("/api/esphome/version")
def esphome_version() -> dict:
    return _esphome_version()


@app.get("/api/info")
def api_info() -> dict:
    return {
        "version": builder.addon_version(),
        "esphome": _esphome_version(),
        "mqtt": {**mqtt_bridge.bridge.status(), "wanted": mqtt_wanted()},
        "legacy_devices": len(devices.list_legacy_devices()),
    }


@app.get("/api/mqtt/status")
def api_mqtt_status() -> dict:
    return mqtt_bridge.bridge.status()


@app.get("/api/boards")
def list_boards() -> list[dict]:
    return [
        {
            "key": b.key,
            "label": b.label,
            "chip_family": b.chip_family,
            "dual_band": b.dual_band,
            "radar_uart_pins": list(b.radar_uart_pins) if b.radar_uart_pins else None,
        }
        for b in BOARDS.values()
    ]


# --- devices ---------------------------------------------------------------


def _device_view(device: devices.Device) -> dict:
    data = device.public()
    snap = links.snapshot(device.id)
    data["link"] = snap.as_dict() if snap else None
    room = rooms.room_for_device(device.id)
    data["room"] = {"id": room.id, "name": room.name} if room else None
    return data


@app.get("/api/devices")
def api_list_devices() -> list[dict]:
    return [_device_view(d) for d in devices.list_devices()]


@app.post("/api/devices", status_code=201)
async def api_create_device(payload: dict) -> dict:
    try:
        config = devices.DeviceCreate.model_validate(payload)
    except ValidationError as err:
        raise HTTPException(status_code=422, detail=_validation_detail(err)) from err
    device = devices.create_device(config)
    await refresh()
    return _device_view(device)


@app.get("/api/devices/{device_id}")
def api_get_device(device_id: str) -> dict:
    return _device_view(_device_or_404(device_id))


@app.patch("/api/devices/{device_id}")
async def api_update_device(device_id: str, payload: dict) -> dict:
    device = _device_or_404(device_id)
    try:
        patch = devices.DeviceUpdate.model_validate(payload)
    except ValidationError as err:
        raise HTTPException(status_code=422, detail=_validation_detail(err)) from err
    device.apply_update(patch)
    devices.save_device(device)
    await refresh()
    return _device_view(device)


@app.patch("/api/devices/{device_id}/config")
async def api_reconfigure_device(device_id: str, payload: dict) -> dict:
    """Change firmware options; `firmware_behind_config` says a flash is due."""
    try:
        device = devices.reconfigure(device_id, payload)
    except ValidationError as err:
        raise HTTPException(status_code=422, detail=_validation_detail(err)) from err
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    await refresh()
    return _device_view(device)


@app.delete("/api/devices/{device_id}", status_code=204)
async def api_delete_device(device_id: str) -> None:
    if not devices.delete_device(device_id):
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    rooms.release_device(device_id)
    await refresh()


@app.get("/api/devices/{device_id}/credentials")
def api_device_credentials(device_id: str) -> dict:
    """The device's secrets, one device at a time and on purpose."""
    return _device_or_404(device_id).credentials()


@app.get("/api/devices/{device_id}/reachability")
async def api_reachability(device_id: str, host: str | None = None) -> dict:
    device = _device_or_404(device_id)
    target = (host or device.ota_address()).strip()
    if not target:
        raise HTTPException(status_code=422, detail="Keine Adresse angegeben")
    result = await reachability.check(target)
    return {**result, "message": reachability.explain(result)}


@app.post("/api/devices/{device_id}/ota", status_code=202)
async def api_start_ota(device_id: str, payload: dict | None = None) -> dict:
    """Push the built firmware to the running device over the network."""
    device = _device_or_404(device_id)
    if device.status != devices.BuildStatus.SUCCESS or not device.firmware_bin:
        raise HTTPException(status_code=409, detail="Die Firmware wurde noch nicht gebaut")
    address = ((payload or {}).get("address") or device.ota_address()).strip()
    if not address:
        raise HTTPException(status_code=422, detail="Keine Adresse angegeben")
    # One lock covers builds and OTA alike: both write the build directory.
    if not builder.try_start_build(device_id):
        raise HTTPException(status_code=409, detail="Für dieses Gerät läuft bereits ein Build oder Update")
    if address != device.address:
        device.address = address
    device.ota_status = devices.BuildStatus.QUEUED
    devices.save_device(device)
    task = asyncio.create_task(asyncio.to_thread(builder.run_ota, device, address))
    _background_builds.add(task)
    task.add_done_callback(_background_builds.discard)
    return {"status": "queued", "address": address}


@app.get("/api/devices/{device_id}/toolchain")
def api_toolchain_state(device_id: str) -> dict:
    device = _device_or_404(device_id)
    board = BOARDS.get(device.config.board)
    if board is None:
        raise HTTPException(status_code=422, detail=f"Unbekanntes Board '{device.config.board}'")
    return {"board": board.key, "label": board.label, **builder.toolchain_state(board)}


@app.post("/api/devices/{device_id}/toolchain/reset")
def api_reset_toolchain(device_id: str) -> dict:
    device = _device_or_404(device_id)
    board = BOARDS.get(device.config.board)
    if board is None:
        raise HTTPException(status_code=422, detail=f"Unbekanntes Board '{device.config.board}'")
    if device.status in (devices.BuildStatus.QUEUED, devices.BuildStatus.RUNNING):
        raise HTTPException(status_code=409, detail="Für dieses Gerät läuft gerade ein Build — warte, bis er beendet ist")
    removed = builder.reset_toolchain(board)
    return {"removed": removed, **builder.toolchain_state(board)}


@app.post("/api/devices/{device_id}/build", status_code=202)
async def api_build_device(device_id: str) -> dict:
    device = _device_or_404(device_id)
    if not builder.try_start_build(device_id):
        raise HTTPException(status_code=409, detail="Für dieses Gerät läuft bereits ein Build")
    device.status = devices.BuildStatus.QUEUED
    devices.save_device(device)
    task = asyncio.create_task(asyncio.to_thread(builder.run_build, device))
    _background_builds.add(task)
    task.add_done_callback(_background_builds.discard)
    return {"status": "queued"}


@app.get("/api/devices/{device_id}/manifest.json")
def api_device_manifest(device_id: str) -> JSONResponse:
    device = _device_or_404(device_id)
    if device.status != devices.BuildStatus.SUCCESS or not device.firmware_bin:
        raise HTTPException(status_code=409, detail="Die Firmware wurde noch nicht gebaut")
    manifest = {
        "name": f"Echolot – {device.display_name()}",
        "version": str(int(device.updated_at)),
        "new_install_prompt_erase": True,
        "builds": [{"chipFamily": device.chip_family, "parts": [{"path": "firmware.bin", "offset": 0}]}],
    }
    # Neither the manifest nor its relative firmware URL may be reused
    # after a rebuild; ESP Web Tools cannot tell a stale response apart.
    return JSONResponse(manifest, headers={"Cache-Control": "no-store"})


# HEAD as well as GET: other flashers ask for the size before fetching.
@app.api_route("/api/devices/{device_id}/firmware.bin", methods=["GET", "HEAD"])
def api_device_firmware(device_id: str) -> Response:
    device = _device_or_404(device_id)
    if device.status != devices.BuildStatus.SUCCESS or not device.firmware_bin:
        raise HTTPException(status_code=409, detail="Die Firmware wurde noch nicht gebaut")
    path = devices.device_dir(device_id) / device.firmware_bin
    if not path.exists():
        raise HTTPException(status_code=404, detail="Firmware-Datei fehlt auf der Festplatte")
    # A finite response rather than a stream: the Ingress proxy has to
    # relay it to ESP Web Tools' fetch(), and a stream left open by either
    # hop leaves its dialog at "Preparing installation".
    try:
        content = path.read_bytes()
    except OSError as err:
        logger.exception("Could not read firmware for device %s", device_id)
        raise HTTPException(status_code=500, detail="Firmware-Datei konnte nicht gelesen werden") from err
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store", "Content-Disposition": 'inline; filename="firmware.bin"'},
    )


# --- Wi-Fi CSI devices from before 1.0 ------------------------------------


@app.get("/api/legacy-devices")
def api_list_legacy_devices() -> list[dict]:
    return devices.list_legacy_devices()


@app.post("/api/legacy-devices/{device_id}/convert")
async def api_convert_legacy_device(device_id: str, payload: dict | None = None) -> dict:
    """Make a CSI device a radar node under the same id and credentials."""
    try:
        device = devices.convert_legacy(device_id, payload or {})
    except ValidationError as err:
        raise HTTPException(status_code=422, detail=_validation_detail(err)) from err
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    if device is None:
        raise HTTPException(status_code=404, detail="Kein CSI-Gerät mit dieser Kennung")
    await refresh()
    return _device_view(device)


@app.delete("/api/legacy-devices/{device_id}", status_code=204)
def api_delete_legacy_device(device_id: str) -> None:
    if not devices.delete_legacy_device(device_id):
        raise HTTPException(status_code=404, detail="Kein CSI-Gerät mit dieser Kennung")


# --- rooms -----------------------------------------------------------------


def _room_or_404(room_id: str) -> rooms.Room:
    room = rooms.get_room(room_id)
    if room is None:
        raise HTTPException(status_code=404, detail="Raum nicht gefunden")
    return room


def _room_view(room: rooms.Room) -> dict:
    data = room.model_dump()
    if room.image:
        data["image"] = {
            "url": f"api/rooms/{room.id}/image?v={room.image.get('version', 0)}",
            "opacity": room.image.get("opacity", 0.6),
        }
    return data


@app.get("/api/rooms")
def api_list_rooms() -> list[dict]:
    return [_room_view(r) for r in rooms.list_rooms()]


@app.post("/api/rooms", status_code=201)
async def api_create_room(payload: dict) -> dict:
    try:
        data = rooms.RoomCreate.model_validate(payload)
        if data.device_id and devices.get_device(data.device_id) is None:
            raise ValueError("Diesen Sensor gibt es nicht")
        room = rooms.create_room(data)
    except ValidationError as err:
        raise HTTPException(status_code=422, detail=_validation_detail(err)) from err
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    await refresh()
    return _room_view(room)


@app.get("/api/rooms/{room_id}")
def api_get_room(room_id: str) -> dict:
    return _room_view(_room_or_404(room_id))


@app.put("/api/rooms/{room_id}")
async def api_save_room(room_id: str, payload: dict) -> dict:
    device_id = (payload.get("sensor") or {}).get("device_id")
    if device_id and devices.get_device(device_id) is None:
        raise HTTPException(status_code=422, detail="Diesen Sensor gibt es nicht")
    try:
        room = rooms.save_room(room_id, payload)
    except rooms.RevisionConflict as conflict:
        raise HTTPException(
            status_code=409,
            detail={"message": str(conflict), "room": _room_view(conflict.current)},
        ) from conflict
    except ValidationError as err:
        raise HTTPException(status_code=422, detail=_validation_detail(err)) from err
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    if room is None:
        raise HTTPException(status_code=404, detail="Raum nicht gefunden")
    await refresh()
    return _room_view(room)


@app.delete("/api/rooms/{room_id}", status_code=204)
async def api_delete_room(room_id: str) -> None:
    if not rooms.delete_room(room_id):
        raise HTTPException(status_code=404, detail="Raum nicht gefunden")
    captures.forget_room(room_id)
    await refresh()


@app.put("/api/rooms/{room_id}/image")
async def api_upload_room_image(room_id: str, request: Request) -> dict:
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    body = await request.body()
    try:
        room = rooms.set_image(room_id, body, content_type)
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    if room is None:
        raise HTTPException(status_code=404, detail="Raum nicht gefunden")
    await refresh()
    return _room_view(room)


@app.delete("/api/rooms/{room_id}/image")
async def api_delete_room_image(room_id: str) -> dict:
    room = rooms.clear_image(room_id)
    if room is None:
        raise HTTPException(status_code=404, detail="Raum nicht gefunden")
    await refresh()
    return _room_view(room)


@app.get("/api/rooms/{room_id}/image")
def api_room_image(room_id: str) -> Response:
    room = _room_or_404(room_id)
    path = rooms.image_path(room)
    if path is None:
        raise HTTPException(status_code=404, detail="Kein Grundriss hinterlegt")
    # Read whole, like the firmware: a finite body with a length is what
    # the Ingress proxy relays reliably. The URL carries a version, so the
    # browser may keep it.
    return Response(
        content=path.read_bytes(),
        media_type=room.image.get("content_type"),
        headers={"Cache-Control": "private, max-age=31536000"},
    )


# --- calibration -----------------------------------------------------------


# The recording routes are async on purpose: they then run on the event
# loop, where the radar link hands frames to the recording, rather than in
# a worker thread next to it.


@app.post("/api/rooms/{room_id}/capture", status_code=201)
async def api_start_capture(room_id: str, payload: dict) -> dict:
    """Start listening: `kind` "empty" (learn reflections) or "point"."""
    room = _room_or_404(room_id)
    if room.sensor.device_id and devices.get_device(room.sensor.device_id) is None:
        raise HTTPException(status_code=422, detail="Der zugeordnete Sensor existiert nicht mehr")
    try:
        capture = captures.start(
            room, str(payload.get("kind")),
            delay_s=payload.get("delay_s"), duration_s=payload.get("duration_s"),
        )
    except (TypeError, ValueError) as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return captures.view(capture)


@app.get("/api/rooms/{room_id}/capture", response_model=None)
async def api_get_capture(room_id: str) -> dict | Response:
    """The running or finished recording; 204 when there is none."""
    _room_or_404(room_id)
    capture = captures.get(room_id)
    if capture is None:
        return Response(status_code=204)
    return captures.view(capture)


@app.delete("/api/rooms/{room_id}/capture", status_code=204)
async def api_cancel_capture(room_id: str) -> None:
    if not captures.cancel(room_id):
        raise HTTPException(status_code=404, detail="Für diesen Raum läuft keine Aufnahme")


def _pairs(payload: dict) -> list:
    points = payload.get("points")
    if not isinstance(points, list) or not 1 <= len(points) <= 12:
        raise ValueError("1 bis 12 Standpunkte")
    pairs = []
    for point in points:
        try:
            raw = [float(v) for v in point["raw"]]
            ref = [float(v) for v in point["ref"]]
        except (KeyError, TypeError, ValueError) as err:
            raise ValueError("Jeder Standpunkt braucht raw [x, y] und ref [x, y]") from err
        if len(raw) != 2 or len(ref) != 2 or not all(map(math.isfinite, raw + ref)):
            raise ValueError("Jeder Standpunkt braucht raw [x, y] und ref [x, y]")
        pairs.append((tuple(raw), tuple(ref)))
    return pairs


@app.post("/api/rooms/{room_id}/alignment")
def api_solve_alignment(room_id: str, payload: dict) -> dict:
    """What the standpoints say about the sensor's placement. Saves nothing."""
    room = _room_or_404(room_id)
    try:
        pairs = _pairs(payload)
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return alignment.solve(pairs, room.sensor.model_dump(), room.width, room.height)


@app.put("/api/rooms/{room_id}/calibration")
async def api_apply_calibration(room_id: str, payload: dict) -> dict:
    """Keep a calibration result.

    Any of: `filter` {confirm_s, smoothing}; `alignment` {x, y, angle,
    mirror, rms_m, points}; `interference` {spots, device_id}, or null to
    forget the learned spots.
    """
    room = _room_or_404(room_id)
    sensor: dict = {}
    calibration: dict = {}
    for key in ("filter", "alignment", "interference"):
        if key in payload and payload[key] is not None and not isinstance(payload[key], dict):
            raise HTTPException(status_code=422, detail=f"„{key}“ muss ein Objekt sein")
    if "filter" in payload:
        wanted = payload["filter"] or {}
        calibration.update({k: wanted[k] for k in ("confirm_s", "smoothing") if k in wanted})
    if "alignment" in payload:
        found = payload["alignment"] or {}
        try:
            sensor = {k: found[k] for k in ("x", "y", "angle", "mirror")}
        except KeyError as err:
            raise HTTPException(status_code=422, detail="Die Ausrichtung braucht x, y, angle und mirror") from err
        calibration.update({
            "aligned_at": time.time(),
            "alignment_rms_m": found.get("rms_m"),
            "alignment_points": found.get("points"),
        })
    if "interference" in payload:
        learned = payload["interference"]
        if learned is None:
            calibration.update({"interference": [], "interference_device_id": None, "interference_learned_at": None})
        else:
            if learned.get("device_id") != room.sensor.device_id:
                raise HTTPException(
                    status_code=409,
                    detail="Die Störquellen wurden mit einem anderen Sensor aufgenommen als dem, der jetzt im Raum steht",
                )
            spots = learned.get("spots") or []
            stored = room.calibration
            # Taking single spots away is an edit of what was learned, not
            # a new learning run: it keeps the date.
            edit = (
                stored.interference_device_id == room.sensor.device_id
                and isinstance(spots, list)
                and all(isinstance(s, dict) for s in spots)
                and all(s in [o.model_dump() for o in stored.interference] for s in spots)
            )
            calibration.update({
                "interference": spots,
                "interference_device_id": room.sensor.device_id,
                "interference_learned_at": stored.interference_learned_at if edit else time.time(),
            })
    if not sensor and not calibration:
        raise HTTPException(status_code=422, detail="Nichts zu übernehmen")
    try:
        updated = rooms.update_calibration(room_id, sensor=sensor or None, calibration=calibration or None)
    except ValidationError as err:
        raise HTTPException(status_code=422, detail=_validation_detail(err)) from err
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    if updated is None:
        raise HTTPException(status_code=404, detail="Raum nicht gefunden")
    await refresh()
    return _room_view(updated)


# --- live ------------------------------------------------------------------


@app.get("/api/live")
def api_live() -> dict:
    """Every room's current evaluation — what the map draws, 3× a second."""
    return {"rooms": list(engine.latest.values())}


_ASSET_RE = re.compile(r'((?:href|src)="static/[^"?]+\.(?:css|js))"')


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    # Single-page app: Ingress serves this behind a per-session token path
    # prefix, and every asset/API call uses relative URLs so they resolve
    # under it. A second HTML route would nest them one level too deep.
    #
    # Stylesheet and scripts carry the add-on version, so a browser (or a
    # proxy in front of Home Assistant) never pairs this page with the
    # files of another release; the page itself is never cached.
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    version = builder.addon_version() or "0"
    html = _ASSET_RE.sub(lambda m: f'{m.group(1)}?v={version}"', html)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})
