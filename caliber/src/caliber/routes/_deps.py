"""Route helpers shared across handlers.

The two big ones today:

* :func:`get_session_factory` pulls the per-app sessionmaker off ``app.state``
  so route handlers don't depend on a module-level singleton (which would make
  testing harder).
* :func:`envelope_response` wraps a Pydantic model in the standard
  ``{"data": ...}`` envelope and returns a Starlette :class:`JSONResponse`.

Keep this module small. Anything broader than a handler-level convenience
belongs in its own module.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from caliber.db.scoping import VisibilityTier
from caliber.schemas import Envelope
from caliber.storage import WorkingDirectoryService, build_backend


def visibility_param(request: Request) -> VisibilityTier | None:
    """Read an optional ``?visibility=project|user|public`` query param.

    Used by list endpoints so the My Library (``user``) and Public Library
    (``public``) views can request a single visibility tier. Unknown or absent
    values return ``None`` (the normal all-tiers, in-project view).
    """
    raw = request.query_params.get("visibility")
    if raw == "project":
        return "project"
    if raw == "user":
        return "user"
    if raw == "public":
        return "public"
    return None


def list_limit(request: Request, *, default: int = 500, cap: int = 2000) -> tuple[int, int]:
    """Read ``?limit``/``?offset`` for a list endpoint, returning ``(limit, offset)``.

    Bounds list queries that would otherwise ``.scalars().all()`` the whole table.
    The generous ``default`` (500) is well above realistic asset counts so it
    never silently truncates today's data, while still capping a pathological
    scan; ``cap`` (2000) bounds an explicit oversized request. Invalid values
    fall back to the defaults rather than erroring.
    """
    raw_limit = request.query_params.get("limit")
    limit = default
    if raw_limit is not None:
        try:
            limit = max(1, min(cap, int(raw_limit)))
        except ValueError:
            limit = default
    raw_offset = request.query_params.get("offset")
    offset = 0
    if raw_offset is not None:
        try:
            offset = max(0, int(raw_offset))
        except ValueError:
            offset = 0
    return limit, offset


def get_session_factory(request: Request) -> sessionmaker[Session]:
    """Return the request app's session factory.

    Stored on ``app.state`` by :func:`caliber.server.create_app`. Tests can
    override the factory by assigning a different one before the request is
    issued.
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    return factory


def envelope_response(
    data: BaseModel | Sequence[BaseModel],
    status_code: int = 200,
) -> JSONResponse:
    """Wrap a Pydantic model (or sequence of them) in an :class:`Envelope`.

    Lists serialize as ``[ {...}, {...} ]`` inside ``data``. Single objects
    serialize as ``{...}`` inside ``data``. Both match the convention used by
    the design docs and the frontend's API client.

    The parameter is typed as ``Sequence`` rather than ``list`` so callers can
    pass any list of a concrete Pydantic subclass without running into
    list-invariance complaints from mypy.
    """
    payload: Any
    if isinstance(data, BaseModel):
        payload = data.model_dump(mode="json")
    else:
        payload = [item.model_dump(mode="json") for item in data]
    envelope = {"data": payload}
    return JSONResponse(envelope, status_code=status_code)


def envelope_response_dict(
    payload: dict[str, Any] | list[Any],
    status_code: int = 200,
) -> JSONResponse:
    """Wrap a plain dict/list (not a Pydantic model) in the ``{"data": ...}`` envelope.

    Used by handlers whose payload is assembled as a dict rather than a Pydantic
    model (e.g. the storage file routes). Mirrors :func:`envelope_response`.
    """
    return JSONResponse({"data": payload}, status_code=status_code)


def get_working_dir_service(request: Request) -> WorkingDirectoryService:
    """Return the per-app :class:`WorkingDirectoryService`, building it once.

    The storage backend is constructed from ``app.state.config`` and cached on
    ``app.state`` so we don't re-create it (and re-``mkdir`` the local root) on
    every request. Tests can pre-seed ``app.state.working_dir_service``.
    """
    cached: WorkingDirectoryService | None = getattr(request.app.state, "working_dir_service", None)
    if cached is not None:
        return cached
    config = request.app.state.config
    service = WorkingDirectoryService(
        build_backend(config.workflow_storage), config.workflow_storage
    )
    request.app.state.working_dir_service = service
    return service


