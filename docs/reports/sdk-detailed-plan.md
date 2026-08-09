# CALIBER SDK Strategy + Detailed Implementation Plan (Developer-First)

Status: Draft (design document; no code changes implied by this doc)

Repository baseline: `c65b76790` (caliber-suite `main`)

> The original baseline recorded here, `2ef4a6124`, was the pre-squash tip of the
> PR #37 branch. That branch was deleted when the PR was squash-merged, so the
> SHA resolves for nobody after a `git fetch --prune`. `c65b76790` is the
> squashed commit of the same work, and it is on `main`.

Last updated: 2026-08-09

---

## 0) Why this document exists

CALIBER today is primarily operated through the UI, but the repository already contains:

- A large, consistent HTTP surface under `"/ajax-api/2.0/mlflow/caliber/*"` (Starlette routes).
- Several clean internal protocol seams for extensibility (LLM provider, eval provider, storage backend, promoter).
- A first-class “deploy workflow as a service” surface that already emits OpenAPI per workflow.

This design proposes a developer-first SDK strategy that lets developers:

1) Integrate with CALIBER programmatically from their own applications (client SDK).
2) Extend CALIBER with custom components without forking (plugin SDK).
3) Build reliable operational automation (CLI built on the SDK).

Deliverable: a concrete architecture + interface spec + gap analysis + roadmap.

---

## 1) Goals / Non-goals

### Goals

- Provide a stable, typed, documented SDK surface for programmatic management:
  prompts, skills, tools, workflows/versions/runs, eval assets, projects/files, integrations (MCP, gateway), releases, observability.
- Provide a versioned plugin interface that allows third parties to add:
  optimizers, judges, tools, storage backends, and (later) provider adapters.
- Make SDK usage ergonomic in developer environments:
  local scripts, CI pipelines, notebooks, and “ops automation”.
- Preserve CALIBER’s safety posture:
  CSRF/RBAC boundaries remain enforced; secrets remain write-only; audit trails remain intact.
- Reuse the existing CALIBER API surface as the SDK foundation.
  The SDK should normalize and package the current platform capabilities rather than require a backend rewrite.

### SDK design principles

The SDK should follow these platform-level principles:

1) **Simple first**
   - Common tasks should be easy with minimal code.
   - The first path should be obvious: create client, authenticate, scope project, call a resource API.

2) **Consistent APIs**
   - The same naming and behavior should be used across the SDK.
   - Pagination, filtering, idempotency, retries, and errors should follow uniform patterns.

3) **Clear separation of concerns**
   - Transport, auth, models, retries, utilities, and resource modules should remain separate.
   - The SDK should not leak UI implementation details into the public developer surface.

4) **Stable and versioned**
   - The SDK must have a clear versioning and deprecation policy.
   - Additive platform growth should not force rewrites for developers.

5) **Predictable behavior**
   - Errors, defaults, retries, and side effects should be explicit and uniform.
   - Method names should make mutations and long-running operations obvious.

6) **Good developer experience**
   - Strong docs, examples, types, autocomplete, and sensible defaults are part of the contract.

7) **Composable and extensible**
   - The SDK should support both simple one-call workflows and advanced multi-step automation.
   - Low-level access should remain available without undermining the high-level API.

8) **Platform-aligned, not UI-bound**
   - The SDK should expose the real CALIBER platform, not only the subset currently optimized for the UI.

9) **Secure by default**
   - Auth, scoping, CSRF, secret handling, and redaction should be safe out of the box.

10) **Observable and debuggable**
    - Exceptions should include status, detail, and request context when available.
    - Logging and tracing hooks should be easy to enable.

11) **Portable and integration-friendly**
    - The SDK should work cleanly in scripts, services, notebooks, and CI/CD.
    - It should not depend on the full CALIBER server runtime.

12) **Backward compatible and future-ready**
    - The architecture should allow new route groups and new capabilities without redesigning the client.

### Non-goals (for the first SDK release)

- Rewriting CALIBER’s server framework (e.g., migrating Starlette routes wholesale to FastAPI).
- Rewriting or replacing existing CALIBER APIs just to make the SDK possible.
  The preferred path is to formalize and wrap the current HTTP contracts, then improve them incrementally.
- Guaranteeing backward compatibility for every internal endpoint immediately.
- Designing a public marketplace UX (that is a product initiative; the SDK is the foundation).

---

## 2) Current repository & API analysis (what’s already here)

### 2.1 Repository boundaries (what ships today)

- Python package boundary is `caliber/pyproject.toml` (distribution name `caliber-suite`).
  - The MLflow plugin entrypoint is `caliber.server:create_app` (`[project.entry-points."mlflow.app"]`).
- UI is a separate React/Vite project at `caliber/caliber-ui/`.
- The Python distribution can include the built SPA bundle under `caliber/src/caliber/ui` when CI stages `caliber-ui/dist` into it (see `caliber/pyproject.toml` hatch artifacts).

Implication: a developer-first SDK should not require shipping the entire server distribution (MLflow + SQLAlchemy + Starlette) to run in a developer script. The SDK should be separately packaged.

### 2.2 Public HTTP surface (how the UI already talks to the backend)

Route registration is centralized in `caliber/src/caliber/routes/__init__.py` (good: auditable).

Key global contracts:

- API base path: `"/ajax-api/2.0/mlflow/caliber"` (same-origin with MLflow).
- SPA base path: `"/caliber/"` served via `caliber/src/caliber/routes/static.py`.
- Envelope: most JSON responses are wrapped as `{"data": ...}` via `envelope_response` in `caliber/src/caliber/routes/_deps.py`.
- Errors: `HTTPException` is rendered as JSON with shape `{"detail": ..., "status_code": ...}` via `caliber/src/caliber/routes/_errors.py`.
- Validation errors: structured 400 with `errors: [{loc,msg,type},...]` in `caliber/src/caliber/routes/_errors.py`.

The frontend already codifies these conventions in `caliber/caliber-ui/src/api/caliberApi.ts`:

- Unwraps `{"data": ...}`.
- Converts non-2xx into a typed `ApiError` with parsed body.
- Adds default GET timeouts to prevent “infinite loading skeleton” behavior.
- Handles CSRF bootstrap via `GET /csrf` and sends `X-CALIBER-CSRF` on writes.
- Sends `X-CALIBER-Project` for workspace scoping (projects/workspaces).

This is an important design signal: CALIBER already has an internal SDK shape.
The external SDK should generalize and stabilize that existing API contract for developers rather than invent a parallel backend surface.

### 2.3 Authentication and scoping (what SDKs must support)

`caliber/src/caliber/auth.py` defines identity resolution order:

1) Validated session token (cookie or `Authorization: Bearer …`)
2) Trusted header mode (`X-CALIBER-User`), optionally with proxy secret (`X-CALIBER-Proxy-Secret`)
3) Dev fallback (only in trusted-header mode; config-gated)
4) Anonymous

Important SDK takeaways:

- “API client” authentication is already supported via `Authorization: Bearer <session-token>`.
  The UI usually uses the HttpOnly cookie; an SDK will usually use the Bearer form.
- Multi-user scoping uses `X-CALIBER-Project` to select an active project/workspace.
- Writes may require CSRF protection (depending on deployment settings). Token is issued from:
  `GET /ajax-api/2.0/mlflow/caliber/csrf` and sent as `X-CALIBER-CSRF`.

### 2.4 Existing typed request/response models (what we can reuse)

The server already defines many Pydantic models in `caliber/src/caliber/schemas.py`, including:

- Agents: `Agent*Schema` + create/update requests
- Tools: `ToolSchema`, `ToolRegisterRequest`, `ToolUpdateRequest`, test/cases/calibration schemas
- Skills: `SkillSchema`, `SkillCreateRequest`, `SkillUpdateRequest`, test/cases/calibration, skill package schemas
- Workflows: `WorkflowSchema`, versions, runs, deployments, benchmarks, templates, components
- Eval datasets: `EvalDatasetSchema`, create/update requests
- Judges: `JudgeSchema`, create/update requests, test/alignment schemas
- MCP servers: `McpServerSchema`, create/update, tools/policy/testcases/calibration schemas
- Workflow services: `WorkflowServiceSchema`, tokens, and service invoke schemas

Gaps exist: some endpoints still return plain dict payloads (not modeled), which makes OpenAPI generation and SDK typing harder (see §7).

### 2.5 Extensibility seams (internal protocols already present)

Several internal protocol seams are already clean and are the foundation for a plugin SDK:

- LLM provider: `caliber/src/caliber/llm/provider.py` (`LLMProvider` protocol + `build_provider`)
- Artifact store: `caliber/src/caliber/artifact_store.py` (`ArtifactStore` protocol + `build_store`)
- Eval provider: `caliber/src/caliber/eval/provider.py` (`EvalProvider` protocol + `build_provider`)
- Promoter: `caliber/src/caliber/promoter.py` (`Promoter` protocol + `build_promoter`)
- Storage backend: `caliber/src/caliber/storage/service.py` (`StorageBackend` + `build_backend`)
- Event bus backends: `caliber/src/caliber/events/nats_bus.py` (`build_event_bus`)

These are currently selected via config strings and implemented with hard-coded `if provider == ...` branches. That is a good internal seam but not yet a third-party extension model.

### 2.6 Workflow service OpenAPI (a strong precedent)

`caliber/src/caliber/routes/services.py` already:

- Manages workflow “service” configuration and tokens (internal surface).
- Exposes an external invocation API authenticated by per-service tokens.
- Serves an OpenAPI 3.0 document per workflow at:
  `GET /ajax-api/2.0/mlflow/caliber/services/{workflow_id}/openapi.json`

This is a strong pattern to replicate for CALIBER’s management API: a served OpenAPI document enables SDK generation, Postman imports, and strongly typed clients.

---

## 3) Proposed SDK strategy (two SDKs + a CLI)

### 3.1 The three developer surfaces

1) **Client SDK (remote, typed)** — integrates with a running CALIBER deployment via HTTP.
   - Primary language: Python (first).
   - Optional later: TypeScript (mirrors UI, supports Node apps).
   - Built on the current CALIBER management APIs and service APIs, with compatibility wrappers where contracts are uneven today.

2) **Plugin SDK (in-process, versioned)** — extends the CALIBER server with new implementations:
   optimizers, judges, tools, storage backends, provider adapters.

3) **CLI (thin, scriptable)** — `caliber` / `caliberctl` built on the client SDK for:
   CI automation, operational procedures, bulk management.

### 3.2 Packaging recommendation (keeps dependencies sane)

Create two new Python distributions in this repository under a top-level `sdk/`:

- `caliber-sdk` (lightweight):
  - deps: `httpx`, `pydantic`, `typing-extensions` (and minimal helpers).
  - no `mlflow`, no `sqlalchemy`, no `starlette`.
  - shipped for external developers and automation.

- `caliber-plugin-sdk` (also lightweight, but server-facing):
  - deps: `pydantic`, `typing-extensions` (plus small utilities).
  - defines the stable interfaces and contexts for server plugins.

Proposed repo layout (not currently present; implementation plan in §8):

```text
caliber-suite/
  caliber/            # the server distribution (caliber-suite)
  sdk/
    caliber-sdk/
      pyproject.toml
      src/caliber_sdk/
    caliber-plugin-sdk/
      pyproject.toml
      src/caliber_plugin_sdk/
```

Use hyphenated names for the published distributions (`caliber-sdk`, `caliber-plugin-sdk`) and underscored names for the Python import packages (`caliber_sdk`, `caliber_plugin_sdk`).

#### Why `sdk/` is a repository-root sibling, not `caliber/sdk/`

An earlier draft nested these inside `caliber/`. That directory is itself a
distribution root, and nesting distributions inside another distribution's root
breaks in three measured ways:

1. **The server sdist would ship both SDKs.** `[tool.hatch.build.targets.sdist]`
   in `caliber/pyproject.toml` excludes caches, venvs, and build outputs — it has
   no `sdk` entry. Hatchling would therefore include `caliber/sdk/**` in the
   `caliber-suite` source distribution, shipping two unrelated distributions'
   sources inside a third. Fixable with another exclude, but only for as long as
   everyone remembers it.
