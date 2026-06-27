"""Tests for caliber.routes.assistant — HTTP integration tests.

These tests exercise the full route → service → DB round-trip via the
Starlette TestClient, mirroring the pattern used by the rest of the
CALIBER route test suite.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import caliber.routes.assistant as assistant_routes
from caliber.config import CaliberConfig
from caliber.db.models import CaliberAssistantSession
from caliber.server import create_app
from tests.workflow_helpers import make_manifest

PREFIX = "/ajax-api/2.0/mlflow/caliber/assistant"
CALIBER_PREFIX = "/ajax-api/2.0/mlflow/caliber"


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class TestSessionRoutes:
    def test_create_session(self, client: TestClient) -> None:
        resp = client.post(
            f"{PREFIX}/sessions",
            json={
                "title": "Route test",
                "goal": "Build a skill",
                "metadata_": {"prompt_ref": "prompts:/support-agent@prod"},
            },
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["session_id"].startswith("ASST-")
        assert data["title"] == "Route test"
        assert data["metadata_"]["prompt_ref"] == "prompts:/support-agent@prod"

    def test_list_sessions(self, client: TestClient) -> None:
        client.post(f"{PREFIX}/sessions", json={"title": "A"})
        resp = client.get(f"{PREFIX}/sessions")
        assert resp.status_code == 200
        assert isinstance(resp.json()["data"], list)

    def test_prompt_draft_returns_assistant_seed(self, client: TestClient) -> None:
        resp = client.post(
            f"{PREFIX}/prompt-draft",
            json={"description": "Classify support tickets into billing or technical."},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        # The fake engine drafts a prompt artifact; the seed carries its template.
        assert data["template"] == "Hello, {{name}}!"
        assert data["name"] == "fake_prompt"
        assert data["variables"] == ["name"]

    def test_prompt_draft_requires_description(self, client: TestClient) -> None:
        resp = client.post(f"{PREFIX}/prompt-draft", json={"description": "   "})
        assert resp.status_code == 400

    def test_get_session(self, client: TestClient) -> None:
        create = client.post(f"{PREFIX}/sessions", json={"title": "detail"})
        sid = create.json()["data"]["session_id"]
        resp = client.get(f"{PREFIX}/sessions/{sid}")
        assert resp.status_code == 200
        assert resp.json()["data"]["session_id"] == sid

    def test_get_session_404(self, client: TestClient) -> None:
        resp = client.get(f"{PREFIX}/sessions/ASST-00000000")
        assert resp.status_code == 404

    def test_patch_session(self, client: TestClient) -> None:
        create = client.post(f"{PREFIX}/sessions", json={"title": "old"})
        sid = create.json()["data"]["session_id"]
        resp = client.patch(
            f"{PREFIX}/sessions/{sid}",
            json={"title": "new", "metadata_": {"prompt_alias": "prod"}},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["title"] == "new"
        assert resp.json()["data"]["metadata_"]["prompt_alias"] == "prod"

    def test_cross_user_get_session_returns_404(self, client: TestClient) -> None:
        create = client.post(
            f"{PREFIX}/sessions",
            json={"title": "private"},
            headers={"X-CALIBER-User": "@a"},
        )
        assert create.status_code == 201
        sid = create.json()["data"]["session_id"]
        resp = client.get(f"{PREFIX}/sessions/{sid}", headers={"X-CALIBER-User": "@b"})
        assert resp.status_code == 404

    def test_owner_filter_requires_admin_for_other_owner(self, client: TestClient) -> None:
        resp = client.get(
            f"{PREFIX}/sessions?owner=@test",
            headers={"X-CALIBER-User": "@viewer"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


class TestMessageRoutes:
    def _session_id(self, client: TestClient) -> str:
        resp = client.post(f"{PREFIX}/sessions", json={"title": "msg-test"})
        return resp.json()["data"]["session_id"]

    def test_send_and_list_messages(self, client: TestClient) -> None:
        sid = self._session_id(client)
        send = client.post(f"{PREFIX}/sessions/{sid}/messages", json={"content": "hello"})
        assert send.status_code == 201
        data = send.json()["data"]
        assert data["assistant_message"]["role"] == "assistant"

        msgs = client.get(f"{PREFIX}/sessions/{sid}/messages")
        assert msgs.status_code == 200
        assert len(msgs.json()["data"]) >= 2

    def test_cross_user_list_messages_returns_404(self, client: TestClient) -> None:
        create = client.post(
            f"{PREFIX}/sessions",
            json={"title": "private-messages"},
            headers={"X-CALIBER-User": "@a"},
        )
        sid = create.json()["data"]["session_id"]
        resp = client.get(f"{PREFIX}/sessions/{sid}/messages", headers={"X-CALIBER-User": "@b"})
        assert resp.status_code == 404

    def test_send_empty_message_rejected(self, client: TestClient) -> None:
        sid = self._session_id(client)
        resp = client.post(f"{PREFIX}/sessions/{sid}/messages", json={"content": ""})
        assert resp.status_code in (400, 422)

    def test_send_message_persists_project_aware_task_context(
        self,
        client: TestClient,
        db_session,
    ) -> None:
        sid = self._session_id(client)
        resp = client.post(
            f"{PREFIX}/sessions/{sid}/messages",
            headers={"X-CALIBER-Project": "PRJ-19"},
            json={
                "content": "Resume this plan",
                "constraints": {"must_test": True},
                "done_when": ["all checks green"],
                "context_refs": [
                    {"ref_type": "workflow", "ref_id": "WF-1", "label": "Support Flow"}
                ],
                "selected_resources": [
                    {
                        "ref_type": "knowledge_base",
                        "ref_id": "KB-1",
                        "label": "Support KB",
                    }
                ],
                "resume_from_plan_id": "PLAN-42",
            },
        )
        assert resp.status_code == 201, resp.text

        session_row = db_session.get(CaliberAssistantSession, sid)
        assert session_row is not None
        stored = session_row.metadata_["assistant_task_context"]
        assert stored["project_id"] == "PRJ-19"
        assert stored["current_surface"] == "assistant_drawer"
        assert stored["constraints"] == {"must_test": True}
        assert stored["done_when"] == ["all checks green"]
        assert stored["context_refs"][0]["ref_id"] == "WF-1"
        assert stored["selected_resources"][0]["ref_id"] == "KB-1"
        assert stored["resume_from_plan_id"] == "PLAN-42"
        assert "scopes" not in stored
        assert "task_kind" not in stored


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------


class TestDraftRoutes:
    def _draft_id(self, client: TestClient) -> tuple[str, str]:
        sid = client.post(f"{PREFIX}/sessions", json={"title": "draft-test"}).json()["data"][
            "session_id"
        ]
        client.post(f"{PREFIX}/sessions/{sid}/messages", json={"content": "create a tool"})
        turn2 = client.post(f"{PREFIX}/sessions/{sid}/messages", json={"content": "name it foo"})
        data = turn2.json()["data"]
        drafts = data.get("draft_updates", [])
        if not drafts:
            # If no draft in turn response, list them explicitly
            listed = client.get(f"{PREFIX}/sessions/{sid}/drafts").json()["data"]
            if listed:
                return sid, listed[0]["draft_id"]
            pytest.skip("FakeEngine did not produce a draft")
        return sid, drafts[0]["draft_id"]

    def test_list_drafts(self, client: TestClient) -> None:
        sid, _ = self._draft_id(client)
        resp = client.get(f"{PREFIX}/sessions/{sid}/drafts")
        assert resp.status_code == 200
        assert isinstance(resp.json()["data"], list)

    def test_cross_user_list_drafts_returns_404(self, client: TestClient) -> None:
        create = client.post(
            f"{PREFIX}/sessions",
            json={"title": "private-drafts"},
            headers={"X-CALIBER-User": "@a"},
        )
        sid = create.json()["data"]["session_id"]
        resp = client.get(f"{PREFIX}/sessions/{sid}/drafts", headers={"X-CALIBER-User": "@b"})
        assert resp.status_code == 404

    def test_get_draft(self, client: TestClient) -> None:
        _, draft_id = self._draft_id(client)
        resp = client.get(f"{PREFIX}/drafts/{draft_id}")
        assert resp.status_code == 200
        assert resp.json()["data"]["draft_id"] == draft_id

    def test_get_draft_404(self, client: TestClient) -> None:
        resp = client.get(f"{PREFIX}/drafts/ADRF-00000000")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Draft actions
# ---------------------------------------------------------------------------


class TestDraftActionRoutes:
    def _approved_draft(self, client: TestClient) -> str:
        sid = client.post(f"{PREFIX}/sessions", json={"title": "action"}).json()["data"][
            "session_id"
        ]
        client.post(f"{PREFIX}/sessions/{sid}/messages", json={"content": "create a tool"})
        t2 = client.post(f"{PREFIX}/sessions/{sid}/messages", json={"content": "name it foo"})
        drafts = t2.json()["data"].get("draft_updates", [])
        if not drafts:
            listed = client.get(f"{PREFIX}/sessions/{sid}/drafts").json()["data"]
            if not listed:
                pytest.skip("FakeEngine did not produce a draft")
            draft_id = listed[0]["draft_id"]
        else:
            draft_id = drafts[0]["draft_id"]
        client.post(f"{PREFIX}/drafts/{draft_id}/approve")
        return draft_id

    def test_validate_draft(self, client: TestClient) -> None:
        sid = client.post(f"{PREFIX}/sessions", json={"title": "val"}).json()["data"]["session_id"]
        client.post(f"{PREFIX}/sessions/{sid}/messages", json={"content": "create a tool"})
        t2 = client.post(f"{PREFIX}/sessions/{sid}/messages", json={"content": "name it foo"})
        drafts = t2.json()["data"].get("draft_updates", [])
        if not drafts:
            pytest.skip("No draft produced")
        draft_id = drafts[0]["draft_id"]
        resp = client.post(f"{PREFIX}/drafts/{draft_id}/validate")
        assert resp.status_code == 200
        assert "valid" in resp.json()["data"]

    def test_test_draft(self, client: TestClient) -> None:
        sid = client.post(f"{PREFIX}/sessions", json={"title": "tst"}).json()["data"]["session_id"]
        client.post(f"{PREFIX}/sessions/{sid}/messages", json={"content": "create a tool"})
        t2 = client.post(f"{PREFIX}/sessions/{sid}/messages", json={"content": "name it foo"})
        drafts = t2.json()["data"].get("draft_updates", [])
        if not drafts:
            pytest.skip("No draft produced")
        draft_id = drafts[0]["draft_id"]
        resp = client.post(f"{PREFIX}/drafts/{draft_id}/test")
        assert resp.status_code == 200
        assert "passed" in resp.json()["data"]

    def test_approve_draft(self, client: TestClient) -> None:
        sid = client.post(f"{PREFIX}/sessions", json={"title": "appr"}).json()["data"]["session_id"]
        client.post(f"{PREFIX}/sessions/{sid}/messages", json={"content": "create a tool"})
        t2 = client.post(f"{PREFIX}/sessions/{sid}/messages", json={"content": "name it foo"})
        drafts = t2.json()["data"].get("draft_updates", [])
        if not drafts:
            pytest.skip("No draft produced")
        draft_id = drafts[0]["draft_id"]
        resp = client.post(f"{PREFIX}/drafts/{draft_id}/approve")
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "approved"

    def test_publish_draft(self, client: TestClient) -> None:
        draft_id = self._approved_draft(client)
        resp = client.post(f"{PREFIX}/drafts/{draft_id}/publish")
        assert resp.status_code == 200
        assert resp.json()["data"]["success"]


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


class TestRunRoutes:
    def test_get_run_from_send(self, client: TestClient) -> None:
        sid = client.post(f"{PREFIX}/sessions", json={"title": "run"}).json()["data"]["session_id"]
        turn = client.post(f"{PREFIX}/sessions/{sid}/messages", json={"content": "hi"})
        run = turn.json()["data"].get("run")
        if run is None:
            pytest.skip("No run returned")
        run_id = run["run_id"]
        resp = client.get(f"{PREFIX}/runs/{run_id}")
        assert resp.status_code == 200
        assert resp.json()["data"]["run_id"] == run_id

    def test_send_message_run_uses_request_trace_id(self, client: TestClient) -> None:
        sid = client.post(f"{PREFIX}/sessions", json={"title": "trace"}).json()["data"][
            "session_id"
        ]
        turn = client.post(
            f"{PREFIX}/sessions/{sid}/messages",
            json={"content": "hi"},
            headers={"X-Trace-Id": "route-trace-123"},
        )
        assert turn.status_code == 201
        run = turn.json()["data"]["run"]
        assert run["trace_id"] == "route-trace-123"

    def test_get_run_404(self, client: TestClient) -> None:
        resp = client.get(f"{PREFIX}/runs/ARN-00000000")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Auth / RBAC
# ---------------------------------------------------------------------------


class TestAssistantAuth:
    def test_no_user_header_returns_401(self, client: TestClient) -> None:
        resp = client.get(
            f"{PREFIX}/sessions",
            headers={"X-CALIBER-User": ""},
        )
        assert resp.status_code == 401

    def test_config_update_requires_operator(self, client: TestClient) -> None:
        resp = client.patch(
            f"{PREFIX}/config",
            json={"model": "gpt-4o"},
            headers={"X-CALIBER-User": "@viewer"},
        )
        assert resp.status_code == 403

    def test_config_can_update_disabled_intents(self, client: TestClient) -> None:
        initial = client.get(f"{PREFIX}/config")
        assert initial.status_code == 200
        assert initial.json()["data"]["disabled_intents"] == []
        assert initial.json()["data"]["disabled_domains"] == []

        resp = client.patch(
            f"{PREFIX}/config",
            json={"disabled_intents": ["generate_test_cases"], "disabled_domains": ["tool"]},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["disabled_intents"] == ["generate_test_cases"]
        assert resp.json()["data"]["disabled_domains"] == ["tool"]

        app = client.app
        assert app.state.assistant_service._settings.disabled_intents == ("generate_test_cases",)
        assert app.state.assistant_service._settings.disabled_domains == ("tool",)

    def test_config_can_update_reasoning_without_resending_model(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rebuild_calls: list[tuple[str, str, str]] = []

        monkeypatch.setattr(
            assistant_routes,
            "_rebuild_engine",
            lambda app, provider, model, reasoning: rebuild_calls.append(
                (provider, model, reasoning)
            ),
        )
        client.app.state._assistant_overrides = {
            "engine": "openai",
            "model": "gpt-5.2",
            "reasoning": "",
        }

        resp = client.patch(f"{PREFIX}/config", json={"reasoning": "high"})

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["engine"] == "openai"
        assert data["model"] == "gpt-5.2"
        assert data["provider"] == "openai"
        assert data["reasoning"] == "high"
        assert rebuild_calls == [("openai", "gpt-5.2", "high")]

    def test_config_includes_discovered_ollama_models(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            assistant_routes,
            "_list_ollama_models",
            lambda: [{"id": "qwen2.5:7b", "name": "qwen2.5:7b", "provider": "ollama"}],
        )

        resp = client.get(f"{PREFIX}/config")

        assert resp.status_code == 200
        models = resp.json()["data"]["available_models"]
        assert any(
            model["id"] == "qwen2.5:7b" and model["provider"] == "ollama" for model in models
        )

    def test_config_can_switch_to_discovered_ollama_model(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            assistant_routes,
            "_list_ollama_models",
            lambda: [{"id": "qwen2.5:7b", "name": "qwen2.5:7b", "provider": "ollama"}],
        )

        resp = client.patch(f"{PREFIX}/config", json={"model": "qwen2.5:7b"})

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["model"] == "qwen2.5:7b"
        assert data["provider"] == "ollama"
        assert data["engine"] == "ollama"

    def test_config_preserves_selected_ollama_model_when_discovery_is_unavailable(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            assistant_routes,
            "_list_ollama_models",
            lambda: [{"id": "qwen2.5:7b", "name": "qwen2.5:7b", "provider": "ollama"}],
        )

        update = client.patch(f"{PREFIX}/config", json={"model": "qwen2.5:7b"})
        assert update.status_code == 200

        monkeypatch.setattr(assistant_routes, "_list_ollama_models", lambda: [])

        resp = client.get(f"{PREFIX}/config")

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["model"] == "qwen2.5:7b"
        assert data["provider"] == "ollama"
        assert any(model["id"] == "qwen2.5:7b" for model in data["available_models"])

    def test_config_rejects_unknown_disabled_intent(self, client: TestClient) -> None:
        resp = client.patch(
            f"{PREFIX}/config",
            json={"disabled_intents": ["publish_mcp_server"]},
        )
        assert resp.status_code == 400
        assert "unknown assistant intent" in resp.json()["detail"]

    def test_config_rejects_unknown_disabled_domain(self, client: TestClient) -> None:
        resp = client.patch(
            f"{PREFIX}/config",
            json={"disabled_domains": ["billing"]},
        )
        assert resp.status_code == 400
        assert "unknown assistant domain" in resp.json()["detail"]

    def test_disabled_assistant_routes_return_503(self, tmp_path) -> None:
        config = CaliberConfig.load(
            environ={
                "CALIBER_DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'disabled.db'}",
                "CALIBER_ASSISTANT_ENABLED": "false",
                "CALIBER_ASSISTANT_DISABLED_INTENTS": "generate_test_cases",
                "CALIBER_ASSISTANT_DISABLED_DOMAINS": "prompt",
                "CALIBER_ADMIN_USERS": "@test",
                "CALIBER_BACKGROUND_TASKS_ENABLED": "false",
            }
        )
        app = create_app(config=config)
        with TestClient(app, headers={"X-CALIBER-User": "@test"}) as disabled_client:
            config_resp = disabled_client.get(f"{PREFIX}/config")
            assert config_resp.status_code == 200
            assert config_resp.json()["data"]["enabled"] is False
            assert config_resp.json()["data"]["disabled_intents"] == ["generate_test_cases"]
            assert config_resp.json()["data"]["disabled_domains"] == ["prompt"]

            resp = disabled_client.get(f"{PREFIX}/sessions")
            assert resp.status_code == 503
            assert resp.json()["detail"] == "Assistant disabled."


class TestIntentRoutes:
    def _session_id(self, client: TestClient) -> str:
        return client.post(
            f"{PREFIX}/sessions",
            json={"title": "intent", "metadata_": {"prompt_ref": "prompts:/support-agent@prod"}},
        ).json()["data"]["session_id"]

    def test_resolve_and_plan(self, client: TestClient) -> None:
        sid = self._session_id(client)

        resolved = client.post(
            f"{PREFIX}/sessions/{sid}/intent/resolve",
            json={"content": "optimize prompt support-agent with dataset ED-qa123"},
        )
        assert resolved.status_code == 200
        assert resolved.json()["data"]["intent"]["name"] == "run_prompt_optimization"

        planned = client.post(
            f"{PREFIX}/sessions/{sid}/plans",
            json={},
        )
        assert planned.status_code == 201
        plan_data = planned.json()["data"]
        assert plan_data["intent"]["name"] == "run_prompt_optimization"
        assert "plan_id" in plan_data

        latest = client.get(f"{PREFIX}/sessions/{sid}/plans/latest")
        assert latest.status_code == 200
        assert latest.json()["data"]["plan_id"] == plan_data["plan_id"]

    def test_execute_plan_create_prompt(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sid = self._session_id(client)

        from caliber.routes import prompts as prompt_routes

        monkeypatch.setattr(
            prompt_routes,
            "register_prompt_version",
            lambda **_kwargs: {
                "name": "support-agent",
                "version": 3,
                "uri": "prompts:/support-agent/3",
                "template_preview": "x",
                "template_length": 1,
            },
        )

        planned = client.post(
            f"{PREFIX}/sessions/{sid}/plans",
            json={
                "intent_name": "create_prompt",
                "slot_overrides": {
                    "prompt_name": "support-agent",
                    "template": "You are support.",
                },
            },
        )
        assert planned.status_code == 201
        plan_id = planned.json()["data"]["plan_id"]

        executed = client.post(
            f"{PREFIX}/sessions/{sid}/plans/execute",
            json={"plan_id": plan_id, "confirm": True},
        )
        assert executed.status_code == 201
        payload = executed.json()["data"]
        assert payload["status"] == "completed"
        assert payload["executed_action"] == "register_prompt"

        operation_id = payload["operation_id"]
        operation = client.get(f"{PREFIX}/sessions/{sid}/operations/{operation_id}")
        assert operation.status_code == 200
        assert operation.json()["data"]["status"] == "completed"

    def test_execute_plan_create_tool(self, client: TestClient) -> None:
        sid = self._session_id(client)
        planned = client.post(
            f"{PREFIX}/sessions/{sid}/plans",
            json={
                "intent_name": "create_tool",
                "slot_overrides": {
                    "tool_name": "double_tool",
                    "source": "def double_tool(x: int) -> dict:\n    return {'value': x * 2}\n",
                    "callable_name": "double_tool",
                    "input_schema": {
                        "type": "object",
                        "properties": {"x": {"type": "integer"}},
                    },
                    "tests": [
                        {
                            "name": "doubles two",
                            "input": {"x": 2},
                            "expected": {"value": 4},
                        }
                    ],
                },
            },
        )
        assert planned.status_code == 201
        plan_data = planned.json()["data"]
        assert plan_data["ready"] is True
        assert plan_data["actions"][0]["action"] == "create_validate_test_tool_draft"

        executed = client.post(
            f"{PREFIX}/sessions/{sid}/plans/execute",
            json={"plan_id": plan_data["plan_id"], "confirm": True},
        )
        assert executed.status_code == 201
        payload = executed.json()["data"]
        assert payload["executed_action"] == "create_validate_test_tool_draft"
        assert payload["result"]["result_type"] == "tool_draft"
        assert payload["result"]["status"] == "completed"
        assert payload["result"]["test_report"]["passed"] is True
        draft_id = payload["result"]["ids"]["draft_id"]

        draft = client.get(f"{PREFIX}/drafts/{draft_id}")
        assert draft.status_code == 200
        assert draft.json()["data"]["status"] == "tested"

        tested = client.post(f"{PREFIX}/drafts/{draft_id}/test")
        assert tested.status_code == 200
        assert tested.json()["data"]["passed"] is True
        assert tested.json()["data"]["details"][0]["name"] == "doubles two"

    def test_execute_plan_create_skill(self, client: TestClient) -> None:
        sid = self._session_id(client)
        planned = client.post(
            f"{PREFIX}/sessions/{sid}/plans",
            json={
                "intent_name": "create_skill",
                "slot_overrides": {
                    "skill_name": "support-triage",
                    "description": "Use when a support ticket needs triage.",
                    "summary": "Classifies support tickets before handoff.",
                    "content": "Classify urgency, summarize context, and recommend the next owner.",
                    "category": "workflow_automation",
                    "tags": ["assistant-generated", "support"],
                },
            },
        )
        assert planned.status_code == 201
        plan_data = planned.json()["data"]
        assert plan_data["ready"] is True
        assert plan_data["actions"][0]["action"] == "create_validate_package_skill_draft"

        executed = client.post(
            f"{PREFIX}/sessions/{sid}/plans/execute",
            json={"plan_id": plan_data["plan_id"], "confirm": True},
        )
        assert executed.status_code == 201
        payload = executed.json()["data"]
        assert payload["executed_action"] == "create_validate_package_skill_draft"
        assert payload["result"]["result_type"] == "skill_draft"
        assert payload["result"]["status"] == "completed"
        assert payload["result"]["test_report"]["passed"] is True
        draft_id = payload["result"]["ids"]["draft_id"]

        draft = client.get(f"{PREFIX}/drafts/{draft_id}")
        assert draft.status_code == 200
        assert draft.json()["data"]["artifact_type"] == "skill"
        assert draft.json()["data"]["status"] == "tested"

        tested = client.post(f"{PREFIX}/drafts/{draft_id}/test")
        assert tested.status_code == 200
        assert tested.json()["data"]["passed"] is True
        assert tested.json()["data"]["details"][0]["test"] == "package_build"

    def test_execute_plan_create_workflow(self, client: TestClient) -> None:
        sid = self._session_id(client)
        planned = client.post(
            f"{PREFIX}/sessions/{sid}/plans",
            json={
                "intent_name": "create_workflow",
                "slot_overrides": {
                    "workflow_name": "Support Triage",
                    "description": "Route a support request through a single agent.",
                    "manifest": make_manifest("support_triage", name="Support Triage"),
                },
            },
        )
        assert planned.status_code == 201
        plan_data = planned.json()["data"]
        assert plan_data["ready"] is True
        assert plan_data["actions"][0]["action"] == "create_validate_compile_workflow_draft"

        executed = client.post(
            f"{PREFIX}/sessions/{sid}/plans/execute",
            json={"plan_id": plan_data["plan_id"], "confirm": True},
        )
        assert executed.status_code == 201
        payload = executed.json()["data"]
        assert payload["executed_action"] == "create_validate_compile_workflow_draft"
        assert payload["result"]["result_type"] == "workflow_draft"
        assert payload["result"]["status"] == "completed"
        assert payload["result"]["test_report"]["passed"] is True
        draft_id = payload["result"]["ids"]["draft_id"]

        draft = client.get(f"{PREFIX}/drafts/{draft_id}")
        assert draft.status_code == 200
        assert draft.json()["data"]["artifact_type"] == "workflow"
        assert draft.json()["data"]["status"] == "tested"

        tested = client.post(f"{PREFIX}/drafts/{draft_id}/test")
        assert tested.status_code == 200
        assert tested.json()["data"]["passed"] is True
        assert tested.json()["data"]["details"][0]["test"] == "workflow_compile"

    def test_execute_plan_create_mcp_server(self, client: TestClient) -> None:
        sid = self._session_id(client)
        planned = client.post(
            f"{PREFIX}/sessions/{sid}/plans",
            json={
                "intent_name": "create_mcp_server",
                "slot_overrides": {
                    "server_name": "filesystem",
                    "description": "Local filesystem MCP server.",
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                    "discovered_tools": [{"name": "read_file", "description": "Read a file"}],
                },
            },
        )
        assert planned.status_code == 201
        plan_data = planned.json()["data"]
        assert plan_data["ready"] is True
        assert plan_data["actions"][0]["action"] == "create_validate_test_mcp_server_draft"

        executed = client.post(
            f"{PREFIX}/sessions/{sid}/plans/execute",
            json={"plan_id": plan_data["plan_id"], "confirm": True},
        )
        assert executed.status_code == 201
        payload = executed.json()["data"]
        assert payload["executed_action"] == "create_validate_test_mcp_server_draft"
        assert payload["result"]["result_type"] == "mcp_server_draft"
        assert payload["result"]["status"] == "completed"
        assert payload["result"]["test_report"]["passed"] is True
        draft_id = payload["result"]["ids"]["draft_id"]

        draft = client.get(f"{PREFIX}/drafts/{draft_id}")
        assert draft.status_code == 200
        assert draft.json()["data"]["artifact_type"] == "mcp_server"
        assert draft.json()["data"]["status"] == "tested"

        tested = client.post(f"{PREFIX}/drafts/{draft_id}/test")
        assert tested.status_code == 200
        assert tested.json()["data"]["passed"] is True
        assert tested.json()["data"]["details"][0]["test"] == "mcp_connection_preview"

    def test_execute_plan_requires_confirm(self, client: TestClient) -> None:
        sid = self._session_id(client)
        planned = client.post(
            f"{PREFIX}/sessions/{sid}/plans",
            json={
                "intent_name": "create_prompt",
                "slot_overrides": {
                    "prompt_name": "support-agent",
                    "template": "You are support.",
                },
            },
        )
        assert planned.status_code == 201
        plan_id = planned.json()["data"]["plan_id"]

        denied = client.post(
            f"{PREFIX}/sessions/{sid}/plans/execute",
            json={"plan_id": plan_id, "confirm": False},
        )
        assert denied.status_code == 400

    def test_get_operation_404(self, client: TestClient) -> None:
        sid = self._session_id(client)
        resp = client.get(f"{PREFIX}/sessions/{sid}/operations/ARN-00000000")
        assert resp.status_code == 404

    def test_generate_cases_then_save_eval_dataset_e2e(self, client: TestClient) -> None:
        sid = self._session_id(client)

        generated_plan = client.post(
            f"{PREFIX}/sessions/{sid}/plans",
            json={
                "intent_name": "generate_test_cases",
                "slot_overrides": {"prompt_name": "support-agent"},
            },
        )
        assert generated_plan.status_code == 201
        generated_plan_id = generated_plan.json()["data"]["plan_id"]

        generated = client.post(
            f"{PREFIX}/sessions/{sid}/plans/execute",
            json={"plan_id": generated_plan_id, "confirm": True},
        )
        assert generated.status_code == 201
        examples = generated.json()["data"]["result"]["examples"]
        assert len(examples) >= 1

        save_plan = client.post(
            f"{PREFIX}/sessions/{sid}/plans",
            json={
                "intent_name": "save_eval_dataset",
                "slot_overrides": {"dataset_name": "support-agent-route-tests"},
            },
        )
        assert save_plan.status_code == 201
        save_plan_data = save_plan.json()["data"]
        assert save_plan_data["ready"] is True
        assert "examples" not in save_plan_data["missing_slots"]
        save_plan_id = save_plan_data["plan_id"]

        saved = client.post(
            f"{PREFIX}/sessions/{sid}/plans/execute",
            json={"plan_id": save_plan_id, "confirm": True},
        )
        assert saved.status_code == 201
        result = saved.json()["data"]["result"]
        assert result["result_type"] == "eval_dataset"
        dataset_id = result["ids"]["dataset_id"]

        dataset = client.get(f"{CALIBER_PREFIX}/eval-datasets/{dataset_id}")
        assert dataset.status_code == 200
        assert dataset.json()["data"]["name"] == "support-agent-route-tests"

    def test_execute_disabled_intent_returns_typed_blocked_result(self, client: TestClient) -> None:
        config = client.patch(
            f"{PREFIX}/config",
            json={"disabled_intents": ["generate_test_cases"]},
        )
        assert config.status_code == 200
        sid = self._session_id(client)
        planned = client.post(
            f"{PREFIX}/sessions/{sid}/plans",
            json={
                "intent_name": "generate_test_cases",
                "slot_overrides": {"prompt_name": "support-agent"},
            },
        )
        assert planned.status_code == 201
        plan_id = planned.json()["data"]["plan_id"]

        executed = client.post(
            f"{PREFIX}/sessions/{sid}/plans/execute",
            json={"plan_id": plan_id, "confirm": True},
        )
        assert executed.status_code == 201
        data = executed.json()["data"]
        assert data["executed_action"] == "intent_disabled"
        assert data["result"]["result_type"] == "blocked"
        assert data["result"]["ids"]["intent_name"] == "generate_test_cases"

    def test_execute_disabled_domain_returns_typed_blocked_result(self, client: TestClient) -> None:
        config = client.patch(
            f"{PREFIX}/config",
            json={"disabled_domains": ["prompt"]},
        )
        assert config.status_code == 200
        sid = self._session_id(client)
        planned = client.post(
            f"{PREFIX}/sessions/{sid}/plans",
            json={
                "intent_name": "generate_test_cases",
                "slot_overrides": {"prompt_name": "support-agent"},
            },
        )
        assert planned.status_code == 201
        plan_id = planned.json()["data"]["plan_id"]

        executed = client.post(
            f"{PREFIX}/sessions/{sid}/plans/execute",
            json={"plan_id": plan_id, "confirm": True},
        )
        assert executed.status_code == 201
        data = executed.json()["data"]
        assert data["executed_action"] == "domain_disabled"
        assert data["result"]["result_type"] == "blocked"
        assert data["result"]["ids"]["disabled_name"] == "prompt"
