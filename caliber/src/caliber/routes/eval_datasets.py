"""``/caliber/eval-datasets`` endpoints.

A dataset is a versioned set of input/expected pairs the refinement
pipeline scores candidate prompts against. Examples are append-only so
historical job runs stay auditable — to replace an example, post a
fresh one and supersede the old.

Surface:

* ``GET /eval-datasets`` — list (filterable by status + tag).
* ``GET /eval-datasets/{dataset_id}`` — single dataset.
* ``POST /eval-datasets`` (operator) — create.
* ``PATCH /eval-datasets/{dataset_id}`` (admin) — update metadata,
  archive.
* ``GET /eval-datasets/{dataset_id}/examples`` — list examples
  (optionally pinned to a dataset_version).
* ``POST /eval-datasets/{dataset_id}/examples`` (operator) — append
  an example; bumps dataset.version.
* ``POST /eval-datasets/{dataset_id}/examples/{example_id}/supersede``
  (admin) — mark an example as retired without deleting.
* ``POST /eval-datasets/{dataset_id}/examples/{example_id}/revise``
  (operator) — edit a row: supersede the old + append a replacement
  atomically (one version bump), for the curation UI's row editor.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
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
from caliber.db.models import CaliberEvalDataset, CaliberEvalDatasetExample
from caliber.db.scoping import apply_visibility_filter, get_visible
from caliber.eval.dataset_sync import (
    DatasetRecord,
    DatasetSyncClient,
    MLflowDatasetSyncClient,
)
from caliber.ids import new_eval_dataset_id, new_eval_example_id
from caliber.routes._deps import (
    envelope_response,
    get_session_factory,
    list_limit,
    parse_json_object,
    visibility_param,
)
from caliber.schemas import (
    EvalDatasetCreateRequest,
    EvalDatasetSchema,
    EvalDatasetUpdateRequest,
    EvalExampleCreateRequest,
    EvalExampleFromTraceRequest,
    EvalExampleReviseRequest,
    EvalExampleSchema,
)
from caliber.trace_client import fetch_trace_detail

logger = logging.getLogger(__name__)

LIST_PATH = "/ajax-api/2.0/mlflow/caliber/eval-datasets"
DETAIL_PATH = "/ajax-api/2.0/mlflow/caliber/eval-datasets/{dataset_id}"
EXAMPLES_PATH = "/ajax-api/2.0/mlflow/caliber/eval-datasets/{dataset_id}/examples"
FROM_TRACE_PATH = "/ajax-api/2.0/mlflow/caliber/eval-datasets/{dataset_id}/examples/from-trace"
SUPERSEDE_PATH = (
    "/ajax-api/2.0/mlflow/caliber/eval-datasets/{dataset_id}/examples/{example_id}/supersede"
)
REVISE_PATH = "/ajax-api/2.0/mlflow/caliber/eval-datasets/{dataset_id}/examples/{example_id}/revise"
SYNC_PATH = "/ajax-api/2.0/mlflow/caliber/eval-datasets/{dataset_id}/sync"

# See :data:`caliber.routes.skills._LIST_STATUS_VALUES` — same allowlist
# applies (deep-review consistency note #1).
_LIST_STATUS_VALUES: frozenset[str] = frozenset({"active", "archived", "all"})


async def list_datasets(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    factory = get_session_factory(request)
    requested_status = request.query_params.get("status", "active")
    if requested_status not in _LIST_STATUS_VALUES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid value for 'status': {requested_status!r}; "
                f"expected one of {sorted(_LIST_STATUS_VALUES)}"
            ),
        )
    tag_filter = request.query_params.get("tag")
    limit, offset = list_limit(request)
    with factory() as session:
        stmt = select(CaliberEvalDataset).order_by(CaliberEvalDataset.name)
        if requested_status != "all":
            stmt = stmt.where(CaliberEvalDataset.status == requested_status)
        stmt = apply_visibility_filter(
            stmt,
            CaliberEvalDataset,
            identity,
            identity.active_project_id,
            only=visibility_param(request),
        )
        rows = session.execute(stmt.limit(limit).offset(offset)).scalars().all()
    items = [EvalDatasetSchema.model_validate(row) for row in rows]
    if tag_filter:
        items = [item for item in items if tag_filter in item.tags]
    return envelope_response(items)


async def get_dataset(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    dataset_id = request.path_params["dataset_id"]
    factory = get_session_factory(request)
    with factory() as session:
        row = get_visible(
            session, CaliberEvalDataset, CaliberEvalDataset.dataset_id, dataset_id, identity
        )
    if row is None:
        raise HTTPException(status_code=404, detail=f"eval dataset {dataset_id!r} not found")
    return envelope_response(EvalDatasetSchema.model_validate(row))


def create_eval_dataset_record(
    session: Any,
    *,
    payload: EvalDatasetCreateRequest,
    actor: str,
    project_id: str | None,
) -> CaliberEvalDataset:
    """Create a test-set row (single definition reused by the route + Aria).

    Raises :class:`ValueError` on a duplicate name; flushes but does not commit.
    The owner is always the acting actor (``payload.owner`` is ignored).
    """
    existing = (
        session.execute(select(CaliberEvalDataset).where(CaliberEvalDataset.name == payload.name))
        .scalars()
        .first()
    )
    if existing is not None:
        raise ValueError(
            f"eval-dataset name {payload.name!r} is already in use by {existing.dataset_id!r}"
        )
    dataset = CaliberEvalDataset(
        dataset_id=new_eval_dataset_id(),
        name=payload.name,
        description=payload.description,
        owner=actor,
        project_id=project_id,
        visibility="project" if project_id else "user",
        tags=list(payload.tags),
        status="active",
        version=1,
    )
    session.add(dataset)
    session.flush()
    audit_record(
        session,
        actor=actor,
        action="create_eval_dataset",
        entity_type="eval_dataset",
        entity_id=dataset.dataset_id,
        details={"name": dataset.name, "owner": dataset.owner},
    )
    return dataset


async def create_dataset(request: Request) -> JSONResponse:
    body = await parse_json_object(request)
    payload = EvalDatasetCreateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)

    factory = get_session_factory(request)
    with factory() as session:
        try:
            dataset = create_eval_dataset_record(
                session, payload=payload, actor=actor, project_id=identity.active_project_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        session.commit()
        data = EvalDatasetSchema.model_validate(dataset)
    return envelope_response(data, status_code=201)


_UPDATABLE_FIELDS = ("description", "owner", "tags", "status")


async def update_dataset(request: Request) -> JSONResponse:
    dataset_id = request.path_params["dataset_id"]
    body = await parse_json_object(request)
    payload = EvalDatasetUpdateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_ADMIN])

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="request body must include at least one field")

    factory = get_session_factory(request)
    with factory() as session:
        dataset = session.get(CaliberEvalDataset, dataset_id)
        if dataset is None:
            raise HTTPException(status_code=404, detail=f"eval dataset {dataset_id!r} not found")

        diff: dict[str, dict[str, object]] = {}
        for field in _UPDATABLE_FIELDS:
            if field not in changes:
                continue
            new_value = changes[field]
            old_value = getattr(dataset, field)
            if new_value != old_value:
                diff[field] = {"from": old_value, "to": new_value}
                setattr(dataset, field, new_value)

        if not diff:
            return envelope_response(EvalDatasetSchema.model_validate(dataset))

        audit_record(
            session,
            actor=actor,
            action="update_eval_dataset",
            entity_type="eval_dataset",
            entity_id=dataset.dataset_id,
            details={"changes": diff},
        )
        session.commit()
        data = EvalDatasetSchema.model_validate(dataset)
    return envelope_response(data)


async def list_examples(request: Request) -> JSONResponse:
    """List examples in a dataset.

    Two query-string knobs:

    * ``version=<N>`` returns only examples whose ``dataset_version``
      matches — the "what did version N actually contain?" view.
    * ``include_superseded=true`` opts into showing retired examples
      (default hides them; the operator usually wants the current set).
    """
    require_user(request)
    dataset_id = request.path_params["dataset_id"]
    factory = get_session_factory(request)
    version_filter = request.query_params.get("version")
    include_superseded = request.query_params.get("include_superseded") == "true"
    limit, offset = list_limit(request)
    with factory() as session:
        if session.get(CaliberEvalDataset, dataset_id) is None:
            raise HTTPException(status_code=404, detail=f"eval dataset {dataset_id!r} not found")
        stmt = (
            select(CaliberEvalDatasetExample)
            .where(CaliberEvalDatasetExample.dataset_id == dataset_id)
            .order_by(CaliberEvalDatasetExample.created_at)
        )
        if version_filter is not None:
            try:
                parsed_version = int(version_filter)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"version must be an integer, got {version_filter!r}",
                ) from exc
            # Cap the parsed integer to a sane range. ``int()`` accepts
            # arbitrarily large values; without this guard a request
            # like ``?version=99999999999999999999`` runs the SQL
            # comparison and silently returns no rows. Versions
            # monotonically increase from 1 and the DB column is a
            # 32-bit int, so 2**31 is the natural ceiling.
            if parsed_version < 1 or parsed_version >= 2**31:
                raise HTTPException(
                    status_code=400,
                    detail=(f"version must be between 1 and {2**31 - 1}, got {parsed_version}"),
                )
            stmt = stmt.where(CaliberEvalDatasetExample.dataset_version == parsed_version)
        if not include_superseded:
            stmt = stmt.where(CaliberEvalDatasetExample.superseded_at.is_(None))
        rows = session.execute(stmt.limit(limit).offset(offset)).scalars().all()
    items = [EvalExampleSchema.model_validate(row) for row in rows]
    return envelope_response(items)


async def create_example(request: Request) -> JSONResponse:
    """Append an example to a dataset.

    Bumps ``dataset.version`` so the job-eval pin can detect "this
    job was scored against a now-stale snapshot." The new example's
    ``dataset_version`` is set to the bumped value.
    """
    dataset_id = request.path_params["dataset_id"]
    body = await parse_json_object(request)
    payload = EvalExampleCreateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])

    factory = get_session_factory(request)
    with factory() as session:
        dataset = session.get(CaliberEvalDataset, dataset_id)
        if dataset is None:
            raise HTTPException(status_code=404, detail=f"eval dataset {dataset_id!r} not found")
        dataset.version = dataset.version + 1
        example = CaliberEvalDatasetExample(
            example_id=new_eval_example_id(),
            dataset_id=dataset.dataset_id,
            dataset_version=dataset.version,
            input=dict(payload.input),
            expected=dict(payload.expected),
            weight=payload.weight,
            tags=list(payload.tags),
        )
        session.add(example)
        session.flush()
        audit_record(
            session,
            actor=actor,
            action="append_eval_example",
            entity_type="eval_dataset",
            entity_id=dataset.dataset_id,
            details={
                "example_id": example.example_id,
                "new_version": dataset.version,
            },
        )
        session.commit()
        data = EvalExampleSchema.model_validate(example)
    return envelope_response(data, status_code=201)


def _trace_field_text(value: Any) -> str:
    """Coerce a trace request/response payload to a single string.

    The trace detail's ``request``/``response`` may be a string or a JSON-able
    object (sanitized dict/list). Strings pass through; everything else is
    rendered as stable JSON so no information is dropped from the captured pair.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


