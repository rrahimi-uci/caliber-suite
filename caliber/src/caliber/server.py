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

import asyncio
import logging
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy import Engine
from starlette.applications import Starlette
from starlette.exceptions import HTTPException

from caliber.artifact_store import ArtifactStore, build_store
from caliber.assistant.plan_worker import AriaPlanWorker
from caliber.audit import configure_redactor
from caliber.config import CaliberConfig, ConfigError, resolve_assistant_engine
from caliber.csrf import CSRFMiddleware, build_token_manager
from caliber.db.session import create_engine_from_config, sessionmaker_from_engine
from caliber.db_url import shared_sqlite_store
from caliber.egress import EgressPolicy
from caliber.eval.provider import EvalProvider
from caliber.eval.provider import build_provider as build_eval_provider
from caliber.events.bus import EventBus
from caliber.events.nats_bus import build_event_bus
from caliber.events.outbox import OutboxPublisher, bind_outbox, outbox_active
from caliber.events.webhooks import WebhookDispatcher, build_dispatcher
from caliber.execution_role import ExecutionRoleController
from caliber.features import FeatureGateMiddleware
from caliber.github_workspace_provider import GitHubWorkspaceSourceProvider
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
from caliber.orchestrator.workspace_release_evaluator import WorkspaceReleaseEvaluator
from caliber.profile import reference_profile_violations
from caliber.promoter import Promoter, build_promoter
from caliber.rate_limit import RateLimitMiddleware, build_limiter
from caliber.redaction import build_redactor
from caliber.request_limits import BodySizeLimitMiddleware
from caliber.routes import register_routes
from caliber.routes._errors import http_exception_handler, validation_error_handler
from caliber.routes.csrf import CSRF_PATH
from caliber.routes.github_webhooks import PATH as GITHUB_WEBHOOK_PATH
from caliber.routes.health import HEALTH_PATH
from caliber.routes.static import build_handler as build_static_ui_handler
from caliber.routes.system_effects import RECEIPT_CALLBACK_PATH
from caliber.runtime_advisories import (
    SUPPORTED_PYTHON_MAX_EXCLUSIVE,
    SUPPORTED_PYTHON_MIN,
    SUPPORTED_PYTHON_RANGE_LABEL,
    get_runtime_dependency_advisories,
)
from caliber.storage import WorkingDirectoryService, build_backend
from caliber.testing.fault_barriers import ENV_VAR as FAULT_BARRIERS_ENV
from caliber.testing.fault_barriers import armed_barriers
from caliber.trace_client import MLflowTraceClient
from caliber.workflows.admission import AdmissionSettings
from caliber.workflows.runtime import bind_sandbox_config
from caliber.workflows.tools import bind_module_allowlist
from caliber.workspace_import_worker import WorkspaceImportWorker
from caliber.workspace_release_adapters import WorkspaceResourceAdapterRegistry
from caliber.workspace_release_eval_dataset_adapter import EvalDatasetWorkspaceResourceAdapter
from caliber.workspace_release_judge_adapter import JudgeWorkspaceResourceAdapter
from caliber.workspace_release_mcp_server_adapter import McpServerWorkspaceResourceAdapter
from caliber.workspace_release_prompt_adapter import PromptWorkspaceResourceAdapter
from caliber.workspace_release_skill_adapter import SkillWorkspaceResourceAdapter
from caliber.workspace_release_tool_adapter import ToolWorkspaceResourceAdapter
from caliber.workspace_release_workflow_adapter import WorkflowWorkspaceResourceAdapter
from caliber.workspace_sources import WorkspaceSourceProviderRegistry

if TYPE_CHECKING:
    from starlette.types import ASGIApp


logger = logging.getLogger("caliber")
_WARNED_UNSUPPORTED_PYTHON_VERSIONS: set[tuple[int, int]] = set()
_WARNED_DEPENDENCY_ADVISORIES: set[tuple[str, str]] = set()

