"""Tests for the machine-readable route required-scope inventory (`P0-A`).

Two kinds of coverage:

* Unit tests on the AST walker itself (`infer_required_scope`), using small
  synthetic functions -- cheaper and more precise for edge cases than only
  testing against real handlers.
* An enforcement gate against the live route table, mirroring
  `test_routes_openapi.py::test_every_tag_has_a_declared_stability_tier`'s
  shape: every route must resolve to a definite classification, and a
  "dynamic"/"public" classification with no matching note in
  `_DYNAMIC_SCOPE_NOTES`/`_PUBLIC_ROUTES` fails by name. This is the test
  that would have caught `review_queues.py::submit_item`'s missing scope
  check (fixed in PR #279) automatically, had it existed then.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.routing import Route
from starlette.testclient import TestClient

from caliber.routes.openapi import PREFIX
from caliber.routes.scope_inference import (
    _DYNAMIC_SCOPE_NOTES,
    _PUBLIC_ROUTES,
    ScopeRequirement,
    _qualified_name,
    _unwrap_endpoint,
    infer_required_scope,
    serialize_scope_requirement,
)

OPENAPI_URL = PREFIX + "/openapi.json"


def _live_operations(app) -> list[tuple[str, str, object]]:
    """(method, path, endpoint) for every management-API operation."""
    live: list[tuple[str, str, object]] = []
    for route in app.routes:
        if not isinstance(route, Route) or not route.path.startswith(PREFIX):
            continue
        for method in sorted((route.methods or set()) - {"HEAD", "OPTIONS"}):
            live.append((method, route.path, route.endpoint))
    return live


# ---------------------------------------------------------------------------
# AST-walker unit tests
# ---------------------------------------------------------------------------


async def _single_scope(request: Request) -> None:
    from caliber.auth import SCOPE_OPERATOR, require_scopes

    require_scopes(request, [SCOPE_OPERATOR])


async def _two_scopes_one_call(request: Request) -> None:
    from caliber.auth import SCOPE_ADMIN, SCOPE_OPERATOR, require_scopes

    require_scopes(request, [SCOPE_OPERATOR, SCOPE_ADMIN])


async def _two_sequential_calls(request: Request) -> None:
    from caliber.auth import SCOPE_ADMIN, SCOPE_OPERATOR, require_scopes

    require_scopes(request, [SCOPE_OPERATOR])
    require_scopes(request, [SCOPE_ADMIN])


async def _authenticated_only(request: Request) -> None:
    from caliber.auth import require_user

    require_user(request)


async def _dynamic_scope(request: Request) -> None:
    from caliber.auth import SCOPE_ADMIN, SCOPE_OPERATOR, require_scopes

    computed = SCOPE_ADMIN if request.query_params.get("x") else SCOPE_OPERATOR
    require_scopes(request, [computed])


async def _no_check_at_all(request: Request) -> None:
    return None


async def _project_role_default_action(request: Request, session, identity, project_id):
    from caliber.resource_access import require_project_access

    require_project_access(session, identity, project_id)


async def _project_role_explicit_action(request: Request, session, identity, project_id):
    from caliber.resource_access import require_project_access

    require_project_access(session, identity, project_id, "resource.write")


def test_single_static_scope_is_classified_as_scope() -> None:
    result = infer_required_scope(_single_scope)
    assert result.kind == "scope"
    assert result.scopes == frozenset({"SCOPE_OPERATOR"})


def test_multiple_scopes_in_one_call_are_all_captured() -> None:
    result = infer_required_scope(_two_scopes_one_call)
    assert result.kind == "scope"
    assert result.scopes == frozenset({"SCOPE_OPERATOR", "SCOPE_ADMIN"})


def test_two_sequential_calls_union_their_scopes() -> None:
    """Two calls in one handler is a conjunction in effect (both must pass to
    reach the code after them), but this module records the union of what
    each call independently accepts, not a conjunction requirement -- there
    is no real handler with two calls today, so this pins the current
    (additive) behavior rather than asserting a specific policy."""
    result = infer_required_scope(_two_sequential_calls)
    assert result.kind == "scope"
    assert result.scopes == frozenset({"SCOPE_OPERATOR", "SCOPE_ADMIN"})


def test_require_user_only_is_authenticated() -> None:
    result = infer_required_scope(_authenticated_only)
    assert result == ScopeRequirement(kind="authenticated")


def test_non_literal_scope_argument_is_dynamic() -> None:
    result = infer_required_scope(_dynamic_scope)
    assert result.kind == "dynamic"
    assert result.scopes == frozenset()


def test_no_authorization_call_is_public() -> None:
    result = infer_required_scope(_no_check_at_all)
    assert result.kind == "public"


def test_project_access_with_omitted_action_resolves_the_default() -> None:
    result = infer_required_scope(_project_role_default_action)
    assert result.kind == "project_role"
    assert result.action == "read"


def test_project_access_with_explicit_action_captures_it() -> None:
    result = infer_required_scope(_project_role_explicit_action)
    assert result.kind == "project_role"
    assert result.action == "resource.write"


def test_serialize_omits_empty_fields() -> None:
    assert serialize_scope_requirement(ScopeRequirement(kind="authenticated")) == {
        "kind": "authenticated"
    }
    assert serialize_scope_requirement(
        ScopeRequirement(kind="scope", scopes=frozenset({"SCOPE_ADMIN"}))
    ) == {"kind": "scope", "scopes": ["SCOPE_ADMIN"]}
    assert serialize_scope_requirement(ScopeRequirement(kind="project_role", action="read")) == {
        "kind": "project_role",
        "action": "read",
    }


# ---------------------------------------------------------------------------
# Enforcement gate against the live route table
# ---------------------------------------------------------------------------


def test_every_live_route_has_a_definite_scope_classification(client: TestClient) -> None:
    """No route may go uninventoried.

    A "dynamic" or "public" classification is only acceptable when it's
    backed by a reviewed note explaining why -- otherwise this fails by
    name, the same way an unclassified OpenAPI tag fails
    `test_every_tag_has_a_declared_stability_tier`. This is what would have
    caught `review_queues.py::submit_item`'s missing scope check
    automatically before it shipped.
    """
    app = client.app
    unexplained: list[str] = []
    for method, path, endpoint in _live_operations(app):
        requirement = infer_required_scope(endpoint)
        if requirement.kind in ("dynamic", "public") and not requirement.note:
            unexplained.append(
                f"{method} {path} -> {requirement.kind} with no note "
                f"(add one to _DYNAMIC_SCOPE_NOTES or _PUBLIC_ROUTES in "
                f"caliber/routes/scope_inference.py, or add the missing "
                f"scope check)"
            )
    assert not unexplained, "\n".join(unexplained)


def test_stale_notes_do_not_linger() -> None:
    """A note for a route that no longer exists, or one that no longer
    needs it, is a stale claim -- the same staleness `_STABILITY` guards
    against via `test_stale_tiers_do_not_linger`."""
    from caliber.config import CaliberConfig
    from caliber.server import create_app

    app = create_app(config=CaliberConfig.load(environ={}))
    live_qualified: set[str] = set()
    for _method, _path, endpoint in _live_operations(app):
        requirement = infer_required_scope(endpoint)
        if requirement.kind in ("dynamic", "public"):
            live_qualified.add(_qualified_name(_unwrap_endpoint(endpoint)))

    for source, label in (
        (_DYNAMIC_SCOPE_NOTES, "_DYNAMIC_SCOPE_NOTES"),
        (_PUBLIC_ROUTES, "_PUBLIC_ROUTES"),
    ):
        stale = sorted(set(source) - live_qualified)
        assert not stale, (
            f"{label} names handlers that no longer need it (or no longer exist): {stale}"
        )


def test_apply_job_requires_operator_scope(client: TestClient) -> None:
    """Regression pin against a known, real handler -- catches a bug in the
    walker itself, not just missing coverage."""
    doc = client.get(OPENAPI_URL).json()
    operation = doc["paths"][PREFIX + "/jobs/{job_id}/apply"]["post"]
    assert operation["x-caliber-required-scope"] == {
        "kind": "scope",
        "scopes": ["SCOPE_OPERATOR"],
    }


def test_register_agent_requires_admin_scope(client: TestClient) -> None:
    doc = client.get(OPENAPI_URL).json()
    operation = doc["paths"][PREFIX + "/agents"]["post"]
    assert operation["x-caliber-required-scope"] == {
        "kind": "scope",
        "scopes": ["SCOPE_ADMIN"],
    }


def test_known_dynamic_handlers_are_classified_dynamic_with_their_notes(
    client: TestClient,
) -> None:
    doc = client.get(OPENAPI_URL).json()

    update_judge = doc["paths"][PREFIX + "/judges/{judge_id}"]["patch"]["x-caliber-required-scope"]
    assert update_judge["kind"] == "dynamic"
    assert update_judge["note"] == _DYNAMIC_SCOPE_NOTES["caliber.routes.judges.update_judge"]


def test_openapi_document_scope_field_matches_a_fresh_inference(client: TestClient) -> None:
    """Doc/reality parity, mirroring
    `test_capabilities_and_openapi_agree_on_stability`'s pattern: the served
    document must not disagree with what a fresh inference computes."""
    from caliber.routes.openapi import _normalize_path

    app = client.app
    doc = client.get(OPENAPI_URL).json()

    checked = 0
    for method, path, endpoint in _live_operations(app):
        normalized = _normalize_path(path)
        operation = doc["paths"][normalized][method.lower()]
        expected = serialize_scope_requirement(infer_required_scope(endpoint))
        assert operation["x-caliber-required-scope"] == expected, (
            f"{method} {path}: document has {operation['x-caliber-required-scope']!r}, "
            f"fresh inference says {expected!r}"
        )
        checked += 1
    assert checked > 100  # sanity: the walk actually ran over the real surface


# ---------------------------------------------------------------------------
# Closed-actions ratification (`P0-B`)
# ---------------------------------------------------------------------------


def test_every_live_project_role_action_is_a_known_action(client: TestClient) -> None:
    """`decide_project_access()` treats an unrecognized action as simply
    absent from every role's permission set -- a typo'd or unregistered
    action string would silently deny everyone, indistinguishable from a
    real permission gap, with nothing failing here otherwise. This is the
    closure property: every `action` a live route actually requires must be
    a key `resource_access.py::PROJECT_ACTIONS` recognizes.
    """
    from caliber.resource_access import PROJECT_ACTIONS

    doc = client.get(OPENAPI_URL).json()
    live_actions: set[str] = set()
    for operations in doc["paths"].values():
        for operation in operations.values():
            requirement = operation.get("x-caliber-required-scope", {})
            if requirement.get("kind") == "project_role" and requirement.get("action"):
                live_actions.add(requirement["action"])

    unknown = live_actions - set(PROJECT_ACTIONS)
    assert not unknown, (
        f"route(s) require an action not in PROJECT_ACTIONS: {sorted(unknown)} "
        f"-- add it to resource_access.py::PROJECT_ACTIONS or fix the typo"
    )


def test_the_live_vs_reserved_action_partition_is_pinned() -> None:
    """Ratchet, matching this session's inventory-test style (e.g.
    `test_resource_inventory.py`'s distribution pin): `PROJECT_ACTIONS`
    declares 7 actions today, but only 5 are wired to any live route's
    `require_project_access()`/`_require_project_action()` call --
    `resource.approve` and `resource.execute` are reserved, declared for a
    future check that doesn't exist yet. A change to either side is a real
    event (a route started/stopped enforcing an action, or the registry
    gained/lost a reserved key) and must update this pin deliberately, not
    drift past it silently. Section 2.4's much larger future action
    vocabulary (`P1-B`'s "Closed action enum") is out of scope here -- this
    pins *today's* registry only.
    """
    from caliber.resource_access import PROJECT_ACTIONS
    from caliber.routes.openapi import build_openapi_document
    from caliber.server import create_app

    app = create_app()
    doc = build_openapi_document(app)
    live_actions: set[str] = set()
    for operations in doc["paths"].values():
        for operation in operations.values():
            requirement = operation.get("x-caliber-required-scope", {})
            if requirement.get("kind") == "project_role" and requirement.get("action"):
                live_actions.add(requirement["action"])

    assert live_actions == {
        "read",
        "project.update",
        "project.manage_members",
        "resource.write",
        "resource.publish",
    }
    assert set(PROJECT_ACTIONS) - live_actions == {"resource.approve", "resource.execute"}
