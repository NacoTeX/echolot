"""Where the sensor really hangs, worked out from where somebody stood.

The plan says where the sensor is and which way it looks — as well as
somebody could measure and draw it. The module has its own view. The
alignment compares the two: a person stands on a spot marked on the plan,
the module reports a position (app/calibration.py), and the difference is
the error of the placement.

  one spot      Only the direction can be corrected; the position stays
                as drawn. The distance still tells whether the position
                is plausible.
  two or more   Position and direction together: the rigid fit that
                brings the reported positions closest to the marked ones
                (least squares, Kabsch in two dimensions).

Mirrored or not — which side the module counts positive, which its manual
does not say — is decided by the fit where the spots can decide it:
three spots not on one line fit one way clearly better than the other.
Two spots fit both ways equally well; then the solution that keeps the
sensor nearer to where it is drawn wins, and when that is no clearer
either, the current setting stays and the result says so.
"""

import math

from app import geometry

#: A solution may put the sensor this far outside the walls, and is then
#: moved onto them: the module hangs on a wall, and its reports scatter
#: by a decimetre or two. Farther than that, the spots do not describe
#: this room.
MAX_OUTSIDE_M = 0.5
#: Spots closer together than this say little about the direction.
MIN_SPREAD_M = 1.0


def _normalize(angle: float) -> float:
    return ((angle + 180.0) % 360.0) - 180.0


def _prepare(pairs, mirror: bool):
    return [((-px if mirror else px), py, qx, qy) for (px, py), (qx, qy) in pairs]


def fit_free(pairs, mirror: bool) -> tuple[float, float, float, float]:
    """(x, y, angle, rms) of the best rigid fit — two spots or more."""
    rows = _prepare(pairs, mirror)
    n = len(rows)
    pcx = sum(r[0] for r in rows) / n
    pcy = sum(r[1] for r in rows) / n
    qcx = sum(r[2] for r in rows) / n
    qcy = sum(r[3] for r in rows) / n
    dot = cross = 0.0
    for px, py, qx, qy in rows:
        ax, ay, bx, by = px - pcx, py - pcy, qx - qcx, qy - qcy
        dot += ax * bx + ay * by
        cross += ax * by - ay * bx
    angle = math.degrees(math.atan2(cross, dot))
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    x = qcx - (pcx * c - pcy * s)
    y = qcy - (pcx * s + pcy * c)
    placement = {"x": x, "y": y, "angle": angle, "mirror": mirror}
    return x, y, _normalize(angle), rms(pairs, placement)


def fit_direction(pairs, x: float, y: float, mirror: bool) -> tuple[float, float]:
    """(angle, rms) with the position held where it is."""
    dot = cross = 0.0
    for px, py, qx, qy in _prepare(pairs, mirror):
        bx, by = qx - x, qy - y
        dot += px * bx + py * by
        cross += px * by - py * bx
    angle = _normalize(math.degrees(math.atan2(cross, dot)))
    return angle, rms(pairs, {"x": x, "y": y, "angle": angle, "mirror": mirror})


def errors(pairs, placement: dict) -> list[float]:
    """Metres between each marked spot and where the placement puts the report."""
    out = []
    for (px, py), (qx, qy) in pairs:
        x, y = geometry.to_room(px, py, placement)
        out.append(math.hypot(x - qx, y - qy))
    return out


def rms(pairs, placement: dict) -> float:
    e = errors(pairs, placement)
    return math.sqrt(sum(v * v for v in e) / len(e)) if e else 0.0


def _outside(x: float, y: float, width: float, height: float) -> float:
    dx = max(0.0, -x, x - width)
    dy = max(0.0, -y, y - height)
    return math.hypot(dx, dy)


