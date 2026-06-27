"""Tests targeting the remaining ~125 uncovered lines to push coverage
toward 100%.

Covers: events_stream, manifest validation, static routes, promoter
error paths, workflow tools, rate_limit edge cases, approvals
checkpoint helpers, workflows import YAML errors, workflow_versions
compile errors.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberRefinementJob,
    CaliberWorkflow,
    CaliberWorkflowVersion,
)
from caliber.ids import new_job_id
from tests.workflow_helpers import make_support_manifest

PREFIX = "/ajax-api/2.0/mlflow/caliber"


# ══════════════════════════════════════════════════════════════════════
# 1. events_stream.py — _format_event branches + stream_events endpoint
# ══════════════════════════════════════════════════════════════════════


def test_format_event_with_event_type() -> None:
    """Cover the if-event_type branch (L76–77)."""
    from caliber.routes.events_stream import _format_event

    result = _format_event({"type": "job_update", "id": "J-1"})
    text = result.decode("utf-8")
    assert "event: job_update" in text
    assert "data:" in text


def test_format_event_no_type_key() -> None:
    """Cover the no-type branch."""
    from caliber.routes.events_stream import _format_event

    result = _format_event({"id": "J-1"})
    text = result.decode("utf-8")
    assert "event:" not in text
    assert "data:" in text


def test_stream_events_endpoint_registered(client: TestClient) -> None:
    """The SSE endpoint should be reachable (L115–117)."""
    from caliber.routes.events_stream import STREAM_PATH

    # Verify the route is registered by checking the app routes
    app = client.app
    routes = [getattr(r, "path", "") for r in getattr(app, "routes", [])]
    assert STREAM_PATH in routes or any(STREAM_PATH in str(r) for r in routes)


# ══════════════════════════════════════════════════════════════════════
# 2. manifest.py — validation error paths
# ══════════════════════════════════════════════════════════════════════


def test_manifest_sdk_version_policy_non_runtime_pinned() -> None:
    """L344: reject sdk_version_policy != 'runtime-pinned'."""
    from pydantic import ValidationError

    from caliber.workflows.manifest import RuntimeConfig

    with pytest.raises(ValidationError, match="sdk_version_policy"):
        RuntimeConfig(sdk_version_policy="manifest-pinned")


def test_manifest_node_key_mismatch_error() -> None:
    """L414: node key doesn't match node.id."""
    from caliber.workflows.manifest import WorkflowManifest

    m = make_support_manifest("w2")
    # Rename a node key but keep the id the same
    node = m["nodes"]["support_agent"]
    del m["nodes"]["support_agent"]
    m["nodes"]["wrong_key"] = node
    # Update edges to point to wrong_key
    for edge in m["edges"]:
        if edge["from"] == "support_agent":
            edge["from"] = "wrong_key"
        if edge["to"] == "support_agent":
            edge["to"] = "wrong_key"
    with pytest.raises(Exception, match="does not match"):
        WorkflowManifest.model_validate(m)


def test_manifest_invalid_tool_key() -> None:
    """L437: tool key must match identifier pattern."""
    from caliber.workflows.manifest import WorkflowManifest

    m = make_support_manifest("w3")
    m["tools"]["invalid key!"] = {
        "registry_ref": "some-tool/1.0",
        "version_constraint": "",
    }
    with pytest.raises(Exception, match="tool key"):
        WorkflowManifest.model_validate(m)


def test_manifest_duplicate_edge_ids_error() -> None:
    """L439 area: duplicate edge ids."""
    from caliber.workflows.manifest import WorkflowManifest

    m = make_support_manifest("w4")
    if len(m["edges"]) >= 2:
        m["edges"][1]["id"] = m["edges"][0]["id"]
    with pytest.raises(Exception, match="duplicate edge"):
        WorkflowManifest.model_validate(m)


def test_compute_manifest_hash_raw_dict_parseable() -> None:
    """L499: compute_manifest_hash with a raw dict that parses OK."""
    from caliber.workflows.manifest import compute_manifest_hash

    m = make_support_manifest("w5")
    h = compute_manifest_hash(m)
    assert isinstance(h, str) and len(h) == 64


def test_compute_manifest_hash_raw_dict_unparseable() -> None:
    """L500: compute_manifest_hash with a raw dict that doesn't parse."""
    from caliber.workflows.manifest import compute_manifest_hash

    h = compute_manifest_hash({"bad": True})
    assert isinstance(h, str) and len(h) == 64


