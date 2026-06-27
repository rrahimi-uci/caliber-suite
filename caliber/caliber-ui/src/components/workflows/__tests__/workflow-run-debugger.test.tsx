import { describe, expect, it, vi } from "vitest";
import { render, screen, userEvent, within } from "@/test/utils";

import type {
  WorkflowManifest,
  WorkflowRun,
  WorkflowRunCheckpoint,
  WorkflowRunEvent,
} from "@/api/workflowTypes";
import { WorkflowRunDebugger } from "@/components/workflows/WorkflowRunDebugger";

const manifest: WorkflowManifest = {
  schema_version: 1,
  workflow_id: "WF-1",
  name: "Workflow",
  nodes: {
    start: {
      id: "start",
      type: "start",
      outputs: { msg: { type: "string" } },
    },
    knowledge: {
      id: "knowledge",
      type: "knowledge_query",
      knowledge_base_id: "KB-1",
      retrieval_modes: ["age_graph"],
      inputs: { question: { type: "string" } },
      outputs: {
        answer: { type: "string" },
        result: { type: "structured" },
      },
    },
    final: {
      id: "final",
      type: "output",
      inputs: { response: { type: "string" } },
    },
  },
  edges: [
    { id: "e1", from: "start", to: "knowledge", map: { msg: "question" } },
    { id: "e2", from: "knowledge", to: "final", map: { answer: "response" } },
  ],
};

function run(): WorkflowRun {
  return {
    workflow_run_id: "WR-1",
    workflow_id: "WF-1",
    project_id: null,
    tenant_id: null,
    workflow_version_id: "WFV-1",
    deployment_alias: "prod",
    mlflow_run_id: null,
    trace_id: "trace-1",
    session_id: null,
    status: "completed",
    source: "manual",
    priority: 0,
    queued_at: "2026-06-13T00:00:00Z",
    started_at: "2026-06-13T00:00:01Z",
    completed_at: "2026-06-13T00:00:03Z",
    current_node_id: "final",
    summary: {
      node_path: ["start", "knowledge", "final"],
      steps: [
        {
          node_id: "start",
          node_type: "start",
          status: "ok",
          output: "Where is the escalation playbook?",
          tool_calls: [],
          handoff_target: null,
          detail: "captured question",
          duration_ms: 21,
          output_by_port: { msg: "Where is the escalation playbook?" },
        },
        {
          node_id: "knowledge",
          node_type: "knowledge_query",
          status: "ok",
          output: "The escalation playbook is in Handbook.md.",
          tokens: 42,
          prompt_tokens: 18,
          completion_tokens: 24,
          cached_prompt_tokens: 12,
          cost_usd: 0.000042,
          model: "gpt-4.1-mini",
          prompt_version: "openai_responses",
          tool_calls: [],
          handoff_target: null,
          detail:
            "answered via age_graph · 1 citation · 1 chunk · seeded from question text",
          duration_ms: 187,
          input_by_port: { question: "Where is the escalation playbook?" },
          output_by_port: {
            answer: "The escalation playbook is in Handbook.md.",
            result: {
              versions: [
                {
                  retrieval_mode: "age_graph",
                  citations: [
                    {
                      chunk_id: "CH-1",
                      label: "[1] Handbook.md",
                    },
                  ],
                  retrieved_chunks: [
                    {
                      chunk_id: "CH-1",
                      source_name: "Handbook.md",
                      source_key: "docs/handbook.md",
                      score: 0.94,
                      content:
                        "Escalation playbook coverage and routing details.",
                      matched_entity_labels: ["Escalation Playbook"],
                    },
                  ],
                  graph_context: {
                    matched_entities: ["Escalation Playbook"],
                    age_graph_name: "knowledge_graph",
                    age_seed_strategy: "query_text",
                    strict_age_retrieval: true,
                  },
                },
              ],
            },
          },
        },
        {
          node_id: "final",
          node_type: "output",
          status: "ok",
          output: "The escalation playbook is in Handbook.md.",
          tool_calls: [],
          handoff_target: null,
          detail: "returned answer",
          duration_ms: 9,
          input_by_port: {
            response: "The escalation playbook is in Handbook.md.",
          },
          output_by_port: {
            response: "The escalation playbook is in Handbook.md.",
          },
        },
      ],
    },
  };
}

function events(): WorkflowRunEvent[] {
  const startStep = run().summary?.steps?.[0];
  const knowledgeStep = run().summary?.steps?.[1];
  return [
    {
      event_id: 1,
      workflow_run_id: "WR-1",
      project_id: null,
      sequence: 1,
      event_type: "workflow.run.started",
      node_id: null,
      payload: { at: "2026-06-13T00:00:01Z" },
      created_at: "2026-06-13T00:00:01Z",
    },
    {
      event_id: 2,
      workflow_run_id: "WR-1",
      project_id: null,
      sequence: 2,
      event_type: "workflow.run.step",
      node_id: "start",
      payload: {
        step: startStep as Record<string, unknown>,
      },
      created_at: "2026-06-13T00:00:01Z",
    },
    {
      event_id: 3,
      workflow_run_id: "WR-1",
      project_id: null,
      sequence: 3,
      event_type: "workflow.run.step",
      node_id: "knowledge",
      payload: {
        step: knowledgeStep as Record<string, unknown>,
      },
      created_at: "2026-06-13T00:00:02Z",
    },
    {
      event_id: 4,
      workflow_run_id: "WR-1",
      project_id: null,
      sequence: 4,
      event_type: "workflow.run.completed",
      node_id: null,
      payload: { output: "The escalation playbook is in Handbook.md." },
      created_at: "2026-06-13T00:00:03Z",
    },
  ];
}

function runtimeApprovalCheckpoint(): WorkflowRunCheckpoint {
  return {
    checkpoint_id: "CHK-runtime",
    workflow_run_id: "WR-runtime",
    project_id: null,
    sequence: 4,
    node_id: "knowledge",
    state_blob: {
      kind: "runtime_approval",
      input_by_port: {
        question: "Where is the escalation playbook?",
      },
      output: "Where is the escalation playbook?",
    },
    created_at: "2026-06-13T00:00:02Z",
  };
}

function runtimeApprovalEvents(): WorkflowRunEvent[] {
  const startStep = run().summary?.steps?.[0];
  const knowledgeStep = run().summary?.steps?.[1];
  return [
    {
      event_id: 1,
      workflow_run_id: "WR-runtime",
      project_id: null,
      sequence: 1,
      event_type: "workflow.run.started",
      node_id: null,
      payload: { at: "2026-06-13T00:00:01Z" },
      created_at: "2026-06-13T00:00:01Z",
    },
    {
      event_id: 2,
      workflow_run_id: "WR-runtime",
      project_id: null,
      sequence: 2,
      event_type: "workflow.run.step",
      node_id: "start",
      payload: {
        step: startStep as Record<string, unknown>,
      },
      created_at: "2026-06-13T00:00:01Z",
    },
    {
      event_id: 3,
      workflow_run_id: "WR-runtime",
      project_id: null,
      sequence: 3,
      event_type: "workflow.run.step",
      node_id: "knowledge",
      payload: {
        step: knowledgeStep as Record<string, unknown>,
      },
      created_at: "2026-06-13T00:00:02Z",
    },
    {
      event_id: 4,
      workflow_run_id: "WR-runtime",
      project_id: null,
      sequence: 4,
      event_type: "workflow.run.waiting_approval",
      node_id: "knowledge",
      payload: {
        reason: "Tool execution requires approval.",
      },
      created_at: "2026-06-13T00:00:02Z",
    },
  ];
}

function humanApprovalCheckpoint(): WorkflowRunCheckpoint {
  return {
    checkpoint_id: "CHK-human",
    workflow_run_id: "WR-human",
    project_id: null,
    sequence: 4,
    node_id: "knowledge",
    state_blob: {
      kind: "human_approval",
      approval_count: 1,
      required_role: "ops_admin",
    },
    created_at: "2026-06-13T00:00:02Z",
  };
}

