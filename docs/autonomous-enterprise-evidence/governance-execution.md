# R1: Governance and execution half of Caliber, as implemented on main (82f41d1b8)

Researcher scope: runtime and workflow interpreter, agent-step boundary, effect ledger, policy/grants/approvals, execution ownership, tool access, authn/authz, tenancy, security, audit, scalability. Evidence is from code, tests and docs opened by me. Paths are relative to the repo root; `caliber/src/caliber/` is abbreviated `src/`. I did not execute anything (read-only), so every "wired / not wired" claim below is from reading call sites and grepping for producers, not from running a scenario.

## 0. Headline verdict

1. The governance half is much deeper than its autonomy half. Caliber has a serious, unusually rigorous *authority-to-effect* spine: immutable run manifests, run-bound grants rechecked on every use, a typed policy decision, an intent-hash-bound effect proposal/approval/receipt chain with a seven-state machine, execution-owner lease + fencing, and a reconciler that refuses to blindly re-send. That is real, tested code, and it is the platform's differentiator.
2. That spine is only partly connected to the thing that actually runs workflows today. Three load-bearing links are absent on main: (a) nothing in production builds `RuntimePlan.governed_effects` or `RuntimePlan.governed_agent_steps`; (b) the decision route refuses any approval request bound to a proposal (`409 approval_bound_not_wired`); (c) no code dispatches an approved agent proposal. So on main the exact-intent approval chain can be exercised in tests and scenario harnesses but cannot complete end to end for an agent-proposed action in a queued production run. Docs say this openly (`docs/operate/reference-profile.md`, sections "Governed agent steps" and "Not enforced yet"); the ADR table text ("routed through the dispatcher") describes the target owner, not current behaviour.
3. Autonomy, as the vision means it, does not exist in code. ADR 0004's L0-L5, `principal_type`, `autonomy_level`, `max_autonomy_level`, `autonomy_level_exceeded`, promotion and demotion have zero occurrences in `caliber/src`. What exists instead are three unrelated, un-unified "autonomy" vocabularies (Aria session approval mode, Aria plan dial, per-tool `requires_approval`/`side_effect_level`), plus a hard-coded "risk 5 = a person, never a run".
4. A "role" is not a first-class concept in the workflow. Human vs agent responsibility is baked into node type (`agent`, `human_approval`, `review_queue_enqueue`), the human gate's `required_role` is a platform scope string, and a run's workload identity is derived from the workflow-version digest. Moving a step from human to agent today is a manifest edit, a new sealed version, a new workload identity and new grants.
5. Several controls that look like governance are off by default and not required by the reference profile. The most important: a `human_approval` node is an explicit pass-through unless `workflow_run_runtime_approvals_enabled` is true (default false, not in the reference profile's refusal list).

## 1. Status ledger (my scope)

Status vocabulary: Supported | Partially Supported | Missing | Architecturally Conflicting. "Fix kind": A architectural, P product, I implementation, R roadmap (already scheduled in docs).

| Capability | Status | One-line reason | Fix kind |
| --- | --- | --- | --- |
| Roles and responsibilities (governance side) | Partially (conflicting in one respect) | Two closed role vocabularies; role-in-workflow absent; role=node type | A |
| Humans and agents as principals | Missing | No principal type; agents are user-id strings with scopes from env lists | A/R (M27) |
| Human-agent collaboration | Partially | Human gates, review queue, Aria modes exist; no assignable role slot | A/P |
| Agent execution | Partially | Ungoverned loop is the production path; governed step contract and 4 adapters exist but unwired | I/R |
| Workflow orchestration / runtime execution | Supported (for its own model) | Typed IR, 31 node types, park/resume, loops, boundaries, subworkflows | - |
| Autonomy levels | Missing | Docs only; 3 disjoint ad hoc dials in code | A/R |
| Autonomy promotion/demotion | Missing | No metrics, no demotion, no promotion record | A/R |
| Capabilities and tool access | Partially | Registry + fail-closed inventory + trusted connector admission; MCP/OpenAPI/file/bucket paths still ungoverned; capabilities not tied to roles | I/A |
| Permissions (RBAC) | Partially | Solid project RBAC + PAT ceilings; scopes are env config; humans only | A |
| Policies and governance | Partially | Typed decision + grants good; policy is a module constant, not data | A |
| Human approval | Partially | Strong binding design; decision path unwired; pass-through default | I |
| Exception handling | Partially | Error boundary, retries, park on unknown effect, reconcile | - |
| Escalation | Missing | `escalate` is explicitly rejected or aliased to block | P/R (M06) |
| Auditability | Partially | Good control-record immutability; audit log mutable by convention; no tamper evidence; no evidence package | I/R |
| Security | Partially | Egress pinning, fail-closed trace redaction, secrets, payload-free errors; model calls bypass egress; regex PII only | I |
| Multi-agent coordination | Partially | In-run handoffs only; governed path refuses them | A |
| Multi-tenancy | Partially (project isolation) / Missing (tenant) | `tenant_id='local'` everywhere; shared tenancy out of scope by owner decision | R |
| Enterprise scalability | Partially | One execution-owner process runs all loops, one run per tick | A |
| Run budgets / limits | Missing on main | Contract exists (`StepLimits`); enforcement is in-flight work (B08.3/B08.4) | I |

## 2. Capability detail

### 2.1 Roles, principals and human/agent assignment

What exists (verified):
- Platform scopes: `SCOPE_VIEWER/OPERATOR/APPROVER/ADMIN` in `src/auth.py`, with `_SCOPE_IMPLIES`. Assignment is "config-driven: a comma-separated list of user IDs per scope" (module docstring; `CALIBER_ADMIN_USERS` etc.), so granting or revoking a platform scope is a configuration change, not a runtime record.
- Project roles: `owner/editor/reviewer/viewer` and a closed `PROJECT_ACTIONS` map (`src/resource_access.py`), rows in `caliber_project_members` (dynamic, DB-backed, `status`, `deactivated_at`). `owner` is additionally capped to `editor` unless the caller holds both operator and approver scopes (`project_role`, `OWNER_ROLE_REQUIRED_SCOPES`), which makes "project owner" depend on static platform config.
- Principals for a run: `Subject` (user + `auth_method` in {session, pat, trusted_header, service_principal}) and `Workload` (id derived from the full workflow-version digest) in `src/policy/principal.py`; `ServicePrincipal` for cron and service-token ingress (bound to tenant, project, workflow, workload, purpose, action/resource sets). It is explicitly not a promotion to autonomous approver.
- Credentials: `CaliberPersonalAccessToken` (user-owned, scope ceiling intersected with the owner's live scopes, optional project binding, rotation lineage), `CaliberServiceToken` (workflow-bound). `src/db/models.py`.

What is missing relative to "a role can be held by a human or an agent":
- No `principal_type`. ADR 0004 says "every account has a principal_type: human or agent"; `grep principal_type` over `caliber/src` returns nothing. `CaliberUserAccount` is a password account (`src/db/models.py`); an "agent" today is just a user id with a PAT or an entry in an env list. Aria's reviewer is `CALIBER_ASSISTANT_REVIEWER_USER`, a string that must also appear in `CALIBER_APPROVER_USERS`/`CALIBER_ADMIN_USERS` (`src/config.py`, `src/assistant/service.py::_autonomy_configuration_error`).
- No responsibility-level role. Project roles are permission bundles for humans (Developer=editor, QA=reviewer, Admin=owner per the comments in `resource_access.py`); nothing models "Operator", "Validator", "Supervisor of agents", "Exception handler". No place to say who evaluates an agent, who may reduce its autonomy, who is accountable for outcomes, except the single `owner`.
- Roles are closed vocabularies in code, not extensible per deployment.
- Audit and approval rows record a bare string actor. `ActorProvenance` (`src/actor_provenance.py`) carries user_id plus credential kind, not human/agent, not pins.

Design choice that bites: the grant model is "a grant never exceeds the subject's current role" (`src/policy/grants.py::create_grant`, `authorize`). All run authority is derived from a human (or a bound service principal) subject. An agent cannot hold authority of its own; it borrows the starter's, or a service principal's pre-bound action set. At runtime the workflow's inner calls (knowledge, MCP) execute as `CaliberIdentity(user_id=workflow.owner, scopes={viewer})` (`src/workflows/promoter.py::build_workflow_identity` and `build_plan`), which is a *different* identity from the grant's subject. Two identity notions coexist inside one run.

### 2.2 Runtime execution and workflow orchestration

Supported for what it is (verified in `src/workflows/ir.py`, `runtime.py`, `run_state.py`, `orchestrator/workflow_run_worker.py`):
- 31 `NodeType`s including `parallel/join`, `for_each`, `loop`, `error_boundary` (with `compensate_with`), `subworkflow` (depth-limited), `wait_until`, `wait_for_event`, `human_approval`, `agent` with handoffs, `router`, `guardrail`, `webhook`, `api_request`, `python_code`, `external_app`.
- Run states `queued/running/waiting_approval/waiting_effect/waiting_event/completed/failed/cancelled/expired` with an explicit transition table (`run_state.py`). Park/resume uses `caliber_workflow_run_checkpoints` with kinds `wait_for_event`, `wait_until`, `human_approval`, `runtime_approval`, `effect_reconciliation`.
- Queue claim by `UPDATE ... WHERE status=queued` ordered by priority; lease and heartbeat on the run row; `_recover_expired_leases` requeues from the start unless a checkpoint exists (ADR 0002 recovery table; `workflow_run_worker.py::_tick`).
- `effect_gate`: before any node runs, the runtime refuses if an effect the node or anything upstream asked for is `dispatched/unknown/reconciled` (`effects/reconcile.py::effect_gate`, hooked at `runtime.py::_check_effect_gate`). The run parks (`waiting_effect`) rather than proceeding on an effect nobody can vouch for. This is genuinely uncommon.

Limits and honest caveats:
- Compensation is `error_boundary.compensate_with`: one inline node run when the target fails. It is a handler, not saga-style reversal of committed effects by receipt. `reversible_write` is defined in ADR 0001 as "can be undone with a compensating call", but no code derives or runs a compensation from a committed proposal (grep `compensat` in `src/effects` returns nothing).
- Governed agent steps refuse handoffs, inline targets (fan-out/loop/boundary) and parallel branches (`_governed_plan_refusal`, `reference-profile.md` table). The governed model and the rich orchestration model do not compose yet.
- Preview/eval/sync-run paths use `execute()` with `runtime_approvals_enabled=False`, so a `human_approval` node returns `ok` with the detail string "approval required (pass-through in MVP)" (`runtime.py`, `isinstance(node, IRHumanApproval)` branch). On the queued path it blocks only if `config.workflow_run_runtime_approvals_enabled` (default False, `src/config.py`) and the queue and checkpointing flags are also on (both default False). `src/profile.py::reference_profile_violations` does not require any of them. A workflow author who drew a human gate gets a gate only in a deployment that flipped three flags.

### 2.3 Agent execution and the agent-step boundary

What exists (verified):
- `src/contracts/agent_step.py` (462 lines) and `docs/architecture/adr/0003-control-record-schemas.md`: `AgentStepRequest` (run/context manifest digests, grant id, `StepLimits{max_turns,max_tool_calls,max_tokens,deadline}`) and `AgentStepResult` (stop reasons, at most one `ProposedAction`, usage). The result has no `state`, `receipt` or `approval` field; unknown fields are violations; refusals never echo model text. The model's `effect_class` and `connector_version` are treated as claims and bound to the platform's by `ProposedAction.bound_to` (`ClaimMismatch`).
- `src/workflows/agent_step.py`: `AgentStepAdapter` protocol; `run_agent_step` validates the request before the adapter is touched and treats everything returned or raised as untrusted (snapshot via JSON round trip, schema parse, step identity match). Adapters get only tools the inventory classes `read`.
- Provider adapters: `OpenAIChat/Responses/Agents/AnthropicChat` each implement `run_step` in `src/workflows/runtime.py` (lines ~799, 1031, 1262, 1603) and `src/workflows/step_adapters/anthropic_chat.py`.
- `src/effects/step_proposal.py` (580 lines): classification by registry identity, connector from a producer-trusted map, `submit_proposal` + `create_approval_request` in one transaction, node parks `waiting_approval`.

What is not true on main:
- Nothing builds a `GovernedStepContext`. `grep GovernedStepContext|governed_agent_steps` outside `runtime.py` finds only the worker's `getattr` guard and tests (`caliber/tests/test_governed_agent_step.py`, `tests/egi_scenarios/test_b08_protected_action_proposal.py`). `docs/operate/reference-profile.md` states "nothing in production builds one yet". The production agent path remains the ungoverned loop (`_run_agent_with_history`, `_run_agent_traced`, executors' `run_agent`) in which a model-selected tool executes directly when its binding does not set `requires_approval`, and a `requires_approval` tool is simply *not executed* ("gated marker", `runtime.py::_execute_model_tool_call`, `_invoke_agent_tools`). In the ungoverned loop "requires approval" means "refuse", not "ask".
- Only one backend is reachable by agent steps: `AGENT_STEP_BACKENDS = {"document_request"}` (`step_proposal.py`), and the only connector with a resource pattern is `^cases/<id>/document-requests$` (`effects/dispatcher.py::_CASE_RESOURCE`). The governed agent path is proven for one scenario shape.
- A governed step that completes without proposing is refused ("no output text channel in the v1 result contract"); handoff targets are not offered to the model; structured output is not applied (`anthropic_chat.py` docstring). So governed steps cannot yet be used for ordinary agent work (answer, summarize, route), only for "propose a protected action".
- `requested_by` of the approval request is `run_initiator` (see the `GovernedStepContext` example in `reference-profile.md`), so an action an *agent* proposed is attributed in the approval record to the human who started the run. The agent principal and pins appear only indirectly (run manifest `pins.model_routes`, context manifest digest). `requested_by_type: human|agent` appears in `docs/architecture/approval-ux-contract.md` as a view field to be computed; no code computes it.
- Limits and budgets: `StepLimits` is a contract field; platform enforcement and token reservation are B08.3/B08.4 (the checked-out branch `feat/b08-3-central-step-limits` is that work in flight). On main there is no run-level token/cost budget (grep for budget shows only preview budget in `workflows/sandbox.py` and context-assembly token caps).

### 2.4 Effect ledger, dispatcher, receipts, reconciliation

Strong, verified design (this is the platform's genuine core):
- Proposal: immutable intent (`Intent`, `intent-v1` hash over connector id+version, effect class, resource, args, expected resource version), keyed by `(deployment, run, node, occurrence)`; intent columns frozen by DB trigger; seven states with compare-and-set `version` (`src/effects/records.py`, migrations 0123 and later; `db/immutable.py`).
- `submit_proposal` never calls a connector; class is the inventory's answer, never the claim (`dispatcher.py::_inventory_effect_class`, floor can only tighten). `dispatch` validates the exact approval and the captured execution-owner lease in one transaction (run -> proposal -> approval row locks), consumes the approval, writes an outbox event, then calls a private transport after commit (`_claim_dispatch`, `dispatch`). A transport that fails to settle ends `unknown` under the same lease, never retried.
- Receipt: `committed` only with a receipt verified by reading the downstream back (`effects/receipts.py`, `verified_by=query`). A claim nobody could confirm leaves the proposal `unknown`.
- Reconciler (`effects/reconcile.py`, `orchestrator/effect_reconciler.py::EffectReconcilerTask`): owner-only, lease-fenced writes, "abandoned" = dispatched under a lower fencing token, lookup by original logical key, 404 is `failed` only for a connector that proves absence final, no edge `unknown -> dispatched` ("no blind redrive"). Operator action `reconcile` cannot assert an outcome, only ask the downstream (`effects/operator_action.py`). The operator view labels legacy ledger rows apart (`effects/operator_view.py`).
- Connector admission (`effects/admission.py`, `profile.py::REFERENCE_CONNECTORS`): implementation identity, capability digest and configuration digest pinned; a name is never an identity.
- Fail-closed inventory (`effects/inventory.py`): 132 rows parsed from `docs/architecture/protected-effect-paths.md` at runtime, unknown tool -> `irreversible_write`; reserved built-in names cannot be claimed by custom tools (#641).

Gaps and nuances:
- Wiring (above): `_perform_http_effect` refuses a manifest-backed plan lacking `governed_effects` ("requires a governed effect context for manifest HTTP execution"). `build_plan` always sets `workflow_version_id` and never sets `governed_effects` (`src/workflows/promoter.py::build_plan`; the worker adds only `effect_ledger` and `effect_gate`). By reading, queued production runs containing `webhook` or `api_request` nodes, and protected registered tools, now fail closed. `caliber/tests/test_workflow_run_worker.py` (~line 5900) says so in a comment: "the worker binds no governed effect context yet". That is safe, but it is also a capability regression pending the producer (B06.4 / dispatch wiring).
- The ADR 0001/0002 tables overstate: ADR 0001 says `read` needs only a recorded policy decision, `reversible_write` needs no approval, `irreversible_write` and `export` need exact approval. The dispatcher requires an approval id for every proposal that reaches `dispatch` (`dispatch()` raises `proposal_not_approved` without one). Class-differentiated approval requirement is not implemented. Registry tools classed `read` run inline with no recorded decision (`route_registered_tool` returns `INLINE`). ADR 0002's "routed through the dispatcher" for most `dispatch` rows is target ownership; the inventory header itself counts guards as none 98 / approval 20 / egress 11 / ledger 3.
- Only three transports exist and are pinned in code (`document_request`, `http`, `registered_tool`). MCP, OpenAPI tools, file writes, bucket writes, knowledge builds are still on their old guards (inventory rows EP-007/008/009/013/030-037/047-051). Adding a connector means a code change plus new pinned digests in `profile.py`. That is a deliberate safety choice, and a platform-extensibility constraint.
- For custom (non-built-in) registry tools the effect class is whatever the tool's own registry row declares (`_DECLARED_CLASS[side_effect_level]`), i.e. author-declared, with a floor only for reserved names.
- The effect model is single-effect-per-step in v1 (`proposed_actions` max 1; ADR 0003 says a second would share the logical key).
- `unknown` outcomes block dependants permanently until a connector proves the outcome. For connectors without lookup-by-key (generic HTTP) a human must resolve it with the downstream's owner. Safe, but it is a load that scales with autonomous volume.

### 2.5 Policy and governance

Exists (verified `src/policy/decisions.py`, `grants.py`, `src/workflows/admission.py`, `run_identity.py`):
- `decide()` records an immutable allow/deny `PolicyDecision` with `input_hash` for replay, fixed reason-code order, audit row in the same savepoint; unrecorded allow is a deny. Grants are exact (no wildcard, no "newest grant"), row-locked and rechecked against the subject's current membership and role on every use; revocation and demotion narrow standing grants at once.
- Admission pins everything a run depends on (sealed bundle, prompt/tool/skill/KB versions, model routes, policy version, optional rule bundle digest) into one immutable manifest, in the run-creating transaction (`CALIBER_ADMISSION_ENABLED`, default False; always on in the reference profile).
- Apply and release governance: distinct Apply approver in the reference profile (`profile.py::apply_distinct_approver_required`), workspace release decisions with role-specific go/no-go bound to recomputed gate evidence, break-glass authorization with a one-hour TTL cap (`src/workspace_release_governance.py`, `src/apply.py`).

Limits that matter for progressive autonomy:
- The policy is a module constant: `REFERENCE_POLICY_V1` with `_ACTION_RISK` (0-5) and `risk_ceiling=4`, passed as a default argument (`workflows/admission.py`, `run_identity.py`, `context/authorize.py`). Risk 5 is documented as "a person, never a run" and covers every approve/accept action and project administration. There is no policy store, no per-use-case, per-project or per-role policy, and no way to say "this use case may let an agent approve `reversible_write`". Raising autonomy therefore means changing code and `policy_version`.
- Action risk is per action name, not per (action, effect class, risk tier, principal type). ADR 0004's ceiling model needs that grid.
- Four distinct, non-shared "distinct actor" implementations compare user-id strings: `workflows/approval_policy.py::assert_distinct_actor` (vs `run.created_by`, with an `allow_self` escape), `actor_provenance.py::require_distinct_actors`, `workspace_release_governance._require_distinct_actor` (vs a set of originators), and Aria's reviewer/release identity checks in `assistant/service.py`. Each docstring explains why they were not merged. None can express ADR 0004 invariant 2 (agent reviewer must differ from agent editor in a behaviour pin).
- Run-level governance for the agent step records `max_turns`, tokens and deadline as request fields but nothing yet makes them binding.
- Approval policy supports `required_role` (a platform scope), `approval_count` (distinct approvers), `allow_self_approval`, TTL default 24 h (`approval_ttl_seconds`), supersession and expiry (`approval_policy.py`, `approval_requests.py`). `timeout_behavior` is limited to `block`: `escalate` and `auto_reject` are rejected at manifest validation because nothing implements them.

### 2.6 Human approval

Design is strong; delivery is partial.
- Commit-time recheck (`approval_policy.py::_recheck_approval`): row locks in fixed order; binding of intent hash, evidence hash, policy digest, expected resource version; expiry and supersession; single consumption; approver authority independent of the run subject (approver scopes and project role `resource.approve` = owner|reviewer, owner narrowed without operator+approver scopes); distinct from `run.created_by`; then `identity_for_run` re-decides the *run's* grant so that a revoked grant, membership or expiry blocks dispatch even with a valid approval. `caliber/tests/egi_scenarios/test_v04_stale_approval.py`, `test_v05_revocation_while_waiting.py` cover it.
- But `routes/workflow_runs.py` answers `409 approval_bound_not_wired` for any request with `proposal_id` (`_select_pending_runtime_approval`), pinned by `tests/test_routes_workflow_runs_async.py` and `test_v04_stale_approval.py`. The legacy node-level approval path (scope + project role + quorum + self-approval rule) is what actually works, and it is not intent-bound.
- `approver_scopes` is an argument the caller of `dispatch` supplies, and the validator checks `required_role in scopes`. The scopes are not re-derived from config inside the dispatcher. The trust is in the caller (the future producer), which does not yet exist.
- Approval is addressed to a platform scope and a project role, never to a named or assigned principal; there is no reassignment, SLA timer or delegation (M06 later). Approver identity is `decided_by` string.

### 2.7 Autonomy (the central gap)

Three disjoint things exist; none is the vision's model.

| Existing mechanism | Scope | Evidence-gated? | Who can raise it |
| --- | --- | --- | --- |
| `AssistantApprovalMode` `manual/auto_safe/auto_all/agent_review/full_autonomy` (`src/assistant/models.py`, `service.py::_auto_advance_drafts`) | Aria authoring session; one deployment-wide reviewer identity and release identity from env | No; reviewer confidence threshold 0.8 and TTL are the only checks | The session user |
| `CaliberAriaPlan.autonomy` `ask_each/approve_plan/auto_guarded` and `gate_decision` by capability tier (`assistant/executor.py`, `plans.py::set_autonomy`) | One Aria plan | No; raising is an audited state change by the plan actor | The plan actor |
| Per-tool `requires_approval` / `side_effect_level`, MCP `tool_policies`, node `required_role` (`mcp_policy.py::validate_tool_access`, `ir.py`) | One tool or node, independent of who calls | No | Whoever edits the tool/manifest |

ADR 0004 (L0-L5, per use case, bounded by effect class and risk tier, automatic one-step demotion, human-promoted) is Accepted but, by the ADR's own consequences section and `map-gap.md` ("not a claim that L0-L5 are presently enforced"), scheduled for M27 (D10, after governance record M24). No GovernanceRecord, risk tier, `autonomy_level`, demotion metric or trajectory gate exists. There is no V35 scenario (egi_scenarios has V00-V14 subset only).

Direct consequences:
- No promotion record, no evidence window, no demotion trigger, no scoped stop. A grep for `kill_switch`, `quarantine` or an equivalent finds nothing in `src`. Available "stop" levers are run `cancel`, `revoke_grant` (per run), PAT revoke, tool/MCP policy edit, and disabling a deployment alias. None is scoped "all of use case X's agent-held roles".
- The existing Aria dial lets a user raise their own plan's autonomy with no independent check. That pattern contradicts ADR 0004 invariant 5 and is what M27 must replace, not extend.

### 2.8 Capabilities, tool access, permissions

- Tools: `CaliberToolRegistry` rows (name+version unique, owner, module/callable, backend, `side_effect_level`, `requires_approval`), OpenAPI integrations/operations/drafts, MCP servers with discovered-tool allowlist and mandatory explicit classification (`validate_tool_access` requires `allowed`, `side_effect_level`, `requires_approval`, optional rate limit), execution policy for stdio vs remote (`mcp_policy.py`). Identity of a tool is the registry row, not its name (#641, `workflows/builtin_tools.py::is_builtin_tool`).
- A capability is not an object that attaches to a role. Tool access is attached to (a) the agent node's `tools` list in the manifest, (b) the tool's own policy, (c) the project role of whoever starts the run. There is no "role X is allowed capabilities {A,B}, whoever holds X". Tool policies are caller-independent.
- MCP and OpenAPI tool calls do not go through the effect ledger (EP-007, 008, 030-037: current guard none or egress). OpenAPI executor calls and MCP calls from a workflow carry the workflow-owner identity.
- Aria's tools in `auto_all`/`full_autonomy` can invoke write and external-action OpenAPI tools, run workflows, run draft source in the sandbox and publish drafts (`protected-effect-paths.md` findings; `assistant/agent_tools.py`).

### 2.9 Exception handling and escalation

- Handling: node `execution_policy` (timeout up to 3600 s, `max_retries` 0-10, `idempotent` flag), `error_boundary`, guardrail `on_failure`, effect park on `unknown`, `wait_for_event` timeout expiry, lease recovery, janitor reaping.
- Escalation does not exist: `approval_policy.validate_timeout_behavior` rejects `escalate`; guardrail `on_failure="escalate"` is literally treated like `block` (`runtime.py` around line 8012, `guardrails.py` 571); `demo_tools.escalate` is a demo tool returning `{"escalated": True}`. The nearest real mechanism is `review_queue_enqueue` (writes a review item with `assigned_to` free text). No SLA, no timer, no reassignment, no "exception supervisor" target. ADR 0004 L3 ("human approver within SLA via M06 timers and escalation") depends on M06 that is not present.

### 2.10 Multi-agent coordination

In-run handoffs only (`IRAgent.handoffs`, `_collect_agent_handoff_specs`, `_resolve_agent_handoff_target`), with a visible handoff tool list, `subworkflow` nesting and bounded parallel branches. No inter-run or inter-agent protocol, no agent identity per sub-agent (all share the run's subject/workload), and the governed agent path refuses handoffs. Federation/peer exchange is plan-only (M19-M21).

### 2.11 Multi-tenancy

- Tenant: `tenant_id` columns default to `local` (`CaliberWorkflowRun`, `CaliberProject`, `CaliberEffectProposal`, `CaliberWorkflowFile`); ADR 0001: "tenant local (one tenant; multi-tenancy is out of the Beta)"; `map-gap.md`: shared tenancy out of scope by owner decision. `tenant_id` is a required field in `run_manifest` and `proposal` records, so the *contracts* are tenant-aware even if enforcement is absent.
- Project/owner/visibility: 3-tier `project|user|public` row scoping (`src/db/scoping.py`, `apply_visibility_filter`), a mechanically derived per-model inventory with a ratchet test (`src/db/resource_inventory.py`, `tests/test_resource_inventory.py`), project RBAC on routes, PAT project binding, per-project outbox aggregate, project-scoped trace payload access (`trace.payload.read`, owner/editor). Strong for single-tenant multi-project. Known gap from the gap analysis: trace read APIs were not project-scoped (B11.4 addressed payloads only; I did not re-audit every trace route).
- `public` visibility tier means "visible to everyone", which is incompatible with strong tenant isolation unless redefined per tenant.

### 2.12 Security

Verified strengths: egress policy resolves and pins the vetted IP, no redirects, deny-by-category (`src/egress.py`); reference profile refuses 8+ unsafe settings at startup (`src/profile.py`); CSRF; session-validated identity with trusted-header mode requiring a proxy secret; run manifests and policy decisions DB-immutable by trigger (`db/immutable.py`); fail-closed trace payload redaction with an allow-list of safe span attributes (`profile.py::SAFE_SPAN_ATTRIBUTES`, `payload_governance_active`); payload-free refusal messages and codes across effects/approval/agent-step; stored secrets never leave the platform (`reference-profile.md`); sandboxed registered tools default on; connector admission by implementation identity.
Gaps: model-provider calls bypass `caliber.egress` (inventory scope decision); audit PII redaction is three regexes (email, US phone, SSN) plus operator extras (`src/redaction.py`); MCP stdio is "containment, not a sandbox" by its own docstring; host-file read tools and the DB MCP write tools are `refuse` rows whose run-time refusal is #774 and listed under "Not enforced yet"; `caliber_audit_log` is "append-only by convention" with no trigger or hash chain (`db/models.py`), while the control records are trigger-immutable.

### 2.13 Auditability and observability

- Present: audit rows written in the caller's transaction (`src/audit.py::record`), policy decision rows with replay hash, run manifest and context manifest (digests of each segment, never content), outbox events for dispatch, receipts with downstream request ids, governed spans carrying only row ids (`observability/span_attributes.py`), operator effect view, worker heartbeats and `/readiness` with `execution_role`.
- Missing: the per-decision "principals with type and pins" (ADR 0004 invariant 7) and the evidence package (`src/evidence/`, `src/acceptance/` named in ADR 0002 do not exist; `orchestrator/evidence.py` is a different thing); tamper-evident audit export (#662 in plan); AcceptanceDecision (an outcome record separate from "effect committed"). So "effect committed" is provable; "outcome was correct and accepted" is not.

### 2.14 Execution ownership, state and enterprise scalability

- Ownership: one `execution-owner` lease with database-time expiry and a monotonically increasing fencing token (`src/ownership.py`, `execution_role.py`, migration 0116); `assert_fenced` row-locks on PostgreSQL; takeover verified by scenario V14. Per-row run leases and worker heartbeats underneath.
- All 14 loops (workflow runs, refinement, scheduler, outbox, reconciler, webhooks, KB builds, release evaluator...) are `owner_only` (`observability/worker_inventory.py`, ADR 0002 table). Replicas serve HTTP only. `WorkflowRunWorker._tick` claims one run and executes it synchronously in a thread (`_claim_next_run` then `_execute_run`), so one run is executed at a time per deployment, with intra-run parallelism bounded by thread pools (`foreach_max_workers`, `parallel_branch_max_workers`). I found no setting for worker concurrency or multiple workers.
- Not yet fenced per transition: "A loop mid-transition when the lease is lost finishes that transition" (ADR 0002, `execution_role.py` docstring); an old owner's last transition may overlap a new owner's first by up to the shutdown grace.
- SQLite is development only; row-lock guarantees are PostgreSQL-only. Reference profile = one tenant, one region, PostgreSQL 16, MLflow external, S3-compatible store.
- The plan acknowledges throughput work is later (M08 bulkheads, M12 measured envelope, M17 fairness).
- Memory/state: workflow session memory in-process or store, optional mem0, knowledge versions with approved-context-source authorization. Memory is out of my scope beyond noting that "memory-to-policy promotion" has no controls because neither exists.

## 3. Wired vs written: what is real on main

| Link in the authority-to-outcome chain | Code exists | Wired in production path | Evidence |
| --- | --- | --- | --- |
| Admission pins and grant | yes | yes when `admission_enabled` (default off; reference profile on) | `workflows/admission.py` |
| Policy decision per run | yes | yes (admission, resume) | `workflows/run_identity.py`, worker |
| Context manifest and source authorization | yes | yes for admitted runs | `context/*`, worker |
| Effect proposal for HTTP node | yes | **no** (`governed_effects` never set) | `runtime.py::_perform_http_effect`, `promoter.py::build_plan` |
| Effect proposal for registry tool | yes | **no** (same) | `_governed_tool_route` |
| Governed agent step | yes | **no** (`governed_agent_steps` never set) | `reference-profile.md` |
| Approval request bound to intent | yes | only inside governed step (unwired) | `approval_requests.py` |
| Approval decision for bound request | commit validator yes | **no** (route 409) | `routes/workflow_runs.py` |
| Dispatch with lease and approval | yes | only via tests and unwired producers | `effects/dispatcher.py` |
| Receipt, reconcile loop, operator action | yes | reconciler loop runs in owner; nothing creates dispatched proposals in prod | `orchestrator/effect_reconciler.py` |
| Legacy effect ledger (at-most-once for webhook/API) | yes | yes for legacy/no-version plans; refused for manifest-backed plans | `_perform_guarded_effect` |
| Human approval node | yes | only with three flags on | `runtime.py`, `config.py` |
| Escalation, scoped stop, autonomy level, promotion/demotion | no | no | greps above |
| Run budgets and step limits | contract only | no | B08.3/B08.4 pending |

## 4. Genuinely differentiated strengths

1. Model-output-as-untrusted at the contract level: the agent-step result has no field with which a model can claim an effect happened; the class and connector version are claims bound to platform truth; refusals never echo model text (`contracts/agent_step.py`, `workflows/agent_step.py`). Many platforms trust tool-call output.
2. Exact-intent approval with commit-time recheck of everything that can have changed (policy digest, resource version, expiry, supersession, approver's current role, and the *run's* grant), single consumption, all inside the same transaction as the lease fence.
3. Effects as a durable ledger with a no-blind-redrive rule and a gate that parks downstream work on `unknown` effects (`effect_gate`, `waiting_effect`). This is operationally mature thinking.
4. Owner-lease with database-time and fencing token, tested by kill-the-owner scenarios.
5. Typed, replayable policy decisions and DB-enforced immutability of control records; additive-versioned, strict JSON-schema control contracts (`additionalProperties:false`, readers-before-writers rule).
6. Fail-closed defaults repeated consistently: unclassified tool = `irreversible_write`; unknown connector refused; unpinned run refused; fail-closed trace redaction; reference profile that refuses startup.
7. Governance for the platform's own change path (Apply/release, break-glass with TTL, gate evidence recomputation) is more developed than most agent platforms' deployment controls.

## 5. Decisions that may become long-term constraints or dead ends for progressive autonomy

| Decision | Why it constrains progressive autonomy | Reversibility |
| --- | --- | --- |
| Workload identity = digest of the workflow version (`policy/principal.py::workload_id_for`); grant binds workload; policy decisions and approvals bind the grant | Any change of who performs a step, if expressed as a manifest change, mints a new workload and invalidates grants/pins. Autonomy movement becomes "release a new workflow version" rather than a runtime attribute. That might be the right *governed* answer (promotion = governed release per ADR 0004) but only if role-holder assignment lives outside the sealed manifest | Needs an architectural decision: role-holder binding as policy data referenced by, not embedded in, the sealed bundle |
| Role = node type (`agent` vs `human_approval` vs `review_queue_enqueue`) and `required_role` = platform scope string in the manifest | No indirection slot "approver role" that resolves to human or agent at admission time | Medium; IR and manifest change, plus migration of stored manifests |
| Authority of a run derives from a human subject's *current role* (`grants.py`) | An agent cannot hold authority; it can only be bound through a `ServicePrincipal` with fixed action/resource sets created by the scheduler or service route | Needs an agent-principal type with its own grant source |
| Platform scopes assigned via env-var lists | Granting/revoking an agent's approver scope = restart/redeploy; no runtime revocation record, no expiry, no audit of the grant | Moderate; scopes could move to DB without changing call sites (module docstring says so) |
| `REFERENCE_POLICY_V1` is a module constant with `risk_ceiling=4` and risk 5 = person | L4/L5 cannot be expressed without editing code; policy not per use case/tier | Moderate-high; must become versioned data with its own governed release path |
| Owner human at every level (ADR 0004) and owner = operator+approver scopes | Intentional; accountability stays human. Risk: "owner" is a single project-level role for all use cases in the project, while ADR 0004 wants per use case | Medium; needs a use-case (governance record) owner distinct from project owner |
| Four effect classes assume the *effect* is the only risk axis | ADR 0004 ceiling uses (effect class, risk tier); risk tier and reversibility cost/volume/blast radius are not fields on a proposal. `risk_class` in `approval_request` is just the effect class | Additive field possible (schema v1 permits optional fields, readers first) |
| One proposed action per agent step; no tool name in the proposal | Bounds agent autonomy per step to one effect; multi-effect plans require extra steps. Within a connector the class is the strictest of mapped tools | Additive in schema, but needs per-action identity |
| Connectors pinned in `profile.py` by digest | Excellent for safety, a gating cost for ecosystem breadth: every new system of record needs code review of identity/capability/config digests | By design; scale via an attested external connector registry later |
| Single execution owner process for all loops | One run at a time per deployment; horizontal scale of autonomous throughput not possible without redesigning claim/fencing | Large; ADR 0002 was written for safety first |
| Approval/SoD compares user-id strings against `run.created_by` | Agent-vs-agent independence (pins) cannot be expressed; agent proposals are attributed to the human initiator | Moderate |
| `human_approval` pass-through default | A workflow authored with a gate can run ungated; "autonomy" can silently become L5 by configuration omission | Easy to fix (require flags in reference profile) |

## 6. Findings that conflict with "a role can be held by a human or an agent, and autonomy can move without rebuilding the workflow"

I classify these as Architecturally Conflicting (as opposed to merely Missing) because closing them requires changing existing abstractions rather than adding beside them:

1. Sealed manifest embeds responsibility (node type + scope string), and run identity is derived from the manifest digest. Changing who performs a step is a rebuild by construction.
2. Authority derivation: grants are capped by a human's role at issue and on every use. An agent principal cannot be an authority source, only a borrowed or pre-bound one.
3. Two identities in one run: grant subject (human starter) versus workflow-owner viewer identity used for knowledge and MCP calls. Autonomy accounting and attribution (who did what) will not reconcile without unifying these.
4. Hard-coded risk ceiling (`risk_ceiling=4`, risk 5 never a run) in a constant, with action-name-keyed risk. Any non-human approver contradicts the current enforcement unless the constant becomes governed data.
5. Single-actor assumption in SoD: "distinct" means "different user id from run.created_by". For agents that is both too weak (two agent ids with the same model and prompt pass) and wrongly shaped (the agent that proposes is recorded as the human who started the run).

Items that I judge Missing but not conflicting: principal typing, autonomy levels, demotion, escalation, per-role capability attachment, evaluation-gated promotion.

## 7. Plan versus code (so docs are not read as capability)

| Claim in docs | Reality on main |
| --- | --- |
| ADR 0004 L0-L5, `principal_type`, `autonomy_level_exceeded`, demotion metrics | Not in code; M27/D10; `map-gap.md` says "not presently enforced" |
| ADR 0002 "AcceptanceDecision" in `acceptance/`, "evidence package" in `evidence/` | Directories do not exist |
| ADR 0002 protected-path table "routed through the dispatcher" for ~40 rows | Target ownership. Production-wired dispatcher callers: none (producer missing) |
| ADR 0001 class-to-control table | Dispatcher requires an approval for all classes; read tools inline with no decision |
| "timeout_behavior escalate" in manifests | Rejected at validation |
| Approval UX contract (`requested_by_type`, `request_changes`, diff, preview) | Contract document; `request_changes` absent from route (`ALLOWED_RESPONSES` exists in records only) |
| M06 timers, reassignment, scoped stop | Not in code |
| V35 autonomy scenario | Not in `tests/egi_scenarios` |
| EGI program size | Plan: D1-D15, MVP week 122, 3 people (`CALIBER_EGI_DELIVERY_PLAN.md`); Beta D5 not reached on main |

## 8. Assessment for the study's questions (governance and execution half only)

- Can the governance spine support autonomy that is earned and revocable? Its primitives are the right shape: exact bindings, rechecks, fencing, reconcile, immutable records, strict contracts. Revocation of a *run's* authority is genuinely immediate (grant recheck at every use and at commit). What is not there is revocation or promotion of an *actor's standing authority across runs*, because the actor has no standing identity or record: authority is minted per run from a human or a pre-bound service principal.
- Does anything built so far contradict the L0-L5 model? Not outright. ADR 0004 was written knowing the code. The risk is in the unwritten bridge: the ADR assumes a GovernanceRecord, principal types, a policy that is data, and a reviewer-independence check. Today's code contains four ad hoc SoD checks, a constant policy and an env-list scope model that each need replacing, not extending.
- Ordering risk: the plan puts autonomy (M27, D10) after the producer wiring (B04/B06/B08) and governance record (M24). That ordering is sound; the main danger is shipping Beta with Aria-style dials (a user-controlled mode switch, deployment-wide reviewer identity) as the *only* autonomy a customer sees, and having them become the de facto model. ADR 0004 already says Aria's mode must read the use-case level in M27.

## 9. Recommendations from this slice (not advocacy; each is conditional on the vision being adopted)

Now (cheap, avoids dead ends; do not add features):
1. Treat "role-holder" as data outside the sealed bundle: decide, before more node types or approval fields ship, where the binding "this approval slot is held by {human group | agent principal}" lives and how admission pins it. Do not add more `required_role` semantics to the manifest.
2. Add principal typing at the identity layer (human/agent/service) even if only `human` and `service` are populated; stamp type (and agent pins) onto `approval_request.requested_by`, `approval_decision.decided_by` and audit rows. ADR 0003 allows optional additive fields with readers first.
3. Make the reference profile refuse a `human_approval` node that can pass through (require runtime approvals, queue and checkpointing flags), and refuse a manifest with a `human_approval` on a deployment where the decision path is not wired.
4. Move `REFERENCE_POLICY_V1` behind a loader that reads a versioned policy record (even with one record) so `policy_version` is data. Do not yet add levels.
5. Consolidate the four distinct-actor checks behind one function that takes principals (not strings), keeping the existing messages by adapter, so ADR 0004 invariant 2 has one place to land.
6. Unify the attribution gap: governed step approvals should record the agent principal as requester and the human as run initiator separately.

Next (production adoption): wire the producers (governed effect context, bound approval decision route, dispatch of approved agent proposals), add escalation/SLA (M06), a scoped stop, tamper-evident audit export, worker concurrency under the lease, and move platform-scope assignment from env lists to a revocable table.

Later (autonomy): GovernanceRecord with risk tier and autonomy level, evidence-gated promotion as a governed release, automatic demotion with in-flight reconciliation preserved, agent-principal grant source, reviewer-independence checks over behaviour pins.

## 10. What I could not verify

- Runtime behaviour: I did not run any test or scenario, so "unwired" conclusions (notably that queued production runs refuse `webhook`/`api_request` nodes and protected registered tools) rest on code reading plus a test comment, not on an executed run. An open PR (#1042, B06.4) is described in docs as the one that wires bound-approval decisions; I did not check its state or branch.
- Whether any deployment configuration (compose files under `deploy/`, `.github/workflows`) sets the runtime-approval, queue and checkpoint flags on; I only confirmed defaults and the profile's refusal list.
- Trace read routes' project scoping end to end, MCP-gateway identity propagation for every caller, and the full set of 132 inventory rows against current code (I sampled).
- Performance: no measured throughput; the "one run per tick" conclusion is from `_tick` structure and the absence of any concurrency setting, not from a load test.
- Evaluation, calibration and knowledge/memory internals (other researchers' scope), and the UI.
