// Historical renderer and archive integrity helpers. No current issue synchronization.
import assert from "node:assert/strict";

export const markerStart = "<!-- caliber-archive-brief:v1 -->";
export const markerEnd = "<!-- /caliber-archive-brief:v1 -->";
const archiveNotice = "> ARCHIVE ONLY — historical planning snapshot; never publish as current issue scope. Current authority: https://github.com/users/rrahimi-uci/projects/3 and linked Issues.";
function requireArchive(options) {
  assert(options?.archive === true, "Retired current-brief renderer: pass { archive: true } only for historical inspection; use GitHub Project + Issues for current scope.");
}

const bullets = (items) => items.map((x) => `- ${x}`).join("\n");
const numbered = (items) => items.map((x, i) => `${i + 1}. ${x}`).join("\n");
const issueLink = (n) => `[#${n}](https://github.com/rrahimi-uci/caliber-suite/issues/${n})`;

export function releaseFor(plan, milestone) {
  const matches = plan.release_train.filter((r) => r.milestones.includes(milestone));
  assert.equal(matches.length, 1, `ambiguous release for ${milestone}`);
  return matches[0].name;
}

export function pendingInputs(plan, task, phase) {
  return (task.human_inputs || []).filter((x) => !phase || x.phase === phase).map((ref) => {
    const decision = plan.decisions.find((d) => d.id === ref.id);
    assert(decision, `unknown decision ${ref.id}`);
    return { ...decision, phase: ref.phase };
  }).filter((d) => d.status !== "resolved" || !d.evidence?.trim());
}

export function readiness(plan, task, live) {
  if (live.get(task.number)?.state === "CLOSED") return "Done";
  if (pendingInputs(plan, task, "start").length) return "Needs input";
  return task.blocked_by.some((n) => live.get(n)?.state !== "CLOSED") ? "Blocked" : "Ready";
}

export function inputStatus(plan, task) {
  const pending = pendingInputs(plan, task);
  return pending.length ? pending.map((d) => `${d.id}: ${d.phase}`).join(", ") : "None pending";
}

export function epicInputs(plan, epic) {
  const refs = epic.children.flatMap((n) => pendingInputs(plan, plan.tasks.find((t) => t.number === n)));
  return [...new Map(refs.map((d) => [d.id, d])).values()];
}

export function validateBriefs(plan) {
  assert.equal(plan.release_train.map((r) => r.name).join("|"), "Alpha|Preview 1|Preview 2|Preview 3|Beta|Pilot|MVP");
  const ids = new Set(plan.decisions.map((d) => d.id));
  assert.equal(ids.size, plan.decisions.length, "duplicate decision");
  for (const d of plan.decisions) {
    assert(d.owner?.trim() && d.question?.trim(), `incomplete decision ${d.id}`);
    assert(["pending", "resolved"].includes(d.status), `invalid decision status ${d.id}`);
    assert(d.status !== "resolved" || d.evidence?.trim(), `resolved decision lacks evidence ${d.id}`);
  }
  for (const task of plan.tasks) {
    assert.equal(task.release, releaseFor(plan, task.milestone), `release drift #${task.number}`);
    assert.equal(typeof task.release_required, "boolean", `missing release scope #${task.number}`);
    const e = task.execution;
    assert(e?.kind && e.evidence_path, `missing execution contract #${task.number}`);
    for (const field of ["work_units", "commands", "qa", "stop_conditions"]) {
      assert(Array.isArray(e[field]) && e[field].length, `missing ${field} #${task.number}`);
    }
    assert(e.work_units.every((s) => typeof s === "string" && s.trim()), `empty work unit #${task.number}`);
    assert(e.commands.every((c) => c.cwd === "." && c.command?.trim() && c.purpose?.trim()), `invalid command #${task.number}`);
    assert(e.commands.some((c) => c.command === "git diff --check"), `missing diff check #${task.number}`);
    assert(e.test_targets.every((t) => /^(tests|caliber\/tests|sdk\/[^/]+\/tests|caliber\/caliber-ui)\//.test(t.path)
      && /\/test_[^/]+\.py$|\.(test|spec)\.[jt]sx?$/.test(t.path)
      && !t.path.includes("..") && ["existing", "planned"].includes(t.at_audit)), `invalid test target #${task.number}`);
    assert(!/^(that test|the file|the spec|the suite itself|those two tests)/i.test(task.tests), `ambiguous tests #${task.number}`);
    for (const ref of task.human_inputs) {
      assert(ids.has(ref.id) && ["start", "acceptance"].includes(ref.phase), `invalid human input #${task.number}`);
    }
  }
  for (const control of plan.controls) {
    const d = Number(control.milestone.slice(1));
    assert.equal(control.release, releaseFor(plan, control.milestone));
    assert(control.outcome?.trim(), `missing outcome ${control.milestone}`);
    if (d > 1) assert(control.blocked_by.includes(control.issue - 1), `noncumulative gate ${control.milestone}`);
    assert(!control.blocked_by.includes(608), "whole-program staffing must not deadlock D1");
  }
  assert.equal(plan.gates.pilot, 617, "D9 must be the Pilot decision");
}

