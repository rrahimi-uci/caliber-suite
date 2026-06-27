export type WorkflowRunApprovalCheckpointKind =
  | "human_approval"
  | "runtime_approval";

export type WorkflowRunLifecycleLabelMode = "summary" | "marker";

const WORKFLOW_RUN_STATUS_LABELS: Record<string, string> = {
  blocked: "Blocked",
  cancel_requested: "Cancel requested",
  cancelled: "Cancelled",
  completed: "Completed",
  expired: "Expired",
  failed: "Failed",
  queued: "Queued",
  rejected: "Rejected",
  resuming: "Resuming",
  running: "Running",
  waiting_approval: "Awaiting approval",
  waiting_event: "Waiting for event",
};

const WORKFLOW_RUN_STATUS_BORDER_CLASSES: Record<string, string> = {
  blocked:
    "bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950/40 dark:text-amber-300 dark:border-amber-800/70",
  cancel_requested:
    "bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950/40 dark:text-amber-300 dark:border-amber-800/70",
  cancelled:
    "bg-slate-100 text-slate-600 border-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:border-slate-700/70",
  completed:
    "bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950/40 dark:text-emerald-300 dark:border-emerald-800/70",
  expired:
    "bg-slate-100 text-slate-600 border-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:border-slate-700/70",
  failed:
    "bg-red-50 text-red-700 border-red-200 dark:bg-red-950/40 dark:text-red-300 dark:border-red-800/70",
  queued:
    "bg-violet-50 text-violet-700 border-violet-200 dark:bg-violet-950/40 dark:text-violet-300 dark:border-violet-800/70",
  rejected:
    "bg-red-50 text-red-700 border-red-200 dark:bg-red-950/40 dark:text-red-300 dark:border-red-800/70",
  resuming:
    "bg-sky-50 text-sky-700 border-sky-200 dark:bg-sky-950/40 dark:text-sky-300 dark:border-sky-800/70",
  running:
    "bg-sky-50 text-sky-700 border-sky-200 dark:bg-sky-950/40 dark:text-sky-300 dark:border-sky-800/70",
  waiting_approval:
    "bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950/40 dark:text-amber-300 dark:border-amber-800/70",
  waiting_event:
    "bg-sky-50 text-sky-700 border-sky-200 dark:bg-sky-950/40 dark:text-sky-300 dark:border-sky-800/70",
};

const WORKFLOW_RUN_STATUS_RING_CLASSES: Record<string, string> = {
  blocked:
    "bg-amber-50 text-amber-700 ring-amber-200/80 dark:bg-amber-950/40 dark:text-amber-300 dark:ring-amber-800/70",
  cancel_requested:
    "bg-amber-50 text-amber-700 ring-amber-200/80 dark:bg-amber-950/40 dark:text-amber-300 dark:ring-amber-800/70",
  cancelled:
    "bg-slate-100 text-slate-600 ring-slate-200/80 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700/70",
  completed:
    "bg-emerald-50 text-emerald-700 ring-emerald-200/80 dark:bg-emerald-950/40 dark:text-emerald-300 dark:ring-emerald-800/70",
  expired:
    "bg-slate-100 text-slate-600 ring-slate-200/80 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700/70",
  failed:
    "bg-red-50 text-red-700 ring-red-200/80 dark:bg-red-950/40 dark:text-red-300 dark:ring-red-800/70",
  queued:
    "bg-violet-50 text-violet-700 ring-violet-200/80 dark:bg-violet-950/40 dark:text-violet-300 dark:ring-violet-800/70",
  rejected:
    "bg-red-50 text-red-700 ring-red-200/80 dark:bg-red-950/40 dark:text-red-300 dark:ring-red-800/70",
  resuming:
    "bg-sky-50 text-sky-700 ring-sky-200/80 dark:bg-sky-950/40 dark:text-sky-300 dark:ring-sky-800/70",
  running:
    "bg-sky-50 text-sky-700 ring-sky-200/80 dark:bg-sky-950/40 dark:text-sky-300 dark:ring-sky-800/70",
  waiting_approval:
    "bg-amber-50 text-amber-700 ring-amber-200/80 dark:bg-amber-950/40 dark:text-amber-300 dark:ring-amber-800/70",
  waiting_event:
    "bg-sky-50 text-sky-700 ring-sky-200/80 dark:bg-sky-950/40 dark:text-sky-300 dark:ring-sky-800/70",
};

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function titleCaseWords(value: string): string {
  return value
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function lifecycleTypeLabel(value: string): string {
  return value
    .replace(/^workflow\.run\./, "")
    .split(".")
    .flatMap((part) => part.split("_"))
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function readPayloadString(
  payload: unknown,
  key: string,
): string | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return null;
  }
  return readString((payload as Record<string, unknown>)[key]);
}

