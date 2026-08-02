"""Dependency readiness — whether CALIBER can actually do its job right now.

The review's finding: ``/readiness`` "always returns 200 and reports provider
selector/feature flags rather than dependency connectivity", and ``/health``
checks "only API/package and database ``SELECT 1``, not workers, scheduler, queue
lag, MLflow, object store, event bus, or provider connectivity". Both were
*configuration* surfaces wearing operational names, which is worse than having
none: an orchestrator wired to a probe that cannot fail will never restart or
depool a broken instance.

This module answers the operational question by probing the dependencies that a
given configuration actually needs, and it distinguishes three states:

``ready``
    Probed and working.
``not ready``
    Probed and failing. If the check is **required**, readiness fails.
``skipped``
    Not applicable to this configuration (e.g. no object store because storage is
    a local filesystem). Never counted as a pass or a failure.

Requiredness is *derived from configuration*, not from a hand-maintained list, so
it cannot drift: if a deployment stores workflow files in S3, object storage is
required; if it does not, the check is skipped rather than being reported as a
passing dependency it never had.

Every probe is bounded, independent, and exception-guarded — a probe that raises
becomes a "not ready" verdict rather than a 500 on the readiness endpoint, which
would make the endpoint itself the outage.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

#: Bounded so a hung dependency cannot make the readiness probe itself hang. A
#: probe that exceeds this is *not ready*, which is the correct verdict: a
#: dependency too slow to answer in two seconds cannot serve a request either.
PROBE_TIMEOUT_SECONDS = 2.0

#: Provider-selector values that mean a real (non-simulated) backend. Shared with
#: the SPA honesty banner via :mod:`caliber.routes.health`.
REAL_PROVIDER_VALUES = frozenset({"openai", "anthropic", "mlflow"})

#: A dependency answering below this is up and routing — 401/404 from a health
#: path still proves the process is alive, which is what readiness of a
#: *dependency* asks. 5xx means it is broken.
_HTTP_SERVER_ERROR_FLOOR = 500

#: Storage schemes that name a remote object store, so its reachability is a real
#: dependency rather than a local filesystem path.
_REMOTE_STORAGE_SCHEMES = frozenset({"s3", "gs", "az", "azure"})

#: Explicit ``WorkflowStorageConfig.backend`` values that mean "off this host".
#: This is the authoritative signal — see :func:`_plan_object_store`.
_REMOTE_STORAGE_BACKENDS = frozenset({"s3", "gs", "az", "azure"})


@dataclass(frozen=True)
class Check:
    """One dependency's verdict."""

    name: str
    #: ``True`` ready, ``False`` not ready, ``None`` skipped/not applicable.
    ready: bool | None
    required: bool
    detail: str
    latency_ms: int | None = None

    @property
    def blocking(self) -> bool:
        """A required check that is definitively not ready."""
        return self.required and self.ready is False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ready": self.ready,
            "required": self.required,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
        }


@dataclass(frozen=True)
class ReadinessReport:
    checks: list[Check]

    @property
    def ready(self) -> bool:
        return not any(check.blocking for check in self.checks)

    @property
    def blockers(self) -> list[str]:
        return [f"{check.name}: {check.detail}" for check in self.checks if check.blocking]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "checks": [check.to_dict() for check in self.checks],
            "blockers": self.blockers,
        }


def _skipped(name: str, reason: str, *, required: bool = False) -> Check:
    return Check(name=name, ready=None, required=required, detail=reason)


async def _timed(name: str, required: bool, coro: Any) -> Check:
    """Run a probe with a hard timeout, turning any failure into a verdict."""
    started = time.monotonic()
    try:
        detail = await asyncio.wait_for(coro, timeout=PROBE_TIMEOUT_SECONDS)
    except TimeoutError:
        return Check(
            name=name,
            ready=False,
            required=required,
            detail=f"probe timed out after {PROBE_TIMEOUT_SECONDS:g}s",
        )
    except Exception as exc:
        return Check(
            name=name,
            ready=False,
            required=required,
            detail=str(exc) or exc.__class__.__name__,
        )
    return Check(
        name=name,
        ready=True,
        required=required,
        detail=detail,
        latency_ms=int((time.monotonic() - started) * 1000),
    )


# ---------------------------------------------------------------------------
# Individual probes
# ---------------------------------------------------------------------------


def _probe_database_sync(session_factory: Any) -> str:
    from sqlalchemy import text  # noqa: PLC0415

    with session_factory() as session:
        session.execute(text("SELECT 1"))
    return "SELECT 1 ok"


async def _probe_http(url: str, label: str) -> str:
    import httpx  # noqa: PLC0415

    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
        response = await client.get(url)
    if response.status_code >= _HTTP_SERVER_ERROR_FLOOR:
        raise RuntimeError(f"{label} returned HTTP {response.status_code}")
    return f"{label} HTTP {response.status_code}"


async def _probe_tcp(host: str, port: int, label: str) -> str:
    reader, writer = await asyncio.open_connection(host, port)
    del reader
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return f"{label} {host}:{port} open"


