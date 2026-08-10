"""Tests for GEPA (Genetic-Pareto) optimizer integration.

Covers:
- GEPA selection logic in ``optimizer_select.py``
- GEPA-aware CandidateContext fields
- GEPA dispatch in FakeLLMProvider
- GEPA candidate generation pipeline
- GEPA config fields
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from caliber.artifact_store import FakeArtifactStore
from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberRefinementJob,
    CaliberSkill,
    CaliberVerificationItem,
)
from caliber.llm.fake import FakeLLMProvider
from caliber.llm.provider import (
    CandidateContext,
    Diagnosis,
    PromptCandidate,
)
from caliber.orchestrator.candidate import run_candidate
from caliber.orchestrator.optimizer_select import (
    _diagnosis_suggests_gepa,
    select_optimizer,
)

# ───────────────────── Fixtures / helpers ─────────────────────

# db_session is provided by conftest.py — no local override needed.


def _make_agent(
    session: Session,
    agent_id: str = "support-agent",
    skills: list[str] | None = None,
    optimizer_config: dict | None = None,
) -> CaliberAgentConfig:
    agent = CaliberAgentConfig(
        agent_id=agent_id,
        experiment_id="exp-1",
        name="Support Agent",
        owner="@test",
        artifact_types=["prompt"],
        eval_thresholds={},
        optimizer_config=optimizer_config or {"skills": skills or []},
        approval_policy={},
    )
    session.add(agent)
    session.flush()
    return agent


def _make_item(session: Session) -> CaliberVerificationItem:
    item = CaliberVerificationItem(
        item_id="FB-GEPA",
        agent_id="support-agent",
        category="quality",
        free_text="Response quality degraded",
        severity="critical",
        status="verified",
    )
    session.add(item)
    session.flush()
    return item


def _make_job(
    session: Session,
    *,
    artifact_type: str = "prompt",
    diagnosis: dict | None = None,
    skill_name: str | None = None,
    status: str = "running",
    stage: str = "candidate",
) -> CaliberRefinementJob:
    job = CaliberRefinementJob(
        job_id="RFN-GEPA",
        agent_id="support-agent",
        primary_item_id="FB-GEPA",
        artifact_type=artifact_type,
        status=status,
        current_stage=stage,
        bundle_targets=[],
        skill_name=skill_name,
    )
    if diagnosis:
        job.diagnosis = diagnosis
    session.add(job)
    session.commit()
    return job


def _make_skill(
    session: Session,
    name: str = "tool-use",
    content: str = "Use tools carefully.",
) -> CaliberSkill:
    skill = CaliberSkill(
        skill_id=f"SKL-{name}",
        name=name,
        content=content,
        version=1,
        status="active",
        allowed_tools="Bash(python:*) WebFetch",
        owner="@test",
    )
    session.add(skill)
    session.flush()
    return skill


# ═══════════════════════════════════════════════════════════════
# 1. GEPA Config fields
# ═══════════════════════════════════════════════════════════════


class TestGEPAConfig:
    """CaliberConfig exposes GEPA-specific knobs."""

    def test_defaults(self) -> None:
        cfg = CaliberConfig()
        assert cfg.gepa_reflection_model == "gpt-4o"
        assert cfg.gepa_max_metric_calls == 100

    def test_custom_values(self) -> None:
        cfg = CaliberConfig(
            gepa_reflection_model="openai:/gpt-4o-mini",
            gepa_max_metric_calls=50,
        )
        assert cfg.gepa_reflection_model == "openai:/gpt-4o-mini"
        assert cfg.gepa_max_metric_calls == 50

    def test_max_metric_calls_minimum(self) -> None:
        with pytest.raises(Exception):  # Pydantic validation
            CaliberConfig(gepa_max_metric_calls=5)


# ═══════════════════════════════════════════════════════════════
# 2. GEPA selection logic — _diagnosis_suggests_gepa
# ═══════════════════════════════════════════════════════════════


class TestDiagnosisSuggestsGEPA:
    """Low-level GEPA heuristic tests."""

    def test_low_confidence_triggers_gepa(self, db_session: Session) -> None:
        _make_agent(db_session)
        _make_item(db_session)
        job = _make_job(
            db_session,
            diagnosis={
                "root_cause": "Unclear issue",
                "confidence": 0.5,
                "alternatives": [],
                "affected_components": [],
            },
        )
        assert _diagnosis_suggests_gepa(job) is True

    def test_high_confidence_does_not_trigger(self, db_session: Session) -> None:
        _make_agent(db_session)
        _make_item(db_session)
        job = _make_job(
            db_session,
            diagnosis={
                "root_cause": "Clear issue",
                "confidence": 0.9,
                "alternatives": [],
                "affected_components": [],
            },
        )
        assert _diagnosis_suggests_gepa(job) is False

    def test_borderline_confidence_does_not_trigger(self, db_session: Session) -> None:
        _make_agent(db_session)
        _make_item(db_session)
        job = _make_job(
            db_session,
            diagnosis={
                "root_cause": "Clear-ish",
                "confidence": 0.7,
                "alternatives": [],
                "affected_components": [],
            },
        )
        assert _diagnosis_suggests_gepa(job) is False

    def test_many_alternatives_triggers_gepa(self, db_session: Session) -> None:
        _make_agent(db_session)
        _make_item(db_session)
        job = _make_job(
            db_session,
            diagnosis={
                "root_cause": "Multiple possible causes",
                "confidence": 0.8,
                "alternatives": ["alt1", "alt2", "alt3"],
                "affected_components": [],
            },
        )
        assert _diagnosis_suggests_gepa(job) is True

    def test_few_alternatives_does_not_trigger(self, db_session: Session) -> None:
        _make_agent(db_session)
        _make_item(db_session)
        job = _make_job(
            db_session,
            diagnosis={
                "root_cause": "Some issue",
                "confidence": 0.8,
                "alternatives": ["alt1", "alt2"],
                "affected_components": [],
            },
        )
        assert _diagnosis_suggests_gepa(job) is False

    def test_competing_objectives_in_root_cause(self, db_session: Session) -> None:
        _make_agent(db_session)
        _make_item(db_session)
        job = _make_job(
            db_session,
            diagnosis={
                "root_cause": "Competing objectives between brevity and detail",
                "confidence": 0.9,
                "alternatives": [],
                "affected_components": [],
            },
        )
        assert _diagnosis_suggests_gepa(job) is True

    def test_tradeoff_keyword_in_alternatives(self, db_session: Session) -> None:
        _make_agent(db_session)
        _make_item(db_session)
        job = _make_job(
            db_session,
            diagnosis={
                "root_cause": "Length issue",
                "confidence": 0.9,
                "alternatives": ["Possible tradeoff with empathy"],
                "affected_components": [],
            },
        )
        assert _diagnosis_suggests_gepa(job) is True

    def test_pareto_keyword_triggers(self, db_session: Session) -> None:
        _make_agent(db_session)
        _make_item(db_session)
        job = _make_job(
            db_session,
            diagnosis={
                "root_cause": "Pareto frontier between safety and helpfulness",
                "confidence": 0.9,
                "alternatives": [],
                "affected_components": [],
            },
        )
        assert _diagnosis_suggests_gepa(job) is True

    def test_no_diagnosis_returns_false(self, db_session: Session) -> None:
        _make_agent(db_session)
        _make_item(db_session)
        job = _make_job(db_session, diagnosis=None)
        assert _diagnosis_suggests_gepa(job) is False

    def test_empty_diagnosis_returns_false(self, db_session: Session) -> None:
        _make_agent(db_session)
        _make_item(db_session)
        job = _make_job(db_session, diagnosis={})
        assert _diagnosis_suggests_gepa(job) is False


# ═══════════════════════════════════════════════════════════════
# 3. select_optimizer — GEPA routing for prompts and skills
# ═══════════════════════════════════════════════════════════════


class TestSelectOptimizerGEPA:
    """GEPA is selected appropriately for both prompt and skill jobs."""

    def test_low_confidence_prompt_selects_gepa(self, db_session: Session) -> None:
        agent = _make_agent(db_session)
        _make_item(db_session)
        job = _make_job(
            db_session,
            artifact_type="prompt",
            diagnosis={
                "root_cause": "Unclear",
                "confidence": 0.4,
                "alternatives": [],
                "affected_components": [],
            },
        )
        assert select_optimizer(agent, job) == "GEPA"

    def test_low_confidence_skill_selects_gepa(self, db_session: Session) -> None:
        """GEPA applies to skill jobs too, not just prompts."""
        agent = _make_agent(db_session, skills=["tool-use"])
        _make_item(db_session)
        job = _make_job(
            db_session,
            artifact_type="skill",
            skill_name="tool-use",
            diagnosis={
                "root_cause": "Ambiguous tool format",
                "confidence": 0.5,
                "alternatives": ["format A", "format B", "format C"],
                "affected_components": ["skill"],
            },
        )
        assert select_optimizer(agent, job) == "GEPA"

    def test_high_confidence_prompt_selects_metaprompt(self, db_session: Session) -> None:
        agent = _make_agent(db_session)
        _make_item(db_session)
        job = _make_job(
            db_session,
            artifact_type="prompt",
            diagnosis={
                "root_cause": "Clear issue",
                "confidence": 0.9,
                "alternatives": [],
                "affected_components": [],
            },
        )
        assert select_optimizer(agent, job) == "MetaPrompt"

    def test_high_confidence_skill_selects_skill_meta_prompt(self, db_session: Session) -> None:
        agent = _make_agent(db_session, skills=["tool-use"])
        _make_item(db_session)
        job = _make_job(
            db_session,
            artifact_type="skill",
            skill_name="tool-use",
            diagnosis={
                "root_cause": "Clear skill issue",
                "confidence": 0.9,
                "alternatives": [],
                "affected_components": ["skill"],
            },
        )
        assert select_optimizer(agent, job) == "SkillMetaPrompt"

    def test_explicit_override_wins_over_gepa(self, db_session: Session) -> None:
        """Operator override always wins, even when GEPA criteria match."""
        agent = _make_agent(db_session, optimizer_config={"type": "MetaPrompt", "skills": []})
        _make_item(db_session)
        job = _make_job(
            db_session,
            artifact_type="prompt",
            diagnosis={
                "root_cause": "Unclear — competing objectives",
                "confidence": 0.3,
                "alternatives": ["a", "b", "c"],
                "affected_components": [],
            },
        )
        assert select_optimizer(agent, job) == "MetaPrompt"

    def test_explicit_gepa_override(self, db_session: Session) -> None:
        """Operator can force GEPA even when criteria don't match."""
        agent = _make_agent(db_session, optimizer_config={"type": "GEPA", "skills": []})
        _make_item(db_session)
        job = _make_job(
            db_session,
            artifact_type="prompt",
            diagnosis={
                "root_cause": "Crystal clear issue",
                "confidence": 0.99,
                "alternatives": [],
                "affected_components": [],
            },
        )
        assert select_optimizer(agent, job) == "GEPA"

    def test_competing_objectives_prompt_selects_gepa(self, db_session: Session) -> None:
        agent = _make_agent(db_session)
        _make_item(db_session)
        job = _make_job(
            db_session,
            artifact_type="prompt",
            diagnosis={
                "root_cause": "Competing objectives between conciseness and detail",
                "confidence": 0.85,
                "alternatives": [],
                "affected_components": [],
            },
        )
        assert select_optimizer(agent, job) == "GEPA"