2. **The SDKs would be linted but never type-checked.** CI runs from `caliber/`
   with `ruff check .` (recursive, so it *would* reach `caliber/sdk/**` and apply
   the server's ruff config) but `mypy src` (which would *not*). The result is
   the worst of both: the SDK looks covered by the lint job while its types are
   unchecked.
3. **Config inheritance is accidental rather than chosen.** A separate
   distribution should pick its own lint, typing, and test settings, not inherit
   the server's because of where it happens to sit on disk.

A root-level `sdk/` avoids all three with no exclusions to maintain, and matches
how this repository is already organised: every top-level directory is one
concern (`caliber/`, `deploy/`, `docs/`, `docs-site/`, `paper/`, `scripts/`).
Each SDK then owns its own `pyproject.toml`, and CI adds jobs with an explicit
`working-directory` rather than relying on the repo-wide `caliber` default.

This avoids coupling external consumers to the heavyweight server distribution while allowing the server to depend on the plugin SDK internally.

### 3.3 Stability tiers and contract strategy

The previous draft over-scoped the first public SDK release. To stay aligned with “simple first” and “stable and versioned,” the SDK should publish explicit stability tiers.

Recommended tiers:

Names below are the route modules under `caliber/src/caliber/routes/`, so that
the stability annotation in M0-PR3 has an unambiguous target. Every module
except `static` (which serves the SPA, not an API) appears in exactly one tier.

- **GA in `caliber-sdk` v1**
  - `auth`, `csrf`, `me`, `capabilities`, `settings`
  - `prompts`, `skills`, `tools`, `agents`
  - `workflows`, `workflow_versions`, `workflow_runs`, `workflow_deployments`, `services`
  - `projects`, `files`
  - `eval_datasets`, `evaluations`, `judges`
- **Beta in `caliber-sdk` v1**
  - `mcp_servers`, `gateway`
  - `knowledge_bases`, `object_store`
  - `jobs`, `review_queues`, `aria_plans`
  - `releases`, `rollback`, `workflow_calibration`
  - `observability`, `audit`, `events_stream`
  - `cookbooks`, `secrets`
- **Internal / not part of the public SDK contract**
  - `assistant`, `memory`
  - `dashboard`, `metrics`, `health`
  - `gate_verdicts`, `llm_pricing`
  - `system_effects`, `system_services`

> The workflow surface is five route modules, not one. `workflows` holds the
> container; versions, runs, deployments, and calibration are separate modules.
> An earlier draft named only `workflows`, which would have left four modules
> with no stability annotation while §5.7 still described their operations as
> GA. Calibration is placed in beta rather than GA because it is the queued,
> optimizer-backed path whose semantics differ per asset family.

Contract strategy:

- The SDK should wrap the existing `"/ajax-api/2.0/mlflow/caliber"` routes first.
- The management OpenAPI document should describe the current routes and annotate each operation with stability metadata such as `ga`, `beta`, or `internal`.
- Every public SDK method should map to one or more existing endpoints; if an endpoint shape is awkward, fix the contract incrementally rather than creating a second parallel API during the first SDK release.

### 3.4 SDK documentation delivery (HTML, searchable, tested, published)

SDK documentation is part of the product deliverable, not follow-up polish.

CALIBER already has the right publishing path for this:

- the documentation site is built into `docs-site/`;
- `caliber/caliber-ui/scripts/sync-docs.mjs` syncs the generated site into the UI-served and packaged docs copies;
- `.github/workflows/pages.yml` publishes the composed site to GitHub Pages.

The SDK plan should use that existing path rather than inventing a second docs system.

Required SDK documentation outputs:

1. **Developer landing + quickstart**
   - install, auth, CSRF, project scoping, sync client usage, retries, waiters, and stability tiers.

2. **Searchable API reference in HTML**
   - published into the existing `docs-site` HTML site;
   - searchable by client, resource, method, request model, and response model;
   - each API entry should include:
     - method signature,
     - purpose,
     - mapped backend route(s),
     - auth/scopes expectations,
     - request/response model details,
     - stability tier,
     - at least one working example.

3. **Tested example library**
   - examples must come from executable SDK examples, not hand-written pseudo-code;
   - the docs should either embed those examples directly from source or derive snippets from a tested examples directory so the docs cannot silently drift from working code.

4. **Cookbooks developed with SDK as exemplars**
   - the SDK docs must include cookbook pages that show how to build CALIBER workflows using only the SDK surface;
   - these cookbook pages are part of the developer docs, not separate marketing material;
   - cookbook exemplar coverage should follow the same completeness line as this report:
     current SDK target is 15/16 with Cookbook 04 explicitly blocked by the backend file-reference gap.

5. **GitHub Pages publication**
   - SDK docs must land in the same Pages site the repo already serves;
   - the published output should remain searchable and navigable from the existing docs UI.

Implementation direction:

- add SDK docs sources under `docs/sdk/` (or an equivalently explicit docs subtree inside `docs/`);
- add corresponding generated HTML modules to `docs-site/build-docs.mjs`;
- extend the current docs search/navigation layer in `docs-site/docs.js` and related generated metadata so SDK APIs and cookbook exemplars are discoverable by search;
- keep `docs-site/` as the single published-site source, then continue syncing through `caliber/caliber-ui/scripts/sync-docs.mjs`;
- verify publication through the existing Pages deployment in `.github/workflows/pages.yml`.

Documentation correctness rules:

- examples shown in docs must be tested in CI;
- docs pages must be generated/synced as part of the normal docs pipeline, not manually copied;
- the published Pages site must include the SDK pages and cookbook exemplar pages;
- the docs contract test suite should fail if generated SDK docs or synced copies drift.
- A future alias such as `"/api/caliber/v1"` is optional, not required for the first release.

---

## 4) Client SDK architecture (remote SDK)

### 4.1 Design principles

- “One `CaliberClient`, many resource APIs” (like Stripe/GitHub SDKs).
- Typed models by default; escape hatch for raw JSON when needed.
- Explicit scoping (project header) and explicit auth mode.
- Predictable errors (raise typed exceptions, include server `detail`, `status_code`, headers).
- Safe defaults: conservative timeouts, exponential backoff on 429/503, idempotency keys for ambiguous retries.
- Sync-first delivery for v1. Add an async client only after the core sync contract, streaming model, and file-transfer patterns are stable.

### 4.2 Transport layer

Use `httpx` as the core transport with:

- Base URL handling:
  - Accept a deployment root, e.g. `http://host:5000` (or `https://.../mlflow`).
  - Allow passing `static_prefix` if CALIBER is behind a reverse proxy subpath.
- Envelope:
  - Reads parse `{"data": ...}` and return the inner payload.
  - A strict mode can validate presence of `data` to catch contract drift early.
- Errors:
  - Parse JSON error bodies from `caliber.routes._errors`.
  - Preserve response headers (notably `Retry-After`).
  - Capture request correlation metadata such as `X-Request-Id` or equivalent headers when available.
- Timeouts:
  - Default GET timeout similar to UI (`~20s`) to avoid hanging workflows.
  - Writes default to “no timeout” unless explicitly set (some operations are long-running).
- Retries:
  - Retry safe methods on `502/503/504` with bounded exponential backoff.
  - Retry `429` honoring `Retry-After` if present.
  - Never retry non-idempotent writes unless an idempotency key is provided and the endpoint supports it.
- Hooks and observability:
  - Allow request/response hooks for logging, metrics, and tracing.
  - Send an SDK-specific `User-Agent` so server telemetry can distinguish SDK traffic from the UI.

### 4.3 Authentication providers

Define an interface:

```python
class AuthProvider(Protocol):
    def headers(self) -> dict[str, str]: ...
    def on_response(self, response: httpx.Response) -> None: ...
```

Implementations:

- `BearerSessionAuth(token: str)`:
  - Sets `Authorization: Bearer <token>`.
- `SessionCookieAuth(login: (user,password))`:
  - Calls `POST /auth/login`, stores cookie jar (httpx client cookies).
  - Supports `logout`.
- `TrustedHeaderAuth(user: str, proxy_secret: str|None)`:
  - Sets `X-CALIBER-User` and optional `X-CALIBER-Proxy-Secret`.
- `WorkflowServiceTokenAuth(token: str)`:
  - Uses service tokens for `/services/{workflow_id}/invoke` and `/runs/{run_id}` polling.

CSRF handling:

- Provide `CsrfManager` that can:
  - `GET /csrf` to bootstrap.
  - Cache `{enabled, token, ttl_seconds}`.
  - Auto-refresh on CSRF-shaped 403 failures (like UI does).

### 4.4 Scoping & multi-project support

SDK should let callers set:

- `project_id` header (`X-CALIBER-Project`)
- optional `visibility` query param on list endpoints (`?visibility=project|user|public`)

Ergonomics:

- `client.with_project(project_id)` returns a scoped client.
- `client.projects.select(project_id)` sets default scoping for the instance.
- On initialization, the client should optionally read `/capabilities` once and cache the result so unsupported features fail fast with clear errors instead of late 404/409 surprises.

### 4.5 Resource modules (high-level)

The SDK should expose sub-clients aligned with today’s domains/routes:

- `auth`, `csrf`, `me`, `capabilities`, `settings`
- `prompts`, `skills`, `tools`, `agents`
- `workflows` (including versions, runs, deployments, services)
- `eval_datasets`, `evaluations`, `judges`
- `knowledge_bases`, `object_store`, `projects`, `files`
- `mcp_servers`, `gateway`
- `releases`, `rollback`
- `jobs`, `review_queues`, `aria`, `cookbooks`, `secrets`
- `observability`, `audit`, `events` (SSE)

---

## 5) Client SDK interface design (detailed)

This section specifies the developer-facing APIs. Names match existing domains but can evolve; the core requirement is consistency.

Unless stated otherwise, endpoint paths referenced below are relative to the management API base:

- `API_BASE = /ajax-api/2.0/mlflow/caliber`

### 5.1 Root client

```python
class CaliberClient:
    def __init__(
        self,
        base_url: str,
        *,
        static_prefix: str = "",
        auth: AuthProvider | None = None,
        project_id: str | None = None,
        timeout_get_seconds: float = 20.0,
        timeout_write_seconds: float | None = None,
        retries: RetryPolicy | None = None,
    ): ...

    # Common
    def with_project(self, project_id: str | None) -> "CaliberClient": ...
    def close(self) -> None: ...

    # Sub-clients (each shares the same transport/auth/scoping)
    raw: RawApi
    wait: WaitersApi
    auth: AuthApi
    csrf: CsrfApi
    me: MeApi
    capabilities: CapabilitiesApi
    settings: SettingsApi
    secrets: SecretsApi
    cookbooks: CookbooksApi
    prompts: PromptsApi
    skills: SkillsApi
    tools: ToolsApi
    agents: AgentsApi
    workflows: WorkflowsApi
    services: ServicesApi
    jobs: JobsApi
    review_queues: ReviewQueuesApi
    aria: AriaPlansApi
    mcp_servers: McpServersApi
    gateway: GatewayApi
    projects: ProjectsApi
    files: FilesApi
    knowledge: KnowledgeApi
    object_store: ObjectStoreApi
    eval_datasets: EvalDatasetsApi
    evaluations: EvaluationsApi
    judges: JudgesApi
    releases: ReleasesApi
    rollback: RollbackApi
    observability: ObservabilityApi
    audit: AuditApi
    events: EventsApi
```

### 5.2 Common patterns

#### Envelope & models

- Methods return typed Pydantic models where the server already defines them in `caliber.schemas`.
- For endpoints that currently return dict payloads, SDK returns typed models defined in the SDK package (until the server adopts Pydantic models).

#### Pagination

Today many list endpoints accept `limit` and `offset` (see `list_limit` in `caliber.routes._deps`).

SDK should expose:

- `list(..., limit=..., offset=...)` (thin wrapper)
- `iter_all(...)` (auto-paginates until completion)

Later (recommended): migrate API to cursor pagination; SDK can support both in a compatibility layer.

#### Errors

Raise exceptions like:

- `CaliberApiError(status: int, detail: str, body: dict|None, headers: dict)`
- `CaliberValidationError(errors: list[...])` (maps the structured Pydantic errors)
- `CaliberRateLimitError(retry_after_seconds: float|None)`

Exceptions should also carry request identifiers and the resolved SDK/server capability context when available so failures can be debugged from logs without packet capture.

#### Raw escape hatch

The SDK should include a low-level raw transport API for gaps between SDK coverage and server evolution:

- `client.raw.get(path, *, params=None, headers=None)`
- `client.raw.post(path, *, json=None, data=None, files=None, headers=None)`
- `client.raw.request(...)`

This preserves forward-compatibility without forcing developers to drop down to a separate `httpx` client.

#### Long-running operations

CALIBER already has long-running patterns in workflow runs, services, jobs, evaluations, and release/report jobs. The SDK should standardize polling helpers instead of pushing that complexity to every consumer.

```python
class WaitersApi:
    def for_run(self, run_id: str, *, timeout_seconds: float | None = None, poll_interval_seconds: float = 1.0) -> WorkflowRunSchema: ...
    def for_job(self, job_id: str, *, timeout_seconds: float | None = None, poll_interval_seconds: float = 2.0) -> JobDetailSchema: ...
    def for_service_run(self, workflow_id: str, run_id: str, *, timeout_seconds: float | None = None) -> ServiceRunStatusSchema: ...
    def for_evaluation_run(self, run_id: str, *, timeout_seconds: float | None = None, poll_interval_seconds: float = 2.0) -> EvalRunSchema: ...
    def for_release_report_job(self, report_job_id: str, *, timeout_seconds: float | None = None, poll_interval_seconds: float = 2.0) -> ReleaseReportJobSchema: ...
```

The first release should prefer polling helpers over a fully async API surface. That keeps the common automation path simple while remaining compatible with the existing backend.

### 5.3 Auth & session

Endpoints in `caliber.routes.auth`:

- `POST /auth/login`
- `POST /auth/logout`
- `GET /auth/session`
- admin account CRUD

SDK surface:

```python
class AuthApi:
    def login(self, user_id: str, password: str) -> AuthLoginResult: ...
    def logout(self) -> LogoutResult: ...
    def session(self) -> AuthSessionInfo: ...
    def list_accounts(self) -> UserAccountListResult: ...
    def create_account(self, payload: UserAccountCreateRequest) -> UserAccountResult: ...
    def update_account(self, user_id: str, payload: UserAccountUpdateRequest) -> UserAccountResult: ...
    def revoke_account_sessions(self, user_id: str) -> RevokeSessionsResult: ...
```

Recommendation (gap): add Personal Access Tokens (PATs) with scopes so SDK users don’t need password-based login in automation (see §7).

### 5.4 Prompts API (MLflow prompt registry + CALIBER workspace helpers)

Server routes: `caliber.routes.prompts` (selected endpoints):

- `GET/POST /prompts`
- `GET /prompts/{name}`
- `GET /prompts/{name}/versions` and `GET /prompts/{name}/versions/{version}`
- `PUT /prompts/{name}/aliases/{alias}`
- `POST /prompts/{name}/rollback`
- `GET /prompts/template-library` + `POST /prompts/template-library/preview`
- Optimization and calibration helpers, workspace/bind/baseline endpoints

SDK surface:

```python
class PromptsApi:
    def list(self, *, limit: int = 500, offset: int = 0) -> list[PromptInfo]: ...
    def get(self, name: str, *, alias: str = "prod") -> PromptDetail: ...
    def create(self, *, name: str, template: str, tags: dict[str,str]|None=None) -> PromptCreateResult: ...
    def create_version(self, name: str, payload: PromptVersionCreateRequest) -> PromptCreateResult: ...
    def list_versions(self, name: str) -> list[PromptVersionInfo]: ...
    def get_version(self, name: str, version: int) -> PromptVersionDetail: ...
    def set_alias(self, name: str, alias: str, version: int, *, idempotency_key: str|None=None) -> PromptAliasResult: ...
    def rollback(self, name: str, *, alias: str = "prod") -> PromptRollbackResult: ...
    def test_render(self, name: str, variables: dict[str, str]) -> PromptRenderResult: ...
    def calibration_options(self) -> PromptCalibrationOptions: ...
    def create_calibration_run(self, payload: PromptCalibrationRunRequest) -> PromptCalibrationRunResult: ...
    def optimization_options(self) -> PromptOptimizationOptions: ...
    def create_optimization_run(self, payload: PromptOptimizationRunRequest) -> PromptOptimizationRunResult: ...
    def save_test_run(self, payload: PromptTestRunCreateRequest) -> PromptTestRunSummary: ...
    def list_test_runs(self, *, name: str | None = None, limit: int = 50) -> list[PromptTestRunSummary]: ...
    def get_test_run(self, test_run_id: str) -> PromptTestRunDetail: ...

    # “workspace” helpers (pytest-for-prompts)
    def workspace(self, name: str) -> PromptWorkspaceResponse: ...
    def bind(self, name: str, payload: PromptBindRequest) -> PromptBindResult: ...
    def baseline(self, name: str, payload: PromptBaselineRequest) -> BaselineResult: ...
```

Gap: prompt list/detail/version payloads are not currently modeled in `caliber.schemas` (they are currently dict-oriented). To fully type the SDK, formalize these in server schemas.

### 5.5 Skills API (registry + packaging)

Server routes: `caliber.routes.skills`, plus `caliber.skill_packages`.

SDK should cover:

- CRUD + archive/restore
- test runs and calibration
- package export/import (JSON files + ZIP)

```python
class SkillsApi:
    def list(self, *, status: str = "active", visibility: str|None=None, limit: int = 500, offset: int = 0) -> list[SkillSchema]: ...
    def get(self, skill_id: str) -> SkillSchema: ...
    def create(self, payload: SkillCreateRequest) -> SkillSchema: ...
    def update(self, skill_id: str, payload: SkillUpdateRequest) -> SkillSchema: ...
    def rollback(self, skill_id: str) -> SkillSchema: ...
    def list_versions(self, skill_id: str) -> list[SkillVersionInfo]: ...
    def test_render(self, skill_id: str, variables: dict[str, str]) -> SkillRenderResult: ...
    def test_selection(self, skill_id: str, payload: SkillSelectionRequest) -> SkillSelectionResult: ...
    def workspace(self, skill_id: str) -> SkillWorkspaceResponse: ...
    def save_test_run(self, payload: SkillTestRunCreateRequest) -> SkillTestRunSummary: ...
    def list_test_runs(self, *, skill_id: str | None = None, kind: str | None = None, limit: int = 50) -> list[SkillTestRunSummary]: ...
    def get_test_run(self, test_run_id: str) -> SkillTestRunDetail: ...
    def set_baseline(self, skill_id: str, test_run_id: str) -> BaselineResult: ...
    def bind(self, skill_id: str, payload: SkillBindRequest) -> SkillBindResult: ...
    def calibrate(self, skill_id: str, payload: SkillCalibrateRequest | None = None) -> SkillCalibrationRunResult: ...

    def export_package(self, skill_id: str) -> SkillPackageSchema: ...
    def export_package_zip(self, skill_id: str) -> bytes: ...
    def import_package(self, payload: SkillPackageImportRequest) -> SkillSchema: ...
    def import_package_zip(self, zip_bytes: bytes) -> SkillSchema: ...
```

### 5.6 Tools API (registry + test/cases/calibration + source fetch)

Server routes: `caliber.routes.tools`, plus runtime resolution in `caliber.workflows.tools`.

Key SDK needs:

- Tool CRUD (admin)
- Test runs
- Test cases update/list
- Calibration flows and job resolution
- Source retrieval (bounded, safe)

```python
class ToolsApi:
    def list(self, *, status: str = "active", visibility: str|None=None, limit: int = 500, offset: int = 0) -> list[ToolSchema]: ...
    def get(self, tool_id: str) -> ToolSchema: ...
    def list_versions(self, tool_id: str) -> list[ToolSchema]: ...
    def register(self, payload: ToolRegisterRequest) -> ToolSchema: ...
    def update(self, tool_id: str, payload: ToolUpdateRequest) -> ToolSchema: ...
    def archive(self, tool_id: str) -> ToolSchema: ...
    def usage(self, tool_id: str) -> ToolUsage: ...
    def source(self, tool_id: str) -> ToolSource: ...
    def workspace(self, tool_id: str) -> ToolWorkspaceResponse: ...

    def create_test_run(self, tool_id: str, payload: ToolTestRunCreateRequest) -> ToolTestRunDetail: ...
    def list_test_runs(self, *, limit: int = 20, offset: int = 0) -> list[ToolTestRunSummary]: ...
    def get_test_run(self, test_run_id: str) -> ToolTestRunDetail: ...
    def save_test_run(self, payload: ToolTestRunCreateRequest) -> ToolTestRunSummary: ...
    def set_baseline(self, tool_id: str, test_run_id: str) -> BaselineResult: ...

    def get_test_cases(self, tool_id: str) -> ToolTestCasesResponse: ...
    def update_test_cases(self, tool_id: str, payload: ToolTestCasesUpdateRequest) -> ToolTestCasesResponse: ...
    def calibrate(self, tool_id: str) -> ToolCalibrationResult: ...
    def submit_calibration_job(self, tool_id: str) -> ToolCalibrationJob: ...
    def list_calibration_jobs(self, tool_id: str) -> ToolCalibrationJobsResponse: ...
    def resolve_calibration_job(self, tool_id: str, job_id: str, payload: ToolCalibrationJobResolveRequest) -> ToolCalibrationJobResolution: ...
```

### 5.7 Workflows API (container + versions + runs + deployments + services)

Workflows are split across multiple route modules:

- `caliber.routes.workflows`: workflow container CRUD, templates/components, import preview, cron preview, benchmarks, session memory.
- `caliber.routes.workflow_versions`: draft lifecycle (validate/compile/publish), export, preview-run, propose patch, copilot edit, plan build.
- `caliber.routes.workflow_runs`: create/run control plane operations (cancel/retry/resume/approvals), lineage/trace/events.
- `caliber.routes.workflow_deployments`: deployments/promotions.
- `caliber.routes.services`: deploy-as-a-service, tokens, invoke/poll, service OpenAPI.

SDK approach:

- Provide a top-level `WorkflowsApi` for the container.
- Provide nested APIs (or methods) for versions, runs, deployments, services.

```python
class WorkflowsApi:
    def list(self, *, status: str="active", visibility: str|None=None, limit: int=500, offset: int=0) -> list[WorkflowSchema]: ...
    def get(self, workflow_id: str) -> WorkflowSchema: ...
    def create(self, payload: WorkflowCreateRequest) -> WorkflowSchema: ...
    def update(self, workflow_id: str, payload: WorkflowUpdateRequest) -> WorkflowSchema: ...
    def archive(self, workflow_id: str) -> WorkflowSchema: ...
    def list_components(self) -> WorkflowComponentCatalog: ...
    def list_templates(self) -> WorkflowTemplateCatalog: ...
    def preview_cron(self, expr: str, *, timezone: str | None = None, count: int | None = None) -> WorkflowCronPreview: ...

    def list_versions(self, workflow_id: str, *, limit: int=500, offset: int=0) -> list[WorkflowVersionSchema]: ...
    def get_version(self, version_id: str) -> WorkflowVersionSchema: ...
    def create_version(self, workflow_id: str, payload: WorkflowVersionCreateRequest) -> WorkflowVersionSchema: ...
    def update_version(self, version_id: str, payload: WorkflowVersionUpdateRequest, *, if_match_manifest_hash: str|None=None) -> WorkflowVersionSchema: ...
    def validate_version(self, version_id: str) -> ValidationReport: ...
    def compile_version(self, version_id: str) -> CompileResult: ...
    def publish_version(self, version_id: str) -> PublishResult: ...
    def restore_version(self, version_id: str) -> WorkflowVersionSchema: ...
    def diff_versions(self, base_version_id: str, other_version_id: str) -> WorkflowVersionDiff: ...
    def preview_run(self, version_id: str, payload: PreviewRunRequest) -> WorkflowRunResult: ...
    def run_version(self, version_id: str, payload: WorkflowVersionRunRequest) -> WorkflowRunSchema: ...
    def propose_patch(self, version_id: str, payload: WorkflowPatchProposalRequest) -> WorkflowPatchProposal: ...
    def copilot_edit(self, version_id: str, payload: WorkflowCopilotEditRequest) -> WorkflowVersionSchema: ...
    def plan_build(self, version_id: str, payload: WorkflowPlanBuildRequest) -> WorkflowPlanBuildResult: ...
    def export_manifest(self, version_id: str) -> dict[str, object]: ...
    def export_python(self, version_id: str) -> str: ...
    def calibration_options(self, workflow_id: str) -> WorkflowCalibrationOptions: ...
    def create_calibration_run(self, workflow_id: str, payload: WorkflowCalibrationRunRequest) -> WorkflowCalibrationRunResult: ...

    def create_run(self, payload: WorkflowRunCreateRequest) -> WorkflowRunSchema: ...
    def get_run(self, run_id: str) -> WorkflowRunSchema: ...
    def get_run_by_trace(self, trace_id: str) -> WorkflowRunSchema: ...
    def cancel_run(self, run_id: str, payload: WorkflowRunCancelRequest) -> WorkflowRunSchema: ...
    def retry_run(self, run_id: str, payload: WorkflowRunRetryRequest) -> WorkflowRunSchema: ...
    def resume_run(self, run_id: str, payload: WorkflowRunResumeRequest) -> WorkflowRunSchema: ...
    def resume_run_by_event(self, payload: WorkflowRunResumeByEventRequest) -> WorkflowRunSchema: ...
    def list_checkpoints(self, run_id: str) -> list[WorkflowRunCheckpointSchema]: ...
    def approve(self, run_id: str, payload: WorkflowRunApprovalDecisionRequest) -> WorkflowRunSchema: ...
    def reject(self, run_id: str, payload: WorkflowRunApprovalDecisionRequest) -> WorkflowRunSchema: ...
    def trace(self, run_id: str) -> WorkflowRunTraceSchema: ...
    def events(self, run_id: str) -> list[WorkflowRunEventSchema]: ...

    def list_deployments(self, workflow_id: str) -> list[WorkflowDeploymentSchema]: ...
    def promote(self, workflow_id: str, alias: str, payload: PromoteRequest) -> PromoteResult: ...
    def rollback(self, workflow_id: str, alias: str) -> WorkflowDeploymentSchema: ...

class ServicesApi:
    def get_service(self, workflow_id: str) -> WorkflowServiceSchema: ...
    def publish_service(self, workflow_id: str, payload: WorkflowServicePublishRequest) -> WorkflowServiceSchema: ...
    def unpublish_service(self, workflow_id: str) -> WorkflowServiceSchema: ...
    def create_token(self, workflow_id: str, payload: ServiceTokenCreateRequest) -> ServiceTokenCreatedSchema: ...
    def list_tokens(self, workflow_id: str) -> list[ServiceTokenSchema]: ...
    def revoke_token(self, workflow_id: str, token_id: str) -> ServiceTokenSchema: ...
    def openapi(self, workflow_id: str) -> dict[str, object]: ...

    # External invocation surface
    def invoke(self, workflow_id: str, payload: ServiceInvokeRequest, *, idempotency_key: str|None=None) -> ServiceInvokeResponse: ...
    def run_status(self, workflow_id: str, run_id: str) -> ServiceRunStatusSchema: ...
```

### 5.8 Projects & files API

Projects/workspaces are managed in `caliber.routes.projects` and files are managed in:

- `caliber.routes.files`: run-scoped uploads and downloads, playground runs.
- `caliber.routes.projects`: project-scoped file browsing independent of runs.

SDK should expose first-class helpers for multipart upload:

```python
class ProjectsApi:
    def list(self) -> list[Project]: ...
    def create(self, *, name: str, description: str="") -> Project: ...
    def get(self, project_id: str) -> Project: ...
    def update(self, project_id: str, **fields) -> Project: ...
    def archive(self, project_id: str) -> Project: ...

    def list_files(self, project_id: str, *, prefix: str="") -> list[WorkflowFile]: ...
    def upload_file(self, project_id: str, *, path: str, data: bytes, kind: str="input", media_type: str|None=None, metadata: dict|None=None) -> WorkflowFile: ...
    def download_file(self, project_id: str, file_id: str) -> bytes: ...

class FilesApi:
    def upload_run_file(self, run_id: str, *, path: str, data: bytes, kind: str="input", ...) -> WorkflowFile: ...
    def list_run_files(self, run_id: str) -> WorkflowFileList: ...
    def download_run_file(self, run_id: str, file_id: str) -> bytes: ...
```

Gap: some project/file payloads are dicts rather than server Pydantic models; formalize for full typing.

### 5.9 MCP Servers & Gateway integrations

MCP is a major integration surface with additional policy and secret-handling constraints (`caliber.routes.mcp_servers`, `caliber.mcp_policy`, `caliber.mcp_secrets`).

SDK must preserve:

- Write-only secret sentinel behavior for sensitive config fields.
- Ability to:
  list/create/update servers, test connection, discover tools, invoke tools, manage tool policies and test cases.

```python
class McpServersApi:
    def list(self, *, status: str="all", limit: int=500, offset: int=0) -> list[McpServerSchema]: ...
    def get(self, server_id: str) -> McpServerSchema: ...
    def create(self, payload: McpServerCreateRequest) -> McpServerSchema: ...
    def update(self, server_id: str, payload: McpServerUpdateRequest) -> McpServerSchema: ...
    def delete(self, server_id: str) -> DeleteResult: ...
    def history(self, server_id: str) -> list[McpServerHistoryEntry]: ...
    def test_connection(self, server_id: str) -> McpTestConnectionResult: ...
    def discover_tools(self, server_id: str) -> McpDiscoverToolsResponse: ...
    def tools(self, server_id: str) -> McpServerToolsResponse: ...
    def update_tool_policy(self, server_id: str, tool_name: str, payload: McpToolPolicyUpdateRequest) -> McpToolPolicyResult: ...
    def save_tool_test_cases(self, server_id: str, tool_name: str, payload: McpToolTestCasesUpdateRequest) -> McpToolTestCasesResult: ...
    def calibrate_tool(self, server_id: str, tool_name: str) -> McpToolCalibrationResult: ...
    def invoke_tool(self, server_id: str, payload: InvokePayload) -> McpToolInvocationResult: ...
```

### 5.10 Knowledge Bases API (grounding + retrieval)

Knowledge bases are a first-class surface with their own schemas in `caliber/src/caliber/knowledge/schemas.py` and routes in `caliber.routes.knowledge_bases`.

Route constants include:

- `/knowledge-bases/options`
- `/knowledge-bases` + `/knowledge-bases/{knowledge_base_id}`
- `/knowledge-bases/{knowledge_base_id}/versions` + activation/rollback
- `/knowledge/query`
- per-version listing: chunks/entities/relationships/graph and AGE sync

SDK surface:

```python
class KnowledgeApi:
    def options(self) -> KnowledgeOptionsSchema: ...

    def list_bases(self, *, limit: int = 500, offset: int = 0) -> list[KnowledgeBaseSchema]: ...
    def create_base(self, payload: KnowledgeBaseCreateRequest) -> KnowledgeBaseSchema: ...
    def get_base(self, knowledge_base_id: str) -> KnowledgeBaseSchema: ...
    def update_base(self, knowledge_base_id: str, payload: KnowledgeBaseUpdateRequest) -> KnowledgeBaseSchema: ...
    def delete_base(self, knowledge_base_id: str) -> DeleteResult: ...

    def list_versions(self, knowledge_base_id: str) -> list[KnowledgeBaseVersionSchema]: ...
    def get_version(self, version_id: str) -> KnowledgeBaseVersionSchema: ...
    def create_version(self, knowledge_base_id: str, payload: KnowledgeBaseVersionCreateRequest) -> KnowledgeBaseVersionSchema: ...
    def activate_version(self, knowledge_base_id: str, version_id: str) -> KnowledgeBaseSchema: ...
    def rollback(self, knowledge_base_id: str) -> KnowledgeBaseSchema: ...
    def sync_age(self, version_id: str) -> KnowledgeAgeSyncResult: ...
    def list_sources(self, version_id: str, *, limit: int = 200, offset: int = 0) -> list[KnowledgeSourceSchema]: ...
    def list_runs(self, knowledge_base_id: str, *, limit: int = 100, offset: int = 0) -> list[KnowledgeRunSchema]: ...
    def list_run_events(self, run_id: str, *, limit: int = 200, offset: int = 0) -> list[KnowledgeRunEventSchema]: ...
    def calibrate(self, knowledge_base_id: str) -> KnowledgeCalibrationRunResult: ...
    def list_test_runs(self, knowledge_base_id: str, *, limit: int = 100, offset: int = 0) -> list[KnowledgeCalibrationRunSummary]: ...
    def get_test_run(self, test_run_id: str) -> KnowledgeCalibrationRunDetail: ...
    def set_baseline(self, knowledge_base_id: str, test_run_id: str) -> BaselineResult: ...

    def query(self, payload: KnowledgeQueryRequest) -> KnowledgeQueryResultSchema: ...
    def graph_explore(self, payload: KnowledgeGraphExploreRequest) -> KnowledgeGraphExploreResultSchema: ...

    def list_chunks(self, version_id: str, *, limit: int = 200, offset: int = 0) -> list[KnowledgeBaseChunkSchema]: ...
    def list_entities(self, version_id: str, *, limit: int = 200, offset: int = 0) -> list[KnowledgeBaseEntitySchema]: ...
    def list_relationships(self, version_id: str, *, limit: int = 200, offset: int = 0) -> list[KnowledgeBaseRelationshipSchema]: ...
```

### 5.11 Object Store API (S3 console-style operations)

Object Store routes (`caliber.routes.object_store`) are operational (bucket/object browsing) and distinct from workflow file storage.

Route constants include:

- `/object-store/status`
- `/object-store/buckets` + `/object-store/buckets/{bucket}`
- `/object-store/buckets/{bucket}/objects` + delete, preview, extract, import

SDK surface:

```python
class ObjectStoreApi:
    def status(self) -> ObjectStoreStatus: ...
    def list_buckets(self) -> list[ObjectStoreBucket]: ...
    def create_bucket(self, bucket: str) -> ObjectStoreBucket: ...
    def delete_bucket(self, bucket: str) -> DeleteResult: ...

    def list_objects(self, bucket: str, *, prefix: str = "", limit: int = 500) -> ObjectStoreListing: ...
    def delete_objects(self, bucket: str, keys: list[str]) -> ObjectStoreDeleteResult: ...
    def download_object(self, bucket: str, key: str) -> bytes: ...
    def upload_object(self, bucket: str, *, key: str, data: bytes, content_type: str | None = None) -> UploadResult: ...

    def preview(self, bucket: str, key: str, *, max_bytes: int | None = None) -> ObjectStorePreview: ...
    def extract(self, bucket: str, key: str) -> ObjectStoreExtract: ...
    def import_to_project(self, bucket: str, *, key: str, project_id: str, path: str) -> ImportResult: ...
```

Gap: these payloads are currently dict-oriented in the server route module, so full typing requires formal schemas (or SDK-local models) and eventually OpenAPI coverage.

### 5.12 Events / streaming

The UI subscribes to server-sent events at `EVENT_STREAM_PATH = /events/stream`.

SDK should provide:

- `events.stream()` generator that yields parsed event dicts.
- Optional filtering by event type.

### 5.13 Evaluations, judges, and datasets (quality measurement)

The evaluation surface spans:

- `caliber.routes.eval_datasets` (datasets + examples + sync)
- `caliber.routes.evaluations` (evaluation runs)
- `caliber.routes.judges` (custom LLM judges)

SDK should support:

```python
class EvalDatasetsApi:
    def list(self, *, status: str = "active", limit: int = 500, offset: int = 0) -> list[EvalDatasetSchema]: ...
    def get(self, dataset_id: str) -> EvalDatasetSchema: ...
    def create(self, payload: EvalDatasetCreateRequest) -> EvalDatasetSchema: ...
    def update(self, dataset_id: str, payload: EvalDatasetUpdateRequest) -> EvalDatasetSchema: ...
    def restore(self, dataset_id: str, version: int) -> EvalDatasetSchema: ...
    def list_examples(self, dataset_id: str, *, version: int | None = None, as_of_version: int | None = None, include_superseded: bool = False) -> list[EvalExampleSchema]: ...
    def add_example(self, dataset_id: str, payload: EvalExampleCreateRequest) -> EvalExampleSchema: ...
    def add_example_from_trace(self, dataset_id: str, payload: EvalExampleFromTraceRequest) -> EvalExampleSchema: ...
    def revise_example(self, dataset_id: str, example_id: str, payload: EvalExampleCreateRequest) -> EvalExampleSchema: ...
    def supersede_example(self, dataset_id: str, example_id: str) -> EvalExampleSchema: ...
    def sync_to_mlflow(self, dataset_id: str) -> SyncResult: ...

class EvaluationsApi:
    def list_runs(self, *, limit: int = 100, offset: int = 0) -> list[EvalRunSummary]: ...
    def get_run(self, run_id: str) -> EvalRunDetail: ...
    def create_run(self, payload: EvalRunCreateRequest) -> EvalRunDetail: ...

class JudgesApi:
    def list(self, *, status: str = "active", limit: int = 500, offset: int = 0) -> list[JudgeSchema]: ...
    def create(self, payload: JudgeCreateRequest) -> JudgeSchema: ...
    def update(self, judge_id: str, payload: JudgeUpdateRequest) -> JudgeSchema: ...
    def test_run(self, judge_id: str, payload: JudgeTestRunRequest) -> JudgeTestRunResult: ...
    def alignment(self, judge_id: str, payload: JudgeAlignmentRequest) -> JudgeAlignmentResult: ...
```

### 5.14 Releases API (governed promotion + signoff)

Release routes are in `caliber.routes.releases` with schemas in `caliber.schemas` (e.g., `ReleaseCandidateSchema`, `ReleaseSignoffSchema`, `ReleaseReportJobSchema`).

SDK should expose:

```python
class ReleasesApi:
    def timeline(self) -> list[ReleaseTimelineEvent]: ...
    def live(self) -> list[ReleaseLiveEntry]: ...
    def list_operations(self, *, limit: int = 200, offset: int = 0) -> list[ReleaseOperation]: ...
    def reconcile_operations(self) -> ReconcileResult: ...
    def resolve_operation(self, operation_id: str, *, action: str, notes: str = "") -> ReleaseOperation: ...

    def list_candidates(self, *, limit: int = 200, offset: int = 0) -> list[ReleaseCandidateSchema]: ...
    def create_candidate(self, payload: ReleaseCandidateCreateRequest) -> ReleaseCandidateSchema: ...
    def get_candidate(self, candidate_id: str) -> ReleaseCandidateSchema: ...
    def evaluate_candidate(self, candidate_id: str) -> EvaluationResult: ...
    def signoff(self, candidate_id: str, payload: ReleaseSignoffRequest) -> ReleaseSignoffSchema: ...
    def waive(self, candidate_id: str, payload: ReleaseWaiverRequest) -> ReleaseCandidateSchema: ...
    def create_report_job(self, candidate_id: str) -> ReleaseReportJobSchema: ...
    def report_job(self, report_job_id: str) -> ReleaseReportJobSchema: ...
```

### 5.15 Observability API (traces + experiments + metrics)

Observability routes (`caliber.routes.observability`) wrap MLflow tracing and Allure serving:

- `GET /observability/traces` + `GET /observability/traces/{trace_id}`
- `POST /observability/traces/{trace_id}/feedback`
- `GET /observability/experiments` and `GET /observability/metrics`
- `GET /observability/allure-report/*`

SDK surface:

```python
class ObservabilityApi:
    def list_traces(self, *, limit: int = 50) -> list[ObservabilityTrace]: ...
    def get_trace(self, trace_id: str) -> ObservabilityTraceDetail: ...
    def add_feedback(self, trace_id: str, payload: ObservabilityTraceAssessment) -> ObservabilityTraceAssessment: ...
    def experiments(self) -> list[ObservabilityExperiment]: ...
    def metrics(self, *, bucket_count: int | None = None) -> ObservabilityMetrics: ...
    def allure_report_url(self) -> str: ...
```

Gap: these payloads are largely dict-oriented today; full typing requires formal schemas or SDK-local models.

### 5.16 Gateway API (provider endpoints + guardrails + usage)

Gateway routes (`caliber.routes.gateway`) already use Pydantic schemas for status/guardrails.

SDK surface:

```python
class GatewayApi:
    def status(self) -> GatewayStatusSchema: ...
    def guardrails(self) -> GatewayGuardrailsStatusSchema: ...
    def guardrails_catalog(self) -> GatewayGuardrailCatalogSchema: ...
    def create_guardrail(self, payload: GatewayGuardrailCreateRequest) -> GatewayGuardrail: ...
    def update_guardrail(self, guardrail_id: str, payload: GatewayGuardrailConfigUpdateRequest) -> GatewayGuardrail: ...
    def attach_guardrail(self, endpoint_id: str, payload: GatewayGuardrailAttachRequest) -> AttachResult: ...
    def detach_guardrail(self, endpoint_id: str, guardrail_id: str) -> DetachResult: ...
    def usage(self) -> GatewayUsage: ...
```

### 5.17 Audit & “me” APIs

SDK should expose:

- `GET /me`
- `GET /audit-log` + export

```python
class MeApi:
    def get(self) -> CurrentUserInfo: ...

class AuditApi:
    def list(self, filters: AuditLogFilters, *, limit: int = 200, offset: int = 0) -> AuditLogPage: ...
    def export(self, filters: AuditLogFilters) -> bytes: ...

class RollbackApi:
    def list_checkpoints(self, agent_id: str, *, limit: int = 100) -> list[RollbackCheckpointSchema]: ...
    def rollback(self, agent_id: str, *, checkpoint_id: str | None = None) -> RollbackResponse: ...
```

### 5.18 Review Queues API (structured human review)

Review queues are a first-class governed workflow surface, not a UI convenience. They are part of the agentic loop because they let workflows and evaluations route uncertain or high-risk traces into structured human adjudication.

```python
class ReviewQueuesApi:
    def list(self, *, status: str = "active", limit: int = 200, offset: int = 0) -> list[ReviewQueueSchema]: ...
    def get(self, queue_id: str) -> ReviewQueueDetailSchema: ...
    def create(self, payload: ReviewQueueCreateRequest) -> ReviewQueueSchema: ...
    def update(self, queue_id: str, payload: ReviewQueueUpdateRequest) -> ReviewQueueSchema: ...
    def add_items(self, queue_id: str, payload: ReviewQueueItemsCreateRequest) -> list[ReviewQueueItemSchema]: ...
    def submit_item(self, queue_id: str, item_id: str, answers: dict[str, object]) -> ReviewQueueItemSchema: ...
    def import_alignment_examples(self, queue_id: str, question_key: str) -> ReviewAlignmentImportSchema: ...
```

### 5.19 Aria Plans API (agentic orchestration)

Aria is the platform’s current agentic orchestration layer. The important distinction is:

- the platform already supports plan creation, approval, execution, polling, and interaction handling;
- it does **not** yet support full autonomous artifact creation from intent without externalized step inputs.

The SDK should therefore expose the real current contract cleanly rather than implying a stronger autonomy model than the backend provides.

```python
class AriaPlansApi:
    def capabilities(self) -> AriaCapabilitiesSchema: ...
    def list(self, *, session_id: str | None = None, limit: int = 100, offset: int = 0) -> list[AriaPlanSchema]: ...
    def get(self, plan_id: str) -> AriaPlanDetailSchema: ...
    def create(self, payload: AriaPlanCreateRequest) -> AriaPlanDetailSchema: ...
    def update(self, plan_id: str, payload: AriaPlanUpdateRequest) -> AriaPlanDetailSchema: ...
    def approve(self, plan_id: str) -> AriaPlanDetailSchema: ...
    def execute(self, plan_id: str) -> AriaPlanDetailSchema: ...
    def poll(self, plan_id: str) -> AriaPlanDetailSchema: ...
    def list_interactions(self, plan_id: str) -> list[AriaInteractionSchema]: ...
    def answer_interaction(self, interaction_id: str, payload: AriaInteractionAnswerRequest) -> AriaPlanDetailSchema: ...
```

### 5.20 Jobs and Cookbooks APIs

Jobs and Cookbooks are important supporting surfaces for agentic development:

- `JobsApi` covers durable background refinement/calibration work and operator-controlled apply flows.
- `CookbooksApi` lets developers bootstrap governed examples through the same backend-owned catalog the UI uses.

```python
class JobsApi:
    def list(self, *, status: str | None = None, job_type: str | None = None, limit: int = 100, offset: int = 0) -> list[JobSummarySchema]: ...
    def get(self, job_id: str) -> JobDetailSchema: ...
    def targets(self, job_id: str) -> list[JobTargetSchema]: ...
    def apply(self, job_id: str, payload: JobApplyRequest | None = None) -> JobApplyResultSchema: ...

class CookbooksApi:
    def list(self) -> CookbookCatalogSchema: ...
    def install(self, cookbook_id: str, payload: CookbookInstallRequest) -> CookbookInstallResultSchema: ...
```

### 5.21 Agentic workflow SDK completeness

For agentic workflows specifically, the SDK proposal should make a strict distinction between what becomes complete through SDK work and what still depends on backend evolution.

#### Complete after SDK expansion

The following become fully covered by the typed SDK once the detailed methods above are adopted:

- workflow authoring, versioning, validation, compile/publish, and preview execution;
- workflow run control: create, cancel, retry, resume, resume-by-event, approvals, traces, events, checkpoints;
- workflow calibration and durable background refinement flows;
- Aria planning lifecycle: create, approve, execute, poll, interaction handling, capability inspection;
- governed human review loops through review queues;
- prompt/skill/tool/knowledge/evaluation sub-artifacts that agentic workflows compose;
- cookbook installation and scripted reproduction of the current operator-assisted Aria examples.

#### Still blocked by backend/platform gaps

Even after the SDK is complete, two meaningful limits remain:

1) **Full autonomous Aria artifact creation is not complete yet.**
   - The planner/executor flow exists, but the current Aria interaction contract does not populate per-step inputs for arbitrary create/update actions.
   - That means the SDK can faithfully expose today’s plan-then-create-via-routes model, but it cannot make the platform more autonomous than it really is.

