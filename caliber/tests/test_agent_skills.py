"""Tests for ``GET /caliber/agents/{agent_id}/skills``.

The endpoint resolves skill *names* cited in an agent's
``optimizer_config.skills`` into full skill records, reporting any
unresolved names separately so the UI can flag broken references.
"""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberAgentConfig, CaliberSkill
from caliber.routes.agents import SKILLS_PATH


def _seed_agent(session: Session, *, optimizer_config: dict[str, object]) -> None:
    session.add(
        CaliberAgentConfig(
            agent_id="agent",
            experiment_id="exp",
            name="A",
            owner="@x",
            artifact_types=["prompt"],
            eval_thresholds={},
            optimizer_config=optimizer_config,
            approval_policy={},
        )
    )
    session.commit()


def _seed_skill(session: Session, *, name: str, skill_id: str | None = None) -> None:
    session.add(
        CaliberSkill(
            skill_id=skill_id or f"SK-{name}",
            name=name,
            description="",
            content="x",
            owner="@x",
            tags=[],
            status="active",
            version=1,
        )
    )
    session.commit()


def test_agent_skills_404_for_missing_agent(client: TestClient) -> None:
    response = client.get(SKILLS_PATH.replace("{agent_id}", "nope"))
    assert response.status_code == 404


def test_agent_skills_empty_when_optimizer_config_has_none(
    client: TestClient, db_session: Session
) -> None:
    _seed_agent(db_session, optimizer_config={})
    response = client.get(SKILLS_PATH.replace("{agent_id}", "agent"))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["skills"] == []
    assert data["missing"] == []


def test_agent_skills_resolves_cited_names(client: TestClient, db_session: Session) -> None:
    _seed_skill(db_session, name="reasoning_v1")
    _seed_skill(db_session, name="tool_use")
    _seed_agent(
        db_session,
        optimizer_config={"skills": ["reasoning_v1", "tool_use"], "type": "MetaPrompt"},
    )
    response = client.get(SKILLS_PATH.replace("{agent_id}", "agent"))
    assert response.status_code == 200
    data = response.json()["data"]
    names = {s["name"] for s in data["skills"]}
    assert names == {"reasoning_v1", "tool_use"}
    assert data["missing"] == []


def test_agent_skills_reports_missing_references(client: TestClient, db_session: Session) -> None:
    _seed_skill(db_session, name="reasoning_v1")
    _seed_agent(
        db_session,
        optimizer_config={"skills": ["reasoning_v1", "deleted_skill"]},
    )
    response = client.get(SKILLS_PATH.replace("{agent_id}", "agent"))
    data = response.json()["data"]
    assert [s["name"] for s in data["skills"]] == ["reasoning_v1"]
    assert data["missing"] == ["deleted_skill"]


def test_agent_skills_includes_archived(client: TestClient, db_session: Session) -> None:
    """An archived skill still referenced by an agent surfaces — not
    silently dropped — so the operator can decide to unstrand it."""
    db_session.add(
        CaliberSkill(
            skill_id="SK-old",
            name="old_skill",
            description="",
            content="x",
            owner="@x",
            tags=[],
            status="archived",
            version=1,
        )
    )
    db_session.commit()
    _seed_agent(db_session, optimizer_config={"skills": ["old_skill"]})
    response = client.get(SKILLS_PATH.replace("{agent_id}", "agent"))
    data = response.json()["data"]
    assert len(data["skills"]) == 1
    assert data["skills"][0]["status"] == "archived"
    assert data["missing"] == []


def test_agent_skills_handles_malformed_optimizer_config(
    client: TestClient, db_session: Session
) -> None:
    """A non-list ``skills`` value (operator typo) is silently ignored
    — the endpoint is forgiving so the dashboard doesn't crash."""
    _seed_agent(db_session, optimizer_config={"skills": "not-a-list"})
    response = client.get(SKILLS_PATH.replace("{agent_id}", "agent"))
    assert response.status_code == 200
    assert response.json()["data"] == {"skills": [], "missing": []}
