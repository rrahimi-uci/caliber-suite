import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { after, test } from "node:test";
import assert from "node:assert/strict";

import { generateAllureInsights, injectInsightsEntry } from "./generate-allure-insights.mjs";

const roots = [];

after(() => {
  for (const root of roots) {
    rmSync(root, { recursive: true, force: true });
  }
});

function seedReportFixture() {
  const root = mkdtempSync(join(tmpdir(), "caliber-allure-insights-"));
  roots.push(root);

  mkdirSync(join(root, "widgets"), { recursive: true });
  mkdirSync(join(root, "history"), { recursive: true });
  mkdirSync(join(root, "data", "test-cases"), { recursive: true });

  writeFileSync(join(root, "index.html"), "<html><body><div>Allure shell</div></body></html>", "utf8");
  writeFileSync(
    join(root, "widgets", "summary.json"),
    JSON.stringify({
      statistic: {
        failed: 1,
        broken: 0,
        skipped: 1,
        passed: 3,
        unknown: 0,
        total: 5,
      },
      time: {
        duration: 22_000,
        sumDuration: 36_000,
        minDuration: 10,
        maxDuration: 9_000,
      },
    }),
    "utf8",
  );
  writeFileSync(
    join(root, "history", "history-trend.json"),
    JSON.stringify([
      { data: { failed: 1, broken: 0, skipped: 1, passed: 3, unknown: 0, total: 5 } },
      { data: { failed: 0, broken: 0, skipped: 1, passed: 4, unknown: 0, total: 5 } },
    ]),
    "utf8",
  );
  writeFileSync(
    join(root, "history", "duration-trend.json"),
    JSON.stringify([{ data: { duration: 22_000 } }, { data: { duration: 19_000 } }]),
    "utf8",
  );
  writeFileSync(
    join(root, "history", "retry-trend.json"),
    JSON.stringify([{ data: { run: 5, retry: 1 } }, { data: { run: 5, retry: 0 } }]),
    "utf8",
  );
  writeFileSync(
    join(root, "history", "categories-trend.json"),
    JSON.stringify([{ data: { "Known issue": 1 } }, { data: { "Known issue": 0 } }]),
    "utf8",
  );

  const cases = [
    {
      name: "backend smoke",
      status: "passed",
      time: { duration: 4500 },
      retriesCount: 0,
      flaky: false,
      labels: [
        { name: "framework", value: "pytest" },
        { name: "epic", value: "Backend" },
        { name: "feature", value: "Observability" },
        { name: "suite", value: "test_routes_observability" },
        { name: "package", value: "tests.test_routes_observability" },
      ],
    },
    {
      name: "frontend smoke",
      status: "failed",
      time: { duration: 9000 },
      retriesCount: 1,
      flaky: true,
      newFailed: true,
      labels: [
        { name: "framework", value: "vitest" },
        { name: "epic", value: "Frontend" },
        { name: "feature", value: "Settings" },
        { name: "suite", value: "settings-page" },
      ],
    },
  ];
  for (const [index, item] of cases.entries()) {
    writeFileSync(join(root, "data", "test-cases", `${index}.json`), JSON.stringify(item), "utf8");
  }

  return root;
}

test("generateAllureInsights writes a sidecar page with extra sections", () => {
  const root = seedReportFixture();
  const target = generateAllureInsights(root);
  const html = readFileSync(target, "utf8");

  assert.match(html, /CALIBER insights for the current Allure report/);
  assert.match(html, /Current framework mix/);
  assert.match(html, /Flake and retry hotspots/);
  assert.match(html, /backend smoke/);
  assert.match(html, /frontend smoke/);
});

test("injectInsightsEntry adds a visible launch link to index.html", () => {
  const root = seedReportFixture();
  const changed = injectInsightsEntry(root);
  const html = readFileSync(join(root, "index.html"), "utf8");

  assert.equal(changed, true);
  assert.match(html, /caliber-insights-entry/);
  assert.match(html, /extra charts/);
});
