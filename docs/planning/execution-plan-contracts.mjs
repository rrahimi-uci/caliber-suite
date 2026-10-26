// Regression checks for reviewed planning contracts, not proof of feature implementation.
import assert from "node:assert/strict";

// Each rule checks both the prescribed behavior and its observable test/acceptance oracle.
export const contractRules = [
  [657, "scope", "no database lookup, admission writer or worker integration"],
  [726, "scope", "No context/admission/database integration"],
  [676, "scope", "#726 load_rule_bundle"],
  [684, "scope", "after #683 pin loading"],
  [699, "scope", "run_id=run_manifest.workflow_run_id, grant_id"],
  [718, "scope", "Separately check the actual approving principal"],
  [718, "tests", "approver_checked_separately"],
  [700, "scope", "connector identity/version, effect_class, target, arguments and expected_resource_version"],
  [661, "scope", "iss (configured installation issuer), aud=caliber-credential-broker"],
  [661, "tests", "missing claims"],
  [678, "tests", "revoked_grant_before_redeem"],
  [748, "scope", "v1 remains read-only compatible; v2 canonical content excludes integrity and signature"],
  [748, "tests", "signed_round_trip"],
  [802, "scope", "proven_not_committed or unknown"],
  [802, "tests", "delayed_commit_after_unchanged_read"],
  [814, "tests", "concurrent_redrive_single_claim"],
  [808, "scope", "All API-only peers also enforce maintenance mode"],
  [808, "tests", "two_migrators_one_lock"],
  [815, "scope", "main([target])"],
  [815, "tests", "real_cli_argument_contract"],
  [695, "scope", "Preserve GET /workflow-runs/{run_id}/manifest"],
  [695, "scope", "GET /workflow-runs/{run_id}/admission-manifest"],
  [695, "tests", "legacy_snapshot_preserved"],
  [820, "scope", "#812 authorized counterfactual fork"],
  [820, "tests", "changed_candidate_changes_result"],
  [827, "scope", "regardless of mean score or any waiver"],
  [827, "tests", "critical_blocks_with_active_waiver"],
  [826, "scope", "entire canonical manifest"],
  [826, "tests", "parent_link_tamper"],
  [907, "scope", "buffered durably"],
  [907, "tests", "sequence3_buffered_before2_then_drained"],
  [819, "scope", "do not invent a paused workflow lifecycle status"],
  [819, "tests", "each_current_state_on_revoke"],
  [921, "scope", "No team-authored fallback"],
  [921, "acceptance", "stub or same-author fixture cannot"],
  [662, "tests", "lower_id_commits_after_higher_id_export"],
  [786, "scope", "small reference-profile overlay"],
  [777, "scope", "actual AgentStepAdapter protocol"],
  [771, "tests", "preview_label_retained"],
  [739, "tests", "forged_body_creator_ignored"],
  [852, "tests", "existing_workflow_creation_unchanged"],
];

export const prerequisiteRules = [[676, 726], [684, 683], [777, 686], [777, 812]];

export function validateReviewedContracts(plan) {
  const tasks = new Map(plan.tasks.map((task) => [task.number, task]));
  assert.equal(plan.contract_review?.record, "docs/planning/EXECUTION_CONTRACT_CORRECTIONS.md",
    "missing contract review record");
  for (const [number, field, expected] of contractRules) {
    assert(tasks.get(number)?.[field]?.includes(expected), `reviewed contract #${number} ${field}: ${expected}`);
  }
  for (const [consumer, producer] of prerequisiteRules) {
    assert(tasks.get(consumer).blocked_by.includes(producer), `missing producer #${producer} -> #${consumer}`);
  }
  assert.equal(tasks.get(726).parent, 524, "rule loader must precede D2 admission");
  assert.equal(tasks.get(726).milestone, "D2", "rule loader must precede D2 admission");
  for (const task of tasks.values()) {
    const text = [task.scope, task.files, task.tests, task.acceptance].join("\n");
    assert(!/same migration|migration \(same file as task|(?:columns|tables?) (?:go )?in the task-1 migration file/i.test(text),
      `merged migration rewrite #${task.number}`);
    assert(!/with an active waiver for that slice it passes|or the fallback above|main\(\["upgrade", target\]\)/.test(text),
      `superseded unsafe instruction #${task.number}`);
  }
}
