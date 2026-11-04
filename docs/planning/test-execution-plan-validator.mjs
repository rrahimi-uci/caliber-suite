import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test } from "node:test";
import { contractRules, prerequisiteRules, validateReviewedContracts } from "./execution-plan-contracts.mjs";
import { markerStart, markerEnd, releaseFor, pendingInputs, readiness, inputStatus,
  validateBriefs, renderTask, renderEpic, renderControl, renderDecisions, managedBody } from "./execution-briefs.mjs";

const source = fs.readFileSync(new URL("validate-execution-plan.mjs", import.meta.url));
const baseline = JSON.parse(fs.readFileSync(new URL("beta-mvp-execution.json", import.meta.url)));

test("every execution brief renders its scope, outcomes and dependency links", () => {
  validateBriefs(baseline);
  for (const task of baseline.tasks) {
    const body = renderTask(baseline, task);
    for (const text of [task.scope, task.acceptance, task.files, task.tests, task.execution.evidence_path, markerStart, markerEnd]) assert(body.includes(text));
    for (const n of task.blocked_by) assert(body.includes(`[#${n}]`));
    assert.equal(body.split(markerStart).length, 2);
  }
  for (const epic of baseline.epics) assert(renderEpic(baseline, epic).includes(epic.decision));
  for (const control of baseline.controls) assert(renderControl(baseline, control).includes(control.outcome));
  assert(renderDecisions(baseline).includes("H16"));
});

test("readiness distinguishes producing blockers, start input and acceptance input", () => {
  const p = structuredClone(baseline);
  const task = p.tasks.find((t) => t.number === 1086);
  const live = new Map(task.blocked_by.map((n) => [n, { state: "CLOSED" }]));
  assert.equal(readiness(p, task, live), "Needs input");
  assert.match(inputStatus(p, task), /H04: start/);
  live.set(task.number, { state: "CLOSED" });
  assert.equal(readiness(p, task, live), "Done");
  live.delete(task.number);
  task.human_inputs[0].phase = "acceptance";
  assert.equal(readiness(p, task, live), "Ready");
  task.blocked_by.push(999999);
  assert.equal(readiness(p, task, live), "Blocked");
  const d = p.decisions.find((d) => d.id === "H04");
  d.status = "resolved";
  assert.equal(pendingInputs(p, task).length, 1, "status without evidence is not approval");
  d.evidence = "Owner decision at recorded SHA";
  assert.equal(inputStatus(p, task), "None pending");
  assert(renderTask(p, { ...task, blocked_by: [] }).includes(d.evidence));
});

test("managed updates preserve progress text and are idempotent", () => {
  const old = "Completed migration; remaining review is pending.";
  const brief = renderTask(baseline, baseline.tasks[0]);
  const once = managedBody(old, brief);
  assert(once.includes(old));
  assert.equal(managedBody(once, brief), once);
  const changed = managedBody(once, `${markerStart}\nnew contract\n${markerEnd}`);
  assert(changed.includes(old));
  assert(!changed.includes(baseline.tasks[0].scope));
  assert.throws(() => managedBody(`${markerStart} broken`, brief), /unterminated/);
});

for (const [name, mutate, expected] of [
  ["missing cumulative gate", (p) => { p.controls[1].blocked_by = p.controls[1].blocked_by.filter((n) => n !== 609); }, /noncumulative/],
  ["whole-program staffing gate", (p) => p.controls[0].blocked_by.push(608), /deadlock/],
  ["missing control outcome", (p) => { p.controls[0].outcome = ""; }, /missing outcome/],
  ["unknown input", (p) => p.tasks[0].human_inputs.push({ id: "UNKNOWN", phase: "start" }), /human input/],
  ["false approval", (p) => { p.decisions[0].status = "resolved"; }, /lacks evidence/],
  ["empty unit", (p) => { p.tasks[0].execution.work_units = [""]; }, /empty work unit/],
  ["missing commands", (p) => { p.tasks[0].execution.commands = []; }, /missing commands/],
  ["missing release scope", (p) => { delete p.tasks[0].release_required; }, /release scope/],
  ["wrong release", (p) => { p.tasks[0].release = "MVP"; }, /release drift/],
  ["unsafe target path", (p) => p.tasks[0].execution.test_targets.push({ path: "../secret", at_audit: "existing" }), /test target/],
]) test(`handoff rejects ${name}`, () => {
  const p = structuredClone(baseline); mutate(p); assert.throws(() => validateBriefs(p), expected);
});

