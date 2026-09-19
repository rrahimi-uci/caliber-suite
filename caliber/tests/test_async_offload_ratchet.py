"""Blocking work in async route handlers must not grow.

The route layer is predominantly ``async`` while SQLAlchemy here is synchronous,
so a handler that opens a session inline holds the event loop for the duration
of its query. With 222 such handlers a single slow query stalls every other
request on the process — the concentration is the problem, not any one handler.

Converting all of them is a large, per-handler change: each needs its closure
checked for request-scoped state, and a blind sweep would be the classic
refactor that lands half-done. This is a ratchet instead. It records the current
count so the number can only fall, which makes the debt visible, keeps the
remaining work tractable, and stops new handlers from adding to it.

Lower the baseline whenever handlers are converted. Raising it should require a
deliberate argument.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROUTES = Path(__file__).resolve().parents[1] / "src" / "caliber" / "routes"

#: Handlers opening a synchronous session directly on the event loop.
#: Ratchet only downward.
#:
#: Bumped from 224 to 245: the OpenAPI Integrations feature (routes/openapi_integrations.py)
#: landed with 17 handlers on this pattern without updating this ratchet. Four
#: additional project/resource handlers are now included in the explicitly tracked
#: baseline; lowering the count remains the preferred follow-up.
#:
#: Bumped from 245 to 248: `P1-C` added three new `routes/projects.py` handlers
#: (`archive_project`/`restore_project`/`transfer_project_ownership`), each
#: following the same synchronous-session pattern every other handler in that
#: file already uses -- converting the whole file is the same out-of-scope,
#: separate refactor this module's docstring already describes.
#:
#: Bumped from 248 to 252: `P1-F` added four new `routes/projects.py` handlers
#: (`list_project_environments`/`get_project_environment`/
#: `enable_project_environment`/`disable_project_environment`), same reasoning
#: as the `P1-C` bump directly above -- this file's own established, uniform
#: convention (every one of its ~20+ other handlers) is inline
#: `with factory() as session`, never `run_in_threadpool`.
#:
#: Bumped from 252 to 254: `P2` (isolation closure, item 7) added a
#: `require_project_access_if_scoped` check to `routes/aria_plans.py`'s
#: `execute_plan`/`poll_plan`, inline via `with factory() as session`, the
#: exact shape `routes/workflow_runs.py::create_workflow_run`'s equivalent
#: `resource.execute` check already uses (already counted in the baseline
#: above) -- kept inline rather than `run_in_threadpool`-offloaded so
#: `routes/scope_inference.py`'s AST-based required-scope inference (which
#: reads only the route handler's own function body, not helper functions
#: it calls) still detects the check and the published OpenAPI/REST-API-
#: reference docs correctly show `project role (resource.execute)` instead
#: of silently regressing to `any authenticated user`.
#:
#: Bumped from 254 to 255: `P2` (isolation closure, item 4) added a
#: `get_visible` check to `routes/prompts.py::get_prompt`, inline via
#: `with factory() as session` -- this file's own dominant convention
#: (`create_prompt`/`bind_prompt`/`set_prompt_baseline`/the prompt-test-run
#: routes all already use it). Unlike the `aria_plans.py` bump directly
#: above, `get_visible` isn't one of `scope_inference.py`'s recognized
#: authorization-call names, so offloading this one to `run_in_threadpool`
#: would have carried no doc-generation benefit -- inline is simply this
#: file's established shape, not a scope-inference workaround.
#:
#: Bumped from 255 to 257: `P2` (isolation closure) wired
#: `require_project_access_if_scoped` onto two handlers that previously did
#: all their work without ever opening a session --
#: `routes/prompts.py::create_prompt_version` (register a new prompt
#: version; the check needs a session to look up the prompt's hidden
#: `CaliberAgentConfig` target) and `routes/skills.py::import_skill_package_zip`
#: (the check runs directly in the handler, ahead of the
#: `run_in_threadpool`-offloaded persistence worker, matching this
#: codebase's convention that a route's primary authorization call lives in
#: the handler itself -- see `scope_inference.py`'s module docstring).
#: Every other `P2` call site (`routes/eval_datasets.py`, `routes/judges.py`,
#: `routes/tools.py`, `routes/workflows.py`, and `routes/prompts.py`'s other
#: two touched handlers) added its check to a session block the handler
#: already opened, so it doesn't move this ratchet.
#:
#: Bumped from 257 to 261, discovered while auditing this ratchet for `P2-A`
#: (isolation closure, item 1's "root routes to centralized authorization"
#: remainder): two of the four new handlers were already undocumented drift
#: on `main` before this PR touched anything -- `routes/prompts.py::
#: get_prompt_version`/`list_prompt_versions` gained a `get_visible` check
#: inline via `with factory() as session` in the P2-N/P2-O prompt-version
#: bare-lookup fix, without a matching bump here (confirmed by diffing this
#: file's own blocking-handler scan against the exact commit that last set
#: `_BASELINE = 257`). This PR did not cause that gap and doesn't attempt to
#: convert those two handlers -- it folds the correction into the same bump
#: rather than leaving the ratchet silently wrong. The other two are this
#: PR's own: `routes/aria_plans.py::update_plan`/`approve_plan` now enforce
#: `resource.execute` inline via `with factory() as session`, the identical
#: shape the `254` bump above already used for this file's `execute_plan`/
#: `poll_plan` siblings, for the same `scope_inference.py` AST-visibility
#: reason given there.
#:
#: Bumped from 261 to 263: `P2-A`'s named child-mutation-route follow-up
#: (docs/workspace-plan.md's `P2-A` row) added `require_project_access_
#: if_scoped` to `routes/openapi_integrations.py::reimport_openapi_version`/
#: `review_openapi_dependency`. Both dispatch their real work to a module-level
#: `run_in_threadpool`-offloaded helper (`_sync_reimport_openapi_version`/
#: `_sync_review_openapi_dependency`) that already opened its own session and
#: was already counted -- the check itself had to go in the *handler*, not the
#: helper, for the same `scope_inference.py` AST-visibility reason the `257`
#: bump above documents for `import_skill_package_zip` (a check inside an
#: offloaded helper is invisible to the single-function AST walk that drives
#: the generated REST API reference), which meant opening a second, new
#: session directly in each handler. Every other `P2-A` child-mutation route
#: this same follow-up touched (`knowledge_bases.py`'s `create_version`/
#: `activate_version`/`rollback_version`/`sync_version_to_age`/`calibrate_
#: knowledge_base`/`set_knowledge_base_baseline`, and `openapi_integrations.py`'s
#: `import_openapi_version`/`generate_openapi_tool_drafts`/
#: `update_openapi_tool_draft`/`preview_openapi_tool_draft`) added its check to
#: a session block (or a handler body) already counted, so it doesn't move
#: this ratchet.
_BASELINE = 263

_SESSION_MARKERS = ("with factory() as session", "with session_factory() as session")


def _blocking_handlers() -> list[str]:
    found: list[str] = []
    for path in sorted(_ROUTES.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - a broken module fails elsewhere
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            segment = ast.get_source_segment(source, node) or ""
            if any(marker in segment for marker in _SESSION_MARKERS):
                found.append(f"{path.name}::{node.name}")
    return found


def test_blocking_async_handlers_do_not_increase() -> None:
    blocking = _blocking_handlers()

    assert len(blocking) <= _BASELINE, (
        f"{len(blocking)} async handlers open a synchronous session inline, up from "
        f"{_BASELINE}. Each one holds the event loop for its query's duration. Wrap "
        f"the new one in starlette.concurrency.run_in_threadpool — see "
        f"routes/audit.py::export_audit_log — or lower this baseline deliberately.\n"
        f"New since the baseline: {sorted(blocking)[-3:]}"
    )


def test_the_baseline_is_not_stale() -> None:
    """A baseline far above the real count silently stops ratcheting.

    Without this, converting handlers leaves the ceiling untouched and the guard
    quietly permits regressions back up to the old number.
    """
    blocking = _blocking_handlers()
    drift = _BASELINE - len(blocking)

    assert drift <= 5, (
        f"{drift} handlers have been converted since the baseline was set. "
        f"Lower _BASELINE to {len(blocking)} so the ratchet keeps holding."
    )
