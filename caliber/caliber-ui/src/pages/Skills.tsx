/**
 * Skills — landing list + per-skill Workspace ("open a skill → a focused
 * Workspace"), following the prompt/tool Workspace pattern.
 *
 * The landing surface is the skill registry (search + filters). Opening a skill
 * enters its Workspace: a status header (name · category · version · lifecycle
 * pill) over six stage tabs — Author · Render Preview · Trigger Tests · Scenario
 * Sets · Runs · Bind — each scoped to the one open skill. Calibration is
 * agent-free (POST /skills/{id}/calibrate auto-provisions the hidden target);
 * the mandatory agent picker is gone.
 *
 * Follows the Anthropic skill standard: kebab-case names, progressive
 * disclosure (summary + content), use-case categories, metadata,
 * composability via depends_on, and security restrictions (no XML in
 * system-prompt-facing fields, reserved name prefixes).
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError, caliberApi } from "@/api/caliberApi";
import { ClearFiltersButton } from "@/components/ClearFiltersButton";
import { CopyButton } from "@/components/CopyButton";
import { FilterBar } from "@/components/FilterBar";
import { ListRow, ListRows } from "@/components/ListRow";
import { PageHeader } from "@/components/PageHeader";
import { PageTabs, type PageTab } from "@/components/PageTabs";
import { SearchInput } from "@/components/SearchInput";
import { FilterSelect } from "@/components/FilterSelect";
import { ViewToggle } from "@/components/ViewToggle";
import type {
  AgentConfig,
  ResourceStatus,
  Skill,
  SkillBindPayload,
  SkillCategory,
  SkillSelectionResult,
  SkillTestRunDetail,
  SkillTestRunResultCase,
  SkillTestRunSummary,
  SkillUpdatePayload,
  SkillWorkspaceResponse,
} from "@/api/types";
import type { SkillRenderResult, Workflow } from "@/api/workflowTypes";
import { useApi } from "@/hooks/useApi";
import { useViewMode } from "@/hooks/useViewMode";
import { relativeTime } from "@/lib/time";
import { SkillWizard } from "@/pages/SkillWizard";

/* ── Skill lifecycle pill ─────────────────────────────────────────────────── */

/** Tailwind tone per skill lifecycle status (matches the backend status enum). */
const SKILL_STATUS_TONES: Record<string, string> = {
  Draft: "bg-slate-100 text-slate-600 ring-slate-200/60",
  "Has scenarios": "bg-blue-50 text-blue-700 ring-blue-200/60",
  Tested: "bg-violet-50 text-caliber-purple ring-violet-200/60",
  Calibrated: "bg-amber-50 text-amber-700 ring-amber-200/60",
  Bound: "bg-emerald-50 text-emerald-700 ring-emerald-200/60",
};

