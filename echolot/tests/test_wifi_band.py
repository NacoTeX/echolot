"""Which band a device measures on, and what a profile is worth on another.

The ESP32-C5 is the only chip here with two radios' worth of choice, and
until 0.13.8 Echolot made none: the template set no `band_mode:`, so
ESPHome's default applied. ESPectre reads exactly that key —

    def _runtime_wifi_band_policy():
        if get_esp32_variant() != esp32_const.VARIANT_ESP32C5:
            return "2g"
        band_mode = str(CORE.config[CONF_WIFI].get(CONF_BAND_MODE, "AUTO"))
        return _WIFI_BAND_POLICY_BY_MODE[band_mode]

(src/cpp/frontend/esphome/components/espectre/__init__.py at the pinned
ce23b0b6) — so a C5 built by Echolot came up on AUTO and associated
wherever the router pointed it. Upstream's own SETUP.md says of that
band: "Detection quality on 5 GHz is not characterized yet."

Two consequences, both tested here. A device could be measuring on a band
nobody has characterised without anyone choosing it, and a baseline could
be learned across a band change — the same measurement-definition problem
as the transport mismatch in 0.13.7, one layer down.

ESPHome accepts `band_mode` *only* on the C5
(`only_on_variant(supported=[VARIANT_ESP32C5])`, wifi/__init__.py), so
rendering it anywhere else is a config error rather than a no-op.
"""

import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import builder, devices, main, presence_rate, samples  # noqa: E402
from app.board_registry import BOARDS, get_board  # noqa: E402
from app.devices import Device, DeviceCreate  # noqa: E402
from app.telemetry import Sample  # noqa: E402

PROFILE = presence_rate.RateProfile(
    crossing_threshold=1e-3,
    baseline_rate=0.05,
    baseline_spread=0.01,
    window_seconds=60.0,
    sample_count=1200,
    observed_seconds=1200.0,
)


def make_device(board="esp32c5", device_id="probe", **config):
    return Device(
        id=device_id,
        created_at=0,
        updated_at=0,
        config=DeviceCreate(name=device_id, board=board, wifi_ssid="netz",
                            wifi_password="passwort123", **config),
    )


# --- the choice ---------------------------------------------------------


def test_only_the_c5_is_marked_dual_band():
    """The flag has to agree with ESPHome's own variant list, or the
    rendered config is rejected on one side or wrong on the other."""
    dual = {key for key, board in BOARDS.items() if board.dual_band}
    assert dual == {"esp32c5"}


def test_a_c5_created_now_measures_on_the_characterised_band():
    device = make_device()
    assert device.config.wifi_band == devices.BAND_24
    assert "band_mode: 2.4GHZ" in builder.render_yaml(device)


def test_the_rendered_band_is_the_one_that_was_chosen():
    for band, rendered in ((devices.BAND_5, "5GHZ"), (devices.BAND_AUTO, "AUTO")):
        yaml = builder.render_yaml(make_device(wifi_band=band))
        assert f"band_mode: {rendered}" in yaml, band


def test_a_single_band_board_is_never_given_a_band_mode():
    """ESPHome refuses the key outside the C5; a rendered one is a config
    error, not an ignored line."""
    for key, board in BOARDS.items():
        if board.dual_band:
            continue
        assert "band_mode" not in builder.render_yaml(make_device(board=key)), key


def test_a_band_the_board_has_no_radio_for_is_refused():
    for band in (devices.BAND_5, devices.BAND_AUTO):
        with pytest.raises(ValueError, match="ESP32-C5"):
            DeviceCreate(name="probe", board="esp32c6", wifi_ssid="netz",
                         wifi_password="passwort123", wifi_band=band)


def test_the_board_list_says_which_boards_offer_the_choice():
    """The form can only offer the field where it means something."""
    listed = {b["key"]: b for b in main.list_boards()}
    assert listed["esp32c5"]["dual_band"] is True
    assert listed["esp32c6"]["dual_band"] is False


# --- what was already flashed ------------------------------------------


def test_a_c5_stored_before_the_band_was_a_choice_keeps_its_own():
    """The migration must not repin an image that is already flashed.

    A C5 set up before 0.13.8 is running on AUTO — that is what was
    built. Defaulting it to 2.4 GHz would make the stored config describe
    a device that does not exist, and would quietly change what its
    baseline was learned under. It stays on AUTO until somebody chooses.
    """
    stored = {"name": "alt", "board": "esp32c5", "wifi_ssid": "netz",
              "wifi_password": "passwort123"}
    assert devices._migrate_config(stored) is True
    assert stored["wifi_band"] == devices.BAND_AUTO


