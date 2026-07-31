# CALIBER Platform Architecture

This document is the authoritative overview of the CALIBER platform. It describes
how the system is bootstrapped, how its layers fit together, where state lives,
and how the cross-cutting subsystems combine into a single control plane. The
per-feature documents that accompany it describe how individual bounded contexts
plug into the substrate established here; this document is the anchor they refer
back to.

Throughout, all HTTP routes are mounted under the
`/ajax-api/2.0/mlflow/caliber` prefix. To keep the prose readable, endpoint
paths are shown relative to that prefix once the convention has been stated.

## At a glance

| Dimension | Where CALIBER stands |
| --- | --- |
| **What it is** | A system-level ASGI control plane with two MLflow-integrated topologies: embedded `mlflow.app` or standalone service. |
| **Where it runs** | Either in the MLflow server process, or as the bundled standalone CALIBER service that calls MLflow over HTTP. |
| **How users reach it** | A React SPA served under `/caliber/`; every action flows through the API under `/ajax-api/2.0/mlflow/caliber/*`. |
| **Source of truth** | SQLAlchemy relational metadata is authoritative; object storage owns file bytes; MLflow owns prompt versions and traces. |
| **Work model** | Request-path actions return inline; durable work is queued to in-process background loops (refinement, calibration, workflow/Aria/KB work, janitor, and webhooks). |
| **Trust model** | Session mode verifies a database-backed account and resolves the `viewer` / `operator` / `approver` / `admin` scopes; trusted identity headers are an explicit proxy-mode option. |

The sections below start from this picture and drill down — scope, boundaries,
runtime, state, surfaces, lifecycle, security, operations, and the seams the
system is extended along.

```diagram-svg
assets/platform-overview.svg
```

*Platform overview — the same CALIBER ASGI application can be embedded in MLflow or
served separately, exposes one same-origin browser control plane in either topology,
and coordinates off-request work through durable state.*

## Reference

## 1. Scope and responsibilities

The `caliber` module is the system-level control plane for the product. Its
`create_app()` factory is used in two ways: MLflow can load it as an in-process
`mlflow.app`, or Uvicorn can serve it independently while it uses MLflow's HTTP APIs.
The first topology shares MLflow's process failure domain; the second does not.
Its responsibilities are correspondingly broad — wider than those of any single
feature module — and span the following concerns:

- It bootstraps the Starlette application and the shared runtime dependencies on
  which every feature depends.
- It serves the React SPA under `/caliber/`.
- It exposes the CALIBER API surface under
  `/ajax-api/2.0/mlflow/caliber/*`.
- It owns persistence, authorization, CSRF, rate limiting, project scoping, and
  audit.
- It orchestrates background loops for refinement, tool calibration, workflow runs,
  Aria plans, knowledge-base builds, janitor work, and webhook dispatch/recovery.
- It integrates with MLflow tracing, the MLflow prompt registry, object storage,
  and optional event-bus backends.

These responsibilities are realized across a small set of primary code paths,
which serve as the entry points for the rest of this document:

- `caliber/src/caliber/server.py`
- `caliber/src/caliber/routes/__init__.py`
- `caliber/src/caliber/config.py`
- `caliber/src/caliber/db/session.py`
- `caliber/src/caliber/auth.py`
- `caliber/src/caliber/routes/static.py`
- `caliber/caliber-ui/src/components/AppShell.tsx`

## 2. Module boundaries

Given that breadth, CALIBER is best understood as a layered application rather
than a monolith stitched together from feature-specific entry points. Each layer
has a clear responsibility and consumes the layers beneath it through stable
interfaces.