function humanizeWorkflowRunReason(value: string | null): string | null {
  switch (value) {
    case "lease_expired":
      return "worker lease expired";
    default:
      return value;
  }
}

function lifecycleReasonOrError(payload: unknown): string | null {
  return humanizeWorkflowRunReason(
    readPayloadString(payload, "reason") ?? readPayloadString(payload, "error"),
  );
}

function checkpointKindValue(value: unknown): string | null {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return readString((value as Record<string, unknown>).kind);
  }
  return readString(value);
}

export function approvalCheckpointKind(
  value: unknown,
): WorkflowRunApprovalCheckpointKind | null {
  const kind = checkpointKindValue(value);
  if (kind === "human_approval" || kind === "runtime_approval") {
    return kind;
  }
  return null;
}

export function workflowRunCheckpointLabel(kind: string | null): string {
  switch (kind) {
    case "human_approval":
      return "Human approval";
    case "runtime_approval":
      return "Runtime approval";
    case "wait_for_event":
      return "Wait for event";
    case "wait_until":
      return "Scheduled wait";
    case "wait_event":
      return "Wait event";
    default:
      if (kind === "checkpoint") return "Checkpoint";
      return kind ?? "Checkpoint";
  }
}

export function workflowRunCheckpointMarkerLabel(kind: string | null): string {
  switch (kind) {
    case "human_approval":
      return "Approval gate";
    case "runtime_approval":
      return "Runtime approval";
    case "wait_for_event":
      return "Event wait";
    case "wait_until":
      return "Scheduled wait";
    case "wait_event":
      return "Resume gate";
    default:
      return workflowRunCheckpointLabel(kind);
  }
}

export function workflowRunApprovalTitle(
  kind: WorkflowRunApprovalCheckpointKind | null,
): string {
  return kind === "runtime_approval" ? "Runtime approval" : "Approval";
}

export function workflowRunApprovalNoun(
  kind: WorkflowRunApprovalCheckpointKind | null,
): string {
  return kind === "runtime_approval" ? "runtime approval" : "approval";
}

export function workflowRunApprovalRecordNoun(
  kind: WorkflowRunApprovalCheckpointKind | null,
): string {
  return kind === "runtime_approval" ? "runtime approval record" : "approval record";
}

export function workflowRunApprovalSubject(
  kind: WorkflowRunApprovalCheckpointKind | null,
): string {
  if (kind === "runtime_approval") return "runtime approval gate";
  if (kind === "human_approval") return "human approval step";
  return "approval step";
}

export function workflowRunAwaitingApprovalLabel(
  kind: WorkflowRunApprovalCheckpointKind | null,
): string {
  return `Awaiting ${workflowRunApprovalNoun(kind)}`;
}

export function workflowRunPendingApprovalChipLabel(
  kind: WorkflowRunApprovalCheckpointKind | null,
): string {
  return `pending ${workflowRunApprovalNoun(kind)}`;
}

export function workflowRunApprovalRecordedLabel(
  kind: WorkflowRunApprovalCheckpointKind | null,
): string {
  return kind === "runtime_approval"
    ? "Runtime approval recorded"
    : "Approval recorded";
}

export function workflowRunApprovalRejectedLabel(
  kind: WorkflowRunApprovalCheckpointKind | null,
): string {
  return kind === "runtime_approval"
    ? "Runtime approval rejected"
    : "Approval rejected";
}

export function workflowRunApprovalBlockedLabel(
  kind: WorkflowRunApprovalCheckpointKind | null,
): string {
  return kind === "runtime_approval"
    ? "Runtime approval blocked"
    : "Approval gate blocked";
}

export function workflowRunApprovalPauseLabel(
  kind: WorkflowRunApprovalCheckpointKind | null,
): string {
  return kind === "runtime_approval"
    ? "Paused for runtime approval"
    : "Paused for approval";
}