async def create_example_from_trace(request: Request) -> JSONResponse:
    """Append an example built from an observed MLflow trace.

    The trace's request becomes the example ``input`` and its response the
    ``expected`` answer — capturing a real interaction as a regression case in
    one click from the trace viewer. Optional ``input``/``expected`` overrides
    let the caller correct the captured pair before saving. Tagged with
    ``from-trace`` + the source trace id for lineage.
    """
    dataset_id = request.path_params["dataset_id"]
    body = await parse_json_object(request)
    payload = EvalExampleFromTraceRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])

    detail = fetch_trace_detail(payload.trace_id)
    derived_input: dict[str, object] = (
        dict(payload.input)
        if payload.input is not None
        else {"input": _trace_field_text(detail.request)}
    )
    derived_expected: dict[str, object] = (
        dict(payload.expected)
        if payload.expected is not None
        else {"expected": _trace_field_text(detail.response)}
    )

    # Reject an empty capture (trace missing / not yet flushed) unless the
    # caller supplied the input explicitly — never persist a blank example.
    if payload.input is None and not _trace_field_text(detail.request).strip():
        raise HTTPException(
            status_code=404,
            detail=(
                f"trace {payload.trace_id!r} not found or has no request to capture; "
                "supply an explicit 'input' to add it anyway"
            ),
        )

    # De-duplicate the lineage + caller tags while preserving order.
    seen: set[str] = set()
    tags: list[str] = []
    for tag in ["from-trace", f"trace:{payload.trace_id}", *payload.tags]:
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)

    factory = get_session_factory(request)
    with factory() as session:
        dataset = session.get(CaliberEvalDataset, dataset_id)
        if dataset is None:
            raise HTTPException(status_code=404, detail=f"eval dataset {dataset_id!r} not found")
        dataset.version = dataset.version + 1
        example = CaliberEvalDatasetExample(
            example_id=new_eval_example_id(),
            dataset_id=dataset.dataset_id,
            dataset_version=dataset.version,
            input=derived_input,
            expected=derived_expected,
            weight=payload.weight,
            tags=tags,
        )
        session.add(example)
        session.flush()
        audit_record(
            session,
            actor=actor,
            action="append_eval_example_from_trace",
            entity_type="eval_dataset",
            entity_id=dataset.dataset_id,
            details={
                "example_id": example.example_id,
                "trace_id": payload.trace_id,
                "new_version": dataset.version,
            },
        )
        session.commit()
        data = EvalExampleSchema.model_validate(example)
    return envelope_response(data, status_code=201)


