# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- **Aria plans: cross-user IDOR on every detail/action route.** `GET`,
  `PATCH`, `approve`, `execute`, `poll`, and the interactions list resolved a
  plan by bare id with no owner check (only `list` was scoped), so any
  authenticated user could read/edit/approve/execute another user's plan by a
  guessed id. `PlanService.get_plan` now scopes through `db.scoping.get_visible`
  and the routes pass the caller's identity (out-of-scope → 404).
- **Aria interactions: any user could answer/deny another's plan.** Answering a
  non-gated permission ask had no authorization at all. A new `_authorize_answer`
  restricts plain asks/denials to the plan owner (or an admin) and routes scoped
  gated approvals through separation-of-duties; an unmapped `required_scope` now
  denies instead of silently authorizing anyone.
- **Aria capability scopes are enforced at execution.** A capability's declared
  `required_scopes` were never checked when a plan step ran, so an
  under-privileged owner's auto-running plan could invoke an operator-only
  capability. The executor now enforces them against the plan owner (the
  `@system` async-poller and a separate gate-approver aren't required to hold
  the scope themselves).
- **Aria plan autonomy edits are audited.** Changing a plan's autonomy dial
  (e.g. relaxing it to `auto_guarded`) is now routed through an audited
  `PlanService.set_autonomy` (race-safe 404, not a 500) instead of a bare,
  unaudited in-place column write.
- **Cross-project prompt listing leak.** `GET /prompts` did not apply the
  visibility filter, so it could surface prompt configs owned by other
  projects/users. It now scopes results to the caller's visibility tier
  like every other list endpoint.
