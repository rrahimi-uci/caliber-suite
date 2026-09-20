"""Repo-wide static regression checker for bare project-scoped lookups.

`P2-Q`'s own row (`docs/workspace-plan.md` section 16) closed a manual,
one-time sweep of every ``session.get(<Model>, ...)`` call across
``routes/*.py`` for the 15 :data:`caliber.db.resource_inventory.SCOPING_VISIBILITY`
models, but explicitly declined to build the AST-based regression check that
would keep that sweep's result from silently rotting: "a reliable one would
need to tell every carve-out ... apart from a real gap without
false-positiving ... which is a meaningfully larger, separate design effort
... named here as an explicit follow-up candidate, not attempted." This
module is that follow-up.

It follows :mod:`caliber.routes.scope_inference`'s own established shape for
this kind of inventory: read each route module's own source with :mod:`ast`
rather than hand-duplicating a second copy of the truth, classify every call
site into a small closed set of kinds, and require a human-reviewed,
justified allowlist entry (:data:`_REVIEWED_BARE_LOOKUPS`, playing the same
role ``_DYNAMIC_SCOPE_NOTES``/``_PUBLIC_ROUTES`` play there) for any call
site this module's own heuristics cannot explain -- enforced by
``tests/test_bare_lookup_inventory.py``, which fails by name on an
unclassified/unreviewed finding the same way
``test_route_scope_inventory.py`` fails on an unexplained "dynamic"/"public"
route.

Scope, deliberately narrower than a "find every gap" tool
------------------------------------------------------------------------
This walks every top-level function (route handler or module-level helper)
in every ``caliber.routes.*`` module and finds every ``session.get(<Model>,
...)`` call whose first argument is a literal reference to one of the 15
``SCOPING_VISIBILITY`` models. It does **not** also walk the
``select(<Model>).where(<Model>.<pk> == ...)`` shape the original follow-up
prompt floated as a possible equivalent: a scan of the live route tree found
three dozen ``select(<Model>)`` call sites for these same 15 models, and
reading a sample (``routes/review_queues.py::list_queues``,
``routes/tools.py::list_tools``, ``routes/skills.py::_persist_skill_package_zip``)
showed that the *overwhelming majority* are either already-visibility-filtered
list queries (``apply_visibility_filter`` chained onto the same ``stmt``) or
name-uniqueness pre-checks (``.where(Model.name == ...)``, not a primary-key
lookup at all) -- telling those apart from a genuine bare single-row PK
lookup reliably needs tracing whether the ``Select`` is later reassigned
through ``apply_visibility_filter`` before execution and confirming the
``.where`` clause's column is actually the primary key, which is real
semantic/dataflow analysis, not a quick AST walk. Attempting it here would
either flood this checker's output with false positives on ordinary list
routes (if unfiltered) or silently miss the shape it claims to cover (if
filtered loosely enough to dodge those false positives) -- exactly the
"much larger design effort" `P2-Q`'s row declined to build. ``session.get``
is the idiomatic, unambiguous single-row PK-lookup spelling in this codebase
(it is what ``get_visible`` itself wraps), and it is the exact shape `P2-Q`'s
own sweep searched for ("grepped every ``session.get(<Model>, ...)``
across ``routes/*.py``"), so this module covers that shape completely and
says so plainly rather than covering the ``select`` shape unreliably.

Four kinds of "already safe, no human review needed" call sites, mirroring
`P2-Q`'s own carve-out taxonomy:

* ``"gated"`` -- the same function also calls ``get_visible``/
  ``apply_visibility_filter`` on the same model (and, for ``get_visible``,
  the identical primary-key expression -- covers both a
  ``get_visible(...) if identity is not None else session.get(...)``
  fallback ternary and a same-function re-fetch after an earlier
  ``get_visible`` check on the same id, e.g.
  ``eval_datasets.py::sync_dataset_to_mlflow``'s post-MLflow-write re-fetch).
* ``"admin_only"`` -- the same function also calls
  ``require_scopes``/``require_all_scopes`` with a literal scope list that
  is exactly ``{SCOPE_ADMIN}``. An admin identity already bypasses the
  visibility predicate unconditionally (see
  ``db/scoping.py::apply_visibility_filter``), so a route only reachable by
  an admin needs no separate per-row check -- the same precedent
  `P2-Q` recorded for ``eval_datasets.py``'s ``update_dataset``/
  ``supersede_example``.
* ``"role_gated_own_project"`` -- the looked-up row is bound to a name, and
  the same function later calls ``require_project_access``/
  ``_require_project_action``/``require_project_access_if_scoped`` with that
  row's own ``.project_id`` attribute (found anywhere in the call's
  arguments, including inside a ternary) -- the ``routes/prompts.py``
  hidden-target family's shape, and ``eval_datasets.py``/``skills.py``'s
  admin routes that also re-derive the action from the row's project.
* ``"existence_only"`` -- the call's result (bound to a name or not) is
  used *only* in an ``is None``/``is not None`` comparison, never for
  attribute access -- a pre-insert uniqueness/FK-validity check that
  discloses nothing about the row beyond a boolean, the same reasoning
  `P2-Q` applied to ``agents.py::register_agent`` and
  ``workflows.py::create_workflow``/``create_workflow_benchmark_report``.

Anything not covered by one of those four requires an entry in
:data:`_REVIEWED_BARE_LOOKUPS` recording *why* it's safe -- a "reviewed"
finding. A finding that matches none of the four kinds and has no allowlist
entry is ``"unclassified"`` and fails the enforcement test by name.
"""

