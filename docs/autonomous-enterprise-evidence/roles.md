# R1: The Caliber role model, inventoried and assessed against the progressive-autonomy vision

Scope: every concept of "role" in the repository (main at `82f41d1b85d`), read-only. Paths below are relative to the repo root; `caliber/src/caliber/` is abbreviated `src/`. Every repo claim was verified by opening the file. Anything about other systems comes from my own knowledge and was not checked externally (marked **[unverified]**).

Method note. "Role" is used for at least eleven different things in this repository. I separate (a) authorization roles, (b) decision-chain responsibilities, (c) process/runtime roles, (d) agent/workflow constructs, (e) planned-only roles that exist in docs and not in code, (f) delivery-team roles, and (g) incidental uses of the word. The vision's model is Work -> Roles -> Human/Agent Assignment -> Capabilities -> Autonomy -> Evaluation -> Governance; the headline result is that today's repo implements the *Roles* link only as permission tiers on user ids, and has no first-class object for any of the other links except one accidental assignment table.

---

## 0. Findings in brief

1. **Roles are permission bundles attached to a user-id string, not responsibilities.** Two closed vocabularies exist: four platform scopes (`src/auth.py:94-97`) and four project roles (`src/resource_access.py:20-24`), plus a 32-action registry (`PROJECT_ACTIONS`, `resource_access.py:55`) mapping actions to project roles. Scope membership is a comma-separated env var (`src/config.py:1708-1730`); project role is a column on `caliber_project_members` (`src/db/models.py:4377`). There is no role table, no custom role, no role-assignment record that is not a user id.
2. **No principal type exists in code.** `principal_type` / `requested_by_type` / `autonomy_level` appear only in docs (ADR 0004, approval UX contract, EGI plan M27); a repo-wide grep of `*.py` finds none. Audit `actor`, `decided_by`, `requested_by`, `created_by` are all `String(256)`. The only runtime human/agent discriminators are `CaliberAssistantReview.reviewer_kind` (`db/models.py:5101`, always `"agent"`) and `SourceControlReview.actor_kind` (`src/workspace_source_control.py:262`), which is used to *exclude* bots.
3. **An agent can already hold a decision role in exactly one place, by impersonating a user id.** Aria's `agent_review` and `full_autonomy` modes require `CALIBER_ASSISTANT_REVIEWER_USER` to be a distinct user id listed as approver, and `..._RELEASE_USER` a third id listed as operator (`src/assistant/service.py:5529-5551`). It is a convention built on the human role lists, not a first-class agent principal. The structurally correct non-human identity (`ServicePrincipal`, `src/policy/principal.py`) deliberately holds **no** role or scope and cannot approve, review or own.
4. **The workflow node that is closest to "a responsibility" is mislabeled and cannot name an assignee.** `HumanApprovalNode.required_role` (`src/workflows/manifest.py:866`) is validated against *platform scopes* (`VALID_ROLES`, `workflows/approval_policy.py:90`), not project roles; at commit time the same approval additionally requires a project role in `PROJECT_ACTIONS["resource.approve"]` = {owner, reviewer} (`approval_policy.py:508-540`). There is no assignee, no group, no agent option, and `escalate`/`auto_reject` are refused at validation.
5. **Autonomy is currently self-assigned and un-owned.** A plan's `autonomy` dial is editable by any member holding `resource.execute` (owner, editor, reviewer) while the plan is a draft (`src/routes/aria_plans.py:150-185`), and `approval_mode` is a per-turn request field gated only by `caliber.operator` plus deployment readiness. Nobody is the "autonomy authority". Grants have `revoke_grant` but nothing calls it outside its module (grep), so revocation happens only indirectly by demoting or removing the member.
6. **Roles that the vision needs and the repo does not have as code:** supervisor of agents, evaluator/validator (as a held responsibility), exception/escalation owner, autonomy authority, autonomy revoker/incident commander, accountable owner (as a record), auditor, policy author. All are either planned (M06, M24, M27, M05 "audit-reader roles") or absent. ADR 0004 is the only serious design and it models a different, smaller role set (editor, reviewer, approver, owner).
7. **The planning docs' "role:" labels are team hats (A-E, P1-P3), not product roles.** They must not be confused with product roles; the EGI plan keeps them only because tracked GitHub issues carry a `Role` field.

---

## 1. Inventory

### 1.1 Authorization roles (a person's permission level)

