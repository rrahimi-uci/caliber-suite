# R1: Evaluation, learning and observability half of Caliber, assessed against "autonomy is earned"

Scope: main at 82f41d1b8 (read-only). Method: opened code, models, tests and docs; where a claim rests only on docs or plan it is labelled **[plan]** or **[doc]**. All paths are repo-relative to the checkout. Posture: critical, not advocating. The vision may itself be wrong in places; those are flagged in section 9.

---

## 0. Bottom line in ten sentences

1. Caliber today has a **real, honest, release-time evaluation and evidence stack**: pinned datasets, deterministic and LLM-judge scorers, per-dimension regression gate, digest-bound evidence, intent-first alias release with rollback anchors. This is the strongest asset in this half of the platform.
2. It has **no runtime feedback from outcomes to authority**. Nothing observes production traces, nothing scores live runs, nothing computes a track record per agent, role, workflow or version, and nothing can change what an actor is allowed to do as a result of measured behaviour.
3. "Autonomy" in code is **configured**, not earned: Aria's `AssistantApprovalMode` (`manual`..`full_autonomy`) and `CaliberAriaPlan.autonomy` (`ask_each`/`approve_plan`/`auto_guarded`) are user/session settings. The only agent-holds-a-human-role path (`agent_review`/`full_autonomy`) is gated by a static self-reported LLM confidence threshold, not by any measured history.
4. The accepted ADR 0004 (L0-L5, promotion as governed release, automatic one-step demotion) is the **right shape** but is **100% [plan]**: `principal_type`, `autonomy_level`, `GovernanceRecord`, acceptance predicates and trajectory gates appear in zero Python files under `caliber/src`. It lives in ADR 0004, `docs/CALIBER_EGI_DELIVERY_PLAN.md`, `docs/architecture/approval-ux-contract.md` and tasks M24/M27/M09/B09 of `docs/planning/beta-mvp-execution.json`, all scheduled D4-D10.
5. The word "agent" in Caliber's own schema (`CaliberAgentConfig`) means a **managed application/experiment under refinement**, not an acting principal. This naming collision is a latent source of design confusion for the autonomy work.
6. Eval gates are **inconsistent across artifact families and often advisory**. `artifact_capabilities.py` itself records six distinct `gate_mode` values; the direct prompt alias route accepts a client-supplied `gate_state` and does not enforce it.
7. The refinement loop automates diagnosis, candidate generation and gating; **a human operator must click Apply** (and the two-person rule applies only in the reference profile). That is a correct default for "earn autonomy", but there is no mechanism by which a high-performing loop ever stops needing the click.
8. Observability is strong on **provenance and linkage** (run manifests, context manifests, pinned versions, causal span attributes, owner registry, SLO incident records) and weak on **behavioural telemetry per actor** (SLOs are platform-wide; Prometheus labels are `agent_id` for refinement jobs only).
9. State/memory is partially pinned (KB versions, approved context sources, context manifest with trust tiers) but memory is default-off, free-form partitioned, and not governed; replay and acceptance (B09) are plan only.
10. Differentiated vs commodity: digest-bound release evidence, intent-first releases, pinned run/context manifests, exact-intent approvals and the (planned) evidence-governed autonomy ladder are differentiators if shipped. Scorers, judges, tracing, prompt registries, optimizers (GEPA/DSPy/MetaPrompt) and dataset management are commodity or MLflow-provided.

---

## 1. Capability classification (my scope)

Legend: **S** Supported, **P** Partially supported, **M** Missing, **C** Architecturally conflicting (an existing structural decision resists the vision). "Fix kind": A architectural, P product, I implementation, R roadmap (already planned, needs sequencing).

| # | Capability | Status | One-line evidence | Fix kind |
|---|---|---|---|---|
| 1 | Evaluation (offline, pinned) | **P** (strong) | `eval/` stack, `workspace_release_evaluation_plan.py`, `eval/evidence.py` | I/R |
| 2 | Evaluation (online / continuous on live runs) | **M** | no scorer runs on production traces; no poller (`routes/observability.py::post_feedback` docstring says so) | A/P |
| 3 | Validation (acceptance of business outcome, independent of model text) | **M** [plan B09] | no acceptance predicate code; `workflow_success_rate` = run completed | A |
| 4 | Calibration / refinement loop | **P** | `orchestrator/*`, `workflows/refinement.py`, `workflows/calibration.py`; starts at a prompt + test set, not at a flagged trace | P |
| 5 | Release gates / governance | **P** | gate modes differ per family; some advisory; workspace release path behind off-by-default flags | A |
| 6 | Rollback / checkpoints | **P** | `CaliberRollbackCheckpoint`, per-family mechanism; manual only | I |
| 7 | Progressive delivery (shadow/canary/traffic split) | **M** | `CaliberWorkflowDeployment` is one `version_id` per alias | A |
| 8 | Drift / regression monitoring | **M** [plan M09.3] | only candidate-vs-baseline at release; no live drift | A |
| 9 | Per-actor/role/workflow track record (scorecard) | **M** | `eval/scorecard.py` is a per-dataset-row scorer, not a track record | A |
| 10 | Autonomy levels | **M** in code, designed in ADR 0004 | grep: zero hits for `autonomy_level` | A/R |
| 11 | Autonomy promotion / demotion | **M** [plan M27.4a/b] | only `CaliberAgentConfig.enabled` (admin flag, refinement worker only) | A/R |
| 12 | Evidence model for autonomy | **P (raw material only)** | `CaliberWorkspaceReleaseEvidence` closed 5-kind table, `GateDecision`, audit log | A |
| 13 | Observability: tracing and linkage | **P/S** | `observability/mlflow_tracing.py`, `span_attributes.py`, `run_manifest.py`; trace-to-chain read-back (B11.3b) not implemented | I |
| 14 | Observability: health, SLO, incidents | **P** | `observability/slo.py`, `incidents.py`, `queue_health.py`; platform signals only | I/P |
| 15 | Auditability | **S/P** | `CaliberAuditLog`, `audit.record` everywhere; tamper-evident archive and evidence package B14 [plan] | R |
| 16 | State and memory | **P** | KB versions, approved context sources, context manifest; mem0 default-off, ungoverned partitions [map-gap G15] | R/A |
| 17 | Multi-agent coordination | **P (structural only)** | workflow handoffs compiled to OpenAI Agents SDK; Aria planner/reviewer; no trust/delegation model | A |
| 18 | Human-agent collaboration | **P** | Aria sessions/plans, runtime approvals, quality reviews, rework tasks, change requests | P |
| 19 | Exception handling / escalation | **P** | rework tasks, approval expiry, incident open/silence; no timers/escalation chain (M06 [plan]) | R |
| 20 | UI / SDK / CLI exposure of roles, approvals, evaluation | **P** | approvals and evaluation have surfaces; no role/principal/autonomy surface | P/R |

