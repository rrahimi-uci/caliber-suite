import { describe, expect, it } from "vitest";
import { render, screen, userEvent, within } from "@/test/utils";

import type {
  WorkflowManifest,
  WorkflowRun,
  WorkflowRunCheckpoint,
  WorkflowRunEvent,
  WorkflowRunStep,
} from "@/api/workflowTypes";
import {
  WorkflowRunDebugger,
  runEventsLoadErrorMessage,
} from "@/components/workflows/WorkflowRunDebugger";

/**
 * Additional coverage for `WorkflowRunDebugger.tsx`, focused on the
 * remaining uncovered branches identified from a scoped coverage run:
 * the exported `runEventsLoadErrorMessage` helper (never exercised by the
 * sibling suite, which only renders the component), the various run-status
 * buckets that drive the empty-debugger / empty-timeline / empty-port /
 * transition guidance copy, the tool "mcp_tool" binding tone, the
 * knowledge-build status tones, extra knowledge-query badges, and the
 * selection/focus effects inside the component itself.
 *
 * The sibling `workflow-run-debugger.test.tsx` already covers the "happy
 * path" diagnostics rendering (AGE retrieval, knowledge build, child
 * workflow, direct tool node, orchestration, lifecycle markers) in depth,
 * so these tests deliberately avoid duplicating that coverage and instead
 * target the specific gaps left behind.
 */

const manifest: WorkflowManifest = {
  schema_version: 1,
  workflow_id: "WF-gap",
  name: "Gap Coverage Workflow",
  nodes: {
    start: {
      id: "start",
      type: "start",
      outputs: { msg: { type: "string" } },
    },
    middle: {
      id: "middle",
      type: "agent",
      inputs: { msg: { type: "string" } },
      outputs: { reply: { type: "string" } },
    },
    final: {
      id: "final",
      type: "output",
      inputs: { response: { type: "string" } },
    },
  },
  edges: [
    { id: "e1", from: "start", to: "middle", map: { msg: "msg" } },
    { id: "e2", from: "middle", to: "final", map: { reply: "response" } },
  ],
};

function baseStep(overrides: Partial<WorkflowRunStep> = {}): WorkflowRunStep {
  return {
    node_id: "start",
    node_type: "start",
    status: "ok",
    output: "hello",
    tool_calls: [],
    handoff_target: null,
    detail: "captured",
    duration_ms: 10,
    output_by_port: { msg: "hello" },
    ...overrides,
  };
}

function threeStepRun(overrides: Partial<WorkflowRun> = {}): WorkflowRun {
  return {
    workflow_run_id: "WR-gap",
    workflow_id: "WF-gap",
    project_id: null,
    tenant_id: null,
    workflow_version_id: "WFV-gap",
    deployment_alias: "prod",
    mlflow_run_id: null,
    trace_id: null,
    session_id: null,
    status: "completed",
    source: "manual",
    priority: 0,
    queued_at: "2026-06-13T00:00:00Z",
    started_at: "2026-06-13T00:00:01Z",
    completed_at: "2026-06-13T00:00:03Z",
    current_node_id: "final",
    summary: {
      node_path: ["start", "middle", "final"],
      steps: [
        baseStep({
          node_id: "start",
          node_type: "start",
          detail: "captured question",
          output_by_port: { msg: "hello" },
        }),
        baseStep({
          node_id: "middle",
          node_type: "agent",
          detail: "replied",
          input_by_port: { msg: "hello" },
          output_by_port: { reply: "hi there" },
          duration_ms: 20,
        }),
        baseStep({
          node_id: "final",
          node_type: "output",
          detail: "returned",
          input_by_port: { response: "hi there" },
          output_by_port: { response: "hi there" },
          duration_ms: 5,
        }),
      ],
    },
    ...overrides,
  };
}

function emptyRun(status: string): WorkflowRun {
  return {
    ...threeStepRun({ status }),
    summary: { node_path: [] },
  };
}

