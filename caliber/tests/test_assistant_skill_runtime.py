"""Tests for the Caliber Assistant skill runtime resolver."""

from __future__ import annotations

from sqlalchemy.orm import Session

from caliber.assistant.skill_runtime import (
    AssistantSkillResolutionRequest,
    build_skill_prompt_block,
    resolve_assistant_skills,
)
from caliber.db.models import CaliberSkill


def _skill(
    db: Session,
    name: str,
    *,
    summary: str = "Useful assistant guidance.",
    content: str = "Follow this guidance when relevant.",
    description: str = "Assistant runtime skill.",
    tags: list[str] | None = None,
    category: str = "custom",
    status: str = "active",
    depends_on: list[str] | None = None,
    allowed_tools: str | None = None,
) -> CaliberSkill:
    row = CaliberSkill(
        skill_id=f"SK-{name}",
        name=name,
        description=description,
        summary=summary,
        content=content,
        owner="@test",
        category=category,
        tags=tags or [],
        status=status,
        depends_on=depends_on or [],
        allowed_tools=allowed_tools,
        version=1,
    )
    db.add(row)
    db.commit()
    return row


def _request(**overrides: object) -> AssistantSkillResolutionRequest:
    base = {
        "user_message": "please calibrate this workflow",
        "artifact_type": "workflow",
        "session_goal": "",
        "mode": "auto",
        "explicit_skill_names": (),
        "pinned_skill_names": (),
        "disabled_skill_names": (),
    }
    base.update(overrides)
    return AssistantSkillResolutionRequest(**base)  # type: ignore[arg-type]


def test_explicit_skill_selected_by_name(db_session: Session) -> None:
    _skill(db_session, "workflow-calibration-review")

    result = resolve_assistant_skills(
        db_session,
        _request(mode="manual", explicit_skill_names=("workflow-calibration-review",)),
    )

    assert [skill.name for skill in result.skills] == ["workflow-calibration-review"]
    assert result.skills[0].selection_reason == "explicit"


def test_archived_skill_is_not_selected(db_session: Session) -> None:
    _skill(db_session, "old-skill", status="archived")

    result = resolve_assistant_skills(
        db_session,
        _request(mode="manual", explicit_skill_names=("old-skill",)),
    )

    assert result.skills == ()
    assert "not found or is not active" in result.warnings[0]


def test_disabled_skill_excluded(db_session: Session) -> None:
    _skill(db_session, "workflow-calibration-review")

    result = resolve_assistant_skills(
        db_session,
        _request(
            mode="manual",
            explicit_skill_names=("workflow-calibration-review",),
            disabled_skill_names=("workflow-calibration-review",),
        ),
    )

    assert result.skills == ()
    assert "disabled" in result.warnings[0]


def test_dependency_included_and_cycle_warns(db_session: Session) -> None:
    _skill(db_session, "review-base", depends_on=["workflow-calibration-review"])
    _skill(db_session, "workflow-calibration-review", depends_on=["review-base"])

    result = resolve_assistant_skills(
        db_session,
        _request(mode="manual", explicit_skill_names=("review-base",), max_skills=5),
    )

    assert [skill.name for skill in result.skills] == ["review-base", "workflow-calibration-review"]
    assert any("Dependency cycle" in warning for warning in result.warnings)


def test_auto_mode_selects_by_tag_summary_and_artifact_type(db_session: Session) -> None:
    _skill(
        db_session,
        "workflow-calibration-review",
        summary="Review workflow calibration candidates and eval datasets.",
        tags=["assistant-runtime", "calibration"],
        category="workflow_automation",
    )
    _skill(db_session, "unrelated-copy-style", summary="Write concise email copy.")

    result = resolve_assistant_skills(db_session, _request())

    assert [skill.name for skill in result.skills] == ["workflow-calibration-review"]
    assert result.skills[0].selection_reason.startswith("auto:")


def test_max_skills_and_content_budget_are_enforced(db_session: Session) -> None:
    for idx in range(4):
        _skill(
            db_session,
            f"calibration-{idx}",
            summary="workflow calibration",
            content="x" * 40,
            tags=["calibration"],
        )

    result = resolve_assistant_skills(
        db_session,
        _request(max_skills=2, max_content_chars=10),
    )

    assert len(result.skills) == 2
    assert all(not skill.content_included for skill in result.skills)
    assert any("limited to 2" in warning for warning in result.warnings)
    assert any("prompt budget" in warning for warning in result.warnings)


def test_unknown_explicit_skill_returns_warning(db_session: Session) -> None:
    result = resolve_assistant_skills(
        db_session,
        _request(mode="manual", explicit_skill_names=("missing-skill",)),
    )

    assert result.skills == ()
    assert "missing-skill" in result.warnings[0]


def test_prompt_block_records_provenance(db_session: Session) -> None:
    _skill(db_session, "tool-safety", allowed_tools="read-only")
    result = resolve_assistant_skills(
        db_session,
        _request(mode="manual", explicit_skill_names=("tool-safety",)),
    )

    block = build_skill_prompt_block(result.skills)

    assert "CALIBER ASSISTANT SKILLS" in block
    assert "Skill: tool-safety" in block
    assert "Version: 1" in block
    assert "Why selected: explicit" in block
    assert "Allowed tools guidance: read-only" in block


def test_truncation_drops_dependent_when_its_dependency_is_cut(db_session: Session) -> None:
    """Regression (#20): truncating the selected list (deps appended after the
    dependent) must not keep a skill whose required dependency was dropped —
    the result would reference an absent prerequisite."""
    _skill(db_session, "alpha", depends_on=["beta"])
    _skill(db_session, "beta")
    result = resolve_assistant_skills(
        db_session, _request(explicit_skill_names=("alpha",), max_skills=1)
    )
    names = {s.name for s in result.skills}
    # Whenever the dependent is kept, its dependency must be present too.
    assert "alpha" not in names or "beta" in names
