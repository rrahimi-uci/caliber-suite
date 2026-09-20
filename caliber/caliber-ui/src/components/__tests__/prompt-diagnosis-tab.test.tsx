/**
 * PromptDiagnosisTab — the per-prompt Workspace surface that lets an
 * operator open a flagged verification-queue item, see why it was flagged,
 * and follow the linked refinement job's pipeline progress into Calibration
 * (`docs/two-week-alpha-plan.md`'s journey, step 1-2). Covers: the empty
 * state, rendering a flagged item's reason/severity/status, the linked-job
 * pipeline summary (with and without a match), the no-job fallback, the
 * pending-item Verify/Dismiss actions, and the load-error path.
 */

import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { PromptDiagnosisTab } from "@/components/PromptDiagnosisTab";
import type { PromptInfo } from "@/api/types";
import { render, screen, userEvent, waitFor, within } from "@/test/utils";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

const prompt: PromptInfo = {
  agent_id: "intake-classifier",
  agent_name: "Intake Classifier",
  agent_enabled: true,
  prompt_name: "intake-classifier",
  version: 1,
  alias: "prod",
  available_aliases: ["prod"],
  template_preview: "Classify the incoming support ticket.",
  template_length: 40,
  approval_id: null,
  artifact_ref: "prompts:/intake-classifier@prod",
  has_prompt: true,
  needs_prompt: false,
  source: "both",
};

function verificationItem(overrides: Record<string, unknown> = {}) {
  return {
    item_id: "VQ-1",
    agent_id: "intake-classifier",
    project_id: null,
    assessment_id: null,
    trace_id: "trace-flagged-1",
    experiment_id: null,
    session_id: null,
    workflow_id: null,
    category: "Missed compound-request review",
    free_text:
      "Classified a compound ticket as a single billing issue and set needs_review=false.",
    severity: "critical",
    artifact_type_hint: "prompt",
    artifact_ref: null,
    submitted_context: null,
    status: "verified",
    priority: 1,
    assigned_to: null,
    verified_by: "@day1-seed-fixture",
    verified_at: "2026-09-19T00:00:00Z",
    verification_notes: null,
    refinement_target: null,
    duplicate_of_id: null,
    created_at: "2026-09-19T00:00:00Z",
    ...overrides,
  };
}

function refinementJob(overrides: Record<string, unknown> = {}) {
  return {
    job_id: "JOB-1",
    agent_id: "intake-classifier",
    workflow_id: null,
    primary_item_id: "VQ-1",
    mlflow_run_id: null,
    artifact_type: "prompt",
    optimizer_type: "DSPyBootstrapFewShot",
    status: "candidate_ready",
    current_stage: "eval",
    attempt_count: 1,
    error_message: null,
    total_tokens: 100,
    cost_usd: 0.01,
    bundle_targets: [],
    bundle_expansion_count: 1,
    diagnosis: { summary: "Ambiguity not detected." },
    candidate: { content: "Improved prompt" },
    eval_results: {
      candidate: { overall: 0.92 },
      gate: { passed: true, reasons: [] },
    },
    calibration_spec: null,
    created_at: "2026-09-19T00:00:00Z",
    updated_at: "2026-09-19T00:05:00Z",
    ...overrides,
  };
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
});
afterAll(() => server.close());

