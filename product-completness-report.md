# CALIBER Product Completeness — Development Report

> **Date:** 2026-08-01. **Updated 2026-08-06** ([§10](#10-update-2026-08-06)), **2026-08-07**
> ([§11](#11-update-2026-08-07)), and **2026-08-08** ([§12](#12-update-2026-08-08)) — see those
> sections for what has changed since, and which statements below are now superseded. The
> newest update is the one to read first: it discharges the local-only evidence caveat both
> prior updates closed on, and records that the Pages fix §10.2 calls resolved has never
> reached the site.
>
> **Reviewed document:** `product-completness-review-report.md` (audit baseline `f69d945a0`).
> That file was deleted from the tree in `6ff4b7927`; this report is the surviving record of
> its findings.
>
> **Implementation baseline:** `a75f743ae`; work landed on `fix/product-completeness-wave-0`
>
> **Scope:** validate all 22 findings as hypotheses, implement those with a correct bounded
> fix, triage the rest with rationale.
>
> **How to read this document.** §1–§9 are the record as written at `a75f743ae` and are
> deliberately **not** rewritten — the value of a dated report is that it says what was
> known when. Where a later change has falsified a statement, the statement is left standing
> with an inline superseded marker pointing at §10. Test totals here are historical; use the
> latest `main` CI run for current release evidence.

## 1. Executive summary

All 22 findings in the review were re-derived from source rather than accepted. **19 were
confirmed, 6 partly confirmed, 1 refuted**, and several were materially different from
their description — including the report's own headline claim.

Fixes landed across three waves, each with a regression test; twelve of those tests were
proven red against the pre-fix code rather than merely asserted. The remaining findings
are real but genuinely XL or blocked on a product decision, and are triaged in §7 with
what each would actually take.

The third wave was not in the review at all — it is what landing the work exposed in the
verification machinery, including a packaging defect that had been shipping wheels without
a bundled SPA behind a correct check that had never once executed (§3c).

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
| F-01 | Confirmed — understated | Critical | **Implemented** (promotion gate + confined file root) |
| F-02 | Confirmed | Critical | **Implemented** (3 of 4 slices); cancellation deferred |
| F-03 | Confirmed | Critical | **Implemented** (retry scope); coverage extension deferred |
| F-04 | Confirmed | High | **Implemented** (memory half); trace half deferred |
| F-05 | Confirmed | Medium | **Implemented** (centralized half); query keys deferred |
| F-06 | Partly | Medium | **Rejected as a defect** — product-semantics decision, needs sign-off |
| F-07 | Confirmed — worse | High | **Implemented** |
| F-08 | Confirmed | High | **Implemented** |
| F-09 | Confirmed | High | **Implemented** |
| F-10 | Confirmed | High | **Implemented** (drop-to-dead-letter); broker replay deferred |
| F-11 | Confirmed | High | **Partly implemented** — heaviest handler converted + ratchet on 224 |
| F-12 | Confirmed | High | **Implemented** (graph recall); lexical fusion deferred |
| F-13 | Partly | — | **Rejected** — umbrella over F-13a–d, headline wrong in both directions |
| F-13a | Partly | Low | **Rejected** — audit missed `tool_sandbox/_runner.py`'s builtins allowlist |
| F-13b | Confirmed | Medium | **Implemented** (egress resolution) |
| F-13c | **Refuted** | Not a defect | **Rejected** — disclosed fail-closed boundary, not a silent hole |
| F-13d | Confirmed | Medium | **Implemented** (opt-in gate) |
| F-14 | Confirmed | High | **Implemented** |
| F-15 | Partly — wrong cause | Medium | **Implemented** (see §1) |
| F-16 | Confirmed | Medium | **Rejected as a defect** — a scope statement; its own validation said no fix exists |
| F-17 | Confirmed | Medium | **Implemented** |
| F-18 | Confirmed | High | **Implemented** (F-18a scope narrowing); export fidelity deferred |
| F-19 | Confirmed | Medium | **Implemented** (metrics token, Redis probe) |
| F-20 | Partly | Low | **Implemented** (parity test); shared layout deferred |
| F-21 | **Largely refuted** | Low | 2 of 5 anchors do not exist; one real claim fixed |
| F-22 | Confirmed | Low | **Implemented** |

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

### F-17 — webhook redirects (`events/webhooks.py`)

`_post` called `urllib.request.urlopen`, which installs the default opener and
follows redirects — turning an operator-configured URL into one the *receiver*
chooses. Measured against a local server pair before the fix: **a 302 was followed
to the second host carrying `X-Caliber-Signature` and `X-Caliber-Timestamp`
intact.** urllib rewrites POST to GET on a 302 so the body was dropped, but a valid
HMAC for that payload had already reached an unauthorized host. 307 happened to
raise rather than follow, so only the 301/302/303 path was exposed.

Now delivers through a module-level opener built with a `_NoRedirect` handler.
A refused redirect surfaces as an `HTTPError`, which `_post` already classifies:
3xx is neither 2xx nor a 4xx client error, so it retries and finally dead-letters
with the status visible — the right reading for a destination pointing elsewhere.

Fixed a real leak found while testing: the `HTTPError` branch never closed the
response, so the socket was reclaimed by the GC later and surfaced as a
`ResourceWarning` in whichever test was running.

**The test seam moved.** 22 sites patched `caliber.events.webhooks.urllib.request.urlopen`;
`_OPENER.open` bypassed that patch, so the dispatcher briefly made real network
calls — 16 failures and a 5.6 s → 251 s runtime. The patch target is now the
module's own opener, which is better encapsulation regardless.

### F-18a — exported runtimes started over-privileged (`workflows/export_runtime.py`)

`_default_identity` granted all four scopes. Two were never exercised and both
mattered: **`admin`**, because the visibility layer treats it as a bypass —
`db/scoping.py` drops the project predicate entirely, so an exported workflow read
across *every* project regardless of the `active_project_id` it was handed; and
**`approver`**, standing authority to satisfy a human-approval gate. Narrowed to
`{operator, viewer}`. `operator` is retained because exported runtimes legitimately
write via `knowledge_build`. Verified first that neither knowledge path gates on a
scope — both filter on visibility — so this bounds reach rather than unblocking
calls. Callers can still pass an explicit `identity`.

### F-20 — silent template drift (`tests/test_workflow_template_parity.py`)

`Workflows.tsx` ships `FALLBACK_TEMPLATES`, rendered whenever the catalog query
fails. Because the fallback is silent, a template added, removed, renamed, or
reordered server-side yields a UI that looks correct and offers a different set.
The two lists agree today; a parity test asserting kinds, labels, and order keeps
them agreeing and names which side moved. Half of F-20's original claim (shared
layout) was refuted or deferred; this is the half that survived.

## 3b. Second wave — the deferred findings

After the first pass the remaining validated findings were implemented as well.
Eight commits on `fix/product-completeness-wave-1`.

### F-01b — confined workflow file root

The promotion gate from wave 1 was a single layer: anything reachable *before*
promotion resolves a supplied path with the server's authority.
`CALIBER_WORKFLOW_FILE_ROOT` confines `file_input`, `folder_input`, and
`output_folder`. Resolution runs before any existence check and covers symlinks —
`<root>/link → /etc` passes a string comparison and fails this one. Default
unconfined so existing development workflows keep working.

**Bug found in my own wave-1 commit:** the F-01a escape hatch
`CALIBER_WORKFLOW_HOST_PATH_NODES_ALLOWED_ENVIRONMENT_CLASSES` had no entry in
the env-var table, so setting it did nothing while the config object reported
the default. Fixed, plus two invariant tests — every field reachable from the
environment, no mapping pointing at a dead field. A 173-field audit found it was
the only gap.

### F-09 — run-event sequence allocation

Three writers each did an unlocked `SELECT max(sequence)` then `INSERT max+1`.
The unique constraint makes the interleaving a *lost* event, not a duplicate.
One shared allocator now serves all three: `FOR UPDATE` on the parent run (real
on PostgreSQL, omitted on SQLite) plus bounded retry, each attempt in a savepoint
so a retry cannot discard the caller's other work.

The threaded test this wanted failed on SQLite's file-level write lock before
reaching any CALIBER code; WAL, `busy_timeout`, and SQLAlchemy's pysqlite
`SAVEPOINT` workaround did not clear it. Rather than tune a test until it passed,
the race is driven deterministically through a named `_current_max` seam.

### F-04 — memory scope authorization

`user_id` must be the caller's own unless admin. `agent_id`/`run_id` are checked
only when the value names a governed resource. My first attempt required those
rows to exist and broke 7 tests — they are free-form partition labels. The
residual limitation is documented and pinned by a test: a guessed free-form label
still reads that partition, because a partition with no owner has nothing to
enforce.

### F-19, F-10 — operational surfaces

Opt-in `/metrics` bearer token (closing it by default would break every existing
scrape config). Redis probed for readiness like NATS — it was selectable as an
event backend and never checked. Bus-queue overflow now writes a dead letter
instead of a log line; that drop happens upstream of every durability mechanism
the dispatcher has.

### F-13b, F-13d — MCP and tool isolation

The MCP host allowlist matched a hostname *string* and never resolved it, while
workflow HTTP resolves and checks the address. Both now use the same
`EgressPolicy`. The symmetric tool-isolation gate is **opt-in**, and that was a
correction: modelled on the MCP rule it defaulted on and broke 8 existing prod
promotions. MCP servers are a deliberate integration; registered tools are
ubiquitous, so the same default is a gate in one case and a forced migration in
the other.

### F-22, F-05, F-21, F-12, F-02b, F-11

`GET /caliber/aria/capabilities` discloses the real registry — seven built-ins,
with `TIER_GATED` currently empty. `useApi` now listens for
`WORKSPACE_CHANGED_EVENT`, which had **zero listeners app-wide**, so a switch
invalidated half the application. F-21 was largely refuted: two of five anchors
do not exist and the cookbooks are accurate; one real overclaim ("production-ready")
removed from the login hero. Graph retrieval can now surface chunks outside the
ANN pool. The node deadline bounds execution rather than reporting it afterwards —
as a *backstop* with grace, after it pre-empted more specific per-node timeout
messages. F-11 converted the heaviest handler and added a ratchet over the
remaining 224 rather than a blind sweep.

## 3c. Third wave — the toolchain, and what CI found

Landing the work exposed a set of defects in the verification machinery itself.
These were not in the review's 22 findings, and one of them is the most
consequential single thing in this report.

### The development interpreter was outside the supported range

Every "green" result reported during waves 1 and 2 came from Python **3.14**,
while the package declares `>=3.10,<3.13` and CI runs 3.11. Root cause was
`Makefile`'s venv rule invoking bare `python3`, which takes whatever the machine
ships — so this was never one workstation's mistake.

`CALIBER_PYTHON` now prefers 3.12 and `scripts/check_python_version.py` refuses
anything outside the range *before* the venv is created. pip's `requires-python`
does eventually refuse the editable install, but only after building a venv and
resolving dependencies.

The dev venv was rebuilt on 3.12 and the suite re-verified there. Both
interpreters produce 5,868 passed, so the risk did not materialize — but it was
unmeasured until the last moment before publishing.

### Two blockers the rebuild uncovered

**numpy broke `mypy` on any clean install.** numpy is an unpinned transitive
dependency whose bundled stubs moved to PEP 695 `type` statements in 2.5. mypy
parses third-party stubs with the target grammar, so `python_version = "3.10"`
failed with a syntax error *inside numpy*. Per-module overrides cannot suppress
it — `follow_imports` and `ignore_errors` both apply after parsing; both were
tried. Resolved by targeting mypy at 3.12, with the tradeoff recorded next to the
setting: static 3.11+ detection is lost, but the 3.10 CI leg installs and imports
the package, and an audit of `src/` found no 3.11+ constructs at all.

**`make install-extended` did not reproduce CI.** `tests/test_memory_service.py`
imports mem0 at module scope and CI installs the `memory` extra — its own env
comment says so — but the Makefile did not. A contributor following the
documented profile got a red suite for a missing dependency rather than a real
failure.

### Three CI failures after the first push

| Failure | Cause | Whose |
| --- | --- | --- |
| `ruff format --check` | Ran `ruff check` throughout and read it as covering formatting; they are separate commands and CI runs both | Mine |
| Docs generation gate | Committing the report deletion left dangling links in `ARCHITECTURE.md` and `README.md`; the generator classifies a link by whether its target exists, so the deletion silently changed generated output | Mine |
| `bucket-select.test.tsx` | Flake — passed on the next run with no frontend change | Neither |

> **Superseded — see [§10.4](#104-two-flakes-root-caused-not-re-run).** "Flake, whose: neither"
> was the wrong disposition. It is a real race in the test — the upload control is gated on a
> `/me` response the test never waits for — and it is now fixed. Re-running a red test until it
> is green is the same error this report criticises elsewhere: it treats absence of evidence as
> evidence of correctness.

### The wheel has been shipping without a SPA

`[tool.hatch.build.targets.wheel]` carried a comment stating it includes the
built SPA and **no `artifacts` key to do it**. The sdist target had the key; the
wheel target had only the prose. Hatchling honours `.gitignore` and
`src/caliber/ui` is gitignored, so the bundle was silently dropped.

The check that asserts `caliber/ui/index.html` is present in the wheel is
correct and has existed all along. It runs last, gated behind six other jobs,
and had been **skipped on all twelve recent CI runs**. It executed for the first
time only because this branch made every prerequisite pass. A correct gate
behind a gate that never opened.

This is the third instance of one pattern in this work, and the pattern is the
review's own subject:

1. CSRF **logged a warning** where it should have refused to start (F-08).
2. A config escape hatch was **documented with no env-var mapping**, so setting
   it did nothing (introduced by this work, in wave 1).
3. Packaging **described a behaviour it did not implement**.

Each is a control whose description outran its implementation, and in each case
the surrounding text asserted the control was working.

### Verification after the third wave

**GitHub CI is fully green — all 11 jobs**, including the wheel build, for the
first time in recent history. Verified on the remote, not inferred from a local
run.

> **Superseded — see [§10.3](#103-verification-status).** The suite is now **12 jobs**;
> `Cookbook UI-only browser journeys` was added with the cookbook platform.

Separately, **GitHub Pages fails and has done since before this work**
(`635f001f5`, `c74e2eb2e`, and every run since). It is
`Create Pages site failed: Resource not accessible by integration` — Pages is
not enabled in repository settings. Not a code defect; requires an owner action.

> **Superseded — see [§10.2](#102-the-pages-failure-was-two-defects-not-one).** The owner
> action has been taken and the site publishes. The diagnosis above was correct but
> incomplete: enabling Pages exposed a *second* defect underneath it, which is the more
> interesting one and is the same pattern §3c is about.

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
**5,868 passed, 9 skipped, 0 failed** in 408 s — the final state after both waves.
Frontend: `npm run typecheck` exit 0; 21 tests across the changed files.
`ruff check src tests` clean; `mypy src` clean on 310 source files.

The run before the last commit was 5,866 passed / **2 failed**, both in
`test_events_bus_edge_cases.py`. The F-10 change widened the bus's `_subscribers`
entries from `(queue, loop)` to `(queue, loop, on_drop)`, and those two tests
construct the tuples directly to simulate a closed loop and a full queue. The
per-area slices missed it because none of them reach into `_subscribers`. Fixed
by updating the tests to the current shape rather than making `publish` accept
both, since silently tolerating a stale shape would hide the change from the next
reader.

That was the second time in this work a change passed a focused slice and only
the full suite disagreed — the first was a provider `timeout` kwarg breaking an
incomplete test double. Both had the same shape: widening a structure that a test
reaches into directly.

One observed flake, not fixed and not caused by this work. An earlier run of the
same commit produced `1 failed` —
`test_a_dead_letter_is_persisted_when_a_session_factory_is_bound` — while the
machine was heavily loaded (570 s wall time versus 349-358 s unloaded). The test
passes in isolation (0.26 s), serially with its whole file (43/43), and across
three consecutive `-n 4` runs of that file. It waits on a background delivery
loop to persist a row, so it has a latent timing sensitivity that surfaces under
CPU contention. Recorded rather than silently re-run: an intermittent test is a
real defect in the evidence, even when the code under it is correct. The 9 skips are opt-in integration
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

> **Superseded — see [§10.5](#105-7-and-8-were-never-updated-after-wave-2).** This section and
> §8 were written after wave 1 and never revised, so they contradict §2 and §3b: F-04, F-09,
> F-10, F-12, F-13b, F-13d, F-19, F-01b and F-02b are listed here as outstanding while the
> validation table above records them as implemented. Read §10.5 for the corrected ledger. The
> *reasoning* in each bullet is still worth reading — it is why the work was sequenced as it
> was — but the dispositions are stale.

Ordered by what each would actually take.

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
| 1 | F-04 memory scoping | High | M | Real cross-resource visibility gap; half is bounded today |
| 2 | F-19 + fail-closed production preflight | High | M | Completes the review's Wave 0; needs deploy artifacts |
| 3 | F-09 sequencing + F-10 drop callback | High | M | Removes silent event loss |
| 4 | F-01b confined file root | High | M/L | Retires the promotion gate as the *only* defence |
| 5 | F-05 workspace identity | Medium | M | One trap; do it deliberately |
| 6 | F-11 async offload, tier 1 | Medium/high | L | Mechanical; the target pattern exists in-tree |
| 7 | C-5 / C-2B / H-1 | Very high | XL | Unchanged; these define the supported-production and enterprise gates |

## 9. Overall assessment

The review's central judgement stands: **feature-rich Alpha, credible for a controlled
technical pilot, not ready for supported production or untrusted authoring.** Nothing in this
work changes that verdict, and it was not intended to — six of the eight blocking items are
XL by construction.

One finding outranks the rest and did not come from the review. The packaging gate that
asserts the wheel contains a SPA is correct, has existed all along, and had been **skipped
on all twelve recent CI runs** because it sits behind six other jobs. It executed for the
first time only when this branch made every prerequisite pass — and immediately failed. A
gate that never runs is indistinguishable from a gate that passes, and the release
checklist counted it as coverage. Any assessment of production readiness should treat
"which gates have actually executed recently?" as a first-order question.

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

---

## 10. Update, 2026-08-06

> **Tree:** `5b0ad0c75` → `90ec9c1db`. **31 commits** on `main` since the `a75f743ae` baseline
> above.
>
> **Scope of this update:** correct the statements §1–§9 got wrong or that later work
> falsified, reconcile §7/§8 with §2, and record what landed since. No claim in §1–§9 has been
> rewritten in place.

### 10.1 What landed since the baseline

Four substantive changes, two of which bear directly on this report's central pattern — *a
control whose description outran its implementation*.

**Release reconciliation is now automatic (`046437e20`).** Prompt-alias releases were already
intent-first: the durable operation commits before the provider call. But settling an
`applying` or `reconcile_required` row was reachable **only by an operator hitting the releases
route**, so a crash left an observable divergence that nothing would resolve on its own. A
`ReleaseReconcilerTask` in `orchestrator/release_reconciler.py` now runs the sweep from the
server lifespan every 60 s. It still refuses to guess: it settles `applied` or `failed` only
when the observed alias matches the recorded `version_after` or `version_before`, and leaves
anything else in `reconcile_required` for a human. It never touches `prepared` — that state
proves no provider call started, so retry-or-abandon stays an explicit decision.

This is the same shape as F-08 and the wheel/SPA defect: a documented recovery whose only
execution path was a human remembering to invoke it. Worth noting the loop count moved with it —
**nine in-process loops, not eight**, which is a live discrepancy against the paper's Table 4.

**Per-family capabilities are declared as data, not prose (`046437e20`).**
`artifact_capabilities.py` replaces the hand-maintained narrative with declarations. This is
the first step toward the capability-interface design the paper's §5.1 argues the system
should have had, and toward generating the guarantee table from declarations rather than
maintaining it by hand. Directly relevant here: the entire class of defect this report
documents is a description drifting from an implementation, and the fix for that class is to
stop having two copies.

**Eight deterministic structural checks with a manifest (`046437e20`).**
`paper/benchmarks/run_structural.py` records controlled interleavings — conditional queue
ownership, operator fencing of a late calibration result, release intent ordering,
reconciliation convergence, prepared-release abandonment, resolver outage fallback, and
human-gated Aria publication — and `make repro` verifies them without rewriting the manifest.
The manifest labels itself `deterministic_structural_checks` and carries an explicit
`not_evidence_for` list: production latency, throughput, replica-scale stress, human
agreement. That self-limitation is the right instinct, and it is the direct answer to §9's
"which gates have actually executed recently?"

**Cookbook platform, paper, deck, runbook.** 16 installable cookbook examples with a
browser-only CI journey (`248fc4d8c`), the CALIBER paper (`6ff4b7927`, `e96b374f1`), a
generated 25-slide seminar deck (`55d297343`), and an operations runbook (`5b0ad0c75`).
The runbook is the operational counterpart to this report: where this document records *what
was wrong*, `docs/runbook.md` records *what an operator does about it* — including the three
recoveries the platform deliberately will not perform alone.

### 10.2 The Pages failure was two defects, not one

§3c diagnosed this as "Pages is not enabled in repository settings… requires an owner action."
That was correct and it was not the whole story.

The owner action has been taken: Pages is enabled with `build_type: workflow`, and the site
serves at `https://rrahimi-uci.github.io/caliber-suite/`. Enabling it exposed a second defect
that had been masked the entire time — **two workflows were publishing to the same URL**:

| Workflow | Published | Mechanism |
| --- | --- | --- |
| `pages.yml` | `docs-site/` | `actions/deploy-pages` |
| `ci.yml` (`allure-report`) | the Allure test report | `peaceiris/actions-gh-pages` → `gh-pages` branch |

A Pages deployment replaces the whole site, so only one could ever *be* the site. With the
build type set to `workflow`, the branch push published nothing reachable — while the job
doing it reported success on every run. Resolved in `7321c2574` by composing rather than
choosing: one deployment now carries the docs at `/` and the report at `/tests/`, and the
`gh-pages` push is retitled to what it actually is, the trend-history store that
`Load report history` reads back.

**This is the fourth instance of §3c's pattern, and the most instructive**, because the
control was not merely undescribed — it was *green*. A job that published to a branch nothing
served passed for months. §3c's own formulation covers it exactly: a gate that never runs is
indistinguishable from a gate that passes. Extend that to publication: **a publisher with no
reader is indistinguishable from a publisher that works.**

Also recorded, since it will matter operationally: the composed site is ~152 MB, of which the
report is ~126 MB across ~9k files, and its `history/history.json` is 15 MB and **grows every
run**. Well inside the 1 GB Pages limit; it is the first number to check if a deployment starts
timing out.

### 10.3 Verification status

Measured on `90ec9c1db` against a 3.12 dev venv, the interpreter §3c pinned.

| Suite | Result | Command |
| --- | --- | --- |
| Backend | **5,965 passed, 9 skipped, 0 failed** (353 s) | `pytest -n auto --dist loadgroup` |
| Frontend | **115 files, 1,570 tests, 0 failed** (160 s) | `npm test` |
| CI job count | **12**, not the 11 in §3c | `Cookbook UI-only browser journeys` added |
| Implementation counts | Re-derived and **unchanged** | `paper/scripts/gen_stats.py` |

The backend figure is the **re-run after** the fix in `90ec9c1db`. The first run was
**5,963 passed, 2 failed** — see §10.6, which is the finding this update turned up. The 9
skips are the opt-in Postgres-marked tests, unchanged from baseline. Against §3c's 5,868 the
suite has grown by 97 tests, all from work landed since.

One measurement artifact worth recording rather than hiding: the frontend suite under
`--reporter=dot` reported 114 files / 1,552 tests plus one error and took 401 s, against
115 / 1,570 clean in 160 s with the default reporter. The default-reporter number is the one
CI produces and the one quoted above. The discrepancy appears only under contention, which
puts it in the same family as §10.4.

Regenerating the implementation counts moved only the git-revision stamp — every count in the
paper is current at this tree: 322 Python modules / 126k lines, 308 pytest modules / 137k
lines, 292 TS modules / 176k lines, 487 route declarations across 47 modules, 85 Alembic
revisions, 76 ORM models, 226 Pydantic schemas, 31 workflow node types.

**A caveat on CI evidence.** GitHub Actions and Pages were in a declared `major_outage`
(critical impact, from 15:22Z on 2026-08-06) while this update was written. Jobs died at
`Set up job` with `Failed to resolve action download info`, and several were cancelled while
queued. Every number in this section is therefore a **local** measurement. §3c's standard —
"verified on the remote, not inferred from a local run" — is not met here, and should be
re-established with `gh run rerun` once Actions recovers.

### 10.4 Two flakes root-caused, not re-run

§3c dispositioned `bucket-select.test.tsx` as "Flake — passed on the next run with no frontend
change | Whose: Neither." That was wrong, and the way it was wrong is worth recording because
this report is otherwise careful about exactly this.

| Test | Real cause | Fix |
| --- | --- | --- |
| `bucket-select.test.tsx` › upload | The upload control renders only after `/me` resolves `is_admin`, but the test awaited the **object listing** and then queried the control synchronously. Two independent requests, and under CI load `/me` lost | `getBy` → `findBy` |
| `test_load_prompt_infos_for_names_bounds_slow_lookups` | Asserted `elapsed < 1.5` against a fake that slept 2.0 s behind a 0.3 s deadline — 1.2 s of slack on a shared runner. Read 5.15 s and failed **while the deadline worked correctly** | Block on an Event released in a `finally`; widen the bound to 10 s against a 30 s cap |

Both are latency or ordering assertions that encode an assumption about scheduling. Neither
was a product defect. The second also freed a worker it had been leaving blocked inside a
module-level `ThreadPoolExecutor` shared with every other test in its file.

§5 already recorded a third of this family and handled it correctly —
`test_a_dead_letter_is_persisted_when_a_session_factory_is_bound`, recorded rather than
silently re-run, with "an intermittent test is a real defect in the evidence, even when the
code under it is correct." That sentence is the standard. The `bucket-select` row did not meet
it, and this is the correction.

### 10.5 §7 and §8 were never updated after wave 2

§7 and §8 were written after wave 1 and never revised, so they contradict §2 and §3b. Nine
items appear in §7 as outstanding while the validation table records them as implemented. The
corrected ledger:

| Item | §7/§8 say | §2/§3b say | Actual |
| --- | --- | --- | --- |
| F-04 memory scoping | Bounded, blocked | Implemented (memory half) | **Implemented**; trace half still needs a product decision |
| F-19 metrics token, Redis probe | Bounded, blocked | Implemented | **Implemented** — `metrics_token_env` in `config.py`, opt-in by design |
| F-13b / F-13d | Bounded, blocked | Implemented | **Implemented**; F-13d is opt-in, deliberately |
| F-09 sequence allocation | Genuinely large | Implemented | **Implemented** — one shared allocator, `FOR UPDATE` + bounded retry |
| F-10 durable delivery | Genuinely large | Implemented (drop-to-dead-letter) | **Implemented** in part; broker replay still deferred |
| F-12 retrieval fusion | Genuinely large | Implemented (graph recall) | **Implemented** in part; lexical fusion still deferred |
| F-01b confined file root | Genuinely large | Implemented | **Implemented** — `CALIBER_WORKFLOW_FILE_ROOT`, symlink-resolving, default unconfined |
| F-02b node deadlines | Genuinely large | Implemented | **Implemented** as a backstop with grace; cancellation propagation still deferred |
| F-11 async offload | Genuinely large | Partly implemented | **Unchanged** — heaviest handler converted, ratchet over the remaining 224 |

What is genuinely still open, after reconciliation:

- **F-11**, the 224 remaining synchronous sessions in async handlers (~30 files) — the ratchet
  holds the line but does not move it.
- **F-04 trace half**, **F-10 broker replay**, **F-12 lexical fusion**, **F-02b cancellation
  propagation** — each an explicitly scoped remainder, not an oversight.
- **F-06** admin project-scope semantics — still awaiting the product sign-off §2 named.
- **F-16 / C-5 / C-2B / H-1** — no production topology, HA/DR, management API, SDK, CLI, or
  enterprise identity. Unchanged and correctly XL.

§9's central verdict is unaffected: **feature-rich Alpha, credible for a controlled technical
pilot, not ready for supported production or untrusted authoring.** The one item that would
move it — a supported production topology — has not been attempted.

### 10.6 A regression this update found, and how it got in

Running the full backend suite for §10.3 turned up **2 failures on `main`**, both in
`tests/test_docs_generation_contract.py`:

```text
FAILED test_all_manifest_markdown_is_current_and_published  — assert 21 == 20
FAILED test_all_materialized_docs_copies_match_docs_site    — assert 67 == 65
```

Those are **ratchet assertions** pinning the published module count and served file count so
an accidental addition or removal is caught. Adding `docs/runbook.md` to the manifest in
`5b0ad0c75` moved them from 20/65 to 21/67 without bumping them. Fixed in `90ec9c1db`; the
suite is green on the re-run recorded in §10.3.

How it got in is the part worth keeping, because it is a near-repeat of §3c's lesson:

1. **The check that was run.** `sync-docs.mjs` plus the Pages workflow's parity gate —
   `git diff --exit-code` over `docs-site` and the materialized copies. Both pass, correctly:
   the generated output genuinely does match its sources.
2. **The check that was not.** This pytest assertion is a *different question in a different
   suite* — not "is the output current" but "is the manifest the size we agreed on". Passing
   the first says nothing about the second.
3. **Why nothing caught it.** The Actions outage in §10.3 meant CI could not run before the
   merge landed, and the merge went ahead on local verification alone.

§3c called its pattern "a correct gate behind a gate that never opened." This one is adjacent
and worth naming separately: **a correct gate that was never asked.** Two independent checks
guarded the same change, one was run, and running one was treated as covering both. The
mitigation is not more gates — it is knowing which suite each gate lives in, which is why
§10.3 now states the command next to every number.

---

## 11. Update, 2026-08-07

> **Scope of this update:** attack the *pattern* §3c names four times and §10.2 names a
> fifth, rather than another finding from the 22. No claim in §1–§10 has been rewritten.

### 11.1 The pattern, stated once

Five instances are now on record, and they are the same defect: **one fact, two copies,
and nothing making them the same object.**

1. CSRF logged a warning where it should have refused to start (§3c).
2. A config escape hatch was documented with no env-var mapping (§3c).
3. Packaging described a bundled SPA it did not implement (§3c).
4. A publisher wrote to a branch nothing served, and was green for months (§10.2).
5. **New, found while auditing for this update:** the paper said eight in-process loops.
   `ReleaseReconcilerTask` (§10.1) made it nine on 2026-08-06, and §10.1 recorded the
   discrepancy without closing it.

The fifth is the most instructive, because a check was already watching it and passed.
`gen_stats.py` verified that `\statLoopsW{eight}` agreed with `\statLoops{8}` — *three*
hand-typed copies (two in `macros.tex`, one in the script's own `WORD_FORMS`) validated
against **each other** and never against the tree. A consistency check between copies is
not a check on the fact. Nine further hand-written copies of the same number were in
`ARCHITECTURE.md` (four), two `docs/` sources (four), and `paper/slides/README.md` (one).
The deck's own slides were the exception: they interpolate `\statLoopsW` and moved on
their own the moment the macro did, which is the difference this section is about.

### 11.2 What landed

**The loop count is derived.** `gen_stats.py` reads it off the `await <task>.start()`
calls in the lifespan via AST, emits the numeral and both prose spellings, and fails the
build if `macros.tex`'s fallback default, or `tab-loops.tex`'s row count, disagrees with
the tree. `tab-loops.tex` gained its ninth row; the nine prose copies were corrected and
the deck regenerated. `\statLoops` is out of `WORD_FORMS`, with a comment on why
agreement between spellings was never the property worth checking.

**The capability registry became a contract.** `artifact_capabilities.py` now carries
`kind` and — the load-bearing addition — `rollback`, the *mechanism* rather than a
boolean. Four families report `rollbackable: true` and each means something different:
an alias restore, a checkpoint-stack pop, a derivation from activation history, a prior
snapshot written back as a *new* version. That is the operator trap §5.1 of the paper
names, and a client given only the flag could not render it. `PlatformCapabilities` in
`workflowTypes.ts` enumerates the same field list again and is updated, so the SPA can
actually read the field.

Cross-field invariants are checked at import: a family cannot be promotable with no live
target, cannot carry a rollback flag disagreeing with its mechanism, and cannot be an
evidence or scoring asset that is nonetheless deployable. Contradictions fail the module
every route imports rather than rendering like a correct row. Eight tests pin the
invariants, five of them run against deliberately corrupted copies of the registry, so
they fail when the *check* is removed rather than only when the data is wrong.

**A route-shaped projection was written and discarded.** The obvious stronger control —
assert a family declaring `rollbackable` has a rollback route — is wrong:
`/agents/{agent_id}/rollback` is agent-*scoped* but restores a prompt alias or a skill
snapshot, so a path match contradicts a correct declaration. Making it sound needs the
`Releasable`/`Rollbackable` handler interfaces the paper records as future work. The
reasoning is recorded in the module rather than lost, because a check that asserts
something it cannot see is the failure mode the module exists to prevent.

**The paper's guarantee table is generated.** `tab-families.tex` was a fourth copy of
what the registry declares. It is now the float, caption and column spec around
`generated/families-table.tex`, which `gen_stats.py` renders from the registry — facts
from the implementation, wording keyed by the registry's own vocabulary. A family with a
mechanism the wording does not cover fails `make stats`. One cell changed in the process:
knowledge bases and tools both declare no gate, and the hand-written table gave them
different cells, asserting a distinction the code does not make.

The paper's claims were narrowed to match, not widened: §1, §5.1 and §14 now say the
obligations are *declared in a checked registry and rendered from it*, and that a
declaration is still not tied to the handler that discharges it.

**§9's own question is now answerable.** §9 said any readiness assessment should treat
"which gates have actually executed recently?" as first-order, and nothing could answer
it. `.github/scripts/gate_ledger.py` reads the run's job conclusions, writes a per-gate
ledger to the job summary, and **fails a run that would otherwise report success while a
required gate produced no evidence**. It deliberately does not fail on a skip that
follows a failure — that cascade is how `needs:` works, the run is already red, and
re-reporting it would train readers to ignore the job.

**The structure that caused the incident is gone.** `package` — the wheel/SPA gate
skipped on twelve consecutive runs — waited on six jobs. It now waits on `ui` alone, the
only one whose artifact it consumes, and even that is soft. A wheel with no SPA is broken
whether or not the tests pass. Three tests pin this: the ledger's gate names must match
job names with matrix legs expanded, it must wait for every gate it reports on, and
`package` must not re-acquire unrelated `needs`. Each was verified against a mutated
workflow — rename a job, drop a matrix leg, re-sequence the gate — and each is caught.

**The ratchets carry information.** §10.6's failure was `assert 21 == 20`: it says
something moved without saying what, must be bumped by hand, and passes for the wrong
reason if an addition and a removal cancel. Both docs-manifest counts are replaced by
coverage assertions — the manifest and the published site must be in bijection, modulo
the cookbook pages and index, which are checked against their own sources. Adding
`docs/runbook.md` would now name the file instead of printing two integers.

### 11.3 Verification

Local, on a 3.12 dev venv. GitHub CI has not run these changes.

| Suite | Result | Command |
| --- | --- | --- |
| Backend | **5,979 passed, 9 skipped, 0 failed** (357 s) | `pytest -n auto --dist loadgroup` |
| Lint / format | clean | `ruff check .` · `ruff format --check .` |
| Types | clean, 322 files | `mypy src` |
| Frontend types | clean | `npm run typecheck` |
| Paper | 76 pages, 0 overfull, no undefined refs | `make repro` |
| Structural checks | 8 passed | `make repro` |

Against §10.3's 5,965 the suite has grown by 14 — the capability-contract and CI-ledger
tests added here.

**The standing of this evidence is the same as §10.3's and no better.** Every number is
a local measurement. §3c's standard — "verified on the remote, not inferred from a local
run" — is not met, and the gate ledger itself has by definition never executed, which is
precisely the condition it exists to detect. It should be treated as unproven until a
real run produces its first summary.

### 11.4 What this does not change

§9's verdict stands unaltered: **feature-rich Alpha, credible for a controlled technical
pilot, not ready for supported production or untrusted authoring.** Nothing here was
aimed at it. The one item that would move it — a supported production topology — remains
unattempted, and §10.5's ledger of genuinely open work (F-11's 224 handlers, F-04's trace
half, F-10's broker replay, F-12's lexical fusion, F-02b's cancellation propagation,
F-06's product decision) is unchanged.

What changed is narrower: the class of defect this report spent three waves finding one
instance at a time is now, in five specific places, structurally harder to introduce.

### 11.5 A LinkedIn article package, and the copy that drifted inside one session

Landed in `LinkedIn_Caliber/`: a Markdown article on the paper, a DOCX and PDF built
from it, eight generated images, and `make_images.py`, which regenerates every image
from SVG via `rsvg-convert`. It is external communication collateral, the same category
as the seminar deck §10.1 records, and it changes nothing about the product.

Three things about it are worth recording rather than left to be discovered.

**It states no performance numbers, because the paper has none.** The obvious shape for
a package like this is a hero slide of percentages. The paper's results table is empty
by choice, so the hero carries architectural counts instead — nine families, seven chain
terms, four rollback mechanisms — and one of the eight images is devoted entirely to
what the paper does *not* establish. A marketing asset that quietly invented the numbers
the paper refused to invent would undo the discipline §11.2 is about.

**It is three more hand-written copies of the family table.** `make_images.py` hardcodes
the full nine-row guarantee table for one image, and the nine family/rollback-mechanism
pairs again for the one-pager; `LinkedIn_Article.md` names all five rollback semantics a
third time in prose. All three agree with `artifact_capabilities.py` today because they
were authored from the generated table after §11.2 landed. Nothing keeps them agreeing.

**And one of them had already gone stale before this section was written.** The §11.2
edit that narrowed the paper's §5.1 — from "mitigated by documentation, which is a weak
mitigation" to the registry carrying the mechanism — left the article still asserting the
old sentence. Two hours old, one session, no intervening commit, and it was wrong. The
article is corrected; the point is that the drift did not wait for a future editor. It is
the cheapest available demonstration that a hand-maintained copy of a moving fact decays
immediately, not eventually.

We are still not wiring the package to the registry, and the reason is a boundary rather
than laziness: a published article is a dated statement about what was true when it was
written, and silently regenerating its claims from a moving tree would make it a worse
record, not a better one. The deck is treated the other way — it interpolates its counts
from `macros.tex` — because it ships *with* the paper and has to move when the paper does.

The residual risk is therefore real and bounded, and now demonstrated: if a family's
rollback mechanism changes, the paper's table follows automatically and this package does
not. It is outside the product, outside CI, and outside every check §11 added. Anyone
editing it should diff the claims against `paper/tables/tab-families.tex` first.

---

## 12. Update, 2026-08-08

> **Tree:** `678127229` (`main`, and `origin/main` — §11's work merged as PR #31).
>
> **Scope of this update:** discharge the evidence caveat §11.3 left open, and record what
> checking it turned up. No product code was changed for this section. No claim in §1–§11 has
> been rewritten in place.
>
> **One finding dominates.** The Pages fix §10.2 recorded as resolved has never reached the
> site: the deployment has been parked in `waiting` for ~59 hours and every publication run
> behind it has been cancelled or is still queued. See
> [§12.4](#124-the-fix-in-102-is-merged-and-has-never-deployed).

### 12.1 The evidence standard §10.3 and §11.3 could not meet is now met

Both prior updates closed by saying their numbers were local, that §3c's standard —
"verified on the remote, not inferred from a local run" — was not met, and that it should be
re-established once Actions recovered. It has been. CI run
[`31235327573`](https://github.com/rrahimi-uci/caliber-suite/actions/runs/31235327573) ran the
merged tree on `main` at 2026-08-08T02:36Z and **all 13 jobs succeeded**.

| Suite | Result | Where |
| --- | --- | --- |
| Backend (Python 3.11) | **5,976 passed, 12 skipped, 0 failed** (986 s), 93% coverage | CI job `Test (Python 3.11)` |
| Frontend | **115 files, 1,570 tests, 0 failed** (269 s) | CI job `UI (test + build)` |
| Compatibility | 3.10 and 3.12 legs green | CI jobs `Compatibility (Python 3.10/3.12)` |
| Lint, format, types, security, wheel, compose, cookbook journeys | green | remaining CI jobs |
| Gate ledger | `all 11 required gates executed` | CI job `Gate execution ledger` |

**The gate ledger has now executed — and its failing branch has not.** §11.3 said the ledger
"has by definition never executed, which is precisely the condition it exists to detect," and
that it "should be treated as unproven until a real run produces its first summary." That run
has happened: it enumerated all eleven required gates, found every one `success`, and printed
`all 11 required gates executed`.

What that proves is that the ledger *runs and reports*. It does not prove the thing the ledger
exists for. `gate_ledger.py` has three exits: `return 0` when nothing is missing, `return 0`
when gates are missing *but another job already failed* (the deliberate `needs:` cascade
exemption §11.2 describes), and `return 1` when a run would have reported success with a gate
that produced no evidence. Only the first has ever run — the green `main` run took it, and the
PR run that preceded it ([§12.3](#123-a-transitive-dependency-moved-under-a-clean-install-for-the-third-time))
had a real failure and so took the second. **The `return 1` branch has never executed, and no
test drives it.** The three contract tests §11.2 describes import the module only for its
`REQUIRED_GATES` constant and check that constant against `ci.yml`; none constructs a job list
with a skipped gate and asserts a non-zero exit.

That is worth stating plainly because of what the control is for. §3c's finding was a correct
gate that had been skipped on twelve consecutive runs, and §9 concluded that "a gate that never
runs is indistinguishable from a gate that passes." The ledger was built to make that
detectable, and the detecting half of it is now itself a never-executed path — with strictly
less standing than `package` had, because `package` at least had no test *and* was expected to
run. This is not an argument against the ledger; it is the smallest possible version of the
same defect, and it is a ten-line test to close: call `main()` against a fabricated job list
with one gate `skipped` and no failures, assert `1`.

**Job-count correction.** §10.3 recorded 12 CI jobs. There are now **13** — `678127229` added
`Gate execution ledger`. The ledger's own "11" is a different quantity and the two should not
be conflated: 11 is the count of *required gates*, which excludes `Allure report` (a reporting
step, not a gate) and the ledger job itself.

### 12.2 The skip characterisation narrowed between §5 and §10.3, and the narrowed one is wrong

§5 describes its 9 skips as "opt-in integration **and** Postgres-marked tests," which is
exactly right. §10.3 restates them as "the opt-in Postgres-marked tests, unchanged from
baseline," dropping the integration half — and §11.3 quotes the same 9 with no
characterisation at all. The claim did not start wrong; it got worse each time it was copied
forward, which is §11.1's pattern operating on this document itself.

The actual composition, read off the CI run's short test summary:

| Skips | Reason | Marked |
| --- | --- | --- |
| 6 | `set CALIBER_INTEGRATION_TESTS=1 to run integration tests` | `integration` |
| 3 | `POSTGRES_URL not set` | Postgres |
| 2 | `tesseract binary not installed` | environment-dependent |
| 1 | `UI dist/package bundle not built in this checkout` | environment-dependent |

Only three of the twelve are Postgres. Six are integration-marked, a different opt-in with a
different meaning — and the last three are not opt-in at all, they are *this machine lacks a
binary*. Calling them all "opt-in Postgres-marked" made the suite look more uniformly
deliberate than it is.

The local and remote figures reconcile exactly, which is the useful part. A local run for this
update (`pytest -n auto --dist loadgroup`, 3.12 venv, 356 s) reported **5,978 passed, 1 failed,
9 skipped**, and its skip summary is six `CALIBER_INTEGRATION_TESTS` plus three
`POSTGRES_URL not set` — §5's description exactly, and none of §10.3's. So:

| | Collected | Passed | Skipped | Failed |
| --- | --- | --- | --- | --- |
| Local (this update) | 5,988 | 5,978 | 9 | 1 (self-inflicted, [§12.5](#125-an-uncommitted-rename-in-the-working-tree-reproduces-3cs-docs-failure)) |
| Local (§11.3) | 5,988 | 5,979 | 9 | 0 |
| CI `31235327573` | 5,988 | 5,976 | 12 | 0 |

Three totals, three machines, one collection count. The three tests that move are precisely the
environment-dependent ones — a dev machine has tesseract installed and a built UI bundle, so it
runs what CI skips. The runs do not disagree; they differ in exactly the way the skip reasons
predict, which is what makes the reconciliation worth doing rather than just quoting the larger
number.

### 12.3 A transitive dependency moved under a clean install, for the third time

The PR that became `678127229` failed its first CI run
([`31233201952`](https://github.com/rrahimi-uci/caliber-suite/actions/runs/31233201952)) with
**3 failed, 5,973 passed** — all three in `tests/test_mcp_db_tools.py`, all three
`pydantic_settings.exceptions.IncompleteFieldDefinitionWarning`. pydantic-settings 2.15.0
introduced that warning class; `mcp`'s FastMCP `Settings` model trips it because its
`lifespan` field is annotated with a forward reference the model never rebuilds; and the
suite's `filterwarnings = error` turns a third-party warning into three failures. Neither the
field nor the fix is ours. A venv built a day earlier had 2.14.2 and passed.

This is the **third instance of one shape**, and it is now worth naming separately from §3c's
pattern rather than filed under it:

1. numpy 2.5 moved its bundled stubs to PEP 695 `type` statements and broke `mypy` on any
   clean install (§3c).
2. `make install-extended` did not install the `memory` extra CI installs, so a contributor
   following the documented profile got a red suite (§3c).
3. pydantic-settings 2.15.0 added a warning class that `error` promotes to a failure.

The common mechanism is not a control whose description outran its implementation. It is
narrower and more mundane: **CI is the only machine in this project that ever performs a clean
dependency resolve.** Every developer result comes from a venv built at some past moment and
never re-resolved, so the tree is continuously being validated against a dependency set that
no fresh install would reproduce. Two of the three were caught by CI on first contact, which
is the system working; the point is that nothing else *could* have caught them.

The fix is scoped narrowly and its reasoning is recorded beside it in `caliber/pyproject.toml`
rather than in a commit message: the filter matches the warning **message**, not the category,
because naming the category would make the whole suite unrunnable against pydantic-settings
< 2.15 where the class does not exist; and it is pinned to the single `lifespan` field so a
genuinely new incomplete definition — in `mcp` or in our own settings models — still fails.

### 12.4 The fix in §10.2 is merged and has never deployed

§10.2 records the two-publisher defect as closed: "Resolved in `7321c2574` by composing rather
than choosing: one deployment now carries the docs at `/` and the report at `/tests/`." The
workflow file does exactly that. The site does not.

Measured against the live site on 2026-08-08:

| URL | Status |
| --- | --- |
| `https://rrahimi-uci.github.io/caliber-suite/` | **200** |
| `https://rrahimi-uci.github.io/caliber-suite/tests/` | **404** |
| `https://rrahimi-uci.github.io/caliber-suite/runbook.html` | **404** |

The runbook 404 dates the live site. `docs/runbook.md` landed in `5b0ad0c75` and §10.1
announced it as published; it is not on the published site. What is serving is GitHub
deployment `5781346617`, **sha `764ffb9c5`**, successful at 2026-08-06T14:31:15Z — one commit
*before* `7321c2574`. The composed layout has therefore never been the site, and §10.2's
measured "~152 MB composed, report ~126 MB across ~9k files, `history.json` 15 MB" describes an
artifact that exists only as a build output.

Every Pages run since:

| Run | Trigger | Sha | Outcome |
| --- | --- | --- | --- |
| `31111026410` | `workflow_dispatch` | `764ffb9c5` | **success** — this is the live site |
| `31121508186` | push | `7321c2574` | `build` success; **`deploy` has been `waiting` since 2026-08-06T17:09Z** |
| `31121567501`, `31121570594` | push, `workflow_run` | `7321c2574` | cancelled |
| `31235327584` | push | `678127229` | cancelled |
| `31236063049` | `workflow_run` | `678127229` | **pending**, and still pending 25 hours after CI went green |

Deployment `5783235987` (`7321c2574`) has exactly one status event in its whole history —
`waiting`, at 2026-08-06T17:09:56Z — and never reached `queued`. For contrast, the live
deployment went `queued → in_progress → success` in 118 seconds.

**What can and cannot be concluded.** The cascade is mechanical and certain:
`concurrency: {group: pages, cancel-in-progress: false}` admits one run to the group, so a run
that never finishes holds it and everything behind it queues or is cancelled — which is
exactly the observed sequence of two cancels and a pending. Why the first deploy parked in
`waiting` is *not* established here. It is not a human approval gate: the environment's single
protection rule is a branch policy whose one entry is `main`, the run is on `main`, and the
pending-deployment record shows `wait_timer: 0` with `reviewers: []`. Recorded as unexplained
rather than guessed at.

The workflow's own comment chose that concurrency setting with this reasoning:

> One deployment at a time, and in-flight runs are NOT cancelled: […] cancelling one
> mid-flight leaves the environment holding a cancelled deployment. A superseded run costs a
> few minutes; a wedged deployment costs an outage.

The reasoning is sound and the feared outcome happened anyway — a wedged deployment, now
~59 hours old, blocking every publication change since `7321c2574`. The setting did not cause the
wedge, but it converted one stuck run into a stopped pipeline, and there is no alarm on that
state: the *workflow* is not red. Four Pages runs in a row failed to publish and not one of
them reports as a failure.

**This is the sixth instance of §11.1's pattern and it lands on §10.2's own sentence.** §10.2
coined "a publisher with no reader is indistinguishable from a publisher that works." The
correction it prescribed earns a sharper one: **a fix that is merged is not a fix that is
deployed.** §11.1's formulation — one fact, two copies, nothing making them the same object —
holds here with the two copies being *the workflow file* and *the live site*. `7321c2574`
changed the first and nothing has ever compared it to the second. The gate ledger §11.2 added
answers "which CI gates ran?"; nothing answers "did the last merge reach the site?"

**Owner action, not a code change.** Cancel the wedged run (`gh run cancel 31121508186`), then
re-dispatch Pages and confirm `/tests/` and `/runbook.html` return 200. The durable fix is a
check that reads the live site rather than the workflow's exit code — the same move §11.2 made
for the gate ledger, applied one layer further out.

### 12.5 An uncommitted rename in the working tree reproduces §3c's docs failure

The working tree at the time of this update carries an uncommitted rename of this document:
`product-completness-developement-report.md` deleted, `product-completness-report.md`
untracked. Four committed files link to the old name — [README.md:247](README.md#L247),
[ARCHITECTURE.md:608](ARCHITECTURE.md#L608), `docs-site/m-00-layered-architecture.md:603`, and
`caliber/caliber-ui/public/docs/m-00-layered-architecture.md:603`, plus the gitignored
packaged copy under `caliber/src/caliber/ui/docs/`.

The Pages parity gate was reproduced against this tree rather than reasoned about:

```console
$ CALIBER_DOCS_STRICT=1 node caliber/caliber-ui/scripts/sync-docs.mjs
$ git diff --exit-code -- docs-site caliber/caliber-ui/public/docs
  → 2 files changed        # FAILS
```

The generator classifies a link by whether its target exists and rewrites the missing one from
an absolute `blob/main` URL to a relative path. This is §3c's second CI failure verbatim —
"committing the report deletion left dangling links […] the generator classifies a link by
whether its target exists, so the deletion silently changed generated output" — reproduced by
the same file, two waves later.

**And `tests/test_docs_generation_contract.py` passes on that same tree** (5 passed, verified
twice). §10.6 named this exact structure — "a correct gate that was never asked" — and this is
its mirror: here the pytest gate *is* asked, answers a different question, and is green while
the Pages gate is red on the identical defect.

The reason is not an oversight, which is what makes it worth recording. The staleness test,
`test_all_manifest_markdown_is_current_and_published`, replaces **every link destination with a
`<LINK>` placeholder** before comparing source to generated output, and its docstring explains
why: "Link destinations intentionally change during flattening. Removing only those
destinations lets this independent test catch stale prose […] while the link-resolution test
below validates the rewritten destinations themselves." That division of labour is sound in
principle. In practice this defect is *entirely* a link destination, so the staleness test
blanks it by construction, and the link-resolution test — which asks whether the flattened
links resolve inside `docs-site/` — does not object to the rewritten one either. Two tests,
each correct about its own question, and the defect falls in the seam between them.

Only the Pages gate regenerates and diffs against git, so only the Pages gate compares the
output to what is *committed* rather than to another derivation of the same stale input. §11.1
said it about `gen_stats.py` and it holds here: a consistency check between copies is not a
check on the fact.

The rename is fine to make. It must be made together with an edit to the four linking files
and a regeneration, in one commit.

**A recovery defect found by walking into it.** Reproducing the parity gate above means running
the generator, which writes three destinations: `docs-site/`, `caliber/caliber-ui/public/docs/`,
and `caliber/src/caliber/ui/docs/`. The first two are tracked and `git checkout` restores them.
**The third is gitignored, so git cannot restore it at all** — and once it holds regenerated
content while the tracked copies hold committed content, the copies no longer agree and
`test_all_materialized_docs_copies_match_docs_site` fails. That is the single failure in the
local run in §12.1: not a defect in the tree, but one this exercise created and then could not
`git checkout` away.

The only way back is to make the sources consistent and regenerate — here, restore the deleted
report file, re-run `sync-docs.mjs`, confirm `git diff --exit-code` is clean on the tracked
pair, then re-apply the deletion. That works, and it is not discoverable from the failure
message, which names a mismatch between two directories and says nothing about one of them
being outside version control. Worth a line in `docs/runbook.md`: *a generated tree that is
excluded from version control has no restore path, only a rebuild path.*

Also uncommitted, and outside every number in §12.1 (CI measured `678127229`, not this tree):
`start.sh` gains MLflow host-port conflict detection and automatic free-port selection, with
matching notes in `deploy/.env.example`, `deploy/README.md`, and `deploy/mlflow/README.md`.

### 12.6 Two smaller drifts found while checking

**The paper's provenance stamp names a tree that is not in history.**
`paper/generated/stats.tex` carries `\statGitRev{3b0c85959-dirty}` and the header comment
"Produced […] from the CALIBER tree at `3b0c85959-dirty`". Two things are true of that string:
it is not `678127229`, the commit the paper ships from; and the `-dirty` suffix means it names
a working-tree state that was never committed and cannot be recovered. Nothing checks the
stamp against `HEAD` — `gen_stats.py` emits it and no test reads it. Low severity, but a
provenance field that cannot be resolved to a tree is not provenance. The *counts* are current
and were re-derived: 322 Python modules / **127k** lines (§10.3 said 126k), **310** pytest
modules (§10.3 said 308) / 137k lines, 292 TS modules / 176k, 487 routes across 47 modules,
85 migrations, 76 ORM models, 226 schemas, 31 node types. The loop count was re-checked
against the tree rather than the macro: `server.py` lines 192–204 contain exactly **nine**
`await <task>.start()` calls, matching `\statLoops{9}`, which is §11.2's derivation working.

**Seven test modules cite a provenance document that does not exist.**
`tests/test_scoping.py`, `test_routes_evaluations_visibility.py`,
`test_routes_evaluations_reproducibility.py`, `test_workflow_run_trace_linkage.py`,
`test_settings_routes.py`, `test_eval_scorecard_weighting.py` and
`test_cookbook_doc_contract.py` — plus `docs-site/cookbooks/CRITIQUE-REPORT.md` — attribute
their existence to `ui-complete-report.md`, several citing specific sections (`§C4`, `§C2`,
`§3`, `§4`). No file of that name exists anywhere in the tree. What exists is `ui-complete.md`
at the repository root, which nothing references, nothing publishes, and no manifest lists.
Either the citations are misnamed or the file is; as it stands, seven test modules explain
*why they exist* by pointing at nothing, and a root-level document sits unreachable from any
index. That is the cheapest possible form of the same defect — and unlike the others it costs
a rename to fix.

### 12.7 What this does not change

§9's verdict is unaltered: **feature-rich Alpha, credible for a controlled technical pilot,
not ready for supported production or untrusted authoring.** No product code was changed for
this update. §10.5's ledger of genuinely open work — F-11's 224 handlers, F-04's trace half,
F-10's broker replay, F-12's lexical fusion, F-02b's cancellation propagation, F-06's product
decision, and the XL items F-16 / C-5 / C-2B / H-1 — is unchanged and was not attempted.

What *did* change is the standing of the evidence, in both directions. §11.3's caveat is
discharged: the tree is verified on the remote, all 13 jobs green, with the gate ledger's first
real summary. And a gate one layer beyond CI was found open — the merge-to-published path,
where **five consecutive Pages runs have failed to publish and not one of them reports as
failed**. §9 said any readiness assessment should treat "which gates have actually executed
recently?" as first-order. §11.2 made that answerable for CI. §12.4 is the same question asked
one step later — *did the last merge reach the reader?* — and the answer is that nobody was
asking it, so for 59 hours the answer was no.

The three items this update leaves for someone to act on, smallest first: rename
`ui-complete.md` or the seven citations to it (§12.6); commit the report rename together with
its four link edits and a regeneration (§12.5); and unwedge Pages, then add a check that reads
the site rather than the workflow's exit code (§12.4).
