"""The page's own files: its icons."""

import re
from collections import Counter
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def symbols() -> Counter:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return Counter(re.findall(r'<symbol id="i-([a-z0-9-]+)"', html))


def test_every_icon_is_drawn_once():
    """A second symbol with the same id is ignored by one browser and not
    by the next."""
    twice = [name for name, n in symbols().items() if n > 1]
    assert twice == []


def test_every_icon_the_pages_ask_for_is_there():
    drawn = set(symbols())
    asked = set()
    for script in STATIC.glob("*.js"):
        source = script.read_text(encoding="utf-8")
        # icon("name"), and names picked by a condition: icon(a ? "x" : "y").
        for call in re.findall(r'\bicon\(([^()]*)\)', source):
            asked.update(re.findall(r'"([a-z0-9-]+)"', call))
    missing = sorted(asked - drawn - {"icon"})
    assert missing == []
