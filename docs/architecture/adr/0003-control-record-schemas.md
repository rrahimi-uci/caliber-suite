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

### context_manifest

ContextManifest (B07.3a, #704): the ordered, labelled context one agent step was given. Written once, by `caliber.context.manifest`,
before the provider sees any of it; stored as canonical bytes with their SHA-256 (the record has no digest field: the digest excludes itself).

| field | type | required | note |
| --- | --- | --- | --- |
| `schema_version` | const `v1` | yes | the schema version of this record |
| `context_manifest_id` | string | yes | unique id of the manifest |
| `workflow_run_id` | string | yes | the run it belongs to |
| `step_request_id` | string | yes | the agent-step request it was assembled for; unique with `revision` per run |
| `revision` | integer (at least 1) | yes | a re-assembly of the same step is a new revision, never an edit |
| `tenant_id` | string | yes | `local` in the reference profile (ADR 0001) |
| `region` | string | yes | `local` in the reference profile |
| `residency` | string | no | `local` when absent |
| `project_id` | string | no | the project the run was admitted in |
| `segments` | array of objects | yes | in the order the provider sees them: `position` (0-based, contiguous), `tier` (`instruction` or `data`), `kind` (`system_instruction`, `workflow_instruction`, `user_input`, `retrieved_text`, `tool_output`, `history`, `memory`), `source` (`kind` of `prompt`, `input`, `knowledge_base`, `tool`, `memory`, `history`, with `knowledge_base_id`, `version_id` or `reference`), `content_digest`, `tokens`, optional `retrieval` (`mode`, `index_version`), `transform` (`name`, `version`) and `authorization_decision_id`. The content itself is not stored here, only its digest. |
| `omitted` | array of objects | no | what was left out: `source`, `reason` (`token_budget`, `unauthorized`, `redacted`, `filtered`, `duplicate`) and `tokens` |
| `redacted` | array of objects | no | segments changed for a policy reason: `position`, `fields`, `reason` |
| `token_allocation` | object | yes | `budget`, `used`, `instruction`, `data`, and optionally `sections`: one `{section, cap_tokens, used_tokens}` per capped section (`retrieved`, `history`), where `used_tokens` equals the tokens of that section's segments and does not exceed `cap_tokens` (B07.4, #719; absent on a manifest written before it) |
| `created_at` | string (RFC 3339 date-time) | yes | when the manifest was written |

Rules beyond field shapes. The schema enforces these: an `instruction` segment comes only from a system or workflow
instruction and every one is `instruction` tier; **a segment's `kind` must match its `source.kind`** (instructions from a
`prompt`, `user_input` from `input`, `tool_output` from `tool`, `history` from `history`, `memory` from `memory`,
`retrieved_text` from a `knowledge_base`), so text cannot be promoted to an instruction by labelling it; retrieved text is
`data` tier; a knowledge-base source names `knowledge_base_id` and `version_id`, a prompt or memory source a `reference`;
and anything loaded from a knowledge base or memory carries the `authorization_decision_id` of the recorded `context.read`
allow that let it be loaded. `caliber.context.manifest` also enforces: `position` equals the segment's index (reordering a
stored record is detectable twice, by the digest and by the positions); `used` is the sum of the segments' tokens and
equals `instruction` plus `data`, within `budget`; a segment is redacted once; and an `omitted` source refused as
`unauthorized` is recorded by `kind` only, never by identifier. Stored records are canonical JSON (one record, one byte
form, one digest) with integer counts.

### proposal

Proposal: one effect a run asks to perform, before it is performed.

| field | type | required | note |
| --- | --- | --- | --- |
| `schema_version` | const `v1` | yes | the schema version of this record |
| `proposal_id` | string | yes | unique id |
| `workflow_run_id` | string | yes | the run that proposed it |
| `node_id` | string | yes | the workflow node |
| `logical_key` | string | yes | stable key of the logical effect: the SHA-256 of `(deployment, run, node, occurrence_id)`. A retry presents the same occurrence and finds the same proposal; a legitimate repeat (a loop iteration) is the next occurrence |
| `intent_hash` | string | yes | `sha256:` and the digest of the canonical intent: connector identity and version, effect class, target resource, arguments and expected resource version (`caliber.effects.records.Intent`, algorithm `intent-v1`). Every approval and receipt reuses this full digest; none computes a reduced one |
| `effect_class` | enum | yes | `read`, `reversible_write`, `irreversible_write`, `export` (ADR 0001) |
| `target` | object | yes | `connector`, `resource` |
| `args` | object | yes | the arguments of the effect |
| `expected_resource_version` | string or null | yes | the downstream version the proposer saw; null when the target is not versioned |
| `state` | enum | yes | `proposed`, `approved`, `dispatched`, `committed`, `failed`, `unknown`, `reconciled` |
| `tenant_id` | string | yes | `local` in the reference profile |
| `region` | string | yes | `local` in the reference profile |
| `occurrence_id` | string | no | the durable occurrence the logical key was built from; absent on a record written before #700 |
| `connector_version` | string | no | the connector's version, part of the intent; absent on a record written before #700 |
| `intent_algorithm` | string | no | the version of the canonical-intent algorithm that produced `intent_hash` (`intent-v1`); absent on a record written before #700 |

