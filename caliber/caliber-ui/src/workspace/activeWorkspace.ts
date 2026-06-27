/**
 * Active workspace (project) selection — the SPA side of project scoping.
 *
 * The backend reads ``X-CALIBER-Project`` into ``identity.active_project_id`` and
 * scopes prompts/skills/tools/agents/datasets to it (golden-path roadmap, Wave 3
 * — completes "project unify": selecting a project both opens its files and
 * scopes everything). This module persists the choice and the API layer injects
 * the header on every request. Mirrors the ``localAuth`` user-header pattern.
 */

const ACTIVE_PROJECT_KEY = "caliber.active_project_id";
export const WORKSPACE_CHANGED_EVENT = "caliber-workspace-changed";

export function getActiveProjectId(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(ACTIVE_PROJECT_KEY) || null;
}

export function setActiveProjectId(projectId: string | null): void {
  if (typeof window === "undefined") return;
  if (projectId) {
    window.localStorage.setItem(ACTIVE_PROJECT_KEY, projectId);
  } else {
    window.localStorage.removeItem(ACTIVE_PROJECT_KEY);
  }
  window.dispatchEvent(new Event(WORKSPACE_CHANGED_EVENT));
}