# A Starlette lifespan is "an async-context-manager factory taking the app and
# yielding ``None``." Spelling it out keeps mypy happy without leaking the
# inner ``asynccontextmanager`` implementation detail to callers.
Lifespan = Callable[[Starlette], AbstractAsyncContextManager[None]]


def _assistant_model_kwargs(engine_name: str, model: str) -> dict[str, Any]:
    """The ``model=`` argument for the assistant engine, or nothing to use the engine's default.

    An empty model means "the resolved provider's own default". A model that cannot belong to the
    resolved provider is dropped for the same reason: the shipped default model name is an OpenAI
    one, and Claude rejects it, so a deployment that only has an Anthropic key would otherwise fail
    every Aria turn with an opaque 404 instead of using Claude's default.
    """
    if not model:
        return {}
    if engine_name == "anthropic" and not model.lower().startswith("claude"):
        logger.info(
            "CALIBER_ASSISTANT_MODEL=%s is not a Claude model; the Anthropic engine "
            "will use its own default instead",
            model,
        )
        return {}
    return {"model": model}


def _resolve_assistant_engine_name(configured: str) -> str:
    """Resolve the assistant engine, expanding ``auto`` to a real provider.

    ``auto`` (the production default) prefers OpenAI when ``OPENAI_API_KEY`` is
    present, then Anthropic (Claude) when ``ANTHROPIC_API_KEY`` is present, and
    otherwise falls back to OpenAI — it never resolves to the ``fake`` stub, so a
    live deployment always talks to a real model. Explicit values
    (``openai`` / ``anthropic`` / ``ollama`` / ``fake``) pass through unchanged;
    ``fake`` is reserved for tests, which set it explicitly.
    """
    return resolve_assistant_engine(configured)


#: MLflow environment variables that can point its store at a SQLite file.
_MLFLOW_STORE_ENV_VARS = ("MLFLOW_TRACKING_URI", "MLFLOW_REGISTRY_URI", "MLFLOW_BACKEND_STORE_URI")


def _warn_if_sqlite_store_is_shared(database_url: str) -> None:
    """Warn when CALIBER's database and an MLflow store are the same SQLite file.

    ``scripts/run-dev.sh`` and the browser-test harness do this on purpose, and it
    works: Apply commits before it calls MLflow, so the two engines no longer wait
    on each other during a release. SQLite still allows only one writer at a time,
    so any *other* long write on either side makes the other wait for its busy
    timeout. That is fine for local development and a bad surprise anywhere
    shared, hence a warning rather than a refusal.
    """

    shared = shared_sqlite_store(
        database_url, *(os.environ.get(name) for name in _MLFLOW_STORE_ENV_VARS)
    )
    if shared is None:
        return
    logger.warning(
        "CALIBER's database and an MLflow store share one SQLite file (%s). That is supported "
        "for local development, but SQLite allows a single writer, so concurrent CALIBER and "
        "MLflow writes can wait on each other until the busy timeout. Use separate files, or "
        "PostgreSQL, for anything shared.",
        shared,
    )


def _require_reference_profile(config: CaliberConfig) -> None:
    """Refuse to start under ``CALIBER_PROFILE=reference`` while any setting violates the profile.

    Every violation is listed, so an operator fixes them in one pass. Runs before the engine is
    created and before the bootstrap admin account is written: a refused start must leave no trace.
    """
    armed = armed_barriers()
    if armed:
        # A barrier ends the process on purpose, without cleanup, so it must never be a surprise: say so
        # in every profile (the reference profile goes on to refuse it).
        logger.warning(
            "fault barriers are armed (%s): this process exits at once when it reaches one. "
            "Unset %s outside scenario tests.",
            ", ".join(sorted(armed)),
            FAULT_BARRIERS_ENV,
        )
    if config.profile != "reference":
        return
    violations = reference_profile_violations(config)
    if violations:
        count = len(violations)
        raise ConfigError(
            f"reference profile refused ({count} violation{'s' if count != 1 else ''}):\n"
            + "\n".join(f"- {v.message}" for v in violations)
        )