test("release lookup and input lookup fail explicitly; empty optional input is safe", () => {
  assert.throws(() => releaseFor(baseline, "D99"), /ambiguous release/);
  assert.throws(() => pendingInputs(baseline, { human_inputs: [{ id: "unknown" }] }), /unknown decision/);
  assert.deepEqual(pendingInputs(baseline, {}), []);
  const p = structuredClone(baseline);
  p.decisions[0].evidence = "named owner decision";
  p.decisions[0].start_tasks = [1086];
  assert(renderDecisions(p).includes("named owner decision"));
  const control = { ...p.controls[0], blocked_by: [], human_inputs: [] };
  assert(renderControl(p, control).includes("near-term kickoff"));
});

test("task CLI returns the exact handoff and rejects absent/unknown/extra arguments", () => {
  const cli = new URL("render-task-brief.mjs", import.meta.url).pathname;
  const task = baseline.tasks[0];
  const result = spawnSync(process.execPath, [cli, String(task.number)], { encoding: "utf8" });
  assert.equal(result.status, 0);
  assert.equal(result.stdout.trim(), renderTask(baseline, task));
  for (const args of [[], ["999999"], [String(task.number), "extra"]]) {
    const result = spawnSync(process.execPath, [cli, ...args], { encoding: "utf8" });
    assert.equal(result.status, 2); assert.match(result.stderr, /Usage:/);
  }
});

function liveFixture() {
  const connection = (numbers) => ({ nodes: numbers.map((number) => ({ number })), pageInfo: { hasNextPage: false } });
  const issue = (number, data, body, kind) => ({ number, title: data.title, state: "OPEN", body,
    milestone: { title: `${data.milestone} — fixture` }, parent: null,
    labels: { nodes: [kind, `priority-${(data.priority || "P0").toLowerCase()}`,
      `stage:${Number(data.milestone.slice(1)) <= 5 ? "beta" : "mvp"}`,
      `release:${data.release.toLowerCase().replaceAll(" ", "-")}`].map((name) => ({ name })) },
    blockedBy: connection(data.blocked_by || []), subIssues: connection(data.children || []) });
  const issues = baseline.tasks.map((t) => ({ ...issue(t.number, t, renderTask(baseline, t), "task"), parent: { number: t.parent } }));
  issues.push(...baseline.epics.map((e) => ({ ...issue(e.number, e, renderEpic(baseline, e), "epic"),
    parent: { number: ({ 589: 523, 592: 553, 594: 542, 595: 540 })[e.number] || 608 + Number(e.milestone.slice(1)) } })));
  issues.push(...baseline.controls.map((c) => issue(c.issue, c, renderControl(baseline, c), "control")));
  issues.push(issue(608, { milestone: "D1", release: "Preview 1" }, renderDecisions(baseline), "coordination"));
  const live = new Map(issues.map((i) => [i.number, i]));
  const items = baseline.tasks.map((t) => ({ content: { number: t.number }, stage: t.stage, release: t.release,
    "human input": inputStatus(baseline, t), priority: t.priority, lane: t.lane, "item type": "Task",
    "execution order": t.execution_order, "gap IDs": t.gaps.join(", "), readiness: readiness(baseline, t, live) }));
  items.push(...baseline.epics.map((e) => ({ content: { number: e.number }, release: e.release })),
    ...baseline.controls.map((c) => ({ content: { number: c.issue }, release: c.release })));
  return { issues, board: { items }, milestones: [...baseline.controls.map((c) => ({ title: `${c.milestone} — fixture`, due_on: null })), { title: "retired", state: "closed" }] };
}

