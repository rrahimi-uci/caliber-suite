"""Shared pytest fixtures.

The fixture stack:

* ``app_config`` — a :class:`CaliberConfig` pointing at an in-memory SQLite DB
  (one DB per test for isolation).
* ``engine`` — built from ``app_config``, with ``Base.metadata.create_all``
  applied so the schema matches a fresh ``alembic upgrade head``.
* ``session_factory`` — bound to the engine.
* ``app`` / ``client`` — the CALIBER ASGI app with ``app.state`` rewired to
  point at the fixture engine, accessed via Starlette's TestClient.

The TestClient is httpx-backed and in-process — no network, no port binding,
fast. ``with TestClient(app)`` also triggers the Starlette lifespan, so the
feedback poller starts and stops as part of every request-driven test.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Disable MLflow's telemetry/usage tracking for the whole test run. MLflow 3.x
# phones home to ``config.mlflow-telemetry.io`` / CloudFront over HTTPS; the
# connection's socket is left unclosed and, under ``filterwarnings = error``,
# the eventual ``ResourceWarning: unclosed <ssl.SSLSocket ...>`` fails whatever
# test happens to be running when the GC collects it — a load-dependent flake.
# Tests must never phone home, so disable it before any ``mlflow`` import.
os.environ.setdefault("MLFLOW_DISABLE_TELEMETRY", "true")
os.environ.setdefault("DO_NOT_TRACK", "true")

# Isolate MLflow's tracking/registry store to a throwaway temp file for the test
# session. Without this, any test that touches real ``mlflow`` (e.g. importing
# ``mlflow.genai``) creates a stray ``./mlflow.db`` in the repo cwd — and a dev
# shell that exports a live ``MLFLOW_TRACKING_URI`` could have tests write to a
# real registry. Integration tests (``CALIBER_INTEGRATION_TESTS=1``) opt out and
# set their own per-test URI.
_MLFLOW_TEST_ROOTS: list[Path] = []
if os.environ.get("CALIBER_INTEGRATION_TESTS") != "1":
    # Isolate the MLflow store per xdist worker so parallel runs (`pytest -n auto`)
    # don't lock-contend on one shared SQLite file. ``PYTEST_XDIST_WORKER`` is
    # ``gw0``/``gw1``/... under xdist and unset for a serial run (→ ``main``).
    _worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    _mlflow_test_root = Path(
        tempfile.mkdtemp(prefix=f"caliber-test-mlflow-{_worker}-{os.getpid()}-")
    )
    _MLFLOW_TEST_ROOTS.append(_mlflow_test_root)
    _mlflow_store = f"sqlite:///{_mlflow_test_root / 'mlflow.db'}"
    os.environ["MLFLOW_TRACKING_URI"] = _mlflow_store
    os.environ["MLFLOW_REGISTRY_URI"] = _mlflow_store
    # MLflow's SQL tracking-store factory otherwise defaults artifacts to
    # ``./mlruns`` even when the database URI is isolated. This internal env
    # name is what MLflow 3.14's tracking-service factory reads.
    os.environ["_MLFLOW_SERVER_ARTIFACT_ROOT"] = (_mlflow_test_root / "artifacts").as_uri()
    # Tests assert synchronous outcomes. Background trace exporters can outlive
    # their test and race teardown, producing cross-test flakes and stray I/O.
    os.environ["MLFLOW_ENABLE_ASYNC_TRACE_LOGGING"] = "false"


def _cleanup_mlflow_test_root() -> None:
    """Drain MLflow trace work and remove this process's isolated test root."""
    if not _MLFLOW_TEST_ROOTS:
        return
    root = _MLFLOW_TEST_ROOTS.pop()
    try:
        if "mlflow" in sys.modules:
            import mlflow

            flush = getattr(mlflow, "flush_trace_async_logging", None)
            if callable(flush):
                flush(terminate=True)
    except Exception:
        pass  # teardown must not mask the test result
    finally:
        shutil.rmtree(root, ignore_errors=True)


atexit.register(_cleanup_mlflow_test_root)

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

import caliber.server as caliber_server
from caliber.config import CaliberConfig
from caliber.db import Base
from caliber.db.session import create_engine_from_config, sessionmaker_from_engine
from caliber.observability.mlflow_tracing import set_tracer
from caliber.server import create_app

