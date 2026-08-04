/**
 * CaliberAssistantPanel — right-side sliding drawer (MLflow-style).
 *
 * Renders over the main content area, accessible from any page via the
 * sidebar toggle.  Manages its own session / message / draft state
 * internally, delegating to the same API surface the old full-page view
 * used.
 */

import { useCallback, useRef, useState, useEffect } from "react";

import { caliberApi } from "@/api/caliberApi";
import type {
  ArtifactType,
  AssistantApprovalMode,
  AssistantConfig,
  AssistantDraft,
  AssistantMessage,
  AssistantMode,
  AssistantQueuedMessage,
  AssistantSession,
  ClarifyingQuestion,
} from "@/api/assistantTypes";
import { ARTIFACT_TYPE_LABELS } from "@/api/assistantTypes";
import type { AriaAutonomy, AriaPlanDetail } from "@/api/types";
import {
  AUTONOMY_LABELS,
  AUTONOMY_LEVELS,
  pickResumablePlan,
} from "@/components/aria/planView";
import {
  useApiQuery,
  useApiMutation,
  useInvalidate,
} from "@/hooks/useApiQuery";
import { showToast } from "@/lib/toast";
import { cn } from "@/lib/utils";
import { readDefaultAssistantSkillMode } from "@/lib/assistantPreferences";

import { AccessBadge } from "./AccessBadge";
import { AriaLogo } from "./AriaLogo";
import { AriaPlanCard } from "./AriaPlanCard";
import {
  ASSISTANT_PANEL_DEFAULT_WIDTH,
  useAssistantPanel,
} from "./AssistantPanelContext";
import { ApprovalModeSelector } from "./ApprovalModeSelector";
import { AssistantModelSelector } from "./AssistantModelSelector";
import { AssistantSettings } from "./AssistantSettings";
import { AttachmentBar } from "./AttachmentBar";
import { ChatHistory } from "./ChatHistory";
import { DraftStatusBadge } from "./DraftStatusBadge";
import { ModeSelector } from "./ModeSelector";
import { QuestionList } from "./QuestionList";
import { QueuedMessages } from "./QueuedMessages";
import {
  AssistantProcessSteps,
  processStepsFromMetadata,
} from "./AssistantProcessSteps";
import { ToolCallList, toolCallsFromMetadata } from "./ToolCallList";

/* ------------------------------------------------------------------ */
/* Query keys                                                         */
/* ------------------------------------------------------------------ */

const QK = {
  sessions: ["assistant", "sessions"] as const,
  messages: (id: string) => ["assistant", "messages", id] as const,
  drafts: (id: string) => ["assistant", "drafts", id] as const,
  queue: (id: string) => ["assistant", "queue", id] as const,
};

// The active session id is persisted so a page refresh restores the
// conversation instead of starting blank (the panel reloads its messages).
const ACTIVE_SESSION_KEY = "caliber.assistant.session.active";

function readActiveSessionId(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(ACTIVE_SESSION_KEY);
  } catch {
    return null;
  }
}

function writeActiveSessionId(id: string | null): void {
  try {
    if (id) window.localStorage.setItem(ACTIVE_SESSION_KEY, id);
    else window.localStorage.removeItem(ACTIVE_SESSION_KEY);
  } catch {
    // Ignore storage failures; session still active for this page load.
  }
}

const EMPTY_SESSIONS: AssistantSession[] = [];

/* ------------------------------------------------------------------ */
/* Chat bubble icon                                                   */
/* ------------------------------------------------------------------ */

function ChatIcon({ className }: { className?: string }): JSX.Element {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
    >
      <path d="M21 11.5a8.38 8.38 0 01-.9 3.8 8.5 8.5 0 01-7.6 4.7 8.38 8.38 0 01-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 01-.9-3.8 8.5 8.5 0 014.7-7.6 8.38 8.38 0 013.8-.9h.5a8.48 8.48 0 018 8v.5z" />
    </svg>
  );
}

function formatAssistantMessageTime(value: string): string {
  return new Date(value).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  });
}

function AssistantMessageBubble({
  msg,
}: {
  msg: AssistantMessage;
}): JSX.Element {
  const isUser = msg.role === "user";
  const toolCalls = isUser ? [] : toolCallsFromMetadata(msg.metadata_);
  const processSteps = isUser ? [] : processStepsFromMetadata(msg.metadata_);

  return (
    <div
      className={cn("flex w-full", isUser ? "justify-end" : "justify-start")}
      data-testid={
        isUser ? "assistant-message-user" : "assistant-message-assistant"
      }
    >
      <div
        className={cn(
          "flex max-w-[92%] items-start gap-2.5",
          isUser ? "pl-8" : "pr-4",
        )}
      >
        {!isUser && (
          <AriaLogo
            data-testid="assistant-message-avatar"
            className="mt-1 h-8 w-8 shrink-0 ring-1 ring-slate-200 dark:ring-slate-700"
            alt=""
          />
        )}
        <div
          className={cn(
            "rounded-2xl border px-3.5 py-3 shadow-sm",
            isUser
              ? "border-caliber-200/80 bg-caliber-50 text-slate-900 dark:border-caliber-400/30 dark:bg-caliber-500/15 dark:text-caliber-50"
              : "border-slate-200/80 bg-white text-slate-800 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100",
          )}
        >
          <div
            className={cn(
              "mb-1.5 flex items-center gap-2",
              isUser && "justify-end",
            )}
          >
            <span
              className={cn(
                "text-[10px] font-semibold uppercase tracking-[0.18em]",
                isUser
                  ? "text-caliber-700 dark:text-caliber-200"
                  : "text-slate-500 dark:text-slate-400",
              )}
            >
              {isUser ? "You" : "Aria"}
            </span>
            <time className="text-[10px] text-slate-400 dark:text-slate-500">
              {formatAssistantMessageTime(msg.created_at)}
            </time>
          </div>
          {!isUser && <AssistantProcessSteps steps={processSteps} />}
          <p
            className={cn(
              "whitespace-pre-wrap",
              isUser ? "text-sm leading-6" : "text-[13px] leading-5",
            )}
          >
            {msg.content}
          </p>
          {toolCalls.length > 0 && <ToolCallList toolCalls={toolCalls} />}
        </div>
      </div>
    </div>
  );
}

