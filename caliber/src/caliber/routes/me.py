"""``/caliber/me`` — current-user identity for the SPA.

The SPA cannot read its own ``X-CALIBER-User`` request header (it's injected by
the upstream proxy and never returned to JavaScript), so it needs a server
endpoint to learn who it is and what it can do. This is the source the
frontend ``UserContext`` reads on load.

Anonymous callers get ``user_id = "anonymous"`` with an empty scope set rather
than a 401 — the SPA renders a degraded/read-only state instead of erroring.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.auth import SCOPE_ADMIN, current_scopes, current_user
from caliber.routes._deps import envelope_response
from caliber.schemas import IdentitySchema

ME_PATH = "/ajax-api/2.0/mlflow/caliber/me"


async def get_me(request: Request) -> JSONResponse:
    """Return the caller's identity and resolved scopes.

    Shape: ``{ user_id, scopes, is_admin }``. ``scopes`` is sorted for a
    stable response; ``is_admin`` is a convenience flag the SPA uses to gate
    admin-only UI (promote-to-public, demote, cross-user views).
    """
    user = current_user(request)
    scopes = current_scopes(request)
    return envelope_response(
        IdentitySchema(user_id=user, scopes=sorted(scopes), is_admin=SCOPE_ADMIN in scopes)
    )


def register(app: Starlette) -> None:
    """Add the current-user route to the given Starlette application."""
    app.routes.append(Route(ME_PATH, get_me, methods=["GET"]))