from __future__ import annotations

# A bounded AST interpreter, matching scope_inference.py's own suppression:
# its branches check literal argument positions (`get_visible`'s 2nd/4th
# args, `require_scopes`'s 2nd arg), which are call-shape constants, not the
# "unexplained magic number" this rule exists to catch.
# ruff: noqa: PLR2004
import ast
import importlib
import inspect
import pkgutil
from dataclasses import dataclass

from caliber.db.resource_inventory import SCOPING_VISIBILITY, resource_inventory


#: The 15 models this session's manual sweep (`P2-Q`) covered, derived live
#: from the same registry `db/resource_inventory.py` itself classifies --
#: rather than a hand-copied literal list that could silently drift from the
#: real `SCOPING_VISIBILITY` partition (the same "don't hand-duplicate a
#: derivable fact" discipline `resource_inventory.py`'s own docstring
#: describes).
def _scoping_visibility_model_names() -> frozenset[str]:
    return frozenset(
        descriptor.name
        for descriptor in resource_inventory()
        if descriptor.scoping == SCOPING_VISIBILITY
    )


#: Names of functions that gate access via the project-role axis, duplicated
#: from `scope_inference.py::_PROJECT_ACCESS_CALL_NAMES` rather than
#: imported -- the two modules answer unrelated questions (required scope vs.
#: bare-lookup safety) and this set is three names long, so a shared import
#: would couple them for no real benefit (`scope_inference.py`'s own
#: `_unwrap_endpoint` docstring names this exact trade-off).
_PROJECT_ACCESS_CALL_NAMES = frozenset(
    {"require_project_access", "_require_project_action", "require_project_access_if_scoped"}
)

#: Scope constant names, duplicated from `scope_inference.py::_SCOPE_CONSTANT_NAMES`
#: for the same reason as `_PROJECT_ACCESS_CALL_NAMES` above.
_SCOPE_CONSTANT_NAMES = frozenset(
    {"SCOPE_VIEWER", "SCOPE_OPERATOR", "SCOPE_APPROVER", "SCOPE_ADMIN"}
)


#: The `caliber.routes` submodules this checker walks. Derived live from the
#: package's own `__path__` (like `resource_inventory()`'s live mapper walk)
#: rather than a hand-maintained list, so a new route module is covered
#: automatically rather than silently skipped.
def _route_module_names() -> list[str]:
    import caliber.routes as routes_package  # noqa: PLC0415

    return sorted(
        f"caliber.routes.{info.name}"
        for info in pkgutil.iter_modules(routes_package.__path__)
        if not info.name.startswith("_")
    )


