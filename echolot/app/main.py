"""Echolot backend.

Phase 1 established the add-on skeleton (Ingress UI, ESPHome CLI check).
Phase 2 added device management and browser-based flashing: the backend
renders a per-device ESPHome/ESPectre YAML, compiles it, and serves the
result as an ESP Web Tools manifest + firmware image.
Phase 3 adds zones (grouping devices with OR-logic presence aggregation)
and pushing runtime parameters (detection threshold, recalibration) to
already-flashed devices via Home Assistant's Core API — ESPectre exposes
these as HA entities already, so no direct device protocol is needed.
"""

import asyncio
import logging
import os
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from app import (
    builder,
    devices,
    entity_resolver,
    ha_client,
    mqtt_bridge,
    overview,
    presets,
    reachability,
    zone_logic,
    zones,
)
from app.board_registry import BOARDS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("echolot")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Run the MQTT zone publisher for the lifetime of the server.

    Zones are only useful outside this add-on once they exist as Home
    Assistant entities, and that mirroring has to keep running whether or
    not anyone has the dashboard open.
    """
    task = None
    if os.environ.get("ECHOLOT_MQTT_EXPORT", "true").lower() in ("0", "false", "no"):
        logger.info("MQTT export disabled by configuration")
    else:
        try:
            await mqtt_bridge.bridge.start()
            task = asyncio.create_task(
                mqtt_bridge.publish_loop(compute_all_zone_states, zones.list_zones, forget_zone_runtime)
            )
        except mqtt_bridge.MqttUnavailable as err:
            # Entirely normal without the Mosquitto add-on; everything else
            # keeps working, the zones just stay local to this UI.
            logger.info("MQTT export inactive: %s", err)
            mqtt_bridge.bridge.error = str(err)

    try:
        yield
    finally:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        mqtt_bridge.bridge.stop()
        await ha_client.close_client()


app = FastAPI(title="Echolot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Builds run for minutes in a background task; asyncio only holds a weak
# reference to a task once created, so without keeping one here the task
# risks being garbage-collected mid-build. Discarded again once it's done.
_background_builds: set[asyncio.Task] = set()


def _validation_detail(err: ValidationError) -> list[dict]:
    # err.errors() can carry raw exception objects in "ctx" (e.g. from a
    # validator's `raise ValueError`), which json.dumps can't serialize.
    # Keep only the plain-text fields the UI actually uses.
    return [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in err.errors()]


def _safe_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_ts(value) -> float | None:
    """ISO-8601 timestamp -> epoch seconds. HA emits a trailing 'Z' that
    fromisoformat only learned to accept in 3.11+, so normalise it."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _adopt_entities(device: devices.Device, states: list[dict]) -> bool:
    """Resolve the device's entities against a snapshot and persist the result."""
    found = entity_resolver.resolve(device, states)
    if not found:
        return False
    for field, entity_id in found.items():
        setattr(device, field, entity_id)
    devices.save_device(device)
    logger.info("Entities für %s erkannt: %s", device.config.name, found)
    return True


async def _autodetect_entities(device: devices.Device) -> bool:
    """Fetch a snapshot, then adopt from it.

    Called when a single-device read finds its motion entity missing. A
    flashed device that Home Assistant adopted under a name we did not
    predict is the common case; without this the device sits at "nicht
    verfügbar" forever with a working sensor behind it.
    """
    try:
        states = await ha_client.list_states()
    except ha_client.HomeAssistantUnavailable:
        return False
    return _adopt_entities(device, states)


