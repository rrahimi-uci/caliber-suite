"""Layout guards for the published documentation.

These assert two CSS decisions that are easy to undo by accident and whose
failure is invisible in the source: both bugs below rendered perfectly, they just
rendered wrong. The rendered behaviour was verified with a headless browser
across 80 pages at three viewport widths; these tests read the stylesheet so they
cost nothing and need no browser in CI.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_SITE = REPO_ROOT / "docs-site"


def test_table_cells_never_use_overflow_wrap_anywhere() -> None:
    """``anywhere`` and ``break-word`` are not interchangeable inside a table.

    ``overflow-wrap: anywhere`` *reduces* an element's min-content width, and auto
    table layout sizes columns from exactly that — so applying it to a cell lets
    the browser shrink a column below its longest word and stack that word one
    character per line. Measured before this was fixed: a six-column table whose
    true min-content width was 1034px rendered at 778px, and a cookbook label
    ``Instructions`` rendered in a 32px column, 43 lines tall.

    ``break-word`` still rescues a genuinely unbreakable token from overflowing,
    without licensing the collapse. This test reads the stylesheet rather than the
    rendered page so it costs nothing and runs everywhere; the rendered behaviour
    was verified separately with a headless browser.
    """
    css = (DOCS_SITE / "docs.css").read_text(encoding="utf-8")

    # Strip comments so prose explaining the rule cannot trip its own check.
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    offenders: list[str] = []
    for block in re.findall(r"([^{}]+)\{([^{}]*)\}", stripped):
        selector, body = block[0].strip(), block[1]
        if "overflow-wrap:" not in body.replace(" ", ""):
            continue
        if "anywhere" not in body:
            continue
        # A selector that targets table cells must not use `anywhere`.
        if re.search(r"\b(td|th)\b", selector):
            offenders.append(" ".join(selector.split())[:120])

    assert not offenders, (
        "these selectors apply `overflow-wrap: anywhere` to table cells, which "
        f"collapses column widths: {offenders}"
    )


def test_every_table_has_somewhere_to_scroll() -> None:
    """A table at its natural width needs a scroll container, or the page scrolls.

    Generated tables sit inside ``.table-wrap``, which scrolls. Hand-authored
    pages have bare ``<table>`` elements that do not — and once cells stopped
    being crushed, one of those pushed ``walkthrough.html`` into 38px of
    horizontal *page* scroll at 390px wide. The stylesheet carries a rule making
    any unwrapped table its own scroll container; this asserts it stays.
    """
    css = (DOCS_SITE / "docs.css").read_text(encoding="utf-8")
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    rule = re.search(r"table:not\(\.table-wrap\s*>\s*table\)\s*\{([^}]*)\}", stripped)
    assert rule, "the fallback scroll rule for unwrapped tables is gone"
    body = rule.group(1).replace(" ", "")
    assert "overflow-x:auto" in body, "unwrapped tables must scroll themselves"
    assert "display:block" in body, (
        "the table needs to be its own block-level scroll container for overflow-x to take effect"
    )
