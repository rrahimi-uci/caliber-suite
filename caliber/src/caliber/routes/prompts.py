"""``/caliber/prompts`` endpoint — unified prompt management.

Merges prompts from the **MLflow Prompt Registry** with Caliber's
``CaliberAgentConfig`` rows so that both agent-linked prompts and
standalone MLflow prompts appear in a single list.  Also provides
endpoints for creating new prompts and registering new versions—
all writes go directly to the MLflow Prompt Registry so prompts
are always visible from both Caliber and MLflow.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from datetime import datetime, timezone
from types import ModuleType
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import (
    SCOPE_ADMIN,
    SCOPE_OPERATOR,
    require_scopes,
    require_user,
    resolve_identity,
)
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberAuditLog,
    CaliberEvalDataset,
    CaliberPromptTestRun,
    CaliberRefinementJob,
    CaliberVerificationItem,
    CaliberWorkflow,
    CaliberWorkflowVersion,
)
from caliber.db.scoping import apply_visibility_filter
from caliber.eval.gate import DEFAULT_MAX_REGRESSION_DELTA, DEFAULT_MIN_AGGREGATE_SCORE
from caliber.gate_verdicts import GATE_STATES, record_gate_verdict
from caliber.ids import new_item_id, new_job_id, new_prompt_test_run_id
from caliber.prompt_targets import (
    ensure_prompt_target,
    is_hidden_prompt_target,
    prompt_target_status,
)
from caliber.prompt_template_library import (
    list_prompt_template_catalog,
    preview_prompt_template,
)
from caliber.routes._deps import get_session_factory, parse_json_object
from caliber.schemas import (
    PromptBaselineRequest,
    PromptBindRequest,
    PromptOptimizationRunRequest,
    PromptOptimizationRunResponse,
    PromptTestRunCreateRequest,
    PromptTestRunDetail,
    PromptTestRunSummary,
    PromptWorkspaceLastRun,
    PromptWorkspaceResponse,
    RefinementJobSchema,
    VerificationItemSchema,
)

logger = logging.getLogger("caliber.routes.prompts")

# Max characters shown in a template preview before truncation.
_TEMPLATE_PREVIEW_CHARS = 200

LIST_PATH = "/ajax-api/2.0/mlflow/caliber/prompts"
CREATE_PATH = "/ajax-api/2.0/mlflow/caliber/prompts"
DETAIL_PATH = "/ajax-api/2.0/mlflow/caliber/prompts/{name}"
VERSION_PATH = "/ajax-api/2.0/mlflow/caliber/prompts/{name}/versions"
VERSIONS_LIST_PATH = "/ajax-api/2.0/mlflow/caliber/prompts/{name}/versions"
VERSION_DETAIL_PATH = "/ajax-api/2.0/mlflow/caliber/prompts/{name}/versions/{version}"
ALIAS_SET_PATH = "/ajax-api/2.0/mlflow/caliber/prompts/{name}/aliases/{alias}"
ROLLBACK_PATH = "/ajax-api/2.0/mlflow/caliber/prompts/{name}/rollback"
TEST_RENDER_PATH = "/ajax-api/2.0/mlflow/caliber/prompts/{agent_id}/test-render"
TEMPLATE_LIBRARY_PATH = "/ajax-api/2.0/mlflow/caliber/prompts/template-library"
TEMPLATE_PREVIEW_PATH = "/ajax-api/2.0/mlflow/caliber/prompts/template-library/preview"
OPTIMIZATION_OPTIONS_PATH = "/ajax-api/2.0/mlflow/caliber/prompts/optimization/options"
OPTIMIZATION_RUNS_PATH = "/ajax-api/2.0/mlflow/caliber/prompts/optimization/runs"
CALIBRATION_OPTIONS_PATH = "/ajax-api/2.0/mlflow/caliber/prompts/calibration/options"
CALIBRATION_RUNS_PATH = "/ajax-api/2.0/mlflow/caliber/prompts/calibration/runs"
TEST_RUNS_PATH = "/ajax-api/2.0/mlflow/caliber/prompts/test-runs"
TEST_RUN_DETAIL_PATH = "/ajax-api/2.0/mlflow/caliber/prompts/test-runs/{test_run_id}"
WORKSPACE_PATH = "/ajax-api/2.0/mlflow/caliber/prompts/{name}/workspace"
BIND_PATH = "/ajax-api/2.0/mlflow/caliber/prompts/{name}/bind"
BASELINE_PATH = "/ajax-api/2.0/mlflow/caliber/prompts/{name}/baseline"

# History listing defaults/cap for ``GET /prompts/test-runs``.
_TEST_RUNS_DEFAULT_LIMIT = 20
_TEST_RUNS_MAX_LIMIT = 100

_SUPPORTED_PROMPT_OPTIMIZERS: tuple[str, ...] = ("MetaPrompt", "GEPA")
_DEEPEVAL_INSTALL_COMMAND = "pip install -U deepeval"
# v1 single-environment mode: prompts resolve and publish against one live
# alias only. Restore the dev→staging→prod ladder by listing the extra aliases
# here again, e.g. ("prod", "staging", "dev").
_PROMPT_DISCOVERY_ALIASES: tuple[str, ...] = ("prod",)
_PROMPT_WRITE_ALIASES: frozenset[str] = frozenset(_PROMPT_DISCOVERY_ALIASES)
_SCORER_SPECS: tuple[dict[str, Any], ...] = (
    {
        "name": "Correctness",
        "label": "Correctness",
        "description": "Scores factual correctness of the response.",
        "provider": "mlflow",
        "category": "core",
        "default_enabled": True,
        "requires_config": False,
        "config_template": None,
    },
    {
        "name": "Guidelines",
        "label": "Guidelines",
        "description": "Checks responses against provided policy guidelines.",
        "provider": "mlflow",
        "category": "core",
        "default_enabled": False,
        "requires_config": True,
        "config_template": {"guidelines": ["Do not hallucinate."]},
    },
    {
        "name": "RelevanceToQuery",
        "label": "Relevance",
        "description": "Measures how directly the response addresses the input.",
        "provider": "mlflow",
        "category": "core",
        "default_enabled": True,
        "requires_config": False,
        "config_template": None,
    },
    {
        "name": "Safety",
        "label": "Safety",
        "description": "Detects unsafe or policy-violating response content.",
        "provider": "mlflow",
        "category": "core",
        "default_enabled": True,
        "requires_config": False,
        "config_template": None,
    },
    {
        "name": "DeepEval.AnswerRelevancy",
        "label": "Answer Relevancy",
        "description": "DeepEval metric for answer relevancy to the query.",
        "provider": "deepeval",
        "category": "deepeval_beta",
        "default_enabled": False,
        "requires_config": False,
        "config_template": None,
    },
    {
        "name": "DeepEval.Faithfulness",
        "label": "Faithfulness",
        "description": "DeepEval metric for factual faithfulness to supplied context.",
        "provider": "deepeval",
        "category": "deepeval_beta",
        "default_enabled": False,
        "requires_config": False,
        "config_template": None,
    },
    {
        "name": "DeepEval.Toxicity",
        "label": "Toxicity",
        "description": "DeepEval metric for toxic or harmful response content.",
        "provider": "deepeval",
        "category": "deepeval_beta",
        "default_enabled": False,
        "requires_config": False,
        "config_template": None,
    },
    {
        "name": "DeepEval.ToolUse",
        "label": "Tool Use",
        "description": "DeepEval metric for tool-use quality and correctness.",
        "provider": "deepeval",
        "category": "deepeval_beta",
        "default_enabled": False,
        "requires_config": False,
        "config_template": None,
    },
)


def _module_available(module_name: str) -> bool:
    """Return true when ``module_name`` is importable in the current env."""
    return importlib.util.find_spec(module_name) is not None


def _deepeval_runtime_status() -> dict[str, Any]:
    available = _module_available("deepeval")
    reason = None if available else "deepeval is not installed in this environment"
    return {
        "available": available,
        "package": "deepeval",
        "install_policy": "latest",
        "install_command": _DEEPEVAL_INSTALL_COMMAND,
        "reason": reason,
    }


def _build_scorer_capabilities() -> tuple[
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    list[str],
    dict[str, Any],
]:
    deepeval_runtime = _deepeval_runtime_status()

    scorer_options: list[dict[str, Any]] = []
    scorer_index: dict[str, dict[str, Any]] = {}
    default_scorers: list[str] = []

    for spec in _SCORER_SPECS:
        option = dict(spec)
        provider = str(option.get("provider") or "mlflow")

        if provider == "deepeval":
            available = bool(deepeval_runtime["available"])
            option["available"] = available
            option["install_command"] = _DEEPEVAL_INSTALL_COMMAND
            option["unavailable_reason"] = None if available else deepeval_runtime.get("reason")
        else:
            option["available"] = True
            option["install_command"] = None
            option["unavailable_reason"] = None

        name = str(option["name"])
        scorer_options.append(option)
        scorer_index[name] = option

        if bool(option.get("default_enabled")) and bool(option.get("available")):
            default_scorers.append(name)

    runtime = {"deepeval": deepeval_runtime}
    return scorer_options, scorer_index, default_scorers, runtime


def register_prompt_version(
    *,
    name: str,
    template: str,
    commit_message: str,
    tags: dict[str, Any] | None = None,
    source: str = "caliber-ui",
    target_alias: str | None = None,
    set_prod_alias: bool | None = None,
) -> dict[str, Any]:
    """Register a prompt version and best-effort point one alias at it.

    This helper is intentionally shared across prompt routes and assistant
    intent execution adapters so all writes follow the same validations
    and MLflow API behavior.
    """
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="'name' is required")
    if not re.match(r"^[a-zA-Z0-9_-]+$", clean_name):
        raise HTTPException(
            status_code=400,
            detail="'name' must contain only alphanumerics, hyphens, and underscores",
        )

    clean_template = template.strip()
    if not clean_template:
        raise HTTPException(status_code=400, detail="'template' is required")

    alias_to_set: str | None = None
    if isinstance(target_alias, str) and target_alias.strip():
        clean_target_alias = target_alias.strip().lower()
        if clean_target_alias not in _PROMPT_WRITE_ALIASES:
            raise HTTPException(
                status_code=400,
                detail=(f"'target_alias' must be one of {sorted(_PROMPT_WRITE_ALIASES)}"),
            )
        alias_to_set = clean_target_alias
    elif set_prod_alias is not False:
        alias_to_set = "prod"

    payload_tags = dict(tags) if isinstance(tags, dict) else {}
    payload_tags["caliber.source"] = source

    mlflow = _get_mlflow_module()
    if mlflow is None:
        raise HTTPException(status_code=503, detail="mlflow not available")

    register_prompt = _resolve_api(mlflow, "register_prompt")
    if register_prompt is None:
        raise HTTPException(status_code=503, detail="mlflow prompt registry API not available")

    try:
        version = register_prompt(
            name=clean_name,
            template=clean_template,
            commit_message=commit_message,
            tags=payload_tags,
        )
    except Exception as exc:
        logger.exception("register_prompt failed for %s", clean_name)
        raise HTTPException(status_code=502, detail=f"failed to register prompt: {exc}") from exc

    version_number = int(getattr(version, "version", 0))
    uri = getattr(version, "uri", f"prompts:/{clean_name}/{version_number}")

    set_alias = _resolve_api(mlflow, "set_prompt_alias")
    if set_alias and version_number and alias_to_set:
        try:
            set_alias(clean_name, alias_to_set, version_number)
        except Exception:
            logger.debug(
                "set_prompt_alias failed for %s@%s v%s",
                clean_name,
                alias_to_set,
                version_number,
                exc_info=True,
            )

    return {
        "name": clean_name,
        "version": version_number,
        "uri": str(uri),
        "template_preview": (clean_template[:_TEMPLATE_PREVIEW_CHARS] + "…")
        if len(clean_template) > _TEMPLATE_PREVIEW_CHARS
        else clean_template,
        "template_length": len(clean_template),
        "alias_changed": bool(set_alias and version_number and alias_to_set),
        "active_alias": alias_to_set or "",
    }


def enqueue_prompt_optimization_run(  # noqa: PLR0912, PLR0915 - sequential validate→pin→persist→audit flow is intentionally explicit
    *,
    session: Session,
    payload: PromptOptimizationRunRequest,
    actor: str,
    project_id: str | None = None,
) -> PromptOptimizationRunResponse:
    """Create verification item + refinement job for manual prompt optimization."""
    if payload.optimizer_type not in _SUPPORTED_PROMPT_OPTIMIZERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unsupported optimizer_type {payload.optimizer_type!r}; "
                f"expected one of {list(_SUPPORTED_PROMPT_OPTIMIZERS)}"
            ),
        )

    _, scorer_index, _, _ = _build_scorer_capabilities()
    supported_scorer_names = sorted(scorer_index.keys())

    seen_scorers: set[str] = set()
    normalized_scorers: list[dict[str, Any]] = []
    for scorer in payload.scorers:
        scorer_meta = scorer_index.get(scorer.name)
        if scorer_meta is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unsupported scorer {scorer.name!r}; expected one of {supported_scorer_names}"
                ),
            )
        if scorer.name in seen_scorers:
            raise HTTPException(
                status_code=400,
                detail=f"duplicate scorer {scorer.name!r} is not allowed",
            )
        seen_scorers.add(scorer.name)

        if not bool(scorer_meta.get("available", True)):
            reason = str(scorer_meta.get("unavailable_reason") or "dependency unavailable")
            install_command = scorer_meta.get("install_command")
            detail = f"scorer {scorer.name!r} is unavailable: {reason}"
            if isinstance(install_command, str) and install_command:
                detail = f"{detail}. Install latest with '{install_command}' and restart CALIBER"
            raise HTTPException(status_code=400, detail=detail)

        if bool(scorer_meta.get("requires_config", False)) and not scorer.config:
            raise HTTPException(
                status_code=400,
                detail=f"scorer {scorer.name!r} requires a non-empty config object",
            )

        normalized_scorers.append(
            {
                "name": scorer.name,
                "weight": float(scorer.weight),
                "config": dict(scorer.config),
            }
        )

    gate_overrides = payload.gate.model_dump(exclude_none=True) if payload.gate else {}

    # Auto-provision a hidden runtime identity for the prompt rather than
    # demanding a pre-registered agent. This makes calibration "just work"
    # against any prompt name and satisfies the verification-item + refinement-
    # job FKs (both reference caliber_agent_config.agent_id). The dataset is
    # pinned below to ``optimizer_config.dataset_id`` so the "Has test set"
    # status signal reads from the target.
    target = ensure_prompt_target(
        session,
        payload.agent_id,
        owner=actor,
        project_id=project_id,
    )

    prompt_alias = str(payload.prompt_alias or "prod").strip().lower() or "prod"
    if prompt_alias not in _PROMPT_WRITE_ALIASES:
        raise HTTPException(
            status_code=400,
            detail=f"prompt_alias must be one of {sorted(_PROMPT_WRITE_ALIASES)}",
        )

    dataset = session.get(CaliberEvalDataset, payload.eval_dataset_id)
    if dataset is None:
        raise HTTPException(
            status_code=404,
            detail=f"eval dataset {payload.eval_dataset_id!r} not found",
        )
    if dataset.status != "active":
        raise HTTPException(
            status_code=400,
            detail=f"eval dataset {payload.eval_dataset_id!r} is not active",
        )

    # Pin the eval-dataset version for reproducibility. If the caller passed a
    # version it must be in range (the dataset only ever had versions 1..N);
    # otherwise pin the dataset's current version at launch.
    if payload.eval_dataset_version is not None:
        if not (1 <= payload.eval_dataset_version <= dataset.version):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"eval_dataset_version must be between 1 and {dataset.version} "
                    f"for dataset {payload.eval_dataset_id!r}, got "
                    f"{payload.eval_dataset_version}"
                ),
            )
        pinned_version = payload.eval_dataset_version
    else:
        pinned_version = dataset.version

    # Record the dataset on the hidden target so the "Has test set" status signal
    # reads off ``optimizer_config.dataset_id`` (a calibration run implies the
    # prompt has an associated test set).
    if isinstance(target.optimizer_config, dict):
        target.optimizer_config = {
            **target.optimizer_config,
            "dataset_id": payload.eval_dataset_id,
        }

    submitted_context: dict[str, Any] = {
        "source": "prompt_optimization",
        "prompt_optimization": {
            "eval_dataset_id": payload.eval_dataset_id,
            "eval_dataset_version": pinned_version,
            "optimizer_type": payload.optimizer_type,
            "scorers": normalized_scorers,
            "gate": gate_overrides,
            "prompt_alias": prompt_alias,
        },
    }
    baseline_content = _load_prompt_template(payload.agent_id, alias=prompt_alias)
    if isinstance(baseline_content, str) and baseline_content:
        submitted_context["prompt_optimization"]["baseline_content"] = baseline_content

    item = CaliberVerificationItem(
        item_id=new_item_id(),
        agent_id=payload.agent_id,
        category="prompt_optimization",
        free_text=(
            f"Manual prompt optimization run for {payload.agent_id} "
            f"against dataset {payload.eval_dataset_id}"
        ),
        severity="standard",
        artifact_type_hint="prompt",
        artifact_ref=f"prompts:/{payload.agent_id}@{prompt_alias}",
        submitted_context=submitted_context,
        status="verified",
        verified_by=actor,
        verified_at=datetime.now(timezone.utc),
        verification_notes=payload.notes,
        refinement_target="prompt",
    )
    session.add(item)
    session.flush()

    job = CaliberRefinementJob(
        job_id=new_job_id(),
        agent_id=payload.agent_id,
        primary_item_id=item.item_id,
        artifact_type="prompt",
        optimizer_type=payload.optimizer_type,
        status="queued",
        current_stage="triage",
        bundle_targets=[],
    )
    session.add(job)
    session.flush()

    audit_record(
        session,
        actor=actor,
        action="create_item",
        entity_type="verification_item",
        entity_id=item.item_id,
        details={
            "source": "prompt_optimization",
            "severity": "standard",
            "linked_job_id": job.job_id,
        },
    )
    audit_record(
        session,
        actor=actor,
        action="create_job",
        entity_type="refinement_job",
        entity_id=job.job_id,
        details={
            "from_item_id": item.item_id,
            "agent_id": payload.agent_id,
            "artifact_type": "prompt",
            "optimizer_type": payload.optimizer_type,
            "eval_dataset_id": payload.eval_dataset_id,
            "eval_dataset_version": pinned_version,
            "scorers": [s["name"] for s in normalized_scorers],
        },
    )
    session.commit()

    return PromptOptimizationRunResponse(
        item=VerificationItemSchema.model_validate(item),
        job=RefinementJobSchema.model_validate(job),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_mlflow_module() -> ModuleType | None:
    """Lazy-import mlflow and return it, or ``None`` if unavailable."""
    try:
        import mlflow  # noqa: PLC0415

        return mlflow
    except ImportError:
        logger.warning("mlflow not installed; prompt operations unavailable")
        return None


def _resolve_api(mlflow_mod: ModuleType, name: str) -> Callable[..., Any] | None:
    """Return ``mlflow.genai.<name>`` when available, else ``mlflow.<name>``."""
    genai = getattr(mlflow_mod, "genai", None)
    if genai is not None:
        fn = getattr(genai, name, None)
        if callable(fn):
            return cast(Callable[..., Any], fn)
    fn = getattr(mlflow_mod, name, None)
    if callable(fn):
        return cast(Callable[..., Any], fn)
    return None


def _load_prompt_info(agent_id: str, alias: str = "prod") -> dict[str, Any] | None:
    """Load the active prompt for an agent from the MLflow registry.

    Returns a dict with version metadata, or ``None`` if no prompt has
    been promoted yet (cold start).
    """
    mlflow = _get_mlflow_module()
    if mlflow is None:
        return None

    load_prompt = _resolve_api(mlflow, "load_prompt")
    if load_prompt is None:
        return None

    ref = f"prompts:/{agent_id}@{alias}"
    try:
        prompt = load_prompt(ref, allow_missing=True)
    except Exception:
        logger.debug("failed to load prompt %s", ref, exc_info=True)
        return None

    if prompt is None:
        return None

    template = getattr(prompt, "template", None) or getattr(prompt, "content", None)
    version = getattr(prompt, "version", None)
    name = getattr(prompt, "name", agent_id)

    tags = {}
    raw_tags = getattr(prompt, "tags", None)
    if isinstance(raw_tags, dict):
        tags = raw_tags

    return {
        "agent_id": agent_id,
        "prompt_name": name,
        "version": int(version) if version is not None else None,
        "alias": alias,
        "template_preview": (template[:_TEMPLATE_PREVIEW_CHARS] + "…")
        if template and len(template) > _TEMPLATE_PREVIEW_CHARS
        else template,
        "template_length": len(template) if template else 0,
        "approval_id": tags.get("caliber.approval_id"),
        "artifact_ref": f"prompts:/{agent_id}@{alias}",
    }


# Each prompt list row needs the live registry record for prod/staging/dev.
# Done naively that is one blocking MLflow round-trip per (prompt, alias) — for
# N prompts that is 3N *sequential* network calls, and a single slow/hung
# registry call stalls the entire page (the 6-7 min "stuck pending" symptom).
# The three knobs below bound that: a short cross-request cache, a worker pool
# for concurrency, and an overall deadline so the route always returns quickly.
_PROMPT_INFO_CACHE_TTL_SECONDS = float(os.getenv("CALIBER_PROMPT_INFO_TTL_SECONDS", "10") or "10")
_PROMPT_LOOKUP_WORKERS = max(1, int(os.getenv("CALIBER_PROMPT_LOOKUP_WORKERS", "8") or "8"))
_PROMPT_LOOKUP_TIMEOUT_SECONDS = float(
    os.getenv("CALIBER_PROMPT_LOOKUP_TIMEOUT_SECONDS", "8") or "8"
)

# (agent_id, alias) -> (monotonic_ts, info | None)
_prompt_info_cache: dict[tuple[str, str], tuple[float, dict[str, Any] | None]] = {}

# Reused across requests so we don't spin up a pool per page load. Never shut
# down with wait=True (a hung MLflow call would re-introduce the freeze); the
# overall deadline below is what protects the route.
_prompt_lookup_executor = ThreadPoolExecutor(
    max_workers=_PROMPT_LOOKUP_WORKERS, thread_name_prefix="prompt-lookup"
)


def _reset_prompt_info_cache() -> None:
    """Drop the cached registry records (used by tests for isolation)."""
    _prompt_info_cache.clear()


def _load_prompt_info_cached(agent_id: str, alias: str = "prod") -> dict[str, Any] | None:
    """``_load_prompt_info`` with a short TTL cache to avoid re-hitting MLflow."""
    if _PROMPT_INFO_CACHE_TTL_SECONDS <= 0:
        return _load_prompt_info(agent_id, alias=alias)
    key = (agent_id, alias)
    now = time.monotonic()
    cached = _prompt_info_cache.get(key)
    if cached is not None and (now - cached[0]) < _PROMPT_INFO_CACHE_TTL_SECONDS:
        return cached[1]
    value = _load_prompt_info(agent_id, alias=alias)
    _prompt_info_cache[key] = (now, value)
    return value


def _load_prompt_infos_by_alias(agent_id: str) -> dict[str, dict[str, Any]]:
    """Load every deployed alias we care about for one prompt."""
    out: dict[str, dict[str, Any]] = {}
    for alias in _PROMPT_DISCOVERY_ALIASES:
        info = _load_prompt_info_cached(agent_id, alias=alias)
        if info is not None:
            out[alias] = info
    return out


def _load_prompt_infos_for_names(
    names: list[str] | tuple[str, ...],
) -> dict[str, dict[str, dict[str, Any]]]:
    """Resolve prod/staging/dev records for many prompts concurrently.

    Every (prompt, alias) lookup runs on the shared worker pool, and the whole
    batch is bounded by ``_PROMPT_LOOKUP_TIMEOUT_SECONDS`` — so a slow or
    unreachable registry degrades to "no live info for the lagging rows" in a
    few seconds instead of hanging the page for minutes.
    """
    unique = [name for name in dict.fromkeys(names) if name]
    result: dict[str, dict[str, dict[str, Any]]] = {name: {} for name in unique}
    if not unique:
        return result

    tasks = [(name, alias) for name in unique for alias in _PROMPT_DISCOVERY_ALIASES]
    future_map = {
        _prompt_lookup_executor.submit(_load_prompt_info_cached, name, alias): (
            name,
            alias,
        )
        for name, alias in tasks
    }
    done, not_done = futures_wait(future_map, timeout=_PROMPT_LOOKUP_TIMEOUT_SECONDS)
    for future in done:
        name, alias = future_map[future]
        try:
            info = future.result()
        except Exception:
            logger.debug("prompt lookup failed for %s@%s", name, alias, exc_info=True)
            info = None
        if info is not None:
            result[name][alias] = info
    if not_done:
        logger.warning(
            "prompt registry lookups exceeded %ss deadline; %d of %d still pending",
            _PROMPT_LOOKUP_TIMEOUT_SECONDS,
            len(not_done),
            len(future_map),
        )
        for future in not_done:
            future.cancel()
    return result


def _load_prompt_info_any_alias(agent_id: str) -> dict[str, Any] | None:
    """Load the first available prompt alias, preferring prod over staging/dev."""
    infos = _load_prompt_infos_by_alias(agent_id)
    for alias in _PROMPT_DISCOVERY_ALIASES:
        if alias in infos:
            return infos[alias]
    return None


def _load_prompt_template(agent_id: str, alias: str = "prod") -> str | None:
    """Load the full prompt template content for a concrete alias."""
    mlflow = _get_mlflow_module()
    if mlflow is None:
        return None

    load_prompt = _resolve_api(mlflow, "load_prompt")
    if load_prompt is None:
        return None

    ref = f"prompts:/{agent_id}@{alias}"
    try:
        prompt = load_prompt(ref, allow_missing=True)
    except Exception:
        logger.debug("failed to load prompt template %s", ref, exc_info=True)
        return None
    if prompt is None:
        return None

    template = getattr(prompt, "template", None) or getattr(prompt, "content", None)
    return template if isinstance(template, str) else None


def _safe_prompt_version_number(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _build_mlflow_client() -> Any:
    if _get_mlflow_module() is None:
        raise HTTPException(status_code=503, detail="mlflow not available")
    try:
        from mlflow import MlflowClient  # noqa: PLC0415

        return MlflowClient()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"mlflow client unavailable: {exc}") from exc


def _prompt_versions_from_registry(client: Any, name: str) -> tuple[list[Any], dict[int, set[str]]]:
    search_prompt_versions = getattr(client, "search_prompt_versions", None)
    get_prompt_version_by_alias = getattr(client, "get_prompt_version_by_alias", None)
    if not callable(search_prompt_versions):
        return [], {}
    try:
        versions = list(search_prompt_versions(name))
    except Exception:
        return [], {}

    alias_by_version: dict[int, set[str]] = {}
    for version in versions:
        version_no = _safe_prompt_version_number(getattr(version, "version", 0))
        if version_no is None:
            continue
        for alias_name in getattr(version, "aliases", []) or []:
            alias_by_version.setdefault(version_no, set()).add(str(alias_name))

    if callable(get_prompt_version_by_alias):
        # Prompt alias metadata is incomplete in some MLflow builds.
        # Resolve the deployment aliases we actively use so the UI can
        # still mark the live version correctly.
        for alias_name in ("prod", "staging", "dev"):
            try:
                aliased = get_prompt_version_by_alias(name, alias_name)
            except Exception:  # noqa: S112
                continue
            version_no = _safe_prompt_version_number(getattr(aliased, "version", 0))
            if version_no is None:
                continue
            alias_by_version.setdefault(version_no, set()).add(alias_name)
    return versions, alias_by_version


def _prompt_versions_from_model_registry(
    client: Any, name: str
) -> tuple[list[Any], dict[int, set[str]]] | None:
    try:
        registered = client.get_registered_model(name)
        alias_to_version = dict(getattr(registered, "aliases", {}) or {})
        versions = list(client.search_model_versions(f"name='{name}'"))
    except Exception:
        return None

    alias_by_version: dict[int, set[str]] = {}
    for alias_name, alias_version in alias_to_version.items():
        version_no = _safe_prompt_version_number(alias_version)
        if version_no is None:
            continue
        alias_by_version.setdefault(version_no, set()).add(str(alias_name))
    return versions, alias_by_version


def _prompt_versions_from_prompt_info(name: str) -> list[dict[str, Any]]:
    info = _load_prompt_info_any_alias(name)
    if info is None or info.get("version") is None:
        raise HTTPException(status_code=404, detail=f"prompt {name!r} not found") from None
    version_no = int(info["version"])
    alias = str(info.get("alias") or "prod")
    return [
        {
            "name": name,
            "version": version_no,
            "aliases": [alias],
            "creation_timestamp": None,
            "updated_timestamp": None,
            "run_id": None,
            "source": info.get("artifact_ref") or f"prompts:/{name}/{version_no}",
            "commit_message": None,
            "current": alias == "prod",
        }
    ]


def _serialize_prompt_version_items(
    name: str,
    versions: list[Any],
    alias_by_version: dict[int, set[str]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for version in versions:
        version_no = _safe_prompt_version_number(getattr(version, "version", 0))
        if version_no is None:
            continue
        aliases = sorted(alias_by_version.get(version_no, set()))
        source = getattr(version, "source", None) or f"prompts:/{name}/{version_no}"
        tags = getattr(version, "tags", None) or {}
        commit_message = (
            getattr(version, "commit_message", None)
            or getattr(version, "description", None)
            or tags.get("mlflow.prompt.commit_message")
        )
        items.append(
            {
                "name": name,
                "version": version_no,
                "aliases": aliases,
                "creation_timestamp": getattr(version, "creation_timestamp", None),
                "updated_timestamp": getattr(version, "last_updated_timestamp", None),
                "run_id": getattr(version, "run_id", None),
                "source": source,
                "commit_message": commit_message,
                "current": "prod" in alias_by_version.get(version_no, set()),
            }
        )
    items.sort(key=lambda item: item["version"], reverse=True)
    return items


def _list_prompt_version_items(name: str) -> list[dict[str, Any]]:
    client = _build_mlflow_client()
    versions, alias_by_version = _prompt_versions_from_registry(client, name)
    if not versions:
        model_registry_result = _prompt_versions_from_model_registry(client, name)
        if model_registry_result is None:
            return _prompt_versions_from_prompt_info(name)
        versions, alias_by_version = model_registry_result
    return _serialize_prompt_version_items(name, versions, alias_by_version)


def _search_mlflow_prompts() -> list[dict[str, Any]]:
    """Return all prompts registered in the MLflow Prompt Registry.

    Each item contains ``name``, ``description``, ``creation_timestamp``,
    and ``tags``.  Falls back to an empty list on error or when mlflow
    is unavailable.
    """
    mlflow = _get_mlflow_module()
    if mlflow is None:
        return []

    search_prompts = _resolve_api(mlflow, "search_prompts")
    if search_prompts is None:
        return []

    try:
        results = search_prompts()
    except Exception:
        logger.debug("search_prompts failed", exc_info=True)
        return []

    items: list[dict[str, Any]] = []
    for p in results:
        items.append(
            {
                "name": getattr(p, "name", None),
                "description": getattr(p, "description", None),
                "creation_timestamp": getattr(p, "creation_timestamp", None),
                "tags": getattr(p, "tags", {}),
            }
        )
    return items


def _workflow_agent_prompt_name(workflow_id: str, node_id: str) -> str:
    """Stable prompt name for a workflow agent node."""
    return f"wf-{workflow_id}-{node_id}".replace("/", "-")


def _template_preview(template: str | None, limit: int = 200) -> str | None:
    if not template:
        return None
    return (template[:limit] + "...") if len(template) > limit else template


def _extract_workflow_agent_prompts(session: Session) -> dict[str, dict[str, Any]]:  # noqa: PLR0912
    """Collect prompt candidates from the latest version of each workflow."""
    stmt = (
        select(
            CaliberWorkflow.workflow_id,
            CaliberWorkflow.name,
            CaliberWorkflowVersion.version_number,
            CaliberWorkflowVersion.manifest,
        )
        .join(
            CaliberWorkflowVersion,
            CaliberWorkflowVersion.workflow_id == CaliberWorkflow.workflow_id,
        )
        .order_by(CaliberWorkflow.workflow_id.asc(), CaliberWorkflowVersion.version_number.desc())
    )
    rows = session.execute(stmt).all()

    latest_by_workflow: dict[str, tuple[str, dict[str, Any]]] = {}
    for workflow_id, workflow_name, _version_number, manifest in rows:
        wid = str(workflow_id)
        if wid in latest_by_workflow:
            continue
        latest_by_workflow[wid] = (
            str(workflow_name or wid),
            manifest if isinstance(manifest, dict) else {},
        )

    out: dict[str, dict[str, Any]] = {}
    for workflow_id, (workflow_name, manifest) in latest_by_workflow.items():
        nodes = manifest.get("nodes")
        if not isinstance(nodes, dict):
            continue
        artifacts = manifest.get("artifacts") if isinstance(manifest, dict) else {}
        prompts = artifacts.get("prompts") if isinstance(artifacts, dict) else {}

        for node_key, raw_node in nodes.items():
            if not isinstance(raw_node, dict) or raw_node.get("type") != "agent":
                continue

            node_id = str(raw_node.get("id") or node_key)
            node_name = str(raw_node.get("name") or node_id)
            instructions = raw_node.get("instructions")
            prompt_name = _workflow_agent_prompt_name(workflow_id, node_id)
            inline_template: str | None = None

            if isinstance(instructions, dict):
                instr_type = instructions.get("type")
                if instr_type == "inline":
                    text = instructions.get("text")
                    if isinstance(text, str) and text.strip():
                        inline_template = text.strip()
                elif instr_type == "mlflow_prompt":
                    ref = instructions.get("ref")
                    if isinstance(ref, str) and isinstance(prompts, dict):
                        artifact = prompts.get(ref)
                        if isinstance(artifact, dict):
                            registry_name = artifact.get("registry_name")
                            if isinstance(registry_name, str) and registry_name.strip():
                                prompt_name = registry_name.strip()

            out.setdefault(
                prompt_name,
                {
                    "agent_id": prompt_name,
                    "agent_name": f"{workflow_name} / {node_name}",
                    "agent_enabled": None,
                    "prompt_name": prompt_name,
                    "template_preview": _template_preview(inline_template),
                    "template_length": len(inline_template) if inline_template else 0,
                    "description": f"Workflow agent prompt for {workflow_id}/{node_id}",
                },
            )

    return out


async def list_prompts(request: Request) -> JSONResponse:  # noqa: PLR0912, PLR0915 - merges agent, workflow, and alias-backed prompt rows
    """Return the full prompt inventory: deployed prompts *and* the backlog.

    Each returned prompt is enriched with Caliber agent / workflow-node
    metadata (name, description) when a matching row exists, so deployed
    workflow prompts still read as e.g. "Workflow / Node". Every
    prompt-bearing asset is returned — both prompts with a resolvable
    deployed version (``has_prompt``) and promptless assets (unregistered
    agents + workflow agent nodes whose prompt still lives inline in the
    manifest). Promptless rows carry ``needs_prompt=True`` so the page can
    act as the canonical backlog of "everything that still needs prompt
    work" while the FE keeps them out of flows that require a deployed
    prompt (playground, calibration, selectors).
    """
    require_user(request)
    identity = resolve_identity(request)
    factory = get_session_factory(request)

    # 1. Caliber-registered agents. We load the full rows (not a column subset)
    #    so we can (a) drop auto-provisioned hidden prompt targets from the
    #    backlog, and (b) read each prompt's status/model/dataset off its
    #    optimizer_config without an extra query per row. Scoped to what the
    #    caller may see (project/user/public) — never the whole table.
    with factory() as session:
        stmt = apply_visibility_filter(
            select(CaliberAgentConfig).order_by(CaliberAgentConfig.name),
            CaliberAgentConfig,
            identity,
            identity.active_project_id,
        )
        agent_rows = session.execute(stmt).scalars().all()
        workflow_prompt_map = _extract_workflow_agent_prompts(session)
        # Batch the status signals: which agent_ids have ≥1 test run, and which
        # have an ``applied`` (terminal-success) refinement job. One query each,
        # not N+1 per row.
        tested_agent_ids: set[str] = set(
            session.execute(select(CaliberPromptTestRun.agent_id).distinct()).scalars().all()
        )
        calibrated_agent_ids: set[str] = set(
            session.execute(
                select(CaliberRefinementJob.agent_id)
                .where(CaliberRefinementJob.status == "applied")
                .distinct()
            )
            .scalars()
            .all()
        )

    # Index every target row (hidden or not) by agent_id so prompt rows can read
    # status/model even when the row itself is a hidden runtime identity.
    target_by_name: dict[str, CaliberAgentConfig] = {row.agent_id: row for row in agent_rows}

    agent_map: dict[str, dict[str, Any]] = {}
    for row in agent_rows:
        # Hidden prompt targets are a runtime-identity detail — never surface
        # them as their own backlog ("needs_prompt") inventory entry. Their
        # status/model still attach to the prompt row (via target_by_name).
        if is_hidden_prompt_target(row):
            continue
        agent_map[row.agent_id] = {
            "agent_id": row.agent_id,
            "agent_name": row.name,
            "agent_enabled": row.enabled,
        }

    def _status_and_model(prompt_name: str) -> tuple[str, str | None]:
        target = target_by_name.get(prompt_name)
        cfg = target.optimizer_config if target is not None else None
        cfg = cfg if isinstance(cfg, dict) else {}
        model = cfg.get("model") if isinstance(cfg.get("model"), str) else None
        status = prompt_target_status(
            target=target,
            has_test_run=prompt_name in tested_agent_ids,
            has_applied_job=prompt_name in calibrated_agent_ids,
        )
        return status, model

    # 2. All prompts from MLflow Prompt Registry
    mlflow_prompts = _search_mlflow_prompts()

    # Resolve every prompt's live registry record in ONE bounded, concurrent
    # batch instead of 3 sequential MLflow calls per row inside the loops below.
    candidate_names: list[str] = [str(mp["name"]) for mp in mlflow_prompts if mp.get("name")]
    candidate_names.extend(agent_map.keys())
    candidate_names.extend(workflow_prompt_map.keys())
    infos_by_name = _load_prompt_infos_for_names(candidate_names)

    # 3. Merge: iterate over MLflow prompts first (they have content),
    #    then add Caliber agents that have no MLflow prompt yet.
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    for mp in mlflow_prompts:
        pname = mp["name"]
        if not pname or pname in seen:
            continue
        seen.add(pname)

        info_by_alias = infos_by_name.get(pname, {})
        info = None
        for alias in _PROMPT_DISCOVERY_ALIASES:
            if alias in info_by_alias:
                info = info_by_alias[alias]
                break
        agent = agent_map.get(pname)
        workflow_agent = workflow_prompt_map.get(pname)
        status, model = _status_and_model(pname)

        items.append(
            {
                "agent_id": pname,
                "agent_name": (
                    agent["agent_name"]
                    if agent
                    else workflow_agent["agent_name"]
                    if workflow_agent
                    else pname
                ),
                "agent_enabled": (
                    agent["agent_enabled"]
                    if agent
                    else workflow_agent["agent_enabled"]
                    if workflow_agent
                    else None
                ),
                "prompt_name": info["prompt_name"] if info else pname,
                "version": info["version"] if info else None,
                "alias": info["alias"] if info else "prod",
                "template_preview": info["template_preview"] if info else None,
                "template_length": info["template_length"] if info else 0,
                "approval_id": info["approval_id"] if info else None,
                "artifact_ref": info["artifact_ref"] if info else None,
                "available_aliases": sorted(info_by_alias.keys()),
                "has_prompt": info is not None,
                "needs_prompt": info is None,
                "description": mp.get("description")
                or (workflow_agent["description"] if workflow_agent else None),
                "creation_timestamp": mp.get("creation_timestamp"),
                "source": "both" if agent else "mlflow",
                "status": status,
                "model": model,
            }
        )

    # Caliber agents that don't have an MLflow prompt yet
    for agent_id, meta in agent_map.items():
        if agent_id in seen:
            continue
        seen.add(agent_id)
        info_by_alias = infos_by_name.get(agent_id, {})
        info = None
        for alias in _PROMPT_DISCOVERY_ALIASES:
            if alias in info_by_alias:
                info = info_by_alias[alias]
                break
        status, model = _status_and_model(agent_id)
        items.append(
            {
                "agent_id": agent_id,
                "agent_name": meta["agent_name"],
                "agent_enabled": meta["agent_enabled"],
                "prompt_name": info["prompt_name"] if info else None,
                "version": info["version"] if info else None,
                "alias": info["alias"] if info else "prod",
                "template_preview": info["template_preview"] if info else None,
                "template_length": info["template_length"] if info else 0,
                "approval_id": info["approval_id"] if info else None,
                "artifact_ref": info["artifact_ref"] if info else None,
                "available_aliases": sorted(info_by_alias.keys()),
                "has_prompt": info is not None,
                "needs_prompt": info is None,
                "description": None,
                "creation_timestamp": None,
                "source": "caliber",
                "status": status,
                "model": model,
            }
        )

    # Workflow agent prompts that are not represented by a Caliber agent row yet.
    # These rows let operators create/manage workflow prompts from the same table.
    for prompt_name, meta in workflow_prompt_map.items():
        if prompt_name in seen:
            continue
        seen.add(prompt_name)
        info_by_alias = infos_by_name.get(prompt_name, {})
        info = None
        for alias in _PROMPT_DISCOVERY_ALIASES:
            if alias in info_by_alias:
                info = info_by_alias[alias]
                break
        status, model = _status_and_model(prompt_name)
        items.append(
            {
                "agent_id": prompt_name,
                "agent_name": meta["agent_name"],
                "agent_enabled": None,
                "prompt_name": prompt_name,
                "version": info["version"] if info else None,
                "alias": info["alias"] if info else "prod",
                "template_preview": (
                    info["template_preview"] if info else meta["template_preview"]
                ),
                "template_length": (info["template_length"] if info else meta["template_length"]),
                "approval_id": info["approval_id"] if info else None,
                "artifact_ref": info["artifact_ref"] if info else None,
                "available_aliases": sorted(info_by_alias.keys()),
                "has_prompt": info is not None,
                "needs_prompt": info is None,
                "description": meta["description"],
                "creation_timestamp": None,
                "source": "mlflow",
                "status": status,
                "model": model,
            }
        )

    # Surface the full inventory: deployed prompts AND the promptless backlog
    # (unregistered agents + workflow agent nodes whose prompts live inline in
    # the manifest). Promptless rows are flagged ``needs_prompt`` so the page is
    # the canonical "what still needs prompt work" list; the FE renders them in a
    # separate group and keeps them out of playground/calibration/selectors.
    # Deterministic order: deployed first, then needs-prompt, each alpha by name.
    items.sort(
        key=lambda x: (
            0 if x.get("has_prompt") else 1,
            (x.get("agent_name") or x.get("agent_id") or "").lower(),
        )
    )

    return JSONResponse({"data": items})


async def create_prompt(request: Request) -> JSONResponse:
    """``POST /caliber/prompts`` — register a new prompt in MLflow.

    Body: ``{"name": "...", "template": "...", "commit_message": "...", "tags": {}}``

    Creates the prompt in the MLflow Prompt Registry so it's visible
    from both Caliber and the native MLflow UI.
    """
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    body = await parse_json_object(request)

    name = (body.get("name") or "").strip()
    template = (body.get("template") or "").strip()
    commit_message = body.get("commit_message", "created via CALIBER")
    target_alias = str(body.get("target_alias") or "prod")
    # Optional model the author pins for this prompt; recorded on the hidden
    # runtime target so testing/calibration can default to it.
    raw_model = body.get("model")
    model = raw_model.strip() if isinstance(raw_model, str) and raw_model.strip() else None
    raw_tags = body.get("tags")
    if raw_tags is None:
        tags: dict[str, Any] = {}
    elif isinstance(raw_tags, dict):
        tags = raw_tags
    else:
        raise HTTPException(status_code=400, detail="'tags' must be a dict")

    result = register_prompt_version(
        name=name,
        template=template,
        commit_message=str(commit_message),
        tags=tags,
        source="caliber-ui",
        target_alias=target_alias,
    )

    # Auto-provision the hidden runtime identity so the prompt is immediately
    # testable/calibratable without a separate agent registration. ``name`` is
    # validated by ``register_prompt_version`` above (alnum/-/_ only).
    factory = get_session_factory(request)
    with factory() as session:
        ensure_prompt_target(
            session,
            name,
            owner=actor,
            model=model,
            project_id=identity.active_project_id,
        )
        session.commit()

    return JSONResponse({"data": result}, status_code=201)


async def get_prompt(request: Request) -> JSONResponse:
    """``GET /caliber/prompts/{name}`` — load full prompt details.

    Returns the full template for the active alias (default: ``prod``)
    so the UI can open and edit prompts without relying on truncated
    table previews.
    """
    require_user(request)
    name = request.path_params["name"]
    alias = (request.query_params.get("alias") or "prod").strip() or "prod"

    mlflow = _get_mlflow_module()
    if mlflow is None:
        raise HTTPException(status_code=503, detail="mlflow not available")
    load_prompt = _resolve_api(mlflow, "load_prompt")
    if load_prompt is None:
        raise HTTPException(status_code=503, detail="mlflow prompt registry API not available")

    ref = f"prompts:/{name}@{alias}"
    try:
        prompt = load_prompt(ref, allow_missing=True)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"failed to load prompt: {exc}") from exc

    if prompt is None:
        raise HTTPException(
            status_code=404, detail=f"prompt {name!r} not found for alias {alias!r}"
        )

    template = getattr(prompt, "template", None) or getattr(prompt, "content", None)
    if not isinstance(template, str):
        raise HTTPException(
            status_code=404, detail=f"prompt {name!r} not found for alias {alias!r}"
        )

    version = getattr(prompt, "version", None)
    prompt_name = getattr(prompt, "name", name)

    return JSONResponse(
        {
            "data": {
                "name": str(prompt_name),
                "version": int(version) if version is not None else None,
                "alias": alias,
                "template": template,
                "template_length": len(template),
                "artifact_ref": ref,
            }
        }
    )


async def create_prompt_version(request: Request) -> JSONResponse:
    """``POST /caliber/prompts/{name}/versions`` — add a new version.

    Body: ``{"template": "...", "commit_message": "..."}``

    Registers a new version of an existing prompt in MLflow.
    """
    require_scopes(request, [SCOPE_OPERATOR])
    name = request.path_params["name"]
    body = await parse_json_object(request)

    template = (body.get("template") or "").strip()
    commit_message = body.get("commit_message", "new version via CALIBER")

    # ``promote`` decouples authoring from going live. When false, the version is
    # registered but NO alias is rotated, so a developer can iterate / evaluate a
    # candidate before it serves the live alias. Defaults to true for backward
    # compatibility (the historical behavior always promoted to ``prod``).
    promote = body.get("promote", True)
    if not isinstance(promote, bool):
        raise HTTPException(status_code=400, detail="'promote' must be a boolean")
    raw_target_alias = body.get("target_alias")
    target_alias: str | None = None
    if promote:
        target_alias = str(raw_target_alias) if raw_target_alias else "prod"

    raw_tags = body.get("tags")
    if raw_tags is None:
        tags: dict[str, Any] = {}
    elif isinstance(raw_tags, dict):
        tags = raw_tags
    else:
        raise HTTPException(status_code=400, detail="'tags' must be a dict")

    result = register_prompt_version(
        name=name,
        template=template,
        commit_message=str(commit_message),
        tags=tags,
        source="caliber-ui",
        target_alias=target_alias,
        set_prod_alias=promote,
    )

    return JSONResponse({"data": result}, status_code=201)


async def delete_prompt(request: Request) -> JSONResponse:
    """``DELETE /caliber/prompts/{name}`` — permanently delete a prompt.

    Admin-only and irreversible: removes the prompt, all its versions, and
    its aliases from the MLflow Prompt Registry. Tries the direct
    ``delete_prompt`` first and, if the registry refuses while versions/aliases
    remain, cascades (clear aliases → delete each version → delete the prompt).
    """
    actor = require_scopes(request, [SCOPE_ADMIN])
    clean_name = (request.path_params["name"] or "").strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="'name' is required")

    client = _build_mlflow_client()
    try:
        client.delete_prompt(clean_name)
    except Exception as direct_exc:
        logger.info(
            "delete_prompt(%s) direct failed (%s); cascading versions+aliases",
            clean_name,
            direct_exc,
        )
        try:
            for item in _list_prompt_version_items(clean_name):
                for alias in item.get("aliases") or []:
                    try:
                        client.delete_prompt_alias(clean_name, alias)
                    except Exception:
                        logger.debug(
                            "delete_prompt_alias %s@%s failed", clean_name, alias, exc_info=True
                        )
                try:
                    client.delete_prompt_version(clean_name, str(item["version"]))
                except Exception:
                    logger.debug(
                        "delete_prompt_version %s v%s failed",
                        clean_name,
                        item.get("version"),
                        exc_info=True,
                    )
            client.delete_prompt(clean_name)
        except Exception as cascade_exc:
            logger.exception("delete_prompt cascade failed for %s", clean_name)
            raise HTTPException(
                status_code=502, detail=f"failed to delete prompt: {cascade_exc}"
            ) from cascade_exc

    factory = get_session_factory(request)
    with factory() as session:
        audit_record(
            session,
            actor=actor,
            action="prompt.delete",
            entity_type="prompt",
            entity_id=clean_name,
        )

    return JSONResponse({"data": {"deleted": clean_name}})


async def list_prompt_versions(request: Request) -> JSONResponse:
    """``GET /caliber/prompts/{name}/versions`` — list prompt versions.

    Uses MLflow model registry version metadata for prompt history and
    includes aliases that point to each version.
    """
    require_user(request)
    name = request.path_params["name"]
    return JSONResponse({"data": _list_prompt_version_items(name)})


async def get_prompt_version(request: Request) -> JSONResponse:
    """``GET /caliber/prompts/{name}/versions/{version}`` — load full template for a specific version."""
    require_user(request)
    name = request.path_params["name"]
    version_raw = request.path_params["version"]

    try:
        version = int(version_raw)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="'version' must be an integer") from exc

    mlflow = _get_mlflow_module()
    if mlflow is None:
        raise HTTPException(status_code=503, detail="mlflow not available")

    load_prompt = _resolve_api(mlflow, "load_prompt")
    if load_prompt is None:
        raise HTTPException(status_code=503, detail="mlflow prompt registry API not available")

    ref = f"prompts:/{name}/{version}"
    try:
        prompt = load_prompt(ref, allow_missing=True)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"failed to load prompt version: {exc}"
        ) from exc

    if prompt is None:
        raise HTTPException(status_code=404, detail=f"prompt {name!r} version {version} not found")

    template = getattr(prompt, "template", None) or getattr(prompt, "content", None) or ""
    prompt_name = getattr(prompt, "name", name)

    return JSONResponse(
        {
            "data": {
                "name": str(prompt_name),
                "version": version,
                "template": template,
                "template_length": len(template),
                "artifact_ref": ref,
            }
        }
    )


def set_prompt_alias_version(*, name: str, alias: str, version: int | str) -> dict[str, Any]:
    """Point a prompt alias at a concrete version via MLflow Prompt Registry."""
    if not re.match(r"^[a-zA-Z0-9_-]+$", alias):
        raise HTTPException(status_code=400, detail="invalid alias")

    try:
        version_num = int(version)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="'version' must be an integer") from exc

    mlflow = _get_mlflow_module()
    if mlflow is None:
        raise HTTPException(status_code=503, detail="mlflow not available")

    set_alias = _resolve_api(mlflow, "set_prompt_alias")
    if set_alias is None:
        raise HTTPException(status_code=503, detail="mlflow prompt alias API not available")

    try:
        set_alias(name, alias, version_num)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"failed to set alias: {exc}") from exc

    return {"name": name, "alias": alias, "version": version_num}


def _extract_gate_details(body: dict[str, Any]) -> dict[str, Any]:
    """Pull the optional advisory-gate / override fields off a promote body.

    The eval gate is *advisory* in v1 — there is no rotation-boundary
    enforcement — so these fields are recorded on the audit row purely so an
    operator override is attributable (who promoted past a FAIL, and why).
    """
    details: dict[str, Any] = {}
    gate_state = body.get("gate_state")
    if gate_state is not None:
        if not isinstance(gate_state, str) or gate_state not in GATE_STATES:
            raise HTTPException(
                status_code=400,
                detail=f"'gate_state' must be one of {sorted(GATE_STATES)}",
            )
        details["gate_state"] = gate_state
    gate_score = body.get("gate_score")
    if gate_score is not None:
        if isinstance(gate_score, bool) or not isinstance(gate_score, (int, float)):
            raise HTTPException(status_code=400, detail="'gate_score' must be a number")
        details["gate_score"] = float(gate_score)
    overridden = body.get("overridden", False)
    if not isinstance(overridden, bool):
        raise HTTPException(status_code=400, detail="'overridden' must be a boolean")
    details["overridden"] = overridden
    override_reason = body.get("override_reason")
    if override_reason is not None:
        if not isinstance(override_reason, str):
            raise HTTPException(status_code=400, detail="'override_reason' must be a string")
        details["override_reason"] = override_reason
    return details


async def set_prompt_alias(request: Request) -> JSONResponse:
    """``POST /caliber/prompts/{name}/aliases/{alias}`` — audited promote.

    Captures the alias's current (outgoing) version *before* rotating so the
    exact previously-live target is recorded, rotates the MLflow alias, then
    writes an auditable ``promote_prompt`` row carrying the advisory-gate
    verdict and any operator override. That audit row is the data source for
    the Releases timeline and for the per-version ``wasLiveUntil`` pointer that
    :func:`rollback_prompt` reads — replacing the prior path that wrote nothing.
    """
    actor = require_scopes(request, [SCOPE_OPERATOR])
    name = request.path_params["name"]
    alias = request.path_params["alias"]
    body = await parse_json_object(request)

    raw_version = body.get("version")
    if not isinstance(raw_version, (int, str)):
        raise HTTPException(status_code=400, detail="'version' must be an integer")

    gate_details = _extract_gate_details(body)

    # Read the currently-live version BEFORE rotating so rollback is exact
    # (not derived from ``version_after - 1``). ``None`` on cold start.
    previous_info = _load_prompt_info(name, alias)
    previous_version = previous_info.get("version") if previous_info else None

    result = set_prompt_alias_version(name=name, alias=alias, version=raw_version)
    to_version = result["version"]

    factory = get_session_factory(request)
    with factory() as session:
        audit_record(
            session,
            actor=actor,
            action="promote_prompt",
            entity_type="prompt",
            entity_id=name,
            details={
                "alias": alias,
                "from_version": previous_version,
                "to_version": to_version,
                **gate_details,
            },
        )
        # Persist the operator-supplied verdict keyed by the now-live version so
        # the Version panel can read it back (advisory gate; never blocks).
        gate_state = gate_details.get("gate_state")
        if isinstance(gate_state, str):
            record_gate_verdict(
                session,
                artifact_type="prompt",
                version_key=str(to_version),
                state=gate_state,
                score=gate_details.get("gate_score"),
            )
        session.commit()

    result["previous_live_version"] = previous_version
    return JSONResponse({"data": result})


def _previous_live_version(session: Session, name: str, alias: str, current: int) -> int | None:
    """The version that was live on ``alias`` immediately before ``current``.

    Read from the audit trail: the most recent promote/rollback row for this
    prompt+alias whose recorded ``to_version`` equals the version currently
    live. Its ``from_version`` is the exact target to roll back to. Walking the
    trail this way makes repeated rollbacks hop backwards through real promotion
    history rather than guessing ``current - 1``.
    """
    rows = (
        session.execute(
            select(CaliberAuditLog)
            .where(CaliberAuditLog.entity_type == "prompt")
            .where(CaliberAuditLog.entity_id == name)
            .where(CaliberAuditLog.action.in_(("promote_prompt", "rollback_prompt")))
            .order_by(CaliberAuditLog.timestamp.desc(), CaliberAuditLog.log_id.desc())
            .limit(100)
        )
        .scalars()
        .all()
    )
    for row in rows:
        details = row.details or {}
        if details.get("alias") != alias:
            continue
        if details.get("to_version") == current:
            target = details.get("from_version")
            return int(target) if isinstance(target, int) else None
    return None


async def rollback_prompt(request: Request) -> JSONResponse:
    """``POST /caliber/prompts/{name}/rollback`` — roll the live alias back.

    Rotates the alias back to the exact version that was live before the
    current one (read from the ``promote_prompt`` audit trail) and records a
    ``rollback_prompt`` row. Returns 409 when there is no recorded prior live
    version to restore. Body (optional): ``{"alias": "prod"}``.
    """
    actor = require_scopes(request, [SCOPE_OPERATOR])
    name = request.path_params["name"]
    body = await parse_json_object(request, allow_empty=True)

    raw_alias = body.get("alias")
    alias = raw_alias.strip() if isinstance(raw_alias, str) and raw_alias.strip() else "prod"
    if not re.match(r"^[a-zA-Z0-9_-]+$", alias):
        raise HTTPException(status_code=400, detail="invalid alias")

    current_info = _load_prompt_info(name, alias)
    current = current_info.get("version") if current_info else None
    if current is None:
        raise HTTPException(
            status_code=409,
            detail=f"prompt {name!r} has no live version on alias {alias!r} to roll back",
        )

    factory = get_session_factory(request)
    with factory() as session:
        target = _previous_live_version(session, name, alias, int(current))
        if target is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"no recorded prior live version for {name!r}@{alias!r}; "
                    "rollback needs an audited promotion to restore"
                ),
            )
        set_prompt_alias_version(name=name, alias=alias, version=target)
        audit_record(
            session,
            actor=actor,
            action="rollback_prompt",
            entity_type="prompt",
            entity_id=name,
            details={"alias": alias, "from_version": int(current), "to_version": target},
        )
        session.commit()

    return JSONResponse(
        {
            "data": {
                "name": name,
                "alias": alias,
                "version": target,
                "rolled_back_from": int(current),
            }
        }
    )


async def test_render_prompt(request: Request) -> JSONResponse:
    """``POST /caliber/prompts/{agent_id}/test-render``

    Render a deployed prompt template with user-supplied variables.
    Returns the rendered output, detected variables, and metadata.
    """
    agent_id = request.path_params["agent_id"]
    require_scopes(request, [SCOPE_OPERATOR])
    body = await parse_json_object(request)
    raw_variables = body.get("variables", {})
    if not isinstance(raw_variables, dict):
        raise HTTPException(status_code=400, detail="'variables' must be a dict")
    variables: dict[str, str] = {str(k): str(v) for k, v in raw_variables.items()}

    factory = get_session_factory(request)
    with factory() as session:
        agent = session.get(CaliberAgentConfig, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"agent {agent_id!r} not found")
        agent_name = agent.name

    start = time.monotonic()

    # Load the prompt template from MLflow
    template: str | None = None
    info = _load_prompt_info_any_alias(agent_id)
    if info and info.get("template_preview"):
        prompt_alias = str(info.get("alias") or "prod")
        template = _load_prompt_template(agent_id, alias=prompt_alias)

    if template is None:
        # Generate a demo template for playground purposes
        template = (
            "You are {{agent_name}}, a helpful AI assistant.\n\n"
            "Your role is to assist users with {{task_type}} tasks.\n"
            "Always respond in {{language}} and keep responses {{tone}}.\n\n"
            "User context: {{user_context}}\n\n"
            "Follow these guidelines:\n"
            "1. Be accurate and helpful\n"
            "2. Ask clarifying questions when needed\n"
            "3. Provide examples when appropriate\n"
            "4. Respect user preferences set in {{preferences}}"
        )

    # Detect {{variable}} placeholders
    detected_vars = sorted(set(re.findall(r"\{\{(\w+)\}\}", template)))

    # Perform substitution
    rendered = template
    for var_name, var_value in variables.items():
        rendered = rendered.replace("{{" + var_name + "}}", str(var_value))

    unresolved = sorted(set(re.findall(r"\{\{(\w+)\}\}", rendered)))
    duration_ms = round((time.monotonic() - start) * 1000, 1)

    return JSONResponse(
        {
            "data": {
                "agent_id": agent_id,
                "agent_name": agent_name,
                "rendered_content": rendered,
                "original_template": template,
                "detected_variables": detected_vars,
                "unresolved_variables": unresolved,
                "variables_applied": variables,
                "version": info["version"] if info else None,
                "word_count": len(rendered.split()),
                "char_count": len(rendered),
                "duration_ms": duration_ms,
            }
        }
    )


async def get_prompt_template_library(request: Request) -> JSONResponse:
    """Return the prompt-builder catalog used by the Create Prompt flow."""
    require_user(request)
    return JSONResponse({"data": list_prompt_template_catalog()})


async def preview_prompt_template_route(request: Request) -> JSONResponse:
    """Compile a prompt-builder recipe into a single prompt + validation report."""
    require_scopes(request, [SCOPE_OPERATOR])
    body = await parse_json_object(request, allow_empty=True)

    base_template_id = str(body.get("base_template_id") or "").strip()
    if not base_template_id:
        raise HTTPException(status_code=400, detail="'base_template_id' is required")

    raw_modifier_ids = body.get("modifier_ids") or []
    if not isinstance(raw_modifier_ids, list):
        raise HTTPException(status_code=400, detail="'modifier_ids' must be an array")
    modifier_ids = [str(item) for item in raw_modifier_ids]

    raw_builder_values = body.get("builder_values") or {}
    if not isinstance(raw_builder_values, dict):
        raise HTTPException(status_code=400, detail="'builder_values' must be an object")
    builder_values = {str(key): str(value) for key, value in raw_builder_values.items()}

    raw_preview_variables = body.get("preview_variables") or {}
    if not isinstance(raw_preview_variables, dict):
        raise HTTPException(status_code=400, detail="'preview_variables' must be an object")
    preview_variables = {str(key): str(value) for key, value in raw_preview_variables.items()}

    raw_runtime_variables = body.get("runtime_variables") or []
    if not isinstance(raw_runtime_variables, list):
        raise HTTPException(status_code=400, detail="'runtime_variables' must be an array")

    template_override_raw = body.get("template_override")
    if template_override_raw is not None and not isinstance(template_override_raw, str):
        raise HTTPException(status_code=400, detail="'template_override' must be a string")

    raw_section_overrides = body.get("section_overrides") or {}
    if not isinstance(raw_section_overrides, dict):
        raise HTTPException(status_code=400, detail="'section_overrides' must be an object")
    section_overrides = {str(key): str(value) for key, value in raw_section_overrides.items()}

    try:
        preview = preview_prompt_template(
            base_template_id=base_template_id,
            modifier_ids=modifier_ids,
            builder_values=builder_values,
            preview_variables=preview_variables,
            runtime_variables=[str(item) for item in raw_runtime_variables],
            template_override=template_override_raw,
            section_overrides=section_overrides,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return JSONResponse({"data": preview})


async def get_prompt_optimization_options(request: Request) -> JSONResponse:
    """Return optimizer/scorer capabilities for manual prompt calibration runs."""
    require_user(request)
    scorer_options, _scorer_index, default_scorers, runtime = _build_scorer_capabilities()
    return JSONResponse(
        {
            "data": {
                "optimizers": list(_SUPPORTED_PROMPT_OPTIMIZERS),
                "default_optimizer": "MetaPrompt",
                "scorers": scorer_options,
                "default_scorers": default_scorers,
                "default_gate": {
                    "min_aggregate_score": DEFAULT_MIN_AGGREGATE_SCORE,
                    "max_regression_delta": DEFAULT_MAX_REGRESSION_DELTA,
                },
                "runtime": runtime,
            }
        }
    )


async def create_prompt_optimization_run(request: Request) -> JSONResponse:
    """Queue a manual prompt calibration run from the Prompts tab."""
    body = await parse_json_object(request)
    payload = PromptOptimizationRunRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)

    factory = get_session_factory(request)
    with factory() as session:
        response = enqueue_prompt_optimization_run(
            session=session,
            payload=payload,
            actor=actor,
            project_id=identity.active_project_id,
        )

    return JSONResponse({"data": response.model_dump(mode="json")}, status_code=201)


# ---------------------------------------------------------------------------
# Ad-hoc prompt-test runs — durable history for the Prompts-tab test runner.
# ---------------------------------------------------------------------------


def _aggregate_test_run(
    results: list[Any],
) -> tuple[int, int, int, int, float | None]:
    """Recompute (size, passed, failed, partial, overall_score) from results.

    The client never supplies aggregates — they're derived here so a buggy or
    malicious payload can't desync the durable summary from the per-case data.
    """
    size = len(results)
    passed = sum(1 for r in results if r.verdict == "pass")
    failed = sum(1 for r in results if r.verdict == "fail")
    partial = sum(1 for r in results if r.verdict == "partial")
    overall = (sum(r.score for r in results) / size) if size else None
    return size, passed, failed, partial, overall


async def create_prompt_test_run(request: Request) -> JSONResponse:
    """``POST /caliber/prompts/test-runs`` — persist a completed prompt-test run.

    The browser test runner POSTs the assembled per-case ``results`` plus a
    prompt-identity snapshot. The server recomputes the count/score summary
    (never trusting client aggregates) and stores one durable row.
    """
    body = await parse_json_object(request)
    payload = PromptTestRunCreateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)

    if not payload.results:
        raise HTTPException(status_code=400, detail="'results' must not be empty")

    size, passed, failed, partial, overall = _aggregate_test_run(payload.results)
    now = datetime.now(timezone.utc)

    factory = get_session_factory(request)
    with factory() as session:
        # Auto-provision the hidden runtime identity so the run always has a
        # valid target — prompt testing never requires registering an agent.
        ensure_prompt_target(
            session,
            payload.agent_id,
            owner=actor,
            model=payload.model,
            project_id=identity.active_project_id,
        )
        run = CaliberPromptTestRun(
            test_run_id=new_prompt_test_run_id(),
            agent_id=payload.agent_id,
            prompt_name=payload.prompt_name,
            prompt_alias=payload.prompt_alias,
            prompt_version=payload.prompt_version,
            model=payload.model,
            eval_dataset_id=payload.eval_dataset_id,
            test_set_size=size,
            passed_count=passed,
            failed_count=failed,
            partial_count=partial,
            overall_score=overall,
            results=[r.model_dump(mode="json") for r in payload.results],
            trace_id=payload.trace_id,
            mlflow_run_id=payload.mlflow_run_id,
            created_by=actor,
            status="completed",
            completed_at=now,
        )
        session.add(run)
        session.flush()

        audit_record(
            session,
            actor=actor,
            action="create_prompt_test_run",
            entity_type="prompt_test_run",
            entity_id=run.test_run_id,
            details={
                "agent_id": run.agent_id,
                "prompt_name": run.prompt_name,
                "prompt_alias": run.prompt_alias,
                "test_set_size": size,
                "passed_count": passed,
                "failed_count": failed,
                "partial_count": partial,
            },
        )
        session.commit()
        summary = PromptTestRunSummary.model_validate(run)

    return JSONResponse({"data": summary.model_dump(mode="json")}, status_code=201)


async def list_prompt_test_runs(request: Request) -> JSONResponse:
    """``GET /caliber/prompts/test-runs`` — newest-first run history (summaries).

    ``agent_id`` filters to one agent (all agents if omitted). ``limit`` defaults
    to 20 and is capped at 100. The heavy per-case ``results`` array is omitted.
    """
    require_user(request)
    agent_id = request.query_params.get("agent_id")

    raw_limit = request.query_params.get("limit")
    limit = _TEST_RUNS_DEFAULT_LIMIT
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="'limit' must be an integer") from exc
        if limit < 1:
            raise HTTPException(status_code=400, detail="'limit' must be >= 1")
    limit = min(limit, _TEST_RUNS_MAX_LIMIT)

    stmt = select(CaliberPromptTestRun)
    if agent_id:
        stmt = stmt.where(CaliberPromptTestRun.agent_id == agent_id)
    stmt = stmt.order_by(CaliberPromptTestRun.created_at.desc()).limit(limit)

    factory = get_session_factory(request)
    with factory() as session:
        rows = session.execute(stmt).scalars().all()
        summaries = [
            PromptTestRunSummary.model_validate(row).model_dump(mode="json") for row in rows
        ]

    return JSONResponse({"data": summaries})


async def get_prompt_test_run(request: Request) -> JSONResponse:
    """``GET /caliber/prompts/test-runs/{test_run_id}`` — full run incl. results."""
    require_user(request)
    test_run_id = request.path_params["test_run_id"]

    factory = get_session_factory(request)
    with factory() as session:
        run = session.get(CaliberPromptTestRun, test_run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"test run {test_run_id!r} not found")
        detail = PromptTestRunDetail.model_validate(run)

    return JSONResponse({"data": detail.model_dump(mode="json")})


# ---------------------------------------------------------------------------
# Prompt workspace + bind ("pytest for prompts")
# ---------------------------------------------------------------------------


async def get_prompt_workspace(request: Request) -> JSONResponse:
    """``GET /caliber/prompts/{name}/workspace`` — runtime facts + lifecycle status.

    Returns the prompt's pinned model, current resolved registry version, the
    computed lifecycle ``status`` (Bound > Calibrated > Tested > Has test set >
    Draft), the bind target, the associated dataset, and the latest test-run
    summary. ``model``/``bound_to``/``dataset_id`` are read off the hidden
    runtime target's ``optimizer_config`` (null when no target row exists yet).
    """
    require_user(request)
    name = request.path_params["name"]

    # Resolve the current registry version (same path the prompt list uses).
    info = _load_prompt_info_any_alias(name)
    version = info["version"] if info else None

    factory = get_session_factory(request)
    with factory() as session:
        target = session.get(CaliberAgentConfig, name)
        cfg = target.optimizer_config if target is not None else None
        cfg = cfg if isinstance(cfg, dict) else {}

        has_test_run = (
            session.execute(
                select(CaliberPromptTestRun.test_run_id)
                .where(CaliberPromptTestRun.agent_id == name)
                .limit(1)
            ).first()
            is not None
        )
        has_applied_job = (
            session.execute(
                select(CaliberRefinementJob.job_id)
                .where(CaliberRefinementJob.agent_id == name)
                .where(CaliberRefinementJob.status == "applied")
                .limit(1)
            ).first()
            is not None
        )

        latest_run = (
            session.execute(
                select(CaliberPromptTestRun)
                .where(CaliberPromptTestRun.agent_id == name)
                .order_by(CaliberPromptTestRun.created_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        last_run = (
            PromptWorkspaceLastRun.model_validate(latest_run) if latest_run is not None else None
        )

        status = prompt_target_status(
            target=target,
            has_test_run=has_test_run,
            has_applied_job=has_applied_job,
        )

        # Surface the pinned baseline run (if any) plus a cheap summary so the
        # Runs tab can mark/compare against it without a second round-trip. A
        # stale id (run since deleted, or no longer belonging to this prompt) is
        # treated as no baseline.
        raw_baseline_id = cfg.get("baseline_run_id")
        baseline_run_id = raw_baseline_id if isinstance(raw_baseline_id, str) else None
        baseline_run: PromptWorkspaceLastRun | None = None
        if baseline_run_id:
            baseline_row = session.get(CaliberPromptTestRun, baseline_run_id)
            if baseline_row is not None and baseline_row.agent_id == name:
                baseline_run = PromptWorkspaceLastRun.model_validate(baseline_row)
            else:
                baseline_run_id = None

    bound_to = cfg.get("bound_to") if isinstance(cfg.get("bound_to"), dict) else None
    response = PromptWorkspaceResponse(
        model=cfg.get("model") if isinstance(cfg.get("model"), str) else None,
        version=version,
        status=status,
        bound_to=bound_to,
        dataset_id=cfg.get("dataset_id") if isinstance(cfg.get("dataset_id"), str) else None,
        last_run=last_run,
        baseline_run_id=baseline_run_id,
        baseline_run=baseline_run,
    )
    return JSONResponse({"data": response.model_dump(mode="json")})


async def bind_prompt(request: Request) -> JSONResponse:
    """``POST /caliber/prompts/{name}/bind`` — record where a prompt is wired in.

    Records ``bound_to`` on the prompt's hidden runtime target (which is created
    if it doesn't yet exist), then performs the per-kind wiring:

    * ``agent`` — point the named real agent at this prompt by recording a
      ``prompt`` alias link on the agent's ``optimizer_config``.
    * ``workflow_node`` — best-effort rewrite the manifest node ``instructions``
      to an ``mlflow_prompt`` ref for this prompt (see limitation note below).
    * ``standalone`` — just record ``bound_to``.
    """
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    name = request.path_params["name"]
    body = await parse_json_object(request)
    payload = PromptBindRequest.model_validate(body)

    if payload.kind == "agent" and not payload.agent_id:
        raise HTTPException(status_code=400, detail="kind 'agent' requires 'agent_id'")
    if payload.kind == "workflow_node" and not (payload.workflow_id and payload.node_id):
        raise HTTPException(
            status_code=400,
            detail="kind 'workflow_node' requires 'workflow_id' and 'node_id'",
        )

    bound_to: dict[str, Any] = {"kind": payload.kind}
    if payload.kind == "agent":
        bound_to["agent_id"] = payload.agent_id
    elif payload.kind == "workflow_node":
        bound_to["workflow_id"] = payload.workflow_id
        bound_to["node_id"] = payload.node_id

    factory = get_session_factory(request)
    with factory() as session:
        target = ensure_prompt_target(
            session,
            name,
            owner=actor,
            project_id=identity.active_project_id,
        )
        if isinstance(target.optimizer_config, dict):
            target.optimizer_config = {**target.optimizer_config, "bound_to": bound_to}

        if payload.kind == "agent":
            agent = session.get(CaliberAgentConfig, payload.agent_id)
            if agent is None:
                raise HTTPException(status_code=404, detail=f"agent {payload.agent_id!r} not found")
            # Point the real agent at this prompt: record the prompt link on the
            # agent's optimizer_config so the runtime resolves this prompt name.
            if isinstance(agent.optimizer_config, dict):
                agent.optimizer_config = {**agent.optimizer_config, "prompt": name}
        elif payload.kind == "workflow_node":
            # Best-effort: rewrite the latest version's manifest node instructions
            # to an mlflow_prompt ref. Updating the manifest in place across the
            # artifacts map + version history is non-trivial (versions are
            # immutable-by-convention and a deploy re-syncs them), so for Phase 1
            # we record ``bound_to`` and leave the manifest rewrite to the
            # workflow editor / deploy path. The binding is still durable and the
            # workspace status flips to Bound.
            pass

        audit_record(
            session,
            actor=actor,
            action="bind_prompt",
            entity_type="prompt",
            entity_id=name,
            details={"bound_to": bound_to},
        )
        session.commit()

    return JSONResponse({"data": {"bound_to": bound_to, "status": "Bound"}}, status_code=200)


async def set_prompt_baseline(request: Request) -> JSONResponse:
    """``POST /caliber/prompts/{name}/baseline`` — pin a run as the comparison baseline.

    Validates that ``test_run_id`` refers to an existing prompt-test run AND that
    the run belongs to this prompt (``agent_id == name``); 404 if the run is
    missing, 400 if it belongs to a different prompt. On success the id is
    recorded on the prompt's hidden runtime target under
    ``optimizer_config.baseline_run_id`` (same precedent as ``bound_to``) so the
    Runs tab can diff the current run against it.
    """
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    name = request.path_params["name"]
    body = await parse_json_object(request)
    payload = PromptBaselineRequest.model_validate(body)

    factory = get_session_factory(request)
    with factory() as session:
        run = session.get(CaliberPromptTestRun, payload.test_run_id)
        if run is None:
            raise HTTPException(
                status_code=404, detail=f"test run {payload.test_run_id!r} not found"
            )
        if run.agent_id != name:
            raise HTTPException(
                status_code=400,
                detail=(f"test run {payload.test_run_id!r} does not belong to prompt {name!r}"),
            )

        target = ensure_prompt_target(
            session,
            name,
            owner=actor,
            project_id=identity.active_project_id,
        )
        if isinstance(target.optimizer_config, dict):
            target.optimizer_config = {
                **target.optimizer_config,
                "baseline_run_id": payload.test_run_id,
            }

        audit_record(
            session,
            actor=actor,
            action="set_prompt_baseline",
            entity_type="prompt",
            entity_id=name,
            details={"baseline_run_id": payload.test_run_id},
        )
        session.commit()

    return JSONResponse({"data": {"baseline_run_id": payload.test_run_id}}, status_code=200)


def register(app: Starlette) -> None:
    app.routes.append(Route(LIST_PATH, list_prompts, methods=["GET"]))
    app.routes.append(Route(CREATE_PATH, create_prompt, methods=["POST"]))
    app.routes.append(Route(TEMPLATE_LIBRARY_PATH, get_prompt_template_library, methods=["GET"]))
    app.routes.append(Route(TEMPLATE_PREVIEW_PATH, preview_prompt_template_route, methods=["POST"]))
    app.routes.append(
        Route(CALIBRATION_OPTIONS_PATH, get_prompt_optimization_options, methods=["GET"])
    )
    app.routes.append(
        Route(CALIBRATION_RUNS_PATH, create_prompt_optimization_run, methods=["POST"])
    )
    app.routes.append(
        Route(OPTIMIZATION_OPTIONS_PATH, get_prompt_optimization_options, methods=["GET"])
    )
    app.routes.append(
        Route(OPTIMIZATION_RUNS_PATH, create_prompt_optimization_run, methods=["POST"])
    )
    # Test-run routes must precede the ``{name}`` DETAIL_PATH so ``/prompts/
    # test-runs`` resolves to these handlers rather than being captured as a
    # prompt named "test-runs".
    app.routes.append(Route(TEST_RUNS_PATH, create_prompt_test_run, methods=["POST"]))
    app.routes.append(Route(TEST_RUNS_PATH, list_prompt_test_runs, methods=["GET"]))
    app.routes.append(Route(TEST_RUN_DETAIL_PATH, get_prompt_test_run, methods=["GET"]))
    app.routes.append(Route(WORKSPACE_PATH, get_prompt_workspace, methods=["GET"]))
    app.routes.append(Route(BIND_PATH, bind_prompt, methods=["POST"]))
    app.routes.append(Route(BASELINE_PATH, set_prompt_baseline, methods=["POST"]))
    app.routes.append(Route(DETAIL_PATH, get_prompt, methods=["GET"]))
    app.routes.append(Route(DETAIL_PATH, delete_prompt, methods=["DELETE"]))
    app.routes.append(Route(VERSION_PATH, create_prompt_version, methods=["POST"]))
    app.routes.append(Route(VERSIONS_LIST_PATH, list_prompt_versions, methods=["GET"]))
    app.routes.append(Route(VERSION_DETAIL_PATH, get_prompt_version, methods=["GET"]))
    app.routes.append(Route(ALIAS_SET_PATH, set_prompt_alias, methods=["POST"]))
    app.routes.append(Route(ROLLBACK_PATH, rollback_prompt, methods=["POST"]))
    app.routes.append(Route(TEST_RENDER_PATH, test_render_prompt, methods=["POST"]))
