import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { Observability } from "@/pages/Observability";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const OBS = `${API_BASE}/observability`;
const NOW = Date.now();
const OLD = 1_600_000_000_000;

function envelope<T>(data: T): { data: T } {
  return { data };
}

const ALPHA = {
  trace_id: "tr-1",
  name: "agent.alpha",
  status: "OK",
  experiment_id: "1",
  experiment_name: "agents",
  session_id: "sess-1",
  user: "@me",
  request_preview: "hello alpha",
  response_preview: "hi",
  timestamp_ms: NOW,
  execution_time_ms: 1200,
  span_count: 2,
  tool_call_count: 1,
  total_tokens: 140,
  cost_usd: 0.0035,
};
const BETA = {
  trace_id: "tr-2",
  name: "agent.beta",
  status: "ERROR",
  experiment_id: "0",
  experiment_name: "Default",
  session_id: "sess-2",
  user: "@me",
  request_preview: "beta request",
  response_preview: "",
  timestamp_ms: OLD,
  execution_time_ms: 5,
  span_count: 1,
  tool_call_count: 0,
  total_tokens: null,
  cost_usd: null,
};

function detail(traceId: string) {
  const base = {
    trace_id: traceId,
    experiment_id: traceId === "tr-2" ? "0" : "1",
    session_id: traceId === "tr-2" ? "sess-2" : "sess-1",
    user: "@me",
    mlflow_url: "http://localhost:5000/#/traces/" + traceId,
    request_time_ms: NOW,
    tags: {},
    assessments: [],
    prompt_tokens: null,
    completion_tokens: null,
    cost_usd: null,
  };
  if (traceId === "tr-2") {
    return {
      ...base,
      name: "agent.beta",
      status: "ERROR",
      spans: [
        {
          span_id: "b1",
          parent_id: null,
          name: "beta.root",
          span_type: "AGENT",
          start_time_ms: 0,
          end_time_ms: 5,
          duration_ms: 5,
          status: "ERROR",
          inputs: null,
          outputs: null,
          attributes: {},
        },
      ],
      request: "REQ-2",
      response: "RESP-2",
      execution_time_ms: 5,
      total_tokens: null,
    };
  }
  return {
    ...base,
    name: "agent.alpha",
    status: "OK",
    spans: [
      {
        span_id: "r1",
        parent_id: null,
        name: "alpha.root",
        span_type: "AGENT",
        start_time_ms: 0,
        end_time_ms: 12,
        duration_ms: 12,
        status: "OK",
        inputs: null,
        outputs: "ok",
        attributes: { "caliber.tokens": 140 },
      },
      {
        span_id: "t1",
        parent_id: "r1",
        name: "tool.lookup",
        span_type: "TOOL",
        start_time_ms: 2,
        end_time_ms: 6,
        duration_ms: 4,
        status: "OK",
        inputs: null,
        outputs: null,
        attributes: {},
      },
    ],
    request: "REQ-1",
    response: "RESP-1",
    execution_time_ms: 1200,
    total_tokens: 140,
    prompt_tokens: 100,
    completion_tokens: 40,
    cost_usd: 0.0035,
    tags: { env: "dev" },
    assessments: [{ name: "relevance", value: 0.9, rationale: "looks good", source: "judge" }],
  };
}

let feedbackBody: Record<string, unknown> | null = null;
let fromTraceBody: Record<string, unknown> | null = null;

const EVAL_DATASETS = [
  {
    dataset_id: "ED-1",
    name: "factual-checks",
    description: "",
    owner: "@me",
    tags: [],
    status: "active",
    version: 2,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  },
];

const METRICS = {
  bucket_ms: 1000,
  totals: { count: 3, error_rate: 0.3333, p50_ms: 5, p95_ms: 9, tokens: 175, cost_usd: 0.01 },
  buckets: [
    { ts: 1000, count: 2, error_count: 1, error_rate: 0.5, p50_ms: 5, p95_ms: 9, tokens: 150, cost_usd: 0.008 },
    { ts: 2000, count: 1, error_count: 0, error_rate: 0, p50_ms: 4, p95_ms: 4, tokens: 25, cost_usd: 0.002 },
  ],
};

