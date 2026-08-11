#!/usr/bin/env python3
"""Generate the CALIBER LinkedIn image set.

Everything is authored as SVG and rasterised at 2x by ``rsvg-convert``, so type
stays vector-crisp at any size and there is no font rasterisation to fight. The
first half of this file is the drawing kit -- one palette, one shadow, one type
scale, one icon weight -- which is what makes the eight images read as a single
system rather than eight separate slides.

    python3 make_images.py images

Requires ``rsvg-convert`` (``brew install librsvg``). No Python packages.
"""

from __future__ import annotations

import html
import subprocess
import sys
from pathlib import Path


# ----------------------------------------------------------------- palette --
INK = "#0A1628"        # deepest navy — headers, display type
INK_2 = "#16273F"      # gradient partner
SLATE = "#475569"      # body copy
MUTED = "#94A3B8"      # secondary copy
FAINT = "#CBD5E1"      # hairlines on dark

TEAL = "#14B8A6"       # primary accent
TEAL_D = "#0D9488"
TEAL_L = "#5EEAD4"
TEAL_BG = "#EFFCF9"

AMBER = "#F59E0B"      # secondary accent — the human decision, the caveat
AMBER_D = "#B45309"
AMBER_BG = "#FFFBEB"

VIOLET = "#8B5CF6"
SKY = "#38BDF8"
EMERALD = "#10B981"
EMERALD_BG = "#ECFDF5"
ROSE = "#F43F5E"
ROSE_BG = "#FFF1F2"

PAPER = "#FFFFFF"
PAGE = "#F8FAFC"
BORDER = "#E2E8F0"

FONT = "Helvetica Neue, Helvetica, Arial, sans-serif"

W, H = 1600, 900
SCALE = 2

# Average glyph width as a fraction of font-size, by weight. Used to fit copy to
# a pixel width instead of guessing a character count — the old generator wrapped
# on character counts and overflowed every narrow column.
_ADVANCE = {"300": 0.495, "400": 0.505, "500": 0.520, "600": 0.535, "700": 0.550, "800": 0.565}


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def text_width(s: str, size: float, weight: str = "400") -> float:
    return len(s) * size * _ADVANCE.get(weight, 0.505)


def fit(s: str, px: float, size: float, weight: str = "400") -> list[str]:
    """Wrap ``s`` to lines no wider than ``px``."""
    words, lines, cur = s.split(), [], ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if text_width(trial, size, weight) <= px or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


# -------------------------------------------------------------- primitives --
def txt(x, y, s, size=17, fill=SLATE, weight="400", anchor="start", ls=0, opacity=None):
    op = f' opacity="{opacity}"' if opacity is not None else ""
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{size}" '
        f'fill="{fill}" font-weight="{weight}" text-anchor="{anchor}" '
        f'letter-spacing="{ls}"{op}>{esc(s)}</text>'
    )


def para(x, y, s, px, size=17, fill=SLATE, lh=None, weight="400", anchor="start"):
    lh = lh or size * 1.45
    return "".join(
        txt(x, y + i * lh, line, size=size, fill=fill, weight=weight, anchor=anchor)
        for i, line in enumerate(fit(s, px, size, weight))
    )


def para_height(s, px, size=17, lh=None, weight="400") -> float:
    lh = lh or size * 1.45
    return len(fit(s, px, size, weight)) * lh


def rect(x, y, w, h, fill, r=16, stroke=None, sw=1, shadow=False, opacity=None):
    st = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
    fl = ' filter="url(#soft)"' if shadow else ""
    op = f' opacity="{opacity}"' if opacity is not None else ""
    return (
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{r}" '
        f'fill="{fill}"{st}{fl}{op}/>'
    )


def line(x1, y1, x2, y2, stroke=BORDER, sw=1, dash=None, cap="round"):
    da = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
        f'stroke="{stroke}" stroke-width="{sw}" stroke-linecap="{cap}"{da}/>'
    )


def circle(cx, cy, r, fill, stroke=None, sw=2, shadow=False, opacity=None):
    st = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
    fl = ' filter="url(#soft)"' if shadow else ""
    op = f' opacity="{opacity}"' if opacity is not None else ""
    return f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="{fill}"{st}{fl}{op}/>'


def arrow(x1, y, x2, color=TEAL, sw=3):
    """Horizontal arrow with a matched head."""
    head = 5 + sw * 2
    return (
        line(x1, y, x2 - head, y, stroke=color, sw=sw)
        + f'<path d="M {x2:.1f} {y:.1f} L {x2 - head:.1f} {y - head * 0.62:.1f} '
        f'L {x2 - head:.1f} {y + head * 0.62:.1f} Z" fill="{color}"/>'
    )


def arrow_down(x, y1, y2, color=TEAL, sw=3):
    head = 5 + sw * 2
    return (
        line(x, y1, x, y2 - head, stroke=color, sw=sw)
        + f'<path d="M {x:.1f} {y2:.1f} L {x - head * 0.62:.1f} {y2 - head:.1f} '
        f'L {x + head * 0.62:.1f} {y2 - head:.1f} Z" fill="{color}"/>'
    )


def pill(x, y, label, fill, text_fill, size=14, pad=14, h=28, weight="700", ls=0.4):
    w = text_width(label, size, weight) + pad * 2
    return (
        rect(x, y, w, h, fill, r=h / 2)
        + txt(x + w / 2, y + h / 2 + size * 0.36, label, size=size, fill=text_fill,
              weight=weight, anchor="middle", ls=ls)
    ), w


def eyebrow(x, y, label, fill=TEAL_D, size=13):
    """Small letterspaced caps — the label above a section."""
    return txt(x, y, label.upper(), size=size, fill=fill, weight="700", ls=1.6)


def badge(cx, cy, n, fill=TEAL, text_fill="#FFFFFF", r=19, size=17):
    return circle(cx, cy, r, fill) + txt(
        cx, cy + size * 0.36, str(n), size=size, fill=text_fill, weight="700", anchor="middle"
    )


