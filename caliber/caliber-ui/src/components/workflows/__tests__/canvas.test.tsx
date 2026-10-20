import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ValidationReport, WorkflowManifest } from "@/api/workflowTypes";

const mockState: { reactFlowProps?: Record<string, unknown> } = {};
const fitView = vi.fn();

vi.mock("@xyflow/react", async () => {
  const React = await import("react");

  return {
    Background: () => <div data-testid="mock-background" />,
    BackgroundVariant: { Dots: "dots" },
    ConnectionLineType: { SmoothStep: "smoothstep" },
    ConnectionMode: { Loose: "loose" },
    Controls: () => <div data-testid="mock-controls" />,
    MiniMap: () => <div data-testid="mock-minimap" />,
    ReactFlowProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
    useReactFlow: () => ({
      screenToFlowPosition: ({ x, y }: { x: number; y: number }) => ({ x: x + 1, y: y + 2 }),
      fitView,
    }),
    ReactFlow: (props: Record<string, unknown>) => {
      mockState.reactFlowProps = props;
      const onNodeClick = props.onNodeClick as
        | ((event: unknown, node: { id: string }) => void)
        | undefined;
      const onNodeDoubleClick = props.onNodeDoubleClick as
        | ((event: unknown, node: { id: string }) => void)
        | undefined;
      const onPaneClick = props.onPaneClick as (() => void) | undefined;
      const onEdgeClick = props.onEdgeClick as
        | ((event: unknown, edge: { id: string }) => void)
        | undefined;
      const onConnect = props.onConnect as
        | ((connection: { source: string; target: string }) => void)
        | undefined;
      const onConnectStart = props.onConnectStart as
        | ((event: unknown, params: { nodeId: string | null }) => void)
        | undefined;
      const onConnectEnd = props.onConnectEnd as ((event: unknown) => void) | undefined;
      const children = props.children as React.ReactNode;

      return (
        <div data-testid="mock-react-flow">
          <button data-testid="mock-node-click" onClick={() => onNodeClick?.({}, { id: "agent" })} />
          <button
            data-testid="mock-node-double-click"
            onClick={() => onNodeDoubleClick?.({}, { id: "agent" })}
          />
          <button data-testid="mock-pane-click" onClick={() => onPaneClick?.()} />
          <button
            data-testid="mock-edge-click"
            onClick={() => onEdgeClick?.({ clientX: 18, clientY: 24 }, { id: "e1" })}
          />
          <button
            data-testid="mock-connect"
            onClick={() => onConnect?.({ source: "start", target: "agent" })}
          />
          <button
            data-testid="mock-connect-start"
            onClick={() => onConnectStart?.({}, { nodeId: "start" })}
          />
          <button
            data-testid="mock-connect-end-empty"
            onClick={() =>
              onConnectEnd?.({
                clientX: 10,
                clientY: 20,
                target: { closest: () => null },
              })
            }
          />
          <button
            data-testid="mock-connect-end-handle"
            onClick={() =>
              onConnectEnd?.({
                clientX: 10,
                clientY: 20,
                target: { closest: () => ({}) },
              })
            }
          />
          {children}
        </div>
      );
    },
  };
});

import { Canvas } from "@/components/workflows/Canvas";

function testManifest(): WorkflowManifest {
  return {
    schema_version: 1,
    workflow_id: "WF-1",
    name: "Canvas Test",
    nodes: {
      start: {
        id: "start",
        type: "start",
        outputs: { user_message: { type: "string" } },
      },
      agent: {
        id: "agent",
        type: "agent",
        name: "support-agent",
        model: "inherit",
        instructions: { type: "inline", text: "help" },
        tools: [],
        inputs: { input: { type: "string" } },
        outputs: { final_output: { type: "string" } },
      },
    },
    edges: [{ id: "e1", from: "start", to: "agent", map: { user_message: "input" } }],
  };
}

