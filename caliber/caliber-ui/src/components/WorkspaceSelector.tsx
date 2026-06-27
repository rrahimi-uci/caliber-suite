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

  function onChange(event: ChangeEvent<HTMLSelectElement>): void {
    const value = event.target.value;
    setActive(value);
    setActiveProjectId(value || null);
    // Scoped lists are cached per query key; invalidate so they refetch for the
    // newly-selected workspace (otherwise stale, prior-workspace data lingers).
    void queryClient.invalidateQueries();
  }

  return (
    <label
      className="hidden sm:flex items-center gap-1.5 text-xs text-slate-500"
      title="Active workspace — scopes prompts, skills, tools, agents, and datasets"
    >
      <svg className="w-3.5 h-3.5 text-slate-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
        <path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" />
      </svg>
      <select
        aria-label="Active workspace"
        value={active}
        onChange={onChange}
        className="bg-transparent text-slate-600 font-medium rounded-md px-1 py-0.5 outline-none hover:text-caliber-purple focus-visible:ring-2 focus-visible:ring-caliber-purple/40 cursor-pointer max-w-[10rem] truncate"
      >
        <option value="">All workspaces</option>
        {projects.map((project) => (
          <option key={project.project_id} value={project.project_id}>
            {project.name}
          </option>
        ))}
      </select>
    </label>
  );
}
