"""``/caliber/secrets`` — the secret administration surface C2 said was absent.

The review recorded "No production page was found for **Secrets** …" and, under C2,
that MCP literals were "ordinary JSON at rest for runtime use, with no durable
encrypted/reference-backed resolver, deployment binding, rotation, or revocation
lifecycle". This is that lifecycle, as an API.

The invariant that shapes every handler: **a stored value is never readable back
through this API.** Not for admins, not in an audit row, not in an error message.
Writes go in; only metadata comes out. That is what makes storing a credential here
strictly better than storing it in an MCP config field, and it is why there is no
``GET /secrets/{name}/value`` — an endpoint that returned plaintext would recreate
the readback path C2 is about.

Endpoints (all admin-scoped, because a secret write is a credential change):

* ``GET    /caliber/secrets``                  — inventory: names, versions, state.
* ``PUT    /caliber/secrets/{name}``           — store a value; rotation is the
  same call, since it creates a new version and supersedes the old one.
* ``POST   /caliber/secrets/{name}/revoke``    — stop resolving, retain history.
* ``DELETE /caliber/secrets/{name}``           — purge ciphertext for real.
"""

from __future__ import annotations

import logging

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import SCOPE_ADMIN, require_scopes
from caliber.routes._deps import (
    envelope_response_dict,
    get_session_factory,
    parse_json_object,
)
from caliber.secret_store import (
    SECRET_SCHEME,
    SecretNotConfiguredError,
    SecretStore,
    SecretStoreError,
)

logger = logging.getLogger("caliber.routes.secrets")

LIST_PATH = "/ajax-api/2.0/mlflow/caliber/secrets"
DETAIL_PATH = "/ajax-api/2.0/mlflow/caliber/secrets/{name}"
REVOKE_PATH = "/ajax-api/2.0/mlflow/caliber/secrets/{name}/revoke"


def _store(request: Request) -> SecretStore:
    """The bound store, or a 503 explaining exactly what is missing.

    503 rather than 500: the store being unconfigured is a deployment state, not a
    fault, and the message names the setting that turns it on.
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
    return store  # type: ignore[no-any-return]


async def list_secrets(request: Request) -> JSONResponse:
    """Inventory of stored secrets. Metadata only — never a value."""
    require_scopes(request, [SCOPE_ADMIN])
    store = _store(request)
    factory = get_session_factory(request)
    with factory() as session:
        secrets = store.list_all(session)
    return envelope_response_dict(
        {
            "secrets": secrets,
            "total": len(secrets),
            # Told to the caller so the UI can render the exact reference to paste
            # into an MCP/provider/tool field, rather than inventing the syntax.
            "reference_scheme": SECRET_SCHEME,
        }
    )


async def put_secret(request: Request) -> JSONResponse:
    """Store a value, creating a new version.

    This *is* rotation: a second call supersedes the first rather than overwriting
    it, so an operator can see when each value took effect. The response carries the
    new version number and the reference to use, never the value.
    """
    actor = require_scopes(request, [SCOPE_ADMIN])
    name = request.path_params["name"]
    body = await parse_json_object(request)
    value = body.get("value")
    if not isinstance(value, str) or not value:
        raise HTTPException(status_code=400, detail="'value' must be a non-empty string")
    description = str(body.get("description") or "")

    store = _store(request)
    factory = get_session_factory(request)
    with factory() as session:
        try:
            version = store.put(
                session, name=name, value=value, actor=actor, description=description
            )
        except SecretNotConfiguredError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except SecretStoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit_record(
            session,
            actor=actor,
            action="put_secret",
            entity_type="secret",
            entity_id=name,
            # The version number is the whole payload. Recording anything derived
            # from the value — even a length or a prefix — would leak into the audit
            # trail, which is a surface C2 already had to be contained on.
            details={"version": version},
        )
        session.commit()
        described = store.describe(session, name) or {}
    return envelope_response_dict(
        {**described, "reference": f"{SECRET_SCHEME}{name}"},
        status_code=201 if version == 1 else 200,
    )


async def revoke_secret(request: Request) -> JSONResponse:
    """Stop the secret resolving, retaining ciphertext for audit.

    Distinct from delete: a revoked secret still shows *when* and *by whom* it was
    revoked, which is what an incident review needs.
    """
    actor = require_scopes(request, [SCOPE_ADMIN])
    name = request.path_params["name"]
    store = _store(request)
    factory = get_session_factory(request)
    with factory() as session:
        try:
            revoked = store.revoke(session, name=name, actor=actor)
        except SecretStoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not revoked and store.describe(session, name) is None:
            raise HTTPException(status_code=404, detail=f"secret {name!r} not found")
        audit_record(
            session,
            actor=actor,
            action="revoke_secret",
            entity_type="secret",
            entity_id=name,
            details={"already_revoked": not revoked},
        )
        session.commit()
        described = store.describe(session, name) or {}
    return envelope_response_dict(described)


async def delete_secret(request: Request) -> JSONResponse:
    """Purge every version's ciphertext and the record itself."""
    actor = require_scopes(request, [SCOPE_ADMIN])
    name = request.path_params["name"]
    store = _store(request)
    factory = get_session_factory(request)
    with factory() as session:
        if store.describe(session, name) is None:
            raise HTTPException(status_code=404, detail=f"secret {name!r} not found")
        try:
            removed = store.purge(session, name=name)
        except SecretStoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit_record(
            session,
            actor=actor,
            action="delete_secret",
            entity_type="secret",
            entity_id=name,
            details={"versions_removed": removed},
        )
        session.commit()
    return envelope_response_dict({"name": name, "versions_removed": removed})


def register(app: Starlette) -> None:
    app.routes.append(Route(LIST_PATH, list_secrets, methods=["GET"]))
    app.routes.append(Route(DETAIL_PATH, put_secret, methods=["PUT"]))
    app.routes.append(Route(DETAIL_PATH, delete_secret, methods=["DELETE"]))
    app.routes.append(Route(REVOKE_PATH, revoke_secret, methods=["POST"]))
