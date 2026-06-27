import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import type { GatewayUsage } from "@/api/types";
import { GatewayUsageTab } from "@/components/gateway/GatewayUsageTab";
import { render, screen, userEvent, waitFor, within } from "@/test/utils";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

const POPULATED: GatewayUsage = {
  buckets: [
    {
      ts: 1_700_000_000_000,
      count: 3,
      error_count: 1,
      error_rate: 0.33,
      p50_ms: 100,
      p95_ms: 200,
      tokens: 50,
      cost_usd: 0.004,
    },
    {
      ts: 1_700_000_060_000,
      count: 5,
      error_count: 0,
      error_rate: 0,
      p50_ms: 120,
      p95_ms: 240,
      tokens: 90,
      cost_usd: 1.25,
    },
  ],
  bucket_ms: 60_000,
  totals: {
    count: 8,
    error_rate: 0.125,
    p50_ms: 110,
    p95_ms: 220,
    tokens: 140,
    cost_usd: 1.31,
  },
  by_model: [
    { model: "gpt-4o", calls: 5, tokens: 90, cost_usd: 1.25 },
    { model: "gpt-4o-mini", calls: 3, tokens: 50, cost_usd: 0.004 },
  ],
};

const EMPTY: GatewayUsage = {
  buckets: [],
  bucket_ms: 60_000,
  totals: {
    count: 0,
    error_rate: 0,
    p50_ms: null,
    p95_ms: null,
    tokens: 0,
    cost_usd: 0,
  },
  by_model: [],
};

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("GatewayUsageTab", () => {
  it("shows the loading placeholder before data resolves", async () => {
    const deferred: { resolve: () => void } = { resolve: () => undefined };
    const gate = new Promise<void>((resolve) => {
      deferred.resolve = resolve;
    });
    server.use(
      http.get(`${API_BASE}/gateway/usage`, async () => {
        await gate;
        return HttpResponse.json(envelope(POPULATED));
      }),
    );

    render(<GatewayUsageTab />);

    expect(screen.getByText("Loading usage…")).toBeInTheDocument();

    deferred.resolve();
    await waitFor(() => {
      expect(screen.queryByText("Loading usage…")).not.toBeInTheDocument();
    });
    expect(await screen.findByTestId("gateway-usage-by-model")).toBeInTheDocument();
  });

  it("renders totals, charts, and the by-model table for a populated payload", async () => {
    server.use(
      http.get(`${API_BASE}/gateway/usage`, () => HttpResponse.json(envelope(POPULATED))),
    );

    render(<GatewayUsageTab />);

    // The by-model table renders every model row.
    const byModel = await screen.findByTestId("gateway-usage-by-model");
    expect(byModel.textContent).toContain("gpt-4o");
    expect(byModel.textContent).toContain("gpt-4o-mini");
    const dataRows = within(byModel).getAllByRole("row").slice(1); // drop header row
    expect(dataRows).toHaveLength(2);

    // Totals stats. count.toLocaleString() === "8", tokens === "140".
    const stats = within(screen.getByTestId("gateway-usage"));
    expect(stats.getByText("Requests")).toBeInTheDocument();
    expect(stats.getByText("8")).toBeInTheDocument();
    expect(stats.getByText("140")).toBeInTheDocument();
    // error_rate 0.125 → "12.5%"; latency p50 110 → "110 ms".
    expect(stats.getByText("12.5%")).toBeInTheDocument();
    expect(stats.getByText("110 ms")).toBeInTheDocument();
    expect(stats.getByText("220 ms")).toBeInTheDocument();
    // cost 1.31 → fmtCost uses toFixed(2) when >= 0.01 → "$1.31".
    expect(stats.getByText("$1.31")).toBeInTheDocument();

    // The chart cards render their titles (ResizeObserver is mocked).
    expect(screen.getByText("Request volume & errors")).toBeInTheDocument();
    expect(screen.getByText("Tokens & cost")).toBeInTheDocument();
    expect(screen.getByText("Latency (ms)")).toBeInTheDocument();
    // "Error rate" is both a totals-stat label and the fourth chart-card title.
    expect(screen.getAllByText("Error rate").length).toBeGreaterThanOrEqual(2);

    // No empty-state placeholder when buckets exist.
    expect(screen.queryByTestId("gateway-usage-empty")).not.toBeInTheDocument();
  });

  it("refetches with a new since_ms window when the time range changes", async () => {
    const sinceValues: (string | null)[] = [];
    server.use(
      http.get(`${API_BASE}/gateway/usage`, ({ request }) => {
        const url = new URL(request.url);
        sinceValues.push(url.searchParams.get("since_ms"));
        return HttpResponse.json(envelope(POPULATED));
      }),
    );

    const user = userEvent.setup();
    render(<GatewayUsageTab />);

    // Initial fetch (default range = 24h) lands.
    await screen.findByTestId("gateway-usage-by-model");
    expect(sinceValues).toHaveLength(1);
    const firstSince = Number(sinceValues[0]);
    expect(Number.isNaN(firstSince)).toBe(false);

    // Switch to the 1h window → a smaller lookback → a fresh fetch fires.
    await user.click(screen.getByRole("button", { name: "1h" }));
    await waitFor(() => expect(sinceValues).toHaveLength(2));
    const secondSince = Number(sinceValues[1]);
    // 1h window starts later (closer to now) than the 24h window.
    expect(secondSince).toBeGreaterThan(firstSince);

    // Switch to 7d → a larger lookback → another fetch with an earlier since.
    await user.click(screen.getByRole("button", { name: "7d" }));
    await waitFor(() => expect(sinceValues).toHaveLength(3));
    const thirdSince = Number(sinceValues[2]);
    expect(thirdSince).toBeLessThan(firstSince);
  });

  it("shows the empty-range placeholder and hides charts/table for a degraded payload", async () => {
    server.use(
      http.get(`${API_BASE}/gateway/usage`, () => HttpResponse.json(envelope(EMPTY))),
    );

    render(<GatewayUsageTab />);

    // Empty-state placeholder appears once data (with no buckets) resolves.
    expect(await screen.findByTestId("gateway-usage-empty")).toBeInTheDocument();

    // Charts are not rendered when there are no buckets.
    expect(screen.queryByText("Request volume & errors")).not.toBeInTheDocument();
    // The by-model table is hidden when there are no rows.
    expect(screen.queryByTestId("gateway-usage-by-model")).not.toBeInTheDocument();

    // Totals still render, with the null-latency dash and zeroed values.
    const stats = within(screen.getByTestId("gateway-usage"));
    expect(stats.getByText("Requests")).toBeInTheDocument();
    expect(stats.getAllByText("—").length).toBeGreaterThanOrEqual(2); // p50 + p95 null → "—"
    expect(stats.getByText("0.0%")).toBeInTheDocument(); // error rate
  });

  it("surfaces an API error in a red banner", async () => {
    server.use(
      http.get(`${API_BASE}/gateway/usage`, () =>
        HttpResponse.json({ detail: "usage backend unavailable" }, { status: 500 }),
      ),
    );

    render(<GatewayUsageTab />);

    await waitFor(() => {
      expect(screen.queryByText("Loading usage…")).not.toBeInTheDocument();
    });
    // No data → no totals/table, and the error banner is shown.
    expect(screen.queryByTestId("gateway-usage-by-model")).not.toBeInTheDocument();
    expect(screen.getByText(/usage backend unavailable/i)).toBeInTheDocument();
  });
});
