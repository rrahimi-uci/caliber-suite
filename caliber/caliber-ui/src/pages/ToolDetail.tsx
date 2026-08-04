/**
 * Tool Detail (§16.1) — schema, side-effect level, versions of the family,
 * referencing workflows (usage), and deprecate/archive actions (admin).
 */

import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { caliberApi } from "@/api/caliberApi";
import { makeToolVersionAdapter } from "@/components/versioning/adapters";
import { VersionPanel } from "@/components/versioning/VersionPanel";
import type {
  CalibrationCase,
  CalibrationResult,
  ToolDefinition,
  ToolUpdatePayload,
} from "@/api/workflowTypes";
import { CalibrationPanel } from "@/components/CalibrationPanel";
import { CopyButton } from "@/components/CopyButton";
import { ToolCalibrationJobs } from "@/components/ToolCalibrationJobs";
import { CodeBlock } from "@/components/workflows/CodeHighlight";
import {
  useApiMutation,
  useApiQuery,
  useInvalidate,
} from "@/hooks/useApiQuery";
import { toolBindingForDefinition } from "@/lib/workflowGraph";

const SIDE_EFFECT_BADGE: Record<string, string> = {
  read: "🟢 read",
  write: "🟡 write",
  external_action: "🔴 external",
};

interface ToolEditState {
  description: string;
  side_effect_level: ToolDefinition["side_effect_level"];
  requires_approval: boolean;
  allow_in_preview: boolean;
  owner: string;
  status: ToolDefinition["status"];
  successor_tool_id: string;
}

const EMPTY_EDIT: ToolEditState = {
  description: "",
  side_effect_level: "read",
  requires_approval: false,
  allow_in_preview: false,
  owner: "",
  status: "active",
  successor_tool_id: "",
};

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

function parseRunInput(raw: string): Record<string, unknown> {
  const parsed = JSON.parse(raw) as unknown;
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("Tool input must be a JSON object.");
  }
  return parsed as Record<string, unknown>;
}

function pretty(value: unknown): string {
  return JSON.stringify(value ?? {}, null, 2);
}

