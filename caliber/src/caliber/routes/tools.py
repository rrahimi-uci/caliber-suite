"""``/caliber/tools`` endpoints — the tool registry (plan §15.4, §18.1).

Only admins register/update/archive tools; designers select registered tools in
the editor. Archiving is blocked while a ``prod``-deployed workflow references
the tool family (plan §14.4, §19.12). Secret values are never accepted or
returned — only ``secret_refs`` (names).
"""

from __future__ import annotations

import contextlib
import importlib
import inspect
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import String, cast, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
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
from caliber.calibration import aggregate, evaluate_assertion
from caliber.db.models import (
    CaliberToolRegistry,
    CaliberToolTestRun,
    CaliberWorkflowDeployment,
    CaliberWorkflowVersion,
)
from caliber.db.scoping import apply_visibility_filter
from caliber.ids import new_tool_id, new_tool_test_run_id
from caliber.routes._deps import (
    envelope_response,
    get_session_factory,
    parse_json_object,
    visibility_param,
)
from caliber.schemas import (
    ToolBaselineRequest,
    ToolCalibrationResponse,
    ToolRegisterRequest,
    ToolSchema,
    ToolTestCasesResponse,
    ToolTestCasesUpdateRequest,
    ToolTestRunCreateRequest,
    ToolTestRunDetail,
    ToolTestRunSummary,
    ToolUpdateRequest,
    ToolWorkspaceLastRun,
    ToolWorkspaceResponse,
)
from caliber.workflows.builtin_tools import register_builtin_tools
from caliber.workflows.ir import IRToolBinding
from caliber.workflows.sandbox import make_preview_callable, should_mock_in_preview
from caliber.workflows.tools import family_name

LIST_PATH = "/ajax-api/2.0/mlflow/caliber/tools"
# Durable tool-test-run history. Registered BEFORE ``DETAIL_PATH`` so the
# literal ``/tools/test-runs`` segment isn't captured as a ``{tool_id}``.
TEST_RUNS_PATH = "/ajax-api/2.0/mlflow/caliber/tools/test-runs"
TEST_RUN_DETAIL_PATH = "/ajax-api/2.0/mlflow/caliber/tools/test-runs/{test_run_id}"
DETAIL_PATH = "/ajax-api/2.0/mlflow/caliber/tools/{tool_id}"
ARCHIVE_PATH = DETAIL_PATH + "/archive"
TEST_RUN_PATH = DETAIL_PATH + "/test-run"
TEST_CASES_PATH = DETAIL_PATH + "/test-cases"
CALIBRATE_PATH = DETAIL_PATH + "/calibrate"
SOURCE_PATH = DETAIL_PATH + "/source"
WORKSPACE_PATH = DETAIL_PATH + "/workspace"
BASELINE_PATH = DETAIL_PATH + "/baseline"

_LIST_STATUS_VALUES = frozenset({"active", "deprecated", "archived", "all"})

# History listing defaults/cap for ``GET /tools/test-runs``.
_TEST_RUNS_DEFAULT_LIMIT = 20
_TEST_RUNS_MAX_LIMIT = 100
_UPDATABLE_FIELDS = (
    "description",
    "side_effect_level",
    "requires_approval",
    "allow_in_preview",
    "owner",
    "status",
    "successor_tool_id",
)


async def list_tools(request: Request) -> JSONResponse:
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
        try:
            created = register_builtin_tools(session)
            if created:
                session.commit()
        except IntegrityError:
            # Another first-load request may have inserted the same built-ins.
            session.rollback()
        stmt = select(CaliberToolRegistry).order_by(
            CaliberToolRegistry.name, CaliberToolRegistry.version
        )
        if requested_status != "all":
            stmt = stmt.where(CaliberToolRegistry.status == requested_status)
        stmt = apply_visibility_filter(
            stmt,
            CaliberToolRegistry,
            identity,
            identity.active_project_id,
            only=visibility_param(request),
        )
        rows = session.execute(stmt).scalars().all()
        items = [ToolSchema.model_validate(r) for r in rows]
    return envelope_response(items)


async def get_tool(request: Request) -> JSONResponse:
    require_user(request)
    tool_id = request.path_params["tool_id"]
    factory = get_session_factory(request)
    with factory() as session:
        row = session.get(CaliberToolRegistry, tool_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"tool {tool_id!r} not found")
        data = ToolSchema.model_validate(row)
    return envelope_response(data)


