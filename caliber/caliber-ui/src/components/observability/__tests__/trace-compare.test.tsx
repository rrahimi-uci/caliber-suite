import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { TraceCompare } from "@/components/observability/TraceCompare";
import { render, screen } from "@/test/utils";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function trace(id: string, overrides: Record<string, unknown> = {}) {
  return {
    trace_id: id,
    name: id === "a" ? "alpha" : "beta",
    status: id === "a" ? "OK" : "ERROR",
    execution_time_ms: id === "a" ? 5 : 12,
    total_tokens: id === "a" ? 10 : null,
    cost_usd: id === "a" ? 0.004 : 0.02,
    response: id === "a" ? "alpha response" : { answer: "beta" },
    spans: [
      {
        span_id: `${id}-root`,
        parent_id: null,
        name: `${id} root`,
        span_type: "AGENT",
        start_time_ms: 0,
        end_time_ms: 5,
        duration_ms: 5,
        status: "OK",
        inputs: null,
        outputs: null,
        attributes: {},
      },
      {
        span_id: `${id}-tool`,
        parent_id: `${id}-root`,
        name: `${id} tool`,
        span_type: "TOOL",
        start_time_ms: 1,
        end_time_ms: 3,
        duration_ms: 2,
        status: "OK",
        inputs: null,
        outputs: null,
        attributes: {},
      },
    ],
    ...overrides,
  };
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("TraceCompare", () => {
  it("renders loading and API failure states", async () => {
    server.use(
      http.get(`${API_BASE}/observability/traces/a`, () =>
        HttpResponse.json({ detail: "trace unavailable" }, { status: 503 }),
      ),
      http.get(`${API_BASE}/observability/traces/b`, () =>
        HttpResponse.json(envelope(trace("b"))),
      ),
    );
    render(<TraceCompare idA="a" idB="b" />);
    expect(await screen.findByText("trace unavailable")).toBeInTheDocument();
  });

  it("compares numeric deltas, nulls, responses, and tool spans", async () => {
    server.use(
      http.get(`${API_BASE}/observability/traces/a`, () =>
        HttpResponse.json(envelope(trace("a"))),
      ),
      http.get(`${API_BASE}/observability/traces/b`, () =>
        HttpResponse.json(
          envelope(
            trace("b", {
              total_tokens: 20,
              response: null,
            }),
          ),
        ),
      ),
    );
    render(<TraceCompare idA="a" idB="b" />);
    const compare = await screen.findByTestId("trace-compare");
    expect(compare).toHaveTextContent("alpha");
    expect(compare).toHaveTextContent("beta");
    expect(compare).toHaveTextContent("+7");
    expect(compare).toHaveTextContent("$0.02");
    expect(compare).toHaveTextContent("alpha response");
    expect(compare).toHaveTextContent("a tool");
  });
});
