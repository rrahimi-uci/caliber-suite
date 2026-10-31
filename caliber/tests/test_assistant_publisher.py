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
    return AssistantPublisher(reference_profile=False)


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

    @pytest.mark.parametrize(
        "name",
        [
            "grep_files",
            "read_text_file",
            "sandbox_python",
            "write_workdir_file",
            "memory_add",
            "extract_document",
            "Grep_Files",
            "  grep_files  ",
            "tool.grep_files.v2",
            "grep_files.v3",
        ],
    )
    def test_a_built_in_tool_name_is_refused_and_nothing_is_registered(
        self,
        publisher: AssistantPublisher,
        session_factory: sessionmaker[Session],
        name: str,
    ) -> None:
        from caliber.db.models import CaliberToolRegistry

        result = publisher.publish(
            artifact_type="tool",
            artifact={"name": name, "module_path": "caliber.workflows.demo_tools"},
            draft_id="ADRF-reserved",
            session_factory=session_factory,
            user=USER,
        )
        assert result["success"] is False
        assert result["code"] == "reserved_tool_name"
        assert "reserved_tool_name:" in result["error"]
        assert "built-in CALIBER tool" in result["error"]
        assert "different name" in result["error"], "the refusal says how to recover"
        assert "registry_id" not in result
        with session_factory() as db:
            assert db.query(CaliberToolRegistry).count() == 0

    def test_the_refusal_names_the_built_in_the_name_collides_with(
        self,
        publisher: AssistantPublisher,
        session_factory: sessionmaker[Session],
    ) -> None:
        result = publisher.publish(
            artifact_type="tool",
            artifact={"name": "tool.Grep_Files.v2"},
            draft_id="ADRF-reserved-name",
            session_factory=session_factory,
            user=USER,
        )
        assert "'tool.Grep_Files.v2'" in result["error"]
        assert "'grep_files'" in result["error"]

    def test_a_built_in_name_is_refused_for_an_identity_owned_publish_too(
        self,
        publisher: AssistantPublisher,
        session_factory: sessionmaker[Session],
    ) -> None:
        """Even a publisher whose user id is ``@caliber`` (the built-ins' owner) cannot take the name."""
        identity = CaliberIdentity(user_id="@caliber", scopes=frozenset({"caliber.admin"}))
        result = publisher.publish(
            artifact_type="tool",
            artifact={"name": "grep_files", "module_path": "caliber.workflows.demo_tools"},
            draft_id="ADRF-spoof",
            session_factory=session_factory,
            user="@caliber",
            identity=identity,
        )
        assert result["success"] is False
        assert result["code"] == "reserved_tool_name"

    @pytest.mark.parametrize("name", ["grep_files_v2", "my_grep_files", "grep-files", "greet"])
    def test_a_name_that_only_resembles_a_built_in_is_published(
        self,
        publisher: AssistantPublisher,
        session_factory: sessionmaker[Session],
        name: str,
    ) -> None:
        result = publisher.publish(
            artifact_type="tool",
            artifact={"name": name},
            draft_id="ADRF-near",
            session_factory=session_factory,
            user=USER,
        )
        assert result["success"] is True
        with session_factory() as db:
            from caliber.db.models import CaliberToolRegistry

            assert db.get(CaliberToolRegistry, result["registry_id"]).name == name

    def test_other_artifact_types_may_use_a_built_in_tool_name(
        self,
        publisher: AssistantPublisher,
        session_factory: sessionmaker[Session],
    ) -> None:
        """The reservation is for the tool registry, where the name is an identity."""
        result = publisher.publish(
            artifact_type="skill",
            artifact={"name": "grep_files", "prompt": "Use carefully."},
            draft_id="ADRF-skill",
            session_factory=session_factory,
            user=USER,
        )
        assert result["success"] is True


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


class TestPublishWorkflowExperimentClaims:
    """A published workflow's ``default_experiment_id`` is a claim like any other: ownership is decided on the
    experiment the tracking backend resolves it to, and an unresolvable one is not guessed."""

    @staticmethod
    def _foreign_agent(session_factory: sessionmaker[Session], experiment_ref: str) -> None:
        from caliber.db.models import CaliberAgentConfig

        with session_factory() as db:
            db.add(
                CaliberAgentConfig(
                    agent_id="agent-foreign",
                    experiment_id=experiment_ref,
                    name="foreign",
                    owner="@someone-else",
                    project_id="PRJ-someone-else",
                    visibility="project",
                    artifact_types=["prompt"],
                    optimizer_config={},
                )
            )
            db.commit()

    @staticmethod
    def _publish(publisher: AssistantPublisher, session_factory: sessionmaker[Session], ref: str):
        return publisher.publish(
            artifact_type="workflow",
            artifact={"name": "claimer", "manifest": {"steps": []}, "default_experiment_id": ref},
            draft_id="ADRF-claim",
            session_factory=session_factory,
            user=USER,
            identity=_project_identity(),
        )

    def test_an_alias_of_another_projects_experiment_is_refused(
        self,
        publisher: AssistantPublisher,
        session_factory: sessionmaker[Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import types

        import mlflow

        self._foreign_agent(session_factory, "billing-exp")
        monkeypatch.setattr(
            mlflow,
            "get_experiment_by_name",
            lambda name: (
                types.SimpleNamespace(experiment_id="7") if name == "billing-exp" else None
            ),
        )
        monkeypatch.setattr(
            mlflow,
            "get_experiment",
            lambda experiment_id: types.SimpleNamespace(experiment_id=experiment_id),
        )
        refused = self._publish(publisher, session_factory, "7")
        assert not refused["success"]
        assert "already claimed" in refused["error"]

    def test_an_unresolvable_experiment_is_not_claimed(
        self,
        publisher: AssistantPublisher,
        session_factory: sessionmaker[Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import mlflow

        def backend_down(name: str) -> None:
            raise ConnectionError("tracking server unreachable")

        monkeypatch.setattr(mlflow, "get_experiment_by_name", backend_down)
        refused = self._publish(publisher, session_factory, "some-experiment")
        assert not refused["success"]
        assert "was not claimed" in refused["error"]
        with session_factory() as db:
            from caliber.db.models import CaliberWorkflow

            assert db.query(CaliberWorkflow).filter_by(name="claimer").count() == 0


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


class TestTrustedConnectorNamesAreReserved:
    @pytest.mark.parametrize(
        "name", ["document_request", "document-request", "Document_Request", "tool.http.v1"]
    )
    def test_a_tool_draft_named_like_a_connector_is_refused_and_nothing_is_registered(
        self,
        publisher: AssistantPublisher,
        session_factory: sessionmaker[Session],
        name: str,
    ) -> None:
        from caliber.db.models import CaliberToolRegistry

        result = publisher.publish(
            artifact_type="tool",
            artifact={"name": name},
            draft_id="ADRF-connector-name",
            session_factory=session_factory,
            user=USER,
        )
        assert result["success"] is False
        assert result["code"] == "reserved_tool_name"
        assert "trusted connector" in result["error"]
        assert "different name" in result["error"]
        with session_factory() as db:
            assert db.query(CaliberToolRegistry).count() == 0
