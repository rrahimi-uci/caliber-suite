---
audience:
  - architect
  - decision-maker
  - developer
  - operator
doc_type: assessment
product_area: strategy
stability: draft
summary: Evidence-grounded review and corrected execution plan for the CALIBER v1.0.0 single-tenant MVP.
prerequisites:
  - Read docs/roadmap.md and the current GitHub Project #2
  - Use a current checkout and the supported Python and MLflow profile before treating any finding as release evidence
reviewed_on: 2026-08-14
version_applicability: current main, GitHub Project #2, and untagged v1.0.0 plan
tags:
  - mvp
  - roadmap
  - architecture-review
  - single-tenant
---

# v1.0.0 architecture and backlog review

## Decision

The existing roadmap is a useful alpha-to-pilot outline, but it is **not yet an
executable v1.0.0 enterprise single-tenant plan**. The recent rewrite that
called the narrow prompt-refinement path the entire MVP was directionally good
scope control, but it incorrectly moved security, process ownership,
deployment, recovery, and acceptance out of the MVP. Those are precisely the
capabilities that make a bounded single-tenant product operable rather than a
prototype.

The corrected v1 contract is one governed prompt-refinement journey in one
tenant and one active CALIBER process, backed by PostgreSQL and named MLflow /
storage dependencies. It must be deployable with safe configuration, observable,
recoverable, upgradeable, authenticated, auditable, and measured. It explicitly
does not promise multi-tenancy, enterprise SSO, HA, broker-backed worker fleets,
multi-region DR, managed hosting, untrusted tool/MCP execution, or a uniform
lifecycle for every asset family.

`caliber-v0.1.0` is therefore a controlled-pilot gate, not the MVP. The MVP
release decision remains `caliber-v1.0.0`.

## Evidence snapshot

| Area | Status | Repository evidence | v1 implication |
| --- | --- | --- | --- |
| Core control plane and SPA | Implemented, alpha | `server.py:create_app`, centralized route registration, React SPA, 335 Python test modules and 117 UI test/spec files | Reuse; support only the named journey rather than all visible pages. |
| Prompt release integrity | Substantial but requires end-to-end proof | `release_operations.py` persists `prepared -> applying -> applied` intents and `release_reconciler.py` settles indeterminate MLflow alias effects | Keep #105 and #152 on the critical path; prove fake and live-compatible failure injection. |
| Auth, RBAC, audit, redaction | Implemented but deployment-configured | Session/trusted-header modes, scope checks, CSRF, secret sources, audit redaction, and trace redaction are in `config.py`, routes, and observability modules | v1 needs a hardened profile plus an operator test. OIDC/SCIM are not required. |
| Workflow-run recovery | Partially suitable | `WorkflowRunWorker` has durable leases, checkpoints, and an SQL effect ledger | Eligible only for the selected path after #151 decides the worker model. |
| Refinement/calibration recovery | Not sufficient as-is | `JanitorTask` marks stale refinement jobs failed; the runbook states refinement is not requeued and calibration may remain running | #114 must implement or explicitly document a safe operator recovery path for the canonical journey; merely testing the current loss semantics is not enough. |
| Process ownership | Missing for v1 | All workers, scheduler, janitor, and release reconciler are started in the ASGI lifespan; there is no active-owner guard | #125 and #130 are v1 blockers even for a single-tenant deployment. Broker work is not. |
| Deployment | Missing supported v1 artifact | `deploy/README.md` says the supplied Compose stack is development-only, loopback-only, and uses convenience credentials | Add a hardened single-tenant reference topology and repeatable upgrade/restore verification. |
| Observability | Implemented, needs deployment proof | health/readiness, Prometheus endpoint, queue health, incidents, logs, and MLflow tracing exist | Configure protected metrics, alert routing, retention, and a real operator drill. |
| Tool/MCP execution | Do not support in v1 | `server.py` documents that registered-tool sandbox configuration is bound but the runtime is not routed through it; MCP docs state OAuth is not implemented | Exclude general/untrusted tool and MCP execution from the v1 contract. |
| CI and documentation | Currently blocked | Remote main CI runs 31854956855, 31855446318, and 31856637114 failed because `docs/roadmap.md` omitted required `prerequisites` front matter; the full suite otherwise reached 6,298 passed / 13 skipped on the relevant run | Restore the docs gate before any release claim; passing historical runs do not certify current main. |

## Architecture judgement

The repository has a broad control-plane architecture, not an empty MVP. The
risk is not lack of screens or route handlers. It is treating a broad alpha
surface as a supported enterprise product without deciding the deployment
profile and proving its operational boundaries.

The v1 supported topology must be deliberately simple:

```text
one tenant
  -> TLS/ingress and network policy owned by the operator
  -> one CALIBER API + active worker process
  -> PostgreSQL (CALIBER metadata)
  -> named MLflow tracking/profile dependency
  -> object storage only when the canonical fixture uses it
  -> external log/metric/alert collection
```

