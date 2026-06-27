import { describe, expect, it, vi } from "vitest";

import type {
  WorkflowRun,
  WorkflowRunCheckpoint,
  WorkflowRunEvent,
  WorkflowRunLineage,
  WorkflowRuntimeApproval,
} from "@/api/workflowTypes";
import { WorkflowRunCheckpointPanel } from "@/components/workflows/WorkflowRunCheckpointPanel";
import { WorkflowRunLineagePanel } from "@/components/workflows/WorkflowRunLineagePanel";
import { WorkflowRunRecoveryPanel } from "@/components/workflows/WorkflowRunRecoveryPanel";
import { render, screen, userEvent, within } from "@/test/utils";

function baseRun(overrides: Partial<WorkflowRun> = {}): WorkflowRun {
  return {
    workflow_run_id: "WR-2",
    workflow_id: "WF-1",
    project_id: null,
    tenant_id: null,
    workflow_version_id: "WFV-1",
    deployment_alias: "prod",
    mlflow_run_id: null,
    trace_id: "trace-2",
    session_id: null,
    status: "waiting_event",
    source: "manual",
    priority: 0,
    queued_at: "2026-06-13T00:00:00Z",
    started_at: "2026-06-13T00:00:10Z",
    completed_at: null,
    current_node_id: "approval",
    last_heartbeat_at: "2026-06-13T00:01:00Z",
    lease_expires_at: "2099-06-13T00:02:00Z",
    attempt_number: 2,
    parent_run_id: "WR-1",
    summary: {
      resume_checkpoint_id: "CHK-2",
    },
    ...overrides,
  };
}

function checkpoints(): WorkflowRunCheckpoint[] {
  return [
    {
      checkpoint_id: "CHK-2",
      workflow_run_id: "WR-2",
      project_id: null,
      sequence: 2,
      node_id: "approval",
      state_blob: {
        kind: "wait_for_event",
        expected_event_name: "ticket.approved",
        correlation_key: "ticket_id",
        correlation_value: "T-42",
        timeout_seconds: 600,
        input_by_port: {
          question: "Approve the launch checklist?",
        },
        output_by_port: {
          status: "waiting",
        },
        output: "Waiting for ticket.approved",
      },
      created_at: "2026-06-13T00:01:00Z",
    },
    {
      checkpoint_id: "CHK-1",
      workflow_run_id: "WR-2",
      project_id: null,
      sequence: 1,
      node_id: "draft",
      state_blob: {
        kind: "human_approval",
        output: "Draft generated",
      },
      created_at: "2026-06-13T00:00:30Z",
    },
  ];
}

function approvals(): WorkflowRuntimeApproval[] {
  return [
    {
      runtime_approval_id: "APP-1",
      workflow_run_id: "WR-2",
      project_id: null,
      node_id: "approval",
      status: "pending",
      requested_at: "2026-06-13T00:01:00Z",
      decided_at: null,
      decided_by: null,
      decision_reason: null,
      policy_snapshot: {
        required_role: "ops_admin",
        approval_count: 2,
        timeout_behavior: "reject",
      },
    },
  ];
}

function events(): WorkflowRunEvent[] {
  return [
    {
      event_id: 1,
      workflow_run_id: "WR-2",
      project_id: null,
      sequence: 1,
      event_type: "workflow.run.started",
      node_id: null,
      payload: null,
      created_at: "2026-06-13T00:00:10Z",
    },
    {
      event_id: 2,
      workflow_run_id: "WR-2",
      project_id: null,
      sequence: 2,
      event_type: "workflow.run.waiting_approval",
      node_id: "approval",
      payload: {
        reason: "Need operator confirmation before launch.",
      },
      created_at: "2026-06-13T00:01:00Z",
    },
    {
      event_id: 3,
      workflow_run_id: "WR-2",
      project_id: null,
      sequence: 3,
      event_type: "workflow.run.waiting_event",
      node_id: "approval",
      payload: {
        reason: "Waiting for ticket.approved",
      },
      created_at: "2026-06-13T00:01:30Z",
    },
  ];
}

function runtimeApprovalCheckpoint(): WorkflowRunCheckpoint {
  return {
    checkpoint_id: "CHK-3",
    workflow_run_id: "WR-2",
    project_id: null,
    sequence: 3,
    node_id: "tool_gate",
    state_blob: {
      kind: "runtime_approval",
      input_by_port: {
        input: "delete ticket T-300",
      },
      output: "delete ticket T-300",
    },
    created_at: "2026-06-13T00:01:45Z",
  };
}