async def _read_device_state(device: devices.Device, allow_detect: bool = True) -> dict:
    """Current motion/score/threshold for one device.

    The three entities are read concurrently, so this costs one round-trip
    of latency rather than three, and a few hundred bytes rather than the
    whole state machine of the installation.

    An earlier version fetched /api/states once and served every device
    from that snapshot. It looked like a win — fifteen requests down to
    one for a five-device zone — but that one request carries *every*
    entity Home Assistant knows about, on a timer, forever. On a large
    installation that is megabytes every ten seconds to read a handful of
    numbers. Targeted reads, issued together, are the better trade;
    /api/states is kept for discovery, where it earns its size.
    """
    if not device.entity_motion:
        return {"available": False, "error": "Für dieses Gerät ist keine Bewegungs-Entity konfiguriert"}

    async def read(entity_id: str | None):
        return await ha_client.get_state(entity_id) if entity_id else None

    try:
        motion, score, threshold = await asyncio.gather(
            read(device.entity_motion),
            read(device.entity_movement_score),
            read(device.entity_threshold),
        )
        if motion is None and allow_detect and await _autodetect_entities(device):
            # Entities were just relearned — read once more before giving up.
            return await _read_device_state(device, allow_detect=False)
    except ha_client.HomeAssistantUnavailable as err:
        return {"available": False, "error": str(err)}
    if motion is None:
        return {
            "available": False,
            "error": (
                f"Entity {device.entity_motion} existiert in Home Assistant nicht. "
                "Wurde das Gerät dort schon hinzugefügt? Einstellungen → Geräte & "
                "Dienste → Integrationen, dort sollte ESPHome das Gerät zur "
                "Einrichtung anbieten."
            ),
        }
    return {
        "available": True,
        "motion": motion["state"] == "on",
        "movement_score": _safe_float(score["state"]) if score else None,
        "threshold": _safe_float(threshold["state"]) if threshold else None,
    }


#: Per-zone state-machine memory. Zones are few and short-lived compared
#: to the process, and a forgotten entry only costs a dict slot, but a
#: deleted zone should not keep its hold running if the id is reused.
_zone_runtimes: dict[str, zone_logic.ZoneRuntime] = {}


def forget_zone_runtime(zone_id: str) -> None:
    _zone_runtimes.pop(zone_id, None)


async def _read_devices(device_ids: list[str]) -> list[tuple[str, devices.Device | None, dict]]:
    """Read several devices at once.

    Concurrency is what makes targeted reads affordable: a zone of five
    devices is fifteen small requests, but they all go out together over
    one pooled connection, so it costs one round-trip of latency rather
    than fifteen.
    """
    resolved = [(device_id, devices.get_device(device_id)) for device_id in device_ids]
    states = await asyncio.gather(
        *(
            _read_device_state(device)
            if device is not None
            else _missing_device_state()
            for _, device in resolved
        )
    )
    return [(device_id, device, state) for (device_id, device), state in zip(resolved, states)]


async def _missing_device_state() -> dict:
    """A zone member whose device was deleted out from under it."""
    return {"available": False, "motion": None, "error": "Gerät existiert nicht mehr"}


async def compute_zone_state(zone: zones.Zone) -> dict:
    """Aggregate a zone's members and run its presence state machine.

    Shared by the API route and the MQTT publisher, so what Home Assistant
    receives can never drift from what the dashboard shows. The raw
    aggregation is still OR over the members — any device seeing movement
    means the zone sees movement — but hysteresis and hold time now sit
    between that and the published `occupied` flag (see zone_logic).
    """
    members = []
    raw_motion = False
    any_available = False
    best_score: float | None = None
    for device_id, device, state in await _read_devices(zone.device_ids):
        if device is None:
            members.append({"device_id": device_id, "name": None, **state})
            continue
        if state.get("available"):
            any_available = True
            if state.get("motion"):
                raw_motion = True
            score = state.get("movement_score")
            if score is not None and (best_score is None or score > best_score):
                best_score = score
        members.append(
            {
                "device_id": device_id,
                "name": device.config.friendly_name or device.config.name,
                **state,
            }
        )

    runtime = _zone_runtimes.setdefault(zone.id, zone_logic.ZoneRuntime())
    verdict = zone_logic.evaluate(
        runtime,
        motion=raw_motion,
        score=best_score,
        enter_threshold=zone.enter_threshold,
        exit_threshold=zone.exit_threshold,
        hold_seconds=zone.hold_seconds,
        now=time.monotonic(),
    )
    return {"available": any_available, "members": members, **verdict.as_dict()}


