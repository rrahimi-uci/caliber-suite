import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  it,
  vi,
} from "vitest";

import type { RefinementJob } from "@/api/types";
import {
  evidenceView,
  SkillCalibrationJobs,
  skillTargetAgentId,
} from "@/components/SkillCalibrationJobs";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function job(overrides: Partial<RefinementJob> = {}): RefinementJob {
  return {
    job_id: "RFN-1",
    agent_id: "skill::refund-replies",
    workflow_id: null,
    primary_item_id: "FB-1",
    mlflow_run_id: null,
    artifact_type: "skill",
    optimizer_type: "SkillMetaPrompt",
    skill_name: "refund-replies",
    status: "candidate_ready",
    current_stage: "done",
    attempt_count: 1,
    error_message: null,
    total_tokens: 0,
    cost_usd: 0,
    bundle_targets: [],
    bundle_expansion_count: 0,
    diagnosis: null,
    candidate: {
      artifact_type: "skill",
      content: "Reply politely.\nOffer a refund.",
      baseline_content: "Reply politely.",
    },
    eval_results: {
      baseline: { overall: 0.88 },
      candidate: { overall: 0.94 },
      deltas: { overall: 0.06 },
      gate: { passed: true, reasons: [] },
      n_examples: 3,
      eval_dataset_id: "DS-1",
      provider: "fake",
      simulated: true,
    },
    calibration_spec: null,
    created_at: "2026-10-13T10:00:00Z",
    updated_at: "2026-10-13T10:00:00Z",
    ...overrides,
  };
}

function serveJobs(jobs: RefinementJob[]): string[] {
  const seenAgents: string[] = [];
  server.use(
    http.get(`${API_BASE}/jobs`, ({ request }) => {
      seenAgents.push(new URL(request.url).searchParams.get("agent_id") ?? "");
      return HttpResponse.json({ data: jobs });
    }),
  );
  return seenAgents;
}

function renderPanel(
  props: Partial<React.ComponentProps<typeof SkillCalibrationJobs>> = {},
): void {
  render(<SkillCalibrationJobs skillName="refund-replies" {...props} />);
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
});
afterAll(() => server.close());

