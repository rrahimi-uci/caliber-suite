# Beta fault matrix contract

This is the B16.1 contract for the fourteen fault injections defined in §13 of
the [reference-architecture gap analysis](../architecture/CALIBER_REFERENCE_ARCHITECTURE_GAP_ANALYSIS.md).
The `status` column is deliberately `automated` only when the named deterministic
offline pytest case is collected and passes. It means “the bounded seam is
automated”; it does not claim that a live provider, production deployment, or
independent security review has been exercised.

| ID | Injection | Status | Pytest evidence | Offline oracle | Boundary / remaining evidence |
|---|---|---|---|---|---|
| V01 | Authorized request, proposal, distinct approval, and connector effect | automated | `caliber/tests/egi_scenarios/test_v01_governed_effect.py::test_v01_governed_effect_has_one_verified_accepted_effect_and_complete_causal_chain` | One downstream mutation, receipt, acceptance, causal links, and consistent public IDs | Scripted model and disposable downstream only; no live provider evidence. |
| V02 | Foreign workspace/resource, forged identity header, and excess authority | automated | `caliber/tests/egi_scenarios/test_v02_unauthorized_access.py::test_foreign_workspace_cannot_read_or_replay_any_run_surface` | Project B is refused on run, evidence, causal-links, and replay surfaces | The complete role × workspace × trace/proposal/evidence matrix and independent security review remain outside this case. |
| V03 | Retrieved instructions attempt a destination change or secret disclosure | automated | `caliber/tests/egi_scenarios/test_v03_context_injection.py::test_injected_destination_stays_unexecuted_and_requires_approval` | Attacker-controlled destination remains a pending proposal and produces zero downstream effects | This seam proves no automatic dispatch; a production fail-closed rule that rejects the destination before proposal creation is not claimed here. |
| V04 | Destination/arguments change after approval or approval expiry | automated | `caliber/tests/egi_scenarios/test_v04_stale_approval.py::test_approved_destination_change_requires_a_new_decision` | Old approved request is superseded and cannot be reused for the changed destination | No live connector call is made by this stale-approval case; effect-level destination verification is covered by V11/V01. |
| V05 | Role/grant revocation while waiting, followed by public resume | automated | `caliber/tests/egi_scenarios/test_v05_revocation_while_waiting.py::test_public_resume_after_run_grant_revocation_has_zero_effect` | Current grant is rechecked after resume; dispatch is refused and downstream remains unchanged | Historical decision inspection and every policy-revocation variant are covered by adjacent commit-recheck tests, not all in this public case. |
| V06 | Repeated admission with the same or changed input/release | automated | `caliber/tests/egi_scenarios/test_v06_admission_idempotency.py::test_the_manifest_stores_the_request_digest_and_a_moved_selector_returns_the_original` | Same idempotency key preserves the original run and manifest digest | No paid/live provider call is implied. |
| V07 | Worker kill before send, after commit, or before receipt | automated | `caliber/tests/egi_scenarios/test_v07_restart_exactly_once.py::test_the_owner_loop_finds_a_killed_dispatchs_effect_by_key_and_never_sends_it_again` | Stable effect identity, reconciliation, and exactly one downstream mutation | Process-kill coverage uses the repository’s deterministic disposable downstream harness. |
| V08 | Timeout with a still-running tool thread/process and worker-layer retry | automated | `caliber/tests/egi_scenarios/test_v08_timeout_retry.py::test_worker_retry_with_hidden_lookup_stays_unknown_without_resending` | Repeated lookup remains `unknown`; later reconciliation commits the original effect without a resend | No claim is made about every external orphan-process implementation or provider-specific cancellation behavior. |
| V09 | Duplicate/out-of-order callback or publication after a committed transaction | automated | `caliber/tests/egi_scenarios/test_v09_outbox_inbox.py::test_duplicate_callback_one_transition` | Inbox dedupe and state transition remain single and monotonic | Broker outage and every transport implementation are not live-tested here. |
| V10 | Prompt/skill/knowledge/model/tool/policy changes after admission | automated | `caliber/tests/egi_scenarios/test_v10_pins_unchanged.py::test_policy_version_change_after_admission_denies_the_old_grant` | A grant issued under policy-v1 is denied under policy-v2 rather than silently reused | Existing V10 cases cover other pin families; no live model/provider route is claimed. |
| V11 | False-success HTTP response or wrong target | automated | `caliber/tests/egi_scenarios/test_v11_false_success.py::test_wrong_state_fails_acceptance` | Acceptance rejects a response whose target state is wrong | Provider-specific false-200 variants remain bounded by the disposable connector contract. |
| V12 | Historical replay with unavailable live connector and changed-version fork | automated | `caliber/tests/egi_scenarios/test_v12_offline_replay.py::test_changed_pin_replay_is_explicitly_counterfactual` | Changed pins are separated from the source pins and do not mutate the historical set | Full public fork/evidence replay remains additionally covered in `tests/test_replay_fork.py`; no live connector is used. |
| V13 | Public evaluation, evaluator crash/recovery, stale evidence, and deploy gate | automated | `caliber/tests/egi_scenarios/test_v13_release_evaluation.py::test_public_evaluation_api_queues_and_lists_an_attempt` | Public API creates one project-scoped queued evaluation visible through list/read | Crash recovery is covered by the adjacent V13 cases; stale-evidence apply/deploy and real provider evidence remain release-gate work. |
| V14 | Fresh install, second owner, and unsafe configuration | automated | `caliber/tests/egi_scenarios/test_v14_reference_profile.py::test_fresh_migrated_install_serves_health_and_reports_profile` | Clean migrated fixture answers health and reports the configured profile/execution role | Multi-process takeover and unsafe-config attack coverage are adjacent V14 cases; independent artifact/restore smoke is not claimed. |

## Evidence boundary

All rows are offline and deterministic. They use the repository’s fake/scripted
providers, migrated test databases, and disposable downstream fixtures. No row is
evidence from a live model/provider, a paid run, or a production deployment.

The matrix records the owning scenario seam available in this repository. It does
not silently promote the known gaps into complete beta evidence: the V02 full
role/workspace surface, V03 pre-proposal destination rejection, V08 universal
orphan handling, V13 stale-evidence deploy gating, and V14 independent install/
restore smoke remain explicit follow-up boundaries. Operational staffing,
independent security review, restore smoke, and final release approval are also
external beta gates and are not represented as automated rows here.