function humanApprovalEvents(): WorkflowRunEvent[] {
  return runtimeApprovalEvents().map((event) => ({
    ...event,
    workflow_run_id: "WR-human",
  }));
}

function sameSnapshotRun(status: WorkflowRun["status"]): WorkflowRun {
  return {
    ...run(),
    workflow_run_id: `WR-same-snapshot-${status}`,
    status,
    completed_at: status === "completed" ? "2026-06-13T00:00:03Z" : null,
    current_node_id: "knowledge",
    summary: {
      node_path: ["start", "knowledge"],
      steps: [
        {
          node_id: "start",
          node_type: "start",
          status: "ok",
          output: "carry forward",
          tool_calls: [],
          handoff_target: null,
          detail: "captured input",
          duration_ms: 5,
          output_by_port: { msg: "carry forward" },
        },
        {
          node_id: "knowledge",
          node_type: "knowledge_query",
          status: status === "failed" ? "error" : "ok",
          output: status === "failed" ? "stopped" : "unchanged snapshot",
          tool_calls: [],
          handoff_target: null,
          detail: status === "failed" ? "execution stopped" : "reused same snapshot",
          duration_ms: 12,
          input_by_port: { msg: "carry forward" },
          output_by_port: { answer: "unchanged snapshot" },
        },
      ],
    },
  };
}

