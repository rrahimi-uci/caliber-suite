"""Tests for the machine-readable pagination-shape inventory (`P0-B`).

Confirms the inventory covers every live list route with a definite
classification (nothing paginated-looking falls through uninventoried), and
pins today's exact shape distribution as a ratchet -- a route changing shape,
or a new list route appearing, is a real event this test surfaces by name
rather than letting it drift past silently.
"""

from __future__ import annotations

from starlette.routing import Route
from starlette.testclient import TestClient

from caliber.routes.openapi import PREFIX
from caliber.routes.pagination_inventory import (
    _PAGINATION_NOTES,
    PaginationShape,
    classify_pagination,
    looks_like_a_list_route,
)


def _live_operations(app) -> list[tuple[str, str, object]]:
    """(method, path, endpoint) for every management-API operation."""
    live: list[tuple[str, str, object]] = []
    for route in app.routes:
        if not isinstance(route, Route) or not route.path.startswith(PREFIX):
            continue
        for method in sorted((route.methods or set()) - {"HEAD", "OPTIONS"}):
            live.append((method, route.path, route.endpoint))
    return live


def _normalized(app) -> dict[tuple[str, str], object]:
    from caliber.routes.openapi import _normalize_path

    return {
        (method.lower(), _normalize_path(path)): endpoint
        for method, path, endpoint in _live_operations(app)
    }


def test_every_pagination_looking_route_has_a_definite_classification(
    client: TestClient,
) -> None:
    """A route that reads a `limit`/`offset`/`cursor` query param must
    resolve to a classification -- either detected as a `list_limit()` user,
    or present in `_PAGINATION_NOTES`. Otherwise it fails by name, the same
    discipline `test_route_scope_inventory.py` uses for scope classification.
    """
    app = client.app
    unclassified: list[str] = []
    for method, path, endpoint in _live_operations(app):
        if not looks_like_a_list_route(endpoint):
            continue
        key = (method.lower(), path)
        mechanical = classify_pagination(endpoint)
        if mechanical is not None:
            continue
        if key in _PAGINATION_NOTES:
            continue
        unclassified.append(
            f"{method} {path} reads a pagination-looking query param but has no "
            f"classification -- add it to _PAGINATION_NOTES in "
            f"caliber/routes/pagination_inventory.py"
        )
    assert not unclassified, "\n".join(unclassified)


def test_stale_pagination_notes_do_not_linger(client: TestClient) -> None:
    """A note for a route that no longer exists, or one that now uses the
    shared `list_limit()` helper, is a stale claim."""
    from caliber.routes.openapi import _normalize_path

    app = client.app
    live_keys = {
        (method.lower(), _normalize_path(path)) for method, path, _ in _live_operations(app)
    }
    stale_gone = sorted(set(_PAGINATION_NOTES) - live_keys)
    assert not stale_gone, f"_PAGINATION_NOTES names routes that no longer exist: {stale_gone}"

    normalized = _normalized(app)
    stale_now_helper = [
        key
        for key in _PAGINATION_NOTES
        if key in normalized and classify_pagination(normalized[key]) is not None
    ]
    assert not stale_now_helper, (
        f"these routes now use list_limit() and no longer need a hand-maintained "
        f"note: {sorted(stale_now_helper)}"
    )


def test_the_pagination_shape_distribution_is_pinned(client: TestClient) -> None:
    """Ratchet, matching this session's inventory-test style: pins today's
    exact shape distribution across all classified list routes. A route
    changing shape, or a new list route appearing under a shape other than
    `list_limit_helper`, is a real event that must update this pin
    deliberately."""
    app = client.app
    normalized = _normalized(app)

    counts: dict[PaginationShape, int] = {}
    for key, endpoint in normalized.items():
        mechanical = classify_pagination(endpoint)
        if mechanical is not None:
            counts[mechanical.shape] = counts.get(mechanical.shape, 0) + 1
        elif key in _PAGINATION_NOTES:
            shape = _PAGINATION_NOTES[key].shape
            counts[shape] = counts.get(shape, 0) + 1

    assert counts == {
        PaginationShape.LIST_LIMIT_HELPER: 10,
        PaginationShape.AD_HOC_LIMIT_ONLY: 12,
        PaginationShape.BESPOKE_TOTAL_ENVELOPE: 1,
        PaginationShape.UNPAGINATED_FAKE_TOTAL: 2,
        PaginationShape.CURSOR: 1,
        PaginationShape.SEQUENCE_WATERMARK: 2,
    }


def test_list_limit_helper_routes_spot_check(client: TestClient) -> None:
    """Regression pin against known real handlers, catching a bug in the
    AST detector itself, not just missing coverage."""
    app = client.app
    normalized = _normalized(app)
    for method, path in [
        ("get", PREFIX + "/agents"),
        ("get", PREFIX + "/jobs"),
        ("get", PREFIX + "/judges"),
        ("get", PREFIX + "/skills"),
    ]:
        from caliber.routes.openapi import _normalize_path

        key = (method, _normalize_path(path))
        result = classify_pagination(normalized[key])
        assert result is not None
        assert result.shape is PaginationShape.LIST_LIMIT_HELPER


def test_the_one_cursor_route_is_correctly_classified(client: TestClient) -> None:
    from caliber.routes.openapi import _normalize_path

    app = client.app
    normalized = _normalized(app)
    key = ("get", _normalize_path(PREFIX + "/workflows/{workflow_id}/runs"))
    assert _PAGINATION_NOTES[key].shape is PaginationShape.CURSOR
    assert key in normalized  # the route is actually live today
