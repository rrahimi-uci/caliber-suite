/**
 * Tool Registry (§16.7, §15.4) — the landing list of registered tools with
 * side-effect badges and the registration wizard, plus the per-tool Workspace:
 * open a tool to reach a focused header + six stage tabs (Spec · Sandbox ·
 * Fixtures · Test Runs · Hardening · Publish) scoped to that one tool.
 */

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { caliberApi } from "@/api/caliberApi";
import type { AssistantConfig, AssistantModelOption } from "@/api/assistantTypes";
import type {
  CalibrationCase,
  CalibrationResult,
  ToolDefinition,
  ToolSource,
  ToolTestRunResult,
  ToolUpdatePayload,
} from "@/api/workflowTypes";
import type {
  ToolTestRunDetail,
  ToolTestRunKind,
  ToolTestRunResultCase,
  ToolTestRunSummary,
  ToolWorkspaceResponse,
} from "@/api/types";
import { CalibrationPanel } from "@/components/CalibrationPanel";
import { ClearFiltersButton } from "@/components/ClearFiltersButton";
import { FilterBar } from "@/components/FilterBar";
import { CodeBlock } from "@/components/workflows/CodeHighlight";
import { ListRow, ListRows } from "@/components/ListRow";
import { PageHeader } from "@/components/PageHeader";
import { ToolSignature } from "@/components/tools/ToolSignature";
import { PageTabs, type PageTab } from "@/components/PageTabs";
import { SearchInput } from "@/components/SearchInput";
import { FilterSelect } from "@/components/FilterSelect";
import { ViewToggle } from "@/components/ViewToggle";
import { useApiMutation, useApiQuery, useInvalidate } from "@/hooks/useApiQuery";
import { useViewMode } from "@/hooks/useViewMode";
import { getActiveProjectId } from "@/workspace/activeWorkspace";
import { ToolWizard } from "./ToolWizard";

/** Lifecycle pill tones, keyed by the workspace ``lifecycle`` string. */
const TOOL_LIFECYCLE_TONES: Record<string, string> = {
  Draft: "bg-slate-100 text-slate-600 ring-slate-200/60",
  "Has fixtures": "bg-blue-50 text-blue-700 ring-blue-200/60",
  Tested: "bg-violet-50 text-caliber-purple ring-violet-200/60",
  Hardened: "bg-amber-50 text-amber-700 ring-amber-200/60",
  Published: "bg-emerald-50 text-emerald-700 ring-emerald-200/60",
};

