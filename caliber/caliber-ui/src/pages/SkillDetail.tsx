/**
 * Skill Detail — view a skill's full content and edit it in place.
 *
 * Progressive disclosure: summary (level 1) + content (level 2). Editing the
 * content publishes a new version (the backend bumps ``version`` automatically
 * and audits the diff), so this page surfaces the version prominently and
 * reports the bump after a save.
 *
 * Sections that have no API source — Versions, Used-by, Quick-stats, the
 * selection/render test, the refinement cascade, and token economics — are
 * intentionally deferred (not fabricated). Everything rendered here is backed
 * by the Skill / SkillPackage payloads.
 */

import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { caliberApi } from "@/api/caliberApi";
import type {
  ResourceStatus,
  Skill,
  SkillCategory,
  SkillPackage,
  SkillPackageImportFile,
  SkillPackageImportPayload,
  SkillUpdatePayload,
} from "@/api/types";
import { CopyButton } from "@/components/CopyButton";
import { useApiMutation, useApiQuery, useInvalidate } from "@/hooks/useApiQuery";
import { relativeTime } from "@/lib/time";

const CATEGORY_OPTIONS: SkillCategory[] = [
  "document_creation",
  "data_analysis",
  "data_extraction",
  "code_generation",
  "content_writing",
  "summarization",
  "classification",
  "research",
  "customer_support",
  "communication",
  "reasoning_planning",
  "tool_integration",
  "compliance_safety",
  "workflow_automation",
  "mcp_enhancement",
  "custom",
];

/** Humanize a raw snake_case category into Title-cased prose (data-backed). */
function humanizeCategory(category: string): string {
  return category.replace(/_/g, " ").replace(/\b\w/g, (m) => m.toUpperCase());
}

type DetailTab = "overview" | "content";

interface EditState {
  owner: string;
  summary: string;
  description: string;
  content: string;
  category: SkillCategory;
  tags: string;
  depends_on: string;
  allowed_tools: string;
  skill_metadata: string;
  status: ResourceStatus;
}

function toEditState(skill: Skill): EditState {
  return {
    owner: skill.owner,
    summary: skill.summary,
    description: skill.description,
    content: skill.content,
    category: skill.category,
    tags: skill.tags.join(", "),
    depends_on: skill.depends_on.join(", "),
    allowed_tools: skill.allowed_tools ?? "",
    skill_metadata: JSON.stringify(skill.skill_metadata ?? {}, null, 2),
    status: skill.status,
  };
}

function parseList(value: string): string[] {
  return value
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
}

