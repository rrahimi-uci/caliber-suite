"""Infer a route handler's required scope from its own source.

Part of `P0-A` (`docs/workspace-plan.md` section 16): a machine-readable
route-level authorization inventory. `docs/workspace-plan.md` section 2.4's
25-row action-to-scope registry exists only as markdown prose today, with no
file:line grounding and no way to detect drift against the actual
enforcement code (`require_scopes()`/`require_user()`/`require_project_access()`
calls scattered across 45+ route handler files). This module closes that gap
the same way `openapi_inference.py` closes the equivalent gap for
request/response shapes: by reading the handler's own source with `ast`,
rather than hand-duplicating a second copy of the truth that could silently
disagree with what a real request actually enforces.

Every handler in this codebase puts its primary authorization call --
`require_scopes(request, [...])`, `require_user(request)`, or
`require_project_access(...)`/`_require_project_action(...)` -- directly in
the function Starlette dispatches to (never behind an indirection layer)
-- confirmed by reading `agents.py`, `jobs.py`, `judges.py`, `releases.py`,
`workflow_deployments.py`, `projects.py`, `files.py`, `object_store.py`,
`openapi_integrations.py`, and every route module built this session -- so
a single-function AST walk (matching `openapi_inference.py::_analyze`'s own
scope) is sufficient; this does not chase into helpers the handler calls
beyond the two known project-access wrapper names.

Five possible classifications (:class:`ScopeRequirement.kind`):

* ``"scope"`` -- one or more `require_scopes(request, [SCOPE_X, ...])` calls
  where every argument is a literal reference to one of the four scope
  constants. Multiple calls in one handler are a conjunction (AND): each
  independently raises if unsatisfied, so reaching the code after both means
  both passed. (`require_scopes` itself is OR-within-one-call -- "at least
  one of" its own list -- matching `docs/workspace-plan.md` section 2.4's
  note that today's helper cannot express a single call requiring two scopes
  at once.)
* ``"authenticated"`` -- `require_user(request)` only: any signed-in caller,
  no specific scope.
* ``"project_role"`` -- gated by `require_project_access(...)` (or the
  local `_require_project_action(...)` wrapper `routes/projects.py` builds
  on top of it), not a global scope at all: a separate, project-membership-role
  axis (owner/editor/reviewer/viewer against an action string like
  ``"read"``/``"resource.write"``/``"project.manage_members"`` -- the same
  action vocabulary `docs/workspace-plan.md` section 2.4 names). Confirmed
  across `files.py`, `projects.py`, `object_store.py`, and
  `openapi_integrations.py` -- the four modules using this pattern today.
  Detected by call name rather than by a scope-list shape, since
  `require_project_access` takes an action string, not a scope list; the
  action string is captured in :attr:`ScopeRequirement.action` when it's a
  literal.
* ``"dynamic"`` -- a `require_scopes(...)` call whose scope-list argument is
  not a clean literal (e.g. a name bound earlier by a conditional
  expression, such as `judges.py::update_judge`'s
  ``SCOPE_ADMIN if "status" in changes else SCOPE_OPERATOR``). Detected
  generically -- any future runtime-computed requirement is automatically
  caught, not just today's two known cases -- and requires a `_DYNAMIC_SCOPE_NOTES`
  entry (enforced by `tests/test_route_scope_inventory.py`) explaining the
  rule in prose, since it cannot be reduced to one static value.
* ``"public"`` -- none of the above calls appear in the handler body at
  all. Requires a `_PUBLIC_ROUTES` entry with a reason, for the same
  test-enforced reason: a route with no authorization call and no reviewed
  justification is exactly the gap that shipped silently as
  `review_queues.py::submit_item` before it was found and fixed. Genuinely
  different authorization axes (a per-service configurable token, an opt-in
  metrics scrape token) still classify as ``"public"`` from this module's
  global-scope lens -- the note explains what actually gates them instead.
"""

from __future__ import annotations

# A bounded AST interpreter, matching openapi_inference.py's own suppression:
# its branches check literal argument positions (require_scopes' 2nd arg,
# require_project_access's 4th), which are call-shape constants, not the
# "unexplained magic number" this rule exists to catch.
# ruff: noqa: PLR2004
import ast
import inspect
import textwrap
from dataclasses import dataclass, field
from typing import Any

#: Names of functions that gate access via the project-role axis
#: (`resource_access.py::require_project_access`) rather than a global
#: scope. `_require_project_action` is `routes/projects.py`'s own local
#: wrapper -- it calls `require_project_access` internally, but since this
#: module deliberately doesn't chase into helpers, its name is allowlisted
#: here directly instead.
_PROJECT_ACCESS_CALL_NAMES = frozenset({"require_project_access", "_require_project_action"})

