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
