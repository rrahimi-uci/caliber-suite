/**
 * Prompts — read-only list of deployed prompts per agent,
 * plus Playground tab for chatting with an LLM using a prompt,
 * and Test Cases tab for auto-generated prompt testing.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { MessageSquareText } from "lucide-react";
import { Link } from "react-router-dom";

import { caliberApi } from "@/api/caliberApi";
import { ClearFiltersButton } from "@/components/ClearFiltersButton";
import { FilterBar } from "@/components/FilterBar";
import { LIVE_ALIAS, SINGLE_ENVIRONMENT } from "@/lib/environment";
import {
  diffLines,
  diffStats,
  DIFF_LINE_CLASS,
  DIFF_WORD_CLASS,
} from "@/lib/textDiff";
import { ListRow, ListRows } from "@/components/ListRow";
import { PromptBuilder } from "@/components/PromptBuilder";
import { PageHeader } from "@/components/PageHeader";
import { PageTabs, type PageTab } from "@/components/PageTabs";
import { SearchInput } from "@/components/SearchInput";
import { FilterSelect } from "@/components/FilterSelect";
import { ViewToggle } from "@/components/ViewToggle";
import { CalibrationStep, StepConnector } from "@/components/CalibrationStep";
import { VersionPanel } from "@/components/versioning/VersionPanel";
import { makePromptVersionAdapter } from "@/components/versioning/adapters";
import { useApiQuery } from "@/hooks/useApiQuery";
import { useViewMode } from "@/hooks/useViewMode";
import type {
  AgentConfig,
  EvalDataset,
  PromptBindPayload,
  PromptCalibrationOptions,
  PromptCalibrationScorerOption,
  PromptCalibrationScorerSelection,
  PromptCreateResult,
  PromptInfo,
  PromptTestRunDetail,
  PromptTestRunSummary,
  PromptVersionInfo,
  PromptWorkspaceResponse,
  RefinementJob,
} from "@/api/types";
import type { Workflow } from "@/api/workflowTypes";
import type {
  AssistantConfig,
  AssistantIntentExecuteResult,
  AssistantIntentPlanResult,
  AssistantIntentResolveResult,
  AssistantModelOption,
  AssistantOperationStatus,
} from "@/api/assistantTypes";
import { useApi } from "@/hooks/useApi";

/**
 * The six per-prompt Workspace stages, in pipeline order. Opening a prompt from
 * the inventory drops into this focused workspace; each stage renders the reused
 * stage component scoped to the open prompt (no internal prompt picker).
 */
const WORKSPACE_STAGES: PageTab[] = [
  {
    key: "author",
    label: "Author",
    icon: (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.75"
      >
        <path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z" />
      </svg>
    ),
  },
  {
    key: "playground",
    label: "Playground",
    icon: (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.75"
      >
        <path d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
      </svg>
    ),
  },
  {
    key: "test-sets",
    label: "Test Sets",
    icon: (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.75"
      >
        <path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2M9 5a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2M9 5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2m-6 9l2 2 4-4" />
      </svg>
    ),
  },
  {
    key: "runs",
    label: "Runs",
    icon: (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.75"
      >
        <path d="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM9.555 7.168A1 1 0 0 0 8 8v4a1 1 0 0 0 1.555.832l3-2a1 1 0 0 0 0-1.664l-3-2z" />
      </svg>
    ),
  },
  {
    key: "calibration",
    label: "Calibration",
    icon: (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.75"
      >
        <path d="M4 6h16M4 12h10M4 18h7" />
        <circle cx="18" cy="12" r="3" />
        <circle cx="15" cy="18" r="2" />
      </svg>
    ),
  },
  {
    key: "bind",
    label: "Bind",
    icon: (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.75"
      >
        <path d="M10 13a5 5 0 0 0 7.07 0l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71M14 11a5 5 0 0 0-7.07 0l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
      </svg>
    ),
  },
];

type WorkspaceStage =
  | "author"
  | "playground"
  | "test-sets"
  | "runs"
  | "calibration"
  | "bind";

/** Tailwind tone per prompt lifecycle status (matches the backend status enum). */
const PROMPT_STATUS_TONES: Record<string, string> = {
  Draft: "bg-slate-100 text-slate-600 ring-slate-200/60",
  "Has test set": "bg-blue-50 text-blue-700 ring-blue-200/60",
  Tested: "bg-violet-50 text-caliber-purple ring-violet-200/60",
  Calibrated: "bg-amber-50 text-amber-700 ring-amber-200/60",
  Bound: "bg-emerald-50 text-emerald-700 ring-emerald-200/60",
};

/**
 * Small colored pill for the Workspace status. ``StatusBadge`` only knows the
 * job/approval vocabulary, so the prompt lifecycle states get their own pill.
 */
function PromptStatusBadge({ status }: { status: string }): JSX.Element {
  const tone =
    PROMPT_STATUS_TONES[status] ??
    "bg-slate-100 text-slate-600 ring-slate-200/60";
  return (
    <span
      data-testid="workspace-status-badge"
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ${tone}`}
    >
      {status}
    </span>
  );
}

/**
 * Human-readable labels for the Source filter dropdown. Capitalized so the
 * option text never textually collides with the lowercase source chip rendered
 * on each prompt card.
 */
const SOURCE_LABELS: Record<string, string> = {
  caliber: "Caliber",
  mlflow: "MLflow",
  both: "Both",
};

/**
 * Centered modal overlay used for the Edit and Versions panels. Rendered as a
 * fixed overlay so the panel is always visible no matter how far the prompt
 * grid is scrolled — previously these rendered inline at the top of the page,
 * so clicking "Edit"/"Versions" near the bottom appeared to do nothing.
 * Closes on Escape and backdrop click.
 */
function PromptModal({
  onClose,
  ariaLabelledBy,
  children,
}: {
  onClose: () => void;
  ariaLabelledBy: string;
  children: React.ReactNode;
}): JSX.Element {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby={ariaLabelledBy}
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-slate-900/40 p-4 backdrop-blur-sm sm:p-8"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="my-4 w-full max-w-2xl rounded-2xl bg-white shadow-card-hover sm:my-8">
        {children}
      </div>
    </div>
  );
}

export function Prompts(): JSX.Element {
  // Top level is now just the inventory or — when a prompt is open — its focused
  // Workspace. ``openPromptName`` drives which view shows; ``creatingPrompt`` is
  // the brand-new-prompt path (the Author stage hosts the create surface, and on
  // create we flip into the saved prompt's Workspace).
  const [openPromptName, setOpenPromptName] = useState<string | null>(null);
  const [creatingPrompt, setCreatingPrompt] = useState(false);
  const [createPrefillName, setCreatePrefillName] = useState("");
  const [createFlowKey, setCreateFlowKey] = useState(0);
  const [showEdit, setShowEdit] = useState(false);
  const [editTarget, setEditTarget] = useState<PromptInfo | null>(null);
  const [editTemplate, setEditTemplate] = useState("");
  const [editCommitMessage, setEditCommitMessage] = useState("");
  const [editTargetAlias, setEditTargetAlias] = useState(
    SINGLE_ENVIRONMENT ? LIVE_ALIAS : "staging",
  );
  const [loadingEdit, setLoadingEdit] = useState(false);
  const [savingEdit, setSavingEdit] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  const [initialEditTemplate, setInitialEditTemplate] = useState("");
  const [showVersions, setShowVersions] = useState(false);
  const [versionsTarget, setVersionsTarget] = useState<PromptInfo | null>(null);
  const [versionsData, setVersionsData] = useState<PromptVersionInfo[]>([]);
  const [loadingVersions, setLoadingVersions] = useState(false);
  const [versionsError, setVersionsError] = useState<string | null>(null);
  const [promotingVersion, setPromotingVersion] = useState<number | null>(null);
  const [compareLeftVersion, setCompareLeftVersion] = useState<number | null>(
    null,
  );
  const [compareRightVersion, setCompareRightVersion] = useState<number | null>(
    null,
  );
  const [compareLeftTemplate, setCompareLeftTemplate] = useState("");
  const [compareRightTemplate, setCompareRightTemplate] = useState("");
  const [loadingCompare, setLoadingCompare] = useState(false);
  const [compareError, setCompareError] = useState<string | null>(null);
  // Line + word level diff of the two compared templates, recomputed only when
  // either loaded template changes.
  const compareDiff = useMemo(
    () => diffLines(compareLeftTemplate, compareRightTemplate),
    [compareLeftTemplate, compareRightTemplate],
  );
  const compareDiffStats = useMemo(() => diffStats(compareDiff), [compareDiff]);
  // The versions promotable straight from the diff header (deduped, left then right).
  const comparePromoteVersions = useMemo(() => {
    const out: number[] = [];
    if (compareLeftVersion != null) out.push(compareLeftVersion);
    if (
      compareRightVersion != null &&
      compareRightVersion !== compareLeftVersion
    ) {
      out.push(compareRightVersion);
    }
    return out;
  }, [compareLeftVersion, compareRightVersion]);
  const [deletingPrompt, setDeletingPrompt] = useState(false);
  const [bulkDeleting, setBulkDeleting] = useState(false);
  // Page-level feedback for delete actions. ``editError`` only renders inside the
  // Edit modal, so a card/bulk delete that succeeds or fails needs its own banner
  // on the Prompts tab — otherwise the action looks like it did nothing.
  const [deleteNotice, setDeleteNotice] = useState<{
    tone: "success" | "error";
    message: string;
  } | null>(null);
  // ``/me`` is cached app-wide by react-query; deleting a prompt requires the
  // admin scope server-side, so the destructive affordance is hidden otherwise.
  const meQuery = useApiQuery(["me"], (s) => caliberApi.getMe(s));
  const isAdmin = meQuery.data?.is_admin ?? false;
  const fetcher = useCallback(
    (signal: AbortSignal) => caliberApi.listPrompts(signal),
    [],
  );
  const { data, error, loading, refresh } = useApi(fetcher);
  const promptRows = data ?? [];
  const editablePrompts = promptRows.filter((p) => p.has_prompt);
  // The set a user can play / calibrate. A "testable prompt" is any row that
  // represents an actual prompt with content — it has a non-empty ``prompt_name``
  // (a registered prompt, including a draft that is not yet aliased to prod, so
  // ``has_prompt`` may be false). It excludes pure promptless-agent placeholders
  // (a ``needs_prompt`` agent node with no ``prompt_name``). The backend
  // auto-provisions a hidden runtime target from the prompt name, so no agent
  // registration step is needed to test or calibrate any of these.
  const testablePrompts = promptRows.filter((p) => isTestablePrompt(p));
  const [promptSearch, setPromptSearch] = useState("");
  // ``stateFilter`` collapses the inventory to a single group (deployed / needs
  // prompt); ``sourceFilter`` narrows by registry source. Both default to the
  // empty "All" sentinel so the unfiltered view is unchanged.
  const [stateFilter, setStateFilter] = useState("");
  const [sourceFilter, setSourceFilter] = useState("");
  const [viewMode, setViewMode] = useViewMode("prompts");
  const promptQuery = promptSearch.trim().toLowerCase();
  // Source options are derived from the values actually present so we never show
  // an empty bucket (e.g. no "both" filter when nothing has both sources).
  const sourceOptions = Array.from(new Set(promptRows.map((p) => p.source)))
    .filter(Boolean)
    .sort()
    .map((source) => ({
      value: source,
      label: SOURCE_LABELS[source] ?? source,
    }));
  const filteredPrompts = promptRows.filter((p) => {
    if (sourceFilter && p.source !== sourceFilter) return false;
    if (!promptQuery) return true;
    return [
      p.agent_id,
      p.agent_name,
      p.prompt_name,
      p.alias,
      (p.available_aliases ?? []).join(" "),
      p.description,
      p.template_preview,
    ]
      .filter(Boolean)
      .some((field) => String(field).toLowerCase().includes(promptQuery));
  });
  const hasPromptFilters = Boolean(promptSearch || stateFilter || sourceFilter);
  // Two clearly-labelled groups in the inventory: prompts that are live in the
  // registry, and the backlog of assets that still need a prompt authored. The
  // State filter, when set, hides the non-matching group entirely.
  const deployedPrompts =
    stateFilter === "needs" ? [] : filteredPrompts.filter((p) => p.has_prompt);
  const needsPromptRows =
    stateFilter === "deployed"
      ? []
      : filteredPrompts.filter((p) => !p.has_prompt);
  // After the State filter hides a group, the empty-state must key off what is
  // actually visible — not the raw text-filtered list.
  const visiblePromptCount = deployedPrompts.length + needsPromptRows.length;
  const promptFiltersActive = Boolean(
    promptQuery || stateFilter || sourceFilter,
  );
  const deployedCount = promptRows.filter((p) => p.has_prompt).length;
  const promptlessCount = promptRows.filter((p) => !p.has_prompt).length;
  const sourceCount = new Set(promptRows.map((p) => p.source)).size;
  const PROMPT_STAT_TILES: Array<{
    key: string;
    label: string;
    value: number;
    tone: string;
    icon: JSX.Element;
  }> = [
    {
      key: "agents",
      label: "Agents in registry",
      value: promptRows.length,
      tone: "bg-violet-50 text-caliber-purple",
      icon: (
        <path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" />
      ),
    },
    {
      key: "deployed",
      label: "Deployed prompts",
      value: deployedCount,
      tone: "bg-emerald-50 text-emerald-600",
      icon: <path d="M22 11.08V12a10 10 0 11-5.93-9.14M22 4L12 14.01l-3-3" />,
    },
    {
      key: "draftless",
      label: "Missing prompts",
      value: promptlessCount,
      tone: "bg-amber-50 text-amber-600",
      icon: (
        <path d="M12 9v4M12 17h.01M10.29 3.86l-8 14A1 1 0 003.14 19h17.72a1 1 0 00.87-1.5l-8-14a1 1 0 00-1.74 0z" />
      ),
    },
    {
      key: "sources",
      label: "Sources represented",
      value: sourceCount,
      tone: "bg-blue-50 text-blue-600",
      icon: <path d="M12 3l7 4v5c0 5-3.5 9-7 10-3.5-1-7-5-7-10V7l7-4z" />,
    },
  ];

  // Open an existing prompt's Workspace (from a card/row click).
  const openPromptWorkspace = (prompt: PromptInfo) => {
    setCreatingPrompt(false);
    setCreatePrefillName("");
    setOpenPromptName(resolvePromptName(prompt));
  };

  // Start a brand-new prompt — the Workspace opens in create mode, where the
  // Author stage hosts the builder. ``prefillName`` seeds it from a backlog row.
  const openCreateWorkspace = (prefillName = "") => {
    setCreatePrefillName(prefillName);
    setCreateFlowKey((current) => current + 1);
    setCreatingPrompt(true);
    setOpenPromptName(null);
  };

  // Return to the inventory from the Workspace.
  const closeWorkspace = () => {
    setOpenPromptName(null);
    setCreatingPrompt(false);
    setCreatePrefillName("");
  };

  // A create-mode Workspace just saved a prompt: refresh the inventory and flip
  // straight into the saved prompt's Workspace (no more "open calibration" fork
  // — the workspace's own Calibration stage is one tab away).
  const handlePromptCreated = (created: PromptCreateResult) => {
    setCreatingPrompt(false);
    setCreatePrefillName("");
    refresh();
    setOpenPromptName(created.name);
  };

  const openEditPrompt = async (prompt: PromptInfo) => {
    const promptName = prompt.prompt_name ?? prompt.agent_id;
    setEditTarget(prompt);
    setEditTargetAlias(defaultEditTargetAlias(prompt));
    setShowEdit(true);
    setLoadingEdit(true);
    setEditError(null);
    setEditCommitMessage("");
    try {
      const detail = await caliberApi.getPrompt(
        promptName,
        prompt.alias || "prod",
      );
      setEditTemplate(detail.template);
      setInitialEditTemplate(detail.template);
    } catch (err) {
      setEditTemplate("");
      setInitialEditTemplate("");
      setEditError(
        err instanceof Error ? err.message : "Failed to load prompt",
      );
    } finally {
      setLoadingEdit(false);
    }
  };

  const hasUnsavedEditChanges =
    showEdit &&
    editTarget !== null &&
    !loadingEdit &&
    editTemplate !== initialEditTemplate;

  const confirmDiscardEditChanges = () => {
    if (!hasUnsavedEditChanges) {
      return true;
    }
    return window.confirm("You have unsaved prompt changes. Discard them?");
  };

  const closeEditPanel = () => {
    setShowEdit(false);
    setEditTarget(null);
    setEditTemplate("");
    setInitialEditTemplate("");
    setEditCommitMessage("");
    setEditTargetAlias(SINGLE_ENVIRONMENT ? LIVE_ALIAS : "staging");
    setEditError(null);
  };

  const closeVersionsPanel = () => {
    setShowVersions(false);
    setVersionsTarget(null);
    setVersionsData([]);
    setVersionsError(null);
    setCompareLeftVersion(null);
    setCompareRightVersion(null);
    setCompareLeftTemplate("");
    setCompareRightTemplate("");
    setCompareError(null);
  };

  const submitEditPrompt = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!editTarget) return;
    if (!editTemplate.trim()) {
      setEditError("Template is required.");
      return;
    }
    const promptName = editTarget.prompt_name ?? editTarget.agent_id;
    setSavingEdit(true);
    setEditError(null);
    try {
      const created = await caliberApi.createPromptVersion(promptName, {
        template: editTemplate,
        commit_message: editCommitMessage.trim() || undefined,
      });
      await caliberApi.promotePrompt(promptName, created.version, {
        alias: editTargetAlias,
        gate_state: "none",
        overridden: true,
        override_reason: "direct prompt edit activation",
      });
      closeEditPanel();
      refresh();
    } catch (err) {
      setEditError(
        err instanceof Error ? err.message : "Failed to save prompt changes",
      );
    } finally {
      setSavingEdit(false);
    }
  };

  const handleDeletePrompt = async (prompt: PromptInfo) => {
    const promptName = prompt.prompt_name ?? prompt.agent_id;
    const confirmed = window.confirm(
      `Permanently delete the prompt "${promptName}" and all of its versions? This cannot be undone.`,
    );
    if (!confirmed) return;
    setDeletingPrompt(true);
    setEditError(null);
    setDeleteNotice(null);
    try {
      await caliberApi.deletePrompt(promptName);
      if (
        editTarget &&
        (editTarget.prompt_name ?? editTarget.agent_id) === promptName
      ) {
        closeEditPanel();
      }
      if (
        versionsTarget &&
        (versionsTarget.prompt_name ?? versionsTarget.agent_id) === promptName
      ) {
        closeVersionsPanel();
      }
      refresh();
      setDeleteNotice({
        tone: "success",
        message: `Deleted prompt “${promptName}”. The agent registration remains, so the card now shows as having no prompt.`,
      });
    } catch (err) {
      const message =
        err instanceof Error ? err.message : "Failed to delete prompt";
      // Mirror to the modal banner when editing, and always surface on the page.
      setEditError(message);
      setDeleteNotice({
        tone: "error",
        message: `Failed to delete “${promptName}”: ${message}`,
      });
    } finally {
      setDeletingPrompt(false);
    }
  };

  // Admin-only bulk delete: remove every deployed prompt in one pass. Deletes run
  // sequentially so a mid-run failure leaves a clear "deleted N, failed on X" state
  // rather than a pile of overlapping registry errors.
  const handleDeleteAllPrompts = async () => {
    const deletable = promptRows.filter((p) => p.has_prompt);
    if (deletable.length === 0) return;
    const confirmed = window.confirm(
      `Permanently delete ALL ${deletable.length} deployed prompt(s) and every version? This cannot be undone.`,
    );
    if (!confirmed) return;
    setBulkDeleting(true);
    setEditError(null);
    setDeleteNotice(null);
    let deleted = 0;
    const failures: string[] = [];
    for (const prompt of deletable) {
      const promptName = prompt.prompt_name ?? prompt.agent_id;
      try {
        await caliberApi.deletePrompt(promptName);
        deleted += 1;
      } catch (err) {
        const message = err instanceof Error ? err.message : "unknown error";
        failures.push(`${promptName} (${message})`);
      }
    }
    closeEditPanel();
    closeVersionsPanel();
    refresh();
    setBulkDeleting(false);
    setDeleteNotice(
      failures.length === 0
        ? {
            tone: "success",
            message: `Deleted all ${deleted} deployed prompt(s).`,
          }
        : {
            tone: "error",
            message: `Deleted ${deleted} prompt(s); failed on ${failures.length}: ${failures.join("; ")}`,
          },
    );
  };

  const loadCompareTemplates = async (
    prompt: PromptInfo,
    leftVersion: number,
    rightVersion: number,
  ) => {
    const promptName = prompt.prompt_name ?? prompt.agent_id;
    setLoadingCompare(true);
    setCompareError(null);
    try {
      if (leftVersion === rightVersion) {
        const detail = await caliberApi.getPromptVersion(
          promptName,
          leftVersion,
        );
        setCompareLeftTemplate(detail.template);
        setCompareRightTemplate(detail.template);
      } else {
        const [leftDetail, rightDetail] = await Promise.all([
          caliberApi.getPromptVersion(promptName, leftVersion),
          caliberApi.getPromptVersion(promptName, rightVersion),
        ]);
        setCompareLeftTemplate(leftDetail.template);
        setCompareRightTemplate(rightDetail.template);
      }
    } catch (err) {
      setCompareLeftTemplate("");
      setCompareRightTemplate("");
      setCompareError(
        err instanceof Error ? err.message : "Failed to compare versions",
      );
    } finally {
      setLoadingCompare(false);
    }
  };

  const openVersions = async (prompt: PromptInfo) => {
    const promptName = prompt.prompt_name ?? prompt.agent_id;
    setShowVersions(true);
    setVersionsTarget(prompt);
    setLoadingVersions(true);
    setVersionsError(null);
    setCompareLeftVersion(null);
    setCompareRightVersion(null);
    setCompareLeftTemplate("");
    setCompareRightTemplate("");
    setCompareError(null);
    try {
      const items = await caliberApi.listPromptVersions(promptName);
      setVersionsData(items);
      if (items.length > 0) {
        const leftVersion = items[0]!.version;
        const rightVersion = items[1]?.version ?? items[0]!.version;
        setCompareLeftVersion(leftVersion);
        setCompareRightVersion(rightVersion);
        await loadCompareTemplates(prompt, leftVersion, rightVersion);
      }
    } catch (err) {
      setVersionsData([]);
      setVersionsError(
        err instanceof Error ? err.message : "Failed to load versions",
      );
    } finally {
      setLoadingVersions(false);
    }
  };

  const runCompare = async () => {
    if (
      !versionsTarget ||
      compareLeftVersion == null ||
      compareRightVersion == null
    ) {
      return;
    }
    await loadCompareTemplates(
      versionsTarget,
      compareLeftVersion,
      compareRightVersion,
    );
  };

  const promoteToProd = async (version: number) => {
    if (!versionsTarget) return;
    const promptName = versionsTarget.prompt_name ?? versionsTarget.agent_id;
    setPromotingVersion(version);
    setVersionsError(null);
    try {
      await caliberApi.setPromptAlias(promptName, LIVE_ALIAS, version);
      const items = await caliberApi.listPromptVersions(promptName);
      setVersionsData(items);
      refresh();
    } catch (err) {
      setVersionsError(
        err instanceof Error ? err.message : "Failed to promote version",
      );
    } finally {
      setPromotingVersion(null);
    }
  };

  // ── Brand-new prompt: a create-mode Workspace whose Author stage hosts the
  // builder. On save we flip into the saved prompt's Workspace.
  if (creatingPrompt) {
    return (
      <PromptWorkspace
        key={`create-${createFlowKey}`}
        prompts={testablePrompts}
        loading={loading}
        creating
        createPrefillName={createPrefillName}
        onBack={closeWorkspace}
        onPromptCreated={handlePromptCreated}
      />
    );
  }

  // ── Open an existing prompt's Workspace. Normally resolved from the inventory
  // rows; right after a create the new prompt may not be in the (re-fetching)
  // list yet, so fall back to a minimal synthesized row keyed by its name. The
  // Workspace header fetches its own live facts from the workspace endpoint, so
  // a thin placeholder is enough to mount the stages.
  if (openPromptName) {
    const openPrompt =
      promptRows.find((p) => resolvePromptName(p) === openPromptName) ??
      synthesizePromptInfo(openPromptName);
    return (
      <PromptWorkspace
        key={openPromptName}
        prompts={
          testablePrompts.some((p) => resolvePromptName(p) === openPromptName)
            ? testablePrompts
            : [openPrompt, ...testablePrompts]
        }
        loading={loading}
        prompt={openPrompt}
        onBack={closeWorkspace}
        onPromptCreated={handlePromptCreated}
      />
    );
  }

  return (
    <div className="space-y-6 animate-fade-in">
      <PageHeader
        title="Prompts"
        subtitle="Active prompt versions deployed to each agent via the MLflow Prompt Registry."
        actions={
          <button
            type="button"
            onClick={() => openCreateWorkspace()}
            className="inline-flex items-center gap-1.5 rounded-md bg-caliber-600 px-3 py-2 text-sm font-semibold text-white hover:bg-caliber-700"
          >
            <svg
              className="h-4 w-4"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              aria-hidden="true"
            >
              <path d="M12 5v14M5 12h14" />
            </svg>
            New prompt
          </button>
        }
      />

      {showEdit && editTarget && (
        <PromptModal
          ariaLabelledBy="edit-prompt-dialog-title"
          onClose={() => {
            if (confirmDiscardEditChanges()) closeEditPanel();
          }}
        >
          <div className="max-h-[85vh] overflow-y-auto rounded-2xl border border-blue-200 bg-blue-50/30 p-5">
            <div className="mb-3 flex items-center justify-between">
              <h2
                id="edit-prompt-dialog-title"
                className="text-sm font-semibold text-gray-900"
              >
                Edit Prompt: {editTarget.agent_name}
              </h2>
              <button
                type="button"
                className="text-xs text-gray-500 hover:text-gray-700"
                onClick={() => {
                  if (!confirmDiscardEditChanges()) {
                    return;
                  }
                  closeEditPanel();
                }}
              >
                Cancel
              </button>
            </div>
            <form className="space-y-3" onSubmit={submitEditPrompt}>
              <div>
                <label className="mb-1 block text-xs font-medium text-gray-700">
                  Switch prompt
                </label>
                <select
                  aria-label="Switch prompt"
                  value={editTarget.agent_id}
                  onChange={(e) => {
                    if (!confirmDiscardEditChanges()) {
                      return;
                    }
                    const next = editablePrompts.find(
                      (p) => p.agent_id === e.target.value,
                    );
                    if (next) {
                      void openEditPrompt(next);
                    }
                  }}
                  disabled={loadingEdit || savingEdit}
                  className="w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500 disabled:bg-gray-100"
                >
                  {editablePrompts.map((p) => (
                    <option key={p.agent_id} value={p.agent_id}>
                      {p.agent_name}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-gray-700">
                  Prompt
                </label>
                <input
                  aria-label="Prompt name"
                  value={editTarget.prompt_name ?? editTarget.agent_id}
                  disabled
                  className="w-full rounded-md border border-gray-300 bg-gray-100 px-3 py-2 text-sm text-gray-600"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-gray-700">
                  Template
                </label>
                <textarea
                  value={editTemplate}
                  onChange={(e) => setEditTemplate(e.target.value)}
                  placeholder="Prompt template"
                  rows={8}
                  disabled={loadingEdit || savingEdit}
                  className="w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm font-mono outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500 disabled:bg-gray-100"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-gray-700">
                  Commit message (optional)
                </label>
                <input
                  value={editCommitMessage}
                  onChange={(e) => setEditCommitMessage(e.target.value)}
                  placeholder="Updated prompt"
                  disabled={loadingEdit || savingEdit}
                  className="w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500 disabled:bg-gray-100"
                />
              </div>
              {!SINGLE_ENVIRONMENT && (
                <div>
                  <label className="mb-1 block text-xs font-medium text-gray-700">
                    Save version to alias
                  </label>
                  <select
                    aria-label="Save prompt alias"
                    value={editTargetAlias}
                    onChange={(e) => setEditTargetAlias(e.target.value)}
                    disabled={loadingEdit || savingEdit}
                    className="w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500 disabled:bg-gray-100"
                  >
                    <option value="staging">@staging</option>
                    <option value="prod">@prod</option>
                    <option value="dev">@dev</option>
                  </select>
                  <p className="mt-1 text-[11px] text-blue-700">
                    Save to <span className="font-mono">@staging</span> when you
                    want to calibrate before promoting live.
                  </p>
                </div>
              )}
              {loadingEdit && (
                <div className="rounded-md border border-blue-200 bg-blue-50 px-3 py-2 text-xs text-blue-700">
                  Loading prompt template...
                </div>
              )}
              {editError && (
                <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                  {editError}
                </div>
              )}
              <div className="flex items-center justify-end gap-2">
                <p className="mr-auto text-[11px] text-blue-700">
                  This creates a new version and updates the alias you selected
                  above.
                </p>
                {isAdmin && (
                  <button
                    type="button"
                    onClick={() => void handleDeletePrompt(editTarget)}
                    disabled={loadingEdit || savingEdit || deletingPrompt}
                    className="inline-flex items-center gap-1.5 rounded-md border border-red-200 bg-white px-3 py-2 text-xs font-medium text-red-600 hover:bg-red-50 disabled:opacity-60"
                  >
                    {deletingPrompt ? "Deleting..." : "Delete Prompt"}
                  </button>
                )}
                <button
                  type="submit"
                  disabled={loadingEdit || savingEdit || deletingPrompt}
                  className="inline-flex items-center gap-2 rounded-md bg-caliber-600 px-3 py-2 text-xs font-medium text-white hover:bg-caliber-700 disabled:opacity-60"
                >
                  {savingEdit ? "Saving..." : "Save as New Version"}
                </button>
              </div>
            </form>
          </div>
        </PromptModal>
      )}

      {showVersions && versionsTarget && (
        <PromptModal
          onClose={closeVersionsPanel}
          ariaLabelledBy="prompt-versions-dialog-title"
        >
          <div className="max-h-[85vh] overflow-y-auto rounded-2xl border border-emerald-200 bg-emerald-50/30 p-5">
            <div className="mb-3 flex items-center justify-between">
              <h2
                id="prompt-versions-dialog-title"
                className="text-sm font-semibold text-gray-900"
              >
                Versions: {versionsTarget.agent_name}
              </h2>
              <button
                type="button"
                className="text-xs text-gray-500 hover:text-gray-700"
                onClick={closeVersionsPanel}
              >
                Close
              </button>
            </div>
            {loadingVersions ? (
              <div className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-700">
                Loading prompt versions...
              </div>
            ) : versionsData.length === 0 ? (
              <div className="rounded-md border border-gray-200 bg-white px-3 py-2 text-xs text-gray-600">
                No versions found.
              </div>
            ) : (
              <div className="overflow-x-auto rounded-md border border-gray-200 bg-white">
                <table className="w-full text-xs">
                  <thead className="bg-gray-50 text-gray-600 uppercase tracking-wide">
                    <tr>
                      <th className="px-3 py-2 text-left">Version</th>
                      <th className="px-3 py-2 text-left">Aliases</th>
                      <th className="px-3 py-2 text-left">Commit</th>
                      <th className="px-3 py-2 text-left">Created</th>
                      <th className="px-3 py-2 text-right">Action</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100">
                    {versionsData.map((v) => {
                      const isProd = v.aliases.includes("prod");
                      return (
                        <tr key={v.version}>
                          <td className="px-3 py-2 font-mono text-gray-700">
                            v{v.version}
                          </td>
                          <td className="px-3 py-2">
                            {v.aliases.length > 0 ? (
                              <div className="flex flex-wrap gap-1">
                                {v.aliases.map((alias) => (
                                  <span
                                    key={`${v.version}-${alias}`}
                                    className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${
                                      alias === "prod"
                                        ? "bg-emerald-100 text-emerald-700"
                                        : "bg-gray-100 text-gray-700"
                                    }`}
                                  >
                                    {SINGLE_ENVIRONMENT && alias === LIVE_ALIAS
                                      ? "Live"
                                      : `@${alias}`}
                                  </span>
                                ))}
                              </div>
                            ) : (
                              <span className="text-gray-400">—</span>
                            )}
                          </td>
                          <td className="px-3 py-2 text-gray-600">
                            {v.commit_message ?? "—"}
                          </td>
                          <td className="px-3 py-2 text-gray-500">
                            {v.creation_timestamp
                              ? new Date(v.creation_timestamp).toLocaleString()
                              : "—"}
                          </td>
                          <td className="px-3 py-2 text-right">
                            {isProd ? (
                              <span className="text-[10px] font-medium text-emerald-700">
                                Live
                              </span>
                            ) : (
                              <button
                                type="button"
                                onClick={() => void promoteToProd(v.version)}
                                disabled={promotingVersion === v.version}
                                className="text-[10px] font-medium text-blue-700 hover:underline disabled:opacity-50"
                              >
                                {promotingVersion === v.version
                                  ? SINGLE_ENVIRONMENT
                                    ? "Setting live…"
                                    : "Promoting..."
                                  : SINGLE_ENVIRONMENT
                                    ? "Make live"
                                    : "Promote to @prod"}
                              </button>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}

            {versionsData.length > 0 && (
              <div className="mt-3 rounded-md border border-gray-200 bg-white p-3">
                <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-gray-600">
                  Compare Templates
                </div>
                <div className="mb-3 grid gap-2 md:grid-cols-[1fr_1fr_auto]">
                  <select
                    aria-label="Compare left version"
                    value={compareLeftVersion ?? ""}
                    onChange={(e) =>
                      setCompareLeftVersion(Number(e.target.value))
                    }
                    className="rounded-md border border-gray-300 bg-white px-2 py-1.5 text-xs outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500"
                  >
                    {versionsData.map((v) => (
                      <option key={`left-${v.version}`} value={v.version}>
                        v{v.version}
                        {v.aliases.includes(LIVE_ALIAS)
                          ? SINGLE_ENVIRONMENT
                            ? " (live)"
                            : " @prod"
                          : ""}
                      </option>
                    ))}
                  </select>
                  <select
                    aria-label="Compare right version"
                    value={compareRightVersion ?? ""}
                    onChange={(e) =>
                      setCompareRightVersion(Number(e.target.value))
                    }
                    className="rounded-md border border-gray-300 bg-white px-2 py-1.5 text-xs outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500"
                  >
                    {versionsData.map((v) => (
                      <option key={`right-${v.version}`} value={v.version}>
                        v{v.version}
                        {v.aliases.includes(LIVE_ALIAS)
                          ? SINGLE_ENVIRONMENT
                            ? " (live)"
                            : " @prod"
                          : ""}
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    onClick={() => void runCompare()}
                    disabled={
                      compareLeftVersion == null ||
                      compareRightVersion == null ||
                      loadingCompare
                    }
                    className="rounded-md bg-emerald-600 px-2.5 py-1.5 text-xs font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
                  >
                    {loadingCompare ? "Comparing..." : "Compare"}
                  </button>
                </div>

                {!loadingCompare &&
                  compareLeftVersion != null &&
                  compareRightVersion != null && (
                    <>
                      <div className="mb-2 flex flex-wrap items-center gap-2 text-[11px] text-gray-500">
                        <span>
                          {compareLeftTemplate === compareRightTemplate
                            ? "Selected versions are text-identical."
                            : "Selected versions differ."}
                        </span>
                        {compareLeftTemplate !== compareRightTemplate && (
                          <span className="text-gray-400">
                            (
                            <span className="text-emerald-700">
                              +{compareDiffStats.additions}
                            </span>{" "}
                            <span className="text-red-700">
                              −{compareDiffStats.deletions}
                            </span>{" "}
                            lines)
                          </span>
                        )}
                      </div>
                      {/* Promote either compared version straight from the diff. Labels are
                      version-scoped so they never collide with the table's promote button. */}
                      <div className="mb-2 flex flex-wrap items-center gap-2">
                        <span className="text-[10px] uppercase tracking-wide text-gray-400">
                          v{compareLeftVersion} → v{compareRightVersion}
                        </span>
                        {comparePromoteVersions.map((vn) => {
                          const isLive = versionsData
                            .find((v) => v.version === vn)
                            ?.aliases.includes(LIVE_ALIAS);
                          if (isLive) {
                            return (
                              <span
                                key={`cmp-live-${vn}`}
                                className="text-[10px] font-medium text-emerald-700"
                              >
                                v{vn} {SINGLE_ENVIRONMENT ? "live" : "@prod"}
                              </span>
                            );
                          }
                          return (
                            <button
                              key={`cmp-promote-${vn}`}
                              type="button"
                              onClick={() => void promoteToProd(vn)}
                              disabled={promotingVersion === vn}
                              className="rounded-md border border-blue-200 px-2 py-0.5 text-[10px] font-medium text-blue-700 hover:bg-blue-50 disabled:opacity-50"
                            >
                              {promotingVersion === vn
                                ? SINGLE_ENVIRONMENT
                                  ? `Setting v${vn} live…`
                                  : `Promoting v${vn}…`
                                : SINGLE_ENVIRONMENT
                                  ? `Make v${vn} live`
                                  : `Promote v${vn}`}
                            </button>
                          );
                        })}
                      </div>
                      <pre className="max-h-72 overflow-auto rounded-md border border-gray-200 bg-slate-50 py-1 text-[11px] leading-relaxed text-gray-700">
                        {compareDiff.length === 0 ? (
                          <div className="px-3 py-1 text-gray-400">
                            (empty template)
                          </div>
                        ) : (
                          compareDiff.map((line, i) => (
                            <div
                              key={i}
                              className={`flex ${DIFF_LINE_CLASS[line.op]}`}
                            >
                              <span className="select-none px-2 text-gray-400">
                                {line.op === "insert"
                                  ? "+"
                                  : line.op === "delete"
                                    ? "−"
                                    : " "}
                              </span>
                              <span className="whitespace-pre-wrap break-words pr-2">
                                {line.text === "" ? " " : line.text}
                              </span>
                            </div>
                          ))
                        )}
                      </pre>
                    </>
                  )}

                {compareError && (
                  <div className="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                    {compareError}
                  </div>
                )}
              </div>
            )}

            {versionsError && (
              <div className="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                {versionsError}
              </div>
            )}
          </div>
        </PromptModal>
      )}

      {/* ── inventory ──────────────────────────────────────── */}
      <>
        {error && (
          <div className="mb-4 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
            <div className="font-medium">Failed to load prompts</div>
            <div className="text-xs mt-0.5">{error.message}</div>
          </div>
        )}

        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          {PROMPT_STAT_TILES.map((tile) => (
            <div
              key={tile.key}
              data-testid={`prompt-tile-${tile.key}`}
              className="stat-card"
            >
              <div className="flex items-start justify-between">
                <span
                  className={`grid h-10 w-10 place-items-center rounded-xl ${tile.tone}`}
                >
                  <svg
                    className="w-5 h-5"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.85"
                  >
                    {tile.icon}
                  </svg>
                </span>
              </div>
              <div className="mt-4 text-3xl font-bold tracking-tight text-slate-900">
                {tile.value}
              </div>
              <div className="mt-1 text-sm text-slate-500">{tile.label}</div>
            </div>
          ))}
        </div>

        <FilterBar
          search={
            <SearchInput
              value={promptSearch}
              onChange={setPromptSearch}
              ariaLabel="Search prompts"
              placeholder="Search prompts by agent, name, alias…"
              className="w-full"
            />
          }
          filters={
            <>
              <FilterSelect
                label="State"
                allLabel="All states"
                value={stateFilter}
                onChange={setStateFilter}
                options={[
                  { value: "deployed", label: "Deployed" },
                  { value: "needs", label: "Needs prompt" },
                ]}
                className="w-full sm:w-44"
              />
              <FilterSelect
                label="Source"
                allLabel="All sources"
                value={sourceFilter}
                onChange={setSourceFilter}
                options={sourceOptions}
                className="w-full sm:w-44"
              />
            </>
          }
          actions={
            <>
              <ClearFiltersButton
                visible={hasPromptFilters}
                onClear={() => {
                  setPromptSearch("");
                  setStateFilter("");
                  setSourceFilter("");
                }}
              />
              <ViewToggle value={viewMode} onChange={setViewMode} />
              {isAdmin && deployedCount > 0 && (
                <button
                  type="button"
                  onClick={() => void handleDeleteAllPrompts()}
                  disabled={bulkDeleting || deletingPrompt}
                  className="inline-flex items-center gap-1.5 rounded-md border border-red-200 bg-white px-3 py-2 text-xs font-medium text-red-600 hover:bg-red-50 disabled:opacity-60"
                >
                  <svg
                    className="h-3.5 w-3.5"
                    viewBox="0 0 20 20"
                    fill="currentColor"
                    aria-hidden="true"
                  >
                    <path
                      fillRule="evenodd"
                      d="M8.75 1a1 1 0 0 0-.95.68L7.42 3H4a1 1 0 0 0 0 2h.3l.66 11.06A2 2 0 0 0 6.96 18h6.08a2 2 0 0 0 2-1.94L15.7 5H16a1 1 0 1 0 0-2h-3.42l-.38-1.32A1 1 0 0 0 11.25 1h-2.5ZM8.5 7a.75.75 0 0 1 1.5 0v7a.75.75 0 0 1-1.5 0V7Zm3.5-.75A.75.75 0 0 0 11.25 7v7a.75.75 0 0 0 1.5 0V7a.75.75 0 0 0-.75-.75Z"
                      clipRule="evenodd"
                    />
                  </svg>
                  {bulkDeleting
                    ? "Deleting all…"
                    : `Delete all (${deployedCount})`}
                </button>
              )}
            </>
          }
        />

        {deleteNotice && (
          <div
            role="status"
            className={`flex items-start justify-between gap-3 rounded-md border px-4 py-3 text-xs ${
              deleteNotice.tone === "success"
                ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                : "border-red-200 bg-red-50 text-red-700"
            }`}
          >
            <span>{deleteNotice.message}</span>
            <button
              type="button"
              aria-label="Dismiss notice"
              className="shrink-0 font-medium hover:underline"
              onClick={() => setDeleteNotice(null)}
            >
              Dismiss
            </button>
          </div>
        )}

        {loading && !data && (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
            {[1, 2, 3].map((i) => (
              <div
                key={i}
                className="rounded-2xl border border-slate-200/60 bg-white p-5 shadow-card shimmer h-44"
              />
            ))}
          </div>
        )}

        {data && visiblePromptCount === 0 && (
          <div className="rounded-2xl border-2 border-dashed border-slate-200 bg-gradient-hero px-8 py-12 text-center">
            <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-white shadow-card">
              <svg
                className="h-7 w-7 text-slate-300"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
              >
                <path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" />
              </svg>
            </div>
            <div className="text-sm font-semibold text-slate-600">
              {promptFiltersActive
                ? "No agents match the current filters."
                : "No agents registered yet."}
            </div>
            <div className="mx-auto mt-1.5 max-w-sm text-xs text-slate-400">
              {promptFiltersActive
                ? "Try a different agent, alias, source, or state."
                : "Create the first prompt to start managing agent instructions here."}
            </div>
          </div>
        )}

        {data && visiblePromptCount > 0 && (
          <div className="space-y-6">
            {/* ── Deployed group: prompts live in the registry ───────── */}
            {deployedPrompts.length > 0 && (
              <section data-testid="prompt-group-deployed">
                <div className="mb-3 flex items-center gap-2">
                  <h3 className="text-sm font-semibold text-slate-700">
                    Deployed
                  </h3>
                  <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700 ring-1 ring-emerald-200/50">
                    {deployedPrompts.length}
                  </span>
                </div>
                {viewMode === "list" ? (
                  <ListRows testId="prompt-group-deployed-list">
                    {deployedPrompts.map((prompt) => (
                      <PromptRow
                        key={prompt.agent_id}
                        prompt={prompt}
                        isAdmin={isAdmin}
                        deleting={deletingPrompt}
                        onOpen={() => openPromptWorkspace(prompt)}
                        onEdit={() => void openEditPrompt(prompt)}
                        onVersions={() => void openVersions(prompt)}
                        onCreate={() =>
                          openCreateWorkspace(
                            prompt.prompt_name ?? prompt.agent_id,
                          )
                        }
                        onDelete={() => void handleDeletePrompt(prompt)}
                      />
                    ))}
                  </ListRows>
                ) : (
                  <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
                    {deployedPrompts.map((prompt) => (
                      <PromptCard
                        key={prompt.agent_id}
                        prompt={prompt}
                        isAdmin={isAdmin}
                        deleting={deletingPrompt}
                        onOpen={() => openPromptWorkspace(prompt)}
                        onEdit={() => void openEditPrompt(prompt)}
                        onVersions={() => void openVersions(prompt)}
                        onCreate={() =>
                          openCreateWorkspace(
                            prompt.prompt_name ?? prompt.agent_id,
                          )
                        }
                        onDelete={() => void handleDeletePrompt(prompt)}
                      />
                    ))}
                  </div>
                )}
              </section>
            )}

            {/* ── Needs-prompt group: the backlog of promptless assets ── */}
            {needsPromptRows.length > 0 && (
              <section data-testid="prompt-group-needs-prompt">
                <div className="mb-3 flex items-center gap-2">
                  <h3 className="text-sm font-semibold text-slate-700">
                    Needs prompt
                  </h3>
                  <span className="rounded-full bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700 ring-1 ring-amber-200/50">
                    {needsPromptRows.length}
                  </span>
                </div>
                <p className="mb-3 text-xs text-slate-500">
                  Agents and workflow nodes that still need a prompt authored.
                  Create one to deploy it to the registry.
                </p>
                {viewMode === "list" ? (
                  <ListRows testId="prompt-group-needs-prompt-list">
                    {needsPromptRows.map((prompt) => (
                      <NeedsPromptRow
                        key={prompt.agent_id}
                        prompt={prompt}
                        onCreate={() =>
                          openCreateWorkspace(
                            prompt.prompt_name ?? prompt.agent_id,
                          )
                        }
                      />
                    ))}
                  </ListRows>
                ) : (
                  <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
                    {needsPromptRows.map((prompt) => (
                      <NeedsPromptCard
                        key={prompt.agent_id}
                        prompt={prompt}
                        onCreate={() =>
                          openCreateWorkspace(
                            prompt.prompt_name ?? prompt.agent_id,
                          )
                        }
                      />
                    ))}
                  </div>
                )}
              </section>
            )}
          </div>
        )}
      </>
    </div>
  );
}

