"""The ten design principles say the same thing everywhere they appear.

The principles are restated on four surfaces with different audiences: the
canonical statement in ``ARCHITECTURE.md``, the README, the published landing
page, and the paper. Restating them is deliberate --- a reader of the paper
should not have to open the repository --- but restatement is exactly how a
principle set rots: one surface gains an eleventh principle, another reorders
two, and the set stops being a design constraint and becomes four opinions.

These tests pin the titles and their order to the canonical list. The *order*
matters as much as the membership, because the documented tie-break rule is
"the lower number wins" -- a reordering silently changes which principle
yields.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ARCHITECTURE = REPO_ROOT / "ARCHITECTURE.md"
README = REPO_ROOT / "README.md"
LANDING_PAGE = REPO_ROOT / "docs-site" / "index.html"
PAPER_OVERVIEW = REPO_ROOT / "paper" / "sections" / "04-system-overview.tex"

#: The canonical ten, in order. Spelled out here rather than parsed from
#: ARCHITECTURE.md so that a change to the canonical list is a deliberate edit
#: in two places, not something a stray find-and-replace can carry everywhere at
#: once without anyone noticing.
PRINCIPLES = (
    "Governed Agentic Workflows",
    "Progressive Autonomy",
    "Auditability & Observability",
    "Evaluation by Design",
    "Open & Extensible",
    "Modular & Composable",
    "Scalable & Reliable",
    "Open-Source First",
    "Developer & User Friendly",
    "Future Ready",
)


def _ordered_hits(haystack: str, needles: tuple[str, ...]) -> list[str]:
    """The needles that appear, in the order they first appear."""
    found = [(haystack.index(needle), needle) for needle in needles if needle in haystack]
    return [needle for _, needle in sorted(found)]


def test_the_canonical_statement_lists_all_ten_in_order() -> None:
    text = ARCHITECTURE.read_text(encoding="utf-8")
    assert "## Design principles" in text, "ARCHITECTURE.md lost its Design principles section"
    assert _ordered_hits(text, PRINCIPLES) == list(PRINCIPLES)


def test_the_readme_restates_all_ten_in_order() -> None:
    assert _ordered_hits(README.read_text(encoding="utf-8"), PRINCIPLES) == list(PRINCIPLES)


def test_the_landing_page_restates_all_ten_in_order() -> None:
    # The page is HTML, so ampersands are entity-escaped.
    text = LANDING_PAGE.read_text(encoding="utf-8").replace("&amp;", "&")
    assert 'id="principles"' in text, "the landing page lost its principles section"
    assert _ordered_hits(text, PRINCIPLES) == list(PRINCIPLES)


#: The paper renders the same ten in prose style -- sentence case, conjunctions
#: spelled out. Listed explicitly rather than derived, because deriving them was
#: what hid a missing principle: a head-word rule matched "Open" against
#: principle 5 and then failed to find "Open-Source" at all, leaving nine.
PAPER_TITLES = (
    "Governed agentic workflows",
    "Progressive autonomy",
    "Auditability and observability",
    "Evaluation by design",
    "Open and extensible",
    "Modular and composable",
    "Scalable and reliable",
    "Open-source first",
    "Developer and user friendly",
    "Future ready",
)


def test_the_paper_restates_all_ten_in_order() -> None:
    assert len(PAPER_TITLES) == len(PRINCIPLES), (
        "the paper's rendering must stay 1:1 with the canonical list"
    )
    text = PAPER_OVERVIEW.read_text(encoding="utf-8")
    assert r"\label{sec:principles}" in text, "the paper lost its principles subsection"
    assert _ordered_hits(text, PAPER_TITLES) == list(PAPER_TITLES)


def test_no_surface_invents_an_eleventh_principle() -> None:
    """The canonical section defines exactly ten numbered rows.

    Counting guards the failure this whole file exists for: a principle added to
    one surface and nowhere else. The other tests would still pass, because they
    only assert the known ten appear in order.
    """
    text = ARCHITECTURE.read_text(encoding="utf-8")
    section = text.split("## Design principles", 1)[1].split("## 1 ·", 1)[0]
    numbered = re.findall(r"^\| \*\*(\d+)\*\* \|", section, flags=re.MULTILINE)
    assert numbered == [str(n) for n in range(1, 11)], (
        f"the canonical table should number 1..10 exactly once each; got {numbered}"
    )


def test_the_tie_break_rule_is_stated_wherever_the_order_is_shown() -> None:
    """An ordered list without its tie-break rule is just a list.

    The order is the load-bearing part of this principle set, so every surface
    that shows it also has to say what the order is *for*.
    """
    for path in (ARCHITECTURE, README, LANDING_PAGE, PAPER_OVERVIEW):
        text = path.read_text(encoding="utf-8")
        assert "lower" in text and "wins" in text, (
            f"{path.name} shows the ordered principles without stating the tie-break rule"
        )
