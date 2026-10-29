"""Draw the theme wallpapers: one original SVG scene per theme, in that theme's colours.

Deterministic (seeded per theme), a few KB each, sharp at any size. Re-run after changing a
palette:  uv run python scripts/make_wallpapers.py
Output: zhunt/workspace/css/wallpapers/<theme>.svg (legacy illustrations only).
"""

from __future__ import annotations

import math
import random
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "zhunt" / "workspace" / "css" / "wallpapers"
W, H = 1600, 1000

# name: (sky top, sky bottom, accents..., scene)
PALETTES = {
    "tokyo-night": (["#0f1020", "#1a1b26", "#24283b"], ["#7aa2f7", "#bb9af7", "#7dcfff", "#ff9e64"], "city"),
    "nord": (["#1b2029", "#2e3440", "#3b4252"], ["#88c0d0", "#81a1c1", "#a3be8c", "#b48ead", "#eceff4"], "peaks"),
    "everforest": (["#1e2326", "#2d353b", "#3d484d"], ["#a7c080", "#83c092", "#dbbc7f", "#e69875"], "forest"),
    "gruvbox": (["#1d2021", "#3c3836", "#7c3a14"], ["#fe8019", "#fabd2f", "#fb4934", "#d3869b"], "dunes"),
    "kanagawa": (["#16161d", "#1f1f28", "#223249"], ["#7e9cd8", "#7fb4ca", "#dcd7ba", "#ffa066"], "waves"),
    "catppuccin": (["#11111b", "#1e1e2e", "#313244"], ["#cba6f7", "#f5c2e7", "#89b4fa", "#fab387"], "hills"),
    "catppuccin-latte": (["#dce0e8", "#eff1f5", "#f5e0dc"], ["#8839ef", "#ea76cb", "#1e66f5", "#fe640b"], "hills"),
    "rose-pine": (["#13111c", "#191724", "#26233a"], ["#ebbcba", "#c4a7e7", "#eb6f92", "#f6c177"], "petals"),
    "matte-black": (["#070707", "#0e0e0e", "#161616"], ["#e8a33d", "#3a3a3a", "#5a5a5a", "#d6d6d6"], "arcs"),
    "flexoki-light": (["#fffcf0", "#f2f0e5", "#e6e4d9"], ["#205ea6", "#24837b", "#403e3c", "#bc5215"], "ink"),
}


def ridge(rng: random.Random, base: float, amp: float, steps: int = 14, jag: float = 0.0) -> str:
    """A closed silhouette path from left to right edge, filled to the bottom."""

    pts = []
    for i in range(steps + 1):
        x = W * i / steps
        y = base + math.sin(i * 0.9 + rng.random() * 3) * amp * 0.5 + (rng.random() - 0.5) * amp + (rng.random() - 0.5) * jag
        pts.append((x, y))
    d = f"M0,{H} L0,{pts[0][1]:.0f} "
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        mx = (x0 + x1) / 2
        d += f"Q{x0:.0f},{y0:.0f} {mx:.0f},{(y0 + y1) / 2:.0f} "
    d += f"L{W},{pts[-1][1]:.0f} L{W},{H} Z"
    return d


def stars(rng: random.Random, n: int, color: str, top: float = 0.55) -> str:
    out = []
    for _ in range(n):
        r = rng.choice((0.8, 1, 1.2, 1.6))
        out.append(f'<circle cx="{rng.random() * W:.0f}" cy="{rng.random() * H * top:.0f}" r="{r}" fill="{color}" opacity="{0.35 + rng.random() * 0.55:.2f}"/>')
    return "".join(out)


