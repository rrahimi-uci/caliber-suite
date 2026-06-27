import type { WorkflowSessionMemoryEntry } from "@/api/workflowTypes";

interface WorkflowSessionMemoryPanelProps {
  sessionId: string | null | undefined;
  runStatus?: string | null;
  entries?: WorkflowSessionMemoryEntry[];
  loading?: boolean;
  clearingSession?: boolean;
  clearingNodeId?: string | null;
  onClearSession?: () => void;
  onClearNode?: (nodeId: string) => void;
}

function isActiveRunStatus(runStatus: string | null | undefined): boolean {
  return (
    runStatus === "queued"
    || runStatus === "running"
    || runStatus === "resuming"
    || runStatus === "cancel_requested"
    || runStatus === "waiting_approval"
    || runStatus === "waiting_event"
  );
}

function isStoppedRunStatus(runStatus: string | null | undefined): boolean {
  return (
    runStatus === "failed"
    || runStatus === "cancelled"
    || runStatus === "rejected"
    || runStatus === "expired"
    || runStatus === "blocked"
  );
}

function emptySessionMemoryMessage(runStatus: string | null | undefined): JSX.Element {
  if (isActiveRunStatus(runStatus)) {
    return (
      <>
        No persisted memory exists for this session yet. This run may still be executing or the
        next assistant turn may not have been flushed, so reuse the same{" "}
        <span className="font-mono">session_id</span> across runs and refresh this panel after the
        next assistant turn is recorded.
      </>
    );
  }
  if (runStatus === "completed") {
    return (
      <>
        No persisted memory was recorded for this session during the completed run. Reuse the same{" "}
        <span className="font-mono">session_id</span> on the next run if you want future agent
        turns to accumulate here, and inspect the debugger or final outputs if you need to confirm
        whether any assistant turn actually executed.
      </>
    );
  }
  if (isStoppedRunStatus(runStatus)) {
    return (
      <>
        No persisted memory was recorded for this session before the run stopped. Inspect the
        debugger and recovery panels to confirm whether execution reached an assistant turn, then
        reuse the same <span className="font-mono">session_id</span> when you retry or rerun it.
      </>
    );
  }
  return (
    <>
      No persisted memory exists for this session yet. Reuse the same{" "}
      <span className="font-mono">session_id</span> across runs to accumulate agent state, then
      refresh this panel after the next assistant turn is recorded.
    </>
  );
}

function missingSessionMemoryMessage(runStatus: string | null | undefined): JSX.Element {
  if (isActiveRunStatus(runStatus)) {
    return (
      <>
        This run did not set a shared <span className="font-mono">session_id</span>, so no
        reusable agent memory can be attached while the current execution is still in flight.
        Queue the next run with the <span className="font-mono">session_id</span> you want to
        carry across retries or follow-up executions.
      </>
    );
  }
  if (runStatus === "completed") {
    return (
      <>
        This completed run did not set a shared <span className="font-mono">session_id</span>, so
        no reusable agent memory was recorded for it. Inspect the debugger or final outputs if you
        need to confirm whether assistant turns executed, then rerun it with the same{" "}
        <span className="font-mono">session_id</span> you want to carry across follow-up
        executions.
      </>
    );
  }
  if (isStoppedRunStatus(runStatus)) {
    return (
      <>
        This run stopped without setting a shared <span className="font-mono">session_id</span>, so
        no reusable agent memory could be attached before execution failed. Inspect the debugger and
        recovery panels to trace where it stopped, then retry or rerun it with the{" "}
        <span className="font-mono">session_id</span> you want to reuse.
      </>
    );
  }
  return (
    <>
      This run did not set a shared <span className="font-mono">session_id</span>, so no reusable
      agent memory was attached to it. Re-run it with the same{" "}
      <span className="font-mono">session_id</span> you want to carry across retries or follow-up
      executions.
    </>
  );
}

