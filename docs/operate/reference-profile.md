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
  can see; `user_id` must be the caller's own identity, admins included. A request that names no partition acts
  on the caller's own (`user_id` derived from the authenticated identity). A run id alone is refused (`403`).
- **One refusal, no disclosure.** A partition that does not exist, one that belongs to another project or user, and a
  label that names nothing all answer `404 memory partition not found`; no id is echoed.
- **Writes are verbatim.** `POST /memory` requires `infer=false` (`403` otherwise), because LLM-inferred writes create
  durable facts nobody owns; and startup refuses `CALIBER_MEMORY_INFER=true` while memory is enabled (table above).
- **No bulk deletion.** `DELETE /memory` answers `403`: deleting memory needs the lifecycle's retention and legal-hold
  rules.
- **No bypass.** The service layer enforces the same rules (`MemoryLifecycleUnavailableError`), so a caller that does
  not go through the route (the in-run tools build the service directly) cannot do what the route refuses.

The development profile keeps the previous behaviour.

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

## Not enforced yet

ADR 0001 describes the whole reference shape; this table is what the *process* checks today. Not checked:

- **The shape itself:** one tenant and region, MLflow addressed by `MLFLOW_TRACKING_URI`, an S3-compatible object store, at least two projects, and one real model provider next to the scripted adapter. Readiness reporting for MLflow and the object store is #535. The PostgreSQL version and the `pgvector` extension are checked by the migrations (migration `0060`) and the migration-parity CI job, not by the profile.
- **Request bounds and the secret canary** (#533). ADR 0001 also names origin rules for #533, but #533 has no allowed-origins task yet; that rule is not scheduled.
- **The trusted-connector set** (#534), **refusing code-execution paths and the full attack pack** (#535), **a single execution owner** (#536) and **the Apply separation-of-duties flag** (#589).
- **Cookie flags:** `auth_session_cookie_secure` is not checked.

A configuration that passes this page is the minimum for the reference profile, not the whole of it.

## Related

- [Health and readiness](health-and-readiness.md) for what the readiness response contains.
- [Configuration and provider setup](configuration-and-provider-setup.md) for the settings themselves.