| Layer | Main code paths | Responsibilities |
| --- | --- | --- |
| Boot and dependency graph | `server.py`, `config.py` | Load configuration, construct providers, wire `app.state`, and own process lifecycle. |
| Transport and routing | `routes/__init__.py`, `routes/*.py` | Register HTTP endpoints, apply auth and request parsing, and translate errors to API responses. |
| Persistence | `db/models.py`, `db/session.py` | Define relational state, session factories, and durable audit/reference entities. |
| Feature modules | `routes/prompts.py`, `routes/tools.py`, `routes/skills.py`, `routes/agents.py`, `routes/mcp_servers.py`, `routes/workflows*.py`, `routes/assistant.py` | Implement domain-specific API behavior and orchestration. |
| Async execution | `orchestrator/*.py`, `workflows/run_launch.py` | Run queued refinement and workflow execution off the request path. |
| Shared subsystems | `auth.py`, `observability/*`, `storage/*`, `events/*`, `tool_sandbox/*` | Provide cross-cutting concerns used by multiple modules. |
| Frontend shell | `caliber-ui/src/components/AppShell.tsx`, `TopBar.tsx`, `Sidebar.tsx`, `WorkspaceSelector.tsx` | Route users into page-level feature modules, select or create the active project, and keep shared chrome, state, and assistant panel behavior consistent. |

The architectural boundary that matters most is that feature modules do not own
application bootstrapping. `server.py` constructs every long-lived dependency
exactly once, stores it on `app.state`, and leaves the route modules to consume
those dependencies rather than create their own. This single-construction
discipline is what keeps the layering above honest in practice.

## 3. Runtime architecture

The layers described above resolve, at runtime, into the flow shown below: a
browser drives the SPA and static surfaces, requests fan into the route modules,
and the route modules and background workers share access to persistence,
MLflow, object storage, the event bus, and the provider adapters.

```mermaid
flowchart LR
    B[Browser]:::user
    SPA[React SPA<br/>/caliber/]:::ui
    ST[Static route handler]:::ctrl
    API[Starlette route modules]:::ctrl
    AUTH[Auth, CSRF, rate limit, project scoping]:::ctrl
    DB[(SQLAlchemy metadata DB)]:::store
    ML[MLflow APIs and traces]:::ext
    OBJ[(Object store / S3 / MinIO)]:::store
    BUS[Event bus]:::async
    WK[Background workers]:::async
    EXT[Provider adapters<br/>LLM, eval, promoter, MCP, sandbox]:::ext

    B --> SPA
    B --> ST
    SPA --> API
    ST --> API
    API --> AUTH
    AUTH --> DB
    API --> DB
    API --> ML
    API --> OBJ
    API --> EXT
    API --> BUS
    WK --> DB
    WK --> ML
    WK --> OBJ
    WK --> BUS
    WK --> EXT
```

```legend
```

Several structural properties follow from these topologies and are worth making
explicit, because they shape every design decision downstream:

- Embedded mode loads CALIBER into the MLflow server as a sibling ASGI surface.
  Standalone mode serves the same application separately and points
  `MLFLOW_TRACKING_URI` at MLflow. Neither mode makes CALIBER a transparent gateway
  in front of MLflow.
- The frontend shell is bundled separately with Vite, but it is served through
  the CALIBER package by `routes/static.py` rather than from a distinct origin.
- Shared dependencies are constructed once in `create_app()` and injected via
  `app.state`. They comprise the SQLAlchemy engine and session factory, the
  artifact store, the LLM provider, the eval provider, the promoter, the event
  bus, the assistant service, and the background workers.
- `CALIBER_DATABASE_URL` independently owns CALIBER's tables. It is not required to
  equal MLflow's backend-store URL; the bundled stack uses separate logical databases
  so the two Alembic histories never compete for one version table.
- The codebase uses sync SQLAlchemy consistently, even inside Starlette. Sync
  route handlers execute in Starlette's threadpool; this keeps the ORM model
  aligned with MLflow's own sync behavior and avoids the impedance mismatch of
  mixing async ORM access into a host process that is itself synchronous.

## 4. Data model and state

With the runtime established, the next question is where authoritative state
lives. The persistence model is deliberately broad: CALIBER keeps durable
product state in SQLAlchemy rows and treats external systems such as MLflow and
object storage as integrated subsystems rather than as replacements for
relational metadata.

That state divides into the following domains, each anchored to a representative
set of tables:

