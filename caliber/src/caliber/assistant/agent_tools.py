"""Aria's agentic tool surface — the closed run → observe → fix loop.

Unlike the read-only :class:`~caliber.assistant.tools.RegistryToolDispatcher`,
this toolset is built **per turn** and bound to the acting user, session, and
approval policy, and it can *execute* and *observe* real CALIBER capabilities:
read workflow runs + their MLflow traces, validate/test drafts, preview-run a
workflow (sandboxed), enqueue a real run, run a quick eval, propose a fix patch,
while approval and publication remain human-gated — so the model can iterate
within a turn without crossing the release boundary autonomously.

Permissioning is non-interactive (a synchronous turn can't pause for a human
click): the toolset only *exposes* tools allowed by the current
``mode`` x ``approval_mode``, mirroring a code assistant's permission modes:

* read tools  — available in every mode.
* safe tools  — build mode + every non-manual approval policy.
* mutate tools — build mode + ``auto_all``/``full_autonomy``.
* gated tools — never exposed to a synchronous turn (approve / publish / deploy).

Capabilities are called as plain service-layer functions (``session_factory`` +
``config`` only); draft gate methods are injected by the service so this module
never imports :class:`AssistantService`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from caliber.assistant.capabilities import (
    TIER_GATED,
    TIER_MUTATE,
    TIER_READ,
    TIER_SAFE,
    CapabilityContext,
    capability_by_tool_name,
    registered_capabilities,
)
from caliber.assistant.tools import _fn

logger = logging.getLogger(__name__)

_MAX_ROWS = 25
_MAX_TRACE_SPANS = 40
_MAX_QUICK_EVAL_EXAMPLES = 5
_MAX_DATASET_EXAMPLES = 50  # cap on examples a single create_eval_dataset call persists
_MAX_EVAL_EXAMPLES = 20  # cap on examples scored inline per evaluate_* call (turn budget)

# Tool risk tiers are single-sourced in caliber.assistant.capabilities (imported
# above) so the hand-written tools below and the capability-registry projection
# share one definition. TIER_GATED is never auto-exposed (see _tier_allowed).


def _ok(data: Any) -> str:
    return json.dumps({"ok": True, "data": data}, default=str)[:8000]


def _err(message: str) -> str:
    return json.dumps({"error": message})


# Per-tool (spec, tier). Specs follow the OpenAI function-schema shape via ``_fn``.
def _build_tool_specs() -> dict[str, tuple[dict[str, Any], str]]:
    s: dict[str, tuple[dict[str, Any], str]] = {}
    # ---- read ----
    s["list_skills"] = (
        _fn("list_skills", "List active skills (name, summary, category)."),
        TIER_READ,
    )
    s["get_skill"] = (
        _fn(
            "get_skill",
            "Get a skill's full content by exact name.",
            {"name": {"type": "string"}},
            ["name"],
        ),
        TIER_READ,
    )
    s["list_tools"] = (_fn("list_tools", "List registered tools (name, description)."), TIER_READ)
    s["list_workflows"] = (
        _fn("list_workflows", "List workflows in CALIBER (id, name, status)."),
        TIER_READ,
    )
    s["get_workflow_manifest"] = (
        _fn(
            "get_workflow_manifest",
            "Get a workflow version's manifest (node graph). Latest version if version_number omitted.",
            {
                "workflow_id": {"type": "string"},
                "version_number": {"type": "integer", "description": "Optional specific version."},
            },
            ["workflow_id"],
        ),
        TIER_READ,
    )
    s["list_workflow_runs"] = (
        _fn(
            "list_workflow_runs",
            "List recent workflow runs (id, status, trace_id).",
            {
                "workflow_id": {"type": "string", "description": "Optional filter."},
                "limit": {"type": "integer"},
            },
        ),
        TIER_READ,
    )
    s["get_workflow_run"] = (
        _fn(
            "get_workflow_run",
            "Get one workflow run's status, error, and summary.",
            {"run_id": {"type": "string"}},
            ["run_id"],
        ),
        TIER_READ,
    )
    s["get_workflow_run_trace"] = (
        _fn(
            "get_workflow_run_trace",
            "Get a workflow run's MLflow trace as a span tree — inspect what actually happened.",
            {"run_id": {"type": "string"}},
            ["run_id"],
        ),
        TIER_READ,
    )
    s["list_session_drafts"] = (
        _fn("list_session_drafts", "List the drafts in the current session."),
        TIER_READ,
    )
    s["get_draft"] = (
        _fn(
            "get_draft",
            "Get one draft (artifact + status + reports) by id.",
            {"draft_id": {"type": "string"}},
            ["draft_id"],
        ),
        TIER_READ,
    )
    s["list_knowledge_bases"] = (
        _fn(
            "list_knowledge_bases",
            "List knowledge bases (id, name, status, active version, last run, calibration baseline).",
        ),
        TIER_READ,
    )
    s["get_knowledge_base"] = (
        _fn(
            "get_knowledge_base",
            "Get one knowledge base with its versions.",
            {"knowledge_base_id": {"type": "string"}},
            ["knowledge_base_id"],
        ),
        TIER_READ,
    )
    s["get_knowledge_base_calibration"] = (
        _fn(
            "get_knowledge_base_calibration",
            "Read a KB's recent calibration runs (Recall@k / nDCG@k / Faithfulness / Answer-correctness).",
            {"knowledge_base_id": {"type": "string"}},
            ["knowledge_base_id"],
        ),
        TIER_READ,
    )
    s["preview_skill_selection"] = (
        _fn(
            "preview_skill_selection",
            "Show which active skills Aria's deterministic selector would activate for a query.",
            {
                "query": {"type": "string"},
                "artifact_type": {"type": "string", "description": "Optional artifact-type hint."},
            },
            ["query"],
        ),
        TIER_READ,
    )
    # ---- safe ----
    s["query_knowledge_base"] = (
        _fn(
            "query_knowledge_base",
            "Run a retrieval query against a KB's active version and return the retrieved chunks "
            "(and answer). The KB analog of preview-run — use it to inspect retrieval quality.",
            {
                "knowledge_base_id": {"type": "string"},
                "question": {"type": "string"},
                "top_k": {"type": "integer"},
                "retrieval_mode": {
                    "type": "string",
                    "description": "dense | hybrid | graph_hybrid | age_graph (default dense).",
                },
            },
            ["knowledge_base_id", "question"],
        ),
        TIER_SAFE,
    )
    s["run_tool_sandbox"] = (
        _fn(
            "run_tool_sandbox",
            "Invoke a tool DRAFT's source with one input in the isolated subprocess sandbox and "
            "return its output — use to observe what the tool actually does.",
            {
                "draft_id": {"type": "string"},
                "input": {
                    "type": "object",
                    "description": "Keyword arguments for the tool callable.",
                },
            },
            ["draft_id"],
        ),
        TIER_SAFE,
    )
    s["create_eval_dataset"] = (
        _fn(
            "create_eval_dataset",
            "Create a reusable eval dataset from examples you assemble from the conversation / "
            "attachments. Each example is {input, expected} (objects or strings).",
            {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "examples": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "List of {input, expected} pairs.",
                },
            },
            ["name", "examples"],
        ),
        TIER_SAFE,
    )
    s["evaluate_tool_draft"] = (
        _fn(
            "evaluate_tool_draft",
            "Run a tool DRAFT over a dataset's inputs (sandboxed) and score outputs vs expected.",
            {
                "draft_id": {"type": "string"},
                "dataset_id": {"type": "string"},
                "scorers": {"type": "array", "items": {"type": "string"}},
                "max_examples": {"type": "integer"},
            },
            ["draft_id", "dataset_id"],
        ),
        TIER_SAFE,
    )
    s["evaluate_workflow_draft"] = (
        _fn(
            "evaluate_workflow_draft",
            "Replay a workflow DRAFT over a dataset (sandboxed) and return structural quality / "
            "tool-adherence / completion / safety scores per the examples' expectations.",
            {
                "draft_id": {"type": "string"},
                "dataset_id": {"type": "string"},
                "max_examples": {"type": "integer"},
            },
            ["draft_id", "dataset_id"],
        ),
        TIER_SAFE,
    )
    s["evaluate_skill_selection"] = (
        _fn(
            "evaluate_skill_selection",
            "Check, per dataset example, whether the expected skill (example.expected.skill) is the "
            "one Aria's selector activates for the input — returns selection accuracy.",
            {"dataset_id": {"type": "string"}, "max_examples": {"type": "integer"}},
            ["dataset_id"],
        ),
        TIER_SAFE,
    )
    s["validate_draft"] = (
        _fn(
            "validate_draft",
            "Validate a draft artifact's structure.",
            {"draft_id": {"type": "string"}},
            ["draft_id"],
        ),
        TIER_SAFE,
    )
    s["test_draft"] = (
        _fn(
            "test_draft",
            "Run a draft's tests (sandboxed tool / skill render / workflow compile).",
            {"draft_id": {"type": "string"}},
            ["draft_id"],
        ),
        TIER_SAFE,
    )
    s["preview_workflow_draft"] = (
        _fn(
            "preview_workflow_draft",
            "Sandbox-run a workflow DRAFT on one input and return what each node did. "
            "No real side effects — use this to see if the workflow wires up and routes correctly.",
            {"draft_id": {"type": "string"}, "input_text": {"type": "string"}},
            ["draft_id", "input_text"],
        ),
        TIER_SAFE,
    )
    s["preview_workflow_version"] = (
        _fn(
            "preview_workflow_version",
            "Sandbox-run an existing workflow VERSION on one input (no real side effects).",
            {"version_id": {"type": "string"}, "input_text": {"type": "string"}},
            ["version_id", "input_text"],
        ),
        TIER_SAFE,
    )
    s["run_quick_eval"] = (
        _fn(
            "run_quick_eval",
            "Quick-score given prompt instructions against a small sample of an eval dataset.",
            {
                "dataset_id": {"type": "string"},
                "instructions": {
                    "type": "string",
                    "description": "Prompt/system instructions under test.",
                },
                "scorers": {"type": "array", "items": {"type": "string"}},
                "max_examples": {"type": "integer"},
            },
            ["dataset_id", "instructions"],
        ),
        TIER_SAFE,
    )
    s["propose_workflow_patch"] = (
        _fn(
            "propose_workflow_patch",
            "From failure evidence, propose a deterministic fix patch for a workflow draft's manifest.",
            {
                "draft_id": {"type": "string"},
                "evidence": {
                    "type": "object",
                    "description": "Failure signals: category, node_id, required_tools, observed_tool_calls, wrong_handoff, etc.",
                },
            },
            ["draft_id", "evidence"],
        ),
        TIER_SAFE,
    )
    # ---- mutate ----
    s["run_workflow"] = (
        _fn(
            "run_workflow",
            "Enqueue a REAL (non-sandboxed) run of a workflow version. Returns a run_id to observe.",
            {"version_id": {"type": "string"}, "input_text": {"type": "string"}},
            ["version_id", "input_text"],
        ),
        TIER_MUTATE,
    )
    s["approve_draft"] = (
        _fn(
            "approve_draft",
            "Approve a tested draft (pre-publish gate).",
            {"draft_id": {"type": "string"}},
            ["draft_id"],
        ),
        TIER_GATED,
    )
    s["publish_draft"] = (
        _fn(
            "publish_draft",
            "Publish an approved draft to the artifact registry.",
            {"draft_id": {"type": "string"}},
            ["draft_id"],
        ),
        TIER_GATED,
    )
    return s


_TOOL_SPECS = _build_tool_specs()


@dataclass
class AgentToolDeps:
    """Service-injected dependencies (so this module never imports the service)."""

    session_factory: Any
    config: Any
    validate_draft: Callable[..., Any]
    test_draft: Callable[..., Any]
    approve_draft: Callable[..., Any]
    publish_draft: Callable[..., Any]
    get_draft: Callable[..., Any]
    list_drafts: Callable[..., Any]


class AssistantAgentToolset:
    """Per-turn, context-bound, permissioned tool surface for the engine."""

    def __init__(
        self,
        *,
        deps: AgentToolDeps,
        user: str,
        session_id: str,
        mode: str,
        approval_mode: str,
        project_id: str | None = None,
    ) -> None:
        self._deps = deps
        self._user = user
        self._session_id = session_id
        self._mode = mode
        self._approval_mode = approval_mode
        self._project_id = project_id

    # -- permission gate -------------------------------------------------

    def _tier_allowed(self, tier: str) -> bool:
        if tier == TIER_READ:
            return True
        # Gated (publish/promote/deploy/spend) is never model-callable in the
        # synchronous author turn. Autonomous release runs later through the
        # reviewer/release service boundary, not through this tool projection.
        if tier == TIER_GATED:
            return False
        if self._mode != "build":
            return False
        if tier == TIER_SAFE:
            return self._approval_mode in (
                "auto_safe",
                "auto_all",
                "agent_review",
                "full_autonomy",
            )
        if tier == TIER_MUTATE:
            return self._approval_mode in ("auto_all", "full_autonomy")
        return False

    def _capability_context(self) -> CapabilityContext:
        return CapabilityContext(
            session_factory=self._deps.session_factory,
            config=self._deps.config,
            actor=self._user,
            project_id=self._project_id,
        )

    def specs(self) -> list[dict[str, Any]]:
        # Hand-written tools + the capability-registry projection (both tier-gated).
        out = [spec for spec, tier in _TOOL_SPECS.values() if self._tier_allowed(tier)]
        out.extend(
            cap.to_spec() for cap in registered_capabilities() if self._tier_allowed(cap.tier)
        )
        return out

    def _dispatch_capability(self, cap: Any, arguments: dict[str, Any]) -> str:
        if not self._tier_allowed(cap.tier):
            return _err(
                f"tool {cap.tool_name!r} is not permitted in mode={self._mode!r} "
                f"approval={self._approval_mode!r}"
            )
        try:
            return _ok(cap.handler(self._capability_context(), arguments))
        except Exception as exc:  # never let a tool error break the loop
            logger.exception("capability %s failed", cap.key)
            return _err(f"{type(exc).__name__}: {exc}")

    def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        # Capability-registry projection first (its tool names don't collide with
        # the hand-written ones).
        cap = capability_by_tool_name(name)
        if cap is not None:
            return self._dispatch_capability(cap, arguments)

        entry = _TOOL_SPECS.get(name)
        if entry is None:
            return _err(f"unknown tool {name!r}")
        if not self._tier_allowed(entry[1]):
            return _err(
                f"tool {name!r} is not permitted in mode={self._mode!r} "
                f"approval={self._approval_mode!r}"
            )
        handler: Callable[[dict[str, Any]], str] | None = getattr(self, f"_t_{name}", None)
        if handler is None:
            return _err(f"tool {name!r} has no handler")
        try:
            return handler(arguments)
        except Exception as exc:  # never let a tool error break the loop
            logger.exception("agent tool %s failed", name)
            return _err(f"{type(exc).__name__}: {exc}")

    # -- read handlers ---------------------------------------------------

    def _t_list_skills(self, _a: dict[str, Any]) -> str:
        from caliber.db.models import CaliberSkill  # noqa: PLC0415

        with self._deps.session_factory() as db:
            rows = (
                db.query(CaliberSkill)
                .filter(CaliberSkill.status == "active")
                .limit(_MAX_ROWS)
                .all()
            )
            return _ok(
                [{"name": r.name, "summary": r.summary or "", "category": r.category} for r in rows]
            )

    def _t_get_skill(self, a: dict[str, Any]) -> str:
        from caliber.db.models import CaliberSkill  # noqa: PLC0415

        name = str(a.get("name", ""))
        if not name:
            return _err("name is required")
        with self._deps.session_factory() as db:
            r = db.query(CaliberSkill).filter(CaliberSkill.name == name).first()
            if r is None:
                return _err(f"skill {name!r} not found")
            return _ok(
                {
                    "name": r.name,
                    "version": r.version,
                    "category": r.category,
                    "summary": r.summary or "",
                    "content": r.content,
                    "allowed_tools": r.allowed_tools,
                    "depends_on": list(r.depends_on or []),
                }
            )

    def _t_list_tools(self, _a: dict[str, Any]) -> str:
        from caliber.db.models import CaliberToolRegistry  # noqa: PLC0415

        with self._deps.session_factory() as db:
            rows = db.query(CaliberToolRegistry).limit(_MAX_ROWS).all()
            return _ok([{"name": r.name, "description": r.description} for r in rows])

    def _t_list_workflows(self, _a: dict[str, Any]) -> str:
        from caliber.db.models import CaliberWorkflow  # noqa: PLC0415

        with self._deps.session_factory() as db:
            rows = (
                db.query(CaliberWorkflow)
                .filter(CaliberWorkflow.status == "active")
                .limit(_MAX_ROWS)
                .all()
            )
            return _ok(
                [
                    {
                        "workflow_id": r.workflow_id,
                        "name": r.name,
                        "status": r.status,
                        "description": r.description,
                    }
                    for r in rows
                ]
            )

    def _t_get_workflow_manifest(self, a: dict[str, Any]) -> str:
        from caliber.db.models import CaliberWorkflowVersion  # noqa: PLC0415

        workflow_id = str(a.get("workflow_id", ""))
        if not workflow_id:
            return _err("workflow_id is required")
        version_number = a.get("version_number")
        with self._deps.session_factory() as db:
            q = db.query(CaliberWorkflowVersion).filter(
                CaliberWorkflowVersion.workflow_id == workflow_id
            )
            if version_number is not None:
                q = q.filter(CaliberWorkflowVersion.version_number == int(version_number))
            row = q.order_by(CaliberWorkflowVersion.version_number.desc()).first()
            if row is None:
                return _err(f"no version found for workflow {workflow_id!r}")
            return _ok(
                {
                    "version_id": row.version_id,
                    "version_number": row.version_number,
                    "status": row.status,
                    "manifest": row.manifest,
                }
            )

    def _t_list_workflow_runs(self, a: dict[str, Any]) -> str:
        from caliber.db.models import CaliberWorkflowRun  # noqa: PLC0415

        limit = min(int(a.get("limit", 10) or 10), _MAX_ROWS)
        workflow_id = a.get("workflow_id")
        with self._deps.session_factory() as db:
            q = db.query(CaliberWorkflowRun)
            if workflow_id:
                q = q.filter(CaliberWorkflowRun.workflow_id == str(workflow_id))
            rows = q.order_by(CaliberWorkflowRun.queued_at.desc()).limit(limit).all()
            return _ok(
                [
                    {
                        "run_id": r.workflow_run_id,
                        "workflow_id": r.workflow_id,
                        "status": r.status,
                        "source": r.source,
                        "trace_id": r.trace_id,
                        "error_summary": r.error_summary,
                    }
                    for r in rows
                ]
            )

    def _t_get_workflow_run(self, a: dict[str, Any]) -> str:
        from caliber.db.models import CaliberWorkflowRun  # noqa: PLC0415

        run_id = str(a.get("run_id", ""))
        with self._deps.session_factory() as db:
            r = db.get(CaliberWorkflowRun, run_id)
            if r is None:
                return _err(f"run {run_id!r} not found")
            return _ok(
                {
                    "run_id": r.workflow_run_id,
                    "workflow_id": r.workflow_id,
                    "version_id": r.workflow_version_id,
                    "status": r.status,
                    "trace_id": r.trace_id,
                    "error_code": r.error_code,
                    "error_summary": r.error_summary,
                    "summary": r.summary,
                }
            )

    def _t_get_workflow_run_trace(self, a: dict[str, Any]) -> str:
        from caliber.db.models import CaliberWorkflowRun  # noqa: PLC0415
        from caliber.trace_client import fetch_trace_spans  # noqa: PLC0415

        run_id = str(a.get("run_id", ""))
        with self._deps.session_factory() as db:
            r = db.get(CaliberWorkflowRun, run_id)
            if r is None:
                return _err(f"run {run_id!r} not found")
            trace_id = r.trace_id
        tree = fetch_trace_spans(trace_id)
        spans = list(tree.spans or [])
        return _ok(
            {
                "trace_id": tree.trace_id,
                "mlflow_url": tree.mlflow_url,
                "span_count": len(spans),
                "spans": spans[:_MAX_TRACE_SPANS],
            }
        )

    def _t_list_session_drafts(self, _a: dict[str, Any]) -> str:
        drafts = self._deps.list_drafts(
            self._session_id, session_factory=self._deps.session_factory, user=self._user
        )
        return _ok(
            [
                {
                    "draft_id": d.draft_id,
                    "artifact_type": d.artifact_type,
                    "status": d.status,
                    "title": d.title,
                }
                for d in drafts
            ]
        )

    def _t_get_draft(self, a: dict[str, Any]) -> str:
        draft = self._deps.get_draft(
            str(a.get("draft_id", "")), session_factory=self._deps.session_factory, user=self._user
        )
        if draft is None:
            return _err("draft not found")
        return _ok(draft.model_dump(mode="json"))

    # -- knowledge-base handlers (read) ---------------------------------

    def _kb_identity(self) -> Any:
        """Synthesize the acting identity for KB visibility (operator-scoped turn)."""
        from caliber.auth import (  # noqa: PLC0415
            SCOPE_OPERATOR,
            SCOPE_VIEWER,
            CaliberIdentity,
        )

        return CaliberIdentity(user_id=self._user, scopes=frozenset({SCOPE_OPERATOR, SCOPE_VIEWER}))

    def _kb_service(self) -> Any:
        from caliber.knowledge.service import KnowledgeBaseService  # noqa: PLC0415

        return KnowledgeBaseService(
            config=self._deps.config, session_factory=self._deps.session_factory
        )

    def _t_list_knowledge_bases(self, _a: dict[str, Any]) -> str:
        kbs = self._kb_service().list_knowledge_bases(identity=self._kb_identity())
        return _ok(
            [
                {
                    "knowledge_base_id": kb.knowledge_base_id,
                    "name": kb.name,
                    "status": kb.status,
                    "active_version_id": kb.active_version_id,
                    "last_run_status": kb.last_run_status,
                    "baseline_run_id": kb.baseline_run_id,
                }
                for kb in kbs[:_MAX_ROWS]
            ]
        )

    def _t_get_knowledge_base(self, a: dict[str, Any]) -> str:
        kb_id = str(a.get("knowledge_base_id", ""))
        ident = self._kb_identity()
        svc = self._kb_service()
        kb = svc.get_knowledge_base(kb_id, identity=ident)
        versions = svc.list_versions(kb_id, identity=ident)
        return _ok(
            {
                "knowledge_base_id": kb.knowledge_base_id,
                "name": kb.name,
                "status": kb.status,
                "active_version_id": kb.active_version_id,
                "last_run_status": kb.last_run_status,
                "versions": [
                    {
                        "version_id": v.version_id,
                        "version_number": v.version_number,
                        "status": v.status,
                    }
                    for v in versions[:_MAX_ROWS]
                ],
            }
        )

    def _t_get_knowledge_base_calibration(self, a: dict[str, Any]) -> str:
        runs = self._kb_service().list_calibration_runs(
            str(a.get("knowledge_base_id", "")), identity=self._kb_identity(), limit=5
        )
        return _ok([r.model_dump(mode="json") for r in runs])

    def _t_preview_skill_selection(self, a: dict[str, Any]) -> str:
        from caliber.assistant.skill_runtime import (  # noqa: PLC0415
            AssistantSkillResolutionRequest,
            resolve_assistant_skills,
        )

        query = str(a.get("query", ""))
        artifact_type = a.get("artifact_type")
        req = AssistantSkillResolutionRequest(
            user_message=query,
            artifact_type=str(artifact_type) if artifact_type else None,
            session_goal="",
            mode="auto",
            explicit_skill_names=(),
            pinned_skill_names=(),
            disabled_skill_names=(),
        )
        with self._deps.session_factory() as db:
            result = resolve_assistant_skills(db, req)
        return _ok(
            {
                "selected": [
                    {"name": s.name, "selection_reason": s.selection_reason, "category": s.category}
                    for s in result.skills
                ],
                "warnings": list(result.warnings),
            }
        )

    # -- safe handlers ---------------------------------------------------

    def _t_validate_draft(self, a: dict[str, Any]) -> str:
        report = self._deps.validate_draft(
            str(a.get("draft_id", "")), session_factory=self._deps.session_factory, user=self._user
        )
        return _ok(report.model_dump(mode="json"))

    def _t_test_draft(self, a: dict[str, Any]) -> str:
        report = self._deps.test_draft(
            str(a.get("draft_id", "")), session_factory=self._deps.session_factory, user=self._user
        )
        return _ok(report.model_dump(mode="json"))

    def _preview_result(self, result: Any) -> dict[str, Any]:
        return {
            "status": getattr(result, "status", None),
            "output": getattr(result, "output", None),
            "error": getattr(result, "error", None),
            "steps": [
                {
                    "node_id": s.node_id,
                    "node_type": s.node_type,
                    "status": s.status,
                    "handoff_target": s.handoff_target,
                    "detail": s.detail,
                }
                for s in getattr(result, "steps", [])
            ],
            "guardrail_results": getattr(result, "guardrail_results", None),
        }

    def _t_preview_workflow_draft(self, a: dict[str, Any]) -> str:
        from caliber.db.models import CaliberWorkflowVersion  # noqa: PLC0415
        from caliber.workflows.promoter import build_executor, build_plan  # noqa: PLC0415
        from caliber.workflows.runtime import execute  # noqa: PLC0415

        draft = self._deps.get_draft(
            str(a.get("draft_id", "")), session_factory=self._deps.session_factory, user=self._user
        )
        if draft is None:
            return _err("draft not found")
        if draft.artifact_type != "workflow":
            return _err(f"draft is a {draft.artifact_type}, not a workflow")
        manifest = draft.artifact
        input_text = str(a.get("input_text", ""))
        with self._deps.session_factory() as db:
            transient = CaliberWorkflowVersion(
                version_id="preview-draft",
                workflow_id=f"draft-{draft.draft_id}",
                version_number=0,
                manifest=manifest,
                created_by=self._user,
            )
            plan = build_plan(db, transient, manifest_override=manifest, config=self._deps.config)
            executor = build_executor(self._deps.config, ir=plan.ir)
            result = execute(
                plan, input_text, executor=executor, session_id=self._session_id, preview=True
            )
        return _ok(self._preview_result(result))

    def _t_preview_workflow_version(self, a: dict[str, Any]) -> str:
        from caliber.db.models import CaliberWorkflowVersion  # noqa: PLC0415
        from caliber.workflows.promoter import run_preview  # noqa: PLC0415

        version_id = str(a.get("version_id", ""))
        input_text = str(a.get("input_text", ""))
        with self._deps.session_factory() as db:
            version = db.get(CaliberWorkflowVersion, version_id)
            if version is None:
                return _err(f"version {version_id!r} not found")
            result = run_preview(
                db, version, input_text, session_id=self._session_id, config=self._deps.config
            )
        return _ok(
            {
                k: result.get(k)
                for k in (
                    "workflow_run_id",
                    "status",
                    "output",
                    "error",
                    "steps",
                    "guardrail_results",
                )
            }
        )

    def _t_run_quick_eval(self, a: dict[str, Any]) -> str:
        from caliber.db.models import CaliberEvalDataset, CaliberEvalDatasetExample  # noqa: PLC0415
        from caliber.eval.predict import build_completion_fn, user_message  # noqa: PLC0415
        from caliber.eval.scorecard import ScorecardInputError, run_scorecard  # noqa: PLC0415

        dataset_id = str(a.get("dataset_id", ""))
        instructions = str(a.get("instructions", ""))
        if not instructions:
            return _err("instructions are required")
        complete = build_completion_fn(self._deps.config)
        if complete is None:
            return _err("quick eval needs a real LLM provider (set CALIBER_LLM_PROVIDER + key)")
        cap = min(
            int(a.get("max_examples", _MAX_QUICK_EVAL_EXAMPLES) or _MAX_QUICK_EVAL_EXAMPLES),
            _MAX_QUICK_EVAL_EXAMPLES,
        )
        scorers = a.get("scorers") or None
        with self._deps.session_factory() as db:
            ds = db.get(CaliberEvalDataset, dataset_id)
            if ds is None:
                return _err(f"dataset {dataset_id!r} not found")
            rows = (
                db.query(CaliberEvalDatasetExample)
                .filter(
                    CaliberEvalDatasetExample.dataset_id == dataset_id,
                    CaliberEvalDatasetExample.superseded_at.is_(None),
                )
                .order_by(CaliberEvalDatasetExample.created_at)
                .limit(cap)
                .all()
            )
            examples = [
                {
                    "example_id": r.example_id,
                    "input": dict(r.input or {}),
                    "expected": dict(r.expected or {}),
                    "weight": r.weight if r.weight is not None else 1.0,
                    "tags": list(r.tags or []),
                }
                for r in rows
            ]
        if not examples:
            return _err("dataset has no examples")

        def predict(inputs: Mapping[str, Any]) -> str:
            return complete(instructions, user_message(dict(inputs)))

        try:
            result = run_scorecard(examples, predict, scorers, pass_threshold=0.5)
        except ScorecardInputError as exc:
            return _err(str(exc))
        return _ok(
            {
                "n_examples": len(result.rows),
                "pass_rate": result.pass_rate,
                "overall_score": result.overall,
                "aggregate": result.aggregate,
            }
        )

    def _t_propose_workflow_patch(self, a: dict[str, Any]) -> str:
        from caliber.workflows.manifest import parse_manifest  # noqa: PLC0415
        from caliber.workflows.refinement import (  # noqa: PLC0415
            generate_workflow_patch,
            localize_failure,
        )

        draft = self._deps.get_draft(
            str(a.get("draft_id", "")), session_factory=self._deps.session_factory, user=self._user
        )
        if draft is None:
            return _err("draft not found")
        if draft.artifact_type != "workflow":
            return _err(f"draft is a {draft.artifact_type}, not a workflow")
        evidence = a.get("evidence") or {}
        manifest = parse_manifest(draft.artifact)
        diagnosis = localize_failure(evidence if isinstance(evidence, dict) else {})
        candidate = generate_workflow_patch(manifest, diagnosis)
        return _ok(
            {
                "summary": candidate.summary,
                "ops": candidate.semantic_ops,
                "prompt_suggestion": candidate.prompt_suggestion,
                "patched_manifest": candidate.candidate_manifest,
            }
        )

    def _t_query_knowledge_base(self, a: dict[str, Any]) -> str:
        from caliber.knowledge.schemas import KnowledgeQueryRequest  # noqa: PLC0415

        kb_id = str(a.get("knowledge_base_id", ""))
        question = str(a.get("question", ""))
        if not question:
            return _err("question is required")
        mode = str(a.get("retrieval_mode") or "dense")
        if mode not in ("dense", "hybrid", "graph_hybrid", "age_graph"):
            return _err(f"unknown retrieval_mode {mode!r}")
        top_k = min(max(int(a.get("top_k", 6) or 6), 1), 20)
        ident = self._kb_identity()
        svc = self._kb_service()
        kb = svc.get_knowledge_base(kb_id, identity=ident)
        if not kb.active_version_id:
            return _err("knowledge base has no active version — build/index it first")
        payload = KnowledgeQueryRequest(
            version_ids=[kb.active_version_id],
            question=question,
            top_k=top_k,
            retrieval_modes=[mode],  # type: ignore[list-item]
        )
        result = svc.query(payload, identity=ident)
        versions = []
        for ver in result.versions:
            chunks = [
                {"chunk_id": c.chunk_id, "score": c.score, "content": (c.content or "")[:600]}
                for c in (ver.retrieved_chunks or [])[:top_k]
            ]
            versions.append(
                {
                    "version_id": ver.version_id,
                    "answer": ver.answer,
                    "retrieved_chunks": chunks,
                    "timing_ms": ver.timing_ms,
                }
            )
        return _ok({"question": result.question, "versions": versions})

    def _t_run_tool_sandbox(self, a: dict[str, Any]) -> str:
        from caliber.tool_sandbox.models import ToolSandboxRunRequest  # noqa: PLC0415
        from caliber.tool_sandbox.service import sandbox_from_optional_config  # noqa: PLC0415

        draft = self._deps.get_draft(
            str(a.get("draft_id", "")), session_factory=self._deps.session_factory, user=self._user
        )
        if draft is None:
            return _err("draft not found")
        if draft.artifact_type != "tool":
            return _err(f"draft is a {draft.artifact_type}, not a tool")
        artifact = draft.artifact
        source = str(artifact.get("source") or "")
        callable_name = str(artifact.get("callable_name") or artifact.get("name") or "")
        if not source or not callable_name:
            return _err("tool draft is missing source or a callable name")
        tool_input = a.get("input") or {}
        sandbox = sandbox_from_optional_config(self._deps.config)
        run = sandbox.run_tool(
            ToolSandboxRunRequest(
                source_code=source,
                callable_name=callable_name,
                input=tool_input if isinstance(tool_input, dict) else {},
            )
        )
        return _ok(
            {
                "status": run.status,
                "output": run.output,
                "error": run.error,
                "stdout": (run.stdout or "")[:1000],
                "duration_ms": run.duration_ms,
            }
        )

    # -- dataset creation + dataset-scored evaluation -------------------

    def _load_dataset_examples(self, db: Any, dataset_id: str, cap: int) -> list[dict[str, Any]]:
        from caliber.db.models import CaliberEvalDatasetExample  # noqa: PLC0415

        rows = (
            db.query(CaliberEvalDatasetExample)
            .filter(
                CaliberEvalDatasetExample.dataset_id == dataset_id,
                CaliberEvalDatasetExample.superseded_at.is_(None),
            )
            .order_by(CaliberEvalDatasetExample.created_at)
            .limit(cap)
            .all()
        )
        return [
            {
                "example_id": r.example_id,
                "input": dict(r.input or {}),
                "expected": dict(r.expected or {}),
                "weight": r.weight if r.weight is not None else 1.0,
                "tags": list(r.tags or []),
            }
            for r in rows
        ]

    def _t_create_eval_dataset(self, a: dict[str, Any]) -> str:
        from caliber.db.models import (  # noqa: PLC0415
            CaliberEvalDataset,
            CaliberEvalDatasetExample,
        )
        from caliber.ids import new_eval_dataset_id, new_eval_example_id  # noqa: PLC0415

        name = str(a.get("name", "")).strip()
        if not name:
            return _err("name is required")
        raw = a.get("examples")
        if not isinstance(raw, list) or not raw:
            return _err("examples must be a non-empty list of {input, expected}")
        description = str(a.get("description") or "")
        with self._deps.session_factory() as db:
            if db.query(CaliberEvalDataset).filter(CaliberEvalDataset.name == name).first():
                return _err(f"dataset name {name!r} already exists")
            dataset = CaliberEvalDataset(
                dataset_id=new_eval_dataset_id(),
                name=name,
                description=description,
                owner=self._user,
                status="active",
                version=1,
            )
            db.add(dataset)
            count = 0
            for ex in raw[:_MAX_DATASET_EXAMPLES]:
                if not isinstance(ex, dict):
                    continue
                inp = ex.get("input")
                exp = ex.get("expected")
                inp = inp if isinstance(inp, dict) else {"input": "" if inp is None else str(inp)}
                exp = (
                    exp if isinstance(exp, dict) else {"expected": "" if exp is None else str(exp)}
                )
                db.add(
                    CaliberEvalDatasetExample(
                        example_id=new_eval_example_id(),
                        dataset_id=dataset.dataset_id,
                        dataset_version=1,
                        input=inp,
                        expected=exp,
                    )
                )
                count += 1
            if count == 0:
                return _err("no valid examples provided")
            db.commit()
            return _ok({"dataset_id": dataset.dataset_id, "name": name, "examples": count})

    def _t_evaluate_tool_draft(self, a: dict[str, Any]) -> str:
        from caliber.eval.scorecard import ScorecardInputError, run_scorecard  # noqa: PLC0415
        from caliber.tool_sandbox.models import ToolSandboxRunRequest  # noqa: PLC0415
        from caliber.tool_sandbox.service import sandbox_from_optional_config  # noqa: PLC0415

        draft = self._deps.get_draft(
            str(a.get("draft_id", "")), session_factory=self._deps.session_factory, user=self._user
        )
        if draft is None:
            return _err("draft not found")
        if draft.artifact_type != "tool":
            return _err(f"draft is a {draft.artifact_type}, not a tool")
        source = str(draft.artifact.get("source") or "")
        callable_name = str(draft.artifact.get("callable_name") or draft.artifact.get("name") or "")
        if not source or not callable_name:
            return _err("tool draft is missing source or a callable name")
        cap = min(
            int(a.get("max_examples", _MAX_EVAL_EXAMPLES) or _MAX_EVAL_EXAMPLES), _MAX_EVAL_EXAMPLES
        )
        with self._deps.session_factory() as db:
            examples = self._load_dataset_examples(db, str(a.get("dataset_id", "")), cap)
        if not examples:
            return _err("dataset has no examples")
        sandbox = sandbox_from_optional_config(self._deps.config)

        def predict(inputs: Mapping[str, Any]) -> str:
            run = sandbox.run_tool(
                ToolSandboxRunRequest(
                    source_code=source, callable_name=callable_name, input=dict(inputs)
                )
            )
            if run.status != "completed":
                raise RuntimeError(run.error or run.status)
            out = run.output
            return out if isinstance(out, str) else json.dumps(out, default=str, sort_keys=True)

        try:
            result = run_scorecard(
                examples,
                predict,
                a.get("scorers") or None,
                pass_threshold=0.5,
            )
        except ScorecardInputError as exc:
            return _err(str(exc))
        return _ok(
            {
                "n_examples": len(result.rows),
                "pass_rate": result.pass_rate,
                "overall_score": result.overall,
                "aggregate": result.aggregate,
            }
        )

    def _t_evaluate_workflow_draft(self, a: dict[str, Any]) -> str:
        from caliber.db.models import CaliberWorkflowVersion  # noqa: PLC0415
        from caliber.eval.predict import user_message  # noqa: PLC0415
        from caliber.workflows.calibration import (  # noqa: PLC0415
            WorkflowCalibrationExample,
            score_workflow_calibration_run,
        )
        from caliber.workflows.promoter import build_executor, build_plan  # noqa: PLC0415
        from caliber.workflows.runtime import execute  # noqa: PLC0415

        draft = self._deps.get_draft(
            str(a.get("draft_id", "")), session_factory=self._deps.session_factory, user=self._user
        )
        if draft is None:
            return _err("draft not found")
        if draft.artifact_type != "workflow":
            return _err(f"draft is a {draft.artifact_type}, not a workflow")
        manifest = draft.artifact
        cap = min(
            int(a.get("max_examples", _MAX_EVAL_EXAMPLES) or _MAX_EVAL_EXAMPLES), _MAX_EVAL_EXAMPLES
        )
        with self._deps.session_factory() as db:
            rows = self._load_dataset_examples(db, str(a.get("dataset_id", "")), cap)
            if not rows:
                return _err("dataset has no examples")
            transient = CaliberWorkflowVersion(
                version_id="eval-draft",
                workflow_id=f"draft-{draft.draft_id}",
                version_number=0,
                manifest=manifest,
                created_by=self._user,
            )
            plan = build_plan(db, transient, manifest_override=manifest, config=self._deps.config)
            executor = build_executor(self._deps.config, ir=plan.ir)
            totals: dict[str, float] = {}
            counts: dict[str, int] = {}
            for row in rows:
                example = WorkflowCalibrationExample(
                    input_text=user_message(row["input"]),
                    expected=row["expected"],
                    weight=float(row.get("weight", 1.0)),
                    tags=[str(tag) for tag in row.get("tags", [])],
                    example_id=row["example_id"],
                )
                run = execute(
                    plan,
                    example.input_text,
                    executor=executor,
                    session_id=self._session_id,
                    preview=True,
                )
                scored = score_workflow_calibration_run(run, example)
                for dim, val in scored.scores.items():
                    if isinstance(val, (int, float)):
                        totals[dim] = totals.get(dim, 0.0) + float(val)
                        counts[dim] = counts.get(dim, 0) + 1
        scores = {dim: round(totals[dim] / counts[dim], 4) for dim in totals if counts.get(dim)}
        return _ok({"n_examples": len(rows), "scores": scores})

    def _t_evaluate_skill_selection(self, a: dict[str, Any]) -> str:
        from caliber.assistant.skill_runtime import (  # noqa: PLC0415
            AssistantSkillResolutionRequest,
            resolve_assistant_skills,
        )
        from caliber.eval.predict import user_message  # noqa: PLC0415

        cap = min(
            int(a.get("max_examples", _MAX_EVAL_EXAMPLES) or _MAX_EVAL_EXAMPLES), _MAX_EVAL_EXAMPLES
        )
        rows_out: list[dict[str, Any]] = []
        scored = 0
        correct = 0
        with self._deps.session_factory() as db:
            examples = self._load_dataset_examples(db, str(a.get("dataset_id", "")), cap)
            if not examples:
                return _err("dataset has no examples")
            for ex in examples:
                expected_skill = str(
                    ex["expected"].get("skill") or ex["expected"].get("expected") or ""
                )
                req = AssistantSkillResolutionRequest(
                    user_message=user_message(ex["input"]),
                    artifact_type=None,
                    session_goal="",
                    mode="auto",
                    explicit_skill_names=(),
                    pinned_skill_names=(),
                    disabled_skill_names=(),
                )
                result = resolve_assistant_skills(db, req)
                selected = [s.name for s in result.skills]
                hit = bool(expected_skill) and expected_skill in selected
                if expected_skill:
                    scored += 1
                    correct += int(hit)
                rows_out.append(
                    {"expected_skill": expected_skill, "selected": selected, "correct": hit}
                )
        accuracy = round(correct / scored, 4) if scored else None
        return _ok(
            {
                "n_examples": len(examples),
                "scored": scored,
                "selection_accuracy": accuracy,
                "results": rows_out[:_MAX_ROWS],
            }
        )

    # -- mutate handlers -------------------------------------------------

    def _t_run_workflow(self, a: dict[str, Any]) -> str:
        from caliber.db.models import CaliberWorkflow, CaliberWorkflowVersion  # noqa: PLC0415
        from caliber.workflows.run_launch import enqueue_workflow_run  # noqa: PLC0415

        version_id = str(a.get("version_id", ""))
        input_text = str(a.get("input_text", ""))
        with self._deps.session_factory() as db:
            version = db.get(CaliberWorkflowVersion, version_id)
            if version is None:
                return _err(f"version {version_id!r} not found")
            workflow = db.get(CaliberWorkflow, version.workflow_id)
            if workflow is None:
                return _err("parent workflow not found")
            run, created = enqueue_workflow_run(
                db,
                workflow=workflow,
                version=version,
                alias="",
                source="assistant",
                actor=self._user,
                input_text=input_text,
                session_id=self._session_id,
            )
            db.commit()
            run_id = run.workflow_run_id
            status = run.status
        return _ok(
            {
                "workflow_run_id": run_id,
                "status": status,
                "created": created,
                "note": "Queued — observe with get_workflow_run / get_workflow_run_trace.",
            }
        )

    def _t_approve_draft(self, a: dict[str, Any]) -> str:
        result = self._deps.approve_draft(
            str(a.get("draft_id", "")), session_factory=self._deps.session_factory, user=self._user
        )
        if result is None:
            return _err("draft not found")
        return _ok({"draft_id": result.draft_id, "status": result.status})

    def _t_publish_draft(self, a: dict[str, Any]) -> str:
        report = self._deps.publish_draft(
            str(a.get("draft_id", "")), session_factory=self._deps.session_factory, user=self._user
        )
        return _ok(report)
