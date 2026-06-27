"""Tests for the candidate-generation stage of the refinement pipeline."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.artifact_store import FakeArtifactStore
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberAuditLog,
    CaliberRefinementJob,
    CaliberVerificationItem,
)
from caliber.llm.fake import FakeLLMProvider
from caliber.llm.provider import (
    CandidateContext,
    LLMProviderError,
    LLMUsage,
    PromptCandidate,
)
from caliber.orchestrator.candidate import CandidateStateError, run_candidate


def _seed_job(
    session: Session,
    *,
    status: str = "running",
    stage: str = "candidate",
    diagnosis: dict[str, object] | None = None,
    optimizer_config: dict[str, object] | None = None,
    initial_tokens: int = 0,
    initial_cost: float = 0.0,
) -> CaliberRefinementJob:
    session.add(
        CaliberAgentConfig(
            agent_id="support-agent",
            experiment_id="exp",
            name="Support",
            owner="@sarah",
            artifact_types=["prompt"],
            eval_thresholds={},
            optimizer_config=optimizer_config or {},
            approval_policy={},
        )
    )
    session.flush()
    session.add(
        CaliberVerificationItem(
            item_id="FB-C",
            agent_id="support-agent",
            category="hallucination",
            free_text="Agent invented refund timeline.",
            severity="critical",
            status="verified",
        )
    )
    session.flush()
    job = CaliberRefinementJob(
        job_id="RFN-C",
        agent_id="support-agent",
        primary_item_id="FB-C",
        artifact_type="prompt",
        status=status,
        current_stage=stage,
        bundle_targets=[],
        diagnosis=diagnosis
        or {
            "root_cause": "Prompt allows skipping lookup_policy.",
            "affected_components": ["prompt"],
            "confidence": 0.85,
            "alternatives": [],
        },
        total_tokens=initial_tokens,
        cost_usd=initial_cost,
    )
    session.add(job)
    session.commit()
    return job


def test_run_candidate_advances_to_eval(db_session: Session) -> None:
    _seed_job(db_session)
    job = run_candidate(db_session, "RFN-C", FakeLLMProvider(), FakeArtifactStore())

    assert job.status == "running"
    assert job.current_stage == "eval"
    assert job.optimizer_type == "MetaPrompt"


def test_run_candidate_persists_candidate_payload(db_session: Session) -> None:
    _seed_job(db_session)
    canned = PromptCandidate(
        artifact_type="prompt",
        content="rewritten prompt body",
        rationale="addresses tool-skip pattern",
        diff_summary="+5 / -3 lines",
    )
    provider = FakeLLMProvider(candidate_response=canned)
    run_candidate(db_session, "RFN-C", provider, FakeArtifactStore())

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-C")
    assert job is not None
    assert job.candidate == {
        "artifact_type": "prompt",
        "content": "rewritten prompt body",
        "rationale": "addresses tool-skip pattern",
        "diff_summary": "+5 / -3 lines",
    }


def test_run_candidate_accumulates_tokens_and_cost(db_session: Session) -> None:
    _seed_job(db_session, initial_tokens=100, initial_cost=0.005)
    provider = FakeLLMProvider(
        candidate_usage=LLMUsage(input_tokens=400, output_tokens=120, cost_usd=0.003)
    )
    run_candidate(db_session, "RFN-C", provider, FakeArtifactStore())

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-C")
    assert job is not None
    assert job.total_tokens == 100 + 400 + 120
    assert job.cost_usd == pytest.approx(0.005 + 0.003)


def test_run_candidate_passes_current_artifact(db_session: Session) -> None:
    """The artifact store's value flows into the context the LLM sees."""
    _seed_job(db_session)
    store = FakeArtifactStore({"support-agent": "active prompt v4.1 text"})
    provider = FakeLLMProvider()
    run_candidate(db_session, "RFN-C", provider, store)

    assert len(provider.candidate_calls) == 1
    ctx: CandidateContext = provider.candidate_calls[0]
    assert ctx.current_artifact_content == "active prompt v4.1 text"
    assert ctx.optimizer_type == "MetaPrompt"


def test_run_candidate_handles_missing_artifact(db_session: Session) -> None:
    """An empty artifact store yields a None current_artifact_content."""
    _seed_job(db_session)
    provider = FakeLLMProvider()
    run_candidate(db_session, "RFN-C", provider, FakeArtifactStore())

    ctx = provider.candidate_calls[0]
    assert ctx.current_artifact_content is None