def _resolve_tool_source(module_path: str, callable_name: str) -> dict[str, Any]:
    """Best-effort source/signature/doc for a registered tool's callable.

    Imports the tool's module (operator-registered, first-party) and reflects on
    the callable. Returns ``available=False`` with an ``error`` when the module
    can't be imported or the source isn't on disk (e.g. a C builtin).
    """
    result: dict[str, Any] = {
        "module_path": module_path,
        "callable_name": callable_name,
        "available": False,
        "signature": callable_name,
        "doc": "",
        "source": "",
        "error": None,
    }
    try:
        module = importlib.import_module(module_path)
        fn = getattr(module, callable_name)
    except (ImportError, AttributeError) as exc:
        result["error"] = f"could not import {module_path}.{callable_name}: {exc}"
        return result
    with contextlib.suppress(TypeError, ValueError):
        result["signature"] = f"{callable_name}{inspect.signature(fn)}"
    result["doc"] = inspect.getdoc(fn) or ""
    try:
        result["source"] = inspect.getsource(fn)
        result["available"] = True
    except (OSError, TypeError) as exc:
        result["error"] = f"source unavailable: {exc}"
    return result


async def get_tool_source(request: Request) -> JSONResponse:
    """Return the tool callable's real source + signature + docstring."""
    require_user(request)
    tool_id = request.path_params["tool_id"]
    factory = get_session_factory(request)
    with factory() as session:
        row = session.get(CaliberToolRegistry, tool_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"tool {tool_id!r} not found")
        module_path = row.module_path
        callable_name = row.callable_name
    return JSONResponse({"data": _resolve_tool_source(module_path, callable_name)})


async def register_tool(request: Request) -> JSONResponse:
    body = await parse_json_object(request)
    payload = ToolRegisterRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_ADMIN])
    identity = resolve_identity(request)
    factory = get_session_factory(request)
    with factory() as session:
        existing = (
            session.execute(
                select(CaliberToolRegistry).where(
                    CaliberToolRegistry.name == payload.name,
                    CaliberToolRegistry.version == payload.version,
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"tool {payload.name!r} version {payload.version!r} already registered",
            )
        tool = CaliberToolRegistry(
            tool_id=new_tool_id(),
            name=payload.name,
            version=payload.version,
            description=payload.description,
            module_path=payload.module_path,
            callable_name=payload.callable_name,
            input_schema=payload.input_schema,
            output_schema=payload.output_schema,
            side_effect_level=payload.side_effect_level,
            requires_approval=payload.requires_approval,
            allow_in_preview=payload.allow_in_preview,
            secret_refs=list(payload.secret_refs),
            # Owner is the authenticated actor (payload.owner is ignored).
            owner=actor,
            project_id=identity.active_project_id,
            visibility="project" if identity.active_project_id else "user",
            status="active",
        )
        session.add(tool)
        session.flush()
        audit_record(
            session,
            actor=actor,
            action="register_tool",
            entity_type="tool",
            entity_id=tool.tool_id,
            details={
                "name": tool.name,
                "version": tool.version,
                "side_effect_level": tool.side_effect_level,
            },
        )
        session.commit()
        data = ToolSchema.model_validate(tool)
    return envelope_response(data, status_code=201)


async def update_tool(request: Request) -> JSONResponse:
    tool_id = request.path_params["tool_id"]
    body = await parse_json_object(request)
    payload = ToolUpdateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_ADMIN])
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="request body must include at least one field")

    factory = get_session_factory(request)
    with factory() as session:
        tool = session.get(CaliberToolRegistry, tool_id)
        if tool is None:
            raise HTTPException(status_code=404, detail=f"tool {tool_id!r} not found")
        diff: dict[str, dict[str, object]] = {}
        for field in _UPDATABLE_FIELDS:
            if field not in changes:
                continue
            new_value = changes[field]
            old_value = getattr(tool, field)
            if new_value != old_value:
                diff[field] = {"from": old_value, "to": new_value}
                setattr(tool, field, new_value)
        if not diff:
            return envelope_response(ToolSchema.model_validate(tool))
        if diff.get("status", {}).get("to") == "deprecated" and tool.deprecated_at is None:
            tool.deprecated_at = datetime.now(timezone.utc)
        audit_record(
            session,
            actor=actor,
            action="update_tool",
            entity_type="tool",
            entity_id=tool.tool_id,
            details={"changes": diff},
        )
        session.commit()
        data = ToolSchema.model_validate(tool)
    return envelope_response(data)