describe("WorkflowRunDebugger", () => {
  it("surfaces AGE retrieval diagnostics across the step map, snapshot, and event timeline", async () => {
    const onSelectNodeId = vi.fn();

    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={run()}
        events={events()}
        onSelectNodeId={onSelectNodeId}
      />,
    );

    const knowledgeStep = screen.getByTestId("workflow-run-step-button-1");
    expect(
      within(knowledgeStep).getByText("Apache AGE graph"),
    ).toBeInTheDocument();
    expect(within(knowledgeStep).getByText("gpt-4.1-mini")).toBeInTheDocument();
    expect(within(knowledgeStep).getByText("42 tokens")).toBeInTheDocument();
    expect(
      within(knowledgeStep).getByText("Prompt openai_responses"),
    ).toBeInTheDocument();
    expect(knowledgeStep).toHaveTextContent("18 prompt");
    expect(knowledgeStep).toHaveTextContent("24 completion");
    expect(knowledgeStep).toHaveTextContent("12 cached prompt");
    expect(knowledgeStep).toHaveTextContent("Est. $0.000042");
    expect(
      within(knowledgeStep).getByText("AGE knowledge_graph"),
    ).toBeInTheDocument();
    expect(within(knowledgeStep).getByText("1 citation")).toBeInTheDocument();
    expect(within(knowledgeStep).getByText("1 chunk")).toBeInTheDocument();
    expect(
      within(knowledgeStep).getByText("Seeded from question text"),
    ).toBeInTheDocument();
    expect(within(knowledgeStep).getByText("strict AGE")).toBeInTheDocument();
    expect(
      within(knowledgeStep).getByText("Matched Escalation Playbook"),
    ).toBeInTheDocument();

    await userEvent.click(knowledgeStep);
    expect(onSelectNodeId).toHaveBeenCalledWith("knowledge");
    expect(screen.getByTestId("workflow-run-step-detail")).toHaveTextContent(
      "The escalation playbook is in Handbook.md.",
    );
    const snapshot = screen.getByTestId("workflow-run-step-snapshot");
    expect(within(snapshot).getByText("Retrieval")).toBeInTheDocument();
    const snapshotKnowledge = screen.getByTestId(
      "workflow-run-step-snapshot-knowledge",
    );
    expect(
      within(snapshotKnowledge).getByText("Apache AGE graph"),
    ).toBeInTheDocument();
    expect(
      within(snapshotKnowledge).getByText("AGE knowledge_graph"),
    ).toBeInTheDocument();
    expect(
      within(snapshotKnowledge).getByText("Seeded from question text"),
    ).toBeInTheDocument();
    const snapshotTelemetry = screen.getByTestId(
      "workflow-run-step-snapshot-telemetry",
    );
    expect(snapshotTelemetry).toHaveTextContent("gpt-4.1-mini");
    expect(snapshotTelemetry).toHaveTextContent("42 tokens");
    expect(snapshotTelemetry).toHaveTextContent("Prompt openai_responses");
    expect(snapshot).toHaveTextContent("42 total");
    expect(snapshot).toHaveTextContent("18 prompt");
    expect(snapshot).toHaveTextContent("24 completion");
    expect(snapshot).toHaveTextContent("12 cached prompt");
    expect(snapshot).toHaveTextContent("$0.000042");

    const eventKnowledge = screen.getByTestId("workflow-run-event-knowledge-3");
    expect(
      within(eventKnowledge).getByText("Apache AGE graph"),
    ).toBeInTheDocument();
    expect(
      within(eventKnowledge).getByText("AGE knowledge_graph"),
    ).toBeInTheDocument();
    expect(within(eventKnowledge).getByText("strict AGE")).toBeInTheDocument();
    const eventTelemetry = screen.getByTestId("workflow-run-event-telemetry-3");
    expect(eventTelemetry).toHaveTextContent("gpt-4.1-mini");
    expect(eventTelemetry).toHaveTextContent("42 tokens");
    expect(eventTelemetry).toHaveTextContent("Prompt openai_responses");
    expect(eventTelemetry).toHaveTextContent("12 cached prompt");
    expect(eventTelemetry).toHaveTextContent("Est. $0.000042");
    expect(screen.getByTestId("workflow-run-event-timeline")).toHaveTextContent(
      "Matched Escalation Playbook",
    );
  });

  it("labels runtime approval checkpoints distinctly in the step map and event timeline", () => {
    const runtimeApprovalRun: WorkflowRun = {
      ...run(),
      workflow_run_id: "WR-runtime",
      status: "waiting_approval",
      completed_at: null,
      current_node_id: "knowledge",
      summary: {
        resume_checkpoint_id: "CHK-runtime",
        node_path: ["start", "knowledge"],
        steps: (run().summary?.steps ?? []).slice(0, 2),
      },
    };
    const runtimeApprovalDecisionEvents = [
      ...runtimeApprovalEvents(),
      {
        event_id: 5,
        workflow_run_id: "WR-runtime",
        project_id: null,
        sequence: 5,
        event_type: "workflow.run.approval.approved",
        node_id: "knowledge",
        payload: {
          runtime_approval_id: "RA-runtime",
          reason: "policy reviewed",
        },
        created_at: "2026-06-13T00:00:03Z",
      },
    ];

    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={runtimeApprovalRun}
        events={runtimeApprovalDecisionEvents}
        checkpoints={[runtimeApprovalCheckpoint()]}
      />,
    );

    const knowledgeStep = screen.getByTestId("workflow-run-step-button-1");
    expect(within(knowledgeStep).getByText("Runtime approval")).toBeInTheDocument();
    expect(within(knowledgeStep).getByText("Resume target")).toBeInTheDocument();
    expect(within(knowledgeStep).getByText("Paused for runtime approval")).toBeInTheDocument();
    expect(within(knowledgeStep).getByText("Runtime approval recorded")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-run-event-timeline")).toHaveTextContent(
      "Awaiting runtime approval",
    );
    expect(screen.getByTestId("workflow-run-event-timeline")).toHaveTextContent(
      "Runtime approval recorded · RA-runtime · policy reviewed",
    );
  });

  it("keeps human approval events generic in the event timeline", () => {
    const humanApprovalRun: WorkflowRun = {
      ...run(),
      workflow_run_id: "WR-human",
      status: "waiting_approval",
      completed_at: null,
      current_node_id: "knowledge",
      summary: {
        resume_checkpoint_id: "CHK-human",
        node_path: ["start", "knowledge"],
        steps: (run().summary?.steps ?? []).slice(0, 2),
      },
    };

    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={humanApprovalRun}
        events={humanApprovalEvents()}
        checkpoints={[humanApprovalCheckpoint()]}
      />,
    );

    const timeline = screen.getByTestId("workflow-run-event-timeline");
    const knowledgeStep = screen.getByTestId("workflow-run-step-button-1");
    expect(within(knowledgeStep).getByText("Approval gate")).toBeInTheDocument();
    expect(within(knowledgeStep).getByText("Paused for approval")).toBeInTheDocument();
    expect(within(knowledgeStep).queryByText("Paused for runtime approval")).not.toBeInTheDocument();
    expect(timeline).toHaveTextContent("Awaiting approval");
    expect(timeline).not.toHaveTextContent("Awaiting runtime approval");
  });

  it("marks inherited approval checkpoints distinctly in the step map", () => {
    const inheritedApprovalRun: WorkflowRun = {
      ...run(),
      workflow_run_id: "WR-inherited",
      status: "waiting_approval",
      completed_at: null,
      current_node_id: "knowledge",
      summary: {
        retry_of: "WR-parent",
        retry_mode: "checkpoint",
        resume_checkpoint_id: "CHK-parent",
        resume_checkpoint_run_id: "WR-parent",
        node_path: ["start", "knowledge"],
        steps: (run().summary?.steps ?? []).slice(0, 2),
      },
    };
    const inheritedEvents = humanApprovalEvents().map((event) => ({
      ...event,
      workflow_run_id: "WR-inherited",
    }));

    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={inheritedApprovalRun}
        events={inheritedEvents}
        checkpoints={[
          {
            ...humanApprovalCheckpoint(),
            checkpoint_id: "CHK-parent",
            workflow_run_id: "WR-parent",
            sequence: 7,
          },
        ]}
      />,
    );

    const knowledgeStep = screen.getByTestId("workflow-run-step-button-1");
    expect(within(knowledgeStep).getByText("Approval gate")).toBeInTheDocument();
    expect(within(knowledgeStep).getByText("Inherited checkpoint")).toBeInTheDocument();
    expect(within(knowledgeStep).getByText("Resume target")).toBeInTheDocument();
    expect(within(knowledgeStep).getByText("Paused for approval")).toBeInTheDocument();
  });

  it("surfaces knowledge-build diagnostics across the step map, snapshot, and event timeline", async () => {
    const onSelectNodeId = vi.fn();
    const buildManifest: WorkflowManifest = {
      schema_version: 1,
      workflow_id: "WF-build",
      name: "Knowledge Build Workflow",
      nodes: {
        start: {
          id: "start",
          type: "start",
          outputs: { source: { type: "string" } },
        },
        knowledge_build: {
          id: "knowledge_build",
          type: "knowledge_build",
          knowledge_base_id: "KB-1",
          inputs: { source: { type: "string" } },
          outputs: {
            result: { type: "structured" },
          },
        } as WorkflowManifest["nodes"][string],
        final: {
          id: "final",
          type: "output",
          inputs: { response: { type: "string" } },
        },
      },
      edges: [
        {
          id: "e1",
          from: "start",
          to: "knowledge_build",
          map: { source: "source" },
        },
        {
          id: "e2",
          from: "knowledge_build",
          to: "final",
          map: { result: "response" },
        },
      ],
    };

    const buildRun: WorkflowRun = {
      ...run(),
      workflow_run_id: "WR-build",
      workflow_id: "WF-build",
      workflow_version_id: "WFV-build",
      summary: {
        node_path: ["start", "knowledge_build", "final"],
        steps: [
          {
            node_id: "start",
            node_type: "start",
            status: "ok",
            output: "docs/support/",
            tool_calls: [],
            handoff_target: null,
            detail: "captured source prefix",
            duration_ms: 16,
            output_by_port: { source: "docs/support/" },
          },
          {
            node_id: "knowledge_build",
            node_type: "knowledge_build",
            status: "ok",
            output: "Knowledge base build completed.",
            tool_calls: [],
            handoff_target: null,
            detail: "built semantic chunks and activated version",
            duration_ms: 241,
            input_by_port: { source: "docs/support/" },
            output_by_port: {
              result: {
                status: "completed",
                knowledge_base: {
                  knowledge_base_id: "KB-1",
                  active_version_id: "KBV-3",
                },
                version: {
                  knowledge_base_version_id: "KBV-3",
                  version_number: 3,
                  status: "completed",
                  chunking_strategy: "semantic",
                  embedding_model: "intfloat/e5-large-v2",
                  graph_config: {
                    extractor_backend: "spacy",
                    output_target: "object_store_and_age",
                    default_retrieval_mode: "age_graph",
                    retrieval_strength: "balanced",
                  },
                  summary: {
                    age_sync_status: "synced",
                  },
                },
                run: {
                  knowledge_base_run_id: "KBR-3",
                  status: "completed",
                },
                await_completion: {
                  requested: true,
                  status: "completed",
                  timeout_seconds: 900,
                },
                activation: {
                  requested: true,
                  status: "activated",
                  active_version_id: "KBV-3",
                },
              },
            },
          },
          {
            node_id: "final",
            node_type: "output",
            status: "ok",
            output: "Knowledge base build completed.",
            tool_calls: [],
            handoff_target: null,
            detail: "returned build result",
            duration_ms: 11,
            input_by_port: {
              response: "Knowledge base build completed.",
            },
            output_by_port: {
              response: "Knowledge base build completed.",
            },
          },
        ],
      },
    };

    const buildEvents: WorkflowRunEvent[] = [
      {
        event_id: 21,
        workflow_run_id: "WR-build",
        project_id: null,
        sequence: 1,
        event_type: "workflow.run.started",
        node_id: null,
        payload: { at: "2026-06-13T00:20:01Z" },
        created_at: "2026-06-13T00:20:01Z",
      },
      {
        event_id: 22,
        workflow_run_id: "WR-build",
        project_id: null,
        sequence: 2,
        event_type: "workflow.run.step",
        node_id: "start",
        payload: {
          step: buildRun.summary?.steps?.[0] as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:20:01Z",
      },
      {
        event_id: 23,
        workflow_run_id: "WR-build",
        project_id: null,
        sequence: 3,
        event_type: "workflow.run.step",
        node_id: "knowledge_build",
        payload: {
          step: buildRun.summary?.steps?.[1] as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:20:02Z",
      },
      {
        event_id: 24,
        workflow_run_id: "WR-build",
        project_id: null,
        sequence: 4,
        event_type: "workflow.run.completed",
        node_id: null,
        payload: { output: "Knowledge base build completed." },
        created_at: "2026-06-13T00:20:03Z",
      },
    ];

    render(
      <WorkflowRunDebugger
        manifest={buildManifest}
        run={buildRun}
        events={buildEvents}
        onSelectNodeId={onSelectNodeId}
      />,
    );

    const buildStep = screen.getByTestId("workflow-run-step-button-1");
    const buildBadges = screen.getByTestId("workflow-run-step-knowledge-build-1");
    expect(within(buildBadges).getByText("Build completed")).toBeInTheDocument();
    expect(within(buildBadges).getByText("KB KB-1")).toBeInTheDocument();
    expect(within(buildBadges).getByText("KBV-3")).toBeInTheDocument();
    expect(within(buildBadges).getByText("KBR-3")).toBeInTheDocument();
    expect(
      within(buildBadges).getByText("Object store + AGE"),
    ).toBeInTheDocument();
    expect(
      within(buildBadges).getByText("Apache AGE graph"),
    ).toBeInTheDocument();
    expect(
      within(buildBadges).getByText("Activated KBV-3"),
    ).toBeInTheDocument();
    expect(within(buildBadges).getByText("AGE synced")).toBeInTheDocument();
    expect(buildStep).toHaveTextContent("semantic");
    expect(buildStep).toHaveTextContent("intfloat/e5-large-v2");
    expect(buildStep).toHaveTextContent("spacy");
    expect(buildStep).toHaveTextContent("balanced");
    expect(buildStep).toHaveTextContent("900s timeout");

    await userEvent.click(buildStep);
    expect(onSelectNodeId).toHaveBeenCalledWith("knowledge_build");

    const snapshot = screen.getByTestId("workflow-run-step-snapshot");
    expect(snapshot).toHaveTextContent("Knowledge base");
    expect(snapshot).toHaveTextContent("Build status");
    expect(snapshot).toHaveTextContent("KB version");
    expect(snapshot).toHaveTextContent("Build run");
    expect(snapshot).toHaveTextContent("Wait policy");
    expect(snapshot).toHaveTextContent("Activation");
    expect(snapshot).toHaveTextContent("Graph target");
    expect(snapshot).toHaveTextContent("Default retrieval");
    expect(snapshot).toHaveTextContent("AGE sync");
    expect(snapshot).toHaveTextContent("Build completed");
    expect(snapshot).toHaveTextContent("Object store + AGE");
    expect(snapshot).toHaveTextContent("Apache AGE graph");

    const snapshotBuild = screen.getByTestId(
      "workflow-run-step-snapshot-knowledge-build",
    );
    expect(
      within(snapshotBuild).getByText("Build completed"),
    ).toBeInTheDocument();
    expect(within(snapshotBuild).getByText("KB KB-1")).toBeInTheDocument();
    expect(within(snapshotBuild).getByText("KBV-3")).toBeInTheDocument();
    expect(
      within(snapshotBuild).getByText("Activated KBV-3"),
    ).toBeInTheDocument();
    expect(snapshotBuild).toHaveTextContent("semantic");
    expect(snapshotBuild).toHaveTextContent("intfloat/e5-large-v2");
    expect(snapshotBuild).toHaveTextContent("spacy");
    expect(snapshotBuild).toHaveTextContent("balanced");

    const eventBuild = screen.getByTestId(
      "workflow-run-event-knowledge-build-3",
    );
    expect(within(eventBuild).getByText("Build completed")).toBeInTheDocument();
    expect(within(eventBuild).getByText("KB KB-1")).toBeInTheDocument();
    expect(within(eventBuild).getByText("KBV-3")).toBeInTheDocument();
    expect(
      within(eventBuild).getByText("Activated KBV-3"),
    ).toBeInTheDocument();
    const eventTimeline = screen.getByTestId("workflow-run-event-timeline");
    expect(eventTimeline).toHaveTextContent("semantic");
    expect(eventTimeline).toHaveTextContent("intfloat/e5-large-v2");
    expect(eventTimeline).toHaveTextContent("spacy");
    expect(eventTimeline).toHaveTextContent("AGE synced");
  });

  it("surfaces child workflow diagnostics across the step map, snapshot, and event timeline", async () => {
    const onSelectNodeId = vi.fn();
    const subworkflowManifest: WorkflowManifest = {
      schema_version: 1,
      workflow_id: "WF-parent",
      name: "Parent Workflow",
      nodes: {
        start: {
          id: "start",
          type: "start",
          outputs: { msg: { type: "string" } },
        },
        child_workflow: {
          id: "child_workflow",
          type: "subworkflow",
          workflow_id: "WF-child",
          alias: "prod",
          inputs: { input: { type: "string" } },
          outputs: {
            output: { type: "string" },
            result: { type: "structured" },
          },
        } as WorkflowManifest["nodes"][string],
        final: {
          id: "final",
          type: "output",
          inputs: { response: { type: "string" } },
        },
      },
      edges: [
        {
          id: "e1",
          from: "start",
          to: "child_workflow",
          map: { msg: "input" },
        },
        {
          id: "e2",
          from: "child_workflow",
          to: "final",
          map: { output: "response" },
        },
      ],
    };

    const subworkflowRun: WorkflowRun = {
      ...run(),
      workflow_run_id: "WR-sub",
      workflow_id: "WF-parent",
      workflow_version_id: "WFV-parent",
      current_node_id: "final",
      summary: {
        node_path: ["start", "child_workflow", "final"],
        steps: [
          {
            node_id: "start",
            node_type: "start",
            status: "ok",
            output: "Escalate the refund exception.",
            tool_calls: [],
            handoff_target: null,
            detail: "captured escalation request",
            duration_ms: 18,
            output_by_port: { msg: "Escalate the refund exception." },
          },
          {
            node_id: "child_workflow",
            node_type: "subworkflow",
            status: "ok",
            output: "Escalated to the governed child workflow.",
            tool_calls: [],
            handoff_target: null,
            detail: "executed governed child workflow",
            duration_ms: 212,
            input_by_port: { input: "Escalate the refund exception." },
            output_by_port: {
              output: "Escalated to the governed child workflow.",
              result: {
                status: "completed",
                workflow_id: "WF-child",
                alias: "prod",
                workflow_version_id: "WFV-child",
                workflow_version_number: 4,
                tokens: 17,
                steps: ["child_start", "child_review", "child_final"],
                output: "Escalated to the governed child workflow.",
              },
            },
          },
          {
            node_id: "final",
            node_type: "output",
            status: "ok",
            output: "Escalated to the governed child workflow.",
            tool_calls: [],
            handoff_target: null,
            detail: "returned child workflow response",
            duration_ms: 12,
            input_by_port: {
              response: "Escalated to the governed child workflow.",
            },
            output_by_port: {
              response: "Escalated to the governed child workflow.",
            },
          },
        ],
      },
    };

    const childStep = subworkflowRun.summary?.steps?.[1];
    const subworkflowEvents: WorkflowRunEvent[] = [
      {
        event_id: 11,
        workflow_run_id: "WR-sub",
        project_id: null,
        sequence: 1,
        event_type: "workflow.run.started",
        node_id: null,
        payload: { at: "2026-06-13T00:00:01Z" },
        created_at: "2026-06-13T00:00:01Z",
      },
      {
        event_id: 12,
        workflow_run_id: "WR-sub",
        project_id: null,
        sequence: 2,
        event_type: "workflow.run.step",
        node_id: "start",
        payload: {
          step: subworkflowRun.summary?.steps?.[0] as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:00:01Z",
      },
      {
        event_id: 13,
        workflow_run_id: "WR-sub",
        project_id: null,
        sequence: 3,
        event_type: "workflow.run.step",
        node_id: "child_workflow",
        payload: {
          step: childStep as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:00:02Z",
      },
      {
        event_id: 14,
        workflow_run_id: "WR-sub",
        project_id: null,
        sequence: 4,
        event_type: "workflow.run.completed",
        node_id: null,
        payload: { output: "Escalated to the governed child workflow." },
        created_at: "2026-06-13T00:00:03Z",
      },
    ];

    render(
      <WorkflowRunDebugger
        manifest={subworkflowManifest}
        run={subworkflowRun}
        events={subworkflowEvents}
        onSelectNodeId={onSelectNodeId}
      />,
    );

    const childWorkflowStep = screen.getByTestId("workflow-run-step-button-1");
    expect(within(childWorkflowStep).getByText("WF-child")).toBeInTheDocument();
    expect(
      within(childWorkflowStep).getByText("Alias prod"),
    ).toBeInTheDocument();
    expect(
      within(childWorkflowStep).getByText("Child completed"),
    ).toBeInTheDocument();
    expect(
      within(childWorkflowStep).getByText("WFV-child"),
    ).toBeInTheDocument();
    expect(
      within(childWorkflowStep).getByText("3 child steps"),
    ).toBeInTheDocument();
    expect(
      within(childWorkflowStep).getByText("17 tokens"),
    ).toBeInTheDocument();
    expect(childWorkflowStep).toHaveTextContent(
      "Path child_start -> child_review -> child_final",
    );

    await userEvent.click(childWorkflowStep);
    expect(onSelectNodeId).toHaveBeenCalledWith("child_workflow");

    expect(screen.getByTestId("workflow-run-step-detail")).toHaveTextContent(
      "Escalated to the governed child workflow.",
    );
    const snapshot = screen.getByTestId("workflow-run-step-snapshot");
    expect(snapshot).toHaveTextContent("Child workflow");
    expect(snapshot).toHaveTextContent("WF-child");
    expect(snapshot).toHaveTextContent("Child alias");
    expect(snapshot).toHaveTextContent("Child status");
    expect(snapshot).toHaveTextContent("Child version");
    expect(snapshot).toHaveTextContent("Child steps");
    expect(snapshot).toHaveTextContent("Child tokens");

    const snapshotChild = screen.getByTestId(
      "workflow-run-step-snapshot-subworkflow",
    );
    expect(
      within(snapshotChild).getByText("Child completed"),
    ).toBeInTheDocument();
    expect(within(snapshotChild).getByText("WFV-child")).toBeInTheDocument();
    expect(snapshotChild).toHaveTextContent(
      "Path child_start -> child_review -> child_final",
    );

    const eventChild = screen.getByTestId("workflow-run-event-subworkflow-3");
    expect(within(eventChild).getByText("WF-child")).toBeInTheDocument();
    expect(within(eventChild).getByText("Alias prod")).toBeInTheDocument();
    expect(within(eventChild).getByText("Child completed")).toBeInTheDocument();
    expect(within(eventChild).getByText("3 child steps")).toBeInTheDocument();
    expect(within(eventChild).getByText("17 tokens")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-run-event-timeline")).toHaveTextContent(
      "Path child_start -> child_review -> child_final",
    );
  });

  it("surfaces direct tool-node diagnostics across the step map, snapshot, and event timeline", async () => {
    const toolManifest: WorkflowManifest = {
      schema_version: 1,
      workflow_id: "WF-tool",
      name: "Tool Workflow",
      nodes: {
        start: {
          id: "start",
          type: "start",
          outputs: { query: { type: "string" } },
        },
        policy_lookup: {
          id: "policy_lookup",
          type: "tool",
          tool_ref: "tool:lookup_policy",
          inputs: { input: { type: "string" } },
          outputs: {
            text: { type: "string" },
            result: { type: "structured" },
          },
        } as WorkflowManifest["nodes"][string],
        final: {
          id: "final",
          type: "output",
          inputs: { response: { type: "string" } },
        },
      },
      edges: [
        {
          id: "e1",
          from: "start",
          to: "policy_lookup",
          map: { query: "input" },
        },
        {
          id: "e2",
          from: "policy_lookup",
          to: "final",
          map: { text: "response" },
        },
      ],
    };

    const toolRun: WorkflowRun = {
      ...run(),
      workflow_run_id: "WR-tool",
      workflow_id: "WF-tool",
      workflow_version_id: "WFV-tool",
      current_node_id: "final",
      summary: {
        node_path: ["start", "policy_lookup", "final"],
        steps: [
          {
            node_id: "start",
            node_type: "start",
            status: "ok",
            output: "refund policy",
            tool_calls: [],
            handoff_target: null,
            detail: "captured request",
            duration_ms: 19,
            output_by_port: { query: "refund policy" },
          },
          {
            node_id: "policy_lookup",
            node_type: "tool",
            status: "ok",
            output: "Found refund coverage.",
            tool_calls: [],
            handoff_target: null,
            detail: "invoked lookup_policy",
            duration_ms: 144,
            input_by_port: { input: "refund policy" },
            output_by_port: {
              text: "Found refund coverage.",
              result: {
                matched_policy: "refund-policy",
              },
              metadata: {
                tool_name: "lookup_policy",
                registry_ref: "tool:lookup_policy",
                binding_type: "registered_function",
                requires_approval: true,
                side_effect_level: "read",
                module_path: "caliber.workflows.demo_tools",
                callable_name: "lookup_policy",
                arguments: {
                  policy_id: "refund-policy",
                  topic: "refunds",
                },
              },
              tool_calls: [
                {
                  tool: "lookup_policy",
                  registry_ref: "tool:lookup_policy",
                  binding_type: "registered_function",
                  arguments: {
                    policy_id: "refund-policy",
                    topic: "refunds",
                  },
                  result: {
                    text: "Found refund coverage.",
                  },
                },
              ],
            },
          },
          {
            node_id: "final",
            node_type: "output",
            status: "ok",
            output: "Found refund coverage.",
            tool_calls: [],
            handoff_target: null,
            detail: "returned tool result",
            duration_ms: 8,
            input_by_port: { response: "Found refund coverage." },
            output_by_port: { response: "Found refund coverage." },
          },
        ],
      },
    };

    const toolEvents: WorkflowRunEvent[] = [
      {
        event_id: 11,
        workflow_run_id: "WR-tool",
        project_id: null,
        sequence: 1,
        event_type: "workflow.run.started",
        node_id: null,
        payload: { at: "2026-06-13T00:10:01Z" },
        created_at: "2026-06-13T00:10:01Z",
      },
      {
        event_id: 12,
        workflow_run_id: "WR-tool",
        project_id: null,
        sequence: 2,
        event_type: "workflow.run.step",
        node_id: "start",
        payload: {
          step: toolRun.summary?.steps?.[0] as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:10:01Z",
      },
      {
        event_id: 13,
        workflow_run_id: "WR-tool",
        project_id: null,
        sequence: 3,
        event_type: "workflow.run.step",
        node_id: "policy_lookup",
        payload: {
          step: toolRun.summary?.steps?.[1] as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:10:02Z",
      },
      {
        event_id: 14,
        workflow_run_id: "WR-tool",
        project_id: null,
        sequence: 4,
        event_type: "workflow.run.completed",
        node_id: null,
        payload: { output: "Found refund coverage." },
        created_at: "2026-06-13T00:10:03Z",
      },
    ];

    render(
      <WorkflowRunDebugger
        manifest={toolManifest}
        run={toolRun}
        events={toolEvents}
      />,
    );

    const toolStep = screen.getByTestId("workflow-run-step-button-1");
    const toolBadges = screen.getByTestId("workflow-run-step-tool-1");
    expect(within(toolBadges).getByText("lookup_policy")).toBeInTheDocument();
    expect(
      within(toolBadges).getByText("Registered function"),
    ).toBeInTheDocument();
    expect(
      within(toolBadges).getByText("tool:lookup_policy"),
    ).toBeInTheDocument();
    expect(
      within(toolBadges).getByText("Approval required"),
    ).toBeInTheDocument();
    expect(
      within(toolStep).getByText(
        "Binding caliber.workflows.demo_tools:lookup_policy",
      ),
    ).toBeInTheDocument();
    expect(
      within(toolStep).getByText("2 keys: policy_id, topic"),
    ).toBeInTheDocument();

    await userEvent.click(toolStep);

    const snapshot = screen.getByTestId("workflow-run-step-snapshot");
    expect(within(snapshot).getByText("Tool")).toBeInTheDocument();
    expect(within(snapshot).getByText("Binding type")).toBeInTheDocument();
    expect(snapshot).toHaveTextContent("Registered function");
    expect(snapshot).toHaveTextContent("tool:lookup_policy");
    expect(snapshot).toHaveTextContent(
      "caliber.workflows.demo_tools:lookup_policy",
    );
    const snapshotTool = screen.getByTestId("workflow-run-step-snapshot-tool");
    expect(within(snapshotTool).getByText("lookup_policy")).toBeInTheDocument();
    expect(
      within(snapshotTool).getByText("Registered function"),
    ).toBeInTheDocument();

    const eventTool = screen.getByTestId("workflow-run-event-tool-3");
    expect(within(eventTool).getByText("lookup_policy")).toBeInTheDocument();
    expect(
      within(eventTool).getByText("Registered function"),
    ).toBeInTheDocument();
    expect(
      within(eventTool).getByText("Approval required"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("workflow-run-event-timeline")).toHaveTextContent(
      "Binding caliber.workflows.demo_tools:lookup_policy",
    );
  });

  it("surfaces orchestration diagnostics across the step map, snapshot, and event timeline", async () => {
    const orchestrationManifest: WorkflowManifest = {
      schema_version: 1,
      workflow_id: "WF-orch",
      name: "Orchestration Workflow",
      nodes: {
        start: {
          id: "start",
          type: "start",
          outputs: { items: { type: "array" } },
        },
        fanout: {
          id: "fanout",
          type: "for_each",
          inputs: { items: { type: "array" } },
          outputs: {
            results: { type: "array" },
            text: { type: "string" },
            metadata: { type: "structured" },
          },
        } as WorkflowManifest["nodes"][string],
        merge: {
          id: "merge",
          type: "join",
          inputs: {
            results: { type: "array" },
            policy: { type: "string" },
            notes: { type: "string" },
          },
          outputs: {
            output: { type: "string" },
            merged: { type: "structured" },
          },
        },
        guard: {
          id: "guard",
          type: "error_boundary",
          inputs: { input: { type: "string" } },
          outputs: {
            output: { type: "string" },
            error: { type: "structured" },
          },
        } as WorkflowManifest["nodes"][string],
        final: {
          id: "final",
          type: "output",
          inputs: { response: { type: "string" } },
        },
      },
      edges: [
        { id: "e1", from: "start", to: "fanout", map: { items: "items" } },
        { id: "e2", from: "fanout", to: "merge", map: { text: "notes" } },
        { id: "e3", from: "merge", to: "guard", map: { output: "input" } },
        { id: "e4", from: "guard", to: "final", map: { output: "response" } },
      ],
    };

    const orchestrationRun: WorkflowRun = {
      ...run(),
      workflow_run_id: "WR-orch",
      workflow_id: "WF-orch",
      workflow_version_id: "WFV-orch",
      current_node_id: "final",
      summary: {
        node_path: ["start", "fanout", "merge", "guard", "final"],
        steps: [
          {
            node_id: "start",
            node_type: "start",
            status: "ok",
            output: "chunk-a.md\nchunk-b.md\nchunk-c.md",
            tool_calls: [],
            handoff_target: null,
            detail: "captured chunk set",
            duration_ms: 18,
            output_by_port: {
              items: ["chunk-a.md", "chunk-b.md", "chunk-c.md"],
            },
          },
          {
            node_id: "fanout",
            node_type: "for_each",
            status: "ok",
            output: "chunk-a summary\nchunk-c summary",
            tool_calls: [],
            handoff_target: null,
            detail: "processed 3 item(s) via agent (1 failed)",
            duration_ms: 312,
            input_by_port: {
              items: ["chunk-a.md", "chunk-b.md", "chunk-c.md"],
            },
            output_by_port: {
              results: [
                {
                  item: "chunk-a.md",
                  output: "chunk-a summary",
                  status: "ok",
                  tool_calls: [{ tool: "summarize" }],
                },
                {
                  item: "chunk-b.md",
                  output: "",
                  error: "rate limit exceeded",
                  tool_calls: [],
                },
                {
                  item: "chunk-c.md",
                  output: "chunk-c summary",
                  status: "ok",
                  artifacts: ["summary.json"],
                  tool_calls: [],
                },
              ],
              metadata: {
                count: 3,
                failed: 1,
                target_node_id: "summarize_agent",
                target_node_type: "agent",
                artifacts: {
                  "item-0/summary.json": "{}",
                  "item-2/summary.json": "{}",
                },
              },
            },
          },
          {
            node_id: "merge",
            node_type: "join",
            status: "ok",
            output: "merged synthesis",
            tool_calls: [],
            handoff_target: null,
            detail: "merged branch outputs",
            duration_ms: 27,
            input_by_port: {
              results: ["chunk-a summary", "chunk-c summary"],
              policy: "refund coverage",
              notes: "fanout notes",
            },
            output_by_port: {
              output: "merged synthesis",
              merged: {
                policy: "refund coverage",
                notes: "fanout notes",
                response: "merged synthesis",
              },
            },
          },
          {
            node_id: "guard",
            node_type: "error_boundary",
            status: "ok",
            output: "fallback synthesis",
            tool_calls: [],
            handoff_target: null,
            detail: "handled error: upstream timeout",
            duration_ms: 61,
            input_by_port: {
              input: "merged synthesis",
            },
            output_by_port: {
              output: "fallback synthesis",
              error: {
                message: "upstream timeout",
                target_node_id: "fetch_policy",
                target_node_type: "tool",
                compensation_node_id: "fallback_agent",
                compensation_node_type: "agent",
                compensation_outputs: {
                  output: "fallback synthesis",
                },
                artifacts: {
                  "fallback/log.txt": "timeout observed",
                },
              },
            },
          },
          {
            node_id: "final",
            node_type: "output",
            status: "ok",
            output: "fallback synthesis",
            tool_calls: [],
            handoff_target: null,
            detail: "returned guarded result",
            duration_ms: 9,
            input_by_port: { response: "fallback synthesis" },
            output_by_port: { response: "fallback synthesis" },
          },
        ],
      },
    };

    const orchestrationEvents: WorkflowRunEvent[] = [
      {
        event_id: 201,
        workflow_run_id: "WR-orch",
        project_id: null,
        sequence: 1,
        event_type: "workflow.run.started",
        node_id: null,
        payload: { at: "2026-06-13T00:20:01Z" },
        created_at: "2026-06-13T00:20:01Z",
      },
      {
        event_id: 202,
        workflow_run_id: "WR-orch",
        project_id: null,
        sequence: 2,
        event_type: "workflow.run.step",
        node_id: "start",
        payload: {
          step: orchestrationRun.summary?.steps?.[0] as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:20:01Z",
      },
      {
        event_id: 203,
        workflow_run_id: "WR-orch",
        project_id: null,
        sequence: 3,
        event_type: "workflow.run.step",
        node_id: "fanout",
        payload: {
          step: orchestrationRun.summary?.steps?.[1] as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:20:02Z",
      },
      {
        event_id: 204,
        workflow_run_id: "WR-orch",
        project_id: null,
        sequence: 4,
        event_type: "workflow.run.step",
        node_id: "merge",
        payload: {
          step: orchestrationRun.summary?.steps?.[2] as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:20:02Z",
      },
      {
        event_id: 205,
        workflow_run_id: "WR-orch",
        project_id: null,
        sequence: 5,
        event_type: "workflow.run.step",
        node_id: "guard",
        payload: {
          step: orchestrationRun.summary?.steps?.[3] as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:20:03Z",
      },
      {
        event_id: 206,
        workflow_run_id: "WR-orch",
        project_id: null,
        sequence: 6,
        event_type: "workflow.run.completed",
        node_id: null,
        payload: { output: "fallback synthesis" },
        created_at: "2026-06-13T00:20:03Z",
      },
    ];

    render(
      <WorkflowRunDebugger
        manifest={orchestrationManifest}
        run={orchestrationRun}
        events={orchestrationEvents}
      />,
    );

    const loopStep = screen.getByTestId("workflow-run-step-button-1");
    expect(within(loopStep).getByText("3 items")).toBeInTheDocument();
    expect(
      within(loopStep).getByText("Target summarize_agent · Agent"),
    ).toBeInTheDocument();
    expect(within(loopStep).getByText("1 failed")).toBeInTheDocument();
    expect(
      within(loopStep).getByText("Failure chunk-b.md: rate limit exceeded"),
    ).toBeInTheDocument();

    const joinStep = screen.getByTestId("workflow-run-step-button-2");
    expect(within(joinStep).getByText("3 merged ports")).toBeInTheDocument();
    expect(
      within(joinStep).getByText("Keys notes, policy, response"),
    ).toBeInTheDocument();

    const boundaryStep = screen.getByTestId("workflow-run-step-button-3");
    expect(
      within(boundaryStep).getByText("Handled failure"),
    ).toBeInTheDocument();
    expect(
      within(boundaryStep).getByText("Protected fetch_policy · Tool"),
    ).toBeInTheDocument();
    expect(
      within(boundaryStep).getByText("Compensation fallback_agent · Agent"),
    ).toBeInTheDocument();

    await userEvent.click(joinStep);
    const snapshotJoin = screen.getByTestId("workflow-run-step-snapshot-join");
    expect(
      within(snapshotJoin).getByText("3 merged ports"),
    ).toBeInTheDocument();
    expect(within(snapshotJoin).getByText("notes")).toBeInTheDocument();
    expect(within(snapshotJoin).getByText("policy")).toBeInTheDocument();
    expect(within(snapshotJoin).getByText("response")).toBeInTheDocument();

    await userEvent.click(boundaryStep);
    const snapshot = screen.getByTestId("workflow-run-step-snapshot");
    const snapshotBoundary = screen.getByTestId(
      "workflow-run-step-snapshot-error-boundary",
    );
    expect(
      within(snapshotBoundary).getByText("Handled failure"),
    ).toBeInTheDocument();
    expect(snapshot).toHaveTextContent("upstream timeout");
    expect(snapshot).toHaveTextContent("Recovery fallback synthesis");

    const eventBoundary = screen.getByTestId(
      "workflow-run-event-error-boundary-5",
    );
    expect(
      within(eventBoundary).getByText("Handled failure"),
    ).toBeInTheDocument();
    expect(
      within(eventBoundary).getByText("Protected fetch_policy · Tool"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("workflow-run-event-timeline")).toHaveTextContent(
      "upstream timeout",
    );
  });

  it("surfaces queued, started, failed, and retried lifecycle diagnostics around the relevant steps", async () => {
    const lifecycleRun: WorkflowRun = {
      ...run(),
      workflow_run_id: "WR-lifecycle",
      status: "failed",
      current_node_id: null,
      completed_at: "2026-06-13T00:20:04Z",
      summary: {
        node_path: ["start", "knowledge"],
        steps: [
          {
            node_id: "start",
            node_type: "start",
            status: "ok",
            output: "deployment status",
            tool_calls: [],
            handoff_target: null,
            detail: "captured lifecycle question",
            duration_ms: 13,
            output_by_port: { msg: "deployment status" },
          },
          {
            node_id: "knowledge",
            node_type: "knowledge_query",
            status: "error",
            output: "query failed",
            tool_calls: [],
            handoff_target: null,
            detail: "knowledge query failed",
            duration_ms: 108,
            input_by_port: { question: "deployment status" },
            output_by_port: { answer: "query failed" },
          },
        ],
      },
    };

    const lifecycleEvents: WorkflowRunEvent[] = [
      {
        event_id: 21,
        workflow_run_id: "WR-lifecycle",
        project_id: null,
        sequence: 1,
        event_type: "workflow.run.queued",
        node_id: null,
        payload: { actor: "@ops" },
        created_at: "2026-06-13T00:20:00Z",
      },
      {
        event_id: 22,
        workflow_run_id: "WR-lifecycle",
        project_id: null,
        sequence: 2,
        event_type: "workflow.run.started",
        node_id: null,
        payload: { worker_id: "worker-1" },
        created_at: "2026-06-13T00:20:01Z",
      },
      {
        event_id: 23,
        workflow_run_id: "WR-lifecycle",
        project_id: null,
        sequence: 3,
        event_type: "workflow.run.step",
        node_id: "start",
        payload: {
          step: lifecycleRun.summary?.steps?.[0] as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:20:01Z",
      },
      {
        event_id: 24,
        workflow_run_id: "WR-lifecycle",
        project_id: null,
        sequence: 4,
        event_type: "workflow.run.step",
        node_id: "knowledge",
        payload: {
          step: lifecycleRun.summary?.steps?.[1] as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:20:03Z",
      },
      {
        event_id: 25,
        workflow_run_id: "WR-lifecycle",
        project_id: null,
        sequence: 5,
        event_type: "workflow.run.failed",
        node_id: null,
        payload: { status: "failed", error: "vector index unavailable" },
        created_at: "2026-06-13T00:20:04Z",
      },
      {
        event_id: 26,
        workflow_run_id: "WR-lifecycle",
        project_id: null,
        sequence: 6,
        event_type: "workflow.run.retried",
        node_id: null,
        payload: { retried_run_id: "WR-lifecycle-retry" },
        created_at: "2026-06-13T00:20:05Z",
      },
    ];

    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={lifecycleRun}
        events={lifecycleEvents}
      />,
    );

    const startStep = screen.getByTestId("workflow-run-step-button-0");
    expect(within(startStep).getByText("Queued")).toBeInTheDocument();
    expect(within(startStep).getByText("Run started")).toBeInTheDocument();

    const failedStep = screen.getByTestId("workflow-run-step-button-1");
    expect(within(failedStep).getByText("Run failed")).toBeInTheDocument();
    expect(within(failedStep).getByText("Retried")).toBeInTheDocument();

    await userEvent.click(failedStep);
    expect(screen.getByTestId("workflow-run-step-detail")).toHaveTextContent(
      "Run failed",
    );
    expect(screen.getByTestId("workflow-run-step-detail")).toHaveTextContent(
      "Retried",
    );
    expect(screen.getByTestId("workflow-run-event-timeline")).toHaveTextContent(
      "Run failed · vector index unavailable",
    );
    expect(screen.getByTestId("workflow-run-event-timeline")).toHaveTextContent(
      "Retried as WR-lifecycle-retry",
    );
  });

  it("surfaces expired lifecycle diagnostics around the relevant step", async () => {
    const expiredRun: WorkflowRun = {
      ...run(),
      workflow_run_id: "WR-expired",
      status: "expired",
      current_node_id: "knowledge",
      completed_at: null,
      lease_expires_at: "2026-06-13T00:25:04Z",
      summary: {
        node_path: ["start", "knowledge"],
        steps: [
          {
            node_id: "start",
            node_type: "start",
            status: "ok",
            output: "deployment status",
            tool_calls: [],
            handoff_target: null,
            detail: "captured lifecycle question",
            duration_ms: 13,
            output_by_port: { msg: "deployment status" },
          },
          {
            node_id: "knowledge",
            node_type: "knowledge_query",
            status: "ok",
            output: "lease lost while finishing query",
            tool_calls: [],
            handoff_target: null,
            detail: "query finished but lease expired",
            duration_ms: 108,
            input_by_port: { question: "deployment status" },
            output_by_port: { answer: "lease lost while finishing query" },
          },
        ],
      },
    };

    const expiredEvents: WorkflowRunEvent[] = [
      {
        event_id: 31,
        workflow_run_id: "WR-expired",
        project_id: null,
        sequence: 1,
        event_type: "workflow.run.queued",
        node_id: null,
        payload: { actor: "@ops" },
        created_at: "2026-06-13T00:25:00Z",
      },
      {
        event_id: 32,
        workflow_run_id: "WR-expired",
        project_id: null,
        sequence: 2,
        event_type: "workflow.run.started",
        node_id: null,
        payload: { worker_id: "worker-7" },
        created_at: "2026-06-13T00:25:01Z",
      },
      {
        event_id: 33,
        workflow_run_id: "WR-expired",
        project_id: null,
        sequence: 3,
        event_type: "workflow.run.step",
        node_id: "start",
        payload: {
          step: expiredRun.summary?.steps?.[0] as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:25:01Z",
      },
      {
        event_id: 34,
        workflow_run_id: "WR-expired",
        project_id: null,
        sequence: 4,
        event_type: "workflow.run.step",
        node_id: "knowledge",
        payload: {
          step: expiredRun.summary?.steps?.[1] as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:25:03Z",
      },
      {
        event_id: 35,
        workflow_run_id: "WR-expired",
        project_id: null,
        sequence: 5,
        event_type: "workflow.run.expired",
        node_id: null,
        payload: { error: "worker lease lost" },
        created_at: "2026-06-13T00:25:04Z",
      },
    ];

    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={expiredRun}
        events={expiredEvents}
      />,
    );

    const expiredStep = screen.getByTestId("workflow-run-step-button-1");
    expect(within(expiredStep).getByText("Run expired")).toBeInTheDocument();

    await userEvent.click(expiredStep);
    expect(screen.getByTestId("workflow-run-step-detail")).toHaveTextContent(
      "Run expired",
    );
    expect(screen.getByTestId("workflow-run-event-timeline")).toHaveTextContent(
      "Run expired · worker lease lost",
    );
  });

  it("surfaces recovered lease diagnostics around the relevant step", () => {
    const recoveredRun: WorkflowRun = {
      ...run(),
      workflow_run_id: "WR-recovered",
      status: "queued",
      completed_at: null,
      current_node_id: null,
      summary: {
        node_path: ["start", "knowledge"],
        steps: (run().summary?.steps ?? []).slice(0, 2),
      },
    };

    const recoveredEvents: WorkflowRunEvent[] = [
      {
        event_id: 41,
        workflow_run_id: "WR-recovered",
        project_id: null,
        sequence: 1,
        event_type: "workflow.run.started",
        node_id: null,
        payload: { worker_id: "worker-1" },
        created_at: "2026-06-13T00:30:00Z",
      },
      {
        event_id: 42,
        workflow_run_id: "WR-recovered",
        project_id: null,
        sequence: 2,
        event_type: "workflow.run.step",
        node_id: "start",
        payload: {
          step: recoveredRun.summary?.steps?.[0] as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:30:01Z",
      },
      {
        event_id: 43,
        workflow_run_id: "WR-recovered",
        project_id: null,
        sequence: 3,
        event_type: "workflow.run.step",
        node_id: "knowledge",
        payload: {
          step: recoveredRun.summary?.steps?.[1] as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:30:02Z",
      },
      {
        event_id: 44,
        workflow_run_id: "WR-recovered",
        project_id: null,
        sequence: 4,
        event_type: "workflow.run.recovered",
        node_id: null,
        payload: { reason: "lease_expired", worker_id: "worker-7" },
        created_at: "2026-06-13T00:30:03Z",
      },
    ];

    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={recoveredRun}
        events={recoveredEvents}
      />,
    );

    const recoveredStep = screen.getByTestId("workflow-run-step-button-1");
    expect(within(recoveredStep).getByText("Recovered")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-run-event-timeline")).toHaveTextContent(
      "Run recovered · worker lease expired",
    );
  });

  it("turns missing step telemetry into recovery guidance backed by other run evidence", () => {
    const lightweightRun: WorkflowRun = {
      ...run(),
      summary: {
        node_path: ["start", "knowledge", "final"],
      },
    };
    const lifecycleOnlyEvents = events().filter(
      (event) => event.event_type !== "workflow.run.step",
    );

    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={lightweightRun}
        events={lifecycleOnlyEvents}
        checkpoints={[runtimeApprovalCheckpoint()]}
      />,
    );

    const emptyState = screen.getByTestId("workflow-run-debugger-empty");
    expect(emptyState).toHaveTextContent("No recorded step details yet.");
    expect(emptyState).toHaveTextContent("2 lifecycle events and 1 checkpoint");
    expect(emptyState).toHaveTextContent(
      "This execution completed with only lightweight persisted evidence.",
    );
    expect(emptyState).toHaveTextContent(
      "final outputs, and generated artifacts",
    );
  });

  it("turns missing step telemetry into in-flight guidance for active runs", () => {
    const lightweightRun: WorkflowRun = {
      ...run(),
      status: "running",
      completed_at: null,
      current_node_id: "knowledge",
      summary: {
        node_path: ["start", "knowledge"],
      },
    };
    const lifecycleOnlyEvents = events().filter(
      (event) => event.event_type !== "workflow.run.step",
    );

    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={lightweightRun}
        events={lifecycleOnlyEvents}
        checkpoints={[runtimeApprovalCheckpoint()]}
      />,
    );

    const emptyState = screen.getByTestId("workflow-run-debugger-empty");
    expect(emptyState).toHaveTextContent("No recorded step details yet.");
    expect(emptyState).toHaveTextContent("2 lifecycle events and 1 checkpoint");
    expect(emptyState).toHaveTextContent(
      "This run may still be executing or step persistence may still be catching up.",
    );
    expect(emptyState).toHaveTextContent("while execution continues");
  });

  it("turns an empty event timeline into guidance that uses step and checkpoint evidence", () => {
    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={run()}
        events={[]}
        checkpoints={[runtimeApprovalCheckpoint()]}
      />,
    );

    const timeline = screen.getByTestId("workflow-run-event-timeline");
    expect(timeline).toHaveTextContent(
      "No persisted run events were found for this workflow run.",
    );
    expect(timeline).toHaveTextContent(
      "This execution completed without stored event history",
    );
    expect(timeline).toHaveTextContent(
      "stored checkpoint details, and final outputs",
    );
  });

  it("turns an empty event timeline into in-flight guidance when the run is still active", () => {
    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={{
          ...run(),
          status: "running",
          completed_at: null,
          current_node_id: "knowledge",
        }}
        events={[]}
        checkpoints={[runtimeApprovalCheckpoint()]}
      />,
    );

    const timeline = screen.getByTestId("workflow-run-event-timeline");
    expect(timeline).toHaveTextContent(
      "No persisted run events were found for this workflow run yet.",
    );
    expect(timeline).toHaveTextContent(
      "This run may still be executing or event persistence may still be catching up",
    );
    expect(timeline).toHaveTextContent(
      "while execution continues",
    );
  });

  it("turns an empty event timeline into completed-run guidance when stored event history is missing", () => {
    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={{
          ...run(),
          status: "completed",
          completed_at: "2026-06-13T00:02:00Z",
        }}
        events={[]}
        checkpoints={[runtimeApprovalCheckpoint()]}
      />,
    );

    const timeline = screen.getByTestId("workflow-run-event-timeline");
    expect(timeline).toHaveTextContent(
      "No persisted run events were found for this workflow run.",
    );
    expect(timeline).toHaveTextContent(
      "This execution completed without stored event history",
    );
    expect(timeline).toHaveTextContent(
      "stored checkpoint details, and final outputs",
    );
  });

  it("turns unchanged transition diffs into completed-run guidance", async () => {
    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={sameSnapshotRun("completed")}
        events={[]}
      />,
    );

    await userEvent.click(screen.getByTestId("workflow-run-step-button-1"));

    const transition = screen.getByTestId("workflow-run-step-diff-transition");
    expect(transition).toHaveTextContent(
      "No port-level changes were recorded between these persisted steps.",
    );
    expect(transition).toHaveTextContent(
      "received the same snapshot that the previous step emitted",
    );
    expect(transition).toHaveTextContent(
      "step detail, tool calls, final outputs, and generated artifacts",
    );
  });

  it("turns unchanged transition diffs into stopped-run guidance", async () => {
    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={sameSnapshotRun("failed")}
        events={[]}
      />,
    );

    await userEvent.click(screen.getByTestId("workflow-run-step-button-1"));

    const transition = screen.getByTestId("workflow-run-step-diff-transition");
    expect(transition).toHaveTextContent(
      "No port-level changes were recorded between these persisted steps before the run stopped.",
    );
    expect(transition).toHaveTextContent(
      "checkpoint trail, and recovery diagnostics",
    );
    expect(transition).toHaveTextContent(
      "stalled while carrying the same snapshot forward",
    );
  });
});
