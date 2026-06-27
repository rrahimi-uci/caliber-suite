"""Tests for the SPA static-file handler.

The handler has three contracts worth pinning:

1. ``/caliber/`` serves ``index.html`` with the runtime static-prefix
   injected into a ``<script>`` tag.
2. ``/caliber/<existing-asset>`` streams the asset directly.
3. ``/caliber/<missing-path>`` falls back to ``index.html`` so client-
   side router deep links survive a hard refresh.
4. Path traversal attempts (``../../etc/passwd`` style) return the SPA
   shell rather than escaping the sandbox.
5. When the UI bundle isn't on disk, the handler returns a 503 with
   operator-facing instructions instead of a generic 404.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from caliber.config import CaliberConfig
from caliber.routes.static import StaticUIHandler, _inject_prefix
from caliber.server import create_app


@pytest.fixture
def ui_dir(tmp_path: Path) -> Path:
    """Build a minimal UI bundle on disk.

    Two files: an ``index.html`` with a real ``</head>`` to splice into,
    and an ``assets/app.js`` that the asset-streaming path can serve.
    """
    root = tmp_path / "ui"
    root.mkdir()
    (root / "index.html").write_text(
        "<!doctype html><html><head><title>CALIBER</title></head>"
        "<body><div id='root'></div></body></html>",
        encoding="utf-8",
    )
    assets = root / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("// dummy bundle\n", encoding="utf-8")
    return root


@pytest.fixture
def client_with_ui(
    app_config: CaliberConfig,
    engine: Engine,
    session_factory: sessionmaker[Session],
    ui_dir: Path,
) -> Iterator[TestClient]:
    """A TestClient against a CALIBER app whose static handler points at the
    fixture ``ui_dir`` instead of the package-bundled location."""
    app = create_app(config=app_config)
    app.state.engine = engine
    app.state.session_factory = session_factory
    # Swap in a handler scoped to the temp dir. The prefix is empty —
    # the prefix-injection test exercises that path separately via the
    # unit-level ``_inject_prefix``.
    app.state.static_ui_handler = StaticUIHandler(ui_dir, static_prefix="")
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Unit tests for _inject_prefix
# ---------------------------------------------------------------------------


def test_inject_prefix_places_script_before_head_close() -> None:
    html = "<html><head><title>x</title></head><body></body></html>"
    patched = _inject_prefix(html, "/mlflow")
    assert '<script>window.__CALIBER_STATIC_PREFIX__="/mlflow";</script>' in patched
    # Script must come before </head>.
    assert patched.index("window.__CALIBER_STATIC_PREFIX__") < patched.index("</head>")


def test_inject_prefix_handles_empty_prefix() -> None:
    patched = _inject_prefix("<html><head></head></html>", "")
    assert 'window.__CALIBER_STATIC_PREFIX__=""' in patched


def test_inject_prefix_is_idempotent() -> None:
    """A second pass should not double-insert the script."""
    once = _inject_prefix("<html><head></head></html>", "/mlflow")
    twice = _inject_prefix(once, "/mlflow")
    assert once == twice


def test_inject_prefix_falls_back_when_head_missing() -> None:
    """Pathological input without </head> still gets the script (prepended)."""
    patched = _inject_prefix("no head", "/mlflow")
    assert patched.startswith("<script>")
    assert "no head" in patched


def test_inject_prefix_escapes_quotes_via_json_encoding() -> None:
    """The prefix is JSON-encoded so an embedded quote can't break out
    of the script tag's string literal."""
    patched = _inject_prefix("<html><head></head></html>", '/m"x')
    # JSON encodes the embedded quote as \".
    assert r"\"" in patched


# ---------------------------------------------------------------------------
# Unit tests for StaticUIHandler.resolve_asset
# ---------------------------------------------------------------------------


def test_resolve_asset_returns_path_for_existing_file(ui_dir: Path) -> None:
    handler = StaticUIHandler(ui_dir, static_prefix="")
    asset = handler.resolve_asset("assets/app.js")
    assert asset is not None
    assert asset == (ui_dir / "assets" / "app.js").resolve()


def test_resolve_asset_returns_none_for_missing_file(ui_dir: Path) -> None:
    handler = StaticUIHandler(ui_dir, static_prefix="")
    assert handler.resolve_asset("does/not/exist.js") is None


def test_resolve_asset_rejects_path_traversal(ui_dir: Path) -> None:
    handler = StaticUIHandler(ui_dir, static_prefix="")
    # Even though /etc/passwd may exist on the test runner, the resolved
    # path lands outside ``ui_dir`` so the containment check rejects it.
    assert handler.resolve_asset("../../../etc/passwd") is None


def test_resolve_asset_empty_path_returns_none(ui_dir: Path) -> None:
    handler = StaticUIHandler(ui_dir, static_prefix="")
    assert handler.resolve_asset("") is None


# ---------------------------------------------------------------------------
# Integration tests against the live TestClient
# ---------------------------------------------------------------------------