async def archive_tool(request: Request) -> JSONResponse:
    tool_id = request.path_params["tool_id"]
    actor = require_scopes(request, [SCOPE_ADMIN])
    factory = get_session_factory(request)
    with factory() as session:
        tool = session.get(CaliberToolRegistry, tool_id)
        if tool is None:
            raise HTTPException(status_code=404, detail=f"tool {tool_id!r} not found")
        blocking = _referencing_deployments(session, tool.name)
        if blocking:
            raise HTTPException(
                status_code=409,
                detail=f"tool {tool.name!r} is referenced by deployed workflow(s) {blocking}; "
                "undeploy them before archiving",
            )
        tool.status = "archived"
        audit_record(
            session,
            actor=actor,
            action="archive_tool",
            entity_type="tool",
            entity_id=tool.tool_id,
            details={"name": tool.name, "version": tool.version},
        )
        session.commit()
        data = ToolSchema.model_validate(tool)
    return envelope_response(data)


def _referencing_deployments(session: Session, tool_name: str) -> list[str]:
    """Return ``{alias}@{workflow_id}`` for every *active* deployment whose
    version references ``tool_name`` (ext E1 — guards all aliases, not just prod)."""
    deployments = (
        session.execute(
            select(CaliberWorkflowDeployment).where(CaliberWorkflowDeployment.status == "active")
        )
        .scalars()
        .all()
    )
    blocking: list[str] = []
    for deployment in deployments:
        version = session.get(CaliberWorkflowVersion, deployment.version_id)
        if version is None:
            continue
        tools = (version.manifest or {}).get("tools", {})
        refs = [b.get("registry_ref", "") for b in tools.values() if isinstance(b, dict)]
        if any(ref and family_name(ref) == tool_name for ref in refs):
            blocking.append(f"{deployment.alias}@{deployment.workflow_id}")
    return blocking


