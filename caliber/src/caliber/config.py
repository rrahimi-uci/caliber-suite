"""CALIBER runtime configuration.

Phase 1: the configuration surface is intentionally small. We expose what the
plugin shell needs to load and serve a health check, with hooks for later phases
to grow into. Every field is environment-variable-driven and validated at
construction time so a misconfigured deployment fails fast instead of failing
strangely at request time.

Later phases add fields under this same surface (LLM provider URIs, optimizer
defaults, approval-policy defaults, observability sinks) so the import path
``caliber.config.CaliberConfig`` stays stable.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from caliber.db_url import normalize_database_url

#: Shipped ceiling for one LLM provider HTTP request. Referenced by both the
#: ``CaliberConfig`` field and :func:`provider_request_timeout` so the default
#: has exactly one definition.
_PROVIDER_REQUEST_TIMEOUT_DEFAULT: Final[float] = 120.0

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LogSink = Literal["stderr", "s3"]
WorkflowRunEventBackend = Literal["in_process", "nats", "database", "redis"]
KnowledgeGraphExtractorBackend = Literal["heuristic", "spacy"]
OpenAIWorkflowAPI = Literal["chat_completions", "responses", "agents_sdk"]
OpenAIParallelToolCallsMode = Literal["auto", "enabled", "disabled"]
OpenAIPromptCacheMode = Literal["auto", "enabled", "disabled"]
OpenAIPromptCacheRetention = Literal["default", "in_memory", "24h"]


class RetentionConfig(BaseModel):
    """Run-namespace retention policy (storage doc §2.6).

    A janitor reads these to decide when a finalized run's files may be cleaned.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    default_run_days: int = Field(default=30, ge=1)
    failed_run_days: int = Field(default=14, ge=1)
    preview_run_days: int = Field(default=7, ge=1)
    eval_run_days: int = Field(default=90, ge=1)
    keep_promoted_artifacts: bool = True