async def compute_all_zone_states(zone_list: list) -> list[tuple]:
    """Every zone's state, evaluated concurrently.

    The MQTT loop walks every zone on every tick. Walking them in sequence
    would add up their latencies; running them together means the whole
    cycle costs about as long as its slowest zone.
    """
    states = await asyncio.gather(*(compute_zone_state(zone) for zone in zone_list))
    return list(zip(zone_list, states))


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/overview")
async def api_overview() -> dict:
    """Everything the first screen needs, in one request.

    Assembled here rather than in the browser: the alternative is one
    request per zone plus three per device from a page whose whole job is
    to load fast. Devices and zones are read concurrently, so the page
    costs roughly one round-trip regardless of how many there are.
    """
    device_list = devices.list_devices()
    zone_list = zones.list_zones()

    built = [d for d in device_list if str(d.status) == "success"]
    built_states = dict(
        zip(
            (d.id for d in built),
            await asyncio.gather(*(_read_device_state(d) for d in built)),
        )
    )
    device_states = [(d, built_states.get(d.id)) for d in device_list]

    zone_verdicts = await asyncio.gather(*(compute_zone_state(z) for z in zone_list))

    zone_views = []
    for zone, verdict in zip(zone_list, zone_verdicts):
        zone_views.append(
            {
                "id": zone.id,
                "name": zone.name,
                "state": verdict["state"],
                "occupied": verdict["occupied"],
                "hold_remaining": verdict["hold_remaining"],
                "available": verdict["available"],
                "device_count": len(zone.device_ids),
            }
        )

    mqtt_status = mqtt_bridge.bridge.status()
    mqtt_wanted = os.environ.get("ECHOLOT_MQTT_EXPORT", "true").lower() not in ("0", "false", "no")
    esphome = _esphome_version()

    problems = overview.collect_problems(
        device_states=device_states,
        zones_without_devices=[z for z in zone_list if not z.device_ids],
        mqtt_status=mqtt_status,
        mqtt_wanted=mqtt_wanted,
        esphome=esphome,
    )

    return {
        "zones": zone_views,
        "problems": [p.as_dict() for p in problems],
        "devices": {
            "total": len(device_list),
            "built": sum(1 for d in device_list if str(d.status) == "success"),
        },
        "esphome": esphome,
        "mqtt": {**mqtt_status, "wanted": mqtt_wanted},
        "radio_load_kb_per_second": overview.radio_load(device_list, presets.KB_PER_SECOND_PER_PPS),
    }


@app.get("/api/mqtt/status")
def api_mqtt_status() -> dict:
    return mqtt_bridge.bridge.status()


@app.get("/api/presets")
def api_presets() -> dict:
    return {
        "presets": presets.as_dicts(),
        "kb_per_second_per_pps": presets.KB_PER_SECOND_PER_PPS,
    }


_esphome_version_cache: dict | None = None


