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


def test_the_plan_labels_keep_their_letters_apart():
    """letter-spacing computes to a length where it is set and is inherited
    as that length. From the page it reached the plan's labels, a sixth of
    a unit high in metres, and laid their letters on top of each other."""
    css = re.sub(r"/\*.*?\*/", "", (STATIC / "style.css").read_text(encoding="utf-8"), flags=re.S)
    rules = [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]
    for selector, body in rules:
        names = {name.strip() for name in selector.split(",")}
        if names & {"html", "body", "body:not(.embed)", ":root"}:
            assert "letter-spacing" not in body, selector
    assert any(sel == "svg text" and "letter-spacing: normal" in body for sel, body in rules)
