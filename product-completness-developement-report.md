# CALIBER Product Completeness — Development Report

> **Date:** 2026-08-01
>
> **Reviewed document:** `product-completness-review-report.md` (audit baseline `f69d945a0`)
>
> **Implementation baseline:** `a75f743ae`; work landed on `fix/product-completeness-wave-0`
>
> **Scope:** validate all 22 findings as hypotheses, implement those with a correct bounded
> fix, triage the rest with rationale.

## 1. Executive summary

All 22 findings in the review were re-derived from source rather than accepted. **19 were
confirmed, 6 partly confirmed, 1 refuted**, and several were materially different from
their description — including the report's own headline claim.

Seven fixes landed, each with a regression test; four of those tests were proven red
against the pre-fix code rather than merely asserted. The remaining findings are real but
genuinely XL or blocked on a product decision, and are triaged in §7 with what each would
actually take.

Three corrections to the review are worth stating up front, because they change decisions:

**The release gate was not open in CI.** The report's controlling result — "the release gate
is open… a release cannot be certified from the present checkout" — rests on a red backend
suite it attributed to "event backend fixture state leaking between tests". The real
mechanism is a single import: `litellm/__init__.py` calls `_dotenv.load_dotenv()` at import
time unless `LITELLM_MODE != DEV`, and `tests/test_dspy_optimizer.py` imports
`dspy → litellm` at module scope, so `find_dotenv()` injected the developer's `caliber/.env`
during *collection*. That file is gitignored, so **GitHub CI never had a file to find**. This
was a developer-machine gate, not a CI gate. It is now fixed, and the fix makes a local run
match CI.

**F-01 is worse than reported, and its bounded fix is much smaller than implied.** The audit
described a data-boundary crossing and proposed a data-plane redesign. The write path is
also an *arbitrary host-write primitive*: the directory is an arbitrary absolute path with
`mkdir -p` and the artifact filename is author-controllable. But the audit missed that
Preview already refuses exactly these node types, so the predicate a promotion gate needs
already existed. A ~40-line gate at the single rotation chokepoint closes the production
exposure; the data-plane redesign remains deferred.

**F-17 was right and my first reading was wrong.** `egress.py` documents that the shipped
sender sets `follow_redirects=False`, which initially looked like it refuted the finding.
That docstring describes the *workflow node* path. The operator-webhook dispatcher is a
different sender using `urllib.request.urlopen`, which follows redirects by default. Two
senders, two policies, one documented.

## 2. Validation results

| ID | Verdict | Severity (re-assessed) | Disposition |
| --- | --- | --- | --- |
| F-01 | Confirmed — understated | Critical | **Implemented** (promotion gate); data plane deferred |
| F-02 | Confirmed | Critical | **Implemented** (2 of 4 slices); rest deferred |
| F-03 | Confirmed | Critical | **Implemented** (retry scope); coverage extension deferred |
| F-04 | Confirmed | High | Deferred — memory half bounded, trace half needs a product decision |
| F-05 | Confirmed | Medium | Deferred — one trap makes it unsafe to rush |
| F-06 | Partly | Medium | **Rejected as a defect** — product-semantics decision, needs sign-off |
| F-07 | Confirmed — worse | High | **Implemented** |
| F-08 | Confirmed | High | **Implemented** |
| F-09 | Confirmed | High | Deferred — mechanical, but needs test infrastructure that does not exist |
| F-10 | Confirmed | High | Deferred — 4 separable pieces, framing fix is not bounded |
| F-11 | Confirmed | High | Deferred — L, ~30 files, 224 sync sessions in async handlers |
| F-12 | Confirmed | High | Deferred — needs a migration and a Postgres-marked test |
| F-13 | Partly | — | **Rejected** — umbrella over F-13a–d, headline wrong in both directions |
| F-13a | Partly | Low | **Rejected** — audit missed `tool_sandbox/_runner.py`'s builtins allowlist |
| F-13b | Confirmed | Medium | Deferred — policy half S, transport half needs an SDK answer |
| F-13c | **Refuted** | Not a defect | **Rejected** — disclosed fail-closed boundary, not a silent hole |
| F-13d | Confirmed | Medium | Deferred — edits the same function as F-01a; needs rollout policy |
| F-14 | Confirmed | High | **Implemented** |
| F-15 | Partly — wrong cause | Medium | **Implemented** (see §1) |
| F-16 | Confirmed | Medium | **Rejected as a defect** — a scope statement; its own validation said no fix exists |
| F-17 | Confirmed | Medium | Deferred — S, first item to pull forward |
| F-18 | Confirmed | High | Deferred — S, second item to pull forward |
| F-19 | Confirmed | Medium | Deferred — needs deploy-side artifacts in the same commit |
| F-20 | Partly | Low | Deferred — half refuted; template parity test is S |
| F-21 | Partly | Medium | Deferred — 2 of 5 anchors wrong; ticket needs rewriting first |
| F-22 | Confirmed | Low | Deferred — a small feature, not a repair |