2) **Portable document-extraction workflows are not complete yet.**
   - Cookbook 04 still depends on a host-local path contract for document extraction.
   - A remote SDK cannot paper over that with typing alone; the backend needs a portable file-reference abstraction.

#### Final agentic workflow verdict

- **Can CALIBER support agentic workflow development through SDK after these additions?** Yes, for the current operator-assisted model.
- **Do we have a fully complete public SDK for agentic workflow development today?** No, not yet.
- **Can the proposal be made complete?** Yes. This document should define the full typed surface above, and it should explicitly label the remaining backend blockers rather than hiding them behind SDK language.

### 5.22 Python SDK API catalog (proposed package inventory)

The Python SDK should ship a clear inventory of sub-clients so developers can discover capabilities without reading route modules. The list below is aligned to the current repository surface, but it should not be read as “all of these are `GA` in v1.” The stability tiers in §3.3 control what ships as `GA`, what ships as `beta`, and what stays internal.

| Python API | Purpose | Core functionality |
| --- | --- | --- |
| `CaliberClient` | Root entrypoint and transport owner. | Base URL handling, auth wiring, project scoping, retries, timeouts, raw transport access, lifecycle management. |
| `RawApi` | Low-level forward-compatible escape hatch. | Direct `get/post/request` access against the existing CALIBER HTTP surface while reusing SDK auth, retries, CSRF, headers, and error normalization. |
| `WaitersApi` | Polling helpers for long-running operations. | Wait for workflow runs, jobs, service runs, and other async-style backend operations to reach terminal states without bespoke polling code. |
| `AuthApi` | Session and account management. | Login, logout, session inspection, admin account listing/creation/update, later PAT creation/revocation. |
| `CsrfApi` | CSRF bootstrap for non-browser clients. | Fetch token, expose enablement/TTL state, refresh on CSRF-shaped failures. |
| `MeApi` | Identity introspection. | Current user/profile/scopes lookup for scripts and CLIs. |
| `CapabilitiesApi` | Runtime feature discovery. | Fetch deployment capability matrix so automation can adapt to enabled/disabled features. |
| `SettingsApi` | Safe runtime configuration inspection. | Read runtime and LLM setup status, versioning defaults, dependency advisories, effective configuration summaries. |
| `SecretsApi` | Secret inventory and rotation without plaintext reads. | List secret metadata, store/rotate values, revoke, purge, inspect version/state without ever returning the secret body. |
| `CookbooksApi` | Starter recipe/catalog workflows. | List cookbook recipes, inspect recipe metadata, install recipe drafts into a project/workspace. |
| `PromptsApi` | Prompt registry and release helpers. | CRUD-like prompt management, versions, aliases, rollback, template-library preview, workspace/baseline/bind helpers. |
| `SkillsApi` | Skill registry and package lifecycle. | List/create/update/archive skills, export/import packages, ZIP package transport, test and calibration support. |
| `ToolsApi` | Tool registry and validation flows. | Register/update/archive tools, inspect source, manage test cases, create test runs, calibration and preview policies. |
| `AgentsApi` | Agent inventory and experiment bindings. | Create/update/delete agents, inspect attached skills, resolve experiment bindings, manage agent metadata used by prompts/workflows. |
| `WorkflowsApi` | Workflow authoring and execution control plane. | Workflow CRUD, import/export, versions, validation/compile/publish, preview runs, run creation, approvals, retries, lineage, traces, events. |
| `ServicesApi` | Workflow-as-service publication layer. | Publish/unpublish workflow services, manage service tokens, fetch per-workflow OpenAPI, invoke services, poll service runs, idempotent external invocation. |
| `ProjectsApi` | Workspace/project management. | Create/update/archive projects, browse project folders/files, backend selection, project-scoped uploads/downloads. |
| `FilesApi` | Run-scoped and playground file operations. | Upload/download/list run files, manage run-related artifacts and playground attachments. |
| `McpServersApi` | MCP integration management. | Create/update/list servers, test connections, discover tools, inspect policy, invoke tools, preserve write-only secret fields. |
| `GatewayApi` | Provider gateway and guardrail operations. | Status, endpoint inventory, guardrail catalog, create/update/attach/detach guardrails, usage and routing visibility. |
| `KnowledgeApi` | Knowledge-base and retrieval operations. | Create/update knowledge bases, manage versions, activate/rollback, query, graph exploration, inspect chunks/entities/relationships. |
| `ObjectStoreApi` | Operational object-store console. | Bucket listing/creation/deletion, object list/upload/download/delete, preview/extract/import to project. |
| `EvalDatasetsApi` | Evaluation dataset management. | Dataset CRUD, example list/add/update, version-aware sync to MLflow or other backing stores. |
| `EvaluationsApi` | Evaluation run execution and inspection. | Launch evaluation runs for prompts/workflows/artifacts, list runs, inspect detailed results and judge outputs. |
| `JudgesApi` | Judge authoring and alignment. | List/create/update judges, run test sets, compute alignment/audit results, manage judge configuration. |
| `ReviewQueuesApi` | Human review workflows on traces/examples. | Create/update queues, enqueue review items, fetch queue counts, submit answers, retrieve alignment examples, write back adjudications. |
| `AriaPlansApi` | Agentic plan orchestration for Aria. | Create draft plans, approve/edit/cancel, execute/resume, poll async progress, inspect interactions, answer required questions, read Aria capabilities. |
| `JobsApi` | Long-running CALIBER refinement job control. | List jobs, fetch job detail, inspect targets, apply job outputs, filter by status/type/target. |
| `ReleasesApi` | Governed release candidate workflow. | Create/list candidates, evaluate, waive, sign off, create/fetch report jobs, inspect timeline/live state, reconcile operations. |
| `RollbackApi` | Generic checkpoint-based rollback helpers for artifact families that expose the shared rollback route. | List rollback checkpoints, select a checkpoint, execute rollback with audit-safe reason capture. |
| `ObservabilityApi` | Traces, experiments, metrics, and QA artifacts. | List/get traces, add feedback, fetch experiment summaries, aggregated metrics, and Allure/QA report metadata. |
| `AuditApi` | Audit-log access for operators and automation. | Filtered audit list, export, actor/entity/date filters, evidence retrieval for governance. |
| `EventsApi` | Server-sent event consumption. | Subscribe to event stream, parse typed events, reconnect after transient failures, optional event filtering. |