describe("workflow run panels", () => {
  it("lets operators inspect and retry persisted checkpoints", async () => {
    const onRetryFromCheckpoint = vi.fn();
    const user = userEvent.setup();

    render(
      <WorkflowRunCheckpointPanel
        run={baseRun()}
        checkpoints={checkpoints()}
        canRetryFromCheckpoint
        onRetryFromCheckpoint={onRetryFromCheckpoint}
      />,
    );

    const detail = screen.getByTestId("workflow-run-checkpoint-detail");
    expect(detail).toHaveTextContent("Wait for event");
    expect(detail).toHaveTextContent("ticket.approved");
    expect(detail).toHaveTextContent("Correlation key");
    expect(detail).toHaveTextContent("ticket_id");
    expect(detail).toHaveTextContent("Correlation value");
    expect(detail).toHaveTextContent("T-42");
    expect(detail).toHaveTextContent("Wait timeout");
    expect(detail).toHaveTextContent("600s");
    expect(detail).toHaveTextContent("Input ports");
    expect(detail).toHaveTextContent("Output ports");
    expect(detail).toHaveTextContent("Waiting for ticket.approved");

    await user.click(screen.getByTestId("workflow-run-checkpoint-retry"));
    expect(onRetryFromCheckpoint).toHaveBeenCalledWith("CHK-2");

    await user.click(screen.getByTestId("workflow-run-checkpoint-item-1"));
    expect(detail).toHaveTextContent("Human approval");
    expect(detail).toHaveTextContent("Draft generated");
  });

  it("labels runtime approval checkpoints distinctly from human approval nodes", () => {
    render(
      <WorkflowRunCheckpointPanel
        run={baseRun({
          status: "waiting_approval",
          current_node_id: "tool_gate",
          summary: {
            resume_checkpoint_id: "CHK-3",
          },
        })}
        checkpoints={[runtimeApprovalCheckpoint(), ...checkpoints()]}
      />,
    );

    const detail = screen.getByTestId("workflow-run-checkpoint-detail");
    expect(detail).toHaveTextContent("Runtime approval");
    expect(detail).toHaveTextContent("tool_gate");
    expect(detail).toHaveTextContent("Input ports");
    expect(detail).toHaveTextContent("1 port: input");
    expect(detail).toHaveTextContent("delete ticket T-300");
  });

  it("warns when the active checkpoint node identity disagrees with the current run gate", () => {
    render(
      <WorkflowRunCheckpointPanel
        run={baseRun({
          status: "waiting_event",
          current_node_id: "wait_gate",
          summary: {
            resume_checkpoint_id: "CHK-BAD",
          },
        })}
        checkpoints={[
          {
            checkpoint_id: "CHK-BAD",
            workflow_run_id: "WR-2",
            project_id: null,
            sequence: 4,
            node_id: "tool_gate",
            state_blob: {
              kind: "wait_for_event",
              node_id: "other_gate",
              expected_event_name: "ticket.approved",
              input_by_port: {
                input: "resume me",
              },
              output: "waiting on stale gate",
            },
            created_at: "2026-06-13T00:02:00Z",
          },
          ...checkpoints(),
        ]}
      />,
    );

    const detail = screen.getByTestId("workflow-run-checkpoint-detail");
    expect(detail).toHaveTextContent("CHK-BAD");
    const note = screen.getByTestId("workflow-run-checkpoint-integrity-note");
    expect(note).toHaveTextContent("Checkpoint CHK-BAD has inconsistent node identity");
    expect(note).toHaveTextContent("active run is waiting on node wait_gate");
    expect(note).toHaveTextContent("checkpoint row points at tool_gate");
    expect(note).toHaveTextContent("checkpoint payload points at other_gate");
    expect(note).toHaveTextContent("recovery, lineage, and debugger panels");
  });

  it("surfaces inherited source checkpoints for checkpoint-based retries", () => {
    render(
      <WorkflowRunCheckpointPanel
        run={baseRun({
          workflow_run_id: "WR-RETRY",
          status: "queued",
          current_node_id: null,
          summary: {
            retry_of: "WR-2",
            retry_mode: "checkpoint",
            resume_checkpoint_id: "CHK-1",
            resume_checkpoint_run_id: "WR-2",
          },
        })}
        checkpoints={[]}
        resumeSourceCheckpoint={checkpoints()[1]}
        canRetryFromCheckpoint
      />,
    );

    const panel = screen.getByTestId("workflow-run-checkpoint-panel");
    expect(panel).toHaveTextContent("resumes from CHK-1 captured on WR-2");
    expect(screen.getByTestId("workflow-run-checkpoint-item-source")).toHaveTextContent(
      "Inherited",
    );
    expect(screen.getByTestId("workflow-run-checkpoint-detail")).toHaveTextContent(
      "Checkpoint run",
    );
    expect(screen.getByTestId("workflow-run-checkpoint-detail")).toHaveTextContent("WR-2");
    expect(screen.getByTestId("workflow-run-checkpoint-source-note")).toHaveTextContent(
      "belongs to WR-2",
    );
    expect(screen.queryByTestId("workflow-run-checkpoint-retry")).not.toBeInTheDocument();
  });

  it("turns missing inherited checkpoint details into recovery guidance", () => {
    render(
      <WorkflowRunCheckpointPanel
        run={baseRun({
          workflow_run_id: "WR-RETRY",
          status: "queued",
          current_node_id: null,
          summary: {
            retry_of: "WR-FAILED",
            retry_mode: "checkpoint",
            resume_checkpoint_id: "CHK-FAILED",
            resume_checkpoint_run_id: "WR-FAILED",
          },
        })}
        checkpoints={[]}
      />,
    );

    const panel = screen.getByTestId("workflow-run-checkpoint-panel");
    expect(panel).toHaveTextContent(
      "checkpoint retry is queued to resume from inherited checkpoint CHK-FAILED on WR-FAILED",
    );
    expect(panel).toHaveTextContent(
      "Inspect the lineage, recovery, and debugger panels to follow the originating run while the current attempt is still in flight",
    );
    expect(panel).toHaveTextContent("checkpoint trail catches up");
  });

  it("turns missing inherited checkpoint details into active-gate guidance for paused runs", () => {
    render(
      <WorkflowRunCheckpointPanel
        run={baseRun({
          workflow_run_id: "WR-RETRY-WAIT",
          status: "waiting_event",
          current_node_id: "wait_gate",
          summary: {
            retry_of: "WR-FAILED",
            retry_mode: "checkpoint",
            resume_checkpoint_id: "CHK-WAIT",
            resume_checkpoint_run_id: "WR-FAILED",
          },
        })}
        checkpoints={[]}
      />,
    );

    const panel = screen.getByTestId("workflow-run-checkpoint-panel");
    expect(panel).toHaveTextContent(
      "checkpoint retry is queued to resume from inherited checkpoint CHK-WAIT on WR-FAILED",
    );
    expect(panel).toHaveTextContent(
      "Inspect the lineage and recovery panels to follow the originating run and current resume gate",
    );
    expect(panel).toHaveTextContent("checkpoint trail can be restored");
  });

  it("turns missing inherited checkpoint details into completed-run reconstruction guidance", () => {
    render(
      <WorkflowRunCheckpointPanel
        run={baseRun({
          workflow_run_id: "WR-RETRY-DONE",
          status: "completed",
          completed_at: "2026-06-13T00:03:00Z",
          summary: {
            retry_of: "WR-FAILED",
            retry_mode: "checkpoint",
            resume_checkpoint_id: "CHK-DONE",
            resume_checkpoint_run_id: "WR-FAILED",
          },
        })}
        checkpoints={[]}
      />,
    );

    const panel = screen.getByTestId("workflow-run-checkpoint-panel");
    expect(panel).toHaveTextContent(
      "Inspect the lineage, debugger, final outputs, and generated artifacts",
    );
    expect(panel).toHaveTextContent(
      "without restoring the original checkpoint details",
    );
  });

  it("turns an empty checkpoint trail into recovery guidance for paused runs", () => {
    render(
      <WorkflowRunCheckpointPanel
        run={baseRun({
          status: "waiting_approval",
          parent_run_id: "WR-1",
          summary: {
            retry_of: "WR-1",
          },
        })}
        checkpoints={[]}
      />,
    );

    const panel = screen.getByTestId("workflow-run-checkpoint-empty");
    expect(panel).toHaveTextContent("No persisted checkpoints exist for this run yet.");
    expect(panel).toHaveTextContent(
      "Inspect the recovery panel to confirm the active gate",
    );
    expect(panel).toHaveTextContent(
      "open lineage to trace earlier attempts",
    );
  });

  it("turns an empty checkpoint trail into in-flight guidance for active runs", () => {
    render(
      <WorkflowRunCheckpointPanel
        run={baseRun({
          status: "running",
          parent_run_id: null,
          summary: {},
        })}
        checkpoints={[]}
      />,
    );

    const panel = screen.getByTestId("workflow-run-checkpoint-empty");
    expect(panel).toHaveTextContent("No persisted checkpoints exist for this run yet.");
    expect(panel).toHaveTextContent(
      "This execution may still be in flight before it reaches a resumable boundary.",
    );
    expect(panel).toHaveTextContent(
      "Inspect the recovery and debugger panels",
    );
  });

  it("turns an empty checkpoint trail into completed-run guidance when no resumable boundary was reached", () => {
    render(
      <WorkflowRunCheckpointPanel
        run={baseRun({
          status: "completed",
          completed_at: "2026-06-13T00:02:00Z",
          parent_run_id: null,
          summary: {},
        })}
        checkpoints={[]}
      />,
    );

    const panel = screen.getByTestId("workflow-run-checkpoint-empty");
    expect(panel).toHaveTextContent("No persisted checkpoints exist for this run yet.");
    expect(panel).toHaveTextContent(
      "This run completed without ever pausing at a resumable boundary.",
    );
    expect(panel).toHaveTextContent(
      "Inspect the debugger, final output, and generated artifacts",
    );
  });

  it("summarizes the active recovery path, approvals, and lifecycle events", () => {
    render(
      <WorkflowRunRecoveryPanel
        run={baseRun({ status: "waiting_approval" })}
        approvals={approvals()}
        checkpoints={checkpoints()}
        events={events()}
      />,
    );

    const panel = screen.getByTestId("workflow-run-recovery-panel");
    expect(panel).toHaveTextContent("Awaiting approval");
    expect(panel).toHaveTextContent("Approval APP-1 is pending on approval.");
    expect(panel).toHaveTextContent("Pending approval since");
    expect(panel).toHaveTextContent("Required approvals");
    expect(panel).toHaveTextContent("2");
    expect(panel).toHaveTextContent("ops_admin");
    expect(panel).toHaveTextContent("Awaiting approval");
    expect(panel).toHaveTextContent("Waiting for event");
    expect(panel).toHaveTextContent("Correlation key");
    expect(panel).toHaveTextContent("ticket_id");
    expect(panel).toHaveTextContent("Correlation value");
    expect(panel).toHaveTextContent("T-42");
    expect(panel).toHaveTextContent("Wait timeout");
    expect(panel).toHaveTextContent("600s");
    expect(panel).toHaveTextContent("Pending approval since");

    const approvalCard = screen.getByTestId("workflow-run-recovery-approval-APP-1");
    expect(approvalCard).toHaveTextContent("requires 2");
    expect(approvalCard).toHaveTextContent("timeout reject");
  });

  it("surfaces durable artifact persistence details in recovery diagnostics", () => {
    render(
      <WorkflowRunRecoveryPanel
        run={baseRun({
          status: "completed",
          current_node_id: "output",
          completed_at: "2026-06-13T00:02:00Z",
          summary: {
            artifact_persistence: {
              status: "persisted",
              bucket: "caliber-suite",
              object_count: 3,
              artifact_names: ["kg.json", "report.html"],
            },
          },
        })}
        approvals={[]}
        checkpoints={[]}
        events={[]}
      />,
    );

    const panel = screen.getByTestId("workflow-run-recovery-panel");
    expect(panel).toHaveTextContent("Artifact upload");
    expect(panel).toHaveTextContent("Persisted");

    const persistence = screen.getByTestId("workflow-run-recovery-artifact-persistence");
    expect(persistence).toHaveTextContent("Run artifacts reached object storage");
    expect(persistence).toHaveTextContent("Bucket");
    expect(persistence).toHaveTextContent("caliber-suite");
    expect(persistence).toHaveTextContent("Stored objects");
    expect(persistence).toHaveTextContent("3");
    expect(persistence).toHaveTextContent("Named artifacts: kg.json, report.html");
  });

  it("elevates artifact upload failures into recovery warnings and detail cards", () => {
    render(
      <WorkflowRunRecoveryPanel
        run={baseRun({
          status: "completed",
          current_node_id: "output",
          completed_at: "2026-06-13T00:02:00Z",
          summary: {
            artifact_persistence: {
              status: "failed",
              bucket: "caliber-suite",
              object_count: 3,
              persisted_object_count: 1,
              artifact_names: ["kg.json", "report.html"],
              recent_persisted_keys: ["pipeline/WR-1/kg.json"],
              failed_object_key: "pipeline/WR-1/report.html",
              error:
                "RuntimeError: object store offline while uploading pipeline/WR-1/report.html after storing 1 of 3 object(s)",
            },
          },
        })}
        approvals={[]}
        checkpoints={[]}
        events={[]}
      />,
    );

    expect(screen.getByTestId("workflow-run-recovery-warning")).toHaveTextContent(
      "Object-store upload to caliber-suite failed after execution completed after 1 of 3 objects were stored: RuntimeError: object store offline while uploading pipeline/WR-1/report.html after storing 1 of 3 object(s)",
    );
    expect(screen.getByTestId("workflow-run-recovery-artifact-persistence")).toHaveTextContent(
      "Object-store upload failed after completion",
    );
    expect(screen.getByTestId("workflow-run-recovery-artifact-persistence")).toHaveTextContent(
      "Upload error",
    );
    expect(screen.getByTestId("workflow-run-recovery-artifact-persistence")).toHaveTextContent(
      "Stored before failure",
    );
    expect(screen.getByTestId("workflow-run-recovery-artifact-persistence")).toHaveTextContent(
      "Planned objects",
    );
    expect(screen.getByTestId("workflow-run-recovery-artifact-persistence")).toHaveTextContent(
      "Failed object",
    );
    expect(screen.getByTestId("workflow-run-recovery-artifact-persistence")).toHaveTextContent(
      "pipeline/WR-1/report.html",
    );
  });

  it("blocks manual approval recovery when no approved decision is attached", () => {
    render(
      <WorkflowRunRecoveryPanel
        run={baseRun({
          status: "waiting_approval",
          current_node_id: "draft",
          summary: {
            resume_checkpoint_id: "CHK-1",
          },
        })}
        approvals={[]}
        checkpoints={checkpoints()}
        events={events()}
      />,
    );

    const panel = screen.getByTestId("workflow-run-recovery-panel");
    expect(panel).toHaveTextContent("Approval gate blocked");
    expect(panel).toHaveTextContent(
      "still needs an approved decision before it can resume",
    );
    expect(screen.getByTestId("workflow-run-recovery-approvals")).toHaveTextContent(
      "Use the active approval checkpoint and recovery warning above",
    );
    expect(screen.getByTestId("workflow-run-recovery-warning")).toHaveTextContent(
      "no approved approval record is attached",
    );
  });

  it("describes runtime approval gates without calling them human approval nodes", () => {
    render(
      <WorkflowRunRecoveryPanel
        run={baseRun({
          status: "waiting_approval",
          current_node_id: "tool_gate",
          summary: {
            resume_checkpoint_id: "CHK-3",
          },
        })}
        approvals={[]}
        checkpoints={[runtimeApprovalCheckpoint(), ...checkpoints()]}
        events={events()}
      />,
    );

    const panel = screen.getByTestId("workflow-run-recovery-panel");
    expect(panel).toHaveTextContent("Runtime approval blocked");
    expect(panel).toHaveTextContent(
      "paused behind a runtime approval gate",
    );
    expect(panel).not.toHaveTextContent("human approval node");
    expect(screen.getByTestId("workflow-run-recovery-approvals")).toHaveTextContent(
      "Use the active runtime approval checkpoint and recovery warning above",
    );
    expect(screen.getByTestId("workflow-run-recovery-warning")).toHaveTextContent(
      "no approved runtime approval record is attached",
    );
  });

  it("separates historical approval rows from the active blocked gate", () => {
    render(
      <WorkflowRunRecoveryPanel
        run={baseRun({
          status: "waiting_approval",
          current_node_id: "tool_gate",
          summary: {
            resume_checkpoint_id: "CHK-3",
          },
        })}
        approvals={[
          {
            runtime_approval_id: "APP-HIST",
            workflow_run_id: "WR-2",
            project_id: null,
            node_id: "draft",
            status: "approved",
            requested_at: "2026-06-13T00:00:40Z",
            decided_at: "2026-06-13T00:00:45Z",
            decided_by: "ops@example.com",
            decision_reason: "draft approved",
            policy_snapshot: {
              required_role: "ops_admin",
              approval_count: 1,
              timeout_behavior: "reject",
            },
          },
        ]}
        checkpoints={[runtimeApprovalCheckpoint(), ...checkpoints()]}
        events={events()}
      />,
    );

    const approvalsPanel = screen.getByTestId("workflow-run-recovery-approvals");
    expect(approvalsPanel).toHaveTextContent(
      "No runtime approval records are attached to the active gate on this run.",
    );
    expect(approvalsPanel).toHaveTextContent(
      "Earlier approval rows exist on other nodes",
    );
    expect(approvalsPanel).not.toHaveTextContent("APP-HIST is approved");

    const historical = screen.getByTestId("workflow-run-recovery-historical-approvals");
    expect(historical).toHaveTextContent(
      "do not unblock the current gate on tool_gate",
    );
    expect(
      screen.getByTestId("workflow-run-recovery-historical-approval-APP-HIST"),
    ).toHaveTextContent("node draft");
  });

  it("includes runtime approval decision events in the recovery timeline", () => {
    render(
      <WorkflowRunRecoveryPanel
        run={baseRun({
          status: "waiting_approval",
          current_node_id: "tool_gate",
          summary: {
            resume_checkpoint_id: "CHK-3",
          },
        })}
        approvals={[]}
        checkpoints={[runtimeApprovalCheckpoint()]}
        events={[
          {
            event_id: 31,
            workflow_run_id: "WR-2",
            project_id: null,
            sequence: 1,
            event_type: "workflow.run.approval.approved",
            node_id: "tool_gate",
            payload: {
              runtime_approval_id: "APP-DECISION",
              reason: "policy reviewed",
            },
            created_at: "2026-06-13T00:01:45Z",
          },
          {
            event_id: 32,
            workflow_run_id: "WR-2",
            project_id: null,
            sequence: 2,
            event_type: "workflow.run.approval.rejected",
            node_id: "tool_gate",
            payload: {
              runtime_approval_id: "APP-REJECT",
              reason: "unsafe tool scope",
            },
            created_at: "2026-06-13T00:01:50Z",
          },
        ]}
      />,
    );

    const timeline = screen.getByTestId("workflow-run-recovery-events");
    expect(timeline).toHaveTextContent(
      "Runtime approval recorded · APP-DECISION · policy reviewed",
    );
    expect(timeline).toHaveTextContent(
      "Runtime approval rejected · APP-REJECT · unsafe tool scope",
    );
  });

  it("surfaces active checkpoint node drift in the recovery warning", () => {
    render(
      <WorkflowRunRecoveryPanel
        run={baseRun({
          status: "waiting_event",
          current_node_id: "wait_gate",
          summary: {
            resume_checkpoint_id: "CHK-DRIFT",
          },
        })}
        approvals={[]}
        checkpoints={[
          {
            checkpoint_id: "CHK-DRIFT",
            workflow_run_id: "WR-2",
            project_id: null,
            sequence: 5,
            node_id: "tool_gate",
            state_blob: {
              kind: "wait_for_event",
              node_id: "other_gate",
              expected_event_name: "ticket.approved",
              input_by_port: {
                input: "resume me",
              },
            },
            created_at: "2026-06-13T00:02:10Z",
          },
        ]}
        events={[]}
      />,
    );

    const warning = screen.getByTestId("workflow-run-recovery-warning");
    expect(warning).toHaveTextContent("Checkpoint CHK-DRIFT has inconsistent node identity");
    expect(warning).toHaveTextContent("checkpoint row points at tool_gate instead of active node wait_gate");
    expect(warning).toHaveTextContent("checkpoint payload points at other_gate instead of active node wait_gate");
    expect(warning).toHaveTextContent("lineage, debugger state, and run events");
  });

  it("turns empty approval history into completed-run guidance when approval rows never persisted", () => {
    render(
      <WorkflowRunRecoveryPanel
        run={baseRun({
          status: "completed",
          current_node_id: "tool_gate",
          completed_at: "2026-06-13T00:02:00Z",
          summary: {
            resume_checkpoint_id: "CHK-3",
          },
        })}
        approvals={[]}
        checkpoints={[runtimeApprovalCheckpoint()]}
        events={[]}
      />,
    );

    const approvalsPanel = screen.getByTestId("workflow-run-recovery-approvals");
    expect(approvalsPanel).toHaveTextContent(
      "No runtime approval records are attached to the active gate on this run.",
    );
    expect(approvalsPanel).toHaveTextContent(
      "Inspect the active checkpoint details, final outputs, and debugger state above",
    );
    expect(approvalsPanel).toHaveTextContent(
      "resumed past approval before approval history was persisted",
    );
  });

  it("turns empty approval history into stopped-run guidance when no approval checkpoint exists", () => {
    render(
      <WorkflowRunRecoveryPanel
        run={baseRun({
          status: "failed",
          current_node_id: "tool_gate",
          summary: {},
        })}
        approvals={[]}
        checkpoints={[]}
        events={[]}
      />,
    );

    const approvalsPanel = screen.getByTestId("workflow-run-recovery-approvals");
    expect(approvalsPanel).toHaveTextContent(
      "No runtime approval records are attached to the active gate on this run.",
    );
    expect(approvalsPanel).toHaveTextContent(
      "Use the current failure state, recovery timeline, and debugger details above",
    );
    expect(approvalsPanel).toHaveTextContent(
      "before it stopped",
    );
  });

  it("includes lease recovery events in the recovery timeline", () => {
    render(
      <WorkflowRunRecoveryPanel
        run={baseRun({
          status: "queued",
          current_node_id: null,
          summary: {
            resume_checkpoint_id: "CHK-3",
          },
        })}
        approvals={[]}
        checkpoints={[runtimeApprovalCheckpoint()]}
        events={[
          {
            event_id: 33,
            workflow_run_id: "WR-2",
            project_id: null,
            sequence: 3,
            event_type: "workflow.run.recovered",
            node_id: null,
            payload: {
              reason: "lease_expired",
              worker_id: "worker-7",
            },
            created_at: "2026-06-13T00:01:55Z",
          },
        ]}
      />,
    );

    const timeline = screen.getByTestId("workflow-run-recovery-events");
    expect(timeline).toHaveTextContent("Run recovered · worker lease expired");
  });

  it("turns an empty recovery timeline into guidance that uses existing recovery evidence", () => {
    render(
      <WorkflowRunRecoveryPanel
        run={baseRun({ status: "waiting_approval" })}
        approvals={approvals()}
        checkpoints={checkpoints()}
        events={[]}
      />,
    );

    const timeline = screen.getByTestId("workflow-run-recovery-events");
    expect(timeline).toHaveTextContent(
      "No recovery-specific lifecycle events have been recorded yet.",
    );
    expect(timeline).toHaveTextContent(
      "Use the approval card and stored checkpoint details above to trace the active resume gate",
    );
    expect(timeline).toHaveTextContent("lifecycle history catches up");
  });

  it("turns an empty recovery timeline into in-flight guidance for active runs", () => {
    render(
      <WorkflowRunRecoveryPanel
        run={baseRun({
          status: "running",
          current_node_id: "tool_gate",
          summary: {
            resume_checkpoint_id: "CHK-3",
          },
        })}
        approvals={[]}
        checkpoints={[runtimeApprovalCheckpoint()]}
        events={[]}
      />,
    );

    const timeline = screen.getByTestId("workflow-run-recovery-events");
    expect(timeline).toHaveTextContent(
      "No recovery-specific lifecycle events have been recorded yet.",
    );
    expect(timeline).toHaveTextContent(
      "This run may still be executing or recovery persistence may still be catching up",
    );
    expect(timeline).toHaveTextContent(
      "stored checkpoint details above while execution continues",
    );
  });

  it("turns an empty recovery timeline into completed-run guidance when stored recovery history is missing", () => {
    render(
      <WorkflowRunRecoveryPanel
        run={baseRun({
          status: "completed",
          current_node_id: "tool_gate",
          completed_at: "2026-06-13T00:02:00Z",
          summary: {
            resume_checkpoint_id: "CHK-3",
          },
        })}
        approvals={[]}
        checkpoints={[runtimeApprovalCheckpoint()]}
        events={[]}
      />,
    );

    const timeline = screen.getByTestId("workflow-run-recovery-events");
    expect(timeline).toHaveTextContent(
      "No recovery-specific lifecycle events have been recorded yet.",
    );
    expect(timeline).toHaveTextContent(
      "This execution completed without stored recovery history",
    );
    expect(timeline).toHaveTextContent(
      "stored checkpoint details, debugger state, and final outputs above",
    );
  });

  it("renders retry lineage and allows jumping to a related run", async () => {
    const onSelectRun = vi.fn();
    const user = userEvent.setup();
    const currentRun = baseRun({
      status: "failed",
      workflow_run_id: "WR-2",
      parent_run_id: "WR-1",
      attempt_number: 2,
      error_summary: "Tool timeout while calling vendor API.",
    });
    const rootRun = baseRun({
      workflow_run_id: "WR-1",
      parent_run_id: null,
      attempt_number: 1,
      status: "completed",
      completed_at: "2026-06-13T00:00:50Z",
      current_node_id: "output",
      summary: {},
    });
    const childRun = baseRun({
      workflow_run_id: "WR-3",
      parent_run_id: "WR-2",
      attempt_number: 3,
      status: "queued",
      queued_at: "2026-06-13T00:02:00Z",
      current_node_id: "start",
      summary: {},
    });

    render(
      <WorkflowRunLineagePanel
        run={currentRun}
        runs={[rootRun, childRun]}
        onSelectRun={onSelectRun}
      />,
    );

    const panel = screen.getByTestId("workflow-run-lineage-panel");
    expect(panel).toHaveTextContent("Attempt 2 of 3");
    expect(panel).toHaveTextContent("Child retries");
    expect(panel).toHaveTextContent("1");

    const currentItem = screen.getByTestId("workflow-run-lineage-item-WR-2");
    expect(within(currentItem).getByText("current")).toBeInTheDocument();
    expect(within(currentItem).queryByText("parent")).not.toBeInTheDocument();
    expect(within(currentItem).getByText("Failed")).toBeInTheDocument();

    const rootItem = screen.getByTestId("workflow-run-lineage-item-WR-1");
    expect(within(rootItem).getByText("root")).toBeInTheDocument();
    expect(within(rootItem).getByText("Completed")).toBeInTheDocument();

    const childItem = screen.getByTestId("workflow-run-lineage-item-WR-3");
    expect(within(childItem).getByText("child")).toBeInTheDocument();
    expect(within(childItem).getByText("Queued")).toBeInTheDocument();

    await user.click(rootItem);
    expect(onSelectRun).toHaveBeenCalledWith(expect.objectContaining({ workflow_run_id: "WR-1" }));
  });

  it("turns an empty retry lineage into recovery guidance for paused runs", () => {
    render(
      <WorkflowRunLineagePanel
        run={baseRun({
          workflow_run_id: "WR-SOLO",
          parent_run_id: null,
          attempt_number: 1,
          status: "waiting_event",
          summary: {},
        })}
        runs={[]}
      />,
    );

    const panel = screen.getByTestId("workflow-run-lineage-empty");
    expect(panel).toHaveTextContent("No retries have been recorded for this run yet.");
    expect(panel).toHaveTextContent(
      "Use the recovery and checkpoint panels to inspect the active gate",
    );
    expect(panel).toHaveTextContent("retry chain remains empty");
  });

  it("turns an empty retry lineage into in-flight guidance for active runs", () => {
    render(
      <WorkflowRunLineagePanel
        run={baseRun({
          workflow_run_id: "WR-ACTIVE",
          parent_run_id: null,
          attempt_number: 1,
          status: "running",
          summary: {},
        })}
        runs={[]}
      />,
    );

    const panel = screen.getByTestId("workflow-run-lineage-empty");
    expect(panel).toHaveTextContent("No retries have been recorded for this run yet.");
    expect(panel).toHaveTextContent(
      "This attempt is still in flight, so no retry lineage exists yet.",
    );
    expect(panel).toHaveTextContent(
      "inspect the current execution until it either completes or spawns another attempt",
    );
  });

  it("turns an empty retry lineage into completed-run guidance when no retry chain was created", () => {
    render(
      <WorkflowRunLineagePanel
        run={baseRun({
          workflow_run_id: "WR-DONE",
          parent_run_id: null,
          attempt_number: 1,
          status: "completed",
          completed_at: "2026-06-13T00:02:00Z",
          summary: {},
        })}
        runs={[]}
      />,
    );

    const panel = screen.getByTestId("workflow-run-lineage-empty");
    expect(panel).toHaveTextContent("No retries have been recorded for this run yet.");
    expect(panel).toHaveTextContent(
      "This run completed on its first attempt, so no retry chain was created.",
    );
    expect(panel).toHaveTextContent(
      "Use the debugger, outputs, and generated artifacts to inspect the terminal result.",
    );
  });

  it("surfaces checkpoint retry context inside retry lineage summaries", () => {
    const currentRun = baseRun({
      workflow_run_id: "WR-3",
      status: "queued",
      parent_run_id: "WR-2",
      attempt_number: 3,
      current_node_id: null,
      summary: {
        retry_of: "WR-2",
        retry_mode: "checkpoint",
        resume_checkpoint_id: "CHK-1",
        resume_checkpoint_run_id: "WR-2",
      },
    });
    const rootRun = baseRun({
      workflow_run_id: "WR-1",
      parent_run_id: null,
      attempt_number: 1,
      status: "completed",
      summary: {},
    });
    const parentRun = baseRun({
      workflow_run_id: "WR-2",
      parent_run_id: "WR-1",
      attempt_number: 2,
      status: "failed",
      summary: {},
    });

    render(
      <WorkflowRunLineagePanel
        run={currentRun}
        runs={[rootRun, parentRun]}
      />,
    );

    const panel = screen.getByTestId("workflow-run-lineage-panel");
    expect(panel).toHaveTextContent("Checkpoint retry");
    expect(panel).toHaveTextContent("Resumed from checkpoint CHK-1 on WR-2.");
    expect(screen.getByTestId("workflow-run-lineage-item-WR-3")).toHaveTextContent(
      "checkpoint retry via CHK-1 from WR-2",
    );
  });

  it("prefers server-backed lineage metadata and surfaces partial-chain warnings", () => {
    const currentRun = baseRun({
      status: "failed",
      workflow_run_id: "WR-2",
      parent_run_id: "WR-1",
      attempt_number: 2,
    });
    const rootRun = baseRun({
      workflow_run_id: "WR-1",
      parent_run_id: null,
      attempt_number: 1,
      status: "completed",
      summary: {},
    });
    const childRun = baseRun({
      workflow_run_id: "WR-3",
      parent_run_id: "WR-2",
      attempt_number: 3,
      status: "queued",
      summary: {},
    });
    const lineage: WorkflowRunLineage = {
      workflow_run_id: "WR-2",
      root_run_id: "WR-1",
      total_attempts: 4,
      parent_count: 1,
      child_count: 1,
      missing_parent_id: "WR-0",
      truncated: true,
      runs: [rootRun, currentRun, childRun],
    };

    render(
      <WorkflowRunLineagePanel
        run={currentRun}
        lineage={lineage}
        runs={[
          baseRun({
            workflow_run_id: "WR-9",
            parent_run_id: "WR-8",
            attempt_number: 9,
            summary: {},
          }),
        ]}
      />,
    );

    const panel = screen.getByTestId("workflow-run-lineage-panel");
    expect(panel).toHaveTextContent("Attempt 2 of 4");
    expect(panel).toHaveTextContent(
      "Parent run WR-0 is outside the currently loaded run history",
    );
    expect(panel).toHaveTextContent(
      "Open the nearest visible parent/current attempts, debugger state, and recovery diagnostics",
    );
    expect(panel).toHaveTextContent(
      "Use the visible attempts, debugger state, and recovery diagnostics to trace where the retry chain stopped.",
    );
    expect(panel).toHaveTextContent("WR-3");
    expect(panel).not.toHaveTextContent("WR-9");
  });

  it("turns partial lineage warnings into active-run guidance when parent history is missing", () => {
    const currentRun = baseRun({
      workflow_run_id: "WR-ACTIVE",
      parent_run_id: "WR-MISSING",
      attempt_number: 2,
      status: "running",
      summary: {},
    });
    const lineage: WorkflowRunLineage = {
      workflow_run_id: "WR-ACTIVE",
      root_run_id: "WR-1",
      total_attempts: 3,
      parent_count: 1,
      child_count: 0,
      missing_parent_id: "WR-MISSING",
      truncated: true,
      runs: [
        baseRun({
          workflow_run_id: "WR-1",
          parent_run_id: null,
          attempt_number: 1,
          status: "failed",
          summary: {},
        }),
        currentRun,
      ],
    };

    render(<WorkflowRunLineagePanel run={currentRun} lineage={lineage} />);

    const panel = screen.getByTestId("workflow-run-lineage-panel");
    expect(panel).toHaveTextContent(
      "This attempt is still active, so refresh workflow run history",
    );
    expect(panel).toHaveTextContent(
      "recovery state, and checkpoints while newer lineage evidence is still arriving",
    );
    expect(panel).toHaveTextContent(
      "Keep following the visible attempts plus the recovery and debugger panels while execution is still in flight.",
    );
  });

  it("turns partial lineage warnings into completed-run reconstruction guidance", () => {
    const currentRun = baseRun({
      workflow_run_id: "WR-DONE",
      parent_run_id: "WR-0",
      attempt_number: 4,
      status: "completed",
      completed_at: "2026-06-13T00:04:00Z",
      summary: {},
    });
    const lineage: WorkflowRunLineage = {
      workflow_run_id: "WR-DONE",
      root_run_id: "WR-1",
      total_attempts: 6,
      parent_count: 3,
      child_count: 0,
      missing_parent_id: "WR-0",
      truncated: true,
      runs: [
        baseRun({
          workflow_run_id: "WR-3",
          parent_run_id: "WR-2",
          attempt_number: 3,
          status: "failed",
          summary: {},
        }),
        currentRun,
      ],
    };

    render(<WorkflowRunLineagePanel run={currentRun} lineage={lineage} />);

    const panel = screen.getByTestId("workflow-run-lineage-panel");
    expect(panel).toHaveTextContent(
      "Open the nearest visible parent/current attempts, debugger state, outputs, and generated artifacts to reconstruct how this retry chain resolved.",
    );
    expect(panel).toHaveTextContent(
      "Use the visible attempts, outputs, and generated artifacts to inspect how the chain converged on its completed result.",
    );
  });
});
