"""Versioned knowledge-base build and RAG endpoints."""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.auth import (
    SCOPE_OPERATOR,
    CaliberIdentity,
    require_scopes,
    require_user,
    resolve_identity,
)
from caliber.db.models import CaliberKnowledgeBase, CaliberKnowledgeBaseVersion
from caliber.db.scoping import get_visible
from caliber.knowledge.embeddings import KnowledgeDependencyError
from caliber.knowledge.schemas import (
    KnowledgeBaseCreateRequest,
    KnowledgeBaselineRequest,
    KnowledgeBaselineResponse,
    KnowledgeBaseUpdateRequest,
    KnowledgeBaseVersionCreateRequest,
    KnowledgeCalibrationRequest,
    KnowledgeCalibrationRunDetail,
    KnowledgeGraphExploreRequest,
    KnowledgeQueryRequest,
)
from caliber.knowledge.service import KnowledgeBaseService
from caliber.resource_access import require_project_access_if_scoped
from caliber.routes._deps import (
    envelope_response,
    get_session_factory,
    parse_json_object,
    visibility_param,
)

PREFIX = "/ajax-api/2.0/mlflow/caliber"
OPTIONS_PATH = PREFIX + "/knowledge-bases/options"
LIST_PATH = PREFIX + "/knowledge-bases"
DETAIL_PATH = PREFIX + "/knowledge-bases/{knowledge_base_id}"
VERSIONS_PATH = PREFIX + "/knowledge-bases/{knowledge_base_id}/versions"
ACTIVATE_PATH = PREFIX + "/knowledge-bases/{knowledge_base_id}/versions/{version_id}/activate"
ROLLBACK_PATH = PREFIX + "/knowledge-bases/{knowledge_base_id}/rollback"
RUNS_PATH = PREFIX + "/knowledge-bases/{knowledge_base_id}/runs"
VERSION_DETAIL_PATH = PREFIX + "/knowledge-base-versions/{version_id}"
VERSION_SOURCES_PATH = PREFIX + "/knowledge-base-versions/{version_id}/sources"
VERSION_CHUNKS_PATH = PREFIX + "/knowledge-base-versions/{version_id}/chunks"
VERSION_ENTITIES_PATH = PREFIX + "/knowledge-base-versions/{version_id}/entities"
VERSION_RELATIONSHIPS_PATH = PREFIX + "/knowledge-base-versions/{version_id}/relationships"
VERSION_GRAPH_PATH = PREFIX + "/knowledge-base-versions/{version_id}/graph"
VERSION_AGE_SYNC_PATH = PREFIX + "/knowledge-base-versions/{version_id}/age-sync"
RUN_EVENTS_PATH = PREFIX + "/knowledge-runs/{run_id}/events"
QUERY_PATH = PREFIX + "/knowledge/query"
# Calibration (Phase K1): durable retrieval-quality runs + baseline.
CALIBRATE_PATH = PREFIX + "/knowledge-bases/{knowledge_base_id}/calibrate"
TEST_RUNS_PATH = PREFIX + "/knowledge-bases/{knowledge_base_id}/test-runs"
BASELINE_PATH = PREFIX + "/knowledge-bases/{knowledge_base_id}/baseline"
# Full calibration-run detail. The literal ``/knowledge/test-runs`` prefix is
# distinct from ``/knowledge/query`` and the ``/knowledge-bases/...`` family, so
# the ``{test_run_id}`` capture here cannot shadow another route — it is still
# registered before any future ``/knowledge/{...}`` capture as a precaution.
TEST_RUN_DETAIL_PATH = PREFIX + "/knowledge/test-runs/{test_run_id}"

_STATUS_VALUES = frozenset({"active", "archived", "all"})


def _require_visible_knowledge_base(
    session: Session, knowledge_base_id: str, identity: CaliberIdentity
) -> CaliberKnowledgeBase:
    """Fetch a KB by id, gated by visibility -- 404s an invisible/missing one.

    `P2-A` child-mutation closure: shared by every child-mutation route below
    (`create_version`/`activate_version`/`rollback_version`/`calibrate_
    knowledge_base`/`set_knowledge_base_baseline`) so each can add its own
    project-role check on the KB's *own* project (not the caller's active
    one) before delegating to the service -- the same shape `update_
    knowledge_base`/`delete_knowledge_base` above already inline individually.
    """
    kb = get_visible(
        session,
        CaliberKnowledgeBase,
        CaliberKnowledgeBase.knowledge_base_id,
        knowledge_base_id,
        identity,
    )
    if kb is None:
        raise HTTPException(
            status_code=404, detail=f"knowledge base {knowledge_base_id!r} not found"
        )
    return kb  # type: ignore[no-any-return]


