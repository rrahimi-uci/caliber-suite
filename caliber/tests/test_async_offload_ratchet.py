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
_BASELINE = 224

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
