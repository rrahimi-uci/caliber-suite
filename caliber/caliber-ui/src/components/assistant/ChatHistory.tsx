/**
 * ChatHistory — browse, switch, rename, and archive past Aria sessions.
 * Renders as a drawer inside the panel (like the drafts drawer).
 */

import { useState } from "react";

import { caliberApi } from "@/api/caliberApi";
import type { AssistantSession } from "@/api/assistantTypes";
import { useApiMutation, useInvalidate } from "@/hooks/useApiQuery";
import { showToast } from "@/lib/toast";
import { cn } from "@/lib/utils";

const SESSIONS_KEY = ["assistant", "sessions"] as const;

interface ChatHistoryProps {
  sessions: AssistantSession[];
  activeSessionId: string | null;
  onSelect: (session: AssistantSession) => void;
  onNewChat: () => void;
}

export function ChatHistory({
  sessions,
  activeSessionId,
  onSelect,
  onNewChat,
}: ChatHistoryProps): JSX.Element {
  const invalidate = useInvalidate();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState("");

  const rename = useApiMutation(
    ({ id, title }: { id: string; title: string }) =>
      caliberApi.updateAssistantSession(id, { title }),
    {
      onSuccess() {
        invalidate(SESSIONS_KEY);
        setEditingId(null);
      },
    },
  );

  const archive = useApiMutation(
    (id: string) => caliberApi.updateAssistantSession(id, { status: "archived" }),
    {
      onSuccess() {
        invalidate(SESSIONS_KEY);
        showToast.success("Session archived");
      },
    },
  );

  const visible = sessions.filter((s) => s.status !== "archived");

  return (
    <div
      data-testid="assistant-history"
      className="border-b border-slate-200 dark:border-slate-700 bg-slate-50/50 dark:bg-slate-900/60 max-h-72 overflow-y-auto"
    >
      <div className="p-3 space-y-2">
        <div className="flex items-center justify-between">
          <p className="text-[10px] font-semibold text-slate-400 dark:text-slate-300 uppercase tracking-wide">
            Chat history ({visible.length})
          </p>
          <button
            type="button"
            onClick={onNewChat}
            className="text-[11px] font-medium text-caliber-600 dark:text-caliber-300 hover:text-caliber-800 dark:hover:text-caliber-200"
          >
            + New chat
          </button>
        </div>

        {visible.length === 0 ? (
          <p className="text-xs text-slate-400 dark:text-slate-400">No sessions yet.</p>
        ) : (
          visible.map((session) => (
            <div
              key={session.session_id}
              className={cn(
                "rounded-lg border px-2.5 py-2 text-sm",
                session.session_id === activeSessionId
                  ? "border-caliber-300 dark:border-caliber-400/60 bg-white dark:bg-slate-900"
                  : "border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900",
              )}
            >
              {editingId === session.session_id ? (
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    rename.mutate({ id: session.session_id, title: draftTitle.trim() || "Untitled" });
                  }}
                  className="flex items-center gap-1.5"
                >
                  <input
                    autoFocus
                    value={draftTitle}
                    onChange={(e) => setDraftTitle(e.target.value)}
                    className="flex-1 rounded border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 px-2 py-1 text-xs text-slate-800 dark:text-slate-100"
                  />
                  <button
                    type="submit"
                    className="text-[11px] font-medium text-caliber-600 dark:text-caliber-300"
                  >
                    Save
                  </button>
                </form>
              ) : (
                <div className="flex items-center justify-between gap-2">
                  <button
                    type="button"
                    onClick={() => onSelect(session)}
                    className="min-w-0 flex-1 text-left"
                  >
                    <p className="truncate text-xs font-medium text-slate-800 dark:text-slate-100">
                      {session.title || "Untitled"}
                    </p>
                    <p className="text-[10px] text-slate-400 dark:text-slate-400">
                      {new Date(session.updated_at).toLocaleString()}
                    </p>
                  </button>
                  <div className="flex shrink-0 items-center gap-1">
                    <button
                      type="button"
                      aria-label="Rename session"
                      title="Rename"
                      onClick={() => {
                        setEditingId(session.session_id);
                        setDraftTitle(session.title);
                      }}
                      className="p-1 rounded text-slate-400 hover:text-slate-600 dark:hover:text-slate-200"
                    >
                      <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                        <path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7" />
                        <path d="M18.5 2.5a2.12 2.12 0 013 3L12 15l-4 1 1-4 9.5-9.5z" />
                      </svg>
                    </button>
                    <button
                      type="button"
                      aria-label="Archive session"
                      title="Archive"
                      onClick={() => archive.mutate(session.session_id)}
                      className="p-1 rounded text-slate-400 hover:text-red-500 dark:hover:text-red-300"
                    >
                      <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                        <polyline points="21 8 21 21 3 21 3 8" />
                        <rect x="1" y="3" width="22" height="5" />
                        <line x1="10" y1="12" x2="14" y2="12" />
                      </svg>
                    </button>
                  </div>
                </div>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
