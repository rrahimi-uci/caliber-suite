import { describe, expect, it } from "vitest";

import { render, screen, within } from "@/test/utils";

import { TraceMetricsCharts } from "@/components/observability/TraceMetricsCharts";

import type {
  ObservabilityMetricBucket,
  ObservabilityMetrics,
} from "@/api/workflowTypes";

function bucket(over: Partial<ObservabilityMetricBucket> = {}): ObservabilityMetricBucket {
  return {
    ts: 1_700_000_000_000,
    count: 5,
    error_count: 1,
    error_rate: 0.2,
    p50_ms: 120,
    p95_ms: 340,
    tokens: 1500,
    cost_usd: 0.42,
    ...over,
  };
}

function metrics(over: Partial<ObservabilityMetrics> = {}): ObservabilityMetrics {
  return {
    buckets: [
      bucket({ ts: 1_700_000_000_000 }),
      bucket({ ts: 1_700_000_060_000, count: 8, error_count: 0, error_rate: 0 }),
    ],
    bucket_ms: 60_000,
    totals: {
      count: 13,
      error_rate: 0.1,
      p50_ms: 130,
      p95_ms: 350,
      tokens: 3000,
      cost_usd: 1.23,
    },
    ...over,
  };
}

describe("TraceMetricsCharts", () => {
  it("renders the empty state when there are no buckets", () => {
    render(<TraceMetricsCharts metrics={metrics({ buckets: [] })} />);

    expect(screen.getByTestId("observability-metrics-empty")).toBeInTheDocument();
    expect(
      screen.getByText(/No metrics in this range/i),
    ).toBeInTheDocument();
    // The populated dashboard must NOT mount in the empty branch.
    expect(screen.queryByTestId("observability-metrics")).not.toBeInTheDocument();
  });

  it("renders the stat tiles and all four chart cards for populated metrics", () => {
    render(<TraceMetricsCharts metrics={metrics()} />);

    const dashboard = screen.getByTestId("observability-metrics");
    expect(dashboard).toBeInTheDocument();
    // Empty state must not appear when buckets exist.
    expect(
      screen.queryByTestId("observability-metrics-empty"),
    ).not.toBeInTheDocument();

    // Stat labels (each appears at least once). "Error rate" is also a chart
    // title, so assert presence via getAllByText to tolerate the duplicate.
    for (const label of ["Traces", "p50", "p95", "Tokens", "Cost"]) {
      expect(within(dashboard).getByText(label)).toBeInTheDocument();
    }
    expect(within(dashboard).getAllByText("Error rate").length).toBeGreaterThanOrEqual(1);

    // Chart card titles (unique strings prove each ChartCard wrapper rendered).
    expect(screen.getByText("Trace volume & errors")).toBeInTheDocument();
    expect(screen.getByText("Latency (ms)")).toBeInTheDocument();
    expect(screen.getByText("Tokens & cost")).toBeInTheDocument();
    // "Error rate" appears as both a stat label and a chart title.
    expect(screen.getAllByText("Error rate").length).toBeGreaterThanOrEqual(2);
  });

  it("formats the totals: locale counts, percent error rate, rounded latency, dollar cost", () => {
    render(
      <TraceMetricsCharts
        metrics={metrics({
          totals: {
            count: 12_345,
            error_rate: 0.0567,
            p50_ms: 130.6,
            p95_ms: 349.2,
            tokens: 98_765,
            cost_usd: 7.5,
          },
        })}
      />,
    );

    const dashboard = screen.getByTestId("observability-metrics");
    expect(within(dashboard).getByText((12_345).toLocaleString())).toBeInTheDocument();
    expect(within(dashboard).getByText("5.7%")).toBeInTheDocument();
    expect(within(dashboard).getByText("131 ms")).toBeInTheDocument();
    expect(within(dashboard).getByText("349 ms")).toBeInTheDocument();
    expect(within(dashboard).getByText((98_765).toLocaleString())).toBeInTheDocument();
    // cost >= 0.01 → 2 decimals.
    expect(within(dashboard).getByText("$7.50")).toBeInTheDocument();
  });

  it("shows an em dash for null latency percentiles and 4 decimals for sub-cent cost", () => {
    render(
      <TraceMetricsCharts
        metrics={metrics({
          totals: {
            count: 0,
            error_rate: 0,
            p50_ms: null,
            p95_ms: null,
            tokens: 0,
            cost_usd: 0.0042,
          },
        })}
      />,
    );

    const dashboard = screen.getByTestId("observability-metrics");
    // Both p50 and p95 fall back to the em dash.
    expect(within(dashboard).getAllByText("—")).toHaveLength(2);
    // cost < 0.01 → 4 decimals.
    expect(within(dashboard).getByText("$0.0042")).toBeInTheDocument();
    expect(within(dashboard).getByText("0.0%")).toBeInTheDocument();
  });
});