@dataclass(frozen=True)
class BareLookupFinding:
    """One `session.get(<SCOPING_VISIBILITY model>, ...)` call site."""

    module: str
    qualname: str  # enclosing top-level function's name
    lineno: int
    model: str
    kind: str  # "gated" | "admin_only" | "role_gated_own_project" | "existence_only" | "reviewed" | "unclassified"
    note: str | None = None

    @property
    def key(self) -> str:
        """Stable identity for allowlisting and stale-entry detection."""
        return f"{self.module}.{self.qualname}:{self.lineno}"


#: Human-reviewed carve-outs this checker's own heuristics cannot explain.
#: Keyed by `BareLookupFinding.key` (`f"{module}.{qualname}:{lineno}"`) so a
#: future edit that moves the line goes stale here and must be re-justified,
#: not silently keep matching -- enforced by
#: `test_bare_lookup_inventory.py::test_reviewed_allowlist_entries_are_all_still_live`,
#: mirroring `test_route_scope_inventory.py::test_stale_notes_do_not_linger`.
#: Every entry below was traced by hand against the actual gating code (not
#: just pattern-matched) as part of building this checker; see each comment.
_REVIEWED_BARE_LOOKUPS: dict[str, str] = {
    "caliber.routes.aria_plans._record_plan_authorization_snapshot:224": (
        "Persists a one-time authorization snapshot onto a plan already "
        "resolved through `assistant/plans.py::PlanService.get_plan`, which "
        "calls `get_visible(session, CaliberAriaPlan, ...)` keyed on the "
        "identical `plan_id` before either caller (`execute_plan`/"
        "`poll_plan`) reaches this helper -- a cross-module re-fetch of an "
        "already-checked row, not a fresh lookup. This checker only walks a "
        "single function's own body (matching `scope_inference.py`'s own "
        "documented scope), so it cannot see the check performed in the "
        "caller's caller, in a different module."
    ),
    "caliber.routes.services.invoke_service:839": (
        "`routes/services.py`'s service-invocation surface authenticates via "
        "a per-service bearer token (`_lock_service_configuration`/"
        "`_validate_service_token_in_session`, both called earlier in this "
        "same function), not the caller-identity visibility scheme -- the "
        "same separate auth axis `scope_inference.py::_PUBLIC_ROUTES` "
        "already records this exact route under for the required-scope "
        "inventory. The invoked workflow's own visibility is not the "
        "applicable gate here."
    ),
    "caliber.routes.services.service_openapi:1147": (
        "Same per-service bearer-token gate as `invoke_service` above "
        "(`_lock_service_configuration`/`_validate_service_token_in_session`, "
        "called earlier in this function) -- also listed in "
        "`scope_inference.py::_PUBLIC_ROUTES` for the same reason."
    ),
    "caliber.routes.skills.get_skill_workspace:1131": (
        "`target_agent_id` is computed via `skill_target_agent_id(skill.name)`, "
        "deterministically derived from `skill.name` of the `CaliberSkill` "
        "row this same function already resolved through "
        "`_visible_skill_or_404` a few lines above -- the caller cannot steer "
        "this id anywhere except the hidden runtime target of a skill they "
        "already have visibility into. Not the same model/pk shape "
        "`get_visible` in this function checks (that call is for "
        "`CaliberSkill` by `skill_id`; this lookup is `CaliberAgentConfig` "
        "by a derived id), so this checker's same-model/same-pk `gated` rule "
        "does not and should not auto-clear it -- recorded here by hand "
        "instead."
    ),
    "caliber.routes.workflow_versions._run_workflow_version_sync:862": (
        "The parent workflow was already authorized a few lines above via "
        "`_get_version_or_404(session, version_id, request=request)`, which "
        "calls `_deps.py::scoped_child_or_404` -- itself gated on this exact "
        "`CaliberWorkflow` (`version.workflow_id`) through "
        "`apply_visibility_filter`/`get_visible`-equivalent parent-visibility "
        "logic (a forbidden parent 404s identically to a missing one, per "
        "that helper's own docstring). This checker does not chase through "
        "`_get_version_or_404`/`scoped_child_or_404` into a different "
        "function's body to see that (the same 'do not chase into helpers' "
        "scope `scope_inference.py` documents for itself), so the re-fetch "
        "here -- needed only to read `workflow.status` -- is recorded by "
        "hand instead of auto-detected."
    ),
}


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)


