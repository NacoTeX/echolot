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
from app.radar_link import MOUNT_MODES, MountingError, links
from app.room_engine import RoomEngine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("echolot")

STATIC_DIR = Path(__file__).parent / "static"

engine = RoomEngine(links)
links.add_listener(engine.wake)
captures = Captures(links)
links.add_listener(captures.on_frame)


def _module_mounting(device_id: str) -> bool:
    """A module's mounting, as read back, into its room. True if the room
    changed (first report, or a change that drops what was measured)."""
    snap = links.snapshot(device_id)
    mounting = snap.mounting() if snap else None
    if mounting is None:
        return False
    return rooms.note_module_mounting(device_id, mounting) is not None


def _on_module_mounting(device_id: str) -> None:
    if _module_mounting(device_id):
        asyncio.get_running_loop().create_task(refresh())


links.add_mounting_listener(_on_module_mounting)


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
#: Where the page loads its stylesheet, scripts and images from. The
#: version is part of the path, not a query: Home Assistant's service
#: worker (active only over HTTPS) answers some paths from its own cache
#: with the query ignored, and handed out the 0.x stylesheet for
#: `static/style.css?v=1.1.1`. A new path per release cannot be answered
#: from any cache. `/static` stays mounted for anything that still asks.
ASSET_PREFIX = f"assets/{builder.addon_version() or '0'}"
app.mount(f"/{ASSET_PREFIX}", StaticFiles(directory=STATIC_DIR), name="assets")
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


#: How long the mounting route waits for the module to read a change back.
MOUNTING_CONFIRM_S = 6.0


@app.put("/api/devices/{device_id}/mounting")
async def api_set_module_mounting(device_id: str, payload: dict) -> dict:
    """Write the module's own mounting — {mode: "side"|"top", height_m,
    angle_deg} — and wait for it to read the change back.

    `confirmed` says whether it did: the answer carries the mounting the
    module reports, not the one asked for. A change the module confirms
    drops what the room measured with the old one (rooms.note_module_mounting).
    """
    device = _device_or_404(device_id)
    try:
        mode = str(payload.get("mode"))
        height = float(payload.get("height_m"))
        angle = float(payload.get("angle_deg"))
    except (TypeError, ValueError) as err:
        raise HTTPException(status_code=422, detail="Montage braucht mode, height_m und angle_deg") from err
    if mode not in MOUNT_MODES:
        raise HTTPException(status_code=422, detail="Montage ist „side“ (Wand) oder „top“ (Decke)")
    if not (0.5 <= height <= 5.0 and 0.0 <= angle <= 90.0 and math.isfinite(height) and math.isfinite(angle)):
        raise HTTPException(status_code=422, detail="Höhe 0,5–5 m und Neigung 0–90°")
    link = links.link(device.id)
    if link is None:
        raise HTTPException(status_code=409, detail="Der Sensor ist gerade nicht verbunden.")
    wanted = {"mode": mode, "height_m": round(height, 2), "angle_deg": round(angle, 2)}
    # What the room was measured with, on record before it changes — or
    # the change would pass for the module's first word and drop nothing.
    _module_mounting(device.id)
    try:
        link.write_mounting(mode, wanted["height_m"], wanted["angle_deg"])
    except MountingError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err
    deadline = time.monotonic() + MOUNTING_CONFIRM_S
    confirmed = False
    while time.monotonic() < deadline:
        now = link.snapshot.mounting()
        if now is not None and rooms._same_mounting(now, wanted):
            confirmed = True
            break
        await asyncio.sleep(0.2)
    # Not waiting for the listener's settle time: the room is brought in
    # line with the module before the answer, so the page shows both.
    if _module_mounting(device.id):
        await refresh()
    return {"confirmed": confirmed, "mounting": link.snapshot.mounting(), "wanted": wanted}


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
    data["alignment_state"] = rooms.alignment_state(room)
    snap = links.snapshot(room.sensor.device_id)
    # How the module says it is mounted, live: what it reads back now.
    data["module"] = {
        "connected": bool(snap and snap.connected),
        "entities": bool(snap and snap.mounting_entities),
        "mounting": snap.mounting() if snap else None,
    }
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


