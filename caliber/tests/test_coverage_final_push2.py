"""Final coverage push — targets remaining testable uncovered statements.

Uncovered lines targeted:
- rollback.py L200: _select_checkpoint returns None when checkpoint_id not found
- eval_datasets.py L297: supersede with non-existent dataset -> 404
- workflow_versions.py L333-334: patch generation raises CompileError -> 400
- workflow_stages.py L328: deploy_gates thresholds iteration
- openai_agents.py L256-257: mlflow.genai import failure -> LLMProviderError
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberWorkflow,
    CaliberWorkflowVersion,
)

PREFIX = "/ajax-api/2.0/mlflow/caliber"


# ---------------------------------------------------------------------------
# 1. rollback.py L200 — _select_checkpoint returns None for missing checkpoint
# ---------------------------------------------------------------------------


def test_select_checkpoint_returns_none_for_missing(db_session: Session) -> None:
    """L200: checkpoint_id not found in DB -> return None."""
    from caliber.routes.rollback import _select_checkpoint

    result = _select_checkpoint(db_session, "agent-x", "NONEXISTENT-CHECKPOINT")
    assert result is None


# ---------------------------------------------------------------------------
# 2. eval_datasets.py L297 — supersede with non-existent dataset -> 404
# ---------------------------------------------------------------------------


def test_supersede_nonexistent_dataset_404(client: TestClient) -> None:
    r = client.post(f"{PREFIX}/eval-datasets/NO-SUCH-DS/examples/EX-1/supersede")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 6. workflow_versions.py L333-334 — patch generation raises error -> 400
# ---------------------------------------------------------------------------


def test_propose_patch_compile_error_400(client: TestClient, db_session: Session) -> None:
    """L333-334: generate_workflow_patch raises CompileError -> 400."""
    wf = CaliberWorkflow(
        workflow_id="WF-PATCH-ERR",
        name="test-patch-err",
        description="test",
        owner="@test",
    )
    db_session.add(wf)

    version = CaliberWorkflowVersion(
        version_id="WFV-PATCH-ERR",
        workflow_id="WF-PATCH-ERR",
        version_number=1,
        status="published",
        manifest={
            "schema_version": 1,
            "nodes": {
                "agent-a": {
                    "kind": "agent",
                    "name": "Agent A",
                    "model": "gpt-4o",
                    "instructions": {"type": "inline", "text": "You are A."},
                }
            },
            "edges": [],
            "tools": {},
            "artifacts": {"prompts": {}, "eval_datasets": {}},
            "deploy_gates": {},
        },
        manifest_hash="hash-patch-err",
        created_by="@test",
    )
    db_session.add(version)
    db_session.commit()

    from caliber.workflows.compiler import CompileError

    with (
        patch(
            "caliber.routes.workflow_versions.parse_manifest",
            return_value=MagicMock(),
        ),
        patch(
            "caliber.routes.workflow_versions.generate_workflow_patch",
            side_effect=CompileError("bad graph"),
        ),
    ):
        r = client.post(
            f"{PREFIX}/workflow-versions/WFV-PATCH-ERR/propose-patch",
            json={
                "evidence": {"node_id": "agent-a", "error": "test"},
            },
        )
    assert r.status_code == 400
    assert "cannot generate patch" in r.json().get("detail", r.text)


# ---------------------------------------------------------------------------
# 7. openai_agents.py L256-257 — mlflow.genai import failure
# ---------------------------------------------------------------------------


def test_gepa_mlflow_genai_import_error() -> None:
    """L256-257: when mlflow.genai.scorers import fails, raise LLMProviderError."""
    import builtins

    from caliber.llm.openai_agents import OpenAIAgentsLLMProvider
    from caliber.llm.provider import LLMProviderError

    provider = OpenAIAgentsLLMProvider.__new__(OpenAIAgentsLLMProvider)
    provider._model = "gpt-4o"
    provider._api_key = "test-key"
    provider._candidate_agent = None
    provider._diagnosis_agent = None

    ctx = MagicMock()
    ctx.job_id = "job-test"
    ctx.artifact_content = "You are a helpful assistant."
    ctx.current_artifact_content = "You are a helpful assistant."
    ctx.artifact_type = "prompt"
    ctx.verification_items = [{"text": "bad answer"}]
    ctx.previous_diagnosis = "hallucination detected"
    ctx.optimizer_type = "GEPA"
    ctx.skill_name = None
    ctx.agent_id = "test-agent"

    original_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        # Let GepaPromptOptimizer import succeed (first try block)
        if name == "mlflow.genai.optimize.optimizers":
            m = MagicMock()
            return m
        # Fail the second import for mlflow.genai.scorers
        if name == "mlflow.genai.scorers":
            raise ImportError("no genai scorers")
        if name == "mlflow" and any("mlflow.genai" in str(a) for a in args):
            raise ImportError("no genai")
        return original_import(name, *args, **kwargs)

    with patch.object(builtins, "__import__", side_effect=mock_import):
        with pytest.raises(LLMProviderError, match="mlflow\\[genai\\]"):
            provider.generate_candidate(ctx)
