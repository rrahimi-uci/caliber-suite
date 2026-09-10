"""Focused tests for `docs-site/generate_rest_api_docs.py`'s
`x-caliber-required-scope` rendering.

`test_docs_generation_contract.py::_generated_rest_api_fragment` only
proves the checked-in Markdown matches what the generator currently
produces -- a wrong formatter could make both the generated Markdown and
the checked-in copy consistently wrong while that contract keeps passing.
These tests import the formatter functions directly and assert their
actual output, independent of the checked-in fragment, for every
`x-caliber-required-scope` `kind` (`routes/scope_inference.py`'s
`ScopeRequirement`), plus malformed/unknown input.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_SITE = REPO_ROOT / "docs-site"
if str(DOCS_SITE) not in sys.path:
    sys.path.insert(0, str(DOCS_SITE))

import generate_rest_api_docs as gen


def test_scope_value_resolves_the_real_constant() -> None:
    assert gen._scope_value("SCOPE_OPERATOR") == "caliber.operator"
    assert gen._scope_value("SCOPE_ADMIN") == "caliber.admin"
    assert gen._scope_value("SCOPE_APPROVER") == "caliber.approver"
    assert gen._scope_value("SCOPE_VIEWER") == "caliber.viewer"


def test_scope_value_falls_back_to_the_raw_name_for_an_unknown_constant() -> None:
    assert gen._scope_value("SCOPE_NOT_REAL") == "SCOPE_NOT_REAL"


def test_single_scope_renders_as_one_code_span() -> None:
    op = {"x-caliber-required-scope": {"kind": "scope", "scopes": ["SCOPE_OPERATOR"]}}
    assert gen._format_required_scope(op) == "`caliber.operator`"


def test_multiple_scopes_render_with_explicit_or_semantics() -> None:
    """require_scopes() grants access on ANY of the listed scopes
    (auth.py's `required.isdisjoint(granted)` check) -- a comma join would
    misleadingly read as "all of these are required"."""
    op = {
        "x-caliber-required-scope": {
            "kind": "scope",
            "scopes": ["SCOPE_ADMIN", "SCOPE_OPERATOR"],
        }
    }
    rendered = gen._format_required_scope(op)
    assert rendered == "one of `caliber.admin` or `caliber.operator`"


def test_authenticated_kind_renders_plainly() -> None:
    op = {"x-caliber-required-scope": {"kind": "authenticated"}}
    assert gen._format_required_scope(op) == "any authenticated user"


def test_project_role_kind_includes_the_action() -> None:
    op = {"x-caliber-required-scope": {"kind": "project_role", "action": "resource.write"}}
    assert gen._format_required_scope(op) == "project role (`resource.write`)"


def test_project_role_kind_without_an_action_still_renders() -> None:
    op = {"x-caliber-required-scope": {"kind": "project_role"}}
    assert gen._format_required_scope(op) == "project role"


def test_dynamic_kind_includes_its_note() -> None:
    op = {
        "x-caliber-required-scope": {
            "kind": "dynamic",
            "note": "SCOPE_ADMIN if 'status' in changes else SCOPE_OPERATOR",
        }
    }
    assert (
        gen._format_required_scope(op)
        == "dynamic — SCOPE_ADMIN if 'status' in changes else SCOPE_OPERATOR"
    )


def test_dynamic_kind_without_a_note_still_renders() -> None:
    op = {"x-caliber-required-scope": {"kind": "dynamic"}}
    assert gen._format_required_scope(op) == "dynamic"


def test_public_kind_includes_its_note() -> None:
    op = {
        "x-caliber-required-scope": {
            "kind": "public",
            "note": "Liveness probe; load balancers call it pre-auth.",
        }
    }
    assert (
        gen._format_required_scope(op)
        == "public — Liveness probe; load balancers call it pre-auth."
    )


def test_missing_field_renders_an_em_dash() -> None:
    assert gen._format_required_scope({}) == "—"


def test_non_dict_requirement_renders_an_em_dash() -> None:
    assert gen._format_required_scope({"x-caliber-required-scope": "not-a-dict"}) == "—"
    assert gen._format_required_scope({"x-caliber-required-scope": None}) == "—"


def test_unknown_kind_renders_an_em_dash() -> None:
    op = {"x-caliber-required-scope": {"kind": "something-new"}}
    assert gen._format_required_scope(op) == "—"


def test_scope_kind_with_no_scopes_list_renders_an_em_dash() -> None:
    """Malformed input (the "scope" kind is supposed to always carry a
    non-empty `scopes` list per scope_inference.py) degrades to "—" rather
    than raising."""
    assert gen._format_required_scope({"x-caliber-required-scope": {"kind": "scope"}}) == "—"
    op = {"x-caliber-required-scope": {"kind": "scope", "scopes": "not-a-list"}}
    assert gen._format_required_scope(op) == "—"