/** Lifecycle status pill for the Workspace header (mirrors PromptStatusBadge). */
function ToolStatusBadge({ status }: { status: string }): JSX.Element {
  const tone =
    TOOL_LIFECYCLE_TONES[status] ?? "bg-slate-100 text-slate-600 ring-slate-200/60";
  return (
    <span
      data-testid="tool-workspace-status-badge"
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ${tone}`}
    >
      {status}
    </span>
  );
}

/** Pass/fail/partial tallies + mean score for a per-case result array. */
function summarizeToolResults(results: ToolTestRunResultCase[]): {
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

const SIDE_EFFECT_BADGE: Record<string, string> = {
  read: "🟢 read",
  write: "🟡 write",
  external_action: "🔴 external",
};

/** Landing view: the registry list, or the in-page registration wizard. */
type ToolView = "registry" | "register";

/** Six per-tool Workspace stages. */
type ToolStage = "spec" | "sandbox" | "fixtures" | "runs" | "hardening" | "publish";

const TOOL_WORKSPACE_STAGES: PageTab[] = [
  {
    key: "spec",
    label: "Spec",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
        <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" />
        <path d="M14 2v6h6M16 13H8M16 17H8M10 9H8" />
      </svg>
    ),
  },
  {
    key: "sandbox",
    label: "Sandbox",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
        <path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z" />
      </svg>
    ),
  },
  {
    key: "fixtures",
    label: "Fixtures",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
        <path d="M4 7V4h16v3M9 20h6M12 4v16" />
      </svg>
    ),
  },
  {
    key: "runs",
    label: "Test Runs",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
        <path d="M3 3v18h18M9 17V9M14 17v-4M19 17V5" />
      </svg>
    ),
  },
  {
    key: "hardening",
    label: "Hardening",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
        <path d="M12 3l7 4v5c0 5-3.5 9-7 10-3.5-1-7-5-7-10V7l7-4z" />
        <path d="M9 12l2 2 4-4" />
      </svg>
    ),
  },
  {
    key: "publish",
    label: "Publish",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
        <path d="M22 11.08V12a10 10 0 11-5.93-9.14M22 4L12 14.01l-3-3" />
      </svg>
    ),
  },
];

export function ToolRegistry(): JSX.Element {
  const [activeTab, setActiveTab] = useState<ToolView>("registry");
  // The tool currently opened into the Workspace (null = landing list).
  const [openToolId, setOpenToolId] = useState<string | null>(null);
  const [toolSearch, setToolSearch] = useState("");
  // Both filters default to the empty "All" sentinel; a row must match the text
  // query AND every active filter (additive).
  const [statusFilter, setStatusFilter] = useState("");
  const [sideEffectFilter, setSideEffectFilter] = useState("");
  const [viewMode, setViewMode] = useViewMode("tools");

  const query = useApiQuery(["tools", "all"], (s) => caliberApi.listTools("all", s));

  const tools: ToolDefinition[] = query.data ?? [];
  const toolQuery = toolSearch.trim().toLowerCase();
  const filteredTools = tools.filter((tool) => {
    if (statusFilter && tool.status !== statusFilter) return false;
    if (sideEffectFilter && tool.side_effect_level !== sideEffectFilter) {
      return false;
    }
    if (!toolQuery) return true;
    return [tool.name, tool.description, tool.status, tool.side_effect_level]
      .filter(Boolean)
      .some((field) => String(field).toLowerCase().includes(toolQuery));
  });
  const hasRegistryFilters = Boolean(
    toolSearch || statusFilter || sideEffectFilter,
  );
  const activeCount = tools.filter((tool) => tool.status === "active").length;
  const approvalCount = tools.filter((tool) => tool.requires_approval).length;
  const previewCount = tools.filter((tool) => tool.allow_in_preview).length;
  const TOOL_STAT_TILES: Array<{ key: string; label: string; value: number; tone: string; icon: JSX.Element }> = [
    {
      key: "registry",
      label: "Tools in registry",
      value: tools.length,
      tone: "bg-violet-50 text-caliber-purple",
      icon: <path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z" />,
    },
    {
      key: "active",
      label: "Active",
      value: activeCount,
      tone: "bg-emerald-50 text-emerald-600",
      icon: <path d="M22 11.08V12a10 10 0 11-5.93-9.14M22 4L12 14.01l-3-3" />,
    },
    {
      key: "approval",
      label: "Approval required",
      value: approvalCount,
      tone: "bg-amber-50 text-amber-600",
      icon: <path d="M12 3l7 4v5c0 5-3.5 9-7 10-3.5-1-7-5-7-10V7l7-4z" />,
    },
    {
      key: "preview",
      label: "Preview enabled",
      value: previewCount,
      tone: "bg-blue-50 text-blue-600",
      icon: <path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12zM12 15a3 3 0 100-6 3 3 0 000 6z" />,
    },
  ];

  // Opening a tool replaces the landing list with its focused Workspace; the
  // open tool is resolved from the loaded list so the header has facts to show.
  const openTool = openToolId
    ? tools.find((t) => t.tool_id === openToolId) ?? null
    : null;
  if (openToolId) {
    return (
      <ToolWorkspace
        toolId={openToolId}
        tool={openTool}
        onBack={() => setOpenToolId(null)}
      />
    );
  }

  return (
    <div className="space-y-6 animate-fade-in">
      <PageHeader
        title="Tools"
        subtitle="Registered tools with side-effect classification, preview gating, and sandbox test-run capabilities."
        actions={
          activeTab === "registry" ? (
            <button
              type="button"
              data-testid="tool-register-action"
              onClick={() => setActiveTab("register")}
              className="inline-flex items-center gap-1.5 rounded-md bg-caliber-600 px-3.5 py-2 text-sm font-medium text-white hover:bg-caliber-700"
            >
              <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M12 5v14M5 12h14" />
              </svg>
              Register Tool
            </button>
          ) : (
            <button
              type="button"
              onClick={() => setActiveTab("registry")}
              className="inline-flex items-center gap-1.5 text-sm font-medium text-slate-500 hover:text-slate-700"
            >
              <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M19 12H5M12 19l-7-7 7-7" />
              </svg>
              Back to tools
            </button>
          )
        }
      />

      {/* ── tab content ────────────────────────────────────── */}
      {activeTab === "register" && (
        <div className="rounded-2xl border border-caliber-200/60 bg-caliber-50/30 p-5 shadow-card">
          <ToolWizard onClose={() => setActiveTab("registry")} />
        </div>
      )}

      {activeTab === "registry" && (
        <>
          {query.error && (
            <div className="rounded-2xl border border-red-200/60 bg-red-50 px-5 py-4 text-sm text-red-700 shadow-card">
              <div className="font-semibold">Failed to load tools</div>
              <div className="mt-0.5 text-xs text-red-500">{query.error.message}</div>
            </div>
          )}

          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            {TOOL_STAT_TILES.map((tile) => (
              <div key={tile.key} data-testid={`tool-tile-${tile.key}`} className="stat-card">
                <div className="flex items-start justify-between">
                  <span className={`grid h-10 w-10 place-items-center rounded-xl ${tile.tone}`}>
                    <svg className="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.85">{tile.icon}</svg>
                  </span>
                </div>
                <div className="mt-4 text-3xl font-bold tracking-tight text-slate-900">{tile.value}</div>
                <div className="mt-1 text-sm text-slate-500">{tile.label}</div>
              </div>
            ))}
          </div>

          <FilterBar
            search={
              <SearchInput
                value={toolSearch}
                onChange={setToolSearch}
                ariaLabel="Search tools"
                placeholder="Search tools by name, description, status…"
                className="w-full"
              />
            }
            filters={
              <>
                <FilterSelect
                  label="Status"
                  allLabel="All statuses"
                  value={statusFilter}
                  onChange={setStatusFilter}
                  options={[
                    { value: "active", label: "Active" },
                    { value: "deprecated", label: "Deprecated" },
                    { value: "archived", label: "Archived" },
                  ]}
                  className="w-full sm:w-44"
                />
                <FilterSelect
                  label="Side effect"
                  allLabel="All side effects"
                  value={sideEffectFilter}
                  onChange={setSideEffectFilter}
                  options={[
                    { value: "read", label: "Read" },
                    { value: "write", label: "Write" },
                    { value: "external_action", label: "External action" },
                  ]}
                  className="w-full sm:w-44"
                />
              </>
            }
            actions={
              <>
                <ClearFiltersButton
                  visible={hasRegistryFilters}
                  onClear={() => {
                    setToolSearch("");
                    setStatusFilter("");
                    setSideEffectFilter("");
                  }}
                />
                <ViewToggle value={viewMode} onChange={setViewMode} />
              </>
            }
          />

          {query.isLoading && tools.length === 0 && (
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
              {[1, 2, 3].map((i) => (
                <div key={i} className="rounded-2xl border border-slate-200/60 bg-white p-5 shadow-card shimmer h-44" />
              ))}
            </div>
          )}

          {tools.length === 0 && query.data && (
            <div
              data-testid="tools-empty"
              className="rounded-2xl border-2 border-dashed border-slate-200 bg-gradient-hero px-8 py-12 text-center"
            >
              <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-white shadow-card">
                <svg className="h-7 w-7 text-slate-300" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                  <path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z" />
                </svg>
              </div>
              <div className="text-sm font-semibold text-slate-600">No tools registered.</div>
              <div className="mx-auto mt-1.5 max-w-sm text-xs text-slate-400">
                Register a tool to expose it to agents, workflows, and sandbox test runs.
              </div>
            </div>
          )}

          {tools.length > 0 && filteredTools.length === 0 && (
            <div className="rounded-2xl border-2 border-dashed border-slate-200 bg-gradient-hero px-8 py-12 text-center">
              <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-white shadow-card">
                <svg className="h-7 w-7 text-slate-300" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                  <path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z" />
                </svg>
              </div>
              <div className="text-sm font-semibold text-slate-600">
                {toolQuery
                  ? `No tools match “${toolSearch.trim()}”.`
                  : "No tools match the current filters."}
              </div>
              <div className="mx-auto mt-1.5 max-w-sm text-xs text-slate-400">
                Try a different name, status, or side-effect term.
              </div>
            </div>
          )}

          {filteredTools.length > 0 && viewMode === "list" && (
            <ListRows testId="tools-list">
              {filteredTools.map((tool) => {
                const tone =
                  tool.side_effect_level === "external_action"
                    ? "bg-red-50 text-red-600"
                    : tool.side_effect_level === "write"
                      ? "bg-amber-50 text-amber-600"
                      : "bg-emerald-50 text-emerald-600";
                return (
                  <ListRow
                    key={tool.tool_id}
                    testId={`tool-list-row-${tool.name}`}
                    title_attr="Open this tool's workspace"
                    onClick={() => setOpenToolId(tool.tool_id)}
                    icon={
                      <span className={`grid h-9 w-9 place-items-center rounded-xl ${tone}`}>
                        <svg
                          className="h-4 w-4"
                          viewBox="0 0 24 24"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="1.75"
                        >
                          <path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z" />
                        </svg>
                      </span>
                    }
                    title={<span className="font-mono">{tool.name}</span>}
                    subtitle={
                      tool.description ||
                      "No description yet. Register a tool summary so downstream users know when to call it."
                    }
                    columns={
                      <>
                        <span className="rounded-full bg-slate-50 px-2 py-0.5 text-[10px] font-semibold text-slate-500 ring-1 ring-slate-200/50">
                          v{tool.version}
                        </span>
                        <span className="w-20 font-mono text-slate-400">{tool.status}</span>
                        <span className="rounded-full bg-slate-50 px-2 py-0.5 text-[10px] font-semibold text-slate-600 ring-1 ring-slate-200/60">
                          {SIDE_EFFECT_BADGE[tool.side_effect_level] ?? tool.side_effect_level}
                        </span>
                        <span className="w-28 truncate" title={tool.owner || undefined}>
                          {tool.owner || "—"}
                        </span>
                      </>
                    }
                    actions={
                      <button
                        type="button"
                        data-testid={`tool-open-${tool.name}`}
                        onClick={(e) => {
                          e.stopPropagation();
                          setOpenToolId(tool.tool_id);
                        }}
                        className="shrink-0 px-2 text-xs font-semibold text-caliber-purple hover:underline"
                      >
                        Open
                      </button>
                    }
                  />
                );
              })}
            </ListRows>
          )}

          {filteredTools.length > 0 && viewMode === "grid" && (
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
              {filteredTools.map((tool) => {
                const tone =
                  tool.side_effect_level === "external_action"
                    ? "bg-red-50 text-red-600"
                    : tool.side_effect_level === "write"
                      ? "bg-amber-50 text-amber-600"
                      : "bg-emerald-50 text-emerald-600";
                return (
                  <div
                    key={tool.tool_id}
                    data-testid={`tool-row-${tool.name}`}
                    title="Double-click to open this tool's workspace"
                    onDoubleClick={() => setOpenToolId(tool.tool_id)}
                    className="group card flex h-full cursor-pointer flex-col p-5 transition-all duration-300 hover:-translate-y-0.5 hover:shadow-card-hover"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex min-w-0 items-start gap-3">
                        <span className={`grid h-10 w-10 shrink-0 place-items-center rounded-xl ${tone}`}>
                          <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
                            <path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z" />
                          </svg>
                        </span>
                        <div className="min-w-0">
                          <button
                            type="button"
                            onClick={() => setOpenToolId(tool.tool_id)}
                            className="block max-w-full truncate text-left font-mono text-sm font-semibold text-slate-900 transition-colors group-hover:text-caliber-purple"
                          >
                            {tool.name}
                          </button>
                          <div className="mt-0.5 flex flex-wrap items-center gap-1.5">
                            <span className="rounded-full bg-slate-50 px-2 py-0.5 text-[10px] font-semibold text-slate-500 ring-1 ring-slate-200/50">
                              v{tool.version}
                            </span>
                            <span className="font-mono text-[10px] text-slate-400">{tool.status}</span>
                          </div>
                        </div>
                      </div>
                      <span className="rounded-full bg-slate-50 px-2 py-0.5 text-[10px] font-semibold text-slate-600 ring-1 ring-slate-200/60">
                        {SIDE_EFFECT_BADGE[tool.side_effect_level] ?? tool.side_effect_level}
                      </span>
                    </div>

                    <p className="mt-3 line-clamp-3 text-xs leading-relaxed text-slate-500">
                      {tool.description || "No description yet. Register a tool summary so downstream users know when to call it."}
                    </p>

                    <div className="mt-3 flex flex-wrap gap-1.5">
                      <span className="rounded-md bg-slate-50 px-2 py-0.5 text-[10px] font-semibold text-slate-500 ring-1 ring-slate-200/50">
                        {tool.requires_approval ? "Approval required" : "No approval"}
                      </span>
                      <span className="rounded-md bg-slate-50 px-2 py-0.5 text-[10px] font-semibold text-slate-500 ring-1 ring-slate-200/50">
                        {tool.allow_in_preview ? "Preview allowed" : "Preview blocked"}
                      </span>
                      {tool.owner ? (
                        <span className="rounded-md bg-slate-50 px-2 py-0.5 text-[10px] font-semibold text-slate-500 ring-1 ring-slate-200/50">
                          {tool.owner}
                        </span>
                      ) : null}
                    </div>

                    <div className="mt-auto flex items-center justify-between gap-3 border-t border-slate-100 pt-3 text-[11px] text-slate-400">
                      <span className="truncate font-mono">
                        {tool.module_path}.{tool.callable_name}
                      </span>
                      <button
                        type="button"
                        data-testid={`tool-open-${tool.name}`}
                        onClick={() => setOpenToolId(tool.tool_id)}
                        className="shrink-0 font-semibold text-caliber-purple hover:underline"
                      >
                        Open
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </>
      )}
    </div>
  );
}

/* ── Tool Workspace — open a tool → header + six stage tabs ───────────────
 *
 * Mirrors the prompt Workspace: a status header (name · version · side-effect ·
 * lifecycle pill) over ``PageTabs`` whose stages reuse the existing tool pieces,
 * each scoped to the one open tool. ``getToolWorkspace`` powers the header and
 * is refetched after a run / set-baseline / archive so the lifecycle pill keeps
 * up. */
function ToolWorkspace({
  toolId,
  tool,
  onBack,
}: {
  toolId: string;
  tool: ToolDefinition | null;
  onBack: () => void;
}): JSX.Element {
  const [stage, setStage] = useState<ToolStage>("spec");
  const [workspace, setWorkspace] = useState<ToolWorkspaceResponse | null>(null);
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);

  // The detail fetch backs stages that need the full tool record (fixtures,
  // publish, spec); the list-supplied ``tool`` seeds the header immediately.
  const detailQuery = useApiQuery(
    ["tool", toolId],
    (s) => caliberApi.getTool(toolId, s),
    { enabled: Boolean(toolId) },
  );
  const resolvedTool = detailQuery.data ?? tool;

  const refreshWorkspace = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const data = await caliberApi.getToolWorkspace(toolId, signal);
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
    [toolId],
  );

  useEffect(() => {
    const controller = new AbortController();
    void refreshWorkspace(controller.signal);
    return () => controller.abort();
  }, [refreshWorkspace]);

  const headerName = resolvedTool?.name ?? toolId;
  const headerVersion = workspace?.version ?? resolvedTool?.version ?? null;
  const headerSideEffect =
    workspace?.side_effect_level ?? resolvedTool?.side_effect_level ?? "read";
  const headerLifecycle = workspace?.lifecycle ?? "Draft";

  return (
    <div className="space-y-6 animate-fade-in">
      <div>
        <button
          type="button"
          onClick={onBack}
          className="mb-3 inline-flex items-center gap-1.5 text-sm font-medium text-slate-500 hover:text-slate-700"
        >
          <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
            <path d="M19 12H5M12 19l-7-7 7-7" />
          </svg>
          Back to tools
        </button>

        {/* ── Status header ── */}
        <div
          data-testid="tool-workspace-header"
          className="flex flex-wrap items-center justify-between gap-4 rounded-2xl border border-slate-200/70 bg-white p-5 shadow-card"
        >
          <div className="flex min-w-0 items-center gap-3">
            <span className="grid h-11 w-11 shrink-0 place-items-center rounded-xl bg-violet-50 text-caliber-purple">
              <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.85">
                <path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z" />
              </svg>
            </span>
            <div className="min-w-0">
              <h1 className="truncate font-mono text-xl font-bold tracking-tight text-slate-900">
                {headerName}
              </h1>
              <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-slate-500">
                <span>
                  Version:{" "}
                  <span className="font-mono text-slate-700">
                    {headerVersion != null ? `v${headerVersion}` : "—"}
                  </span>
                </span>
                <span className="text-slate-300">·</span>
                <span className="rounded-full bg-slate-50 px-2 py-0.5 text-[10px] font-semibold text-slate-600 ring-1 ring-slate-200/60">
                  {SIDE_EFFECT_BADGE[headerSideEffect] ?? headerSideEffect}
                </span>
              </div>
            </div>
          </div>
          <ToolStatusBadge status={headerLifecycle} />
        </div>

        {workspaceError && (
          <div className="mt-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
            {workspaceError}
          </div>
        )}
      </div>

      <PageTabs
        tabs={TOOL_WORKSPACE_STAGES}
        active={stage}
        onChange={(k) => setStage(k as ToolStage)}
      />

      {/* ── Stage content — each reused piece is scoped to the open tool ── */}
      {stage === "spec" && (
        <ToolSpecStage toolId={toolId} tool={resolvedTool} />
      )}

      {stage === "sandbox" && resolvedTool && (
        <ToolPlayground
          tools={[resolvedTool]}
          loading={detailQuery.isLoading}
          onRun={async (toolDef, input, result) => {
            // Each sandbox invoke is also captured as a durable kind:"sandbox"
            // run (one-case results array) so it shows under Test Runs.
            try {
              await caliberApi.saveToolTestRun({
                tool_id: toolDef.tool_id,
                kind: "sandbox",
                tool_version: toolDef.version,
                results: [
                  {
                    name: "sandbox invoke",
                    input,
                    output: result.output,
                    error: result.error,
                    verdict: result.error ? "fail" : "pass",
                    score: result.error ? 0 : 1,
                    duration_ms: result.duration_ms,
                    reasoning: result.error
                      ? "Sandbox invocation errored."
                      : "Sandbox invocation returned without error.",
                  },
                ],
              });
              void refreshWorkspace();
            } catch {
              /* persistence is best-effort; the inline result still shows */
            }
          }}
        />
      )}

      {stage === "fixtures" && resolvedTool && (
        <ToolFixturesStage
          tool={resolvedTool}
          onSaved={() => {
            void detailQuery.refetch();
            void refreshWorkspace();
          }}
        />
      )}

      {stage === "runs" && (
        <ToolRunsStage
          toolId={toolId}
          workspace={workspace}
          onAfterRun={() => void refreshWorkspace()}
        />
      )}

      {stage === "hardening" && resolvedTool && (
        <ToolHardeningStage
          tool={resolvedTool}
          onSaved={() => {
            void detailQuery.refetch();
            void refreshWorkspace();
          }}
          onAfterRun={() => void refreshWorkspace()}
        />
      )}

      {stage === "publish" && (
        <ToolPublishStage
          toolId={toolId}
          tool={resolvedTool}
          onChanged={() => {
            void detailQuery.refetch();
            void refreshWorkspace();
          }}
        />
      )}

      {(stage === "sandbox" ||
        stage === "fixtures" ||
        stage === "hardening") &&
        !resolvedTool && (
          <div className="rounded-2xl border border-dashed border-slate-200 bg-slate-50 px-8 py-12 text-center text-sm text-slate-500">
            Loading tool…
          </div>
        )}
    </div>
  );
}

/* ── Tool Playground ─────────────────────────────────────────────────── */

export function ToolPlayground({
  tools,
  loading,
  onRun,
}: {
  tools: ToolDefinition[];
  loading: boolean;
  /**
   * Optional hook fired after every invoke (success or error) with the parsed
   * input + result. The Workspace uses it to persist the run durably; standalone
   * renders (and tests) omit it and keep only the fast inline result + history.
   */
  onRun?: (
    tool: ToolDefinition,
    input: Record<string, unknown>,
    result: ToolTestRunResult,
  ) => void;
}): JSX.Element {
  const [selectedId, setSelectedId] = useState("");
  const selected = tools.find((t) => t.tool_id === selectedId) ?? null;
  const [toolInput, setToolInput] = useState("{}");
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<ToolTestRunResult | null>(null);
  const [history, setHistory] = useState<
    { tool: string; input: string; result: ToolTestRunResult; timestamp: number }[]
  >([]);

  // Auto-select first tool
  if (!selectedId && tools.length > 0) {
    setSelectedId(tools[0]!.tool_id);
  }

  const handleRun = async () => {
    if (!selected) return;
    setRunning(true);
    setResult(null);
    let parsed: Record<string, unknown> = {};
    try {
      try {
        parsed = JSON.parse(toolInput);
      } catch {
        /* leave empty */
      }
      const res = await caliberApi.testRunTool(selected.tool_id, parsed);
      setResult(res);
      setHistory((h) => [
        { tool: selected.name, input: toolInput, result: res, timestamp: Date.now() },
        ...h.slice(0, 19),
      ]);
      onRun?.(selected, parsed, res);
    } catch {
      const err: ToolTestRunResult = {
        tool_id: selected.tool_id,
        output: null,
        mocked: false,
        duration_ms: 0,
        error: "Request failed",
      };
      setResult(err);
      onRun?.(selected, parsed, err);
    } finally {
      setRunning(false);
    }
  };

  if (loading && tools.length === 0) {
    return <div className="text-sm text-zinc-400 animate-pulse py-10 text-center">Loading tools…</div>;
  }

  if (tools.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-zinc-300 bg-zinc-50 p-8 text-center">
        <p className="text-sm text-zinc-500 mb-2">No tools registered yet.</p>
        <p className="text-sm text-zinc-400">Switch to the Registry tab to register one.</p>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
      {/* ── LEFT: tool picker + details ── */}
      <div className="lg:col-span-4 space-y-4">
        <div>
          <label className="block text-xs font-medium text-zinc-700 mb-1">Tool</label>
          <select
            aria-label="Select tool"
            value={selectedId}
            onChange={(e) => {
              setSelectedId(e.target.value);
              setResult(null);
              setToolInput("{}");
            }}
            className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none"
          >
            {tools.map((t) => (
              <option key={t.tool_id} value={t.tool_id}>{t.name}</option>
            ))}
          </select>
        </div>

        {selected && (
          <div className="rounded-lg border border-zinc-200 bg-white p-4 space-y-3">
            <h3 className="text-xs font-semibold text-zinc-800 uppercase tracking-wider">Tool Details</h3>
            <dl className="space-y-2 text-xs">
              <div className="flex justify-between">
                <dt className="text-zinc-500">Name</dt>
                <dd className="font-mono text-zinc-800">{selected.name}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-zinc-500">Version</dt>
                <dd className="font-mono text-zinc-800">{selected.version}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-zinc-500">Side Effect</dt>
                <dd>{SIDE_EFFECT_BADGE[selected.side_effect_level] ?? selected.side_effect_level}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-zinc-500">Approval</dt>
                <dd className="text-zinc-800">{selected.requires_approval ? "Required" : "Not required"}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-zinc-500">Preview</dt>
                <dd className="text-zinc-800">{selected.allow_in_preview ? "Allowed" : "Blocked"}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-zinc-500">Status</dt>
                <dd className="text-zinc-800">{selected.status}</dd>
              </div>
            </dl>
            {selected.description && (
              <p className="text-xs text-zinc-500 pt-2 border-t border-zinc-100">{selected.description}</p>
            )}
            <div className="space-y-3 border-t border-zinc-100 pt-3">
              <ToolSignature
                title="Input Signature"
                schema={selected.input_schema}
                emptyLabel="No input schema declared; send any JSON object."
                testId="tool-input-signature"
              />
              <ToolSignature
                title="Output Signature"
                schema={selected.output_schema}
                emptyLabel="No output schema declared; inspect the test-run result."
                testId="tool-output-signature"
              />
            </div>
          </div>
        )}
      </div>

      {/* ── RIGHT: input + run + result ── */}
      <div className="lg:col-span-8 space-y-4">
        <div className="rounded-lg border border-zinc-200 bg-white p-4">
          <div className="flex items-center gap-3 mb-3">
            <div className="w-8 h-8 rounded-lg bg-blue-50 border border-blue-100 flex items-center justify-center">
              <svg className="w-4 h-4 text-blue-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z" />
              </svg>
            </div>
            <div>
              <h3 className="text-sm font-semibold text-zinc-900 font-mono">{selected?.name ?? "—"}</h3>
              <p className="text-xs text-zinc-500">Sandbox test-run — {selected?.side_effect_level === "read" && selected.allow_in_preview ? "live execution" : "mocked output"}</p>
            </div>
          </div>

          <div className="mb-3">
            <label className="block text-xs font-medium text-zinc-700 mb-1">Input JSON</label>
            <textarea
              value={toolInput}
              onChange={(e) => setToolInput(e.target.value)}
              rows={5}
              placeholder='{"key": "value"}'
              className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm font-mono focus:border-blue-400 focus:ring-1 focus:ring-blue-400 outline-none resize-y"
            />
          </div>

          <button
            onClick={handleRun}
            disabled={running || !selected}
            className="w-full flex items-center justify-center gap-2 rounded-md bg-emerald-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-emerald-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {running ? (
              <>
                <svg className="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
                Running…
              </>
            ) : (
              <>
                <svg className="w-4 h-4" viewBox="0 0 20 20" fill="currentColor">
                  <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM9.555 7.168A1 1 0 008 8v4a1 1 0 001.555.832l3-2a1 1 0 000-1.664l-3-2z" clipRule="evenodd" />
                </svg>
                Test Run
              </>
            )}
          </button>
        </div>

        {/* result card */}
        {result && (
          <div className={`rounded-lg border ${result.error ? "border-red-200" : "border-emerald-200"}`}>
            <div className={`px-4 py-3 border-b ${result.error ? "bg-red-50 border-red-200" : "bg-emerald-50 border-emerald-200"} rounded-t-lg`}>
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  {result.error ? (
                    <svg className="w-4 h-4 text-red-600" viewBox="0 0 20 20" fill="currentColor"><path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clipRule="evenodd" /></svg>
                  ) : (
                    <svg className="w-4 h-4 text-emerald-600" viewBox="0 0 20 20" fill="currentColor"><path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clipRule="evenodd" /></svg>
                  )}
                  <span className={`text-xs font-semibold ${result.error ? "text-red-800" : "text-emerald-800"}`}>
                    {result.error ? "Error" : "Success"}{result.mocked ? " (mocked)" : ""}
                  </span>
                </div>
                <span className={`text-[10px] font-mono ${result.error ? "text-red-600" : "text-emerald-600"}`}>{result.duration_ms}ms</span>
              </div>
              {result.error && <p className="text-xs text-red-700 mt-1 font-mono">{result.error}</p>}
            </div>
            {result.output != null && (
              <pre className="px-4 py-3 text-[11px] text-zinc-700 font-mono overflow-x-auto bg-white rounded-b-lg max-h-72 overflow-y-auto whitespace-pre-wrap break-words">
                {JSON.stringify(result.output, null, 2)}
              </pre>
            )}
          </div>
        )}

        {/* history */}
        {history.length > 0 && (
          <div className="rounded-lg border border-zinc-200 bg-white">
            <div className="px-4 py-3 border-b border-zinc-100 flex items-center justify-between">
              <h3 className="text-xs font-semibold text-zinc-800 uppercase tracking-wider">History</h3>
              <button onClick={() => setHistory([])} className="text-[10px] text-zinc-400 hover:text-zinc-600">Clear</button>
            </div>
            <div className="divide-y divide-zinc-100 max-h-48 overflow-y-auto">
              {history.map((entry, i) => (
                <div
                  key={`${entry.tool}-${entry.timestamp}-${i}`}
                  className="px-4 py-2.5 flex items-center justify-between text-xs cursor-pointer hover:bg-zinc-50"
                  onClick={() => {
                    const t = tools.find((x) => x.name === entry.tool);
                    if (t) {
                      setSelectedId(t.tool_id);
                      setToolInput(entry.input);
                      setResult(entry.result);
                    }
                  }}
                >
                  <div className="flex items-center gap-2">
                    <span className={`w-1.5 h-1.5 rounded-full ${entry.result.error ? "bg-red-500" : "bg-emerald-500"}`} />
                    <span className="font-mono text-zinc-800">{entry.tool}</span>
                  </div>
                  <div className="flex items-center gap-2 text-zinc-400">
                    <span>{entry.result.duration_ms}ms</span>
                    <span>{new Date(entry.timestamp).toLocaleTimeString()}</span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/* ── Tool Tests ──────────────────────────────────────────────────────── */

interface GeneratedToolTestCase {
  id: string;
  input: Record<string, unknown>;
  expectedOutput: unknown;
  expectedBehavior: string;
  tags: string[];
}

interface ToolUnitTestResult {
  testCaseId: string;
  actualOutput: unknown;
  error: string | null;
  verdict: "pass" | "fail" | "partial";
  score: number;
  reasoning: string;
  durationMs: number;
}

const MIN_TOOL_TEST_CASE_COUNT = 1;
const MAX_TOOL_TEST_CASE_COUNT = 50;

export function clampToolTestCount(value: number): number {
  if (!Number.isFinite(value)) return MIN_TOOL_TEST_CASE_COUNT;
  return Math.max(MIN_TOOL_TEST_CASE_COUNT, Math.min(MAX_TOOL_TEST_CASE_COUNT, Math.round(value)));
}

export function extractJsonArray(raw: string): unknown[] {
  const match = raw.match(/\[[\s\S]*\]/);
  if (!match) throw new Error("LLM did not return a JSON array.");
  const parsed = JSON.parse(match[0]) as unknown;
  if (!Array.isArray(parsed)) throw new Error("Generated tests must be a JSON array.");
  return parsed;
}

export function extractJsonObject(raw: string): Record<string, unknown> | null {
  const match = raw.match(/\{[\s\S]*\}/);
  if (!match) return null;
  const parsed = JSON.parse(match[0]) as unknown;
  return parsed && typeof parsed === "object" && !Array.isArray(parsed)
    ? parsed as Record<string, unknown>
    : null;
}

export function normalizeToolTestCases(raw: unknown[]): GeneratedToolTestCase[] {
  return raw.map((row, index) => {
    if (!row || typeof row !== "object" || Array.isArray(row)) {
      throw new Error(`Test case ${index + 1} must be an object.`);
    }
    const payload = row as Record<string, unknown>;
    const input = payload.input;
    if (!input || typeof input !== "object" || Array.isArray(input)) {
      throw new Error(`Test case ${index + 1} needs an object input.`);
    }
    const expectedBehavior = typeof payload.expectedBehavior === "string"
      ? payload.expectedBehavior.trim()
      : "";
    const expectedOutput = payload.expectedOutput ?? payload.output ?? {};
    if (!expectedBehavior && expectedOutput === undefined) {
      throw new Error(`Test case ${index + 1} needs expectedOutput or expectedBehavior.`);
    }
    const tags = Array.isArray(payload.tags)
      ? payload.tags.filter((tag): tag is string => typeof tag === "string" && tag.trim().length > 0)
      : [];
    return {
      id: `tool-tc-${Date.now()}-${index}`,
      input: input as Record<string, unknown>,
      expectedOutput,
      expectedBehavior,
      tags,
    };
  });
}

export function ToolTests({
  tools,
  loading,
  onRun,
}: {
  tools: ToolDefinition[];
  loading: boolean;
  /**
   * Optional hook fired once a full LLM generate+judge run completes, with the
   * generated cases + their judged results. The Hardening tab uses it to persist
   * the run durably (kind:"hardening"); standalone renders omit it.
   */
  onRun?: (
    tool: ToolDefinition,
    cases: GeneratedToolTestCase[],
    results: ToolUnitTestResult[],
  ) => void;
}): JSX.Element {
  const [selectedId, setSelectedId] = useState("");
  const selected = tools.find((tool) => tool.tool_id === selectedId) ?? null;
  const [selectedModel, setSelectedModel] = useState("");
  const [config, setConfig] = useState<AssistantConfig | null>(null);
  const [configLoading, setConfigLoading] = useState(true);
  const [count, setCount] = useState(5);
  const [cases, setCases] = useState<GeneratedToolTestCase[]>([]);
  const [results, setResults] = useState<ToolUnitTestResult[]>([]);
  const [generating, setGenerating] = useState(false);
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState({ current: 0, total: 0 });
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    caliberApi.getAssistantConfig()
      .then((value) => {
        if (cancelled) return;
        setConfig(value);
        setSelectedModel(value.model);
        setConfigLoading(false);
      })
      .catch(() => {
        if (!cancelled) setConfigLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!selectedId && tools.length > 0) {
    setSelectedId(tools[0]!.tool_id);
  }

  const setCountClamped = (next: number): void => {
    setCount(clampToolTestCount(next));
  };

  const syncModel = async (): Promise<void> => {
    if (config && selectedModel && selectedModel !== config.model) {
      const updated = await caliberApi.updateAssistantConfig({ model: selectedModel });
      setConfig(updated);
    }
  };

  const generate = async (): Promise<void> => {
    if (!selected) return;
    setGenerating(true);
    setError(null);
    try {
      await syncModel();
      const requestedCount = clampToolTestCount(count);
      const goal = [
        `Generate exactly ${requestedCount} unit test cases for the CALIBER tool "${selected.name}".`,
        `The tests must include realistic input data and expected output data.`,
        ``,
        `## Tool`,
        `Description: ${selected.description || "(none)"}`,
        `Side effect level: ${selected.side_effect_level}`,
        `Requires approval: ${selected.requires_approval}`,
        ``,
        `## Input JSON Schema`,
        JSON.stringify(selected.input_schema ?? {}, null, 2),
        ``,
        `## Output JSON Schema`,
        JSON.stringify(selected.output_schema ?? {}, null, 2),
        ``,
        `Respond with ONLY a valid JSON array. Each item must include:`,
        `- "input": JSON object compatible with the input schema`,
        `- "expectedOutput": JSON value or object compatible with the output schema`,
        `- "expectedBehavior": concise assertion text`,
        `- "tags": array of 1-3 short tags`,
      ].join("\n");
      const session = await caliberApi.createAssistantSession({
        title: `Tool Unit Tests: ${selected.name}`,
        goal,
        artifact_type: "tool",
      });
      const turn = await caliberApi.sendAssistantMessage(session.session_id, {
        content: "Generate the unit test cases now.",
        artifact_type: "tool",
      });
      setCases(normalizeToolTestCases(extractJsonArray(turn.assistant_message.content)));
      setResults([]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to generate tool tests");
    } finally {
      setGenerating(false);
    }
  };

  const run = async (): Promise<void> => {
    if (!selected || cases.length === 0) return;
    setRunning(true);
    setError(null);
    setResults([]);
    setProgress({ current: 0, total: cases.length });
    try {
      await syncModel();
      const nextResults: ToolUnitTestResult[] = [];
      for (let i = 0; i < cases.length; i += 1) {
        const testCase = cases[i]!;
        setProgress({ current: i + 1, total: cases.length });
        let actual: ToolTestRunResult | null = null;
        try {
          actual = await caliberApi.testRunTool(selected.tool_id, testCase.input);
        } catch (err) {
          actual = {
            tool_id: selected.tool_id,
            output: null,
            mocked: false,
            duration_ms: 0,
            error: err instanceof Error ? err.message : "Tool run failed",
          };
        }

        let verdict: ToolUnitTestResult["verdict"] = actual.error ? "fail" : "partial";
        let score = actual.error ? 0 : 0.5;
        let reasoning = actual.error ?? "Judge response was not available.";
        if (!actual.error) {
          try {
            const judgeGoal = [
              `Judge whether a CALIBER tool test passed.`,
              `Tool: ${selected.name}`,
              ``,
              `## Input`,
              JSON.stringify(testCase.input, null, 2),
              ``,
              `## Expected Output`,
              JSON.stringify(testCase.expectedOutput, null, 2),
              ``,
              `## Expected Behavior`,
              testCase.expectedBehavior || "(none)",
              ``,
              `## Actual Output`,
              JSON.stringify(actual.output, null, 2),
              ``,
              `Respond with ONLY JSON: {"verdict":"pass"|"fail"|"partial","score":0.0-1.0,"reasoning":"brief explanation"}`,
            ].join("\n");
            const judgeSession = await caliberApi.createAssistantSession({
              title: `Judge Tool Test: ${selected.name} #${i + 1}`,
              goal: judgeGoal,
              artifact_type: "tool",
            });
            const judgeTurn = await caliberApi.sendAssistantMessage(judgeSession.session_id, {
              content: "Judge this tool test now.",
              artifact_type: "tool",
            });
            const judged = extractJsonObject(judgeTurn.assistant_message.content);
            if (judged) {
              const rawVerdict = judged.verdict;
              verdict = rawVerdict === "pass" || rawVerdict === "fail" || rawVerdict === "partial"
                ? rawVerdict
                : "fail";
              score = typeof judged.score === "number" ? Math.max(0, Math.min(1, judged.score)) : 0;
              reasoning = typeof judged.reasoning === "string" ? judged.reasoning : "No reasoning returned.";
            }
          } catch (err) {
            reasoning = err instanceof Error ? err.message : "Judge failed";
          }
        }
        nextResults.push({
          testCaseId: testCase.id,
          actualOutput: actual.output,
          error: actual.error,
          verdict,
          score,
          reasoning,
          durationMs: actual.duration_ms,
        });
        setResults([...nextResults]);
      }
      // Hand the completed generate+judge run to the Workspace so it can persist
      // a durable kind:"hardening" run (no-op for standalone renders).
      onRun?.(selected, cases, nextResults);
    } finally {
      setRunning(false);
    }
  };

  if (loading && tools.length === 0) {
    return <div className="text-sm text-zinc-400 animate-pulse py-10 text-center">Loading tools…</div>;
  }

  if (tools.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-zinc-300 bg-zinc-50 p-8 text-center">
        <p className="text-sm text-zinc-500 mb-2">No tools registered yet.</p>
        <p className="text-sm text-zinc-400">Register a tool before generating unit tests.</p>
      </div>
    );
  }

  const averageScore = results.length
    ? results.reduce((sum, item) => sum + item.score, 0) / results.length
    : null;

  return (
    <div className="space-y-6" data-testid="tool-tests-panel">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <label className="block">
          <span className="block text-xs font-medium text-zinc-700 mb-1">Tool</span>
          <select
            aria-label="Select tool for tests"
            value={selectedId}
            onChange={(event) => {
              setSelectedId(event.target.value);
              setCases([]);
              setResults([]);
            }}
            disabled={generating || running}
            className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none disabled:opacity-50"
          >
            {tools.map((tool) => (
              <option key={tool.tool_id} value={tool.tool_id}>{tool.name}</option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="block text-xs font-medium text-zinc-700 mb-1">LLM Model</span>
          {configLoading ? (
            <div className="text-xs text-zinc-400 py-2">Loading models…</div>
          ) : (
            <select
              aria-label="Select test generation model"
              value={selectedModel}
              onChange={(event) => setSelectedModel(event.target.value)}
              disabled={generating || running}
              className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none disabled:opacity-50"
            >
              {config?.available_models.map((model: AssistantModelOption) => (
                <option key={model.id} value={model.id}>{model.name} ({model.provider})</option>
              ))}
            </select>
          )}
        </label>
        <div>
          <span className="block text-xs font-medium text-zinc-700 mb-1">Number of Unit Tests</span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              aria-label="Decrease tool test count"
              disabled={generating || running || count <= MIN_TOOL_TEST_CASE_COUNT}
              onClick={() => setCountClamped(count - 1)}
              className="rounded-md border border-zinc-300 bg-white px-2.5 py-2 text-sm font-semibold disabled:opacity-50"
            >
              -
            </button>
            <input
              aria-label="Number of tool unit tests"
              type="number"
              min={MIN_TOOL_TEST_CASE_COUNT}
              max={MAX_TOOL_TEST_CASE_COUNT}
              value={count}
              disabled={generating || running}
              onChange={(event) => setCountClamped(Number(event.target.value))}
              className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none disabled:opacity-50"
            />
            <button
              type="button"
              aria-label="Increase tool test count"
              disabled={generating || running || count >= MAX_TOOL_TEST_CASE_COUNT}
              onClick={() => setCountClamped(count + 1)}
              className="rounded-md border border-zinc-300 bg-white px-2.5 py-2 text-sm font-semibold disabled:opacity-50"
            >
              +
            </button>
          </div>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          data-testid="tool-tests-generate"
          onClick={() => void generate()}
          disabled={!selected || generating || running || configLoading}
          className="rounded-md bg-caliber-600 px-4 py-2 text-sm font-medium text-white hover:bg-caliber-700 disabled:opacity-50"
        >
          {generating ? "Generating…" : "Generate Input/Output Tests"}
        </button>
        <button
          type="button"
          data-testid="tool-tests-run"
          onClick={() => void run()}
          disabled={cases.length === 0 || running || generating}
          className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
        >
          {running ? `Running ${progress.current}/${progress.total}…` : "Run Unit Tests"}
        </button>
        {averageScore !== null && (
          <span className="rounded-full bg-zinc-100 px-3 py-1 text-xs font-semibold text-zinc-700">
            Average score {(averageScore * 100).toFixed(0)}%
          </span>
        )}
      </div>

      {error && (
        <div className="rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
        </div>
      )}

      {selected && (
        <div className="grid gap-3 md:grid-cols-2">
          <ToolSignature
            title="Input Signature"
            schema={selected.input_schema}
            emptyLabel="No input schema declared."
          />
          <ToolSignature
            title="Output Signature"
            schema={selected.output_schema}
            emptyLabel="No output schema declared."
          />
        </div>
      )}

      {cases.length === 0 ? (
        <div className="rounded-lg border border-dashed border-zinc-300 bg-zinc-50 p-8 text-center">
          <p className="text-sm font-medium text-zinc-600 mb-1">No generated unit tests yet.</p>
          <p className="text-xs text-zinc-400">Generate cases to create input fixtures and expected outputs for this tool.</p>
        </div>
      ) : (
        <div className="space-y-3">
          {cases.map((testCase, index) => {
            const result = results.find((item) => item.testCaseId === testCase.id);
            return (
              <div key={testCase.id} className="rounded-lg border border-zinc-200 bg-white p-4">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="text-sm font-semibold text-zinc-900">Unit Test {index + 1}</div>
                    <div className="mt-1 text-xs text-zinc-500">{testCase.expectedBehavior || "Output shape assertion"}</div>
                  </div>
                  {result && (
                    <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${
                      result.verdict === "pass"
                        ? "bg-emerald-50 text-emerald-700"
                        : result.verdict === "partial"
                          ? "bg-amber-50 text-amber-700"
                          : "bg-red-50 text-red-700"
                    }`}>
                      {result.verdict} · {(result.score * 100).toFixed(0)}%
                    </span>
                  )}
                </div>
                <div className="mt-3 grid gap-3 lg:grid-cols-3">
                  <pre className="rounded-md bg-zinc-50 p-3 text-[11px] text-zinc-700 overflow-x-auto">
                    {JSON.stringify({ input: testCase.input }, null, 2)}
                  </pre>
                  <pre className="rounded-md bg-zinc-50 p-3 text-[11px] text-zinc-700 overflow-x-auto">
                    {JSON.stringify({ expectedOutput: testCase.expectedOutput }, null, 2)}
                  </pre>
                  <pre className="rounded-md bg-zinc-50 p-3 text-[11px] text-zinc-700 overflow-x-auto">
                    {JSON.stringify({ actualOutput: result?.actualOutput ?? null, error: result?.error ?? null }, null, 2)}
                  </pre>
                </div>
                {testCase.tags.length > 0 && (
                  <div className="mt-3 flex flex-wrap gap-1">
                    {testCase.tags.map((tag) => (
                      <span key={tag} className="rounded bg-zinc-100 px-1.5 py-0.5 text-[10px] text-zinc-500">
                        {tag}
                      </span>
                    ))}
                  </div>
                )}
                {result && (
                  <p className="mt-3 text-xs text-zinc-500">{result.reasoning}</p>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/* ── Spec stage — tool details, schema, side-effect, flags, source ──────── */

function ToolSpecRow({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}): JSX.Element {
  return (
    <div className="flex justify-between gap-3">
      <span className="text-zinc-500">{label}</span>
      <span className="text-right text-zinc-800">{children}</span>
    </div>
  );
}

function ToolSpecStage({
  toolId,
  tool,
}: {
  toolId: string;
  tool: ToolDefinition | null;
}): JSX.Element {
  const sourceQuery = useApiQuery(
    ["tool-source", toolId],
    (s) => caliberApi.getToolSource(toolId, s),
    { enabled: Boolean(toolId) },
  );
  const source: ToolSource | undefined = sourceQuery.data;

  if (!tool) {
    return (
      <div className="text-sm text-zinc-400 animate-pulse py-10 text-center">Loading tool…</div>
    );
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(320px,420px)]">
      {/* Implementation: signature, schemas, docstring, source */}
      <section data-testid="tool-spec-implementation" className="rounded-lg border border-zinc-200 bg-white p-4">
        <h3 className="text-sm font-semibold text-zinc-900">Implementation</h3>
        <p className="mt-0.5 text-xs text-zinc-500">{tool.description || "No description."}</p>

        {source?.signature && (
          <div className="mt-3">
            <div className="text-xs font-semibold text-gray-500">Signature — what to pass when you call it</div>
            <CodeBlock
              code={`def ${source.signature}`}
              language="python"
              testId="tool-spec-signature"
              className="mt-1 overflow-x-auto rounded border border-gray-200 bg-gray-50 p-2"
            />
          </div>
        )}

        <div className="mt-3 grid gap-3 lg:grid-cols-2">
          <ToolSignature
            title="Input Signature"
            schema={tool.input_schema}
            emptyLabel="No input schema declared; send any JSON object."
            testId="tool-spec-input-signature"
          />
          <ToolSignature
            title="Output Signature"
            schema={tool.output_schema}
            emptyLabel="No output schema declared; inspect a sandbox result."
            testId="tool-spec-output-signature"
          />
        </div>

        {source?.doc && (
          <p className="mt-3 whitespace-pre-wrap text-xs text-gray-600">{source.doc}</p>
        )}

        <div className="mt-3">
          <div className="text-xs font-semibold text-gray-500">Source</div>
          {sourceQuery.isLoading && <div className="mt-1 text-xs text-gray-400">Loading source…</div>}
          {source?.available ? (
            <CodeBlock
              code={source.source}
              language="python"
              testId="tool-spec-source"
              className="mt-1 max-h-[420px] overflow-auto rounded border border-gray-200 bg-gray-50 p-3"
            />
          ) : (
            source && (
              <div className="mt-1 text-xs text-gray-400" data-testid="tool-spec-source-unavailable">
                Source unavailable{source.error ? `: ${source.error}` : "."}
              </div>
            )
          )}
        </div>
      </section>

      {/* Facts: ids, side effect, flags, owner, callable path */}
      <section className="rounded-lg border border-zinc-200 bg-white p-4">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-zinc-800">Tool facts</h3>
        <div className="mt-2 space-y-2 text-xs">
          <ToolSpecRow label="Tool ID">
            <span data-testid="tool-spec-id" className="font-mono">{tool.tool_id}</span>
          </ToolSpecRow>
          <ToolSpecRow label="Version">
            <span className="font-mono">v{tool.version}</span>
          </ToolSpecRow>
          <ToolSpecRow label="Side effect">
            {SIDE_EFFECT_BADGE[tool.side_effect_level] ?? tool.side_effect_level}
          </ToolSpecRow>
          <ToolSpecRow label="Requires approval">
            {tool.requires_approval ? "Required" : "Not required"}
          </ToolSpecRow>
          <ToolSpecRow label="Allow in preview">
            {tool.allow_in_preview ? "Allowed" : "Blocked"}
          </ToolSpecRow>
          <ToolSpecRow label="Owner">{tool.owner || "Unassigned"}</ToolSpecRow>
          <ToolSpecRow label="Callable">
            <span data-testid="tool-spec-callable" className="font-mono">
              {tool.module_path}.{tool.callable_name}
            </span>
          </ToolSpecRow>
          <ToolSpecRow label="Status">{tool.status}</ToolSpecRow>
        </div>
      </section>
    </div>
  );
}

/* ── Fixtures stage — saved test_cases editor (deterministic inputs + asserts)
 *
 * Reuses CalibrationPanel's case editing wired to ``saveToolTestCases``. It is
 * the storage surface only; the deterministic suite is RUN on the Hardening
 * tab (which scores these saved cases). */
function ToolFixturesStage({
  tool,
  onSaved,
}: {
  tool: ToolDefinition;
  onSaved: () => void;
}): JSX.Element {
  return (
    <div className="space-y-3">
      <div>
        <h3 className="text-sm font-semibold text-zinc-900">Fixtures</h3>
        <p className="text-xs text-zinc-500">
          Deterministic inputs + assertions saved against this tool. Score them on the Hardening tab.
        </p>
      </div>
      <CalibrationPanel
        key={tool.tool_id}
        idPrefix="tool-fixtures"
        calibrateTestId="tool-fixtures-calibrate-btn"
        initialCases={tool.test_cases ?? []}
        lastResult={tool.last_calibration ?? null}
        onSave={async (cases: CalibrationCase[]) => {
          const saved = await caliberApi.saveToolTestCases(tool.tool_id, cases);
          onSaved();
          return saved;
        }}
        onCalibrate={async (): Promise<CalibrationResult> => {
          const scored = await caliberApi.calibrateTool(tool.tool_id);
          onSaved();
          return scored;
        }}
      />
    </div>
  );
}

/* ── Test Runs stage — durable history + per-case + baseline diff/regressions
 *
 * Mirrors PromptRunsStage but reads tool runs (snake_case per-case shape) and
 * has no inline "run" button — tool runs originate on Sandbox / Hardening. A
 * kind filter narrows the list. */
const TOOL_RUN_KIND_FILTERS: Array<{ value: "" | ToolTestRunKind; label: string }> = [
  { value: "", label: "All kinds" },
  { value: "sandbox", label: "Sandbox" },
  { value: "suite", label: "Suite" },
  { value: "hardening", label: "Hardening" },
];

function ToolRunsStage({
  toolId,
  workspace,
  onAfterRun,
}: {
  toolId: string;
  workspace: ToolWorkspaceResponse | null;
  onAfterRun: () => void;
}): JSX.Element {
  const baselineRunId = workspace?.baseline_run_id ?? null;

  const [kindFilter, setKindFilter] = useState<"" | ToolTestRunKind>("");
  const [history, setHistory] = useState<ToolTestRunSummary[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(true);

  const [viewedRunId, setViewedRunId] = useState<string | null>(null);
  const [viewedDetail, setViewedDetail] = useState<ToolTestRunDetail | null>(null);
  const [viewedLoading, setViewedLoading] = useState(false);
  const [baselineDetail, setBaselineDetail] = useState<ToolTestRunDetail | null>(null);
  const [pinning, setPinning] = useState(false);
  const [pinError, setPinError] = useState<string | null>(null);

  const refreshHistory = useCallback(
    async (signal?: AbortSignal) => {
      setLoadingHistory(true);
      try {
        const runs = await caliberApi.listToolTestRuns(
          toolId,
          kindFilter || undefined,
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
    [toolId, kindFilter],
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
      .getToolTestRun(viewedRunId)
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
      .getToolTestRun(baselineRunId)
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

  // Default the viewed run to the latest once history loads.
  useEffect(() => {
    if (viewedRunId || history.length === 0) return;
    setViewedRunId(history[0]!.test_run_id);
  }, [history, viewedRunId]);

  const pinAsBaseline = async (testRunId: string): Promise<void> => {
    setPinning(true);
    setPinError(null);
    try {
      await caliberApi.setToolBaseline(toolId, testRunId);
      onAfterRun();
    } catch (err) {
      setPinError(err instanceof Error ? err.message : "Failed to set baseline");
    } finally {
      setPinning(false);
    }
  };

  const viewedIsBaseline = viewedRunId != null && viewedRunId === baselineRunId;
  const showComparison =
    baselineRunId != null && !viewedIsBaseline && viewedDetail != null && baselineDetail != null;

  // Per-case diff + regressions when a baseline exists and another run is viewed.
  const comparison = useMemo(() => {
    if (!showComparison || !viewedDetail || !baselineDetail) return null;
    const baseByName = new Map(baselineDetail.results.map((r) => [r.name, r]));
    const baseByInput = new Map(
      baselineDetail.results.map((r) => [JSON.stringify(r.input), r]),
    );
    const rows = viewedDetail.results.map((cur) => {
      const base =
        baseByName.get(cur.name) ?? baseByInput.get(JSON.stringify(cur.input)) ?? null;
      const regressed =
        base != null &&
        (base.verdict === "pass" || base.verdict === "partial") &&
        cur.verdict === "fail";
      return { cur, base, regressed };
    });
    const regressions = rows.filter((r) => r.regressed);
    const curScore = summarizeToolResults(viewedDetail.results).overallScore ?? 0;
    const baseScore = summarizeToolResults(baselineDetail.results).overallScore ?? 0;
    return { rows, regressions, scoreDelta: curScore - baseScore };
  }, [showComparison, viewedDetail, baselineDetail]);

  const viewedSummary = viewedDetail ? summarizeToolResults(viewedDetail.results) : null;

  const renderCaseValue = (value: unknown): string => {
    if (value == null) return "—";
    if (typeof value === "string") return value;
    return JSON.stringify(value, null, 2);
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-caliber-100 bg-caliber-50/60 px-4 py-3">
        <div className="text-sm text-caliber-800">
          Durable run history for this tool. Runs are captured from the{" "}
          <span className="font-medium">Sandbox</span> and{" "}
          <span className="font-medium">Hardening</span> tabs; pin one as the baseline to compare against.
        </div>
        <label className="flex items-center gap-2 text-xs text-zinc-600">
          Kind
          <select
            aria-label="Filter runs by kind"
            value={kindFilter}
            onChange={(e) => {
              setKindFilter(e.target.value as "" | ToolTestRunKind);
              setViewedRunId(null);
            }}
            className="rounded-md border border-zinc-300 bg-white px-2 py-1 text-xs focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500 outline-none"
          >
            {TOOL_RUN_KIND_FILTERS.map((opt) => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        </label>
      </div>

      {pinError && (
        <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          {pinError}
        </div>
      )}

      {/* Viewed run results + score */}
      {viewedRunId && (
        <div data-testid="tool-workspace-run-results" className="rounded-lg border border-zinc-200 bg-white p-4">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-semibold text-zinc-700">
                {viewedRunId === history[0]?.test_run_id ? "Latest run" : "Selected run"}
              </h3>
              {viewedIsBaseline ? (
                <span
                  data-testid="tool-run-baseline-marker"
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
                <span className="font-medium text-emerald-600">{viewedSummary.passCount} pass</span>
                {" · "}
                <span className="font-medium text-amber-600">{viewedSummary.partialCount} partial</span>
                {" · "}
                <span className="font-medium text-red-600">{viewedSummary.failCount} fail</span>
              </div>
            )}
          </div>

          {viewedLoading || viewedDetail == null ? (
            <div className="text-xs text-zinc-400 animate-pulse">Loading results…</div>
          ) : (
            <div className="space-y-2">
              {viewedDetail.results.map((r, i) => (
                <div
                  key={`${r.name}-${i}`}
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
                    <span className="text-zinc-500">Score {(r.score * 100).toFixed(0)}%</span>
                    <span className="font-mono text-zinc-700">{r.name}</span>
                  </div>
                  <div className="grid gap-2 md:grid-cols-2">
                    <pre className="max-h-40 overflow-auto rounded-md border border-zinc-200 bg-slate-50 px-2 py-1.5 text-[11px] text-zinc-700 whitespace-pre-wrap break-words">
                      {renderCaseValue(r.input)}
                    </pre>
                    <pre className="max-h-40 overflow-auto rounded-md border border-zinc-200 bg-slate-50 px-2 py-1.5 text-[11px] text-zinc-700 whitespace-pre-wrap break-words">
                      {r.error ? r.error : renderCaseValue(r.output)}
                    </pre>
                  </div>
                  {r.reasoning && <p className="mt-2 text-zinc-500">{r.reasoning}</p>}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Baseline comparison: net delta + regressions + per-case output diff */}
      {showComparison && comparison && (
        <div data-testid="tool-workspace-run-comparison" className="rounded-lg border border-blue-200 bg-blue-50/40 p-4">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-sm font-semibold text-blue-900">Vs. baseline</h3>
            <span
              data-testid="tool-run-score-delta"
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

          <div data-testid="tool-run-regressions" className="mb-3">
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
                key={`${row.cur.name}-${i}`}
                className={`rounded-md border bg-white p-3 text-xs ${
                  row.regressed ? "border-red-300" : "border-zinc-200"
                }`}
              >
                <div className="mb-2 flex items-center gap-2">
                  <span className="font-mono text-zinc-500">{row.cur.name}</span>
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
                      {row.base
                        ? row.base.error || renderCaseValue(row.base.output)
                        : "(no baseline output)"}
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
                      {row.cur.error || renderCaseValue(row.cur.output)}
                    </pre>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Durable run history */}
      <div data-testid="tool-workspace-run-history" className="space-y-2">
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
            No saved runs yet. Invoke on Sandbox or run the suite on Hardening to capture one.
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
                  <span className="w-24 shrink-0">
                    <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold text-slate-600">
                      {run.kind}
                    </span>
                  </span>
                  <span className="w-14 shrink-0 text-sm font-semibold text-zinc-800">
                    {run.overall_score !== null
                      ? `${(run.overall_score * 100).toFixed(0)}%`
                      : "—"}
                  </span>
                  <span className="text-xs text-zinc-500">
                    <span className="font-medium text-emerald-600">{run.passed_count} pass</span>
                    {" · "}
                    <span className="font-medium text-amber-600">{run.partial_count} partial</span>
                    {" · "}
                    <span className="font-medium text-red-600">{run.failed_count} fail</span>
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

/* ── Hardening stage — deterministic fixtures suite (authoritative) + the
 * LLM generate/judge lane (secondary). Both persist as durable runs: the
 * deterministic calibrate as kind:"suite", the LLM-judged run as
 * kind:"hardening". This tab replaces the old "Calibration" label. */
function ToolHardeningStage({
  tool,
  onSaved,
  onAfterRun,
}: {
  tool: ToolDefinition;
  onSaved: () => void;
  onAfterRun: () => void;
}): JSX.Element {
  return (
    <div className="space-y-6">
      {/* Authoritative lane: deterministic assertions over saved fixtures. */}
      <section data-testid="tool-hardening-deterministic" className="rounded-lg border border-zinc-200 bg-white p-4">
        <div className="mb-2">
          <h3 className="text-sm font-semibold text-zinc-900">Deterministic suite</h3>
          <p className="text-xs text-zinc-500">
            The authoritative lane: score the saved fixtures (deterministic assertions). Each run is captured as a durable suite run.
          </p>
        </div>
        <CalibrationPanel
          key={tool.tool_id}
          idPrefix="tool-hardening"
          calibrateTestId="tool-hardening-calibrate-btn"
          initialCases={tool.test_cases ?? []}
          lastResult={tool.last_calibration ?? null}
          onSave={async (cases: CalibrationCase[]) => {
            const saved = await caliberApi.saveToolTestCases(tool.tool_id, cases);
            onSaved();
            return saved;
          }}
          onCalibrate={async (): Promise<CalibrationResult> => {
            const scored = await caliberApi.calibrateTool(tool.tool_id);
            // Persist the deterministic suite result as a durable kind:"suite" run.
            try {
              await caliberApi.saveToolTestRun({
                tool_id: tool.tool_id,
                kind: "suite",
                tool_version: tool.version,
                results: scored.cases.map((c) => ({
                  name: c.name,
                  input: {},
                  output: c.output,
                  error: c.error,
                  verdict: c.passed ? "pass" : "fail",
                  score: c.passed ? 1 : 0,
                  duration_ms: c.duration_ms,
                  reasoning: c.error
                    ? "Assertion failed."
                    : "Assertion satisfied.",
                })),
              });
            } catch {
              /* persistence is best-effort; the pass-rate still renders */
            }
            onAfterRun();
            return scored;
          }}
        />
      </section>

      {/* Secondary lane: LLM generate + judge unit tests. */}
      <section data-testid="tool-hardening-llm" className="rounded-lg border border-zinc-200 bg-white p-4">
        <div className="mb-3">
          <h3 className="text-sm font-semibold text-zinc-900">LLM-judged unit tests</h3>
          <p className="text-xs text-zinc-500">
            A secondary lane: generate input/output cases and judge them with an LLM. A completed run is captured as a durable hardening run.
          </p>
        </div>
        <ToolTests
          tools={[tool]}
          loading={false}
          onRun={async (toolDef, cases, results) => {
            try {
              const byId = new Map(cases.map((c) => [c.id, c]));
              await caliberApi.saveToolTestRun({
                tool_id: toolDef.tool_id,
                kind: "hardening",
                tool_version: toolDef.version,
                results: results.map((r, i) => {
                  const tc = byId.get(r.testCaseId);
                  return {
                    name: tc?.expectedBehavior?.trim() || `case ${i + 1}`,
                    input: tc?.input ?? {},
                    output: r.actualOutput,
                    error: r.error,
                    verdict: r.verdict,
                    score: r.score,
                    duration_ms: r.durationMs,
                    reasoning: r.reasoning,
                  };
                }),
              });
              onAfterRun();
            } catch {
              /* persistence is best-effort; inline results still render */
            }
          }}
        />
      </section>
    </div>
  );
}

/* ── Publish stage — activation/availability surface (status + where-used) ── */
function ToolPublishStage({
  toolId,
  tool,
  onChanged,
}: {
  toolId: string;
  tool: ToolDefinition | null;
  onChanged: () => void;
}): JSX.Element {
  const invalidate = useInvalidate();
  const usageQuery = useApiQuery(
    ["tool-usage", toolId],
    (s) => caliberApi.getToolUsage(toolId, s),
    { enabled: Boolean(toolId) },
  );
  const meQuery = useApiQuery(["me"], (s) => caliberApi.getMe(s));
  const isAdmin = meQuery.data?.is_admin ?? false;
  const activeProjectId = getActiveProjectId();
  const projectQuery = useApiQuery(
    ["project-access", activeProjectId],
    (s) => caliberApi.getProject(activeProjectId!, s),
    { enabled: Boolean(activeProjectId) },
  );
  const accessRole = projectQuery.data?.access_role ?? "member";
  const projectPermissions = projectQuery.data?.permissions ?? [];

  const deprecateMut = useApiMutation(
    () => caliberApi.updateTool(toolId, { status: "deprecated" } as ToolUpdatePayload),
    {
      onSuccess: async () => {
        await invalidate(["tool", toolId]);
        await invalidate(["tools"]);
        onChanged();
      },
    },
  );
  const archiveMut = useApiMutation(() => caliberApi.archiveTool(toolId), {
    onSuccess: async () => {
      await invalidate(["tool", toolId]);
      await invalidate(["tools"]);
      onChanged();
    },
  });

  const status = tool?.status ?? "active";
  const statusTone =
    status === "active"
      ? "bg-emerald-50 text-emerald-700 ring-emerald-200/60"
      : status === "deprecated"
        ? "bg-amber-50 text-amber-700 ring-amber-200/60"
        : "bg-slate-100 text-slate-600 ring-slate-200/60";

  return (
    <div className="space-y-4">
      <section data-testid="tool-publish-status" className="rounded-lg border border-zinc-200 bg-white p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="text-sm font-semibold text-zinc-900">Availability</h3>
            <p className="text-xs text-zinc-500">
              Tools are active on register. Deprecate to discourage new use; archive to retire.
            </p>
          </div>
          <span
            className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ${statusTone}`}
          >
            {status}
          </span>
        </div>

        {isAdmin ? (
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button
              type="button"
              data-testid="tool-publish-deprecate"
              disabled={status !== "active" || deprecateMut.isPending}
              onClick={() => deprecateMut.mutate(undefined)}
              className="rounded border border-gray-300 px-3 py-1.5 text-xs disabled:opacity-50"
            >
              {deprecateMut.isPending ? "Deprecating…" : "Deprecate"}
            </button>
            <button
              type="button"
              data-testid="tool-publish-archive"
              disabled={status === "archived" || archiveMut.isPending}
              onClick={() => archiveMut.mutate(undefined)}
              className="rounded border border-gray-300 px-3 py-1.5 text-xs text-red-600 disabled:opacity-50"
            >
              {archiveMut.isPending ? "Archiving…" : "Archive"}
            </button>
            {archiveMut.error && (
              <span data-testid="tool-publish-archive-error" className="text-xs text-red-600">
                {archiveMut.error.message}
              </span>
            )}
          </div>
        ) : (
          <p className="mt-3 text-xs text-gray-400">
            Deprecate / archive actions require admin access.
          </p>
        )}
        <div
          data-testid="tool-publish-access"
          className="mt-3 flex flex-wrap items-center gap-2 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600"
        >
          <span className="font-semibold text-slate-700">Project access:</span>
          <span className="rounded-full bg-white px-2 py-0.5 font-medium ring-1 ring-slate-200">
            {activeProjectId ? accessRole : "organization"}
          </span>
          {activeProjectId && projectPermissions.includes("resource.publish") ? (
            <span className="text-emerald-700">You can publish project resources.</span>
          ) : activeProjectId ? (
            <span>Ask the project owner for publish permission.</span>
          ) : (
            <span>Select a project to see resource permissions.</span>
          )}
        </div>
      </section>

      <section className="rounded-lg border border-zinc-200 bg-white p-4">
        <h3 className="text-sm font-semibold text-zinc-900">Where used</h3>
        {usageQuery.data && usageQuery.data.usage.length === 0 && (
          <div data-testid="tool-publish-usage-empty" className="mt-1 text-xs text-gray-400">
            Not referenced by any workflow.
          </div>
        )}
        <ul data-testid="tool-publish-usage" className="mt-1 space-y-1 text-xs">
          {(usageQuery.data?.usage ?? []).map((u) => (
            <li key={u.version_id}>
              <Link to={`/workflows/${u.workflow_id}`} className="text-blue-600 hover:underline">
                {u.workflow_id}
              </Link>{" "}
              v{u.version_number} ({u.status})
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
