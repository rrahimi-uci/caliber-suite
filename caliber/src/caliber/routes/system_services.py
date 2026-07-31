"""``/caliber/system/services`` — health of the backing platform services.

Surfaces the underlying running services (MLflow, the MLflow AI Gateway, object
storage, the metadata DB, the event bus, the graph console) with their browsable
URLs and a live health probe, so operators can see the whole stack from Settings
instead of hopping between consoles.

All probing is server-side (the CALIBER service can reach the others over the
compose network / configured endpoints; the browser can't, and CORS would block
it anyway). Each probe is independent, short-timeout, and guarded: a service that
is down or not configured degrades to ``healthy=False``/``None`` with a reason —
it never fails the request. Probes run concurrently so the page stays snappy.

Distinct from :mod:`caliber.routes.services` (workflow-as-a-service endpoints).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from sqlalchemy import text
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import SCOPE_OPERATOR, require_scopes, require_user
from caliber.observability import incidents, slo
from caliber.observability.queue_health import collect_queue_health_for_config
from caliber.observability.readiness import collect_readiness
from caliber.routes._deps import (
    envelope_response_dict,
    get_session_factory,
    parse_json_object,
)
from caliber.schemas import SystemServiceSchema

SERVICES_PATH = "/ajax-api/2.0/mlflow/caliber/system/services"
QUEUE_PATH = "/ajax-api/2.0/mlflow/caliber/system/queue"
ALERTS_PATH = "/ajax-api/2.0/mlflow/caliber/system/alerts"
INCIDENTS_PATH = "/ajax-api/2.0/mlflow/caliber/system/incidents"
INCIDENT_SILENCE_PATH = INCIDENTS_PATH + "/{incident_id}/silence"
INCIDENT_ACK_PATH = INCIDENTS_PATH + "/{incident_id}/acknowledge"

_HTTP_TIMEOUT_SECONDS = 2.5
_TCP_TIMEOUT_SECONDS = 2.0
# A response below this means the service is up + answering (401/404 still prove
# liveness); 5xx means it's broken.
_HTTP_SERVER_ERROR = 500

# Public host the operator browses from. The backend probes services over the
# internal network (docker service names), but the clickable links must resolve
# in the user's browser — default to localhost, overridable for remote setups.
_PUBLIC_HOST = os.environ.get("CALIBER_PUBLIC_HOST", "localhost").strip() or "localhost"

# The AGE viewer is published to the host on :8082 but listens on :3000 inside
# the compose network — the CALIBER container reaches it by service name, not via
# the public ``localhost:8082`` browse URL. Probe the internal address first,
# then fall back to whatever ``knowledge_age_viewer_url`` is configured (covers
# native/non-compose runs). Overridable for non-default deployments.
_AGE_VIEWER_INTERNAL_URL = os.environ.get(
    "CALIBER_AGE_VIEWER_INTERNAL_URL", "http://age-viewer:3000"
).strip()


@dataclass
class _Probe:
    healthy: bool | None
    detail: str
    latency_ms: int | None


def _public(port: int, path: str = "") -> str:
    return f"http://{_PUBLIC_HOST}:{port}{path}"


def _http_target(configured: str, fallback: str) -> str:
    """Pick the URL to probe: the configured (internal) one if it's http(s)."""
    url = (configured or "").strip()
    if url.lower().startswith(("http://", "https://")):
        return url.rstrip("/")
    return fallback.rstrip("/")


async def _probe_http(client: httpx.AsyncClient, url: str) -> _Probe:
    start = time.monotonic()
    try:
        resp = await client.get(url)
        latency = int((time.monotonic() - start) * 1000)
        healthy = resp.status_code < _HTTP_SERVER_ERROR
        return _Probe(healthy, f"HTTP {resp.status_code}", latency)
    except Exception as exc:  # connection refused / DNS / timeout
        return _Probe(False, str(exc) or exc.__class__.__name__, None)


async def _probe_http_any(client: httpx.AsyncClient, urls: list[str]) -> _Probe:
    """Probe several candidate URLs; healthy if ANY responds.

    Used where the browse URL differs from the address CALIBER can actually
    reach — e.g. a service whose configured URL is the *public* host
    (``localhost:8082``) that the CALIBER container can't resolve, but which is
    reachable on the compose network at an internal name (``age-viewer:3000``).
    """
    last = _Probe(False, "no candidate URL", None)
    for url in urls:
        if not url:
            continue
        last = await _probe_http(client, url)
        if last.healthy:
            return last
    return last