function dependencyText(plan, numbers) {
  return numbers.map((n) => {
    const task = plan.tasks.find((t) => t.number === n);
    const control = plan.controls.find((c) => c.issue === n);
    const epic = plan.epics.find((e) => e.number === n);
    return `${issueLink(n)} — ${task?.title || epic?.title || `${control.milestone} ${control.release} acceptance`}`;
  });
}

export function renderTask(plan, task, options) {
  requireArchive(options);
  const epic = plan.epics.find((e) => e.number === task.parent);
  const inputs = task.human_inputs.map((ref) => {
    const d = plan.decisions.find((d) => d.id === ref.id);
    return `**${d.id} — ${ref.phase} gate; ${d.status}. ${d.owner}:** ${d.question}${d.evidence ? ` Evidence: ${d.evidence}` : " No approval evidence recorded."}`;
  });
  const e = task.execution;
  return `${archiveNotice}
${markerStart}
## Archived execution brief

This block describes the historical snapshot only. Current scope and acceptance criteria must be read from GitHub Project + Issues. A closed issue is retained delivery history; do not redo it without a new regression or an explicit reopening decision.

**Release:** ${task.release} · **Control:** ${task.milestone} · **Parent:** ${issueLink(task.parent)} · **Package:** ${task.package} · **Priority/lane:** ${task.priority}/${task.lane}
**Release requirement:** ${task.release_required ? "Required for this control" : "Nonblocking maintenance; its own tests must pass before closing"}. **Work type:** ${e.kind}.

## Expected outcome and acceptance criteria

${task.acceptance}

## Dependencies and entry conditions

${task.blocked_by.length ? bullets(dependencyText(plan, task.blocked_by)) : "No producing task dependency; read the named existing interfaces before editing."}

Each producing contract must be merged and available on the task branch. Closed issue state alone does not prove integrated release acceptance. Recheck native blockers and pending start decisions before coding; unresolved acceptance decisions permit only the offline work described below.

## Implementation scope

${task.scope}

## Ordered work units

${numbered(e.work_units)}

Land a coherent independently tested unit per PR when these units span different interfaces. The parent task closes only after all listed acceptance criteria and required evidence are complete. Do not create a second issue for work already tracked by a prerequisite.

## Files, boundaries and shared contracts

${task.files}

**Binding package decision:** ${epic.decision}

Use existing service/route/schema conventions. Add a fresh migration after merged predecessors; never rewrite a shipped migration. Keep contract readers compatible before changing writers. Changes outside the listed owners must be justified by a concrete caller/consumer and reflected in this brief. Coordinate shared models/runtime/server/generated-client edits with the integration owner. No unrelated refactor, new authority service or provider dependency is implied.

## Tests and QA

${task.tests}

${e.test_targets.length ? bullets(e.test_targets.map((t) => `\`${t.path}\` — ${t.at_audit} at the audit baseline.`)) : "This validation task uses the scripts/evidence named above; runtime unit coverage alone cannot certify it."}

${bullets(e.qa)}

Commands run from the repository root after the documented environment setup in AGENT_EXECUTION_GUIDE.md:

${e.commands.map((c) => `\`\`\`sh\n${c.command}\n\`\`\`\n${c.purpose}`).join("\n\n")}

New test paths are deliverables, not a claim that tests already exist. For API/schema/UI/SDK changes, exercise the changed public contract and the old supported consumer. A required service that cannot start is an unexecuted check, not an acceptable skip.

## Human inputs and stop conditions

${inputs.length ? bullets(inputs) : "No task-specific human product decision is pending. Escalate a newly discovered contract contradiction with exact evidence; do not ask for routine implementation permission."}

${bullets(e.stop_conditions)}

## Documentation and completion evidence

${task.documentation}

Supply \`${e.evidence_path}\` with candidate SHA, changed interfaces, exact commands/results, measured affected-code coverage, artifact hashes where relevant, and unresolved inputs. Only control ${issueLink(608 + Number(task.milestone.slice(1)))} owns \`docs/reports/egi/${task.milestone}-acceptance.md\`; a child supplies a fragment. Use the PR template, resolve review findings, and follow AGENTS.md's quality and review gates before any agent merges through the PR. Do not close from a checkbox, simulated external review, or a skipped required check.
${markerEnd}`;
}

