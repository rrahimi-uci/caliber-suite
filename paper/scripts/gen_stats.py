#!/usr/bin/env python3
"""Recompute the paper's quantitative facts from the CALIBER tree.

Every number the paper states about the size and shape of the implementation is
produced here and written to ``paper/generated/stats.tex`` as LaTeX macro
definitions, plus ``paper/generated/implementation-table.tex`` as a formatted
table body. ``paper/tex/macros.tex`` declares the same macros with
``\\providecommand``, so the document still typesets when this script has not
been run -- but a real build re-derives them, which is what keeps the prose from
drifting away from the artifact it describes.

Usage (from anywhere)::

    python3 paper/scripts/gen_stats.py

The script only reads; it writes nothing outside ``paper/generated/``.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PKG = REPO / "caliber" / "src" / "caliber"
TESTS = REPO / "caliber" / "tests"
UI = REPO / "caliber" / "caliber-ui" / "src"
OUT = REPO / "paper" / "generated"

# Directories that hold vendored or build output rather than authored source.
EXCLUDE_PARTS = {
    "__pycache__",
    "node_modules",
    ".venv",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "allure-results",
    "mlruns",
}


def _authored(path: Path) -> bool:
    return not (set(path.parts) & EXCLUDE_PARTS)


def _walk(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix in suffixes and _authored(p)
    )


def _loc(paths: list[Path]) -> int:
    total = 0
    for p in paths:
        try:
            total += sum(1 for _ in p.open("r", encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return total


def _count_matches(paths: list[Path], pattern: str) -> int:
    rx = re.compile(pattern, re.MULTILINE)
    total = 0
    for p in paths:
        try:
            total += len(rx.findall(p.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            pass
    return total


def _findall(paths: list[Path], pattern: str) -> list[str]:
    """Every capture-group match of ``pattern`` across ``paths``."""
    rx = re.compile(pattern, re.MULTILINE)
    out: list[str] = []
    for p in paths:
        try:
            out.extend(rx.findall(p.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            pass
    return out


def _enum_members(path: Path, class_name: str) -> int:
    """Count ``NAME = "value"`` members inside a named class body."""
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.search(rf"^class {class_name}\b.*?:\n(.*?)(?=^\S)", text, re.S | re.M)
    if not m:
        return 0
    return len(re.findall(r'^\s+[A-Z][A-Z0-9_]* = "', m.group(1), re.M))


def _round_k(n: int) -> str:
    """Render a line count the way the paper cites it: nearest thousand."""
    return f"{round(n / 1000)}k"


def _git_rev() -> str:
    try:
        revision = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        rev = revision.stdout.strip() or "unknown"
        status = subprocess.run(
            ["git", "-C", str(REPO), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return f"{rev}-dirty" if status.stdout.strip() else rev
    except (OSError, subprocess.SubprocessError):
        return "unknown"


@dataclass(frozen=True)
class Stat:
    macro: str
    value: str
    note: str


def collect() -> list[Stat]:
    py = _walk(PKG, (".py",))
    tests = [p for p in _walk(TESTS, (".py",)) if p.name.startswith("test_")]
    tests_all = _walk(TESTS, (".py",))
    ui = _walk(UI, (".ts", ".tsx"))
    routes = _walk(PKG / "routes", (".py",))
    migrations = _walk(PKG / "db" / "migrations" / "versions", (".py",))

    models = PKG / "db" / "models.py"
    schemas = PKG / "schemas.py"
    ir = PKG / "workflows" / "ir.py"
    assistant = _walk(PKG / "assistant", (".py",))
    capabilities = PKG / "assistant" / "capabilities.py"

    return [
        Stat("statPyFiles", str(len(py)), "authored Python modules in the package"),
        Stat("statPyLoc", _round_k(_loc(py)), "physical lines of Python"),
        Stat("statTestFiles", str(len(tests)), "pytest modules"),
        Stat("statTestLoc", _round_k(_loc(tests_all)), "lines of test code"),
        Stat("statUiFiles", str(len(ui)), "TypeScript/TSX modules in the SPA"),
        Stat("statUiLoc", _round_k(_loc(ui)), "lines of TypeScript"),
        Stat("statRouteModules", str(len(routes)), "route modules"),
        Stat(
            "statRouteDecls",
            str(_count_matches(routes, r'\.(?:get|post|put|patch|delete)\("')),
            "HTTP method declarations",
        ),
        Stat("statMigrations", str(len(migrations)), "Alembic revisions"),
        Stat(
            "statModels",
            str(_count_matches([models], r"^class \w+\(")),
            "ORM model classes",
        ),
        Stat(
            "statSchemas",
            str(_count_matches([schemas], r"^class \w+\(")),
            "Pydantic schema classes",
        ),
        Stat("statNodeTypes", str(_enum_members(ir, "NodeType")), "workflow node types"),
        # These two quantify how far the capability-registry seam has actually got:
        # hand-written tools versus registry-projected capabilities. The paper calls
        # the seam "emerging" on the strength of this ratio, so it must be derived
        # rather than asserted.
        Stat(
            "statAriaTools",
            str(len(set(_findall(assistant, r"def (_t_[a-z_]+)")))),
            "hand-written assistant tools",
        ),
        Stat(
            "statAriaProjected",
            str(len(set(_findall([capabilities], r'key="([a-z_.]+)"')))),
            "registry-projected capabilities",
        ),
    ]


# Architectural constants appear twice in the paper: as a numeral (figures and
# tables) and as a word (prose). They are structural rather than measured, so they
# live in macros.tex -- but macros.tex claims this script checks that the two forms
# agree, and this is where that claim is honoured.
WORD_FORMS = {
    "statLayers": "six",
    "statModes": "six",
    "statFamilies": "nine",
    "statScopes": "four",
    "statLoops": "eight",
    "statOptimizers": "five",
    "statChainTerms": "seven",
}
NUMERALS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def check_word_forms(macros: Path) -> list[str]:
    """Verify each \\statXW word form matches its \\statX numeral in macros.tex."""
    if not macros.exists():
        return [f"macros.tex not found at {macros}"]
    text = macros.read_text(encoding="utf-8", errors="replace")
    problems = []
    for macro, word in WORD_FORMS.items():
        num = re.search(rf"\\providecommand\{{\\{macro}\}}\{{(\d+)\}}", text)
        if not num:
            problems.append(f"\\{macro} numeral not declared")
            continue
        # Both the lowercase (prose) and capitalized (sentence-initial) spellings.
        for suffix in ("W", "C"):
            wrd = re.search(
                rf"\\providecommand\{{\\{macro}{suffix}\}}\{{(\w+)\}}", text
            )
            if not wrd:
                problems.append(f"\\{macro}{suffix} word form not declared")
                continue
            if NUMERALS.get(wrd.group(1).lower()) != int(num.group(1)):
                problems.append(
                    f"\\{macro} = {num.group(1)} but "
                    f"\\{macro}{suffix} = {wrd.group(1)}"
                )
    return problems


TEX_HEADER = """%% GENERATED FILE -- do not edit.
%% Produced by paper/scripts/gen_stats.py from the CALIBER tree at {rev}.
%% Re-run that script after any change to the implementation.
"""


def main() -> int:
    word_problems = check_word_forms(REPO / "paper" / "tex" / "macros.tex")
    stats = collect()
    missing = [s.macro for s in stats if s.value in ("0", "0k")]
    OUT.mkdir(parents=True, exist_ok=True)
    rev = _git_rev()

    lines = [TEX_HEADER.format(rev=rev)]
    for s in stats:
        lines.append(f"\\renewcommand{{\\{s.macro}}}{{{s.value}}}  % {s.note}")
    lines.append(f"\\providecommand{{\\statGitRev}}{{{rev}}}")
    lines.append(f"\\renewcommand{{\\statGitRev}}{{{rev}}}")
    (OUT / "stats.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # A formatted table body the Implementation section \inputs directly.
    rows = [
        ("Python modules (package)", "statPyFiles", "statPyLoc"),
        ("TypeScript/TSX modules (SPA)", "statUiFiles", "statUiLoc"),
        ("Test modules", "statTestFiles", "statTestLoc"),
    ]
    body = [TEX_HEADER.format(rev=rev), "\\begin{tabular}{@{}lrr@{}}", "\\toprule",
            "Component & Files & Lines \\\\", "\\midrule"]
    for label, files, loc in rows:
        body.append(f"{label} & \\{files} & \\{loc} \\\\")
    body += ["\\midrule",
             "Route modules / HTTP declarations & \\statRouteModules & \\statRouteDecls \\\\",
             "ORM models / Pydantic schemas & \\statModels & \\statSchemas \\\\",
             "Alembic revisions / workflow node types & \\statMigrations & \\statNodeTypes \\\\",
             "\\bottomrule", "\\end{tabular}"]
    (OUT / "implementation-table.tex").write_text(
        "\n".join(body) + "\n", encoding="utf-8"
    )

    width = max(len(s.macro) for s in stats)
    print(f"CALIBER tree at {rev}")
    for s in stats:
        print(f"  {s.macro:<{width}}  {s.value:>6}   {s.note}")
    print(f"\nwrote {OUT / 'stats.tex'}")
    print(f"wrote {OUT / 'implementation-table.tex'}")
    ok = True
    if missing:
        print(f"\nERROR: these resolved to zero, check the paths: {missing}",
              file=sys.stderr)
        ok = False
    if word_problems:
        print("\nERROR: numeral and word forms disagree in macros.tex:",
              file=sys.stderr)
        for p in word_problems:
            print(f"  {p}", file=sys.stderr)
        ok = False
    else:
        print(f"checked {len(WORD_FORMS)} numeral/word pairs in macros.tex")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