/** Lifecycle status pill for the Workspace header (mirrors PromptStatusBadge). */
function SkillStatusBadge({ status }: { status: string }): JSX.Element {
  const tone =
    SKILL_STATUS_TONES[status] ?? "bg-slate-100 text-slate-600 ring-slate-200/60";
  return (
    <span
      data-testid="skill-workspace-status-badge"
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ${tone}`}
    >
      {status}
    </span>
  );
}

/* ── Workspace stage tabs ─────────────────────────────────────────────────── */

type SkillStage =
  | "author"
  | "render"
  | "trigger"
  | "scenarios"
  | "runs"
  | "bind";

const SKILL_WORKSPACE_STAGES: PageTab[] = [
  {
    key: "author",
    label: "Author",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
        <path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7" />
        <path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z" />
      </svg>
    ),
  },
  {
    key: "render",
    label: "Render Preview",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
        <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
        <circle cx="12" cy="12" r="3" />
      </svg>
    ),
  },
  {
    key: "trigger",
    label: "Trigger Tests",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
        <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" />
      </svg>
    ),
  },
  {
    key: "scenarios",
    label: "Scenario Sets",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
        <path d="M4 7V4h16v3M9 20h6M12 4v16" />
      </svg>
    ),
  },
  {
    key: "runs",
    label: "Runs",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
        <path d="M3 3v18h18M9 17V9M14 17v-4M19 17V5" />
      </svg>
    ),
  },
  {
    key: "bind",
    label: "Bind",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
        <path d="M10 13a5 5 0 0 0 7.07 0l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71M14 11a5 5 0 0 0-7.07 0l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
      </svg>
    ),
  },
];

export function Skills(): JSX.Element {
  const fetcher = useCallback(
    (signal: AbortSignal) => caliberApi.listSkills({ status: "all" }, signal),
    [],
  );
  const { data, error, loading, refresh } = useApi(fetcher, []);
  // Archiving / restoring a skill is admin-only on the backend (SCOPE_ADMIN);
  // gate the control so only admins see it.
  const meFetcher = useCallback((signal: AbortSignal) => caliberApi.getMe(signal), []);
  const { data: me } = useApi(meFetcher, []);
  const isAdmin = me?.is_admin ?? false;
  // The skill currently opened into the Workspace (null = landing list).
  const [openSkillId, setOpenSkillId] = useState<string | null>(null);
  // Create-mode Workspace whose Author stage hosts the build wizard.
  const [building, setBuilding] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pendingArchive, setPendingArchive] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  // Status + Category default to the empty "All" sentinel; a row must match the
  // text query AND every active filter (additive).
  const [statusFilter, setStatusFilter] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("");
  const [viewMode, setViewMode] = useViewMode("skills");

  const allSkills = data ?? [];
  const activeCount = allSkills.filter((s) => s.status === "active").length;
  const archivedCount = allSkills.filter((s) => s.status === "archived").length;
  const categoriesInUse = new Set(allSkills.map((s) => s.category)).size;
  // Only offer categories that are actually present, humanized snake_case →
  // Title Case so we don't list all 16 when the registry uses a handful.
  const categoryOptions = Array.from(
    new Set(allSkills.map((s) => s.category)),
  )
    .sort()
    .map((category) => ({ value: category, label: humanizeCategory(category) }));

  const q = search.trim().toLowerCase();
  const filteredSkills = allSkills.filter((skill) => {
    if (statusFilter && skill.status !== statusFilter) return false;
    if (categoryFilter && skill.category !== categoryFilter) return false;
    if (!q) return true;
    return [
      skill.name,
      skill.summary,
      skill.description,
      skill.category,
      skill.owner,
      ...(skill.tags ?? []),
    ]
      .filter(Boolean)
      .some((field) => String(field).toLowerCase().includes(q));
  });
  const hasSkillFilters = Boolean(search || statusFilter || categoryFilter);

  const STAT_TILES: Array<{ key: string; label: string; value: number; tone: string; icon: JSX.Element }> = [
    {
      key: "registry", label: "Skills in registry", value: allSkills.length, tone: "bg-violet-50 text-caliber-purple",
      icon: <path d="M19.439 7.85c-.049.322.059.648.289.878l1.568 1.568c.47.47.706 1.087.706 1.704s-.235 1.233-.706 1.704l-1.611 1.611a.98.98 0 01-.837.276c-.47-.07-.802-.48-.968-.925a2.501 2.501 0 10-3.214 3.214c.446.166.855.497.925.968a.979.979 0 01-.276.837l-1.61 1.61a2.404 2.404 0 01-1.705.707 2.402 2.402 0 01-1.704-.706l-1.568-1.568a1.026 1.026 0 00-.877-.29c-.493.074-.84.504-1.02.968a2.5 2.5 0 11-3.237-3.237c.464-.18.894-.527.967-1.02a1.026 1.026 0 00-.289-.877l-1.568-1.568A2.402 2.402 0 011 12c0-.617.236-1.234.706-1.704L2.96 9.042c.198-.198.49-.276.764-.213.397.092.683.45.853.823a2.5 2.5 0 103.314-3.313c-.373-.17-.731-.456-.823-.853-.063-.273.015-.566.213-.764l1.255-1.254A2.402 2.402 0 0110.24 3c.617 0 1.234.236 1.704.706l1.568 1.568c.23.23.556.338.877.29.493-.074.84-.504 1.02-.968a2.5 2.5 0 113.237 3.237c-.464.18-.894.527-.967 1.02z" />,
    },
    {
      key: "active", label: "Active", value: activeCount, tone: "bg-emerald-50 text-emerald-600",
      icon: <path d="M22 11.08V12a10 10 0 11-5.93-9.14M22 4L12 14.01l-3-3" />,
    },
    {
      key: "archived", label: "Archived", value: archivedCount, tone: "bg-slate-100 text-slate-500",
      icon: <path d="M21 8v13H3V8M1 3h22v5H1zM10 12h4" />,
    },
    {
      key: "categories", label: "Categories in use", value: categoriesInUse, tone: "bg-blue-50 text-blue-600",
      icon: <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" />,
    },
  ];

  const setStatus = async (skill: Skill, status: ResourceStatus): Promise<void> => {
    setPendingArchive(skill.skill_id);
    setActionError(null);
    try {
      await caliberApi.updateSkill(skill.skill_id, { status });
      refresh();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "update failed");
    } finally {
      setPendingArchive(null);
    }
  };

  // ── Brand-new skill: a create-mode Workspace whose Author stage hosts the
  // build wizard. On close we re-fetch and return to the landing list.
  if (building) {
    return (
      <SkillWorkspace
        skill={null}
        creating
        onBack={() => setBuilding(false)}
        onCreated={() => {
          setBuilding(false);
          refresh();
        }}
        onChanged={refresh}
      />
    );
  }

  // ── Open an existing skill's Workspace. Resolved from the loaded list so the
  // header has facts to seed; the workspace endpoint fetches live lifecycle.
  if (openSkillId) {
    const openSkill = allSkills.find((s) => s.skill_id === openSkillId) ?? null;
    return (
      <SkillWorkspace
        key={openSkillId}
        skillId={openSkillId}
        skill={openSkill}
        onBack={() => setOpenSkillId(null)}
        onChanged={refresh}
      />
    );
  }

  return (
    <div className="space-y-6 animate-fade-in">
      <PageHeader
        title="Skills"
        subtitle="Reusable capability packages that encapsulate instructions, workflows, domain knowledge, tools, and supporting assets. Agents can compose Skills into their runtime behavior through progressive disclosure, loading only what is needed for the task. Update a Skill once and every agent that references it can benefit from the change — versioned, governed, reusable, and packageable as OpenAI-compatible folders."
        actions={
          <>
            {data && (
              <span className="hidden items-center gap-1.5 text-[11px] text-slate-400 sm:flex" title="Across this project">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
                {activeCount} active · {archivedCount} archived
              </span>
            )}
            <button
              type="button"
              data-testid="new-skill"
              className="btn-primary flex items-center gap-2"
              onClick={() => setBuilding(true)}
            >
              <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <line x1="12" y1="5" x2="12" y2="19" />
                <line x1="5" y1="12" x2="19" y2="12" />
              </svg>
              Build Skill
            </button>
          </>
        }
      />

      {error && (
        <div className="flex items-start gap-3 rounded-2xl border border-red-200/60 bg-red-50 px-5 py-4 shadow-card">
          <svg className="w-4 h-4 mt-0.5 text-red-500 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="12" cy="12" r="10" />
            <line x1="12" y1="8" x2="12" y2="12" />
            <line x1="12" y1="16" x2="12.01" y2="16" />
          </svg>
          <div className="flex-1 text-sm text-red-700">
            <div className="font-semibold">Failed to load skills</div>
            <div className="text-xs mt-0.5 text-red-500">{error.message}</div>
          </div>
        </div>
      )}

      {actionError && (
        <div className="flex items-start gap-3 rounded-2xl border border-red-200/60 bg-red-50 px-5 py-4 shadow-card">
          <div className="text-sm text-red-700">{actionError}</div>
        </div>
      )}

      {/* ── Summary tiles ── */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {STAT_TILES.map((tile) => (
          <div key={tile.key} data-testid={`skill-tile-${tile.key}`} className="stat-card">
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

      {/* ── Filters + search ── */}
      <FilterBar
        search={
          <SearchInput
            value={search}
            onChange={setSearch}
            ariaLabel="Search skills"
            placeholder="Search by name, tag, category, owner…"
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
                { value: "archived", label: "Archived" },
              ]}
              className="w-full sm:w-44"
            />
            <FilterSelect
              label="Category"
              allLabel="All categories"
              value={categoryFilter}
              onChange={setCategoryFilter}
              options={categoryOptions}
              className="w-full sm:w-44"
            />
          </>
        }
        actions={
          <>
            <ClearFiltersButton
              visible={hasSkillFilters}
              onClear={() => {
                setSearch("");
                setStatusFilter("");
                setCategoryFilter("");
              }}
            />
            <ViewToggle value={viewMode} onChange={setViewMode} />
          </>
        }
      />

      {/* ── Skill cards grid ── */}
      {loading && !data && (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="rounded-2xl border border-slate-200/60 bg-white p-5 shadow-card shimmer h-44" />
          ))}
        </div>
      )}

      {data && filteredSkills.length === 0 && (
        <div data-testid="skills-empty" className="rounded-2xl border-2 border-dashed border-slate-200 bg-gradient-hero px-8 py-12 text-center">
          <div className="flex items-center justify-center w-14 h-14 mx-auto rounded-2xl bg-white shadow-card mb-4">
            <svg className="w-7 h-7 text-slate-300" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
              <path d="M4 19.5A2.5 2.5 0 016.5 17H20" />
              <path d="M4 4.5A2.5 2.5 0 016.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15z" />
            </svg>
          </div>
          <div className="text-sm font-semibold text-slate-600">
            {q
              ? `No skills match “${search.trim()}”.`
              : statusFilter || categoryFilter
                ? "No skills match the current filters."
                : "No skills in this view."}
          </div>
          <div className="mt-1.5 text-xs text-slate-400 max-w-sm mx-auto">
            Try a different search term — or build a new skill.
          </div>
        </div>
      )}

      {data && filteredSkills.length > 0 && viewMode === "list" && (
        <ListRows testId="skills-list">
          {filteredSkills.map((skill) => {
            const colorCls = CATEGORY_COLORS[skill.category] ?? CATEGORY_COLORS.custom;
            return (
              <ListRow
                key={skill.skill_id}
                testId={`skill-row-${skill.skill_id}`}
                title_attr="Open this skill's Workspace"
                onClick={() => setOpenSkillId(skill.skill_id)}
                icon={
                  <span className={`grid h-9 w-9 place-items-center rounded-xl ${colorCls}`}>
                    <svg
                      className="h-4 w-4"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.75"
                    >
                      <path d="M4 19.5A2.5 2.5 0 016.5 17H20" />
                      <path d="M4 4.5A2.5 2.5 0 016.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15z" />
                    </svg>
                  </span>
                }
                title={<span className="font-mono">{skill.name}</span>}
                subtitle={skill.summary || skill.description || skill.content.split("\n")[0]}
                columns={
                  <>
                    <CategoryPill category={skill.category} />
                    <span className="font-mono text-[10px] text-slate-400">v{skill.version}</span>
                    <span className="w-24 truncate" title={skill.owner || "No owner"}>
                      {skill.owner || "No owner"}
                    </span>
                    <span className="w-28 shrink-0" title={skill.updated_at}>
                      {relativeTime(skill.updated_at)}
                    </span>
                    <StatusPill status={skill.status} />
                  </>
                }
                actions={
                  <>
                    <button
                      type="button"
                      data-testid={`skill-open-${skill.name}`}
                      onClick={(e) => {
                        e.stopPropagation();
                        setOpenSkillId(skill.skill_id);
                      }}
                      className="rounded-lg px-2.5 py-1 text-xs font-medium text-caliber-purple transition-colors hover:bg-caliber-purple/5 hover:underline"
                    >
                      Open
                    </button>
                    {isAdmin && (
                      <button
                        type="button"
                        disabled={pendingArchive === skill.skill_id}
                        onClick={(e) => {
                          e.stopPropagation();
                          void setStatus(skill, skill.status === "active" ? "archived" : "active");
                        }}
                        className="rounded-lg px-2.5 py-1 text-xs font-medium text-caliber-purple transition-colors hover:bg-caliber-purple/5 hover:underline disabled:opacity-50"
                      >
                        {skill.status === "active" ? "Archive" : "Restore"}
                      </button>
                    )}
                  </>
                }
              />
            );
          })}
        </ListRows>
      )}

      {data && filteredSkills.length > 0 && viewMode === "grid" && (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          {filteredSkills.map((skill) => {
            const colorCls = CATEGORY_COLORS[skill.category] ?? CATEGORY_COLORS.custom;
            return (
              <div
                key={skill.skill_id}
                data-testid={`skill-card-${skill.skill_id}`}
                title="Click to open this skill's Workspace"
                onDoubleClick={() => setOpenSkillId(skill.skill_id)}
                className="group card flex cursor-pointer flex-col p-5 transition-all duration-300 hover:-translate-y-0.5 hover:shadow-card-hover"
              >
                {/* Top: icon + name + meta + open/archive actions */}
                <div className="flex items-start justify-between gap-3">
                  <div className="flex min-w-0 items-center gap-3">
                    <span className={`grid h-10 w-10 shrink-0 place-items-center rounded-xl ${colorCls}`}>
                      <svg className="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
                        <path d="M4 19.5A2.5 2.5 0 016.5 17H20" />
                        <path d="M4 4.5A2.5 2.5 0 016.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15z" />
                      </svg>
                    </span>
                    <div className="min-w-0">
                      <button
                        type="button"
                        onClick={() => setOpenSkillId(skill.skill_id)}
                        className="block w-full truncate text-left font-mono text-sm font-semibold text-slate-900 group-hover:text-caliber-purple transition-colors"
                      >
                        {skill.name}
                      </button>
                      <div className="mt-0.5 flex items-center gap-1.5">
                        <CategoryPill category={skill.category} />
                        <span className="font-mono text-[10px] text-slate-400">v{skill.version}</span>
                        <span className="font-mono text-[10px] text-slate-300">·</span>
                        <span className="inline-flex items-center gap-1">
                          <span className="font-mono text-[10px] text-slate-400">{skill.skill_id}</span>
                          <CopyButton
                            value={skill.skill_id}
                            label="Copy skill ID"
                            className="opacity-0 group-hover:opacity-100"
                          />
                        </span>
                      </div>
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-1">
                    <button
                      type="button"
                      data-testid={`skill-open-${skill.name}`}
                      onClick={(e) => {
                        e.stopPropagation();
                        setOpenSkillId(skill.skill_id);
                      }}
                      className="rounded-lg px-2.5 py-1 text-xs font-medium text-caliber-purple hover:bg-caliber-purple/5 hover:underline transition-colors"
                    >
                      Open
                    </button>
                    {isAdmin && (
                      <button
                        type="button"
                        disabled={pendingArchive === skill.skill_id}
                        onClick={(e) => {
                          e.stopPropagation();
                          void setStatus(skill, skill.status === "active" ? "archived" : "active");
                        }}
                        className="rounded-lg px-2.5 py-1 text-xs font-medium text-caliber-purple hover:bg-caliber-purple/5 hover:underline disabled:opacity-50 transition-colors"
                      >
                        {skill.status === "active" ? "Archive" : "Restore"}
                      </button>
                    )}
                  </div>
                </div>

                {/* Summary */}
                <p className="mt-3 line-clamp-2 text-xs leading-relaxed text-slate-500">
                  {skill.summary || skill.description || skill.content.split("\n")[0]}
                </p>

                {/* Tags + dependency chips */}
                <div className="mt-3 flex flex-wrap gap-1.5">
                  {skill.tags.map((tag) => (
                    <span
                      key={tag}
                      className="text-[10px] font-semibold bg-slate-50 text-slate-500 px-2 py-0.5 rounded-md ring-1 ring-slate-200/50"
                    >
                      {tag}
                    </span>
                  ))}
                  {skill.depends_on.map((dep) => (
                    <span
                      key={dep}
                      title="depends_on — composed before this skill"
                      className="inline-flex items-center gap-1 rounded-md bg-amber-50 px-2 py-0.5 text-[10px] font-semibold text-amber-600 ring-1 ring-amber-200/50"
                    >
                      <svg className="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
                        <line x1="6" y1="3" x2="6" y2="15" />
                        <circle cx="18" cy="6" r="3" />
                        <circle cx="6" cy="18" r="3" />
                        <path d="M18 9a9 9 0 01-9 9" />
                      </svg>
                      depends on: {dep}
                    </span>
                  ))}
                </div>

                {/* Footer: owner + updated + status */}
                <div className="mt-auto flex items-center justify-between border-t border-slate-100 pt-3 text-[11px] text-slate-400">
                  <span className="flex min-w-0 items-center gap-1.5 truncate">
                    <svg className="w-3.5 h-3.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
                      <path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2" />
                      <circle cx="12" cy="7" r="4" />
                    </svg>
                    <span className="truncate">{skill.owner || "No owner"}</span>
                    <span className="text-slate-300">·</span>
                    <span title={skill.updated_at} className="shrink-0">{relativeTime(skill.updated_at)}</span>
                  </span>
                  <StatusPill status={skill.status} />
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/* ── Skill Workspace — open a skill → header + six stage tabs ──────────────
 *
 * Mirrors the prompt/tool Workspace: a status header (name · category · version
 * · lifecycle pill) over ``PageTabs`` whose stages reuse the existing skill
 * pieces, each scoped to the one open skill. ``getSkillWorkspace`` powers the
 * header and is refetched after a run / calibrate / set-baseline / bind so the
 * lifecycle pill keeps up. In create mode the Author stage hosts the build
 * wizard and the other stages stay inert until the skill exists. */
function SkillWorkspace({
  skillId,
  skill,
  creating = false,
  onBack,
  onCreated,
  onChanged,
}: {
  skillId?: string;
  skill: Skill | null;
  creating?: boolean;
  onBack: () => void;
  onCreated?: () => void;
  onChanged?: () => void;
}): JSX.Element {
  const [stage, setStage] = useState<SkillStage>("author");
  const [workspace, setWorkspace] = useState<SkillWorkspaceResponse | null>(null);
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  // The detail fetch backs stages that need the full skill record; the
  // list-supplied ``skill`` seeds the header immediately.
  const [detail, setDetail] = useState<Skill | null>(skill);
  // Scenario cases live at the Workspace level so Scenario Sets (the editor) and
  // Trigger Tests (which runs them) share one set, scoped to the open skill.
  const [scenarios, setScenarios] = useState<SkillScenarioCase[]>([]);

  const refreshDetail = useCallback(
    async (signal?: AbortSignal) => {
      if (!skillId) return;
      try {
        const data = await caliberApi.getSkill(skillId, signal);
        if (!signal?.aborted) setDetail(data);
      } catch {
        /* keep the list-supplied seed */
      }
    },
    [skillId],
  );

  const refreshWorkspace = useCallback(
    async (signal?: AbortSignal) => {
      if (!skillId) return;
      try {
        const data = await caliberApi.getSkillWorkspace(skillId, signal);
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
    [skillId],
  );

  useEffect(() => {
    if (!skillId) return;
    const controller = new AbortController();
    void refreshDetail(controller.signal);
    void refreshWorkspace(controller.signal);
    return () => controller.abort();
  }, [skillId, refreshDetail, refreshWorkspace]);

  const resolvedSkill = detail ?? skill;
  // Create-mode header has no live status yet; show a Draft placeholder.
  const headerName = creating
    ? "New skill"
    : resolvedSkill?.name ?? skillId ?? "Skill";
  const headerCategory = workspace?.category ?? resolvedSkill?.category ?? null;
  const headerVersion = workspace?.version ?? resolvedSkill?.version ?? null;
  const headerStatus = creating ? "Draft" : workspace?.lifecycle ?? "Draft";

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
          Back to skills
        </button>

        {/* ── Status header ── */}
        <div
          data-testid="skill-workspace-header"
          className="flex flex-wrap items-center justify-between gap-4 rounded-2xl border border-slate-200/70 bg-white p-5 shadow-card"
        >
          <div className="flex min-w-0 items-center gap-3">
            <span className="grid h-11 w-11 shrink-0 place-items-center rounded-xl bg-violet-50 text-caliber-purple">
              <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.85">
                <path d="M4 19.5A2.5 2.5 0 016.5 17H20" />
                <path d="M4 4.5A2.5 2.5 0 016.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15z" />
              </svg>
            </span>
            <div className="min-w-0">
              <h1 className="truncate font-mono text-xl font-bold tracking-tight text-slate-900">
                {headerName}
              </h1>
              <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-slate-500">
                <span>
                  Category:{" "}
                  <span className="text-slate-700">
                    {headerCategory ? humanizeCategory(headerCategory) : "—"}
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
          <SkillStatusBadge status={headerStatus} />
        </div>

        {workspaceError && (
          <div className="mt-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
            {workspaceError}
          </div>
        )}
      </div>

      <PageTabs
        tabs={SKILL_WORKSPACE_STAGES}
        active={stage}
        onChange={(k) => setStage(k as SkillStage)}
      />

      {/* ── Stage content — each reused piece is scoped to the open skill ── */}
      {stage === "author" &&
        (creating || !resolvedSkill ? (
          <div className="rounded-2xl border border-caliber-200/60 bg-caliber-50/30 p-5 shadow-card animate-scale-in">
            <SkillWizard onClose={() => onCreated?.()} />
          </div>
        ) : (
          <SkillAuthorStage
            skill={resolvedSkill}
            onSaved={() => {
              void refreshDetail();
              void refreshWorkspace();
              onChanged?.();
            }}
          />
        ))}

      {stage === "render" && resolvedSkill && (
        <SkillPlaygroundPanel skills={[resolvedSkill]} loading={false} />
      )}

      {stage === "trigger" && resolvedSkill && (
        <SkillTriggerTestsStage
          skill={resolvedSkill}
          scenarios={scenarios}
          onAfterRun={() => void refreshWorkspace()}
          onGoToScenarios={() => setStage("scenarios")}
        />
      )}

      {stage === "scenarios" && resolvedSkill && (
        <SkillScenarioSetsStage
          skill={resolvedSkill}
          scenarios={scenarios}
          onChange={setScenarios}
          onGoToTrigger={() => setStage("trigger")}
        />
      )}

      {stage === "runs" && resolvedSkill && (
        <SkillRunsStage
          skill={resolvedSkill}
          workspace={workspace}
          onAfterRun={() => void refreshWorkspace()}
        />
      )}

      {stage === "bind" && resolvedSkill && (
        <SkillBindStage
          skill={resolvedSkill}
          boundTo={workspace?.bound_to ?? null}
          status={workspace?.lifecycle ?? null}
          onBound={() => void refreshWorkspace()}
        />
      )}

      {/* In create mode the non-Author stages are inert until the skill exists. */}
      {stage !== "author" && !resolvedSkill && (
        <div className="rounded-2xl border border-dashed border-slate-200 bg-slate-50 px-8 py-12 text-center text-sm text-slate-500">
          Save the skill on the Author stage to unlock this stage.
        </div>
      )}
    </div>
  );
}

/* ── Author stage — edit an existing skill in place ──────────────────────── */

/**
 * Focused edit-in-place surface for an existing skill (the wizard handles
 * create). Edits summary / description / category / tags / content and saves a
 * new version via ``updateSkill``. No agent anywhere.
 */
function SkillAuthorStage({
  skill,
  onSaved,
}: {
  skill: Skill;
  onSaved: () => void;
}): JSX.Element {
  const [summary, setSummary] = useState(skill.summary);
  const [description, setDescription] = useState(skill.description);
  const [category, setCategory] = useState<SkillCategory>(skill.category);
  const [tagsText, setTagsText] = useState(skill.tags.join(", "));
  const [content, setContent] = useState(skill.content);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Re-seed when the open skill changes (e.g. a re-fetch after save).
  useEffect(() => {
    setSummary(skill.summary);
    setDescription(skill.description);
    setCategory(skill.category);
    setTagsText(skill.tags.join(", "));
    setContent(skill.content);
  }, [skill]);

  const dirty =
    summary !== skill.summary ||
    description !== skill.description ||
    category !== skill.category ||
    tagsText !== skill.tags.join(", ") ||
    content !== skill.content;

  const save = async (): Promise<void> => {
    setSaving(true);
    setError(null);
    try {
      const payload: SkillUpdatePayload = {
        summary,
        description,
        category,
        content,
        tags: tagsText
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
      };
      await caliberApi.updateSkill(skill.skill_id, payload);
      setSavedAt(Date.now());
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save skill");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div data-testid="skill-author-stage" className="space-y-4">
      <div className="rounded-2xl border border-slate-200/70 bg-white p-5 shadow-card space-y-4">
        <div>
          <h2 className="text-sm font-semibold text-slate-900">Author</h2>
          <p className="mt-1 text-xs text-slate-500">
            Edit <span className="font-mono">{skill.name}</span> and save a new version.
          </p>
        </div>

        <label className="block">
          <span className="block text-[11px] font-semibold uppercase tracking-wider text-slate-400 mb-1.5">Summary</span>
          <input
            aria-label="Skill summary"
            value={summary}
            onChange={(e) => setSummary(e.target.value)}
            className="form-input"
          />
        </label>

        <label className="block">
          <span className="block text-[11px] font-semibold uppercase tracking-wider text-slate-400 mb-1.5">Description</span>
          <textarea
            aria-label="Skill description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={2}
            className="form-input"
          />
        </label>

        <div className="grid gap-4 sm:grid-cols-2">
          <label className="block">
            <span className="block text-[11px] font-semibold uppercase tracking-wider text-slate-400 mb-1.5">Category</span>
            <select
              aria-label="Skill category"
              value={category}
              onChange={(e) => setCategory(e.target.value as SkillCategory)}
              className="form-input"
            >
              {(Object.keys(CATEGORY_LABELS) as SkillCategory[]).map((c) => (
                <option key={c} value={c}>{humanizeCategory(c)}</option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className="block text-[11px] font-semibold uppercase tracking-wider text-slate-400 mb-1.5">Tags (comma-separated)</span>
            <input
              aria-label="Skill tags"
              value={tagsText}
              onChange={(e) => setTagsText(e.target.value)}
              className="form-input"
            />
          </label>
        </div>

        <label className="block">
          <span className="block text-[11px] font-semibold uppercase tracking-wider text-slate-400 mb-1.5">Content</span>
          <textarea
            aria-label="Skill content"
            value={content}
            onChange={(e) => setContent(e.target.value)}
            rows={10}
            className="form-input font-mono text-sm"
          />
        </label>

        {error && (
          <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
            {error}
          </div>
        )}

        <div className="flex items-center justify-end gap-3">
          {savedAt !== null && !dirty && !error && (
            <span className="text-xs text-emerald-600">Saved</span>
          )}
          <button
            type="button"
            data-testid="skill-author-save"
            onClick={() => void save()}
            disabled={saving || !dirty}
            className="btn-primary disabled:opacity-50"
          >
            {saving ? "Saving…" : "Save changes"}
          </button>
        </div>
      </div>
    </div>
  );
}

/* ── Scenario Sets ────────────────────────────────────────────────────────── */

/** One scenario case: a user message + whether the skill should auto-select. */
export interface SkillScenarioCase {
  id: string;
  userMessage: string;
  expectedSelected: boolean;
  notes: string;
  tags: string[];
}

/**
 * Scenario Sets stage: build/manage the scenario cases (user message + expected
 * "selected?" + notes/tags) scoped to the skill, with NO agent pick. These feed
 * Trigger Tests / Runs. Mirrors the case-editor pattern but speaks
 * skill/scenario language.
 */
function SkillScenarioSetsStage({
  skill,
  scenarios,
  onChange,
  onGoToTrigger,
}: {
  skill: Skill;
  scenarios: SkillScenarioCase[];
  onChange: (next: SkillScenarioCase[]) => void;
  onGoToTrigger: () => void;
}): JSX.Element {
  const [draftMessage, setDraftMessage] = useState("");
  const [draftExpected, setDraftExpected] = useState(true);
  const [draftNotes, setDraftNotes] = useState("");
  const [draftTags, setDraftTags] = useState("");

  const addCase = (): void => {
    const userMessage = draftMessage.trim();
    if (!userMessage) return;
    onChange([
      ...scenarios,
      {
        id: `sc-${Date.now()}-${scenarios.length}`,
        userMessage,
        expectedSelected: draftExpected,
        notes: draftNotes.trim(),
        tags: draftTags
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
      },
    ]);
    setDraftMessage("");
    setDraftNotes("");
    setDraftTags("");
    setDraftExpected(true);
  };

  const removeCase = (id: string): void => {
    onChange(scenarios.filter((c) => c.id !== id));
  };

  return (
    <div data-testid="skill-scenario-sets" className="space-y-4">
      <div className="rounded-lg border border-caliber-100 bg-caliber-50/60 px-4 py-3 text-sm text-caliber-800">
        Build scenario cases for <span className="font-mono">{skill.name}</span> — a
        user message plus whether this skill should auto-select. Run them on{" "}
        <button
          type="button"
          onClick={onGoToTrigger}
          className="font-medium text-caliber-700 underline"
        >
          Trigger Tests
        </button>
        . No agent required.
      </div>

      {/* Add a scenario */}
      <div className="rounded-2xl border border-slate-200/70 bg-white p-5 shadow-card space-y-3">
        <h3 className="text-sm font-semibold text-slate-900">Add scenario</h3>
        <label className="block">
          <span className="block text-[11px] font-semibold uppercase tracking-wider text-slate-400 mb-1.5">User message</span>
          <textarea
            aria-label="Scenario user message"
            value={draftMessage}
            onChange={(e) => setDraftMessage(e.target.value)}
            rows={2}
            className="form-input"
            placeholder="e.g. Summarize this 30-page contract into bullet points"
          />
        </label>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="flex items-center gap-2 text-sm text-slate-600">
            <input
              type="checkbox"
              aria-label="Expected: skill should be selected"
              checked={draftExpected}
              onChange={(e) => setDraftExpected(e.target.checked)}
              className="h-4 w-4 rounded border-slate-300 text-caliber-600 focus:ring-caliber-500"
            />
            Expected: skill should be selected
          </label>
          <label className="block">
            <span className="block text-[11px] font-semibold uppercase tracking-wider text-slate-400 mb-1.5">Tags (comma-separated)</span>
            <input
              aria-label="Scenario tags"
              value={draftTags}
              onChange={(e) => setDraftTags(e.target.value)}
              className="form-input"
            />
          </label>
        </div>
        <label className="block">
          <span className="block text-[11px] font-semibold uppercase tracking-wider text-slate-400 mb-1.5">Notes</span>
          <input
            aria-label="Scenario notes"
            value={draftNotes}
            onChange={(e) => setDraftNotes(e.target.value)}
            className="form-input"
          />
        </label>
        <div className="flex justify-end">
          <button
            type="button"
            data-testid="skill-scenario-add"
            onClick={addCase}
            disabled={!draftMessage.trim()}
            className="btn-primary disabled:opacity-50"
          >
            Add scenario
          </button>
        </div>
      </div>

      {/* Existing scenarios */}
      {scenarios.length === 0 ? (
        <div className="rounded-lg border border-dashed border-zinc-300 bg-zinc-50 px-4 py-6 text-center text-xs text-zinc-400">
          No scenarios yet. Add one above to drive the Trigger Tests.
        </div>
      ) : (
        <div className="space-y-2">
          <div className="text-xs font-semibold text-slate-500">
            {scenarios.length} scenario{scenarios.length === 1 ? "" : "s"}
          </div>
          {scenarios.map((c, index) => (
            <div
              key={c.id}
              data-testid="skill-scenario-case"
              className="group rounded-xl border border-slate-200/70 bg-white p-4 shadow-card"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="flex h-5 w-5 items-center justify-center rounded-lg bg-slate-100 text-[10px] font-bold text-slate-500">{index + 1}</span>
                    <span className="text-sm font-medium text-slate-900">{c.userMessage}</span>
                    <span
                      className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${
                        c.expectedSelected
                          ? "bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200/60"
                          : "bg-slate-100 text-slate-500 ring-1 ring-slate-200/60"
                      }`}
                    >
                      {c.expectedSelected ? "expect selected" : "expect not selected"}
                    </span>
                  </div>
                  {c.notes && <div className="ml-7 mt-1 text-xs text-slate-400">{c.notes}</div>}
                  {c.tags.length > 0 && (
                    <div className="ml-7 mt-2 flex flex-wrap gap-1.5">
                      {c.tags.map((tag) => (
                        <span key={tag} className="rounded-md bg-slate-50 px-2 py-0.5 text-[10px] font-semibold text-slate-500 ring-1 ring-slate-200/50">
                          {tag}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
                <button
                  type="button"
                  aria-label={`Remove scenario ${index + 1}`}
                  onClick={() => removeCase(c.id)}
                  className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-300 hover:bg-red-50 hover:text-red-500 transition-all"
                >
                  <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2" />
                  </svg>
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ── Trigger Tests (test-selection) ──────────────────────────────────────── */

interface SkillTriggerResult {
  id: string;
  userMessage: string;
  expectedSelected: boolean | null;
  result: SkillSelectionResult;
}

/**
 * Trigger Tests stage: the core "does the skill correctly auto-select?" unit
 * test. Input a user message (+ optional artifact_type / session_goal) → call
 * ``testSkillSelection`` → show is_selected / selection_score / selection_reason.
 * The whole loaded scenario set can be run at once, and a completed batch saved
 * as a durable ``kind:"selection"`` run.
 */
function SkillTriggerTestsStage({
  skill,
  scenarios,
  onAfterRun,
  onGoToScenarios,
}: {
  skill: Skill;
  scenarios: SkillScenarioCase[];
  onAfterRun: () => void;
  onGoToScenarios: () => void;
}): JSX.Element {
  const [userMessage, setUserMessage] = useState("");
  const [artifactType, setArtifactType] = useState("");
  const [sessionGoal, setSessionGoal] = useState("");
  const [running, setRunning] = useState(false);
  const [results, setResults] = useState<SkillTriggerResult[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedRunId, setSavedRunId] = useState<string | null>(null);

  const runOne = async (): Promise<void> => {
    const message = userMessage.trim();
    if (!message) return;
    setRunning(true);
    setError(null);
    setSavedRunId(null);
    try {
      const res = await caliberApi.testSkillSelection(skill.skill_id, {
        user_message: message,
        artifact_type: artifactType.trim() || undefined,
        session_goal: sessionGoal.trim() || undefined,
      });
      setResults((cur) => [
        { id: `tr-${Date.now()}`, userMessage: message, expectedSelected: null, result: res },
        ...cur,
      ]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Selection check failed");
    } finally {
      setRunning(false);
    }
  };

  const runScenarios = async (): Promise<void> => {
    if (scenarios.length === 0) return;
    setRunning(true);
    setError(null);
    setSavedRunId(null);
    try {
      const out: SkillTriggerResult[] = [];
      for (const c of scenarios) {
        const res = await caliberApi.testSkillSelection(skill.skill_id, {
          user_message: c.userMessage,
        });
        out.push({
          id: c.id,
          userMessage: c.userMessage,
          expectedSelected: c.expectedSelected,
          result: res,
        });
      }
      setResults(out);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Scenario run failed");
    } finally {
      setRunning(false);
    }
  };

  // Build the durable per-case results from the current batch. A scenario case
  // with an explicit expectation is judged against it; an ad-hoc check just
  // records the decision.
  const saveRun = async (): Promise<void> => {
    if (results.length === 0) return;
    setSaving(true);
    setError(null);
    try {
      const cases: SkillTestRunResultCase[] = results.map((r) => {
        const matched =
          r.expectedSelected == null
            ? true
            : r.result.is_selected === r.expectedSelected;
        return {
          name: r.userMessage.slice(0, 120) || "selection case",
          input: { user_message: r.userMessage },
          output: {
            is_selected: r.result.is_selected,
            selection_score: r.result.selection_score,
          },
          verdict: matched ? "pass" : "fail",
          score: matched ? 1 : 0,
          reasoning: r.result.selection_reason,
        };
      });
      const saved = await caliberApi.saveSkillTestRun({
        skill_id: skill.skill_id,
        kind: "selection",
        skill_version: skill.version,
        results: cases,
      });
      setSavedRunId(saved.test_run_id);
      onAfterRun();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save run");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div data-testid="skill-trigger-tests" className="space-y-4">
      <div className="rounded-lg border border-caliber-100 bg-caliber-50/60 px-4 py-3 text-sm text-caliber-800">
        Check whether <span className="font-mono">{skill.name}</span> auto-selects for
        a user message. This is the core trigger unit test — no agent involved.
      </div>

      {/* Ad-hoc selection check */}
      <div className="rounded-2xl border border-slate-200/70 bg-white p-5 shadow-card space-y-3">
        <label className="block">
          <span className="block text-[11px] font-semibold uppercase tracking-wider text-slate-400 mb-1.5">User message</span>
          <textarea
            data-testid="skill-trigger-message"
            aria-label="Trigger test user message"
            value={userMessage}
            onChange={(e) => setUserMessage(e.target.value)}
            rows={2}
            className="form-input"
            placeholder="e.g. Extract the line items from this invoice PDF"
          />
        </label>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block">
            <span className="block text-[11px] font-semibold uppercase tracking-wider text-slate-400 mb-1.5">Artifact type (optional)</span>
            <input
              aria-label="Trigger test artifact type"
              value={artifactType}
              onChange={(e) => setArtifactType(e.target.value)}
              className="form-input"
            />
          </label>
          <label className="block">
            <span className="block text-[11px] font-semibold uppercase tracking-wider text-slate-400 mb-1.5">Session goal (optional)</span>
            <input
              aria-label="Trigger test session goal"
              value={sessionGoal}
              onChange={(e) => setSessionGoal(e.target.value)}
              className="form-input"
            />
          </label>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <button
            type="button"
            onClick={() => void runScenarios()}
            disabled={running || scenarios.length === 0}
            title={scenarios.length === 0 ? "Add cases on Scenario Sets first" : undefined}
            className="rounded-md border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
          >
            {scenarios.length > 0
              ? `Run ${scenarios.length} scenario${scenarios.length === 1 ? "" : "s"}`
              : "No scenarios"}
          </button>
          <button
            type="button"
            data-testid="skill-trigger-run"
            onClick={() => void runOne()}
            disabled={running || !userMessage.trim()}
            className="btn-primary disabled:opacity-50"
          >
            {running ? "Checking…" : "Check selection"}
          </button>
        </div>
        {scenarios.length === 0 && (
          <p className="text-[11px] text-slate-400">
            Tip: build a reusable batch on{" "}
            <button type="button" onClick={onGoToScenarios} className="underline">
              Scenario Sets
            </button>
            .
          </p>
        )}
      </div>

      {error && (
        <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          {error}
        </div>
      )}

      {/* Results */}
      {results.length > 0 && (
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold text-slate-700">Selection results</h3>
            <div className="flex items-center gap-3">
              {savedRunId && (
                <span data-testid="skill-trigger-saved" className="text-xs text-emerald-600">
                  Saved run {savedRunId}
                </span>
              )}
              <button
                type="button"
                data-testid="skill-trigger-save"
                onClick={() => void saveRun()}
                disabled={saving}
                className="rounded-md border border-blue-200 bg-white px-2.5 py-1 text-[11px] font-medium text-blue-700 hover:bg-blue-50 disabled:opacity-50"
              >
                {saving ? "Saving…" : "Save as run"}
              </button>
            </div>
          </div>
          {results.map((r) => {
            const matched =
              r.expectedSelected == null
                ? null
                : r.result.is_selected === r.expectedSelected;
            return (
              <div
                key={r.id}
                data-testid="skill-trigger-result"
                className={`rounded-xl border bg-white p-4 text-xs shadow-card ${
                  matched === false ? "border-red-300" : "border-slate-200/70"
                }`}
              >
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <span
                    data-testid="skill-trigger-selected"
                    className={`rounded-full px-2 py-0.5 font-semibold ${
                      r.result.is_selected
                        ? "bg-emerald-100 text-emerald-700"
                        : "bg-slate-100 text-slate-600"
                    }`}
                  >
                    {r.result.is_selected ? "selected" : "not selected"}
                  </span>
                  <span className="text-slate-500">
                    Score{" "}
                    <span className="font-semibold text-slate-800">
                      {(r.result.selection_score * 100).toFixed(0)}%
                    </span>
                  </span>
                  {matched != null && (
                    <span
                      className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${
                        matched ? "bg-emerald-50 text-emerald-700" : "bg-red-100 text-red-700"
                      }`}
                    >
                      {matched ? "matches expectation" : "regression"}
                    </span>
                  )}
                </div>
                <p className="text-slate-700">
                  <span className="font-medium text-slate-500">Message:</span> {r.userMessage}
                </p>
                <p className="mt-1 text-slate-500">
                  <span className="font-medium">Reason:</span> {r.result.selection_reason || "—"}
                </p>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/* ── Runs ─────────────────────────────────────────────────────────────────── */

/** Pass/fail/partial tallies + mean score for a per-case result array. */
function summarizeSkillResults(results: SkillTestRunResultCase[]): {
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

const SKILL_RUN_KIND_FILTERS: Array<{ value: "" | "selection" | "render" | "scenario"; label: string }> = [
  { value: "", label: "All kinds" },
  { value: "selection", label: "Selection" },
  { value: "scenario", label: "Scenario" },
  { value: "render", label: "Render" },
];

/**
 * Runs stage: durable skill-run history + per-case pass/fail/score; set-baseline;
 * diff/regression vs the pinned baseline (mirrors PromptRunsStage). Also hosts
 * the de-frictioned calibrate action (``calibrateSkill``, NO agent) which queues
 * an improvement run.
 */
function SkillRunsStage({
  skill,
  workspace,
  onAfterRun,
}: {
  skill: Skill;
  workspace: SkillWorkspaceResponse | null;
  onAfterRun: () => void;
}): JSX.Element {
  const skillId = skill.skill_id;
  const baselineRunId = workspace?.baseline_run_id ?? null;

  const [kindFilter, setKindFilter] = useState<"" | "selection" | "render" | "scenario">("");
  const [history, setHistory] = useState<SkillTestRunSummary[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(true);

  const [viewedRunId, setViewedRunId] = useState<string | null>(null);
  const [viewedDetail, setViewedDetail] = useState<SkillTestRunDetail | null>(null);
  const [viewedLoading, setViewedLoading] = useState(false);
  const [baselineDetail, setBaselineDetail] = useState<SkillTestRunDetail | null>(null);
  const [pinning, setPinning] = useState(false);
  const [pinError, setPinError] = useState<string | null>(null);

  const [calibrating, setCalibrating] = useState(false);
  const [calibrateMsg, setCalibrateMsg] = useState<string | null>(null);
  const [calibrateError, setCalibrateError] = useState<string | null>(null);

  const refreshHistory = useCallback(
    async (signal?: AbortSignal) => {
      setLoadingHistory(true);
      try {
        const runs = await caliberApi.listSkillTestRuns(
          skillId,
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
    [skillId, kindFilter],
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
      .getSkillTestRun(viewedRunId)
      .then((d) => {
        if (!cancelled) setViewedDetail(d);
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
      .getSkillTestRun(baselineRunId)
      .then((d) => {
        if (!cancelled) setBaselineDetail(d);
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
      await caliberApi.setSkillBaseline(skillId, testRunId);
      onAfterRun();
    } catch (err) {
      setPinError(err instanceof Error ? err.message : "Failed to set baseline");
    } finally {
      setPinning(false);
    }
  };

  const calibrate = async (): Promise<void> => {
    setCalibrating(true);
    setCalibrateError(null);
    setCalibrateMsg(null);
    try {
      const res = await caliberApi.calibrateSkill(skillId, {});
      const jobId = typeof res.job?.job_id === "string" ? res.job.job_id : null;
      setCalibrateMsg(
        jobId ? `Calibration queued — job ${jobId}.` : "Calibration queued.",
      );
      onAfterRun();
    } catch (err) {
      setCalibrateError(err instanceof Error ? err.message : "Failed to queue calibration");
    } finally {
      setCalibrating(false);
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
    const curScore = summarizeSkillResults(viewedDetail.results).overallScore ?? 0;
    const baseScore = summarizeSkillResults(baselineDetail.results).overallScore ?? 0;
    return { rows, regressions, scoreDelta: curScore - baseScore };
  }, [showComparison, viewedDetail, baselineDetail]);

  const viewedSummary = viewedDetail ? summarizeSkillResults(viewedDetail.results) : null;

  const renderCaseValue = (value: unknown): string => {
    if (value == null) return "—";
    if (typeof value === "string") return value;
    return JSON.stringify(value, null, 2);
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-caliber-100 bg-caliber-50/60 px-4 py-3">
        <div className="text-sm text-caliber-800">
          Durable run history for this skill. Runs are captured from{" "}
          <span className="font-medium">Trigger Tests</span>; pin one as the baseline to compare against.
        </div>
        <label className="flex items-center gap-2 text-xs text-zinc-600">
          Kind
          <select
            aria-label="Filter runs by kind"
            value={kindFilter}
            onChange={(e) => {
              setKindFilter(e.target.value as "" | "selection" | "render" | "scenario");
              setViewedRunId(null);
            }}
            className="rounded-md border border-zinc-300 bg-white px-2 py-1 text-xs focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500 outline-none"
          >
            {SKILL_RUN_KIND_FILTERS.map((opt) => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        </label>
      </div>

      {/* ── Calibrate (agent-free) ── */}
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-slate-200/70 bg-white p-4 shadow-card">
        <div className="text-sm text-slate-600">
          <span className="font-semibold text-slate-800">Calibrate this skill</span> — queue an
          improvement run. Caliber auto-provisions the target; no agent to pick.
        </div>
        <button
          type="button"
          data-testid="skill-calibrate-btn"
          onClick={() => void calibrate()}
          disabled={calibrating}
          className="btn-primary disabled:opacity-50"
        >
          {calibrating ? "Queuing…" : "Calibrate"}
        </button>
      </div>
      {calibrateMsg && (
        <div data-testid="skill-calibrate-result" className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-700">
          {calibrateMsg}
        </div>
      )}
      {calibrateError && (
        <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          {calibrateError}
        </div>
      )}

      {pinError && (
        <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          {pinError}
        </div>
      )}

      {/* Viewed run results + score */}
      {viewedRunId && (
        <div data-testid="skill-workspace-run-results" className="rounded-lg border border-zinc-200 bg-white p-4">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-semibold text-zinc-700">
                {viewedRunId === history[0]?.test_run_id ? "Latest run" : "Selected run"}
              </h3>
              {viewedIsBaseline ? (
                <span
                  data-testid="skill-run-baseline-marker"
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
        <div data-testid="skill-workspace-run-comparison" className="rounded-lg border border-blue-200 bg-blue-50/40 p-4">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-sm font-semibold text-blue-900">Vs. baseline</h3>
            <span
              data-testid="skill-run-score-delta"
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

          <div data-testid="skill-run-regressions" className="mb-3">
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
      <div data-testid="skill-workspace-run-history" className="space-y-2">
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
            No saved runs yet. Run a batch on Trigger Tests to capture one.
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

/* ── Bind ─────────────────────────────────────────────────────────────────── */

type SkillBindKind = "agent" | "workflow_node" | "standalone";

/** Human-readable label for a recorded binding (read off ``bound_to``). */
function describeSkillBinding(boundTo: Record<string, unknown> | null): string | null {
  if (!boundTo || typeof boundTo.kind !== "string") return null;
  const kind = boundTo.kind as string;
  if (kind === "agent") {
    return typeof boundTo.agent_id === "string"
      ? `Production agent · ${boundTo.agent_id}`
      : "Production agent";
  }
  if (kind === "workflow_node") {
    const wf = typeof boundTo.workflow_id === "string" ? boundTo.workflow_id : "?";
    const node = typeof boundTo.node_id === "string" ? boundTo.node_id : "?";
    return `Workflow node · ${wf} / ${node}`;
  }
  if (kind === "standalone") return "Standalone";
  return kind;
}

/**
 * Bind stage: attach a skill to where it actually runs — a production agent
 * (picked from the registry), a workflow node, or standalone. On bind we POST
 * ``/skills/{id}/bind`` and refetch the workspace so the header status flips to
 * **Bound** and the current-binding panel reflects the new target.
 */
function SkillBindStage({
  skill,
  boundTo,
  status,
  onBound,
}: {
  skill: Skill;
  boundTo: Record<string, unknown> | null;
  status: string | null;
  onBound: () => void;
}): JSX.Element {
  const boundKind =
    boundTo && typeof boundTo.kind === "string" ? (boundTo.kind as string) : null;
  const currentLabel = describeSkillBinding(boundTo);
  const notReady = status === "Draft" || status === "Has scenarios";

  const [kind, setKind] = useState<SkillBindKind>("agent");
  const [agents, setAgents] = useState<AgentConfig[]>([]);
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [loadingTargets, setLoadingTargets] = useState(false);
  const [selectedAgentId, setSelectedAgentId] = useState("");
  const [selectedWorkflowId, setSelectedWorkflowId] = useState("");
  const [nodeId, setNodeId] = useState("");
  const [binding, setBinding] = useState(false);
  const [bindError, setBindError] = useState<string | null>(null);

  // Load real agents + workflows for the pickers.
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
        if (agentList.length > 0) setSelectedAgentId((cur) => cur || agentList[0]!.agent_id);
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
      (kind === "workflow_node" && Boolean(selectedWorkflowId) && Boolean(nodeId.trim())));

  const submitBind = async (): Promise<void> => {
    setBinding(true);
    setBindError(null);
    try {
      let payload: SkillBindPayload;
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
      await caliberApi.bindSkill(skill.skill_id, payload);
      onBound();
    } catch (err) {
      setBindError(err instanceof Error ? err.message : "Failed to bind skill");
    } finally {
      setBinding(false);
    }
  };

  const KIND_OPTIONS: Array<{ value: SkillBindKind; label: string; hint: string }> = [
    { value: "agent", label: "Production agent", hint: "Attach this skill to a registered agent." },
    { value: "workflow_node", label: "Workflow node", hint: "Wire this skill to a workflow agent node." },
    { value: "standalone", label: "Standalone", hint: "Keep this skill on its own." },
  ];

  return (
    <div className="space-y-4">
      <div className="rounded-2xl border border-slate-200/70 bg-white p-5 shadow-card">
        <h2 className="text-sm font-semibold text-slate-900">Bind</h2>
        <p className="mt-1 text-xs text-slate-500">
          Attach the skill <span className="font-mono">{skill.name}</span> to where it runs.
        </p>

        {/* ── Current binding ── */}
        <div className="mt-4 rounded-xl border border-slate-200 bg-slate-50/60 p-4">
          <div className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            Current binding
          </div>
          {boundKind ? (
            <div
              data-testid="skill-workspace-bound-to"
              className="mt-2 inline-flex items-center gap-1.5 rounded-full bg-emerald-50 px-2.5 py-0.5 text-xs font-medium text-emerald-700 ring-1 ring-emerald-200/60"
            >
              {currentLabel ?? boundKind}
            </div>
          ) : (
            <div className="mt-2 text-sm text-slate-500">
              Not bound yet — this skill stands alone.
            </div>
          )}
        </div>

        {/* ── Pick a target ── */}
        <div className="mt-4 space-y-3 rounded-xl border border-slate-200 bg-white p-4">
          {notReady && (
            <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-700">
              Tip: test or calibrate this skill first so you bind a vetted skill. You can still bind now.
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
                  <div className="mt-0.5 text-[11px] text-slate-400">{opt.hint}</div>
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
                <div className="text-xs text-slate-400 animate-pulse py-2">Loading agents…</div>
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
                  <div className="text-xs text-slate-400 animate-pulse py-2">Loading workflows…</div>
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
              Binding as standalone records that this skill is intentionally kept on its own,
              with no agent or workflow node attached.
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
              aria-label="Bind skill"
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

/* ── Render Preview panel (variable substitution) ─────────────────────────── */

export function parseSkillVariables(raw: string): Record<string, string> {
  const parsed = JSON.parse(raw) as unknown;
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("Variables must be a JSON object.");
  }
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(parsed)) {
    out[key] = typeof value === "string" ? value : JSON.stringify(value);
  }
  return out;
}

export function detectedSkillVariables(content: string): string[] {
  const names = new Set<string>();
  for (const match of content.matchAll(/\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g)) {
    if (match[1]) names.add(match[1]);
  }
  return Array.from(names).sort();
}

export function SkillPlaygroundPanel({
  skills,
  loading,
}: {
  skills: Skill[];
  loading: boolean;
}): JSX.Element {
  const [selectedSkillId, setSelectedSkillId] = useState("");
  const selected = skills.find((skill) => skill.skill_id === selectedSkillId) ?? skills[0] ?? null;
  const [variablesJson, setVariablesJson] = useState("{}");
  const [rendering, setRendering] = useState(false);
  const [result, setResult] = useState<SkillRenderResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const localVariables = selected ? detectedSkillVariables(selected.content) : [];

  useEffect(() => {
    if (!selectedSkillId && skills.length > 0) {
      setSelectedSkillId(skills[0]!.skill_id);
    }
  }, [selectedSkillId, skills]);

  const renderSkill = async (): Promise<void> => {
    if (!selected) return;
    setRendering(true);
    setError(null);
    try {
      const variables = parseSkillVariables(variablesJson);
      const rendered = await caliberApi.testRenderSkill(selected.skill_id, variables);
      setResult(rendered);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Skill render failed");
    } finally {
      setRendering(false);
    }
  };

  if (loading) {
    return (
      <div className="space-y-3">
        {[1, 2].map((i) => (
          <div key={i} className="rounded-2xl border border-slate-200/60 bg-white p-5 shadow-card shimmer h-16" />
        ))}
      </div>
    );
  }

  if (!selected) {
    return (
      <div className="rounded-2xl border-2 border-dashed border-slate-200 bg-gradient-hero px-8 py-12 text-center">
        <div className="flex items-center justify-center w-14 h-14 mx-auto rounded-2xl bg-white shadow-card mb-4">
          <svg className="w-7 h-7 text-slate-300" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
            <path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z" />
          </svg>
        </div>
        <div className="text-sm font-semibold text-slate-600">No skills available for playground rendering</div>
        <div className="mt-1.5 text-xs text-slate-400">Create a skill first to test it in the playground.</div>
      </div>
    );
  }

  return (
    <div data-testid="skill-playground-panel" className="grid grid-cols-1 gap-6 lg:grid-cols-12">
      <div className="lg:col-span-4 space-y-5">
        <div className="rounded-2xl border border-slate-200/60 bg-white p-5 shadow-card">
        <label className="block">
          <span className="block text-[11px] font-semibold uppercase tracking-wider text-slate-400 mb-1.5">Skill</span>
          <select
            aria-label="Select playground skill"
            value={selected.skill_id}
            onChange={(event) => {
              setSelectedSkillId(event.target.value);
              setResult(null);
              setError(null);
            }}
            className="form-input"
          >
            {skills.map((skill) => (
              <option key={skill.skill_id} value={skill.skill_id}>{skill.name}</option>
            ))}
          </select>
        </label>
        </div>

        <div className="rounded-2xl border border-slate-200/60 bg-white p-5 shadow-card space-y-4">
          <div className="flex items-center gap-2">
            <div className="flex items-center justify-center w-7 h-7 rounded-lg bg-gradient-brand-subtle">
              <svg className="w-3.5 h-3.5 text-caliber-purple" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
                <path d="M4 19.5A2.5 2.5 0 016.5 17H20" />
                <path d="M4 4.5A2.5 2.5 0 016.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15z" />
              </svg>
            </div>
            <h3 className="text-xs font-bold uppercase tracking-wider text-slate-900">Skill Details</h3>
          </div>
          <dl className="space-y-2.5 text-xs">
            <div className="flex justify-between gap-3">
              <dt className="text-slate-400">Name</dt>
              <dd className="font-mono text-slate-800">{selected.name}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-slate-400">Category</dt>
              <dd><CategoryPill category={selected.category} /></dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-slate-400">Version</dt>
              <dd className="font-mono text-slate-800">v{selected.version}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-slate-400">Status</dt>
              <dd><StatusPill status={selected.status} /></dd>
            </div>
          </dl>
          <p className="border-t border-slate-100 pt-3 text-xs leading-relaxed text-slate-400">
            {selected.summary || selected.description || "No summary."}
          </p>
          <div className="border-t border-slate-100 pt-3">
            <div className="text-[10px] font-bold uppercase tracking-wider text-slate-400">Detected Variables</div>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {localVariables.length > 0 ? localVariables.map((name) => (
                <span key={name} className="rounded-md bg-slate-50 px-2 py-0.5 font-mono text-[10px] font-semibold text-slate-500 ring-1 ring-slate-200/50">
                  {name}
                </span>
              )) : (
                <span className="text-xs text-slate-300">No template variables detected.</span>
              )}
            </div>
          </div>
        </div>
      </div>

      <div className="lg:col-span-8 space-y-5">
        <div className="rounded-2xl border border-slate-200/60 bg-white p-5 shadow-card">
          <label className="block">
            <span className="block text-[11px] font-semibold uppercase tracking-wider text-slate-400 mb-1.5">Variables JSON</span>
            <textarea
              data-testid="skill-playground-variables"
              value={variablesJson}
              onChange={(event) => setVariablesJson(event.target.value)}
              rows={5}
              className="form-input font-mono text-sm"
              placeholder='{"customer_name":"Ada","policy_id":"refund-30"}'
            />
          </label>
          <button
            type="button"
            data-testid="skill-playground-render"
            disabled={rendering}
            onClick={() => void renderSkill()}
            className="btn-primary mt-4 w-full flex items-center justify-center gap-2"
          >
            {rendering ? (
              <>
                <svg className="w-3.5 h-3.5 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 12a9 9 0 11-6.219-8.56" /></svg>
                Rendering…
              </>
            ) : (
              "Render Skill"
            )}
          </button>
        </div>

        {error && (
          <div className="flex items-start gap-3 rounded-2xl border border-red-200/60 bg-red-50 px-5 py-4 shadow-card">
            <svg className="w-4 h-4 mt-0.5 text-red-500 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" />
            </svg>
            <div className="text-sm text-red-700">{error}</div>
          </div>
        )}

        {result ? (
          <div className="rounded-2xl border border-emerald-200/60 bg-white shadow-card overflow-hidden">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-emerald-100 bg-emerald-50/50 px-5 py-3.5">
              <div>
                <h3 className="text-sm font-bold text-emerald-900">Rendered Output</h3>
                <p className="text-xs text-emerald-600">{result.word_count} words · {result.char_count} chars · {result.duration_ms}ms</p>
              </div>
              {result.unresolved_variables.length > 0 && (
                <span className="rounded-md bg-amber-50 px-2.5 py-0.5 text-[10px] font-semibold text-amber-700 ring-1 ring-amber-200/50">
                  {result.unresolved_variables.length} unresolved
                </span>
              )}
            </div>
            <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words px-5 py-4 font-mono text-xs text-slate-700 leading-relaxed">
              {result.rendered_content}
            </pre>
          </div>
        ) : (
          <div className="rounded-2xl border-2 border-dashed border-slate-200 bg-gradient-hero p-10 text-center">
            <div className="flex items-center justify-center w-12 h-12 mx-auto rounded-2xl bg-white shadow-card mb-3">
              <svg className="w-6 h-6 text-slate-300" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                <path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z" />
              </svg>
            </div>
            <p className="text-sm font-semibold text-slate-600">No render yet</p>
            <p className="mt-1.5 text-xs text-slate-400">Supply variables and render the selected skill.</p>
          </div>
        )}
      </div>
    </div>
  );
}

/* ── Shared helpers + pills ───────────────────────────────────────────────── */

interface GeneratedSkillTestCase {
  id: string;
  input: string;
  expectedBehavior: string;
  tags: string[];
}

/** Validate + normalize a raw JSON array of skill test cases (pure helper). */
export function normalizeSkillTestCases(raw: unknown): GeneratedSkillTestCase[] {
  if (!Array.isArray(raw)) {
    throw new Error("Test cases must be a JSON array.");
  }

  return raw.map((row, index) => {
    if (!row || typeof row !== "object") {
      throw new Error(`Test case ${index + 1} must be an object.`);
    }
    const payload = row as Record<string, unknown>;
    const input = typeof payload.input === "string" ? payload.input.trim() : "";
    const expectedBehavior = typeof payload.expectedBehavior === "string"
      ? payload.expectedBehavior.trim()
      : "";
    if (!input || !expectedBehavior) {
      throw new Error(`Test case ${index + 1} needs input and expectedBehavior.`);
    }
    const tags = Array.isArray(payload.tags)
      ? payload.tags.filter((tag): tag is string => typeof tag === "string" && tag.trim().length > 0)
      : [];
    return {
      id: `skill-tc-${Date.now()}-${index}`,
      input,
      expectedBehavior,
      tags,
    };
  });
}

export function agentReferencesSkill(agent: AgentConfig, skillName: string): boolean {
  const rawSkills = agent.optimizer_config.skills;
  return Array.isArray(rawSkills) && rawSkills.includes(skillName);
}

export function agentLabel(agent: AgentConfig): string {
  return agent.name && agent.name !== agent.agent_id
    ? `${agent.name} (${agent.agent_id})`
    : agent.agent_id;
}

function StatusPill({ status }: { status: ResourceStatus }): JSX.Element {
  const cls =
    status === "active"
      ? "bg-emerald-50 text-emerald-600 ring-1 ring-emerald-200/50"
      : "bg-slate-100 text-slate-500 ring-1 ring-slate-200/50";
  return (
    <span className={`text-[10px] font-semibold uppercase px-2 py-0.5 rounded-md ${cls}`}>
      {status}
    </span>
  );
}

/** snake_case → Title Case for the Category filter dropdown options. */
function humanizeCategory(category: string): string {
  return category
    .split("_")
    .map((part) => (part ? part.charAt(0).toUpperCase() + part.slice(1) : part))
    .join(" ");
}

const CATEGORY_LABELS: Record<SkillCategory, string> = {
  document_creation: "Document",
  data_analysis: "Data",
  data_extraction: "Extract",
  code_generation: "Code",
  content_writing: "Writing",
  summarization: "Summary",
  classification: "Classify",
  research: "Research",
  customer_support: "Support",
  communication: "Comms",
  reasoning_planning: "Reasoning",
  tool_integration: "Tools",
  compliance_safety: "Safety",
  workflow_automation: "Workflow",
  mcp_enhancement: "MCP",
  custom: "Custom",
};

const CATEGORY_COLORS: Record<SkillCategory, string> = {
  document_creation: "bg-blue-50 text-blue-600 ring-1 ring-blue-200/50",
  data_analysis: "bg-cyan-50 text-cyan-600 ring-1 ring-cyan-200/50",
  data_extraction: "bg-teal-50 text-teal-600 ring-1 ring-teal-200/50",
  code_generation: "bg-sky-50 text-sky-600 ring-1 ring-sky-200/50",
  content_writing: "bg-pink-50 text-pink-600 ring-1 ring-pink-200/50",
  summarization: "bg-purple-50 text-purple-600 ring-1 ring-purple-200/50",
  classification: "bg-fuchsia-50 text-fuchsia-600 ring-1 ring-fuchsia-200/50",
  research: "bg-emerald-50 text-emerald-600 ring-1 ring-emerald-200/50",
  customer_support: "bg-orange-50 text-orange-600 ring-1 ring-orange-200/50",
  communication: "bg-rose-50 text-rose-600 ring-1 ring-rose-200/50",
  reasoning_planning: "bg-yellow-50 text-yellow-700 ring-1 ring-yellow-200/50",
  tool_integration: "bg-green-50 text-green-600 ring-1 ring-green-200/50",
  compliance_safety: "bg-red-50 text-red-600 ring-1 ring-red-200/50",
  workflow_automation: "bg-amber-50 text-amber-600 ring-1 ring-amber-200/50",
  mcp_enhancement: "bg-violet-50 text-violet-600 ring-1 ring-violet-200/50",
  custom: "bg-slate-50 text-slate-500 ring-1 ring-slate-200/50",
};

function CategoryPill({ category }: { category: SkillCategory }): JSX.Element {
  return (
    <span
      className={`text-[10px] font-semibold uppercase px-2 py-0.5 rounded-md ${CATEGORY_COLORS[category] ?? CATEGORY_COLORS.custom}`}
    >
      {CATEGORY_LABELS[category] ?? category}
    </span>
  );
}