export function renderEpic(plan, epic, options) {
  requireArchive(options);
  const children = epic.children.map((n) => plan.tasks.find((t) => t.number === n));
  const inputs = epicInputs(plan, epic);
  return `${archiveNotice}
${markerStart}\n## Archived epic contract\n\n**Release:** ${epic.release} · **Control:** ${epic.milestone} · **Package:** ${epic.package}\n\n${epic.decision}\n\n## Delivery units\n\n${bullets(children.map((t) => `${issueLink(t.number)} — ${t.title}${t.release_required ? "" : " (nonblocking maintenance)"}`))}\n\n${inputs.length ? "## Pending human inputs\n\n" + bullets(inputs.map((d) => `**${d.id} — ${d.owner}:** ${d.question} See child phase before starting work.`)) + "\n\n" : ""}Required children provide implementation evidence; the milestone control owns cumulative acceptance on one exact candidate. Parent/child hierarchy is ownership, not an instruction to wait for unrelated children. Follow each child's explicit producing dependencies, human-input phases, boundaries, tests and QA. Existing closed work remains historical evidence and is not automatically reopened by this audit.\n${markerEnd}`;
}

export function renderControl(plan, control, options) {
  requireArchive(options);
  const release = plan.release_train.find((r) => r.name === control.release);
  const inputs = control.human_inputs.map((id) => plan.decisions.find((d) => d.id === id));
  return `${archiveNotice}
${markerStart}\n## Archived milestone acceptance contract\n\n**${control.milestone}: ${control.release}**. ${release.window}. Windows are conditional planning estimates; #608 records capacity and dated commitments.\n\n## Required outcome\n\n${control.outcome}\n\n## Required predecessors\n\n${bullets(dependencyText(plan, control.blocked_by)) || "Completed D1 foundation evidence and H01 near-term kickoff inputs."}\n\n## Acceptance and required QA\n\n- Every required child delivers its declared outcome, tests and evidence; nonblocking maintenance is explicitly labeled.\n- The preceding D control must have a PASS report. Revalidate affected earlier guarantees on this candidate; closed issue state and stale SHA evidence are insufficient.\n- Run all applicable deterministic unit/integration, PostgreSQL, public API/SDK/CLI, browser, build/install/upgrade/restore, docs and security checks on the exact candidate and artifacts. Missing/skipped required checks produce HOLD.\n- Each Preview reserves week 3 for QA, bug bash, fixes and release validation. An unmet gate delays the release; the next release cannot inherit an unearned PASS.\n- Required independent reviews/trials and human decisions have actual provenance. No open critical/high security or release-blocking defect remains.\n- This issue alone owns docs/reports/egi/${control.milestone}-acceptance.md with SHA/hashes, required/executed test IDs, findings, approvals, scope limits and PASS/HOLD. Retain child fragments and old report provenance.\n- Reforecast remaining work with confirmed capacity before setting dates. D9 is the Pilot release decision; D15 is the retained enterprise MVP decision. Final tags/publication follow PASS and use the already verified artifact bytes.\n\n## Human inputs\n\n${inputs.length ? bullets(inputs.map((d) => `**${d.id} — ${d.status}; ${d.owner}:** ${d.question}`)) : "No additional decision beyond the named predecessor and child requirements."}\n\nThe long-lived #608 decision tracker need not be closed to accept a near-term control; the specific inputs above must be resolved. This issue does not implement features or authorize agents to invent approvals, targets or external test results.\n${markerEnd}`;
}

export function renderDecisions(plan, options) {
  requireArchive(options);
  return `${archiveNotice}
${markerStart}\n## Archived release decision and staffing tracker\n\nThis is a long-lived coordination record. Resolve inputs by the release/task that needs them; do not require all future MVP staffing to close before Preview 1. Missing acceptance inputs block the named trial or release, not independent offline implementation. Dates remain unset until a capacity-based reforecast is approved.\n\n${plan.decisions.map((d) => `### ${d.id} — ${d.owner}\n\n${d.question}\n\nStatus: **${d.status}**. Needed by: ${d.needed_by}. Coding blocked: ${d.start_tasks.map(issueLink).join(", ") || "none"}. Acceptance blocked: ${d.acceptance_tasks.map(issueLink).join(", ") || "none"}. Evidence: ${d.evidence || "not recorded"}.`).join("\n\n")}\n\nA resolution must name the accountable person, decision date, approved scope/thresholds and evidence URL/commit. The manifest synchronization workflow is retired; resolve current decisions in GitHub Project + Issues. Never substitute a role letter, inferred consent or agent-written review for actual approval.\n${markerEnd}`;
}

// Fail explicitly for existing sync callers before they can construct an issue update.
export function managedBody() {
  throw new Error("Retired manifest-to-issue synchronization: preserve live issue scope and evidence. Read and update current planning through GitHub Project + Issues using .agents/ workflows.");
}
