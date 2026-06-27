"""Unit coverage for approval impact previews."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberApprovalRequest,
    CaliberEvalDataset,
    CaliberRefinementJob,
    CaliberSkill,
    CaliberVerificationItem,
)
from caliber.impact import (
    _candidate_artifact_name,
    _current_content,
    _dataset_names,
    _optional_str,
    _risk_flags,
    _skill_names,
    build_impact_preview,
)
from caliber.schemas import ImpactAgentSchema, ImpactReferenceSchema


class _Store:
    def __init__(self, content: str | None) -> None:
        self.content = content

    def get_active_prompt(self, agent_id: str) -> str | None:
        return self.content


def _seed_preview_rows(
    session: Session,
    *,
    artifact_type: str = "prompt",
    candidate_snapshot: dict[str, object] | None = None,
    job_candidate: dict[str, object] | None = None,
) -> None:
    session.add_all(
        [
            CaliberAgentConfig(
                agent_id="support-agent",
                experiment_id="exp",
                name="Support",
                owner="@test",
                artifact_types=[artifact_type],
                optimizer_config={},
                approval_policy={},
                eval_thresholds={},
            ),
            CaliberVerificationItem(
                item_id="FB-1",
                agent_id="support-agent",
                category="hallucination",
                free_text="bad answer",
                severity="standard",
            ),
        ]
    )
    session.flush()
    session.add(
        CaliberRefinementJob(
            job_id="RFN-1",
            agent_id="support-agent",
            primary_item_id="FB-1",
            artifact_type=artifact_type,
            status="awaiting_approval",
            current_stage="approval",
            candidate=job_candidate
            or {
                "artifact_type": artifact_type,
                "content": "candidate",
                "baseline_content": "baseline",
            },
            bundle_targets=[],
        )
    )
    session.flush()
    session.add(
        CaliberApprovalRequest(
            approval_id="AP-1",
            job_id="RFN-1",
            agent_id="support-agent",
            status="pending",
            candidate_snapshot=candidate_snapshot,
        )
    )
    session.commit()


def test_build_impact_preview_missing_references_raise(db_session: Session) -> None:
    with pytest.raises(LookupError, match="not found"):
        build_impact_preview(db_session, "missing")

    _seed_preview_rows(db_session)
    job = db_session.get(CaliberRefinementJob, "RFN-1")
    assert job is not None
    db_session.delete(job)
    db_session.commit()
    with pytest.raises(LookupError, match="references missing job"):
        build_impact_preview(db_session, "AP-1")


def test_build_impact_preview_missing_agent_raises(db_session: Session) -> None:
    _seed_preview_rows(db_session)
    agent = db_session.get(CaliberAgentConfig, "support-agent")
    assert agent is not None
    db_session.delete(agent)
    db_session.commit()

    with pytest.raises(LookupError, match="references missing agent"):
        build_impact_preview(db_session, "AP-1")


def test_skill_impact_preview_resolves_shared_agents_and_unresolved_datasets(
    db_session: Session,
) -> None:
    _seed_preview_rows(
        db_session,
        artifact_type="skill",
        candidate_snapshot={"artifact_type": "skill", "skill_name": "lookup-policy"},
        job_candidate={
            "artifact_type": "skill",
            "content": "",
            "baseline_content": "previous skill content",
            "skill_name": "lookup-policy",
        },
    )
    primary = db_session.get(CaliberAgentConfig, "support-agent")
    assert primary is not None
    primary.optimizer_config = {"skills": "lookup-policy"}
    primary.approval_policy = {}
    primary.eval_thresholds = {
        "eval_dataset_ids": ["missing-dataset"],
        "golden_dataset_id": "golden-support",
    }
    db_session.add_all(
        [
            CaliberAgentConfig(
                agent_id="shared-agent",
                experiment_id="exp-shared",
                name="Shared",
                owner="@test",
                optimizer_config={"skills": ["lookup-policy"]},
                approval_policy={},
                eval_thresholds={},
            ),
            CaliberSkill(
                skill_id="SK-lookup",
                name="lookup-policy",
                description="",
                content="skill",
                owner="@test",
                tags=[],
            ),
            CaliberEvalDataset(
                dataset_id="ED-golden",
                name="golden-support",
                owner="@test",
                tags=[],
            ),
        ]
    )
    db_session.commit()

    preview = build_impact_preview(db_session, "AP-1", artifact_store=_Store(None))

    assert [agent.agent_id for agent in preview.impacted_agents] == [
        "support-agent",
        "shared-agent",
    ]
    assert preview.impacted_agents[1].role == "shared_skill"
    assert preview.impacted_skills[0].name == "lookup-policy"
    dataset_names = {dataset.name for dataset in preview.eval_datasets}
    assert {"golden-support", "missing-dataset"} <= dataset_names
    assert "candidate_content_empty" in preview.risk_flags
    assert "multi_agent_blast_radius" in preview.risk_flags


def test_impact_helper_fallbacks(db_session: Session) -> None:
    _seed_preview_rows(db_session)
    job = db_session.get(CaliberRefinementJob, "RFN-1")
    assert job is not None

    assert _current_content(_Store("from-store"), "support-agent", {}, job) == "from-store"
    assert (
        _current_content(None, "support-agent", {"current_content": "candidate-base"}, job)
        == "candidate-base"
    )
    assert _current_content(None, "support-agent", {}, job) == "baseline"
    job.candidate = None
    assert _current_content(None, "support-agent", {}, job) is None

    assert _candidate_artifact_name({"name": "artifact"}) == "artifact"
    assert _candidate_artifact_name({"artifact_name": ""}) is None
    assert _skill_names({"skills": ["a", "", 3, "b"]}) == {"a", "b"}
    assert _skill_names({"skills": "solo"}) == {"solo"}
    assert _skill_names({"skills": 3}) == set()
    assert _skill_names(None) == set()
    assert _dataset_names(
        {
            "eval_dataset_id": "a",
            "eval_dataset": "b",
            "golden_dataset_id": "c",
            "eval_dataset_ids": ["d", ""],
            "golden_dataset_ids": ["e"],
            "regression_dataset_ids": ["f", 1],
        }
    ) == {"a", "b", "c", "d", "e", "f"}
    assert _dataset_names(None) == set()
    assert _optional_str("x") == "x"
    assert _optional_str("") is None
    assert _risk_flags(
        current_content=None,
        candidate_content="",
        impacted_agents=[
            ImpactAgentSchema(agent_id="a"),
            ImpactAgentSchema(agent_id="b"),
        ],
        skills=[ImpactReferenceSchema(id="skill", name="skill")],
        datasets=[],
    ) == [
        "current_artifact_unavailable",
        "candidate_content_empty",
        "multi_agent_blast_radius",
        "skill_dependency_present",
        "no_eval_dataset_resolved",
    ]