async def _probe_tcp(host: str, port: int) -> _Probe:
    start = time.monotonic()
    try:
        fut = asyncio.open_connection(host, port)
        _reader, writer = await asyncio.wait_for(fut, timeout=_TCP_TIMEOUT_SECONDS)
        latency = int((time.monotonic() - start) * 1000)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return _Probe(True, f"TCP {host}:{port} open", latency)
    except Exception as exc:
        return _Probe(False, str(exc) or exc.__class__.__name__, None)


def _probe_db_sync(engine: object) -> _Probe:
    start = time.monotonic()
    try:
        with engine.connect() as conn:  # type: ignore[attr-defined]
            conn.execute(text("SELECT 1"))
        latency = int((time.monotonic() - start) * 1000)
        return _Probe(True, "SELECT 1 ok", latency)
    except Exception as exc:
        return _Probe(False, str(exc) or exc.__class__.__name__, None)


def _db_target(database_url: str) -> str:
    """Human-readable DB target (driver + host/db), never the password."""
    try:
        parsed = urlparse(database_url)
    except Exception:
        return "database"
    if parsed.scheme.startswith("sqlite"):
        return "sqlite (local file)"
    host = parsed.hostname or "?"
    port = f":{parsed.port}" if parsed.port else ""
    db = parsed.path.lstrip("/") or "?"
    return f"{host}{port}/{db}"


async def get_services(request: Request) -> JSONResponse:
    require_user(request)
    config = getattr(request.app.state, "config", None)
    engine = getattr(request.app.state, "engine", None)

    def cfg(name: str, default: str = "") -> str:
        return str(getattr(config, name, default) or default).strip()

    tracking = _http_target(os.environ.get("MLFLOW_TRACKING_URI", ""), _public(5000))
    gateway = _http_target(cfg("gateway_uri"), _public(5002))
    minio = _http_target(cfg("object_store_endpoint_url"), _public(9000))
    age_url = cfg("knowledge_age_viewer_url")
    nats_raw = cfg("nats_url", "nats://localhost:4222")
    database_url = cfg("database_url", "sqlite:///./caliber.db")

    nats_parsed = urlparse(nats_raw.split(",")[0]) if nats_raw else None
    nats_host = nats_parsed.hostname if nats_parsed else None
    nats_port = nats_parsed.port if nats_parsed else None

    # (schema-fields, probe coroutine/None). DB + NATS run off the HTTP client.
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS, follow_redirects=False) as client:
        specs: list[tuple[dict[str, object], Awaitable[_Probe] | None]] = [
            (
                {
                    "key": "mlflow",
                    "name": "MLflow Tracking",
                    "description": "Experiments, runs, traces, and the model/prompt registry.",
                    "category": "Tracking",
                    "url": _public(5000),
                    "target": tracking,
                },
                _probe_http(client, f"{tracking}/health"),
            ),
            (
                {
                    "key": "mlflow_gateway",
                    "name": "MLflow AI Gateway",
                    "description": "LLM gateway fronting the providers (one key boundary).",
                    "category": "LLM",
                    "url": _public(5002),
                    "target": gateway,
                },
                _probe_http(client, f"{gateway}/health"),
            ),
            (
                {
                    "key": "object_store",
                    "name": "Object Storage (MinIO)",
                    "description": "S3-compatible store for artifacts and workspaces.",
                    "category": "Storage",
                    "url": _public(9001),
                    "target": minio,
                },
                _probe_http(client, f"{minio}/minio/health/live"),
            ),
            (
                {
                    "key": "database",
                    "name": "Metadata Database (PostgreSQL)",
                    "description": "CALIBER + MLflow metadata store.",
                    "category": "Storage",
                    "url": _public(8081),
                    "target": _db_target(database_url),
                },
                asyncio.to_thread(_probe_db_sync, engine) if engine is not None else None,
            ),
            (
                {
                    "key": "event_bus",
                    "name": "Event Bus (NATS)",
                    "description": "Workflow run event fan-out.",
                    "category": "Messaging",
                    "url": None,
                    "target": f"{nats_host}:{nats_port}" if nats_host else nats_raw,
                },
                _probe_tcp(nats_host, nats_port) if nats_host and nats_port else None,
            ),
        ]
        if age_url:
            specs.append(
                (
                    {
                        "key": "graph_console",
                        "name": "Graph Console (Apache AGE)",
                        "description": "Interactive console for the knowledge graph.",
                        "category": "Knowledge",
                        "url": age_url,
                        "target": age_url,
                    },
                    _probe_http_any(client, [_AGE_VIEWER_INTERNAL_URL, age_url]),
                )
            )

        probes: list[Awaitable[_Probe]] = [
            coro if coro is not None else _noop_probe() for _, coro in specs
        ]
        results: list[_Probe] = await asyncio.gather(*probes)

    services: list[SystemServiceSchema] = []
    for (fields, _), probe in zip(specs, results, strict=True):
        services.append(
            SystemServiceSchema(
                **fields,  # type: ignore[arg-type]
                healthy=probe.healthy,
                detail=probe.detail,
                latency_ms=probe.latency_ms,
            )
        )

    return envelope_response_dict(
        {
            "services": [s.model_dump(mode="json") for s in services],
            "checked_at_ms": int(time.time() * 1000),
        }
    )


