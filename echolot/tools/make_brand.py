"""Render icon.png and logo.png.

Kept as a script rather than two opaque binaries: the mark is geometry, and
geometry that lives in code can be adjusted later without a design tool.

The mark is a sonar scope, because that is what "Echolot" means and what
the add-on does — an emitter in the middle, range rings going out, a
bearing line, and one contact that came back. The icon before this was the
generic Wi-Fi fan, which says "wireless" and nothing else; half the
integrations in the sidebar use it.

Three decisions that came out of looking at the result rather than
planning it:

  * The contact is amber, not white. White with a glow reads as a moon.
    Amber against the teal reads as a find, and it is the only warm pixel
    in a sidebar full of blue.
  * Three rings, not four. The fourth crowded the corners and cost the
    contact its space at small sizes.
  * The bearing line stays. Without it the rings plus centre dot are a
    bullseye — a target, which is the wrong verb.

Run from the add-on directory:

    python3 tools/make_brand.py
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent

#: Drawn at four times the delivered size and downsampled, which is the
#: cheapest antialiasing there is for pure geometry.
SUPERSAMPLE = 4

BG_TOP = (16, 62, 82)      # deep water
BG_BOTTOM = (18, 26, 66)   # deep water, further down
RING = (94, 234, 212)      # the pulse
CONTACT = (252, 211, 77)   # what came back
TEXT = (241, 245, 249)

#: Radius, stroke width and opacity of each range ring, as fractions of the
#: canvas. The pulse weakens as it travels, so the outer rings are thinner
#: and fainter: that is what makes it one pulse going out rather than three
#: circles sitting there.
RINGS = ((0.190, 0.030, 230), (0.310, 0.024, 155), (0.420, 0.019, 100))

#: Where the contact sits. Off-axis on purpose — symmetry would look
#: calmer and would say nothing, and the point of the device is that
#: something is in the room.
CONTACT_ANGLE = math.radians(-42)

#: Whichever of these exists renders the wordmark. The committed logo.png
#: was made with Outfit-Bold; the others are legible fallbacks, not
#: matches, so regenerating without that font changes the logo.
FONT_CANDIDATES = (
    "/mnt/skills/examples/canvas-design/canvas-fonts/Outfit-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def _font(size: int):
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _gradient(size: tuple[int, int]) -> Image.Image:
    """A vertical gradient, one row at a time."""
    width, height = size
    image = Image.new("RGB", size)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        t = y / max(1, height - 1)
        draw.line(
            [(0, y), (width, y)],
            fill=tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)),
        )
    return image


def _rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size[0] - 1, size[1] - 1], radius, fill=255
    )
    return mask


def sonar(size: int) -> Image.Image:
    """The mark alone, transparent, `size` pixels square."""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    centre = size / 2

    distance = size * RINGS[-1][0]
    bx = centre + math.cos(CONTACT_ANGLE) * distance
    by = centre + math.sin(CONTACT_ANGLE) * distance

    # Bearing first, so the rings cross over it rather than under.
    draw.line([centre, centre, bx, by], fill=RING + (100,), width=max(1, round(size * 0.012)))

    for radius_frac, width_frac, alpha in RINGS:
        r = size * radius_frac
        draw.ellipse(
            [centre - r, centre - r, centre + r, centre + r],
            outline=RING + (alpha,),
            width=max(1, round(size * width_frac)),
        )

    r = size * 0.052
    draw.ellipse([centre - r, centre - r, centre + r, centre + r], fill=RING + (255,))

    r = size * 0.044
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        [bx - r * 2, by - r * 2, bx + r * 2, by + r * 2], fill=CONTACT + (65,)
    )
    layer = Image.alpha_composite(layer, glow.filter(ImageFilter.GaussianBlur(size * 0.020)))
    ImageDraw.Draw(layer).ellipse([bx - r, by - r, bx + r, by + r], fill=CONTACT + (255,))
    return layer


def make_icon(path: Path, size: int = 256) -> None:
    big = size * SUPERSAMPLE
    canvas = Image.alpha_composite(_gradient((big, big)).convert("RGBA"), sonar(big))
    canvas.putalpha(_rounded_mask((big, big), round(big * 0.225)))
    canvas.resize((size, size), Image.LANCZOS).save(path)


def _fit(text: str, box: int, start: int) -> "ImageFont.FreeTypeFont":
    """The largest of these sizes whose text still fits in `box` pixels.

    Measured rather than guessed: the fallback fonts are wider than Outfit,
    so a size that fits with one runs off the canvas with another.
    """
    size = start
    while size > 8:
        font = _font(size)
        if font.getbbox(text)[2] - font.getbbox(text)[0] <= box:
            return font
        size -= 2
    return _font(8)


def make_logo(path: Path, size: tuple[int, int] = (500, 250)) -> None:
    width, height = (v * SUPERSAMPLE for v in size)
    canvas = _gradient((width, height)).convert("RGBA")

    # The mark sits square at the left, inset so the outer ring does not
    # run into the edge.
    mark_size = round(height * 0.78)
    inset = round((height - mark_size) / 2)
    mark = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    mark.alpha_composite(sonar(mark_size), (inset, inset))
    canvas = Image.alpha_composite(canvas, mark)

    left = inset + mark_size + round(height * 0.10)
    box = width - left - round(height * 0.10)

    NAME = "Echolot"
    TAGLINE = "W I - F I   C S I   P R E S E N C E"

    draw = ImageDraw.Draw(canvas)
    draw.text(
        (left, height * 0.545),
        NAME,
        font=_fit(NAME, box, round(height * 0.30)),
        fill=TEXT,
        anchor="ls",
    )
    draw.text(
        (left + round(height * 0.012), height * 0.70),
        TAGLINE,
        font=_fit(TAGLINE, box, round(height * 0.090)),
        fill=RING,
        anchor="ls",
    )
    canvas.convert("RGB").resize(size, Image.LANCZOS).save(path)


if __name__ == "__main__":
    make_icon(ROOT / "icon.png")
    make_logo(ROOT / "logo.png")
    print("wrote icon.png and logo.png")