describe("SkillCalibrationJobs", () => {
  it("asks only for the jobs filed under the skill's hidden target", async () => {
    const seen = serveJobs([]);
    renderPanel();
    expect(
      await screen.findByText(/No calibration runs yet/),
    ).toBeInTheDocument();
    expect(seen).toEqual([skillTargetAgentId("refund-replies")]);
    expect(skillTargetAgentId("refund-replies")).toBe("skill::refund-replies");
  });

  it("lists runs newest first and offers review only for a ready candidate", async () => {
    serveJobs([
      job({
        job_id: "RFN-old",
        status: "applied",
        created_at: "2026-10-01T00:00:00Z",
      }),
      job({
        job_id: "RFN-new",
        status: "candidate_ready",
        created_at: "2026-10-13T00:00:00Z",
      }),
      job({
        job_id: "RFN-run",
        status: "running",
        created_at: "2026-10-12T00:00:00Z",
      }),
    ]);
    renderPanel();
    await screen.findByTestId("skill-job-RFN-new");
    const order = screen
      .getAllByTestId(/^skill-job-RFN/)
      .map((node) => node.getAttribute("data-testid"));
    expect(order).toEqual([
      "skill-job-RFN-new",
      "skill-job-RFN-run",
      "skill-job-RFN-old",
    ]);
    expect(screen.getByTestId("skill-review-RFN-new")).toBeInTheDocument();
    expect(
      screen.queryByTestId("skill-review-RFN-old"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("skill-review-RFN-run"),
    ).not.toBeInTheDocument();
  });

  it("badges simulated evidence and reports the real example count", async () => {
    serveJobs([job()]);
    renderPanel();
    fireEvent.click(await screen.findByTestId("skill-review-RFN-1"));

    const banner = await screen.findByTestId("skill-simulated-evaluation");
    expect(banner).toHaveTextContent("Simulated evaluation — not evidence");
    expect(banner).toHaveTextContent("fake evaluator");
    expect(
      screen.getByText(/3 evaluation examples · DS-1 · gate passed/),
    ).toBeInTheDocument();
    expect(screen.getByText("0.88")).toBeInTheDocument();
    expect(screen.getByText("0.94")).toBeInTheDocument();
    expect(screen.getByText("+0.06")).toBeInTheDocument();
    expect(screen.getByTestId("skill-candidate-diff")).toHaveTextContent(
      "Offer a refund.",
    );
  });

  it("shows no simulated badge for measured evidence", async () => {
    serveJobs([
      job({
        eval_results: {
          baseline: { overall: 0.5 },
          candidate: { overall: 0.7 },
          deltas: { overall: 0.2 },
          gate: { passed: true, reasons: [] },
          n_examples: 40,
          provider: "mlflow",
          simulated: false,
        },
      }),
    ]);
    renderPanel();
    fireEvent.click(await screen.findByTestId("skill-review-RFN-1"));
    await screen.findByTestId("skill-review-panel");
    expect(
      screen.queryByTestId("skill-simulated-evaluation"),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/40 evaluation examples/)).toBeInTheDocument();
  });

  it("applies the candidate, reports the new version and tells the page to reload", async () => {
    serveJobs([job()]);
    let applied = 0;
    server.use(
      http.post(`${API_BASE}/jobs/RFN-1/apply`, () => {
        applied += 1;
        return HttpResponse.json({
          data: {
            job_id: "RFN-1",
            status: "applied",
            simulated: false,
            promotion: { details: { old_version: 1, new_version: 2 } },
          },
        });
      }),
    );
    const onApplied = vi.fn();
    renderPanel({ onApplied });
    fireEvent.click(await screen.findByTestId("skill-review-RFN-1"));
    fireEvent.click(await screen.findByTestId("skill-apply-RFN-1"));

    const result = await screen.findByTestId("skill-apply-result");
    expect(result).toHaveTextContent("the skill is now version 2");
    expect(result).toHaveTextContent("Roll back from the version history");
    expect(applied).toBe(1);
    expect(onApplied).toHaveBeenCalledTimes(1);
  });

  it("shows the server's refusal and does not claim success", async () => {
    serveJobs([job()]);
    server.use(
      http.post(`${API_BASE}/jobs/RFN-1/apply`, () =>
        HttpResponse.json(
          {
            detail: "project role 'viewer' cannot perform release.apply",
            status_code: 403,
          },
          { status: 403 },
        ),
      ),
    );
    const onApplied = vi.fn();
    renderPanel({ onApplied });
    fireEvent.click(await screen.findByTestId("skill-review-RFN-1"));
    fireEvent.click(await screen.findByTestId("skill-apply-RFN-1"));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "cannot perform release.apply",
    );
    expect(screen.queryByTestId("skill-apply-result")).not.toBeInTheDocument();
    expect(onApplied).not.toHaveBeenCalled();
  });

  it("will not apply a candidate whose gate failed or whose content is missing", async () => {
    serveJobs([
      job({
        job_id: "RFN-gate",
        eval_results: {
          gate: { passed: false, reasons: ["regression on tone"] },
          simulated: false,
        },
      }),
      job({
        job_id: "RFN-empty",
        candidate: {},
        created_at: "2026-10-12T00:00:00Z",
      }),
    ]);
    renderPanel();

    fireEvent.click(await screen.findByTestId("skill-review-RFN-gate"));
    expect(await screen.findByText("regression on tone")).toBeInTheDocument();
    expect(screen.getByTestId("skill-apply-RFN-gate")).toBeDisabled();

    fireEvent.click(screen.getByTestId("skill-review-RFN-gate"));
    fireEvent.click(screen.getByTestId("skill-review-RFN-empty"));
    expect(
      await screen.findByText(/candidate content is unavailable/),
    ).toBeInTheDocument();
    expect(screen.getByTestId("skill-apply-RFN-empty")).toBeDisabled();
  });

  it("hides the review action from a caller who cannot operate", async () => {
    serveJobs([job()]);
    renderPanel({ canOperate: false });
    await screen.findByTestId("skill-job-RFN-1");
    expect(screen.queryByTestId("skill-review-RFN-1")).not.toBeInTheDocument();
  });

  it("surfaces a failed run's reason", async () => {
    serveJobs([
      job({
        status: "failed",
        error_message: "optimizer crashed",
        candidate: null,
      }),
    ]);
    renderPanel();
    expect(await screen.findByText("optimizer crashed")).toBeInTheDocument();
  });
});

describe("evidenceView", () => {
  it("reads a well-formed result", () => {
    expect(evidenceView(job())).toEqual({
      baseline: 0.88,
      candidate: 0.94,
      delta: 0.06,
      gatePassed: true,
      gateReasons: [],
      exampleCount: 3,
      datasetId: "DS-1",
      simulated: true,
      provider: "fake",
    });
  });

  it("treats missing or malformed fields as unknown, never as a pass or as measured", () => {
    const view = evidenceView(
      job({
        eval_results: { gate: "yes", simulated: "true", n_examples: "3" },
      }),
    );
    expect(view.gatePassed).toBeNull();
    expect(view.simulated).toBe(false);
    expect(view.exampleCount).toBeNull();
    expect(evidenceView(job({ eval_results: null })).baseline).toBeNull();
  });
});