export function sessionMemoryLoadErrorMessage(
  runStatus: string | null | undefined,
  errorMessage: string,
): JSX.Element {
  const detail = errorMessage.trim() || "Unknown error";
  if (isActiveRunStatus(runStatus)) {
    return (
      <>
        Session memory could not be loaded while this run is still in flight. The next assistant
        turn may not have flushed yet, or the session-memory lookup may be degraded, so use the
        debugger and recovery panels to confirm live execution state while this history is
        unavailable.
        <span className="mt-2 block text-red-700/80">Latest lookup error: {detail}</span>
      </>
    );
  }
  if (runStatus === "completed") {
    return (
      <>
        Session memory could not be loaded for this completed run even though it recorded a shared{" "}
        <span className="font-mono">session_id</span>. Inspect the debugger, final outputs, and
        generated artifacts to confirm whether assistant turns executed, then retry this lookup or
        rerun with the same <span className="font-mono">session_id</span> if you need the reusable
        conversation state restored.
        <span className="mt-2 block text-red-700/80">Latest lookup error: {detail}</span>
      </>
    );
  }
  if (isStoppedRunStatus(runStatus)) {
    return (
      <>
        Session memory could not be loaded for this stopped run even though it recorded a shared{" "}
        <span className="font-mono">session_id</span>. Inspect the debugger and recovery panels to
        trace where execution stopped, then retry this lookup or rerun with the same{" "}
        <span className="font-mono">session_id</span> if you still need the reusable conversation
        state.
        <span className="mt-2 block text-red-700/80">Latest lookup error: {detail}</span>
      </>
    );
  }
  return (
    <>
      Session memory could not be loaded for this run. Inspect the debugger and recovery panels,
      then retry this lookup if you need the reusable conversation state for the recorded{" "}
      <span className="font-mono">session_id</span>.
      <span className="mt-2 block text-red-700/80">Latest lookup error: {detail}</span>
    </>
  );
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function roleBadge(role: "user" | "assistant"): string {
  return role === "user"
    ? "bg-slate-100 text-slate-600 ring-slate-200/80"
    : "bg-emerald-50 text-emerald-700 ring-emerald-200/80";
}

export function WorkflowSessionMemoryPanel({
  sessionId,
  runStatus = null,
  entries = [],
  loading = false,
  clearingSession = false,
  clearingNodeId = null,
  onClearSession,
  onClearNode,
}: WorkflowSessionMemoryPanelProps): JSX.Element {
  if (!sessionId) {
    return (
      <div
        data-testid="workflow-session-memory-missing"
        className="rounded-xl border border-dashed border-slate-200 bg-slate-50/70 px-4 py-3 text-xs leading-relaxed text-slate-500"
      >
        {missingSessionMemoryMessage(runStatus)}
      </div>
    );
  }

  const totalMessages = entries.reduce((sum, entry) => sum + entry.message_count, 0);
  const totalTurns = entries.reduce((sum, entry) => sum + entry.turn_count, 0);

  return (
    <div data-testid="workflow-session-memory-panel" className="space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
            Session Memory
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-500">
            <span className="rounded-md bg-slate-50 px-2 py-1 font-mono text-slate-600 ring-1 ring-slate-200/70">
              {sessionId}
            </span>
            {!loading && entries.length > 0 && (
              <>
                <span>{entries.length} node histories</span>
                <span>{totalMessages} messages</span>
                <span>{totalTurns} assistant turns</span>
              </>
            )}
          </div>
        </div>
        {onClearSession && entries.length > 0 && (
          <button
            type="button"
            data-testid="workflow-session-memory-clear-session"
            disabled={clearingSession}
            onClick={onClearSession}
            className="rounded-lg border border-red-200 bg-red-50 px-3 py-1.5 text-xs font-semibold text-red-700 transition-colors hover:bg-red-100 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {clearingSession ? "Clearing..." : "Clear session"}
          </button>
        )}
      </div>

      {loading ? (
        <div className="rounded-xl border border-slate-200 bg-slate-50/70 px-4 py-4 text-xs text-slate-400">
          Loading stored conversation state…
        </div>
      ) : entries.length === 0 ? (
        <div
          data-testid="workflow-session-memory-empty"
          className="rounded-xl border border-dashed border-slate-200 bg-slate-50/70 px-4 py-4 text-xs leading-relaxed text-slate-500"
        >
          {emptySessionMemoryMessage(runStatus)}
        </div>
      ) : (
        <div className="space-y-3">
          {entries.map((entry) => {
            const visibleMessages = entry.message_history.slice(-6);
            const hiddenCount = Math.max(0, entry.message_history.length - visibleMessages.length);
            const lastUpdated = formatTimestamp(entry.updated_at);
            return (
              <div
                key={`${entry.session_id}:${entry.node_id}`}
                data-testid={`workflow-session-memory-entry-${entry.node_id}`}
                className="rounded-2xl border border-slate-200/70 bg-white px-4 py-4 shadow-sm"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <div className="font-mono text-sm font-semibold text-slate-800">{entry.node_id}</div>
                    <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
                      <span>{entry.message_count} messages</span>
                      <span>{entry.turn_count} assistant turns</span>
                      <span>updated {lastUpdated}</span>
                    </div>
                  </div>
                  {onClearNode && (
                    <button
                      type="button"
                      data-testid={`workflow-session-memory-clear-node-${entry.node_id}`}
                      disabled={clearingNodeId === entry.node_id}
                      onClick={() => onClearNode(entry.node_id)}
                      className="rounded-lg border border-slate-200 bg-white px-2.5 py-1 text-[11px] font-semibold text-slate-600 transition-colors hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {clearingNodeId === entry.node_id ? "Clearing..." : "Clear node"}
                    </button>
                  )}
                </div>

                {(entry.last_user_message || entry.last_assistant_message) && (
                  <div className="mt-3 grid gap-2 md:grid-cols-2">
                    {entry.last_user_message && (
                      <div className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2">
                        <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">
                          Latest user turn
                        </div>
                        <div className="mt-1 text-xs leading-relaxed text-slate-600">
                          {entry.last_user_message}
                        </div>
                      </div>
                    )}
                    {entry.last_assistant_message && (
                      <div className="rounded-xl border border-emerald-200/80 bg-emerald-50/60 px-3 py-2">
                        <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-emerald-700/80">
                          Latest assistant turn
                        </div>
                        <div className="mt-1 text-xs leading-relaxed text-emerald-900/80">
                          {entry.last_assistant_message}
                        </div>
                      </div>
                    )}
                  </div>
                )}

                <div className="mt-3 rounded-xl border border-slate-200 bg-slate-50/80 p-3">
                  <div className="mb-2 flex items-center justify-between gap-2">
                    <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">
                      Transcript
                    </div>
                    {hiddenCount > 0 && (
                      <div className="text-[10px] text-slate-400">
                        Showing latest {visibleMessages.length} of {entry.message_history.length}
                      </div>
                    )}
                  </div>
                  <div className="max-h-64 space-y-2 overflow-auto">
                    {visibleMessages.map((message, index) => (
                      <div
                        key={`${entry.node_id}-${index}-${message.role}`}
                        className="rounded-xl border border-white/80 bg-white px-3 py-2 shadow-sm"
                      >
                        <div className="mb-1">
                          <span
                            className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] ring-1 ${roleBadge(message.role)}`}
                          >
                            {message.role}
                          </span>
                        </div>
                        <div className="whitespace-pre-wrap text-xs leading-relaxed text-slate-600">
                          {message.content}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
