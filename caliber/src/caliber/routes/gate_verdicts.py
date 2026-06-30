"""``/caliber/gate-verdicts/{artifact_type}/{version_key}`` — advisory gate verdicts.

The eval gate is advisory in v1 (it never blocks an alias rotation). These
endpoints give the Version panel a version-addressable place to read the latest
PASS/FAIL/none verdict before a promotion, and let the evaluation flow (or an
operator) record one:

* ``GET  …/{artifact_type}/{version_key}`` — the verdict, or ``{"state": "none"}``.
* ``POST …/{artifact_type}/{version_key}`` — upsert a verdict (operator).
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import SCOPE_OPERATOR, require_scopes, require_user
from caliber.gate_verdicts import (
    GATE_STATES,
    get_gate_verdict,
    record_gate_verdict,
    serialize_gate_verdict,
)
from caliber.routes._deps import get_session_factory, parse_json_object

logger = logging.getLogger("caliber.routes.gate_verdicts")

DETAIL_PATH = "/ajax-api/2.0/mlflow/caliber/gate-verdicts/{artifact_type}/{version_key}"

# Artifact types that carry a versioned gate verdict (mirrors the FE
# VersionedArtifactType members that support gating).
_GATED_ARTIFACT_TYPES: frozenset[str] = frozenset({"prompt", "workflow", "skill"})


def _require_artifact_type(artifact_type: str) -> None:
    if artifact_type not in _GATED_ARTIFACT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unsupported artifact_type {artifact_type!r}; "
                f"expected one of {sorted(_GATED_ARTIFACT_TYPES)}"
            ),
        )


async def get_verdict(request: Request) -> JSONResponse:
    require_user(request)
    artifact_type = request.path_params["artifact_type"]
    version_key = request.path_params["version_key"]
    _require_artifact_type(artifact_type)
    factory = get_session_factory(request)
    with factory() as session:
        row = get_gate_verdict(session, artifact_type, version_key)
        data = serialize_gate_verdict(row)
    return JSONResponse({"data": data})


def _coerce_number(body: dict[str, Any], field: str) -> float | None:
    value = body.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HTTPException(status_code=400, detail=f"{field!r} must be a number")
    return float(value)


async def post_verdict(request: Request) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_OPERATOR])
    artifact_type = request.path_params["artifact_type"]
    version_key = request.path_params["version_key"]
    _require_artifact_type(artifact_type)
    body = await parse_json_object(request)

    state = body.get("state")
    if not isinstance(state, str) or state not in GATE_STATES:
        raise HTTPException(
            status_code=400,
            detail=f"'state' must be one of {sorted(GATE_STATES)}",
        )
    eval_run_id = body.get("eval_run_id")
    if eval_run_id is not None and not isinstance(eval_run_id, str):
        raise HTTPException(status_code=400, detail="'eval_run_id' must be a string")
    score = _coerce_number(body, "score")

    factory = get_session_factory(request)
    with factory() as session:
        record_gate_verdict(
            session,
            artifact_type=artifact_type,
            version_key=version_key,
            state=state,
            score=score,
            baseline_score=_coerce_number(body, "baseline_score"),
            min_aggregate_score=_coerce_number(body, "min_aggregate_score"),
            worst_regression=_coerce_number(body, "worst_regression"),
            max_regression_delta=_coerce_number(body, "max_regression_delta"),
            eval_run_id=eval_run_id,
        )
        audit_record(
            session,
            actor=actor,
            action="record_gate_verdict",
            entity_type=artifact_type,
            entity_id=version_key,
            details={"state": state, "score": score},
        )
        session.commit()
        row = get_gate_verdict(session, artifact_type, version_key)
        data = serialize_gate_verdict(row)
    return JSONResponse({"data": data})


def register(app: Starlette) -> None:
    app.routes.append(Route(DETAIL_PATH, get_verdict, methods=["GET"]))
    app.routes.append(Route(DETAIL_PATH, post_verdict, methods=["POST"]))
