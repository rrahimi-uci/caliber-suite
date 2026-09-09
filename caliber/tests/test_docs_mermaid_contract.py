"""Mermaid syntax gate for the Markdown documentation sources.

Why this exists: a Mermaid block with a syntax error does not fail a build. It
renders as an error box on GitHub and in the docs site, so a broken diagram
ships silently and stays broken until a human happens to look at that page.
Three of four recently-added documents shipped with a non-rendering diagram
before this gate existed.

Scope and honesty about it: this is a **lint for the failure classes that have
actually occurred in this repository**, not a full Mermaid parse. A full parse
needs the Mermaid library and a DOM, which is a heavy dependency for CI to carry
and is not currently vendored. Every rule below therefore encodes a specific,
observed, silent-failure mode and is regression-tested against the real broken
source that motivated it (see ``test_rules_catch_the_historical_regressions``).

To run a genuine full parse locally, outside CI:

    npm install mermaid@11 jsdom
    # then parse each block with mermaid.parse() under jsdom

Sources checked are the Markdown **inputs** only — ``ARCHITECTURE.md`` and
``docs/**/*.md``. ``docs-site/``, ``caliber/caliber-ui/public/docs/`` and
``caliber/src/caliber/ui/docs/`` are generated copies; linting them would report
the same defect several times and would fail on content nobody edits directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: Directories holding generated copies of the Markdown sources.
_GENERATED_PARTS: Final[frozenset[str]] = frozenset({"docs-site", "node_modules", "public"})

#: Diagram types this repository uses. An unrecognized first token is far more
#: likely to be a typo than a new Mermaid diagram type, and a typo there fails
#: the whole block.
_KNOWN_DIAGRAM_TYPES: Final[frozenset[str]] = frozenset(
    {
        "flowchart",
        "graph",
        "sequencediagram",
        "statediagram",
        "statediagram-v2",
        "erdiagram",
        "classdiagram",
        "journey",
        "gantt",
        "pie",
        "mindmap",
        "timeline",
        "quadrantchart",
        "gitgraph",
        "c4context",
        "block-beta",
        "sankey-beta",
        "xychart-beta",
    }
)

#: A sequence-diagram message: ``Actor->>Other: text`` in any arrow form.
_SEQUENCE_MESSAGE: Final[re.Pattern[str]] = re.compile(
    r"^\s*[A-Za-z_][\w-]*\s*"  # sender
    r"(?:-{1,2}>>?|--?>|-[-.]->?|<<->>|<->)"  # arrow
    r"[+-]?\s*[A-Za-z_][\w-]*\s*:\s*(?P<body>.+)$"
)

#: Shape openers that must be closed by their mirror inside a ``[...]`` label.
#: ``A[/text/]`` is a parallelogram; ``A[/text]`` opens one and never closes it,
#: which is a lexical error that kills the block.
_ASYMMETRIC_SHAPES: Final[tuple[tuple[str, str], ...]] = (("/", "/"), ("\\", "\\"))


@dataclass(frozen=True)
class Block:
    """One fenced ``mermaid`` block located in a Markdown source."""

    path: Path
    start_line: int
    body: str

    @property
    def rel(self) -> str:
        return self.path.relative_to(REPO_ROOT).as_posix()

    def where(self, offset: int = 0) -> str:
        return f"{self.rel}:{self.start_line + offset}"


def _markdown_sources() -> list[Path]:
    """Every Markdown source the gate covers, excluding generated copies."""
    sources: list[Path] = []
    architecture = REPO_ROOT / "ARCHITECTURE.md"
    if architecture.is_file():
        sources.append(architecture)
    docs = REPO_ROOT / "docs"
    if docs.is_dir():
        sources.extend(
            path
            for path in sorted(docs.rglob("*.md"))
            if not _GENERATED_PARTS.intersection(path.parts)
        )
    return sources


def _mermaid_blocks(path: Path) -> list[Block]:
    """Extract fenced ``mermaid`` blocks with 1-indexed body start lines."""
    blocks: list[Block] = []
    lines = path.read_text(encoding="utf-8").split("\n")
    body: list[str] | None = None
    body_start = 0
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if body is None:
            if stripped == "```mermaid":
                body = []
                body_start = index + 1
            continue
        if stripped == "```":
            blocks.append(Block(path=path, start_line=body_start, body="\n".join(body)))
            body = None
            continue
        body.append(line)
    return blocks


def _all_blocks() -> list[Block]:
    return [block for path in _markdown_sources() for block in _mermaid_blocks(path)]


def _diagram_type(body: str) -> str:
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped and not stripped.startswith("%%"):
            return stripped.split()[0].split("(")[0].lower()
    return ""


def _semicolon_violations(block: Block) -> list[str]:
    """Rule 1 — ``;`` inside a sequence-diagram message body.

    Mermaid reads ``;`` as a statement separator, so everything after it is
    parsed as a new statement and the block fails. ``docs/STYLE.md`` documents
    this: "In a sequenceDiagram message, never use ``;`` -- use ``,`` or
    ``and``." It is the single most frequent defect in this repository's history.
    """
    if _diagram_type(block.body) != "sequencediagram":
        return []
    problems: list[str] = []
    for offset, line in enumerate(block.body.split("\n")):
        match = _SEQUENCE_MESSAGE.match(line)
        if match and ";" in match.group("body"):
            problems.append(
                f"{block.where(offset)}: ';' in a sequenceDiagram message ends the "
                f"statement and breaks the diagram -- use ',' or 'and'. Line: "
                f"{line.strip()!r}"
            )
    return problems


def _unclosed_shape_violations(block: Block) -> list[str]:
    """Rule 2 — a ``[...]`` node label that opens an asymmetric shape.

    ``A[/text/]`` is a parallelogram. ``A[/text]`` opens the parallelogram and
    never closes it, which is a lexical error rather than a rendering quirk. A
    label that legitimately starts with a slash (a URL path, for instance) has
    to be quoted: ``A["/projects wire API"]``.
    """
    problems: list[str] = []
    for offset, line in enumerate(block.body.split("\n")):
        for label in re.findall(r"\[([^\[\]]*)\]", line):
            if label.startswith('"') or label.startswith("'"):
                continue
            for opener, closer in _ASYMMETRIC_SHAPES:
                if label.startswith(opener) and not label.endswith(closer):
                    problems.append(
                        f"{block.where(offset)}: node label {label!r} opens an "
                        f"asymmetric '[{opener}' shape without closing "
                        f"'{closer}]'. Quote the label instead: [\"{label}\"]. "
                        f"Line: {line.strip()!r}"
                    )
    return problems


def _diagram_type_violations(block: Block) -> list[str]:
    """Rule 3 — an unrecognized diagram type on the first meaningful line."""
    declared = _diagram_type(block.body)
    if not declared:
        return [f"{block.where()}: empty mermaid block"]
    if declared not in _KNOWN_DIAGRAM_TYPES:
        return [
            f"{block.where()}: unrecognized diagram type {declared!r}. If this is "
            f"a genuinely new Mermaid diagram type, add it to "
            f"_KNOWN_DIAGRAM_TYPES; otherwise it is a typo that fails the block."
        ]
    return []


def _lint(block: Block) -> list[str]:
    return [
        *_diagram_type_violations(block),
        *_semicolon_violations(block),
        *_unclosed_shape_violations(block),
    ]


def test_documentation_sources_contain_mermaid_blocks() -> None:
    """Guard the gate itself: if extraction silently breaks, the suite must fail
    loudly rather than pass by checking nothing."""
    blocks = _all_blocks()
    assert len(blocks) >= 20, (
        f"expected the documentation to contain many mermaid diagrams, found "
        f"{len(blocks)} -- block extraction is probably broken"
    )


def test_every_mermaid_block_passes_the_known_failure_rules() -> None:
    """No documentation source may contain a diagram that fails to render."""
    problems = [problem for block in _all_blocks() for problem in _lint(block)]
    assert not problems, "mermaid defects found:\n  " + "\n  ".join(problems)


def test_rules_catch_the_historical_regressions() -> None:
    """Regression-test the linter against the real defects that motivated it.

    Without this, a refactor could quietly weaken a rule and the gate would keep
    reporting success. Each case below is the actual broken source that shipped.
    """
    # The ';' defect, from the workspace promotion and GitHub-import diagrams.
    semicolon = Block(
        path=REPO_ROOT / "docs" / "example.md",
        start_line=1,
        body=(
            "sequenceDiagram\n"
            "    participant IQ as Import queue\n"
            "    participant DB as CALIBER DB\n"
            "    IQ->>DB: mark revision ready; job succeeded"
        ),
    )
    found = _lint(semicolon)
    assert found, "the ';' rule no longer catches its own historical regression"
    assert "';' in a sequenceDiagram message" in found[0]

    # The unclosed-parallelogram defect, from the SDK client architecture diagram.
    unclosed = Block(
        path=REPO_ROOT / "docs" / "example.md",
        start_line=1,
        body=("flowchart LR\n    ST[Sync transport] --> API[/projects wire API]"),
    )
    found = _lint(unclosed)
    assert found, "the asymmetric-shape rule no longer catches its regression"
    assert "asymmetric" in found[0]

    # And the corrected forms must both pass.
    assert not _lint(
        Block(
            path=REPO_ROOT / "docs" / "example.md",
            start_line=1,
            body=(
                "sequenceDiagram\n"
                "    participant IQ as Import queue\n"
                "    participant DB as CALIBER DB\n"
                "    IQ->>DB: mark revision ready, job succeeded"
            ),
        )
    )
    assert not _lint(
        Block(
            path=REPO_ROOT / "docs" / "example.md",
            start_line=1,
            body=('flowchart LR\n    ST[Sync transport] --> API["/projects wire API"]'),
        )
    )


def test_semicolons_outside_sequence_messages_are_allowed() -> None:
    """``classDef`` and ``style`` statements legitimately end with ';', and a
    ';' in flowchart prose is harmless. The rule must not flag those."""
    assert not _lint(
        Block(
            path=REPO_ROOT / "docs" / "example.md",
            start_line=1,
            body=(
                "flowchart LR\n"
                "    A[Start] --> B[End]\n"
                "    classDef chain fill:#ede9fe,stroke:#7c3aed;\n"
                "    class A,B chain;"
            ),
        )
    )
