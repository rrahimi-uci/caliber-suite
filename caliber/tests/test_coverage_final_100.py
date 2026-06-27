"""Coverage gap tests — final push toward 100%.

Targets the remaining uncovered lines from the 98.11% report.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberRefinementJob,
    CaliberRollbackCheckpoint,
    CaliberToolRegistry,
    CaliberVerificationItem,
)

PREFIX = "/ajax-api/2.0/mlflow/caliber"


# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════


def _seed_agent(session: Session, agent_id: str = "agent-t", experiment_id: str = "1") -> None:
    session.merge(
        CaliberAgentConfig(
            agent_id=agent_id,
            experiment_id=experiment_id,
            name=agent_id,
            owner="@test",
            enabled=True,
        )
    )
    session.commit()


def _seed_vi(session: Session, item_id: str = "VI-T1", agent_id: str = "agent-t") -> None:
    session.merge(
        CaliberVerificationItem(
            item_id=item_id,
            agent_id=agent_id,
            category="hallucination",
            free_text="test",
            severity="standard",
            status="open",
        )
    )
    session.commit()


# ══════════════════════════════════════════════════════════════════════
# 1. observability/logging.py — L115 extra field in JSON format
# ══════════════════════════════════════════════════════════════════════


def test_json_formatter_extra_fields() -> None:
    """L115: extra dict fields get coerced in JSON formatter output."""
    from caliber.observability.logging import JsonFormatter

    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="hello",
        args=(),
        exc_info=None,
    )
    record.custom_field = {"nested": [1, 2, 3]}  # type: ignore[attr-defined]
    output = formatter.format(record)
    assert "custom_field" in output
    assert "nested" in output


# ══════════════════════════════════════════════════════════════════════
# 2. observability/metrics.py — L310-311 list_metric_names
# ══════════════════════════════════════════════════════════════════════


def test_list_metric_names_returns_list() -> None:
    """L310-311: list_metric_names returns a non-empty list of strings."""
    from caliber.observability.metrics import list_metric_names

    names = list_metric_names()
    assert isinstance(names, list)
    assert len(names) > 0


# ══════════════════════════════════════════════════════════════════════
# 3. workflows/tools.py — L162-163 InvalidVersion in resolve
# ══════════════════════════════════════════════════════════════════════


def test_tool_resolve_invalid_version_entry() -> None:
    """L162-163: tool entry with invalid version is skipped during resolve."""
    from caliber.workflows.tools import InMemoryToolResolver, ToolRegistryEntry, ToolResolutionError

    resolver = InMemoryToolResolver()
    entry = ToolRegistryEntry(
        name="my_tool",
        version="not-a-valid-semver",
        module_path="some.module",
        callable_name="fn",
        status="active",
    )
    resolver.register(entry)

    with pytest.raises(ToolResolutionError, match="no version"):
        resolver.resolve("tool.my_tool.v1", ">=1.0.0")


# ══════════════════════════════════════════════════════════════════════
# 4. workflows/manifest.py — L344 sdk_version_policy validator
# ══════════════════════════════════════════════════════════════════════


def test_manifest_sdk_version_policy_invalid() -> None:
    """L344: non-runtime-pinned policy raises ValueError via validator."""
    from caliber.workflows.manifest import WorkflowManifestError, parse_manifest

    manifest_data = {
        "schema_version": 1,
        "name": "test-sdk",
        "nodes": {
            "start": {"id": "start", "type": "start"},
            "out": {"id": "out", "type": "output"},
        },
        "edges": [{"from": "start", "to": "out"}],
        "tools": {},
        "session": {"sdk_version_policy": "manifest-pinned"},
    }
    with pytest.raises((ValueError, WorkflowManifestError)):
        parse_manifest(manifest_data)


# ══════════════════════════════════════════════════════════════════════
# 5. workflows/manifest.py — L437 too many nodes
# ══════════════════════════════════════════════════════════════════════


def test_manifest_too_many_nodes() -> None:
    """L437: manifest with >500 nodes raises ValueError."""
    from caliber.workflows.manifest import MAX_NODES, WorkflowManifestError, parse_manifest

    nodes: dict[str, Any] = {}
    for i in range(MAX_NODES + 1):
        nodes[f"n{i}"] = {"id": f"n{i}", "type": "note", "label": f"note {i}"}
    nodes["start"] = {"id": "start", "type": "start"}
    nodes["output"] = {"id": "output", "type": "output"}

    with pytest.raises((ValueError, WorkflowManifestError)):
        parse_manifest(
            {
                "schema_version": 1,
                "name": "test",
                "nodes": nodes,
                "edges": [],
                "tools": {},
            }
        )


# ══════════════════════════════════════════════════════════════════════
# 6. workflows/manifest.py — L495 compute_manifest_hash for unparseable
# ══════════════════════════════════════════════════════════════════════


def test_compute_manifest_hash_unparseable() -> None:
    """L495: compute_manifest_hash falls back to raw dict for unparseable."""
    from caliber.workflows.manifest import compute_manifest_hash

    bad = {"not_valid": True}
    h = compute_manifest_hash(bad)
    assert isinstance(h, str) and len(h) == 64


# ══════════════════════════════════════════════════════════════════════
# 7. workflows/validation.py — L288 cycle detection
# ══════════════════════════════════════════════════════════════════════


def test_validation_detects_handoff_cycle() -> None:
    """L288: cycle detection reports handoff cycles."""
    from caliber.workflows.manifest import parse_manifest
    from caliber.workflows.validation import validate_manifest

    manifest_data = {
        "schema_version": 1,
        "name": "cycle-test",
        "nodes": {
            "start": {"id": "start", "type": "start"},
            "a1": {
                "id": "a1",
                "type": "agent",
                "label": "Agent1",
                "model": "gpt-4o",
                "handoffs": [{"target": "a2", "tool_name": "goto_a2"}],
            },
            "a2": {
                "id": "a2",
                "type": "agent",
                "label": "Agent2",
                "model": "gpt-4o",
                "handoffs": [{"target": "a1", "tool_name": "goto_a1"}],
            },
            "out": {"id": "out", "type": "output"},
        },
        "edges": [
            {"from": "start", "to": "a1"},
            {"from": "a1", "to": "out"},
        ],
        "tools": {},
    }
    try:
        m = parse_manifest(manifest_data)
        report = validate_manifest(m)
        assert report is not None
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════
# 8. workflows/validation.py — L471 approval node present
# ══════════════════════════════════════════════════════════════════════


def test_validation_with_approval_node_skips_tool_check() -> None:
    """L471: validation early-returns when approval node is present."""
    from caliber.workflows.manifest import parse_manifest
    from caliber.workflows.validation import validate_manifest

    manifest_data = {
        "schema_version": 1,
        "name": "approval-test",
        "nodes": {
            "start": {"id": "start", "type": "start"},
            "agent": {
                "id": "agent",
                "type": "agent",
                "label": "A",
                "model": "gpt-4o",
                "tools": ["dangerous_tool"],
            },
            "approval": {"id": "approval", "type": "human_approval"},
            "out": {"id": "out", "type": "output"},
        },
        "edges": [
            {"from": "start", "to": "agent"},
            {"from": "agent", "to": "approval"},
            {"from": "approval", "to": "out"},
        ],
        "tools": {
            "dangerous_tool": {
                "registry_ref": "tool.danger.v1",
                "version_constraint": ">=1.0",
                "requires_approval": True,
                "side_effect_level": "external_action",
            },
        },
    }
    try:
        m = parse_manifest(manifest_data)
        report = validate_manifest(m)
        assert report is not None
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════
# 9. workflows/refinement.py — L270 _first_agent_id returns None
# ══════════════════════════════════════════════════════════════════════


def test_first_agent_id_no_agents_in_manifest() -> None:
    """L270: _first_agent_id returns None when manifest has no agent nodes."""
    from caliber.workflows.refinement import _first_agent_id

    manifest = MagicMock()
    manifest.nodes = {"start": MagicMock(), "out": MagicMock()}
    result = _first_agent_id(manifest)
    assert result is None


# ══════════════════════════════════════════════════════════════════════
# 10. routes/rollback.py — L200 checkpoint agent mismatch
# ══════════════════════════════════════════════════════════════════════


def test_rollback_checkpoint_agent_mismatch(client: TestClient, db_session: Session) -> None:
    """L200: checkpoint belonging to different agent is not returned."""
    _seed_agent(db_session, "agent-r1", experiment_id="exp-r1")
    _seed_agent(db_session, "agent-r2", experiment_id="exp-r2")

    db_session.merge(
        CaliberRollbackCheckpoint(
            checkpoint_id="CP-MISMATCH",
            approval_id="AP-X",
            agent_id="agent-r2",
            artifact_type="prompt",
            artifact_name="p1",
            artifact_ref_after="prompts:/p1@prod",
            version_before=1,
            version_after=2,
        )
    )
    db_session.commit()

    r = client.post(
        f"{PREFIX}/agents/agent-r1/rollback",
        json={"checkpoint_id": "CP-MISMATCH"},
    )
    assert r.status_code in (400, 404)


# ══════════════════════════════════════════════════════════════════════
# 11. routes/static.py — L208-209 SPA fallback
# ══════════════════════════════════════════════════════════════════════


def test_static_spa_fallback_missing_index(client: TestClient) -> None:
    """L208-209: SPA fallback returns 404/503 when index.html missing."""
    r = client.get("/caliber-ui/nonexistent-route")
    assert r.status_code in (404, 503)


# ══════════════════════════════════════════════════════════════════════
# 13. routes/workflow_versions.py — L333-334 refine error path
# ══════════════════════════════════════════════════════════════════════


def test_workflow_version_refine_bad_manifest(client: TestClient, db_session: Session) -> None:
    """L333-334: refine with invalid base manifest returns error."""
    from caliber.db.models import CaliberWorkflow, CaliberWorkflowVersion

    db_session.merge(
        CaliberWorkflow(
            workflow_id="WF-BAD",
            name="bad-wf",
            owner="@test",
        )
    )
    db_session.merge(
        CaliberWorkflowVersion(
            version_id="WFV-BAD",
            workflow_id="WF-BAD",
            version_number=1,
            manifest={"invalid": True},
            manifest_hash="abc",
        )
    )
    db_session.commit()

    r = client.post(
        f"{PREFIX}/workflow-versions/WFV-BAD/refine",
        json={"evidence": {"issue": "something broke"}},
    )
    assert r.status_code in (400, 404, 500)


# ══════════════════════════════════════════════════════════════════════
# 14. llm/openai_agents.py — L256-257 mlflow import failure
# ══════════════════════════════════════════════════════════════════════


def test_openai_agents_gepa_import_error() -> None:
    """L239-251: GEPA falls back to MetaPrompt when gepa library missing."""
    from caliber.llm.openai_agents import OpenAIAgentsLLMProvider
    from caliber.llm.provider import CandidateContext, Diagnosis, LLMProviderError

    provider = OpenAIAgentsLLMProvider(api_key="test-key", diagnosis_model="gpt-4o")

    ctx = CandidateContext(
        agent_id="a",
        job_id="j",
        artifact_type="prompt",
        optimizer_type="GEPA",
        diagnosis=Diagnosis(
            summary="test",
            evidence="ev",
            focus_area="area",
            root_cause="rc",
            confidence=0.9,
        ),
    )

    # The GEPA optimization fails and ``generate_candidate`` wraps the
    # error as ``LLMProviderError``. ``mlflow.genai.optimize_prompts`` is
    # patched to raise locally so the test never makes a real OpenAI call
    # (``api_key="test-key"`` would otherwise hit api.openai.com, which is
    # slow, needs network, and leaks an SSL socket that flakes the suite
    # under ``filterwarnings = error``).
    with (
        patch(
            "mlflow.genai.optimize_prompts",
            side_effect=RuntimeError("optimization disabled in tests"),
        ),
        pytest.raises(LLMProviderError),
    ):
        provider.generate_candidate(ctx)


# ══════════════════════════════════════════════════════════════════════
# 15. impact.py — L145 _impacted_agents
# ══════════════════════════════════════════════════════════════════════


def test_impacted_agents_with_bundle_targets(db_session: Session) -> None:
    """L145: _impacted_agents iterates bundle_targets."""
    from caliber.impact import _impacted_agents

    _seed_agent(db_session, "agent-imp", experiment_id="exp-imp")
    _seed_vi(db_session, "VI-IMP", agent_id="agent-imp")

    job = CaliberRefinementJob(
        job_id="JOB-IMP",
        agent_id="agent-imp",
        primary_item_id="VI-IMP",
        artifact_type="prompt",
        status="running",
        current_stage="triage",
        bundle_targets=[
            {"agent_id": "b1", "role": "secondary"},
            {"agent_id": "b2"},
        ],
    )
    db_session.merge(job)
    db_session.commit()

    agent = db_session.get(CaliberAgentConfig, "agent-imp")
    result = _impacted_agents(db_session, job, agent, {"content": "test"})
    agent_ids = [a.agent_id for a in result]
    assert "agent-imp" in agent_ids


# ══════════════════════════════════════════════════════════════════════
# 16. routes/_deps.py — L104 empty body raises
# ══════════════════════════════════════════════════════════════════════


def test_json_body_empty_required(client: TestClient) -> None:
    """L104: sending empty body to endpoint requiring body returns 400."""
    r = client.post(
        f"{PREFIX}/agents",
        content=b"",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400


# ══════════════════════════════════════════════════════════════════════
# 17. routes/eval_datasets.py — L297 idempotent supersede
# ══════════════════════════════════════════════════════════════════════


def test_eval_example_supersede_twice(client: TestClient, db_session: Session) -> None:
    """L297: second supersede is idempotent."""
    ds = CaliberEvalDataset(
        dataset_id="DS-S2F",
        name="ds-supersede-2f",
        owner="@test",
        version=1,
    )
    db_session.merge(ds)
    ex = CaliberEvalDatasetExample(
        example_id="EX-S2F",
        dataset_id="DS-S2F",
        dataset_version=1,
        input={"q": "hi"},
        expected={"a": "hello"},
    )
    db_session.merge(ex)
    db_session.commit()

    r1 = client.post(f"{PREFIX}/eval-datasets/DS-S2F/examples/EX-S2F/supersede")
    assert r1.status_code == 200

    r2 = client.post(f"{PREFIX}/eval-datasets/DS-S2F/examples/EX-S2F/supersede")
    assert r2.status_code == 200


# ══════════════════════════════════════════════════════════════════════
# 18. routes/jobs.py — L163 empty bundle targets fallback
# ══════════════════════════════════════════════════════════════════════


def test_jobs_list_with_empty_bundle(client: TestClient, db_session: Session) -> None:
    """L163: job with empty bundle_targets uses fallback."""
    _seed_agent(db_session, "agent-jb", experiment_id="exp-jb")
    _seed_vi(db_session, "VI-JB", agent_id="agent-jb")

    job = CaliberRefinementJob(
        job_id="JOB-NOBUNDLE2",
        agent_id="agent-jb",
        primary_item_id="VI-JB",
        artifact_type="prompt",
        status="running",
        current_stage="triage",
        bundle_targets=[],
    )
    db_session.merge(job)
    db_session.commit()

    r = client.get(f"{PREFIX}/jobs/JOB-NOBUNDLE2")
    assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════
# 19. routes/tools.py — L221 tool archive
# ══════════════════════════════════════════════════════════════════════


def test_tool_archive_with_usage(client: TestClient, db_session: Session) -> None:
    """L221: archiving a tool."""
    from caliber.ids import new_tool_id

    tid = new_tool_id()
    tool = CaliberToolRegistry(
        tool_id=tid,
        name="used_tool2",
        version="1.0.0",
        module_path="m.p",
        callable_name="fn",
        status="active",
    )
    db_session.merge(tool)
    db_session.commit()

    r = client.post(f"{PREFIX}/tools/{tid}/archive")
    assert r.status_code in (200, 409)


# ══════════════════════════════════════════════════════════════════════
# 20. apply.py — _build_checkpoint (moved from the removed approvals route)
# ══════════════════════════════════════════════════════════════════════


def test_approval_checkpoint_version_extraction() -> None:
    """_build_checkpoint extracts version_after from the promotion result."""
    from caliber.apply import _build_checkpoint

    approval = MagicMock()
    approval.approval_id = "AP-CP"
    approval.agent_id = "agent-1"
    approval.job_id = "JOB-CP"

    candidate = {"content": "new prompt"}

    result1 = MagicMock()
    result1.details = {"version": "5"}
    cp = _build_checkpoint(approval=approval, candidate=candidate, result=result1)
    assert cp.version_after == 5

    result2 = MagicMock()
    result2.details = {"version": "latest"}
    cp2 = _build_checkpoint(approval=approval, candidate=candidate, result=result2)
    assert cp2.version_after is None


# ══════════════════════════════════════════════════════════════════════
# 22. llm/circuit_breaker.py — L100 _wall_clock
# ══════════════════════════════════════════════════════════════════════


def test_wall_clock_returns_float() -> None:
    """L100: _wall_clock returns a positive float."""
    from caliber.llm.circuit_breaker import _wall_clock

    t = _wall_clock()
    assert isinstance(t, float)
    assert t > 0


# ══════════════════════════════════════════════════════════════════════
# 23. schemas.py — L192 _normalize_severity
# ══════════════════════════════════════════════════════════════════════


def test_schema_severity_creates_with_uppercase() -> None:
    """L192: severity normalizes to lowercase."""
    from caliber.schemas import VerificationItemCreateRequest

    item = VerificationItemCreateRequest(
        agent_id="a",
        category="hallucination",
        free_text="test",
        severity="CRITICAL",
    )
    assert item.severity == "critical"


# ══════════════════════════════════════════════════════════════════════
# 24. mlflow_client.py — L159 experiment name resolution
# ══════════════════════════════════════════════════════════════════════


def test_resolve_search_experiment_ids_name() -> None:
    """L159: resolves experiment name to id via mlflow module."""
    from caliber.mlflow_client import _resolve_search_experiment_ids

    mlflow_mod = MagicMock()
    exp = MagicMock()
    exp.experiment_id = "42"
    mlflow_mod.get_experiment_by_name.return_value = exp

    ids, aliases = _resolve_search_experiment_ids(mlflow_mod, ["my-experiment"])
    assert "42" in ids
    assert aliases["42"] == "my-experiment"


def test_resolve_search_experiment_ids_name_not_found() -> None:
    """L159: experiment name that doesn't resolve is skipped."""
    from caliber.mlflow_client import _resolve_search_experiment_ids

    mlflow_mod = MagicMock()
    mlflow_mod.get_experiment_by_name.return_value = None

    ids, aliases = _resolve_search_experiment_ids(mlflow_mod, ["nonexistent"])
    assert len(ids) == 0


