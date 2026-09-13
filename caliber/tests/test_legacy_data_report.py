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
    duplicate_name_report,
    legacy_null_report,
)
from caliber.db.models import CaliberSkill, CaliberWorkflow


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
    """`CaliberWorkflow.name` has no DB-level unique constraint (only an
    application-level check in `create_workflow`) -- exactly the case this
    report exists to surface directly-inserted or pre-Phase-2 collisions
    for."""
    _workflow(db_session, workflow_id="WF-r1", name="dup-name", project_id="P-1")
    _workflow(db_session, workflow_id="WF-r2", name="dup-name", project_id="P-2")
    _workflow(db_session, workflow_id="WF-r3", name="dup-name", project_id=None)
    _workflow(db_session, workflow_id="WF-r4", name="unique-name", project_id="P-1")

    findings = duplicate_name_report(db_session)
    workflow_findings = [f for f in findings if f.model == "CaliberWorkflow"]
    assert workflow_findings == [
        DuplicateNameFinding(
            model="CaliberWorkflow",
            table="caliber_workflows",
            name="dup-name",
            project_ids=("P-1", "P-2", None),
        )
    ]


def test_duplicate_name_report_ignores_a_name_used_in_only_one_project(
    db_session: Session,
) -> None:
    _workflow(db_session, workflow_id="WF-r5", name="only-here", project_id="P-1")
    findings = [f for f in duplicate_name_report(db_session) if f.model == "CaliberWorkflow"]
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