def _calibration_conflict(conflict: rooms.CalibrationConflict) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"message": str(conflict), "changed": conflict.changed, "room": _room_view(conflict.current)},
    )


def _standpoint_request(value) -> dict:
    """Where the person stands for a standpoint recording: {ref, role,
    replace_id}."""
    if not isinstance(value, dict):
        raise ValueError("„standpoint“ muss ein Objekt sein")
    try:
        ref = [float(v) for v in value["ref"]]
    except (KeyError, TypeError, ValueError) as err:
        raise ValueError("Der Standpunkt braucht ref [x, y]") from err
    if len(ref) != 2 or not all(map(math.isfinite, ref)):
        raise ValueError("Der Standpunkt braucht ref [x, y]")
    role = value.get("role", "fit")
    if role not in ("fit", "check"):
        raise ValueError("Ein Standpunkt ist zum Anpassen (fit) oder zur Kontrolle (check)")
    replace_id = value.get("replace_id")
    if replace_id is not None and not isinstance(replace_id, str):
        raise ValueError("replace_id muss die Kennung eines Standpunkts sein")
    return {"ref": ref, "role": role, "replace_id": replace_id}


@app.post("/api/rooms/{room_id}/capture", status_code=201)
async def api_start_capture(room_id: str, payload: dict) -> dict:
    """Start listening: `kind` "empty" (learn reflections) or "point".

    A "point" names its `standpoint` {ref, role, replace_id}; its result
    is then kept with the room's standpoints as soon as it is in, by
    whichever request sees it first — a reload in between loses nothing.
    """
    room = _room_or_404(room_id)
    if room.sensor.device_id and devices.get_device(room.sensor.device_id) is None:
        raise HTTPException(status_code=422, detail="Der zugeordnete Sensor existiert nicht mehr")
    try:
        standpoint = _standpoint_request(payload["standpoint"]) if payload.get("standpoint") is not None else None
        capture = captures.start(
            room, str(payload.get("kind")),
            delay_s=payload.get("delay_s"), duration_s=payload.get("duration_s"), standpoint=standpoint,
        )
    except (TypeError, ValueError) as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return captures.view(capture)


def _keep_standpoint(capture, view: dict) -> None:
    """A finished standpoint recording goes to the room's standpoints, once."""
    if capture.kind != "point" or capture.standpoint is None or capture.kept or capture.keep_error:
        return
    result = capture.result
    if not result or not result.get("ok"):
        return
    # Marked first: nothing between here and the write awaits, and a
    # second request must not keep it twice.
    capture.kept = True
    wanted = capture.standpoint
    try:
        point = rooms.Standpoint(
            ref=tuple(wanted["ref"]), raw=tuple(result["raw"]), role=wanted["role"],
            spread_m=result.get("spread_m"), share=result.get("share"),
            measured_at=time.time() - view["ended_ago_s"], source="capture",
            warnings=list(result.get("warnings") or [])[:5],
        )
        rooms.add_standpoint(capture.room_id, point, device_id=capture.device_id, epoch=capture.epoch,
                             replace_id=wanted.get("replace_id"))
    except (rooms.CalibrationConflict, rooms.GoneError, ValidationError, ValueError) as err:
        capture.kept = False
        capture.keep_error = str(err)


@app.get("/api/rooms/{room_id}/capture", response_model=None)
async def api_get_capture(room_id: str) -> dict | Response:
    """The running or finished recording; 204 when there is none."""
    _room_or_404(room_id)
    capture = captures.get(room_id)
    if capture is None:
        return Response(status_code=204)
    view = captures.view(capture)
    if view["phase"] == "done":
        _keep_standpoint(capture, view)
        view = captures.view(capture)
    return view