def test_a_single_band_device_is_not_touched_by_that_migration():
    """Its firmware always ran on 2.4 GHz — ESPectre returns "2g" for
    every variant but the C5 — so the default already tells the truth."""
    stored = {"name": "alt", "board": "esp32c6", "wifi_ssid": "netz",
              "wifi_password": "passwort123"}
    devices._migrate_config(stored)
    assert "wifi_band" not in stored
    assert devices.effective_band(DeviceCreate(**stored)) == devices.BAND_24


def test_the_band_is_part_of_what_was_built():
    manifest = builder.build_manifest(make_device(wifi_band=devices.BAND_5), None)
    assert manifest["wifi_band"] == devices.BAND_5
    assert builder.build_manifest(make_device(board="esp32c6"), None)["wifi_band"] == (
        devices.BAND_24
    )


def test_changing_the_band_is_a_different_build():
    """Otherwise the fingerprint says two images are the same when one of
    them measures on a different radio."""
    assert builder.config_fingerprint(make_device()) != builder.config_fingerprint(
        make_device(wifi_band=devices.BAND_5)
    )


# --- what a baseline is worth on another band ---------------------------


def busy(device_id, *, crossings=40, count=60, span=60.0):
    end = time.time()
    samples.bus.forget(device_id)
    for index in range(count):
        samples.bus.publish(
            device_id,
            Sample(t=end - span + span * index / (count - 1),
                   movement_score=0.5 if index < crossings else 0.0,
                   threshold=0.0, motion=False),
            source=samples.SOURCE_HOME_ASSISTANT,
        )


@pytest.fixture
def clean(monkeypatch):
    monkeypatch.delenv("ECHOLOT_SAMPLE_SOURCE", raising=False)
    monkeypatch.setattr(main, "evaluator", main.ZoneEvaluator())
    main._rate_state.clear()
    yield
    samples.bus.forget("probe")
    main._rate_state.clear()


def test_a_baseline_learned_on_another_band_stops_judging(clean):
    """2.4 GHz and 5 GHz are different measurements of the same room.
    What the room does empty on one says nothing about the other, and
    upstream has not characterised the second at all."""
    device = make_device(wifi_band=devices.BAND_5)
    device.presence_profile = {**PROFILE.as_dict(), "band": devices.BAND_24}
    busy(device.id)
    main._rate_state[device.id] = {"remembered": True}

    verdict, reason = main._device_rate_evidence(device)

    assert verdict is None
    assert reason == main.RATE_BAND_MISMATCH
    assert device.id not in main._rate_state, "die Hysterese hat das Profil überlebt"


def test_a_baseline_learned_under_auto_does_not_carry_to_a_pinned_band(clean):
    """AUTO is not a band. A recording made under it cannot say which
    radio it was on, so it is not a baseline for either."""
    device = make_device(wifi_band=devices.BAND_24)
    device.presence_profile = {**PROFILE.as_dict(), "band": devices.BAND_AUTO}
    busy(device.id)

    assert main._device_rate_evidence(device) == (None, main.RATE_BAND_MISMATCH)


def test_a_baseline_from_before_the_band_was_recorded_still_judges(clean):
    """Same rule as the transport: an old profile recorded no band, and
    throwing every one of them away would be a worse answer than using
    them. Nothing about them changed."""
    device = make_device()
    device.presence_profile = PROFILE.as_dict()
    busy(device.id)

    assert main._device_rate_evidence(device)[0] is True


def test_the_same_band_judges_as_before(clean):
    device = make_device()
    device.presence_profile = {**PROFILE.as_dict(), "band": devices.BAND_24}
    busy(device.id)

    assert main._device_rate_evidence(device)[0] is True


def test_a_single_band_device_never_mismatches(clean):
    """It has one radio; a profile recorded on it is on it still."""
    device = make_device(board="esp32c6")
    device.presence_profile = {**PROFILE.as_dict(), "band": devices.BAND_24}
    busy(device.id)

    assert main._device_rate_evidence(device)[0] is True


def test_a_learned_profile_records_the_band_it_was_measured_on():
    """Stamped from the device, because the readings do not carry it —
    which is exactly why AUTO is worth flagging rather than trusting."""
    profile = presence_rate.RateProfile(
        crossing_threshold=1e-3, baseline_rate=0.05, baseline_spread=0.01,
        window_seconds=60.0, sample_count=1200, observed_seconds=1200.0,
        band=devices.BAND_5,
    )
    assert profile.as_dict()["band"] == devices.BAND_5
    assert presence_rate.profile_from_dict(profile.as_dict()).band == devices.BAND_5