function AssistantThinkingBubble(): JSX.Element {
  return (
    <div className="flex w-full justify-start">
      <div className="flex max-w-[92%] items-start gap-2.5 pr-4">
        <AriaLogo
          data-testid="assistant-message-avatar"
          className="mt-1 h-8 w-8 shrink-0 ring-1 ring-slate-200 dark:ring-slate-700"
          alt=""
        />
        <div className="rounded-2xl border border-slate-200/80 bg-white px-3.5 py-3 text-[13px] leading-5 text-slate-500 shadow-sm dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300">
          <div className="mb-1.5 flex items-center gap-2">
            <span className="text-[10px] font-semibold uppercase tracking-[0.18em] text-slate-500 dark:text-slate-400">
              Aria
            </span>
            <span className="text-[10px] text-slate-400 dark:text-slate-500">
              Thinking
            </span>
          </div>
          <div className="inline-flex items-center gap-2">
            <span className="inline-flex gap-1">
              <span className="animate-bounce">·</span>
              <span className="animate-bounce [animation-delay:150ms]">·</span>
              <span className="animate-bounce [animation-delay:300ms]">·</span>
            </span>
            <span>Thinking…</span>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Suggested prompt cards                                              */
/* ------------------------------------------------------------------ */

// No leading emoji: each card already renders a ChatIcon, and the string is sent
// verbatim as the user's first message — a decorative emoji shouldn't land in the
// transcript or the model input.
const SUGGESTED_PROMPTS = [
  "Create a tool that validates email addresses and returns clear error messages",
  "Build a skill that summarizes support tickets with severity and next action",
  "Add an MCP server and wire it into a workflow for live data access",
];

/* ------------------------------------------------------------------ */
/* Panel                                                              */
/* ------------------------------------------------------------------ */

export function CaliberAssistantPanel(): JSX.Element | null {
  const { open, close, panelWidth, setPanelWidth, collapsed, toggleCollapsed } =
    useAssistantPanel();
  const [activeSessionId, setActiveSessionId] = useState<string | null>(() =>
    readActiveSessionId(),
  );
  const [activeSession, setActiveSession] = useState<AssistantSession | null>(
    null,
  );
  const [artifactType] = useState<ArtifactType | null>(null);
  const [questions, setQuestions] = useState<ClarifyingQuestion[]>([]);
  const [input, setInput] = useState("");
  // Interaction mode mirrors a code assistant's Chat / Build / Plan toggle.
  // "plan" drives the intent workbench; "chat"/"build" go through send-message.
  const [mode, setMode] = useState<AssistantMode>("build");
  const [approvalMode, setApprovalMode] =
    useState<AssistantApprovalMode>("manual");
  const [showHistory, setShowHistory] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [ariaPlan, setAriaPlan] = useState<AriaPlanDetail | null>(null);
  const [planAutonomy, setPlanAutonomy] =
    useState<AriaAutonomy>("approve_plan");
  const [planError, setPlanError] = useState<string | null>(null);
  const [isPlanning, setIsPlanning] = useState(false);
  const [showDrafts, setShowDrafts] = useState(false);
  const [isDesktop, setIsDesktop] = useState<boolean>(() => {
    if (
      typeof window === "undefined" ||
      typeof window.matchMedia !== "function"
    )
      return true;
    return window.matchMedia("(min-width: 768px)").matches;
  });
  const [isResizing, setIsResizing] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const resizeStartXRef = useRef(0);
  const resizeStartWidthRef = useRef(ASSISTANT_PANEL_DEFAULT_WIDTH);
  const invalidate = useInvalidate();

  const collapsedView = isDesktop && collapsed;

  useEffect(() => {
    if (
      typeof window === "undefined" ||
      typeof window.matchMedia !== "function"
    ) {
      setIsDesktop(true);
      return;
    }
    const media = window.matchMedia("(min-width: 768px)");
    const update = (event: MediaQueryListEvent | MediaQueryList): void => {
      setIsDesktop(event.matches);
    };

    update(media);
    if (typeof media.addEventListener === "function") {
      media.addEventListener("change", update);
      return () => media.removeEventListener("change", update);
    }

    media.addListener(update);
    return () => media.removeListener(update);
  }, []);

  useEffect(() => {
    if (!isResizing) return;

    const onMouseMove = (event: MouseEvent): void => {
      const delta = resizeStartXRef.current - event.clientX;
      setPanelWidth(resizeStartWidthRef.current + delta);
    };
    const onMouseUp = (): void => setIsResizing(false);

    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";

    return () => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
  }, [isResizing, setPanelWidth]);

  const handleResizeStart = useCallback(
    (event: React.MouseEvent<HTMLButtonElement>) => {
      if (!isDesktop || collapsedView) return;
      event.preventDefault();
      resizeStartXRef.current = event.clientX;
      resizeStartWidthRef.current = panelWidth;
      setIsResizing(true);
    },
    [collapsedView, isDesktop, panelWidth],
  );

  /* ---- sessions (also used to restore the persisted active session) ---- */
  const sessionsQuery = useApiQuery<AssistantSession[]>(
    QK.sessions,
    () => caliberApi.listAssistantSessions(),
    {
      enabled: open,
    },
  );
  const sessions = sessionsQuery.data ?? EMPTY_SESSIONS;

  const assistantConfigQuery = useApiQuery<AssistantConfig>(
    ["assistant", "config"],
    () => caliberApi.getAssistantConfig(),
    { enabled: open },
  );
  const [assistantConfig, setAssistantConfig] =
    useState<AssistantConfig | null>(null);

  useEffect(() => {
    if (assistantConfigQuery.data) {
      setAssistantConfig(assistantConfigQuery.data);
    }
  }, [assistantConfigQuery.data]);

  // Persist the active session id across refreshes.
  useEffect(() => {
    writeActiveSessionId(activeSessionId);
  }, [activeSessionId]);

  // After a refresh we restore `activeSessionId` from storage but not the
  // session *object*; hydrate it from the sessions list once it loads. If the
  // persisted id is gone (deleted/expired), drop it so we don't query a ghost.
  useEffect(() => {
    if (!activeSessionId || activeSession || !sessionsQuery.isSuccess) return;
    const match = sessions.find((s) => s.session_id === activeSessionId);
    if (match) {
      setActiveSession(match);
    } else {
      setActiveSessionId(null);
    }
  }, [activeSessionId, activeSession, sessions, sessionsQuery.isSuccess]);

  // Reattach an in-flight goal-plan when the active session changes, so reopening
  // a session restores its plan inline instead of losing it — durable plans
  // outlive the chat turn. Terminal plans stay in the Plans dashboard, not the
  // thread. Functional updates avoid clobbering a plan just created in-session.
  useEffect(() => {
    let cancelled = false;
    const sid = activeSessionId;
    // Drop a plan carried over from a different session; keep one for this session.
    setAriaPlan((prev) => (prev && prev.plan.session_id === sid ? prev : null));
    if (!sid) return undefined;
    void (async () => {
      try {
        const plans = await caliberApi.listAriaPlans(sid);
        if (cancelled) return;
        const resumable = pickResumablePlan(plans);
        if (!resumable) return;
        const detail = await caliberApi.getAriaPlan(resumable.plan_id);
        if (!cancelled) setAriaPlan((prev) => prev ?? detail);
      } catch {
        /* no in-flight plan to restore */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activeSessionId]);

  // Restore the session's last interaction mode and approval mode when it loads.
  useEffect(() => {
    const stored = activeSession?.metadata_?.assistant_mode;
    if (stored === "chat" || stored === "build" || stored === "plan") {
      setMode(stored);
    }
    const approval = activeSession?.metadata_?.assistant_approval_mode;
    if (
      approval === "manual" ||
      approval === "auto_safe" ||
      approval === "auto_all" ||
      approval === "agent_review" ||
      approval === "full_autonomy"
    ) {
      setApprovalMode(approval);
    }
  }, [activeSession]);

  const createSession = useApiMutation(
    async () => {
      const s = await caliberApi.createAssistantSession({
        title: "New session",
        goal: "",
        artifact_type: artifactType ?? undefined,
        skill_mode: readDefaultAssistantSkillMode(),
      });
      return s;
    },
    {
      onSuccess(session: AssistantSession) {
        invalidate(QK.sessions);
        setActiveSessionId(session.session_id);
        setActiveSession(session);
        setQuestions([]);
        setShowDrafts(false);
        setInput("");
      },
    },
  );

  /* ---- messages ---- */
  const { data: messages = [] } = useApiQuery<AssistantMessage[]>(
    activeSessionId ? QK.messages(activeSessionId) : ["noop"],
    () =>
      activeSessionId
        ? caliberApi.listAssistantMessages(activeSessionId)
        : Promise.resolve([]),
    { enabled: !!activeSessionId && open },
  );

  /* ---- drafts ---- */
  const { data: drafts = [] } = useApiQuery<AssistantDraft[]>(
    activeSessionId ? QK.drafts(activeSessionId) : ["noop-d"],
    () =>
      activeSessionId
        ? caliberApi.listAssistantDrafts(activeSessionId)
        : Promise.resolve([]),
    { enabled: !!activeSessionId && open },
  );

  /* ---- send message ---- */
  const sendMessage = useApiMutation(
    async ({
      content,
      sessionId,
      mode: overrideMode,
      steer,
    }: {
      content: string;
      sessionId?: string;
      mode?: AssistantMode;
      steer?: boolean;
    }) => {
      const sid = sessionId ?? activeSessionId;
      if (!sid) return;
      const res = await caliberApi.sendAssistantMessage(sid, {
        content,
        artifact_type: artifactType ?? undefined,
        mode: overrideMode ?? mode,
        steer: steer ?? false,
        approval_mode: approvalMode,
        current_surface: "assistant_drawer",
      });
      return res;
    },
    {
      onSuccess(res) {
        if (!activeSessionId || !res) return;
        invalidate(QK.messages(activeSessionId));
        invalidate(QK.drafts(activeSessionId));
        if (res.questions?.length) {
          setQuestions(res.questions);
        } else {
          setQuestions([]);
        }
      },
    },
  );

  /* ---- message queue ("add to queue" + steer) ---- */
  const { data: queued = [] } = useApiQuery<AssistantQueuedMessage[]>(
    activeSessionId ? QK.queue(activeSessionId) : ["noop-q"],
    () =>
      activeSessionId
        ? caliberApi.listAssistantQueue(activeSessionId)
        : Promise.resolve([]),
    { enabled: !!activeSessionId && open },
  );

  const enqueueMessage = useApiMutation(
    async ({
      content,
      kind,
    }: {
      content: string;
      kind: "queued" | "steer";
    }) => {
      const sid = await ensureSession();
      await caliberApi.enqueueAssistantMessage(sid, { content, mode, kind });
      return sid;
    },
    {
      onSuccess(sid) {
        invalidate(QK.queue(sid));
      },
    },
  );

  const cancelQueued = useApiMutation(
    async (queueId: string) => caliberApi.cancelAssistantQueued(queueId),
    {
      onSuccess() {
        if (activeSessionId) invalidate(QK.queue(activeSessionId));
      },
    },
  );

  // Auto-dispatch the head of the queue once the current turn settles, so
  // queued follow-ups (and priority steers, which sit at the front) flow in
  // order — the OpenAI-chat / Claude-Code behavior.
  const dispatchingRef = useRef(false);
  // Queue ids this panel has already dispatched. The row is only removed from the
  // server queue *after* the send succeeds (see below), so without this the same
  // head would be re-dispatched on every re-render until the request completed.
  const dispatchedRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!activeSessionId || !open) return;
    if (sendMessage.isPending || isPlanning || createSession.isPending) return;
    if (dispatchingRef.current || queued.length === 0) return;
    const head = queued[0];
    if (!head) return;
    if (dispatchedRef.current.has(head.queue_id)) return;
    dispatchingRef.current = true;
    dispatchedRef.current.add(head.queue_id);
    // Send first, remove second. The previous order deleted the queue row before
    // sending, swallowed the delete's failure, and issued the send from a
    // ``.finally()`` so it ran on both branches — so a send that failed left the
    // user's message deleted server-side, absent from the panel, and unreported.
    // Losing typed input is worse than a visible retry, so the row now survives
    // until the turn is accepted.
    sendMessage.mutate(
      {
        content: head.content,
        mode: head.mode,
        steer: head.kind === "steer",
      },
      {
        onSuccess: () => {
          void caliberApi
            .cancelAssistantQueued(head.queue_id)
            .catch(() => {
              // The turn was accepted, so the message is not lost; the queue row
              // is. Report it rather than swallowing, because the stale row is
              // visible to the user and would otherwise look like a stuck item.
              showToast.error(
                "Sent, but the queued copy could not be cleared.",
              );
            })
            .finally(() => {
              invalidate(QK.queue(activeSessionId));
            });
        },
        onError: () => {
          // Leave the row queued and allow another attempt.
          dispatchedRef.current.delete(head.queue_id);
          showToast.error(
            "Could not send the queued message. It is still in the queue.",
          );
        },
        onSettled: () => {
          dispatchingRef.current = false;
        },
      },
    );
  }, [
    queued,
    sendMessage,
    isPlanning,
    createSession.isPending,
    activeSessionId,
    open,
    invalidate,
  ]);

  /* ---- draft actions ---- */
  const validateDraft = useApiMutation(
    async (draftId: string) => caliberApi.validateAssistantDraft(draftId),
    {
      onSuccess() {
        if (activeSessionId) invalidate(QK.drafts(activeSessionId));
        showToast.success("Validation complete");
      },
    },
  );

  const testDraft = useApiMutation(
    async (draftId: string) => caliberApi.testAssistantDraft(draftId),
    {
      onSuccess() {
        if (activeSessionId) invalidate(QK.drafts(activeSessionId));
        showToast.success("Tests complete");
      },
    },
  );

  const approveDraft = useApiMutation(
    async (draftId: string) => caliberApi.approveAssistantDraft(draftId),
    {
      onSuccess() {
        if (activeSessionId) invalidate(QK.drafts(activeSessionId));
        showToast.success("Draft approved");
      },
    },
  );

  const publishDraft = useApiMutation(
    async (draftId: string) => caliberApi.publishAssistantDraft(draftId),
    {
      onSuccess() {
        if (activeSessionId) invalidate(QK.drafts(activeSessionId));
        showToast.success("Published!");
      },
    },
  );

  const updateAssistantConfig = useApiMutation(
    async (body: { model?: string; reasoning?: string }) =>
      caliberApi.updateAssistantConfig(body),
    {
      onSuccess(updated) {
        setAssistantConfig(updated);
      },
      onError(error) {
        showToast.error(error.message || "Failed to update Aria runtime");
      },
    },
  );

  /* ---- handlers ---- */
  const resetAriaPlan = useCallback(() => {
    setAriaPlan(null);
    setPlanError(null);
  }, []);

  // Resolve the session to attach context to, creating one on first use so the
  // user can attach files before sending their first message.
  const ensureSession = useCallback(async (): Promise<string> => {
    if (activeSessionId) return activeSessionId;
    const session = await caliberApi.createAssistantSession({
      title: "New session",
      goal: "",
      artifact_type: artifactType ?? undefined,
      skill_mode: readDefaultAssistantSkillMode(),
    });
    invalidate(QK.sessions);
    setActiveSessionId(session.session_id);
    setActiveSession(session);
    return session.session_id;
  }, [activeSessionId, artifactType, invalidate]);

  // Plan mode: hand the goal to the orchestrator, which decomposes it into a
  // capability-bound plan rendered inline (approve/run/answer happen in-thread).
  const buildAriaPlan = useCallback(
    async (goal: string) => {
      setIsPlanning(true);
      setPlanError(null);
      try {
        const sid = await ensureSession();
        const detail = await caliberApi.createAriaPlan({
          goal,
          session_id: sid,
          autonomy: planAutonomy,
        });
        setAriaPlan(detail);
      } catch (error) {
        setPlanError(
          error instanceof Error ? error.message : "Failed to build plan",
        );
      } finally {
        setIsPlanning(false);
      }
    },
    [ensureSession, planAutonomy],
  );

  const turnBusy = sendMessage.isPending || isPlanning;

  const handleSubmit = useCallback(
    (e?: React.FormEvent) => {
      e?.preventDefault();
      const trimmed = input.trim();
      if (!trimmed || createSession.isPending) return;
      // A turn is already running — stack this as a queued follow-up instead of
      // dropping it; the auto-dispatch effect sends it when the turn settles.
      if (turnBusy) {
        setInput("");
        enqueueMessage.mutate({ content: trimmed, kind: "queued" });
        return;
      }
      setInput("");
      if (mode === "plan") {
        void buildAriaPlan(trimmed);
        return;
      }
      if (!activeSessionId) {
        createSession.mutate(undefined, {
          onSuccess(session) {
            if (!session) return;
            sendMessage.mutate({
              content: trimmed,
              sessionId: session.session_id,
            });
          },
        });
      } else {
        sendMessage.mutate({ content: trimmed });
      }
    },
    [
      input,
      sendMessage,
      activeSessionId,
      createSession,
      turnBusy,
      enqueueMessage,
      mode,
      buildAriaPlan,
    ],
  );

  // Steer: a priority course-correction that jumps to the front of the queue.
  const handleSteer = useCallback(() => {
    const trimmed = input.trim();
    if (!trimmed) return;
    setInput("");
    enqueueMessage.mutate({ content: trimmed, kind: "steer" });
  }, [input, enqueueMessage]);

  const handleSuggestedPrompt = useCallback(
    (prompt: string) => {
      if (!activeSessionId) {
        // Auto-create session, then send
        createSession.mutate(undefined, {
          onSuccess(session) {
            if (!session) return;
            sendMessage.mutate({
              content: prompt,
              sessionId: session.session_id,
            });
          },
        });
      } else {
        sendMessage.mutate({ content: prompt });
      }
    },
    [activeSessionId, createSession, sendMessage],
  );

  const handleNewChat = useCallback(() => {
    resetAriaPlan();
    createSession.mutate(undefined);
  }, [createSession, resetAriaPlan]);

  const handleQuestionAnswer = useCallback(
    (answer: string) => sendMessage.mutate({ content: answer }),
    [sendMessage],
  );

  // Scroll to bottom when the conversation changes — including when a plan is
  // decomposed or updates in Plan mode (which doesn't touch messages/questions),
  // so a freshly-built plan or mid-run interaction isn't left below the fold.
  useEffect(() => {
    if (typeof bottomRef.current?.scrollIntoView === "function") {
      bottomRef.current.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages.length, questions.length, ariaPlan, isPlanning]);

  // Escape closes the slide-over (matches the dropdown/settings modals).
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, close]);

  if (!open) return null;

  const hasSession = !!activeSessionId;
  const hasMessages = messages.length > 0;

  return (
    <div
      className="fixed top-14 right-0 bottom-0 z-50 flex"
      data-testid="assistant-panel"
    >
      {/* Backdrop for mobile */}
      <button
        type="button"
        aria-label="Close assistant"
        className="md:hidden fixed inset-0 top-14 bg-black/20 backdrop-blur-sm"
        onClick={close}
      />

      {/* Panel */}
      <aside
        aria-label="Aria assistant"
        className="relative ml-auto w-full sm:w-[380px] md:w-[var(--assistant-panel-width)] max-w-full bg-white dark:bg-slate-900 border-l border-slate-200 dark:border-slate-700 shadow-xl flex flex-col animate-slide-in-right"
      >
        {isDesktop && !collapsedView && (
          <button
            type="button"
            aria-label="Resize assistant panel"
            data-testid="assistant-resize-handle"
            onMouseDown={handleResizeStart}
            className="hidden md:block absolute -left-1 top-0 h-full w-2 cursor-col-resize"
          >
            <span className="sr-only">Resize assistant panel</span>
          </button>
        )}

        {collapsedView ? (
          <div className="h-full flex flex-col items-center justify-between px-2 py-3 bg-white dark:bg-slate-900">
            <div className="flex flex-col items-center gap-3">
              <AriaLogo className="w-8 h-8" />
              <button
                type="button"
                onClick={toggleCollapsed}
                className="p-1.5 rounded-md text-slate-500 dark:text-slate-300 hover:text-slate-700 dark:hover:text-slate-100 hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors"
                title="Expand assistant"
                aria-label="Expand assistant"
              >
                <svg
                  className="w-4 h-4"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                >
                  <path d="M9 18l6-6-6-6" />
                </svg>
              </button>
            </div>
            <button
              type="button"
              onClick={close}
              className="p-1.5 rounded-md text-slate-400 dark:text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors"
              title="Close"
              aria-label="Close"
            >
              <svg
                className="w-4 h-4"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
              >
                <path d="M18 6L6 18M6 6l12 12" />
              </svg>
            </button>
          </div>
        ) : (
          <>
            {/* ---- Header ---- */}
            <div className="flex items-center justify-between px-4 py-3 border-b border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900">
              <div className="flex items-center gap-2 min-w-0">
                <AriaLogo className="w-8 h-8 shrink-0" alt="" />
                <span className="text-sm font-semibold text-slate-800 dark:text-slate-100 truncate">
                  Aria
                </span>
                <AccessBadge />
              </div>
              <div className="flex items-center gap-1">
                {isDesktop && (
                  <button
                    type="button"
                    onClick={toggleCollapsed}
                    className="p-1.5 rounded-md text-slate-400 dark:text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors"
                    title="Collapse assistant"
                    aria-label="Collapse assistant"
                  >
                    <svg
                      className="w-4 h-4"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                    >
                      <path d="M15 18l-6-6 6-6" />
                    </svg>
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => setShowHistory((v) => !v)}
                  className={cn(
                    "p-1.5 rounded-md transition-colors",
                    showHistory
                      ? "text-caliber-600 dark:text-caliber-300 bg-caliber-50 dark:bg-caliber-500/20"
                      : "text-slate-400 dark:text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800",
                  )}
                  title="Chat history"
                  aria-label="Chat history"
                >
                  <svg
                    className="w-4 h-4"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                  >
                    <circle cx="12" cy="12" r="9" />
                    <path d="M12 7v5l3 2" />
                  </svg>
                </button>
                <button
                  type="button"
                  onClick={() => setShowSettings(true)}
                  className="p-1.5 rounded-md text-slate-400 dark:text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors"
                  title="Aria settings"
                  aria-label="Aria settings"
                >
                  <svg
                    className="w-4 h-4"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                  >
                    <circle cx="12" cy="12" r="3" />
                    <path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 11-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 11-2.83-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 110-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06a1.65 1.65 0 001.82.33H9a1.65 1.65 0 001-1.51V3a2 2 0 114 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 112.83 2.83l-.06.06a1.65 1.65 0 00-.33 1.82V9a1.65 1.65 0 001.51 1H21a2 2 0 110 4h-.09a1.65 1.65 0 00-1.51 1z" />
                  </svg>
                </button>
                <button
                  type="button"
                  onClick={handleNewChat}
                  className="p-1.5 rounded-md text-slate-400 dark:text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors"
                  title="New Chat"
                  aria-label="New Chat"
                >
                  <svg
                    className="w-4 h-4"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                  >
                    <path d="M12 5v14M5 12h14" />
                  </svg>
                </button>
                {drafts.length > 0 && (
                  <button
                    type="button"
                    onClick={() => setShowDrafts((v) => !v)}
                    className={cn(
                      "p-1.5 rounded-md transition-colors",
                      showDrafts
                        ? "text-caliber-600 dark:text-caliber-300 bg-caliber-50 dark:bg-caliber-500/20"
                        : "text-slate-400 dark:text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800",
                    )}
                    title="Toggle drafts"
                    aria-label="Toggle drafts"
                  >
                    <svg
                      className="w-4 h-4"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                    >
                      <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" />
                      <polyline points="14 2 14 8 20 8" />
                    </svg>
                    <span className="sr-only">Drafts ({drafts.length})</span>
                  </button>
                )}
                <button
                  type="button"
                  onClick={close}
                  className="p-1.5 rounded-md text-slate-400 dark:text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors"
                  title="Close"
                  aria-label="Close"
                >
                  <svg
                    className="w-4 h-4"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                  >
                    <path d="M18 6L6 18M6 6l12 12" />
                  </svg>
                </button>
              </div>
            </div>

            {/* ---- Chat history drawer ---- */}
            {showHistory && (
              <ChatHistory
                sessions={sessions}
                activeSessionId={activeSessionId}
                onSelect={(session) => {
                  setActiveSessionId(session.session_id);
                  setActiveSession(session);
                  setQuestions([]);
                  setShowDrafts(false);
                  setShowHistory(false);
                }}
                onNewChat={() => {
                  setShowHistory(false);
                  handleNewChat();
                }}
              />
            )}

            {/* ---- Drafts drawer ---- */}
            {showDrafts && drafts.length > 0 && (
              <div className="border-b border-slate-200 dark:border-slate-700 bg-slate-50/50 dark:bg-slate-900/60 max-h-64 overflow-y-auto">
                <div className="p-3 space-y-2">
                  <p className="text-[10px] font-semibold text-slate-400 dark:text-slate-300 uppercase tracking-wide">
                    Drafts ({drafts.length})
                  </p>
                  {drafts.map((d) => (
                    <DraftCard
                      key={d.draft_id}
                      draft={d}
                      busy={
                        validateDraft.isPending ||
                        testDraft.isPending ||
                        approveDraft.isPending ||
                        publishDraft.isPending
                      }
                      onValidate={() => validateDraft.mutate(d.draft_id)}
                      onTest={() => testDraft.mutate(d.draft_id)}
                      onApprove={() => approveDraft.mutate(d.draft_id)}
                      onPublish={() => publishDraft.mutate(d.draft_id)}
                    />
                  ))}
                </div>
              </div>
            )}

            {/* ---- Body ---- */}
            <div className="flex-1 overflow-y-auto">
              {!hasSession && !hasMessages ? (
                /* Welcome state */
                <div className="flex flex-col items-center justify-center h-full p-6 gap-4">
                  <AriaLogo className="w-[72px] h-[72px]" />
                  <p className="text-sm text-slate-500 dark:text-slate-300 text-center leading-relaxed">
                    Hi, I&apos;m Aria 👋 I can help you design and create tools,
                    skills, prompts, workflows, and MCP servers. I&apos;ll ask
                    clarifying questions and draft implementation-ready outputs
                    you can review and publish.
                  </p>
                  <div className="w-full space-y-2 mt-2">
                    {SUGGESTED_PROMPTS.map((prompt) => (
                      <button
                        key={prompt}
                        type="button"
                        onClick={() => handleSuggestedPrompt(prompt)}
                        className="w-full flex items-start gap-2.5 px-3 py-2.5 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 text-left text-sm text-slate-600 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-slate-800 hover:border-slate-300 dark:hover:border-slate-600 transition-colors group"
                      >
                        <ChatIcon className="mt-0.5 h-4 w-4 shrink-0 text-caliber-500 dark:text-caliber-300 group-hover:text-caliber-600 dark:group-hover:text-caliber-200" />
                        <span>{prompt}</span>
                      </button>
                    ))}
                  </div>
                </div>
              ) : (
                /* Chat messages */
                <div className="min-h-full space-y-4 bg-slate-50/50 p-4 dark:bg-slate-950/35">
                  {mode === "plan" && (
                    <div className="space-y-2">
                      <div className="flex items-center justify-between gap-2 rounded-md border border-violet-200 bg-violet-50/60 px-3 py-2 text-xs text-gray-600 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300">
                        <span>
                          Plan mode — describe a goal and Aria drafts a plan you
                          approve & run here.
                        </span>
                        <label className="flex items-center gap-1.5">
                          Autonomy
                          <select
                            aria-label="Plan autonomy"
                            value={planAutonomy}
                            disabled={turnBusy}
                            title={AUTONOMY_LABELS[planAutonomy].hint}
                            onChange={(e) =>
                              setPlanAutonomy(e.target.value as AriaAutonomy)
                            }
                            className="rounded-md border border-surface-200 bg-white px-2 py-1 text-xs dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
                          >
                            {AUTONOMY_LEVELS.map((a) => (
                              <option key={a} value={a}>
                                {AUTONOMY_LABELS[a].label}
                              </option>
                            ))}
                          </select>
                        </label>
                      </div>
                      {planError && (
                        <div className="rounded-md border border-red-200 bg-red-50 px-2.5 py-2 text-xs text-red-700">
                          {planError}
                        </div>
                      )}
                      {isPlanning && !ariaPlan && (
                        <p className="px-1 text-xs text-slate-500 dark:text-slate-300">
                          Decomposing the goal into a plan…
                        </p>
                      )}
                      {ariaPlan && (
                        <AriaPlanCard
                          initialDetail={ariaPlan}
                          onChange={setAriaPlan}
                        />
                      )}
                    </div>
                  )}

                  {messages.map((msg) => (
                    <AssistantMessageBubble key={msg.message_id} msg={msg} />
                  ))}

                  <QuestionList
                    questions={questions}
                    onAnswer={handleQuestionAnswer}
                  />

                  {sendMessage.isPending && <AssistantThinkingBubble />}

                  {/* Inline draft cards in chat */}
                  {drafts.length > 0 && !showDrafts && (
                    <div className="space-y-2 pt-2">
                      <button
                        type="button"
                        onClick={() => setShowDrafts(true)}
                        className="text-xs text-caliber-600 dark:text-caliber-300 hover:text-caliber-800 dark:hover:text-caliber-200 font-medium"
                      >
                        {drafts.length} draft{drafts.length !== 1 ? "s" : ""}{" "}
                        generated — view details →
                      </button>
                    </div>
                  )}

                  <div ref={bottomRef} />
                </div>
              )}
            </div>

            {/* ---- Input ---- */}
            <div className="space-y-3 border-t border-slate-200 bg-gradient-to-b from-white to-slate-50/80 p-3 dark:border-slate-700 dark:from-slate-900 dark:to-slate-950/80">
              <div className="rounded-[26px] border border-slate-200/80 bg-white/90 shadow-sm dark:border-slate-700 dark:bg-slate-950/75">
                <div className="border-b border-slate-200/80 px-2.5 py-2 dark:border-slate-700/80">
                  <div className="flex flex-wrap items-center gap-2">
                    <ModeSelector
                      value={mode}
                      onChange={setMode}
                      disabled={sendMessage.isPending || isPlanning}
                    />
                    <ApprovalModeSelector
                      value={approvalMode}
                      onChange={setApprovalMode}
                      autonomy={assistantConfig?.autonomy}
                      disabled={sendMessage.isPending || isPlanning}
                    />
                    <AssistantModelSelector
                      config={assistantConfig}
                      disabled={sendMessage.isPending || isPlanning}
                      isLoading={
                        assistantConfigQuery.isLoading && !assistantConfig
                      }
                      isSaving={updateAssistantConfig.isPending}
                      onModelChange={(nextModel) =>
                        updateAssistantConfig.mutate({ model: nextModel })
                      }
                      onReasoningChange={(nextReasoning) =>
                        updateAssistantConfig.mutate({
                          reasoning: nextReasoning,
                        })
                      }
                    />
                  </div>
                </div>
                <form
                  onSubmit={handleSubmit}
                  className="flex items-end gap-2 p-2"
                >
                  <input
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    placeholder={
                      turnBusy
                        ? "Type to queue a follow-up…"
                        : mode === "plan"
                          ? "Describe a plan..."
                          : "Ask Aria for follow-up changes..."
                    }
                    className="flex-1 rounded-[20px] border border-transparent bg-transparent px-3 py-2.5 text-[13px] text-slate-800 outline-none transition-colors placeholder:text-slate-400 focus:border-caliber-400/40 focus:bg-slate-50 focus:ring-2 focus:ring-caliber-400/20 dark:text-slate-100 dark:placeholder:text-slate-500 dark:focus:bg-slate-900/60"
                  />
                  <button
                    type="button"
                    onClick={handleSteer}
                    disabled={!input.trim()}
                    title="Steer — send a priority course-correction"
                    aria-label="Steer"
                    className="inline-flex h-10 w-10 items-center justify-center rounded-full border border-amber-200 bg-amber-50 text-amber-700 transition-colors hover:bg-amber-100 disabled:cursor-not-allowed disabled:opacity-30 dark:border-amber-400/30 dark:bg-amber-500/12 dark:text-amber-200 dark:hover:bg-amber-500/20"
                  >
                    <svg
                      className="h-5 w-5"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                    >
                      <circle cx="12" cy="12" r="9" />
                      <path d="M12 3v3M12 18v3M3 12h3M18 12h3" />
                      <circle
                        cx="12"
                        cy="12"
                        r="2.5"
                        fill="currentColor"
                        stroke="none"
                      />
                    </svg>
                  </button>
                  <button
                    type="submit"
                    disabled={!input.trim()}
                    aria-label={
                      turnBusy
                        ? "Add to queue"
                        : mode === "plan"
                          ? "Build plan"
                          : "Send message"
                    }
                    className="inline-flex h-10 w-10 items-center justify-center rounded-full bg-caliber-600 text-white shadow-sm transition-colors hover:bg-caliber-700 disabled:cursor-not-allowed disabled:bg-slate-200 disabled:text-slate-400 dark:bg-caliber-500 dark:hover:bg-caliber-400 dark:disabled:bg-slate-800 dark:disabled:text-slate-500"
                  >
                    {turnBusy ? (
                      <svg
                        className="h-5 w-5"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="2"
                      >
                        <path d="M12 5v14M5 12h14" />
                      </svg>
                    ) : (
                      <svg
                        className="h-5 w-5"
                        viewBox="0 0 24 24"
                        fill="currentColor"
                      >
                        <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z" />
                      </svg>
                    )}
                  </button>
                </form>
              </div>
              <AttachmentBar
                sessionId={activeSessionId}
                ensureSession={ensureSession}
                disabled={sendMessage.isPending || isPlanning}
              />
              <QueuedMessages
                items={queued}
                onCancel={(id) => cancelQueued.mutate(id)}
              />
            </div>
          </>
        )}
      </aside>

      {showSettings && (
        <AssistantSettings onClose={() => setShowSettings(false)} />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Inline draft card for the panel                                    */
/* ------------------------------------------------------------------ */

interface DraftCardProps {
  draft: AssistantDraft;
  onValidate: () => void;
  onTest: () => void;
  onApprove: () => void;
  onPublish: () => void;
  /** A draft mutation is in flight — disable the action buttons to prevent a
   *  double-submit (publish writes a live resource). */
  busy?: boolean;
}

function DraftCard({
  draft,
  onValidate,
  onTest,
  onApprove,
  onPublish,
  busy = false,
}: DraftCardProps): JSX.Element {
  const [expanded, setExpanded] = useState(false);
  const canValidate = ["draft", "validation_failed"].includes(draft.status);
  const canTest = ["validated", "test_failed"].includes(draft.status);
  const canApprove = draft.status === "tested";
  const canPublish = draft.status === "approved";

  return (
    <div className="rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-2.5 text-sm">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0">
          <p className="font-medium text-slate-800 dark:text-slate-100 truncate text-xs">
            {draft.title || "Untitled"}
          </p>
          <p className="text-[10px] text-slate-400 dark:text-slate-400">
            {ARTIFACT_TYPE_LABELS[draft.artifact_type] ?? draft.artifact_type}
          </p>
        </div>
        <DraftStatusBadge status={draft.status} />
      </div>

      <div className="flex items-center gap-1.5 mt-2">
        {canValidate && (
          <button
            type="button"
            onClick={onValidate}
            disabled={busy}
            className="px-2 py-1 text-[10px] font-medium rounded border border-slate-200 dark:border-slate-700 text-slate-600 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-slate-800 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Validate
          </button>
        )}
        {canTest && (
          <button
            type="button"
            onClick={onTest}
            disabled={busy}
            className="px-2 py-1 text-[10px] font-medium rounded border border-slate-200 dark:border-slate-700 text-slate-600 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-slate-800 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Test
          </button>
        )}
        {canApprove && (
          <button
            type="button"
            onClick={onApprove}
            disabled={busy}
            className="px-2 py-1 text-[10px] font-medium rounded border border-amber-300 bg-amber-50 text-amber-700 hover:bg-amber-100 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Approve
          </button>
        )}
        {canPublish && (
          <button
            type="button"
            onClick={onPublish}
            disabled={busy}
            className="px-2 py-1 text-[10px] font-medium rounded bg-green-600 text-white hover:bg-green-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Publish
          </button>
        )}
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="ml-auto px-2 py-1 text-[10px] text-slate-400 dark:text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 transition-colors"
        >
          {expanded ? "Hide" : "Details"}
        </button>
      </div>

      {expanded && (
        <pre className="mt-2 text-[10px] bg-slate-50 dark:bg-slate-800 rounded p-2 overflow-auto text-slate-600 dark:text-slate-200 border border-slate-100 dark:border-slate-700 max-h-40">
          {JSON.stringify(draft.artifact, null, 2)}
        </pre>
      )}
    </div>
  );
}