class WorkflowStorageConfig(BaseModel):
    """File/workspace storage settings (storage doc §3.5).

    A plain frozen ``BaseModel`` — not ``BaseSettings``. Populated by
    :meth:`CaliberConfig.load` from ``CALIBER_WORKFLOW_STORAGE_*`` env vars via a
    dedicated table (see ``_WORKFLOW_STORAGE_ENV_TABLE``). Credentials are
    *sources* resolved through :func:`caliber.secrets.resolve_secret`, never
    literal keys.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["local", "s3"] = "local"

    # Local backend
    base_uri: str = "file://./caliber-workspaces"  # local-only; ignored for s3

    # S3 / MinIO backend (required when backend == "s3")
    bucket: str | None = None
    prefix: str = ""
    internal_endpoint_url: str | None = None
    public_endpoint_url: str | None = None
    region: str | None = None
    force_path_style: bool = False
    access_key_source: str | None = None
    secret_key_source: str | None = None

    # Limits / quotas
    max_upload_bytes: int = Field(default=250 * 1024 * 1024, ge=1)
    max_run_bytes: int = Field(default=2 * 1024 * 1024 * 1024, ge=1)
    max_files_per_run: int = Field(default=500, ge=1)
    signed_url_ttl_seconds: int = Field(default=900, ge=1)
    signed_url_max_ttl_seconds: int = Field(default=3600, ge=1)

    # Validation
    allowed_media_types: list[str] = Field(default_factory=list)
    denied_extensions: list[str] = Field(default_factory=lambda: [".exe", ".dll", ".bat", ".sh"])
    sniff_content_type: bool = True
    scan_uploads: bool = False

    retention: RetentionConfig = Field(default_factory=RetentionConfig)


class CaliberConfig(BaseModel):
    """Runtime configuration loaded from the ``CALIBER_*`` environment.

    Use :meth:`load` rather than instantiating directly so environment loading
    is centralized and exception messages are uniform.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    log_level: LogLevel = Field(
        default="INFO",
        description="Standard library logging level for the CALIBER plugin.",
    )
    log_sink: LogSink = Field(
        default="s3",
        description=(
            "Primary deployment log destination. ``s3`` keeps stderr enabled and mirrors "
            "JSONL batches to the configured Object Store / S3 bucket when available. "
            "``stderr`` disables bucket mirroring."
        ),
    )
    log_bucket: str = Field(
        default="caliber-log",
        description="Bucket used when ``CALIBER_LOG_SINK=s3``.",
    )
    log_prefix: str = Field(
        default="service",
        description="Object-key prefix for S3-backed JSONL service logs.",
    )
    log_s3_auto_create_bucket: bool = Field(
        default=True,
        description=(
            "When true, the S3 log sink creates ``log_bucket`` if it receives a "
            "missing-bucket response. Disable in production environments where buckets "
            "are provisioned separately."
        ),
    )
    log_s3_flush_lines: int = Field(
        default=1,
        ge=1,
        description=(
            "Number of JSON log lines buffered before each S3 object write. The default "
            "prioritizes visibility; increase to batch writes in higher-volume deployments."
        ),
    )

    static_prefix: str = Field(
        default="",
        description=(
            "URL prefix MLflow is served behind (e.g. ``/mlflow`` when reverse-proxied). "
            "CALIBER's API and SPA routes are nested under this prefix."
        ),
    )

    database_url: str = Field(
        default="sqlite:///./caliber.db",
        description=(
            "SQLAlchemy URL for the CALIBER metadata store. In production this is the "
            "same database MLflow uses for its backend store. The default is a local "
            "SQLite file for development; production deployments override via "
            "``CALIBER_DATABASE_URL``."
        ),
    )

    # ------------------------------------------------------------------
    # LLM provider settings (consumed by caliber.llm.*).
    # ------------------------------------------------------------------

    llm_provider: str = Field(
        default="fake",
        description=(
            "Which LLM provider implementation to construct by default. "
            "``openai`` uses OpenAI-backed providers (requires the ``[llm]`` extra). "
            "``fake`` returns a deterministic stub — safe default so unit tests "
            "and demos work with no API key."
        ),
    )
    llm_diagnosis_model: str = Field(
        default="gpt-4o-mini",
        description="Model passed to the diagnosis agent when ``llm_provider=='openai'``.",
    )
    gepa_reflection_model: str = Field(
        default="gpt-4o",
        description=(
            "Model used by the GEPA optimizer's reflection and mutation steps. "
            "Format: ``<provider>:/<model>`` for MLflow's native GEPA integration "
            "(e.g. ``openai:/gpt-4o``). Only used when the optimizer selector "
            "picks GEPA."
        ),
    )
    gepa_max_metric_calls: int = Field(
        default=100,
        ge=10,
        description=(
            "Maximum number of evaluation calls during GEPA optimization. "
            "Higher values may lead to better results but increase cost."
        ),
    )
    dspy_max_bootstrapped_demos: int = Field(
        default=4,
        ge=0,
        description=(
            "Maximum number of self-generated few-shot demonstrations DSPy's "
            "BootstrapFewShot teleprompter may add to a prompt. Each demo is "
            "produced by running the current prompt over a trainset example and "
            "keeping it only if the metric passes — so higher values cost more "
            "LLM calls during candidate generation. Only used when the optimizer "
            "selector picks DSPyBootstrapFewShot. Requires the ``[dspy]`` extra."
        ),
    )
    dspy_max_labeled_demos: int = Field(
        default=4,
        ge=0,
        description=(
            "Maximum number of raw labeled trainset examples DSPy's "
            "BootstrapFewShot teleprompter may add directly as few-shot demos "
            "(no teacher pass). Also caps MIPROv2's labeled demos. Only used "
            "when the optimizer selector picks a DSPy teleprompter. Requires the "
            "``[dspy]`` extra."
        ),
    )
    dspy_mipro_auto: str = Field(
        default="light",
        description=(
            "MIPROv2 budget preset: ``light`` / ``medium`` / ``heavy``. Controls "
            "how many candidate instructions + demo sets MIPROv2 proposes and "
            "evaluates — higher presets cost proportionally more LLM calls. Only "
            "used when the optimizer selector picks DSPyMIPRO. Requires the "
            "``[dspy]`` extra."
        ),
    )
    allow_flagged_dspy_optimizers: bool = Field(
        default=False,
        description=(
            "Override the runtime-advisory guard and allow DSPy / LiteLLM-backed "
            "optimizers to run even when the optional diskcache or LiteLLM stack "
            "is flagged upstream. Intended only for accepted-risk, isolated deployments."
        ),
    )

    # ------------------------------------------------------------------
    # Agent long-term memory (mem0) — consumed by caliber.memory.*.
    # Vector store reuses ``database_url`` (the platform pgvector); the
    # extraction LLM routes through ``llm_base_url`` (the gateway). Default-off;
    # requires the ``[memory]`` extra.
    # ------------------------------------------------------------------
    memory_enabled: bool = Field(
        default=False,
        description=(
            "Enable mem0-backed agent long-term memory. When false, the memory "
            "service refuses to construct. Requires the ``[memory]`` extra."
        ),
    )
    memory_llm_model: str = Field(
        default="gpt-4o-mini",
        description=(
            "Model mem0 uses to extract/consolidate memories on write. Routed "
            "through ``llm_base_url`` (the gateway) when that is set."
        ),
    )
    memory_embedder_model: str = Field(
        default="text-embedding-3-small",
        description="Embedding model mem0 uses for memory vectors.",
    )
    memory_embedding_dims: int = Field(
        default=1536,
        ge=1,
        description=(
            "Embedding dimensionality — must match ``memory_embedder_model`` and "
            "the pgvector column. 1536 for text-embedding-3-small."
        ),
    )
    memory_collection: str = Field(
        default="caliber_memories",
        description="pgvector collection/table name mem0 stores memories in.",
    )
    memory_embedder_base_url: str = Field(
        default="",
        description=(
            "OpenAI-compatible base URL for the memory *embedder*. Blank → "
            "OpenAI direct (not every gateway proxies ``/embeddings``); set to "
            "the gateway only if it exposes an embeddings route."
        ),
    )
    memory_infer: bool = Field(
        default=True,
        description=(
            "When true, mem0 runs an LLM extraction pass on write (smart "
            "memories + dedup). When false, text is stored verbatim — cheaper, "
            "no per-write LLM call."
        ),
    )

    llm_api_key_env: str = Field(
        default="OPENAI_API_KEY",
        description=(
            "Source of the LLM API key. Accepts either an env var name "
            "(bare string; backwards-compatible) or a "
            ":mod:`caliber.secrets` URI like "
            "``file:///run/secrets/openai`` for file-based secret "
            "mounts. The resolved value never lands on the config "
            "object so it can't leak into logs."
        ),
    )

    llm_base_url: str = Field(
        default="",
        description=(
            "Optional OpenAI-compatible base URL for workflow agent LLM calls. "
            "Empty (default) = call api.openai.com directly. Set to an "
            "OpenAI-compatible gateway such as the MLflow AI Gateway "
            "(e.g. ``http://127.0.0.1:5000/gateway/mlflow/v1``) to route every "
            "workflow agent call through it; the agent node's ``model`` must then "
            "name a configured gateway endpoint."
        ),
    )
    gateway_uri: str = Field(
        default="",
        description=(
            "Base URI of the MLflow AI Gateway (LLM gateway) for the Gateway page "
            "to discover. Read-only discovery: CALIBER lists the gateway's "
            "endpoints + health from ``<uri>/api/2.0/endpoints/`` and reports "
            "whether ``llm_base_url`` routes through it. Empty (default) = no "
            "gateway configured; the page shows a not-configured hint. This does "
            "NOT itself route LLM traffic — set ``llm_base_url`` to opt into that."
        ),
    )
    openai_workflow_api: OpenAIWorkflowAPI = Field(
        default="chat_completions",
        description=(
            "Which OpenAI API surface workflow agent nodes use when "
            "``llm_provider=='openai'``. ``chat_completions`` preserves "
            "compatibility with older OpenAI-compatible gateways; ``responses`` "
            "enables the newer Responses API executor; ``agents_sdk`` runs "
            "agent nodes through the OpenAI Agents SDK."
        ),
    )
    openai_workflow_parallel_tool_calls: OpenAIParallelToolCallsMode = Field(
        default="auto",
        description=(
            "Whether OpenAI workflow executors request parallel tool calling. "
            "``auto`` enables it for direct OpenAI traffic and leaves it off for "
            "OpenAI-compatible gateways; ``enabled`` forces it on; ``disabled`` "
            "forces sequential tool calling."
        ),
    )
    openai_prompt_cache_mode: OpenAIPromptCacheMode = Field(
        default="auto",
        description=(
            "Whether CALIBER should attach OpenAI ``prompt_cache_key`` hints to "
            "workflow-agent requests. ``auto`` enables the hints for direct "
            "api.openai.com traffic and leaves them off for OpenAI-compatible "
            "gateways; ``enabled`` forces them on; ``disabled`` omits the hint."
        ),
    )
    openai_prompt_cache_retention: OpenAIPromptCacheRetention = Field(
        default="default",
        description=(
            "Prompt-cache retention policy for OpenAI workflow-agent requests. "
            "``default`` defers to OpenAI's model/org default; ``in_memory`` and "
            "``24h`` request explicit retention when the target model supports it."
        ),
    )

    workflow_llm_judge_enabled: bool = Field(
        default=False,
        description=(
            "When true (and a real ``llm_provider`` with a key), workflow "
            "refinement eval scores run OUTPUTS with an LLM judge (quality "
            "dimension) instead of the structural completion/guardrail proxy "
            "(golden-path roadmap, Wave 5.2). Default off — eval is unchanged "
            "until opted in. Falls back to the structural score on any judge error."
        ),
    )

    refinement_max_iterations: int = Field(
        default=0,
        ge=0,
        description=(
            "Max automatic candidate→eval→re-candidate iterations on a failed "
            "regression gate before the job is rejected. 0 (default) = off: a "
            "failed gate rejects immediately. When >0, the eval stage feeds the "
            "gate reasons back as review notes and re-runs the candidate stage "
            "up to this many extra times, so the optimizer can self-correct."
        ),
    )

    # LLM circuit breaker (parity checklist §5.24). Wraps the LLM
    # provider so a misbehaving upstream doesn't burn through retry
    # budget — when the circuit is open, the worker re-queues jobs
    # instead of failing them.

    llm_circuit_breaker_enabled: bool = Field(
        default=True,
        description=(
            "When true, the configured LLM provider is wrapped with a "
            "circuit breaker. Disable for diagnostics where you want "
            "the raw provider behavior."
        ),
    )
    llm_circuit_failure_threshold: int = Field(
        default=5,
        ge=1,
        description=(
            "Number of provider failures within "
            "``llm_circuit_window_seconds`` that trips the breaker."
        ),
    )
    llm_circuit_window_seconds: float = Field(
        default=60.0,
        gt=0,
        description=(
            "Rolling window for the failure count. Failures older than "
            "this age out and don't count toward the trip threshold."
        ),
    )
    llm_circuit_open_duration_seconds: float = Field(
        default=30.0,
        gt=0,
        description=(
            "How long the breaker stays open before allowing a probe "
            "call through (transitioning to half-open). On success the "
            "circuit closes; on failure it re-opens for another full "
            "cooldown."
        ),
    )

    artifact_store_provider: str = Field(
        default="fake",
        description=(
            "Which ArtifactStore implementation to construct. ``mlflow`` reads "
            "active prompts from the MLflow Prompt Registry via the ``@prod`` "
            "alias convention; ``fake`` returns nothing (safe default so the "
            "server boots without a registry configured)."
        ),
    )

    eval_provider: str = Field(
        default="fake",
        description=(
            "Which EvalProvider to construct. ``mlflow`` wraps "
            "``mlflow.genai.evaluate`` (production); ``fake`` returns canned "
            "scores (default; safe for development and CI)."
        ),
    )

    promoter_provider: str = Field(
        default="fake",
        description=(
            "Which Promoter to construct on approve. ``mlflow`` registers a "
            "new prompt version and rotates the ``@prod`` alias; ``fake`` "
            "records the promotion in-memory and returns a synthetic ref. "
            "Default is ``fake`` so the server boots without an MLflow "
            "Prompt Registry write path configured."
        ),
    )

    # ------------------------------------------------------------------
    # Observability / MLflow tracing (golden-path roadmap, Wave 0-1)
    # ------------------------------------------------------------------

    tracing_enabled: bool = Field(
        default=True,
        description=(
            "Master switch for CALIBER MLflow tracing (workflow runs, agent "
            "turns, tool calls, token/cost). On by default but inert when MLflow "
            "is unavailable; set false to disable all CALIBER-emitted spans/runs."
        ),
    )
    tracing_autolog_enabled: bool = Field(
        default=True,
        description=(
            "When true (and ``tracing_enabled``), enable mlflow.openai / "
            "mlflow.anthropic autolog at startup so LLM calls and their token "
            "usage are captured automatically. Only patches provider SDKs that "
            "are actually installed."
        ),
    )
    tracing_max_attribute_bytes: int = Field(
        default=4096,
        ge=256,
        description=(
            "Per-attribute byte cap for span attributes / run tags, applied "
            "after PII redaction. Large tool inputs/outputs are truncated."
        ),
    )
    tracing_experiment: str = Field(
        default="",
        description=(
            "Optional MLflow experiment name for CALIBER runs. Empty inherits "
            "the ambient experiment of the host MLflow server."
        ),
    )
    allure_report_dir: str = Field(
        default="",
        description=(
            "Directory holding a generated Allure HTML report; CALIBER serves it "
            "in-app at /observability/allure-report. Empty falls back to "
            "caliber/caliber-ui/allure-report relative to the working directory."
        ),
    )

    # ------------------------------------------------------------------
    # Reliability / shutdown
    # ------------------------------------------------------------------

    janitor_interval_seconds: float = Field(
        default=60.0,
        gt=0,
        description="Seconds between janitor sweeps for stale-heartbeat jobs.",
    )
    janitor_stale_threshold_seconds: float = Field(
        default=300.0,
        gt=0,
        description=(
            "A ``running`` job whose ``last_heartbeat_at`` is older than "
            "``now - this`` is presumed crashed and reaped by the janitor. "
            "Must be comfortably longer than the longest expected single stage."
        ),
    )
    workflow_run_retention_days: float = Field(
        default=0.0,
        ge=0,
        description=(
            "Retention window for indexed workflow preview/run rows. ``0`` "
            "disables pruning; positive values let the janitor delete runs "
            "older than this many days."
        ),
    )
    workflow_run_queue_enabled: bool = Field(
        default=False,
        description=(
            "When true, ``POST /workflow-runs`` accepts async queue submissions "
            "and persists queued lifecycle records. Disabled by default to keep "
            "legacy sync run behavior as the safe rollout baseline."
        ),
    )
    workflow_run_runtime_approvals_enabled: bool = Field(
        default=False,
        description=(
            "Enables runtime approval pause/resume capabilities in the workflow-run "
            "lifecycle. Routes and UI should hide approval-specific controls while off."
        ),
    )
    workflow_run_checkpointing_enabled: bool = Field(
        default=False,
        description=(
            "Enables persisted run checkpoints and resume-related capabilities. "
            "When disabled, checkpoint listing and resume routes reject requests "
            "and workflow recovery controls should stay hidden."
        ),
    )
    workflow_run_event_backend: WorkflowRunEventBackend = Field(
        default="in_process",
        description=(
            "Live run-event fanout backend. ``in_process`` uses the local event bus; "
            "``database`` uses the shared ``caliber_live_events`` table; ``nats`` "
            "uses a shared NATS subject; ``redis`` uses a shared Redis pub/sub "
            "channel for multi-replica delivery."
        ),
    )
    nats_url: str = Field(
        default="nats://localhost:4222",
        description=(
            "NATS server URL used when ``workflow_run_event_backend=='nats'``. "
            "Comma-separated URLs are accepted for clustered deployments."
        ),
    )
    nats_subject: str = Field(
        default="caliber.events",
        description="NATS subject used for CALIBER live event fanout.",
    )
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description=(
            "Redis URL used when ``workflow_run_event_backend=='redis'``. "
            "Supports standalone, Sentinel-routed, and TLS-prefixed Redis deployments "
            "handled by redis-py."
        ),
    )
    redis_channel: str = Field(
        default="caliber.events",
        description="Redis pub/sub channel used for CALIBER live event fanout.",
    )
    workflow_run_worker_enabled: bool = Field(
        default=True,
        description=(
            "When true, the workflow-run queue worker starts with app lifespan and "
            "claims queued runs for execution."
        ),
    )
    workflow_run_worker_interval_seconds: float = Field(
        default=2.0,
        gt=0,
        description="Polling interval for workflow-run worker claim ticks.",
    )
    aria_plan_worker_interval_seconds: float = Field(
        default=10.0,
        gt=0,
        description=(
            "Polling interval for the Aria plan worker, which resolves in-flight "
            "async job steps (waiting_job) and resumes the plan when they finish."
        ),
    )
    workflow_scheduler_enabled: bool = Field(
        default=True,
        description=(
            "When true, the cron scheduler starts with app lifespan and enqueues runs "
            "for workflows whose Start node has a cron trigger."
        ),
    )
    workflow_scheduler_interval_seconds: float = Field(
        default=60.0,
        gt=0,
        description="Polling interval for the workflow cron scheduler (minute granularity).",
    )
    workflow_run_lease_seconds: float = Field(
        default=60.0,
        gt=0,
        description="Claim-lease duration for running workflow runs before recovery.",
    )
    slo_severities: str = Field(
        default="",
        description=(
            "Comma-separated 'objective=severity' pairs, e.g. "
            "'success_ratio>=0.95=critical,queue_depth<=100=warning'. Severity is operator "
            "configuration rather than something inferred from how far past target an "
            "observation is: how bad a breach is depends on the service, not the number. "
            "Unlisted objectives open at 'warning'. An unrecognised severity is ignored so "
            "one typo cannot stop the other objectives being evaluated."
        ),
    )
    slo_objectives: str = Field(
        default="",
        description=(
            "Comma-separated service-level objectives evaluated by "
            "caliber.observability.slo, e.g. "
            "'workflow_success_rate>=0.99,workflow_p95_latency_ms<=30000'. Empty means "
            "no objectives are declared; an objective naming an unknown signal is "
            "reported as a configuration error rather than silently ignored."
        ),
    )
    slo_window_minutes: float = Field(
        default=60.0,
        gt=0,
        description="Trailing window, in minutes, over which SLO signals are computed.",
    )
    workflow_queue_max_age_seconds: float = Field(
        default=300.0,
        gt=0,
        description=(
            "Backlog tolerance for the workflow run queue. A run waiting longer than "
            "this marks the queue operationally degraded, which is what separates "
            "'busy' from 'nothing is consuming the queue'. Read by "
            "caliber.observability.queue_health; it does not change scheduling."
        ),
    )
    knowledge_build_queue_enabled: bool = Field(
        default=True,
        description=(
            "When true, knowledge-base build requests are enqueued durably and "
            "processed by the background worker instead of running inline on the "
            "request thread."
        ),
    )
    knowledge_build_worker_enabled: bool = Field(
        default=True,
        description=(
            "When true, the knowledge-base build worker starts with app lifespan "
            "and claims queued knowledge-base runs for execution."
        ),
    )
    knowledge_build_worker_interval_seconds: float = Field(
        default=2.0,
        gt=0,
        description="Polling interval for knowledge-base worker claim ticks.",
    )
    knowledge_build_lease_seconds: float = Field(
        default=120.0,
        gt=0,
        description="Claim-lease duration for running knowledge-base builds before recovery.",
    )
    knowledge_graph_extractor_backend: KnowledgeGraphExtractorBackend = Field(
        default="heuristic",
        description=(
            "Entity extractor used to build knowledge-graph artifacts and graph-aware "
            "retrieval hints. ``heuristic`` is lightweight and dependency-free; "
            "``spacy`` enables named-entity extraction when the optional spaCy "
            "runtime and model are installed."
        ),
    )
    knowledge_graph_spacy_model: str = Field(
        default="en_core_web_sm",
        description=(
            "spaCy model to load when ``knowledge_graph_extractor_backend=='spacy'``. "
            "If the model is unavailable, CALIBER falls back to the heuristic "
            "extractor and records the fallback in graph metadata."
        ),
    )
    knowledge_age_enabled: bool = Field(
        default=False,
        description=(
            "When true, knowledge-base builds may sync their graph lineage into "
            "Apache AGE and the playground exposes AGE-backed retrieval."
        ),
    )
    knowledge_age_graph_name: str = Field(
        default="knowledge_graph",
        description=(
            "Shared Apache AGE graph name used for knowledge-base version sync "
            "and AGE-backed retrieval."
        ),
    )
    knowledge_age_viewer_url: str | None = Field(
        default=None,
        description=(
            "Optional Apache AGE viewer URL surfaced in the Knowledge Base UI so "
            "operators can jump directly into the shared graph console."
        ),
    )
    allow_flagged_local_embeddings: bool = Field(
        default=False,
        description=(
            "Override the runtime-advisory guard and allow local Hugging Face "
            "embedding builds to run even when the optional torch stack is "
            "flagged upstream. Intended only for accepted-risk, isolated deployments."
        ),
    )
    knowledge_pgvector_enabled: bool = Field(
        default=False,
        description=(
            "When true (and running on Postgres with the pgvector extension), "
            "dense retrieval generates its candidate pool with a SQL-side "
            "approximate-nearest-neighbour top-k over the chunk vector column "
            "instead of scanning every chunk in Python — the path that scales to "
            "millions of chunks. Ignored on SQLite (the Python scan is the fallback)."
        ),
    )
    knowledge_ann_candidate_pool_size: int = Field(
        default=50,
        ge=1,
        le=2000,
        description=(
            "First-stage retrieval candidate-pool size. Retrieval gathers this "
            "many candidates (by ANN / cosine) before the optional cross-encoder "
            "rerank narrows them to the requested top_k. Larger = better recall, "
            "slower rerank."
        ),
    )
    knowledge_embedding_dimension: int = Field(
        default=1024,
        ge=1,
        le=2000,
        description=(
            "Embedding dimension the pgvector ANN column + HNSW index are built "
            "for (pgvector's HNSW caps at 2000 dims). The curated models are 1024 "
            "(BGE-M3 / E5-large / Qwen3) or 384 (MiniLM); set this to the dimension "
            "of the model your knowledge bases use. Chunks whose embedding dimension "
            "differs are skipped for ANN and fall back to the in-memory cosine scan, "
            "so a mismatch degrades gracefully rather than failing."
        ),
    )
    knowledge_rerank_enabled: bool = Field(
        default=False,
        description=(
            "When true, a local cross-encoder re-scores the first-stage candidate "
            "pool and reorders it before the final top_k is returned — markedly "
            "better ranking quality at the cost of a local model run. Uses the "
            "same flagged-local-model guard as embeddings."
        ),
    )
    knowledge_rerank_model: str = Field(
        default="cross-encoder/ms-marco-MiniLM-L-6-v2",
        description=(
            "Hugging Face cross-encoder model id used for reranking when "
            "CALIBER_KNOWLEDGE_RERANK_ENABLED is set."
        ),
    )
    workflow_run_max_attempts: int = Field(
        default=3,
        ge=1,
        description="Maximum workflow-run attempts permitted for retry lineage.",
    )
    workflow_run_artifact_bucket: str = Field(
        default="",
        description=(
            "When set, the worker persists a completed run's file artifacts (e.g. a "
            "python_code node's kg.json / report.html) and a per-run JSON log to this "
            "object-store bucket, using the object_store_* connection settings. Empty "
            "(default) = no persistence, so behavior is unchanged unless opted in."
        ),
    )
    workflow_run_artifact_prefix: str = Field(
        default="pipeline",
        description="Key prefix under workflow_run_artifact_bucket for run artifacts.",
    )
    workflow_run_log_prefix: str = Field(
        default="logs",
        description="Key prefix under workflow_run_artifact_bucket for the per-run JSON log.",
    )
    shutdown_grace_seconds: float = Field(
        default=30.0,
        ge=0,
        description=(
            "On SIGTERM the background tasks (worker, poller, janitor) stop "
            "accepting new work and are given this many seconds to drain "
            "their current iteration before being cancelled."
        ),
    )
    background_tasks_enabled: bool = Field(
        default=True,
        description=(
            "When false, app lifespan startup skips the poller, worker, janitor, "
            "and webhook dispatcher loops. Primarily used by tests and local "
            "one-shot tooling."
        ),
    )

    dev_user: str = Field(
        default="",
        description=(
            "Optional development user identity used when no upstream "
            "``X-CALIBER-User`` header is present. Honoured ONLY when "
            "auth_dev_fallback_enabled is true, which is off by default: with the "
            "shipped Compose values this fallback made an unauthenticated request an "
            "admin."
        ),
    )

    # ------------------------------------------------------------------
    # Identity boundary (C1)
    # ------------------------------------------------------------------
    # ``session`` is the default: CALIBER validates a password against
    # caliber_user_accounts and issues a revocable session. ``trusted_header``
    # restores the previous posture for deployments that genuinely sit behind an
    # identity proxy — now an explicit, documented choice rather than the
    # unconditional behaviour.

    # ------------------------------------------------------------------
    # Encrypted secret store (C2)
    # ------------------------------------------------------------------
    # Without a key the store refuses to operate rather than storing plaintext
    # under an "encrypted" name — a silent downgrade would look fixed while
    # reproducing the original defect.

    secret_encryption_key_source: str = Field(
        default="",
        description=(
            "Env var or caliber.secrets URI holding the 32-byte AES-256 data-encryption "
            "key (base64 or hex) for caliber_secret_versions. Empty disables the secret "
            "store: 'secret://name' references then resolve to nothing, which callers "
            "treat as unresolved rather than falling back to plaintext. Generate one "
            'with: python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"'
        ),
    )
    secret_encryption_additional_keys: str = Field(
        default="",
        description=(
            "Comma-separated additional key sources retained so secret versions written "
            "before a key rotation still decrypt. Encryption always uses the primary key."
        ),
    )

    auth_mode: str = Field(
        default="session",
        pattern="^(session|trusted_header)$",
        description=(
            "How request identity is established. 'session' validates credentials "
            "server-side and issues a revocable session (default). 'trusted_header' "
            "trusts X-CALIBER-User and is only safe behind a proxy that sets it; "
            "combine it with auth_trusted_proxy_secret_env so a request that bypasses "
            "the proxy is rejected."
        ),
    )
    auth_dev_fallback_enabled: bool = Field(
        default=False,
        description=(
            "Honour dev_user when a request carries no identity at all. OFF by "
            "default: with the shipped admin lists this turned an unauthenticated "
            "request into an admin. Never enable on a reachable deployment."
        ),
    )
    auth_trusted_proxy_secret_env: str = Field(
        default="",
        description=(
            "Env var or caliber.secrets URI holding a shared secret the trusted proxy "
            "must send in X-CALIBER-Proxy-Secret. When set, trusted_header mode "
            "rejects any request without it, so bypassing the proxy is not enough to "
            "assert an identity."
        ),
    )
    auth_session_ttl_seconds: float = Field(
        default=43200.0,
        gt=0,
        description="Session lifetime in seconds (default 12 hours).",
    )
    auth_session_cookie_name: str = Field(
        default="caliber_session",
        description="Name of the HttpOnly cookie carrying the session token.",
    )
    auth_session_cookie_secure: bool = Field(
        default=True,
        description=(
            "Mark the session cookie Secure. Default true; set false only for plain "
            "HTTP local development, where the browser would otherwise drop it."
        ),
    )
    auth_bootstrap_admin_user: str = Field(
        default="admin",
        description=(
            "User id to create on startup when no account exists yet, so a fresh "
            "session-mode deployment is reachable. Loopback development launchers may "
            "explicitly opt into a one-time admin/admin bootstrap when no password "
            "source is set. Set auth_bootstrap_admin_password_env to use an operator "
            "secret instead."
        ),
    )
    auth_bootstrap_admin_password_env: str = Field(
        default="",
        description=(
            "Env var or caliber.secrets URI holding the bootstrap admin's password. "
            "Never the password itself, so it cannot land in a config dump."
        ),
    )
    auth_bootstrap_allow_insecure_default: bool = Field(
        default=False,
        description=(
            "Explicitly allow the one-time admin/admin bootstrap when the session cookie "
            "is also non-Secure. OFF by default; local loopback launchers may opt in, but "
            "a network-reachable deployment must supply a strong password source instead."
        ),
    )

    # ------------------------------------------------------------------
    # Caliber Assistant
    # ------------------------------------------------------------------

    assistant_enabled: bool = Field(
        default=True,
        description="Whether Caliber Assistant routes should accept requests.",
    )
    assistant_skill_runtime_enabled: bool = Field(
        default=True,
        description="Whether assistant turns may resolve and inject active CALIBER skills.",
    )
    builtin_skills_auto_seed: bool = Field(
        default=False,
        description=(
            "When true, the Skills list endpoint idempotently seeds CALIBER's "
            "public built-in skill catalog. Default false keeps tests and "
            "library-style app construction explicit; container deployments "
            "enable it."
        ),
    )
    assistant_engine: str = Field(
        default="auto",
        description=(
            "Assistant engine: auto (default), openai, anthropic, ollama, or fake. "
            "'auto' selects a real provider by available API key — OpenAI if "
            "OPENAI_API_KEY is set, else Anthropic (Claude) if ANTHROPIC_API_KEY "
            "is set, else OpenAI. The 'fake' deterministic stub is for tests only "
            "and is never selected automatically."
        ),
    )
    assistant_model: str = Field(
        default="",
        description=(
            "Model id for the assistant engine. Empty means use the resolved "
            "provider's default (OpenAI gpt-5.6-luna / Anthropic claude-sonnet-4)."
        ),
    )
    assistant_reasoning: str = Field(
        default="medium",
        description="Reasoning effort/setting for the OpenAI assistant engine.",
    )
    assistant_disabled_intents: str = Field(
        default="",
        description="Comma-separated assistant intent names to disable at runtime.",
    )
    assistant_disabled_domains: str = Field(
        default="",
        description="Comma-separated assistant domains to disable at runtime.",
    )
    assistant_max_turns: int = Field(default=30, ge=1)
    assistant_max_questions_per_turn: int = Field(default=3, ge=1)
    assistant_max_drafts_per_session: int = Field(default=20, ge=1)
    assistant_publish_requires_approval: bool = Field(default=True)
    assistant_reviewer_user: str = Field(
        default="",
        description=(
            "Service identity used by agent_review/full_autonomy. It must be distinct "
            "from the author and listed in CALIBER_APPROVER_USERS or CALIBER_ADMIN_USERS."
        ),
    )
    assistant_release_user: str = Field(
        default="",
        description=(
            "Service identity that publishes after an autonomous review. It must be "
            "distinct from author/reviewer and carry caliber.operator."
        ),
    )
    assistant_reviewer_policy_version: str = Field(default="aria-draft-review-v1")
    assistant_reviewer_min_confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    assistant_review_ttl_seconds: int = Field(default=3600, ge=1)
    assistant_tool_source_max_bytes: int = Field(default=200_000, ge=1)
    assistant_run_timeout_seconds: float = Field(default=60.0, gt=0)
    provider_request_timeout_seconds: float = Field(
        default=_PROVIDER_REQUEST_TIMEOUT_DEFAULT,
        gt=0,
        description=(
            "Wall-clock ceiling for a single LLM provider HTTP request, applied to "
            "the OpenAI and Anthropic clients the workflow runtime and Aria build. "
            "Without it the SDK defaults govern — openai ships a 600s read timeout "
            "with 2 automatic retries, so one model call could hold a worker for "
            "half an hour and no CALIBER-side deadline would be reached."
        ),
    )

    # ------------------------------------------------------------------
    # Tool sandbox
    # ------------------------------------------------------------------

    #: Run admin-registered Python tools in a subprocess instead of importing them into
    #: the control plane (C8). **Default on**, because the alternative is that a registered
    #: module shares the API server's memory, descriptors, environment, and credentials —
    #: and its *import* runs there too, which an allowlist narrows but does not contain.
    #:
    #: Turning this off restores in-process execution. The only intended use is a test that
    #: needs to monkeypatch a tool's module attribute, which cannot work across a process
    #: boundary; production deployments should leave it on. It is deliberately not
    #: off-by-default: a containment boundary that an operator has to discover and enable is
    #: the "decorative control" this codebase has repeatedly been audited for.
    registered_tool_sandbox_enabled: bool = True
    tool_sandbox_timeout_seconds: float = Field(default=5.0, gt=0)
    #: Timeout for a **registered module** run, kept separate from the source-snippet
    #: timeout above because the two budgets pay for different things.
    #:
    #: A source snippet is a few lines with no heavy imports, so 5s is 5s of execution. A
    #: registered module runs in a cold ``python -I`` that must import the module and
    #: whatever it depends on: measured at ~0.05s for a trivial module but ~0.55s for one
    #: importing the caliber package, on an idle machine. Under a saturated host that
    #: startup alone exceeded the 5s budget and a working tool was reported as timed out.
    #:
    #: This is the requested tool-call budget. The isolated child starts an uncatchable
    #: watchdog after decoding the request; that one deadline covers source/module
    #: resolution, signature inspection, test-suite execution, and invocation. Interpreter
    #: startup necessarily happens before the child can start the watchdog and is bounded
    #: separately by the parent's budget-plus-startup-grace backstop.
    registered_tool_sandbox_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    tool_sandbox_backend: str = Field(
        default="",
        description=(
            "Optional 'package.module:Factory' implementing the ToolSandbox protocol, "
            "called with the config. Empty uses the built-in subprocess sandbox, which is "
            "a process boundary — separate interpreter, empty environment, private working "
            "directory, POSIX rlimits — and NOT a container, VM, or seccomp boundary: the "
            "child keeps ambient filesystem and network access on the host. That is "
            "appropriate for trusted tool authors and is not isolation for untrusted ones. "
            "Portable Python cannot provide OS-enforced isolation (namespaces are "
            "Linux-only and privileged, seccomp needs a native binding, containers are "
            "infrastructure), so a deployment that needs it supplies a Docker/gVisor/"
            "Firecracker-backed factory here instead of forking."
        ),
    )
    # Caps a sandboxed node's serialized return. 64 KiB was too small for nodes
    # that emit data/HTML (e.g. a KG report); 1 MiB is a safer default, and big
    # document pipelines raise it via CALIBER_TOOL_SANDBOX_MAX_OUTPUT_BYTES.
    tool_sandbox_max_output_bytes: int = Field(default=1_048_576, gt=0)
    tool_sandbox_max_memory_bytes: int = Field(default=268_435_456, ge=33_554_432)
    tool_sandbox_max_file_bytes: int = Field(default=1_048_576, gt=0)
    tool_sandbox_max_open_files: int = Field(default=32, ge=8, le=4096)

    # MCP execution containment.  Local stdio servers are child processes, not
    # an OS security sandbox: the command/host allowlists and private working
    # directory reduce ambient authority, while production aliases can require
    # a verified namespace profile, operator-attested managed sidecar, or remote
    # HTTPS MCP boundary.
    mcp_stdio_command_allowlist: str = Field(
        default="${PYTHON}",
        description=(
            "Comma-separated stdio executables allowed for MCP child processes. "
            "${PYTHON} means the interpreter running CALIBER."
        ),
    )
    mcp_stdio_safe_path: str = Field(
        default=os.defpath,
        description=(
            "CALIBER-controlled PATH supplied to stdio MCP children. Server records "
            "cannot override PATH; add directories here only when an allowlisted "
            "launcher needs them."
        ),
    )
    mcp_stdio_python_module_allowlist: str = Field(
        default="caliber.mcp_servers.db",
        description=(
            "Comma-separated Python modules allowed after '-m' when the MCP stdio "
            "command resolves to the CALIBER interpreter."
        ),
    )
    mcp_stdio_python_script_allowlist: str = Field(
        default="",
        description="Comma-separated absolute Python MCP script paths explicitly allowed to run.",
    )
    mcp_remote_host_allowlist: str = Field(
        default="localhost,127.0.0.1,::1",
        description="Comma-separated exact hostnames allowed for remote MCP transports.",
    )
    mcp_managed_sidecar_hosts: str = Field(
        default="",
        description=(
            "Comma-separated remote MCP hosts that the deployment operator attests run "
            "in a separately isolated sidecar boundary."
        ),
    )
    mcp_allow_insecure_http: bool = Field(
        default=False,
        description="Allow plain HTTP for non-loopback MCP hosts (disabled by default).",
    )
    mcp_stdio_isolated_workdir: bool = Field(
        default=True,
        description="Start each stdio MCP session in a fresh private temporary directory.",
    )
    mcp_stdio_isolation_prefix: str = Field(
        default="",
        description=(
            "Optional shell-style argv prefix for an operator-managed isolation wrapper. "
            "It is parsed without a shell and prepended to the MCP executable."
        ),
    )
    mcp_stdio_isolation_profile: str = Field(
        default="none",
        pattern="^(none|bubblewrap)$",
        description=(
            "Recognized stdio containment profile. 'bubblewrap' requires CALIBER's "
            "exact network-unshared, read-only-root namespace argv, but never counts "
            "as a production isolation boundary."
        ),
    )
    # ------------------------------------------------------------------
    # Outbound egress policy (SSRF defence)
    # ------------------------------------------------------------------
    # A webhook/api_request URL comes from a manifest and CALIBER runs inside the
    # deployment's network, so without this a workflow could reach the cloud
    # instance-metadata endpoint, CALIBER's own API on loopback, or anything in the
    # VPC. Checked against the RESOLVED address, because a hostname allowlist alone
    # does not stop a name that resolves to 169.254.169.254.

    approval_allow_self_approval: bool = Field(
        default=False,
        description=(
            "Allow the account that triggered a run to approve its own human-approval "
            "gate. Off by default: that is the one separation-of-duties rule that is "
            "meaningful without a role hierarchy. Enable it for a single-operator "
            "install, which would otherwise deadlock on its own approval gates."
        ),
    )

    egress_policy_enabled: bool = Field(
        default=True,
        description=(
            "Enforce outbound egress policy on workflow webhook/api_request nodes. "
            "Disabling it removes the SSRF defence entirely; prefer adding specific "
            "hosts to egress_allowed_hosts."
        ),
    )
    egress_blocked_categories: str = Field(
        default="link_local,loopback,private,other_reserved",
        description=(
            "Address categories workflow HTTP nodes may not reach: link_local "
            "(includes the cloud metadata endpoint), loopback (CALIBER's own API and "
            "MCP sidecars), private (RFC1918/unique-local), other_reserved "
            "(multicast/unspecified/reserved). Empty falls back to all four."
        ),
    )
    egress_allowed_hosts: str = Field(
        default="",
        description=(
            "Comma-separated hostnames or literal addresses always permitted, even if "
            "they resolve into a blocked category. This is how an internal service "
            "stays reachable without reopening the metadata endpoint."
        ),
    )

    egress_allow_unresolvable_hosts: bool = Field(
        default=False,
        description=(
            "Permit an outbound host this process cannot resolve. Off by default: the "
            "policy check and the connection perform independent DNS lookups, so a name "
            "that fails here can succeed at connect time and return the metadata "
            "address — meaning the one case with no vetted address would be the one case "
            "that skipped vetting. Enable only when egress is routed through a proxy that "
            "enforces policy itself, because this process then has nothing to check."
        ),
    )

    external_app_entrypoint_allowlist: str = Field(
        default="",
        description=(
            "Comma-separated 'package.module:callable' entrypoints an external_app node "
            "may invoke. Empty means NONE are permitted: the node imports and calls "
            "installed Python in the control-plane process, and configuration shipped "
            "allowlists for every other execution surface (MCP commands, Python "
            "modules/scripts, remote hosts) but not this one. Entries may end in '*' to "
            "allow a module prefix, e.g. 'mycompany.integrations.*'."
        ),
    )

    registered_tool_module_allowlist: str = Field(
        default="",
        description=(
            "Comma-separated module prefixes a registered tool may be imported from, e.g. "
            "'caliber.*,mycompany.tools.*'. By default, a registered module is imported "
            "and invoked in the configured sandbox child; disabling that boundary restores "
            "in-process execution without disabling this allowlist. Empty means unrestricted, "
            "which is the shipped default because tool registration already requires the "
            "admin scope and a fail-closed default would break every existing install on "
            "upgrade — but an unset allowlist is reported by /readiness as an unset control "
            "rather than passing silently. Entries may end in '*' to allow a prefix."
        ),
    )

    mcp_require_external_isolation_for_aliases: str = Field(
        default="prod",
        description=(
            "Legacy explicit alias list that rejects local stdio MCP execution. Retained "
            "for backward compatibility and honoured as an ADDITIONAL opt-in; the primary "
            "rule is now the environment class (see "
            "mcp_require_external_isolation_for_environment_classes), because keying a "
            "safety requirement to an alias string let 'production'/'prod-eu'/'PROD' "
            "through."
        ),
    )
    mcp_require_external_isolation_for_environment_classes: str = Field(
        default="production",
        description=(
            "Comma-separated environment classes (production/staging/development) whose "
            "deployments reject local stdio MCP execution. Resolved from the alias by "
            "caliber.deployment_environments, so spelling variants cannot bypass it."
        ),
    )
    workflow_host_path_nodes_allowed_environment_classes: str = Field(
        default="development",
        description=(
            "Comma-separated environment classes (production/staging/development) whose "
            "deployments may use unmanaged host-filesystem nodes — a file_input without "
            "a managed file_ref, a folder_input, or an output_folder. Everywhere else, "
            "promotion refuses the version. These nodes read and write arbitrary paths "
            "as the server user, so they are a development affordance; widen this only "
            "for a deployment whose authors are as trusted as its operators."
        ),
    )
    metrics_token_env: str = Field(
        default="",
        description=(
            "Name of the secret source holding a bearer token that GET /metrics "
            "requires. Accepts an env-var name or a caliber.secrets URI, like the "
            "CSRF signing secret. Empty (the default) leaves /metrics open, which "
            "preserves existing Prometheus scrape configs on upgrade and is only "
            "safe behind a network policy that keeps the endpoint internal."
        ),
    )
    tool_sandbox_require_external_isolation_for_environment_classes: str = Field(
        default="production",
        description=(
            "Comma-separated environment classes (production/staging/development) whose "
            "deployments refuse registered tools unless CALIBER_TOOL_SANDBOX_BACKEND "
            "supplies an OS-enforced isolation boundary. Defaults to 'production', "
            "matching CALIBER_MCP_REQUIRE_EXTERNAL_ISOLATION_FOR_ENVIRONMENT_CLASSES: "
            "the built-in subprocess sandbox is a process boundary, not a container or "
            "seccomp boundary, so a production alias running registered tool code now "
            "has to say which isolation backend it trusts. Set to '' to restore the "
            "previous never-refuse behaviour — a deployment whose tool authors are as "
            "trusted as its operators is the case that justifies it."
        ),
    )
    workflow_file_root: str = Field(
        default="",
        description=(
            "Absolute directory that unmanaged host-filesystem nodes (file_input "
            "without a managed file_ref, folder_input, output_folder) may read and "
            "write within. Paths are resolved — symlinks included — and refused if "
            "they land outside. Three states: EMPTY (the default) refuses these "
            "nodes outright; a directory confines them to it; the literal "
            "'unconfined' allows any path the server can reach, which was the "
            "default before confinement was made fail-closed. The promotion gate "
            "(CALIBER_WORKFLOW_HOST_PATH_NODES_ALLOWED_ENVIRONMENT_CLASSES) still "
            "keeps these nodes out of production aliases; this setting is the "
            "data-plane half, so the gate is no longer the only defence."
        ),
    )
    deployment_environment_classes: str = Field(
        default="",
        description=(
            "Optional explicit alias-to-environment-class map, e.g. "
            "'blue=production,green=production,demo=development'. Wins over the built-in "
            "name patterns so a house-style alias can be classified correctly."
        ),
    )
    release_require_quality_gate_for_environment_classes: str = Field(
        default="production",
        description=(
            "Environment classes whose promotions must have a passing deploy gate. "
            "Rotating a production alias onto a version with no graded evidence is "
            "refused; set to '' to allow it (development installs, migrations)."
        ),
    )
    release_require_graded_executor_for_environment_classes: str = Field(
        default="production",
        description=(
            "Environment classes whose deploy gate must be graded by a real configured "
            "model rather than the deterministic fake executor. A production verdict "
            "produced by the fake proves the graph and the data, not the model that "
            "will serve traffic, so presenting it as release evidence is false "
            "confidence; set to '' for installs that deliberately run CALIBER with "
            "CALIBER_LLM_PROVIDER=fake."
        ),
    )
    release_require_human_approval_for_environment_classes: str = Field(
        default="",
        description=(
            "Environment classes whose promotions pause for a human approval instead "
            "of rotating immediately. Empty ships single-environment behaviour; set to "
            "'production' to require sign-off. The promotion/approval machinery is "
            "fully wired, so this is configuration rather than a code change."
        ),
    )
    deployment_default_environment_class: str = Field(
        default="production",
        pattern="^(production|staging|development)$",
        description=(
            "Environment class for an alias that matches no pattern or explicit mapping. "
            "Defaults to 'production' so an unrecognised alias fails closed into the "
            "strictest requirements rather than silently escaping them."
        ),
    )
    # Bounded concurrency for a ForEach node whose target is an agent: how many
    # items run their agent call in parallel. 1 (default) = sequential, identical
    # to pre-concurrency behavior. Raising it lets large fan-out document
    # pipelines (e.g. per-chunk extraction) parallelize their LLM calls. Each
    # worker runs in a copied context so its agent span nests under the same
    # ForEach trace; only the agent-target branch parallelizes (subworkflow
    # targets stay sequential).
    workflow_foreach_max_workers: int = Field(default=1, ge=1, le=64)

    # Ceiling on concurrent *parallel-branch* threads. The pool used to be sized to
    # the branch count with no cap, so a wide graph spawned one thread per branch:
    # a manifest-authoring choice could exhaust the process's threads. Excess
    # branches queue, so raising or lowering this changes throughput, not results.
    workflow_parallel_branch_max_workers: int = Field(default=8, ge=1, le=64)

    # ------------------------------------------------------------------
    # Object store (MinIO / S3) console
    # ------------------------------------------------------------------
    # The Object Store UI manages buckets + objects against this S3-compatible
    # endpoint via boto3. Distinct from the workflow file-storage backend; the
    # defaults target the suite's local MinIO. Credentials are *sources*
    # (env-var names or caliber.secrets URIs), never literal keys.

    object_store_endpoint_url: str = Field(
        default="http://localhost:9000",
        description="S3-compatible endpoint the Object Store console manages (e.g. MinIO).",
    )
    object_store_region: str = Field(default="us-east-1")
    object_store_access_key_source: str = Field(
        default="MINIO_ROOT_USER",
        description="Secret source (env var name or caliber.secrets URI) for the access key.",
    )
    object_store_secret_key_source: str = Field(
        default="MINIO_ROOT_PASSWORD",
        description="Secret source for the secret key.",
    )
    object_store_force_path_style: bool = Field(
        default=True,
        description="Use path-style addressing (required by MinIO).",
    )

    # ------------------------------------------------------------------
    # RBAC — see the parity checklist §11.
    # User-list assignments are config-driven for the first cut: each
    # field is a comma-separated set of user IDs the upstream proxy /
    # MLflow auth surfaces via the ``X-CALIBER-User`` header. A future
    # milestone can swap this for a DB-backed assignment table without
    # touching call sites — the resolver in :mod:`caliber.auth`
    # already returns ``frozenset[str]``.
    # ------------------------------------------------------------------

    admin_users: str = Field(
        default="",
        description=(
            "Comma-separated user IDs granted the ``caliber.admin`` scope. "
            "Admins get every other scope implicitly and can call the "
            "batch-approve endpoint."
        ),
    )
    approver_users: str = Field(
        default="",
        description=(
            "Comma-separated user IDs granted the ``caliber.approver`` scope. "
            "Approvers can resolve approval requests (approve/reject/"
            "request-changes). Admins inherit this scope."
        ),
    )
    operator_users: str = Field(
        default="",
        description=(
            "Comma-separated user IDs granted the ``caliber.operator`` scope. "
            "Operators can submit feedback, verify items, register agents, "
            "and roll back. Admins inherit this scope."
        ),
    )

    # ------------------------------------------------------------------
    # Outbound webhooks. Notifications go out on state-changing events
    # (approvals, verifications, rollbacks, job failures) so an external
    # service (PagerDuty, Slack, an internal bus) can react. Every
    # outbound POST is signed with HMAC-SHA256 so receivers can verify
    # authenticity and reject replays older than 5 minutes.
    # ------------------------------------------------------------------

    webhook_urls: str = Field(
        default="",
        description=(
            "Comma-separated list of HTTPS URLs that receive CALIBER webhook "
            "notifications. Empty (the default) disables the dispatcher entirely."
        ),
    )
    webhook_signing_secret_env: str = Field(
        default="CALIBER_WEBHOOK_SIGNING_SECRET",
        description=(
            "Source of the webhook signing secret. Accepts either an "
            "env var name (bare string; backwards-compatible) or a "
            ":mod:`caliber.secrets` URI (``env://VAR``, "
            "``file:///abs/path``). Kept as a *source*, not the value "
            "itself, so the secret never lands in the resolved config "
            "(and therefore never in audit logs)."
        ),
    )
    webhook_event_filter: str = Field(
        default=(
            "approval.promoted,approval.rejected,approval.changes_requested,"
            "verification.verified,job.failed,agent.rolled_back,"
            "slo.incident.opened,slo.incident.resolved"
        ),
        description=(
            "Comma-separated event types the dispatcher subscribes to. The "
            "default covers the state changes operators usually want to be "
            "paged on; ``*`` subscribes to every event."
        ),
    )

    # ------------------------------------------------------------------
    # Feedback → eval-dataset harvesting (gap-analysis R2.3). When a human
    # verifies a feedback item, the correction is a durable learning signal;
    # CALIBER captures it as a versioned, append-only eval-dataset example so
    # the refinement loop compounds instead of informing only one job.
    # ------------------------------------------------------------------

    feedback_harvest_enabled: bool = Field(
        default=True,
        description=(
            "When true, verifying a feedback item appends the human "
            "correction to the agent's eval dataset — its configured "
            "dataset if ``eval_thresholds['eval_dataset_id']`` is set, "
            "else an auto-created ``caliber-feedback/{agent_id}`` dataset "
            "— as an append-only, versioned example. Operators can "
            "disable to keep eval datasets strictly hand-curated."
        ),
    )

    # ------------------------------------------------------------------
    # PII redaction (parity checklist §11, dev plan §9.1 item 5.22).
    # Applied at audit-log write time so the DB never holds raw PII.
    # ------------------------------------------------------------------

    pii_redaction_enabled: bool = Field(
        default=True,
        description=(
            "When true, audit-log ``details`` values are scanned for "
            "emails / phone numbers / SSNs and matches are replaced with "
            "``pii_redaction_replacement``. Operators can disable for "
            "compliance regimes that *require* unredacted forensic data."
        ),
    )
    pii_redaction_replacement: str = Field(
        default="[REDACTED]",
        description="Literal string substituted in for each PII match.",
    )
    pii_redaction_extra_patterns: str = Field(
        default="",
        description=(
            "Newline-separated additional regex patterns to redact. "
            "Newlines (not commas) so an operator can include a comma "
            "inside a regex without escaping. Useful for API-key "
            "prefixes (``sk-...``), AWS access keys (``AKIA...``), JWTs, "
            "and similar deployment-specific tokens."
        ),
    )

    # ------------------------------------------------------------------
    # CSRF protection (parity checklist §11). Opt-in because the common
    # deployment shape — MLflow's auth proxy in front of CALIBER —
    # already handles cross-site forgery via SameSite cookies plus the
    # proxy's own origin enforcement. Deployments that don't have that
    # protection enable it here.
    # ------------------------------------------------------------------

    csrf_enabled: bool = Field(
        default=False,
        description=(
            "When true, ``POST/PATCH/PUT/DELETE`` requests must include "
            "an ``X-CALIBER-CSRF`` header carrying a token from "
            "``GET /caliber/csrf``. The token is HMAC-signed and bound "
            "to the user identity, so an attacker can't forge tokens "
            "for other users without the signing secret."
        ),
    )
    csrf_signing_secret_env: str = Field(
        default="CALIBER_CSRF_SIGNING_SECRET",
        description=(
            "Source of the CSRF signing secret. Accepts either an env "
            "var name (bare string; backwards-compatible) or a "
            ":mod:`caliber.secrets` URI (``env://VAR``, "
            "``file:///abs/path``). Stored as a *source*, not the "
            "value, so the secret never lands in the resolved config "
            "object."
        ),
    )
    csrf_token_ttl_seconds: int = Field(
        default=3600,
        gt=0,
        description=(
            "How long a CSRF token stays valid after issuance. One hour "
            "is the conventional sweet spot — short enough that a leaked "
            "token is mostly useless, long enough that an active user "
            "doesn't have to re-fetch every few seconds."
        ),
    )

    # ------------------------------------------------------------------
    # Published workflow-service ingress. This limit is intentionally
    # independent of the optional per-user request-rate limiter: public
    # services have no CALIBER user identity, while protected services must
    # reject a bad Bearer token before consuming their request body.
    # ------------------------------------------------------------------

    service_invoke_max_body_bytes: int = Field(
        default=1_048_576,
        ge=1,
        description=(
            "Maximum raw JSON-envelope bytes accepted by a published workflow-service "
            "invoke request. The route counts streamed ASGI chunks rather than trusting "
            "Content-Length, so chunked and understated requests remain bounded."
        ),
    )

    # ------------------------------------------------------------------
    # Per-user rate limiting (parity checklist §11). Opt-in because
    # most deployments already rate-limit at the gateway (NGINX/Envoy/
    # corporate WAF). Single-replica only — state is in-process so two
    # replicas don't share counters. A Redis-backed store is a future
    # enhancement when multi-replica deployments warrant it.
    # ------------------------------------------------------------------

    rate_limit_enabled: bool = Field(
        default=False,
        description=(
            "When true, ``RateLimitMiddleware`` enforces a per-user "
            "token-bucket budget. Anonymous traffic shares one bucket "
            "keyed ``anonymous`` so a single client without identity "
            "can't drown out legit traffic."
        ),
    )
    rate_limit_requests_per_minute: float = Field(
        default=120.0,
        gt=0,
        description=(
            "Sustained per-user request rate. Becomes the refill rate "
            "(``requests_per_minute / 60`` per second) for each bucket. "
            "120/min ≈ 2/sec is a comfortable ceiling for an active "
            "human reviewer; bump it for service-account identities."
        ),
    )
    rate_limit_burst: int = Field(
        default=30,
        ge=1,
        description=(
            "Maximum bucket size — the largest spike a single user can "
            "fire off without hitting 429. Defaults to a quarter of "
            "``requests_per_minute`` so short bursts of dashboard "
            "refreshes pass cleanly while sustained abuse trips the "
            "limit."
        ),
    )
    rate_limit_max_buckets: int = Field(
        default=10_000,
        ge=1,
        description=(
            "Cap on the in-memory per-user bucket dict. When the cap is "
            "hit, the least-recently-used bucket is evicted (the next "
            "request from that principal gets a fresh full bucket). "
            "Prevents unbounded memory growth in long-lived deployments "
            "seeing many distinct identities."
        ),
    )

    workflow_storage: WorkflowStorageConfig = Field(
        default_factory=WorkflowStorageConfig,
        description=(
            "File/workspace storage settings (storage doc §3.5). Built from "
            "CALIBER_WORKFLOW_STORAGE_* env vars in load()."
        ),
    )

    @classmethod
    def load(cls, environ: dict[str, str] | None = None) -> CaliberConfig:
        """Construct a config from ``CALIBER_*`` environment variables.

        Parameters
        ----------
        environ:
            Optional mapping to read from instead of ``os.environ``. Useful in
            tests; production callers should pass ``None`` to read from the
            real environment.

        Raises
        ------
        ConfigError
            If any value is invalid (wrong type, out-of-range enum, etc.).
        """
        env = os.environ if environ is None else environ
        # kwargs typed as ``dict[str, Any]`` because the values flow through
        # Pydantic for real validation; the static type can't express "this
        # str will be Literal[...] after Pydantic narrows it."
        kwargs: dict[str, Any] = {}
        for env_var, field_name, coerce in _ENV_VAR_TABLE:
            if env_var in env:
                kwargs[field_name] = coerce(env[env_var])
        # Nested workflow-storage config from its own env table (storage doc
        # §0.1 rule 7: a plain BaseModel built here, not BaseSettings).
        storage_kwargs: dict[str, Any] = {}
        retention_kwargs: dict[str, Any] = {}
        for env_var, field_name, coerce in _WORKFLOW_STORAGE_ENV_TABLE:
            if env_var in env:
                storage_kwargs[field_name] = coerce(env[env_var])
        for env_var, field_name, coerce in _RETENTION_ENV_TABLE:
            if env_var in env:
                retention_kwargs[field_name] = coerce(env[env_var])
        if retention_kwargs:
            storage_kwargs["retention"] = retention_kwargs
        if storage_kwargs:
            kwargs["workflow_storage"] = storage_kwargs
        try:
            return cls(**kwargs)
        except ValidationError as exc:
            raise ConfigError(f"invalid CALIBER configuration: {exc}") from exc


