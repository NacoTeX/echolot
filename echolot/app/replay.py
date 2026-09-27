"""A recording played back through the room engine.

The same RoomEngine the add-on runs, on a virtual clock, fed from the
recording instead of a radar link: every frame line through the link's
own sequence rules (LinkSnapshot.record), every connection as it came and
went. Nothing here is a second implementation of the rules — which is
the point: a change to them is judged by playing the same recording
through both.

Deterministic: the same recording with the same room gives the same
results, evaluation for evaluation. Evaluations happen after every
recorded line and connection change, and every IDLE_INTERVAL in between,
as the live engine's timer does — not at the live engine's exact
instants, which depend on the scheduler; the tracker takes each report
at its own time either way. Playback starts without history: targets are
confirmed as after a restart.

Errors are computed only where the recording says what was really going
on (recording.Mark). Without marks, a report describes what the engine
did — rates, changes, availability — and names no error. A standpoint is
judged on the evaluations after its mark, until it is left: somebody
presses the button once standing, and an evaluation at that very moment
still rests on reports from before. How many people were in the room,
and whether a zone was occupied, is judged on what was shown at every
moment — the delay until the engine caught up is part of the answer.
"""

import math
import statistics

from app import rooms
from app.devices import Device, DeviceCreate
from app.radar_frame import parse_frame
from app.radar_link import LinkSnapshot
from app.room_engine import IDLE_INTERVAL, RoomEngine

#: Statuses that count for the room (room_engine).
COUNTING = ("counted", "held")


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Links:
    def __init__(self, snap: LinkSnapshot) -> None:
        self._snap = snap

    def snapshot(self, device_id):
        return self._snap if device_id == self._snap.device_id else None


def _device(header: dict) -> Device:
    info = header["device"]
    fields = {
        "name": info.get("name") or "aufnahme",
        "friendly_name": info.get("friendly_name"),
        "board": info.get("board") or "esp32",
        "wifi_ssid": "-",
        "radar_quiet_means_empty": bool(info.get("radar_quiet_means_empty", False)),
    }
    try:
        config = DeviceCreate.model_validate(fields)
    except ValueError:
        # A name or board this version does not know: the engine needs only
        # the quiet rule.
        config = DeviceCreate.model_validate({**fields, "name": "aufnahme", "board": "esp32", "friendly_name": None})
    return Device(id=info["id"], created_at=0, updated_at=0, config=config)


def room_for(header: dict, settings: dict | None = None, current=None) -> rooms.Room:
    """The room to play a recording with.

    None: as recorded. `current`: that room's settings today, on the
    recorded sensor. `settings` then overrides the filters (confirm_s,
    smoothing) and the room's hold and assume times.
    """
    data = (current.model_dump(mode="json") if current is not None else dict(header["room"]))
    data["sensor"] = {**data["sensor"], "device_id": header["device"]["id"]}
    for key in ("hold_s", "assume_present_s"):
        if settings and settings.get(key) is not None:
            data[key] = settings[key]
    calibration = dict(data.get("calibration") or {})
    for key in ("confirm_s", "smoothing"):
        if settings and settings.get(key) is not None:
            calibration[key] = settings[key]
    data["calibration"] = calibration
    return rooms.Room.model_validate(data)


def run(header: dict, events: list[dict], room: rooms.Room) -> list[dict]:
    """[{"t": seconds, "result": the engine's result}, ...], one per
    evaluation, in order."""
    device = _device(header)
    snap = LinkSnapshot(device_id=device.id)
    clock = _Clock()
    engine = RoomEngine(_Links(snap), clock=clock)
    link = header.get("link") or {}
    if link.get("connected"):
        snap.new_session()
        snap.connected = True
        snap.no_frame_entity = bool(link.get("no_frame_entity"))
    last = header.get("last_line")
    if last and snap.connected:
        try:
            snap.record(parse_frame(last["text"]), -float(last.get("age_s") or 0.0))
        except ValueError:
            pass
    engine.load([room], [device])
    timeline: list[dict] = []

    def evaluate(t: float) -> None:
        clock.now = t
        result = engine.evaluate()[0]
        result.pop("evaluated_at", None)
        timeline.append({"t": round(t, 4), "result": result})

    evaluate(0.0)
    last_eval = 0.0
    for event in events:
        t = event["t"]
        while last_eval + IDLE_INTERVAL < t:
            last_eval += IDLE_INTERVAL
            evaluate(last_eval)
        clock.now = t
        if event["type"] == "line":
            try:
                frame = parse_frame(event["text"])
            except ValueError:
                continue
            if not snap.connected:
                continue  # a line with no connection cannot be; as live
            snap.record(frame, t)
        elif event["type"] == "link":
            if event["connected"]:
                snap.new_session()
                snap.connected = True
                snap.no_frame_entity = bool(event.get("no_frame_entity"))
            else:
                snap.connected = False
        else:
            continue
        evaluate(t)
        last_eval = t
    return timeline


