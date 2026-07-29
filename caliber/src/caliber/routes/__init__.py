"""HTTP route registration for the CALIBER plugin.

The :func:`register_routes` entry point is the single place ``server.py`` calls
to wire up all of CALIBER's routes onto its Starlette application. Subsequent
phases add modules under this package (verification, jobs, workflows,
dashboard) and register them here. Keeping registration centralized makes the
total API surface auditable in one file.
"""

from __future__ import annotations

from starlette.applications import Starlette

from caliber.routes import (
    agents,
    aria_plans,
    assistant,
    audit,
    auth,
    capabilities,
    csrf,
    dashboard,
    eval_datasets,
    evaluations,
    events_stream,
    files,
    gate_verdicts,
    gateway,
    health,
    jobs,
    judges,
    knowledge_bases,
    llm_pricing,
    mcp_servers,
    me,
    memory,
    metrics,
    object_store,
    observability,
    projects,
    prompts,
    releases,
    review_queues,
    rollback,
    services,
    settings,
    skills,
    static,
    system_effects,
    system_services,
    tools,
    workflow_calibration,
    workflow_deployments,
    workflow_runs,
    workflow_versions,
    workflows,
)
from caliber.routes import (
    secrets as secrets_routes,
)


def register_routes(app: Starlette) -> None:
    """Register every CALIBER HTTP route onto the given Starlette app.

    All CALIBER API endpoints live under ``/ajax-api/2.0/mlflow/caliber/``
    (the same-origin path MLflow uses for its own AJAX surface). The SPA
    is served from ``/caliber/`` by :mod:`caliber.routes.static`; client-
    side routes (``/caliber/approvals``, ``/caliber/jobs/...``) all
    resolve to the same SPA shell with React Router taking over.
    """
    health.register(app)
    auth.register(app)
    secrets_routes.register(app)
    csrf.register(app)
    me.register(app)
    capabilities.register(app)
    settings.register(app)
    agents.register(app)
    skills.register(app)
    eval_datasets.register(app)
    evaluations.register(app)
    judges.register(app)
    gate_verdicts.register(app)
    llm_pricing.register(app)
    review_queues.register(app)
    aria_plans.register(app)
    jobs.register(app)
    rollback.register(app)
    releases.register(app)
    dashboard.register(app)
    prompts.register(app)
    knowledge_bases.register(app)
    events_stream.register(app)
    metrics.register(app)
    observability.register(app)
    audit.register(app)
    gateway.register(app)
    system_effects.register(app)
    system_services.register(app)
    # Workflow Studio (plan §15). Registered before the SPA catch-all so the
    # API paths resolve to handlers rather than the static shell.
    tools.register(app)
    mcp_servers.register(app)
    memory.register(app)
    object_store.register(app)
    workflows.register(app)
    workflow_calibration.register(app)
    workflow_versions.register(app)
    workflow_runs.register(app)
    workflow_deployments.register(app)
    services.register(app)
    # File/workspace storage routes (storage doc §4.7). Before the SPA catch-all.
    files.register(app)
    projects.register(app)
    assistant.register(app)
    static.register(app)