---

## 2. What the evaluation stack actually is

### 2.1 Evaluation providers and scorers (Supported, commodity-plus-discipline)

- `eval/provider.py`: the seam. `EvalProvider.evaluate(EvalRequest) -> EvalComparison` returns a candidate `ScoreSet`, optional baseline, per-dimension deltas, `evidence_rows`, and `evaluation_run_ids`. Implementations: `FakeEvalProvider` (`eval/fake.py`, constant canned 0.94/0.88) and `MLflowEvalProvider` (`eval/mlflow_runner.py`, wrapping `mlflow.genai.evaluate` with Correctness, Guidelines, RelevanceToQuery, Safety by default).
- Default is **simulated**: `config.py` `eval_provider` defaults to `fake`, `llm_provider` to `fake`; `describe_provider()` stamps `{provider, simulated}` into stored results so a simulation cannot be read later as a measurement. This is an unusually honest design choice and a trust prerequisite for earned autonomy.
- `eval/scorecard.py`: deterministic scorers (`exact_match`, `token_f1`, `contains_expected`, `non_empty`) plus `Judge.<id>` custom LLM judges via `eval/judge_scorer.py` (`mlflow.genai.make_judge`). Scores in [0,1]; row passes at mean >= threshold (default 0.5).
- `eval/tool_use_scorer.py`: checks `expected_tool in tool_calls` from the predict-fn output, explicitly **not** inspecting real trace spans. This is the closest thing to a trajectory scorer and it is an output-shape check.
- `eval/alignment.py`: observed agreement and Cohen's kappa of a judge against human labels, surfaced by `routes/judges.py` (`POST /judges/{id}/alignment`). Judge trustworthiness measurement exists, but it is **on-demand and not consumed by any gate**: no code requires a judge to meet a kappa floor before it can gate a release.
- Missing relative to the autonomy-evidence dimensions: no scorers for security (injection resistance, exfiltration), policy compliance, calibration of confidence, consistency across repeated runs (variance), or exception handling. `workflows/guardrails.py` has runtime checks (`pii_detection`, `toxicity_check`, `llm_jailbreak`, `llm_groundedness`, `budget_limit`, `schema_validation`, `tool_required_before_claim`) but these are **design-time configured blocking checks inside a workflow**, not measured and recorded as evidence over time.

### 2.2 Datasets and evidence (Supported, strongest area)

- `CaliberEvalDataset` / `CaliberEvalDatasetExample`: versioned, append-only examples with weights and tags; `eval/dataset_sync.py` pushes to MLflow's dataset registry (Postgres stays authoritative). `routes/eval_datasets.py` offers `examples/from-trace` so a **human** can promote a trace into an example.
- `eval/evidence.py` (schema v1) records dataset digest vs content digest (separating "same data?" from "same result?"), pre-truncation sampling facts, per-scorer denominators, tag slices, resolved subject/judge/model identity, latency. `build_release_gate_evidence` (`workspace-release-gate-evidence/v2`) includes full rows, digests, plan/input digests, deltas and gate result, and the digest is recomputable without a provider.
- This is better than most agent platforms' eval records and is the correct substrate for evidence-of-autonomy. Its limits: slices are only dataset tags, "critical slice" semantics do not exist (planned in M09.1b), and judge definitions are mutable rows (`CaliberJudge`, unique by name, no version column; M09.2a plans to fix).

### 2.3 Gates (Partially supported; heterogeneous)

`eval/gate.py::apply_gate` is a pure function: overall >= `min_aggregate_score` (default 0.85) and no per-dimension delta below `-max_regression_delta` (default 0.02) when a baseline exists; non-finite scores fail closed. Thresholds live in `CaliberAgentConfig.eval_thresholds` (JSON, per "agent" i.e. per managed application).

`artifact_capabilities.py` (machine-readable per-family table, checked for self-consistency at import) is the honest map of gating:

| Family | live target | rollback mechanism | gate_mode |
|---|---|---|---|
| prompt | alias | alias_restore | `enforced_refinement_advisory_direct` |
| workflow | deployment_alias | checkpoint_stack_pop | `enforced_deployment_gate` |
| knowledge_base | active_version_id | derived_from_activation_history | `none` |
| skill | current_record | snapshot_restored_as_new_version | `enforced_refinement_only` |
| tool | none | none | `none` |
| test_set | none | none | `evidence_asset` |
| mcp_server | managed_definition | none | `workflow_preflight` |
| judge | none | none | `scoring_asset` |
| agent (config) | enabled_flag | none | `not_applicable` |

Verified specifics:
- **Prompt, refinement path**: `orchestrator/eval_stage.py::run_eval` applies the gate; pass -> `candidate_ready` (never auto-applies), fail -> self-correction retry up to `refinement_max_iterations` (default 0) then `rejected` plus a `CaliberReworkTask` (`failure_kind` `machine_gate` / `iterations_exhausted`). A `CaliberRegressionRun` records the replay with `required_for_approval=True`, but a grep shows that column is **never read** by Apply; the gate is enforced indirectly because only the eval stage can set `candidate_ready`.
- **Prompt, direct route**: `routes/prompts.py::set_prompt_alias` -> `_extract_gate_details` docstring: "The eval gate is *advisory* in v1 — there is no rotation-boundary enforcement"; `gate_state` and `gate_score` are read **from the request body**. An operator can move `@prod` with a self-declared `pass`, `none` or `fail`. `gate_verdicts.py` calls itself "advisory".
- **Workflow**: `workflows/deploy_gate.py` replays the manifest over a bounded sample with deterministic scorers, latency, tokens and error rate; unknown threshold keys **fail closed** (a good pattern). `config.release_require_quality_gate_for_environment_classes` defaults to `production`, `release_require_graded_executor_for_environment_classes` defaults to `production` (a fake-executor verdict cannot gate prod), `release_require_human_approval_for_environment_classes` defaults to empty. This is the closest existing analogue of "required evidence depends on class of target" and is a ready template for "required evidence depends on autonomy level".
- **Workspace release (the newest, most rigorous path)**: `workspace_release_service.py` state machine (`draft -> evaluating -> blocked|rejected|awaiting_quality_signoff|awaiting_approval|approved`), `workspace_release_evaluation_plan.py` (digest-bound plan from immutable revision pins, re-verified before dispatch), `orchestrator/workspace_release_evaluator.py` (owner-only fenced worker), `workspace_release_governance.py` (quality and release decisions, `CaliberWorkspaceBreakGlassAuthorization` with max 1h TTL). **But**: gated by `CALIBER_FEATURE_RELEASE_CANDIDATES` (stability `record_only`, off by default) and `workspace_release_evaluator_enabled` (default False). The evaluator's own docstring says gate-evidence rows and verdict records "arrive in B10.3", durable attempt history in B10.4. `map-gap.md` G08: "Scaffold can be mistaken for completed evaluation integration."
- **Human quality review** `routes/quality_reviews.py`: "go" is purely advisory; "no_go" terminally rejects and creates a rework task. Docstring: no separation-of-duty check on this route.

