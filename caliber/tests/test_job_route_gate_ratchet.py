"""Ratchet: every job-facing route function must apply the project gate.

``CaliberRefinementJob`` has no ``project_id``, so the mechanical bare-lookup
inventory (``routes/bare_lookup_inventory.py``, derived from table columns)
cannot cover it. This is the equivalent net for jobs: in the route modules that
serve jobs, any function that fetches or lists ``CaliberRefinementJob`` must also
call ``load_visible_job`` or ``scope_jobs_to_visible_agents`` (see
``routes/_job_access.py``). A new route that reads a job by id, or a new list
query, fails here until it is gated or explicitly allow-listed with a reason.

Alpha gap P0-1: before this gate existed, every one of these routes was open to
any signed-in user (reads) or any operator (Apply), across projects.
"""

from __future__ import annotations

import ast
import inspect

import caliber.routes.jobs as jobs_module
import caliber.routes.quality_reviews as quality_reviews_module

_GATES = {"load_visible_job", "scope_jobs_to_visible_agents"}

#: ``module.function`` -> why it may touch a job without calling the gate itself.
_ALLOWED: dict[str, str] = {
    "jobs._revert_unstarted_apply": (
        "internal compensation for a failed Apply; its only caller is apply_job, "
        "which has already passed load_visible_job(..., action=ACTION_APPLY) for "
        "this same job id before anything was claimed"
    ),
}


def _touches_jobs(function: ast.AST) -> bool:
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name in {"get", "select", "update"}:
                for arg in node.args:
                    if isinstance(arg, ast.Name) and arg.id == "CaliberRefinementJob":
                        return True
    return False


def _calls_gate(function: ast.AST) -> bool:
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name in _GATES:
                return True
    return False


def _functions(module: object) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    tree = ast.parse(inspect.getsource(module))  # type: ignore[arg-type]
    return [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def test_every_job_route_function_applies_the_project_gate() -> None:
    ungated: list[str] = []
    gate_callers: set[str] = set()
    for module in (jobs_module, quality_reviews_module):
        short = module.__name__.rsplit(".", 1)[-1]
        for function in _functions(module):
            key = f"{short}.{function.name}"
            if _calls_gate(function):
                gate_callers.add(key)
            if _touches_jobs(function) and not _calls_gate(function) and key not in _ALLOWED:
                ungated.append(key)
    assert not ungated, (
        "functions that read or change CaliberRefinementJob without "
        f"load_visible_job / scope_jobs_to_visible_agents: {ungated}"
    )
    # Sanity: the walk really found every gated entry point -- list, detail,
    # targets, Apply, request-changes, and quality-review submit and list.
    assert gate_callers == {
        "jobs.list_jobs",
        "jobs.get_job",
        "jobs.get_job_targets",
        "jobs.apply_job",
        "jobs._request_changes_sync",
        "quality_reviews._submit_quality_review_sync",
        "quality_reviews._list_quality_reviews_sync",
    }


def test_the_walker_flags_an_ungated_job_lookup() -> None:
    tree = ast.parse(
        "def sneaky(session, job_id):\n    return session.get(CaliberRefinementJob, job_id)\n"
    )
    function = tree.body[0]
    assert _touches_jobs(function)
    assert not _calls_gate(function)


def test_the_walker_accepts_a_gated_job_lookup() -> None:
    tree = ast.parse(
        "def fine(session, identity, job_id):\n"
        "    load_visible_job(session, identity, job_id)\n"
        "    return session.get(CaliberRefinementJob, job_id)\n"
    )
    function = tree.body[0]
    assert _touches_jobs(function) and _calls_gate(function)