| # | Name | Defined at | Represents | Who/what can hold it | What attaches |
|---|---|---|---|---|---|
| A1 | `caliber.viewer` | `src/auth.py:94` | Platform scope tier. Every authenticated user gets it implicitly (`scopes_for_user`, `auth.py:272-290`) | Any authenticated user id (human by convention; a PAT owned by a user) | Reads; not used as a gate (grep: zero `require_scopes(...SCOPE_VIEWER)`). |
| A2 | `caliber.operator` | `auth.py:95`; env `CALIBER_OPERATOR_USERS` (`config.py:1724`) | Platform scope: feedback, register agents, mutate resources, start runs, apply | User id on a config list; admin implies it | The dominant gate: about 190 single-line `require_scopes(..., [SCOPE_OPERATOR])` call sites in `src/routes/` (grep count, multi-line calls not counted). |
| A3 | `caliber.approver` | `auth.py:96`; `CALIBER_APPROVER_USERS` (`config.py:1716`) | Platform scope for approval decisions. Implies only viewer, **not** operator (`_SCOPE_IMPLIES`, `auth.py:111-116`) | User id on a config list | Workflow approval nodes' `required_role` default; Aria gated-step approval (`assistant/executor.py:163`); promotion approval (`routes/workflow_deployments.py:319`); only 3 direct `require_scopes` sites. Combined with operator it is the owner-role precondition. |
| A4 | `caliber.admin` | `auth.py:97`; `CALIBER_ADMIN_USERS` (`config.py:1708`) | Platform scope that implies all lower scopes. Explicitly **not** an implicit project member (`resource_access.py::project_role` docstring) | User id on a config list | About 60 route gates (grep); legacy release-candidate sign-off and waivers (`routes/releases.py:443,514`); break-glass (`workspace_release_governance.py` requires `SCOPE_ADMIN` + session credential). |
| A5 | `owner` (project role) | `resource_access.py:20`; column `CaliberProjectMember.role` (`db/models.py:4391`) | Project permission bundle: 31 of 32 actions | User id with an active membership row; the project's primary owner (`CaliberProject.owner`, `db/models.py:2704`) counts as owner without a row | Narrowed to `editor` unless the holder also has operator **and** approver scopes (`OWNER_ROLE_REQUIRED_SCOPES`, `resource_access.py:220`; `project_role`). Only holder of `release.approve`, `change_request.accept`, `release.rollback`, `release.reconcile`, member management. |
| A6 | `editor` (product label "Developer" per removed workspace plan) | `resource_access.py:21` | Project bundle: author runtime assets, publish, execute, request/apply releases, review CRs. 19 of 32 actions | User id | Cannot sign quality or finally approve. |
| A7 | `reviewer` (product label "QA") | `resource_access.py:22` | Project bundle: evidence authoring, execute, approve promotions, comment on CRs, **sole** holder of `release.quality_signoff`. 9 of 32 actions | User id | Quality sign-off requires the exact role `reviewer` (`workspace_release_governance._authorize_decision` uses `decision.role != expected_role`) **and** both operator and approver scopes (`_REQUIRED_QA_SCOPES`). An owner cannot sign quality. |
| A8 | `viewer` (project role) | `resource_access.py:23` | Read only. 1 of 32 actions | User id | |
| A9 | Visibility tier `project` / `user` / `public` | `src/db/scoping.py:3-10`, `visibility` columns | Row-level audience of an asset, keyed on the row's `owner` string | Resource rows | Not a role but an access dimension orthogonal to A1-A8. |
| A10 | Personal access token | `CaliberPersonalAccessToken`, `db/models.py:1529`; `src/auth.py:77-80,225-229` | Automation credential: a ceiling intersected with the owner's scopes, optionally bound to one project | Owned by a user id; cannot exceed the owner's scopes | Not a role; the only automation identity that reaches normal role checks, and it is *the human's* identity narrowed. |
| A11 | Session | `CaliberSession`, `db/models.py:1500` | Interactive credential. Break-glass demands `credential_kind == "session"` | Human (password account) | |

The product labels Developer / QA / Admin are **not** in code. `resource_access.py:54-61` states they "haven't been renamed onto these strings anywhere in code yet". The authoritative mapping (job functions vs permission roles, QA = reviewer + operator + approver) lived in `docs/workspace-plan.md`, which was deleted in PR #435 (recoverable at `git show cc4c320ec66:docs/workspace-plan.md`) while source docstrings still cite it throughout (dangling references).

### 1.2 Decision-chain responsibilities (workspace release, change request, break-glass)

| # | Name | Defined at | Represents | Holder | Attaches |
|---|---|---|---|---|---|
| B1 | Change-request **Reviewer** (assignment) | `CaliberWorkspaceChangeRequestReviewer`, `db/models.py:3270`; `workspace_change_request_service.py:739-809,909-930` | A per-CR technical review assignment (the only persisted assignment of a person to a responsibility in the platform, apart from rework tasks) | A user id that is owner/editor **and** holds operator or admin (`_require_reviewer_eligible`, `:254-263`) | Review must be by an active assignee; `revision.created_by != reviewer` ("self_review_forbidden"). `actor_role`, `actor_scopes` snapshotted on the review row (`db/models.py:3395-3396`). |
| B2 | Change originator / requester | `workspace_release_governance._originators` (`:176`) | Provenance set {revision creator, release requester, CR creator, head changer} | User ids | Decision makers must not be in it (`_require_distinct_actor`). |
| B3 | Quality sign-off (QA decision) | `record_workspace_release_decision(kind="quality")`; `CaliberWorkspaceReleaseDecision`, `db/models.py:3832` | Human quality gate bound to gate-evidence digest | `reviewer` + operator + approver, distinct from originators | `actor_role_snapshot`, `effective_scope_snapshot` stored (`:3861-3862`). |
| B4 | Release approver (final approval) | same function, `kind="release"`, `expected_role=ROLE_OWNER` | Production-only final approval | `owner` (with live operator+approver) distinct from originators | Needs a fresh passing quality decision with identical digests. |
| B5 | Release apply actor | `PROJECT_ACTIONS["release.apply"]` = {owner, editor} | Executor of the promotion; deliberately not distinct from approver (rationale in the removed workspace plan section 5.2) | Project role | Approver-equals-applier is permitted by design. |
| B6 | Break-glass authorizer | `create_workspace_break_glass_apply` | Exceptional production authority: admin scope, interactive session, reason, incident ref, <= 1 h expiry, release-scoped, distinct from originators | `caliber.admin` + session | `release.break_glass_apply` is intentionally absent from `PROJECT_ACTIONS` ("no ordinary role grant", `resource_access.py:177-183`). The only exception-handler role in the product. |
| B7 | External provider reviewer | `SourceControlReview.actor_kind`, `workspace_source_control.py:262,555-557` | A Git-provider approver mapped to an eligible CALIBER member | `actor_kind == "human"` only; bots are excluded | The sole code that encodes "must be a person". |
| B8 | Rework-task assignee | `CaliberReworkTask.assigned_to`; `routes/rework_tasks.py:230,276,358` | Owner of a rejected job/release (exception work item). Enforced: non-admin updates need to be the assignee; reassignment needs project owner | User id | The only enforced assignment of an exception to a person. |
| B9 | Legacy release signer and waiver grantor | `CaliberReleaseSignoff` (`db/models.py:643`), `routes/releases.py:443,514` | "Accountable go/no-go" on release candidates | `caliber.admin` only; no project role | A second, older, differently-gated sign-off system. |
| B10 | Prompt/job Apply actor and quality reviewer | `CaliberQualityReview` (`db/models.py:391`); `routes/quality_reviews.py` | Human no-go veto; `go` is advisory | operator scope + project role owner/editor/reviewer | Apply has **no second approver** (`docs/build/auth-and-project-scoping.md`, "What the alpha does not enforce"). |

### 1.3 Workflow-runtime and queue roles