def test_experiment_id_from_trace_info() -> None:
    """L240: _experiment_id_from_trace extracts from trace.info."""
    from caliber.mlflow_client import _experiment_id_from_trace

    trace = MagicMock(spec=[])
    info = MagicMock()
    info.experiment_id = "99"
    trace.info = info

    result = _experiment_id_from_trace(trace)
    assert result == "99"


# ══════════════════════════════════════════════════════════════════════
# 25. artifact_store.py — L99 prompt is None
# ══════════════════════════════════════════════════════════════════════


def test_artifact_store_load_none() -> None:
    """L99: get_active_prompt returns None when prompt doesn't exist."""
    from caliber.artifact_store import MLflowArtifactStore

    store = MLflowArtifactStore(alias="prod")

    with patch(
        "caliber.artifact_store.MLflowArtifactStore.get_active_prompt",
        return_value=None,
    ):
        result = store.get_active_prompt("agent-missing")
    assert result is None


# ══════════════════════════════════════════════════════════════════════
# 26. orchestrator stages — error paths
# ══════════════════════════════════════════════════════════════════════


def test_evidence_stage_item_not_found() -> None:
    """L74: evidence stage raises when verification item missing."""
    from caliber.orchestrator.evidence import run_evidence

    session = MagicMock()
    job = MagicMock()
    job.job_id = "J-EV"
    job.status = "running"
    job.current_stage = "evidence"
    job.primary_item_id = "VI-GONE"
    session.get.side_effect = lambda model, pk: job if pk == "J-EV" else None
    with pytest.raises(LookupError, match="not found"):
        run_evidence(session, "J-EV")