# ---------------------------------------------------------------------------
# Allure categorisation — auto-label every backend test with a functional
# "feature" (Allure's Behaviors tab) so the report groups the suite by area
# instead of one flat list. Keyword → feature, first match on the module stem
# wins; anything unmatched falls back to a humanised module name so it's still
# categorised rather than dumped into "tests".
# ---------------------------------------------------------------------------
_FEATURE_RULES: tuple[tuple[str, str], ...] = (
    ("observability", "Observability"),
    ("trace", "Observability"),
    ("metric", "Observability"),
    ("system_services", "Platform Services"),
    ("eval", "Evaluations"),
    ("gateway", "LLM Gateway"),
    ("workflow", "Workflows"),
    ("orchestrat", "Workflows"),
    ("knowledge", "Knowledge Base"),
    ("skill", "Skills"),
    ("mcp", "MCP Servers"),
    ("tool", "Tools"),
    ("prompt", "Prompts"),
    ("assistant", "Assistant"),
    ("memory", "Agent Memory"),
    ("object_store", "Object Storage"),
    ("storage", "Object Storage"),
    ("rollback", "Rollback"),
    ("approval", "Approvals & Governance"),
    ("verification", "Approvals & Governance"),
    ("guardrail", "Approvals & Governance"),
    ("job", "Refinement Jobs"),
    ("refinement", "Refinement Jobs"),
    ("dashboard", "Dashboard"),
    ("project", "Projects"),
    ("auth", "Auth & RBAC"),
    ("rbac", "Auth & RBAC"),
    ("secret", "Auth & RBAC"),
    ("gepa", "Optimizers"),
    ("dspy", "Optimizers"),
    ("mipro", "Optimizers"),
    ("optimiz", "Optimizers"),
    ("candidate", "Optimizers"),
    ("config", "Platform Core"),
    ("ids", "Platform Core"),
    ("schema", "Platform Core"),
    ("db", "Database"),
    ("migration", "Database"),
    # Catch-all for the coverage-only test modules — kept LAST so a real feature
    # keyword (e.g. test_observability_coverage) wins over this generic bucket.
    ("coverage", "Coverage & Edge Cases"),
)


def _feature_for(stem: str) -> str:
    name = stem.lower()
    for keyword, feature in _FEATURE_RULES:
        if keyword in name:
            return feature
    # Fallback: humanise the module stem (drop test_ prefix/suffix).
    cleaned = name.removeprefix("test_").removesuffix("_test").replace("_", " ").strip()
    return cleaned.title() or "Other"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Tag each collected test with an Allure epic + feature for the report.

    No-op when allure-pytest isn't active (normal ``make test`` runs without
    ``--alluredir``) — the markers are harmless either way.
    """
    try:
        import allure
    except Exception:
        return
    for item in items:
        stem = getattr(getattr(item, "path", None), "stem", "") or ""
        item.add_marker(allure.epic("Backend"))
        item.add_marker(allure.feature(_feature_for(stem)))


# Defect buckets for Allure's **Categories** tab. Order matters: a result is
# placed in the first category it matches, so specific rules (flaky, infra)
# precede the catch-alls. ``allure generate`` reads ``categories.json`` from the
# results dir and applies it report-wide (one file governs the merged report).
_ALLURE_CATEGORIES: list[dict[str, object]] = [
    {"name": "Flaky tests", "matchedStatuses": ["failed", "broken"], "flaky": True},
    {
        "name": "Infrastructure problems",
        "matchedStatuses": ["broken", "failed"],
        "messageRegex": ".*(ConnectionError|[Cc]onnection refused|ECONNREFUSED|"
        "timed? ?out|TimeoutError|Could not connect|OperationalError|"
        "Address already in use|Broken pipe|getaddrinfo).*",
    },
    {"name": "Product defects", "matchedStatuses": ["failed"]},
    {"name": "Test defects (broken)", "matchedStatuses": ["broken"]},
    {"name": "Skipped / known issues", "matchedStatuses": ["skipped"]},
]


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Clean test MLflow state and optionally write Allure categories.

    When running with ``--alluredir``, the combined report (``allure generate``)
    merges ``categories.json`` so the Categories tab is populated for every
    suite.
    """
    _cleanup_mlflow_test_root()
    alluredir = session.config.getoption("--alluredir", default=None)
    if not alluredir:
        return
    import json as _json

    target = Path(str(alluredir))
    try:
        target.mkdir(parents=True, exist_ok=True)
        (target / "categories.json").write_text(
            _json.dumps(_ALLURE_CATEGORIES, indent=2), encoding="utf-8"
        )
    except OSError:
        pass  # report categorisation is best-effort, never fail the run


# The default user identity tests run as. Granted ``caliber.admin`` in
# the default ``app_config`` so existing tests keep passing once scope
# checks land on every write endpoint. Tests that exercise the
# unauthorized branches override the header explicitly.
DEFAULT_TEST_USER = "@test"