# ------------------------------------------------------------------- icons --
# 24x24 stroked line icons. One weight, round joins — the same hand throughout.
_ICONS = {
    "doc": "M7 3h7l4 4v14H7z M14 3v4h4",
    "layers": "M12 3l8 4.5-8 4.5-8-4.5z M4 12l8 4.5 8-4.5 M4 16.5l8 4.5 8-4.5",
    "shield": "M12 3l7 3v6c0 4-3 7-7 9-4-2-7-5-7-9V6z M12 10.5v3 M12 16.6v.2",
    "ledger": "M5 4h11l3 3v13H5z M8 9h8 M8 13h8 M8 17h5",
    "check": "M12 3a9 9 0 100 18 9 9 0 000-18z M8 12.2l2.8 2.8L16 9.6",
    "cross": "M12 3a9 9 0 100 18 9 9 0 000-18z M9 9l6 6 M15 9l-6 6",
    "gate": "M4 20V9l8-5 8 5v11 M9 20v-6h6v6 M4 20h16",
    "person": "M12 4a3.6 3.6 0 100 7.2A3.6 3.6 0 0012 4z M5 20.5c0-3.6 3.1-6 7-6s7 2.4 7 6",
    "rotate": "M20 12a8 8 0 11-2.5-5.8 M20 3v4h-4",
    "pointer": "M12 3v18 M12 7h7l-2.4 3L19 13h-7",
    "flag": "M6 21V4h11l-2 3.5L17 11H6",
    "beaker": "M9 3h6 M10 3v6.5L5.5 18a2 2 0 001.8 3h9.4a2 2 0 001.8-3L14 9.5V3 M7.8 14h8.4",
    "scale": "M12 4v16 M6 20h12 M4 9h16 M4 9l-2.5 5h5z M20 9l2.5 5h-5z",
    "anchor": "M12 5.5a2 2 0 100 4 2 2 0 000-4z M12 9.5V21 M5 14a7 7 0 0014 0 M5 14h3 M19 14h-3",
    "clock": "M12 3a9 9 0 100 18 9 9 0 000-18z M12 7v5.4l3.4 2",
    "lock": "M6 11h12v9H6z M9 11V7.6a3 3 0 016 0V11",
    "search": "M11 4a7 7 0 100 14 7 7 0 000-14z M20 20l-4.2-4.2",
    "spark": "M12 3l2.2 5.6L20 11l-5.8 2.4L12 19l-2.2-5.6L4 11l5.8-2.4z",
}


def icon(name, x, y, size=24, color=TEAL, sw=1.8):
    d = _ICONS.get(name)
    if d is None:
        return ""
    s = size / 24
    return (
        f'<g transform="translate({x:.1f},{y:.1f}) scale({s:.4f})" fill="none" '
        f'stroke="{color}" stroke-width="{sw / s:.2f}" stroke-linecap="round" '
        f'stroke-linejoin="round"><path d="{d}"/></g>'
    )


def icon_tile(x, y, name, color, bg, size=52, glyph=26):
    """An icon in a soft tinted rounded tile — the card's visual anchor."""
    return rect(x, y, size, size, bg, r=size * 0.3) + icon(
        name, x + (size - glyph) / 2, y + (size - glyph) / 2, glyph, color, sw=1.9
    )


# ------------------------------------------------------------------ chrome --
DEFS = f"""<defs>
  <filter id="soft" x="-40%" y="-40%" width="180%" height="180%">
    <feDropShadow dx="0" dy="4" stdDeviation="8" flood-color="#0A1628" flood-opacity="0.09"/>
  </filter>
  <filter id="lift" x="-40%" y="-40%" width="180%" height="180%">
    <feDropShadow dx="0" dy="10" stdDeviation="18" flood-color="#0A1628" flood-opacity="0.18"/>
  </filter>
  <linearGradient id="band" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="{INK}"/><stop offset="1" stop-color="{INK_2}"/>
  </linearGradient>
  <linearGradient id="tealgrad" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="{TEAL}"/><stop offset="1" stop-color="{TEAL_D}"/>
  </linearGradient>
  <linearGradient id="ambergrad" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="{AMBER}"/><stop offset="1" stop-color="#D97706"/>
  </linearGradient>
  <linearGradient id="hero" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#071120"/><stop offset="0.55" stop-color="{INK}"/>
    <stop offset="1" stop-color="#123049"/>
  </linearGradient>
  <radialGradient id="glowteal"><stop offset="0" stop-color="{TEAL}" stop-opacity="0.55"/>
    <stop offset="1" stop-color="{TEAL}" stop-opacity="0"/></radialGradient>
  <radialGradient id="glowsky"><stop offset="0" stop-color="{SKY}" stop-opacity="0.34"/>
    <stop offset="1" stop-color="{SKY}" stop-opacity="0"/></radialGradient>
  <radialGradient id="glowviolet"><stop offset="0" stop-color="{VIOLET}" stop-opacity="0.30"/>
    <stop offset="1" stop-color="{VIOLET}" stop-opacity="0"/></radialGradient>
  <pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse">
    <circle cx="1.6" cy="1.6" r="1.6" fill="{INK}" opacity="0.05"/>
  </pattern>
</defs>"""


def header(title, sub=None, accent=TEAL, height=None):
    """The dark title band every in-article figure shares."""
    h = height or (176 if sub else 140)
    out = [
        f'<rect x="0" y="0" width="{W}" height="{h}" fill="url(#band)"/>',
        f'<circle cx="{W - 120}" cy="{h * 0.2:.0f}" r="200" fill="url(#glowteal)" opacity="0.5"/>',
        f'<rect x="0" y="{h - 4}" width="{W}" height="4" fill="{accent}"/>',
    ]
    # A small stacked mark echoing the six layers. It sits clear of the title's
    # cap height -- at 40pt that is baseline-29, so the mark has to end above it.
    for i in range(4):
        out.append(rect(64, 26 + i * 8, 28 - i * 2, 4.5, accent, r=2.2, opacity=0.95 - i * 0.16))
    out.append(txt(64, 102 if sub else 96, title, size=40, fill="#FFFFFF", weight="700", ls=-0.4))
    if sub:
        out.append(txt(64, 140, sub, size=20, fill="#9DB2C8", weight="400"))
    return "".join(out), h


def page(body, w=W, h=H, bg=PAGE, dotted=True):
    grid = f'<rect width="{w}" height="{h}" fill="url(#dots)"/>' if dotted else ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}">{DEFS}<rect width="{w}" height="{h}" fill="{bg}"/>'
        f"{grid}{body}</svg>"
    )


def render(out_dir: Path, name: str, svg: str, w=W, h=H):
    svg_path = out_dir / f"{name}.svg"
    svg_path.write_text(svg, encoding="utf-8")
    png = out_dir / f"{name}.png"
    subprocess.run(
        ["rsvg-convert", "-w", str(int(w * SCALE)), "-h", str(int(h * SCALE)),
         "-o", str(png), str(svg_path)],
        check=True,
    )
    svg_path.unlink()
    print(f"  {png.name:<24} {int(w * SCALE)}x{int(h * SCALE)}")



OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("images")
OUT.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------ hero banner --
def hero_banner():
    b = [f'<rect width="{W}" height="{H}" fill="url(#hero)"/>']
    b += [
        f'<circle cx="1330" cy="180" r="330" fill="url(#glowteal)"/>',
        f'<circle cx="1500" cy="720" r="300" fill="url(#glowsky)"/>',
        f'<circle cx="180" cy="820" r="260" fill="url(#glowviolet)"/>',
    ]

    # The six layers as a foundation-up stack: infrastructure widest at the base,
    # surfaces narrowest at the top. Read bottom-up, exactly as the paper's figure 1.
    layers = [
        ("L6", "surfaces"),
        ("L5", "lifecycle modes"),
        ("L4", "asset families"),
        ("L3", "governance"),
        ("L2", "kernel"),
        ("L1", "infrastructure"),
    ]
    mid, top0, rowh = 1300, 214, 68
    b.append(txt(mid, top0 - 34, "SIX LAYERS, READ BOTTOM-UP", size=13, fill="#7FA6BE",
                 weight="700", ls=1.8, anchor="middle"))
    for i, (tag, label) in enumerate(layers):
        y = top0 + i * rowh
        wdt = 300 + i * 32
        op = 0.13 + i * 0.052
        b.append(rect(mid - wdt / 2, y, wdt, 54, TEAL_L, r=12, opacity=op))
        b.append(rect(mid - wdt / 2, y, 4, 54, TEAL_L, r=2, opacity=0.35 + i * 0.11))
        b.append(txt(mid - wdt / 2 + 22, y + 34, tag, size=16, fill="#FFFFFF", weight="700",
                     opacity=0.5 + i * 0.09))
        b.append(txt(mid - wdt / 2 + 62, y + 34, label, size=17, fill="#CFEFEA",
                     opacity=0.5 + i * 0.09))

    # Wordmark
    for i in range(4):
        b.append(rect(64, 150 + i * 17, 132 - i * 12, 8, TEAL, r=4, opacity=1 - i * 0.2))
    b.append(txt(60, 348, "CALIBER", size=104, fill="#FFFFFF", weight="700", ls=-2))
    b.append(rect(64, 388, 92, 5, TEAL, r=3))
    b.append(txt(64, 452, "AI agent governance, workflow orchestration,", size=29, fill="#C9DCEB"))
    b.append(txt(64, 492, "and progressive autonomy", size=29, fill="#C9DCEB"))
    b.append(para(64, 546,
                  "Governing the prompts, workflows, tools, MCP servers and retrieval "
                  "corpora an agent depends on — not just one of them.",
                  px=780, size=19, fill="#8CA8C0", lh=28))

    stats = [
        ("9", "asset families", "one mode vocabulary"),
        ("7", "chain terms", "each leaves a record"),
        # "mechanisms", not "semantics": four families *declare* a rollback and no two
        # perform the same one. The matrix slide separately says five *semantics*,
        # counting the fifth family that shares the version-history panel and has no
        # rollback at all. Same fact, two counts — so the words have to differ.
        ("4", "rollback mechanisms", "and no two alike"),
    ]
    sy = 654
    b.append(line(64, sy, 968, sy, stroke="#26405A", sw=1))
    x = 64
    for i, (big, label, note) in enumerate(stats):
        if i:
            b.append(line(x - 34, sy + 26, x - 34, sy + 118, stroke="#26405A", sw=1))
        b.append(txt(x, sy + 84, big, size=54, fill="#FFFFFF", weight="700"))
        b.append(txt(x + text_width(big, 54, "700") + 14, sy + 84, label, size=21, fill=TEAL_L,
                     weight="600"))
        b.append(txt(x, sy + 116, note, size=16, fill="#7E97AE"))
        x += 292
    b.append(line(64, sy + 148, 968, sy + 148, stroke="#26405A", sw=1))

    b.append(txt(64, 838, "Based on the paper by Reza Rahimi  ·  Jazzx AI", size=17,
                 fill="#61809A", ls=0.3))
    return page("".join(b), dotted=False)


# ------------------------------------------------------ the governance chain --
def governance_chain():
    hdr, band = header(
        "The governance chain",
        "Seven concepts, not seven stages — and every one of them leaves a record behind.",
    )
    b = [hdr]

    terms = [
        ("Signal", "a failure worth acting on", "verification item", "flag"),
        ("Evidence", "traces become a corpus", "assembled corpus", "beaker"),
        ("Candidate", "a proposed better version", "candidate artifact", "spark"),
        ("Measurement", "scored against that corpus", "scores + gate verdict", "scale"),
        ("Decision", "an operator applies, or not", "apply action + anchor", "person"),
        ("Release", "the live target moves", "checkpoint + audit row", "rotate"),
        ("Trace", "the next signal arrives", "new production traces", "search"),
    ]

    pad = 56
    n = len(terms)
    span = W - 2 * pad
    step = span / n
    rail_y = band + 156
    cx0 = pad + step / 2

    # the rail, continuous: the seventh term is the input to the next iteration
    b.append(line(cx0, rail_y, cx0 + step * (n - 1), rail_y, stroke="#B9E7E1", sw=3))

    for i, (name, sub, residue, ico) in enumerate(terms):
        cx = cx0 + i * step
        human = i == 4
        col = AMBER if human else TEAL
        # node
        b.append(circle(cx, rail_y, 30, PAPER, stroke=col, sw=3, shadow=True))
        b.append(icon(ico, cx - 12, rail_y - 12, 24, col, sw=1.9))
        b.append(circle(cx + 22, rail_y - 22, 12, col))
        b.append(txt(cx + 22, rail_y - 17.5, str(i + 1), size=12, fill="#FFFFFF",
                     weight="700", anchor="middle"))
        # label above
        b.append(txt(cx, rail_y - 62, name, size=20, fill=INK, weight="700", anchor="middle"))
        # copy below
        b.append(para(cx, rail_y + 62, sub, px=step - 22, size=15, fill=SLATE, lh=21,
                      anchor="middle"))
        # tick down into the residue band
        b.append(line(cx, rail_y + 128, cx, rail_y + 168, stroke="#CBE9E5", sw=2, dash="2 6"))

    # human-decision marker
    cxh = cx0 + 4 * step
    marker, mw = pill(0, 0, "HUMAN DECISION", AMBER_BG, AMBER_D, size=12, pad=13, h=26)
    b.append(f'<g transform="translate({cxh - mw / 2:.1f},{rail_y - 126:.1f})">{marker}</g>')

    # residue band
    ry = rail_y + 168
    b.append(rect(pad, ry, span, 150, TEAL_BG, r=20, stroke="#BFE9E2"))
    b.append(eyebrow(pad + 30, ry + 38,
                     "Durable residue — what each term deposits, so a release is reconstructable",
                     TEAL_D))
    for i, (_, _, residue, _) in enumerate(terms):
        cx = cx0 + i * step
        b.append(circle(cx, ry + 66, 3.5, TEAL))
        b.append(para(cx, ry + 96, residue, px=step - 24, size=15, fill="#0E5C56",
                      lh=21, weight="600", anchor="middle"))

    qy = ry + 178
    b.append(rect(pad, qy, span, 126, INK, r=20))
    b.append(rect(pad, qy, 6, 126, TEAL, r=3))
    b.append(txt(pad + 44, qy + 54,
                 "“Why is the prompt like this, and what did we know when we changed it?”",
                 size=25, fill="#FFFFFF", weight="700"))
    b.append(txt(pad + 44, qy + 90,
                 "Answerable from records rather than from recollection — the property CALIBER exists to provide.",
                 size=17.5, fill="#8FAAC2"))
    return page("".join(b))


