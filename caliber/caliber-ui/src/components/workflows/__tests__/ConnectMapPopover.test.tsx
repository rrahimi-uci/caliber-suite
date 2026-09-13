import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ManifestNode } from "@/api/workflowTypes";
import { ConnectMapPopover } from "@/components/workflows/ConnectMapPopover";

function agentSource(overrides: Partial<ManifestNode> = {}): ManifestNode {
  return {
    id: "agent_a",
    type: "agent",
    name: "support-agent",
    outputs: {
      final_output: { type: "string" },
      structured_output: { type: "structured" },
    },
    ...overrides,
  } as ManifestNode;
}

function agentTarget(overrides: Partial<ManifestNode> = {}): ManifestNode {
  return {
    id: "agent_b",
    type: "agent",
    name: "billing-agent",
    inputs: {
      input: { type: "string" },
      messages: { type: "messages" },
    },
    ...overrides,
  } as ManifestNode;
}

function renderPopover(overrides: Partial<Parameters<typeof ConnectMapPopover>[0]> = {}) {
  const onChange = vi.fn();
  const onDone = vi.fn();
  const onRemove = vi.fn();
  const props = {
    source: agentSource(),
    target: agentTarget(),
    map: {},
    onChange,
    onDone,
    onRemove,
    ...overrides,
  };
  const rendered = render(<ConnectMapPopover {...props} />);
  return { ...rendered, onChange, onDone, onRemove, props };
}

