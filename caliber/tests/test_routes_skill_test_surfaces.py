"""Tests for the skill test-render + test-selection routes (golden-path Wave 3)."""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberSkill
from caliber.routes.skills import LIST_PATH


def _insert_skill(session: Session, **overrides: object) -> CaliberSkill:
    defaults: dict[str, object] = {
        "skill_id": "SK-test0001",
        "name": "reasoning",
        "description": "Chain-of-thought reasoning rubric.",
        "summary": "chain of thought reasoning",
        "content": "Hello {{name}}, your role is {{role}}.",
        "owner": "@sarah",
        "tags": ["reasoning"],
        "status": "active",
        "version": 1,
    }
    defaults.update(overrides)
    skill = CaliberSkill(**defaults)
    session.add(skill)
    session.commit()
    return skill


def _render_url(skill_id: str) -> str:
    return f"{LIST_PATH}/{skill_id}/test-render"


def _selection_url(skill_id: str) -> str:
    return f"{LIST_PATH}/{skill_id}/test-selection"


# --------------------------------------------------------------------------- #
# test-render
# --------------------------------------------------------------------------- #


def test_test_render_substitutes_and_reports_variables(
    client: TestClient, db_session: Session
) -> None:
    _insert_skill(db_session, skill_id="SK-r1")
    resp = client.post(_render_url("SK-r1"), json={"variables": {"name": "Reza"}})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["rendered_content"] == "Hello Reza, your role is {{role}}."
    assert data["detected_variables"] == ["name", "role"]
    assert data["unresolved_variables"] == ["role"]
    assert data["variables_applied"] == {"name": "Reza"}
    assert data["skill_name"] == "reasoning"
    assert data["char_count"] == len(data["rendered_content"])


def test_test_render_404_for_missing_skill(client: TestClient) -> None:
    resp = client.post(_render_url("SK-missing"), json={"variables": {}})
    assert resp.status_code == 404


def test_test_render_rejects_non_object_variables(client: TestClient, db_session: Session) -> None:
    _insert_skill(db_session, skill_id="SK-r2")
    resp = client.post(_render_url("SK-r2"), json={"variables": ["not", "an", "object"]})
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# test-selection
# --------------------------------------------------------------------------- #


def test_test_selection_triggers_on_match(client: TestClient, db_session: Session) -> None:
    _insert_skill(db_session, skill_id="SK-s1", name="reasoning", tags=["reasoning"])
    resp = client.post(
        _selection_url("SK-s1"),
        json={"user_message": "I need chain of thought reasoning for this"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["is_selected"] is True
    assert data["selection_score"] > 0
    assert data["selection_reason"].startswith("auto:")


def test_test_selection_does_not_trigger_on_no_match(
    client: TestClient, db_session: Session
) -> None:
    _insert_skill(
        db_session,
        skill_id="SK-s2",
        name="reasoning",
        summary="chain of thought",
        tags=["reasoning"],
    )
    resp = client.post(
        _selection_url("SK-s2"), json={"user_message": "tomorrow's weather forecast"}
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["is_selected"] is False
    assert data["selection_score"] == 0


def test_test_selection_honors_stopword_aware_negative_trigger(
    client: TestClient, db_session: Session
) -> None:
    _insert_skill(
        db_session,
        skill_id="SK-s-negative",
        name="incident-review",
        summary="Review production incidents and deployment health",
        tags=["incident", "deployment"],
        skill_metadata={
            "test_triggers": {
                "should_trigger": ["review a production deployment incident"],
                "should_not_trigger": ["review a deployment tutorial"],
            }
        },
    )
    excluded = client.post(
        _selection_url("SK-s-negative"),
        json={"user_message": "Please review the deployment tutorial for beginners"},
    ).json()["data"]
    assert excluded["is_selected"] is False
    assert excluded["selection_score"] == 0
    assert excluded["selection_reason"].startswith("excluded:negative_trigger:")

    included = client.post(
        _selection_url("SK-s-negative"),
        json={"user_message": "Review the production deployment incident"},
    ).json()["data"]
    assert included["is_selected"] is True
    assert "positive_trigger" in included["selection_reason"]


def test_test_selection_404_for_missing_skill(client: TestClient) -> None:
    resp = client.post(_selection_url("SK-missing"), json={"user_message": "x"})
    assert resp.status_code == 404


def test_test_selection_requires_query(client: TestClient, db_session: Session) -> None:
    _insert_skill(db_session, skill_id="SK-s3")
    resp = client.post(_selection_url("SK-s3"), json={})
    assert resp.status_code == 400