`caliber.effects.records` stores a proposal in `caliber_effect_proposals` (B05.1, #700). The same logical key with the same intent is the same proposal; the same key with a different intent is refused (`ProposalConflict`, answered 409) and the stored intent stands. The intent columns are frozen by database triggers; `state` moves only along the seven-state table (B05.2, #705): `proposed` to `approved` or `failed`, `approved` to `dispatched` or `failed`, `dispatched` to `committed`, `failed` or `unknown`, `unknown` to `reconciled`, `reconciled` to `committed` or `failed`, `committed` and `failed` final, with a compare-and-set `version`.

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
| `attempt` | integer | no | the dispatch attempt it answers (B05.4a, #717); absent on a record written before it |
| `logical_key` | string | no | the proposal's logical key: the idempotency key that was emitted, echoed by the downstream |
| `intent_hash` | string | no | the proposal's `intent_hash`, matched against the intent rebuilt from what the downstream stored |
| `connector` | string | no | the connector's identity |
| `connector_version` | string | no | the connector's version |
| `resource` | string | no | the target resource |
| `resource_version` | string or null | no | the resource version the effect produced, as the downstream reports it |

`caliber.effects.receipts` stores a receipt in `caliber_effect_receipts` (B05.4a, #717) only once the platform has read the effect back through the downstream's own query (`verified_by` is `query` for every stored receipt); a claim nobody could confirm is not a receipt and leaves the proposal `unknown`. There is one authoritative receipt per `(proposal, attempt)`, a downstream request id belongs to one proposal only, and a proposal moves to `committed` only with such a receipt (`receipt_id` on the proposal).

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

### agent_step_request

AgentStepRequest (B08.1a, #713): one bounded agent step, asked of an adapter. Built by the platform, never by the provider.
Every identifier below (`step_request_id`, `workflow_run_id`, `node_id`, `occurrence_id`, `grant_id`) follows the rule
`caliber.effects.records` applies to the same fields: not blank, no control characters, no lone surrogates.

| field | type | required | note |
| --- | --- | --- | --- |
| `schema_version` | const `v1` | yes | the schema version of this record |
| `step_request_id` | string (1 to 128) | yes | names one step: the one `(run, node, occurrence, attempt)` it was allocated for, bound to that tuple when the request is made; the `context_manifest` assembled for the step carries the same id |
| `workflow_run_id` | string (1 to 64) | yes | the run the step belongs to |
| `node_id` | string (1 to 128) | yes | the workflow node |
| `occurrence_id` | string (1 to 128) | yes | the durable occurrence of the node (B05.1): allocated and persisted by the caller before the step runs; a retry presents the same one |
| `attempt` | integer (at least 1) | yes | the attempt of this occurrence; the token reservation (B08.4) is keyed by run, node, occurrence and attempt |
| `run_manifest_digest` | string (64 lowercase hex) | yes | SHA-256 of the run's `run_manifest` bytes |
| `context_manifest_digest` | string (64 lowercase hex) | yes | SHA-256 of the `context_manifest` bytes the provider is given |
| `grant_id` | string (1 to 64) | yes | the one grant admission issued to the run (`run_manifest.grant_id`) |
| `limits` | object | yes | `max_turns`, `max_tool_calls` and `max_tokens` (integers from 0 to 2147483647, zero allowed: a step that may not start that work) and `deadline` (RFC 3339 date-time, ASCII digits, at most nine fractional digits, an offset, and no leap second `:60`); all required. The platform enforces them before work and against the usage the provider returns; `caliber.contracts.agent_step.StepLimits.deadline_at` is the deadline as an aware `datetime` (no clock is read) |

### agent_step_result

AgentStepResult (B08.1a, #713): what one step proposed and used. It never claims that an effect happened: there is no `state`,
`receipt` or `approval` field, and an unknown field is a violation. **Everything in it is model output.** Its `effect_class` and
`connector_version` are claims, not facts: a model can call an irreversible write `read`, and a `read` can be approved by policy
alone. The platform's class comes from the protected-effect inventory, and `ProposedAction.bound_to(inventory_effect_class)`
returns the action carrying that class and refuses a differing claim (`ClaimMismatch`, code `claim_mismatch`); B08.2 must call it
before `record_proposal` and must not record an action that is not `platform_bound`.

| field | type | required | note |
| --- | --- | --- | --- |
| `schema_version` | const `v1` | yes | the schema version of this record |
| `step_request_id` | string | yes | repeats the request's. The step is that id and the tuple below together: `check_result_answers_request` refuses a result whose tuple matches but whose id differs, and one whose id matches but whose tuple differs |
| `workflow_run_id` | string | yes | repeats the request's |
| `node_id` | string | yes | repeats the request's |
| `occurrence_id` | string | yes | repeats the request's |
| `attempt` | integer (at least 1) | yes | repeats the request's |
| `stop_reason` | enum | yes | `completed`, `proposed_action`, `max_turns`, `max_tool_calls`, `max_tokens`, `deadline`, `refused`, `error` |
| `proposed_actions` | array of object (at most 1) | yes | the effect the step asks to perform: one element exactly when `stop_reason` is `proposed_action`, none otherwise. **v1 carries at most one proposed action per step.** A logical effect is keyed by `(run, node, occurrence)`, so a second action would share the first's key; the array stays so a later version can carry several, but only after it adds a per-action identity. Each is a ProposedAction (below) |
| `usage` | object | yes | `prompt_tokens`, `completion_tokens`, `total_tokens`, `turns`, `tool_calls` (integers, at least 0; all required) and optionally `cached_prompt_tokens`. `total_tokens` is the figure a budget settles against: it may not be less than `prompt_tokens` plus `completion_tokens` and `cached_prompt_tokens` may not exceed `prompt_tokens` (`caliber.contracts.agent_step` checks both; a schema cannot) |
| `usage_source` | enum | no | `provider` (when absent), `estimated`, `unknown`: where the usage figures come from, so a failed or ambiguous call does not need figures that look genuine. A refused or malformed result is not a release of the step's token reservation: B08.4 keeps a conservative charge |
| `error_code` | string (1 to 64) | no | a short stable lowercase token (`[a-z][a-z0-9_.-]*`); only with `stop_reason` `error` or `refused` |

A **ProposedAction** is the one action shape shared with the `proposal` record: its fields carry the names and meaning they have there, and
`caliber.contracts.agent_step.ProposedAction.intent` is the very `caliber.effects.records.Intent` behind a proposal, so the digest of what a
step proposed is the `intent_hash` every approval and receipt reuses. Its fields: `effect_class` (`read`, `reversible_write`, `irreversible_write`,
`export`; required), `target` (`connector` and `resource`, both required, no other field), `connector_version`, `args` (an object) and
`expected_resource_version` (string or null), all required. A step has no `state`: the proposal that records an action starts `proposed`.
Beyond the `proposal` record, the step contract also bounds sizes (`connector` 128, `connector_version` 64, `resource` 512,
`expected_resource_version` 256 characters) and applies the identifier rule above, as `caliber.effects.records.Intent` does; `proposal` v1
stays as shipped. A `ProposedAction` is immutable (`args` is a read-only deep view, `intent` and `record()` are fresh copies), and two are
equal exactly when their canonical intents are.

**Quiet refusals.** Both agent-step kinds take model-influenced input, so `validate_record` refuses them without echoing it: a message
names the field and the rule it broke (`proposed_actions/0/effect_class: is not one of [...]`), never the offending value, an unexpected
property name or an unbounded amount of text; at most 20 violations are listed, an unsupported version is named only when it is a short
plain token (otherwise by its type), and a record of more than 1 MiB of JSON text (1048576 characters) is refused before it is
validated. An integer field accepts only an integer: `1.0` is not one. No other kind changes.

## Consequences

- #517 (run_manifest), #518 (policy_decision), #522 and #625 (proposal, receipt), #523 and #571 (approval_request,
  approval_decision) and #536 (event_envelope) write their records against these schemas and add fixtures under
  `caliber/tests/fixtures/contracts/<kind>/`; the registry test discovers them by file name and needs no edit.
- A later field is added as an optional property of the existing schema and a fixture, not a new kind.
- The database tables that store these records are not defined here; each owning package adds its own migration.
- A package that needs a field this record does not have changes this record's schema (a version bump if it is a
  breaking change) in the same PR as its first use.