def _invoke_tool_in_sandbox(tool_data: ToolSchema, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Run one tool invocation through the preview sandbox.

    Shared by the one-off ``test-run`` endpoint and the scored ``calibrate``
    endpoint so both go through the identical sandbox path:
    ``write``/``external_action`` tools are always mocked, ``read`` tools run
    live only when ``allow_in_preview`` is True. Returns
    ``{output, mocked, duration_ms, error}``.
    """
    binding = IRToolBinding(
        local_name=tool_data.name,
        registry_ref=f"tool.{tool_data.name}.v{tool_data.version.split('.')[0]}",
        version_constraint="",
        requires_approval=tool_data.requires_approval,
        side_effect_level=tool_data.side_effect_level,
        allow_in_preview=tool_data.allow_in_preview,
        module_path=tool_data.module_path,
        callable_name=tool_data.callable_name,
        secret_refs=tuple(tool_data.secret_refs),
        output_schema=tool_data.output_schema
        if isinstance(tool_data.output_schema, dict)
        else None,
    )

    # Attempt to import the real callable.
    real_callable = None
    import_error: str | None = None
    try:
        mod = importlib.import_module(binding.module_path)
        real_callable = getattr(mod, binding.callable_name, None)
        if real_callable is None:
            import_error = (
                f"module '{binding.module_path}' has no attribute '{binding.callable_name}'"
            )
    except ImportError as exc:
        import_error = f"cannot import module '{binding.module_path}': {exc}"

    mocked = should_mock_in_preview(binding)
    wrapped = make_preview_callable(binding, real_callable)

    start_time = time.monotonic()
    error: str | None = import_error
    output: Any = None

    if error is None:
        try:
            output = wrapped(**tool_input)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

    duration_ms = round((time.monotonic() - start_time) * 1000, 1)
    return {"output": output, "mocked": mocked, "duration_ms": duration_ms, "error": error}


async def test_run_tool(request: Request) -> JSONResponse:
    """``POST /caliber/tools/{tool_id}/test-run`` — sandbox-isolated test invocation.

    Accepts ``{"input": {...}}`` and executes the tool through the sandbox.
    ``write``/``external_action`` tools are always mocked.
    ``read`` tools run live only when ``allow_in_preview`` is True.
    """
    tool_id = request.path_params["tool_id"]
    body = await parse_json_object(request)
    # A non-persisting sandbox preview should not demand a higher privilege than
    # persisting the tool itself (create/update require OPERATOR), so it is
    # OPERATOR-gated rather than ADMIN-gated.
    require_scopes(request, [SCOPE_OPERATOR])

    tool_input: dict[str, Any] = body.get("input", {})
    if not isinstance(tool_input, dict):
        raise HTTPException(status_code=400, detail="'input' must be an object")

    factory = get_session_factory(request)
    with factory() as session:
        tool = session.get(CaliberToolRegistry, tool_id)
        if tool is None:
            raise HTTPException(status_code=404, detail=f"tool {tool_id!r} not found")
        tool_data = ToolSchema.model_validate(tool)

    invocation = _invoke_tool_in_sandbox(tool_data, tool_input)
    return JSONResponse({"data": {"tool_id": tool_id, **invocation}})


async def save_tool_test_cases(request: Request) -> JSONResponse:
    """``PUT /caliber/tools/{tool_id}/test-cases`` — persist calibration cases."""
    tool_id = request.path_params["tool_id"]
    body = await parse_json_object(request)
    payload = ToolTestCasesUpdateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])

    cases = [case.model_dump() for case in payload.test_cases]
    factory = get_session_factory(request)
    with factory() as session:
        tool = session.get(CaliberToolRegistry, tool_id)
        if tool is None:
            raise HTTPException(status_code=404, detail=f"tool {tool_id!r} not found")
        tool.test_cases = cases
        audit_record(
            session,
            actor=actor,
            action="update_tool_test_cases",
            entity_type="tool",
            entity_id=tool.tool_id,
            details={"count": len(cases)},
        )
        session.commit()
    return envelope_response(ToolTestCasesResponse(tool_id=tool_id, test_cases=payload.test_cases))


async def calibrate_tool(request: Request) -> JSONResponse:
    """``POST /caliber/tools/{tool_id}/calibrate`` — score saved test cases.

    Runs every saved test case through the same sandbox invocation
    ``test_run_tool`` uses, scores each against its assertion, and stores the
    aggregate result in ``last_calibration``.
    """
    tool_id = request.path_params["tool_id"]
    actor = require_scopes(request, [SCOPE_OPERATOR])

    factory = get_session_factory(request)
    with factory() as session:
        tool = session.get(CaliberToolRegistry, tool_id)
        if tool is None:
            raise HTTPException(status_code=404, detail=f"tool {tool_id!r} not found")
        saved_cases = list(tool.test_cases or [])
        if not saved_cases:
            raise HTTPException(
                status_code=400,
                detail=f"tool {tool_id!r} has no saved test cases to calibrate against",
            )
        tool_data = ToolSchema.model_validate(tool)

        scored: list[dict[str, Any]] = []
        for case in saved_cases:
            name = str(case.get("name", "")) or "case"
            raw_input = case.get("input")
            case_input: dict[str, Any] = dict(raw_input) if isinstance(raw_input, dict) else {}
            invocation = _invoke_tool_in_sandbox(tool_data, case_input)
            passed = evaluate_assertion(
                case.get("assertion"), invocation["output"], invocation["error"]
            )
            scored.append(
                {
                    "name": name,
                    "passed": passed,
                    "output": invocation["output"],
                    "error": invocation["error"],
                    "duration_ms": invocation["duration_ms"],
                }
            )

        result = aggregate(scored)
        result["ran_at"] = datetime.now(timezone.utc).isoformat()
        tool.last_calibration = result
        audit_record(
            session,
            actor=actor,
            action="calibrate_tool",
            entity_type="tool",
            entity_id=tool.tool_id,
            details={
                "pass_rate": result["pass_rate"],
                "total": result["total"],
                "passed": result["passed"],
            },
        )
        session.commit()

    return envelope_response(ToolCalibrationResponse(tool_id=tool_id, **result))


async def tool_usage(request: Request) -> JSONResponse:
    """Return the workflow versions that reference a tool (plan §16.1 ToolDetail).

    Scans every workflow version's manifest for a tool binding whose registry
    ref resolves to this tool's family, so the UI can show "where is this used"
    and warn before deprecate/archive.
    """
    require_user(request)
    tool_id = request.path_params["tool_id"]
    factory = get_session_factory(request)
    with factory() as session:
        tool = session.get(CaliberToolRegistry, tool_id)
        if tool is None:
            raise HTTPException(status_code=404, detail=f"tool {tool_id!r} not found")
        # ``family_name(ref) == tool.name`` implies ``tool.name`` is a substring
        # of the serialized manifest (it's embedded in the registry_ref), so a
        # coarse SQL ``contains`` prefilter is a correctness-preserving superset
        # — the exact ``family_name`` check below refines away false positives.
        # This avoids hydrating every workflow version (full manifests) just to
        # find the handful that reference one tool. Only the display columns are
        # loaded, not the ORM entity.
        rows = session.execute(
            select(
                CaliberWorkflowVersion.workflow_id,
                CaliberWorkflowVersion.version_id,
                CaliberWorkflowVersion.version_number,
                CaliberWorkflowVersion.status,
                CaliberWorkflowVersion.manifest,
            ).where(
                cast(CaliberWorkflowVersion.manifest, String).contains(tool.name, autoescape=True)
            )
        ).all()
        usage: list[dict[str, object]] = []
        for workflow_id, version_id, version_number, status, manifest in rows:
            tools = (manifest or {}).get("tools", {})
            refs = [b.get("registry_ref", "") for b in tools.values() if isinstance(b, dict)]
            if any(ref and family_name(ref) == tool.name for ref in refs):
                usage.append(
                    {
                        "workflow_id": workflow_id,
                        "version_id": version_id,
                        "version_number": version_number,
                        "status": status,
                    }
                )
    return JSONResponse({"data": {"tool_id": tool_id, "name": tool.name, "usage": usage}})


# ---------------------------------------------------------------------------
# Durable tool-test runs — run history for the Tools tab (prompt analog).
# ---------------------------------------------------------------------------


def _aggregate_tool_test_run(
    results: list[Any],
) -> tuple[int, int, int, int, float | None]:
    """Recompute (size, passed, failed, partial, overall_score) from results.

    The client never supplies aggregates — they're derived here so a buggy or
    malicious payload can't desync the durable summary from the per-case data.
    """
    size = len(results)
    passed = sum(1 for r in results if r.verdict == "pass")
    failed = sum(1 for r in results if r.verdict == "fail")
    partial = sum(1 for r in results if r.verdict == "partial")
    overall = (sum(r.score for r in results) / size) if size else None
    return size, passed, failed, partial, overall


def _tool_lifecycle(
    *,
    status: str,
    has_run: bool,
    has_calibration: bool,
    has_fixtures: bool,
) -> str:
    """Compute the tool lifecycle pill.

    Precedence (highest first):

    * ``Published`` — the tool is registry-``active`` AND has ≥1 durable run
      (a validated, live tool — not merely an active-but-never-exercised row).
    * ``Hardened`` — a ``last_calibration`` result is present.
    * ``Tested`` — ≥1 durable tool-test run exists.
    * ``Has fixtures`` — saved ``test_cases`` are non-empty.
    * ``Draft`` — none of the above.
    """
    if status == "active" and has_run:
        return "Published"
    if has_calibration:
        return "Hardened"
    if has_run:
        return "Tested"
    if has_fixtures:
        return "Has fixtures"
    return "Draft"


async def create_tool_test_run(request: Request) -> JSONResponse:
    """``POST /caliber/tools/test-runs`` — persist a completed tool-test run.

    Body: ``tool_id``, optional ``kind``/``tool_version`` snapshot, the per-case
    ``results`` array, and optional ``trace_id``/``mlflow_run_id``. The server
    recomputes the count/score summary (never trusting client aggregates) and
    stores one durable row. 400 on empty results; 404 if the tool is unknown.
    """
    body = await parse_json_object(request)
    payload = ToolTestRunCreateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])

    if not payload.results:
        raise HTTPException(status_code=400, detail="'results' must not be empty")

    size, passed, failed, partial, overall = _aggregate_tool_test_run(payload.results)
    now = datetime.now(timezone.utc)

    factory = get_session_factory(request)
    with factory() as session:
        tool = session.get(CaliberToolRegistry, payload.tool_id)
        if tool is None:
            raise HTTPException(status_code=404, detail=f"tool {payload.tool_id!r} not found")

        run = CaliberToolTestRun(
            test_run_id=new_tool_test_run_id(),
            tool_id=payload.tool_id,
            tool_version=payload.tool_version,
            kind=payload.kind,
            test_set_size=size,
            passed_count=passed,
            failed_count=failed,
            partial_count=partial,
            overall_score=overall,
            results=[r.model_dump(mode="json") for r in payload.results],
            trace_id=payload.trace_id,
            mlflow_run_id=payload.mlflow_run_id,
            created_by=actor,
            status="completed",
            completed_at=now,
        )
        session.add(run)
        session.flush()

        audit_record(
            session,
            actor=actor,
            action="create_tool_test_run",
            entity_type="tool_test_run",
            entity_id=run.test_run_id,
            details={
                "tool_id": run.tool_id,
                "kind": run.kind,
                "test_set_size": size,
                "passed_count": passed,
                "failed_count": failed,
                "partial_count": partial,
            },
        )
        session.commit()
        summary = ToolTestRunSummary.model_validate(run)

    return JSONResponse({"data": summary.model_dump(mode="json")}, status_code=201)


async def list_tool_test_runs(request: Request) -> JSONResponse:
    """``GET /caliber/tools/test-runs`` — newest-first run history (summaries).

    ``tool_id`` filters to one tool (all tools if omitted). ``kind`` filters to
    one run kind. ``limit`` defaults to 20 and is capped at 100. The heavy
    per-case ``results`` array is omitted.
    """
    require_user(request)
    tool_id = request.query_params.get("tool_id")
    kind = request.query_params.get("kind")

    raw_limit = request.query_params.get("limit")
    limit = _TEST_RUNS_DEFAULT_LIMIT
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="'limit' must be an integer") from exc
        if limit < 1:
            raise HTTPException(status_code=400, detail="'limit' must be >= 1")
    limit = min(limit, _TEST_RUNS_MAX_LIMIT)

    stmt = select(CaliberToolTestRun)
    if tool_id:
        stmt = stmt.where(CaliberToolTestRun.tool_id == tool_id)
    if kind:
        stmt = stmt.where(CaliberToolTestRun.kind == kind)
    stmt = stmt.order_by(CaliberToolTestRun.created_at.desc()).limit(limit)

    factory = get_session_factory(request)
    with factory() as session:
        rows = session.execute(stmt).scalars().all()
        summaries = [ToolTestRunSummary.model_validate(row).model_dump(mode="json") for row in rows]

    return JSONResponse({"data": summaries})


async def get_tool_test_run(request: Request) -> JSONResponse:
    """``GET /caliber/tools/test-runs/{test_run_id}`` — full run incl. results."""
    require_user(request)
    test_run_id = request.path_params["test_run_id"]

    factory = get_session_factory(request)
    with factory() as session:
        run = session.get(CaliberToolTestRun, test_run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"test run {test_run_id!r} not found")
        detail = ToolTestRunDetail.model_validate(run)

    return JSONResponse({"data": detail.model_dump(mode="json")})


async def get_tool_workspace(request: Request) -> JSONResponse:
    """``GET /caliber/tools/{tool_id}/workspace`` — runtime facts + lifecycle.

    Returns the tool's registry version, side-effect level, registry status, the
    computed ``lifecycle`` pill (Published > Hardened > Tested > Has fixtures >
    Draft), the latest run summary, the pinned baseline (id + summary), whether
    saved fixtures exist, and the last calibration's pass-rate.
    """
    require_user(request)
    tool_id = request.path_params["tool_id"]

    factory = get_session_factory(request)
    with factory() as session:
        tool = session.get(CaliberToolRegistry, tool_id)
        if tool is None:
            raise HTTPException(status_code=404, detail=f"tool {tool_id!r} not found")

        latest_run = (
            session.execute(
                select(CaliberToolTestRun)
                .where(CaliberToolTestRun.tool_id == tool_id)
                .order_by(CaliberToolTestRun.created_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        last_run = (
            ToolWorkspaceLastRun.model_validate(latest_run) if latest_run is not None else None
        )

        has_run = latest_run is not None
        has_fixtures = bool(tool.test_cases)
        calibration = tool.last_calibration if isinstance(tool.last_calibration, dict) else None
        has_calibration = calibration is not None
        last_calibration_score = (
            float(calibration["pass_rate"])
            if calibration is not None and isinstance(calibration.get("pass_rate"), (int, float))
            else None
        )

        lifecycle = _tool_lifecycle(
            status=tool.status,
            has_run=has_run,
            has_calibration=has_calibration,
            has_fixtures=has_fixtures,
        )

        # Surface the pinned baseline (if any) plus a cheap summary. A stale id
        # (run since deleted, or no longer belonging to this tool) reads as no
        # baseline.
        baseline_run_id = tool.baseline_run_id
        baseline_run: ToolWorkspaceLastRun | None = None
        if baseline_run_id:
            baseline_row = session.get(CaliberToolTestRun, baseline_run_id)
            if baseline_row is not None and baseline_row.tool_id == tool_id:
                baseline_run = ToolWorkspaceLastRun.model_validate(baseline_row)
            else:
                baseline_run_id = None

        response = ToolWorkspaceResponse(
            version=tool.version,
            side_effect_level=tool.side_effect_level,
            status=tool.status,
            lifecycle=lifecycle,
            last_run=last_run,
            baseline_run_id=baseline_run_id,
            baseline_run=baseline_run,
            has_fixtures=has_fixtures,
            last_calibration_score=last_calibration_score,
        )

    return JSONResponse({"data": response.model_dump(mode="json")})


async def set_tool_baseline(request: Request) -> JSONResponse:
    """``POST /caliber/tools/{tool_id}/baseline`` — pin a run as the baseline.

    Validates that ``test_run_id`` refers to an existing tool-test run AND that
    the run belongs to this tool (``tool_id`` matches); 404 if the run is
    missing, 400 if it belongs to a different tool. On success the id is recorded
    on the tool's ``baseline_run_id`` column so the Runs tab can diff against it.
    """
    tool_id = request.path_params["tool_id"]
    body = await parse_json_object(request)
    payload = ToolBaselineRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])

    factory = get_session_factory(request)
    with factory() as session:
        tool = session.get(CaliberToolRegistry, tool_id)
        if tool is None:
            raise HTTPException(status_code=404, detail=f"tool {tool_id!r} not found")

        run = session.get(CaliberToolTestRun, payload.test_run_id)
        if run is None:
            raise HTTPException(
                status_code=404, detail=f"test run {payload.test_run_id!r} not found"
            )
        if run.tool_id != tool_id:
            raise HTTPException(
                status_code=400,
                detail=(f"test run {payload.test_run_id!r} does not belong to tool {tool_id!r}"),
            )

        tool.baseline_run_id = payload.test_run_id
        audit_record(
            session,
            actor=actor,
            action="set_tool_baseline",
            entity_type="tool",
            entity_id=tool_id,
            details={"baseline_run_id": payload.test_run_id},
        )
        session.commit()

    return JSONResponse({"data": {"baseline_run_id": payload.test_run_id}}, status_code=200)


def register(app: Starlette) -> None:
    app.routes.append(Route(LIST_PATH, list_tools, methods=["GET"]))
    app.routes.append(Route(LIST_PATH, register_tool, methods=["POST"]))
    # Test-run routes must precede the ``{tool_id}`` DETAIL_PATH so
    # ``/tools/test-runs`` resolves here rather than being captured as a tool id.
    app.routes.append(Route(TEST_RUNS_PATH, create_tool_test_run, methods=["POST"]))
    app.routes.append(Route(TEST_RUNS_PATH, list_tool_test_runs, methods=["GET"]))
    app.routes.append(Route(TEST_RUN_DETAIL_PATH, get_tool_test_run, methods=["GET"]))
    app.routes.append(Route(DETAIL_PATH, get_tool, methods=["GET"]))
    app.routes.append(Route(SOURCE_PATH, get_tool_source, methods=["GET"]))
    app.routes.append(Route(DETAIL_PATH, update_tool, methods=["PATCH"]))
    app.routes.append(Route(ARCHIVE_PATH, archive_tool, methods=["POST"]))
    app.routes.append(Route(TEST_RUN_PATH, test_run_tool, methods=["POST"]))
    app.routes.append(Route(TEST_CASES_PATH, save_tool_test_cases, methods=["PUT"]))
    app.routes.append(Route(CALIBRATE_PATH, calibrate_tool, methods=["POST"]))
    app.routes.append(Route(WORKSPACE_PATH, get_tool_workspace, methods=["GET"]))
    app.routes.append(Route(BASELINE_PATH, set_tool_baseline, methods=["POST"]))
    app.routes.append(Route(DETAIL_PATH + "/usage", tool_usage, methods=["GET"]))