def test_parse_manifest_non_dict() -> None:
    """L516: parse_manifest with a non-dict argument."""
    from caliber.workflows.manifest import WorkflowManifestError, parse_manifest

    with pytest.raises(WorkflowManifestError, match="JSON object"):
        parse_manifest([1, 2, 3])  # type: ignore[arg-type]


# ══════════════════════════════════════════════════════════════════════
# 3. static.py — missing UI fallback + register guard
# ══════════════════════════════════════════════════════════════════════


def test_static_missing_ui_response() -> None:
    """L245: _missing_ui_response_for_tests returns an HTML response."""
    from caliber.routes.static import _missing_ui_response_for_tests

    r = _missing_ui_response_for_tests()
    assert r.status_code == 503


def test_static_register_without_handler() -> None:
    """L234: register() raises when static_ui_handler is missing."""
    from starlette.applications import Starlette as StarletteApp

    from caliber.routes.static import register

    app = StarletteApp()
    with pytest.raises(RuntimeError, match="static_ui_handler missing"):
        register(app)


def test_static_spa_fallback_no_index(client: TestClient) -> None:
    """L208–209: serve_path falls back when index.html is absent.

    The handler's ``index_html()`` raises ``FileNotFoundError`` when
    the UI dist directory is missing — the route returns
    ``_missing_ui_response()``.
    """
    handler = client.app.state.static_ui_handler
    with (
        patch.object(handler, "resolve_asset", return_value=None),
        patch.object(handler, "index_html", side_effect=FileNotFoundError("no index")),
    ):
        r = client.get("/caliber/some/deep/path")

    assert r.status_code == 503


# ══════════════════════════════════════════════════════════════════════
# 4. promoter.py — import error + skill promotion failure
# ══════════════════════════════════════════════════════════════════════


def test_mlflow_promoter_rollback_import_error() -> None:
    """L221–222: mlflow import failure during rollback."""
    from caliber.promoter import MLflowPromoter, PromoterError, RollbackRequest

    promoter = MLflowPromoter()
    req = RollbackRequest(
        agent_id="agent-1",
        artifact_type="prompt",
        version_before=2,
        checkpoint_id="CK-1",
    )
    with patch.dict("sys.modules", {"mlflow": None}):
        with pytest.raises(PromoterError, match="not installed"):
            promoter.rollback(req)


def test_skill_promoter_generic_exception() -> None:
    """L443–445: skill promotion raises a non-PromoterError exception."""
    from caliber.promoter import PromoterError, PromotionRequest, SkillPromoter

    # Make the session factory's context manager raise
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(side_effect=RuntimeError("db gone"))
    mock_session.__exit__ = MagicMock(return_value=False)
    promoter = SkillPromoter(session_factory=MagicMock(return_value=mock_session))

    req = PromotionRequest(
        agent_id="agent-1",
        artifact_type="skill",
        new_content="new stuff",
        rationale="reason",
        approval_id="AP-1",
    )
    with pytest.raises(PromoterError, match="failed to promote skill"):
        promoter.promote(req)


# ══════════════════════════════════════════════════════════════════════
# 5. workflows/tools.py — version constraint miss, invalid version
# ══════════════════════════════════════════════════════════════════════


def test_tool_resolution_no_version_satisfies() -> None:
    """L162–163: no version satisfies the constraint."""
    from caliber.workflows.tools import InMemoryToolResolver, ToolRegistryEntry, ToolResolutionError

    resolver = InMemoryToolResolver()
    resolver.register(
        ToolRegistryEntry(
            name="my_tool",
            version="1.0.0",
            module_path="mod",
            callable_name="fn",
            status="active",
        ),
        callable_=lambda: None,
    )
    with pytest.raises(ToolResolutionError, match="no version"):
        resolver.resolve("tool.my_tool.v1", version_constraint=">=2.0")


def test_tool_resolution_invalid_version_sort_key() -> None:
    """L177–178: tool with unparseable version falls back to Version('0')."""
    from caliber.workflows.tools import InMemoryToolResolver, ToolRegistryEntry

    resolver = InMemoryToolResolver()
    # Register two versions of the same tool: one with valid, one with invalid version
    resolver.register(
        ToolRegistryEntry(
            name="my_tool",
            version="abc",
            module_path="mod",
            callable_name="fn",
            status="active",
        ),
        callable_=lambda: None,
    )
    resolver.register(
        ToolRegistryEntry(
            name="my_tool",
            version="1.0.0",
            module_path="mod",
            callable_name="fn",
            status="active",
        ),
        callable_=lambda: None,
    )
    # Resolving by family name picks the valid version (higher sort key)
    result = resolver.resolve("tool.my_tool.v1")
    assert result.entry.version == "1.0.0"