async def supersede_example(request: Request) -> JSONResponse:
    """Mark an example as retired (does not delete).

    The dataset version bumps so callers know the active set
    changed. The retired row remains in the DB so historical
    runs that scored against it stay reproducible.
    """
    dataset_id = request.path_params["dataset_id"]
    example_id = request.path_params["example_id"]
    actor = require_scopes(request, [SCOPE_ADMIN])

    factory = get_session_factory(request)
    with factory() as session:
        dataset = session.get(CaliberEvalDataset, dataset_id)
        if dataset is None:
            raise HTTPException(status_code=404, detail=f"eval dataset {dataset_id!r} not found")
        example = session.get(CaliberEvalDatasetExample, example_id)
        if example is None or example.dataset_id != dataset_id:
            raise HTTPException(
                status_code=404, detail=f"example {example_id!r} not found in dataset"
            )
        if example.superseded_at is not None:
            # Idempotent — already retired.
            return envelope_response(EvalExampleSchema.model_validate(example))

        dataset.version = dataset.version + 1
        example.superseded_at = datetime.now(timezone.utc)
        # Record the version at which this example retired so pinned runs can
        # reconstruct the active set as of any historical version N.
        example.superseded_version = dataset.version
        audit_record(
            session,
            actor=actor,
            action="supersede_eval_example",
            entity_type="eval_dataset",
            entity_id=dataset.dataset_id,
            details={"example_id": example_id, "new_version": dataset.version},
        )
        session.commit()
        data = EvalExampleSchema.model_validate(example)
    return envelope_response(data)


