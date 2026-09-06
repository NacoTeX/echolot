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