| # | Name | Defined at | Represents | Holder | Attaches |
|---|---|---|---|---|---|
| C1 | `HumanApprovalNode.required_role`, `approval_count`, `timeout_behavior` | `workflows/manifest.py:849-888`; IR `workflows/ir.py:356`; `ApprovalPolicy`, `workflows/approval_policy.py:165-222` | Per-node approval gate. `required_role` holds a **scope name**; quorum of distinct approvers; self-approval refused by default | A user id with the scope **and** a project role in {owner, reviewer} at commit (`approval_valid_at_commit`) | Policy snapshot and digest frozen onto the request; no assignee field. |
| C2 | Run **subject** (initiator) | `Subject`, `policy/principal.py:115` | Who started the run; recorded as `user_id` + `auth_method` in `run_manifest.subject` | User id, or a synthetic id for service principals | Distinct-actor rule compares approver to `run.created_by`. |
| C3 | Run **workload** | `Workload`, `policy/principal.py:197` | The pinned workflow version (id derived from digest) | A workflow version | The nearest thing to an "agent identity", but identifies a whole workflow, not a role inside it. |
| C4 | `ServicePrincipal` | `policy/principal.py:242`; subjects `service_principal:cron:<workflow_id>` (`orchestrator/scheduler.py:175`), `service_token:<token_id>` (`routes/services.py:586`) | Server-created, workload-bound non-human ingress | Scheduler / validated service token | `scopes=frozenset()`, explicit `allowed_actions`/`allowed_resources`, bound to tenant/project/workflow/purpose; **not** a project member and holds no role (`grants.create_grant` takes a separate branch, `policy/grants.py:197-262`). |
| C5 | Run grant | `CaliberRunGrant`, `policy/grants.py` | Per-run authority: actions drawn from `PROJECT_ACTIONS`, resources, expiry. Never exceeds the subject's *current* role | Bound to subject + workload | Run admission issues only `resource.execute` and optionally `context.read` (`workflows/admission.py:104-106,632`). Tools and agents do not get their own grants. |
| C6 | Review queue `reviewers` and item `assigned_to` | `CaliberReviewQueue.reviewers`, `CaliberReviewItem.assigned_to` (`db/models.py:1000,1028`); `IRReviewQueueEnqueue.assigned_to` (`workflows/ir.py:275`) | Intended reviewer pool / assignee | Free-form user id strings | **Not enforced**: `submit_item` (`routes/review_queues.py:595-680`) checks operator scope and `feedback.submit` only; `reviewers` is written on create/update and never read for authorization (grep). Decorative. |
| C7 | `AgentNode` / `IRAgent` | `workflows/manifest.py:795`, `workflows/ir.py:157` | An LLM step: `name`, `model`, `instructions`, `tools`, `skills`, `handoffs` | A workflow node | **No `role`, persona, owner or assignee field.** Capabilities attach as `tools: list[str]` and `skills`; handoffs (`HandoffSpec`) name other agent nodes. |
| C8 | Guardrail `on_failure="escalate"` | `manifest.py:829`, `workflows/guardrails.py:571`, `runtime.py:8012` | Declared escalation | n/a | Behaves exactly like `block`: no target exists. Contradicts the approval policy's own rule that "a control that does nothing" is rejected (`approval_policy.py:22-31`). |

### 1.4 Aria (assistant) autonomy and agent constructs

| # | Name | Defined at | Represents | Holder | Attaches |
|---|---|---|---|---|---|
| D1 | `AssistantApprovalMode` | `assistant/models.py:42` (`manual`, `auto_safe`, `auto_all`, `agent_review`, `full_autonomy`) | A per-session/per-turn autonomy policy for drafts | Chosen by the requesting operator (`body.approval_mode`, `assistant/service.py:5131-5138`) | Readiness depends on deployment config only (`routes/assistant.py:636-661`). |
| D2 | Aria reviewer agent (`reviewer_user`, `reviewer_kind="agent"`) | `assistant/reviewer.py`; `service.py:5529-5700`; `CaliberAssistantReview` (`db/models.py:5081`) | An isolated no-tools model call that approves/rejects a draft bound to candidate hash, with expiry | A config-named user id with approver scope, distinct from author | Only agent-held approval in the platform. `confidence` and policy version are recorded. |
| D3 | Aria release principal (`release_user`) | `config.py:1290`; `service.py:5543-5551` | Third identity that publishes after agent review | A config-named user id with operator scope, distinct from author and reviewer | |
| D4 | Plan `autonomy` dial and capability tiers | `CaliberAriaPlan.autonomy` (`db/models.py:1062`: `ask_each`/`approve_plan`/`auto_guarded`); tiers `read/safe/mutate/gated` (`assistant/capabilities.py:35-38`); `gate_decision` (`assistant/executor.py:141`) | Per-plan, per-step-tier autonomy | Set by whoever can `resource.execute` on a draft plan | `gated` always pauses for a human; capabilities declare `required_scopes` that are checked against the plan **owner** (`executor.py:788-805`). |
| D5 | Plan owner and interaction answerer | `CaliberAriaPlan.owner`; `executor.py:582` (`_enforce_sod`) | The human on whose behalf a plan runs; approver of a gated step must hold approver scope and differ from the owner | User id | Capabilities are the only registry in the repo where "what may be done" (scopes) and "how risky" (tier) are declared together and consumed by an autonomy decision. |
| D6 | Registered tool risk class | `effects/registered_tools.py`, `effects/inventory.py` (`read`, `reversible_write`, `irreversible_write`, `export`) | Effect class, resolved from the registry row, never from manifest or model | A tool row | Determines whether a call runs inline or becomes an approvable proposal. Not tied to any role. |

### 1.5 Process, execution and evaluation constructs

