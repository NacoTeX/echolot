"""What is wrong with a flashed device, judged from what Home Assistant says.

Echolot could tell you that a device was reachable and that its build
succeeded. Neither answers the question the product exists for: is this
thing actually sensing? The first look at real hardware turned up three
ways it can be online, healthy-looking and useless, none of which
anything here would have reported:

  * A device running `high_accuracy` whose threshold was the *lightweight*
    default. Upstream only derives the per-algorithm default when the
    detector was chosen at runtime and saved to NVS; an algorithm set in
    YAML — which is every device Echolot builds — keeps the schema
    default, and that constant is hard-wired to the lightweight one
    (`runtime/esp_idf/esp_idf_runtime.cpp`, `runtime_sensing_schema.h`).
    The bar ends up 32 % higher than upstream intends for that model.
  * A device publishing every diagnostic and control entity but none of
    the three sensing ones, because it still ran firmware from an older
    build. It looked perfectly healthy.
  * Every CSI rate sitting at `unknown` on both devices, because those
    sensors only publish when the "Refresh Diagnostics" button is pressed
    and nothing ever pressed it. So nobody could see whether usable CSI
    was arriving at all.

This module is pure: it takes states and returns findings. Reading and
pressing lives in main.py.
"""

from dataclasses import dataclass

#: Upstream's per-algorithm defaults, from ESPectre's
#: `src/cpp/core/detector_types.h`. Kept as literals rather than fetched,
#: because the point is to recognise the exact bit pattern a device
#: reports and say which algorithm it belongs to.
PROFILE_DEFAULT_THRESHOLD: dict[str, float] = {
    "lightweight": 0.6621854538596202,
    "high_accuracy": 0.5,
}

#: A reported float is compared against those with room for the round trip
#: through float32, JSON and Home Assistant's string state.
THRESHOLD_EPSILON = 1e-6

#: Entities upstream's component always creates (`__init__.py`: "optional
#: with defaults, always created"). A device that answers in Home
#: Assistant but lacks one is running firmware older than that promise.
CORE_ENTITY_LABELS = {
    "entity_motion": "Motion Detected",
    "entity_movement_score": "Movement Score",
    "entity_threshold": "Threshold",
}

#: The diagnostics that only publish on demand. Names are upstream's.
DIAGNOSTIC_LABELS = {
    "diag_csi_accepted_rate": "CSI Accepted Rate",
    "diag_csi_filtered_rate": "CSI Filtered Rate",
    "diag_csi_stale_rate": "CSI Stale Rate",
    "diag_csi_missing_slot_rate": "CSI Missing Slot Rate",
    "diag_csi_out_of_order_rate": "CSI Out-of-order Rate",
    "diag_traffic_tx_rate": "Traffic TX Rate",
}

#: Below this share of the configured target, the radio is not delivering
#: enough CSI for the detector to work with. Deliberately generous: rates
#: fluctuate, and a false alarm here is worse than a late one.
CSI_STARVED_FRACTION = 0.25

#: How far under the threshold the highest observed score has to stay
#: before it is worth mentioning. Two orders of magnitude is far outside
#: anything a working detector does with someone walking past.
SCORE_UNREACHABLE_FACTOR = 100.0

SEVERITY_ORDER = {"blocker": 0, "warning": 1, "info": 2}


@dataclass(frozen=True)
class Finding:
    """One thing wrong, with the button that would address it."""

    kind: str
    severity: str  # blocker | warning | info
    message: str
    #: What the UI should offer: recalibrate, rebuild, refresh_diagnostics,
    #: or None when there is nothing to press.
    action: str | None = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "message": self.message,
            "action": self.action,
        }


def _number(state) -> float | None:
    """The numeric value of a Home Assistant state, or None.

    Home Assistant sends every state as a string, and uses the literal
    strings "unknown" and "unavailable" for entities that have not
    reported. Both must read as absent, not as zero — the difference
    between "no CSI arriving" and "never asked" is the whole point.
    """
    if not isinstance(state, dict):
        return None
    raw = state.get("state")
    if raw in (None, "", "unknown", "unavailable"):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _matching_profile(threshold: float) -> str | None:
    """Which algorithm's default this threshold is, if it is one."""
    for profile, default in PROFILE_DEFAULT_THRESHOLD.items():
        if abs(threshold - default) <= THRESHOLD_EPSILON:
            return profile
    return None


