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

test("every archived brief retains scope, outcomes and dependency links with a warning", () => {
  validateBriefs(baseline);
  for (const task of baseline.tasks) {
    const body = renderTask(baseline, task, { archive: true });
    for (const text of [task.scope, task.acceptance, task.files, task.tests, task.execution.evidence_path, markerStart, markerEnd]) assert(body.includes(text));
    assert.match(body, /follow AGENTS\.md's quality and review gates before any agent merges through the PR/);
    assert.doesNotMatch(body, /owner's final review\/merge/);
    for (const n of task.blocked_by) assert(body.includes(`[#${n}]`));
    assert.equal(body.split(markerStart).length, 2);
    assert.match(body, /^> ARCHIVE ONLY/);
    assert.doesNotMatch(body, /This block is authoritative|## Current|caliber-agent-brief:v2/);
  }
  for (const epic of baseline.epics) {
    const body = renderEpic(baseline, epic, { archive: true });
    assert(body.includes(epic.decision));
    assert.match(body, /^> ARCHIVE ONLY/);
  }
  for (const control of baseline.controls) {
    const body = renderControl(baseline, control, { archive: true });
    assert(body.includes(control.outcome));
    assert.match(body, /^> ARCHIVE ONLY/);
  }
  assert.match(renderDecisions(baseline, { archive: true }), /^> ARCHIVE ONLY/);
  assert(renderDecisions(baseline, { archive: true }).includes("H16"));
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
  assert(renderTask(p, { ...task, blocked_by: [] }, { archive: true }).includes(d.evidence));
});

test("legacy synchronization and implicit current rendering fail without altering evidence", () => {
  const previous = "Completed migration; remaining review is pending.";
  for (const options of [undefined, {}, { archive: false }]) {
    for (const render of [
      () => renderTask(baseline, baseline.tasks[0], options),
      () => renderEpic(baseline, baseline.epics[0], options),
      () => renderControl(baseline, baseline.controls[0], options),
      () => renderDecisions(baseline, options),
    ]) assert.throws(render, /Retired current-brief renderer/);
  }
  const brief = renderTask(baseline, baseline.tasks[0], { archive: true });
  for (const body of [previous, `<!-- caliber-agent-brief:v2 -->${previous}<!-- /caliber-agent-brief:v2 -->`]) {
    assert.throws(() => managedBody(body, brief), /Retired manifest-to-issue synchronization/);
    assert(body.includes(previous));
  }
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
  assert(renderDecisions(p, { archive: true }).includes("named owner decision"));
  const control = { ...p.controls[0], blocked_by: [], human_inputs: [] };
  assert(renderControl(p, control, { archive: true }).includes("near-term kickoff"));
});

test("archive CLI requires explicit intent and rejects absent/unknown/extra arguments", () => {
  const cli = new URL("render-task-brief.mjs", import.meta.url).pathname;
  const task = baseline.tasks[0];
  const result = spawnSync(process.execPath, [cli, "--archive", String(task.number)], { encoding: "utf8" });
  assert.equal(result.status, 0);
  assert.equal(result.stdout.trim(), renderTask(baseline, task, { archive: true }));
  for (const args of [[], [String(task.number)], ["--archive"], ["--archive", "999999"],
    ["--archive", "-1"], ["--archive", "no"], ["--archive", String(task.number), "extra"]]) {
    const result = spawnSync(process.execPath, [cli, ...args], { encoding: "utf8" });
    assert.equal(result.status, 2);
    assert.equal(result.stdout, "");
    assert.match(result.stderr, /historical|archive/);
    assert.match(result.stderr, /GitHub Project/);
  }
});

test("retired parity modes fail before reading snapshots or invoking GitHub", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "caliber-retired-plan-"));
  try {
    const called = path.join(directory, "gh-called");
    fs.writeFileSync(path.join(directory, "gh"), `#!${process.execPath}\nrequire('node:fs').writeFileSync(${JSON.stringify(called)}, 'called');\nprocess.exit(1);\n`, { mode: 0o755 });
    const cli = new URL("validate-execution-plan.mjs", import.meta.url).pathname;
    for (const args of [["--live"], ["--snapshot"], ["--snapshot", "/missing/snapshot.json"], ["--live", "--snapshot", "/missing"], ["--unexpected"]]) {
      const result = spawnSync(process.execPath, [cli, ...args], {
        encoding: "utf8", env: { ...process.env, PATH: directory },
      });
      assert.equal(result.status, 2, result.stderr);
      assert.equal(result.stdout, "");
      assert.match(result.stderr, /Retired planning parity interface/);
      assert.match(result.stderr, /GitHub Project/);
      assert(!fs.existsSync(called), "retired command must not contact GitHub");
    }
    const offline = spawnSync(process.execPath, [cli], {
      encoding: "utf8", env: { ...process.env, PATH: directory },
    });
    assert.equal(offline.status, 0, offline.stderr);
    assert.match(offline.stdout, /Archive only/);
    assert(!fs.existsSync(called));
  } finally {
    fs.rmSync(directory, { recursive: true });
  }
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

test("reviewed contracts accept the archived plan", () => validateReviewedContracts(baseline));
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

test("archive passes without network access", () => {
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