def solve(pairs, placement: dict, width: float, height: float) -> dict:
    """A proposal for x, y, angle and mirror, with what it would change.

    `pairs` is [((raw_x, raw_y), (room_x, room_y)), ...]: the module's
    median report in sensor metres, and the spot marked on the plan.
    Nothing is saved here.
    """
    if not pairs:
        raise ValueError("Mindestens ein Standpunkt ist nötig")
    current = {
        "x": float(placement["x"]), "y": float(placement["y"]),
        "angle": float(placement.get("angle", 0.0)), "mirror": bool(placement.get("mirror", False)),
    }
    warnings: list[str] = []
    mirror = current["mirror"]
    mirror_basis = "kept"

    if len(pairs) == 1:
        mode = "direction"
        x, y = current["x"], current["y"]
        angle, _ = fit_direction(pairs, x, y, mirror)
        (px, py), (qx, qy) = pairs[0]
        mismatch = abs(math.hypot(px, py) - math.hypot(qx - x, qy - y))
        if mismatch > 0.4:
            warnings.append(
                f"Der gemessene Abstand zum Sensor weicht um {mismatch * 100:.0f} cm vom Plan ab. "
                "Steht der Sensor auf dem Plan am richtigen Platz? Ein zweiter Standpunkt "
                "korrigiert auch die Position."
            )
    else:
        mode = "full"
        fits = {m: fit_free(pairs, m) for m in (False, True)}
        r_plain, r_mirror = fits[False][3], fits[True][3]
        best, worst = min(r_plain, r_mirror), max(r_plain, r_mirror)
        if len(pairs) >= 3 and worst - best > 0.1 and best < 0.6 * worst:
            mirror = r_mirror < r_plain
            mirror_basis = "fit"
        else:
            d_plain = math.hypot(fits[False][0] - current["x"], fits[False][1] - current["y"])
            d_mirror = math.hypot(fits[True][0] - current["x"], fits[True][1] - current["y"])
            if abs(d_plain - d_mirror) > 0.3:
                mirror = d_mirror < d_plain
                mirror_basis = "position"
            else:
                warnings.append(
                    "Ob links und rechts getauscht sind, lässt sich aus diesen Standpunkten "
                    "nicht sicher sagen. Die Einstellung bleibt, wie sie ist. Ein weiterer "
                    "Standpunkt abseits der Linie zwischen den bisherigen klärt es."
                )
        x, y, angle, _ = fits[mirror]
        spread = max(math.hypot(a[1][0] - b[1][0], a[1][1] - b[1][1]) for a in pairs for b in pairs)
        if spread < MIN_SPREAD_M:
            warnings.append(
                "Die Standpunkte liegen keinen Meter auseinander — die Richtung wird dadurch "
                "ungenau. Weiter auseinander stehen hilft."
            )
        outside = _outside(x, y, width, height)
        if outside > MAX_OUTSIDE_M:
            # These spots describe no place in this room for the sensor.
            # Keep the drawn position and correct only the direction.
            warnings.append(
                f"Die Standpunkte ergäben einen Sensor {outside:.1f} m außerhalb des Raums. "
                "Das passt nicht — die Position bleibt, nur die Richtung wird angepasst. "
                "Stimmen Raummaße und markierte Punkte?"
            )
            mode = "direction"
            mirror, mirror_basis = current["mirror"], "kept"
            x, y = current["x"], current["y"]
            angle, _ = fit_direction(pairs, x, y, mirror)
        elif outside > 0:
            x = min(max(x, 0.0), width)
            y = min(max(y, 0.0), height)
            angle, _ = fit_direction(pairs, x, y, mirror)

    proposal = {"x": round(x, 3), "y": round(y, 3), "angle": round(_normalize(angle), 1), "mirror": mirror}
    before = errors(pairs, current)
    after = errors(pairs, proposal)
    return {
        **proposal,
        "mode": mode,
        "mirror_basis": mirror_basis,
        "points": len(pairs),
        "errors_before_m": [round(v, 3) for v in before],
        "errors_after_m": [round(v, 3) for v in after],
        "rms_before_m": round(rms(pairs, current), 3),
        "rms_m": round(rms(pairs, proposal), 3),
        "shift_m": round(math.hypot(proposal["x"] - current["x"], proposal["y"] - current["y"]), 3),
        "turn_deg": round(_normalize(proposal["angle"] - current["angle"]), 1),
        "mirror_changed": proposal["mirror"] != current["mirror"],
        "warnings": warnings,
    }
