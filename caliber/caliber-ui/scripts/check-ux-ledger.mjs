/**
 * Verify §15.2's ledger against git history.
 *
 * The ledger in `ux-analysis-report.md` claims, per work package, whether its
 * PR has merged. That claim went stale five times in one review cycle: each
 * correction of one sentence left an adjacent one contradicting it, and a
 * reviewer had to catch every round. Hand-maintained facts that are also
 * recorded somewhere authoritative should be checked against that record
 * rather than re-read carefully.
 *
 * The authoritative record here is `git log` on the default branch, where the
 * squash-merge convention leaves "(#NNN)" in every merged commit subject. So:
 *
 *   - a row marked **Landed** must have its PR in the log;
 *   - a row marked **In review** must not;
 *   - a row marked **Open** must cite no PR at all.
 *
 * Deliberately git-only. No network, no `gh`, no token — the same constraint
 * `ux-census.mjs` holds, so this runs identically in CI, offline, and on a
 * fork. The cost is that it cannot tell "PR open" from "PR never existed";
 * both read as not-merged, which is exactly the distinction the ledger's
 * Status column is *for* and a human still owns.
 *
 * **What it cannot catch: omission.** It verifies the ledger does not
 * *contradict* git history. A row marked `Open` with no PR is internally
 * consistent even if that package shipped last week under a PR the ledger
 * never mentions — nothing here links a work-package id to a commit, because
 * no enforced convention carries one. So a green result means "no row lies",
 * not "the ledger is current". Building the id→PR link on the unenforced
 * habit of naming UX ids in commit bodies would trade a known limit for an
 * unreliable check, which is a worse trade.
 *
 * Usage:
 *   node scripts/check-ux-ledger.mjs            # exits non-zero on drift
 *   node scripts/check-ux-ledger.mjs --list     # print the parsed rows
 */

import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

/** Where the ledger lives, and how its table announces itself. */
const SECTION_HEADING = "### 15.2 Status ledger";
const TABLE_HEADING = "| ID |";

/** The three merge states the Status column may claim. */
export const STATUS_LANDED = "Landed";
export const STATUS_IN_REVIEW = "In review";
export const STATUS_OPEN = "Open";

/**
 * Parse the §15.2 ledger rows out of the report.
 *
 * Rows look like:
 *   | **UX-05** | Render structured validation errors | **Landed** ¹ | ... | `#243` |
 *
 * The footnote marker and bold markup are noise for this purpose; only the id,
 * the status word, and any cited PR numbers matter.
 */
