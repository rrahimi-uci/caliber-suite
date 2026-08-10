# TypeScript SDK: not yet, and the shape it should take

**Status:** deferred, with a concrete plan. Last assessed 2026-08-10.

M5's third item is a TypeScript SDK. The plan gates it explicitly:

> Only after the Python SDK is stable:
> - add a TypeScript SDK from the same OpenAPI contract or extracted shared client logic

This records that the gate is not met, what would be built when it is, and the
reasoning — so the next person picking this up does not re-derive it.

## Why not now

**The Python SDK is `0.1.0.dev0`, and its beta surfaces landed this week.** Thirteen
route groups (integrations, operations, the agentic loop) are marked beta
precisely because their shapes may still move. Building a second client against
shapes that are still moving means two clients to change on every move, and the
second one — the less-exercised one — is where a rename gets missed.

**The failure mode is documented in this repository's own history.** During M2 and
M3, `GET /workflow-runs` (POST-only), `GET /services` (404), and
`ReleasesAPI.list` (no such route) were each written against a plausible reading
of the API and each caught only by driving the SDK against the real running
application. A TypeScript client would need equivalent coverage — an in-process
or containerised server, driven from vitest — or it would ship with the same
class of defect and no way to find it.

**The plugin contract just proved the general point.** `caliber-plugin-sdk` and the
server neither import each other, and that independence is exactly where they
diverged: the server's loader required its own `OptimizerSpec`, so a plugin
written against the plugin SDK's documented API was rejected outright. It took a
cross-package test to find. A second client in a second language is that same
seam, wider.

## What should be built

Not a hand-written second client. Two halves, and the split matters.

### 1. Extract the SPA's client core

`caliber/caliber-ui/src/api/caliberApi.ts` is 4,727 lines and already solves
everything an SDK core must solve, in production, under real load:

| Concern | Where it lives today |
| --- | --- |
| Auth boundary + generation tracking | `authGeneration`, `authBoundary`, `requireSameAuthBoundary` |
| CSRF bootstrap, refresh, and rejection detection | `fetchCsrfToken`, `bootstrapCsrf`, `refreshCsrfToken`, `isCsrfRejection` |
| The request loop and envelope unwrapping | `doFetch`, `request<T>`, `requestEnvelope<T>` |
| Multipart upload and binary download | `uploadMultipart`, `downloadFile` |
| Read timeouts | `setApiReadTimeoutMs` |
| Session invalidation on 401 | `clearSessionOnUnauthorized` |

That is roughly lines 289–953. Everything after line 954 is the `caliberApi`
object: several hundred thin per-endpoint methods, which is the part that should
be generated rather than extracted.

The extraction is the whole first PR: move the core into
`sdk/caliber-ts/src/core/`, have the SPA import it, and change nothing about
behaviour. It is testable as a pure refactor — the SPA's existing vitest suite is
the regression gate — and it is valuable even if the SDK never ships, because the
core stops being buried in the same file as 700 endpoint wrappers.

One thing the extraction must **not** carry across: `clearSessionOnUnauthorized`
and the auth-generation machinery are SPA concerns. A browser app clearing its
session on 401 is correct; a Node script doing it is meaningless. Those stay in
the SPA layer, and the extracted core exposes a hook instead.

### 2. Generate the endpoint surface

The management API already serves its own OpenAPI document at
`GET /ajax-api/2.0/mlflow/caliber/openapi.json` — 287 paths, 359 operations, built
from the live route table rather than hand-maintained. That document is the
contract, and it is why the endpoint layer should be generated:

- a generated client cannot drift from the route table, because the route table
  is its input;
- `x-caliber-stability` per operation lets generation emit GA and beta surfaces
  into separate modules, so a consumer's imports say which tier they depend on;
- when the beta shapes settle, regenerating is a build step rather than a
  translation exercise.

Generation belongs in CI with a check that the committed output matches what the
current document produces — the same shape as the existing docs-bijection gate,
for the same reason: generated output that nobody verifies is just stale output.

### 3. What it needs to be honest

- **A real-server test leg.** vitest against the containerised app (the `compose`
  CI job already builds one), covering at least the flows
  `caliber/tests/test_sdk_against_server.py` covers for Python. Mocked tests
  prove a client is self-consistent and nothing more.
- **A parity gate against the Python SDK.** The two clients must agree on envelope
  handling, which methods are retried, and the error taxonomy.
  `sdk/caliber-sdk/tests/test_async_parity.py` is the model — it asserts shared
  decisions by identity, not by behaviour, because two implementations can behave
  identically today and diverge on the next edit.
- **Its own CI job**, matching the `sdk`, `plugin-sdk`, and `cli` jobs: lint,
  typecheck, test, build, and an assertion about the published package's
  dependencies.

## The gate to re-check

Build it when all three hold:

1. `caliber-sdk` has cut a non-dev release, and its beta surfaces have gone a
   release without a breaking shape change.
2. The served OpenAPI document is stable enough that generated output changes only
   when routes change — verifiable by regenerating across two releases and
   diffing.
3. There is a consumer. A TypeScript SDK with no caller outside the SPA is a
   second client to maintain in exchange for nothing; the SPA is already served
   by `caliberApi.ts`, and extraction (step 1) delivers most of the internal
   value on its own.

Point 3 is the one most likely to be skipped. It is also the one that decides
whether this is an SDK or a liability.

## What was delivered instead

M5 shipped the two items whose preconditions *were* met:

- **`caliberctl`** (`sdk/caliber-cli`) — non-interactive operator commands, with
  six exit codes because a CI tool that only exits 0 or 1 forces its callers to
  parse output.
- **The async Python client** (`caliber_sdk.aio`) — because its precondition was
  checkable: sync request handling, SSE streaming, and file transfer were all
  implemented and tested. Its typed coverage is deliberately narrower than the
  sync client's, for the same reason this document recommends generating the
  TypeScript endpoint layer rather than writing it: a second hand-written copy of
  a resource tree is two places for a path to be renamed and one place for it to
  be forgotten.
