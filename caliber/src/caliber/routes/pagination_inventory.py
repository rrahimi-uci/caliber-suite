"""Machine-readable inventory of list-route pagination shapes (`P0-B`).

Phase 0's job is to freeze contracts, not to change behavior. Research for
this ticket found pagination is **not** one contract today -- it is at least
five different shapes spread across ~25 list routes, with nothing asserting
which route uses which. This module classifies every live list route so a
future route can't silently introduce a sixth undocumented shape, and so
reconciling this inventory into one real contract (a genuine behavior
change -- Phase 1+/2+ work, not attempted here) starts from a known,
exhaustive list instead of a fresh audit.

Two kinds of derivation, the same split `worker_inventory.py` uses:

* Whether a route calls the shared `_deps.list_limit()` helper is safely
  AST-derivable (a bounded call-name check, the same technique
  `scope_inference.py` uses for `require_scopes()`) -- no hand-maintenance
  needed for that part.
* Which *other* shape a non-`list_limit()` list route uses (ad hoc
  limit-only with its own default/cap, audit's bespoke limit/offset/total
  envelope, a hardcoded scan with a `total` that isn't a true grand total, or
  real cursor pagination) is not mechanically inferable from one shared call
  site -- each is bespoke. `_PAGINATION_NOTES` hand-maintains those,
  verified by a staleness test the same way `_DYNAMIC_SCOPE_NOTES` /
  `_PUBLIC_ROUTES` are in `scope_inference.py`.

A route is only in scope for this inventory if it looks like a list route at
all -- detected by whether its own source reads a `limit`/`offset`/`cursor`
query parameter. A route with none of those and no note is simply not a list
route (the vast majority of the API), and is left `NOT_A_LIST_ROUTE`.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass
from enum import Enum
from typing import Any

from caliber.routes.openapi import PREFIX
from caliber.routes.scope_inference import _unwrap_endpoint


def _key(path: str) -> tuple[str, str]:
    """Build a `_PAGINATION_NOTES` key: lowercase GET (every route below is a
    GET) plus the full `PREFIX`-included path, matching what
    `routes/openapi.py::_normalize_path` actually returns (it does not strip
    the management-API prefix)."""
    return ("get", PREFIX + path)


class PaginationShape(str, Enum):
    """The distinct pagination shapes found across today's list routes."""

    #: `_deps.list_limit()`: `limit`+`offset`, default 500, cap 2000. The
    #: closest thing to a shared convention today.
    LIST_LIMIT_HELPER = "list_limit_helper"
    #: Own `limit`-only parsing (no `offset`), its own default and cap.
    AD_HOC_LIMIT_ONLY = "ad_hoc_limit_only"
    #: Own `limit`+`offset` parsing, plus a `total` field in a route-specific
    #: response envelope shape (not the generic `{"data": [...]}` list shape).
    BESPOKE_TOTAL_ENVELOPE = "bespoke_total_envelope"
    #: No client-supplied paging params; a hardcoded/capped scan, with a
    #: `total` that is the size of what was returned, not a true grand total.
    UNPAGINATED_FAKE_TOTAL = "unpaginated_fake_total"
    #: Real cursor pagination: `limit`+opaque `cursor`, computes a genuine
    #: `next_cursor`.
    CURSOR = "cursor"
    #: `after`+`limit` sequence watermark: `after` is a visible integer
    #: sequence number (not an opaque token), events after it are returned
    #: up to `limit`. Functionally cursor-like but a different wire shape.
    SEQUENCE_WATERMARK = "sequence_watermark"
    #: Not a list/pagination route at all -- the default for everything not
    #: named below and not detected as using `list_limit()`.
    NOT_A_LIST_ROUTE = "not_a_list_route"


@dataclass(frozen=True)
class PaginationNote:
    shape: PaginationShape
    note: str