# Other user identities tests rely on for audit-log assertions
# (``actor == "@sarah"`` and friends). They're all granted admin scope
# so the audit shape stays unchanged after RBAC enforcement landed.
# Tests that need 401/403 branches use names *not* in this list —
# e.g. ``@notadmin`` and ``@viewer``.
_PERMISSIVE_TEST_USERS = "@test,@admin,@reza,@sarah,@alex,@a,@b"


@pytest.fixture(autouse=True)
def _clear_prompt_info_cache() -> Iterator[None]:
    """Reset the cross-request prompt-registry cache around every test.

    ``list_prompts`` caches resolved registry records for a few seconds so
    repeated page loads don't re-hit MLflow. Tests stub MLflow per-test (often
    reusing the same prompt names with different fixtures), so the cache must be
    empty at each boundary or one test's records bleed into the next.
    """
    from caliber.routes import prompts as _prompt_routes

    _prompt_routes._reset_prompt_info_cache()
    yield
    _prompt_routes._reset_prompt_info_cache()


@pytest.fixture(autouse=True)
def _reset_pricing_source() -> Iterator[None]:
    """Reset the process-wide per-model pricing source + cache around every test.

    ``create_app`` registers the app's session factory as the cost-attribution
    pricing source, and the resolved table is cached (a short TTL). Across tests
    that each build a throwaway app (and dispose its engine), a stale factory /
    cached override would otherwise bleed into a later test's ``model_cost_usd``.
    Mirror ``_clear_prompt_info_cache``: clear at both boundaries.
    """
    from caliber.observability import mlflow_tracing as _mt

    yield
    # Teardown-only: never clobber a live create_app registration mid-test; just
    # ensure nothing (a disposed factory or a cached override) leaks forward.
    _mt.register_pricing_source(None)
    _mt.invalidate_pricing_cache()