function useObservabilityHandlers(): void {
  feedbackBody = null;
  server.use(
    http.get(`${OBS}/metrics`, () => HttpResponse.json(envelope(METRICS))),
    http.post(`${OBS}/traces/:traceId/feedback`, async ({ request }) => {
      feedbackBody = (await request.json()) as Record<string, unknown>;
      return HttpResponse.json(
        envelope({
          assessments: [
            { name: "feedback", value: true, rationale: "great answer", source: "@me" },
          ],
        }),
      );
    }),
    http.get(`${OBS}/experiments`, () =>
      HttpResponse.json(
        envelope({
          experiments: [
            { experiment_id: "0", name: "Default" },
            { experiment_id: "1", name: "agents" },
          ],
        }),
      ),
    ),
    http.get(`${OBS}/traces`, ({ request }) => {
      const url = new URL(request.url);
      let list = [ALPHA, BETA];
      const status = url.searchParams.get("status");
      const exp = url.searchParams.get("experiment_id");
      const session = url.searchParams.get("session");
      const since = url.searchParams.get("since_ms");
      if (status) list = list.filter((t) => t.status === status);
      if (exp) list = list.filter((t) => t.experiment_id === exp);
      if (session) list = list.filter((t) => t.session_id === session);
      if (since) list = list.filter((t) => (t.timestamp_ms ?? 0) >= Number(since));
      return HttpResponse.json(envelope({ traces: list }));
    }),
    http.get(`${OBS}/traces/:traceId`, ({ params }) =>
      HttpResponse.json(envelope(detail(String(params.traceId)))),
    ),
    http.get(`${API_BASE}/eval-datasets`, () => HttpResponse.json(envelope(EVAL_DATASETS))),
    http.post(
      `${API_BASE}/eval-datasets/:datasetId/examples/from-trace`,
      async ({ request, params }) => {
        fromTraceBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            example_id: "EX-new",
            dataset_id: String(params.datasetId),
            dataset_version: 3,
            input: { input: "REQ-1" },
            expected: { expected: "RESP-1" },
            weight: 1,
            tags: ["from-trace", "trace:tr-1"],
            created_at: "2026-01-01T00:00:00Z",
            superseded_at: null,
          }),
          { status: 201 },
        );
      },
    ),
  );
}

