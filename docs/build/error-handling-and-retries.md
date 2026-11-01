---
audience:
  - developer
doc_type: how-to
product_area: sdk
stability: ga
prerequisites:
  - A CALIBER integration using the SDK or REST API
reviewed_on: 2026-10-14
version_applicability: current main branch docs contract
tags:
  - errors
  - retries
  - waiters
  - sdk
---

# Error handling and retries

Use this page when you need the practical integration policy for failures:
which problems are auth or scope issues, which are transient, and when to use
waiters instead of building your own polling loop.

## At a glance

| Concern | Recommended default | Related docs |
| --- | --- | --- |
| Authentication and project scope | validate them first | [Auth and project scoping](../build/auth-and-project-scoping.md) |
| API errors | prefer typed SDK errors where available | [SDK API reference](../sdk/reference.md) |
| Long-running operations | use the SDK waiter/polling path | [CLI and async client](../sdk/cli.md) |
| Raw HTTP diagnostics | capture request ids and envelopes | [Authentication and conventions](../api/auth.md) |

## 1. Classify the failure before retrying

Start by separating:

- auth or permission failures
- wrong-project or wrong-scope failures
- request-shape or validation failures
- transient transport or dependency failures

Retrying the first three classes usually only creates noise.

On the wire the classes map to statuses like this:

| Status | Usually means |
| --- | --- |
| 400 | request-shape failure; a validation failure carries an `errors` list of `loc`, `msg` and `type` |
| 401 | no usable credential |
| 403 | the token's scope, the caller's project role, a project-bound token naming another project, or (where CSRF is enabled) a missing or stale CSRF token |
| 404 | the object does not exist, or the caller is not a member of its project |
| 409 | a state conflict (for example a primary owner that cannot be changed, or a duplicate member) |
| 429 | rate limited; the response carries `Retry-After` |
| 503 | a dependency is down: the database, or MLflow (the message names `MLFLOW_TRACKING_URI`); `GET /readiness` says which. Lock contention is also a `503`, with `reason_code` `apply_busy` (Apply) or `approval_busy` (an approval decision): a lock timeout, a deadlock the request lost, or a busy SQLite file; nothing was decided, so retry |

Some routes also return a `reason_code` string beside `detail`; it is optional,
so fall back to `detail` and the status when it is absent.

`POST /jobs/{job_id}/apply` is the clearest case. Its refusals are `403`
(`apply_self_approval`, `apply_creator_unknown`, `apply_permission_revoked`), `409`
(`apply_candidate_changed`, `apply_candidate_unverified`) and `503` (`apply_busy`). Each
one changed nothing, so it is safe to call again after the cause is fixed, but only
`apply_busy` is worth retrying as it is: it means another transaction held a lock the
claim needed and nothing was decided. On PostgreSQL that is a row lock that was not
granted within 5 seconds, or a lock-order deadlock in which this claim was the victim;
on SQLite it is the write lock held past the 30 second busy timeout. The SDK does not retry a `POST` on its own, so
`caliber.jobs.apply(...)` raises `CaliberServerError` with `reason_code == "apply_busy"`
and the caller waits and calls again. The other codes need a person to act (a different
account, a restored grant, another evaluation pass). A `500` from Apply carries no code
and is not one of these lock cases (a serialization failure, `40001`, is deliberately not mapped
and also answers `500`): read the job's status before calling again. The `503` carries
`Retry-After: 1`. The table of codes, the recovery for each and an SDK example are in
[Apply: refusal catalogue, audit and recovery](../operate/reference-profile.md#apply-refusal-catalogue-audit-and-recovery-b065-762).

## 2. Use waiters for long-running operations

If the product already exposes a waiter or polling contract, use that rather
than inventing an uncontrolled sleep loop in your application.

That keeps retry behavior aligned with the documented SDK surface. The SDK waiters
poll with a growing interval (2 seconds, times 1.5 each poll, up to 15 seconds),
never sleep past your deadline, and raise `WaitTimeout` when it passes. The default
deadline is 900 seconds for runs, jobs, evaluations and Workspace operations, and
600 seconds for tool calibration jobs. A job that stops for a person
(`candidate_ready`) is a stopped state, not a timeout.

## What the SDK retries for you

The SDK retries only `GET`, `HEAD` and `OPTIONS`, on a transport error or a 429,
502, 503 or 504. It makes two retries by default (`max_retries`), waits
0.5 seconds and then 1 second (`backoff_factor` doubling per attempt), and uses a
numeric `Retry-After` instead when the server sends one. It never retries a
`POST`, `PATCH`, `PUT` or `DELETE`, because it cannot know whether the first
attempt took effect; workflow run submission accepts an `idempotency_key` so you
can retry that one safely yourself. The one replay it does make on a write is a
single re-send after a CSRF refusal, once it has fetched a token.

## 3. Retry safely

For automated retries:

- prefer idempotent reads first
- be careful with writes that can change external state
- capture request identifiers and final error context

## 4. Related docs

- [Auth and project scoping](../build/auth-and-project-scoping.md)
- [Developer troubleshooting](../build/developer-troubleshooting.md)
- [SDK API reference](../sdk/reference.md)
- [Authentication and conventions](../api/auth.md)
