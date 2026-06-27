"""Expanded schema-contract coverage for Aria request/response models."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from caliber.assistant.models import (
    ARTIFACT_TYPES,
    ASSISTANT_APPROVAL_MODES,
    ASSISTANT_DOMAINS,
    ASSISTANT_MODES,
    ATTACHMENT_KINDS,
    INTENT_NAMES,
    LIBRARY_RESOURCE_TYPES,
    QUEUED_MESSAGE_KINDS,
    AssistantTurnRequest,
    AssistantTurnResult,
    AttachmentCreateRequest,
    AttachmentResponse,
    ClarifyingQuestion,
    DraftDelta,
    DraftResponse,
    DraftUpdateRequest,
    IntentCandidate,
    IntentExecuteRequest,
    IntentPlanRequest,
    IntentResolveRequest,
    IntentSlot,
    MessageResponse,
    MessageSendRequest,
    OperationStatusResponse,
    PlanAction,
    QueuedMessageCreateRequest,
    QueuedMessageResponse,
    RunResponse,
    SessionCreateRequest,
    SessionResponse,
    SessionUpdateRequest,
    TurnResponse,
    ValidationReport,
)
from caliber.assistant.models import (
    TestReport as AssistantTestReport,
)

NOW = datetime(2026, 6, 20, tzinfo=timezone.utc)


@pytest.mark.parametrize("artifact_type", sorted(ARTIFACT_TYPES))
def test_session_create_accepts_all_artifact_types(artifact_type: str) -> None:
    req = SessionCreateRequest(artifact_type=artifact_type)
    assert req.artifact_type == artifact_type


@pytest.mark.parametrize("skill_mode", ["auto", "manual", "off"])
def test_session_create_accepts_all_skill_runtime_modes(skill_mode: str) -> None:
    req = SessionCreateRequest(skill_mode=skill_mode)
    assert req.skill_mode == skill_mode


@pytest.mark.parametrize("mode", sorted(ASSISTANT_MODES))
def test_session_create_accepts_all_assistant_modes(mode: str) -> None:
    req = SessionCreateRequest(mode=mode)
    assert req.mode == mode


@pytest.mark.parametrize("approval_mode", sorted(ASSISTANT_APPROVAL_MODES))
def test_session_create_accepts_all_approval_modes(approval_mode: str) -> None:
    req = SessionCreateRequest(approval_mode=approval_mode)
    assert req.approval_mode == approval_mode


@pytest.mark.parametrize("status", ["active", "completed", "archived"])
def test_session_update_accepts_all_statuses(status: str) -> None:
    req = SessionUpdateRequest(status=status)
    assert req.status == status


@pytest.mark.parametrize("skill_mode", ["auto", "manual", "off"])
def test_session_update_accepts_all_skill_runtime_modes(skill_mode: str) -> None:
    req = SessionUpdateRequest(skill_mode=skill_mode)
    assert req.skill_mode == skill_mode


@pytest.mark.parametrize("mode", sorted(ASSISTANT_MODES))
def test_session_update_accepts_all_modes(mode: str) -> None:
    req = SessionUpdateRequest(mode=mode)
    assert req.mode == mode


@pytest.mark.parametrize("approval_mode", sorted(ASSISTANT_APPROVAL_MODES))
def test_session_update_accepts_all_approval_modes(approval_mode: str) -> None:
    req = SessionUpdateRequest(approval_mode=approval_mode)
    assert req.approval_mode == approval_mode


@pytest.mark.parametrize("artifact_type", sorted(ARTIFACT_TYPES))
def test_message_send_accepts_all_artifact_types(artifact_type: str) -> None:
    req = MessageSendRequest(content="hello", artifact_type=artifact_type)
    assert req.artifact_type == artifact_type


@pytest.mark.parametrize("skill_mode", ["auto", "manual", "off"])
def test_message_send_accepts_all_skill_runtime_modes(skill_mode: str) -> None:
    req = MessageSendRequest(content="hello", skill_mode=skill_mode)
    assert req.skill_mode == skill_mode


@pytest.mark.parametrize("mode", sorted(ASSISTANT_MODES))
def test_message_send_accepts_all_modes(mode: str) -> None:
    req = MessageSendRequest(content="hello", mode=mode)
    assert req.mode == mode


@pytest.mark.parametrize("approval_mode", sorted(ASSISTANT_APPROVAL_MODES))
def test_message_send_accepts_all_approval_modes(approval_mode: str) -> None:
    req = MessageSendRequest(content="hello", approval_mode=approval_mode)
    assert req.approval_mode == approval_mode


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("object_file", {"bucket": "docs", "key": "assistant/spec.md"}),
        (
            "library_resource",
            {"resource_type": "prompt", "resource_id": "PR-123"},
        ),
        ("text_snippet", {"name": "Notes", "text": "Review the draft carefully."}),
    ],
)
def test_attachment_create_accepts_supported_json_kinds(
    kind: str,
    payload: dict[str, str],
) -> None:
    req = AttachmentCreateRequest(kind=kind, **payload)
    assert req.kind == kind


@pytest.mark.parametrize("queue_kind", sorted(QUEUED_MESSAGE_KINDS))
def test_queued_message_create_accepts_all_kinds(queue_kind: str) -> None:
    req = QueuedMessageCreateRequest(content="follow up", kind=queue_kind)
    assert req.kind == queue_kind


@pytest.mark.parametrize("mode", sorted(ASSISTANT_MODES))
def test_queued_message_create_accepts_all_modes(mode: str) -> None:
    req = QueuedMessageCreateRequest(content="follow up", mode=mode)
    assert req.mode == mode


@pytest.mark.parametrize("intent_name", sorted(INTENT_NAMES))
def test_intent_plan_request_accepts_all_intent_names(intent_name: str) -> None:
    req = IntentPlanRequest(intent_name=intent_name)
    assert req.intent_name == intent_name


@pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0])
def test_intent_candidate_accepts_boundary_confidences(confidence: float) -> None:
    candidate = IntentCandidate(name="create_tool", confidence=confidence)
    assert candidate.confidence == confidence


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_intent_candidate_rejects_out_of_range_confidence(confidence: float) -> None:
    with pytest.raises(ValidationError):
        IntentCandidate(name="create_tool", confidence=confidence)


@pytest.mark.parametrize("source", ["user", "inferred", "default", "memory", "system"])
def test_intent_slot_accepts_all_sources(source: str) -> None:
    slot = IntentSlot(name="tool_name", source=source)
    assert slot.source == source


@pytest.mark.parametrize("status", ["pending", "blocked", "ready"])
def test_plan_action_accepts_all_statuses(status: str) -> None:
    action = PlanAction(action="validate", description="Validate a draft", status=status)
    assert action.status == status


@pytest.mark.parametrize(
    "mutation_type",
    ["none", "assistant_metadata", "domain_write", "publish_or_promote"],
)
def test_plan_action_accepts_all_mutation_types(mutation_type: str) -> None:
    action = PlanAction(
        action="validate",
        description="Validate a draft",
        mutation_type=mutation_type,
    )
    assert action.mutation_type == mutation_type


@pytest.mark.parametrize(
    ("factory", "payload"),
    [
        (SessionCreateRequest, {"title": "x", "unknown": True}),
        (SessionUpdateRequest, {"title": "x", "unknown": True}),
        (MessageSendRequest, {"content": "hello", "unknown": True}),
        (AttachmentCreateRequest, {"kind": "text_snippet", "text": "hi", "unknown": True}),
        (QueuedMessageCreateRequest, {"content": "hello", "unknown": True}),
        (DraftUpdateRequest, {"version": 1, "unknown": True}),
        (IntentResolveRequest, {"content": "hello", "unknown": True}),
        (IntentPlanRequest, {"intent_name": "create_tool", "unknown": True}),
        (IntentExecuteRequest, {"plan_id": "APLN-1", "unknown": True}),
    ],
)
def test_request_models_forbid_extra_fields(factory, payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        factory(**payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("artifact_type", "database"),
        ("skill_mode", "sometimes"),
        ("mode", "review"),
        ("approval_mode", "always"),
    ],
)
def test_session_create_rejects_invalid_literal_values(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        SessionCreateRequest(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("artifact_type", "database"),
        ("skill_mode", "sometimes"),
        ("mode", "review"),
        ("approval_mode", "always"),
    ],
)
def test_message_send_rejects_invalid_literal_values(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        MessageSendRequest(content="hello", **{field: value})


def test_message_send_rejects_blank_content() -> None:
    with pytest.raises(ValidationError):
        MessageSendRequest(content="")


def test_queued_message_create_rejects_blank_content() -> None:
    with pytest.raises(ValidationError):
        QueuedMessageCreateRequest(content="")


def test_intent_resolve_request_rejects_blank_content() -> None:
    with pytest.raises(ValidationError):
        IntentResolveRequest(content="")


def test_draft_update_requires_version() -> None:
    with pytest.raises(ValidationError):
        DraftUpdateRequest()  # type: ignore[call-arg]


def test_assistant_turn_request_defaults_reflect_aria_design() -> None:
    req = AssistantTurnRequest(session_id="ASST-1", user_message="hello")
    assert req.history == []
    assert req.drafts == []
    assert req.selected_skills == []
    assert req.skill_runtime_mode == "auto"
    assert req.mode == "build"
    assert req.attachments == []
    assert req.steer is False
    assert req.approval_mode == "manual"


def test_assistant_turn_result_defaults_reflect_reply_first_contract() -> None:
    result = AssistantTurnResult()
    assert result.reply == ""
    assert result.questions == []
    assert result.draft_deltas == []
    assert result.tool_calls == []
    assert result.error is None


@pytest.mark.parametrize("domain", sorted(ASSISTANT_DOMAINS))
def test_assistant_domains_enumerate_supported_aria_asset_families(domain: str) -> None:
    assert domain in ASSISTANT_DOMAINS


@pytest.mark.parametrize("kind", sorted(ATTACHMENT_KINDS))
def test_attachment_kinds_cover_all_supported_context_sources(kind: str) -> None:
    assert kind in ATTACHMENT_KINDS


@pytest.mark.parametrize("resource_type", sorted(LIBRARY_RESOURCE_TYPES))
def test_library_resource_types_cover_all_supported_library_attachments(resource_type: str) -> None:
    assert resource_type in LIBRARY_RESOURCE_TYPES


def test_session_response_round_trips_metadata() -> None:
    response = SessionResponse(
        session_id="ASST-1",
        title="Aria",
        owner="@test",
        status="active",
        goal="Ship a tool",
        metadata_={"assistant_mode": "plan"},
        active_draft_id=None,
        created_at=NOW,
        updated_at=NOW,
    )
    assert response.metadata_["assistant_mode"] == "plan"


def test_message_response_keeps_metadata_alias_name() -> None:
    response = MessageResponse(
        message_id="AMSG-1",
        session_id="ASST-1",
        role="assistant",
        content="hello",
        metadata_={"process_steps": [{"label": "Thinking"}]},
        sequence_number=2,
        created_at=NOW,
    )
    assert response.metadata_["process_steps"][0]["label"] == "Thinking"


def test_draft_response_accepts_validation_and_test_reports() -> None:
    response = DraftResponse(
        draft_id="ADRF-1",
        session_id="ASST-1",
        artifact_type="tool",
        status="validated",
        title="Validate email",
        summary="Tool summary",
        spec={},
        artifact={},
        validation_report={"valid": True, "errors": [], "warnings": []},
        test_report={"passed": True, "total": 1, "failures": 0, "details": [], "error": None},
        target_registry_id=None,
        version=3,
        created_by="@test",
        updated_by="@test",
        created_at=NOW,
        updated_at=NOW,
    )
    assert response.validation_report == {"valid": True, "errors": [], "warnings": []}
    assert response.test_report == {
        "passed": True,
        "total": 1,
        "failures": 0,
        "details": [],
        "error": None,
    }


def test_run_response_preserves_trace_fields() -> None:
    response = RunResponse(
        run_id="ARN-1",
        session_id="ASST-1",
        draft_id="ADRF-1",
        status="completed",
        engine="OpenAIAssistantEngine",
        model="gpt-5.2",
        input_summary="Build me a tool",
        output_summary="Drafted a tool",
        trace_id="trace-1",
        mlflow_run_id="mlflow-1",
        error=None,
        started_at=NOW,
        completed_at=NOW,
    )
    assert response.trace_id == "trace-1"
    assert response.mlflow_run_id == "mlflow-1"


def test_attachment_response_serializes_context_snapshot_fields() -> None:
    response = AttachmentResponse(
        attachment_id="AATT-1",
        session_id="ASST-1",
        kind="text_snippet",
        ref_type="text",
        ref_id="inline",
        name="Notes",
        content_text="Review this carefully.",
        bytes_size=22,
        truncated=False,
        metadata_={"source": "manual"},
        created_by="@test",
        created_at=NOW,
    )
    assert response.metadata_["source"] == "manual"


def test_queued_message_response_serializes_ordering_fields() -> None:
    response = QueuedMessageResponse(
        queue_id="AQUE-1",
        session_id="ASST-1",
        content="Follow up",
        mode="build",
        kind="queued",
        position=2,
        status="pending",
        created_by="@test",
        created_at=NOW,
    )
    assert response.position == 2
    assert response.kind == "queued"


def test_validation_report_defaults_are_empty_and_invalid() -> None:
    report = ValidationReport()
    assert report.valid is False
    assert report.errors == []
    assert report.warnings == []


def test_test_report_defaults_are_empty_and_failed() -> None:
    report = AssistantTestReport()
    assert report.passed is False
    assert report.total == 0
    assert report.failures == 0
    assert report.details == []
    assert report.error is None


def test_turn_response_wraps_assistant_message_questions_drafts_and_run() -> None:
    response = TurnResponse(
        assistant_message=MessageResponse(
            message_id="AMSG-1",
            session_id="ASST-1",
            role="assistant",
            content="I drafted a tool.",
            metadata_={"selected_skills": []},
            sequence_number=2,
            created_at=NOW,
        ),
        questions=[ClarifyingQuestion(question="What should it be named?")],
        draft_updates=[
            DraftResponse(
                draft_id="ADRF-1",
                session_id="ASST-1",
                artifact_type="tool",
                status="draft",
                title="Untitled tool",
                summary="",
                spec={},
                artifact={},
                validation_report=None,
                test_report=None,
                target_registry_id=None,
                version=1,
                created_by="@test",
                updated_by="@test",
                created_at=NOW,
                updated_at=NOW,
            )
        ],
        run=RunResponse(
            run_id="ARN-1",
            session_id="ASST-1",
            draft_id="ADRF-1",
            status="completed",
            engine="FakeAssistantEngine",
            model="fake",
            input_summary="Build a tool",
            output_summary="Draft created",
            trace_id="trace-1",
            mlflow_run_id=None,
            error=None,
            started_at=NOW,
            completed_at=NOW,
        ),
        tool_calls=[],
    )
    assert response.assistant_message.content == "I drafted a tool."
    assert response.questions[0].question == "What should it be named?"
    assert response.draft_updates[0].draft_id == "ADRF-1"
    assert response.run is not None


def test_operation_status_response_embeds_optional_run() -> None:
    response = OperationStatusResponse(
        operation_id="AOP-1",
        session_id="ASST-1",
        plan_id="APLN-1",
        intent_name="create_tool",
        status="completed",
        created_at=NOW,
        updated_at=NOW,
        result={"draft_id": "ADRF-1"},
        run=RunResponse(
            run_id="ARN-1",
            session_id="ASST-1",
            draft_id="ADRF-1",
            status="completed",
            engine="FakeAssistantEngine",
            model="fake",
            input_summary="Build a tool",
            output_summary="Draft created",
            trace_id="trace-1",
            mlflow_run_id=None,
            error=None,
            started_at=NOW,
            completed_at=NOW,
        ),
    )
    assert response.result["draft_id"] == "ADRF-1"
    assert response.run is not None and response.run.run_id == "ARN-1"


def test_draft_delta_defaults_match_incremental_update_contract() -> None:
    delta = DraftDelta()
    assert delta.draft_id is None
    assert delta.artifact_type is None
    assert delta.title == ""
    assert delta.summary == ""
    assert delta.spec == {}
    assert delta.artifact == {}