_SCOPE_CONSTANT_NAMES = frozenset(
    {"SCOPE_VIEWER", "SCOPE_OPERATOR", "SCOPE_APPROVER", "SCOPE_ADMIN"}
)

#: Handlers whose scope requirement is genuinely computed at request time,
#: not a fixed literal. Keyed by ``f"{module}.{qualname}"``. A "dynamic"
#: classification with no entry here fails
#: ``test_route_scope_inventory.py`` -- this dict is where a human explains
#: *why*, not a place new entries get added silently.
_DYNAMIC_SCOPE_NOTES: dict[str, str] = {
    "caliber.routes.judges.update_judge": (
        "SCOPE_ADMIN if the request body includes 'status' (archive/restore, "
        "the delete-equivalent for a judge), else SCOPE_OPERATOR for content "
        "fields (description/instructions/model/feedback_value_type/tags)."
    ),
    "caliber.routes.workflow_deployments.promote_deployment": (
        "SCOPE_ADMIN if requires_human_approval(alias, config) -- i.e. the "
        "target alias is a gated environment under the deployment's release "
        "policy -- else SCOPE_OPERATOR for an immediate rotation."
    ),
}

#: Handlers with no `require_scopes`/`require_user` call anywhere in their
#: body, and why that's intentional rather than a gap. Keyed the same way as
#: `_DYNAMIC_SCOPE_NOTES`. A "public" classification with no entry here
#: fails `test_route_scope_inventory.py`.
_PUBLIC_ROUTES: dict[str, str] = {
    "caliber.routes.health.healthcheck": "Liveness probe; load balancers and deploy gates call it pre-auth.",
    "caliber.routes.health.readiness": "Dependency-readiness probe; same pre-auth callers as health.",
    "caliber.routes.csrf.issue_token": (
        "Anonymous is a legitimate caller by design (see the route's own "
        "comment): CSRF token issuance must work before login."
    ),
    "caliber.routes.auth.login": "Pre-authentication by definition -- this is what establishes identity.",
    "caliber.routes.auth.logout": (
        "Acts on whatever session cookie is present, including none/expired; "
        "there is nothing to hold a scope check against."
    ),
    "caliber.routes.auth.session_info": (
        "Reports whether a session is currently valid; an anonymous caller "
        "must be able to ask this rather than getting a 401 for asking."
    ),
    "caliber.routes.me.get_me": (
        "Reports the caller's own resolved identity/scopes, including the "
        "anonymous case -- the same 'must answer before login' shape as "
        "/csrf and /auth/session."
    ),
    "caliber.routes.metrics.metrics_endpoint": (
        "Opt-in bearer-token gate configured via CALIBER_METRICS_TOKEN_ENV "
        "(see the route module's own docstring), not the caller-identity "
        "scope system -- a Prometheus scrape config cannot carry a session."
    ),
    "caliber.routes.services.service_openapi": (
        "Per-service, operator-configured token gate (service.auth_required, "
        "validated in-handler), not the caller-identity scope system -- an "
        "externally published service authenticates its own callers."
    ),
    "caliber.routes.services.invoke_service": (
        "Same per-service token gate as service_openapi -- see _preauthorize_service_invocation."
    ),
    "caliber.routes.services.get_service_run_status": (
        "Same per-service token gate as service_openapi."
    ),
}


@dataclass(frozen=True)
class ScopeRequirement:
    """The result of inferring one handler's required scope."""

    kind: str  # "scope" | "authenticated" | "project_role" | "dynamic" | "public"
    scopes: frozenset[str] = frozenset()
    action: str | None = None  # the project action string, for kind="project_role"
    note: str | None = None


def _unwrap_endpoint(endpoint: Any) -> Any:
    """Prefer the real handler over transport wrappers, matching
    ``openapi_inference._unwrap_endpoint`` (kept as a separate copy rather
    than a shared import, since the two modules answer unrelated questions
    and importing across them for four lines would couple them for no
    reason)."""
    target = inspect.unwrap(endpoint)
    closure = getattr(target, "__closure__", None) or ()
    for cell in closure:
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if callable(value) and getattr(value, "__name__", None) == getattr(
            target, "__name__", None
        ):
            return inspect.unwrap(value)
    return target


def _qualified_name(endpoint: Any) -> str:
    module = getattr(endpoint, "__module__", "") or ""
    qualname = getattr(endpoint, "__qualname__", "") or getattr(endpoint, "__name__", "")
    return f"{module}.{qualname}"


def _function_body(endpoint: Any) -> list[ast.stmt]:
    source = textwrap.dedent(inspect.getsource(endpoint))
    tree = ast.parse(source)
    fn = next(
        (node for node in tree.body if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))),
        None,
    )
    return fn.body if fn is not None else []