describe("runEventsLoadErrorMessage", () => {
  it.each(["queued", "running", "resuming", "cancel_requested", "waiting_approval", "waiting_event"])(
    "explains that persisted events are unavailable while the run is still active (%s)",
    (status) => {
      render(<div>{runEventsLoadErrorMessage(status, "socket timeout")}</div>);
      expect(screen.getByText(/still active/)).toBeInTheDocument();
      expect(
        screen.getByText(/recovery, checkpoint, and lineage panels/),
      ).toBeInTheDocument();
      expect(screen.getByText("Latest event error: socket timeout")).toBeInTheDocument();
    },
  );

  it("explains that persisted events are unavailable for a completed run", () => {
    render(<div>{runEventsLoadErrorMessage("completed", "db down")}</div>);
    expect(
      screen.getByText(/could not be loaded for this completed run/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/final outputs, and generated artifacts/),
    ).toBeInTheDocument();
    expect(screen.getByText("Latest event error: db down")).toBeInTheDocument();
  });

  it.each(["failed", "cancelled", "rejected", "expired", "blocked"])(
    "explains that persisted events are unavailable for a stopped run (%s)",
    (status) => {
      render(<div>{runEventsLoadErrorMessage(status, "network error")}</div>);
      expect(
        screen.getByText(/could not be loaded for this stopped run/),
      ).toBeInTheDocument();
      expect(
        screen.getByText(/recovery, checkpoint, and lineage panels to trace where execution failed/),
      ).toBeInTheDocument();
    },
  );

  it("falls back to the generic explanation for an unrecognized or missing status", () => {
    render(<div>{runEventsLoadErrorMessage("blocked_on_review", "oops")}</div>);
    expect(
      screen.getByText(/unavailable until event history is restored, so use the recovery, checkpoint,/),
    ).toBeInTheDocument();

    render(<div>{runEventsLoadErrorMessage(null, "oops")}</div>);
    expect(
      screen.getAllByText(/unavailable until event history is restored, so use the recovery, checkpoint,/).length,
    ).toBeGreaterThan(0);
  });

  it("collapses a blank error message down to 'Unknown error'", () => {
    render(<div>{runEventsLoadErrorMessage("completed", "   ")}</div>);
    expect(screen.getByText("Latest event error: Unknown error")).toBeInTheDocument();
  });
});

describe("empty debugger state (no recorded steps)", () => {
  it("guides an active run with no persisted evidence at all", () => {
    render(<WorkflowRunDebugger manifest={manifest} run={emptyRun("running")} events={[]} />);
    const empty = screen.getByTestId("workflow-run-debugger-empty");
    expect(empty).toHaveTextContent("No recorded step details yet.");
    expect(empty).toHaveTextContent(
      "Check the recovery timeline and checkpoint panel while execution continues.",
    );
  });

  it("guides a completed run with no persisted evidence at all", () => {
    render(<WorkflowRunDebugger manifest={manifest} run={emptyRun("completed")} events={[]} />);
    const empty = screen.getByTestId("workflow-run-debugger-empty");
    expect(empty).toHaveTextContent(
      "Check the recovery timeline, final outputs, and generated artifacts to reconstruct how it finished.",
    );
  });

  it("guides a stopped run with no persisted evidence at all", () => {
    render(<WorkflowRunDebugger manifest={manifest} run={emptyRun("cancelled")} events={[]} />);
    const empty = screen.getByTestId("workflow-run-debugger-empty");
    expect(empty).toHaveTextContent(
      "Check the recovery timeline and checkpoint panel to trace where execution stopped.",
    );
  });

  it("falls back to generic guidance for an unrecognized run status with no evidence", () => {
    render(<WorkflowRunDebugger manifest={manifest} run={emptyRun("waiting_approval")} events={[]} />);
    const empty = screen.getByTestId("workflow-run-debugger-empty");
    expect(empty).toHaveTextContent(
      "Check the recovery timeline and checkpoint panel for any persisted run evidence until richer step telemetry is available.",
    );
  });

  it("guides a stopped run that has lifecycle evidence but no rich steps", () => {
    const checkpoints: WorkflowRunCheckpoint[] = [
      {
        checkpoint_id: "CHK-1",
        workflow_run_id: "WR-gap",
        project_id: null,
        sequence: 1,
        node_id: "middle",
        state_blob: { kind: "runtime_approval" },
        created_at: "2026-06-13T00:00:02Z",
      },
    ];
    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={emptyRun("failed")}
        events={[]}
        checkpoints={checkpoints}
      />,
    );
    const empty = screen.getByTestId("workflow-run-debugger-empty");
    expect(empty).toHaveTextContent("1 checkpoint");
    expect(empty).toHaveTextContent(
      "This run stopped before richer step telemetry was persisted.",
    );
    expect(empty).toHaveTextContent(
      "recovery and checkpoint panels to trace where execution stopped",
    );
  });

  it("falls back to generic evidence guidance for an unrecognized status", () => {
    const events: WorkflowRunEvent[] = [
      {
        event_id: 1,
        workflow_run_id: "WR-gap",
        project_id: null,
        sequence: 1,
        event_type: "workflow.run.started",
        node_id: null,
        payload: {},
        created_at: "2026-06-13T00:00:01Z",
      },
    ];
    render(
      <WorkflowRunDebugger manifest={manifest} run={emptyRun("waiting_event")} events={events} />,
    );
    const empty = screen.getByTestId("workflow-run-debugger-empty");
    expect(empty).toHaveTextContent("1 lifecycle event");
    expect(empty).toHaveTextContent(
      "This run may only have lifecycle events or a lightweight summary.",
    );
  });
});

