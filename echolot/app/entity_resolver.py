"""Find a flashed device's entities by asking Home Assistant what it named them.

Echolot used to predict entity ids from the ESPHome node name:
`binary_sensor.<node>_motion_detected`. That is wrong whenever a device has
a friendly name, which is the normal case. The chain is:

  * ESPHome's `EntityBase` sends `object_id = snake_case(entity name)` —
    just `motion_detected`, with no device prefix (core/entity_base.cpp).
  * Home Assistant's ESPHome integration attaches that entity to a device
    whose name is the config's `friendly_name` (falling back to `name`),
    and builds the entity id as `<domain>.<device name>_<entity name>`.

So a node named `flur` with friendly name "Flur unten" produces
`binary_sensor.flur_unten_motion_detected`, and the old prediction missed
it — leaving the device permanently "nicht verfügbar" in the UI even
though the flash worked perfectly.

Rather than reproduce Home Assistant's slug rules (which transliterate
umlauts differently than ours do, and append `_2` on collisions), match on
the one thing Home Assistant states outright: the `friendly_name`
attribute, which it sets to "<device name> <entity name>". Slug prediction
stays only as a fallback.
"""

import re
import unicodedata

#: field name -> (entity domain, the `name:` our firmware template gives it)
#: The labels come from app/templates/espectre.yaml.j2 and must track it.
ENTITY_SPECS: dict[str, tuple[str, str]] = {
    "entity_motion": ("binary_sensor", "Motion Detected"),
    "entity_movement_score": ("sensor", "Movement Score"),
    "entity_threshold": ("number", "Threshold"),
    # ESPectre replaced the "Calibrate" switch with a "Recalibrate"
    # button when it restructured; a switch that has to be turned
    # off again was always the wrong shape for a one-shot action.
    "entity_calibrate": ("button", "Recalibrate"),
}


def _slug(text: str) -> str:
    """Approximate Home Assistant's slugify for the fallback path.

    Only an approximation: Home Assistant transliterates via unidecode, so
    "Küche" becomes "kuche" there and "kueche" under a German-aware
    scheme. Both spellings are offered as candidates rather than picking
    one and hoping.
    """
    decomposed = unicodedata.normalize("NFKD", text.lower())
    stripped = decomposed.encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", stripped).strip("_")


def _slug_german(text: str) -> str:
    lowered = text.lower()
    for source, target in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        lowered = lowered.replace(source, target)
    return _slug(lowered)


def device_display_name(device) -> str:
    """What Home Assistant will have called this device.

    Mirrors ESPHome's own fallback: friendly_name when set, else the node
    name.
    """
    return device.config.friendly_name or device.config.name


def resolve(device, states: list[dict]) -> dict[str, str]:
    """Map entity fields to the ids Home Assistant actually uses.

    Returns only the fields that were found, so a partial match still
    improves on a wrong guess. An empty result means Home Assistant knows
    nothing about this device — usually because the ESPHome integration
    has not adopted it yet.
    """
    display = device_display_name(device)
    candidates = {_slug(display), _slug_german(display), _slug(device.config.name)}

    found: dict[str, str] = {}
    for field, (domain, label) in ENTITY_SPECS.items():
        prefix = f"{domain}."

        # Primary: Home Assistant states the full name outright.
        wanted = f"{display} {label}".casefold()
        match = next(
            (
                s["entity_id"]
                for s in states
                if s.get("entity_id", "").startswith(prefix)
                and str(s.get("attributes", {}).get("friendly_name", "")).casefold() == wanted
            ),
            None,
        )

        # Fallback: predict the id, trying each plausible device slug.
        if match is None:
            known = {s.get("entity_id") for s in states}
            suffix = _slug(label)
            match = next(
                (
                    guess
                    for cand in sorted(candidates)
                    if cand and (guess := f"{prefix}{cand}_{suffix}") in known
                ),
                None,
            )

        if match:
            found[field] = match
    return found
