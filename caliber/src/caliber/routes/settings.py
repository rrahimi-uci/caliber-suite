"""``/caliber/settings/runtime`` — safe runtime configuration inventory.

The resolved :class:`caliber.config.CaliberConfig` is frozen and mostly
environment-backed. This route gives the SPA an auditable, grouped inventory of
the knobs CALIBER understands without exposing secret values or pretending those
deployment-level settings can be changed safely in-process.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import Any

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.auth import SCOPE_ADMIN, SCOPE_OPERATOR, require_scopes
from caliber.config import CaliberConfig
from caliber.eval.gate import DEFAULT_MAX_REGRESSION_DELTA, DEFAULT_MIN_AGGREGATE_SCORE
from caliber.routes._deps import envelope_response_dict, parse_json_object
from caliber.runtime_advisories import (
    RuntimeDependencyAdvisory,
    get_runtime_dependency_advisories,
)
from caliber.secrets import resolve_secret

PREFIX = "/ajax-api/2.0/mlflow/caliber"
RUNTIME_PATH = PREFIX + "/settings/runtime"
LLM_PATH = PREFIX + "/settings/llm"

# When the configured key source is a bare env-var name we write straight to it;
# a ``file://`` / ``env://`` source can't be set in-process, so we fall back to
# the conventional variable name.
_OPENAI_KEY_ENV_FALLBACK = "OPENAI_API_KEY"
_ANTHROPIC_KEY_ENV = "ANTHROPIC_API_KEY"
# Below this length, showing the last 4 characters would leak most of the
# secret, so the fingerprint collapses to a bare mask instead.
_FINGERPRINT_MIN_LENGTH = 8

_DEFAULT_CONFIG = CaliberConfig.load(environ={})
_BYTES_PER_UNIT = 1024
_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600


def _mask_dsn(value: str) -> str:
    """Mask passwords in URL-ish database strings."""
    return re.sub(r"(://[^:/@\s]+:)[^@\s]+(@)", r"\1********\2", value)


def _csv_count(value: str, *, noun: str) -> str:
    count = len([item for item in value.split(",") if item.strip()])
    if count == 0:
        return "Not configured"
    suffix = "" if count == 1 else "s"
    return f"{count} {noun}{suffix} configured"


def _line_count(value: str, *, noun: str) -> str:
    count = len([item for item in value.splitlines() if item.strip()])
    if count == 0:
        return "Not configured"
    suffix = "" if count == 1 else "s"
    return f"{count} {noun}{suffix} configured"


def _bytes(value: int | float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    amount = float(value)
    idx = 0
    while amount >= _BYTES_PER_UNIT and idx < len(units) - 1:
        amount /= _BYTES_PER_UNIT
        idx += 1
    if idx == 0:
        return f"{int(amount)} {units[idx]}"
    return f"{amount:.1f} {units[idx]}"


def _seconds(value: int | float) -> str:
    amount = float(value)
    if amount < _SECONDS_PER_MINUTE:
        return f"{amount:g}s"
    if amount < _SECONDS_PER_HOUR:
        return f"{amount / _SECONDS_PER_MINUTE:g}m"
    return f"{amount / _SECONDS_PER_HOUR:g}h"


def _days(value: int | float) -> str:
    return f"{value:g} days"


def _bool(value: bool) -> str:
    return "Enabled" if value else "Disabled"


def _as_display(value: Any) -> str:
    if isinstance(value, bool):
        return _bool(value)
    if value is None:
        return "Not configured"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) if value else "Not configured"
    if isinstance(value, str):
        return value if value else "Not configured"
    return str(value)


def _value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "list"
    if value is None:
        return "empty"
    return "string"


def _get(config: CaliberConfig, path: str) -> Any:
    current: Any = config
    for part in path.split("."):
        current = getattr(current, part)
    return current


def _source_for(
    *,
    env_var: str,
    value: Any,
    default_value: Any,
) -> str:
    if env_var in os.environ:
        return "environment"
    if value != default_value:
        return "configured"
    return "default"


Formatter = Callable[[Any], str]


def _setting(
    config: CaliberConfig,
    *,
    key: str,
    env_var: str,
    label: str,
    description: str,
    control: str = "environment",
    restart_required: bool = True,
    sensitive: bool = False,
    formatter: Formatter | None = None,
) -> dict[str, Any]:
    value = _get(config, key)
    default_value = _get(_DEFAULT_CONFIG, key)
    display_value = formatter(value) if formatter is not None else _as_display(value)
    return {
        "key": key,
        "env_var": env_var,
        "label": label,
        "description": description,
        "display_value": display_value,
        "value_type": _value_type(value),
        "source": _source_for(env_var=env_var, value=value, default_value=default_value),
        "control": control,
        "restart_required": restart_required,
        "sensitive": sensitive,
    }


def _group(
    *,
    group_id: str,
    title: str,
    description: str,
    settings: list[dict[str, Any]],
) -> dict[str, Any]:
    configured = sum(1 for item in settings if item["source"] != "default")
    live = sum(1 for item in settings if item["control"] == "live")
    return {
        "id": group_id,
        "title": title,
        "description": description,
        "configured_count": configured,
        "live_editable_count": live,
        "settings": settings,
    }


def _advisory_setting(advisory: RuntimeDependencyAdvisory) -> dict[str, Any]:
    advisory_ids = ", ".join(advisory.advisory_ids)
    return {
        "key": f"dependency_advisory.{advisory.package_name}",
        "env_var": f"runtime://dependency-advisories/{advisory.package_name}",
        "label": f"{advisory.package_name} advisory",
        "description": f"{advisory.summary} {advisory.recommended_action}",
        "display_value": f"{advisory.installed_version} ({advisory_ids})",
        "value_type": "string",
        "source": "environment",
        "control": "environment",
        "restart_required": False,
        "sensitive": False,
    }


def _runtime_groups(config: CaliberConfig) -> list[dict[str, Any]]:
    return [
        _group(
            group_id="assistant",
            title="Assistant Runtime",
            description="Assistant availability, model routing, live rollout gates, and per-session limits.",
            settings=[
                _setting(
                    config,
                    key="assistant_enabled",
                    env_var="CALIBER_ASSISTANT_ENABLED",
                    label="Assistant enabled",
                    description="Allows assistant routes to accept requests.",
                    formatter=_bool,
                ),
                _setting(
                    config,
                    key="assistant_skill_runtime_enabled",
                    env_var="CALIBER_ASSISTANT_SKILL_RUNTIME_ENABLED",
                    label="Skill runtime",
                    description="Lets assistant turns resolve and inject active CALIBER skills.",
                    formatter=_bool,
                ),
                _setting(
                    config,
                    key="assistant_engine",
                    env_var="CALIBER_ASSISTANT_ENGINE",
                    label="Assistant engine",
                    description="Provider implementation used at startup: fake, openai, or anthropic.",
                ),
                _setting(
                    config,
                    key="assistant_model",
                    env_var="CALIBER_ASSISTANT_MODEL",
                    label="Assistant model",
                    description="Initial model for provider-backed assistant engines.",
                    control="live",
                    restart_required=False,
                ),
                _setting(
                    config,
                    key="assistant_reasoning",
                    env_var="CALIBER_ASSISTANT_REASONING",
                    label="Reasoning effort",
                    description="Optional reasoning setting for OpenAI assistant engines.",
                    control="live",
                    restart_required=False,
                ),
                _setting(
                    config,
                    key="assistant_disabled_intents",
                    env_var="CALIBER_ASSISTANT_DISABLED_INTENTS",
                    label="Disabled intents",
                    description="Assistant intents disabled for runtime rollout control.",
                    control="live",
                    restart_required=False,
                ),
                _setting(
                    config,
                    key="assistant_disabled_domains",
                    env_var="CALIBER_ASSISTANT_DISABLED_DOMAINS",
                    label="Disabled domains",
                    description="Artifact domains disabled for assistant authoring.",
                    control="live",
                    restart_required=False,
                ),
                _setting(
                    config,
                    key="assistant_max_turns",
                    env_var="CALIBER_ASSISTANT_MAX_TURNS",
                    label="Max turns",
                    description="Maximum turns allowed in an assistant session.",
                ),
                _setting(
                    config,
                    key="assistant_max_questions_per_turn",
                    env_var="CALIBER_ASSISTANT_MAX_QUESTIONS_PER_TURN",
                    label="Questions per turn",
                    description="Maximum clarifying questions the assistant may ask in one turn.",
                ),
                _setting(
                    config,
                    key="assistant_max_drafts_per_session",
                    env_var="CALIBER_ASSISTANT_MAX_DRAFTS_PER_SESSION",
                    label="Drafts per session",
                    description="Maximum drafts retained for one assistant session.",
                ),
                _setting(
                    config,
                    key="assistant_publish_requires_approval",
                    env_var="CALIBER_ASSISTANT_PUBLISH_REQUIRES_APPROVAL",
                    label="Publish requires approval",
                    description="Requires approval before assistant-created drafts can publish.",
                    formatter=_bool,
                ),
                _setting(
                    config,
                    key="assistant_tool_source_max_bytes",
                    env_var="CALIBER_ASSISTANT_TOOL_SOURCE_MAX_BYTES",
                    label="Tool source limit",
                    description="Maximum source bytes loaded while authoring tools.",
                    formatter=_bytes,
                ),
                _setting(
                    config,
                    key="assistant_run_timeout_seconds",
                    env_var="CALIBER_ASSISTANT_RUN_TIMEOUT_SECONDS",
                    label="Run timeout",
                    description="Assistant operation timeout.",
                    formatter=_seconds,
                ),
            ],
        ),
        _group(
            group_id="model-providers",
            title="Models & Providers",
            description="Provider selectors for optimization, evaluation, promotion, and LLM reliability.",
            settings=[
                _setting(
                    config,
                    key="llm_provider",
                    env_var="CALIBER_LLM_PROVIDER",
                    label="LLM provider",
                    description="Default LLM provider for CALIBER refinement stages.",
                ),
                _setting(
                    config,
                    key="openai_workflow_api",
                    env_var="CALIBER_OPENAI_WORKFLOW_API",
                    label="OpenAI workflow API",
                    description=(
                        "Execution surface for OpenAI-backed workflow agent nodes: "
                        "chat completions, responses, or the OpenAI Agents SDK."
                    ),
                ),
                _setting(
                    config,
                    key="openai_workflow_parallel_tool_calls",
                    env_var="CALIBER_OPENAI_WORKFLOW_PARALLEL_TOOL_CALLS",
                    label="Parallel tool calls",
                    description=(
                        "Direct OpenAI traffic can opt into parallel tool calling; "
                        "auto leaves the parameter off for OpenAI-compatible gateways."
                    ),
                ),
                _setting(
                    config,
                    key="openai_prompt_cache_mode",
                    env_var="CALIBER_OPENAI_PROMPT_CACHE_MODE",
                    label="OpenAI prompt-cache hints",
                    description=(
                        "Controls whether CALIBER sends ``prompt_cache_key`` hints to "
                        "OpenAI workflow-agent requests. Auto enables the hint only "
                        "for direct OpenAI traffic."
                    ),
                ),
                _setting(
                    config,
                    key="openai_prompt_cache_retention",
                    env_var="CALIBER_OPENAI_PROMPT_CACHE_RETENTION",
                    label="OpenAI prompt-cache retention",
                    description=(
                        "Optional retention request for OpenAI prompt caching. "
                        "Default defers to the target model and org policy."
                    ),
                ),
                _setting(
                    config,
                    key="llm_diagnosis_model",
                    env_var="CALIBER_LLM_DIAGNOSIS_MODEL",
                    label="Diagnosis model",
                    description="Model used by diagnosis agents when OpenAI-backed LLMs are enabled.",
                ),
                _setting(
                    config,
                    key="gepa_reflection_model",
                    env_var="CALIBER_GEPA_REFLECTION_MODEL",
                    label="GEPA reflection model",
                    description="Model used for GEPA reflection and mutation steps.",
                ),
                _setting(
                    config,
                    key="gepa_max_metric_calls",
                    env_var="CALIBER_GEPA_MAX_METRIC_CALLS",
                    label="GEPA metric budget",
                    description="Maximum evaluation calls during GEPA optimization.",
                ),
                _setting(
                    config,
                    key="allow_flagged_dspy_optimizers",
                    env_var="CALIBER_ALLOW_FLAGGED_DSPY_OPTIMIZERS",
                    label="Allow flagged DSPy stack",
                    description=(
                        "Overrides the runtime-advisory guard and lets DSPy / "
                        "LiteLLM-backed optimizers run even when the optional "
                        "diskcache or LiteLLM stack is flagged."
                    ),
                    formatter=_bool,
                ),
                _setting(
                    config,
                    key="llm_api_key_env",
                    env_var="CALIBER_LLM_API_KEY_ENV",
                    label="LLM API key source",
                    description="Secret source used to resolve provider API credentials.",
                    sensitive=True,
                ),
                _setting(
                    config,
                    key="artifact_store_provider",
                    env_var="CALIBER_ARTIFACT_STORE_PROVIDER",
                    label="Artifact store provider",
                    description="Provider used to read active prompts and artifacts.",
                ),
                _setting(
                    config,
                    key="eval_provider",
                    env_var="CALIBER_EVAL_PROVIDER",
                    label="Eval provider",
                    description="Provider used for evaluation runs.",
                ),
                _setting(
                    config,
                    key="promoter_provider",
                    env_var="CALIBER_PROMOTER_PROVIDER",
                    label="Promoter provider",
                    description="Provider used to promote approved prompt versions.",
                ),
                _setting(
                    config,
                    key="llm_circuit_breaker_enabled",
                    env_var="CALIBER_LLM_CIRCUIT_BREAKER_ENABLED",
                    label="LLM circuit breaker",
                    description="Protects workers from repeatedly calling failing upstream LLMs.",
                    formatter=_bool,
                ),
                _setting(
                    config,
                    key="llm_circuit_failure_threshold",
                    env_var="CALIBER_LLM_CIRCUIT_FAILURE_THRESHOLD",
                    label="Failure threshold",
                    description="Failures within the rolling window that open the LLM circuit.",
                ),
                _setting(
                    config,
                    key="llm_circuit_window_seconds",
                    env_var="CALIBER_LLM_CIRCUIT_WINDOW_SECONDS",
                    label="Failure window",
                    description="Rolling window used for LLM circuit failure counting.",
                    formatter=_seconds,
                ),
                _setting(
                    config,
                    key="llm_circuit_open_duration_seconds",
                    env_var="CALIBER_LLM_CIRCUIT_OPEN_DURATION_SECONDS",
                    label="Open duration",
                    description="Cooldown before the circuit allows a half-open probe.",
                    formatter=_seconds,
                ),
            ],
        ),
        _group(
            group_id="memory",
            title="Agent Memory",
            description=(
                "mem0-backed long-term memory over the platform pgvector + gateway "
                "LLM. Default-off; requires the [memory] extra."
            ),
            settings=[
                _setting(
                    config,
                    key="memory_enabled",
                    env_var="CALIBER_MEMORY_ENABLED",
                    label="Memory enabled",
                    description="Enable mem0-backed agent long-term memory.",
                    formatter=_bool,
                ),
                _setting(
                    config,
                    key="memory_llm_model",
                    env_var="CALIBER_MEMORY_LLM_MODEL",
                    label="Memory LLM model",
                    description="Model mem0 uses to extract/consolidate memories on write (routed via the gateway).",
                ),
                _setting(
                    config,
                    key="memory_embedder_model",
                    env_var="CALIBER_MEMORY_EMBEDDER_MODEL",
                    label="Memory embedder model",
                    description="Embedding model mem0 uses for memory vectors.",
                ),
                _setting(
                    config,
                    key="memory_embedding_dims",
                    env_var="CALIBER_MEMORY_EMBEDDING_DIMS",
                    label="Embedding dimensions",
                    description="Embedding dimensionality — must match the embedder model and pgvector column.",
                ),
                _setting(
                    config,
                    key="memory_collection",
                    env_var="CALIBER_MEMORY_COLLECTION",
                    label="Memory collection",
                    description="pgvector collection/table name memories are stored in.",
                ),
                _setting(
                    config,
                    key="memory_embedder_base_url",
                    env_var="CALIBER_MEMORY_EMBEDDER_BASE_URL",
                    label="Embedder base URL",
                    description="OpenAI-compatible base URL for the embedder; blank routes to OpenAI directly.",
                ),
                _setting(
                    config,
                    key="memory_infer",
                    env_var="CALIBER_MEMORY_INFER",
                    label="Infer on write",
                    description="Run mem0's LLM extraction/dedup pass on write; disable to store text verbatim.",
                    formatter=_bool,
                ),
            ],
        ),
        _group(
            group_id="storage",
            title="Files & Storage",
            description="Local/MinIO storage selection, upload limits, signed URL policy, validation, and retention.",
            settings=[
                _setting(
                    config,
                    key="workflow_storage.backend",
                    env_var="CALIBER_WORKFLOW_STORAGE_BACKEND",
                    label="Default backend",
                    description="Default backend for file directories and workflow run files.",
                ),
                _setting(
                    config,
                    key="workflow_storage.base_uri",
                    env_var="CALIBER_WORKFLOW_STORAGE_BASE_URI",
                    label="Local base URI",
                    description="Base URI for the local filesystem backend.",
                ),
                _setting(
                    config,
                    key="workflow_storage.bucket",
                    env_var="CALIBER_WORKFLOW_STORAGE_BUCKET",
                    label="S3 bucket",
                    description="Bucket used by MinIO/S3-backed file directories.",
                ),
                _setting(
                    config,
                    key="workflow_storage.prefix",
                    env_var="CALIBER_WORKFLOW_STORAGE_PREFIX",
                    label="S3 prefix",
                    description="Object-key prefix for S3-backed storage.",
                ),
                _setting(
                    config,
                    key="workflow_storage.internal_endpoint_url",
                    env_var="CALIBER_WORKFLOW_STORAGE_INTERNAL_ENDPOINT_URL",
                    label="Internal endpoint",
                    description="Server-side MinIO/S3 endpoint URL.",
                ),
                _setting(
                    config,
                    key="workflow_storage.public_endpoint_url",
                    env_var="CALIBER_WORKFLOW_STORAGE_PUBLIC_ENDPOINT_URL",
                    label="Public endpoint",
                    description="Browser-visible MinIO/S3 endpoint URL.",
                ),
                _setting(
                    config,
                    key="workflow_storage.region",
                    env_var="CALIBER_WORKFLOW_STORAGE_REGION",
                    label="Region",
                    description="S3 region.",
                ),
                _setting(
                    config,
                    key="workflow_storage.force_path_style",
                    env_var="CALIBER_WORKFLOW_STORAGE_FORCE_PATH_STYLE",
                    label="Path-style requests",
                    description="Use path-style S3 URLs, commonly needed for MinIO.",
                    formatter=_bool,
                ),
                _setting(
                    config,
                    key="workflow_storage.access_key_source",
                    env_var="CALIBER_WORKFLOW_STORAGE_ACCESS_KEY_SOURCE",
                    label="Access key source",
                    description="Secret source for S3 access key material.",
                    sensitive=True,
                ),
                _setting(
                    config,
                    key="workflow_storage.secret_key_source",
                    env_var="CALIBER_WORKFLOW_STORAGE_SECRET_KEY_SOURCE",
                    label="Secret key source",
                    description="Secret source for S3 secret key material.",
                    sensitive=True,
                ),
                _setting(
                    config,
                    key="workflow_storage.max_upload_bytes",
                    env_var="CALIBER_WORKFLOW_STORAGE_MAX_UPLOAD_BYTES",
                    label="Max upload",
                    description="Maximum size for one uploaded file.",
                    formatter=_bytes,
                ),
                _setting(
                    config,
                    key="workflow_storage.max_run_bytes",
                    env_var="CALIBER_WORKFLOW_STORAGE_MAX_RUN_BYTES",
                    label="Max run storage",
                    description="Maximum storage per workflow run.",
                    formatter=_bytes,
                ),
                _setting(
                    config,
                    key="workflow_storage.max_files_per_run",
                    env_var="CALIBER_WORKFLOW_STORAGE_MAX_FILES_PER_RUN",
                    label="Files per run",
                    description="Maximum files indexed for one workflow run.",
                ),
                _setting(
                    config,
                    key="workflow_storage.signed_url_ttl_seconds",
                    env_var="CALIBER_WORKFLOW_STORAGE_SIGNED_URL_TTL_SECONDS",
                    label="Signed URL TTL",
                    description="Default TTL for generated signed URLs.",
                    formatter=_seconds,
                ),
                _setting(
                    config,
                    key="workflow_storage.signed_url_max_ttl_seconds",
                    env_var="CALIBER_WORKFLOW_STORAGE_SIGNED_URL_MAX_TTL_SECONDS",
                    label="Max signed URL TTL",
                    description="Maximum allowed TTL for generated signed URLs.",
                    formatter=_seconds,
                ),
                _setting(
                    config,
                    key="workflow_storage.allowed_media_types",
                    env_var="CALIBER_WORKFLOW_STORAGE_ALLOWED_MEDIA_TYPES",
                    label="Allowed media types",
                    description="Optional allow-list for uploaded file media types.",
                ),
                _setting(
                    config,
                    key="workflow_storage.denied_extensions",
                    env_var="CALIBER_WORKFLOW_STORAGE_DENIED_EXTENSIONS",
                    label="Denied extensions",
                    description="File extensions rejected during upload validation.",
                ),
                _setting(
                    config,
                    key="workflow_storage.sniff_content_type",
                    env_var="CALIBER_WORKFLOW_STORAGE_SNIFF_CONTENT_TYPE",
                    label="Content sniffing",
                    description="Enables content-type sniffing during uploads.",
                    formatter=_bool,
                ),
                _setting(
                    config,
                    key="workflow_storage.scan_uploads",
                    env_var="CALIBER_WORKFLOW_STORAGE_SCAN_UPLOADS",
                    label="Upload scanning",
                    description="Enables upload scanning hook when configured.",
                    formatter=_bool,
                ),
                _setting(
                    config,
                    key="workflow_storage.retention.default_run_days",
                    env_var="CALIBER_WORKFLOW_STORAGE_RETENTION_DEFAULT_DAYS",
                    label="Default run retention",
                    description="Retention for finalized run files.",
                    formatter=_days,
                ),
                _setting(
                    config,
                    key="workflow_storage.retention.failed_run_days",
                    env_var="CALIBER_WORKFLOW_STORAGE_RETENTION_FAILED_DAYS",
                    label="Failed run retention",
                    description="Retention for failed run files.",
                    formatter=_days,
                ),
                _setting(
                    config,
                    key="workflow_storage.retention.preview_run_days",
                    env_var="CALIBER_WORKFLOW_STORAGE_RETENTION_PREVIEW_DAYS",
                    label="Preview run retention",
                    description="Retention for preview run files.",
                    formatter=_days,
                ),
                _setting(
                    config,
                    key="workflow_storage.retention.eval_run_days",
                    env_var="CALIBER_WORKFLOW_STORAGE_RETENTION_EVAL_DAYS",
                    label="Eval run retention",
                    description="Retention for evaluation run files.",
                    formatter=_days,
                ),
                _setting(
                    config,
                    key="workflow_storage.retention.keep_promoted_artifacts",
                    env_var="CALIBER_WORKFLOW_STORAGE_RETENTION_KEEP_PROMOTED",
                    label="Keep promoted artifacts",
                    description="Keeps promoted artifacts outside normal cleanup.",
                    formatter=_bool,
                ),
            ],
        ),
        _group(
            group_id="knowledge",
            title="Knowledge & GraphRAG",
            description="Knowledge-base workers, graph extraction defaults, and Apache AGE retrieval settings.",
            settings=[
                _setting(
                    config,
                    key="knowledge_build_queue_enabled",
                    env_var="CALIBER_KNOWLEDGE_BUILD_QUEUE_ENABLED",
                    label="Build queue",
                    description="Queues knowledge-base builds for background execution instead of inline processing.",
                    formatter=_bool,
                ),
                _setting(
                    config,
                    key="knowledge_build_worker_enabled",
                    env_var="CALIBER_KNOWLEDGE_BUILD_WORKER_ENABLED",
                    label="Build worker",
                    description="Starts the background worker that claims queued knowledge-base builds.",
                    formatter=_bool,
                ),
                _setting(
                    config,
                    key="knowledge_build_worker_interval_seconds",
                    env_var="CALIBER_KNOWLEDGE_BUILD_WORKER_INTERVAL_SECONDS",
                    label="Worker poll interval",
                    description="Seconds between knowledge-base worker claim ticks.",
                    formatter=_seconds,
                ),
                _setting(
                    config,
                    key="knowledge_build_lease_seconds",
                    env_var="CALIBER_KNOWLEDGE_BUILD_LEASE_SECONDS",
                    label="Build lease",
                    description="Lease duration before queued knowledge-base builds are recoverable.",
                    formatter=_seconds,
                ),
                _setting(
                    config,
                    key="knowledge_graph_extractor_backend",
                    env_var="CALIBER_KNOWLEDGE_GRAPH_EXTRACTOR_BACKEND",
                    label="Graph extractor",
                    description="Default extractor backend for entity and relationship enrichment.",
                ),
                _setting(
                    config,
                    key="knowledge_graph_spacy_model",
                    env_var="CALIBER_KNOWLEDGE_GRAPH_SPACY_MODEL",
                    label="spaCy model",
                    description="Default spaCy model used when graph extraction runs in spaCy mode.",
                ),
                _setting(
                    config,
                    key="knowledge_age_enabled",
                    env_var="CALIBER_KNOWLEDGE_AGE_ENABLED",
                    label="Apache AGE retrieval",
                    description="Enables Apache AGE sync and graph-native retrieval surfaces for knowledge bases.",
                    formatter=_bool,
                ),
                _setting(
                    config,
                    key="knowledge_age_graph_name",
                    env_var="CALIBER_KNOWLEDGE_AGE_GRAPH_NAME",
                    label="Apache AGE graph name",
                    description="Shared AGE graph CALIBER syncs knowledge-base versions into and queries back from.",
                ),
                _setting(
                    config,
                    key="knowledge_age_viewer_url",
                    env_var="CALIBER_KNOWLEDGE_AGE_VIEWER_URL",
                    label="Apache AGE viewer URL",
                    description="Optional graph-console URL the Knowledge Base UI links to for direct AGE inspection.",
                ),
                _setting(
                    config,
                    key="allow_flagged_local_embeddings",
                    env_var="CALIBER_ALLOW_FLAGGED_LOCAL_EMBEDDINGS",
                    label="Allow flagged local embeddings",
                    description=(
                        "Overrides the runtime-advisory guard and lets local Hugging "
                        "Face embedding builds run even when the optional torch "
                        "stack is flagged."
                    ),
                    formatter=_bool,
                ),
                _setting(
                    config,
                    key="knowledge_pgvector_enabled",
                    env_var="CALIBER_KNOWLEDGE_PGVECTOR_ENABLED",
                    label="pgvector ANN retrieval",
                    description=(
                        "On Postgres with pgvector, generate the dense candidate pool "
                        "with a SQL-side nearest-neighbour top-k instead of a Python "
                        "scan over every chunk — the path that scales to millions of "
                        "chunks. No-op on SQLite."
                    ),
                    formatter=_bool,
                ),
                _setting(
                    config,
                    key="knowledge_ann_candidate_pool_size",
                    env_var="CALIBER_KNOWLEDGE_ANN_CANDIDATE_POOL_SIZE",
                    label="Retrieval candidate-pool size",
                    description=(
                        "How many first-stage candidates retrieval gathers before the "
                        "optional cross-encoder rerank narrows them to top_k."
                    ),
                ),
                _setting(
                    config,
                    key="knowledge_embedding_dimension",
                    env_var="CALIBER_KNOWLEDGE_EMBEDDING_DIMENSION",
                    label="Embedding dimension (ANN/HNSW)",
                    description=(
                        "Dimension the pgvector ANN column + HNSW index are built for "
                        "(max 2000). Match your embedding model (1024 for BGE-M3/E5/Qwen, "
                        "384 for MiniLM); off-dimension KBs fall back to the in-memory scan."
                    ),
                ),
                _setting(
                    config,
                    key="knowledge_rerank_enabled",
                    env_var="CALIBER_KNOWLEDGE_RERANK_ENABLED",
                    label="Cross-encoder rerank",
                    description=(
                        "Re-score the candidate pool with a local cross-encoder and "
                        "reorder it before returning top_k — better ranking quality at "
                        "the cost of a local model run."
                    ),
                    formatter=_bool,
                ),
                _setting(
                    config,
                    key="knowledge_rerank_model",
                    env_var="CALIBER_KNOWLEDGE_RERANK_MODEL",
                    label="Reranker model",
                    description="Hugging Face cross-encoder model id used for reranking.",
                ),
            ],
        ),
        _group(
            group_id="security",
            title="Access & Security",
            description="RBAC, local dev identity, CSRF, rate limits, and PII redaction controls.",
            settings=[
                _setting(
                    config,
                    key="dev_user",
                    env_var="CALIBER_DEV_USER",
                    label="Dev fallback user",
                    description="Development identity used when no upstream user header is present.",
                ),
                _setting(
                    config,
                    key="admin_users",
                    env_var="CALIBER_ADMIN_USERS",
                    label="Admin users",
                    description="Comma-separated identities granted admin scope.",
                    formatter=lambda value: _csv_count(str(value), noun="admin"),
                ),
                _setting(
                    config,
                    key="approver_users",
                    env_var="CALIBER_APPROVER_USERS",
                    label="Approver users",
                    description="Comma-separated identities granted approval scope.",
                    formatter=lambda value: _csv_count(str(value), noun="approver"),
                ),
                _setting(
                    config,
                    key="operator_users",
                    env_var="CALIBER_OPERATOR_USERS",
                    label="Operator users",
                    description="Comma-separated identities granted operator scope.",
                    formatter=lambda value: _csv_count(str(value), noun="operator"),
                ),
                _setting(
                    config,
                    key="csrf_enabled",
                    env_var="CALIBER_CSRF_ENABLED",
                    label="CSRF protection",
                    description=(
                        "Requires state-changing requests to include X-CALIBER-CSRF. "
                        "The server refuses to start if this is on without a resolvable "
                        "signing secret, so this row reflects enforcement, not intent."
                    ),
                    formatter=_bool,
                ),
                _setting(
                    config,
                    key="csrf_signing_secret_env",
                    env_var="CALIBER_CSRF_SIGNING_SECRET_ENV",
                    label="CSRF secret source",
                    description="Secret source used to sign CSRF tokens.",
                    sensitive=True,
                ),
                _setting(
                    config,
                    key="csrf_token_ttl_seconds",
                    env_var="CALIBER_CSRF_TOKEN_TTL_SECONDS",
                    label="CSRF token TTL",
                    description="How long issued CSRF tokens remain valid.",
                    formatter=_seconds,
                ),
                _setting(
                    config,
                    key="rate_limit_enabled",
                    env_var="CALIBER_RATE_LIMIT_ENABLED",
                    label="Rate limiting",
                    description="Enables per-user token-bucket request limiting.",
                    formatter=_bool,
                ),
                _setting(
                    config,
                    key="service_invoke_max_body_bytes",
                    env_var="CALIBER_SERVICE_INVOKE_MAX_BODY_BYTES",
                    label="Published-service invoke body limit",
                    description=(
                        "Maximum raw JSON-envelope bytes consumed by a published "
                        "workflow-service invocation."
                    ),
                    formatter=_bytes,
                ),
                _setting(
                    config,
                    key="rate_limit_requests_per_minute",
                    env_var="CALIBER_RATE_LIMIT_REQUESTS_PER_MINUTE",
                    label="Request rate",
                    description="Sustained per-user request rate.",
                    formatter=lambda value: f"{value:g}/min",
                ),
                _setting(
                    config,
                    key="rate_limit_burst",
                    env_var="CALIBER_RATE_LIMIT_BURST",
                    label="Burst size",
                    description="Maximum per-user burst before 429 responses.",
                ),
                _setting(
                    config,
                    key="rate_limit_max_buckets",
                    env_var="CALIBER_RATE_LIMIT_MAX_BUCKETS",
                    label="Bucket cap",
                    description="Maximum number of in-memory user buckets.",
                ),
                _setting(
                    config,
                    key="pii_redaction_enabled",
                    env_var="CALIBER_PII_REDACTION_ENABLED",
                    label="PII redaction",
                    description="Redacts common PII patterns before audit details are stored.",
                    formatter=_bool,
                ),
                _setting(
                    config,
                    key="pii_redaction_replacement",
                    env_var="CALIBER_PII_REDACTION_REPLACEMENT",
                    label="Redaction replacement",
                    description="Replacement text for PII matches.",
                ),
                _setting(
                    config,
                    key="pii_redaction_extra_patterns",
                    env_var="CALIBER_PII_REDACTION_EXTRA_PATTERNS",
                    label="Extra redaction patterns",
                    description="Deployment-specific newline-separated regex patterns.",
                    formatter=lambda value: _line_count(str(value), noun="pattern"),
                    sensitive=True,
                ),
            ],
        ),
        _group(
            group_id="runtime-advisories",
            title="Runtime Dependency Advisories",
            description=(
                "Known-risk dependency versions detected in the running environment. "
                "These findings come from CALIBER's supported-matrix audit and "
                "affect optional feature posture."
            ),
            settings=[
                _advisory_setting(advisory) for advisory in get_runtime_dependency_advisories()
            ],
        ),
        _group(
            group_id="operations",
            title="Operations & Automation",
            description="Background workers, janitor cadence, shutdown behavior, webhooks, and serving basics.",
            settings=[
                _setting(
                    config,
                    key="background_tasks_enabled",
                    env_var="CALIBER_BACKGROUND_TASKS_ENABLED",
                    label="Background tasks",
                    description="Starts poller, worker, janitor, and webhook dispatcher loops.",
                    formatter=_bool,
                ),
                _setting(
                    config,
                    key="janitor_interval_seconds",
                    env_var="CALIBER_JANITOR_INTERVAL_SECONDS",
                    label="Janitor interval",
                    description="Seconds between stale-heartbeat janitor sweeps.",
                    formatter=_seconds,
                ),
                _setting(
                    config,
                    key="janitor_stale_threshold_seconds",
                    env_var="CALIBER_JANITOR_STALE_THRESHOLD_SECONDS",
                    label="Stale threshold",
                    description="Running-job heartbeat age before janitor reaping.",
                    formatter=_seconds,
                ),
                _setting(
                    config,
                    key="workflow_run_retention_days",
                    env_var="CALIBER_WORKFLOW_RUN_RETENTION_DAYS",
                    label="Workflow run retention",
                    description="Retention window for indexed workflow run rows; zero disables pruning.",
                    formatter=_days,
                ),
                _setting(
                    config,
                    key="workflow_run_queue_enabled",
                    env_var="CALIBER_WORKFLOW_RUN_QUEUE_ENABLED",
                    label="Workflow run queue",
                    description="Enables async workflow-run submission via POST /workflow-runs.",
                    formatter=_bool,
                ),
                _setting(
                    config,
                    key="workflow_run_runtime_approvals_enabled",
                    env_var="CALIBER_WORKFLOW_RUN_RUNTIME_APPROVALS_ENABLED",
                    label="Runtime approvals",
                    description="Enables runtime approval wait/decision capabilities for workflow runs.",
                    formatter=_bool,
                ),
                _setting(
                    config,
                    key="workflow_run_checkpointing_enabled",
                    env_var="CALIBER_WORKFLOW_RUN_CHECKPOINTING_ENABLED",
                    label="Run checkpointing",
                    description="Enables persisted workflow-run checkpoints and resume capabilities.",
                    formatter=_bool,
                ),
                _setting(
                    config,
                    key="workflow_run_event_backend",
                    env_var="CALIBER_WORKFLOW_RUN_EVENT_BACKEND",
                    label="Run event backend",
                    description="Live event fanout backend for workflow-run lifecycle updates.",
                ),
                _setting(
                    config,
                    key="nats_url",
                    env_var="CALIBER_NATS_URL",
                    label="NATS URL",
                    description="NATS server URL used when the run event backend is nats.",
                ),
                _setting(
                    config,
                    key="nats_subject",
                    env_var="CALIBER_NATS_SUBJECT",
                    label="NATS subject",
                    description="NATS subject used for CALIBER live event fanout.",
                ),
                _setting(
                    config,
                    key="redis_url",
                    env_var="CALIBER_REDIS_URL",
                    label="Redis URL",
                    description="Redis URL used when the run event backend is redis.",
                ),
                _setting(
                    config,
                    key="redis_channel",
                    env_var="CALIBER_REDIS_CHANNEL",
                    label="Redis channel",
                    description="Redis pub/sub channel used for CALIBER live event fanout.",
                ),
                _setting(
                    config,
                    key="workflow_run_worker_enabled",
                    env_var="CALIBER_WORKFLOW_RUN_WORKER_ENABLED",
                    label="Run worker enabled",
                    description="Starts the queue worker that claims and executes workflow runs.",
                    formatter=_bool,
                ),
                _setting(
                    config,
                    key="workflow_run_worker_interval_seconds",
                    env_var="CALIBER_WORKFLOW_RUN_WORKER_INTERVAL_SECONDS",
                    label="Run worker interval",
                    description="Seconds between workflow-run claim ticks.",
                    formatter=_seconds,
                ),
                _setting(
                    config,
                    key="workflow_run_lease_seconds",
                    env_var="CALIBER_WORKFLOW_RUN_LEASE_SECONDS",
                    label="Run lease duration",
                    description="Claim lease duration for active workflow runs before recovery.",
                    formatter=_seconds,
                ),
                _setting(
                    config,
                    key="workflow_run_max_attempts",
                    env_var="CALIBER_WORKFLOW_RUN_MAX_ATTEMPTS",
                    label="Run max attempts",
                    description="Maximum attempt count allowed for retry lineage.",
                ),
                _setting(
                    config,
                    key="shutdown_grace_seconds",
                    env_var="CALIBER_SHUTDOWN_GRACE_SECONDS",
                    label="Shutdown grace",
                    description="Time background loops get to drain on shutdown.",
                    formatter=_seconds,
                ),
                _setting(
                    config,
                    key="webhook_urls",
                    env_var="CALIBER_WEBHOOK_URLS",
                    label="Webhook endpoints",
                    description="Comma-separated HTTPS endpoints receiving signed notifications.",
                    formatter=lambda value: _csv_count(str(value), noun="endpoint"),
                    sensitive=True,
                ),
                _setting(
                    config,
                    key="webhook_signing_secret_env",
                    env_var="CALIBER_WEBHOOK_SIGNING_SECRET_ENV",
                    label="Webhook secret source",
                    description="Secret source used to sign outbound webhook payloads.",
                    sensitive=True,
                ),
                _setting(
                    config,
                    key="webhook_event_filter",
                    env_var="CALIBER_WEBHOOK_EVENT_FILTER",
                    label="Webhook event filter",
                    description="Event types the dispatcher subscribes to; '*' subscribes to all.",
                ),
                _setting(
                    config,
                    key="log_level",
                    env_var="CALIBER_LOG_LEVEL",
                    label="Log level",
                    description="Standard logging level for the CALIBER service.",
                ),
                _setting(
                    config,
                    key="log_sink",
                    env_var="CALIBER_LOG_SINK",
                    label="Log sink",
                    description="Use stderr only, or mirror JSONL service logs to Object Store / S3.",
                ),
                _setting(
                    config,
                    key="log_bucket",
                    env_var="CALIBER_LOG_BUCKET",
                    label="Log bucket",
                    description="Bucket receiving JSONL service logs when CALIBER_LOG_SINK=s3.",
                ),
                _setting(
                    config,
                    key="log_prefix",
                    env_var="CALIBER_LOG_PREFIX",
                    label="Log prefix",
                    description="Object-key prefix used inside the configured log bucket.",
                ),
                _setting(
                    config,
                    key="log_s3_auto_create_bucket",
                    env_var="CALIBER_LOG_S3_AUTO_CREATE_BUCKET",
                    label="Create log bucket",
                    description="Creates the log bucket automatically after a missing-bucket response.",
                    formatter=_bool,
                ),
                _setting(
                    config,
                    key="log_s3_flush_lines",
                    env_var="CALIBER_LOG_S3_FLUSH_LINES",
                    label="Log flush lines",
                    description="Number of JSON log lines written per S3 object.",
                ),
                _setting(
                    config,
                    key="static_prefix",
                    env_var="CALIBER_STATIC_PREFIX",
                    label="Static prefix",
                    description="Reverse-proxy URL prefix used for API and SPA routes.",
                ),
                _setting(
                    config,
                    key="database_url",
                    env_var="CALIBER_DATABASE_URL",
                    label="Database URL",
                    description="SQLAlchemy URL for the CALIBER metadata store.",
                    sensitive=True,
                    formatter=lambda value: _mask_dsn(str(value)),
                ),
            ],
        ),
        _group(
            group_id="tool-sandbox",
            title="Tool Sandbox",
            description="Limits for execution output and runtime when CALIBER tests generated tools.",
            settings=[
                _setting(
                    config,
                    key="tool_sandbox_timeout_seconds",
                    env_var="CALIBER_TOOL_SANDBOX_TIMEOUT_SECONDS",
                    label="Execution timeout",
                    description="Maximum runtime for sandboxed tool executions.",
                    formatter=_seconds,
                ),
                _setting(
                    config,
                    key="tool_sandbox_max_output_bytes",
                    env_var="CALIBER_TOOL_SANDBOX_MAX_OUTPUT_BYTES",
                    label="Output limit",
                    description="Maximum captured output bytes from sandboxed tool executions.",
                    formatter=_bytes,
                ),
            ],
        ),
        _group(
            group_id="versioning",
            title="Versioning & Releases",
            description=(
                "The advisory eval-gate defaults that govern promotion. The Releases page "
                "shows what's live and the promotion/rollback timeline; the gate is advisory "
                "and never blocks an alias rotation. Workflow-run retention is configured "
                "under Operations (CALIBER_WORKFLOW_RUN_RETENTION_DAYS)."
            ),
            settings=[
                _versioning_default_item(
                    key="gate_min_aggregate_score_default",
                    label="Eval gate: min aggregate score (default)",
                    description=("Advisory promotion-gate floor on a candidate's overall score."),
                    value=DEFAULT_MIN_AGGREGATE_SCORE,
                ),
                _versioning_default_item(
                    key="gate_max_regression_delta_default",
                    label="Eval gate: max regression delta (default)",
                    description=(
                        "Advisory promotion-gate ceiling on per-dimension regression "
                        "versus the baseline."
                    ),
                    value=DEFAULT_MAX_REGRESSION_DELTA,
                ),
            ],
        ),
    ]


def _versioning_default_item(
    *, key: str, label: str, description: str, value: float
) -> dict[str, Any]:
    """A read-only informational setting for a code-level default (not a config var)."""
    return {
        "key": key,
        "env_var": f"runtime://versioning/{key}",
        "label": label,
        "description": description,
        "display_value": str(value),
        "value_type": "number",
        "source": "default",
        "control": "environment",
        "restart_required": False,
        "sensitive": False,
    }


async def get_runtime_settings(request: Request) -> JSONResponse:
    """Return a grouped, safe inventory of runtime configuration knobs."""
    require_scopes(request, [SCOPE_ADMIN])
    config = getattr(request.app.state, "config", None)
    if config is None:
        config = CaliberConfig.load()
    groups = _runtime_groups(config)
    flat = [setting for group in groups for setting in group["settings"]]
    summary = {
        "total": len(flat),
        "live_editable": sum(1 for item in flat if item["control"] == "live"),
        "environment_managed": sum(1 for item in flat if item["control"] == "environment"),
        "configured": sum(1 for item in flat if item["source"] != "default"),
        "defaults": sum(1 for item in flat if item["source"] == "default"),
        "secret_sources": sum(1 for item in flat if item["sensitive"]),
    }
    return envelope_response_dict({"summary": summary, "groups": groups})


# ---------------------------------------------------------------------------
# LLM credentials & gateway — the one live-editable settings surface.
#
# CaliberConfig is frozen and credentials live in the deployment environment, so
# this applies a *runtime override*: keys are written to the process env and the
# MLflow gateway URL replaces ``app.state.config`` (read fresh per request). It
# takes effect on the running server immediately; the durable source of truth
# stays the deployment env / .env. Secrets are never read back — only presence.
# ---------------------------------------------------------------------------


def _openai_key_env(config: CaliberConfig) -> str:
    source = str(getattr(config, "llm_api_key_env", "") or "").strip()
    if source and "://" not in source:
        return source
    return _OPENAI_KEY_ENV_FALLBACK


def _key_fingerprint(key: str) -> str:
    """Return a non-reversible display hint for a resolved credential.

    Enough for an operator to confirm *which* key is live ("…the one ending
    4f2a") without the response ever carrying the secret. Short keys collapse
    to a bare mask so a 6-character value can't be reconstructed from its tail.
    """
    key = key.strip()
    if not key:
        return ""
    if len(key) <= _FINGERPRINT_MIN_LENGTH:
        return "••••"
    return f"••••{key[-4:]}"


def _llm_setup_status(config: CaliberConfig) -> dict[str, Any]:
    openai_source = str(getattr(config, "llm_api_key_env", "") or _OPENAI_KEY_ENV_FALLBACK)
    openai_key = resolve_secret(openai_source) or ""
    anthropic_key = os.environ.get(_ANTHROPIC_KEY_ENV, "") or ""
    return {
        "llm_provider": str(getattr(config, "llm_provider", "") or ""),
        "gateway_url": str(getattr(config, "llm_base_url", "") or ""),
        "openai_key_env": _openai_key_env(config),
        "openai_key_present": bool(openai_key),
        "anthropic_key_present": bool(anthropic_key),
        "assistant_engine": str(getattr(config, "assistant_engine", "") or ""),
        # Presence + a masked tail only. This route used to return the fully
        # resolved provider keys so the Settings UI could prefill its password
        # fields — which put live credentials into every operator's browser,
        # React Query cache, and any response log, contradicting the route's
        # own "never returns secret values" contract. Writes stay supported via
        # PATCH; reads are now write-only-safe.
        "openai_key_fingerprint": _key_fingerprint(openai_key),
        "anthropic_key_fingerprint": _key_fingerprint(anthropic_key),
    }


def _config_from(request: Request) -> CaliberConfig:
    config = getattr(request.app.state, "config", None)
    return config if isinstance(config, CaliberConfig) else CaliberConfig.load()


async def get_llm_setup(request: Request) -> JSONResponse:
    """Report the LLM provider, gateway URL, and which credentials are present.

    Never returns secret values — only whether each key resolves, plus a masked
    ``••••<last 4>`` fingerprint so an operator can tell *which* key is live.
    """
    require_scopes(request, [SCOPE_OPERATOR])
    return envelope_response_dict(_llm_setup_status(_config_from(request)))


async def update_llm_setup(request: Request) -> JSONResponse:
    """Apply GPT/Claude credentials + the MLflow gateway URL at runtime (admin)."""
    require_scopes(request, [SCOPE_ADMIN])
    data = await parse_json_object(request)
    config = _config_from(request)

    openai_key = data.get("openai_api_key")
    anthropic_key = data.get("anthropic_api_key")
    gateway_url = data.get("gateway_url")

    if openai_key is not None:
        if not isinstance(openai_key, str):
            raise HTTPException(status_code=400, detail="'openai_api_key' must be a string")
        if openai_key.strip():
            os.environ[_openai_key_env(config)] = openai_key.strip()

    if anthropic_key is not None:
        if not isinstance(anthropic_key, str):
            raise HTTPException(status_code=400, detail="'anthropic_api_key' must be a string")
        if anthropic_key.strip():
            os.environ[_ANTHROPIC_KEY_ENV] = anthropic_key.strip()

    if gateway_url is not None:
        if not isinstance(gateway_url, str):
            raise HTTPException(status_code=400, detail="'gateway_url' must be a string")
        # Empty string is a valid clear (route LLM traffic directly to OpenAI).
        request.app.state.config = config.model_copy(update={"llm_base_url": gateway_url.strip()})
        config = request.app.state.config

    return envelope_response_dict(_llm_setup_status(config))


def register(app: Starlette) -> None:
    """Add settings routes to the Starlette application."""
    app.routes.append(Route(RUNTIME_PATH, get_runtime_settings, methods=["GET"]))
    app.routes.append(Route(LLM_PATH, get_llm_setup, methods=["GET"]))
    app.routes.append(Route(LLM_PATH, update_llm_setup, methods=["PATCH"]))