Not every registered route should become a stable public SDK surface in v1. The following route groups appear to be internal, UI-only, or lower-level operational surfaces and should stay out of the initial stable contract unless there is a concrete developer use case:

- `assistant`, `memory`: assistant/chat orchestration endpoints likely to change with product iteration.
- `dashboard`, `metrics`, `health`: operational readouts that are better handled by product telemetry or infra checks than a typed developer SDK.
- `gate_verdicts`, `llm_pricing`, `system_effects`, `system_services`: useful internal APIs, but likely too implementation-coupled to freeze before the broader management API is stabilized.

---

### 5.23 Cookbook development by only using SDK

This section answers a narrower product question: can a developer recreate the shipped Cookbooks using only `caliber-sdk`, without relying on browser-only UI flows?

#### Verdict

- **Using the typed SDK proposal exactly as originally written above:** **no**.
  The proposal covered the main resource families, but it omitted several cookbook-critical operations that already exist in the backend and in the internal TypeScript client.
- **Using the existing CALIBER APIs plus the additions listed in this section:** **almost yes**.
  After adding those typed methods, the SDK design is sufficient for **15 of the 16** cookbook tracks.
- **Residual blocker to true 16/16 completeness:** **Cookbook 04** remains limited by a platform contract, not an SDK design omission.
  The current document-extraction flow still expects a host-local path for `extract_document(ref)`, so a clean remote SDK-only workflow is not fully portable yet.