async def revise_example(request: Request) -> JSONResponse:
    """Edit a row: supersede the old example and append a replacement atomically.

    Honours the append-only model — the original row is retired (not deleted, so
    historical runs stay reproducible) and a new row carrying the edited content
    is appended. The dataset version bumps exactly once and a single audit entry
    links old→new. Returns the new (replacement) example with 201.
    """
    dataset_id = request.path_params["dataset_id"]
    example_id = request.path_params["example_id"]
    body = await parse_json_object(request)
    payload = EvalExampleReviseRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])

    factory = get_session_factory(request)
    with factory() as session:
        dataset = session.get(CaliberEvalDataset, dataset_id)
        if dataset is None:
            raise HTTPException(status_code=404, detail=f"eval dataset {dataset_id!r} not found")
        old = session.get(CaliberEvalDatasetExample, example_id)
        if old is None or old.dataset_id != dataset_id:
            raise HTTPException(
                status_code=404, detail=f"example {example_id!r} not found in dataset"
            )
        if old.superseded_at is not None:
            raise HTTPException(
                status_code=409,
                detail=f"example {example_id!r} is already superseded; revise the current row",
            )

        dataset.version = dataset.version + 1
        old.superseded_at = datetime.now(timezone.utc)
        old.superseded_version = dataset.version
        replacement = CaliberEvalDatasetExample(
            example_id=new_eval_example_id(),
            dataset_id=dataset.dataset_id,
            dataset_version=dataset.version,
            input=dict(payload.input),
            expected=dict(payload.expected),
            weight=payload.weight,
            tags=list(payload.tags),
        )
        session.add(replacement)
        session.flush()
        audit_record(
            session,
            actor=actor,
            action="revise_eval_example",
            entity_type="eval_dataset",
            entity_id=dataset.dataset_id,
            details={
                "superseded_example_id": example_id,
                "replacement_example_id": replacement.example_id,
                "new_version": dataset.version,
            },
        )
        session.commit()
        data = EvalExampleSchema.model_validate(replacement)
    return envelope_response(data, status_code=201)


def _resolve_sync_client(request: Request) -> DatasetSyncClient:
    """Return the MLflow dataset-sync client.

    Honours an ``app.state.dataset_sync_client`` override (tests inject a fake);
    otherwise builds the real ``mlflow.genai.datasets``-backed client.
    """
    override: DatasetSyncClient | None = getattr(request.app.state, "dataset_sync_client", None)
    if override is not None:
        return override
    return MLflowDatasetSyncClient()