def check_threshold_profile(profile: str, threshold_state) -> Finding | None:
    """Flag a threshold that belongs to the other detection profile."""
    threshold = _number(threshold_state)
    if threshold is None:
        return None
    belongs_to = _matching_profile(threshold)
    if belongs_to is None or belongs_to == profile:
        # Either a value someone chose deliberately, or the right default.
        return None

    wanted = PROFILE_DEFAULT_THRESHOLD.get(profile)
    return Finding(
        kind="threshold_profile_mismatch",
        severity="warning",
        message=(
            f"Die Schwelle steht auf {threshold:g} — das ist die Vorgabe für das "
            f"Profil „{belongs_to}“, das Gerät läuft aber auf „{profile}“ "
            f"(Vorgabe {wanted:g}). Die Firmware übernimmt die passende Vorgabe nur, "
            "wenn das Profil zur Laufzeit umgestellt wurde; ein im YAML gesetztes "
            "Profil behält den anderen Wert. „Neu kalibrieren“ setzt ihn richtig."
        ),
        action="recalibrate",
    )


def check_core_entities(present_fields: set[str], reachable: bool) -> Finding | None:
    """Flag a device that answers but is missing its sensing entities."""
    if not reachable:
        # Nothing published at all is a different problem, and the
        # overview already words it as one.
        return None
    missing = [
        label for field, label in CORE_ENTITY_LABELS.items() if field not in present_fields
    ]
    if not missing:
        return None
    return Finding(
        kind="core_entities_missing",
        severity="blocker",
        message=(
            "Das Gerät meldet sich in Home Assistant, aber ohne "
            + ", ".join(f"„{name}“" for name in missing)
            + ". Die aktuelle ESPectre-Version legt diese Entities immer an, also "
            "läuft dort ältere Firmware. Es kann nichts erkennen, sieht aber "
            "gesund aus — neu bauen und flashen."
        ),
        action="rebuild",
    )


def check_diagnostics(diagnostic_states: dict) -> Finding | None:
    """Flag diagnostics that have never been fetched."""
    if not diagnostic_states:
        return None
    if any(_number(state) is not None for state in diagnostic_states.values()):
        return None
    return Finding(
        kind="diagnostics_never_read",
        severity="info",
        message=(
            "Keine der CSI-Diagnosen hat je einen Wert gemeldet. Sie veröffentlichen "
            "nur auf Anforderung, und danach hat noch nichts gefragt — ohne sie lässt "
            "sich nicht sehen, ob überhaupt verwertbare CSI-Pakete ankommen."
        ),
        action="refresh_diagnostics",
    )


def check_csi_rate(accepted_state, target_pps: int) -> Finding | None:
    """Flag a radio that is not delivering the CSI the detector expects."""
    accepted = _number(accepted_state)
    if accepted is None or target_pps <= 0:
        return None
    floor = target_pps * CSI_STARVED_FRACTION
    if accepted >= floor:
        return None
    return Finding(
        kind="csi_starved",
        severity="blocker",
        message=(
            f"Es kommen {accepted:g} verwertbare CSI-Pakete pro Sekunde an, angepeilt "
            f"sind {target_pps}. Der Detektor bekommt zu wenig Material; die Erkennung "
            "ist damit Zufall. Meist liegt es am Abstand zum Access Point oder an "
            "einem überlasteten Kanal."
        ),
        action=None,
    )


def check_score_reachability(
    threshold_state, observed_scores: list[float], window_minutes: int
) -> Finding | None:
    """Point out a threshold nothing has come close to.

    Deliberately worded as an observation. An empty flat produces low
    scores for the honest reason, and this module cannot tell that apart
    from an unreachable threshold — only someone walking through the room
    can.
    """
    threshold = _number(threshold_state)
    if threshold is None or threshold <= 0 or not observed_scores:
        return None
    highest = max(observed_scores)
    if highest * SCORE_UNREACHABLE_FACTOR >= threshold:
        return None
    return Finding(
        kind="score_far_below_threshold",
        severity="info",
        message=(
            f"In den letzten {window_minutes} Minuten lag der höchste Bewegungswert bei "
            f"{highest:.3g}, die Schwelle bei {threshold:g} — Faktor "
            f"{threshold / highest:.0f} dazwischen, falls überhaupt jemand im Raum war. "
            "Entweder war es ruhig, oder die Schwelle ist unerreichbar. Das trennt nur "
            "ein Gehtest: eine Minute durch den Raum laufen und hier noch einmal schauen."
        ),
        action=None,
    )


def inspect(
    *,
    profile: str,
    target_pps: int,
    present_fields: set[str],
    reachable: bool,
    threshold_state=None,
    diagnostic_states: dict | None = None,
    observed_scores: list[float] | None = None,
    window_minutes: int = 0,
) -> list[Finding]:
    """Every finding for one device, most serious first."""
    findings = [
        check_core_entities(present_fields, reachable),
        check_threshold_profile(profile, threshold_state),
        check_csi_rate((diagnostic_states or {}).get("diag_csi_accepted_rate"), target_pps),
        check_diagnostics(diagnostic_states or {}),
        check_score_reachability(threshold_state, observed_scores or [], window_minutes),
    ]
    present = [f for f in findings if f is not None]
    return sorted(present, key=lambda f: SEVERITY_ORDER.get(f.severity, 9))
