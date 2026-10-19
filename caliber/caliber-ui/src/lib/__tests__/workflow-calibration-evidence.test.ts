import { describe, expect, it } from "vitest";

import type { RefinementJob } from "@/api/types";
import {
  CALIBRATION_JOBS_POLL_MS,
  calibrationJobsRefetchInterval,
  workflowCalibrationView,
} from "@/lib/workflowCalibration";

function job(evalResults: Record<string, unknown> | null): RefinementJob {
  return {
    job_id: "RFN-1",
    agent_id: "a",
    workflow_id: "WF-1",
    primary_item_id: "FB-1",
    mlflow_run_id: null,
    artifact_type: "workflow_manifest",
    optimizer_type: null,
    status: "candidate_ready",
    current_stage: "done",
    attempt_count: 1,
    error_message: null,
    total_tokens: 0,
    cost_usd: 0,
    bundle_targets: [],
    bundle_expansion_count: 0,
    diagnosis: null,
    candidate: null,
    eval_results: evalResults,
    calibration_spec: { objective: { maximize: "quality" } },
    created_at: "2026-10-13T00:00:00Z",
    updated_at: "2026-10-13T00:00:00Z",
  };
}

describe("workflow calibration evidence markers", () => {
  it("reads the provider, whether it was simulated, and how it was scored", () => {
    const view = workflowCalibrationView(
      job({ provider: "fake", simulated: true, scorer: "structural" }),
    );
    expect(view?.simulated).toBe(true);
    expect(view?.evalProvider).toBe("fake");
    expect(view?.scorer).toBe("structural");
  });

  it("reads a measured, judged evaluation as such", () => {
    const view = workflowCalibrationView(
      job({ provider: "openai", simulated: false, scorer: "llm_judge" }),
    );
    expect(view?.simulated).toBe(false);
    expect(view?.scorer).toBe("llm_judge");
  });

  it("treats an older run with no markers as unknown, never as measured or as simulated", () => {
    const view = workflowCalibrationView(job({ n_examples: 3 }));
    expect(view?.simulated).toBeNull();
    expect(view?.evalProvider).toBeNull();
    expect(view?.scorer).toBeNull();
    expect(workflowCalibrationView(job(null))?.simulated).toBeNull();
  });

  it("ignores malformed markers", () => {
    const view = workflowCalibrationView(
      job({ simulated: "yes", scorer: 7, provider: "" }),
    );
    expect(view?.simulated).toBeNull();
    expect(view?.scorer).toBeNull();
    expect(view?.evalProvider).toBeNull();
  });
});

describe("calibration run list polling", () => {
  it("polls while any run is queued or running, and stops once all are done", () => {
    expect(calibrationJobsRefetchInterval([{ status: "queued" }])).toBe(
      CALIBRATION_JOBS_POLL_MS,
    );
    expect(
      calibrationJobsRefetchInterval([
        { status: "rejected" },
        { status: "running" },
      ]),
    ).toBe(CALIBRATION_JOBS_POLL_MS);
    expect(
      calibrationJobsRefetchInterval([
        { status: "rejected" },
        { status: "candidate_ready" },
        { status: "failed" },
      ]),
    ).toBe(false);
  });

  it("does not poll before the first response or when there are no runs", () => {
    expect(calibrationJobsRefetchInterval(undefined)).toBe(false);
    expect(calibrationJobsRefetchInterval([])).toBe(false);
  });
});