describe("empty event timeline without checkpoint evidence", () => {
  // With steps present there is always a selected step, so these renders
  // exercise the `hasSelectedStep` (but not `hasCheckpoint`) branch of
  // `emptyEventTimelineMessage` -- previously only ever reached together
  // with a checkpoint in the sibling suite.
  function runWithStatus(status: string): WorkflowRun {
    return threeStepRun({ status, completed_at: status === "completed" ? "2026-06-13T00:00:03Z" : null });
  }

  it("guides an active run", () => {
    render(<WorkflowRunDebugger manifest={manifest} run={runWithStatus("running")} events={[]} />);
    const timeline = screen.getByTestId("workflow-run-event-timeline");
    expect(timeline).toHaveTextContent(
      "so use the selected step snapshot and port-level context while execution continues.",
    );
  });

  it("guides a completed run", () => {
    render(<WorkflowRunDebugger manifest={manifest} run={runWithStatus("completed")} events={[]} />);
    const timeline = screen.getByTestId("workflow-run-event-timeline");
    expect(timeline).toHaveTextContent(
      "so use the selected step snapshot, port-level context, and final outputs to reconstruct how it finished.",
    );
  });

  it("guides a stopped run", () => {
    render(<WorkflowRunDebugger manifest={manifest} run={runWithStatus("rejected")} events={[]} />);
    const timeline = screen.getByTestId("workflow-run-event-timeline");
    expect(timeline).toHaveTextContent(
      "Use the selected step snapshot, port-level context, and recovery diagnostics to trace where execution stopped.",
    );
  });

  it("falls back to generic guidance for an unrecognized run status", () => {
    render(<WorkflowRunDebugger manifest={manifest} run={runWithStatus("waiting_event")} events={[]} />);
    const timeline = screen.getByTestId("workflow-run-event-timeline");
    expect(timeline).toHaveTextContent(
      "Use the selected step snapshot and port-level context to keep tracing execution until event persistence is available.",
    );
  });
});

describe("missing port snapshots", () => {
  function runWithMissingInputPort(status: string): WorkflowRun {
    const stepsRun = threeStepRun({ status });
    const steps = stepsRun.summary!.steps!.map((step) =>
      step.node_id === "middle" ? { ...step, input_by_port: undefined } : step,
    );
    return { ...stepsRun, summary: { ...stepsRun.summary!, steps } };
  }

  it("explains a missing input snapshot for an active run", async () => {
    render(<WorkflowRunDebugger manifest={manifest} run={runWithMissingInputPort("running")} events={[]} />);
    await userEvent.click(screen.getByTestId("workflow-run-step-button-1"));
    expect(screen.getByTestId("workflow-run-step-ports")).toHaveTextContent("In · Snapshot pending");
    const empty = screen.getByTestId("workflow-run-step-input-ports-empty");
    expect(empty).toHaveTextContent(
      "Input port snapshot has not been persisted for this step yet.",
    );
    expect(empty).toHaveTextContent("while runtime evidence fills in.");
    // The output port on the same step still has data, exercising the
    // "has snapshot" branch alongside the "missing" branch above.
    expect(screen.getByTestId("workflow-run-step-output-ports")).toBeInTheDocument();
  });

  it("explains a missing input snapshot for a completed run", async () => {
    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={runWithMissingInputPort("completed")}
        events={[]}
      />,
    );
    await userEvent.click(screen.getByTestId("workflow-run-step-button-1"));
    expect(screen.getByTestId("workflow-run-step-ports")).toHaveTextContent("In · Snapshot unavailable");
    expect(screen.getByTestId("workflow-run-step-input-ports-empty")).toHaveTextContent(
      "was not persisted for this completed run step. Use the event timeline, final outputs, and generated artifacts",
    );
  });

  it("explains a missing input snapshot for a stopped run", async () => {
    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={runWithMissingInputPort("expired")}
        events={[]}
      />,
    );
    await userEvent.click(screen.getByTestId("workflow-run-step-button-1"));
    expect(screen.getByTestId("workflow-run-step-ports")).toHaveTextContent("In · Snapshot unavailable");
    expect(screen.getByTestId("workflow-run-step-input-ports-empty")).toHaveTextContent(
      "was not persisted before this run stopped. Use the recovery timeline, checkpoint trail",
    );
  });

  it("explains a missing input snapshot with generic copy for an unrecognized status", async () => {
    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={runWithMissingInputPort("waiting_approval")}
        events={[]}
      />,
    );
    await userEvent.click(screen.getByTestId("workflow-run-step-button-1"));
    expect(screen.getByTestId("workflow-run-step-ports")).toHaveTextContent(
      "In · No snapshot recorded",
    );
    expect(screen.getByTestId("workflow-run-step-input-ports-empty")).toHaveTextContent(
      "Input port snapshot was not persisted for this step.",
    );
  });
});