# ═══════════════════════════════════════════════════════════════
# 4. CandidateContext — GEPA fields
# ═══════════════════════════════════════════════════════════════


class TestCandidateContextGEPAFields:
    """CandidateContext carries GEPA-specific fields."""

    def test_gepa_fields_default_to_none(self) -> None:
        ctx = CandidateContext(
            agent_id="a",
            job_id="j",
            artifact_type="prompt",
            optimizer_type="MetaPrompt",
            diagnosis=Diagnosis(
                root_cause="test",
                affected_components=[],
                confidence=0.9,
                alternatives=[],
            ),
        )
        assert ctx.pareto_dims is None
        assert ctx.population_size is None
        assert ctx.generations is None

    def test_gepa_fields_populated(self) -> None:
        ctx = CandidateContext(
            agent_id="a",
            job_id="j",
            artifact_type="prompt",
            optimizer_type="GEPA",
            diagnosis=Diagnosis(
                root_cause="trade-off",
                affected_components=[],
                confidence=0.5,
                alternatives=["a", "b", "c"],
            ),
            pareto_dims=["quality", "safety", "length"],
            population_size=12,
            generations=5,
        )
        assert ctx.pareto_dims == ["quality", "safety", "length"]
        assert ctx.population_size == 12
        assert ctx.generations == 5

    def test_gepa_fields_with_skill(self) -> None:
        """GEPA fields coexist with skill fields."""
        ctx = CandidateContext(
            agent_id="a",
            job_id="j",
            artifact_type="skill",
            optimizer_type="GEPA",
            diagnosis=Diagnosis(
                root_cause="competing objectives",
                affected_components=["skill"],
                confidence=0.4,
                alternatives=["a", "b", "c", "d"],
            ),
            skill_name="tool-use",
            allowed_tools="Bash(python:*)",
            pareto_dims=["precision", "recall"],
            population_size=8,
            generations=3,
        )
        assert ctx.skill_name == "tool-use"
        assert ctx.pareto_dims == ["precision", "recall"]


