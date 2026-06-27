import { describe, expect, it } from "vitest";

import type { WorkflowRun, WorkflowRunCheckpoint } from "@/api/workflowTypes";
import {
  buildSyntheticWorkflowRunManifest,
  normalizeWorkflowRunArtifactPersistence,
  workflowRunArtifactPersistence,
} from "@/lib/workflowRunSummary";

function makeRun(overrides: Partial<WorkflowRun> = {}): WorkflowRun {
  return {
    workflow_run_id: "WR-1",
    workflow_id: "WF-1",
    project_id: null,
    tenant_id: null,
    workflow_version_id: "WFV-1",
    deployment_alias: null,
    mlflow_run_id: null,
    trace_id: null,
    session_id: null,
    status: "running",
    source: "manual",
    priority: null,
    queued_at: null,
    started_at: null,
    completed_at: null,
    summary: {},
    ...overrides,
  };
}

function makeCheckpoint(
  overrides: Partial<WorkflowRunCheckpoint> = {},
): WorkflowRunCheckpoint {
  return {
    checkpoint_id: "CHK-1",
    workflow_run_id: "WR-1",
    project_id: null,
    sequence: 1,
    node_id: "wait_gate",
    state_blob: {
      kind: "wait_for_event",
      node_id: "wait_gate",
      input_by_port: { input: "resume on event" },
      output: "resume on event",
    },
    created_at: null,
    ...overrides,
  };
}

describe("buildSyntheticWorkflowRunManifest", () => {
  it("reconstructs a sequential graph from node path and current node", () => {
    const manifest = buildSyntheticWorkflowRunManifest(
      makeRun({
        status: "waiting_event",
        current_node_id: "wait_gate",
        summary: {
          node_path: ["start", "wait_gate"],
          steps: [],
        },
      }),
    );

    expect(manifest).not.toBeNull();
    expect(manifest?.workflow_id).toBe("WF-1");
    expect(Object.keys(manifest?.nodes ?? {})).toEqual(["start", "wait_gate"]);
    expect(manifest?.nodes.start.type).toBe("start");
    expect(manifest?.nodes.wait_gate.type).toBe("note");
    expect(manifest?.edges).toEqual([
      { id: "synthetic-start-wait_gate", from: "start", to: "wait_gate", map: {} },
    ]);
  });

  it("preserves recorded step types and infers simple port types from step payloads", () => {
    const manifest = buildSyntheticWorkflowRunManifest(
      makeRun({
        status: "completed",
        current_node_id: "final",
        summary: {
          node_path: ["start", "support_agent", "final"],
          steps: [
            {
              node_id: "support_agent",
              node_type: "agent",
              status: "ok",
              output: "Resolved",
              model: "gpt-4.1",
              detail: "Handled support request",
              duration_ms: 42,
              tool_calls: [],
              input_by_port: { input: "Customer needs help", metadata: { tier: "gold" } },
              output_by_port: { final_output: "Resolved", approved: true },
            },
          ],
        },
      }),
    );

    expect(manifest).not.toBeNull();
    expect(manifest?.nodes.support_agent.type).toBe("agent");
    expect(manifest?.nodes.support_agent.instructions).toEqual({
      type: "inline",
      text: "Handled support request",
    });
    expect(manifest?.nodes.support_agent.inputs).toEqual({
      input: { type: "string" },
      metadata: { type: "structured" },
    });
    expect(manifest?.nodes.support_agent.outputs).toEqual({
      final_output: { type: "string" },
      approved: { type: "boolean" },
    });
    expect(manifest?.nodes.final.type).toBe("output");
    expect(manifest?.edges).toEqual([
      { id: "synthetic-start-support_agent", from: "start", to: "support_agent", map: {} },
      { id: "synthetic-support_agent-final", from: "support_agent", to: "final", map: {} },
    ]);
  });

  it("reconstructs a blocked wait graph from checkpoints when step history is unavailable", () => {
    const manifest = buildSyntheticWorkflowRunManifest(
      makeRun({
        status: "waiting_event",
        current_node_id: "wait_gate",
        summary: {
          resume_checkpoint_id: "CHK-1",
          steps: [],
          node_path: [],
        },
      }),
      [makeCheckpoint()],
    );

    expect(manifest).not.toBeNull();
    expect(Object.keys(manifest?.nodes ?? {})).toEqual(["start", "wait_gate"]);
    expect(manifest?.nodes.wait_gate.type).toBe("wait_for_event");
    expect(manifest?.nodes.wait_gate.inputs).toEqual({
      input: { type: "string" },
    });
    expect(manifest?.nodes.wait_gate.outputs).toEqual({
      output: { type: "string" },
      event_payload: { type: "structured" },
      event_name: { type: "string" },
    });
    expect(manifest?.edges).toEqual([
      { id: "synthetic-start-wait_gate", from: "start", to: "wait_gate", map: {} },
    ]);
  });

  it("adds a synthetic start node when current node is the only recorded clue", () => {
    const manifest = buildSyntheticWorkflowRunManifest(
      makeRun({
        status: "waiting_approval",
        current_node_id: "human_gate",
        summary: {},
      }),
    );

    expect(manifest).not.toBeNull();
    expect(Object.keys(manifest?.nodes ?? {})).toEqual(["start", "human_gate"]);
    expect(manifest?.edges).toEqual([
      { id: "synthetic-start-human_gate", from: "start", to: "human_gate", map: {} },
    ]);
  });
});

describe("workflowRunArtifactPersistence", () => {
  it("normalizes persisted artifact upload summaries from run metadata", () => {
    expect(
      workflowRunArtifactPersistence(
        makeRun({
          summary: {
            artifact_persistence: {
              status: "persisted",
              bucket: "caliber-suite",
              object_count: "3",
              artifact_names: ["kg.json", "", "report.html"],
              persisted_object_count: "3",
              recent_persisted_keys: ["pipeline/WR-1/kg.json", "", "logs/WR-1.json"],
            },
          } as unknown as WorkflowRun["summary"],
        }),
      ),
    ).toEqual({
      status: "persisted",
      bucket: "caliber-suite",
      object_count: 3,
      artifact_names: ["kg.json", "report.html"],
      persisted_object_count: 3,
      recent_persisted_keys: ["pipeline/WR-1/kg.json", "logs/WR-1.json"],
    });
  });

  it("ignores malformed artifact persistence snapshots", () => {
    expect(
      normalizeWorkflowRunArtifactPersistence({
        status: "persisted",
        object_count: 2,
      }),
    ).toBeNull();
    expect(
      workflowRunArtifactPersistence(
        makeRun({
          summary: {
            artifact_persistence: {
              status: "",
              bucket: "caliber-suite",
              object_count: 2,
            },
          },
        }),
      ),
    ).toBeNull();
  });

  it("preserves partial-progress diagnostics for failed uploads", () => {
    expect(
      normalizeWorkflowRunArtifactPersistence({
        status: "failed",
        bucket: "caliber-suite",
        object_count: 3,
        persisted_object_count: "1",
        artifact_names: ["kg.json"],
        recent_persisted_keys: ["pipeline/WR-1/kg.json"],
        failed_object_key: "pipeline/WR-1/report.html",
        error: "RuntimeError: object store offline",
      }),
    ).toEqual({
      status: "failed",
      bucket: "caliber-suite",
      object_count: 3,
      persisted_object_count: 1,
      artifact_names: ["kg.json"],
      recent_persisted_keys: ["pipeline/WR-1/kg.json"],
      failed_object_key: "pipeline/WR-1/report.html",
      error: "RuntimeError: object store offline",
    });
  });
});
