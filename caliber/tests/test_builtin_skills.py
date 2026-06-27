"""Built-in general-purpose skills register cleanly and surface in the UI."""

from __future__ import annotations

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from caliber.builtin_skills import BUILTIN_SKILLS, register_builtin_skills
from caliber.config import CaliberConfig
from caliber.db.models import CaliberSkill
from caliber.routes.skills import LIST_PATH
from caliber.schemas import SKILL_CATEGORIES
from caliber.server import create_app

PREFIX = "/ajax-api/2.0/mlflow/caliber"
_VALID_CATEGORIES = set(SKILL_CATEGORIES)
_ANTHROPIC_SKILL_NAMES = {
    "algorithmic-art",
    "brand-guidelines",
    "canvas-design",
    "claude-api",
    "doc-coauthoring",
    "docx",
    "frontend-design",
    "internal-comms",
    "mcp-builder",
    "pdf",
    "pptx",
    "skill-creator",
    "slack-gif-creator",
    "theme-factory",
    "web-artifacts-builder",
    "webapp-testing",
    "xlsx",
}


def test_register_builtin_skills_is_idempotent(db_session: Session) -> None:
    created = register_builtin_skills(db_session)
    db_session.commit()
    assert created == len(BUILTIN_SKILLS)
    again = register_builtin_skills(db_session)
    db_session.commit()
    assert again == 0  # nothing duplicated
    assert db_session.query(CaliberSkill).count() == len(BUILTIN_SKILLS)


def test_builtin_skills_are_well_formed(db_session: Session) -> None:
    register_builtin_skills(db_session)
    db_session.commit()
    for skill in db_session.query(CaliberSkill).all():
        assert skill.name == skill.name.lower()  # kebab-case handle
        assert "_" not in skill.name and " " not in skill.name
        assert skill.summary and skill.content and skill.description
        assert skill.category in _VALID_CATEGORIES
        assert skill.tags  # at least one tag
        assert skill.visibility == "public"


def test_anthropic_skill_catalog_is_included(db_session: Session) -> None:
    register_builtin_skills(db_session)
    db_session.commit()
    rows = db_session.query(CaliberSkill).all()
    names = {row.name for row in rows}
    assert names >= _ANTHROPIC_SKILL_NAMES
    for row in rows:
        if row.name in _ANTHROPIC_SKILL_NAMES:
            assert row.skill_metadata["source"] == "anthropics/skills"
            assert row.skill_metadata["source_skill"] == row.name


def test_builtin_skills_listed_by_skills_endpoint(client: TestClient, db_session: Session) -> None:
    register_builtin_skills(db_session)
    db_session.commit()
    rows = client.get(f"{PREFIX}/skills").json()["data"]
    names = {r["name"] for r in rows}
    assert {"tool-grounding", "safe-refusal", "structured-output"} <= names


def test_builtin_skills_auto_seed_when_enabled(
    app_config: CaliberConfig,
    engine: Engine,
    session_factory: sessionmaker[Session],
) -> None:
    seeded_config = app_config.model_copy(update={"builtin_skills_auto_seed": True})
    app = create_app(config=seeded_config)
    app.state.engine = engine
    app.state.session_factory = session_factory
    with TestClient(app, headers={"X-CALIBER-User": "@test"}) as test_client:
        rows = test_client.get(LIST_PATH).json()["data"]
    names = {row["name"] for row in rows}
    assert {"tool-grounding", "docx", "webapp-testing"} <= names
