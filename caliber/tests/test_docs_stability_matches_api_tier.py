"""A docs page never claims a more stable tier than the API it describes (alpha gap P1-10).

The docs site renders a stability chip from each page's ``stability`` front matter. Pages
about knowledge bases, the object store and the assistant said ``ga`` while the API tags
they describe are ``beta`` or ``internal``, so the chip promised a stability the API does
not. The mapping from page to tag is explicit below, because a docs page does not name
its OpenAPI tag, and a page missing from it is a page nobody has checked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from caliber.routes.openapi import _STABILITY

REPO = Path(__file__).resolve().parents[2]
DOCS = REPO / "docs"

pytestmark = pytest.mark.skipif(not DOCS.exists(), reason="repository docs not present")

#: Higher is more stable. ``experimental`` is a docs-only tier below ``beta``.
_RANK = {"internal": 0, "experimental": 1, "beta": 2, "ga": 3}

#: docs page -> the OpenAPI tags it describes. The page may not be more stable than the
#: least stable tag.
PAGE_TAGS: dict[str, tuple[str, ...]] = {
    "02-prompts/architecture.md": ("prompts",),
    "03-tools/architecture.md": ("tools",),
    "04-skills/architecture.md": ("skills",),
    "05-mcp/architecture.md": ("mcp-servers",),
    "06-workflows/architecture.md": ("workflows",),
    "07-object-store/architecture.md": ("object-store",),
    "08-knowledge-bases/architecture.md": ("knowledge-bases",),
    "09-observability/architecture.md": ("observability",),
    "10-gateways/architecture.md": ("gateway",),
    "11-test-sets/architecture.md": ("eval-datasets",),
    "12-assistant/architecture.md": ("aria",),
    "14-evaluation/architecture.md": ("evaluations",),
    "16-openapi-integrations/architecture.md": ("openapi-integrations",),
    "use/knowledge-bases.md": ("knowledge-bases",),
}


def _stability_of(page: Path) -> str:
    for line in page.read_text(encoding="utf-8").splitlines()[:30]:
        if line.startswith("stability:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"{page} has no stability front matter")


@pytest.mark.parametrize(("relative", "tags"), sorted(PAGE_TAGS.items()))
def test_a_page_is_not_more_stable_than_its_api(relative: str, tags: tuple[str, ...]) -> None:
    page = DOCS / relative
    assert page.exists(), f"{relative} is mapped but missing"
    for tag in tags:
        assert tag in _STABILITY, f"{tag} is not an OpenAPI tag"
    api_floor = min((_STABILITY[tag] for tag in tags), key=lambda tier: _RANK[tier])
    claimed = _stability_of(page)

    assert claimed in _RANK, f"{relative}: unknown stability {claimed!r}"
    assert _RANK[claimed] <= _RANK[api_floor], (
        f"{relative} claims {claimed!r} but its API ({', '.join(tags)}) is {api_floor!r}. "
        "Lower the page's stability, or promote the API tier."
    )


def test_the_object_store_knowledge_base_and_assistant_pages_are_not_ga() -> None:
    """The four pages the gap named, so a regression names them."""
    for relative in (
        "07-object-store/architecture.md",
        "08-knowledge-bases/architecture.md",
        "12-assistant/architecture.md",
        "use/knowledge-bases.md",
    ):
        assert _stability_of(DOCS / relative) != "ga", relative
