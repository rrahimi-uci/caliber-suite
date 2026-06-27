/**
 * MCP Servers — registry of Model Context Protocol server connections.
 *
 * Shows all configured MCP servers, their connection status, transport
 * type, and discovered tools. Supports adding new servers, testing
 * connections, removing servers, and interactively invoking tools via
 * the Playground tab.
 */

import { useCallback, useEffect, useState } from "react";

import { caliberApi } from "@/api/caliberApi";
import type { AssistantConfig, AssistantModelOption } from "@/api/assistantTypes";
import type {
  CalibrationCase,
  CalibrationResult,
  McpDiscoveredToolWithPolicy,
  McpServer,
  McpServerCreatePayload,
  McpServerDiscoveredTool,
  McpTestConnectionResult,
  McpToolPolicy,
  McpToolInvocationResult,
} from "@/api/workflowTypes";
import { CalibrationPanel } from "@/components/CalibrationPanel";
import { ClearFiltersButton } from "@/components/ClearFiltersButton";
import { FilterBar } from "@/components/FilterBar";
import { PageHeader } from "@/components/PageHeader";
import { PageTabs, type PageTab } from "@/components/PageTabs";
import { SearchInput } from "@/components/SearchInput";
import { FilterSelect } from "@/components/FilterSelect";
import { ToolSignature } from "@/components/tools/ToolSignature";
import { useApi } from "@/hooks/useApi";

const MCP_TABS: PageTab[] = [
  {
    key: "servers",
    label: "Servers",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
        <rect x="2" y="3" width="20" height="6" rx="2" />
        <rect x="2" y="15" width="20" height="6" rx="2" />
        <path d="M6 6h.01M6 18h.01M12 9v6" />
      </svg>
    ),
  },
  {
    key: "playground",
    label: "Playground",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
        <path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z" />
      </svg>
    ),
  },
];

/* ── status badge --------------------------------------------------------- */

const STATUS_STYLES: Record<string, string> = {
  active:
    "bg-emerald-50 text-emerald-700 border-emerald-200",
  error:
    "bg-red-50 text-red-700 border-red-200",
  disabled:
    "bg-zinc-100 text-zinc-500 border-zinc-200",
};

const STATUS_DOT: Record<string, string> = {
  active: "bg-emerald-500",
  error: "bg-red-500",
  disabled: "bg-zinc-400",
};

/* ── transport badge ------------------------------------------------------ */

const TRANSPORT_LABEL: Record<string, string> = {
  stdio: "stdio",
  sse: "SSE",
  "streamable-http": "HTTP",
};

const MCP_ERROR_PREFIXES = [
  /^MCP tools\/(?:call|list) failed:\s*/i,
  /^MCP session failed:\s*/i,
];

function PlaywrightIcon({
  className,
}: {
  className: string;
}): JSX.Element {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <rect x="3" y="4" width="18" height="14" rx="3" />
      <path d="M8 20h8" />
      <path d="M7.5 8.5h9" />
      <path d="M8.5 12.25h2.25" />
      <path d="m13.5 12.25 1.5 1.5 3-3" />
    </svg>
  );
}

function formatMcpOperatorError(raw: string | null | undefined): string | null {
  if (!raw) return null;
  let message = raw.trim();
  if (!message) return null;

  let changed = true;
  while (changed) {
    changed = false;
    for (const prefix of MCP_ERROR_PREFIXES) {
      const next = message.replace(prefix, "").trim();
      if (next !== message) {
        message = next;
        changed = true;
      }
    }
  }

  if (message.includes("unhandled errors in a TaskGroup")) {
    return "Request failed — check the server is running";
  }

  if (/\[errno\s*2\]|no such file or directory|\benoent\b/i.test(message)) {
    return "Request failed — check the server is installed or available";
  }

  if (/\b(timeout|timed out)\b/i.test(message)) {
    return "Request timed out — the server did not respond";
  }

  const invalidInputMatch = message.match(/^Invalid input:\s*(\[.*\])$/s);
  if (invalidInputMatch) {
    const issuesPayload = invalidInputMatch[1];
    if (!issuesPayload) return message;
    try {
      const issues = JSON.parse(issuesPayload) as Array<{
        path?: unknown;
        message?: unknown;
      }>;
      const parts = issues
        .map((issue) => {
          const path = Array.isArray(issue.path)
            ? issue.path
                .map((item) => String(item).trim())
                .filter(Boolean)
                .join(".")
            : "";
          const detail =
            typeof issue.message === "string" ? issue.message.trim() : "";
          if (path && /^required$/i.test(detail)) return `${path} is required`;
          if (path && detail) return `${path}: ${detail}`;
          return detail || path;
        })
        .filter(Boolean);
      if (parts.length > 0) {
        return `Invalid input: ${parts.join("; ")}`;
      }
    } catch {
      return message;
    }
  }

  return message;
}

/* ── icon map ------------------------------------------------------------- */

const ICONS: Record<string, JSX.Element> = {
  github: (
    <svg className="w-5 h-5" viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z" />
    </svg>
  ),
  slack: (
    <svg className="w-5 h-5" viewBox="0 0 24 24" fill="currentColor">
      <path d="M5.042 15.165a2.528 2.528 0 01-2.52 2.523A2.528 2.528 0 010 15.165a2.527 2.527 0 012.522-2.52h2.52v2.52zm1.271 0a2.527 2.527 0 012.521-2.52 2.527 2.527 0 012.521 2.52v6.313A2.528 2.528 0 018.834 24a2.528 2.528 0 01-2.521-2.522v-6.313zM8.834 5.042a2.528 2.528 0 01-2.521-2.52A2.528 2.528 0 018.834 0a2.528 2.528 0 012.521 2.522v2.52H8.834zm0 1.271a2.528 2.528 0 012.521 2.521 2.528 2.528 0 01-2.521 2.521H2.522A2.528 2.528 0 010 8.834a2.528 2.528 0 012.522-2.521h6.312zM18.956 8.834a2.528 2.528 0 012.522-2.521A2.528 2.528 0 0124 8.834a2.528 2.528 0 01-2.522 2.521h-2.522V8.834zm-1.27 0a2.528 2.528 0 01-2.523 2.521 2.527 2.527 0 01-2.52-2.521V2.522A2.527 2.527 0 0115.163 0a2.528 2.528 0 012.523 2.522v6.312zM15.163 18.956a2.528 2.528 0 012.523 2.522A2.528 2.528 0 0115.163 24a2.527 2.527 0 01-2.52-2.522v-2.522h2.52zm0-1.27a2.527 2.527 0 01-2.52-2.523 2.526 2.526 0 012.52-2.52h6.315A2.528 2.528 0 0124 15.163a2.528 2.528 0 01-2.522 2.523h-6.315z" />
    </svg>
  ),
  database: (
    <svg className="w-5 h-5" viewBox="0 0 20 20" fill="currentColor">
      <path d="M10 3.5c-3.59 0-6.5 1.12-6.5 2.5v8c0 1.38 2.91 2.5 6.5 2.5s6.5-1.12 6.5-2.5V6c0-1.38-2.91-2.5-6.5-2.5zM10 5c2.76 0 5 .9 5 1.5S12.76 8 10 8 5 7.1 5 6.5 7.24 5 10 5zm5 9c0 .6-2.24 1.5-5 1.5S5 14.6 5 14v-2.26C6.47 12.53 8.18 13 10 13s3.53-.47 5-1.26V14zm0-4c0 .6-2.24 1.5-5 1.5S5 10.6 5 10V7.74C6.47 8.53 8.18 9 10 9s3.53-.47 5-1.26V10z" />
    </svg>
  ),
  folder: (
    <svg className="w-5 h-5" viewBox="0 0 20 20" fill="currentColor">
      <path d="M2 6a2 2 0 012-2h5l2 2h5a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V6z" />
    </svg>
  ),
  alert: (
    <svg className="w-5 h-5" viewBox="0 0 20 20" fill="currentColor">
      <path fillRule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clipRule="evenodd" />
    </svg>
  ),
  graph: (
    <svg className="w-5 h-5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
      <circle cx="4.5" cy="10" r="2.2" />
      <circle cx="10" cy="4.5" r="2.2" />
      <circle cx="15.5" cy="10" r="2.2" />
      <circle cx="10" cy="15.5" r="2.2" />
      <path d="M6.5 9l2-3" />
      <path d="M11.5 6.2l2 2.6" />
      <path d="M13.3 11.6l-2 2.3" />
      <path d="M8.8 13.9l-2-2.2" />
    </svg>
  ),
  ollama: (
    <svg className="w-5 h-5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
      <circle cx="7" cy="7.5" r="1.2" />
      <circle cx="13" cy="7.5" r="1.2" />
      <path d="M5.2 12.2c1.5 1.7 8.1 1.7 9.6 0" />
      <path d="M4.8 10.5v-3.2a5.2 5.2 0 0 1 10.4 0v3.2" />
      <path d="M10 3.4v-1.6" />
    </svg>
  ),
  playwright: <PlaywrightIcon className="w-5 h-5" />,
};

function ServerIcon({ icon }: { icon: string }): JSX.Element {
  if (ICONS[icon]) return <span className="text-zinc-600">{ICONS[icon]}</span>;
  return (
    <span className="w-5 h-5 rounded bg-zinc-200 flex items-center justify-center text-[10px] font-bold text-zinc-500">
      MCP
    </span>
  );
}

/* ── Add-server dialog ---------------------------------------------------- */

interface AddServerInitialValues {
  name?: string;
  description?: string;
  transport?: "stdio" | "sse" | "streamable-http";
  command?: string;
  args?: string;
  uri?: string;
  authType?: "none" | "token" | "oauth";
  tokenEnvVar?: string;
  icon?: string;
  /** Env vars passed to the server (values may use ${VAR} placeholders). */
  env?: Record<string, string>;
  /** Well-known tools (from a catalog template) to seed the server with. */
  tools?: McpServerDiscoveredTool[];
}