def _flag(raw: str) -> bool:
    """Parse a boolean environment flag.

    Accepts the common truthy spellings (``true`` / ``1`` / ``yes`` / ``on``)
    case-insensitively; everything else (including ``false`` / ``0`` / unset)
    is false. Previously only the exact string ``"true"`` counted, so a
    perfectly reasonable ``=1`` / ``=yes`` silently disabled fail-safe defaults.
    """
    return raw.strip().lower() in {"true", "1", "yes", "on"}


# (env-var, field-name, coercion) for every config field that's set via
# the environment. Kept at module scope so the branch count of
# :meth:`CaliberConfig.load` stays under the lint cap as we add fields.
_ENV_VAR_TABLE: list[tuple[str, str, Any]] = [
    ("CALIBER_LOG_LEVEL", "log_level", str.upper),
    ("CALIBER_LOG_SINK", "log_sink", str.lower),
    ("CALIBER_LOG_BUCKET", "log_bucket", str),
    ("CALIBER_LOG_PREFIX", "log_prefix", str),
    (
        "CALIBER_LOG_S3_AUTO_CREATE_BUCKET",
        "log_s3_auto_create_bucket",
        _flag,
    ),
    ("CALIBER_LOG_S3_FLUSH_LINES", "log_s3_flush_lines", int),
    ("CALIBER_STATIC_PREFIX", "static_prefix", str),
    ("CALIBER_DATABASE_URL", "database_url", normalize_database_url),
    ("CALIBER_LLM_PROVIDER", "llm_provider", str),
    ("CALIBER_LLM_DIAGNOSIS_MODEL", "llm_diagnosis_model", str),
    ("CALIBER_GEPA_REFLECTION_MODEL", "gepa_reflection_model", str),
    ("CALIBER_GEPA_MAX_METRIC_CALLS", "gepa_max_metric_calls", int),
    ("CALIBER_DSPY_MAX_BOOTSTRAPPED_DEMOS", "dspy_max_bootstrapped_demos", int),
    ("CALIBER_DSPY_MAX_LABELED_DEMOS", "dspy_max_labeled_demos", int),
    ("CALIBER_DSPY_MIPRO_AUTO", "dspy_mipro_auto", str),
    (
        "CALIBER_ALLOW_FLAGGED_DSPY_OPTIMIZERS",
        "allow_flagged_dspy_optimizers",
        _flag,
    ),
    ("CALIBER_MEMORY_ENABLED", "memory_enabled", _flag),
    ("CALIBER_MEMORY_LLM_MODEL", "memory_llm_model", str),
    ("CALIBER_MEMORY_EMBEDDER_MODEL", "memory_embedder_model", str),
    ("CALIBER_MEMORY_EMBEDDING_DIMS", "memory_embedding_dims", int),
    ("CALIBER_MEMORY_COLLECTION", "memory_collection", str),
    ("CALIBER_MEMORY_EMBEDDER_BASE_URL", "memory_embedder_base_url", str),
    ("CALIBER_MEMORY_INFER", "memory_infer", _flag),
    ("CALIBER_LLM_API_KEY_ENV", "llm_api_key_env", str),
    ("CALIBER_LLM_BASE_URL", "llm_base_url", str),
    ("CALIBER_GATEWAY_URI", "gateway_uri", str),
    ("CALIBER_OPENAI_WORKFLOW_API", "openai_workflow_api", str),
    (
        "CALIBER_OPENAI_WORKFLOW_PARALLEL_TOOL_CALLS",
        "openai_workflow_parallel_tool_calls",
        str,
    ),
    ("CALIBER_OPENAI_PROMPT_CACHE_MODE", "openai_prompt_cache_mode", str),
    (
        "CALIBER_OPENAI_PROMPT_CACHE_RETENTION",
        "openai_prompt_cache_retention",
        str,
    ),
    (
        "CALIBER_WORKFLOW_LLM_JUDGE_ENABLED",
        "workflow_llm_judge_enabled",
        _flag,
    ),
    ("CALIBER_REFINEMENT_MAX_ITERATIONS", "refinement_max_iterations", int),
    (
        "CALIBER_LLM_CIRCUIT_BREAKER_ENABLED",
        "llm_circuit_breaker_enabled",
        _flag,
    ),
    ("CALIBER_LLM_CIRCUIT_FAILURE_THRESHOLD", "llm_circuit_failure_threshold", int),
    ("CALIBER_LLM_CIRCUIT_WINDOW_SECONDS", "llm_circuit_window_seconds", float),
    (
        "CALIBER_LLM_CIRCUIT_OPEN_DURATION_SECONDS",
        "llm_circuit_open_duration_seconds",
        float,
    ),
    ("CALIBER_ARTIFACT_STORE_PROVIDER", "artifact_store_provider", str),
    ("CALIBER_EVAL_PROVIDER", "eval_provider", str),
    ("CALIBER_PROMOTER_PROVIDER", "promoter_provider", str),
    ("CALIBER_JANITOR_INTERVAL_SECONDS", "janitor_interval_seconds", float),
    ("CALIBER_JANITOR_STALE_THRESHOLD_SECONDS", "janitor_stale_threshold_seconds", float),
    ("CALIBER_WORKFLOW_RUN_RETENTION_DAYS", "workflow_run_retention_days", float),
    (
        "CALIBER_WORKFLOW_RUN_QUEUE_ENABLED",
        "workflow_run_queue_enabled",
        _flag,
    ),
    (
        "CALIBER_WORKFLOW_RUN_RUNTIME_APPROVALS_ENABLED",
        "workflow_run_runtime_approvals_enabled",
        _flag,
    ),
    (
        "CALIBER_WORKFLOW_RUN_CHECKPOINTING_ENABLED",
        "workflow_run_checkpointing_enabled",
        _flag,
    ),
    ("CALIBER_WORKFLOW_RUN_EVENT_BACKEND", "workflow_run_event_backend", str),
    ("CALIBER_NATS_URL", "nats_url", str),
    ("CALIBER_NATS_SUBJECT", "nats_subject", str),
    ("CALIBER_REDIS_URL", "redis_url", str),
    ("CALIBER_REDIS_CHANNEL", "redis_channel", str),
    (
        "CALIBER_WORKFLOW_RUN_WORKER_ENABLED",
        "workflow_run_worker_enabled",
        _flag,
    ),
    (
        "CALIBER_WORKFLOW_RUN_WORKER_INTERVAL_SECONDS",
        "workflow_run_worker_interval_seconds",
        float,
    ),
    (
        "CALIBER_ARIA_PLAN_WORKER_INTERVAL_SECONDS",
        "aria_plan_worker_interval_seconds",
        float,
    ),
    (
        "CALIBER_WORKFLOW_SCHEDULER_ENABLED",
        "workflow_scheduler_enabled",
        _flag,
    ),
    (
        "CALIBER_WORKFLOW_SCHEDULER_INTERVAL_SECONDS",
        "workflow_scheduler_interval_seconds",
        float,
    ),
    ("CALIBER_WORKFLOW_RUN_LEASE_SECONDS", "workflow_run_lease_seconds", float),
    ("CALIBER_WORKFLOW_QUEUE_MAX_AGE_SECONDS", "workflow_queue_max_age_seconds", float),
    ("CALIBER_SLO_OBJECTIVES", "slo_objectives", str),
    ("CALIBER_SLO_SEVERITIES", "slo_severities", str),
    ("CALIBER_SLO_WINDOW_MINUTES", "slo_window_minutes", float),
    (
        "CALIBER_KNOWLEDGE_BUILD_QUEUE_ENABLED",
        "knowledge_build_queue_enabled",
        _flag,
    ),
    (
        "CALIBER_KNOWLEDGE_BUILD_WORKER_ENABLED",
        "knowledge_build_worker_enabled",
        _flag,
    ),
    (
        "CALIBER_KNOWLEDGE_BUILD_WORKER_INTERVAL_SECONDS",
        "knowledge_build_worker_interval_seconds",
        float,
    ),
    ("CALIBER_KNOWLEDGE_BUILD_LEASE_SECONDS", "knowledge_build_lease_seconds", float),
    (
        "CALIBER_KNOWLEDGE_GRAPH_EXTRACTOR_BACKEND",
        "knowledge_graph_extractor_backend",
        str,
    ),
    (
        "CALIBER_KNOWLEDGE_GRAPH_SPACY_MODEL",
        "knowledge_graph_spacy_model",
        str,
    ),
    (
        "CALIBER_KNOWLEDGE_AGE_ENABLED",
        "knowledge_age_enabled",
        _flag,
    ),
    (
        "CALIBER_KNOWLEDGE_AGE_GRAPH_NAME",
        "knowledge_age_graph_name",
        str,
    ),
    (
        "CALIBER_KNOWLEDGE_AGE_VIEWER_URL",
        "knowledge_age_viewer_url",
        str,
    ),
    (
        "CALIBER_ALLOW_FLAGGED_LOCAL_EMBEDDINGS",
        "allow_flagged_local_embeddings",
        _flag,
    ),
    (
        "CALIBER_KNOWLEDGE_PGVECTOR_ENABLED",
        "knowledge_pgvector_enabled",
        _flag,
    ),
    (
        "CALIBER_KNOWLEDGE_ANN_CANDIDATE_POOL_SIZE",
        "knowledge_ann_candidate_pool_size",
        int,
    ),
    (
        "CALIBER_KNOWLEDGE_EMBEDDING_DIMENSION",
        "knowledge_embedding_dimension",
        int,
    ),
    (
        "CALIBER_KNOWLEDGE_RERANK_ENABLED",
        "knowledge_rerank_enabled",
        _flag,
    ),
    (
        "CALIBER_KNOWLEDGE_RERANK_MODEL",
        "knowledge_rerank_model",
        str,
    ),
    ("CALIBER_WORKFLOW_RUN_MAX_ATTEMPTS", "workflow_run_max_attempts", int),
    ("CALIBER_SHUTDOWN_GRACE_SECONDS", "shutdown_grace_seconds", float),
    ("CALIBER_BACKGROUND_TASKS_ENABLED", "background_tasks_enabled", _flag),
    ("CALIBER_DEV_USER", "dev_user", str),
    ("CALIBER_AUTH_MODE", "auth_mode", str),
    ("CALIBER_SECRET_ENCRYPTION_KEY_SOURCE", "secret_encryption_key_source", str),
    (
        "CALIBER_SECRET_ENCRYPTION_ADDITIONAL_KEYS",
        "secret_encryption_additional_keys",
        str,
    ),
    ("CALIBER_AUTH_DEV_FALLBACK_ENABLED", "auth_dev_fallback_enabled", _flag),
    ("CALIBER_AUTH_TRUSTED_PROXY_SECRET_ENV", "auth_trusted_proxy_secret_env", str),
    ("CALIBER_AUTH_SESSION_TTL_SECONDS", "auth_session_ttl_seconds", float),
    ("CALIBER_AUTH_SESSION_COOKIE_NAME", "auth_session_cookie_name", str),
    ("CALIBER_AUTH_SESSION_COOKIE_SECURE", "auth_session_cookie_secure", _flag),
    ("CALIBER_AUTH_BOOTSTRAP_ADMIN_USER", "auth_bootstrap_admin_user", str),
    (
        "CALIBER_AUTH_BOOTSTRAP_ADMIN_PASSWORD_ENV",
        "auth_bootstrap_admin_password_env",
        str,
    ),
    (
        "CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT",
        "auth_bootstrap_allow_insecure_default",
        _flag,
    ),
    ("CALIBER_ASSISTANT_ENABLED", "assistant_enabled", _flag),
    (
        "CALIBER_ASSISTANT_SKILL_RUNTIME_ENABLED",
        "assistant_skill_runtime_enabled",
        _flag,
    ),
    (
        "CALIBER_BUILTIN_SKILLS_AUTO_SEED",
        "builtin_skills_auto_seed",
        _flag,
    ),
    ("CALIBER_ASSISTANT_ENGINE", "assistant_engine", str),
    ("CALIBER_ASSISTANT_MODEL", "assistant_model", str),
    ("CALIBER_ASSISTANT_REASONING", "assistant_reasoning", str),
    ("CALIBER_ASSISTANT_DISABLED_INTENTS", "assistant_disabled_intents", str),
    ("CALIBER_ASSISTANT_DISABLED_DOMAINS", "assistant_disabled_domains", str),
    ("CALIBER_ASSISTANT_MAX_TURNS", "assistant_max_turns", int),
    ("CALIBER_ASSISTANT_MAX_QUESTIONS_PER_TURN", "assistant_max_questions_per_turn", int),
    ("CALIBER_ASSISTANT_MAX_DRAFTS_PER_SESSION", "assistant_max_drafts_per_session", int),
    (
        "CALIBER_ASSISTANT_PUBLISH_REQUIRES_APPROVAL",
        "assistant_publish_requires_approval",
        _flag,
    ),
    ("CALIBER_ASSISTANT_REVIEWER_USER", "assistant_reviewer_user", str),
    ("CALIBER_ASSISTANT_RELEASE_USER", "assistant_release_user", str),
    (
        "CALIBER_ASSISTANT_REVIEWER_POLICY_VERSION",
        "assistant_reviewer_policy_version",
        str,
    ),
    (
        "CALIBER_ASSISTANT_REVIEWER_MIN_CONFIDENCE",
        "assistant_reviewer_min_confidence",
        float,
    ),
    ("CALIBER_ASSISTANT_REVIEW_TTL_SECONDS", "assistant_review_ttl_seconds", int),
    ("CALIBER_ASSISTANT_TOOL_SOURCE_MAX_BYTES", "assistant_tool_source_max_bytes", int),
    ("CALIBER_ASSISTANT_RUN_TIMEOUT_SECONDS", "assistant_run_timeout_seconds", float),
    ("CALIBER_WORKFLOW_FILE_ROOT", "workflow_file_root", str),
    ("CALIBER_METRICS_TOKEN_ENV", "metrics_token_env", str),
    (
        "CALIBER_TOOL_SANDBOX_REQUIRE_EXTERNAL_ISOLATION_FOR_ENVIRONMENT_CLASSES",
        "tool_sandbox_require_external_isolation_for_environment_classes",
        str,
    ),
    (
        "CALIBER_WORKFLOW_HOST_PATH_NODES_ALLOWED_ENVIRONMENT_CLASSES",
        "workflow_host_path_nodes_allowed_environment_classes",
        str,
    ),
    (
        "CALIBER_PROVIDER_REQUEST_TIMEOUT_SECONDS",
        "provider_request_timeout_seconds",
        float,
    ),
    ("CALIBER_REGISTERED_TOOL_SANDBOX_ENABLED", "registered_tool_sandbox_enabled", _flag),
    ("CALIBER_TOOL_SANDBOX_TIMEOUT_SECONDS", "tool_sandbox_timeout_seconds", float),
    (
        "CALIBER_REGISTERED_TOOL_SANDBOX_TIMEOUT_SECONDS",
        "registered_tool_sandbox_timeout_seconds",
        float,
    ),
    ("CALIBER_TOOL_SANDBOX_BACKEND", "tool_sandbox_backend", str),
    ("CALIBER_TOOL_SANDBOX_MAX_OUTPUT_BYTES", "tool_sandbox_max_output_bytes", int),
    ("CALIBER_TOOL_SANDBOX_MAX_MEMORY_BYTES", "tool_sandbox_max_memory_bytes", int),
    ("CALIBER_TOOL_SANDBOX_MAX_FILE_BYTES", "tool_sandbox_max_file_bytes", int),
    ("CALIBER_TOOL_SANDBOX_MAX_OPEN_FILES", "tool_sandbox_max_open_files", int),
    ("CALIBER_MCP_STDIO_COMMAND_ALLOWLIST", "mcp_stdio_command_allowlist", str),
    ("CALIBER_MCP_STDIO_SAFE_PATH", "mcp_stdio_safe_path", str),
    (
        "CALIBER_MCP_STDIO_PYTHON_MODULE_ALLOWLIST",
        "mcp_stdio_python_module_allowlist",
        str,
    ),
    (
        "CALIBER_MCP_STDIO_PYTHON_SCRIPT_ALLOWLIST",
        "mcp_stdio_python_script_allowlist",
        str,
    ),
    ("CALIBER_MCP_REMOTE_HOST_ALLOWLIST", "mcp_remote_host_allowlist", str),
    (
        "CALIBER_EXTERNAL_APP_ENTRYPOINT_ALLOWLIST",
        "external_app_entrypoint_allowlist",
        str,
    ),
    (
        "CALIBER_REGISTERED_TOOL_MODULE_ALLOWLIST",
        "registered_tool_module_allowlist",
        str,
    ),
    ("CALIBER_APPROVAL_ALLOW_SELF_APPROVAL", "approval_allow_self_approval", _flag),
    ("CALIBER_EGRESS_POLICY_ENABLED", "egress_policy_enabled", _flag),
    ("CALIBER_EGRESS_BLOCKED_CATEGORIES", "egress_blocked_categories", str),
    ("CALIBER_EGRESS_ALLOWED_HOSTS", "egress_allowed_hosts", str),
    ("CALIBER_EGRESS_ALLOW_UNRESOLVABLE_HOSTS", "egress_allow_unresolvable_hosts", _flag),
    ("CALIBER_MCP_MANAGED_SIDECAR_HOSTS", "mcp_managed_sidecar_hosts", str),
    ("CALIBER_MCP_ALLOW_INSECURE_HTTP", "mcp_allow_insecure_http", _flag),
    ("CALIBER_MCP_STDIO_ISOLATED_WORKDIR", "mcp_stdio_isolated_workdir", _flag),
    ("CALIBER_MCP_STDIO_ISOLATION_PREFIX", "mcp_stdio_isolation_prefix", str),
    ("CALIBER_MCP_STDIO_ISOLATION_PROFILE", "mcp_stdio_isolation_profile", str),
    (
        "CALIBER_MCP_REQUIRE_EXTERNAL_ISOLATION_FOR_ALIASES",
        "mcp_require_external_isolation_for_aliases",
        str,
    ),
    (
        "CALIBER_MCP_REQUIRE_EXTERNAL_ISOLATION_FOR_ENVIRONMENT_CLASSES",
        "mcp_require_external_isolation_for_environment_classes",
        str,
    ),
    ("CALIBER_DEPLOYMENT_ENVIRONMENT_CLASSES", "deployment_environment_classes", str),
    (
        "CALIBER_RELEASE_REQUIRE_QUALITY_GATE_FOR_ENVIRONMENT_CLASSES",
        "release_require_quality_gate_for_environment_classes",
        str,
    ),
    (
        "CALIBER_RELEASE_REQUIRE_GRADED_EXECUTOR_FOR_ENVIRONMENT_CLASSES",
        "release_require_graded_executor_for_environment_classes",
        str,
    ),
    (
        "CALIBER_RELEASE_REQUIRE_HUMAN_APPROVAL_FOR_ENVIRONMENT_CLASSES",
        "release_require_human_approval_for_environment_classes",
        str,
    ),
    (
        "CALIBER_DEPLOYMENT_DEFAULT_ENVIRONMENT_CLASS",
        "deployment_default_environment_class",
        str,
    ),
    ("CALIBER_WORKFLOW_FOREACH_MAX_WORKERS", "workflow_foreach_max_workers", int),
    (
        "CALIBER_WORKFLOW_PARALLEL_BRANCH_MAX_WORKERS",
        "workflow_parallel_branch_max_workers",
        int,
    ),
    ("CALIBER_WORKFLOW_RUN_ARTIFACT_BUCKET", "workflow_run_artifact_bucket", str),
    ("CALIBER_WORKFLOW_RUN_ARTIFACT_PREFIX", "workflow_run_artifact_prefix", str),
    ("CALIBER_WORKFLOW_RUN_LOG_PREFIX", "workflow_run_log_prefix", str),
    ("CALIBER_OBJECT_STORE_ENDPOINT_URL", "object_store_endpoint_url", str),
    ("CALIBER_OBJECT_STORE_REGION", "object_store_region", str),
    ("CALIBER_OBJECT_STORE_ACCESS_KEY_SOURCE", "object_store_access_key_source", str),
    ("CALIBER_OBJECT_STORE_SECRET_KEY_SOURCE", "object_store_secret_key_source", str),
    (
        "CALIBER_OBJECT_STORE_FORCE_PATH_STYLE",
        "object_store_force_path_style",
        _flag,
    ),
    ("CALIBER_ADMIN_USERS", "admin_users", str),
    ("CALIBER_APPROVER_USERS", "approver_users", str),
    ("CALIBER_OPERATOR_USERS", "operator_users", str),
    ("CALIBER_WEBHOOK_URLS", "webhook_urls", str),
    ("CALIBER_WEBHOOK_SIGNING_SECRET_ENV", "webhook_signing_secret_env", str),
    ("CALIBER_WEBHOOK_EVENT_FILTER", "webhook_event_filter", str),
    (
        "CALIBER_FEEDBACK_HARVEST_ENABLED",
        "feedback_harvest_enabled",
        _flag,
    ),
    ("CALIBER_TRACING_ENABLED", "tracing_enabled", _flag),
    ("CALIBER_TRACING_AUTOLOG_ENABLED", "tracing_autolog_enabled", _flag),
    ("CALIBER_TRACING_MAX_ATTRIBUTE_BYTES", "tracing_max_attribute_bytes", int),
    ("CALIBER_TRACING_EXPERIMENT", "tracing_experiment", str),
    ("CALIBER_ALLURE_REPORT_DIR", "allure_report_dir", str),
    ("CALIBER_PII_REDACTION_ENABLED", "pii_redaction_enabled", _flag),
    ("CALIBER_PII_REDACTION_REPLACEMENT", "pii_redaction_replacement", str),
    ("CALIBER_PII_REDACTION_EXTRA_PATTERNS", "pii_redaction_extra_patterns", str),
    ("CALIBER_CSRF_ENABLED", "csrf_enabled", _flag),
    ("CALIBER_CSRF_SIGNING_SECRET_ENV", "csrf_signing_secret_env", str),
    ("CALIBER_CSRF_TOKEN_TTL_SECONDS", "csrf_token_ttl_seconds", int),
    (
        "CALIBER_SERVICE_INVOKE_MAX_BODY_BYTES",
        "service_invoke_max_body_bytes",
        int,
    ),
    ("CALIBER_RATE_LIMIT_ENABLED", "rate_limit_enabled", _flag),
    ("CALIBER_RATE_LIMIT_REQUESTS_PER_MINUTE", "rate_limit_requests_per_minute", float),
    ("CALIBER_RATE_LIMIT_BURST", "rate_limit_burst", int),
    ("CALIBER_RATE_LIMIT_MAX_BUCKETS", "rate_limit_max_buckets", int),
]


