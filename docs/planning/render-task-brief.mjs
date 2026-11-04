import fs from "node:fs";
import { renderTask } from "./execution-briefs.mjs";

const plan = JSON.parse(fs.readFileSync(new URL("beta-mvp-execution.json", import.meta.url)));
const number = Number(process.argv[2]);
const task = plan.tasks.find((item) => item.number === number);
if (!task || process.argv.length !== 3) {
  console.error("Usage: node docs/planning/render-task-brief.mjs <task-issue-number>");
  process.exitCode = 2;
} else {
  console.log(renderTask(plan, task));
}