def _require_simulated_opt_in(config: CaliberConfig) -> None:
    """Refuse to boot with simulated release or evidence providers unless opted in.

    Two combinations produce a result that looks real and is not (alpha gaps P0-3
    and P0-4):

    * a ``fake`` promoter or artifact store: Apply would return ``applied`` and
      write ``promote`` audit rows while the MLflow registry is unchanged;
    * a real LLM provider with the ``fake`` evaluation provider: genuine candidates
      would be scored with canned numbers and pass the gate on them.

    A ``fake`` LLM with a ``fake`` evaluator is the offline default and stays
    allowed; its output is marked ``simulated`` on every stored result. All of
    these remain available for tests, demos and the browser-test harness, but only
    when the operator says so explicitly.
    """
    simulated = [
        f"CALIBER_{name.upper()}=fake"
        for name, value in (
            ("promoter_provider", config.promoter_provider),
            ("artifact_store_provider", config.artifact_store_provider),
        )
        if value.strip().lower() == "fake"
    ]
    real_llm_with_fake_eval = (
        config.eval_provider.strip().lower() == "fake"
        and config.llm_provider.strip().lower() not in {"", "fake"}
    )
    if not config.allow_simulated_providers:
        if simulated:
            raise ValueError(
                f"{' and '.join(simulated)} simulate releases: Apply would report success while "
                "the MLflow registry is unchanged. Use the default (`mlflow`), or set "
                "CALIBER_ALLOW_SIMULATED_PROVIDERS=true to run a simulation deliberately "
                "(tests, demos)."
            )
        if real_llm_with_fake_eval:
            raise ValueError(
                f"CALIBER_LLM_PROVIDER={config.llm_provider} is a real provider but "
                "CALIBER_EVAL_PROVIDER=fake would score its candidates with canned numbers and "
                "pass the gate on them. Set CALIBER_EVAL_PROVIDER=mlflow, or set "
                "CALIBER_ALLOW_SIMULATED_PROVIDERS=true to run that combination deliberately."
            )


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


