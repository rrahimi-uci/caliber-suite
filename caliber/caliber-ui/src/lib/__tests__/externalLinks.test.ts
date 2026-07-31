import { afterEach, describe, expect, it } from "vitest";

import {
  appendThemeHintToUrl,
  buildCaliberHref,
  buildCaliberRouteHref,
  buildMlflowHref,
} from "@/lib/externalLinks";

afterEach(() => {
  window.localStorage.clear();
  window.__CALIBER_STATIC_PREFIX__ = undefined;
  window.history.replaceState({}, "", "/");
});

describe("externalLinks", () => {
  it("builds the MLflow gateway entry link by default", () => {
    expect(buildMlflowHref()).toBe("/?ui=mlflow");
  });

  it("includes theme hint + hash route for MLflow links", () => {
    window.localStorage.setItem("caliber.theme", "dark");
    expect(
      buildMlflowHref({ hash: "#/experiments/42/runs/mlflow-run-1" }),
    ).toBe("/?ui=mlflow&theme=dark#/experiments/42/runs/mlflow-run-1");
  });

  it("uses static prefix deployments when configured", () => {
    window.__CALIBER_STATIC_PREFIX__ = "/mlflow";
    window.localStorage.setItem("caliber.theme", "light");
    expect(buildMlflowHref()).toBe("/mlflow/?theme=light");
    expect(buildCaliberHref()).toBe("/mlflow/caliber/?theme=light");
    expect(buildCaliberRouteHref("workflow-runs/WR-1")).toBe(
      "/mlflow/caliber/workflow-runs/WR-1",
    );
  });

  it("appends theme hints to absolute URLs", () => {
    window.localStorage.setItem("caliber.theme", "dark");
    expect(appendThemeHintToUrl("http://127.0.0.1:9001")).toBe(
      "http://127.0.0.1:9001/?theme=dark",
    );
    expect(appendThemeHintToUrl("http://127.0.0.1:8081/?pgsql=postgres")).toBe(
      "http://127.0.0.1:8081/?pgsql=postgres&theme=dark",
    );
  });
});
