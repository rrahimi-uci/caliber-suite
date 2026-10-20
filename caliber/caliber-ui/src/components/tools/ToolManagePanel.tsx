/**
 * What a tool's owner manages after it is registered: edit its metadata, copy the binding an
 * agent uses, follow its calibration jobs, and move between its versions (including making a
 * new one). It used to be a page of its own (``/tools/:id``) that overlapped the workspace;
 * it lives in the workspace's Publish stage now, so a tool has one page.
 */

import { useEffect, useMemo, useState } from "react";

import { caliberApi } from "@/api/caliberApi";
import type { ToolDefinition, ToolUpdatePayload } from "@/api/workflowTypes";
import { ToolCalibrationJobs } from "@/components/ToolCalibrationJobs";
import { makeToolVersionAdapter } from "@/components/versioning/adapters";
import { VersionPanel } from "@/components/versioning/VersionPanel";
import { useApiMutation, useApiQuery, useInvalidate } from "@/hooks/useApiQuery";
import { SCOPE_OPERATOR, hasScope } from "@/lib/scopes";
import { toolBindingForDefinition } from "@/lib/workflowGraph";

interface ToolEditState {
  description: string;
  side_effect_level: ToolDefinition["side_effect_level"];
  requires_approval: boolean;
  allow_in_preview: boolean;
  owner: string;
  status: ToolDefinition["status"];
  successor_tool_id: string;
}

function editStateFromTool(tool: ToolDefinition): ToolEditState {
  return {
    description: tool.description ?? "",
    side_effect_level: tool.side_effect_level,
    requires_approval: tool.requires_approval,
    allow_in_preview: tool.allow_in_preview,
    owner: tool.owner ?? "",
    status: tool.status,
    successor_tool_id: tool.successor_tool_id ?? "",
  };
}

