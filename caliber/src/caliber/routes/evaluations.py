"""``/caliber/evaluations`` endpoints — the scorecard surface.

Mirrors MLflow's evaluation UI inside CALIBER: run an eval dataset's examples
through a predict target + deterministic scorers and persist the per-example
results so users can see *which* examples passed/failed and compare runs over
time — not just the aggregate the refinement gate records.

Surface:

* ``GET /evaluations[?dataset_id=&limit=]`` — list run summaries (no heavy rows).
* ``GET /evaluations/{run_id}`` — one run with its full per-example results.
* ``POST /evaluations`` (operator) — run a dataset through the scorers now.

The run is synchronous (governance datasets are small); a real LLM provider is
required for the ``llm`` predict target — there is never a fabricated-score
fallback, matching :mod:`caliber.eval.predict`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select
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
from caliber.db.models import (
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberEvalRun,
    CaliberJudge,
    CaliberSkill,
)
from caliber.db.scoping import apply_visibility_filter
from caliber.eval.judge_scorer import JudgeError, build_judge, score_with_judge
from caliber.eval.predict import (
    CompletionFn,
    build_completion_fn,
    build_default_predict_fn_factory,
    user_message,
)
from caliber.eval.scorecard import JUDGE_SCORER_PREFIX, JudgeRunner, PredictFn, run_scorecard
from caliber.ids import new_eval_run_id
from caliber.routes._deps import (
    envelope_response,
    get_session_factory,
    parse_json_object,
    visibility_param,
)
from caliber.schemas import (
    EvalRunCreateRequest,
    EvalRunSchema,
    EvalRunSummarySchema,
)

LIST_PATH = "/ajax-api/2.0/mlflow/caliber/evaluations"
DETAIL_PATH = "/ajax-api/2.0/mlflow/caliber/evaluations/{run_id}"

# Default cap on examples scored when the caller doesn't pin ``max_examples`` —
# the run blocks on one LLM call per example, so an unbounded set would risk a
# request timeout. Governance datasets are typically well under this.
_DEFAULT_MAX_EXAMPLES = 50

# Workflow scoring runs a full workflow per example (many model/tool calls), so
# the synchronous run is bounded tighter than the generic-completion cap.
_WORKFLOW_MAX_EXAMPLES = 20

# Neutral instruction for the ``llm`` predict target: the model under test
# answers each example's input directly, then the scorers compare to the gold
# expectation. Operators annotate the model/intent via the run ``label``.
_SCORE_SYSTEM_PROMPT = (
    "You are being evaluated. Answer the user's request as accurately and "
    "concisely as possible, using only the information provided."
)


def _load_example_rows(session: Any, dataset_id: str, version: int | None) -> list[dict[str, Any]]:
    """Return ``{example_id, input, expected}`` rows for a dataset.

    Same version semantics as :func:`caliber.eval.predict.build_db_load_dataset`:
    ``None`` returns the current active set; a pinned ``version`` reconstructs
    the historical set "as of version N" for a reproducible run.
    """
    stmt = (
        select(CaliberEvalDatasetExample)
        .where(CaliberEvalDatasetExample.dataset_id == dataset_id)
        .order_by(CaliberEvalDatasetExample.created_at)
    )
    if version is None:
        stmt = stmt.where(CaliberEvalDatasetExample.superseded_at.is_(None))
    else:
        stmt = stmt.where(CaliberEvalDatasetExample.dataset_version <= version).where(
            or_(
                CaliberEvalDatasetExample.superseded_version.is_(None),
                CaliberEvalDatasetExample.superseded_version > version,
            )
        )
    rows = session.execute(stmt).scalars().all()
    return [
        {
            "example_id": row.example_id,
            "input": dict(row.input or {}),
            "expected": dict(row.expected or {}),
        }
        for row in rows
    ]


def load_prompt_template(subject_ref: str) -> str:
    """Load a registered prompt version's template text for a ``prompt`` target.

    ``subject_ref`` is ``"<name>@<version>"`` (version = an integer or a registry
    alias) or ``"<name>"`` (treated as version 1). Mirrors the prompt route's use
    of ``mlflow.load_prompt(prompts:/<name>/<version>)``. Module-level so tests
    can monkeypatch it without a live prompt registry. Raises ``HTTPException``.
    """
    name, sep, version = subject_ref.partition("@")
    name = name.strip()
    version = version.strip() if sep else "1"
    if not name:
        raise HTTPException(status_code=400, detail="prompt subject_ref must include a name")
    ref = f"prompts:/{name}/{version}"
    try:
        import mlflow  # noqa: PLC0415

        load_prompt = getattr(mlflow, "load_prompt", None)
        if load_prompt is None:
            raise HTTPException(status_code=503, detail="mlflow prompt registry API not available")
        prompt = load_prompt(ref, allow_missing=True)
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - registry/transport failure
        raise HTTPException(
            status_code=502, detail=f"failed to load prompt {subject_ref!r}: {exc}"
        ) from exc
    if prompt is None:
        raise HTTPException(status_code=404, detail=f"prompt {subject_ref!r} not found")
    template = getattr(prompt, "template", None) or getattr(prompt, "content", None) or ""
    if not str(template).strip():
        raise HTTPException(
            status_code=400, detail=f"prompt {subject_ref!r} has no template content"
        )
    return str(template)


def _artifact_predict(complete: CompletionFn, system_text: str) -> PredictFn:
    """A predict_fn that renders ``system_text`` (a prompt/skill body) as the
    system instruction with ``{{var}}`` filled from inputs — so the *artifact* is
    what's scored, not a generic completion. Reuses the gate's render logic."""
    predict_fn = build_default_predict_fn_factory(complete)(system_text)

    def predict(inputs: Mapping[str, Any]) -> str:
        return str(predict_fn(**dict(inputs)))

    return predict


def _llm_predict(complete: CompletionFn) -> PredictFn:
    """The neutral generic-completion predict for the ``llm`` target."""

    def predict(inputs: Mapping[str, Any]) -> str:
        return complete(_SCORE_SYSTEM_PROMPT, user_message(dict(inputs)))

    return predict


def _resolve_predict(
    payload: EvalRunCreateRequest,
    complete: CompletionFn,
    skill_content: str | None,
    workflow_predict: PredictFn | None,
) -> PredictFn:
    """Pick the predict callable for the run's target.

    ``prompt`` / ``skill`` render the *artifact* as the system instruction (so the
    artifact is what's under test); ``workflow`` reuses the pre-compiled
    workflow predict built in the session block; ``llm`` keeps the neutral
    generic-completion behaviour.
    """
    if payload.predict_target == "prompt":
        return _artifact_predict(
            complete, load_prompt_template((payload.subject_ref or "").strip())
        )
    if payload.predict_target == "skill":
        return _artifact_predict(complete, skill_content or "")
    if payload.predict_target == "workflow" and workflow_predict is not None:
        return workflow_predict
    return _llm_predict(complete)


def _build_workflow_predict(session: Any, version_id: str, config: Any) -> PredictFn:
    """Build a predict_fn that scores a real workflow version.

    Compiles the version to a runtime plan + executor **once** (not per example)
    via the same `caliber.workflows.promoter` seam the preview/run paths use, then
    each prediction executes that plan in preview mode (tools sandboxed) and
    returns the workflow's output. Runs synchronously, so the example count is
    capped tighter (``_WORKFLOW_MAX_EXAMPLES``). Raises 404 on an unknown version
    and 400 if the version won't compile.
    """
    from caliber.db.models import CaliberWorkflowVersion  # noqa: PLC0415
    from caliber.workflows.promoter import build_executor, build_plan  # noqa: PLC0415
    from caliber.workflows.runtime import execute  # noqa: PLC0415

    version = session.get(CaliberWorkflowVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail=f"workflow version {version_id!r} not found")
    try:
        plan = build_plan(session, version, config=config)
        executor = build_executor(config, ir=plan.ir)
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"failed to compile workflow {version_id!r}: {exc}"
        ) from exc

    def predict(inputs: Mapping[str, Any]) -> str:
        result = execute(plan, user_message(dict(inputs)), executor=executor, preview=True)
        return str(getattr(result, "output", "") or "")

    return predict


def _make_judge_runner(judge_obj: Any) -> JudgeRunner:
    """Wrap a built judge as a scorecard ``JudgeRunner`` (prediction + context → float)."""

    def runner(prediction: str, inputs: Mapping[str, Any], expected: Mapping[str, Any]) -> float:
        return score_with_judge(
            judge_obj,
            inputs=dict(inputs),
            outputs=prediction,
            expectations=dict(expected) or None,
        ).score

    return runner


def _hydrate_judge_runners(session: Any, scorer_names: list[str]) -> dict[str, JudgeRunner]:
    """Build a ``JudgeRunner`` for every ``Judge.<judge_id>`` token in ``scorer_names``.

    Resolves each token against ``caliber_judges`` and builds the judge through
    the shared :func:`caliber.eval.judge_scorer.build_judge` path — so an
    operator-authored LLM judge now runs from the Evaluations scorecard, not just
    the optimization gate. 404s an unknown/archived judge; 400s a judge whose
    definition ``make_judge`` rejects.
    """
    runners: dict[str, JudgeRunner] = {}
    for name in scorer_names:
        if not name.startswith(JUDGE_SCORER_PREFIX) or name in runners:
            continue
        judge_id = name.partition(".")[2]
        judge = session.get(CaliberJudge, judge_id)
        if judge is None or judge.status != "active":
            raise HTTPException(
                status_code=404,
                detail=f"judge {judge_id!r} not found or not active",
            )
        try:
            judge_obj = build_judge(
                judge.name,
                judge.instructions,
                model=judge.model,
                feedback_value_type=judge.feedback_value_type,
            )
        except JudgeError as exc:
            raise HTTPException(
                status_code=400, detail=f"failed to build judge {judge.name!r}: {exc}"
            ) from exc
        runners[name] = _make_judge_runner(judge_obj)
    return runners


async def list_evaluations(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    factory = get_session_factory(request)
    dataset_filter = request.query_params.get("dataset_id")
    try:
        limit = min(int(request.query_params.get("limit", "100")), 500)
    except ValueError:
        limit = 100
    with factory() as session:
        stmt = select(CaliberEvalRun).order_by(CaliberEvalRun.created_at.desc())
        if dataset_filter:
            stmt = stmt.where(CaliberEvalRun.dataset_id == dataset_filter)
        stmt = apply_visibility_filter(
            stmt,
            CaliberEvalRun,
            identity,
            identity.active_project_id,
            only=visibility_param(request),
        )
        rows = session.execute(stmt.limit(limit)).scalars().all()
    items = [EvalRunSummarySchema.model_validate(row) for row in rows]
    return envelope_response(items)


async def get_evaluation(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    run_id = request.path_params["run_id"]
    factory = get_session_factory(request)
    with factory() as session:
        row = session.get(CaliberEvalRun, run_id)
        # Scope the detail read the same way list_evaluations scopes its list —
        # a bare get() leaked another project's run (and its full per-example
        # results) by id. CaliberEvalRun has no `owner` column, so scope on
        # project + visibility directly (admins see everything).
        visible = row is not None and (
            identity.has_scope(SCOPE_ADMIN)
            or row.visibility == "public"
            or (row.project_id is not None and row.project_id == identity.active_project_id)
        )
        if not visible:
            raise HTTPException(status_code=404, detail=f"evaluation run {run_id!r} not found")
        data = EvalRunSchema.model_validate(row)
    return envelope_response(data)


async def create_evaluation(request: Request) -> JSONResponse:
    body = await parse_json_object(request)
    payload = EvalRunCreateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    config = getattr(request.app.state, "config", None)
    factory = get_session_factory(request)

    skill_content: str | None = None
    workflow_predict: PredictFn | None = None
    with factory() as session:
        dataset = session.get(CaliberEvalDataset, payload.dataset_id)
        if dataset is None:
            raise HTTPException(
                status_code=404, detail=f"eval dataset {payload.dataset_id!r} not found"
            )
        dataset_version = (
            payload.dataset_version if payload.dataset_version is not None else dataset.version
        )
        rows = _load_example_rows(session, payload.dataset_id, payload.dataset_version)
        # Hydrate any ``Judge.<id>`` scorers from the judge registry while the
        # session is open (404/400 here, before we touch the model).
        judge_runners = _hydrate_judge_runners(session, list(payload.scorers or []))
        # A ``skill`` target reads its content (the system instruction) now.
        if payload.predict_target == "skill":
            skill = session.get(CaliberSkill, (payload.subject_ref or "").strip())
            if skill is None or skill.status != "active":
                raise HTTPException(
                    status_code=404,
                    detail=f"skill {payload.subject_ref!r} not found or not active",
                )
            skill_content = skill.content
        # A ``workflow`` target compiles the version once here; the predict fn then
        # executes that plan per example (preview mode → tools sandboxed).
        elif payload.predict_target == "workflow":
            workflow_predict = _build_workflow_predict(
                session, (payload.subject_ref or "").strip(), config
            )

    if not rows:
        suffix = "" if payload.dataset_version is None else f" at version {payload.dataset_version}"
        raise HTTPException(
            status_code=400,
            detail=f"eval dataset {payload.dataset_id!r} has no examples to score{suffix}",
        )

    cap = payload.max_examples or _DEFAULT_MAX_EXAMPLES
    if payload.predict_target == "workflow":
        # Each example runs a full workflow (multiple model/tool calls), so bound
        # the synchronous run tighter than the generic completion cap.
        cap = min(cap, _WORKFLOW_MAX_EXAMPLES)
    if len(rows) > cap:
        rows = rows[:cap]

    # Validate scorers up-front so an unknown name 400s before we call the model.
    # Hydrated judge tokens are passed as allowed so they aren't rejected.
    from caliber.eval.scorecard import resolve_scorers  # noqa: PLC0415

    try:
        resolve_scorers(payload.scorers, allowed_judges=list(judge_runners))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    complete = build_completion_fn(config) if config is not None else None
    if complete is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "evaluation requires a real LLM provider; set CALIBER_LLM_PROVIDER "
                "(openai/anthropic) and the matching API key, then retry."
            ),
        )
    model = getattr(config, "llm_diagnosis_model", None)
    predict = _resolve_predict(payload, complete, skill_content, workflow_predict)

    result = run_scorecard(
        rows,
        predict,
        payload.scorers,
        pass_threshold=payload.pass_threshold,
        judge_runners=judge_runners,
    )

    return _persist_eval_run(
        factory,
        payload=payload,
        dataset_version=dataset_version,
        model=model,
        result=result,
        actor=actor,
        project_id=identity.active_project_id,
    )


def _persist_eval_run(
    factory: Any,
    *,
    payload: EvalRunCreateRequest,
    dataset_version: int,
    model: str | None,
    result: Any,
    actor: str,
    project_id: str | None,
) -> JSONResponse:
    """Persist a finished scorecard run + its audit row, returning the 201 envelope.

    If every row errored the provider is effectively unusable for this run, so we
    mark it ``failed`` (surfacing the first error) rather than reporting a
    misleading all-zero scorecard.
    """
    all_errored = bool(result.rows) and all(row.error for row in result.rows)
    status = "failed" if all_errored else "completed"
    error_message = result.rows[0].error if all_errored else None

    now = datetime.now(timezone.utc)
    with factory() as session:
        run = CaliberEvalRun(
            run_id=new_eval_run_id(),
            dataset_id=payload.dataset_id,
            dataset_version=dataset_version,
            label=payload.label,
            predict_target=payload.predict_target,
            subject_ref=(payload.subject_ref or None) if payload.predict_target != "llm" else None,
            model=model,
            scorers=list(result.scorers),
            pass_threshold=payload.pass_threshold,
            n_examples=result.n_examples,
            passed_count=result.passed_count,
            failed_count=result.failed_count,
            overall_score=result.overall,
            pass_rate=result.pass_rate,
            aggregate=dict(result.aggregate),
            results=[row.to_dict() for row in result.rows],
            status=status,
            error_message=error_message,
            created_by=actor,
            project_id=project_id,
            visibility="project" if project_id else "user",
            completed_at=now,
        )
        session.add(run)
        session.flush()
        audit_record(
            session,
            actor=actor,
            action="run_evaluation",
            entity_type="eval_run",
            entity_id=run.run_id,
            details={
                "dataset_id": payload.dataset_id,
                "dataset_version": dataset_version,
                "n_examples": result.n_examples,
                "overall_score": result.overall,
                "status": status,
            },
        )
        session.commit()
        data = EvalRunSchema.model_validate(run)
    return envelope_response(data, status_code=201)


def register(app: Starlette) -> None:
    app.routes.append(Route(LIST_PATH, list_evaluations, methods=["GET"]))
    app.routes.append(Route(LIST_PATH, create_evaluation, methods=["POST"]))
    app.routes.append(Route(DETAIL_PATH, get_evaluation, methods=["GET"]))
