"""Ready-made parameter sets, so a new device doesn't start with a blank
form and a handful of numbers whose trade-offs aren't obvious.

Field names and ranges follow ESPectre's own runtime schema
(src/cpp/runtime/runtime_sensing_schema.h). The traffic-rate constant is
ESPectre's own figure: the default 100 packets/s costs roughly 9 KB/s of
Wi-Fi per device.
"""

from dataclasses import asdict, dataclass

# KB/s of Wi-Fi traffic each packet-per-second of CSI probing costs.
KB_PER_SECOND_PER_PPS = 0.09


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    description: str
    csi_target_pps: int
    detection_algorithm: str
    evaluation_interval_ms: int


PRESETS: tuple[Preset, ...] = (
    Preset(
        key="balanced",
        label="Ausgewogen",
        description="ESPectres Standardwerte — guter Kompromiss aus Empfindlichkeit und Netzlast.",
        csi_target_pps=100,
        detection_algorithm="lightweight",
        evaluation_interval_ms=250,
    ),
    Preset(
        key="quiet",
        label="Sparsam",
        description=(
            "Weniger als die Hälfte der Funklast, und der Detektor läuft seltener. "
            "Erkennt deutliche Bewegung zuverlässig, feine Regungen eher nicht."
        ),
        csi_target_pps=40,
        detection_algorithm="lightweight",
        evaluation_interval_ms=500,
    ),
    Preset(
        key="sensitive",
        label="Empfindlich",
        description=(
            "Doppelte Abtastrate und häufigere Auswertung für kleinste Regungen — "
            "dafür spürbar mehr Funklast und Rechenzeit auf dem Gerät."
        ),
        csi_target_pps=200,
        detection_algorithm="lightweight",
        evaluation_interval_ms=125,
    ),
    Preset(
        key="no_calibration",
        label="Ohne Kalibrierung",
        description=(
            "Neuronales Netz statt gewichtetem Modell: kein Einlernen nach dem Start, "
            "dafür mehr Flash- und Rechenbedarf."
        ),
        csi_target_pps=100,
        detection_algorithm="high_accuracy",
        evaluation_interval_ms=250,
    ),
)


def as_dicts() -> list[dict]:
    return [asdict(p) for p in PRESETS]


def estimate_kb_per_second(rate: int) -> float:
    return round(rate * KB_PER_SECOND_PER_PPS, 2)