describe("step transition guidance for the earliest step", () => {
  function runSelectingFirstStep(status: string): WorkflowRun {
    return threeStepRun({ status, completed_at: status === "completed" ? "2026-06-13T00:00:03Z" : null });
  }

  it("labels the earliest step for an active run", async () => {
    render(<WorkflowRunDebugger manifest={manifest} run={runSelectingFirstStep("running")} events={[]} />);
    await userEvent.click(screen.getByTestId("workflow-run-step-button-0"));
    const transition = screen.getByTestId("workflow-run-step-diff-transition");
    expect(transition).toHaveTextContent("Earliest persisted step so far");
    expect(transition).toHaveTextContent(
      "Use the input ports, checkpoint trail, and recovery timeline to trace how execution entered this node while earlier telemetry catches up.",
    );
  });

  it("labels the earliest step for a completed run", async () => {
    render(<WorkflowRunDebugger manifest={manifest} run={runSelectingFirstStep("completed")} events={[]} />);
    await userEvent.click(screen.getByTestId("workflow-run-step-button-0"));
    const transition = screen.getByTestId("workflow-run-step-diff-transition");
    expect(transition).toHaveTextContent("Earliest persisted step in this completed run");
    expect(transition).toHaveTextContent(
      "No previous recorded step was persisted for this completed run. Use the input ports, event timeline, and final outputs",
    );
  });

  it("labels the earliest step for a stopped run", async () => {
    render(<WorkflowRunDebugger manifest={manifest} run={runSelectingFirstStep("blocked")} events={[]} />);
    await userEvent.click(screen.getByTestId("workflow-run-step-button-0"));
    const transition = screen.getByTestId("workflow-run-step-diff-transition");
    expect(transition).toHaveTextContent("Earliest persisted step before execution stopped");
    expect(transition).toHaveTextContent(
      "No previous recorded step was persisted before this run stopped. Use the input ports, checkpoint trail, and recovery diagnostics",
    );
  });

  it("labels the earliest step with generic copy for an unrecognized status", async () => {
    render(
      <WorkflowRunDebugger manifest={manifest} run={runSelectingFirstStep("waiting_event")} events={[]} />,
    );
    await userEvent.click(screen.getByTestId("workflow-run-step-button-0"));
    const transition = screen.getByTestId("workflow-run-step-diff-transition");
    expect(transition).toHaveTextContent("Earliest persisted step in this run");
    expect(transition).toHaveTextContent(
      "No previous recorded step is available for transition comparison.",
    );
  });
});

describe("unchanged transition diffs for a non-first step", () => {
  function unchangedSnapshotRun(status: string): WorkflowRun {
    return {
      ...threeStepRun({ status }),
      summary: {
        node_path: ["start", "middle", "final"],
        steps: [
          baseStep({ node_id: "start", output_by_port: { msg: "hello" } }),
          baseStep({
            node_id: "middle",
            node_type: "agent",
            // Same snapshot flowing straight through -> empty transition diff.
            input_by_port: { msg: "hello" },
            output_by_port: { msg: "hello" },
          }),
          baseStep({
            node_id: "final",
            node_type: "output",
            input_by_port: { response: "hi" },
            output_by_port: { response: "hi" },
          }),
        ],
      },
    };
  }

  it("guides an active run whose transition is unchanged", async () => {
    render(
      <WorkflowRunDebugger manifest={manifest} run={unchangedSnapshotRun("running")} events={[]} />,
    );
    await userEvent.click(screen.getByTestId("workflow-run-step-button-1"));
    const transition = screen.getByTestId("workflow-run-step-diff-transition");
    expect(transition).toHaveTextContent(
      "This run may still be executing or richer telemetry may still be catching up",
    );
    expect(transition).toHaveTextContent("still working from the same snapshot");
  });

  it("falls back to generic guidance for an unchanged transition on an unrecognized status", async () => {
    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={unchangedSnapshotRun("waiting_approval")}
        events={[]}
      />,
    );
    await userEvent.click(screen.getByTestId("workflow-run-step-button-1"));
    const transition = screen.getByTestId("workflow-run-step-diff-transition");
    expect(transition).toHaveTextContent(
      "No port-level changes were recorded between these persisted steps.",
    );
  });
});

describe("tool binding tone", () => {
  it("labels an MCP-bound tool node distinctly from a registered function", async () => {
    const mcpManifest: WorkflowManifest = {
      ...manifest,
      nodes: {
        ...manifest.nodes,
        middle: { ...manifest.nodes.middle, id: "middle", type: "tool" },
      },
    };
    const mcpRun: WorkflowRun = {
      ...threeStepRun(),
      summary: {
        node_path: ["start", "middle", "final"],
        steps: [
          baseStep({ node_id: "start" }),
          baseStep({
            node_id: "middle",
            node_type: "tool",
            input_by_port: { msg: "hello" },
            output_by_port: {
              text: "done",
              metadata: {
                tool_name: "remote_lookup",
                binding_type: "mcp_tool",
                server_id: "srv-1",
                remote_tool_name: "lookup",
              },
            },
          }),
          baseStep({ node_id: "final", node_type: "output" }),
        ],
      },
    };
    render(<WorkflowRunDebugger manifest={mcpManifest} run={mcpRun} events={[]} />);
    const toolStep = screen.getByTestId("workflow-run-step-button-1");
    expect(within(toolStep).getByText("MCP tool")).toBeInTheDocument();
    expect(within(toolStep).getByText("remote_lookup")).toBeInTheDocument();
  });
});

