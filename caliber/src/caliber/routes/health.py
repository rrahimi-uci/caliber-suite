"""Health-check endpoint.

Trivial, but load-bearing: it proves the plugin loaded, the database connection
is reachable (once we wire it in Phase 1.2), and the version reported by the
server matches the installed package. Load balancers, deploy gates, and CI
smoke tests all probe this endpoint, so its shape needs to be stable.
"""

from __future__ import annotations

import os

from sqlalchemy import text
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import caliber
from caliber.observability.queue_health import collect_queue_health
from caliber.observability.readiness import collect_readiness

HEALTH_PATH = "/ajax-api/2.0/mlflow/caliber/health"
READINESS_PATH = "/ajax-api/2.0/mlflow/caliber/readiness"

# Provider-selector values that mean a real (non-simulated) backend.
_REAL_PROVIDER_VALUES = frozenset({"openai", "anthropic", "mlflow"})


async def healthcheck(request: Request) -> JSONResponse:
    """Return a health payload, including a real DB-reachability probe.

    Probes the database with ``SELECT 1`` so load balancers / deploy gates get a
    truthful signal: a reachable plugin with a dead DB now reports
    ``status: "degraded"`` + ``db: "down"`` with HTTP 503, instead of a
    misleading 200. When the DB is healthy it returns ``status: "ok"`` + 200 as
    before. Future-compatibility note: callers parse ``status`` and ``version``;
    adding fields (``db``) is allowed, removing/renaming the two existing fields
    is a breaking change.
    """
    db_ok = True
    factory = getattr(request.app.state, "session_factory", None)
    if factory is not None:
        try:
            with factory() as session:
                session.execute(text("SELECT 1"))
        except Exception:
            db_ok = False
    return JSONResponse(
        {
            "status": "ok" if db_ok else "degraded",
            "version": caliber.__version__,
            "db": "ok" if db_ok else "down",
        },
        status_code=200 if db_ok else 503,
    )


async def readiness(request: Request) -> JSONResponse:
    """Report provider configuration **and** live dependency readiness.

    This endpoint used to return 200 unconditionally while reporting only
    provider-selector enum values, so an orchestrator wired to it could never
    depool or restart a broken instance. It now also probes the dependencies this
    configuration actually needs — database, MLflow tracking, object storage,
    event broker, and workflow queue/worker liveness — and returns **503** when a
    required one is not ready (see :mod:`caliber.observability.readiness`).

    The existing ``providers`` / ``simulated`` / ``all_real`` / tracing fields are
    preserved unchanged: the SPA honesty banner reads them, and a probe surface
    growing new fields must not break its consumers. Provider simulation
    deliberately does **not** fail readiness — the deterministic fake provider is
    a supported mode, and failing a probe an orchestrator acts on would take a
    working development instance out of service.

    Never exposes secrets or keys.
    """
    config = getattr(request.app.state, "config", None)

    def _provider(name: str) -> str:
        return str(getattr(config, name, "fake") or "fake").strip().lower()

    providers = {
        "llm": _provider("llm_provider"),
        "eval": _provider("eval_provider"),
        "promoter": _provider("promoter_provider"),
        "artifact_store": _provider("artifact_store_provider"),
    }
    simulated = sorted(
        key for key, value in providers.items() if value not in _REAL_PROVIDER_VALUES
    )

    factory = getattr(request.app.state, "session_factory", None)
    queue_health = None
    if factory is not None and bool(getattr(config, "workflow_run_queue_enabled", False)):
        lease = float(getattr(config, "workflow_run_lease_seconds", 60.0) or 60.0)
        max_age = float(getattr(config, "workflow_queue_max_age_seconds", 300.0) or 300.0)
        try:
            with factory() as session:
                queue_health = collect_queue_health(
                    session, lease_seconds=lease, max_queue_age_seconds=max_age
                )
        except Exception:
            queue_health = None

    report = await collect_readiness(
        config=config,
        session_factory=factory,
        environ=dict(os.environ),
        queue_health=queue_health,
    )
    return JSONResponse(
        {
            "data": {
                "providers": providers,
                "simulated": simulated,
                "all_real": not simulated,
                "tracing_enabled": bool(getattr(config, "tracing_enabled", False)),
                "tracing_autolog_enabled": bool(getattr(config, "tracing_autolog_enabled", False)),
                "workflow_llm_judge_enabled": bool(
                    getattr(config, "workflow_llm_judge_enabled", False)
                ),
                **report.to_dict(),
            }
        },
        status_code=200 if report.ready else 503,
    )


def register(app: Starlette) -> None:
    """Add the health + readiness routes to the given Starlette application."""
    app.routes.append(Route(HEALTH_PATH, healthcheck, methods=["GET"]))
    app.routes.append(Route(READINESS_PATH, readiness, methods=["GET"]))
