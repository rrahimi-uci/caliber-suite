"""Tests for caliber.assistant.service — AssistantService orchestrator."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from caliber.assistant.fake import FakeAssistantEngine
from caliber.assistant.models import (
    AssistantToolCall,
    AssistantTurnRequest,
    AssistantTurnResult,
    ClarifyingQuestion,
    DraftUpdateRequest,
    IntentExecuteRequest,
    IntentPlanRequest,
    IntentResolveRequest,
    MessageSendRequest,
    SessionCreateRequest,
    SessionUpdateRequest,
)
from caliber.assistant.service import AssistantRuntimeSettings, AssistantService, ConflictError
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberApprovalRequest,
    CaliberAssistantDraft,
    CaliberAssistantSession,
    CaliberAuditLog,
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberMcpServer,
    CaliberRefinementJob,
    CaliberRollbackCheckpoint,
    CaliberSkill,
    CaliberWorkflow,
    CaliberWorkflowVersion,
)
from caliber.observability.trace import bind_trace_id
from tests.workflow_helpers import make_manifest, make_support_manifest, seed_eval_dataset

USER = "@test"


class CapturingAssistantEngine:
    def __init__(self) -> None:
        self.requests: list[AssistantTurnRequest] = []

    def run_turn(
        self, request: AssistantTurnRequest, *, toolset: object | None = None
    ) -> AssistantTurnResult:
        self.requests.append(request)
        return AssistantTurnResult(reply="captured")


@pytest.fixture
def svc() -> AssistantService:
    return AssistantService(engine=FakeAssistantEngine())


def test_draft_prompt_from_description_returns_engine_draft(
    svc: AssistantService,
) -> None:
    """The engine may open with a question; the draft helper advances to a draft."""
    result = svc.draft_prompt_from_description(
        "Answer billing questions strictly from policy docs.",
    )

    # FakeAssistantEngine asks a clarifying question first, then drafts a prompt
    # artifact — the helper must auto-advance through to the draft.
    assert result["template"] == "Hello, {{name}}!"
    assert result["name"] == "fake_prompt"
    assert result["variables"] == ["name"]


def test_draft_prompt_from_description_handles_no_draft() -> None:
    """A non-drafting engine yields an empty template rather than hanging."""

    class _SilentEngine:
        def run_turn(
            self, request: object, *, toolset: object | None = None
        ) -> AssistantTurnResult:
            return AssistantTurnResult(reply="ok", done=True)

    svc = AssistantService(engine=_SilentEngine())  # type: ignore[arg-type]
    result = svc.draft_prompt_from_description("Do a thing.")

    assert result["template"] == ""
    assert result["variables"] == []


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class TestSessionCRUD:
    def test_create_and_get(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        resp = svc.create_session(
            SessionCreateRequest(
                title="My session",
                goal="Build tool",
                metadata_={"prompt_ref": "prompts:/support-agent@prod"},
            ),
            session_factory=session_factory,
            user=USER,
        )
        assert resp.session_id.startswith("ASST-")
        assert resp.title == "My session"
        assert resp.owner == USER
        assert resp.metadata_["prompt_ref"] == "prompts:/support-agent@prod"
        assert resp.metadata_["assistant_skill_runtime"]["mode"] == "auto"

        fetched = svc.get_session(resp.session_id, session_factory=session_factory)
        assert fetched is not None
        assert fetched.session_id == resp.session_id
        assert fetched.metadata_["prompt_ref"] == "prompts:/support-agent@prod"

    def test_create_persists_artifact_type(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        resp = svc.create_session(
            SessionCreateRequest(title="Prompt work", artifact_type="prompt"),
            session_factory=session_factory,
            user=USER,
        )
        assert resp.metadata_["artifact_type"] == "prompt"

    def test_create_stores_skill_runtime_controls(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        resp = svc.create_session(
            SessionCreateRequest(
                title="Skill runtime",
                skill_mode="manual",
                pinned_skill_names=["workflow-calibration-review"],
            ),
            session_factory=session_factory,
            user=USER,
        )

        runtime = resp.metadata_["assistant_skill_runtime"]
        assert runtime["mode"] == "manual"
        assert runtime["pinned_skill_names"] == ["workflow-calibration-review"]

    def test_get_session_cross_owner_returns_none(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        created = svc.create_session(
            SessionCreateRequest(title="private"),
            session_factory=session_factory,
            user="@alice",
        )
        assert (
            svc.get_session(created.session_id, session_factory=session_factory, user="@bob")
            is None
        )

    def test_get_nonexistent(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        assert svc.get_session("ASST-00000000", session_factory=session_factory) is None

    def test_list_sessions(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        svc.create_session(
            SessionCreateRequest(title="A"), session_factory=session_factory, user=USER
        )
        svc.create_session(
            SessionCreateRequest(title="B"), session_factory=session_factory, user=USER
        )
        sessions = svc.list_sessions(session_factory=session_factory)
        assert len(sessions) == 2

    def test_list_sessions_owner_filter(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        svc.create_session(
            SessionCreateRequest(title="A"), session_factory=session_factory, user="@alice"
        )
        svc.create_session(
            SessionCreateRequest(title="B"), session_factory=session_factory, user="@bob"
        )
        alice = svc.list_sessions(session_factory=session_factory, owner="@alice")
        assert len(alice) == 1
        assert alice[0].owner == "@alice"

    def test_update_session(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        created = svc.create_session(
            SessionCreateRequest(title="Old"),
            session_factory=session_factory,
            user=USER,
        )
        updated = svc.update_session(
            created.session_id,
            SessionUpdateRequest(
                title="New",
                metadata_={"prompt_alias": "prod"},
            ),
            session_factory=session_factory,
            user=USER,
        )
        assert updated is not None
        assert updated.title == "New"
        assert updated.metadata_["prompt_alias"] == "prod"

    def test_update_session_skill_runtime_controls(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        created = svc.create_session(
            SessionCreateRequest(title="runtime"),
            session_factory=session_factory,
            user=USER,
        )
        updated = svc.update_session(
            created.session_id,
            SessionUpdateRequest(
                skill_mode="off",
                pinned_skill_names=["workflow-calibration-review"],
                disabled_skill_names=["old-skill"],
            ),
            session_factory=session_factory,
            user=USER,
        )

        assert updated is not None
        runtime = updated.metadata_["assistant_skill_runtime"]
        assert runtime["mode"] == "off"
        assert runtime["pinned_skill_names"] == ["workflow-calibration-review"]
        assert runtime["disabled_skill_names"] == ["old-skill"]

    def test_update_nonexistent(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        result = svc.update_session(
            "ASST-00000000",
            SessionUpdateRequest(title="X"),
            session_factory=session_factory,
            user=USER,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Messages & send_message
# ---------------------------------------------------------------------------


class TestMessages:
    def _make_session(
        self,
        svc: AssistantService,
        sf: sessionmaker[Session],
    ) -> str:
        resp = svc.create_session(
            SessionCreateRequest(title="chat"),
            session_factory=sf,
            user=USER,
        )
        return resp.session_id

    def test_send_message_first_turn(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = self._make_session(svc, session_factory)
        turn = svc.send_message(
            sid,
            MessageSendRequest(content="Build me a tool"),
            session_factory=session_factory,
            user=USER,
        )
        assert turn.assistant_message is not None
        assert turn.assistant_message.role == "assistant"
        # FakeEngine: first turn asks questions
        assert len(turn.questions) > 0
        assert turn.run is not None
        assert turn.run.status == "completed"

    def test_send_message_second_turn_produces_draft(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = self._make_session(svc, session_factory)
        svc.send_message(
            sid,
            MessageSendRequest(content="Build tool"),
            session_factory=session_factory,
            user=USER,
        )
        turn = svc.send_message(
            sid,
            MessageSendRequest(content="Call it greet"),
            session_factory=session_factory,
            user=USER,
        )
        assert len(turn.draft_updates) > 0
        assert turn.draft_updates[0].draft_id.startswith("ADRF-")

    def test_send_message_nonexistent_session(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        with pytest.raises(ValueError, match="not found"):
            svc.send_message(
                "ASST-00000000",
                MessageSendRequest(content="hi"),
                session_factory=session_factory,
                user=USER,
            )

    def test_list_messages(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = self._make_session(svc, session_factory)
        svc.send_message(
            sid, MessageSendRequest(content="hello"), session_factory=session_factory, user=USER
        )
        msgs = svc.list_messages(sid, session_factory=session_factory)
        assert len(msgs) >= 2  # user + assistant
        assert msgs[0].role == "user"
        assert msgs[1].role == "assistant"

    def test_send_message_records_current_trace_id(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = self._make_session(svc, session_factory)
        with bind_trace_id("trace-turn"):
            turn = svc.send_message(
                sid,
                MessageSendRequest(content="hello"),
                session_factory=session_factory,
                user=USER,
            )
        assert turn.run is not None
        assert turn.run.trace_id == "trace-turn"

    def test_send_message_builds_task_context_and_persists_stable_metadata(
        self,
        session_factory: sessionmaker[Session],
    ) -> None:
        engine = CapturingAssistantEngine()
        svc = AssistantService(engine=engine)
        sid = svc.create_session(
            SessionCreateRequest(
                title="task context",
                metadata_={
                    "assistant_task_context": {
                        "project_id": "PRJ-OLD",
                        "done_when": ["stale target"],
                        "current_surface": "old_surface",
                    }
                },
            ),
            session_factory=session_factory,
            user=USER,
        ).session_id

        svc.send_message(
            sid,
            MessageSendRequest(
                content="Resume the plan",
                constraints={"must_test": True},
                done_when=["all tests pass"],
                context_refs=[
                    {
                        "ref_type": "workflow",
                        "ref_id": "WF-1",
                        "label": "Support Flow",
                    }
                ],
                selected_resources=[
                    {
                        "ref_type": "knowledge_base",
                        "ref_id": "KB-1",
                        "label": "Support KB",
                    }
                ],
                resume_from_plan_id="PLAN-42",
            ),
            session_factory=session_factory,
            user=USER,
            project_id=None,
            scopes=("caliber.operator", "caliber.viewer"),
            current_surface="assistant_panel_test",
        )

        request = engine.requests[0]
        assert request.task_context.project_id is None
        assert request.task_context.scopes == ["caliber.operator", "caliber.viewer"]
        assert request.task_context.current_surface == "assistant_panel_test"
        assert request.task_context.task_kind == "resume"
        assert request.task_context.constraints == {"must_test": True}
        assert request.task_context.done_when == ["all tests pass"]
        assert request.task_context.context_refs[0].ref_type == "workflow"
        assert request.task_context.selected_resources[0].ref_id == "KB-1"
        assert request.task_context.resume_from_plan_id == "PLAN-42"

        with session_factory() as db:
            session_row = db.get(CaliberAssistantSession, sid)
            assert session_row is not None
            stored = session_row.metadata_["assistant_task_context"]
            assert stored["project_id"] is None
            assert stored["current_surface"] == "assistant_panel_test"
            assert stored["constraints"] == {"must_test": True}
            assert stored["done_when"] == ["all tests pass"]
            assert stored["context_refs"][0]["ref_id"] == "WF-1"
            assert stored["selected_resources"][0]["ref_id"] == "KB-1"
            assert stored["resume_from_plan_id"] == "PLAN-42"
            assert "scopes" not in stored
            assert "task_kind" not in stored

    def test_send_message_persists_selected_skill_provenance(
        self,
        session_factory: sessionmaker[Session],
    ) -> None:
        engine = CapturingAssistantEngine()
        svc = AssistantService(engine=engine)
        sid = svc.create_session(
            SessionCreateRequest(
                title="skill runtime",
                pinned_skill_names=["workflow-calibration-review"],
            ),
            session_factory=session_factory,
            user=USER,
        ).session_id
        with session_factory() as db:
            db.add(
                CaliberSkill(
                    skill_id="SK-workflow-calibration-review",
                    name="workflow-calibration-review",
                    description="Review workflow calibration.",
                    summary="Review workflow calibration.",
                    content="Use eval datasets and preserve approval gates.",
                    owner=USER,
                    category="workflow_automation",
                    tags=["assistant-runtime", "calibration"],
                    status="active",
                    version=3,
                )
            )
            db.commit()

        turn = svc.send_message(
            sid,
            MessageSendRequest(content="calibrate this workflow", artifact_type="workflow"),
            session_factory=session_factory,
            user=USER,
        )

        metadata = turn.assistant_message.metadata_
        assert metadata["skill_runtime_mode"] == "auto"
        assert metadata["selected_skills"][0]["name"] == "workflow-calibration-review"
        assert metadata["selected_skills"][0]["version"] == 3
        assert (
            engine.requests[0].selected_skills[0]["content"]
            == "Use eval datasets and preserve approval gates."
        )
        with session_factory() as db:
            session_row = db.get(CaliberAssistantSession, sid)
            assert session_row is not None
            last = session_row.metadata_["assistant_skill_runtime"]["last_selected_skills"]
            assert last[0]["name"] == "workflow-calibration-review"

    def test_send_message_persists_process_steps_for_actions_and_questions(
        self,
        session_factory: sessionmaker[Session],
    ) -> None:
        class _ProcessEngine:
            def run_turn(
                self,
                request: AssistantTurnRequest,
                *,
                toolset: object | None = None,
            ) -> AssistantTurnResult:
                return AssistantTurnResult(
                    reply="I checked the draft and need one more detail.",
                    questions=[ClarifyingQuestion(question="What should it be named?")],
                    tool_calls=[
                        AssistantToolCall(
                            name="preview_workflow_draft",
                            arguments={},
                            result_summary="draft prepared",
                            ok=True,
                        )
                    ],
                )

        svc = AssistantService(engine=_ProcessEngine())  # type: ignore[arg-type]
        sid = self._make_session(svc, session_factory)

        turn = svc.send_message(
            sid,
            MessageSendRequest(content="Create a workflow"),
            session_factory=session_factory,
            user=USER,
        )

        labels = [step["label"] for step in turn.assistant_message.metadata_["process_steps"]]
        assert labels == ["Thinking", "1 action", "Needs input"]

    def test_request_level_skill_mode_off_overrides_session_for_one_turn(
        self,
        session_factory: sessionmaker[Session],
    ) -> None:
        engine = CapturingAssistantEngine()
        svc = AssistantService(engine=engine)
        sid = svc.create_session(
            SessionCreateRequest(
                title="skill runtime",
                pinned_skill_names=["workflow-calibration-review"],
            ),
            session_factory=session_factory,
            user=USER,
        ).session_id
        with session_factory() as db:
            db.add(
                CaliberSkill(
                    skill_id="SK-workflow-calibration-review",
                    name="workflow-calibration-review",
                    description="Review workflow calibration.",
                    summary="Review workflow calibration.",
                    content="Use eval datasets.",
                    owner=USER,
                    category="workflow_automation",
                    tags=["calibration"],
                    status="active",
                    version=1,
                )
            )
            db.commit()

        turn = svc.send_message(
            sid,
            MessageSendRequest(content="calibrate", skill_mode="off"),
            session_factory=session_factory,
            user=USER,
        )

        assert turn.assistant_message.metadata_["skill_runtime_mode"] == "off"
        assert turn.assistant_message.metadata_["selected_skills"] == []
        assert engine.requests[0].selected_skills == []
        session = svc.get_session(sid, session_factory=session_factory, user=USER)
        assert session is not None
        assert session.metadata_["assistant_skill_runtime"]["mode"] == "auto"

    def test_missing_explicit_skill_warning_is_persisted(
        self,
        session_factory: sessionmaker[Session],
    ) -> None:
        svc = AssistantService(engine=CapturingAssistantEngine())
        sid = self._make_session(svc, session_factory)

        turn = svc.send_message(
            sid,
            MessageSendRequest(
                content="use a missing skill",
                skill_mode="manual",
                skill_names=["missing-skill"],
            ),
            session_factory=session_factory,
            user=USER,
        )

        assert turn.assistant_message.metadata_["selected_skills"] == []
        assert "missing-skill" in turn.assistant_message.metadata_["skill_runtime_warnings"][0]


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------


class TestDrafts:
    def _create_draft(
        self,
        svc: AssistantService,
        sf: sessionmaker[Session],
    ) -> str:
        sid = svc.create_session(
            SessionCreateRequest(title="d"),
            session_factory=sf,
            user=USER,
        ).session_id
        svc.send_message(
            sid, MessageSendRequest(content="create a tool"), session_factory=sf, user=USER
        )
        turn = svc.send_message(
            sid, MessageSendRequest(content="name it foo"), session_factory=sf, user=USER
        )
        assert len(turn.draft_updates) > 0
        return turn.draft_updates[0].draft_id

    def test_list_and_get_draft(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        draft_id = self._create_draft(svc, session_factory)
        draft = svc.get_draft(draft_id, session_factory=session_factory)
        assert draft is not None
        assert draft.draft_id == draft_id

    def test_update_draft(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        draft_id = self._create_draft(svc, session_factory)
        draft = svc.get_draft(draft_id, session_factory=session_factory)
        assert draft is not None
        updated = svc.update_draft(
            draft_id,
            DraftUpdateRequest(version=draft.version, title="renamed"),
            session_factory=session_factory,
            user=USER,
        )
        assert updated is not None
        assert updated.title == "renamed"
        assert updated.version == draft.version + 1

    def test_update_draft_version_conflict(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        draft_id = self._create_draft(svc, session_factory)
        with pytest.raises(ConflictError):
            svc.update_draft(
                draft_id,
                DraftUpdateRequest(version=999, title="bad"),
                session_factory=session_factory,
                user=USER,
            )

    def test_get_draft_nonexistent(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        assert svc.get_draft("ADRF-00000000", session_factory=session_factory) is None


# ---------------------------------------------------------------------------
# Validate / Test / Approve / Publish
# ---------------------------------------------------------------------------


class TestDraftLifecycle:
    def _create_draft(
        self,
        svc: AssistantService,
        sf: sessionmaker[Session],
    ) -> str:
        sid = svc.create_session(
            SessionCreateRequest(title="lc"),
            session_factory=sf,
            user=USER,
        ).session_id
        svc.send_message(
            sid, MessageSendRequest(content="create a tool"), session_factory=sf, user=USER
        )
        turn = svc.send_message(
            sid, MessageSendRequest(content="name it foo"), session_factory=sf, user=USER
        )
        return turn.draft_updates[0].draft_id

    def test_validate(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        draft_id = self._create_draft(svc, session_factory)
        report = svc.validate_draft(draft_id, session_factory=session_factory)
        # FakeEngine drafts have name + source → should pass validation
        assert report.valid

    def test_validate_nonexistent(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        report = svc.validate_draft("ADRF-00000000", session_factory=session_factory)
        assert not report.valid

    def test_test_draft(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        draft_id = self._create_draft(svc, session_factory)
        report = svc.test_draft(draft_id, session_factory=session_factory)
        assert report.passed

    def test_approve(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        draft_id = self._create_draft(svc, session_factory)
        approved = svc.approve_draft(draft_id, session_factory=session_factory, user=USER)
        assert approved is not None
        assert approved.status == "approved"

    def test_approve_nonexistent(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        assert (
            svc.approve_draft("ADRF-00000000", session_factory=session_factory, user=USER) is None
        )

    def test_publish_without_approval(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        draft_id = self._create_draft(svc, session_factory)
        result = svc.publish_draft(draft_id, session_factory=session_factory, user=USER)
        assert not result["success"]
        assert "approved" in result["error"].lower()

    def test_publish_after_approval(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        draft_id = self._create_draft(svc, session_factory)
        svc.approve_draft(draft_id, session_factory=session_factory, user=USER)
        with bind_trace_id("trace-publish"):
            result = svc.publish_draft(draft_id, session_factory=session_factory, user=USER)
        assert result["success"]
        assert result["trace_id"] == "trace-publish"
        assert isinstance(result["correlation_id"], str)
        assert result["correlation_id"].startswith("acorr-")
        with session_factory() as db:
            audit_row = (
                db.query(CaliberAuditLog)
                .filter(
                    CaliberAuditLog.action == "publish_draft",
                    CaliberAuditLog.entity_id == draft_id,
                )
                .one()
            )
            assert audit_row.details is not None
            assert audit_row.details["trace_id"] == "trace-publish"
            assert audit_row.details["correlation_id"] == result["correlation_id"]
            assert audit_row.details["success"] is True
            assert audit_row.details["policy"]["passed"] is True
            assert "rollback_metadata" in audit_row.details

    def test_publish_prompt_alias_requires_policy_approval(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = svc.create_session(
            SessionCreateRequest(title="prompt publish policy"),
            session_factory=session_factory,
            user=USER,
        ).session_id
        draft_id = "ADRF-policy-block"
        with session_factory() as db:
            db.add(
                CaliberAssistantDraft(
                    draft_id=draft_id,
                    session_id=sid,
                    artifact_type="prompt",
                    status="approved",
                    title="support-agent-policy",
                    spec={"plan_id": "APLN-policy"},
                    artifact={
                        "name": "support-agent-policy",
                        "template": "Hello {{name}}",
                        "target_alias": "prod",
                    },
                    created_by=USER,
                    updated_by=USER,
                )
            )
            db.commit()

        result = svc.publish_draft(draft_id, session_factory=session_factory, user=USER)

        assert result["success"] is False
        assert "requires an approved promotion approval" in result["error"]
        assert result["policy"]["requires_approval"] is True
        assert result["policy"]["passed"] is False
        with session_factory() as db:
            draft = db.get(CaliberAssistantDraft, draft_id)
            assert draft is not None
            assert draft.status == "approved"
            audit_row = (
                db.query(CaliberAuditLog)
                .filter(
                    CaliberAuditLog.action == "publish_draft",
                    CaliberAuditLog.entity_id == draft_id,
                )
                .one()
            )
            assert audit_row.details is not None
            assert audit_row.details["success"] is False
            assert audit_row.details["policy"]["target_alias"] == "prod"
            assert audit_row.details["rollback_metadata"]["available"] is False

    def test_publish_prompt_alias_with_policy_approval(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sid = svc.create_session(
            SessionCreateRequest(title="prompt approved publish"),
            session_factory=session_factory,
            user=USER,
        ).session_id
        draft_id = "ADRF-policy-pass"
        approval_id = "AP-draft-publish"
        prompt_name = "support-agent-policy"
        template = "Hello {{name}}"

        captured_register: dict[str, object] = {}
        captured_alias: dict[str, object] = {}

        def fake_register_prompt_version(**kwargs: object) -> dict[str, object]:
            captured_register.update(kwargs)
            return {
                "name": kwargs["name"],
                "version": 12,
                "uri": f"prompts:/{kwargs['name']}/12",
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
            lambda name, alias: {
                "artifact_ref": f"prompts:/{name}@{alias}",
                "version": 3,
            },
        )

        with session_factory() as db:
            db.add(
                CaliberAgentConfig(
                    agent_id=prompt_name,
                    experiment_id="exp-support-agent-policy",
                    name="Support Agent Policy",
                    owner=USER,
                    artifact_types=["prompt"],
                    eval_thresholds={},
                    optimizer_config={},
                    approval_policy={},
                )
            )
            db.add(
                CaliberRefinementJob(
                    job_id="RFN-draft-publish",
                    agent_id=prompt_name,
                    primary_item_id="FB-draft-publish",
                    artifact_type="prompt",
                    optimizer_type="AssistantDraftPublish",
                    status="completed",
                    current_stage="done",
                    candidate={},
                )
            )
            db.add(
                CaliberApprovalRequest(
                    approval_id=approval_id,
                    job_id="RFN-draft-publish",
                    agent_id=prompt_name,
                    status="approved",
                    candidate_snapshot={
                        "promotion_type": "assistant_draft_publish",
                        "artifact_type": "prompt",
                        "prompt_name": prompt_name,
                        "target_alias": "prod",
                        "assistant_draft_id": draft_id,
                    },
                )
            )
            db.add(
                CaliberAssistantDraft(
                    draft_id=draft_id,
                    session_id=sid,
                    artifact_type="prompt",
                    status="approved",
                    title=prompt_name,
                    spec={"plan_id": "APLN-policy", "operation_id": "ARUN-policy"},
                    artifact={
                        "name": prompt_name,
                        "template": template,
                        "target_alias": "prod",
                        "approval_id": approval_id,
                    },
                    created_by=USER,
                    updated_by=USER,
                )
            )
            db.commit()

        result = svc.publish_draft(draft_id, session_factory=session_factory, user=USER)

        assert result["success"] is True
        assert result["alias_changed"] is True
        assert result["target_alias"] == "prod"
        assert result["policy"]["passed"] is True
        assert result["rollback_metadata"]["checkpoint_ids"]
        assert captured_register["set_prod_alias"] is False
        assert captured_register["tags"]["caliber.approval_id"] == approval_id
        assert captured_alias == {"name": prompt_name, "alias": "prod", "version": 12}

        with session_factory() as db:
            draft = db.get(CaliberAssistantDraft, draft_id)
            assert draft is not None
            assert draft.status == "published"
            checkpoint = db.get(
                CaliberRollbackCheckpoint,
                result["rollback_metadata"]["checkpoint_ids"][0],
            )
            assert checkpoint is not None
            assert checkpoint.approval_id == approval_id
            assert checkpoint.version_before == 3
            assert checkpoint.version_after == 12
            audit_row = (
                db.query(CaliberAuditLog)
                .filter(
                    CaliberAuditLog.action == "publish_draft",
                    CaliberAuditLog.entity_id == draft_id,
                )
                .one()
            )
            assert audit_row.details is not None
            assert audit_row.details["plan_id"] == "APLN-policy"
            assert audit_row.details["operation_id"] == "ARUN-policy"
            assert audit_row.details["rollback_metadata"]["checkpoint_ids"]

    def test_publish_nonexistent(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        result = svc.publish_draft("ADRF-00000000", session_factory=session_factory, user=USER)
        assert not result["success"]


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


class TestRuns:
    def test_get_run_after_send(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = svc.create_session(
            SessionCreateRequest(title="r"),
            session_factory=svc.__dict__.get("_sf", session_factory),
            user=USER,
        ).session_id
        turn = svc.send_message(
            sid, MessageSendRequest(content="hi"), session_factory=session_factory, user=USER
        )
        assert turn.run is not None
        run = svc.get_run(turn.run.run_id, session_factory=session_factory)
        assert run is not None
        assert run.status == "completed"

    def test_get_run_nonexistent(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        assert svc.get_run("ARN-00000000", session_factory=session_factory) is None


class TestIntentWorkbench:
    def _session_id(self, svc: AssistantService, sf: sessionmaker[Session]) -> str:
        return svc.create_session(
            SessionCreateRequest(
                title="intent", metadata_={"prompt_ref": "prompts:/support-agent@prod"}
            ),
            session_factory=sf,
            user=USER,
        ).session_id

    def test_resolve_intent_detects_optimization(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = self._session_id(svc, session_factory)
        resolved = svc.resolve_intent(
            sid,
            IntentResolveRequest(
                content="Optimize prompt support-agent with dataset ED-qa123 and faithfulness scorer",
            ),
            session_factory=session_factory,
            user=USER,
        )
        assert resolved.intent.name == "run_prompt_optimization"
        slot_names = {slot.name for slot in resolved.slots}
        assert "eval_dataset_id" in slot_names
        assert "scorers" in slot_names

    def test_resolve_intent_detects_calibration(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = self._session_id(svc, session_factory)
        resolved = svc.resolve_intent(
            sid,
            IntentResolveRequest(
                content="Calibrate prompt support-agent with dataset ED-qa123 and faithfulness scorer",
            ),
            session_factory=session_factory,
            user=USER,
        )
        assert resolved.intent.name == "run_prompt_optimization"
        assert (
            resolved.intent.rationale == "Detected prompt calibration language in the user request."
        )

    def test_create_plan_fills_optimization_defaults(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = self._session_id(svc, session_factory)
        plan = svc.create_intent_plan(
            sid,
            IntentPlanRequest(content="optimize prompt support-agent with dataset ED-qa123"),
            session_factory=session_factory,
            user=USER,
        )
        assert plan.intent.name == "run_prompt_optimization"
        assert plan.ready
        assert plan.missing_slots == []
        slot_names = {slot.name for slot in plan.slots}
        assert "optimizer_type" in slot_names
        assert "scorers" in slot_names

    def _seed_workflow_calibration_target(self, sf: sessionmaker[Session]) -> None:
        with sf() as db:
            db.add(
                CaliberAgentConfig(
                    agent_id="support-agent",
                    experiment_id="exp-support-workflow-calibration",
                    name="Support Agent",
                    owner="@test",
                    enabled=True,
                )
            )
            db.add(CaliberWorkflow(workflow_id="WF-1", name="Support Workflow", owner="@test"))
            db.add(
                CaliberWorkflowVersion(
                    version_id="WFV-1",
                    workflow_id="WF-1",
                    version_number=1,
                    status="published",
                    manifest=make_support_manifest(
                        "WF-1",
                        deploy_gates={
                            "support_eval_gate": {
                                "type": "deploy_gate",
                                "dataset_ref": "support_eval",
                                "required_for_aliases": ["dev"],
                                "thresholds": {},
                            }
                        },
                    ),
                    manifest_hash="hash-wfv-1",
                )
            )
            db.commit()
            seed_eval_dataset(db)

    def test_resolve_intent_detects_workflow_calibration(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = self._session_id(svc, session_factory)
        resolved = svc.resolve_intent(
            sid,
            IntentResolveRequest(content="Calibrate workflow WF-1 against its eval dataset"),
            session_factory=session_factory,
            user=USER,
        )

        assert resolved.intent.name == "run_workflow_calibration"
        assert any(slot.name == "workflow_id" and slot.value == "WF-1" for slot in resolved.slots)

    def test_create_plan_for_workflow_calibration_is_ready(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = self._session_id(svc, session_factory)
        plan = svc.create_intent_plan(
            sid,
            IntentPlanRequest(
                content="Run workflow calibration",
                slot_overrides={
                    "workflow_id": "WF-1",
                    "agent_id": "support-agent",
                    "objective": "tool_adherence",
                    "epsilon": 0.0,
                    "max_candidates": 2,
                },
            ),
            session_factory=session_factory,
            user=USER,
        )

        assert plan.intent.name == "run_workflow_calibration"
        assert plan.ready is True
        assert plan.missing_slots == []

    def test_execute_workflow_calibration_intent_queues_job(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        self._seed_workflow_calibration_target(session_factory)
        sid = self._session_id(svc, session_factory)
        plan = svc.create_intent_plan(
            sid,
            IntentPlanRequest(
                intent_name="run_workflow_calibration",
                slot_overrides={
                    "workflow_id": "WF-1",
                    "agent_id": "support-agent",
                    "objective": "tool_adherence",
                    "epsilon": 0.0,
                    "max_candidates": 2,
                },
            ),
            session_factory=session_factory,
            user=USER,
        )

        executed = svc.execute_intent_plan(
            sid,
            IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
            session_factory=session_factory,
            user=USER,
        )

        assert executed.status == "completed"
        assert executed.executed_action == "enqueue_workflow_calibration"
        assert executed.result["result_type"] == "workflow_calibration_run"
        job_id = executed.result["ids"]["job_id"]
        with session_factory() as db:
            job = db.get(CaliberRefinementJob, job_id)
            assert job is not None
            assert job.workflow_id == "WF-1"
            assert job.calibration_spec["objective"]["maximize"] == "tool_adherence"
            assert job.calibration_spec["budget"]["max_candidates"] == 2

    def test_review_workflow_calibration_result_returns_score_table(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        with session_factory() as db:
            db.add(
                CaliberAgentConfig(
                    agent_id="support-agent",
                    experiment_id="exp-review-workflow-calibration",
                    name="Support Agent",
                    owner="@test",
                    enabled=True,
                )
            )
            db.add(
                CaliberRefinementJob(
                    job_id="RFN-WFCAL",
                    agent_id="support-agent",
                    primary_item_id="FB-1",
                    workflow_id="WF-1",
                    artifact_type="workflow_manifest",
                    status="awaiting_approval",
                    current_stage="approval",
                    calibration_spec={"objective": {"maximize": "quality"}},
                    candidate={
                        "calibration": True,
                        "calibration_winner_id": "cal-1",
                        "calibration_candidates": [
                            {
                                "candidate_id": "cal-0",
                                "accepted": False,
                                "scores": {"quality": 0.7},
                                "deltas": {"quality": 0.01},
                                "rejected_reason": "delta too small",
                            },
                            {
                                "candidate_id": "cal-1",
                                "accepted": True,
                                "scores": {"quality": 0.9},
                                "deltas": {"quality": 0.2},
                            },
                        ],
                    },
                    eval_results={"calibration": True, "calibration_winner_id": "cal-1"},
                )
            )
            db.commit()

        sid = self._session_id(svc, session_factory)
        plan = svc.create_intent_plan(
            sid,
            IntentPlanRequest(
                intent_name="review_workflow_calibration_result",
                slot_overrides={"job_id": "RFN-WFCAL"},
            ),
            session_factory=session_factory,
            user=USER,
        )
        executed = svc.execute_intent_plan(
            sid,
            IntentExecuteRequest(plan_id=plan.plan_id),
            session_factory=session_factory,
            user=USER,
        )

        assert executed.result["result_type"] == "workflow_calibration_review"
        assert executed.result["ids"]["winner_id"] == "cal-1"
        assert executed.result["score_table"][1]["candidate_id"] == "cal-1"

    def test_execute_plan_create_prompt_records_operation(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sid = self._session_id(svc, session_factory)

        from caliber.routes import prompts as prompt_routes

        captured: dict[str, object] = {}

        def _fake_register(**kwargs: object) -> dict[str, object]:
            captured.update(kwargs)
            return {
                "name": "support-agent",
                "version": 7,
                "uri": "prompts:/support-agent/7",
                "template_preview": "hello",
                "template_length": 5,
                "alias_changed": False,
            }

        monkeypatch.setattr(
            prompt_routes,
            "register_prompt_version",
            _fake_register,
        )

        plan = svc.create_intent_plan(
            sid,
            IntentPlanRequest(
                intent_name="create_prompt",
                slot_overrides={
                    "prompt_name": "support-agent",
                    "template": "You are support.",
                },
            ),
            session_factory=session_factory,
            user=USER,
        )
        assert plan.ready

        executed = svc.execute_intent_plan(
            sid,
            IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
            session_factory=session_factory,
            user=USER,
        )
        assert executed.status == "completed"
        assert executed.executed_action == "register_prompt"
        assert executed.run is not None
        assert captured["set_prod_alias"] is False
        assert executed.result["alias_changed"] is False
        assert executed.result["result_type"] == "prompt_version"

        operation = svc.get_operation_status(
            sid,
            executed.operation_id,
            session_factory=session_factory,
        )
        assert operation is not None
        assert operation.status == "completed"
        assert operation.plan_id == plan.plan_id

    def test_execute_plan_requires_confirmation(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = self._session_id(svc, session_factory)
        plan = svc.create_intent_plan(
            sid,
            IntentPlanRequest(
                intent_name="create_prompt",
                slot_overrides={
                    "prompt_name": "support-agent",
                    "template": "You are support.",
                },
            ),
            session_factory=session_factory,
            user=USER,
        )
        with pytest.raises(ValueError, match="confirm"):
            svc.execute_intent_plan(
                sid,
                IntentExecuteRequest(plan_id=plan.plan_id, confirm=False),
                session_factory=session_factory,
                user=USER,
            )

    def test_execute_plan_optimization_path(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sid = self._session_id(svc, session_factory)

        monkeypatch.setattr(
            AssistantService,
            "_execute_prompt_optimization",
            lambda self, plan, session_factory, user: {
                "item": {"item_id": "FB-12345678"},
                "job": {"job_id": "RFN-12345678"},
            },
        )

        plan = svc.create_intent_plan(
            sid,
            IntentPlanRequest(
                intent_name="run_prompt_optimization",
                slot_overrides={
                    "agent_id": "support-agent",
                    "eval_dataset_id": "ED-qa123",
                    "optimizer_type": "MetaPrompt",
                    "scorers": [{"name": "Correctness", "weight": 1.0, "config": {}}],
                    "gate": {
                        "min_aggregate_score": 0.75,
                        "max_regression_delta": 0.05,
                    },
                },
            ),
            session_factory=session_factory,
            user=USER,
        )
        assert plan.ready

        executed = svc.execute_intent_plan(
            sid,
            IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
            session_factory=session_factory,
            user=USER,
        )
        assert executed.status == "completed"
        assert executed.executed_action == "enqueue_prompt_optimization"
        assert "job" in executed.result

    def test_review_optimization_result_is_read_only(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = self._session_id(svc, session_factory)
        with session_factory() as db:
            db.add(
                CaliberRefinementJob(
                    job_id="RFN-review01",
                    agent_id="support-agent",
                    primary_item_id="FB-review01",
                    artifact_type="prompt",
                    optimizer_type="MetaPrompt",
                    status="awaiting_approval",
                    current_stage="approval",
                    eval_results={"gate": {"passed": True}},
                    candidate={"prompt_version": 4},
                )
            )
            db.commit()

        plan = svc.create_intent_plan(
            sid,
            IntentPlanRequest(
                intent_name="review_optimization_result",
                slot_overrides={"job_id": "RFN-review01"},
            ),
            session_factory=session_factory,
            user=USER,
        )
        assert plan.requires_confirmation is False

        executed = svc.execute_intent_plan(
            sid,
            IntentExecuteRequest(plan_id=plan.plan_id, confirm=False),
            session_factory=session_factory,
            user=USER,
        )
        assert executed.status == "completed"
        assert executed.result["result_type"] == "optimization_review"
        assert executed.result["ids"]["job_id"] == "RFN-review01"

    def test_execute_intent_plan_records_trace_and_correlation(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = self._session_id(svc, session_factory)
        plan = svc.create_intent_plan(
            sid,
            IntentPlanRequest(
                intent_name="generate_test_cases",
                slot_overrides={"prompt_name": "support-agent"},
            ),
            session_factory=session_factory,
            user=USER,
        )

        with bind_trace_id("trace-intent"):
            executed = svc.execute_intent_plan(
                sid,
                IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
                session_factory=session_factory,
                user=USER,
            )

        assert executed.run is not None
        assert executed.run.trace_id == "trace-intent"
        assert executed.result["trace_id"] == "trace-intent"
        correlation_id = executed.result["correlation_id"]
        assert isinstance(correlation_id, str)
        assert correlation_id.startswith("acorr-")

        operation = svc.get_operation_status(
            sid,
            executed.operation_id,
            session_factory=session_factory,
            user=USER,
        )
        assert operation is not None
        assert operation.run is not None
        assert operation.run.trace_id == "trace-intent"
        assert operation.result["correlation_id"] == correlation_id

        with session_factory() as db:
            session = db.get(CaliberAssistantSession, sid)
            assert session is not None
            assert session.metadata_["assistant_correlation_id"] == correlation_id
            stored = session.metadata_["intent_workbench"]["operations"][executed.operation_id]
            assert stored["trace_id"] == "trace-intent"
            assert stored["correlation_id"] == correlation_id

    def test_execute_intent_plan_respects_disabled_intent_flag(
        self,
        session_factory: sessionmaker[Session],
    ) -> None:
        svc = AssistantService(
            engine=FakeAssistantEngine(),
            settings=AssistantRuntimeSettings(disabled_intents=("generate_test_cases",)),
        )
        sid = self._session_id(svc, session_factory)
        plan = svc.create_intent_plan(
            sid,
            IntentPlanRequest(
                intent_name="generate_test_cases",
                slot_overrides={"prompt_name": "support-agent"},
            ),
            session_factory=session_factory,
            user=USER,
        )

        executed = svc.execute_intent_plan(
            sid,
            IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
            session_factory=session_factory,
            user=USER,
        )

        assert executed.status == "completed"
        assert executed.executed_action == "intent_disabled"
        assert executed.result["result_type"] == "blocked"
        assert executed.result["ids"]["intent_name"] == "generate_test_cases"

        operation = svc.get_operation_status(
            sid,
            executed.operation_id,
            session_factory=session_factory,
            user=USER,
        )
        assert operation is not None
        assert operation.result["status"] == "blocked"

    def test_execute_intent_plan_respects_disabled_domain_flag(
        self,
        session_factory: sessionmaker[Session],
    ) -> None:
        svc = AssistantService(
            engine=FakeAssistantEngine(),
            settings=AssistantRuntimeSettings(disabled_domains=("prompt",)),
        )
        sid = self._session_id(svc, session_factory)
        plan = svc.create_intent_plan(
            sid,
            IntentPlanRequest(
                intent_name="generate_test_cases",
                slot_overrides={"prompt_name": "support-agent"},
            ),
            session_factory=session_factory,
            user=USER,
        )

        executed = svc.execute_intent_plan(
            sid,
            IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
            session_factory=session_factory,
            user=USER,
        )

        assert executed.status == "completed"
        assert executed.executed_action == "domain_disabled"
        assert executed.result["result_type"] == "blocked"
        assert executed.result["ids"]["disabled_kind"] == "domain"
        assert executed.result["ids"]["disabled_name"] == "prompt"

    @pytest.mark.parametrize(
        ("content", "expected_intent"),
        [
            ("build a tool named double_tool with callable double_tool", "create_tool"),
            ("create a skill named support-triage for ticket triage", "create_skill"),
            ("draft a workflow named support routing", "create_workflow"),
            ("set up an MCP server named filesystem over stdio", "create_mcp_server"),
        ],
    )
    def test_resolve_intent_detects_phase2_domains(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
        content: str,
        expected_intent: str,
    ) -> None:
        sid = self._session_id(svc, session_factory)
        resolved = svc.resolve_intent(
            sid,
            IntentResolveRequest(content=content),
            session_factory=session_factory,
            user=USER,
        )
        assert resolved.intent.name == expected_intent

    def test_create_tool_intent_creates_validated_tested_draft(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = self._session_id(svc, session_factory)
        plan = svc.create_intent_plan(
            sid,
            IntentPlanRequest(
                intent_name="create_tool",
                slot_overrides={
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
            ),
            session_factory=session_factory,
            user=USER,
        )
        assert plan.ready is True
        assert plan.requires_confirmation is True
        assert plan.actions[0].action == "create_validate_test_tool_draft"

        executed = svc.execute_intent_plan(
            sid,
            IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
            session_factory=session_factory,
            user=USER,
        )

        assert executed.executed_action == "create_validate_test_tool_draft"
        assert executed.result["result_type"] == "tool_draft"
        assert executed.result["status"] == "completed"
        assert executed.result["validation_report"]["valid"] is True
        assert executed.result["test_report"]["passed"] is True
        assert executed.result["test_report"]["details"][0]["name"] == "doubles two"
        assert executed.result["next_actions"][0]["intent_name"] == "approve_draft"
        draft_id = executed.result["ids"]["draft_id"]

        report = svc.test_draft(
            draft_id,
            session_factory=session_factory,
            user=USER,
        )
        assert report.passed is True
        assert report.details[0]["name"] == "doubles two"

        with session_factory() as db:
            session = db.get(CaliberAssistantSession, sid)
            assert session is not None
            assert session.active_draft_id == draft_id
            draft = db.get(CaliberAssistantDraft, draft_id)
            assert draft is not None
            assert draft.artifact_type == "tool"
            assert draft.status == "tested"
            assert draft.validation_report is not None
            assert draft.validation_report["valid"] is True
            assert draft.test_report is not None
            assert draft.test_report["passed"] is True
            assert draft.artifact["callable_name"] == "double_tool"
            audit_row = (
                db.query(CaliberAuditLog)
                .filter(
                    CaliberAuditLog.action == "create_tool_draft",
                    CaliberAuditLog.entity_id == draft_id,
                )
                .first()
            )
            assert audit_row is not None
            assert audit_row.details is not None
            assert audit_row.details["plan_id"] == plan.plan_id
            assert audit_row.details["sandbox_passed"] is True

    def test_create_tool_intent_blocks_on_failing_sandbox_test(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = self._session_id(svc, session_factory)
        plan = svc.create_intent_plan(
            sid,
            IntentPlanRequest(
                intent_name="create_tool",
                slot_overrides={
                    "tool_name": "double_tool",
                    "source": "def double_tool(x: int) -> dict:\n    return {'value': x * 2}\n",
                    "callable_name": "double_tool",
                    "input_schema": {
                        "type": "object",
                        "properties": {"x": {"type": "integer"}},
                    },
                    "tests": [
                        {
                            "name": "catches mismatch",
                            "input": {"x": 2},
                            "expected": {"value": 5},
                        }
                    ],
                },
            ),
            session_factory=session_factory,
            user=USER,
        )

        executed = svc.execute_intent_plan(
            sid,
            IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
            session_factory=session_factory,
            user=USER,
        )

        assert executed.status == "completed"
        assert executed.result["result_type"] == "tool_draft"
        assert executed.result["status"] == "blocked"
        assert executed.result["test_report"]["passed"] is False
        assert executed.result["test_report"]["failures"] == 1
        assert executed.result["test_report"]["details"][0]["status"] == "failed"
        assert "output did not match expected value" in executed.result["warnings"]
        assert executed.result["next_actions"] == []

        draft_id = executed.result["ids"]["draft_id"]
        with session_factory() as db:
            draft = db.get(CaliberAssistantDraft, draft_id)
            assert draft is not None
            assert draft.status == "test_failed"
            assert draft.test_report is not None
            assert draft.test_report["failures"] == 1

    def test_create_skill_intent_creates_package_checked_draft(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = self._session_id(svc, session_factory)
        plan = svc.create_intent_plan(
            sid,
            IntentPlanRequest(
                intent_name="create_skill",
                slot_overrides={
                    "skill_name": "support-triage",
                    "description": "Use when a support ticket needs triage.",
                    "summary": "Classifies support tickets before handoff.",
                    "content": "Classify urgency, summarize context, and recommend the next owner.",
                    "category": "workflow_automation",
                    "tags": ["assistant-generated", "support"],
                },
            ),
            session_factory=session_factory,
            user=USER,
        )
        assert plan.ready is True
        assert plan.actions[0].action == "create_validate_package_skill_draft"

        executed = svc.execute_intent_plan(
            sid,
            IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
            session_factory=session_factory,
            user=USER,
        )

        assert executed.executed_action == "create_validate_package_skill_draft"
        assert executed.result["result_type"] == "skill_draft"
        assert executed.result["status"] == "completed"
        assert executed.result["test_report"]["passed"] is True
        assert executed.result["test_report"]["details"][0]["test"] == "package_build"
        assert executed.result["next_actions"][0]["intent_name"] == "approve_draft"
        draft_id = executed.result["ids"]["draft_id"]

        report = svc.test_draft(
            draft_id,
            session_factory=session_factory,
            user=USER,
        )
        assert report.passed is True
        assert report.details[0]["test"] == "package_build"

        with session_factory() as db:
            session = db.get(CaliberAssistantSession, sid)
            assert session is not None
            assert session.active_draft_id == draft_id
            draft = db.get(CaliberAssistantDraft, draft_id)
            assert draft is not None
            assert draft.artifact_type == "skill"
            assert draft.status == "tested"
            assert draft.validation_report is not None
            assert draft.validation_report["valid"] is True
            assert draft.test_report is not None
            assert draft.test_report["passed"] is True
            assert draft.artifact["category"] == "workflow_automation"
            audit_row = (
                db.query(CaliberAuditLog)
                .filter(
                    CaliberAuditLog.action == "create_skill_draft",
                    CaliberAuditLog.entity_id == draft_id,
                )
                .first()
            )
            assert audit_row is not None
            assert audit_row.details is not None
            assert audit_row.details["package_check_passed"] is True

        approved = svc.approve_draft(
            draft_id,
            session_factory=session_factory,
            user=USER,
        )
        assert approved is not None
        published = svc.publish_draft(
            draft_id,
            session_factory=session_factory,
            user=USER,
        )
        assert published["success"] is True
        assert published["type"] == "skill"

        with session_factory() as db:
            skill = db.get(CaliberSkill, published["registry_id"])
            assert skill is not None
            assert skill.name == "support-triage"
            assert skill.summary == "Classifies support tickets before handoff."
            assert skill.category == "workflow_automation"
            assert skill.tags == ["assistant-generated", "support"]

    def test_create_skill_intent_blocks_on_package_warning(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = self._session_id(svc, session_factory)
        plan = svc.create_intent_plan(
            sid,
            IntentPlanRequest(
                intent_name="create_skill",
                slot_overrides={
                    "skill_name": "support-triage",
                    "description": "Use when a support ticket needs triage.",
                    "content": "Classify urgency and recommend the next owner.",
                    "skill_metadata": {
                        "openai_package": {
                            "resources": [
                                {"path": "../bad.md", "content": "not allowed"},
                            ],
                        },
                    },
                },
            ),
            session_factory=session_factory,
            user=USER,
        )

        executed = svc.execute_intent_plan(
            sid,
            IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
            session_factory=session_factory,
            user=USER,
        )

        assert executed.result["result_type"] == "skill_draft"
        assert executed.result["status"] == "blocked"
        assert executed.result["validation_report"]["valid"] is True
        assert executed.result["test_report"]["passed"] is False
        assert executed.result["test_report"]["details"][0]["test"] == "package_build"
        assert executed.result["warnings"]
        assert executed.result["next_actions"] == []

        with session_factory() as db:
            draft = db.get(CaliberAssistantDraft, executed.result["ids"]["draft_id"])
            assert draft is not None
            assert draft.status == "test_failed"
            assert draft.test_report is not None
            assert draft.test_report["failures"] == 1

    def test_create_workflow_intent_creates_compiled_draft(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = self._session_id(svc, session_factory)
        manifest = make_manifest("support_triage", name="Support Triage")
        plan = svc.create_intent_plan(
            sid,
            IntentPlanRequest(
                intent_name="create_workflow",
                slot_overrides={
                    "workflow_name": "Support Triage",
                    "description": "Route a support request through a single agent.",
                    "manifest": manifest,
                },
            ),
            session_factory=session_factory,
            user=USER,
        )
        assert plan.ready is True
        assert plan.actions[0].action == "create_validate_compile_workflow_draft"

        executed = svc.execute_intent_plan(
            sid,
            IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
            session_factory=session_factory,
            user=USER,
        )

        assert executed.executed_action == "create_validate_compile_workflow_draft"
        assert executed.result["result_type"] == "workflow_draft"
        assert executed.result["status"] == "completed"
        assert executed.result["validation_report"]["valid"] is True
        assert executed.result["test_report"]["passed"] is True
        assert executed.result["test_report"]["details"][0]["test"] == "workflow_compile"
        draft_id = executed.result["ids"]["draft_id"]

        report = svc.test_draft(
            draft_id,
            session_factory=session_factory,
            user=USER,
        )
        assert report.passed is True
        assert report.details[0]["test"] == "workflow_compile"

        with session_factory() as db:
            draft = db.get(CaliberAssistantDraft, draft_id)
            assert draft is not None
            assert draft.artifact_type == "workflow"
            assert draft.status == "tested"
            assert draft.validation_report is not None
            assert draft.validation_report["valid"] is True
            assert draft.test_report is not None
            assert draft.test_report["passed"] is True
            audit_row = (
                db.query(CaliberAuditLog)
                .filter(
                    CaliberAuditLog.action == "create_workflow_draft",
                    CaliberAuditLog.entity_id == draft_id,
                )
                .first()
            )
            assert audit_row is not None
            assert audit_row.details is not None
            assert audit_row.details["compile_preview_passed"] is True

        assert svc.approve_draft(draft_id, session_factory=session_factory, user=USER) is not None
        published = svc.publish_draft(draft_id, session_factory=session_factory, user=USER)
        assert published["success"] is True
        assert published["type"] == "workflow"

        with session_factory() as db:
            workflow = db.get(CaliberWorkflow, published["registry_id"])
            assert workflow is not None
            assert workflow.name == "Support Triage"
            version = db.get(CaliberWorkflowVersion, published["version_id"])
            assert version is not None
            assert version.status == "published"
            assert version.manifest["workflow_id"] == workflow.workflow_id
            assert version.manifest_hash

    def test_create_workflow_intent_blocks_on_compile_failure(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = self._session_id(svc, session_factory)
        manifest = make_manifest("support_triage", name="Support Triage")
        manifest["nodes"]["agent"]["tools"] = ["missing_tool"]
        manifest["tools"] = {"missing_tool": {"registry_ref": "tool.missing.v1"}}
        plan = svc.create_intent_plan(
            sid,
            IntentPlanRequest(
                intent_name="create_workflow",
                slot_overrides={
                    "workflow_name": "Support Triage",
                    "manifest": manifest,
                },
            ),
            session_factory=session_factory,
            user=USER,
        )

        executed = svc.execute_intent_plan(
            sid,
            IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
            session_factory=session_factory,
            user=USER,
        )

        assert executed.result["result_type"] == "workflow_draft"
        assert executed.result["status"] == "blocked"
        assert executed.result["validation_report"]["valid"] is True
        assert executed.result["test_report"]["passed"] is False
        assert executed.result["test_report"]["details"][0]["test"] == "workflow_compile"
        assert executed.result["next_actions"] == []

        with session_factory() as db:
            draft = db.get(CaliberAssistantDraft, executed.result["ids"]["draft_id"])
            assert draft is not None
            assert draft.status == "test_failed"

    def test_create_mcp_server_intent_creates_connection_checked_draft(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = self._session_id(svc, session_factory)
        plan = svc.create_intent_plan(
            sid,
            IntentPlanRequest(
                intent_name="create_mcp_server",
                slot_overrides={
                    "server_name": "filesystem",
                    "description": "Local filesystem MCP server.",
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                    "discovered_tools": [{"name": "read_file", "description": "Read a file"}],
                },
            ),
            session_factory=session_factory,
            user=USER,
        )
        assert plan.ready is True
        assert plan.actions[0].action == "create_validate_test_mcp_server_draft"

        executed = svc.execute_intent_plan(
            sid,
            IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
            session_factory=session_factory,
            user=USER,
        )

        assert executed.executed_action == "create_validate_test_mcp_server_draft"
        assert executed.result["result_type"] == "mcp_server_draft"
        assert executed.result["status"] == "completed"
        assert executed.result["validation_report"]["valid"] is True
        assert executed.result["test_report"]["passed"] is True
        assert executed.result["test_report"]["details"][0]["test"] == "mcp_connection_preview"
        draft_id = executed.result["ids"]["draft_id"]

        report = svc.test_draft(
            draft_id,
            session_factory=session_factory,
            user=USER,
        )
        assert report.passed is True
        assert report.details[0]["tool_count"] == 1

        with session_factory() as db:
            draft = db.get(CaliberAssistantDraft, draft_id)
            assert draft is not None
            assert draft.artifact_type == "mcp_server"
            assert draft.status == "tested"
            assert draft.artifact["command"] == "npx"
            audit_row = (
                db.query(CaliberAuditLog)
                .filter(
                    CaliberAuditLog.action == "create_mcp_server_draft",
                    CaliberAuditLog.entity_id == draft_id,
                )
                .first()
            )
            assert audit_row is not None
            assert audit_row.details is not None
            assert audit_row.details["connection_preview_passed"] is True

        assert svc.approve_draft(draft_id, session_factory=session_factory, user=USER) is not None
        published = svc.publish_draft(draft_id, session_factory=session_factory, user=USER)
        assert published["success"] is True
        assert published["type"] == "mcp_server"

        with session_factory() as db:
            server = db.get(CaliberMcpServer, published["registry_id"])
            assert server is not None
            assert server.name == "filesystem"
            assert server.command == "npx"
            assert server.discovered_tools == [{"name": "read_file", "description": "Read a file"}]

    def test_create_mcp_server_intent_blocks_on_invalid_config(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = self._session_id(svc, session_factory)
        plan = svc.create_intent_plan(
            sid,
            IntentPlanRequest(
                intent_name="create_mcp_server",
                slot_overrides={
                    "server_name": "filesystem",
                    "transport": "stdio",
                },
            ),
            session_factory=session_factory,
            user=USER,
        )

        executed = svc.execute_intent_plan(
            sid,
            IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
            session_factory=session_factory,
            user=USER,
        )

        assert executed.result["result_type"] == "mcp_server_draft"
        assert executed.result["status"] == "blocked"
        assert executed.result["validation_report"]["valid"] is False
        assert "stdio transport requires a 'command' field." in executed.result["warnings"]
        assert executed.result["next_actions"] == []

        with session_factory() as db:
            draft = db.get(CaliberAssistantDraft, executed.result["ids"]["draft_id"])
            assert draft is not None
            assert draft.status == "validation_failed"

    def test_generate_then_save_eval_dataset(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = self._session_id(svc, session_factory)
        gen_plan = svc.create_intent_plan(
            sid,
            IntentPlanRequest(
                intent_name="generate_test_cases",
                slot_overrides={"prompt_name": "support-agent"},
            ),
            session_factory=session_factory,
            user=USER,
        )
        generated = svc.execute_intent_plan(
            sid,
            IntentExecuteRequest(plan_id=gen_plan.plan_id, confirm=True),
            session_factory=session_factory,
            user=USER,
        )
        examples = generated.result["examples"]

        save_plan = svc.create_intent_plan(
            sid,
            IntentPlanRequest(
                intent_name="save_eval_dataset",
                slot_overrides={
                    "dataset_name": "support-generated-tests",
                    "examples": examples,
                },
            ),
            session_factory=session_factory,
            user=USER,
        )
        saved = svc.execute_intent_plan(
            sid,
            IntentExecuteRequest(plan_id=save_plan.plan_id, confirm=True),
            session_factory=session_factory,
            user=USER,
        )
        assert saved.result["result_type"] == "eval_dataset"
        dataset_id = saved.result["ids"]["dataset_id"]
        with session_factory() as db:
            dataset = db.get(CaliberEvalDataset, dataset_id)
            assert dataset is not None
            assert dataset.name == "support-generated-tests"
            count = (
                db.query(CaliberEvalDatasetExample)
                .filter(CaliberEvalDatasetExample.dataset_id == dataset_id)
                .count()
            )
            assert count == len(examples)

    def test_save_eval_dataset_uses_latest_generated_examples(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = self._session_id(svc, session_factory)
        gen_plan = svc.create_intent_plan(
            sid,
            IntentPlanRequest(
                intent_name="generate_test_cases",
                slot_overrides={"prompt_name": "support-agent"},
            ),
            session_factory=session_factory,
            user=USER,
        )
        generated = svc.execute_intent_plan(
            sid,
            IntentExecuteRequest(plan_id=gen_plan.plan_id, confirm=True),
            session_factory=session_factory,
            user=USER,
        )

        save_plan = svc.create_intent_plan(
            sid,
            IntentPlanRequest(
                intent_name="save_eval_dataset",
                slot_overrides={"dataset_name": "support-workbench-tests"},
            ),
            session_factory=session_factory,
            user=USER,
        )
        assert save_plan.ready is True
        assert "examples" not in save_plan.missing_slots
        examples_slot = next(slot for slot in save_plan.slots if slot.name == "examples")
        assert examples_slot.source == "memory"
        assert examples_slot.value == generated.result["examples"]

        saved = svc.execute_intent_plan(
            sid,
            IntentExecuteRequest(plan_id=save_plan.plan_id, confirm=True),
            session_factory=session_factory,
            user=USER,
        )
        assert saved.result["result_type"] == "eval_dataset"
        assert saved.result["ids"]["dataset_name"] == "support-workbench-tests"

    def test_workbench_retains_bounded_plans_and_operations(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = self._session_id(svc, session_factory)
        latest_plan_id = ""
        latest_operation_id = ""
        for index in range(55):
            plan = svc.create_intent_plan(
                sid,
                IntentPlanRequest(
                    intent_name="generate_test_cases",
                    slot_overrides={"prompt_name": f"support-agent-{index}"},
                ),
                session_factory=session_factory,
                user=USER,
            )
            latest_plan_id = plan.plan_id
            executed = svc.execute_intent_plan(
                sid,
                IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
                session_factory=session_factory,
                user=USER,
            )
            latest_operation_id = executed.operation_id

        with session_factory() as db:
            session = db.get(CaliberAssistantSession, sid)
            assert session is not None
            workbench = session.metadata_["intent_workbench"]
            assert len(workbench["plans"]) == 25
            assert len(workbench["operations"]) == 50
            assert workbench["latest_plan_id"] == latest_plan_id
            assert latest_plan_id in workbench["plans"]
            assert workbench["latest_operation_id"] == latest_operation_id
            assert latest_operation_id in workbench["operations"]

    def test_propose_promotion_blocks_without_source_version(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = self._session_id(svc, session_factory)
        plan = svc.create_intent_plan(
            sid,
            IntentPlanRequest(
                intent_name="propose_promotion",
                slot_overrides={"prompt_name": "support-agent", "target_alias": "prod"},
            ),
            session_factory=session_factory,
            user=USER,
        )
        executed = svc.execute_intent_plan(
            sid,
            IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
            session_factory=session_factory,
            user=USER,
        )
        assert executed.result["result_type"] == "blocked"
        assert executed.result["status"] == "blocked"

    def test_propose_promotion_creates_pending_approval(
        self,
        svc: AssistantService,
        session_factory: sessionmaker[Session],
    ) -> None:
        sid = self._session_id(svc, session_factory)
        with session_factory() as db:
            db.add(
                CaliberAgentConfig(
                    agent_id="support-agent",
                    experiment_id="exp-support-agent",
                    name="Support Agent",
                    owner=USER,
                    artifact_types=["prompt"],
                    eval_thresholds={},
                    optimizer_config={},
                    approval_policy={},
                )
            )
            db.commit()

        plan = svc.create_intent_plan(
            sid,
            IntentPlanRequest(
                intent_name="propose_promotion",
                slot_overrides={
                    "prompt_name": "support-agent",
                    "source_version": 7,
                    "target_alias": "prod",
                },
            ),
            session_factory=session_factory,
            user=USER,
        )
        executed = svc.execute_intent_plan(
            sid,
            IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
            session_factory=session_factory,
            user=USER,
        )
        assert executed.result["result_type"] == "promotion_proposal"
        assert executed.result["status"] == "completed"
        approval_id = executed.result["ids"]["approval_id"]
        assert approval_id.startswith("AP-")

        with session_factory() as db:
            approval = db.get(CaliberApprovalRequest, approval_id)
            assert approval is not None
            assert approval.status == "pending"
            assert approval.candidate_snapshot is not None
            assert approval.candidate_snapshot["promotion_type"] == "prompt_alias"
            assert approval.candidate_snapshot["source_version"] == 7
            assert approval.candidate_snapshot["target_alias"] == "prod"
            assert approval.candidate_snapshot["assistant_session_id"] == sid
            assert approval.candidate_snapshot["assistant_plan_id"] == plan.plan_id
            assert approval.candidate_snapshot["assistant_operation_id"] == executed.operation_id
            assert approval.candidate_snapshot["assistant_trace_id"] == executed.result["trace_id"]
            assert (
                approval.candidate_snapshot["assistant_correlation_id"]
                == executed.result["correlation_id"]
            )
            job = db.get(CaliberRefinementJob, approval.job_id)
            assert job is not None
            assert job.status == "awaiting_approval"
            assert job.current_stage == "approval"
            audit_rows = (
                db.query(CaliberAuditLog).filter(CaliberAuditLog.entity_id == approval_id).all()
            )
            assert len(audit_rows) == 1
            assert audit_rows[0].details is not None
            assert audit_rows[0].details["operation_id"] == executed.operation_id
            assert audit_rows[0].details["trace_id"] == executed.result["trace_id"]
            assert audit_rows[0].details["correlation_id"] == executed.result["correlation_id"]

        repeated = svc.execute_intent_plan(
            sid,
            IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
            session_factory=session_factory,
            user=USER,
        )
        assert repeated.result["ids"]["approval_id"] == approval_id

        with session_factory() as db:
            approvals = db.query(CaliberApprovalRequest).all()
            assert len(approvals) == 1