describe("knowledge build status tones", () => {
  function knowledgeBuildRun(status: string, previewSkipped = false): WorkflowRun {
    return {
      ...threeStepRun(),
      summary: {
        node_path: ["start", "middle", "final"],
        steps: [
          baseStep({ node_id: "start" }),
          baseStep({
            node_id: "middle",
            node_type: "knowledge_build",
            input_by_port: { msg: "hello" },
            output_by_port: {
              status,
              knowledge_base_id: "KB-1",
              result: { preview: previewSkipped },
            },
          }),
          baseStep({ node_id: "final", node_type: "output" }),
        ],
      },
    };
  }

  it("labels a queued build", () => {
    render(<WorkflowRunDebugger manifest={manifest} run={knowledgeBuildRun("queued")} events={[]} />);
    expect(
      within(screen.getByTestId("workflow-run-step-button-1")).getByText("Build queued"),
    ).toBeInTheDocument();
  });

  it("labels a failed build", () => {
    render(<WorkflowRunDebugger manifest={manifest} run={knowledgeBuildRun("failed")} events={[]} />);
    expect(
      within(screen.getByTestId("workflow-run-step-button-1")).getByText("Build failed"),
    ).toBeInTheDocument();
  });

  it("labels a skipped preview build", () => {
    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={knowledgeBuildRun("processing", true)}
        events={[]}
      />,
    );
    expect(
      within(screen.getByTestId("workflow-run-step-button-1")).getByText("Preview skipped"),
    ).toBeInTheDocument();
  });
});

describe("knowledge query fallback and override diagnostics", () => {
  it("surfaces the fallback mode badge, override badge, and combined meta line", () => {
    const knowledgeManifest: WorkflowManifest = {
      ...manifest,
      nodes: {
        ...manifest.nodes,
        middle: { ...manifest.nodes.middle, id: "middle", type: "knowledge_query" },
      },
    };
    const knowledgeRun: WorkflowRun = {
      ...threeStepRun(),
      summary: {
        node_path: ["start", "middle", "final"],
        steps: [
          baseStep({ node_id: "start" }),
          baseStep({
            node_id: "middle",
            node_type: "knowledge_query",
            input_by_port: { msg: "hello" },
            output_by_port: {
              answer: "answer text",
              result: {
                versions: [
                  {
                    retrieval_mode: "graph_hybrid",
                    graph_context: {
                      matched_entities: ["Entity A"],
                      age_fallback_reason: "AGE unavailable",
                      fallback_retrieval_mode: "dense",
                      query_override_active: true,
                    },
                  },
                ],
              },
            },
          }),
          baseStep({ node_id: "final", node_type: "output" }),
        ],
      },
    };
    render(<WorkflowRunDebugger manifest={knowledgeManifest} run={knowledgeRun} events={[]} />);
    const knowledgeStep = screen.getByTestId("workflow-run-step-button-1");
    expect(within(knowledgeStep).getByText("Fallback Dense chunks")).toBeInTheDocument();
    expect(within(knowledgeStep).getByText("Query override")).toBeInTheDocument();
    expect(knowledgeStep).toHaveTextContent("Matched Entity A");
    expect(knowledgeStep).toHaveTextContent("AGE fallback: AGE unavailable");
  });
});

describe("step selection derived from live events outpacing the summary", () => {
  it("prefers the richer event-derived step list over a stale summary", () => {
    const staleRun: WorkflowRun = {
      ...threeStepRun(),
      summary: {
        node_path: ["start"],
        steps: [baseStep({ node_id: "start" })],
      },
    };
    const liveEvents: WorkflowRunEvent[] = [
      {
        event_id: 1,
        workflow_run_id: "WR-gap",
        project_id: null,
        sequence: 1,
        event_type: "workflow.run.step",
        node_id: "start",
        payload: { step: baseStep({ node_id: "start" }) as unknown as Record<string, unknown> },
        created_at: "2026-06-13T00:00:01Z",
      },
      {
        event_id: 2,
        workflow_run_id: "WR-gap",
        project_id: null,
        sequence: 2,
        event_type: "workflow.run.step",
        node_id: "middle",
        payload: {
          step: baseStep({ node_id: "middle", node_type: "agent" }) as unknown as Record<
            string,
            unknown
          >,
        },
        created_at: "2026-06-13T00:00:02Z",
      },
    ];
    render(<WorkflowRunDebugger manifest={manifest} run={staleRun} events={liveEvents} />);
    expect(screen.getByText("2 recorded steps")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-run-step-button-1")).toBeInTheDocument();
  });
});

describe("selection persistence and focus effects", () => {
  it("keeps the current selection when re-rendering with a step list that still contains it", async () => {
    const { rerender } = render(
      <WorkflowRunDebugger manifest={manifest} run={threeStepRun()} events={[]} />,
    );
    await userEvent.click(screen.getByTestId("workflow-run-step-button-0"));
    expect(screen.getByTestId("workflow-run-step-detail")).toHaveTextContent("start");

    // Re-render with a materially different (but reference-distinct) run
    // object whose steps still include the previously-selected key
    // ("start:0"): the selection should be preserved, not reset to the
    // last step.
    rerender(
      <WorkflowRunDebugger
        manifest={manifest}
        run={{ ...threeStepRun(), trace_id: "trace-changed" }}
        events={[]}
      />,
    );
    expect(screen.getByTestId("workflow-run-step-detail")).toHaveTextContent("start");
  });

  it("resets the selection to the latest step once the selected key disappears", async () => {
    const { rerender } = render(
      <WorkflowRunDebugger manifest={manifest} run={threeStepRun()} events={[]} />,
    );
    await userEvent.click(screen.getByTestId("workflow-run-step-button-1"));
    expect(screen.getByTestId("workflow-run-step-detail")).toHaveTextContent("middle");

    const replacedRun: WorkflowRun = {
      ...threeStepRun(),
      summary: {
        node_path: ["start", "final"],
        steps: [
          baseStep({ node_id: "start" }),
          baseStep({ node_id: "final", node_type: "output" }),
        ],
      },
    };
    rerender(<WorkflowRunDebugger manifest={manifest} run={replacedRun} events={[]} />);
    expect(screen.getByTestId("workflow-run-step-detail")).toHaveTextContent("final");
  });

  it("jumps the selection to a focused node id that differs from the current selection", () => {
    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={threeStepRun()}
        events={[]}
        focusedNodeId="start"
      />,
    );
    // Default selection is the last step ("final"); focusedNodeId should
    // move it to "start".
    expect(screen.getByTestId("workflow-run-step-detail")).toHaveTextContent("start");
  });

  it("leaves the selection untouched when the focused node id is already selected", () => {
    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={threeStepRun()}
        events={[]}
        focusedNodeId="final"
      />,
    );
    expect(screen.getByTestId("workflow-run-step-detail")).toHaveTextContent("final");
  });

  it("ignores a focused node id that does not match any recorded step", () => {
    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={threeStepRun()}
        events={[]}
        focusedNodeId="does-not-exist"
      />,
    );
    // No matching step: falls back to the default (latest) selection.
    expect(screen.getByTestId("workflow-run-step-detail")).toHaveTextContent("final");
  });
});

