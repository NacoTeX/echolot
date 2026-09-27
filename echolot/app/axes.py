"""Which way the module's axes point, from two walks.

The manual does not say which side of the module is +x. Rather than
asking anybody to tell left from right, the calibration page draws two
short walks on the plan — A to B straight away from the sensor, and A to
B across in front of it — and somebody walks them
(calibration.analyze_walk). What the module reported is compared with
what was drawn:

  away    The distance the module reports must grow. If it does not, the
          walk went elsewhere than drawn, or the sensor stands elsewhere
          on the plan than in the room. Turned into the room, the walk
          also shows how far the sensor on the plan looks off — for
          either mirror setting, since with the plan turned the walk has
          a sideways part, and its sign is the mirror's.
  across  With the direction put right for a mirror setting, only one of
          the two turns this walk into the direction drawn. That one is
          proposed, with its direction — and only when the other one
          plainly does not fit.

A module built in on its side measures height where it should measure
the side: walked across, it reports next to no movement. That shows in
the walk itself (calibration.analyze_walk says so).

Nothing here changes anything; the page proposes, the person decides.
"""

import math

from app import geometry

#: The walk away must be seen within this of straight away from the
#: module; beyond it, it went elsewhere, or the plan is far off.
AWAY_WITHIN_DEG = 60.0
#: The sensor on the plan is corrected when it looks this far off.
PLAN_OFF_DEG = 20.0
#: A mirror setting is taken when it puts the walk across within this of
#: the drawn direction and the other one at least MIRROR_OTHER_DEG off.
MIRROR_FIT_DEG = 45.0
MIRROR_OTHER_DEG = 90.0


def _signed_angle(a: tuple, b: tuple) -> float:
    """Degrees from direction a to direction b, as geometry.to_room turns."""
    cross = a[0] * b[1] - a[1] * b[0]
    dot = a[0] * b[0] + a[1] * b[1]
    return math.degrees(math.atan2(cross, dot))


def _normalize(deg: float) -> float:
    return ((deg + 180.0) % 360.0) - 180.0


def _room_delta(leg: dict, placement: dict) -> tuple[float, float]:
    sx, sy = geometry.to_room(*leg["start_raw"], placement)
    ex, ey = geometry.to_room(*leg["end_raw"], placement)
    return ex - sx, ey - sy


def _drawn(leg: dict) -> tuple[float, float]:
    return leg["to"][0] - leg["from"][0], leg["to"][1] - leg["from"][1]


def verdict(placement: dict, away: dict | None = None, across: dict | None = None) -> dict:
    """What the walks say. Each leg is {"from": [x, y], "to": [x, y]} in
    the room, as drawn, and {"start_raw", "end_raw"} in sensor metres, as
    walked (calibration.analyze_walk).

    `reason` says why nothing is proposed: "away" (the walk away was not
    seen going away), "across" (no walk across) or "undecided" (it fits
    neither mirror setting plainly); None when `ok`.
    """
    out = {
        "module_angle_deg": None, "plan_error_deg": None, "across_error_deg": None,
        "mirror": None, "angle": None, "ok": False, "reason": None, "messages": [],
    }
    angle = float(placement.get("angle", 0.0))
    if away is not None:
        dx = away["end_raw"][0] - away["start_raw"][0]
        dy = away["end_raw"][1] - away["start_raw"][1]
        module_angle = math.degrees(math.atan2(dx, dy))
        out["module_angle_deg"] = round(module_angle, 1)
        if abs(module_angle) > AWAY_WITHIN_DEG:
            out["messages"].append(
                "Beim Gang vom Sensor weg ist die gemessene Entfernung "
                + ("kleiner geworden" if abs(module_angle) > 120 else "kaum gewachsen")
                + f" ({module_angle:+.0f}° gegen die Blickrichtung des Moduls). Ging der Weg wirklich von A nach B, "
                "geradeaus vom Sensor weg? Steht der Sensor auf dem Plan an der richtigen Wand, und blickt er dort "
                "ungefähr in die richtige Richtung? Das erst richten, dann noch einmal gehen."
            )
            out["reason"] = "away"
            return out

    def plan_error(mirror: bool) -> float:
        if away is None:
            return 0.0
        return _normalize(_signed_angle(_drawn(away), _room_delta(away, {**placement, "mirror": mirror})))

    if across is None:
        out["reason"] = "across"
        out["messages"].append("Ohne den Gang quer vor dem Sensor lässt sich über links und rechts nichts sagen.")
        return out
    errors, turns = {}, {}
    for mirror in (False, True):
        turns[mirror] = plan_error(mirror)
        corrected = {**placement, "mirror": mirror, "angle": angle - turns[mirror]}
        errors[mirror] = abs(_signed_angle(_drawn(across), _room_delta(across, corrected)))
    best = min(errors, key=errors.get)
    out["across_error_deg"] = round(errors[best], 1)
    if errors[best] > MIRROR_FIT_DEG or errors[not best] < MIRROR_OTHER_DEG:
        out["messages"].append(
            f"Der Gang quer vor dem Sensor passt zu keiner Einstellung eindeutig (bestenfalls {errors[best]:.0f}° "
            "daneben). Noch einmal gehen — geradeaus von A nach B, allein im Raum."
        )
        out["reason"] = "undecided"
        return out
    out["mirror"] = best
    changed = best != bool(placement.get("mirror", False))
    out["messages"].append(
        "Links und rechts sind vertauscht: „Links und rechts tauschen“ gehört eingeschaltet." if changed and best
        else "„Links und rechts tauschen“ ist eingeschaltet, gehört aber aus." if changed
        else "Links und rechts stimmen, so wie eingestellt."
    )
    if away is not None:
        out["plan_error_deg"] = round(turns[best], 1)
        if abs(turns[best]) > PLAN_OFF_DEG:
            corrected = _normalize(round((angle - turns[best]) / 5.0) * 5.0)
            out["angle"] = corrected
            out["messages"].append(
                f"Auf dem Plan blickt der Sensor um etwa {abs(turns[best]):.0f}° in eine andere Richtung als im Raum. "
                f"Vorschlag: Blickrichtung {corrected:.0f}° statt {angle:.0f}°."
            )
    out["ok"] = True
    return out
