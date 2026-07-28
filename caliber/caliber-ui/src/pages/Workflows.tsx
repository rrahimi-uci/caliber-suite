/**
 * Workflows — professional list surface: template picker, summary tiles,
 * status filters, and rich workflow cards with rename/delete actions.
 *
 * All data comes from listWorkflows() (workflow_id, name, description, owner,
 * status, created_at, updated_at). Per-workflow deployments/triggers/runs live
 * on the detail endpoint, so they are intentionally not shown on this list.
 */

import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { caliberApi } from "@/api/caliberApi";
import { ClearFiltersButton } from "@/components/ClearFiltersButton";
import { FilterBar } from "@/components/FilterBar";
import { ListRow, ListRows } from "@/components/ListRow";
import { PageHeader } from "@/components/PageHeader";
import { FilterSelect } from "@/components/FilterSelect";
import { SearchInput } from "@/components/SearchInput";
import { ViewToggle } from "@/components/ViewToggle";
import { WorkflowImportDialog } from "@/components/workflows/WorkflowImportDialog";
import type {
  Workflow,
  WorkflowManifest,
  WorkflowTemplate,
  WorkflowTemplateKind,
} from "@/api/workflowTypes";
import {
  useApiMutation,
  useApiQuery,
  useInvalidate,
} from "@/hooks/useApiQuery";
import { useEventStream } from "@/hooks/useEventStream";
import { useViewMode } from "@/hooks/useViewMode";
import { showToast } from "@/lib/toast";
import { relativeTime } from "@/lib/time";
import { templateManifest } from "@/lib/workflowGraph";

const WORKFLOW_TEMPLATE_ID_MARKER = "__CALIBER_WORKFLOW_ID__";
const WORKFLOW_TEMPLATE_NAME_MARKER = "__CALIBER_WORKFLOW_NAME__";
const WORKFLOW_REFRESH_EVENTS = [
  "workflow.created",
  "workflow.deleted",
  "workflow.paused",
  "workflow.resumed",
  "workflow.updated",
];