def _build_lifespan(  # noqa: PLR0915 - one lifespan: every owner loop's start and stop
    engine: Engine,
    worker: RefinementWorker,
    workflow_run_worker: WorkflowRunWorker | None,
    aria_plan_worker: AriaPlanWorker | None,
    knowledge_build_worker: KnowledgeBaseWorker | None,
    workspace_import_worker: WorkspaceImportWorker | None,
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
    *,
    session_factory: Any = None,
    reference_profile: bool = False,
    execution_lease_seconds: float = 30.0,
    outbox_publisher: OutboxPublisher | None = None,
    workspace_release_evaluator: WorkspaceReleaseEvaluator | None = None,
) -> Lifespan:
    """Create the Starlette lifespan callback that starts/stops background tasks.

    Startup order: refinement worker → workflow-run worker → Aria worker →
    knowledge-build worker → workspace-import worker → scheduler → janitor →
    release reconciler → workspace release evaluator → calibration drain → webhooks.
    Shutdown stops the webhook dispatcher first, then every other loop at once (each drain waits for its own
    in-flight work, so one after another they would keep claiming new work for the sum of the graces); the event
    bus stops, and the database engine is disposed, only after all of them.

    This list is prose and drifts; ``paper/scripts/gen_stats.py`` derives the loop
    count from the ``await <task>.start()`` calls below rather than from here.

    In the reference profile the loops are owner-only (ADR 0002): they start only while this process holds the
    execution-owner lease and stop when it is lost, and a process that does not hold it serves HTTP only
    (:class:`caliber.execution_role.ExecutionRoleController`, published as ``app.state.execution_role``). Elsewhere
    they start unconditionally, as before.

    When ``background_tasks_enabled`` is False the loops are not started (the
    ``stop`` calls are no-ops on an unstarted task). This is used by unit/route
    tests so the worker doesn't race their assertions by claiming seeded jobs;
    end-to-end tests opt back in via a dedicated fixture.
    """

    async def start_owner_loops() -> None:
        await worker.start()
        if workflow_run_worker is not None:
            await workflow_run_worker.start()
        if aria_plan_worker is not None:
            await aria_plan_worker.start()
        if knowledge_build_worker is not None:
            await knowledge_build_worker.start()
        if workspace_import_worker is not None:
            await workspace_import_worker.start()
        if scheduler is not None:
            await scheduler.start()
        await janitor.start()
        await release_reconciler.start()
        if workspace_release_evaluator is not None:
            await workspace_release_evaluator.start()
        await calibration_drain_task.start()
        await webhooks.start()
        if outbox_publisher is not None:
            await outbox_publisher.start()

    async def stop_owner_loops() -> None:
        # Webhooks first, then every other loop at once: each drain waits for its own in-flight work, and one after
        # another they would keep claiming new work (and the lease would be held) for the sum of the graces.
        await webhooks.stop()
        stops: list[Awaitable[None]] = []
        if scheduler is not None:
            stops.append(scheduler.stop())
        if workspace_import_worker is not None:
            stops.append(workspace_import_worker.stop(grace_seconds=grace_seconds))
        if knowledge_build_worker is not None:
            stops.append(knowledge_build_worker.stop(grace_seconds=grace_seconds))
        if aria_plan_worker is not None:
            stops.append(aria_plan_worker.stop(grace_seconds=grace_seconds))
        if workflow_run_worker is not None:
            stops.append(workflow_run_worker.stop(grace_seconds=grace_seconds))
        if outbox_publisher is not None:
            stops.append(outbox_publisher.stop(grace_seconds=grace_seconds))
        if workspace_release_evaluator is not None:
            stops.append(workspace_release_evaluator.stop(grace_seconds=grace_seconds))
        stops += [
            worker.stop(grace_seconds=grace_seconds),
            calibration_drain_task.stop(grace_seconds=grace_seconds),
            release_reconciler.stop(),
            janitor.stop(),
        ]
        failures = [
            r
            for r in await asyncio.gather(*stops, return_exceptions=True)
            if isinstance(r, BaseException)
        ]
        if failures:
            raise failures[0]

    leased = reference_profile and session_factory is not None
    role = ExecutionRoleController(
        session_factory,
        lease_seconds=execution_lease_seconds,
        start_loops=start_owner_loops,
        stop_loops=stop_owner_loops,
        leased=leased,
        run_loops=background_tasks_enabled,
    )
    if workspace_release_evaluator is not None and leased:
        # Every claim is fenced with the exact lease this process holds, and nothing is claimed without one.
        workspace_release_evaluator.bind_lease_provider(lambda: role.lease)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        start_bus = getattr(_event_bus, "start", None)
        if callable(start_bus):
            await start_bus()
        await role.start()
        _app.state.execution_role = role
        try:
            yield
        finally:
            await role.stop()
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


def _warn_if_nobody_can_log_in(session_factory: Any) -> None:
    """Say so when session auth starts with no account and no way to create one.

    Without a bootstrap password source the server creates nothing, so the login page
    works but no credential does, and until this warning the log said nothing.
    """
    from caliber.sessions import account_count  # noqa: PLC0415

    try:
        with session_factory() as session:
            empty = account_count(session) == 0
    except Exception:  # pragma: no cover - the schema may not be migrated yet
        return
    if empty:
        logger.warning(
            "session auth is enabled but no account exists and no bootstrap password source "
            "is configured, so nobody can log in. Set CALIBER_AUTH_BOOTSTRAP_ADMIN_PASSWORD_ENV "
            "to the name of an environment variable holding the admin password, or for a "
            "local-only install set CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT=true together "
            "with CALIBER_AUTH_SESSION_COOKIE_SECURE=false"
        )