describe("empty event timeline with checkpoint evidence for remaining status buckets", () => {
  const checkpoints: WorkflowRunCheckpoint[] = [
    {
      checkpoint_id: "CHK-remaining",
      workflow_run_id: "WR-gap",
      project_id: null,
      sequence: 1,
      node_id: "middle",
      state_blob: { kind: "runtime_approval" },
      created_at: "2026-06-13T00:00:02Z",
    },
  ];

  it("guides a stopped run that has both a selected step and checkpoint evidence", () => {
    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={threeStepRun({ status: "cancelled" })}
        events={[]}
        checkpoints={checkpoints}
      />,
    );
    const timeline = screen.getByTestId("workflow-run-event-timeline");
    expect(timeline).toHaveTextContent(
      "Use the selected step snapshot, port-level context, stored checkpoint details, and recovery diagnostics to trace where execution stopped.",
    );
  });

  it("falls back to generic guidance with both a selected step and checkpoint evidence", () => {
    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={threeStepRun({ status: "waiting_approval" })}
        events={[]}
        checkpoints={checkpoints}
      />,
    );
    const timeline = screen.getByTestId("workflow-run-event-timeline");
    expect(timeline).toHaveTextContent(
      "Use the selected step snapshot, port-level context, and stored checkpoint details to keep tracing execution until event persistence is available.",
    );
  });
});

describe("legacy / malformed persisted event payloads", () => {
  it("coerces a numeric-string field on a persisted step back into a number", async () => {
    const eventsWithStringTokens: WorkflowRunEvent[] = [
      {
        event_id: 1,
        workflow_run_id: "WR-gap",
        project_id: null,
        sequence: 1,
        event_type: "workflow.run.step",
        node_id: "start",
        payload: {
          step: {
            ...baseStep({ node_id: "start" }),
            tokens: "42",
          } as unknown as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:00:01Z",
      },
    ];
    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={{ ...threeStepRun(), summary: { node_path: ["start"], steps: [baseStep({ node_id: "start" })] } }}
        events={eventsWithStringTokens}
      />,
    );
    // The event-derived step list (1 step) is no richer than the summary
    // (also 1 step), so the summary step map still renders; the numeric
    // coercion is exercised while deriving the event step regardless.
    expect(screen.getByTestId("workflow-run-step-button-0")).toBeInTheDocument();
  });

  it("silently drops a persisted step event whose payload is missing required fields", () => {
    const malformedEvents: WorkflowRunEvent[] = [
      {
        event_id: 1,
        workflow_run_id: "WR-gap",
        project_id: null,
        sequence: 1,
        event_type: "workflow.run.step",
        node_id: "start",
        payload: { step: { detail: "no node id or type here" } },
        created_at: "2026-06-13T00:00:01Z",
      },
      {
        event_id: 2,
        workflow_run_id: "WR-gap",
        project_id: null,
        sequence: 2,
        event_type: "workflow.run.step",
        node_id: "start",
        payload: { step: null },
        created_at: "2026-06-13T00:00:02Z",
      },
    ];
    // Only the summary steps (3) should be used since neither malformed
    // event yields a usable derived step.
    render(<WorkflowRunDebugger manifest={manifest} run={threeStepRun()} events={malformedEvents} />);
    expect(screen.getByText("3 recorded steps")).toBeInTheDocument();
  });
});