def _is_literal_admin_only_scope_call(call: ast.Call) -> bool:
    """True for `require_scopes(request, [SCOPE_ADMIN])`/`require_all_scopes(...)`
    with a scope-list argument that is a clean literal equal to exactly
    `{SCOPE_ADMIN}`. Deliberately conservative: a multi-scope list (even one
    that includes `SCOPE_ADMIN`) does not qualify, since a non-admin caller
    could then legitimately reach the code past this call."""
    if _call_name(call) not in {"require_scopes", "require_all_scopes"}:
        return False
    if len(call.args) < 2 or not isinstance(call.args[1], (ast.List, ast.Tuple, ast.Set)):
        return False
    names: set[str] = set()
    for element in call.args[1].elts:
        if not isinstance(element, ast.Name) or element.id not in _SCOPE_CONSTANT_NAMES:
            return False
        names.add(element.id)
    return names == {"SCOPE_ADMIN"}


def _build_parent_map(root: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(root):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _compares_to_none(node: ast.AST | None) -> bool:
    """True if `node` is an `ast.Compare` testing `Is`/`IsNot` against a
    literal `None`, on either side."""
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return False
    if not isinstance(node.ops[0], (ast.Is, ast.IsNot)):
        return False
    other = node.comparators[0]
    return (isinstance(node.left, ast.Constant) and node.left.value is None) or (
        isinstance(other, ast.Constant) and other.value is None
    )


def _is_existence_only_use(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    """True if `call`'s result -- whether bound to a name or used inline --
    is referenced *only* through an `is None`/`is not None` comparison
    anywhere in the enclosing function, never through attribute access,
    a return, or being passed elsewhere."""
    parent = parents.get(call)
    if (
        isinstance(parent, ast.Assign)
        and len(parent.targets) == 1
        and isinstance(parent.targets[0], ast.Name)
    ):
        bound_name = parent.targets[0].id
        # Walk the whole function (available via the Assign's own ancestry)
        # for every *other* Load reference to this name.
        function = _enclosing_function(parent, parents)
        if function is None:
            return False
        uses = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Name)
            and node.id == bound_name
            and isinstance(node.ctx, ast.Load)
        ]
        if not uses:
            return False
        return all(_compares_to_none(parents.get(use)) for use in uses)
    # Not assigned: the call's own result must be the direct operand of an
    # `is None`/`is not None` comparison (e.g. `if session.get(...) is not
    # None:`) to count as existence-only.
    return parent is not None and _compares_to_none(parent)


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    current = parents.get(node)
    while current is not None and not isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
        current = parents.get(current)
    return current


def _project_id_attr_names(call: ast.Call, bound_name: str) -> bool:
    """True if any argument of `call` contains `<bound_name>.project_id`,
    including inside a ternary (`ast.IfExp`) -- covers
    `routes/prompts.py`'s `existing_target.project_id if existing_target is
    not None else ...` shape as well as a direct attribute reference."""
    for argument in (*call.args, *(keyword.value for keyword in call.keywords)):
        for node in ast.walk(argument):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "project_id"
                and isinstance(node.value, ast.Name)
                and node.value.id == bound_name
            ):
                return True
    return False