@pytest.fixture(autouse=True)
def _dispose_create_app_engines(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Dispose every engine ``create_app`` builds during a test.

    Several fixtures (``client`` here, plus ``rbac_client`` and friends in other
    test modules) build a throwaway app via ``create_app`` and then swap
    ``app.state`` to a per-test engine — leaking the engine ``create_app`` built
    internally. Disposing it deterministically closes its pooled
    ``sqlite3.Connection`` objects instead of letting them finalize at GC, which
    on Python 3.14 is the main contributor to the benign
    "Exception ignored while finalizing database connection" unraisable. (The
    residual, checked-out-connection case is handled by a targeted
    ``filterwarnings`` entry in ``pyproject.toml``.)

    ``create_app`` calls ``create_engine_from_config`` as a module global, so we
    wrap that one reference, record every engine produced, and dispose them all
    at teardown (idempotent for engines a fixture already disposed).
    """
    created: list[Engine] = []
    real = caliber_server.create_engine_from_config

    def _tracking(config: CaliberConfig) -> Engine:
        engine = real(config)
        created.append(engine)
        return engine

    monkeypatch.setattr(caliber_server, "create_engine_from_config", _tracking)
    try:
        yield
    finally:
        for engine in created:
            engine.dispose()


@pytest.fixture(autouse=True)
def _reset_tracer_singleton() -> Iterator[None]:
    """Reset the process-wide MLflow tracer between tests.

    ``create_app`` calls ``configure_tracing`` which sets a module-global,
    *enabled* tracer (tracing defaults on). Without this reset that would leak
    across tests — so a test running after one that built the app would start
    emitting real MLflow spans from instrumented code paths. Resetting to the
    inert default keeps tracing opt-in per test; tracing tests inject their own.
    """
    set_tracer(None)
    try:
        yield
    finally:
        set_tracer(None)


@pytest.fixture
def app_config(tmp_path: Path) -> CaliberConfig:
    """Per-test config pointing at a fresh file-based SQLite database.

    We previously used the shared in-memory cache (``file::memory:?cache=
    shared&uri=true``), but :func:`caliber.server.create_app` builds its
    *own* engine and session_factory from the config it receives — even
    though tests override ``app.state.engine`` afterwards, the worker /
    poller / janitor were already constructed with the internal factory.
    That gave us two separate connection pools racing on the same shared
    cache, with intermittent ``database table is locked`` failures as
    the writes interleaved.

    A file in ``tmp_path`` (pytest's per-test directory) sidesteps the
    problem: the two pools still both talk to the same file, but SQLite's
    file-level locking is robust where its in-memory cache locking is
    flaky. Slight cost in test runtime, large reduction in flakiness.

    The default test user (:data:`DEFAULT_TEST_USER`) is configured as a
    CALIBER admin so existing tests keep passing once scope checks land
    on every write endpoint. The :func:`client` fixture below sets the
    user header automatically; tests that need the 401/403 branches
    pass a different ``X-CALIBER-User`` header on the request.
    """
    db_path = tmp_path / "caliber.db"
    return CaliberConfig.load(
        environ={
            "CALIBER_DATABASE_URL": f"sqlite+pysqlite:///{db_path}",
            "CALIBER_ADMIN_USERS": _PERMISSIVE_TEST_USERS,
            # The shared test venv currently carries flagged upstream versions in
            # the optional DSPy and local-embedding stacks. Keep the default app
            # fixture opted into those paths so unrelated route/runtime tests stay
            # focused on their own behavior; targeted security tests exercise the
            # production default-off guard explicitly.
            "CALIBER_ALLOW_FLAGGED_DSPY_OPTIMIZERS": "true",
            "CALIBER_ALLOW_FLAGGED_LOCAL_EMBEDDINGS": "true",
            # Unit/route tests run with background loops OFF so the refinement
            # worker can't race their assertions by claiming seeded jobs. The
            # ``worker_client`` fixture re-enables them for end-to-end tests.
            "CALIBER_BACKGROUND_TASKS_ENABLED": "false",
            # Production now defaults the assistant to a real provider (auto →
            # OpenAI/Claude). Tests have no API keys, so pin the deterministic
            # fake engine here — the one place fake is meant to be used.
            "CALIBER_ASSISTANT_ENGINE": "fake",
            # Isolate the local workflow-storage root to the per-test tmp dir.
            # Without this the default (relative) ``file://./caliber-workspaces``
            # base URI makes any test that writes a workflow file pollute the
            # repo cwd. Same rationale as the SQLite-in-tmp_path choice above.
            "CALIBER_WORKFLOW_STORAGE_BASE_URI": f"file://{tmp_path / 'workspaces'}",
        }
    )


@pytest.fixture
def engine(app_config: CaliberConfig) -> Iterator[Engine]:
    """Build the engine and create all tables from the model metadata.

    We deliberately bypass Alembic here so tests stay fast and isolated from
    migration changes; the migration path itself is exercised by a dedicated
    test in ``test_migrations.py``.
    """
    eng = create_engine_from_config(app_config)
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        Base.metadata.drop_all(eng)
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker_from_engine(engine)


@pytest.fixture
def db_session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Yield a session for direct DB manipulation in arrange-steps of tests."""
    with session_factory() as session:
        yield session


@pytest.fixture
def client(
    app_config: CaliberConfig,
    engine: Engine,
    session_factory: sessionmaker[Session],
) -> Iterator[TestClient]:
    """A Starlette TestClient against a CALIBER app rewired to the fixture DB.

    ``create_app`` builds its own engine from ``app_config``; we then swap in
    the fixture engine and factory on ``app.state`` so the app and the test
    share the same DB connection (instead of each opening their own
    in-memory DB and never seeing each other's rows).
    """
    # The engine ``create_app`` builds internally is disposed centrally by the
    # autouse ``_dispose_create_app_engines`` fixture; here we just rewire
    # ``app.state`` to the fixture engine so app + test share one DB.
    app = create_app(config=app_config)
    app.state.engine = engine
    app.state.session_factory = session_factory
    # Default to the admin test user so existing tests don't need to
    # know about RBAC. Tests that need to exercise the 401/403 paths
    # pass an explicit ``X-CALIBER-User`` header on the request itself,
    # which overrides this default.
    with TestClient(app, headers={"X-CALIBER-User": DEFAULT_TEST_USER}) as test_client:
        yield test_client


@pytest.fixture
def worker_client(
    app_config: CaliberConfig,
    engine: Engine,
    session_factory: sessionmaker[Session],
) -> Iterator[TestClient]:
    """Like :func:`client` but with the background loops (worker/poller/janitor)
    running, for end-to-end tests that submit work via the API and poll for the
    worker to advance it. Shares the per-test SQLite file (the internal engine
    ``create_app`` builds resolves to the same ``database_url``)."""
    worker_config = app_config.model_copy(update={"background_tasks_enabled": True})
    app = create_app(config=worker_config)
    app.state.engine = engine
    app.state.session_factory = session_factory
    with TestClient(app, headers={"X-CALIBER-User": DEFAULT_TEST_USER}) as test_client:
        yield test_client


@pytest.fixture
def gated_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    """Require human approval for ``prod`` promotions.

    Release governance is now keyed to the alias's *environment class*, so this
    sets the shipped configuration switch rather than patching a module constant:
    ``release_require_human_approval_for_environment_classes=production`` is
    exactly what an operator would set. The legacy ``GATED_ALIASES`` frozenset is
    still honoured as an additional opt-in and is set here too, so tests that
    reach the promoter directly (without an app config) behave the same.
    """
    monkeypatch.setattr("caliber.workflows.promoter.GATED_ALIASES", frozenset({"prod"}))
