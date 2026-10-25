import { describe, expect, it } from "vitest";

import {
  flattenSpans,
  formatDuration,
  spanTypeTone,
  statusTone,
  TraceSpanTree,
} from "@/components/observability/TraceSpanTree";
import { TraceTimeline } from "@/components/observability/TraceTimeline";
import { render, screen, userEvent } from "@/test/utils";

function span(overrides: Record<string, unknown> = {}) {
  return {
    span_id: "root",
    parent_id: null,
    name: "root span",
    span_type: "AGENT",
    start_time_ms: 100,
    end_time_ms: 125,
    duration_ms: 25,
    status: "OK",
    inputs: null,
    outputs: null,
    attributes: {},
    ...overrides,
  } as never;
}

describe("trace renderer decision branches", () => {
  it("covers span tones, statuses, durations, and tree construction", () => {
    for (const type of ["AGENT", "TOOL", "GUARDRAIL", "ROUTER", "RETRIEVER", "PARSER", "CUSTOM"]) {
      expect(spanTypeTone(type)).toContain("border-");
    }
    expect(statusTone("ERROR")).toContain("red");
    expect(statusTone("FAILED")).toContain("red");
    expect(statusTone("OK")).toContain("emerald");
    expect(statusTone("COMPLETED")).toContain("emerald");
    expect(statusTone("QUEUED")).toContain("slate");
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(Number.NaN)).toBe("—");
    expect(formatDuration(0.25)).toBe("250 µs");
    expect(formatDuration(5)).toBe("5.0 ms");
    expect(formatDuration(25)).toBe("25 ms");
    expect(formatDuration(1500)).toBe("1.50 s");

    const rows = flattenSpans([
      span({ span_id: "root", name: "root" }),
      span({ span_id: "child", parent_id: "root", name: "child" }),
      span({ span_id: "orphan", parent_id: "missing", name: "orphan" }),
      span({ span_id: null, parent_id: null, name: "anonymous" }),
    ]);
    expect(rows.map((row) => [row.name, row.depth])).toEqual([
      ["root", 0],
      ["child", 1],
      ["orphan", 0],
      ["anonymous", 0],
    ]);
  });

  it("expands detailed spans while keeping sparse rows inert", async () => {
    const user = userEvent.setup();
    render(
      <TraceSpanTree
        spans={[
          span({
            span_id: "detailed",
            name: "detailed",
            span_type: "TOOL",
            status: "FAILED",
            inputs: { query: "Ada" },
            outputs: "answer",
            attributes: { "caliber.tokens": 4 },
          }),
          span({
            span_id: "sparse",
            name: "sparse",
            span_type: "CUSTOM",
            inputs: undefined,
            outputs: undefined,
            attributes: {},
          }),
        ]}
      />,
    );

    const detailed = screen.getByRole("button", { name: /detailed/ });
    const sparse = screen.getByRole("button", { name: /sparse/ });
    expect(sparse).toBeDisabled();
    await user.click(detailed);
    expect(screen.getByTestId("trace-span-detail")).toHaveTextContent('"query": "Ada"');
    expect(screen.getByTestId("trace-span-detail")).toHaveTextContent("answer");
    expect(screen.getByTestId("trace-span-detail")).toHaveTextContent("caliber.tokens");
    await user.click(detailed);
    expect(screen.queryByTestId("trace-span-detail")).not.toBeInTheDocument();
  });

  it("renders timeline bars for every span type and missing timing values", () => {
    render(
      <TraceTimeline
        spans={[
          ...["AGENT", "TOOL", "GUARDRAIL", "ROUTER", "RETRIEVER", "PARSER", "CUSTOM"].map(
            (spanType, index) =>
              span({
                span_id: `span-${index}`,
                name: `${spanType.toLowerCase()} row`,
                span_type: spanType,
                start_time_ms: index === 6 ? null : 100 + index * 10,
                end_time_ms: index === 6 ? null : 110 + index * 10,
                duration_ms: index === 6 ? null : 10,
              }),
          ),
        ]}
      />,
    );
    expect(screen.getAllByTestId("trace-timeline-row")).toHaveLength(7);
    expect(screen.getByTitle("CUSTOM · —")).toBeInTheDocument();
    expect(screen.getByTitle("AGENT · 10 ms")).toBeInTheDocument();
  });

  it("renders an empty timeline without a timing domain", () => {
    render(<TraceTimeline spans={[]} />);
    expect(screen.getByTestId("trace-timeline")).toBeInTheDocument();
    expect(screen.queryByTestId("trace-timeline-row")).not.toBeInTheDocument();
  });
});