def _probe_object_store_sync(config: Any) -> str:
    """Prove the configured bucket is reachable, not merely that a URL is set."""
    from caliber.storage import build_backend  # noqa: PLC0415

    backend = build_backend(config.workflow_storage)
    # A bounded ``list`` is the cheapest operation that exercises credentials,
    # endpoint, and bucket existence together — ``exists`` on a made-up key would
    # pass against a bucket that does not exist on some backends.
    backend.list("", limit=1)
    return "bucket listable"


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def _tracking_uri(config: Any, environ: dict[str, str]) -> str:
    for candidate in (
        environ.get("MLFLOW_TRACKING_URI", ""),
        str(getattr(config, "mlflow_tracking_uri", "") or ""),
    ):
        url = candidate.strip().rstrip("/")
        if url.lower().startswith(("http://", "https://")):
            return url
    return ""


@dataclass(frozen=True)
class _Plan:
    """Either a probe to await, or an already-decided verdict."""

    check: Check | None = None
    awaitable: Any | None = None


def _plan_database(session_factory: Any | None) -> _Plan:
    """Always required: nothing in the control plane works without the database."""
    if session_factory is None:
        return _Plan(
            check=Check(
                name="database",
                ready=False,
                required=True,
                detail="no session factory is configured",
            )
        )
    return _Plan(
        awaitable=_timed("database", True, asyncio.to_thread(_probe_database_sync, session_factory))
    )


def _plan_mlflow(config: Any, environ: dict[str, str]) -> _Plan:
    """Required only when an HTTP tracking server is configured.

    A local file/sqlite tracking store has no service to probe, so asserting it
    would report a dependency the deployment does not have.
    """
    tracking = _tracking_uri(config, environ)
    if not tracking:
        return _Plan(check=_skipped("mlflow", "no HTTP tracking server configured (local store)"))
    return _Plan(awaitable=_timed("mlflow", True, _probe_http(f"{tracking}/health", "MLflow")))


def _plan_object_store(config: Any) -> _Plan:
    """Required only when workflow storage is remote.

    Keyed to the **explicit** ``WorkflowStorageConfig.backend``, not to the scheme of
    ``base_uri``. That inversion was a real misclassification: ``base_uri`` is
    documented as *ignored* when the backend is ``s3``, so the shipped S3/MinIO
    topology — ``backend="s3"``, a bucket, and the default ``file://`` base URI —
    made this planner report ``required=false, ready=null, "local storage backend"``.
    Readiness then returned 200 while object storage was unreachable, which is the
    one outcome a readiness probe must never produce.

    The URI scheme is still honoured as an *additional* trigger, so a deployment that
    points ``base_uri`` at a remote scheme is probed too rather than skipped. Both
    signals can only ever add a probe, never suppress one.
    """
    storage = getattr(config, "workflow_storage", None)
    backend = str(getattr(storage, "backend", "") or "").strip().lower()
    storage_uri = str(getattr(storage, "base_uri", "") or "")
    # Report the *scheme* only. ``/readiness`` is unauthenticated, and the full URI
    # would disclose a server filesystem path or a bucket name — neither is a
    # secret, but neither belongs in a public response either.
    scheme = storage_uri.split("://", 1)[0].lower() if "://" in storage_uri else ""
    if backend not in _REMOTE_STORAGE_BACKENDS and scheme not in _REMOTE_STORAGE_SCHEMES:
        return _Plan(
            check=_skipped(
                "object_store", f"local storage backend ({backend or scheme or 'unset'})"
            )
        )
    if backend == "s3" and not str(getattr(storage, "bucket", "") or "").strip():
        # Fail closed without probing: ``build_backend`` would raise on this, and a
        # thrown probe reads as "the bucket is down" rather than "no bucket is named".
        return _Plan(
            check=Check(
                name="object_store",
                ready=False,
                required=True,
                detail="storage backend is 's3' but no bucket is configured",
            )
        )
    return _Plan(
        awaitable=_timed("object_store", True, asyncio.to_thread(_probe_object_store_sync, config))
    )


def _plan_event_bus(config: Any) -> _Plan:
    """Required only when an external broker is configured.

    The in-process bus is always available and has nothing to probe.
    """
    backend = str(getattr(config, "workflow_run_event_backend", "") or "").strip().lower()
    # Redis was configurable as an event backend but never probed, so a
    # deployment with an unreachable Redis reported ready and then dropped every
    # cross-replica event. A backend that is selected is a dependency.
    url_attr = {"nats": "nats_url", "redis": "redis_url"}.get(backend, "")
    broker_url = str(getattr(config, url_attr, "") or "").strip() if url_attr else ""
    if not broker_url:
        return _Plan(
            check=_skipped(
                "event_bus", f"no external broker to probe (backend={backend or 'in_process'})"
            )
        )
    label = backend.upper()
    parsed = urlparse(broker_url.split(",")[0])
    if not parsed.hostname or not parsed.port:
        return _Plan(
            check=Check(
                name="event_bus",
                ready=False,
                required=True,
                detail=f"configured {label} URL has no host:port",
            )
        )
    return _Plan(
        awaitable=_timed("event_bus", True, _probe_tcp(parsed.hostname, parsed.port, label))
    )


