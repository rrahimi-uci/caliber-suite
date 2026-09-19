"""Encrypted GitHub App connection routes for one Workspace source (`P4-E`).

Sibling to ``routes/workspace.py``'s source lifecycle routes
(``GET``/``PUT /projects/{id}/source``), gated by the same two-layer
permission check (``caliber.operator`` scope, then the project's own
``source.manage`` role action) and using the same
``X-CALIBER-Project`` header-vs-path consistency guard. Kept in its own
module rather than folded into ``workspace.py`` because connection
material is security-sensitive enough to want its own small, auditable
surface: every handler here either writes secret material straight through
``secret_store`` (never persisting it itself) or returns a projection that
statically cannot contain a private key or webhook secret, by schema.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, cast

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import (
    SCOPE_OPERATOR,
    CaliberIdentity,
    require_scopes,
    require_user,
    resolve_identity,
)
from caliber.db.models import CaliberWorkspaceSource, CaliberWorkspaceSourceConnection
from caliber.github_http_transport import HTTPXGitHubTransport
from caliber.github_source_control import GitHubTransport
from caliber.github_webhook_reconciliation import (
    WebhookReconciliationError,
    reconcile_connection,
)
from caliber.resource_access import require_project_access
from caliber.routes._deps import envelope_response, get_session_factory, parse_json_object
from caliber.schemas import (
    WorkspaceSourceConnectionConfigureRequest,
    WorkspaceSourceConnectionResponse,
    WorkspaceSourceConnectionSchema,
    WorkspaceSourceReconciliationResultSchema,
)
from caliber.secret_store import SecretStore
from caliber.workspace_source_connections import (
    WorkspaceSourceConnectionError,
    WorkspaceSourceConnectionNotFoundError,
    configure_connection,
    get_connection,
    revoke_connection,
)

PREFIX = "/ajax-api/2.0/mlflow/caliber"
CONNECTION_PATH = PREFIX + "/projects/{project_id}/source/connection"
CONNECTION_REVOKE_PATH = PREFIX + "/projects/{project_id}/source/connection:revoke"
CONNECTION_RECONCILE_PATH = PREFIX + "/projects/{project_id}/source/connection:reconcile-deliveries"

_Factory = sessionmaker[Session]


def _require_workspace_header(request: Request, project_id: str) -> None:
    header_project_id = request.headers.get("X-CALIBER-Project")
    if header_project_id is not None and header_project_id != project_id:
        raise HTTPException(status_code=400, detail="workspace_context_mismatch")


def _store(request: Request) -> SecretStore:
    """The bound encrypted secret store, or a 503 explaining what is missing.

    Mirrors ``routes/secrets.py::_store`` exactly -- a connection cannot be
    configured at all without a bound store, since there is nowhere else
    its secret material could go.
    """
    store = getattr(request.app.state, "secret_store", None)
    if store is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "the encrypted secret store is not configured; set "
                "CALIBER_SECRET_ENCRYPTION_KEY_SOURCE to a 32-byte base64/hex key"
            ),
        )
    return cast(SecretStore, store)


def _github_transport_factory(request: Request) -> Callable[[str], GitHubTransport]:
    """The per-host GitHub transport builder, overridable for tests.

    Mirrors ``routes/workspace.py``'s ``_provider_registry`` pattern: a
    real ``HTTPXGitHubTransport`` in production, but a test can install
    ``app.state.github_transport_factory`` to inject a fake transport and
    keep this route's tests offline, matching this whole series'
    credential-free/network-free testing convention.
    """
    factory = getattr(request.app.state, "github_transport_factory", None)
    if factory is not None:
        return cast(Callable[[str], GitHubTransport], factory)
    return lambda host: HTTPXGitHubTransport(host=host)


def _source_for_project(session: Session, project_id: str) -> CaliberWorkspaceSource | None:
    return session.execute(
        select(CaliberWorkspaceSource).where(CaliberWorkspaceSource.project_id == project_id)
    ).scalar_one_or_none()


def _connection_schema(
    connection: CaliberWorkspaceSourceConnection,
) -> WorkspaceSourceConnectionSchema:
    return WorkspaceSourceConnectionSchema(
        connection_id=connection.connection_id,
        source_id=connection.source_id,
        project_id=connection.project_id,
        provider=cast(Literal["github", "gitlab", "bitbucket"], connection.provider),
        app_id=connection.app_id,
        installation_id=connection.installation_id,
        status=cast(Literal["active", "revoked"], connection.status),
        created_at=connection.created_at.isoformat() if connection.created_at else None,
        updated_at=connection.updated_at.isoformat() if connection.updated_at else None,
    )


def _get_connection_sync(
    factory: _Factory,
    *,
    project_id: str,
    identity: CaliberIdentity,
) -> WorkspaceSourceConnectionResponse:
    with factory() as session:
        require_project_access(session, identity, project_id, "read")
        source = _source_for_project(session, project_id)
        if source is None:
            return WorkspaceSourceConnectionResponse(connection=None)
        connection = get_connection(session, source.source_id)
        return WorkspaceSourceConnectionResponse(
            connection=_connection_schema(connection) if connection is not None else None
        )


def _configure_connection_sync(
    factory: _Factory,
    secret_store: SecretStore,
    *,
    project_id: str,
    actor: str,
    identity: CaliberIdentity,
    payload: WorkspaceSourceConnectionConfigureRequest,
) -> WorkspaceSourceConnectionResponse:
    with factory() as session:
        require_project_access(session, identity, project_id, "source.manage")
        source = _source_for_project(session, project_id)
        if source is None:
            raise HTTPException(status_code=409, detail="workspace_source_not_configured")
        try:
            connection = configure_connection(
                session,
                secret_store,
                source=source,
                app_id=payload.app_id,
                installation_id=payload.installation_id,
                private_key=payload.private_key,
                webhook_secret=payload.webhook_secret,
                actor=actor,
            )
        except WorkspaceSourceConnectionError as exc:
            # ``configure_connection`` already normalizes every failure it can
            # raise -- provider mismatch and any underlying secret-store
            # error -- into this one type (see its own docstring); there is
            # deliberately no second except clause here for
            # ``SecretStoreError``/``SecretNotConfiguredError`` directly,
            # since a bound ``SecretStore`` (guaranteed by ``_store()``
            # above) cannot raise the latter, and the former never escapes
            # ``configure_connection`` unwrapped.
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        audit_record(
            session,
            actor=actor,
            action="configure_workspace_source_connection",
            entity_type="workspace_source_connection",
            entity_id=connection.connection_id,
            # Never the secret material itself -- only identifiers that are
            # already visible in the (secret-free) read projection.
            details={
                "project_id": project_id,
                "source_id": source.source_id,
                "app_id": connection.app_id,
                "installation_id": connection.installation_id,
            },
        )
        session.commit()
        return WorkspaceSourceConnectionResponse(connection=_connection_schema(connection))


def _revoke_connection_sync(
    factory: _Factory,
    secret_store: SecretStore,
    *,
    project_id: str,
    actor: str,
    identity: CaliberIdentity,
) -> WorkspaceSourceConnectionResponse:
    with factory() as session:
        require_project_access(session, identity, project_id, "source.manage")
        source = _source_for_project(session, project_id)
        if source is None:
            raise HTTPException(status_code=404, detail="workspace source not found")
        try:
            connection = revoke_connection(session, secret_store, source=source, actor=actor)
        except WorkspaceSourceConnectionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        audit_record(
            session,
            actor=actor,
            action="revoke_workspace_source_connection",
            entity_type="workspace_source_connection",
            entity_id=connection.connection_id,
            details={"project_id": project_id, "source_id": source.source_id},
        )
        session.commit()
        return WorkspaceSourceConnectionResponse(connection=_connection_schema(connection))


def _reconcile_connection_sync(
    factory: _Factory,
    secret_store: SecretStore,
    transport_factory: Callable[[str], GitHubTransport],
    *,
    project_id: str,
    actor: str,
    identity: CaliberIdentity,
) -> WorkspaceSourceReconciliationResultSchema:
    with factory() as session:
        require_project_access(session, identity, project_id, "source.manage")
        source = _source_for_project(session, project_id)
        if source is None:
            raise HTTPException(status_code=404, detail="workspace source not found")
        connection = get_connection(session, source.source_id)
        if connection is None:
            raise HTTPException(
                status_code=409, detail="workspace_source_connection_not_configured"
            )
        transport = transport_factory(source.provider_host)
        try:
            result = reconcile_connection(session, secret_store, connection, transport)
        except WebhookReconciliationError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        finally:
            close = getattr(transport, "close", None)
            if callable(close):
                close()
        audit_record(
            session,
            actor=actor,
            action="reconcile_workspace_source_connection_deliveries",
            entity_type="workspace_source_connection",
            entity_id=connection.connection_id,
            details={
                "project_id": project_id,
                "source_id": source.source_id,
                "checked": result.checked,
                "missed": len(result.missed),
                "redelivery_requested": len(result.redelivery_requested),
            },
        )
        session.commit()
        return WorkspaceSourceReconciliationResultSchema(
            checked=result.checked,
            missed_delivery_ids=[delivery.guid for delivery in result.missed],
            redelivery_requested_ids=list(result.redelivery_requested),
        )


async def get_source_connection(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    require_user(request)
    identity = resolve_identity(request)
    response = await run_in_threadpool(
        _get_connection_sync,
        get_session_factory(request),
        project_id=project_id,
        identity=identity,
    )
    return envelope_response(response)


async def put_source_connection(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    body = await parse_json_object(request)
    payload = WorkspaceSourceConnectionConfigureRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    store = _store(request)
    response = await run_in_threadpool(
        _configure_connection_sync,
        get_session_factory(request),
        store,
        project_id=project_id,
        actor=actor,
        identity=identity,
        payload=payload,
    )
    return envelope_response(response, status_code=201)


async def revoke_source_connection(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    store = _store(request)
    response = await run_in_threadpool(
        _revoke_connection_sync,
        get_session_factory(request),
        store,
        project_id=project_id,
        actor=actor,
        identity=identity,
    )
    return envelope_response(response)


async def reconcile_source_connection_deliveries(request: Request) -> JSONResponse:
    """Find and request redelivery of any webhook deliveries this source's
    inbox is missing, using GitHub's own App-scoped delivery log.

    See ``github_webhook_reconciliation.py``'s module docstring: this route
    never inserts anything into the inbox itself -- a requested redelivery
    re-enters through ``routes/github_webhooks.py`` exactly like a live
    delivery would.
    """
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    store = _store(request)
    transport_factory = _github_transport_factory(request)
    response = await run_in_threadpool(
        _reconcile_connection_sync,
        get_session_factory(request),
        store,
        transport_factory,
        project_id=project_id,
        actor=actor,
        identity=identity,
    )
    return envelope_response(response)


def register(app: Starlette) -> None:
    app.routes.append(Route(CONNECTION_PATH, get_source_connection, methods=["GET"]))
    app.routes.append(Route(CONNECTION_PATH, put_source_connection, methods=["PUT"]))
    app.routes.append(Route(CONNECTION_REVOKE_PATH, revoke_source_connection, methods=["POST"]))
    app.routes.append(
        Route(CONNECTION_RECONCILE_PATH, reconcile_source_connection_deliveries, methods=["POST"])
    )