Assessment: the **data structures and discipline** (digests, fail-closed thresholds, class-keyed requirements, simulated provenance) are good. The **enforcement is per-family and partly advisory**, so evaluation cannot yet be a uniform authority input.

---

## 3. Can evaluation gate behaviour at runtime (not only release)?

**No, with two narrow exceptions that are not evidence-driven.**

| Mechanism | Runtime effect | Driven by measured evidence? |
|---|---|---|
| `workflows/guardrails.py` blocking checks (`GuardrailBlockedError`) | blocks an individual agent output | No: author-configured static checks |
| `config.reviewer_min_confidence` in Aria `_review_draft` | converts an agent "approve" into "reject" if self-reported confidence is low | No: single-sample, model-self-reported confidence; not a calibrated or historical metric |
| `run_pins.verify_run_pins` | refuses to run if pinned versions drifted | No: integrity check, not performance |
| `CaliberAgentConfig.enabled` (flip needs admin scope) | refinement worker stops claiming jobs (`orchestrator/worker.py` joins `enabled=True`) | No: manual; does not affect runtime workflow execution |
| `circuit_breaker.py` (`llm/`) | provider-availability circuit breaker | Operational, not quality |
| Preview `TokenBudget` | caps preview cost | Cost only, preview-only; durable budgets are B08.4 [plan] |

Nothing reads a score, an incident, a rollback count or a rejection rate and then changes what an agent, workflow or role may do. The SLO incident system (`observability/incidents.py`) publishes `slo.incident.opened/resolved` onto the event bus for webhooks; **no internal subscriber demotes anything**.

**Shadow / canary / progressive delivery**: absent. `grep` for canary hits only a comment in `deployment_environments.py` that an unknown alias named `canary` is classified as production (fail closed). `CaliberWorkflowDeployment` holds exactly one `version_id` per `(workflow_id, alias)`; no weights, no cohorts, no mirrored execution, no comparison of live vs candidate.

**Drift / regression monitoring**: absent for live traffic. The only drift concept is pin drift (`run_pins`, release evaluation-plan re-verification) and the planned M09.3 `detect_drift` that compares candidate vs baseline bundle on a frozen suite at release time.

**Error budgets**: `observability/slo.py` computes `error_budget_remaining` and `burn_rate`, but only for the single ratio signal `workflow_success_rate`, **globally across all runs** (`collect_signals` selects every terminal `CaliberWorkflowRun` in a window with no workflow, version, alias or principal filter). The module states this honestly: "not per-workflow or per-agent SLOs". It also measures "run completed", which is not task correctness. Objectives are env-configured (`CALIBER_SLO_OBJECTIVES`). Incident rows (`CaliberIncident`) are keyed by objective string only.

**Incident-triggered demotion**: absent. ADR 0004 invariant 6 and M27.4b specify it [plan].

---

## 4. Is there a per-agent, per-role, per-workflow track record?

**No.** What exists and what is merely adjacent:

- `eval/scorecard.py` is a per-example scoring engine for ad-hoc dataset runs; its "scorecard" is the result of one run, not a longitudinal record. `CaliberWorkflowBenchmarkReport` is a manual bake-off worksheet.
- `CaliberWorkflowRun` stores `workflow_version_id`, `deployment_alias`, `created_by`, `trace_id`, status, timing, so **the raw rows needed to compute per-version and per-alias run statistics exist**, but no aggregator, view or API computes them beyond platform totals (`routes/dashboard.py`, `slo.py`).
- Prometheus (`observability/metrics.py`) has `agent_id`-labelled counters (`caliber_jobs_total`, `caliber_approvals_total`, `caliber_promotions_total`, `caliber_rollbacks_total`) and a stage histogram. These count **refinement-pipeline events for a managed application**, not outcomes of any acting principal. They are process-local counters (reset on restart), not a durable track record.
- `CaliberAssistantReview` (append-only, with `reviewer_kind="agent"`, `model`, `confidence`, `candidate_hash`) records every agent review decision. This is a **genuine decision log for an agent holding a role** and the closest existing seed of a track record. Nothing joins it to later outcomes (was an agent-approved publish later rolled back, edited, or flagged?). `routes/dashboard.py::_compute_assistant_slo` summarises Aria plan readiness, clarification rate, execution completed/failed and publish success/failed, i.e. mechanical success, not correctness.
- `CaliberRegressionRun`, `CaliberRollbackCheckpoint`, `CaliberReworkTask`, `CaliberQualityReview`, `CaliberAuditLog` together contain the *events* a demotion metric would use (rollbacks of applied candidates, human no-go after machine pass, rework tasks), but none is linked to a principal that "owned" the decision, because no principal type exists.

The central structural blocker: **there is no entity to attach a track record to.** The principals in code are `Subject` (a user and auth method) and `Workload` (a workflow version digest) in `policy/principal.py`, plus the server-created `ServicePrincipal` for cron/service-token ingress. There is no `agent` principal, no role-occupancy record ("who or what held the reviewer chair for decision X"), and no `principal_type`. `grep principal_type` over `caliber/src`, the UI and the SDK returns nothing; it appears only in ADR 0004, the EGI plan and the approval UX contract.