/* ── Per-prompt Workspace ─────────────────────────────────────────────── */

/**
 * The focused per-prompt Workspace: a status header plus six stage tabs
 * (Author · Playground · Test Sets · Runs · Calibration · Bind). Every stage
 * reuses the existing stage component scoped to the open prompt — none of them
 * render an internal prompt/agent picker, because the prompt is fixed here.
 *
 * Two modes:
 *  - ``creating``: a brand-new prompt. The Author stage hosts the builder; the
 *    other stages are inert until the prompt exists. On save, the parent flips
 *    into the saved prompt's Workspace.
 *  - ``prompt``: an existing prompt. The header fetches
 *    ``GET /prompts/{name}/workspace`` for model/version/status and refetches
 *    after actions that change it (run / calibration / bind).
 */
function PromptWorkspace({
  prompts,
  loading,
  prompt,
  creating = false,
  createPrefillName = "",
  onBack,
  onPromptCreated,
}: {
  prompts: PromptInfo[];
  loading: boolean;
  prompt?: PromptInfo;
  creating?: boolean;
  createPrefillName?: string;
  onBack: () => void;
  onPromptCreated: (created: PromptCreateResult) => void;
}): JSX.Element {
  const [stage, setStage] = useState<WorkspaceStage>("author");
  const [workspace, setWorkspace] = useState<PromptWorkspaceResponse | null>(
    null,
  );
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  const promptName = prompt ? resolvePromptName(prompt) : null;

  // Fetch (and refetch) the header summary for the open prompt. Create mode has
  // no prompt yet, so it stays on the builder with no summary.
  const refreshWorkspace = useCallback(
    async (signal?: AbortSignal) => {
      if (!promptName) return;
      try {
        const data = await caliberApi.getPromptWorkspace(promptName, signal);
        if (!signal?.aborted) {
          setWorkspace(data);
          setWorkspaceError(null);
        }
      } catch (err) {
        if (!signal?.aborted) {
          setWorkspaceError(
            err instanceof Error ? err.message : "Failed to load workspace",
          );
        }
      }
    },
    [promptName],
  );

  useEffect(() => {
    if (!promptName) return;
    const controller = new AbortController();
    void refreshWorkspace(controller.signal);
    return () => controller.abort();
  }, [promptName, refreshWorkspace]);

  // Create-mode header has no live status yet; show a Draft placeholder.
  const headerName = creating
    ? createPrefillName.trim() || "New prompt"
    : (prompt?.agent_name ?? promptName ?? "Prompt");
  const headerModel = workspace?.model ?? null;
  const headerVersion = workspace?.version ?? prompt?.version ?? null;
  const headerStatus = creating ? "Draft" : (workspace?.status ?? "Draft");

  return (
    <div className="space-y-6 animate-fade-in">
      <div>
        <button
          type="button"
          onClick={onBack}
          className="mb-3 inline-flex items-center gap-1.5 text-sm font-medium text-slate-500 hover:text-slate-700"
        >
          <svg
            className="h-4 w-4"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            aria-hidden="true"
          >
            <path d="M19 12H5M12 19l-7-7 7-7" />
          </svg>
          Back to prompts
        </button>

        {/* ── Status header ── */}
        <div
          data-testid="workspace-header"
          className="flex flex-wrap items-center justify-between gap-4 rounded-2xl border border-slate-200/70 bg-white p-5 shadow-card"
        >
          <div className="flex min-w-0 items-center gap-3">
            <span className="grid h-11 w-11 shrink-0 place-items-center rounded-xl bg-violet-50 text-caliber-purple">
              <MessageSquareText className="h-5 w-5" strokeWidth={1.85} />
            </span>
            <div className="min-w-0">
              <h1 className="truncate text-xl font-bold tracking-tight text-slate-900">
                {headerName}
              </h1>
              <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-slate-500">
                <span>
                  Model:{" "}
                  <span className="font-mono text-slate-700">
                    {headerModel ?? "—"}
                  </span>
                </span>
                <span className="text-slate-300">·</span>
                <span>
                  Version:{" "}
                  <span className="font-mono text-slate-700">
                    {headerVersion != null ? `v${headerVersion}` : "—"}
                  </span>
                </span>
              </div>
            </div>
          </div>
          <PromptStatusBadge status={headerStatus} />
        </div>

        {workspaceError && (
          <div className="mt-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
            {workspaceError}
          </div>
        )}
      </div>

      <PageTabs
        tabs={WORKSPACE_STAGES}
        active={stage}
        onChange={(k) => setStage(k as WorkspaceStage)}
      />

      {/* ── Stage content — each reused component is scoped to the open prompt ── */}
      {stage === "author" &&
        (creating || !prompt ? (
          <PromptBuilder
            prefillName={createPrefillName}
            onCancel={onBack}
            onCreated={(created) => onPromptCreated(created)}
          />
        ) : (
          <PromptAuthorStage
            prompt={prompt}
            onSaved={() => void refreshWorkspace()}
          />
        ))}

      {stage === "playground" && prompt && (
        <PromptChatPlayground
          prompts={prompts}
          loading={loading}
          lockedPrompt={prompt}
        />
      )}

      {stage === "test-sets" && prompt && (
        <PromptTestCases
          prompts={prompts}
          loading={loading}
          lockedPrompt={prompt}
          onDatasetSaved={() => void refreshWorkspace()}
        />
      )}

      {stage === "runs" && prompt && (
        <PromptRunsStage
          prompt={prompt}
          workspace={workspace}
          onAfterRun={() => void refreshWorkspace()}
        />
      )}

      {stage === "calibration" && prompt && (
        <PromptOptimizationTab
          prompts={prompts}
          loading={loading}
          lockedPrompt={prompt}
        />
      )}

      {stage === "bind" && prompt && (
        <PromptBindStage
          prompt={prompt}
          boundTo={workspace?.bound_to ?? null}
          status={workspace?.status ?? null}
          onBound={() => void refreshWorkspace()}
        />
      )}

      {/* In create mode the non-Author stages are inert until the prompt exists. */}
      {stage !== "author" && !prompt && (
        <div className="rounded-2xl border border-dashed border-slate-200 bg-slate-50 px-8 py-12 text-center text-sm text-slate-500">
          Save the prompt on the Author stage to unlock this stage.
        </div>
      )}
    </div>
  );
}

/**
 * Author stage for an existing prompt: load the current template, edit it, and
 * save it as a new version (the create/version flow). The full template-builder
 * fork lives in create mode; this is the focused edit-in-place surface.
 */
function PromptAuthorStage({
  prompt,
  onSaved,
}: {
  prompt: PromptInfo;
  onSaved: () => void;
}): JSX.Element {
  const promptName = resolvePromptName(prompt);
  const initialAlias = prompt.alias || "prod";
  const [template, setTemplate] = useState("");
  const [initialTemplate, setInitialTemplate] = useState("");
  const [commitMessage, setCommitMessage] = useState("");
  const [targetAlias, setTargetAlias] = useState(
    SINGLE_ENVIRONMENT ? LIVE_ALIAS : initialAlias,
  );
  const [loadingTemplate, setLoadingTemplate] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedNote, setSavedNote] = useState<string | null>(null);
  // Bumped after each successful save so the embedded <VersionPanel> re-fetches
  // and shows the version just created (instead of a stale list).
  const [versionRefresh, setVersionRefresh] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoadingTemplate(true);
    setError(null);
    void caliberApi
      .getPrompt(promptName, initialAlias)
      .then((detail) => {
        if (cancelled) return;
        setTemplate(detail.template);
        setInitialTemplate(detail.template);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Failed to load prompt");
      })
      .finally(() => {
        if (!cancelled) setLoadingTemplate(false);
      });
    return () => {
      cancelled = true;
    };
  }, [promptName, initialAlias]);

  // Memoize the adapter so <VersionPanel> doesn't reload-loop on every render
  // (its load effect keys off the adapter identity).
  const versionAdapter = useMemo(
    () => makePromptVersionAdapter(promptName),
    [promptName],
  );

  // Authoring always creates an immutable draft. Save-and-promote then invokes
  // the governed alias endpoint as a separate, durable release operation.
  const save = async (promote: boolean) => {
    if (!template.trim()) {
      setError("Template is required.");
      return;
    }
    setSaving(true);
    setError(null);
    setSavedNote(null);
    try {
      const created = await caliberApi.createPromptVersion(promptName, {
        template,
        commit_message: commitMessage.trim() || undefined,
      });
      if (promote) {
        await caliberApi.promotePrompt(promptName, created.version, {
          alias: targetAlias,
          gate_state: "none",
          overridden: true,
          override_reason: "authoring-panel save and promote",
        });
      }
      setInitialTemplate(template);
      setCommitMessage("");
      setSavedNote(
        promote ? "Saved & promoted a new version." : "Saved a draft version.",
      );
      setVersionRefresh((n) => n + 1);
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save prompt");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="rounded-2xl border border-slate-200/70 bg-white p-5 shadow-card">
      <h2 className="text-sm font-semibold text-slate-900">Author</h2>
      <p className="mt-1 text-xs text-slate-500">
        Edit the prompt template for{" "}
        <span className="font-mono">{promptName}</span> and save it as a new
        version.
      </p>

      <div className="mt-4 space-y-3">
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-700">
            Template
          </label>
          <textarea
            aria-label="Prompt template"
            value={template}
            onChange={(e) => setTemplate(e.target.value)}
            rows={12}
            disabled={loadingTemplate || saving}
            placeholder={
              loadingTemplate ? "Loading template…" : "Prompt template"
            }
            className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 font-mono text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500 disabled:bg-slate-100"
          />
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          <div>
            <label className="mb-1 block text-xs font-medium text-slate-700">
              Commit message (optional)
            </label>
            <input
              aria-label="Commit message"
              value={commitMessage}
              onChange={(e) => setCommitMessage(e.target.value)}
              disabled={loadingTemplate || saving}
              placeholder="Updated prompt"
              className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500 disabled:bg-slate-100"
            />
          </div>
          {!SINGLE_ENVIRONMENT && (
            <div>
              <label className="mb-1 block text-xs font-medium text-slate-700">
                Save version to alias
              </label>
              <select
                aria-label="Save prompt alias"
                value={targetAlias}
                onChange={(e) => setTargetAlias(e.target.value)}
                disabled={loadingTemplate || saving}
                className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500 disabled:bg-slate-100"
              >
                <option value="staging">@staging</option>
                <option value="prod">@prod</option>
                <option value="dev">@dev</option>
              </select>
            </div>
          )}
        </div>

        {error && (
          <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
            {error}
          </div>
        )}
        {savedNote && (
          <div className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-700">
            {savedNote}
          </div>
        )}

        <div className="flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={() => void save(false)}
            disabled={
              loadingTemplate ||
              saving ||
              template === initialTemplate ||
              !template.trim()
            }
            className="rounded-md border border-slate-300 bg-white px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-60"
          >
            {saving ? "Saving…" : "Save draft"}
          </button>
          <button
            type="button"
            onClick={() => void save(true)}
            disabled={
              loadingTemplate ||
              saving ||
              template === initialTemplate ||
              !template.trim()
            }
            className="rounded-md bg-caliber-600 px-3 py-2 text-xs font-semibold text-white hover:bg-caliber-700 disabled:opacity-60"
          >
            {saving ? "Saving…" : "Save & promote"}
          </button>
        </div>
      </div>

      {/* Version history for the open prompt — promote/roll back live here. */}
      <div className="mt-6 border-t border-slate-200/70 pt-4">
        <h3 className="text-sm font-semibold text-slate-900">
          Version history
        </h3>
        <div className="mt-3">
          <VersionPanel adapter={versionAdapter} refreshKey={versionRefresh} />
        </div>
      </div>
    </div>
  );
}

/**
 * Runs stage: a real run-and-compare surface for the open prompt.
 *
 * It runs the current prompt against its **pinned dataset** (or, failing that,
 * the cases stored in the latest saved run) via the shared
 * ``runPromptTestCases`` helper, persists a durable run, and lets the operator
 * pin any run as a **baseline**. When a baseline is set and a different run is
 * viewed, it renders a per-case **output diff** (baseline vs current) and a
 * **regressions** list (cases that were pass/partial in the baseline but fail
 * now), plus the net score delta. The durable run history is the third panel;
 * selecting a run loads its detail and (if a baseline is set) the comparison.
 */
