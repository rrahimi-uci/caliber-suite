/**
 * QueuedMessages — pending turns waiting to be sent, shown above the composer
 * (OpenAI-style "add to queue"). Steer items are marked as priority
 * course-corrections that jump ahead of plain follow-ups.
 */

import type { AssistantQueuedMessage } from "@/api/assistantTypes";
import { cn } from "@/lib/utils";

interface QueuedMessagesProps {
  items: AssistantQueuedMessage[];
  onCancel: (queueId: string) => void;
}

export function QueuedMessages({ items, onCancel }: QueuedMessagesProps): JSX.Element | null {
  if (items.length === 0) return null;
  return (
    <div className="space-y-1" data-testid="assistant-queue">
      <p className="text-[10px] font-semibold uppercase tracking-wide text-slate-400 dark:text-slate-400">
        Queued ({items.length})
      </p>
      {items.map((item) => (
        <div
          key={item.queue_id}
          className={cn(
            "flex items-center gap-2 rounded-lg border px-2.5 py-1.5 text-xs",
            item.kind === "steer"
              ? "border-amber-300 dark:border-amber-400/50 bg-amber-50 dark:bg-amber-500/10 text-amber-800 dark:text-amber-100"
              : "border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-600 dark:text-slate-200",
          )}
        >
          {item.kind === "steer" && (
            <span className="shrink-0 rounded-full bg-amber-200/70 dark:bg-amber-400/20 px-1.5 py-0.5 text-[9px] font-semibold uppercase">
              Steer
            </span>
          )}
          <span className="min-w-0 flex-1 truncate">{item.content}</span>
          <button
            type="button"
            aria-label="Remove queued message"
            onClick={() => onCancel(item.queue_id)}
            className="shrink-0 rounded-full p-0.5 text-slate-400 hover:text-red-500 dark:hover:text-red-300"
          >
            <svg className="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
              <path d="M18 6L6 18M6 6l12 12" />
            </svg>
          </button>
        </div>
      ))}
    </div>
  );
}
