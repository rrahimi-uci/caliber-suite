// Offline integrity checks for the frozen planning archive; never a current release gate.
import assert from "node:assert/strict";
import fs from "node:fs";
import { validateReviewedContracts } from "./execution-plan-contracts.mjs";
import { validateBriefs } from "./execution-briefs.mjs";

if (process.argv.length > 2) {
  console.error("Retired planning parity interface: --live, --snapshot and other arguments are unsupported. GitHub Project + Issues own current planning. Run without arguments for offline archive integrity only; use .agents/ workflows for live planning.");
  process.exit(2);
}

const plan = JSON.parse(fs.readFileSync(new URL("beta-mvp-execution.json", import.meta.url)));
const epics = new Map(plan.epics.map((item) => [item.number, item]));
const tasks = new Map(plan.tasks.map((item) => [item.number, item]));
const controls = new Map(plan.controls.map((item) => [item.issue, item]));
assert.equal(epics.size, plan.epics.length, "duplicate epic");
assert.equal(tasks.size, plan.tasks.length, "duplicate task");
assert.equal(new Set(plan.tasks.map((item) => item.key)).size, tasks.size, "duplicate task key");
assert.equal(controls.size, 15, "missing milestone control");
const gaps = new Set();
for (const task of tasks.values()) {
  assert(epics.has(task.parent), `orphan #${task.number}`);
  assert(epics.get(task.parent).children.includes(task.number), `missing parent membership #${task.number}`);
  for (const field of ["scope", "files", "tests", "acceptance", "documentation"]) {
    assert(task[field]?.trim(), `missing ${field} #${task.number}`);
  }
  const milestone = Number(task.milestone.slice(1));
  assert.equal(task.stage, milestone <= 5 ? "Beta" : "MVP", `stage #${task.number}`);
  assert.equal(task.milestone, epics.get(task.parent).milestone, `milestone #${task.number}`);
  task.gaps.forEach((gap) => gaps.add(gap));
  for (const prerequisite of task.blocked_by) {
    const producer = tasks.get(prerequisite) || controls.get(prerequisite);
    assert(producer, `unknown prerequisite #${prerequisite}`);
    assert(Number(producer.milestone.slice(1)) <= milestone, `backward milestone #${task.number}`);
  }
}
for (const epic of epics.values()) {
  assert(epic.children.length, `empty epic #${epic.number}`);
  for (const child of epic.children) assert.equal(tasks.get(child)?.parent, epic.number);
}
assert.deepEqual([...gaps].sort(), Array.from({ length: 22 }, (_, i) => `G${String(i + 1).padStart(2, "0")}`));
assert(plan.excluded.some((item) => item.id === "V28-shared-tenancy"));
const graph = new Map([...tasks.values()].map((item) => [item.number, item.blocked_by]));
// Completion gates depend on epics; epics aggregate children. This catches cycles hidden by a task-only DAG.
for (const epic of epics.values()) graph.set(epic.number, epic.children.filter((n) => tasks.get(n).release_required !== false));
for (const control of controls.values()) graph.set(control.issue, control.blocked_by);
graph.set(608, []);
const active = new Set();
const done = new Set();
function visit(number) {
  if (done.has(number)) return;
  assert(!active.has(number), `dependency cycle: ${[...active, number].join(" -> ")}`);
  active.add(number);
  for (const dependency of graph.get(number) || []) visit(dependency);
  active.delete(number);
  done.add(number);
}
for (const number of graph.keys()) visit(number);
validateReviewedContracts(plan);
validateBriefs(plan);

console.log(`Archive only: ${epics.size} epics, ${tasks.size} tasks, 22 gaps; full task/epic/gate DAG passes. This is not current planning or release validation.`);