| # | Name | Defined at | Represents | Holder | Attaches |
|---|---|---|---|---|---|
| E1 | Execution role `owner` / `secondary` | `src/execution_role.py:62-64`; `ExecutionRoleController`; `ownership.py` lease; `observability/worker_inventory.py` (`owner_only`) | A **process** role: which replica runs background loops (ADR 0002) | A process incarnation holding a fenced lease | Nothing to do with people, agents or authorization. |
| E2 | Experiment owner | `CaliberExperimentOwner`, `db/models.py:2492` | Who owns an MLflow experiment (`project`/`user`/`public`, quarantined) | Project or user | Trace visibility. |
| E3 | `owner` column | 18 tables (`owner: Mapped[str]`) | Creator/visibility key of an asset, not an accountability assignment | User id | Drives `apply_visibility_filter` (`db/scoping.py`). |
| E4 | Judge | `CaliberJudge`, `db/models.py:878`; `workflows/judge.py`; `eval/judge_scorer.py` | Evaluator construct (LLM scorer) authored under `resource.write.evidence` | A definition, not a principal | No identity, no pins on the verdict beyond model; `routes/judges.py::alignment` returns agreement and Cohen's kappa. "Certified"/"rejected" is a cookbook procedure; no persisted certification state (grep: no `certified` in non-cookbook code). |
| E5 | Machine gates | `eval/gate.py`, `DeployGate` (`manifest.py:1073`), release gate evidence (`eval/evidence.py`) | Deterministic pass/no-go | Code | Quality sign-off requires matching successful gate digest. |
| E6 | `@system` poller / background loops | `assistant/executor.py` (async poll), `orchestrator/*` | System actors resuming work | Process | Not modeled as a principal with a role. |

### 1.6 Planned-only or documented-only roles (not in code)

| # | Name | Where | Status |
|---|---|---|---|
| F1 | `principal_type` (`human`/`agent`) with behaviour pins | ADR 0004, EGI plan M27, `approval-ux-contract.md` (`requested_by_type`) | Accepted ADR, scheduled D10; no code. |
| F2 | Autonomy levels `L0_manual` ... `L5_governed_autonomy` over roles editor, reviewer, approver, owner | `docs/architecture/adr/0004-autonomy-levels.md`; `map-gap.md` sections 7 | Design only; `map-gap.md` says "not a claim that L0-L5 are presently enforced". |
| F3 | Risk owner, GovernanceRecord owners, ExceptionWaiver | EGI plan M24 | Planned (D10). |
| F4 | Escalation queue, claim/reassign, scoped emergency stop, override with reason | EGI plan / gap analysis M06 | Planned. |
| F5 | Audit-reader roles | M05 | Planned. |
| F6 | OwnershipTransfer with acknowledgment | M23 | Planned. |
| F7 | SCIM groups / OIDC identity | M17 | Planned. |
| F8 | A2A peer and federated task adoption | M19 | Planned. |
| F9 | Job functions (Developer, QA, release manager, stakeholder), Reviewer vs `qa` env | removed `docs/workspace-plan.md` section 2.1 | Historical doc; source docstrings still cite it. |
| F10 | PRD personas: AI/application developer, authorized operator, platform operator, engineering/product lead | `docs/prd.md:63-68` | Product personas, not enforced roles; "operator" here collides with `caliber.operator`. |

### 1.7 Delivery-program team roles (NOT product roles)

| Name | Where | Meaning |
|---|---|---|
| A technical lead/backend, B backend/runtime, C platform/security, D full-stack/SDK, E test/reliability | `docs/CALIBER_FIVE_PERSON_DELIVERY_PLAN.md:66-72` | Five responsibility hats of the delivery program. Retained as `Role` field on GitHub issues. |
| P1/P2/P3 (three-person mapping) | `docs/CALIBER_EGI_DELIVERY_PLAN.md:102ff`, `map-gap.md:488` | Three people wearing A-E. |
| D1-D15 | Plan docs | **Milestones**, not roles ("D-roles" is a misnomer). |
| External domain owner, independent pilot operator, security reviewer | Five-person plan line 78 | External dependencies of the program. |
| `docs/planning/beta-mvp-execution.json` | 291 tasks | I found no `role` key on any task (script check); the label lives only on the GitHub side. |

### 1.8 Incidental "role" (no governance meaning)

LLM chat message role (`CaliberAssistantMessage.role`, `db/models.py:5017`), dataset-example file role (`CaliberEvalDatasetFile.role`, `db/models.py:4132`), prompt template `{{role_statement}}` (`prompt_template_library.py:55`), database runtime role in PostgreSQL grants (`docs/operate/reference-profile.md:464`), knowledge-graph entity roles (`knowledge/graph.py`, not read in depth). A reader or tool grepping "role" will be misled by these.

---

## 2. Analysis against the vision

### 2.1 Are roles responsibilities or identities?

**Neither cleanly; they are permission tiers that are keyed on identities.** Evidence:

- The model is `user_id -> {scopes from env lists} x {project role from a row}`. Every decision code takes a `CaliberIdentity(user_id, scopes, active_project_id, credential_kind, ...)` (`auth.py:321`). The "responsibility" ("QA signs off quality") is not a first-class thing; it is re-derived at each call site as `(action key, expected role literal, required scope set, distinct-actor check)`. For instance, quality sign-off is the conjunction of `release.quality_signoff` in `PROJECT_ACTIONS` (reviewer only), `expected_role=ROLE_REVIEWER`, `_REQUIRED_QA_SCOPES={operator, approver}`, and `_require_distinct_actor` (`workspace_release_governance.py:133-146`). Four mechanisms to say "this responsibility".
- **Two-axis AND-chain.** Authority is the intersection of a platform scope (global, config-file) and a project role (per project, DB row), documented as `authorize()`'s "effective-decision AND-chain" (`resource_access.py`, `authorize` docstring). `owner` is further capped by the holder's live scopes (`OWNER_ROLE_REQUIRED_SCOPES`). So "approver" in the vision's sense is not one thing: it is scope `caliber.approver` **and** project role owner/reviewer (`approval_valid_at_commit`). The same word names a scope in one table and has no project-role counterpart; the ADR's four chain roles (editor, reviewer, approver, owner) are therefore a *third* vocabulary overlapping the two existing ones (`map-gap.md:276` admits this: "these do not replace the existing distinct platform-scope and project-role vocabularies").
- The removed workspace plan explicitly chose this: "A role earns its existence only when it gates a decision that a *different human* must make" and "Job functions are not permission roles". That is a defensible RBAC position, and the opposite of the vision's "roles represent responsibilities" for any role that is not a distinct-decision gate.
- A responsibility that *is* modeled as an object, the CR reviewer assignment (B1) and the rework task assignee (B8), is **per-instance and per-user-id**: it names a person, not a role, and nothing generalizes it.

Verdict: the code treats a role as "a named set of actions a user may perform", never as "a duty in a workflow whose occupant can change".

### 2.2 Predefined or extensible?