export function workflowRunStatusLabel(status: string | null): string {
  if (!status) return "Unknown";
  return WORKFLOW_RUN_STATUS_LABELS[status] ?? titleCaseWords(status);
}

export function workflowRunStatusPhrase(status: string | null): string {
  return workflowRunStatusLabel(status).toLowerCase();
}

export function workflowRunStatusVerbPhrase(status: string | null): string {
  switch (status) {
    case "blocked":
      return "is blocked";
    case "cancel_requested":
      return "has a cancel request pending";
    case "cancelled":
      return "was cancelled";
    case "completed":
      return "completed";
    case "expired":
      return "expired";
    case "failed":
      return "failed";
    case "queued":
      return "is queued";
    case "rejected":
      return "was rejected";
    case "resuming":
      return "is resuming";
    case "running":
      return "is running";
    case "waiting_approval":
      return "is awaiting approval";
    case "waiting_event":
      return "is waiting for event";
    default:
      return `has status ${workflowRunStatusPhrase(status)}`;
  }
}

export function workflowRunStatusMessage(
  runId: string,
  status: string | null,
): string {
  return `Run ${runId} ${workflowRunStatusVerbPhrase(status)}.`;
}

export function workflowRunStatusBorderClass(status: string | null): string {
  return WORKFLOW_RUN_STATUS_BORDER_CLASSES[status ?? ""] ??
    "bg-slate-100 text-slate-600 border-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:border-slate-700/70";
}

export function workflowRunStatusRingClass(status: string | null): string {
  return WORKFLOW_RUN_STATUS_RING_CLASSES[status ?? ""] ??
    "bg-slate-100 text-slate-600 ring-slate-200/80 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700/70";
}

export function workflowRunStatusFromEventType(eventType: string): string | null {
  switch (eventType) {
    case "workflow.run.queued":
      return "queued";
    case "workflow.run.recovered":
      return "queued";
    case "workflow.run.started":
    case "workflow.run.node_started":
      return "running";
    case "workflow.run.waiting_approval":
      return "waiting_approval";
    case "workflow.run.waiting_event":
      return "waiting_event";
    case "workflow.run.resumed":
      return "queued";
    case "workflow.run.cancelled":
      return "cancelled";
    case "workflow.run.completed":
      return "completed";
    case "workflow.run.expired":
      return "expired";
    case "workflow.run.failed":
    case "workflow.run.approval.rejected":
      return "failed";
    default:
      return null;
  }
}

export function workflowRunStatusFromStep(
  step:
    | {
        detail?: string | null;
        node_type?: string | null;
        status?: string | null;
      }
    | null
    | undefined,
): string | null {
  const detail = readString(step?.detail) ?? "";
  if (detail.startsWith("waiting_event:")) return "waiting_event";
  if (detail.startsWith("waiting_approval:")) return "waiting_approval";

  const status = readString(step?.status);
  if (status === "waiting_event" || status === "waiting_approval") {
    return status;
  }
  if (status === "blocked") {
    const nodeType = readString(step?.node_type);
    if (nodeType === "wait_for_event" || nodeType === "wait_until") {
      return "waiting_event";
    }
    if (nodeType === "human_approval") {
      return "waiting_approval";
    }
  }
  return null;
}

export function workflowRunLifecycleLabel(
  eventType: string,
  options: {
    approvalKind?: WorkflowRunApprovalCheckpointKind | null;
    mode?: WorkflowRunLifecycleLabelMode;
  } = {},
): string {
  const mode = options.mode ?? "summary";
  switch (eventType) {
    case "workflow.run.queued":
      return mode === "marker" ? "Queued" : "Run queued";
    case "workflow.run.recovered":
      return mode === "marker" ? "Recovered" : "Run recovered";
    case "workflow.run.started":
      return "Run started";
    case "workflow.run.node_started":
      return mode === "marker" ? "Node started" : "Node started";
    case "workflow.run.approval.approved":
      return workflowRunApprovalRecordedLabel("runtime_approval");
    case "workflow.run.approval.rejected":
      return workflowRunApprovalRejectedLabel("runtime_approval");
    case "workflow.run.waiting_approval":
      return mode === "marker"
        ? workflowRunApprovalPauseLabel(options.approvalKind ?? null)
        : workflowRunAwaitingApprovalLabel(options.approvalKind ?? null);
    case "workflow.run.waiting_event":
      return mode === "marker" ? "Paused for event" : "Waiting for event";
    case "workflow.run.resumed":
      return mode === "marker" ? "Resumed" : "Run resumed";
    case "workflow.run.retried":
      return mode === "marker" ? "Retried" : "Run retried";
    case "workflow.run.cancel_requested":
      return "Cancel requested";
    case "workflow.run.cancelled":
      return "Run cancelled";
    case "workflow.run.completed":
      return "Run completed";
    case "workflow.run.expired":
      return "Run expired";
    case "workflow.run.failed":
      return "Run failed";
    default:
      return lifecycleTypeLabel(eventType);
  }
}