export function ToolManagePanel({
  tool,
  onNewVersion,
}: {
  tool: ToolDefinition;
  /** Start registering the next version of this tool (the Tools page opens the wizard on it). */
  onNewVersion: (tool: ToolDefinition) => void;
}): JSX.Element {
  const toolId = tool.tool_id;
  const invalidate = useInvalidate();
  const [edit, setEdit] = useState<ToolEditState>(() => editStateFromTool(tool));

  // Editing a tool is admin-only on the backend (SCOPE_ADMIN); only admins see the controls.
  const meQuery = useApiQuery(["me"], (s) => caliberApi.getMe(s));
  const isAdmin = meQuery.data?.is_admin ?? false;
  // ``GET /me`` returns the effective scope set, so an admin already carries ``caliber.operator``.
  const canOperate = hasScope(meQuery.data?.scopes, SCOPE_OPERATOR);

  const saveMut = useApiMutation(
    (payload: ToolUpdatePayload) => caliberApi.updateTool(toolId, payload),
    {
      onSuccess: async (updated) => {
        setEdit(editStateFromTool(updated));
        await invalidate(["tool", toolId]);
        await invalidate(["tools"]);
      },
    },
  );

  useEffect(() => {
    setEdit(editStateFromTool(tool));
  }, [tool]);

  // Memoized so the VersionPanel's load effect doesn't re-fire each render.
  const toolVersionAdapter = useMemo(
    () => makeToolVersionAdapter(toolId, tool.name),
    [toolId, tool.name],
  );
  const agentBinding = toolBindingForDefinition(tool);

  function updateEdit<K extends keyof ToolEditState>(key: K, value: ToolEditState[K]): void {
    setEdit((current) => ({ ...current, [key]: value }));
  }

  function saveTool(): void {
    const payload: ToolUpdatePayload = {
      description: edit.description,
      side_effect_level: edit.side_effect_level,
      requires_approval: edit.requires_approval,
      allow_in_preview: edit.allow_in_preview,
      status: edit.status,
      successor_tool_id: edit.successor_tool_id.trim() || null,
    };
    if (edit.owner.trim()) payload.owner = edit.owner.trim();
    saveMut.mutate(payload);
  }

  return (
    <div data-testid="tool-manage" className="space-y-4">
      {isAdmin && (
        <section className="border-t border-gray-200 pt-3">
          <h2 className="text-sm font-semibold text-gray-900">Edit</h2>
          <div className="mt-2 space-y-2 text-xs">
            <label className="block">
              <span className="font-semibold text-gray-500">Description</span>
              <textarea
                data-testid="tool-edit-description"
                value={edit.description}
                onChange={(event) =>
                  updateEdit("description", event.target.value)
                }
                className="mt-1 h-20 w-full rounded border border-gray-300 px-2 py-1 font-mono"
              />
            </label>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              <label className="block">
                <span className="font-semibold text-gray-500">
                  Side effect
                </span>
                <select
                  data-testid="tool-edit-side-effect"
                  value={edit.side_effect_level}
                  onChange={(event) =>
                    updateEdit(
                      "side_effect_level",
                      event.target
                        .value as ToolDefinition["side_effect_level"],
                    )
                  }
                  className="mt-1 w-full rounded border border-gray-300 px-2 py-1"
                >
                  <option value="read">read</option>
                  <option value="write">write</option>
                  <option value="external_action">external_action</option>
                </select>
              </label>
              <label className="block">
                <span className="font-semibold text-gray-500">Status</span>
                <select
                  data-testid="tool-edit-status"
                  value={edit.status}
                  onChange={(event) =>
                    updateEdit(
                      "status",
                      event.target.value as ToolDefinition["status"],
                    )
                  }
                  className="mt-1 w-full rounded border border-gray-300 px-2 py-1"
                >
                  <option value="active">active</option>
                  <option value="deprecated">deprecated</option>
                  <option value="archived">archived</option>
                </select>
              </label>
            </div>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              <label className="block">
                <span className="font-semibold text-gray-500">Owner</span>
                <input
                  data-testid="tool-edit-owner"
                  value={edit.owner}
                  onChange={(event) =>
                    updateEdit("owner", event.target.value)
                  }
                  className="mt-1 w-full rounded border border-gray-300 px-2 py-1"
                />
              </label>
              <label className="block">
                <span className="font-semibold text-gray-500">
                  Successor ID
                </span>
                <input
                  data-testid="tool-edit-successor"
                  value={edit.successor_tool_id}
                  onChange={(event) =>
                    updateEdit("successor_tool_id", event.target.value)
                  }
                  className="mt-1 w-full rounded border border-gray-300 px-2 py-1 font-mono"
                />
              </label>
            </div>
            <label className="flex items-center gap-2">
              <input
                data-testid="tool-edit-requires-approval"
                type="checkbox"
                checked={edit.requires_approval}
                onChange={(event) =>
                  updateEdit("requires_approval", event.target.checked)
                }
              />
              <span>Requires approval</span>
            </label>
            <label className="flex items-center gap-2">
              <input
                data-testid="tool-edit-allow-preview"
                type="checkbox"
                checked={edit.allow_in_preview}
                onChange={(event) =>
                  updateEdit("allow_in_preview", event.target.checked)
                }
              />
              <span>Allow live preview for read tools</span>
            </label>
            <div className="flex items-center gap-2">
              <button
                type="button"
                data-testid="tool-save"
                disabled={saveMut.isPending}
                onClick={saveTool}
                className="rounded bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-50"
              >
                Save
              </button>
              {saveMut.isSuccess && (
                <span
                  data-testid="tool-save-status"
                  className="text-gray-500"
                >
                  Saved
                </span>
              )}
              {saveMut.error && (
                <span data-testid="tool-save-error" className="text-red-600">
                  {saveMut.error.message}
                </span>
              )}
            </div>
          </div>
        </section>
      )}
      <section className="rounded-lg border border-zinc-200 bg-white p-4">
        <div className="text-xs font-semibold text-gray-500">Agent binding</div>
        <pre
          data-testid="tool-agent-binding"
          className="mt-1 max-h-48 overflow-auto rounded bg-gray-50 p-2 text-xs"
        >
          {JSON.stringify(agentBinding, null, 2)}
        </pre>
      </section>

      <section
        className="rounded-lg border border-zinc-200 bg-white p-4"
        data-testid="tool-versions"
      >
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <div className="text-xs font-semibold text-gray-500">Version history</div>
          {isAdmin && (
            <button
              type="button"
              data-testid="tool-new-version"
              onClick={() => onNewVersion(tool)}
              className="rounded border border-gray-300 px-2 py-1 text-xs font-medium text-gray-800 hover:bg-gray-50"
            >
              New version
            </button>
          )}
        </div>
        <VersionPanel adapter={toolVersionAdapter} />
      </section>

      <ToolCalibrationJobs toolId={toolId} canOperate={canOperate} />
    </div>
  );
}