def _classify_call(
    call: ast.Call,
    *,
    model: str,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    parents: dict[ast.AST, ast.AST],
) -> tuple[str, str | None]:
    other_calls = [
        node for node in ast.walk(function) if isinstance(node, ast.Call) and node is not call
    ]

    # "gated": a `get_visible`/`apply_visibility_filter` call on the same
    # model elsewhere in this function -- for `get_visible`, additionally
    # requiring the identical primary-key expression (its 4th positional
    # argument) so this doesn't clear a same-function check on some *other*
    # row of the same model.
    call_pk = ast.dump(call.args[1]) if len(call.args) > 1 else None
    for other in other_calls:
        name = _call_name(other)
        if (
            name == "get_visible"
            and len(other.args) > 3
            and isinstance(other.args[1], ast.Name)
            and other.args[1].id == model
            and call_pk is not None
            and ast.dump(other.args[3]) == call_pk
        ):
            return "gated", "get_visible on the same model+pk elsewhere in this function"
        if (
            name == "apply_visibility_filter"
            and len(other.args) > 1
            and isinstance(other.args[1], ast.Name)
            and other.args[1].id == model
        ):
            return "gated", "apply_visibility_filter on the same model elsewhere in this function"

    # "admin_only": same-function literal admin-only scope requirement, i.e.
    # a `require_scopes`/`require_all_scopes` call whose literal scope list
    # is exactly the admin scope constant.
    for other in other_calls:
        if _is_literal_admin_only_scope_call(other):
            return (
                "admin_only",
                "SCOPE_ADMIN-only route (admin bypasses visibility unconditionally)",
            )

    # "role_gated_own_project": the looked-up row is bound to a name, and a
    # later project-access call in this function references that name's own
    # `.project_id`.
    parent = parents.get(call)
    if (
        isinstance(parent, ast.Assign)
        and len(parent.targets) == 1
        and isinstance(parent.targets[0], ast.Name)
    ):
        bound_name = parent.targets[0].id
        for other in other_calls:
            if _call_name(other) in _PROJECT_ACCESS_CALL_NAMES and _project_id_attr_names(
                other, bound_name
            ):
                return (
                    "role_gated_own_project",
                    "a later project-access call in this function is keyed on the "
                    "looked-up row's own project_id",
                )

    # "existence_only": the result is never used for anything but an
    # is-None/is-not-None check.
    if _is_existence_only_use(call, parents):
        return "existence_only", "result is only ever compared to None, never inspected"

    return "unclassified", None


def _scan_module(module_name: str, model_names: frozenset[str]) -> list[BareLookupFinding]:
    module = importlib.import_module(module_name)
    try:
        source = inspect.getsource(module)
    except (OSError, TypeError):  # pragma: no cover - no source available
        return []
    tree = ast.parse(source)
    parents = _build_parent_map(tree)

    findings: list[BareLookupFinding] = []
    for function in tree.body:
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(function):
            # `isinstance(node.func, ast.Attribute)` restricts this to a
            # `<something>.get(...)` method-call shape (e.g. `session.get`),
            # not a bare `get(...)` name call -- the only shape this
            # codebase actually uses for a primary-key row fetch.
            if (
                not isinstance(node, ast.Call)
                or not isinstance(node.func, ast.Attribute)
                or node.func.attr != "get"
            ):
                continue
            if not node.args or not isinstance(node.args[0], ast.Name):
                continue
            model = node.args[0].id
            if model not in model_names:
                continue
            kind, note = _classify_call(node, model=model, function=function, parents=parents)
            finding = BareLookupFinding(
                module=module_name,
                qualname=function.name,
                lineno=node.lineno,
                model=model,
                kind=kind,
                note=note,
            )
            if kind == "unclassified":
                reviewed_note = _REVIEWED_BARE_LOOKUPS.get(finding.key)
                if reviewed_note is not None:
                    finding = BareLookupFinding(
                        module=module_name,
                        qualname=function.name,
                        lineno=node.lineno,
                        model=model,
                        kind="reviewed",
                        note=reviewed_note,
                    )
            findings.append(finding)
    return findings


def bare_lookup_inventory() -> list[BareLookupFinding]:
    """Every `session.get(<SCOPING_VISIBILITY model>, ...)` call site across
    every `caliber.routes.*` module, classified per this module's docstring."""
    model_names = _scoping_visibility_model_names()
    findings: list[BareLookupFinding] = []
    for module_name in _route_module_names():
        findings.extend(_scan_module(module_name, model_names))
    return sorted(findings, key=lambda finding: (finding.module, finding.lineno))


__all__ = [
    "BareLookupFinding",
    "bare_lookup_inventory",
]
