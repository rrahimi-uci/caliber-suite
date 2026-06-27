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
  },
});
