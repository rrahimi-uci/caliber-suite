"""A minimal ASGI app that reaches a fault barrier on request, for exercising the harness itself.

``GET /barrier/<name>`` calls ``barrier(<name>)``: it answers 200 when the barrier is not armed and never
answers when it is, because the process exits with status 137 first. The platform's own barriers sit in
the dispatcher and the ledger (#522), which do not exist yet; this app lets the harness be proven now.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.testing.fault_barriers import barrier


async def _health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


async def _hit(request: Request) -> JSONResponse:
    name = request.path_params["name"]
    barrier(name)
    return JSONResponse({"passed": name})


app = Starlette(
    routes=[Route("/health", _health), Route("/barrier/{name}", _hit)],
)
