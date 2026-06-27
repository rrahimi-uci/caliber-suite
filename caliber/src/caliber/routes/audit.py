"""``/audit-log`` — read-only explorer + export over the append-only audit trail.

Every state change CALIBER makes is written to ``caliber_audit_log`` by
:func:`caliber.audit.record`, but until now the table had no read surface — an
auditor had to query the database directly. This module exposes a filtered,
paginated list plus a CSV/JSON export so "who did what to which artifact when,
and why?" can be answered from the UI.

Admin-only: the trail spans every user and project, so reading it requires the
``admin`` scope (the write paths already gate per-action on operator/admin).
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from caliber.auth import SCOPE_ADMIN, require_scopes
from caliber.db.models import CaliberAuditLog
from caliber.routes._deps import envelope_response, get_session_factory
from caliber.schemas import AuditLogEntrySchema, AuditLogPageSchema

LIST_PATH = "/ajax-api/2.0/mlflow/caliber/audit-log"
EXPORT_PATH = "/ajax-api/2.0/mlflow/caliber/audit-log/export"

_DEFAULT_LIMIT = 100
_MAX_LIMIT = 500
# Bound the export so a single request can't stream the entire history into
# memory. The UI surfaces this cap so a truncated export is never mistaken for
# the whole trail.
_EXPORT_CAP = 10_000

_CSV_COLUMNS = (
    "log_id",
    "timestamp",
    "actor",
    "action",
    "entity_type",
    "entity_id",
    "details",
)

# Equality filters: query-param name -> ORM column.
_EQ_FILTERS = (
    ("actor", CaliberAuditLog.actor),
    ("action", CaliberAuditLog.action),
    ("entity_type", CaliberAuditLog.entity_type),
    ("entity_id", CaliberAuditLog.entity_id),
)


def _parse_timestamp(raw: str, *, field: str) -> datetime:
    """Parse an ISO-8601 timestamp, accepting a trailing ``Z`` for UTC."""
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"invalid {field} timestamp: {raw!r} (expected ISO-8601)",
        ) from exc


def _apply_filters(stmt: Select[Any], request: Request) -> Select[Any]:
    """Apply the request's audit filters as ``WHERE`` clauses.

    Shared by the list count, the list page, and the export so all three see an
    identical filtered set. The caller owns ordering / limit / offset.
    """
    params = request.query_params
    for key, column in _EQ_FILTERS:
        value = params.get(key, "").strip()
        if value:
            stmt = stmt.where(column == value)
    since = params.get("since", "").strip()
    if since:
        stmt = stmt.where(CaliberAuditLog.timestamp >= _parse_timestamp(since, field="since"))
    until = params.get("until", "").strip()
    if until:
        stmt = stmt.where(CaliberAuditLog.timestamp <= _parse_timestamp(until, field="until"))
    return stmt


def _newest_first(stmt: Select[Any]) -> Select[Any]:
    return stmt.order_by(CaliberAuditLog.timestamp.desc(), CaliberAuditLog.log_id.desc())


async def list_audit_log(request: Request) -> JSONResponse:
    """Return a filtered, paginated page of audit entries (admin-only)."""
    require_scopes(request, [SCOPE_ADMIN])
    try:
        limit = min(
            max(int(request.query_params.get("limit", str(_DEFAULT_LIMIT))), 1),
            _MAX_LIMIT,
        )
    except ValueError:
        limit = _DEFAULT_LIMIT
    try:
        offset = max(int(request.query_params.get("offset", "0")), 0)
    except ValueError:
        offset = 0

    count_stmt = _apply_filters(select(func.count()).select_from(CaliberAuditLog), request)
    page_stmt = _newest_first(_apply_filters(select(CaliberAuditLog), request))

    session_factory = get_session_factory(request)
    with session_factory() as session:
        total = int(session.execute(count_stmt).scalar_one())
        rows = session.execute(page_stmt.limit(limit).offset(offset)).scalars().all()
        entries = [AuditLogEntrySchema.model_validate(row) for row in rows]

    page = AuditLogPageSchema(entries=entries, total=total, limit=limit, offset=offset)
    return envelope_response(page)


async def export_audit_log(request: Request) -> Response:
    """Export the filtered audit entries as CSV (default) or JSON (admin-only)."""
    require_scopes(request, [SCOPE_ADMIN])
    fmt = request.query_params.get("format", "csv").strip().lower()
    if fmt not in {"csv", "json"}:
        raise HTTPException(status_code=400, detail="format must be 'csv' or 'json'")

    page_stmt = _newest_first(_apply_filters(select(CaliberAuditLog), request))
    session_factory = get_session_factory(request)
    with session_factory() as session:
        rows = session.execute(page_stmt.limit(_EXPORT_CAP)).scalars().all()
        entries = [AuditLogEntrySchema.model_validate(row) for row in rows]

    if fmt == "json":
        body = [entry.model_dump(mode="json") for entry in entries]
        return JSONResponse(
            body,
            headers={"Content-Disposition": 'attachment; filename="caliber-audit-log.json"'},
        )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_CSV_COLUMNS)
    for entry in entries:
        writer.writerow(
            [
                entry.log_id,
                entry.timestamp.isoformat(),
                entry.actor,
                entry.action,
                entry.entity_type,
                entry.entity_id,
                "" if entry.details is None else json.dumps(entry.details, sort_keys=True),
            ]
        )
    return PlainTextResponse(
        buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="caliber-audit-log.csv"'},
    )


def register(app: Starlette) -> None:
    """Register the audit-log explorer + export routes (admin-only)."""
    app.routes.append(Route(EXPORT_PATH, export_audit_log, methods=["GET"]))
    app.routes.append(Route(LIST_PATH, list_audit_log, methods=["GET"]))