- **Detail-GET object scoping (IDOR).** `GET /judges/{id}`, `/agents/{id}`,
  `/eval-datasets/{id}`, and `/review-queues/{id}` used bare primary-key
  lookups that ignored visibility. They now resolve through a shared
  `db.scoping.get_visible()` helper and return 404 for out-of-scope rows
  (so existence isn't leaked); admins still see everything.
- **Workflow service invocations are now audited.** Invoking a published
  workflow service writes an `invoke_workflow_service` audit row, so
  token-authenticated calls are traceable.

### Added

- **LLM Gateway: guardrails, per-model cost config, and usage graphs.** The
  Gateway page is now tabbed — **Endpoints** (discovery, as before),
  **Guardrails**, **Pricing**, and **Usage** — to surface the full MLflow AI
  Gateway governance surface:
  - **Guardrails** — lists the scorer-based gateway guardrails (stage / action /
    scorer) + per-endpoint coverage read from the MLflow tracking server, and
    lets operators attach / detach existing guardrails on an endpoint
    (operator-scoped + audited). Degrades gracefully when the gateway-guardrail
    API is unavailable; scorer creation stays in the MLflow image.
  - **Pricing** — a new editable `caliber_llm_model_pricing` resource
    (`/llm-pricing` CRUD, migration 0061): per-provider/model USD-per-1K-token
    rates that override the built-in `DEFAULT_MODEL_PRICING`. Cost attribution
    (trace spans, refinement jobs, the usage graphs) resolves these via a cached
    `mlflow_tracing.resolve_model_pricing` override, invalidated on edit.
  - **Usage** — trace-derived token / cost / latency / error metrics over time
    plus a by-model rollup (the gateway API doesn't expose usage stats in this
    MLflow version; CALIBER's own traces do — reuses the observability
    aggregation, now recording a `caliber.model` span attribute).
- **Aria UX polish.** The assistant slide-over closes on `Escape` and carries an
  `aria-label`; the Plans page shows friendly autonomy labels (not raw enums) and
  a copyable plan id; plan/step status badges expose a `title`; draft action
  buttons (Validate/Test/Approve/Publish) disable while a mutation is in flight
  (no double-submit); plan-mode plans scroll into view; the attachment pickers
  surface a load error instead of a misleading "none found"; and suggested-prompt
  cards no longer send a decorative emoji as the message content.
- **One-click copy + keyboard-accessible list rows.** A shared `CopyButton`
  component sits beside resource IDs / model identifiers (Tools, Skills,
  Gateway, Audit Log) for one-click clipboard copy, and `ListRow` is now
  reachable and activatable by keyboard (`role="button"`, `Tab`,
  `Enter`/`Space`). The judge-model field offers a `<datalist>` of models
  already in use.

- **GEPA (Guided Evolution for Prompt Adaptation) optimizer integration.**
  When the diagnosis suggests competing objectives (multi-dimensional
  trade-offs, low confidence with many alternatives, or a rollback
  history), CALIBER now automatically selects the GEPA optimizer instead
  of MetaPrompt. GEPA uses Pareto-front evolution to explore the
  trade-off surface — producing candidates that balance competing
  quality dimensions rather than over-optimizing one at the expense
  of another.
  - New `gepa>=0.1` optional dependency in the `[llm]` extra.
  - New config fields: `gepa_reflection_model` (default
    `"gpt-4o-mini"`), `gepa_max_metric_calls` (default 50).
  - `CandidateContext` extended with `pareto_dims`, `population_size`,
    and `generations` fields for GEPA parameterization.
  - `optimizer_select.py` gains `_diagnosis_suggests_gepa()` heuristic
    that scores diagnosis shape against five signals (multi-dimensional
    thresholds, low confidence, many alternatives, competing-objectives
    keywords, rollback history).
  - `OpenAIAgentsLLMProvider` dispatches to GEPA when selected, with
    graceful fallback to MetaPrompt if the `gepa` package is not
    installed.
  - `FakeLLMProvider` returns GEPA-shaped stub responses with evolution
    metadata (`generation`, `pareto_rank`, `crowding_distance`).
  - 38 new tests in `tests/test_gepa_integration.py` covering
    selection heuristics, provider dispatch, fallback behavior,
    config propagation, and the fake provider's GEPA mode.

- **Skill optimization workflow.** The refinement pipeline now handles
  `artifact_type="skill"` natively. When a feedback item targets a
  skill, CALIBER runs the same six-stage pipeline (triage → evidence →
  diagnosis → candidate → eval → approval) with skill-aware prompting
  in the diagnosis and candidate stages. Approved skill changes are
  promoted via `SkillPromoter` (DB-side version bump) rather than
  MLflow Prompt Registry rotation.
  - New `SkillPromoter` class in `caliber.promoter` — promotes skills
    by updating the `CaliberSkill` row's `content` and bumping
    `version` in a single transaction.
  - New `CompositePromoter` wrapper that routes `"skill"` artifact
    types to `SkillPromoter` and everything else to the configured
    default promoter (MLflow or Fake).
  - `build_promoter()` always wraps the base promoter in a
    `CompositePromoter` so skill promotion works regardless of
    backend provider.

### Changed

- **Aria plans: pagination + N+1 removal + bounded async polling.** `GET
  /aria/plans` accepts `?limit`/`?offset` and computes per-plan step counts in a
  single grouped query (was a COUNT-per-plan N+1); the interactions list is
  paginated and scoped; and the background plan poller is bounded to a batch per
  tick so a backlog of parked plans can't run one tick unboundedly long.
- **`/health` now probes the database.** The readiness endpoint executes a
  trivial `SELECT 1` and returns `{"status", "db", "version"}` with HTTP 503
  when the database is unreachable (was always 200), giving load balancers
  and deploy gates a truthful signal.
- **List endpoints are bounded + paginatable.** `GET` list routes
  (eval-datasets, examples, jobs, agents, judges, skills, MCP servers) accept
  `?limit`/`?offset` (default 500, cap 2000) instead of returning the entire
  table unbounded.
- **Tool sandbox test-run requires `operator`, not `admin`.** A non-persisting
  sandbox preview no longer demands a higher privilege than persisting the
  tool itself (create/update are operator-gated).
- **Removed per-row N+1 queries** in the project list (file counts), the
  review-queue list (item/pending counts), and the tool "where-used" lookup —
  each now issues a single grouped/filtered query.
- **MLflow minimum bumped to 3.14.0.** The core dependency is now
  `mlflow>=3.14.0,<4` (was `>=3.13`), and the standalone deploy images
  (`deploy/mlflow/Dockerfile`, `deploy/mlflow-gateway/Dockerfile`) install
  the matching `mlflow>=3.14.0,<4` / `mlflow[gateway]>=3.14.0,<4`.

### Fixed

- **Docs & cookbooks: corrected stale claims after a code audit.** Architecture
  docs were realigned with the shipped code: `10-gateways` now lists the
  guardrail-governance / usage / pricing routes and their scopes (not a single
  read-only `GET /gateway`) and splits `/health` (DB probe + 503) from
  `/readiness` (provider honesty); `13-qa-plan` documents the dedicated
  `/review-queues` and `/judges` route modules instead of claiming none exists;
  `14-evaluation` notes that authored custom judges (as `Judge.<id>` tokens) are
  selectable on the Evaluations scorecard, not only deterministic heuristics;
  `12-assistant` disambiguates operator-scoped intent-plan execution from the
  owner-scoped `/aria/plans/*` routes; `15-calibration` corrects "five
  dimensions" to four. Cookbooks `06`/`12`/`16` dropped the stale "judges aren't
  selectable in Evaluations" claim (the UI has a Custom LLM judges section), and
  `03` now states the workflow-run runtime-approval flags are deployment env
  vars (shown read-only in Settings), not UI toggles.
- **Assistant (Aria) sent the wrong reasoning parameter to OpenAI.** The OpenAI
  engine passed `reasoning={"effort": ...}` (a Responses-API object) to Chat
  Completions, which 400s; it now sends the top-level string `reasoning_effort`
  (validated against the known efforts), so reasoning-model assistant turns work.
- **Aria silently presented engine errors as success.** When the engine returned
  a result carrying `error` (rather than raising), the turn was persisted as
  `completed`. The run is now marked `failed` with the error and the message
  metadata flags it.
- **OpenAI engine misread plain JSON content as a draft envelope.** A model reply
  that happened to be a JSON object was parsed as a structured turn; it now
  requires a `reply` key (matching the Anthropic/Ollama engines).
- **Aria prompt could balloon from attachments.** Per-attachment text is capped,
  but a session's attachments are now also bounded by a combined budget in the
  system prompt (with an "N omitted" note), and the drafts summary is compact
  valid JSON rather than a serialized blob sliced mid-token.
- **Custom LLM judges crashed on reasoning models.** `workflows/judge.py`
  hardcoded `temperature=0.0` (and `max_tokens=8`) with no guard, so judges
  backed by `gpt-5*`/o-series models returned an HTTP 400 from the provider.
  A shared `caliber/llm/models.py::is_reasoning_model()` helper now gates the
  temperature/token params across all four call sites that had drifted apart
  (workflow judge, eval predict, workflow runtime, assistant engine).

- **`str(None)` → `"None"` silent data corruption.** The pattern
  `str(dict.get("key", ""))` produces the literal string `"None"`
  when the key is present but maps to `null` — because `dict.get`
  only substitutes the fallback for *missing* keys, and
  `str(None)` is `"None"`. This passed truthiness checks and
  silently corrupted data in the following locations:
  - `eval_stage.py` — candidate content fed to the eval provider
    could be the literal string `"None"`, passing the emptiness
    check and producing garbage eval results.
  - `regression.py` (2 locations) — candidate hash would hash the
    string `"None"`, causing false regression-gate mismatches.
  - `impact.py` — diff calculation against `"None"` produced
    meaningless impact previews.
  - `approvals.py` (7 locations) — promoted artifact content,
    rationale, and artifact_type could all be the string `"None"`.
  - `candidate.py` — diagnosis `root_cause` from a corrupted DB row.
  - `optimizer_select.py` — GEPA heuristic keyword search against
    `"None"`.
  - `mlflow_client.py` — assessment category defaulting to `"None"`
    instead of `"feedback"`.
  All locations now use `isinstance(value, str)` type checks instead
  of `str()` coercion, consistent with the `_coerce_str()` helper
  already present in `bundle.py`.

- **`CompositePromoter` attribute delegation.** The `CompositePromoter`
  wrapper now delegates `__getattr__` and `__setattr__` to the
  underlying default promoter, so test code that accesses
  `FakePromoter`-specific attributes (`calls`, `fail_with`) through
  the composite works correctly.

- **Test: `test_e2e_pipeline` expected wrong status code.** The verify
  endpoint returns 201 (resource created), not 200. Test assertion
  corrected.

- **Test: skill creation tests used underscores in names.** The
  `SkillCreateRequest` schema requires kebab-case names
  (`^[a-z0-9]+(?:-[a-z0-9]+)*$`). Test data changed from `tool_use`
  to `tool-use`, etc.

- **Test: dashboard summary race condition.** The background worker
  could process a queued job between the test's `commit()` and the
  dashboard `GET`, causing `jobs_queued` to drop by 1. Test now
  asserts `>= 1` for mutable job states.

- Initial project scaffolding under Apache 2.0.
- `caliber` Python package with `__version__`.
- `pyproject.toml` with ruff, mypy, and pytest configuration.
- GitHub Actions CI (lint, type-check, test matrix, security scan).
- Pre-commit configuration (ruff + mypy + gitleaks).
- OSS hygiene files: LICENSE, README, CONTRIBUTING, CODE_OF_CONDUCT, SECURITY.
- `mlflow.app` entry point registered (`caliber = "caliber.server:create_app"`).
- SQLAlchemy 2.0 ORM layer (`db.base`, `db.models`, `db.session`) with the
  first two CALIBER tables (`caliber_agent_config`, `caliber_verification_queue`).
- Alembic migration tooling and the `0001_initial` migration that creates
  both tables. Migration drift is verified by `tests/test_migrations.py`.
- Pydantic response schemas (`schemas.py`) with the shared `{"data": ...}`
  envelope used by every endpoint.
- Read-only HTTP endpoints:
  - `GET /ajax-api/2.0/mlflow/caliber/agents`
  - `GET /ajax-api/2.0/mlflow/caliber/agents/{agent_id}`
  - `GET /ajax-api/2.0/mlflow/caliber/verification-queue` (with `status`,
    `severity`, `agent_id` filters)
  - `GET /ajax-api/2.0/mlflow/caliber/verification-queue/{item_id}`
- JSON error envelope handler (`routes._errors`) so 4xx/5xx responses match
  the API contract in `docs/architecture/backend.md §5.1`.
- Feedback poller lifecycle stub (`events.poller.FeedbackPoller`) wired into
  the Starlette lifespan — starts on app startup, stops cleanly on shutdown,
  swallows tick-level exceptions, idempotent stop.
- `CALIBER_DATABASE_URL` and `CALIBER_STATIC_PREFIX` environment variables.
- `caliber_refinement_jobs` and `caliber_audit_log` tables (migration `0002`).
- `caliber.ids` — `FB-/RFN-/AP-` prefixed UUID generators for verification
  items, refinement jobs, and approval requests.
- `caliber.auth` — `current_user` / `require_user` resolver reading
  `X-CALIBER-User` (placeholder until MLflow auth context is wired in).
- `caliber.audit.record()` — single entry point for writing audit-log rows
  inside the caller's transaction so audit and state stay atomic.
- `caliber.orchestrator.run_triage()` — first refinement-pipeline stage.
  Phase 2.5 ships the deterministic state-machine + classifier stub; the
  LLM-driven classifier wires into the same function in the next milestone.
- Pydantic `ValidationError` handler — request-body validation errors return
  a structured `{"detail", "status_code", "errors": [...]}` envelope.
- New HTTP endpoints:
  - `POST /caliber/verification-queue` — operator-submitted feedback
  - `POST /caliber/verification-queue/{id}/verify` — verify + auto-queue
    a refinement job in `triage` stage
  - `POST /caliber/verification-queue/{id}/dismiss` — close (optionally as
    a duplicate of another item)
  - `GET /caliber/jobs` — list refinement jobs with `status`, `stage`,
    `agent_id` filters
  - `GET /caliber/jobs/{job_id}` — single-job detail
- `caliber_runtime_locks` table (migration `0003`) — durable checkpoints
  and leases for singleton background tasks.
- `caliber.mlflow_client` — `MLflowAssessmentClient` Protocol with two
  implementations: production (`MLflowAssessmentClientImpl` against MLflow
  3.12's `search_traces` + `Trace.search_assessments`) and `FakeMLflowAssessmentClient`
  for tests. The feedback poller depends on the Protocol so it can be
  exercised end-to-end without an MLflow server.
- Feedback-poller integration: `FeedbackPoller._tick` now reads new
  assessments from the injected client, inserts verification-queue rows
  (idempotent on `assessment_id`), writes one audit row per ingest, and
  advances the `last_polled_at` checkpoint in `caliber_runtime_locks`.
  Bootstrap lookback defaults to one hour.
- `caliber.orchestrator.run_evidence()` — evidence stage of the refinement
  pipeline. Same state-machine + audit shape as triage; advances
  `running/evidence → running/diagnosis`.
- `caliber.orchestrator.RefinementWorker` — background task that picks
  the oldest queued job and advances it through every available stage
  (triage → evidence → diagnosis as of this milestone). Failure-handling
  marks the job `failed` with an `error_message` and writes a `fail_job`
  audit row. Lifespan-managed in `server.py` alongside `FeedbackPoller`.
- `diagnosis` JSON column on `caliber_refinement_jobs` (migration `0004`)
  for the structured root-cause output of the diagnosis stage.
- `[llm]` install extra (`openai-agents`, `openai`). Production deployments
  install with the extra; tests use the in-memory `FakeLLMProvider`.
- `caliber.llm` package: `LLMProvider` Protocol with `Diagnosis`,
  `EvidenceContext`, and `LLMUsage` types; `FakeLLMProvider` (deterministic
  test double); `OpenAIAgentsLLMProvider` (production wrapper around the
  OpenAI Agents SDK); and a `build_provider()` factory keyed off
  `CaliberConfig.llm_provider`.
- `caliber.orchestrator.run_diagnosis()` — fourth stage of the pipeline.
  Calls the injected `LLMProvider`, persists the structured diagnosis
  to the job row, accumulates `total_tokens` and `cost_usd`, advances
  `running/diagnosis → running/candidate`. Provider errors roll back
  and surface to the worker as `LLMProviderError`.
- New config fields: `llm_provider` (default `"fake"`), `llm_diagnosis_model`
  (default `"gpt-4o-mini"`), `llm_api_key_env` (default `"OPENAI_API_KEY"`).
- `candidate` JSON column on `caliber_refinement_jobs` (migration `0005`)
  for the structured output of the candidate-generation stage.
- `caliber.artifact_store` — `ArtifactStore` Protocol with `FakeArtifactStore`
  (in-memory test double) and `MLflowArtifactStore` (production wrapper around
  `mlflow.load_prompt("{agent_id}@prod")`). Provider selectable via the
  `artifact_store_provider` config field; defaults to `"fake"` so the server
  boots without a registry configured.
- `caliber.llm` package extended with `PromptCandidate` Pydantic model and
  `CandidateContext` dataclass. `LLMProvider.generate_candidate` added to the
  Protocol; both `FakeLLMProvider` and `OpenAIAgentsLLMProvider` implement it.
- `caliber.orchestrator.optimizer_select.select_optimizer()` — Phase 2.8 stub
  that respects per-agent overrides and otherwise returns `"MetaPrompt"`. The
  selection-rule seam matches `docs/architecture/backend.md §6.6.9`; later milestones
  fill in the GEPA / TextGrad / DSPy* branches.
- `caliber.orchestrator.run_candidate()` — fifth stage of the pipeline.
  Picks the optimizer, calls the LLM to generate a `PromptCandidate`,
  persists it to the job row alongside the optimizer name, accumulates
  cost telemetry, and advances `running/candidate → running/eval`.
- `RefinementWorker` constructor now takes `artifact_store`. The
  `_advance_job` chain extends triage → evidence → diagnosis → candidate.
- New config field: `artifact_store_provider` (default `"fake"`).
- `eval_results` JSON column on `caliber_refinement_jobs` and new
  `caliber_approval_requests` table (migration `0006`). The approval row
  carries denormalized `eval_results`, `candidate_snapshot`, and
  `diagnosis_snapshot` so historical approvals stay readable after the
  source job moves on.
- `caliber.eval` package: `EvalProvider` Protocol with `EvalRequest`,
  `EvalComparison`, `ScoreSet` types; `FakeEvalProvider` test double;
  `MLflowEvalProvider` stub that surfaces a clear "not implemented yet"
  error so operators who pick `eval_provider="mlflow"` know what to do.
- `caliber.eval.gate.apply_gate()` — pure-function regression gate
  matching `docs/architecture/backend.md §7.2`. Enforces `min_aggregate_score` and
  `max_regression_delta` from `agent_config.eval_thresholds`. Defaults
  `0.85` / `0.02` per spec.
- `caliber.orchestrator.run_eval()` — sixth pipeline stage. On gate
  pass: advances `running/eval → awaiting_approval/approval` and creates
  a `CaliberApprovalRequest`. On gate fail: marks job `rejected` with
  the gate reasons in `error_message`. Two audit rows on pass (stage
  advance + approval creation), one on rejection.
- `RefinementWorker` constructor now takes `eval_provider`; `_advance_job`
  extends through the new stage.
- New HTTP endpoints: `GET /caliber/approvals` (with `status` /
  `agent_id` filters) and `GET /caliber/approvals/{id}`.
- New config field: `eval_provider` (default `"fake"`).
- `caliber.promoter` module — `Promoter` Protocol with `PromotionRequest`
  and `PromotionResult` types; `FakePromoter` test double (records calls,
  supports simulated failure via `fail_with`); `MLflowPromoter` stub that
  surfaces a clear "not yet wired" error so operators picking
  `promoter_provider="mlflow"` see actionable guidance.
- New HTTP endpoints:
  - `POST /caliber/approvals/{id}/approve` — calls the promoter,
    marks the approval `approved`, marks the job `completed/done`,
    writes both `approve` and `promote` audit rows. Promoter failures
    surface as 502 with the approval staying `pending` for retry.
  - `POST /caliber/approvals/{id}/reject` — marks the approval
    `rejected` with a required `reason`, marks the job `rejected/done`,
    writes a `reject` audit row. Does not call the promoter.
  - `POST /caliber/approvals/{id}/request-changes` — marks the approval
    `request_changes` with required reviewer `notes`, marks the job
    `rejected/done`. A proper retry flow that returns the job to the
    candidate stage lands in a follow-up milestone.
- New request schemas: `ApprovalApproveRequest`, `ApprovalRejectRequest`,
  `ApprovalRequestChangesRequest`, `ApprovalActionResponse`.
- New config field: `promoter_provider` (default `"fake"`).
- **Atomic multi-replica safe job claim.** `RefinementWorker._claim_next_job()`
  now uses an `UPDATE ... WHERE status='queued' RETURNING job_id` transaction
  so two workers running simultaneously can never claim the same job. Works
  on both SQLite (3.35+) and Postgres without a backend-specific code path.
  Triage's eligibility check moved from `status='queued'` to `status='running'`
  to match (the atomic claim does the queued→running transition before
  any stage runs).
- **Stage-driven worker loop.** `RefinementWorker._advance_job()` now reads
  `current_stage` after each stage and dispatches via a `_STAGE_DISPATCH`
  table, so retry jobs (entered at `queued/candidate`) skip
  triage/evidence/diagnosis and start at the candidate stage. Bounded by
  `_MAX_STAGES_PER_JOB` to defend against a stage that fails to advance.
- **Multi-approver vote flow.** New `caliber_approval_votes` table
  (migration `0007`) with `(approval_id, voter)` uniqueness. The approve
  endpoint records a vote and only flips the approval to `approved` once
  the count reaches `agent_config.required_approvals`. Reject is
  single-rejector (first reject vote terminates). Double-voting by the
  same reviewer returns 409.
- **Request-changes retry semantics.** Migration `0007` drops the unique
  constraint on `caliber_approval_requests.job_id` so a job can have
  multiple approval rows across retry attempts. Each new approval row
  records its `attempt_number`. The endpoint now resets the job to
  `queued/candidate` with the reviewer's notes attached
  (`caliber_refinement_jobs.review_notes`); the worker re-claims it and
  the candidate stage threads the notes into the LLM context (via the
  new `CandidateContext.review_notes` field) before clearing them.
- New ORM model: `CaliberApprovalVote`.
- New columns: `caliber_refinement_jobs.review_notes`,
  `caliber_approval_requests.attempt_number`.
- New test files: `test_worker_atomic_claim.py`,
  `test_routes_approvals_votes.py`, `test_request_changes_retry.py`.
- **Real MLflow Prompt Registry integration.** `MLflowArtifactStore.get_active_prompt()`
  now reads via `mlflow.genai.load_prompt("prompts:/<agent_id>@<alias>")`,
  using MLflow 3.13+'s `prompts:/` URI form (bare `<name>@<alias>` is no
  longer parsed as an alias ref). The legacy top-level `mlflow.load_prompt`
  is used as a fallback for older builds.
- **Real MLflow promoter.** `MLflowPromoter.promote()` registers a new
  prompt version with `mlflow.genai.register_prompt(name, template,
  commit_message, tags)` then rotates the alias with
  `mlflow.genai.set_prompt_alias(name, alias, version)`. Both calls wrap
  backend exceptions in `PromoterError` so the approval endpoint returns
  a clean 502. The tags carry CALIBER's `approval_id` and `artifact_type`
  so the registry row is traceable back to the approval row.
- **Real MLflow eval provider.** `MLflowEvalProvider.evaluate()` calls
  `mlflow.genai.evaluate(data, predict_fn, scorers)` for the candidate
  (and baseline, when present), folds the result metrics into a
  `ScoreSet` (using `{scorer}/mean` keys, with `overall` derived as the
  mean of dimensions when not provided directly), and returns an
  `EvalComparison` with deltas. Operators register a per-agent
  `predict_fn` factory via `provider.register_predict_fn(agent_id,
  factory)`; CALIBER never imports the agent's code directly.
- New helper `_resolve_prompt_api` in `caliber.promoter` and a small
  attribute-resolution helper in `caliber.artifact_store` that prefer
  `mlflow.genai.*` (3.13+) and fall back to the module-level alias on
  older builds — so contributors on either MLflow version get clean
  behavior without `FutureWarning`s polluting CI.
- Integration tests (`tests/test_integration_mlflow.py`) cover all three
  real-MLflow integrations against a fresh SQLite-backed tracking +
  registry store under `tmp_path`. The suite is opt-in via the
  `CALIBER_INTEGRATION_TESTS=1` env var and marked
  `@pytest.mark.integration`, with a dedicated `integration` job added
  to `.github/workflows/test.yml`.
- `pyproject.toml` warning filter now also ignores `FutureWarning` and
  `UserWarning` from the `mlflow` namespace (in addition to the existing
  `DeprecationWarning` filter) — MLflow's own type-inference utilities
  emit these on import and they aren't CALIBER bugs.
- CONTRIBUTING.md gained a "Running integration tests" section
  documenting the opt-in flag, the `-m integration` marker, and what
  the suite covers.
- **Agent CRUD endpoints**:
  - `POST /caliber/agents` — register an agent (one-shot at deploy
    time). 409s on duplicate `agent_id` or `experiment_id`.
  - `PATCH /caliber/agents/{agent_id}` — partial update; the
    `enabled` field is the pause/resume toggle the worker reads. Audit
    rows record the diff (`{"from": …, "to": …}` per changed field).
  - `RefinementWorker._claim_next_job()` joins on
    `caliber_agent_config.enabled = True`, so a paused agent's queued
    jobs sit in the queue without blocking other agents.
- **Verification-queue batch + duplicate endpoints**:
  - `POST /caliber/verification-queue/{id}/duplicate` — dedicated
    "mark duplicate of X" route. Audit action is `duplicate` (vs.
    `dismiss`) so stats queries can distinguish them.
  - `POST /caliber/verification-queue/batch` — apply `verify` or
    `dismiss` to a list of item IDs in one round-trip. Per-item
    failures (409, 404) land in the response's `results` array rather
    than failing the whole batch.
- **Approval comments thread** (`caliber_approval_comments` table,
  migration `0008`):
  - `GET /caliber/approvals/{id}/comments` — list in chronological
    order.
  - `POST /caliber/approvals/{id}/comments` — append a free-form
    comment. Each post writes one `comment_added` audit row.
- **Batch-approve admin stub**: `POST /caliber/approvals/batch-approve`
  is wired into the API surface but returns 403 with an actionable
  message until Phase 5 RBAC lands. URL reservation matches the spec;
  callers see "you're not authorized" not "URL doesn't exist."
- **Rollback support** (`caliber_rollback_checkpoints` table, migration
  `0008`):
  - Every successful approval-flow promotion writes a checkpoint row
    capturing the prior + new artifact ref and version numbers.
  - `Promoter` protocol gains a `rollback(RollbackRequest)` method.
    `MLflowPromoter.rollback()` rotates the `@prod` alias back to the
    prior version via `mlflow.genai.set_prompt_alias`. `FakePromoter`
    records the call.
  - `GET /caliber/agents/{agent_id}/checkpoints` — list checkpoints
    (newest first).
  - `POST /caliber/agents/{agent_id}/rollback` — roll back to the
    most recent unused checkpoint, or to a specific
    `{"checkpoint_id": "CK-…"}` when provided. 409 on
    already-rolled-back, 502 on promoter failure (e.g. cold-start
    checkpoints with no prior version).
  - New `CK-` prefix in `caliber.ids` for checkpoint IDs.
- **Dashboard summary**: `GET /caliber/dashboard/summary` returns a
  point-in-time-consistent rollup of agents, verification queue, job
  states, and pending approvals. One round-trip replaces what would
  have been six.
- **SSE event stream** (`GET /caliber/events/stream`): in-process
  `EventBus` parked on `app.state`; the stream endpoint subscribes and
  forwards each event as `event: <type>\ndata: <json>\n\n` SSE frames.
  Heartbeats (`:keepalive`) every 15s keep reverse proxies from idling
  the connection. First proof-of-wiring publisher: the approve flow
  emits `approval.promoted` after a successful promotion. The SPA's
  `EventSource` connection replaces queue-badge polling.
- **Multi-agent targets endpoint** (`GET /caliber/jobs/{job_id}/targets`):
  returns the impacted agents/artifacts for a bundle job. Single-agent
  jobs return one target (their own agent) so the UI's bundle-review
  component doesn't need a special case; bundle jobs return one row per
  `bundle_targets` entry with extra metadata (blast radius, role,
  current version) flowing through via `ConfigDict(extra="allow")`.
- **CALIBER-namespaced eval tags**: `run_eval` now populates a
  `caliber_tags` block on `eval_results` with the parity-checklist §4
  fields — `caliber.aggregate_score`, `caliber.test_case_count`,
  `caliber.max_regression_delta`, `caliber.regression_detected`,
  `caliber.gate_passed`. They live on the JSON column today; the next
  MLflow-run integration logs them as tags on the parent run from the
  same payload.
- **SPA scaffold under `caliber-ui/`** — Vite + React 18 + TypeScript
  (strict mode) + Tailwind CSS. Design tokens (`caliber-purple`,
  `mlflow-blue`, surface neutrals, Inter font) match the mockups under
  `caliber-suite/ui-mockups/`. First page (`/`) is the Overview
  dashboard bound to `GET /dashboard/summary`; it subscribes to
  `GET /events/stream` and re-fetches on any state-changing event
  (verification, approval, job-advance, rollback) so counts update
  without a polling timer. Sidebar badges share the same summary fetch
  so they stay in lockstep with the cards. The router uses
  `BrowserRouter` with a prefix-aware `basename` (driven by
  `window.__CALIBER_STATIC_PREFIX__`) so the SPA serves cleanly behind
  any reverse-proxy mount. Other pages render placeholder cards for
  now — they land in follow-up milestones.
- **CSRF protection** (parity checklist §11). Opt-in because the
  common deployment shape behind MLflow's auth proxy already handles
  cross-site forgery via SameSite cookies + the proxy's origin
  enforcement. Deployments without that protection enable it via
  config.
  - New `caliber.csrf` module: `CSRFTokenManager` (stateless HMAC),
    `CSRFMiddleware` (ASGI), `CSRFValidationError`. Token format is
    `{unix_ts}.{hex_hmac_sha256(secret, "{ts}.{user}")}` — bound to
    user identity (cross-user replay rejected via constant-time
    `hmac.compare_digest`) and timestamped (replay after TTL rejected).
    A 60s clock-skew window is granted on both ends so a slightly slow
    validator doesn't reject fresh tokens.
  - New endpoint `GET /caliber/csrf` — issues a fresh token for the
    current user when enabled, returns `{enabled: false}` otherwise.
    Itself exempt from the middleware (the SPA needs to bootstrap a
    token before it has one).
  - Middleware enforces on `POST/PATCH/PUT/DELETE` only; reads pass
    through. When CSRF is disabled, the middleware fast-paths on a
    single `is_enabled` check — runtime cost in default deployments is
    one branch per request.
  - New config: `csrf_enabled` (default false), `csrf_signing_secret_env`
    (default `CALIBER_CSRF_SIGNING_SECRET`; name-not-value pattern so
    the secret never lands in the resolved config object),
    `csrf_token_ttl_seconds` (default 3600).
  - 22 new tests in `tests/test_csrf.py` pin the pure HMAC math
    (issuance format, user-binding, expiry, tamper detection on both
    the signature and the timestamp), the issuance endpoint's
    enabled/disabled paths + auth requirement, and the middleware's
    enforcement matrix: missing/expired/cross-user/forged tokens
    rejected, fresh tokens accepted, reads skipped, the `/csrf` URL
    itself exempted, disabled-by-default lets writes through.
- **PII redaction in audit log** (dev plan §9.1 item 5.22, parity
  checklist §11).
  - New `caliber.redaction.Redactor` — recursive walker that
    substitutes regex matches in every string leaf of a JSON-shaped
    value. Default pattern set covers emails (RFC-5322-loose),
    US phone numbers (with optional country code + various
    separators), and strict-form SSNs (`123-45-6789`). The bare 9-digit
    SSN form is *not* in the defaults so trace IDs and run IDs don't
    get false-positive redacted.
  - `caliber.audit.record()` now runs every `details` payload through
    the active redactor before persisting. A module-level singleton
    (`configure_redactor` / `get_redactor`) keeps the call sites
    unchanged — they don't need to know about the sanitization.
    Defaults to `IDENTITY_REDACTOR` so unit tests that never touch
    `create_app` behave identically to pre-redaction code.
  - New config: `pii_redaction_enabled` (default true),
    `pii_redaction_replacement` (default `[REDACTED]`),
    `pii_redaction_extra_patterns` (newline-separated additional
    regexes for deployment-specific tokens — API-key prefixes,
    AWS access keys, JWTs, etc.).
  - `server.create_app` calls `configure_redactor` after
    `configure_logging` and before route registration so every code
    path that fires `audit.record` lands on the right redactor.
  - 18 new tests in `tests/test_redaction.py` pin the default pattern
    catalog (positives + negatives), the recursive walk over nested
    dicts/lists, non-string passthrough, custom replacement markers,
    extra-pattern composition, blank-line handling, and the end-to-end
    audit integration. No new runtime dependency — stdlib `re`.
- **HMAC-signed outbound webhooks** (docs/architecture/backend.md §8.4, dev plan
  §9.1 items 5.1 + 5.20).
  - New `caliber.events.webhooks.WebhookDispatcher` — subscribes to the
    in-process `EventBus`, filters by configured event types, signs
    each POST with HMAC-SHA256, and sends it to every configured URL.
    Sync `urllib.request.urlopen` runs on a thread via
    `asyncio.to_thread` so a slow receiver doesn't block the loop.
    Per-URL failures are logged and isolated; one bad receiver
    doesn't taint the others. Best-effort delivery for v1 — retries
    with a queue table land in a follow-up.
  - **Signature format** matches the Stripe convention so receiver-
    side OSS libraries already know how to verify:
    `X-Caliber-Signature: t=<unix-ts>,v1=<hex of HMAC-SHA256(secret,
    "{ts}.{payload}")>`. Plus a separate `X-Caliber-Timestamp` header
    so receivers can pin a `±5 minute` replay window without parsing
    the signature line.
  - New config fields: `webhook_urls` (comma-separated),
    `webhook_signing_secret_env` (name of an env var; the secret
    value itself never lands in the resolved config object), and
    `webhook_event_filter` (default subscribes to approvals,
    verification, job failures, rollbacks; `*` subscribes to all).
  - Server lifespan starts/stops the dispatcher alongside the worker,
    poller, and janitor. Disabled with a logged info / warning when
    URLs are missing or the signing secret env var isn't set.
  - 16 new tests in `tests/test_events_webhooks.py` pin: the exact
    signing format, payload-binding (tamper detection),
    timestamp-binding (replay defense), construction-time
    enable/disable rules, event filtering, per-URL failure isolation,
    non-2xx logging, and the dispatcher lifecycle. No new runtime
    dependency — stdlib `urllib` + `hmac`.
- **Cross-endpoint RBAC enforcement.** Every write endpoint now calls
  ``require_scopes`` and returns 401 (anonymous) or 403 (signed-in,
  missing scope) before touching state. Mapping:
  - Verification-queue writes (create / verify / dismiss / duplicate /
    batch) → `caliber.operator`.
  - Approval decisions (approve / reject / request-changes / add-comment)
    → `caliber.approver`.
  - Rollback execution → `caliber.operator`.
  - Agent CRUD (register / update / pause / resume) → `caliber.admin`.
  - Batch-approve was already admin; unchanged.
  - Reads stay open — any authenticated request lands on `caliber.viewer`.
  - 29 new tests in `tests/test_rbac_enforcement.py` walk the full write
    surface in a parametrized table: every endpoint × {anonymous, viewer}
    is asserted to return 401/403 respectively. Plus spot-checks that
    cross-scope boundaries (operator can verify but not approve, approver
    can decide but not register agents) work as designed.
  - `current_user` now treats an empty `X-CALIBER-User` header the same
    as a missing one — upstream proxies that forward stripped headers
    as empty strings get the same anonymous treatment as proxies that
    omit the header entirely.
- **Test infrastructure: scoped default client.** The conftest's
  `client` fixture now sets a default `X-CALIBER-User` header and the
  default `app_config` grants admin scope to a small permissive list
  (`@test, @admin, @reza, @sarah, @alex, @a, @b`). Existing tests that
  asserted on audit-log actor keep working without changes; tests that
  need the 401/403 branches override the header per-request.
- **RBAC scopes + admin-gated batch-approve** (parity checklist §11 /
  development-plan §9.1 item 5.6).
  - New scope vocabulary in `caliber.auth`: `SCOPE_VIEWER`,
    `SCOPE_OPERATOR`, `SCOPE_APPROVER`, `SCOPE_ADMIN`. Admin inherits
    every lower scope; approver and operator both inherit viewer. Every
    authenticated user gets viewer access — reads stay open.
  - `current_scopes(request) -> frozenset[str]` resolves the active
    user against config-driven user lists (`admin_users`,
    `approver_users`, `operator_users` — all comma-separated, settable
    via `CALIBER_*_USERS` env vars). The first cut is config-driven so
    OSS deployments work out of the box; a future milestone can swap
    the resolver for a DB-backed assignment table without changing any
    call site (signature stays `(Request) -> frozenset[str]`).
  - `require_scopes(request, scopes)` raises 401 for anonymous
    requests, 403 for signed-in-but-missing-scope, and returns the
    authenticated user ID otherwise (so the caller passes it straight
    to `audit_record(actor=…)`).
  - `POST /caliber/approvals/batch-approve` is no longer a 403 stub:
    admins force-approve a list of pending approvals, bypassing the
    per-agent quorum. Each approval still goes through the same
    promoter call and checkpoint write as the single-approve path; the
    audit trail records one extra `admin_override` row per approval so
    it's unambiguous who bypassed the quorum.
  - 16 new tests in `tests/test_auth_scopes.py` cover the scope
    resolver (anonymous, single role, multi-role union, admin
    inheritance), the `require_scopes` 401/403/200 branches, and
    the end-to-end admin-batch-approve flow + skipped/failed
    per-item outcomes.
- **Test infrastructure: per-test SQLite file** (incidental but
  load-bearing). The previous shared in-memory cache caused
  intermittent `database table is locked` failures once the suite grew
  past ~350 tests — `create_app`'s internal engine and the fixture
  engine were two separate connection pools racing on the same cache.
  Each test now gets its own SQLite file under `tmp_path`. Test
  runtime went from ~3s to ~6s; flake rate went from ~40% to 0/10
  across deliberate retries.
- **Job heartbeat + janitor + graceful shutdown** (parity checklist
  §10 / development-plan §9.1 items 5.18-19).
  - New `caliber_refinement_jobs.last_heartbeat_at` column (migration
    `0009`). Seeded at claim time and bumped at the start of every
    stage so the worker's liveness is observable from the DB alone.
  - New `caliber.orchestrator.janitor.JanitorTask` — periodic
    background sweeper. Each tick selects `status='running'` jobs
    whose `last_heartbeat_at` is older than the configured stale
    threshold (or whose `last_heartbeat_at IS NULL` AND
    `updated_at` is older — the legacy-row fallback) and marks them
    as `failed` with a diagnostic `error_message`. Writes one
    `reap_stale_job` audit row per reaped job and bumps the
    `caliber_jobs_total{status=failed}` metric so the failure shows
    up alongside other terminal transitions.
  - Graceful shutdown for the worker and poller: `stop(grace_seconds)`
    now waits up to the configured window for the in-flight tick to
    finish (`asyncio.wait_for` + `asyncio.shield`) before cancelling.
    A long stage gets to commit its terminal status; only an
    over-grace task is cancelled. Symmetric on both background tasks.
  - Server lifespan teardown threads the worker / poller / janitor
    through in the right order: worker drain → poller drain →
    janitor cancel → engine dispose. No task is mid-DB-write when
    the engine closes.
  - New config fields: `janitor_interval_seconds` (default 60s),
    `janitor_stale_threshold_seconds` (default 300s),
    `shutdown_grace_seconds` (default 30s). All overridable via
    `CALIBER_*` env vars; `config.load`'s env→kwargs mapping was
    refactored to a table so future fields don't push the function
    over the per-function branch cap.
  - 15 new tests across `tests/test_orchestrator_janitor.py` and
    `tests/test_worker_heartbeat_shutdown.py` pin the heartbeat
    bumping, the four reap branches (stale-heartbeat, fresh-
    heartbeat, NULL-heartbeat-but-recent-updated_at, NULL-and-old),
    the audit row contents, the lifecycle, and the graceful-shutdown
    drain + timeout-cancel paths.
- **Observability — structured logging + trace IDs + Prometheus metrics.**
  - New `src/caliber/observability/` package:
    - `trace.py` — `trace_id_var` ContextVar, `bind_trace_id` helper,
      and `TraceIdMiddleware`. The middleware reads
      `X-Request-Id` / `X-Trace-Id` from the request, falls back to a
      fresh 16-hex-char ID, pushes it onto the ContextVar for log/
      metric correlation, and echoes it back on the response.
      Background tasks (`RefinementWorker._tick`,
      `FeedbackPoller._tick`) each wrap one iteration in
      `bind_trace_id()` so every log line emitted during one tick
      shares a trace ID.
    - `logging.py` — `JsonFormatter` emits single-line JSON per
      `LogRecord`: `t`/`severity`/`logger`/`message` plus optional
      `trace_id`, `error`, `stack_trace`, and any `extra=` keys. The
      `configure_logging()` helper installs it as the root handler.
      `server.create_app` calls it on every startup.
    - `metrics.py` — Prometheus collectors (counters, histograms,
      gauges) against a CALIBER-owned `CollectorRegistry`. Helper
      facades (`record_verification_outcome`, `record_job_terminal`,
      `record_approval_decision`, `record_promotion`,
      `record_rollback`, `observe_stage_duration`, plus gauge setters)
      keep call sites short. `reset_metrics_for_test()` zeros the
      registry between tests.
  - New `src/caliber/routes/metrics.py` — `GET /ajax-api/2.0/mlflow/
    caliber/metrics` exposes the registry in Prometheus text format.
  - Metric increments wired into the approve / reject / request-changes
    endpoints, verify / dismiss / duplicate endpoints, rollback
    endpoint, and the worker's failure path. The standard scrape
    config sees every state change CALIBER cares about.
  - New runtime dependency: `prometheus-client>=0.20,<1`. Zero-config
    library with no transitive deps.
  - 21 new tests (in `tests/test_observability_*.py`) pin JSON-log
    schema, trace-ID propagation through the middleware (including the
    64-char cap on oversized inbound headers), and metric increments
    end-to-end through the approve endpoint.
- **SPA ship-side wiring.** The Vite build now ships inside the Python
  wheel, served by the plugin itself.
  - New [`caliber.routes.static`](caliber/src/caliber/routes/static.py)
    module registers `GET /caliber/` and `GET /caliber/{path:path}`.
    Existing files in the bundled `ui/` dir stream via
    `FileResponse`; everything else falls back to `index.html` so the
    React Router's history-mode deep links survive a hard refresh.
  - At serve time the handler injects
    `<script>window.__CALIBER_STATIC_PREFIX__="…"</script>` into the
    served `index.html` from `CaliberConfig.static_prefix`, so the SPA
    builds prefix-aware API URLs and router basenames when MLflow runs
    behind a reverse-proxy subpath. JSON-encoding the prefix keeps an
    embedded quote from breaking out of the script literal.
  - Path-traversal attempts are caught: each requested path is
    resolved against `ui_dir` and rejected if the resolution lands
    outside, so `../../etc/passwd` falls back to the SPA shell.
  - When the UI bundle isn't on disk (a `pip install -e .` checkout
    without an `npm run build`), `GET /caliber/` returns a 503 with
    operator-facing instructions instead of a generic 404. The API
    surface keeps working in that mode.
  - `pyproject.toml`: Hatchling `artifacts = ["src/caliber/ui"]`
    bundles the SPA into the wheel even though the directory is
    `.gitignored`.
  - New CI jobs in `.github/workflows/test.yml`:
    - `ui` builds + typechecks the SPA and uploads `caliber-ui/dist/`
      as an artifact.
    - `package` (depends on the unit + UI jobs) downloads the SPA
      artifact, stages it into `src/caliber/ui/`, runs `python -m
      build`, and sanity-checks that `caliber/ui/index.html` made it
      into the wheel.
  - 16 new tests in [`tests/test_routes_static.py`](caliber/tests/test_routes_static.py)
    pin the prefix-injection logic, the asset/index fallback, the
    traversal guard, and the 503-when-missing behavior.