Closed and code-defined:

- Scopes: four constants; `ALL_SCOPES` is checked so typos fail. A second copy of the same list lives in `VALID_ROLES` (`approval_policy.py:92`) used to validate manifests, so adding a scope requires two edits plus every `_SCOPE_IMPLIES` entry.
- Project roles: `PROJECT_ROLES` frozenset; `PROJECT_ACTIONS` is a closed registry and unknown actions are denied (`decide_project_access`). `POLICY_VERSION` is a hand-bumped string (`resource_access.py:210`).
- The `owner` project role has a hard-coded eligibility rule keyed to two scopes (`OWNER_ROLE_REQUIRED_SCOPES`).
- Run policy is `REFERENCE_POLICY_V1` with a risk ceiling and an explicit action-risk map that a test forces to cover every `PROJECT_ACTIONS` key (`policy/decisions.py`).
- Assignment storage: scopes in env (`CALIBER_*_USERS`; the config comment says "a future milestone can swap this for a DB-backed assignment table"); project roles in `caliber_project_members`. No workflow- or use-case-scoped role object.

An organization cannot define "Claims Triage Agent" or "Validator", cannot attach actions to it, and cannot bind a use case to a set of role occupants without a code change. For a platform whose pitch is that enterprises build their own applications on it, this is the largest structural limit of the role model.

### 2.3 Can a human or an agent occupy the same role today?

**Mechanically yes for a user id; semantically no.** What assumes a human principal:

| Assumption | Where |
|---|---|
| Identity is a password account or a PAT owned by one; scope assignment is by user id in an env list | `CaliberUserAccount` (`db/models.py:1471`), `auth.py:272-290` |
| Approvals are "decided by a person"; request/decision records carry only a string id (`requested_by`, `decided_by` are `{"type":"string","minLength":1}` in the JSON schemas) | `workflows/approval_requests.py:1`; `contracts/schemas/approval_request.v1.json`, `approval_decision.v1.json` |
| "Distinct people" is implemented as distinct strings; the repo says so: "identity uniqueness does not prove distinct humans" | `map-gap.md:138`; `actor_provenance.is_same_actor` compares `user_id` only; `approval_policy.assert_distinct_actor` |
| `owner` and quality sign-off need operator+approver scope lists, so an agent would need to be written into the same env lists as the humans | `resource_access.py:220`, `workspace_release_governance._REQUIRED_QA_SCOPES` (`:66`) |
| Break-glass needs an interactive session; PAT and service credentials are refused | `workspace_release_governance.create_workspace_break_glass_apply` |
| External Git reviews count only for `actor_kind == "human"` | `workspace_source_control.py:555` |
| Change-request reviewer must be owner/editor **and** operator/admin | `workspace_change_request_service._require_reviewer_eligible` |
| The approval UX contract's `requested_by_type` is derived from a `principal_type` that does not exist | `docs/architecture/approval-ux-contract.md` "Panel data" |

What would break if an agent held a role today:

1. **Authentication.** There is no non-human credential that resolves into a `CaliberIdentity` with scopes. `ServicePrincipal.to_identity()` returns `scopes=frozenset()` (`principal.py`), so an agent run as a service principal cannot satisfy any scope or role check, and `create_grant` deliberately skips the role check for it (`policy/grants.py`, `else:` branch computes `_current_role` only for non-service subjects). Therefore **moving a duty from a human to an agent changes which *authorization model* applies**: from role-and-membership (humans) to a fixed claim binding (service principals). That is a model change, not an occupant change.
2. **Provenance.** Audit rows cannot say whether a decision was human or agent (`audit.record(actor: str, ...)`, `CaliberAuditLog.actor String(256)`). ADR 0004 invariant 7 is unimplementable until a type exists.
3. **Independence.** ADR 0004 invariant 2 requires an agent reviewer to differ from the agent editor in a behaviour pin. The run manifest's pins are **per run, not per role/node** (`run_manifest.v1.json`: `model_routes`, `prompt_versions`, ... arrays for the whole workload), and an `AgentNode` has no identity at all, only `name`. There is nothing to compare.
4. **Quorum.** `approval_count` counts distinct strings; five agent identities would satisfy a "quorum of five" with no independence property.
5. **Approver SoD target.** The check compares approver to `run.created_by` (the initiator). For a cron-started run the "initiator" is `service_principal:cron:<workflow_id>`, so every human approver trivially differs, and a scheduled agent chain has no accountable human initiator recorded.

What already works for agents (do not understate it): Aria's reviewer/release principals are persisted as `reviewer_user`/`reviewer_kind="agent"` with candidate hash binding, expiry, and distinctness from author and from each other (`service.py:6108-6136`). The proposal/approval/receipt chain for protected effects is principal-agnostic in its binding checks (intent hash, evidence hash, policy digest, expected version, expiry; `approval_valid_at_commit`).

### 2.4 How do capabilities, tools, permissions, policies attach?

| Thing | Attaches to | Evidence |
|---|---|---|
| Scopes | A user id (config list), further narrowed by a PAT | `auth.py` |
| Project role | A (project, user id) membership row | `db/models.py:4377` |
| Actions | A project role (inverse map `PROJECT_ACTIONS`) | `resource_access.py:55` |
| Tools available to an agent | The **agent node** in the manifest (`tools: list[str]`, `skills`), not any role or principal | `manifest.py:795` |
| Tool risk | The **tool registry row** (`side_effect_level`, resolved by `effects/registered_tools.py`), never the manifest or the model | module docstring |
| Run authority | A **run grant** = (subject, workload, actions, resources, expiry, policy version) | `policy/grants.py` |
| Policy | The policy version pinned at admission; `REFERENCE_POLICY_V1` with a risk ceiling | `policy/decisions.py` |
| MCP/egress/trace policy | The project / the resource, independent of roles (except `trace.payload.read` = owner, editor) | `mcp_policy.py`, `observability/trace_access.py` |
| Aria capabilities | The **capability registry**: key, tier, `required_scopes`, checked against the plan owner | `assistant/capabilities.py`, `executor.py:788` |
| Approval policy | The **approval node** (required scope, quorum, timeout), snapshotted onto the request | `approval_policy.py` |
| Autonomy | The **plan** (`autonomy`) or the **chat turn** (`approval_mode`); planned: the use case (ADR 0004) | above |

