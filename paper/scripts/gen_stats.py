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

import ast
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
TABLES = REPO / "paper" / "tables"

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


def _lifespan_loops(server: Path) -> list[str]:
    """Names of the background loops the ASGI lifespan starts.

    Derived rather than declared, and the reason is a defect this replaces. The
    loop count lived as three hand-typed copies -- the numeral and the word form
    in ``macros.tex`` and a third in this script's ``WORD_FORMS`` -- and
    ``check_word_forms`` verified them against *each other*. When
    ``ReleaseReconcilerTask`` was added to the lifespan the check went on passing
    and the paper went on saying eight. A number describing the implementation
    has to be read off the implementation.

    Matches ``await <name>.start()`` inside the lifespan callback. The event bus
    is started through a ``getattr`` handle rather than an attribute call, so it
    does not match -- which is correct, it is a transport and not a drain loop.
    """
    if not server.exists():
        return []
    tree = ast.parse(server.read_text(encoding="utf-8", errors="replace"))
    names: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Await):
                continue
            call = inner.value
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "start"
                and isinstance(func.value, ast.Name)
                and func.value.id not in names
            ):
                names.append(func.value.id)
    return names


def _loop_table_rows(table: Path) -> int:
    """Count the loop rows in ``tab-loops.tex``.

    The table is the prose form of the same fact as ``\\statLoops``. Counting its
    rows is what lets a mismatch between the table and the tree be an error
    rather than something a reader notices.
    """
    if not table.exists():
        return 0
    body = table.read_text(encoding="utf-8", errors="replace")
    body = body.split("\\midrule", 1)[-1].split("\\bottomrule", 1)[0]
    return len(re.findall(r"^\\textbf\{", body, re.M))


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
        Stat(
            "statLoops",
            str(len(_lifespan_loops(PKG / "server.py"))),
            "background loops started by the lifespan",
        ),
    ]


# Architectural constants appear twice in the paper: as a numeral (figures and
# tables) and as a word (prose). They are structural rather than measured, so they
# live in macros.tex -- but macros.tex claims this script checks that the two forms
# agree, and this is where that claim is honoured.
#
# \statLoops is deliberately *not* here. It described the implementation rather
# than the architecture, so agreement between its two spellings was never the
# property worth checking -- both were hand-typed and both were wrong the moment
# a ninth loop landed. It is derived in ``collect`` and checked against the tree
# by ``check_derived``.
WORD_FORMS = {
    "statLayers": "six",
    "statModes": "six",
    "statFamilies": "nine",
    "statScopes": "four",
    "statOptimizers": "five",
    "statChainTerms": "seven",
}
NUMERALS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
WORDS = {n: w for w, n in NUMERALS.items()}


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


# --------------------------------------------------------------------------
# The per-family guarantee table
# --------------------------------------------------------------------------
#
# Table 2 is the paper's most important table and was, until this script grew
# this section, a fourth hand-written copy of facts the implementation already
# declares in ``caliber/artifact_capabilities.py``. The failure mode is not
# hypothetical: a family whose rollback semantics changed would leave the table
# stating the old ones, and nothing anywhere would disagree.
#
# The split of responsibility is deliberate. The registry owns the *facts* --
# which families exist, what each one's rollback mechanism is, whether it is
# promotable. This script owns the *wording*, keyed by the registry's own
# vocabulary rather than by family, so a family that changes its rollback
# mechanism gets the new cell automatically and a mechanism with no wording is
# an error rather than a blank.

FAMILY_LABELS = {
    "prompt": "Prompt",
    "workflow": "Workflow",
    "knowledge_base": "Knowledge base",
    "skill": "Skill",
    "tool": "Tool",
    "test_set": "Test set",
    "mcp_server": "MCP server",
    "judge": "Judge",
    "agent": "Agent",
}

HISTORY_PROSE = {
    "immutable_registry_versions": "immutable \\mlflow{} registry versions",
    "immutable_published_versions": "editable drafts promoted to published version rows",
    "immutable_build_versions": "immutable build versions",
    "forward_only_snapshots": "mutable current record plus immutable snapshots",
    "named_version_rows": "separate \\code{(name, version)} rows with lifecycle status",
    "versioned_examples": "version counter plus per-example validity intervals",
    "audited_edits": (
        "mutable managed definitions with discovered inventories and audited edit history"
    ),
    "reusable_named_scorer": "operator-authored, reusable by \\code{Judge.<id>} token",
    "anchor_record": "the anchor record that items, jobs and approvals attach to",
}

# Each carries its own connector so the two halves compose into one clause.
LIVE_TARGET_PROSE = {
    "alias": " behind an alias such as \\prodalias{}",
    "deployment_alias": "; deployment aliases select one",
    "active_version_id": " behind \\code{active\\_version\\_id}",
    "current_record": "",
    "managed_definition": "",
    "enabled_flag": "",
    "none": "",
}

