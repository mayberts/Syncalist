"""Generates the Syncalist logo SVGs. Run: python branding/make_logo.py

Mark: the standard "playlist" glyph (three lines and a music note) inside two
circular sync arrows, on an amber rounded square. A simplified version without the
arrowheads' fine detail is used for tiny favicons.
"""

import math
from pathlib import Path

HERE = Path(__file__).parent
INK = "#1d1a16"


def polar(cx, cy, r, deg):
    a = math.radians(deg)
    return cx + r * math.cos(a), cy + r * math.sin(a)


def arc_with_head(cx, cy, r, start, end, width, head):
    """Clockwise arc from start to end degrees, with an arrowhead at the end."""
    x1, y1 = polar(cx, cy, r, start)
    x2, y2 = polar(cx, cy, r, end - head * 0.55)  # stop the stroke under the arrowhead
    large = 1 if (end - start) % 360 > 180 else 0
    path = f'<path d="M{x1:.1f},{y1:.1f} A{r},{r} 0 {large} 1 {x2:.1f},{y2:.1f}" fill="none" ' \
           f'stroke="{INK}" stroke-width="{width}" stroke-linecap="round"/>'
    # arrowhead: tip ahead along the circle, base across the stroke
    tip = polar(cx, cy, r, end + head * 0.35)
    bx, by = polar(cx, cy, r, end - head * 0.65)
    nx, ny = math.cos(math.radians(end - head * 0.65)), math.sin(math.radians(end - head * 0.65))
    hw = width * 1.25
    p1 = (bx + nx * hw, by + ny * hw)
    p2 = (bx - nx * hw, by - ny * hw)
    tri = f'<path d="M{tip[0]:.1f},{tip[1]:.1f} L{p1[0]:.1f},{p1[1]:.1f} L{p2[0]:.1f},{p2[1]:.1f} Z" ' \
          f'fill="{INK}" stroke="{INK}" stroke-width="{width * 0.35:.1f}" stroke-linejoin="round"/>'
    return path + tri


def background(radius=112):
    return f'''<defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#f7b733"/>
      <stop offset="1" stop-color="#e3820f"/>
    </linearGradient>
  </defs>
  <rect width="512" height="512" rx="{radius}" fill="url(#bg)"/>'''


def playlist_glyph(scale=1.0, cx=256, cy=256):
    """Three list lines plus a music note, centred on (cx, cy)."""
    s = scale
    def X(v): return cx + (v - 256) * s
    def Y(v): return cy + (v - 256) * s
    w = 22 * s
    lines = "".join(
        f'<line x1="{X(172):.1f}" y1="{Y(y):.1f}" x2="{X(x2):.1f}" y2="{Y(y):.1f}" stroke="{INK}" '
        f'stroke-width="{w:.1f}" stroke-linecap="round"/>'
        for y, x2 in ((200, 290), (246, 290), (292, 238)))
    # note: head at bottom right, stem, flag
    note = (f'<ellipse cx="{X(296):.1f}" cy="{Y(320):.1f}" rx="{30 * s:.1f}" ry="{24 * s:.1f}" '
            f'transform="rotate(-20 {X(296):.1f} {Y(320):.1f})" fill="{INK}"/>'
            f'<rect x="{X(311):.1f}" y="{Y(176):.1f}" width="{17 * s:.1f}" height="{146 * s:.1f}" '
            f'rx="{8 * s:.1f}" fill="{INK}"/>'
            f'<path d="M{X(320):.1f},{Y(176):.1f} C{X(352):.1f},{Y(184):.1f} {X(364):.1f},{Y(206):.1f} '
            f'{X(352):.1f},{Y(238):.1f} C{X(350):.1f},{Y(214):.1f} {X(338):.1f},{Y(206):.1f} '
            f'{X(320):.1f},{Y(204):.1f} Z" fill="{INK}"/>')
    return lines + note


def tiny():
    """16-32 px: no arrows (they turn to mush), just a bold playlist glyph."""
    lines = "".join(
        f'<rect x="88" y="{y}" width="{w}" height="56" rx="28" fill="{INK}"/>'
        for y, w in ((104, 228), (228, 228), (352, 140)))
    note = (f'<ellipse cx="352" cy="384" rx="62" ry="50" transform="rotate(-20 352 384)" fill="{INK}"/>'
            f'<rect x="374" y="96" width="40" height="292" rx="18" fill="{INK}"/>'
            f'<path d="M394,96 C452,108 470,150 448,206 C446,170 426,156 394,154 Z" fill="{INK}"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-label="Syncalist">\n'
            f'  {background(128)}\n  {lines}{note}\n</svg>\n')


def logo(small=False):
    if small:
        # Favicon: bigger glyph, thicker arrows, no fine gaps.
        arrows = (arc_with_head(256, 256, 176, 205, 335, 40, 26)
                  + arc_with_head(256, 256, 176, 25, 155, 40, 26))
        glyph = playlist_glyph(1.22, 262, 254)
        radius = 120
    else:
        arrows = (arc_with_head(256, 256, 170, 200, 335, 30, 22)
                  + arc_with_head(256, 256, 170, 20, 155, 30, 22))
        glyph = playlist_glyph(1.0, 250, 254)
        radius = 112
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-label="Syncalist">\n'
            f'  {background(radius)}\n  {arrows}\n  {glyph}\n</svg>\n')


if __name__ == "__main__":
    (HERE / "logo.svg").write_text(logo())
    (HERE / "logo-small.svg").write_text(logo(small=True))
    (HERE / "logo-tiny.svg").write_text(tiny())
    print("wrote logo.svg, logo-small.svg, logo-tiny.svg")