export function workflowRunLifecycleSummary(
  eventType: string,
  payload: unknown,
  options: {
    approvalKind?: WorkflowRunApprovalCheckpointKind | null;
    mode?: WorkflowRunLifecycleLabelMode;
  } = {},
): string {
  const base = workflowRunLifecycleLabel(eventType, options);
  switch (eventType) {
    case "workflow.run.approval.approved": {
      const approvalId = readPayloadString(payload, "runtime_approval_id");
      const reason = lifecycleReasonOrError(payload);
      const detail = [approvalId, reason].filter(Boolean).join(" · ");
      return detail ? `${base} · ${detail}` : base;
    }
    case "workflow.run.approval.rejected": {
      const approvalId = readPayloadString(payload, "runtime_approval_id");
      const reason = lifecycleReasonOrError(payload);
      const detail = [approvalId, reason].filter(Boolean).join(" · ");
      return detail ? `${base} · ${detail}` : base;
    }
    case "workflow.run.recovered": {
      const detail = lifecycleReasonOrError(payload);
      return detail ? `${base} · ${detail}` : base;
    }
    case "workflow.run.resumed": {
      const eventName = readPayloadString(payload, "event_name");
      return eventName ? `${base} · ${eventName}` : base;
    }
    case "workflow.run.node_started": {
      const nodeType = readPayloadString(payload, "node_type");
      const nodeId = readPayloadString(payload, "node_id");
      const detail = [
        nodeType ? titleCaseWords(nodeType) : null,
        nodeId,
      ]
        .filter(Boolean)
        .join(" · ");
      return detail ? `${base} · ${detail}` : base;
    }
    case "workflow.run.retried": {
      const retriedRunId = readPayloadString(payload, "retried_run_id");
      return retriedRunId ? `Retried as ${retriedRunId}` : base;
    }
    case "workflow.run.cancel_requested":
    case "workflow.run.cancelled":
    case "workflow.run.expired":
    case "workflow.run.failed": {
      const detail = lifecycleReasonOrError(payload);
      return detail ? `${base} · ${detail}` : base;
    }
    default:
      return base;
  }
}

