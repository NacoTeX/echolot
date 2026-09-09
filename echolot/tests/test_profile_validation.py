"""A damaged profile must not take the other rooms with it.

From the follow-up review (R7). `profile_from_dict({"version": "broken"})`
raised ValueError, because the version was converted outside the try
block — and that call sits on the shared evaluation pass, so one
hand-edited or half-written record could stop every zone.
"""

import math
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import presence_rate  # noqa: E402


def valid(**overrides) -> dict:
    return {
        "crossing_threshold": 1e-3,
        "baseline_rate": 0.084,
        "baseline_spread": 0.01,
        "window_seconds": 60.0,
        "sample_count": 1200,
        "observed_seconds": 1200.0,
        "version": presence_rate.PROFILE_VERSION,
        **overrides,
    }


def test_a_valid_profile_still_works():
    profile = presence_rate.profile_from_dict(valid())
    assert profile is not None
    assert profile.window_seconds == 60.0
    assert presence_rate.profile_status(valid()) == presence_rate.USABLE


@pytest.mark.parametrize("version", ["broken", None, [], {}, "2.0", object()])
def test_an_unreadable_version_returns_rather_than_raises(version):
    """The reproduction from the review, as a regression test."""
    data = valid(version=version)
    assert presence_rate.profile_from_dict(data) is None
    assert presence_rate.profile_status(data) in (
        presence_rate.MALFORMED,
        presence_rate.OUTDATED,
    )


@pytest.mark.parametrize("field", ["baseline_rate", "crossing_threshold", "window_seconds"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_refused(field, bad):
    """They survive float() and then make every comparison False, so a
    profile carrying one reads as a room that is never occupied."""
    assert presence_rate.profile_from_dict(valid(**{field: bad})) is None


@pytest.mark.parametrize("window", [0, -1, -60.0])
def test_a_window_of_zero_or_less_is_refused(window):
    """It divides by zero downstream."""
    assert presence_rate.profile_from_dict(valid(window_seconds=window)) is None


def test_a_negative_baseline_is_refused():
    """Every ratio against it is meaningless and every room occupied."""
    assert presence_rate.profile_from_dict(valid(baseline_rate=-0.5)) is None


@pytest.mark.parametrize("field", ["crossing_threshold", "baseline_rate", "window_seconds"])
def test_a_missing_required_field_is_refused(field):
    data = valid()
    del data[field]
    assert presence_rate.profile_from_dict(data) is None
    assert presence_rate.profile_status(data) == presence_rate.MALFORMED


@pytest.mark.parametrize("junk", [None, "", [], 42, "profil"])
def test_no_profile_at_all_is_missing_not_malformed(junk):
    """Most devices have none, and that is not a fault."""
    assert presence_rate.profile_status(junk) == presence_rate.MISSING
    assert presence_rate.profile_from_dict(junk) is None


def test_an_older_version_is_outdated_not_malformed():
    """It needs a recalibration, not a look at the file."""
    assert presence_rate.profile_status(valid(version=1)) == presence_rate.OUTDATED


def test_nothing_in_the_read_path_raises():
    """The property being defended: never an unhandled exception."""
    for junk in [
        {"version": "broken"}, {"version": float("nan")}, {"window_seconds": "sechzig"},
        {"crossing_threshold": None}, valid(sample_count="viele"),
        valid(observed_seconds=float("inf")), valid(baseline_spread="breit"),
    ]:
        assert presence_rate.profile_from_dict(junk) is None
        assert isinstance(presence_rate.profile_status(junk), str)