def test_run_candidate_writes_audit_row(db_session: Session) -> None:
    _seed_job(db_session)
    run_candidate(db_session, "RFN-C", FakeLLMProvider(), FakeArtifactStore(), actor="@reza")

    rows = (
        db_session.execute(select(CaliberAuditLog).where(CaliberAuditLog.entity_id == "RFN-C"))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    log = rows[0]
    assert log.actor == "@reza"
    assert log.action == "advance_stage"
    assert log.details is not None
    assert log.details["from_stage"] == "candidate"
    assert log.details["to_stage"] == "eval"
    assert log.details["optimizer_type"] == "MetaPrompt"
    assert "usage" in log.details


def test_run_candidate_uses_agent_optimizer_override(db_session: Session) -> None:
    """An explicit optimizer override on agent_config flows through to the job row."""
    _seed_job(db_session, optimizer_config={"type": "GEPA"})

    # GEPA isn't implemented in the production provider, but the fake ignores
    # optimizer_type — we're just verifying that select_optimizer + persistence
    # respect the override.
    job = run_candidate(db_session, "RFN-C", FakeLLMProvider(), FakeArtifactStore())
    assert job.optimizer_type == "GEPA"


def test_run_candidate_rejects_unknown_job(db_session: Session) -> None:
    with pytest.raises(LookupError):
        run_candidate(db_session, "RFN-NONE", FakeLLMProvider(), FakeArtifactStore())


def test_run_candidate_rejects_missing_diagnosis(db_session: Session) -> None:
    _seed_job(db_session, diagnosis=None)
    # Trying to seed with diagnosis=None bypasses our helper's default — set
    # it explicitly post-seed:
    job = db_session.get(CaliberRefinementJob, "RFN-C")
    assert job is not None
    job.diagnosis = None
    db_session.commit()

    with pytest.raises(LookupError, match=r"no diagnosis"):
        run_candidate(db_session, "RFN-C", FakeLLMProvider(), FakeArtifactStore())


def test_run_candidate_rejects_queued_job(db_session: Session) -> None:
    _seed_job(db_session, status="queued", stage="triage")
    with pytest.raises(CandidateStateError):
        run_candidate(db_session, "RFN-C", FakeLLMProvider(), FakeArtifactStore())


def test_run_candidate_propagates_llm_provider_error(db_session: Session) -> None:
    _seed_job(db_session)

    def boom(_ctx: object) -> tuple[PromptCandidate, LLMUsage]:
        raise LLMProviderError("simulated rate limit")

    provider = FakeLLMProvider(candidate_callable=boom)
    with pytest.raises(LLMProviderError, match=r"rate limit"):
        run_candidate(db_session, "RFN-C", provider, FakeArtifactStore())

    # State machine untouched.
    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-C")
    assert job is not None
    assert job.current_stage == "candidate"
    assert job.candidate is None


def test_run_candidate_registers_draft_prompt_when_job_has_mlflow_run_id(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_job(db_session)
    job = db_session.get(CaliberRefinementJob, "RFN-C")
    assert job is not None
    job.mlflow_run_id = "run-123"
    db_session.commit()

    calls: list[dict[str, object]] = []

    def _register_prompt(**kwargs: object) -> object:
        calls.append(kwargs)
        return SimpleNamespace(uri="prompts:/support-agent/12", version=12)

    fake_mlflow = SimpleNamespace(genai=SimpleNamespace(register_prompt=_register_prompt))
    monkeypatch.setattr("caliber.orchestrator.candidate._import_mlflow", lambda: fake_mlflow)

    run_candidate(db_session, "RFN-C", FakeLLMProvider(), FakeArtifactStore())

    assert len(calls) == 1
    payload = calls[0]
    assert payload["name"] == "support-agent"
    assert payload["commit_message"] == "CALIBER candidate draft for job RFN-C"
    tags = payload["tags"]
    assert isinstance(tags, dict)
    assert tags["caliber.mlflow_run_id"] == "run-123"
    assert tags["caliber.review_status"] == "draft"

    audit_row = (
        db_session.execute(select(CaliberAuditLog).where(CaliberAuditLog.entity_id == "RFN-C"))
        .scalars()
        .one()
    )
    assert audit_row.details is not None
    assert audit_row.details["mlflow_candidate_prompt_ref"] == "prompts:/support-agent/12"


def test_run_candidate_skips_draft_prompt_registration_without_mlflow_run_id(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_job(db_session)

    def _should_not_import() -> object:
        raise AssertionError("mlflow should not be imported without job.mlflow_run_id")

    monkeypatch.setattr("caliber.orchestrator.candidate._import_mlflow", _should_not_import)

    run_candidate(db_session, "RFN-C", FakeLLMProvider(), FakeArtifactStore())

    audit_row = (
        db_session.execute(select(CaliberAuditLog).where(CaliberAuditLog.entity_id == "RFN-C"))
        .scalars()
        .one()
    )
    assert audit_row.details is not None
    assert audit_row.details["mlflow_candidate_prompt_ref"] is None


def test_run_candidate_continues_when_draft_prompt_registration_fails(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_job(db_session)
    job = db_session.get(CaliberRefinementJob, "RFN-C")
    assert job is not None
    job.mlflow_run_id = "run-456"
    db_session.commit()

    def _register_prompt(**_kwargs: object) -> object:
        raise RuntimeError("registry offline")

    fake_mlflow = SimpleNamespace(genai=SimpleNamespace(register_prompt=_register_prompt))
    monkeypatch.setattr("caliber.orchestrator.candidate._import_mlflow", lambda: fake_mlflow)

    result = run_candidate(db_session, "RFN-C", FakeLLMProvider(), FakeArtifactStore())
    assert result.current_stage == "eval"

    audit_row = (
        db_session.execute(select(CaliberAuditLog).where(CaliberAuditLog.entity_id == "RFN-C"))
        .scalars()
        .one()
    )
    assert audit_row.details is not None
    assert audit_row.details["mlflow_candidate_prompt_ref"] is None
