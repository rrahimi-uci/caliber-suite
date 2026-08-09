"""GA routes returning unmodelled payloads must not grow.

A typed SDK can only be generated for endpoints whose payloads are declared.
Where a handler returns ``envelope_response_dict(...)`` with an ad-hoc dict, the
shape exists only in the handler body: it cannot be published in the OpenAPI
document, cannot be generated into a client, and changes without anything
noticing.

Converting all of them at once is the refactor that lands half-done — each
handler needs its real shape established, and several assemble their payload
across branches. This is a ratchet instead, matching
``test_async_offload_ratchet``: it records the current count so the number can
only fall, which keeps the debt visible and stops new GA handlers adding to it.

Lower the baseline whenever handlers are converted. Raising it should require a
deliberate argument.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROUTES = Path(__file__).resolve().parents[1] / "src" / "caliber" / "routes"

#: Route modules in the SDK's GA tier (see ``caliber.routes.openapi._STABILITY``).
#: Beta and internal modules are excluded deliberately: the ratchet exists to
#: protect the surface an SDK promises compatibility for, and pulling in every
#: module would make the number too coarse to act on.
_GA_MODULES = (
    "agents",
    "auth",
    "capabilities",
    "csrf",
    "eval_datasets",
    "evaluations",
    "files",
    "judges",
    "me",
    "projects",
    "prompts",
    "services",
    "settings",
    "skills",
    "tools",
    "workflow_deployments",
    "workflow_runs",
    "workflow_versions",
    "workflows",
)

#: GA handlers returning an unmodelled dict payload. Ratchet only downward.
#: 52 -> 43 when projects.py was formalized (M0-PR2, tranche 1).
_BASELINE = 43


def _unmodelled_returns() -> list[str]:
    """Handlers whose response payload is an ad-hoc dict rather than a schema.

    Counted by call rather than by handler: one handler can return different
    shapes on different branches, and each is a separate undeclared contract.
    """
    found: list[str] = []
    for name in _GA_MODULES:
        path = _ROUTES / f"{name}.py"
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - a broken module fails elsewhere
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if called == "envelope_response_dict":
                found.append(f"{path.name}:{node.lineno}")
    return found


def test_unmodelled_ga_responses_do_not_increase() -> None:
    unmodelled = _unmodelled_returns()

    assert len(unmodelled) <= _BASELINE, (
        f"{len(unmodelled)} GA responses return an unmodelled dict, up from {_BASELINE}. "
        "A payload declared only inside a handler cannot be published in the OpenAPI "
        "document or generated into an SDK client. Define a schema in caliber/schemas.py "
        "and return it through envelope_response(), or lower this baseline deliberately.\n"
        f"Sites: {sorted(unmodelled)}"
    )


def test_the_ratchet_watches_a_surface_that_actually_exists() -> None:
    """A typo in ``_GA_MODULES`` would silently shrink what is being measured."""
    missing = [name for name in _GA_MODULES if not (_ROUTES / f"{name}.py").is_file()]
    assert not missing, f"_GA_MODULES names modules that do not exist: {missing}"


def test_the_baseline_is_not_slack() -> None:
    """The recorded baseline must be the real count, not a comfortable ceiling.

    A baseline set above the truth silently permits regressions up to the gap,
    which is the failure mode that makes a ratchet worse than no test at all.
    """
    assert len(_unmodelled_returns()) == _BASELINE, (
        f"the baseline no longer matches the real count; lower it to {len(_unmodelled_returns())}"
    )
