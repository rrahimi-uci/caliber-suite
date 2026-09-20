"""Tests for the repo-wide bare-lookup regression checker (`P2-Q` follow-up).

Two kinds of coverage, mirroring `test_route_scope_inventory.py`'s own split:

* Unit tests on the AST walker itself (`bare_lookup_inventory`/`_classify_call`
  and friends), using small synthetic functions -- cheaper and more precise
  for edge cases than only testing against real route modules.
* An enforcement gate against the live route tree: every
  `session.get(<SCOPING_VISIBILITY model>, ...)` call site found today must
  resolve to a non-"unclassified" kind, and the `_REVIEWED_BARE_LOOKUPS`
  allowlist must contain no stale entries (a reviewed key with no matching
  live finding). This is the actual regression gate `P2-Q`'s own row named
  as a follow-up: a *new* bare lookup on one of these 15 models will either
  auto-classify as one of the four recognized safe shapes, or show up as
  "unclassified" and fail `test_no_unclassified_bare_lookups` by name until
  it's fixed or explicitly, visibly allowlisted with a reason.
"""

from __future__ import annotations

import ast
import textwrap

from caliber.routes.bare_lookup_inventory import (
    _REVIEWED_BARE_LOOKUPS,
    BareLookupFinding,
    _classify_call,
    bare_lookup_inventory,
)

# ---------------------------------------------------------------------------
# AST-walker unit tests
# ---------------------------------------------------------------------------


def _classify_source(source: str, *, model: str = "CaliberSkill") -> tuple[str, str | None]:
    """Parse `source` (a single top-level function definition), find its one
    `session.get(<model>, ...)` call, and classify it."""
    tree = ast.parse(textwrap.dedent(source))
    function = tree.body[0]
    assert isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    calls = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) == "get"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == model
    ]
    assert len(calls) == 1, (
        f"expected exactly one session.get({model}, ...) call, found {len(calls)}"
    )
    return _classify_call(calls[0], model=model, function=function, parents=parents)


def test_bare_lookup_alongside_get_visible_same_pk_is_gated() -> None:
    kind, note = _classify_source(
        """
        def handler(session, skill_id, identity):
            skill = (
                get_visible(session, CaliberSkill, CaliberSkill.skill_id, skill_id, identity)
                if identity is not None
                else session.get(CaliberSkill, skill_id)
            )
            return skill
        """
    )
    assert kind == "gated"
    assert note is not None


def test_bare_lookup_with_different_pk_than_get_visible_is_not_gated() -> None:
    """The `gated` rule requires the *same* primary-key expression -- a
    `get_visible` check on a different id in the same function must not
    clear an unrelated bare lookup."""
    kind, _note = _classify_source(
        """
        def handler(session, skill_id, other_id, identity):
            checked = get_visible(session, CaliberSkill, CaliberSkill.skill_id, other_id, identity)
            skill = session.get(CaliberSkill, skill_id)
            return checked, skill
        """
    )
    assert kind == "unclassified"


def test_bare_lookup_with_apply_visibility_filter_same_model_is_gated() -> None:
    kind, _note = _classify_source(
        """
        def handler(session, skill_id, identity, project_id):
            stmt = apply_visibility_filter(select(CaliberSkill), CaliberSkill, identity, project_id)
            rows = session.execute(stmt).scalars().all()
            skill = session.get(CaliberSkill, skill_id)
            return rows, skill
        """
    )
    assert kind == "gated"


def test_bare_lookup_in_admin_only_route_is_admin_only() -> None:
    kind, note = _classify_source(
        """
        async def handler(request):
            actor = require_scopes(request, [SCOPE_ADMIN])
            skill = session.get(CaliberSkill, skill_id)
            return skill
        """
    )
    assert kind == "admin_only"
    assert note is not None


def test_bare_lookup_in_multi_scope_route_is_not_admin_only() -> None:
    """A scope list that merely *includes* SCOPE_ADMIN alongside others does
    not qualify -- a non-admin caller with one of the other scopes could
    still reach the lookup."""
    kind, _note = _classify_source(
        """
        async def handler(request):
            actor = require_scopes(request, [SCOPE_ADMIN, SCOPE_OPERATOR])
            skill = session.get(CaliberSkill, skill_id)
            return skill
        """
    )
    assert kind == "unclassified"


def test_bare_lookup_with_require_all_scopes_admin_only_is_admin_only() -> None:
    kind, _note = _classify_source(
        """
        async def handler(request):
            actor = require_all_scopes(request, [SCOPE_ADMIN])
            skill = session.get(CaliberSkill, skill_id)
            return skill
        """
    )
    assert kind == "admin_only"


def test_bare_lookup_role_gated_on_own_project_id_direct() -> None:
    kind, note = _classify_source(
        """
        def handler(session, skill_id, identity):
            skill = session.get(CaliberSkill, skill_id)
            require_project_access_if_scoped(session, identity, skill.project_id, "resource.write")
            return skill
        """
    )
    assert kind == "role_gated_own_project"
    assert note is not None


def test_bare_lookup_role_gated_on_own_project_id_inside_ternary() -> None:
    """`routes/prompts.py`'s hidden-target shape: the project id passed to
    the project-access call is wrapped in a ternary, not a bare attribute
    access."""
    kind, _note = _classify_source(
        """
        def handler(session, name, identity):
            existing = session.get(CaliberSkill, name)
            require_project_access_if_scoped(
                session,
                identity,
                existing.project_id if existing is not None else identity.active_project_id,
                "resource.write",
            )
            return existing
        """
    )
    assert kind == "role_gated_own_project"