def _csv(raw: str) -> list[str]:
    """Split a comma-separated env value into a trimmed, non-empty list."""
    return [item.strip() for item in raw.split(",") if item.strip()]


# (env-var, WorkflowStorageConfig field, coercion). Built in load() into the
# nested workflow_storage model (storage doc §3.5).
_WORKFLOW_STORAGE_ENV_TABLE: list[tuple[str, str, Any]] = [
    ("CALIBER_WORKFLOW_STORAGE_BACKEND", "backend", str),
    ("CALIBER_WORKFLOW_STORAGE_BASE_URI", "base_uri", str),
    ("CALIBER_WORKFLOW_STORAGE_BUCKET", "bucket", str),
    ("CALIBER_WORKFLOW_STORAGE_PREFIX", "prefix", str),
    ("CALIBER_WORKFLOW_STORAGE_INTERNAL_ENDPOINT_URL", "internal_endpoint_url", str),
    ("CALIBER_WORKFLOW_STORAGE_PUBLIC_ENDPOINT_URL", "public_endpoint_url", str),
    ("CALIBER_WORKFLOW_STORAGE_REGION", "region", str),
    ("CALIBER_WORKFLOW_STORAGE_FORCE_PATH_STYLE", "force_path_style", _flag),
    ("CALIBER_WORKFLOW_STORAGE_ACCESS_KEY_SOURCE", "access_key_source", str),
    ("CALIBER_WORKFLOW_STORAGE_SECRET_KEY_SOURCE", "secret_key_source", str),
    ("CALIBER_WORKFLOW_STORAGE_MAX_UPLOAD_BYTES", "max_upload_bytes", int),
    ("CALIBER_WORKFLOW_STORAGE_MAX_RUN_BYTES", "max_run_bytes", int),
    ("CALIBER_WORKFLOW_STORAGE_MAX_FILES_PER_RUN", "max_files_per_run", int),
    ("CALIBER_WORKFLOW_STORAGE_SIGNED_URL_TTL_SECONDS", "signed_url_ttl_seconds", int),
    (
        "CALIBER_WORKFLOW_STORAGE_SIGNED_URL_MAX_TTL_SECONDS",
        "signed_url_max_ttl_seconds",
        int,
    ),
    ("CALIBER_WORKFLOW_STORAGE_ALLOWED_MEDIA_TYPES", "allowed_media_types", _csv),
    ("CALIBER_WORKFLOW_STORAGE_DENIED_EXTENSIONS", "denied_extensions", _csv),
    ("CALIBER_WORKFLOW_STORAGE_SNIFF_CONTENT_TYPE", "sniff_content_type", _flag),
    ("CALIBER_WORKFLOW_STORAGE_SCAN_UPLOADS", "scan_uploads", _flag),
]

