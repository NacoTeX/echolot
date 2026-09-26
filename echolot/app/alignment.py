"""Where the sensor really hangs, and how its module errs — from standpoints.

The plan says where the sensor is and which way it looks, as well as
somebody could measure and draw it. The module has its own view, and its
own errors. The calibration compares the two: a person stands on a spot
marked on the plan, the module reports a position (app/calibration.py),
and from several such spots Echolot works out

  placement    x, y, angle and mirror of the sensor on the plan;
  sensor model how the module's positions differ from the truth
               (geometry.correct): a distance scale and offset, an angle
               scale, and whether it measures the slant line from where it
               hangs rather than along the floor.

Moving and turning the sensor removes errors that are the same
everywhere. The ones that grow with distance — somebody four metres away
shown half a metre off — are the model's. It is fitted by least squares
(Levenberg–Marquardt; no numpy in the add-on image, and six parameters
do not need it).

More terms always fit a few spots better, and a model fitted to its own
noise is worse than none. So each candidate model is scored by the
Bayesian information criterion, with a noise floor: a residual below what
a person standing still and a module's scatter produce is not evidence
for anything. The simplest model the spots support wins.

How well it does is three different numbers, kept apart (see solve): the
fit to its own spots, a cross-check that redoes the whole choice without
each spot, and control spots that took no part at all. Only the last is
an independent check.

  one spot     only the direction; the position stays as drawn
  two spots    position and direction (rigid fit, Kabsch)
  three+       position, direction, mirror and, as the spots allow, the
               sensor model
"""

import math

from app import geometry

#: A solution may put the sensor this far outside the walls, and is then
#: moved onto them: the module hangs on a wall, and its reports scatter.
#: Farther than that, the spots do not describe this room.
MAX_OUTSIDE_M = 0.5
#: Spots closer together than this say little about the direction.
MIN_SPREAD_M = 1.0
#: Scatter of one standpoint's median, per axis: the module's own scatter
#: plus a person not standing exactly on the mark. Residuals below this
#: are not evidence for a richer model.
NOISE_M = 0.06

#: The alignment procedure. A proposal computed by one version is not
#: applied by another: 1 was 1.3 (the model chosen once, a conditional
#: leave-one-out as its accuracy), 2 redoes the choice per fold and takes
#: control spots.
VERSION = 2

#: Candidate sensor models, simplest first: the terms each one fits on
#: top of the placement.
MODELS = (
    ("placement", ()),
    ("range", ("range_scale",)),
    ("range_offset", ("range_scale", "range_offset_m")),
    ("range_azimuth", ("range_scale", "azimuth_scale")),
    ("full", ("range_scale", "range_offset_m", "azimuth_scale")),
)
MODEL_LABELS = {
    "placement": "Position und Richtung",
    "range": "Position, Richtung, Entfernungsmaßstab",
    "range_offset": "Position, Richtung, Entfernungsmaßstab und -versatz",
    "range_azimuth": "Position, Richtung, Entfernungs- und Winkelmaßstab",
    "full": "Position, Richtung, Entfernungsmaßstab, -versatz und Winkelmaßstab",
}
BOUNDS = {"range_scale": (0.7, 1.4), "range_offset_m": (-0.6, 0.6), "azimuth_scale": (0.6, 1.5)}
NEUTRAL = dict(geometry.MODEL_DEFAULTS)


def _normalize(angle: float) -> float:
    return ((angle + 180.0) % 360.0) - 180.0


# --- the closed-form fits for few spots -------------------------------------


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
    return math.hypot(max(0.0, -x, x - width), max(0.0, -y, y - height))


# --- least squares ------------------------------------------------------------