def _resolve_experiment_id(request: Request) -> str | None:
    """Resolve a single MLflow experiment id from ``CALIBER_TRACING_EXPERIMENT``.

    Numeric values are used as-is; a name is resolved via MLflow; anything
    unresolved returns ``None`` so MLflow associates the dataset with the
    inferred/default experiment.
    """
    config = getattr(request.app.state, "config", None)
    configured = str(getattr(config, "tracing_experiment", "") or "").strip()
    if not configured:
        return None
    if configured.isdigit():
        return configured
    try:
        import mlflow  # noqa: PLC0415

        getter = getattr(mlflow, "get_experiment_by_name", None)
        experiment = getter(configured) if callable(getter) else None
        experiment_id = getattr(experiment, "experiment_id", None) if experiment else None
        return str(experiment_id) if experiment_id is not None else None
    except Exception:  # pragma: no cover - mlflow unavailable / lookup failed
        return None


async def sync_dataset_to_mlflow(request: Request) -> JSONResponse:
    """Push the dataset's current example set to MLflow's GenAI dataset registry.

    CALIBER stays the source of truth; this records the linkage so MLflow 3.14's
    native dataset UI + source-trace lineage become available. Re-syncs are
    idempotent (``merge_records`` upserts by input hash). MLflow failures degrade
    to a clean 502 rather than corrupting CALIBER state.
    """
    dataset_id = request.path_params["dataset_id"]
    actor = require_scopes(request, [SCOPE_OPERATOR])

    factory = get_session_factory(request)
    with factory() as session:
        dataset = session.get(CaliberEvalDataset, dataset_id)
        if dataset is None:
            raise HTTPException(status_code=404, detail=f"eval dataset {dataset_id!r} not found")

        rows = (
            session.execute(
                select(CaliberEvalDatasetExample)
                .where(CaliberEvalDatasetExample.dataset_id == dataset_id)
                .where(CaliberEvalDatasetExample.superseded_at.is_(None))
                .order_by(CaliberEvalDatasetExample.created_at)
            )
            .scalars()
            .all()
        )
        records = [
            DatasetRecord(inputs=dict(row.input), expectations=dict(row.expected)) for row in rows
        ]
        synced_version = dataset.version
        dataset_name = dataset.name

    client = _resolve_sync_client(request)
    experiment_id = _resolve_experiment_id(request)
    tags = {"caliber.dataset_id": dataset_id, "caliber.version": str(synced_version)}
    try:
        result = client.sync_dataset(
            name=dataset_name,
            records=records,
            experiment_id=experiment_id,
            tags=tags,
        )
    except Exception as exc:  # pragma: no cover - exercised via fake raising
        logger.warning("eval-dataset sync to MLflow failed (%s)", exc)
        raise HTTPException(
            status_code=502,
            detail=f"MLflow dataset sync failed: {exc}",
        ) from exc

    with factory() as session:
        dataset = session.get(CaliberEvalDataset, dataset_id)
        if dataset is None:  # pragma: no cover - deleted mid-sync
            raise HTTPException(status_code=404, detail=f"eval dataset {dataset_id!r} not found")
        dataset.mlflow_dataset_id = result.mlflow_dataset_id
        dataset.mlflow_synced_at = result.synced_at
        dataset.mlflow_synced_version = synced_version
        dataset.mlflow_record_count = result.record_count
        dataset.mlflow_digest = result.digest
        audit_record(
            session,
            actor=actor,
            action="sync_eval_dataset_mlflow",
            entity_type="eval_dataset",
            entity_id=dataset_id,
            details={
                "mlflow_dataset_id": result.mlflow_dataset_id,
                "record_count": result.record_count,
                "synced_version": synced_version,
            },
        )
        session.commit()
        data = EvalDatasetSchema.model_validate(dataset)
    return envelope_response(data)


def register(app: Starlette) -> None:
    app.routes.append(Route(LIST_PATH, list_datasets, methods=["GET"]))
    app.routes.append(Route(LIST_PATH, create_dataset, methods=["POST"]))
    app.routes.append(Route(DETAIL_PATH, get_dataset, methods=["GET"]))
    app.routes.append(Route(DETAIL_PATH, update_dataset, methods=["PATCH"]))
    app.routes.append(Route(EXAMPLES_PATH, list_examples, methods=["GET"]))
    app.routes.append(Route(EXAMPLES_PATH, create_example, methods=["POST"]))
    app.routes.append(Route(FROM_TRACE_PATH, create_example_from_trace, methods=["POST"]))
    app.routes.append(Route(SUPERSEDE_PATH, supersede_example, methods=["POST"]))
    app.routes.append(Route(REVISE_PATH, revise_example, methods=["POST"]))
    app.routes.append(Route(SYNC_PATH, sync_dataset_to_mlflow, methods=["POST"]))