export function parseLedger(markdown) {
  // Every boundary below is checked explicitly. The earlier version relied on
  // `slice()` doing something reasonable with `indexOf`'s `-1`, which it does
  // -- just not the reasonable thing you would pick on purpose. `slice(-1)`
  // returns the document's last character rather than nothing, and
  // `slice(0, -1)` drops the final character rather than taking everything.
  // Both happened to yield the right answer for today's formatting, so the
  // parser was correct by luck; a table that ends at EOF, or a renamed header
  // column, would have silently dropped rows. A dropped row is an unchecked
  // claim, which is the exact failure this script exists to prevent.
  const sectionStart = markdown.indexOf(SECTION_HEADING);
  if (sectionStart === -1) {
    throw new Error(`"${SECTION_HEADING}" not found — has the section moved or been renamed?`);
  }
  const section = markdown.slice(sectionStart);

  const headerStart = section.indexOf(TABLE_HEADING);
  if (headerStart === -1) {
    throw new Error(
      `"${TABLE_HEADING}" not found under "${SECTION_HEADING}" — has the ledger's first column been renamed?`,
    );
  }

  // A Markdown table ends at the first blank line. When it runs to the end of
  // the document there is no blank line, and the whole remainder is the table.
  const blankLine = section.indexOf("\n\n", headerStart);
  const table = blankLine === -1 ? section.slice(headerStart) : section.slice(headerStart, blankLine);

  const rows = [];
  for (const line of table.split("\n")) {
    const cells = line.split("|").map((c) => c.trim());
    // | id | outcome | status | wave | size | pr |  → 8 cells with the empties
    if (cells.length < 7) continue;
    const id = cells[1].replace(/\*/g, "").trim();
    if (!/^UX-\d+[a-z]?$/.test(id)) continue;
    const statusCell = cells[3];
    const status = [STATUS_LANDED, STATUS_IN_REVIEW, STATUS_OPEN].find((s) =>
      statusCell.replace(/\*/g, "").trim().startsWith(s),
    );
    const prs = [...cells[6].matchAll(/#(\d+)/g)].map((m) => Number(m[1]));
    rows.push({ id, status, prs });
  }
  return rows;
}

/** PR numbers that appear as "(#NNN)" in the given git log subjects. */
export function mergedPrNumbers(logSubjects) {
  const merged = new Set();
  for (const subject of logSubjects) {
    for (const match of subject.matchAll(/\(#(\d+)\)/g)) {
      merged.add(Number(match[1]));
    }
  }
  return merged;
}

/**
 * Compare parsed rows against the merged set.
 *
 * Returns a list of problems, each naming the row, what it claimed, and what
 * the log says — enough for the message to be actionable without opening the
 * document.
 */
export function checkLedger(rows, merged) {
  const problems = [];
  for (const { id, status, prs } of rows) {
    if (!status) {
      problems.push(`${id}: Status cell does not start with a known state`);
      continue;
    }
    if (status === STATUS_OPEN) {
      if (prs.length > 0) {
        problems.push(`${id}: marked Open but cites ${prs.map((n) => `#${n}`).join(", ")}`);
      }
      continue;
    }
    if (prs.length === 0) {
      problems.push(`${id}: marked ${status} but cites no PR`);
      continue;
    }
    for (const pr of prs) {
      const isMerged = merged.has(pr);
      if (status === STATUS_LANDED && !isMerged) {
        problems.push(`${id}: marked Landed but #${pr} is not in the git log`);
      }
      if (status === STATUS_IN_REVIEW && isMerged) {
        problems.push(`${id}: marked In review but #${pr} has merged`);
      }
    }
  }
  return problems;
}

function gitLogSubjects(repoRoot) {
  return execFileSync("git", ["log", "--format=%s", "-n", "500"], {
    cwd: repoRoot,
    encoding: "utf8",
  })
    .split("\n")
    .filter(Boolean);
}

function main(argv) {
  const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
  let rows;
  try {
    rows = parseLedger(readFileSync(resolve(repoRoot, "ux-analysis-report.md"), "utf8"));
  } catch (error) {
    // A structural change to the report is a real failure, not a crash: report
    // it in the same voice as a drift finding so it is equally actionable.
    process.stderr.write(`[check-ux-ledger] ${error.message}\n`);
    return 1;
  }

  if (rows.length === 0) {
    process.stderr.write(
      "[check-ux-ledger] the ledger table was found but contains no UX-nn rows\n",
    );
    return 1;
  }

  if (argv.includes("--list")) {
    for (const row of rows) {
      const prs = row.prs.length ? row.prs.map((n) => `#${n}`).join(", ") : "—";
      process.stdout.write(`${row.id.padEnd(8)} ${String(row.status).padEnd(10)} ${prs}\n`);
    }
    return 0;
  }

  const problems = checkLedger(rows, mergedPrNumbers(gitLogSubjects(repoRoot)));
  if (problems.length > 0) {
    process.stderr.write("[check-ux-ledger] ledger disagrees with git history:\n");
    for (const problem of problems) process.stderr.write(`  - ${problem}\n`);
    process.stderr.write(
      "\nA row is Landed only once its PR appears in the log. If a PR merged, " +
        "update §15.2 — and nothing else, since it is the only place this " +
        "document states merge state.\n",
    );
    return 1;
  }

  process.stdout.write(`[check-ux-ledger] ${rows.length} rows agree with git history\n`);
  return 0;
}

if (process.argv[1] && process.argv[1].endsWith("check-ux-ledger.mjs")) {
  process.exit(main(process.argv.slice(2)));
}
