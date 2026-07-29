"""AssistantService — orchestrator for sessions, messages, drafts, runs."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import logging
import re
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal, cast

from pydantic import ValidationError

from caliber.assistant.agent_tools import AgentToolDeps, AssistantAgentToolset
from caliber.assistant.context_builder import AssistantContextBuilder
from caliber.assistant.engine import AssistantEngine
from caliber.assistant.models import (
    ASSISTANT_APPROVAL_MODES,
    ASSISTANT_DOMAINS,
    ASSISTANT_MODES,
    ATTACHMENT_TEXT_MAX_CHARS,
    DEFAULT_APPROVAL_MODE,
    DEFAULT_ASSISTANT_MODE,
    INTENT_DOMAINS,
    INTENT_NAMES,
    QUEUED_MESSAGE_KINDS,
    AssistantMode,
    AssistantTurnRequest,
    AttachmentResponse,
    ClarifyingQuestion,
    DraftDelta,
    DraftResponse,
    DraftUpdateRequest,
    IntentCandidate,
    IntentExecuteRequest,
    IntentExecuteResponse,
    IntentPlanRequest,
    IntentPlanResponse,
    IntentResolveRequest,
    IntentResolveResponse,
    IntentSlot,
    MessageResponse,
    MessageSendRequest,
    OperationStatusResponse,
    PlanAction,
    QueuedMessageResponse,
    RunResponse,
    SessionCreateRequest,
    SessionResponse,
    SessionUpdateRequest,
    TestReport,
    TurnResponse,
    ValidationReport,
)
from caliber.assistant.publisher import AssistantPublisher
from caliber.assistant.skill_runtime import (
    AssistantSkillResolutionRequest,
    normalize_skill_names,
    resolve_assistant_skills,
    runtime_metadata_from_session,
    update_session_skill_runtime_metadata,
)
from caliber.assistant.task_context import update_session_task_context_metadata
from caliber.assistant.task_manager import TaskManager
from caliber.assistant.tracing import AssistantTracer, AssistantTraceSpan
from caliber.assistant.validators import validate_draft
from caliber.audit import record as audit_record
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberApprovalRequest,
    CaliberAssistantAttachment,
    CaliberAssistantDraft,
    CaliberAssistantMessage,
    CaliberAssistantPublishEvent,
    CaliberAssistantQueuedMessage,
    CaliberAssistantRun,
    CaliberAssistantSession,
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberKnowledgeBase,
    CaliberRefinementJob,
    CaliberSkill,
    CaliberToolRegistry,
    CaliberVerificationItem,
    CaliberWorkflow,
)
from caliber.eval.gate import DEFAULT_MAX_REGRESSION_DELTA, DEFAULT_MIN_AGGREGATE_SCORE
from caliber.ids import (
    new_approval_id,
    new_assistant_attachment_id,
    new_assistant_draft_id,
    new_assistant_message_id,
    new_assistant_publish_id,
    new_assistant_queued_message_id,
    new_assistant_run_id,
    new_assistant_session_id,
    new_eval_dataset_id,
    new_eval_example_id,
    new_item_id,
    new_job_id,
)
from caliber.observability.trace import current_trace_id, new_trace_id
from caliber.schemas import (
    EvalExampleCreateRequest,
    McpServerCreateRequest,
    PromptOptimizationRunRequest,
    SkillCreateRequest,
    WorkflowCalibrationRunRequest,
)
from caliber.skill_packages import build_skill_package
from caliber.tool_sandbox.models import (
    ToolSandboxTestCase,
    ToolSandboxTestSuiteRequest,
    ToolSandboxTestSuiteResult,
)
from caliber.tool_sandbox.service import sandbox_from_optional_config
from caliber.workflows.compiler import CompileError, compile_workflow
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.tools import InMemoryToolResolver

logger = logging.getLogger(__name__)

_PLAN_ID_PREFIX = "APLN-"
_MAX_PLANS_PER_SESSION = 25
_MAX_OPERATIONS_PER_SESSION = 50


@dataclass(frozen=True)
class AssistantRuntimeSettings:
    enabled: bool = True
    skill_runtime_enabled: bool = True
    disabled_intents: tuple[str, ...] = ()
    disabled_domains: tuple[str, ...] = ()
    max_turns: int = 30
    max_questions_per_turn: int = 3
    max_drafts_per_session: int = 20
    max_attachments_per_session: int = 25
    max_queued_per_session: int = 20
    publish_requires_approval: bool = True
    tool_source_max_bytes: int = 200_000
    run_timeout_seconds: float = 60.0


_INTENT_REQUIRED_SLOTS: dict[str, tuple[str, ...]] = {
    "create_tool": ("tool_name", "source", "callable_name", "tests"),
    "create_skill": ("skill_name", "description", "content"),
    "create_workflow": ("workflow_name", "manifest"),
    "create_mcp_server": ("server_name", "transport"),
    "create_prompt": ("prompt_name", "template"),
    "edit_prompt": ("prompt_name", "template"),
    "generate_test_cases": ("prompt_name",),
    "save_eval_dataset": ("dataset_name", "examples"),
    "run_prompt_optimization": (
        "agent_id",
        "eval_dataset_id",
        "optimizer_type",
        "scorers",
        "gate.min_aggregate_score",
        "gate.max_regression_delta",
    ),
    "review_optimization_result": ("job_id",),
    "run_workflow_calibration": ("workflow_id",),
    "review_workflow_calibration_result": ("job_id",),
    "propose_promotion": ("prompt_name", "target_alias"),
}

_INTENT_ACTIONS: dict[str, list[PlanAction]] = {
    "create_tool": [
        PlanAction(
            action="create_validate_test_tool_draft",
            description="Create a tool draft, validate its source, and run sandbox tests.",
            mutation_type="assistant_metadata",
            result_type="tool_draft",
            required_scopes=["caliber.operator"],
        ),
    ],
    "create_skill": [
        PlanAction(
            action="create_validate_package_skill_draft",
            description="Create a skill draft, validate it, and build its package preview.",
            mutation_type="assistant_metadata",
            result_type="skill_draft",
            required_scopes=["caliber.operator"],
        ),
    ],
    "create_workflow": [
        PlanAction(
            action="create_validate_compile_workflow_draft",
            description="Create a workflow draft, validate its manifest, and compile a preview.",
            mutation_type="assistant_metadata",
            result_type="workflow_draft",
            required_scopes=["caliber.operator"],
        ),
    ],
    "create_mcp_server": [
        PlanAction(
            action="create_validate_test_mcp_server_draft",
            description="Create an MCP server draft, validate config, and preview connection.",
            mutation_type="assistant_metadata",
            result_type="mcp_server_draft",
            required_scopes=["caliber.operator"],
        ),
    ],
    "create_prompt": [
        PlanAction(
            action="register_prompt",
            description="Register a non-live prompt version in MLflow Prompt Registry.",
            mutation_type="domain_write",
            result_type="prompt_version",
            required_scopes=["caliber.operator"],
        ),
    ],
    "edit_prompt": [
        PlanAction(
            action="register_prompt_version",
            description="Create a new non-live prompt version in MLflow Prompt Registry.",
            mutation_type="domain_write",
            result_type="prompt_version",
            required_scopes=["caliber.operator"],
        ),
    ],
    "generate_test_cases": [
        PlanAction(
            action="generate_test_cases",
            description="Generate candidate test cases for the selected prompt.",
            mutation_type="assistant_metadata",
            result_type="test_cases",
            required_scopes=["caliber.operator"],
        ),
    ],
    "save_eval_dataset": [
        PlanAction(
            action="save_eval_dataset",
            description="Persist a curated evaluation dataset for prompt calibration runs.",
            mutation_type="domain_write",
            result_type="eval_dataset",
            required_scopes=["caliber.operator"],
        ),
    ],
    "run_prompt_optimization": [
        PlanAction(
            action="enqueue_prompt_optimization",
            description="Queue a prompt calibration job against an active eval dataset.",
            mutation_type="domain_write",
            result_type="optimization_run",
            required_scopes=["caliber.operator"],
        ),
    ],
    "review_optimization_result": [
        PlanAction(
            action="review_optimization_result",
            description="Summarize and inspect calibration job outputs.",
            mutation_type="none",
            result_type="optimization_review",
        ),
    ],
    "run_workflow_calibration": [
        PlanAction(
            action="enqueue_workflow_calibration",
            description="Queue workflow calibration against the deploy-gate eval dataset.",
            mutation_type="domain_write",
            result_type="workflow_calibration_run",
            required_scopes=["caliber.operator"],
        ),
    ],
    "review_workflow_calibration_result": [
        PlanAction(
            action="review_workflow_calibration_result",
            description="Summarize workflow calibration candidates and selected winner.",
            mutation_type="none",
            result_type="workflow_calibration_review",
        ),
    ],
    "propose_promotion": [
        PlanAction(
            action="propose_promotion",
            description="Prepare a promotion proposal for explicit human approval.",
            mutation_type="publish_or_promote",
            result_type="promotion_proposal",
            required_scopes=["caliber.operator"],
        ),
    ],
}

_SCORE_HINTS: dict[str, str] = {
    "create_tool": "Detected tool-creation language in the user request.",
    "create_skill": "Detected skill-creation language in the user request.",
    "create_workflow": "Detected workflow-creation language in the user request.",
    "create_mcp_server": "Detected MCP server setup language in the user request.",
    "run_prompt_optimization": "Detected prompt calibration language in the user request.",
    "create_prompt": "Detected prompt-creation language in the user request.",
    "edit_prompt": "Detected prompt-editing language in the user request.",
    "generate_test_cases": "Detected test-case generation language in the user request.",
    "save_eval_dataset": "Detected evaluation dataset save language in the user request.",
    "review_optimization_result": "Detected calibration review language in the user request.",
    "run_workflow_calibration": "Detected workflow calibration language in the user request.",
    "review_workflow_calibration_result": "Detected workflow calibration review language in the user request.",
    "propose_promotion": "Detected promotion language in the user request.",
}


def _new_plan_id() -> str:
    return f"{_PLAN_ID_PREFIX}{new_assistant_run_id().split('-', 1)[1]}"


def _non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return len(value) > 0
    if isinstance(value, dict):
        return len(value) > 0
    return True


def _parse_prompt_ref_name(value: str) -> str | None:
    if not value:
        return None
    ref_match = re.match(r"prompts:/([^@/]+)", value)
    if ref_match:
        return ref_match.group(1)
    return None


def _extract_template_from_text(content: str) -> str | None:
    template_match = re.search(r"template\s*:\s*(.+)", content, flags=re.IGNORECASE | re.DOTALL)
    if template_match:
        candidate = template_match.group(1).strip()
        if candidate:
            return candidate
    return None


def _operation_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _assistant_process_steps(
    *,
    questions: list[ClarifyingQuestion],
    tool_calls: list[Any],
    draft_updates: list[DraftResponse],
    approval_mode: str,
    error: bool = False,
) -> list[dict[str, str]]:
    """Summarize the completed turn as UI-friendly process steps.

    This is not a streaming event log; it is a compact post-turn trail the UI
    can render under the assistant message so users see how Aria arrived at the
    answer and whether a draft now needs review.
    """

    steps: list[dict[str, str]] = [{"key": "thinking", "label": "Thinking", "tone": "neutral"}]

    if tool_calls:
        ok = all(bool(getattr(call, "ok", False)) for call in tool_calls)
        count = len(tool_calls)
        steps.append(
            {
                "key": "actions",
                "label": f"{count} action" if count == 1 else f"{count} actions",
                "tone": "success" if ok else "warning",
            }
        )

    if questions:
        steps.append({"key": "needs_input", "label": "Needs input", "tone": "warning"})

    if draft_updates:
        statuses = {draft.status for draft in draft_updates}
        steps.append({"key": "drafted", "label": "Drafted", "tone": "success"})

        if statuses & {
            "validated",
            "testing",
            "tested",
            "approved",
            "publishing",
            "published",
            "test_failed",
            "publish_failed",
        }:
            steps.append({"key": "validated", "label": "Validated", "tone": "success"})
        if "validation_failed" in statuses:
            steps.append(
                {"key": "validation_failed", "label": "Validation failed", "tone": "error"}
            )
            return steps

        if statuses & {"tested", "approved", "publishing", "published", "publish_failed"}:
            steps.append({"key": "tested", "label": "Tested", "tone": "success"})
        if "test_failed" in statuses:
            steps.append({"key": "test_failed", "label": "Test failed", "tone": "error"})
            return steps

        if statuses & {"approved", "publishing", "published", "publish_failed"}:
            steps.append({"key": "approved", "label": "Approved", "tone": "success"})

        if "published" in statuses:
            steps.append({"key": "published", "label": "Published", "tone": "success"})
        elif "publish_failed" in statuses:
            steps.append({"key": "publish_failed", "label": "Publish failed", "tone": "error"})
        elif approval_mode in {"manual", "auto_safe"}:
            steps.append({"key": "review", "label": "Review required", "tone": "warning"})

    if error:
        steps.append({"key": "error", "label": "Error", "tone": "error"})
    return steps


def normalize_disabled_intents(raw: Any, *, strict: bool = False) -> tuple[str, ...]:
    """Normalize comma/list disabled-intent config into known intent names."""
    if raw is None:
        return ()
    if isinstance(raw, str):
        values = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, list | tuple | set):
        values = [str(part).strip() for part in raw]
    else:
        if strict:
            raise ValueError("disabled_intents must be a comma string or list of strings")
        return ()

    normalized: list[str] = []
    unknown: list[str] = []
    for value in values:
        if not value:
            continue
        if value not in INTENT_NAMES:
            unknown.append(value)
            continue
        if value not in normalized:
            normalized.append(value)
    if unknown and strict:
        raise ValueError(f"unknown assistant intent(s): {', '.join(sorted(set(unknown)))}")
    return tuple(normalized)


def normalize_disabled_domains(raw: Any, *, strict: bool = False) -> tuple[str, ...]:
    """Normalize comma/list disabled-domain config into known assistant domains."""
    if raw is None:
        return ()
    if isinstance(raw, str):
        values = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, list | tuple | set):
        values = [str(part).strip() for part in raw]
    else:
        if strict:
            raise ValueError("disabled_domains must be a comma string or list of strings")
        return ()

    normalized: list[str] = []
    unknown: list[str] = []
    for value in values:
        if not value:
            continue
        if value not in ASSISTANT_DOMAINS:
            unknown.append(value)
            continue
        if value not in normalized:
            normalized.append(value)
    if unknown and strict:
        raise ValueError(f"unknown assistant domain(s): {', '.join(sorted(set(unknown)))}")
    return tuple(normalized)


def default_prompt_fetcher(name: str) -> str | None:
    """Best-effort fetch of a prompt's template text from the MLflow registry.

    Grounds test-case generation in the real prompt (golden-path roadmap, Wave 5.3).
    Guarded: returns ``None`` on any failure (MLflow absent, prompt missing, no
    ``[llm]`` extra) so generation cleanly falls back to a name-only instruction.
    """
    try:
        import mlflow  # noqa: PLC0415

        loader = getattr(mlflow, "load_prompt", None)
        if not callable(loader):
            return None
        for ref in (f"prompts:/{name}@prod", f"prompts:/{name}@latest", f"prompts:/{name}"):
            try:
                prompt = loader(ref)
            except Exception:  # noqa: S112 - best-effort probe across alias forms
                continue
            template = getattr(prompt, "template", None)
            if isinstance(template, str) and template.strip():
                return template
    except Exception:
        return None
    return None


class AssistantService:
    """High-level operations for assistant authoring.

    Every public method receives a ``session_factory`` (SQLAlchemy sessionmaker)
    and a ``user`` string from the route layer.
    """

    def __init__(
        self,
        engine: AssistantEngine,
        publisher: AssistantPublisher | None = None,
        settings: AssistantRuntimeSettings | None = None,
        tracer: AssistantTracer | None = None,
        prompt_fetcher: Callable[[str], str | None] | None = None,
        runtime_config: Any | None = None,
    ) -> None:
        self._engine = engine
        self._publisher = publisher or AssistantPublisher()
        self._settings = settings or AssistantRuntimeSettings()
        self._tracer = tracer or AssistantTracer()
        self._context_builder = AssistantContextBuilder()
        self._task_manager = TaskManager()
        # Optional: fetch a prompt's template text to ground test-case generation
        # (Wave 5.3). None → generation uses a name-only instruction.
        self._prompt_fetcher = prompt_fetcher
        self._runtime_config = runtime_config

    def _current_or_new_trace_id(self) -> str:
        return current_trace_id() or new_trace_id()

    def _ensure_correlation_id(self, metadata: dict[str, Any]) -> str:
        raw = metadata.get("assistant_correlation_id")
        if isinstance(raw, str) and raw.strip():
            return raw
        correlation_id = f"acorr-{new_trace_id()}"
        metadata["assistant_correlation_id"] = correlation_id
        return correlation_id

    def _disabled_intents(self) -> set[str]:
        return set(normalize_disabled_intents(self._settings.disabled_intents))

    def _disabled_domains(self) -> set[str]:
        return set(normalize_disabled_domains(self._settings.disabled_domains))

    def _execution_disabled_reason(self, intent_name: str) -> tuple[str, str] | None:
        if intent_name in self._disabled_intents():
            return ("intent", intent_name)
        domain = INTENT_DOMAINS.get(intent_name)
        if domain is not None and domain in self._disabled_domains():
            return ("domain", domain)
        return None

    def _get_owned_session(
        self,
        db: Any,
        session_id: str,
        user: str | None,
    ) -> CaliberAssistantSession | None:
        row: CaliberAssistantSession | None = db.get(CaliberAssistantSession, session_id)
        if row is None:
            return None
        if user is not None and row.owner != user:
            return None
        return row

    def _result_envelope(
        self,
        *,
        result_type: str,
        status: str,
        summary: str = "",
        ids: dict[str, Any] | None = None,
        links: list[dict[str, Any]] | None = None,
        warnings: list[str] | None = None,
        next_actions: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "result_type": result_type,
            "status": status,
            "summary": summary,
            "ids": ids or {},
            "links": links or [],
            "warnings": warnings or [],
            "next_actions": next_actions or [],
            "mlflow_trace_id": None,
            "mlflow_run_id": None,
        }
        if extra:
            payload.update(extra)
        return payload

    @contextlib.contextmanager
    def _trace_span(
        self,
        name: str,
        *,
        trace_id: str,
        correlation_id: str,
        attributes: dict[str, Any] | None = None,
        user: str | None = None,
    ) -> Iterator[Any]:
        session_id = (attributes or {}).get("caliber.assistant.session_id")
        with self._tracer.span(
            name,
            trace_id=trace_id,
            correlation_id=correlation_id,
            attributes=attributes,
        ) as span:
            # Stamp MLflow-native session/user so multi-turn assistant chats
            # group into one session and Observability's Session/User populate.
            self._tracer.annotate_trace(
                session_id=str(session_id) if session_id else None,
                user=user,
            )
            yield span

    def _span_value(
        self,
        *spans: AssistantTraceSpan | None,
        field_name: str,
    ) -> str | None:
        for span in spans:
            if span is None:
                continue
            value = getattr(span, field_name, None)
            if isinstance(value, str) and value:
                return value
        return None

    def _attach_operation_observability(
        self,
        payload: dict[str, Any],
        *,
        trace_id: str,
        correlation_id: str,
        mlflow_trace_id: str | None = None,
        mlflow_run_id: str | None = None,
    ) -> dict[str, Any]:
        out = dict(payload)
        out.setdefault("trace_id", trace_id)
        out.setdefault("correlation_id", correlation_id)
        if mlflow_trace_id is not None:
            out["mlflow_trace_id"] = mlflow_trace_id
        else:
            out.setdefault("mlflow_trace_id", None)
        if mlflow_run_id is not None:
            out["mlflow_run_id"] = mlflow_run_id
        else:
            out.setdefault("mlflow_run_id", None)
        return out

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def create_session(
        self,
        body: SessionCreateRequest,
        *,
        session_factory: Any,
        user: str,
    ) -> SessionResponse:
        sid = new_assistant_session_id()
        metadata = copy.deepcopy(body.metadata_)
        if body.artifact_type is not None:
            metadata["artifact_type"] = body.artifact_type
        metadata = update_session_skill_runtime_metadata(
            metadata,
            skill_mode=body.skill_mode,
            pinned_skill_names=body.pinned_skill_names if body.pinned_skill_names else None,
        )
        if body.mode is not None:
            metadata["assistant_mode"] = body.mode
        if body.approval_mode is not None:
            metadata["assistant_approval_mode"] = body.approval_mode
        self._ensure_correlation_id(metadata)
        with session_factory() as db:
            row = CaliberAssistantSession(
                session_id=sid,
                title=body.title or "New session",
                owner=user,
                goal=body.goal,
                metadata_=metadata,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return SessionResponse.model_validate(row)

    def get_session(
        self,
        session_id: str,
        *,
        session_factory: Any,
        user: str | None = None,
    ) -> SessionResponse | None:
        with session_factory() as db:
            row = self._get_owned_session(db, session_id, user)
            if row is None:
                return None
            return SessionResponse.model_validate(row)

    def list_sessions(
        self,
        *,
        session_factory: Any,
        user: str | None = None,
        owner: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[SessionResponse]:
        with session_factory() as db:
            q = db.query(CaliberAssistantSession).order_by(
                CaliberAssistantSession.updated_at.desc(),
            )
            if owner:
                q = q.filter(CaliberAssistantSession.owner == owner)
            elif user is not None:
                q = q.filter(CaliberAssistantSession.owner == user)
            if cursor:
                q = q.filter(CaliberAssistantSession.session_id < cursor)
            rows = q.limit(limit).all()
            return [SessionResponse.model_validate(r) for r in rows]

    def update_session(
        self,
        session_id: str,
        body: SessionUpdateRequest,
        *,
        session_factory: Any,
        user: str,
    ) -> SessionResponse | None:
        with session_factory() as db:
            row = self._get_owned_session(db, session_id, user)
            if row is None:
                return None
            if body.title is not None:
                row.title = body.title
            if body.status is not None:
                row.status = body.status
            if body.metadata_ is not None:
                row.metadata_ = body.metadata_
            if (
                body.skill_mode is not None
                or body.pinned_skill_names is not None
                or body.disabled_skill_names is not None
            ):
                row.metadata_ = update_session_skill_runtime_metadata(
                    row.metadata_,
                    skill_mode=body.skill_mode,
                    pinned_skill_names=body.pinned_skill_names,
                    disabled_skill_names=body.disabled_skill_names,
                )
            if body.mode is not None or body.approval_mode is not None:
                md = dict(row.metadata_ or {})
                if body.mode is not None:
                    md["assistant_mode"] = body.mode
                if body.approval_mode is not None:
                    md["assistant_approval_mode"] = body.approval_mode
                row.metadata_ = md
            db.commit()
            db.refresh(row)
            return SessionResponse.model_validate(row)

    # ------------------------------------------------------------------
    # Intent-driven planning and execution
    # ------------------------------------------------------------------

    def resolve_intent(
        self,
        session_id: str,
        body: IntentResolveRequest,
        *,
        session_factory: Any,
        user: str,
    ) -> IntentResolveResponse:
        with session_factory() as db:
            session_row = self._get_owned_session(db, session_id, user)
            if session_row is None:
                raise ValueError(f"Session {session_id} not found")

            metadata, workbench = self._metadata_and_workbench(session_row.metadata_)
            correlation_id = self._ensure_correlation_id(metadata)
            trace_id = self._current_or_new_trace_id()
            with self._trace_span(
                "caliber.assistant.resolve_intent",
                trace_id=trace_id,
                correlation_id=correlation_id,
                user=user,
                attributes={
                    "caliber.assistant.session_id": session_id,
                    "caliber.assistant.context_keys": sorted(body.context),
                    "caliber.assistant.content_bytes": len(body.content.encode("utf-8")),
                },
            ):
                resolved = self._resolve_intent_content(
                    content=body.content,
                    context=body.context,
                    session_metadata=metadata,
                )
            workbench["latest_intent"] = resolved.model_dump(mode="json")
            metadata["intent_workbench"] = workbench
            session_row.metadata_ = metadata
            db.commit()
            return resolved

    def create_intent_plan(
        self,
        session_id: str,
        body: IntentPlanRequest,
        *,
        session_factory: Any,
        user: str,
    ) -> IntentPlanResponse:
        with session_factory() as db:
            session_row = self._get_owned_session(db, session_id, user)
            if session_row is None:
                raise ValueError(f"Session {session_id} not found")

            metadata, workbench = self._metadata_and_workbench(session_row.metadata_)
            correlation_id = self._ensure_correlation_id(metadata)
            trace_id = self._current_or_new_trace_id()
            with self._trace_span(
                "caliber.assistant.create_intent_plan",
                trace_id=trace_id,
                correlation_id=correlation_id,
                user=user,
                attributes={
                    "caliber.assistant.session_id": session_id,
                    "caliber.assistant.intent_name": body.intent_name or "",
                    "caliber.assistant.has_content": body.content is not None,
                    "caliber.assistant.override_keys": sorted(body.slot_overrides),
                    "caliber.assistant.context_keys": sorted(body.context),
                },
            ):
                resolved: IntentResolveResponse | None = None
                if body.content:
                    resolved = self._resolve_intent_content(
                        content=body.content,
                        context=body.context,
                        session_metadata=metadata,
                    )
                    workbench["latest_intent"] = resolved.model_dump(mode="json")
                else:
                    raw_intent = workbench.get("latest_intent")
                    if isinstance(raw_intent, dict):
                        try:
                            resolved = IntentResolveResponse.model_validate(raw_intent)
                        except Exception:
                            logger.warning(
                                "discarding unparseable stored intent for session %s",
                                session_id,
                                exc_info=True,
                            )
                            resolved = None

                if body.intent_name is not None:
                    if resolved is None:
                        resolved = IntentResolveResponse(
                            intent=IntentCandidate(
                                name=body.intent_name,
                                confidence=0.8,
                                rationale="Intent explicitly provided by user.",
                            ),
                        )
                    else:
                        resolved.intent = IntentCandidate(
                            name=body.intent_name,
                            confidence=max(resolved.intent.confidence, 0.8),
                            rationale="Intent explicitly provided by user.",
                        )

                if resolved is None:
                    raise ValueError(
                        "No intent context found; provide content or resolve intent first"
                    )

                plan = self._build_plan_from_resolved(
                    resolved=resolved,
                    slot_overrides=body.slot_overrides,
                    session_metadata=metadata,
                )

            plans = workbench.get("plans")
            if not isinstance(plans, dict):
                plans = {}
            plans[plan.plan_id] = plan.model_dump(mode="json")
            plans = self._trim_workbench_records(
                plans,
                max_records=_MAX_PLANS_PER_SESSION,
                preserve_key=plan.plan_id,
            )

            workbench["plans"] = plans
            workbench["latest_plan"] = plan.model_dump(mode="json")
            workbench["latest_plan_id"] = plan.plan_id
            metadata["intent_workbench"] = workbench

            session_row.metadata_ = metadata
            db.commit()
            return plan

    def get_latest_plan(
        self,
        session_id: str,
        *,
        session_factory: Any,
        user: str | None = None,
    ) -> IntentPlanResponse | None:
        with session_factory() as db:
            session_row = self._get_owned_session(db, session_id, user)
            if session_row is None:
                return None
            _metadata, workbench = self._metadata_and_workbench(session_row.metadata_)
            raw_plan = workbench.get("latest_plan")
            if not isinstance(raw_plan, dict):
                return None
            try:
                return IntentPlanResponse.model_validate(raw_plan)
            except Exception:
                logger.warning(
                    "discarding unparseable stored plan for session %s",
                    session_id,
                    exc_info=True,
                )
                return None

    def execute_intent_plan(  # noqa: PLR0912, PLR0915
        self,
        session_id: str,
        body: IntentExecuteRequest,
        *,
        session_factory: Any,
        user: str,
    ) -> IntentExecuteResponse:
        with session_factory() as db:
            session_row = self._get_owned_session(db, session_id, user)
            if session_row is None:
                raise ValueError(f"Session {session_id} not found")

            metadata, workbench = self._metadata_and_workbench(session_row.metadata_)
            correlation_id = self._ensure_correlation_id(metadata)
            trace_id = self._current_or_new_trace_id()
            raw_plan: dict[str, Any] | None = None
            if body.plan_id:
                plans = workbench.get("plans")
                if isinstance(plans, dict):
                    maybe_plan = plans.get(body.plan_id)
                    if isinstance(maybe_plan, dict):
                        raw_plan = maybe_plan

            if raw_plan is None:
                maybe_latest = workbench.get("latest_plan")
                if isinstance(maybe_latest, dict):
                    raw_plan = maybe_latest

            if raw_plan is None:
                raise ValueError("No plan found to execute")

            plan = IntentPlanResponse.model_validate(raw_plan)
            if body.plan_id and plan.plan_id != body.plan_id:
                raise ValueError(f"Plan {body.plan_id!r} not found")
            if not plan.ready:
                raise ValueError(f"Plan is not ready; missing slots: {plan.missing_slots}")
            if plan.requires_confirmation and not body.confirm:
                raise ValueError("Execution requires explicit confirmation")

            operation_id = new_assistant_run_id()
            started = datetime.now(timezone.utc)

            run_row = CaliberAssistantRun(
                run_id=operation_id,
                session_id=session_id,
                status="running",
                engine="assistant-intent",
                model=plan.intent.name,
                input_summary=f"execute {plan.intent.name} ({plan.plan_id})",
                trace_id=trace_id,
            )
            db.add(run_row)

            operations = workbench.get("operations")
            if not isinstance(operations, dict):
                operations = {}
            operations[operation_id] = {
                "operation_id": operation_id,
                "plan_id": plan.plan_id,
                "intent_name": plan.intent.name,
                "status": "running",
                "executed_action": "",
                "created_at": started.isoformat(),
                "updated_at": started.isoformat(),
                "trace_id": trace_id,
                "correlation_id": correlation_id,
                "result": {},
            }
            workbench["operations"] = self._trim_workbench_records(
                operations,
                max_records=_MAX_OPERATIONS_PER_SESSION,
                preserve_key=operation_id,
            )
            metadata["intent_workbench"] = workbench
            session_row.metadata_ = metadata

            db.commit()

        executed_action = ""
        result_payload: dict[str, Any] = {}
        run_status = "completed"
        error_text: str | None = None
        execute_span: AssistantTraceSpan | None = None
        adapter_span: AssistantTraceSpan | None = None

        try:
            with self._trace_span(
                "caliber.assistant.execute_intent",
                trace_id=trace_id,
                correlation_id=correlation_id,
                user=user,
                attributes={
                    "caliber.assistant.session_id": session_id,
                    "caliber.assistant.plan_id": plan.plan_id,
                    "caliber.assistant.operation_id": operation_id,
                    "caliber.assistant.intent_name": plan.intent.name,
                    "caliber.assistant.requires_confirmation": plan.requires_confirmation,
                    "caliber.assistant.confirmed": body.confirm,
                    "caliber.assistant.action_count": len(plan.actions),
                },
            ) as execute_span:
                disabled_reason = self._execution_disabled_reason(plan.intent.name)
                if disabled_reason is not None:
                    disabled_kind, disabled_name = disabled_reason
                    executed_action = f"{disabled_kind}_disabled"
                    result_payload = self._result_envelope(
                        result_type="blocked",
                        status="blocked",
                        summary=(
                            f"Execution for assistant {disabled_kind} "
                            f"{disabled_name!r} is disabled."
                        ),
                        ids={
                            "intent_name": plan.intent.name,
                            "disabled_kind": disabled_kind,
                            "disabled_name": disabled_name,
                        },
                        warnings=[
                            (
                                f"Assistant execution for {disabled_kind} {disabled_name!r} "
                                "is disabled by rollout configuration."
                            )
                        ],
                    )
                    execute_span.set_attribute("caliber.assistant.result_type", "blocked")
                    execute_span.set_attribute("caliber.assistant.result_status", "blocked")
                    execute_span.set_attribute("caliber.assistant.disabled_kind", disabled_kind)
                    execute_span.set_attribute("caliber.assistant.disabled_name", disabled_name)
                else:
                    with self._trace_span(
                        f"caliber.assistant.adapter.{plan.intent.name}",
                        trace_id=trace_id,
                        correlation_id=correlation_id,
                        user=user,
                        attributes={
                            "caliber.assistant.session_id": session_id,
                            "caliber.assistant.plan_id": plan.plan_id,
                            "caliber.assistant.operation_id": operation_id,
                            "caliber.assistant.intent_name": plan.intent.name,
                        },
                    ) as adapter_span:
                        if plan.intent.name == "create_tool":
                            executed_action = "create_validate_test_tool_draft"
                            result_payload = self._execute_create_tool(
                                plan,
                                session_id=session_id,
                                session_factory=session_factory,
                                user=user,
                            )
                        elif plan.intent.name == "create_skill":
                            executed_action = "create_validate_package_skill_draft"
                            result_payload = self._execute_create_skill(
                                plan,
                                session_id=session_id,
                                session_factory=session_factory,
                                user=user,
                            )
                        elif plan.intent.name == "create_workflow":
                            executed_action = "create_validate_compile_workflow_draft"
                            result_payload = self._execute_create_workflow(
                                plan,
                                session_id=session_id,
                                session_factory=session_factory,
                                user=user,
                            )
                        elif plan.intent.name == "create_mcp_server":
                            executed_action = "create_validate_test_mcp_server_draft"
                            result_payload = self._execute_create_mcp_server(
                                plan,
                                session_id=session_id,
                                session_factory=session_factory,
                                user=user,
                            )
                        elif plan.intent.name == "create_prompt":
                            executed_action = "register_prompt"
                            result_payload = self._execute_prompt_write(
                                plan,
                                is_edit=False,
                                user=user,
                            )
                        elif plan.intent.name == "edit_prompt":
                            executed_action = "register_prompt_version"
                            result_payload = self._execute_prompt_write(
                                plan,
                                is_edit=True,
                                user=user,
                            )
                        elif plan.intent.name == "run_prompt_optimization":
                            executed_action = "enqueue_prompt_optimization"
                            result_payload = self._execute_prompt_optimization(
                                plan,
                                session_factory=session_factory,
                                user=user,
                            )
                        elif plan.intent.name == "generate_test_cases":
                            executed_action = "generate_test_cases"
                            result_payload = self._execute_generate_test_cases(plan)
                        elif plan.intent.name == "save_eval_dataset":
                            executed_action = "save_eval_dataset"
                            result_payload = self._execute_save_eval_dataset(
                                plan,
                                session_factory=session_factory,
                                user=user,
                            )
                        elif plan.intent.name == "review_optimization_result":
                            executed_action = "review_optimization_result"
                            result_payload = self._execute_review_optimization_result(
                                plan,
                                session_factory=session_factory,
                            )
                        elif plan.intent.name == "run_workflow_calibration":
                            executed_action = "enqueue_workflow_calibration"
                            result_payload = self._execute_workflow_calibration(
                                plan,
                                session_factory=session_factory,
                                user=user,
                            )
                        elif plan.intent.name == "review_workflow_calibration_result":
                            executed_action = "review_workflow_calibration_result"
                            result_payload = self._execute_review_workflow_calibration_result(
                                plan,
                                session_factory=session_factory,
                            )
                        elif plan.intent.name == "propose_promotion":
                            executed_action = "propose_promotion"
                            result_payload = self._execute_propose_promotion(
                                plan,
                                session_id=session_id,
                                operation_id=operation_id,
                                trace_id=trace_id,
                                correlation_id=correlation_id,
                                session_factory=session_factory,
                                user=user,
                            )
                        else:
                            raise ValueError(
                                f"No execution adapter implemented for intent {plan.intent.name!r}",
                            )
                        adapter_span.set_attribute(
                            "caliber.assistant.result_type",
                            result_payload.get("result_type", ""),
                        )
                        adapter_span.set_attribute(
                            "caliber.assistant.result_status",
                            result_payload.get("status", run_status),
                        )
        except Exception as exc:
            run_status = "failed"
            error_text = str(exc)
            result_payload = self._result_envelope(
                result_type="error",
                status="failed",
                summary="Assistant operation failed.",
                warnings=[error_text],
                extra={"error": error_text},
            )

        mlflow_trace_id = self._span_value(
            adapter_span,
            execute_span,
            field_name="mlflow_trace_id",
        )
        mlflow_run_id = self._span_value(
            adapter_span,
            execute_span,
            field_name="mlflow_run_id",
        )
        result_payload = self._attach_operation_observability(
            result_payload,
            trace_id=trace_id,
            correlation_id=correlation_id,
            mlflow_trace_id=mlflow_trace_id,
            mlflow_run_id=mlflow_run_id,
        )

        with session_factory() as db:
            session_row = self._get_owned_session(db, session_id, user)
            if session_row is None:
                raise ValueError(f"Session {session_id} not found")

            run_row = db.get(CaliberAssistantRun, operation_id)
            if run_row is None:
                raise ValueError(f"Run {operation_id} not found")

            run_row.status = run_status
            run_row.output_summary = f"{plan.intent.name}: {run_status}"[:500]
            run_row.error = error_text[:2000] if error_text else None
            run_row.trace_id = trace_id
            run_row.mlflow_run_id = mlflow_run_id
            run_row.completed_at = datetime.now(timezone.utc)

            metadata, workbench = self._metadata_and_workbench(session_row.metadata_)
            self._ensure_correlation_id(metadata)
            operations = workbench.get("operations")
            if not isinstance(operations, dict):
                operations = {}
            operation = operations.get(operation_id)
            if not isinstance(operation, dict):
                operation = {"operation_id": operation_id, "created_at": _operation_timestamp()}
            operation.update(
                {
                    "plan_id": plan.plan_id,
                    "intent_name": plan.intent.name,
                    "status": run_status,
                    "executed_action": executed_action,
                    "result": result_payload,
                    "updated_at": _operation_timestamp(),
                    "trace_id": trace_id,
                    "correlation_id": correlation_id,
                }
            )
            operations[operation_id] = operation
            workbench["operations"] = self._trim_workbench_records(
                operations,
                max_records=_MAX_OPERATIONS_PER_SESSION,
                preserve_key=operation_id,
            )
            workbench["latest_operation_id"] = operation_id
            metadata["intent_workbench"] = workbench
            session_row.metadata_ = metadata

            db.commit()
            db.refresh(run_row)

            return IntentExecuteResponse(
                operation_id=operation_id,
                plan_id=plan.plan_id,
                intent_name=plan.intent.name,
                status=run_status,
                executed_action=executed_action,
                result=result_payload,
                run=RunResponse.model_validate(run_row),
            )

    def get_operation_status(
        self,
        session_id: str,
        operation_id: str,
        *,
        session_factory: Any,
        user: str | None = None,
    ) -> OperationStatusResponse | None:
        with session_factory() as db:
            session_row = self._get_owned_session(db, session_id, user)
            if session_row is None:
                return None

            _metadata, workbench = self._metadata_and_workbench(session_row.metadata_)
            operations = workbench.get("operations")
            operation = operations.get(operation_id) if isinstance(operations, dict) else None
            run_row = db.get(CaliberAssistantRun, operation_id)

            if operation is None and run_row is None:
                return None

            op_data = operation if isinstance(operation, dict) else {}
            created_raw = op_data.get("created_at")
            created_at = run_row.started_at if run_row is not None else datetime.now(timezone.utc)
            if isinstance(created_raw, str):
                with contextlib.suppress(ValueError):
                    created_at = datetime.fromisoformat(created_raw)

            updated_raw = op_data.get("updated_at")
            updated_at: datetime | None = None
            if isinstance(updated_raw, str):
                try:
                    updated_at = datetime.fromisoformat(updated_raw)
                except ValueError:
                    updated_at = None
            elif run_row is not None:
                updated_at = run_row.completed_at

            return OperationStatusResponse(
                operation_id=operation_id,
                session_id=session_id,
                plan_id=op_data.get("plan_id") if isinstance(op_data.get("plan_id"), str) else None,
                intent_name=str(
                    op_data.get("intent_name") or (run_row.model if run_row else "unknown")
                ),
                status=str(op_data.get("status") or (run_row.status if run_row else "unknown")),
                created_at=created_at,
                updated_at=updated_at,
                result=_op_result if isinstance(_op_result := op_data.get("result"), dict) else {},
                run=RunResponse.model_validate(run_row) if run_row else None,
            )

    def _metadata_and_workbench(self, metadata_raw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        metadata = copy.deepcopy(metadata_raw) if isinstance(metadata_raw, dict) else {}
        workbench = metadata.get("intent_workbench")
        if not isinstance(workbench, dict):
            workbench = {}
        if not isinstance(workbench.get("plans"), dict):
            workbench["plans"] = {}
        if not isinstance(workbench.get("operations"), dict):
            workbench["operations"] = {}
        metadata["intent_workbench"] = workbench
        return metadata, workbench

    def _trim_workbench_records(
        self,
        records: dict[str, Any],
        *,
        max_records: int,
        preserve_key: str | None = None,
    ) -> dict[str, Any]:
        if len(records) <= max_records:
            return records
        trimmed = dict(records)
        for key in list(trimmed):
            if len(trimmed) <= max_records:
                break
            if preserve_key is not None and key == preserve_key:
                continue
            trimmed.pop(key, None)
        return trimmed

    def _latest_generated_test_cases(  # noqa: PLR0912
        self, session_metadata: dict[str, Any]
    ) -> dict[str, Any] | None:
        workbench = session_metadata.get("intent_workbench")
        if not isinstance(workbench, dict):
            return None
        operations = workbench.get("operations")
        if not isinstance(operations, dict):
            return None

        operation_ids = list(reversed(operations.keys()))
        latest_operation_id = workbench.get("latest_operation_id")
        if isinstance(latest_operation_id, str) and latest_operation_id in operations:
            operation_ids = [
                latest_operation_id,
                *[key for key in operation_ids if key != latest_operation_id],
            ]

        for operation_id in operation_ids:
            operation = operations.get(operation_id)
            if not isinstance(operation, dict):
                continue
            if operation.get("intent_name") != "generate_test_cases":
                continue
            result = operation.get("result")
            if not isinstance(result, dict) or result.get("result_type") != "test_cases":
                continue
            examples = result.get("examples")
            if not isinstance(examples, list) or not examples:
                continue
            dataset_name = None
            next_actions = result.get("next_actions")
            if isinstance(next_actions, list):
                for action in next_actions:
                    if not isinstance(action, dict):
                        continue
                    if action.get("intent_name") != "save_eval_dataset":
                        continue
                    slot_overrides = action.get("slot_overrides")
                    if isinstance(slot_overrides, dict) and isinstance(
                        slot_overrides.get("dataset_name"),
                        str,
                    ):
                        dataset_name = slot_overrides["dataset_name"].strip()
                        break
            return {"examples": examples, "dataset_name": dataset_name}
        return None

    def _resolve_intent_content(
        self,
        *,
        content: str,
        context: dict[str, Any],
        session_metadata: dict[str, Any],
    ) -> IntentResolveResponse:
        candidates = self._classify_intent(content)
        selected = candidates[0]
        slots, assumptions, evidence = self._extract_slots_for_intent(
            intent_name=selected.name,
            content=content,
            context=context,
            session_metadata=session_metadata,
        )

        required = _INTENT_REQUIRED_SLOTS.get(selected.name, ())
        values = {slot.name: slot.value for slot in slots}
        missing = [slot_name for slot_name in required if not _non_empty(values.get(slot_name))]
        questions = [self._question_for_slot(slot_name) for slot_name in missing[:3]]

        return IntentResolveResponse(
            intent=selected,
            alternatives=candidates[1:3],
            slots=slots,
            assumptions=assumptions,
            questions=questions,
            evidence=evidence,
        )

    def _classify_intent(self, content: str) -> list[IntentCandidate]:  # noqa: PLR0912, PLR0915
        lower = content.lower()
        scores: dict[str, float] = dict.fromkeys(_INTENT_REQUIRED_SLOTS, 0.0)

        calibration_terms = ("optimiz", "tune", "improve", "calibrat")
        workflow_calibration = "workflow" in lower and any(
            term in lower for term in calibration_terms
        )
        if workflow_calibration:
            scores["run_workflow_calibration"] += 0.9
        if (
            "review" in lower
            and "workflow" in lower
            and any(term in lower for term in ("calibration", "calibrator", "candidate", "winner"))
        ):
            scores["review_workflow_calibration_result"] += 0.88
        if re.search(r"\bWF-[A-Za-z0-9_-]+\b", content) and workflow_calibration:
            scores["run_workflow_calibration"] += 0.05
        if re.search(r"\bRFN-[A-Za-z0-9_-]+\b", content) and "workflow" in lower:
            scores["review_workflow_calibration_result"] += 0.05

        caliber_as_verb = "caliber" in lower and any(
            term in lower for term in ("prompt", "run", "dataset", "scorer", "gate")
        )
        if (
            any(term in lower for term in calibration_terms) and "workflow" not in lower
        ) or caliber_as_verb:
            scores["run_prompt_optimization"] += 0.72
        if "dataset" in lower or re.search(r"\bEDS?-[A-Za-z0-9_-]+\b", content):
            scores["run_prompt_optimization"] += 0.15
        if any(k in lower for k in ["faithfulness", "toxicity", "correctness", "scorer"]):
            scores["run_prompt_optimization"] += 0.1

        if "create" in lower and "prompt" in lower:
            scores["create_prompt"] += 0.8
        if "new prompt" in lower:
            scores["create_prompt"] += 0.1

        if any(k in lower for k in ["create", "build", "author", "generate"]) and "tool" in lower:
            scores["create_tool"] += 0.83
        if "tool source" in lower or "callable" in lower:
            scores["create_tool"] += 0.1

        if any(k in lower for k in ["create", "build", "author", "draft"]) and "skill" in lower:
            scores["create_skill"] += 0.83
        if "skill package" in lower or "skill instructions" in lower:
            scores["create_skill"] += 0.1

        if any(k in lower for k in ["create", "build", "author", "draft"]) and "workflow" in lower:
            scores["create_workflow"] += 0.83
        if "workflow manifest" in lower or ("compile" in lower and "workflow" in lower):
            scores["create_workflow"] += 0.1

        if "mcp" in lower and any(
            k in lower for k in ["create", "build", "register", "configure", "setup", "set up"]
        ):
            scores["create_mcp_server"] += 0.84
        if "mcp server" in lower or "model context protocol" in lower:
            scores["create_mcp_server"] += 0.1

        if (
            any(k in lower for k in ["edit", "update", "revise", "rewrite", "stricter"])
            and "prompt" in lower
        ):
            scores["edit_prompt"] += 0.82

        if "test case" in lower or "test cases" in lower:
            scores["generate_test_cases"] += 0.8

        if "save" in lower and "dataset" in lower:
            scores["save_eval_dataset"] += 0.82

        if "review" in lower and any(
            term in lower for term in ("optimization", "calibration", "calibrator")
        ):
            scores["review_optimization_result"] += 0.8

        if "promote" in lower or "alias" in lower or "prod" in lower:
            scores["propose_promotion"] += 0.75

        if max(scores.values(), default=0.0) <= 0.0:
            scores["create_prompt"] = 0.34
            scores["edit_prompt"] = 0.33
            scores["run_prompt_optimization"] = 0.32

        ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        candidates: list[IntentCandidate] = []
        for intent_name, score in ordered[:3]:
            bounded = max(0.0, min(0.99, score))
            if bounded <= 0.0:
                continue
            candidates.append(
                IntentCandidate(
                    name=intent_name,
                    confidence=bounded,
                    rationale=_SCORE_HINTS.get(intent_name, "Intent inferred from user text."),
                )
            )

        if not candidates:
            candidates = [
                IntentCandidate(
                    name="create_prompt",
                    confidence=0.3,
                    rationale="No clear intent detected; defaulting to create_prompt.",
                )
            ]
        return candidates

    def _extract_slots_for_intent(  # noqa: PLR0912, PLR0915
        self,
        *,
        intent_name: str,
        content: str,
        context: dict[str, Any],
        session_metadata: dict[str, Any],
    ) -> tuple[list[IntentSlot], list[str], list[str]]:
        slots: list[IntentSlot] = []
        assumptions: list[str] = []
        evidence: list[str] = []

        def _add_slot(
            name: str,
            value: Any,
            *,
            required: bool,
            source: str,
            confidence: float,
            needs_confirmation: bool = False,
        ) -> None:
            if not _non_empty(value):
                return
            slots.append(
                IntentSlot(
                    name=name,
                    value=value,
                    required=required,
                    source=cast(Literal["user", "inferred", "default", "memory", "system"], source),
                    confidence=confidence,
                    needs_confirmation=needs_confirmation,
                )
            )

        prompt_name: str | None = None
        prompt_source = "memory"
        prompt_match = re.search(
            r"(?:prompt|agent)\s+(?:named\s+)?[\"'`]?([a-zA-Z0-9_-]{3,64})",
            content,
            flags=re.IGNORECASE,
        )
        if prompt_match:
            prompt_name = prompt_match.group(1)
            prompt_source = "user"
            evidence.append("Found prompt/agent name in request text")
        elif isinstance(context.get("prompt_name"), str):
            prompt_name = str(context["prompt_name"])
            prompt_source = "user"
            assumptions.append("Used prompt_name provided in request context")
        elif isinstance(context.get("agent_id"), str):
            prompt_name = str(context["agent_id"])
            prompt_source = "user"
            assumptions.append("Used agent_id from request context as prompt_name")
        elif isinstance(session_metadata.get("prompt_name"), str):
            prompt_name = str(session_metadata["prompt_name"])
            prompt_source = "memory"
            assumptions.append("Inferred prompt_name from session metadata")
        elif isinstance(session_metadata.get("prompt_ref"), str):
            from_ref = _parse_prompt_ref_name(str(session_metadata["prompt_ref"]))
            if from_ref:
                prompt_name = from_ref
                prompt_source = "memory"
                assumptions.append("Inferred prompt_name from session prompt_ref")

        if intent_name == "create_tool":
            tool_name = None
            if isinstance(context.get("tool_name"), str):
                tool_name = str(context["tool_name"]).strip()
                assumptions.append("Used tool_name from request context")
            elif isinstance(context.get("name"), str):
                tool_name = str(context["name"]).strip()
                assumptions.append("Used name from request context as tool_name")
            else:
                tool_match = re.search(
                    r"(?:tool)\s+(?:named|called)?\s*[\"'`]?([a-zA-Z_][a-zA-Z0-9_]{1,63})",
                    content,
                    flags=re.IGNORECASE,
                )
                if tool_match:
                    tool_name = tool_match.group(1)
                    evidence.append("Found tool name in request text")

            source = context.get("source")
            if not isinstance(source, str):
                source = context.get("tool_source")
            if isinstance(source, str) and source.strip():
                assumptions.append("Used tool source from request context")
                source = source.strip()
            else:
                source_match = re.search(
                    r"source\s*:\s*(.+)", content, flags=re.IGNORECASE | re.DOTALL
                )
                source = source_match.group(1).strip() if source_match else None
                if source:
                    evidence.append("Found tool source in request text")

            callable_name = context.get("callable_name")
            if not isinstance(callable_name, str):
                callable_name = context.get("function_name")
            if not isinstance(callable_name, str):
                callable_name = None
            if not callable_name and isinstance(tool_name, str):
                callable_name = tool_name
                assumptions.append("Defaulted callable_name to tool_name")

            tests = context.get("tests")
            if not isinstance(tests, list):
                tests = context.get("tool_tests")
            if isinstance(tests, list):
                assumptions.append("Used sandbox tests from request context")
            else:
                tests = None

            _add_slot("tool_name", tool_name, required=True, source="user", confidence=0.9)
            _add_slot("source", source, required=True, source="user", confidence=0.9)
            _add_slot("callable_name", callable_name, required=True, source="user", confidence=0.86)
            _add_slot("tests", tests, required=True, source="user", confidence=0.9)

            optional_tool_slots = (
                "description",
                "input_schema",
                "output_schema",
                "side_effect_level",
                "requires_approval",
                "allow_in_preview",
                "secret_refs",
                "module_path",
                "version",
            )
            for optional_name in optional_tool_slots:
                if optional_name in context:
                    _add_slot(
                        optional_name,
                        context.get(optional_name),
                        required=False,
                        source="user",
                        confidence=0.9,
                    )

        if intent_name == "create_skill":
            skill_name = None
            if isinstance(context.get("skill_name"), str):
                skill_name = str(context["skill_name"]).strip()
                assumptions.append("Used skill_name from request context")
            elif isinstance(context.get("name"), str):
                skill_name = str(context["name"]).strip()
                assumptions.append("Used name from request context as skill_name")
            else:
                skill_match = re.search(
                    r"(?:skill)\s+(?:named|called)?\s*[\"'`]?([a-z0-9][a-z0-9-]{1,126})",
                    content,
                    flags=re.IGNORECASE,
                )
                if skill_match:
                    skill_name = skill_match.group(1).lower()
                    evidence.append("Found skill name in request text")

            description = context.get("description")
            if isinstance(description, str) and description.strip():
                description = description.strip()
                assumptions.append("Used skill description from request context")
            else:
                description_match = re.search(
                    r"description\s*:\s*(.+?)(?:\n\s*(?:content|instructions)\s*:|$)",
                    content,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                description = description_match.group(1).strip() if description_match else None
                if description:
                    evidence.append("Found skill description in request text")

            skill_content = context.get("content")
            if not isinstance(skill_content, str):
                skill_content = context.get("skill_content")
            if isinstance(skill_content, str) and skill_content.strip():
                skill_content = skill_content.strip()
                assumptions.append("Used skill content from request context")
            else:
                content_match = re.search(
                    r"(?:content|instructions)\s*:\s*(.+)",
                    content,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                skill_content = content_match.group(1).strip() if content_match else None
                if skill_content:
                    evidence.append("Found skill content in request text")

            _add_slot("skill_name", skill_name, required=True, source="user", confidence=0.9)
            _add_slot("description", description, required=True, source="user", confidence=0.9)
            _add_slot("content", skill_content, required=True, source="user", confidence=0.9)

            optional_skill_slots = (
                "summary",
                "category",
                "tags",
                "skill_metadata",
                "allowed_tools",
                "depends_on",
            )
            for optional_name in optional_skill_slots:
                if optional_name in context:
                    _add_slot(
                        optional_name,
                        context.get(optional_name),
                        required=False,
                        source="user",
                        confidence=0.9,
                    )

        if intent_name == "create_workflow":
            workflow_name = None
            if isinstance(context.get("workflow_name"), str):
                workflow_name = str(context["workflow_name"]).strip()
                assumptions.append("Used workflow_name from request context")
            elif isinstance(context.get("name"), str):
                workflow_name = str(context["name"]).strip()
                assumptions.append("Used name from request context as workflow_name")
            else:
                workflow_match = re.search(
                    r"(?:workflow)\s+(?:named|called)?\s*[\"'`]?([a-zA-Z0-9 _-]{3,128})",
                    content,
                    flags=re.IGNORECASE,
                )
                if workflow_match:
                    workflow_name = workflow_match.group(1).strip()
                    evidence.append("Found workflow name in request text")

            manifest = context.get("manifest")
            if isinstance(manifest, dict):
                assumptions.append("Used workflow manifest from request context")
            else:
                manifest = None

            _add_slot("workflow_name", workflow_name, required=True, source="user", confidence=0.9)
            _add_slot("manifest", manifest, required=True, source="user", confidence=0.9)
            for optional_name in ("description", "default_experiment_id"):
                if optional_name in context:
                    _add_slot(
                        optional_name,
                        context.get(optional_name),
                        required=False,
                        source="user",
                        confidence=0.9,
                    )

        if intent_name == "create_mcp_server":
            server_name = None
            if isinstance(context.get("server_name"), str):
                server_name = str(context["server_name"]).strip()
                assumptions.append("Used server_name from request context")
            elif isinstance(context.get("name"), str):
                server_name = str(context["name"]).strip()
                assumptions.append("Used name from request context as server_name")
            else:
                server_match = re.search(
                    r"(?:mcp\s+server|server)\s+(?:named|called)?\s*[\"'`]?([a-zA-Z0-9 _-]{3,128})",
                    content,
                    flags=re.IGNORECASE,
                )
                if server_match:
                    server_name = server_match.group(1).strip()
                    evidence.append("Found MCP server name in request text")

            transport = context.get("transport")
            if isinstance(transport, str):
                transport = transport.strip()
                assumptions.append("Used MCP transport from request context")
            elif "streamable-http" in content.lower():
                transport = "streamable-http"
                evidence.append("Found MCP transport in request text")
            elif "sse" in content.lower():
                transport = "sse"
                evidence.append("Found MCP transport in request text")
            elif "stdio" in content.lower():
                transport = "stdio"
                evidence.append("Found MCP transport in request text")
            else:
                transport = None

            _add_slot("server_name", server_name, required=True, source="user", confidence=0.9)
            _add_slot("transport", transport, required=True, source="user", confidence=0.9)
            optional_mcp_slots = (
                "description",
                "uri",
                "command",
                "args",
                "env",
                "headers",
                "auth_type",
                "auth_config",
                "icon",
                "discovered_tools",
            )
            for optional_name in optional_mcp_slots:
                if optional_name in context:
                    _add_slot(
                        optional_name,
                        context.get(optional_name),
                        required=False,
                        source="user",
                        confidence=0.9,
                    )

        if intent_name in {
            "create_prompt",
            "edit_prompt",
            "generate_test_cases",
            "propose_promotion",
        }:
            _add_slot(
                "prompt_name",
                prompt_name,
                required="prompt_name" in _INTENT_REQUIRED_SLOTS.get(intent_name, ()),
                source=prompt_source,
                confidence=0.9 if prompt_source == "user" else 0.75,
                needs_confirmation=prompt_source != "user",
            )

        if intent_name in {"create_prompt", "edit_prompt"}:
            template_value = None
            if isinstance(context.get("template"), str):
                template_value = str(context["template"]).strip()
                assumptions.append("Used template from request context")
            else:
                template_value = _extract_template_from_text(content)
                if template_value:
                    evidence.append("Found template content in request text")

            _add_slot(
                "template",
                template_value,
                required=True,
                source="user" if template_value else "inferred",
                confidence=0.92 if template_value else 0.0,
            )

            commit_msg_match = re.search(
                r"commit\s+message\s*:\s*(.+)", content, flags=re.IGNORECASE
            )
            commit_message = str(context.get("commit_message") or "").strip()
            if not commit_message and commit_msg_match:
                commit_message = commit_msg_match.group(1).strip()
            _add_slot(
                "commit_message",
                commit_message,
                required=False,
                source="user" if commit_message else "default",
                confidence=0.9 if commit_message else 0.0,
            )

        if intent_name == "run_workflow_calibration":
            workflow_id = None
            if isinstance(context.get("workflow_id"), str):
                workflow_id = str(context["workflow_id"]).strip()
                assumptions.append("Used workflow_id from request context")
            else:
                workflow_match = re.search(r"\bWF-[A-Za-z0-9_-]+\b", content)
                if workflow_match:
                    workflow_id = workflow_match.group(0)
                    evidence.append("Found workflow id in request text")

            agent_id = context.get("agent_id")
            if isinstance(agent_id, str) and agent_id.strip():
                agent_id = agent_id.strip()
                assumptions.append("Used agent_id from request context")
            else:
                agent_id = None

            objective = context.get("objective")
            if isinstance(objective, dict):
                objective_value = objective.get("maximize")
            else:
                objective_value = objective
            if not isinstance(objective_value, str):
                objective_value = "tool_adherence" if "tool" in content.lower() else "quality"
                assumptions.append(f"Defaulted workflow calibration objective to {objective_value}")
            objective_value = objective_value.strip()

            epsilon = context.get("epsilon")
            if epsilon is None and isinstance(context.get("objective"), dict):
                epsilon = context["objective"].get("epsilon")
            if epsilon is None:
                epsilon_match = re.search(
                    r"epsilon\s*[:=]?\s*(0(?:\.\d+)?|1(?:\.0+)?)", content, flags=re.IGNORECASE
                )
                epsilon = float(epsilon_match.group(1)) if epsilon_match else None

            max_candidates = context.get("max_candidates")
            if max_candidates is None:
                budget = context.get("budget")
                if isinstance(budget, dict):
                    max_candidates = budget.get("max_candidates")
            if max_candidates is None:
                max_match = re.search(
                    r"(?:max\s*)?candidates?\s*[:=]?\s*([1-5])", content, flags=re.IGNORECASE
                )
                max_candidates = int(max_match.group(1)) if max_match else None

            _add_slot("workflow_id", workflow_id, required=True, source="user", confidence=0.9)
            _add_slot("agent_id", agent_id, required=False, source="user", confidence=0.85)
            _add_slot(
                "objective", objective_value, required=False, source="default", confidence=0.75
            )
            _add_slot("epsilon", epsilon, required=False, source="user", confidence=0.85)
            _add_slot(
                "max_candidates", max_candidates, required=False, source="user", confidence=0.85
            )

        if intent_name == "run_prompt_optimization":
            agent_id = None
            if isinstance(context.get("agent_id"), str):
                agent_id = str(context["agent_id"])
                assumptions.append("Used agent_id from request context")
            elif prompt_name:
                agent_id = prompt_name
                assumptions.append("Used inferred prompt_name as agent_id")

            dataset_id = None
            if isinstance(context.get("eval_dataset_id"), str):
                dataset_id = str(context["eval_dataset_id"])
                assumptions.append("Used eval_dataset_id from request context")
            else:
                dataset_match = re.search(r"\bEDS?-[A-Za-z0-9_-]+\b", content)
                if dataset_match:
                    dataset_id = dataset_match.group(0)
                    evidence.append("Found eval dataset id in request text")

            optimizer_type = None
            if isinstance(context.get("optimizer_type"), str):
                optimizer_type = str(context["optimizer_type"])
            elif "gepa" in content.lower():
                optimizer_type = "GEPA"
            elif "meta prompt" in content.lower() or "metaprompt" in content.lower():
                optimizer_type = "MetaPrompt"

            scorer_map = {
                "correctness": "Correctness",
                "guidelines": "Guidelines",
                "relevance": "RelevanceToQuery",
                "safety": "Safety",
                "faithfulness": "DeepEval.Faithfulness",
                "toxicity": "DeepEval.Toxicity",
                "answer relevancy": "DeepEval.AnswerRelevancy",
                "tool use": "DeepEval.ToolUse",
            }
            scorers: list[dict[str, Any]] = []
            if isinstance(context.get("scorers"), list):
                raw_scorers = context["scorers"]
                for scorer in raw_scorers:
                    if isinstance(scorer, str):
                        scorers.append({"name": scorer, "weight": 1.0, "config": {}})
                    elif isinstance(scorer, dict) and isinstance(scorer.get("name"), str):
                        scorers.append(
                            {
                                "name": str(scorer.get("name")),
                                "weight": float(scorer.get("weight", 1.0)),
                                "config": dict(scorer.get("config") or {}),
                            }
                        )
            else:
                lowered = content.lower()
                for keyword, scorer_name in scorer_map.items():
                    if keyword in lowered:
                        scorers.append({"name": scorer_name, "weight": 1.0, "config": {}})

            _add_slot(
                "agent_id",
                agent_id,
                required=True,
                source="inferred",
                confidence=0.78,
                needs_confirmation=True,
            )
            _add_slot("eval_dataset_id", dataset_id, required=True, source="user", confidence=0.9)
            _add_slot(
                "optimizer_type",
                optimizer_type,
                required=True,
                source="user" if optimizer_type else "default",
                confidence=0.88 if optimizer_type else 0.0,
            )
            _add_slot(
                "scorers",
                scorers,
                required=True,
                source="user",
                confidence=0.82,
                needs_confirmation=True,
            )

            gate_obj = context.get("gate")
            if isinstance(gate_obj, dict):
                _add_slot(
                    "gate.min_aggregate_score",
                    gate_obj.get("min_aggregate_score"),
                    required=True,
                    source="user",
                    confidence=0.9,
                )
                _add_slot(
                    "gate.max_regression_delta",
                    gate_obj.get("max_regression_delta"),
                    required=True,
                    source="user",
                    confidence=0.9,
                )

        if intent_name == "save_eval_dataset":
            dataset_name = None
            if isinstance(context.get("dataset_name"), str):
                dataset_name = str(context["dataset_name"]).strip()
                assumptions.append("Used dataset_name from request context")
            else:
                dataset_match = re.search(
                    r"(?:dataset\s+(?:named|called)|save\s+(?:it\s+)?as)\s+[\"'`]?([a-zA-Z0-9_-]{3,128})",
                    content,
                    flags=re.IGNORECASE,
                )
                if dataset_match:
                    dataset_name = dataset_match.group(1)
                    evidence.append("Found dataset name in request text")

            examples = context.get("examples")
            if not isinstance(examples, list):
                examples = None
            if examples is not None:
                assumptions.append("Used examples from request context")

            _add_slot("dataset_name", dataset_name, required=True, source="user", confidence=0.9)
            _add_slot("examples", examples, required=True, source="user", confidence=0.9)

        if intent_name == "review_optimization_result":
            job_id = None
            if isinstance(context.get("job_id"), str):
                job_id = str(context["job_id"]).strip()
                assumptions.append("Used job_id from request context")
            else:
                job_match = re.search(r"\bRFN-[A-Za-z0-9_-]+\b", content)
                if job_match:
                    job_id = job_match.group(0)
                    evidence.append("Found refinement job id in request text")
            _add_slot("job_id", job_id, required=True, source="user", confidence=0.9)

        if intent_name == "review_workflow_calibration_result":
            job_id = None
            if isinstance(context.get("job_id"), str):
                job_id = str(context["job_id"]).strip()
                assumptions.append("Used job_id from request context")
            else:
                job_match = re.search(r"\bRFN-[A-Za-z0-9_-]+\b", content)
                if job_match:
                    job_id = job_match.group(0)
                    evidence.append("Found refinement job id in request text")
            _add_slot("job_id", job_id, required=True, source="user", confidence=0.9)

        if intent_name == "propose_promotion":
            target_alias = None
            if isinstance(context.get("target_alias"), str):
                target_alias = str(context["target_alias"]).strip()
                assumptions.append("Used target_alias from request context")
            elif "prod" in content.lower() or "production" in content.lower():
                target_alias = "prod"
                assumptions.append("Defaulted target_alias to prod from promotion wording")

            source_version = context.get("source_version")
            if source_version is None:
                version_match = re.search(r"(?:version|v)\s*(\d+)", content, flags=re.IGNORECASE)
                if version_match:
                    source_version = int(version_match.group(1))
                    evidence.append("Found prompt source version in request text")
            _add_slot("target_alias", target_alias, required=True, source="user", confidence=0.88)
            _add_slot(
                "source_version", source_version, required=False, source="user", confidence=0.85
            )

        return slots, assumptions, evidence

    def _build_plan_from_resolved(  # noqa: PLR0912
        self,
        *,
        resolved: IntentResolveResponse,
        slot_overrides: dict[str, Any],
        session_metadata: dict[str, Any],
    ) -> IntentPlanResponse:
        intent_name = resolved.intent.name
        required = _INTENT_REQUIRED_SLOTS.get(intent_name, ())

        slot_map: dict[str, IntentSlot] = {slot.name: slot for slot in resolved.slots}

        for key, value in self._flatten_slot_overrides(slot_overrides).items():
            slot_map[key] = IntentSlot(
                name=key,
                value=value,
                required=key in required,
                source="user",
                confidence=1.0,
                needs_confirmation=False,
            )

        assumptions = list(resolved.assumptions)
        if intent_name == "run_prompt_optimization":
            from caliber.routes import prompts as prompt_routes  # noqa: PLC0415

            if "optimizer_type" not in slot_map:
                slot_map["optimizer_type"] = IntentSlot(
                    name="optimizer_type",
                    value="MetaPrompt",
                    required=True,
                    source="default",
                    confidence=0.95,
                    needs_confirmation=False,
                )
                assumptions.append("Defaulted optimizer_type to MetaPrompt")

            if "scorers" not in slot_map:
                _scorer_options, _scorer_index, default_scorers, _runtime = (
                    prompt_routes._build_scorer_capabilities()
                )
                normalized = [
                    {"name": name, "weight": 1.0, "config": {}} for name in default_scorers
                ]
                slot_map["scorers"] = IntentSlot(
                    name="scorers",
                    value=normalized,
                    required=True,
                    source="default",
                    confidence=0.9,
                    needs_confirmation=True,
                )
                assumptions.append("Defaulted scorers to currently available recommended scorers")

            if "gate.min_aggregate_score" not in slot_map:
                slot_map["gate.min_aggregate_score"] = IntentSlot(
                    name="gate.min_aggregate_score",
                    value=DEFAULT_MIN_AGGREGATE_SCORE,
                    required=True,
                    source="default",
                    confidence=0.95,
                )
            if "gate.max_regression_delta" not in slot_map:
                slot_map["gate.max_regression_delta"] = IntentSlot(
                    name="gate.max_regression_delta",
                    value=DEFAULT_MAX_REGRESSION_DELTA,
                    required=True,
                    source="default",
                    confidence=0.95,
                )

        if intent_name == "save_eval_dataset":
            generated = self._latest_generated_test_cases(session_metadata)
            if generated is not None:
                if "examples" not in slot_map:
                    slot_map["examples"] = IntentSlot(
                        name="examples",
                        value=generated["examples"],
                        required=True,
                        source="memory",
                        confidence=0.86,
                        needs_confirmation=True,
                    )
                    assumptions.append(
                        "Used examples from the latest generated test-case operation"
                    )
                if "dataset_name" not in slot_map and isinstance(
                    generated.get("dataset_name"), str
                ):
                    slot_map["dataset_name"] = IntentSlot(
                        name="dataset_name",
                        value=generated["dataset_name"],
                        required=True,
                        source="memory",
                        confidence=0.82,
                        needs_confirmation=True,
                    )
                    assumptions.append(
                        "Used dataset_name from the latest generated test-case operation"
                    )

        ordered_slots: list[IntentSlot] = []
        for slot_name in required:
            slot = slot_map.get(slot_name)
            if slot is None:
                ordered_slots.append(IntentSlot(name=slot_name, required=True, confidence=0.0))
            else:
                slot.required = True
                ordered_slots.append(slot)

        for slot_name, slot in slot_map.items():
            if slot_name in required:
                continue
            ordered_slots.append(slot)

        missing_slots = [
            slot_name
            for slot_name in required
            if not ((slot := slot_map.get(slot_name)) is not None and _non_empty(slot.value))
        ]
        questions = [self._question_for_slot(slot_name) for slot_name in missing_slots[:3]]
        ready = len(missing_slots) == 0

        action_template = _INTENT_ACTIONS.get(intent_name, [])
        actions = [
            PlanAction(
                action=action.action,
                description=action.description,
                status="ready" if ready else "blocked",
                mutation_type=action.mutation_type,
                result_type=action.result_type,
                required_scopes=list(action.required_scopes),
            )
            for action in action_template
        ]
        requires_confirmation = any(action.mutation_type != "none" for action in actions)

        return IntentPlanResponse(
            plan_id=_new_plan_id(),
            intent=resolved.intent,
            actions=actions,
            slots=ordered_slots,
            missing_slots=missing_slots,
            assumptions=assumptions,
            questions=questions,
            ready=ready,
            requires_confirmation=requires_confirmation,
        )

    def _flatten_slot_overrides(self, slot_overrides: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in slot_overrides.items():
            if key == "gate" and isinstance(value, dict):
                for gate_key, gate_value in value.items():
                    out[f"gate.{gate_key}"] = gate_value
            else:
                out[key] = value
        return out

    def _question_for_slot(self, slot_name: str) -> str:
        questions = {
            "prompt_name": "Which prompt should this apply to?",
            "template": "What prompt template should be used?",
            "agent_id": "Which agent should be calibrated?",
            "eval_dataset_id": "Which active eval dataset should be used?",
            "optimizer_type": "Which calibration strategy should run: MetaPrompt or GEPA?",
            "scorers": "Which scorers should be used for evaluation?",
            "gate.min_aggregate_score": "What minimum aggregate score gate should be enforced?",
            "gate.max_regression_delta": "What maximum regression delta should be allowed?",
            "dataset_name": "What should the evaluation dataset be named?",
            "examples": "Which structured examples should be saved to the dataset?",
            "job_id": "Which calibration job should be reviewed?",
            "workflow_id": "Which workflow should be calibrated?",
            "target_alias": "Which alias should the prompt version target?",
            "source_version": "Which prompt version should be promoted?",
            "tool_name": "What should the tool be named?",
            "source": "What Python source code should define the tool?",
            "callable_name": "Which callable should the sandbox run?",
            "tests": "Which structured sandbox test cases should verify the tool?",
            "skill_name": "What should the skill be named?",
            "description": "What should the skill description be?",
            "content": "What skill instructions should be packaged?",
            "workflow_name": "What should the workflow be named?",
            "manifest": "Which structured workflow manifest should be compiled?",
            "server_name": "What should the MCP server be named?",
            "transport": "Which MCP transport should be used?",
        }
        return questions.get(slot_name, f"Please provide {slot_name}.")

    def _slot_value(self, plan: IntentPlanResponse, slot_name: str) -> Any:
        for slot in plan.slots:
            if slot.name == slot_name:
                return slot.value
        return None

    def _execute_create_tool(  # noqa: PLR0912, PLR0915
        self,
        plan: IntentPlanResponse,
        *,
        session_id: str,
        session_factory: Any,
        user: str,
    ) -> dict[str, Any]:
        tool_name = self._slot_value(plan, "tool_name")
        source = self._slot_value(plan, "source")
        callable_name = self._slot_value(plan, "callable_name")
        raw_tests = self._slot_value(plan, "tests")

        if not isinstance(tool_name, str) or not tool_name.strip():
            raise ValueError("tool_name must be a non-empty string")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source must be a non-empty string")
        if not isinstance(callable_name, str) or not callable_name.strip():
            raise ValueError("callable_name must be a non-empty string")

        tests = self._coerce_tool_tests(raw_tests)
        draft_id = new_assistant_draft_id()
        clean_tool_name = tool_name.strip()
        clean_callable_name = callable_name.strip()
        side_effect_level = self._slot_value(plan, "side_effect_level")
        if not isinstance(side_effect_level, str) or not side_effect_level.strip():
            side_effect_level = "read"
        requires_approval = self._slot_value(plan, "requires_approval")
        if not isinstance(requires_approval, bool):
            requires_approval = side_effect_level != "read"
        allow_in_preview = self._slot_value(plan, "allow_in_preview")
        if not isinstance(allow_in_preview, bool):
            allow_in_preview = side_effect_level == "read"
        secret_refs = self._slot_value(plan, "secret_refs")
        if not isinstance(secret_refs, list):
            secret_refs = []

        description = self._slot_value(plan, "description")
        if not isinstance(description, str):
            description = ""
        input_schema = self._slot_value(plan, "input_schema")
        if not isinstance(input_schema, dict):
            input_schema = {}
        output_schema = self._slot_value(plan, "output_schema")
        if not isinstance(output_schema, dict):
            output_schema = {}
        module_path = self._slot_value(plan, "module_path")
        if not isinstance(module_path, str) or not module_path.strip():
            module_path = f"assistant.drafts.{draft_id.lower().replace('-', '_')}"
        version = self._slot_value(plan, "version")
        if not isinstance(version, str) or not version.strip():
            version = "1.0.0"

        artifact = {
            "name": clean_tool_name,
            "description": description.strip(),
            "source": source.strip(),
            "callable_name": clean_callable_name,
            "module_path": module_path.strip(),
            "input_schema": input_schema,
            "output_schema": output_schema,
            "side_effect_level": side_effect_level.strip(),
            "requires_approval": requires_approval,
            "allow_in_preview": allow_in_preview,
            "secret_refs": [
                str(ref) for ref in secret_refs if isinstance(ref, str) and ref.strip()
            ],
            "tests": [test.model_dump(mode="json") for test in tests],
            "version": version.strip(),
        }
        validation_report = validate_draft(
            "tool",
            artifact,
            max_source_bytes=self._settings.tool_source_max_bytes,
        )
        if validation_report.valid:
            test_report = self._run_tool_sandbox_tests(artifact)
        else:
            test_report = TestReport(
                passed=False,
                total=len(tests),
                failures=len(tests),
                details=[],
                error="Tool draft validation failed; sandbox tests were not run.",
            )

        passed = validation_report.valid and test_report.passed
        draft_status = (
            "tested"
            if passed
            else ("test_failed" if validation_report.valid else "validation_failed")
        )
        warnings = [
            *validation_report.warnings,
            *validation_report.errors,
        ]
        if test_report.error:
            warnings.append(test_report.error)
        for detail in test_report.details:
            if detail.get("status") == "failed" and isinstance(detail.get("error"), str):
                warnings.append(str(detail["error"]))

        with session_factory() as db:
            session_row = self._get_owned_session(db, session_id, user)
            if session_row is None:
                raise ValueError(f"Session {session_id} not found")

            draft = CaliberAssistantDraft(
                draft_id=draft_id,
                session_id=session_id,
                artifact_type="tool",
                status=draft_status,
                title=clean_tool_name,
                summary=f"Tool draft for {clean_callable_name}.",
                spec={
                    "intent_name": plan.intent.name,
                    "plan_id": plan.plan_id,
                    "callable_name": clean_callable_name,
                    "test_count": len(tests),
                },
                artifact=artifact,
                validation_report=validation_report.model_dump(mode="json"),
                test_report=test_report.model_dump(mode="json"),
                created_by=user,
                updated_by=user,
            )
            db.add(draft)
            session_row.active_draft_id = draft_id
            audit_record(
                db,
                actor=user,
                action="create_tool_draft",
                entity_type="assistant_draft",
                entity_id=draft_id,
                details={
                    "source": "caliber-assistant",
                    "plan_id": plan.plan_id,
                    "session_id": session_id,
                    "tool_name": clean_tool_name,
                    "callable_name": clean_callable_name,
                    "validation_valid": validation_report.valid,
                    "sandbox_passed": test_report.passed,
                    "draft_status": draft_status,
                },
            )
            db.commit()

        next_actions: list[dict[str, Any]] = []
        if passed:
            next_actions.extend(
                [
                    {
                        "intent_name": "approve_draft",
                        "label": "Approve tool draft",
                        "slot_overrides": {"draft_id": draft_id},
                        "requires_confirmation": True,
                    },
                    {
                        "intent_name": "publish_draft",
                        "label": "Publish after approval",
                        "slot_overrides": {"draft_id": draft_id},
                        "requires_confirmation": True,
                    },
                ]
            )

        return self._result_envelope(
            result_type="tool_draft",
            status="completed" if passed else "blocked",
            summary=(
                f"Created and tested tool draft {clean_tool_name}."
                if passed
                else f"Created tool draft {clean_tool_name}, but validation or tests failed."
            ),
            ids={
                "draft_id": draft_id,
                "tool_name": clean_tool_name,
                "callable_name": clean_callable_name,
            },
            links=[
                {
                    "label": "Tool draft",
                    "resource_type": "assistant_draft",
                    "id": draft_id,
                    "path": f"/assistant/drafts/{draft_id}",
                }
            ],
            warnings=warnings,
            next_actions=next_actions,
            extra={
                "draft": {
                    "draft_id": draft_id,
                    "artifact_type": "tool",
                    "status": draft_status,
                    "title": clean_tool_name,
                    "artifact": artifact,
                },
                "validation_report": validation_report.model_dump(mode="json"),
                "test_report": test_report.model_dump(mode="json"),
            },
        )

    def _coerce_tool_tests(self, raw_tests: Any) -> list[ToolSandboxTestCase]:
        if not isinstance(raw_tests, list) or len(raw_tests) == 0:
            raise ValueError("tests must include at least one structured sandbox test")
        tests: list[ToolSandboxTestCase] = []
        for raw_test in raw_tests:
            if not isinstance(raw_test, dict):
                raise ValueError("each sandbox test must be an object")
            tests.append(ToolSandboxTestCase.model_validate(raw_test))
        return tests

    def _tool_sandbox_timeout_seconds(self) -> float:
        return min(120.0, max(0.1, float(self._settings.run_timeout_seconds)))

    def _tool_sandbox_report(self, result: ToolSandboxTestSuiteResult) -> TestReport:
        details = [test.model_dump(mode="json") for test in result.tests]
        failures = sum(1 for test in result.tests if test.status != "passed")
        if (result.status == "timed_out" and failures == 0) or (
            result.status == "failed" and failures == 0
        ):
            failures = 1
        return TestReport(
            passed=result.status == "passed" and failures == 0,
            total=max(len(result.tests), failures, 1),
            failures=failures,
            details=details,
            error=result.error,
        )

    def _run_tool_sandbox_tests(self, artifact: dict[str, Any]) -> TestReport:
        tests = self._coerce_tool_tests(artifact.get("tests"))
        source = artifact.get("source")
        callable_name = artifact.get("callable_name") or artifact.get("name")
        if not isinstance(source, str) or not source.strip():
            return TestReport(
                passed=False,
                total=len(tests),
                failures=len(tests),
                error="Tool source code is required.",
            )
        if not isinstance(callable_name, str) or not callable_name.strip():
            return TestReport(
                passed=False,
                total=len(tests),
                failures=len(tests),
                error="Tool callable_name is required.",
            )

        request = ToolSandboxTestSuiteRequest(
            source_code=source,
            callable_name=callable_name,
            tests=tests,
            timeout_seconds=self._tool_sandbox_timeout_seconds(),
        )
        result = sandbox_from_optional_config(
            self._runtime_config,
            default_timeout_seconds=self._tool_sandbox_timeout_seconds(),
        ).run_tests(request)
        return self._tool_sandbox_report(result)

    def _execute_create_skill(  # noqa: PLR0912, PLR0915
        self,
        plan: IntentPlanResponse,
        *,
        session_id: str,
        session_factory: Any,
        user: str,
    ) -> dict[str, Any]:
        skill_name = self._slot_value(plan, "skill_name")
        description = self._slot_value(plan, "description")
        content = self._slot_value(plan, "content")

        if not isinstance(skill_name, str) or not skill_name.strip():
            raise ValueError("skill_name must be a non-empty string")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("description must be a non-empty string")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be a non-empty string")

        summary = self._slot_value(plan, "summary")
        if not isinstance(summary, str):
            summary = ""
        category = self._slot_value(plan, "category")
        if not isinstance(category, str) or not category.strip():
            category = "custom"
        tags = self._slot_value(plan, "tags")
        if not isinstance(tags, list):
            tags = []
        skill_metadata = self._slot_value(plan, "skill_metadata")
        if not isinstance(skill_metadata, dict):
            skill_metadata = {}
        allowed_tools = self._slot_value(plan, "allowed_tools")
        if not isinstance(allowed_tools, str):
            allowed_tools = None
        depends_on = self._slot_value(plan, "depends_on")
        if not isinstance(depends_on, list):
            depends_on = []

        artifact = {
            "name": skill_name.strip(),
            "description": description.strip(),
            "summary": summary.strip(),
            "content": content.strip(),
            "owner": user,
            "category": category.strip(),
            "tags": [str(tag) for tag in tags if isinstance(tag, str) and tag.strip()],
            "skill_metadata": skill_metadata,
            "allowed_tools": allowed_tools.strip() if isinstance(allowed_tools, str) else None,
            "depends_on": [
                str(dependency)
                for dependency in depends_on
                if isinstance(dependency, str) and dependency.strip()
            ],
        }

        schema_errors: list[str] = []
        try:
            SkillCreateRequest.model_validate(artifact)
        except ValidationError as exc:
            schema_errors = [
                f"{'.'.join(str(part) for part in error.get('loc', ()))}: {error.get('msg', 'invalid')}"
                for error in exc.errors()
            ]

        base_validation = validate_draft("skill", artifact)
        validation_errors = [*base_validation.errors, *schema_errors]
        validation_report = ValidationReport(
            valid=len(validation_errors) == 0,
            errors=validation_errors,
            warnings=base_validation.warnings,
        )
        if validation_report.valid:
            test_report = self._run_skill_package_tests(artifact)
        else:
            test_report = TestReport(
                passed=False,
                total=1,
                failures=1,
                details=[],
                error="Skill draft validation failed; package check was not run.",
            )

        passed = validation_report.valid and test_report.passed
        draft_status = (
            "tested"
            if passed
            else ("test_failed" if validation_report.valid else "validation_failed")
        )
        draft_id = new_assistant_draft_id()
        warnings = [
            *validation_report.warnings,
            *validation_report.errors,
        ]
        if test_report.error:
            warnings.append(test_report.error)

        with session_factory() as db:
            session_row = self._get_owned_session(db, session_id, user)
            if session_row is None:
                raise ValueError(f"Session {session_id} not found")

            draft = CaliberAssistantDraft(
                draft_id=draft_id,
                session_id=session_id,
                artifact_type="skill",
                status=draft_status,
                title=artifact["name"],
                summary=str(artifact["description"]),
                spec={
                    "intent_name": plan.intent.name,
                    "plan_id": plan.plan_id,
                    "category": artifact["category"],
                    "package_check": test_report.passed,
                },
                artifact=artifact,
                validation_report=validation_report.model_dump(mode="json"),
                test_report=test_report.model_dump(mode="json"),
                created_by=user,
                updated_by=user,
            )
            db.add(draft)
            session_row.active_draft_id = draft_id
            audit_record(
                db,
                actor=user,
                action="create_skill_draft",
                entity_type="assistant_draft",
                entity_id=draft_id,
                details={
                    "source": "caliber-assistant",
                    "plan_id": plan.plan_id,
                    "session_id": session_id,
                    "skill_name": artifact["name"],
                    "validation_valid": validation_report.valid,
                    "package_check_passed": test_report.passed,
                    "draft_status": draft_status,
                },
            )
            db.commit()

        next_actions: list[dict[str, Any]] = []
        if passed:
            next_actions.extend(
                [
                    {
                        "intent_name": "approve_draft",
                        "label": "Approve skill draft",
                        "slot_overrides": {"draft_id": draft_id},
                        "requires_confirmation": True,
                    },
                    {
                        "intent_name": "publish_draft",
                        "label": "Publish after approval",
                        "slot_overrides": {"draft_id": draft_id},
                        "requires_confirmation": True,
                    },
                ]
            )

        return self._result_envelope(
            result_type="skill_draft",
            status="completed" if passed else "blocked",
            summary=(
                f"Created and package-checked skill draft {artifact['name']}."
                if passed
                else f"Created skill draft {artifact['name']}, but validation or package checks failed."
            ),
            ids={"draft_id": draft_id, "skill_name": artifact["name"]},
            links=[
                {
                    "label": "Skill draft",
                    "resource_type": "assistant_draft",
                    "id": draft_id,
                    "path": f"/assistant/drafts/{draft_id}",
                }
            ],
            warnings=warnings,
            next_actions=next_actions,
            extra={
                "draft": {
                    "draft_id": draft_id,
                    "artifact_type": "skill",
                    "status": draft_status,
                    "title": artifact["name"],
                    "artifact": artifact,
                },
                "validation_report": validation_report.model_dump(mode="json"),
                "test_report": test_report.model_dump(mode="json"),
            },
        )

    def _run_skill_package_tests(self, artifact: dict[str, Any]) -> TestReport:
        try:
            payload = SkillCreateRequest.model_validate(artifact)
        except ValidationError as exc:
            details = [
                {
                    "test": "skill_schema",
                    "passed": False,
                    "field": ".".join(str(part) for part in error.get("loc", ())),
                    "error": error.get("msg", "invalid"),
                }
                for error in exc.errors()
            ]
            return TestReport(
                passed=False,
                total=max(len(details), 1),
                failures=max(len(details), 1),
                details=details,
                error="Skill schema validation failed.",
            )

        skill = CaliberSkill(
            skill_id="assistant-draft",
            name=payload.name,
            description=payload.description,
            summary=payload.summary,
            content=payload.content,
            owner=payload.owner,
            category=payload.category,
            tags=list(payload.tags),
            skill_metadata=dict(payload.skill_metadata),
            allowed_tools=payload.allowed_tools,
            depends_on=list(payload.depends_on),
            status="active",
            version=1,
        )
        package = build_skill_package(skill)
        detail = {
            "test": "package_build",
            "passed": package.is_valid,
            "file_count": len(package.files),
            "resource_counts": package.resource_counts,
            "warnings": package.warnings,
        }
        return TestReport(
            passed=package.is_valid,
            total=1,
            failures=0 if package.is_valid else 1,
            details=[detail],
            error="; ".join(package.warnings) if package.warnings else None,
        )

    def _execute_create_workflow(  # noqa: PLR0915
        self,
        plan: IntentPlanResponse,
        *,
        session_id: str,
        session_factory: Any,
        user: str,
    ) -> dict[str, Any]:
        workflow_name = self._slot_value(plan, "workflow_name")
        raw_manifest = self._slot_value(plan, "manifest")
        if not isinstance(workflow_name, str) or not workflow_name.strip():
            raise ValueError("workflow_name must be a non-empty string")
        if not isinstance(raw_manifest, dict):
            raise ValueError("manifest must be a structured object")

        draft_id = new_assistant_draft_id()
        clean_name = workflow_name.strip()
        manifest = copy.deepcopy(raw_manifest)
        manifest.setdefault("workflow_id", f"wf_{draft_id.lower().replace('-', '_')}")
        manifest.setdefault("name", clean_name)
        manifest.setdefault("owner", user)

        description = self._slot_value(plan, "description")
        if not isinstance(description, str) or not description.strip():
            description = str(manifest.get("description") or "")
        if description:
            manifest.setdefault("description", description)
        default_experiment_id = self._slot_value(plan, "default_experiment_id")
        if not isinstance(default_experiment_id, str) or not default_experiment_id.strip():
            default_experiment_id = None

        artifact = {
            "name": clean_name,
            "description": description.strip() if isinstance(description, str) else "",
            "owner": user,
            "default_experiment_id": default_experiment_id,
            "workflow_id": str(manifest.get("workflow_id") or ""),
            "manifest": manifest,
        }

        schema_errors: list[str] = []
        try:
            parse_manifest(manifest)
        except Exception as exc:
            schema_errors = [f"Workflow manifest validation failed: {type(exc).__name__}: {exc}"]

        base_validation = validate_draft("workflow", artifact)
        validation_warnings = [
            warning
            for warning in base_validation.warnings
            if warning != "Manifest missing 'version' field."
        ]
        validation_errors = [*base_validation.errors, *schema_errors]
        validation_report = ValidationReport(
            valid=len(validation_errors) == 0,
            errors=validation_errors,
            warnings=validation_warnings,
        )
        if validation_report.valid:
            test_report = self._run_workflow_compile_tests(artifact)
        else:
            test_report = TestReport(
                passed=False,
                total=1,
                failures=1,
                details=[],
                error="Workflow draft validation failed; compile preview was not run.",
            )

        passed = validation_report.valid and test_report.passed
        draft_status = (
            "tested"
            if passed
            else ("test_failed" if validation_report.valid else "validation_failed")
        )
        warnings = [
            *validation_report.warnings,
            *validation_report.errors,
        ]
        if test_report.error:
            warnings.append(test_report.error)

        with session_factory() as db:
            session_row = self._get_owned_session(db, session_id, user)
            if session_row is None:
                raise ValueError(f"Session {session_id} not found")

            draft = CaliberAssistantDraft(
                draft_id=draft_id,
                session_id=session_id,
                artifact_type="workflow",
                status=draft_status,
                title=clean_name,
                summary=artifact["description"],
                spec={
                    "intent_name": plan.intent.name,
                    "plan_id": plan.plan_id,
                    "workflow_id": artifact["workflow_id"],
                    "compile_preview": test_report.passed,
                },
                artifact=artifact,
                validation_report=validation_report.model_dump(mode="json"),
                test_report=test_report.model_dump(mode="json"),
                created_by=user,
                updated_by=user,
            )
            db.add(draft)
            session_row.active_draft_id = draft_id
            audit_record(
                db,
                actor=user,
                action="create_workflow_draft",
                entity_type="assistant_draft",
                entity_id=draft_id,
                details={
                    "source": "caliber-assistant",
                    "plan_id": plan.plan_id,
                    "session_id": session_id,
                    "workflow_name": clean_name,
                    "workflow_id": artifact["workflow_id"],
                    "validation_valid": validation_report.valid,
                    "compile_preview_passed": test_report.passed,
                    "draft_status": draft_status,
                },
            )
            db.commit()

        next_actions: list[dict[str, Any]] = []
        if passed:
            next_actions.extend(
                [
                    {
                        "intent_name": "approve_draft",
                        "label": "Approve workflow draft",
                        "slot_overrides": {"draft_id": draft_id},
                        "requires_confirmation": True,
                    },
                    {
                        "intent_name": "publish_draft",
                        "label": "Publish after approval",
                        "slot_overrides": {"draft_id": draft_id},
                        "requires_confirmation": True,
                    },
                ]
            )

        return self._result_envelope(
            result_type="workflow_draft",
            status="completed" if passed else "blocked",
            summary=(
                f"Created and compiled workflow draft {clean_name}."
                if passed
                else f"Created workflow draft {clean_name}, but validation or compile checks failed."
            ),
            ids={
                "draft_id": draft_id,
                "workflow_name": clean_name,
                "workflow_id": artifact["workflow_id"],
            },
            links=[
                {
                    "label": "Workflow draft",
                    "resource_type": "assistant_draft",
                    "id": draft_id,
                    "path": f"/assistant/drafts/{draft_id}",
                }
            ],
            warnings=warnings,
            next_actions=next_actions,
            extra={
                "draft": {
                    "draft_id": draft_id,
                    "artifact_type": "workflow",
                    "status": draft_status,
                    "title": clean_name,
                    "artifact": artifact,
                },
                "validation_report": validation_report.model_dump(mode="json"),
                "test_report": test_report.model_dump(mode="json"),
            },
        )

    def _run_workflow_compile_tests(self, artifact: dict[str, Any]) -> TestReport:
        manifest_payload = artifact.get("manifest")
        if not isinstance(manifest_payload, dict):
            return TestReport(
                passed=False,
                total=1,
                failures=1,
                details=[{"test": "workflow_manifest", "passed": False}],
                error="Workflow manifest must be a structured object.",
            )
        try:
            manifest = parse_manifest(manifest_payload)
            result = compile_workflow(
                manifest,
                resolver=InMemoryToolResolver([]),
                version="draft",
            )
        except CompileError as exc:
            return TestReport(
                passed=False,
                total=1,
                failures=1,
                details=[
                    {
                        "test": "workflow_compile",
                        "passed": False,
                        "report": exc.report or {},
                    }
                ],
                error=str(exc),
            )
        except Exception as exc:
            return TestReport(
                passed=False,
                total=1,
                failures=1,
                details=[{"test": "workflow_compile", "passed": False}],
                error=f"{type(exc).__name__}: {exc}",
            )

        detail = {
            "test": "workflow_compile",
            "passed": True,
            "manifest_hash": result.manifest_hash,
            "node_count": len(result.ir.nodes),
            "edge_count": len(result.ir.edges),
            "generated_python_bytes": len(result.generated_python.encode("utf-8")),
            "compiler_report": result.report,
        }
        return TestReport(passed=True, total=1, failures=0, details=[detail])

    def _execute_create_mcp_server(  # noqa: PLR0912, PLR0915
        self,
        plan: IntentPlanResponse,
        *,
        session_id: str,
        session_factory: Any,
        user: str,
    ) -> dict[str, Any]:
        server_name = self._slot_value(plan, "server_name")
        transport = self._slot_value(plan, "transport")
        if not isinstance(server_name, str) or not server_name.strip():
            raise ValueError("server_name must be a non-empty string")
        if not isinstance(transport, str) or not transport.strip():
            raise ValueError("transport must be a non-empty string")

        description = self._slot_value(plan, "description")
        if not isinstance(description, str):
            description = ""
        uri = self._slot_value(plan, "uri")
        if not isinstance(uri, str):
            uri = ""
        command = self._slot_value(plan, "command")
        if not isinstance(command, str):
            command = ""
        args = self._slot_value(plan, "args")
        if not isinstance(args, list):
            args = []
        env = self._slot_value(plan, "env")
        if not isinstance(env, dict):
            env = {}
        headers = self._slot_value(plan, "headers")
        if not isinstance(headers, dict):
            headers = {}
        auth_type = self._slot_value(plan, "auth_type")
        if not isinstance(auth_type, str) or not auth_type.strip():
            auth_type = "none"
        auth_config = self._slot_value(plan, "auth_config")
        if not isinstance(auth_config, dict):
            auth_config = {}
        icon = self._slot_value(plan, "icon")
        if not isinstance(icon, str):
            icon = ""
        discovered_tools = self._slot_value(plan, "discovered_tools")
        if not isinstance(discovered_tools, list):
            discovered_tools = []

        artifact = {
            "name": server_name.strip(),
            "description": description.strip(),
            "transport": transport.strip(),
            "uri": uri.strip(),
            "command": command.strip(),
            "args": [str(arg) for arg in args if isinstance(arg, str)],
            "env": {str(key): str(value) for key, value in env.items()},
            "headers": {str(key): str(value) for key, value in headers.items()},
            "auth_type": auth_type.strip(),
            "auth_config": auth_config,
            "icon": icon.strip(),
            "owner": user,
            "discovered_tools": [dict(tool) for tool in discovered_tools if isinstance(tool, dict)],
        }
        schema_payload = {
            key: value for key, value in artifact.items() if key != "discovered_tools"
        }
        schema_errors: list[str] = []
        try:
            McpServerCreateRequest.model_validate(schema_payload)
        except ValidationError as exc:
            schema_errors = [
                f"{'.'.join(str(part) for part in error.get('loc', ()))}: {error.get('msg', 'invalid')}"
                for error in exc.errors()
            ]

        base_validation = validate_draft("mcp_server", artifact)
        validation_errors = [*base_validation.errors, *schema_errors]
        validation_report = ValidationReport(
            valid=len(validation_errors) == 0,
            errors=validation_errors,
            warnings=base_validation.warnings,
        )
        if validation_report.valid:
            test_report = self._run_mcp_connection_tests(artifact)
        else:
            test_report = TestReport(
                passed=False,
                total=1,
                failures=1,
                details=[],
                error="MCP server draft validation failed; connection preview was not run.",
            )

        passed = validation_report.valid and test_report.passed
        draft_status = (
            "tested"
            if passed
            else ("test_failed" if validation_report.valid else "validation_failed")
        )
        draft_id = new_assistant_draft_id()
        warnings = [
            *validation_report.warnings,
            *validation_report.errors,
        ]
        if test_report.error:
            warnings.append(test_report.error)

        with session_factory() as db:
            session_row = self._get_owned_session(db, session_id, user)
            if session_row is None:
                raise ValueError(f"Session {session_id} not found")

            draft = CaliberAssistantDraft(
                draft_id=draft_id,
                session_id=session_id,
                artifact_type="mcp_server",
                status=draft_status,
                title=artifact["name"],
                summary=artifact["description"],
                spec={
                    "intent_name": plan.intent.name,
                    "plan_id": plan.plan_id,
                    "transport": artifact["transport"],
                    "connection_preview": test_report.passed,
                },
                artifact=artifact,
                validation_report=validation_report.model_dump(mode="json"),
                test_report=test_report.model_dump(mode="json"),
                created_by=user,
                updated_by=user,
            )
            db.add(draft)
            session_row.active_draft_id = draft_id
            audit_record(
                db,
                actor=user,
                action="create_mcp_server_draft",
                entity_type="assistant_draft",
                entity_id=draft_id,
                details={
                    "source": "caliber-assistant",
                    "plan_id": plan.plan_id,
                    "session_id": session_id,
                    "server_name": artifact["name"],
                    "transport": artifact["transport"],
                    "validation_valid": validation_report.valid,
                    "connection_preview_passed": test_report.passed,
                    "draft_status": draft_status,
                },
            )
            db.commit()

        next_actions: list[dict[str, Any]] = []
        if passed:
            next_actions.extend(
                [
                    {
                        "intent_name": "approve_draft",
                        "label": "Approve MCP server draft",
                        "slot_overrides": {"draft_id": draft_id},
                        "requires_confirmation": True,
                    },
                    {
                        "intent_name": "publish_draft",
                        "label": "Publish after approval",
                        "slot_overrides": {"draft_id": draft_id},
                        "requires_confirmation": True,
                    },
                ]
            )

        return self._result_envelope(
            result_type="mcp_server_draft",
            status="completed" if passed else "blocked",
            summary=(
                f"Created and connection-checked MCP server draft {artifact['name']}."
                if passed
                else f"Created MCP server draft {artifact['name']}, but validation or connection checks failed."
            ),
            ids={"draft_id": draft_id, "server_name": artifact["name"]},
            links=[
                {
                    "label": "MCP server draft",
                    "resource_type": "assistant_draft",
                    "id": draft_id,
                    "path": f"/assistant/drafts/{draft_id}",
                }
            ],
            warnings=warnings,
            next_actions=next_actions,
            extra={
                "draft": {
                    "draft_id": draft_id,
                    "artifact_type": "mcp_server",
                    "status": draft_status,
                    "title": artifact["name"],
                    "artifact": artifact,
                },
                "validation_report": validation_report.model_dump(mode="json"),
                "test_report": test_report.model_dump(mode="json"),
            },
        )

    def _run_mcp_connection_tests(self, artifact: dict[str, Any]) -> TestReport:
        transport = str(artifact.get("transport") or "")
        errors: list[str] = []
        if transport == "stdio" and not str(artifact.get("command") or "").strip():
            errors.append("stdio transport requires a 'command'")
        if transport in {"sse", "streamable-http"} and not str(artifact.get("uri") or "").strip():
            errors.append(f"{transport} transport requires a 'uri'")
        tools = artifact.get("discovered_tools")
        if not isinstance(tools, list):
            tools = []
        if errors:
            return TestReport(
                passed=False,
                total=1,
                failures=1,
                details=[
                    {
                        "test": "mcp_connection_preview",
                        "passed": False,
                        "transport": transport,
                        "tools": [],
                    }
                ],
                error="; ".join(errors),
            )
        return TestReport(
            passed=True,
            total=1,
            failures=0,
            details=[
                {
                    "test": "mcp_connection_preview",
                    "passed": True,
                    "transport": transport,
                    "tools": tools,
                    "tool_count": len(tools),
                }
            ],
        )

    def _execute_prompt_write(
        self,
        plan: IntentPlanResponse,
        *,
        is_edit: bool,
        user: str,
    ) -> dict[str, Any]:
        prompt_name = self._slot_value(plan, "prompt_name")
        template = self._slot_value(plan, "template")
        commit_message = self._slot_value(plan, "commit_message")

        if not isinstance(prompt_name, str) or not prompt_name.strip():
            raise ValueError("prompt_name must be a non-empty string")
        if not isinstance(template, str) or not template.strip():
            raise ValueError("template must be a non-empty string")

        effective_commit = (
            str(commit_message).strip()
            if isinstance(commit_message, str) and commit_message.strip()
            else ("edited via CALIBER assistant" if is_edit else "created via CALIBER assistant")
        )

        from caliber.routes import prompts as prompt_routes  # noqa: PLC0415

        raw = prompt_routes.register_prompt_version(
            name=prompt_name.strip(),
            template=template,
            commit_message=effective_commit,
            tags={
                "caliber.source": "caliber-assistant",
                "caliber.intent": plan.intent.name,
                "caliber.plan_id": plan.plan_id,
                "caliber.actor": user,
            },
            set_prod_alias=False,
        )
        prompt_version = raw.get("version")
        prompt_id = prompt_name.strip()
        return self._result_envelope(
            result_type="prompt_version",
            status="completed",
            summary=f"Registered prompt version for {prompt_id}.",
            ids={"prompt_name": prompt_id, "version": prompt_version},
            links=[
                {
                    "label": "Prompt version",
                    "resource_type": "prompt_version",
                    "id": f"{prompt_id}:{prompt_version}",
                    "path": f"/prompts/{prompt_id}/versions/{prompt_version}",
                }
            ],
            next_actions=[
                {
                    "intent_name": "propose_promotion",
                    "label": "Propose promotion",
                    "slot_overrides": {
                        "prompt_name": prompt_id,
                        "source_version": prompt_version,
                        "target_alias": "prod",
                    },
                    "requires_confirmation": True,
                }
            ],
            extra={**raw, "alias_changed": False},
        )

    def _execute_prompt_optimization(
        self,
        plan: IntentPlanResponse,
        *,
        session_factory: Any,
        user: str,
    ) -> dict[str, Any]:
        agent_id = self._slot_value(plan, "agent_id")
        eval_dataset_id = self._slot_value(plan, "eval_dataset_id")
        optimizer_type = self._slot_value(plan, "optimizer_type")
        scorers_raw = self._slot_value(plan, "scorers")
        min_aggregate = self._slot_value(plan, "gate.min_aggregate_score")
        max_regression = self._slot_value(plan, "gate.max_regression_delta")

        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError("agent_id must be a non-empty string")
        if not isinstance(eval_dataset_id, str) or not eval_dataset_id.strip():
            raise ValueError("eval_dataset_id must be a non-empty string")
        if not isinstance(optimizer_type, str) or not optimizer_type.strip():
            raise ValueError("optimizer_type must be a non-empty string")

        normalized_scorers: list[dict[str, Any]] = []
        if isinstance(scorers_raw, list):
            for scorer in scorers_raw:
                if isinstance(scorer, str):
                    normalized_scorers.append({"name": scorer, "weight": 1.0, "config": {}})
                elif isinstance(scorer, dict) and isinstance(scorer.get("name"), str):
                    normalized_scorers.append(
                        {
                            "name": str(scorer.get("name")),
                            "weight": float(scorer.get("weight", 1.0)),
                            "config": dict(scorer.get("config") or {}),
                        }
                    )
        if len(normalized_scorers) == 0:
            raise ValueError("scorers must include at least one scorer")

        payload_dict: dict[str, Any] = {
            "agent_id": agent_id,
            "eval_dataset_id": eval_dataset_id,
            "optimizer_type": optimizer_type,
            "scorers": normalized_scorers,
            "notes": f"Queued from assistant intent plan {plan.plan_id}",
        }
        gate_payload: dict[str, Any] = {}
        if min_aggregate is not None:
            gate_payload["min_aggregate_score"] = float(min_aggregate)
        if max_regression is not None:
            gate_payload["max_regression_delta"] = float(max_regression)
        if gate_payload:
            payload_dict["gate"] = gate_payload

        payload = PromptOptimizationRunRequest.model_validate(payload_dict)

        from caliber.routes import prompts as prompt_routes  # noqa: PLC0415

        with session_factory() as db:
            response = prompt_routes.enqueue_prompt_optimization_run(
                session=db,
                payload=payload,
                actor=user,
            )
            raw = response.model_dump(mode="json")
            _job = raw.get("job")
            job = _job if isinstance(_job, dict) else {}
            _item = raw.get("item")
            item = _item if isinstance(_item, dict) else {}
            return self._result_envelope(
                result_type="optimization_run",
                status="completed",
                summary="Queued prompt calibration run.",
                ids={
                    "job_id": job.get("job_id"),
                    "item_id": item.get("item_id"),
                    "agent_id": agent_id,
                    "eval_dataset_id": eval_dataset_id,
                },
                links=[
                    {
                        "label": "Calibration job",
                        "resource_type": "job",
                        "id": str(job.get("job_id") or ""),
                        "path": f"/jobs/{job.get('job_id')}",
                    }
                ]
                if job.get("job_id")
                else [],
                extra=raw,
            )

    def _execute_workflow_calibration(
        self,
        plan: IntentPlanResponse,
        *,
        session_factory: Any,
        user: str,
    ) -> dict[str, Any]:
        workflow_id = self._slot_value(plan, "workflow_id")
        agent_id = self._slot_value(plan, "agent_id")
        objective = self._slot_value(plan, "objective")
        epsilon = self._slot_value(plan, "epsilon")
        max_candidates = self._slot_value(plan, "max_candidates")

        if not isinstance(workflow_id, str) or not workflow_id.strip():
            raise ValueError("workflow_id must be a non-empty string")

        warnings: list[str] = []
        if not isinstance(agent_id, str) or not agent_id.strip():
            with session_factory() as db:
                agent = (
                    db.query(CaliberAgentConfig)
                    .filter(CaliberAgentConfig.enabled.is_(True))
                    .order_by(CaliberAgentConfig.created_at.asc())
                    .first()
                )
                if agent is None:
                    raise ValueError("No enabled agent is available for workflow calibration")
                agent_id = agent.agent_id
                warnings.append(f"Defaulted agent_id to {agent_id}.")

        objective_name = str(objective or "quality").strip()
        if objective_name not in {"quality", "tool_correctness", "tool_adherence"}:
            objective_name = "quality"
            warnings.append("Unsupported objective was replaced with quality.")
        try:
            epsilon_value = float(epsilon) if epsilon is not None else 0.02
        except (TypeError, ValueError):
            epsilon_value = 0.02
        epsilon_value = max(0.0, min(1.0, epsilon_value))
        try:
            max_candidates_value = int(max_candidates) if max_candidates is not None else 3
        except (TypeError, ValueError):
            max_candidates_value = 3
        max_candidates_value = max(1, min(5, max_candidates_value))

        payload = WorkflowCalibrationRunRequest.model_validate(
            {
                "agent_id": agent_id.strip(),
                "objective": {"maximize": objective_name, "epsilon": epsilon_value},
                "budget": {"max_candidates": max_candidates_value},
            }
        )

        from caliber.routes import (  # noqa: PLC0415
            workflow_calibration as workflow_calibration_routes,
        )

        with session_factory() as db:
            response = workflow_calibration_routes.enqueue_workflow_calibration_run(
                session=db,
                workflow_id=workflow_id.strip(),
                payload=payload,
                actor=user,
                config=self._runtime_config,
            )
            raw = response.model_dump(mode="json")
            _job = raw.get("job")
            job = _job if isinstance(_job, dict) else {}
            _item = raw.get("item")
            item = _item if isinstance(_item, dict) else {}
            return self._result_envelope(
                result_type="workflow_calibration_run",
                status="completed",
                summary=f"Queued workflow calibration run for {workflow_id.strip()}.",
                ids={
                    "workflow_id": workflow_id.strip(),
                    "job_id": job.get("job_id"),
                    "item_id": item.get("item_id"),
                    "agent_id": agent_id.strip(),
                },
                links=[
                    {
                        "label": "Calibration job",
                        "resource_type": "job",
                        "id": str(job.get("job_id") or ""),
                        "path": f"/jobs/{job.get('job_id')}",
                    },
                    {
                        "label": "Workflow",
                        "resource_type": "workflow",
                        "id": workflow_id.strip(),
                        "path": f"/workflows/{workflow_id.strip()}",
                    },
                ]
                if job.get("job_id")
                else [],
                warnings=warnings,
                extra=raw,
            )

    def _execute_generate_test_cases(self, plan: IntentPlanResponse) -> dict[str, Any]:
        prompt_name = self._slot_value(plan, "prompt_name")
        if not isinstance(prompt_name, str) or not prompt_name.strip():
            raise ValueError("prompt_name must be a non-empty string")
        clean_name = prompt_name.strip()
        examples = self._generate_test_cases(clean_name)
        return self._result_envelope(
            result_type="test_cases",
            status="completed",
            summary=f"Generated {len(examples)} candidate test cases for {clean_name}.",
            ids={"prompt_name": clean_name},
            next_actions=[
                {
                    "intent_name": "save_eval_dataset",
                    "label": "Save as eval dataset",
                    "slot_overrides": {
                        "dataset_name": f"{clean_name}-generated-tests",
                        "examples": examples,
                    },
                    "requires_confirmation": True,
                }
            ],
            extra={"examples": examples},
        )

    def _generate_test_cases(self, prompt_name: str) -> list[dict[str, Any]]:
        """Generate candidate test cases for a prompt via the assistant engine.

        Falls back to a deterministic set when the engine is unavailable or its
        reply isn't parseable JSON (e.g. the fake engine), so this never raises
        and the fake/CI path stays deterministic (golden-path roadmap, Wave 5.3).
        """
        grounding = ""
        if self._prompt_fetcher is not None:
            try:
                template = self._prompt_fetcher(prompt_name)
            except Exception:
                template = None
            if template and template.strip():
                grounding = (
                    f'Here is the prompt template under test:\n"""\n{template.strip()}\n"""\n\n'
                )
        instruction = (
            grounding + "Generate 3 to 5 diverse evaluation test cases for the prompt named "
            f"{prompt_name!r}. Return ONLY a JSON array (no prose, no code fences); "
            'each element an object with keys: "input" (object with a "query" '
            'string), "expected" (object with a "behavior" string describing the '
            'desired behavior), and "tags" (array of strings).'
        )
        try:
            result = self._engine.run_turn(
                AssistantTurnRequest(
                    session_id=f"gen-test-cases:{prompt_name}",
                    user_message=instruction,
                    goal="generate_test_cases",
                )
            )
            cases = self._parse_test_cases(result.reply if result else "")
        except Exception:
            logger.debug("test-case generation via engine failed; using fallback", exc_info=True)
            cases = []
        return cases or self._fallback_test_cases()

    @staticmethod
    def _parse_test_cases(reply: str) -> list[dict[str, Any]]:
        """Extract a validated list of test cases from a model reply, or ``[]``.

        Tolerant of surrounding prose, code fences, footnote brackets (``[3]``),
        and multiple arrays: tries the fenced block, the whole reply, then each
        balanced ``[...]`` span, and returns the first that validates into a
        non-empty list of ``{input, expected, tags}`` cases.
        """
        import json  # noqa: PLC0415

        text = (reply or "").strip()
        if not text:
            return []
        candidates: list[str] = []
        fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if fence:
            candidates.append(fence.group(1).strip())
        candidates.append(text)
        candidates.extend(AssistantService._balanced_array_spans(text))
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(parsed, list):
                continue
            cases = AssistantService._validate_cases(parsed)
            if cases:
                return cases
        return []

    @staticmethod
    def _balanced_array_spans(text: str) -> list[str]:
        """Return each top-level balanced ``[...]`` substring (string-aware)."""
        spans: list[str] = []
        i, n = 0, len(text)
        while i < n:
            if text[i] != "[":
                i += 1
                continue
            depth = 0
            in_str = False
            esc = False
            j = i
            while j < n:
                c = text[j]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                elif c == '"':
                    in_str = True
                elif c == "[":
                    depth += 1
                elif c == "]":
                    depth -= 1
                    if depth == 0:
                        spans.append(text[i : j + 1])
                        break
                j += 1
            i = j + 1
        return spans

    @staticmethod
    def _validate_cases(raw: list[Any]) -> list[dict[str, Any]]:
        cases: list[dict[str, Any]] = []
        for item in raw[:5]:
            if not isinstance(item, dict):
                continue
            inp = item.get("input")
            if isinstance(inp, str):
                inp = {"query": inp}
            if not isinstance(inp, dict) or not inp:
                continue
            expected = item.get("expected")
            if isinstance(expected, str):
                expected = {"behavior": expected}
            if not isinstance(expected, dict):
                expected = {}
            tags = item.get("tags")
            if not isinstance(tags, list):
                tags = ["generated"]
            cases.append({"input": inp, "expected": expected, "tags": [str(t) for t in tags]})
        return cases

    @staticmethod
    def _fallback_test_cases() -> list[dict[str, Any]]:
        """Deterministic test cases used when engine generation is unavailable."""
        return [
            {
                "input": {"query": "Summarize the current refund policy."},
                "expected": {
                    "behavior": "Ground the answer in policy text and avoid unsupported claims."
                },
                "tags": ["generated", "policy"],
            },
            {
                "input": {"query": "Escalate a frustrated customer asking for an exception."},
                "expected": {"behavior": "Acknowledge the concern and follow escalation guidance."},
                "tags": ["generated", "escalation"],
            },
            {
                "input": {"query": "Answer when the requested information is missing."},
                "expected": {"behavior": "Ask a clarifying question instead of hallucinating."},
                "tags": ["generated", "clarification"],
            },
        ]

    def _execute_save_eval_dataset(
        self,
        plan: IntentPlanResponse,
        *,
        session_factory: Any,
        user: str,
    ) -> dict[str, Any]:
        dataset_name = self._slot_value(plan, "dataset_name")
        raw_examples = self._slot_value(plan, "examples")
        if not isinstance(dataset_name, str) or not dataset_name.strip():
            raise ValueError("dataset_name must be a non-empty string")
        if not isinstance(raw_examples, list) or len(raw_examples) == 0:
            raise ValueError("examples must include at least one example")

        examples: list[EvalExampleCreateRequest] = []
        for raw in raw_examples:
            if not isinstance(raw, dict):
                raise ValueError("each example must be an object")
            examples.append(EvalExampleCreateRequest.model_validate(raw))

        with session_factory() as db:
            existing = (
                db.query(CaliberEvalDataset)
                .filter(CaliberEvalDataset.name == dataset_name.strip())
                .first()
            )
            if existing is not None:
                raise ValueError(f"eval dataset name {dataset_name!r} is already in use")

            dataset = CaliberEvalDataset(
                dataset_id=new_eval_dataset_id(),
                name=dataset_name.strip(),
                description=f"Created by CALIBER assistant plan {plan.plan_id}",
                owner=user,
                tags=["assistant-generated"],
                status="active",
                version=1,
            )
            db.add(dataset)
            db.flush()

            example_ids: list[str] = []
            for example_payload in examples:
                dataset.version += 1
                example = CaliberEvalDatasetExample(
                    example_id=new_eval_example_id(),
                    dataset_id=dataset.dataset_id,
                    dataset_version=dataset.version,
                    input=dict(example_payload.input),
                    expected=dict(example_payload.expected),
                    weight=example_payload.weight,
                    tags=list(example_payload.tags),
                )
                db.add(example)
                db.flush()
                example_ids.append(example.example_id)

            db.commit()
            return self._result_envelope(
                result_type="eval_dataset",
                status="completed",
                summary=f"Saved {len(example_ids)} examples to eval dataset {dataset.name}.",
                ids={
                    "dataset_id": dataset.dataset_id,
                    "dataset_name": dataset.name,
                    "dataset_version": dataset.version,
                    "example_ids": example_ids,
                },
                links=[
                    {
                        "label": "Eval dataset",
                        "resource_type": "eval_dataset",
                        "id": dataset.dataset_id,
                        "path": f"/eval-datasets/{dataset.dataset_id}",
                    }
                ],
            )

    def _execute_review_optimization_result(
        self,
        plan: IntentPlanResponse,
        *,
        session_factory: Any,
    ) -> dict[str, Any]:
        job_id = self._slot_value(plan, "job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("job_id must be a non-empty string")

        with session_factory() as db:
            job = db.get(CaliberRefinementJob, job_id.strip())
            if job is None:
                raise ValueError(f"refinement job {job_id!r} not found")
            eval_results = getattr(job, "eval_results", None) or {}
            candidate = getattr(job, "candidate", None) or {}
            diagnosis = getattr(job, "diagnosis", None) or {}
            return self._result_envelope(
                result_type="optimization_review",
                status="completed",
                summary=f"Calibration job {job.job_id} is {job.status} at stage {job.current_stage}.",
                ids={
                    "job_id": job.job_id,
                    "agent_id": job.agent_id,
                    "mlflow_run_id": job.mlflow_run_id,
                },
                links=[
                    {
                        "label": "Calibration job",
                        "resource_type": "job",
                        "id": job.job_id,
                        "path": f"/jobs/{job.job_id}",
                    }
                ],
                extra={
                    "job": {
                        "job_id": job.job_id,
                        "status": job.status,
                        "current_stage": job.current_stage,
                        "artifact_type": job.artifact_type,
                        "optimizer_type": job.optimizer_type,
                        "error_message": job.error_message,
                    },
                    "eval_results": eval_results,
                    "candidate": candidate,
                    "diagnosis": diagnosis,
                },
            )

    def _execute_review_workflow_calibration_result(
        self,
        plan: IntentPlanResponse,
        *,
        session_factory: Any,
    ) -> dict[str, Any]:
        job_id = self._slot_value(plan, "job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("job_id must be a non-empty string")

        with session_factory() as db:
            job = db.get(CaliberRefinementJob, job_id.strip())
            if job is None:
                raise ValueError(f"refinement job {job_id!r} not found")
            eval_results = getattr(job, "eval_results", None) or {}
            candidate = getattr(job, "candidate", None) or {}
            candidates_raw = candidate.get("calibration_candidates")
            if not isinstance(candidates_raw, list):
                candidates_raw = eval_results.get("calibration_candidates")
            if not isinstance(candidates_raw, list):
                candidates_raw = []
            score_table = [
                {
                    "candidate_id": entry.get("candidate_id"),
                    "accepted": entry.get("accepted"),
                    "scores": entry.get("scores") or {},
                    "deltas": entry.get("deltas") or {},
                    "rejected_reason": entry.get("rejected_reason"),
                }
                for entry in candidates_raw
                if isinstance(entry, dict)
            ]
            winner_id = candidate.get("calibration_winner_id") or eval_results.get(
                "calibration_winner_id"
            )
            return self._result_envelope(
                result_type="workflow_calibration_review",
                status="completed",
                summary=(
                    f"Workflow calibration job {job.job_id} is {job.status} "
                    f"at stage {job.current_stage}."
                ),
                ids={
                    "job_id": job.job_id,
                    "workflow_id": job.workflow_id,
                    "agent_id": job.agent_id,
                    "winner_id": winner_id,
                },
                links=[
                    {
                        "label": "Calibration job",
                        "resource_type": "job",
                        "id": job.job_id,
                        "path": f"/jobs/{job.job_id}",
                    }
                ],
                extra={
                    "job": {
                        "job_id": job.job_id,
                        "status": job.status,
                        "current_stage": job.current_stage,
                        "artifact_type": job.artifact_type,
                        "workflow_id": job.workflow_id,
                        "error_message": job.error_message,
                    },
                    "winner_id": winner_id,
                    "score_table": score_table,
                    "eval_results": eval_results,
                    "candidate": candidate,
                    "calibration_spec": job.calibration_spec,
                },
            )

    def _execute_propose_promotion(
        self,
        plan: IntentPlanResponse,
        *,
        session_id: str,
        operation_id: str,
        trace_id: str,
        correlation_id: str,
        session_factory: Any,
        user: str,
    ) -> dict[str, Any]:
        prompt_name = self._slot_value(plan, "prompt_name")
        target_alias = self._slot_value(plan, "target_alias")
        source_version = self._slot_value(plan, "source_version")
        if not isinstance(prompt_name, str) or not prompt_name.strip():
            raise ValueError("prompt_name must be a non-empty string")
        if not isinstance(target_alias, str) or not target_alias.strip():
            raise ValueError("target_alias must be a non-empty string")
        clean_prompt_name = prompt_name.strip()
        clean_target_alias = target_alias.strip()

        if source_version is None:
            return self._result_envelope(
                result_type="blocked",
                status="blocked",
                summary="Promotion proposal needs an explicit source_version.",
                ids={"prompt_name": clean_prompt_name, "target_alias": clean_target_alias},
                warnings=["source_version is required before an alias promotion can be proposed"],
                next_actions=[
                    {
                        "intent_name": "propose_promotion",
                        "label": "Provide source version",
                        "slot_overrides": {
                            "prompt_name": clean_prompt_name,
                            "target_alias": clean_target_alias,
                        },
                        "requires_confirmation": True,
                    }
                ],
            )

        try:
            version_number = int(source_version)
        except (TypeError, ValueError):
            raise ValueError("source_version must be an integer") from None
        if version_number < 1:
            raise ValueError("source_version must be a positive integer")

        artifact_ref = f"prompts:/{clean_prompt_name}/{version_number}"
        with session_factory() as db:
            agent = db.get(CaliberAgentConfig, clean_prompt_name)
            if agent is None:
                return self._result_envelope(
                    result_type="blocked",
                    status="blocked",
                    summary="Promotion proposals require a Caliber agent linked to the prompt.",
                    ids={
                        "prompt_name": clean_prompt_name,
                        "source_version": version_number,
                        "target_alias": clean_target_alias,
                    },
                    warnings=[
                        f"Caliber agent {clean_prompt_name!r} was not found; approval requests require an agent_id",
                    ],
                )

            existing = self._find_pending_prompt_alias_approval(
                db,
                prompt_name=clean_prompt_name,
                source_version=version_number,
                target_alias=clean_target_alias,
            )
            if existing is not None:
                return self._promotion_proposal_envelope(
                    approval=existing,
                    prompt_name=clean_prompt_name,
                    source_version=version_number,
                    target_alias=clean_target_alias,
                    summary="Reused an existing pending promotion approval.",
                )

            item = CaliberVerificationItem(
                item_id=new_item_id(),
                agent_id=clean_prompt_name,
                session_id=session_id,
                category="prompt_promotion",
                free_text=(
                    f"Assistant proposal to promote {artifact_ref} to @{clean_target_alias}."
                ),
                severity="standard",
                artifact_type_hint="prompt",
                artifact_ref=artifact_ref,
                submitted_context={
                    "source": "caliber-assistant",
                    "intent": "propose_promotion",
                    "plan_id": plan.plan_id,
                    "operation_id": operation_id,
                    "session_id": session_id,
                    "trace_id": trace_id,
                    "correlation_id": correlation_id,
                    "target_alias": clean_target_alias,
                    "source_version": version_number,
                },
                status="verified",
                verified_by=user,
                verified_at=datetime.now(timezone.utc),
                verification_notes="Created from assistant promotion proposal.",
                refinement_target="prompt",
            )
            db.add(item)
            db.flush()

            candidate_snapshot = {
                "artifact_type": "prompt",
                "promotion_type": "prompt_alias",
                "prompt_name": clean_prompt_name,
                "source_version": version_number,
                "target_alias": clean_target_alias,
                "artifact_ref": artifact_ref,
                "assistant_session_id": session_id,
                "assistant_plan_id": plan.plan_id,
                "assistant_operation_id": operation_id,
                "assistant_trace_id": trace_id,
                "assistant_correlation_id": correlation_id,
                "rationale": "Assistant-proposed alias promotion for an existing prompt version.",
            }
            diagnosis_snapshot = {
                "root_cause": "assistant_promotion_proposal",
                "confidence": plan.intent.confidence,
                "alternatives": [],
                "plan_id": plan.plan_id,
                "operation_id": operation_id,
                "trace_id": trace_id,
                "correlation_id": correlation_id,
            }
            eval_results = {
                "source": "assistant_promotion_proposal",
                "gate": {
                    "passed": None,
                    "reasons": ["Human approval is required before alias rotation."],
                },
            }
            job = CaliberRefinementJob(
                job_id=new_job_id(),
                agent_id=clean_prompt_name,
                primary_item_id=item.item_id,
                artifact_type="prompt",
                optimizer_type="AssistantPromotion",
                status="awaiting_approval",
                current_stage="approval",
                attempt_count=1,
                diagnosis=diagnosis_snapshot,
                candidate=candidate_snapshot,
                eval_results=eval_results,
                bundle_targets=[],
            )
            db.add(job)
            db.flush()

            approval = CaliberApprovalRequest(
                approval_id=new_approval_id(),
                job_id=job.job_id,
                agent_id=clean_prompt_name,
                status="pending",
                attempt_number=1,
                eval_results=eval_results,
                candidate_snapshot=candidate_snapshot,
                diagnosis_snapshot=diagnosis_snapshot,
            )
            db.add(approval)

            audit_record(
                db,
                actor=user,
                action="create_item",
                entity_type="verification_item",
                entity_id=item.item_id,
                details={
                    "source": "caliber-assistant",
                    "plan_id": plan.plan_id,
                    "operation_id": operation_id,
                    "session_id": session_id,
                    "trace_id": trace_id,
                    "correlation_id": correlation_id,
                    "approval_intent": "propose_promotion",
                },
            )
            audit_record(
                db,
                actor=user,
                action="create_job",
                entity_type="refinement_job",
                entity_id=job.job_id,
                details={
                    "from_item_id": item.item_id,
                    "agent_id": clean_prompt_name,
                    "artifact_type": "prompt",
                    "optimizer_type": "AssistantPromotion",
                    "approval_id": approval.approval_id,
                    "source": "caliber-assistant",
                    "plan_id": plan.plan_id,
                    "operation_id": operation_id,
                    "session_id": session_id,
                    "trace_id": trace_id,
                    "correlation_id": correlation_id,
                },
            )
            audit_record(
                db,
                actor=user,
                action="create_approval",
                entity_type="approval",
                entity_id=approval.approval_id,
                details={
                    "job_id": job.job_id,
                    "source": "caliber-assistant",
                    "plan_id": plan.plan_id,
                    "operation_id": operation_id,
                    "session_id": session_id,
                    "trace_id": trace_id,
                    "correlation_id": correlation_id,
                    "prompt_name": clean_prompt_name,
                    "source_version": version_number,
                    "target_alias": clean_target_alias,
                },
            )
            db.commit()

            return self._promotion_proposal_envelope(
                approval=approval,
                prompt_name=clean_prompt_name,
                source_version=version_number,
                target_alias=clean_target_alias,
                summary="Created a pending approval for prompt alias promotion.",
            )

    def _find_pending_prompt_alias_approval(
        self,
        db: Any,
        *,
        prompt_name: str,
        source_version: int,
        target_alias: str,
    ) -> CaliberApprovalRequest | None:
        rows = (
            db.query(CaliberApprovalRequest)
            .filter(
                CaliberApprovalRequest.agent_id == prompt_name,
                CaliberApprovalRequest.status == "pending",
            )
            .all()
        )
        for row in rows:
            candidate = row.candidate_snapshot or {}
            if not isinstance(candidate, dict):
                continue
            if (
                candidate.get("promotion_type") == "prompt_alias"
                and candidate.get("source_version") == source_version
                and candidate.get("target_alias") == target_alias
            ):
                return cast("CaliberApprovalRequest", row)
        return None

    def _promotion_proposal_envelope(
        self,
        *,
        approval: CaliberApprovalRequest,
        prompt_name: str,
        source_version: int,
        target_alias: str,
        summary: str,
    ) -> dict[str, Any]:
        return self._result_envelope(
            result_type="promotion_proposal",
            status="completed",
            summary=summary,
            ids={
                "approval_id": approval.approval_id,
                "job_id": approval.job_id,
                "prompt_name": prompt_name,
                "source_version": source_version,
                "target_alias": target_alias,
            },
            links=[
                {
                    "label": "Approval request",
                    "resource_type": "approval",
                    "id": approval.approval_id,
                    "path": f"/approvals/{approval.approval_id}",
                },
                {
                    "label": "Prompt version",
                    "resource_type": "prompt_version",
                    "id": f"{prompt_name}:{source_version}",
                    "path": f"/prompts/{prompt_name}/versions/{source_version}",
                },
            ],
            next_actions=[
                {
                    "intent_name": "review_optimization_result",
                    "label": "Review approval job",
                    "slot_overrides": {"job_id": approval.job_id},
                    "requires_confirmation": False,
                }
            ],
        )

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Context attachments ("+ add files")
    # ------------------------------------------------------------------

    @staticmethod
    def _cap_attachment_text(text: str) -> tuple[str, bool]:
        if len(text) <= ATTACHMENT_TEXT_MAX_CHARS:
            return text, False
        return text[:ATTACHMENT_TEXT_MAX_CHARS], True

    def list_attachments(
        self,
        session_id: str,
        *,
        session_factory: Any,
        user: str | None = None,
    ) -> list[AttachmentResponse]:
        with session_factory() as db:
            if self._get_owned_session(db, session_id, user) is None:
                return []
            rows = (
                db.query(CaliberAssistantAttachment)
                .filter(CaliberAssistantAttachment.session_id == session_id)
                .order_by(CaliberAssistantAttachment.created_at)
                .all()
            )
            return [AttachmentResponse.model_validate(r) for r in rows]

    def create_attachment_record(
        self,
        session_id: str,
        *,
        kind: str,
        session_factory: Any,
        user: str,
        ref_type: str = "",
        ref_id: str = "",
        name: str = "",
        content_text: str = "",
        bytes_size: int = 0,
        truncated: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> AttachmentResponse:
        """Low-level insert of a context attachment with a capped text snapshot.

        Callers that have already pulled bytes from the object store (object_file /
        upload) resolve the text themselves and pass it in; text snippets and
        library resources are resolved by the higher-level helpers below.
        """
        capped, was_truncated = self._cap_attachment_text(content_text or "")
        with session_factory() as db:
            if self._get_owned_session(db, session_id, user) is None:
                raise ValueError(f"Session {session_id} not found")
            count = (
                db.query(CaliberAssistantAttachment)
                .filter(CaliberAssistantAttachment.session_id == session_id)
                .count()
            )
            if count >= self._settings.max_attachments_per_session:
                raise ConflictError(
                    "Attachment limit reached for this session "
                    f"({self._settings.max_attachments_per_session})."
                )
            row = CaliberAssistantAttachment(
                attachment_id=new_assistant_attachment_id(),
                session_id=session_id,
                kind=kind,
                ref_type=ref_type or "",
                ref_id=ref_id or "",
                name=name or "",
                content_text=capped,
                bytes_size=int(bytes_size or len(capped.encode("utf-8"))),
                truncated=bool(truncated or was_truncated),
                metadata_=metadata or {},
                created_by=user or "",
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return AttachmentResponse.model_validate(row)

    def add_text_attachment(
        self,
        session_id: str,
        *,
        name: str | None,
        text: str,
        session_factory: Any,
        user: str,
    ) -> AttachmentResponse:
        clean = (text or "").strip()
        if not clean:
            raise ValueError("Text snippet is empty.")
        label = (name or "").strip() or "Pasted text"
        return self.create_attachment_record(
            session_id,
            kind="text_snippet",
            session_factory=session_factory,
            user=user,
            name=label,
            content_text=clean,
        )

    def add_library_attachment(
        self,
        session_id: str,
        *,
        resource_type: str,
        resource_id: str,
        session_factory: Any,
        user: str,
    ) -> AttachmentResponse:
        name, content = self._resolve_library_resource(
            resource_type, resource_id, session_factory=session_factory
        )
        return self.create_attachment_record(
            session_id,
            kind="library_resource",
            session_factory=session_factory,
            user=user,
            ref_type=resource_type,
            ref_id=resource_id,
            name=name,
            content_text=content,
            metadata={"resource_type": resource_type, "resource_id": resource_id},
        )

    def _resolve_library_resource(
        self,
        resource_type: str,
        resource_id: str,
        *,
        session_factory: Any,
    ) -> tuple[str, str]:
        """Resolve a CALIBER asset into a (display-name, text-snapshot) pair."""
        if resource_type == "prompt":
            template = self._prompt_fetcher(resource_id) if self._prompt_fetcher else None
            if template:
                return resource_id, f"Prompt: {resource_id}\n\n{template}"
            return resource_id, f"Prompt reference: {resource_id}"

        with session_factory() as db:
            if resource_type == "skill":
                row = db.get(CaliberSkill, resource_id)
                if row is None:
                    raise ValueError(f"Skill {resource_id} not found")
                content = "\n".join(
                    part
                    for part in [
                        f"Skill: {row.name}",
                        f"Summary: {row.summary}" if row.summary else "",
                        f"Category: {row.category}",
                        row.content or "",
                    ]
                    if part
                )
                return row.name, content
            if resource_type == "tool":
                row = db.get(CaliberToolRegistry, resource_id)
                if row is None:
                    raise ValueError(f"Tool {resource_id} not found")
                content = "\n".join(
                    part
                    for part in [
                        f"Tool: {row.name} (v{row.version})",
                        f"Description: {row.description}" if row.description else "",
                        f"Callable: {row.module_path}:{row.callable_name}",
                        f"Input schema: {json.dumps(row.input_schema, default=str)}"
                        if row.input_schema
                        else "",
                    ]
                    if part
                )
                return row.name, content
            if resource_type == "workflow":
                row = db.get(CaliberWorkflow, resource_id)
                if row is None:
                    raise ValueError(f"Workflow {resource_id} not found")
                content = "\n".join(
                    part
                    for part in [
                        f"Workflow: {row.name}",
                        f"Description: {row.description}" if row.description else "",
                        f"Status: {row.status}",
                    ]
                    if part
                )
                return row.name, content
            if resource_type == "knowledge_base":
                row = db.get(CaliberKnowledgeBase, resource_id)
                if row is None:
                    raise ValueError(f"Knowledge base {resource_id} not found")
                content = "\n".join(
                    part
                    for part in [
                        f"Knowledge base: {row.name}",
                        f"Description: {row.description}" if row.description else "",
                        f"Source bucket: {row.source_bucket}",
                    ]
                    if part
                )
                return row.name, content
        raise ValueError(f"Unsupported library resource type: {resource_type}")

    def delete_attachment(
        self,
        attachment_id: str,
        *,
        session_factory: Any,
        user: str | None = None,
    ) -> bool:
        with session_factory() as db:
            row = db.get(CaliberAssistantAttachment, attachment_id)
            if row is None:
                return False
            if self._get_owned_session(db, row.session_id, user) is None:
                return False
            db.delete(row)
            db.commit()
            return True

    # ------------------------------------------------------------------
    # Message queue ("add to queue" + "steer")
    # ------------------------------------------------------------------

    def list_queue(
        self,
        session_id: str,
        *,
        session_factory: Any,
        user: str | None = None,
    ) -> list[QueuedMessageResponse]:
        with session_factory() as db:
            if self._get_owned_session(db, session_id, user) is None:
                return []
            rows = (
                db.query(CaliberAssistantQueuedMessage)
                .filter(
                    CaliberAssistantQueuedMessage.session_id == session_id,
                    CaliberAssistantQueuedMessage.status == "pending",
                )
                .order_by(
                    CaliberAssistantQueuedMessage.position,
                    CaliberAssistantQueuedMessage.created_at,
                )
                .all()
            )
            return [QueuedMessageResponse.model_validate(r) for r in rows]

    def enqueue_message(
        self,
        session_id: str,
        *,
        content: str,
        session_factory: Any,
        user: str,
        mode: str | None = None,
        kind: str = "queued",
    ) -> QueuedMessageResponse:
        clean = (content or "").strip()
        if not clean:
            raise ValueError("Queued message is empty.")
        if kind not in QUEUED_MESSAGE_KINDS:
            raise ValueError(f"Unknown queued-message kind: {kind}")
        resolved_mode = mode if mode in ASSISTANT_MODES else DEFAULT_ASSISTANT_MODE
        with session_factory() as db:
            if self._get_owned_session(db, session_id, user) is None:
                raise ValueError(f"Session {session_id} not found")
            pending = (
                db.query(CaliberAssistantQueuedMessage)
                .filter(
                    CaliberAssistantQueuedMessage.session_id == session_id,
                    CaliberAssistantQueuedMessage.status == "pending",
                )
                .all()
            )
            if len(pending) >= self._settings.max_queued_per_session:
                raise ConflictError(
                    "Queue limit reached for this session "
                    f"({self._settings.max_queued_per_session})."
                )
            positions = [p.position for p in pending]
            # Steer jumps to the front (lowest position); a queued item appends.
            if kind == "steer":
                position = (min(positions) - 1) if positions else 0
            else:
                position = (max(positions) + 1) if positions else 0
            row = CaliberAssistantQueuedMessage(
                queue_id=new_assistant_queued_message_id(),
                session_id=session_id,
                content=clean,
                mode=resolved_mode,
                kind=kind,
                position=position,
                status="pending",
                created_by=user or "",
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return QueuedMessageResponse.model_validate(row)

    def cancel_queued(
        self,
        queue_id: str,
        *,
        session_factory: Any,
        user: str | None = None,
    ) -> bool:
        with session_factory() as db:
            row = db.get(CaliberAssistantQueuedMessage, queue_id)
            if row is None:
                return False
            if self._get_owned_session(db, row.session_id, user) is None:
                return False
            db.delete(row)
            db.commit()
            return True

    def list_messages(
        self,
        session_id: str,
        *,
        session_factory: Any,
        user: str | None = None,
    ) -> list[MessageResponse]:
        with session_factory() as db:
            if self._get_owned_session(db, session_id, user) is None:
                return []
            rows = (
                db.query(CaliberAssistantMessage)
                .filter(CaliberAssistantMessage.session_id == session_id)
                .order_by(CaliberAssistantMessage.sequence_number)
                .all()
            )
            return [MessageResponse.model_validate(r) for r in rows]

    def _next_seq(self, db: Any, session_id: str) -> int:
        from sqlalchemy import func  # noqa: PLC0415

        result = (
            db.query(func.coalesce(func.max(CaliberAssistantMessage.sequence_number), -1))
            .filter(CaliberAssistantMessage.session_id == session_id)
            .scalar()
        )
        max_seq = int(result) if result is not None else -1
        return max_seq + 1

    def _persist_message(
        self,
        db: Any,
        *,
        session_id: str,
        role: str,
        content: str,
        metadata_: dict[str, Any] | None = None,
    ) -> CaliberAssistantMessage:
        seq = self._next_seq(db, session_id)
        msg = CaliberAssistantMessage(
            message_id=new_assistant_message_id(),
            session_id=session_id,
            role=role,
            content=content,
            metadata_=metadata_ or {},
            sequence_number=seq,
        )
        db.add(msg)
        return msg

    # ------------------------------------------------------------------
    # One-shot prompt drafting (the "Describe it" on-ramp)
    # ------------------------------------------------------------------

    def draft_prompt_from_description(
        self, description: str, *, max_turns: int = 5
    ) -> dict[str, Any]:
        """Drive the engine to a single prompt draft from a free-text task.

        This is deliberately a *seed*, not a separate publish path: the engine
        may open with a clarifying question, so we auto-advance with the
        description until it emits a prompt draft (or ``max_turns`` is hit). The
        returned ``template``/``variables``/``name`` are handed to the manual
        prompt builder, where the user runs the identical compose -> validate ->
        save -> calibrate steps as a hand-authored prompt.
        """
        clean = description.strip()
        session_id = new_assistant_session_id()
        history: list[dict[str, Any]] = []
        reply = ""
        for index in range(max(1, max_turns)):
            message = (
                clean
                if index == 0
                else "Use your best judgment from the description above and output the prompt now."
            )
            request = AssistantTurnRequest(
                session_id=session_id,
                user_message=message,
                history=list(history),
                artifact_type="prompt",
                goal=clean,
            )
            try:
                result = self._engine.run_turn(request)
            except Exception:
                logger.warning("assistant draft turn failed", exc_info=True)
                break
            if result.reply:
                reply = result.reply
            history.append({"role": "user", "content": message})
            history.append({"role": "assistant", "content": result.reply})
            for delta in result.draft_deltas:
                if (delta.artifact_type or "prompt") != "prompt":
                    continue
                artifact = delta.artifact or {}
                template = artifact.get("template")
                if isinstance(template, str) and template.strip():
                    variables = [
                        str(value) for value in artifact.get("variables", []) if str(value).strip()
                    ]
                    return {
                        "reply": reply,
                        "name": str(artifact.get("name") or "").strip(),
                        "template": template,
                        "variables": variables,
                        "summary": delta.summary,
                    }
            if result.done:
                break
        # No usable draft — return an empty template; the builder falls back to
        # seeding the description so the user still continues the manual flow.
        return {
            "reply": reply,
            "name": "",
            "template": "",
            "variables": [],
            "summary": "",
        }

    # ------------------------------------------------------------------
    # Send message (the main orchestration point)
    # ------------------------------------------------------------------

    def _build_agent_toolset(
        self,
        *,
        session_factory: Any,
        user: str,
        session_id: str,
        mode: str,
        approval_mode: str,
        project_id: str | None = None,
    ) -> AssistantAgentToolset:
        """Per-turn, context-bound tool surface for the engine's agentic loop.

        Draft gate methods are injected so ``agent_tools`` never imports this
        service; execution tools (preview/run/trace/eval/patch) call plain
        service-layer functions with ``session_factory`` + the runtime config.
        """
        deps = AgentToolDeps(
            session_factory=session_factory,
            config=self._runtime_config,
            validate_draft=self.validate_draft,
            test_draft=self.test_draft,
            approve_draft=self.approve_draft,
            publish_draft=self.publish_draft,
            get_draft=self.get_draft,
            list_drafts=self.list_drafts,
        )
        return AssistantAgentToolset(
            deps=deps,
            user=user,
            session_id=session_id,
            mode=mode,
            approval_mode=approval_mode,
            project_id=project_id,
        )

    def send_message(  # noqa: PLR0912, PLR0915
        self,
        session_id: str,
        body: MessageSendRequest,
        *,
        session_factory: Any,
        user: str,
        project_id: str | None = None,
        scopes: Sequence[str] | None = None,
        current_surface: str = "assistant_drawer",
    ) -> TurnResponse:
        # 1. Persist user message.
        trace_id = self._current_or_new_trace_id()
        task_context = None
        with session_factory() as db:
            session_row = self._get_owned_session(db, session_id, user)
            if session_row is None:
                raise ValueError(f"Session {session_id} not found")
            metadata, _workbench = self._metadata_and_workbench(session_row.metadata_)
            metadata = update_session_skill_runtime_metadata(metadata)
            correlation_id = self._ensure_correlation_id(metadata)
            raw_mode = body.mode or metadata.get("assistant_mode") or DEFAULT_ASSISTANT_MODE
            # ``metadata.get`` is ``Any`` and ``DEFAULT_ASSISTANT_MODE`` is a bare
            # ``str``; the membership check guarantees one of the three literals,
            # which the cast makes legible to the type checker.
            mode: AssistantMode = (
                cast(AssistantMode, raw_mode)
                if raw_mode in ASSISTANT_MODES
                else cast(AssistantMode, DEFAULT_ASSISTANT_MODE)
            )
            metadata["assistant_mode"] = mode
            approval_mode = (
                body.approval_mode
                or metadata.get("assistant_approval_mode")
                or DEFAULT_APPROVAL_MODE
            )
            if approval_mode not in ASSISTANT_APPROVAL_MODES:
                approval_mode = DEFAULT_APPROVAL_MODE
            metadata["assistant_approval_mode"] = approval_mode
            task_decision = self._task_manager.choose(
                mode=mode,
                explicit_task_kind=body.task_kind,
                resume_from_plan_id=body.resume_from_plan_id,
            )
            task_context = self._context_builder.build_turn_task_context(
                session_metadata=metadata,
                body=body,
                project_id=project_id,
                scopes=scopes,
                current_surface=current_surface,
                task_kind=task_decision.task_kind,
            )
            metadata = update_session_task_context_metadata(metadata, task_context)
            session_row.metadata_ = metadata
            session_goal = session_row.goal
            session_artifact_type = (
                str(metadata.get("artifact_type"))
                if isinstance(metadata.get("artifact_type"), str)
                else None
            )
            skill_playground = metadata.get("skill_playground") is True
            runtime_metadata = runtime_metadata_from_session(metadata)
            user_turns = (
                db.query(CaliberAssistantMessage)
                .filter(
                    CaliberAssistantMessage.session_id == session_id,
                    CaliberAssistantMessage.role == "user",
                )
                .count()
            )
            if user_turns >= self._settings.max_turns:
                raise ValueError("Assistant session has reached the configured turn limit")

            user_msg = self._persist_message(
                db,
                session_id=session_id,
                role="user",
                content=body.content,
                metadata_={"steer": True} if body.steer else None,
            )
            db.commit()
            db.refresh(user_msg)

        assert task_context is not None

        # 2. Load history + drafts for context.
        history = [
            {"role": m.role, "content": m.content}
            for m in self.list_messages(session_id, session_factory=session_factory, user=user)
        ]
        drafts_rows = self.list_drafts(session_id, session_factory=session_factory, user=user)
        drafts_ctx = [d.model_dump() for d in drafts_rows]
        attachment_rows = self.list_attachments(
            session_id, session_factory=session_factory, user=user
        )
        attachments_ctx = [
            {
                "name": a.name,
                "kind": a.kind,
                "ref_type": a.ref_type,
                "content_text": a.content_text,
                "truncated": a.truncated,
            }
            for a in attachment_rows
        ]
        skill_mode = body.skill_mode or str(runtime_metadata["mode"])
        if not self._settings.skill_runtime_enabled:
            skill_mode = "off"
        with session_factory() as db:
            skill_result = resolve_assistant_skills(
                db,
                AssistantSkillResolutionRequest(
                    user_message=body.content,
                    artifact_type=body.artifact_type or session_artifact_type,
                    session_goal=session_goal,
                    mode=skill_mode,
                    explicit_skill_names=normalize_skill_names(body.skill_names),
                    pinned_skill_names=normalize_skill_names(
                        runtime_metadata["pinned_skill_names"]
                    ),
                    disabled_skill_names=normalize_skill_names(
                        runtime_metadata["disabled_skill_names"]
                    ),
                ),
            )
        selected_skill_payloads = [asdict(skill) for skill in skill_result.skills]
        selected_skill_metadata = skill_result.metadata
        selected_skill_names = [skill.name for skill in skill_result.skills]

        # 3. Persist run start.
        run_id = new_assistant_run_id()
        with session_factory() as db:
            run_row = CaliberAssistantRun(
                run_id=run_id,
                session_id=session_id,
                status="running",
                engine=self._engine.__class__.__name__,
                input_summary=body.content[:500],
                trace_id=trace_id,
            )
            db.add(run_row)
            db.commit()

        # 4. Call engine.
        turn_span: AssistantTraceSpan | None = None
        try:
            with self._trace_span(
                "caliber.assistant.send_message",
                trace_id=trace_id,
                correlation_id=correlation_id,
                user=user,
                attributes={
                    "caliber.assistant.session_id": session_id,
                    "caliber.assistant.run_id": run_id,
                    "caliber.assistant.engine": self._engine.__class__.__name__,
                    "caliber.assistant.artifact_type": body.artifact_type or "",
                    "caliber.assistant.history_count": len(history),
                    "caliber.assistant.draft_count": len(drafts_ctx),
                    "caliber.assistant.content_bytes": len(body.content.encode("utf-8")),
                    "caliber.assistant.skill_runtime.mode": skill_mode,
                    "caliber.assistant.skill_runtime.selected_count": len(selected_skill_names),
                    "caliber.assistant.skill_runtime.skill_names": selected_skill_names,
                    "caliber.assistant.skill_runtime.warning_count": len(skill_result.warnings),
                    "caliber.assistant.mode": mode,
                    "caliber.assistant.attachment_count": len(attachments_ctx),
                    "caliber.assistant.steer": body.steer,
                    "caliber.assistant.approval_mode": approval_mode,
                    "caliber.assistant.project_id": task_context.project_id or "",
                    "caliber.assistant.task_kind": task_context.task_kind or "",
                    "caliber.assistant.current_surface": task_context.current_surface,
                    "caliber.assistant.context_ref_count": len(task_context.context_refs),
                    "caliber.assistant.selected_resource_count": len(
                        task_context.selected_resources
                    ),
                    "caliber.assistant.done_when_count": len(task_context.done_when),
                    "caliber.assistant.resume_from_plan_id": task_context.resume_from_plan_id or "",
                },
            ) as turn_span:
                request = AssistantTurnRequest(
                    session_id=session_id,
                    user_message=body.content,
                    history=history,
                    drafts=drafts_ctx,
                    artifact_type=body.artifact_type,
                    goal=session_goal,
                    selected_skills=selected_skill_payloads,
                    skill_runtime_mode=skill_mode,  # type: ignore[arg-type]
                    skill_playground=skill_playground,
                    mode=mode,
                    attachments=attachments_ctx,
                    steer=body.steer,
                    user=user,
                    approval_mode=approval_mode,  # type: ignore[arg-type]
                    task_context=task_context,
                )
                toolset = self._build_agent_toolset(
                    session_factory=session_factory,
                    user=user,
                    session_id=session_id,
                    mode=mode,
                    approval_mode=approval_mode,
                    project_id=task_context.project_id,
                )
                executor = ThreadPoolExecutor(max_workers=1)
                future = executor.submit(lambda: self._engine.run_turn(request, toolset=toolset))
                try:
                    result = future.result(timeout=self._settings.run_timeout_seconds)
                except FuturesTimeoutError as exc:
                    future.cancel()
                    raise TimeoutError("Assistant run timed out") from exc
                finally:
                    executor.shutdown(wait=False, cancel_futures=True)
                turn_span.set_attribute("caliber.assistant.question_count", len(result.questions))
                turn_span.set_attribute(
                    "caliber.assistant.draft_delta_count", len(result.draft_deltas)
                )
        except Exception as exc:
            with session_factory() as db:
                run_row = db.get(CaliberAssistantRun, run_id)
                if run_row:
                    run_row.status = "failed"
                    run_row.error = str(exc)[:2000]
                    run_row.mlflow_run_id = self._span_value(turn_span, field_name="mlflow_run_id")
                    run_row.completed_at = datetime.now(timezone.utc)
                err_msg = self._persist_message(
                    db,
                    session_id=session_id,
                    role="assistant",
                    content=f"An error occurred: {type(exc).__name__}",
                    metadata_={
                        "error": True,
                        "selected_skills": selected_skill_metadata,
                        "skill_runtime_mode": skill_mode,
                        "skill_runtime_warnings": list(skill_result.warnings),
                        "process_steps": _assistant_process_steps(
                            questions=[],
                            tool_calls=[],
                            draft_updates=[],
                            approval_mode=approval_mode,
                            error=True,
                        ),
                    },
                )
                session_row = db.get(CaliberAssistantSession, session_id)
                if session_row is not None:
                    session_row.metadata_ = update_session_skill_runtime_metadata(
                        session_row.metadata_,
                        last_selected_skills=selected_skill_metadata,
                    )
                db.commit()
                db.refresh(err_msg)
                db.refresh(run_row)
                return TurnResponse(
                    assistant_message=MessageResponse.model_validate(err_msg),
                    run=RunResponse.model_validate(run_row) if run_row else None,
                )

        # 5. Persist assistant response, draft updates, and run completion.
        with session_factory() as db:
            questions = result.questions[: self._settings.max_questions_per_turn]
            draft_responses: list[DraftResponse] = []
            # Drafts are an authoring artifact: only materialize them in "build"
            # mode. In chat / plan mode the reply text stands on its own.
            draft_deltas = result.draft_deltas if mode == "build" else []
            for delta in draft_deltas:
                if not delta.draft_id:
                    draft_count = (
                        db.query(CaliberAssistantDraft)
                        .filter(CaliberAssistantDraft.session_id == session_id)
                        .count()
                    )
                    if draft_count >= self._settings.max_drafts_per_session:
                        continue
                draft_resp = self._apply_draft_delta(
                    db,
                    session_id=session_id,
                    delta=delta,
                    user=user,
                )
                draft_responses.append(draft_resp)

            asst_msg = self._persist_message(
                db,
                session_id=session_id,
                role="assistant",
                content=result.reply,
                metadata_={
                    "questions": [q.model_dump() for q in questions],
                    "selected_skills": selected_skill_metadata,
                    "skill_runtime_mode": skill_mode,
                    "skill_runtime_warnings": list(skill_result.warnings),
                    "tool_calls": [tc.model_dump(mode="json") for tc in result.tool_calls],
                    # The engine can return an error it handled internally (reply +
                    # error) rather than raising — surface it so the turn isn't
                    # silently presented as fully successful.
                    "error": bool(result.error),
                    "process_steps": _assistant_process_steps(
                        questions=questions,
                        tool_calls=list(result.tool_calls),
                        draft_updates=draft_responses,
                        approval_mode=approval_mode,
                        error=bool(result.error),
                    ),
                },
            )
            session_row = db.get(CaliberAssistantSession, session_id)
            if session_row is not None:
                session_row.metadata_ = update_session_skill_runtime_metadata(
                    session_row.metadata_,
                    last_selected_skills=selected_skill_metadata,
                )

            run_row = db.get(CaliberAssistantRun, run_id)
            if run_row:
                # An engine-handled error (returned, not raised) marks the run
                # failed with its message rather than silently "completed".
                if result.error:
                    run_row.status = "failed"
                    run_row.error = result.error[:2000]
                else:
                    run_row.status = "completed"
                run_row.output_summary = result.reply[:500]
                run_row.trace_id = result.trace_id or trace_id
                run_row.mlflow_run_id = self._span_value(turn_span, field_name="mlflow_run_id")
                run_row.completed_at = datetime.now(timezone.utc)
                if draft_responses:
                    run_row.draft_id = draft_responses[-1].draft_id

            db.commit()
            db.refresh(asst_msg)
            if run_row:
                db.refresh(run_row)

            turn = TurnResponse(
                assistant_message=MessageResponse.model_validate(asst_msg),
                questions=questions,
                draft_updates=draft_responses,
                run=RunResponse.model_validate(run_row) if run_row else None,
                tool_calls=list(result.tool_calls),
            )

        # 6. Approval mode: auto-advance freshly created drafts through the
        # validate -> test -> approve -> publish gates per the session policy.
        # Runs outside the turn transaction because each gate manages its own
        # session; the refreshed drafts replace the turn's draft_updates.
        if mode == "build" and approval_mode != "manual" and turn.draft_updates:
            advanced = self._auto_advance_drafts(
                [d.draft_id for d in turn.draft_updates if d.status == "draft"],
                approval_mode=approval_mode,
                session_factory=session_factory,
                user=user,
            )
            if advanced:
                turn.draft_updates = advanced
                with session_factory() as db:
                    msg_row = db.get(CaliberAssistantMessage, turn.assistant_message.message_id)
                    if msg_row is not None:
                        metadata = (
                            copy.deepcopy(msg_row.metadata_)
                            if isinstance(msg_row.metadata_, dict)
                            else {}
                        )
                        metadata["process_steps"] = _assistant_process_steps(
                            questions=turn.questions,
                            tool_calls=turn.tool_calls,
                            draft_updates=turn.draft_updates,
                            approval_mode=approval_mode,
                        )
                        msg_row.metadata_ = metadata
                        db.commit()
                        db.refresh(msg_row)
                        turn.assistant_message = MessageResponse.model_validate(msg_row)
        return turn

    def _auto_advance_drafts(
        self,
        draft_ids: list[str],
        *,
        approval_mode: str,
        session_factory: Any,
        user: str,
    ) -> list[DraftResponse]:
        """Advance freshly created drafts through the approval gates.

        ``auto_safe`` validates then tests; ``auto_all`` additionally approves and
        publishes a draft that passes both. Each gate manages its own transaction,
        and a failure (or a failed gate) stops the chain for that draft without
        raising — the turn must never break because auto-advance stumbled.
        """
        refreshed: list[DraftResponse] = []
        for draft_id in draft_ids:
            try:
                report = self.validate_draft(draft_id, session_factory=session_factory, user=user)
                if report.valid:
                    test_report = self.test_draft(
                        draft_id, session_factory=session_factory, user=user
                    )
                    if test_report.passed and approval_mode == "auto_all":
                        approved = self.approve_draft(
                            draft_id, session_factory=session_factory, user=user
                        )
                        if approved is not None:
                            self.publish_draft(draft_id, session_factory=session_factory, user=user)
            except Exception:
                logger.exception("assistant auto-advance failed for draft %s", draft_id)
            current = self.get_draft(draft_id, session_factory=session_factory, user=user)
            if current is not None:
                refreshed.append(current)
        return refreshed

    def _apply_draft_delta(
        self,
        db: Any,
        *,
        session_id: str,
        delta: DraftDelta,
        user: str,
    ) -> DraftResponse:
        if delta.draft_id:
            row = db.get(CaliberAssistantDraft, delta.draft_id)
            if row:
                if delta.title:
                    row.title = delta.title
                if delta.summary:
                    row.summary = delta.summary
                if delta.spec:
                    row.spec = {**row.spec, **delta.spec}
                if delta.artifact:
                    row.artifact = {**row.artifact, **delta.artifact}
                row.version += 1
                row.updated_by = user
                db.flush()
                return DraftResponse.model_validate(row)

        draft_id = new_assistant_draft_id()
        artifact_type = delta.artifact_type or "tool"
        row = CaliberAssistantDraft(
            draft_id=draft_id,
            session_id=session_id,
            artifact_type=artifact_type,
            title=delta.title,
            summary=delta.summary,
            spec=delta.spec,
            artifact=delta.artifact,
            created_by=user,
            updated_by=user,
        )
        db.add(row)
        db.flush()
        return DraftResponse.model_validate(row)

    # ------------------------------------------------------------------
    # Drafts
    # ------------------------------------------------------------------

    def list_drafts(
        self,
        session_id: str,
        *,
        session_factory: Any,
        user: str | None = None,
    ) -> list[DraftResponse]:
        with session_factory() as db:
            if self._get_owned_session(db, session_id, user) is None:
                return []
            rows = (
                db.query(CaliberAssistantDraft)
                .filter(CaliberAssistantDraft.session_id == session_id)
                .order_by(CaliberAssistantDraft.created_at)
                .all()
            )
            return [DraftResponse.model_validate(r) for r in rows]

    def get_draft(
        self,
        draft_id: str,
        *,
        session_factory: Any,
        user: str | None = None,
    ) -> DraftResponse | None:
        with session_factory() as db:
            row = db.get(CaliberAssistantDraft, draft_id)
            if row is None:
                return None
            if user is not None:
                session_row = self._get_owned_session(db, row.session_id, user)
                if session_row is None:
                    return None
            return DraftResponse.model_validate(row)

    def update_draft(
        self,
        draft_id: str,
        body: DraftUpdateRequest,
        *,
        session_factory: Any,
        user: str,
    ) -> DraftResponse | None:
        with session_factory() as db:
            row = db.get(CaliberAssistantDraft, draft_id)
            if row is None:
                return None
            if self._get_owned_session(db, row.session_id, user) is None:
                return None
            if row.version != body.version:
                raise ConflictError(
                    f"Draft version mismatch: expected {row.version}, got {body.version}",
                )
            if body.title is not None:
                row.title = body.title
            if body.summary is not None:
                row.summary = body.summary
            if body.spec is not None:
                row.spec = body.spec
            if body.artifact is not None:
                row.artifact = body.artifact
            row.version += 1
            row.updated_by = user
            db.commit()
            db.refresh(row)
            return DraftResponse.model_validate(row)

    # ------------------------------------------------------------------
    # Validate / Test
    # ------------------------------------------------------------------

    def validate_draft(
        self,
        draft_id: str,
        *,
        session_factory: Any,
        user: str | None = None,
        max_source_bytes: int | None = None,
    ) -> ValidationReport:
        with session_factory() as db:
            row = db.get(CaliberAssistantDraft, draft_id)
            if row is None:
                return ValidationReport(valid=False, errors=["Draft not found."])
            if self._get_owned_session(db, row.session_id, user) is None:
                return ValidationReport(valid=False, errors=["Draft not found."])
            row.status = "validating"
            db.commit()

            report = validate_draft(
                row.artifact_type,
                row.artifact,
                max_source_bytes=max_source_bytes or self._settings.tool_source_max_bytes,
            )

            row.validation_report = report.model_dump()
            row.status = "validated" if report.valid else "validation_failed"
            db.commit()
            return report

    def test_draft(
        self,
        draft_id: str,
        *,
        session_factory: Any,
        user: str | None = None,
    ) -> TestReport:
        with session_factory() as db:
            row = db.get(CaliberAssistantDraft, draft_id)
            if row is None:
                return TestReport(passed=False, error="Draft not found.")
            if self._get_owned_session(db, row.session_id, user) is None:
                return TestReport(passed=False, error="Draft not found.")
            row.status = "testing"
            db.commit()

            if (
                row.artifact_type == "tool"
                and isinstance(row.artifact.get("tests"), list)
                and row.artifact["tests"]
            ):
                report = self._run_tool_sandbox_tests(row.artifact)
            elif row.artifact_type == "skill":
                report = self._run_skill_package_tests(row.artifact)
            elif row.artifact_type == "workflow":
                report = self._run_workflow_compile_tests(row.artifact)
            elif row.artifact_type == "mcp_server":
                report = self._run_mcp_connection_tests(row.artifact)
            else:
                report = self._run_structural_tests(row.artifact_type, row.artifact)

            row.test_report = report.model_dump()
            row.status = "tested" if report.passed else "test_failed"
            db.commit()
            return report

    def _run_structural_tests(
        self,
        artifact_type: str,
        artifact: dict[str, Any],
    ) -> TestReport:
        """Basic structural tests for drafts without runnable test cases."""
        failures: list[dict[str, Any]] = []

        if not artifact:
            return TestReport(
                passed=False, total=1, failures=1, details=[{"test": "non_empty", "passed": False}]
            )

        if artifact_type == "tool" and artifact.get("source"):
            import ast as _ast  # noqa: PLC0415

            try:
                _ast.parse(artifact["source"])
            except SyntaxError as exc:
                failures.append({"test": "syntax", "passed": False, "error": str(exc)})

        total = max(1, 1 + len(failures))
        return TestReport(
            passed=len(failures) == 0,
            total=total,
            failures=len(failures),
            details=failures or [{"test": "structural", "passed": True}],
        )

    # ------------------------------------------------------------------
    # Approve / Publish
    # ------------------------------------------------------------------

    def approve_draft(
        self,
        draft_id: str,
        *,
        session_factory: Any,
        user: str,
    ) -> DraftResponse | None:
        with session_factory() as db:
            row = db.get(CaliberAssistantDraft, draft_id)
            if row is None:
                return None
            if self._get_owned_session(db, row.session_id, user) is None:
                return None
            row.status = "approved"
            row.updated_by = user
            db.commit()
            db.refresh(row)
            return DraftResponse.model_validate(row)

    def _prompt_alias_publish_policy(  # noqa: PLR0911
        self,
        db: Any,
        *,
        draft_id: str,
        artifact: dict[str, Any],
    ) -> tuple[bool, dict[str, Any]]:
        target_alias = str(artifact.get("target_alias") or "").strip()
        if not target_alias:
            return (
                True,
                {
                    "passed": True,
                    "requires_approval": False,
                    "checks": ["no_alias_requested"],
                },
            )

        clean_name = str(artifact.get("name") or "").strip()
        clean_template = str(artifact.get("template") or "").strip()
        base_report: dict[str, Any] = {
            "passed": False,
            "requires_approval": self._settings.publish_requires_approval,
            "target_alias": target_alias,
            "prompt_name": clean_name,
            "checks": [],
        }

        if not self._settings.publish_requires_approval:
            base_report.update({"passed": True, "checks": ["approval_policy_disabled"]})
            return True, base_report

        approval_id = str(
            artifact.get("approval_id") or artifact.get("policy_approval_id") or ""
        ).strip()
        base_report["approval_id"] = approval_id
        if not approval_id:
            base_report["reason"] = "Prompt alias publish requires an approved promotion approval."
            return False, base_report

        approval = db.get(CaliberApprovalRequest, approval_id)
        if approval is None:
            base_report["reason"] = f"Approval {approval_id!r} was not found."
            return False, base_report
        base_report["approval_status"] = approval.status
        if approval.status != "approved":
            base_report["reason"] = (
                f"Approval {approval_id!r} is not approved (current: {approval.status})."
            )
            return False, base_report

        candidate = approval.candidate_snapshot or {}
        if not isinstance(candidate, dict):
            candidate = {}
        candidate_name = str(
            candidate.get("prompt_name") or candidate.get("name") or approval.agent_id or ""
        ).strip()
        if candidate_name != clean_name:
            base_report["reason"] = (
                f"Approval {approval_id!r} targets prompt {candidate_name!r}, not {clean_name!r}."
            )
            return False, base_report

        candidate_alias = str(candidate.get("target_alias") or "").strip()
        if candidate_alias != target_alias:
            base_report["reason"] = (
                f"Approval {approval_id!r} targets alias {candidate_alias!r}, not {target_alias!r}."
            )
            return False, base_report

        candidate_draft_id = str(
            candidate.get("assistant_draft_id") or candidate.get("draft_id") or ""
        ).strip()
        candidate_hash = str(
            candidate.get("template_hash") or candidate.get("content_hash") or ""
        ).strip()
        candidate_template = candidate.get("template")
        if not isinstance(candidate_template, str):
            candidate_template = candidate.get("content")
        content_matches = (
            bool(candidate_draft_id and candidate_draft_id == draft_id)
            or bool(candidate_hash and candidate_hash == _content_hash(clean_template))
            or bool(
                isinstance(candidate_template, str) and candidate_template.strip() == clean_template
            )
        )
        if not content_matches:
            base_report["reason"] = (
                f"Approval {approval_id!r} does not match draft {draft_id!r} "
                "or the draft template content."
            )
            return False, base_report

        base_report.update(
            {
                "passed": True,
                "checks": [
                    "approval_status",
                    "prompt_name",
                    "target_alias",
                    "draft_or_content_match",
                ],
            }
        )
        return True, base_report

    def publish_draft(
        self,
        draft_id: str,
        *,
        session_factory: Any,
        user: str,
    ) -> dict[str, Any]:
        trace_id = self._current_or_new_trace_id()
        correlation_id = ""
        session_id = ""
        artifact_type = ""
        artifact: dict[str, Any] = {}
        publish_policy: dict[str, Any] = {
            "passed": True,
            "requires_approval": False,
            "checks": [],
        }
        with session_factory() as db:
            row = db.get(CaliberAssistantDraft, draft_id)
            if row is None:
                return {"success": False, "error": "Draft not found."}
            session_row = self._get_owned_session(db, row.session_id, user)
            if session_row is None:
                return {"success": False, "error": "Draft not found."}
            if row.status != "approved":
                return {
                    "success": False,
                    "error": f"Draft must be approved before publishing (current: {row.status}).",
                }

            metadata, _workbench = self._metadata_and_workbench(session_row.metadata_)
            correlation_id = self._ensure_correlation_id(metadata)
            session_row.metadata_ = metadata
            session_id = row.session_id
            artifact_type = row.artifact_type
            artifact = copy.deepcopy(row.artifact)
            spec = row.spec if isinstance(row.spec, dict) else {}
            if artifact_type == "prompt":
                policy_ok, publish_policy = self._prompt_alias_publish_policy(
                    db,
                    draft_id=draft_id,
                    artifact=artifact,
                )
                if not policy_ok:
                    report = {
                        "success": False,
                        "error": publish_policy.get(
                            "reason",
                            "Draft publish blocked by assistant publish policy.",
                        ),
                        "type": artifact_type,
                        "trace_id": trace_id,
                        "correlation_id": correlation_id,
                        "policy": publish_policy,
                        "dependency_checks": {
                            "passed": False,
                            "checks": ["publish_policy"],
                        },
                        "impact_checks": {
                            "passed": False,
                            "checks": ["prompt_alias_publish"],
                        },
                        "rollback_metadata": {
                            "available": False,
                            "checkpoint_ids": [],
                        },
                    }
                    audit_record(
                        db,
                        actor=user,
                        action="publish_draft",
                        entity_type="assistant_draft",
                        entity_id=draft_id,
                        details={
                            "source": "caliber-assistant",
                            "session_id": session_id,
                            "plan_id": spec.get("plan_id"),
                            "operation_id": spec.get("operation_id"),
                            "artifact_type": artifact_type,
                            "success": False,
                            "target_artifact": artifact.get("name"),
                            "target_registry_id": None,
                            "target_version": None,
                            "trace_id": trace_id,
                            "correlation_id": correlation_id,
                            "policy": publish_policy,
                            "dependency_checks": report["dependency_checks"],
                            "impact_checks": report["impact_checks"],
                            "rollback_metadata": report["rollback_metadata"],
                            "error": report["error"],
                        },
                    )
                    db.commit()
                    return report
            row.status = "publishing"
            db.commit()

        report = self._publisher.publish(
            artifact_type=artifact_type,
            artifact=artifact,
            draft_id=draft_id,
            session_factory=session_factory,
            user=user,
        )
        report = dict(report)
        report.setdefault("trace_id", trace_id)
        report.setdefault("correlation_id", correlation_id)
        report.setdefault("policy", publish_policy)
        report.setdefault(
            "dependency_checks",
            {"passed": bool(report.get("success")), "checks": []},
        )
        report.setdefault("impact_checks", {"passed": True, "checks": []})
        report.setdefault(
            "rollback_metadata",
            {"available": False, "checkpoint_ids": []},
        )

        with session_factory() as db:
            row = db.get(CaliberAssistantDraft, draft_id)
            if row is None:
                return report
            spec = row.spec if isinstance(row.spec, dict) else {}

            if report.get("success"):
                row.status = "published"
                row.target_registry_id = report.get("registry_id")
                event = CaliberAssistantPublishEvent(
                    event_id=new_assistant_publish_id(),
                    draft_id=draft_id,
                    artifact_type=row.artifact_type,
                    target_registry_id=report.get("registry_id", ""),
                    target_version=report.get("version_id") or report.get("target_version"),
                    approved_by=user,
                    published_by=user,
                    publish_report=report,
                )
                db.add(event)
            else:
                row.status = "publish_failed"

            audit_record(
                db,
                actor=user,
                action="publish_draft",
                entity_type="assistant_draft",
                entity_id=draft_id,
                details={
                    "source": "caliber-assistant",
                    "session_id": session_id,
                    "plan_id": spec.get("plan_id"),
                    "operation_id": spec.get("operation_id"),
                    "artifact_type": artifact_type,
                    "success": bool(report.get("success")),
                    "target_artifact": report.get("registry_id"),
                    "target_registry_id": report.get("registry_id"),
                    "target_version": report.get("version_id") or report.get("target_version"),
                    "trace_id": trace_id,
                    "correlation_id": correlation_id,
                    "policy": report.get("policy"),
                    "dependency_checks": report.get("dependency_checks"),
                    "impact_checks": report.get("impact_checks"),
                    "rollback_metadata": report.get("rollback_metadata"),
                    "error": report.get("error"),
                },
            )
            db.commit()
        return report

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def get_run(
        self,
        run_id: str,
        *,
        session_factory: Any,
        user: str | None = None,
    ) -> RunResponse | None:
        with session_factory() as db:
            row = db.get(CaliberAssistantRun, run_id)
            if row is None:
                return None
            if self._get_owned_session(db, row.session_id, user) is None:
                return None
            return RunResponse.model_validate(row)


class ConflictError(Exception):
    """Raised when an optimistic-concurrency check fails (HTTP 409)."""