This gives a real operating model without pretending that the in-process loops
are horizontally safe. The deployment must reject a second active worker, or
run it API-only with an explicit unhealthy/unsupported state. A later broker and
ownership design can change that boundary; it must not be smuggled into v1.

## Corrected epic structure

| Epic | Product outcome and value | Scope and boundary | MVP success criteria | Dependencies and children |
| --- | --- | --- | --- | --- |
| **#56 — v0.1 foundation and controlled pilot** | A repeatable, evidence-backed prompt-refinement foundation lets the team decide whether to invest in v1 hardening. | One canonical journey, fixture, release semantics, supported automation subset, and pilot evidence. It is not an enterprise deployment claim. | A clean checkout runs the selected journey; external alias failure is reconciled; API/UI evidence agrees; controlled-pilot decision is recorded. | #59–#66 and #76; attach #151–#154. Children #100–#121 and #129. |
| **#57 — v1 single-tenant safety and operations** | A deployable, recoverable, secure single-tenant product can be used for real development and controlled operations. | One active process, one tenant, one bounded workload. No HA, multi-region, broker fleet, or SSO. | Safe configuration is refused; active-loop ownership is enforced; restore/rollback and upgrade drills pass; measured limits and v1 acceptance evidence exist. | #67–#72 and #77. Children #122–#125, #130–#142, plus #157–#159. Broker tickets #126–#128 move out. |
| **#58 — post-v1 breadth decisions** | Deferred enterprise breadth is selected by evidence and customer demand, not by architecture aspiration. | Identity/provisioning, broad lifecycle/plugins, hosting, multi-region, and scale. | Each item ends in a decision record, bounded prototype, or separately funded implementation. | #73–#75 and #143–#149; add #126–#128 here. |
| **#78 — exploratory first-mile pilot** | Usability findings improve the alpha product without silently expanding the v1 contract. | The selected canonical journeys are release input; other feature journeys are exploratory. | #79, #81, #89, #94, #96, and #99 have evidence. The other journeys may run in parallel but cannot block v1 scope freeze without a supported-path finding. | Feeds #59; it is not a substitute for engineering acceptance tests. |

## Required backlog corrections

### Reparent and reschedule existing work

- Attach #153 and #154 to #59, #152 to #61, and #151 to #63. They are currently
  orphan issues: absent from Project #2 and absent from every epic.
- Keep #122–#125 and #130 as v1 work in M5. The current roadmap incorrectly
  lists #122–#124 in M2 while the Project retains them in M5.
- Move #126–#128 (broker contract, acknowledgement/dead letters, replay) to
  #58/post-v1. The supported v1 topology has one active process; these are not
  needed to make that process safe.
- Keep #131 as a measurement decision in M5. Make #132 conditional: only
  implement an async offload if #131 shows the selected v1 envelope cannot be
  met. Keep #133 and #134 as required bounded-concurrency and published-limit
  work.
- Move #135 to M5 as the recovery design input; execute #136–#138 in M6.
- Close #155 as an over-broad duplicate after #157–#159 are created. Its stated
  scope combines topology, migration, backup, restore, artifacts, rollback,
  reconciliation, and an independent drill—far beyond two developer days.

### Create these missing v1 tickets

#### #157 — Publish a hardened single-tenant reference deployment

**Objective.** Replace the development-only Compose stack with an operator-owned
reference deployment for the bounded v1 topology.

**Scope.** Add a production-profile manifest/overlay and a configuration
reference for one CALIBER process, PostgreSQL, the selected MLflow dependency,
optional selected object storage, external TLS/ingress assumptions, non-root
runtime identity, network exposure, strong bootstrap-secret source, secure
cookies, CSRF key, metrics token, egress policy, log sink, and explicit worker
count of one.

**Out of scope.** Helm, Kubernetes operators, HA, managed hosting, TLS
certificate issuance, or a broker worker fleet.

**Technical context.** `deploy/` is explicitly development-only;
`CaliberConfig` owns the safety settings; `server.py` starts the active loops;
`routes/health.py` and `routes/metrics.py` expose the probes to configure.

**Implementation requirements.** The overlay must use no convenience
credentials, publish no backing service by default, document the ingress trust
boundary, and fail validation when required secret sources or the one-process
constraint are missing.

**Deliverables.** Versioned deployment assets, a redacted example environment
file, deployment guide, and a configuration-validation test.

**Acceptance criteria.** An isolated host can render/validate the deployment;
the service reaches readiness only with declared dependencies; unsafe defaults
are refused; the documented public endpoints are only ingress and explicitly
authorized metrics access.

**Testing requirements.** Compose/manifest parsing, startup validation tests,
and a clean isolated deployment smoke test.

**Dependencies.** #122, #125, and #151.

**Documentation.** Update `deploy/README.md`, the configuration reference, and
the v1 release checklist.

#### #158 — Rehearse an in-place upgrade and migration rollback boundary

