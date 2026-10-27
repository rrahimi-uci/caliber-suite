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

## Not enforced yet

ADR 0001 describes the whole reference shape; this table is what the *process* checks today. Not checked:

- **The shape itself:** one tenant and region, MLflow addressed by `MLFLOW_TRACKING_URI`, an S3-compatible object store, at least two projects, and one real model provider next to the scripted adapter. Readiness reporting for MLflow and the object store is #535. The PostgreSQL version and the `pgvector` extension are checked by the migrations (migration `0060`) and the migration-parity CI job, not by the profile.
- **Request bounds and the secret canary** (#533). ADR 0001 also names origin rules for #533, but #533 has no allowed-origins task yet; that rule is not scheduled.
- **The trusted-connector set** (#534), **refusing code-execution paths and the full attack pack** (#535), **a single execution owner** (#536) and **the Apply separation-of-duties flag** (#589).
- **Cookie flags:** `auth_session_cookie_secure` is not checked.
- **The database role:** whether the application connects as a role without `UPDATE`, `DELETE` or `TRUNCATE` on `caliber_run_manifests` (see "Run manifests are written once").

A configuration that passes this page is the minimum for the reference profile, not the whole of it.

## Related

- [Health and readiness](health-and-readiness.md) for what the readiness response contains.
- [Configuration and provider setup](configuration-and-provider-setup.md) for the settings themselves.