def _service(request: Request) -> KnowledgeBaseService:
    service: KnowledgeBaseService | None = getattr(
        request.app.state, "knowledge_base_service", None
    )
    injected_client = getattr(request.app.state, "object_store_client", None)
    if service is None:
        service = KnowledgeBaseService(
            config=request.app.state.config,
            session_factory=get_session_factory(request),
            object_store_client=injected_client,
        )
        request.app.state.knowledge_base_service = service
    elif injected_client is not None:
        service._object_store_client = injected_client
    return service


async def get_options(request: Request) -> JSONResponse:
    require_user(request)
    return envelope_response(_service(request).options())


async def list_knowledge_bases(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    status = request.query_params.get("status", "active")
    if status not in _STATUS_VALUES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid value for 'status': {status!r}; expected one of {sorted(_STATUS_VALUES)}"
            ),
        )
    rows = _service(request).list_knowledge_bases(
        identity=identity,
        status=status,
        visibility=visibility_param(request),
    )
    return envelope_response(rows)


async def create_knowledge_base(request: Request) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    body = await parse_json_object(request)
    payload = KnowledgeBaseCreateRequest.model_validate(body)
    # `P2-A` (isolation closure, item 1's "root routes to centralized
    # authorization"): this route previously checked only the global
    # `caliber.operator` scope, with no project-role check at all -- a
    # project `viewer` (visible but not meant to write) could create a
    # knowledge base into any project they merely belong to. Same
    # `resource.write.runtime` action `create_prompt`/`create_workflow`/
    # `register_tool`/`create_skill` already use for this identical "root
    # create" shape. A no-op when no project is active (a personal/global KB).
    with get_session_factory(request)() as session:
        require_project_access_if_scoped(
            session, identity, identity.active_project_id, "resource.write.runtime"
        )
    try:
        result = _service(request).create_knowledge_base(payload, identity=identity, actor=actor)
    except KnowledgeDependencyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return envelope_response(result, status_code=201)


