"""``/caliber/review-queues`` endpoints — structured human review of traces.

The CALIBER-native analogue of MLflow Review Queues (Databricks-only). A queue
defines a label schema of review questions; items pin observed traces; reviewers
answer the questions and the answers are written back onto each trace as MLflow
assessments (feedback) or expectations (ground truth) via the OSS primitives.

Surface:

* ``GET /review-queues`` — list (with item/pending counts).
* ``POST /review-queues`` (operator) — create.
* ``GET /review-queues/{queue_id}`` — queue + its items.
* ``PATCH /review-queues/{queue_id}`` (admin) — update / archive.
* ``POST /review-queues/{queue_id}/items`` (operator) — enqueue traces.
* ``POST /review-queues/{queue_id}/items/{item_id}/submit`` — answer + write back.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, cast

from sqlalchemy import case, func, select, update
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
from caliber.db.models import CaliberReviewItem, CaliberReviewQueue
from caliber.db.scoping import apply_visibility_filter, get_visible
from caliber.ids import new_review_item_id, new_review_queue_id
from caliber.review.writeback import (
    AnswerWriteBack,
    MLflowReviewWriteBackClient,
    ReviewWriteBackClient,
)
from caliber.routes._deps import (
    envelope_response,
    envelope_response_dict,
    get_session_factory,
    parse_json_object,
    visibility_param,
)
from caliber.schemas import (
    ReviewItemsAddRequest,
    ReviewItemSchema,
    ReviewItemSubmitRequest,
    ReviewQueueCreateRequest,
    ReviewQueueSchema,
    ReviewQueueUpdateRequest,
)
from caliber.trace_client import fetch_trace_detail

logger = logging.getLogger(__name__)

LIST_PATH = "/ajax-api/2.0/mlflow/caliber/review-queues"
DETAIL_PATH = "/ajax-api/2.0/mlflow/caliber/review-queues/{queue_id}"
ITEMS_PATH = "/ajax-api/2.0/mlflow/caliber/review-queues/{queue_id}/items"
SUBMIT_PATH = "/ajax-api/2.0/mlflow/caliber/review-queues/{queue_id}/items/{item_id}/submit"
ALIGNMENT_EXAMPLES_PATH = "/ajax-api/2.0/mlflow/caliber/review-queues/{queue_id}/alignment-examples"

_LIST_STATUS_VALUES: frozenset[str] = frozenset({"active", "archived", "all"})


def _resolve_writeback_client(request: Request) -> ReviewWriteBackClient:
    override: ReviewWriteBackClient | None = getattr(
        request.app.state, "review_writeback_client", None
    )
    if override is not None:
        return override
    return MLflowReviewWriteBackClient()


def _queue_schema_with_counts(
    queue: CaliberReviewQueue, item_count: int, pending_count: int
) -> ReviewQueueSchema:
    schema = ReviewQueueSchema.model_validate(queue)
    schema.item_count = item_count
    schema.pending_count = pending_count
    return schema


async def list_queues(request: Request) -> JSONResponse:
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
    with factory() as session:
        stmt = select(CaliberReviewQueue).order_by(CaliberReviewQueue.name)
        if requested_status != "all":
            stmt = stmt.where(CaliberReviewQueue.status == requested_status)
        stmt = apply_visibility_filter(
            stmt,
            CaliberReviewQueue,
            identity,
            identity.active_project_id,
            only=visibility_param(request),
        )
        rows = session.execute(stmt).scalars().all()
        # Total + pending item counts for every queue in one grouped query,
        # rather than two COUNT round-trips per queue (was 2N queries). The
        # ``case`` sum is used over ``count().filter(...)`` for portability
        # across the SQLite/Postgres backends CALIBER targets.
        queue_ids = [queue.queue_id for queue in rows]
        counts: dict[str, tuple[int, int]] = dict.fromkeys(queue_ids, (0, 0))
        if queue_ids:
            count_stmt = (
                select(
                    CaliberReviewItem.queue_id,
                    func.count().label("total"),
                    func.sum(case((CaliberReviewItem.status == "pending", 1), else_=0)).label(
                        "pending"
                    ),
                )
                .where(CaliberReviewItem.queue_id.in_(queue_ids))
                .group_by(CaliberReviewItem.queue_id)
            )
            for queue_id, total, pending in session.execute(count_stmt):
                counts[queue_id] = (int(total or 0), int(pending or 0))
        items = [
            _queue_schema_with_counts(queue, *counts.get(queue.queue_id, (0, 0))) for queue in rows
        ]
    return envelope_response(items)


def create_review_queue_record(
    session: Any,
    *,
    payload: ReviewQueueCreateRequest,
    actor: str,
    project_id: str | None,
) -> CaliberReviewQueue:
    """Create a review-queue row (single definition reused by the route + Aria).

    Raises :class:`ValueError` on a duplicate name; flushes but does not commit
    (the caller owns the transaction).
    """
    existing = (
        session.execute(select(CaliberReviewQueue).where(CaliberReviewQueue.name == payload.name))
        .scalars()
        .first()
    )
    if existing is not None:
        raise ValueError(f"review-queue name {payload.name!r} is already in use")
    queue = CaliberReviewQueue(
        queue_id=new_review_queue_id(),
        name=payload.name,
        description=payload.description,
        questions=[q.model_dump() for q in payload.questions],
        reviewers=list(payload.reviewers),
        owner=actor,
        project_id=project_id,
        visibility="project" if project_id else "user",
        status="active",
    )
    session.add(queue)
    session.flush()
    audit_record(
        session,
        actor=actor,
        action="create_review_queue",
        entity_type="review_queue",
        entity_id=queue.queue_id,
        details={"name": queue.name, "questions": len(queue.questions)},
    )
    return queue


async def create_queue(request: Request) -> JSONResponse:
    body = await parse_json_object(request)
    payload = ReviewQueueCreateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)

    factory = get_session_factory(request)
    with factory() as session:
        try:
            queue = create_review_queue_record(
                session, payload=payload, actor=actor, project_id=identity.active_project_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        session.commit()
        data = _queue_schema_with_counts(queue, 0, 0)
    return envelope_response(data, status_code=201)


async def get_queue(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    queue_id = request.path_params["queue_id"]
    factory = get_session_factory(request)
    with factory() as session:
        queue = get_visible(
            session, CaliberReviewQueue, CaliberReviewQueue.queue_id, queue_id, identity
        )
        if queue is None:
            raise HTTPException(status_code=404, detail=f"review queue {queue_id!r} not found")
        rows = (
            session.execute(
                select(CaliberReviewItem)
                .where(CaliberReviewItem.queue_id == queue_id)
                .order_by(CaliberReviewItem.created_at)
            )
            .scalars()
            .all()
        )
        item_schemas = [ReviewItemSchema.model_validate(row) for row in rows]
        pending = sum(1 for row in rows if row.status == "pending")
        queue_schema = _queue_schema_with_counts(queue, len(rows), pending)
    return envelope_response_dict(
        {
            "queue": queue_schema.model_dump(mode="json"),
            "items": [item.model_dump(mode="json") for item in item_schemas],
        }
    )


_UPDATABLE_FIELDS = ("description", "questions", "reviewers", "status")


async def update_queue(request: Request) -> JSONResponse:
    queue_id = request.path_params["queue_id"]
    body = await parse_json_object(request)
    payload = ReviewQueueUpdateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_ADMIN])

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="request body must include at least one field")

    factory = get_session_factory(request)
    with factory() as session:
        queue = session.get(CaliberReviewQueue, queue_id)
        if queue is None:
            raise HTTPException(status_code=404, detail=f"review queue {queue_id!r} not found")
        changed: list[str] = []
        for field in _UPDATABLE_FIELDS:
            if field not in changes:
                continue
            setattr(queue, field, changes[field])
            changed.append(field)
        if changed:
            audit_record(
                session,
                actor=actor,
                action="update_review_queue",
                entity_type="review_queue",
                entity_id=queue_id,
                details={"changed": sorted(changed)},
            )
        session.commit()
        total = session.execute(
            select(func.count())
            .select_from(CaliberReviewItem)
            .where(CaliberReviewItem.queue_id == queue_id)
        ).scalar_one()
        pending = session.execute(
            select(func.count())
            .select_from(CaliberReviewItem)
            .where(CaliberReviewItem.queue_id == queue_id)
            .where(CaliberReviewItem.status == "pending")
        ).scalar_one()
        data = _queue_schema_with_counts(queue, int(total), int(pending))
    return envelope_response(data)


def add_review_items_records(
    session: Any,
    *,
    queue_id: str,
    trace_ids: list[str],
    experiment_id: str | None,
    assigned_to: str | None,
    actor: str,
) -> list[CaliberReviewItem]:
    """Enqueue traces into a review queue (single definition; route + Aria reuse).

    Idempotent — a trace is queued at most once per queue. Raises
    :class:`ValueError` if the queue is missing; flushes but does not commit.
    """
    queue = session.get(CaliberReviewQueue, queue_id)
    if queue is None:
        raise ValueError(f"review queue {queue_id!r} not found")
    if queue.status != "active":
        raise ValueError(f"review queue {queue_id!r} is not active")
    existing_traces = {
        row.trace_id
        for row in session.execute(
            select(CaliberReviewItem).where(CaliberReviewItem.queue_id == queue_id)
        )
        .scalars()
        .all()
    }
    created: list[CaliberReviewItem] = []
    for trace_id in trace_ids:
        if trace_id in existing_traces:
            continue
        existing_traces.add(trace_id)
        item = CaliberReviewItem(
            item_id=new_review_item_id(),
            queue_id=queue_id,
            trace_id=trace_id,
            experiment_id=experiment_id,
            status="pending",
            assigned_to=assigned_to,
            answers={},
            assessment_ids=[],
        )
        session.add(item)
        session.flush()
        created.append(item)
    audit_record(
        session,
        actor=actor,
        action="add_review_items",
        entity_type="review_queue",
        entity_id=queue_id,
        details={"added": len(created)},
    )
    return created


async def add_items(request: Request) -> JSONResponse:
    queue_id = request.path_params["queue_id"]
    body = await parse_json_object(request)
    payload = ReviewItemsAddRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)

    factory = get_session_factory(request)
    with factory() as session:
        queue = get_visible(
            session, CaliberReviewQueue, CaliberReviewQueue.queue_id, queue_id, identity
        )
        if queue is None:
            raise HTTPException(status_code=404, detail=f"review queue {queue_id!r} not found")
        try:
            created = add_review_items_records(
                session,
                queue_id=queue_id,
                trace_ids=list(payload.trace_ids),
                experiment_id=payload.experiment_id,
                assigned_to=payload.assigned_to,
                actor=actor,
            )
        except ValueError as exc:
            status_code = 409 if "is not active" in str(exc) else 404
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        schemas = [ReviewItemSchema.model_validate(item) for item in created]
        session.commit()
    return envelope_response(schemas, status_code=201)


def _build_writebacks(
    questions: list[dict[str, Any]], answers: dict[str, Any]
) -> list[AnswerWriteBack]:
    """Validate the submitted answers against the queue schema and map them to
    write-backs. Raises HTTP 400 on a missing required answer / unknown key."""
    by_key = {str(q.get("key")): q for q in questions}
    unknown = set(answers) - set(by_key)
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown answer keys: {sorted(unknown)}")
    writebacks: list[AnswerWriteBack] = []
    for key, question in by_key.items():
        if key not in answers or answers[key] is None or answers[key] == "":
            if question.get("required", True):
                raise HTTPException(status_code=400, detail=f"missing required answer {key!r}")
            continue
        value = answers[key]
        question_type = str(question.get("type", "pass_fail"))
        if question_type == "pass_fail" and not isinstance(value, bool):
            raise HTTPException(status_code=400, detail=f"answer {key!r} must be a boolean")
        if question_type == "categorical" and (
            not isinstance(value, str) or value not in question.get("options", [])
        ):
            raise HTTPException(
                status_code=400,
                detail=f"answer {key!r} must be one of {question.get('options', [])}",
            )
        if question_type == "numeric" and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            raise HTTPException(status_code=400, detail=f"answer {key!r} must be numeric")
        if question_type == "text" and not isinstance(value, str):
            raise HTTPException(status_code=400, detail=f"answer {key!r} must be text")
        writebacks.append(
            AnswerWriteBack(
                name=key,
                value=value,
                target=str(question.get("target", "feedback")),
            )
        )
    return writebacks


def _alignment_label(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"pass", "passed", "true", "yes", "1"}:
            return True
        if normalized in {"fail", "failed", "false", "no", "0"}:
            return False
    return None


def alignment_examples(request: Request) -> JSONResponse:
    """Prepare completed queue labels for the Judge Human-alignment panel."""

    require_user(request)
    identity = resolve_identity(request)
    queue_id = request.path_params["queue_id"]
    question_key = str(request.query_params.get("question_key") or "").strip()
    if not question_key:
        raise HTTPException(status_code=400, detail="question_key is required")

    factory = get_session_factory(request)
    with factory() as session:
        queue = get_visible(
            session, CaliberReviewQueue, CaliberReviewQueue.queue_id, queue_id, identity
        )
        if queue is None:
            raise HTTPException(status_code=404, detail=f"review queue {queue_id!r} not found")
        question = next(
            (item for item in queue.questions if str(item.get("key")) == question_key), None
        )
        if question is None:
            raise HTTPException(status_code=400, detail=f"unknown question {question_key!r}")
        if question.get("type") != "pass_fail":
            raise HTTPException(
                status_code=400, detail="alignment import requires pass_fail labels"
            )
        items = (
            session.execute(
                select(CaliberReviewItem)
                .where(
                    CaliberReviewItem.queue_id == queue_id,
                    CaliberReviewItem.status == "completed",
                )
                .order_by(CaliberReviewItem.completed_at, CaliberReviewItem.item_id)
            )
            .scalars()
            .all()
        )

    examples: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for item in items:
        label = _alignment_label(item.answers.get(question_key))
        if label is None:
            skipped.append({"item_id": item.item_id, "reason": "label is not pass/fail"})
            continue
        detail = fetch_trace_detail(item.trace_id)
        if detail.response is None or detail.response == "":
            skipped.append({"item_id": item.item_id, "reason": "trace response unavailable"})
            continue
        outputs = (
            detail.response
            if isinstance(detail.response, str)
            else json.dumps(detail.response, sort_keys=True, ensure_ascii=False)
        )
        examples.append(
            {
                "inputs": {
                    "trace_id": item.trace_id,
                    "request": detail.request,
                    "review_item_id": item.item_id,
                },
                "outputs": outputs,
                "expectations": {
                    key: value for key, value in item.answers.items() if key != question_key
                },
                "label": label,
                "provenance": {
                    "queue_id": queue_id,
                    "item_id": item.item_id,
                    "trace_id": item.trace_id,
                    "question_key": question_key,
                    "completed_by": item.completed_by,
                    "assessment_ids": list(item.assessment_ids),
                },
            }
        )
    return envelope_response_dict(
        {
            "queue_id": queue_id,
            "question_key": question_key,
            "examples": examples,
            "skipped": skipped,
        }
    )


async def submit_item(request: Request) -> JSONResponse:
    queue_id = request.path_params["queue_id"]
    item_id = request.path_params["item_id"]
    body = await parse_json_object(request)
    payload = ReviewItemSubmitRequest.model_validate(body)
    actor = require_user(request)
    identity = resolve_identity(request)

    factory = get_session_factory(request)
    with factory() as session:
        queue = get_visible(
            session, CaliberReviewQueue, CaliberReviewQueue.queue_id, queue_id, identity
        )
        if queue is None:
            raise HTTPException(status_code=404, detail=f"review queue {queue_id!r} not found")
        if queue.status != "active":
            raise HTTPException(status_code=409, detail=f"review queue {queue_id!r} is not active")
        item = session.get(CaliberReviewItem, item_id)
        if item is None or item.queue_id != queue_id:
            raise HTTPException(status_code=404, detail=f"review item {item_id!r} not found")
        if item.status != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"review item {item_id!r} is already {item.status}",
            )
        writebacks = _build_writebacks(list(queue.questions), dict(payload.answers))
        trace_id = item.trace_id
        # Claim the item before the external write-back. The conditional update
        # prevents two concurrent requests from both recording answers.
        claimed = session.execute(
            update(CaliberReviewItem)
            .where(CaliberReviewItem.item_id == item_id)
            .where(CaliberReviewItem.status == "pending")
            .values(status="submitting")
        )
        if cast(Any, claimed).rowcount != 1:
            session.rollback()
            raise HTTPException(
                status_code=409, detail=f"review item {item_id!r} is being submitted"
            )
        session.commit()

    # Write answers back to the trace (outside the session — guarded MLflow call).
    client = _resolve_writeback_client(request)
    try:
        assessment_ids = client.write_answers(trace_id=trace_id, answers=writebacks, user=actor)
    except Exception as exc:  # pragma: no cover - exercised via raising fake
        logger.warning("review write-back failed for trace %s (%s)", trace_id, exc)
        # A failed external call is retryable. Only release the claim if this
        # request still owns the transient state.
        with factory() as session:
            session.execute(
                update(CaliberReviewItem)
                .where(CaliberReviewItem.item_id == item_id)
                .where(CaliberReviewItem.status == "submitting")
                .values(status="pending")
            )
            session.commit()
        raise HTTPException(
            status_code=502, detail=f"failed writing review answers to trace: {exc}"
        ) from exc

    with factory() as session:
        item = session.get(CaliberReviewItem, item_id)
        if item is None:  # pragma: no cover - deleted mid-submit
            raise HTTPException(status_code=404, detail=f"review item {item_id!r} not found")
        if item.status != "submitting":
            raise HTTPException(
                status_code=409,
                detail=f"review item {item_id!r} is no longer awaiting completion",
            )
        item.answers = dict(payload.answers)
        item.assessment_ids = list(assessment_ids)
        item.status = "completed"
        item.completed_at = datetime.now(timezone.utc)
        item.completed_by = actor
        audit_record(
            session,
            actor=actor,
            action="submit_review_item",
            entity_type="review_queue",
            entity_id=queue_id,
            details={"item_id": item_id, "assessments": len(assessment_ids)},
        )
        session.commit()
        data = ReviewItemSchema.model_validate(item)
    return envelope_response(data)


def register(app: Starlette) -> None:
    app.routes.append(Route(LIST_PATH, list_queues, methods=["GET"]))
    app.routes.append(Route(LIST_PATH, create_queue, methods=["POST"]))
    app.routes.append(Route(DETAIL_PATH, get_queue, methods=["GET"]))
    app.routes.append(Route(DETAIL_PATH, update_queue, methods=["PATCH"]))
    app.routes.append(Route(ITEMS_PATH, add_items, methods=["POST"]))
    app.routes.append(Route(ALIGNMENT_EXAMPLES_PATH, alignment_examples, methods=["GET"]))
    app.routes.append(Route(SUBMIT_PATH, submit_item, methods=["POST"]))
