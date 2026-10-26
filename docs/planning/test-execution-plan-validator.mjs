import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test } from "node:test";

const source = fs.readFileSync(new URL("validate-execution-plan.mjs", import.meta.url));
const baseline = JSON.parse(fs.readFileSync(new URL("beta-mvp-execution.json", import.meta.url)));

function run(change) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "caliber-plan-validator-"));
  try {
    const fixture = structuredClone(baseline);
    change(fixture);
    fs.writeFileSync(path.join(directory, "validate-execution-plan.mjs"), source);
    fs.writeFileSync(path.join(directory, "beta-mvp-execution.json"), JSON.stringify(fixture));
    return spawnSync(process.execPath, [path.join(directory, "validate-execution-plan.mjs")], { encoding: "utf8" });
  } finally {
    // Only the fresh, uniquely named test directory above is removed.
    fs.rmSync(directory, { recursive: true });
  }
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