**Objective.** Make a routine version upgrade reproducible and make the point
of no return explicit before the v1 release.

**Scope.** Define supported source/target versions, image/version pinning,
database migration preflight, compatibility backup, process drain/start order,
health verification, and the documented rollback decision boundary.

**Out of scope.** Zero-downtime upgrades, multi-replica rolling updates, and
automatic schema downgrades.

**Technical context.** Alembic migrations are under `caliber/alembic/`; package
and UI assembly are checked by `scripts/ci-local.sh`; active work is started in
the ASGI lifespan.

**Implementation requirements.** Stop active work cleanly before schema change;
record image, migration, and database versions; do not claim a rollback after a
non-reversible migration without restoring the verified backup.

**Deliverables.** Upgrade script/runbook, compatibility matrix, and an automated
preflight/negative test.

**Acceptance criteria.** The documented prior checkpoint upgrades to the target
in an isolated environment with no manual SQL; readiness and the canonical
journey pass afterward; the rollback boundary is observed and recorded.

**Testing requirements.** Migration tests plus one isolated upgrade rehearsal.

**Dependencies.** #117, #118, #135, and #157.

**Documentation.** Update backup/recovery and deployment guides.

#### #159 — Define the v1 operational data-retention profile

**Objective.** Ensure that trace, audit, workflow-file, log, and object-storage
data are retained and protected according to an explicit enterprise tenant
policy rather than defaults.

**Scope.** Publish the v1 retention, redaction, access, deletion, and backup
matrix for data emitted by the canonical journey, and bind the selected
configuration values into the reference deployment.

**Out of scope.** Legal advice, cross-tenant retention, full DLP, or a general
data-governance product.

**Technical context.** Trace/audit redaction is configured in `server.py` and
`observability/mlflow_tracing.py`; workflow retention is configured through the
janitor; separate metadata and storage ownership is documented in
`docs/operate/storage-and-state.md`.

**Implementation requirements.** Identify every retained data store and its
owner; make unsafe defaults visible; ensure the backup inventory matches the
policy; never place secrets in the policy examples or evidence.

**Deliverables.** Retention matrix, reference-deployment configuration, and a
focused configuration/documentation contract test.

**Acceptance criteria.** Each v1 data class has a retention, access, redaction,
and restore decision; operators can verify the selected settings without source
inspection; the v1 evidence packet links to the matrix.

**Testing requirements.** Configuration parsing and documentation contract
tests, plus one canonical-path inspection proving redaction is enabled.

**Dependencies.** #124, #135, and #157.

**Documentation.** Update storage/state, backup/recovery, observability, and
the release checklist.

## Ticket quality correction

All implementation tickets #100–#149 currently omit at least four required
execution fields: **Objective, Technical Context, Implementation Requirements,
and Testing Requirements**. The first-mile pilot tickets #79–#99 additionally
use an evidence-journey template rather than an implementation ticket template.
That is acceptable only if they remain exploratory test work; it is not an
adequate specification for a coding agent.

Before a ticket enters `In Progress`, replace its body with this exact ordered
structure. The ticket must name real files, commands, and test modules rather
than an aspirational subsystem.

```markdown
## Objective
## Scope
## Out of Scope
## Technical Context
## Implementation Requirements
## Deliverables
## Acceptance Criteria
## Testing Requirements
## Dependencies
## Documentation
```

Every `Acceptance Criteria` list must contain observable outcomes, and every
`Testing Requirements` list must name the test level and command or CI gate.
Decision tickets may have no code deliverable, but must produce a versioned
decision record and change the dependent ticket set explicitly. A test-only
ticket must say whether a discovered defect creates a new implementation issue
instead of growing its own scope.

## Critical path and release evidence

```text
#153/#154 + selected #78 evidence
  -> #100/#101
  -> #102 -> #103 -> #104
  -> #105 -> #106 -> #152
  -> #108/#109/#110/#111 + #112/#113/#114 + #115/#116 + #151
  -> #117/#118/#119 -> #129/#120 -> #121 (controlled-pilot decision)
  -> #122/#123/#124 -> #125/#130 -> #157 -> #131/#133 -> #134/#135
  -> #158/#159 -> #136/#137/#138
  -> #139/#140 -> #141 -> #142 (v1.0.0 decision)
```

Parallel work is limited to documentation/evidence versus the active critical
implementation item. Do not parallelize several high-risk state-machine or
deployment changes on a 1.5-developer plan. #132 is a conditional branch from
#131; #126–#128, #143–#149, and the noncanonical pilot journeys are post-critical
path.

The v1 release packet must include the tagged commit; supported dependency
profile; topology/configuration manifest; clean-deployment output; scope and
role evidence; canonical journey evidence through UI and automation; release
failure/reconciliation and rollback drill; backup/restore and upgrade drill;
workload envelope; readiness/metric/alert verification; open-risk register;
and the named human go/no-go record. A green subset, feature page, or merged PR
does not substitute for this packet.