# --------------------------------------------------------- the governed asset --
def governed_asset():
    hdr, band = header(
        "The governed asset",
        "Four facets hold for every family. Eight do not — and that asymmetry is the architecture.",
    )
    b = [hdr]
    pad, gap = 56, 20
    cw = (W - 2 * pad - 3 * gap) / 4
    top = band + 74

    b.append(eyebrow(pad, top - 20, "The shared base contract  ·  true of all nine families", TEAL_D))
    universal = [
        ("doc", TEAL, TEAL_BG, "Typed definition",
         "A schema-validated specification that is the source of truth for the asset, not a rendering of it."),
        ("layers", SKY, "#EFF9FE", "Version history",
         "Immutable snapshots, or immutable registry versions held externally."),
        ("shield", VIOLET, "#F5F1FE", "Authority model",
         "Which of the four RBAC scopes may read, mutate and release it."),
        ("ledger", EMERALD, EMERALD_BG, "Trace and audit trail",
         "The durable record of what was done to it, and by whom."),
    ]
    ch = 264
    for i, (ico, col, bg, title, desc) in enumerate(universal):
        x = pad + i * (cw + gap)
        b.append(rect(x, top, cw, ch, PAPER, r=20, stroke=BORDER, shadow=True))
        b.append(rect(x, top, cw, 5, col, r=2.5))
        b.append(icon_tile(x + 28, top + 34, ico, col, bg))
        b.append(para(x + 28, top + 128, title, px=cw - 56, size=22, fill=INK, weight="700", lh=28))
        th = para_height(title, cw - 56, 22, 28, "700")
        b.append(para(x + 28, top + 128 + th + 16, desc, px=cw - 56, size=16, fill=SLATE, lh=24))

    fy = top + ch + 40
    fh = 176
    b.append(rect(pad, fy, W - 2 * pad, fh, AMBER_BG, r=20, stroke="#F6DFB0"))
    b.append(eyebrow(pad + 30, fy + 38,
                     "Family-specific  ·  available to every family, guaranteed by none", AMBER_D))
    chips = ["live target", "test surface", "evidence base", "evaluation",
             "gate semantics", "calibration", "release path", "packaging"]
    cx = pad + 30
    cy = fy + 62
    for i, chip in enumerate(chips):
        if i == 4:
            cx = pad + 30
            cy += 50
        markup, cwid = pill(cx, cy, chip, PAPER, AMBER_D, size=17, pad=20, h=40, weight="600", ls=0)
        b.append(markup.replace(f'fill="{PAPER}"',
                                f'fill="{PAPER}" stroke="#EFD5A4" stroke-width="1"', 1))
        cx += cwid + 12

    ny = fy + fh + 34
    b.append(rect(pad, ny, 6, 62, TEAL, r=3))
    b.append(txt(pad + 26, ny + 26,
                 "Adjacency in a layered architecture confers capability availability, not capability inheritance.",
                 size=21, fill=INK, weight="700"))
    b.append(txt(pad + 26, ny + 54,
                 "A family placed in the asset layer obtains lifecycle behaviour only by explicitly wiring it.",
                 size=18, fill=SLATE))
    return page("".join(b))


# --------------------------------------------------------------- gate flow --
def gate_flow():
    hdr, band = header(
        "Where the enforced gate sits",
        "Enforce on candidate advancement, keep the release verdict advisory — and the gate keeps carrying information.",
        accent=AMBER,
    )
    b = [hdr]
    pad = 56

    lane = [
        (56, 380, "flag", TEAL, "Flagged trace",
         "A production failure a human confirmed is real."),
        (496, 380, "spark", TEAL, "Candidate",
         "Diagnosis, evidence and an optimizer produce a proposed version."),
        (936, 380, "gate", AMBER, "Enforced gate",
         "The candidate-advancement gate. Unbypassable inside the refinement state machine."),
    ]
    r1y, r1h = band + 54, 172
    for x, cwid, ico, col, title, desc in lane:
        gate = col is AMBER
        b.append(rect(x, r1y, cwid, r1h, PAPER, r=20, stroke=BORDER, shadow=True))
        b.append(rect(x, r1y, cwid, 5, col, r=2.5))
        b.append(icon_tile(x + 26, r1y + 26, ico, col, AMBER_BG if gate else TEAL_BG, size=44,
                           glyph=22))
        b.append(txt(x + 84, r1y + 56, title, size=21, fill=INK, weight="700"))
        b.append(para(x + 26, r1y + 106, desc, px=cwid - 52, size=16, fill=SLATE, lh=23))
    b.append(arrow(452, r1y + r1h / 2, 486, color=TEAL, sw=3))
    b.append(arrow(892, r1y + r1h / 2, 926, color=TEAL, sw=3))

    gate_cx = 936 + 190
    rail_y = r1y + r1h + 54
    fail_x, fail_w = 150, 560
    pass_x, pass_w = 800, 744
    fail_cx, pass_cx = fail_x + fail_w / 2, pass_x + pass_w / 2
    cy = rail_y + 52

    b.append(line(gate_cx, r1y + r1h, gate_cx, rail_y, stroke="#94A3B8", sw=2.5))
    b.append(line(fail_cx, rail_y, max(pass_cx, gate_cx), rail_y, stroke="#94A3B8", sw=2.5))
    b.append(arrow_down(fail_cx, rail_y, cy, color=ROSE, sw=2.5))
    b.append(arrow_down(pass_cx, rail_y, cy, color=EMERALD, sw=2.5))

    ch = 236
    # refusal branch
    b.append(rect(fail_x, cy, fail_w, ch, ROSE_BG, r=20, stroke="#FBC7CE"))
    b.append(icon("cross", fail_x + 28, cy + 28, 28, ROSE, sw=2))
    b.append(txt(fail_x + 70, cy + 50, "Fails — the job stops", size=22, fill="#9F1239",
                 weight="700"))
    b.append(para(fail_x + 28, cy + 100,
                  "Nothing downstream sees the candidate. No operator is ever asked to "
                  "adjudicate a candidate that failed its own evaluation.",
                  px=fail_w - 56, size=16.5, fill="#9F1239", lh=24))
    b.append(line(fail_x + 28, cy + 178, fail_x + fail_w - 28, cy + 178, stroke="#F6BAC3", sw=1))
    b.append(para(fail_x + 28, cy + 208,
                  "Withholding is never what an operator needs to override.",
                  px=fail_w - 56, size=15, fill="#BE4359", lh=21))

    # pass branch
    b.append(rect(pass_x, cy, pass_w, ch, EMERALD_BG, r=20, stroke="#B7E6CD"))
    b.append(icon("check", pass_x + 28, cy + 28, 28, EMERALD, sw=2))
    b.append(txt(pass_x + 70, cy + 50, "Passes — the human decision", size=22, fill="#065F46",
                 weight="700"))
    steps = [
        (EMERALD, "Review queue", "only candidates that passed their own evaluation"),
        (VIOLET, "Human Apply", "one human decision, always — never an auto-apply threshold"),
        (TEAL, "Audited release", "the outgoing target is recorded before rotation"),
    ]
    for i, (col, title, desc) in enumerate(steps):
        yy = cy + 96 + i * 48
        b.append(badge(pass_x + 42, yy, i + 1, fill=col, r=15, size=14))
        b.append(txt(pass_x + 70, yy + 6, title, size=17, fill=INK, weight="700"))
        b.append(txt(pass_x + 70 + text_width(title, 17, "700") + 14, yy + 6, desc, size=16,
                     fill="#3F6B5C"))

    ny = cy + ch + 34
    b.append(rect(pad, ny, 6, 62, AMBER, r=3))
    b.append(txt(pad + 26, ny + 26,
                 "A gate that blocks release must be overridable. Once it is overridable, the override becomes the normal path.",
                 size=21, fill=INK, weight="700"))
    b.append(txt(pad + 26, ny + 54,
                 "So the unbypassable check sits earlier. The per-version release verdict stays advisory: it is filed as evidence and blocks nothing.",
                 size=17.5, fill=SLATE))
    return page("".join(b))


