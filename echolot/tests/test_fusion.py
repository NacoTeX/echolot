"""Confidence fusion turns calibrated direct samples into explainable zones."""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import fusion  # noqa: E402


PROFILE = {
    "enter_threshold": 2.0,
    "exit_threshold": 1.0,
    "noise": 0.2,
    "quality": "good",
}


def evidence(score, *, stamp=100, profile=PROFILE, motion=None):
    return fusion.device_evidence(
        {"t": stamp, "movement_score": score, "threshold": 2.0, "motion": motion},
        profile,
        now=100,
    )


def test_calibrated_scores_produce_strong_explainable_evidence():
    occupied = evidence(3.0)
    vacant = evidence(0.2)

    assert occupied["probability"] > 0.99
    assert vacant["probability"] < 0.01
    assert occupied["reliability"] == 1.0
    assert occupied["basis"] == "calibrated_score"


def test_freshness_degrades_then_expires_without_turning_into_vacancy():
    fresh = fusion.device_evidence({"t": 100, "motion": True}, None, now=100)
    aging = fusion.device_evidence({"t": 90, "motion": True}, None, now=100)
    stale = fusion.device_evidence({"t": 70, "motion": True}, None, now=100)

    assert fresh["reliability"] > aging["reliability"] > stale["reliability"]
    assert stale["reliability"] == 0
    assert fusion.fuse([stale])["state"] == "unavailable"


def test_agreeing_sensors_make_a_confident_zone():
    members = [evidence(3.0), evidence(2.8)]
    result = fusion.fuse(members)

    assert result["state"] == "occupied"
    assert result["occupied"] is True
    assert result["confidence"] > 0.99
    assert result["agreement"] > 0.99


def test_disagreement_is_reported_as_uncertain_instead_of_forced_to_or():
    result = fusion.fuse([evidence(3.0), evidence(0.2)])

    assert result["state"] == "uncertain"
    assert result["occupied"] is None
    assert 0.45 < result["confidence"] < 0.55
    assert result["agreement"] < 0.1


class Hub:
    def __init__(self, points):
        self.points = points

    def snapshot(self, device_id, seconds):
        return {"points": self.points.get(device_id, [])}


def test_zone_result_names_each_devices_basis_and_reliability():
    zone = SimpleNamespace(id="z", name="Wohnzimmer", device_ids=["a", "b"])
    devices = {
        key: SimpleNamespace(config=SimpleNamespace(name=key, friendly_name=None))
        for key in ("a", "b")
    }
    hub = Hub(
        {
            "a": [{"t": 100, "movement_score": 3.0, "threshold": 2.0, "motion": True}],
            "b": [{"t": 100, "movement_score": 2.5, "threshold": 2.0, "motion": True}],
        }
    )

    result = fusion.evaluate_zone(
        zone, devices.get, hub, {"a": PROFILE}, now=100
    )

    assert result["zone_id"] == "z"
    assert result["state"] == "occupied"
    assert result["members"][0]["basis"] == "calibrated_score"
    assert result["members"][1]["basis"] == "device_threshold"