describe("PromptDiagnosisTab", () => {
  it("shows an empty state when the agent has no flagged items", async () => {
    server.use(
      http.get(`${API_BASE}/verification-queue`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/jobs`, () => HttpResponse.json(envelope([]))),
    );

    render(
      <PromptDiagnosisTab prompt={prompt} onOpenCalibration={vi.fn()} />,
    );

    expect(
      await screen.findByText(/No flagged items for this prompt yet/i),
    ).toBeInTheDocument();
  });

  it("surfaces a load error", async () => {
    server.use(
      http.get(`${API_BASE}/verification-queue`, () =>
        HttpResponse.json({ detail: "verification queue offline" }, { status: 500 }),
      ),
      http.get(`${API_BASE}/jobs`, () => HttpResponse.json(envelope([]))),
    );

    render(
      <PromptDiagnosisTab prompt={prompt} onOpenCalibration={vi.fn()} />,
    );

    expect(
      await screen.findByText(/verification queue offline/i),
    ).toBeInTheDocument();
  });

  it("opens a flagged item, shows why it was flagged, and the linked job's pipeline progress", async () => {
    server.use(
      http.get(`${API_BASE}/verification-queue`, () =>
        HttpResponse.json(envelope([verificationItem()])),
      ),
      http.get(`${API_BASE}/jobs`, () =>
        HttpResponse.json(envelope([refinementJob()])),
      ),
    );
    const onOpenCalibration = vi.fn();

    render(
      <PromptDiagnosisTab prompt={prompt} onOpenCalibration={onOpenCalibration} />,
    );

    const detail = await screen.findByTestId("diagnosis-item-detail");
    expect(
      within(detail).getByText(
        /Classified a compound ticket as a single billing issue/i,
      ),
    ).toBeInTheDocument();
    expect(within(detail).getByText("trace-flagged-1")).toBeInTheDocument();

    // Linked job renders its pipeline progress and eval evidence.
    expect(screen.getByTestId("diagnosis-linked-job")).toBeInTheDocument();
    expect(screen.getByText(/92\.0%/)).toBeInTheDocument();
    expect(screen.getByText(/Gate passed/i)).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(
      screen.getByTestId("diagnosis-open-calibration-btn"),
    );
    expect(onOpenCalibration).toHaveBeenCalledTimes(1);
  });

  it("falls back to a no-job message when no job is linked to the item", async () => {
    server.use(
      http.get(`${API_BASE}/verification-queue`, () =>
        HttpResponse.json(envelope([verificationItem()])),
      ),
      http.get(`${API_BASE}/jobs`, () => HttpResponse.json(envelope([]))),
    );

    render(
      <PromptDiagnosisTab prompt={prompt} onOpenCalibration={vi.fn()} />,
    );

    expect(await screen.findByTestId("diagnosis-item-detail")).toBeInTheDocument();
    expect(screen.queryByTestId("diagnosis-linked-job")).not.toBeInTheDocument();
    expect(
      screen.getByText(/No refinement job is linked to this item yet/i),
    ).toBeInTheDocument();
  });

  it("verifies a pending item and refreshes, replacing the Verify/Dismiss actions", async () => {
    let currentStatus = "pending";
    server.use(
      http.get(`${API_BASE}/verification-queue`, () =>
        HttpResponse.json(
          envelope([verificationItem({ status: currentStatus })]),
        ),
      ),
      http.get(`${API_BASE}/jobs`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/verification-queue/:itemId/verify`, () => {
        currentStatus = "verified";
        return HttpResponse.json(
          envelope({
            item: verificationItem({ status: "verified" }),
            job: null,
          }),
        );
      }),
    );

    const user = userEvent.setup();
    render(
      <PromptDiagnosisTab prompt={prompt} onOpenCalibration={vi.fn()} />,
    );

    await screen.findByTestId("diagnosis-verify-btn");
    await user.click(screen.getByTestId("diagnosis-verify-btn"));

    await waitFor(() =>
      expect(
        screen.queryByTestId("diagnosis-verify-btn"),
      ).not.toBeInTheDocument(),
    );
  });

  it("dismisses a pending item", async () => {
    let dismissed = false;
    server.use(
      http.get(`${API_BASE}/verification-queue`, () =>
        HttpResponse.json(
          envelope([
            verificationItem({ status: dismissed ? "dismissed" : "pending" }),
          ]),
        ),
      ),
      http.get(`${API_BASE}/jobs`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/verification-queue/:itemId/dismiss`, () => {
        dismissed = true;
        return HttpResponse.json(
          envelope(verificationItem({ status: "dismissed" })),
        );
      }),
    );

    const user = userEvent.setup();
    render(
      <PromptDiagnosisTab prompt={prompt} onOpenCalibration={vi.fn()} />,
    );

    await screen.findByTestId("diagnosis-dismiss-btn");
    await user.click(screen.getByTestId("diagnosis-dismiss-btn"));

    await waitFor(() =>
      expect(
        screen.queryByTestId("diagnosis-dismiss-btn"),
      ).not.toBeInTheDocument(),
    );
  });

  it("surfaces a verify action error without losing the item detail", async () => {
    server.use(
      http.get(`${API_BASE}/verification-queue`, () =>
        HttpResponse.json(
          envelope([verificationItem({ status: "pending" })]),
        ),
      ),
      http.get(`${API_BASE}/jobs`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/verification-queue/:itemId/verify`, () =>
        HttpResponse.json({ detail: "verify failed" }, { status: 500 }),
      ),
    );

    const user = userEvent.setup();
    render(
      <PromptDiagnosisTab prompt={prompt} onOpenCalibration={vi.fn()} />,
    );

    await screen.findByTestId("diagnosis-verify-btn");
    await user.click(screen.getByTestId("diagnosis-verify-btn"));

    expect(await screen.findByText(/verify failed/i)).toBeInTheDocument();
    // The item is still selected/rendered — a failed action doesn't blank the panel.
    expect(screen.getByTestId("diagnosis-item-detail")).toBeInTheDocument();
  });

  it("switches the selected item when a different row is clicked", async () => {
    const second = verificationItem({
      item_id: "VQ-2",
      category: "Second flagged concern",
      free_text: "A different reason entirely.",
      created_at: "2026-09-18T00:00:00Z",
    });
    server.use(
      http.get(`${API_BASE}/verification-queue`, () =>
        HttpResponse.json(envelope([verificationItem(), second])),
      ),
      http.get(`${API_BASE}/jobs`, () => HttpResponse.json(envelope([]))),
    );

    const user = userEvent.setup();
    render(
      <PromptDiagnosisTab prompt={prompt} onOpenCalibration={vi.fn()} />,
    );

    await screen.findByTestId("diagnosis-item-detail");
    expect(
      screen.getByText(/Classified a compound ticket/i),
    ).toBeInTheDocument();

    await user.click(screen.getByTestId("diagnosis-item-row-VQ-2"));

    expect(
      await screen.findByText(/A different reason entirely/i),
    ).toBeInTheDocument();
  });
});
