"""Tests for the device diagnosis.

Several cases carry values actually read off the first two devices this
project ever ran on, so the findings stay tied to real readings rather
than to imagined ones.

One exception is marked below: the missing-sensing-entities case is
constructed. The device 0.12.6 cited for it was not actually missing
them — see the 0.12.8 entry in CHANGELOG.md.
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import health  # noqa: E402


def state(value):
    return {"state": value}


ALL_CORE = set(health.CORE_ENTITY_LABELS)


# --- threshold vs. profile -------------------------------------------------


def test_the_threshold_read_off_real_hardware_is_recognised():
    # test22 ran high_accuracy and reported exactly the lightweight default.
    finding = health.check_threshold_profile("high_accuracy", state("0.6621854"))
    assert finding is not None
    assert finding.kind == "threshold_profile_mismatch"
    assert finding.action == "recalibrate"
    assert "lightweight" in finding.message
    assert "high_accuracy" in finding.message
    assert "0.5" in finding.message


def test_the_matching_default_is_not_a_finding():
    assert health.check_threshold_profile("high_accuracy", state("0.5")) is None
    assert health.check_threshold_profile("lightweight", state("0.6621854538596202")) is None


def test_a_value_someone_chose_is_left_alone():
    # Not either default, so it was set deliberately; saying anything
    # about it would just be second-guessing the user.
    assert health.check_threshold_profile("high_accuracy", state("0.42")) is None


def test_an_unreported_threshold_says_nothing():
    for missing in (None, state("unknown"), state("unavailable"), state(""), state("n/a")):
        assert health.check_threshold_profile("high_accuracy", missing) is None


# --- missing core entities -------------------------------------------------


def test_a_device_answering_without_its_sensing_entities_is_a_blocker():
    # A device answering with its diagnostic and control entities but
    # none of the three that matter. Hypothetical, not observed: the
    # device 0.12.6 blamed turned out to have all three under a second
    # entity-id prefix (see 0.12.8 and test_entity_resolver.py).
    finding = health.check_core_entities({"entity_calibrate"}, reachable=True)
    assert finding is not None
    assert finding.severity == "blocker"
    assert finding.action == "rebuild"
    for label in ("Motion Detected", "Movement Score", "Threshold"):
        assert label in finding.message


def test_a_complete_device_is_not_flagged():
    assert health.check_core_entities(ALL_CORE, reachable=True) is None


def test_a_silent_device_is_left_to_the_overview():
    # Nothing published at all is a different problem with different
    # wording; reporting stale firmware for it would be wrong.
    assert health.check_core_entities(set(), reachable=False) is None


# --- diagnostics -----------------------------------------------------------


def test_diagnostics_that_never_reported_are_flagged_once():
    diagnostics = {field: state("unknown") for field in health.DIAGNOSTIC_LABELS}
    finding = health.check_diagnostics(diagnostics)
    assert finding is not None
    assert finding.action == "refresh_diagnostics"


def test_one_reported_diagnostic_is_enough_to_stay_quiet():
    diagnostics = {field: state("unknown") for field in health.DIAGNOSTIC_LABELS}
    diagnostics["diag_traffic_tx_rate"] = state("0")
    assert health.check_diagnostics(diagnostics) is None


def test_zero_is_a_value_not_a_missing_reading():
    # The distinction the whole module rests on: "no CSI arriving" must
    # not read the same as "never asked".
    assert health._number(state("0")) == 0.0
    assert health._number(state("unknown")) is None


# --- CSI rate --------------------------------------------------------------


def test_a_starved_radio_is_a_blocker():
    finding = health.check_csi_rate(state("3"), target_pps=100)
    assert finding is not None
    assert finding.severity == "blocker"
    assert "3" in finding.message and "100" in finding.message


def test_a_healthy_rate_says_nothing():
    assert health.check_csi_rate(state("95"), target_pps=100) is None
    # Exactly at the boundary counts as healthy.
    assert health.check_csi_rate(state("25"), target_pps=100) is None


def test_an_unread_rate_is_not_treated_as_zero():
    assert health.check_csi_rate(state("unknown"), target_pps=100) is None
    assert health.check_csi_rate(None, target_pps=100) is None


# --- score reachability ----------------------------------------------------


def test_a_threshold_nothing_approaches_is_pointed_out():
    # The real spread: scores around 1e-6 against a threshold of 0.66.
    finding = health.check_score_reachability(
        state("0.6621854"), [1e-7, 9.2e-6, 4.3e-6], window_minutes=15
    )
    assert finding is not None
    assert finding.severity == "info"
    assert "Gehtest" in finding.message


def test_a_score_that_gets_close_is_not_mentioned():
    assert (
        health.check_score_reachability(state("0.5"), [0.02, 0.41], window_minutes=15)
        is None
    )


def test_no_history_means_no_claim():
    assert health.check_score_reachability(state("0.5"), [], window_minutes=15) is None


# --- ordering --------------------------------------------------------------


def test_findings_come_back_worst_first():
    findings = health.inspect(
        profile="high_accuracy",
        target_pps=100,
        present_fields={"entity_calibrate"},
        reachable=True,
        threshold_state=state("0.6621854"),
        diagnostic_states={f: state("unknown") for f in health.DIAGNOSTIC_LABELS},
        observed_scores=[1e-6],
        window_minutes=15,
    )
    severities = [f.severity for f in findings]
    assert severities == sorted(severities, key=lambda s: health.SEVERITY_ORDER[s])
    assert findings[0].kind == "core_entities_missing"


def test_a_healthy_device_produces_nothing():
    assert (
        health.inspect(
            profile="high_accuracy",
            target_pps=100,
            present_fields=ALL_CORE,
            reachable=True,
            threshold_state=state("0.5"),
            diagnostic_states={"diag_csi_accepted_rate": state("98")},
            observed_scores=[0.3],
            window_minutes=15,
        )
        == []
    )