async function inspectFixture(snapshot, mode = "snapshot") {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "caliber-live-plan-fixture-"));
  try {
    const file = path.join(directory, "snapshot.json");
    fs.writeFileSync(file, JSON.stringify(snapshot));
    if (mode === "live") {
      // An isolated fake GitHub executable exercises pagination/CLI parsing without network access.
      const stub = `#!${process.execPath}\nconst fs=require('node:fs');const s=JSON.parse(fs.readFileSync(${JSON.stringify(file)},'utf8'));const a=process.argv.slice(2);let r;if(a[0]==='project')r=s.board;else if(a.includes('graphql')){const q=JSON.parse(fs.readFileSync(0,'utf8'));const start=q.variables.cursor?200:0;const nodes=s.issues.slice(start,start+200);r={data:{repository:{issues:{nodes,pageInfo:{hasNextPage:start===0,endCursor:'page2'}}}}};}else r=[s.milestones];process.stdout.write(JSON.stringify(r));\n`;
      fs.writeFileSync(path.join(directory, "gh"), stub, { mode: 0o755 });
    }
    const args = mode === "live" ? ["--live"] : mode === "offline" ? [] : ["--snapshot", file];
    const result = spawnSync(process.execPath, [new URL("validate-execution-plan.mjs", import.meta.url).pathname, ...args], {
      encoding: "utf8", env: { ...process.env, PATH: `${directory}${path.delimiter}${process.env.PATH}` },
    });
    if (result.status !== 0) throw new Error(result.stderr);
  } finally {
    fs.rmSync(directory, { recursive: true });
  }
}

test("live plan checks accept a complete offline fixture through snapshot and paginated CLI", async () => {
  await inspectFixture(liveFixture(), "offline");
  await inspectFixture(liveFixture());
  await inspectFixture(liveFixture(), "live");
});

for (const [name, mutate, error] of [
  ["missing native blocker", (s) => { s.issues.find((i) => i.number === 823).blockedBy.nodes = []; }, /native blockers/],
  ["unexpected gate blocker", (s) => { s.issues.find((i) => i.number === 609).blockedBy.nodes.push({ number: 608 }); }, /gate #609/],
  ["stale brief", (s) => { s.issues[0].body = "old scope"; }, /brief drift/],
  ["wrong epic release", (s) => { s.board.items.find((i) => i.content.number === 517).release = "MVP"; }, /board release/],
  ["conflicting label", (s) => { s.issues[0].labels.nodes.push({ name: "release:mvp" }); }, /release labels/],
  ["missing human flag", (s) => { s.board.items.find((i) => i.content.number === 1086)["human input"] = "None pending"; }, /human input/],
  ["invented ready state", (s) => { s.board.items.find((i) => i.content.number === 1086).readiness = "Ready"; }, /readiness/],
  ["unapproved due date", (s) => { s.milestones[0].due_on = "2026-12-01"; }, /unapproved date/],
]) test(`live checks reject ${name} offline`, async () => {
  const snapshot = liveFixture(); mutate(snapshot); await assert.rejects(inspectFixture(snapshot), error);
});

function run(change) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "caliber-plan-validator-"));
  try {
    const fixture = structuredClone(baseline);
    change(fixture);
    fs.writeFileSync(path.join(directory, "validate-execution-plan.mjs"), source);
    fs.copyFileSync(new URL("execution-plan-contracts.mjs", import.meta.url), path.join(directory, "execution-plan-contracts.mjs"));
    fs.copyFileSync(new URL("execution-briefs.mjs", import.meta.url), path.join(directory, "execution-briefs.mjs"));
    fs.writeFileSync(path.join(directory, "beta-mvp-execution.json"), JSON.stringify(fixture));
    return spawnSync(process.execPath, [path.join(directory, "validate-execution-plan.mjs")], { encoding: "utf8" });
  } finally {
    // Only the fresh, uniquely named test directory above is removed.
    fs.rmSync(directory, { recursive: true });
  }
}