# --- what a playback says -----------------------------------------------------------


def _p95(values: list[float]) -> float | None:
    """Nearest rank: the smallest value at or above 95 % of them."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _spans(timeline: list[dict], end: float) -> list[tuple[dict, float]]:
    """Each evaluation with the time it stands for, until the next one."""
    out = []
    for i, entry in enumerate(timeline):
        until = timeline[i + 1]["t"] if i + 1 < len(timeline) else end
        out.append((entry, max(0.0, until - entry["t"])))
    return out


def _intervals(marks: list[dict], start_kind: str, stop_kinds: tuple, end: float) -> list[tuple[float, float, dict]]:
    """(from, to, mark) for each mark of `start_kind`, until the next one of
    those kinds or the end."""
    out = []
    for i, mark in enumerate(marks):
        if mark["kind"] != start_kind:
            continue
        stop = next((m["t"] for m in marks[i + 1:] if m["kind"] in stop_kinds), end)
        out.append((mark["t"], stop, mark))
    return out


def _round(value, digits=3):
    return None if value is None else round(value, digits)


def report(header: dict, events: list[dict], timeline: list[dict]) -> dict:
    """What the engine did with a recording, and — where marks say what was
    really going on — how far off it was."""
    end = max([e["t"] for e in events] + [timeline[-1]["t"] if timeline else 0.0])
    spans = _spans(timeline, end)
    available = [(e, d) for e, d in spans if e["result"]["available"]]
    lines = [e for e in events if e["type"] == "line"]

    def changes(key):
        values = [e["result"][key] for e, _ in available]
        return sum(1 for a, b in zip(values, values[1:]) if a != b)

    summary = {
        "duration_s": _round(end, 1),
        "evaluations": len(timeline),
        "lines": len(lines),
        "available_share": _round(sum(d for _, d in available) / end if end else None),
        "count_changes": changes("count"),
        "occupied_changes": changes("occupied"),
        "reports_per_s": _round(len({e["text"] for e in lines}) / end, 2) if end else None,
    }
    marks = [e for e in events if e["type"] == "mark"]
    out = {"summary": summary, "reference": bool(marks)}
    if not marks:
        return out

    # Standpoints: somebody standing still at a marked spot.
    standpoints = []
    all_errors: list[float] = []
    jitter_sq: list[float] = []
    detected = total = 0.0
    switches = 0
    for t0, t1, mark in _intervals(marks, "standpoint", ("standpoint", "standpoint_end"), end):
        errors, positions, ids = [], [], []
        seen = time = 0.0
        for entry, d in spans:
            if not t0 < entry["t"] <= t1 or not entry["result"]["available"]:
                continue
            time += d
            counting = [t for t in entry["result"]["targets"] if t["status"] in COUNTING]
            if not counting:
                continue
            seen += d
            near = min(counting, key=lambda t: math.hypot(t["x"] - mark["x"], t["y"] - mark["y"]))
            errors.append(math.hypot(near["x"] - mark["x"], near["y"] - mark["y"]))
            positions.append((near["x"], near["y"]))
            if not ids or ids[-1] != near["id"]:
                ids.append(near["id"])
        if positions:
            mx = statistics.fmean(p[0] for p in positions)
            my = statistics.fmean(p[1] for p in positions)
            jitter_sq += [(x - mx) ** 2 + (y - my) ** 2 for x, y in positions]
        all_errors += errors
        detected += seen
        total += time
        switches += max(0, len(ids) - 1)
        standpoints.append({
            "x": mark["x"], "y": mark["y"], "uncertainty_m": mark["uncertainty_m"],
            "from_s": _round(t0, 1), "to_s": _round(t1, 1),
            "detected_share": _round(seen / time if time else None),
            "median_error_m": _round(statistics.median(errors) if errors else None),
            "p95_error_m": _round(_p95(errors)),
            "id_changes": max(0, len(ids) - 1),
        })
    if standpoints:
        out["standpoints"] = {
            "spots": standpoints,
            "median_error_m": _round(statistics.median(all_errors) if all_errors else None),
            "p95_error_m": _round(_p95(all_errors)),
            # Scatter around where the engine put somebody standing still,
            # whatever the offset from the mark.
            "jitter_m": _round(math.sqrt(statistics.fmean(jitter_sq)) if jitter_sq else None),
            "detected_share": _round(detected / total if total else None),
            "id_changes": switches,
            "reference_uncertainty_m": max(s["uncertainty_m"] for s in standpoints),
        }

    # People: how many were really in the room.
    people = _intervals(marks, "people", ("people",), end)
    if people:
        match = false_occupied = missed = judged = 0.0
        for t0, t1, mark in people:
            for entry, d in spans:
                if not t0 <= entry["t"] < t1 or not entry["result"]["available"]:
                    continue
                judged += d
                result = entry["result"]
                match += d if result["count"] == mark["count"] else 0.0
                if mark["count"] == 0 and result["occupied"]:
                    false_occupied += d
                if mark["count"] > 0 and not result["occupied"]:
                    missed += d
        out["people"] = {
            "judged_s": _round(judged, 1),
            "count_right_share": _round(match / judged if judged else None),
            "occupied_while_empty_s": _round(false_occupied, 1),
            "empty_while_occupied_s": _round(missed, 1),
        }

    # Zones: somebody going into and out of a detection zone.
    zone_marks = [m for m in marks if m["kind"] == "zone"]
    if zone_marks:
        zones_out = {}
        for zone_id in sorted({m["zone_id"] for m in zone_marks}):
            ref = [m for m in zone_marks if m["zone_id"] == zone_id]
            states = [(e["t"], z["occupied"]) for e, _ in available
                      for z in e["result"]["zones"] if z["id"] == zone_id]
            measured = sum(1 for (_, a), (_, b) in zip(states, states[1:]) if a != b)
            reference = sum(1 for a, b in zip([False] + [m["inside"] for m in ref], [m["inside"] for m in ref]) if a != b)

            def latency(mark, want):
                later = [t for t, occupied in states if t >= mark["t"] and occupied == want]
                return later[0] - mark["t"] if later else None

            enters = [latency(m, True) for m in ref if m["inside"]]
            leaves = [latency(m, False) for m in ref if not m["inside"]]
            zones_out[zone_id] = {
                "reference_changes": reference,
                "measured_changes": measured,
                "extra_changes": max(0, measured - reference),
                "enter_latency_s": [_round(v, 2) for v in enters],
                "leave_latency_s": [_round(v, 2) for v in leaves],
            }
        out["zones"] = zones_out
    return out


def compare(header: dict, events: list[dict], variants: list[dict], timeline: bool = False) -> list[dict]:
    """The same recording through several rooms: [{"label", "report"}],
    each variant {"label", "room": rooms.Room}. With `timeline`, the first
    also carries its evaluations for the viewer (compact)."""
    out = []
    for i, variant in enumerate(variants):
        evaluations = run(header, events, variant["room"])
        item = {"label": variant["label"], "report": report(header, events, evaluations)}
        if timeline and i == 0:
            item["timeline"] = compact(evaluations)
        out.append(item)
    return out


def compact(timeline: list[dict], max_rate_hz: float = 10.0) -> list[dict]:
    """The timeline for the viewer: at most `max_rate_hz` evaluations a
    second, each with what the plan draws."""
    out = []
    last = -math.inf
    for i, entry in enumerate(timeline):
        is_last = i == len(timeline) - 1
        if entry["t"] - last < 1.0 / max_rate_hz and not is_last:
            continue
        last = entry["t"]
        r = entry["result"]
        out.append({
            "t": entry["t"],
            "available": r["available"],
            "reason": r.get("reason_text"),
            "count": r["count"],
            "occupied": r["occupied"],
            "assumed_present": r.get("assumed_present"),
            "targets": [{"id": t["id"], "x": t["x"], "y": t["y"], "status": t["status"]} for t in r["targets"]],
            "zones": [{"id": z["id"], "count": z["count"], "occupied": z["occupied"]} for z in r["zones"]],
        })
    return out