### Rejections, with rationale

**F-13c — MCP OAuth absent.** Factually true, but it is neither silent nor a hole:
`mcp_policy.execution_readiness` appends an explicit blocker naming the limitation and
documenting the token-auth workaround. It is a disclosed fail-closed capability boundary —
the opposite of the decorative-control pattern the rest of the audit correctly hunts.

**F-13a — tool subprocesses retain ambient authority.** Substantially refuted for the threat
model invoked. The audit missed `tool_sandbox/_runner.py`, which replaces `__builtins__`
with a 30-entry allowlist deliberately omitting `__import__`, `open`, `getattr`, `eval`,
`exec`, `compile`, `type`, and `object`. Probes confirmed: `open` → `NameError`, `import os`
→ `ImportError`, dunder access → `ValueError`.

**F-13 — umbrella.** Redundant with F-13a–d, and its headline ("tool execution is not
suitable for untrusted authors without hardened external isolation") is wrong in both
directions. Carrying it forward as a ticket would double-count.

**F-16 — no SDK/CLI/HA/production deployment.** Every clause checks out, but it is a scope
statement about product completeness rather than a code defect, and its own validation
concluded no bounded fix exists. Tracked in §8 as roadmap, not as a defect.

**F-06 — admin project-scope semantics.** Not a bug. `db/scoping.py` returns the bare
statement for an admin when `only is None`. Whether that is correct depends on what the
workspace selector is *supposed* to mean for an admin, which is a product decision requiring
sign-off before code moves.

## 3. What was implemented

### F-15 — release gate (`caliber/tests/conftest.py`)

Two independent guards. `LITELLM_MODE=PRODUCTION` is set before any import, stopping
`litellm` from injecting `caliber/.env` during collection. Separately, an ambient
`CALIBER_WORKFLOW_RUN_EVENT_BACKEND` is popped for non-integration runs, because a developer
who exports it reproduces the same failure and the blast radius is wider than the two files
the report named — `test_routes_capabilities` asserts the default backend is `in_process`.

Reproduced before: `1 failed, 61 passed, 15 errors`. After: `77 passed`.

### F-01 — host-filesystem nodes at promotion (`promoter.py`, `deployment_environments.py`, `config.py`)

`_host_path_blockers` at `require_alias_target_ready` — the single chokepoint every alias
transition passes through, rollback included. Refuses a `file_input` without a managed
`file_ref`, a `folder_input`, or an `output_folder` when the alias is not in an allowed
environment class. Reuses the predicate Preview already applies, including its exemption for
a pinned managed file, so preview and promotion finally agree.

Widenable via `CALIBER_WORKFLOW_HOST_PATH_NODES_ALLOWED_ENVIRONMENT_CLASSES`, which falls
back to development-only on an unrecognised value so a typo cannot open production. This
makes `docs/06-workflows/architecture.md`'s existing "development option" claim enforced
rather than aspirational.

### F-08 — CSRF fail-closed (`server.py`, `routes/settings.py`)

`build_token_manager` correctly declines to sign with an empty key, but the process then
started with `CSRFMiddleware` short-circuiting, so every state-changing request passed
untokened while config, the Settings page, and the operator all reported protection on. Now
raises at app build, naming the unset source and the flag to set instead.

### F-07 — rate-limiter identity (`rate_limit.py`)

Buckets were keyed on `X-CALIBER-User`, which the shipped `session` mode does not use for
authentication. Rotating it per request minted a fresh full bucket while executing with real
signed-in authority — the limit was absent, not weak. Now delegates to
`caliber.auth.current_user`, exactly as `CSRFMiddleware` already does for the identical trap.
Anonymous callers still share one bucket, which is the existing deliberate choice.

### F-03 — duplicate effects on retry (`effect_ledger.py`, `runtime.py`)

The occurrence counter is per-instance and one instance serves a whole run, so a node retry
re-derived occurrence 1 for an effect the previous attempt claimed as 0, produced a different
idempotency key, and re-sent a call the ledger had already recorded. The reachable path is a
slow-but-*successful* webhook: the deadline is checked after the call returns, turning a
success into a retry. `SqlEffectLedger.attempt_scope()` snapshots and restores the counter
per attempt — which is what the module docstring's "it resets per attempt" already promised.

### F-02 — timeouts that return control (`runtime.py`, both assistant engines, `config.py`)

Two of the four slices:

1. `_resilient_callable` entered its pool with `with`, whose `__exit__` calls
   `shutdown(wait=True)` and joins the thread the timeout just abandoned. A tool declaring a
   0.2 s timeout and sleeping 3 s returned control at 3 s — the deadline existed in the error
   message and nowhere in the wall clock. Now uses the non-blocking pattern already present
   in `_run_external_app_entrypoint`.
2. All five provider client constructions (the validation found five; the report listed four,
   missing `runtime.py:1360`) now carry `timeout=provider_request_timeout()`. Without it the
   SDK default governs — `openai` ships a 600 s read timeout with 2 automatic retries, so one
   call could hold a worker for half an hour past any CALIBER-side deadline.

### F-14 — assistant queue destroying messages (`CaliberAssistantPanel.tsx`)

The dispatcher issued a hard `DELETE` of the queue row *before* sending, swallowed the
delete's failure, and fired the send from a `.finally()` so it ran on both branches. A send
that then failed left the user's typed message deleted server-side, absent from the panel,
and unreported. Now sends first and removes second, keeps a dispatched-id set so the head is
not re-dispatched while in flight, and surfaces both failure modes.

## 4. Architectural and design changes

Three additions, all following patterns already in the tree:

- **`deployment_environments.host_path_nodes_allowed_classes` / `allows_host_path_nodes`** —
  mirrors the existing `isolation_required_classes` / `requires_external_isolation` pair.
  Expressed as an allowlist rather than a refusal list because the safe set is the small one,
  so an unrecognised value fails toward development-only.
- **`SqlEffectLedger.attempt_scope()`** — a new boundary concept on the ledger: occurrence
  numbering is now explicitly scoped to an attempt, not merely to a process.
- **`config.provider_request_timeout()`** — a module-level resolver beside its field, sharing
  one default constant. The executors build clients deep in a path that never receives a
  `CaliberConfig`, and threading one through five constructors purely to carry a float was
  the worse trade.

No breaking changes. Every new setting defaults to current behaviour except the F-01 gate,
which is deliberately restrictive-by-default and documented with an escape hatch, and F-08,
which converts a silent downgrade into a startup refusal for a flag that defaults off.

## 5. Tests

11 backend tests and 1 frontend test added. **Four proven red against pre-fix code**, not
merely asserted — reverted the fix, observed the failure, restored, observed the pass:

| Test | Pre-fix | Post-fix |
| --- | --- | --- |
| `test_rotating_the_identity_header_cannot_mint_fresh_budgets` | FAILED | passed |
| `test_a_node_retry_replays_instead_of_re_firing` | FAILED | passed |
| `test_resilient_callable_timeout_returns_control_without_joining_the_orphan` | FAILED (3.11 s) | passed (0.38 s) |
| `aria-queue › keeps a queued message when the send fails` | FAILED | passed |
| F-15 reproduction (`dspy` + `csrf` + `rate_limit`) | 1 failed, 15 errors | 77 passed |

Tests deliberately pinning the *opposite* error, so the fixes cannot over-correct: a loop's
repeated identical effects must all still fire; a managed `file_ref` must still promote; a
dev-class alias must still accept host-path nodes; CSRF disabled without a secret must still
boot.

Suites executed: 688 config/event/auth/csrf/rate · 344 promotion/deploy/alias · 138 workflow
runtime · 115 effect/webhook/runtime-tool · 63 settings + deployment-policy · 260 frontend
assistant (14 files) · 159 frontend workflow. `ruff check src tests` clean; `mypy src` clean
on 309 files; `npm run typecheck` clean.

**Full backend suite**, run as CI does (`pytest -n auto --dist loadgroup`):
**5,822 passed, 9 skipped, 0 failed** in 262 s. The 9 skips are opt-in integration
and Postgres-marked tests, unchanged from baseline.

The first full run after the provider-timeout change was **3 failed, 5,819 passed** —
a regression this work introduced. All three were in `tests/test_anthropic_engine.py`,
whose `FakeAnthropic.__init__(self, *, api_key: str)` did not accept `timeout`. The
double modelled a narrower constructor than the SDK it stands in for, which is why it
could pass while the client was unbounded. Fixed by widening all three fakes and, in the
recording one, asserting `calls["timeout"] == provider_request_timeout()` — so the test
now proves the ceiling is applied rather than tolerating the argument. The OpenAI engine
tests passed throughout because their fake already accepted flexible kwargs: the same
change, two doubles, one of which could catch it.

## 6. Reliability, security, and usability effect

**Security.** Three real boundaries closed: a plain operator can no longer put arbitrary host
reads or writes behind a production alias; an explicitly enabled CSRF control can no longer
silently be off; and the rate limiter can no longer be bypassed by rotating a header the
authenticator ignores.

**Reliability.** A retry can no longer manufacture a duplicate external effect out of a
successful call. A tool timeout now bounds the wall clock rather than only the error message.
Every LLM provider call has a ceiling, so a hung provider consumes a worker for 120 s rather
than up to 30 minutes.

**Usability.** A failed assistant send no longer destroys the user's typed message.

## 7. Remaining gaps and technical debt

Ordered by what each would actually take.

**Bounded, next up (S each):**
- **F-17** webhook redirects — a `_NoRedirect` handler and a module-level opener; ~10 lines.
- **F-18a** exported runtimes default to broad admin/approver/operator/viewer scopes; narrow
  `_default_identity` to viewer after verifying what `knowledge_build` needs.
- **F-20 (template half)** a parity test asserting frontend and backend template catalogs
  match, modelled on `test_cookbook_doc_contract.py`.

**Bounded but blocked on a decision:**
- **F-19** metrics scrape token, Redis readiness, provider connectivity — each needs a
  deploy-side artifact in the same commit.
- **F-13b/F-13d** MCP transport pinning and sandbox promotion gates — F-13d edits the same
  function as F-01a, so it must not be applied concurrently.
- **F-04 (memory half)** ~2 h; the trace half needs a product decision on trace visibility.

**Genuinely large:**
- **F-11** 224 synchronous SQLAlchemy sessions opened directly in async handlers, ~30 files.
- **F-09** event sequence allocation race — mechanical, but proving it needs concurrency test
  infrastructure the suite does not have.
- **F-10** durable event delivery — the drop-callback piece is S; the framing is not.
- **F-12** retrieval fusion and server-side graph traversal — needs a migration and a
  Postgres-marked test.
- **F-01b** the data-plane half: a confined workflow file root. Deferred because the safe
  default is a design decision, not a code change.
- **F-02b** node-level deadlines and cancellation propagation — changes failure modes rather
  than fixing a bug in place.

**Unchanged from the review:** no production topology, HA/DR, management API, SDK, CLI, or
enterprise identity. These are correctly scoped as XL and gated behind each other.

## 8. Recommended next priorities

| # | Item | Impact | Effort | Why this order |
| --- | --- | --- | --- | --- |
| 1 | F-17, F-18a, F-20 template parity | Medium–high | S each | Bounded, verified, no decisions pending |
| 2 | F-04 memory scoping | High | M | Real cross-resource visibility gap; half is bounded today |
| 3 | F-19 + fail-closed production preflight | High | M | Completes the review's Wave 0; needs deploy artifacts |
| 4 | F-09 sequencing + F-10 drop callback | High | M | Removes silent event loss |
| 5 | F-01b confined file root | High | M/L | Retires the promotion gate as the *only* defence |
| 6 | F-05 workspace identity | Medium | M | One trap; do it deliberately |
| 7 | F-11 async offload, tier 1 | Medium/high | L | Mechanical; the target pattern exists in-tree |
| 8 | C-5 / C-2B / H-1 | Very high | XL | Unchanged; these define the supported-production and enterprise gates |

## 9. Overall assessment

The review's central judgement stands: **feature-rich Alpha, credible for a controlled
technical pilot, not ready for supported production or untrusted authoring.** Nothing in this
work changes that verdict, and it was not intended to — six of the eight blocking items are
XL by construction.

What did change is the quality of the evidence. The release gate is closed and a local run
now matches CI: **5,822 passed, 0 failed**, where the same checkout previously produced
15 errors before a single test ran. Four defects that were reachable by an ordinary operator — arbitrary host
writes behind a production alias, a security control that silently disabled itself, a rate
limit that could be bypassed with a header, and duplicate external effects from a successful
call — are fixed and pinned by tests that fail without the fix.

Two cautions carry forward. First, the audit's severity calls were good but its *mechanisms*
were wrong often enough that acting on the ticket text without re-deriving the cause would
have produced the wrong fix at least three times (F-15, F-01, F-13a). Second, the promotion
gate is now the only thing standing between an operator and a host-filesystem write; the
data-plane confinement in F-01b should not be left indefinitely, because a single-layer
defence at a policy boundary is exactly the pattern this review exists to find.
