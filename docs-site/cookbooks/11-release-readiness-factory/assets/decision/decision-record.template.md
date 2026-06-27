<!--
Fill-in go/no-go decision record for SCN-11. Copy this file, replace every
<FILL: ...> placeholder, and keep it with the release as the audit trail.

There is no release-scoring engine: the scores below are computed BY HAND from
the evidence in ../dataset/run-id-manifest.json using ../rubric/release-rubric.json.
Allure is generated externally (`make allure-report`) and only served in-app.

GATES (all three must hold for a go):
  overall_release_score >= 0.90  AND  blocker_count == 0  AND  Allure visible.
-->

# Release Decision Record — <FILL: release tag, e.g. r2026.06.0>

- **Date:** <FILL: YYYY-MM-DD>
- **Prepared by:** <FILL: name / role>
- **Decision:** <FILL: go | no_go>

## Evidence: run-id list

Source: `../dataset/run-id-manifest.json` (filled).

| Scenario | Eval run id | Review queue complete | Critical-slice re-run id | Notes |
| --- | --- | --- | --- | --- |
| SCN-07 support-triage | <FILL: eval_run_id> | <FILL: yes/no> | <FILL: approval-branch run id> | <FILL> |
| SCN-08 incident-response | <FILL: eval_run_id> | <FILL: yes/no> | <FILL: rollback-path run id> | <FILL> |
| SCN-09 workflow-debugger | <FILL: eval_run_id> | <FILL: yes/no> | <FILL: retry/resume run id> | <FILL> |
| SCN-10 judge-certification | <FILL: eval_run_id> | <FILL: yes/no> | <FILL: re-run id> | <FILL> |

## Evidence: Allure report

- **Regenerated (`make allure-report`):** <FILL: yes/no — must be yes>
- **Report URL:** <FILL: e.g. /observability/allure-report/>
- **Loaded in-app (Settings → Allure Report):** <FILL: yes/no — must be yes for a go>

## Rubric scoring (computed by hand)

Weights and per-dimension checklist: `../rubric/release-rubric.json`.

| Dimension | Weight | Status (pass/partial/fail) | Factor | Weighted | Evidence cited |
| --- | --- | --- | --- | --- | --- |
| component_readiness | 0.30 | <FILL> | <FILL: 1.0/0.5/0.0> | <FILL> | <FILL: run ids> |
| workflow_readiness | 0.30 | <FILL> | <FILL> | <FILL> | <FILL: re-run ids> |
| review_coverage | 0.20 | <FILL> | <FILL> | <FILL> | <FILL: review queues> |
| evidence_visibility | 0.20 | <FILL> | <FILL> | <FILL> | <FILL: Allure URL + manifest> |

- **Overall release score:** <FILL: sum of weighted column> (gate: ≥ 0.90)

## Blockers

Each blocker MUST map to its owning scenario (reopen that scenario's gate to fix).

| Blocker id | Summary | Owning scenario | Status (open/fixed/waived) |
| --- | --- | --- | --- |
| <FILL: BLK-1> | <FILL> | <FILL: SCN-0x> | <FILL> |

- **Blocker count (open):** <FILL: integer> (gate: must equal 0)
- _If a GitHub blocker issue was opened via the MCP `create_issue` tool
  (approval-gated), link it here:_ <FILL: issue URL or n/a>

## Waiver log

Record any waiver with an explicit expiry and accountable owner. A waived blocker
is NOT a fixed blocker — it must still be tracked to expiry.

| Blocker id | Reason for waiver | Owner | Expiry (YYYY-MM-DD) |
| --- | --- | --- | --- |
| <FILL or "none"> | <FILL> | <FILL> | <FILL> |

## Decision + rationale

- **Final decision:** <FILL: go | no_go>
- **Gate check:** overall_release_score <FILL> ≥ 0.90? <FILL: yes/no> · blocker_count <FILL> == 0? <FILL: yes/no> · Allure visible? <FILL: yes/no>
- **Rationale:** <FILL: cite the evidence by id/name. If no_go, name the specific failing gate(s) and the owning scenario(s) for each blocker. Do not publish go unless all three gates hold and every gate state traces back to a concrete run id / review answer / Allure result.>
