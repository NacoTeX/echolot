"""Tests for finding a device's entities in Home Assistant.

The bug these guard against: Echolot predicted entity ids from the ESPHome
*node* name, but Home Assistant builds them from the *device* name, which
is the config's friendly_name. Every device with a friendly name therefore
read as permanently unavailable despite a working sensor.

The `states` fixtures below use Home Assistant's real /api/states shape.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import entity_resolver  # noqa: E402
from app.devices import DeviceCreate, default_entity_ids  # noqa: E402


class FakeDevice:
    def __init__(self, name, friendly_name=None):
        self.config = DeviceCreate(
            name=name, friendly_name=friendly_name, board="esp32c6",
            wifi_ssid="netz", wifi_password="passwort123",
        )


def state(entity_id, friendly_name):
    return {"entity_id": entity_id, "state": "off",
            "attributes": {"friendly_name": friendly_name}}


def espectre_states(device_name, prefix=None):
    """What Home Assistant exposes for one flashed ESPectre node."""
    prefix = prefix if prefix is not None else device_name
    return [
        state(f"binary_sensor.{prefix}_motion_detected", f"{device_name} Motion Detected"),
        state(f"sensor.{prefix}_movement_score", f"{device_name} Movement Score"),
        state(f"number.{prefix}_threshold", f"{device_name} Threshold"),
        state(f"button.{prefix}_recalibrate", f"{device_name} Recalibrate"),
    ]


def test_the_default_guess_follows_the_friendly_name():
    """Regression: the node name produced ids Home Assistant never creates."""
    guessed = default_entity_ids("flur", "Flur unten")
    assert guessed["entity_motion"] == "binary_sensor.flur_unten_motion_detected"


def test_the_default_guess_falls_back_to_the_node_name():
    guessed = default_entity_ids("flur", None)
    assert guessed["entity_motion"] == "binary_sensor.flur_motion_detected"


def test_it_finds_entities_named_after_the_friendly_name():
    device = FakeDevice("flur", "Flur unten")
    found = entity_resolver.resolve(device, espectre_states("Flur unten", "flur_unten"))
    assert found == {
        "entity_motion": "binary_sensor.flur_unten_motion_detected",
        "entity_movement_score": "sensor.flur_unten_movement_score",
        "entity_threshold": "number.flur_unten_threshold",
        "entity_calibrate": "button.flur_unten_recalibrate",
    }


def test_it_finds_entities_when_there_is_no_friendly_name():
    device = FakeDevice("flur")
    found = entity_resolver.resolve(device, espectre_states("flur"))
    assert found["entity_motion"] == "binary_sensor.flur_motion_detected"


def test_it_survives_home_assistants_collision_suffix():
    """A second device of the same name gets `_2` appended — the
    friendly_name attribute still identifies it, so matching on that
    rather than on a predicted id keeps working."""
    device = FakeDevice("flur", "Flur unten")
    states = espectre_states("Flur unten", "flur_unten_2")
    assert entity_resolver.resolve(device, states)["entity_motion"] == (
        "binary_sensor.flur_unten_2_motion_detected"
    )


def test_it_handles_umlauts_whichever_way_home_assistant_transliterated():
    """"Küche" slugs to `kuche` in Home Assistant and `kueche` under a
    German-aware scheme; neither spelling may be a coin flip."""
    device = FakeDevice("kueche", "Küche")
    for prefix in ("kuche", "kueche"):
        found = entity_resolver.resolve(device, espectre_states("Küche", prefix))
        assert found["entity_motion"] == f"binary_sensor.{prefix}_motion_detected"


def test_it_does_not_claim_another_devices_entities():
    """Every ESPectre node has a `_motion_detected` entity. Matching on the
    suffix alone would hand one device another's sensor."""
    device = FakeDevice("flur", "Flur unten")
    assert entity_resolver.resolve(device, espectre_states("Wohnzimmer", "wohnzimmer")) == {}


def test_an_unadopted_device_resolves_to_nothing():
    """Flashed but not yet added in Home Assistant: no entities exist, and
    the caller has to say so rather than invent an id."""
    device = FakeDevice("flur", "Flur unten")
    assert entity_resolver.resolve(device, []) == {}


def test_a_partial_match_still_returns_what_was_found():
    device = FakeDevice("flur", "Flur unten")
    states = [state("binary_sensor.flur_unten_motion_detected", "Flur unten Motion Detected")]
    found = entity_resolver.resolve(device, states)
    assert list(found) == ["entity_motion"]


def test_matching_ignores_case_differences():
    device = FakeDevice("flur", "Flur Unten")
    states = [state("binary_sensor.flur_unten_motion_detected", "flur unten motion detected")]
    assert "entity_motion" in entity_resolver.resolve(device, states)


def test_one_device_whose_entity_ids_carry_two_different_prefixes():
    """Observed on real hardware, and the reason this matches on names.

    A device renamed in Home Assistant keeps the old slug on the entities
    that already existed and mints new ones under the new slug. The result
    is a single device answering under two prefixes at once — here the
    sensing entities under `test1_` and everything else under
    `zuhause_test1_`, with `test11` as the display name throughout.

    No prefix guess can resolve that. The friendly_name attribute can,
    because Home Assistant sets it to "<device name> <entity name>"
    regardless of which slug the id ended up with.
    """
    # Echolot node names use hyphens; Home Assistant turns those into
    # underscores when it builds an entity id.
    device = FakeDevice("zuhause-test1", friendly_name="test11")
    states = [
        # Minted after the rename.
        state("binary_sensor.test1_motion_detected", "test11 Motion Detected"),
        state("sensor.test1_movement_score", "test11 Movement Score"),
        state("number.test1_threshold", "test11 Threshold"),
        # Minted before it, under the original name.
        state("button.zuhause_test1_recalibrate", "test11 Recalibrate"),
        state("sensor.zuhause_test1_uptime", "test11 Uptime"),
    ]

    found = entity_resolver.resolve(device, states)

    assert found == {
        "entity_motion": "binary_sensor.test1_motion_detected",
        "entity_movement_score": "sensor.test1_movement_score",
        "entity_threshold": "number.test1_threshold",
        "entity_calibrate": "button.zuhause_test1_recalibrate",
    }