- **All eight Phase 3 frontend pages.** The SPA now matches the page set
  from the mockups in `caliber-suite/ui-mockups/`:
  - **Verification Queue** + **Verification Detail** — filterable list
    with severity/status badges + batch verify/dismiss bar; per-item
    detail page with three-tab action panel (verify, dismiss, mark
    duplicate). URL-param filters survive refresh.
  - **Refinement Jobs** + **Job Detail** — list with compact pipeline-
    progress dots + optimizer/cost columns; detail page with full six-
    stage progress, diagnosis/candidate/eval section cards, and a
    bundle-targets sidebar when `bundle_size > 1`.
  - **Approvals** + **Approval Detail** — list defaults to
    `status=pending`, each row showing aggregate score + Δ-vs-baseline
    pill; detail page is the human-in-the-loop apex (candidate +
    rationale, eval comparison + delta pills, gate banner, three-action
    panel for approve/reject/request-changes, comments thread).
    Successful approve shows a `PromotionBanner` with the artifact ref
    and auto-navigates to the job after 1.2s.
  - **Settings** — agent fleet table with inline pause/resume toggle,
    register-agent form panel, per-agent inline edit form (with JSON
    fields for `eval_thresholds` / `optimizer_config`).
  - **Agent History** — per-agent timeline with refinement-jobs table,
    promotion / rollback checkpoints, and one-click rollback per
    checkpoint (subject to the same 409/502 surface the backend
    exposes).