# ═══════════════════════════════════════════════════════════════
# 5. FakeLLMProvider — GEPA candidate generation
# ═══════════════════════════════════════════════════════════════


class TestFakeLLMProviderGEPA:
    """FakeLLMProvider generates GEPA-aware fake responses."""

    def test_gepa_prompt_candidate_includes_gepa_metadata(self) -> None:
        llm = FakeLLMProvider()
        ctx = CandidateContext(
            agent_id="support-agent",
            job_id="RFN-G1",
            artifact_type="prompt",
            optimizer_type="GEPA",
            diagnosis=Diagnosis(
                root_cause="Competing objectives between brevity and detail",
                affected_components=["prompt"],
                confidence=0.5,
                alternatives=["shorter", "longer", "balanced"],
            ),
            current_artifact_content="You are a helpful assistant.",
            pareto_dims=["quality", "safety"],
        )
        candidate, usage = llm.generate_candidate(ctx)
        assert "GEPA" in candidate.content
        assert "genetic-Pareto" in candidate.content
        assert "Confidence: 0.5" in candidate.content
        assert "Alternatives considered: 3" in candidate.content

    def test_gepa_skill_candidate_includes_gepa_and_skill_info(self) -> None:
        llm = FakeLLMProvider()
        ctx = CandidateContext(
            agent_id="support-agent",
            job_id="RFN-G2",
            artifact_type="skill",
            optimizer_type="GEPA",
            diagnosis=Diagnosis(
                root_cause="Tool format tradeoff",
                affected_components=["skill"],
                confidence=0.4,
                alternatives=["json", "xml", "yaml"],
            ),
            current_artifact_content="Use tools with JSON format.",
            skill_name="tool-use",
            allowed_tools="Bash(python:*) WebFetch",
            pareto_dims=["precision", "recall"],
        )
        candidate, usage = llm.generate_candidate(ctx)
        assert "GEPA" in candidate.content
        assert "tool-use" in candidate.content
        assert "Bash(python:*) WebFetch" in candidate.content

    def test_gepa_cold_start_includes_gepa_metadata(self) -> None:
        """Cold-start (no existing content) with GEPA."""
        llm = FakeLLMProvider()
        ctx = CandidateContext(
            agent_id="support-agent",
            job_id="RFN-G3",
            artifact_type="prompt",
            optimizer_type="GEPA",
            diagnosis=Diagnosis(
                root_cause="No prompt exists yet — competing approaches",
                affected_components=["prompt"],
                confidence=0.3,
                alternatives=["formal", "casual", "technical"],
            ),
        )
        candidate, usage = llm.generate_candidate(ctx)
        assert "GEPA" in candidate.content
        assert "genetic-Pareto" in candidate.content

    def test_metaprompt_candidate_no_gepa_metadata(self) -> None:
        """MetaPrompt candidates should NOT have GEPA metadata."""
        llm = FakeLLMProvider()
        ctx = CandidateContext(
            agent_id="support-agent",
            job_id="RFN-G4",
            artifact_type="prompt",
            optimizer_type="MetaPrompt",
            diagnosis=Diagnosis(
                root_cause="Simple fix",
                affected_components=["prompt"],
                confidence=0.9,
                alternatives=[],
            ),
            current_artifact_content="You are a helper.",
        )
        candidate, usage = llm.generate_candidate(ctx)
        assert "GEPA" not in candidate.content
        assert "genetic-Pareto" not in candidate.content