def scene(name: str, sky: list[str], acc: list[str], kind: str) -> str:
    rng = random.Random(name)
    layers: list[str] = []
    glow = lambda cx, cy, r, c, o: f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{c}" opacity="{o}" filter="url(#blur)"/>'  # noqa: E731
    if kind == "city":
        layers += [glow(1180, 260, 260, acc[1], 0.35), glow(300, 180, 320, acc[0], 0.3), stars(rng, 90, "#ffffff"),
                   f'<circle cx="1230" cy="210" r="70" fill="#c0caf5" opacity="0.9"/><circle cx="1258" cy="196" r="62" fill="{sky[1]}"/>']
        for depth, (color, hmin, hmax, op) in enumerate([(acc[1], 160, 380, 0.35), ("#16161e", 220, 520, 1)]):
            x = 0
            while x < W:
                bw = rng.randint(50, 130)
                bh = rng.randint(hmin, hmax)
                layers.append(f'<rect x="{x}" y="{H - bh}" width="{bw}" height="{bh}" fill="{color}" opacity="{op}"/>')
                if depth == 1:
                    for _ in range(bh // 40):
                        wx, wy = x + rng.randint(8, max(9, bw - 14)), H - bh + rng.randint(14, max(15, bh - 20))
                        layers.append(f'<rect x="{wx}" y="{wy}" width="6" height="9" fill="{rng.choice(acc)}" opacity="{0.4 + rng.random() * 0.5:.2f}"/>')
                x += bw + rng.randint(0, 14)
    elif kind == "peaks":
        layers += [stars(rng, 70, acc[4], 0.45)]
        for i in range(4):
            layers.append(f'<path d="M0,{220 + i * 50} C400,{120 + i * 60} 900,{330 + i * 40} 1600,{180 + i * 45}" stroke="{acc[i % 4]}" stroke-width="{70 - i * 12}" fill="none" opacity="0.22" filter="url(#blur)"/>')
        for i, (base, color) in enumerate([(560, acc[1]), (660, "#3b4252"), (780, "#2e3440")]):
            d = ridge(rng, base, 260 - i * 50, steps=9 + i * 3, jag=120)
            layers.append(f'<path d="{d}" fill="{color}" opacity="{0.55 + i * 0.2:.2f}"/>')
        layers.append(f'<path d="{ridge(rng, 600, 230, 9, 120)}" fill="{acc[4]}" opacity="0.12"/>')
    elif kind == "forest":
        layers += [glow(1250, 300, 220, acc[2], 0.35), stars(rng, 40, acc[0], 0.4)]
        for i, (base, color, op) in enumerate([(540, acc[1], 0.35), (650, acc[0], 0.35), (760, "#232a2e", 0.95)]):
            layers.append(f'<path d="{ridge(rng, base, 140, 12)}" fill="{color}" opacity="{op}"/>')
            for _ in range(40 + i * 25):
                tx, th = rng.random() * W, rng.randint(40, 90) + i * 25
                ty = base + rng.randint(-30, 60)
                layers.append(f'<path d="M{tx:.0f},{ty - th} L{tx - th * 0.28:.0f},{ty} L{tx + th * 0.28:.0f},{ty} Z" fill="{color}" opacity="{op}"/>')
    elif kind == "dunes":
        layers += [f'<circle cx="800" cy="560" r="230" fill="url(#sun)"/>', glow(800, 560, 380, acc[0], 0.35)]
        for i, (base, color, op) in enumerate([(640, acc[3], 0.35), (720, acc[2], 0.45), (800, acc[0], 0.5), (880, "#1d2021", 0.9)]):
            layers.append(f'<path d="{ridge(rng, base, 120 - i * 15, 6)}" fill="{color}" opacity="{op}"/>')
    elif kind == "waves":
        layers += [f'<circle cx="1260" cy="230" r="110" fill="{acc[3]}" opacity="0.8"/>', glow(1260, 230, 260, acc[3], 0.25), stars(rng, 50, acc[2], 0.4)]
        for i in range(9):
            y = 470 + i * 62
            amp = 70 - i * 4
            d = f"M0,{y} " + " ".join(f"Q{x + 100},{y - amp * (1 if (x // 200) % 2 == 0 else -0.4):.0f} {x + 200},{y}" for x in range(0, W, 200)) + f" L{W},{H} L0,{H} Z"
            layers.append(f'<path d="{d}" fill="{acc[i % 2]}" opacity="{0.12 + i * 0.07:.2f}"/>')
            layers.append(f'<path d="{d.split(" L")[0]}" stroke="{acc[2]}" stroke-width="2" fill="none" opacity="0.35"/>')
    elif kind == "hills":
        layers += [glow(300, 250, 300, acc[0], 0.4), glow(1300, 350, 280, acc[1], 0.35), glow(800, 120, 220, acc[2], 0.25)]
        if sky[0] < "#8":
            layers.append(stars(rng, 60, "#ffffff", 0.45))
        for i, (base, color, op) in enumerate([(600, acc[2], 0.3), (690, acc[0], 0.35), (790, acc[1], 0.4), (880, sky[2], 0.85)]):
            layers.append(f'<path d="{ridge(rng, base, 150, 7)}" fill="{color}" opacity="{op}"/>')
    elif kind == "petals":
        layers += [glow(400, 300, 320, acc[1], 0.35), glow(1200, 650, 320, acc[2], 0.3), stars(rng, 50, acc[0], 0.6)]
        for _ in range(26):
            cx, cy, r = rng.random() * W, rng.random() * H, rng.randint(30, 120)
            rot = rng.randint(0, 180)
            layers.append(f'<ellipse cx="{cx:.0f}" cy="{cy:.0f}" rx="{r}" ry="{r * 0.45:.0f}" transform="rotate({rot} {cx:.0f} {cy:.0f})" fill="{rng.choice(acc)}" opacity="{0.08 + rng.random() * 0.22:.2f}"/>')
    elif kind == "arcs":
        layers.append(glow(1180, 820, 420, acc[0], 0.22))
        for i in range(18):
            r = 90 + i * 62
            accent = i in (4, 9)
            layers.append(f'<circle cx="1180" cy="900" r="{r}" stroke="{acc[0] if accent else acc[3]}" stroke-width="{2.5 if accent else 1.2}" fill="none" opacity="{0.75 if accent else 0.16}"/>')
        layers.append(f'<circle cx="1180" cy="900" r="60" fill="{acc[0]}" opacity="0.85"/>')
    elif kind == "ink":
        for i, (base, color, op) in enumerate([(520, acc[0], 0.10), (600, acc[1], 0.14), (690, acc[2], 0.16), (790, acc[2], 0.24)]):
            layers.append(f'<path d="{ridge(rng, base, 240 - i * 40, 10, 90)}" fill="{color}" opacity="{op}" filter="url(#soft)"/>')
        layers.append(f'<circle cx="1250" cy="230" r="90" fill="{acc[3]}" opacity="0.75"/>')
    defs = (
        '<defs>'
        f'<linearGradient id="sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{sky[0]}"/><stop offset=".6" stop-color="{sky[1]}"/><stop offset="1" stop-color="{sky[2]}"/></linearGradient>'
        f'<radialGradient id="sun"><stop offset="0" stop-color="{acc[1]}"/><stop offset=".55" stop-color="{acc[0]}"/><stop offset="1" stop-color="{acc[2]}" stop-opacity="0"/></radialGradient>'
        '<filter id="blur" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="60"/></filter>'
        '<filter id="soft"><feGaussianBlur stdDeviation="3"/></filter>'
        '</defs>'
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" preserveAspectRatio="xMidYMid slice">'
        f'{defs}<rect width="{W}" height="{H}" fill="url(#sky)"/>{"".join(layers)}</svg>\n'
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, (sky, acc, kind) in PALETTES.items():
        svg = scene(name, sky, acc, kind)
        (OUT / f"{name}.svg").write_text(svg, encoding="utf-8")
        print(f"{name}: {len(svg) / 1024:.1f} KB ({kind})")


if __name__ == "__main__":
    main()