const FALLBACK_TEMPLATES: WorkflowTemplate[] = [
  {
    kind: "single_agent",
    label: "Single Agent",
    description: "One agent with tools and output.",
    icon: "🤖",
    gradient: "from-violet-500/10 to-caliber-500/10",
    manifest_template: templateManifest(
      "single_agent",
      WORKFLOW_TEMPLATE_ID_MARKER,
      WORKFLOW_TEMPLATE_NAME_MARKER,
    ),
  },
  {
    kind: "multi_agent_handoff",
    label: "Multi-Agent Handoff",
    description: "Coordinator agent delegates specialist work via handoff.",
    icon: "🤝",
    gradient: "from-fuchsia-500/10 to-rose-500/10",
    manifest_template: templateManifest(
      "multi_agent_handoff",
      WORKFLOW_TEMPLATE_ID_MARKER,
      WORKFLOW_TEMPLATE_NAME_MARKER,
    ),
  },
  {
    kind: "guarded_pipeline",
    label: "Guarded Pipeline",
    description: "Agent → guardrail → output.",
    icon: "🛡️",
    gradient: "from-amber-500/10 to-orange-500/10",
    manifest_template: templateManifest(
      "guarded_pipeline",
      WORKFLOW_TEMPLATE_ID_MARKER,
      WORKFLOW_TEMPLATE_NAME_MARKER,
    ),
  },
  {
    kind: "parallel_fanout",
    label: "Parallel Fan-Out",
    description: "Fork work across two agents, then join the results.",
    icon: "⚡",
    gradient: "from-sky-500/10 to-indigo-500/10",
    manifest_template: templateManifest(
      "parallel_fanout",
      WORKFLOW_TEMPLATE_ID_MARKER,
      WORKFLOW_TEMPLATE_NAME_MARKER,
    ),
  },
  {
    kind: "hitl_review",
    label: "Human Review",
    description: "Agent → PII redact → human approval → output.",
    icon: "✋",
    gradient: "from-emerald-500/10 to-teal-500/10",
    manifest_template: templateManifest(
      "hitl_review",
      WORKFLOW_TEMPLATE_ID_MARKER,
      WORKFLOW_TEMPLATE_NAME_MARKER,
    ),
  },
  {
    kind: "for_each_loop",
    label: "Batch Loop",
    description: "Process a list of items through one reusable worker agent.",
    icon: "🔁",
    gradient: "from-cyan-500/10 to-teal-500/10",
    manifest_template: templateManifest(
      "for_each_loop",
      WORKFLOW_TEMPLATE_ID_MARKER,
      WORKFLOW_TEMPLATE_NAME_MARKER,
    ),
  },
  {
    kind: "refinement_loop",
    label: "Refinement Loop",
    description: "Iteratively improve one draft through the same worker agent.",
    icon: "🌀",
    gradient: "from-sky-500/10 to-emerald-500/10",
    manifest_template: templateManifest(
      "refinement_loop",
      WORKFLOW_TEMPLATE_ID_MARKER,
      WORKFLOW_TEMPLATE_NAME_MARKER,
    ),
  },
  {
    kind: "knowledge_rag",
    label: "Knowledge Q&A",
    description: "Start → knowledge query → output.",
    icon: "📚",
    gradient: "from-sky-500/10 to-cyan-500/10",
    manifest_template: templateManifest(
      "knowledge_rag",
      WORKFLOW_TEMPLATE_ID_MARKER,
      WORKFLOW_TEMPLATE_NAME_MARKER,
    ),
  },
  {
    kind: "graph_hybrid_rag",
    label: "GraphRAG Hybrid",
    description: "Start → graph-hybrid knowledge query → output.",
    icon: "🧠",
    gradient: "from-cyan-500/10 to-emerald-500/10",
    manifest_template: templateManifest(
      "graph_hybrid_rag",
      WORKFLOW_TEMPLATE_ID_MARKER,
      WORKFLOW_TEMPLATE_NAME_MARKER,
    ),
  },
  {
    kind: "knowledge_age",
    label: "AGE Graph Retrieval",
    description: "Start → AGE-backed knowledge query → output.",
    icon: "🕸️",
    gradient: "from-emerald-500/10 to-blue-500/10",
    manifest_template: templateManifest(
      "knowledge_age",
      WORKFLOW_TEMPLATE_ID_MARKER,
      WORKFLOW_TEMPLATE_NAME_MARKER,
    ),
  },
  {
    kind: "knowledge_age_build",
    label: "AGE Knowledge Build",
    description: "Launch a graph-synced knowledge-base build for Apache AGE.",
    icon: "🏗️",
    gradient: "from-emerald-500/10 to-teal-500/10",
    manifest_template: templateManifest(
      "knowledge_age_build",
      WORKFLOW_TEMPLATE_ID_MARKER,
      WORKFLOW_TEMPLATE_NAME_MARKER,
    ),
  },
  {
    kind: "event_resume",
    label: "Event Resume Gate",
    description: "Pause for an external event, then continue with an agent.",
    icon: "📨",
    gradient: "from-amber-500/10 to-sky-500/10",
    manifest_template: templateManifest(
      "event_resume",
      WORKFLOW_TEMPLATE_ID_MARKER,
      WORKFLOW_TEMPLATE_NAME_MARKER,
    ),
  },
  {
    kind: "blank",
    label: "Blank Canvas",
    description: "Start from scratch.",
    icon: "📄",
    gradient: "from-slate-500/10 to-gray-500/10",
    manifest_template: templateManifest(
      "blank",
      WORKFLOW_TEMPLATE_ID_MARKER,
      WORKFLOW_TEMPLATE_NAME_MARKER,
    ),
  },
];

const STATUS_CONFIG: Record<string, { dot: string; label: string }> = {
  active: {
    dot: "bg-emerald-500",
    label: "bg-emerald-50 text-emerald-700 ring-emerald-200/50",
  },
  paused: {
    dot: "bg-amber-500",
    label: "bg-amber-50 text-amber-700 ring-amber-200/50",
  },
  archived: {
    dot: "bg-slate-400",
    label: "bg-slate-50 text-slate-600 ring-slate-200/50",
  },
};

type StatusFilter = "all" | "active" | "paused" | "archived";

const FILTER_CHIPS: Array<{ key: StatusFilter; label: string }> = [
  { key: "all", label: "All" },
  { key: "active", label: "Active" },
  { key: "paused", label: "Paused" },
  { key: "archived", label: "Archived" },
];

function materializeTemplateValue(
  value: unknown,
  workflowId: string,
  name: string,
): unknown {
  if (value === WORKFLOW_TEMPLATE_ID_MARKER) return workflowId;
  if (value === WORKFLOW_TEMPLATE_NAME_MARKER) return name;
  if (Array.isArray(value)) {
    return value.map((item) =>
      materializeTemplateValue(item, workflowId, name),
    );
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        key,
        materializeTemplateValue(item, workflowId, name),
      ]),
    );
  }
  return value;
}

