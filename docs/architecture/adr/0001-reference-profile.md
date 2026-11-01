# ADR 0001 — Reference profile

## Status

Accepted, 2026-10-26. Builds on nothing; ADR 0002 (execution ownership), ADR 0003 (control-record schemas) and
ADR 0004 (autonomy levels) cite it.

## Context

CALIBER runs in many shapes: SQLite or PostgreSQL, a dev identity fallback or sessions, egress policy on or off,
simulated or real providers. The EGI program (the reference-architecture implementation of *Building Enterprise
General Intelligence*) makes claims about governed action, safe restart and evidence that only hold for one
concrete shape. Without a named shape, every package decides for itself which deployment it supports, a test can
pass on a configuration nobody runs, and "the platform refuses an unsafe action" has no defined meaning.

`CALIBER_PROFILE=reference` (#570, merged) already refuses to start on eight unsafe settings. This record says
what the reference profile *is*, and which control each class of effect needs in it.

## Decision

**The reference profile is one supported deployment shape. Everything outside it is out of the Beta; the
settings the profile can check are refused at startup, the rest are stated here and enforced as later packages
land.** A configuration that does not match the table below is not a reference deployment, whatever it is called.
The `enforced` column says what `CALIBER_PROFILE=reference` checks today (`caliber/src/caliber/profile.py` is the
source of truth for the list).

| component | value | enforced |
| --- | --- | --- |
| tenant | `local` (one tenant; multi-tenancy is out of the Beta) | not yet: no second tenant can be created, so nothing to refuse |
| region | `local` (one region; regional cells are ADR 0008) | not yet |
| database | PostgreSQL 16 with the pgvector extension | yes for SQLite (`sqlite_database`); the PostgreSQL version and extension are checked by migration `0060` and the migration-parity job |
| experiment tracking | MLflow, external, addressed by `MLFLOW_TRACKING_URI` | not yet (readiness reporting, #535) |
| object store | S3-compatible (MinIO in tests) | not yet (#535) |
| projects | at least two, so isolation between them is always exercised | not yet; the scenario fixtures seed two |
| identity | server-validated sessions, or `trusted_header` only with a proxy secret | yes: `trusted_header_without_proxy_secret`, `dev_fallback_enabled`, `insecure_bootstrap` |
| request integrity | CSRF protection on | yes: `csrf_disabled` |
| egress and connectors | egress policy on; plain-HTTP MCP off; trusted connectors only | yes for `egress_policy_disabled`, `mcp_insecure_http` and `mcp_allowlist_not_in_profile`; the trusted-connector list is #534 |
| approvals | no self-approval | yes: `self_approval_allowed` |
| model adapters | the scripted adapter (`caliber/src/caliber/llm/fake.py`) for deterministic runs, and one real provider | not yet |
| connector | keep: the OpenAPI document-request connector (decided by the D1 experiment, `docs/reports/egi/D1-connector-capability.md`) | not yet (#534) |
| profile switch | `CALIBER_PROFILE=reference` (#570); `CALIBER_FAULT_BARRIERS` must be unset | yes: the switch itself, and `fault_barriers_enabled` (#541) |

Connector: keep — see docs/reports/egi/D1-connector-capability.md. The D1 experiment (#567) found the OpenAPI document-request connector carries a stable key, a precondition and a query by key. The executor contract is now complete for the Beta import path: #711 made retry safety and typed transport state explicit, and #990 added canonical key/version inputs, import-persisted retry/timeout settings, conflict refusal, and per-attempt/replay metadata. Receipt verification remains the separate B05.4 connector responsibility; an executor result alone is not proof that the downstream record matches the proposal.

Every effect class in `docs/architecture/protected-effect-paths.md` has a required control. The control is what the
dispatcher (B04) enforces before the effect leaves the platform:

| effect class | required control |
| --- | --- |
| `read` | a policy decision (allow or deny), recorded |
| `reversible_write` | a policy decision, a persisted proposal and a receipt |
| `irreversible_write` | the same as `reversible_write`, plus an exact-intent approval by a distinct principal (ADR 0004 invariant 1) |
| `export` | the same as `reversible_write`, plus the `export` action in the caller's grant |

An unclassified tool is `irreversible_write` (`effects.inventory.effect_class` fails closed), and a tool name is
trusted only for tools CALIBER ships (`caliber_defined`).

**Unsupported paths are refused, not degraded.** A path the inventory marks `refuse` (code that runs arbitrary
source or imports an arbitrary module, a host-filesystem write, a knowledge-build runner, a runnable export, an
unauthenticated sandbox server) fails in the reference profile with the error code `unsupported_in_reference_profile`,
and the message names the path and the setting that allows it elsewhere. The dispatcher raises it (#520);
`profile.py` supplies the switch. Two module imports are deployment configuration, not model-reachable, and stay
`exempt` in the inventory: the sandbox backend factory (EP-024) and optimizer plugins behind an allowlist (EP-121).
The profile never silently falls back to a weaker control.

### Amendment, 2026-10-31 (#1068): the governed-context refusal is a reference-profile control

B04.2, B04.3 and B04.4 made a protected node of a manifest-backed plan refuse with "requires a governed effect context" in
every profile, while no production code binds a governed context yet (#1051). That contradicted the paragraphs above:
"unsupported paths are refused, not degraded" is a statement about the reference profile (the refusal code is
`unsupported_in_reference_profile`, raised when `config.profile == "reference"`, #774), and "features outside the shape keep
working in `development`" is the Consequences below. The refusal for a missing context is therefore enforced **in the
reference profile only**. Outside it, a protected webhook, API-request, registered-tool or OpenAPI-tool node of a plan with no
bound context uses the legacy guarded path (the `_perform_guarded_effect` ledger for HTTP nodes, the inline call behind the
node's own approval gate for tools). That path is not the exact-intent governed path and makes none of its claims. A bound
context is used in every profile. The reference profile still never falls back to a weaker control. The mechanics, and what
the development path does and does not guarantee, are in `docs/operate/reference-profile.md` ("Profiles and a missing
governed context").

### Amendment, 2026-10-31 (#746): the profile lists no MCP command or host

An MCP stdio command is arbitrary code on the CALIBER host and a remote MCP host is an endpoint nobody reviewed; the dispatcher
admits an MCP *call* (`REFERENCE_CONNECTORS`, `mcp`), never a server. The reference profile therefore fixes
`caliber.profile.REFERENCE_MCP_ALLOWLIST` as an **empty tuple** for the Beta, and `CALIBER_PROFILE=reference` refuses to start
(`mcp_allowlist_not_in_profile`) while `CALIBER_MCP_STDIO_COMMAND_ALLOWLIST` or `CALIBER_MCP_REMOTE_HOST_ALLOWLIST` holds any
entry, the shipped defaults included. Adding an entry to the tuple is a change to this record, made only when the exact
dispatcher contract for that server is supported. A reference deployment consequently reaches no MCP server, the first-party
PostgreSQL database servers (EP-114) included. The development profile never runs the check.

### Amendment, 2026-11-01 (#754): executable code is refused from the compiler's kind registry

ADR 0001 marks a path that runs arbitrary code `refuse` and names the refusal `unsupported_in_reference_profile`. B12.4a (#754)
raises that code, for code execution only, at four boundaries (validation, compile, promotion and runtime entry), reading the
kinds from `caliber.workflows.ir.NodeType` rather than a hand-kept list: `python_code` and `external_app` are the node kinds
that execute code, and every other kind is classified as a tool carrier (`agent`, `tool`) or inert, so a new kind cannot
arrive unclassified (`tests/test_profile_code_refusal.py`). A tool is judged by its execution form: an OpenAPI or MCP tool runs
no registered Python; a registered `python_callable` tool is administrator-registered code, admitted as trusted code only while
it runs in the sandbox child (`CALIBER_REGISTERED_TOOL_SANDBOX_ENABLED`, on by default) and refused when the sandbox is off,
because it would then be imported into the control plane. A `python_code` node is refused although it runs in that sandbox: the
built-in sandbox is a process boundary with ambient filesystem and network authority, and the code is the workflow author's. A
sandbox label is never isolation, and nothing a tool, manifest or registry row carries decides it. Plugin allowlisting is
trusted-code admission, not containment of hostile code, and is #763. The development profile keeps every one of these.
The remaining `refuse` rows of the inventory stay with B04.7 (#774).

## Consequences

- Scenario tests (V01–V35) run on this shape: the "EGI scenarios (PostgreSQL)" job is the one that proves the
  profile; SQLite instances run `development` with the same safe settings and prove only scenario logic.
- Later packages extend the profile's refusal list (origin rules #533, the trusted-connector list #534, readiness
  reporting #535, the Apply separation-of-duties flag #589) by adding a code to `reference_profile_violations`; they do not
  redefine this table.
- Features outside the shape (SQLite, simulated providers as a *release* path, plain-HTTP MCP, in-process
  sandboxes) keep working in `development` and stay out of Beta claims.
- A different shape (another database, a second tenant, a regional cell) needs a new record that supersedes this
  one, not an edit.
