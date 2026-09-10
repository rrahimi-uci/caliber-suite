"""``/caliber/jobs/{job_id}/quality-reviews`` endpoints — the QA sign-off.

Wires :class:`caliber.db.models.CaliberQualityReview`, an append-only human
go/no-go decision on a refinement job's candidate, distinct from the machine
eval gate. See ``docs/workspace-plan.md`` section 3.6, item 2.

* ``POST /jobs/{job_id}/quality-reviews`` (operator) — record a decision.
  Requires ``job.status == "candidate_ready"``. ``"go"`` is purely advisory:
  it records the row and does not touch the job, so ``POST
  /jobs/{job_id}/apply`` is not coupled to it (matching
  ``routes/gate_verdicts.py``'s "advisory in v1" precedent). ``"no_go"`` is
  not advisory: it terminally rejects the job (``candidate_ready ->
  rejected``, same conditional-UPDATE claim idiom
  ``routes/jobs.py::apply_job``/``request_changes`` use) and creates a
  :class:`caliber.db.models.CaliberReworkTask` with ``failure_kind =
  "quality_no_go"`` in the same transaction -- the exact pattern
  ``orchestrator/eval_stage.py`` uses for a machine-gate rejection, just
  triggered by a human decision instead of the gate.
* ``GET /jobs/{job_id}/quality-reviews`` — a job's review history, newest
  first.

This is a standalone module rather than an addition to ``routes/jobs.py``,
matching ``routes/rework_tasks.py``'s precedent: a new record type with its
own identity gets its own module even when its URL nests under
``/jobs/{job_id}/...``. The OpenAPI tag is derived from the URL path (see
``routes/openapi.py::_tag_for``), not the Python module, so these routes
still land under the existing ``jobs`` tag with no new stability entry
needed.

No distinct-actor / separation-of-duty check exists here, matching
``apply_job``/``request_changes``'s existing precedent -- closing that gap
needs the stored Workspace roles ``docs/workspace-plan.md`` section 5.1
describes, which don't exist yet.

Every handler offloads its synchronous SQLAlchemy work to
:func:`starlette.concurrency.run_in_threadpool`, matching
``routes/rework_tasks.py`` — see ``tests/test_async_offload_ratchet.py``.
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import SCOPE_OPERATOR, require_scopes, require_user
from caliber.db.models import CaliberQualityReview, CaliberRefinementJob, CaliberReworkTask
from caliber.ids import new_quality_review_id, new_rework_task_id
from caliber.routes._deps import envelope_response, get_session_factory, parse_json_object
from caliber.schemas import QualityReviewRequest, QualityReviewSchema

#: One URL, two methods (POST creates, GET lists) -- both routes below
#: register against this same path.
PATH = "/ajax-api/2.0/mlflow/caliber/jobs/{job_id}/quality-reviews"

_Factory = sessionmaker[Session]


def _submit_quality_review_sync(
    factory: _Factory, *, job_id: str, actor: str, payload: QualityReviewRequest
) -> QualityReviewSchema:
    with factory() as session:
        if payload.decision == "no_go":
            # Same conditional-UPDATE claim idiom as apply_job/request_changes:
            # the transition lives in this transaction, so a racing Apply or
            # request-changes call observes rowcount=0 before any further
            # effect runs.
            claim = session.execute(
                update(CaliberRefinementJob)
                .where(CaliberRefinementJob.job_id == job_id)
                .where(CaliberRefinementJob.status == "candidate_ready")
                .values(
                    status="rejected",
                    current_stage="done",
                    error_message=f"quality review: {payload.rationale}",
                )
            )
            if int(getattr(claim, "rowcount", 0) or 0) != 1:
                current = session.get(CaliberRefinementJob, job_id)
                if current is None:
                    raise HTTPException(
                        status_code=404, detail=f"refinement job {job_id!r} not found"
                    )
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"job {job_id!r} cannot be quality-reviewed: "
                        f"current status is {current.status!r} (expected 'candidate_ready')"
                    ),
                )
            job = session.get(CaliberRefinementJob, job_id)
            assert job is not None
        else:
            job = session.get(CaliberRefinementJob, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"refinement job {job_id!r} not found")
            if job.status != "candidate_ready":
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"job {job_id!r} cannot be quality-reviewed: "
                        f"current status is {job.status!r} (expected 'candidate_ready')"
                    ),
                )

        review = CaliberQualityReview(
            review_id=new_quality_review_id(),
            job_id=job.job_id,
            agent_id=job.agent_id,
            decision=payload.decision,
            rationale=payload.rationale,
            decided_by=actor,
            candidate_snapshot=dict(job.candidate) if job.candidate else None,
            eval_results_snapshot=dict(job.eval_results) if job.eval_results else None,
        )
        session.add(review)
        session.flush()
        audit_record(
            session,
            actor=actor,
            action="submit_quality_review",
            entity_type="refinement_job",
            entity_id=job.job_id,
            details={"review_id": review.review_id, "decision": payload.decision},
        )

        if payload.decision == "no_go":
            task = CaliberReworkTask(
                task_id=new_rework_task_id(),
                job_id=job.job_id,
                agent_id=job.agent_id,
                failure_kind="quality_no_go",
                reason=payload.rationale,
                gate_evidence=None,
                status="open",
                created_by=actor,
            )
            session.add(task)
            session.flush()
            audit_record(
                session,
                actor=actor,
                action="create_rework_task",
                entity_type="rework_task",
                entity_id=task.task_id,
                details={"job_id": job.job_id, "failure_kind": task.failure_kind},
            )

        session.commit()
        return QualityReviewSchema.model_validate(review)


async def submit_quality_review(request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    body = await parse_json_object(request)
    payload = QualityReviewRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])

    factory = get_session_factory(request)
    schema = await run_in_threadpool(
        _submit_quality_review_sync, factory, job_id=job_id, actor=actor, payload=payload
    )
    return envelope_response(schema, status_code=201)


def _list_quality_reviews_sync(factory: _Factory, *, job_id: str) -> list[QualityReviewSchema]:
    with factory() as session:
        job = session.get(CaliberRefinementJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"refinement job {job_id!r} not found")
        rows = (
            session.execute(
                select(CaliberQualityReview)
                .where(CaliberQualityReview.job_id == job_id)
                .order_by(CaliberQualityReview.created_at.desc())
            )
            .scalars()
            .all()
        )
        return [QualityReviewSchema.model_validate(row) for row in rows]


async def list_quality_reviews(request: Request) -> JSONResponse:
    require_user(request)
    job_id = request.path_params["job_id"]
    factory = get_session_factory(request)
    schemas = await run_in_threadpool(_list_quality_reviews_sync, factory, job_id=job_id)
    return envelope_response(schemas)


def register(app: Starlette) -> None:
    app.routes.append(Route(PATH, submit_quality_review, methods=["POST"]))
    app.routes.append(Route(PATH, list_quality_reviews, methods=["GET"]))