- Reusable UI components:
  [`SeverityBadge`](caliber/caliber-ui/src/components/SeverityBadge.tsx),
  [`StatusBadge`](caliber/caliber-ui/src/components/StatusBadge.tsx),
  [`PipelineProgress`](caliber/caliber-ui/src/components/PipelineProgress.tsx)
  (compact dots + expanded labelled mode),
  [`EvalComparison`](caliber/caliber-ui/src/components/EvalComparison.tsx)
  (shared across Job Detail and Approval Detail), and
  [`CommentThread`](caliber/caliber-ui/src/components/CommentThread.tsx).
- SSE auto-refresh wired into every list/detail page — `useEventStream`
  subscribes to the event-type subset each page cares about and triggers
  a `refresh()` on any match (scoped to the matching ID for detail
  pages). Reviewers see counts and progress bars update without a
  polling loop.
- The API client (`src/api/caliberApi.ts`) exposes typed methods for
  every backend endpoint shipped so far — dashboard, verification queue
  (list / detail / verify / dismiss / mark-duplicate / batch), jobs
  (list / detail / targets), approvals (list / detail / approve /
  reject / request-changes / comments), agents (list / detail /
  register / update), checkpoints (list / rollback).
- New `caliber-ui/` directory:
  - `package.json` (Vite 5, React 18, react-router 6) — no
    data-fetching library yet; a small `useApi` hook over `fetch` is
    plenty for the first slice.
  - `src/api/caliberApi.ts` — typed fetch wrapper with envelope
    unwrapping and a typed `ApiError`.
  - `src/hooks/useEventStream.ts` — native `EventSource` subscription
    with type filtering, used by the Overview page for live updates.
  - `src/components/{AppShell,Sidebar,TopBar,StatCard}.tsx` — the
    chrome and one reusable card.
  - `src/pages/Overview.tsx` — the landing page; six stat cards plus
    a fleet summary band.
  - `vite.config.ts` — dev proxy forwards `/ajax-api/*` to
    `CALIBER_API_TARGET` (default `http://localhost:5000`).
  - `README.md` — developer quick-start, configuration, and project
    layout.
