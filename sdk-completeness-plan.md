# CALIBER SDK Completeness Plan

> **Status:** Proposed · **Date:** 2026-08-30 · **Scope:** `sdk/caliber-sdk` (primary),
> `sdk/caliber-cli`, `sdk/caliber-plugin-sdk`, and the server + SPA contract surfaces they
> depend on.
>
> **Purpose.** Define what "the CALIBER SDK is complete" means, prove where it is not
> complete today with reproducible measurements, and lay out a phased plan that closes the
> gap *and* keeps it closed as the product grows.

---

## 0 · Executive summary

The CALIBER SDK has an **excellent foundation and an incomplete surface**. Transport,
error mapping, authentication, waiters, and forward/backward-compatible decoding are
carefully designed and well tested. What is missing is *breadth* and, more importantly,
**any mechanism that makes breadth a tracked, enforced property**.

Measured against the live server route table built from `create_app()`:

| Measure | Value |
| --- | --- |
| Total operations incl. SPA shell routes | 390 |
| **API operations** (excl. SPA shell) | **386 across 45 route modules** |
| Operations reachable through a typed SDK method | **197 (51.0 %)** |
| Addressable operations (after documented exclusions, §2.8) | **380** |
| **Addressable coverage** | **197 / 380 = 51.8 %** |
| Addressable operations with no typed method | **183** |
| Route modules with **zero** typed coverage | **14** |
| Async client resources vs. sync client resources | **6 vs. 27** |
| Route tags published to users as "Typed SDK" with < 80 % coverage | **13** |
| SPA API-client methods (the UI's full reach) | **358** |
| Automated checks that fail when the SDK lags a new route | **0** |

> **Measurement validated.** The 386/45 figures reproduce
> [`ARCHITECTURE.md §1`](ARCHITECTURE.md)'s independently-documented "386 HTTP operations
> across 45 route modules" exactly, which confirms the extraction method rather than
> merely asserting it.

The last row is the root cause; every other number is a symptom. The repository has a
genuinely strong parity net — server↔OpenAPI is exact and bidirectional, docs↔SDK is
enforced at module and symbol level — but there is **no SDK↔API coverage gate**, so the
SDK falls behind silently and the published documentation overstates what it covers.

This plan therefore treats **the gate as the primary deliverable** and the missing
endpoints as work the gate schedules, not the other way round.

---

## 1 · Current state

### 1.1 The three distributions

| Package | Purpose | Direction | Runtime deps |
| --- | --- | --- | --- |
| **`caliber-sdk`** | Typed Python client for the management API | outside → in | `httpx`, `typing-extensions` |
| **`caliber-cli`** (`caliberctl`) | Terminal front-end; thin wrapper, **zero HTTP logic of its own** | outside → in | `caliber-sdk` only |
| **`caliber-plugin-sdk`** | Contracts for third-party **optimizers** CALIBER calls into | inside → out | **none** (CI-enforced) |

`caliber-sdk` at a glance ([`sdk/caliber-sdk/src/caliber_sdk/`](sdk/caliber-sdk/src/caliber_sdk/)):

- **Root client** — [`client.py`](sdk/caliber-sdk/src/caliber_sdk/client.py): 27 resource
  attributes plus `whoami()`, `capabilities()`, `openapi()`, `health()`, `stability`.
- **Transport** — [`transport.py`](sdk/caliber-sdk/src/caliber_sdk/transport.py): envelope
  unwrapping, typed errors, idempotent-only retries with capped backoff and `Retry-After`,
  one-shot CSRF bootstrap-and-replay, project header, `X-Request-Id` correlation,
  `download()`, `stream_lines()`, `paginate()`.
- **Errors** — [`errors.py`](sdk/caliber-sdk/src/caliber_sdk/errors.py): 11 classes mapped
  from status code, each carrying status, detail, method, URL, request id, raw payload.
- **Models** — ~55 frozen dataclasses across 8 modules, decoded tolerantly by
  [`models/_decode.py`](sdk/caliber-sdk/src/caliber_sdk/models/_decode.py).
- **Async** — [`aio/`](sdk/caliber-sdk/src/caliber_sdk/aio/): a second transport plus **6** resources.
- **CLI** — 22 commands reaching 9 of the client's 26 resource attributes; 7 named exit
  codes including `AWAITING_HUMAN` (3) and `GATE_FAILED` (4), which is a genuinely good
  governance-aware design.

### 1.2 What is good — and must be preserved

1. **Zero server dependency**, asserted in CI ([`ci.yml:198`](.github/workflows/ci.yml)).
2. **Decisions shared, not copied.** [`test_async_parity.py`](sdk/caliber-sdk/tests/test_async_parity.py)
   asserts *object identity* — not equality — for `_RETRYABLE_METHODS`, `_RETRYABLE_STATUS`,
   `_decode`, `_unwrap`, `API_PREFIX`, `USER_AGENT`, `Response`.
3. **Forward and backward compatible decoding** — unknown keys preserved in `extra`,
   missing keys defaulted.
4. **Honest retry semantics** — `POST` is never auto-retried.
5. **A permanent escape hatch** — `client.raw` reaches any endpoint.
6. **Stability tiers are a real server contract** — `GET /capabilities` serves
   `sdk_stability`, and a test asserts it never disagrees with the OpenAPI document's
   per-operation `x-caliber-stability`.
7. **Docs are generated and gated** — published examples are *executed*, and every
   documented route is checked to exist against the live OpenAPI document.

### 1.3 Established design principles (extracted from the code, not invented here)

| # | Principle | Source |
| --- | --- | --- |
| S1 | **Simple first.** The common case is a URL and a token. | [`client.py`](sdk/caliber-sdk/src/caliber_sdk/client.py) |
| S2 | **The SDK must never be the reason something is impossible.** | [`resources/_base.py`](sdk/caliber-sdk/src/caliber_sdk/resources/_base.py) |
| S3 | **No server dependencies.** | [`pyproject.toml`](sdk/caliber-sdk/pyproject.toml), CI-enforced |
| S4 | **Degrade in both directions, fail in neither.** | [`models/_decode.py`](sdk/caliber-sdk/src/caliber_sdk/models/_decode.py) |
| S5 | **One decision, one object.** Sync and async share decisions, never copies. | [`test_async_parity.py`](sdk/caliber-sdk/tests/test_async_parity.py) |
| S6 | **Never retry what cannot be proven safe to retry.** | [`transport.py`](sdk/caliber-sdk/src/caliber_sdk/transport.py) |
| S7 | **A failure must be actionable in someone else's CI log.** | [`errors.py`](sdk/caliber-sdk/src/caliber_sdk/errors.py) |
| S8 | **Resource shape follows the caller's mental model, not the route layer's.** Five route groups became one `workflows` tree. | [`resources/workflows.py`](sdk/caliber-sdk/src/caliber_sdk/resources/workflows.py) |

These map onto the repository's ten principles
([`ARCHITECTURE.md`](ARCHITECTURE.md#design-principles)): S2/S3/S8 discharge *Open &
Extensible* and *Modular & Composable*; S4–S7 discharge *Scalable & Reliable* and
*Auditability & Observability*; S1 discharges *Developer & User Friendly*.

---

## 2 · UI → API → SDK mapping, and the gaps it reveals

### 2.1 How the UI reaches the backend

The SPA routes **every** backend call through one client,
[`caliber-ui/src/api/caliberApi.ts`](caliber/caliber-ui/src/api/caliberApi.ts) (5,019
lines, **358 methods**). There is **zero** direct `fetch` in pages or components, which
makes that file an authoritative, machine-readable statement of the UI's reach — and the
natural reference for "everything available through the UI."

Two caveats that shape how it is used in this plan:

- **The UI client is ahead of the UI.** 42 of its methods have no call site in any page or
  component. They are capability the client knows about but no screen exposes.
- **The UI client is, in one place, ahead of the *server*.** It defines **7 methods
  against `/verification-queue*`** (`listVerificationItems`, `getVerificationItem`,
  `createVerificationItem`, `batchVerificationAction`, `verifyItem`, `dismissItem`,
  `markDuplicate`) for which **no server route is registered** — confirmed by grepping the
  live route table (0 matches) and the route modules (0 path registrations). This is dead
  client code and is reported as a defect in §2.7, not as SDK scope.

**Consequence for this plan:** the SDK's coverage denominator is the **server route
table**, not the UI client. The UI client is used as the *design reference* for ergonomics
— it shows how endpoints are composed into real tasks — but it cannot be the target of
record, because it both overshoots and undershoots the real API.

### 2.2 Feature-area mapping: UI ↔ SDK

Every area below is fully reachable in the UI. The right column is what the SDK offers.

| UI area (SPA route) | SDK coverage | Notable SDK gaps |
| --- | ---: | --- |
| Prompts (`/prompts`, 7.7k LOC page) | 6/22 (27 %) | rollback, baseline, bind, test-runs, template library, calibration + optimization runs, workspace |
| Workflows authoring (`/workflows/:id/editor/:versionId`, 5.3k LOC) | 7/20 versions (35 %) | diff, restore, export manifest/python, preview-run, copilot-edit, plan-build, propose-patch |
| Workflow execution (`/workflow-runs/:runId`, 3k LOC debugger) | 3/15 (20 %) | events, trace, lineage, manifest, checkpoints, approvals approve/reject, retry, resume, trigger |
| Workflow deployment & promotion | 0/6 (0 %) | deployments list, promote, rollback, promotion approve/reject |
| Skills (`/skills`) | 7/19 (37 %) | rollback, baseline, bind, calibrate, package import/export, test-runs, workspace |
| Tools (`/tools`) | 7/20 (35 %) | test-runs, test-cases, source, usage, versions, archive, baseline, calibration-job resolve |
| Agents (`/agents`) | **0/7 (0 %)** | the entire family — and it is tagged `ga` |
| Agent rollback | **0/2 (0 %)** | checkpoints, rollback |
| Aria assistant chat panel (1.4k LOC) | **0/29 (0 %)** | sessions, messages, queue, attachments, intent, plans, drafts, runs, config |
| Aria plans (`/aria/plans`) | 10/10 (100 %) | — |
| Knowledge bases (`/knowledge-bases`, 9k LOC — largest page) | 24/24 (100 %) | — |
| OpenAPI integrations | 23/23 (100 %) | — |
| MCP servers | 13/13 (100 %) | — |
| Object store (`/object-store`) | 11/13 (85 %) | object upload, object download |
| Test sets (`/eval-datasets`) | 6/11 (55 %) | update, revise, supersede, restore, MLflow sync |
| Judges (`/judges`) | 4/6 (67 %) | update, test-run |
| Review queues | 7/7 (100 %) | — |
| Evaluations | 3/3 (100 %) | — |
| Releases & recovery (`/releases`) | 7/13 (54 %) | live targets, operations, reconcile, resolve, timeline |
| System effects / dead letters | **0/5 (0 %)** | effect ledger, webhook replay/acknowledge |
| Observability (`/observability`) | 4/7 (57 %) | trace feedback |
| LLM Gateway (`/gateway`) | 7/9 (78 %) + 0/4 pricing | guardrail update/delete; all LLM pricing |
| Audit log (`/audit-log`) | 1/2 (50 %) | CSV/JSON export |
| Settings & administration | 3/3 settings, 0/6 system services | system health, queue, incidents, alerts |
| Dashboard (`/`) | **0/1 (0 %)** | summary |
| Cookbooks | 2/2 (100 %) | — |
| Gate verdicts (cross-artifact version panel) | **0/2 (0 %)** | read + record verdicts |

**The pattern in the gaps is not random.** The uncovered set is concentrated in exactly
the governance verbs the platform exists to provide: **rollback** (prompts, skills,
agents, workflow deployments), **promotion approve/reject**, **release reconciliation**,
**gate verdicts**, and the **effect ledger**. A developer can author assets through the
SDK but cannot drive the governed release path that
[`ARCHITECTURE.md §2`](ARCHITECTURE.md) presents as the product's core value.

### 2.3 Gap A — Coverage: 183 addressable operations have no typed method

**14 route modules have zero typed coverage.**

| Module | Ops | Cov | % | What is unreachable |
| --- | ---: | ---: | ---: | --- |
| `assistant` | 29 | 0 | 0 % | **Largest single gap** — Aria conversational sessions, messages, drafts, attachments, plan execution, queue |
| `files` | 9 | 0 | 0 % | Workflow-run and playground artifacts; upload + content download |
| `agents` | 7 | 0 | 0 % | **Tagged `ga`**; the anchor record jobs and approvals hang off |
| `system_services` | 6 | 0 | 0 % | Service health, queue depth, incidents, alerts |
| `workflow_deployments` | 6 | 0 | 0 % | Deployment aliases, promote, rollback, promotion approve/reject |
| `system_effects` | 5 | 0 | 0 % | Effect ledger, webhook dead letters, replay |
| `llm_pricing` | 4 | 0 | 0 % | Per-model pricing CRUD |
| `memory` | 4 | 0 | 0 % | Workflow session memory |
| `workflow_calibration` | 2 | 0 | 0 % | Calibration options and run submission |
| `gate_verdicts` | 2 | 0 | 0 % | Per-version advisory release evidence |
| `rollback` | 2 | 0 | 0 % | Agent checkpoints and rollback |
| `dashboard` | 1 | 0 | 0 % | Summary tiles |
| `events_stream` | 1 | 0 | 0 % | SSE live events (transport supports it; no typed resource) |
| `metrics` | 1 | 0 | 0 % | Prometheus text — *excluded, §2.8* |
| `workflow_runs` | 15 | 3 | 20 % | resume, retry, approvals, events, checkpoints, lineage, manifest, trace |
| `prompts` | 22 | 6 | 27 % | rollback, baseline, bind, test-runs, template library, calibration |
| `workflows` | 16 | 5 | 31 % | import/export, components, templates, cron preview, benchmark reports |
| `tools` | 20 | 7 | 35 % | test-runs, test-cases, source, versions, usage, archive, baseline |
| `workflow_versions` | 20 | 7 | 35 % | diff, export, restore, preview-run, propose-patch, copilot-edit |
| `skills` | 19 | 7 | 37 % | rollback, baseline, bind, calibrate, package import/export |
| `services` | 10 | 5 | 50 % | service token management |
| `releases` | 13 | 7 | 54 % | live, operations, reconcile, resolve, timeline |
| `eval_datasets` | 11 | 6 | 55 % | update, revise, supersede, restore, sync |
| `observability` | 7 | 4 | 57 % | trace feedback |
| `judges` | 6 | 4 | 67 % | update, test-run |
| `auth` | 11 | 8 | 73 % | session revocation (login/logout excluded, §2.8) |
| `gateway` | 9 | 7 | 78 % | guardrail update/delete |
| `object_store` | 13 | 11 | 85 % | object upload/download |
| `projects` | 14 | 12 | 86 % | file upload, content download |
| `audit` | 2 | 1 | 50 % | CSV/JSON export |
| `health` | 2 | 1 | 50 % | readiness probe |
| `knowledge_bases`, `openapi_integrations`, `mcp_servers`, `aria_plans`, `review_queues`, `jobs`, `secrets`, `settings`, `evaluations`, `cookbooks`, `me`, `openapi`, `capabilities` | — | — | **100 %** | The proof the resource pattern works when applied |

### 2.4 Gap B — Async is a second, narrower, hand-written client

The async client exposes **6** resources (`raw`, `me`, `capabilities_api`, `workflows`,
`jobs`, `events`) against the sync client's **27**, and `AsyncWorkflowRunsAPI`
re-implements `submit`/`get`/`list`/`cancel`/`wait` rather than sharing them.

This is **documented as deliberate** — `test_the_async_client_documents_what_it_does_not_cover`
asserts the module docstring says so — and that honesty is worth preserving. But the
*mechanism* does not scale: every new endpoint must be written twice, and in practice gets
written once. **The decision to have a narrow async surface is defensible; maintaining it
by hand-duplication is what must change.**

### 2.5 Gap C — "Typed" is thinner than it looks

1. **Untyped keyword escape hatches** — 60 occurrences of `**options: Any` /
   `**changes: Any`. `workflows.create(name, **options)` validates nothing client-side; a
   typo becomes a server 400 rather than an editor error.
2. **83 methods return bare `Any`** versus 177 returning a typed model. Some are
   principled (`WorkflowVersionsAPI.validate` defers to the server's validator — "a schema
   here would be a second definition of a contract that lives there"). Many are simply
   unmodelled.
3. **No typed request models at all.** The SDK models responses only, so request
   construction is untyped dict-building everywhere.

### 2.6 Gap D — Cross-cutting mechanics the UI has and the SDK lacks

These are the capabilities that make the missing endpoints *hard*, not merely numerous.
Each is a first-class UI behaviour with no SDK equivalent.

| Mechanic | UI reality | SDK today |
| --- | --- | --- |
| **Multipart upload** (6 endpoints) | object-store objects, workflow-run files, playground-run files, project files, assistant attachments, skill package ZIP — all via one `uploadMultipart` helper | `Transport.request()` accepts `files=`, but **no resource method uses it** and there is no upload helper |
| **File download** (9 helpers) | audit-log export blob, workflow manifest/python export, service OpenAPI, skill package ZIP, object-store download/view, run-file content, project-file content, Allure report | `Transport.download()` exists; **no resource method calls it**. 6 of the UI helpers return *raw URL strings* for the browser — an SDK must actually fetch bytes instead |
| **SSE streaming** | one endpoint, `GET /events/stream`, consumed with **typed frames** (`approval.promoted`, …) by 4 pages | `Transport.stream_lines()` exists; `EventsAPI` yields **raw strings**, no typed frames |
| **Long-running polling** | 6+ distinct poll loops: Aria plan execute, tool calibration jobs, KB builds (8 sites), workflow runs (7 sites), prompt calibration/optimization, health | Good generic `wait_for`/`wait_for_terminal_state`, but wired to only 3 operations |
| **Bulk operations** | verification batch *(server route absent, §2.7)*, object-store bulk delete by key array **and** folder prefix, review-queue bulk item add | Only review-queue bulk add is typed |
| **Cursor pagination** | `GET /workflows/{id}/runs?limit=&cursor=` returns `next_cursor` | `paginate()` recomputes its own offset and **ignores `next_cursor`** — see §2.7 |
| **Idempotency** | prompt release mutations reuse a client-generated `REL-ui-<uuid>` operation id across ambiguous retries | `idempotency_key` is a JSON body field on exactly **one** method |

### 2.7 Gap E — No SDK↔API parity gate (the root cause), plus contract inconsistencies

**The missing edge.** The repository's parity net is strong everywhere except here:

| Edge | Enforced? | Where |
| --- | --- | --- |
| server ↔ OpenAPI document | ✅ exact, bidirectional set comparison | `caliber/tests/test_routes_openapi.py` |
| docs ↔ SDK package | ✅ module- and symbol-level coverage | `caliber/tests/test_sdk_docs_contract.py` |
| docs ↔ real routes / CLI / env | ✅ regex-checked against live OpenAPI | `caliber/tests/test_docs_executable_spec_contract.py` |
| SDK ↔ server *correctness* | ✅ but only on paths already modelled | `caliber/tests/test_sdk_against_server.py` |
| **SDK ↔ API *coverage*** | ❌ **nothing** | — |

`test_sdk_against_server.py` is explicitly "the only place the SDK is driven against the
real application," and its coverage tests are named
`test_the_sdk_lists_every_ga_surface_it_models` — *it models*. **Nothing fails when the
server gains a route the SDK ignores.** That is how coverage reached 51 % without a single
red build.

**The published table overstates and fails open.**
[`docs-site/generate_rest_api_docs.py:34`](docs-site/generate_rest_api_docs.py) defines
`SDK_SURFACE_MAP`, a hand-maintained dict rendering an "SDK coverage" column:

- **Tag-granular, so it overstates.** A tag shows "Typed SDK" if *any* method touches it.
  **13 tags are published as "Typed SDK" with < 80 % operation coverage** — `workflow-runs`
  20 %, `prompts` 27 %, `workflows` 31 %, `tools` 35 %, `workflow-versions` 35 %, `skills`
  37 %, `services` 50 %, `releases` 54 %, `eval-datasets` 55 %, `observability` 57 %,
  `judges` 67 %, `auth` 73 %, `gateway` 78 %.
- **Fails open.** `_sdk_surface_row()` silently returns "Raw only" for any unmapped tag, so
  a brand-new GA route family renders as *deliberately unwrapped*.
- **Untested.** Nothing verifies its `client.x.y` strings resolve to real attributes.

**Other contract inconsistencies found:**

| Issue | Detail |
| --- | --- |
| **Dead UI client code** | `caliberApi.ts` defines 7 `/verification-queue*` methods; **no such server route exists**. Either the routes were removed and the client not cleaned up, or the feature was never shipped. Needs a decision, then cleanup or implementation. |
| **Pagination is inconsistent** | Most list endpoints return a bare array in `{"data": [...]}` with `?limit`/`?offset` and **no total**. Exactly one — `GET /workflows/{id}/runs` — returns `next_cursor` (an integer offset under the hood, `workflow_versions.py:1632`). `Transport.paginate()` handles neither well: it re-derives offset and ignores `next_cursor`, so it works by luck today and breaks if cursors become opaque. |
| **Dead SDK abstraction** | `models.common.Page` is defined, exported in `__all__`, and **used nowhere**. |
| **Naming** | `client.capabilities_api` is named around a collision with `capabilities()`; `client.datasets` fronts `EvalDatasetsAPI`; `client.aria` covers `/aria/plans/*` but **not** `/assistant/*` — a developer reasonably expects `aria` to mean Aria. |
| **Stability promises exceed delivery** | `agents` is `STABILITY_GA` with **0 %** coverage. |
| **Tolerant decode masks shape errors** | `decode_list` returns `[]` for any non-list payload — indistinguishable from a genuinely empty list, so a renamed response key degrades to silence. |
| **No `Makefile` targets** | The root [`Makefile`](Makefile) has no `sdk`, `cli`, or `plugin-sdk` target; those jobs live only in CI, so a contributor cannot run the gate the way they run every other component's. |
| **Docs teach `raw` where typed methods exist** | The SDK README's "Getting a token" uses `caliber.raw.post("/auth/tokens", ...)` though `client.auth.tokens.create(...)` exists. |
| **CLI docs drift** | `prompt` and `service` command groups exist in the parser but are absent from the CLI README's command table. |

### 2.8 Explicitly out of scope (documented exclusions)

| Operations | Why excluded |
| --- | --- |
| `GET /`, `/caliber`, `/caliber/`, `/caliber/{path}` (4) | SPA shell, not API — already excluded from the 386 count |
| `GET /csrf` (1) | Handled inside the transport by `bootstrap_csrf()`; a resource method would duplicate it |
| `POST /auth/login`, `POST /auth/logout` (2) | Browser cookie session. The SDK authenticates with tokens by design; the README states cookie auth is deliberately not modelled |
| `GET /observability/allure-report{,/path}` (2) | Static HTML report proxy |
| `GET /metrics` (1) | Prometheus text exposition, not JSON — belongs to a scraper |

**Addressable surface = 386 − 6 = 380. Current addressable coverage = 197/380 = 51.8 %.**

---

## 3 · Target state

### 3.1 The definition of "complete"

> **The CALIBER SDK is complete when every addressable server operation is reachable
> through a typed, documented, tested method whose existence is enforced by CI — and when
> adding a route to the server without adding SDK coverage turns a build red.**

Completeness is a **ratchet**, not a milestone. Three properties:

1. **Total addressable coverage** — every operation outside §2.8 has a typed method.
2. **Enforced** — a machine-checked gate, not a hand-maintained table.
3. **Sustained** — the gate makes regression impossible, so the property survives growth.

### 3.2 Target architecture

```text
caliber_sdk/
├── client.py            CaliberClient          — sync facade
├── aio/client.py        AsyncCaliberClient     — async facade (full parity)
├── _core/                                      ← NEW: shared, transport-agnostic core
│   ├── operations.py    one declarative record per API operation
│   ├── binding.py       operation -> bound method (sync + async from one spec)
│   └── registry.py      the operation table the parity gate reads
├── resources/           typed facades (sync)   — thin; call into _core
├── models/
│   ├── responses/       response dataclasses (existing, reorganised)
│   └── requests/                               ← NEW: typed request models
├── transport.py / aio/transport.py             — + idempotency, cursor pagination,
│                                                 upload/download helpers
├── errors.py                                   — unchanged
└── waiters.py                                  — unchanged
```

**The single architectural decision that resolves Gaps A, B, C and E at once:** a
**declarative operation registry** — one record per API operation naming its method, path,
request model, response model, stability tier, and scope — from which *both* sync and
async bound methods are produced, and which the parity gate diffs against the served
OpenAPI document.

This preserves S5 ("one decision, one object") and extends it from transport decisions to
the resource surface itself. S8 survives because the registry describes *operations* while
the hand-written facade decides how they are *grouped* — so `workflows.versions`/`runs`/
`services` nesting is unaffected.

### 3.3 What the target does *not* change

- The `raw` escape hatch stays permanently (S2).
- Zero server dependencies (S3) — the registry is plain data, not a server import.
- Tolerant decoding (S4) and never-retry-a-POST (S6) are unchanged.
- Response models remain frozen dataclasses; **no pydantic** enters the client.
- A narrow async surface remains *available*: the registry can mark an operation
  sync-only, but that becomes an explicit, reviewable decision rather than an accident.

---

## 4 · Architectural decisions

### AD-1 · Declarative operation registry, hand-written facades

**Decision.** Describe every operation once as data. Generate bound sync and async methods
from it. Keep the caller-facing grouping hand-written.

**Rejected — full OpenAPI codegen.** The served document is generated from the live route
table and would give total coverage cheaply, but it would flatten the deliberate
caller-oriented grouping (S8), regenerate 55 carefully-named models into machine names, and
discard the prose docstrings that make this SDK teachable. Codegen wins on breadth and
loses on exactly the properties this SDK is good at.

**Rejected — continue hand-writing both clients.** This is the status quo that produced a
6-vs-27 split.

### AD-2 · The parity gate is the primary deliverable

**Decision.** Build the SDK↔OpenAPI coverage gate **first**, in Phase 1, with the current
183-operation gap encoded as an explicit, shrinking allowlist. Ship it red-to-green rather
than waiting for full coverage.

**Rationale.** A gate that lands last protects nothing during the work. A gate with an
allowlist lands immediately, prevents *new* drift on day one, and converts the backlog into
a countdown CI reports on every build.

**Rejected — fix endpoints first, gate at the end.** Coverage reached 51 % with no gate;
there is no reason to believe the last mile behaves differently.

### AD-3 · Typed request models, additive and optional

**Decision.** Introduce `models/requests/` dataclasses. Methods accept **either** the typed
model **or** the existing keywords, so no existing call breaks.

**Rejected — replacing `**options` outright.** A breaking change on an alpha SDK whose
published cookbook examples are executed by CI. Additive keeps S1 intact while making the
strict path available.

### AD-4 · Idempotency becomes a transport concern

**Decision.** Add an `idempotency_key` request option handled by the transport, replacing
the single body-field special case, available on every server-supported idempotent route.

**Rationale.** S6 says a `POST` is never auto-retried because safety cannot be proven. An
idempotency key is precisely that proof; making it a transport option is what could
eventually make write retries safe.

### AD-5 · Uniform pagination contract (cross-component)

**Decision.** Propose a server change: list envelopes carry `total` and a `next_cursor`,
uniformly. Adopt the shape `GET /workflows/{id}/runs` already uses rather than inventing a
third convention. Then activate `Page` and make `paginate()` honour `next_cursor`.

**Note.** This is the one item that **cannot be delivered inside `sdk/`** — it needs a
server contract change with its own OpenAPI implications. Scheduled in Phase 4; the SDK
ships without it because `paginate()`'s signature was designed to survive the move.

### AD-6 · Naming corrections behind deprecation aliases

**Decision.** `capabilities_api` → `capabilities_info`; `datasets` → `eval_datasets`;
`aria` grows to cover `/assistant/*` as `aria.sessions`, `aria.drafts`, `aria.plans`. Old
names remain as aliases emitting `DeprecationWarning` for one minor cycle.

### AD-7 · Upload and download are first-class SDK operations

**Decision.** Add `upload()` (multipart) and typed `download()` resource methods returning
**bytes or a streaming handle** — never a URL string.

**Rationale.** Six UI helpers return raw URLs because a browser can follow them. A Python
caller cannot; returning a URL would push authentication and error handling back onto the
user and violate S7. This is the clearest case where the SDK must *not* mirror the UI
client's shape.

---

## 5 · Implementation phases

Ordered so **the gate exists before the bulk work**, and each phase ends shippable and
independently reviewable. Sizes are relative, not calendar estimates.

### Phase 1 — Make the gap visible and enforced *(foundation; implements AD-2)*

| # | Deliverable | Acceptance |
| --- | --- | --- |
| 1.1 | `caliber/tests/test_sdk_api_coverage.py` — enumerate operations from `build_openapi_document(create_app())`, resolve SDK coverage, fail on any uncovered operation not in the allowlist | Red without the allowlist, green with it |
| 1.2 | `sdk/caliber-sdk/coverage_allowlist.toml` — the 183 addressable gaps each with an owning phase, plus §2.8 exclusions in a separate justified section | Every entry carries a reason |
| 1.3 | Derive `SDK_SURFACE_MAP` from the registry; make `_sdk_surface_row()` **fail closed** for unmapped `ga`/`beta` tags | The published table cannot overstate |
| 1.4 | Publish **per-operation** counts per tag, not a binary "Typed SDK" | `prompts` renders "6/22 typed" |
| 1.5 | `make sdk`, `make cli`, `make plugin-sdk`, `make sdk-coverage` in the root `Makefile` | A contributor can run the gate locally |
| 1.6 | Decide and act on the dead `/verification-queue*` UI client methods — implement the routes or delete the methods | `caliberApi.ts` references no non-existent route; add a UI-client↔route check to the docs contract suite |

> **This phase changes no SDK behaviour.** It makes the truth visible, publishes it
> accurately, and stops the bleeding.

### Phase 2 — The shared core and async parity *(structural)*

| # | Deliverable | Acceptance |
| --- | --- | --- |
| 2.1 | `_core/operations.py` + `registry.py`: declarative records for the 197 covered operations | Registry round-trips the existing surface |
| 2.2 | `_core/binding.py`: produce sync and async bound methods from one record | No behavioural change |
| 2.3 | Migrate existing resources onto the core, preserving **every** public name and docstring | Existing suite green, unchanged |
| 2.4 | Async parity for every registry operation not explicitly marked sync-only | `AsyncCaliberClient` exposes 27 resources |
| 2.5 | Extend `test_async_parity.py` to assert **surface** parity, not only decision sharing | A sync-only operation must be explicitly flagged |

### Phase 3 — Close the coverage gap *(bulk; allowlist shrinks to zero)*

Ordered by governance value, then user-visible impact.

| Wave | Modules | Ops | Rationale |
| --- | --- | ---: | --- |
| **3a · Governance verbs** | `rollback`, `gate_verdicts`, `workflow_deployments`, `releases` (rest), `system_effects` | 21 | The release/rollback path is the product's core claim and is currently unreachable |
| **3b · GA completion** | `agents`, `prompts`, `skills`, `tools`, `workflows`, `workflow_versions`, `workflow_runs`, `services` | 89 | Everything tagged `ga` fully typed; clears all 13 overstated tags |
| **3c · Assistant / Aria** | `assistant` (29), `memory` | 33 | Largest module; needs session, message, draft, attachment, plan-execution models |
| **3d · Files & content** | `files`, `object_store` rest, `projects` rest | 13 | Depends on AD-7 upload/download helpers |
| **3e · Operations surface** | `system_services`, `dashboard`, `observability` rest, `llm_pricing`, `eval_datasets` rest, `judges`, `gateway`, `auth`, `workflow_calibration`, `events_stream`, `audit`, `health` | 27 | Completes the addressable set |

**Exit condition: `coverage_allowlist.toml` contains only the §2.8 exclusions.**

### Phase 4 — Depth, ergonomics, and the server contract *(quality)*

| # | Deliverable |
| --- | --- |
| 4.1 | `models/requests/` typed request models for every mutating operation (AD-3) |
| 4.2 | Replace `**options`/`**changes` with typed parameters where a stable contract exists; keep them only where the server's schema is genuinely open-ended, each with a comment saying why |
| 4.3 | Transport-level `idempotency_key` (AD-4) |
| 4.4 | **Server change:** uniform `total` + `next_cursor` envelopes; activate `Page`; `paginate()` honours cursors (AD-5) |
| 4.5 | Typed SSE frame models for `events_stream`, mirroring the UI's typed frames |
| 4.6 | Naming corrections behind deprecation aliases (AD-6) |
| 4.7 | Strict mode for `decode_list` so "unexpected shape" is distinguishable from "empty", used by the contract tests |
| 4.8 | Bulk-operation helpers (object-store bulk delete by keys and by prefix) |

### Phase 5 — Versioning, documentation, release readiness *(graduation)*

| # | Deliverable |
| --- | --- |
| 5.1 | Documented versioning policy: SemVer; what `ga`/`beta`/`internal` promise; deprecation window (one minor cycle, `DeprecationWarning`, named removal release) |
| 5.2 | Graduate `caliber-sdk` from `0.1.0.dev0`/Alpha to `1.0.0` **only when Phase 3 exits and the allowlist is empty** |
| 5.3 | CLI expansion to the governance verbs (rollback, promote, gate verdicts); fix the README command table to match the parser |
| 5.4 | Regenerate SDK reference/cookbook docs; fix the README `raw` token example to use `client.auth.tokens.create` |
| 5.5 | Server/SDK compatibility matrix, published and tested |

---

## 6 · API / SDK coverage matrix

The authoritative matrix is machine-generated by the Phase 1 gate; §2.3 is its current
snapshot. Once 1.3–1.4 land the published REST inventory carries the same numbers,
replacing today's binary "Typed SDK" column.

**Reproduce the measurement:**

```bash
cd caliber && .venv/bin/python - <<'PY' > /tmp/routes.json
import json, os
os.environ.setdefault("CALIBER_DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("CALIBER_BACKGROUND_TASKS_ENABLED", "false")
from starlette.routing import Route
from caliber.server import create_app
app = create_app()
print(json.dumps([
    {"method": m, "path": r.path, "mod": r.endpoint.__module__}
    for r in app.routes if isinstance(r, Route)
    for m in sorted(r.methods or []) if m not in ("HEAD", "OPTIONS")
]))
PY
# then diff against SDK path literals — becomes tests/test_sdk_api_coverage.py in Phase 1
```

Coverage tiers used by the gate:

| Tier | Meaning | Gate behaviour |
| --- | --- | --- |
| **Typed** | A typed method exists, with a response model | Counts as covered |
| **Raw-only** | Reachable via `client.raw` only | **Not** covered; must be allowlisted with an owning phase |
| **Excluded** | §2.8 permanent exclusion | Covered by justification; reviewed when it changes |

---

## 7 · Testing and validation strategy

| Level | What it proves | Mechanism | Status |
| --- | --- | --- | --- |
| **L1 · Coverage** | Every addressable operation has a typed method | `test_sdk_api_coverage.py` vs. live OpenAPI | **NEW — Phase 1** |
| **L2 · Contract** | Paths, methods, decode keys match a real server | `test_sdk_against_server.py`, extended per wave | Exists; extend |
| **L3 · Surface parity** | Sync and async expose the same operations | `test_async_parity.py` extended (2.5) | Exists; extend |
| **L4 · Decision parity** | Shared decisions are one object, not two copies | identity assertions | **Exists — preserve unchanged** |
| **L5 · Unit** | Request shaping, error mapping, waiters, decoding | `pytest-httpx` suites | Exists |
| **L6 · Docs** | Published examples execute; documented routes exist | `test_sdk_docs_contract.py`, `test_docs_executable_spec_contract.py` | **Exists — strong** |
| **L7 · Packaging** | No server dependency; wheel builds; py3.10 floor | `test_packaging.py` + CI assertion | Exists |
| **L8 · UI-client integrity** | The SPA client references no non-existent route | New check (1.6), same technique as L6 | **NEW — Phase 1** |
| **L9 · Compatibility** | Older SDK tolerates newer server and vice versa | Decode tests against recorded payloads from two server versions | **NEW — Phase 5** |

**Reusable asset.** [`caliber-ui/src/test/handlers.ts`](caliber/caliber-ui/src/test/handlers.ts)
(3,071 lines of MSW mocks) is an independent enumeration of expected response shapes per
endpoint. It is a ready-made source of fixtures for L5 when modelling the 183 uncovered
operations — worth mining rather than re-deriving shapes from route source.

**Non-negotiables carried from the current suite:**

- Validation stays **deterministic and offline** — no credentials, no network, no model
  calls (repository `CLAUDE.md` requirement).
- Published examples are *executed*, never merely linted.
- Every new resource method lands with an L5 unit test, an L2 contract test, and a docs
  entry — the bar the existing modules met.

---

## 8 · Acceptance criteria

### 8.1 Coverage

- [ ] `test_sdk_api_coverage.py` passes with `coverage_allowlist.toml` containing **only** §2.8 exclusions.
- [ ] Addressable coverage = **380/380 (100 %)**.
- [ ] Every `ga`/`beta` operation has a typed method with a typed response model.
- [ ] No route module sits at 0 % typed coverage.

### 8.2 Parity

- [ ] `AsyncCaliberClient` exposes every resource `CaliberClient` does.
- [ ] Every registry operation is bound in both clients, or explicitly marked sync-only with a recorded reason.
- [ ] The L4 identity assertions still pass unchanged.

### 8.3 Enforcement

- [ ] Adding a server route without SDK coverage **fails CI**.
- [ ] `SDK_SURFACE_MAP` is derived from the registry, not hand-maintained.
- [ ] `_sdk_surface_row()` fails closed for unmapped `ga`/`beta` tags.
- [ ] The published REST inventory shows per-operation counts; no tag can display a claim above its measured value.
- [ ] `make sdk-coverage` reproduces the CI result locally.
- [ ] The SPA API client references no route the server does not serve.

### 8.4 Interface quality

- [ ] Every mutating operation accepts a typed request model.
- [ ] `**options: Any` survives only where the server's schema is genuinely open-ended, each occurrence commented.
- [ ] `idempotency_key` is a transport option on every idempotent route.
- [ ] Upload and download are typed resource methods returning bytes/streams, never URL strings.
- [ ] SSE events decode to typed frames.
- [ ] No exported symbol is dead (`Page` used or removed).
- [ ] Naming corrections shipped with deprecation aliases.

### 8.5 Documentation and versioning

- [ ] Generated SDK reference covers 100 % of public modules and symbols (already gated).
- [ ] Versioning and deprecation policy published.
- [ ] Server/SDK compatibility matrix published and tested.
- [ ] CLI README command table matches the real parser.
- [ ] No documentation example uses `raw` where a typed method exists.
- [ ] `caliber-sdk` is `1.0.0`, `Development Status :: 5 - Production/Stable`.

### 8.6 Backward compatibility

- [ ] No existing public method signature broke without a deprecation cycle.
- [ ] Every cookbook example still executes unchanged.
- [ ] Tolerant decoding (S4) verified against recorded payloads from two server versions.

---

## 9 · Risks and limitations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| **Phase 3 is large** (~183 ops) | Long branch, review fatigue | Wave-sized PRs (3a–3e), each independently green with the allowlist shrinking measurably |
| **Phase 2 touches every resource** | Regression risk in the part that currently works well | Migration must not change a single public name or docstring; the existing suite is the gate, run unchanged |
| **AD-5 needs a server change** | Outside `sdk/`; own migration and OpenAPI impact | Isolated to Phase 4; `paginate()` already forward-compatible, so the SDK ships without it |
| **Typed models for `assistant` (29 ops)** | Aria session/draft/plan shapes are the least stable in the product | Land 3c after 3b; mark `beta`; lean on tolerant decoding (S4) |
| **Overstated-coverage fix is user-visible** | Published docs will show *lower* coverage than today | Correct and intended — ship 1.3/1.4 with a changelog note explaining the number moved because the measurement got honest, not because the SDK regressed |
| **Deprecation aliases add surface** | Two names for one thing during the window | Time-boxed to one minor cycle; removal release named when the alias is introduced |
| **`/verification-queue` decision is not the SDK's to make** | 1.6 may block on product input | Default to deleting the dead client methods; implementing routes is a separate product decision |

**Stated limits.** This plan does not expand the **plugin SDK** beyond noting its
independence — `caliber-plugin-sdk` is pre-alpha, optimizer-only, correctly decoupled, and
turning it into a general extension mechanism is a separate product decision. It does not
propose a TypeScript client; the SPA's `src/api/` client is internal and out of scope. And
it does not claim the SDK will reach *UI feature parity* — the UI composes endpoints into
screens with client-side state (graph editors, multi-select, drag-and-drop) that an SDK
should not and will not reproduce. **The SDK's completeness target is the API surface, not
the interaction surface.**

---

## 10 · Traceability

| Repository principle ([`ARCHITECTURE.md`](ARCHITECTURE.md#design-principles)) | How this plan discharges it |
| --- | --- |
| 3 · Auditability & Observability | The L1 gate makes SDK completeness a measured, published fact rather than a claim |
| 4 · Evaluation by Design | The gate ships in Phase 1, before the work it measures |
| 5 · Open & Extensible | 100 % addressable coverage means every governed operation is automatable |
| 6 · Modular & Composable | AD-1's registry separates *what the API offers* from *how callers navigate it* |
| 9 · Developer & User Friendly | S1 preserved: typed models are additive, the simple path stays simple |
| 10 · Future Ready | Registry + fail-closed gate means a new asset family cannot ship without SDK coverage |

---

## Appendix A · Second-pass validation

The plan was reviewed a second time against the repository and the proposed target state.
Findings that changed the plan:

| # | Second-pass finding | Resolution |
| --- | --- | --- |
| A1 | First pass counted 390 operations; ARCHITECTURE.md documents 386 across 45 modules | Reconciled — the 4-operation difference is the SPA shell. Both figures now stated, and the match validates the extraction method |
| A2 | First pass claimed the server has "no cursor" pagination | **Corrected.** `GET /workflows/{id}/runs` does return `next_cursor`. The real defect is *inconsistency* plus `paginate()` ignoring it (§2.7) |
| A3 | First pass had no UI→API→SDK mapping, which the objective explicitly required | Added §2.1–2.2, including the finding that the UI client both overshoots and undershoots the server |
| A4 | Cross-cutting mechanics (multipart, download, SSE frames, bulk, polling) were scattered across phases without being named as a class of gap | Promoted to §2.6 and given a dedicated decision, AD-7 |
| A5 | The dead `/verification-queue*` UI client methods were not accounted for anywhere | Added as a §2.7 defect, a Phase 1.6 deliverable, and an L8 test level |
| A6 | `caliber-ui/src/test/handlers.ts` (3,071 lines of MSW mocks) is an untapped fixture source | Named in §7 as a reusable asset for L5 |
| A7 | Phase 3 wave totals were wrong (3b stated 94, actually 89; 3e stated 22, actually 27) and the `audit` and `health` modules appeared in no wave at all | Recomputed programmatically; both modules added to §2.3 and wave 3e; waves now sum to exactly 183 with no module unassigned |
| A8 | No explicit statement that SDK completeness ≠ UI parity | Added to §9's stated limits — the target is the API surface, not the interaction surface |

**Consistency checks performed:** wave totals (21+94+33+13+22 = 183) equal the addressable
gap; every module in §2.3 appears in exactly one Phase 3 wave; every gap in §2 maps to at
least one acceptance criterion in §8; every architectural decision AD-1…AD-7 is referenced
by at least one phase deliverable.
