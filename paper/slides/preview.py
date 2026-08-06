#!/usr/bin/env python3
"""Render the generated deck to SVG (and PNG, if ``rsvg-convert`` is present).

The generator's fit checker proves that no text box asks for more height than it
was given. It cannot prove that two boxes do not sit on top of each other, and it
cannot show you what the deck looks like. This does both, by reading the finished
``.pptx`` back and re-drawing it -- so the proof sheet is made from the shipped
file rather than from the generator's intentions.

The rendering is deliberately approximate: it re-wraps text with the same width
model the generator uses. It is a proof sheet for layout, not a typesetter.

Usage::

    .venv/bin/python paper/slides/preview.py [--png]
"""

from __future__ import annotations

import html
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deck_kit import _chars_per_line

HERE = Path(__file__).resolve().parent
DECK = HERE / "caliber-layered-control-plane.pptx"
OUTDIR = HERE / "preview"

A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
EMU = 12700.0


def _fill(node) -> str | None:
    solid = node.find(f"{A}solidFill")
    if solid is None:
        return None
    clr = solid.find(f"{A}srgbClr")
    return f"#{clr.get('val')}" if clr is not None else None


def _wrap(words, per_line):
    lines, cur, used = [], [], 0
    for word, style in words:
        add = len(word) + (1 if used else 0)
        if used and used + add > per_line:
            lines.append(cur)
            cur, used = [(word, style)], len(word)
        else:
            cur.append((word, style))
            used += add
    if cur:
        lines.append(cur)
    return lines


def render(slide_xml: bytes, bg_default: str = "#FFFFFF") -> str:
    root = ET.fromstring(slide_xml)
    out = []
    bg = bg_default
    bgpr = root.find(f".//{P}bg")
    if bgpr is not None:
        clr = bgpr.find(f".//{A}srgbClr")
        if clr is not None:
            bg = f"#{clr.get('val')}"
    out.append(f'<rect width="960" height="540" fill="{bg}"/>')

    for sp in root.iter():
        if sp.tag not in (f"{P}sp",):
            continue
        xfrm = sp.find(f".//{A}xfrm")
        if xfrm is None:
            continue
        off, ext = xfrm.find(f"{A}off"), xfrm.find(f"{A}ext")
        x, y = int(off.get("x")) / EMU, int(off.get("y")) / EMU
        w, h = int(ext.get("cx")) / EMU, int(ext.get("cy")) / EMU

        spPr = sp.find(f"{P}spPr")
        geom = spPr.find(f"{A}prstGeom")
        prst = geom.get("prst") if geom is not None else None
        fill = _fill(spPr)
        ln = spPr.find(f"{A}ln")
        stroke = _fill(ln) if ln is not None else None

        if fill:
            if prst == "ellipse":
                out.append(
                    f'<ellipse cx="{x + w / 2:.1f}" cy="{y + h / 2:.1f}" '
                    f'rx="{w / 2:.1f}" ry="{h / 2:.1f}" fill="{fill}"/>'
                )
            else:
                rx = 9 if prst == "roundRect" else 0
                s = f' stroke="{stroke}" stroke-width="0.75"' if stroke else ""
                out.append(
                    f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" '
                    f'height="{h:.1f}" rx="{rx}" fill="{fill}"{s}/>'
                )

        tx = sp.find(f"{P}txBody")
        if tx is None:
            continue
        bodyPr = tx.find(f"{A}bodyPr")
        anchor = bodyPr.get("anchor", "t") if bodyPr is not None else "t"

        blocks = []
        for para in tx.findall(f"{A}p"):
            pPr = para.find(f"{A}pPr")
            algn = pPr.get("algn", "l") if pPr is not None else "l"
            spc = 1.22
            if pPr is not None:
                pct = pPr.find(f"{A}lnSpc/{A}spcPct")
                if pct is not None:
                    spc = int(pct.get("val")) / 100000.0
            groups, words, size, bold = [], [], 10.0, False
            for child in para:
                if child.tag == f"{A}br":          # a forced line break
                    groups.append(words)
                    words = []
                    continue
                if child.tag != f"{A}r":
                    continue
                rPr = child.find(f"{A}rPr")
                size = float(rPr.get("sz", "1000")) / 100.0
                bold = rPr.get("b") == "1"
                clr = rPr.find(f"{A}solidFill/{A}srgbClr")
                color = f"#{clr.get('val')}" if clr is not None else "#000000"
                text = child.find(f"{A}t").text or ""
                for word in text.split(" "):
                    if word:
                        words.append((word, (color, size, bold)))
            groups.append(words)
            if any(groups):
                blocks.append((groups, size, bold, spc, algn))

        if not blocks:
            continue

        def _lines(groups, size, bold, w=w):
            out_lines = []
            for grp in groups:
                out_lines.extend(_wrap(grp, _chars_per_line(w, size, bold, False))
                                 if grp else [[]])
            return out_lines

        total = sum(len(_lines(g, sz, bd)) * sz * spc
                    for g, sz, bd, spc, _ in blocks)
        cy = y if anchor == "t" else (y + (h - total) / 2 if anchor == "m"
                                     else y + h - total)
        for groups, size, bold, spc, algn in blocks:
            for line in _lines(groups, size, bold):
                if not line:
                    cy += size * spc
                    continue
                cy += size * spc
                text = " ".join(word for word, _ in line)
                color, sz, bd = line[0][1]
                anchor_attr = {"l": "start", "ctr": "middle",
                               "r": "end"}.get(algn, "start")
                lx = {"l": x, "ctr": x + w / 2, "r": x + w}[
                    {"l": "l", "ctr": "ctr", "r": "r"}.get(algn, "l")]
                out.append(
                    f'<text x="{lx:.1f}" y="{cy - size * 0.22:.1f}" '
                    f'font-family="Calibri, Carlito, DejaVu Sans, sans-serif" '
                    f'font-size="{sz}" fill="{color}" '
                    f'text-anchor="{anchor_attr}"'
                    f'{" font-weight=\"bold\"" if bd else ""}>'
                    f"{html.escape(text)}</text>"
                )

    body = "\n".join(out)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540" '
        f'viewBox="0 0 960 540">\n{body}\n</svg>'
    )


def main() -> None:
    if not DECK.exists():
        raise SystemExit(f"{DECK} not found; run generate_slides.py first")
    OUTDIR.mkdir(exist_ok=True)
    for old in OUTDIR.glob("slide-*"):
        old.unlink()

    with zipfile.ZipFile(DECK) as z:
        names = sorted(
            (n for n in z.namelist()
             if n.startswith("ppt/slides/slide") and n.endswith(".xml")),
            key=lambda n: int("".join(c for c in Path(n).stem if c.isdigit())),
        )
        paths = []
        for i, name in enumerate(names, 1):
            svg = OUTDIR / f"slide-{i:02d}.svg"
            svg.write_text(render(z.read(name)), encoding="utf-8")
            paths.append(svg)

    print(f"wrote {len(paths)} SVGs to {OUTDIR.relative_to(HERE.parent.parent)}")
    if "--png" in sys.argv:
        tool = shutil.which("rsvg-convert")
        if not tool:
            raise SystemExit("rsvg-convert not found; SVGs written anyway")
        for svg in paths:
            subprocess.run(
                [tool, "-w", "1280", "-o", str(svg.with_suffix(".png")),
                 str(svg)], check=True,
            )
        print(f"wrote {len(paths)} PNGs")


if __name__ == "__main__":
    main()