function PromptRunsStage({
  prompt,
  workspace,
  onAfterRun,
}: {
  prompt: PromptInfo;
  workspace: PromptWorkspaceResponse | null;
  onAfterRun: () => void;
}): JSX.Element {
  const agentId = prompt.agent_id;
  const promptName = resolvePromptName(prompt);
  const alias = prompt.alias || "prod";
  const datasetId = workspace?.dataset_id ?? null;
  const baselineRunId = workspace?.baseline_run_id ?? null;

  const { template, loading: templateLoading } = usePromptTemplate(
    prompt,
    alias,
  );

  const [history, setHistory] = useState<PromptTestRunSummary[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(true);

  // The run currently being viewed (its full per-case detail). After a fresh
  // run we view that run; selecting a history row views that one instead.
  const [viewedRunId, setViewedRunId] = useState<string | null>(null);
  const [viewedDetail, setViewedDetail] = useState<PromptTestRunDetail | null>(
    null,
  );
  const [viewedLoading, setViewedLoading] = useState(false);

  // The baseline run's per-case detail, lazily loaded so the diff/regression
  // view can line cases up against it.
  const [baselineDetail, setBaselineDetail] =
    useState<PromptTestRunDetail | null>(null);

  const [running, setRunning] = useState(false);
  const [runProgress, setRunProgress] = useState({ current: 0, total: 0 });
  const [runError, setRunError] = useState<string | null>(null);
  const [pinning, setPinning] = useState(false);
  const [pinError, setPinError] = useState<string | null>(null);

  const refreshHistory = useCallback(
    async (signal?: AbortSignal) => {
      setLoadingHistory(true);
      try {
        const runs = await caliberApi.listPromptTestRuns(
          agentId,
          undefined,
          signal,
        );
        if (!signal?.aborted) setHistory(runs);
      } catch {
        if (!signal?.aborted) setHistory([]);
      } finally {
        if (!signal?.aborted) setLoadingHistory(false);
      }
    },
    [agentId],
  );

  useEffect(() => {
    const controller = new AbortController();
    void refreshHistory(controller.signal);
    return () => controller.abort();
  }, [refreshHistory]);

  // Load the viewed run's full detail whenever the selection changes.
  useEffect(() => {
    if (!viewedRunId) {
      setViewedDetail(null);
      return;
    }
    let cancelled = false;
    setViewedLoading(true);
    void caliberApi
      .getPromptTestRun(viewedRunId)
      .then((detail) => {
        if (!cancelled) setViewedDetail(detail);
      })
      .catch(() => {
        if (!cancelled) setViewedDetail(null);
      })
      .finally(() => {
        if (!cancelled) setViewedLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [viewedRunId]);

  // Lazily load the baseline run's detail (skipped when it is the viewed run).
  useEffect(() => {
    if (!baselineRunId || baselineRunId === viewedRunId) {
      setBaselineDetail(null);
      return;
    }
    let cancelled = false;
    void caliberApi
      .getPromptTestRun(baselineRunId)
      .then((detail) => {
        if (!cancelled) setBaselineDetail(detail);
      })
      .catch(() => {
        if (!cancelled) setBaselineDetail(null);
      });
    return () => {
      cancelled = true;
    };
  }, [baselineRunId, viewedRunId]);

  // Default the viewed run to the latest saved run once history loads.
  useEffect(() => {
    if (viewedRunId || history.length === 0) return;
    setViewedRunId(history[0]!.test_run_id);
  }, [history, viewedRunId]);

  // Source the cases to run: the pinned dataset's examples, falling back to the
  // cases captured in the latest saved run so "Run tests" always has something.
  const loadCases = useCallback(async (): Promise<TestCase[]> => {
    if (datasetId) {
      const examples = await caliberApi.listEvalExamples(datasetId);
      const cases = examples
        .map((ex, i) => {
          const input =
            typeof ex.input.user_message === "string"
              ? ex.input.user_message
              : typeof ex.input.input === "string"
                ? ex.input.input
                : "";
          const expected =
            typeof ex.expected.expected_response === "string"
              ? ex.expected.expected_response
              : typeof ex.expected.behavior === "string"
                ? ex.expected.behavior
                : typeof ex.expected.expected === "string"
                  ? ex.expected.expected
                  : "";
          return {
            id: ex.example_id || `ex-${i}`,
            input,
            expectedBehavior: expected,
            tags: ex.tags ?? [],
          } satisfies TestCase;
        })
        .filter((c) => c.input.trim());
      if (cases.length > 0) return cases;
    }
    // Fallback: reuse the latest run's stored cases.
    if (history.length > 0) {
      const detail = await caliberApi.getPromptTestRun(history[0]!.test_run_id);
      return detail.results.map((r, i) => ({
        id: `run-case-${i}-${r.testCaseId}`,
        input: r.input,
        expectedBehavior: r.expectedBehavior,
        tags: [],
      }));
    }
    return [];
  }, [datasetId, history]);

  const runTests = async () => {
    if (template == null) {
      setRunError("Prompt template is still loading. Try again in a moment.");
      return;
    }
    setRunning(true);
    setRunError(null);
    try {
      const cases = await loadCases();
      if (cases.length === 0) {
        setRunError(
          datasetId
            ? "The pinned test set has no usable cases. Add cases on the Test Sets tab."
            : "No test set is pinned and no previous run exists. Build one on the Test Sets tab first.",
        );
        return;
      }
      setRunProgress({ current: 0, total: cases.length });
      const results = await runPromptTestCases({
        promptName: prompt.agent_name,
        template,
        version: prompt.version,
        alias,
        cases,
        onProgress: (current, total) => setRunProgress({ current, total }),
      });
      const saved = await caliberApi.savePromptTestRun({
        agent_id: agentId,
        prompt_name: promptName,
        prompt_alias: alias,
        prompt_version: prompt.version,
        model: workspace?.model ?? null,
        eval_dataset_id: datasetId,
        results,
      });
      await refreshHistory();
      setViewedRunId(saved.test_run_id);
      // Refresh the workspace header so status flips to Tested.
      onAfterRun();
    } catch (err) {
      setRunError(err instanceof Error ? err.message : "Test run failed");
    } finally {
      setRunning(false);
    }
  };

  const pinAsBaseline = async (testRunId: string) => {
    setPinning(true);
    setPinError(null);
    try {
      await caliberApi.setPromptBaseline(promptName, testRunId);
      onAfterRun();
    } catch (err) {
      setPinError(
        err instanceof Error ? err.message : "Failed to set baseline",
      );
    } finally {
      setPinning(false);
    }
  };

  const viewedIsBaseline = viewedRunId != null && viewedRunId === baselineRunId;
  const showComparison =
    baselineRunId != null &&
    !viewedIsBaseline &&
    viewedDetail != null &&
    baselineDetail != null;

  // Per-case diff + regressions when a baseline exists and another run is viewed.
  const comparison = useMemo(() => {
    if (!showComparison || !viewedDetail || !baselineDetail) return null;
    const baseByCase = new Map(
      baselineDetail.results.map((r) => [r.testCaseId, r]),
    );
    // Cases are aligned by input text when ids differ across runs (a re-run from
    // a dataset mints fresh ids), falling back to the case id.
    const baseByInput = new Map(
      baselineDetail.results.map((r) => [r.input, r]),
    );
    const rows = viewedDetail.results.map((cur) => {
      const base =
        baseByCase.get(cur.testCaseId) ?? baseByInput.get(cur.input) ?? null;
      const regressed =
        base != null &&
        (base.verdict === "pass" || base.verdict === "partial") &&
        cur.verdict === "fail";
      return { cur, base, regressed };
    });
    const regressions = rows.filter((r) => r.regressed);
    const curScore = summarizeResults(viewedDetail.results).overallScore ?? 0;
    const baseScore =
      summarizeResults(baselineDetail.results).overallScore ?? 0;
    return { rows, regressions, scoreDelta: curScore - baseScore };
  }, [showComparison, viewedDetail, baselineDetail]);

  const viewedSummary = viewedDetail
    ? summarizeResults(viewedDetail.results)
    : null;

  return (
    <div className="space-y-4">
      {/* ── Run + compare control bar ── */}
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-caliber-100 bg-caliber-50/60 px-4 py-3">
        <div className="text-sm text-caliber-800">
          Run <span className="font-mono">{promptName}</span> against its{" "}
          {datasetId ? (
            <span className="font-medium">pinned test set</span>
          ) : (
            <span className="font-medium">last run&apos;s cases</span>
          )}
          , then pin a run as the baseline to compare against.
        </div>
        <button
          type="button"
          onClick={() => void runTests()}
          disabled={running || templateLoading}
          className="inline-flex items-center gap-2 rounded-md bg-caliber-600 px-4 py-2 text-sm font-medium text-white hover:bg-caliber-700 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {running ? (
            <>
              <svg
                className="h-4 w-4 animate-spin"
                viewBox="0 0 24 24"
                fill="none"
              >
                <circle
                  className="opacity-25"
                  cx="12"
                  cy="12"
                  r="10"
                  stroke="currentColor"
                  strokeWidth="4"
                />
                <path
                  className="opacity-75"
                  fill="currentColor"
                  d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
                />
              </svg>
              {runProgress.total > 0
                ? `Running ${runProgress.current}/${runProgress.total}…`
                : "Running…"}
            </>
          ) : (
            "Run tests"
          )}
        </button>
      </div>

      {runError && (
        <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          {runError}
        </div>
      )}
      {pinError && (
        <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          {pinError}
        </div>
      )}

      {/* ── Viewed run results + score ── */}
      {viewedRunId && (
        <div
          data-testid="workspace-run-results"
          className="rounded-lg border border-zinc-200 bg-white p-4"
        >
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-semibold text-zinc-700">
                {viewedRunId === history[0]?.test_run_id
                  ? "Latest run"
                  : "Selected run"}
              </h3>
              {viewedIsBaseline ? (
                <span
                  data-testid="run-baseline-marker"
                  className="rounded-full bg-blue-50 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-blue-700 ring-1 ring-blue-200/60"
                >
                  Baseline
                </span>
              ) : (
                <button
                  type="button"
                  onClick={() => void pinAsBaseline(viewedRunId)}
                  disabled={pinning}
                  className="rounded-md border border-blue-200 bg-white px-2.5 py-1 text-[11px] font-medium text-blue-700 hover:bg-blue-50 disabled:opacity-50"
                >
                  {pinning ? "Setting…" : "Set as baseline"}
                </button>
              )}
            </div>
            {viewedSummary && (
              <div className="text-xs text-zinc-500">
                <span className="text-sm font-semibold text-zinc-800">
                  {viewedSummary.overallScore !== null
                    ? `${(viewedSummary.overallScore * 100).toFixed(0)}%`
                    : "—"}
                </span>
                {"  "}
                <span className="font-medium text-emerald-600">
                  {viewedSummary.passCount} pass
                </span>
                {" · "}
                <span className="font-medium text-amber-600">
                  {viewedSummary.partialCount} partial
                </span>
                {" · "}
                <span className="font-medium text-red-600">
                  {viewedSummary.failCount} fail
                </span>
              </div>
            )}
          </div>

          {viewedLoading || viewedDetail == null ? (
            <div className="text-xs text-zinc-400 animate-pulse">
              Loading results…
            </div>
          ) : (
            <div className="space-y-2">
              {viewedDetail.results.map((r, i) => (
                <div
                  key={`${r.testCaseId}-${i}`}
                  className="rounded-md border border-zinc-200 bg-white p-3 text-xs"
                >
                  <div className="mb-2 flex items-center gap-2">
                    <span
                      className={`rounded px-2 py-0.5 font-medium ${
                        r.verdict === "pass"
                          ? "bg-emerald-100 text-emerald-700"
                          : r.verdict === "partial"
                            ? "bg-amber-100 text-amber-700"
                            : "bg-red-100 text-red-700"
                      }`}
                    >
                      {r.verdict}
                    </span>
                    <span className="text-zinc-500">
                      Score {(r.score * 100).toFixed(0)}%
                    </span>
                  </div>
                  <div className="space-y-1 text-zinc-700">
                    <p>
                      <span className="font-medium text-zinc-500">Input:</span>{" "}
                      {r.input}
                    </p>
                    <p>
                      <span className="font-medium text-zinc-500">
                        Expected:
                      </span>{" "}
                      {r.expectedBehavior}
                    </p>
                    <p>
                      <span className="font-medium text-zinc-500">Actual:</span>{" "}
                      {r.actualResponse || "—"}
                    </p>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ── Baseline comparison: net delta + regressions + per-case output diff ── */}
      {showComparison && comparison && (
        <div
          data-testid="workspace-run-comparison"
          className="rounded-lg border border-blue-200 bg-blue-50/40 p-4"
        >
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-sm font-semibold text-blue-900">
              Vs. baseline
            </h3>
            <span
              data-testid="run-score-delta"
              className={`text-xs font-semibold ${
                comparison.scoreDelta > 0
                  ? "text-emerald-700"
                  : comparison.scoreDelta < 0
                    ? "text-red-700"
                    : "text-zinc-600"
              }`}
            >
              Net score {comparison.scoreDelta >= 0 ? "+" : ""}
              {(comparison.scoreDelta * 100).toFixed(0)}%
            </span>
          </div>

          <div data-testid="run-regressions" className="mb-3">
            {comparison.regressions.length === 0 ? (
              <div className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-700">
                No regressions — no case that passed in the baseline now fails.
              </div>
            ) : (
              <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                <span className="font-semibold">
                  {comparison.regressions.length} regression
                  {comparison.regressions.length === 1 ? "" : "s"}
                </span>{" "}
                — cases that were passing/partial in the baseline now fail.
              </div>
            )}
          </div>

          <div className="space-y-2">
            {comparison.rows.map((row, i) => (
              <div
                key={`${row.cur.testCaseId}-${i}`}
                className={`rounded-md border bg-white p-3 text-xs ${
                  row.regressed ? "border-red-300" : "border-zinc-200"
                }`}
              >
                <div className="mb-2 flex items-center gap-2">
                  <span className="text-zinc-500">{row.cur.input}</span>
                  {row.regressed && (
                    <span className="rounded bg-red-100 px-1.5 py-0.5 text-[10px] font-semibold text-red-700">
                      regression
                    </span>
                  )}
                </div>
                <div className="grid gap-3 md:grid-cols-2">
                  <div>
                    <div className="mb-1 flex items-center gap-1.5 text-[11px] font-medium text-zinc-500">
                      Baseline
                      {row.base && (
                        <span
                          className={`rounded px-1.5 py-0.5 text-[10px] ${
                            row.base.verdict === "pass"
                              ? "bg-emerald-100 text-emerald-700"
                              : row.base.verdict === "partial"
                                ? "bg-amber-100 text-amber-700"
                                : "bg-red-100 text-red-700"
                          }`}
                        >
                          {row.base.verdict}
                        </span>
                      )}
                    </div>
                    <pre className="max-h-40 overflow-auto rounded-md border border-zinc-200 bg-slate-50 px-2 py-1.5 text-[11px] text-zinc-700 whitespace-pre-wrap break-words">
                      {row.base?.actualResponse || "(no baseline output)"}
                    </pre>
                  </div>
                  <div>
                    <div className="mb-1 flex items-center gap-1.5 text-[11px] font-medium text-zinc-500">
                      Current
                      <span
                        className={`rounded px-1.5 py-0.5 text-[10px] ${
                          row.cur.verdict === "pass"
                            ? "bg-emerald-100 text-emerald-700"
                            : row.cur.verdict === "partial"
                              ? "bg-amber-100 text-amber-700"
                              : "bg-red-100 text-red-700"
                        }`}
                      >
                        {row.cur.verdict}
                      </span>
                    </div>
                    <pre className="max-h-40 overflow-auto rounded-md border border-zinc-200 bg-slate-50 px-2 py-1.5 text-[11px] text-zinc-700 whitespace-pre-wrap break-words">
                      {row.cur.actualResponse || "(no output)"}
                    </pre>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── Durable run history ── */}
      <div data-testid="workspace-run-history" className="space-y-2">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-zinc-700">Run history</h3>
          <button
            type="button"
            onClick={() => void refreshHistory()}
            className="text-xs font-medium text-caliber-700 hover:underline"
          >
            Refresh
          </button>
        </div>
        {loadingHistory ? (
          <div className="text-xs text-zinc-400 animate-pulse">Loading…</div>
        ) : history.length === 0 ? (
          <div className="rounded-lg border border-dashed border-zinc-300 bg-zinc-50 px-4 py-6 text-center text-xs text-zinc-400">
            No saved runs yet. Run tests above to capture one.
          </div>
        ) : (
          <div className="overflow-hidden rounded-lg border border-zinc-200 bg-white divide-y divide-zinc-100">
            {history.map((run) => {
              const isViewed = viewedRunId === run.test_run_id;
              const isBaseline = baselineRunId === run.test_run_id;
              return (
                <button
                  key={run.test_run_id}
                  type="button"
                  aria-label={`View run ${run.test_run_id}`}
                  onClick={() => setViewedRunId(run.test_run_id)}
                  className={`flex w-full items-center gap-3 px-4 py-3 text-left ${
                    isViewed ? "bg-caliber-50/60" : ""
                  }`}
                >
                  <span className="w-36 shrink-0 text-xs text-zinc-400">
                    {new Date(run.created_at).toLocaleString()}
                  </span>
                  <span className="w-28 shrink-0 text-xs text-zinc-600">
                    v{run.prompt_version ?? "—"} @{run.prompt_alias ?? "—"}
                  </span>
                  <span className="w-14 shrink-0 text-sm font-semibold text-zinc-800">
                    {run.overall_score !== null
                      ? `${(run.overall_score * 100).toFixed(0)}%`
                      : "—"}
                  </span>
                  <span className="text-xs text-zinc-500">
                    <span className="font-medium text-emerald-600">
                      {run.passed_count} pass
                    </span>
                    {" · "}
                    <span className="font-medium text-amber-600">
                      {run.partial_count} partial
                    </span>
                    {" · "}
                    <span className="font-medium text-red-600">
                      {run.failed_count} fail
                    </span>
                    {` · ${run.test_set_size} total`}
                  </span>
                  {isBaseline && (
                    <span className="ml-auto rounded-full bg-blue-50 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-blue-700 ring-1 ring-blue-200/60">
                      Baseline
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

type BindKind = "agent" | "workflow_node" | "standalone";

/** Human-readable label for a recorded binding (read off ``bound_to``). */
function describeBinding(
  boundTo: Record<string, unknown> | null,
): string | null {
  if (!boundTo || typeof boundTo.kind !== "string") return null;
  const kind = boundTo.kind as string;
  if (kind === "agent") {
    return typeof boundTo.agent_id === "string"
      ? `Production agent · ${boundTo.agent_id}`
      : "Production agent";
  }
  if (kind === "workflow_node") {
    const wf =
      typeof boundTo.workflow_id === "string" ? boundTo.workflow_id : "?";
    const node = typeof boundTo.node_id === "string" ? boundTo.node_id : "?";
    return `Workflow node · ${wf} / ${node}`;
  }
  if (kind === "standalone") return "Standalone";
  return kind;
}

/**
 * Bind stage: wire a prompt target to where it actually runs. Three kinds —
 * **Production agent** (picked from the real agent registry), **Workflow node**
 * (a workflow + node id), or **Standalone**. On bind we POST
 * ``/prompts/{name}/bind`` and refetch the workspace so the header status flips
 * to **Bound** and the current-binding panel reflects the new target. Binding
 * is encouraged once the prompt is Tested/Calibrated but never hard-blocked.
 */
function PromptBindStage({
  prompt,
  boundTo,
  status,
  onBound,
}: {
  prompt: PromptInfo;
  boundTo: Record<string, unknown> | null;
  status: string | null;
  onBound: () => void;
}): JSX.Element {
  const promptName = resolvePromptName(prompt);
  const boundKind =
    boundTo && typeof boundTo.kind === "string"
      ? (boundTo.kind as string)
      : null;
  const currentLabel = describeBinding(boundTo);
  const notReady = status === "Draft" || status === "Has test set";

  const [kind, setKind] = useState<BindKind>("agent");
  const [agents, setAgents] = useState<AgentConfig[]>([]);
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [loadingTargets, setLoadingTargets] = useState(false);
  const [selectedAgentId, setSelectedAgentId] = useState("");
  const [selectedWorkflowId, setSelectedWorkflowId] = useState("");
  const [nodeId, setNodeId] = useState("");
  const [binding, setBinding] = useState(false);
  const [bindError, setBindError] = useState<string | null>(null);

  // Load real agents + workflows for the pickers. Hidden prompt-targets are
  // already excluded server-side, so the agent list is the real fleet.
  useEffect(() => {
    let cancelled = false;
    setLoadingTargets(true);
    void Promise.all([
      caliberApi.listAgents().catch(() => [] as AgentConfig[]),
      caliberApi.listWorkflows(undefined).catch(() => [] as Workflow[]),
    ])
      .then(([agentList, workflowList]) => {
        if (cancelled) return;
        setAgents(agentList);
        setWorkflows(workflowList);
        if (agentList.length > 0)
          setSelectedAgentId((cur) => cur || agentList[0]!.agent_id);
        if (workflowList.length > 0)
          setSelectedWorkflowId((cur) => cur || workflowList[0]!.workflow_id);
      })
      .finally(() => {
        if (!cancelled) setLoadingTargets(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const canBind =
    !binding &&
    (kind === "standalone" ||
      (kind === "agent" && Boolean(selectedAgentId)) ||
      (kind === "workflow_node" &&
        Boolean(selectedWorkflowId) &&
        Boolean(nodeId.trim())));

  const submitBind = async () => {
    setBinding(true);
    setBindError(null);
    try {
      let payload: PromptBindPayload;
      if (kind === "agent") {
        payload = { kind: "agent", agent_id: selectedAgentId };
      } else if (kind === "workflow_node") {
        payload = {
          kind: "workflow_node",
          workflow_id: selectedWorkflowId,
          node_id: nodeId.trim(),
        };
      } else {
        payload = { kind: "standalone" };
      }
      await caliberApi.bindPrompt(promptName, payload);
      // Refetch the workspace so the header flips to Bound and the current
      // binding panel reflects the new target.
      onBound();
    } catch (err) {
      setBindError(
        err instanceof Error ? err.message : "Failed to bind prompt",
      );
    } finally {
      setBinding(false);
    }
  };

  const KIND_OPTIONS: Array<{ value: BindKind; label: string; hint: string }> =
    [
      {
        value: "agent",
        label: "Production agent",
        hint: "Point a registered agent at this prompt.",
      },
      {
        value: "workflow_node",
        label: "Workflow node",
        hint: "Wire this prompt to a workflow agent node.",
      },
      {
        value: "standalone",
        label: "Standalone",
        hint: "Run this prompt as its own target.",
      },
    ];

  return (
    <div className="space-y-4">
      <div className="rounded-2xl border border-slate-200/70 bg-white p-5 shadow-card">
        <h2 className="text-sm font-semibold text-slate-900">Bind</h2>
        <p className="mt-1 text-xs text-slate-500">
          Wire the prompt target <span className="font-mono">{promptName}</span>{" "}
          to where it runs.
        </p>

        {/* ── Current binding ── */}
        <div className="mt-4 rounded-xl border border-slate-200 bg-slate-50/60 p-4">
          <div className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            Current binding
          </div>
          {boundKind ? (
            <div
              data-testid="workspace-bound-to"
              className="mt-2 inline-flex items-center gap-1.5 rounded-full bg-emerald-50 px-2.5 py-0.5 text-xs font-medium text-emerald-700 ring-1 ring-emerald-200/60"
            >
              {currentLabel ?? boundKind}
            </div>
          ) : (
            <div className="mt-2 text-sm text-slate-500">
              Not bound yet — this prompt runs as a standalone target.
            </div>
          )}
        </div>

        {/* ── Pick a target ── */}
        <div className="mt-4 space-y-3 rounded-xl border border-slate-200 bg-white p-4">
          {notReady && (
            <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-700">
              Tip: test or calibrate this prompt first so you bind a vetted
              prompt. You can still bind now.
            </div>
          )}

          <div>
            <label className="mb-1 block text-xs font-medium text-slate-600">
              Bind to
            </label>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
              {KIND_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => setKind(opt.value)}
                  className={`rounded-lg border px-3 py-2 text-left text-xs transition-colors ${
                    kind === opt.value
                      ? "border-caliber-500 bg-caliber-50 text-caliber-800 ring-1 ring-caliber-300"
                      : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50"
                  }`}
                >
                  <div className="font-semibold">{opt.label}</div>
                  <div className="mt-0.5 text-[11px] text-slate-400">
                    {opt.hint}
                  </div>
                </button>
              ))}
            </div>
          </div>

          {kind === "agent" && (
            <div>
              <label className="mb-1 block text-xs font-medium text-slate-600">
                Agent
              </label>
              {loadingTargets ? (
                <div className="text-xs text-slate-400 animate-pulse py-2">
                  Loading agents…
                </div>
              ) : agents.length === 0 ? (
                <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-500">
                  No registered agents to bind to yet.
                </div>
              ) : (
                <select
                  aria-label="Select agent to bind"
                  value={selectedAgentId}
                  onChange={(e) => setSelectedAgentId(e.target.value)}
                  className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500"
                >
                  {agents.map((a) => (
                    <option key={a.agent_id} value={a.agent_id}>
                      {a.name} ({a.agent_id})
                    </option>
                  ))}
                </select>
              )}
            </div>
          )}

          {kind === "workflow_node" && (
            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <label className="mb-1 block text-xs font-medium text-slate-600">
                  Workflow
                </label>
                {loadingTargets ? (
                  <div className="text-xs text-slate-400 animate-pulse py-2">
                    Loading workflows…
                  </div>
                ) : workflows.length === 0 ? (
                  <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-500">
                    No workflows available.
                  </div>
                ) : (
                  <select
                    aria-label="Select workflow to bind"
                    value={selectedWorkflowId}
                    onChange={(e) => setSelectedWorkflowId(e.target.value)}
                    className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500"
                  >
                    {workflows.map((w) => (
                      <option key={w.workflow_id} value={w.workflow_id}>
                        {w.name} ({w.workflow_id})
                      </option>
                    ))}
                  </select>
                )}
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-slate-600">
                  Node id
                </label>
                <input
                  aria-label="Workflow node id"
                  value={nodeId}
                  onChange={(e) => setNodeId(e.target.value)}
                  placeholder="e.g. classifier"
                  className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500"
                />
              </div>
            </div>
          )}

          {kind === "standalone" && (
            <p className="text-xs text-slate-500">
              Binding as standalone records that this prompt is intentionally
              run on its own, with no agent or workflow node attached.
            </p>
          )}

          {bindError && (
            <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
              {bindError}
            </div>
          )}

          <div className="flex justify-end">
            <button
              type="button"
              aria-label="Bind prompt target"
              onClick={() => void submitBind()}
              disabled={!canBind}
              className="inline-flex items-center gap-1.5 rounded-md bg-caliber-600 px-4 py-2 text-xs font-medium text-white hover:bg-caliber-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {binding ? "Binding…" : "Bind"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ── Prompt Chat Playground ──────────────────────────────────────────── */

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  timestamp: string;
  fileName?: string;
}

interface AttachedFile {
  name: string;
  content: string;
  size: number;
}

interface PromptIdentitySnapshot {
  agent_id: string;
  agent_name: string;
  prompt_name: string;
  alias: string;
  version: number | null;
  artifact_ref: string;
}

export function resolvePromptName(prompt: PromptInfo): string {
  return prompt.prompt_name ?? prompt.agent_id;
}

/**
 * True when a row represents an actual prompt the user can test / calibrate.
 *
 * A testable prompt has a non-empty ``prompt_name`` — a registered prompt,
 * including a *draft* that exists in the registry but is not yet aliased to prod
 * (so ``has_prompt`` is false). It excludes pure promptless-agent placeholders:
 * a ``needs_prompt`` agent/workflow node that carries no ``prompt_name`` and has
 * no prompt content to run against. The backend auto-provisions a hidden runtime
 * target from the prompt name, so these can all be played/calibrated with no
 * agent-registration step.
 */
export function isTestablePrompt(prompt: PromptInfo): boolean {
  return Boolean(prompt.prompt_name && prompt.prompt_name.trim());
}

/**
 * A minimal ``PromptInfo`` keyed by a prompt name, used to mount a Workspace for
 * a just-created prompt before the inventory re-fetch lands it in the list. The
 * Workspace header fetches its live facts (model/version/status) from the
 * workspace endpoint, so this thin row only needs the identity fields.
 */
export function synthesizePromptInfo(name: string): PromptInfo {
  return {
    agent_id: name,
    agent_name: name,
    agent_enabled: null,
    prompt_name: name,
    version: null,
    alias: "prod",
    available_aliases: [],
    template_preview: null,
    template_length: 0,
    approval_id: null,
    artifact_ref: null,
    has_prompt: true,
    needs_prompt: false,
    source: "caliber",
  };
}

export function resolvePromptRef(prompt: PromptInfo): string {
  const promptName = resolvePromptName(prompt);
  if (prompt.artifact_ref) {
    return prompt.artifact_ref;
  }
  if (prompt.version != null) {
    return `prompts:/${promptName}/${prompt.version}`;
  }
  return `prompts:/${promptName}@${prompt.alias || "prod"}`;
}

export function toPromptIdentitySnapshot(
  prompt: PromptInfo,
): PromptIdentitySnapshot {
  return {
    agent_id: prompt.agent_id,
    agent_name: prompt.agent_name,
    prompt_name: resolvePromptName(prompt),
    alias: prompt.alias || "prod",
    version: prompt.version,
    artifact_ref: resolvePromptRef(prompt),
  };
}

export function getPromptAliasOptions(prompt: PromptInfo | null): string[] {
  if (!prompt) return [];
  const aliases = [...(prompt.available_aliases ?? [])];
  if (prompt.alias && !aliases.includes(prompt.alias)) {
    aliases.push(prompt.alias);
  }
  if (aliases.length === 0) {
    aliases.push("prod");
  }
  return aliases.sort((left, right) => {
    const order = ["prod", "staging", "dev"];
    const leftIndex = order.indexOf(left);
    const rightIndex = order.indexOf(right);
    if (leftIndex === -1 && rightIndex === -1) return left.localeCompare(right);
    if (leftIndex === -1) return 1;
    if (rightIndex === -1) return -1;
    return leftIndex - rightIndex;
  });
}

function defaultEditTargetAlias(prompt: PromptInfo): string {
  // Single-environment mode: every edit saves to the one live alias.
  if (SINGLE_ENVIRONMENT) {
    return LIVE_ALIAS;
  }
  const aliases = getPromptAliasOptions(prompt);
  if (prompt.alias && prompt.alias !== "prod") {
    return prompt.alias;
  }
  if (aliases.includes("staging")) {
    return "staging";
  }
  return prompt.alias || aliases[0] || "prod";
}

function usePromptTemplate(
  prompt: PromptInfo | null,
  alias: string,
): { template: string | null; loading: boolean; error: string | null } {
  const [template, setTemplate] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!prompt?.has_prompt) {
      setTemplate(prompt?.template_preview ?? null);
      setLoading(false);
      setError(null);
      return;
    }

    let cancelled = false;
    setLoading(true);
    setError(null);

    void caliberApi
      .getPrompt(resolvePromptName(prompt), alias || prompt.alias || "prod")
      .then((detail) => {
        if (cancelled) return;
        setTemplate(detail.template);
      })
      .catch((err) => {
        if (cancelled) return;
        setTemplate(prompt.template_preview ?? null);
        setError(
          err instanceof Error ? err.message : "Failed to load prompt template",
        );
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [alias, prompt]);

  return { template, loading, error };
}

const TEXT_EXTENSIONS = new Set([
  "txt",
  "md",
  "csv",
  "json",
  "jsonl",
  "xml",
  "yaml",
  "yml",
  "toml",
  "ini",
  "log",
  "sql",
  "html",
  "htm",
  "css",
  "js",
  "ts",
  "jsx",
  "tsx",
  "py",
  "java",
  "go",
  "rs",
  "rb",
  "php",
  "sh",
  "bash",
  "zsh",
  "c",
  "cpp",
  "h",
  "hpp",
  "cs",
  "swift",
  "kt",
  "scala",
  "r",
  "lua",
  "pl",
  "ex",
  "exs",
  "env",
  "conf",
  "cfg",
  "properties",
  "makefile",
  "dockerfile",
]);

const MAX_FILE_SIZE = 256 * 1024;

export function PromptChatPlayground({
  prompts,
  loading,
  lockedPrompt,
}: {
  prompts: PromptInfo[];
  loading: boolean;
  /**
   * When set, the playground is scoped to this single prompt: the in-tab prompt
   * picker is hidden and this prompt is always the active one. Used by the
   * per-prompt Workspace, where the prompt is fixed by the open workspace.
   */
  lockedPrompt?: PromptInfo;
}): JSX.Element {
  // In locked mode the prompt list collapses to the single open prompt, so the
  // picker, auto-select, and detail panel all operate on a fixed target.
  const effectivePrompts = lockedPrompt ? [lockedPrompt] : prompts;
  const [selectedId, setSelectedId] = useState(lockedPrompt?.agent_id ?? "");
  const selected = lockedPrompt
    ? lockedPrompt
    : (effectivePrompts.find((p) => p.agent_id === selectedId) ?? null);
  const [selectedAlias, setSelectedAlias] = useState("prod");
  const [selectedModel, setSelectedModel] = useState("");
  const [config, setConfig] = useState<AssistantConfig | null>(null);
  const [configLoading, setConfigLoading] = useState(true);
  const aliasOptions = getPromptAliasOptions(selected);
  const {
    template: selectedTemplate,
    loading: templateLoading,
    error: templateError,
  } = usePromptTemplate(selected, selectedAlias);

  // Chat state
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [chatError, setChatError] = useState<string | null>(null);
  const [sessionStarting, setSessionStarting] = useState(false);
  const [sessionPromptSnapshot, setSessionPromptSnapshot] =
    useState<PromptIdentitySnapshot | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [attachedFile, setAttachedFile] = useState<AttachedFile | null>(null);

  // Load assistant config for model list
  useEffect(() => {
    let cancelled = false;
    caliberApi
      .getAssistantConfig()
      .then((c) => {
        if (cancelled) return;
        setConfig(c);
        setSelectedModel(c.model);
        setConfigLoading(false);
      })
      .catch(() => {
        if (!cancelled) setConfigLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Auto-select a prompt — prefer one already deployed, otherwise the first
  // testable prompt (which may be a draft). A locked prompt skips this entirely.
  if (!lockedPrompt && !selectedId && effectivePrompts.length > 0) {
    const first =
      effectivePrompts.find((p) => p.has_prompt) ?? effectivePrompts[0]!;
    setSelectedId(first.agent_id);
  }

  useEffect(() => {
    if (!selected) return;
    const fallbackAlias = selected.alias || aliasOptions[0] || "prod";
    if (!aliasOptions.includes(selectedAlias) || !selectedAlias) {
      setSelectedAlias(fallbackAlias);
    }
  }, [aliasOptions, selected, selectedAlias]);

  // Scroll to bottom on new messages
  useEffect(() => {
    const endNode = messagesEndRef.current;
    if (endNode && typeof endNode.scrollIntoView === "function") {
      endNode.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages]);

  const startSession = async () => {
    if (!selected) return;
    const snapshot = toPromptIdentitySnapshot(selected);
    setSessionStarting(true);
    setChatError(null);
    try {
      // Update model if changed
      if (config && selectedModel !== config.model) {
        const updated = await caliberApi.updateAssistantConfig({
          model: selectedModel,
        });
        setConfig(updated);
      }
      // Create session with prompt template as goal
      const promptContext = [
        `You are an AI assistant. The following is the system prompt you must follow precisely in all your responses.`,
        ``,
        `## Agent: ${snapshot.agent_name}`,
        `## Alias: @${selectedAlias}`,
        ``,
        `### System Prompt:`,
        selectedTemplate ?? "(no prompt template available)",
      ].join("\n");

      const session = await caliberApi.createAssistantSession({
        title: `Prompt Playground: ${snapshot.agent_name}`,
        goal: promptContext,
        metadata_: {
          source: "prompts-playground",
          model: selectedModel,
          prompt_context: {
            ...snapshot,
            alias: selectedAlias || snapshot.alias,
            artifact_ref: `prompts:/${snapshot.prompt_name}@${selectedAlias || snapshot.alias}`,
          },
        },
        artifact_type: "prompt",
      });
      const lockedSnapshot: PromptIdentitySnapshot = {
        ...snapshot,
        alias: selectedAlias || snapshot.alias,
        artifact_ref: `prompts:/${snapshot.prompt_name}@${selectedAlias || snapshot.alias}`,
      };
      setSessionId(session.session_id);
      setSessionPromptSnapshot(lockedSnapshot);
      setMessages([]);
    } catch (err) {
      setChatError(
        err instanceof Error ? err.message : "Failed to start session",
      );
    } finally {
      setSessionStarting(false);
    }
  };

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    e.target.value = "";

    if (file.size > MAX_FILE_SIZE) {
      setChatError(
        `File too large (${(file.size / 1024).toFixed(0)} KB). Maximum is ${MAX_FILE_SIZE / 1024} KB.`,
      );
      return;
    }

    const ext = file.name.split(".").pop()?.toLowerCase() ?? "";
    const isText = TEXT_EXTENSIONS.has(ext) || file.type.startsWith("text/");
    if (!isText) {
      setChatError(
        "Only text-based files are supported (code, CSV, JSON, Markdown, etc.).",
      );
      return;
    }

    const reader = new FileReader();
    reader.onload = () => {
      setAttachedFile({
        name: file.name,
        content: reader.result as string,
        size: file.size,
      });
      setChatError(null);
    };
    reader.onerror = () => setChatError("Failed to read file.");
    reader.readAsText(file);
  };

  const sendMessage = async () => {
    if (!sessionId || !input.trim() || sending) return;
    const userMsg = input.trim();
    const file = attachedFile;
    setInput("");
    setAttachedFile(null);
    setSending(true);
    setChatError(null);

    const displayContent = file ? `📎 ${file.name}\n\n${userMsg}` : userMsg;

    setMessages((prev) => [
      ...prev,
      {
        role: "user",
        content: displayContent,
        timestamp: new Date().toISOString(),
        fileName: file?.name,
      },
    ]);

    const llmContent = file
      ? `The user has uploaded a file named "${file.name}". Here is the file content:\n\n\`\`\`\n${file.content}\n\`\`\`\n\nUser's question: ${userMsg}`
      : userMsg;

    try {
      const turn = await caliberApi.sendAssistantMessage(sessionId, {
        content: llmContent,
        artifact_type: "prompt",
      });
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content: turn.assistant_message.content,
          timestamp: turn.assistant_message.created_at,
        },
      ]);
    } catch (err) {
      setChatError(
        err instanceof Error ? err.message : "Failed to send message",
      );
    } finally {
      setSending(false);
    }
  };

  const resetChat = () => {
    setSessionId(null);
    setMessages([]);
    setChatError(null);
    setAttachedFile(null);
    setSessionPromptSnapshot(null);
  };

  if (loading && effectivePrompts.length === 0) {
    return (
      <div className="text-sm text-zinc-400 animate-pulse py-10 text-center">
        Loading prompts…
      </div>
    );
  }

  if (effectivePrompts.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-zinc-300 dark:border-zinc-600 bg-zinc-50 dark:bg-zinc-800 p-8 text-center">
        <p className="text-sm text-zinc-500 dark:text-zinc-400 mb-2">
          No prompts to test yet.
        </p>
        <p className="text-sm text-zinc-400 dark:text-zinc-500">
          Create a prompt on the Create Prompt tab, then play with it here.
        </p>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
      {/* ── LEFT: prompt picker + model picker ── */}
      <div className="lg:col-span-4 space-y-4">
        {/* The prompt picker is hidden when the prompt is fixed by a Workspace. */}
        {!lockedPrompt && (
          <div>
            <label className="block text-xs font-medium text-zinc-700 dark:text-zinc-300 mb-1">
              Prompt
            </label>
            <select
              aria-label="Select a prompt"
              value={selectedId}
              onChange={(e) => {
                setSelectedId(e.target.value);
                resetChat();
              }}
              disabled={!!sessionId}
              className="w-full rounded-md border border-zinc-300 dark:border-zinc-600 bg-white dark:bg-zinc-800 dark:text-zinc-200 px-3 py-2 text-sm focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500 outline-none disabled:opacity-50 disabled:bg-zinc-50 dark:disabled:bg-zinc-700"
            >
              {effectivePrompts.map((p) => (
                <option key={p.agent_id} value={p.agent_id}>
                  {p.agent_name}
                  {p.has_prompt ? "" : " (draft)"}
                </option>
              ))}
            </select>
          </div>
        )}

        <div>
          <label className="block text-xs font-medium text-zinc-700 dark:text-zinc-300 mb-1">
            LLM Model
          </label>
          {configLoading ? (
            <div className="text-xs text-zinc-400 py-2">Loading models…</div>
          ) : (
            <select
              aria-label="Select model"
              value={selectedModel}
              onChange={(e) => setSelectedModel(e.target.value)}
              disabled={!!sessionId}
              className="w-full rounded-md border border-zinc-300 dark:border-zinc-600 bg-white dark:bg-zinc-800 dark:text-zinc-200 px-3 py-2 text-sm focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500 outline-none disabled:opacity-50 disabled:bg-zinc-50 dark:disabled:bg-zinc-700"
            >
              {config?.available_models.map((m: AssistantModelOption) => (
                <option key={m.id} value={m.id}>
                  {m.name} ({m.provider})
                </option>
              ))}
            </select>
          )}
        </div>

        <div>
          <label className="block text-xs font-medium text-zinc-700 dark:text-zinc-300 mb-1">
            Prompt alias
          </label>
          <select
            aria-label="Select prompt alias"
            value={selectedAlias}
            onChange={(e) => {
              setSelectedAlias(e.target.value);
              resetChat();
            }}
            disabled={!selected || !!sessionId || aliasOptions.length === 0}
            className="w-full rounded-md border border-zinc-300 dark:border-zinc-600 bg-white dark:bg-zinc-800 dark:text-zinc-200 px-3 py-2 text-sm focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500 outline-none disabled:opacity-50 disabled:bg-zinc-50 dark:disabled:bg-zinc-700"
          >
            {aliasOptions.map((alias) => (
              <option key={alias} value={alias}>
                @{alias}
              </option>
            ))}
          </select>
        </div>

        {selected && (
          <div className="rounded-lg border border-zinc-200 dark:border-zinc-700 bg-white dark:bg-zinc-800 p-4 space-y-3">
            <h3 className="text-xs font-semibold text-zinc-800 dark:text-zinc-200 uppercase tracking-wider">
              Prompt Details
            </h3>
            <dl className="space-y-2 text-xs">
              <div className="flex justify-between">
                <dt className="text-zinc-500 dark:text-zinc-400">Prompt</dt>
                <dd className="font-mono text-zinc-800 dark:text-zinc-200">
                  {selected.agent_name}
                </dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-zinc-500 dark:text-zinc-400">Status</dt>
                <dd>
                  {selected.has_prompt ? (
                    <span className="text-emerald-700 font-medium">
                      Deployed
                    </span>
                  ) : (
                    <span className="text-zinc-400">Draft</span>
                  )}
                </dd>
              </div>
              {selected.version != null && (
                <div className="flex justify-between">
                  <dt className="text-zinc-500 dark:text-zinc-400">Version</dt>
                  <dd className="font-mono text-zinc-800 dark:text-zinc-200">
                    v{selected.version}
                  </dd>
                </div>
              )}
              <div className="flex justify-between">
                <dt className="text-zinc-500 dark:text-zinc-400">Alias</dt>
                <dd className="font-mono text-zinc-800 dark:text-zinc-200">
                  @{selectedAlias}
                </dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-zinc-500 dark:text-zinc-400">Length</dt>
                <dd className="text-zinc-800 dark:text-zinc-200">
                  {selected.template_length.toLocaleString()} chars
                </dd>
              </div>
            </dl>
          </div>
        )}

        {/* Prompt template preview (collapsed) */}
        {selected && (selectedTemplate || selected.template_preview) && (
          <details className="rounded-lg border border-zinc-200 dark:border-zinc-700 bg-white dark:bg-zinc-800">
            <summary className="px-4 py-3 text-xs font-semibold text-zinc-800 dark:text-zinc-200 uppercase tracking-wider cursor-pointer hover:bg-zinc-50 dark:hover:bg-zinc-700">
              Prompt Template
            </summary>
            <pre className="px-4 py-3 text-[11px] text-zinc-600 dark:text-zinc-400 font-mono max-h-40 overflow-y-auto whitespace-pre-wrap break-words border-t border-zinc-100 dark:border-zinc-700">
              {selectedTemplate ?? selected.template_preview}
            </pre>
          </details>
        )}

        {templateError && (
          <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
            {templateError}
          </div>
        )}

        {/* Start / Reset session */}
        {!sessionId ? (
          <button
            onClick={startSession}
            disabled={
              sessionStarting || !selected || configLoading || templateLoading
            }
            className="w-full flex items-center justify-center gap-2 rounded-md bg-caliber-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-caliber-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {sessionStarting ? (
              <>
                <svg
                  className="w-4 h-4 animate-spin"
                  viewBox="0 0 24 24"
                  fill="none"
                >
                  <circle
                    className="opacity-25"
                    cx="12"
                    cy="12"
                    r="10"
                    stroke="currentColor"
                    strokeWidth="4"
                  />
                  <path
                    className="opacity-75"
                    fill="currentColor"
                    d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
                  />
                </svg>
                Starting…
              </>
            ) : (
              <>
                <svg
                  className="w-4 h-4"
                  viewBox="0 0 20 20"
                  fill="currentColor"
                >
                  <path
                    fillRule="evenodd"
                    d="M10 18a8 8 0 100-16 8 8 0 000 16zM9.555 7.168A1 1 0 008 8v4a1 1 0 001.555.832l3-2a1 1 0 000-1.664l-3-2z"
                    clipRule="evenodd"
                  />
                </svg>
                Start Chat Session
              </>
            )}
          </button>
        ) : (
          <button
            onClick={resetChat}
            className="w-full flex items-center justify-center gap-2 rounded-md border border-zinc-300 dark:border-zinc-600 bg-white dark:bg-zinc-800 px-4 py-2 text-sm font-medium text-zinc-700 dark:text-zinc-300 hover:bg-zinc-50 dark:hover:bg-zinc-700 transition-colors"
          >
            <svg className="w-4 h-4" viewBox="0 0 20 20" fill="currentColor">
              <path
                fillRule="evenodd"
                d="M4 2a1 1 0 011 1v2.101a7.002 7.002 0 0111.601 2.566 1 1 0 11-1.885.666A5.002 5.002 0 005.999 7H9a1 1 0 010 2H4a1 1 0 01-1-1V3a1 1 0 011-1zm.008 9.057a1 1 0 011.276.61A5.002 5.002 0 0014.001 13H11a1 1 0 110-2h5a1 1 0 011 1v5a1 1 0 11-2 0v-2.101a7.002 7.002 0 01-11.601-2.566 1 1 0 01.61-1.276z"
                clipRule="evenodd"
              />
            </svg>
            New Session
          </button>
        )}
      </div>

      {/* ── RIGHT: chat area ── */}
      <div className="lg:col-span-8 flex flex-col">
        {!sessionId ? (
          <div className="flex-1 flex items-center justify-center rounded-lg border border-dashed border-zinc-300 dark:border-zinc-600 bg-zinc-50 dark:bg-zinc-800/50 min-h-[400px]">
            <div className="text-center px-8">
              <svg
                className="w-12 h-12 text-zinc-300 dark:text-zinc-600 mx-auto mb-3"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
              >
                <path
                  d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              <p className="text-sm font-medium text-zinc-600 dark:text-zinc-300 mb-1">
                Prompt Playground
              </p>
              <p className="text-xs text-zinc-400 dark:text-zinc-500 max-w-sm">
                Select a prompt and LLM model, then start a chat session. The
                model will follow the prompt template in its responses.
              </p>
            </div>
          </div>
        ) : (
          <div className="flex flex-col rounded-lg border border-zinc-200 dark:border-zinc-700 bg-white dark:bg-zinc-800 min-h-[400px] max-h-[600px]">
            {/* Chat header */}
            <div className="px-4 py-3 border-b border-zinc-100 dark:border-zinc-700 bg-zinc-50 dark:bg-zinc-900 rounded-t-lg flex items-center justify-between">
              <div>
                <div className="flex items-center gap-2">
                  <div className="w-2 h-2 rounded-full bg-emerald-500" />
                  <span className="text-xs font-semibold text-zinc-700 dark:text-zinc-200">
                    {sessionPromptSnapshot?.agent_name ??
                      selected?.agent_name ??
                      "Agent"}
                  </span>
                  <span className="text-[10px] text-zinc-400">·</span>
                  <span className="text-[10px] text-zinc-500 dark:text-zinc-400 font-mono">
                    {config?.available_models.find(
                      (m: AssistantModelOption) => m.id === selectedModel,
                    )?.name ?? selectedModel}
                  </span>
                </div>
                {sessionPromptSnapshot && (
                  <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[10px] text-zinc-500 dark:text-zinc-400">
                    <span>
                      Prompt:{" "}
                      <span className="font-mono text-zinc-700 dark:text-zinc-300">
                        {sessionPromptSnapshot.prompt_name}
                      </span>
                    </span>
                    <span>
                      Alias:{" "}
                      <span className="font-mono text-zinc-700 dark:text-zinc-300">
                        @{sessionPromptSnapshot.alias}
                      </span>
                    </span>
                    <span>
                      Version:{" "}
                      <span className="font-mono text-zinc-700 dark:text-zinc-300">
                        {sessionPromptSnapshot.version != null
                          ? `v${sessionPromptSnapshot.version}`
                          : "n/a"}
                      </span>
                    </span>
                    <span>
                      Ref:{" "}
                      <span className="font-mono text-zinc-700 dark:text-zinc-300">
                        {sessionPromptSnapshot.artifact_ref}
                      </span>
                    </span>
                  </div>
                )}
              </div>
              <span className="text-[10px] text-zinc-400">
                {messages.length} messages
              </span>
            </div>

            {/* Messages */}
            <div className="flex-1 overflow-y-auto px-4 py-3 space-y-3">
              {messages.length === 0 && !sending && (
                <div className="text-center py-8">
                  <p className="text-xs text-zinc-400">
                    Session started. Send a message to chat with the agent using
                    the{" "}
                    <strong>
                      {sessionPromptSnapshot?.agent_name ??
                        selected?.agent_name}
                    </strong>{" "}
                    prompt.
                  </p>
                  {sessionPromptSnapshot && (
                    <p
                      className="mt-1 text-[11px] text-zinc-500"
                      data-testid="playground-prompt-ref"
                    >
                      Locked prompt ref:{" "}
                      <span className="font-mono">
                        {sessionPromptSnapshot.artifact_ref}
                      </span>
                    </p>
                  )}
                </div>
              )}
              {messages.map((msg, i) => (
                <div
                  key={i}
                  className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}
                >
                  <div
                    className={`max-w-[80%] rounded-lg px-3 py-2 text-sm ${
                      msg.role === "user"
                        ? "bg-caliber-600 text-white"
                        : "bg-zinc-100 dark:bg-zinc-700 text-zinc-800 dark:text-zinc-200"
                    }`}
                  >
                    <div className="whitespace-pre-wrap break-words">
                      {msg.content}
                    </div>
                    <div
                      className={`text-[10px] mt-1 ${
                        msg.role === "user"
                          ? "text-caliber-200"
                          : "text-zinc-400 dark:text-zinc-500"
                      }`}
                    >
                      {new Date(msg.timestamp).toLocaleTimeString()}
                    </div>
                  </div>
                </div>
              ))}
              {sending && (
                <div className="flex justify-start">
                  <div className="bg-zinc-100 dark:bg-zinc-700 rounded-lg px-3 py-2">
                    <div className="flex items-center gap-1.5">
                      <div className="w-1.5 h-1.5 bg-zinc-400 rounded-full animate-bounce" />
                      <div className="w-1.5 h-1.5 bg-zinc-400 rounded-full animate-bounce delay-150" />
                      <div className="w-1.5 h-1.5 bg-zinc-400 rounded-full animate-bounce delay-300" />
                    </div>
                  </div>
                </div>
              )}
              <div ref={messagesEndRef} />
            </div>

            {/* Error */}
            {chatError && (
              <div className="mx-4 mb-2 rounded-md border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-900/30 px-3 py-2 text-xs text-red-700 dark:text-red-400">
                {chatError}
              </div>
            )}

            {/* Input */}
            <div className="border-t border-zinc-100 dark:border-zinc-700 px-4 py-3">
              {attachedFile && (
                <div className="flex items-center gap-2 mb-2 px-2 py-1.5 rounded-md bg-caliber-50 dark:bg-caliber-900/30 border border-caliber-200 dark:border-caliber-800 text-xs">
                  <svg
                    className="w-3.5 h-3.5 text-caliber-500 flex-shrink-0"
                    viewBox="0 0 20 20"
                    fill="currentColor"
                  >
                    <path
                      fillRule="evenodd"
                      d="M15.621 4.379a3 3 0 00-4.242 0l-7 7a3 3 0 004.241 4.243h.001l.497-.5a.75.75 0 011.064 1.057l-.498.501-.002.002a4.5 4.5 0 01-6.364-6.364l7-7a4.5 4.5 0 016.368 6.36l-3.455 3.553A2.625 2.625 0 119.52 9.52l3.45-3.451a.75.75 0 111.061 1.06l-3.45 3.451a1.125 1.125 0 001.587 1.595l3.454-3.553a3 3 0 000-4.242z"
                      clipRule="evenodd"
                    />
                  </svg>
                  <span className="text-caliber-700 dark:text-caliber-300 font-medium truncate">
                    {attachedFile.name}
                  </span>
                  <span className="text-caliber-400 dark:text-caliber-500">
                    ({(attachedFile.size / 1024).toFixed(1)} KB)
                  </span>
                  <button
                    type="button"
                    onClick={() => setAttachedFile(null)}
                    className="ml-auto text-caliber-400 hover:text-caliber-600 dark:hover:text-caliber-300"
                    title="Remove file"
                  >
                    <svg
                      className="w-3.5 h-3.5"
                      viewBox="0 0 20 20"
                      fill="currentColor"
                    >
                      <path d="M6.28 5.22a.75.75 0 00-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 101.06 1.06L10 11.06l3.72 3.72a.75.75 0 101.06-1.06L11.06 10l3.72-3.72a.75.75 0 00-1.06-1.06L10 8.94 6.28 5.22z" />
                    </svg>
                  </button>
                </div>
              )}
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void sendMessage();
                }}
                className="flex items-center gap-2"
              >
                <input
                  ref={fileInputRef}
                  type="file"
                  className="hidden"
                  onChange={handleFileSelect}
                  aria-label="Attach a file"
                  accept=".txt,.md,.csv,.json,.jsonl,.xml,.yaml,.yml,.toml,.ini,.log,.sql,.html,.htm,.css,.js,.ts,.jsx,.tsx,.py,.java,.go,.rs,.rb,.php,.sh,.bash,.c,.cpp,.h,.hpp,.cs,.swift,.kt,.scala,.r,.lua,.pl,.ex,.exs,.env,.conf,.cfg,.properties"
                />
                <button
                  type="button"
                  title="Attach a file"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={sending}
                  className="rounded-md border border-zinc-300 dark:border-zinc-600 bg-white dark:bg-zinc-900 px-2 py-2 text-zinc-500 dark:text-zinc-400 hover:bg-zinc-50 dark:hover:bg-zinc-800 hover:text-zinc-700 dark:hover:text-zinc-200 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                >
                  <svg
                    className="w-4 h-4"
                    viewBox="0 0 20 20"
                    fill="currentColor"
                  >
                    <path
                      fillRule="evenodd"
                      d="M15.621 4.379a3 3 0 00-4.242 0l-7 7a3 3 0 004.241 4.243h.001l.497-.5a.75.75 0 011.064 1.057l-.498.501-.002.002a4.5 4.5 0 01-6.364-6.364l7-7a4.5 4.5 0 016.368 6.36l-3.455 3.553A2.625 2.625 0 119.52 9.52l3.45-3.451a.75.75 0 111.061 1.06l-3.45 3.451a1.125 1.125 0 001.587 1.595l3.454-3.553a3 3 0 000-4.242z"
                      clipRule="evenodd"
                    />
                  </svg>
                </button>
                <input
                  type="text"
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  placeholder={
                    attachedFile
                      ? "Ask a question about the file…"
                      : "Type a message…"
                  }
                  disabled={sending}
                  className="flex-1 rounded-md border border-zinc-300 dark:border-zinc-600 bg-white dark:bg-zinc-900 dark:text-zinc-200 px-3 py-2 text-sm focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500 outline-none disabled:opacity-50 dark:placeholder-zinc-500"
                  autoFocus
                />
                <button
                  type="submit"
                  title="Send message"
                  disabled={sending || !input.trim()}
                  className="rounded-md bg-caliber-600 px-3 py-2 text-sm font-medium text-white hover:bg-caliber-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                >
                  <svg
                    className="w-4 h-4"
                    viewBox="0 0 20 20"
                    fill="currentColor"
                  >
                    <path d="M10.894 2.553a1 1 0 00-1.788 0l-7 14a1 1 0 001.169 1.409l5-1.429A1 1 0 009 15.571V11a1 1 0 112 0v4.571a1 1 0 00.725.962l5 1.428a1 1 0 001.17-1.408l-7-14z" />
                  </svg>
                </button>
              </form>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/* ── Shared components ────────────────────────────────────────────────── */

/**
 * Backlog card for an asset that has no deployed prompt yet. Shows the
 * agent/node identity (and, for a workflow node, its "Workflow / Node" name)
 * with a primary "Create prompt" CTA that opens the builder prefilled with the
 * agent's name. These rows never appear in playground / calibration selectors.
 */
function NeedsPromptCard({
  prompt,
  onCreate,
}: {
  prompt: PromptInfo;
  onCreate: () => void;
}): JSX.Element {
  return (
    <div
      data-testid={`needs-prompt-card-${prompt.agent_id}`}
      className="group card flex h-full flex-col border-dashed p-5 transition-all duration-300 hover:-translate-y-0.5 hover:shadow-card-hover"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-amber-50 text-amber-600">
            <MessageSquareText className="h-5 w-5" strokeWidth={1.85} />
          </span>
          <div className="min-w-0">
            <span className="block truncate font-medium text-slate-900">
              {prompt.agent_name}
            </span>
            <div className="mt-0.5 flex flex-wrap items-center gap-1.5">
              <span
                title={`Source: ${prompt.source}`}
                className="inline-flex items-center rounded-full bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 ring-1 ring-amber-200/50"
              >
                {prompt.source}
              </span>
              <span className="font-mono text-[10px] text-slate-400">
                {prompt.agent_id}
              </span>
            </div>
          </div>
        </div>
        <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700 ring-1 ring-amber-200/50">
          <span className="h-1.5 w-1.5 rounded-full bg-amber-500" />
          Needs prompt
        </span>
      </div>

      <p className="mt-3 line-clamp-3 text-xs leading-relaxed text-slate-500">
        {prompt.template_preview || "No deployed prompt for this agent yet."}
      </p>

      <div className="mt-auto flex items-center justify-end gap-3 border-t border-slate-100 pt-3">
        <button
          type="button"
          onClick={onCreate}
          className="inline-flex items-center gap-1.5 rounded-md bg-caliber-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-caliber-700"
        >
          <svg
            className="h-3.5 w-3.5"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            aria-hidden="true"
          >
            <path d="M12 5v14M5 12h14" />
          </svg>
          Create prompt
        </button>
      </div>
    </div>
  );
}

function PromptCard({
  prompt,
  isAdmin,
  deleting,
  onOpen,
  onEdit,
  onVersions,
  onCreate,
  onDelete,
}: {
  prompt: PromptInfo;
  isAdmin: boolean;
  deleting: boolean;
  /** Open this prompt's focused Workspace (the card body + "Open" CTA). */
  onOpen: () => void;
  onEdit: () => void;
  onVersions: () => void;
  onCreate: () => void;
  onDelete: () => void;
}): JSX.Element {
  const hasCaliberAgent =
    prompt.source === "caliber" || prompt.source === "both";
  const sourceChipClass =
    prompt.source === "both"
      ? "bg-emerald-50 text-emerald-700 ring-emerald-200/50"
      : prompt.source === "mlflow"
        ? "bg-blue-50 text-blue-700 ring-blue-200/50"
        : "bg-amber-50 text-amber-700 ring-amber-200/50";
  const cardTone = prompt.has_prompt
    ? "bg-emerald-50 text-emerald-600"
    : "bg-slate-100 text-slate-500";
  const promptName = resolvePromptName(prompt);
  return (
    <div
      data-testid={`prompt-card-${prompt.agent_id}`}
      className="group card flex h-full flex-col p-5 transition-all duration-300 hover:-translate-y-0.5 hover:shadow-card-hover"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <span
            className={`grid h-10 w-10 shrink-0 place-items-center rounded-xl ${cardTone}`}
          >
            <MessageSquareText className="h-5 w-5" strokeWidth={1.85} />
          </span>
          <div className="min-w-0">
            <button
              type="button"
              onClick={onOpen}
              className="block max-w-full truncate text-left font-medium text-slate-900 hover:text-caliber-700 hover:underline"
              title="Open this prompt's workspace"
            >
              {prompt.agent_name}
            </button>
            <div className="mt-0.5 flex flex-wrap items-center gap-1.5">
              <span
                title={`Source: ${prompt.source}`}
                className={`inline-flex items-center rounded-full px-1.5 py-0.5 text-[10px] font-medium ring-1 ${sourceChipClass}`}
              >
                {prompt.source}
              </span>
              <span className="font-mono text-[10px] text-slate-400">
                {prompt.agent_id}
              </span>
            </div>
          </div>
        </div>
        {prompt.has_prompt ? (
          <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700 ring-1 ring-emerald-200/50">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
            Deployed
          </span>
        ) : (
          <span className="inline-flex items-center gap-1.5 rounded-full bg-gray-50 px-2 py-0.5 text-xs font-medium text-gray-500 ring-1 ring-gray-200/50">
            <span className="h-1.5 w-1.5 rounded-full bg-gray-400" />
            No prompt
          </span>
        )}
      </div>

      <p className="mt-3 line-clamp-3 text-xs leading-relaxed text-slate-500">
        {prompt.template_preview ||
          (prompt.has_prompt
            ? "No preview available."
            : "No deployed prompt for this agent yet.")}
      </p>

      <div className="mt-3 flex flex-wrap gap-1.5">
        <span className="rounded-md bg-slate-50 px-2 py-0.5 font-mono text-[10px] font-semibold text-slate-500 ring-1 ring-slate-200/50">
          {promptName}
        </span>
        <span className="rounded-md bg-slate-50 px-2 py-0.5 font-mono text-[10px] font-semibold text-slate-500 ring-1 ring-slate-200/50">
          {prompt.version != null ? `v${prompt.version}` : "No version"} · @
          {prompt.alias || "prod"}
        </span>
        {(prompt.available_aliases ?? [])
          .filter((alias) => alias !== prompt.alias)
          .map((alias) => (
            <span
              key={`${prompt.agent_id}-${alias}`}
              className="rounded-md bg-blue-50 px-2 py-0.5 font-mono text-[10px] font-semibold text-blue-700 ring-1 ring-blue-200/60"
            >
              @{alias}
            </span>
          ))}
        {prompt.approval_id ? (
          <span className="inline-flex items-center rounded-md bg-blue-50 px-2 py-0.5 text-[10px] font-semibold text-blue-700 ring-1 ring-blue-200/50">
            Approval linked
          </span>
        ) : (
          <span className="rounded-md bg-slate-50 px-2 py-0.5 text-[10px] font-semibold text-slate-400 ring-1 ring-slate-200/50">
            No approval
          </span>
        )}
      </div>

      <div className="mt-auto flex items-center justify-between gap-3 border-t border-slate-100 pt-3">
        <div className="flex min-w-0 flex-wrap items-center gap-2 text-[11px] text-slate-400">
          {prompt.approval_id ? (
            <span className="font-mono font-medium text-slate-500">
              {prompt.approval_id.slice(0, 8)}…
            </span>
          ) : (
            <span>Approval pending</span>
          )}
          <span className="text-slate-300">·</span>
          <span>{hasCaliberAgent ? "CALIBER agent" : "No agent"}</span>
        </div>
        <div className="inline-flex flex-wrap items-center justify-end gap-3">
          {prompt.has_prompt ? (
            <>
              <button
                type="button"
                onClick={onOpen}
                className="inline-flex items-center gap-1 rounded-md bg-caliber-600 px-2.5 py-1 text-xs font-semibold text-white hover:bg-caliber-700"
              >
                Open
              </button>
              <button
                type="button"
                onClick={onVersions}
                className="text-xs font-medium text-emerald-700 hover:underline"
              >
                Versions
              </button>
              <button
                type="button"
                onClick={onEdit}
                className="inline-flex items-center gap-1 text-xs font-medium text-blue-700 hover:underline"
              >
                <svg
                  className="h-3.5 w-3.5"
                  viewBox="0 0 20 20"
                  fill="currentColor"
                  aria-hidden="true"
                >
                  <path d="M14.69 2.86a1.5 1.5 0 0 1 2.12 2.12l-8.4 8.4a1 1 0 0 1-.45.26l-3 1a.5.5 0 0 1-.63-.63l1-3a1 1 0 0 1 .26-.45l8.4-8.4ZM13.98 4.27l1.75 1.75.38-.38a.5.5 0 0 0-.71-.71l-.38.34ZM13.27 4.98l-6.96 6.96-.58 1.75 1.75-.58 6.96-6.96-1.17-1.17Z" />
                </svg>
                Edit
              </button>
              {isAdmin && (
                <button
                  type="button"
                  onClick={onDelete}
                  disabled={deleting}
                  aria-label={`Delete prompt ${prompt.agent_name}`}
                  className="inline-flex items-center gap-1 text-xs font-medium text-red-600 hover:underline disabled:opacity-50"
                >
                  <svg
                    className="h-3.5 w-3.5"
                    viewBox="0 0 20 20"
                    fill="currentColor"
                    aria-hidden="true"
                  >
                    <path
                      fillRule="evenodd"
                      d="M8.75 1a1 1 0 0 0-.95.68L7.42 3H4a1 1 0 0 0 0 2h.3l.66 11.06A2 2 0 0 0 6.96 18h6.08a2 2 0 0 0 2-1.94L15.7 5H16a1 1 0 1 0 0-2h-3.42l-.38-1.32A1 1 0 0 0 11.25 1h-2.5ZM8.5 7a.75.75 0 0 1 1.5 0v7a.75.75 0 0 1-1.5 0V7Zm3.5-.75A.75.75 0 0 0 11.25 7v7a.75.75 0 0 0 1.5 0V7a.75.75 0 0 0-.75-.75Z"
                      clipRule="evenodd"
                    />
                  </svg>
                  {deleting ? "Deleting…" : "Delete"}
                </button>
              )}
            </>
          ) : prompt.prompt_name ? (
            <button
              type="button"
              onClick={onCreate}
              className="text-xs font-medium text-blue-700 hover:underline"
            >
              Create
            </button>
          ) : (
            <span className="text-xs text-gray-400">No deployed prompt</span>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * PromptRow — the list-view counterpart of {@link PromptCard}. Same item, same
 * handlers, denser presentation: the agent name + source/id, a status pill, the
 * registry name + version/alias columns, and the same Open / Versions / Edit /
 * Delete (or Create) actions. Rendered inside {@link ListRows} when the Prompts
 * inventory is switched to list view.
 */
function PromptRow({
  prompt,
  isAdmin,
  deleting,
  onOpen,
  onEdit,
  onVersions,
  onCreate,
  onDelete,
}: {
  prompt: PromptInfo;
  isAdmin: boolean;
  deleting: boolean;
  onOpen: () => void;
  onEdit: () => void;
  onVersions: () => void;
  onCreate: () => void;
  onDelete: () => void;
}): JSX.Element {
  const cardTone = prompt.has_prompt
    ? "bg-emerald-50 text-emerald-600"
    : "bg-slate-100 text-slate-500";
  const promptName = resolvePromptName(prompt);
  return (
    <ListRow
      testId={`prompt-row-${prompt.agent_id}`}
      title_attr="Open this prompt's workspace"
      onClick={prompt.has_prompt ? onOpen : undefined}
      icon={
        <span
          className={`grid h-9 w-9 place-items-center rounded-xl ${cardTone}`}
        >
          <MessageSquareText className="h-4 w-4" strokeWidth={1.85} />
        </span>
      }
      title={prompt.agent_name}
      subtitle={
        <span className="font-mono">
          {promptName}
          <span className="text-slate-300"> · </span>
          {prompt.source}
        </span>
      }
      columns={
        <>
          {prompt.has_prompt ? (
            <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700 ring-1 ring-emerald-200/50">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
              Deployed
            </span>
          ) : (
            <span className="inline-flex items-center gap-1.5 rounded-full bg-gray-50 px-2 py-0.5 text-xs font-medium text-gray-500 ring-1 ring-gray-200/50">
              <span className="h-1.5 w-1.5 rounded-full bg-gray-400" />
              No prompt
            </span>
          )}
          <span className="w-28 font-mono">
            {prompt.version != null ? `v${prompt.version}` : "No version"} · @
            {prompt.alias || "prod"}
          </span>
          <span className="w-24 truncate">
            {prompt.approval_id ? "Approval linked" : "No approval"}
          </span>
        </>
      }
      actions={
        prompt.has_prompt ? (
          <>
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onOpen();
              }}
              className="inline-flex items-center gap-1 rounded-md bg-caliber-600 px-2.5 py-1 text-xs font-semibold text-white hover:bg-caliber-700"
            >
              Open
            </button>
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onVersions();
              }}
              className="px-1.5 text-xs font-medium text-emerald-700 hover:underline"
            >
              Versions
            </button>
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onEdit();
              }}
              className="px-1.5 text-xs font-medium text-blue-700 hover:underline"
            >
              Edit
            </button>
            {isAdmin && (
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  onDelete();
                }}
                disabled={deleting}
                aria-label={`Delete prompt ${prompt.agent_name}`}
                className="px-1.5 text-xs font-medium text-red-600 hover:underline disabled:opacity-50"
              >
                {deleting ? "Deleting…" : "Delete"}
              </button>
            )}
          </>
        ) : prompt.prompt_name ? (
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onCreate();
            }}
            className="px-1.5 text-xs font-medium text-blue-700 hover:underline"
          >
            Create
          </button>
        ) : (
          <span className="px-1.5 text-xs text-gray-400">
            No deployed prompt
          </span>
        )
      }
    />
  );
}

/** NeedsPromptRow — list-view counterpart of {@link NeedsPromptCard}. */
function NeedsPromptRow({
  prompt,
  onCreate,
}: {
  prompt: PromptInfo;
  onCreate: () => void;
}): JSX.Element {
  return (
    <ListRow
      testId={`needs-prompt-row-${prompt.agent_id}`}
      icon={
        <span className="grid h-9 w-9 place-items-center rounded-xl bg-amber-50 text-amber-600">
          <MessageSquareText className="h-4 w-4" strokeWidth={1.85} />
        </span>
      }
      title={prompt.agent_name}
      subtitle={
        <span className="font-mono">
          {prompt.agent_id}
          <span className="text-slate-300"> · </span>
          {prompt.source}
        </span>
      }
      columns={
        <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700 ring-1 ring-amber-200/50">
          <span className="h-1.5 w-1.5 rounded-full bg-amber-500" />
          Needs prompt
        </span>
      }
      actions={
        <button
          type="button"
          onClick={onCreate}
          className="inline-flex items-center gap-1.5 rounded-md bg-caliber-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-caliber-700"
        >
          <svg
            className="h-3.5 w-3.5"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            aria-hidden="true"
          >
            <path d="M12 5v14M5 12h14" />
          </svg>
          Create prompt
        </button>
      }
    />
  );
}

/* ── Prompt Test Cases ────────────────────────────────────────────────── */

interface TestCase {
  id: string;
  input: string;
  expectedBehavior: string;
  tags: string[];
}

interface TestResult {
  testCaseId: string;
  input: string;
  expectedBehavior: string;
  actualResponse: string;
  verdict: "pass" | "fail" | "partial";
  score: number;
  reasoning: string;
}

/**
 * Run one prompt test set through the assistant + LLM-judge, returning the
 * per-case verdicts. This is the single source of truth for "run a prompt
 * against cases and judge it", shared by both the Test Sets surface
 * (``PromptTestCases``) and the Runs surface (``PromptRunsStage``) so the two
 * never drift. For each case it (1) opens an assistant session seeded with the
 * prompt template, sends the case input, then (2) opens a judge session that
 * scores the response against the expected behavior. Failures per-case are
 * captured as a ``fail`` result rather than aborting the whole run.
 *
 * ``onProgress`` reports ``(current, total)`` before each case and
 * ``onPartial`` streams the accumulated results so the caller can render them
 * live. The promise resolves with the final ``TestResult[]``.
 */
async function runPromptTestCases({
  promptName,
  template,
  version,
  alias,
  cases,
  onProgress,
  onPartial,
}: {
  promptName: string;
  template: string;
  version: number | null;
  alias: string;
  cases: TestCase[];
  onProgress?: (current: number, total: number) => void;
  onPartial?: (results: TestResult[]) => void;
}): Promise<TestResult[]> {
  const promptContext = [
    `You are an AI assistant. Follow the system prompt instructions precisely.`,
    ``,
    `## Agent: ${promptName}`,
    `**Version:** ${version ?? "N/A"}`,
    `**Alias:** @${alias}`,
    ``,
    `### System Prompt:`,
    template,
  ]
    .filter(Boolean)
    .join("\n");

  const results: TestResult[] = [];

  for (let i = 0; i < cases.length; i++) {
    const tc = cases[i]!;
    onProgress?.(i + 1, cases.length);

    try {
      const agentSession = await caliberApi.createAssistantSession({
        title: `Test Run: ${promptName} #${i + 1}`,
        goal: promptContext,
        artifact_type: "prompt",
      });

      const agentTurn = await caliberApi.sendAssistantMessage(
        agentSession.session_id,
        {
          content: tc.input,
          artifact_type: "prompt",
        },
      );

      const actualResponse = agentTurn.assistant_message.content;

      const judgePrompt = [
        `You are an expert evaluator. Judge the following AI response against the expected behavior.`,
        ``,
        `## Test Input`,
        tc.input,
        ``,
        `## Expected Behavior`,
        tc.expectedBehavior,
        ``,
        `## Actual Response`,
        actualResponse,
        ``,
        `Evaluate whether the actual response satisfies the expected behavior.`,
        `Respond with ONLY a JSON object:`,
        `{"verdict": "pass"|"fail"|"partial", "score": 0.0-1.0, "reasoning": "brief explanation"}`,
      ].join("\n");

      const judgeSession = await caliberApi.createAssistantSession({
        title: `Judge: ${promptName} #${i + 1}`,
        goal: judgePrompt,
      });

      const judgeTurn = await caliberApi.sendAssistantMessage(
        judgeSession.session_id,
        {
          content: "Judge the response now.",
        },
      );

      const judgeRaw = judgeTurn.assistant_message.content;
      const judgeMatch = judgeRaw.match(/\{[\s\S]*\}/);
      let verdict: "pass" | "fail" | "partial" = "fail";
      let score = 0;
      let reasoning = "Could not parse judge response";

      if (judgeMatch) {
        try {
          const judgeResult = JSON.parse(judgeMatch[0]) as {
            verdict: string;
            score: number;
            reasoning: string;
          };
          verdict = (
            ["pass", "fail", "partial"].includes(judgeResult.verdict)
              ? judgeResult.verdict
              : "fail"
          ) as "pass" | "fail" | "partial";
          score = Math.max(0, Math.min(1, judgeResult.score));
          reasoning = judgeResult.reasoning;
        } catch {
          reasoning = "Judge response was not valid JSON";
        }
      }

      results.push({
        testCaseId: tc.id,
        input: tc.input,
        expectedBehavior: tc.expectedBehavior,
        actualResponse,
        verdict,
        score,
        reasoning,
      });
    } catch (err) {
      results.push({
        testCaseId: tc.id,
        input: tc.input,
        expectedBehavior: tc.expectedBehavior,
        actualResponse: "",
        verdict: "fail",
        score: 0,
        reasoning: err instanceof Error ? err.message : "Execution error",
      });
    }

    onPartial?.([...results]);
  }

  return results;
}

/** Aggregate verdict counts + mean score for a set of judged results. */
function summarizeResults(results: TestResult[]): {
  passCount: number;
  failCount: number;
  partialCount: number;
  overallScore: number | null;
} {
  return {
    passCount: results.filter((r) => r.verdict === "pass").length,
    failCount: results.filter((r) => r.verdict === "fail").length,
    partialCount: results.filter((r) => r.verdict === "partial").length,
    overallScore:
      results.length > 0
        ? results.reduce((sum, r) => sum + r.score, 0) / results.length
        : null,
  };
}

const MIN_TEST_CASE_COUNT = 1;
const MAX_TEST_CASE_COUNT = 50;

export function clampTestCaseCount(value: number): number {
  if (!Number.isFinite(value)) {
    return MIN_TEST_CASE_COUNT;
  }
  return Math.max(
    MIN_TEST_CASE_COUNT,
    Math.min(MAX_TEST_CASE_COUNT, Math.round(value)),
  );
}

/**
 * Prompt Calibration — one tab, two steps. A single shared agent scope drives
 * both: ① generate/curate a test set, then ② optimize the prompt against it.
 * A test set saved in ① is handed straight to ②'s dataset selection, so there
 * is no manual dataset hunting between the two halves of the loop.
 */
export function PromptCalibrationTab({
  prompts,
  loading,
  agentId,
  onAgentChange,
  promptAlias,
  onPromptAliasChange,
}: {
  prompts: PromptInfo[];
  loading: boolean;
  agentId?: string;
  onAgentChange?: (agentId: string) => void;
  promptAlias?: string;
  onPromptAliasChange?: (alias: string) => void;
}): JSX.Element {
  const agentControlled = agentId !== undefined;
  const [internalAgentId, setInternalAgentId] = useState("");
  const [internalPromptAlias, setInternalPromptAlias] = useState("prod");
  const [handoffDatasetId, setHandoffDatasetId] = useState("");
  const selectedAgentId = agentControlled ? agentId : internalAgentId;
  const selectedPromptAlias = promptAlias ?? internalPromptAlias;

  const setSelectedAgentId = useCallback(
    (nextAgentId: string) => {
      onAgentChange?.(nextAgentId);
      if (!agentControlled) {
        setInternalAgentId(nextAgentId);
      }
    },
    [agentControlled, onAgentChange],
  );

  const setSelectedPromptAlias = useCallback(
    (nextAlias: string) => {
      onPromptAliasChange?.(nextAlias);
      if (promptAlias === undefined) {
        setInternalPromptAlias(nextAlias);
      }
    },
    [onPromptAliasChange, promptAlias],
  );

  // The shared scope defaults to the first deployed prompt, falling back to the
  // first testable prompt (which may be a draft).
  useEffect(() => {
    if (selectedAgentId || prompts.length === 0) return;
    const first = prompts.find((p) => p.has_prompt) ?? prompts[0]!;
    setSelectedAgentId(first.agent_id);
  }, [prompts, selectedAgentId, setSelectedAgentId]);

  const selected = prompts.find((p) => p.agent_id === selectedAgentId) ?? null;
  const aliasOptions = getPromptAliasOptions(selected);

  useEffect(() => {
    if (!selected) return;
    if (!aliasOptions.includes(selectedPromptAlias)) {
      setSelectedPromptAlias(selected.alias || aliasOptions[0] || "prod");
    }
  }, [aliasOptions, selected, selectedPromptAlias, setSelectedPromptAlias]);

  return (
    <div className="space-y-7">
      {prompts.length > 0 && (
        <div className="rounded-2xl border border-slate-200/60 bg-white shadow-card p-5">
          <div className="flex flex-wrap items-end gap-4">
            <div className="min-w-[240px] flex-1">
              <label className="mb-1 block text-xs font-medium text-slate-600">
                Calibrating
              </label>
              <select
                aria-label="Select a prompt"
                value={selectedAgentId}
                onChange={(e) => setSelectedAgentId(e.target.value)}
                className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500"
              >
                {prompts.map((p) => (
                  <option key={p.agent_id} value={p.agent_id}>
                    {p.agent_name}
                    {p.has_prompt ? "" : " (draft)"}
                  </option>
                ))}
              </select>
            </div>
            {selected && (
              <div className="min-w-[180px]">
                <label className="mb-1 block text-xs font-medium text-slate-600">
                  Alias
                </label>
                <select
                  aria-label="Select calibration alias"
                  value={selectedPromptAlias}
                  onChange={(e) => setSelectedPromptAlias(e.target.value)}
                  className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500"
                >
                  {aliasOptions.map((alias) => (
                    <option key={alias} value={alias}>
                      @{alias}
                    </option>
                  ))}
                </select>
              </div>
            )}
            <p className="flex-1 text-xs text-slate-400 leading-relaxed">
              Pick the prompt to improve — a draft works too. The test set you
              build in step&nbsp;① becomes the evaluation data the optimizer in
              step&nbsp;② is scored against.
            </p>
          </div>
        </div>
      )}

      <CalibrationStep
        index={1}
        title="Build a test set"
        description="Generate or curate the cases that define good behavior, then save them as a reusable test set."
      >
        <PromptTestCases
          prompts={prompts}
          loading={loading}
          agentId={selectedAgentId}
          onAgentChange={setSelectedAgentId}
          promptAlias={selectedPromptAlias}
          onPromptAliasChange={setSelectedPromptAlias}
          hideAgentPicker
          onDatasetSaved={setHandoffDatasetId}
        />
      </CalibrationStep>

      <StepConnector />

      <CalibrationStep
        index={2}
        title="Run calibration"
        description="Optimize the prompt against your test set with scorers and a quality gate — manually or assistant-guided."
      >
        <PromptOptimizationTab
          prompts={prompts}
          loading={loading}
          agentId={selectedAgentId}
          onAgentChange={setSelectedAgentId}
          promptAlias={selectedPromptAlias}
          onPromptAliasChange={setSelectedPromptAlias}
          hideAgentPicker
          datasetId={handoffDatasetId}
          onDatasetChange={setHandoffDatasetId}
        />
      </CalibrationStep>
    </div>
  );
}

export function PromptTestCases({
  prompts,
  loading,
  agentId,
  onAgentChange,
  promptAlias,
  onPromptAliasChange,
  hideAgentPicker = false,
  onDatasetSaved,
  lockedPrompt,
}: {
  prompts: PromptInfo[];
  loading: boolean;
  /** When provided, the selected agent is controlled by the parent (shared scope). */
  agentId?: string;
  onAgentChange?: (agentId: string) => void;
  promptAlias?: string;
  onPromptAliasChange?: (alias: string) => void;
  /** Hide the built-in agent picker — the parent renders a single shared one. */
  hideAgentPicker?: boolean;
  /** Fired after cases are saved to a new eval dataset — the handoff into calibration. */
  onDatasetSaved?: (datasetId: string) => void;
  /**
   * When set, the builder is scoped to this single prompt: the picker is hidden
   * and this prompt is always the active one (used by the per-prompt Workspace).
   */
  lockedPrompt?: PromptInfo;
}): JSX.Element {
  // A locked prompt collapses the list to itself, fixes the selection, and
  // forces the picker hidden — the Workspace owns the prompt scope.
  const effectivePrompts = lockedPrompt ? [lockedPrompt] : prompts;
  const pickerHidden = hideAgentPicker || Boolean(lockedPrompt);
  const agentControlled = lockedPrompt !== undefined || agentId !== undefined;
  const [internalId, setInternalId] = useState("");
  const selectedId = lockedPrompt
    ? lockedPrompt.agent_id
    : agentId !== undefined
      ? agentId
      : internalId;
  const setSelectedId = useCallback(
    (id: string) => {
      onAgentChange?.(id);
      if (agentId === undefined) setInternalId(id);
    },
    [agentId, onAgentChange],
  );
  const selected =
    effectivePrompts.find((p) => p.agent_id === selectedId) ?? null;
  const [internalAlias, setInternalAlias] = useState("prod");
  const selectedAlias = promptAlias ?? internalAlias;
  const aliasOptions = getPromptAliasOptions(selected);
  const {
    template: selectedTemplate,
    loading: templateLoading,
    error: templateError,
  } = usePromptTemplate(selected, selectedAlias);
  const setSelectedAlias = useCallback(
    (alias: string) => {
      onPromptAliasChange?.(alias);
      if (promptAlias === undefined) {
        setInternalAlias(alias);
      }
    },
    [onPromptAliasChange, promptAlias],
  );
  const [selectedModel, setSelectedModel] = useState("");
  const [config, setConfig] = useState<AssistantConfig | null>(null);
  const [configLoading, setConfigLoading] = useState(true);

  // Test case state
  const [testCases, setTestCases] = useState<TestCase[]>([]);
  const [generating, setGenerating] = useState(false);
  const [numToGenerate, setNumToGenerate] = useState(5);
  const [genError, setGenError] = useState<string | null>(null);

  // Test run state
  const [results, setResults] = useState<TestResult[]>([]);
  const [running, setRunning] = useState(false);
  const [runProgress, setRunProgress] = useState({ current: 0, total: 0 });
  const [runError, setRunError] = useState<string | null>(null);

  // Save state
  const [saving, setSaving] = useState(false);
  const [savedDatasetId, setSavedDatasetId] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  // Durable run-history state. ``runDatasetId`` tracks the eval dataset the
  // current test cases came from (set on Replay / save) so the auto-save snapshot
  // can record it. ``savedRunId`` drives the "saved" indicator after a run.
  const [runDatasetId, setRunDatasetId] = useState<string | null>(null);
  const [history, setHistory] = useState<PromptTestRunSummary[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [savedRunId, setSavedRunId] = useState<string | null>(null);
  const [runSaveError, setRunSaveError] = useState<string | null>(null);
  const [expandedRunId, setExpandedRunId] = useState<string | null>(null);
  const [runDetail, setRunDetail] = useState<PromptTestRunDetail | null>(null);
  const [runDetailLoading, setRunDetailLoading] = useState(false);
  const [replaying, setReplaying] = useState(false);

  const setNumToGenerateClamped = useCallback((value: number) => {
    setNumToGenerate(clampTestCaseCount(value));
  }, []);

  useEffect(() => {
    let cancelled = false;
    caliberApi
      .getAssistantConfig()
      .then((c) => {
        if (cancelled) return;
        setConfig(c);
        setSelectedModel(c.model);
        setConfigLoading(false);
      })
      .catch(() => {
        if (!cancelled) setConfigLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // When uncontrolled, default to the first agent with a prompt. When the
  // parent controls the agent (shared scope) or a prompt is locked, it owns the
  // default instead.
  if (!agentControlled && !selectedId && effectivePrompts.length > 0) {
    const first =
      effectivePrompts.find((p) => p.has_prompt) ?? effectivePrompts[0]!;
    setSelectedId(first.agent_id);
  }

  useEffect(() => {
    if (!selected) return;
    if (!aliasOptions.includes(selectedAlias)) {
      setSelectedAlias(selected.alias || aliasOptions[0] || "prod");
    }
  }, [aliasOptions, selected, selectedAlias, setSelectedAlias]);

  // Reset generated cases whenever the agent changes (from either the built-in
  // picker or the shared parent scope), so cases never bleed across agents.
  const prevAgentRef = useRef(selectedId);
  useEffect(() => {
    if (prevAgentRef.current === selectedId) return;
    prevAgentRef.current = selectedId;
    setTestCases([]);
    setResults([]);
    setSavedDatasetId(null);
    setRunDatasetId(null);
    setSavedRunId(null);
    setRunSaveError(null);
    setExpandedRunId(null);
    setRunDetail(null);
  }, [selectedId]);

  // Load durable run history for the selected agent.
  const refreshHistory = useCallback(
    async (agentId: string, signal?: AbortSignal) => {
      if (!agentId) {
        setHistory([]);
        return;
      }
      setHistoryLoading(true);
      try {
        const runs = await caliberApi.listPromptTestRuns(
          agentId,
          undefined,
          signal,
        );
        if (!signal?.aborted) setHistory(runs);
      } catch {
        if (!signal?.aborted) setHistory([]);
      } finally {
        if (!signal?.aborted) setHistoryLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    if (!selectedId) {
      setHistory([]);
      return;
    }
    const controller = new AbortController();
    void refreshHistory(selectedId, controller.signal);
    return () => controller.abort();
  }, [selectedId, refreshHistory]);

  const generateTestCases = async () => {
    if (!selected) return;
    if (selectedTemplate == null) {
      setGenError("Prompt template is still loading. Try again in a moment.");
      return;
    }
    setGenerating(true);
    setGenError(null);
    try {
      const requestedCaseCount = clampTestCaseCount(numToGenerate);

      if (config && selectedModel !== config.model) {
        const updated = await caliberApi.updateAssistantConfig({
          model: selectedModel,
        });
        setConfig(updated);
      }

      const prompt = [
        `Generate exactly ${requestedCaseCount} test cases for testing the following agent prompt.`,
        `Each test case should validate a different aspect or edge case of the prompt instructions.`,
        ``,
        `## Agent: ${selected.agent_name}`,
        `**Prompt Version:** ${selected.version ?? "N/A"}`,
        `**Prompt Alias:** @${selectedAlias}`,
        ``,
        `### System Prompt:`,
        selectedTemplate,
        ``,
        `Respond with ONLY a valid JSON array. Each element must have:`,
        `- "input": the user message/question to test`,
        `- "expectedBehavior": what a correct response should contain or do`,
        `- "tags": array of 1-2 short tags categorizing the test (e.g. "edge-case", "basic", "error-handling")`,
        ``,
        `Example format:`,
        `[{"input":"...", "expectedBehavior":"...", "tags":["basic"]}]`,
      ]
        .filter(Boolean)
        .join("\n");

      const session = await caliberApi.createAssistantSession({
        title: `Test Gen: ${selected.agent_name}`,
        goal: prompt,
        artifact_type: "prompt",
      });

      const turn = await caliberApi.sendAssistantMessage(session.session_id, {
        content: "Generate the test cases now.",
      });

      const raw = turn.assistant_message.content;
      const jsonMatch = raw.match(/\[[\s\S]*\]/);
      if (!jsonMatch) {
        throw new Error("LLM did not return a valid JSON array. Try again.");
      }
      const parsed = JSON.parse(jsonMatch[0]) as Array<{
        input: string;
        expectedBehavior: string;
        tags?: string[];
      }>;

      const cases: TestCase[] = parsed.map((tc, i) => ({
        id: `tc-${Date.now()}-${i}`,
        input: tc.input,
        expectedBehavior: tc.expectedBehavior,
        tags: tc.tags ?? [],
      }));

      setTestCases(cases);
      setResults([]);
      setSavedDatasetId(null);
    } catch (err) {
      setGenError(
        err instanceof Error ? err.message : "Failed to generate test cases",
      );
    } finally {
      setGenerating(false);
    }
  };

  const runTests = async () => {
    if (!selected || testCases.length === 0) return;
    if (selectedTemplate == null) {
      setRunError("Prompt template is still loading. Try again in a moment.");
      return;
    }
    setRunning(true);
    setRunError(null);
    setResults([]);
    setRunProgress({ current: 0, total: testCases.length });

    try {
      if (config && selectedModel !== config.model) {
        const updated = await caliberApi.updateAssistantConfig({
          model: selectedModel,
        });
        setConfig(updated);
      }

      // Delegate the per-case assistant+judge loop to the shared helper so the
      // Test Sets and Runs surfaces stay in lock-step.
      const newResults = await runPromptTestCases({
        promptName: selected.agent_name,
        template: selectedTemplate,
        version: selected.version,
        alias: selectedAlias,
        cases: testCases,
        onProgress: (current, total) => setRunProgress({ current, total }),
        onPartial: (partial) => setResults(partial),
      });

      // Auto-persist the completed run so it survives a refresh. The save is
      // non-blocking for the UI: failures surface as a non-fatal inline note
      // rather than discarding the just-finished results.
      if (newResults.length > 0) {
        setSavedRunId(null);
        setRunSaveError(null);
        try {
          const saved = await caliberApi.savePromptTestRun({
            agent_id: selected.agent_id,
            prompt_name: selected.prompt_name ?? selected.agent_id,
            prompt_alias: selectedAlias,
            prompt_version: selected.version,
            model: selectedModel || null,
            eval_dataset_id: runDatasetId,
            results: newResults,
          });
          setSavedRunId(saved.test_run_id);
          void refreshHistory(selected.agent_id);
        } catch (err) {
          setRunSaveError(
            err instanceof Error ? err.message : "Failed to save run history",
          );
        }
      }
    } catch (err) {
      setRunError(err instanceof Error ? err.message : "Test run failed");
    } finally {
      setRunning(false);
    }
  };

  const replayRun = async (run: PromptTestRunSummary) => {
    setReplaying(true);
    setRunSaveError(null);
    setSavedRunId(null);
    try {
      const detail = await caliberApi.getPromptTestRun(run.test_run_id);
      // Load the stored cases back into the runner's test-set state so the user
      // can re-run them (which then auto-saves a fresh run via runTests).
      const cases: TestCase[] = detail.results.map((r, i) => ({
        id: `replay-${run.test_run_id}-${i}-${r.testCaseId}`,
        input: r.input,
        expectedBehavior: r.expectedBehavior,
        tags: [],
      }));
      setTestCases(cases);
      setResults([]);
      setSavedDatasetId(null);
      setRunDatasetId(detail.eval_dataset_id);
      setExpandedRunId(null);
      setRunDetail(null);
    } catch (err) {
      setRunSaveError(
        err instanceof Error ? err.message : "Failed to load run for replay",
      );
    } finally {
      setReplaying(false);
    }
  };

  const toggleRunDetail = async (testRunId: string) => {
    if (expandedRunId === testRunId) {
      setExpandedRunId(null);
      return;
    }
    setExpandedRunId(testRunId);
    setRunDetail(null);
    setRunDetailLoading(true);
    try {
      const detail = await caliberApi.getPromptTestRun(testRunId);
      setRunDetail(detail);
    } catch {
      setRunDetail(null);
    } finally {
      setRunDetailLoading(false);
    }
  };

  const saveToEvalDataset = async () => {
    if (!selected || testCases.length === 0) return;
    setSaving(true);
    setSaveError(null);
    try {
      const dataset = await caliberApi.createEvalDataset({
        name: `Prompt Test: ${selected.agent_name} (${new Date().toLocaleDateString()})`,
        description: `Auto-generated test cases for agent prompt "${selected.agent_name}"`,
        owner: "@local-admin",
        tags: ["prompt-test", "auto-generated"],
      });

      for (const tc of testCases) {
        const result = results.find((r) => r.testCaseId === tc.id);
        await caliberApi.appendEvalExample(dataset.dataset_id, {
          input: { user_message: tc.input },
          expected: {
            expected_response: tc.expectedBehavior,
            ...(result
              ? { last_score: result.score, last_verdict: result.verdict }
              : {}),
          },
          tags: tc.tags,
        });
      }

      setSavedDatasetId(dataset.dataset_id);
      // Record the dataset origin so a subsequent run's auto-save snapshot links
      // it back to the dataset these cases came from.
      setRunDatasetId(dataset.dataset_id);
      onDatasetSaved?.(dataset.dataset_id);
    } catch (err) {
      setSaveError(
        err instanceof Error ? err.message : "Failed to save test cases",
      );
    } finally {
      setSaving(false);
    }
  };

  const removeTestCase = (id: string) => {
    setTestCases((prev) => prev.filter((tc) => tc.id !== id));
    setResults((prev) => prev.filter((r) => r.testCaseId !== id));
  };

  const overallScore =
    results.length > 0
      ? results.reduce((sum, r) => sum + r.score, 0) / results.length
      : null;

  const passCount = results.filter((r) => r.verdict === "pass").length;
  const failCount = results.filter((r) => r.verdict === "fail").length;
  const partialCount = results.filter((r) => r.verdict === "partial").length;

  if (loading && effectivePrompts.length === 0) {
    return (
      <div className="text-sm text-zinc-400 animate-pulse py-10 text-center">
        Loading prompts…
      </div>
    );
  }

  if (effectivePrompts.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-zinc-300 bg-zinc-50 p-8 text-center">
        <p className="text-sm text-zinc-500 mb-2">No prompts to test yet.</p>
        <p className="text-sm text-zinc-400">
          Create a prompt on the Create Prompt tab to build a test set for it
          here.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* ── Config: prompt + model pickers ── */}
      <div
        className={`grid grid-cols-1 gap-4 ${pickerHidden ? "md:grid-cols-3" : "md:grid-cols-4"}`}
      >
        {!pickerHidden && (
          <div>
            <label className="block text-xs font-medium text-zinc-700 mb-1">
              Prompt
            </label>
            <select
              aria-label="Select a prompt"
              value={selectedId}
              onChange={(e) => setSelectedId(e.target.value)}
              disabled={generating || running}
              className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500 outline-none disabled:opacity-50 disabled:bg-zinc-50"
            >
              {effectivePrompts.map((p) => (
                <option key={p.agent_id} value={p.agent_id}>
                  {p.agent_name}
                  {p.has_prompt ? "" : " (draft)"}
                </option>
              ))}
            </select>
          </div>
        )}

        <div>
          <label className="block text-xs font-medium text-zinc-700 mb-1">
            LLM Model
          </label>
          {configLoading ? (
            <div className="text-xs text-zinc-400 py-2">Loading models…</div>
          ) : (
            <select
              aria-label="Select model"
              value={selectedModel}
              onChange={(e) => setSelectedModel(e.target.value)}
              disabled={generating || running}
              className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500 outline-none disabled:opacity-50 disabled:bg-zinc-50"
            >
              {config?.available_models.map((m: AssistantModelOption) => (
                <option key={m.id} value={m.id}>
                  {m.name} ({m.provider})
                </option>
              ))}
            </select>
          )}
        </div>

        <div>
          <label className="block text-xs font-medium text-zinc-700 mb-1">
            Prompt Alias
          </label>
          <select
            aria-label="Select prompt alias"
            value={selectedAlias}
            onChange={(e) => setSelectedAlias(e.target.value)}
            disabled={generating || running || aliasOptions.length === 0}
            className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500 outline-none disabled:opacity-50 disabled:bg-zinc-50"
          >
            {aliasOptions.map((alias) => (
              <option key={alias} value={alias}>
                @{alias}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className="block text-xs font-medium text-zinc-700 mb-1">
            Number of Test Cases
          </label>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <button
                type="button"
                aria-label="Decrease test case count"
                onClick={() => setNumToGenerateClamped(numToGenerate - 1)}
                disabled={
                  generating || running || numToGenerate <= MIN_TEST_CASE_COUNT
                }
                className="rounded-md border border-zinc-300 bg-white px-2.5 py-2 text-sm font-semibold text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                -
              </button>
              <input
                type="number"
                min={MIN_TEST_CASE_COUNT}
                max={MAX_TEST_CASE_COUNT}
                step={1}
                aria-label="Number of test cases"
                value={numToGenerate}
                onChange={(e) =>
                  setNumToGenerateClamped(Number(e.target.value))
                }
                disabled={generating || running}
                className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500 outline-none disabled:opacity-50 disabled:bg-zinc-50"
              />
              <button
                type="button"
                aria-label="Increase test case count"
                onClick={() => setNumToGenerateClamped(numToGenerate + 1)}
                disabled={
                  generating || running || numToGenerate >= MAX_TEST_CASE_COUNT
                }
                className="rounded-md border border-zinc-300 bg-white px-2.5 py-2 text-sm font-semibold text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                +
              </button>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {[3, 5, 8, 10, 15, 20, 30].map((n) => (
                <button
                  key={n}
                  type="button"
                  onClick={() => setNumToGenerateClamped(n)}
                  disabled={generating || running}
                  className={`rounded px-2 py-1 text-[11px] font-medium transition-colors ${
                    numToGenerate === n
                      ? "bg-caliber-600 text-white"
                      : "border border-zinc-300 bg-white text-zinc-700 hover:bg-zinc-50"
                  } disabled:opacity-50 disabled:cursor-not-allowed`}
                >
                  {n}
                </button>
              ))}
            </div>
            <p className="text-[11px] text-zinc-500">
              Choose any value from {MIN_TEST_CASE_COUNT} to{" "}
              {MAX_TEST_CASE_COUNT}.
            </p>
          </div>
        </div>
      </div>

      {templateError && (
        <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
          {templateError}
        </div>
      )}

      {/* ── Actions ── */}
      <div className="flex items-center gap-3 flex-wrap">
        <button
          onClick={() => void generateTestCases()}
          disabled={
            generating ||
            running ||
            !selected ||
            configLoading ||
            templateLoading
          }
          className="flex items-center gap-2 rounded-md bg-caliber-600 px-4 py-2 text-sm font-medium text-white hover:bg-caliber-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {generating ? (
            <>
              <svg
                className="w-4 h-4 animate-spin"
                viewBox="0 0 24 24"
                fill="none"
              >
                <circle
                  className="opacity-25"
                  cx="12"
                  cy="12"
                  r="10"
                  stroke="currentColor"
                  strokeWidth="4"
                />
                <path
                  className="opacity-75"
                  fill="currentColor"
                  d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
                />
              </svg>
              Generating…
            </>
          ) : (
            <>
              <svg className="w-4 h-4" viewBox="0 0 20 20" fill="currentColor">
                <path
                  fillRule="evenodd"
                  d="M10 3a1 1 0 011 1v5h5a1 1 0 110 2h-5v5a1 1 0 11-2 0v-5H4a1 1 0 110-2h5V4a1 1 0 011-1z"
                  clipRule="evenodd"
                />
              </svg>
              Generate Test Cases
            </>
          )}
        </button>

        {testCases.length > 0 && (
          <>
            <button
              onClick={() => void runTests()}
              disabled={
                generating ||
                running ||
                testCases.length === 0 ||
                templateLoading
              }
              className="flex items-center gap-2 rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {running ? (
                <>
                  <svg
                    className="w-4 h-4 animate-spin"
                    viewBox="0 0 24 24"
                    fill="none"
                  >
                    <circle
                      className="opacity-25"
                      cx="12"
                      cy="12"
                      r="10"
                      stroke="currentColor"
                      strokeWidth="4"
                    />
                    <path
                      className="opacity-75"
                      fill="currentColor"
                      d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
                    />
                  </svg>
                  Running {runProgress.current}/{runProgress.total}…
                </>
              ) : (
                <>
                  <svg
                    className="w-4 h-4"
                    viewBox="0 0 20 20"
                    fill="currentColor"
                  >
                    <path
                      fillRule="evenodd"
                      d="M10 18a8 8 0 100-16 8 8 0 000 16zM9.555 7.168A1 1 0 008 8v4a1 1 0 001.555.832l3-2a1 1 0 000-1.664l-3-2z"
                      clipRule="evenodd"
                    />
                  </svg>
                  Run Tests &amp; Judge
                </>
              )}
            </button>

            <button
              onClick={() => void saveToEvalDataset()}
              disabled={saving || generating || running}
              className="flex items-center gap-2 rounded-md border border-zinc-300 bg-white px-4 py-2 text-sm font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {saving ? (
                <>
                  <svg
                    className="w-4 h-4 animate-spin"
                    viewBox="0 0 24 24"
                    fill="none"
                  >
                    <circle
                      className="opacity-25"
                      cx="12"
                      cy="12"
                      r="10"
                      stroke="currentColor"
                      strokeWidth="4"
                    />
                    <path
                      className="opacity-75"
                      fill="currentColor"
                      d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
                    />
                  </svg>
                  Saving…
                </>
              ) : (
                <>
                  <svg
                    className="w-4 h-4"
                    viewBox="0 0 20 20"
                    fill="currentColor"
                  >
                    <path d="M2 6a2 2 0 012-2h5l2 2h5a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V6z" />
                  </svg>
                  Save to Test Sets
                </>
              )}
            </button>
          </>
        )}
      </div>

      {/* ── Errors / Success ── */}
      {genError && (
        <div className="rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {genError}
        </div>
      )}
      {runError && (
        <div className="rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {runError}
        </div>
      )}
      {saveError && (
        <div className="rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {saveError}
        </div>
      )}
      {savedDatasetId && (
        <div className="rounded-md border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-700 flex items-center gap-2">
          <svg className="w-4 h-4" viewBox="0 0 20 20" fill="currentColor">
            <path
              fillRule="evenodd"
              d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z"
              clipRule="evenodd"
            />
          </svg>
          Saved to Test Sets.{" "}
          <Link
            to="/eval-datasets"
            className="underline font-medium hover:text-emerald-800"
          >
            View Test Sets →
          </Link>
        </div>
      )}
      {savedRunId && !running && (
        <div
          data-testid="run-saved-indicator"
          className="rounded-md border border-emerald-200 bg-emerald-50 px-4 py-2 text-xs text-emerald-700 flex items-center gap-2"
        >
          <svg className="w-3.5 h-3.5" viewBox="0 0 20 20" fill="currentColor">
            <path
              fillRule="evenodd"
              d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z"
              clipRule="evenodd"
            />
          </svg>
          Run saved to history.
        </div>
      )}
      {runSaveError && (
        <div className="rounded-md border border-amber-200 bg-amber-50 px-4 py-2 text-xs text-amber-700">
          {runSaveError}
        </div>
      )}

      {/* ── Score summary ── */}
      {results.length > 0 && (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
          <div className="rounded-lg border border-zinc-200 bg-white p-3 text-center">
            <div className="text-2xl font-bold text-zinc-800">
              {overallScore !== null
                ? `${(overallScore * 100).toFixed(0)}%`
                : "—"}
            </div>
            <div className="text-[10px] uppercase tracking-wider text-zinc-500 mt-1">
              Overall Score
            </div>
          </div>
          <div className="rounded-lg border border-zinc-200 bg-white p-3 text-center">
            <div className="text-2xl font-bold text-zinc-800">
              {results.length}
            </div>
            <div className="text-[10px] uppercase tracking-wider text-zinc-500 mt-1">
              Total
            </div>
          </div>
          <div className="rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-center">
            <div className="text-2xl font-bold text-emerald-700">
              {passCount}
            </div>
            <div className="text-[10px] uppercase tracking-wider text-emerald-600 mt-1">
              Pass
            </div>
          </div>
          <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-center">
            <div className="text-2xl font-bold text-amber-700">
              {partialCount}
            </div>
            <div className="text-[10px] uppercase tracking-wider text-amber-600 mt-1">
              Partial
            </div>
          </div>
          <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-center">
            <div className="text-2xl font-bold text-red-700">{failCount}</div>
            <div className="text-[10px] uppercase tracking-wider text-red-600 mt-1">
              Fail
            </div>
          </div>
        </div>
      )}

      {/* ── Test cases table ── */}
      {testCases.length > 0 && (
        <div className="bg-white rounded-lg border border-zinc-200 overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-zinc-500 uppercase tracking-wide border-b border-zinc-200 bg-zinc-50">
                <th className="text-left font-medium px-4 py-3 w-8">#</th>
                <th className="text-left font-medium px-4 py-3">Input</th>
                <th className="text-left font-medium px-4 py-3">
                  Expected Behavior
                </th>
                <th className="text-left font-medium px-4 py-3 w-16">Tags</th>
                {results.length > 0 && (
                  <>
                    <th className="text-center font-medium px-4 py-3 w-20">
                      Verdict
                    </th>
                    <th className="text-center font-medium px-4 py-3 w-16">
                      Score
                    </th>
                  </>
                )}
                <th className="text-right font-medium px-4 py-3 w-10" />
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-100">
              {testCases.map((tc, i) => {
                const result = results.find((r) => r.testCaseId === tc.id);
                return (
                  <PromptTestCaseRow
                    key={tc.id}
                    index={i + 1}
                    testCase={tc}
                    result={result}
                    hasResults={results.length > 0}
                    onRemove={() => removeTestCase(tc.id)}
                  />
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* ── Empty state ── */}
      {testCases.length === 0 && !generating && (
        <div className="flex items-center justify-center rounded-lg border border-dashed border-zinc-300 bg-zinc-50 min-h-[200px]">
          <div className="text-center px-8">
            <svg
              className="w-12 h-12 text-zinc-300 mx-auto mb-3"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
            >
              <path
                d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            <p className="text-sm font-medium text-zinc-600 mb-1">
              Prompt Test Cases
            </p>
            <p className="text-xs text-zinc-400 max-w-sm">
              Select a prompt and click "Generate Test Cases" to create test
              inputs based on the prompt template. Then run the tests to
              evaluate prompt performance using LLM-as-judge.
            </p>
          </div>
        </div>
      )}

      {/* ── Run history ── */}
      <div data-testid="run-history" className="space-y-2">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-zinc-700">Run history</h3>
          {historyLoading && (
            <span className="text-xs text-zinc-400 animate-pulse">
              Loading…
            </span>
          )}
        </div>
        {history.length === 0 ? (
          <div className="rounded-lg border border-dashed border-zinc-300 bg-zinc-50 px-4 py-6 text-center text-xs text-zinc-400">
            No saved runs yet. Completed test runs are saved automatically.
          </div>
        ) : (
          <div className="bg-white rounded-lg border border-zinc-200 overflow-hidden divide-y divide-zinc-100">
            {history.map((run) => {
              const isExpanded = expandedRunId === run.test_run_id;
              return (
                <div key={run.test_run_id}>
                  <div className="flex items-center gap-3 px-4 py-3">
                    <button
                      type="button"
                      aria-label={`Toggle details for run ${run.test_run_id}`}
                      onClick={() => void toggleRunDetail(run.test_run_id)}
                      className="flex flex-1 items-center gap-3 text-left"
                    >
                      <span className="text-xs text-zinc-400 w-36 shrink-0">
                        {new Date(run.created_at).toLocaleString()}
                      </span>
                      <span className="text-xs text-zinc-600 w-28 shrink-0">
                        v{run.prompt_version ?? "—"} @{run.prompt_alias ?? "—"}
                      </span>
                      <span className="text-sm font-semibold text-zinc-800 w-14 shrink-0">
                        {run.overall_score !== null
                          ? `${(run.overall_score * 100).toFixed(0)}%`
                          : "—"}
                      </span>
                      <span className="text-xs text-zinc-500">
                        <span className="text-emerald-600 font-medium">
                          {run.passed_count} pass
                        </span>
                        {" · "}
                        <span className="text-amber-600 font-medium">
                          {run.partial_count} partial
                        </span>
                        {" · "}
                        <span className="text-red-600 font-medium">
                          {run.failed_count} fail
                        </span>
                        {` · ${run.test_set_size} total`}
                      </span>
                    </button>
                    <button
                      type="button"
                      onClick={() => void replayRun(run)}
                      disabled={replaying || running || generating}
                      className="rounded-md border border-zinc-300 bg-white px-3 py-1.5 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      Replay
                    </button>
                  </div>
                  {isExpanded && (
                    <div className="border-t border-zinc-100 bg-zinc-50/50 px-4 py-3">
                      {runDetailLoading ||
                      runDetail?.test_run_id !== run.test_run_id ? (
                        <div className="text-xs text-zinc-400 animate-pulse">
                          Loading details…
                        </div>
                      ) : (
                        <div className="space-y-3">
                          {runDetail.results.map((r, i) => (
                            <div
                              key={`${r.testCaseId}-${i}`}
                              className="rounded-md border border-zinc-200 bg-white p-3 text-xs"
                            >
                              <div className="flex items-center gap-2 mb-2">
                                <span
                                  className={`rounded px-2 py-0.5 font-medium ${
                                    r.verdict === "pass"
                                      ? "bg-emerald-100 text-emerald-700"
                                      : r.verdict === "partial"
                                        ? "bg-amber-100 text-amber-700"
                                        : "bg-red-100 text-red-700"
                                  }`}
                                >
                                  {r.verdict}
                                </span>
                                <span className="text-zinc-500">
                                  Score {(r.score * 100).toFixed(0)}%
                                </span>
                              </div>
                              <div className="space-y-1 text-zinc-700">
                                <p>
                                  <span className="font-medium text-zinc-500">
                                    Input:
                                  </span>{" "}
                                  {r.input}
                                </p>
                                <p>
                                  <span className="font-medium text-zinc-500">
                                    Expected:
                                  </span>{" "}
                                  {r.expectedBehavior}
                                </p>
                                <p>
                                  <span className="font-medium text-zinc-500">
                                    Actual:
                                  </span>{" "}
                                  {r.actualResponse || "—"}
                                </p>
                                <p>
                                  <span className="font-medium text-zinc-500">
                                    Judge reasoning:
                                  </span>{" "}
                                  {r.reasoning || "—"}
                                </p>
                              </div>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

interface UploadedDatasetExample {
  input: Record<string, unknown>;
  expected: Record<string, unknown>;
  tags: string[];
  weight?: number;
}

interface ScorerDraft {
  enabled: boolean;
  weight: string;
  configText: string;
}

interface PromptOptimizationRunSummary {
  started_at: string;
  prompt: PromptIdentitySnapshot;
  dataset_id: string;
  dataset_name: string;
  dataset_version: number | null;
  optimizer_type: string;
  scorers: Array<{ name: string; weight: number }>;
  gate_min_aggregate_score: number;
  gate_max_regression_delta: number;
  notes: string | null;
}

const TERMINAL_JOB_STATUSES = new Set([
  "candidate_ready",
  "completed",
  "failed",
  "rejected",
  "cancelled",
]);

const TERMINAL_OPERATION_STATUSES = new Set([
  "completed",
  "failed",
  "rejected",
  "cancelled",
]);

const ASSISTANT_OPTIMIZATION_SESSION_KEY_PREFIX =
  "caliber.prompts.optimization.assistantSession";

export function getAssistantOptimizationSessionStorageKey(
  agentId: string,
): string {
  return `${ASSISTANT_OPTIMIZATION_SESSION_KEY_PREFIX}.${agentId}`;
}

export function PromptOptimizationTab({
  prompts,
  loading,
  agentId,
  onAgentChange,
  promptAlias,
  onPromptAliasChange,
  hideAgentPicker = false,
  datasetId,
  onDatasetChange,
  lockedPrompt,
}: {
  prompts: PromptInfo[];
  loading: boolean;
  /** When provided, the agent is controlled by the parent (shared scope). */
  agentId?: string;
  onAgentChange?: (agentId: string) => void;
  promptAlias?: string;
  onPromptAliasChange?: (alias: string) => void;
  /** Hide the built-in prompt picker — the parent renders a single shared one. */
  hideAgentPicker?: boolean;
  /** When provided, the eval dataset is controlled by the parent (test-set handoff). */
  datasetId?: string;
  onDatasetChange?: (datasetId: string) => void;
  /**
   * When set, calibration is scoped to this single prompt: the picker is hidden
   * and this prompt is always the active one (used by the per-prompt Workspace).
   */
  lockedPrompt?: PromptInfo;
}): JSX.Element {
  // Any prompt with real content is calibratable — including a draft that is not
  // yet aliased to prod. The optimization run auto-provisions a hidden runtime
  // target from the prompt name; a brand-new draft just starts from a null
  // baseline, which the run handles. We no longer gate on ``has_prompt``.
  // A locked prompt collapses the option list to itself and forces the picker
  // hidden — the Workspace owns the prompt scope.
  const promptOptions = useMemo(
    () =>
      lockedPrompt
        ? [lockedPrompt]
        : prompts.filter((p) => isTestablePrompt(p)),
    [lockedPrompt, prompts],
  );
  const pickerHidden = hideAgentPicker || Boolean(lockedPrompt);

  const agentControlled = lockedPrompt !== undefined || agentId !== undefined;
  const [internalAgentId, setInternalAgentId] = useState("");
  const selectedAgentId = lockedPrompt
    ? lockedPrompt.agent_id
    : agentId !== undefined
      ? agentId
      : internalAgentId;
  const setSelectedAgentId = useCallback(
    (id: string) => {
      onAgentChange?.(id);
      if (agentId === undefined) setInternalAgentId(id);
    },
    [agentId, onAgentChange],
  );
  const [internalPromptAlias, setInternalPromptAlias] = useState("prod");
  const selectedPromptAlias = promptAlias ?? internalPromptAlias;
  const setSelectedPromptAlias = useCallback(
    (alias: string) => {
      onPromptAliasChange?.(alias);
      if (promptAlias === undefined) {
        setInternalPromptAlias(alias);
      }
    },
    [onPromptAliasChange, promptAlias],
  );

  const [datasets, setDatasets] = useState<EvalDataset[]>([]);
  const [options, setOptions] = useState<PromptCalibrationOptions | null>(null);
  const datasetControlled = datasetId !== undefined;
  const [internalDatasetId, setInternalDatasetId] = useState("");
  const selectedDatasetId = datasetControlled ? datasetId : internalDatasetId;
  const setSelectedDatasetId = useCallback(
    (id: string) => {
      onDatasetChange?.(id);
      if (!datasetControlled) setInternalDatasetId(id);
    },
    [datasetControlled, onDatasetChange],
  );
  const [selectedOptimizer, setSelectedOptimizer] = useState("MetaPrompt");
  const [scorerDrafts, setScorerDrafts] = useState<Record<string, ScorerDraft>>(
    {},
  );
  const [gateMinScore, setGateMinScore] = useState("0.85");
  const [gateMaxRegression, setGateMaxRegression] = useState("0.02");
  const [notes, setNotes] = useState("");

  const [runs, setRuns] = useState<RefinementJob[]>([]);
  const [activeRunJobId, setActiveRunJobId] = useState<string | null>(null);
  const [activeRun, setActiveRun] = useState<RefinementJob | null>(null);
  const [activeRunSummary, setActiveRunSummary] =
    useState<PromptOptimizationRunSummary | null>(null);

  const [loadingConfig, setLoadingConfig] = useState(true);
  const [loadingRuns, setLoadingRuns] = useState(false);
  const [startingRun, setStartingRun] = useState(false);
  const [uploadingDataset, setUploadingDataset] = useState(false);

  const [runError, setRunError] = useState<string | null>(null);
  const [applyingJobId, setApplyingJobId] = useState<string | null>(null);
  const [reviewRun, setReviewRun] = useState<RefinementJob | null>(null);
  const [runSuccess, setRunSuccess] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadSuccess, setUploadSuccess] = useState<string | null>(null);

  const [assistantSessionId, setAssistantSessionId] = useState<string | null>(
    null,
  );
  const [assistantIntentInput, setAssistantIntentInput] = useState("");
  const [resolvingIntent, setResolvingIntent] = useState(false);
  const [planningIntent, setPlanningIntent] = useState(false);
  const [executingIntent, setExecutingIntent] = useState(false);
  const [assistantIntentError, setAssistantIntentError] = useState<
    string | null
  >(null);
  const [assistantIntentResolve, setAssistantIntentResolve] =
    useState<AssistantIntentResolveResult | null>(null);
  const [assistantIntentPlan, setAssistantIntentPlan] =
    useState<AssistantIntentPlanResult | null>(null);
  const [assistantIntentExecution, setAssistantIntentExecution] =
    useState<AssistantIntentExecuteResult | null>(null);
  const [assistantOperationStatus, setAssistantOperationStatus] =
    useState<AssistantOperationStatus | null>(null);

  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadDatasetName, setUploadDatasetName] = useState("");
  const uploadDatasetDescription = "Uploaded from Prompt Calibration";

  const selectedPrompt =
    promptOptions.find((prompt) => prompt.agent_id === selectedAgentId) ?? null;
  const promptAliasOptions = getPromptAliasOptions(selectedPrompt);
  const deepevalRuntime = options?.runtime?.deepeval ?? null;
  const scorerGroups = useMemo(() => {
    const grouped = new Map<string, PromptCalibrationScorerOption[]>();
    for (const scorer of options?.scorers ?? []) {
      const category = scorer.category || "core";
      const rows = grouped.get(category) ?? [];
      rows.push(scorer);
      grouped.set(category, rows);
    }
    return Array.from(grouped.entries());
  }, [options]);

  useEffect(() => {
    if (!selectedAgentId) {
      setAssistantSessionId(null);
      return;
    }

    const storedSessionId = window.localStorage.getItem(
      getAssistantOptimizationSessionStorageKey(selectedAgentId),
    );
    setAssistantSessionId(
      storedSessionId && storedSessionId.trim() ? storedSessionId : null,
    );
    setAssistantIntentResolve(null);
    setAssistantIntentPlan(null);
    setAssistantIntentExecution(null);
    setAssistantOperationStatus(null);
    setAssistantIntentError(null);
  }, [selectedAgentId]);

  useEffect(() => {
    if (!selectedPrompt) return;
    if (!promptAliasOptions.includes(selectedPromptAlias)) {
      setSelectedPromptAlias(
        selectedPrompt.alias || promptAliasOptions[0] || "prod",
      );
    }
  }, [
    promptAliasOptions,
    selectedPrompt,
    selectedPromptAlias,
    setSelectedPromptAlias,
  ]);

  const buildIntentContext = useCallback((): Record<string, unknown> => {
    const context: Record<string, unknown> = {};
    if (selectedAgentId) {
      context.agent_id = selectedAgentId;
    }
    if (selectedPrompt) {
      context.prompt_name =
        selectedPrompt.prompt_name ?? selectedPrompt.agent_id;
    }
    if (selectedPromptAlias) {
      context.prompt_alias = selectedPromptAlias;
    }
    if (selectedDatasetId) {
      context.eval_dataset_id = selectedDatasetId;
    }
    if (selectedOptimizer) {
      context.optimizer_type = selectedOptimizer;
    }

    const selectedScorers: Array<Record<string, unknown>> = [];
    for (const scorer of options?.scorers ?? []) {
      const draft = scorerDrafts[scorer.name];
      if (!draft?.enabled || scorer.available === false) continue;
      const next: Record<string, unknown> = {
        name: scorer.name,
        weight: Number(draft.weight) || 1,
      };
      const rawConfig = draft.configText.trim();
      if (rawConfig) {
        try {
          const parsed = JSON.parse(rawConfig);
          if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
            next.config = parsed;
          }
        } catch {
          // Keep context lightweight; invalid JSON is validated before manual run.
        }
      }
      selectedScorers.push(next);
    }
    if (selectedScorers.length > 0) {
      context.scorers = selectedScorers;
    }

    const minScore = Number(gateMinScore);
    const maxRegression = Number(gateMaxRegression);
    const gate: Record<string, number> = {};
    if (Number.isFinite(minScore)) {
      gate.min_aggregate_score = minScore;
    }
    if (Number.isFinite(maxRegression)) {
      gate.max_regression_delta = maxRegression;
    }
    if (Object.keys(gate).length > 0) {
      context.gate = gate;
    }

    const cleanNotes = notes.trim();
    if (cleanNotes) {
      context.notes = cleanNotes;
    }
    return context;
  }, [
    gateMaxRegression,
    gateMinScore,
    notes,
    options,
    scorerDrafts,
    selectedAgentId,
    selectedPromptAlias,
    selectedDatasetId,
    selectedOptimizer,
    selectedPrompt,
  ]);

  const ensureAssistantSession = useCallback(async () => {
    if (assistantSessionId) {
      return assistantSessionId;
    }

    const session = await caliberApi.createAssistantSession({
      title: selectedPrompt
        ? `Prompt calibration workbench: ${selectedPrompt.agent_name}`
        : "Prompt calibration workbench",
      goal: "Intent-driven prompt calibration planning and execution.",
      metadata_: {
        source: "prompts-optimization-intent",
        prompt_context: selectedPrompt
          ? {
              ...toPromptIdentitySnapshot(selectedPrompt),
              alias: selectedPromptAlias || selectedPrompt.alias,
            }
          : null,
      },
      artifact_type: "prompt",
    });
    setAssistantSessionId(session.session_id);
    if (selectedAgentId) {
      window.localStorage.setItem(
        getAssistantOptimizationSessionStorageKey(selectedAgentId),
        session.session_id,
      );
    }
    return session.session_id;
  }, [
    assistantSessionId,
    selectedAgentId,
    selectedPrompt,
    selectedPromptAlias,
  ]);

  const applyPlanToForm = useCallback(
    (plan: AssistantIntentPlanResult) => {
      const slotMap = new Map(
        plan.slots.map((slot) => [slot.name, slot.value]),
      );

      const explicitAgentId = slotMap.get("agent_id");
      const promptName = slotMap.get("prompt_name");
      if (typeof explicitAgentId === "string" && explicitAgentId.trim()) {
        setSelectedAgentId(explicitAgentId.trim());
      } else if (typeof promptName === "string" && promptName.trim()) {
        const match = promptOptions.find((prompt) => {
          const rowPromptName = prompt.prompt_name ?? prompt.agent_id;
          return rowPromptName === promptName || prompt.agent_id === promptName;
        });
        if (match) {
          setSelectedAgentId(match.agent_id);
        }
      }

      const planDatasetId = slotMap.get("eval_dataset_id");
      if (typeof planDatasetId === "string" && planDatasetId.trim()) {
        setSelectedDatasetId(planDatasetId.trim());
      }

      const promptAliasValue = slotMap.get("prompt_alias");
      if (typeof promptAliasValue === "string" && promptAliasValue.trim()) {
        setSelectedPromptAlias(promptAliasValue.trim());
      }

      const optimizerType = slotMap.get("optimizer_type");
      if (typeof optimizerType === "string" && optimizerType.trim()) {
        setSelectedOptimizer(optimizerType.trim());
      }

      const minScore = slotMap.get("gate.min_aggregate_score");
      if (typeof minScore === "number" && Number.isFinite(minScore)) {
        setGateMinScore(String(minScore));
      }

      const maxRegression = slotMap.get("gate.max_regression_delta");
      if (typeof maxRegression === "number" && Number.isFinite(maxRegression)) {
        setGateMaxRegression(String(maxRegression));
      }

      const notesValue = slotMap.get("notes");
      if (typeof notesValue === "string") {
        setNotes(notesValue);
      }

      const scorerValue = slotMap.get("scorers");
      if (Array.isArray(scorerValue) && options) {
        const selectedScorers = new Map<
          string,
          { weight: number; config: Record<string, unknown> | null }
        >();
        for (const row of scorerValue) {
          if (typeof row === "string") {
            selectedScorers.set(row, { weight: 1, config: null });
            continue;
          }
          if (!row || typeof row !== "object") continue;
          const payload = row as Record<string, unknown>;
          const scorerName = payload.name;
          if (typeof scorerName !== "string" || !scorerName) continue;
          const weight = Number(payload.weight);
          const config = payload.config;
          selectedScorers.set(scorerName, {
            weight: Number.isFinite(weight) && weight > 0 ? weight : 1,
            config:
              config && typeof config === "object" && !Array.isArray(config)
                ? (config as Record<string, unknown>)
                : null,
          });
        }

        if (selectedScorers.size > 0) {
          const nextDrafts: Record<string, ScorerDraft> = {};
          for (const scorer of options.scorers) {
            const templateConfig =
              scorer.config_template &&
              typeof scorer.config_template === "object" &&
              !Array.isArray(scorer.config_template)
                ? JSON.stringify(scorer.config_template)
                : scorer.requires_config
                  ? '{"guidelines": ["Do not hallucinate."]}'
                  : "";
            const selected = selectedScorers.get(scorer.name);
            nextDrafts[scorer.name] = {
              enabled: Boolean(selected) && scorer.available !== false,
              weight: selected ? String(selected.weight) : "1",
              configText: selected?.config
                ? JSON.stringify(selected.config)
                : templateConfig,
            };
          }
          setScorerDrafts(nextDrafts);
        }
      }
    },
    [
      options,
      promptOptions,
      setSelectedAgentId,
      setSelectedDatasetId,
      setSelectedPromptAlias,
    ],
  );

  const parseSelectedScorers =
    useCallback((): PromptCalibrationScorerSelection[] => {
      const scorerSelections: PromptCalibrationScorerSelection[] = [];
      for (const scorer of options?.scorers ?? []) {
        const draft = scorerDrafts[scorer.name];
        if (!draft?.enabled) continue;

        if (scorer.available === false) {
          const installHint = scorer.install_command
            ? ` Install latest with '${scorer.install_command}'.`
            : "";
          throw new Error(
            `Scorer ${scorer.name} is unavailable.${installHint}`,
          );
        }

        const weight = Number(draft.weight);
        if (!Number.isFinite(weight) || weight <= 0) {
          throw new Error(`Scorer ${scorer.name} must have a positive weight.`);
        }

        let config: Record<string, unknown> | undefined;
        const rawConfig = draft.configText.trim();
        if (rawConfig) {
          const parsed = JSON.parse(rawConfig);
          if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
            throw new Error(
              `Scorer ${scorer.name} config must be a JSON object.`,
            );
          }
          config = parsed as Record<string, unknown>;
        }

        if (
          scorer.requires_config &&
          (!config || Object.keys(config).length === 0)
        ) {
          throw new Error(`Scorer ${scorer.name} requires config JSON.`);
        }

        scorerSelections.push({
          name: scorer.name,
          weight,
          config,
        });
      }
      return scorerSelections;
    }, [options, scorerDrafts]);

  useEffect(() => {
    if (!assistantSessionId) {
      return;
    }

    let cancelled = false;
    void (async () => {
      try {
        const latestPlan =
          await caliberApi.getAssistantLatestPlan(assistantSessionId);
        if (cancelled) {
          return;
        }
        setAssistantIntentPlan(latestPlan);
      } catch {
        // Ignore sessions that do not yet have an intent plan.
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [assistantSessionId]);

  useEffect(() => {
    if (!assistantIntentPlan) {
      return;
    }
    applyPlanToForm(assistantIntentPlan);
  }, [assistantIntentPlan, applyPlanToForm]);

  const resolveIntentWithAssistant = useCallback(async () => {
    const content = assistantIntentInput.trim();
    if (!content) {
      setAssistantIntentError("Describe what you want to accomplish first.");
      return;
    }

    setResolvingIntent(true);
    setAssistantIntentError(null);
    try {
      const sid = await ensureAssistantSession();
      const resolved = await caliberApi.resolveAssistantIntent(sid, {
        content,
        context: buildIntentContext(),
      });
      setAssistantIntentResolve(resolved);
    } catch (err) {
      setAssistantIntentError(
        err instanceof Error ? err.message : "Failed to resolve intent",
      );
    } finally {
      setResolvingIntent(false);
    }
  }, [assistantIntentInput, buildIntentContext, ensureAssistantSession]);

  const createPlanWithAssistant = useCallback(async () => {
    setPlanningIntent(true);
    setAssistantIntentError(null);
    try {
      const sid = await ensureAssistantSession();
      const content = assistantIntentInput.trim();
      const hintContext = buildIntentContext();
      const plan = await caliberApi.createAssistantPlan(sid, {
        content: content || undefined,
        slot_overrides: hintContext,
        context: hintContext,
      });
      setAssistantIntentPlan(plan);
    } catch (err) {
      setAssistantIntentError(
        err instanceof Error ? err.message : "Failed to build plan",
      );
    } finally {
      setPlanningIntent(false);
    }
  }, [assistantIntentInput, buildIntentContext, ensureAssistantSession]);

  const executePlanWithAssistant = useCallback(async () => {
    if (!assistantIntentPlan) {
      setAssistantIntentError("Build a plan before execution.");
      return;
    }

    setExecutingIntent(true);
    setAssistantIntentError(null);
    try {
      const sid = await ensureAssistantSession();
      const executed = await caliberApi.executeAssistantPlan(sid, {
        plan_id: assistantIntentPlan.plan_id,
        confirm: true,
      });
      setAssistantIntentExecution(executed);

      const immediateStatus: AssistantOperationStatus = {
        operation_id: executed.operation_id,
        session_id: sid,
        plan_id: executed.plan_id,
        intent_name: executed.intent_name,
        status: executed.status,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        result: executed.result,
        run: executed.run,
      };
      setAssistantOperationStatus(immediateStatus);

      const job = executed.result?.job;
      if (
        job &&
        typeof job === "object" &&
        typeof (job as { job_id?: unknown }).job_id === "string"
      ) {
        const typedJob = job as unknown as RefinementJob;
        setActiveRunJobId(typedJob.job_id);
        setActiveRun(typedJob);
        setRuns((prev) =>
          [
            typedJob,
            ...prev.filter((row) => row.job_id !== typedJob.job_id),
          ].slice(0, 12),
        );

        if (selectedPrompt) {
          const selectedScorers = parseSelectedScorers();
          setActiveRunSummary({
            started_at: new Date().toISOString(),
            prompt: toPromptIdentitySnapshot(selectedPrompt),
            dataset_id: selectedDatasetId,
            dataset_name:
              datasets.find(
                (dataset) => dataset.dataset_id === selectedDatasetId,
              )?.name ?? selectedDatasetId,
            dataset_version:
              datasets.find(
                (dataset) => dataset.dataset_id === selectedDatasetId,
              )?.version ?? null,
            optimizer_type: selectedOptimizer,
            scorers: selectedScorers.map((scorer) => ({
              name: scorer.name,
              weight: scorer.weight,
            })),
            gate_min_aggregate_score: Number(gateMinScore),
            gate_max_regression_delta: Number(gateMaxRegression),
            notes: notes.trim() || null,
          });
        }
      }
    } catch (err) {
      setAssistantIntentError(
        err instanceof Error ? err.message : "Failed to execute plan",
      );
    } finally {
      setExecutingIntent(false);
    }
  }, [
    assistantIntentPlan,
    datasets,
    ensureAssistantSession,
    gateMaxRegression,
    gateMinScore,
    notes,
    parseSelectedScorers,
    selectedDatasetId,
    selectedOptimizer,
    selectedPrompt,
  ]);

  useEffect(() => {
    if (!assistantSessionId || !assistantOperationStatus?.operation_id) {
      return;
    }
    if (TERMINAL_OPERATION_STATUSES.has(assistantOperationStatus.status)) {
      return;
    }

    let cancelled = false;
    const intervalId = window.setInterval(() => {
      void (async () => {
        try {
          const latest = await caliberApi.getAssistantOperation(
            assistantSessionId,
            assistantOperationStatus.operation_id,
          );
          if (cancelled) return;
          setAssistantOperationStatus(latest);
          if (TERMINAL_OPERATION_STATUSES.has(latest.status)) {
            window.clearInterval(intervalId);
          }
        } catch {
          // Keep polling unless canceled.
        }
      })();
    }, 2000);

    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [assistantOperationStatus, assistantSessionId]);

  useEffect(() => {
    // When the parent controls the agent (shared scope), it owns the default.
    if (agentControlled || selectedAgentId || promptOptions.length === 0)
      return;
    setSelectedAgentId(promptOptions[0]!.agent_id);
  }, [agentControlled, selectedAgentId, promptOptions, setSelectedAgentId]);

  // When a test set is handed off from step ①, its id may not be in the
  // dataset list yet (it was just created) — re-fetch so it resolves to a name.
  useEffect(() => {
    if (!selectedDatasetId) return;
    if (datasets.some((d) => d.dataset_id === selectedDatasetId)) return;
    let cancelled = false;
    void (async () => {
      try {
        const fresh = await caliberApi.listEvalDatasets({ status: "active" });
        if (!cancelled) setDatasets(fresh);
      } catch {
        // Ignore — the selection still applies; the name falls back to the id.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedDatasetId, datasets]);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      setLoadingConfig(true);
      setLoadError(null);
      try {
        const [loadedOptions, loadedDatasets] = await Promise.all([
          caliberApi.getPromptCalibrationOptions(),
          caliberApi.listEvalDatasets({ status: "active" }),
        ]);
        if (cancelled) return;

        setOptions(loadedOptions);
        setDatasets(loadedDatasets);
        if (loadedDatasets.length > 0 && !selectedDatasetId) {
          setSelectedDatasetId(loadedDatasets[0]!.dataset_id);
        }
        if (loadedOptions.default_optimizer) {
          setSelectedOptimizer(loadedOptions.default_optimizer);
        }

        const nextDrafts: Record<string, ScorerDraft> = {};
        for (const scorer of loadedOptions.scorers) {
          const hasTemplate =
            scorer.config_template &&
            typeof scorer.config_template === "object" &&
            !Array.isArray(scorer.config_template);
          const defaultConfigText = hasTemplate
            ? JSON.stringify(scorer.config_template)
            : scorer.requires_config
              ? '{"guidelines": ["Do not hallucinate."]}'
              : "";

          nextDrafts[scorer.name] = {
            enabled:
              scorer.available === false
                ? false
                : loadedOptions.default_scorers.includes(scorer.name),
            weight: "1",
            configText: defaultConfigText,
          };
        }
        setScorerDrafts(nextDrafts);
        setGateMinScore(String(loadedOptions.default_gate.min_aggregate_score));
        setGateMaxRegression(
          String(loadedOptions.default_gate.max_regression_delta),
        );
      } catch (err) {
        if (!cancelled) {
          setLoadError(
            err instanceof Error
              ? err.message
              : "Failed to load calibration settings",
          );
        }
      } finally {
        if (!cancelled) setLoadingConfig(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [selectedDatasetId, setSelectedDatasetId]);

  const refreshRuns = useCallback(async () => {
    if (!selectedAgentId) {
      setRuns([]);
      return;
    }
    setLoadingRuns(true);
    try {
      const allJobs = await caliberApi.listJobs({ agent_id: selectedAgentId });
      const promptJobs = allJobs
        .filter((job) => job.artifact_type === "prompt")
        .sort(
          (left, right) =>
            new Date(right.created_at).getTime() -
            new Date(left.created_at).getTime(),
        )
        .slice(0, 12);
      setRuns(promptJobs);
      if (activeRunJobId) {
        const current =
          promptJobs.find((job) => job.job_id === activeRunJobId) ?? null;
        if (current) {
          setActiveRun(current);
          if (TERMINAL_JOB_STATUSES.has(current.status)) {
            setActiveRunJobId(null);
          }
        }
      } else {
        // Restore the newest persisted run after a reload or tab switch. The
        // previous implementation only populated Active Run for jobs launched
        // during the current component mount, making a successful background
        // calibration look as if nothing happened when the user returned.
        const latest = promptJobs[0] ?? null;
        setActiveRun(latest);
        if (latest && !TERMINAL_JOB_STATUSES.has(latest.status)) {
          setActiveRunJobId(latest.job_id);
        }
      }
    } catch (err) {
      setRunError(
        err instanceof Error ? err.message : "Failed to load recent runs",
      );
    } finally {
      setLoadingRuns(false);
    }
  }, [selectedAgentId, activeRunJobId]);

  const applyRun = useCallback(
    async (jobId: string) => {
      setApplyingJobId(jobId);
      setRunError(null);
      setRunSuccess(null);
      try {
        const result = await caliberApi.applyJob(jobId);
        const artifactRef = readString(result.promotion?.artifact_ref);
        setReviewRun(null);
        setRunSuccess(
          `Candidate applied. ${SINGLE_ENVIRONMENT ? "The promoted prompt version is now live." : `@${selectedPromptAlias} now points to the promoted prompt version.`}${artifactRef ? ` Artifact: ${artifactRef}.` : ""}`,
        );
        await refreshRuns();
      } catch (err) {
        setRunError(
          err instanceof Error ? err.message : "Failed to apply candidate",
        );
      } finally {
        setApplyingJobId(null);
      }
    },
    [refreshRuns, selectedPromptAlias],
  );

  const reviewCandidate = useCallback((run: RefinementJob) => {
    setRunError(null);
    setRunSuccess(null);
    setReviewRun(run);
  }, []);

  useEffect(() => {
    void refreshRuns();
  }, [refreshRuns]);

  useEffect(() => {
    if (!activeRunJobId) return;
    let cancelled = false;
    const intervalId = window.setInterval(() => {
      void (async () => {
        try {
          const next = await caliberApi.getJob(activeRunJobId);
          if (cancelled) return;
          setActiveRun(next);
          setRuns((prev) => {
            const existingIndex = prev.findIndex(
              (job) => job.job_id === next.job_id,
            );
            if (existingIndex === -1) {
              return [next, ...prev].slice(0, 12);
            }
            const copy = [...prev];
            copy[existingIndex] = next;
            return copy;
          });
          if (TERMINAL_JOB_STATUSES.has(next.status)) {
            window.clearInterval(intervalId);
            setActiveRunJobId(null);
          }
        } catch {
          // Keep polling unless the run reaches a terminal state.
        }
      })();
    }, 2000);

    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [activeRunJobId]);

  const onScorerToggle = (name: string, enabled: boolean) => {
    setScorerDrafts((prev) => ({
      ...prev,
      [name]: {
        ...(prev[name] ?? { enabled: false, weight: "1", configText: "" }),
        enabled,
      },
    }));
  };

  const onScorerWeightChange = (name: string, weight: string) => {
    setScorerDrafts((prev) => ({
      ...prev,
      [name]: {
        ...(prev[name] ?? { enabled: true, weight: "1", configText: "" }),
        weight,
      },
    }));
  };

  const onScorerConfigChange = (name: string, configText: string) => {
    setScorerDrafts((prev) => ({
      ...prev,
      [name]: {
        ...(prev[name] ?? { enabled: true, weight: "1", configText: "" }),
        configText,
      },
    }));
  };

  const startRun = async () => {
    if (!selectedAgentId) {
      setRunError("Please select an agent prompt.");
      return;
    }
    if (!selectedDatasetId) {
      setRunError("Please select or upload an eval dataset.");
      return;
    }
    if (!selectedOptimizer) {
      setRunError("Please select a calibration algorithm.");
      return;
    }

    setStartingRun(true);
    setRunError(null);
    try {
      const scorers = parseSelectedScorers();
      if (scorers.length === 0) {
        throw new Error("Select at least one scorer.");
      }

      if (!selectedPrompt) {
        throw new Error("Select a prompt before starting a run.");
      }

      const selectedDataset = datasets.find(
        (dataset) => dataset.dataset_id === selectedDatasetId,
      );
      // Pin the dataset's current version so the run stays reproducible: a later
      // edit to the dataset (which bumps its version) won't change what this run
      // scored against.
      const pinnedDatasetVersion = selectedDataset?.version;
      const runSummary: PromptOptimizationRunSummary = {
        started_at: new Date().toISOString(),
        prompt: {
          ...toPromptIdentitySnapshot(selectedPrompt),
          alias: selectedPromptAlias || selectedPrompt.alias,
          artifact_ref: `prompts:/${resolvePromptName(selectedPrompt)}@${selectedPromptAlias || selectedPrompt.alias}`,
        },
        dataset_id: selectedDatasetId,
        dataset_name: selectedDataset?.name ?? selectedDatasetId,
        dataset_version: pinnedDatasetVersion ?? null,
        optimizer_type: selectedOptimizer,
        scorers: scorers.map((scorer) => ({
          name: scorer.name,
          weight: scorer.weight,
        })),
        gate_min_aggregate_score: Number(gateMinScore),
        gate_max_regression_delta: Number(gateMaxRegression),
        notes: notes.trim() || null,
      };

      const payload = {
        agent_id: selectedAgentId,
        prompt_alias: selectedPromptAlias,
        eval_dataset_id: selectedDatasetId,
        ...(pinnedDatasetVersion != null
          ? { eval_dataset_version: pinnedDatasetVersion }
          : {}),
        optimizer_type: selectedOptimizer,
        scorers,
        gate: {
          min_aggregate_score: Number(gateMinScore),
          max_regression_delta: Number(gateMaxRegression),
        },
        notes: notes.trim() || undefined,
      };

      const created = await caliberApi.createPromptCalibrationRun(payload);
      setActiveRunJobId(created.job.job_id);
      setActiveRun(created.job);
      setActiveRunSummary(runSummary);
      setRuns((prev) =>
        [
          created.job,
          ...prev.filter((job) => job.job_id !== created.job.job_id),
        ].slice(0, 12),
      );
    } catch (err) {
      setRunError(
        err instanceof Error ? err.message : "Failed to start calibration run",
      );
    } finally {
      setStartingRun(false);
    }
  };

  const onUploadFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0] ?? null;
    setUploadFile(file);
    setUploadError(null);
    setUploadSuccess(null);
    if (file && !uploadDatasetName.trim()) {
      const inferred = file.name.replace(/\.[^.]+$/, "");
      setUploadDatasetName(`prompt-cal-${inferred}`);
    }
    event.target.value = "";
  };

  const uploadDataset = async () => {
    if (!uploadFile) {
      setUploadError("Select a dataset file first.");
      return;
    }
    const datasetName = uploadDatasetName.trim();
    if (!datasetName) {
      setUploadError("Dataset name is required.");
      return;
    }

    setUploadingDataset(true);
    setUploadError(null);
    setUploadSuccess(null);

    try {
      const fileText = await uploadFile.text();
      const parsedExamples = parseUploadedDataset(fileText, uploadFile.name);
      if (parsedExamples.length === 0) {
        throw new Error("Uploaded file did not contain any valid examples.");
      }

      const dataset = await caliberApi.createEvalDataset({
        name: datasetName,
        description: uploadDatasetDescription,
        owner: "@local-admin",
        tags: ["prompt-calibration", "prompt-optimization", "upload"],
      });

      for (const example of parsedExamples) {
        await caliberApi.appendEvalExample(dataset.dataset_id, {
          input: example.input,
          expected: example.expected,
          tags: example.tags,
          weight: example.weight,
        });
      }

      const nextDatasets = await caliberApi.listEvalDatasets({
        status: "active",
      });
      setDatasets(nextDatasets);
      setSelectedDatasetId(dataset.dataset_id);
      setUploadSuccess(
        `Uploaded ${parsedExamples.length} examples to ${dataset.name}.`,
      );
    } catch (err) {
      setUploadError(
        err instanceof Error ? err.message : "Failed to upload dataset",
      );
    } finally {
      setUploadingDataset(false);
    }
  };

  if (loading && prompts.length === 0) {
    return (
      <div className="text-sm text-zinc-400 animate-pulse py-10 text-center">
        Loading prompts…
      </div>
    );
  }

  if (promptOptions.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-zinc-300 bg-zinc-50 p-8 text-center">
        <p className="text-sm text-zinc-500 mb-2">
          No prompts to calibrate yet.
        </p>
        <p className="text-sm text-zinc-400">
          Create a prompt on the Create Prompt tab — even a draft can be
          calibrated here.
        </p>
      </div>
    );
  }

  const assistantResult = assistantOperationStatus?.result ?? null;
  const assistantResultType = readString(assistantResult?.result_type);
  const assistantResultStatus = readString(assistantResult?.status);
  const assistantResultSummary = readString(assistantResult?.summary);
  const assistantResultWarnings = readStringArray(assistantResult?.warnings);
  const assistantTraceId = readString(assistantResult?.trace_id);
  const assistantCorrelationId = readString(assistantResult?.correlation_id);

  return (
    <div className="space-y-5">
      <div className="rounded-lg border border-caliber-100 bg-caliber-50/60 px-4 py-3 text-sm text-caliber-800">
        Launch a prompt calibration run with a selected dataset, strategy, and
        scoring setup.
      </div>

      {loadError && (
        <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
          {loadError}
        </div>
      )}

      <div className="rounded-lg border border-sky-200 bg-sky-50/60 p-4">
        <div className="flex items-center justify-between gap-3">
          <h2 className="text-sm font-semibold text-sky-900">
            Assistant-Guided Calibration
          </h2>
          {assistantSessionId && (
            <span className="text-[11px] text-sky-700">
              Session: <span className="font-mono">{assistantSessionId}</span>
            </span>
          )}
        </div>

        <p className="mt-1 text-xs text-sky-800">
          Describe what you want to calibrate. The assistant resolves intent,
          builds a plan, then executes it with confirmation.
        </p>

        <div className="mt-3">
          <label className="mb-1 block text-xs font-medium text-sky-900">
            Assistant intent request
          </label>
          <textarea
            aria-label="Assistant intent request"
            value={assistantIntentInput}
            onChange={(event) => setAssistantIntentInput(event.target.value)}
            rows={2}
            placeholder="Example: Calibrate support-agent against support-v1 dataset for faithfulness and answer relevance."
            className="w-full rounded-md border border-sky-200 bg-white px-3 py-2 text-sm text-zinc-900"
          />
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => void resolveIntentWithAssistant()}
            disabled={resolvingIntent || planningIntent || executingIntent}
            className="rounded-md border border-sky-300 bg-white px-3 py-1.5 text-xs font-medium text-sky-800 hover:bg-sky-100 disabled:opacity-60"
          >
            {resolvingIntent ? "Analyzing…" : "Analyze Intent"}
          </button>
          <button
            type="button"
            onClick={() => void createPlanWithAssistant()}
            disabled={planningIntent || executingIntent}
            className="rounded-md border border-sky-300 bg-white px-3 py-1.5 text-xs font-medium text-sky-800 hover:bg-sky-100 disabled:opacity-60"
          >
            {planningIntent ? "Planning…" : "Build Plan"}
          </button>
          <button
            type="button"
            onClick={() => void executePlanWithAssistant()}
            disabled={executingIntent || !assistantIntentPlan?.ready}
            className="rounded-md bg-sky-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-sky-800 disabled:opacity-60"
          >
            {executingIntent ? "Executing…" : "Execute Confirmed Plan"}
          </button>
        </div>

        {assistantIntentError && (
          <div className="mt-3 rounded border border-red-200 bg-red-50 px-2.5 py-1.5 text-xs text-red-700">
            {assistantIntentError}
          </div>
        )}

        {(assistantIntentResolve ||
          assistantIntentPlan ||
          assistantOperationStatus) && (
          <div className="mt-3 grid gap-2 md:grid-cols-3">
            <div className="rounded border border-sky-200 bg-white px-2.5 py-2 text-xs">
              <div className="font-semibold text-zinc-800">Resolved Intent</div>
              {assistantIntentResolve ? (
                <>
                  <div className="mt-1 text-zinc-700">
                    {assistantIntentResolve.intent.name}
                  </div>
                  <div className="text-zinc-500">
                    Confidence{" "}
                    {(assistantIntentResolve.intent.confidence * 100).toFixed(
                      0,
                    )}
                    %
                  </div>
                  {assistantIntentResolve.questions.length > 0 && (
                    <div className="mt-1 text-zinc-600">
                      Questions: {assistantIntentResolve.questions.join(" | ")}
                    </div>
                  )}
                </>
              ) : (
                <div className="mt-1 text-zinc-500">
                  Analyze intent to see classifier output.
                </div>
              )}
            </div>

            <div className="rounded border border-sky-200 bg-white px-2.5 py-2 text-xs">
              <div className="font-semibold text-zinc-800">Plan</div>
              {assistantIntentPlan ? (
                <>
                  <div className="mt-1 text-zinc-700">
                    {assistantIntentPlan.intent.name}
                  </div>
                  <div className="text-zinc-500">
                    Plan ID{" "}
                    <span className="font-mono">
                      {assistantIntentPlan.plan_id}
                    </span>
                  </div>
                  <div className="text-zinc-500">
                    Status {assistantIntentPlan.ready ? "ready" : "needs input"}
                  </div>
                  {assistantIntentPlan.missing_slots.length > 0 && (
                    <div className="mt-1 text-amber-700">
                      Missing: {assistantIntentPlan.missing_slots.join(", ")}
                    </div>
                  )}
                  <div className="mt-1 text-zinc-600">
                    Applied {assistantIntentPlan.slots.length} slot values to
                    the run form.
                  </div>
                </>
              ) : (
                <div className="mt-1 text-zinc-500">
                  Build a plan to populate run configuration.
                </div>
              )}
            </div>

            <div className="rounded border border-sky-200 bg-white px-2.5 py-2 text-xs">
              <div className="font-semibold text-zinc-800">Operation</div>
              {assistantOperationStatus ? (
                <>
                  <div className="mt-1 text-zinc-700">
                    {assistantOperationStatus.intent_name}
                  </div>
                  <div
                    className={`font-medium ${statusTone(assistantOperationStatus.status)}`}
                  >
                    {assistantOperationStatus.status}
                  </div>
                  <div className="text-zinc-500">
                    Op{" "}
                    <span className="font-mono">
                      {assistantOperationStatus.operation_id}
                    </span>
                  </div>
                  {assistantIntentExecution && (
                    <div className="mt-1 text-zinc-600">
                      Last action: {assistantIntentExecution.executed_action}
                    </div>
                  )}
                  {(assistantResultType || assistantResultStatus) && (
                    <div className="mt-1 text-zinc-600">
                      Result {assistantResultType ?? "operation"}
                      {assistantResultStatus && (
                        <span
                          className={`ml-1 font-medium ${statusTone(assistantResultStatus)}`}
                        >
                          {assistantResultStatus}
                        </span>
                      )}
                    </div>
                  )}
                  {assistantResultSummary && (
                    <div className="mt-1 text-zinc-600">
                      {assistantResultSummary}
                    </div>
                  )}
                  {assistantResultWarnings.length > 0 && (
                    <div className="mt-1 text-amber-700">
                      {assistantResultWarnings.slice(0, 2).join(" ")}
                    </div>
                  )}
                  {(assistantTraceId || assistantCorrelationId) && (
                    <div className="mt-1 space-y-0.5 text-[11px] text-zinc-500">
                      {assistantTraceId && (
                        <div>
                          Trace{" "}
                          <span className="font-mono">{assistantTraceId}</span>
                        </div>
                      )}
                      {assistantCorrelationId && (
                        <div>
                          Correlation{" "}
                          <span className="font-mono">
                            {assistantCorrelationId}
                          </span>
                        </div>
                      )}
                    </div>
                  )}
                </>
              ) : (
                <div className="mt-1 text-zinc-500">
                  Execute a plan to create an operation.
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      <div className="grid gap-5 lg:grid-cols-[1.3fr_1fr]">
        <div className="space-y-4 rounded-lg border border-zinc-200 bg-white p-4">
          <h2 className="text-sm font-semibold text-zinc-900">
            Run Configuration
          </h2>

          <div
            className={`grid gap-3 ${pickerHidden ? "md:grid-cols-2" : "md:grid-cols-3"}`}
          >
            {!pickerHidden && (
              <div>
                <label className="mb-1 block text-xs font-medium text-zinc-700">
                  Prompt
                </label>
                <select
                  aria-label="Calibration prompt"
                  value={selectedAgentId}
                  onChange={(event) => setSelectedAgentId(event.target.value)}
                  disabled={loadingConfig || startingRun}
                  className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500"
                >
                  {promptOptions.map((prompt) => (
                    <option key={prompt.agent_id} value={prompt.agent_id}>
                      {prompt.agent_name}
                    </option>
                  ))}
                </select>
              </div>
            )}
            <div>
              <label className="mb-1 block text-xs font-medium text-zinc-700">
                Alias
              </label>
              <select
                aria-label="Calibration prompt alias"
                value={selectedPromptAlias}
                onChange={(event) => setSelectedPromptAlias(event.target.value)}
                disabled={
                  loadingConfig ||
                  startingRun ||
                  promptAliasOptions.length === 0
                }
                className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500"
              >
                {promptAliasOptions.map((alias) => (
                  <option key={alias} value={alias}>
                    @{alias}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-zinc-700">
                Calibration Strategy
              </label>
              <select
                aria-label="Calibration strategy"
                value={selectedOptimizer}
                onChange={(event) => setSelectedOptimizer(event.target.value)}
                disabled={loadingConfig || startingRun}
                className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500"
              >
                {(options?.optimizers ?? []).map((optimizer) => (
                  <option key={optimizer} value={optimizer}>
                    {optimizer}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="rounded-md border border-zinc-200 bg-zinc-50 p-3">
            <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-600">
              Eval Dataset
            </div>
            <div className="grid gap-3 md:grid-cols-2">
              <div>
                <label className="mb-1 block text-xs font-medium text-zinc-700">
                  Existing Dataset
                </label>
                <select
                  aria-label="Calibration dataset"
                  value={selectedDatasetId}
                  onChange={(event) => setSelectedDatasetId(event.target.value)}
                  disabled={loadingConfig || startingRun || uploadingDataset}
                  className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500"
                >
                  <option value="">Select a dataset…</option>
                  {datasets.map((dataset) => (
                    <option key={dataset.dataset_id} value={dataset.dataset_id}>
                      {dataset.name}
                    </option>
                  ))}
                </select>
              </div>
              <div className="space-y-2">
                <label className="mb-1 block text-xs font-medium text-zinc-700">
                  Upload JSON/JSONL
                </label>
                <input
                  type="file"
                  aria-label="Upload calibration dataset"
                  accept=".json,.jsonl"
                  onChange={onUploadFileChange}
                  disabled={uploadingDataset || startingRun}
                  className="block w-full text-xs text-zinc-700"
                />
                <input
                  value={uploadDatasetName}
                  onChange={(event) => setUploadDatasetName(event.target.value)}
                  placeholder="Dataset name"
                  className="w-full rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-xs outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500"
                />
                <button
                  type="button"
                  onClick={() => void uploadDataset()}
                  disabled={uploadingDataset || startingRun}
                  className="rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-xs font-medium text-zinc-700 hover:bg-zinc-100 disabled:opacity-60"
                >
                  {uploadingDataset ? "Uploading…" : "Upload Dataset"}
                </button>
              </div>
            </div>
            {uploadError && (
              <div className="mt-2 rounded border border-red-200 bg-red-50 px-2.5 py-1.5 text-xs text-red-700">
                {uploadError}
              </div>
            )}
            {uploadSuccess && (
              <div className="mt-2 rounded border border-emerald-200 bg-emerald-50 px-2.5 py-1.5 text-xs text-emerald-700">
                {uploadSuccess}
              </div>
            )}
          </div>

          <div className="space-y-2">
            <div className="text-xs font-semibold uppercase tracking-wide text-zinc-600">
              Scoring
            </div>
            {deepevalRuntime && (
              <div className="rounded border border-zinc-200 bg-zinc-50 px-3 py-2 text-xs text-zinc-700">
                <div className="font-semibold text-zinc-800">
                  DeepEval runtime:{" "}
                  {deepevalRuntime.available ? "Available" : "Not installed"}
                </div>
                {!deepevalRuntime.available && (
                  <div className="mt-1 text-zinc-600">
                    {deepevalRuntime.reason ?? "DeepEval scorers are disabled."}{" "}
                    Install latest with{" "}
                    <span className="font-mono">
                      {deepevalRuntime.install_command}
                    </span>{" "}
                    and restart CALIBER.
                  </div>
                )}
              </div>
            )}
            <div className="space-y-2">
              {scorerGroups.map(([category, scorers]) => (
                <div
                  key={category}
                  className="rounded border border-zinc-200 bg-zinc-50/60 p-2"
                >
                  <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-600">
                    {scorerCategoryLabel(category)}
                  </div>
                  <div className="space-y-2">
                    {scorers.map((scorer) => {
                      const draft = scorerDrafts[scorer.name] ?? {
                        enabled: false,
                        weight: "1",
                        configText: "",
                      };
                      const available = scorer.available !== false;
                      return (
                        <div
                          key={scorer.name}
                          className="rounded border border-zinc-200 bg-white px-3 py-2"
                        >
                          <div className="flex items-center justify-between gap-3">
                            <label className="inline-flex items-center gap-2 text-sm font-medium text-zinc-800">
                              <input
                                type="checkbox"
                                checked={draft.enabled}
                                disabled={!available}
                                onChange={(event) =>
                                  onScorerToggle(
                                    scorer.name,
                                    event.target.checked,
                                  )
                                }
                              />
                              {scorer.label}
                            </label>
                            <div className="flex items-center gap-2">
                              <span className="rounded bg-zinc-100 px-1.5 py-0.5 text-[10px] font-medium text-zinc-600">
                                {scorer.provider || "mlflow"}
                              </span>
                              <span className="text-[11px] text-zinc-500">
                                Weight
                              </span>
                              <input
                                aria-label={`${scorer.name} weight`}
                                value={draft.weight}
                                onChange={(event) =>
                                  onScorerWeightChange(
                                    scorer.name,
                                    event.target.value,
                                  )
                                }
                                disabled={!draft.enabled || !available}
                                className="w-16 rounded border border-zinc-300 px-2 py-1 text-xs"
                              />
                            </div>
                          </div>
                          <div className="mt-1 text-[11px] text-zinc-500">
                            {scorer.description}
                          </div>
                          {!available && (
                            <div className="mt-2 rounded border border-amber-200 bg-amber-50 px-2 py-1.5 text-[11px] text-amber-800">
                              {scorer.unavailable_reason ??
                                "Scorer dependency is unavailable."}
                              {scorer.install_command && (
                                <>
                                  {" "}
                                  Install latest with{" "}
                                  <span className="font-mono">
                                    {scorer.install_command}
                                  </span>
                                  .
                                </>
                              )}
                            </div>
                          )}
                          {scorer.requires_config &&
                            draft.enabled &&
                            available && (
                              <textarea
                                value={draft.configText}
                                onChange={(event) =>
                                  onScorerConfigChange(
                                    scorer.name,
                                    event.target.value,
                                  )
                                }
                                rows={2}
                                className="mt-2 w-full rounded border border-zinc-300 bg-white px-2 py-1.5 text-xs font-mono"
                                placeholder='{"guidelines": ["Do not hallucinate."]}'
                              />
                            )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="grid gap-3 md:grid-cols-2">
            <div>
              <label className="mb-1 block text-xs font-medium text-zinc-700">
                Min Aggregate Score
              </label>
              <input
                aria-label="Minimum aggregate score"
                value={gateMinScore}
                onChange={(event) => setGateMinScore(event.target.value)}
                className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm"
              />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-zinc-700">
                Max Regression Delta
              </label>
              <input
                aria-label="Maximum regression delta"
                value={gateMaxRegression}
                onChange={(event) => setGateMaxRegression(event.target.value)}
                className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm"
              />
            </div>
          </div>

          <div>
            <label className="mb-1 block text-xs font-medium text-zinc-700">
              Run Notes (optional)
            </label>
            <textarea
              aria-label="Calibration run notes"
              value={notes}
              onChange={(event) => setNotes(event.target.value)}
              rows={2}
              className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm"
            />
          </div>

          {runError && (
            <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
              {runError}
            </div>
          )}

          {runSuccess && (
            <div
              role="status"
              className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-800"
            >
              {runSuccess}
            </div>
          )}

          <div className="flex items-center justify-between">
            <div className="text-xs text-zinc-500">
              {selectedPrompt
                ? `Target: ${selectedPrompt.agent_name} @${selectedPromptAlias}`
                : "Select a prompt."}
            </div>
            <button
              type="button"
              onClick={() => void startRun()}
              disabled={startingRun || loadingConfig || !selectedPrompt}
              className="rounded-md bg-caliber-600 px-3 py-2 text-xs font-medium text-white hover:bg-caliber-700 disabled:opacity-60"
            >
              {startingRun ? "Starting…" : "Start Calibration Run"}
            </button>
          </div>
        </div>

        <div className="space-y-4">
          <div className="rounded-lg border border-zinc-200 bg-white p-4">
            <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-600">
              Active Run
            </div>
            {!activeRun ? (
              <div className="text-sm text-zinc-500">No active run.</div>
            ) : (
              <div className="space-y-2 text-sm">
                <div>
                  <span className="text-zinc-500">Job:</span>{" "}
                  <span className="font-mono text-zinc-800">
                    {activeRun.job_id}
                  </span>
                </div>
                <div>
                  <span className="text-zinc-500">Status:</span>{" "}
                  <span
                    className={`font-medium ${statusTone(activeRun.status)}`}
                  >
                    {activeRun.status}
                  </span>
                </div>
                {activeRun.status === "candidate_ready" && (
                  <div>
                    <button
                      type="button"
                      data-testid="job-apply-btn"
                      disabled={applyingJobId === activeRun.job_id}
                      onClick={() => reviewCandidate(activeRun)}
                      className="rounded-md bg-caliber-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-caliber-700 disabled:opacity-60"
                    >
                      Review &amp; apply
                    </button>
                  </div>
                )}
                <div>
                  <span className="text-zinc-500">Stage:</span>{" "}
                  <span className="font-medium text-zinc-800">
                    {activeRun.current_stage}
                  </span>
                </div>
                {activeRun.eval_results && (
                  <div>
                    <span className="text-zinc-500">Score:</span>{" "}
                    <span className="font-medium text-zinc-800">
                      {formatOverallScore(activeRun.eval_results)}
                    </span>
                  </div>
                )}
                {activeRun.error_message && (
                  <div
                    role="alert"
                    className="rounded border border-red-200 bg-red-50 px-2.5 py-2 text-xs text-red-700"
                  >
                    {activeRun.error_message}
                  </div>
                )}
                {activeRunSummary && (
                  <div className="mt-1 rounded border border-zinc-200 bg-zinc-50 px-2.5 py-2 text-xs">
                    <div className="mb-1 font-semibold uppercase tracking-wide text-zinc-600">
                      Run Provenance
                    </div>
                    <div>
                      <span className="text-zinc-500">Prompt:</span>{" "}
                      <span className="font-mono text-zinc-700">
                        {activeRunSummary.prompt.prompt_name}
                      </span>
                    </div>
                    <div>
                      <span className="text-zinc-500">Ref:</span>{" "}
                      <span className="font-mono text-zinc-700">
                        {activeRunSummary.prompt.artifact_ref}
                      </span>
                    </div>
                    <div>
                      <span className="text-zinc-500">Alias / Version:</span>{" "}
                      <span className="font-mono text-zinc-700">
                        @{activeRunSummary.prompt.alias} /{" "}
                        {activeRunSummary.prompt.version != null
                          ? `v${activeRunSummary.prompt.version}`
                          : "n/a"}
                      </span>
                    </div>
                    <div>
                      <span className="text-zinc-500">Dataset:</span>{" "}
                      <span className="text-zinc-700">
                        {activeRunSummary.dataset_name}
                        {activeRunSummary.dataset_version != null
                          ? ` @ v${activeRunSummary.dataset_version}`
                          : ""}
                      </span>{" "}
                      <span className="font-mono text-zinc-500">
                        ({activeRunSummary.dataset_id})
                      </span>
                    </div>
                    <div>
                      <span className="text-zinc-500">
                        Calibration Strategy:
                      </span>{" "}
                      <span className="text-zinc-700">
                        {activeRunSummary.optimizer_type}
                      </span>
                    </div>
                    <div>
                      <span className="text-zinc-500">Scorers:</span>{" "}
                      <span className="text-zinc-700">
                        {activeRunSummary.scorers
                          .map((scorer) => `${scorer.name} (${scorer.weight})`)
                          .join(", ")}
                      </span>
                    </div>
                    <div>
                      <span className="text-zinc-500">Gate:</span>{" "}
                      <span className="font-mono text-zinc-700">
                        min={activeRunSummary.gate_min_aggregate_score} /
                        regression={activeRunSummary.gate_max_regression_delta}
                      </span>
                    </div>
                    {activeRunSummary.notes && (
                      <div>
                        <span className="text-zinc-500">Notes:</span>{" "}
                        <span className="text-zinc-700">
                          {activeRunSummary.notes}
                        </span>
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="rounded-lg border border-zinc-200 bg-white p-4">
            <div className="mb-2 flex items-center justify-between">
              <div className="text-xs font-semibold uppercase tracking-wide text-zinc-600">
                Recent Prompt Runs
              </div>
              <button
                type="button"
                onClick={() => void refreshRuns()}
                className="text-xs font-medium text-caliber-700 hover:underline"
              >
                Refresh
              </button>
            </div>
            {loadingRuns ? (
              <div className="text-sm text-zinc-500">Loading…</div>
            ) : runs.length === 0 ? (
              <div className="text-sm text-zinc-500">
                No runs yet for this prompt.
              </div>
            ) : (
              <div className="max-h-72 overflow-auto rounded border border-zinc-200">
                <table className="w-full text-xs">
                  <thead className="bg-zinc-50 text-zinc-500">
                    <tr>
                      <th className="px-2 py-1.5 text-left font-medium">Job</th>
                      <th className="px-2 py-1.5 text-left font-medium">
                        Status
                      </th>
                      <th className="px-2 py-1.5 text-left font-medium">
                        Stage
                      </th>
                      <th className="px-2 py-1.5 text-left font-medium">
                        Strategy
                      </th>
                      <th className="px-2 py-1.5 text-right font-medium">
                        Action
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.map((run) => (
                      <tr key={run.job_id} className="border-t border-zinc-100">
                        <td className="px-2 py-1.5 font-mono text-zinc-700">
                          {run.job_id}
                        </td>
                        <td
                          className={`px-2 py-1.5 font-medium ${statusTone(run.status)}`}
                        >
                          {run.status}
                        </td>
                        <td className="px-2 py-1.5 text-zinc-700">
                          {run.current_stage}
                        </td>
                        <td className="px-2 py-1.5 text-zinc-700">
                          {run.optimizer_type ?? "—"}
                        </td>
                        <td className="px-2 py-1.5 text-right">
                          {run.status === "candidate_ready" ? (
                            <button
                              type="button"
                              data-testid="job-apply-btn"
                              disabled={applyingJobId === run.job_id}
                              onClick={() => reviewCandidate(run)}
                              className="rounded-md bg-caliber-600 px-2 py-1 text-[11px] font-medium text-white hover:bg-caliber-700 disabled:opacity-60"
                            >
                              Review &amp; apply
                            </button>
                          ) : (
                            <span className="text-zinc-300">—</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      </div>
      {reviewRun && (
        <CalibrationApplyReviewDialog
          run={reviewRun}
          alias={selectedPromptAlias}
          busy={applyingJobId === reviewRun.job_id}
          error={runError}
          onCancel={() => setReviewRun(null)}
          onApply={() => void applyRun(reviewRun.job_id)}
        />
      )}
    </div>
  );
}

interface CalibrationScoreSnapshot {
  overall: number | null;
  dimensions: Record<string, number>;
}

interface CalibrationReviewView {
  baselineContent: string | null;
  candidateContent: string | null;
  rationale: string | null;
  diffSummary: string | null;
  candidateScore: CalibrationScoreSnapshot;
  baselineScore: CalibrationScoreSnapshot;
  overallDelta: number | null;
  gatePassed: boolean | null;
  gateReasons: string[];
  exampleCount: number | null;
  datasetId: string | null;
}

function scoreSnapshot(value: unknown): CalibrationScoreSnapshot {
  if (!value || typeof value !== "object") {
    return { overall: null, dimensions: {} };
  }
  const row = value as Record<string, unknown>;
  const dimensions: Record<string, number> = {};
  if (row.dimensions && typeof row.dimensions === "object") {
    for (const [name, rawScore] of Object.entries(
      row.dimensions as Record<string, unknown>,
    )) {
      if (typeof rawScore === "number") dimensions[name] = rawScore;
    }
  }
  return {
    overall: typeof row.overall === "number" ? row.overall : null,
    dimensions,
  };
}

export function calibrationReviewView(
  run: RefinementJob,
): CalibrationReviewView {
  const candidate = run.candidate ?? {};
  const evalResults = run.eval_results ?? {};
  const deltas =
    evalResults.deltas && typeof evalResults.deltas === "object"
      ? (evalResults.deltas as Record<string, unknown>)
      : {};
  const gate =
    evalResults.gate && typeof evalResults.gate === "object"
      ? (evalResults.gate as Record<string, unknown>)
      : {};
  const candidateScore = scoreSnapshot(evalResults.candidate);
  const baselineScore = scoreSnapshot(evalResults.baseline);
  return {
    baselineContent: readString(candidate.baseline_content),
    candidateContent: readString(candidate.content),
    rationale: readString(candidate.rationale),
    diffSummary: readString(candidate.diff_summary),
    candidateScore,
    baselineScore,
    overallDelta:
      typeof deltas.overall === "number"
        ? deltas.overall
        : candidateScore.overall !== null && baselineScore.overall !== null
          ? candidateScore.overall - baselineScore.overall
          : null,
    gatePassed: typeof gate.passed === "boolean" ? gate.passed : null,
    gateReasons: readStringArray(gate.reasons),
    exampleCount:
      typeof evalResults.n_examples === "number"
        ? evalResults.n_examples
        : null,
    datasetId: readString(evalResults.eval_dataset_id),
  };
}

function formatScore(value: number | null): string {
  return value === null ? "—" : `${(value * 100).toFixed(1)}%`;
}

function formatDelta(value: number | null): string {
  if (value === null) return "—";
  const points = value * 100;
  return `${points >= 0 ? "+" : ""}${points.toFixed(1)} pts`;
}

function deltaTone(value: number | null): string {
  if (value === null || value === 0) return "text-zinc-600";
  return value < 0 ? "text-red-700" : "text-emerald-700";
}

function CalibrationApplyReviewDialog({
  run,
  alias,
  busy,
  error,
  onCancel,
  onApply,
}: {
  run: RefinementJob;
  alias: string;
  busy: boolean;
  error: string | null;
  onCancel: () => void;
  onApply: () => void;
}): JSX.Element {
  const view = calibrationReviewView(run);
  const lines = view.candidateContent
    ? diffLines(view.baselineContent ?? "", view.candidateContent)
    : [];
  const stats = diffStats(lines);
  const dimensions = Array.from(
    new Set([
      ...Object.keys(view.baselineScore.dimensions),
      ...Object.keys(view.candidateScore.dimensions),
    ]),
  ).sort();
  const gateLabel =
    view.gatePassed === true
      ? "Passed"
      : view.gatePassed === false
        ? "Failed"
        : "Not recorded";

  return (
    <PromptModal
      onClose={() => !busy && onCancel()}
      ariaLabelledBy="calibration-apply-review-title"
    >
      <div aria-labelledby="calibration-apply-review-title">
        <div className="border-b border-zinc-200 px-5 py-4 sm:px-6">
          <div className="flex items-start justify-between gap-4">
            <div>
              <div className="text-xs font-semibold uppercase tracking-wide text-caliber-700">
                Production change review
              </div>
              <h2
                id="calibration-apply-review-title"
                className="mt-1 text-lg font-semibold text-zinc-900"
              >
                Review candidate before applying
              </h2>
              <p className="mt-1 text-sm text-zinc-600">
                Applying publishes this candidate, makes the promoted prompt
                version
                {SINGLE_ENVIRONMENT ? " live" : ` available at @${alias}`}, and
                records an audit event plus rollback checkpoint.
              </p>
            </div>
            <button
              type="button"
              aria-label="Close apply review"
              autoFocus
              disabled={busy}
              onClick={onCancel}
              className="rounded p-1 text-zinc-400 hover:bg-zinc-100 hover:text-zinc-700 disabled:opacity-50"
            >
              ×
            </button>
          </div>
          <div className="mt-3 font-mono text-xs text-zinc-500">
            {run.job_id}
          </div>
        </div>

        <div className="max-h-[70vh] space-y-5 overflow-y-auto px-5 py-5 sm:px-6">
          <section aria-labelledby="calibration-score-heading">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <h3
                id="calibration-score-heading"
                className="text-sm font-semibold text-zinc-900"
              >
                Evaluation score
              </h3>
              <span
                className={`rounded-full px-2.5 py-1 text-xs font-semibold ${
                  view.gatePassed === true
                    ? "bg-emerald-100 text-emerald-800"
                    : view.gatePassed === false
                      ? "bg-red-100 text-red-800"
                      : "bg-zinc-100 text-zinc-700"
                }`}
              >
                Gate: {gateLabel}
              </span>
            </div>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
              {[
                ["Baseline", formatScore(view.baselineScore.overall)],
                ["Candidate", formatScore(view.candidateScore.overall)],
                ["Change", formatDelta(view.overallDelta)],
              ].map(([label, value]) => (
                <div
                  key={label}
                  className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2"
                >
                  <div className="text-[11px] uppercase tracking-wide text-zinc-500">
                    {label}
                  </div>
                  <div className="mt-0.5 text-base font-semibold text-zinc-900">
                    {value}
                  </div>
                </div>
              ))}
            </div>
            {(view.exampleCount !== null || view.datasetId) && (
              <p className="mt-2 text-xs text-zinc-500">
                {view.exampleCount !== null
                  ? `${view.exampleCount} evaluation examples`
                  : "Evaluation dataset"}
                {view.datasetId ? ` · ${view.datasetId}` : ""}
              </p>
            )}
            {dimensions.length > 0 && (
              <div className="mt-3 overflow-x-auto rounded-lg border border-zinc-200">
                <table className="w-full text-xs">
                  <thead className="bg-zinc-50 text-zinc-500">
                    <tr>
                      <th className="px-3 py-2 text-left font-medium">
                        Scorer
                      </th>
                      <th className="px-3 py-2 text-right font-medium">
                        Baseline
                      </th>
                      <th className="px-3 py-2 text-right font-medium">
                        Candidate
                      </th>
                      <th className="px-3 py-2 text-right font-medium">
                        Change
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {dimensions.map((name) => {
                      const baseline =
                        view.baselineScore.dimensions[name] ?? null;
                      const candidate =
                        view.candidateScore.dimensions[name] ?? null;
                      const delta =
                        baseline !== null && candidate !== null
                          ? candidate - baseline
                          : null;
                      return (
                        <tr key={name} className="border-t border-zinc-100">
                          <td className="px-3 py-2 font-medium text-zinc-700">
                            {name}
                          </td>
                          <td className="px-3 py-2 text-right text-zinc-600">
                            {formatScore(baseline)}
                          </td>
                          <td className="px-3 py-2 text-right text-zinc-800">
                            {formatScore(candidate)}
                          </td>
                          <td
                            className={`px-3 py-2 text-right font-medium ${deltaTone(delta)}`}
                          >
                            {formatDelta(delta)}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
            {view.gateReasons.length > 0 && (
              <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-red-700">
                {view.gateReasons.map((reason, index) => (
                  <li key={`${index}-${reason}`}>{reason}</li>
                ))}
              </ul>
            )}
          </section>

          <section aria-labelledby="calibration-diff-heading">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <h3
                id="calibration-diff-heading"
                className="text-sm font-semibold text-zinc-900"
              >
                Prompt diff
              </h3>
              {view.candidateContent ? (
                <span className="text-xs text-zinc-500">
                  <span className="text-emerald-700">+{stats.additions}</span>{" "}
                  <span className="text-red-700">−{stats.deletions}</span> lines
                </span>
              ) : (
                <span className="text-xs text-zinc-500">Diff unavailable</span>
              )}
            </div>
            {view.diffSummary && (
              <p className="mb-2 text-xs text-zinc-600">{view.diffSummary}</p>
            )}
            {!view.baselineContent && (
              <div className="mb-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                No baseline snapshot was recorded. The candidate is shown as
                entirely new content.
              </div>
            )}
            {!view.candidateContent ? (
              <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                Candidate prompt content is unavailable. Do not apply this run
                until its candidate artifact is inspected.
              </div>
            ) : (
              <div
                data-testid="calibration-prompt-diff"
                className="max-h-72 overflow-auto rounded-lg border border-zinc-200 bg-slate-50 py-2 font-mono text-[11px] leading-relaxed text-zinc-700"
              >
                {lines.map((line, index) => (
                  <div
                    key={`${line.op}-${index}`}
                    className={`flex ${DIFF_LINE_CLASS[line.op]}`}
                  >
                    <span className="w-8 shrink-0 select-none px-2 text-right text-zinc-400">
                      {line.op === "insert"
                        ? "+"
                        : line.op === "delete"
                          ? "−"
                          : " "}
                    </span>
                    <span className="whitespace-pre-wrap break-words pr-3">
                      {line.words
                        ? line.words.map((word, wordIndex) => (
                            <span
                              key={wordIndex}
                              className={DIFF_WORD_CLASS[word.op]}
                            >
                              {word.value}
                            </span>
                          ))
                        : line.text || " "}
                    </span>
                  </div>
                ))}
              </div>
            )}
            {view.rationale && (
              <div className="mt-3 rounded-lg border border-blue-100 bg-blue-50 px-3 py-2 text-xs text-blue-900">
                <span className="font-semibold">Optimizer rationale:</span>{" "}
                {view.rationale}
              </div>
            )}
          </section>
          {error && (
            <div
              role="alert"
              className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800"
            >
              Apply failed: {error}
            </div>
          )}
        </div>

        <div className="flex flex-col-reverse gap-2 border-t border-zinc-200 px-5 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-6">
          <p className="text-xs text-zinc-500">
            Rollback remains available from the release checkpoint.
          </p>
          <div className="flex justify-end gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={onCancel}
              className="rounded-md border border-zinc-300 px-3 py-2 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={!view.candidateContent || view.gatePassed === false}
              aria-busy={busy}
              aria-disabled={
                busy || !view.candidateContent || view.gatePassed === false
              }
              onClick={() => {
                if (!busy) onApply();
              }}
              className="rounded-md bg-caliber-600 px-3 py-2 text-xs font-medium text-white hover:bg-caliber-700 disabled:opacity-50 aria-disabled:opacity-50"
            >
              {busy
                ? "Applying…"
                : SINGLE_ENVIRONMENT
                  ? "Apply candidate live"
                  : `Apply candidate to @${alias}`}
            </button>
          </div>
        </div>
      </div>
    </PromptModal>
  );
}

export function statusTone(status: string): string {
  if (status === "completed" || status === "applied") return "text-emerald-700";
  if (status === "running" || status === "queued") return "text-blue-700";
  if (status === "blocked" || status === "candidate_ready")
    return "text-amber-700";
  if (status === "failed" || status === "rejected" || status === "cancelled")
    return "text-red-700";
  return "text-zinc-700";
}

export function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

export function readStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (item): item is string =>
      typeof item === "string" && item.trim().length > 0,
  );
}

export function scorerCategoryLabel(category: string): string {
  if (category === "deepeval_beta") return "DeepEval (Beta)";
  if (category === "core") return "Core MLflow";
  return category;
}

export function formatOverallScore(
  evalResults: Record<string, unknown>,
): string {
  const candidate = evalResults.candidate;
  if (!candidate || typeof candidate !== "object") return "—";
  const overall = (candidate as Record<string, unknown>).overall;
  if (typeof overall !== "number") return "—";
  return `${(overall * 100).toFixed(1)}%`;
}

export function parseUploadedDataset(
  text: string,
  fileName: string,
): UploadedDatasetExample[] {
  const lowerName = fileName.toLowerCase();
  const rawRows: unknown[] = [];

  if (lowerName.endsWith(".jsonl")) {
    const lines = text
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line.length > 0);
    for (const line of lines) {
      rawRows.push(JSON.parse(line));
    }
  } else {
    const parsed = JSON.parse(text);
    if (Array.isArray(parsed)) {
      rawRows.push(...parsed);
    } else if (
      parsed &&
      typeof parsed === "object" &&
      Array.isArray((parsed as { examples?: unknown[] }).examples)
    ) {
      rawRows.push(...(parsed as { examples: unknown[] }).examples);
    } else {
      throw new Error(
        "JSON upload must be an array or an object with an examples array.",
      );
    }
  }

  return rawRows.map((row, index) => normalizeUploadedExample(row, index));
}

export function normalizeUploadedExample(
  raw: unknown,
  index: number,
): UploadedDatasetExample {
  if (!raw || typeof raw !== "object") {
    throw new Error(`Invalid example at row ${index + 1}.`);
  }
  const row = raw as Record<string, unknown>;

  let input: Record<string, unknown>;
  const rawInput = row.input;
  if (rawInput && typeof rawInput === "object" && !Array.isArray(rawInput)) {
    input = rawInput as Record<string, unknown>;
  } else if (typeof rawInput === "string") {
    input = { user_message: rawInput };
  } else if (typeof row.user_message === "string") {
    input = { user_message: row.user_message };
  } else {
    input = { user_message: `Example ${index + 1}` };
  }

  let expected: Record<string, unknown>;
  const rawExpected = row.expected;
  if (
    rawExpected &&
    typeof rawExpected === "object" &&
    !Array.isArray(rawExpected)
  ) {
    expected = rawExpected as Record<string, unknown>;
  } else if (typeof rawExpected === "string") {
    expected = { expected_response: rawExpected };
  } else if (typeof row.reference_answer === "string") {
    expected = { expected_response: row.reference_answer };
  } else {
    expected = { expected_response: "" };
  }

  const tags = Array.isArray(row.tags)
    ? row.tags.filter((tag): tag is string => typeof tag === "string")
    : [];

  const weight = typeof row.weight === "number" ? row.weight : undefined;

  return { input, expected, tags, weight };
}

export function PromptTestCaseRow({
  index,
  testCase,
  result,
  hasResults,
  onRemove,
}: {
  index: number;
  testCase: TestCase;
  result?: TestResult;
  hasResults: boolean;
  onRemove: () => void;
}): JSX.Element {
  const [expanded, setExpanded] = useState(false);

  const verdictColors = {
    pass: "bg-emerald-100 text-emerald-700",
    partial: "bg-amber-100 text-amber-700",
    fail: "bg-red-100 text-red-700",
  };

  return (
    <>
      <tr
        className="hover:bg-zinc-50 cursor-pointer"
        onClick={() => result && setExpanded(!expanded)}
      >
        <td className="px-4 py-3 text-zinc-400 font-mono text-xs">{index}</td>
        <td className="px-4 py-3 text-zinc-800 max-w-xs">
          <div className="truncate">{testCase.input}</div>
        </td>
        <td className="px-4 py-3 text-zinc-600 max-w-xs">
          <div className="truncate">{testCase.expectedBehavior}</div>
        </td>
        <td className="px-4 py-3">
          <div className="flex gap-1 flex-wrap">
            {testCase.tags.map((tag) => (
              <span
                key={tag}
                className="text-[10px] bg-zinc-100 text-zinc-600 px-1.5 py-0.5 rounded"
              >
                {tag}
              </span>
            ))}
          </div>
        </td>
        {hasResults && (
          <>
            <td className="px-4 py-3 text-center">
              {result ? (
                <span
                  className={`text-[10px] font-semibold uppercase px-2 py-0.5 rounded ${verdictColors[result.verdict]}`}
                >
                  {result.verdict}
                </span>
              ) : (
                <span className="text-xs text-zinc-400">—</span>
              )}
            </td>
            <td className="px-4 py-3 text-center">
              {result ? (
                <span className="text-xs font-mono font-medium text-zinc-700">
                  {(result.score * 100).toFixed(0)}%
                </span>
              ) : (
                <span className="text-xs text-zinc-400">—</span>
              )}
            </td>
          </>
        )}
        <td className="px-4 py-3 text-right">
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onRemove();
            }}
            className="text-zinc-400 hover:text-red-500 transition-colors"
            title="Remove test case"
          >
            <svg
              className="w-3.5 h-3.5"
              viewBox="0 0 20 20"
              fill="currentColor"
            >
              <path d="M6.28 5.22a.75.75 0 00-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 101.06 1.06L10 11.06l3.72 3.72a.75.75 0 101.06-1.06L11.06 10l3.72-3.72a.75.75 0 00-1.06-1.06L10 8.94 6.28 5.22z" />
            </svg>
          </button>
        </td>
      </tr>
      {expanded && result && (
        <tr>
          <td
            colSpan={hasResults ? 7 : 5}
            className="px-4 py-3 bg-zinc-50 border-t border-zinc-100"
          >
            <div className="space-y-3 text-xs">
              <div>
                <div className="font-semibold text-zinc-700 mb-1">
                  Judge Reasoning
                </div>
                <p className="text-zinc-600">{result.reasoning}</p>
              </div>
              <div>
                <div className="font-semibold text-zinc-700 mb-1">
                  Actual Response
                </div>
                <pre className="bg-white border border-zinc-200 rounded-md p-3 text-zinc-700 whitespace-pre-wrap break-words max-h-48 overflow-y-auto font-mono text-[11px]">
                  {result.actualResponse || "(empty response)"}
                </pre>
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