# ═══════════════════════════════════════════════════════════════
# 6. Candidate pipeline — GEPA context passthrough
# ═══════════════════════════════════════════════════════════════


class TestCandidatePipelineGEPA:
    """run_candidate passes GEPA fields through to the LLM provider."""

    def test_gepa_selected_for_low_confidence_prompt(self, db_session: Session) -> None:
        _make_agent(db_session)
        _make_item(db_session)
        job = _make_job(
            db_session,
            artifact_type="prompt",
            diagnosis={
                "root_cause": "Ambiguous root cause",
                "confidence": 0.4,
                "alternatives": ["a", "b", "c"],
                "affected_components": ["prompt"],
            },
        )

        store = FakeArtifactStore(prompts={"support-agent": "You are an agent."})
        llm = FakeLLMProvider()

        result = run_candidate(db_session, "RFN-GEPA", llm, store)
        assert result.optimizer_type == "GEPA"
        assert result.current_stage == "eval"

        # Verify GEPA context was passed.
        ctx = llm.candidate_calls[0]
        assert ctx.optimizer_type == "GEPA"
        assert ctx.pareto_dims is not None
        assert ctx.population_size is not None
        assert ctx.generations is not None

    def test_gepa_selected_for_low_confidence_skill(self, db_session: Session) -> None:
        _make_agent(db_session, skills=["tool-use"])
        _make_skill(db_session)
        _make_item(db_session)
        job = _make_job(
            db_session,
            artifact_type="skill",
            skill_name="tool-use",
            diagnosis={
                "root_cause": "Unclear tool format competing objectives",
                "confidence": 0.5,
                "alternatives": [],
                "affected_components": ["skill"],
            },
        )

        store = FakeArtifactStore(skills={"tool-use": "Use tools carefully."})
        llm = FakeLLMProvider()

        result = run_candidate(db_session, "RFN-GEPA", llm, store)
        assert result.optimizer_type == "GEPA"

        ctx = llm.candidate_calls[0]
        assert ctx.optimizer_type == "GEPA"
        assert ctx.skill_name == "tool-use"
        assert ctx.pareto_dims is not None

    def test_gepa_candidate_content_has_gepa_markers(self, db_session: Session) -> None:
        _make_agent(db_session)
        _make_item(db_session)
        job = _make_job(
            db_session,
            artifact_type="prompt",
            diagnosis={
                "root_cause": "Trade-off between safety and helpfulness",
                "confidence": 0.3,
                "alternatives": [],
                "affected_components": ["prompt"],
            },
        )

        store = FakeArtifactStore(prompts={"support-agent": "Be helpful."})
        llm = FakeLLMProvider()

        result = run_candidate(db_session, "RFN-GEPA", llm, store)
        candidate_data = result.candidate
        assert "GEPA" in candidate_data["content"]

    def test_no_gepa_for_high_confidence(self, db_session: Session) -> None:
        """High-confidence diagnosis should NOT trigger GEPA."""
        _make_agent(db_session)
        _make_item(db_session)
        job = _make_job(
            db_session,
            artifact_type="prompt",
            diagnosis={
                "root_cause": "Clear and simple issue",
                "confidence": 0.95,
                "alternatives": [],
                "affected_components": ["prompt"],
            },
        )

        store = FakeArtifactStore(prompts={"support-agent": "Be helpful."})
        llm = FakeLLMProvider()

        result = run_candidate(db_session, "RFN-GEPA", llm, store)
        assert result.optimizer_type == "MetaPrompt"

        ctx = llm.candidate_calls[0]
        assert ctx.pareto_dims is None  # Not GEPA
        assert ctx.population_size is None

    def test_gepa_custom_pareto_dims_from_agent_config(self, db_session: Session) -> None:
        """Agent config can override GEPA params."""
        _make_agent(
            db_session,
            optimizer_config={
                "skills": [],
                "pareto_dims": ["empathy", "length", "accuracy"],
                "population_size": 16,
                "generations": 5,
            },
        )
        _make_item(db_session)
        job = _make_job(
            db_session,
            artifact_type="prompt",
            diagnosis={
                "root_cause": "Unclear problem",
                "confidence": 0.4,
                "alternatives": [],
                "affected_components": ["prompt"],
            },
        )

        store = FakeArtifactStore(prompts={"support-agent": "Be helpful."})
        llm = FakeLLMProvider()

        result = run_candidate(db_session, "RFN-GEPA", llm, store)
        assert result.optimizer_type == "GEPA"

        ctx = llm.candidate_calls[0]
        assert ctx.pareto_dims == ["empathy", "length", "accuracy"]
        assert ctx.population_size == 16
        assert ctx.generations == 5