export function ToolDetail(): JSX.Element {
  const { toolId } = useParams<{ toolId: string }>();
  const invalidate = useInvalidate();
  const [edit, setEdit] = useState<ToolEditState>(EMPTY_EDIT);
  const [runInput, setRunInput] = useState("{}");
  const [runInputError, setRunInputError] = useState<string | null>(null);

  const toolQuery = useApiQuery(
    ["tool", toolId],
    (s) => caliberApi.getTool(toolId!, s),
    {
      enabled: Boolean(toolId),
    },
  );
  const usageQuery = useApiQuery(
    ["tool-usage", toolId],
    (s) => caliberApi.getToolUsage(toolId!, s),
    {
      enabled: Boolean(toolId),
    },
  );
  const sourceQuery = useApiQuery(
    ["tool-source", toolId],
    (s) => caliberApi.getToolSource(toolId!, s),
    {
      enabled: Boolean(toolId),
    },
  );
  // Editing, deprecating, and archiving a tool are admin-only on the backend
  // (SCOPE_ADMIN); gate the controls so only admins see them.
  const meQuery = useApiQuery(["me"], (s) => caliberApi.getMe(s));
  const isAdmin = meQuery.data?.is_admin ?? false;
  const canOperate =
    isAdmin || (meQuery.data?.scopes ?? []).includes("caliber.operator");

  const deprecateMut = useApiMutation(
    () => caliberApi.updateTool(toolId!, { status: "deprecated" }),
    {
      onSuccess: () => invalidate(["tool", toolId]),
    },
  );
  const archiveMut = useApiMutation(() => caliberApi.archiveTool(toolId!), {
    onSuccess: () => invalidate(["tool", toolId]),
  });
  const saveMut = useApiMutation(
    (payload: ToolUpdatePayload) => caliberApi.updateTool(toolId!, payload),
    {
      onSuccess: async (updated) => {
        setEdit(editStateFromTool(updated));
        await invalidate(["tool", toolId]);
        await invalidate(["tools"]);
      },
    },
  );
  const testRunMut = useApiMutation((input: Record<string, unknown>) =>
    caliberApi.testRunTool(toolId!, input),
  );

  const tool = toolQuery.data;

  // Memoized so the VersionPanel's load effect doesn't re-fire each render.
  const toolVersionAdapter = useMemo(
    () => makeToolVersionAdapter(toolId ?? "", tool?.name ?? ""),
    [toolId, tool?.name],
  );

  useEffect(() => {
    if (tool) setEdit(editStateFromTool(tool));
  }, [tool]);

  if (toolQuery.isLoading) {
    return <div className="text-sm text-gray-400">Loading tool…</div>;
  }
  if (!tool) {
    return (
      <div
        role="alert"
        data-testid="tool-detail-error"
        className="rounded border border-red-300 bg-red-50 p-4 text-sm text-red-800"
      >
        <p>
          Could not load this tool:{" "}
          {toolQuery.error?.message ?? "Tool not found."}
        </p>
        <Link to="/tools" className="mt-2 inline-block underline">
          Back to tools
        </Link>
      </div>
    );
  }
  const agentBinding = toolBindingForDefinition(tool);

  function updateEdit<K extends keyof ToolEditState>(
    key: K,
    value: ToolEditState[K],
  ): void {
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

  function runTool(): void {
    setRunInputError(null);
    try {
      testRunMut.mutate(parseRunInput(runInput));
    } catch (err) {
      setRunInputError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div data-testid="tool-detail">
      {toolQuery.error && (
        <div
          role="status"
          data-testid="tool-detail-refresh-warning"
          className="mb-3 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900"
        >
          Showing the last loaded tool because refresh failed:{" "}
          {toolQuery.error.message}
        </div>
      )}
      <Link to="/tools" className="text-xs text-gray-400 hover:underline">
        ← Tools
      </Link>
      <h1 className="text-xl font-semibold text-gray-900">
        {tool.name}{" "}
        <span className="text-xs text-gray-400">
          v{tool.version} ·{" "}
          {SIDE_EFFECT_BADGE[tool.side_effect_level] ?? tool.side_effect_level}{" "}
          · {tool.status}
        </span>
      </h1>
      <p className="text-sm text-gray-500">
        {tool.description || "No description."}
      </p>

      {isAdmin && (
        <div className="my-3 flex flex-wrap gap-2">
          <button
            type="button"
            data-testid="tool-deprecate"
            disabled={tool.status !== "active" || deprecateMut.isPending}
            onClick={() => deprecateMut.mutate(undefined)}
            className="rounded border border-gray-300 px-2 py-1 text-xs disabled:opacity-50"
          >
            Deprecate
          </button>
          <button
            type="button"
            data-testid="tool-archive"
            disabled={archiveMut.isPending}
            onClick={() => archiveMut.mutate(undefined)}
            className="rounded border border-gray-300 px-2 py-1 text-xs text-red-600 disabled:opacity-50"
          >
            Archive
          </button>
          {archiveMut.error && (
            <span
              data-testid="tool-archive-error"
              className="text-xs text-red-600"
            >
              {archiveMut.error.message}
            </span>
          )}
        </div>
      )}

      <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(320px,420px)]">
        <section className="border-t border-gray-200 pt-3">
          <h2 className="text-sm font-semibold text-gray-900">Details</h2>
          <dl className="mt-2 grid grid-cols-1 gap-2 text-xs sm:grid-cols-2">
            <div>
              <dt className="font-semibold text-gray-500">Tool ID</dt>
              <dd
                data-testid="tool-id"
                className="flex items-center gap-1.5 font-mono text-gray-800"
              >
                <span className="truncate">{tool.tool_id}</span>
                <CopyButton
                  value={tool.tool_id}
                  label="Copy tool ID"
                  testId="copy-tool-id"
                />
              </dd>
            </div>
            <div>
              <dt className="font-semibold text-gray-500">Owner</dt>
              <dd className="text-gray-800">{tool.owner || "Unassigned"}</dd>
            </div>
            <div>
              <dt className="font-semibold text-gray-500">Module</dt>
              <dd data-testid="tool-module" className="font-mono text-gray-800">
                {tool.module_path}
              </dd>
            </div>
            <div>
              <dt className="font-semibold text-gray-500">Callable</dt>
              <dd
                data-testid="tool-callable"
                className="font-mono text-gray-800"
              >
                {tool.callable_name}
              </dd>
            </div>
            <div>
              <dt className="font-semibold text-gray-500">Created</dt>
              <dd className="text-gray-800">{tool.created_at}</dd>
            </div>
            <div>
              <dt className="font-semibold text-gray-500">Updated</dt>
              <dd className="text-gray-800">{tool.updated_at}</dd>
            </div>
          </dl>
        </section>

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
      </div>

      <section
        className="mt-4 border-t border-gray-200 pt-3"
        data-testid="tool-implementation"
      >
        <h2 className="text-sm font-semibold text-gray-900">Implementation</h2>

        {sourceQuery.data?.signature && (
          <div className="mt-2">
            <div className="text-xs font-semibold text-gray-500">
              Signature — what to pass when you run it
            </div>
            <CodeBlock
              code={`def ${sourceQuery.data.signature}`}
              language="python"
              testId="tool-signature"
              className="mt-1 overflow-x-auto rounded border border-gray-200 bg-gray-50 p-2"
            />
          </div>
        )}

        {(tool.input_schema || tool.output_schema) && (
          <div className="mt-3 grid gap-3 lg:grid-cols-2">
            <div>
              <div className="text-xs font-semibold text-gray-500">
                Input schema
              </div>
              <CodeBlock
                code={pretty(tool.input_schema ?? {})}
                language="json"
                className="mt-1 max-h-60 overflow-auto rounded border border-gray-200 bg-gray-50 p-2"
              />
            </div>
            <div>
              <div className="text-xs font-semibold text-gray-500">
                Output schema
              </div>
              <CodeBlock
                code={pretty(tool.output_schema ?? {})}
                language="json"
                className="mt-1 max-h-60 overflow-auto rounded border border-gray-200 bg-gray-50 p-2"
              />
            </div>
          </div>
        )}

        {sourceQuery.data?.doc && (
          <p className="mt-3 whitespace-pre-wrap text-xs text-gray-600">
            {sourceQuery.data.doc}
          </p>
        )}

        <div className="mt-3">
          <div className="text-xs font-semibold text-gray-500">Source</div>
          {sourceQuery.isLoading && (
            <div className="mt-1 text-xs text-gray-400">Loading source…</div>
          )}
          {sourceQuery.data?.available ? (
            <CodeBlock
              code={sourceQuery.data.source}
              language="python"
              testId="tool-source"
              className="mt-1 max-h-[480px] overflow-auto rounded border border-gray-200 bg-gray-50 p-3"
            />
          ) : (
            sourceQuery.data && (
              <div
                className="mt-1 text-xs text-gray-400"
                data-testid="tool-source-unavailable"
              >
                Source unavailable
                {sourceQuery.data.error ? `: ${sourceQuery.data.error}` : "."}
              </div>
            )
          )}
        </div>
      </section>

      <section className="mt-4 border-t border-gray-200 pt-3">
        <h2 className="text-sm font-semibold text-gray-900">Run</h2>
        <div className="mt-2 grid gap-3 lg:grid-cols-2">
          <div>
            <label
              className="text-xs font-semibold text-gray-500"
              htmlFor="tool-run-input"
            >
              Input JSON
            </label>
            <textarea
              id="tool-run-input"
              data-testid="tool-run-input"
              value={runInput}
              onChange={(event) => setRunInput(event.target.value)}
              className="mt-1 h-40 w-full rounded border border-gray-300 px-2 py-1 font-mono text-xs"
            />
            <div className="mt-2 flex items-center gap-2">
              <button
                type="button"
                data-testid="tool-run"
                disabled={testRunMut.isPending}
                onClick={runTool}
                className="rounded bg-gray-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-black disabled:opacity-50"
              >
                Run Tool
              </button>
              {runInputError && (
                <span
                  data-testid="tool-run-input-error"
                  className="text-xs text-red-600"
                >
                  {runInputError}
                </span>
              )}
            </div>
          </div>
          <div>
            <div className="text-xs font-semibold text-gray-500">Result</div>
            {testRunMut.error && (
              <div
                data-testid="tool-run-error"
                className="mt-1 text-xs text-red-600"
              >
                {testRunMut.error.message}
              </div>
            )}
            {testRunMut.data ? (
              <pre
                data-testid="tool-run-result"
                className="mt-1 max-h-64 overflow-auto rounded bg-gray-50 p-2 text-xs"
              >
                {pretty(testRunMut.data)}
              </pre>
            ) : (
              <div
                data-testid="tool-run-empty"
                className="mt-1 rounded bg-gray-50 p-2 text-xs text-gray-400"
              >
                No run yet.
              </div>
            )}
          </div>
        </div>
      </section>

      <section className="mt-4 border-t border-gray-200 pt-3">
        <CalibrationPanel
          key={tool.tool_id}
          idPrefix="tool"
          calibrateTestId="tool-calibrate-btn"
          initialCases={tool.test_cases ?? []}
          lastResult={tool.last_calibration ?? null}
          onSave={async (cases: CalibrationCase[]) => {
            const saved = await caliberApi.saveToolTestCases(
              tool.tool_id,
              cases,
            );
            await invalidate(["tool", toolId]);
            return saved;
          }}
          onCalibrate={async (): Promise<CalibrationResult> => {
            const scored = await caliberApi.calibrateTool(tool.tool_id);
            await invalidate(["tool", toolId]);
            return scored;
          }}
        />
        <ToolCalibrationJobs toolId={tool.tool_id} canOperate={canOperate} />
      </section>

      <div className="grid grid-cols-2 gap-3 text-xs">
        <div>
          <div className="font-semibold text-gray-500">Input schema</div>
          <pre className="mt-1 max-h-48 overflow-auto rounded bg-gray-50 p-2">
            {pretty(tool.input_schema)}
          </pre>
        </div>
        <div>
          <div className="font-semibold text-gray-500">Output schema</div>
          <pre className="mt-1 max-h-48 overflow-auto rounded bg-gray-50 p-2">
            {pretty(tool.output_schema)}
          </pre>
        </div>
      </div>

      <div className="mt-3 text-xs">
        <div className="font-semibold text-gray-500">Agent binding</div>
        <pre
          data-testid="tool-agent-binding"
          className="mt-1 max-h-48 overflow-auto rounded bg-gray-50 p-2"
        >
          {JSON.stringify(agentBinding, null, 2)}
        </pre>
      </div>

      <section
        className="mt-4 border-t border-gray-200 pt-3"
        data-testid="tool-versions"
      >
        <div className="mb-2 text-xs font-semibold text-gray-500">
          Version history
        </div>
        <VersionPanel adapter={toolVersionAdapter} />
      </section>

      <div className="mt-3">
        <div className="text-xs font-semibold text-gray-500">Used by</div>
        {usageQuery.data && usageQuery.data.usage.length === 0 && (
          <div data-testid="tool-usage-empty" className="text-xs text-gray-400">
            Not referenced by any workflow.
          </div>
        )}
        <ul data-testid="tool-usage" className="text-xs">
          {(usageQuery.data?.usage ?? []).map((u) => (
            <li key={u.version_id}>
              <Link
                to={`/workflows/${u.workflow_id}`}
                className="text-blue-600 hover:underline"
              >
                {u.workflow_id}
              </Link>{" "}
              v{u.version_number} ({u.status})
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