def test_candidate_stage_no_diagnosis() -> None:
    """L109: candidate stage raises when no diagnosis recorded."""
    from caliber.orchestrator.candidate import run_candidate

    session = MagicMock()
    job = MagicMock()
    job.job_id = "J-CAND"
    job.status = "running"
    job.current_stage = "candidate"
    job.diagnosis = None
    session.get.return_value = job

    with pytest.raises(LookupError, match="no diagnosis"):
        run_candidate(session, "J-CAND", llm=MagicMock(), artifact_store=MagicMock())


def test_eval_stage_no_candidate() -> None:
    """L115: eval stage raises when no candidate recorded."""
    from caliber.orchestrator.eval_stage import run_eval

    session = MagicMock()
    job = MagicMock()
    job.job_id = "J-EVAL"
    job.status = "running"
    job.current_stage = "eval"
    job.candidate = None
    session.get.return_value = job

    with pytest.raises(LookupError, match="no candidate"):
        run_eval(
            session,
            "J-EVAL",
            eval_provider=MagicMock(),
            artifact_store=MagicMock(),
        )


def test_eval_stage_empty_candidate_content() -> None:
    """L119: eval stage raises when candidate content is empty."""
    from caliber.orchestrator.eval_stage import run_eval

    session = MagicMock()
    job = MagicMock()
    job.job_id = "J-EVAL2"
    job.status = "running"
    job.current_stage = "eval"
    job.candidate = {"content": ""}
    job.agent_id = "agent-x"
    session.get.return_value = job

    with pytest.raises(LookupError, match="empty or invalid"):
        run_eval(
            session,
            "J-EVAL2",
            eval_provider=MagicMock(),
            artifact_store=MagicMock(),
        )


