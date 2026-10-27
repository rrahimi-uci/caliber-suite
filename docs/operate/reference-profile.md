---
audience:
  - operator
doc_type: reference
product_area: operations
stability: beta
prerequisites:
  - A PostgreSQL database and a way to set environment variables for the CALIBER process
reviewed_on: 2026-10-26
version_applicability: current main branch docs contract
tags:
  - reference-profile
  - configuration
  - security
  - operations
---

# Reference profile

Use this page to run CALIBER as the reference deployment: one supported shape that **refuses to start**
while any of the settings below is unsafe. It is the deployment the EGI reference architecture's claims
(governed action, safe restart, evidence) are made about; `docs/architecture/adr/0001-reference-profile.md`
defines the whole shape, and this page lists what the process itself enforces today.

## Turn it on

Set `CALIBER_PROFILE=reference`. The default, `development`, starts with whatever is configured and behaves
exactly as before. Under `reference`, `create_app` checks the configuration **before** it opens the database
or creates the bootstrap account, so a refused start leaves nothing behind, and it lists **every** violation
at once:

```text
reference profile refused (2 violations):
- database_url is a SQLite file; set CALIBER_DATABASE_URL=<a PostgreSQL URL> for the reference profile
- csrf_enabled is false; set CALIBER_CSRF_ENABLED=true for the reference profile
```

An unknown profile name is a configuration error. `GET /ajax-api/2.0/mlflow/caliber/readiness` carries a
`profile` field, so an operator can see which rules the running process was started under.

## What it requires

| violation code | setting | environment variable | required | why |
| --- | --- | --- | --- | --- |
| `sqlite_database` | `database_url` | `CALIBER_DATABASE_URL` | a PostgreSQL URL | SQLite allows one writer and the reference profile's recovery and ownership guarantees need row locks and real transactions. |
| `csrf_disabled` | `csrf_enabled` | `CALIBER_CSRF_ENABLED` | true (with CALIBER_CSRF_SIGNING_SECRET set) | Without CSRF protection a page the approver visits can submit an approval for them. |
| `dev_fallback_enabled` | `auth_dev_fallback_enabled` | `CALIBER_AUTH_DEV_FALLBACK_ENABLED` | false | The no-header fallback makes an unauthenticated request an admin. |
| `insecure_bootstrap` | `auth_bootstrap_allow_insecure_default` | `CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT` | false | It allows an `admin`/`admin` bootstrap account when no password is set and the session cookie is not marked secure. |
| `trusted_header_without_proxy_secret` | `auth_mode` | `CALIBER_AUTH_TRUSTED_PROXY_SECRET_ENV` | `session`, or `trusted_header` with the name of the variable that holds the proxy secret | Trusting an identity header without a proxy secret lets any client claim any identity. |
| `egress_policy_disabled` | `egress_policy_enabled` | `CALIBER_EGRESS_POLICY_ENABLED` | true | The egress policy is what stops a tool or webhook from reaching internal addresses. |
| `mcp_insecure_http` | `mcp_allow_insecure_http` | `CALIBER_MCP_ALLOW_INSECURE_HTTP` | false | Plain-HTTP MCP servers send tool calls and results in the clear. |
| `self_approval_allowed` | `approval_allow_self_approval` | `CALIBER_APPROVAL_ALLOW_SELF_APPROVAL` | false | The account that started a run could approve its own protected action. |
| `memory_inference_enabled` | `memory_infer` | `CALIBER_MEMORY_INFER` | false while CALIBER_MEMORY_ENABLED is true | LLM-inferred memory writes create durable, free-form facts nobody owns; the owned memory lifecycle does not exist yet. |
| `fault_barriers_enabled` | `fault_barriers` | `CALIBER_FAULT_BARRIERS` | unset | An armed fault barrier makes the process exit on purpose; it exists only for scenario tests. |

The messages in the refusal name the setting and the variable to change. The table is checked by
`caliber/tests/test_profile.py`: a rule that is not listed here, or a row without a rule, fails the build.

## What it also does at runtime

Beyond refusing to start, the reference profile **bounds every request body**: a body above
`CALIBER_REFERENCE_MAX_BODY_BYTES` (default 1 MiB) is answered `413` with
`{"reason": "request_too_large", "max_bytes": N}`, whether the client declared a large `Content-Length` or
streamed the body without one (or understated it). The check is the outermost layer, so an oversized request
is refused before authentication, CSRF or routing reads it. A request at exactly the limit is accepted, and the
development profile installs no limit.

Two kinds of request have a limit of their own, because their routes accept more than 1 MiB. The larger limit
applies; the default is never lower than a route's own:

| request | limit | setting |
| --- | --- | --- |
| `multipart/form-data` (skill zips, attachments, workspace imports, storage objects) | 256 MiB | `CALIBER_REFERENCE_MAX_UPLOAD_BYTES`; raise it together with `CALIBER_WORKFLOW_STORAGE_MAX_UPLOAD_BYTES` (250 MiB), which each route still enforces, tighter, afterwards |
| the GitHub webhook route | `CALIBER_GITHUB_WEBHOOK_MAX_BODY_BYTES` (2 MiB) | the route's own setting; the global 1 MiB never lowers it |

A refusal carries `x-request-id` (the caller's, or one generated) and writes one warning log line naming the
method, path and size. The body is refused, not drained, so a client that keeps sending a very large body may see
a connection reset rather than the `413`.

## Memory is owned, derived and read-mostly

Until the owned memory lifecycle (M20: retention, legal hold, deletion) exists, the reference profile does not offer
free-form durable memory (B12 memory, #667). The `/memory` API and the in-run memory tools behave as follows:

- **No free-form partition.** `agent_id` must name an agent or workflow, and `run_id` a workflow run, that the caller
  can see; `user_id` must be the caller's own identity, admins included. A scope value that is present but not a
  non-empty string is a `400`, not ignored. A run id alone is refused (`403`).
- **The partition is pinned to the caller.** With an `agent_id`, the memory is the caller's own within that agent or
  workflow (the server adds `user_id`); with none, it is the caller's own namespace, qualified by the active
  project (`<project>:<user>`). A client never chooses whose memory a read or write reaches, and `metadata` may not
  carry `user_id`, `agent_id`, `run_id` or `actor_id` (`400`), because mem0 filters on those stored fields.
- **One refusal, no disclosure.** A partition that does not exist, one that belongs to another project or user, and a
  label that names nothing all answer `404 memory partition not found`; no id is echoed.
- **Writes are verbatim.** `POST /memory` requires `infer=false` (`403` otherwise), because LLM-inferred writes create
  durable facts nobody owns; and startup refuses `CALIBER_MEMORY_INFER=true` while memory is enabled (table above).
- **No bulk deletion.** `DELETE /memory` answers `403`: deleting memory needs the lifecycle's retention and legal-hold
  rules.
- **Bounded reads.** `top_k` is capped at 100.
- **In-run tools.** `memory_search`, `memory_add`, `memory_list` and `list_entities` act on the run initiator's
  memory within the workflow (the initiator is set by the server, never by the model); a run with no initiator gets no
  memory tools.
- **Same rules below the route.** The service layer refuses bulk delete, inferred writes, run-only partitions and
  partition keys in `metadata` (`MemoryLifecycleUnavailableError`). It does not itself check that an `agent_id` or
  `user_id` belongs to the caller: the route and the in-run tool binding do that and are the only callers.

The development profile keeps the previous behaviour. **Known limitation:** the installed mem0 2.x rejects entity ids
passed as top-level search/list arguments, which the service does today (also in development); until that is
fixed (tracked separately) search and list through the memory service return an error.

## A configuration that passes

```ini
CALIBER_PROFILE=reference
CALIBER_DATABASE_URL=postgresql+psycopg://caliber:CHANGE_ME@db.internal:5432/caliber
CALIBER_AUTH_MODE=session
CALIBER_AUTH_BOOTSTRAP_ADMIN_PASSWORD_ENV=CALIBER_BOOTSTRAP_PASSWORD
CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT=false
CALIBER_AUTH_DEV_FALLBACK_ENABLED=false
CALIBER_ADMIN_USERS=admin
CALIBER_APPROVER_USERS=approver
CALIBER_CSRF_ENABLED=true
CALIBER_CSRF_SIGNING_SECRET_ENV=CALIBER_CSRF_SIGNING_SECRET
CALIBER_EGRESS_POLICY_ENABLED=true
CALIBER_MCP_ALLOW_INSECURE_HTTP=false
CALIBER_APPROVAL_ALLOW_SELF_APPROVAL=false
```

Two of the variables name *other* variables that hold secrets (`CALIBER_BOOTSTRAP_PASSWORD` and
`CALIBER_CSRF_SIGNING_SECRET`); set those in your secret store, not in this file. What happens when they are
missing differs, and neither case is a profile refusal:

- **No CSRF signing secret:** with CSRF on, the server refuses to boot (a `RuntimeError`, not the profile's
  `ConfigError`). That check runs *after* the database is opened and the bootstrap account is written, so unlike
  a profile refusal it can leave those behind.
- **No bootstrap password:** the server **starts** and logs a warning, but nobody can sign in.
- **No role lists:** `CALIBER_ADMIN_USERS`, `CALIBER_OPERATOR_USERS` and `CALIBER_APPROVER_USERS` default to
  empty, and the bootstrap account is then a viewer that cannot create projects; the server logs an error. The
  example names an admin and an approver. Because self-approval is refused, an approver must be a different
  account from whoever starts a run.

The database must be migrated (`caliber-migrate`) before the first start.

## A stored secret never leaves the platform

`caliber/tests/test_secret_canary.py` (B12.5b, #666) stores `EGI_CANARY_<uuid4>` as `egi-canary` through the
encrypted secret store, points `llm_api_key_env` at `secret://egi-canary`, and runs the core journey over HTTP with
logging at DEBUG: sign in (the trusted proxy header), create a project, register an agent, publish, promote and run
a workflow, run a **calibration run** to a candidate and **apply** it, read the LLM provider status (which resolves
the secret inside the request; the test fails if nothing in the journey did), and export the audit log, plus refused
requests. It fails if the value appears in any response (status, headers, cookies, body; the helper calls too), any
log record (message, arguments, `extra` fields as the JSON formatter renders them, traceback), any
`caliber_workflow_run_events` row or any audit row. The failure paths of resolution (a revoked secret, corrupted
ciphertext, a provider built from a revoked secret) are scanned the same way, and a control test plants the value in
every scanned surface to prove the scan can fail. It runs in the Backend core job and the full backend suite.

Not covered: the prompt *registry* routes and MLflow traces need a live MLflow registry, which ordinary CI does
not have; the journey's prompts are the agent prompts in the workflow manifest, which the calibration run rewrites.
Sign-in by session login is covered by `test_auth_sessions`, not by this journey.

Running it found that `CALIBER_LOG_LEVEL=DEBUG` with the default S3 log sink recursed: the S3 client's own DEBUG
records re-entered the handler, so each record caused a growing number of S3 writes. The handler now ignores records
emitted while it delivers a batch.

## Run manifests are written once

Migration `0114` adds `caliber_run_manifests` (B02.1a, #656): one row per workflow run holding the canonical
manifest bytes (`manifest_bytes`) and their SHA-256 (`manifest_digest`). The row is immutable:

- a database trigger refuses `UPDATE` and `DELETE` on SQLite and PostgreSQL (PostgreSQL also refuses `TRUNCATE`),
  so a bulk SQL statement fails as well as an ORM one: `caliber_run_manifests is immutable: UPDATE refused`;
- the ORM raises `ImmutableRecordError` before any SQL for an instance update or delete and for an ORM
  `update()`/`delete()` statement (`Session.bulk_*_mappings` and Core/raw SQL skip the ORM and are refused by the
  trigger instead);
- a second manifest for the same run violates `uq_run_manifest_run`, and a digest that is not the SHA-256 of the
  bytes is refused (by the ORM everywhere, and by a CHECK on PostgreSQL).

**Runtime role.** The migration revokes `UPDATE`, `DELETE` and `TRUNCATE` on the table from `PUBLIC`. That only
protects a deployment that runs the application as a role other than the table's owner, and `caliber-migrate`,
`scripts/install.sh` and the shipped compose files use one database URL for both, so by default the application
*is* the owner and is held only by the triggers and the ORM guard. For the privilege layer, run `caliber-migrate`
as the owner and the application as a role granted only what it needs:

```sql
GRANT SELECT, INSERT ON caliber_run_manifests TO caliber_runtime;
GRANT SELECT ON caliber_workflow_runs TO caliber_runtime;  -- already held; the insert trigger reads it
-- no UPDATE, DELETE or TRUNCATE on caliber_run_manifests; avoid ALTER DEFAULT PRIVILEGES ... GRANT ALL
```

A trigger can be disabled by the table's owner or by a session with `session_replication_role = replica`; only the
missing privilege holds against the runtime role. The reference profile does **not** check which role the
application connects as (listed under "Not enforced yet"). The trigger functions are pinned to the migration's
schema with `pg_temp` last in `search_path`, so a temporary table cannot shadow `caliber_workflow_runs`.

**Legacy runs.** `caliber_workflow_runs.manifest_legacy` is `true` by default, so every run that existed when `0114`
ran, and every run created by a path other than admission, is legacy. Such a run has no manifest and can never get
one (`a manifest needs an existing, non-legacy run`, also raised for a run that does not exist), and the flag
cannot be changed afterwards (`caliber_workflow_runs.manifest_legacy is write-once`), so a historical pin cannot be
fabricated. Admission (#676, #680) creates a run with `manifest_legacy = false` in the same transaction as its
manifest. A client with `DELETE` on runs can still delete a legacy run and re-insert the same id as a non-legacy
one; that needs write access to runs and is not guarded here.

**Retention and deletion.** A run that has a manifest is retained evidence: retention pruning
(`CALIBER_WORKFLOW_RUN_RETENTION_DAYS`) skips it, and `DELETE /workflows/{id}` answers `409`
(`cannot delete workflow: its runs have immutable run manifests`) while any of its runs has one, because removing
the run would either violate the manifest's foreign key (PostgreSQL) or orphan it (SQLite). Legacy and
manifest-less runs are pruned and deleted as before. A rollback (`caliber-migrate downgrade 0113`) drops the table
and therefore the manifests, never the runs; upgrading again starts with no manifests and marks every existing run
legacy.

## Run principals: subject and workload

A run acts as two distinct principals, defined as immutable values in `caliber/src/caliber/policy/principal.py`
(B03.1, #657). The **subject** is who started it: `user_id`, `auth_method` (`session`, `pat` or `trusted_header`)
and, as server-side context only, the project. The **workload** is what runs: a name and one workflow-version
digest. The workload id is `wl_` plus the full 64-hex SHA-256 digest; it is derived, never chosen, and a manifest
that carries any other id is refused. Only `user_id` and `auth_method` are written for the subject: a `project_id`
in a manifest `subject` is refused. The types are pure; persisting them (#680) and resolving them for a run
(#684) come later, so this page lists no setting for them.

The types are stricter than the manifest schema, which accepts any non-empty string: a workload digest must be a
full bare SHA-256, and an id, name or grant id may not contain control, format or line-separator characters. A
`user_id` that `caliber.auth` would treat as anonymous (`anonymous`, any case, or one containing a comma) is
refused, and so is any scope `caliber.auth` does not define. A `pat` subject becomes an identity only with its
`credential_id` (and the token's `credential_project_id` when it is bound), so a project binding is never dropped.

Refusals raise `PrincipalError`; the recovery for each:

| refusal | recovery |
| --- | --- |
| `workload_id must be the one derived from the full workflow_version_digest` | build it with `Workload.for_workflow(name, digest)`; never copy an id from elsewhere |
| `workflow_version_digest must be a full lowercase 64-hex SHA-256 digest` | pass the workflow version's whole bare digest, not a prefix, uppercase or `sha256:` form; a version with no digest (a legacy version) cannot be a workload |
| `subject must have exactly ['auth_method', 'user_id']; extra ['project_id'], missing []` | drop `project_id` from the manifest `subject` and pass it as `Subject.from_manifest(data, project_id=...)` |
| `a pat subject needs its credential_id to become an identity` | pass `credential_id` (and `credential_project_id` when the token is bound) to `to_identity` |

## Run grants

A run acts under a grant (B03.2, #664; `caliber/src/caliber/policy/grants.py`, migration `0115`, table
`caliber_run_grants`). It is bound to an exact run and an exact grant id, to the run's subject and workload
([run principals](#run-principals-subject-and-workload)), to a project, to explicit sets of allowed actions and
resources, to the policy version it was issued under and to an expiry. Issuing one is part of admission (#680). This
page lists what the store and the check guarantee:

- **Exact identifiers, no fallback.** Every check names the run id *and* the grant id. There is no "newest grant"
  and no ambient one; a different run, an unknown grant and a mismatch are all `grant_not_found`, so ids cannot be
  probed.
- **Closed vocabulary, explicit sets.** Actions are the names in `caliber.resource_access.PROJECT_ACTIONS` (an unknown
  action cannot be granted; `release.break_glass_apply` is deliberately not in it). Actions and resources match
  exactly: no wildcard, prefix, case folding or ordering by name or risk.
- **A grant never exceeds the subject's role.** Issuing refuses an action the subject's *current* role is not in the
  set for (`PROJECT_ACTIONS[action]`), and every use rechecks the current role (`role_not_allowed`), so a demotion
  narrows a standing grant at once. The project owner counts as `owner` only when the caller supplies live scopes that
  still include the owner-role scopes; without them `owner` is treated as `editor`.
- **Rechecked every time, in this order:** `grant_revoked`, `grant_expired` (the expiry instant itself is expired),
  `not_a_member` (the subject's *current* active membership, or ownership, of the grant's project), `action_not_allowed`
  (not in the grant), `role_not_allowed`, `resource_not_allowed`. A database failure while reading any of it is
  `authority_unavailable`: a denial, never an allow. Datetimes must be timezone-aware (UTC is stored).
- **Revocation and membership changes cannot be raced (PostgreSQL).** A check takes a row lock on the grant and a
  shared lock on the membership and project rows it relied on, in the caller's transaction; a revocation, a member's
  removal and a demotion each wait for a check in flight, and a check that starts after one sees it. Under REPEATABLE
  READ or SERIALIZABLE a change committed after the transaction's snapshot is `authority_unavailable`. Run the
  transition a check guards in the same transaction as the check. **SQLite gives no such guarantee** (the driver does
  not even begin a transaction before the first write); it is development only and the reference profile refuses it.
  SQLite's `INSERT OR REPLACE` also bypasses the triggers below (a development-only gap).
- **Immutable but for a whole revocation, in the database.** Triggers refuse changing any column but the three
  revocation fields (`caliber_run_grants is immutable except for revocation`), refuse any change once revoked (`a
  revoked grant stays revoked`) and refuse `DELETE` (`TRUNCATE` too on PostgreSQL). A CHECK makes a revocation whole:
  `revoked_at`, `revoked_by` and `revoke_reason` are all null or all set. The ORM refuses the same
  (`ImmutableRecordError`), including editing a committed revocation. As with run manifests, run the application as a
  role without `DELETE` or `TRUNCATE` on the table; the profile does not check which role it uses.

Refusals and what to do:

| code | meaning | recovery |
| --- | --- | --- |
| `grant_not_found` | no grant with that exact run id and grant id | use the ids admission returned for this run; never look one up by recency |
| `grant_revoked` | the grant was revoked | request a new run or a new grant through admission |
| `grant_expired` | `expires_at` has passed | the run must be re-admitted; a grant is never extended |
| `not_a_member` | the subject is no longer an active member (or owner) of the project | restore the membership, or stop the run |
| `action_not_allowed` | the action is not in this grant | request a grant that names it (it must be in the vocabulary) |
| `role_not_allowed` | the subject's current role is not allowed to perform the action | change the role deliberately, or use a different action |
| `resource_not_allowed` | the resource is not in this grant | request a grant that names the exact resource |
| `authority_unavailable` | the store could not answer | retry after the database recovers; nothing was authorized |

Issuing refuses (`GrantError`): an empty or oversized set, an action outside the vocabulary or beyond the subject's
role, a subject who is not a current member, a run that does not exist or belongs to another project, a naive datetime,
and an expiry that is not in the future.

This task stores and checks grants; it makes no policy decision and records none (B03.3 and B03.5). A rollback
(`caliber-migrate downgrade 0114`) drops the table and therefore the grants, never the runs.

## Experiment ownership

In the reference profile CALIBER, not the first editor, decides who owns an MLflow experiment
(`caliber/src/caliber/observability/experiment_registry.py`; B11.registry, #665; migration `0118`, table
`caliber_experiment_owners`, keyed by the tracking backend and the canonical experiment id). Registering an agent,
creating or updating a workflow, or publishing from the assistant with an experiment reference:

- creates the experiment server-side, tagged with its owner, when it does not exist (`provenance=created`);
- refuses an experiment that already exists but nobody registered (`409`), unless an administrator is registering
  it, who may adopt it (`provenance=adopted`); and refuses a numeric id that names no experiment;
- refuses an experiment another project or user owns (`409`), whichever alias (name or id) is used, even when two
  requests race (the primary key admits one row);
- fails closed (`503`) when the tracking backend cannot answer, and registers nothing.

The authorization check (project write access) runs *before* the claim, so a request that is refused creates
nothing. The registry row shares the request's database transaction (a rollback leaves no owner), but the MLflow
experiment does not: if a request fails after CALIBER created the experiment, the experiment stays, carrying the tags
CALIBER wrote (`caliber.owner_kind`, `caliber.owner_id`, `caliber.created_by`). The scope those tags name may register
it on retry; nobody else may (an experiment without those tags is an existing one, refused as above). MLflow's own
API is not scoped, so anyone who can reach it can still create or tag experiments directly.

For data from before the registry, an administrator runs `POST /observability/experiment-claims/migrate` once: an
experiment claimed by exactly one scope becomes that scope's, one claimed by two or more is quarantined, and the
report lists `registered`, `quarantined`, `skipped_existing`, `mismatched` (already registered, but a legacy claim
names another scope too) and `unresolved`. It is safe to run again, and to run twice at once. Each quarantine is
settled with `POST /observability/experiment-claim-conflicts/resolve` (the owner is a project that exists, a user, or
`public` with no id; one administrator wins a race, the other gets `404`). A resolved experiment is visible to its
owner (under any alias it wrote, and without any text claim) and hidden from the losing scope even though that scope's
old agent or workflow claim still names it; it no longer appears in the conflicts listing. The SDK has
`migrate_experiment_claims()` and `resolve_experiment_conflict()`. Details of aliases, quarantine and visibility are in
the [observability architecture](../09-observability/architecture.md). Development keeps the earlier behaviour: an
unregistered experiment goes to whoever binds it first, and a name that resolves to nothing stays a plain reference.

Known limits: the registry's key includes the tracking backend's URI (credentials and query dropped, scheme and host
lower-cased, trailing slash removed), so repointing CALIBER at another server starts a new registry and a default
per-directory `file:` store differs by working directory; use a tracking server. Fleet, prompt-target and
skill-target agents that CALIBER names itself are not registered until an administrator runs the migration. There is
no release or transfer path: deleting the agent or workflow does not free the experiment (an administrator cannot
reassign an *active* owner yet).

### Who may read a trace's payload (B11.4a, #672)

Seeing that a trace exists (it is listed, counted, its metadata shown) follows the experiment's visibility. *Reading
what is in it* (span inputs, outputs and tool arguments, which can hold personal data) is a separate, stricter
permission, `trace.payload.read`, held by the project roles **owner** and **editor**; a reviewer or viewer is refused.
`caliber.observability.trace_access.can_read_payloads(session, identity, experiment)` decides it:

- the experiment's owner is the registry's active row (a quarantined row means no owner) or, before the legacy
  migration, the single scope all agent and workflow claims name (none, or two scopes, means no owner);
- a **project** owner: the caller's current membership role must allow the action
  (`decide_project_access(..., "trace.payload.read")`); demotion or removal takes effect on the next call, and
  `caliber.admin` is not an implicit project role, so an administrator who is not a member is refused;
- a **user** owner: only that user. **Public**, no owner, a project that no longer exists, an empty reference and a
  tracking backend that cannot answer are all refused: the function returns `False`, never raises, never guesses.

The permission is declared (and reserved in the route-scope inventory) by this task; the redaction routes (B11.4b)
are the callers that enforce it, so no existing route changes behaviour yet.

## The execution-owner lease

In the reference profile exactly one process holds the execution-owner lease and only the holder runs the background
loops ([ADR 0002](../architecture/adr/0002-execution-ownership.md)). B13.2 (#668; `caliber/src/caliber/ownership.py`,
migration `0116`, table `caliber_execution_owner`) is the lease and its fencing token; the lifespan wiring that makes
the loops obey it is B13.3 (#673, below). What the lease guarantees:

- **Database time.** Expiry is decided by the database's clock (`clock_timestamp()`, so a long transaction does not
  see a lapsed lease as live) inside the statement that tests it; a process's own clock never extends a lease.
- **One owner.** `acquire` returns the lease or `None`. It is a *new acquisition* only: when the lease has expired (or
  never existed) the token grows by exactly one (the first is 1); while a lease is held and unexpired the answer is
  `None` for everyone, **including the holder's own `owner_id`**, so a restart that reuses a stable id cannot adopt
  a paused predecessor's token. The holder extends its lease with `renew(lease)`, which keeps the token.
- **Exact fencing.** `renew`, `release` and `assert_fenced` compare the owner id *and* the captured token for
  equality, and an unexpired lease; there is no "token is older than the latest" test and no fresh read of a current
  token. A stale or future token, another owner's, or another lease name's, is refused (`LeaseLost`).
- **Fenced transitions.** `assert_fenced` runs in the caller's transaction before an owner-only transition and, on
  PostgreSQL, holds a row lock until that transaction ends, so a takeover waits for the transition rather than
  overtaking it (the check happens under a valid lease and the transition is serialized before any takeover; the
  commit itself may land after the database-time expiry). **SQLite gives no such guarantee**; it is development only.
- **Isolation.** Written for READ COMMITTED (PostgreSQL's default): a contended `acquire` blocks and then returns
  `None`. Under REPEATABLE READ or SERIALIZABLE a contended `acquire`, or a fence racing a heartbeat committed after
  the snapshot, raises `LeaseLost` (fails closed).
- **Fails closed.** An unavailable lease store is `LeaseLost` (every database error is reported that way, programming
  errors included: log its `__cause__`), not a lease. A heartbeat that arrives after expiry extends nothing and never
  resurrects the lease; the process must acquire again and gets a new token.
- **Bounds (`LeaseError`).** `ttl_seconds` from 1 to 3600; `owner_id` at most 128 characters; the lease name at most 64.

Guidance for the loop wiring: renew every `ttl / 3` at most; keep fenced transactions much shorter than `ttl`
minus the renewal interval (a fenced transaction blocks the owner's own renewal and every other fenced transition);
give fenced and acquiring transactions a `SET LOCAL lock_timeout`, since a stuck session holding the row would block
failover (`idle_in_transaction_session_timeout` bounds it too).

A rollback (`caliber-migrate downgrade 0115`) drops the table; the next acquirer starts at token 1.

### Who runs the loops (B13.3, #673)

In the reference profile the loops start **only while this process holds the lease** and stop when it is lost
(`caliber/src/caliber/execution_role.py`, wired in `server.py::_build_lifespan`):

- **Owner or secondary.** At startup a process makes one attempt to acquire, so its role is known before the first
  request, then ticks every `CALIBER_EXECUTION_LEASE_SECONDS / 3` (default lease 30 s, minimum 3 s). The holder
  renews the exact lease it captured; a process that does not hold it keeps trying to acquire. `/readiness` carries
  `execution_role`: `owner` while this process runs the loops, `secondary` while it only serves requests (the
  event bus and every HTTP route run in both; no loop, webhook dispatcher, scheduler or worker runs in a secondary).
- **Losing the lease fails closed.** The owner keeps a local deadline of two thirds of the lease after the start of
  its last successful renewal. A renewal that fails, finds the lease gone or does not answer by that deadline (a slow
  or hung database included) stops the loops, and the role reads `secondary` from the moment the stop begins; the
  process tries to acquire again on later ticks and a fresh acquisition has a higher token. The lease calls run on
  their own thread, never the loops' threads, and on PostgreSQL carry a `lock_timeout` and `statement_timeout` of a
  third of the lease. If the loops fail to start after an acquisition, the ones that did start are stopped, the lease
  is released and the process stays `secondary` (the error is logged and the next tick tries again).
- **Takeover.** When the owner dies its lease lapses by database time and a secondary becomes the owner within one
  lease plus one tick plus the time to start the loops (the V14 scenario asserts 2 x the lease, SIGKILL included). A
  clean shutdown stops the loops *while it keeps renewing the lease*, then releases it, so the successor starts
  only after the loops have stopped and does not wait for expiry.
- **Heartbeats.** `caliber_worker_heartbeats` therefore lists owner-only workers from one process only (the holder's).
- **Outside the reference profile** there is no lease: the loops start as before when background tasks are enabled
  (role `owner`), and a process with background tasks disabled reports `secondary`.
- **Not yet fenced per transition.** The loops still claim rows themselves, and they stop concurrently (the webhook
  dispatcher first). A loop mid-transition when the lease is lost finishes that transition, bounded by
  `CALIBER_SHUTDOWN_GRACE_SECONDS`, so an old owner's last transition can overlap a new owner's first one by up to
  that grace after a lease loss. Fencing each transition with `assert_fenced` arrives with the outbox (#685).
- **Events raised on a secondary.** HTTP routes on a secondary publish webhooks and alerts (`job.applied`,
  `agent.rolled_back`, SLO incidents) to the in-process event bus, and no dispatcher runs there: with the default
  `CALIBER_WORKFLOW_RUN_EVENT_BACKEND=in_process` those events are not delivered by any process. Use a shared backend
  (`database`, `nats` or `redis`) so the owner's dispatcher sees them until the durable outbox (#685) makes this
  independent of the backend. Live run streams for a run that executes on the owner reach only the owner's
  subscribers on the in-process backend.

## Policy decisions

`caliber.policy.decisions.decide(...)` (B03.3, #671; migration `0117`, table `caliber_policy_decisions`) answers "may
this run do this action on this resource, for this purpose, right now?" with a typed `PolicyDecision` and records it,
allow or deny, immutably (ADR 0003 `policy_decision` v1 plus the run, subject and workload it was asked about; there
are no foreign keys, so a decision about a grant that does not exist is still evidence).

- **Exact identity, no fallback.** The caller supplies `run_id`, `grant_id`, the `Subject` and the `Workload`. A
  different run, an unknown grant, another subject (user, authentication method or project) and another workload each
  deny, and nothing is looked up by recency.
- **One reason, a fixed order:** `grant_not_found`, `subject_mismatch`, `workload_mismatch`, `grant_revoked`,
  `grant_expired`, `not_a_member`, `action_not_allowed`, `role_not_allowed`, `resource_not_allowed` (the
  [run grant](#run-grants) checks, including the current membership and role), then the policy's own:
  `policy_version_mismatch` (the grant was issued under another policy version), `purpose_not_allowed`,
  `explicit_deny`, `unknown_action_risk` and `risk_above_ceiling`. An unavailable store is `authority_unavailable`.
  An allow carries no reason codes.
- **Explicit maps, never ordering.** Each action has an explicit risk number in the policy (every action in
  `resource_access.PROJECT_ACTIONS` must have one; a test fails otherwise), compared with the policy's ceiling.
  `policy-v1` has a ceiling of 4 (risk equal to the ceiling is allowed). Risk 5 means "a person, never a run": project
  administration (archive, restore, members, ownership transfer) and every approval or acceptance
  (`resource.approve`, `release.approve`, `release.quality_signoff`, `change_request.accept`), so no grant can make a
  run approve its own work (Beta keeps a human accountable owner and human protected-action approval, ADR 0004).
  Explicit denies are exact `(action, resource)` pairs. A `Policy` is read-only and type-checked.
- **Replayable.** `input_hash` is the SHA-256 of the canonical JSON of every input (run, grant, subject, workload,
  action, resource, purpose and the live scopes) plus the policy's own digest; the clock is not an input. The same inputs under the same policy hash identically.
- **Transactions.** A decision is flushed, never committed. For initial admission the run, its grant and the decision
  share one uncommitted transaction and the caller commits only after an allow; a denial rolls all of it back. If an
  decision cannot be recorded, the answer is a deny with `persisted = False` (an allow becomes
  `authority_unavailable`; a deny keeps its reason and gains `authority_unavailable`), and
  `PolicyDecision.allowed` is true only for a recorded allow. A decision never touches the run manifest. The record
  is written in a savepoint, so a failed record never poisons the caller's transaction. **Consumers must know:** a
  denied initial admission rolls its decision row back with the transaction, so evidence of a *denial* must be
  written separately (the audit record of B03.5, or a decision made in its own transaction), and on SQLite
  (development only) a savepoint that is the first write of a transaction commits it, so call `decide` after the
  run and grant writes.
- **Callers must not leak identity mismatches.** `decide` records the specific reason (`subject_mismatch` and
  `workload_mismatch` reveal that a grant exists). It is server-internal: any layer that answers a client (HTTP, the
  admission API) must collapse identity and grant-lookup failures into one uniform refusal. `purpose` is stored
  verbatim: it names why, never context or payload.
- **Malformed input is a bug, not a decision.** A blank, oversized or control-character id, action, resource or
  purpose, a naive `now`, or a non-`Subject` raises `DecisionInputError` and records nothing.
- **Immutable.** Triggers refuse `UPDATE` and `DELETE` (`TRUNCATE` too on PostgreSQL) and the ORM raises
  `ImmutableRecordError`; as with run manifests, run the application as a role without those privileges.

Recovery: `grant_*`, `not_a_member`, `role_not_allowed`, `action_not_allowed` and `resource_not_allowed` are as in the
grant table above; `policy_version_mismatch` means the run must be admitted again under the current policy;
`purpose_not_allowed` and `explicit_deny` are policy decisions to change deliberately, not retry; `risk_above_ceiling`
means the action cannot be done by a run (use the human-approval path or a person); `unknown_action_risk` means the
policy lacks a risk for the action and must be extended; `subject_mismatch` and `workload_mismatch` are always a bug
or an attack: fix the caller.

## Not enforced yet

ADR 0001 describes the whole reference shape; this table is what the *process* checks today. Not checked:

- **The shape itself:** one tenant and region, MLflow addressed by `MLFLOW_TRACKING_URI`, an S3-compatible object store, at least two projects, and one real model provider next to the scripted adapter. Readiness reporting for MLflow and the object store is #535. The PostgreSQL version and the `pgvector` extension are checked by the migrations (migration `0060`) and the migration-parity CI job, not by the profile.
- **Origin rules** (#533). ADR 0001 names them, but #533 has no allowed-origins task yet; that rule is not scheduled.
- **The trusted-connector set** (#534), **refusing code-execution paths and the full attack pack** (#535), **a single execution owner** (#536) and **the Apply separation-of-duties flag** (#589).
- **Cookie flags:** `auth_session_cookie_secure` is not checked.
- **The database role:** whether the application connects as a role without `UPDATE`, `DELETE` or `TRUNCATE` on `caliber_run_manifests` (see "Run manifests are written once").

A configuration that passes this page is the minimum for the reference profile, not the whole of it.

## Related

- [Health and readiness](health-and-readiness.md) for what the readiness response contains.
- [Configuration and provider setup](configuration-and-provider-setup.md) for the settings themselves.
