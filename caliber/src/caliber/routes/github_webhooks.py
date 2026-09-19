"""Production GitHub webhook ingress (`P4-E` slice 2).

Unlike every other route in this package, this one is **not**
session/PAT-authenticated -- GitHub itself is the caller, and there is no
CALIBER identity to resolve. The only admission control is HMAC signature
verification against a stored connection's webhook secret
(``github_source_control.py::verify_webhook``, already built and tested).
This route's job is narrow: authenticate the delivery, then insert it into
the already-durable normalized inbox (``workspace_source_events.py``). It
performs no reconciliation, no Change Request evaluation, and stores no raw
provider payload.

Candidate lookup and the actual trust boundary
------------------------------------------------
The route path is fixed (no ``{source_id}``/``{project_id}`` segment) --
CSRF/rate-limit middleware exemption in ``server.py`` is exact-path-string
matching, not prefix-based, so a per-source path would need per-source
exemption entries, which is worse, not better. Instead, an inbound
delivery's *candidate* connection is resolved from its own untrusted
``installation.id`` claim: a real GitHub App installation is only ever
legitimately bound to one target, enforced by a DB-level partial unique
index on ``(provider, installation_id)`` for active connections (migration
``0109``). That lookup only narrows *which* connection's secret to try --
HMAC verification against that specific connection's own stored webhook
secret is what actually admits the delivery. A forged ``installation.id``
with no matching connection, or a genuine ``installation.id`` with a wrong
signature, are both rejected identically: this route never distinguishes
"no such installation" from "bad signature" in its response, so probing it
cannot be used to enumerate configured installations.

Every rejection path returns the same generic ``401`` with the same body,
regardless of cause (malformed payload, no candidate, signature mismatch,
delivery-evidence conflict, secret unavailable). A rejection is never
logged with a message that includes request header/body content -- only
that a rejection occurred and, where safe, the (already-known, non-secret)
source id.
"""

from __future__ import annotations

import json
import logging
from typing import cast

from sqlalchemy.orm import Session, sessionmaker
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.db.models import CaliberWorkspaceSource
from caliber.github_workspace_provider import build_webhook_verifier
from caliber.routes._deps import get_session_factory, read_request_body
from caliber.secret_store import SecretStore
from caliber.workspace_source_connections import (
    WorkspaceSourceConnectionError,
    get_connection_by_installation,
)
from caliber.workspace_source_control import SourceControlWebhookError
from caliber.workspace_source_events import (
    WorkspaceSourceEventConflictError,
    record_source_event,
)

logger = logging.getLogger("caliber.routes.github_webhooks")

PATH = "/ajax-api/2.0/mlflow/caliber/webhooks/github"

_Factory = sessionmaker[Session]


def _rejected() -> JSONResponse:
    """A fresh, generic rejection response.

    Built new each call rather than shared as a module-level singleton --
    a ``JSONResponse`` is mutated in place by some middleware/response
    pipelines (headers in particular), and this route is exactly the one
    place where two concurrent requests silently sharing response state
    would be a real bug, not just wasteful.
    """
    return JSONResponse({"detail": "webhook rejected"}, status_code=401)


def _extract_installation_id(raw: bytes) -> str | None:
    """Best-effort, untrusted parse of the payload's ``installation.id``.

    Never trusted for anything beyond "which connections to try" -- HMAC
    verification against that connection's own stored secret is the actual
    admission decision. Any parse failure here degrades to "no candidate",
    which is indistinguishable from a genuine signature failure downstream.
    """
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    installation = body.get("installation")
    if not isinstance(installation, dict):
        return None
    installation_id = installation.get("id")
    if installation_id is None:
        return None
    return str(installation_id)


def _process_webhook_sync(
    factory: _Factory,
    secret_store: SecretStore,
    *,
    headers: dict[str, str],
    raw: bytes,
) -> JSONResponse:
    installation_id = _extract_installation_id(raw)
    if installation_id is None:
        return _rejected()

    with factory() as session:
        connection = get_connection_by_installation(
            session, provider="github", installation_id=installation_id
        )
        if connection is None:
            return _rejected()
        source = session.get(CaliberWorkspaceSource, connection.source_id)
        if source is None:  # pragma: no cover - FK guarantees this in practice
            return _rejected()

        try:
            adapter = build_webhook_verifier(
                session, secret_store, connection, host=source.provider_host
            )
            event = adapter.verify_webhook(source.canonical_repository_id, headers, raw)
        except (SourceControlWebhookError, WorkspaceSourceConnectionError):
            # Deliberately no exception text in this log line -- it may
            # (harmlessly) echo header/body fragments, and this path is
            # reached by an untrusted, unauthenticated caller.
            logger.info(
                "rejected a GitHub webhook delivery for source %s: verification failed",
                source.source_id,
            )
            return _rejected()

        try:
            _row, inserted = record_source_event(session, source_id=source.source_id, event=event)
        except WorkspaceSourceEventConflictError:
            session.rollback()
            logger.warning(
                "GitHub webhook delivery id reused with different evidence for source %s",
                source.source_id,
            )
            return _rejected()
        session.commit()
    return JSONResponse({"data": {"received": True, "duplicate": not inserted}}, status_code=202)


async def github_webhook(request: Request) -> JSONResponse:
    """Receive a GitHub App webhook delivery.

    Not session/PAT-authenticated by design (see module docstring).
    Exempted from CSRF and rate-limit middleware in ``server.py`` by exact
    path match -- both would otherwise reject an unauthenticated POST
    before this handler ever runs.
    """
    config = request.app.state.config
    # Body-size enforcement happens unconditionally, before anything else --
    # it is a resource-protection measure, not part of the auth decision,
    # so it must not be skippable by an unbound secret store.
    raw = await read_request_body(request, max_body_bytes=config.github_webhook_max_body_bytes)
    store = getattr(request.app.state, "secret_store", None)
    if store is None:
        # No encrypted secret store bound means no connection could possibly
        # have a resolvable secret. Fail closed the same as "no candidate
        # verified" -- not a distinguishable state for an external caller.
        return _rejected()
    secret_store = cast(SecretStore, store)
    headers = dict(request.headers)
    return await run_in_threadpool(
        _process_webhook_sync,
        get_session_factory(request),
        secret_store,
        headers=headers,
        raw=raw,
    )


def register(app: Starlette) -> None:
    app.routes.append(Route(PATH, github_webhook, methods=["POST"]))


__all__ = ["PATH", "register"]