# ══════════════════════════════════════════════════════════════════════
# 6. rate_limit.py — zero refill, is_enabled, non-ASCII header
# ══════════════════════════════════════════════════════════════════════


def test_token_bucket_zero_refill_returns_inf() -> None:
    """L137: time_to_wait with refill_per_second == 0."""
    from caliber.rate_limit import TokenBucket

    bucket = TokenBucket(capacity=10.0, refill_per_second=0.0)
    bucket.tokens = 0.0
    bucket.last_refill = time.monotonic()
    wait = bucket.seconds_until_available(now=time.monotonic())
    assert wait == float("inf")


def test_rate_limiter_is_enabled() -> None:
    """L218: is_enabled property returns True."""
    from caliber.rate_limit import RateLimiter

    limiter = RateLimiter(requests_per_minute=60.0, burst=10)
    assert limiter.is_enabled is True


def test_read_user_header_non_ascii() -> None:
    """L316–317: non-ASCII header value falls back."""
    from caliber.rate_limit import _read_user_header

    scope = {
        "headers": [
            (b"x-caliber-user", b"\xff\xfe"),
        ],
    }
    result = _read_user_header(scope, fallback_user="anon")
    assert result == "anon"


# ══════════════════════════════════════════════════════════════════════
# 7. apply.py — _build_checkpoint version string (moved from approvals route)
# ══════════════════════════════════════════════════════════════════════


def test_build_checkpoint_version_string() -> None:
    """version_after_raw is a digit string like '3'."""
    from caliber.apply import _build_checkpoint

    approval = MagicMock()
    approval.approval_id = "AP-VER"
    approval.agent_id = "test-agent"

    result_mock = MagicMock()
    result_mock.details = {"version": "3"}
    result_mock.artifact_ref = "prompts:/test-agent/3"

    checkpoint = _build_checkpoint(
        approval,
        {"content": "test", "rationale": "fix"},
        result_mock,
    )
    assert checkpoint.version_before == 2
    assert checkpoint.version_after == 3


# ══════════════════════════════════════════════════════════════════════
# 8. routes/workflows.py — duplicate ID, YAML errors
# ══════════════════════════════════════════════════════════════════════


def _seed_workflow(db_session: Session, wf_id: str = "WF-DUP", name: str = "Test WF") -> None:
    wf = CaliberWorkflow(
        workflow_id=wf_id,
        name=name,
        status="active",
    )
    db_session.add(wf)
    db_session.commit()


def test_create_workflow_duplicate_id(client: TestClient, db_session: Session) -> None:
    """L103: creating a workflow with an existing workflow_id returns 409."""
    _seed_workflow(db_session, "WF-DUP", "Original")
    r = client.post(
        f"{PREFIX}/workflows",
        json={"name": "Different Name", "workflow_id": "WF-DUP"},
    )
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]


def test_import_workflow_invalid_yaml(client: TestClient) -> None:
    """L285–286: import with unparseable YAML."""
    r = client.post(
        f"{PREFIX}/workflows/import",
        json={"manifest_yaml": "{{: bad yaml: ["},
    )
    assert r.status_code == 400
    assert "invalid manifest_yaml" in r.json()["detail"]


def test_import_workflow_yaml_non_dict(client: TestClient) -> None:
    """L288: import with YAML that decodes to a list, not a dict."""
    r = client.post(
        f"{PREFIX}/workflows/import",
        json={"manifest_yaml": "- item1\n- item2\n"},
    )
    assert r.status_code == 400
    assert "must decode to a mapping" in r.json()["detail"]


# ══════════════════════════════════════════════════════════════════════
# 9. routes/workflow_versions.py — compile errors
# ══════════════════════════════════════════════════════════════════════


