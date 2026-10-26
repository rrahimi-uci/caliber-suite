# Execution-contract correction record

Reviewed baseline: main `9687bc99e19f47c0f9ac19af0da238323cf4848e` (PR #935).
Scope: repair implementation briefs, validation oracles and producer/consumer order.
This is **planning**, not implementation of Beta/MVP features or independent release approval.
The [execution manifest](beta-mvp-execution.json) and live child issues carry the corrected instructions.

## Review findings and binding corrections

| ID | Finding / affected issues | Corrected contract and required proof |
| --- | --- | --- |
| C01 | Critical-slice waiver, #827 | Critical failures always block. Test an otherwise valid active waiver and prove all promotion paths remain denied; only explicitly noncritical controls accept waivers. |
| C02 | Recorded replay used as candidate quality, #820 | Execute the candidate through #812 counterfactual fork with independent run/budget and pinned adapters; two scripted candidates over one corpus must produce their respective outputs. Historical replay cannot qualify new candidate quality. |
| C03 | Principal/grant/approval mismatch, #671/#699/#718; incomplete workload token, #661/#678 | Policy calls carry exact run/grant and original run subject/workload. Check the approving human's current role separately, never substitute that human into another subject's grant. Workload JWT binds issuer/audience/worker/time/jti with fixed algorithm and trusted key; recheck grant at lease redemption. |
| C04 | Signature changes sealed digest, #748/#756 | Keep v1 verification unchanged. A versioned v2 envelope defines content separately from signature/integrity; sign domain-separated version/digest/publisher/key metadata. Ordinary bundle and signature verifiers must both pass; tampering with content or signer metadata fails. |
| C05 | Unknown recovery collapsed into failure, #802/#814 | Reconciliation returns unknown unless a connector proves final noncommit. A404 or unchanged ticket cannot permit redrive. Redrive atomically rechecks current authority, retention, preconditions and definitive proof; delayed commit and concurrency tests prove no blind retry. |
| C06 | Hidden producer cycle, #657/#676/#680/#683/#684/#726/#736; incomplete conformance prerequisites, #777 | #657 owns pure identity types only; #680/#684 own persistence/integration. Move the pure bundle loader #726 to B07/D2 before #676. #736 only consumes admitted pins. Add #686 and #812 to conformance prerequisites. No producer depends on its own consumer. |
| C07 | Unsafe upgrade window, invalid CLI call and ancestor migration edits, #808/#815 plus migration-bearing tasks | Exclusive migration lock stays held through verification; every writer observes maintenance/quiescence. Verify coordinated backup hashes/versions, not filename age alone. Existing CLI is main([target]). Allocate fresh revisions after merged prerequisites and never edit their migration files. |
| C08 | Workflow-manifest API replacement, #695 | Preserve the existing workflow manifest response and legacy snapshot behavior. Add a separate authorized admission-manifest endpoint, SDK method and UI type with explicit legacy absence. Run existing consumers unchanged. |
| C09 | Package digest omits metadata/linkage, #826 | Digest the entire canonical authorized-view manifest, including member identity, metadata, parent links and missing-link declarations. Keep payload hashes as members, not the entire package hash input. Tampering with links must fail even with unchanged bytes. |
| C10 | Reject versus buffer callback contract, #907 | Authenticate and durably deduplicate, buffer gaps, then drain contiguous messages exactly once. Preserve late-after-cancel observations for reconciliation without adopting results or restoring authority; cancellation transport can itself be unknown. |
| C11 | Unregistered paused lifecycle, #819 | Reuse #780 dispatch quarantine metadata; retain existing lifecycle vocabulary and original pins. Revocation blocks fresh reads/effects but allows read-only unknown reconciliation. Test every existing runtime state and public serializer. |
| C12 | Same-author adapter fallback, #921 | Independent-author evidence is mandatory. A local stub can develop the harness but cannot close the certification/release gate. Record author, repository, commit, license and exact conformance digest. |

These corrections replace the old child scope, tests and acceptance criteria; an added parent disclaimer alone
does not resolve contradictory instructions. Generic “read current signatures” guidance is not permission to
make new architecture choices or weaken a settled contract.

## Adjacent consistency repairs

- #700 defines one full versioned intent hash reused by approvals/receipts; connector version and effect class
  cannot disappear from the approval hash.
- #662 uses per-row audit export membership and acknowledged segment state. A numeric sequence high-watermark
  alone can omit a lower-ID transaction that commits later. PostgreSQL two-session fault tests cover that case.
- #786/#793/#801 extend the current deployment with a reference overlay instead of duplicating its services.
- #755/#771 gate UI support labels by actual per-adapter capability; no blanket promotion of MCP/integrations.
- #739 tests persisted authenticated job creators through actual callers rather than asserting source formatting.
- #852 bounds the new learning path without disabling existing workflow calibration.
- #777 uses AgentStepAdapter, schema validation and installed plugin discovery, not the obsolete adapter name
  or string searches for provider objects.

## Execution order and scope

The loader move adds a safe independent D2 starting task; it does not move governed context integration ahead
of authority. Native parent/dependency links, milestone and board fields must match the manifest after this move.
All58 epics,280 tasks and22 report gaps remain represented; no new feature or duplicate issue is needed.
Single-tenant scope and D5 Beta / D9 core proof / D15 full retained MVP gates are unchanged.
Calendar, actual staffing, domain thresholds and independent participants remain under #608.

## Validation and limitations

Run from the repository root:

```sh
node docs/planning/validate-execution-plan.mjs
node --test --experimental-test-coverage docs/planning/test-execution-plan-validator.mjs
node docs/planning/validate-execution-plan.mjs --live
git diff --check
```

The offline tests include negative mutations for the reviewed task instructions/oracles and missing producer
edges; CI runs them without GitHub credentials. Live validation additionally compares scope, files, tests,
acceptance, documentation and native relationships/board fields. The correction rules have measured unit-test
coverage; this is not runtime-feature coverage. Phrase assertions prevent the known regressions, but cannot
prove arbitrary prose correct, certify every future implementation or replace architectural review.

The review's two code diagnostics reproduced post-seal signature digest mismatch and rejection of the proposed
upgrade CLI arguments. Those are evidence for correcting future instructions, not runtime fixes made here.
No new release, deployment, live-provider trial or independent security approval is claimed.
