#!/usr/bin/env python3
"""Build every Excalidraw scene, then render and convert each one.

    python3 diagrams/build.py [figure-name ...]

For each figure this writes three artifacts to ``paper/generated/diagrams/``:

    fig-x.excalidraw   a real Excalidraw document -- open it at excalidraw.com
    fig-x.svg          rendered by render.mjs via roughjs
    fig-x.pdf          converted by rsvg-convert, for \\includegraphics

The build refuses to proceed if the diagram palette has drifted from the LaTeX one.
Two visual systems that almost match are worse than two that obviously differ, and
that is a failure a human will not reliably notice by eye.
"""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "generated" / "diagrams"
sys.path.insert(0, str(HERE))

import palette  # noqa: E402
import stats  # noqa: E402

FIGURES = [
    "fig_layers",
    "fig_system",
    "fig_dataflow",
    "fig_topologies",
]


def _run(cmd: list[str], what: str, cwd: Path | None = None) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if proc.returncode != 0:
        sys.stderr.write(f"\n{what} failed:\n{proc.stdout}{proc.stderr}\n")
        raise SystemExit(1)
    if proc.stdout.strip():
        print(proc.stdout.rstrip())


def ensure_roughjs() -> None:
    """Install roughjs on first run; it is the only external dependency."""
    if (HERE / "node_modules" / "roughjs").exists():
        return
    if shutil.which("npm") is None:
        raise SystemExit(
            "npm is required to render the Excalidraw scenes (it installs roughjs, "
            "the stroke library Excalidraw itself uses). Install Node, or build the "
            "paper with the TikZ figures instead -- see paper/README.md."
        )
    print("==> installing roughjs (first run only)")
    _run(["npm", "install", "--no-audit", "--no-fund"], "npm install", cwd=HERE)


def preflight() -> None:
    drift = palette.check_against_tex()
    if drift:
        sys.stderr.write("palette has drifted from tex/preamble.tex:\n")
        for d in drift:
            sys.stderr.write(f"  {d}\n")
        raise SystemExit(1)
    print(f"palette  {len(palette.ROLES)} roles agree with tex/preamble.tex")

    gaps = stats.missing_from_stats_tex()
    if gaps:
        print(f"stats    WARNING: fell back to defaults for {gaps}")
    else:
        print("stats    all counts read from generated/stats.tex")


# rsvg-convert emits any glyph outside WinAnsi as a Type 3 font -- a bitmap-ish
# outline font that is a routine camera-ready rejection, and one that no visual
# check will catch. WinAnsi covers Latin-1, so the rule is simply: every codepoint
# in diagram text must be below U+0100. That excludes the em dash, the en dash, and
# the arrows, all of which are easy to reach for and all of which must be drawn as
# geometry or reworded instead.
def check_winansi(scene) -> list[str]:
    problems = []
    for el in scene.elements:
        if el.get("type") != "text":
            continue
        for ch in el.get("text", ""):
            if ch != "\n" and ord(ch) > 0xFF:
                problems.append(
                    f"U+{ord(ch):04X} {ch!r} in {el['id']}: {el['text'][:48]!r}"
                )
    return sorted(set(problems))


def build_one(module_name: str) -> None:
    mod = importlib.import_module(module_name)
    scene = mod.build()
    stem = scene.name

    excalidraw = OUT / f"{stem}.excalidraw"
    render_json = OUT / f"{stem}.scene.json"
    svg = OUT / f"{stem}.svg"
    pdf = OUT / f"{stem}.pdf"

    bad = check_winansi(scene)
    if bad:
        sys.stderr.write(
            f"{stem}: text contains glyphs outside WinAnsi, which rsvg-convert "
            f"would emit as Type 3 fonts:\n"
        )
        for b in bad:
            sys.stderr.write(f"  {b}\n")
        sys.stderr.write("  reword, or draw the mark as geometry.\n")
        raise SystemExit(1)

    excalidraw.write_text(scene.to_excalidraw(), encoding="utf-8")
    render_json.write_text(scene.to_render_json(), encoding="utf-8")

    _run(["node", str(HERE / "render.mjs"), str(render_json), str(svg)], "render.mjs")

    if shutil.which("rsvg-convert") is None:
        raise SystemExit(
            "rsvg-convert is required to convert the SVG to PDF "
            "(brew install librsvg)."
        )
    _run(["rsvg-convert", "-f", "pdf", "-o", str(pdf), str(svg)], "rsvg-convert")

    # Every scene is authored in points at final size and placed at that width, so
    # the smallest type in the figure must already clear the paper's 7pt floor.
    # Checking here means a figure cannot regress into illegibility unnoticed.
    from scene import FLOOR

    sizes = [e["fontSize"] for e in scene.elements if e.get("type") == "text"]
    if sizes and min(sizes) < FLOOR - 1e-9:
        sys.stderr.write(
            f"{stem}: smallest type is {min(sizes)}pt, below the {FLOOR}pt floor\n"
        )
        raise SystemExit(1)

    if shutil.which("pdffonts"):
        listing = subprocess.run(
            ["pdffonts", str(pdf)], capture_output=True, text=True
        ).stdout
        if "Type 3" in listing:
            sys.stderr.write(
                f"{stem}: the converted PDF contains a Type 3 font:\n{listing}"
            )
            raise SystemExit(1)

    size_kb = pdf.stat().st_size / 1024
    print(f"  {stem}: {len(scene.elements)} elements, "
          f"{scene.width:.0f}x{scene.height:.0f}pt "
          f"({scene.width / 28.4527:.1f}x{scene.height / 28.4527:.1f}cm), "
          f"type >= {min(sizes):.0f}pt, {size_kb:.0f}kB, no Type 3")


def main(argv: list[str]) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    preflight()
    ensure_roughjs()

    wanted = argv or FIGURES
    wanted = [w.replace("-", "_") for w in wanted]
    unknown = [w for w in wanted if w not in FIGURES]
    if unknown:
        raise SystemExit(f"unknown figure(s) {unknown}; known: {FIGURES}")

    print(f"==> building {len(wanted)} scene(s)")
    for name in wanted:
        build_one(name)

    manifest = OUT / "manifest.json"
    manifest.write_text(
        json.dumps({"figures": [f.replace("_", "-") for f in wanted]}, indent=2),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
