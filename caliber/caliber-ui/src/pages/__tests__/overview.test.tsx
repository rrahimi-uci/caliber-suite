import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, caliberApi } from "@/api/caliberApi";
import type { DashboardSummary } from "@/api/types";
import { DashboardSummaryProvider } from "@/components/DashboardSummaryContext";
import { Dashboard } from "@/pages/Overview";

function summary(overrides: Partial<DashboardSummary> = {}): DashboardSummary {
  return {
    agents_total: 5,
    agents_enabled: 4,
    verification_pending: 8,
    verification_pending_critical: 2,
    jobs_queued: 3,
    jobs_running: 2,
    jobs_awaiting_approval: 4,
    jobs_completed: 21,
    jobs_failed: 1,
    jobs_rejected: 0,
    approvals_pending: 4,
    assistant_slo: {
      intent_confidence_avg: 0.86,
      plans_total: 20,
      plans_ready: 16,
      plan_readiness_rate: 0.8,
      clarification_rate: 0.12,
      executions_total: 18,
      executions_completed: 16,
      executions_failed: 2,
      executions_blocked: 1,
      execution_success_rate: 0.89,
      adapter_error_classes: {},
      publish_total: 10,
      publish_success: 9,
      publish_failed: 1,
      publish_success_rate: 0.9,
    },
    generated_at: "2026-06-07T16:00:00Z",
    ...overrides,
  };
}

function renderDashboard({
  data = summary(),
  loading = false,
  error = null,
  refresh = vi.fn(),
}: {
  data?: DashboardSummary | null;
  loading?: boolean;
  error?: ApiError | null;
  refresh?: () => void;
} = {}): ReturnType<typeof render> {
  return render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <DashboardSummaryProvider value={{ data, loading, error, refresh }}>
        <Dashboard />
      </DashboardSummaryProvider>
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Dashboard", () => {
  it("renders key operational metrics and priority workspace links", async () => {
    const refresh = vi.fn();
    const user = userEvent.setup();

    renderDashboard({ refresh });

    expect(screen.getByRole("heading", { name: "Dashboard" })).toBeInTheDocument();
    expect(screen.getByText("CALIBER Operations")).toBeInTheDocument();
    expect(screen.getByText("2 critical · 6 standard")).toBeInTheDocument();
    expect(screen.getByText("4/5 agents enabled")).toBeInTheDocument();

    const promptsTile = screen.getByText("Prompts").closest("a");
    const workflowsTile = screen.getByText("Workflows").closest("a");
    if (!promptsTile || !workflowsTile) {
      throw new Error("Expected dashboard action tiles to render anchor links.");
    }
    expect(promptsTile).toHaveAttribute("href", "/prompts");
    expect(workflowsTile).toHaveAttribute("href", "/workflows");

    await user.click(screen.getByRole("button", { name: "Refresh" }));
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("shows error and retry UI when dashboard data fails to load", async () => {
    const refresh = vi.fn();
    const user = userEvent.setup();

    renderDashboard({
      data: null,
      loading: true,
      error: new ApiError(500, "dashboard unavailable", {
        detail: "dashboard unavailable",
        status_code: 500,
      }),
      refresh,
    });

    expect(await screen.findByText("Failed to load dashboard")).toBeInTheDocument();
    expect(screen.getByText("dashboard unavailable")).toBeInTheDocument();
    expect(screen.getAllByText("Awaiting signal").length).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("renders recent job activity as informational rows", async () => {
    vi.spyOn(caliberApi, "listJobs").mockResolvedValue([
      {
        job_id: "JOB-1",
        agent_id: "billing-agent",
        status: "failed",
        current_stage: "evaluation",
        artifact_type: "prompt",
        updated_at: "2026-06-07T16:02:00Z",
      },
    ] as never);

    renderDashboard();

    const row = await screen.findByText("billing-agent · failed");
    expect(row).toBeInTheDocument();
    expect(screen.getAllByText("Job").length).toBeGreaterThan(0);
    // Generic job rows are informational here and are no longer links.
    expect(row.closest("a")).toBeNull();
  });

  it("surfaces workflow calibration activity and delivery-lane summaries", async () => {
    vi.spyOn(caliberApi, "listVerificationItems").mockResolvedValue([] as never);
    vi.spyOn(caliberApi, "listJobs").mockResolvedValue([
      {
        job_id: "JOB-WF-1",
        agent_id: "support-agent",
        workflow_id: "WF-42",
        status: "candidate_ready",
        current_stage: "eval",
        artifact_type: "workflow_manifest",
        updated_at: "2026-06-07T16:04:00Z",
        candidate: {
          target_alias: "prod",
          calibration_low_confidence: true,
          calibration_candidates: [
            {
              candidate_id: "cal-1",
              accepted: true,
            },
          ],
        },
        eval_results: {
          gate: { passed: false, reasons: ["needs more evidence"] },
        },
        calibration_spec: {
          objective: { maximize: "quality" },
          budget: { max_candidates: 3 },
          judge: { enabled: true },
        },
      },
    ] as never);

    renderDashboard();

    expect(await screen.findByRole("heading", { name: "Workflow Delivery Lane" })).toBeInTheDocument();
    expect(screen.getByTestId("workflow-delivery-panel")).toHaveTextContent("Active calibrations");
    expect(screen.getByTestId("workflow-delivery-panel")).toHaveTextContent("Candidate ready");
    expect(screen.getByTestId("workflow-delivery-panel")).toHaveTextContent("Gate blocked");
    expect(screen.getByTestId("workflow-delivery-panel")).toHaveTextContent("LLM judge");
    expect(screen.getByTestId("workflow-delivery-panel")).toHaveTextContent("WF-42");
    expect(screen.getByTestId("workflow-delivery-panel")).toHaveTextContent("Candidate ready");
    expect(screen.getByTestId("workflow-delivery-panel")).toHaveTextContent("alias prod");
    // Workflow calibration now lives on the workflow detail page.
    expect(screen.getByRole("link", { name: /WF-42 · Candidate ready/i })).toHaveAttribute(
      "href",
      "/workflows/WF-42",
    );
    expect(screen.getAllByText("Workflow").length).toBeGreaterThan(0);
  });

  it("shows recent activity errors and retries activity fetchers", async () => {
    const refresh = vi.fn();
    const user = userEvent.setup();
    const listJobs = vi.spyOn(caliberApi, "listJobs").mockRejectedValue(new Error("jobs down"));

    renderDashboard({ refresh });

    expect(await screen.findByText("jobs down")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => {
      expect(refresh).toHaveBeenCalledTimes(1);
      expect(listJobs).toHaveBeenCalledTimes(2);
    });
  });
});