---

## 5. Refinement loop: what is automated, who approves

Source: `docs/refinement-loop.md` (explicit alpha note), `orchestrator/` (`triage.py`, `diagnosis.py`, `optimizer_select.py`, `candidate.py`, `eval_stage.py`, `worker.py`), `apply.py`, `routes/jobs.py::apply_job`, `workflows/refinement.py`, `workflows/calibration.py`.

| Stage | Automated? | Human? | Verified note |
|---|---|---|---|
| Observe production traces | **No** | n/a | no poller module exists (`mlflow_client.py` docstrings mention one; grep finds no implementation); `post_feedback` docstring: "no poller reads assessments back" |
| Flag / verify | Partly | Yes | canonical prompt/skill/workflow paths create an already-verified item in the same step that starts the job, so stage 1 is not an independent human check on those paths |
| Triage, diagnosis | Yes (LLM, heuristics) | no | `triage._classify`, `diagnosis` |
| Optimizer selection | Yes | override possible | `optimizer_select.py`: explicit pin > GEPA (confidence/multi-objective) > opt-in DSPy few-shot > SkillMetaPrompt > MetaPrompt; DSPyMIPRO explicit-only; "TextGrad, MultiAgentCoord, MemAlign, PromptDistill remain roadmap taxonomy" |
| Candidate + eval + gate | Yes | no | pass gives `candidate_ready`; fail gives bounded self-correction (default 0 iterations) then rejection and rework task |
| Apply (promotion) | **No** | **Operator** (`SCOPE_OPERATOR` + project `release.apply` role) | `apply_job` mints a born-approved `CaliberApprovalRequest` as a rollback anchor; two-person rule (`apply_distinct_approver_required`) is on only in the reference profile or by flag; otherwise one person can request and apply |
| Rollback | No | Yes | `routes/rollback.py`, manual |

What this implies for the vision:
- The loop is a **human-gated candidate generator with a machine quality filter**. That is the correct posture for L1/L2 and a necessary precondition for earned autonomy, because every decision already produces a durable evidence row.
- There is **no autonomy dial on the loop**: nothing allows "apply automatically if gate passes and the artifact class is low risk and the owner's policy permits and the track record is clean". Aria's `agent_review`/`full_autonomy` is a separate, parallel path for assistant drafts and not the refinement loop.
- The loop has no **negative feedback**: if an applied candidate degrades production, nothing correlates the degradation back to the job, the optimizer choice or the gate thresholds, so the system cannot learn that its own gate or optimizer was miscalibrated. Calibration of the gate itself (is 0.85/0.02 a sound threshold for this agent?) is invisible.
- The loop starts at stage 2 in the alpha (stated by the docs themselves). The "Observe -> Evaluate" half of "Build -> Observe -> Evaluate -> Validate -> Approve -> Increase Autonomy -> Monitor" is therefore absent; only Build, Evaluate (offline) and Approve exist.
- Workflow refinement (`workflows/refinement.py`, `calibration.py`) is deterministic and provider-free by design: localize failure to nodes/edges/tools, generate semantic patch ops (max 2 per candidate, `_MAX_CANDIDATE_OPS`), replay and score, apply deploy-gate thresholds. This is a sound, reviewable micro-change discipline and a differentiator for workflows-as-graphs; it does not extend to agent behaviour inside a node beyond prompt/guardrail/tool-contract/edge components.

---

## 6. Observability, state and auditability

### 6.1 Observability (Partial, strong provenance, weak behavioural telemetry)