GATE_PROSE = {
    "enforced_refinement_advisory_direct": (
        "\\chipenf{} advancement to \\code{candidate\\_ready}"
        "\\newline\\chipadv{} per-version verdict"
    ),
    "enforced_deployment_gate": (
        "\\chipenf{} readiness\\newline\\chipenf{} \\depgate{}, optimistic alias check"
    ),
    "enforced_refinement_only": "\\chipenf{} advancement\\newline\\chipnone{} release gate",
    "workflow_preflight": "production workflow preflight",
    "evidence_asset": "\\chipna{} it \\emph{is} the evidence",
    "scoring_asset": "\\chipna{} it \\emph{is} a scorer",
    # Knowledge bases and tools both declare no gate. An earlier hand-written
    # table gave them different cells -- "no prompt-style verdict" against a bare
    # chip -- which asserted a distinction the implementation does not make. If
    # that distinction is real it belongs in the registry, not in the wording.
    "none": "\\chipnone{}",
    "not_applicable": "\\chipna{}",
}

# Keyed by mechanism, because the mechanism is the distinction the paper turns on:
# four families declare rollback and no two of them mean the same thing.
ROLLBACK_PROSE = {
    "alias_restore": (
        "operator- or admin-scoped; records and restores the outgoing alias target"
    ),
    "checkpoint_stack_pop": "rollback pops the deployment's checkpoint stack",
    "derived_from_activation_history": (
        "audited activation; rollback derives the prior build from history"
    ),
    "snapshot_restored_as_new_version": (
        "rollback restores the prior snapshot as a \\emph{new} version"
    ),
}

# The five families with no rollback have five different reasons, and the registry
# records that they have none rather than why. Until it carries the reason, the
# reason is wording and lives here -- but keyed by family, so a new family with no
# rollback has to say something rather than silently rendering blank.
NO_ROLLBACK_PROSE = {
    "tool": "read-only history; \\emph{no} live alias",
    "test_set": "\\chipnone{} no live alias or rollback",
    "mcp_server": (
        "\\chipnone{} no version rollback; connection and policy controls are fail-closed"
    ),
    "judge": "\\chipna{}",
    "agent": "\\code{enabled} is the pause/resume lever the workers read",
}

CALIBRATION_PROSE = {
    "provider_optimizer_and_eval": "provider optimizer + EvalProvider",
    "manifest_replay": "manifest replay",
    "retrieval_quality": "retrieval-quality calibration",
    "agent_free_optimizer": "agent-free optimizer path",
    "revision_fenced_suites": "revision-fenced deterministic suites",
    "connection_and_policy_tests": "connection and policy tests",
    "human_alignment": "Human-alignment agreement ($\\kappa$)",
    "not_applicable": "\\chipna{}",
}


def read_capability_registry(module: Path) -> dict[str, dict[str, object]]:
    """Parse ``ARTIFACT_FAMILY_CAPABILITIES`` without importing the package.

    ``literal_eval`` on the assignment's value node rather than an import,
    because this script has to run in a bare TeX Live environment with no
    CALIBER virtualenv. The registry is all literals, so nothing is lost.
    """
    if not module.exists():
        return {}
    tree = ast.parse(module.read_text(encoding="utf-8", errors="replace"))
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            first = node.targets[0]
            target = first.id if isinstance(first, ast.Name) else None
        if target == "ARTIFACT_FAMILY_CAPABILITIES" and node.value is not None:
            value = ast.literal_eval(node.value)
            return value if isinstance(value, dict) else {}
    return {}


# Hand-measured column widths. They are typesetting rather than facts, but they
# live here because a bare list of rows cannot be \input into an alignment -- TeX
# processes & and \\ during the alignment scan and the file switch breaks it,
# surfacing as "Misplaced \noalign" at the \bottomrule. Emitting the whole tabular
# is also what generated/implementation-table.tex already does.
FAMILIES_COLSPEC = "@{}L{1.86cm}L{3.44cm}L{3.34cm}L{3.38cm}L{2.72cm}@{}"
FAMILIES_HEADINGS = (
    "\\thead{Family} & \\thead{History \\& liveness} & \\thead{Gate semantics} &\n"
    "\\thead{Release / rollback} & \\thead{Calibration idiom} \\\\"
)


def families_table(registry: dict[str, dict[str, object]]) -> tuple[list[str], list[str]]:
    """Render the guarantee table, plus any wording the registry outran."""
    problems: list[str] = []
    rows: list[str] = [
        f"\\begin{{tabular}}{{{FAMILIES_COLSPEC}}}",
        "\\toprule",
        "\\theadrow",
        FAMILIES_HEADINGS,
        "\\midrule",
    ]

    def prose(table: dict[str, str], key: object, what: str, family: str) -> str:
        if key in table:
            return table[str(key)]
        problems.append(f"{family}: no {what} wording for {key!r}")
        return "\\chipna{}"

    for index, (family, contract) in enumerate(registry.items()):
        label = FAMILY_LABELS.get(family)
        if label is None:
            problems.append(f"{family}: no display label")
            label = family.replace("_", " ").capitalize()

        history = prose(HISTORY_PROSE, contract.get("history"), "history", family)
        liveness = prose(
            LIVE_TARGET_PROSE, contract.get("live_target"), "live-target", family
        )
        gate = prose(GATE_PROSE, contract.get("gate_mode"), "gate", family)
        mechanism = contract.get("rollback")
        if mechanism == "none":
            release = prose(NO_ROLLBACK_PROSE, family, "no-rollback", family)
        else:
            release = prose(ROLLBACK_PROSE, mechanism, "rollback", family)
        calibration = prose(
            CALIBRATION_PROSE, contract.get("calibration"), "calibration", family
        )

        # Alternating tint, and no blank lines: a blank line inside a tabular is
        # a \par, which surfaces as "Misplaced \noalign" at the \bottomrule
        # rather than anywhere near the row that caused it.
        if index % 2:
            rows.append("\\zebra")
        rows.append(
            f"\\textbf{{{label}}} &\n{history}{liveness} &\n{gate} &\n"
            f"{release} &\n{calibration} \\\\"
        )
    rows += ["\\bottomrule", "\\end{tabular}"]
    return rows, problems


