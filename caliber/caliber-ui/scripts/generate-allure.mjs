#!/usr/bin/env node
/**
 * Generate the combined Allure report WITHOUT recreating the output directory's
 * inode.
 *
 * Why: CALIBER serves `allure-report/` in-app via a Docker bind mount. `allure
 * generate --clean -o allure-report` DELETES and recreates that directory, which
 * changes its inode and breaks the running container's mount (the container then
 * sees an empty dir → "No Allure report generated yet"). To avoid that, we
 * generate into a temp dir, then replace the contents of `allure-report/` in
 * place — the directory itself (and its inode / the bind mount) is preserved.
 *
 * Reads from both result dirs (frontend `allure-results` + backend
 * `../allure-results`), whichever exist.
 */
import { execFileSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, readdirSync, rmSync } from "node:fs";
import { resolve } from "node:path";

import { generateAllureInsights, injectInsightsEntry } from "./generate-allure-insights.mjs";

const OUT = resolve("allure-report");
const TMP = resolve("allure-report.generated.tmp");
const MERGED = resolve("allure-results.generated.tmp");
const RESULT_DIRS = ["allure-results", "../allure-results"].filter((d) => existsSync(d));

if (RESULT_DIRS.length === 0) {
  console.error("no allure-results found — run the suites first (make test-allure).");
  process.exit(1);
}

function mergeResultDirs() {
  rmSync(MERGED, { recursive: true, force: true });
  mkdirSync(MERGED, { recursive: true });

  // Preserve cross-run trend data once, in one merged result tree. Passing
  // multiple result dirs directly to Allure makes it awkward to carry a single
  // shared history/ forward without duplication.
  const previousHistory = resolve(OUT, "history");
  if (existsSync(previousHistory)) {
    cpSync(previousHistory, resolve(MERGED, "history"), { recursive: true });
  }

  for (const dir of RESULT_DIRS) {
    for (const entry of readdirSync(dir)) {
      if (entry === "history") continue;
      cpSync(resolve(dir, entry), resolve(MERGED, entry), { recursive: true });
    }
  }
}

// 1) Merge the live result dirs into one temp input tree so history can be
// carried forward exactly once.
mergeResultDirs();

// 2) Generate into a throwaway temp dir (safe to --clean: not mounted).
rmSync(TMP, { recursive: true, force: true });
execFileSync("allure", ["generate", MERGED, "--clean", "-o", TMP], {
  stdio: "inherit",
});

// 3) Empty the served dir's CONTENTS in place (keep the dir → mount survives).
mkdirSync(OUT, { recursive: true });
for (const entry of readdirSync(OUT)) {
  rmSync(resolve(OUT, entry), { recursive: true, force: true });
}

// 4) Move the freshly generated contents into the stable dir.
for (const entry of readdirSync(TMP)) {
  cpSync(resolve(TMP, entry), resolve(OUT, entry), { recursive: true });
}

// 5) Add CALIBER-owned insights without forking the generated Allure bundle.
generateAllureInsights(OUT);
injectInsightsEntry(OUT);

// 6) Clean up temp dirs.
rmSync(TMP, { recursive: true, force: true });
rmSync(MERGED, { recursive: true, force: true });

console.log(`Allure report written to ${OUT} (contents replaced in place).`);