- **Tracing**: `observability/mlflow_tracing.py` is a guarded no-op-safe shared tracer with PII redaction (`audit.get_redactor`) and byte caps; `observability/trace.py` binds trace ids; `CaliberWorkflowRun.trace_id` links runs to traces. Cost attribution uses a small static `DEFAULT_MODEL_PRICING` table with DB overrides (`caliber_llm_model_pricing`).
- **Causal linkage (B11.3a)**: `observability/span_attributes.py` allows exactly four keys (`caliber.proposal_id`, `decision_id`, `approval_id`, `effect_id`), bounded identifier regex, refusal code without exception message, and strips these from manifest-supplied tag maps. The read-back that turns those attributes into a causal chain (B11.3b) is **not implemented**; only writers exist in `workflows/runtime.py`.
- **Trace access**: `observability/trace_access.py` plus `experiment_registry.py` (exclusive experiment ownership, quarantine, trusted first claim, B11.registry, #665). `map-gap.md` §4 still records a cross-project alias bypass; B11.identity (#658, #940) later landed canonical-id resolution, so that finding may be superseded. I did not re-run the exploit.
- **Run histories**: `CaliberWorkflowRun`, `CaliberWorkflowRunEvent`, `CaliberWorkflowRunCheckpoint`, `CaliberRunManifest`, `CaliberContextManifest`, `CaliberEffectProposal/Receipt`, `CaliberPolicyDecision`. Rich, append-oriented, version-pinned. (`run_manifest.py`: resolved, never floating pins; manifest digest excludes itself. `workflows/run_pins.py::verify_run_pins` fails a run closed on pin drift, naming what is still live at run time: other model settings, MCP addresses, secret values, dataset file contents.)
- **Health**: `queue_health.py`, `worker_registry.py` (heartbeats), `readiness.py`, `slo.py`, `incidents.py` (open/resolve/silence stored on the row, DB-arbitrated, published via event bus). Platform-operational, not agent-behavioural.
- **What an autonomy platform additionally needs and lacks**: per-principal outcome time series; decision-to-outcome join (approval -> effect -> acceptance -> later incident/rollback); override/intervention rate and latency; confidence calibration; per-slice drift; cost and latency per principal-role; and an evidence-aggregation service that is not process-local counters.

### 6.2 State and memory (Partial)

- **Knowledge bases** (`knowledge/`): versioned builds (`CaliberKnowledgeBaseVersion`), hybrid retrieval, pgvector, optional AGE graph. `context/sources.py` + `CaliberApprovedContextSource`: a project approves exact `(kb, version)` pairs; revocation keeps the row; a run loads nothing unapproved.
- **Context manifest** (`context/manifest.py`, B07.3a): ordered segments with trust tier (`instruction` vs `data`, retrieved text is always data), source, digest, authorization decision id, omissions/redactions, token allocation; unique per `(run, step, revision)`; read-back re-validates. This is a **real and unusual** control for prompt-injection-aware context provenance and a differentiator.
- **Memory** (`memory/service.py`): mem0-backed, optional `[memory]` extra, `CALIBER_MEMORY_ENABLED` default off. `map-gap.md` G15 and the repo's own route comment acknowledge free-form partitions are readable and unowned; consent/purpose/TTL/deletion propagation is M20 [plan]. `workflows/session_memory.py` / `CaliberWorkflowSessionMemory` exist for workflow sessions.
- **Object store / storage backend**: `StorageBackend` protocol (local/S3 style), `artifact_store.py`, `CaliberWorkflowFile`/`CaliberEvalDatasetFile`. Replay-oriented recording of model/read observations (B09.3) is [plan].
- **Replay**: the word "replay" in code means (a) idempotent re-delivery, (b) deploy-gate replay of a manifest over dataset examples with a fake or real executor, (c) regression "replay" bookkeeping. Deterministic **historical replay of a recorded run from stored observations** and **counterfactual fork** (B09.4/B09.5, M09.1a) do not exist.

Impact on autonomy: replay of recorded runs against a candidate is the standard way to turn historical decisions into evidence without live risk. Its absence means evidence for a candidate agent can only be synthetic (dataset) or live.

### 6.3 Auditability

`audit.record(...)` is called at essentially every mutation (`CaliberAuditLog`); Apply, rollback, promotion, release decisions, review decisions, rework creation, incident transitions all write rows with actor and details. Tamper-evident archive, evidence-package schema and server-free verifier are B14 [plan] (#818, #826, #833, #840, #845). The audit log today is a database table; its actor is a string user id, so it cannot distinguish a human decision from an agent-held-role decision except in `CaliberAssistantReview.reviewer_kind` and by convention of reviewer/release service identities (`assistant_reviewer_user`, `assistant_release_user`).

---

## 7. Human-agent collaboration and multi-agent coordination

### 7.1 Aria (assistant): the only place agents already hold human roles

`assistant/models.py` defines `AssistantApprovalMode`: `manual`, `auto_safe`, `auto_all` (legacy), `agent_review`, `full_autonomy`. `assistant/service.py::_auto_advance_drafts`: validate -> test -> (agent_review/full_autonomy) `_review_draft` -> (full_autonomy only) `_publish_reviewed_draft` through a separate release identity. `_autonomy_configuration_error` enforces: reviewer configured, distinct from author, carries `caliber.approver`; release identity distinct from author and reviewer, carries `caliber.operator`. `assistant/reviewer.py::EngineDraftReviewer` runs an isolated turn with no author history and no mutation tools, strict JSON schema, fail-closed on invalid output. Review decisions are bound to a `candidate_hash`; `expires_at` set from `review_ttl_seconds`; a draft changed in flight becomes `error`.

Strengths: independent identity, snapshot-bound decision, fail-closed, append-only record (`CaliberAssistantReview`).

Gaps against "autonomy is earned":
- The mode is a **session-level setting chosen by the user**, not a per-use-case level bounded by risk tier and evidence. (ADR 0004 §"What this is not" says M27 will make it read the use case's level; [plan].)
- Independence is **identity-only**: no check that the reviewer's model or prompt differs from the author's (ADR 0004 invariant 2 requires at least one behaviour-pin difference [plan]). Same model family reviewing its own output is a correlated-failure risk the code does not detect.
- The approval signal is the LLM's **self-reported `confidence`** compared with `reviewer_min_confidence`. Self-reported confidence is poorly calibrated; no calibration measurement exists.
- No deterministic acceptance check sits in the chain (no B09 acceptance predicate); tests are the draft test-run only.
- No outcome feedback: nothing joins an agent-approved publish to later rollback or edit.
- `CaliberAriaPlan.autonomy` (`ask_each`/`approve_plan`/`auto_guarded`, `assistant/plans.py::set_autonomy`, audited) is another **user-controlled autonomy dial** at plan level, independent of the approval mode and of any evidence. Two overlapping, unevidenced dials already exist.
- Tiering exists: capability registry risk tiers `read/safe/mutate/gated` (`assistant/capabilities.py`); `gated` (publish/promote/deploy/spend) is "NEVER auto-executed in a synchronous turn". This is the nearest thing to an effect-class ceiling, enforced for Aria only.

### 7.2 Workflow runtime approvals (S for human gate; P for the agent side)

`workflows/approval_policy.py`, `approval_requests.py`, `effects/step_proposal.py` (B08.2, newest commit 82f41d1b8): a governed agent step's protected action becomes an approvable `proposal`; approvals have required role, quorum, distinct identity, expiry, supersession, intent hash. `docs/architecture/approval-ux-contract.md` defines requested_by_type human/agent **[plan: the field source `principal_type` does not exist in code]**. The approval-decision machinery is mature and is the right chokepoint to hang an autonomy check on (M27.3b: `check_chain` before consuming approval [plan]).

### 7.3 Review, QA and change-request flows

`CaliberReviewQueue`/`CaliberReviewItem` (+ `review/writeback.py`): curated human review with a "provenance-bearing alignment export" feeding judge alignment, a genuine human-in-the-loop evaluation instrument. `CaliberQualityReview` (human go/no-go), `CaliberReworkTask` (claim/resolve/reassign, no SLA or escalation), `CaliberWorkspaceChangeRequest*` (reviewers, checks, external review attestations, `workspace_source_control.py` `required_approvals`), `CaliberWorkspaceBreakGlassAuthorization`. These are human-centric records; none has an actor-type column.

### 7.4 Multi-agent coordination

- Within a workflow: agent nodes with `handoffs` compile to OpenAI Agents SDK (`workflows/compiler.py` `IRHandoff`, `handoff()` helpers; `schemas.py` lists a `reroute_handoff` repair kind; `context/assembly.py` applies context assembly to every handoff target). Governed execution via `AgentStepAdapter` (`workflows/agent_step.py`) treats adapters as untrusted proposers; four provider adapters were still refusing governed execution with `adapter_not_integrated` as of this commit per its docstring (B08 in progress).
- Calibration jobs can target multi-component bundles (`bundle_targets`, `bundle_expansion_count`; the optimizer name "MultiAgentCoord" is roadmap taxonomy only).
- There is **no inter-agent trust or delegation model**: no notion of one agent granting a bounded sub-capability to another, no per-agent capability grants (the grant model `policy/grants.py` binds a *run* to a subject/workload/action set, not an agent role), and no evaluation of coordination quality (handoff correctness is only a check in tool-use style scorers and a repair kind).

### 7.5 Exception handling and escalation

Present: effect ambiguity handling (`effects/reconcile.py`, `orchestrator/effect_reconciler.py`), janitor reaping stale jobs and stranded `applying` jobs, rework tasks on rejection, approval expiry, SLO incident open/ack/silence, worker heartbeats. Absent: approval SLA timers and automatic escalation, reassignment on timeout (M06 [plan]), "exception-supervised" routing (agent proceeds, human sees only exceptions), and any link from exception rate to autonomy.

---

## 8. Evidence for autonomy: what exists and what the data model would need

### 8.1 Existing building blocks (reusable)

| Block | Where | Reuse as |
|---|---|---|
| `GateDecision` (`passed`, `reasons`, `thresholds`) | `eval/gate.py` | the decision primitive; M09.1b extends with trajectory/critical slice |
| Digest-bound evidence block | `eval/evidence.py` | immutable evidence payload with recomputable digest |
| `CaliberWorkspaceReleaseEvidence` | `db/models.py` | pattern: `(release, kind, evidence_ref, evidence_sha256, required)`; **closed CHECK on 5 kinds** (`evaluation_run`, `gate_verdict`, `release_candidate`, `config_snapshot`, `provider_preflight`) |
| Per-family capability table | `artifact_capabilities.py` | model for declaring, per use-case/role, which evidence is required (it already carries `gate_mode`, `evidence_bearing`) |
| Class-keyed requirements | `deployment_environments.py` + `config.release_require_*_for_environment_classes` | model for level-keyed requirements |
| Run/context manifests | `run_manifest.py`, `context/manifest.py` | behaviour pins per decision (what ADR 0004 calls an agent's "pins") |
| Append-only decision records | `CaliberAssistantReview`, `CaliberWorkspaceReleaseDecision`, `CaliberQualityReview` | decision log rows carrying principal kind, model, rationale, hash |
| Incident records | `CaliberIncident` | demotion trigger source (needs a scope key beyond `objective`) |
| `CaliberAuditLog` | `audit.py` | durable events, actor string only |

### 8.2 What must be added (concept level; none of it exists)

A minimal data model that can carry "evidence for autonomy level X at scope S":

1. **Principal and role-occupancy**: `principal(principal_id, type human|agent|service, pins{model_route, prompt/skill digest, adapter_version}, status)` and `role_assignment(use_case_id, role, principal_id, level_at_time, effective_from/to)`. Every decision/approval/review row gains `principal_id` and `principal_type` (planned as M27.1).
2. **Scope unit**: a "use case" or "responsibility" record (ADR 0004 calls it the GovernanceRecord, M24) with `risk_tier`, `max_level`, `current_level`, `owner`, effect-class ceilings, and demotion thresholds. This does not exist; `CaliberAgentConfig` is the nearest row and is the wrong unit (it is a refinement target, one per MLflow experiment, `experiment_id` unique).
3. **Observation ledger**: per decision or run, an immutable record of `(scope, role, principal, pins, input digest, outcome, acceptance_result, human_override?, latency, cost, policy_decisions, exceptions)`. Today these facts are scattered across run, approval, effect, review and audit rows with no common key.
4. **Metric windows**: materialised per-scope aggregates over the dimensions in the charter (correctness via acceptance/held-out slice, reliability via completion/error, consistency via repeat-run variance, policy compliance via policy-decision refusals, security via adversarial suite results, operational stability via incident rate, performance via latency/cost, traceability via evidence-package completeness, exception handling via escalation resolution, risk tolerance via effect class distribution).
5. **Level-change records**: `autonomy_transition(scope, from, to, kind promotion|demotion|revocation, evidence_window, evidence_digest, decided_by, trigger_incident_ids)` recorded "like any release" with digest and rollback (ADR 0004 invariant 5, M27.4a). One recorded demotion per qualifying event set (M27.4b).
6. **Evidence-kind registry that is open**, not a closed CHECK of five kinds: runtime-outcome, acceptance, trajectory, drift, incident, human-override, red-team.

Note how ADR 0004 puts the level on the GovernanceRecord and promises "no second level store". That is consistent with the above, but the GovernanceRecord is itself unbuilt (M24, D10), so **the unit of autonomy is currently undefined in code**.

---

## 9. Critical evaluation (including of the vision)

### 9.1 Where Caliber is differentiated vs commodity

Compared against `docs/competitive-analysis.md` (a self-assessment dated 2026-10-23, mixed with positioning; note the doc's own alpha caveat says the loop starts from a prompt, not production feedback) and general knowledge of the field (LangSmith, Langfuse, Phoenix, Braintrust, MLflow GenAI, Bedrock AgentCore, Vertex, Azure Foundry; verify current feature sets before quoting externally):

| Area | Verdict | Reasoning |
|---|---|---|
| Scorers, LLM judges, dataset management, prompt registry/aliases, tracing, GEPA/DSPy-style prompt optimizers | **Commodity** (largely MLflow's) | competitive-analysis itself says MLflow GenAI has optimizers and CI gating; Phoenix has OSS prompt learning |
| Online evaluation on production traces, annotation queues, alerting | **Commodity elsewhere, missing here** | LangSmith, Langfuse, Phoenix, Braintrust and MLflow ship online evaluators/automation rules to my knowledge; Caliber has none |
| Digest-bound, recomputable release evidence; simulated-provenance stamping; fail-closed thresholds | **Differentiating (discipline), small** | rare rigor; easy to copy conceptually |
| Intent-first release with exact recorded outgoing/target versions and reconciliation | **Differentiating** | most registries just move an alias |
| Run manifest with resolved pins plus run-time pin verification; context manifest with trust tiers and authorization decision ids | **Differentiating** (once B07/B08 land) | few platforms bind a run to immutable pins and label retrieved text as data in a stored record |
| Exact-intent approvals (intent hash, expected resource version, expiry, supersession, single consumption) | **Differentiating** (partly [plan]) | cloud IAM/step functions give pieces; none bind approval to effect intent end to end |
| Per-family capability declaration with checked rollback mechanism | **Differentiating as honesty**, but also evidence of non-uniform lifecycle |
| Evidence-governed autonomy ladder with automatic demotion | **Differentiating if shipped; entirely [plan] today** | hyperscalers offer agent identity, policy and online evals as separate parts; a closed loop from evidence to authority per use case is not a standard product feature to my knowledge |
| Aria (embedded copilot with plans and reviewer agent) | **Commodity-adjacent** | many platforms have copilots; the independent reviewer-identity design is a good small idea |

### 9.2 Long-term constraints and misfits

1. **The unit of "agent" is wrong for autonomy.** `CaliberAgentConfig` = refinement target keyed by a unique `experiment_id`, owner string, thresholds, `collaboration_mode` (a stored field that **no code reads**: grep shows it only in schemas and the create/update route) and `required_approvals` (read only to seed 0 in `prompt_targets.py`/`skill_targets.py`; the real approval counts live in manifests and `workspace_source_control`). Reusing this row as the autonomy scope will inherit its meaning confusion. A separate principal and scope model is needed; naming must change (role-holder vs managed application).
2. **Gate fragmentation.** Six gate modes across nine families, some advisory by deliberate v1 choice. Autonomy promotion needs one authorization policy that every promotion path consumes; M09.1b says "Every promotion/apply caller consumes this same result", which is correct but implies rewriting the prompt direct route, skill path, KB activation and workspace release path.
3. **Closed vocabularies in schema.** `CaliberWorkspaceReleaseEvidence.kind` CHECK (5 values); `ck_workspace_release_decision_kind` (`quality`,`release`); fixed four environments (`dev/qa/staging/prod`, no create/rename/delete "in the MVP"); `ENVIRONMENT_CLASSES` four values. Pragmatic for a bounded MVP, but each is a migration to extend, and autonomy evidence kinds and role kinds will grow.
4. **Platform scopes vs project roles vs decision-chain roles.** `auth.py` (viewer/operator/approver/admin), `resource_access.py` (owner/editor/reviewer/viewer) and the ADR 0004 chain (editor/reviewer/approver/owner) are three overlapping vocabularies; "approver" appears in both platform scope and chain role. ADR 0004 says permissions do not change with level; only the principal type does. That is clean, but it means evidence has to be attached at the **chain decision** level, a join across scope, project role and use-case role that nothing records today.
5. **Platform-global observability keys.** `CaliberIncident` unique open row per `objective` string; SLO signals global; Prometheus counters process-local and labelled by `agent_id` (refinement target). Per-principal-per-scope metrics need a new keying; retrofitting onto global objective strings is awkward.
6. **MLflow coupling.** Prompt versions/aliases, experiment identity, trace storage, assessments and `genai.evaluate` live in MLflow; evidence reproducibility depends on an external server whose runs "may outlive a crashed worker and be orphaned" (comment in `eval/provider.py`). Acceptable, but evidence-of-record must stay in Caliber's DB (the workspace-release path already does this by storing rows and digests locally).
7. **Single-tenant, single-active-process posture.** PRD non-goals exclude HA, multi-worker; ownership via execution-owner lease (ADR 0002). Evidence aggregation and demotion loops run in the owner process; throughput and failover for a fleet of autonomous principals is not designed.
8. **Evaluation default is simulated.** Fine for alpha, but any autonomy claim is vacuous until real-provider evidence exists; `release_require_graded_executor_*` partly covers this for workflow prod promotion only.
9. **Synchronous SQLAlchemy and in-process loops** (map-gap §4): computing windowed metrics across decisions at scale will need separate read paths.
10. **Self-referential evaluation risk**: judges are LLMs configured by the same operators, mutable and unversioned, with alignment measured on demand and never required. An evidence-driven autonomy system multiplies the consequence of a bad judge; a judge qualification gate (kappa floor on a held-out human-labelled set, versioned) is a prerequisite.

### 9.3 Challenging the vision (from this half's evidence)

- **Role-level autonomy is only as good as the ability to attribute outcomes to the role.** In multi-step chains (editor agent -> reviewer agent -> approver) outcomes are produced by the chain, not by a single role-holder. Attributing a bad outcome to the reviewer vs the editor requires counterfactual replay (B09.5) or at least decision-level ground truth. ADR 0004 demotes the *use case's level* on chain-level metrics, which is the more defensible abstraction than per-role scores. Per-role track records risk false precision.
- **Statistical power.** M27.4a defaults (30 days, 200 decisions) are a pragmatic floor. For rare, high-severity failures (the ones that matter for L4/L5) 200 clean decisions bounds the failure rate only to roughly 1.5 percent at 95 percent confidence (rule of three); evidence cannot justify irreversible-effect autonomy by sample size alone. Risk-tier ceilings and deterministic acceptance predicates (already in ADR 0004: no predicate means capped at L2) carry the real safety weight, not the statistics. The vision's "evidence-driven" framing can overpromise.
- **Goodhart and gate gaming**: the refinement loop optimises candidates against the same dataset the gate scores (`eval_dataset_id` per agent); there is no held-out/blind split concept in `eval/gate.py` or the dataset model. A candidate that overfits a dev set passes. Evidence-for-autonomy must use sealed slices.
- **Autonomy that can be reduced must be reduceable faster than it is earned**; the ADR's asymmetry (system demotes, human promotes) is right and is a genuine design strength. Today nothing implements either direction.
- **Levels may be the wrong primary abstraction for some work**: Caliber's own gating shows the real control variable is *effect class x risk tier x reversibility*, with "level" a derived summary. The accepted ADR already makes level a ceiling by effect class at L4; an effect-class capability matrix may be more fundamental than a scalar level. Keep level as presentation, enforce on effect class.
- **Added complexity risk**: three autonomy dials already exist (Aria approval mode, Aria plan autonomy, ADR level). Unifying them (M27.3c) must precede new dials.

---

## 10. Gap list with why-it-matters

| Gap | Why it matters for earned autonomy | What is missing | Fix kind | Priority hint |
|---|---|---|---|---|
| G-E1 No principal type / role occupancy | Cannot attach evidence to an actor or say who held a chair | `principal_type`, pins, role-assignment record, decision rows carrying them | Architectural | Now (identity decisions are hard to retrofit; M27.1 is D10 but schema seams should land with B03) |
| G-E2 No unit of autonomy (use case / GovernanceRecord) | No place to store level, tier, thresholds | M24 record; keep `CaliberAgentConfig` out of this role | Architectural | Now (decide), Next (build) |
| G-E3 No observation ledger / outcome join | No track record, no demotion signal | decision-to-outcome joins; acceptance results; override/rollback linkage | Architectural + implementation | Next |
| G-E4 No online evaluation / production observation | "Observe" stage absent; no drift | scorer-on-trace sampler, feedback poller, alerting into the loop | Product + implementation | Next |
| G-E5 No acceptance predicate / validation independent of model | L3+ cannot be defended; ADR caps at L2 without it | B09.1-.2 acceptance, domain validators per use case | Architectural (contract) | Now for the contract, Next for implementations |
| G-E6 Gate fragmentation and advisory routes | Gates cannot be an authority source | single authorization policy consumed by every promotion path; remove client-supplied `gate_state` trust | Architectural + implementation | Next (small fix: server-compute gate state on direct prompt alias) |
| G-E7 No shadow/canary/traffic split | Cannot gather safe live evidence for a candidate | weighted aliases or shadow execution with comparison | Architectural | Later (needs B09 counterfactual first) |
| G-E8 No drift monitoring | Revocation trigger absent | live per-slice monitors, M09.3 extended to production | Implementation | Next/Later |
| G-E9 SLO/incident not scoped per workflow/principal | Demotion trigger cannot be targeted | scope keys on signals, incidents, Prometheus labels; durable aggregates | Implementation | Next |
| G-E10 No automatic rollback / demotion actuator | Revocation is only manual | incident subscriber that lowers level / blocks dispatch, preserving in-flight reconciliation | Implementation (after G-E1-3) | Later (M27.4b) |
| G-E11 Judge qualification not enforced | Evidence quality depends on judge quality | versioned judges (M09.2a), alignment kappa floor as gate precondition | Implementation | Next |
| G-E12 No held-out/sealed eval slices; no critical slices | Gate gaming; critical failures averaged away | dataset roles (dev/held-out/critical), M09.1b | Implementation | Next |
| G-E13 Security/policy/consistency scorers absent | Evidence dimensions missing | adversarial suites, repeat-run variance, policy-compliance metrics | Product | Next/Later |
| G-E14 Replay/counterfactual absent | Cheap evidence from history unavailable | B09.3-.5 | Roadmap (planned) | Next |
| G-E15 Memory ungoverned | Poisoned/stale memory can silently change behaviour of an "autonomous" actor | M20 governed memory; trust tier already in context manifest | Roadmap | Next before enabling memory in autonomous use |
| G-E16 Multi-agent trust/delegation absent | Cannot bound a sub-agent's authority or score coordination | capability delegation, per-agent grants, handoff evaluation | Architectural | Later; avoid in MVP |
| G-E17 Escalation/timers absent | Exception-supervised mode needs them | M06 timers, reassignment | Roadmap | Next |
| G-E18 Evidence kinds closed in schema | Autonomy evidence needs new kinds | open registry or additive migration pattern | Architectural (small) | Now: decide the pattern before B10.3/M27.5a |
| G-E19 Three overlapping autonomy dials in Aria | Confusing, ungoverned | M27.3c mapping; deprecate user-level dials as authority | Product | Next |
| G-E20 Dead/unused config fields (`collaboration_mode`, `required_approvals`, `CaliberRegressionRun.required_for_approval`) | Looks like control, enforces nothing (the repo's own named failure mode, see `slo.py`, `deploy_gate.py`) | wire or remove | Implementation | Now (cheap, prevents misreadings) |

---

## 11. Things I could not or did not verify

- I did not run the test suite or exercise the running service; all findings are from reading code, models and docs at 82f41d1b8.
- Whether the B11.identity change (#940) fully closed the trace-alias cross-project bypass reported in `map-gap.md` is unverified; I did not reproduce it.
- I did not open the React UI components or the SDK/CLI source beyond directory listings and a grep of `principal_type`; claims about UI/SDK exposure of roles and autonomy are by absence of grep hits and file names (`caliber-ui/src/pages/*`, `sdk/caliber-sdk/src/caliber_sdk/resources/*`).
- `knowledge/calibration.py` and tool/skill calibration paths were skimmed via docs, not read line by line.
- External-platform comparisons come from `docs/competitive-analysis.md` and my prior knowledge; I did not re-verify current vendor features.
- Planned tasks (B09, B10.3/B10.4, B14, M06, M09, M20, M24, M27) are described from `docs/planning/beta-mvp-execution.json` and ADRs; whether any have landed on branches beyond main is not checked beyond `git log` on main.

---

## 12. Key file index

- Evaluation: `caliber/src/caliber/eval/{provider,gate,scorecard,evidence,judge_scorer,alignment,tool_use_scorer,mlflow_runner,dataset_sync,fake}.py`
- Refinement: `caliber/src/caliber/orchestrator/{eval_stage,triage,diagnosis,optimizer_select,candidate,janitor,calibration_drain,workspace_release_evaluator}.py`, `workflows/{refinement,calibration,deploy_gate,promoter}.py`, `apply.py`, `routes/{jobs,rollback,quality_reviews,rework_tasks,gate_verdicts,prompts}.py`
- Release: `workspace_release_{service,governance,evaluation_plan}.py`, `release_operations.py`, `deployment_environments.py`, `artifact_capabilities.py`, `db/models.py` (`CaliberWorkspaceRelease*`, `CaliberAgentConfig`, `CaliberRegressionRun`, `CaliberRollbackCheckpoint`)
- Observability: `observability/{slo,incidents,metrics,span_attributes,mlflow_tracing,trace_access,experiment_registry,queue_health}.py`, `run_manifest.py`, `workflows/run_pins.py`, `context/{manifest,sources,assembly}.py`
- Collaboration: `assistant/{service,models,reviewer,plans,capabilities,executor}.py`, `workflows/{approval_policy,approval_requests,agent_step}.py`, `effects/step_proposal.py`, `policy/{principal,grants,decisions}.py`, `actor_provenance.py`
- Plans/docs: `docs/architecture/adr/0004-autonomy-levels.md`, `docs/architecture/approval-ux-contract.md`, `docs/planning/beta-mvp-execution.json` (M24, M27, M09, B09, B14 tasks), `map-gap.md` (G07, G08, G13-G16, G20), `docs/refinement-loop.md`, `docs/competitive-analysis.md`