def _esphome_version() -> dict:
    """Whether the bundled ESPHome CLI is usable, cached.

    Spawning a subprocess is cheap once and wasteful on every overview
    load — and the answer cannot change while this container runs.
    """
    global _esphome_version_cache
    if _esphome_version_cache is not None:
        return _esphome_version_cache
    try:
        result = subprocess.run(
            ["esphome", "version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        _esphome_version_cache = {"available": True, "version": result.stdout.strip()}
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as err:
        logger.warning("esphome CLI check failed: %s", err)
        # Not cached: a transient failure should not be permanent.
        return {"available": False, "error": str(err)}
    return _esphome_version_cache


@app.get("/api/esphome/version")
def esphome_version() -> dict:
    """Confirm the bundled ESPHome CLI is usable (needed for firmware builds)."""
    return _esphome_version()


@app.get("/api/boards")
def list_boards() -> list[dict]:
    return [
        {
            "key": b.key,
            "label": b.label,
            "chip_family": b.chip_family,
            "experimental": b.experimental,
        }
        for b in BOARDS.values()
    ]


@app.get("/api/devices")
def api_list_devices() -> list[dict]:
    return [d.public() for d in devices.list_devices()]


@app.post("/api/devices", status_code=201)
def api_create_device(payload: dict) -> dict:
    try:
        config = devices.DeviceCreate.model_validate(payload)
    except ValidationError as err:
        raise HTTPException(status_code=422, detail=_validation_detail(err)) from err
    device = devices.create_device(config)
    return device.public()


@app.get("/api/devices/{device_id}")
def api_get_device(device_id: str) -> dict:
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    return device.public()


@app.patch("/api/devices/{device_id}")
def api_update_device(device_id: str, payload: dict) -> dict:
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    try:
        patch = devices.DeviceUpdate.model_validate(payload)
    except ValidationError as err:
        raise HTTPException(status_code=422, detail=_validation_detail(err)) from err
    device.apply_update(patch)
    devices.save_device(device)
    return device.public()


@app.delete("/api/devices/{device_id}", status_code=204)
def api_delete_device(device_id: str) -> None:
    if not devices.delete_device(device_id):
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")


@app.get("/api/devices/{device_id}/state")
async def api_device_state(device_id: str) -> dict:
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    return await _read_device_state(device)


@app.get("/api/devices/{device_id}/history")
async def api_device_history(device_id: str, minutes: int = 30) -> dict:
    """Movement-score history, so the dashboard sparkline starts populated
    instead of building up from nothing on every page load."""
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    if not device.entity_movement_score:
        return {"available": False, "error": "Keine Bewegungswert-Entity konfiguriert", "points": []}

    minutes = max(1, min(minutes, 1440))
    try:
        raw = await ha_client.get_history(device.entity_movement_score, minutes)
    except ha_client.HomeAssistantUnavailable as err:
        return {"available": False, "error": str(err), "points": []}

    points = []
    for entry in raw:
        value = _safe_float(entry.get("state"))
        if value is None:  # skips "unavailable" / "unknown"
            continue
        stamp = entry.get("last_changed") or entry.get("last_updated")
        parsed = _parse_ts(stamp)
        if parsed is None:
            continue
        points.append({"t": parsed, "v": value})
    return {"available": True, "points": points}


@app.post("/api/devices/{device_id}/threshold")
async def api_set_threshold(device_id: str, payload: dict) -> dict:
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    if not device.entity_threshold:
        raise HTTPException(status_code=409, detail="Für dieses Gerät ist keine Schwellen-Entity konfiguriert")
    value = _safe_float(payload.get("value"))
    if value is None or not (0.0 <= value <= 10.0):
        raise HTTPException(status_code=422, detail='Erwartet wird {"value": <Zahl 0.0-10.0>}')
    try:
        await ha_client.call_service("number", "set_value", device.entity_threshold, value=value)
    except ha_client.HomeAssistantUnavailable as err:
        raise HTTPException(status_code=502, detail=f"Home Assistant nicht erreichbar: {err}") from err
    return {"status": "ok"}


@app.post("/api/devices/{device_id}/calibrate")
async def api_calibrate_device(device_id: str) -> dict:
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    if not device.entity_calibrate:
        raise HTTPException(status_code=409, detail="Für dieses Gerät ist keine Kalibrierungs-Entity konfiguriert")
    try:
        # A button press, not a switch: recalibration is a one-shot action
        # upstream now, with nothing to switch back off.
        await ha_client.call_service("button", "press", device.entity_calibrate)
    except ha_client.HomeAssistantUnavailable as err:
        raise HTTPException(status_code=502, detail=f"Home Assistant nicht erreichbar: {err}") from err
    return {"status": "ok"}


@app.post("/api/devices/{device_id}/entities/detect")
async def api_detect_entities(device_id: str) -> dict:
    """Re-learn this device's entity ids from Home Assistant.

    The same lookup the state reader does on its own, exposed so a device
    that was flashed before this existed can be repaired without deleting
    and recreating it.
    """
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    try:
        states = await ha_client.list_states()
    except ha_client.HomeAssistantUnavailable as err:
        raise HTTPException(status_code=503, detail=f"Home Assistant nicht erreichbar: {err}") from err

    found = entity_resolver.resolve(device, states)
    if not found:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Home Assistant kennt keine Entities für „{entity_resolver.device_display_name(device)}“. "
                "Prüfe unter Einstellungen → Geräte & Dienste, ob das Gerät als "
                "ESPHome-Integration hinzugefügt wurde — nach dem Flashen muss es "
                "dort einmalig bestätigt werden."
            ),
        )
    for field, entity_id in found.items():
        setattr(device, field, entity_id)
    devices.save_device(device)
    return {"detected": found}