- **Aria caveat:** Cookbooks 12–15 are SDK-complete for CALIBER’s current operator-assisted Aria semantics, but they are not a full “one intent to all artifacts autonomously created” story until planner input-population is wired in the platform.

#### Cookbook-by-cookbook completeness check

| Cookbook | Principal SDK surfaces | SDK-only completeness verdict |
| --- | --- | --- |
| `01` Prompt classifier | `PromptsApi`, `EvalDatasetsApi`, `EvaluationsApi`, `ObservabilityApi` | Complete after adding prompt render/test/calibration APIs and dataset-from-trace helpers. |
| `02` Precision skills | `SkillsApi` | Complete after adding skill render/selection/test/baseline/bind/calibrate APIs. |
| `03` Policy-safe tool | `ToolsApi`, `WorkflowsApi`, `PromptsApi`, `SkillsApi` | Complete after adding tool calibration/baseline/workspace APIs and richer workflow authoring helpers. |
| `04` Doc-to-JSON pipeline | `ToolsApi`, `WorkflowsApi`, `EvalDatasetsApi`, `FilesApi`, `ObjectStoreApi` | **Not fully complete** until the backend accepts project/object-store/server-managed file refs instead of a host-local path. |
| `05` Governed MCP | `McpServersApi` | Complete after adding per-tool policy, test-case, calibration, and history methods. |
| `06` Grounded knowledge assistant | `KnowledgeApi`, `PromptsApi`, `SkillsApi`, `JudgesApi`, `EvalDatasetsApi`, `EvaluationsApi` | Complete after adding knowledge-build, source, run, calibration, and baseline methods. |
| `07` Support triage copilot | `WorkflowsApi`, `KnowledgeApi`, `McpServersApi`, `ToolsApi`, `PromptsApi`, `SkillsApi` | Complete once Cookbook 05 and 06 additions are covered. |
| `08` Incident response copilot | `WorkflowsApi`, `ServicesApi`, `ToolsApi`, `ObservabilityApi` | Complete after richer workflow/service/run helpers are exposed. |
| `09` Self-healing workflows | `WorkflowsApi`, `JobsApi`, `EvalDatasetsApi`, `JudgesApi`, `ObservabilityApi`, `WaitersApi` | Complete after adding workflow diff/restore/checkpoint/calibration helpers and job waiters. |
| `10` Trustworthy evaluation | `JudgesApi`, `ReviewQueuesApi`, `EvalDatasetsApi`, `EvaluationsApi`, `ObservabilityApi` | Complete after adding review-queue alignment import and dataset example lifecycle methods. |
| `11` Release signoff factory | `ReleasesApi`, `ReviewQueuesApi`, `PromptsApi`, `ObservabilityApi` | Complete after adding release report-job creation and Allure/report helpers. Manual scoring logic can live in user code on top of the SDK. |
| `12` Aria evaluation harness | `AriaPlansApi`, `JudgesApi`, `EvalDatasetsApi`, `EvaluationsApi` | Complete for current plan-then-create-via-routes semantics; not full hands-off autonomy. |
| `13` Aria review governance queue | `AriaPlansApi`, `ReviewQueuesApi` | Complete for current plan-then-create-via-routes semantics; not full hands-off autonomy. |
| `14` Aria governance starter kit | `AriaPlansApi`, `JudgesApi`, `EvalDatasetsApi`, `ReviewQueuesApi` | Complete for current plan-then-create-via-routes semantics; not full hands-off autonomy. |
| `15` Aria triage & recalibrate loop | `AriaPlansApi`, `JobsApi`, `WorkflowsApi`, `ReviewQueuesApi`, `WaitersApi` | Complete for current platform semantics after workflow calibration + job polling helpers are added. |
| `16` Observability & triage | `ObservabilityApi`, `ReviewQueuesApi`, `EvalDatasetsApi`, `EvaluationsApi` | Complete after adding trace-to-dataset capture and review-queue item APIs. |