#: Hand-maintained for every list route that does *not* call
#: `_deps.list_limit()` -- see the module docstring for why these can't be
#: derived mechanically. Keyed by `(method, path)` exactly as they appear in
#: the served OpenAPI document (lowercase method, `PREFIX`-relative path with
#: `{param}` placeholders, matching `routes/openapi.py::_normalize_path`).
_PAGINATION_NOTES: dict[tuple[str, str], PaginationNote] = {
    _key("/evaluations"): PaginationNote(
        PaginationShape.AD_HOC_LIMIT_ONLY,
        "own `limit` parsing (evaluations.py::list_evaluations); default 100, cap 500, no `offset`",
    ),
    _key("/knowledge-base-versions/{version_id}/chunks"): PaginationNote(
        PaginationShape.AD_HOC_LIMIT_ONLY,
        "own `limit` parsing (knowledge_bases.py::list_chunks); default 200, no cap seen",
    ),
    _key("/knowledge-bases/{knowledge_base_id}/test-runs"): PaginationNote(
        PaginationShape.AD_HOC_LIMIT_ONLY,
        "own `limit` parsing (knowledge_bases.py::list_calibration_runs); default 20",
    ),
    _key("/observability/experiments"): PaginationNote(
        PaginationShape.AD_HOC_LIMIT_ONLY,
        "own `_limit()` helper local to observability.py::list_experiments; default 50, cap 200",
    ),
    _key("/releases/timeline"): PaginationNote(
        PaginationShape.AD_HOC_LIMIT_ONLY,
        "own `limit` parsing (releases.py::timeline); default 50, cap 200",
    ),
    _key("/releases/operations"): PaginationNote(
        PaginationShape.AD_HOC_LIMIT_ONLY,
        "own `limit` parsing (releases.py::release_operations); default 100, cap 500",
    ),
    _key("/prompts/test-runs"): PaginationNote(
        PaginationShape.AD_HOC_LIMIT_ONLY,
        "own `limit` parsing (prompts.py::list_prompt_test_runs); default 20, cap 100",
    ),
    _key("/skills/test-runs"): PaginationNote(
        PaginationShape.AD_HOC_LIMIT_ONLY,
        "own `limit` parsing (skills.py::list_skill_test_runs); default 20, cap 100",
    ),
    _key("/tools/test-runs"): PaginationNote(
        PaginationShape.AD_HOC_LIMIT_ONLY,
        "own `limit` parsing (tools.py::list_tool_test_runs); default 20, cap 100",
    ),
    _key("/system/incidents"): PaginationNote(
        PaginationShape.AD_HOC_LIMIT_ONLY,
        "own `limit` parsing (system_services.py::get_incidents); default 50, no cap; "
        "response also carries `total`, but it echoes the returned row count, not a "
        "grand total across all incidents",
    ),
    _key("/system/effects"): PaginationNote(
        PaginationShape.AD_HOC_LIMIT_ONLY,
        "own `limit` parsing (system_effects.py::list_system_effects); default 100, no cap",
    ),
    _key("/system/webhook-dead-letters"): PaginationNote(
        PaginationShape.AD_HOC_LIMIT_ONLY,
        "own `limit` parsing (system_effects.py::list_webhook_dead_letters); default 100, cap 1000",
    ),
    _key("/audit-log"): PaginationNote(
        PaginationShape.BESPOKE_TOTAL_ENVELOPE,
        "audit.py::list_audit_log; own `limit`+`offset` parsing, returns "
        "`AuditLogPageSchema` (`entries`/`total`/`limit`/`offset`) -- structurally "
        "different from every other list route's `{\"data\": [...]}` envelope",
    ),
    _key("/auth/accounts"): PaginationNote(
        PaginationShape.UNPAGINATED_FAKE_TOTAL,
        "auth.py::list_accounts; no client params, full unpaginated scan, "
        "`total=len(accounts)`",
    ),
    _key("/tools/{tool_id}/calibration-jobs"): PaginationNote(
        PaginationShape.UNPAGINATED_FAKE_TOTAL,
        "tools.py::list_calibration_jobs; no client params, hardcoded "
        "`.limit(50)` server-side, `total=len(items)`",
    ),
    _key("/workflows/{workflow_id}/runs"): PaginationNote(
        PaginationShape.CURSOR,
        "workflow_versions.py::list_runs_route; the one route with real cursor "
        "pagination -- `limit`+opaque `cursor`, computes genuine `has_more`/`next_cursor`",
    ),
    _key("/workflow-runs/{run_id}/events"): PaginationNote(
        PaginationShape.SEQUENCE_WATERMARK,
        "workflow_runs.py::list_workflow_run_events; `after`+`limit` via the "
        "file-local `_parse_positive_int()`; default limit 200, max 1000",
    ),
    _key("/workflow-runs/{run_id}/checkpoints"): PaginationNote(
        PaginationShape.SEQUENCE_WATERMARK,
        "workflow_runs.py::list_workflow_run_checkpoints; same `after`+`limit` "
        "shape as the events route above, same file-local helper",
    ),
}

#: Query-param names whose presence in a handler's own source marks it as
#: list-route-shaped, for the completeness check below. A route reading any
#: of these must resolve to a definite classification -- either detected as
#: a `list_limit()` user or present in `_PAGINATION_NOTES`.
_LIST_ROUTE_SIGNAL_PARAMS = ("limit", "offset", "cursor")


def _uses_list_limit_helper(source: str) -> bool:
    """Does this handler's own source call `_deps.list_limit(...)`?

    Mirrors `scope_inference.py`'s bounded-call-name technique: a handler
    that imports and calls the shared helper is unambiguous from its own
    AST, no hand-maintenance needed.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "list_limit"
        ):
            return True
    return False


def _reads_pagination_query_param(source: str) -> bool:
    """Does this handler's own source read a `limit`/`offset`/`cursor`
    query parameter directly (``request.query_params.get("limit")`` and
    friends)? Used only to flag routes this inventory hasn't classified yet
    -- it is deliberately a looser, over-inclusive signal (a string literal
    match, not a full call-graph trace), so it can flag more than it needs
    to without ever missing a real list route.
    """
    return any(f'"{name}"' in source or f"'{name}'" in source for name in _LIST_ROUTE_SIGNAL_PARAMS)


def classify_pagination(endpoint: Any) -> PaginationNote | None:
    """Classify one route's pagination shape from its handler alone.

    Returns ``None`` when the handler doesn't call `list_limit()` and isn't
    in `_PAGINATION_NOTES` -- the caller resolves that against the route's
    `(method, path)` and `_PAGINATION_NOTES` directly; this function only
    covers the mechanically-derivable half.
    """
    fn = _unwrap_endpoint(endpoint)
    try:
        source = inspect.getsource(fn)
    except (OSError, TypeError):
        return None
    if _uses_list_limit_helper(source):
        return PaginationNote(
            PaginationShape.LIST_LIMIT_HELPER,
            "`_deps.list_limit()`: `limit`+`offset`, default 500, cap 2000",
        )
    return None


def looks_like_a_list_route(endpoint: Any) -> bool:
    """Loose, over-inclusive signal used only by the completeness test."""
    fn = _unwrap_endpoint(endpoint)
    try:
        source = inspect.getsource(fn)
    except (OSError, TypeError):
        return False
    return _reads_pagination_query_param(source)


__all__ = [
    "_PAGINATION_NOTES",
    "PaginationNote",
    "PaginationShape",
    "classify_pagination",
    "looks_like_a_list_route",
]