# ---------------------------------------------------------- families matrix --
def families_matrix():
    hdr, band = header(
        "Sharing a substrate does not share the guarantees",
        "The paper's most important table: nine families, and every row is allowed to differ.",
    )
    b = [hdr]

    cols = [("Family", 200, 22), ("History & liveness", 396, 44),
            ("Gate semantics", 296, 33), ("Release / rollback", 396, 44), ("Kind", 152, 0)]
    total = sum(c[1] for c in cols)
    pad = (W - total) / 2

    rows = [
        ("Prompt", "immutable registry versions behind an alias",
         "enforced advancement + advisory verdict", "records and restores the outgoing target",
         "runtime", TEAL),
        ("Workflow", "drafts promoted to published version rows",
         "enforced readiness + deploy gate", "rollback pops a checkpoint stack",
         "runtime", TEAL),
        ("Knowledge base", "immutable builds behind an active pointer", "none",
         "prior build derived from history", "runtime", TEAL),
        ("Skill", "mutable record plus immutable snapshots",
         "enforced advancement, no release gate", "restores a snapshot as a new version",
         "runtime", TEAL),
        ("Tool", "separate (name, version) rows", "none",
         "read-only history — no live alias at all", "runtime", TEAL),
        ("MCP server", "managed definitions, audited edits", "production preflight only",
         "no version rollback; fail-closed controls", "runtime", TEAL),
        ("Test set", "version counter + validity intervals", "n/a — it is the evidence",
         "no live alias or rollback", "evidence", AMBER),
        ("Judge", "operator-authored, referenced by token", "n/a — it is a scorer", "n/a",
         "scoring", AMBER),
        ("Agent", "the anchor items and jobs attach to", "n/a",
         "enabled is the pause/resume lever", "anchor", VIOLET),
    ]

    top, rh, hh = band + 36, 54, 50
    rel_x = pad + sum(c[1] for c in cols[:3])
    rel_w = cols[3][1]

    b.append(rect(pad, top, total, hh, INK, r=12))
    b.append(rect(pad, top + hh - 12, total, 12, INK, r=0))
    # The column the figure is about, marked in the header rather than washed over it.
    b.append(rect(rel_x, top, rel_w, hh, TEAL, r=0, opacity=0.22))
    x = pad
    for i, (label, cwid, _) in enumerate(cols):
        b.append(txt(x + 18, top + 32, label.upper(), size=13,
                     fill=TEAL_L if i == 3 else "#9DB2C8", weight="700", ls=1.2))
        x += cwid
    y = top + hh
    for i, (fam, hist, gate, rel, kind, col) in enumerate(rows):
        if i % 2 == 0:
            b.append(rect(pad, y, total, rh, "#FFFFFF", r=0))
        b.append(line(pad, y, pad + total, y, stroke="#EDF1F6", sw=1))
        b.append(rect(pad + 10, y + 13, 4, rh - 26, col, r=2))
        vals = [fam, hist, gate, rel]
        x = pad
        for j, (val, (_, cwid, wrapchars)) in enumerate(zip(vals, cols)):
            lines = fit(val, cwid - 34, 16 if j else 17, "700" if j == 0 else "400")
            y0 = y + (rh / 2 + 6) if len(lines) == 1 else y + 22
            for k, ln in enumerate(lines[:2]):
                b.append(txt(x + (24 if j == 0 else 18), y0 + k * 20, ln,
                             size=17 if j == 0 else 16,
                             fill=INK if j == 0 else SLATE,
                             weight="700" if j == 0 else "400"))
            x += cwid
        markup, kw = pill(x + 18, y + (rh - 26) / 2, kind, PAPER, col, size=13, pad=13, h=26)
        b.append(markup.replace(f'fill="{PAPER}"', f'fill="{PAPER}" stroke="{col}" stroke-width="1.2"', 1))
        y += rh
    b.append(line(pad, y, pad + total, y, stroke="#E2E8F0", sw=1))
    # Faint tint plus hairlines: emphasis that survives greyscale without hiding the copy.
    b.append(rect(rel_x, top + hh, rel_w, y - top - hh, TEAL, r=0, opacity=0.055))
    b.append(line(rel_x, top, rel_x, y, stroke=TEAL, sw=1.5))
    b.append(line(rel_x + rel_w, top, rel_x + rel_w, y, stroke=TEAL, sw=1.5))

    ny = y + 28
    b.append(rect(pad, ny, 6, 62, TEAL, r=3))
    b.append(txt(pad + 26, ny + 26,
                 "The same version-history panel is mounted for five families through per-artifact adapters.",
                 size=21, fill=INK, weight="700"))
    b.append(txt(pad + 26, ny + 54,
                 "It carries five different rollback semantics. Sharing the component does not share the meaning.",
                 size=17.5, fill=SLATE))
    return page("".join(b))