function renderPage(initialPath = "/observability"): void {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        initialEntries={[initialPath]}
      >
        <Routes>
          <Route path="/observability" element={<Observability />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("Observability page", () => {
  it("lists traces with token/cost rollups", async () => {
    useObservabilityHandlers();
    renderPage();

    const rows = await screen.findAllByTestId("observability-trace-row");
    expect(rows).toHaveLength(2);
    const text = rows.map((r) => r.textContent ?? "").join("|");
    expect(text).toContain("agent.alpha");
    expect(text).toContain("agent.beta");
    expect(rows.some((r) => (r.textContent ?? "").includes("140 tok"))).toBe(true);
  });

  it("shows usage, tags, request, assessments, and identity chips", async () => {
    useObservabilityHandlers();
    renderPage();

    const usage = await screen.findByTestId("trace-usage");
    expect(within(usage).getByText("140")).toBeInTheDocument();
    expect(within(screen.getByTestId("trace-tags")).getByText(/env/)).toBeInTheDocument();
    expect(within(screen.getByTestId("trace-assessments")).getByText("relevance")).toBeInTheDocument();
    expect(within(screen.getByTestId("trace-identity")).getByText("agents")).toBeInTheDocument();
    expect(screen.getByText("REQ-1")).toBeInTheDocument();
    expect((await screen.findAllByTestId("trace-span-row")).length).toBe(2);
  });

  it("switches the span view to the timeline waterfall", async () => {
    useObservabilityHandlers();
    renderPage();

    await screen.findByTestId("trace-usage");
    expect(screen.queryByTestId("trace-timeline")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Timeline" }));
    expect(await screen.findByTestId("trace-timeline")).toBeInTheDocument();
    expect(screen.getAllByTestId("trace-timeline-row").length).toBe(2);
    expect(screen.queryByTestId("trace-span-row")).not.toBeInTheDocument();
  });

  it("filters the list by search text", async () => {
    useObservabilityHandlers();
    renderPage();

    await screen.findAllByTestId("observability-trace-row");
    await userEvent.type(screen.getByLabelText("Search traces"), "beta");
    await waitFor(() => {
      const rows = screen.getAllByTestId("observability-trace-row");
      expect(rows).toHaveLength(1);
      expect(rows[0].textContent).toContain("agent.beta");
    });
  });

  it("refetches when the status filter changes", async () => {
    useObservabilityHandlers();
    renderPage();

    await screen.findAllByTestId("observability-trace-row");
    await userEvent.selectOptions(screen.getByLabelText("Filter by status"), "ERROR");
    await waitFor(() => {
      const rows = screen.getAllByTestId("observability-trace-row");
      expect(rows).toHaveLength(1);
      expect(rows[0].textContent).toContain("agent.beta");
    });
  });

  it("filters by experiment", async () => {
    useObservabilityHandlers();
    renderPage();

    await screen.findAllByTestId("observability-trace-row");
    await userEvent.selectOptions(screen.getByTestId("observability-experiment-select"), "1");
    await waitFor(() => {
      const rows = screen.getAllByTestId("observability-trace-row");
      expect(rows).toHaveLength(1);
      expect(rows[0].textContent).toContain("agent.alpha");
    });
  });

  it("filters by time range", async () => {
    useObservabilityHandlers();
    renderPage();

    await screen.findAllByTestId("observability-trace-row");
    await userEvent.selectOptions(screen.getByLabelText("Time range"), "1h");
    await waitFor(() => {
      const rows = screen.getAllByTestId("observability-trace-row");
      expect(rows).toHaveLength(1);
      expect(rows[0].textContent).toContain("agent.alpha");
    });
  });

  it("drills into a session and clears it", async () => {
    useObservabilityHandlers();
    renderPage();

    const chips = await screen.findAllByTestId("observability-session-chip");
    await userEvent.click(chips[0]); // sess-1 (alpha)
    expect(await screen.findByTestId("observability-session-filter")).toBeInTheDocument();
    await waitFor(() => {
      const rows = screen.getAllByTestId("observability-trace-row");
      expect(rows).toHaveLength(1);
      expect(rows[0].textContent).toContain("agent.alpha");
    });

    await userEvent.click(screen.getByLabelText("Clear session filter"));
    await waitFor(() => {
      expect(screen.getAllByTestId("observability-trace-row")).toHaveLength(2);
    });
  });

  it("deep-links to a trace via ?trace=", async () => {
    useObservabilityHandlers();
    renderPage("/observability?trace=tr-2");

    expect(await screen.findByText("REQ-2")).toBeInTheDocument();
  });

  it("submits human feedback on a trace", async () => {
    useObservabilityHandlers();
    renderPage();

    const widget = await screen.findByTestId("trace-feedback");
    await userEvent.click(within(widget).getByLabelText("Helpful"));
    await userEvent.type(within(widget).getByLabelText("Feedback note"), "great answer");
    await userEvent.click(within(widget).getByRole("button", { name: /Submit/ }));

    await waitFor(() => {
      expect(feedbackBody).toEqual({
        name: "feedback",
        value: true,
        rationale: "great answer",
      });
    });
  });

  it("captures a trace into a test set", async () => {
    useObservabilityHandlers();
    renderPage();

    const widget = await screen.findByTestId("trace-add-to-dataset");
    // Wait for the datasets query to populate the picker before selecting.
    await within(widget).findByRole("option", { name: /factual-checks/ });
    await userEvent.selectOptions(
      within(widget).getByLabelText("Choose test set"),
      "ED-1",
    );
    await userEvent.click(within(widget).getByRole("button", { name: /Add example/ }));

    await waitFor(() => {
      expect(fromTraceBody).toEqual({ trace_id: "tr-1" });
    });
  });

  it("shows the monitoring dashboard with metrics", async () => {
    useObservabilityHandlers();
    renderPage();

    await screen.findAllByTestId("observability-trace-row");
    await userEvent.click(screen.getByRole("button", { name: "Monitor" }));
    const metrics = await screen.findByTestId("observability-metrics");
    expect(within(metrics).getByText("Traces")).toBeInTheDocument();
    // Totals strip renders the aggregate count.
    expect(within(metrics).getByText("3")).toBeInTheDocument();
  });

  it("compares two traces side by side", async () => {
    useObservabilityHandlers();
    renderPage();

    const rows = await screen.findAllByTestId("observability-trace-row");
    await userEvent.click(screen.getByRole("button", { name: "Compare" }));
    await userEvent.click(rows[0]);
    await userEvent.click(rows[1]);

    const compare = await screen.findByTestId("trace-compare");
    expect(within(compare).getByText("agent.alpha")).toBeInTheDocument();
    expect(within(compare).getByText("agent.beta")).toBeInTheDocument();
  });
});