def check_derived(macros: Path, stats: list[Stat]) -> list[str]:
    """Check every hand-written copy of a derived number against the derived one.

    ``macros.tex`` carries a ``\\providecommand`` default for each generated
    macro so the document typesets when this script has not run. That default is
    a second copy, and a second copy of a number is the defect this whole module
    exists to prevent -- so a stale one is an error here rather than a surprise
    in a build that skipped the generator.

    ``tab-loops.tex`` is a third copy in prose form: one row per loop. Its row
    count is checked against the same derived value.
    """
    problems: list[str] = []
    derived = {s.macro: s.value for s in stats}
    if not macros.exists():
        return [f"macros.tex not found at {macros}"]
    text = macros.read_text(encoding="utf-8", errors="replace")

    loops = derived.get("statLoops")
    if loops is None or loops == "0":
        return ["statLoops did not resolve; check caliber/src/caliber/server.py"]

    for suffix, expected in (
        ("", loops),
        ("W", WORDS.get(int(loops), loops)),
        ("C", WORDS.get(int(loops), loops).capitalize()),
    ):
        found = re.search(
            rf"\\providecommand\{{\\statLoops{suffix}\}}\{{(\w+)\}}", text
        )
        if not found:
            problems.append(f"\\statLoops{suffix} default not declared in macros.tex")
        elif found.group(1) != expected:
            problems.append(
                f"macros.tex declares \\statLoops{suffix} = {found.group(1)}, "
                f"but the tree has {expected}"
            )

    rows = _loop_table_rows(TABLES / "tab-loops.tex")
    if rows != int(loops):
        problems.append(
            f"tab-loops.tex has {rows} loop rows but the lifespan starts {loops}. "
            f"Add or remove the row rather than changing the number."
        )
    return problems


TEX_HEADER = """%% GENERATED FILE -- do not edit.
%% Produced by paper/scripts/gen_stats.py from the CALIBER tree at {rev}.
%% Re-run that script after any change to the implementation.
"""


def main() -> int:
    macros = REPO / "paper" / "tex" / "macros.tex"
    word_problems = check_word_forms(macros)
    stats = collect()
    derived_problems = check_derived(macros, stats)
    missing = [s.macro for s in stats if s.value in ("0", "0k")]
    OUT.mkdir(parents=True, exist_ok=True)
    rev = _git_rev()

    lines = [TEX_HEADER.format(rev=rev)]
    for s in stats:
        lines.append(f"\\renewcommand{{\\{s.macro}}}{{{s.value}}}  % {s.note}")
        # A derived count still needs its prose spellings, or the sentence forms
        # become a hand-maintained copy of a generated number.
        if s.macro == "statLoops" and s.value.isdigit():
            word = WORDS.get(int(s.value))
            if word:
                lines.append(f"\\renewcommand{{\\statLoopsW}}{{{word}}}  % prose form")
                lines.append(
                    f"\\renewcommand{{\\statLoopsC}}{{{word.capitalize()}}}"
                    f"  % sentence-initial form"
                )
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

    # The guarantee table's rows, derived from the implementation's own
    # declarations. tab-families.tex keeps the float, caption and column spec --
    # those are typesetting, not facts.
    registry = read_capability_registry(PKG / "artifact_capabilities.py")
    rows, table_problems = families_table(registry)
    (OUT / "families-table.tex").write_text(
        TEX_HEADER.format(rev=rev) + "\n".join(rows) + "\n", encoding="utf-8"
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
    if table_problems:
        print(
            "\nERROR: the capability registry outran the guarantee table's wording:",
            file=sys.stderr,
        )
        for p in table_problems:
            print(f"  {p}", file=sys.stderr)
        ok = False
    else:
        print(f"generated {len(registry)} guarantee-table rows from the registry")
    if derived_problems:
        print("\nERROR: a hand-written copy disagrees with the tree:", file=sys.stderr)
        for p in derived_problems:
            print(f"  {p}", file=sys.stderr)
        ok = False
    else:
        loops = _lifespan_loops(PKG / "server.py")
        print(f"checked {len(loops)} lifespan loops against macros.tex and tab-loops.tex")
        print(f"  loops: {', '.join(loops)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
