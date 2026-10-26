// Read-only validation of the planning artifact; --live also checks GitHub.
import assert from "node:assert/strict";
import fs from "node:fs";
import { execFileSync } from "node:child_process";
import { validateReviewedContracts } from "./execution-plan-contracts.mjs";

const plan = JSON.parse(fs.readFileSync(new URL("beta-mvp-execution.json", import.meta.url)));
const epics = new Map(plan.epics.map((item) => [item.number, item]));
const tasks = new Map(plan.tasks.map((item) => [item.number, item]));
const controls = new Map(plan.controls.map((item) => [item.issue, item]));
const sorted = (values) => [...values].sort((a, b) => a - b);
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
for (const epic of epics.values()) graph.set(epic.number, epic.children);
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

function gh(args, input) {
  return JSON.parse(execFileSync("gh", args, {
    input: input ? JSON.stringify(input) : undefined,
    encoding: "utf8", maxBuffer: 40 * 1024 * 1024,
  }));
}
if (process.argv.includes("--live")) {
  const issues = [];
  let cursor = null;
  do {
    const query = `query($cursor:String){repository(owner:"rrahimi-uci",name:"caliber-suite"){
      issues(first:100,after:$cursor){pageInfo{endCursor hasNextPage} nodes{
        number state body parent{number} milestone{title} labels(first:50){nodes{name}}
        blockedBy(first:100){nodes{number} pageInfo{hasNextPage}}
        subIssues(first:100){nodes{number} pageInfo{hasNextPage}}
      }}
    }}`;
    const result = gh(["api", "graphql", "--input", "-"], { query, variables: { cursor } });
    assert(!result.errors, JSON.stringify(result.errors));
    const connection = result.data.repository.issues;
    issues.push(...connection.nodes);
    cursor = connection.pageInfo.hasNextPage ? connection.pageInfo.endCursor : null;
  } while (cursor);
  const live = new Map(issues.map((item) => [item.number, item]));
  const planned = new Set([...tasks.keys(), ...epics.keys(), ...controls.keys(), 608]);
  for (const item of issues.filter((issue) => issue.state === "OPEN")) {
    assert(planned.has(item.number), `unaccounted open issue #${item.number}`);
    assert.equal(item.labels.nodes.filter((label) => label.name.startsWith("priority-")).length, 1,
      `missing/conflicting priority #${item.number}`);
  }
  for (const task of tasks.values()) {
    const item = live.get(task.number);
    assert(item, `missing GitHub task #${task.number}`);
    assert.equal(item.parent?.number, task.parent, `native parent #${task.number}`);
    assert(!item.blockedBy.pageInfo.hasNextPage, "dependency pagination required");
    assert.deepEqual(sorted(item.blockedBy.nodes.map((x) => x.number)), sorted(task.blocked_by), `native blockers #${task.number}`);
    assert(item.milestone.title.startsWith(`${task.milestone} —`), `milestone #${task.number}`);
    for (const label of ["task", `stage:${task.stage.toLowerCase()}`, `priority-${task.priority.toLowerCase()}`]) {
      assert(item.labels.nodes.some((x) => x.name === label), `label ${label} #${task.number}`);
    }
    for (const field of ["scope", "files", "tests", "acceptance", "documentation"]) {
      assert(item.body.includes(task[field]), `${field} drift #${task.number}`);
    }
    for (const heading of ["## Validation", "## Acceptance criteria", "## Documentation", "## Dependencies"]) {
      assert(item.body.includes(heading), `missing ${heading} #${task.number}`);
    }
  }
  for (const epic of epics.values()) {
    const item = live.get(epic.number);
    const parent = ({ 589: 523, 592: 553, 594: 542, 595: 540 })[epic.number]
      || 608 + Number(epic.milestone.slice(1));
    assert.equal(item.parent?.number, parent, `epic parent #${epic.number}`);
    assert(!item.subIssues.pageInfo.hasNextPage, "sub-issue pagination required");
    // Nested reused epics are also valid children, in addition to direct implementation tasks.
    for (const child of epic.children) assert(item.subIssues.nodes.some((x) => x.number === child), `sub-issue #${child}`);
    assert(item.labels.nodes.some((x) => x.name === "epic"), `epic label #${epic.number}`);
  }
  for (const control of controls.values()) {
    const item = live.get(control.issue);
    for (const dependency of control.blocked_by) assert(item.blockedBy.nodes.some((x) => x.number === dependency), `gate #${control.issue}`);
  }
  const board = gh(["project", "item-list", "3", "--owner", "rrahimi-uci", "--limit", "1000", "--format", "json"]);
  const items = new Map(board.items.map((item) => [item.content?.number, item]));
  for (const task of tasks.values()) {
    const item = items.get(task.number);
    assert(item, `missing board task #${task.number}`);
    assert.equal(item.stage, task.stage);
    assert.equal(item.priority, task.priority);
    assert.equal(item.lane, task.lane);
    assert.equal(item["item type"], "Task");
    assert.equal(item["execution order"], task.execution_order);
    assert.equal(item["gap IDs"], task.gaps.join(", "));
    const blocked = task.blocked_by.some((number) => live.get(number)?.state !== "CLOSED");
    const expected = live.get(task.number).state === "CLOSED" ? "Done" : blocked ? "Blocked" : "Ready";
    assert.equal(item.readiness, expected, `readiness #${task.number}`);
  }
  const milestones = gh(["api", "--paginate", "--slurp", "repos/rrahimi-uci/caliber-suite/milestones?state=all&per_page=100"]).flat();
  for (const milestone of milestones) {
    if (/^D\d+/.test(milestone.title)) assert.equal(milestone.due_on, null, "unapproved date");
    else assert.equal(milestone.state, "closed", "retired milestone still open");
  }
  console.log(`Live GitHub: ${tasks.size} task scopes/labels/parents/dependencies/board fields and 15 gates match.`);
}
console.log(`Plan: ${epics.size} epics, ${tasks.size} tasks, 22 gaps; full task/epic/gate DAG passes.`);
