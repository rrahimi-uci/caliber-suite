"""The frontend's fallback template list must match the backend catalog.

``Workflows.tsx`` ships ``FALLBACK_TEMPLATES``, rendered whenever the catalog
query fails — an offline dev server, a 500, a network blip. Because the fallback
is silent, a template added, removed, or renamed on the server produces a UI that
looks correct and offers a different set, and nothing fails until a user picks a
kind the backend no longer builds.

The two lists agree today. This test is what keeps them agreeing: it is cheap,
it fails loudly at the moment of divergence, and it names which side moved.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from caliber.workflows.template_catalog import build_workflow_template_catalog

_WORKFLOWS_TSX = (
    Path(__file__).resolve().parents[1] / "caliber-ui" / "src" / "pages" / "Workflows.tsx"
)
_FALLBACK_START = "const FALLBACK_TEMPLATES: WorkflowTemplate[] = ["
_ENTRY = re.compile(r'\{\s*kind:\s*"([a-z0-9_]+)",\s*\n\s*label:\s*"([^"]+)"', re.MULTILINE)


def _fallback_entries() -> list[tuple[str, str]]:
    """Extract ``(kind, label)`` from the fallback array, and only that array."""
    source = _WORKFLOWS_TSX.read_text(encoding="utf-8")
    start = source.index(_FALLBACK_START)
    # Stop at the array's closing bracket at column 0, so a later `kind:` in the
    # component body cannot be mistaken for a template entry.
    end = source.index("\n];", start)
    return _ENTRY.findall(source[start:end])


@pytest.mark.skipif(not _WORKFLOWS_TSX.exists(), reason="SPA sources not present in this checkout")
def test_frontend_fallback_templates_match_the_backend_catalog() -> None:
    backend = build_workflow_template_catalog()["templates"]
    backend_pairs = [(item["kind"], item["label"]) for item in backend]
    fallback_pairs = _fallback_entries()

    assert fallback_pairs, "no fallback templates parsed — the array shape changed"

    backend_kinds = [kind for kind, _ in backend_pairs]
    fallback_kinds = [kind for kind, _ in fallback_pairs]
    missing = set(backend_kinds) - set(fallback_kinds)
    extra = set(fallback_kinds) - set(backend_kinds)
    assert not missing, f"backend templates absent from the SPA fallback: {sorted(missing)}"
    assert not extra, f"SPA fallback offers templates the backend cannot build: {sorted(extra)}"

    # Order matters: the fallback renders in array order, so a reordered backend
    # catalog would silently present a different gallery when the query fails.
    assert fallback_kinds == backend_kinds, (
        "template order differs between SPA fallback and backend"
    )

    mismatched = [
        (kind, backend_label, fallback_label)
        for (kind, backend_label), (_, fallback_label) in zip(
            backend_pairs, fallback_pairs, strict=True
        )
        if backend_label != fallback_label
    ]
    assert not mismatched, f"labels differ (kind, backend, spa): {mismatched}"