def test_an_unreadable_band_does_not_take_the_profile_down():
    """Hand-edited files exist. A bad band is not a reason to lose the
    baseline — it is a reason to have no band."""
    parsed = presence_rate.profile_from_dict({**PROFILE.as_dict(), "band": 7})
    assert parsed is not None
    assert parsed.band == "7"


# --- the band a recording was made on ----------------------------------


def test_adopting_a_baseline_records_the_band_the_device_was_on(monkeypatch):
    """The whole point of the field: nothing writes it unless the apply
    step does, and then the mismatch check above has something to work
    with. Driven through the route rather than around it."""
    import csv

    from app import calibration, feature_api

    with open(Path(__file__).parent / "data" / "flat_occupied_room_empty.csv") as fh:
        rows = [
            {"t": float(r["t"]), "movement_score": float(r["movement_score"]),
             "label": r["label"]}
            for r in csv.DictReader(fh)
            if r["movement_score"] and r["label"] == "empty"
        ]

    device = make_device(wifi_band=devices.BAND_5)
    saved = {}
    monkeypatch.setattr(calibration.store, "samples", lambda sid: rows)
    monkeypatch.setattr(calibration.store, "get", lambda sid: {"device_id": device.id})
    monkeypatch.setattr(feature_api.devices, "get_device", lambda did: device)
    monkeypatch.setattr(feature_api.devices, "save_device", lambda d: saved.update(d.presence_profile))

    result = feature_api.apply_presence_rate("sitzung")

    assert result["profile"]["band"] == devices.BAND_5
    assert saved["band"] == devices.BAND_5


def test_the_overview_says_which_band_the_baseline_came_from(monkeypatch):
    """A silent None verdict is not an explanation. The overview names
    the band the profile was learned on and the one the device is on."""
    from app import overview

    device = make_device(wifi_band=devices.BAND_5)
    device.presence_profile = {**PROFILE.as_dict(), "band": devices.BAND_24}

    problems = overview.collect_problems(
        device_states=[(device, None)],
        zones_without_devices=[],
        mqtt_status={"connected": True},
        mqtt_wanted=True,
        esphome={"available": True, "version": "ESPHome 2026.6.5"},
    )

    band = [p for p in problems if p.kind == "profile_band_mismatch"]
    assert len(band) == 1
    assert "2,4 GHz" in band[0].message and "5 GHz" in band[0].message
    assert band[0].device_id == device.id


def test_an_unrelated_update_does_not_repin_a_stored_c5(tmp_path, monkeypatch):
    """`update_device` validated the raw stored record and wrote the
    result back, so the new field's default landed on a device that had
    never been asked — on any update at all, including the address the
    entity resolver writes by itself. The migration has to run on that
    path too, or it only holds until something else touches the record.
    """
    monkeypatch.setattr(devices, "INDEX_PATH", tmp_path / "devices.json")
    monkeypatch.setattr(devices, "DEVICES_DIR", tmp_path / "devices")
    devices._write_index({
        "alt": {
            "id": "alt", "created_at": 0, "updated_at": 0,
            "api_encryption_key": "k", "ota_password": "p",
            # As stored before 0.13.8: no band at all.
            "config": {"name": "alt", "board": "esp32c5", "wifi_ssid": "netz",
                       "wifi_password": "passwort123"},
        }
    })

    updated = devices.update_device("alt", address="192.0.2.7")

    assert updated.config.wifi_band == devices.BAND_AUTO
    assert devices.get_device("alt").config.wifi_band == devices.BAND_AUTO


def test_the_band_is_settled_when_the_device_is_created():
    """A limit, stated rather than papered over.

    The band is a firmware field like the board and the SSID: it is baked
    into an image, so changing it means rebuilding and reflashing. Echolot
    has no notion of "the config has moved on from the flashed image", so
    `DeviceUpdate` carries no firmware fields at all and this one is no
    exception. What that means in practice: the mismatch check above is a
    guard on restored or hand-edited data today, and the band on a profile
    is recorded so that a *later* band change cannot quietly reuse a
    baseline learned under the old one.
    """
    from app.devices import DeviceUpdate

    assert "wifi_band" not in DeviceUpdate.model_fields
    assert not {"board", "wifi_ssid", "csi_target_pps"} & set(DeviceUpdate.model_fields)
