"""Per-tag SDK coverage in the published REST inventory must subtract
permanently-excluded operations from the denominator.

``generate_rest_api_docs.py`` renders one "SDK coverage" cell per route tag.
Before this test existed, the denominator was the tag's raw operation count,
which includes operations ``coverage_allowlist.toml`` marks ``[[exclusion]]``
-- permanently out of SDK scope by design (a browser-cookie-only login flow,
a Prometheus scrape target, ...). Two tags carry such an operation alongside
otherwise fully-typed ones: ``auth`` (``POST /auth/login``,
``POST /auth/logout``) and ``observability`` (the allure-report HTML proxy,
two paths). Both rendered "Partial (N/M)" forever, even once every
*addressable* operation in them had a typed method -- because M counted
operations that can never gain one. See ``sdk-completeness-plan.md`` §2.8 for
the exclusion list's rationale.

This module tests the fix directly against ``generate_rest_api_docs.py``'s
own functions (not a subprocess), and pins the two real-world tags that
exposed the bug so a regression is caught even if the allowlist changes.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_SITE = REPO_ROOT / "docs-site"

# Both modules under test import from here; see test_sdk_api_coverage.py's
# docstring for why the measurement lives outside both packages.
sys.path.insert(0, str(DOCS_SITE))
import generate_rest_api_docs as gen


def test_load_excluded_ops_matches_the_allowlists_exclusion_section() -> None:
    """Sanity check on the loader itself: every permanent exclusion in
    ``coverage_allowlist.toml`` shows up, normalized, and nothing else does
    (a ``[[gap]]`` entry must NOT be treated as excluded -- a gap is
    addressable, temporary debt that should keep counting against a tag)."""
    excluded = gen._load_excluded_ops()
    assert ("POST", "/auth/login") in excluded
    assert ("POST", "/auth/logout") in excluded
    assert ("GET", "/csrf") in excluded
    assert ("GET", "/metrics") in excluded
    assert ("GET", "/observability/allure-report") in excluded
    assert ("GET", "/observability/allure-report/{}") in excluded
    # A route this suite knows is covered (not excluded) must not appear.
    assert ("GET", "/me") not in excluded


def _entry(method: str, path: str) -> tuple[str, str, dict[str, object]]:
    return (f"{gen.PREFIX}{path}", method, {})


def test_sdk_coverage_counts_drops_excluded_ops_from_both_covered_and_total() -> None:
    """The bug, reproduced directly: a tag with one covered op and one
    permanently-excluded op used to report 1/2 ("Partial"). It must report
    1/1 ("fully addressable") once the exclusion is subtracted."""
    entries = [_entry("GET", "/widgets"), _entry("POST", "/auth/login")]
    covered_ops = {("GET", "/widgets")}
    excluded_ops = {("POST", "/auth/login")}

    covered, total = gen._sdk_coverage_counts(entries, covered_ops, excluded_ops)

    assert (covered, total) == (1, 1)


def test_sdk_coverage_counts_with_no_exclusions_is_unchanged() -> None:
    """An empty exclusion set must reproduce the pre-fix arithmetic exactly
    -- this fix must not touch tags that carry no excluded operation."""
    entries = [_entry("GET", "/widgets"), _entry("POST", "/widgets")]
    covered_ops = {("GET", "/widgets")}

    covered, total = gen._sdk_coverage_counts(entries, covered_ops, excluded_ops=set())

    assert (covered, total) == (1, 2)


def test_sdk_surface_row_reports_typed_sdk_when_every_addressable_op_is_covered() -> None:
    label, _entry_text, _note = gen._sdk_surface_row("auth", "ga", covered_count=9, total_count=9)
    assert label == "Typed SDK"


def test_sdk_surface_row_na_message_is_exclusion_aware_not_generic() -> None:
    """``total_count == 0`` can only happen when every operation in the tag
    is permanently excluded (a tag with zero live operations would never
    reach this function at all -- it would not exist in ``grouped``). The
    message must say so, not reuse the "not documented yet" phrasing meant
    for addressable-but-unmapped tags, which wrongly implies future
    coverage is possible."""
    label, entry_text, note = gen._sdk_surface_row("csrf", "ga", covered_count=0, total_count=0)
    assert label == "n/a"
    assert entry_text == "`client.raw`"
    assert "permanently outside SDK scope" in note
    assert "yet" not in note


def test_auth_and_observability_tags_render_typed_sdk_in_the_full_inventory() -> None:
    """End-to-end regression pin for the exact tags that motivated this fix.
    Runs the real generator against the live route table -- if either tag
    ever grows a genuinely-uncovered addressable operation, this correctly
    fails again (as "Partial", not silently)."""
    rendered = gen.render_inventory()
    lines = {
        line.split("|")[1].strip(): line for line in rendered.splitlines() if line.startswith("| ")
    }

    auth_line = next((line for key, line in lines.items() if key.startswith("Auth (")), None)
    assert auth_line is not None, "auth tag row not found in rendered inventory"
    assert "Partial" not in auth_line, f"auth tag regressed to Partial coverage: {auth_line}"

    observability_line = next(
        (line for key, line in lines.items() if key.startswith("Observability (")), None
    )
    assert observability_line is not None, "observability tag row not found in rendered inventory"
    assert "Partial" not in observability_line, (
        f"observability tag regressed to Partial coverage: {observability_line}"
    )