def _code_execution_allowlist_check(config: Any) -> Check:
    """Report which in-process code-execution allowlists are unset.

    Not required, deliberately: an unset allowlist is a legitimate configuration for a
    single-operator install, and failing readiness on it would make an orchestrator
    refuse to route traffic to a working deployment.

    But it must not be *invisible*. ``registered_tool_module_allowlist`` defaults to
    unrestricted so upgrades do not break, and a control that is off by default with no
    surface is indistinguishable from a control that does not exist — which is the
    "decorative control" pattern this codebase has been removing. Naming it here is
    what makes the default an operator decision instead of an accident.
    """
    unset = [
        name
        for name, attr in (
            ("registered_tool_module_allowlist", "registered_tool_module_allowlist"),
            ("external_app_entrypoint_allowlist", "external_app_entrypoint_allowlist"),
        )
        if not str(getattr(config, attr, "") or "").strip()
    ]
    if not unset:
        return Check(
            name="code_execution_allowlists",
            ready=True,
            required=False,
            detail="all in-process code-execution allowlists are configured",
        )
    # ``external_app_entrypoint_allowlist`` unset means *no* entrypoint is permitted
    # (fail-closed), whereas ``registered_tool_module_allowlist`` unset means
    # unrestricted. Opposite meanings, so the detail says which is which rather than
    # lumping them together as "unset".
    parts = []
    if "registered_tool_module_allowlist" in unset:
        parts.append("registered tool modules are unrestricted (any module may be imported)")
    if "external_app_entrypoint_allowlist" in unset:
        parts.append("external_app entrypoints are all refused (fail-closed)")
    return Check(
        name="code_execution_allowlists",
        ready=False,
        required=False,
        detail="; ".join(parts),
    )


def _queue_check(config: Any, queue_health: Any | None) -> Check:
    """Derived from durable run state, so it needs no probe.

    Required only when the queue is enabled: with it off, runs execute
    synchronously and a missing worker is not an outage.
    """
    if queue_health is None:
        return _skipped("workflow_queue", "queue health was not collected")
    if not bool(getattr(config, "workflow_run_queue_enabled", False)):
        return _skipped("workflow_queue", "run queue is disabled (synchronous execution)")
    reasons = list(getattr(queue_health, "degraded_reasons", []) or [])
    return Check(
        name="workflow_queue",
        ready=bool(getattr(queue_health, "healthy", True)),
        required=True,
        detail="; ".join(reasons) if reasons else "queue draining, worker heartbeat fresh",
    )


async def collect_readiness(
    *,
    config: Any,
    session_factory: Any | None,
    environ: dict[str, str] | None = None,
    queue_health: Any | None = None,
) -> ReadinessReport:
    """Probe every dependency this configuration depends on.

    ``queue_health`` is the already-collected
    :class:`caliber.observability.queue_health.QueueHealth`, passed in rather than
    re-derived so the readiness endpoint and the queue endpoint cannot disagree
    about the same moment.
    """
    environ = environ if environ is not None else {}
    pending: list[Any] = []
    static: list[Check] = []

    # One planner per dependency, each deciding *both* whether the dependency
    # applies to this configuration and how to probe it. Keeping them separate is
    # what makes "requiredness is derived from configuration" checkable per
    # dependency rather than buried in one long branch.
    for plan in (
        _plan_database(session_factory),
        _plan_mlflow(config, environ),
        _plan_object_store(config),
        _plan_event_bus(config),
    ):
        if plan.awaitable is not None:
            pending.append(plan.awaitable)
        else:
            assert plan.check is not None  # a plan is one or the other
            static.append(plan.check)

    checks = list(static)
    if pending:
        checks.extend(await asyncio.gather(*pending))

    checks.append(_queue_check(config, queue_health))

    # Provider credentials — a *configuration* fact, deliberately not required.
    # Running on the deterministic fake provider is a legitimate mode, so it must
    # not fail a liveness/readiness probe an orchestrator acts on; it is reported
    # so an operator can see it.
    providers = {
        key: str(getattr(config, attr, "fake") or "fake").strip().lower()
        for key, attr in (
            ("llm", "llm_provider"),
            ("eval", "eval_provider"),
            ("promoter", "promoter_provider"),
            ("artifact_store", "artifact_store_provider"),
        )
    }
    simulated = sorted(key for key, value in providers.items() if value not in REAL_PROVIDER_VALUES)
    checks.append(
        Check(
            name="providers",
            ready=not simulated,
            required=False,
            detail=(
                f"simulated: {', '.join(simulated)}" if simulated else "all providers are real"
            ),
        )
    )

    checks.append(_code_execution_allowlist_check(config))

    order = {
        "database": 0,
        "mlflow": 1,
        "object_store": 2,
        "event_bus": 3,
        "workflow_queue": 4,
        "providers": 5,
        "code_execution_allowlists": 6,
    }
    checks.sort(key=lambda check: order.get(check.name, 99))
    return ReadinessReport(checks=checks)


__all__ = [
    "PROBE_TIMEOUT_SECONDS",
    "REAL_PROVIDER_VALUES",
    "Check",
    "ReadinessReport",
    "collect_readiness",
]