export function SkillDetail(): JSX.Element {
  const { skillId } = useParams<{ skillId: string }>();
  const invalidate = useInvalidate();
  const navigate = useNavigate();

  const skillQuery = useApiQuery(["skill", skillId], (s) => caliberApi.getSkill(skillId!, s), {
    enabled: Boolean(skillId),
  });
  const packageQuery = useApiQuery(
    ["skill-package", skillId],
    (s) => caliberApi.getSkillPackage(skillId!, s),
    { enabled: Boolean(skillId) },
  );
  // Editing and archiving/restoring a skill are admin-only on the backend
  // (SCOPE_ADMIN); gate the controls so only admins see them.
  const meQuery = useApiQuery(["me"], (s) => caliberApi.getMe(s));
  const isAdmin = meQuery.data?.is_admin ?? false;

  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState<EditState | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [tab, setTab] = useState<DetailTab>("overview");

  // Seed the form whenever we enter edit mode with fresh data.
  useEffect(() => {
    if (editing && skillQuery.data && form === null) {
      setForm(toEditState(skillQuery.data));
    }
  }, [editing, skillQuery.data, form]);

  const saveMut = useApiMutation<Skill, SkillUpdatePayload>(
    (payload) => caliberApi.updateSkill(skillId!, payload),
    {
      onSuccess: (updated) => {
        const bumped = skillQuery.data && updated.version > skillQuery.data.version;
        setMessage(
          bumped
            ? `Saved — content changed, now v${updated.version}.`
            : "Saved.",
        );
        setFormError(null);
        setEditing(false);
        setForm(null);
        invalidate(["skill", skillId]);
      },
    },
  );

  // Import a portable skill package (operator scope). On success the new skill
  // exists, so navigate to its detail page (React Query refetches there).
  const importMut = useApiMutation<Skill, SkillPackageImportPayload>(
    (payload) => caliberApi.importSkillPackage(payload),
    { onSuccess: (created) => navigate(`/skills/${created.skill_id}`) },
  );

  const statusMut = useApiMutation<Skill, ResourceStatus>(
    (status) => caliberApi.updateSkill(skillId!, { status }),
    {
      onSuccess: (updated) => {
        setMessage(updated.status === "active" ? "Skill restored." : "Skill archived.");
        invalidate(["skill", skillId]);
      },
    },
  );

  const skill = skillQuery.data;
  if (skillQuery.isLoading || !skill) {
    return <div className="text-sm text-slate-400">Loading skill…</div>;
  }

  const toggleStatus = (): void =>
    statusMut.mutate(skill.status === "active" ? "archived" : "active");

  const onSave = (): void => {
    if (!form) return;
    let metadata: Record<string, unknown>;
    try {
      const parsed = JSON.parse(form.skill_metadata || "{}") as unknown;
      if (parsed === null || Array.isArray(parsed) || typeof parsed !== "object") {
        setFormError("Metadata must be a JSON object.");
        return;
      }
      metadata = parsed as Record<string, unknown>;
    } catch (err) {
      setFormError(`Metadata JSON is invalid: ${err instanceof Error ? err.message : "parse failed"}`);
      return;
    }
    setFormError(null);
    const payload: SkillUpdatePayload = {
      owner: form.owner.trim(),
      summary: form.summary,
      description: form.description,
      content: form.content,
      category: form.category,
      tags: parseList(form.tags),
      depends_on: parseList(form.depends_on),
      allowed_tools: form.allowed_tools.trim() ? form.allowed_tools.trim() : null,
      skill_metadata: metadata,
      status: form.status,
    };
    saveMut.mutate(payload);
  };

  const statusActive = skill.status === "active";

  return (
    <div className="space-y-6 animate-fade-in">
      {/* ── Breadcrumb + header ── */}
      <div>
        <nav className="flex items-center gap-1.5 text-xs text-slate-400">
          <Link to="/" className="transition-colors hover:text-caliber-purple">Dashboard</Link>
          <Chevron />
          <Link to="/skills" className="transition-colors hover:text-caliber-purple">Skills</Link>
          <Chevron />
          <span className="font-medium text-slate-600">{skill.name}</span>
        </nav>

        <div className="mt-3 flex flex-wrap items-start justify-between gap-4">
          <div className="flex items-start gap-4">
            <div className="grid h-12 w-12 shrink-0 place-items-center rounded-2xl bg-gradient-to-br from-violet-500 to-indigo-600 text-white shadow-card">
              <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
                <path d="M9 12l2 2 4-4" />
              </svg>
            </div>
            <div>
              <div className="flex flex-wrap items-center gap-2.5">
                <h1 className="font-mono text-2xl font-bold tracking-tight text-slate-900">{skill.name}</h1>
                <StatusDotPill status={skill.status} />
                <span className="inline-flex items-center gap-1.5 rounded-md bg-rose-50 px-2 py-0.5 text-[11px] font-semibold text-rose-700 ring-1 ring-rose-200/60">
                  {humanizeCategory(skill.category)}
                </span>
                <span
                  title="Bumps automatically when content changes"
                  className="inline-flex items-center gap-1.5 rounded-md bg-violet-50 px-2 py-0.5 text-[11px] font-semibold text-caliber-purple ring-1 ring-violet-200/70"
                >
                  v{skill.version}
                </span>
              </div>
              <p className="mt-2 max-w-2xl text-sm leading-relaxed text-slate-500">
                {skill.description || <span className="italic text-slate-400">No description provided.</span>}
              </p>
              <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs text-slate-400">
                <span className="inline-flex items-center gap-1">
                  <span className="font-mono">{skill.skill_id}</span>
                  <CopyButton value={skill.skill_id} label="Copy skill ID" testId="copy-skill-id" />
                </span>
                <span className="text-slate-200">·</span>
                <span className="inline-flex items-center gap-1.5">
                  <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2" /><circle cx="12" cy="7" r="4" />
                  </svg>
                  {skill.owner}
                </span>
                <span className="text-slate-200">·</span>
                <span title={skill.updated_at} className="inline-flex items-center gap-1.5">
                  <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <circle cx="12" cy="12" r="10" /><path d="M12 6v6l4 2" />
                  </svg>
                  updated {relativeTime(skill.updated_at)}
                </span>
              </div>
            </div>
          </div>

          {/* Header actions */}
          {!editing && isAdmin && (
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                data-testid="skill-status-btn"
                disabled={statusMut.isPending}
                onClick={toggleStatus}
                className="btn-ghost px-3.5 py-2 disabled:opacity-50"
              >
                {statusActive ? "Archive" : "Restore"}
              </button>
              <button
                type="button"
                data-testid="skill-edit-btn"
                onClick={() => setEditing(true)}
                className="btn-ghost px-3.5 py-2"
              >
                <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7" />
                  <path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z" />
                </svg>
                Edit
              </button>
            </div>
          )}
        </div>
      </div>

      {/* ── Status legend ── */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-xl border border-slate-200/70 bg-white px-4 py-2.5 text-[11px] text-slate-500 shadow-card">
        <span className="inline-flex items-center gap-1.5 font-semibold uppercase tracking-wide text-slate-400">Legend</span>
        <LegendDot color="bg-emerald-500" label="active / passing / @prod" />
        <LegendDot color="bg-amber-500" label="refining / low-confidence / warning" />
        <LegendDot color="bg-slate-400" label="draft / archived / superseded" />
        <LegendDot color="bg-caliber-purple" label="eval-gated / version change" />
        <LegendDot color="bg-blue-500" label="portable / packaged" />
      </div>

      {/* ── Banners ── */}
      {skill.status === "archived" && !editing && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-2.5 text-sm text-amber-800">
          This skill is <strong>archived</strong>. You can still edit it (changes are kept) or
          <strong> Restore</strong> it to make it active again.
        </div>
      )}
      {message && (
        <div className="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-2.5 text-sm text-emerald-700">
          {message}
        </div>
      )}
      {saveMut.isError && (
        <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-2.5 text-sm text-red-700">
          Save failed: {saveMut.error?.message}
        </div>
      )}

      {editing && form ? (
        <EditForm
          form={form}
          onChange={setForm}
          onSave={onSave}
          onCancel={() => {
            setEditing(false);
            setForm(null);
            setFormError(null);
          }}
          saving={saveMut.isPending}
          error={formError}
        />
      ) : (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
          {/* ── Main column (tabbed) ── */}
          <div className="lg:col-span-2">
            <nav className="flex flex-wrap items-center gap-1 border-b border-slate-200/70">
              <TabButton active={tab === "overview"} onClick={() => setTab("overview")}>Overview</TabButton>
              <TabButton active={tab === "content"} onClick={() => setTab("content")}>Content</TabButton>
            </nav>

            {tab === "overview" ? (
              <OverviewTab
                skill={skill}
                packagePreview={packageQuery.data ?? null}
                packageLoading={packageQuery.isLoading}
                packageError={packageQuery.error?.message ?? null}
                onSeeContent={() => setTab("content")}
                canImport={isAdmin}
                importing={importMut.isPending}
                importError={importMut.error?.message ?? null}
                onImportFiles={(files) =>
                  importMut.mutate({ owner: meQuery.data?.user_id ?? "", files })
                }
              />
            ) : (
              <ContentTab skill={skill} onEdit={isAdmin ? () => setEditing(true) : undefined} />
            )}
          </div>

          {/* ── Right rail ── */}
          <RightRail skill={skill} onArchive={toggleStatus} archiveDisabled={statusMut.isPending} canArchive={isAdmin} />
        </div>
      )}

      {/* ── Footer caption ── */}
      <p className="pt-2 text-center text-[11px] text-slate-400">
        CALIBER skill library · progressive-disclosure prompt fragments · auto-selecting,
        dependency-tracked, OpenAI-portable · refine once, cascade everywhere
      </p>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Overview tab                                                                */
/* -------------------------------------------------------------------------- */

function OverviewTab({
  skill,
  packagePreview,
  packageLoading,
  packageError,
  onSeeContent,
  canImport,
  importing,
  importError,
  onImportFiles,
}: {
  skill: Skill;
  packagePreview: SkillPackage | null;
  packageLoading: boolean;
  packageError: string | null;
  onSeeContent: () => void;
  canImport: boolean;
  importing: boolean;
  importError: string | null;
  onImportFiles: (files: SkillPackageImportFile[]) => void;
}): JSX.Element {
  return (
    <div className="space-y-6 pt-5">
      {/* Progressive disclosure */}
      <div className="card overflow-hidden">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-5 py-4">
          <div>
            <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-900">
              <svg className="h-4 w-4 text-caliber-purple" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M12 2L2 7l10 5 10-5-10-5z" /><path d="M2 17l10 5 10-5M2 12l10 5 10-5" />
              </svg>
              Progressive disclosure
            </h2>
            <p className="mt-0.5 text-xs text-slate-500">
              A summary the model always sees, plus full instructions loaded only when the skill is
              selected — so the always-on token cost stays tiny.
            </p>
          </div>
        </div>
        {/* level 1 */}
        <div className="border-b border-slate-100 bg-violet-50/30 px-5 py-4">
          <div className="flex items-center gap-2">
            <span className="grid h-5 w-7 place-items-center rounded-md bg-caliber-purple text-[10px] font-bold text-white">L1</span>
            <span className="text-[11px] font-semibold uppercase tracking-wide text-caliber-purple">Summary — always loaded</span>
            <span className="text-[11px] text-slate-400">used by the selector to decide relevance</span>
          </div>
          <p data-testid="skill-summary" className="mt-2.5 whitespace-pre-wrap text-sm leading-relaxed text-slate-700">
            {skill.summary || <span className="italic text-slate-400">No summary provided.</span>}
          </p>
        </div>
        {/* level 2 pointer */}
        <div className="flex items-center justify-between px-5 py-3.5">
          <div className="flex items-center gap-2">
            <span className="grid h-5 w-7 place-items-center rounded-md bg-slate-200 text-[10px] font-bold text-slate-600">L2</span>
            <span className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">Content — loaded when relevant</span>
          </div>
          <button
            type="button"
            onClick={onSeeContent}
            className="inline-flex items-center gap-1 text-[11px] font-medium text-slate-400 hover:text-caliber-purple"
          >
            See the <span className="font-semibold text-caliber-purple">Content</span> tab for full instructions
            <svg className="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M7 17L17 7M7 7h10v10" /></svg>
          </button>
        </div>
      </div>

      <PackagePanel
        skill={skill}
        packagePreview={packagePreview}
        loading={packageLoading}
        error={packageError}
        canImport={canImport}
        importing={importing}
        importError={importError}
        onImportFiles={onImportFiles}
      />
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Content tab                                                                 */
/* -------------------------------------------------------------------------- */

function ContentTab({ skill, onEdit }: { skill: Skill; onEdit?: () => void }): JSX.Element {
  const [copied, setCopied] = useState(false);

  const onCopy = (): void => {
    void navigator.clipboard?.writeText(skill.content).then(
      () => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1500);
      },
      () => undefined,
    );
  };

  return (
    <div className="space-y-6 pt-5">
      <div className="card overflow-hidden">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-5 py-4">
          <div>
            <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-900">
              <svg className="h-4 w-4 text-caliber-purple" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" /><path d="M14 2v6h6" />
              </svg>
              Content — level 2 instructions
            </h2>
            <p className="mt-0.5 text-xs text-slate-500">
              The reusable Markdown fragment injected into the system prompt of every referencing
              agent. Editing this bumps the version and records an audit diff.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button type="button" onClick={onCopy} className="btn-ghost px-2.5 py-1.5 text-[11px]" title="Copy the raw Markdown source">
              <svg className="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <rect x="9" y="9" width="13" height="13" rx="2" /><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1" />
              </svg>
              {copied ? "Copied" : "Copy"}
            </button>
            {onEdit && (
              <button type="button" onClick={onEdit} className="btn-ghost px-2.5 py-1.5 text-[11px]" title="Open the editor">
                <svg className="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7" />
                  <path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z" />
                </svg>
                Edit
              </button>
            )}
          </div>
        </div>
        <div className="border-t border-slate-100">
          <div className="flex items-center justify-between bg-slate-50/60 px-5 py-2.5">
            <span className="inline-flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
              <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M16 18l6-6-6-6M8 6l-6 6 6 6" /></svg>
              Raw source
            </span>
            <span className="font-mono text-[10px] text-slate-400">text/markdown</span>
          </div>
          <pre
            data-testid="skill-content"
            className="scrollbar-thin max-h-72 overflow-auto whitespace-pre-wrap bg-slate-900 px-5 py-4 font-mono text-[11.5px] leading-relaxed text-slate-300"
          >
            {skill.content}
          </pre>
        </div>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* OpenAI package panel                                                        */
/* -------------------------------------------------------------------------- */

const PACKAGE_KIND_BADGE: Record<string, string> = {
  skill: "bg-violet-50 text-caliber-purple ring-1 ring-violet-200/60",
  manifest: "bg-violet-50 text-caliber-purple ring-1 ring-violet-200/60",
  "agent-metadata": "bg-blue-50 text-blue-700 ring-1 ring-blue-200/60",
  agent: "bg-blue-50 text-blue-700 ring-1 ring-blue-200/60",
};

function PackagePanel({
  skill,
  packagePreview,
  loading,
  error,
  canImport,
  importing,
  importError,
  onImportFiles,
}: {
  skill: Skill;
  packagePreview: SkillPackage | null;
  loading: boolean;
  error: string | null;
  canImport: boolean;
  importing: boolean;
  importError: string | null;
  onImportFiles: (files: SkillPackageImportFile[]) => void;
}): JSX.Element {
  const skillMd = packagePreview?.files.find((file) => file.path.endsWith("/SKILL.md"));
  const openaiYaml = packagePreview?.files.find((file) => file.path.endsWith("/agents/openai.yaml"));

  // Read a selected package folder/files as text and hand the {path, content}
  // records to the parent. A directory pick preserves the <root>/SKILL.md layout
  // (via webkitRelativePath) the importer expects; loose files fall back to name.
  const handlePackageFiles = async (fileList: FileList | null): Promise<void> => {
    if (!fileList || fileList.length === 0) return;
    const files: SkillPackageImportFile[] = [];
    for (const file of Array.from(fileList)) {
      const rel = (file as File & { webkitRelativePath?: string }).webkitRelativePath;
      files.push({ path: rel && rel.length > 0 ? rel : file.name, content: await file.text() });
    }
    onImportFiles(files);
  };

  return (
    <section data-testid="skill-package-panel" className="card overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-5 py-4">
        <div>
          <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-900">
            <svg className="h-4 w-4 text-caliber-purple" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M21 16V8a2 2 0 00-1-1.73l-7-4a2 2 0 00-2 0l-7 4A2 2 0 003 8v8a2 2 0 001 1.73l7 4a2 2 0 002 0l7-4A2 2 0 0021 16z" />
              <path d="M3.27 6.96L12 12.01l8.73-5.05M12 22.08V12" />
            </svg>
            OpenAI-compatible package
          </h2>
          <p className="mt-0.5 font-mono text-xs text-slate-500">
            Portable skill folder — SKILL.md, agents/openai.yaml, and bundled resources.
            Import/export without rewrites.
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {canImport && (
            <label
              data-testid="skill-package-import"
              className={`inline-flex items-center gap-2 rounded-xl border border-slate-200/70 bg-white px-3.5 py-2 text-xs font-semibold text-slate-600 shadow-card transition-colors hover:bg-slate-50 ${
                importing ? "cursor-wait opacity-60" : "cursor-pointer"
              }`}
            >
              <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M7 9l5-5 5 5M12 4v12" />
              </svg>
              {importing ? "Importing…" : "Import package"}
              <input
                ref={(el) => {
                  if (el) el.setAttribute("webkitdirectory", "");
                }}
                type="file"
                multiple
                aria-label="Import skill package folder"
                className="hidden"
                disabled={importing}
                onChange={(e) => {
                  void handlePackageFiles(e.target.files);
                  e.target.value = "";
                }}
              />
            </label>
          )}
          <a
            data-testid="skill-package-download"
            href={caliberApi.skillPackageZipUrl(skill.skill_id)}
            download={`${skill.name}.zip`}
            className="inline-flex items-center gap-2 rounded-xl border border-slate-200/70 bg-white px-3.5 py-2 text-xs font-semibold text-slate-600 shadow-card transition-colors hover:bg-slate-50"
          >
            <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M7 10l5 5 5-5M12 15V3" />
            </svg>
            Download ZIP
          </a>
        </div>
      </div>
      {importError && (
        <div className="px-5 pt-4">
          <div
            data-testid="skill-package-import-error"
            className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700"
          >
            Import failed: {importError}
          </div>
        </div>
      )}

      {loading && <div className="px-5 py-4 text-sm text-slate-400">Loading package preview…</div>}
      {error && (
        <div className="px-5 py-4">
          <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
            Package preview failed: {error}
          </div>
        </div>
      )}
      {packagePreview && (
        <>
          {packagePreview.warnings.length > 0 && (
            <div className="px-5 pt-4">
              <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                {packagePreview.warnings.join(" ")}
              </div>
            </div>
          )}

          <div className="grid gap-3 px-5 py-4 sm:grid-cols-3">
            <PackageCount label="Scripts" value={packagePreview.resource_counts.scripts} />
            <PackageCount label="References" value={packagePreview.resource_counts.references} />
            <PackageCount label="Assets" value={packagePreview.resource_counts.assets} />
          </div>

          <div className="px-5 pb-4">
            <div className="divide-y divide-slate-100 overflow-hidden rounded-xl border border-slate-200/70">
              {packagePreview.files.map((file) => (
                <div key={file.path} className="flex items-center justify-between gap-3 px-3.5 py-2.5">
                  <span className="min-w-0 truncate font-mono text-xs text-slate-700">{file.path}</span>
                  <span
                    className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium uppercase ${PACKAGE_KIND_BADGE[file.kind] ?? "bg-slate-100 text-slate-500"}`}
                  >
                    {file.kind}
                  </span>
                </div>
              ))}
            </div>

            {skillMd && <PackagePreview title="SKILL.md" content={skillMd.content} />}
            {openaiYaml && <PackagePreview title="agents/openai.yaml" content={openaiYaml.content} />}

            <div className="mt-3 flex items-start gap-2 rounded-lg bg-blue-50/60 px-3 py-2.5">
              <svg className="mt-0.5 h-3.5 w-3.5 shrink-0 text-blue-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
              </svg>
              <p className="text-[11px] leading-relaxed text-blue-700/80">
                Import validates exactly one SKILL.md with a kebab-case name, rejects path traversal
                and resources outside scripts/references/assets, and 409s on a duplicate name.
              </p>
            </div>
          </div>
        </>
      )}
    </section>
  );
}

function PackageCount({ label, value }: { label: string; value: number }): JSX.Element {
  return (
    <div className="rounded-xl border border-slate-200/70 bg-slate-50/60 px-3.5 py-3">
      <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">{label}</div>
      <div className="mt-1 font-mono text-lg font-semibold text-slate-900">{value}</div>
    </div>
  );
}

function PackagePreview({ title, content }: { title: string; content: string }): JSX.Element {
  return (
    <div className="mt-3">
      <div className="mb-1 text-xs font-medium text-slate-600">{title}</div>
      <pre className="scrollbar-thin max-h-56 overflow-auto whitespace-pre-wrap rounded-xl border border-slate-200/70 bg-slate-50/60 p-3 font-mono text-xs leading-relaxed text-slate-800">
        {content}
      </pre>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Right rail                                                                  */
/* -------------------------------------------------------------------------- */

function RightRail({
  skill,
  onArchive,
  archiveDisabled,
  canArchive,
}: {
  skill: Skill;
  onArchive: () => void;
  archiveDisabled: boolean;
  canArchive: boolean;
}): JSX.Element {
  const metadataJson = JSON.stringify(skill.skill_metadata ?? {}, null, 2);

  return (
    <div className="space-y-6">
      {/* Metadata */}
      <div className="card p-5">
        <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-900">
          <svg className="h-4 w-4 text-caliber-purple" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="12" cy="12" r="10" /><path d="M12 16v-4M12 8h.01" />
          </svg>
          Metadata
        </h2>
        <p className="mt-0.5 text-xs text-slate-500">Identity, ownership, and lifecycle timestamps.</p>
        <dl className="mt-3 space-y-0 divide-y divide-slate-100 text-xs">
          <MetaRow term="Skill ID"><span className="font-mono text-slate-700">{skill.skill_id}</span></MetaRow>
          <MetaRow term="Name"><span className="font-mono text-slate-700">{skill.name}</span></MetaRow>
          <MetaRow term="Category"><span className="text-slate-700">{humanizeCategory(skill.category)}</span></MetaRow>
          <MetaRow term="Version"><span className="font-mono text-slate-700">v{skill.version} · @{skill.status}</span></MetaRow>
          <MetaRow term="Status"><StatusDotPill status={skill.status} /></MetaRow>
          <MetaRow term="Owner"><span className="text-slate-700">{skill.owner}</span></MetaRow>
          <MetaRow term="Created"><span className="text-slate-700">{relativeTime(skill.created_at)}</span></MetaRow>
          <MetaRow term="Updated"><span className="text-slate-700">{relativeTime(skill.updated_at)}</span></MetaRow>
        </dl>
        <div className="mt-3 border-t border-slate-100 pt-3">
          <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">Tags</div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {skill.tags.length ? (
              skill.tags.map((t) => (
                <span key={t} className="rounded-md bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600">{t}</span>
              ))
            ) : (
              <Empty />
            )}
          </div>
        </div>
      </div>

      {/* Composition */}
      <div className="card p-5">
        <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-900">
          <svg className="h-4 w-4 text-caliber-purple" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="12" cy="18" r="3" /><circle cx="6" cy="6" r="3" /><circle cx="18" cy="6" r="3" />
            <path d="M18 9v1a2 2 0 01-2 2H8a2 2 0 01-2-2V9M12 12v3" />
          </svg>
          Composition
        </h2>
        <p className="mt-0.5 text-xs text-slate-500">
          Skills this one composes, and the tool surface it narrows the agent to while active.
        </p>
        <div className="mt-3">
          <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">Depends on</div>
          <div className="mt-2 space-y-2">
            {skill.depends_on.length ? (
              skill.depends_on.map((dep) => (
                <div key={dep} className="flex items-center gap-2.5 rounded-xl border border-slate-200/70 bg-slate-50/60 p-3">
                  <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-white text-caliber-purple ring-1 ring-slate-200/70">
                    <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                      <path d="M19.439 7.85c-.049.322.059.648.289.878l1.568 1.568c.47.47.706 1.087.706 1.704s-.235 1.233-.706 1.704l-1.611 1.611a.98.98 0 01-.837.276c-.47-.07-.802-.48-.968-.925a2.501 2.501 0 10-3.214 3.214c.446.166.855.497.925.968a.979.979 0 01-.276.837l-1.61 1.61a2.404 2.404 0 01-1.705.707 2.402 2.402 0 01-1.704-.706l-1.568-1.568a1.026 1.026 0 00-.877-.29c-.493.074-.84.504-1.02.968a2.5 2.5 0 11-3.237-3.237c.464-.18.894-.527.967-1.02a1.026 1.026 0 00-.289-.877l-1.568-1.568A2.402 2.402 0 011 12c0-.617.236-1.234.706-1.704L2.318 8.69c.23-.23.556-.338.877-.29.493.074.84.504 1.02.968a2.5 2.5 0 103.237-3.237c-.464-.18-.894-.527-.967-1.02a1.026 1.026 0 01.289-.877l1.568-1.568A2.402 2.402 0 0110.296 1c.617 0 1.234.236 1.704.706l1.611 1.611c.23.23.556.338.877.29.493-.074.84-.504 1.02-.968a2.5 2.5 0 113.237 3.237c-.464.18-.894.527-.967 1.02z" />
                    </svg>
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="text-[13px] font-medium text-slate-800">{dep}</div>
                  </div>
                </div>
              ))
            ) : (
              <Empty />
            )}
          </div>
        </div>
        <div className="mt-4">
          <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">Allowed tools</div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {skill.allowed_tools ? (
              <span className="inline-flex items-center gap-1.5 rounded-md bg-slate-50 px-2 py-1 font-mono text-[11px] font-medium text-slate-600 ring-1 ring-slate-200/70">
                {skill.allowed_tools}
              </span>
            ) : (
              <Empty />
            )}
          </div>
          <p className="mt-2 text-[11px] leading-relaxed text-slate-400">
            The skill narrows the agent's tool surface to the listed tools while it is active.
          </p>
        </div>
      </div>

      {/* skill_metadata JSON */}
      <div className="card overflow-hidden">
        <div className="flex items-center justify-between border-b border-slate-100 px-5 py-3.5">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-900">
            <svg className="h-4 w-4 text-caliber-purple" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M8 3H7a2 2 0 00-2 2v5a2 2 0 01-2 2 2 2 0 012 2v5a2 2 0 002 2h1M16 3h1a2 2 0 012 2v5a2 2 0 002 2 2 2 0 00-2 2v5a2 2 0 01-2 2h-1" />
            </svg>
            skill_metadata
          </h2>
          <span className="font-mono text-[10px] text-slate-400">JSON object</span>
        </div>
        <pre
          data-testid="skill-metadata"
          className="scrollbar-thin max-h-60 overflow-auto bg-slate-900 px-4 py-3.5 font-mono text-[11.5px] leading-relaxed text-slate-300"
        >
          {metadataJson}
        </pre>
      </div>

      {/* Lifecycle */}
      <div className="card p-5">
        <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-900">
          <svg className="h-4 w-4 text-caliber-purple" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /><path d="M9 12l2 2 4-4" />
          </svg>
          Lifecycle
        </h2>
        <p className="mt-0.5 text-xs text-slate-500">How edits, promotions, and removal are governed for this artifact.</p>
        <div className="mt-3 space-y-2.5 text-[11px] leading-relaxed text-slate-500">
          <LifecycleRow color="text-caliber-purple">
            Editing <span className="font-semibold text-slate-600">content</span> bumps the version and
            records the old/new pair in the audit diff. A tags-only edit leaves the version unchanged.
          </LifecycleRow>
          <LifecycleRow color="text-emerald-500">
            A promotion is blocked unless a passing regression replay exists for that exact candidate.
          </LifecycleRow>
          <LifecycleRow color="text-blue-500">
            A checkpoint captures <span className="font-mono text-slate-600">content_before</span> at
            promotion, so any version is one-click rollback-restorable.
          </LifecycleRow>
          <LifecycleRow color="text-amber-500">
            There is no hard delete — <span className="font-mono text-slate-600">archive</span> is the
            remove path, and archived skills stay inspectable.
          </LifecycleRow>
        </div>
        {canArchive && (
          <div className="mt-4 flex flex-col gap-2">
            <button
              type="button"
              disabled={archiveDisabled}
              onClick={onArchive}
              className="btn-ghost w-full justify-center px-3.5 py-2 text-amber-600 hover:text-amber-700 disabled:opacity-50"
              title="Archive — the soft-delete path; the skill stays inspectable"
            >
              <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M21 8v13H3V8M1 3h22v5H1zM10 12h4" />
              </svg>
              {skill.status === "active" ? "Archive skill" : "Restore skill"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function MetaRow({ term, children }: { term: string; children: React.ReactNode }): JSX.Element {
  return (
    <div className="flex items-center justify-between py-2.5">
      <dt className="text-slate-400">{term}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function LifecycleRow({ color, children }: { color: string; children: React.ReactNode }): JSX.Element {
  return (
    <p className="flex gap-2">
      <svg className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${color}`} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <circle cx="12" cy="12" r="9" />
      </svg>
      <span>{children}</span>
    </p>
  );
}

/* -------------------------------------------------------------------------- */
/* Edit form (functionally intact; cosmetic restyle only)                      */
/* -------------------------------------------------------------------------- */

function EditForm({
  form,
  onChange,
  onSave,
  onCancel,
  saving,
  error,
}: {
  form: EditState;
  onChange: (next: EditState) => void;
  onSave: () => void;
  onCancel: () => void;
  saving: boolean;
  error: string | null;
}): JSX.Element {
  const set = <K extends keyof EditState>(key: K, value: EditState[K]): void =>
    onChange({ ...form, [key]: value });

  return (
    <div className="card space-y-4 p-5">
      <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-2.5 text-xs text-amber-800">
        Editing the <strong>content</strong> publishes a new version (the version number bumps and
        the change is recorded in the audit log). Editing only metadata (tags, summary, category…)
        leaves the version unchanged.
      </div>

      {error && (
        <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-2.5 text-sm text-red-700">
          {error}
        </div>
      )}

      <LabeledInput label="Summary (level 1)">
        <textarea
          data-testid="skill-summary"
          className="textarea-base"
          rows={3}
          value={form.summary}
          onChange={(e) => set("summary", e.target.value)}
        />
      </LabeledInput>

      <LabeledInput label="Owner">
        <input
          className="input-base"
          data-testid="skill-owner"
          value={form.owner}
          onChange={(e) => set("owner", e.target.value)}
        />
      </LabeledInput>

      <LabeledInput label="Description">
        <textarea
          className="textarea-base"
          rows={2}
          value={form.description}
          onChange={(e) => set("description", e.target.value)}
        />
      </LabeledInput>

      <LabeledInput label="Content (level 2) — changes create a new version">
        <textarea
          data-testid="skill-content"
          className="textarea-base font-mono text-xs"
          rows={12}
          value={form.content}
          onChange={(e) => set("content", e.target.value)}
        />
      </LabeledInput>

      <div className="grid grid-cols-2 gap-4">
        <LabeledInput label="Category">
          <select
            className="input-base"
            value={form.category}
            onChange={(e) => set("category", e.target.value as SkillCategory)}
          >
            {CATEGORY_OPTIONS.map((c) => (
              <option key={c} value={c}>
                {humanizeCategory(c)}
              </option>
            ))}
          </select>
        </LabeledInput>
        <LabeledInput label="Status">
          <select
            className="input-base"
            value={form.status}
            onChange={(e) => set("status", e.target.value as ResourceStatus)}
          >
            <option value="active">active</option>
            <option value="archived">archived</option>
          </select>
        </LabeledInput>
        <LabeledInput label="Tags (comma-separated)">
          <input className="input-base" value={form.tags} onChange={(e) => set("tags", e.target.value)} />
        </LabeledInput>
        <LabeledInput label="Depends on (comma-separated)">
          <input className="input-base" value={form.depends_on} onChange={(e) => set("depends_on", e.target.value)} />
        </LabeledInput>
        <LabeledInput label="Allowed tools">
          <input className="input-base" value={form.allowed_tools} onChange={(e) => set("allowed_tools", e.target.value)} />
        </LabeledInput>
      </div>

      <LabeledInput label="Metadata JSON">
        <textarea
          className="textarea-base font-mono text-xs"
          data-testid="skill-metadata"
          rows={8}
          value={form.skill_metadata}
          onChange={(e) => set("skill_metadata", e.target.value)}
        />
      </LabeledInput>

      <div className="flex gap-2">
        <button
          type="button"
          data-testid="skill-save"
          disabled={saving || !form.owner.trim() || !form.content.trim()}
          onClick={onSave}
          className="btn-primary disabled:opacity-50"
        >
          {saving ? "Saving…" : "Save"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="btn-ghost px-4 py-2"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Small shared bits                                                           */
/* -------------------------------------------------------------------------- */

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}): JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`inline-flex items-center gap-1.5 border-b-2 px-3.5 py-2.5 text-sm font-medium transition-colors ${
        active
          ? "border-caliber-purple text-caliber-purple"
          : "border-transparent text-slate-500 hover:text-slate-700"
      }`}
    >
      {children}
    </button>
  );
}

function StatusDotPill({ status }: { status: ResourceStatus }): JSX.Element {
  const active = status === "active";
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[11px] font-semibold ring-1 ${
        active
          ? "bg-emerald-50 text-emerald-700 ring-emerald-200/60"
          : "bg-slate-100 text-slate-500 ring-slate-200/60"
      }`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${active ? "bg-emerald-500" : "bg-slate-400"}`} />
      {status}
    </span>
  );
}

function LegendDot({ color, label }: { color: string; label: string }): JSX.Element {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={`h-2 w-2 rounded-full ${color}`} />
      {label}
    </span>
  );
}

function LabeledInput({ label, children }: { label: string; children: React.ReactNode }): JSX.Element {
  return (
    <label className="block">
      <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-slate-400">{label}</span>
      {children}
    </label>
  );
}

function Empty(): JSX.Element {
  return <span className="text-sm italic text-slate-400">—</span>;
}

function Chevron(): JSX.Element {
  return (
    <svg className="h-3.5 w-3.5 text-slate-300" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M9 18l6-6-6-6" />
    </svg>
  );
}
