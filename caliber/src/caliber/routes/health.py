"""Health-check endpoint.

Trivial, but load-bearing: it proves the plugin loaded, the database connection
is reachable (once we wire it in Phase 1.2), and the version reported by the
server matches the installed package. Load balancers, deploy gates, and CI
smoke tests all probe this endpoint, so its shape needs to be stable.
"""

from __future__ import annotations

from sqlalchemy import text
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import caliber

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
    """Report which providers are real vs simulated (``fake``) for the SPA banner.

    Honesty surface (golden-path roadmap, Wave 3): exposes only provider-selector
    enum values + observability flags — never secrets or keys. Public, matching the
    health endpoint's posture. Enveloped (``{"data": ...}``) so the SPA unwraps it.
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
            }
        }
    )


def register(app: Starlette) -> None:
    """Add the health + readiness routes to the given Starlette application."""
    app.routes.append(Route(HEALTH_PATH, healthcheck, methods=["GET"]))
    app.routes.append(Route(READINESS_PATH, readiness, methods=["GET"]))