_RETENTION_ENV_TABLE: list[tuple[str, str, Any]] = [
    ("CALIBER_WORKFLOW_STORAGE_RETENTION_DEFAULT_DAYS", "default_run_days", int),
    ("CALIBER_WORKFLOW_STORAGE_RETENTION_FAILED_DAYS", "failed_run_days", int),
    ("CALIBER_WORKFLOW_STORAGE_RETENTION_PREVIEW_DAYS", "preview_run_days", int),
    ("CALIBER_WORKFLOW_STORAGE_RETENTION_EVAL_DAYS", "eval_run_days", int),
    (
        "CALIBER_WORKFLOW_STORAGE_RETENTION_KEEP_PROMOTED",
        "keep_promoted_artifacts",
        _flag,
    ),
]


class ConfigError(Exception):
    """Raised when ``CALIBER_*`` environment variables fail validation."""


def provider_request_timeout(environ: Mapping[str, str] | None = None) -> float:
    """Resolve the per-request LLM provider timeout for a leaf client construction.

    The workflow executors and the Aria engines build their OpenAI/Anthropic
    clients deep inside a call path that never receives a ``CaliberConfig``, and
    threading one through purely to carry a float would touch five constructors
    and their callers. This reads the same environment variable the config field
    maps, and falls back to the same default, so an operator sets one variable and
    both paths agree.

    Never raises: a malformed or non-positive value falls back to the default
    rather than failing a run, because the point of this value is to *bound* a
    call, and refusing to start over a typo in a safety margin would be worse
    than applying the shipped bound.
    """
    source = os.environ if environ is None else environ
    raw = str(source.get("CALIBER_PROVIDER_REQUEST_TIMEOUT_SECONDS", "")).strip()
    if not raw:
        return _PROVIDER_REQUEST_TIMEOUT_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return _PROVIDER_REQUEST_TIMEOUT_DEFAULT
    return value if value > 0 else _PROVIDER_REQUEST_TIMEOUT_DEFAULT