#### Typed SDK additions required for cookbook completeness

The table below is the practical fix list. These operations already exist in the repository surface or internal TS client; they should be promoted into the typed Python SDK so cookbook development does not depend on ad hoc raw requests.

| API | Add these methods to the typed SDK | Needed by cookbooks |
| --- | --- | --- |
| `PromptsApi` | `create_version`, `test_render`, `calibration_options`, `create_calibration_run`, `optimization_options`, `create_optimization_run`, `save_test_run`, `list_test_runs`, `get_test_run` | `01`, `03`, `11` |
| `SkillsApi` | `rollback`, `list_versions`, `test_render`, `test_selection`, `workspace`, `save_test_run`, `list_test_runs`, `get_test_run`, `set_baseline`, `bind`, `calibrate` | `02`, `03`, `06`, `07` |
| `ToolsApi` | `list_versions`, `usage`, `workspace`, `create_test_run`, `save_test_run`, `set_baseline`, `calibrate`, `submit_calibration_job`, `list_calibration_jobs`, `resolve_calibration_job` | `03`, `04`, `05`, `07`, `08` |
| `WorkflowsApi` | `list_components`, `list_templates`, `preview_cron`, `restore_version`, `diff_versions`, `run_version`, `propose_patch`, `copilot_edit`, `plan_build`, `calibration_options`, `create_calibration_run`, `list_checkpoints`, `get_run_by_trace` | `03`, `04`, `07`, `08`, `09`, `15`, `16` |
| `EvalDatasetsApi` | `get`, `restore`, `list_examples(version/as_of_version/include_superseded)`, `add_example_from_trace`, `revise_example`, `supersede_example` | `01`, `04`, `06`, `10`, `12`, `16` |
| `ReviewQueuesApi` | `list`, `get`, `create`, `update`, `add_items`, `submit_item`, `import_alignment_examples` | `10`, `11`, `13`, `14`, `16` |
| `AriaPlansApi` | `list`, `get`, `create`, `update`, `approve`, `execute`, `poll`, `list_interactions`, `answer_interaction`, `capabilities` | `12`, `13`, `14`, `15` |
| `KnowledgeApi` | `delete_base`, `get_version`, `sync_age`, `list_sources`, `list_runs`, `list_run_events`, `calibrate`, `list_test_runs`, `get_test_run`, `set_baseline` | `06`, `07` |
| `McpServersApi` | `delete`, `history`, `update_tool_policy`, `save_tool_test_cases`, `calibrate_tool` | `05`, `07` |
| `ReleasesApi` | `get_candidate`, `create_report_job` | `11` |
| `ObservabilityApi` | `allure_report_url` (or equivalent helper), plus request/trace correlation surfaced consistently | `11`, `16` |
| `JobsApi` | `list`, `get`, `targets`, `apply` | `09`, `15` |
| `CookbooksApi` | `list`, `install` (explicitly documented as typed public APIs, not only catalog conveniences) | catalog bootstrap / installer flows |

#### What should not count as “SDK-complete”

The following do **not** count as a complete cookbook-by-SDK story:

- Requiring cookbook authors to use `client.raw.*` for normal cookbook flows.
  `RawApi` is an escape hatch, not the primary cookbook authoring surface.
- Treating UI-only affordances as the contract when the backend already exposes a reusable route.
- Calling a cookbook “SDK-complete” if the only working path depends on filesystem locality inside the CALIBER server process.

#### Final cookbook completeness conclusion

The proposal should state the cookbook result explicitly:

- **Typed SDK as currently specified in this document:** **not 100% cookbook-complete**.
- **Typed SDK after the additions above:** **cookbook-complete for 15/16 shipped cookbooks**.
- **To reach 16/16:** the backend must close the document-extraction path gap in Cookbook 04 by accepting a portable file reference model (for example project-file ids, object-store references, or a server-managed staged upload abstraction) instead of requiring a raw host-local path.

This is the right bar for the SDK plan because it distinguishes:

- missing typed SDK surface,
- acceptable operator/environment prerequisites, and
- underlying platform constraints that no SDK can hide.

---

## 6) Plugin SDK architecture (in-process extension)

### 6.1 What “plugin SDK” should mean for CALIBER

A third-party should be able to:

- Add an optimizer (candidate-generation implementation) without forking.
- Add a judge provider (beyond MLflow make_judge) without forking.
- Add a storage backend beyond local/s3 without forking.
- Add tool families (registered tools) and package them safely.

This requires:

- A stable set of interfaces (protocols + context dataclasses).
- A stable registration mechanism (entry points).
- A compatibility contract (semver + deprecations).
- A conformance test harness.

### 6.2 Immediate prerequisite: de-hardcode dispatch into registries

Today:

- Candidate generation dispatch is hard-coded inside `caliber.llm.openai_agents` (optimizer types: MetaPrompt/SkillMetaPrompt/GEPA/DSPy*).
- Provider selection is hard-coded `if provider == ...` in multiple build_* factories.

To support plugins:

- Replace hard-coded `if/elif` dispatch with registries keyed by name.
- Support built-ins registered in the registry at import time.
- Support third-party registration via entry points.

### 6.3 Proposed plugin entry point groups

Use `importlib.metadata.entry_points()` with explicit group names, e.g.:

- `caliber.optimizers` → `OptimizerFactory`
- `caliber.llm_providers` → `LLMProviderFactory`
- `caliber.eval_providers` → `EvalProviderFactory`
- `caliber.promoters` → `PromoterFactory`
- `caliber.storage_backends` → `StorageBackendFactory`
- `caliber.judge_providers` → `JudgeProviderFactory`

Each factory receives:

- A stable config object (from plugin SDK), not the full server config.
- Only the minimal dependencies needed (e.g., an `httpx.Client` or a DB accessor interface).

### 6.4 Conformance tests

Ship a conformance suite in `caliber-plugin-sdk` that plugins can run:

- Validates required methods.
- Validates error normalization contracts (never leak raw provider exceptions).
- Validates deterministic serialization and schema expectations.
- Validates idempotency and retry safety for optimizer operations where applicable.

### 6.5 Security posture for plugins

Plugins run in the server process. The SDK must make containment explicit:

- Explicit allowlists for which plugins can load (similar to registered tool module allowlist).
- Clear separation between “data-plane execution” and “control-plane management”.
- Plugins should never receive plaintext secrets by default; they should receive secret references plus a controlled secret resolution interface.

---

## 7) Gap analysis (what blocks a complete SDK experience)

This section is prioritized by “blocks SDK correctness / safety” first, then “blocks ergonomics”.

### API gap summary for a complete SDK

Yes — there are still API gaps if the goal is a truly complete public SDK.

The important nuance is that CALIBER is **not** missing most major route families. The existing backend already exposes the majority of the management and runtime operations that the SDK needs. The remaining work is mostly about **API completeness, contract quality, and automation-safe semantics**, not inventing a second backend.

The concrete API/contract gaps that still matter are:

1) **No served management OpenAPI document.**
   - Without a published machine-readable contract, SDKs drift and cannot be generated or validated reliably.

2) **Not all request/response payloads are formal schemas.**
   - Several endpoints still return flexible dict payloads, which weakens typing, documentation, and compatibility guarantees.

3) **Automation auth is incomplete for a public SDK.**
   - Session-token login exists, but a developer SDK still needs first-class Personal Access Tokens with scopes, rotation, and revocation.

4) **Long-running operation contracts are not standardized enough.**
   - Runs, jobs, evaluations, report generation, and service invocations still rely on resource-specific polling and state handling patterns.

5) **Aria’s execution contract is not yet sufficient for full autonomous artifact materialization.**
   - The lifecycle endpoints exist, but the API does not yet provide enough execution-time structure to populate arbitrary create/update step inputs without operator assistance.

6) **Document extraction lacks a portable server-side file reference contract.**
   - A remote SDK cannot make document workflows complete while extraction still depends on host-local paths.

The P0/P1 items below break these gaps into implementation priorities.

### P0 (must address to ship an SDK)

1) **No served OpenAPI for the management API.**
   - Workflow services have OpenAPI per workflow, but CALIBER’s management API does not.
   - Without OpenAPI, typed SDKs must be handwritten and will drift.
   - Recommendation: add `GET /ajax-api/2.0/mlflow/caliber/openapi.json` that describes the management API (even if partial/“beta” first).

2) **Some endpoints are not modeled as Pydantic schemas.**
   - Many schemas exist in `caliber.schemas`, but prompts/projects/files/object-store responses include dict payloads.
   - Recommendation: formalize missing request/response models in `caliber.schemas` (or a parallel `caliber.api_schemas`), and use them consistently.

3) **No first-class token story for automation.**
   - Session tokens exist and can be used as Bearer tokens, but acquiring them requires password login.
   - Workflow service tokens exist but are scoped to service invocation.
   - Recommendation: add Personal Access Tokens (PATs) with:
     scopes, expiry, rotation, audit rows, and revocation.

4) **No explicit SDK/server compatibility contract.**
   - The route prefix is stable enough to build on, but the proposal needs a clear answer to:
     which CALIBER server versions a given SDK release supports, how capability gaps are detected, and how beta operations are labeled.
   - Recommendation: publish a compatibility matrix and expose machine-readable stability metadata through the management OpenAPI plus `/capabilities`.

5) **Hard-coded dispatch blocks the Plugin SDK.**
   - Optimizer dispatch in `caliber.llm.openai_agents` is not registry-based.
   - Provider factories are `if provider == ...`.
   - Recommendation: introduce registries + entry-point loading, then freeze plugin interfaces with semver.

6) **Aria does not yet support full autonomous step-input population.**
   - The current plan lifecycle is real, but create/update steps still require operator-supplied artifact payloads through the underlying capability routes.
   - Recommendation: keep the SDK honest about current semantics, and track planner input population as a backend/platform milestone rather than an SDK promise.

### P1 (high value; improves developer UX and reduces drift)

7) **The first draft over-scoped the stable v1 surface.**
   - Not every current route group is equally mature or equally central to the initial developer workflow.
   - Recommendation: publish `GA`, `beta`, and `internal` tiers and limit `v1 GA` to the core management and automation APIs.

