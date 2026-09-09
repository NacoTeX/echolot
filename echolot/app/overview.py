"""What to show someone the moment they open Echolot.

The old overview answered questions nobody had: whether the backend was
online (if the page rendered, it was) and which project phase was
finished. What a presence system should say on opening is what it
currently senses, and what is stopping it from sensing correctly.

So this module assembles two things — live zone state, and a list of
problems worded as something to do about them. Everything else (ESPHome
version, device counts, radio load) is background, and is presented that
way.
"""

from dataclasses import dataclass

from app import health, presence_rate


@dataclass(frozen=True)
class Problem:
    """Something wrong, phrased as the thing to do about it."""

    kind: str
    message: str
    #: Which tab resolves it, so the UI can offer a way there.
    tab: str = "devices"
    device_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "message": self.message,
            "tab": self.tab,
            "device_id": self.device_id,
        }


def _device_label(device) -> str:
    return device.config.friendly_name or device.config.name


def collect_problems(
    device_states: list[tuple],
    zones_without_devices: list,
    mqtt_status: dict,
    mqtt_wanted: bool,
    esphome: dict,
) -> list[Problem]:
    """Everything worth acting on, most blocking first.

    `device_states` pairs each device with the state read for it, or None
    when it was never built and so has nothing to read.
    """
    problems: list[Problem] = []

    # Nothing else works if firmware cannot be built.
    if not esphome.get("available"):
        problems.append(
            Problem(
                kind="esphome_missing",
                message=(
                    "ESPHome ist nicht verfügbar — ohne es lässt sich keine Firmware "
                    f"bauen. {esphome.get('error', '')}".strip()
                ),
                tab="devices",
            )
        )

    for device, state in device_states:
        label = _device_label(device)
        status = str(device.status)

        if status == "error":
            problems.append(
                Problem(
                    kind="build_failed",
                    message=f"Der Firmware-Build für „{label}“ ist fehlgeschlagen.",
                    device_id=device.id,
                )
            )
        elif status in ("idle", "queued", "running"):
            if status == "idle":
                problems.append(
                    Problem(
                        kind="not_built",
                        message=f"Für „{label}“ wurde noch keine Firmware gebaut.",
                        device_id=device.id,
                    )
                )
        elif state is not None and not state.get("available"):
            problems.append(
                Problem(
                    kind="device_unavailable",
                    message=(
                        f"„{label}“ ist geflasht, aber Home Assistant liefert keine "
                        "Werte dafür."
                    ),
                    device_id=device.id,
                )
            )

        # The password only reaches a device by being compiled into it.
        # Generating one for the record changes nothing on hardware that
        # is already flashed, so the migration has to be asked for rather
        # than assumed — and the manifest is what distinguishes a device
        # built with a closed fallback AP from one built before 0.13.5.
        if status == "success" and not (device.build_manifest or {}).get(
            "fallback_ap_secured"
        ):
            problems.append(
                Problem(
                    kind="fallback_ap_open",
                    message=(
                        f"„{label}“ wurde gebaut, als der Notfall-Access-Point noch "
                        "ohne Passwort war. Neu bauen und flashen schließt ihn."
                    ),
                    device_id=device.id,
                )
            )

        # A profile written under an older definition is not used, and
        # saying nothing would leave the device quietly contributing
        # nothing to rate-based presence while its card still shows a
        # profile. The measurement changed; the recalibration is real
        # work and has to be asked for.
        if presence_rate.profile_outdated(device.presence_profile):
            problems.append(
                Problem(
                    kind="profile_outdated",
                    message=(
                        f"Das Präsenzprofil von „{label}“ stammt aus einer älteren "
                        "Auswertung und wird nicht mehr verwendet. Eine Leer-Aufnahme "
                        "neu übernehmen."
                    ),
                    tab="calibration",
                    device_id=device.id,
                )
            )

        # Costs nothing extra: the threshold is already in the state that
        # was read for the live view. It is worth saying here rather than
        # only in the per-device diagnosis, because it affects every
        # device Echolot builds — the firmware adopts the algorithm's own
        # default threshold only when the algorithm was chosen at runtime.
        if state is not None and state.get("available"):
            mismatch = health.check_threshold_profile(
                device.config.detection_algorithm,
                {"state": state.get("threshold")},
            )
            if mismatch is not None:
                problems.append(
                    Problem(
                        kind=mismatch.kind,
                        message=f"„{label}“: {mismatch.message}",
                        device_id=device.id,
                    )
                )

        if device.ota_error:
            problems.append(
                Problem(
                    kind="ota_failed",
                    message=f"Das letzte WLAN-Update für „{label}“ ist fehlgeschlagen.",
                    device_id=device.id,
                )
            )

    for zone in zones_without_devices:
        problems.append(
            Problem(
                kind="zone_empty",
                message=f"Der Zone „{zone.name}“ ist kein Gerät zugeordnet.",
                tab="zones",
            )
        )

    # Only a problem if the user asked for the export in the first place.
    if mqtt_wanted and not mqtt_status.get("connected"):
        detail = mqtt_status.get("error")
        problems.append(
            Problem(
                kind="mqtt_down",
                message=(
                    "Zonen werden nicht an Home Assistant exportiert"
                    + (f": {detail}" if detail else " — es ist kein MQTT-Broker erreichbar.")
                ),
                tab="zones",
            )
        )

    return problems


def radio_load(devices: list, kb_per_pps: float) -> float:
    """Total Wi-Fi airtime the fleet spends probing, in KB/s."""
    return round(sum(d.config.csi_target_pps * kb_per_pps for d in devices), 1)