describe("Canvas", () => {
  beforeEach(() => {
    mockState.reactFlowProps = undefined;
    fitView.mockClear();
  });

  describe("refitting when the canvas changes size", () => {
    let report: ((width: number, height: number) => void) | undefined;
    beforeEach(() => {
      vi.useFakeTimers();
      report = undefined;
      vi.stubGlobal(
        "ResizeObserver",
        class {
          constructor(callback: (entries: unknown[]) => void) {
            report = (width, height) => callback([{ contentRect: { width, height } }]);
          }
          observe(): void {}
          disconnect(): void {}
        },
      );
    });
    afterEach(() => {
      vi.useRealTimers();
      vi.unstubAllGlobals();
    });

    it("refits once, after a real size change has settled", () => {
      render(<Canvas manifest={testManifest()} />);
      report?.(900, 600); // React Flow's own first fit used this size
      expect(fitView).not.toHaveBeenCalled();
      // A burst of changes (panels opening, toolbar wrapping) is one refit, after it settles.
      report?.(700, 600);
      report?.(450, 600);
      report?.(450, 560);
      vi.advanceTimersByTime(200);
      expect(fitView).toHaveBeenCalledTimes(1);
      expect(fitView).toHaveBeenCalledWith({ padding: 0.12, minZoom: 0.15 });
    });

    it("ignores a scrollbar-sized change, so a refit can never cause the next refit", () => {
      render(<Canvas manifest={testManifest()} />);
      report?.(450, 600);
      // The lab 16 defect: on a platform where scrollbars take space, every refit moved the
      // graph, a scrollbar appeared or vanished, the canvas changed by ~15px, and it refitted again.
      for (let i = 0; i < 20; i += 1) {
        report?.(i % 2 ? 450 : 435, 600);
        vi.advanceTimersByTime(200);
      }
      expect(fitView).not.toHaveBeenCalled();
    });

    it("stops refitting by itself after a few real changes", () => {
      render(<Canvas manifest={testManifest()} />);
      report?.(100, 600);
      for (let i = 1; i <= 12; i += 1) {
        report?.(100 + i * 100, 600);
        vi.advanceTimersByTime(200);
      }
      expect(fitView).toHaveBeenCalledTimes(4);
    });

    it("lets a wide graph zoom out far enough to fit a laptop-width canvas", () => {
      render(<Canvas manifest={testManifest()} />);
      const props = mockState.reactFlowProps as {
        minZoom: number;
        fitViewOptions: { minZoom: number };
      };
      expect(props.minZoom).toBeLessThanOrEqual(0.2);
      expect(props.fitViewOptions.minZoom).toBe(props.minZoom);
    });

    it("never refits a view the person has panned or zoomed", () => {
      render(<Canvas manifest={testManifest()} />);
      report?.(900, 600);
      const props = mockState.reactFlowProps as { onMoveStart: (event: unknown) => void };
      props.onMoveStart({ type: "mousedown" });
      report?.(450, 600);
      vi.advanceTimersByTime(200);
      expect(fitView).not.toHaveBeenCalled();
    });

    it("still refits after fitView's own programmatic move (no DOM event)", () => {
      render(<Canvas manifest={testManifest()} />);
      report?.(900, 600);
      const props = mockState.reactFlowProps as { onMoveStart: (event: unknown) => void };
      props.onMoveStart(null);
      report?.(450, 600);
      vi.advanceTimersByTime(200);
      expect(fitView).toHaveBeenCalledTimes(1);
    });
  });

  it("decorates nodes/edges and forwards selection + connection handlers", async () => {
    const onSelectNode = vi.fn();
    const onNodeDoubleClick = vi.fn();
    const onEdgeClick = vi.fn();
    const onConnect = vi.fn();
    const onConnectionDrop = vi.fn();
    render(
      <Canvas
        manifest={testManifest()}
        selectedNodeId="agent"
        executionPath={["start", "agent"]}
        onSelectNode={onSelectNode}
        onNodeDoubleClick={onNodeDoubleClick}
        onEdgeClick={onEdgeClick}
        onConnect={onConnect}
        onConnectionDrop={onConnectionDrop}
      />,
    );

    const props = mockState.reactFlowProps as {
      nodes: Array<{ id: string; selected?: boolean; style?: Record<string, unknown> }>;
      edges: Array<{ id: string; animated?: boolean; style?: Record<string, unknown> }>;
    };
    const agentNode = props.nodes.find((node) => node.id === "agent");
    const pathEdge = props.edges.find((edge) => edge.id === "e1");
    expect(agentNode?.selected).toBe(true);
    expect(agentNode?.style).toEqual({ boxShadow: "0 0 0 3px #22C55E" });
    expect(pathEdge?.animated).toBe(true);
    expect(pathEdge?.style).toEqual({ stroke: "#22c55e", strokeWidth: 2.5 });

    const user = userEvent.setup();
    await user.click(screen.getByTestId("mock-node-click"));
    await user.click(screen.getByTestId("mock-node-double-click"));
    await user.click(screen.getByTestId("mock-pane-click"));
    await user.click(screen.getByTestId("mock-edge-click"));
    await user.click(screen.getByTestId("mock-connect"));
    await user.click(screen.getByTestId("mock-connect-start"));
    await user.click(screen.getByTestId("mock-connect-end-empty"));
    await user.click(screen.getByTestId("mock-connect-start"));
    await user.click(screen.getByTestId("mock-connect-end-handle"));

    expect(onSelectNode).toHaveBeenCalledWith("agent");
    expect(onSelectNode).toHaveBeenCalledWith(null);
    expect(onNodeDoubleClick).toHaveBeenCalledWith("agent");
    expect(onEdgeClick).toHaveBeenCalledWith("e1", { x: 18, y: 24 });
    expect(onConnect).toHaveBeenCalledWith({ source: "start", target: "agent" });
    expect(onConnectionDrop).toHaveBeenCalledWith(
      "start",
      { x: 11, y: 22 },
      { x: 10, y: 20 },
    );
    expect(onConnectionDrop).toHaveBeenCalledTimes(1);
  });

  it("decorates node data with validation summaries", () => {
    const validationReport: ValidationReport = {
      valid: false,
      errors: [
        {
          code: "missing_instructions",
          path: "nodes.agent.instructions",
          message: "Agent instructions are required.",
          severity: "error",
        },
      ],
      warnings: [],
    };

    render(<Canvas manifest={testManifest()} validationReport={validationReport} />);

    const props = mockState.reactFlowProps as {
      nodes: Array<{
        id: string;
        data?: {
          validationSummary?: {
            severity: string;
            errors: number;
            title: string;
          };
        };
      }>;
    };
    const agentNode = props.nodes.find((node) => node.id === "agent");
    expect(agentNode?.data?.validationSummary).toMatchObject({
      severity: "error",
      errors: 1,
    });
    expect(agentNode?.data?.validationSummary?.title).toContain("Agent instructions are required.");
  });

  it("decorates nodes with live execution badges and status outlines", () => {
    render(
      <Canvas
        manifest={testManifest()}
        executionPath={["start", "agent"]}
        nodeExecutionByNode={{
          agent: {
            status: "waiting_event",
            label: "event",
            source: "run",
            tone: "warning",
            current: true,
          },
        }}
      />,
    );

    const props = mockState.reactFlowProps as {
      nodes: Array<{
        id: string;
        style?: Record<string, unknown>;
        data?: {
          executionBadge?: {
            status: string;
            label: string;
            current: boolean;
          };
        };
      }>;
    };
    const agentNode = props.nodes.find((node) => node.id === "agent");
    expect(agentNode?.style).toEqual({ boxShadow: "0 0 0 3px #F59E0B" });
    expect(agentNode?.data?.executionBadge).toMatchObject({
      status: "waiting_event",
      label: "event",
      current: true,
    });
  });

  it("handles palette drag and drop events", () => {
    const onDropNode = vi.fn();
    render(<Canvas manifest={testManifest()} onDropNode={onDropNode} />);

    const canvas = screen.getByTestId("wf-canvas");

    const dataTransfer = {
      dropEffect: "none",
      getData: vi.fn(() => "guardrail"),
    };

    fireEvent.dragOver(canvas, { dataTransfer });
    expect(dataTransfer.dropEffect).toBe("move");

    const dropEvent = new Event("drop", { bubbles: true, cancelable: true });
    Object.defineProperty(dropEvent, "clientX", { value: 25 });
    Object.defineProperty(dropEvent, "clientY", { value: 39 });
    Object.defineProperty(dropEvent, "dataTransfer", { value: dataTransfer });
    fireEvent(canvas, dropEvent);

    // Drop point is converted via screenToFlowPosition (mocked as +1/+2),
    // so the node lands in flow coordinates rather than raw canvas pixels.
    expect(onDropNode).toHaveBeenCalledWith("guardrail", { x: 26, y: 41 });

    const emptyDropEvent = new Event("drop", { bubbles: true, cancelable: true });
    Object.defineProperty(emptyDropEvent, "clientX", { value: 0 });
    Object.defineProperty(emptyDropEvent, "clientY", { value: 0 });
    Object.defineProperty(emptyDropEvent, "dataTransfer", {
      value: { dropEffect: "none", getData: () => "" },
    });
    fireEvent(canvas, emptyDropEvent);
    expect(onDropNode).toHaveBeenCalledTimes(1);
  });
});