@dataclass
class _CallSurvey:
    scope_calls: list[ast.Call] = field(default_factory=list)
    project_access_calls: list[ast.Call] = field(default_factory=list)
    has_require_user: bool = False


def _survey_calls(statements: list[ast.stmt]) -> _CallSurvey:
    survey = _CallSurvey()
    for node in ast.walk(ast.Module(body=statements, type_ignores=[])):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name == "require_scopes":
            survey.scope_calls.append(node)
        elif name == "require_user":
            survey.has_require_user = True
        elif name in _PROJECT_ACCESS_CALL_NAMES:
            survey.project_access_calls.append(node)
    return survey


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)


#: `resource_access.py::require_project_access`'s own default for its
#: `action` parameter, applied when a call site omits it entirely (e.g.
#: `projects.py::list_projects`'s per-row `require_project_access(session,
#: identity, row.project_id)`) -- resolving this explicitly rather than
#: reporting "unknown" for the common, uneventful case.
_REQUIRE_PROJECT_ACCESS_DEFAULT_ACTION = "read"


def _literal_project_action(call: ast.Call) -> str | None:
    """The literal action string this call resolves to, including when it
    relies on `require_project_access`'s own default.

    `require_project_access(session, identity, project_id, action=...)` takes
    `action` as its 4th positional argument (default ``"read"`` when
    omitted); `_require_project_action(session, project_id, identity=...,
    action=...)` (`routes/projects.py`'s wrapper) always passes it as the
    keyword `action=`, with no default of its own. Returns ``None`` (not
    "dynamic" -- this is metadata on an already-real authorization call, not
    the whole classification) only when a non-default action is passed
    non-literally.
    """
    for keyword in call.keywords:
        if keyword.arg == "action":
            if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                return keyword.value.value
            return None
    callee = _call_name(call)
    if callee == "require_project_access":
        if len(call.args) < 4:
            return _REQUIRE_PROJECT_ACCESS_DEFAULT_ACTION
        candidate = call.args[3]
        if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
            return candidate.value
        return None
    return None


def _literal_scope_names(call: ast.Call) -> frozenset[str] | None:
    """The call's scope-list argument, if it's a clean literal.

    Returns the resolved scope constant names, or ``None`` if the argument
    isn't a `List` of bare `Name` references to the four known scope
    constants -- e.g. a variable computed by an earlier conditional
    expression, which is exactly what makes a requirement "dynamic" rather
    than static.
    """
    if len(call.args) < 2:
        return None
    scopes_arg = call.args[1]
    if not isinstance(scopes_arg, (ast.List, ast.Tuple, ast.Set)):
        return None
    names: set[str] = set()
    for element in scopes_arg.elts:
        if not isinstance(element, ast.Name) or element.id not in _SCOPE_CONSTANT_NAMES:
            return None
        names.add(element.id)
    return frozenset(names) if names else None


def infer_required_scope(endpoint: Any) -> ScopeRequirement:
    """Classify one route handler's required scope from its own source."""
    target = _unwrap_endpoint(endpoint)
    qualified = _qualified_name(target)
    body = _function_body(target)
    survey = _survey_calls(body)

    if survey.project_access_calls:
        # A route can call this more than once in principle; there is no
        # observed case of two calls disagreeing on the action, so the first
        # literal found wins.
        action = next(
            (a for a in (_literal_project_action(c) for c in survey.project_access_calls) if a),
            None,
        )
        return ScopeRequirement(kind="project_role", action=action)

    if not survey.scope_calls:
        if survey.has_require_user:
            return ScopeRequirement(kind="authenticated")
        return ScopeRequirement(
            kind="public",
            note=_PUBLIC_ROUTES.get(qualified),
        )

    resolved: set[str] = set()
    for call in survey.scope_calls:
        names = _literal_scope_names(call)
        if names is None:
            return ScopeRequirement(
                kind="dynamic",
                note=_DYNAMIC_SCOPE_NOTES.get(qualified),
            )
        resolved |= names
    return ScopeRequirement(kind="scope", scopes=frozenset(resolved))


def serialize_scope_requirement(requirement: ScopeRequirement) -> dict[str, Any]:
    """Compact form for the OpenAPI document's ``x-caliber-required-scope``."""
    payload: dict[str, Any] = {"kind": requirement.kind}
    if requirement.scopes:
        payload["scopes"] = sorted(requirement.scopes)
    if requirement.action:
        payload["action"] = requirement.action
    if requirement.note:
        payload["note"] = requirement.note
    return payload


__all__ = [
    "ScopeRequirement",
    "infer_required_scope",
    "serialize_scope_requirement",
]