def test_bare_lookup_role_gated_on_a_different_rows_project_id_is_not_cleared() -> None:
    kind, _note = _classify_source(
        """
        def handler(session, skill_id, other, identity):
            skill = session.get(CaliberSkill, skill_id)
            require_project_access_if_scoped(session, identity, other.project_id, "resource.write")
            return skill
        """
    )
    assert kind == "unclassified"


def test_bare_lookup_used_only_for_existence_check_not_assigned() -> None:
    kind, note = _classify_source(
        """
        def handler(session, skill_id):
            if session.get(CaliberSkill, skill_id) is not None:
                raise ValueError("already exists")
        """
    )
    assert kind == "existence_only"
    assert note is not None


def test_bare_lookup_used_only_for_existence_check_when_assigned() -> None:
    kind, _note = _classify_source(
        """
        def handler(session, skill_id):
            skill = session.get(CaliberSkill, skill_id)
            if skill is None:
                raise ValueError("not found")
        """
    )
    assert kind == "existence_only"


def test_bare_lookup_assigned_and_attribute_accessed_is_not_existence_only() -> None:
    kind, _note = _classify_source(
        """
        def handler(session, skill_id):
            skill = session.get(CaliberSkill, skill_id)
            if skill is None:
                raise ValueError("not found")
            return skill.name
        """
    )
    assert kind == "unclassified"


def test_bare_lookup_with_no_gating_at_all_is_unclassified() -> None:
    kind, note = _classify_source(
        """
        def handler(session, skill_id):
            skill = session.get(CaliberSkill, skill_id)
            return skill
        """
    )
    assert kind == "unclassified"
    assert note is None


# ---------------------------------------------------------------------------
# Enforcement gate against the live route tree
# ---------------------------------------------------------------------------


def test_no_unclassified_bare_lookups() -> None:
    """The actual regression gate: every bare lookup on a `SCOPING_VISIBILITY`
    model must resolve to a definite, explained kind -- either one of this
    module's four auto-detected safe shapes, or a human-reviewed
    `_REVIEWED_BARE_LOOKUPS` entry. A future new bare lookup that matches
    none of those fails here by name, the same way an unexplained
    "dynamic"/"public" route fails `test_route_scope_inventory.py`.
    """
    findings = bare_lookup_inventory()
    unclassified = [f for f in findings if f.kind == "unclassified"]
    assert not unclassified, "\n".join(
        f"{f.module}.{f.qualname}:{f.lineno} -> bare session.get({f.model}, ...) with no "
        f"recognized gate and no _REVIEWED_BARE_LOOKUPS entry (add one to "
        f"caliber/routes/bare_lookup_inventory.py, or add the missing visibility check)"
        for f in unclassified
    )


def test_reviewed_allowlist_entries_are_all_still_live() -> None:
    """A stale allowlist entry (one whose call site moved or was fixed) is a
    claim that no longer means anything -- the same staleness
    `test_route_scope_inventory.py::test_stale_notes_do_not_linger` guards
    against for `_DYNAMIC_SCOPE_NOTES`/`_PUBLIC_ROUTES`."""
    live_keys = {f.key for f in bare_lookup_inventory() if f.kind == "reviewed"}
    stale = sorted(set(_REVIEWED_BARE_LOOKUPS) - live_keys)
    assert not stale, (
        f"_REVIEWED_BARE_LOOKUPS names call site(s) that no longer exist or no longer "
        f"need review: {stale}"
    )


def test_every_reviewed_finding_has_an_allowlist_entry() -> None:
    """The inverse of the staleness check: every finding this checker itself
    could not auto-classify must actually be recorded, not just silently
    dropped by a bug in the reviewed-lookup wiring."""
    findings = bare_lookup_inventory()
    for finding in findings:
        if finding.kind == "reviewed":
            assert finding.key in _REVIEWED_BARE_LOOKUPS
            assert _REVIEWED_BARE_LOOKUPS[finding.key] == finding.note


def test_bare_lookup_call_site_count_is_pinned() -> None:
    """Ratchet, matching `test_resource_inventory.py`'s `_EXPECTED_COUNTS`
    style: today's exact distribution across the five kinds, confirmed by
    direct reading of every one of the 24 call sites while building this
    checker (see `docs/workspace-plan.md`'s `P2-Q` row and this module's own
    `_REVIEWED_BARE_LOOKUPS` comments). The point isn't that this count is a
    law -- it's that a change gets *noticed and looked at*, not silently
    absorbed into a bigger or smaller total kind-by-kind without anyone
    confirming it's still safe.

    `admin_only` -> `gated` for `verification.create_verification_item_record`
    reflects `P2-Q` slice 2 (#417), landed on `main` after this checker was
    first written: its `identity is not None` branch now routes through
    `get_visible` instead of a bare admin-gated lookup.
    """
    from collections import Counter

    findings = bare_lookup_inventory()
    counts = Counter(f.kind for f in findings)
    assert counts == {
        "gated": 9,
        "admin_only": 6,
        "existence_only": 3,
        "role_gated_own_project": 1,
        "reviewed": 5,
    }
    assert len(findings) == 24


def test_findings_only_cover_scoping_visibility_models() -> None:
    from caliber.db.resource_inventory import SCOPING_VISIBILITY, resource_inventory

    visibility_models = {d.name for d in resource_inventory() if d.scoping == SCOPING_VISIBILITY}
    findings = bare_lookup_inventory()
    assert findings, "sanity: the walk actually found real call sites"
    for finding in findings:
        assert finding.model in visibility_models


def test_bare_lookup_finding_key_is_stable_identity() -> None:
    finding = BareLookupFinding(
        module="caliber.routes.skills",
        qualname="update_skill",
        lineno=512,
        model="CaliberSkill",
        kind="admin_only",
    )
    assert finding.key == "caliber.routes.skills.update_skill:512"
