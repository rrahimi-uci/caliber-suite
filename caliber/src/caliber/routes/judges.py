"""``/caliber/judges`` endpoints — custom LLM judges (MLflow 3.14 make_judge).

A judge is a reusable, operator-authored scorer: a name, natural-language
``instructions`` referencing the ``{{ inputs }}`` / ``{{ outputs }}`` /
``{{ expectations }}`` evaluation variables, and an optional model. Judges are
selected as scorers in eval runs (name ``Judge.<name>``); the eval runner rebuilds
them via ``mlflow.genai.make_judge`` at evaluate-time. CALIBER stays the source of
truth — definitions live here, not in MLflow.

Surface:

* ``GET /judges`` — list (filterable by status).
* ``POST /judges`` (operator) — create.
* ``GET /judges/{judge_id}`` — single judge.
* ``PATCH /judges/{judge_id}`` (admin) — update / archive.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import (
    SCOPE_ADMIN,
    SCOPE_OPERATOR,
    require_scopes,
    require_user,
    resolve_identity,
)
from caliber.db.models import CaliberJudge
from caliber.db.scoping import apply_visibility_filter, get_visible
from caliber.eval.alignment import cohen_kappa, confusion_counts, observed_agreement
from caliber.eval.judge_scorer import JudgeError, build_judge, score_with_judge
from caliber.ids import new_judge_id
from caliber.routes._deps import (
    envelope_response,
    get_session_factory,
    list_limit,
    parse_json_object,
    visibility_param,
)
from caliber.schemas import (
    JudgeAlignmentPerExample,
    JudgeAlignmentRequest,
    JudgeAlignmentResult,
    JudgeCreateRequest,
    JudgeSchema,
    JudgeTestRunRequest,
    JudgeTestRunResult,
    JudgeUpdateRequest,
)

LIST_PATH = "/ajax-api/2.0/mlflow/caliber/judges"
DETAIL_PATH = "/ajax-api/2.0/mlflow/caliber/judges/{judge_id}"
TEST_RUN_PATH = "/ajax-api/2.0/mlflow/caliber/judges/{judge_id}/test-run"
ALIGNMENT_PATH = "/ajax-api/2.0/mlflow/caliber/judges/{judge_id}/alignment"

_LIST_STATUS_VALUES: frozenset[str] = frozenset({"active", "archived", "all"})
_UPDATABLE_FIELDS = (
    "description",
    "instructions",
    "model",
    "feedback_value_type",
    "tags",
    "status",
)


async def list_judges(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    requested_status = request.query_params.get("status", "active")
    if requested_status not in _LIST_STATUS_VALUES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid value for 'status': {requested_status!r}; "
                f"expected one of {sorted(_LIST_STATUS_VALUES)}"
            ),
        )
    factory = get_session_factory(request)
    limit, offset = list_limit(request)
    with factory() as session:
        stmt = select(CaliberJudge).order_by(CaliberJudge.name)
        if requested_status != "all":
            stmt = stmt.where(CaliberJudge.status == requested_status)
        stmt = apply_visibility_filter(
            stmt,
            CaliberJudge,
            identity,
            identity.active_project_id,
            only=visibility_param(request),
        )
        rows = session.execute(stmt.limit(limit).offset(offset)).scalars().all()
    items = [JudgeSchema.model_validate(row) for row in rows]
    return envelope_response(items)


async def get_judge(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    judge_id = request.path_params["judge_id"]
    factory = get_session_factory(request)
    with factory() as session:
        row = get_visible(session, CaliberJudge, CaliberJudge.judge_id, judge_id, identity)
    if row is None:
        raise HTTPException(status_code=404, detail=f"judge {judge_id!r} not found")
    return envelope_response(JudgeSchema.model_validate(row))


def create_judge_record(
    session: Any,
    *,
    payload: JudgeCreateRequest,
    actor: str,
    project_id: str | None,
) -> CaliberJudge:
    """Create a judge row (the single definition reused by the route + Aria).

    Raises :class:`ValueError` on a duplicate name (the route maps it to 409, the
    capability handler surfaces it to the model). Flushes but does not commit —
    the caller owns the transaction.
    """
    existing = (
        session.execute(select(CaliberJudge).where(CaliberJudge.name == payload.name))
        .scalars()
        .first()
    )
    if existing is not None:
        raise ValueError(f"judge name {payload.name!r} is already in use by {existing.judge_id!r}")
    judge = CaliberJudge(
        judge_id=new_judge_id(),
        name=payload.name,
        description=payload.description,
        instructions=payload.instructions,
        model=payload.model,
        feedback_value_type=payload.feedback_value_type,
        owner=actor,
        project_id=project_id,
        visibility="project" if project_id else "user",
        tags=list(payload.tags),
        status="active",
    )
    session.add(judge)
    session.flush()
    audit_record(
        session,
        actor=actor,
        action="create_judge",
        entity_type="judge",
        entity_id=judge.judge_id,
        details={"name": judge.name, "model": judge.model},
    )
    return judge


async def create_judge(request: Request) -> JSONResponse:
    body = await parse_json_object(request)
    payload = JudgeCreateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)

    factory = get_session_factory(request)
    with factory() as session:
        try:
            judge = create_judge_record(
                session, payload=payload, actor=actor, project_id=identity.active_project_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        session.commit()
        data = JudgeSchema.model_validate(judge)
    return envelope_response(data, status_code=201)


async def update_judge(request: Request) -> JSONResponse:
    judge_id = request.path_params["judge_id"]
    body = await parse_json_object(request)
    payload = JudgeUpdateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_ADMIN])

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="request body must include at least one field")

    factory = get_session_factory(request)
    with factory() as session:
        judge = session.get(CaliberJudge, judge_id)
        if judge is None:
            raise HTTPException(status_code=404, detail=f"judge {judge_id!r} not found")

        diff: dict[str, dict[str, object]] = {}
        for field in _UPDATABLE_FIELDS:
            if field not in changes:
                continue
            new_value = changes[field]
            old_value = getattr(judge, field)
            if new_value != old_value:
                diff[field] = {"from": old_value, "to": new_value}
                setattr(judge, field, new_value)

        if not diff:
            return envelope_response(JudgeSchema.model_validate(judge))

        audit_record(
            session,
            actor=actor,
            action="update_judge",
            entity_type="judge",
            entity_id=judge_id,
            details={"changed": sorted(diff)},
        )
        session.commit()
        data = JudgeSchema.model_validate(judge)
    return envelope_response(data)


async def test_run_judge(request: Request) -> JSONResponse:
    """``POST /caliber/judges/{judge_id}/test-run`` — the "Try it" playground.

    Builds the judge through the unified path and runs it once on a sample
    (inputs/outputs/expectations) without persisting anything, returning the unit
    score + raw verdict + rationale. Lets an author sanity-check a judge before
    selecting it as a scorer. Requires a real judge model/gateway at runtime.
    """
    require_user(request)
    judge_id = request.path_params["judge_id"]
    body = await parse_json_object(request)
    payload = JudgeTestRunRequest.model_validate(body)

    factory = get_session_factory(request)
    with factory() as session:
        judge = session.get(CaliberJudge, judge_id)
        if judge is None:
            raise HTTPException(status_code=404, detail=f"judge {judge_id!r} not found")
        name, instructions = judge.name, judge.instructions
        model, value_type = judge.model, judge.feedback_value_type

    try:
        judge_obj = build_judge(name, instructions, model=model, feedback_value_type=value_type)
        outcome = score_with_judge(
            judge_obj,
            inputs=payload.inputs,
            outputs=payload.outputs,
            expectations=payload.expectations or None,
        )
    except JudgeError as exc:
        raise HTTPException(status_code=502, detail=f"judge run failed: {exc}") from exc

    return envelope_response(
        JudgeTestRunResult(score=outcome.score, value=outcome.value, rationale=outcome.rationale)
    )


async def align_judge(request: Request) -> JSONResponse:
    """``POST /caliber/judges/{judge_id}/alignment`` — judge vs human agreement.

    Runs the judge over human-labeled examples, thresholds each unit score into a
    pass/fail verdict, and reports the agreement rate + Cohen's kappa against the
    human labels (plus a binary confusion breakdown). This is the trust check: a
    judge that doesn't agree with humans shouldn't gate releases. Examples whose
    judge call errors are reported but excluded from the agreement math.
    """
    require_user(request)
    judge_id = request.path_params["judge_id"]
    body = await parse_json_object(request)
    payload = JudgeAlignmentRequest.model_validate(body)

    factory = get_session_factory(request)
    with factory() as session:
        judge = session.get(CaliberJudge, judge_id)
        if judge is None:
            raise HTTPException(status_code=404, detail=f"judge {judge_id!r} not found")
        name, instructions = judge.name, judge.instructions
        model, value_type = judge.model, judge.feedback_value_type

    try:
        judge_obj = build_judge(name, instructions, model=model, feedback_value_type=value_type)
    except JudgeError as exc:
        raise HTTPException(status_code=502, detail=f"failed to build judge: {exc}") from exc

    rows: list[JudgeAlignmentPerExample] = []
    judge_labels: list[bool] = []
    human_labels: list[bool] = []
    for example in payload.examples:
        try:
            outcome = score_with_judge(
                judge_obj,
                inputs=example.inputs,
                outputs=example.outputs,
                expectations=example.expectations or None,
            )
        except JudgeError as exc:
            rows.append(
                JudgeAlignmentPerExample(
                    outputs=example.outputs,
                    human_label=example.label,
                    judge_label=None,
                    judge_score=None,
                    agree=False,
                    error=str(exc),
                )
            )
            continue
        judge_label = outcome.score >= payload.threshold
        rows.append(
            JudgeAlignmentPerExample(
                outputs=example.outputs,
                human_label=example.label,
                judge_label=judge_label,
                judge_score=outcome.score,
                agree=judge_label == example.label,
            )
        )
        judge_labels.append(judge_label)
        human_labels.append(example.label)

    result = JudgeAlignmentResult(
        n=len(payload.examples),
        scored=len(judge_labels),
        agreement_rate=round(observed_agreement(judge_labels, human_labels), 4),
        cohen_kappa=round(cohen_kappa(judge_labels, human_labels), 4),
        threshold=payload.threshold,
        confusion=confusion_counts(judge_labels, human_labels),
        per_example=rows,
    )
    return envelope_response(result)


def register(app: Starlette) -> None:
    app.routes.append(Route(LIST_PATH, list_judges, methods=["GET"]))
    app.routes.append(Route(LIST_PATH, create_judge, methods=["POST"]))
    app.routes.append(Route(DETAIL_PATH, get_judge, methods=["GET"]))
    app.routes.append(Route(DETAIL_PATH, update_judge, methods=["PATCH"]))
    app.routes.append(Route(TEST_RUN_PATH, test_run_judge, methods=["POST"]))
    app.routes.append(Route(ALIGNMENT_PATH, align_judge, methods=["POST"]))