def test_caliber_root_serves_index_html(client_with_ui: TestClient) -> None:
    response = client_with_ui.get("/caliber/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<div id='root'></div>" in response.text


def test_caliber_bare_path_serves_index_html_without_redirect(client_with_ui: TestClient) -> None:
    response = client_with_ui.get("/caliber", follow_redirects=False)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<div id='root'></div>" in response.text


def test_service_root_redirects_to_caliber(client_with_ui: TestClient) -> None:
    response = client_with_ui.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers.get("location") == "/caliber/"


def test_service_root_ui_mlflow_redirects_to_mlflow(
    client_with_ui: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MLFLOW_URL", "http://127.0.0.1:5000")
    response = client_with_ui.get("/?ui=mlflow", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers.get("location") == "http://127.0.0.1:5000/"


def test_service_root_ui_mlflow_preserves_extra_query_params(
    client_with_ui: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MLFLOW_URL", "http://127.0.0.1:5000")
    response = client_with_ui.get("/?ui=mlflow&tab=runs", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers.get("location") == "http://127.0.0.1:5000/?tab=runs"


def test_service_root_ui_mlflow_invalid_env_falls_back_default(
    client_with_ui: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MLFLOW_URL", "not-a-url")
    response = client_with_ui.get("/?ui=mlflow", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers.get("location") == "http://127.0.0.1:5000/"


def test_index_cache_refreshes_when_bundle_changes(ui_dir: Path) -> None:
    handler = StaticUIHandler(ui_dir, static_prefix="")
    first = handler.index_html()
    assert "<div id='root'></div>" in first

    (ui_dir / "index.html").write_text(
        "<!doctype html><html><head><title>CALIBER</title></head>"
        "<body><div id='updated-root'></div></body></html>",
        encoding="utf-8",
    )

    second = handler.index_html()
    assert "<div id='updated-root'></div>" in second


def test_caliber_root_injects_static_prefix_global(
    app_config: CaliberConfig,
    engine: Engine,
    session_factory: sessionmaker[Session],
    ui_dir: Path,
) -> None:
    """Verify the prefix injection lands in the served index.

    The app fixture's prefix is empty — we build a custom client whose
    handler is wired with ``/mlflow`` so the served HTML proves the
    static-prefix wiring works end-to-end.
    """
    app = create_app(config=app_config)
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.static_ui_handler = StaticUIHandler(ui_dir, static_prefix="/mlflow")
    with TestClient(app) as client:
        response = client.get("/caliber/")
    assert response.status_code == 200
    assert '<script>window.__CALIBER_STATIC_PREFIX__="/mlflow";</script>' in response.text


def test_existing_asset_is_served_with_correct_body(
    client_with_ui: TestClient,
) -> None:
    response = client_with_ui.get("/caliber/assets/app.js")
    assert response.status_code == 200
    assert "dummy bundle" in response.text
    # FileResponse picks Content-Type from the path extension.
    assert "javascript" in response.headers["content-type"]


def test_spa_shell_served_with_no_cache(client_with_ui: TestClient) -> None:
    """The shell must revalidate on every load — a cached stale shell would pin
    the browser to old content-hashed chunks (the recurring stale-UI bug)."""
    root = client_with_ui.get("/caliber/")
    assert root.headers.get("cache-control") == "no-cache"
    # The deep-link fallback also serves the shell, so it must be no-cache too.
    deep = client_with_ui.get("/caliber/approvals/AP-1")
    assert deep.headers.get("cache-control") == "no-cache"


def test_hashed_assets_served_immutable(client_with_ui: TestClient) -> None:
    """Content-hashed files under ``assets/`` are safe to cache for a year."""
    response = client_with_ui.get("/caliber/assets/app.js")
    assert response.headers.get("cache-control") == "public, max-age=31536000, immutable"


def test_unknown_path_falls_back_to_index_for_spa_history_mode(
    client_with_ui: TestClient,
) -> None:
    """A deep link to a client-side route (e.g. /caliber/approvals/AP-1)
    must serve the SPA shell so React Router can resolve it on the
    client side."""
    response = client_with_ui.get("/caliber/approvals/AP-1")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<div id='root'></div>" in response.text


def test_traversal_attempt_falls_back_to_index(
    client_with_ui: TestClient,
) -> None:
    """A ``..``-laden path resolves outside the sandbox; the handler
    treats it as "no asset" and serves the SPA shell rather than
    leaking files."""
    response = client_with_ui.get("/caliber/../../etc/passwd")
    # Starlette normalizes the URL before routing — the response is
    # either the SPA shell (200) or a 404 if the normalization lands
    # outside the registered routes. Either way, no /etc/passwd content.
    assert response.status_code in {200, 404}
    if response.status_code == 200:
        assert "<div id='root'></div>" in response.text


def test_root_returns_503_when_ui_bundle_absent(
    app_config: CaliberConfig,
    engine: Engine,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    """A pip-installed dev checkout without a built SPA gets a clear
    503 with instructions, not a generic 404."""
    empty = tmp_path / "missing"
    empty.mkdir()  # exists but no index.html
    app = create_app(config=app_config)
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.static_ui_handler = StaticUIHandler(empty, static_prefix="")
    with TestClient(app) as client:
        response = client.get("/caliber/")
    assert response.status_code == 503
    assert "SPA not bundled" in response.text


def test_api_route_is_unaffected_by_spa_routes(client_with_ui: TestClient) -> None:
    """Regression test — the SPA's catch-all ``/caliber/{path}`` route
    must not shadow the API surface under ``/ajax-api/...``."""
    response = client_with_ui.get("/ajax-api/2.0/mlflow/caliber/health")
    assert response.status_code == 200


def test_packaged_ui_index_matches_current_dist_when_built() -> None:
    """Local guard for the generated bundle served by ``caliber.routes.static``."""
    repo_root = Path(__file__).resolve().parents[1]
    dist_index = repo_root / "caliber-ui" / "dist" / "index.html"
    packaged_index = repo_root / "src" / "caliber" / "ui" / "index.html"
    if not dist_index.exists() or not packaged_index.exists():
        pytest.skip("UI dist/package bundle not built in this checkout")
    assert packaged_index.read_text(encoding="utf-8") == dist_index.read_text(encoding="utf-8")