@app.delete("/api/rooms/{room_id}/capture", status_code=204)
async def api_cancel_capture(room_id: str) -> None:
    if not captures.cancel(room_id):
        raise HTTPException(status_code=404, detail="Für diesen Raum läuft keine Aufnahme")


def _pairs(payload: dict) -> tuple[list, list]:
    """(spots to fit, control spots). A control spot has `role` "check"
    and takes no part in the fit."""
    points = payload.get("points")
    if not isinstance(points, list) or not points:
        raise ValueError("Mindestens ein Standpunkt ist nötig")
    pairs, checks = [], []
    for point in points:
        if not isinstance(point, dict):
            raise ValueError("Jeder Standpunkt braucht raw [x, y] und ref [x, y]")
        try:
            raw = [float(v) for v in point["raw"]]
            ref = [float(v) for v in point["ref"]]
        except (KeyError, TypeError, ValueError) as err:
            raise ValueError("Jeder Standpunkt braucht raw [x, y] und ref [x, y]") from err
        if len(raw) != 2 or len(ref) != 2 or not all(map(math.isfinite, raw + ref)):
            raise ValueError("Jeder Standpunkt braucht raw [x, y] und ref [x, y]")
        role = point.get("role", "fit")
        if role not in ("fit", "check"):
            raise ValueError("Ein Standpunkt ist zum Anpassen (fit) oder zur Kontrolle (check)")
        (checks if role == "check" else pairs).append((tuple(raw), tuple(ref)))
    if not 1 <= len(pairs) <= rooms.MAX_FIT_STANDPOINTS or len(checks) > rooms.MAX_CHECK_STANDPOINTS:
        raise ValueError("1 bis 12 Standpunkte zum Anpassen und höchstens 6 Kontrollpunkte")
    return pairs, checks


def _kept_pairs(points: list) -> tuple[list, list]:
    pairs = [(tuple(p.raw), tuple(p.ref)) for p in points if p.role == "fit"]
    checks = [(tuple(p.raw), tuple(p.ref)) for p in points if p.role == "check"]
    if not pairs:
        raise ValueError("Mindestens ein Standpunkt zum Anpassen ist nötig")
    return pairs, checks


def _solve_for(room: rooms.Room, pairs: list, checks: list, key: str, heights: dict) -> dict:
    sensor = rooms.SensorPlacement.model_validate({
        **room.sensor.model_dump(),
        **{k: heights[k] for k in ("mount_height_m", "target_height_m") if k in heights},
    })
    result = alignment.solve(pairs, sensor.model_dump(), room.width, room.height, checks=checks)
    # What this was computed for. Applying checks it against the room.
    result["basis"] = {
        "revision": room.revision,
        "device_id": room.sensor.device_id,
        "epoch": room.calibration.mounting_epoch,
        "algorithm": alignment.VERSION,
        "standpoints": key,
        "mount_height_m": sensor.mount_height_m,
        "target_height_m": sensor.target_height_m,
    }
    return result


@app.post("/api/rooms/{room_id}/alignment")
def api_solve_alignment(room_id: str, payload: dict) -> dict:
    """What the standpoints say about the sensor's placement. Saves nothing.

    The room's kept standpoints, or `points` given here. The heights as
    the page has them, saved or not yet: they decide whether the slant
    line can be tried at all. The answer carries its `basis`.
    """
    room = _room_or_404(room_id)
    try:
        if "points" in payload:
            pairs, checks = _pairs(payload)
            key = rooms.standpoints_key([
                {"role": p.get("role", "fit"), "raw": p["raw"], "ref": p["ref"]} for p in payload["points"]
            ])
        else:
            kept = rooms.draft_points(room)
            pairs, checks = _kept_pairs(kept)
            key = rooms.standpoints_key(kept)
        return _solve_for(room, pairs, checks, key, payload)
    except ValidationError as err:
        raise HTTPException(status_code=422, detail=_validation_detail(err)) from err
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err


