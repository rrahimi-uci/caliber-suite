import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { afterEach, beforeEach } from "vitest";
import { epic, feature } from "allure-js-commons";

// On slower CI runners the default 1000ms findBy*/waitFor timeout lapses while
// React.lazy routes or query-driven panels finish resolving. Give async queries
// more headroom in CI (kept tight locally so real hangs surface fast).
configure({ asyncUtilTimeout: process.env.CI ? 5000 : 1000 });

// Categorise frontend tests in the Allure report (Behaviors tab) so they group
// by functional area under a "Frontend" epic — mirroring the backend's
// auto-labelling — instead of each test name becoming its own epic. Keyword →
// feature on the spec file path; first match wins, "Frontend" as the fallback.
const FE_FEATURE_RULES: ReadonlyArray<readonly [RegExp, string]> = [
  [/observabilit/i, "Observability"],
  [/evaluation|eval-|eval\./i, "Evaluations"],
  [/gateway/i, "LLM Gateway"],
  [/setting/i, "Settings"],
  [/workflow|studio|editor|canvas|inspector|bakeoff|benchmark/i, "Workflows"],
  [/knowledge/i, "Knowledge Base"],
  [/skill/i, "Skills"],
  [/mcp/i, "MCP Servers"],
  [/tool/i, "Tools"],
  [/prompt/i, "Prompts"],
  [/object.?store/i, "Object Storage"],
  [/assistant|aria/i, "Assistant"],
  [/overview|dashboard/i, "Dashboard"],
  [/sidebar|app-?shell|navigation|login|route/i, "App Shell & Navigation"],
  [/provider|environment/i, "Providers & Config"],
];

function featureForPath(path: string): string {
  for (const [pattern, name] of FE_FEATURE_RULES) {
    if (pattern.test(path)) return name;
  }
  return "Frontend";
}

beforeEach(async (ctx) => {
  // ``allure-vitest`` binds the runtime per-test; this no-ops if it isn't active.
  try {
    const path = ctx.task?.file?.name ?? ctx.task?.file?.filepath ?? "";
    await epic("Frontend");
    await feature(featureForPath(path));
  } catch {
    // Allure runtime not active for this run — labelling is best-effort.
  }
});

class ResizeObserverMock {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

Object.defineProperty(globalThis, "ResizeObserver", {
  value: ResizeObserverMock,
  writable: true,
  configurable: true,
});

afterEach(() => {
  cleanup();
  // Isolate tests from persisted UI state (assistant open/session, panel width,
  // search drafts, etc.) so localStorage from one test can't leak into the next.
  try {
    window.localStorage.clear();
  } catch {
    // jsdom without storage — nothing to clear.
  }
});
