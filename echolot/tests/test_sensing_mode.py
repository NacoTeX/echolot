"""Two ways a room could be measured, and only one that exists.

Stufe 1 of the peer-mode work order, bounded to what promises nothing:
a device says which topology it is measuring in, a baseline records the
one it was learned under, and the second topology cannot be selected
until firmware reports that it can do it.

Why the gate rather than a plain enum: `csi_traffic_mode: external` at
the pinned ESPectre commit is a way to *supply* CSI-triggering traffic
through the access point, not a direct A→B radio link — a UDP packet
from A to B's IP still travels via the AP, and an IP source address is
no proof of the immediate 802.11 sender. So nothing in the firmware
Echolot ships today measures a peer link, and the honest way to say that
is a capability the firmware has to report, not a switch in a form.

Nothing sets those capabilities in production: the builder writes them
from the pinned commit, and that commit has none. The tests below supply
them as data, which is the only way to exercise both sides of a gate
that is closed.
"""

import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import builder, devices, firmware, main, presence_rate, samples  # noqa: E402
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


def make_device(device_id="probe", board="esp32c6", **config):
    return Device(
        id=device_id, created_at=0, updated_at=0,
        config=DeviceCreate(name=device_id, board=board, wifi_ssid="netz",
                            wifi_password="passwort123", **config),
    )


def peer_capable(device):
    """A device whose *flashed image* claims it can receive a peer link.

    Written into the build manifest, because that is where "what is on
    this chip" is recorded. Nothing produces this today — it is the
    fixture that lets the closed gate be tested from both sides.
    """
    device.build_manifest = {
        "espectre_ref": "0" * 40,
        "firmware_capabilities": {
            **firmware.CAPABILITIES,
            "supports_peer_rx": True,
            "supports_peer_tx": True,
            "peer_protocol_version": 1,
        },
    }
    return device


# --- what the shipped firmware can do -----------------------------------


def test_the_pinned_firmware_reports_router_sensing_and_nothing_else():
    """Not an assumption about ESPectre — a statement about it, in one
    place, next to the commit it describes."""
    assert firmware.CAPABILITIES["supports_router"] is True
    assert firmware.CAPABILITIES["supports_peer_tx"] is False
    assert firmware.CAPABILITIES["supports_peer_rx"] is False
    assert firmware.CAPABILITIES["peer_protocol_version"] is None


def test_the_capabilities_are_recorded_with_the_image():
    """So a device flashed a year ago can still say what it can do,
    rather than being judged by whatever the add-on ships today."""
    manifest = builder.build_manifest(make_device(), None)
    assert manifest["firmware_capabilities"] == firmware.CAPABILITIES
    assert manifest["espectre_ref"] == firmware.ESPECTRE_REF


def test_a_device_measures_against_the_router_unless_told_otherwise():
    assert make_device().config.sensing_mode == devices.MODE_ROUTER


def test_a_device_stored_before_the_field_existed_is_a_router_device():
    """Which is what it is: every image Echolot has ever built measures
    the access point's traffic."""
    stored = {"name": "alt", "board": "esp32c6", "wifi_ssid": "netz",
              "wifi_password": "passwort123"}
    assert DeviceCreate(**stored).sensing_mode == devices.MODE_ROUTER


# --- the gate -----------------------------------------------------------


def test_a_peer_link_cannot_be_asked_for_while_no_firmware_offers_one():
    with pytest.raises(ValueError, match="Funklink"):
        DeviceCreate(name="paar", board="esp32c6", wifi_ssid="netz",
                     wifi_password="passwort123",
                     sensing_mode=devices.MODE_PEER_LINK)


def test_the_refusal_names_the_capability_that_is_missing():
    """"Not supported" is not an explanation. A reader has to be able to
    tell this from a typo or a permissions problem."""
    with pytest.raises(ValueError) as err:
        DeviceCreate(name="paar", board="esp32c6", wifi_ssid="netz",
                     wifi_password="passwort123",
                     sensing_mode=devices.MODE_PEER_LINK)
    assert "supports_peer_rx" in str(err.value)


def test_a_device_reports_the_modes_its_own_image_supports():
    plain = make_device()
    assert devices.available_sensing_modes(plain) == [devices.MODE_ROUTER]
    assert devices.available_sensing_modes(peer_capable(plain)) == [
        devices.MODE_ROUTER, devices.MODE_PEER_LINK
    ]


def test_an_unbuilt_device_is_judged_by_the_firmware_it_would_get():
    """It has no image yet, so the pinned commit is the only honest
    answer — and it is the one the next build would produce."""
    assert make_device().build_manifest is None
    assert devices.available_sensing_modes(make_device()) == [devices.MODE_ROUTER]


