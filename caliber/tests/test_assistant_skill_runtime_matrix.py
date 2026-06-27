"""Expanded deterministic-skill-runtime coverage for Aria."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from caliber.assistant.skill_runtime import (
    DEFAULT_SKILL_RUNTIME_MODE,
    AssistantResolvedSkill,
    AssistantSkillResolutionRequest,
    _dedupe,
    _words,
    build_skill_prompt_block,
    normalize_skill_names,
    normalize_skill_runtime_mode,
    resolve_assistant_skills,
    runtime_metadata_from_session,
    score_skill_for_query,
    update_session_skill_runtime_metadata,
)
from caliber.db.models import CaliberSkill


def _skill(
    db: Session,
    name: str,
    *,
    description: str = "Assistant runtime skill.",
    summary: str = "Useful assistant guidance.",
    content: str = "Follow this guidance when relevant.",
    tags: list[str] | None = None,
    category: str = "custom",
    status: str = "active",
    depends_on: list[str] | None = None,
    allowed_tools: str | None = None,
    version: int = 1,
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
        version=version,
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
        "max_skills": 3,
        "max_content_chars": 6000,
    }
    base.update(overrides)
    return AssistantSkillResolutionRequest(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("auto", "auto"),
        ("manual", "manual"),
        ("off", "off"),
        ("AUTO", DEFAULT_SKILL_RUNTIME_MODE),
        (" review ", DEFAULT_SKILL_RUNTIME_MODE),
        (None, DEFAULT_SKILL_RUNTIME_MODE),
        (0, DEFAULT_SKILL_RUNTIME_MODE),
    ],
)
def test_normalize_skill_runtime_mode(raw: object, expected: str) -> None:
    assert normalize_skill_runtime_mode(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ()),
        ("workflow-review", ("workflow-review",)),
        (["workflow-review", "workflow-review"], ("workflow-review",)),
        (["workflow-review", " Workflow-Review ", "tool-safety"], ("workflow-review", "tool-safety")),
        (("", " ", "tool-safety"), ("tool-safety",)),
        ((b"bytes",), ("b'bytes'",)),
        (123, ()),
        (["one", "ONE", "Two", "two", " three "], ("one", "Two", "three")),
    ],
)
def test_normalize_skill_names(raw: object, expected: tuple[str, ...]) -> None:
    assert normalize_skill_names(raw) == expected


@pytest.mark.parametrize(
    ("metadata_raw", "expected_mode", "expected_pinned", "expected_disabled", "expected_last"),
    [
        ({}, "auto", [], [], []),
        (None, "auto", [], [], []),
        (
            {"assistant_skill_runtime": {"mode": "manual"}},
            "manual",
            [],
            [],
            [],
        ),
        (
            {
                "assistant_skill_runtime": {
                    "mode": "off",
                    "pinned_skill_names": ["workflow-review", "Workflow-Review", "tool-safety"],
                    "disabled_skill_names": ["legacy", "LEGACY"],
                    "last_selected_skills": [{"name": "workflow-review"}],
                }
            },
            "off",
            ["workflow-review", "tool-safety"],
            ["legacy"],
            [{"name": "workflow-review"}],
        ),
        (
            {"assistant_skill_runtime": {"mode": "invalid", "last_selected_skills": "bad"}},
            "auto",
            [],
            [],
            [],
        ),
    ],
)
def test_runtime_metadata_from_session(
    metadata_raw: object,
    expected_mode: str,
    expected_pinned: list[str],
    expected_disabled: list[str],
    expected_last: list[dict[str, object]],
) -> None:
    runtime = runtime_metadata_from_session(metadata_raw)
    assert runtime["mode"] == expected_mode
    assert runtime["pinned_skill_names"] == expected_pinned
    assert runtime["disabled_skill_names"] == expected_disabled
    assert runtime["last_selected_skills"] == expected_last


@pytest.mark.parametrize(
    ("kwargs", "expected_mode", "expected_pinned", "expected_disabled", "expected_last"),
    [
        ({}, "auto", [], [], []),
        (
            {"skill_mode": "manual"},
            "manual",
            [],
            [],
            [],
        ),
        (
            {"pinned_skill_names": ["workflow-review", "Workflow-Review", "tool-safety"]},
            "auto",
            ["workflow-review", "tool-safety"],
            [],
            [],
        ),
        (
            {"disabled_skill_names": ["legacy", "LEGACY"]},
            "auto",
            [],
            ["legacy"],
            [],
        ),
        (
            {"last_selected_skills": [{"name": "workflow-review"}]},
            "auto",
            [],
            [],
            [{"name": "workflow-review"}],
        ),
        (
            {
                "skill_mode": "off",
                "pinned_skill_names": ["workflow-review"],
                "disabled_skill_names": ["legacy"],
                "last_selected_skills": [{"name": "workflow-review"}],
            },
            "off",
            ["workflow-review"],
            ["legacy"],
            [{"name": "workflow-review"}],
        ),
    ],
)
def test_update_session_skill_runtime_metadata(
    kwargs: dict[str, object],
    expected_mode: str,
    expected_pinned: list[str],
    expected_disabled: list[str],
    expected_last: list[dict[str, object]],
) -> None:
    metadata = update_session_skill_runtime_metadata({}, **kwargs)
    runtime = metadata["assistant_skill_runtime"]
    assert runtime["mode"] == expected_mode
    assert runtime["pinned_skill_names"] == expected_pinned
    assert runtime["disabled_skill_names"] == expected_disabled
    assert runtime["last_selected_skills"] == expected_last


@pytest.mark.parametrize(
    ("skill_kwargs", "query_kwargs", "expected_score", "reason_fragment"),
    [
        (
            {"name": "workflow-review"},
            {"user_message": "workflow review"},
            5,
            "name",
        ),
        (
            {"name": "neutral", "tags": ["calibration", "safety"]},
            {"user_message": "need calibration guidance"},
            5,
            "tag",
        ),
        (
            {"name": "neutral", "category": "workflow_automation"},
            {"user_message": "help", "artifact_type": "workflow"},
            3,
            "artifact_type",
        ),
        (
            {"name": "neutral", "summary": "Review the email routing logic."},
            {"user_message": "email routing"},
            2,
            "summary",
        ),
        (
            {"name": "neutral", "description": "Safety checklist for approvals."},
            {"user_message": "approval safety"},
            1,
            "description",
        ),
        (
            {"name": "neutral", "summary": "Unrelated", "description": "Unrelated"},
            {"user_message": "database sharding"},
            0,
            "",
        ),
    ],
)
def test_score_skill_for_query_matches_expected_reason_buckets(
    db_session: Session,
    skill_kwargs: dict[str, object],
    query_kwargs: dict[str, str],
    expected_score: int,
    reason_fragment: str,
) -> None:
    row = _skill(db_session, **skill_kwargs)
    score, reason = score_skill_for_query(row, **query_kwargs)
    assert score >= expected_score
    if reason_fragment:
        assert reason_fragment in reason
    else:
        assert reason == ""


@pytest.mark.parametrize(
    ("user_message", "expected_words"),
    [
        ("workflow review", ["workflow", "review"]),
        ("workflow-review", ["workflow", "review"]),
        ("v2_calibration", ["v2_calibration"]),
        ("", []),
        ("Mixed CASE Safety", ["mixed", "case", "safety"]),
    ],
)
def test_words_tokenizer_matches_scoring_terms(user_message: str, expected_words: list[str]) -> None:
    assert _words(user_message) == expected_words


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([], []),
        (["a", "b", "a"], ["a", "b"]),
        (["workflow", "workflow", "tool"], ["workflow", "tool"]),
        (["x"], ["x"]),
    ],
)
def test_dedupe_preserves_first_seen_order(values: list[str], expected: list[str]) -> None:
    assert _dedupe(values) == expected


def test_resolve_assistant_skills_returns_nothing_when_mode_is_off(db_session: Session) -> None:
    _skill(db_session, "workflow-review")
    result = resolve_assistant_skills(db_session, _request(mode="off"))
    assert result.skills == ()
    assert result.warnings == ()


def test_resolve_assistant_skills_returns_nothing_when_max_skills_is_zero(db_session: Session) -> None:
    _skill(db_session, "workflow-review")
    result = resolve_assistant_skills(db_session, _request(max_skills=0))
    assert result.skills == ()
    assert result.warnings == ()


def test_resolve_assistant_skills_manual_mode_selects_explicit_then_pinned(db_session: Session) -> None:
    _skill(db_session, "workflow-review")
    _skill(db_session, "tool-safety")

    result = resolve_assistant_skills(
        db_session,
        _request(
            mode="manual",
            explicit_skill_names=("workflow-review",),
            pinned_skill_names=("tool-safety",),
        ),
    )

    assert [skill.name for skill in result.skills] == ["workflow-review", "tool-safety"]
    assert [skill.selection_reason for skill in result.skills] == ["explicit", "pinned"]


def test_resolve_assistant_skills_warns_for_unknown_explicit_skill(db_session: Session) -> None:
    result = resolve_assistant_skills(
        db_session,
        _request(mode="manual", explicit_skill_names=("missing-skill",)),
    )
    assert result.skills == ()
    assert "missing-skill" in result.warnings[0]


def test_resolve_assistant_skills_warns_when_explicit_skill_is_disabled(db_session: Session) -> None:
    _skill(db_session, "workflow-review")
    result = resolve_assistant_skills(
        db_session,
        _request(
            mode="manual",
            explicit_skill_names=("workflow-review",),
            disabled_skill_names=("workflow-review",),
        ),
    )
    assert result.skills == ()
    assert "disabled" in result.warnings[0]


def test_resolve_assistant_skills_skips_archived_rows(db_session: Session) -> None:
    _skill(db_session, "archived-review", status="archived")
    result = resolve_assistant_skills(
        db_session,
        _request(mode="manual", explicit_skill_names=("archived-review",)),
    )
    assert result.skills == ()
    assert "not active" in result.warnings[0]


def test_resolve_assistant_skills_expands_dependencies(db_session: Session) -> None:
    _skill(db_session, "workflow-review", depends_on=["tool-safety"])
    _skill(db_session, "tool-safety")

    result = resolve_assistant_skills(
        db_session,
        _request(mode="manual", explicit_skill_names=("workflow-review",), max_skills=4),
    )

    assert [skill.name for skill in result.skills] == ["workflow-review", "tool-safety"]
    assert result.skills[1].selection_reason == "dependency:workflow-review"


def test_resolve_assistant_skills_warns_on_dependency_cycle(db_session: Session) -> None:
    _skill(db_session, "workflow-review", depends_on=["tool-safety"])
    _skill(db_session, "tool-safety", depends_on=["workflow-review"])

    result = resolve_assistant_skills(
        db_session,
        _request(mode="manual", explicit_skill_names=("workflow-review",), max_skills=4),
    )

    assert [skill.name for skill in result.skills] == ["workflow-review", "tool-safety"]
    assert any("Dependency cycle" in warning for warning in result.warnings)


def test_resolve_assistant_skills_warns_when_dependency_depth_limit_is_exceeded(
    db_session: Session,
) -> None:
    chain = [
        ("skill-1", ["skill-2"]),
        ("skill-2", ["skill-3"]),
        ("skill-3", ["skill-4"]),
        ("skill-4", ["skill-5"]),
        ("skill-5", ["skill-6"]),
        ("skill-6", []),
    ]
    for name, depends_on in chain:
        _skill(db_session, name, depends_on=depends_on)

    result = resolve_assistant_skills(
        db_session,
        _request(mode="manual", explicit_skill_names=("skill-1",), max_skills=8),
    )

    assert [skill.name for skill in result.skills] == [
        "skill-1",
        "skill-2",
        "skill-3",
        "skill-4",
        "skill-5",
    ]
    assert any("depth limit" in warning for warning in result.warnings)


def test_resolve_assistant_skills_auto_mode_scores_and_limits_results(db_session: Session) -> None:
    _skill(
        db_session,
        "workflow-calibration-review",
        summary="Review workflow calibration candidates.",
        tags=["assistant-runtime", "calibration"],
        category="workflow_automation",
    )
    _skill(
        db_session,
        "workflow-checks",
        summary="Workflow review checklist.",
        tags=["workflow"],
        category="workflow_automation",
    )
    _skill(
        db_session,
        "workflow-compliance",
        summary="Workflow compliance review.",
        tags=["workflow"],
        category="workflow_automation",
    )
    _skill(db_session, "copy-editor", summary="Write concise emails.", tags=["copy"])

    result = resolve_assistant_skills(
        db_session,
        _request(max_skills=2, user_message="please review this workflow calibration"),
    )

    assert [skill.name for skill in result.skills] == [
        "workflow-calibration-review",
        "workflow-checks",
    ]
    assert any("limited to 2" in warning for warning in result.warnings)


def test_resolve_assistant_skills_honors_pinned_and_disabled_names_in_auto_mode(
    db_session: Session,
) -> None:
    _skill(db_session, "workflow-review", tags=["workflow"], category="workflow_automation")
    _skill(db_session, "tool-safety", tags=["safety"])
    _skill(db_session, "legacy-skill", tags=["workflow"])

    result = resolve_assistant_skills(
        db_session,
        _request(
            pinned_skill_names=("tool-safety",),
            disabled_skill_names=("legacy-skill",),
            max_skills=3,
        ),
    )

    assert [skill.name for skill in result.skills][:2] == ["tool-safety", "workflow-review"]
    assert "legacy-skill" not in [skill.name for skill in result.skills]


def test_resolve_assistant_skills_applies_content_budget_per_skill(db_session: Session) -> None:
    _skill(
        db_session,
        "workflow-review",
        summary="Workflow review",
        content="A" * 40,
        tags=["workflow"],
        category="workflow_automation",
    )
    _skill(
        db_session,
        "tool-safety",
        summary="Tool safety",
        content="B" * 40,
        tags=["workflow"],
        category="workflow_automation",
    )

    result = resolve_assistant_skills(
        db_session,
        _request(max_content_chars=50, max_skills=2),
    )

    assert len(result.skills) == 2
    assert result.skills[0].content_included is True
    assert result.skills[1].content_included is False
    assert any("prompt budget" in warning for warning in result.warnings)


@pytest.mark.parametrize(
    ("skills", "expected_fragments"),
    [
        (
            [
                AssistantResolvedSkill(
                    skill_id="SK-1",
                    name="workflow-review",
                    version=2,
                    summary="Review workflows.",
                    content="Use eval datasets.",
                    allowed_tools="read-only",
                    depends_on=[],
                    tags=[],
                    category="workflow_automation",
                    selection_reason="explicit",
                    content_included=True,
                )
            ],
            [
                "CALIBER ASSISTANT SKILLS",
                "Skill: workflow-review",
                "Version: 2",
                "Why selected: explicit",
                "Allowed tools guidance: read-only",
                "Instructions:",
            ],
        ),
        (
            [
                AssistantResolvedSkill(
                    skill_id="SK-2",
                    name="tool-safety",
                    version=1,
                    summary="Keep tools safe.",
                    content="",
                    allowed_tools=None,
                    depends_on=[],
                    tags=[],
                    category="tooling",
                    selection_reason="pinned",
                    content_included=False,
                )
            ],
            [
                "Skill: tool-safety",
                "Why selected: pinned",
                "Instructions omitted due to prompt budget; rely on the summary only.",
            ],
        ),
    ],
)
def test_build_skill_prompt_block_surfaces_selection_provenance(
    skills: list[AssistantResolvedSkill],
    expected_fragments: list[str],
) -> None:
    block = build_skill_prompt_block(skills)
    for fragment in expected_fragments:
        assert fragment in block


def test_build_skill_prompt_block_returns_empty_string_for_empty_skill_list() -> None:
    assert build_skill_prompt_block([]) == ""