async def get_knowledge_base(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    knowledge_base_id = request.path_params["knowledge_base_id"]
    row = _service(request).get_knowledge_base(knowledge_base_id, identity=identity)
    return envelope_response(row)


async def update_knowledge_base(request: Request) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    knowledge_base_id = request.path_params["knowledge_base_id"]
    body = await parse_json_object(request)
    payload = KnowledgeBaseUpdateRequest.model_validate(body)
    # `P2-A`: same `resource.write.runtime` role floor as `create_knowledge_
    # base` above, gated on the KB's own project (not the caller's active
    # one). The service's own visibility check below still 404s a genuinely
    # invisible/missing id; this only adds the role check for a visible one.
    with get_session_factory(request)() as session:
        kb = get_visible(
            session,
            CaliberKnowledgeBase,
            CaliberKnowledgeBase.knowledge_base_id,
            knowledge_base_id,
            identity,
        )
        if kb is None:
            raise HTTPException(
                status_code=404, detail=f"knowledge base {knowledge_base_id!r} not found"
            )
        require_project_access_if_scoped(session, identity, kb.project_id, "resource.write.runtime")
    row = _service(request).update_knowledge_base(
        knowledge_base_id,
        payload,
        identity=identity,
        actor=actor,
    )
    return envelope_response(row)


async def delete_knowledge_base(request: Request) -> JSONResponse:
    """``DELETE /knowledge-bases/{id}`` — hard-delete a KB and all its rows.

    Operator-scoped, matching the other KB write routes. Unlike the
    ``PATCH status=archived`` soft delete, this fully removes the KB together
    with every version, source, chunk, entity, relationship, run, run event, and
    calibration test run (FK-safe cascade in :meth:`KnowledgeBaseService.delete`),
    plus best-effort cleanup of the versions' object-store outputs and any Apache
    AGE subgraph. Returns the deleted id; 404 if the KB does not exist.
    """
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    knowledge_base_id = request.path_params["knowledge_base_id"]
    # `P2-A`: same `resource.write.runtime` role floor as `create_knowledge_
    # base`/`update_knowledge_base` above.
    with get_session_factory(request)() as session:
        kb = get_visible(
            session,
            CaliberKnowledgeBase,
            CaliberKnowledgeBase.knowledge_base_id,
            knowledge_base_id,
            identity,
        )
        if kb is None:
            raise HTTPException(
                status_code=404, detail=f"knowledge base {knowledge_base_id!r} not found"
            )
        require_project_access_if_scoped(session, identity, kb.project_id, "resource.write.runtime")
    _service(request).delete(knowledge_base_id, identity=identity, actor=actor)
    return JSONResponse({"data": {"knowledge_base_id": knowledge_base_id, "deleted": True}})


async def list_versions(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    knowledge_base_id = request.path_params["knowledge_base_id"]
    rows = _service(request).list_versions(knowledge_base_id, identity=identity)
    return envelope_response(rows)


async def create_version(request: Request) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    knowledge_base_id = request.path_params["knowledge_base_id"]
    body = await parse_json_object(request)
    payload = KnowledgeBaseVersionCreateRequest.model_validate(body)
    # `P2-A` child-mutation closure: this route previously checked only the
    # global `caliber.operator` scope plus the service's own visibility
    # gate (`get_visible`, via `_require_visible_knowledge_base` inside
    # `create_version`) -- no project-role floor, so a project viewer could
    # trigger a real embedding build. Same `resource.write.runtime` action
    # `create_knowledge_base`/`update_knowledge_base` above already use.
    with get_session_factory(request)() as session:
        kb = _require_visible_knowledge_base(session, knowledge_base_id, identity)
        require_project_access_if_scoped(session, identity, kb.project_id, "resource.write.runtime")
    try:
        result = _service(request).create_version(
            knowledge_base_id,
            payload,
            identity=identity,
            actor=actor,
        )
    except KnowledgeDependencyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return envelope_response(result, status_code=201)


async def activate_version(request: Request) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    knowledge_base_id = request.path_params["knowledge_base_id"]
    version_id = request.path_params["version_id"]
    # `P2-A`: same `resource.write.runtime` floor as `create_version` above
    # -- switching the active version is a write to the KB's own row.
    with get_session_factory(request)() as session:
        kb = _require_visible_knowledge_base(session, knowledge_base_id, identity)
        require_project_access_if_scoped(session, identity, kb.project_id, "resource.write.runtime")
    row = _service(request).activate_version(
        knowledge_base_id,
        version_id,
        identity=identity,
        actor=actor,
    )
    return envelope_response(row)


async def rollback_version(request: Request) -> JSONResponse:
    """``POST /knowledge-bases/{id}/rollback`` — re-activate the prior active version."""
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    knowledge_base_id = request.path_params["knowledge_base_id"]
    # `P2-A`: same `resource.write.runtime` floor as `create_version`/
    # `activate_version` above.
    with get_session_factory(request)() as session:
        kb = _require_visible_knowledge_base(session, knowledge_base_id, identity)
        require_project_access_if_scoped(session, identity, kb.project_id, "resource.write.runtime")
    row = _service(request).rollback_version(
        knowledge_base_id,
        identity=identity,
        actor=actor,
    )
    return envelope_response(row)


async def get_version(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    version_id = request.path_params["version_id"]
    row = _service(request).get_version(version_id, identity=identity)
    return envelope_response(row)


async def sync_version_to_age(request: Request) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    version_id = request.path_params["version_id"]
    # `P2-A`: same `resource.write.runtime` floor as the other child-mutation
    # routes above -- syncing a version's chunks/entities into Apache AGE is
    # a write, not a read. Resolved the same way the service's own
    # `_require_visible_version` does (bare version lookup for its owning
    # KB id, then KB visibility) since this route only has `version_id`.
    with get_session_factory(request)() as session:
        version = session.get(CaliberKnowledgeBaseVersion, version_id)
        if version is None:
            raise HTTPException(
                status_code=404, detail=f"knowledge-base version {version_id!r} not found"
            )
        kb = _require_visible_knowledge_base(session, version.knowledge_base_id, identity)
        require_project_access_if_scoped(session, identity, kb.project_id, "resource.write.runtime")
    row = _service(request).sync_version_to_age(
        version_id,
        identity=identity,
        actor=actor,
    )
    return envelope_response(row)


async def list_sources(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    version_id = request.path_params["version_id"]
    rows = _service(request).list_sources(version_id, identity=identity)
    return envelope_response(rows)


async def list_chunks(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    version_id = request.path_params["version_id"]
    query = request.query_params.get("q", "")
    source_key = request.query_params.get("source_key")
    raw_limit = request.query_params.get("limit", "200")
    try:
        limit = int(raw_limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="'limit' must be an integer") from exc
    rows = _service(request).list_chunks(
        version_id,
        identity=identity,
        query=query,
        source_key=source_key,
        limit=limit,
    )
    return envelope_response(rows)


async def list_entities(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    version_id = request.path_params["version_id"]
    rows = _service(request).list_entities(version_id, identity=identity)
    return envelope_response(rows)


async def list_relationships(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    version_id = request.path_params["version_id"]
    rows = _service(request).list_relationships(version_id, identity=identity)
    return envelope_response(rows)


async def explore_graph(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    version_id = request.path_params["version_id"]
    payload = KnowledgeGraphExploreRequest.model_validate(dict(request.query_params))
    result = _service(request).explore_graph(
        version_id,
        identity=identity,
        query=payload.q,
        source=payload.source,
        entity_type=payload.entity_type,
        minimum_relationship_weight=payload.minimum_relationship_weight,
        traversal_hops=payload.traversal_hops,
        age_seed_mode=payload.age_seed_mode,
        strict_age_retrieval=payload.strict_age_retrieval,
        node_limit=payload.node_limit,
    )
    return envelope_response(result)


async def list_runs(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    knowledge_base_id = request.path_params["knowledge_base_id"]
    rows = _service(request).list_runs(knowledge_base_id, identity=identity)
    return envelope_response(rows)


async def list_run_events(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    run_id = request.path_params["run_id"]
    rows = _service(request).list_run_events(run_id, identity=identity)
    return envelope_response(rows)


async def query_knowledge(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    body = await parse_json_object(request)
    payload = KnowledgeQueryRequest.model_validate(body)
    try:
        result = _service(request).query(payload, identity=identity)
    except KnowledgeDependencyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return envelope_response(result)


async def calibrate_knowledge_base(request: Request) -> JSONResponse:
    """``POST /knowledge-bases/{id}/calibrate`` — score a version, persist a run.

    Operator-scoped. Synchronous for now — the retrieve+judge loop runs inline
    (it can move to the build worker later if datasets grow). Returns the durable
    run summary (aggregate ``metrics``, no heavy per-question ``results``).
    """
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    knowledge_base_id = request.path_params["knowledge_base_id"]
    body = await parse_json_object(request)
    payload = KnowledgeCalibrationRequest.model_validate(body)
    # `P2-A`: `resource.execute`, not `.runtime` -- calibration synchronously
    # runs a real retrieve+judge loop (LLM judge calls, a genuine execution
    # side effect), the same "run a job against this resource" shape
    # `workflow_runs.py`/`evaluations.py` already gate with this action,
    # rather than `.runtime`'s narrower "author/edit the resource" ceiling.
    with get_session_factory(request)() as session:
        kb = _require_visible_knowledge_base(session, knowledge_base_id, identity)
        require_project_access_if_scoped(session, identity, kb.project_id, "resource.execute")
    try:
        summary = _service(request).calibrate(
            knowledge_base_id,
            version_id=payload.version_id,
            eval_dataset_id=payload.eval_dataset_id,
            eval_dataset_version=payload.eval_dataset_version,
            retrieval_mode=payload.retrieval_mode,
            top_k=payload.top_k,
            identity=identity,
            actor=actor,
        )
    except KnowledgeDependencyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return envelope_response(summary, status_code=201)


async def list_calibration_runs(request: Request) -> JSONResponse:
    """``GET /knowledge-bases/{id}/test-runs`` — newest-first run summaries."""
    require_user(request)
    identity = resolve_identity(request)
    knowledge_base_id = request.path_params["knowledge_base_id"]
    raw_limit = request.query_params.get("limit", "20")
    try:
        limit = int(raw_limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="'limit' must be an integer") from exc
    if limit < 1:
        raise HTTPException(status_code=400, detail="'limit' must be >= 1")
    rows = _service(request).list_calibration_runs(
        knowledge_base_id, identity=identity, limit=limit
    )
    return envelope_response(rows)


async def get_calibration_run(request: Request) -> JSONResponse:
    """``GET /knowledge/test-runs/{test_run_id}`` — full run incl. ``results``."""
    require_user(request)
    identity = resolve_identity(request)
    test_run_id = request.path_params["test_run_id"]
    run = _service(request).get_calibration_run(test_run_id, identity=identity)
    return envelope_response(KnowledgeCalibrationRunDetail.model_validate(run))


async def set_knowledge_base_baseline(request: Request) -> JSONResponse:
    """``POST /knowledge-bases/{id}/baseline`` — pin a run as the KB baseline."""
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    knowledge_base_id = request.path_params["knowledge_base_id"]
    body = await parse_json_object(request)
    payload = KnowledgeBaselineRequest.model_validate(body)
    # `P2-A`: same `resource.write.runtime` floor as the other child-mutation
    # routes above -- pinning a baseline mutates the KB's own
    # `baseline_run_id` pointer.
    with get_session_factory(request)() as session:
        kb = _require_visible_knowledge_base(session, knowledge_base_id, identity)
        require_project_access_if_scoped(session, identity, kb.project_id, "resource.write.runtime")
    knowledge_base = _service(request).set_baseline(
        knowledge_base_id,
        test_run_id=payload.test_run_id,
        identity=identity,
        actor=actor,
    )
    return envelope_response(
        KnowledgeBaselineResponse(
            knowledge_base_id=knowledge_base.knowledge_base_id,
            baseline_run_id=knowledge_base.baseline_run_id,
        )
    )


def register(app: Starlette) -> None:
    app.routes.extend(
        [
            Route(OPTIONS_PATH, get_options, methods=["GET"]),
            Route(LIST_PATH, list_knowledge_bases, methods=["GET"]),
            Route(LIST_PATH, create_knowledge_base, methods=["POST"]),
            Route(DETAIL_PATH, get_knowledge_base, methods=["GET"]),
            Route(DETAIL_PATH, update_knowledge_base, methods=["PATCH"]),
            Route(DETAIL_PATH, delete_knowledge_base, methods=["DELETE"]),
            Route(VERSIONS_PATH, list_versions, methods=["GET"]),
            Route(VERSIONS_PATH, create_version, methods=["POST"]),
            Route(ACTIVATE_PATH, activate_version, methods=["POST"]),
            Route(ROLLBACK_PATH, rollback_version, methods=["POST"]),
            Route(RUNS_PATH, list_runs, methods=["GET"]),
            Route(VERSION_DETAIL_PATH, get_version, methods=["GET"]),
            Route(VERSION_AGE_SYNC_PATH, sync_version_to_age, methods=["POST"]),
            Route(VERSION_SOURCES_PATH, list_sources, methods=["GET"]),
            Route(VERSION_CHUNKS_PATH, list_chunks, methods=["GET"]),
            Route(VERSION_ENTITIES_PATH, list_entities, methods=["GET"]),
            Route(VERSION_RELATIONSHIPS_PATH, list_relationships, methods=["GET"]),
            Route(VERSION_GRAPH_PATH, explore_graph, methods=["GET"]),
            Route(RUN_EVENTS_PATH, list_run_events, methods=["GET"]),
            # Calibration: register the literal ``/knowledge/test-runs/{id}``
            # detail before any ``/knowledge/{...}`` capture could shadow it, and
            # the KB-scoped calibrate/test-runs/baseline sub-routes alongside the
            # other ``/knowledge-bases/{id}/...`` family.
            Route(TEST_RUN_DETAIL_PATH, get_calibration_run, methods=["GET"]),
            Route(CALIBRATE_PATH, calibrate_knowledge_base, methods=["POST"]),
            Route(TEST_RUNS_PATH, list_calibration_runs, methods=["GET"]),
            Route(BASELINE_PATH, set_knowledge_base_baseline, methods=["POST"]),
            Route(QUERY_PATH, query_knowledge, methods=["POST"]),
        ]
    )
