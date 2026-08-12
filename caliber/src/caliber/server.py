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
from caliber.egress import EgressPolicy
from caliber.eval.provider import EvalProvider
from caliber.eval.provider import build_provider as build_eval_provider
from caliber.events.bus import EventBus
from caliber.events.nats_bus import build_event_bus
from caliber.events.webhooks import WebhookDispatcher, build_dispatcher
from caliber.integrations.openapi.executor import (
    bind_egress_policy as bind_openapi_egress_policy,
)
from caliber.knowledge.worker import KnowledgeBaseWorker
from caliber.llm.provider import LLMProvider, build_provider
from caliber.observability.logging import configure_logging
from caliber.observability.mlflow_tracing import configure_tracing, register_pricing_source
from caliber.observability.trace import TraceIdMiddleware
from caliber.orchestrator.calibration_drain import CalibrationDrain
from caliber.orchestrator.janitor import JanitorTask
from caliber.orchestrator.release_reconciler import ReleaseReconcilerTask
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
from caliber.workflows.runtime import bind_sandbox_config
from caliber.workflows.tools import bind_module_allowlist

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
    release_reconciler: ReleaseReconcilerTask,
    calibration_drain_task: CalibrationDrain,
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

    Startup order: refinement worker → workflow-run worker → Aria worker →
    knowledge-build worker → scheduler → janitor → release reconciler →
    calibration drain → webhooks.
    Shutdown reverses the dependency edge: webhooks and scheduler stop first,
    followed by the knowledge, Aria, workflow-run, refinement, and calibration
    workers; the janitor, release reconciler, and event bus stop before the
    database engine is disposed.

    This list is prose and drifts; ``paper/scripts/gen_stats.py`` derives the loop
    count from the ``await <task>.start()`` calls below rather than from here.

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
            await release_reconciler.start()
            await calibration_drain_task.start()
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
            await calibration_drain_task.stop(grace_seconds=grace_seconds)
            await release_reconciler.stop()
            await janitor.stop()
            stop_bus = getattr(_event_bus, "stop", None)
            if callable(stop_bus):
                await stop_bus()
            engine.dispose()

    return lifespan


def _build_secret_store(config: CaliberConfig, session_factory: Any) -> Any:
    """Build the encrypted secret store and enable ``secret://`` resolution.

    Returns ``None`` when no encryption key is configured. That is a supported
    state — an operator who has not adopted the store keeps using ``env://`` and
    ``file://`` sources — and it is *not* a silent downgrade: with no store bound,
    a ``secret://`` reference resolves to nothing and its consumer fails closed
    rather than reading a plaintext fallback.
    """
    from caliber.secret_store import SecretNotConfiguredError, SecretStore  # noqa: PLC0415
    from caliber.secrets import bind_secret_store, unbind_secret_store  # noqa: PLC0415

    try:
        store = SecretStore.from_config(config)
    except SecretNotConfiguredError:
        unbind_secret_store()
        logger.info(
            "encrypted secret store disabled (CALIBER_SECRET_ENCRYPTION_KEY_SOURCE unset); "
            "secret:// references will not resolve"
        )
        return None
    except Exception:
        unbind_secret_store()
        logger.warning(
            "encrypted secret store could not be initialized; secret:// references will "
            "not resolve",
            exc_info=True,
        )
        return None
    bind_secret_store(store, session_factory)
    logger.info("encrypted secret store enabled")
    return store