describe("ConnectMapPopover", () => {
  it("renders source/target names, port counts, and falls back to id when name is absent", () => {
    renderPopover({
      source: agentSource({ name: undefined, id: "agent_a" }),
      target: agentTarget({ name: undefined, id: "agent_b" }),
    });
    expect(screen.getByTestId("connect-map-popover")).toHaveTextContent("agent_a");
    expect(screen.getByTestId("connect-map-popover")).toHaveTextContent("agent_b");
    expect(screen.getByText("2 source outputs")).toBeInTheDocument();
    expect(screen.getByText("2 target inputs")).toBeInTheDocument();
  });

  it("uses singular port-count wording for exactly one input/output", () => {
    renderPopover({
      source: agentSource({ outputs: { final_output: { type: "string" } } }),
      target: agentTarget({ inputs: { input: { type: "string" } } }),
    });
    expect(screen.getByText("1 source output")).toBeInTheDocument();
    expect(screen.getByText("1 target input")).toBeInTheDocument();
  });

  it("renders unanchored (centered, absolute) by default with no inline style", () => {
    render(
      <ConnectMapPopover
        source={agentSource()}
        target={agentTarget()}
        map={{}}
        onChange={vi.fn()}
        onDone={vi.fn()}
        onRemove={vi.fn()}
      />,
    );
    const popover = screen.getByTestId("connect-map-popover");
    expect(popover.className).toContain("absolute");
    expect(popover.className).not.toContain("fixed");
    expect(popover.getAttribute("style")).toBeNull();
  });

  it("positions itself near the anchor point, fixed, when an anchor is given", () => {
    Object.defineProperty(window, "innerWidth", { value: 1200, writable: true });
    Object.defineProperty(window, "innerHeight", { value: 900, writable: true });
    render(
      <ConnectMapPopover
        source={agentSource()}
        target={agentTarget()}
        map={{}}
        anchor={{ x: 600, y: 300 }}
        onChange={vi.fn()}
        onDone={vi.fn()}
        onRemove={vi.fn()}
      />,
    );
    const popover = screen.getByTestId("connect-map-popover");
    expect(popover.className).toContain("fixed");
    expect(popover.className).not.toContain("absolute left-1/2");
    // anchor.x(600) - width/2(192) = 408; within bounds so unclamped.
    expect(popover.style.left).toBe("408px");
    // anchor.y(300) + 12 = 312; within bounds so unclamped.
    expect(popover.style.top).toBe("312px");
  });

  it("clamps the anchored position to stay within the viewport on both edges", () => {
    Object.defineProperty(window, "innerWidth", { value: 1200, writable: true });
    Object.defineProperty(window, "innerHeight", { value: 900, writable: true });
    const { rerender } = render(
      <ConnectMapPopover
        source={agentSource()}
        target={agentTarget()}
        map={{}}
        anchor={{ x: 0, y: 0 }}
        onChange={vi.fn()}
        onDone={vi.fn()}
        onRemove={vi.fn()}
      />,
    );
    let popover = screen.getByTestId("connect-map-popover");
    // Clamped to the minimum of 12px on both axes near the top-left corner.
    expect(popover.style.left).toBe("12px");
    expect(popover.style.top).toBe("12px");

    rerender(
      <ConnectMapPopover
        source={agentSource()}
        target={agentTarget()}
        map={{}}
        anchor={{ x: 5000, y: 5000 }}
        onChange={vi.fn()}
        onDone={vi.fn()}
        onRemove={vi.fn()}
      />,
    );
    popover = screen.getByTestId("connect-map-popover");
    // Clamped to viewWidth - width - 12 = 1200 - 384 - 12 = 804.
    expect(popover.style.left).toBe("804px");
    // Clamped to viewHeight - 260 = 640.
    expect(popover.style.top).toBe("640px");
  });

  it("shows 'no declared inputs' when the target has no inputs", () => {
    renderPopover({ target: agentTarget({ inputs: {} }) });
    expect(screen.getByText("Target has no declared inputs.")).toBeInTheDocument();
  });

  it("shows no compatibility warning banner when the map is empty or fully valid", () => {
    renderPopover({ map: { final_output: "input" } });
    expect(
      screen.queryByText(/incompatible port mapping/),
    ).not.toBeInTheDocument();
  });

  it("shows a singular compatibility warning for one incompatible mapping", () => {
    renderPopover({
      map: { structured_output: "input" }, // structured -> string: incompatible
    });
    expect(
      screen.getByText("This edge currently contains one incompatible port mapping."),
    ).toBeInTheDocument();
    expect(screen.getByText(/structured_output \(structured\) → input \(string\)/)).toBeInTheDocument();
  });

  it("shows a plural compatibility warning listing every incompatible mapping", () => {
    renderPopover({
      source: agentSource({
        outputs: {
          final_output: { type: "string" },
          structured_output: { type: "structured" },
          other_output: { type: "structured" },
        },
      }),
      target: agentTarget({
        inputs: {
          input: { type: "string" },
          messages: { type: "messages" },
        },
      }),
      map: { structured_output: "input", other_output: "messages" },
    });
    expect(
      screen.getByText("This edge currently contains 2 incompatible port mappings."),
    ).toBeInTheDocument();
  });

  it("marks unmapped-input outputs as having no compatible source when none are assignable", () => {
    renderPopover({
      source: agentSource({ outputs: { structured_output: { type: "structured" } } }),
      target: agentTarget({ inputs: { input: { type: "string" } } }),
      map: {},
    });
    expect(
      screen.getByText("No compatible source outputs are available for this input yet."),
    ).toBeInTheDocument();
  });

  it("shows the auto-map hint only when there are no warnings and both sides have ports", () => {
    const { rerender } = renderPopover({ map: {} });
    expect(
      screen.getByText(/Auto-map prefers same-name ports first/),
    ).toBeInTheDocument();

    rerender(
      <ConnectMapPopover
        source={agentSource()}
        target={agentTarget()}
        map={{ structured_output: "input" }}
        onChange={vi.fn()}
        onDone={vi.fn()}
        onRemove={vi.fn()}
      />,
    );
    expect(
      screen.queryByText(/Auto-map prefers same-name ports first/),
    ).not.toBeInTheDocument();
  });

  it("hides the auto-map hint when the target has no inputs", () => {
    renderPopover({ target: agentTarget({ inputs: {} }) });
    expect(
      screen.queryByText(/Auto-map prefers same-name ports first/),
    ).not.toBeInTheDocument();
  });

  it("disables incompatible, non-selected source options but keeps the current selection enabled", () => {
    renderPopover({
      source: agentSource({
        outputs: {
          final_output: { type: "string" },
          structured_output: { type: "structured" },
        },
      }),
      target: agentTarget({ inputs: { input: { type: "string" } } }),
      map: { structured_output: "input" },
    });
    const select = screen.getByTestId("map-input-input") as HTMLSelectElement;
    const options = Array.from(select.options);
    const structuredOption = options.find((o) => o.value === "structured_output")!;
    const finalOption = options.find((o) => o.value === "final_output")!;
    // Currently selected even though incompatible -> not disabled.
    expect(structuredOption.disabled).toBe(false);
    expect(structuredOption.textContent).toContain("incompatible");
    // Compatible and not selected -> enabled, no incompatible suffix.
    expect(finalOption.disabled).toBe(false);
    expect(finalOption.textContent).not.toContain("incompatible");
  });

  it("selecting a source output calls onChange with the updated map, replacing any prior mapping for that input", () => {
    const { onChange } = renderPopover({
      source: agentSource({
        outputs: {
          final_output: { type: "string" },
          other: { type: "string" },
        },
      }),
      target: agentTarget({ inputs: { input: { type: "string" } } }),
      map: { other: "input" },
    });
    fireEvent.change(screen.getByTestId("map-input-input"), {
      target: { value: "final_output" },
    });
    expect(onChange).toHaveBeenCalledWith({ final_output: "input" });
  });

  it("selecting '(unmapped)' removes the mapping for that input", () => {
    const { onChange } = renderPopover({
      map: { final_output: "input" },
    });
    fireEvent.change(screen.getByTestId("map-input-input"), {
      target: { value: "" },
    });
    expect(onChange).toHaveBeenCalledWith({});
  });

  it("preserves unrelated mappings when changing one input's source", async () => {
    const user = userEvent.setup();
    const { onChange } = renderPopover({
      source: agentSource({
        outputs: {
          final_output: { type: "string" },
          alt: { type: "messages" },
        },
      }),
      target: agentTarget({
        inputs: {
          input: { type: "string" },
          messages: { type: "messages" },
        },
      }),
      map: { final_output: "input" },
    });
    await user.selectOptions(screen.getByTestId("map-input-messages"), "alt");
    expect(onChange).toHaveBeenCalledWith({ final_output: "input", alt: "messages" });
  });

  it("fires onChange with the auto-mapped ports when Auto-Map is clicked", async () => {
    const user = userEvent.setup();
    const { onChange } = renderPopover({
      source: agentSource({ outputs: { input: { type: "string" } } }),
      target: agentTarget({ inputs: { input: { type: "string" } } }),
      map: {},
    });
    await user.click(screen.getByTestId("map-auto"));
    expect(onChange).toHaveBeenCalledWith({ input: "input" });
  });

  it("fires onRemove when Remove is clicked", async () => {
    const user = userEvent.setup();
    const { onRemove } = renderPopover();
    await user.click(screen.getByTestId("map-remove"));
    expect(onRemove).toHaveBeenCalledTimes(1);
  });

  it("fires onDone when Done is clicked", async () => {
    const user = userEvent.setup();
    const { onDone } = renderPopover();
    await user.click(screen.getByTestId("map-done"));
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it("tolerates a malformed manifest missing a declared input's port spec", () => {
    // Defensive fallback path: a manifest can list an input key without a
    // resolvable PortSpec (e.g. a stale/hand-edited manifest). The row should
    // still render using the neutral fallback color instead of throwing.
    renderPopover({
      target: agentTarget({
        inputs: { input: undefined } as unknown as ManifestNode["inputs"],
      }),
    });
    expect(screen.getByTestId("map-input-input")).toBeInTheDocument();
  });

  it("tolerates a malformed manifest missing a declared output's port spec", () => {
    renderPopover({
      source: agentSource({
        outputs: { final_output: undefined } as unknown as ManifestNode["outputs"],
      }),
      target: agentTarget({ inputs: { input: { type: "string" } } }),
    });
    const select = screen.getByTestId("map-input-input") as HTMLSelectElement;
    const option = Array.from(select.options).find((o) => o.value === "final_output")!;
    // No type suffix rendered when the source port spec can't be resolved.
    expect(option.textContent).toBe("final_output");
  });
});
