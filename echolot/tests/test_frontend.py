"""Checks on the page scripts that need no browser."""

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
METHOD = re.compile(r"^    (?:async )?([A-Za-z_]\w*)\([^)]*\)\s*\{", re.M)
ASSIGNED = re.compile(r"\bthis\.([A-Za-z_]\w*)\s*=(?!=)")


@pytest.mark.parametrize("path", sorted(STATIC.glob("*.js")), ids=lambda p: p.name)
def test_no_view_overwrites_its_own_methods(path):
    """A view is one object: `this.mount = 2.2` for a typed height replaced
    its mount() method, and the page could not be opened again."""
    text = path.read_text(encoding="utf-8")
    methods = set(METHOD.findall(text))
    clashes = sorted(methods & set(ASSIGNED.findall(text)))
    assert clashes == [], f"{path.name}: this.{clashes[0]} = … overwrites the method {clashes[0]}()"


# --- story.js: the room in words ------------------------------------------------

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402

STORY = STATIC / "story.js"
ROOM = {"zones": [{"id": "sofa", "name": "Sofa"}, {"id": "desk", "name": "Schreibtisch"}]}


def say(live):
    script = (f"const S = require({json.dumps(str(STORY))});"
              f"const fmt = (s) => Math.round(s) + ' s';"
              f"process.stdout.write(JSON.stringify(S.describe({json.dumps(ROOM)}, {json.dumps(live)}, fmt)));")
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


def target(status="counted", zones=(), velocity=(0, 0), resting=False):
    return {"status": status, "zones": list(zones), "velocity": list(velocity), "resting": resting}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("live, expected", [
    (None, None),
    ({"available": False}, None),
    ({"available": True, "targets": []}, "Niemand im Raum."),
    ({"available": True, "targets": [], "occupied": True, "hold_remaining": 12},
     "Eben war noch jemand da — der Raum gilt noch 12 s als belegt."),
    ({"available": True, "targets": [], "assumed_present": True},
     "Gerade nicht zu sehen — vermutlich sitzt noch jemand still im Raum."),
    ({"available": True, "targets": [target(zones=["sofa"], resting=True)]},
     "Eine Person verweilt im Bereich „Sofa“."),
    ({"available": True, "targets": [target(velocity=(0.8, 0.2))]}, "Eine Person bewegt sich durch den Raum."),
    # Resting is resting, whatever the speed estimate still says just after
    # somebody sat down.
    ({"available": True, "targets": [target(zones=["sofa"], velocity=(0.5, 0), resting=True)]},
     "Eine Person verweilt im Bereich „Sofa“."),
    # Slow, not resting: still, not "moving".
    ({"available": True, "targets": [target(velocity=(0.1, 0.1))]}, "Eine Person verweilt im Raum."),
    # Not counted: not mentioned; held: counts, but never as moving.
    ({"available": True, "targets": [target(status="pending", velocity=(1, 0)), target(status="held", velocity=(1, 0))]},
     "Eine Person verweilt im Raum."),
    ({"available": True, "targets": [target(zones=["sofa"], resting=True), target(velocity=(0.9, 0)),
                                     target(zones=["desk"], resting=True)]},
     "Drei Personen: eine ruhig im Bereich „Sofa“, eine in Bewegung und eine ruhig im Bereich „Schreibtisch“."),
])
def test_the_room_is_told_by_what_the_result_says(live, expected):
    assert say(live) == expected
