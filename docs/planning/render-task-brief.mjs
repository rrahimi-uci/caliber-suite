// Explicit historical inspection only. The old current-task interface is retired.
import fs from "node:fs";
import { renderTask } from "./execution-briefs.mjs";

const args = process.argv.slice(2);
if (args.length !== 2 || args[0] !== "--archive" || !/^[1-9]\d*$/.test(args[1])) {
  console.error("Retired current-brief interface. Read current scope in GitHub Project + Issues using .agents/ workflows. Usage for historical inspection only: node docs/planning/render-task-brief.mjs --archive <task-issue-number>");
  process.exitCode = 2;
} else {
  const plan = JSON.parse(fs.readFileSync(new URL("beta-mvp-execution.json", import.meta.url)));
  const task = plan.tasks.find((item) => item.number === Number(args[1]));
  if (!task) {
    console.error("Unknown task in the historical archive; consult GitHub Project + Issues for current work.");
    process.exitCode = 2;
  } else {
    console.log(renderTask(plan, task, { archive: true }));
  }
}