# ---------------------------------------------------------------- comparison --
def comparison():
    hdr, band = header("A prompt in a file vs. a governed asset")
    b = [hdr]
    pad = 68
    gap = 96
    cw = (W - 2 * pad - gap) / 2
    top = band + 76
    ch = 500

    left = [
        "History, but no live target",
        "Release is bound to a code deploy",
        "Evidence lives beside the change",
        "No record of who authorized it",
        "Undo is a revert plus a redeploy",
    ]
    right = [
        "A live target resolved at call time",
        "Remediation decoupled from deployment",
        "Scores and corpus bound to the version",
        "Scope-checked, audited authority",
        "Undo restores a recorded checkpoint",
    ]

    panels = [
        (pad, "Prompt in a source file", left, "#64748B", "#F1F5F9", "cross", "#94A3B8"),
        (pad + cw + gap, "Governed asset", right, TEAL_D, TEAL_BG, "check", TEAL),
    ]
    for x, title, items, headfill, bg, ico, icocol in panels:
        b.append(rect(x, top, cw, ch, PAPER, r=22, stroke=BORDER, shadow=True))
        b.append(rect(x, top, cw, 76, bg, r=22))
        b.append(rect(x, top + 54, cw, 22, bg, r=0))
        b.append(txt(x + 34, top + 48, title, size=25, fill=headfill, weight="700"))
        for i, item in enumerate(items):
            yy = top + 126 + i * 74
            b.append(icon(ico, x + 32, yy - 14, 26, icocol, sw=2))
            b.append(para(x + 74, yy + 5, item, px=cw - 108, size=19, fill=SLATE, lh=25))
            if i < len(items) - 1:
                b.append(line(x + 32, yy + 42, x + cw - 32, yy + 42, stroke="#F1F5F9", sw=1))

    vs_cx = pad + cw + gap / 2
    b.append(circle(vs_cx, top + ch / 2, 34, PAPER, stroke=BORDER, sw=1.5, shadow=True))
    b.append(txt(vs_cx, top + ch / 2 + 7, "vs", size=20, fill=MUTED, weight="700", anchor="middle"))

    ny = top + ch + 42
    b.append(rect(pad, ny, W - 2 * pad, 92, INK, r=20))
    b.append(rect(pad, ny, 6, 92, TEAL, r=3))
    b.append(txt(W / 2, ny + 40,
                 "Version control gives history. It does not give a pointer a running agent",
                 size=22, fill="#FFFFFF", weight="700", anchor="middle"))
    b.append(txt(W / 2, ny + 70, "can resolve without a redeployment.",
                 size=22, fill="#FFFFFF", weight="700", anchor="middle"))
    return page("".join(b))


# --------------------------------------------------------- evidence standing --
def evidence_standing():
    hdr, band = header(
        "What the paper establishes — and what it does not",
        "An empty cell is honest and can be filled. A plausible wrong number cannot be unfilled.",
    )
    b = [hdr]
    pad, gap = 56, 32
    cw = (W - 2 * pad - gap) / 2
    top = band + 60
    ch = 434

    est = [
        "A typed, per-family governance architecture with the capability surface stated in full",
        "A seven-term governance chain, distinguished from its six-stage prompt implementation",
        "A gate taxonomy: enforced on advancement, advisory on release",
        "Three stores, three owners, and the two dual-write boundaries named",
        "Eight deterministic claim checks, labelled structural evidence and nothing more",
    ]
    unest = [
        "The quantitative evaluation is specified but has not been run",
        "No user study — “more reviewable” is argued, not demonstrated",
        "Single-system evidence; it cannot show a different factoring is impossible",
        "Containment is not isolation, and the paper never conflates the two",
        "No adversarial evaluation of prompt injection or malicious tool authorship",
    ]

    for idx, (title, items, col, bg, bd, ico) in enumerate([
        ("Established", est, TEAL_D, TEAL_BG, "#BFE9E2", "check"),
        ("Explicitly not established", unest, AMBER_D, AMBER_BG, "#F6DFB0", "cross"),
    ]):
        x = pad + idx * (cw + gap)
        b.append(rect(x, top, cw, ch, PAPER, r=22, stroke=BORDER, shadow=True))
        b.append(rect(x, top, cw, 72, bg, r=22))
        b.append(rect(x, top + 50, cw, 22, bg, r=0))
        b.append(icon(ico, x + 30, top + 22, 28, col, sw=2.1))
        b.append(txt(x + 72, top + 46, title, size=24, fill=col, weight="700"))
        yy = top + 112
        for item in items:
            lines = fit(item, cw - 96, 16.5)
            b.append(circle(x + 42, yy - 5, 4, col))
            for k, ln in enumerate(lines):
                b.append(txt(x + 62, yy + k * 23, ln, size=16.5, fill=SLATE))
            yy += 23 * len(lines) + 22

    qy = top + ch + 42
    b.append(rect(pad, qy, W - 2 * pad, 118, INK, r=20))
    b.append(rect(pad, qy, 6, 118, AMBER, r=3))
    b.append(txt(W / 2, qy + 52,
                 "“A systems paper that decorates an unrun experiment with plausible numbers",
                 size=23, fill="#FFFFFF", weight="700", anchor="middle"))
    b.append(txt(W / 2, qy + 86, "is worse than one that reports an empty table.”",
                 size=23, fill="#FFFFFF", weight="700", anchor="middle"))
    return page("".join(b))


# --------------------------------------------------------------- one-pager --
IW, IH = 900, 2940