test("reviewed contracts accept the current plan", () => validateReviewedContracts(baseline));
test("human-readable gap and epic index matches the manifest", () => {
  const doc = fs.readFileSync(new URL("BETA_MVP_EXECUTION_PLAN.md", import.meta.url), "utf8");
  const gapRows = doc.split("\n").filter((line) => /^\| G\d{2} /.test(line));
  assert.equal(gapRows.length, 22);
  for (const epic of baseline.epics) {
    const rows = doc.split("\n").filter((line) => line.startsWith(`| ${epic.milestone} / ${epic.package} | [#${epic.number}]`));
    assert.equal(rows.length, 1, `missing/duplicate epic index #${epic.number}`);
    const ids = [...rows[0].matchAll(/\[#(\d+)\]/g)].map((match) => Number(match[1]));
    assert.deepEqual(ids, [epic.number, ...epic.children]);
  }
});
for (const [number, field, phrase] of contractRules) {
  test(`rejects semantic regression #${number} ${field}: ${phrase}`, () => {
    const fixture = structuredClone(baseline);
    const task = fixture.tasks.find((t) => t.number === number);
    task[field] = task[field].replace(phrase, "superseded instruction");
    assert.throws(() => validateReviewedContracts(fixture), /reviewed contract/);
  });
}
for (const [consumer, producer] of prerequisiteRules) {
  test(`rejects omitted producer #${producer} -> #${consumer}`, () => {
    const fixture = structuredClone(baseline);
    const task = fixture.tasks.find((t) => t.number === consumer);
    task.blocked_by = task.blocked_by.filter((n) => n !== producer);
    assert.throws(() => validateReviewedContracts(fixture), /missing producer/);
  });
}
for (const [name, mutate, pattern] of [
  ["review provenance", (p) => delete p.contract_review, /missing contract review/],
  ["late loader parent", (p) => { p.tasks.find((t) => t.number === 726).parent = 525; }, /rule loader/],
  ["late loader milestone", (p) => { p.tasks.find((t) => t.number === 726).milestone = "D3"; }, /rule loader/],
  ["merged migration edit", (p) => { p.tasks[0].files += "; same migration"; }, /merged migration rewrite/],
  ["contradictory waiver oracle", (p) => { p.tasks[0].acceptance += "; with an active waiver for that slice it passes"; }, /superseded unsafe instruction/],
]) {
  test(`rejects ${name}`, () => {
    const fixture = structuredClone(baseline);
    mutate(fixture);
    assert.throws(() => validateReviewedContracts(fixture), pattern);
  });
}

test("current plan passes without network access", () => {
  const result = run(() => {});
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /22 gaps; full task\/epic\/gate DAG passes/);
});

for (const [name, mutate, message] of [
  ["duplicate task", (p) => p.tasks.push(p.tasks[0]), "duplicate task"],
  ["orphan task", (p) => { p.tasks[0].parent = 999999; }, "orphan"],
  ["missing test contract", (p) => { p.tasks[0].tests = ""; }, "missing tests"],
  ["missing gap", (p) => { p.tasks.forEach((t) => { t.gaps = t.gaps.filter((g) => g !== "G22"); }); }, "deep-equal"],
  ["future milestone dependency", (p) => { p.tasks[0].blocked_by.push(p.tasks.find((t) => t.milestone === "D15").number); }, "backward milestone"],
  ["task cycle", (p) => { p.tasks[0].blocked_by.push(p.tasks[0].number); }, "dependency cycle"],
  ["gate cycle", (p) => { const t = p.tasks.find((t) => t.milestone === "D5"); t.blocked_by.push(613); }, "dependency cycle"],
  ["missing scope exclusion", (p) => { p.excluded = []; }, "AssertionError"],
]) {
  test(`rejects ${name}`, () => {
    const result = run(mutate);
    assert.notEqual(result.status, 0);
    assert(result.stderr.includes(message), result.stderr);
  });
}
