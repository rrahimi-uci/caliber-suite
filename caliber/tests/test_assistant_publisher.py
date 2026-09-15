"""Tests for caliber.assistant.publisher — AssistantPublisher."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from caliber.assistant.publisher import AssistantPublisher
from caliber.auth import CaliberIdentity

USER = "@test"
PROJECT_ID = "PRJ-assistant-publisher"


def _project_identity() -> CaliberIdentity:
    return CaliberIdentity(
        user_id=USER,
        scopes=frozenset({"caliber.operator"}),
        active_project_id=PROJECT_ID,
    )


@pytest.fixture
def publisher() -> AssistantPublisher:
    return AssistantPublisher()


class TestPublishTool:
    def test_publish_creates_tool_registry_row(
        self,
        publisher: AssistantPublisher,
        session_factory: sessionmaker[Session],
    ) -> None:
        result = publisher.publish(
            artifact_type="tool",
            artifact={"name": "greet", "source": "def greet(): pass", "input_schema": {}},
            draft_id="ADRF-test0001",
            session_factory=session_factory,
            user=USER,
        )
        assert result["success"]
        assert result["registry_id"].startswith("TL-")
        assert result["type"] == "tool"


class TestPublishSkill:
    def test_publish_creates_skill_row(
        self,
        publisher: AssistantPublisher,
        session_factory: sessionmaker[Session],
    ) -> None:
        result = publisher.publish(
            artifact_type="skill",
            artifact={"name": "reasoning", "prompt": "Think step by step."},
            draft_id="ADRF-test0002",
            session_factory=session_factory,
            user=USER,
        )
        assert result["success"]
        assert result["registry_id"].startswith("SK-")
        assert result["type"] == "skill"


class TestPublishPrompt:
    def test_publish_registers_prompt_version(
        self,
        publisher: AssistantPublisher,
        session_factory: sessionmaker[Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, object] = {}

        def fake_register_prompt_version(**kwargs: object) -> dict[str, object]:
            captured.update(kwargs)
            return {
                "name": kwargs["name"],
                "version": 7,
                "uri": "prompts:/greeting/7",
                "alias_changed": False,
            }

        monkeypatch.setattr(
            "caliber.routes.prompts.register_prompt_version",
            fake_register_prompt_version,
        )

        result = publisher.publish(
            artifact_type="prompt",
            artifact={"name": "greeting", "template": "Hello {{name}}"},
            draft_id="ADRF-test0003",
            session_factory=session_factory,
            user=USER,
        )
        assert result["success"]
        assert result["type"] == "prompt"
        assert result["registry_id"] == "greeting"
        assert result["version"] == 7
        assert result["target_version"] == "7"
        assert result["alias_changed"] is False
        assert captured["name"] == "greeting"
        assert captured["template"] == "Hello {{name}}"
        assert captured["source"] == "caliber-assistant"
        assert captured["set_prod_alias"] is False

    def test_publish_prompt_alias_uses_strict_alias_helper(
        self,
        publisher: AssistantPublisher,
        session_factory: sessionmaker[Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured_register: dict[str, object] = {}
        captured_alias: dict[str, object] = {}

        def fake_register_prompt_version(**kwargs: object) -> dict[str, object]:
            captured_register.update(kwargs)
            return {
                "name": kwargs["name"],
                "version": 8,
                "uri": "prompts:/greeting/8",
                "alias_changed": False,
            }

        def fake_set_prompt_alias_version(**kwargs: object) -> dict[str, object]:
            captured_alias.update(kwargs)
            return dict(kwargs)

        monkeypatch.setattr(
            "caliber.routes.prompts.register_prompt_version",
            fake_register_prompt_version,
        )
        monkeypatch.setattr(
            "caliber.routes.prompts.set_prompt_alias_version",
            fake_set_prompt_alias_version,
        )
        monkeypatch.setattr(
            "caliber.routes.prompts._load_prompt_release_info",
            lambda name, alias: {"artifact_ref": f"prompts:/{name}@{alias}", "version": 2},
        )

        result = publisher.publish(
            artifact_type="prompt",
            artifact={
                "name": "greeting",
                "template": "Hello {{name}}",
                "target_alias": "prod",
                "approval_id": "AP-approved",
            },
            draft_id="ADRF-test0003",
            session_factory=session_factory,
            user=USER,
        )

        assert result["success"] is True
        assert result["alias_changed"] is True
        assert result["target_alias"] == "prod"
        assert result["rollback_metadata"]["available"] is True
        assert captured_register["set_prod_alias"] is False
        assert captured_alias == {"name": "greeting", "alias": "prod", "version": 8}
        assert captured_register["tags"]["caliber.approval_id"] == "AP-approved"

    def test_publish_prompt_requires_name(
        self,
        publisher: AssistantPublisher,
        session_factory: sessionmaker[Session],
    ) -> None:
        result = publisher.publish(
            artifact_type="prompt",
            artifact={"template": "Hello {{name}}"},
            draft_id="ADRF-test0003",
            session_factory=session_factory,
            user=USER,
        )
        assert not result["success"]
        assert "name" in result["error"].lower()


class TestPublishWorkflow:
    def test_publish_creates_workflow_and_version(
        self,
        publisher: AssistantPublisher,
        session_factory: sessionmaker[Session],
    ) -> None:
        result = publisher.publish(
            artifact_type="workflow",
            artifact={"name": "my-flow", "description": "test", "manifest": {"steps": []}},
            draft_id="ADRF-test0004",
            session_factory=session_factory,
            user=USER,
        )
        assert result["success"]
        assert result["registry_id"].startswith("WF-")
        assert result["version_id"].startswith("WFV-")
        assert result["type"] == "workflow"


class TestPublishMcpServer:
    def test_publish_creates_mcp_server_row(
        self,
        publisher: AssistantPublisher,
        session_factory: sessionmaker[Session],
    ) -> None:
        result = publisher.publish(
            artifact_type="mcp_server",
            artifact={"name": "my-server", "transport": "stdio", "command": "npx my-server"},
            draft_id="ADRF-test0005",
            session_factory=session_factory,
            user=USER,
        )
        assert result["success"]
        assert result["registry_id"].startswith("MCP-")
        assert result["type"] == "mcp_server"


def test_project_identity_is_persisted_for_published_registries(
    publisher: AssistantPublisher,
    session_factory: sessionmaker[Session],
) -> None:
    """Route-backed publisher calls must not create invisible orphan rows."""
    identity = _project_identity()
    cases = (
        ("tool", {"name": "project-tool"}),
        ("skill", {"name": "project-skill", "prompt": "Use carefully."}),
        ("workflow", {"name": "project-workflow", "manifest": {"steps": []}}),
        (
            "mcp_server",
            {"name": "project-mcp", "transport": "stdio", "command": "project-mcp"},
        ),
    )

    results = {
        artifact_type: publisher.publish(
            artifact_type=artifact_type,
            artifact=artifact,
            draft_id=f"ADRF-{artifact_type}-project",
            session_factory=session_factory,
            user=USER,
            identity=identity,
        )
        for artifact_type, artifact in cases
    }

    assert all(result["success"] for result in results.values())
    with session_factory() as db:
        from caliber.db.models import (
            CaliberMcpServer,
            CaliberSkill,
            CaliberToolRegistry,
            CaliberWorkflow,
        )

        rows = (
            db.get(CaliberToolRegistry, results["tool"]["registry_id"]),
            db.get(CaliberSkill, results["skill"]["registry_id"]),
            db.get(CaliberWorkflow, results["workflow"]["registry_id"]),
            db.get(CaliberMcpServer, results["mcp_server"]["registry_id"]),
        )

    assert all(row is not None for row in rows)
    assert all(row.project_id == PROJECT_ID for row in rows if row is not None)
    assert all(row.visibility == "project" for row in rows if row is not None)
    assert all(row.owner == USER for row in rows if row is not None)


def test_project_prompt_publish_provisions_a_scoped_target_and_preflights_hidden_name(
    publisher: AssistantPublisher,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_register_prompt_version(**kwargs: object) -> dict[str, object]:
        calls.append(str(kwargs["name"]))
        return {"name": kwargs["name"], "version": 1, "uri": "prompts:/scoped-prompt/1"}

    monkeypatch.setattr(
        "caliber.routes.prompts.register_prompt_version",
        fake_register_prompt_version,
    )
    identity = _project_identity()
    result = publisher.publish(
        artifact_type="prompt",
        artifact={"name": "scoped-prompt", "template": "Answer {{question}}"},
        draft_id="ADRF-prompt-project",
        session_factory=session_factory,
        user=USER,
        identity=identity,
    )

    assert result["success"] is True
    with session_factory() as db:
        from caliber.db.models import CaliberAgentConfig

        target = db.get(CaliberAgentConfig, "scoped-prompt")
        assert target is not None
        assert target.owner == USER
        assert target.project_id == PROJECT_ID
        assert target.visibility == "project"
        target = CaliberAgentConfig(
            agent_id="hidden-prompt",
            experiment_id="exp-hidden-prompt",
            name="hidden-prompt",
            owner="@other",
            project_id="PRJ-hidden",
            visibility="project",
        )
        db.add(target)
        db.commit()

    refused = publisher.publish(
        artifact_type="prompt",
        artifact={"name": "hidden-prompt", "template": "must not write"},
        draft_id="ADRF-prompt-hidden",
        session_factory=session_factory,
        user=USER,
        identity=identity,
    )
    assert refused["success"] is False
    assert "not found" in refused["error"]
    assert calls == ["scoped-prompt"]


class TestPublishUnknownType:
    def test_returns_error(
        self,
        publisher: AssistantPublisher,
        session_factory: sessionmaker[Session],
    ) -> None:
        result = publisher.publish(
            artifact_type="unknown",
            artifact={},
            draft_id="ADRF-test0006",
            session_factory=session_factory,
            user=USER,
        )
        assert not result["success"]
        assert "unknown" in result["error"].lower()
