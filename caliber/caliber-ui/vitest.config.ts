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
  },
});
