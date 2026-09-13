/// <reference types="vitest" />
import path from "node:path";
import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    // ``allure-vitest/setup`` registers the per-test Allure lifecycle; our own
    // setup keeps the existing jsdom/testing-library wiring.
    setupFiles: ["allure-vitest/setup", "./src/test/setup.ts"],
    css: true,
    // Keep the default console reporter and additionally emit Allure results to
    // ``allure-results/``. Viewing the report is a separate CLI step
    // (``npm run allure:serve`` / ``allure:generate``).
    reporters: [
      "default",
      ["allure-vitest/reporter", { resultsDir: "allure-results" }],
    ],
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    fileParallelism: false,
    minWorkers: 1,
    // The repo's heaviest jsdom suites intermittently time out while starting
    // worker processes on local/dev hardware. Favor a single sequential worker
    // so release validation stays reliable even when it costs some runtime.
    maxWorkers: 1,
    // CI runners are markedly slower than dev machines: multi-step tests that
    // walk React.lazy routes can exceed the 5s default whole-test timeout, and
    // renders split across more microtasks expose sync-query races. Give tests
    // headroom and, in CI only, retry to absorb genuinely intermittent flakes
    // (a consistently broken test still fails every attempt).
    testTimeout: process.env.CI ? 20000 : 5000,
    hookTimeout: process.env.CI ? 20000 : 10000,
    retry: process.env.CI ? 2 : 0,
    coverage: {
      provider: "v8",
      reporter: ["text", "html", "json-summary", "lcov"],
      // Scoped to the app itself. ``scripts/**`` is Node tooling invoked
      // directly (docs sync, Allure reports, the UX census) -- not part of
      // the SPA bundle and outside what ``include`` above even runs tests
      // against, so counting it here just dilutes the number with an
      // unrelated surface. ``src/test/**`` is test infrastructure (MSW
      // handlers, jsdom setup, render utils) that real tests exercise, not
      // app code with its own coverage target.
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "src/test/**",
        "src/**/*.d.ts",
        "src/main.tsx",
        "src/**/__tests__/**",
        "src/**/*.{test,spec}.{ts,tsx}",
      ],
      // Ratchet, matching the backend's `[tool.coverage.report] fail_under`
      // in caliber/pyproject.toml: pinned a few points below the actual
      // measured run (lines 90.61%, statements 88.92%, functions 89.19%,
      // branches 80.89%) rather than at it, so normal day-to-day
      // fluctuation doesn't fail CI on an unrelated change -- a real
      // regression still fails the build, and closing more of the gap
      // (the four largest remaining files -- WorkflowEditor.tsx, Prompts.tsx,
      // Inspector.tsx, KnowledgeBases.tsx -- account for most of what's left)
      // is the preferred way to raise these thresholds further, not loosening
      // them.
      thresholds: {
        lines: 89,
        statements: 87,
        functions: 87,
        branches: 78,
      },
    },
  },
});