def _error_if_no_role_lists(config: CaliberConfig, user_id: str) -> None:
    """Log an ERROR when the bootstrap account was created but no role list names anyone.

    Accounts hold credentials only; scopes come from ``CALIBER_ADMIN_USERS``,
    ``CALIBER_OPERATOR_USERS`` and ``CALIBER_APPROVER_USERS``. With all three empty the
    new account signs in as a viewer and cannot create a project, which looks like a
    broken install.
    """
    lists = (
        getattr(config, "admin_users", ""),
        getattr(config, "operator_users", ""),
        getattr(config, "approver_users", ""),
    )
    if any(str(value or "").strip() for value in lists):
        return
    logger.error(
        "bootstrap account %r was created but CALIBER_ADMIN_USERS, CALIBER_OPERATOR_USERS and "
        "CALIBER_APPROVER_USERS are all empty, so it signs in with viewer access only and "
        "cannot create projects. Set CALIBER_ADMIN_USERS=%s and restart",
        user_id,
        user_id,
    )


def _report_no_bootstrap_password(secret_source: str, session_factory: Any) -> None:
    """Explain why no bootstrap account was created because no password was available."""
    if secret_source:
        logger.warning(
            "auth bootstrap admin is configured but no password was resolved; "
            "no account was created"
        )
    else:
        _warn_if_nobody_can_log_in(session_factory)


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
        _report_no_bootstrap_password(secret_source, session_factory)
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
        _error_if_no_role_lists(config, user_id)


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
    _require_reference_profile(resolved)

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
    _warn_if_sqlite_store_is_shared(resolved.database_url)

    # Bound MLflow HTTP calls so a hung tracking server can't freeze
    # request handlers. ``mlflow.search_traces`` reads this env var on
    # each call and propagates it down to its requests session. We only
    # set it when the operator hasn't already chosen a value — honour
    # their override.
    os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "15")
    # MLflow's default is seven retries with exponential backoff, which kept a request
    # alive for about eight minutes when the tracking server was down (alpha gap P1-04).
    # One retry with a short backoff fails a hung server in about 30 seconds and a refused
    # connection immediately. Operators who run a flaky link can raise it.
    os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "1")
    os.environ.setdefault("MLFLOW_HTTP_REQUEST_BACKOFF_FACTOR", "1")

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
    _require_simulated_opt_in(resolved)
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
    # `P5-E`/`P4-C`: seven resource types have a real (non-fake) adapter
    # today -- a Workflow Studio workflow (alias-rotation release deploys,
    # plus visibility-checked live row + immutable published-version-history
    # resolve/snapshot for the same `POST .../revisions:snapshot` route --
    # see workspace_release_workflow_adapter.py's module docstring for why
    # this is the same shape a skill's own adapter established, closing the
    # managed-snapshot epic's own follow-up list), a CALIBER prompt (MLflow
    # Prompt Registry resolve/snapshot for `POST
    # .../revisions:snapshot`, plus alias-promotion release deploys), a
    # CALIBER registered tool (registry-row resolve/snapshot for the same
    # snapshot route, plus content-drift-checked verification release
    # deploys -- see workspace_release_tool_adapter.py's module docstring for
    # why a tool has nothing to actually rotate), a CALIBER judge
    # (same-database resolve/snapshot plus a content-integrity release check
    # -- see workspace_release_judge_adapter.py's module docstring for why a
    # judge has no external target to promote either), a CALIBER skill
    # (visibility-checked live row + immutable internal version-history
    # resolve/snapshot, plus the same content-integrity release check as
    # tool/judge -- see workspace_release_skill_adapter.py's module docstring
    # for why a skill is a genuinely fourth resource shape), a CALIBER eval
    # dataset (a collection resource -- resolve/snapshot reconstructs the
    # example set "as of" an explicit pinned version and content-addresses
    # the reconstructed set, plus the same same-database verification release
    # shape as tool/judge -- see workspace_release_eval_dataset_adapter.py's
    # module docstring), and a CALIBER MCP server (same "single mutable row,
    # no version history" shape as a judge, plus a content-drift-checked
    # release check over its connection config, discovered tool catalog, and
    # tool policy -- see workspace_release_mcp_server_adapter.py's module
    # docstring). Every other resource type is left unregistered, so a
    # release/snapshot operation against one fails closed with "no adapter
    # registered" rather than silently doing nothing (see
    # workspace_release_operations.py's and
    # routes/workspace.py::snapshot_revision's own module docstrings).
    workspace_resource_adapter_registry = WorkspaceResourceAdapterRegistry(
        {
            "workflow": WorkflowWorkspaceResourceAdapter(config=resolved),
            "prompt": PromptWorkspaceResourceAdapter(),
            "tool": ToolWorkspaceResourceAdapter(),
            "judge": JudgeWorkspaceResourceAdapter(),
            "skill": SkillWorkspaceResourceAdapter(),
            "eval_dataset": EvalDatasetWorkspaceResourceAdapter(),
            "mcp_server": McpServerWorkspaceResourceAdapter(),
        }
    )
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
    # Aria ships on by default in the alpha (``caliber.features``); the poller only skips
    # starting when a deployment explicitly turns Aria off, since then there is no plan to
    # resume.
    aria_plan_worker = (
        AriaPlanWorker(session_factory=session_factory, config=resolved)
        if resolved.feature_aria_enabled
        else None
    )
    knowledge_build_worker = KnowledgeBaseWorker(
        session_factory=session_factory,
        config=resolved,
    )
    # Built eagerly (rather than lazily on first request, as
    # ``routes/_deps.py::get_working_dir_service`` does) so the same instance
    # can be handed to the worker below; assigning it onto ``app.state`` up
    # front keeps that helper's cache hit on the first request.
    working_dir_service = WorkingDirectoryService(
        build_backend(resolved.workflow_storage), resolved.workflow_storage
    )
    # `P4-E`'s encrypted connection storage now exists, so a real adapter can
    # be registered -- gated on its own flag rather than going live
    # unconditionally on upgrade, matching docs/workspace-plan.md's "P4-E is
    # an independently feature-flagged... increment" deferral note. With the
    # flag off (the default) the registry stays empty, exactly as before: the
    # import worker already treats a missing/unavailable provider as
    # best-effort-skipped, never fatal, since the content it materializes
    # already comes from an independently digest-verified retained snapshot,
    # not from a provider fetch.
    workspace_source_registry = WorkspaceSourceProviderRegistry()
    if resolved.github_source_control_enabled:
        if secret_store is None:
            logger.warning(
                "CALIBER_GITHUB_SOURCE_CONTROL_ENABLED is true but no encrypted secret "
                "store is configured (CALIBER_SECRET_ENCRYPTION_KEY_SOURCE unset); the "
                "GitHub source provider will not be registered"
            )
        else:
            workspace_source_registry.register(
                GitHubWorkspaceSourceProvider(
                    session_factory=session_factory,
                    secret_store=secret_store,
                )
            )
    workspace_import_worker = WorkspaceImportWorker(
        session_factory=session_factory,
        config=resolved,
        storage=working_dir_service,
        provider_registry=workspace_source_registry,
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
    # B10.2: executes queued Workspace release evaluations from the revision's
    # immutable pins. Dormant while the release feature is off.
    if (
        resolved.workspace_release_evaluator_enabled
        and not resolved.feature_release_candidates_enabled
    ):
        logger.warning(
            "CALIBER_WORKSPACE_RELEASE_EVALUATOR_ENABLED is true but CALIBER_FEATURE_RELEASE_CANDIDATES is "
            "false, so the Workspace release evaluator is dormant and no release evaluation will be claimed"
        )
    workspace_release_evaluator = (
        WorkspaceReleaseEvaluator(
            session_factory,
            resolved,
            poll_interval_seconds=resolved.workspace_release_evaluator_interval_seconds,
            eval_provider=eval_provider,
            adapter_registry=workspace_resource_adapter_registry,
        )
        if resolved.feature_release_candidates_enabled
        and resolved.workspace_release_evaluator_enabled
        else None
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
    # B13.4: record run events in the transition's transaction and deliver them from an owner-only loop.
    bind_outbox(outbox_active(resolved))
    outbox_publisher = (
        OutboxPublisher(
            session_factory=session_factory,
            poll_seconds=resolved.event_outbox_poll_seconds,
            max_attempts=resolved.event_outbox_max_attempts,
            retention_days=resolved.event_outbox_retention_days,
        )
        if outbox_active(resolved)
        else None
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
            workspace_import_worker if resolved.workspace_import_worker_enabled else None,
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
            session_factory=session_factory,
            reference_profile=resolved.profile == "reference",
            execution_lease_seconds=resolved.execution_lease_seconds,
            outbox_publisher=outbox_publisher,
            workspace_release_evaluator=workspace_release_evaluator,
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
        # able to fetch a token before it can include one. The GitHub
        # webhook ingress route is exempt for a different reason: GitHub
        # itself is the caller, with no CALIBER session to bind a CSRF
        # token to. Its only admission control is HMAC signature
        # verification inside the handler itself (routes/github_webhooks.py).
        # The receipt callback is a connector's signed call, exempt for the same reason (its signature is its admission).
        exempt_paths=frozenset({CSRF_PATH, GITHUB_WEBHOOK_PATH, RECEIPT_CALLBACK_PATH}),
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
        # The GitHub webhook route is exempt for the same reason as CSRF
        # above: it has no CALIBER identity to key a rate-limit bucket on,
        # and an unauthenticated shared "anonymous" bucket would let a
        # burst of legitimate GitHub deliveries and an unrelated anonymous
        # caller starve each other. Its own body-size ceiling
        # (github_webhook_max_body_bytes) plus the cost of HMAC
        # verification bound worst-case per-request cost instead.
        app.add_middleware(
            RateLimitMiddleware,
            limiter=rate_limiter,
            exempt_paths=frozenset(
                {HEALTH_PATH, CSRF_PATH, GITHUB_WEBHOOK_PATH, RECEIPT_CALLBACK_PATH}
            ),
            dev_user=resolved.dev_user,
        )
    # Outermost, so a disabled preview area answers 404 before CSRF or rate limiting
    # have an opinion about a request to something this deployment does not serve.
    app.add_middleware(FeatureGateMiddleware, config=resolved)
    # The reference profile bounds every request body (declared and streamed) before anything else reads it, so
    # it is the outermost middleware (so it echoes its own trace id on a refusal). The default profile installs
    # nothing.
    if resolved.profile == "reference":
        app.add_middleware(
            BodySizeLimitMiddleware,
            max_bytes=resolved.reference_max_body_bytes,
            multipart_max_bytes=resolved.reference_max_upload_bytes,
            # The webhook route enforces its own, larger limit (github_webhook_max_body_bytes).
            path_limits={GITHUB_WEBHOOK_PATH: resolved.github_webhook_max_body_bytes},
        )
    app.state.config = resolved
    AdmissionSettings.from_config(
        resolved
    )  # a rule bundle path without its digest (or the reverse) fails startup
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.secret_store = secret_store
    app.state.worker = worker
    app.state.workflow_run_worker = workflow_run_worker
    app.state.aria_plan_worker = aria_plan_worker
    app.state.knowledge_build_worker = knowledge_build_worker
    app.state.workspace_import_worker = workspace_import_worker
    app.state.working_dir_service = working_dir_service
    app.state.workspace_source_registry = workspace_source_registry
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
    app.state.workspace_resource_adapter_registry = workspace_resource_adapter_registry

    # Caliber Assistant service.
    from caliber.assistant.engine import AssistantEngine  # noqa: PLC0415
    from caliber.assistant.fake import FakeAssistantEngine  # noqa: PLC0415
    from caliber.assistant.service import AssistantService  # noqa: PLC0415

    asst_engine: AssistantEngine

    # 'auto' (the default) resolves to a REAL provider by available API key —
    # the fake stub is never selected automatically (tests pin it explicitly).
    engine_name = _resolve_assistant_engine_name(resolved.assistant_engine)
    model_kwargs = _assistant_model_kwargs(engine_name, resolved.assistant_model)

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