| Domain | Representative tables | Purpose |
| --- | --- | --- |
| Core governance | verification items, refinement jobs, approvals, rollback checkpoints | Human-in-the-loop refinement pipeline and deployment safety. |
| Prompts and assistants | prompt test runs, assistant sessions/messages/drafts/runs/publish events/attachments, Aria goal-plans/steps/interactions | Prompt authoring, testing, assistant-driven authoring state (Aria context attachments), and Aria's agentic goal-plan orchestration. |
| Tools and skills | tool registry, tool test runs, skill rows, skill test runs | Reusable runtime capabilities and authoring/test surfaces. |
| Workflows | workflows, workflow versions, deployments, runs, events, checkpoints, session memory, benchmark reports, patches, promotions | Workflow Studio source-of-truth and runtime lineage. |
| MCP | MCP servers | External tool endpoints, discovered tools, policies, and calibrations. |
| Knowledge bases | knowledge bases, versions, sources, chunks, entities, relationships, build runs, calibration test runs | Versioned RAG corpora with chunking/embeddings, graph (Apache AGE) extraction, and retrieval-quality calibration. |
| Evaluations / test sets | eval datasets, eval dataset examples, eval dataset files, eval runs, judges, review queues/items, gate verdicts | Versioned test sets, scorecard evaluation runs, operator-authored LLM judges, human review queues, and advisory per-version gate verdicts. |
| Files and projects | projects, workflow files, file events | Workspace and run-scoped file metadata independent of backing store. |

The division of authority across these domains is governed by a small set of
ownership rules, and they hold consistently across the platform:

- Relational metadata is the authoritative control plane.
- Object storage is authoritative for file bytes, but not for the file
  inventory.
- MLflow is authoritative for prompt registry versions and traces, but not for
  CALIBER-specific workflow, tool, or skill metadata.
- Route handlers and workers are both permitted to mutate the same tables, so
  the code coordinates through explicit status transitions, audit rows, and
  durable timeline events rather than through in-memory state. Durability is
  the synchronization mechanism precisely because the two writers do not share a
  process-local view.

Versioning is a cross-artifact UI concern, not one shared persistence or release
contract. The normalized frontend model maps several deliberately different
idioms:

| Artifact | History and liveness | Gate and rollback semantics |
| --- | --- | --- |
| Prompt | Immutable MLflow registry versions behind an alias such as `@prod` | The prompt panel shows a persisted advisory verdict. Audited promote records the exact outgoing alias target; rollback restores that recorded target. |
| Workflow | Editable drafts become published workflow-version rows; deployment aliases select a published version | Promotion evaluates the workflow deploy-gate policy and uses an optimistic alias check. Rollback pops the deployment's recorded checkpoint stack. |
| Knowledge base | Immutable build versions behind `active_version_id` | No prompt-style gate verdict. Activation and rollback are audited; rollback derives the prior active build from activation history. |
| Skill | A mutable current skill plus immutable `CaliberSkillVersion` snapshots | No alias, promotion, or gate. Rollback restores the prior snapshot as a new current version. |
| Test set | A dataset version counter plus example validity intervals (`dataset_version` / `superseded_version`) | Version filtering preserves historical example sets; there is no live alias or generic rollback action. |
| Tool | Separate `(name, version)` registry rows with lifecycle status | Read-only family history in the version panel; no live alias, gate, or rollback. |

The shared `VersionPanel` is mounted for prompts, workflows, knowledge bases,
skills, and tools through per-artifact adapters; sharing the component does not
make their guarantees uniform. The read-only `/releases/timeline` aggregates
audited release actions, while `/releases/live` enumerates the database-backed
workflow deployments and active knowledge-base versions. Prompt liveness remains
in MLflow and is shown on the per-prompt page rather than inferred by that
aggregate.

## 5. API and interaction surfaces

State is reached exclusively through the HTTP surface. That surface is
centralized in `routes/__init__.py`, which registers each feature module under
the same-origin AJAX namespace. The endpoints below are representative rather
than exhaustive:

- `/prompts`
- `/tools`
- `/skills`
- `/mcp-servers`
- `/agents`
- `/workflows`
- `/workflow-versions/*`
- `/workflow-runs/*`
- `/assistant/*`
- `/aria/plans/*` (Aria goal-plan orchestration)
- `/judges`, `/review-queues`, `/eval-datasets`, `/gate-verdicts/*`
- `/releases/timeline`, `/releases/live` (cross-artifact releases & rollback)

On the client side, the corresponding entry points are organized as page
modules, each of which owns a single feature surface:

