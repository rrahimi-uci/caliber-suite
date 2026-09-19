"""Tests for the legacy-null / duplicate-name migration report (`P2`, item 9).

Read-only evidence, zero behavior change -- these tests seed a fixture DB
with deliberately messy rows (some legacy-null `project_id`, some
same-name-different-project collisions) and assert the report finds them.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from caliber.db.legacy_data_report import (
    DuplicateNameFinding,
    LegacyNullFinding,
    OrphanProjectIdFinding,
    duplicate_name_report,
    legacy_null_report,
    orphan_project_id_report,
)
from caliber.db.models import CaliberKnowledgeBase, CaliberProject, CaliberSkill, CaliberWorkflow


def _skill(session: Session, **overrides: object) -> CaliberSkill:
    defaults: dict[str, object] = {
        "skill_id": "SK-report0001",
        "name": "report-skill",
        "content": "content",
        "owner": "@sarah",
    }
    defaults.update(overrides)
    row = CaliberSkill(**defaults)
    session.add(row)
    session.commit()
    return row


def _workflow(session: Session, **overrides: object) -> CaliberWorkflow:
    defaults: dict[str, object] = {
        "workflow_id": "WF-report0001",
        "name": "report-workflow",
        "owner": "@sarah",
    }
    defaults.update(overrides)
    row = CaliberWorkflow(**defaults)
    session.add(row)
    session.commit()
    return row


def _knowledge_base(session: Session, **overrides: object) -> CaliberKnowledgeBase:
    defaults: dict[str, object] = {
        "knowledge_base_id": "KB-report0001",
        "name": "report-kb",
        "owner": "@sarah",
        "source_bucket": "report-bucket",
    }
    defaults.update(overrides)
    row = CaliberKnowledgeBase(**defaults)
    session.add(row)
    session.commit()
    return row


def test_legacy_null_report_counts_null_project_id_rows(db_session: Session) -> None:
    _skill(db_session, skill_id="SK-r1", name="skill-a", project_id=None)
    _skill(db_session, skill_id="SK-r2", name="skill-b", project_id="P-1")
    _skill(db_session, skill_id="SK-r3", name="skill-c", project_id=None)

    findings = {f.model: f for f in legacy_null_report(db_session)}
    assert findings["CaliberSkill"] == LegacyNullFinding(
        model="CaliberSkill", table="caliber_skills", total_rows=3, null_project_id_rows=2
    )


def test_legacy_null_report_omits_models_with_no_rows(db_session: Session) -> None:
    """A project-scoped model nothing has inserted into yet contributes no
    finding -- an all-zero report entry for every one of the ~20+ other
    project-scoped models would bury the ones that actually matter."""
    _skill(db_session)
    findings = {f.model for f in legacy_null_report(db_session)}
    assert "CaliberWorkflow" not in findings


def test_duplicate_name_report_finds_the_same_name_across_projects(db_session: Session) -> None:
    """`CaliberKnowledgeBase.name` is unique only per (project-or-no-project,
    owner) (`uq_knowledge_base_project_owner_name` /
    `uq_knowledge_base_owner_name_no_project`, `P2-A` slice 11) -- a
    same-name collision across *different owners* is still a real,
    DB-permitted case this report exists to surface (directly-inserted or
    pre-Phase-2 collisions, or simply two different people naming their own
    knowledge base the same thing)."""
    _knowledge_base(
        db_session, knowledge_base_id="KB-r1", name="dup-name", owner="@sarah", project_id="P-1"
    )
    _knowledge_base(
        db_session, knowledge_base_id="KB-r2", name="dup-name", owner="@mark", project_id="P-2"
    )
    _knowledge_base(
        db_session, knowledge_base_id="KB-r3", name="dup-name", owner="@erin", project_id=None
    )
    _knowledge_base(
        db_session,
        knowledge_base_id="KB-r4",
        name="unique-name",
        owner="@sarah",
        project_id="P-1",
    )

    findings = duplicate_name_report(db_session)
    kb_findings = [f for f in findings if f.model == "CaliberKnowledgeBase"]
    assert kb_findings == [
        DuplicateNameFinding(
            model="CaliberKnowledgeBase",
            table="caliber_knowledge_bases",
            name="dup-name",
            project_ids=("P-1", "P-2", None),
        )
    ]


def test_duplicate_name_report_ignores_a_name_used_in_only_one_project(
    db_session: Session,
) -> None:
    _knowledge_base(db_session, knowledge_base_id="KB-r5", name="only-here", project_id="P-1")
    findings = [f for f in duplicate_name_report(db_session) if f.model == "CaliberKnowledgeBase"]
    assert findings == []


def test_duplicate_name_report_skips_a_model_with_a_global_unique_name(
    db_session: Session,
) -> None:
    """`CaliberSkill.name` is globally unique (`uq_skill_name`) -- the
    constraint itself makes a real cross-project duplicate impossible, so
    this model should never appear in the report even with rows present."""
    _skill(db_session, skill_id="SK-r10", name="only-skill", project_id="P-1")
    findings = [f for f in duplicate_name_report(db_session) if f.model == "CaliberSkill"]
    assert findings == []


def test_duplicate_name_report_skips_caliber_workflow_since_p2a_slice_11(
    db_session: Session,
) -> None:
    """`CaliberWorkflow.name` gained a DB-level global unique constraint
    (`uq_workflow_name`, `P2-A` slice 11, matching `create_workflow`'s
    pre-existing app-level check) -- like `CaliberSkill` above, a real
    cross-project duplicate is now impossible, so this model should never
    appear in the report even with rows present."""
    _workflow(db_session, workflow_id="WF-r10", name="only-workflow", project_id="P-1")
    findings = [f for f in duplicate_name_report(db_session) if f.model == "CaliberWorkflow"]
    assert findings == []


def test_orphan_project_id_report_groups_unresolvable_project_references(
    db_session: Session,
) -> None:
    db_session.add(
        CaliberProject(
            project_id="P-known",
            name="Known project",
            owner="@sarah",
        )
    )
    db_session.commit()

    _skill(db_session, skill_id="SK-known", name="known", project_id="P-known")
    _skill(db_session, skill_id="SK-orphan-a", name="orphan-a", project_id="P-missing")
    _skill(db_session, skill_id="SK-orphan-b", name="orphan-b", project_id="P-missing")
    _workflow(
        db_session,
        workflow_id="WF-orphan",
        name="orphan-workflow",
        project_id="P-missing",
    )

    assert orphan_project_id_report(db_session) == [
        OrphanProjectIdFinding(
            model="CaliberSkill",
            table="caliber_skills",
            project_id="P-missing",
            row_count=2,
        ),
        OrphanProjectIdFinding(
            model="CaliberWorkflow",
            table="caliber_workflows",
            project_id="P-missing",
            row_count=1,
        ),
    ]


def test_orphan_project_id_report_omits_null_and_resolvable_references(
    db_session: Session,
) -> None:
    db_session.add(
        CaliberProject(
            project_id="P-resolvable",
            name="Resolvable project",
            owner="@sarah",
        )
    )
    db_session.commit()
    _skill(db_session, skill_id="SK-null", name="null-project", project_id=None)
    _skill(
        db_session,
        skill_id="SK-resolvable",
        name="resolvable-project",
        project_id="P-resolvable",
    )

    assert orphan_project_id_report(db_session) == []
