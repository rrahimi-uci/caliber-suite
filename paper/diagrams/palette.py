"""The role-based colour system, mirroring ``paper/tex/preamble.tex``.

Colours are assigned by the *role* a box plays, never by decoration, so a reader
who learns the key once can read every diagram. Keeping this file in step with the
TikZ palette is what lets an Excalidraw figure and a TikZ figure for the same role
be the same colour by construction rather than by eye -- ``check_against_tex``
below asserts it, and ``build.py`` calls that on every run.

Fills are held at a light tint and strokes at full saturation so the figures
survive greyscale printing: the tints differ in luminance, not only in hue.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

TEX_PREAMBLE = Path(__file__).resolve().parents[1] / "tex" / "preamble.tex"


@dataclass(frozen=True)
class Role:
    """One semantic role: a stroke colour and the light tint that fills it."""

    name: str
    stroke: str
    fill: str
    tex_stroke: str  # the \definecolor name in preamble.tex
    tex_fill: str | None


# The eight roles the paper's figures use, in the order the shared key lists them.
ROLES: dict[str, Role] = {
    "surface": Role("surface", "#0284c7", "#e0f2fe", "cbSurface", "cbSurfaceBg"),
    "control": Role("control", "#6d28d9", "#ede9fe", "cbControl", "cbControlBg"),
    "govern": Role("govern", "#be185d", "#fce7f3", "cbGovern", "cbGovernBg"),
    "asset": Role("asset", "#c2410c", "#fff2e6", "cbAsset", "cbAssetBg"),
    "extern": Role("extern", "#15803d", "#ecfdf3", "cbExtern", "cbExternBg"),
    "async": Role("async", "#b45309", "#fef3c7", "cbAsync", "cbAsyncBg"),
    "store": Role("store", "#4338ca", "#eef2ff", "cbStore", "cbStoreBg"),
    "muted": Role("muted", "#64748b", "#f1f5f9", "cbMuted", "cbMutedBg"),
}

INK = "#1f2933"
RULE = "#cbd5e1"
TRANSPARENT = "transparent"


def role(name: str) -> Role:
    try:
        return ROLES[name]
    except KeyError:
        raise KeyError(
            f"unknown role {name!r}; expected one of {sorted(ROLES)}"
        ) from None


def _tex_colours() -> dict[str, str]:
    """Every ``\\definecolor{name}{HTML}{RRGGBB}`` in the LaTeX preamble."""
    if not TEX_PREAMBLE.exists():
        return {}
    text = TEX_PREAMBLE.read_text(encoding="utf-8", errors="replace")
    pattern = r"\\definecolor\s*\{(\w+)\}\s*\{HTML\}\s*\{([0-9A-Fa-f]{6})\}"
    return {m.group(1): "#" + m.group(2).lower() for m in re.finditer(pattern, text)}


def check_against_tex() -> list[str]:
    """Report every colour that has drifted from the LaTeX palette.

    The two palettes must agree or the paper ends up with two visual systems that
    almost match, which is worse than two that obviously differ.
    """
    tex = _tex_colours()
    if not tex:
        return [f"could not read {TEX_PREAMBLE}"]

    problems: list[str] = []
    for r in ROLES.values():
        for ours, tex_name in ((r.stroke, r.tex_stroke), (r.fill, r.tex_fill)):
            if tex_name is None:
                continue
            theirs = tex.get(tex_name)
            if theirs is None:
                problems.append(f"{tex_name} is not defined in preamble.tex")
            elif theirs != ours:
                problems.append(
                    f"{r.name}: {tex_name} is {theirs} in TeX but {ours} here"
                )
    # cbTableAlt is a table banding tint with no diagram role, so it is not
    # mirrored here; only the colours both systems use are cross-checked.
    for ours, tex_name in ((INK, "cbInk"), (RULE, "cbRule")):
        theirs = tex.get(tex_name)
        if theirs != ours:
            problems.append(f"{tex_name} is {theirs} in TeX but {ours} here")
    return problems