- `caliber/caliber-ui/src/pages/Prompts.tsx`
- `caliber/caliber-ui/src/pages/ToolRegistry.tsx`
- `caliber/caliber-ui/src/pages/Skills.tsx`
- `caliber/caliber-ui/src/pages/McpServers.tsx`
- `caliber/caliber-ui/src/pages/Agents.tsx`
- `caliber/caliber-ui/src/pages/AgentDetail.tsx`
- `caliber/caliber-ui/src/pages/Workflows.tsx`
- `caliber/caliber-ui/src/pages/WorkflowEditor.tsx`
- `caliber/caliber-ui/src/pages/Settings.tsx`

The frontend shell never talks to databases or providers directly. Every user
action routes through the CALIBER HTTP surface, even when the eventual effect is
a provider call, an object-store mutation, or a workflow enqueue. Keeping the
client behind a single API boundary is what allows authorization, scoping, and
audit to be enforced uniformly in one place.

The top bar exposes the active workspace rather than leaving project scoping as
an API-only header. An operator can create and immediately select a project;
the API client then sends its id as `X-CALIBER-Project`, invalidates scoped
queries, and uses that project for newly created workflows and managed files.

## 6. Execution lifecycle

The API surface admits two shapes of work: actions that complete on the request
path, and actions that are durably queued for a background worker. The sequence
below shows both branches and the point at which they diverge.

```mermaid
sequenceDiagram
    participant U as User
    participant UI as React SPA
    participant RT as Route handler
    participant DB as SQLAlchemy DB
    participant BUS as Event bus
    participant WK as Background worker
    participant EXT as External subsystem

    U->>UI: Interact with CALIBER page
    UI->>RT: Same-origin API request
    RT->>RT: Auth, scope, CSRF, validation
    RT->>DB: Persist or read control-plane state
    alt Request-path action only
        RT->>EXT: Call MLflow, sandbox, storage, or gateway
        RT-->>UI: Return response
    else Queued background action
        RT->>BUS: Publish event if configured
        RT-->>UI: Return accepted / queued response
        WK->>DB: Claim queued work
        WK->>EXT: Execute provider/runtime work
        WK->>DB: Persist final state, events, and summaries
    end
```

The same lifecycle plays out at three scales — process startup, individual
request handling, and shutdown — and each has well-defined checkpoints:

- At application startup, configuration is loaded and validated, logging and
  tracing are configured, the engine, providers, storage, and event bus are
  built, and the workers are started by the Starlette lifespan manager.
- During request handling, the route module validates the headers, body, and
  query, uses the `app.state` dependencies together with a DB session, and
  either returns directly or enqueues durable work.
- At shutdown, workers receive a stop signal and bounded grace before the event bus
  and SQLAlchemy engine are torn down. Most loops settle or release their durable
  claims. Tool calibration immediately fences its active generation, waits at most its
  grace for the tracked drain, and performs no stop-time database settlement. An interrupted
  claim may remain visibly `running`/ambiguous, while the retained fence rejects any late
  terminal persistence. This is bounded shutdown rather than an unconditional drain guarantee.

## 7. Security and trust boundaries

CALIBER implements its own default session login in both deployment topologies.
Credentials are checked against scrypt hashes in the account table,
and successful login creates a revocable server-side session carried by an HttpOnly
cookie. The product default
`CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT=false` does not create a known
credential. The native launcher opts into `admin` / `admin` only when MLflow binds
to loopback, and the bundled Compose stack opts in only while every published port
is pinned to `127.0.0.1`; the operator must replace that credential immediately.
The bootstrap runs only while the account table is empty and never resets an
existing account. Any network-reachable deployment must keep the opt-in false,
configure a strong bootstrap password source, and use TLS/Secure cookies. Normal
account creation and reset continue to reject `admin` and other weak passwords.

An installation that already has an identity-aware proxy can explicitly select
`trusted_header` mode instead. In that mode, and only that mode, request headers
provide the identity:

- The `X-CALIBER-User` header identifies the caller.
- The `X-CALIBER-Project` header supplies the active project for multi-project
  scoping.
