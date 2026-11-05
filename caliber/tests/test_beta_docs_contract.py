"""Contract checks for the B15.5 Beta documentation package."""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1].parent
DOCS = REPO / "docs"
BUILD = REPO / "docs-site" / "build-docs.mjs"
SITE_MAP = REPO / "docs-site" / "docs-site-map.json"


def _read(relative: str) -> str:
    return (DOCS / relative).read_text(encoding="utf-8")


def test_beta_pages_are_registered_with_unique_outputs() -> None:
    build = BUILD.read_text(encoding="utf-8")
    site_map = json.loads(SITE_MAP.read_text(encoding="utf-8"))
    expected = {
        "operate/intervention-guide.md": "m-67-beta-intervention-guide.html",
        "reports/beta/beta-feature-coverage-matrix.md": "m-68-beta-feature-coverage-matrix.html",
    }
    for source, output in expected.items():
        assert f'md: "{source}", out: "{output}"' in build
        assert any(page["href"] == output for page in site_map["pages"])
    outputs = [page["href"] for page in site_map["pages"]]
    assert len(outputs) == len(set(outputs))


def test_intervention_guide_names_each_supported_caller_and_failure_boundary() -> None:
    text = _read("operate/intervention-guide.md")
    for phrase in (
        "## 1. Inspect a run that is waiting for approval",
        "## 2. Reject an unsafe or stale approval",
        "## 3. Approve the exact proposal as a distinct person",
        "## 4. Resume and verify the accepted outcome",
        "## 5. Read and verify the evidence package",
        "## 6. Replay without another mutation",
        "caliber.workflows.runs.approvals",
        "caliberctl workflow run replay",
        "matches_original",
        "403 project_role_required",
        "410",
        "request_changes",
    ):
        assert phrase in text
    assert "ProposalsAPI" in text
    assert "no standalone `ProposalsAPI`" in text


def test_beta_scope_and_matrix_preserve_non_claims() -> None:
    scope = _read("start/beta-scope.md")
    matrix = _read("reports/beta/beta-feature-coverage-matrix.md")
    for text in (scope, matrix):
        assert "synthetic" in text.lower()
        assert "real-provider" in text
        assert "independent" in text.lower()
        assert "#613" in text
    assert "does not announce a release" in scope
    assert "not a release decision" not in matrix
    assert "D5-acceptance.md" in matrix


def test_architecture_guide_links_the_beta_package() -> None:
    text = _read("architecture/index.md")
    assert "## 6. For the Beta control spine" in text
    assert "../start/beta-scope.md" in text
    assert "../reports/beta/beta-feature-coverage-matrix.md" in text
    assert "../operate/intervention-guide.md" in text
    assert "ProposalsAPI" in text and "ApprovalsAPI" in text