#: Explicit opt-out restoring the pre-confinement behaviour: unmanaged
#: host-filesystem nodes may resolve any absolute path the server can reach.
UNCONFINED_WORKFLOW_FILE_ROOT = "unconfined"


def _raw_workflow_file_root(environ: Mapping[str, str] | None = None) -> str:
    """The verbatim ``CALIBER_WORKFLOW_FILE_ROOT`` value, whitespace-stripped.

    Read from the environment for the same reason as
    :func:`provider_request_timeout`: the node executors resolve paths deep in a
    call path that carries no ``CaliberConfig``, and both read the variable the
    config field maps.
    """
    source = os.environ if environ is None else environ
    return str(source.get("CALIBER_WORKFLOW_FILE_ROOT", "")).strip()


def workflow_host_paths_allowed(environ: Mapping[str, str] | None = None) -> bool:
    """Whether unmanaged host-filesystem nodes may run at all.

    **Unset means no**, which is a deliberate reversal of the original default.
    The three states of one variable are: unset refuses these nodes outright, a
    path confines them to it, and the literal ``unconfined`` restores the old
    behaviour for an operator who genuinely wants it.

    Refusing rather than allowing-anywhere is the same trade F-08 made for CSRF:
    a control whose safe state is off is not a control. It also makes execution
    agree with Preview, which already refuses exactly these node types — the
    argument F-01a used at the promotion chokepoint, applied one layer down to
    the data plane so the promotion gate stops being the only defence.
    """
    return _raw_workflow_file_root(environ) != ""


def workflow_file_root(environ: Mapping[str, str] | None = None) -> Path | None:
    """Resolved root that unmanaged host-filesystem nodes may not escape.

    ``None`` means *no root applies* — either because the nodes are refused
    outright (unset, the default) or because the operator opted out explicitly
    with ``unconfined``. Callers must consult
    :func:`workflow_host_paths_allowed` to tell those two apart; they are
    separate functions precisely so a caller cannot collapse "refused" into
    "unrestricted" by testing one value for falsiness.

    The root is resolved once here so callers compare two resolved paths — a
    root given as a symlink would otherwise never match its own contents.
    """
    raw = _raw_workflow_file_root(environ)
    if not raw or raw == UNCONFINED_WORKFLOW_FILE_ROOT:
        return None
    return Path(raw).expanduser().resolve()