8) **Inconsistent pagination + list defaults.**
   - Some endpoints use `limit/offset` with defaults/caps; others may not.
   - Recommendation: standardize list endpoints (limit/offset now; cursor later).

9) **CSRF requirement is global and can surprise non-browser SDK clients.**
   - If CSRF is enabled, a Bearer-token SDK client may still need to fetch and send CSRF tokens.
   - Recommendation: consider scoping CSRF enforcement to cookie-auth only, or document a uniform requirement and make SDK auto-handle it.

10) **Idempotency is implemented on some durable operations but not consistently exposed.**
   - Example: service invoke has a scoped idempotency key; prompt releases have durable operation machinery.
   - Recommendation: standardize an `Idempotency-Key` header on all endpoints that can be retried safely.

11) **Long-running operations do not yet have a standard client-side contract.**
    - Workflow runs, jobs, evaluations, service invocations, and report generation currently require bespoke polling logic.
    - Recommendation: standardize terminal states and add SDK waiters built on the existing APIs.

12) **Request correlation and observability hooks are not part of the contract.**
    - A production SDK needs request IDs, SDK user-agent tagging, and logging/tracing hooks to be first-class.
    - Recommendation: expose correlation headers consistently and define transport hook interfaces in `caliber-sdk`.

13) **Document extraction still depends on a host-local path contract.**
    - This blocks a truly portable remote-SDK story for document-centric workflows such as Cookbook 04.
    - Recommendation: introduce a portable server-side file reference abstraction for extraction and ingestion tools.

### P2 (nice-to-have / later)

14) **TypeScript SDK extraction.**
   - The UI’s `caliberApi.ts` is already an internal TS SDK; extraction to a standalone package would help Node integrators.
   - Recommendation: after OpenAPI exists, generate a TS client or extract and version a shared API package.

15) **CLI.**
   - Once a stable management SDK exists, a CLI becomes straightforward and high leverage for ops.

### API changes required before SDK GA

The checklist below is the practical “definition of done” for declaring the public SDK generally available.

#### Must be complete before SDK GA

- [ ] Serve a management OpenAPI document for `/ajax-api/2.0/mlflow/caliber`.
- [ ] Ensure all GA SDK endpoints have formal request/response schemas instead of ad hoc dict payloads.
- [ ] Add Personal Access Tokens with scopes, expiry, rotation, revocation, and auditability.
- [ ] Publish SDK/server compatibility rules:
  - supported server-version range;
  - machine-readable capability detection;
  - stability tier per operation (`ga`, `beta`, `internal`).
- [ ] Standardize long-running operation contracts across runs, jobs, evaluations, service invocations, and report generation:
  - common terminal states;
  - common waiter/polling semantics;
  - documented retry expectations.
- [ ] Define a consistent idempotency contract for retry-safe mutating operations.
- [ ] Expose request-correlation headers and SDK observability hooks as public contract.
- [ ] Freeze the initial GA resource set and explicitly mark non-GA APIs as `beta` or `internal`.

#### Required before claiming agentic-workflow SDK completeness

- [ ] Extend Aria execution semantics so plans can materialize arbitrary create/update step inputs without operator-only route handoff.
- [ ] Add a portable file-reference contract for document extraction and ingestion so remote SDK clients are not forced to provide host-local paths.

#### Required before Plugin SDK GA

- [ ] Replace hard-coded optimizer/provider dispatch with registry-based extension loading.
- [ ] Define versioned plugin interfaces and compatibility guarantees for optimizers, providers, and adjacent extension points.

#### Can follow after initial SDK GA

- [ ] Extract or generate a standalone TypeScript SDK from the same API contract.
- [ ] Add a first-party CLI on top of the stable Python SDK.

---

## 8) Implementation roadmap (phased, with acceptance criteria)

Before the phase breakdown, the implementation order needs one explicit constraint:

- do not start the public SDK package until the server contract work in Phase 0 is merged;
- do not claim agentic-workflow SDK completeness until the Phase 2 beta surfaces ship;
- do not start the Plugin SDK contract freeze until registry-based dispatch replaces hard-coded provider/optimizer selection.

### Execution order and code touchpoints

The table below turns the roadmap into an execution plan anchored in likely code areas.

| Workstream | Why it comes first | Expected code targets | Exit criteria |
| --- | --- | --- | --- |
| Contract normalization | The SDK should wrap a stable server contract, not reverse-engineer the UI client. | `caliber/src/caliber/routes/__init__.py`, `caliber/src/caliber/routes/_deps.py`, `caliber/src/caliber/routes/_errors.py`, `caliber/src/caliber/schemas.py`, selected route modules, plus a new management OpenAPI publisher route. | OpenAPI exists, GA routes are classified, and core schemas are formalized. |
| Automation auth | Real SDK adoption depends on non-password automation credentials. | `caliber/src/caliber/auth.py`, existing auth routes, plus a new PAT route/module and storage/audit support. | PAT issuance, rotation, revocation, and scope checks work end to end. |
| Python SDK skeleton | Shared transport/auth/error behavior must exist before resource APIs fan out. | New package at `caliber/sdk/caliber-sdk/src/caliber_sdk/` for transport, auth, models, errors, and waiters. | Client can authenticate, scope, call raw endpoints, and normalize errors. |
| GA resource APIs | These cover the first stable automation story and should be finished before beta surfaces. | `caliber_sdk/resources/*`, generated or handwritten models, examples, and contract tests. | The GA API set works without `client.raw.*` for normal usage. |
| Beta + agentic APIs | Agentic workflow completeness depends on these surfaces, so they must be grouped explicitly. | `caliber_sdk/resources/aria.py`, `review_queues.py`, `jobs.py`, `mcp_servers.py`, `gateway.py`, `events.py`, `observability.py`, and related models/tests. | Current operator-assisted agentic workflows are SDK-executable without browser-only flows. |
| Plugin registry refactor | The Plugin SDK should freeze only after the runtime uses registries instead of hard-coded dispatch. | `caliber/src/caliber/llm/openai_agents.py`, provider modules, optimizer selection modules, new extensibility registry package, and `caliber/sdk/caliber-plugin-sdk/src/caliber_plugin_sdk/`. | A third-party optimizer/provider can load without editing CALIBER source. |

### Phase 0 — “Make the contracts real” (2–3 weeks)

Deliverables:

- Add served management OpenAPI document (initially “beta” but generated from a single source of truth).
- Formalize missing Pydantic schemas for endpoints used by SDK.
- Classify public routes as `ga`, `beta`, or `internal`.
- Publish an initial SDK/server compatibility matrix.
- Add first-class PAT endpoints and storage/audit support for automation.
- Add contract tests that:
  - ensure envelope and error shapes remain stable,
  - ensure OpenAPI generation matches the live routes.

Acceptance criteria:

- `GET /ajax-api/2.0/mlflow/caliber/openapi.json` exists and covers the Phase 1 GA resource set:
  `auth`, `csrf`, `me`, `capabilities`, `settings`, `prompts`, `skills`, `tools`, `agents`, `workflows`, `services`, `projects`, `files`, `eval_datasets`, `evaluations`, `judges`.
- Public operations in the spec include machine-readable stability metadata.
- PAT endpoints exist and can be exercised by an automated smoke test.
- A minimal generated client can be produced from the spec and pass a smoke test against a local server.

### Phase 1 — `caliber-sdk` Python client (2–4 weeks)

Deliverables:

- New package `caliber/sdk/caliber-sdk` with:
  - sync transport, auth providers, csrf manager, raw transport escape hatch, waiters,
  - core `GA` resource APIs only: `auth`, `csrf`, `me`, `capabilities`, `settings`, `prompts`, `skills`, `tools`, `agents`, `workflows`, `services`, `projects`, `files`, `eval_datasets`, `evaluations`, `judges`.
- SDK developer-docs foundation in the existing docs site:
  - quickstart,
  - install/auth/configuration page,
  - GA API reference pages,
  - tested getting-started examples.
- Examples:
  - “list prompts, create prompt, run prompt test, promote alias”.
  - “publish workflow service, mint token, invoke”.
  - “wait for a workflow run/service invocation to complete using SDK waiters”.

Acceptance criteria:

- `pip install caliber-sdk` works without `mlflow`.
- End-to-end scripts run against a local CALIBER deployment.
- Typed exceptions carry `detail/status_code/headers`.
- The SDK performs capability discovery against `/capabilities` and fails unsupported operations with explicit messages.
- Cookbook authoring is scriptable for the `GA` cookbook surfaces without falling back to `client.raw.*`.
- Searchable HTML SDK docs for the GA surface are generated into `docs-site/`, synced into the served docs copies, and include tested examples.

### Phase 2 — Beta surfaces, integrations, and streaming (2–4 weeks)

Deliverables:

- `Beta` modules implemented with the same transport/auth/error patterns:
  `mcp_servers`, `gateway`, `knowledge_bases`, `object_store`, `jobs`, `review_queues`, `aria`, `releases`, `rollback`, `observability`, `audit`, `events`, `cookbooks`, `secrets`.
- Events streaming iterator for SSE.
- Request hooks and request-correlation helpers promoted to documented public APIs.
- SDK cookbook documentation and expanded API reference pages:
  - beta API pages with stability labels,
  - SDK-only cookbook exemplar pages,
  - searchable API/method coverage for the beta and agentic surfaces.

Acceptance criteria:

- SDK can upload and download a project file and a run file.
- SDK can create and test an MCP server (with secrets redacted).
- SDK can stream events and reconnect on transient failures.
- Beta modules are clearly labeled in docs and in the OpenAPI stability metadata.
- The typed SDK surface is sufficient to author and operate the current operator-assisted agentic workflow model without depending on browser-only flows.
- After the Phase 2 additions, cookbook development is typed-SDK-complete for 15/16 shipped cookbooks; the remaining Cookbook 04 gap is explicitly tracked as a backend/platform fix.
- The published GitHub Pages docs include searchable beta API pages plus SDK cookbook exemplars, and the examples rendered there are CI-tested.

### Phase 3 — Plugin SDK foundations (4–8 weeks; higher risk)

Deliverables:

- Introduce registry-based dispatch for:
  - optimizer implementations,
  - provider factories (LLM/eval/promoter/artifact store),
  - storage backends (optional).
- New package `caliber/sdk/caliber-plugin-sdk` with:
  - initially experimental protocols + contexts,
  - entrypoint-based plugin loading,
  - conformance suite,
  - one reference plugin (e.g., a toy optimizer).

Acceptance criteria:

- A third-party wheel can add an optimizer without modifying CALIBER source.
- CALIBER can selectively enable/disable plugins via explicit allowlist config.
- The Plugin SDK is still labeled `experimental` until at least one full CALIBER release validates the registry contract in practice.

### Phase 4 — CLI + optional TypeScript SDK + async client (later)

Deliverables:

- `caliberctl` commands for the main admin/operator flows.
- TS SDK generated from OpenAPI or extracted from UI code.
- Optional `AsyncCaliberClient` only after sync waiters, SSE, and file-transfer patterns have stabilized.

Acceptance criteria:

- Common ops procedures can be executed non-interactively in CI.

---

## 9) Implementation tracker (execution-ready)

This section converts the roadmap into an execution plan with concrete code targets, milestone exits, and validation gates.

Companion execution checklist:

- [sdk-implementation-checklist.md](../../docs/reports/sdk-implementation-checklist.md)

### Execution rules

- Do not start `caliber-sdk` public API modules until Phase 0 server-contract work is merged.
- Do not claim agentic-workflow SDK completeness until the Phase 2 beta modules are complete.
- Do not freeze the Plugin SDK contract until registry-based dispatch replaces hard-coded optimizer/provider selection.
- Keep the SDK honest: when the backend lacks a portable or autonomous contract, document the gap rather than hiding it in client abstractions.

### Milestone tracker