@app.post("/api/rooms/{room_id}/alignment/apply")
async def api_apply_alignment(room_id: str, payload: dict) -> dict:
    """Keep the proposal computed for `basis`, as the solve route returned it.

    Computed once more here from the kept standpoints — what is saved is
    what the server computes, with every standpoint and its report, not
    what a page sends — and saved only if the room is still what it was
    computed for. Anything else is 409 with what changed.
    """
    room = _room_or_404(room_id)
    basis = payload.get("basis")
    if not isinstance(basis, dict):
        raise HTTPException(status_code=422, detail="Übernehmen braucht den Stand, für den der Vorschlag berechnet wurde (basis)")
    changed = rooms.binding_conflicts(room, basis, alignment.VERSION)
    if changed:
        raise _calibration_conflict(rooms.stale_proposal(room, changed))
    kept = rooms.draft_points(room)
    try:
        pairs, checks = _kept_pairs(kept)
        result = await asyncio.to_thread(
            _solve_for, room, pairs, checks, rooms.standpoints_key(kept), basis,
        )
        fields = {k: result[k] for k in ("x", "y", "angle", "mirror", "slant", "range_scale",
                                         "range_offset_m", "azimuth_scale")}
        fields.update({k: result["basis"][k] for k in ("mount_height_m", "target_height_m")})
        updated = rooms.apply_alignment(room_id, basis=basis, algorithm=alignment.VERSION, sensor=fields,
                                        report=result, standpoints=kept)
    except rooms.CalibrationConflict as conflict:
        raise _calibration_conflict(conflict) from conflict
    except ValidationError as err:
        raise HTTPException(status_code=422, detail=_validation_detail(err)) from err
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    if updated is None:
        raise HTTPException(status_code=404, detail="Raum nicht gefunden")
    await refresh()
    return _room_view(updated)


@app.post("/api/rooms/{room_id}/alignment/points", status_code=201)
async def api_add_standpoint(room_id: str, payload: dict) -> dict:
    """A standpoint measured outside Echolot, handed in: {ref, raw, role,
    spread_m, share, replace_id}. Kept as source "api"."""
    room = _room_or_404(room_id)
    try:
        point = rooms.Standpoint.model_validate({
            **{k: payload[k] for k in ("ref", "raw", "role", "spread_m", "share") if k in payload},
            "measured_at": time.time(), "source": "api",
        })
        updated = rooms.add_standpoint(room_id, point, device_id=room.sensor.device_id,
                                       epoch=room.calibration.mounting_epoch, replace_id=payload.get("replace_id"))
    except rooms.CalibrationConflict as conflict:
        raise _calibration_conflict(conflict) from conflict
    except rooms.GoneError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    except ValidationError as err:
        raise HTTPException(status_code=422, detail=_validation_detail(err)) from err
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    if updated is None:
        raise HTTPException(status_code=404, detail="Raum nicht gefunden")
    return _room_view(updated)


@app.delete("/api/rooms/{room_id}/alignment/points/{point_id}")
async def api_drop_standpoint(room_id: str, point_id: str) -> dict:
    _room_or_404(room_id)
    try:
        updated = rooms.drop_standpoint(room_id, point_id)
    except rooms.GoneError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    if updated is None:
        raise HTTPException(status_code=404, detail="Raum nicht gefunden")
    return _room_view(updated)


@app.delete("/api/rooms/{room_id}/alignment/points")
async def api_clear_standpoints(room_id: str) -> dict:
    _room_or_404(room_id)
    updated = rooms.drop_standpoint(room_id, None)
    if updated is None:
        raise HTTPException(status_code=404, detail="Raum nicht gefunden")
    return _room_view(updated)


