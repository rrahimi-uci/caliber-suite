import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { fireEvent } from "@testing-library/react";

import { render, screen, within } from "@/test/utils";

import { TraceMetricsCharts } from "@/components/observability/TraceMetricsCharts";

import type {
  ObservabilityMetricBucket,
  ObservabilityMetrics,
} from "@/api/workflowTypes";

// jsdom has no layout engine, so every element's getBoundingClientRect() /
// offsetWidth / offsetHeight are zeroed. Recharts' <ResponsiveContainer>
// measures its host div this way to decide how big to render; at 0x0 its
// internal chart components bail out before ever calling the tick/tooltip
// formatter callbacks (fmtTick included), so those branches never execute and
// the SVG body never mounts. Stubbing a real, non-zero layout (as an actual
// browser would report) lets the real recharts rendering — and mouse-driven
// tooltip — pipeline run end to end. This is the standard technique for
// exercising recharts under jsdom, not a shortcut around it.
let getBoundingClientRectSpy: ReturnType<typeof vi.spyOn>;
let offsetWidthSpy: ReturnType<typeof vi.spyOn>;
let offsetHeightSpy: ReturnType<typeof vi.spyOn>;

beforeAll(() => {
  getBoundingClientRectSpy = vi
    .spyOn(HTMLElement.prototype, "getBoundingClientRect")
    .mockReturnValue({
      width: 800,
      height: 300,
      top: 0,
      left: 0,
      right: 800,
      bottom: 300,
      x: 0,
      y: 0,
      toJSON() {
        return {};
      },
    } as DOMRect);
  // Recharts' mouse-tracking math divides by `element.offsetWidth` to derive
  // a device-pixel scale factor; jsdom always reports 0 for layout box
  // metrics, which turns that division into Infinity/NaN and silently drops
  // every hover. Stub it to match the bounding-rect size above.
  offsetWidthSpy = vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(800);
  offsetHeightSpy = vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(300);
});

afterAll(() => {
  getBoundingClientRectSpy.mockRestore();
  offsetWidthSpy.mockRestore();
  offsetHeightSpy.mockRestore();
});

/** Hovers the center of a mounted recharts `.recharts-wrapper` element so its
 * Tooltip becomes active, and returns the rendered tooltip's text content
 * (or `null` if no tooltip mounted for that chart). */
function hoverChart(wrapper: Element): string | null {
  const point = { clientX: 400, clientY: 150, pageX: 400, pageY: 150 };
  fireEvent.mouseOver(wrapper, point);
  fireEvent.mouseMove(wrapper, point);
  return wrapper.querySelector(".recharts-tooltip-wrapper")?.textContent ?? null;
}

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

  it("renders a single-bucket series without crashing", () => {
    // Guards the single-data-point edge case: recharts scales/domains can
    // degenerate when there's only one x value, and the chart must still
    // mount all four cards instead of throwing.
    render(<TraceMetricsCharts metrics={metrics({ buckets: [bucket()] })} />);

    const dashboard = screen.getByTestId("observability-metrics");
    expect(within(dashboard).getByText("Trace volume & errors")).toBeInTheDocument();
    expect(within(dashboard).getByText("Tokens & cost")).toBeInTheDocument();
  });

  it("formats hover tooltips per chart: time label, series names/values, and percent error rate", () => {
    // Each chart wires its own Tooltip `labelFormatter` (all four) and, on
    // the error-rate chart, a custom `formatter`. Those callbacks only run
    // once recharts has an active hover point — never during a static
    // render — so this is the only path that exercises them for real.
    const { container } = render(<TraceMetricsCharts metrics={metrics()} />);

    const wrappers = Array.from(container.querySelectorAll(".recharts-wrapper"));
    expect(wrappers).toHaveLength(4);

    const volumeChart = wrappers[0]!;
    const latencyChart = wrappers[1]!;
    const tokensChart = wrappers[2]!;
    const errorRateChart = wrappers[3]!;

    // fmtTick formats the bucket's `ts` (1_700_000_000_000) as a 24h HH:MM
    // label using the local timezone — assert against the same computation
    // the component uses rather than hardcoding a timezone-dependent string.
    const d = new Date(1_700_000_000_000);
    const pad = (n: number): string => String(n).padStart(2, "0");
    const expectedTimeLabel = `${pad(d.getHours())}:${pad(d.getMinutes())}`;

    const volumeText = hoverChart(volumeChart);
    expect(volumeText).toContain(expectedTimeLabel);
    expect(volumeText).toContain("Traces");
    expect(volumeText).toContain("Errors");

    const latencyText = hoverChart(latencyChart);
    expect(latencyText).toContain(expectedTimeLabel);
    expect(latencyText).toContain("p50");
    expect(latencyText).toContain("p95");

    const tokensText = hoverChart(tokensChart);
    expect(tokensText).toContain(expectedTimeLabel);
    expect(tokensText).toContain("Tokens");
    expect(tokensText).toContain("Cost ($)");

    // Error-rate chart's `formatter` renders the raw 0.2 bucket rate as a
    // one-decimal percent string ("20.0%"), distinct from the plain numeric
    // tooltips on the other three charts.
    const errorRateText = hoverChart(errorRateChart);
    expect(errorRateText).toContain(expectedTimeLabel);
    expect(errorRateText).toContain("20.0%");
  });

  it("renders a blank x-axis tick instead of crashing when a bucket has an invalid timestamp", () => {
    // fmtTick guards against an unparseable `ts` (`Number.isNaN(d.getTime())`)
    // by rendering an empty label rather than "Invalid Date" / "NaN:NaN".
    // Malformed timestamps shouldn't reach this component in practice, but if
    // the backend ever sends one the chart must degrade gracefully.
    const { container } = render(
      <TraceMetricsCharts
        metrics={metrics({
          buckets: [bucket({ ts: NaN }), bucket({ ts: NaN, count: 8 })],
        })}
      />,
    );

    expect(screen.getByTestId("observability-metrics")).toBeInTheDocument();
    const tickValues = Array.from(
      container.querySelectorAll(".recharts-cartesian-axis-tick-value"),
    ).map((el) => el.textContent);
    // Every x-axis tick (one per chart) rendered blank instead of "NaN:NaN".
    expect(tickValues.some((t) => t === "")).toBe(true);
    expect(tickValues.every((t) => !t?.includes("NaN"))).toBe(true);
  });
});