@app.get("/api/devices/{device_id}/credentials")
def api_device_credentials(device_id: str) -> dict:
    """The device's API key and OTA password.

    Separated from the device payload so that drawing the device list
    does not hand out every key in the installation. This is reached one
    device at a time, when someone opens the credentials section.
    """
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    return device.credentials()


@app.get("/api/devices/{device_id}/reachability")
async def api_reachability(device_id: str, host: str | None = None) -> dict:
    """Probe the device on the network and say what answered.

    Settles the question a missing entity cannot: is the device silent, or
    is it answering and merely not adopted by Home Assistant?
    """
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    target = (host or device.ota_address()).strip()
    if not target:
        raise HTTPException(status_code=422, detail="Keine Adresse angegeben")
    result = await reachability.check(target)
    return {**result, "message": reachability.explain(result)}


@app.post("/api/devices/{device_id}/ota", status_code=202)
async def api_start_ota(device_id: str, payload: dict | None = None) -> dict:
    """Push the built firmware to the running device over the network."""
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    if device.status != devices.BuildStatus.SUCCESS or not device.firmware_bin:
        raise HTTPException(status_code=409, detail="Die Firmware wurde noch nicht gebaut")

    address = ((payload or {}).get("address") or device.ota_address()).strip()
    if not address:
        raise HTTPException(status_code=422, detail="Keine Adresse angegeben")

    # One lock covers builds and OTA alike: both write the same build
    # directory, and running them together corrupts it.
    if not builder.try_start_build(device_id):
        raise HTTPException(
            status_code=409, detail="Für dieses Gerät läuft bereits ein Build oder Update"
        )

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
    """Whether this board's cross compiler is actually installed.

    PlatformIO downloads it on the first build, so "absent" is normal then;
    "broken" means a download was interrupted and the package has to go.
    """
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    board = BOARDS.get(device.config.board)
    if board is None:
        raise HTTPException(status_code=422, detail=f"Unbekanntes Board '{device.config.board}'")
    return {"board": board.key, "label": board.label, **builder.toolchain_state(board)}


@app.post("/api/devices/{device_id}/toolchain/reset")
def api_reset_toolchain(device_id: str) -> dict:
    """Throw away this board's toolchain package so the next build refetches it.

    Refused while a build is running: deleting the compiler out from under
    a live compile turns one clear failure into a confusing one.
    """
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    board = BOARDS.get(device.config.board)
    if board is None:
        raise HTTPException(status_code=422, detail=f"Unbekanntes Board '{device.config.board}'")
    if device.status in (devices.BuildStatus.QUEUED, devices.BuildStatus.RUNNING):
        raise HTTPException(
            status_code=409,
            detail="Für dieses Gerät läuft gerade ein Build — warte, bis er beendet ist",
        )
    removed = builder.reset_toolchain(board)
    return {"removed": removed, **builder.toolchain_state(board)}