async def _noop_probe() -> _Probe:
    return _Probe(None, "not configured", None)


async def get_queue(request: Request) -> JSONResponse:
    """``GET /caliber/system/queue`` — workflow run queue depth + worker liveness.

    The operational signal ``/health`` cannot give: it proves only that the API
    and its database answer, so a dead worker with a growing backlog looked
    identical to a healthy idle system. Derived entirely from durable run state
    (see :mod:`caliber.observability.queue_health`) — read-only, no new schema.
    """
    require_user(request)
    config = getattr(request.app.state, "config", None)
    lease = float(getattr(config, "workflow_run_lease_seconds", 60.0) or 60.0)
    max_age = float(getattr(config, "workflow_queue_max_age_seconds", 300.0) or 300.0)
    factory = get_session_factory(request)
    with factory() as session:
        health = collect_queue_health_for_config(session, config)
    # Outbound webhook delivery failures belong on the same operations surface: an
    # event that exhausted every retry is a *lost notification*, which reads as
    # "nothing happened" to whoever depends on the webhook stream.
    dispatcher = getattr(request.app.state, "webhook_dispatcher", None)
    webhooks: dict[str, object] | None = None
    if dispatcher is not None and hasattr(dispatcher, "dead_letters"):
        webhooks = dispatcher.dead_letters()
    return envelope_response_dict(
        {
            **health.to_dict(),
            "lease_seconds": lease,
            "max_queue_age_seconds": max_age,
            "webhook_delivery": webhooks,
            "checked_at_ms": int(time.time() * 1000),
        }
    )


async def get_alerts(request: Request) -> JSONResponse:
    """``GET /caliber/system/alerts`` — declared SLOs and which are breached.

    Closes the *evaluation* half of "no alert policies, configurable SLOs, error
    budgets": an operator declares objectives in configuration and this reports the
    observed value, the verdict, and — for ratio objectives — remaining error
    budget and burn rate. Routing, escalation, silencing, and incident history are
    deliberately out of scope (see :mod:`caliber.observability.slo`).

    Queue, webhook, and readiness signals are collected here and handed to the
    evaluator, so this endpoint cannot report a different queue depth than
    ``/system/queue`` did in the same request.
    """
    require_user(request)
    config = getattr(request.app.state, "config", None)
    window = float(getattr(config, "slo_window_minutes", slo.DEFAULT_WINDOW_MINUTES) or 60.0)

    dispatcher = getattr(request.app.state, "webhook_dispatcher", None)
    webhook_delivery = (
        dispatcher.dead_letters()
        if dispatcher is not None and hasattr(dispatcher, "dead_letters")
        else None
    )

    factory = get_session_factory(request)
    with factory() as session:
        queue_health = collect_queue_health_for_config(session, config)
        readiness = await collect_readiness(
            config=config,
            session_factory=factory,
            environ=dict(os.environ),
            queue_health=queue_health,
        )
        report = slo.build_report(
            session,
            raw_objectives=getattr(config, "slo_objectives", ""),
            window_minutes=window,
            queue_health=queue_health,
            webhook_delivery=webhook_delivery,
            readiness=readiness,
        )
        # Detection without memory was the gap: a breach used to be visible only to
        # whoever polled while it was still true. Reconciling here means every evaluation
        # updates the durable record, and routing rides the event bus so it inherits the
        # dispatcher's retry, dead-lettering, and crash recovery rather than growing a
        # second delivery path.
        publish = getattr(getattr(request.app.state, "event_bus", None), "publish", None)
        changed = incidents.reconcile(
            session,
            [
                slo.AlertState(**state) if isinstance(state, dict) else state
                for state in report.get("objectives", [])
            ],
            severities=incidents.parse_severities(getattr(config, "slo_severities", "")),
            publish=publish if callable(publish) else None,
        )
    return envelope_response_dict(
        {**report, "incidents": changed, "checked_at_ms": int(time.time() * 1000)}
    )