- `auth.py` resolves the `viewer`, `operator`, `approver`, and `admin` scopes.
- Route handlers call `require_user()` or `require_scopes()` before any
  mutation.

The shipped local launchers assign `admin` every scope. Before exposing a deployment,
set `CALIBER_BOOTSTRAP_PASSWORD` to a strong value before first boot or reset the
password in Administration; password changes revoke the account's existing sessions.

Layered over this identity model is a set of controls that protect both the
request path and the data it touches:

- CSRF protection guards browser-driven state changes.
- Optional rate-limiting middleware bounds request volume.
- Project and visibility filters are applied at query time.
- Audit logging records durable mutations.
- Secrets are referenced indirectly through `*_source` fields rather than
  carried as raw secret material.
- Manifest validation rejects inline secrets in workflow definitions.
- User-authored Python Code, Aria draft tests, and registered-tool execution use
  a short-lived local subprocess with an empty environment, private working
  directory, bounded output, process-group termination, and best-effort POSIX
  resource limits. The child signals ready and waits for the parent to start
  authored work; the parent then owns the exact runtime deadline and kills the
  process group, while an uncatchable child watchdog independently covers
  source/module resolution, inspection, every test case, invocation, result
  representation, and serialization. A positive startup grace is a separate
  pre-ready allowance; with zero grace, startup consumes the caller's overall
  budget. Sandbox HTTP execution runs off the ASGI event loop. This is process
  containment, not a container, VM, or kernel sandbox.

The dominant trust boundary runs between control-plane state and code execution.
Route handlers may accept user-authored manifests, prompt text, tool metadata,
and skill content, but only specific runtime paths are permitted to execute code
or make external calls. The local subprocess contains Python Code, Aria draft,
and default registered-tool execution; it still retains ambient host filesystem
and network authority. `external_app` callables remain trusted-operator code and
need a separate worker/container boundary before mutually untrusted authors can
use them safely. MCP admission is separately mediated by command/host allowlists
and deployment preflight; local stdio containment is likewise not an OS sandbox.

## 8. Observability and operations

Operating CALIBER depends on an observability spine that is shared across every
module rather than reimplemented per feature. Its core pieces are:

- structured JSON logging in `observability/logging.py`;
- guarded MLflow tracing in `observability/mlflow_tracing.py`;
- request trace-ID propagation in `observability/trace.py`;
- Prometheus metrics exposed on the `/metrics` route;
- workflow-run events and checkpoints, which serve as durable operational
  timeline state.

The operational design choices behind these pieces favor graceful degradation,
so that observability and serving infrastructure fail soft rather than taking
the request path down with them:

- Logging defaults to stderr, with optional mirroring to S3 or the Object Store.
- Tracing is on-but-inert by default and degrades to a no-op when MLflow tracing
  support is unavailable.
- Event publication is best-effort and must never break the request path.
- Static UI serving degrades gracefully, returning a 503 with an
  operator-facing message when the bundle is absent.

## 9. Extension points and current constraints

CALIBER is built to be extended along its existing seams rather than rewired.
The primary extension points follow the layering described earlier:

- A new route module is added and registered in `routes/__init__.py`.
- A new provider adapter is added and wired through `server.py`.
- New worker loops are added into the lifespan graph.
- New storage or event-bus backends are added behind the existing abstractions.
- New pages extend the React SPA under `caliber-ui/src/pages`.

Set against those seams are the architectural constraints the system currently
accepts, stated plainly so that future work can weigh them deliberately:

- Embedded mode couples CALIBER and MLflow to one process failure domain; standalone
  mode trades that coupling for an HTTP dependency and a second service process.
- SQLAlchemy usage is sync throughout the server.
- Many feature modules still perform orchestration directly in the route files
  rather than through deeper service layers.
- Several domains intentionally use hidden runtime identities
  (`prompt_targets`, `skill_targets`) so that they fit into the shared
  refinement machinery.
- Workflow execution and assistant logic remain substantially in-process, even
  though both are structured well enough to be extracted later.

Taken together, the picture is consistent: CALIBER is one ASGI control-plane
codebase, deployable embedded or standalone, with feature modules layered on a common runtime substrate. The
per-feature documents take it from here, describing how each major bounded
context plugs into that substrate.
