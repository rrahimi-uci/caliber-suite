"""Tests for the assistant's read-only registry tool dispatcher."""

from __future__ import annotations

import json

from sqlalchemy.orm import Session, sessionmaker

from caliber.assistant.tools import RegistryToolDispatcher
from caliber.db.models import CaliberSkill, CaliberToolRegistry


def _seed(session: Session) -> None:
    session.add(
        CaliberSkill(
            skill_id="SK-1",
            name="grounding-guard",
            description="",
            summary="Keeps answers grounded.",
            content="When asked about policy, cite the source.",
            owner="@o",
            category="safety",
            tags=[],
            status="active",
            version=2,
        )
    )
    session.add(
        CaliberSkill(
            skill_id="SK-old",
            name="retired",
            description="",
            content="x",
            owner="@o",
            category="other",
            tags=[],
            status="archived",
            version=1,
        )
    )
    session.add(
        CaliberToolRegistry(
            tool_id="T-1",
            name="search_kb",
            version="1",
            description="Search the KB.",
            module_path="m",
            callable_name="c",
        )
    )
    session.commit()


def test_specs_advertise_the_read_only_tools(session_factory: sessionmaker[Session]) -> None:
    names = {s["function"]["name"] for s in RegistryToolDispatcher(session_factory).specs()}
    assert names == {"list_skills", "get_skill", "list_tools"}


def test_list_skills_returns_only_active(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as s:
        _seed(s)
    out = json.loads(RegistryToolDispatcher(session_factory).dispatch("list_skills", {}))
    names = {row["name"] for row in out}
    assert names == {"grounding-guard"}  # archived skill excluded
    assert out[0]["summary"] == "Keeps answers grounded."


def test_get_skill_returns_content(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as s:
        _seed(s)
    out = json.loads(
        RegistryToolDispatcher(session_factory).dispatch("get_skill", {"name": "grounding-guard"})
    )
    assert out["content"] == "When asked about policy, cite the source."
    assert out["version"] == 2


def test_get_skill_missing_is_a_clean_error(session_factory: sessionmaker[Session]) -> None:
    out = json.loads(
        RegistryToolDispatcher(session_factory).dispatch("get_skill", {"name": "nope"})
    )
    assert "error" in out


def test_list_tools_returns_registry(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as s:
        _seed(s)
    out = json.loads(RegistryToolDispatcher(session_factory).dispatch("list_tools", {}))
    assert {row["name"] for row in out} == {"search_kb"}


def test_unknown_tool_is_a_clean_error(session_factory: sessionmaker[Session]) -> None:
    out = json.loads(RegistryToolDispatcher(session_factory).dispatch("frobnicate", {}))
    assert "error" in out
