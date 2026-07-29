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

from caliber.auth import current_user
from caliber.csrf import CSRFTokenManager
from caliber.routes._deps import envelope_response
from caliber.schemas import CSRFTokenResponse

CSRF_PATH = "/ajax-api/2.0/mlflow/caliber/csrf"


def _get_manager(request: Request) -> CSRFTokenManager:
    manager: CSRFTokenManager = request.app.state.csrf_manager
    return manager


async def issue_token(request: Request) -> JSONResponse:
    """Hand the caller a fresh CSRF token bound to their identity.

    **Issued to anonymous callers too, and that is load-bearing.** This endpoint used
    to require authentication, on the reasoning that "an anonymous caller ... wouldn't
    be able to use one for a write anyway, since RBAC rejects anonymous writes with
    401". That was true while identity arrived in a header — every caller was already
    identified. Session authentication (C1) falsified it: ``POST /auth/login`` *is* an
    anonymous state-changing write, and it must succeed.

    With the old rule the two controls deadlocked and login was impossible whenever
    CSRF was enabled — the production posture:

        POST /auth/login -> 403 "CSRF check failed: missing CSRF token"
        GET  /csrf       -> 401 "authentication required"

    Issuing an anonymous-bound token is the fix that keeps **both** controls rather
    than exempting login from CSRF. The token is bound to the caller's current
    identity, so a pre-login token authorizes only pre-login requests; after signing
    in, the client must fetch a new token bound to the authenticated user, which the
    SPA does. Login therefore remains CSRF-protected instead of becoming an
    unprotected hole in the middleware.
    """
    manager = _get_manager(request)
    if not manager.is_enabled:
        # Return a flag rather than 404 — the SPA needs to know whether
        # to include the header on subsequent writes, and the simplest
        # signal is "enabled=false".
        return envelope_response(CSRFTokenResponse(enabled=False, token=None, ttl_seconds=0))

    # ``current_user`` not ``require_user``: anonymous is a legitimate caller here
    # (see the docstring). The token is bound to whatever identity that is.
    token = manager.issue(current_user(request))
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