@app.post("/api/rooms/{room_id}/alignment/restore")
async def api_restore_alignment(room_id: str, payload: dict) -> dict:
    """Put the sensor back as `id` in the alignment history had it —
    `revision` is the room the list was read from."""
    _room_or_404(room_id)
    try:
        updated = rooms.restore_alignment(room_id, str(payload.get("id")), revision=payload.get("revision"))
    except rooms.GoneError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    except rooms.CalibrationConflict as conflict:
        raise _calibration_conflict(conflict) from conflict
    except ValidationError as err:
        raise HTTPException(status_code=422, detail=_validation_detail(err)) from err
    if updated is None:
        raise HTTPException(status_code=404, detail="Raum nicht gefunden")
    await refresh()
    return _room_view(updated)


@app.post("/api/rooms/{room_id}/remount")
async def api_remount_sensor(room_id: str, payload: dict) -> dict:
    """The sensor was taken down and hung up again (or turned by hand):
    what was measured with the old mounting no longer applies."""
    _room_or_404(room_id)
    captures.cancel(room_id)
    try:
        updated = rooms.remount_sensor(room_id, revision=payload.get("revision"))
    except rooms.CalibrationConflict as conflict:
        raise _calibration_conflict(conflict) from conflict
    if updated is None:
        raise HTTPException(status_code=404, detail="Raum nicht gefunden")
    await refresh()
    return _room_view(updated)


@app.get("/api/rooms/{room_id}/alignment/suggest")
def api_suggest_standpoints(room_id: str, count: int = 5) -> dict:
    """Where to stand: spread over distance and angle, in view, off the furniture."""
    room = _room_or_404(room_id)
    return {"points": alignment.suggest_points(room, max(2, min(count, 8)))}


@app.put("/api/rooms/{room_id}/calibration")
async def api_apply_calibration(room_id: str, payload: dict) -> dict:
    """Keep a calibration setting.

    Any of: `filter` {confirm_s, smoothing}; `mounting` {mount_height_m,
    target_height_m}; `reset_model` true; `interference` {spots,
    device_id, epoch}, or null to forget the learned spots. An alignment
    is applied on its own route (alignment/apply), bound to what it was
    computed for.
    """
    room = _room_or_404(room_id)
    sensor: dict = {}
    calibration: dict = {}
    if "alignment" in payload:
        raise HTTPException(status_code=422, detail="Eine Ausrichtung wird über „alignment/apply“ übernommen")
    for key in ("filter", "interference", "mounting"):
        if key in payload and payload[key] is not None and not isinstance(payload[key], dict):
            raise HTTPException(status_code=422, detail=f"„{key}“ muss ein Objekt sein")
    if "filter" in payload:
        wanted = payload["filter"] or {}
        calibration.update({k: wanted[k] for k in ("confirm_s", "smoothing") if k in wanted})
    if "mounting" in payload:
        # Heights alone, before any standpoint is measured.
        wanted = payload["mounting"] or {}
        sensor.update({k: wanted[k] for k in ("mount_height_m", "target_height_m") if k in wanted})
    if payload.get("reset_model"):
        # Back to the module's positions as they are; placement stays.
        sensor.update(dict(rooms.NEUTRAL_MODEL))
        calibration.update({"alignment_model": None, "alignment_check_m": None})
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
            if learned.get("epoch") is not None and learned.get("epoch") != room.calibration.mounting_epoch:
                raise HTTPException(
                    status_code=409,
                    detail="Die Störquellen wurden aufgenommen, bevor der Sensor neu montiert wurde",
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


_ASSET_RE = re.compile(r'((?:href|src)=")static/')


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    # Single-page app: Ingress serves this behind a per-session token path
    # prefix, and every asset/API call uses relative URLs so they resolve
    # under it. A second HTML route would nest them one level too deep.
    #
    # Every file the page loads comes from the versioned asset path (see
    # ASSET_PREFIX), so nothing between here and the browser can pair
    # this page with the files of another release; the page itself is
    # never cached.
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = _ASSET_RE.sub(lambda m: f"{m.group(1)}{ASSET_PREFIX}/", html)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})
