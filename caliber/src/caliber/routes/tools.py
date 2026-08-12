"""``/caliber/tools`` endpoints — the tool registry (plan §15.4, §18.1).

Only admins register/update/archive tools; designers select registered tools in
the editor. Archiving is blocked while a ``prod``-deployed workflow references
the tool family (plan §14.4, §19.12). Secret values are never accepted or
returned — only ``secret_refs`` (names).
"""

from __future__ import annotations

import copy
import re
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import String, cast, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import (
    SCOPE_ADMIN,
    SCOPE_OPERATOR,
    CaliberIdentity,
    require_scopes,
    require_user,
    resolve_identity,
)
from caliber.calibration import aggregate, evaluate_assertion
from caliber.db.models import (
    CaliberCalibrationJob,
    CaliberToolRegistry,
    CaliberToolTestRun,
    CaliberWorkflow,
    CaliberWorkflowDeployment,
    CaliberWorkflowVersion,
)
from caliber.db.scoping import apply_visibility_filter, get_visible
from caliber.egress import EgressPolicy
from caliber.ids import new_tool_id, new_tool_test_run_id
from caliber.orchestrator import calibration_drain
from caliber.routes._deps import (
    envelope_response,
    get_session_factory,
    parse_json_object,
    visibility_param,
)
from caliber.schemas import (
    CalibrationJobListSchema,
    CalibrationJobSchema,
    CalibrationQueuedSchema,
    CalibrationResolutionSchema,
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
from caliber.workflows.runtime import _resolve_bound_tool_callable
from caliber.workflows.sandbox import make_preview_callable, should_mock_in_preview
from caliber.workflows.tools import InMemoryToolResolver, family_name, registered_tool_module_allowed

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
CALIBRATION_JOBS_PATH = DETAIL_PATH + "/calibration-jobs"
CALIBRATION_JOB_DETAIL_PATH = CALIBRATION_JOBS_PATH + "/{job_id}"
CALIBRATION_JOB_RESOLVE_PATH = CALIBRATION_JOB_DETAIL_PATH + "/resolve"
SOURCE_PATH = DETAIL_PATH + "/source"
WORKSPACE_PATH = DETAIL_PATH + "/workspace"
BASELINE_PATH = DETAIL_PATH + "/baseline"
VERSIONS_PATH = DETAIL_PATH + "/versions"

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


def _visible_tool_or_404(session: Session, request: Request, tool_id: str) -> CaliberToolRegistry:
    """Resolve one tool through the same visibility policy as the registry list.

    Tool list was scoped while most nested routes used bare primary-key reads. Knowing a
    tool id therefore unlocked detail/source, live test execution, fixture mutation,
    calibration, usage, workspace, and baseline changes even when the caller could not
    list the parent. A shared parent lookup keeps every verb on one authorization rule and
    returns 404 so a forbidden id is indistinguishable from a missing one.
    """
    row: CaliberToolRegistry | None = get_visible(
        session,
        CaliberToolRegistry,
        CaliberToolRegistry.tool_id,
        tool_id,
        resolve_identity(request),
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"tool {tool_id!r} not found")
    return row


def _resolve_running_calibration_job(
    factory: Callable[[], Session],
    *,
    tool_id: str,
    job_id: str,
    actor: str,
    identity: CaliberIdentity,
    action: str,
    reason: str,
) -> dict[str, object]:
    """Resolve a running calibration outside the async route's event-loop thread."""
    with factory() as session:
        tool = get_visible(
            session,
            CaliberToolRegistry,
            CaliberToolRegistry.tool_id,
            tool_id,
            identity,
        )
        if tool is None:
            raise HTTPException(status_code=404, detail=f"tool {tool_id!r} not found")
        job = session.get(CaliberCalibrationJob, job_id)
        if job is None or job.tool_id != tool_id:
            raise HTTPException(status_code=404, detail=f"calibration job {job_id!r} not found")
        if job.status != calibration_drain.STATUS_RUNNING:
            raise HTTPException(
                status_code=409,
                detail=f"calibration job {job_id!r} is {job.status!r}, not 'running'",
            )
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        job.status = calibration_drain.STATUS_FAILED
        job.error = f"operator {action}: {reason}"[:2000]
        job.finished_at = now
        job.resolution = action
        job.resolution_reason = reason
        job.resolved_by = actor
        job.resolved_at = now
        retry: CaliberCalibrationJob | None = None
        if action == "retry":
            retry = CaliberCalibrationJob(
                job_id=f"CAL-{uuid.uuid4().hex[:12]}",
                tool_id=job.tool_id,
                status=calibration_drain.STATUS_QUEUED,
                requested_by=actor,
                tool_snapshot=copy.deepcopy(job.tool_snapshot),
                test_cases=copy.deepcopy(job.test_cases),
                retry_of_job_id=job.job_id,
                created_at=now,
            )
            session.add(retry)
        audit_record(
            session,
            actor=actor,
            action="resolve_calibration_job",
            entity_type="tool",
            entity_id=tool_id,
            details={
                "job_id": job.job_id,
                "resolution": action,
                "reason": reason,
                "retry_job_id": retry.job_id if retry else None,
            },
        )
        session.commit()
        return {
            "job_id": job.job_id,
            "status": job.status,
            "resolution": action,
            "retry_job_id": retry.job_id if retry else None,
        }


def _visible_tool_test_run_or_404(
    session: Session, request: Request, test_run_id: str
) -> CaliberToolTestRun:
    """Resolve a durable test run through its visible parent tool.

    The run table has no visibility columns of its own. A bare child lookup is still
    useful to find its parent, but the row is not returnable until that parent passes
    the registry visibility policy. Both a missing child and a child of a hidden tool
    therefore produce the same child-level 404 without disclosing the parent id.
    """
    run = session.get(CaliberToolTestRun, test_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"test run {test_run_id!r} not found")
    try:
        _visible_tool_or_404(session, request, run.tool_id)
    except HTTPException as exc:
        raise HTTPException(status_code=404, detail=f"test run {test_run_id!r} not found") from exc
    return run


async def get_tool(request: Request) -> JSONResponse:
    require_user(request)
    tool_id = request.path_params["tool_id"]
    factory = get_session_factory(request)
    with factory() as session:
        row = _visible_tool_or_404(session, request, tool_id)
        data = ToolSchema.model_validate(row)
    return envelope_response(data)


def _resolve_tool_source(
    module_path: str,
    callable_name: str,
    *,
    config: Any | None = None,
) -> dict[str, Any]:
    """Best-effort source/signature/doc for a registered tool's callable.

    The module is imported and reflected **inside the sandbox child**. Importing for
    metadata is still execution, so doing it in this API process would bypass the
    registered-tool boundary before the tool ever ran. Returns ``available=False`` with
    an error when policy refuses the module, import fails, or source is unavailable.
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
    if not registered_tool_module_allowed(module_path):
        result["error"] = (
            f"module '{module_path}' is not in CALIBER_REGISTERED_TOOL_MODULE_ALLOWLIST"
        )
        return result

    from caliber.tool_sandbox.models import ToolSandboxInspectRequest  # noqa: PLC0415
    from caliber.tool_sandbox.service import sandbox_from_optional_config  # noqa: PLC0415

    sandbox = sandbox_from_optional_config(config)
    inspected = sandbox.inspect_tool(
        ToolSandboxInspectRequest(
            module_path=module_path,
            callable_name=callable_name,
            timeout_seconds=float(getattr(config, "registered_tool_sandbox_timeout_seconds", 30.0)),
        )
    )
    result.update(
        {
            "available": inspected.available,
            "signature": inspected.signature or callable_name,
            "doc": inspected.doc,
            "source": inspected.source,
            "error": inspected.error,
        }
    )
    return result


def _tool_version_sort_key(version: str) -> tuple[tuple[int, int, str], ...]:
    """Natural-order sort key for a tool ``version`` string.

    ``version`` is a free-form ``String`` column, so a SQL ``ORDER BY version``
    sorts lexically — ``"10" < "9"`` and ``"1.10" < "1.9"`` — which lists a
    tool's history in the wrong order once it hits a two-digit component. This
    compares each dot/dash/plus/underscore-separated component numerically when
    it is an integer and lexically otherwise, so ``9 < 10`` and ``1.9 < 1.10``.
    """
    components: list[tuple[int, int, str]] = []
    for token in re.split(r"[.\-+_]", version.strip()):
        if token.isdigit():
            components.append((1, int(token), ""))
        else:
            # Non-numeric tokens (e.g. "beta") sort before numeric ones at the
            # same position and compare lexically among themselves.
            components.append((0, 0, token))
    return tuple(components)


async def list_tool_versions(request: Request) -> JSONResponse:
    """``GET /caliber/tools/{tool_id}/versions`` — all versions in this tool's
    family (same ``name``), newest version first.

    Tools have no live alias to promote/roll back; this is the version-history
    inventory the registry was missing (the detail page previously only listed
    *workflow* versions that reference the tool, not the tool's own versions).
    Scoped by visibility so it never leaks another project's tool versions, and
    sorted with a version-aware key so 2-digit components order correctly.
    """
    require_user(request)
    identity = resolve_identity(request)
    tool_id = request.path_params["tool_id"]
    factory = get_session_factory(request)
    with factory() as session:
        tool = _visible_tool_or_404(session, request, tool_id)
        stmt = select(CaliberToolRegistry).where(CaliberToolRegistry.name == tool.name)
        stmt = apply_visibility_filter(
            stmt,
            CaliberToolRegistry,
            identity,
            identity.active_project_id,
            only=visibility_param(request),
        )
        rows = session.execute(stmt).scalars().all()
        rows = sorted(rows, key=lambda r: _tool_version_sort_key(r.version), reverse=True)
        items = [ToolSchema.model_validate(r) for r in rows]
    return envelope_response(items)


async def get_tool_source(request: Request) -> JSONResponse:
    """Return the tool callable's real source + signature + docstring."""
    require_user(request)
    tool_id = request.path_params["tool_id"]
    factory = get_session_factory(request)
    with factory() as session:
        row = _visible_tool_or_404(session, request, tool_id)
        if row.execution_backend != "python_callable":
            return JSONResponse(
                {
                    "data": {
                        "module_path": row.module_path,
                        "callable_name": row.callable_name,
                        "available": False,
                        "signature": row.name,
                        "doc": "",
                        "source": "",
                        "error": (
                            f"tool {row.tool_id!r} uses execution_backend "
                            f"{row.execution_backend!r}; no Python source exists"
                        ),
                    }
                }
            )
        module_path = row.module_path
        callable_name = row.callable_name
    source = await run_in_threadpool(
        _resolve_tool_source,
        module_path,
        callable_name,
        config=getattr(request.app.state, "config", None),
    )
    return JSONResponse({"data": source})


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
            execution_backend=payload.execution_backend,
            backend_config=payload.backend_config,
            input_schema=payload.input_schema,
            output_schema=payload.output_schema,
            side_effect_level=payload.side_effect_level,
            requires_approval=payload.requires_approval,
            allow_in_preview=payload.allow_in_preview,
            secret_refs=list(payload.secret_refs),
            # Owner is the authenticated actor (payload.owner is ignored).
            owner=actor,
            project_id=identity.active_project_id,
            # ``public`` — not ``user`` — when no project is active, and the distinction
            # matters once the Tool family is visibility-scoped.
            #
            # Tool creation is admin-gated and a tool is shared catalog infrastructure: a
            # name, a module path, and a callable that workflow authors reference. ``user``
            # visibility means "only the admin who registered it may use it", and combined
            # with scoping that made the operator test-run path unreachable for the role it
            # exists for — a non-admin operator can never own a tool, so it could never see
            # one. A full-suite run caught it as a 404 in ``test_rbac_enforcement``.
            #
            # An active project still yields ``project`` visibility, so deliberate
            # per-project tools keep isolating. This only changes the no-project case, and
            # for a single-organization deployment "registered by an admin, outside any
            # project" means an org-wide tool rather than a private one.
            visibility="project" if identity.active_project_id else "public",
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
        tool = _visible_tool_or_404(session, request, tool_id)
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
        calibration_drain.invalidate_tool_calibration(session, tool)
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
        tool = _visible_tool_or_404(session, request, tool_id)
        blocking = _referencing_deployments(session, tool.name)
        if blocking:
            raise HTTPException(
                status_code=409,
                detail=f"tool {tool.name!r} is referenced by deployed workflow(s) {blocking}; "
                "undeploy them before archiving",
            )
        if tool.status != "archived":
            tool.status = "archived"
            calibration_drain.invalidate_tool_calibration(session, tool)
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


def _invoke_tool_under_preview_policy(
    tool_data: ToolSchema,
    tool_input: dict[str, Any],
    *,
    config: Any | None = None,
) -> dict[str, Any]:
    """Run one tool invocation under preview policy and process isolation.

    Live read tools use the registered-module sandbox; their module import and callable
    body never enter the API process. Unsafe tools are mocked *without importing their
    module at all*. Importing first and deciding to mock second was itself an execution
    bug: module-level code runs on import, so a tool described as "always mocked" could
    still perform arbitrary effects in the control plane.

    Returns ``{output, mocked, duration_ms, error, isolation}``.
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
        execution_backend=tool_data.execution_backend,
        backend_config=tool_data.backend_config if isinstance(tool_data.backend_config, dict) else None,
        secret_refs=tuple(tool_data.secret_refs),
        input_schema=tool_data.input_schema if isinstance(tool_data.input_schema, dict) else None,
        output_schema=tool_data.output_schema
        if isinstance(tool_data.output_schema, dict)
        else None,
    )

    mocked = should_mock_in_preview(binding)
    import_error: str | None = None
    # The allowlist governs *module imports*. A declarative HTTP tool has no module to
    # import — its ``module_path`` is the ``<openapi_http>`` sentinel — so applying the
    # allowlist to it would reject every published OpenAPI tool on any deployment that
    # configures one, with an error naming a module that does not exist. Its egress is
    # governed instead, by the policy threaded into the executor below.
    if binding.execution_backend == "python_callable" and not registered_tool_module_allowed(
        binding.module_path
    ):
        import_error = (
            f"module '{binding.module_path}' is not in CALIBER_REGISTERED_TOOL_MODULE_ALLOWLIST"
        )

    start_time = time.monotonic()
    error: str | None = import_error
    output: Any = None
    isolation = "not_run"

    if error is None:
        if mocked:
            # No import is necessary for a deterministic preview mock. This ordering is
            # security-significant: importing an unsafe tool merely to avoid calling it
            # still executes arbitrary module initialization in the control plane.
            output = make_preview_callable(binding, None)(**tool_input)
            isolation = "mocked"
        else:
            if binding.execution_backend == "openapi_http":
                isolation = "inline_http"
                try:
                    fn = _resolve_bound_tool_callable(
                        binding,
                        InMemoryToolResolver([]),
                        preview=False,
                        required=True,
                        egress_policy=EgressPolicy.from_config(config),
                    )
                    assert fn is not None
                    output = fn(**tool_input)
                except Exception as exc:
                    error = str(exc)
            else:
                from caliber.tool_sandbox.models import ToolSandboxRunRequest  # noqa: PLC0415
                from caliber.tool_sandbox.service import (  # noqa: PLC0415
                    sandbox_from_optional_config,
                )

                isolation = "subprocess"
                sandbox = sandbox_from_optional_config(config)
                result = sandbox.run_tool(
                    ToolSandboxRunRequest(
                        module_path=binding.module_path,
                        callable_name=binding.callable_name,
                        input=tool_input,
                        timeout_seconds=float(
                            getattr(config, "registered_tool_sandbox_timeout_seconds", 30.0)
                        ),
                    )
                )
                if result.status == "completed":
                    output = result.output
                else:
                    error = result.error or result.status

    duration_ms = round((time.monotonic() - start_time) * 1000, 1)
    return {
        "output": output,
        "mocked": mocked,
        "duration_ms": duration_ms,
        "error": error,
        "isolation": isolation,
    }


async def test_run_tool(request: Request) -> JSONResponse:
    """``POST /caliber/tools/{tool_id}/test-run`` — test invocation under preview policy.

    Accepts ``{"input": {...}}`` and invokes the tool with preview effect policy
    applied: ``write``/``external_action`` tools are always mocked, and ``read`` tools
    run live only when ``allow_in_preview`` is True.

    Live read tools are imported and called in the registered-module subprocess. Unsafe
    tools are mocked without importing their module. The response reports the path as
    ``subprocess``, ``mocked``, or ``not_run``.
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
        tool = _visible_tool_or_404(session, request, tool_id)
        tool_data = ToolSchema.model_validate(tool)

    invocation = await run_in_threadpool(
        _invoke_tool_under_preview_policy,
        tool_data,
        tool_input,
        config=getattr(request.app.state, "config", None),
    )
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
        tool = _visible_tool_or_404(session, request, tool_id)
        if list(tool.test_cases or []) != cases:
            tool.test_cases = cases
            calibration_drain.invalidate_tool_calibration(session, tool)
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


def _score_tool_cases(
    tool_data: ToolSchema,
    saved_cases: list[dict[str, Any]],
    *,
    config: Any | None,
    should_stop: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """Execute a calibration snapshot outside the ASGI event loop."""
    scored: list[dict[str, Any]] = []
    for case in saved_cases:
        if should_stop is not None and should_stop():
            raise InterruptedError("calibration stop requested")
        name = str(case.get("name", "")) or "case"
        raw_input = case.get("input")
        case_input: dict[str, Any] = dict(raw_input) if isinstance(raw_input, dict) else {}
        invocation = _invoke_tool_under_preview_policy(
            tool_data,
            case_input,
            config=config,
        )
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
    return scored


async def submit_calibration(request: Request) -> JSONResponse:
    """``POST /caliber/tools/{tool_id}/calibration-jobs`` — queue a calibration.

    The durable form of ``calibrate_tool``. That route runs every saved case inline and
    returns the aggregate, which is fine for a handful of cases and wrong for two hundred:
    the client holds a connection open for minutes, a proxy timeout or a closed lid loses
    the result, and nothing records that the work happened.

    Returns ``202`` with a job id. Both the executable Tool definition and cases are
    snapshotted here rather than read at execution time, because scoring against a module,
    policy, schema, or fixture the author has since edited would produce a pass rate that
    belongs to no submitted revision.
    """
    tool_id = request.path_params["tool_id"]
    actor = require_scopes(request, [SCOPE_OPERATOR])

    factory = get_session_factory(request)
    with factory() as session:
        tool = _visible_tool_or_404(session, request, tool_id)
        saved_cases = list(tool.test_cases or [])
        if not saved_cases:
            raise HTTPException(
                status_code=400,
                detail=f"tool {tool_id!r} has no saved test cases to calibrate against",
            )
        job = CaliberCalibrationJob(
            job_id=f"CAL-{uuid.uuid4().hex[:12]}",
            tool_id=tool_id,
            status=calibration_drain.STATUS_QUEUED,
            requested_by=actor,
            tool_snapshot=calibration_drain.snapshot_tool(tool),
            test_cases=saved_cases,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        session.add(job)
        audit_record(
            session,
            actor=actor,
            action="submit_calibration",
            entity_type="tool",
            entity_id=tool_id,
            details={"job_id": job.job_id, "cases": len(saved_cases)},
        )
        session.commit()
        job_id = job.job_id

    return envelope_response(
        CalibrationQueuedSchema(
            job_id=job_id, tool_id=tool_id, status=calibration_drain.STATUS_QUEUED
        ),
        status_code=202,
    )


async def get_calibration_job(request: Request) -> JSONResponse:
    """``GET /caliber/tools/{tool_id}/calibration-jobs/{job_id}`` — poll one job.

    Scoped through the tool, like every other child in this family: a job id must not be a
    way to read a calibration for a tool the caller cannot see.
    """
    tool_id = request.path_params["tool_id"]
    job_id = request.path_params["job_id"]
    require_user(request)

    factory = get_session_factory(request)
    with factory() as session:
        _visible_tool_or_404(session, request, tool_id)
        job = session.get(CaliberCalibrationJob, job_id)
        if job is None or job.tool_id != tool_id:
            raise HTTPException(status_code=404, detail=f"calibration job {job_id!r} not found")
        payload = {
            "job_id": job.job_id,
            "tool_id": job.tool_id,
            "status": job.status,
            "requested_by": job.requested_by,
            "result": job.result,
            "error": job.error,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "claimed_at": job.claimed_at.isoformat() if job.claimed_at else None,
            "claimed_by": job.claimed_by,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "retry_of_job_id": job.retry_of_job_id,
            "resolution": job.resolution,
            "resolution_reason": job.resolution_reason,
            "resolved_by": job.resolved_by,
            "resolved_at": job.resolved_at.isoformat() if job.resolved_at else None,
        }
    return envelope_response(CalibrationJobSchema.model_validate(payload))


async def list_calibration_jobs(request: Request) -> JSONResponse:
    """``GET /caliber/tools/{tool_id}/calibration-jobs`` — recent jobs, newest first."""
    tool_id = request.path_params["tool_id"]
    require_user(request)

    factory = get_session_factory(request)
    with factory() as session:
        _visible_tool_or_404(session, request, tool_id)
        rows = (
            session.execute(
                select(CaliberCalibrationJob)
                .where(CaliberCalibrationJob.tool_id == tool_id)
                .order_by(CaliberCalibrationJob.created_at.desc())
                .limit(50)
            )
            .scalars()
            .all()
        )
        items = [
            {
                "job_id": r.job_id,
                "status": r.status,
                "requested_by": r.requested_by,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "claimed_at": r.claimed_at.isoformat() if r.claimed_at else None,
                "claimed_by": r.claimed_by,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "pass_rate": (r.result or {}).get("pass_rate") if r.result else None,
                "retry_of_job_id": r.retry_of_job_id,
                "resolution": r.resolution,
                "resolution_reason": r.resolution_reason,
                "resolved_by": r.resolved_by,
                "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
            }
            for r in rows
        ]
    return envelope_response(
        CalibrationJobListSchema(
            jobs=[CalibrationJobSchema.model_validate(i) for i in items], total=len(items)
        )
    )


async def resolve_calibration_job(request: Request) -> JSONResponse:
    """Abandon or explicitly retry an ambiguously ``running`` calibration.

    Automatic requeue is unsafe because authored tools may have side effects. An
    operator resolution terminally fences the original row; retry creates a new
    queued row with immutable input snapshots and lineage back to the original.
    Late worker completion is ignored by the drain's ``status='running'`` fence.
    """
    tool_id = request.path_params["tool_id"]
    job_id = request.path_params["job_id"]
    actor = require_scopes(request, [SCOPE_OPERATOR])
    body = await parse_json_object(request)
    action = str(body.get("action") or "").strip().casefold()
    reason = str(body.get("reason") or "").strip()
    if action not in {"abandon", "retry"}:
        raise HTTPException(status_code=400, detail="'action' must be 'abandon' or 'retry'")
    if not reason:
        raise HTTPException(status_code=400, detail="a non-empty resolution reason is required")

    factory = get_session_factory(request)
    data = await run_in_threadpool(
        _resolve_running_calibration_job,
        factory,
        tool_id=tool_id,
        job_id=job_id,
        actor=actor,
        identity=resolve_identity(request),
        action=action,
        reason=reason,
    )
    return envelope_response(CalibrationResolutionSchema.model_validate(data))


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
        tool = _visible_tool_or_404(session, request, tool_id)
        saved_cases = list(tool.test_cases or [])
        if not saved_cases:
            raise HTTPException(
                status_code=400,
                detail=f"tool {tool_id!r} has no saved test cases to calibrate against",
            )
        tool_data = ToolSchema.model_validate(tool)
        submitted_snapshot = calibration_drain.snapshot_tool(tool)

    # Spawning and waiting on child processes is blocking I/O. Run the whole snapshot in
    # the bounded Starlette worker pool and, critically, do not hold a database session
    # while as many as 200 cases execute.
    scored = await run_in_threadpool(
        _score_tool_cases,
        tool_data,
        saved_cases,
        config=getattr(request.app.state, "config", None),
    )
    result = aggregate(scored)
    result["ran_at"] = datetime.now(timezone.utc).isoformat()

    with factory() as session:
        tool = _visible_tool_or_404(session, request, tool_id)
        stale_reasons = calibration_drain.attach_calibration_if_current(
            session,
            tool_id,
            submitted_snapshot,
            saved_cases,
            result,
        )
        if stale_reasons:
            changed = []
            if (
                "tool_definition_changed" in stale_reasons
                or "tool_revision_changed" in stale_reasons
            ):
                changed.append("tool definition")
            if "test_cases_changed" in stale_reasons:
                changed.append("test cases")
            raise HTTPException(
                status_code=409,
                detail=(
                    f"tool {tool_id!r} {' and '.join(changed) or 'inputs'} changed while "
                    "calibration was running"
                ),
            )
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
    identity = resolve_identity(request)
    tool_id = request.path_params["tool_id"]
    factory = get_session_factory(request)
    with factory() as session:
        tool = _visible_tool_or_404(session, request, tool_id)
        # ``family_name(ref) == tool.name`` implies ``tool.name`` is a substring
        # of the serialized manifest (it's embedded in the registry_ref), so a
        # coarse SQL ``contains`` prefilter is a correctness-preserving superset
        # — the exact ``family_name`` check below refines away false positives.
        # This avoids hydrating every workflow version (full manifests) just to
        # find the handful that reference one tool. Only the display columns are
        # loaded, not the ORM entity.
        stmt = (
            select(
                CaliberWorkflowVersion.workflow_id,
                CaliberWorkflowVersion.version_id,
                CaliberWorkflowVersion.version_number,
                CaliberWorkflowVersion.status,
                CaliberWorkflowVersion.manifest,
            )
            .join(
                CaliberWorkflow,
                CaliberWorkflow.workflow_id == CaliberWorkflowVersion.workflow_id,
            )
            .where(
                cast(CaliberWorkflowVersion.manifest, String).contains(tool.name, autoescape=True)
            )
        )
        # A visible/shared tool can be referenced by private workflows. Usage is a child
        # view of those workflows, so filter the parent before returning workflow/version
        # ids; scoping only the tool would still leak other projects' release inventory.
        stmt = apply_visibility_filter(
            stmt,
            CaliberWorkflow,
            identity,
            identity.active_project_id,
        )
        rows = session.execute(stmt).all()
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
        _visible_tool_or_404(session, request, payload.tool_id)

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
    identity = resolve_identity(request)
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

    factory = get_session_factory(request)
    with factory() as session:
        if tool_id:
            _visible_tool_or_404(session, request, tool_id)
        visible_tool_ids = apply_visibility_filter(
            select(CaliberToolRegistry.tool_id),
            CaliberToolRegistry,
            identity,
            identity.active_project_id,
        )
        stmt = select(CaliberToolTestRun).where(CaliberToolTestRun.tool_id.in_(visible_tool_ids))
        if tool_id:
            stmt = stmt.where(CaliberToolTestRun.tool_id == tool_id)
        if kind:
            stmt = stmt.where(CaliberToolTestRun.kind == kind)
        stmt = stmt.order_by(CaliberToolTestRun.created_at.desc()).limit(limit)
        rows = session.execute(stmt).scalars().all()
        summaries = [ToolTestRunSummary.model_validate(row).model_dump(mode="json") for row in rows]

    return JSONResponse({"data": summaries})


async def get_tool_test_run(request: Request) -> JSONResponse:
    """``GET /caliber/tools/test-runs/{test_run_id}`` — full run incl. results."""
    require_user(request)
    test_run_id = request.path_params["test_run_id"]

    factory = get_session_factory(request)
    with factory() as session:
        run = _visible_tool_test_run_or_404(session, request, test_run_id)
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
        tool = _visible_tool_or_404(session, request, tool_id)

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
        tool = _visible_tool_or_404(session, request, tool_id)

        run = _visible_tool_test_run_or_404(session, request, payload.test_run_id)
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
    # Durable form of the same work: submit returns 202 and the drain executes it.
    app.routes.append(Route(CALIBRATION_JOBS_PATH, submit_calibration, methods=["POST"]))
    app.routes.append(Route(CALIBRATION_JOBS_PATH, list_calibration_jobs, methods=["GET"]))
    app.routes.append(Route(CALIBRATION_JOB_DETAIL_PATH, get_calibration_job, methods=["GET"]))
    app.routes.append(
        Route(CALIBRATION_JOB_RESOLVE_PATH, resolve_calibration_job, methods=["POST"])
    )
    app.routes.append(Route(WORKSPACE_PATH, get_tool_workspace, methods=["GET"]))
    app.routes.append(Route(BASELINE_PATH, set_tool_baseline, methods=["POST"]))
    app.routes.append(Route(VERSIONS_PATH, list_tool_versions, methods=["GET"]))
    app.routes.append(Route(DETAIL_PATH + "/usage", tool_usage, methods=["GET"]))
