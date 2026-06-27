"""``GET /caliber/csrf`` — issue a CSRF token for the current user.

The SPA fetches this on boot and includes the returned token in an
``X-CALIBER-CSRF`` header on every state-changing request. The
:class:`caliber.routes._middleware.CSRFMiddleware` validates the header
on those requests before they reach a route handler.

The endpoint is *itself* read-only (GET) so the middleware lets it
through without a token — that's the bootstrap: the SPA can't include
a token it hasn't fetched yet. The token is bound to the requester's
identity, so a token issued to user A is rejected when presented by
user B.

When CSRF is disabled (the default OSS deployment shape behind an
auth-handling proxy), the endpoint still works but returns an
``"enabled": false`` flag so the SPA can detect that path and skip
sending the header.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.auth import require_user
from caliber.csrf import CSRFTokenManager
from caliber.routes._deps import envelope_response
from caliber.schemas import CSRFTokenResponse

CSRF_PATH = "/ajax-api/2.0/mlflow/caliber/csrf"


def _get_manager(request: Request) -> CSRFTokenManager:
    manager: CSRFTokenManager = request.app.state.csrf_manager
    return manager


async def issue_token(request: Request) -> JSONResponse:
    """Hand the caller a fresh CSRF token bound to their identity.

    Requires authentication so the token is tied to a known user; an
    anonymous caller can't get a token (and wouldn't be able to use one
    for a write anyway, since RBAC rejects anonymous writes with 401).
    """
    manager = _get_manager(request)
    if not manager.is_enabled:
        # Return a flag rather than 404 — the SPA needs to know whether
        # to include the header on subsequent writes, and the simplest
        # signal is "enabled=false".
        return envelope_response(CSRFTokenResponse(enabled=False, token=None, ttl_seconds=0))

    user = require_user(request)
    token = manager.issue(user)
    return envelope_response(
        CSRFTokenResponse(enabled=True, token=token, ttl_seconds=manager.ttl_seconds)
    )


def register(app: Starlette) -> None:
    """Add the CSRF route to the given Starlette application."""
    if not hasattr(app.state, "csrf_manager"):
        raise RuntimeError(
            "csrf_manager missing from app.state; "
            "create_app must build it before calling register_routes."
        )
    app.routes.append(Route(CSRF_PATH, issue_token, methods=["GET"]))