So authority attaches to six different anchors (user, membership, grant, node, tool row, plan), and **none attaches to a role-in-a-workflow**. The run grant contains only `resource.execute` and `context.read` for the whole run: an agent step cannot be given less authority than the run, nor can two steps in one workflow carry different authority. Within a run, finer authority is exercised via effect-class routing of tools (read inline, writes become proposals needing approval), which is a property of the tool, not of who is acting.

### 2.5 Is there any first-class assignment (role -> human/agent) or role-scoped autonomy?

- **Assignment.** Closest objects: CR reviewer assignment (B1), rework task `assigned_to` (B8), review queue `reviewers`/`assigned_to` (C6, unenforced), `ReviewQueueEnqueueNode.assigned_to`, project membership (A5-A8). All map a *person string* to a *concrete instance* (a CR, a task) or to a *project*. None maps a **role in a workflow definition** to an occupant, none supports "agent" as occupant, none supports "role X in use case Y". The approval node has no assignee at all.
- **Role-scoped autonomy.** None in code. Autonomy exists at three other granularities: per plan (`autonomy`), per chat turn (`approval_mode`), and per capability tier (`gated` is a hard floor). ADR 0004 proposes per use case and per role-in-chain, but says the platform "behaves as L1 everywhere" until M27.
- **Evidence-gated change of occupant.** None. There is no object representing "this duty is currently held by principal P under level L with evidence E". The nearest analogue is the release decision row with `actor_role_snapshot` and `effective_scope_snapshot`, which records *who decided* but not *what authority regime they decided under*.

### 2.6 Overlaps and incorrectly scoped roles (observed)

1. **`required_role` is a scope, not a role** (manifest field and error text: "is not a CALIBER scope"). Authors of workflows who write "role" reach a platform-wide scope, while the enforcement also silently requires a project role {owner, reviewer} at commit time. Two different checks, one label.
2. **`reviewer` has four meanings**: project role (QA), per-CR technical Reviewer assignment (eligible: owner/editor, *not* the `reviewer` role), review-queue reviewer list (unenforced), Aria reviewer agent (approver scope, no project role). The project role `reviewer` is *not eligible* to be a CR reviewer (`_require_reviewer_eligible`), which inverts the name.
3. **`owner` has at least eight meanings**: project role; primary project owner (`CaliberProject.owner`); `owner` column creator key on 18 tables; `CaliberReviewQueue.owner`; `CaliberAriaPlan.owner`; experiment owner; execution owner lease; "package owner" in plans. None is an *accountable owner* in the governance sense (no record, no transfer other than `project.transfer_owner`).
4. **`operator`** is a scope (`caliber.operator`), a PRD persona, an Aria release identity requirement, and the colloquial "platform operator". The scope grants almost everything mutating, so it is not an "operator" in the vision's sense (running agents) but a general write permission (about 190 gates).
5. **Approval has at least eleven independent mechanisms**: workflow runtime approval (C1), workflow promotion approval, Aria gated plan step, Aria draft agent review, CR technical review, workspace quality sign-off, workspace final approval, break-glass, legacy release-candidate sign-off with waivers, refinement job Apply (no second approver), external provider review. Each has its own actor rule and its own distinct-actor implementation; the repo documents deliberately *not* unifying them (`actor_provenance.py`, `approval_policy.assert_distinct_actor` docstrings). For role assignment this multiplies the places an "approver is now an agent" change has to be made.
6. **Admin is double-edged**: `caliber.admin` implies every scope but is not a project member, so an admin cannot approve in any project without membership, yet can read every project's jobs (`docs/build/auth-and-project-scoping.md`) and use break-glass. This is deliberate (P1-B) but means the highest platform role has no day-to-day accountability path.
7. **Dangling design source**: roles are justified in `docs/workspace-plan.md`, which is deleted; role semantics now live in docstrings of `resource_access.py` (very long, change-history style) and in ADR 0004.

### 2.7 Missing roles for the vision, and who answers today

| Vision question | Who answers today | Verdict |
|---|---|---|
| **Who supervises autonomous agents?** | Nobody is assigned. Implicit: the run's initiator (can cancel/resume with `resource.execute`), the plan owner (Aria; answers interactions), approvers at gated steps. No notion of a supervisor of an agent/workflow, no "watch" subscription, no intervention SLA (M06 planned). | Missing |
| **Who evaluates agent performance?** | Any holder of `resource.write.evidence` (owner, editor, reviewer) authors judges/test sets; anyone with `resource.execute` runs an evaluation; machine gate and a `reviewer` quality sign-off for releases. Judges have no identity; judge certification is not persisted. The evaluated artifact's author may also evaluate it (author-vs-evaluator separation exists only at the release sign-off). | Partially: capability exists, role/independence does not |
| **Who handles exceptions?** | Rework task assignee (rejected job/release; enforced); `release.reconcile` (owner) and operator reconcile routes for uncertain release state; effect reconciler (system loop); break-glass admin. Escalation is explicitly not implemented (`timeout_behavior` only `block`; guardrail `escalate` == `block`). | Partially: assignment exists for two failure classes, nothing for agent step failures, budget breaches, approval timeouts |
| **Who approves autonomy increases?** | Nobody. Plan creator (draft, `resource.execute`) and chat requester choose; deployer chooses whether reviewer/release identities exist. ADR 0004 invariant 5 (human owner decision recorded as a governed release) is not built. | Missing (and today self-service) |
| **Who can reduce or revoke autonomy?** | Deployer edits env; plan edit while draft; cancel a run (`resource.execute`); remove/demote member (narrows grants immediately); revoke session/PAT. `revoke_grant` exists but has no caller outside `grants.py`. No scoped stop (M06), no automatic demotion (ADR 0004 invariant 6). | Missing as a role and as a mechanism |
| **Who remains accountable?** | Implied: project primary owner (`CaliberProject.owner`, `project.transfer_owner`); release records name `decided_by`, `requested_by`; run records name `created_by`. For Aria autonomous publishing the author user is the requester of record and the reviewer/release identities are config-named. There is no accountability record per use case (M24 planned). | Partial and implicit |
| **Supervisor / overseer of agents** | see "who supervises" | Missing |
| **Evaluator / validator (as held role)** | QA = `reviewer` + scopes, but human-only by design (B3, `actor_kind` rules) | Present for humans only |
| **Exception handler / escalation owner** | Rework assignee; no routing, no queue, no on-call | Partial |
| **Autonomy approver / authority** | none | Missing |
| **Autonomy revoker / incident commander** | admin break-glass is the only incident-time role, and it grants *more* power, not less; emergency stop planned | Missing |
| **Accountable owner** | `CaliberProject.owner` (identity, not a duty record) | Partial |
| **Operator** (runs/monitors workflows) | `caliber.operator` = general write; there is no run-monitoring duty | Misnamed |
| **Builder/developer** | `editor` | Present |
| **Auditor** | read-only viewer sees "audit" per old plan; platform admin inventory (`routes/platform_admin_inventory.py`); `audit-reader roles` planned in M05 | Missing as a distinct role |
| **Policy author** | `REFERENCE_POLICY_V1` in code; approval policies authored by whoever edits the workflow (`resource.write.runtime`); the author of a workflow also authors its own approval policy and guardrails | Missing; author-of-control equals author-of-controlled |

