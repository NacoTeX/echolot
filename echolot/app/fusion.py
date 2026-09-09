"""Explainable confidence fusion for multi-device presence zones."""

import math
import time

FRESH_SECONDS = 5.0
STALE_SECONDS = 20.0
OCCUPIED_AT = 0.70
VACANT_AT = 0.30


def _sigmoid(value: float) -> float:
    # Bounding avoids overflow for extremely separated calibration datasets.
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, value))))


def _freshness(age: float) -> float:
    if age <= FRESH_SECONDS:
        return 1.0
    if age >= STALE_SECONDS:
        return 0.0
    return 1.0 - (age - FRESH_SECONDS) / (STALE_SECONDS - FRESH_SECONDS)


def device_evidence(sample: dict, profile: dict | None, *, now: float) -> dict:
    """Turn one raw sample into calibrated probability and reliability."""
    age = max(0.0, now - float(sample.get("t") or 0))
    freshness = _freshness(age)
    score = sample.get("movement_score")
    threshold = sample.get("threshold")
    motion = sample.get("motion")

    if profile and score is not None:
        noise = max(float(profile["noise"]), 0.01)
        centre = (float(profile["enter_threshold"]) + float(profile["exit_threshold"])) / 2
        probability = _sigmoid((float(score) - centre) / noise)
        calibration_quality = {"good": 1.0, "fair": 0.8, "poor": 0.55}.get(
            profile.get("quality"), 0.55
        )
        basis = "calibrated_score"
    elif score is not None and threshold is not None:
        scale = max(abs(float(threshold)) * 0.15, 0.1)
        probability = _sigmoid((float(score) - float(threshold)) / scale)
        calibration_quality = 0.65
        basis = "device_threshold"
    elif motion is not None:
        probability = 0.85 if motion else 0.15
        calibration_quality = 0.5
        basis = "motion_only"
    else:
        probability = 0.5
        calibration_quality = 0.0
        basis = "no_signal"

    reliability = freshness * calibration_quality
    return {
        "probability": round(probability, 4),
        "reliability": round(reliability, 4),
        "age_seconds": round(age, 2),
        "freshness": round(freshness, 4),
        "basis": basis,
        "score": score,
        "threshold": threshold,
    }


def fuse(members: list[dict]) -> dict:
    """Combine evidence without allowing many weak sensors to vote a room on."""
    usable = [member for member in members if member["reliability"] > 0]
    if not usable:
        return {
            "available": False,
            "state": "unavailable",
            "occupied": None,
            "confidence": None,
            "agreement": None,
            "members": members,
        }

    weight = sum(member["reliability"] for member in usable)
    confidence = sum(
        member["probability"] * member["reliability"] for member in usable
    ) / weight
    # Agreement is one at unanimity and zero when evidence spans both extremes.
    disagreement = sum(
        member["reliability"] * abs(member["probability"] - confidence)
        for member in usable
    ) / weight
    agreement = max(0.0, 1.0 - 2.0 * disagreement)

    if confidence >= OCCUPIED_AT:
        state = "occupied"
        occupied = True
    elif confidence <= VACANT_AT:
        state = "vacant"
        occupied = False
    else:
        state = "uncertain"
        occupied = None
    return {
        "available": True,
        "state": state,
        "occupied": occupied,
        "confidence": round(confidence, 4),
        "agreement": round(agreement, 4),
        "members": members,
    }


def evaluate_zone(zone, device_getter, source, profiles, *, now=None) -> dict:
    """Fuse a zone from the canonical sample stream.

    `source` used to be the direct telemetry hub specifically, so a
    device without ESPectre's Direct HTTP API contributed nothing at all
    — and that API is closed to this add-on at the pinned upstream (see
    app/ha_sampler.py). It is now the sample bus, which carries whichever
    transport is canonical; anything with a `snapshot(device_id,
    seconds=...)` works.
    """
    now = time.time() if now is None else now
    members = []
    for device_id in zone.device_ids:
        device = device_getter(device_id)
        snapshot = source.snapshot(device_id, seconds=STALE_SECONDS)
        points = snapshot.get("points") or []
        name = (
            device.config.friendly_name or device.config.name
            if device is not None
            else device_id
        )
        if not points:
            members.append(
                {
                    "device_id": device_id,
                    "name": name,
                    "probability": 0.5,
                    "reliability": 0.0,
                    "age_seconds": None,
                    "freshness": 0.0,
                    "basis": "no_recent_sample",
                    "score": None,
                    "threshold": None,
                }
            )
            continue
        members.append(
            {
                "device_id": device_id,
                "name": name,
                **device_evidence(points[-1], profiles.get(device_id), now=now),
            }
        )
    return {
        "zone_id": zone.id,
        "name": zone.name,
        # Which transport these numbers came from. Two rooms fused from
        # different transports are not comparable, and a reader should
        # not have to guess.
        "source": getattr(source, "source", None),
        **fuse(members),
    }
