# ADR 0003 — Control-record schemas

## Status

Accepted, 2026-10-26. Builds on ADR 0001 (the reference profile, whose effect classes the `proposal` record uses)
and ADR 0002 (execution ownership, which names the module that writes each record).

## Context

The EGI program adds records no single package owns: a run's manifest, the proposal of an effect, the receipt
the downstream gives, the policy decision, the approval request and its answer, and the event that announces
each. Packages B02–B06 each write one. Without a shared, versioned definition they diverge, a stored record
cannot be read after its writer changes, and the evidence verifier (B14, #538) has nothing to verify against.
ADR 0002 fixes *who writes* each record; this record fixes *what it contains* and how a change to it is
versioned.

## Decision

**Each control record has one JSON Schema (draft 2020-12) per version, shipped in the wheel under
`caliber/src/caliber/contracts/schemas/<kind>.<version>.json` and applied with
`caliber.contracts.validate_record(kind, payload)`.** The tables below are the contracts in readable form;
`caliber/tests/test_contracts_registry.py::test_adr_0003_matches_the_schemas` fails when a table and its schema
disagree on a field or on whether it is required.

Rules that apply to every kind:

- Every schema sets `additionalProperties: false`: an unknown field is a violation, so a typo cannot be stored.
- A record names its version in `schema_version` (a constant, `v1` today). The CloudEvents envelope cannot: its
  `specversion` is CloudEvents' own and stays `1.0` whatever its attributes do, so it names its schema version in
  the extension attribute `schemaversion`, which is `v1` when absent. A later envelope schema is `v2`, selected
  by `schemaversion: v2`.
- **Versioning.** Adding an optional field keeps the version. Removing a field, retyping one, or making an
  optional field required creates `v2` as a new file; `v1` stays supported until a decision record retires it.
  A record whose version is not supported is rejected with `UnsupportedSchemaVersion`, never read as the nearest
  version. A schema nobody has written a record against yet may still change in place: the `v2` rule protects
  stored records, and none exists before a package's first release. The element shapes of `pins` and the fields
  of `subject` and `workload` are in that position until #517 and #518 write them.
- **Readers before writers.** Because unknown fields are rejected, a build that reads `v1` rejects a record that
  uses an optional field added later. A writer must not emit a new optional field until every reader of that
  kind has been upgraded.
- Timestamps are RFC 3339 `date-time` strings with an offset (`Z` or `+hh:mm`) and a real calendar date and time
  (`2026-02-30T00:00:00Z` is rejected); a naive `datetime` must be given an offset before it is written. Digests are non-empty strings; the digest algorithm is the writer's
  concern (ADR 0002) and is part of the value, for example `sha256:<hex>`.
- `tenant_id` and `region` are `local` in the reference profile (ADR 0001) and are **required** on `run_manifest` and `proposal`
  (`tenantid` on `event_envelope`): a writer states them, it does not leave them to a default.
- A payload is referenced, never inlined: `event_envelope` carries `dataref` and `datahash`, not the data. It is
  a strict subset of CloudEvents: the optional attributes `subject`, `datacontenttype`, `dataschema`, `data` and
  `data_base64` are not carried.
- A record is plain JSON: a set, bytes, `NaN` or `Infinity` anywhere in it is a violation, so the digests that
  cover it are computed over something every reader can reproduce.
- Two rules cross fields: a `receipt` with `verified_by` `query` has a `verified_at` and one with `none` has
  `null`; a `policy_decision` that denies carries at least one reason code.

### run_manifest

RunManifest: what a workflow run was pinned to and who started it.

| field | type | required | note |
| --- | --- | --- | --- |
| `schema_version` | const `v1` | yes | the schema version of this record |
| `manifest_id` | string | yes | unique id of the manifest |
| `workflow_run_id` | string | yes | the run it describes (`caliber_workflow_runs.workflow_run_id`) |
| `tenant_id` | string | yes | `local` in the reference profile (ADR 0001); maps to the existing column `caliber_workflow_runs.tenant_id` |
| `region` | string | yes | `local` in the reference profile; the column is added by #517 |
| `residency` | string | no | where data may be processed; `local` when absent |
| `project_id` | string | no | the project the run was admitted in (server-derived, never the caller's choice); absent for a run outside any project and for records written before #676 |
| `pins` | object | yes | `workflow_version`, `bundle_digest`, `prompt_versions`, `tool_versions`, `skill_versions`, `knowledge_base_versions` (array of `{knowledge_base_id, version_id}`), `model_routes` (list), `policy_version`; all required |
| `subject` | object | yes | `user_id`, `auth_method` |
| `workload` | object | yes | `workload_id`, `name`, `workflow_version_digest` |
| `purpose` | string | yes | why the run exists |
| `legacy` | boolean | no | false when absent; true for a run that predates manifests |
| `rule_bundle_path` | string or null | no | written by #525 |
| `rule_bundle_sha256` | string or null | no | written by #525 |
| `request_digest` | string (64 lowercase hex) or null | no | SHA-256 of the canonical admission intent (workflow, release and version selectors as named, input, purpose, server-derived scope); absent on a manifest written before #687 |
| `grant_id` | string or null | no | the id of the one grant admission issued to the run (#684); a worker names exactly this grant; absent on a manifest written before it existed |
| `created_at` | string (RFC 3339 date-time) | yes | when the manifest was written |

`project_id` (#676), `request_digest` (#687) and `grant_id` (#684) were added to `run_manifest` v1 in place. ADR 0003 requires readers to be upgraded before a writer
emits a new optional field, and this schema is strict (`additionalProperties: false`), so a build that predates the
field would reject a manifest carrying it. The in-place change is acceptable because no
tagged release (`caliber-v0.1.0a1` is the only one) contains `run_manifest`: the first writer (admission, #680) and its
reader (`caliber.run_manifest.read_manifest`) exist only on the development line, so no supported deployment holds a
manifest an older reader must understand. Once a release carries the schema, a new field needs the reader-first
sequence above. Until then a deployment must not run an older development build against manifests a newer one wrote.

### proposal

Proposal: one effect a run asks to perform, before it is performed.

| field | type | required | note |
| --- | --- | --- | --- |
| `schema_version` | const `v1` | yes | the schema version of this record |
| `proposal_id` | string | yes | unique id |
| `workflow_run_id` | string | yes | the run that proposed it |
| `node_id` | string | yes | the workflow node |
| `logical_key` | string | yes | stable key: the same intent in a retry has the same key |
| `intent_hash` | string | yes | digest of the exact intent (target, args, expected version) |
| `effect_class` | enum | yes | `read`, `reversible_write`, `irreversible_write`, `export` (ADR 0001) |
| `target` | object | yes | `connector`, `resource` |
| `args` | object | yes | the arguments of the effect |
| `expected_resource_version` | string or null | yes | the downstream version the proposer saw; null when the target is not versioned |
| `state` | enum | yes | `proposed`, `approved`, `dispatched`, `committed`, `failed`, `unknown`, `reconciled` |
| `tenant_id` | string | yes | `local` in the reference profile |
| `region` | string | yes | `local` in the reference profile |

### receipt

Receipt: what the downstream said about a performed effect.

| field | type | required | note |
| --- | --- | --- | --- |
| `schema_version` | const `v1` | yes | the schema version of this record |
| `receipt_id` | string | yes | unique id |
| `proposal_id` | string | yes | the proposal it answers |
| `downstream_request_id` | string | yes | the request id the downstream assigned or echoed |
| `response_digest` | string | yes | digest of the downstream response |
| `verified_by` | enum | yes | `query` when the platform read the downstream back, `none` otherwise |
| `verified_at` | string (date-time) or null | yes | null when `verified_by` is `none` |

### policy_decision

PolicyDecision: the allow or deny that one action on one resource got.

| field | type | required | note |
| --- | --- | --- | --- |
| `schema_version` | const `v1` | yes | the schema version of this record |
| `decision_id` | string | yes | unique id |
| `grant_id` | string | yes | the grant that was evaluated |
| `action` | string | yes | the action asked for |
| `resource` | string | yes | the resource it was asked of |
| `result` | enum | yes | `allow` or `deny` |
| `reason_codes` | array of string | yes | why; may be empty for an allow |
| `policy_version` | string | yes | the policy that decided |
| `input_hash` | string | yes | digest of the inputs, so the decision can be replayed |
| `evaluated_at` | string (RFC 3339 date-time) | yes | when it was evaluated |

### approval_request

ApprovalRequest: what an approver is asked to approve, bound to the exact intent.

| field | type | required | note |
| --- | --- | --- | --- |
| `schema_version` | const `v1` | yes | the schema version of this record |
| `approval_request_id` | string | yes | unique id |
| `proposal_id` | string | yes | the proposal to approve |
| `intent_hash` | string | yes | must equal the proposal's `intent_hash`; an approval of anything else is void |
| `evidence_hash` | string | yes | digest of the evidence shown to the approver |
| `policy_digest` | string | yes | digest of the policy in force |
| `expected_resource_version` | string or null | yes | the downstream version the approval is valid for |
| `risk_class` | string | yes | the risk class that required approval |
| `requested_by` | string | yes | the principal that requested it |
| `expires_at` | string (RFC 3339 date-time) | yes | after this the request cannot be approved |
| `allowed_responses` | array of enum | yes | a non-empty subset of `approve`, `reject`, `request_changes` |

### approval_decision

ApprovalDecision: the answer to an approval request.

| field | type | required | note |
| --- | --- | --- | --- |
| `schema_version` | const `v1` | yes | the schema version of this record |
| `approval_request_id` | string | yes | the request it answers |
| `decision` | enum | yes | `approve`, `reject`, `request_changes` |
| `decided_by` | string | yes | the deciding principal |
| `decided_at` | string (RFC 3339 date-time) | yes | when it was decided |
| `reason` | string | yes | free text; may be empty |

### event_envelope

EventEnvelope: a CloudEvents 1.0 envelope; the payload is referenced, never inline.

| field | type | required | note |
| --- | --- | --- | --- |
| `specversion` | const `1.0` | yes | CloudEvents version; this is the version field of this kind (schema `v1`) |
| `id` | string | yes | unique per source |
| `source` | string | yes | who emitted it |
| `type` | string | yes | the event type |
| `time` | string (RFC 3339 date-time) | yes | when it happened |
| `tenantid` | string | yes | extension attribute; `local` in the reference profile |
| `correlationid` | string | yes | extension attribute: groups the events of one run |
| `schemaversion` | const `v1` | no | extension attribute: the schema version of this envelope; `v1` when absent |
| `causationid` | string or null | no | extension attribute: the event that caused this one |
| `traceparent` | string (W3C traceparent) or null | no | extension attribute; links to the trace |
| `dataref` | string or null | no | where the payload is stored |
| `datahash` | string or null | no | digest of the payload at `dataref` |

## Consequences

- #517 (run_manifest), #518 (policy_decision), #522 and #625 (proposal, receipt), #523 and #571 (approval_request,
  approval_decision) and #536 (event_envelope) write their records against these schemas and add fixtures under
  `caliber/tests/fixtures/contracts/<kind>/`; the registry test discovers them by file name and needs no edit.
- A later field is added as an optional property of the existing schema and a fixture, not a new kind.
- The database tables that store these records are not defined here; each owning package adds its own migration.
- A package that needs a field this record does not have changes this record's schema (a version bump if it is a
  breaking change) in the same PR as its first use.