---

## 3. Design constraints: what makes "move a responsibility from human to agent without rebuilding the workflow" hard

Each item is a concrete constraint with a citation.

1. **A workflow step names no role.** `AgentNode` has `name` only (`manifest.py:795`); `HumanApprovalNode` has `required_role` (a scope) and a quorum; `ReviewQueueEnqueueNode` has a free-text `assigned_to`. There is no indirection "step -> role -> occupant", so switching from a person to an agent means editing the node type (approval node -> agent node plus separate reviewer plumbing), i.e. rebuilding the workflow.
2. **Human decision and agent decision are different node types and different tables.** Human approvals live in `caliber_runtime_approval_requests` driven by `HumanApprovalNode`; agent review lives in `caliber_assistant_reviews` in the Aria service. The approval UX contract (`approval-ux-contract.md`) only anticipates an agent as *requester* (`requested_by_type`), not as decider, and expects an agent approver above its ceiling to "fall back to a human approver", which presumes a role-holder abstraction that does not exist yet.
3. **Authorization model forks on principal kind.** Role/membership path for users; claim-binding path for service principals (`grants.create_grant`; `ServicePrincipal` with empty scopes). An agent in a "role" requires either extending `Subject`/`ServicePrincipal` to hold project roles (contradicting the ADR 0004 line that a service principal "is not a promotion to an autonomous owner, reviewer or approver") or inventing a user account for the agent (what Aria does).
4. **Authority is an AND of config-file scopes and DB roles.** To give an agent the approver duty you edit an env var and a membership row; one is a deployment change, the other a project change; they have different owners and audit trails. `OWNER_ROLE_REQUIRED_SCOPES` hard-wires this for owner.
5. **Distinctness is string equality with no type.** Quorum, SoD and `is_same_actor` compare `user_id`. If agents are user ids, a single human can mint several "independent" approvers; if they are not, there is no home for them.
6. **No pins at node/role granularity.** Run manifest pins are workload-wide arrays (`run_manifest.v1.json`); ADR 0004's independence rule needs per-role pins. There is also no run-level record of "which node was decided by what kind of principal".
7. **Grants are run-wide and action-coarse.** Admission issues `resource.execute` (+ `context.read`) for the entire run (`admission.py:632`). A responsibility-level authority ("the Editor agent may write to the draft store; the Approver may not") has no representation; step authority is effectively "tool effect class + approval".
8. **Autonomy is stored in three unrelated places** (Aria plan, Aria chat session metadata `assistant_approval_mode`, config readiness) and is written by the actor it constrains. ADR 0004 intends one per-use-case value on a GovernanceRecord that does not exist (M24 planned). Until then, any move of a responsibility is an edit of a per-plan or per-turn flag, with no evidence gate.
9. **No role-assignment history.** Changing a project membership role mutates the row in place (`routes/projects.py` PATCH member updates `member.role`), with an audit entry and `deactivated_at/by` only for removal. There is no versioned "occupant of duty D from t1 to t2", so a promotion/demotion of autonomy cannot be replayed from records. Release decisions snapshot role and scopes, which helps, but only for decisions.
10. **Role literals are hard-coded at call sites.** `expected_role=ROLE_REVIEWER` and `ROLE_OWNER` equality in release governance, `{ROLE_OWNER, ROLE_EDITOR}` in CR eligibility, `ROLE_OWNER` special cases in `project_role`. Making the occupant of "quality sign-off" an agent would require editing those functions, not configuring a binding.
11. **Escalation and timeout do not exist as concepts at the role level.** `validate_timeout_behavior` forbids `escalate`/`auto_reject`; guardrail `escalate` is a `block`. An autonomy design that relies on "agent first, human on exception" has nowhere to route the exception.
12. **Three "safe defaults" that fight autonomy.** `gated` tier is a hard human floor for irreversible Aria capabilities; the `owner` is human at every ADR 0004 level; `caliber.admin` is not a project member. These are good safety positions, but they are encoded as code constants (`gate_decision`), not as policy that a governance process could tune per use case.
13. **Dangling references.** Many docstrings cite section numbers of a deleted plan; the single authoritative statement of role semantics is now scattered, which raises the cost of any redefinition.

---

## 4. Challenges to the vision's own role assumptions

(Critical posture: these are risks to the vision, not endorsements of the current code.)