- **Expanded SSE publishers** — the event bus is now wired into every
  write-path endpoint plus the worker loop:
  - `verification.verified`, `verification.dismissed`,
    `verification.duplicate` from the verification-queue routes.
  - `approval.rejected`, `approval.changes_requested` from the
    approval routes (`approval.promoted` was already wired).
  - `agent.rolled_back` from the rollback endpoint.
  - `job.advanced` (after each successful stage) and `job.failed`
    (from `_mark_failed`) from the refinement worker. The worker's
    constructor now accepts an optional `event_bus` parameter; tests
    that don't care about events leave it unset.
- **LLM circuit breaker** (parity checklist §5.24). Wraps the
  configured LLM provider so a misbehaving upstream doesn't burn
  through the retry budget of every job in the queue.
  - New `caliber.llm.circuit_breaker` module:
    `CircuitBreakerLLMProvider` (CLOSED → OPEN → HALF_OPEN → CLOSED
    state machine over a sliding failure window), `CircuitState`
    enum, and `LLMCircuitOpenError` — a distinct subclass of
    `LLMProviderError` the worker recognizes for the re-queue path.
    Thread-safe via a single `threading.Lock`; the time source is
    parameterized so tests pin transitions deterministically.
  - When the breaker is `OPEN`, calls fast-fail without touching the
    inner provider. After `open_duration_seconds` elapses the
    breaker transitions to `HALF_OPEN` and admits exactly one probe;
    success closes the circuit, failure re-opens it for another
    full cooldown.
  - `build_provider()` wraps the base provider with the breaker by
    default. Extracted to `_build_base_provider` so the wrap-or-skip
    decision lives in one place; tests targeting the raw provider
    can disable the breaker via config.
  - Refinement worker catches `LLMCircuitOpenError` and **re-queues**
    the job at its current stage (status='queued') rather than
    marking it `failed`. An `defer_job` audit row records the
    reason; a `job.deferred` SSE event is published so the UI can
    show the breaker-trip without conflating it with a hard
    failure. This is the "defer jobs without consuming retry
    budget" semantic from parity checklist §5.24.
  - New config fields: `llm_circuit_breaker_enabled` (default true),
    `llm_circuit_failure_threshold` (default 5),
    `llm_circuit_window_seconds` (default 60), and
    `llm_circuit_open_duration_seconds` (default 30).
  - 19 new tests in `tests/test_llm_circuit_breaker.py` pin every
    transition (closed → open at threshold, sliding window expiry,
    open → half-open after cooldown, half-open → closed on probe
    success, half-open → open on probe failure), the
    shared-circuit-across-methods invariant, construction
    validation, and the `maybe_wrap` convenience helper. Two new
    tests in `tests/test_orchestrator_worker.py` pin the
    re-queue + `job.deferred` event path.