# ═══════════════════════════════════════════════════════════════
# 7. OpenAI Agents provider — GEPA dispatch (mocked)
# ═══════════════════════════════════════════════════════════════


class TestOpenAIAgentsGEPADispatch:
    """OpenAIAgentsProvider dispatches to GEPA when optimizer_type='GEPA'."""

    def test_gepa_falls_back_when_not_installed(self) -> None:
        """When gepa package is not installed, falls back to MetaPrompt."""
        from caliber.llm.openai_agents import OpenAIAgentsLLMProvider

        provider = OpenAIAgentsLLMProvider.__new__(OpenAIAgentsLLMProvider)
        provider._diagnosis_model = "gpt-4o-mini"
        provider._candidate_model = "gpt-4o"
        provider._diagnosis_agent = None
        provider._candidate_agent = None

        ctx = CandidateContext(
            agent_id="support-agent",
            job_id="RFN-G-FALLBACK",
            artifact_type="prompt",
            optimizer_type="GEPA",
            diagnosis=Diagnosis(
                root_cause="Low confidence issue",
                affected_components=["prompt"],
                confidence=0.4,
                alternatives=["a", "b", "c"],
            ),
            current_artifact_content="You are a helper.",
        )

        # Mock the import to raise ImportError (gepa not installed)
        import builtins

        real_import = builtins.__import__

        def _mock_import(name, *args, **kwargs):
            if "gepa" in name.lower() or name == "mlflow.genai.optimize.optimizers":
                raise ImportError("gepa not installed")
            return real_import(name, *args, **kwargs)

        # Also mock the MetaPrompt fallback path
        mock_agent = MagicMock()
        mock_result = MagicMock()
        mock_result.final_output = PromptCandidate(
            artifact_type="prompt",
            content="Fallback MetaPrompt content",
            rationale="Fell back from GEPA",
            diff_summary="+1 / -1",
        )
        mock_result.raw_responses = [MagicMock(usage=MagicMock(input_tokens=10, output_tokens=20))]

        with patch.object(builtins, "__import__", side_effect=_mock_import):
            with patch.object(provider, "_ensure_candidate_agent", return_value=mock_agent):
                with patch.object(provider, "_run_agent_sync", return_value=mock_result):
                    candidate, usage = provider.generate_candidate(ctx)

        assert candidate.content == "Fallback MetaPrompt content"
        assert candidate.rationale == "Fell back from GEPA"

    def test_unknown_optimizer_raises(self) -> None:
        """Unknown optimizer types still raise LLMProviderError."""
        from caliber.llm.openai_agents import OpenAIAgentsLLMProvider
        from caliber.llm.provider import LLMProviderError

        provider = OpenAIAgentsLLMProvider.__new__(OpenAIAgentsLLMProvider)
        provider._diagnosis_model = "gpt-4o-mini"
        provider._candidate_model = "gpt-4o"
        provider._diagnosis_agent = None
        provider._candidate_agent = None

        ctx = CandidateContext(
            agent_id="support-agent",
            job_id="RFN-UNK",
            artifact_type="prompt",
            optimizer_type="UnknownOptimizer",
            diagnosis=Diagnosis(
                root_cause="test",
                affected_components=[],
                confidence=0.9,
                alternatives=[],
            ),
        )

        # Rejected by the registry, which also reports the alternatives.
        with pytest.raises(LLMProviderError, match="not registered") as caught:
            provider.generate_candidate(ctx)
        assert "GEPA" in str(caught.value)

    def test_skill_meta_prompt_still_works(self) -> None:
        """SkillMetaPrompt is now also accepted (not just MetaPrompt)."""
        from caliber.llm.openai_agents import OpenAIAgentsLLMProvider

        provider = OpenAIAgentsLLMProvider.__new__(OpenAIAgentsLLMProvider)
        provider._diagnosis_model = "gpt-4o-mini"
        provider._candidate_model = "gpt-4o"
        provider._diagnosis_agent = None
        provider._candidate_agent = None

        ctx = CandidateContext(
            agent_id="support-agent",
            job_id="RFN-SMP",
            artifact_type="skill",
            optimizer_type="SkillMetaPrompt",
            diagnosis=Diagnosis(
                root_cause="Skill issue",
                affected_components=["skill"],
                confidence=0.9,
                alternatives=[],
            ),
            skill_name="tool-use",
        )

        mock_agent = MagicMock()
        mock_result = MagicMock()
        mock_result.final_output = PromptCandidate(
            artifact_type="skill",
            content="Updated skill",
            rationale="Fixed",
            diff_summary="+1/-1",
        )
        mock_result.raw_responses = [MagicMock(usage=MagicMock(input_tokens=5, output_tokens=10))]

        with patch.object(provider, "_ensure_candidate_agent", return_value=mock_agent):
            with patch.object(provider, "_run_agent_sync", return_value=mock_result):
                candidate, usage = provider.generate_candidate(ctx)

        assert candidate.content == "Updated skill"