def infographic():
    M = 52
    CW = IW - 2 * M
    b = []

    # ---- masthead
    mh = 336
    b.append(f'<rect width="{IW}" height="{mh}" fill="url(#hero)"/>')
    b.append(f'<circle cx="{IW - 60}" cy="70" r="230" fill="url(#glowteal)"/>')
    b.append(f'<circle cx="{IW - 140}" cy="330" r="180" fill="url(#glowsky)"/>')
    for i in range(6):
        wdt = 92 + i * 16
        b.append(rect(IW - 96 - wdt, 96 + i * 27, wdt, 20, TEAL_L, r=5,
                      opacity=0.10 + i * 0.05))
    for i in range(4):
        b.append(rect(M, 44 + i * 12, 60 - i * 6, 5.5, TEAL, r=2.8, opacity=1 - i * 0.2))
    b.append(txt(M - 3, 152, "CALIBER", size=54, fill="#FFFFFF", weight="700", ls=-1.2))
    b.append(rect(M, 172, 56, 4, TEAL, r=2))
    b.append(txt(M, 214, "A Layered Control Plane for AI Agent Governance,", size=21, fill="#BFD4E6"))
    b.append(txt(M, 242, "Workflow Orchestration, and Progressive Autonomy", size=21, fill="#BFD4E6"))
    b.append(txt(M, 288, "Reza Rahimi", size=19, fill="#FFFFFF", weight="700"))
    b.append(txt(M, 312, "Jazzx AI, Los Altos, CA  ·  reza.rahimi@jazzx.ai", size=15, fill="#7E9AB4"))

    y = mh + 40

    # ---- lede + pull quote
    lede_w, quote_w = 462, CW - 462 - 26
    qx = M + lede_w + 26
    p1 = ("Production LLM-agent systems are thoroughly observable. Tracing tells you a "
          "prompt regressed. What the toolchain around it will not tell you is what to "
          "change, whether the change is better, who approved it, or how to undo it.")
    p2 = ("Prompt registries closed part of this — for prompts. Not for the workflows, "
          "tool definitions, MCP servers and retrieval corpora an agent equally depends on.")
    b.append(para(M, y + 16, p1, px=lede_w, size=16.5, fill=SLATE, lh=24))
    y2 = y + 16 + para_height(p1, lede_w, 16.5, 24) + 16
    b.append(para(M, y2, p2, px=lede_w, size=16.5, fill=SLATE, lh=24))
    lede_bottom = y2 + para_height(p2, lede_w, 16.5, 24)

    qh = 214
    b.append(rect(qx, y - 8, quote_w, qh, TEAL_BG, r=16, stroke="#BFE9E2"))
    b.append(rect(qx, y - 8, 5, qh, TEAL, r=2.5))
    b.append(txt(qx + 24, y + 44, "“", size=48, fill=TEAL, weight="700"))
    b.append(para(qx + 24, y + 66,
                  "Adjacency in a layered architecture confers capability availability, "
                  "not capability inheritance.",
                  px=quote_w - 48, size=16, fill="#0E5C56", lh=22, weight="700"))
    b.append(para(qx + 24, y + 158,
                  "The guarantees are declared per family — and they differ.",
                  px=quote_w - 48, size=14.5, fill=SLATE, lh=20))
    y = max(lede_bottom, y - 8 + qh) + 48

    def section(num, title, yy):
        b.append(badge(M + 15, yy - 6, num, fill=INK, r=15, size=15))
        b.append(txt(M + 42, yy, title, size=21, fill=INK, weight="700"))
        rule_x = M + 42 + text_width(title, 21, "700") + 18
        b.append(line(rule_x, yy - 7, M + CW, yy - 7, stroke=BORDER, sw=1.5))
        return yy + 34

    # ---- 1 the gap
    y = section(1, "The gap", y)
    steps = [
        ("Decide the failure is real", "a queue, if someone built one"),
        ("Assemble evidence beyond one case", "none standard — teams hand-build spreadsheets"),
        ("Release it so running agents pick it up", "a code deployment; the prompt is a string in a file"),
        ("Record what changed, and on what evidence", "a commit message, if anyone writes one"),
        ("Undo it to the exact prior state", "git revert — reverts the prompt, not the corpus"),
    ]
    ch = 52 + len(steps) * 54 + 12
    b.append(rect(M, y, CW, ch, PAPER, r=18, stroke=BORDER, shadow=True))
    b.append(eyebrow(M + 26, y + 34, "What a team must do  ·  and what tooling exists", "#94A3B8", 12))
    for i, (step, tool) in enumerate(steps):
        yy = y + 68 + i * 54
        b.append(circle(M + 34, yy + 4, 11, AMBER_BG))
        b.append(circle(M + 34, yy + 4, 4, AMBER))
        b.append(txt(M + 56, yy + 2, step, size=16, fill=INK, weight="600"))
        b.append(txt(M + 56, yy + 26, f"Tooling: {tool}", size=14.5, fill=MUTED))
    y += ch + 44

    # ---- 2 the abstraction
    y = section(2, "The abstraction: the governed asset", y)
    b.append(eyebrow(M, y + 6, "True of all nine families", TEAL_D, 12))
    y += 20
    facets = [
        ("doc", TEAL, TEAL_BG, "Typed definition", "schema-validated; the source of truth"),
        ("layers", SKY, "#EFF9FE", "Version history", "immutable snapshots or registry versions"),
        ("shield", VIOLET, "#F5F1FE", "Authority model", "which of four scopes may read, mutate, release"),
        ("ledger", EMERALD, EMERALD_BG, "Audit trail", "what was done to it, and by whom"),
    ]
    fw = (CW - 16) / 2
    fh = 100
    for i, (ico, col, bg, title, desc) in enumerate(facets):
        x = M + (i % 2) * (fw + 16)
        yy = y + (i // 2) * (fh + 14)
        b.append(rect(x, yy, fw, fh, PAPER, r=16, stroke=BORDER, shadow=True))
        b.append(rect(x, yy, 4, fh, col, r=2))
        b.append(icon_tile(x + 22, yy + 22, ico, col, bg, size=40, glyph=21))
        b.append(txt(x + 76, yy + 40, title, size=17, fill=INK, weight="700"))
        b.append(para(x + 76, yy + 64, desc, px=fw - 96, size=14, fill=SLATE, lh=19))
    y += 2 * fh + 14 + 20
    b.append(rect(M, y, CW, 68, AMBER_BG, r=16, stroke="#F6DFB0"))
    b.append(rect(M, y, 5, 68, AMBER, r=2.5))
    b.append(txt(M + 24, y + 28,
                 "Eight further facets — live target, evidence base, gate semantics, release path …",
                 size=14.5, fill=AMBER_D, weight="700"))
    b.append(txt(M + 24, y + 50,
                 "are available to every family and guaranteed by none. A family gets them by wiring them.",
                 size=14.5, fill=AMBER_D))
    y += 68 + 44

    # ---- 3 the chain
    y = section(3, "The governance chain", y)
    chain = ["Signal", "Evidence", "Candidate", "Measurement", "Decision", "Release", "Trace"]
    bw = (CW - 6 * 7) / 7
    for i, name in enumerate(chain):
        x = M + i * (bw + 7)
        col = AMBER if i == 4 else TEAL
        b.append(rect(x, y, bw, 44, col, r=10))
        b.append(txt(x + bw / 2, y + 27, name, size=12.5, fill="#FFFFFF", weight="700",
                     anchor="middle"))
        if i < 6:
            b.append(circle(x + bw + 3.5, y + 22, 1.6, "#CBD5E1"))
    y += 60
    b.append(para(M, y + 16,
                  "Seven concepts, not seven stages — the concrete prompt path has six. Each term "
                  "deposits durable state: a verification item, an assembled corpus, a candidate "
                  "artifact, scores with a gate decision, an apply action with a provenance anchor, "
                  "a rollback checkpoint with an audit row, and new traces.",
                  px=CW, size=15.5, fill=SLATE, lh=23))
    y += para_height(
        "Seven concepts, not seven stages — the concrete prompt path has six. Each term "
        "deposits durable state: a verification item, an assembled corpus, a candidate "
        "artifact, scores with a gate decision, an apply action with a provenance anchor, "
        "a rollback checkpoint with an audit row, and new traces.", CW, 15.5, 23) + 60

    # ---- 4 the gate
    y = section(4, "The distinction the system turns on", y)
    gates = [
        (AMBER, AMBER_BG, "#F6DFB0", "Enforced", "Candidate-advancement gate",
         "A failing gate stops the job. Nothing downstream sees the candidate, and no operator is "
         "asked to review it. Unbypassable — inside the refinement state machine, and only there."),
        ("#64748B", "#F1F5F9", BORDER, "Advisory", "Per-version release verdict",
         "Persisted as release evidence and surfaced in review. It does not block an alias rotation, "
         "by construction — so an urgent fix is never blocked by a stale artifact."),
    ]
    gw = (CW - 16) / 2
    gh = 196
    for i, (col, bg, bd, chip, title, desc) in enumerate(gates):
        x = M + i * (gw + 16)
        b.append(rect(x, y, gw, gh, PAPER, r=16, stroke=BORDER, shadow=True))
        b.append(rect(x, y, gw, 5, col, r=2.5))
        markup, _ = pill(x + 22, y + 24, chip.upper(), bg, col, size=11.5, pad=12, h=24)
        b.append(markup)
        b.append(txt(x + 22, y + 82, title, size=17, fill=INK, weight="700"))
        b.append(para(x + 22, y + 108, desc, px=gw - 44, size=14, fill=SLATE, lh=20))
    y += gh + 18
    b.append(rect(M, y, CW, 56, INK, r=14))
    b.append(rect(M, y, 5, 56, AMBER, r=2.5))
    b.append(txt(M + 24, y + 34,
                 "A gate that blocks release must be overridable — and then the override is the normal path.",
                 size=15, fill="#FFFFFF", weight="700"))
    y += 56 + 44

    # ---- 5 nine families
    y = section(5, "Nine families, and the guarantees differ", y)
    fams = [
        ("Prompt", "alias restore", TEAL), ("Workflow", "checkpoint pop", TEAL),
        ("Knowledge base", "derived from history", TEAL),
        ("Skill", "restore as new version", TEAL), ("Tool", "no rollback at all", TEAL),
        ("MCP server", "no version rollback", TEAL),
        ("Test set", "it is the evidence", AMBER), ("Judge", "it is a scorer", AMBER),
        ("Agent", "the anchor record", VIOLET),
    ]
    kw = (CW - 2 * 12) / 3
    kh = 62
    for i, (fam, mech, col) in enumerate(fams):
        x = M + (i % 3) * (kw + 12)
        yy = y + (i // 3) * (kh + 12)
        b.append(rect(x, yy, kw, kh, PAPER, r=14, stroke=BORDER, shadow=True))
        b.append(rect(x + 12, yy + 14, 4, kh - 28, col, r=2))
        b.append(txt(x + 26, yy + 28, fam, size=15, fill=INK, weight="700"))
        b.append(txt(x + 26, yy + 48, mech, size=13, fill=MUTED))
    y += 3 * kh + 2 * 12 + 22
    b.append(para(M, y + 16,
                  "The shared version-history panel is mounted for five of them through per-artifact "
                  "adapters. It carries five different rollback semantics. Sharing the component does "
                  "not share the meaning.",
                  px=CW, size=15.5, fill=SLATE, lh=23))
    y += 92

    # ---- 6 standing
    y = section(6, "The standing of the evidence", y)
    panels = [
        (TEAL_D, TEAL_BG, "#BFE9E2", "check", "Established",
         ["The architecture and its factoring",
          "The capability surface, per family",
          "Eight deterministic claim checks"]),
        (AMBER_D, AMBER_BG, "#F6DFB0", "cross", "Not established",
         ["The quantitative evaluation is unrun",
          "No user study; reviewability is argued",
          "No adversarial evaluation"]),
    ]
    pw = (CW - 16) / 2
    ph = 190
    for i, (col, bg, bd, ico, title, items) in enumerate(panels):
        x = M + i * (pw + 16)
        b.append(rect(x, y, pw, ph, PAPER, r=16, stroke=BORDER, shadow=True))
        b.append(rect(x, y, pw, 56, bg, r=16))
        b.append(rect(x, y + 36, pw, 20, bg, r=0))
        b.append(icon(ico, x + 20, y + 16, 24, col, sw=2))
        b.append(txt(x + 54, y + 36, title, size=17, fill=col, weight="700"))
        for j, item in enumerate(items):
            yy = y + 86 + j * 34
            b.append(circle(x + 27, yy - 5, 3.5, col))
            b.append(para(x + 40, yy, item, px=pw - 62, size=14, fill=SLATE, lh=19))
    y += ph + 40

    # ---- footer
    fh2 = IH - y
    b.append(f'<rect x="0" y="{y}" width="{IW}" height="{fh2}" fill="url(#band)"/>')
    b.append(rect(0, y, IW, 4, TEAL, r=0))
    b.append(txt(M, y + 62, "“", size=44, fill=TEAL, weight="700"))
    b.append(para(M + 30, y + 52,
                  "What separates a control plane from a dashboard with opinions is being "
                  "precise about where the guarantees stop.",
                  px=CW - 40, size=17, fill="#DCE8F2", lh=25, weight="700"))
    b.append(line(M, y + 118, M + CW, y + 118, stroke="#26405A", sw=1))
    b.append(txt(M, y + 148,
                 "CALIBER  ·  nine asset families  ·  six layers  ·  six lifecycle modes  ·  a seven-term chain",
                 size=13.5, fill="#7E9AB4", ls=0.2))
    return page("".join(b), w=IW, h=IH, dotted=False)


BUILDERS = {
    "hero_banner": (hero_banner, W, H),
    "hero_infographic": (infographic, IW, IH),
    "governance_chain": (governance_chain, W, H),
    "governed_asset": (governed_asset, W, H),
    "gate_flow": (gate_flow, W, H),
    "families_matrix": (families_matrix, W, H),
    "comparison": (comparison, W, H),
    "evidence_standing": (evidence_standing, W, H),
}


if __name__ == "__main__":
    print("generating:")
    only = sys.argv[2:] if len(sys.argv) > 2 else None
    for name, (fn, w, h) in BUILDERS.items():
        if only and name not in only:
            continue
        render(OUT, name, fn(), w, h)