def test_a_manifest_from_before_capabilities_were_recorded_reads_as_router():
    device = make_device()
    device.build_manifest = {"espectre_ref": "0" * 40, "board": "esp32c6"}
    assert devices.available_sensing_modes(device) == [devices.MODE_ROUTER]


# --- the baseline knows which topology it describes ----------------------


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


def test_a_router_baseline_does_not_judge_a_peer_link(clean):
    """A→B is a different radio path from AP→B: different geometry,
    different multipath, different empty room. The reviewer's rule —
    router profiles are never adopted silently."""
    device = peer_capable(make_device())
    device.config.sensing_mode = devices.MODE_PEER_LINK
    device.presence_profile = {**PROFILE.as_dict(), "sensing_mode": devices.MODE_ROUTER}
    busy(device.id)
    main._rate_state[device.id] = {"remembered": True}

    verdict, reason = main._device_rate_evidence(device)

    assert verdict is None
    assert reason == main.RATE_MODE_MISMATCH
    assert device.id not in main._rate_state


def test_a_baseline_from_before_the_mode_was_recorded_still_judges(clean):
    """Every one of them was learned against the router, which is what
    the device is doing. Nothing about them changed."""
    device = make_device()
    device.presence_profile = PROFILE.as_dict()
    busy(device.id)

    assert main._device_rate_evidence(device)[0] is True


def test_a_router_baseline_judges_a_router_device(clean):
    device = make_device()
    device.presence_profile = {**PROFILE.as_dict(), "sensing_mode": devices.MODE_ROUTER}
    busy(device.id)

    assert main._device_rate_evidence(device)[0] is True


def test_the_mode_survives_a_profile_round_trip():
    profile = presence_rate.RateProfile(
        crossing_threshold=1e-3, baseline_rate=0.05, baseline_spread=0.01,
        window_seconds=60.0, sample_count=1200, observed_seconds=1200.0,
        sensing_mode=devices.MODE_PEER_LINK,
    )
    stored = profile.as_dict()
    assert stored["sensing_mode"] == devices.MODE_PEER_LINK
    assert presence_rate.profile_from_dict(stored).sensing_mode == devices.MODE_PEER_LINK


def test_one_definition_decides_all_three_mismatches(clean):
    """Transport, band and topology are the same question asked three
    times: under what conditions was this baseline true. They are
    compared as one recorded definition, so a fourth condition is a row
    rather than a fourth branch."""
    device = make_device(board="esp32c5", wifi_band=devices.BAND_5)
    recorded = presence_rate.measurement_definition(
        presence_rate.profile_from_dict({
            **PROFILE.as_dict(), "source": samples.SOURCE_DIRECT,
            "band": devices.BAND_24, "sensing_mode": devices.MODE_ROUTER,
        })
    )
    current = main._measurement_now(device)

    assert set(recorded) == set(current) == {"source", "band", "sensing_mode"}
    assert recorded["band"] != current["band"]
    assert recorded["sensing_mode"] == current["sensing_mode"]


def test_adopting_a_baseline_records_the_topology_it_was_measured_in(monkeypatch):
    import csv

    from app import calibration, feature_api

    with open(Path(__file__).parent / "data" / "flat_occupied_room_empty.csv") as fh:
        rows = [
            {"t": float(r["t"]), "movement_score": float(r["movement_score"]),
             "label": r["label"]}
            for r in csv.DictReader(fh)
            if r["movement_score"] and r["label"] == "empty"
        ]

    device = make_device()
    monkeypatch.setattr(calibration.store, "samples", lambda sid: rows)
    monkeypatch.setattr(calibration.store, "get", lambda sid: {"device_id": device.id})
    monkeypatch.setattr(feature_api.devices, "get_device", lambda did: device)
    monkeypatch.setattr(feature_api.devices, "save_device", lambda d: None)

    result = feature_api.apply_presence_rate("sitzung")

    assert result["profile"]["sensing_mode"] == devices.MODE_ROUTER


def test_the_overview_names_a_topology_change(monkeypatch):
    from app import overview

    device = peer_capable(make_device())
    device.config.sensing_mode = devices.MODE_PEER_LINK
    device.presence_profile = {**PROFILE.as_dict(), "sensing_mode": devices.MODE_ROUTER}

    problems = overview.collect_problems(
        device_states=[(device, None)],
        zones_without_devices=[],
        mqtt_status={"connected": True},
        mqtt_wanted=True,
        esphome={"available": True, "version": "ESPHome 2026.6.5"},
    )

    mode = [p for p in problems if p.kind == "profile_mode_mismatch"]
    assert len(mode) == 1
    assert devices.MODE_ROUTER in mode[0].message
    assert devices.MODE_PEER_LINK in mode[0].message