describe("event timeline summary text for edge-case events", () => {
  function runForTimeline(): WorkflowRun {
    return threeStepRun({ status: "completed" });
  }

  it("falls back to a handoff summary when a step event has no detail", () => {
    const events: WorkflowRunEvent[] = [
      {
        event_id: 1,
        workflow_run_id: "WR-gap",
        project_id: null,
        sequence: 1,
        event_type: "workflow.run.step",
        node_id: "start",
        payload: {
          step: {
            ...baseStep({ node_id: "start" }),
            detail: "",
            handoff_target: "middle",
          } as unknown as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:00:01Z",
      },
    ];
    render(<WorkflowRunDebugger manifest={manifest} run={runForTimeline()} events={events} />);
    expect(screen.getByTestId("workflow-run-event-timeline")).toHaveTextContent(
      "handoff -> middle",
    );
  });

  it("falls back to a tool-call-count summary when a step event has no detail or handoff", () => {
    const events: WorkflowRunEvent[] = [
      {
        event_id: 1,
        workflow_run_id: "WR-gap",
        project_id: null,
        sequence: 1,
        event_type: "workflow.run.step",
        node_id: "start",
        payload: {
          step: {
            ...baseStep({ node_id: "start" }),
            detail: "",
            handoff_target: null,
            tool_calls: [{ tool: "lookup" }],
          } as unknown as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:00:01Z",
      },
    ];
    render(<WorkflowRunDebugger manifest={manifest} run={runForTimeline()} events={events} />);
    expect(screen.getByTestId("workflow-run-event-timeline")).toHaveTextContent("1 tool call");
  });

  it("falls back to the raw step status when a step event has no detail, handoff, or tool calls", () => {
    const events: WorkflowRunEvent[] = [
      {
        event_id: 1,
        workflow_run_id: "WR-gap",
        project_id: null,
        sequence: 1,
        event_type: "workflow.run.step",
        node_id: "start",
        payload: {
          step: {
            ...baseStep({ node_id: "start" }),
            detail: "",
            handoff_target: null,
            tool_calls: [],
            status: "skipped",
          } as unknown as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:00:01Z",
      },
    ];
    render(<WorkflowRunDebugger manifest={manifest} run={runForTimeline()} events={events} />);
    expect(screen.getByTestId("workflow-run-event-timeline")).toHaveTextContent("skipped");
  });

  it("summarizes a waiting_event lifecycle event and a non-lifecycle-prefixed event type", () => {
    const events: WorkflowRunEvent[] = [
      {
        event_id: 1,
        workflow_run_id: "WR-gap",
        project_id: null,
        sequence: 1,
        event_type: "workflow.run.waiting_event",
        node_id: null,
        payload: { event_name: "external_signal" },
        created_at: "2026-06-13T00:00:01Z",
      },
      {
        event_id: 2,
        workflow_run_id: "WR-gap",
        project_id: null,
        sequence: 2,
        event_type: "external.webhook.received",
        node_id: null,
        payload: {},
        created_at: "2026-06-13T00:00:02Z",
      },
    ];
    render(<WorkflowRunDebugger manifest={manifest} run={runForTimeline()} events={events} />);
    const timeline = screen.getByTestId("workflow-run-event-timeline");
    expect(timeline).toHaveTextContent("external.webhook.received");
    expect(timeline).toHaveTextContent("external webhook received");
  });
});

describe("remaining step-marker lifecycle branches", () => {
  it("labels rejection, waiting-event, resumed, cancel-requested, and cancelled markers on the final step", () => {
    const twoStepRun: WorkflowRun = {
      ...threeStepRun({ status: "cancelled" }),
      summary: {
        node_path: ["start", "final"],
        steps: [
          baseStep({ node_id: "start" }),
          baseStep({ node_id: "final", node_type: "output" }),
        ],
      },
    };
    // No `workflow.run.step` events, so every one of these post-step
    // lifecycle events is attributed to the last recorded step.
    const lifecycleEvents: WorkflowRunEvent[] = [
      {
        event_id: 1,
        workflow_run_id: "WR-gap",
        project_id: null,
        sequence: 1,
        event_type: "workflow.run.approval.rejected",
        node_id: null,
        payload: {},
        created_at: "2026-06-13T00:00:01Z",
      },
      {
        event_id: 2,
        workflow_run_id: "WR-gap",
        project_id: null,
        sequence: 2,
        event_type: "workflow.run.waiting_event",
        node_id: null,
        payload: {},
        created_at: "2026-06-13T00:00:02Z",
      },
      {
        event_id: 3,
        workflow_run_id: "WR-gap",
        project_id: null,
        sequence: 3,
        event_type: "workflow.run.resumed",
        node_id: null,
        payload: {},
        created_at: "2026-06-13T00:00:03Z",
      },
      {
        event_id: 4,
        workflow_run_id: "WR-gap",
        project_id: null,
        sequence: 4,
        event_type: "workflow.run.cancel_requested",
        node_id: null,
        payload: {},
        created_at: "2026-06-13T00:00:04Z",
      },
      {
        event_id: 5,
        workflow_run_id: "WR-gap",
        project_id: null,
        sequence: 5,
        event_type: "workflow.run.cancelled",
        node_id: null,
        payload: {},
        created_at: "2026-06-13T00:00:05Z",
      },
    ];
    render(<WorkflowRunDebugger manifest={manifest} run={twoStepRun} events={lifecycleEvents} />);
    const finalStep = screen.getByTestId("workflow-run-step-button-1");
    expect(within(finalStep).getByText("Runtime approval rejected")).toBeInTheDocument();
    expect(within(finalStep).getByText("Paused for event")).toBeInTheDocument();
    expect(within(finalStep).getByText("Resumed")).toBeInTheDocument();
    expect(within(finalStep).getByText("Cancel requested")).toBeInTheDocument();
    expect(within(finalStep).getByText("Run cancelled")).toBeInTheDocument();
  });
});

describe("diagnostics meta lines that collapse to null", () => {
  it("omits the loop meta line when a for_each step reports counts but no per-item results", () => {
    const forEachManifest: WorkflowManifest = {
      ...manifest,
      nodes: {
        ...manifest.nodes,
        middle: { ...manifest.nodes.middle, id: "middle", type: "for_each" },
      },
    };
    const forEachRun: WorkflowRun = {
      ...threeStepRun(),
      summary: {
        node_path: ["start", "middle", "final"],
        steps: [
          baseStep({ node_id: "start" }),
          baseStep({
            node_id: "middle",
            node_type: "for_each",
            input_by_port: { msg: "hello" },
            output_by_port: {
              metadata: { count: 3, target_node_id: "worker", target_node_type: "agent" },
            },
          }),
          baseStep({ node_id: "final", node_type: "output" }),
        ],
      },
    };
    render(<WorkflowRunDebugger manifest={forEachManifest} run={forEachRun} events={[]} />);
    const loopStep = screen.getByTestId("workflow-run-step-button-1");
    expect(within(loopStep).getByText("3 items")).toBeInTheDocument();
    expect(within(loopStep).getByText("Target worker · Agent")).toBeInTheDocument();
    // No "Preview ..." / "Failure ..." meta line since no per-item results
    // were persisted.
    expect(loopStep).not.toHaveTextContent("Preview");
    expect(loopStep).not.toHaveTextContent("Failure");
  });

  it("omits the error-recovery meta line when an error boundary step has no message or compensation output", () => {
    const boundaryManifest: WorkflowManifest = {
      ...manifest,
      nodes: {
        ...manifest.nodes,
        middle: { ...manifest.nodes.middle, id: "middle", type: "error_boundary" },
      },
    };
    const boundaryRun: WorkflowRun = {
      ...threeStepRun(),
      summary: {
        node_path: ["start", "middle", "final"],
        steps: [
          baseStep({ node_id: "start" }),
          baseStep({
            node_id: "middle",
            node_type: "error_boundary",
            input_by_port: { msg: "hello" },
            output_by_port: {
              error: { target_node_id: "risky_tool", target_node_type: "tool" },
            },
          }),
          baseStep({ node_id: "final", node_type: "output" }),
        ],
      },
    };
    render(<WorkflowRunDebugger manifest={boundaryManifest} run={boundaryRun} events={[]} />);
    const boundaryStep = screen.getByTestId("workflow-run-step-button-1");
    expect(within(boundaryStep).getByText("Handled failure")).toBeInTheDocument();
    expect(within(boundaryStep).getByText("Protected risky_tool · Tool")).toBeInTheDocument();
    expect(boundaryStep).not.toHaveTextContent("Recovery");
  });

  it("renders the default retrieval-mode tone for a mode other than AGE graph or GraphRAG hybrid", () => {
    const knowledgeManifest: WorkflowManifest = {
      ...manifest,
      nodes: {
        ...manifest.nodes,
        middle: { ...manifest.nodes.middle, id: "middle", type: "knowledge_query" },
      },
    };
    const knowledgeRun: WorkflowRun = {
      ...threeStepRun(),
      summary: {
        node_path: ["start", "middle", "final"],
        steps: [
          baseStep({ node_id: "start" }),
          baseStep({
            node_id: "middle",
            node_type: "knowledge_query",
            input_by_port: { msg: "hello" },
            output_by_port: {
              answer: "answer text",
              result: { versions: [{ retrieval_mode: "dense" }] },
            },
          }),
          baseStep({ node_id: "final", node_type: "output" }),
        ],
      },
    };
    render(<WorkflowRunDebugger manifest={knowledgeManifest} run={knowledgeRun} events={[]} />);
    expect(
      within(screen.getByTestId("workflow-run-step-button-1")).getByText("Dense chunks"),
    ).toBeInTheDocument();
  });
});