def _solve_linear(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting; None when singular."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-15:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            for c in range(col, n + 1):
                m[r][c] -= f * m[col][c]
    out = [0.0] * n
    for r in range(n - 1, -1, -1):
        out[r] = (m[r][n] - sum(m[r][c] * out[c] for c in range(r + 1, n))) / m[r][r]
    return out


def _levenberg_marquardt(residuals, p0: list[float], bounds: list[tuple[float, float]], iterations: int = 80):
    """Minimise the sum of squared residuals from p0 within bounds."""
    p = list(p0)
    r = residuals(p)
    cost = sum(v * v for v in r)
    lam = 1e-3
    n = len(p)
    for _ in range(iterations):
        jac = []
        for j in range(n):
            h = 1e-6 * max(1.0, abs(p[j]))
            up, down = list(p), list(p)
            up[j] += h
            down[j] -= h
            ru, rd = residuals(up), residuals(down)
            jac.append([(a - b) / (2 * h) for a, b in zip(ru, rd)])
        jtj = [[sum(jac[i][k] * jac[j][k] for k in range(len(r))) for j in range(n)] for i in range(n)]
        jtr = [sum(jac[i][k] * r[k] for k in range(len(r))) for i in range(n)]
        improved = False
        while lam < 1e12:
            a = [[jtj[i][j] + (lam * max(jtj[i][i], 1e-12) if i == j else 0.0) for j in range(n)] for i in range(n)]
            step = _solve_linear(a, [-v for v in jtr])
            if step is None:
                lam *= 10
                continue
            trial = [min(max(p[i] + step[i], bounds[i][0]), bounds[i][1]) for i in range(n)]
            rt = residuals(trial)
            ct = sum(v * v for v in rt)
            if ct < cost:
                gain = cost - ct
                p, r, cost = trial, rt, ct
                lam = max(lam / 3, 1e-9)
                improved = True
                if gain < 1e-14 * max(cost, 1e-12) + 1e-18:
                    return p, cost
                break
            lam *= 5
        if not improved:
            break
    return p, cost


class _Fit:
    """One candidate: a model, a mirror, slant or not, fitted."""

    def __init__(self, name, terms, mirror, slant, params, cost, n_obs):
        self.name, self.terms, self.mirror, self.slant = name, terms, mirror, slant
        self.params, self.cost, self.n_obs = params, cost, n_obs
        # Placement (3) plus the model's terms. Slant and mirror are
        # choices between models, not fitted numbers.
        self.k = 3 + len(terms)
        floor = n_obs * NOISE_M * NOISE_M
        self.bic = n_obs * math.log((cost + floor) / n_obs) + self.k * math.log(n_obs)

    def placement(self, base: dict) -> dict:
        x, y, angle, *extra = self.params
        out = {**base, **NEUTRAL, "x": x, "y": y, "angle": _normalize(angle), "mirror": self.mirror,
               "slant": self.slant}
        out.update(zip(self.terms, extra))
        return out


def _residual_fn(pairs, base: dict, terms, mirror: bool, slant: bool, fixed_xy=None):
    def residuals(p):
        if fixed_xy is not None:
            p = [fixed_xy[0], fixed_xy[1], *p]
        x, y, angle, *extra = p
        placement = {**base, **NEUTRAL, "x": x, "y": y, "angle": angle, "mirror": mirror, "slant": slant}
        placement.update(zip(terms, extra))
        out = []
        for (px, py), (qx, qy) in pairs:
            rx, ry = geometry.to_room(px, py, placement)
            out += [rx - qx, ry - qy]
        return out
    return residuals


def _fit(pairs, base: dict, name: str, terms, mirror: bool, slant: bool, start=None, fixed_xy=None) -> _Fit:
    if start is None:
        # The rigid fit on positions already corrected for the slant is a
        # start close enough for the rest.
        probe = {**base, **NEUTRAL, "slant": slant}
        corrected = [(geometry.correct(px, py, probe), q) for (px, py), q in pairs]
        x, y, angle, _ = fit_free(corrected, mirror)
        start = [x, y, angle] + [NEUTRAL[t] for t in terms]
    bounds = [(-1e3, 1e3), (-1e3, 1e3), (-1e4, 1e4)] + [BOUNDS[t] for t in terms]
    if fixed_xy is not None:
        start, bounds = start[2:], bounds[2:]
    params, cost = _levenberg_marquardt(_residual_fn(pairs, base, terms, mirror, slant, fixed_xy), start, bounds)
    if fixed_xy is not None:
        params = [fixed_xy[0], fixed_xy[1], *params]
    return _Fit(name, terms, mirror, slant, params, cost, 2 * len(pairs))


# --- the proposal -------------------------------------------------------------

MIRROR_UNSURE = (
    "Ob links und rechts getauscht sind, lässt sich aus diesen Standpunkten "
    "nicht sicher sagen. Die Einstellung bleibt, wie sie ist. Ein weiterer "
    "Standpunkt abseits der Linie zwischen den bisherigen klärt es."
)


def _select(pairs, current: dict, width: float, height: float) -> dict:
    """The whole choice from these spots alone: placement, mirror, slant
    and sensor model. Run once on all spots, and again in every fold of
    the cross-check, so the check sees the choice made without the spot
    it predicts."""
    base = {"mount_height_m": current["mount_height_m"], "target_height_m": current["target_height_m"]}
    warnings: list[str] = []
    mirror_basis = "kept"
    model_name = None
    candidates: list[dict] = []
    n = len(pairs)
    if n < 3:
        # Too few spots for anything but the placement: keep the sensor
        # model as it is and fit where the sensor is and how it looks.
        proposal = dict(current)
        corrected = [(geometry.correct(px, py, current), q) for (px, py), q in pairs]
        if n == 1:
            mode = "direction"
            proposal["angle"], _ = fit_direction(corrected, current["x"], current["y"], current["mirror"])
            (cx, cy), (qx, qy) = corrected[0]
            mismatch = abs(math.hypot(cx, cy) - math.hypot(qx - current["x"], qy - current["y"]))
            if mismatch > 0.4:
                warnings.append(
                    f"Der gemessene Abstand zum Sensor weicht um {mismatch * 100:.0f} cm vom Plan ab. "
                    "Steht der Sensor auf dem Plan am richtigen Platz? Ein zweiter Standpunkt "
                    "korrigiert auch die Position."
                )
        else:
            mode = "full"
            fits = {m: fit_free(corrected, m) for m in (False, True)}
            d_plain = math.hypot(fits[False][0] - current["x"], fits[False][1] - current["y"])
            d_mirror = math.hypot(fits[True][0] - current["x"], fits[True][1] - current["y"])
            mirror = current["mirror"]
            if abs(d_plain - d_mirror) > 0.3:
                mirror = d_mirror < d_plain
                mirror_basis = "position"
            else:
                warnings.append(MIRROR_UNSURE)
            x, y, angle, _ = fits[mirror]
            proposal.update(x=x, y=y, angle=angle, mirror=mirror)
        return {"proposal": proposal, "mode": mode, "mirror_basis": mirror_basis, "model": None,
                "terms": (), "candidates": [], "warnings": warnings}

    mode = "full"
    slants = (False, True) if current["mount_height_m"] is not None else (False,)
    fits: list[_Fit] = []
    for name, terms in MODELS:
        if 2 * n - (3 + len(terms)) < 2:
            continue
        for mirror in (False, True):
            for slant in slants:
                fits.append(_fit(pairs, base, name, terms, mirror, slant))
    # Mirror first: the best of each side, and whether the difference is
    # evidence or noise.
    best_side = {m: min((f for f in fits if f.mirror == m), key=lambda f: f.bic) for m in (False, True)}
    gap = best_side[not current["mirror"]].bic - best_side[current["mirror"]].bic
    if abs(gap) >= 6:
        mirror = best_side[True].bic < best_side[False].bic
        mirror_basis = "fit"
    else:
        d = {m: math.hypot(best_side[m].params[0] - current["x"], best_side[m].params[1] - current["y"])
             for m in (False, True)}
        if abs(d[False] - d[True]) > 0.3:
            mirror = d[True] < d[False]
            mirror_basis = "position"
        else:
            mirror = current["mirror"]
            warnings.append(MIRROR_UNSURE)
    side = sorted((f for f in fits if f.mirror == mirror), key=lambda f: f.bic)
    candidates = [
        {"model": f.name, "slant": f.slant, "fit_rms_m": round(math.sqrt(f.cost / n), 3), "score": round(f.bic, 1)}
        for f in side
    ]
    inside = [f for f in side if _outside(f.params[0], f.params[1], width, height) <= MAX_OUTSIDE_M]
    if not inside:
        outside = _outside(side[0].params[0], side[0].params[1], width, height)
        warnings.append(
            f"Die Standpunkte ergäben einen Sensor {outside:.1f} m außerhalb des Raums. "
            "Das passt nicht — die Position bleibt, nur die Richtung wird angepasst. "
            "Stimmen Raummaße und markierte Punkte?"
        )
        mode = "direction"
        mirror, mirror_basis = current["mirror"], "kept"
        best = _fit(pairs, base, "placement", (), mirror, False, fixed_xy=(current["x"], current["y"]))
    else:
        best = inside[0]
        x, y = best.params[0], best.params[1]
        if _outside(x, y, width, height) > 0:
            # On the wall, then everything else fitted again around it.
            cx, cy = min(max(x, 0.0), width), min(max(y, 0.0), height)
            best = _fit(pairs, base, best.name, best.terms, best.mirror, best.slant,
                        start=list(best.params), fixed_xy=(cx, cy))
    proposal = best.placement(current)
    for term in best.terms:
        low, high = BOUNDS[term]
        if abs(proposal[term] - low) < 1e-6 or abs(proposal[term] - high) < 1e-6:
            warnings.append(
                "Eine Korrektur stößt an ihre Grenze — das Modul verhält sich anders als "
                "erwartet, oder ein Standpunkt ist falsch markiert. Punkte prüfen."
            )
            break
    return {"proposal": proposal, "mode": mode, "mirror_basis": mirror_basis, "model": best.name,
            "terms": best.terms, "candidates": candidates, "warnings": warnings}


def _held_out_errors(pairs, current: dict, chosen: dict) -> list[float] | None:
    """Each spot predicted by the *chosen* model fitted to the others.

    Only for finding a spot marked in the wrong place — not an accuracy:
    the model was chosen with every spot, the one left out included (the
    cross-check in solve() redoes the choice). A flexible model can
    swallow a bad spot when the choice is redone, and this still shows it.
    """
    n = len(pairs)
    if chosen["model"] is None or chosen["mode"] != "full":
        return None
    terms = chosen["terms"]
    if 2 * (n - 1) - (3 + len(terms)) < 1:
        return None
    base = {"mount_height_m": current["mount_height_m"], "target_height_m": current["target_height_m"]}
    prop = chosen["proposal"]
    start = [prop["x"], prop["y"], prop["angle"]] + [prop[t] for t in terms]
    out = []
    for i in range(n):
        fit = _fit(pairs[:i] + pairs[i + 1:], base, chosen["model"], terms, prop["mirror"], prop["slant"], start=start)
        (px, py), (qx, qy) = pairs[i]
        rx, ry = geometry.to_room(px, py, fit.placement(base))
        out.append(math.hypot(rx - qx, ry - qy))
    return out


def _rms(values: list[float]) -> float | None:
    return round(math.sqrt(sum(v * v for v in values) / len(values)), 3) if values else None


def _layout_warnings(pairs, current: dict) -> list[str]:
    """Spot layouts that cannot tell the parameters apart."""
    refs = [q for _, q in pairs]
    n = len(refs)
    out = []
    if n >= 2:
        spread = max(math.hypot(a[0] - b[0], a[1] - b[1]) for a in refs for b in refs)
        if spread < MIN_SPREAD_M:
            out.append(
                "Die Standpunkte liegen keinen Meter auseinander — die Richtung wird dadurch "
                "ungenau. Weiter auseinander stehen hilft."
            )
    if n >= 3:
        mx = sum(q[0] for q in refs) / n
        my = sum(q[1] for q in refs) / n
        sxx = sum((q[0] - mx) ** 2 for q in refs) / n
        syy = sum((q[1] - my) ** 2 for q in refs) / n
        sxy = sum((q[0] - mx) * (q[1] - my) for q in refs) / n
        half = (sxx + syy) / 2
        root = math.sqrt(((sxx - syy) / 2) ** 2 + sxy * sxy)
        if half + root > 0 and (half - root) / (half + root) < 0.03:
            out.append(
                "Die Standpunkte liegen fast auf einer Linie. Links/Rechts und die "
                "Winkelkorrektur lassen sich so nicht bestimmen — ein Punkt abseits der Linie fehlt."
            )
        a = math.radians(current["angle"])
        offs, dists = [], []
        for qx, qy in refs:
            dx, dy = qx - current["x"], qy - current["y"]
            offs.append(math.degrees(math.atan2(dx * math.cos(a) + dy * math.sin(a), -dx * math.sin(a) + dy * math.cos(a))))
            dists.append(math.hypot(dx, dy))
        if max(offs) - min(offs) < 20:
            out.append(
                "Alle Standpunkte liegen in fast derselben Richtung vom Sensor. Ob das Modul "
                "Winkel verzerrt, zeigt sich erst mit Punkten links und rechts."
            )
        if max(dists) - min(dists) < 1.0:
            out.append(
                "Alle Standpunkte sind etwa gleich weit vom Sensor entfernt. Einen "
                "Entfernungsfehler zeigen erst Punkte nah und fern."
            )
    return out


def solve(pairs, placement: dict, width: float, height: float, checks=()) -> dict:
    """A proposal for the placement and the sensor model, with three
    different numbers for how well it does — kept apart on purpose:

      fit_rms_m         how closely it fits the spots it was fitted to.
                        Says little: more terms always fit better.
      cv_rms_m          cross-check: every spot left out once, and the
                        whole choice — model, mirror, slant — made again
                        from the others, then the spot predicted. Honest
                        about the method; still the same session, the
                        same spots.
      validation_rms_m  control spots (`checks`) that took no part in any
                        of it. Only this is an independent check; only
                        with at least two of them is the result called
                        validated and given a grade.

    `pairs` and `checks` are [((raw_x, raw_y), (room_x, room_y)), ...]:
    the module's median report in sensor metres, and the spot marked on
    the plan. `placement` is the sensor as stored, with mount_height_m and
    target_height_m; without a height the slant is not considered.
    Nothing is saved here.
    """
    if not pairs:
        raise ValueError("Mindestens ein Standpunkt ist nötig")
    current = {
        "x": float(placement["x"]), "y": float(placement["y"]),
        "angle": float(placement.get("angle", 0.0)), "mirror": bool(placement.get("mirror", False)),
        "mount_height_m": placement.get("mount_height_m"),
        "target_height_m": float(placement.get("target_height_m", 1.0)),
        **{k: placement.get(k, v) for k, v in NEUTRAL.items()},
    }
    n = len(pairs)
    chosen = _select(pairs, current, width, height)
    held_out = _held_out_errors(pairs, current, chosen)
    proposal = chosen["proposal"]
    warnings = list(chosen["warnings"])
    if chosen["model"] is not None and n < 5:
        warnings.append(
            "Für Entfernungs- und Winkelkorrekturen braucht es mehr Standpunkte: fünf, "
            "verteilt nah und fern, links und rechts."
        )
    layout = _layout_warnings(pairs, current)
    warnings += layout

    proposal["angle"] = round(_normalize(proposal["angle"]), 1)
    proposal["x"], proposal["y"] = round(proposal["x"], 3), round(proposal["y"], 3)
    for term in ("range_scale", "azimuth_scale"):
        proposal[term] = round(float(proposal[term]), 4)
    proposal["range_offset_m"] = round(float(proposal["range_offset_m"]), 3)
    before = errors(pairs, current)
    after = errors(pairs, proposal)

    # Cross-check with the whole choice redone per fold.
    cv_errors = None
    if n >= 3:
        cv_errors = []
        for i in range(n):
            fold = _select(pairs[:i] + pairs[i + 1:], current, width, height)["proposal"]
            (px, py), (qx, qy) = pairs[i]
            rx, ry = geometry.to_room(px, py, fold)
            cv_errors.append(math.hypot(rx - qx, ry - qy))

    # A spot marked in the wrong place, looked for three ways, because each
    # alone misses cases: in the fit to all spots a richer model can
    # swallow the error; left out with the choice redone, it can be
    # swallowed again; left out under the chosen model, a spot can fall
    # where the others say little.
    def stands_out(values, i):
        others = sorted(v for j, v in enumerate(values) if j != i)
        typical = others[len(others) // 2] if others else 0.0
        return values[i] > 0.4 and values[i] > 3 * max(typical, NOISE_M)

    for i in range(n if n >= 3 else 0):
        flagged = [v for v in (after, cv_errors, held_out) if v is not None and stands_out(v, i)]
        if flagged:
            e = max(v[i] for v in flagged)
            warnings.append(
                f"Standpunkt {i + 1} passt nicht zu den anderen ({e * 100:.0f} cm daneben). "
                "Falsch markiert oder bewegt? Neu messen oder entfernen — er verzerrt sonst das Ergebnis."
            )

    validation = errors(list(checks), proposal) if checks else []
    validated = len(validation) >= 2
    validation_rms = _rms(validation)
    quality = None
    if validated:
        quality = "good" if validation_rms <= 0.15 else "fair" if validation_rms <= 0.3 else "poor"

    return {
        "x": proposal["x"], "y": proposal["y"], "angle": proposal["angle"], "mirror": bool(proposal["mirror"]),
        "slant": bool(proposal["slant"]),
        "range_scale": proposal["range_scale"],
        "range_offset_m": proposal["range_offset_m"],
        "azimuth_scale": proposal["azimuth_scale"],
        "model": chosen["model"],
        "model_label": MODEL_LABELS.get(chosen["model"]) if chosen["model"] else None,
        "mode": chosen["mode"],
        "mirror_basis": chosen["mirror_basis"],
        "points": n,
        "errors_before_m": [round(v, 3) for v in before],
        "errors_after_m": [round(v, 3) for v in after],
        "rms_before_m": _rms(before),
        "fit_rms_m": _rms(after),
        "cv_rms_m": _rms(cv_errors) if cv_errors is not None else None,
        "cv_errors_m": [round(v, 3) for v in cv_errors] if cv_errors is not None else None,
        "validation_points": len(validation),
        "validation_rms_m": validation_rms,
        "validation_errors_m": [round(v, 3) for v in validation],
        "validation_status": "validated" if validated else "unvalidated",
        "quality": quality,
        "layout_ok": not layout,
        "candidates": chosen["candidates"],
        "shift_m": round(math.hypot(proposal["x"] - current["x"], proposal["y"] - current["y"]), 3),
        "turn_deg": round(_normalize(proposal["angle"] - current["angle"]), 1),
        "mirror_changed": bool(proposal["mirror"]) != current["mirror"],
        "model_changed": any(abs(float(proposal[k]) - float(current[k])) > 1e-6
                             for k in ("range_scale", "range_offset_m", "azimuth_scale"))
        or bool(proposal["slant"]) != bool(current["slant"]),
        "warnings": warnings,
    }


# --- where to stand -------------------------------------------------------------


def suggest_points(room, count: int = 5) -> list[list[float]]:
    """Spots worth standing on: inside the walls, in front of the sensor,
    off the furniture, and spread over distance and angle — near and far,
    left and right, which is what tells a scale from a turn.

    Uses the planned range and field of view: the only guess there is
    before anything is measured.
    """
    s = room.sensor
    walls = room.outline or [(0, 0), (room.width, 0), (room.width, room.height), (0, room.height)]
    half = math.radians(min(s.fov_deg, 150) / 2) * 0.8
    reach = min(s.range_m, 8.0) * 0.9
    step = 0.25
    candidates = []
    y = step
    while y < room.height:
        x = step
        while x < room.width:
            if geometry.point_in_polygon(x, y, walls) and geometry.distance_to_polygon(x, y, walls) >= 0.4:
                dx, dy = x - s.x, y - s.y
                d = math.hypot(dx, dy)
                # Angle off the sensor's axis; its axis is (-sin a, cos a).
                a = math.radians(s.angle)
                off = math.atan2(dx * math.cos(a) + dy * math.sin(a), -dx * math.sin(a) + dy * math.cos(a))
                on_furniture = any(
                    geometry.point_in_polygon(x, y, geometry.furniture_outline(f.x, f.y, f.w, f.h, f.angle, 0.15))
                    for f in room.furniture
                )
                if 1.0 <= d <= reach and abs(off) <= half and not on_furniture:
                    candidates.append((round(x, 2), round(y, 2), d, off))
            x += step
        y += step
    if not candidates:
        return []
    # The first spot in the middle of the view at mid distance; each next
    # one as far as possible from all chosen, measured in distance from the
    # sensor and angle off its axis — so the set covers both.
    d_max = max(c[2] for c in candidates)

    def key(c):
        return (c[2] / d_max, c[3] / half if half else 0.0)

    first = min(candidates, key=lambda c: abs(c[2] - 0.55 * d_max) + abs(c[3]))
    chosen = [first]
    while len(chosen) < min(count, len(candidates)):
        best = max(
            (c for c in candidates if c not in chosen),
            key=lambda c: min(math.hypot(key(c)[0] - key(o)[0], key(c)[1] - key(o)[1]) for o in chosen),
        )
        chosen.append(best)
    return [[c[0], c[1]] for c in chosen]