- **"Roles represent responsibilities; humans or agents occupy them" is a strong claim.** Evidence in this repo and in the ADR suggests the *asymmetries* are real: an agent needs pins, budgets, effect-class ceilings and revocation by a human; a human needs authentication, availability, conflicts of interest and legal accountability. Treating the occupant as interchangeable can hide these. ADR 0004 handles it by keeping permissions fixed and varying the "kind of principal in each chair", which is a clean idea, but it leans on the existing four-chain-role vocabulary that does not match either existing role vocabulary.
- **The determinant of whether an agent may act is mostly effect class and risk, not the role.** ADR 0004 itself bounds agent approvers by effect class (`read`/`reversible_write` vs `irreversible_write`/`export`) and the risk tier. If the effective control is (effect class x risk tier x evidence), the "role" adds an indirection; it might still be right as the unit of *assignment and accountability*, but then autonomy may belong to the (use case, effect class) pair rather than to the role.
- **"Owner always human" is a governance choice with legal grounding, not a technical invariant.** It is reasonable now; making it an invariant of the platform rather than a policy may constrain enterprises that assign accountability to a team or committee (a group occupant, not a person).
- **Distinct-principals and independence do not compose across occupants.** The repo's own risk table says "Single operator or model reviewer treated as independent approval" is a named risk (`CALIBER_REFERENCE_ARCHITECTURE_GAP_ANALYSIS.md:519`). Agent-agent independence is weaker than human-human; the ADR requires deterministic checks (acceptance predicate, trajectory gate), which means the role abstraction alone cannot carry the trust claim.
- **Complexity cost.** Caliber already has eleven approval mechanisms and three role vocabularies. Adding a fourth (responsibility roles, level per use case) without collapsing the others would make the model harder to reason about than the problem it solves.

---

## 5. How other systems model this **[all unverified externally; from my own knowledge]**

| System | Model | Relevance |
|---|---|---|
| **BPMN lanes / participants** | Lanes partition activities by *performer role* (a process-level responsibility), orthogonal to who fills it; assignment is resolved at runtime (potential owner, human performer) | Closest to "step -> role -> occupant". Caliber's missing indirection. |
| **RACI** | Responsible / Accountable / Consulted / Informed per task; exactly one Accountable | Caliber merges R and A; accountability is implicit (`CaliberProject.owner`). RACI suggests an explicit accountable-owner record, which M24 proposes. |
| **Camunda / Temporal human tasks** | A user task has assignee, candidate users/groups, due dates, escalation, delegation; Temporal expresses it as signals/updates plus app-level assignment | Caliber's approval node lacks assignee, candidate group, due date, escalation, delegation (M06 plans some). |
| **Cloud IAM roles vs agent identities (AWS, Entra)** | IAM roles are assumed by workload identities; Microsoft Entra Agent ID treats an agent as a first-class directory principal with its own lifecycle, sponsor (accountable human), conditional access and permissions | Caliber's `ServicePrincipal` resembles a workload identity but with no standing role; Entra's "sponsor" is the accountable-owner pattern. |
| **Salesforce Agentforce** | Agents defined as topics + actions + guardrails, assigned permission sets, run as a configured user; "digital labor" alongside human users | Capability is attached to the *agent definition* (topics/actions), permissions to a run-as identity. Caliber has the analogue for tools on the node, permissions on the user. |
| **ServiceNow (AI Agent Orchestrator / Now Assist)** **[low confidence]** | Agents get roles and ACLs like users; assignment groups for tasks | Assignment-group routing is the exception-owner pattern Caliber lacks. |
| **UiPath (Orchestrator, Maestro, Action Center)** **[low confidence]** | Robots/agents run under machine identities; human tasks go to Action Center with assignees/groups and escalation; process-level roles | Human-in-the-loop as a first-class task with an assignee queue. |
| **SAE J3016** | Levels define who performs the dynamic driving task, who is the fallback, and the operational design domain (ODD); levels apply per feature/ODD, not per vehicle | Supports per-use-case/ODD autonomy and "fallback-ready user" as an explicit role; analogy is weak where enterprise effects are heterogeneous (map-gap.md section 7 already says so). |
| **"Levels of Autonomy for AI Agents" (Feng et al., 2025)** | Five levels defined by the *user's role*: operator, collaborator, consultant, approver, observer | Matches the vision's idea that autonomy is the human's role relative to the agent; differs in that it defines levels by the human role, not by per-role occupant. ADR 0004's L2-L5 are close to consultant/approver/observer progressions. |
| **Prior survey inside this repo** (removed workspace plan, section 2.6) | Compared LangSmith, Braintrust, Humanloop, W&B, Databricks, Azure AI Foundry, Bedrock, etc.: none had a dedicated QA role; the enforced boundary is author vs deployer; approval is better as a per-instance gate with policy than a role-enum member; Databricks deployment jobs use tagged approval gates | Useful, but it was written for human RBAC and is dated Sept 2026; none of it covers agent occupants. |

---

## 6. Implications to carry into the architecture report (hypotheses, not recommendations)

- The platform would need a first-class **responsibility** object (a named duty in a use case/workflow, with required evidence, effect-class ceiling, distinctness constraints) separate from permission roles, plus an **assignment** record (duty -> principal, with kind, pins, level, since/until, granted-by). Today's closest things (B1, B8) show the pattern at instance level.
- A **principal type** and a **non-human principal that can hold project-level authority** are prerequisites; the ServicePrincipal claim model and the user-role model need reconciling, or the "agent in a chair" would take the weaker, claim-based path.
- The approval node needs an **indirection to a duty** instead of a scope string, and an **escalation target**.
- The ADR 0004 role set (editor, reviewer, approver, owner) is smaller than the vision's (supervisor, evaluator/validator, exception owner, autonomy authority, revoker, accountable owner, auditor, policy author) and conflates scope with project role. This needs deciding before M27 builds on it.
- **Do not** assume roles must be per-use-case extensible on day one: the Beta is a single reference use case, and ADR 0004 already defers autonomy enforcement to D10. The no-dead-end requirement is mostly: add `principal_type` and per-decision principal/pins to records early (cheap, schemas still `v1` with string ids), and avoid adding more literal-role call sites.

---

## 7. What I did not verify

- Behavior at runtime: all findings are from reading source and docs; I did not run the server or tests (the local environment lacked `httpx` for importing the package).
- The book "Building Enterprise General Intelligence" (Chapters 1, 13, 14, 26 cited by ADR 0004) is not in the repo; its role definitions are known only through the ADR.
- External systems (section 5) are from memory; no web lookup was done, including the Feng et al. paper's exact level names and ServiceNow/UiPath specifics.
- GitHub issue `Role` fields and the live Project #3 board (outside the repo).
- `caliber-ui` role displays, `docs-site` generated pages, `sdk` role wrappers beyond `models/core.py` and `models/workspace.py`, `knowledge/graph.py` role usage, and `paper/` content were only sampled by grep.
- Route-level scope counts (about 190 / 60 / 3) come from a single-line grep and undercount multi-line calls.