function AddServerDialog({
  open,
  onClose,
  onCreated,
  initialValues,
  editServer,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
  initialValues?: AddServerInitialValues;
  /** When set, the dialog runs in edit mode: it prefills from this server and
   * PATCHes it on submit instead of creating a new one. */
  editServer?: McpServer | null;
}): JSX.Element | null {
  const isEdit = !!editServer;
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [transport, setTransport] = useState<"stdio" | "sse" | "streamable-http">("stdio");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [uri, setUri] = useState("");
  const [authType, setAuthType] = useState<"none" | "token" | "oauth">("none");
  const [tokenEnvVar, setTokenEnvVar] = useState("");
  const [icon, setIcon] = useState("");
  const [envVars, setEnvVars] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open && editServer) {
      // Edit mode: prefill every field from the existing server.
      setName(editServer.name);
      setDescription(editServer.description ?? "");
      setTransport(editServer.transport);
      setCommand(editServer.command ?? "");
      setArgs((editServer.args ?? []).join(" "));
      setUri(editServer.uri ?? "");
      setAuthType(editServer.auth_type ?? "none");
      setTokenEnvVar(
        typeof editServer.auth_config?.token_env_var === "string"
          ? editServer.auth_config.token_env_var
          : "",
      );
      setIcon(editServer.icon ?? "");
      setEnvVars(editServer.env ?? {});
      setError(null);
    } else if (open && initialValues) {
      setName(initialValues.name ?? "");
      setDescription(initialValues.description ?? "");
      setTransport(initialValues.transport ?? "stdio");
      setCommand(initialValues.command ?? "");
      setArgs(initialValues.args ?? "");
      setUri(initialValues.uri ?? "");
      setAuthType(initialValues.authType ?? "none");
      setTokenEnvVar(initialValues.tokenEnvVar ?? "");
      setIcon(initialValues.icon ?? "");
      setEnvVars(initialValues.env ?? {});
      setError(null);
    } else if (open) {
      setName("");
      setDescription("");
      setTransport("stdio");
      setCommand("");
      setArgs("");
      setUri("");
      setAuthType("none");
      setTokenEnvVar("");
      setIcon("");
      setEnvVars({});
      setError(null);
    }
  }, [open, initialValues, editServer]);

  if (!open) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    const sharedConfig = {
      transport,
      command: transport === "stdio" ? command : "",
      args: transport === "stdio" && args.trim() ? args.split(/\s+/) : [],
      uri: transport !== "stdio" ? uri : "",
      auth_type: authType,
      auth_config:
        authType === "token" && tokenEnvVar ? { token_env_var: tokenEnvVar } : {},
      env: {
        ...envVars,
        ...(authType === "token" && tokenEnvVar
          ? { [tokenEnvVar]: `\${${tokenEnvVar}}` }
          : {}),
      },
      icon,
    } as const;
    try {
      if (editServer) {
        // Edit mode: PATCH the existing server. ``name`` is immutable on the
        // backend, so it is intentionally not sent.
        await caliberApi.updateMcpServer(editServer.server_id, {
          description,
          ...sharedConfig,
        });
      } else {
        const payload: McpServerCreatePayload = {
          name,
          description,
          ...sharedConfig,
          // Seed the server with the catalog template's known tools so they show
          // immediately; a live test-connection later overwrites with real ones.
          discovered_tools: initialValues?.tools ?? [],
        };
        await caliberApi.createMcpServer(payload);
      }
      onCreated();
      onClose();
    } catch (err: unknown) {
      setError(
        err instanceof Error
          ? err.message
          : isEdit
            ? "Failed to update server"
            : "Failed to register server",
      );
    } finally {
      setSubmitting(false);
    }
  };

  const inputClass =
    "w-full rounded-md border border-zinc-300 bg-white px-3 py-1.5 text-sm shadow-sm focus:border-caliber-400 focus:ring-1 focus:ring-caliber-400 outline-none";
  const selectClass = inputClass;
  const labelClass = "block text-xs font-medium text-zinc-700 mb-1";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
      data-testid={isEdit ? "edit-server-dialog" : "add-server-dialog"}
    >
      <div className="w-full max-w-lg rounded-xl border border-zinc-200 bg-white shadow-xl">
        <form onSubmit={handleSubmit}>
          <div className="px-6 py-4 border-b border-zinc-100">
            <h2 className="text-base font-semibold text-zinc-900">
              {isEdit ? "Edit MCP Server" : "Register MCP Server"}
            </h2>
            <p className="text-xs text-zinc-500 mt-0.5">
              {isEdit
                ? "Update this server's connection configuration."
                : "Connect an external tool server so workflow agents can use its tools."}
            </p>
          </div>

          <div className="px-6 py-4 space-y-3 max-h-[60vh] overflow-y-auto">
            {error && (
              <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                {error}
              </div>
            )}

            <div>
              <label className={labelClass}>Name *</label>
              <input
                className={`${inputClass} ${isEdit ? "bg-zinc-100 text-zinc-500" : ""}`}
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. GitHub"
                required
                disabled={isEdit}
                title={isEdit ? "A server's name cannot be changed after creation." : undefined}
                data-testid="server-name-input"
              />
              {isEdit && (
                <p className="text-[11px] text-zinc-400 mt-0.5">
                  Name can&rsquo;t be changed after creation.
                </p>
              )}
            </div>

            <div>
              <label className={labelClass}>Description</label>
              <input
                className={inputClass}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="What does this server provide?"
              />
            </div>

            <div>
              <label className={labelClass}>Transport</label>
              <select
                className={selectClass}
                value={transport}
                onChange={(e) => setTransport(e.target.value as typeof transport)}
                data-testid="server-transport-select"
                aria-label="Transport type"
              >
                <option value="stdio">stdio (local process)</option>
                <option value="sse">SSE (Server-Sent Events)</option>
                <option value="streamable-http">Streamable HTTP</option>
              </select>
            </div>

            {transport === "stdio" && (
              <>
                <div>
                  <label className={labelClass}>Command *</label>
                  <input
                    className={inputClass}
                    value={command}
                    onChange={(e) => setCommand(e.target.value)}
                    placeholder="e.g. npx"
                    required
                    data-testid="server-command-input"
                  />
                </div>
                <div>
                  <label className={labelClass}>Arguments</label>
                  <input
                    className={inputClass}
                    value={args}
                    onChange={(e) => setArgs(e.target.value)}
                    placeholder="e.g. -y @modelcontextprotocol/server-github"
                  />
                </div>
              </>
            )}

            {transport !== "stdio" && (
              <div>
                <label className={labelClass}>Endpoint URI *</label>
                <input
                  className={inputClass}
                  value={uri}
                  onChange={(e) => setUri(e.target.value)}
                  placeholder="e.g. http://localhost:8080/mcp/sse"
                  required
                  data-testid="server-uri-input"
                />
              </div>
            )}

            <div>
              <label className={labelClass}>Authentication</label>
              <select
                className={selectClass}
                value={authType}
                onChange={(e) => setAuthType(e.target.value as typeof authType)}
                aria-label="Authentication type"
              >
                <option value="none">None</option>
                <option value="token">API Token (env variable)</option>
                <option value="oauth">OAuth</option>
              </select>
            </div>

            {authType === "token" && (
              <div>
                <label className={labelClass}>Token env variable</label>
                <input
                  className={inputClass}
                  value={tokenEnvVar}
                  onChange={(e) => setTokenEnvVar(e.target.value)}
                  placeholder="e.g. GITHUB_TOKEN"
                />
              </div>
            )}

            <div>
              <label className={labelClass}>Icon</label>
              <select className={selectClass} value={icon} onChange={(e) => setIcon(e.target.value)} aria-label="Server icon">
                <option value="">Default</option>
                <option value="github">GitHub</option>
                <option value="slack">Slack</option>
                <option value="database">Database</option>
                <option value="graph">Graph</option>
                <option value="ollama">Ollama</option>
                <option value="playwright">Playwright</option>
                <option value="alert">Alert</option>
              </select>
            </div>
          </div>

          <div className="px-6 py-3 border-t border-zinc-100 flex justify-end gap-2">
            <button
              type="button"
              onClick={onClose}
              className="px-3 py-1.5 text-sm rounded-md border border-zinc-300 bg-white text-zinc-700 hover:bg-zinc-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting || !name}
              className="px-3 py-1.5 text-sm rounded-md bg-caliber-600 text-white hover:bg-caliber-700 disabled:opacity-50"
              data-testid="server-submit-btn"
            >
              {isEdit
                ? submitting
                  ? "Saving…"
                  : "Save changes"
                : submitting
                  ? "Registering…"
                  : "Register Server"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

/* ── Server row ----------------------------------------------------------- */

function ServerRow({
  server,
  onRefresh,
  onShowDetail,
  isAdmin,
  onEdit,
}: {
  server: McpServer;
  onRefresh: () => void;
  onShowDetail: (server: McpServer) => void;
  isAdmin: boolean;
  onEdit: (server: McpServer) => void;
}): JSX.Element {
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<McpTestConnectionResult | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const handleDelete = async () => {
    setDeleting(true);
    setDeleteError(null);
    try {
      await caliberApi.deleteMcpServer(server.server_id);
      setConfirmingDelete(false);
      onRefresh();
    } catch (error: unknown) {
      setDeleteError(
        error instanceof Error ? error.message : "Failed to delete server",
      );
    } finally {
      setDeleting(false);
    }
  };

  const handleTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const result = await caliberApi.testMcpConnection(server.server_id);
      setTestResult({
        ...result,
        error: formatMcpOperatorError(result.error),
      });
      onRefresh();
    } catch (error: unknown) {
      setTestResult({
        server_id: server.server_id,
        success: false,
        error:
          formatMcpOperatorError(
            error instanceof Error ? error.message : String(error),
          ) ?? "Failed to test connection",
        tools: [],
      });
    } finally {
      setTesting(false);
    }
  };

  return (
    <tr
      className="hover:bg-surface-50 group cursor-pointer"
      onDoubleClick={() => onShowDetail(server)}
      title="Double-click to view server details"
      data-testid={`mcp-row-${server.server_id}`}
    >
      <td className="px-4 py-3">
        <div className="flex items-center gap-3">
          <ServerIcon icon={server.icon} />
          <div>
            <div className="font-medium text-zinc-900">{server.name}</div>
            <div className="text-xs text-zinc-400 font-mono">{server.server_id}</div>
          </div>
        </div>
      </td>
      <td className="px-4 py-3">
        <span
          className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium ${
            STATUS_STYLES[server.status] ?? STATUS_STYLES.disabled
          }`}
        >
          <span
            className={`inline-block h-1.5 w-1.5 rounded-full ${
              STATUS_DOT[server.status] ?? STATUS_DOT.disabled
            }`}
          />
          {server.status}
        </span>
        {server.connection_error && (
          <div className="text-xs text-red-500 mt-0.5 max-w-[200px] truncate" title={server.connection_error}>
            {server.connection_error}
          </div>
        )}
      </td>
      <td className="px-4 py-3">
        <span className="inline-flex items-center rounded bg-zinc-100 px-2 py-0.5 text-xs font-mono text-zinc-600">
          {TRANSPORT_LABEL[server.transport] ?? server.transport}
        </span>
      </td>
      <td className="px-4 py-3 text-xs text-zinc-600 max-w-[200px]">
        <div className="truncate" title={server.transport === "stdio" ? server.command : server.uri}>
          {server.transport === "stdio"
            ? [server.command, ...server.args].join(" ")
            : server.uri}
        </div>
      </td>
      <td className="px-4 py-3">
        <div className="flex flex-wrap gap-1">
          {server.discovered_tools.length === 0 && (
            <span className="text-xs text-zinc-400">—</span>
          )}
          {server.discovered_tools.slice(0, 4).map((t) => (
            <span
              key={t.name}
              className="inline-flex items-center rounded bg-caliber-50 border border-caliber-100 px-1.5 py-0.5 text-[10px] font-medium text-caliber-700"
              title={t.description}
            >
              {t.name}
            </span>
          ))}
          {server.discovered_tools.length > 4 && (
            <span className="text-[10px] text-zinc-400">
              +{server.discovered_tools.length - 4} more
            </span>
          )}
        </div>
      </td>
      <td className="px-4 py-3 text-xs text-zinc-500">
        {server.last_connected_at
          ? new Date(server.last_connected_at).toLocaleString()
          : "Never"}
      </td>
      <td className="px-4 py-3 text-right">
        <div className="flex items-center justify-end gap-1">
          <button
            type="button"
            onClick={handleTest}
            disabled={testing}
            className="inline-flex items-center gap-1 rounded-md border border-zinc-300 bg-white px-2.5 py-1 text-xs font-medium text-zinc-700 shadow-sm hover:bg-zinc-50 disabled:opacity-50"
            data-testid={`test-btn-${server.server_id}`}
          >
            {testing ? (
              <span className="animate-spin h-3 w-3 border-2 border-zinc-300 border-t-zinc-600 rounded-full" />
            ) : (
              <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z" />
              </svg>
            )}
            {testing ? "Testing…" : "Test"}
          </button>

          {isAdmin && !confirmingDelete && (
            <>
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  onEdit(server);
                }}
                className="inline-flex items-center gap-1 rounded-md border border-zinc-300 bg-white px-2.5 py-1 text-xs font-medium text-zinc-700 shadow-sm hover:bg-zinc-50"
                data-testid={`edit-btn-${server.server_id}`}
              >
                Edit
              </button>
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  setDeleteError(null);
                  setConfirmingDelete(true);
                }}
                className="inline-flex items-center gap-1 rounded-md border border-red-200 bg-white px-2.5 py-1 text-xs font-medium text-red-600 shadow-sm hover:bg-red-50"
                data-testid={`delete-btn-${server.server_id}`}
              >
                Delete
              </button>
            </>
          )}

          {isAdmin && confirmingDelete && (
            <>
              <span className="text-xs text-zinc-600">Delete?</span>
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  void handleDelete();
                }}
                disabled={deleting}
                className="inline-flex items-center gap-1 rounded-md bg-red-600 px-2.5 py-1 text-xs font-medium text-white shadow-sm hover:bg-red-700 disabled:opacity-50"
                data-testid={`confirm-delete-btn-${server.server_id}`}
              >
                {deleting ? "Deleting…" : "Confirm"}
              </button>
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  setConfirmingDelete(false);
                }}
                disabled={deleting}
                className="inline-flex items-center gap-1 rounded-md border border-zinc-300 bg-white px-2.5 py-1 text-xs font-medium text-zinc-700 shadow-sm hover:bg-zinc-50 disabled:opacity-50"
                data-testid={`cancel-delete-btn-${server.server_id}`}
              >
                Cancel
              </button>
            </>
          )}
        </div>
        {testResult && (
          <div className={`text-[10px] mt-1 ${testResult.success ? "text-emerald-600" : "text-red-500"}`}>
            {testResult.success
              ? `Connected · ${testResult.tools.length} tools`
              : testResult.error}
          </div>
        )}
        {deleteError && (
          <div className="text-[10px] mt-1 text-red-500">{deleteError}</div>
        )}
      </td>
    </tr>
  );
}

/* ── page ----------------------------------------------------------------- */

/* ── Quick-connect catalog ------------------------------------------------ */

interface McpTemplate {
  id: string;
  name: string;
  description: string;
  icon: string;
  transport: "stdio" | "sse" | "streamable-http";
  command: string;
  args: string[];
  /** Env vars passed to the server (values may use ${VAR} placeholders). */
  env?: Record<string, string>;
  tokenEnvVar: string;
  docsUrl: string;
  /** Well-known tools this server exposes — seeded onto the server on connect. */
  tools: McpServerDiscoveredTool[];
}

// The three Postgres-family entries are backed by CALIBER's first-party DB MCP
// server (caliber.mcp_servers.db), each launched in a different --mode so it
// exposes write tools tailored to that class. ``${PYTHON}`` resolves to the
// CALIBER interpreter, so the server runs under the same venv (where psycopg is
// installed) without a hardcoded path. ``${POSTGRES_URL}`` is resolved by the
// gateway from the CALIBER process env (set POSTGRES_URL in your .env to
// postgresql://caliber:caliber@localhost:5432/caliber — see deploy/mcp/).
const PG_COMMAND = "${PYTHON}";
const pgArgs = (mode: "relational" | "vector" | "graph"): string[] => [
  "-m",
  "caliber.mcp_servers.db",
  "--mode",
  mode,
];
const PG_ENV = { POSTGRES_URL: "${POSTGRES_URL}" };

const MCP_CATALOG: McpTemplate[] = [
  {
    id: "github",
    name: "GitHub",
    description: "Repos, issues, PRs, code search, actions",
    icon: "github",
    transport: "stdio",
    command: "npx",
    args: ["-y", "@modelcontextprotocol/server-github"],
    tokenEnvVar: "GITHUB_TOKEN",
    docsUrl: "https://github.com/modelcontextprotocol/servers/tree/main/src/github",
    tools: [
      { name: "create_issue", description: "Create a new GitHub issue" },
      { name: "search_repositories", description: "Search for GitHub repositories" },
      { name: "get_file_contents", description: "Read a file from a repository" },
      { name: "create_pull_request", description: "Open a new pull request" },
      { name: "list_commits", description: "List commits on a branch" },
      { name: "create_branch", description: "Create a new branch" },
    ],
  },
  {
    id: "postgres",
    name: "PostgreSQL",
    description: "Create tables, insert/update/delete rows, run SQL",
    icon: "database",
    transport: "stdio",
    command: PG_COMMAND,
    args: pgArgs("relational"),
    env: PG_ENV,
    tokenEnvVar: "",
    docsUrl: "https://www.postgresql.org/docs/",
    tools: [
      { name: "list_tables", description: "List tables in the public schema" },
      { name: "describe_table", description: "Show a table's columns, types, and defaults" },
      { name: "create_table", description: "Create a table from a column spec" },
      { name: "insert_rows", description: "Insert one or more rows" },
      { name: "update_rows", description: "Update rows matching a WHERE filter" },
      { name: "delete_rows", description: "Delete rows matching a WHERE filter" },
      { name: "run_query", description: "Run a read-only SQL query with bound parameters" },
      { name: "execute_sql", description: "Run arbitrary DDL/DML (escape hatch)" },
    ],
  },
  {
    id: "pgvector",
    name: "pgvector",
    description: "Vector tables, embedding upserts, similarity search",
    icon: "database",
    transport: "stdio",
    command: PG_COMMAND,
    args: pgArgs("vector"),
    env: PG_ENV,
    tokenEnvVar: "",
    docsUrl: "https://github.com/pgvector/pgvector",
    tools: [
      { name: "create_vector_table", description: "Create a vector table + HNSW index" },
      { name: "upsert_vectors", description: "Insert/update id + embedding + metadata rows" },
      { name: "similarity_search", description: "Find the k nearest vectors by cosine/l2/ip" },
      { name: "list_tables", description: "List tables, including those with vector columns" },
      { name: "describe_table", description: "Show columns and types, including vector dimensions" },
      { name: "run_query", description: "Run a read-only SQL query with bound parameters" },
    ],
  },
  {
    id: "apache-age",
    name: "Apache AGE",
    description: "Graphs, vertices, edges, and openCypher queries",
    icon: "graph",
    transport: "stdio",
    command: PG_COMMAND,
    args: pgArgs("graph"),
    env: PG_ENV,
    tokenEnvVar: "",
    docsUrl: "https://age.apache.org/",
    tools: [
      { name: "create_graph", description: "Create a named graph" },
      { name: "drop_graph", description: "Drop a graph and its contents" },
      { name: "create_vertex", description: "Create a labelled vertex with properties" },
      { name: "create_edge", description: "Connect two vertices with a labelled edge" },
      { name: "cypher_query", description: "Run an openCypher query via cypher(...)" },
      { name: "run_query", description: "Run a read-only SQL query with bound parameters" },
    ],
  },
  {
    id: "minio",
    name: "MinIO",
    description: "S3-compatible blob storage, buckets, objects, presigned URLs",
    icon: "storage",
    transport: "stdio",
    command: "docker",
    args: [
      "run", "-i", "--rm",
      "-e", "MINIO_ENDPOINT=localhost:9000",
      "-e", "MINIO_ACCESS_KEY=${MINIO_ACCESS_KEY}",
      "-e", "MINIO_SECRET_KEY=${MINIO_SECRET_KEY}",
      "-e", "MINIO_USE_SSL=false",
      "quay.io/minio/aistor/mcp-server-aistor:latest",
      "--allow-write", "--allow-delete",
    ],
    tokenEnvVar: "MINIO_ACCESS_KEY",
    docsUrl: "https://github.com/minio/mcp-server-aistor",
    tools: [
      { name: "list_buckets", description: "List storage buckets" },
      { name: "list_objects", description: "List objects in a bucket" },
      { name: "get_object", description: "Download an object" },
      { name: "put_object", description: "Upload an object" },
      { name: "presigned_url", description: "Generate a presigned URL for an object" },
      { name: "remove_object", description: "Delete an object" },
    ],
  },
  {
    id: "huggingface",
    name: "Hugging Face",
    description: "Models, datasets, spaces, papers, collections search",
    icon: "huggingface",
    transport: "stdio",
    command: "pip",
    args: ["run", "huggingface-mcp-server"],
    tokenEnvVar: "HF_TOKEN",
    docsUrl: "https://pypi.org/project/huggingface-mcp-server/",
    tools: [
      { name: "search_models", description: "Search the Hugging Face model hub" },
      { name: "search_datasets", description: "Search datasets" },
      { name: "search_spaces", description: "Search Spaces apps" },
      { name: "get_model_info", description: "Get details for a specific model" },
      { name: "search_papers", description: "Search ML papers" },
    ],
  },
  {
    id: "ollama",
    name: "Ollama",
    description: "Local LLM inference, model management, chat, and embeddings",
    icon: "ollama",
    transport: "stdio",
    command: "npx",
    args: ["-y", "ollama-mcp-server"],
    tokenEnvVar: "",
    docsUrl: "https://github.com/hyzhak/ollama-mcp-server",
    tools: [
      { name: "list_models", description: "List locally available models" },
      { name: "pull_model", description: "Download a model" },
      { name: "chat", description: "Chat completion with a local model" },
      { name: "generate", description: "Text generation with a local model" },
      { name: "embeddings", description: "Generate embeddings for text" },
    ],
  },
  {
    id: "playwright",
    name: "Playwright",
    description:
      "Browser automation, snapshots, screenshots, tabs, and form interactions",
    icon: "playwright",
    transport: "stdio",
    command: "npx",
    args: ["@playwright/mcp@latest"],
    tokenEnvVar: "",
    docsUrl: "https://playwright.dev/mcp/installation",
    tools: [
      { name: "browser_navigate", description: "Navigate to a URL" },
      { name: "browser_snapshot", description: "Capture an accessibility snapshot" },
      { name: "browser_click", description: "Click an element by snapshot reference" },
      { name: "browser_type", description: "Type text into an element" },
      { name: "browser_fill_form", description: "Fill multiple form fields" },
      { name: "browser_take_screenshot", description: "Capture a page or element screenshot" },
      { name: "browser_wait_for", description: "Wait for text, time, or page readiness" },
      { name: "browser_tabs", description: "Create, close, and switch browser tabs" },
      { name: "browser_console_messages", description: "Read browser console output" },
      { name: "browser_network_requests", description: "Inspect page network requests" },
      { name: "browser_close", description: "Close the browser session" },
    ],
  },
];

/* Extra icons not in the original ICONS map */
const CATALOG_ICONS: Record<string, JSX.Element> = {
  jira: (
    <svg className="w-6 h-6" viewBox="0 0 24 24" fill="currentColor">
      <path d="M11.571 11.513H0a5.218 5.218 0 005.232 5.215h2.13v2.057A5.215 5.215 0 0012.575 24V12.518a1.005 1.005 0 00-1.005-1.005zm5.723-5.756H5.736a5.215 5.215 0 005.215 5.214h2.129v2.058a5.218 5.218 0 005.215 5.214V6.758a1.001 1.001 0 00-1.001-1.001zM23 .012H11.438a5.218 5.218 0 005.215 5.215h2.129v2.057A5.215 5.215 0 0024 12.5V1.013A1.001 1.001 0 0023 .012z" />
    </svg>
  ),
  linear: (
    <svg className="w-6 h-6" viewBox="0 0 24 24" fill="currentColor">
      <path d="M2.654 10.6a.41.41 0 00-.103.414l2.344 7.03a.41.41 0 00.61.201l4.907-3.267a.41.41 0 00.03-.655L5.1 9.177a.41.41 0 00-.536-.028L2.654 10.6zm5.072-3.584a.41.41 0 00-.55.097L4.324 11.06a.41.41 0 00.044.536l5.143 5.143a.41.41 0 00.536.044l3.948-2.854a.41.41 0 00.097-.55L7.726 7.016zm7.192 1.202L11.37 3.396a.41.41 0 00-.615-.03L8.01 6.11a.41.41 0 00-.023.556l6.354 7.528a.41.41 0 00.625-.003l2.605-3.074a.41.41 0 00-.011-.558l-2.643-2.34zm4.036 1.242l-2.387-2.387a.41.41 0 00-.612.039l-2.03 2.595a.41.41 0 00.015.515l3.547 3.749a.41.41 0 00.613-.005l1.89-2.072a.41.41 0 00-.005-.568l-1.031-.866z" />
    </svg>
  ),
  search: (
    <svg className="w-6 h-6" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <circle cx="11" cy="11" r="8" />
      <path d="M21 21l-4.35-4.35" />
    </svg>
  ),
  storage: (
    <svg className="w-6 h-6" viewBox="0 0 24 24" fill="currentColor">
      <path d="M2 20h20v-4H2v4zm2-3h2v2H4v-2zM2 4v4h20V4H2zm4 3H4V5h2v2zm-4 7h20v-4H2v4zm2-3h2v2H4v-2z" />
    </svg>
  ),
  huggingface: (
    <svg className="w-6 h-6" viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 2C6.477 2 2 6.477 2 12s4.477 10 10 10 10-4.477 10-10S17.523 2 12 2zm-1.5 14.5c-1.38 0-2.5-1.12-2.5-2.5 0-.55.18-1.06.5-1.47V9.5c0-.83.67-1.5 1.5-1.5s1.5.67 1.5 1.5v3.03c.32.41.5.92.5 1.47 0 1.38-1.12 2.5-2.5 2.5zm5 0c-1.38 0-2.5-1.12-2.5-2.5 0-.55.18-1.06.5-1.47V9.5c0-.83.67-1.5 1.5-1.5s1.5.67 1.5 1.5v3.03c.32.41.5.92.5 1.47 0 1.38-1.12 2.5-2.5 2.5z" />
    </svg>
  ),
  azure: (
    <svg className="w-6 h-6" viewBox="0 0 24 24" fill="currentColor">
      <path d="M5.483 21.004H14.3l4.222-4.968-5.837-6.95L5.483 21.004zm7.56-16.008l-3.39 8.575 6.193 7.386h3.671L13.043 4.996z" />
    </svg>
  ),
};

function CatalogIcon({ icon }: { icon: string }): JSX.Element {
  if (ICONS[icon]) return <span className="text-zinc-700">{ICONS[icon]}</span>;
  if (CATALOG_ICONS[icon]) return <span className="text-zinc-700">{CATALOG_ICONS[icon]}</span>;
  return (
    <span className="w-6 h-6 rounded bg-zinc-200 flex items-center justify-center text-[10px] font-bold text-zinc-500">
      MCP
    </span>
  );
}

function QuickConnectCatalog({
  existingServers,
  onConnect,
}: {
  existingServers: McpServer[];
  onConnect: (template: McpTemplate) => void;
}): JSX.Element {
  const existingNames = new Set(existingServers.map((s) => s.name.toLowerCase()));

  return (
    <div className="mb-6">
      <h2 className="text-sm font-semibold text-zinc-800 mb-1">Quick Connect</h2>
      <p className="text-xs text-zinc-500 mb-3">
        Click a server to configure and connect it.
      </p>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2.5">
        {MCP_CATALOG.map((tpl) => {
          const alreadyAdded = existingNames.has(tpl.name.toLowerCase());
          return (
            <button
              key={tpl.id}
              type="button"
              onClick={() => {
                onConnect(tpl);
              }}
              className={`relative flex items-start gap-3 rounded-xl border px-3.5 py-3 text-left transition-all select-none ${
                alreadyAdded
                  ? "border-emerald-200 bg-emerald-50/50 hover:border-emerald-300 hover:bg-emerald-50 cursor-pointer active:scale-[0.98]"
                  : "border-zinc-200 bg-white hover:border-caliber-300 hover:bg-caliber-50/40 hover:shadow-sm cursor-pointer active:scale-[0.98]"
              }`}
              title={
                alreadyAdded
                  ? `${tpl.name} is already connected — click to configure another`
                  : `Click to set up ${tpl.name}`
              }
              data-testid={`catalog-${tpl.id}`}
            >
              <div className="mt-0.5 shrink-0">
                <CatalogIcon icon={tpl.icon} />
              </div>
              <div className="min-w-0">
                <div className="flex items-center gap-1.5">
                  <span className="text-sm font-medium text-zinc-900">{tpl.name}</span>
                  {alreadyAdded && (
                    <span className="inline-flex items-center rounded-full bg-emerald-100 px-1.5 py-0.5 text-[9px] font-semibold text-emerald-700">
                      Connected
                    </span>
                  )}
                </div>
                <p className="text-[11px] text-zinc-500 leading-snug mt-0.5">{tpl.description}</p>
                {tpl.tokenEnvVar && (
                  <p className="text-[10px] text-zinc-400 mt-1 font-mono">{tpl.tokenEnvVar}</p>
                )}
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function ServerDetailDialog({
  server,
  onClose,
}: {
  server: McpServer | null;
  onClose: () => void;
}): JSX.Element | null {
  if (!server) return null;
  const statusColor =
    server.status === "active"
      ? "bg-emerald-50 text-emerald-700 ring-emerald-200"
      : server.status === "error"
        ? "bg-red-50 text-red-700 ring-red-200"
        : "bg-zinc-100 text-zinc-600 ring-zinc-200";
  const envKeys = Object.keys(server.env ?? {});
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
      data-testid="mcp-detail-dialog"
    >
      <div
        className="max-h-[85vh] w-full max-w-2xl overflow-y-auto rounded-2xl bg-white p-6 shadow-xl"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="mb-4 flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="text-lg font-semibold text-zinc-900">{server.name}</h2>
            <div className="mt-1 font-mono text-xs text-zinc-400">{server.server_id}</div>
          </div>
          <div className="flex items-center gap-2">
            <span className={`rounded-md px-2 py-0.5 text-xs font-medium ring-1 ${statusColor}`}>
              {server.status}
            </span>
            <button
              type="button"
              onClick={onClose}
              className="rounded-md p-1 text-zinc-400 hover:bg-zinc-100 hover:text-zinc-600"
              aria-label="Close"
            >
              ✕
            </button>
          </div>
        </div>
        {server.description && <p className="mb-4 text-sm text-zinc-600">{server.description}</p>}
        <dl className="mb-5 grid grid-cols-3 gap-x-4 gap-y-2 text-sm">
          <dt className="text-zinc-500">Transport</dt>
          <dd className="col-span-2 font-mono text-zinc-800">{server.transport}</dd>
          {server.transport === "stdio" ? (
            <>
              <dt className="text-zinc-500">Command</dt>
              <dd className="col-span-2 break-all font-mono text-zinc-800">
                {[server.command, ...server.args].filter(Boolean).join(" ") || "—"}
              </dd>
            </>
          ) : (
            <>
              <dt className="text-zinc-500">URI</dt>
              <dd className="col-span-2 break-all font-mono text-zinc-800">{server.uri || "—"}</dd>
            </>
          )}
          <dt className="text-zinc-500">Auth</dt>
          <dd className="col-span-2 text-zinc-800">{server.auth_type}</dd>
          {envKeys.length > 0 && (
            <>
              <dt className="text-zinc-500">Env</dt>
              <dd className="col-span-2 font-mono text-xs text-zinc-700">{envKeys.join(", ")}</dd>
            </>
          )}
          <dt className="text-zinc-500">Last connected</dt>
          <dd className="col-span-2 text-zinc-800">
            {server.last_connected_at ? new Date(server.last_connected_at).toLocaleString() : "Never"}
          </dd>
          {server.connection_error && (
            <>
              <dt className="text-zinc-500">Error</dt>
              <dd className="col-span-2 text-red-600">{server.connection_error}</dd>
            </>
          )}
        </dl>
        <h3 className="mb-2 text-sm font-semibold text-zinc-900">
          Discovered tools ({server.discovered_tools.length})
        </h3>
        {server.discovered_tools.length === 0 ? (
          <p className="text-sm text-zinc-400">No tools discovered yet — run “Test” to connect.</p>
        ) : (
          <ul className="space-y-2">
            {server.discovered_tools.map((tool) => {
              const policy = server.tool_policies?.[tool.name];
              return (
                <li key={tool.name} className="rounded-lg border border-zinc-200 p-3">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-sm font-medium text-zinc-800">{tool.name}</span>
                    {policy && (
                      <span className="rounded-md bg-zinc-100 px-2 py-0.5 text-[11px] text-zinc-600">
                        {policy.side_effect_level}
                        {policy.requires_approval ? " · approval" : ""}
                        {policy.allowed ? "" : " · blocked"}
                      </span>
                    )}
                  </div>
                  {tool.description && <p className="mt-1 text-xs text-zinc-500">{tool.description}</p>}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}

export function McpServers(): JSX.Element {
  const [showAdd, setShowAdd] = useState(false);
  const [addInitial, setAddInitial] = useState<AddServerInitialValues | undefined>();
  const [refreshKey, setRefreshKey] = useState(0);
  const [activeTab, setActiveTab] = useState<"servers" | "playground">("servers");
  const [detailServer, setDetailServer] = useState<McpServer | null>(null);
  const [editServer, setEditServer] = useState<McpServer | null>(null);
  // Search + Status + Transport filters for the server table. All default to the
  // empty "All" sentinel, so the default view is unchanged. A row must match the
  // text query AND every active filter (additive).
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [transportFilter, setTransportFilter] = useState("");

  const fetcher = useCallback(
    (signal: AbortSignal) => caliberApi.listMcpServers(undefined, signal),
    [],
  );
  const { data, error, loading } = useApi(fetcher, [refreshKey]);

  const query = search.trim().toLowerCase();
  const visibleServers = (data ?? []).filter((server) => {
    if (statusFilter && server.status !== statusFilter) return false;
    if (transportFilter && server.transport !== transportFilter) return false;
    if (!query) return true;
    const endpoint = server.transport === "stdio" ? server.command : server.uri;
    return [server.name, server.description, endpoint]
      .filter(Boolean)
      .some((field) => String(field).toLowerCase().includes(query));
  });
  const hasServerFilters = Boolean(search || statusFilter || transportFilter);

  // MCP server config (register / edit / delete) is an admin-only operation on
  // the backend; gate the mutating controls so only admins see them.
  const meFetcher = useCallback((signal: AbortSignal) => caliberApi.getMe(signal), []);
  const { data: me } = useApi(meFetcher, []);
  const isAdmin = me?.is_admin ?? false;

  const refresh = () => setRefreshKey((k) => k + 1);

  const handleCatalogClick = (tpl: McpTemplate) => {
    setAddInitial({
      name: tpl.name,
      description: tpl.description,
      transport: tpl.transport,
      command: tpl.command,
      args: tpl.args.join(" "),
      env: tpl.env,
      authType: tpl.tokenEnvVar ? "token" : "none",
      tokenEnvVar: tpl.tokenEnvVar,
      icon: tpl.icon,
      tools: tpl.tools,
    });
    setShowAdd(true);
  };

  return (
    <div className="space-y-6 animate-fade-in">
      <PageHeader
        title="MCP Servers"
        subtitle="External tool servers that workflow agents can connect to for additional capabilities."
        actions={
          activeTab === "servers" ? (
            <button
              type="button"
              onClick={() => {
                setAddInitial(undefined);
                setShowAdd(true);
              }}
              className="inline-flex items-center gap-1.5 rounded-lg bg-caliber-600 px-3.5 py-2 text-sm font-medium text-white shadow-sm transition-colors hover:bg-caliber-700"
              data-testid="add-server-btn"
            >
              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
              </svg>
              Add Server
            </button>
          ) : undefined
        }
      />

      <PageTabs
        tabs={MCP_TABS}
        active={activeTab}
        onChange={(k) => setActiveTab(k as typeof activeTab)}
      />

      {error && (
        <div className="flex items-start gap-3 rounded-2xl border border-red-200/60 bg-red-50 px-5 py-4 shadow-card">
          <svg className="w-4 h-4 mt-0.5 text-red-500 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" />
          </svg>
          <div className="flex-1 text-sm text-red-700">
            <div className="font-semibold">Failed to load MCP servers</div>
            <div className="text-xs mt-0.5 text-red-500">{error.message}</div>
          </div>
        </div>
      )}

      {/* ── tab content ────────────────────────────────────── */}
      {activeTab === "servers" && (
        <>
          <QuickConnectCatalog
            existingServers={data ?? []}
            onConnect={handleCatalogClick}
          />

          <FilterBar
            search={
              <SearchInput
                value={search}
                onChange={setSearch}
                ariaLabel="Search MCP servers"
                placeholder="Search servers by name, description, endpoint…"
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
                    { value: "error", label: "Error" },
                    { value: "disabled", label: "Disabled" },
                  ]}
                  className="w-full sm:w-44"
                />
                <FilterSelect
                  label="Transport"
                  allLabel="All transports"
                  value={transportFilter}
                  onChange={setTransportFilter}
                  options={[
                    { value: "stdio", label: "stdio" },
                    { value: "sse", label: "SSE" },
                    { value: "streamable-http", label: "Streamable HTTP" },
                  ]}
                  className="w-full sm:w-48"
                />
              </>
            }
            actions={
              <ClearFiltersButton
                visible={hasServerFilters}
                onClear={() => {
                  setSearch("");
                  setStatusFilter("");
                  setTransportFilter("");
                }}
              />
            }
          />

          <div className="bg-white rounded-lg border border-surface-200 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-surface-200 bg-surface-50">
                  <th className="text-left font-medium px-4 py-3">Server</th>
                  <th className="text-left font-medium px-4 py-3">Status</th>
                  <th className="text-left font-medium px-4 py-3">Transport</th>
                  <th className="text-left font-medium px-4 py-3">Endpoint</th>
                  <th className="text-left font-medium px-4 py-3">Tools</th>
                  <th className="text-left font-medium px-4 py-3">Last Connected</th>
                  <th className="text-right font-medium px-4 py-3">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-surface-100">
                {loading && !data && (
                  <tr>
                    <td colSpan={7} className="px-4 py-10 text-center text-sm text-gray-500">
                      Loading…
                    </td>
                  </tr>
                )}
                {data && data.length === 0 && (
                  <tr>
                    <td colSpan={7} className="px-4 py-10 text-center text-sm text-gray-500">
                      No MCP servers registered yet. Click &ldquo;Add Server&rdquo;
                      to connect your first external tool provider.
                    </td>
                  </tr>
                )}
                {data && data.length > 0 && visibleServers.length === 0 && (
                  <tr>
                    <td colSpan={7} className="px-4 py-10 text-center text-sm text-gray-500">
                      No MCP servers match the current filters.
                    </td>
                  </tr>
                )}
                {visibleServers.map((server) => (
                  <ServerRow key={server.server_id} server={server} onRefresh={refresh} onShowDetail={setDetailServer} isAdmin={isAdmin} onEdit={setEditServer} />
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {activeTab === "playground" && (
        <PlaygroundTab servers={data ?? []} loading={loading} refresh={refresh} />
      )}

      <AddServerDialog
        open={showAdd || editServer !== null}
        editServer={editServer}
        onClose={() => { setShowAdd(false); setAddInitial(undefined); setEditServer(null); }}
        onCreated={refresh}
        initialValues={editServer ? undefined : addInitial}
      />

      <ServerDetailDialog server={detailServer} onClose={() => setDetailServer(null)} />
    </div>
  );
}

/* ── Playground tab ─────────────────────────────────────────────────────── */

const PLAYGROUND_STATUS_BADGE: Record<string, { bg: string; dot: string; label: string }> = {
  active:   { bg: "bg-emerald-50 text-emerald-700 border-emerald-200", dot: "bg-emerald-500", label: "Connected" },
  error:    { bg: "bg-red-50 text-red-700 border-red-200",             dot: "bg-red-500",     label: "Error" },
  disabled: { bg: "bg-zinc-100 text-zinc-500 border-zinc-200",         dot: "bg-zinc-400",    label: "Disabled" },
};

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

const DEFAULT_TOOL_POLICY: McpToolPolicy = {
  allowed: true,
  side_effect_level: "read",
  requires_approval: false,
  rate_limit_per_minute: null,
};

function withDefaultPolicy(tool: McpServerDiscoveredTool): McpDiscoveredToolWithPolicy {
  return {
    ...tool,
    policy: { ...DEFAULT_TOOL_POLICY },
  };
}

function PlaygroundTab({
  servers,
  loading,
  refresh,
}: {
  servers: McpServer[];
  loading: boolean;
  refresh: () => void;
}): JSX.Element {
  /* ── selection ──────────────────────────────────── */
  const [selectedId, setSelectedId] = useState<string>("");
  const selected = servers.find((s) => s.server_id === selectedId) ?? null;
  const [discoveredTools, setDiscoveredTools] = useState<McpDiscoveredToolWithPolicy[]>([]);
  const [toolsLoading, setToolsLoading] = useState(false);
  const [toolsError, setToolsError] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedId && servers.length > 0) {
      setSelectedId(servers[0]!.server_id);
    }
  }, [servers, selectedId]);

  const loadTools = useCallback(
    async (serverId: string, fallbackTools: McpServerDiscoveredTool[] = []) => {
      setToolsLoading(true);
      setToolsError(null);
      try {
        const listed = await caliberApi.listMcpTools(serverId);
        setDiscoveredTools(listed.tools);
      } catch (err) {
        const fallbackServer = servers.find((s) => s.server_id === serverId);
        const fallback = fallbackTools.length > 0 ? fallbackTools : (fallbackServer?.discovered_tools ?? []);
        setDiscoveredTools(fallback.map(withDefaultPolicy));
        setToolsError(err instanceof Error ? err.message : "Failed to load MCP tool policies");
      } finally {
        setToolsLoading(false);
      }
    },
    [servers],
  );

  useEffect(() => {
    if (!selected) {
      setDiscoveredTools([]);
      setToolsError(null);
      return;
    }
    void loadTools(selected.server_id);
  }, [selected, loadTools]);

  /* ── test connection ────────────────────────────── */
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<McpTestConnectionResult | null>(null);
  const [testDuration, setTestDuration] = useState<number | null>(null);

  const runTest = async () => {
    if (!selected) return;
    setTesting(true);
    setTestResult(null);
    setTestDuration(null);
    const t0 = performance.now();
    try {
      const discovered = await caliberApi.discoverMcpTools(selected.server_id);
      const result: McpTestConnectionResult = {
        server_id: discovered.server_id,
        success: true,
        error: null,
        tools: discovered.tools,
      };
      setTestDuration(Math.round(performance.now() - t0));
      setTestResult(result);
      refresh();
      await loadTools(selected.server_id, discovered.tools);
    } catch {
      try {
        const result = await caliberApi.testMcpConnection(selected.server_id);
        setTestDuration(Math.round(performance.now() - t0));
        setTestResult({
          ...result,
          error: formatMcpOperatorError(result.error),
        });
        refresh();
        await loadTools(selected.server_id, result.tools);
      } catch (error: unknown) {
        setTestDuration(Math.round(performance.now() - t0));
        setTestResult({
          server_id: selected.server_id,
          success: false,
          error:
            formatMcpOperatorError(
              error instanceof Error ? error.message : String(error),
            ) ?? "Request failed — check the server is running",
          tools: [],
        });
      }
    } finally {
      setTesting(false);
    }
  };

  /* ── tool invocation ────────────────────────────── */
  const [activeTool, setActiveTool] = useState<McpDiscoveredToolWithPolicy | null>(null);
  const [toolArgs, setToolArgs] = useState<Record<string, string>>({});
  const [invoking, setInvoking] = useState(false);
  const [invocationResult, setInvocationResult] = useState<McpToolInvocationResult | null>(null);
  const [invocationHistory, setInvocationHistory] = useState<
    { tool: string; args: Record<string, string>; result: McpToolInvocationResult; timestamp: number }[]
  >([]);

  const handleToolSelect = (tool: McpDiscoveredToolWithPolicy) => {
    setActiveTool(tool);
    setToolArgs({});
    setInvocationResult(null);
  };

  const handleInvoke = async () => {
    if (!selected || !activeTool) return;
    setInvoking(true);
    setInvocationResult(null);
    try {
      // Build typed args from string inputs
      const parsedArgs: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(toolArgs)) {
        if (v.trim()) {
          const propType = activeTool.input_schema?.properties?.[k]?.type;
          if (propType === "integer") {
            parsedArgs[k] = parseInt(v, 10);
          } else if (propType === "number") {
            parsedArgs[k] = parseFloat(v);
          } else if (propType === "boolean") {
            parsedArgs[k] = v === "true";
          } else {
            parsedArgs[k] = v;
          }
        }
      }
      const result = await caliberApi.invokeMcpTool(
        selected.server_id,
        activeTool.name,
        parsedArgs,
      );
      const normalizedResult = result.success
        ? result
        : {
            ...result,
            error:
              formatMcpOperatorError(result.error) ??
              "Request failed — check the server is running",
          };
      setInvocationResult(normalizedResult);
      setInvocationHistory((h) => [
        {
          tool: activeTool.name,
          args: { ...toolArgs },
          result: normalizedResult,
          timestamp: Date.now(),
        },
        ...h.slice(0, 19),
      ]);
    } catch (error: unknown) {
      const errorResult: McpToolInvocationResult = {
        server_id: selected.server_id,
        tool_name: activeTool.name,
        success: false,
        error:
          formatMcpOperatorError(
            error instanceof Error ? error.message : String(error),
          ) ?? "Request failed — check the server is running",
        result: null,
        duration_ms: 0,
      };
      setInvocationResult(errorResult);
      setInvocationHistory((h) => [
        {
          tool: activeTool.name,
          args: { ...toolArgs },
          result: errorResult,
          timestamp: Date.now(),
        },
        ...h.slice(0, 19),
      ]);
    } finally {
      setInvoking(false);
    }
  };

  /* ── render ─────────────────────────────────────── */
  if (loading && servers.length === 0) {
    return (
      <div className="flex items-center justify-center min-h-[30vh]">
        <p className="text-sm text-zinc-400 animate-pulse">Loading servers…</p>
      </div>
    );
  }

  if (servers.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-zinc-300 bg-zinc-50 p-8 text-center">
        <p className="text-sm text-zinc-500 mb-2">No MCP servers registered yet.</p>
        <p className="text-sm text-zinc-400">Switch to the Servers tab to add one.</p>
      </div>
    );
  }

  const badge = selected ? PLAYGROUND_STATUS_BADGE[selected.status] ?? PLAYGROUND_STATUS_BADGE.disabled : null;
  const tools = discoveredTools.length > 0
    ? discoveredTools
    : (testResult?.tools?.length ? testResult.tools : (selected?.discovered_tools ?? [])).map(withDefaultPolicy);

  return (
    <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
      {/* ── LEFT COLUMN: server picker + test + details ── */}
      <div className="lg:col-span-3 space-y-4">
        {/* server picker */}
        <div>
          <label className="block text-xs font-medium text-zinc-700 mb-1">Server</label>
          <select
            aria-label="Select MCP server"
            value={selectedId}
            onChange={(e) => {
              setSelectedId(e.target.value);
              setTestResult(null);
              setTestDuration(null);
              setActiveTool(null);
              setInvocationResult(null);
            }}
            className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500 outline-none"
          >
            {servers.map((s) => (
              <option key={s.server_id} value={s.server_id}>{s.name}</option>
            ))}
          </select>
        </div>

        {/* test button */}
        <button
          onClick={runTest}
          disabled={!selected || testing}
          className="w-full flex items-center justify-center gap-2 rounded-md bg-caliber-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-caliber-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {testing ? (
            <>
              <svg className="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              Testing…
            </>
          ) : (
            <>
              <svg className="w-4 h-4" viewBox="0 0 20 20" fill="currentColor">
                <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM9.555 7.168A1 1 0 008 8v4a1 1 0 001.555.832l3-2a1 1 0 000-1.664l-3-2z" clipRule="evenodd" />
              </svg>
              Test Connection
            </>
          )}
        </button>

        {/* test result card */}
        {testResult && (
          <div className={`rounded-lg border p-4 ${testResult.success ? "bg-emerald-50 border-emerald-200" : "bg-red-50 border-red-200"}`}>
            <div className="flex items-center gap-2 mb-2">
              {testResult.success ? (
                <svg className="w-5 h-5 text-emerald-600" viewBox="0 0 20 20" fill="currentColor">
                  <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clipRule="evenodd" />
                </svg>
              ) : (
                <svg className="w-5 h-5 text-red-600" viewBox="0 0 20 20" fill="currentColor">
                  <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clipRule="evenodd" />
                </svg>
              )}
              <span className={`text-sm font-semibold ${testResult.success ? "text-emerald-800" : "text-red-800"}`}>
                {testResult.success ? "Connection Successful" : "Connection Failed"}
              </span>
            </div>
            {testDuration !== null && (
              <p className={`text-xs ${testResult.success ? "text-emerald-600" : "text-red-600"} mb-1`}>
                Response time: {testDuration}ms
              </p>
            )}
            {testResult.error && (
              <p className="text-xs text-red-700 mt-1 font-mono bg-red-100 rounded px-2 py-1">{testResult.error}</p>
            )}
            {testResult.success && (
              <p className="text-xs text-emerald-700">
                {testResult.tools.length} tool{testResult.tools.length !== 1 ? "s" : ""} discovered
              </p>
            )}
          </div>
        )}

        {/* server details */}
        {selected && (
          <div className="rounded-lg border border-zinc-200 bg-white p-4 space-y-3">
            <h3 className="text-xs font-semibold text-zinc-800 uppercase tracking-wider">Server Details</h3>
            <dl className="space-y-2 text-xs">
              <div className="flex justify-between">
                <dt className="text-zinc-500">Status</dt>
                <dd>
                  {badge && (
                    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-[10px] font-medium ${badge.bg}`}>
                      <span className={`w-1.5 h-1.5 rounded-full ${badge.dot}`} />
                      {badge.label}
                    </span>
                  )}
                </dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-zinc-500">Transport</dt>
                <dd className="font-mono text-zinc-800">{selected.transport}</dd>
              </div>
              {selected.transport === "stdio" && (
                <div className="flex justify-between">
                  <dt className="text-zinc-500">Command</dt>
                  <dd className="font-mono text-zinc-800 text-right max-w-[60%] truncate" title={`${selected.command} ${selected.args.join(" ")}`}>
                    {selected.command}
                  </dd>
                </div>
              )}
              {(selected.transport === "sse" || selected.transport === "streamable-http") && (
                <div className="flex justify-between">
                  <dt className="text-zinc-500">URI</dt>
                  <dd className="font-mono text-zinc-800 text-right max-w-[60%] truncate" title={selected.uri}>
                    {selected.uri}
                  </dd>
                </div>
              )}
              <div className="flex justify-between">
                <dt className="text-zinc-500">Auth</dt>
                <dd className="text-zinc-800">{selected.auth_type}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-zinc-500">Last Connected</dt>
                <dd className="text-zinc-800">{formatDate(selected.last_connected_at)}</dd>
              </div>
            </dl>
          </div>
        )}
      </div>

      {/* ── MIDDLE COLUMN: tools list ──────────────── */}
      <div className="lg:col-span-4">
        <ToolsPanel
          tools={tools}
          activeTool={activeTool}
          onSelectTool={handleToolSelect}
          loading={toolsLoading}
          error={toolsError}
        />
      </div>

      {/* ── RIGHT COLUMN: tool invocation ─────────── */}
      <div className="lg:col-span-5 space-y-4">
        {activeTool ? (
          <>
            {selected && (
              <ToolPolicyEditor
                serverId={selected.server_id}
                tool={activeTool}
                onUpdated={(policy) => {
                  const toolName = activeTool.name;
                  setDiscoveredTools((current) =>
                    current.map((item) => (item.name === toolName ? { ...item, policy } : item)),
                  );
                  setActiveTool((current) =>
                    current && current.name === toolName ? { ...current, policy } : current,
                  );
                }}
              />
            )}
            <ToolInvoker
              tool={activeTool}
              toolArgs={toolArgs}
              setToolArgs={setToolArgs}
              invoking={invoking}
              invocationResult={invocationResult}
              onInvoke={handleInvoke}
            />
            <McpToolCalibration server={selected} tool={activeTool} refresh={refresh} />
            <McpToolTests server={selected} tool={activeTool} />
          </>
        ) : (
          <div className="rounded-lg border border-dashed border-zinc-300 bg-zinc-50 p-12 text-center">
            <svg className="w-10 h-10 text-zinc-300 mx-auto mb-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
              <path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z" />
            </svg>
            <p className="text-sm text-zinc-500 font-medium">Select a tool to invoke</p>
            <p className="text-xs text-zinc-400 mt-1">
              {tools.length === 0
                ? "Run Test Connection first to discover tools"
                : "Click a tool from the list to configure and run it"
              }
            </p>
          </div>
        )}

        {/* invocation history */}
        {invocationHistory.length > 0 && (
          <div className="rounded-lg border border-zinc-200 bg-white">
            <div className="px-4 py-3 border-b border-zinc-100 flex items-center justify-between">
              <h3 className="text-xs font-semibold text-zinc-800 uppercase tracking-wider">History</h3>
              <button
                onClick={() => setInvocationHistory([])}
                className="text-[10px] text-zinc-400 hover:text-zinc-600"
              >
                Clear
              </button>
            </div>
            <div className="divide-y divide-zinc-100 max-h-48 overflow-y-auto">
              {invocationHistory.map((entry, i) => (
                <div
                  key={`${entry.tool}-${entry.timestamp}-${i}`}
                  className="px-4 py-2.5 flex items-center justify-between text-xs cursor-pointer hover:bg-zinc-50"
                  onClick={() => {
                    const tool = tools.find((t) => t.name === entry.tool);
                    if (tool) {
                      setActiveTool(tool);
                      setToolArgs(entry.args);
                      setInvocationResult(entry.result);
                    }
                  }}
                >
                  <div className="flex items-center gap-2">
                    <span className={`w-1.5 h-1.5 rounded-full ${entry.result.success ? "bg-emerald-500" : "bg-red-500"}`} />
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

/* ── Interactive tools panel ──────────────────────────────────────────── */

function ToolsPanel({
  tools,
  activeTool,
  onSelectTool,
  loading,
  error,
}: {
  tools: McpDiscoveredToolWithPolicy[];
  activeTool: McpDiscoveredToolWithPolicy | null;
  onSelectTool: (tool: McpDiscoveredToolWithPolicy) => void;
  loading: boolean;
  error: string | null;
}): JSX.Element {
  const [filter, setFilter] = useState("");
  const filtered = filter
    ? tools.filter(
        (t) =>
          t.name.toLowerCase().includes(filter.toLowerCase()) ||
          t.description.toLowerCase().includes(filter.toLowerCase()),
      )
    : tools;

  return (
    <div className="rounded-lg border border-zinc-200 bg-white">
      <div className="flex items-center justify-between px-4 py-3 border-b border-zinc-100">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-semibold text-zinc-800">Tools</h3>
          <span className="inline-flex items-center rounded-full bg-zinc-100 px-2 py-0.5 text-[10px] font-medium text-zinc-600">
            {tools.length}
          </span>
        </div>
        {tools.length > 3 && (
          <input
            type="text"
            placeholder="Filter…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="w-36 rounded-md border border-zinc-200 bg-zinc-50 px-2.5 py-1.5 text-xs focus:border-caliber-400 focus:ring-1 focus:ring-caliber-400 outline-none"
          />
        )}
      </div>

      {error && (
        <div className="mx-4 mt-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-700">
          Could not load policies. Showing discovered tools only.
        </div>
      )}

      {loading ? (
        <div className="px-4 py-10 text-center text-xs text-zinc-400">Loading tool policies…</div>
      ) : tools.length === 0 ? (
        <div className="px-4 py-10 text-center">
          <svg className="w-8 h-8 text-zinc-300 mx-auto mb-2" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
            <path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z" />
          </svg>
          <p className="text-xs text-zinc-400">
            Click <strong>Test Connection</strong> to discover tools.
          </p>
        </div>
      ) : filtered.length === 0 ? (
        <div className="px-4 py-8 text-center">
          <p className="text-xs text-zinc-400">No tools match &ldquo;{filter}&rdquo;</p>
        </div>
      ) : (
        <div className="divide-y divide-zinc-100 max-h-[65vh] overflow-y-auto">
          {filtered.map((tool, i) => {
            const isActive = activeTool?.name === tool.name;
            const paramCount = Object.keys(tool.input_schema?.properties ?? {}).length;
            return (
              <button
                key={`${tool.name}-${i}`}
                type="button"
                onClick={() => onSelectTool(tool)}
                className={`w-full px-4 py-3 text-left transition-colors ${
                  isActive
                    ? "bg-blue-50 border-l-2 border-caliber-500"
                    : "hover:bg-zinc-50 border-l-2 border-transparent"
                }`}
              >
                <div className="flex items-start gap-3">
                  <div className={`mt-0.5 flex-shrink-0 w-7 h-7 rounded-md flex items-center justify-center ${
                    isActive ? "bg-blue-100 border border-blue-200" : "bg-zinc-100 border border-zinc-200"
                  }`}>
                    <svg className={`w-3.5 h-3.5 ${isActive ? "text-blue-600" : "text-zinc-500"}`} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                      <path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z" />
                    </svg>
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className={`text-sm font-medium font-mono ${isActive ? "text-blue-800" : "text-zinc-900"}`}>{tool.name}</p>
                      {paramCount > 0 && (
                        <span className="text-[9px] text-zinc-400 bg-zinc-100 rounded px-1 py-0.5">
                          {paramCount} param{paramCount !== 1 ? "s" : ""}
                        </span>
                      )}
                      {!tool.policy.allowed && (
                        <span className="text-[9px] font-semibold text-red-700 bg-red-50 rounded px-1.5 py-0.5">Blocked</span>
                      )}
                      {tool.policy.requires_approval && (
                        <span className="text-[9px] font-semibold text-amber-700 bg-amber-50 rounded px-1.5 py-0.5">Approval</span>
                      )}
                      <span className="text-[9px] font-semibold text-zinc-600 bg-zinc-100 rounded px-1.5 py-0.5 uppercase">
                        {tool.policy.side_effect_level}
                      </span>
                    </div>
                    <p className="text-xs text-zinc-500 mt-0.5">{tool.description}</p>
                  </div>
                </div>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ToolPolicyEditor({
  serverId,
  tool,
  onUpdated,
}: {
  serverId: string;
  tool: McpDiscoveredToolWithPolicy;
  onUpdated: (policy: McpToolPolicy) => void;
}): JSX.Element {
  const [policy, setPolicy] = useState<McpToolPolicy>(tool.policy);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    setPolicy(tool.policy);
    setSaving(false);
    setError(null);
  }, [tool.policy]);

  useEffect(() => {
    setSaved(false);
  }, [tool.name]);

  const dirty = (
    policy.allowed !== tool.policy.allowed
    || policy.side_effect_level !== tool.policy.side_effect_level
    || policy.requires_approval !== tool.policy.requires_approval
    || (policy.rate_limit_per_minute ?? null) !== (tool.policy.rate_limit_per_minute ?? null)
  );

  const save = async (): Promise<void> => {
    if (!dirty) return;
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const updated = await caliberApi.updateMcpToolPolicy(serverId, tool.name, {
        allowed: policy.allowed,
        side_effect_level: policy.side_effect_level,
        requires_approval: policy.requires_approval,
        rate_limit_per_minute: policy.rate_limit_per_minute ?? null,
      });
      setPolicy(updated.policy);
      onUpdated(updated.policy);
      setSaved(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update MCP tool policy");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="rounded-lg border border-zinc-200 bg-white p-4 space-y-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-zinc-900">Tool Policy</h3>
          <p className="text-xs text-zinc-500 font-mono">{tool.name}</p>
        </div>
        <button
          type="button"
          disabled={!dirty || saving}
          onClick={() => void save()}
          className="rounded-md bg-caliber-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-caliber-700 disabled:opacity-50"
        >
          {saving ? "Saving…" : "Save Policy"}
        </button>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <label className="flex items-center justify-between rounded-md border border-zinc-200 bg-zinc-50 px-3 py-2 text-xs text-zinc-700">
          <span className="font-medium">Allow tool</span>
          <input
            type="checkbox"
            checked={policy.allowed}
            onChange={(event) => {
              setPolicy((current) => ({ ...current, allowed: event.target.checked }));
              setSaved(false);
            }}
            className="rounded border-zinc-300"
          />
        </label>
        <label className="flex items-center justify-between rounded-md border border-zinc-200 bg-zinc-50 px-3 py-2 text-xs text-zinc-700">
          <span className="font-medium">Requires approval</span>
          <input
            type="checkbox"
            checked={policy.requires_approval}
            onChange={(event) => {
              setPolicy((current) => ({ ...current, requires_approval: event.target.checked }));
              setSaved(false);
            }}
            className="rounded border-zinc-300"
          />
        </label>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <label className="block text-xs">
          <span className="mb-1 block font-medium text-zinc-700">Side effect level</span>
          <select
            value={policy.side_effect_level}
            onChange={(event) => {
              const side_effect_level = event.target.value as McpToolPolicy["side_effect_level"];
              setPolicy((current) => ({ ...current, side_effect_level }));
              setSaved(false);
            }}
            className="w-full rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-xs text-zinc-800 focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500 outline-none"
          >
            <option value="read">read</option>
            <option value="write">write</option>
            <option value="external_action">external_action</option>
          </select>
        </label>
        <label className="block text-xs">
          <span className="mb-1 block font-medium text-zinc-700">Rate limit / minute</span>
          <input
            type="number"
            min={1}
            value={policy.rate_limit_per_minute ?? ""}
            placeholder="unlimited"
            onChange={(event) => {
              const raw = event.target.value.trim();
              const parsed = raw ? Number.parseInt(raw, 10) : null;
              setPolicy((current) => ({
                ...current,
                rate_limit_per_minute: parsed && Number.isFinite(parsed) && parsed > 0 ? parsed : null,
              }));
              setSaved(false);
            }}
            className="w-full rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-xs text-zinc-800 focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500 outline-none"
          />
        </label>
      </div>

      {error && (
        <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          {error}
        </div>
      )}
      {saved && !error && (
        <div className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-700">
          Policy saved.
        </div>
      )}
    </div>
  );
}

/* ── MCP tool generated tests ─────────────────────────────────────────── */

interface McpGeneratedTestCase {
  id: string;
  input: Record<string, unknown>;
  expectedOutput: unknown;
  expectedBehavior: string;
  tags: string[];
}

interface McpGeneratedTestResult {
  testCaseId: string;
  actualOutput: unknown;
  error: string | null;
  verdict: "pass" | "fail" | "partial";
  score: number;
  reasoning: string;
  durationMs: number;
}

function parseJsonArrayFromAssistant(raw: string): unknown[] {
  const match = raw.match(/\[[\s\S]*\]/);
  if (!match) throw new Error("LLM did not return a JSON array.");
  const parsed = JSON.parse(match[0]) as unknown;
  if (!Array.isArray(parsed)) throw new Error("Generated tests must be an array.");
  return parsed;
}

function parseJsonObjectFromAssistant(raw: string): Record<string, unknown> | null {
  const match = raw.match(/\{[\s\S]*\}/);
  if (!match) return null;
  const parsed = JSON.parse(match[0]) as unknown;
  return parsed && typeof parsed === "object" && !Array.isArray(parsed)
    ? parsed as Record<string, unknown>
    : null;
}

function normalizeMcpGeneratedTests(raw: unknown[]): McpGeneratedTestCase[] {
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
    return {
      id: `mcp-tc-${Date.now()}-${index}`,
      input: input as Record<string, unknown>,
      expectedOutput: payload.expectedOutput ?? payload.output ?? {},
      expectedBehavior,
      tags: Array.isArray(payload.tags)
        ? payload.tags.filter((tag): tag is string => typeof tag === "string" && tag.trim().length > 0)
        : [],
    };
  });
}

function McpToolCalibration({
  server,
  tool,
  refresh,
}: {
  server: McpServer | null;
  tool: McpServerDiscoveredTool;
  refresh: () => void;
}): JSX.Element | null {
  if (!server) return null;
  const savedCases = server.tool_test_cases?.[tool.name] ?? [];
  const lastResult = server.tool_calibrations?.[tool.name] ?? null;
  return (
    <div className="rounded-lg border border-zinc-200 bg-white p-4">
      <CalibrationPanel
        key={`${server.server_id}:${tool.name}`}
        idPrefix="mcp"
        calibrateTestId="mcp-calibrate-btn"
        initialCases={savedCases}
        lastResult={lastResult}
        onSave={async (cases: CalibrationCase[]) => {
          const saved = await caliberApi.saveMcpToolTestCases(server.server_id, tool.name, cases);
          refresh();
          return saved;
        }}
        onCalibrate={async (): Promise<CalibrationResult> => {
          const scored = await caliberApi.calibrateMcpTool(server.server_id, tool.name);
          refresh();
          return scored;
        }}
      />
    </div>
  );
}

function McpToolTests({
  server,
  tool,
}: {
  server: McpServer | null;
  tool: McpServerDiscoveredTool;
}): JSX.Element {
  const [config, setConfig] = useState<AssistantConfig | null>(null);
  const [selectedModel, setSelectedModel] = useState("");
  const [configLoading, setConfigLoading] = useState(true);
  const [count, setCount] = useState(5);
  const [cases, setCases] = useState<McpGeneratedTestCase[]>([]);
  const [results, setResults] = useState<McpGeneratedTestResult[]>([]);
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

  useEffect(() => {
    setCases([]);
    setResults([]);
    setError(null);
  }, [server?.server_id, tool.name]);

  const setCountClamped = (next: number): void => {
    const rounded = Number.isFinite(next) ? Math.round(next) : 1;
    setCount(Math.max(1, Math.min(50, rounded)));
  };

  const syncModel = async (): Promise<void> => {
    if (config && selectedModel && selectedModel !== config.model) {
      const updated = await caliberApi.updateAssistantConfig({ model: selectedModel });
      setConfig(updated);
    }
  };

  const generate = async (): Promise<void> => {
    if (!server) return;
    setGenerating(true);
    setError(null);
    try {
      await syncModel();
      const goal = [
        `Generate exactly ${count} MCP tool tests for "${tool.name}" on server "${server.name}".`,
        `The tests must include input data and expected output data suitable for validating this MCP tool.`,
        ``,
        `## Tool Description`,
        tool.description || "(none)",
        ``,
        `## Input Schema`,
        JSON.stringify(tool.input_schema ?? {}, null, 2),
        ``,
        `## Output Schema`,
        JSON.stringify(tool.output_schema ?? {}, null, 2),
        ``,
        `Respond with ONLY a JSON array. Each item must include "input", "expectedOutput", "expectedBehavior", and "tags".`,
      ].join("\n");
      const session = await caliberApi.createAssistantSession({
        title: `MCP Tool Tests: ${tool.name}`,
        goal,
        artifact_type: "mcp_server",
      });
      const turn = await caliberApi.sendAssistantMessage(session.session_id, {
        content: "Generate the MCP tool tests now.",
        artifact_type: "mcp_server",
      });
      setCases(normalizeMcpGeneratedTests(parseJsonArrayFromAssistant(turn.assistant_message.content)));
      setResults([]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to generate MCP tests");
    } finally {
      setGenerating(false);
    }
  };

  const run = async (): Promise<void> => {
    if (!server || cases.length === 0) return;
    setRunning(true);
    setError(null);
    setResults([]);
    setProgress({ current: 0, total: cases.length });
    try {
      await syncModel();
      const nextResults: McpGeneratedTestResult[] = [];
      for (let i = 0; i < cases.length; i += 1) {
        const testCase = cases[i]!;
        setProgress({ current: i + 1, total: cases.length });
        let invocation: McpToolInvocationResult;
        try {
          invocation = await caliberApi.invokeMcpTool(server.server_id, tool.name, testCase.input);
        } catch (err) {
          invocation = {
            server_id: server.server_id,
            tool_name: tool.name,
            success: false,
            error: err instanceof Error ? err.message : "MCP invocation failed",
            result: null,
            duration_ms: 0,
          };
        }

        let verdict: McpGeneratedTestResult["verdict"] = invocation.success ? "partial" : "fail";
        let score = invocation.success ? 0.5 : 0;
        let reasoning = invocation.error ?? "Judge response was not available.";
        if (invocation.success) {
          try {
            const judgeGoal = [
              `Judge whether an MCP tool test passed.`,
              `Server: ${server.name}`,
              `Tool: ${tool.name}`,
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
              JSON.stringify(invocation.result, null, 2),
              ``,
              `Respond with ONLY JSON: {"verdict":"pass"|"fail"|"partial","score":0.0-1.0,"reasoning":"brief explanation"}`,
            ].join("\n");
            const session = await caliberApi.createAssistantSession({
              title: `Judge MCP Tool Test: ${tool.name} #${i + 1}`,
              goal: judgeGoal,
              artifact_type: "mcp_server",
            });
            const turn = await caliberApi.sendAssistantMessage(session.session_id, {
              content: "Judge this MCP test now.",
              artifact_type: "mcp_server",
            });
            const judged = parseJsonObjectFromAssistant(turn.assistant_message.content);
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
          actualOutput: invocation.result,
          error: invocation.error,
          verdict,
          score,
          reasoning,
          durationMs: invocation.duration_ms,
        });
        setResults([...nextResults]);
      }
    } finally {
      setRunning(false);
    }
  };

  const averageScore = results.length
    ? results.reduce((sum, item) => sum + item.score, 0) / results.length
    : null;

  return (
    <div data-testid="mcp-tool-tests" className="rounded-lg border border-zinc-200 bg-white p-4 space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-zinc-900">Generated Tool Tests</h3>
          <p className="mt-0.5 text-xs text-zinc-500">LLM-generated input and expected-output cases for this MCP tool.</p>
        </div>
        {averageScore !== null && (
          <span className="rounded-full bg-zinc-100 px-2 py-0.5 text-[10px] font-semibold text-zinc-700">
            {(averageScore * 100).toFixed(0)}% avg
          </span>
        )}
      </div>

      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <label className="block">
          <span className="block text-xs font-medium text-zinc-700 mb-1">LLM Model</span>
          {configLoading ? (
            <div className="text-xs text-zinc-400 py-2">Loading models…</div>
          ) : (
            <select
              aria-label="Select MCP test model"
              value={selectedModel}
              onChange={(event) => setSelectedModel(event.target.value)}
              disabled={generating || running}
              className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500 outline-none disabled:opacity-50"
            >
              {config?.available_models.map((model: AssistantModelOption) => (
                <option key={model.id} value={model.id}>{model.name} ({model.provider})</option>
              ))}
            </select>
          )}
        </label>
        <label className="block">
          <span className="block text-xs font-medium text-zinc-700 mb-1">Number of Tests</span>
          <input
            aria-label="Number of MCP tool tests"
            type="number"
            min={1}
            max={50}
            value={count}
            disabled={generating || running}
            onChange={(event) => setCountClamped(Number(event.target.value))}
            className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500 outline-none disabled:opacity-50"
          />
        </label>
      </div>

      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          data-testid="mcp-tests-generate"
          onClick={() => void generate()}
          disabled={!server || generating || running || configLoading}
          className="rounded-md bg-caliber-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-caliber-700 disabled:opacity-50"
        >
          {generating ? "Generating…" : "Generate Tests"}
        </button>
        <button
          type="button"
          data-testid="mcp-tests-run"
          onClick={() => void run()}
          disabled={cases.length === 0 || generating || running}
          className="rounded-md bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
        >
          {running ? `Running ${progress.current}/${progress.total}…` : "Run Tests"}
        </button>
      </div>

      {error && (
        <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          {error}
        </div>
      )}

      {cases.length === 0 ? (
        <div className="rounded-md border border-dashed border-zinc-300 bg-zinc-50 p-5 text-center">
          <p className="text-xs text-zinc-500">No generated MCP tests yet.</p>
        </div>
      ) : (
        <div className="space-y-2">
          {cases.map((testCase, index) => {
            const result = results.find((item) => item.testCaseId === testCase.id);
            return (
              <div key={testCase.id} className="rounded-md border border-zinc-200 bg-zinc-50 p-3">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="text-xs font-semibold text-zinc-900">Test {index + 1}</div>
                    <div className="mt-0.5 text-[11px] text-zinc-500">{testCase.expectedBehavior || "Output assertion"}</div>
                  </div>
                  {result && (
                    <span className={`rounded-full px-1.5 py-0.5 text-[9px] font-semibold ${
                      result.verdict === "pass"
                        ? "bg-emerald-50 text-emerald-700"
                        : result.verdict === "partial"
                          ? "bg-amber-50 text-amber-700"
                          : "bg-red-50 text-red-700"
                    }`}>
                      {result.verdict} {(result.score * 100).toFixed(0)}%
                    </span>
                  )}
                </div>
                <div className="mt-2 grid gap-2">
                  <pre className="max-h-32 overflow-auto rounded bg-white p-2 text-[10px] text-zinc-700">
                    {JSON.stringify({ input: testCase.input, expectedOutput: testCase.expectedOutput }, null, 2)}
                  </pre>
                  {result && (
                    <pre className="max-h-32 overflow-auto rounded bg-white p-2 text-[10px] text-zinc-700">
                      {JSON.stringify({ actualOutput: result.actualOutput, error: result.error, reasoning: result.reasoning }, null, 2)}
                    </pre>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/* ── Tool invoker ────────────────────────────────────────────────────── */

function ToolInvoker({
  tool,
  toolArgs,
  setToolArgs,
  invoking,
  invocationResult,
  onInvoke,
}: {
  tool: McpServerDiscoveredTool;
  toolArgs: Record<string, string>;
  setToolArgs: React.Dispatch<React.SetStateAction<Record<string, string>>>;
  invoking: boolean;
  invocationResult: McpToolInvocationResult | null;
  onInvoke: () => void;
}): JSX.Element {
  const schema = tool.input_schema;
  const properties = schema?.properties ?? {};
  const required = new Set(schema?.required ?? []);
  const paramKeys = Object.keys(properties);

  return (
    <div className="space-y-4">
      {/* tool header */}
      <div className="rounded-lg border border-zinc-200 bg-white p-4">
        <div className="flex items-center gap-3 mb-3">
          <div className="w-8 h-8 rounded-lg bg-blue-50 border border-blue-100 flex items-center justify-center">
            <svg className="w-4 h-4 text-blue-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z" />
            </svg>
          </div>
          <div>
            <h3 className="text-sm font-semibold text-zinc-900 font-mono">{tool.name}</h3>
            <p className="text-xs text-zinc-500">{tool.description}</p>
          </div>
        </div>

        <div className="mb-4 grid gap-3 md:grid-cols-2">
          <ToolSignature
            title="Input Signature"
            schema={tool.input_schema ?? null}
            emptyLabel="No input schema reported."
            testId="mcp-tool-input-signature"
          />
          <ToolSignature
            title="Output Signature"
            schema={tool.output_schema ?? null}
            emptyLabel="No output schema reported by this MCP server."
            testId="mcp-tool-output-signature"
          />
        </div>

        {/* parameter inputs */}
        {paramKeys.length > 0 ? (
          <div className="space-y-3">
            <h4 className="text-xs font-medium text-zinc-600 uppercase tracking-wider">Parameters</h4>
            {paramKeys.map((key) => {
              const prop = properties[key];
              const isRequired = required.has(key);
              return (
                <div key={key}>
                  <label className="flex items-baseline gap-1 text-xs font-medium text-zinc-700 mb-1">
                    <span className="font-mono">{key}</span>
                    {isRequired && <span className="text-red-400">*</span>}
                    <span className="text-zinc-400 font-normal ml-1">({prop?.type ?? "string"})</span>
                  </label>
                  {prop?.description && (
                    <p className="text-[10px] text-zinc-400 mb-1">{prop.description}</p>
                  )}
                  <input
                    type="text"
                    value={toolArgs[key] ?? ""}
                    onChange={(e) => setToolArgs((prev) => ({ ...prev, [key]: e.target.value }))}
                    placeholder={prop?.description ?? key}
                    className="w-full rounded-md border border-zinc-300 bg-white px-3 py-1.5 text-sm font-mono focus:border-caliber-400 focus:ring-1 focus:ring-caliber-400 outline-none"
                  />
                </div>
              );
            })}
          </div>
        ) : (
          <p className="text-xs text-zinc-400 italic">No parameters required</p>
        )}

        {/* invoke button */}
        <button
          onClick={onInvoke}
          disabled={invoking}
          className="mt-4 w-full flex items-center justify-center gap-2 rounded-md bg-emerald-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-emerald-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {invoking ? (
            <>
              <svg className="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              Invoking…
            </>
          ) : (
            <>
              <svg className="w-4 h-4" viewBox="0 0 20 20" fill="currentColor">
                <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM9.555 7.168A1 1 0 008 8v4a1 1 0 001.555.832l3-2a1 1 0 000-1.664l-3-2z" clipRule="evenodd" />
              </svg>
              Invoke Tool
            </>
          )}
        </button>
      </div>

      {/* result card */}
      {invocationResult && (
        <div className={`rounded-lg border ${invocationResult.success ? "border-emerald-200" : "border-red-200"}`}>
          <div className={`px-4 py-3 border-b ${invocationResult.success ? "bg-emerald-50 border-emerald-200" : "bg-red-50 border-red-200"} rounded-t-lg`}>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                {invocationResult.success ? (
                  <svg className="w-4 h-4 text-emerald-600" viewBox="0 0 20 20" fill="currentColor">
                    <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clipRule="evenodd" />
                  </svg>
                ) : (
                  <svg className="w-4 h-4 text-red-600" viewBox="0 0 20 20" fill="currentColor">
                    <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clipRule="evenodd" />
                  </svg>
                )}
                <span className={`text-xs font-semibold ${invocationResult.success ? "text-emerald-800" : "text-red-800"}`}>
                  {invocationResult.success ? "Success" : "Failed"}
                </span>
              </div>
              <span className={`text-[10px] font-mono ${invocationResult.success ? "text-emerald-600" : "text-red-600"}`}>
                {invocationResult.duration_ms}ms
              </span>
            </div>
            {invocationResult.error && (
              <p className="text-xs text-red-700 mt-1 font-mono">{invocationResult.error}</p>
            )}
          </div>
          {invocationResult.result != null && (
            <pre className="px-4 py-3 text-[11px] text-zinc-700 font-mono overflow-x-auto bg-white rounded-b-lg max-h-72 overflow-y-auto whitespace-pre-wrap break-words">
              {JSON.stringify(invocationResult.result, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