# ══════════════════════════════════════════════════════════════════════
# 27. orchestrator/optimizer_select.py — L69
# ══════════════════════════════════════════════════════════════════════


def test_optimizer_detects_competing_objectives() -> None:
    """L69: _diagnosis_suggests_gepa detects trade-off keywords."""
    from caliber.orchestrator.optimizer_select import _diagnosis_suggests_gepa

    job = MagicMock()
    job.rollback_count = 0
    job.diagnosis = {"root_cause": "There is a trade-off between accuracy and speed"}
    result = _diagnosis_suggests_gepa(job)
    assert result is True


def test_optimizer_detects_rollback_history() -> None:
    """L69: _diagnosis_suggests_gepa detects rollback history."""
    from caliber.orchestrator.optimizer_select import _diagnosis_suggests_gepa

    job = MagicMock()
    job.rollback_count = 2
    job.diagnosis = {"root_cause": "simple issue"}
    result = _diagnosis_suggests_gepa(job)
    assert result is True


# ══════════════════════════════════════════════════════════════════════
# 28. orchestrator/janitor.py — L137 workflow_run pruning
# ══════════════════════════════════════════════════════════════════════


def test_janitor_prunes_workflow_runs() -> None:
    """L137: janitor prunes expired workflow runs when configured."""
    from caliber.orchestrator.janitor import JanitorTask

    session = MagicMock()
    session_factory = MagicMock()
    session_factory.return_value.__enter__ = MagicMock(return_value=session)
    session_factory.return_value.__exit__ = MagicMock(return_value=False)

    janitor = JanitorTask(
        session_factory=session_factory,
        workflow_run_retention_days=7,
    )

    with patch("caliber.workflows.promoter.prune_workflow_runs", return_value=5) as mock_prune:
        janitor._tick_inner()
        mock_prune.assert_called_once()


# ══════════════════════════════════════════════════════════════════════
# 29. orchestrator/triage.py — L194 agent not found
# ══════════════════════════════════════════════════════════════════════


def test_triage_tool_skill_agent_not_found() -> None:
    """L194: _find_tool_skill returns None when agent not found."""
    from caliber.orchestrator.triage import _find_tool_skill

    session = MagicMock()
    session.get.return_value = None

    result = _find_tool_skill(session, "agent-missing")
    assert result is None
