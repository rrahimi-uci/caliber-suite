"""``/caliber/admin/platform-admins`` — metadata-only platform identity inventory.

`P1-F` (docs/workspace-plan.md Phase 1 item 13): "Add a metadata-only platform
Admin inventory and keep resource content behind ordinary membership or
explicit audited recovery." `P1-B` removed the old `caliber.admin`-as-implicit-
workspace-role shortcut (`resource_access.py::project_role()`) with an
explicit, bare fail-closed default and no replacement recovery path -- an
admin with no real project membership is locked out of that project's content,
by that ticket's own design. This route is the read-only half of the
recovery story that removal left open: it tells a support/security operator
*who currently holds each config-driven global scope*, without granting them
(or this route's own caller) one bit of access to any project's actual
content. The real interactive break-glass mechanism -- an audited, narrowly-
scoped recovery grant -- is Phase 5's job (`P5-B`); this route answers "who do
I even ask" in the meantime.

Names are read directly from the live :class:`caliber.config.CaliberConfig`
(``admin_users``/``approver_users``/``operator_users``) via
:func:`caliber.auth.parse_user_list` -- the same parse ``current_scopes``
itself uses, so this inventory cannot drift from what actually grants scope.
There is no separate database table to keep in sync.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.auth import SCOPE_ADMIN, parse_user_list, require_scopes
from caliber.routes._deps import envelope_response
from caliber.schemas import PlatformAdminInventorySchema

PLATFORM_ADMINS_PATH = "/ajax-api/2.0/mlflow/caliber/admin/platform-admins"


async def get_platform_admin_inventory(request: Request) -> JSONResponse:
    """Who currently holds each config-driven global scope -- metadata
    only, no project or resource content. Admin-only: this is exactly the
    kind of platform-wide, cross-project visibility `audit.py`'s own
    module docstring reserves for `caliber.admin`.
    """
    require_scopes(request, [SCOPE_ADMIN])
    config = request.app.state.config
    payload = PlatformAdminInventorySchema(
        admin_users=sorted(parse_user_list(config.admin_users)),
        approver_users=sorted(parse_user_list(config.approver_users)),
        operator_users=sorted(parse_user_list(config.operator_users)),
    )
    return envelope_response(payload)


def register(app: Starlette) -> None:
    app.routes.append(Route(PLATFORM_ADMINS_PATH, get_platform_admin_inventory, methods=["GET"]))
