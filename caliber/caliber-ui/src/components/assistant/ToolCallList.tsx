/**
 * ToolCallList — the actions Aria took this turn (read/execute tools), shown
 * under an assistant reply so the copilot visibly "shows its work".
 */

import type { AssistantToolCall } from "@/api/assistantTypes";
import { cn } from "@/lib/utils";

interface ToolCallListProps {
  toolCalls: AssistantToolCall[];
}

/** Pull tool calls off a message's metadata (persisted under `tool_calls`). */
export function toolCallsFromMetadata(metadata: Record<string, unknown> | undefined): AssistantToolCall[] {
  const raw = metadata?.tool_calls;
  if (!Array.isArray(raw)) return [];
  return raw.filter((c): c is AssistantToolCall => !!c && typeof c === "object" && "name" in c);
}

export function ToolCallList({ toolCalls }: ToolCallListProps): JSX.Element | null {
  if (toolCalls.length === 0) return null;
  return (
    <details className="mt-2 rounded-lg border border-slate-200/80 bg-slate-50/70 dark:border-slate-700 dark:bg-slate-950/40" data-testid="assistant-tool-calls">
      <summary className="cursor-pointer select-none px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-500 dark:text-slate-400">
        Actions · {toolCalls.length}
      </summary>
      <ul className="space-y-1 px-2.5 pb-2">
        {toolCalls.map((call, i) => (
          <li
            key={`${call.name}-${i}`}
            className="flex items-center gap-2 text-[11px] text-slate-600 dark:text-slate-300"
          >
            <span
              className={cn(
                "inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-[9px] font-bold",
                call.ok
                  ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-500/20 dark:text-emerald-300"
                  : "bg-red-100 text-red-700 dark:bg-red-500/20 dark:text-red-300",
              )}
              aria-hidden="true"
            >
              {call.ok ? "✓" : "!"}
            </span>
            <code className="font-medium text-slate-700 dark:text-slate-200">{call.name}</code>
            <span className="truncate text-slate-400 dark:text-slate-500">{call.result_summary}</span>
          </li>
        ))}
      </ul>
    </details>
  );
}
