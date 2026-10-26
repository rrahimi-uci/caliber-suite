import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test } from "node:test";
import { contractRules, prerequisiteRules, validateReviewedContracts } from "./execution-plan-contracts.mjs";

const source = fs.readFileSync(new URL("validate-execution-plan.mjs", import.meta.url));
const baseline = JSON.parse(fs.readFileSync(new URL("beta-mvp-execution.json", import.meta.url)));

function run(change) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "caliber-plan-validator-"));
  try {
    const fixture = structuredClone(baseline);
    change(fixture);
    fs.writeFileSync(path.join(directory, "validate-execution-plan.mjs"), source);
    fs.copyFileSync(new URL("execution-plan-contracts.mjs", import.meta.url), path.join(directory, "execution-plan-contracts.mjs"));
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
