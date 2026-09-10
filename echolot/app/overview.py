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

from app import devices as devices_module
from app import health, presence_rate, samples


#: How the three band values read to somebody who is not reading YAML.
_BAND_LABELS = {
    devices_module.BAND_24: "2,4 GHz",
    devices_module.BAND_5: "5 GHz",
    devices_module.BAND_AUTO: "automatisch gewähltem Band",
}


def _band_label(band: str) -> str:
    return _BAND_LABELS.get(band, band)


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
        # Three different situations, and they need three different things
        # from the person: recalibrate, look at the file, or nothing.
        status = presence_rate.profile_status(device.presence_profile)
        if status == presence_rate.OUTDATED:
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
        elif status == presence_rate.MALFORMED:
            problems.append(
                Problem(
                    kind="profile_malformed",
                    message=(
                        f"Das Präsenzprofil von „{label}“ ist beschädigt und wird "
                        "übersprungen — die übrigen Räume laufen weiter. Eine "
                        "Leer-Aufnahme neu übernehmen ersetzt es."
                    ),
                    tab="calibration",
                    device_id=device.id,
                )
            )
        elif status == presence_rate.USABLE:
            # What a room does empty is measured through one transport.
            # Judging live readings from another one against it compares
            # two different measurements — which is not a crash, and not
            # something anybody would notice either.
            learned_under = (device.presence_profile or {}).get("source")
            canonical = samples.bus.source
            if learned_under and learned_under != canonical:
                problems.append(
                    Problem(
                        kind="profile_source_mismatch",
                        message=(
                            f"Das Präsenzprofil von „{label}“ wurde über "
                            f"„{learned_under}“ gelernt, gemessen wird jetzt über "
                            f"„{canonical}“. Die beiden Wege lösen unterschiedlich "
                            "fein auf — eine Leer-Aufnahme über die aktuelle Quelle "
                            "neu übernehmen."
                        ),
                        tab="calibration",
                        device_id=device.id,
                    )
                )

            # The same argument on the radio. Only the C5 can move
            # between bands at all, and ESPectre's own SETUP.md says
            # detection quality on 5 GHz is not characterised — so a
            # baseline carried across is not a baseline.
            learned_band = (device.presence_profile or {}).get("band")
            band_now = devices_module.effective_band(device.config)
            if learned_band and learned_band != band_now:
                problems.append(
                    Problem(
                        kind="profile_band_mismatch",
                        message=(
                            f"Das Präsenzprofil von „{label}“ wurde auf "
                            f"{_band_label(learned_band)} gelernt, das Gerät misst "
                            f"jetzt auf {_band_label(band_now)}. Die beiden Bänder "
                            "sind zwei Messungen desselben Raums — eine "
                            "Leer-Aufnahme auf dem aktuellen Band neu übernehmen."
                        ),
                        tab="calibration",
                        device_id=device.id,
                    )
                )

            # And the radio path. AP→B and A→B are different geometry
            # through a different part of the room; a baseline from one
            # is not a scale for the other.
            learned_mode = (device.presence_profile or {}).get("sensing_mode")
            if learned_mode and learned_mode != device.config.sensing_mode:
                problems.append(
                    Problem(
                        kind="profile_mode_mismatch",
                        message=(
                            f"Das Präsenzprofil von „{label}“ wurde im Modus "
                            f"„{learned_mode}“ gelernt, das Gerät misst jetzt im "
                            f"Modus „{device.config.sensing_mode}“. Das sind zwei "
                            "verschiedene Funkstrecken — eine Leer-Aufnahme im "
                            "aktuellen Modus neu übernehmen."
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

        # The config moved and the chip did not. Not an error — the
        # device works, it just works the way it was last flashed — but
        # somebody changed a setting and is entitled to know it has not
        # arrived yet.
        if device.firmware_behind_config:
            problems.append(
                Problem(
                    kind="firmware_behind_config",
                    message=(
                        f"„{label}“ wurde umkonfiguriert, aber noch nicht neu "
                        "gebaut. Auf dem Gerät läuft weiter das zuletzt "
                        "geflashte Image — Firmware neu bauen und übertragen."
                    ),
                    tab="devices",
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