# ═══════════════════════════════════════════════════════════════
# 8. End-to-end: GEPA pipeline (prompt and skill)
# ═══════════════════════════════════════════════════════════════


class TestGEPAEndToEnd:
    """End-to-end GEPA pipeline tests."""

    def test_prompt_gepa_pipeline(self, db_session: Session) -> None:
        """Low-confidence prompt job flows through GEPA selection → candidate."""
        _make_agent(db_session)
        _make_item(db_session)
        _make_job(
            db_session,
            artifact_type="prompt",
            diagnosis={
                "root_cause": "Ambiguous — multiple competing objectives (brevity vs detail)",
                "confidence": 0.35,
                "alternatives": ["be shorter", "add detail", "restructure"],
                "affected_components": ["prompt"],
            },
        )

        store = FakeArtifactStore(prompts={"support-agent": "You are helpful."})
        llm = FakeLLMProvider()

        result = run_candidate(db_session, "RFN-GEPA", llm, store)
        assert result.optimizer_type == "GEPA"
        assert result.current_stage == "eval"
        assert "GEPA" in result.candidate["content"]
        assert "genetic-Pareto" in result.candidate["content"]

    def test_skill_gepa_pipeline(self, db_session: Session) -> None:
        """Low-confidence skill job flows through GEPA selection → candidate."""
        _make_agent(db_session, skills=["tool-use"])
        _make_skill(db_session, name="tool-use", content="Use tools with JSON format.")
        _make_item(db_session)
        _make_job(
            db_session,
            artifact_type="skill",
            skill_name="tool-use",
            diagnosis={
                "root_cause": "Tool format unclear — competing dimensions",
                "confidence": 0.45,
                "alternatives": ["json", "xml", "yaml", "custom"],
                "affected_components": ["skill"],
            },
        )

        store = FakeArtifactStore(skills={"tool-use": "Use tools with JSON format."})
        llm = FakeLLMProvider()

        result = run_candidate(db_session, "RFN-GEPA", llm, store)
        assert result.optimizer_type == "GEPA"
        assert result.current_stage == "eval"
        assert "tool-use" in result.candidate["content"]
        assert "GEPA" in result.candidate["content"]

    def test_gepa_not_triggered_for_clear_diagnosis(self, db_session: Session) -> None:
        """Clear, high-confidence diagnosis uses MetaPrompt, not GEPA."""
        _make_agent(db_session)
        _make_item(db_session)
        _make_job(
            db_session,
            artifact_type="prompt",
            diagnosis={
                "root_cause": "Missing safety guardrail",
                "confidence": 0.95,
                "alternatives": [],
                "affected_components": ["prompt"],
            },
        )

        store = FakeArtifactStore(prompts={"support-agent": "You are an assistant."})
        llm = FakeLLMProvider()

        result = run_candidate(db_session, "RFN-GEPA", llm, store)
        assert result.optimizer_type == "MetaPrompt"
        assert "GEPA" not in result.candidate["content"]
