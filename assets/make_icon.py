"""Generate the claude-use icon (JMR-159): icon.svg, icon.ico, icon.png.

The geometry below is the one source: it's written out as SVG, and drawn with
Pillow (supersampled) for the raster sizes. Pillow is only needed to run this
script, by hand, when the icon changes; the generated files are committed and
the app itself stays standard library only.

    pip install pillow && python assets/make_icon.py
"""

from __future__ import annotations

import math
from pathlib import Path

HERE = Path(__file__).parent
S = 256  # design canvas

BG = "#15171c"
# the dashboard's series colours, blue (s1) to green (s2), with lighter caps
BARS = [  # x, top, colour, cap colour
    (40, 156, "#2a78d6", "#5d9ce6"),
    (88, 120, "#2588c4", "#5aa5d6"),
    (136, 84, "#209ba0", "#55b8bc"),
    (184, 44, "#1baf7a", "#52c89c"),
]
BAR_W, BASE, CAP = 38, 220, 18
NOTE = dict(cx=84, cy=64, w=88, h=52, r=10, angle=-25, fill="#22b45c", medal="#137a3c")
WING = "#eef2f6"
FEATHERS = [  # cx, cy, rx, ry, angle: behind the note's left end, sweeping up
    (40, 98, 28, 7, -5),
    (32, 80, 30, 7, -30),
    (34, 58, 26, 7, -55),
]


def shapes(small: bool) -> list[tuple]:
    """Everything to draw, back to front. small: drop details a 16-24 px icon can't show."""
    out = [("rrect", 0, 0, S, S, 52, 0, BG)]
    for x, top, colour, cap in BARS:
        out.append(("rrect", x, top, BAR_W, BASE - top, 8, 0, colour))
        out.append(("rrect", x, top, BAR_W, CAP, 8, 0, cap))
    if not small:
        out += [("ellipse", *f, WING) for f in FEATHERS]
    n = NOTE
    out.append(("rrect", n["cx"] - n["w"] / 2, n["cy"] - n["h"] / 2, n["w"], n["h"], n["r"],
                n["angle"], n["fill"]))
    out.append(("ellipse", n["cx"], n["cy"], 15, 15, 0, n["medal"]))
    return out


# --- SVG ----------------------------------------------------------------

def svg(small: bool = False) -> str:
    parts = []
    for kind, *a in shapes(small):
        if kind == "rrect":
            x, y, w, h, r, angle, fill = a
            rot = (f' transform="rotate({angle} {x + w / 2:g} {y + h / 2:g})"' if angle else "")
            parts.append(f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" rx="{r:g}"'
                         f' fill="{fill}"{rot}/>')
        else:
            cx, cy, rx, ry, angle, fill = a
            rot = f' transform="rotate({angle} {cx:g} {cy:g})"' if angle else ""
            parts.append(f'<ellipse cx="{cx:g}" cy="{cy:g}" rx="{rx:g}" ry="{ry:g}"'
                         f' fill="{fill}"{rot}/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {S} {S}">'
            + "".join(parts) + "</svg>\n")


# --- raster -------------------------------------------------------------

def _rotate(pts, cx, cy, angle):
    t = math.radians(angle)
    c, s = math.cos(t), math.sin(t)
    return [(cx + (x - cx) * c - (y - cy) * s, cy + (x - cx) * s + (y - cy) * c) for x, y in pts]


def _rrect_pts(x, y, w, h, r, steps=12):
    pts = []
    for cx, cy, start in ((x + w - r, y + r, -90), (x + w - r, y + h - r, 0),
                          (x + r, y + h - r, 90), (x + r, y + r, 180)):
        for i in range(steps + 1):
            t = math.radians(start + 90 * i / steps)
            pts.append((cx + r * math.cos(t), cy + r * math.sin(t)))
    return pts


def _ellipse_pts(cx, cy, rx, ry, steps=48):
    return [(cx + rx * math.cos(2 * math.pi * i / steps), cy + ry * math.sin(2 * math.pi * i / steps))
            for i in range(steps)]


def render(size: int, small: bool = False):
    from PIL import Image, ImageDraw

    k = 8 * size / S  # draw 8x, then downsample: antialiasing
    im = Image.new("RGBA", (8 * size, 8 * size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for kind, *a in shapes(small):
        if kind == "rrect":
            x, y, w, h, r, angle, fill = a
            pts = _rotate(_rrect_pts(x, y, w, h, r), x + w / 2, y + h / 2, angle)
        else:
            cx, cy, rx, ry, angle, fill = a
            pts = _rotate(_ellipse_pts(cx, cy, rx, ry), cx, cy, angle)
        d.polygon([(px * k, py * k) for px, py in pts], fill=fill)
    return im.resize((size, size), Image.LANCZOS)


FAVICON_RE = r'<link rel="icon"[^>]*>'


def write_favicon() -> None:
    """The dashboard's favicon, inline so the page stays self-contained. The
    small variant: a browser tab is 16 px."""
    import base64
    import re

    template = HERE.parent / "template.html"
    data = base64.b64encode(svg(small=True).encode("utf-8")).decode("ascii")
    tag = f'<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,{data}">'
    html = template.read_text(encoding="utf-8")
    html, n = re.subn(FAVICON_RE, lambda _: tag, html, count=1)
    if not n:
        html = html.replace("<title>", tag + "\n<title>", 1)
    template.write_text(html, encoding="utf-8", newline="\n")


def main() -> None:
    (HERE / "icon.svg").write_text(svg(), encoding="utf-8")
    write_favicon()
    render(256).save(HERE / "icon.png")
    # each size drawn on its own, the small ones without the wing
    sizes = [16, 24, 32, 48, 256]
    imgs = [render(n, small=n <= 24) for n in sizes]
    imgs[-1].save(HERE / "icon.ico", sizes=[(n, n) for n in sizes], append_images=imgs[:-1])
    print("wrote icon.svg, icon.png, icon.ico and the favicon in template.html")


if __name__ == "__main__":
    main()