export function workflowRunLifecycleDetail(
  eventType: string,
  payload: unknown,
  fallbacks: {
    cancelReason?: string | null;
    failureDetail?: string | null;
  } = {},
): string | null {
  switch (eventType) {
    case "workflow.run.approval.approved": {
      const approvalId = readPayloadString(payload, "runtime_approval_id");
      const reason = lifecycleReasonOrError(payload);
      if (approvalId && reason) return `Runtime approval ${approvalId} approved: ${reason}`;
      if (approvalId) return `Runtime approval ${approvalId} approved`;
      if (reason) return `Runtime approval approved: ${reason}`;
      return "Runtime approval approved";
    }
    case "workflow.run.approval.rejected": {
      const approvalId = readPayloadString(payload, "runtime_approval_id");
      const reason = lifecycleReasonOrError(payload);
      if (approvalId && reason) return `Runtime approval ${approvalId} rejected: ${reason}`;
      if (approvalId) return `Runtime approval ${approvalId} rejected`;
      if (reason) return `Runtime approval rejected: ${reason}`;
      return "Runtime approval rejected";
    }
    case "workflow.run.recovered": {
      const workerId = readPayloadString(payload, "worker_id");
      const reason = lifecycleReasonOrError(payload);
      if (workerId && reason) return `Recovered by ${workerId}: ${reason}`;
      if (workerId) return `Recovered by ${workerId}`;
      if (reason) return `Recovered: ${reason}`;
      return "Run recovered";
    }
    case "workflow.run.resumed": {
      const eventName = readPayloadString(payload, "event_name");
      return eventName ? `Resume event ${eventName}` : null;
    }
    case "workflow.run.retried": {
      const retriedRunId = readPayloadString(payload, "retried_run_id");
      return retriedRunId ? `Retried as ${retriedRunId}` : null;
    }
    case "workflow.run.cancel_requested": {
      const reason = lifecycleReasonOrError(payload) ?? fallbacks.cancelReason ?? null;
      return reason ? `Cancel requested: ${reason}` : null;
    }
    case "workflow.run.cancelled": {
      const reason = lifecycleReasonOrError(payload) ?? fallbacks.cancelReason ?? null;
      return reason ? `Cancelled: ${reason}` : null;
    }
    case "workflow.run.expired": {
      const detail = lifecycleReasonOrError(payload) ?? fallbacks.failureDetail ?? null;
      return detail ? `Expired: ${detail}` : null;
    }
    case "workflow.run.failed": {
      const detail = lifecycleReasonOrError(payload) ?? fallbacks.failureDetail ?? null;
      return detail ? `Failure: ${detail}` : null;
    }
    default:
      return null;
  }
}

export function workflowRunLifecycleReason(payload: unknown): string | null {
  return lifecycleReasonOrError(payload);
}

export function workflowRunLifecycleMessage(
  runId: string,
  eventType: string,
  payload: unknown,
  options: {
    approvalKind?: WorkflowRunApprovalCheckpointKind | null;
  } = {},
): string {
  switch (eventType) {
    case "workflow.run.queued":
      return `Run ${runId} queued.`;
    case "workflow.run.recovered": {
      const reason = lifecycleReasonOrError(payload);
      return reason
        ? `Run ${runId} recovered and re-queued: ${reason}.`
        : `Run ${runId} recovered and re-queued.`;
    }
    case "workflow.run.started":
      return `Run ${runId} started.`;
    case "workflow.run.node_started":
      return `Run ${runId} is running.`;
    case "workflow.run.approval.approved":
      return `Approval recorded for ${runId}.`;
    case "workflow.run.approval.rejected": {
      const reason = lifecycleReasonOrError(payload);
      return reason
        ? `Runtime approval rejected for ${runId}: ${reason}.`
        : `Runtime approval rejected for ${runId}.`;
    }
    case "workflow.run.waiting_approval":
      return `Run ${runId} is awaiting ${workflowRunApprovalNoun(options.approvalKind ?? null)}.`;
    case "workflow.run.waiting_event":
      return `Run ${runId} is waiting for event.`;
    case "workflow.run.resumed":
      return `Run ${runId} resumed.`;
    case "workflow.run.retried": {
      const retriedRunId = readPayloadString(payload, "retried_run_id");
      return retriedRunId
        ? `Run ${runId} retried as ${retriedRunId}.`
        : `Run ${runId} was retried.`;
    }
    case "workflow.run.cancel_requested": {
      const reason = lifecycleReasonOrError(payload);
      return reason
        ? `Run ${runId} has a cancel request pending: ${reason}.`
        : `Run ${runId} has a cancel request pending.`;
    }
    case "workflow.run.cancelled": {
      const reason = lifecycleReasonOrError(payload);
      return reason
        ? `Run ${runId} was cancelled: ${reason}.`
        : `Run ${runId} was cancelled.`;
    }
    case "workflow.run.completed":
      return `Run ${runId} completed.`;
    case "workflow.run.expired": {
      const detail = lifecycleReasonOrError(payload);
      return detail
        ? `Run ${runId} expired: ${detail}.`
        : `Run ${runId} expired.`;
    }
    case "workflow.run.failed": {
      const detail = lifecycleReasonOrError(payload);
      return detail
        ? `Run ${runId} failed: ${detail}.`
        : `Run ${runId} failed.`;
    }
    default: {
      const status = workflowRunStatusFromEventType(eventType);
      return status
        ? workflowRunStatusMessage(runId, status)
        : `Run ${runId} ${lifecycleTypeLabel(eventType).toLowerCase()}.`;
    }
  }
}