async def get_incidents(request: Request) -> JSONResponse:
    """``GET /system/incidents`` — incident history, newest first.

    The question ``/system/slo`` cannot answer: what happened *before* now.
    """
    require_user(request)
    limit = int(request.query_params.get("limit", "50") or 50)
    status = request.query_params.get("status") or None
    factory = get_session_factory(request)
    with factory() as session:
        rows = incidents.history(session, limit=limit, status=status)
    return envelope_response_dict({"incidents": rows, "total": len(rows)})


async def silence_incident(request: Request) -> JSONResponse:
    """``POST /system/incidents/{incident_id}/silence`` — mute routing, keep the record."""
    actor = require_scopes(request, [SCOPE_OPERATOR])
    incident_id = request.path_params["incident_id"]
    body = await parse_json_object(request, allow_empty=True)
    minutes = int(body.get("minutes", 60) or 60)
    factory = get_session_factory(request)
    with factory() as session:
        incident = incidents.silence(session, incident_id, minutes=minutes)
        if incident is None:
            raise HTTPException(status_code=404, detail=f"incident {incident_id!r} not found")
        audit_record(
            session,
            actor=actor,
            action="silence_incident",
            entity_type="incident",
            entity_id=incident_id,
            details={"minutes": minutes},
        )
        session.commit()
        silenced_until = incident.silenced_until
    return envelope_response_dict(
        {
            "incident_id": incident_id,
            "silenced_until": silenced_until.isoformat() if silenced_until else None,
        }
    )


async def acknowledge_incident(request: Request) -> JSONResponse:
    """``POST /system/incidents/{incident_id}/acknowledge`` — take ownership.

    Deliberately not the same as resolving: "someone is looking at this" and "it stopped"
    are different facts, and merging them would drop an ongoing incident off the open list.
    """
    actor = require_scopes(request, [SCOPE_OPERATOR])
    incident_id = request.path_params["incident_id"]
    factory = get_session_factory(request)
    with factory() as session:
        incident = incidents.acknowledge(session, incident_id, actor=actor)
        if incident is None:
            raise HTTPException(status_code=404, detail=f"incident {incident_id!r} not found")
        audit_record(
            session,
            actor=actor,
            action="acknowledge_incident",
            entity_type="incident",
            entity_id=incident_id,
            details={},
        )
        session.commit()
        status = incident.status
    return envelope_response_dict(
        {"incident_id": incident_id, "acknowledged_by": actor, "status": status}
    )


def register(app: Starlette) -> None:
    app.routes.append(Route(SERVICES_PATH, get_services, methods=["GET"]))
    app.routes.append(Route(QUEUE_PATH, get_queue, methods=["GET"]))
    app.routes.append(Route(ALERTS_PATH, get_alerts, methods=["GET"]))
    app.routes.append(Route(INCIDENTS_PATH, get_incidents, methods=["GET"]))
    app.routes.append(Route(INCIDENT_SILENCE_PATH, silence_incident, methods=["POST"]))
    app.routes.append(Route(INCIDENT_ACK_PATH, acknowledge_incident, methods=["POST"]))