- **Per-user rate limiting** (parity checklist §11). Opt-in because
  most deployments already rate-limit at the gateway (NGINX/Envoy/
  corporate WAF). Single-replica state in-process — Redis-backed
  store is a future enhancement when multi-replica deployments
  warrant it; the middleware contract doesn't change.
  - New `caliber.rate_limit` module: `TokenBucket` (capacity +
    refill_per_second, time-source-parameterized so tests pin the
    clock), `RateLimiter` (thread-safe per-user dict of buckets),
    and `RateLimitMiddleware` (ASGI; reads `X-CALIBER-User`, returns
    429 with `Retry-After` when the bucket is empty). Anonymous /
    missing-header traffic shares one bucket keyed `anonymous` so a
    single client without identity can't drown out legit traffic.
  - Token bucket (rather than fixed-window): absorbs short bursts
    without rejecting, no clock-edge spike, and the refill math is
    fractional so any rate works.
  - `build_limiter()` returns `None` when disabled so
    `server.create_app` skips installing the middleware entirely —
    runtime cost is exactly zero in the default deployment, not
    "one is_enabled check per request."
  - Exempt paths: `/caliber/health` (so liveness probes don't drain
    `anonymous`'s bucket) and `/caliber/csrf` (so the SPA can
    bootstrap a CSRF token before spending its budget on it).
    Defined as module-level constants in `routes/health.py` and
    `routes/csrf.py` so the server imports them by name rather than
    duplicating the URL string.
  - New config: `rate_limit_enabled` (default false),
    `rate_limit_requests_per_minute` (default 120),
    `rate_limit_burst` (default 30).
  - 19 new tests in `tests/test_rate_limit.py` pin: bucket refill
    math (drain, time-based replenishment, capacity cap on idle,
    `seconds_until_available` proportional to refill rate), per-user
    isolation (separate buckets, anonymous bucket for empty
    identity), construction validation, the `build_limiter`
    enable/disable contract, and the end-to-end middleware behavior
    (writes 429 after burst with Retry-After header, separate users
    have separate budgets, health and CSRF endpoints are exempt,
    disabled config doesn't install the middleware).
- **Secret-source resolver** for env-var-name config fields. Every
  field that names a secret (`webhook_signing_secret_env`,
  `csrf_signing_secret_env`, `llm_api_key_env`) now flows through a
  single resolver that supports multiple backends. Backwards
  compatible — bare env-var names still work, but operators can
  now opt into file-mounted secrets without changing the call site.
  - New `caliber.secrets` module: `resolve_secret(source, *,
    environ=None) -> str | None`. Scheme dispatch:
    - Bare string (no scheme) → env var name (backwards compatible).
    - `env://VAR_NAME` → env var read (explicit form).
    - `file:///abs/path` → file contents, whitespace stripped (common
      pattern for Kubernetes `Secret` volume mounts, Docker Compose
      `secrets:` blocks, Vault Agent templates).
  - Future schemes (`vault://`, `awssm://`, `gcpsm://`) plug in
    behind the same interface — no caller changes needed when they
    land.
  - The actual secret value is *never* logged. Log lines reference
    the *source* (`env://X`, `file:///path`) so the audit trail
    survives without leaking the value. Missing/empty sources log at
    WARNING and the resolver returns `None` so the feature
    self-disables instead of running with an empty signing key.
  - Three call sites updated to flow through the resolver:
    `caliber.csrf.build_token_manager`,
    `caliber.events.webhooks.build_dispatcher`, and
    `caliber.llm.provider._build_base_provider` (OpenAI API key).
    `os.environ.get` direct reads removed from all three.
  - Config field descriptions updated to document the new URI form.
    No field renames — the existing `*_env` suffix stays for
    backwards compatibility (treating the field as "secret source"
    rather than literally "env var name" doesn't require a
    schema-incompatible change).
  - 20 new tests in `tests/test_secrets.py` cover the pure
    resolution surface (bare string, `env://`, `file://`,
    missing/empty cases) plus integration through all three call
    sites (CSRF + webhook builders accept file URIs and gate
    themselves off on unresolved secrets; LLM provider loads its API
    key from a file mount as cleanly as from an API key from an env
    var, and fail-fasts when the source is empty).
- **Skills** — first Phase 4 resource type. A skill is a reusable
  prompt fragment (tool-use instructions, safety guardrails,
  formatting conventions, reasoning rubrics) that multiple agents
  can compose into their prompts. Refining a skill once cascades to
  every agent that references it by name — the cross-agent leverage
  that motivates Phase 4 multi-agent bundles.
  - New `caliber_skills` table (migration `0010_skills.py`):
    `skill_id` (PK, `SK-` prefixed UUID via
    :func:`caliber.ids.new_skill_id`), `name` (unique handle agents
    reference), `description`, `content`, `owner`, `tags` (JSON
    array), `status` (`active` / `archived` — the soft-delete
    path), `version` (auto-bumped on content change so external
    references can detect drift), `created_at` / `updated_at`.
  - `CaliberSkill` ORM model in `caliber.db.models`. The migration
    drift test (`tests/test_migrations.py`) verifies the new table
    matches the model.
  - New HTTP endpoints under `/ajax-api/2.0/mlflow/caliber/skills`:
    - `GET /skills` — list, filterable by `status` (default
      `active` hides archived; `all` returns everything; any other
      value filters exactly) and by `tag` (membership-in-JSON).
    - `GET /skills/{skill_id}` — single skill. Returns archived
      skills too so old agent histories that reference them stay
      interpretable.
    - `POST /skills` (operator) — create with a server-generated
      `skill_id`. 409 on duplicate `name`.
    - `PATCH /skills/{skill_id}` (admin) — partial update.
      `version` auto-bumps when `content` changes (but not for tag
      or owner tweaks — those don't invalidate cached references).
      Archive path: `status: "archived"`.
  - New Pydantic schemas in `caliber.schemas`: `SkillSchema`,
    `SkillCreateRequest` (`extra="forbid"`, content/name
    `min_length=1`), `SkillUpdateRequest` (all fields optional,
    `status` regex-restricted to `active|archived`).
  - RBAC: skill creation requires `caliber.operator`; update
    requires `caliber.admin`. Two new rows added to the
    parametrized cross-endpoint RBAC test in
    `tests/test_rbac_enforcement.py` so the anonymous/viewer
    rejection sweep covers the new endpoints automatically.
  - Every write writes one audit row in the same transaction as the
    mutation, with the diff (which fields changed, old/new values,
    and whether the version was bumped).
  - 19 new tests in `tests/test_routes_skills.py` cover list
    filtering, 404 on missing skill, create happy-path + name
    409 + audit row, update version-bump-on-content,
    no-bump-on-non-content, archive flow, validation errors, no-op
    on unchanged values, and the audit row contents.
- **Agent → skill cross-references**. Agents can cite skills by name
  in their ``optimizer_config.skills`` array; a new endpoint
  resolves the citations to full skill records, separating
  ``skills`` (resolved) from ``missing`` (cited names with no
  matching row).
  - New endpoint ``GET /caliber/agents/{agent_id}/skills`` —
    reads the agent's ``optimizer_config.skills`` list, looks up
    every name, and reports unresolvable names so the UI can flag
    broken references rather than silently dropping them. Archived
    skills are included in the resolved list (an old agent config
    pointing at an archived skill still needs to be inspectable).
  - New ``AgentSkillsResponse`` Pydantic schema.
  - 6 new tests in ``tests/test_agent_skills.py`` cover 404,
    empty optimizer_config, the happy path, missing-reference
    reporting, archived inclusion, and malformed-config tolerance.
- **Eval datasets** — versioned input/expected sets the refinement
  pipeline scores candidates against. Examples are append-only with
  a supersede flow so historical job runs stay reproducible.
  - New tables ``caliber_eval_datasets`` and
    ``caliber_eval_dataset_examples`` (migration ``0011``). The
    dataset row carries a monotonically-bumping ``version`` integer
    that increments on every example append or supersede.
  - ``CaliberEvalDataset`` + ``CaliberEvalDatasetExample`` ORM
    models. ``new_eval_dataset_id`` (``ED-`` prefix) +
    ``new_eval_example_id`` (``EX-`` prefix) added to
    ``caliber.ids``.
  - New endpoints under ``/caliber/eval-datasets``:
    - ``GET /eval-datasets`` (list, status + tag filters) and
      ``GET /eval-datasets/{id}`` (detail).
    - ``POST /eval-datasets`` (operator) — create with
      server-generated id; 409 on duplicate name.
    - ``PATCH /eval-datasets/{id}`` (admin) — metadata edits +
      archive.
    - ``GET /eval-datasets/{id}/examples`` (filter by
      ``version`` to recover the exact set for an old job;
      ``include_superseded=true`` to see retired examples).
    - ``POST /eval-datasets/{id}/examples`` (operator) — append an
      example; bumps dataset.version.
    - ``POST /eval-datasets/{id}/examples/{example_id}/supersede``
      (admin) — idempotent retire of an example without delete.
  - 14 new tests in ``tests/test_routes_eval_datasets.py`` cover
    every endpoint plus version-bump on append, supersede
    idempotency, version-pinned example listing, and the audit
    rows.
- **Conversation policies** — multi-turn agent behavior rules (turn
  budgets, escalation triggers, handoff conditions).
  - New table ``caliber_conversation_policies`` (migration
    ``0012``) + ``CaliberConversationPolicy`` ORM model.
    ``new_conversation_policy_id`` (``CP-`` prefix).
  - New endpoints under ``/caliber/conversation-policies``:
    ``GET`` list/detail, ``POST`` create (operator), ``PATCH``
    update (admin). Same CRUD shape as skills. Updating ``rules``
    bumps ``version``; metadata-only updates don't.
  - 9 new tests in
    ``tests/test_routes_conversation_policies.py`` mirror the
    skills coverage.
- **Atomic bundle promotion**. The approve endpoint now treats a
  job's ``bundle_targets`` as a real multi-artifact promotion that
  lands all artifact changes or none. Partial promotions
  auto-rollback in reverse order; the caller sees a 502 with the
  failing target named.
  - New ``caliber.bundle`` module: ``BundleTarget`` dataclass,
    ``resolve_bundle_targets()`` (turns
    ``CaliberRefinementJob.bundle_targets`` JSON into a typed
    list; synthesizes a single primary target for non-bundle
    jobs so the approve path is one code branch), and
    ``promote_bundle()`` (atomic-or-rollback: on any
    ``PromoterError`` rolls back the already-promoted targets in
    reverse order, then raises). Rollback failures are logged
    operator-actionable + named in the wrapped error message.
  - The approve endpoint now records one rollback checkpoint per
    target (legacy single-target shape preserved for back-compat
    with the existing rollback endpoint; multi-target bundles get
    a ``snapshot_payload: {"bundle_target": true}`` marker on each
    row).
  - The approve response gains ``bundle_size`` +
    ``bundle_results`` fields with the per-target
    ``artifact_ref``. Legacy fields (``artifact_ref``,
    ``rotated_at``, ``details``) continue to reference the primary
    target for back-compat.
  - 8 new tests in ``tests/test_bundle.py`` (helper unit tests:
    target resolution, atomic happy path, mid-failure rollback,
    rollback-of-rollback failure tolerance) and 4 new tests in
    ``tests/test_routes_approvals_bundle.py`` (end-to-end: bundle
    promotes all targets; partial failure rolls back + keeps the
    approval pending; audit rows record ``bundle_size``;
    single-target back-compat).
- **Pattern detection on the verification queue**. Surfaces
  clusters of similar feedback so an operator can act on the
  underlying cause rather than refining each item in isolation.
  - New ``caliber.patterns`` module: ``detect_patterns()`` buckets
    items by ``(agent_id, category, severity)`` and emits a
    ``FeedbackPattern`` row for any bucket above
    ``min_cluster_size`` (default 3). Dismissed items are
    excluded — only ``pending`` + ``verified`` items count.
    Results sorted by descending count, ties broken by most-recent
    last-seen.
  - ``category_distribution()`` companion function returns the
    ``{category: count}`` histogram for the dashboard.
  - New endpoint ``GET /caliber/patterns`` with ``agent_id`` and
    ``min_cluster_size`` query parameters; threshold clamped to
    ``[1, 100]``.
  - 16 new tests in ``tests/test_patterns.py`` cover bucketing,
    threshold filtering, dismissed exclusion, agent filtering,
    sort order, first/last-seen, custom threshold, the
    distribution helper, and the HTTP surface.
- **Phase 4 SPA pages** — four new screens for the new resource
  types, plus an extended sidebar.
  - ``caliber-ui/src/pages/Skills.tsx`` — list with status filter
    (active/archived/all), inline create form, one-click
    archive/restore.
  - ``caliber-ui/src/pages/EvalDatasets.tsx`` — list with status
    filter and inline create.
  - ``caliber-ui/src/pages/ConversationPolicies.tsx`` — list with
    a rules-preview column + JSON rules editor in the create
    panel.
  - ``caliber-ui/src/pages/Patterns.tsx`` — two-card layout:
    category-distribution bar chart (no chart library — CSS-only
    proportional bars) and the clusters table with expand-to-show
    member item IDs. Member IDs are clickable links into the
    verification-queue detail pages.
  - Sidebar gains a "Library" section with the four new nav
    items.
  - ``caliberApi.ts`` extends with typed methods for every new
    endpoint; ``types.ts`` adds matching TypeScript models.
  - Vite build + ``tsc --noEmit`` pass cleanly with the new
    pages.