@app.post("/api/devices/{device_id}/build", status_code=202)
async def api_build_device(device_id: str) -> dict:
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
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
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    if device.status != devices.BuildStatus.SUCCESS or not device.firmware_bin:
        raise HTTPException(status_code=409, detail="Die Firmware wurde noch nicht gebaut")
    manifest = {
        "name": f"Echolot – {device.config.friendly_name or device.config.name}",
        "version": str(int(device.updated_at)),
        "new_install_prompt_erase": True,
        "builds": [
            {
                "chipFamily": device.chip_family,
                "parts": [{"path": "firmware.bin", "offset": 0}],
            }
        ],
    }
    return JSONResponse(manifest)


@app.get("/api/devices/{device_id}/firmware.bin")
def api_device_firmware(device_id: str) -> FileResponse:
    device = devices.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
    if device.status != devices.BuildStatus.SUCCESS or not device.firmware_bin:
        raise HTTPException(status_code=409, detail="Die Firmware wurde noch nicht gebaut")
    path = devices.device_dir(device_id) / device.firmware_bin
    if not path.exists():
        raise HTTPException(status_code=404, detail="Firmware-Datei fehlt auf der Festplatte")
    return FileResponse(path, media_type="application/octet-stream", filename="firmware.bin")


@app.get("/api/zones")
def api_list_zones() -> list[dict]:
    return [z.model_dump() for z in zones.list_zones()]


@app.post("/api/zones", status_code=201)
def api_create_zone(payload: dict) -> dict:
    try:
        config = zones.ZoneCreate.model_validate(payload)
    except ValidationError as err:
        raise HTTPException(status_code=422, detail=_validation_detail(err)) from err
    unknown = [d for d in config.device_ids if devices.get_device(d) is None]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unbekannte Geräte-ID(s): {', '.join(unknown)}")
    zone = zones.create_zone(config)
    return zone.model_dump()


@app.get("/api/zones/{zone_id}")
def api_get_zone(zone_id: str) -> dict:
    zone = zones.get_zone(zone_id)
    if zone is None:
        raise HTTPException(status_code=404, detail="Zone nicht gefunden")
    return zone.model_dump()


@app.patch("/api/zones/{zone_id}")
def api_update_zone(zone_id: str, payload: dict) -> dict:
    zone = zones.get_zone(zone_id)
    if zone is None:
        raise HTTPException(status_code=404, detail="Zone nicht gefunden")
    try:
        patch = zones.ZoneUpdate.model_validate(payload)
    except ValidationError as err:
        raise HTTPException(status_code=422, detail=_validation_detail(err)) from err
    if patch.device_ids is not None:
        unknown = [d for d in patch.device_ids if devices.get_device(d) is None]
        if unknown:
            raise HTTPException(status_code=422, detail=f"Unbekannte Geräte-ID(s): {', '.join(unknown)}")
    try:
        # apply_update re-validates the merged zone, so a patch that only
        # moves one threshold can still break the enter/exit invariant.
        zone.apply_update(patch)
    except ValidationError as err:
        raise HTTPException(status_code=422, detail=_validation_detail(err)) from err
    zones.save_zone(zone)
    return zone.model_dump()


@app.delete("/api/zones/{zone_id}", status_code=204)
def api_delete_zone(zone_id: str) -> None:
    if not zones.delete_zone(zone_id):
        raise HTTPException(status_code=404, detail="Zone nicht gefunden")
    # Retract the discovery message too, or the entity lingers in Home
    # Assistant as "unavailable" forever.
    mqtt_bridge.bridge.forget_zone(zone_id)
    forget_zone_runtime(zone_id)


@app.get("/api/zones/{zone_id}/state")
async def api_zone_state(zone_id: str) -> dict:
    zone = zones.get_zone(zone_id)
    if zone is None:
        raise HTTPException(status_code=404, detail="Zone nicht gefunden")
    return await compute_zone_state(zone)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    # Single-page app: Home Assistant Ingress serves this behind a per-session
    # token path prefix, and every asset/API call below uses relative (no
    # leading slash) URLs so they resolve under that prefix. A second route
    # like "/devices" would break that resolution (its relative fetches would
    # nest one level too deep), so device management is a tab on this page
    # instead of a separate path — see static/app.js.
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")