async def parse_json_object(
    request: Request,
    *,
    allow_empty: bool = False,
    max_body_bytes: int | None = None,
) -> dict[str, Any]:
    """Parse a JSON-object request body and translate failures to 400.

    Why this exists: route handlers were calling ``await request.json()``
    directly, so a malformed body bubbled up to Starlette's 500 plain-text
    handler — the API contract is JSON envelopes for every status code,
    and we want 400 for bad input, not 500. This helper centralizes the
    translation so a future client error surfaces consistently across
    every write route.

    Parameters
    ----------
    request:
        The incoming Starlette request.
    allow_empty:
        When ``True``, an empty body resolves to ``{}`` instead of a
        400. Used by the few endpoints (approve, rollback) whose body
        is optional.
    max_body_bytes:
        Optional raw-body ceiling. ``Content-Length`` is only a fast rejection;
        the streamed byte count is authoritative so chunked or understated
        requests cannot bypass the limit.

    Returns
    -------
    dict[str, Any]
        The parsed body as a dict. Pydantic does the field-level
        validation downstream; here we only enforce that the body is
        valid JSON *and* a JSON object (not a bare list/number/null).

    Raises
    ------
    HTTPException
        400 on missing-but-required body, malformed JSON, or
        non-object root.
    """
    if max_body_bytes is not None and max_body_bytes < 1:
        raise ValueError("max_body_bytes must be positive")

    raw: bytes
    if max_body_bytes is None:
        raw = await request.body()
    else:
        declared = request.headers.get("Content-Length")
        if declared is not None:
            try:
                declared_bytes = int(declared)
            except ValueError:
                declared_bytes = -1
            if declared_bytes > max_body_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"request body exceeds {max_body_bytes} bytes",
                )

        buffered = bytearray()
        async for chunk in request.stream():
            if len(buffered) + len(chunk) > max_body_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"request body exceeds {max_body_bytes} bytes",
                )
            buffered.extend(chunk)
        raw = bytes(buffered)
        # Match Request.body()'s replay behavior for instrumentation or wrappers
        # that legitimately inspect the already-consumed body later in the request.
        request._body = raw
    if not raw:
        if allow_empty:
            return {}
        raise HTTPException(status_code=400, detail="request body is required")
    # ``json.loads`` raises ``UnicodeDecodeError`` on byte sequences
    # that aren't a valid UTF-* encoding *before* it gets to parse the
    # JSON — without this branch, a malformed binary body bubbles up
    # as a 500 instead of the structured 400 the API contract
    # promises (V2 review Finding 2).
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON body: {exc}") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    return parsed


__all__ = [
    "Envelope",
    "envelope_response",
    "get_session_factory",
    "parse_json_object",
]


def scoped_child_or_404(
    session: Session,
    request: Request,
    *,
    child: Any,
    child_id: str,
    child_label: str,
    parent_model: Any,
    parent_pk: Any,
    parent_id_attr: str = "workflow_id",
) -> Any:
    """Fetch ``child`` by id, but only if its **parent** is visible to the caller.

    Closes the C3 pattern for nested resources. A version, deployment, promotion, or
    dataset example carries no visibility columns of its own — its access rules live on
    the parent workflow/dataset — so a bare ``session.get(Child, id)`` silently skips
    the predicate its list sibling enforces, and a guessed id crosses a project
    boundary.

    Two deliberate choices:

    * **A forbidden parent 404s, identically to a missing one.** "Exists but you may
      not see it" is itself a disclosure — it confirms the id is real — so the two
      cases are indistinguishable to the caller.
    * **An orphaned child (no resolvable parent) 404s too**, rather than being allowed
      through as "unscoped". Failing open on a dangling foreign key would make data
      corruption an access-control bypass.
    """
    row = session.get(child, child_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"{child_label} {child_id!r} not found")
    parent_id = getattr(row, parent_id_attr, None)
    if not parent_id:
        raise HTTPException(status_code=404, detail=f"{child_label} {child_id!r} not found")
    from caliber.auth import resolve_identity  # noqa: PLC0415 - avoids an import cycle
    from caliber.db.scoping import get_visible  # noqa: PLC0415

    parent = get_visible(session, parent_model, parent_pk, parent_id, resolve_identity(request))
    if parent is None:
        raise HTTPException(status_code=404, detail=f"{child_label} {child_id!r} not found")
    return row
