"""Read the paper's generated counts so a diagram never hard-codes one.

``gen_stats.py`` derives every implementation count from the CALIBER tree and writes
them to ``paper/generated/stats.tex`` as LaTeX macros. The TikZ figures consume those
macros directly. An Excalidraw scene cannot, so it reads the same file here.

That is the whole reason these diagrams are generated rather than drawn: a number in
a hand-drawn diagram is a number that will be wrong after the next refactor, and
nothing will catch it.
"""

from __future__ import annotations

import re
from pathlib import Path

_PAPER = Path(__file__).resolve().parents[1]
# Two sources, deliberately: implementation counts are regenerated into stats.tex,
# while architectural constants (layer count, scope count) are declared once in
# macros.tex. Both are single-sourced; neither is typed into a diagram.
STATS_TEX = _PAPER / "generated" / "stats.tex"
MACROS_TEX = _PAPER / "tex" / "macros.tex"

# Fallbacks, so a diagram still builds before gen_stats.py has been run. These
# mirror the \providecommand defaults in tex/macros.tex.
_DEFAULTS = {
    "statRouteDecls": "454",
    "statRouteModules": "46",
    "statModels": "71",
    "statSchemas": "218",
    "statNodeTypes": "29",
    "statMigrations": "81",
    "statPyLoc": "122k",
    "statUiLoc": "173k",
    "statLoops": "8",
    "statFamilies": "9",
    "statScopes": "4",
    "statOptimizers": "5",
    "statLayers": "6",
    "statModes": "6",
}

_cache: dict[str, str] | None = None


def _load() -> dict[str, str]:
    global _cache
    if _cache is not None:
        return _cache
    values = dict(_DEFAULTS)
    if MACROS_TEX.exists():
        text = MACROS_TEX.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"\\providecommand\{\\(stat\w+)\}\{([^}]*)\}", text):
            values[m.group(1)] = m.group(2)
    if STATS_TEX.exists():
        text = STATS_TEX.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"\\renewcommand\{\\(stat\w+)\}\{([^}]*)\}", text):
            values[m.group(1)] = m.group(2)
    _cache = values
    return values


def stat(name: str) -> str:
    """The generated value for a ``\\statX`` macro, as a string."""
    values = _load()
    if name not in values:
        raise KeyError(
            f"unknown stat {name!r}; add it to _DEFAULTS or run gen_stats.py"
        )
    return values[name]


def missing_from_stats_tex() -> list[str]:
    """Stats that fell back to a hard-coded default rather than being read."""
    if not STATS_TEX.exists():
        return ["generated/stats.tex has not been built (run: make stats)"]
    present: set[str] = set()
    for path, pattern in (
        (STATS_TEX, r"\\renewcommand\{\\(stat\w+)\}"),
        (MACROS_TEX, r"\\providecommand\{\\(stat\w+)\}"),
    ):
        if path.exists():
            present |= set(
                re.findall(pattern, path.read_text(encoding="utf-8", errors="replace"))
            )
    return sorted(k for k in _DEFAULTS if k not in present)