def _seed_workflow_version(
    db_session: Session,
    wf_id: str,
    version_id: str,
    manifest: dict[str, Any] | None = None,
) -> None:
    from caliber.workflows.manifest import compute_manifest_hash

    wf = db_session.get(CaliberWorkflow, wf_id)
    if wf is None:
        wf = CaliberWorkflow(workflow_id=wf_id, name=f"WF {wf_id}", status="active")
        db_session.add(wf)
        db_session.flush()

    m = manifest or make_support_manifest(wf_id)
    version = CaliberWorkflowVersion(
        version_id=version_id,
        workflow_id=wf_id,
        version_number=1,
        status="draft",
        manifest=m,
        manifest_hash=compute_manifest_hash(m),
        created_by="@test",
    )
    db_session.add(version)
    db_session.commit()


def test_workflow_version_export_compile_error(client: TestClient, db_session: Session) -> None:
    """L483–484: export with a manifest that fails to compile."""
    # Seed a version with an invalid manifest that's stored raw
    _seed_workflow_version(
        db_session, "WF-EXP", "VV-EXP", manifest={"schema_version": 1, "bad": True}
    )
    r = client.get(f"{PREFIX}/workflow-versions/VV-EXP/export/python")
    assert r.status_code == 400
    assert "cannot export" in r.json().get("detail", "").lower()


# ══════════════════════════════════════════════════════════════════════
# 10. worker.py — tick exception path
# ══════════════════════════════════════════════════════════════════════


def test_worker_tick_exception_logged() -> None:
    """L180–181: _tick raising continues loop without crashing."""
    from caliber.orchestrator.worker import RefinementWorker

    worker = RefinementWorker(
        session_factory=MagicMock(),
        llm_provider=MagicMock(),
        artifact_store=MagicMock(),
        eval_provider=MagicMock(),
        interval_seconds=0.01,
    )

    call_count = 0
    loop: asyncio.AbstractEventLoop | None = None

    def _tick_raises():
        nonlocal call_count, loop
        call_count += 1
        if call_count <= 2:
            raise RuntimeError("boom")
        # After 2 ticks, signal stop from the event loop thread
        if loop is not None:
            loop.call_soon_threadsafe(worker._stopped.set)

    worker._tick = _tick_raises

    async def run_worker():
        nonlocal loop
        loop = asyncio.get_running_loop()
        await worker._run()

    asyncio.run(run_worker())
    assert call_count >= 2


# ══════════════════════════════════════════════════════════════════════
# 11. Additional small gaps
# ══════════════════════════════════════════════════════════════════════


def test_apply_empty_candidate_content(client: TestClient, db_session: Session) -> None:
    """Applying a candidate_ready job whose candidate has no content → 409."""
    from caliber.db.models import CaliberVerificationItem

    agent = CaliberAgentConfig(
        agent_id="agent-nocon",
        experiment_id="exp-nocon",
        name="Agent No Content",
        owner="@test",
        enabled=True,
    )
    db_session.merge(agent)
    db_session.flush()

    # Need a verification item for primary_item_id FK
    vi = CaliberVerificationItem(
        item_id="VI-NOCON",
        agent_id="agent-nocon",
        category="hallucination",
        free_text="test",
        severity="critical",
        status="verified",
    )
    db_session.merge(vi)
    db_session.flush()

    job_id = new_job_id()
    job = CaliberRefinementJob(
        job_id=job_id,
        agent_id="agent-nocon",
        primary_item_id="VI-NOCON",
        artifact_type="prompt",
        status="candidate_ready",
        current_stage="done",
        candidate={
            "artifact_type": "prompt",
            "content": None,
            "rationale": "fix",
        },
    )
    db_session.add(job)
    db_session.commit()

    r = client.post(f"{PREFIX}/jobs/{job_id}/apply")
    assert r.status_code == 409
    assert "no candidate content" in r.json()["detail"]


def test_workflows_import_both_manifest_and_yaml(client: TestClient) -> None:
    """Providing both manifest and manifest_yaml returns 400."""
    r = client.post(
        f"{PREFIX}/workflows/import",
        json={"manifest": {"a": 1}, "manifest_yaml": "a: 1"},
    )
    assert r.status_code == 400
    assert "exactly one" in r.json()["detail"]


def test_workflows_import_neither_manifest_nor_yaml(client: TestClient) -> None:
    """Providing neither manifest nor manifest_yaml returns 400."""
    r = client.post(
        f"{PREFIX}/workflows/import",
        json={},
    )
    assert r.status_code == 400
    assert "provide one" in r.json()["detail"].lower() or "manifest" in r.json()["detail"].lower()