def _bootstrap_admin_account(config: CaliberConfig, session_factory: Any) -> None:
    """Create the configured bootstrap admin when no account exists yet.

    Only when the account table is **empty**: this seeds a reachable deployment, it
    does not reset a password on every restart. A configured secret keeps the normal
    password policy. For the requested zero-configuration local experience, session
    mode with a non-Secure development cookie may seed ``admin/admin``; the exception
    is not available to normal account creation or password reset.

    Failures are logged and swallowed. A control plane that cannot seed an optional
    convenience account must still start; the operator sees the warning and can create
    the account explicitly.
    """
    from caliber.sessions import (  # noqa: PLC0415
        DEFAULT_BOOTSTRAP_ADMIN_PASSWORD,
        DEFAULT_BOOTSTRAP_ADMIN_USER,
        AccountError,
        account_count,
        create_account,
        verify_password,
    )

    if str(getattr(config, "auth_mode", "session") or "session") != "session":
        return
    user_id = str(getattr(config, "auth_bootstrap_admin_user", "") or "").strip()
    secret_source = str(getattr(config, "auth_bootstrap_admin_password_env", "") or "").strip()
    if not user_id:
        return
    from caliber.secrets import resolve_secret  # noqa: PLC0415

    password = (resolve_secret(secret_source) or "") if secret_source else ""
    allow_insecure_default = bool(
        getattr(config, "auth_bootstrap_allow_insecure_default", False)
    ) and not bool(getattr(config, "auth_session_cookie_secure", True))
    using_default = False
    if (
        not password
        and not secret_source
        and user_id == DEFAULT_BOOTSTRAP_ADMIN_USER
        and allow_insecure_default
    ):
        password = DEFAULT_BOOTSTRAP_ADMIN_PASSWORD
        using_default = True
    if not password:
        if secret_source:
            logger.warning(
                "auth bootstrap admin %r is configured but %s resolved no password; "
                "no account was created",
                user_id,
                secret_source,
            )
        return
    using_default = using_default or (
        allow_insecure_default
        and user_id == DEFAULT_BOOTSTRAP_ADMIN_USER
        and password == DEFAULT_BOOTSTRAP_ADMIN_PASSWORD
    )
    try:
        with session_factory() as session:
            if account_count(session) > 0:
                from caliber.db.models import CaliberUserAccount  # noqa: PLC0415

                existing = session.get(CaliberUserAccount, DEFAULT_BOOTSTRAP_ADMIN_USER)
                if existing is not None and verify_password(
                    DEFAULT_BOOTSTRAP_ADMIN_PASSWORD, existing.password_hash
                ):
                    logger.warning(
                        "INSECURE DEFAULT CREDENTIAL admin/admin is still active; "
                        "change it immediately in Administration"
                    )
                return
            create_account(
                session,
                user_id=user_id,
                password=password,
                actor="bootstrap",
                allow_insecure_default=using_default,
            )
            session.commit()
    except AccountError as exc:
        logger.warning("auth bootstrap admin %r was not created: %s", user_id, exc)
    except Exception:  # pragma: no cover - a seeding failure must not block startup
        logger.warning("auth bootstrap admin %r could not be created", user_id, exc_info=True)
    else:
        if using_default:
            logger.warning(
                "created local bootstrap account with INSECURE DEFAULT CREDENTIAL "
                "admin/admin; change it immediately in Administration"
            )
        else:
            logger.info(
                "created bootstrap admin account %r (no accounts existed); grant it scopes via "
                "CALIBER_ADMIN_USERS",
                user_id,
            )


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
    # A fresh session-mode deployment has no accounts, so nobody could sign in.
    # Seeded here rather than by a migration because the password comes from the
    # environment at boot and must never be written into a migration file.
    _bootstrap_admin_account(resolved, session_factory)
    # Let cost attribution resolve operator-configured per-model pricing
    # (caliber_llm_model_pricing) over the built-in defaults.
    register_pricing_source(session_factory)
    secret_store = _build_secret_store(resolved, session_factory)
    # Bound process-wide rather than threaded: a registered tool is imported from the
    # runtime, generated compiler code, and two route paths, and an allowlist only
    # some of them honoured would read as enforced while leaving the rest open.
    bind_module_allowlist(resolved.registered_tool_module_allowlist)
    # Binds the operator's sandbox limits for the out-of-process registered-tool
    # mechanism. **The runtime is not yet routed through it** — `_bind()` still returns
    # the imported callable — so this is presently wiring for a path nothing takes. An
    # earlier version of this comment claimed registered tools "execute out-of-process",
    # which was false the moment that wiring was reverted; see
    # `_sandboxed_registered_tool` for why it was, and what closing C8 requires.
    bind_sandbox_config(resolved)
    # Same reasoning as the allowlist above: the OpenAPI HTTP executor is reached
    # from the preview route, the tool test-run route, the workflow runtime, and
    # standalone exported tools. Bound once so every one of those paths egresses
    # under the operator's policy rather than the safe-but-generic default.
    bind_openapi_egress_policy(EgressPolicy.from_config(resolved))
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
    promoter = build_promoter(resolved.promoter_provider)
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
    from caliber.routes.prompts import _load_prompt_release_info  # noqa: PLC0415

    release_reconciler = ReleaseReconcilerTask(
        session_factory,
        resolve_alias=_load_prompt_release_info,
        interval_seconds=resolved.janitor_interval_seconds,
    )
    calibration_drain_task = CalibrationDrain(
        session_factory=session_factory,
        config=resolved,
    )
    webhooks = build_dispatcher(
        bus=event_bus,
        urls_csv=resolved.webhook_urls,
        secret_env_var=resolved.webhook_signing_secret_env,
        event_filter_csv=resolved.webhook_event_filter,
        # Makes the dead-letter record survive a restart. Without it the record of an
        # undelivered event was lost exactly when an operator rebooted to fix the
        # receiver that dropped it.
        session_factory=session_factory,
    )
    csrf_manager = build_token_manager(
        enabled=resolved.csrf_enabled,
        secret_env_var=resolved.csrf_signing_secret_env,
        ttl_seconds=resolved.csrf_token_ttl_seconds,
    )
    if resolved.csrf_enabled and not csrf_manager.is_enabled:
        # ``build_token_manager`` returns the disabled sentinel when the secret
        # source resolves empty, and it is right not to sign with an empty key.
        # But an explicitly requested security control must not downgrade itself
        # to "off" and let the process start: CSRFMiddleware short-circuits on a
        # disabled manager, so every state-changing request would pass untokened
        # while the configuration, the Settings page, and the operator all say
        # protection is on. A log line is not a control. Fail closed instead.
        raise RuntimeError(
            "CSRF protection is enabled but its signing secret is missing: "
            f"{resolved.csrf_signing_secret_env!r} resolved to an empty value. "
            "Set that source, or set CALIBER_CSRF_ENABLED=false to run without "
            "CSRF protection deliberately rather than by accident."
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
            release_reconciler,
            calibration_drain_task,
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
    app.state.secret_store = secret_store
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
    from caliber.assistant.reviewer import EngineDraftReviewer  # noqa: PLC0415
    from caliber.assistant.service import (  # noqa: PLC0415
        AssistantRuntimeSettings,
        default_prompt_fetcher,
        normalize_disabled_domains,
        normalize_disabled_intents,
    )

    app.state.assistant_service = AssistantService(
        engine=asst_engine,
        reviewer=EngineDraftReviewer(asst_engine),
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
            reviewer_user=resolved.assistant_reviewer_user,
            release_user=resolved.assistant_release_user,
            reviewer_policy_version=resolved.assistant_reviewer_policy_version,
            reviewer_min_confidence=resolved.assistant_reviewer_min_confidence,
            review_ttl_seconds=resolved.assistant_review_ttl_seconds,
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