function instantiateTemplateManifest(
  template: WorkflowTemplate,
  workflowId: string,
  name: string,
): WorkflowManifest {
  return materializeTemplateValue(
    template.manifest_template,
    workflowId,
    name,
  ) as WorkflowManifest;
}

export function Workflows(): JSX.Element {
  const navigate = useNavigate();
  const invalidate = useInvalidate();
  const workflowEvent = useEventStream(WORKFLOW_REFRESH_EVENTS);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  // Owner filter defaults to the empty "All" sentinel; a row must match the text
  // query AND the status tile AND the owner (additive).
  const [ownerFilter, setOwnerFilter] = useState("");
  const [viewMode, setViewMode] = useViewMode("workflows");

  /* ── Edit / Delete state ── */
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);
  const [importDialog, setImportDialog] = useState<{
    mode: "import" | "clone";
    source?: Workflow;
  } | null>(null);

  const query = useApiQuery(["workflows"], (signal) =>
    caliberApi.listWorkflows(undefined, signal),
  );
  const templatesQuery = useApiQuery(["workflow-templates"], (signal) =>
    caliberApi.listWorkflowTemplates(signal),
  );

  useEffect(() => {
    if (!workflowEvent || typeof workflowEvent.workflow_id !== "string") return;
    void invalidate(["workflows"]);
  }, [workflowEvent, invalidate]);

  /* Dismiss the delete confirmation with Escape (mid-delete keeps it open). */
  useEffect(() => {
    if (!deleteConfirmId) return undefined;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setDeleteConfirmId(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [deleteConfirmId]);

  /* Create from template */
  const createMut = useApiMutation(
    async (vars: { name: string; kind: WorkflowTemplateKind }) => {
      const workflow = await caliberApi.createWorkflow({
        name: vars.name,
        owner: "",
      });
      const selectedTemplate = workflowTemplates.find(
        (item) => item.kind === vars.kind,
      );
      const manifest = selectedTemplate
        ? instantiateTemplateManifest(
            selectedTemplate,
            workflow.workflow_id,
            vars.name,
          )
        : templateManifest(vars.kind, workflow.workflow_id, vars.name);
      const version = await caliberApi.createWorkflowVersion(
        workflow.workflow_id,
        manifest,
      );
      return { workflow, version };
    },
    {
      onSuccess: async ({ workflow, version }) => {
        await invalidate(["workflows"]);
        showToast.success(`Created "${workflow.name}"`);
        navigate(
          `/workflows/${workflow.workflow_id}/editor/${version.version_id}`,
        );
      },
      onError: (err: Error) => {
        showToast.error(`Failed to create workflow: ${err.message}`);
      },
    },
  );

  /* Rename workflow */
  const renameMut = useApiMutation(
    async (vars: { id: string; name: string }) => {
      return caliberApi.updateWorkflow(vars.id, { name: vars.name });
    },
    {
      onSuccess: async (wf) => {
        await invalidate(["workflows"]);
        showToast.success(`Renamed to "${wf.name}"`);
        setEditingId(null);
      },
      onError: (err: Error) => {
        showToast.error(`Rename failed: ${err.message}`);
      },
    },
  );

  /* Delete workflow */
  const deleteMut = useApiMutation(
    async (id: string) => {
      await caliberApi.deleteWorkflow(id);
      return id;
    },
    {
      onSuccess: async () => {
        await invalidate(["workflows"]);
        showToast.success("Workflow deleted");
        setDeleteConfirmId(null);
      },
      onError: (err: Error) => {
        showToast.error(`Delete failed: ${err.message}`);
        setDeleteConfirmId(null);
      },
    },
  );

  const allWorkflows: Workflow[] = query.data ?? [];
  const workflowTemplates =
    templatesQuery.data?.templates ?? FALLBACK_TEMPLATES;

  const counts = {
    all: allWorkflows.length,
    active: allWorkflows.filter((w) => w.status === "active").length,
    paused: allWorkflows.filter((w) => w.status === "paused").length,
    archived: allWorkflows.filter((w) => w.status === "archived").length,
  };
  // Distinct owners present, for the Owner filter dropdown.
  const ownerOptions = Array.from(
    new Set(allWorkflows.map((w) => w.owner)),
  )
    .filter(Boolean)
    .sort()
    .map((owner) => ({ value: owner, label: owner }));
  const searchQuery = search.trim().toLowerCase();
  const workflows: Workflow[] = allWorkflows.filter((w) => {
    if (statusFilter !== "all" && w.status !== statusFilter) return false;
    if (ownerFilter && w.owner !== ownerFilter) return false;
    if (!searchQuery) return true;
    // Broadened beyond name to also cover description + owner.
    return [w.name, w.description, w.owner]
      .filter(Boolean)
      .some((field) => String(field).toLowerCase().includes(searchQuery));
  });
  const hasWorkflowFilters = Boolean(
    search || ownerFilter || statusFilter !== "all",
  );

  const isCreating = createMut.isPending;

  const deleteTarget = deleteConfirmId
    ? allWorkflows.find((w) => w.workflow_id === deleteConfirmId)
    : null;

  /**
   * Commit an inline rename. Single entry point for both Enter (form submit)
   * and blur so they can't fire the mutation twice, and a no-op rename (empty
   * or unchanged) just exits edit mode without a network call.
   */
  const commitRename = (wf: Workflow): void => {
    if (renameMut.isPending) return;
    const next = editName.trim();
    if (next && next !== wf.name) {
      renameMut.mutate({ id: wf.workflow_id, name: next });
    } else {
      setEditingId(null);
    }
  };

  const STAT_TILES: Array<{
    key: StatusFilter;
    label: string;
    value: number;
    icon: JSX.Element;
    tone: string;
  }> = [
    {
      key: "all",
      label: "Workflows",
      value: counts.all,
      tone: "bg-violet-50 text-caliber-purple",
      icon: <path d="M9 6h6a3 3 0 013 3v6M3 3h6v6H3zM15 15h6v6h-6z" />,
    },
    {
      key: "active",
      label: "Active",
      value: counts.active,
      tone: "bg-emerald-50 text-emerald-600",
      icon: <path d="M22 11.08V12a10 10 0 11-5.93-9.14M22 4L12 14.01l-3-3" />,
    },
    {
      key: "paused",
      label: "Paused",
      value: counts.paused,
      tone: "bg-amber-50 text-amber-600",
      icon: <path d="M10 4H6v16h4zM18 4h-4v16h4z" />,
    },
    {
      key: "archived",
      label: "Archived",
      value: counts.archived,
      tone: "bg-slate-100 text-slate-500",
      icon: <path d="M21 8v13H3V8M1 3h22v5H1zM10 12h4" />,
    },
  ];

  return (
    <div className="space-y-6 animate-fade-in">
      {importDialog && (
        <WorkflowImportDialog
          mode={importDialog.mode}
          sourceWorkflow={importDialog.source}
          onClose={() => setImportDialog(null)}
          onImported={({ workflow, version }) => {
            void invalidate(["workflows"]);
            setImportDialog(null);
            showToast.success(
              importDialog.mode === "clone"
                ? `Cloned as "${workflow.name}"`
                : `Imported "${workflow.name}"`,
            );
            navigate(`/workflows/${workflow.workflow_id}/editor/${version.version_id}`);
          }}
        />
      )}
      {/* ── Delete confirmation modal ── */}
      {deleteTarget && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm animate-fade-in"
          onClick={() => {
            if (!deleteMut.isPending) setDeleteConfirmId(null);
          }}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="delete-workflow-title"
            className="w-full max-w-sm rounded-2xl border border-slate-200/60 bg-white p-6 shadow-xl animate-scale-in"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center gap-3 mb-4">
              <div className="flex items-center justify-center w-10 h-10 rounded-xl bg-red-50">
                <svg
                  className="w-5 h-5 text-red-500"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                >
                  <path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2" />
                  <line x1="10" y1="11" x2="10" y2="17" />
                  <line x1="14" y1="11" x2="14" y2="17" />
                </svg>
              </div>
              <div>
                <h3
                  id="delete-workflow-title"
                  className="text-sm font-bold text-slate-900"
                >
                  Delete Workflow
                </h3>
                <p className="text-xs text-slate-400">
                  This action cannot be undone
                </p>
              </div>
            </div>
            <p className="text-sm text-slate-600 mb-6">
              Are you sure you want to delete{" "}
              <span className="font-semibold">
                &ldquo;{deleteTarget.name}&rdquo;
              </span>
              ? All versions, deployments, and runs will be permanently removed.
            </p>
            <div className="flex gap-3 justify-end">
              <button
                type="button"
                className="rounded-xl px-4 py-2 text-xs font-semibold text-slate-600 bg-slate-100 hover:bg-slate-200 transition-colors"
                onClick={() => setDeleteConfirmId(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                data-testid="confirm-delete"
                disabled={deleteMut.isPending}
                className="rounded-xl px-4 py-2 text-xs font-semibold text-white bg-red-500 hover:bg-red-600 transition-colors disabled:opacity-50"
                onClick={() => deleteMut.mutate(deleteConfirmId!)}
              >
                {deleteMut.isPending ? "Deleting…" : "Delete"}
              </button>
            </div>
          </div>
        </div>
      )}

      <PageHeader
        title="Workflows"
        subtitle="Build, test, and publish agentic workflows"
        actions={
          <div className="flex items-center gap-2">
            <button
              type="button"
              data-testid="import-workflow"
              className="btn-secondary flex items-center gap-2"
              onClick={() => setImportDialog({ mode: "import" })}
            >
              <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M12 3v12m0 0l-4-4m4 4l4-4" />
                <path d="M4 17v3h16v-3" />
              </svg>
              Import
            </button>
            <button
              type="button"
              data-testid="new-workflow"
              className="btn-primary flex items-center gap-2"
              onClick={() => setCreating((v) => !v)}
            >
              <svg
                className="w-4 h-4"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
              >
                <line x1="12" y1="5" x2="12" y2="19" />
                <line x1="5" y1="12" x2="19" y2="12" />
              </svg>
              New Workflow
            </button>
          </div>
        }
      />

      {/* ── Summary tiles (double as quick status filters) ── */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {STAT_TILES.map((tile) => {
          const active = statusFilter === tile.key;
          return (
            <button
              key={tile.key}
              type="button"
              data-testid={`workflow-tile-${tile.key}`}
              aria-pressed={active ? "true" : "false"}
              onClick={() => setStatusFilter(tile.key)}
              className={`stat-card flex items-center gap-3 text-left ${active ? "ring-2 ring-caliber-purple/30 border-caliber-purple/30" : ""}`}
            >
              <span
                className={`grid h-10 w-10 shrink-0 place-items-center rounded-xl ${tile.tone}`}
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
              <span className="min-w-0">
                <span className="block text-2xl font-bold leading-none tracking-tight text-slate-900">
                  {tile.value}
                </span>
                <span className="mt-1 block text-xs text-slate-500">
                  {tile.label}
                </span>
              </span>
            </button>
          );
        })}
      </div>

      {/* ── Template picker (toggled) ── */}
      {creating && (
        <div
          data-testid="template-gallery"
          className="rounded-2xl border border-slate-200/60 bg-white p-6 shadow-card animate-scale-in"
        >
          <h2 className="mb-1 text-sm font-bold text-slate-900 flex items-center gap-2">
            <svg
              className="w-4 h-4 text-caliber-purple"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
            >
              <rect x="3" y="3" width="18" height="18" rx="2" />
              <path d="M9 3v18M3 9h18" />
            </svg>
            Start from a template
          </h2>
          <p className="mb-4 text-xs text-slate-400">
            Name your workflow, then start from an agent, a governed pipeline,
            or a graph-native knowledge flow in the Workflow Studio.
          </p>
          <input
            placeholder="Give your workflow a name…"
            data-testid="new-workflow-name"
            className="mb-5 w-full max-w-md form-input"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {workflowTemplates.map((tpl) => (
              <button
                key={tpl.kind}
                type="button"
                data-testid={`template-${tpl.kind}`}
                disabled={!name.trim() || isCreating}
                onClick={() =>
                  createMut.mutate({ name: name.trim(), kind: tpl.kind })
                }
                className={`group relative overflow-hidden rounded-2xl border border-slate-200/60 p-5 text-left transition-all duration-300 hover:shadow-card-hover hover:-translate-y-0.5 disabled:opacity-40 active:scale-[0.98]`}
              >
                <div
                  className={`absolute inset-0 bg-gradient-to-br ${tpl.gradient} opacity-0 group-hover:opacity-100 transition-opacity duration-300`}
                />
                <div className="relative">
                  <div className="mb-3 text-2xl">{tpl.icon}</div>
                  <div className="text-sm font-bold text-slate-900">
                    {tpl.label}
                  </div>
                  <div className="mt-1 text-xs text-slate-400 leading-relaxed">
                    {tpl.description}
                  </div>
                </div>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* ── Toolbar: filter chips + search ── */}
      <FilterBar
        search={
          <SearchInput
            value={search}
            onChange={setSearch}
            ariaLabel="Search workflows"
            placeholder="Search workflows…"
            className="w-full"
          />
        }
        filters={
          <>
            <FilterSelect
              label="Owner"
              allLabel="All owners"
              value={ownerFilter}
              onChange={setOwnerFilter}
              options={ownerOptions}
              className="w-full sm:w-44"
            />
            {FILTER_CHIPS.map((chip) => {
              const active = statusFilter === chip.key;
              return (
                <button
                  key={chip.key}
                  type="button"
                  data-testid={`workflow-filter-${chip.key}`}
                  aria-pressed={active ? "true" : "false"}
                  onClick={() => setStatusFilter(chip.key)}
                  className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs font-semibold transition-colors ${
                    active
                      ? "border-caliber-200 bg-caliber-50 text-caliber-700"
                      : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50"
                  }`}
                >
                  {chip.label}
                  <span
                    className={`rounded-full px-1.5 text-[10px] ${active ? "bg-white text-slate-500" : "bg-slate-100 text-slate-500"}`}
                  >
                    {counts[chip.key]}
                  </span>
                </button>
              );
            })}
          </>
        }
        actions={
          <>
            <ClearFiltersButton
              visible={hasWorkflowFilters}
              onClear={() => {
                setSearch("");
                setOwnerFilter("");
                setStatusFilter("all");
              }}
            />
            <ViewToggle value={viewMode} onChange={setViewMode} />
          </>
        }
      />

      {/* ── Workflow grid ── */}
      {query.isLoading && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {[1, 2, 3].map((i) => (
            <div
              key={i}
              className="rounded-2xl border border-slate-200/60 bg-white p-5 shadow-card shimmer h-36"
            />
          ))}
        </div>
      )}
      {query.error && (
        <div className="rounded-2xl border border-red-200/60 bg-red-50 px-5 py-4 text-sm text-red-600 shadow-card">
          <span className="font-semibold">Error:</span> {query.error.message}
        </div>
      )}
      {query.data && workflows.length === 0 && (
        <div
          data-testid="workflows-empty"
          className="rounded-2xl border-2 border-dashed border-slate-200 bg-gradient-hero px-8 py-12 text-center"
        >
          <div className="flex items-center justify-center w-14 h-14 mx-auto rounded-2xl bg-white shadow-card mb-4">
            <svg
              className="w-7 h-7 text-slate-300"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
            >
              <rect x="3" y="3" width="6" height="6" rx="1.5" />
              <rect x="15" y="15" width="6" height="6" rx="1.5" />
              <path d="M9 6h6a3 3 0 013 3v6" />
            </svg>
          </div>
          <div className="text-sm font-semibold text-slate-600">
            {allWorkflows.length === 0
              ? "No workflows yet"
              : "No workflows match your filters"}
          </div>
          <div className="mt-1.5 text-xs text-slate-400 max-w-sm mx-auto">
            {allWorkflows.length === 0
              ? "Create one from a template above to get started."
              : "Try a different status filter or search term."}
          </div>
        </div>
      )}

      {workflows.length > 0 && viewMode === "list" && (
        <ListRows data-testid="workflows-list">
          {workflows.map((wf) => {
            const statusCfg =
              STATUS_CONFIG[wf.status] ?? STATUS_CONFIG.archived!;
            const isEditing = editingId === wf.workflow_id;
            return (
              <ListRow
                key={wf.workflow_id}
                testId={`workflow-row-${wf.workflow_id}`}
                title_attr={`Open ${wf.name}`}
                onClick={
                  isEditing ? undefined : () => navigate(`/workflows/${wf.workflow_id}`)
                }
                icon={
                  <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-brand-subtle">
                    <svg
                      className="h-4 w-4 text-caliber-purple"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.75"
                    >
                      <rect x="3" y="3" width="6" height="6" rx="1.5" />
                      <rect x="15" y="15" width="6" height="6" rx="1.5" />
                      <path d="M9 6h6a3 3 0 013 3v6" />
                    </svg>
                  </span>
                }
                title={
                  isEditing ? (
                    <form
                      onClick={(e) => e.stopPropagation()}
                      onSubmit={(e) => {
                        e.preventDefault();
                        commitRename(wf);
                      }}
                    >
                      <input
                        autoFocus
                        placeholder="Workflow name"
                        className="form-input !py-0.5 !px-2 !text-sm !font-bold"
                        value={editName}
                        onChange={(e) => setEditName(e.target.value)}
                        onBlur={() => commitRename(wf)}
                        onKeyDown={(e) => {
                          if (e.key === "Escape") setEditingId(null);
                        }}
                      />
                    </form>
                  ) : (
                    wf.name
                  )
                }
                subtitle={wf.description?.trim() || "No description provided."}
                columns={
                  <>
                    <span
                      className={`inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[10px] font-semibold ring-1 ${statusCfg.label}`}
                    >
                      <span className={`h-1.5 w-1.5 rounded-full ${statusCfg.dot}`} />
                      {wf.status}
                    </span>
                    <span className="w-28 truncate" title={wf.owner || "No owner"}>
                      {wf.owner || "No owner"}
                    </span>
                    <span className="w-32 shrink-0" title={wf.updated_at}>
                      updated {relativeTime(wf.updated_at)}
                    </span>
                  </>
                }
                actions={
                  <>
                    <button
                      type="button"
                      data-testid={`clone-workflow-${wf.workflow_id}`}
                      title="Clone workflow as new"
                      className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition-all hover:bg-sky-50 hover:text-sky-600"
                      onClick={(e) => {
                        e.stopPropagation();
                        setImportDialog({ mode: "clone", source: wf });
                      }}
                    >
                      <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                        <rect x="8" y="8" width="12" height="12" rx="2" />
                        <path d="M16 8V6a2 2 0 00-2-2H6a2 2 0 00-2 2v8a2 2 0 002 2h2" />
                      </svg>
                    </button>
                    <button
                      type="button"
                      data-testid={`edit-workflow-${wf.workflow_id}`}
                      title="Rename workflow"
                      className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition-all hover:bg-caliber-purple/5 hover:text-caliber-purple"
                      onClick={(e) => {
                        e.stopPropagation();
                        setEditingId(wf.workflow_id);
                        setEditName(wf.name);
                      }}
                    >
                      <svg
                        className="h-3.5 w-3.5"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="2"
                      >
                        <path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7" />
                        <path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z" />
                      </svg>
                    </button>
                    <button
                      type="button"
                      data-testid={`delete-workflow-${wf.workflow_id}`}
                      title="Delete workflow"
                      className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition-all hover:bg-red-50 hover:text-red-500"
                      onClick={(e) => {
                        e.stopPropagation();
                        setDeleteConfirmId(wf.workflow_id);
                      }}
                    >
                      <svg
                        className="h-3.5 w-3.5"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="2"
                      >
                        <path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2" />
                      </svg>
                    </button>
                  </>
                }
              />
            );
          })}
        </ListRows>
      )}

      {workflows.length > 0 && viewMode === "grid" && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {workflows.map((wf) => {
            const statusCfg =
              STATUS_CONFIG[wf.status] ?? STATUS_CONFIG.archived!;
            const isEditing = editingId === wf.workflow_id;
            return (
              <div
                key={wf.workflow_id}
                data-testid={`workflow-card-${wf.workflow_id}`}
                className="group relative flex flex-col rounded-2xl border border-slate-200/60 bg-white p-5 shadow-card transition-all duration-300 hover:shadow-card-hover hover:-translate-y-0.5"
              >
                {/* Top: icon + name + actions */}
                <div className="flex items-start gap-3">
                  <button
                    type="button"
                    title={`View ${wf.name}`}
                    className="flex items-center justify-center w-9 h-9 rounded-xl bg-gradient-brand-subtle flex-shrink-0 hover:ring-2 hover:ring-caliber-purple/20 transition-all"
                    onClick={() => navigate(`/workflows/${wf.workflow_id}`)}
                  >
                    <svg
                      className="w-4 h-4 text-caliber-purple"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.75"
                    >
                      <rect x="3" y="3" width="6" height="6" rx="1.5" />
                      <rect x="15" y="15" width="6" height="6" rx="1.5" />
                      <path d="M9 6h6a3 3 0 013 3v6" />
                    </svg>
                  </button>

                  <div className="flex-1 min-w-0">
                    {isEditing ? (
                      <form
                        onSubmit={(e) => {
                          e.preventDefault();
                          commitRename(wf);
                        }}
                      >
                        <input
                          autoFocus
                          placeholder="Workflow name"
                          className="form-input !py-0.5 !px-2 !text-sm !font-bold"
                          value={editName}
                          onChange={(e) => setEditName(e.target.value)}
                          onBlur={() => commitRename(wf)}
                          onKeyDown={(e) => {
                            if (e.key === "Escape") setEditingId(null);
                          }}
                        />
                      </form>
                    ) : (
                      <button
                        type="button"
                        className="block w-full truncate text-left text-sm font-bold text-slate-900 hover:text-caliber-purple transition-colors"
                        onClick={() => navigate(`/workflows/${wf.workflow_id}`)}
                      >
                        {wf.name}
                      </button>
                    )}
                    <div className="mt-1.5 flex items-center gap-2">
                      <span
                        className={`inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[10px] font-semibold ring-1 ${statusCfg.label}`}
                      >
                        <span
                          className={`w-1.5 h-1.5 rounded-full ${statusCfg.dot}`}
                        />
                        {wf.status}
                      </span>
                      {renameMut.isPending && isEditing && (
                        <svg
                          className="w-3 h-3 animate-spin text-slate-400"
                          viewBox="0 0 24 24"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="2"
                        >
                          <path d="M21 12a9 9 0 11-6.219-8.56" />
                        </svg>
                      )}
                    </div>
                  </div>

                  {/* Action buttons */}
                  <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                    <button
                      type="button"
                      data-testid={`clone-workflow-${wf.workflow_id}`}
                      title="Clone workflow as new"
                      className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition-all hover:bg-sky-50 hover:text-sky-600"
                      onClick={(e) => {
                        e.stopPropagation();
                        setImportDialog({ mode: "clone", source: wf });
                      }}
                    >
                      <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                        <rect x="8" y="8" width="12" height="12" rx="2" />
                        <path d="M16 8V6a2 2 0 00-2-2H6a2 2 0 00-2 2v8a2 2 0 002 2h2" />
                      </svg>
                    </button>
                    <button
                      type="button"
                      data-testid={`edit-workflow-${wf.workflow_id}`}
                      title="Rename workflow"
                      className="flex items-center justify-center w-8 h-8 rounded-lg text-slate-400 hover:text-caliber-purple hover:bg-caliber-purple/5 transition-all"
                      onClick={(e) => {
                        e.stopPropagation();
                        setEditingId(wf.workflow_id);
                        setEditName(wf.name);
                      }}
                    >
                      <svg
                        className="w-3.5 h-3.5"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="2"
                      >
                        <path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7" />
                        <path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z" />
                      </svg>
                    </button>
                    <button
                      type="button"
                      data-testid={`delete-workflow-${wf.workflow_id}`}
                      title="Delete workflow"
                      className="flex items-center justify-center w-8 h-8 rounded-lg text-slate-400 hover:text-red-500 hover:bg-red-50 transition-all"
                      onClick={(e) => {
                        e.stopPropagation();
                        setDeleteConfirmId(wf.workflow_id);
                      }}
                    >
                      <svg
                        className="w-3.5 h-3.5"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="2"
                      >
                        <path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2" />
                      </svg>
                    </button>
                  </div>
                </div>

                {/* Description */}
                <p className="mt-3 line-clamp-2 min-h-[2.5rem] text-xs leading-relaxed text-slate-500">
                  {wf.description?.trim() || "No description provided."}
                </p>

                {/* Footer: owner + updated + open */}
                <div className="mt-auto flex items-center justify-between border-t border-slate-100 pt-3 text-xs text-slate-400">
                  <span className="flex min-w-0 items-center gap-1.5 truncate">
                    <svg
                      className="w-3.5 h-3.5 shrink-0"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                    >
                      <path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2" />
                      <circle cx="12" cy="7" r="4" />
                    </svg>
                    <span className="truncate">{wf.owner || "No owner"}</span>
                    <span className="text-slate-300">·</span>
                    <span title={wf.updated_at} className="shrink-0">
                      updated {relativeTime(wf.updated_at)}
                    </span>
                  </span>
                  <button
                    type="button"
                    title={`Open ${wf.name}`}
                    aria-label={`Open ${wf.name}`}
                    className="flex shrink-0 items-center gap-1 font-medium text-slate-400 hover:text-caliber-purple transition-colors"
                    onClick={() => navigate(`/workflows/${wf.workflow_id}`)}
                  >
                    Open
                    <svg
                      className="w-3.5 h-3.5"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                    >
                      <path d="M9 18l6-6-6-6" />
                    </svg>
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