| Milestone | Goal | Primary code targets | New code targets | Depends on | Exit gate |
| --- | --- | --- | --- | --- | --- |
| M0 | Normalize server contracts for SDK consumption. | `caliber/src/caliber/routes/__init__.py`, `caliber/src/caliber/routes/_deps.py`, `caliber/src/caliber/routes/_errors.py`, `caliber/src/caliber/schemas.py`, `caliber/src/caliber/auth.py`, `caliber/src/caliber/routes/auth.py`, `caliber/src/caliber/routes/csrf.py`, `caliber/src/caliber/routes/capabilities.py` | `caliber/src/caliber/routes/openapi.py` (or equivalent), PAT route/module, DB migration under `caliber/src/caliber/db/migrations/versions/`, tests for OpenAPI + PATs | none | management OpenAPI is served; PAT flow works; GA route schemas are formalized |
| M1 | Create the Python SDK skeleton and transport contract. | none | `caliber/sdk/caliber-sdk/pyproject.toml`, `caliber/sdk/caliber-sdk/src/caliber_sdk/__init__.py`, `client.py`, `transport.py`, `auth.py`, `csrf.py`, `errors.py`, `models/`, `resources/raw.py`, `waiters.py`, package-local tests | M0 | installable package; auth + raw transport + error normalization work |
| M2 | Ship the GA Python SDK resource modules and GA developer docs. | route contracts already stabilized in M0; existing docs pipeline in `docs-site/` | `caliber/sdk/caliber-sdk/src/caliber_sdk/resources/` modules for `auth`, `me`, `capabilities`, `settings`, `prompts`, `skills`, `tools`, `agents`, `workflows`, `services`, `projects`, `files`, `eval_datasets`, `evaluations`, `judges`; examples/tests; `docs/sdk/`; `docs-site/build-docs.mjs`; `docs-site/docs.js`; `caliber/caliber-ui/scripts/sync-docs.mjs`; `caliber/tests/test_docs_generation_contract.py` | M1 | GA APIs work without `client.raw.*`; searchable HTML SDK docs exist with tested examples |
| M3 | Ship beta modules, agentic workflows, and SDK cookbook docs. | beta route modules already present in `caliber/src/caliber/routes/`; existing Pages/docs pipeline | `caliber/sdk/caliber-sdk/src/caliber_sdk/resources/` modules for `mcp_servers`, `gateway`, `knowledge`, `object_store`, `jobs`, `review_queues`, `aria`, `releases`, `rollback`, `observability`, `audit`, `events`, `cookbooks`, `secrets`; `docs/sdk/`; `docs-site/build-docs.mjs`; `.github/workflows/pages.yml`; docs/tests for searchable cookbook/API pages | M2 | current operator-assisted agentic workflows are SDK-executable; SDK cookbook exemplars are published to Pages |
| M4 | Refactor server extension dispatch and introduce the Plugin SDK. | `caliber/src/caliber/llm/openai_agents.py`, `caliber/src/caliber/orchestrator/optimizer_select.py`, provider modules, `caliber/src/caliber/storage/service.py`, `caliber/src/caliber/promoter.py` | `caliber/src/caliber/extensibility/`, `caliber/sdk/caliber-plugin-sdk/pyproject.toml`, `caliber/sdk/caliber-plugin-sdk/src/caliber_plugin_sdk/`, conformance tests, example plugin | M0 | a third-party optimizer/provider can load without CALIBER source edits |
| M5 | Add operator tooling on top of the stable SDK contract. | CI/docs wiring as needed | `caliberctl` package/entrypoint, optional TS SDK package, optional async client | M2 for CLI, M3 for TS/async, M4 if plugin-admin commands are included | common operator flows run non-interactively |

### Detailed milestone tasks

#### M0 — Server contract normalization

Implementation tasks:

- Add a served management OpenAPI document and register it centrally in [caliber/src/caliber/routes/__init__.py](../../caliber/src/caliber/routes/__init__.py).
- Formalize missing request/response schemas in [caliber/src/caliber/schemas.py](../../caliber/src/caliber/schemas.py) and, if needed, add route-local schema modules only where the shared schema file becomes too crowded.
- Add stability metadata for public operations and make `/capabilities` expose enough machine-readable detail for SDK capability detection.
- Implement PAT issuance, listing, rotation/revocation, and audit coverage using:
  - [caliber/src/caliber/auth.py](../../caliber/src/caliber/auth.py)
  - [caliber/src/caliber/routes/auth.py](../../caliber/src/caliber/routes/auth.py)
  - a new DB migration under `caliber/src/caliber/db/migrations/versions/`
- Keep CSRF semantics explicit for SDK clients by documenting or adjusting the interaction between:
  - [caliber/src/caliber/routes/csrf.py](../../caliber/src/caliber/routes/csrf.py)
  - [caliber/src/caliber/routes/auth.py](../../caliber/src/caliber/routes/auth.py)
- Add/extend focused tests:
  - `caliber/tests/test_routes_openapi.py`
  - `caliber/tests/test_auth.py`
  - `caliber/tests/test_auth_sessions.py`
  - `caliber/tests/test_auth_scopes.py`
  - `caliber/tests/test_auth_csrf_composition.py`
  - `caliber/tests/test_routes_capabilities.py`

Suggested validation commands:

- `pytest caliber/tests/test_auth.py caliber/tests/test_auth_sessions.py caliber/tests/test_auth_scopes.py caliber/tests/test_auth_csrf_composition.py caliber/tests/test_routes_capabilities.py`
- `pytest caliber/tests/test_routes_openapi.py`

Milestone acceptance:

- Management OpenAPI is served from the CALIBER management API.
- The Phase 1 GA route set has formal schemas and stability metadata.
- PAT-based automation auth works without password login.
- A generated or minimally generated smoke client can authenticate and call at least one GA endpoint.

#### M1 — Python SDK skeleton

Implementation tasks:

- Create the package skeleton under:
  - `caliber/sdk/caliber-sdk/pyproject.toml`
  - `caliber/sdk/caliber-sdk/src/caliber_sdk/`
- Implement the base client and transport primitives:
  - `client.py`
  - `transport.py`
  - `auth.py`
  - `csrf.py`
  - `errors.py`
  - `waiters.py`
  - `resources/raw.py`
- Add shared model/error plumbing before resource fan-out:
  - `models/common.py`
  - `models/errors.py`
  - `resources/_base.py`
- Add package-local tests for:
  - auth wiring
  - CSRF bootstrap
  - retry behavior
  - error normalization
  - raw transport

Suggested validation commands:

- `cd caliber/sdk/caliber-sdk && pytest`
- `cd caliber/sdk/caliber-sdk && python -m build`

Milestone acceptance:

- `pip install caliber-sdk` works without installing `mlflow`.
- The client can authenticate, fetch CSRF when needed, and execute raw requests.
- Error normalization is consistent across 4xx/5xx responses.

#### M2 — GA Python SDK modules

Implementation tasks:

- Implement resource modules for the GA set in `caliber/sdk/caliber-sdk/src/caliber_sdk/resources/`:
  - `auth.py`, `me.py`, `capabilities.py`, `settings.py`
  - `prompts.py`, `skills.py`, `tools.py`, `agents.py`
  - `workflows.py`, `services.py`
  - `projects.py`, `files.py`
  - `eval_datasets.py`, `evaluations.py`, `judges.py`
- Add typed model modules aligned to the served OpenAPI and existing route schemas.
- Add waiters for:
  - workflow runs
  - service invocations
  - evaluation-style long-running operations that are already part of the GA contract
- Add end-to-end examples for:
  - prompt lifecycle
  - workflow service publication/invocation
  - project/file operations
- Add SDK developer docs in the existing docs pipeline:
  - quickstart and install/auth pages under `docs/sdk/`
  - GA API reference pages under `docs/sdk/`
  - searchable method/model entries through `docs-site/build-docs.mjs` and the docs UI search layer in `docs-site/docs.js`
  - sync through `caliber/caliber-ui/scripts/sync-docs.mjs`
- Add docs correctness gates so examples shown in docs are tested and generated docs stay in sync with their sources.

Suggested validation commands:

- `cd caliber/sdk/caliber-sdk && pytest`
- run the example scripts against a local CALIBER deployment
- `node docs-site/build-docs.mjs`
- `node caliber/caliber-ui/scripts/sync-docs.mjs`
- `pytest caliber/tests/test_docs_generation_contract.py`

Milestone acceptance:

- Normal GA workflows do not require `client.raw.*`.
- Examples work against a local deployment.
- Capability checks fail cleanly when a server lacks a supported feature.
- Searchable HTML SDK docs for the GA surface are generated, synced, and ready for GitHub Pages publication.

#### M3 — Beta + agentic SDK modules

Implementation tasks:

- Implement beta resource modules in `caliber/sdk/caliber-sdk/src/caliber_sdk/resources/`:
  - `mcp_servers.py`, `gateway.py`
  - `knowledge.py`, `object_store.py`
  - `jobs.py`, `review_queues.py`, `aria.py`
  - `releases.py`, `rollback.py`
  - `observability.py`, `audit.py`, `events.py`
  - `cookbooks.py`, `secrets.py`
- Add SSE event streaming support and reconnection behavior.
- Add explicit agentic-workflow examples that cover:
  - Aria plan creation/approval/execution/polling
  - review queue item writeback
  - workflow/job waiter usage
- Add SDK cookbook documentation pages that use the SDK as the exemplar implementation path:
  - cookbook pages authored from the tested SDK examples and/or SDK-owned cookbook walkthroughs,
  - beta API reference pages for `mcp_servers`, `knowledge`, `aria`, `review_queues`, `jobs`, `observability`, and `releases`,
  - searchable API/method anchors and cookbook entry points in the published docs site.
- Keep backend limitations explicit in docs and tests:
  - full autonomous Aria artifact materialization is still not done
  - document extraction is still not portable until the backend contract changes

Suggested validation commands:

- `cd caliber/sdk/caliber-sdk && pytest`
- `pytest caliber/tests/test_aria_plans.py caliber/tests/test_routes_review_queues.py caliber/tests/test_routes_jobs.py caliber/tests/test_routes_services.py`
- `node docs-site/build-docs.mjs`
- `node caliber/caliber-ui/scripts/sync-docs.mjs`
- `pytest caliber/tests/test_docs_generation_contract.py caliber/tests/test_ci_published_site_gate_contract.py`

Milestone acceptance:

- Current operator-assisted agentic workflows can be implemented from the SDK without browser-only steps.
- Beta modules are labeled clearly in docs and public API metadata.
- Cookbook support reaches the documented 15/16 bar.
- GitHub Pages publishes the SDK API docs and SDK cookbook exemplars through the existing docs-site pipeline.

#### M4 — Plugin SDK + registry refactor

Implementation tasks:

- Introduce registry-based dispatch in the server before publishing plugin contracts:
  - refactor [caliber/src/caliber/llm/openai_agents.py](../../caliber/src/caliber/llm/openai_agents.py)
  - refactor `caliber/src/caliber/orchestrator/optimizer_select.py`
  - add `caliber/src/caliber/extensibility/` for registry/entrypoint loading
- Define experimental plugin protocols and contexts in:
  - `caliber/sdk/caliber-plugin-sdk/src/caliber_plugin_sdk/`
- Add conformance tests and one reference plugin.
- Keep the first Plugin SDK scope narrow:
  - optimizer registration first
  - provider/storage/promoter extension points only after optimizer registration is stable

Suggested validation commands:

- `pytest caliber/tests/test_orchestrator_optimizer_select.py caliber/tests/test_openai_agents_provider.py`
- package-local plugin SDK tests

Milestone acceptance:

- A third-party wheel can register an optimizer through the registry mechanism.
- CALIBER can allowlist or disable plugins explicitly.
- The plugin contract is still marked experimental until it survives a release.

#### M5 — CLI and optional follow-ons

Implementation tasks:

- Build a thin CLI on top of `caliber-sdk` for common operator flows.
- Only after the Python SDK is stable:
  - add a TypeScript SDK from the same OpenAPI contract or extracted shared client logic
  - add an async Python client if sync patterns, SSE, and file transfer behavior are already stable

Suggested validation commands:

- CLI smoke tests in CI
- package build tests for the CLI and any TS SDK package

Milestone acceptance:

- Common operator procedures run non-interactively.
- The CLI does not invent new backend semantics; it remains a thin wrapper over the SDK.

### Recommended branch / merge sequence

To reduce review risk, ship this work in small, reviewable branches:

1. server-contract branch:
   OpenAPI, schemas, PATs, stability metadata, contract tests
2. sdk-skeleton branch:
   package layout, transport, auth, errors, raw client, waiters
3. sdk-ga-resources branch:
   GA resource modules + examples
4. sdk-beta-agentic branch:
   beta modules + streaming + agentic examples
5. plugin-registry branch:
   server registry refactor + experimental Plugin SDK
6. cli-and-follow-ons branch:
   CLI, optional TS SDK, optional async client

This sequence preserves the main dependency edges and keeps the public SDK from outrunning the server contract.

---

## 10) Recommendations (what to do next)

1) Decide the “SDK contract boundary”:
   - Recommendation: bless the existing `/ajax-api/2.0/mlflow/caliber` as the first public management API for SDK purposes.
   - Add a served OpenAPI document and stability metadata before introducing any alias.
   - Introduce `/api/caliber/v1` only if a later product or deployment need justifies a second public path.

2) Prioritize a token story for automation:
   PATs with scopes are the biggest unlock for real-world SDK adoption.

3) Start with Python SDK:
   CALIBER’s ecosystem and MLflow context make Python the highest ROI first.

4) Treat the plugin SDK as a one-way door:
   ship it only after registry dispatch exists, the conformance harness is in place, and the contract has survived at least one release as `experimental`.

---

## Appendix A — Code references (current truth sources)

- Route registration: `caliber/src/caliber/routes/__init__.py`
- Envelopes & list helpers: `caliber/src/caliber/routes/_deps.py`
- Error shapes: `caliber/src/caliber/routes/_errors.py`
- Auth resolution + headers: `caliber/src/caliber/auth.py`
- CSRF issuance: `caliber/src/caliber/routes/csrf.py`
- UI API client (current internal TS SDK): `caliber/caliber-ui/src/api/caliberApi.ts`
- Pydantic request/response models: `caliber/src/caliber/schemas.py`
- Knowledge schemas: `caliber/src/caliber/knowledge/schemas.py`
- Workflow service OpenAPI: `caliber/src/caliber/routes/services.py`
- Extensibility seams:
  - LLM provider: `caliber/src/caliber/llm/provider.py`
  - Eval provider: `caliber/src/caliber/eval/provider.py`
  - Artifact store: `caliber/src/caliber/artifact_store.py`
  - Promoter: `caliber/src/caliber/promoter.py`
  - Storage backend: `caliber/src/caliber/storage/service.py`
  - Event bus: `caliber/src/caliber/events/nats_bus.py`
