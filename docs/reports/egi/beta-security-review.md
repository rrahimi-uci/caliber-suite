# Beta security review evidence (offline slice)

**Issue:** [#851 — Independent Beta boundary security gate](https://github.com/rrahimi-uci/caliber-suite/issues/851)

**Evidence type:** deterministic, repository-local evidence fragment

**Baseline:** `origin/main` at `60a1145b76fd606b8cdb881d0d5b0c60cc9a266e`

**Status:** ready for independent review; not a security sign-off or a Beta release decision

## Purpose and boundary

This report records the offline evidence already implemented by the repository for the
security surfaces named by #851. It is deliberately limited to claims that have a
clear contract in the code and an existing test oracle. This slice adds no production
code and no new attack-test contract: the named attack classes are already covered by
the public-path EGI scenarios and the unit/integration suites listed below.

The report does not claim that the Beta security gate is complete. In particular,
independent human review, bounded real-provider evidence, and the D5 release decision
remain pending. Those are release controls owned by [#613](https://github.com/rrahimi-uci/caliber-suite/issues/613)
and the external-input work in [#608](https://github.com/rrahimi-uci/caliber-suite/issues/608).

## Reference deployment and threat model

The supported security claim is the reference deployment described by
[ADR 0001](../../architecture/adr/0001-reference-profile.md), with control-record
ownership and fencing from [ADR 0002](../../architecture/adr/0002-execution-ownership.md),
record schemas from [ADR 0003](../../architecture/adr/0003-control-record-schemas.md),
and role constraints from [ADR 0004](../../architecture/adr/0004-autonomy-levels.md).
The scenario fixtures exercise one configured organization with two projects and
multiple users. They use scripted/fake adapters for deterministic execution; this is
offline evidence, not real-provider quality evidence.

### Attacker capabilities

The evidence assumes an attacker may:

- send requests with a forged identity header, use a valid identity from another
  project, or request an action beyond the run's grant;
- supply retrieved text or model-selected tool calls containing instructions that try
  to bypass policy;
- replay, alter, expire, supersede, or race an approval, grant, candidate, or pinned
  input;
- operate or restart a worker at a fault boundary, present a stale owner lease, or
  cause a downstream answer to be lost or ambiguous;
- attempt to use an untrusted connector, executable workflow path, plugin, host path,
  or unclassified tool;
- place secret-shaped input into a run, audit record, trace, provider configuration,
  or exported evidence archive; or
- name a foreign, free-form, malformed, or otherwise unauthorized memory partition.

The attacker is not granted host access, database administrator access, or a secret
outside the interfaces under test. Those are separate deployment and infrastructure
controls, not claims made by this report.

### Protected assets and invariants

The evidence below is organized around these observable invariants:

1. Project, run, trace, and source authority are checked before a protected action is
   dispatched; a denial leaves no forbidden downstream mutation.
2. A protected effect is bound to its persisted proposal, exact intent, authorization,
   approval, receipt, and current execution-owner lease. Stale or mismatched records
   fail closed.
3. An effect whose external outcome is unknown is not resent merely because a worker
   restarted. Recovery queries by the stable key and only a verified downstream state
   can settle it.
4. Retrieved instructions remain data, reference-profile refusal paths are not
   silently downgraded, and memory/evidence access is scope-authorized.
5. Secret values do not appear in audit, trace, error, archive, or provider-resolution
   output. Evidence exports are integrity-checked, redacted for unauthorized viewers,
   and tombstoned after retention expiry.

## Attack and evidence mapping

| Threat surface | Observable attack oracle | Existing evidence |
| --- | --- | --- |
| Project identity, authorization, and traces | Foreign-project access, forged trusted headers, revoked membership/grants, excess authority, and denied-run payloads are refused before dispatch; audit details contain identifiers/reasons but not the input. Trace/cause links bind only persisted rows and do not expose protected payloads. | `caliber/tests/egi_scenarios/test_v02_unauthorized_access.py`; `test_b03_run_identity.py`; `test_b11_causal_links.py`; `test_b11_experiment_ownership.py`; `test_v10_pins_unchanged.py` |
| Context and prompt injection | An instruction embedded in retrieved text is recorded as a data-tier segment and reaches the model as input, while the workflow instruction remains the only instruction-tier segment. | `caliber/tests/egi_scenarios/test_v03_context_injection.py`; `caliber/tests/test_memory_reference_scope.py` |
| Effect bypass and unsafe paths | Unapproved protected tools/connectors, untrusted connector identities, executable/code paths, unallowlisted plugins, unlisted MCP entries, and reference-profile export/build paths are refused or remain proposal-only; the downstream mutation counter stays zero on denial. | `caliber/tests/egi_scenarios/test_b04_dispatcher.py`; `test_b04_refusals.py`; `test_b12_code_refusal.py`; `test_b12_mcp_startup_rule.py`; `test_b12_plugin_refusal.py`; `test_v14_reference_profile.py`; `caliber/tests/test_effect_refusals.py`; `caliber/tests/test_workflow_export_runtime.py` |
| Stale approval, revocation, and separation of duties | Expired, superseded, changed-intent, revoked-grant, revoked-approver, and requester-as-approver cases cannot consume an old decision or dispatch an effect. Commit-time rechecks use current authority and binding facts. | `caliber/tests/egi_scenarios/test_v04_stale_approval.py`; `test_v05_revocation_while_waiting.py`; `test_b06_apply_claim_recheck.py`; `test_b06_apply_distinct_approver.py` |
| Unknown recovery and false success | A worker killed before/after send, a lost response, an unprovable absence, a stale precondition, or a downstream record inconsistent with the receipt does not cause a second mutation or a false completion. Reconciliation is by stable key and acceptance reads the downstream record. | `caliber/tests/egi_scenarios/test_v07_restart_exactly_once.py`; `test_b04_dispatcher.py`; `test_b05_governed_worker_wiring.py`; `test_v11_false_success.py` |
| Execution-owner fencing | A stale owner or worker whose lease is held by another owner cannot claim, dispatch, settle, or start protected work; the current owner can recover the durable state. | `caliber/tests/egi_scenarios/test_b05_governed_worker_wiring.py`; `test_v07_restart_exactly_once.py`; `caliber/tests/test_execution_owner.py` |
| Secret disclosure | A canary stored through the encrypted secret path is not echoed by resolution, logs, traces, provider construction, or metadata; unresolved/revoked references expose the reference or a safe error, not the value. | `caliber/tests/test_secret_canary.py`; `caliber/tests/test_secrets.py`; the redacted-denial case in `caliber/tests/egi_scenarios/test_v02_unauthorized_access.py` |
| Memory scope and refusal | Foreign, free-form, malformed, or caller-selected memory partitions are refused or indistinguishable from missing partitions; writes cannot choose another user's/project's destination, and the strict service does not touch the backend on refusal. | `caliber/tests/test_memory_reference_scope.py`; `caliber/tests/test_memory_tools.py`; `caliber/tests/test_memory_tools_coverage.py` |
| Evidence export integrity, access, and retention | Tampered, duplicate, traversal, symlink, oversized, or malformed archives are rejected; viewer exports verify and redact; expired archives are deleted/tombstoned and the route returns 410. Trace and evaluation bundles retain recomputable digests and provider/judge provenance. | `caliber/tests/test_evidence_export.py`; `caliber/tests/test_evidence_trace.py`; `caliber/tests/test_eval_evidence_bundle.py`; `caliber/tests/test_routes_workflow_runs.py` |

The mapping is evidence of exercised controls, not a claim that every possible attack
variant has been exhaustively tested. The reference-profile and PostgreSQL-specific
rows must execute in the CI job described below before they can support a release
decision.

## Recorded local validation

All commands below were run from the clean worktree for this PR at the baseline SHA
above. `--no-cov` is intentional: this PR changes documentation only, so there is no
affected behavioral code for which a new coverage percentage would be meaningful.

### Offline security, evidence, and scope contracts

```sh
PYTHONPATH="$PWD/caliber/src:$PWD/caliber" \
  /private/tmp/caliber-845-ci-env/bin/python -m pytest \
  caliber/tests/test_evidence_export.py \
  caliber/tests/test_secret_canary.py \
  caliber/tests/test_secrets.py \
  caliber/tests/test_memory_reference_scope.py \
  caliber/tests/test_memory_tools.py \
  caliber/tests/test_memory_tools_coverage.py \
  caliber/tests/test_effect_refusals.py \
  caliber/tests/test_workflow_export_runtime.py \
  caliber/tests/test_evidence_trace.py \
  caliber/tests/test_eval_evidence_bundle.py \
  caliber/tests/test_execution_owner.py \
  caliber/tests/test_trace_project_scoping.py \
  caliber/tests/test_trace_redaction.py \
  -q --no-cov
```

Result: **435 passed, 17 skipped in 151.13s**. The skipped cases are explicitly
PostgreSQL-only owner/concurrency checks because `CALIBER_TEST_POSTGRES_URL` was not
set locally.

### EGI public-path attack scenarios

```sh
PYTHONPATH="$PWD/caliber/src:$PWD/caliber" \
  /private/tmp/caliber-845-ci-env/bin/python -m pytest \
  caliber/tests/egi_scenarios/test_v02_unauthorized_access.py \
  caliber/tests/egi_scenarios/test_v03_context_injection.py \
  caliber/tests/egi_scenarios/test_v04_stale_approval.py \
  caliber/tests/egi_scenarios/test_v05_revocation_while_waiting.py \
  caliber/tests/egi_scenarios/test_v07_restart_exactly_once.py \
  caliber/tests/egi_scenarios/test_v10_pins_unchanged.py \
  caliber/tests/egi_scenarios/test_v11_false_success.py \
  caliber/tests/egi_scenarios/test_b03_run_identity.py \
  caliber/tests/egi_scenarios/test_b04_dispatcher.py \
  caliber/tests/egi_scenarios/test_b05_governed_worker_wiring.py \
  caliber/tests/egi_scenarios/test_b06_apply_claim_recheck.py \
  caliber/tests/egi_scenarios/test_b06_apply_distinct_approver.py \
  caliber/tests/egi_scenarios/test_b11_causal_links.py \
  caliber/tests/egi_scenarios/test_b11_experiment_ownership.py \
  caliber/tests/egi_scenarios/test_b12_code_refusal.py \
  caliber/tests/egi_scenarios/test_b12_mcp_startup_rule.py \
  caliber/tests/egi_scenarios/test_b12_plugin_refusal.py \
  caliber/tests/egi_scenarios/test_v14_reference_profile.py \
  -q --no-cov
```

Result: **202 passed, 355 skipped in 476.71s**. The local run exercises the SQLite /
development-profile side where applicable. The skipped cases include the required
PostgreSQL/reference-profile, multi-process, row-lock, and concurrency variants; no
skipped case is counted as a pass here.

### Documentation and repository checks

The repository CI docs job defines the following offline checks. Their results for
this PR are recorded here after the report is added:

```sh
CALIBER_DOCS_PYTHON=/private/tmp/caliber-845-ci-env/bin/python \
  CALIBER_DOCS_STRICT=1 node caliber/caliber-ui/scripts/sync-docs.mjs
node docs/planning/validate-execution-plan.mjs
node --test docs/planning/test-execution-plan-validator.mjs
python -m pytest \
  caliber/tests/test_docs_generation_contract.py \
  caliber/tests/test_docs_mermaid_contract.py \
  caliber/tests/test_sdk_docs_contract.py \
  caliber/tests/test_cookbook_doc_contract.py \
  caliber/tests/test_cookbook_steps_contract.py \
  caliber/tests/test_design_principles_contract.py \
  caliber/tests/test_ci_published_site_gate_contract.py \
  caliber/tests/test_docs_executable_spec_contract.py \
  --no-cov
git diff --check
```

Results:

- `sync-docs.mjs`: passed; regenerated the committed docs targets with `172 file
  write(s) across 2 target(s)` and left no generated-doc diff.
- `validate-execution-plan.mjs`: passed; `58 epics, 291 tasks, 22 gaps; full
  task/epic/gate DAG passes`.
- `node --test docs/planning/test-execution-plan-validator.mjs`: **61 passed, 0
  failed**.
- The eight docs contract files: **106 passed in 25.77s**.
- `git diff --check`: passed.

## Evidence that remains pending

The following are intentionally not represented as completed by this report:

- **PostgreSQL/reference-profile CI:** `.github/workflows/ci.yml` runs the required
  `EGI scenarios (PostgreSQL)` job with `pytest tests/egi_scenarios --no-cov -q`,
  plus the migration, backend, security, and gate-ledger jobs. A green local SQLite
  run cannot replace those checks.
- **Independent security review:** no implementation author, self-review, Copilot
  handoff, or agent review is an independent human security review. A reviewer who
  did not author this implementation must assess this report, the mapped contracts,
  and any findings; critical/high findings require fixes and reruns.
- **Live-provider evidence:** the scenarios intentionally use scripted/fake providers
  and a disposable local downstream. ADR 0001 keeps real-provider support as a
  separate Beta requirement; no quality, spend, availability, or provider-isolation
  claim is made here.
- **D5 acceptance and release:** #613 owns the candidate-SHA acceptance report,
  install/upgrade/restore evidence, fault matrix, real-provider evidence, independent
  review, unresolved findings, and the pass/hold decision. This file contributes an
  offline security fragment only.

## Conclusion

The repository contains deterministic evidence for the listed project-boundary,
governed-effect, stale-intent, unknown-recovery, owner-fencing, secret, memory, and
evidence-export invariants. The fresh local runs above completed without failures, but
their explicit skips and the pending external controls are material limitations. This
report is therefore ready for independent review and follow-up in #851; it does not
close #851 or certify the Beta release.
