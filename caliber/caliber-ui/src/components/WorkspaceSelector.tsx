/**
 * Active-workspace selector (golden-path roadmap, Wave 3 — completes "project
 * unify"). Selecting a project sends ``X-CALIBER-Project`` on every request
 * (via the API layer), scoping prompts/skills/tools/agents/datasets to it.
 * Changing the selection invalidates cached queries so scoped lists refetch.
 */

import type { ChangeEvent } from "react";
import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { caliberApi } from "@/api/caliberApi";
import type { Project } from "@/api/workflowTypes";
import { getActiveProjectId, setActiveProjectId } from "@/workspace/activeWorkspace";

export function WorkspaceSelector(): JSX.Element {
  const queryClient = useQueryClient();
  const [projects, setProjects] = useState<Project[]>([]);
  const [active, setActive] = useState<string>(getActiveProjectId() ?? "");
  const [showCreate, setShowCreate] = useState(false);
  const [projectName, setProjectName] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState("");

  useEffect(() => {
    let cancelled = false;
    caliberApi
      .listProjects()
      .then((rows) => {
        if (!cancelled) setProjects(rows);
      })
      .catch(() => {
        // A failed project list leaves the selector at "All workspaces";
        // scoping silently no-ops, which is the safe default.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function selectProject(value: string): void {
    setActive(value);
    setActiveProjectId(value || null);
    // Scoped lists are cached per query key; invalidate so they refetch for the
    // newly-selected workspace (otherwise stale, prior-workspace data lingers).
    void queryClient.invalidateQueries();
  }

  function onChange(event: ChangeEvent<HTMLSelectElement>): void {
    selectProject(event.target.value);
  }

  async function createWorkspace(): Promise<void> {
    const name = projectName.trim();
    if (!name || creating) return;
    setCreating(true);
    setCreateError("");
    try {
      const project = await caliberApi.createProject({ name });
      setProjects((current) => [...current, project].sort((a, b) => a.name.localeCompare(b.name)));
      selectProject(project.project_id);
      setProjectName("");
      setShowCreate(false);
    } catch (error) {
      setCreateError(error instanceof Error ? error.message : String(error));
    } finally {
      setCreating(false);
    }
  }

  return (
    <div
      className="hidden sm:flex items-center gap-1 text-xs text-slate-500"
      title="Active workspace — scopes prompts, skills, tools, agents, and datasets"
    >
      <svg className="w-3.5 h-3.5 text-slate-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
        <path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" />
      </svg>
      <label className="sr-only" htmlFor="caliber-active-workspace">Active workspace</label>
      <select
          id="caliber-active-workspace"
          aria-label="Active workspace"
          value={active}
          onChange={onChange}
          className="max-w-[10rem] cursor-pointer truncate rounded-md bg-transparent px-1 py-0.5 font-medium text-slate-600 outline-none hover:text-caliber-purple focus-visible:ring-2 focus-visible:ring-caliber-purple/40 dark:text-slate-300"
        >
          <option value="">All workspaces</option>
          {projects.map((project) => (
            <option key={project.project_id} value={project.project_id}>
              {project.name}
            </option>
          ))}
        </select>
      <button
        type="button"
        aria-label="Create workspace"
        title="Create workspace"
        className="grid h-6 w-6 place-items-center rounded-md text-sm font-bold text-slate-400 hover:bg-slate-100 hover:text-caliber-purple dark:hover:bg-white/10"
        onClick={() => {
          setCreateError("");
          setShowCreate(true);
        }}
      >
        +
      </button>

      {showCreate && (
        <div
          className="fixed inset-0 z-[80] grid place-items-center bg-black/40 p-4 backdrop-blur-sm"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !creating) setShowCreate(false);
          }}
        >
          <form
            role="dialog"
            aria-modal="true"
            aria-labelledby="create-workspace-title"
            className="w-full max-w-md rounded-2xl border border-slate-200 bg-white p-6 text-left shadow-xl dark:border-white/10 dark:bg-slate-900"
            onSubmit={(event) => {
              event.preventDefault();
              void createWorkspace();
            }}
          >
            <h2 id="create-workspace-title" className="text-base font-bold text-slate-900 dark:text-white">
              Create workspace
            </h2>
            <p className="mt-1 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
              Workspaces scope managed files and the workflows that pin them.
            </p>
            <label className="mt-4 block text-xs font-semibold text-slate-700 dark:text-slate-200">
              Workspace name
              <input
                autoFocus
                className="form-input mt-1.5 w-full"
                value={projectName}
                onChange={(event) => setProjectName(event.target.value)}
                placeholder="Document automation"
                required
              />
            </label>
            {createError && <p role="alert" className="mt-3 text-xs text-red-600">{createError}</p>}
            <div className="mt-5 flex justify-end gap-2">
              <button type="button" className="btn-secondary" disabled={creating} onClick={() => setShowCreate(false)}>
                Cancel
              </button>
              <button type="submit" className="btn-primary" disabled={creating || !projectName.trim()}>
                {creating ? "Creating…" : "Create and select"}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
