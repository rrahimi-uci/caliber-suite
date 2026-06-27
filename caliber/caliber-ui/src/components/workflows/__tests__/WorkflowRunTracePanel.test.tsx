import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { WorkflowRunTracePanel } from "@/components/workflows/WorkflowRunTracePanel";
import { render, screen, userEvent, waitFor, within } from "@/test/utils";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function span(overrides: Record<string, unknown> = {}) {
  return {
    span_id: "s-root",
    parent_id: null,
    name: "workflow.run",
    span_type: "CHAIN",
    start_time_ms: 0,
    end_time_ms: 4,
    duration_ms: 4,
    status: "OK",
    inputs: null,
    outputs: null,
    attributes: {},
    ...overrides,
  };
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("WorkflowRunTracePanel", () => {
  it("renders the span tree with badges, durations, and nesting", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-runs/WR-1/trace`, () =>
        HttpResponse.json(
          envelope({
            trace_id: "trace-1",
            mlflow_url: "http://localhost:5000/#/traces/trace-1",
            spans: [
              span(),
              span({
                span_id: "s-agent",
                parent_id: "s-root",
                name: "agent.greeter",
                span_type: "AGENT",
                duration_ms: 12.5,
                attributes: { "caliber.model": "gpt-4o" },
                outputs: "hello there",
              }),
              span({
                span_id: "s-tool",
                parent_id: "s-agent",
                name: "tool.lookup",
                span_type: "TOOL",
                duration_ms: 3,
                status: "OK",
              }),
            ],
          }),
        ),
      ),
    );

    render(<WorkflowRunTracePanel runId="WR-1" />);

    const rows = await screen.findAllByTestId("trace-span-row");
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent("workflow.run");
    expect(rows[1]).toHaveTextContent("agent.greeter");
    expect(rows[1]).toHaveTextContent("AGENT");
    expect(rows[2]).toHaveTextContent("tool.lookup");
    expect(rows[2]).toHaveTextContent("TOOL");

    // "Open in MLflow" deep-link is shown when mlflow_url is present.
    const link = screen.getByTestId("run-trace-mlflow-link");
    expect(link).toHaveAttribute("href", "http://localhost:5000/#/traces/trace-1");
  });

  it("expands a span to reveal outputs and attributes", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-runs/WR-2/trace`, () =>
        HttpResponse.json(
          envelope({
            trace_id: "trace-2",
            mlflow_url: null,
            spans: [
              span({
                span_id: "s-agent",
                name: "agent.greeter",
                span_type: "AGENT",
                outputs: "the answer is 42",
                attributes: { "caliber.model": "gpt-4o" },
              }),
            ],
          }),
        ),
      ),
    );

    render(<WorkflowRunTracePanel runId="WR-2" />);

    const row = await screen.findByTestId("trace-span-row");
    expect(screen.queryByTestId("trace-span-detail")).not.toBeInTheDocument();
    await userEvent.click(within(row).getByRole("button"));
    const detail = await screen.findByTestId("trace-span-detail");
    expect(detail).toHaveTextContent("the answer is 42");
    expect(detail).toHaveTextContent("caliber.model");
  });

  it("shows a friendly empty state when the run has no trace", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-runs/WR-empty/trace`, () =>
        HttpResponse.json(envelope({ trace_id: null, spans: [] })),
      ),
    );

    render(<WorkflowRunTracePanel runId="WR-empty" />);

    const empty = await screen.findByTestId("run-trace-empty");
    expect(empty).toHaveTextContent(/no trace spans/i);
    expect(screen.queryByTestId("trace-span-row")).not.toBeInTheDocument();
    expect(screen.queryByTestId("run-trace-mlflow-link")).not.toBeInTheDocument();
  });

  it("surfaces a load error", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-runs/WR-err/trace`, () =>
        HttpResponse.json(envelope({ detail: "boom" }), { status: 500 }),
      ),
    );

    render(<WorkflowRunTracePanel runId="WR-err" />);

    await waitFor(() =>
      expect(screen.getByTestId("run-trace-error")).toBeInTheDocument(),
    );
  });
});
