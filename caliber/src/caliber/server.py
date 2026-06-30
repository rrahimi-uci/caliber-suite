"""MLflow plugin entry point.

CALIBER is loaded by MLflow via the ``mlflow.app`` entry point registered in
``pyproject.toml``:

    [project.entry-points."mlflow.app"]
    caliber = "caliber.server:create_app"

When an operator runs ``mlflow server --app-name caliber``, MLflow imports
this module, calls :func:`create_app`, and mounts the returned ASGI
application alongside MLflow's own routes.

Phase 1.5 wires the DB engine and the background-task lifecycle into the
Starlette app via a lifespan context manager. The lifespan ensures the
background workers stop cleanly on shutdown and the engine disposes its pool.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy import Engine
from starlette.applications import Starlette
from starlette.exceptions import HTTPException

from caliber.artifact_store import ArtifactStore, build_store
from caliber.assistant.plan_worker import AriaPlanWorker
from caliber.audit import configure_redactor
from caliber.config import CaliberConfig
from caliber.csrf import CSRFMiddleware, build_token_manager
from caliber.db.session import create_engine_from_config, sessionmaker_from_engine
from caliber.eval.provider import EvalProvider
from caliber.eval.provider import build_provider as build_eval_provider
from caliber.events.bus import EventBus
from caliber.events.nats_bus import build_event_bus
from caliber.events.webhooks import WebhookDispatcher, build_dispatcher
from caliber.knowledge.worker import KnowledgeBaseWorker
from caliber.llm.provider import LLMProvider, build_provider
from caliber.observability.logging import configure_logging
from caliber.observability.mlflow_tracing import configure_tracing, register_pricing_source
from caliber.observability.trace import TraceIdMiddleware
from caliber.orchestrator.janitor import JanitorTask
from caliber.orchestrator.scheduler import WorkflowSchedulerTask
from caliber.orchestrator.worker import RefinementWorker
from caliber.orchestrator.workflow_run_worker import WorkflowRunWorker
from caliber.promoter import Promoter, build_promoter
from caliber.rate_limit import RateLimitMiddleware, build_limiter
from caliber.redaction import build_redactor
from caliber.routes import register_routes
from caliber.routes._errors import http_exception_handler, validation_error_handler
from caliber.routes.csrf import CSRF_PATH
from caliber.routes.health import HEALTH_PATH
from caliber.routes.static import build_handler as build_static_ui_handler
from caliber.runtime_advisories import (
    SUPPORTED_PYTHON_MAX_EXCLUSIVE,
    SUPPORTED_PYTHON_MIN,
    SUPPORTED_PYTHON_RANGE_LABEL,
    get_runtime_dependency_advisories,
)
from caliber.trace_client import MLflowTraceClient

if TYPE_CHECKING:
    from starlette.types import ASGIApp


logger = logging.getLogger("caliber")
_WARNED_UNSUPPORTED_PYTHON_VERSIONS: set[tuple[int, int]] = set()
_WARNED_DEPENDENCY_ADVISORIES: set[tuple[str, str]] = set()

# A Starlette lifespan is "an async-context-manager factory taking the app and
# yielding ``None``." Spelling it out keeps mypy happy without leaking the
# inner ``asynccontextmanager`` implementation detail to callers.
Lifespan = Callable[[Starlette], AbstractAsyncContextManager[None]]


def _resolve_assistant_engine_name(configured: str) -> str:
    """Resolve the assistant engine, expanding ``auto`` to a real provider.

    ``auto`` (the production default) prefers OpenAI when ``OPENAI_API_KEY`` is
    present, then Anthropic (Claude) when ``ANTHROPIC_API_KEY`` is present, and
    otherwise falls back to OpenAI — it never resolves to the ``fake`` stub, so a
    live deployment always talks to a real model. Explicit values
    (``openai`` / ``anthropic`` / ``ollama`` / ``fake``) pass through unchanged;
    ``fake`` is reserved for tests, which set it explicitly.
    """
    if configured != "auto":
        return configured
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "openai"


def _warn_if_unsupported_python() -> None:
    """Log once when CALIBER runs outside the validated Python matrix.

    Packaging metadata prevents fresh installs outside the supported range, but
    editable installs and source checkouts can still run there. Warn clearly so
    operators do not mistake "it started" for "this runtime is supported."
    """

    major, minor = sys.version_info[:2]
    if SUPPORTED_PYTHON_MIN <= (major, minor) < SUPPORTED_PYTHON_MAX_EXCLUSIVE:
        return
    if (major, minor) in _WARNED_UNSUPPORTED_PYTHON_VERSIONS:
        return

    logger.warning(
        "CALIBER is running on unsupported Python %s.%s; supported versions are %s. "
        "Fresh package installs are constrained to the validated range, but editable "
        "or source installs can still bypass that guard.",
        major,
        minor,
        SUPPORTED_PYTHON_RANGE_LABEL,
    )
    _WARNED_UNSUPPORTED_PYTHON_VERSIONS.add((major, minor))


def _warn_if_known_vulnerable_dependencies() -> None:
    """Warn when the current runtime includes dependency versions we already know are flagged."""

    for advisory in get_runtime_dependency_advisories():
        advisory_key = (advisory.package_name, advisory.installed_version)
        if advisory_key in _WARNED_DEPENDENCY_ADVISORIES:
            continue
        logger.warning(
            "Installed dependency %s %s is flagged (%s). %s %s",
            advisory.package_name,
            advisory.installed_version,
            ", ".join(advisory.advisory_ids),
            advisory.summary,
            advisory.recommended_action,
        )
        _WARNED_DEPENDENCY_ADVISORIES.add(advisory_key)


def _build_lifespan(
    engine: Engine,
    worker: RefinementWorker,
    workflow_run_worker: WorkflowRunWorker | None,
    aria_plan_worker: AriaPlanWorker | None,
    knowledge_build_worker: KnowledgeBaseWorker | None,
    scheduler: WorkflowSchedulerTask | None,
    janitor: JanitorTask,
    webhooks: WebhookDispatcher,
    grace_seconds: float,
    _llm: LLMProvider,  # held in the closure so it lives as long as the worker
    _artifact_store: ArtifactStore,  # held similarly
    _eval_provider: EvalProvider,  # held similarly
    _promoter: Promoter,  # held similarly so routes always see a fresh ref
    _event_bus: EventBus,
    background_tasks_enabled: bool = True,
) -> Lifespan:
    """Create the Starlette lifespan callback that starts/stops background tasks.

    Startup order: worker → workflow-run-worker → knowledge-build-worker
    → janitor → webhooks.
    Shutdown order: webhooks first (drains its bus subscription quickly),
    then knowledge-build-worker, workflow-run-worker and worker (so each can
    finish its in-flight unit of work), then janitor, then engine dispose.

    When ``background_tasks_enabled`` is False the loops are not started (the
    ``stop`` calls are no-ops on an unstarted task). This is used by unit/route
    tests so the worker doesn't race their assertions by claiming seeded jobs;
    end-to-end tests opt back in via a dedicated fixture.
    """

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        start_bus = getattr(_event_bus, "start", None)
        if callable(start_bus):
            await start_bus()
        if background_tasks_enabled:
            await worker.start()
            if workflow_run_worker is not None:
                await workflow_run_worker.start()
            if aria_plan_worker is not None:
                await aria_plan_worker.start()
            if knowledge_build_worker is not None:
                await knowledge_build_worker.start()
            if scheduler is not None:
                await scheduler.start()
            await janitor.start()
            await webhooks.start()
        try:
            yield
        finally:
            await webhooks.stop()
            if scheduler is not None:
                await scheduler.stop()
            if knowledge_build_worker is not None:
                await knowledge_build_worker.stop(grace_seconds=grace_seconds)
            if aria_plan_worker is not None:
                await aria_plan_worker.stop(grace_seconds=grace_seconds)
            if workflow_run_worker is not None:
                await workflow_run_worker.stop(grace_seconds=grace_seconds)
            await worker.stop(grace_seconds=grace_seconds)
            await janitor.stop()
            stop_bus = getattr(_event_bus, "stop", None)
            if callable(stop_bus):
                await stop_bus()
            engine.dispose()

    return lifespan


def create_app(config: CaliberConfig | None = None) -> ASGIApp:  # noqa: PLR0915
    """Build and return the CALIBER ASGI application.

    Parameters
    ----------
    config:
        Optional pre-loaded configuration. Pass ``None`` (the default) to load
        from ``CALIBER_*`` environment variables. Tests can pass an explicit
        config to avoid environment dependency.

    Returns
    -------
    ASGIApp
        A Starlette application that handles every route in CALIBER's surface.
        MLflow mounts this onto its own server so all routes are same-origin
        with MLflow's API.

    Raises
    ------
    caliber.config.ConfigError
        If the environment configuration is invalid. Failing fast here is
        intentional — a misconfigured plugin should not silently come up and
        return errors at request time.
    """
    resolved = config if config is not None else CaliberConfig.load()

    # Single-line JSON to stderr. Replaces stdlib's default handler so
    # the format is consistent across the plugin's own logs, third-party
    # library output (mlflow / sqlalchemy / uvicorn), and any code the
    # operator wires in afterward.
    configure_logging(
        level=resolved.log_level,
        log_sink=resolved.log_sink,
        s3_bucket=resolved.log_bucket,
        s3_prefix=resolved.log_prefix,
        s3_endpoint_url=resolved.object_store_endpoint_url,
        s3_region=resolved.object_store_region,
        s3_force_path_style=resolved.object_store_force_path_style,
        s3_access_key_source=resolved.object_store_access_key_source,
        s3_secret_key_source=resolved.object_store_secret_key_source,
        s3_auto_create_bucket=resolved.log_s3_auto_create_bucket,
        s3_flush_lines=resolved.log_s3_flush_lines,
    )
    logger.info(
        "CALIBER plugin starting (log_level=%s, log_sink=%s)",
        resolved.log_level,
        resolved.log_sink,
    )
    _warn_if_unsupported_python()
    _warn_if_known_vulnerable_dependencies()

    # Bound MLflow HTTP calls so a hung tracking server can't freeze
    # request handlers. ``mlflow.search_traces`` reads this env var on
    # each call and propagates it down to its requests session. We only
    # set it when the operator hasn't already chosen a value — honour
    # their override.
    os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "15")

    # Install the audit-log redactor before any state-changing path
    # can call ``audit.record()``. ``configure_redactor`` is a module-
    # level setter so the worker and route handlers all pick up the
    # same instance without threading it through every signature.
    configure_redactor(
        build_redactor(
            enabled=resolved.pii_redaction_enabled,
            extra_patterns=resolved.pii_redaction_extra_patterns,
            replacement=resolved.pii_redaction_replacement,
        )
    )

    # Configure the process-wide MLflow tracer (workflow runs, agent turns, tool
    # calls). Guarded/no-op-safe: inert when MLflow is unavailable or
    # ``tracing_enabled`` is false. Installed after the redactor so traced
    # attributes are PII-redacted from the first span.
    configure_tracing(resolved)

    engine = create_engine_from_config(resolved)
    session_factory = sessionmaker_from_engine(engine)
    # Let cost attribution resolve operator-configured per-model pricing
    # (caliber_llm_model_pricing) over the built-in defaults.
    register_pricing_source(session_factory)
    llm_provider = build_provider(resolved)
    artifact_store = build_store(
        resolved.artifact_store_provider,
        session_factory=session_factory,
    )
    eval_provider = build_eval_provider(
        resolved.eval_provider,
        session_factory=session_factory,
        config=resolved,
    )
    promoter = build_promoter(
        resolved.promoter_provider,
        session_factory=session_factory,
    )
    event_bus = build_event_bus(resolved, session_factory=session_factory)
    worker = RefinementWorker(
        session_factory=session_factory,
        llm_provider=llm_provider,
        artifact_store=artifact_store,
        eval_provider=eval_provider,
        event_bus=event_bus,
        config=resolved,
        trace_client=MLflowTraceClient(),
    )
    workflow_run_worker = WorkflowRunWorker(
        session_factory=session_factory,
        config=resolved,
        event_bus=event_bus,
    )
    aria_plan_worker = AriaPlanWorker(session_factory=session_factory, config=resolved)
    knowledge_build_worker = KnowledgeBaseWorker(
        session_factory=session_factory,
        config=resolved,
    )
    scheduler = WorkflowSchedulerTask(
        session_factory=session_factory,
        interval_seconds=resolved.workflow_scheduler_interval_seconds,
        publish=getattr(event_bus, "publish", None),
    )
    janitor = JanitorTask(
        session_factory=session_factory,
        interval_seconds=resolved.janitor_interval_seconds,
        stale_threshold_seconds=resolved.janitor_stale_threshold_seconds,
        workflow_run_retention_days=resolved.workflow_run_retention_days,
    )
    webhooks = build_dispatcher(
        bus=event_bus,
        urls_csv=resolved.webhook_urls,
        secret_env_var=resolved.webhook_signing_secret_env,
        event_filter_csv=resolved.webhook_event_filter,
    )
    csrf_manager = build_token_manager(
        enabled=resolved.csrf_enabled,
        secret_env_var=resolved.csrf_signing_secret_env,
        ttl_seconds=resolved.csrf_token_ttl_seconds,
    )
    rate_limiter = build_limiter(
        enabled=resolved.rate_limit_enabled,
        requests_per_minute=resolved.rate_limit_requests_per_minute,
        burst=resolved.rate_limit_burst,
        max_buckets=resolved.rate_limit_max_buckets,
    )

    app = Starlette(
        lifespan=_build_lifespan(
            engine,
            worker,
            workflow_run_worker if resolved.workflow_run_worker_enabled else None,
            aria_plan_worker,
            knowledge_build_worker if resolved.knowledge_build_worker_enabled else None,
            scheduler if resolved.workflow_scheduler_enabled else None,
            janitor,
            webhooks,
            resolved.shutdown_grace_seconds,
            llm_provider,
            artifact_store,
            eval_provider,
            promoter,
            event_bus,
            background_tasks_enabled=resolved.background_tasks_enabled,
        ),
        exception_handlers={
            HTTPException: http_exception_handler,
            ValidationError: validation_error_handler,
        },
    )
    # Raw ASGI middleware — added before any other middleware so it
    # wraps the entire request lifecycle, including exception handling.
    # That way the trace ID lands in 5xx log lines too.
    app.add_middleware(TraceIdMiddleware)
    # CSRF middleware is installed unconditionally; the no-op fast
    # path in ``CSRFMiddleware.__call__`` short-circuits when the
    # manager isn't enabled, so the runtime cost is one ``is_enabled``
    # check per request in the default OSS deployment.
    app.add_middleware(
        CSRFMiddleware,
        manager=csrf_manager,
        # The ``/csrf`` endpoint is itself exempt — the SPA needs to be
        # able to fetch a token before it can include one.
        exempt_paths=frozenset({CSRF_PATH}),
        dev_user=resolved.dev_user,
    )
    # Rate-limit middleware is installed only when enabled — when the
    # limiter is ``None``, we skip the middleware entirely so the
    # runtime cost is exactly zero in the default deployment.
    if rate_limiter is not None:
        # Health and CSRF-issuance bypass: liveness probes shouldn't
        # consume tokens (an aggressive Kubernetes probe interval
        # could otherwise drain anonymous's bucket), and the SPA
        # needs to bootstrap a CSRF token before spending any budget.
        app.add_middleware(
            RateLimitMiddleware,
            limiter=rate_limiter,
            exempt_paths=frozenset({HEALTH_PATH, CSRF_PATH}),
            dev_user=resolved.dev_user,
        )
    app.state.config = resolved
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.worker = worker
    app.state.workflow_run_worker = workflow_run_worker
    app.state.aria_plan_worker = aria_plan_worker
    app.state.knowledge_build_worker = knowledge_build_worker
    app.state.workflow_scheduler = scheduler
    app.state.janitor = janitor
    app.state.webhook_dispatcher = webhooks
    app.state.csrf_manager = csrf_manager
    app.state.rate_limiter = rate_limiter
    app.state.llm_provider = llm_provider
    app.state.artifact_store = artifact_store
    app.state.eval_provider = eval_provider
    app.state.promoter = promoter
    app.state.event_bus = event_bus

    # Caliber Assistant service.
    from caliber.assistant.engine import AssistantEngine  # noqa: PLC0415
    from caliber.assistant.fake import FakeAssistantEngine  # noqa: PLC0415
    from caliber.assistant.service import AssistantService  # noqa: PLC0415

    asst_engine: AssistantEngine

    # 'auto' (the default) resolves to a REAL provider by available API key —
    # the fake stub is never selected automatically (tests pin it explicitly).
    engine_name = _resolve_assistant_engine_name(resolved.assistant_engine)
    # Empty model → let the resolved provider's engine use its own default.
    model_kwargs: dict[str, Any] = (
        {"model": resolved.assistant_model} if resolved.assistant_model else {}
    )

    if not resolved.assistant_enabled:
        asst_engine = FakeAssistantEngine()
    elif engine_name == "openai":
        from caliber.assistant.openai_engine import OpenAIAssistantEngine  # noqa: PLC0415
        from caliber.assistant.tools import RegistryToolDispatcher  # noqa: PLC0415

        asst_engine = OpenAIAssistantEngine(
            reasoning=resolved.assistant_reasoning,
            tool_dispatcher=RegistryToolDispatcher(session_factory),
            **model_kwargs,
        )
    elif engine_name == "anthropic":
        from caliber.assistant.anthropic_engine import AnthropicAssistantEngine  # noqa: PLC0415

        asst_engine = AnthropicAssistantEngine(**model_kwargs)
    elif engine_name == "ollama":
        from caliber.assistant.ollama_engine import OllamaAssistantEngine  # noqa: PLC0415

        asst_engine = OllamaAssistantEngine(**model_kwargs)
    else:  # "fake" — deterministic test double only
        asst_engine = FakeAssistantEngine()
    logger.info(
        "CALIBER assistant engine: %s (configured=%s)", engine_name, resolved.assistant_engine
    )
    from caliber.assistant.service import (  # noqa: PLC0415
        AssistantRuntimeSettings,
        default_prompt_fetcher,
        normalize_disabled_domains,
        normalize_disabled_intents,
    )

    app.state.assistant_service = AssistantService(
        engine=asst_engine,
        prompt_fetcher=default_prompt_fetcher,
        runtime_config=resolved,
        settings=AssistantRuntimeSettings(
            enabled=resolved.assistant_enabled,
            skill_runtime_enabled=resolved.assistant_skill_runtime_enabled,
            disabled_intents=normalize_disabled_intents(
                resolved.assistant_disabled_intents,
            ),
            disabled_domains=normalize_disabled_domains(
                resolved.assistant_disabled_domains,
            ),
            max_turns=resolved.assistant_max_turns,
            max_questions_per_turn=resolved.assistant_max_questions_per_turn,
            max_drafts_per_session=resolved.assistant_max_drafts_per_session,
            publish_requires_approval=resolved.assistant_publish_requires_approval,
            tool_source_max_bytes=resolved.assistant_tool_source_max_bytes,
            run_timeout_seconds=resolved.assistant_run_timeout_seconds,
        ),
    )

    # Built before ``register_routes`` so the static route handlers find
    # it on ``app.state``. Tests that wire ``app.state`` themselves can
    # swap the handler to point at a temp directory.
    app.state.static_ui_handler = build_static_ui_handler(resolved)

    register_routes(app)
    return app
