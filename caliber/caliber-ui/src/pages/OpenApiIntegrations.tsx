/**
 * OpenAPI Integrations — governed import, curation, and publication of
 * third-party OpenAPI contracts.
 *
 * The pipeline is deliberately staged: create an integration shell, pin an
 * imported spec version, review its normalized operations and detected
 * dependencies, generate curated tool drafts from selected operations, then
 * publish an approved draft into CALIBER's governed tool registry. Importing
 * a spec never creates a runtime tool by itself.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, caliberApi } from "@/api/caliberApi";
import type {
  OpenApiAuthBinding,
  OpenApiGraphNode,
  OpenApiGraphSnapshot,
  OpenApiIntegration,
  OpenApiOperation,
  OpenApiOperationDependency,
  OpenApiToolDraft,
} from "@/api/workflowTypes";
import { PageHeader } from "@/components/PageHeader";
import { PageTabs, type PageTab } from "@/components/PageTabs";
import { useApi } from "@/hooks/useApi";
import { relativeTime } from "@/lib/time";

export function OpenApiIntegrations(): JSX.Element {
  const [openIntegrationId, setOpenIntegrationId] = useState<string | null>(null);

  if (openIntegrationId) {
    return (
      <IntegrationDetail
        integrationId={openIntegrationId}
        onBack={() => setOpenIntegrationId(null)}
      />
    );
  }
  return <IntegrationList onOpen={setOpenIntegrationId} />;
}

/* -------------------------------------------------------------------------- */
/* List + create                                                              */
/* -------------------------------------------------------------------------- */

const STATUS_PILL: Record<string, string> = {
  draft: "bg-slate-100 text-slate-600",
  review: "bg-amber-100 text-amber-700",
  ready: "bg-blue-100 text-blue-700",
  published: "bg-emerald-100 text-emerald-700",
  archived: "bg-slate-100 text-slate-400",
};

function StatusPill({ status }: { status: string }): JSX.Element {
  return (
    <span
      className={`text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded ${
        STATUS_PILL[status] ?? "bg-slate-100 text-slate-600"
      }`}
    >
      {status}
    </span>
  );
}

function IntegrationList({ onOpen }: { onOpen: (id: string) => void }): JSX.Element {
  const fetcher = useCallback(
    (signal: AbortSignal) => caliberApi.listOpenApiIntegrations(undefined, signal),
    [],
  );
  const { data, error, loading, refresh } = useApi(fetcher, []);
  const [showCreate, setShowCreate] = useState(false);

  return (
    <div className="openapi-integrations-page">
      <PageHeader
        title="OpenAPI Integrations"
        subtitle="Import a third-party OpenAPI spec, curate which operations become agent-facing tools, and publish them into CALIBER's governed tool registry."
        actions={
          <button
            type="button"
            onClick={() => setShowCreate((v) => !v)}
            className="text-sm font-medium text-white bg-caliber-purple px-3 py-1.5 rounded-md hover:bg-caliber-purple-dark"
          >
            {showCreate ? "Cancel" : "+ New Integration"}
          </button>
        }
      />

      {error && (
        <div className="mb-4 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          <div className="font-medium">Failed to load OpenAPI integrations</div>
          <div className="text-xs mt-0.5">{error.message}</div>
        </div>
      )}

      {showCreate && (
        <CreateIntegrationPanel
          onCancel={() => setShowCreate(false)}
          onSuccess={(integration) => {
            setShowCreate(false);
            refresh();
            onOpen(integration.integration_id);
          }}
        />
      )}

      <div className="bg-white rounded-lg border border-surface-200 overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-surface-200 bg-surface-50">
              <th className="text-left font-medium px-4 py-3">Name</th>
              <th className="text-left font-medium px-4 py-3">Status</th>
              <th className="text-left font-medium px-4 py-3">Owner</th>
              <th className="text-left font-medium px-4 py-3">Updated</th>
              <th className="text-right font-medium px-4 py-3">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-surface-100">
            {loading && !data && (
              <tr>
                <td colSpan={5} className="px-4 py-10 text-center text-sm text-gray-500">
                  Loading…
                </td>
              </tr>
            )}
            {data && data.length === 0 && (
              <tr>
                <td colSpan={5} className="px-4 py-10 text-center text-sm text-gray-500">
                  No OpenAPI integrations yet.
                </td>
              </tr>
            )}
            {(data ?? []).map((integration) => (
              <tr key={integration.integration_id} className="hover:bg-surface-50">
                <td className="px-4 py-3">
                  <button
                    type="button"
                    onClick={() => onOpen(integration.integration_id)}
                    className="font-medium text-gray-900 hover:text-caliber-purple hover:underline"
                  >
                    {integration.name}
                  </button>
                  <div className="text-xs text-gray-500 mt-0.5 max-w-md truncate">
                    {integration.description || "—"}
                  </div>
                </td>
                <td className="px-4 py-3">
                  <StatusPill status={integration.status} />
                </td>
                <td className="px-4 py-3 text-gray-600 text-xs">{integration.owner}</td>
                <td className="px-4 py-3 text-xs text-gray-500">
                  {relativeTime(integration.updated_at)}
                </td>
                <td className="px-4 py-3 text-right">
                  <button
                    type="button"
                    onClick={() => onOpen(integration.integration_id)}
                    className="text-xs font-medium text-caliber-purple hover:underline"
                  >
                    Open
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function CreateIntegrationPanel({
  onCancel,
  onSuccess,
}: {
  onCancel: () => void;
  onSuccess: (integration: OpenApiIntegration) => void;
}): JSX.Element {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (): Promise<void> => {
    setSubmitting(true);
    setError(null);
    try {
      const integration = await caliberApi.createOpenApiIntegration({
        name: name.trim(),
        description: description.trim(),
      });
      onSuccess(integration);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to create integration");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="mb-6 rounded-lg border border-surface-200 bg-white p-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="text-sm">
          <span className="block text-xs font-medium text-gray-600 mb-1">Name</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Ticketing API"
            className="w-full rounded-md border border-surface-300 px-3 py-1.5 text-sm"
          />
        </label>
        <label className="text-sm">
          <span className="block text-xs font-medium text-gray-600 mb-1">Description</span>
          <input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="External ticket API"
            className="w-full rounded-md border border-surface-300 px-3 py-1.5 text-sm"
          />
        </label>
      </div>
      {error && <div className="mt-2 text-xs text-red-600">{error}</div>}
      <div className="mt-3 flex justify-end gap-2">
        <button
          type="button"
          onClick={onCancel}
          className="text-sm px-3 py-1.5 rounded-md border border-surface-300 text-gray-600 hover:bg-surface-50"
        >
          Cancel
        </button>
        <button
          type="button"
          disabled={!name.trim() || submitting}
          onClick={submit}
          className="text-sm font-medium text-white bg-caliber-purple px-3 py-1.5 rounded-md hover:bg-caliber-purple-dark disabled:opacity-50"
        >
          {submitting ? "Creating…" : "Create"}
        </button>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Detail                                                                      */
/* -------------------------------------------------------------------------- */

const DETAIL_TABS: PageTab[] = [
  { key: "import", label: "Import" },
  { key: "operations", label: "Operations" },
  { key: "dependencies", label: "Dependencies" },
  { key: "drafts", label: "Tool Drafts" },
  { key: "graph", label: "Graph" },
];

function Chevron(): JSX.Element {
  return (
    <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M9 18l6-6-6-6" />
    </svg>
  );
}

function IntegrationDetail({
  integrationId,
  onBack,
}: {
  integrationId: string;
  onBack: () => void;
}): JSX.Element {
  const [tab, setTab] = useState<string>("import");
  const fetcher = useCallback(
    (signal: AbortSignal) => caliberApi.getOpenApiIntegration(integrationId, signal),
    [integrationId],
  );
  const { data: integration, error, loading, refresh } = useApi(fetcher, [integrationId]);

  return (
    <div className="openapi-integrations-page">
      <div className="flex items-center gap-2 text-sm text-gray-500 mb-4">
        <Link to="/" className="hover:text-gray-700">
          Dashboard
        </Link>
        <Chevron />
        <button type="button" onClick={onBack} className="hover:text-gray-700">
          OpenAPI Integrations
        </button>
        <Chevron />
        <span className="text-gray-900 font-medium">{integration?.name ?? integrationId}</span>
      </div>

      {loading && !integration && <div className="text-sm text-gray-500">Loading…</div>}
      {error && (
        <div className="mb-4 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error.message}
        </div>
      )}

      {integration && (
        <>
          <div className="flex items-center justify-between mb-6">
            <div>
              <h1 className="text-xl font-semibold text-gray-900 flex items-center gap-2">
                {integration.name}
                <StatusPill status={integration.status} />
              </h1>
              <p className="text-sm text-gray-500 mt-0.5">
                {integration.description || "No description"}
              </p>
            </div>
            {integration.status !== "archived" && (
              <button
                type="button"
                onClick={async () => {
                  await caliberApi.archiveOpenApiIntegration(integrationId);
                  refresh();
                }}
                className="text-xs font-medium text-gray-500 hover:text-red-600 border border-surface-300 rounded-md px-3 py-1.5"
              >
                Archive
              </button>
            )}
          </div>

          <PageTabs tabs={DETAIL_TABS} active={tab} onChange={setTab} />

          {tab === "import" && (
            <ImportTab
              integration={integration}
              onImported={refresh}
            />
          )}
          {tab === "operations" && (
            <OperationsTab integrationId={integrationId} onDraftsGenerated={refresh} />
          )}
          {tab === "dependencies" && <DependenciesTab integrationId={integrationId} />}
          {tab === "drafts" && (
            <ToolDraftsTab integrationId={integrationId} onPublished={refresh} />
          )}
          {tab === "graph" && <GraphTab integrationId={integrationId} />}
        </>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Import tab                                                                  */
/* -------------------------------------------------------------------------- */

type SourceKind = "inline_text" | "upload" | "url";

function arrayBufferToBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  const chunkSize = 0x8000;
  let binary = "";
  for (let index = 0; index < bytes.length; index += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunkSize));
  }
  return btoa(binary);
}

function ImportTab({
  integration,
  onImported,
}: {
  integration: OpenApiIntegration;
  onImported: () => void;
}): JSX.Element {
  const [sourceKind, setSourceKind] = useState<SourceKind>("inline_text");
  const [specText, setSpecText] = useState("");
  const [specUrl, setSpecUrl] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const versionsFetcher = useCallback(
    (signal: AbortSignal) => caliberApi.listOpenApiVersions(integration.integration_id, signal),
    [integration.integration_id],
  );
  const { data: versions, refresh: refreshVersions } = useApi(versionsFetcher, [
    integration.integration_id,
  ]);

  const probeSource = async (): Promise<void> => {
    setError(null);
    setResult(null);
    try {
      const probe = await caliberApi.validateOpenApiSpecSource(integration.integration_id, {
        source_kind: "url",
        spec_url: specUrl,
      });
      setResult(
        probe.allowed && probe.reachable
          ? `Reachable (${probe.status_code ?? "ok"})`
          : `Not usable: ${probe.detail}`,
      );
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not validate source");
    }
  };

  const doImport = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      let payload;
      if (sourceKind === "url") {
        payload = { source_kind: "url" as const, spec_url: specUrl };
      } else if (sourceKind === "upload" && file) {
        const buffer = await file.arrayBuffer();
        const base64 = arrayBufferToBase64(buffer);
        payload = { source_kind: "upload" as const, spec_base64: base64, source_ref: file.name };
      } else {
        payload = { source_kind: "inline_text" as const, spec_text: specText };
      }
      const version = await caliberApi.importOpenApiSpec(integration.integration_id, payload);
      setResult(
        `Imported ${version.version_id} — ${version.operation_count} operations` +
          (version.import_warnings.length
            ? ` (${version.import_warnings.length} warning(s))`
            : ""),
      );
      setSpecText("");
      setSpecUrl("");
      setFile(null);
      refreshVersions();
      onImported();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Import failed");
    } finally {
      setBusy(false);
    }
  };

  const doReimport = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const outcome = await caliberApi.reimportOpenApiSpec(integration.integration_id);
      setResult(
        `Re-imported ${outcome.version.version_id}: +${outcome.diff.summary.added_count} ` +
          `-${outcome.diff.summary.removed_count} changed ${outcome.diff.summary.changed_count} ` +
          `(${outcome.diff.summary.breaking_count} breaking)`,
      );
      refreshVersions();
      onImported();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Reimport failed");
    } finally {
      setBusy(false);
    }
  };

  const canImport =
    !busy &&
    ((sourceKind === "inline_text" && specText.trim().length > 0) ||
      (sourceKind === "url" && specUrl.trim().length > 0) ||
      (sourceKind === "upload" && file !== null));

  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <div className="rounded-lg border border-surface-200 bg-white p-4">
        <h2 className="text-sm font-semibold text-gray-900 mb-3">Import a spec version</h2>
        <div className="flex gap-1 mb-3">
          {(["inline_text", "upload", "url"] as SourceKind[]).map((kind) => (
            <button
              key={kind}
              type="button"
              onClick={() => setSourceKind(kind)}
              className={`text-xs font-medium px-2.5 py-1 rounded-md ${
                sourceKind === kind
                  ? "bg-caliber-purple text-white"
                  : "bg-surface-100 text-gray-600 hover:bg-surface-200"
              }`}
            >
              {kind === "inline_text" ? "Paste" : kind === "upload" ? "Upload" : "URL"}
            </button>
          ))}
        </div>

        {sourceKind === "inline_text" && (
          <textarea
            value={specText}
            onChange={(e) => setSpecText(e.target.value)}
            placeholder="paste an OpenAPI 3.x document (JSON or YAML)…"
            rows={10}
            className="w-full rounded-md border border-surface-300 px-3 py-2 text-xs font-mono"
          />
        )}
        {sourceKind === "upload" && (
          <input
            type="file"
            accept=".json,.yaml,.yml"
            aria-label="OpenAPI spec file"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            className="text-sm"
          />
        )}
        {sourceKind === "url" && (
          <div className="flex gap-2">
            <input
              value={specUrl}
              onChange={(e) => setSpecUrl(e.target.value)}
              placeholder="https://example.com/openapi.json"
              className="flex-1 rounded-md border border-surface-300 px-3 py-1.5 text-sm"
            />
            <button
              type="button"
              onClick={probeSource}
              disabled={!specUrl.trim()}
              className="text-xs font-medium px-3 py-1.5 rounded-md border border-surface-300 text-gray-600 hover:bg-surface-50 disabled:opacity-50"
            >
              Check
            </button>
          </div>
        )}

        {error && <div className="mt-2 text-xs text-red-600">{error}</div>}
        {result && <div className="mt-2 text-xs text-emerald-700">{result}</div>}

        <div className="mt-3 flex gap-2">
          <button
            type="button"
            data-testid="import-submit"
            disabled={!canImport}
            onClick={doImport}
            className="text-sm font-medium text-white bg-caliber-purple px-3 py-1.5 rounded-md hover:bg-caliber-purple-dark disabled:opacity-50"
          >
            {busy ? "Importing…" : "Import"}
          </button>
          {integration.last_imported_version_id && (
            <button
              type="button"
              disabled={busy}
              onClick={doReimport}
              className="text-xs font-medium px-3 py-1.5 rounded-md border border-surface-300 text-gray-600 hover:bg-surface-50 disabled:opacity-50"
              title="Re-fetch the last imported version's URL source and diff it"
            >
              Reimport &amp; diff
            </button>
          )}
        </div>
      </div>

      <div className="rounded-lg border border-surface-200 bg-white p-4">
        <h2 className="text-sm font-semibold text-gray-900 mb-3">Imported versions</h2>
        <div className="space-y-2">
          {(versions ?? []).map((version) => (
            <div key={version.version_id} className="border border-surface-100 rounded-md p-2.5 text-xs">
              <div className="flex items-center justify-between">
                <span className="font-medium text-gray-900">{version.version_id}</span>
                <span className="text-gray-400">{relativeTime(version.created_at)}</span>
              </div>
              <div className="text-gray-500 mt-0.5">
                {version.title || "untitled"} · {version.operation_count} operations ·{" "}
                {version.source_kind}
              </div>
              {version.import_warnings.length > 0 && (
                <div className="text-amber-600 mt-1">
                  {version.import_warnings.length} import warning(s)
                </div>
              )}
            </div>
          ))}
          {versions && versions.length === 0 && (
            <div className="text-xs text-gray-400">No versions imported yet.</div>
          )}
        </div>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Operations tab                                                              */
/* -------------------------------------------------------------------------- */

const SIDE_EFFECT_COLOR: Record<string, string> = {
  read: "text-emerald-700 bg-emerald-50",
  write: "text-amber-700 bg-amber-50",
  external_action: "text-red-700 bg-red-50",
};

function OperationsTab({
  integrationId,
  onDraftsGenerated,
}: {
  integrationId: string;
  onDraftsGenerated: () => void;
}): JSX.Element {
  const fetcher = useCallback(
    (signal: AbortSignal) => caliberApi.listOpenApiOperations(integrationId, undefined, signal),
    [integrationId],
  );
  const { data, error, loading } = useApi(fetcher, [integrationId]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [groupAsPack, setGroupAsPack] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const toggle = (operationId: string): void =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(operationId)) next.delete(operationId);
      else next.add(operationId);
      return next;
    });

  const generate = async (): Promise<void> => {
    if (selected.size === 0) return;
    setGenerating(true);
    setMessage(null);
    try {
      // No explicit server_url: the server defaults to the imported version's
      // first declared server, which is right for the common single-server case.
      const drafts = await caliberApi.generateOpenApiToolDrafts(integrationId, {
        operation_ids: Array.from(selected),
        group_as_pack: groupAsPack,
      });
      setMessage(`Generated ${drafts.length} tool draft${drafts.length === 1 ? "" : "s"}.`);
      setSelected(new Set());
      onDraftsGenerated();
    } catch (err) {
      setMessage(err instanceof ApiError ? `Failed: ${err.message}` : "Failed to generate drafts");
    } finally {
      setGenerating(false);
    }
  };

  if (loading && !data) return <div className="text-sm text-gray-500">Loading…</div>;
  if (error)
    return <div className="text-sm text-red-600">{error.message}</div>;

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <div className="text-xs text-gray-500">
          {selected.size} selected
          {selected.size > 1 && (
            <label className="ml-3 inline-flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={groupAsPack}
                onChange={(e) => setGroupAsPack(e.target.checked)}
              />
              bind as one tool pack
            </label>
          )}
        </div>
        <button
          type="button"
          disabled={selected.size === 0 || generating}
          onClick={generate}
          className="text-sm font-medium text-white bg-caliber-purple px-3 py-1.5 rounded-md hover:bg-caliber-purple-dark disabled:opacity-50"
        >
          {generating ? "Generating…" : "Generate tool draft(s)"}
        </button>
      </div>
      {message && <div className="mb-3 text-xs text-gray-600">{message}</div>}

      <div className="bg-white rounded-lg border border-surface-200 overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-surface-200 bg-surface-50">
              <th className="px-4 py-3 w-8" />
              <th className="text-left font-medium px-4 py-3">Operation</th>
              <th className="text-left font-medium px-4 py-3">Summary</th>
              <th className="text-left font-medium px-4 py-3">Tags</th>
              <th className="text-left font-medium px-4 py-3">Side effect</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-surface-100">
            {(data ?? []).map((op: OpenApiOperation) => (
              <tr key={op.operation_id} className="hover:bg-surface-50">
                <td className="px-4 py-3">
                  <input
                    type="checkbox"
                    checked={selected.has(op.operation_id)}
                    onChange={() => toggle(op.operation_id)}
                  />
                </td>
                <td className="px-4 py-3 font-mono text-xs text-gray-900">{op.operation_key}</td>
                <td className="px-4 py-3 text-gray-600 text-xs">{op.summary || "—"}</td>
                <td className="px-4 py-3 text-gray-500 text-xs">{op.tags.join(", ") || "—"}</td>
                <td className="px-4 py-3">
                  <span
                    className={`text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded ${
                      SIDE_EFFECT_COLOR[op.side_effect_level] ?? "bg-slate-50 text-slate-600"
                    }`}
                  >
                    {op.side_effect_level}
                  </span>
                </td>
              </tr>
            ))}
            {data && data.length === 0 && (
              <tr>
                <td colSpan={5} className="px-4 py-10 text-center text-sm text-gray-500">
                  No operations imported yet — import a spec first.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Dependencies tab                                                            */
/* -------------------------------------------------------------------------- */

const CONFIDENCE_COLOR: Record<string, string> = {
  high: "text-emerald-700 bg-emerald-50",
  medium: "text-amber-700 bg-amber-50",
  low: "text-slate-600 bg-slate-50",
};

const DEPENDENCY_STATUS_COLOR: Record<string, string> = {
  auto_wired: "text-blue-700 bg-blue-50",
  suggested: "text-amber-700 bg-amber-50",
  advisory: "text-slate-700 bg-slate-100",
  confirmed: "text-emerald-700 bg-emerald-50",
  rejected: "text-rose-700 bg-rose-50",
};

const DEPENDENCY_TYPE_LABEL: Record<string, string> = {
  produces_identifier_for: "Produces identifier for",
  consumes_identifier_from: "Consumes identifier from",
  requires_auth: "Shares auth requirement with",
  polls: "Polls",
  paginates_to: "Paginates to",
  compensates: "Compensates",
  precondition_for: "Precondition for",
  grouped_with: "Grouped with",
};

const DEPENDENCY_STATUS_LABEL: Record<string, string> = {
  auto_wired: "Auto-wired",
  suggested: "Suggested",
  advisory: "Advisory",
  confirmed: "Confirmed",
  rejected: "Rejected",
};

type DependencyFilter =
  | "all"
  | "awaiting_review"
  | "auto_wired"
  | "confirmed"
  | "rejected";

function OperationRefCard({
  title,
  subtitle,
  sideEffect,
}: {
  title: string;
  subtitle: string;
  sideEffect: string | null;
}): JSX.Element {
  return (
    <div className="rounded-lg border border-surface-200 bg-surface-50 p-3">
      <div className="font-mono text-[11px] text-gray-900 break-words">{title}</div>
      <div className="mt-1 text-xs text-gray-500">{subtitle}</div>
      {sideEffect && (
        <span
          className={`mt-2 inline-flex text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded ${
            SIDE_EFFECT_COLOR[sideEffect] ?? "bg-slate-50 text-slate-600"
          }`}
        >
          {sideEffect}
        </span>
      )}
    </div>
  );
}

function DependenciesTab({ integrationId }: { integrationId: string }): JSX.Element {
  const dependencyFetcher = useCallback(
    (signal: AbortSignal) => caliberApi.listOpenApiDependencies(integrationId, undefined, signal),
    [integrationId],
  );
  const operationsFetcher = useCallback(
    (signal: AbortSignal) => caliberApi.listOpenApiOperations(integrationId, undefined, signal),
    [integrationId],
  );
  const {
    data: dependencies,
    error: dependencyError,
    loading: dependencyLoading,
    refresh,
  } = useApi(dependencyFetcher, [integrationId]);
  const {
    data: operations,
    error: operationError,
    loading: operationLoading,
  } = useApi(operationsFetcher, [integrationId]);
  const [filter, setFilter] = useState<DependencyFilter>("awaiting_review");

  const operationById = useMemo(
    () => new Map((operations ?? []).map((operation) => [operation.operation_id, operation])),
    [operations],
  );

  const counts = useMemo(() => {
    const rows = dependencies ?? [];
    return {
      all: rows.length,
      awaiting_review: rows.filter(
        (dependency) => dependency.status === "suggested" || dependency.status === "advisory",
      ).length,
      auto_wired: rows.filter((dependency) => dependency.status === "auto_wired").length,
      confirmed: rows.filter((dependency) => dependency.status === "confirmed").length,
      rejected: rows.filter((dependency) => dependency.status === "rejected").length,
    };
  }, [dependencies]);

  const filtered = useMemo(() => {
    const rows = dependencies ?? [];
    if (filter === "all") return rows;
    if (filter === "awaiting_review") {
      return rows.filter(
        (dependency) => dependency.status === "suggested" || dependency.status === "advisory",
      );
    }
    return rows.filter((dependency) => dependency.status === filter);
  }, [dependencies, filter]);

  const review = async (dependencyId: string, status: "confirmed" | "rejected"): Promise<void> => {
    await caliberApi.reviewOpenApiDependency(integrationId, dependencyId, { status });
    refresh();
  };

  const dependencyErrorMessage = dependencyError ?? operationError;

  if ((dependencyLoading || operationLoading) && !dependencies && !operations) {
    return <div className="text-sm text-gray-500">Loading…</div>;
  }
  if (dependencyErrorMessage) {
    return <div className="text-sm text-red-600">{dependencyErrorMessage.message}</div>;
  }

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        {[
          {
            key: "awaiting_review" as const,
            label: "Awaiting Review",
            count: counts.awaiting_review,
            tone: "border-amber-200 bg-amber-50 text-amber-900",
          },
          {
            key: "auto_wired" as const,
            label: "Auto-wired",
            count: counts.auto_wired,
            tone: "border-blue-200 bg-blue-50 text-blue-900",
          },
          {
            key: "confirmed" as const,
            label: "Confirmed",
            count: counts.confirmed,
            tone: "border-emerald-200 bg-emerald-50 text-emerald-900",
          },
          {
            key: "rejected" as const,
            label: "Rejected",
            count: counts.rejected,
            tone: "border-rose-200 bg-rose-50 text-rose-900",
          },
          {
            key: "all" as const,
            label: "All Rows",
            count: counts.all,
            tone: "border-surface-200 bg-white text-gray-900",
          },
        ].map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => setFilter(item.key)}
            className={`rounded-xl border p-4 text-left transition ${
              item.tone
            } ${filter === item.key ? "ring-2 ring-caliber-purple/30" : "hover:border-surface-300"}`}
          >
            <div className="text-[11px] font-semibold uppercase tracking-wide opacity-80">
              {item.label}
            </div>
            <div className="mt-1 text-2xl font-semibold">{item.count}</div>
          </button>
        ))}
      </div>

      <div className="rounded-xl border border-surface-200 bg-white p-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <h3 className="text-sm font-semibold text-gray-900">Dependency Review</h3>
            <p className="mt-1 text-xs text-gray-500">
              Review the detected relationship between concrete operations, not just raw row IDs.
              Auto-wired rows were promoted deterministically; suggested and advisory rows need an
              operator decision.
            </p>
          </div>
          <div className="text-xs text-gray-500">
            Showing <span className="font-medium text-gray-700">{filtered.length}</span> of{" "}
            <span className="font-medium text-gray-700">{counts.all}</span> dependency rows
          </div>
        </div>

        {filtered.length === 0 ? (
          <div className="mt-4 rounded-lg border border-dashed border-surface-300 bg-surface-50 px-4 py-10 text-center text-sm text-gray-500">
            {counts.all === 0
              ? "No dependencies detected yet."
              : "No dependency rows match the current filter."}
          </div>
        ) : (
          <div className="mt-4 space-y-3">
            {filtered.map((dependency: OpenApiOperationDependency) => {
              const fromOperation = operationById.get(dependency.from_operation_id);
              const toOperation = operationById.get(dependency.to_operation_id);
              const bindingEntries = Object.entries(dependency.binding_field_map ?? {});
              const canReview =
                dependency.status === "suggested" || dependency.status === "advisory";

              return (
                <div
                  key={dependency.dependency_id}
                  className="rounded-xl border border-surface-200 bg-surface-50/60 p-4"
                >
                  <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-semibold text-gray-900">
                          {DEPENDENCY_TYPE_LABEL[dependency.dependency_type] ??
                            dependency.dependency_type}
                        </span>
                        <span
                          className={`text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded ${
                            CONFIDENCE_COLOR[dependency.confidence] ??
                            "bg-slate-50 text-slate-600"
                          }`}
                        >
                          {dependency.confidence} confidence
                        </span>
                        <span
                          className={`text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded ${
                            DEPENDENCY_STATUS_COLOR[dependency.status] ??
                            "bg-slate-50 text-slate-600"
                          }`}
                        >
                          {DEPENDENCY_STATUS_LABEL[dependency.status] ?? dependency.status}
                        </span>
                        {dependency.required && (
                          <span className="text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded bg-gray-900 text-white">
                            Required
                          </span>
                        )}
                      </div>
                      <p className="mt-2 text-xs text-gray-600">
                        <span className="font-medium text-gray-800">
                          {fromOperation?.operation_key ?? dependency.from_operation_id}
                        </span>{" "}
                        <span className="text-gray-500">→</span>{" "}
                        <span className="font-medium text-gray-800">
                          {toOperation?.operation_key ?? dependency.to_operation_id}
                        </span>
                      </p>
                    </div>

                    {canReview && (
                      <div className="flex gap-2">
                        <button
                          type="button"
                          onClick={() => review(dependency.dependency_id, "confirmed")}
                          className="text-xs font-medium px-3 py-1.5 rounded-md bg-emerald-600 text-white hover:bg-emerald-700"
                        >
                          Confirm
                        </button>
                        <button
                          type="button"
                          onClick={() => review(dependency.dependency_id, "rejected")}
                          className="text-xs font-medium px-3 py-1.5 rounded-md border border-rose-200 text-rose-700 hover:bg-rose-50"
                        >
                          Reject
                        </button>
                      </div>
                    )}
                  </div>

                  <div className="mt-4 grid gap-3 lg:grid-cols-[1fr_auto_1fr] lg:items-stretch">
                    <OperationRefCard
                      title={fromOperation?.operation_key ?? dependency.from_operation_id}
                      subtitle={fromOperation?.summary || fromOperation?.path || "Source operation"}
                      sideEffect={fromOperation?.side_effect_level ?? null}
                    />
                    <div className="hidden lg:flex items-center justify-center text-gray-300 text-xl">
                      →
                    </div>
                    <OperationRefCard
                      title={toOperation?.operation_key ?? dependency.to_operation_id}
                      subtitle={toOperation?.summary || toOperation?.path || "Target operation"}
                      sideEffect={toOperation?.side_effect_level ?? null}
                    />
                  </div>

                  <div className="mt-4 grid gap-3 xl:grid-cols-3">
                    <div className="rounded-lg border border-surface-200 bg-white p-3">
                      <div className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                        Detector Source
                      </div>
                      <div className="mt-1 text-xs text-gray-700">{dependency.source}</div>
                    </div>
                    <div className="rounded-lg border border-surface-200 bg-white p-3">
                      <div className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                        Field Binding
                      </div>
                      <div className="mt-1 text-xs text-gray-700">
                        {bindingEntries.length > 0
                          ? bindingEntries.map(([target, source]) => `${target} ← ${source}`).join(
                              ", ",
                            )
                          : "No field-level binding recorded"}
                      </div>
                    </div>
                    <div className="rounded-lg border border-surface-200 bg-white p-3">
                      <div className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                        Notes
                      </div>
                      <div className="mt-1 text-xs text-gray-700">
                        {dependency.notes || "No additional notes"}
                      </div>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Tool drafts tab                                                             */
/* -------------------------------------------------------------------------- */

function AuthBindingForm({
  value,
  onChange,
}: {
  value: OpenApiAuthBinding;
  onChange: (binding: OpenApiAuthBinding) => void;
}): JSX.Element {
  const scopesText = (value.scopes ?? []).join(", ");
  const [scopesInput, setScopesInput] = useState(scopesText);

  useEffect(() => {
    setScopesInput(scopesText);
  }, [scopesText]);

  const setScopes = (raw: string): void => {
    setScopesInput(raw);
    onChange({
      ...value,
      scopes: raw
        .split(",")
        .map((entry) => entry.trim())
        .filter(Boolean),
    });
  };

  return (
    <div className="grid gap-2 sm:grid-cols-2">
      <select
        aria-label="Auth kind"
        value={value.kind}
        onChange={(e) => onChange({ kind: e.target.value as OpenApiAuthBinding["kind"] })}
        className="rounded-md border border-surface-300 px-2 py-1.5 text-xs"
      >
        <option value="none">No auth</option>
        <option value="bearer">Bearer token</option>
        <option value="api_key">API key</option>
        <option value="basic">Basic auth</option>
        <option value="header">Custom header</option>
        <option value="oauth_client_credentials">OAuth client credentials</option>
        <option value="oauth_refresh_token">OAuth refresh token</option>
      </select>
      {(value.kind === "bearer" || value.kind === "api_key" || value.kind === "header") && (
        <input
          value={value.secret_ref ?? ""}
          onChange={(e) => onChange({ ...value, secret_ref: e.target.value })}
          placeholder="env://MY_TOKEN or secret://name"
          className="rounded-md border border-surface-300 px-2 py-1.5 text-xs"
        />
      )}
      {value.kind === "api_key" && (
        <>
          <input
            value={value.header_name ?? ""}
            onChange={(e) => onChange({ ...value, header_name: e.target.value })}
            placeholder="X-API-Key header name"
            className="rounded-md border border-surface-300 px-2 py-1.5 text-xs"
          />
          <input
            value={value.query_param_name ?? ""}
            onChange={(e) => onChange({ ...value, query_param_name: e.target.value })}
            placeholder="api_key query param (optional)"
            className="rounded-md border border-surface-300 px-2 py-1.5 text-xs"
          />
        </>
      )}
      {value.kind === "header" && (
        <>
          <input
            value={value.header_name ?? ""}
            onChange={(e) => onChange({ ...value, header_name: e.target.value })}
            placeholder="header name"
            className="rounded-md border border-surface-300 px-2 py-1.5 text-xs"
          />
          <input
            value={value.prefix ?? ""}
            onChange={(e) => onChange({ ...value, prefix: e.target.value })}
            placeholder="prefix (optional)"
            className="rounded-md border border-surface-300 px-2 py-1.5 text-xs"
          />
        </>
      )}
      {value.kind === "bearer" && (
        <input
          value={value.prefix ?? ""}
          onChange={(e) => onChange({ ...value, prefix: e.target.value })}
          placeholder="Bearer prefix (optional)"
          className="rounded-md border border-surface-300 px-2 py-1.5 text-xs"
        />
      )}
      {value.kind === "basic" && (
        <>
          <input
            value={value.username ?? ""}
            onChange={(e) => onChange({ ...value, username: e.target.value })}
            placeholder="username"
            className="rounded-md border border-surface-300 px-2 py-1.5 text-xs"
          />
          <input
            value={value.password_secret_ref ?? ""}
            onChange={(e) => onChange({ ...value, password_secret_ref: e.target.value })}
            placeholder="env://MY_PASSWORD"
            className="rounded-md border border-surface-300 px-2 py-1.5 text-xs"
          />
        </>
      )}
      {(value.kind === "oauth_client_credentials" || value.kind === "oauth_refresh_token") && (
        <>
          <input
            value={value.token_url ?? ""}
            onChange={(e) => onChange({ ...value, token_url: e.target.value })}
            placeholder="https://issuer.example.com/oauth/token"
            className="rounded-md border border-surface-300 px-2 py-1.5 text-xs"
          />
          <select
            aria-label="Client auth method"
            value={value.client_auth_method ?? "basic"}
            onChange={(e) =>
              onChange({
                ...value,
                client_auth_method: e.target.value as OpenApiAuthBinding["client_auth_method"],
              })
            }
            className="rounded-md border border-surface-300 px-2 py-1.5 text-xs"
          >
            <option value="basic">Client auth: HTTP Basic</option>
            <option value="body">Client auth: form body</option>
          </select>
          <input
            value={value.client_id ?? ""}
            onChange={(e) => onChange({ ...value, client_id: e.target.value })}
            placeholder="client_id"
            className="rounded-md border border-surface-300 px-2 py-1.5 text-xs"
          />
          <input
            value={value.client_secret_ref ?? ""}
            onChange={(e) => onChange({ ...value, client_secret_ref: e.target.value })}
            placeholder="env://OAUTH_CLIENT_SECRET (optional for refresh token)"
            className="rounded-md border border-surface-300 px-2 py-1.5 text-xs"
          />
          <input
            value={scopesInput}
            onChange={(e) => setScopes(e.target.value)}
            placeholder="scopes (comma-separated)"
            className="rounded-md border border-surface-300 px-2 py-1.5 text-xs"
          />
          <input
            value={value.audience ?? ""}
            onChange={(e) => onChange({ ...value, audience: e.target.value })}
            placeholder="audience (optional)"
            className="rounded-md border border-surface-300 px-2 py-1.5 text-xs"
          />
          <input
            value={value.resource ?? ""}
            onChange={(e) => onChange({ ...value, resource: e.target.value })}
            placeholder="resource (optional)"
            className="rounded-md border border-surface-300 px-2 py-1.5 text-xs"
          />
          {value.kind === "oauth_refresh_token" && (
            <input
              value={value.refresh_token_secret_ref ?? ""}
              onChange={(e) =>
                onChange({ ...value, refresh_token_secret_ref: e.target.value })
              }
              placeholder="env://OAUTH_REFRESH_TOKEN"
              className="rounded-md border border-surface-300 px-2 py-1.5 text-xs"
            />
          )}
        </>
      )}
    </div>
  );
}

function readSchemaType(schema: Record<string, unknown> | null | undefined): string {
  if (!schema) return "unknown";
  const directType = schema.type;
  if (typeof directType === "string" && directType.length > 0) return directType;
  if (Array.isArray(schema.enum)) return "enum";
  if (schema.properties && typeof schema.properties === "object") return "object";
  if (schema.items && typeof schema.items === "object") return "array";
  return "unknown";
}

function readSchemaProperties(
  schema: Record<string, unknown> | null | undefined,
): Array<[string, Record<string, unknown>]> {
  if (!schema || !schema.properties || typeof schema.properties !== "object") return [];
  return Object.entries(schema.properties).filter(
    (entry): entry is [string, Record<string, unknown>] =>
      Boolean(entry[1]) && typeof entry[1] === "object" && !Array.isArray(entry[1]),
  );
}

function readRequiredFields(schema: Record<string, unknown> | null | undefined): Set<string> {
  if (!schema || !Array.isArray(schema.required)) return new Set();
  return new Set(schema.required.filter((entry): entry is string => typeof entry === "string"));
}

function summarizeSchemaValue(schema: Record<string, unknown> | null | undefined): string {
  if (!schema) return "unknown";
  const schemaType = readSchemaType(schema);
  if (schemaType === "object") {
    const required = readRequiredFields(schema);
    const properties = readSchemaProperties(schema);
    if (properties.length === 0) return "{}";
    return `{ ${properties
      .slice(0, 4)
      .map(([name, spec]) => `${name}${required.has(name) ? "" : "?"}: ${summarizeSchemaValue(spec)}`)
      .join("; ")}${properties.length > 4 ? "; ..." : ""} }`;
  }
  if (schemaType === "array") {
    const items =
      schema.items && typeof schema.items === "object" && !Array.isArray(schema.items)
        ? summarizeSchemaValue(schema.items as Record<string, unknown>)
        : "unknown";
    return `${items}[]`;
  }
  if (schemaType === "enum" && Array.isArray(schema.enum)) {
    return schema.enum
      .slice(0, 4)
      .map((entry) => JSON.stringify(entry))
      .join(" | ");
  }
  return schemaType;
}

function formatToolSignature(draft: OpenApiToolDraft): string {
  return `${draft.name}(input: ${summarizeSchemaValue(draft.input_schema)}) -> ${summarizeSchemaValue(
    draft.output_schema,
  )}`;
}

function SchemaPanel({
  title,
  schema,
  emptyLabel,
}: {
  title: string;
  schema: Record<string, unknown> | null;
  emptyLabel: string;
}): JSX.Element {
  const properties = readSchemaProperties(schema);
  const required = readRequiredFields(schema);

  return (
    <div className="rounded-xl border border-surface-200 bg-white p-3">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">{title}</div>
      {schema ? (
        <>
          <div className="mt-2 break-words font-mono text-[11px] text-gray-900">
            {summarizeSchemaValue(schema)}
          </div>
          {properties.length > 0 ? (
            <div className="mt-3 flex flex-wrap gap-1.5">
              {properties.map(([name, spec]) => (
                <span
                  key={name}
                  className="inline-flex rounded-full bg-surface-100 px-2 py-1 text-[11px] text-gray-700"
                >
                  <span className="font-medium text-gray-900">{name}</span>
                  <span className="ml-1 text-gray-500">
                    {required.has(name) ? ":" : "?:"} {summarizeSchemaValue(spec)}
                  </span>
                </span>
              ))}
            </div>
          ) : (
            <div className="mt-3 text-xs text-gray-500">
              No named fields were projected in this schema.
            </div>
          )}
          <details className="mt-3">
            <summary className="cursor-pointer text-xs font-medium text-caliber-purple">
              Show JSON schema
            </summary>
            <pre className="mt-2 max-h-56 overflow-auto rounded-lg border border-surface-100 bg-surface-50 p-3 text-[11px]">
              {JSON.stringify(schema, null, 2)}
            </pre>
          </details>
        </>
      ) : (
        <div className="mt-2 text-xs text-gray-500">{emptyLabel}</div>
      )}
    </div>
  );
}

function ToolDraftRow({
  draft,
  integrationId,
  onChanged,
}: {
  draft: OpenApiToolDraft;
  integrationId: string;
  onChanged: () => void;
}): JSX.Element {
  const [expanded, setExpanded] = useState(false);
  const [authBinding, setAuthBinding] = useState<OpenApiAuthBinding>(
    draft.auth_binding ?? { kind: "none" },
  );
  const [serverUrl, setServerUrl] = useState(draft.server_url);
  const [previewInput, setPreviewInput] = useState("{}");
  const [previewOutput, setPreviewOutput] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [publication, setPublication] = useState<{
    toolId: string;
    name: string;
    version: string;
    executionBackend: string;
  } | null>(null);

  const saveConfig = async (): Promise<void> => {
    setBusy(true);
    setMessage(null);
    try {
      await caliberApi.updateOpenApiToolDraft(integrationId, draft.draft_id, {
        server_url: serverUrl,
        auth_binding: authBinding,
      });
      setMessage("Saved.");
      onChanged();
    } catch (err) {
      setMessage(err instanceof ApiError ? err.message : "Failed to save");
    } finally {
      setBusy(false);
    }
  };

  const togglePreviewable = async (): Promise<void> => {
    await caliberApi.updateOpenApiToolDraft(integrationId, draft.draft_id, {
      allow_in_preview: !draft.allow_in_preview,
    });
    onChanged();
  };

  const preview = async (): Promise<void> => {
    setBusy(true);
    setPreviewOutput(null);
    try {
      const parsed = JSON.parse(previewInput || "{}");
      const result = await caliberApi.previewOpenApiToolDraft(integrationId, draft.draft_id, parsed);
      setPreviewOutput(JSON.stringify(result.result, null, 2));
    } catch (err) {
      setPreviewOutput(err instanceof ApiError ? `Error: ${err.message}` : "Preview failed");
    } finally {
      setBusy(false);
    }
  };

  const publish = async (): Promise<void> => {
    setBusy(true);
    setMessage(null);
    try {
      const result = await caliberApi.publishOpenApiToolDraft(integrationId, draft.draft_id, {
        version: "1.0",
      });
      setPublication({
        toolId: result.tool.tool_id,
        name: result.tool.name,
        version: result.tool.version,
        executionBackend: result.tool.execution_backend,
      });
      setMessage(null);
      onChanged();
    } catch (err) {
      setPublication(null);
      setMessage(err instanceof ApiError ? err.message : "Publish failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="border border-surface-200 rounded-lg bg-white">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center justify-between px-4 py-3 text-left"
      >
        <div>
          <span className="font-medium text-gray-900 text-sm">{draft.name}</span>
          {draft.additional_operation_ids.length > 0 && (
            <span className="ml-2 text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded bg-purple-50 text-purple-700">
              pack · {draft.additional_operation_ids.length + 1} operations
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <span
            className={`text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded ${
              SIDE_EFFECT_COLOR[draft.side_effect_level] ?? "bg-slate-50 text-slate-600"
            }`}
          >
            {draft.side_effect_level}
          </span>
          <StatusPill status={draft.status} />
        </div>
      </button>
      {expanded && (
        <div className="border-t border-surface-100 px-4 py-3 space-y-3">
          <p className="text-xs text-gray-500">{draft.description}</p>
          <div className="rounded-xl border border-surface-200 bg-surface-50 p-3">
            <div className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
              Callable Signature
            </div>
            <pre className="mt-2 whitespace-pre-wrap break-words font-mono text-[11px] text-gray-900">
              {formatToolSignature(draft)}
            </pre>
          </div>

          <div className="grid gap-3 lg:grid-cols-2">
            <SchemaPanel
              title="Input Signature"
              schema={draft.input_schema}
              emptyLabel="No input schema was generated for this draft."
            />
            <SchemaPanel
              title="Output Signature"
              schema={draft.output_schema}
              emptyLabel="No output schema was generated for this draft."
            />
          </div>

          <div className="grid gap-2 sm:grid-cols-2">
            <label className="text-xs">
              <span className="block font-medium text-gray-600 mb-1">Server URL</span>
              <input
                value={serverUrl}
                onChange={(e) => setServerUrl(e.target.value)}
                className="w-full rounded-md border border-surface-300 px-2 py-1.5 text-xs"
              />
            </label>
            <label className="text-xs">
              <span className="block font-medium text-gray-600 mb-1">Auth binding</span>
              <AuthBindingForm value={authBinding} onChange={setAuthBinding} />
            </label>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={saveConfig}
              className="text-xs font-medium px-3 py-1.5 rounded-md border border-surface-300 text-gray-700 hover:bg-surface-50 disabled:opacity-50"
            >
              Save
            </button>
            <label className="flex items-center gap-1.5 text-xs text-gray-500">
              <input type="checkbox" checked={draft.allow_in_preview} onChange={togglePreviewable} />
              allow live preview (fires a real request)
            </label>
          </div>

          {draft.allow_in_preview && (
            <div>
              <span className="block text-xs font-medium text-gray-600 mb-1">
                Preview input (JSON)
              </span>
              <textarea
                value={previewInput}
                onChange={(e) => setPreviewInput(e.target.value)}
                rows={3}
                className="w-full rounded-md border border-surface-300 px-2 py-1.5 text-xs font-mono"
              />
              <button
                type="button"
                disabled={busy}
                onClick={preview}
                className="mt-1.5 text-xs font-medium px-3 py-1.5 rounded-md border border-surface-300 text-gray-700 hover:bg-surface-50 disabled:opacity-50"
              >
                Run preview
              </button>
              {previewOutput && (
                <pre className="mt-2 rounded-md bg-surface-50 border border-surface-100 p-2 text-[11px] overflow-x-auto">
                  {previewOutput}
                </pre>
              )}
            </div>
          )}

          {publication && (
            <div
              role="status"
              data-testid="openapi-publication-success"
              className="rounded-xl border border-emerald-200 bg-emerald-50 p-4 text-emerald-950"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <div className="text-sm font-semibold">Tool published successfully</div>
                  <p className="mt-1 text-xs text-emerald-800">
                    This draft is now a governed tool in the CALIBER Tool Registry. It uses the
                    declarative OpenAPI HTTP runtime; no per-tool Python source was generated.
                  </p>
                </div>
                <Link
                  to={`/tools/${encodeURIComponent(publication.toolId)}`}
                  className="shrink-0 rounded-md border border-emerald-300 bg-white px-3 py-1.5 text-xs font-medium text-emerald-800 hover:bg-emerald-100"
                >
                  Open in Tool Registry
                </Link>
              </div>
              <dl className="mt-3 grid gap-2 text-xs sm:grid-cols-3">
                <div>
                  <dt className="font-medium uppercase tracking-wide text-emerald-700">Tool ID</dt>
                  <dd className="mt-0.5 font-mono text-emerald-950">{publication.toolId}</dd>
                </div>
                <div>
                  <dt className="font-medium uppercase tracking-wide text-emerald-700">Version</dt>
                  <dd className="mt-0.5 text-emerald-950">{publication.version}</dd>
                </div>
                <div>
                  <dt className="font-medium uppercase tracking-wide text-emerald-700">Runtime</dt>
                  <dd className="mt-0.5 font-mono text-emerald-950">{publication.executionBackend}</dd>
                </div>
              </dl>
            </div>
          )}

          {message && (
            <div role="alert" className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800">
              {message}
            </div>
          )}

          <div className="flex justify-end">
            {draft.published_tool_id ? (
              <span className="rounded-md bg-emerald-50 px-2.5 py-1.5 text-xs font-medium text-emerald-800">
                Published · {draft.published_tool_id}
              </span>
            ) : (
              <button
                type="button"
                disabled={busy}
                onClick={publish}
                className="text-sm font-medium text-white bg-caliber-purple px-3 py-1.5 rounded-md hover:bg-caliber-purple-dark disabled:opacity-50"
              >
                Publish
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function ToolDraftsTab({
  integrationId,
  onPublished,
}: {
  integrationId: string;
  onPublished: () => void;
}): JSX.Element {
  const fetcher = useCallback(
    (signal: AbortSignal) => caliberApi.listOpenApiToolDrafts(integrationId, signal),
    [integrationId],
  );
  const { data, error, loading, refresh } = useApi(fetcher, [integrationId]);

  if (loading && !data) return <div className="text-sm text-gray-500">Loading…</div>;
  if (error) return <div className="text-sm text-red-600">{error.message}</div>;

  return (
    <div className="space-y-2">
      {(data ?? []).map((draft) => (
        <ToolDraftRow
          key={draft.draft_id}
          draft={draft}
          integrationId={integrationId}
          onChanged={() => {
            refresh();
            onPublished();
          }}
        />
      ))}
      {data && data.length === 0 && (
        <div className="text-center text-sm text-gray-500 py-10 bg-white rounded-lg border border-surface-200">
          No tool drafts yet — select operations from the Operations tab and generate one.
        </div>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Graph tab                                                                   */
/* -------------------------------------------------------------------------- */

type OpenApiGraphRelation = {
  dependencyId: string;
  fromNodeId: string;
  toNodeId: string;
  fromLabel: string;
  toLabel: string;
  dependencyType: string;
  status: string;
  confidence: string;
  source: string;
};

type OpenApiGraphLayoutNode = {
  id: string;
  x: number;
  y: number;
  label: string;
  method: string;
  path: string;
  sideEffectLevel: string;
};

type GraphViewMode = "graph" | "tree" | "flow";

const GRAPH_CARD_WIDTH = 220;
const GRAPH_CARD_HEIGHT = 92;
const GRAPH_GAP_X = 72;
const GRAPH_GAP_Y = 48;
const GRAPH_PADDING = 28;

function deriveGraphRelations(snapshot: OpenApiGraphSnapshot): OpenApiGraphRelation[] {
  const operationNodes = new Map(
    snapshot.nodes
      .filter((node) => node.type === "operation")
      .map((node) => [node.id, node]),
  );
  const inboundByDependency = new Map<string, { from: string }>();
  const outboundByDependency = new Map<string, { to: string }>();

  for (const edge of snapshot.edges) {
    if (edge.to.startsWith("dependency:") && edge.from.startsWith("operation:")) {
      inboundByDependency.set(edge.to, { from: edge.from });
    }
    if (edge.from.startsWith("dependency:") && edge.to.startsWith("operation:")) {
      outboundByDependency.set(edge.from, { to: edge.to });
    }
  }

  return snapshot.nodes
    .filter((node) => node.type === "dependency")
    .map((node) => {
      const inbound = inboundByDependency.get(node.id);
      const outbound = outboundByDependency.get(node.id);
      if (!inbound || !outbound) return null;
      const fromNode = operationNodes.get(inbound.from);
      const toNode = operationNodes.get(outbound.to);
      if (!fromNode || !toNode) return null;
      return {
        dependencyId: node.id,
        fromNodeId: fromNode.id,
        toNodeId: toNode.id,
        fromLabel: fromNode.label,
        toLabel: toNode.label,
        dependencyType: String(node.data.dependency_type ?? node.label),
        status: String(node.data.status ?? ""),
        confidence: String(node.data.confidence ?? ""),
        source: String(node.data.source ?? ""),
      };
    })
    .filter((relation): relation is OpenApiGraphRelation => relation !== null);
}

function buildOperationGraphLayout(
  snapshot: OpenApiGraphSnapshot,
  visibleOperationIds?: Set<string>,
): {
  width: number;
  height: number;
  nodes: OpenApiGraphLayoutNode[];
} {
  const operations = snapshot.nodes
    .filter((node) => node.type === "operation")
    .filter((node) => !visibleOperationIds || visibleOperationIds.has(node.id))
    .map((node) => ({
      id: node.id,
      label: node.label,
      method: String(node.data.method ?? ""),
      path: String(node.data.path ?? node.label),
      sideEffectLevel: String(node.data.side_effect_level ?? ""),
    }))
    .sort((left, right) => left.label.localeCompare(right.label));

  const columns = operations.length <= 2 ? operations.length || 1 : operations.length <= 6 ? 2 : 3;
  const rows = Math.max(1, Math.ceil(operations.length / columns));
  const width =
    GRAPH_PADDING * 2 + columns * GRAPH_CARD_WIDTH + Math.max(0, columns - 1) * GRAPH_GAP_X;
  const height =
    GRAPH_PADDING * 2 + rows * GRAPH_CARD_HEIGHT + Math.max(0, rows - 1) * GRAPH_GAP_Y;

  return {
    width,
    height,
    nodes: operations.map((node, index) => {
      const column = index % columns;
      const row = Math.floor(index / columns);
      return {
        ...node,
        x: GRAPH_PADDING + column * (GRAPH_CARD_WIDTH + GRAPH_GAP_X),
        y: GRAPH_PADDING + row * (GRAPH_CARD_HEIGHT + GRAPH_GAP_Y),
      };
    }),
  };
}

function GraphOperationCard({
  node,
  selected,
  onSelect,
}: {
  node: OpenApiGraphLayoutNode;
  selected: boolean;
  onSelect: () => void;
}): JSX.Element {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-label={`Inspect ${node.label}`}
      className={`absolute rounded-2xl border bg-white p-3 text-left shadow-sm transition hover:-translate-y-0.5 hover:shadow-md ${
        selected
          ? "border-caliber-purple ring-2 ring-caliber-purple/20"
          : "border-surface-200"
      }`}
      style={{
        left: node.x,
        top: node.y,
        width: GRAPH_CARD_WIDTH,
        height: GRAPH_CARD_HEIGHT,
      }}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="font-mono text-[11px] font-semibold text-gray-900">{node.method}</div>
        <span
          className={`text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded ${
            SIDE_EFFECT_COLOR[node.sideEffectLevel] ?? "bg-slate-50 text-slate-600"
          }`}
        >
          {node.sideEffectLevel || "unknown"}
        </span>
      </div>
      <div className="mt-2 line-clamp-2 font-mono text-[11px] text-gray-800">{node.path}</div>
      <div className="mt-2 line-clamp-2 text-[11px] text-gray-500">{node.label}</div>
    </button>
  );
}

function DependencyGraphCanvas({
  snapshot,
  visibleOperationIds,
  visibleRelationIds,
  selectedNodeId,
  onSelectNode,
}: {
  snapshot: OpenApiGraphSnapshot;
  visibleOperationIds?: Set<string>;
  visibleRelationIds?: Set<string>;
  selectedNodeId: string | null;
  onSelectNode: (nodeId: string) => void;
}): JSX.Element {
  const layout = useMemo(
    () => buildOperationGraphLayout(snapshot, visibleOperationIds),
    [snapshot, visibleOperationIds],
  );
  const relations = useMemo(
    () =>
      deriveGraphRelations(snapshot).filter(
        (relation) => !visibleRelationIds || visibleRelationIds.has(relation.dependencyId),
      ),
    [snapshot, visibleRelationIds],
  );
  const positions = useMemo(
    () =>
      new Map(
        layout.nodes.map((node) => [
          node.id,
          {
            centerX: node.x + GRAPH_CARD_WIDTH / 2,
            centerY: node.y + GRAPH_CARD_HEIGHT / 2,
          },
        ]),
      ),
    [layout.nodes],
  );

  if (layout.nodes.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-surface-300 bg-surface-50 px-4 py-10 text-center text-sm text-gray-500">
        No API nodes are available in this graph snapshot yet.
      </div>
    );
  }

  return (
    <div
      className="openapi-graph-canvas overflow-auto rounded-xl border border-slate-300 bg-slate-100 p-3"
      data-testid="openapi-graph-canvas"
    >
      <div className="relative" style={{ width: layout.width, height: layout.height }}>
        <svg
          className="absolute inset-0"
          width={layout.width}
          height={layout.height}
          viewBox={`0 0 ${layout.width} ${layout.height}`}
        >
          <defs>
            <marker
              id="openapi-graph-arrow"
              markerWidth="10"
              markerHeight="10"
              refX="8"
              refY="5"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#64748b" />
            </marker>
          </defs>
          {relations.map((relation) => {
            const from = positions.get(relation.fromNodeId);
            const to = positions.get(relation.toNodeId);
            if (!from || !to) return null;
            const controlOffset = Math.max(48, Math.abs(to.centerX - from.centerX) / 3);
            const path = `M ${from.centerX} ${from.centerY} C ${from.centerX + controlOffset} ${from.centerY}, ${to.centerX - controlOffset} ${to.centerY}, ${to.centerX} ${to.centerY}`;
            const midX = (from.centerX + to.centerX) / 2;
            const midY = (from.centerY + to.centerY) / 2;
            const edgeLabel = DEPENDENCY_TYPE_LABEL[relation.dependencyType] ?? relation.dependencyType;
            return (
              <g key={relation.dependencyId}>
                <path
                  d={path}
                  fill="none"
                  stroke={
                    selectedNodeId === relation.fromNodeId || selectedNodeId === relation.toNodeId
                      ? "#6d28d9"
                      : "#64748b"
                  }
                  strokeWidth={
                    selectedNodeId === relation.fromNodeId || selectedNodeId === relation.toNodeId
                      ? "3"
                      : "2"
                  }
                  opacity={selectedNodeId && selectedNodeId !== relation.fromNodeId && selectedNodeId !== relation.toNodeId ? 0.3 : 1}
                  markerEnd="url(#openapi-graph-arrow)"
                />
                <g transform={`translate(${midX - 54}, ${midY - 12})`}>
                  <rect width="108" height="24" rx="12" fill="#ffffff" stroke="#94a3b8" />
                  <text
                    x="54"
                    y="15"
                    textAnchor="middle"
                    fontSize="10"
                    fill="#1e293b"
                    fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
                  >
                    {edgeLabel}
                  </text>
                </g>
              </g>
            );
          })}
        </svg>
        {layout.nodes.map((node) => (
          <GraphOperationCard
            key={node.id}
            node={node}
            selected={selectedNodeId === node.id}
            onSelect={() => onSelectNode(node.id)}
          />
        ))}
      </div>
    </div>
  );
}

function DependencyTreeView({
  operations,
  relations,
  rootId,
  onSelectNode,
}: {
  operations: OpenApiGraphNode[];
  relations: OpenApiGraphRelation[];
  rootId: string | null;
  onSelectNode: (nodeId: string) => void;
}): JSX.Element {
  const operationById = useMemo(
    () => new Map(operations.map((operation) => [operation.id, operation])),
    [operations],
  );
  const root = (rootId ? operationById.get(rootId) : undefined) ?? operations[0];

  if (!root) {
    return (
      <div className="rounded-xl border border-dashed border-surface-300 bg-surface-50 px-4 py-10 text-center text-sm text-gray-500">
        No operations match the current filters.
      </div>
    );
  }

  const renderBranch = (nodeId: string, visited: Set<string>, depth: number): JSX.Element => {
    const node = operationById.get(nodeId);
    if (!node) return <></>;
    const children = relations.filter(
      (relation) => relation.fromNodeId === nodeId && operationById.has(relation.toNodeId),
    );
    const nextVisited = new Set(visited).add(nodeId);

    return (
      <div className={depth > 0 ? "ml-6 border-l border-surface-200 pl-4" : ""}>
        <button
          type="button"
          onClick={() => onSelectNode(node.id)}
          className={`w-full rounded-lg border bg-white p-3 text-left hover:border-caliber-purple ${
            node.id === root.id ? "border-caliber-purple ring-1 ring-caliber-purple/20" : "border-surface-200"
          }`}
        >
          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-slate-700">
              {String(node.data.method ?? "")}
            </span>
            <span className="font-mono text-xs font-medium text-gray-900">{node.label}</span>
            {depth === 0 && (
              <span className="rounded bg-purple-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-purple-700">
                root
              </span>
            )}
          </div>
          <div className="mt-1 text-[11px] text-gray-500">
            {String(node.data.side_effect_level ?? "unknown")} side effect level
          </div>
        </button>
        {children.length > 0 && depth < 8 && (
          <div className="mt-2 space-y-2">
            {children.map((relation) => {
              const cycle = nextVisited.has(relation.toNodeId);
              return (
                <div key={relation.dependencyId}>
                  <div className="mb-1 flex items-center gap-2 text-[10px] text-gray-500">
                    <span className="text-caliber-purple">↓</span>
                    <span>{DEPENDENCY_TYPE_LABEL[relation.dependencyType] ?? relation.dependencyType}</span>
                    <span>· {relation.confidence} confidence</span>
                    {cycle && <span className="rounded bg-amber-100 px-1 text-amber-700">cycle</span>}
                  </div>
                  {cycle ? (
                    <div className="ml-6 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                      Returns to {relation.toLabel}
                    </div>
                  ) : (
                    renderBranch(relation.toNodeId, nextVisited, depth + 1)
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="rounded-xl border border-surface-200 bg-surface-50 p-4" data-testid="openapi-tree-view">
      <div className="mb-3 text-xs text-gray-500">
        Downstream dependency tree from the selected operation. Shared relationships remain visible
        in the Graph view.
      </div>
      {renderBranch(root.id, new Set(), 0)}
    </div>
  );
}

function DependencyFlowView({
  relations,
  operationById,
  rootId,
}: {
  relations: OpenApiGraphRelation[];
  operationById: Map<string, OpenApiGraphNode>;
  rootId: string | null;
}): JSX.Element {
  const flowRelations = rootId
    ? relations.filter((relation) => relation.fromNodeId === rootId || relation.toNodeId === rootId)
    : relations;

  if (flowRelations.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-surface-300 bg-surface-50 px-4 py-10 text-center text-sm text-gray-500">
        No dependency steps match the current filters. Select an operation or change the filters.
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-surface-200 bg-surface-50 p-4" data-testid="openapi-flow-view">
      <div className="mb-3 text-xs text-gray-500">
        A readable sequence projection for the selected operation. Use Graph view when relationships
        branch or converge.
      </div>
      <div className="space-y-2">
        {flowRelations.map((relation, index) => {
          const from = operationById.get(relation.fromNodeId);
          const to = operationById.get(relation.toNodeId);
          return (
            <div key={relation.dependencyId} className="flex items-center gap-2 rounded-lg border border-surface-200 bg-white p-3">
              <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-caliber-purple text-xs font-semibold text-white">
                {index + 1}
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <span className="font-mono font-medium text-gray-900">{from?.label ?? relation.fromLabel}</span>
                  <span className="text-caliber-purple">→</span>
                  <span className="font-mono font-medium text-gray-900">{to?.label ?? relation.toLabel}</span>
                </div>
                <div className="mt-1 text-[11px] text-gray-500">
                  {DEPENDENCY_TYPE_LABEL[relation.dependencyType] ?? relation.dependencyType} · {relation.confidence} confidence
                  {relation.source && ` · ${relation.source}`}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function GraphTab({ integrationId }: { integrationId: string }): JSX.Element {
  const fetcher = useCallback(
    (signal: AbortSignal) => caliberApi.getOpenApiGraph(integrationId, undefined, signal),
    [integrationId],
  );
  const { data, error, loading } = useApi(fetcher, [integrationId]);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<GraphViewMode>("graph");

  const byType = useMemo(() => {
    if (!data) return new Map<string, number>();
    const counts = new Map<string, number>();
    for (const node of data.nodes) {
      counts.set(node.type, (counts.get(node.type) ?? 0) + 1);
    }
    return counts;
  }, [data]);

  const relations = useMemo(() => (data ? deriveGraphRelations(data) : []), [data]);
  const operationNodes = useMemo(
    () => (data ? data.nodes.filter((node) => node.type === "operation") : []),
    [data],
  );
  const visibleOperationIds = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return new Set(
      operationNodes
        .filter(
          (node) =>
            !normalizedQuery ||
            `${node.label} ${String(node.data.method ?? "")} ${String(node.data.path ?? "")}`
              .toLowerCase()
              .includes(normalizedQuery),
        )
        .map((node) => node.id),
    );
  }, [operationNodes, query]);
  const visibleRelations = useMemo(
    () =>
      relations.filter((relation) => statusFilter === "all" || relation.status === statusFilter),
    [relations, statusFilter],
  );
  const visibleRelationIds = useMemo(
    () => new Set(visibleRelations.map((relation) => relation.dependencyId)),
    [visibleRelations],
  );
  const selectedOperation = operationNodes.find((node) => node.id === selectedNodeId) ?? null;
  const selectedIncoming = relations.filter((relation) => relation.toNodeId === selectedNodeId);
  const selectedOutgoing = relations.filter((relation) => relation.fromNodeId === selectedNodeId);
  const operationById = useMemo(
    () => new Map(operationNodes.map((operation) => [operation.id, operation])),
    [operationNodes],
  );
  const rootId = selectedNodeId ?? operationNodes[0]?.id ?? null;
  const knowledgeGraphJson = useMemo(() => {
    if (!data) return "";
    const operationNodes = data.nodes
      .filter((node) => node.type === "operation")
      .map((node) => ({
        id: node.id,
        label: node.label,
        method: node.data.method,
        path: node.data.path,
        side_effect_level: node.data.side_effect_level,
      }));
    const nodeTypeCounts = Object.fromEntries(Array.from(byType.entries()).sort());
    const edgeTypeCounts = Object.fromEntries(
      Array.from(
        data.edges.reduce((counts, edge) => {
          counts.set(edge.type, (counts.get(edge.type) ?? 0) + 1);
          return counts;
        }, new Map<string, number>()),
      ).sort(([left], [right]) => left.localeCompare(right)),
    );
    return JSON.stringify(
      {
        integration_id: data.integration_id,
        integration_version_id: data.integration_version_id,
        summary: data.summary,
        node_type_counts: nodeTypeCounts,
        edge_type_counts: edgeTypeCounts,
        operations: operationNodes,
        dependencies: relations,
      },
      null,
      2,
    );
  }, [byType, data, relations]);

  if (loading && !data) return <div className="text-sm text-gray-500">Loading…</div>;
  if (error) return <div className="text-sm text-red-600">{error.message}</div>;
  if (!data) return <></>;

  return (
    <div>
      <p className="text-xs text-gray-500 mb-4">
        A derived planning aid, not the execution contract — dependency truth lives in the
        Dependencies tab. {data.summary.node_count} nodes, {data.summary.edge_count} edges across{" "}
        {data.summary.operation_count} operations and {data.summary.dependency_count} dependency
        edges.
      </p>

      <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4 xl:grid-cols-6">
        {Array.from(byType.entries()).map(([type, count]) => (
          <div key={type} className="rounded-lg border border-surface-200 bg-white p-3 text-center">
            <div className="text-lg font-semibold text-gray-900">{count}</div>
            <div className="text-[10px] uppercase text-gray-500 tracking-wide">{type}</div>
          </div>
        ))}
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(420px,0.95fr)]">
        <div className="space-y-3">
          <div className="rounded-xl border border-surface-200 bg-white p-4">
            <div className="flex items-center justify-between gap-3">
              <div>
                <h3 className="text-sm font-semibold text-gray-900">API Dependency Graph</h3>
                <p className="mt-1 text-xs text-gray-500">
                  Operation nodes are the API surfaces. Labeled edges show the detected dependency
                  relationship between them.
                </p>
              </div>
              <div className="text-xs text-gray-500">
                {visibleRelations.length} of {relations.length} relationship{relations.length === 1 ? "" : "s"}
              </div>
            </div>
            <div className="mt-4 grid gap-2 md:grid-cols-[minmax(0,1fr)_180px_auto]">
              <label className="sr-only" htmlFor="openapi-graph-search">
                Search operations
              </label>
              <input
                id="openapi-graph-search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search method, path, or operation…"
                className="rounded-md border border-surface-300 px-3 py-2 text-sm"
              />
              <label className="sr-only" htmlFor="openapi-graph-status">
                Filter dependencies by status
              </label>
              <select
                id="openapi-graph-status"
                value={statusFilter}
                onChange={(event) => setStatusFilter(event.target.value)}
                className="rounded-md border border-surface-300 px-3 py-2 text-sm"
              >
                <option value="all">All dependency statuses</option>
                <option value="auto_wired">Auto-wired</option>
                <option value="suggested">Suggested</option>
                <option value="advisory">Advisory</option>
                <option value="confirmed">Confirmed</option>
                <option value="rejected">Rejected</option>
              </select>
              <button
                type="button"
                onClick={() => {
                  setQuery("");
                  setStatusFilter("all");
                  setSelectedNodeId(null);
                }}
                className="rounded-md border border-surface-300 px-3 py-2 text-sm text-gray-600 hover:bg-surface-50"
              >
                Reset
              </button>
            </div>
            <p className="mt-2 text-[11px] text-gray-500">
              Click an operation to inspect its dependency chain. Arrows point from the operation
              that supplies context to the operation that consumes it.
            </p>
            <div className="mt-3 inline-flex rounded-lg border border-surface-200 bg-surface-50 p-1" aria-label="Dependency view mode">
              {(["graph", "tree", "flow"] as const).map((mode) => (
                <button
                  key={mode}
                  type="button"
                  onClick={() => setViewMode(mode)}
                  aria-pressed={viewMode === mode}
                  className={`rounded-md px-3 py-1.5 text-xs font-medium capitalize ${
                    viewMode === mode ? "bg-white text-caliber-purple shadow-sm" : "text-gray-500 hover:text-gray-900"
                  }`}
                >
                  {mode}
                </button>
              ))}
            </div>
            <div className="mt-4">
              {viewMode === "graph" ? (
                <DependencyGraphCanvas
                  snapshot={data}
                  visibleOperationIds={visibleOperationIds}
                  visibleRelationIds={visibleRelationIds}
                  selectedNodeId={selectedNodeId}
                  onSelectNode={setSelectedNodeId}
                />
              ) : viewMode === "tree" ? (
                <DependencyTreeView
                  operations={operationNodes.filter((node) => visibleOperationIds.has(node.id))}
                  relations={visibleRelations}
                  rootId={rootId}
                  onSelectNode={setSelectedNodeId}
                />
              ) : (
                <DependencyFlowView
                  relations={visibleRelations}
                  operationById={operationById}
                  rootId={selectedNodeId}
                />
              )}
            </div>
            {visibleOperationIds.size === 0 && (
              <div className="mt-3 rounded-lg border border-dashed border-surface-300 bg-surface-50 px-3 py-3 text-sm text-gray-500">
                No operations match “{query}”.
              </div>
            )}
          </div>

          {selectedOperation && (
            <div className="rounded-xl border border-caliber-purple/30 bg-purple-50/30 p-4" data-testid="openapi-graph-inspector">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="font-mono text-xs font-semibold text-gray-900">
                    {String(selectedOperation.data.method ?? "")} {String(selectedOperation.data.path ?? selectedOperation.label)}
                  </div>
                  <p className="mt-1 text-sm text-gray-700">{selectedOperation.label}</p>
                </div>
                <button
                  type="button"
                  onClick={() => setSelectedNodeId(null)}
                  className="text-xs text-gray-500 hover:text-gray-900"
                >
                  Clear selection
                </button>
              </div>
              <div className="mt-3 grid gap-3 md:grid-cols-2">
                {[
                  ["Consumes context from", selectedIncoming],
                  ["Supplies context to", selectedOutgoing],
                ].map(([title, items]) => (
                  <div key={title as string} className="rounded-lg border border-surface-200 bg-white p-3">
                    <div className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">{title as string}</div>
                    {(items as OpenApiGraphRelation[]).length === 0 ? (
                      <div className="mt-2 text-xs text-gray-500">None detected</div>
                    ) : (
                      <div className="mt-2 space-y-2">
                        {(items as OpenApiGraphRelation[]).map((relation) => (
                          <div key={relation.dependencyId} className="text-xs">
                            <div className="font-medium text-gray-800">
                              {title === "Consumes context from" ? relation.fromLabel : relation.toLabel}
                            </div>
                            <div className="mt-1 flex flex-wrap gap-1 text-[10px] text-gray-500">
                              <span>{DEPENDENCY_TYPE_LABEL[relation.dependencyType] ?? relation.dependencyType}</span>
                              <span>· {relation.confidence} confidence</span>
                              {relation.source && <span>· {relation.source}</span>}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="rounded-xl border border-surface-200 bg-white p-4">
            <h3 className="text-sm font-semibold text-gray-900">Dependency Edges</h3>
            <div className="mt-3 space-y-2">
              {relations.length > 0 ? (
                relations.map((relation) => (
                  <div
                    key={relation.dependencyId}
                    className="rounded-lg border border-surface-200 bg-surface-50 px-3 py-2"
                  >
                    <div className="text-xs text-gray-800">
                      <span className="font-medium">{relation.fromLabel}</span>
                      <span className="mx-2 text-gray-400">→</span>
                      <span className="font-medium">{relation.toLabel}</span>
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-gray-500">
                      <span>{DEPENDENCY_TYPE_LABEL[relation.dependencyType] ?? relation.dependencyType}</span>
                      <span
                        className={`rounded px-1.5 py-0.5 font-semibold uppercase ${
                          DEPENDENCY_STATUS_COLOR[relation.status] ?? "bg-slate-50 text-slate-600"
                        }`}
                      >
                        {DEPENDENCY_STATUS_LABEL[relation.status] ?? relation.status}
                      </span>
                      <span
                        className={`rounded px-1.5 py-0.5 font-semibold uppercase ${
                          CONFIDENCE_COLOR[relation.confidence] ?? "bg-slate-50 text-slate-600"
                        }`}
                      >
                        {relation.confidence}
                      </span>
                      {relation.source && <span>detector: {relation.source}</span>}
                    </div>
                  </div>
                ))
              ) : (
                <div className="text-sm text-gray-500">No dependency edges are present.</div>
              )}
            </div>
          </div>
        </div>

        <div className="rounded-xl border border-surface-200 bg-white p-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h3 className="text-sm font-semibold text-gray-900">API Knowledge Graph JSON</h3>
              <p className="mt-1 text-xs text-gray-500">
                A structured JSON projection of operation nodes, dependency edges, and graph counts.
              </p>
            </div>
            <span className="text-xs text-gray-500">version {data.integration_version_id}</span>
          </div>
          <pre
            aria-label="API knowledge graph JSON"
            className="mt-4 max-h-[42rem] overflow-auto rounded-lg border border-surface-100 bg-surface-50 p-3 text-[11px]"
          >
            {knowledgeGraphJson}
          </pre>
          <details className="mt-3">
            <summary className="cursor-pointer text-xs font-medium text-caliber-purple">
              Show full graph snapshot JSON
            </summary>
            <pre className="mt-2 max-h-80 overflow-auto rounded-lg border border-surface-100 bg-surface-50 p-3 text-[11px]">
              {JSON.stringify(data, null, 2)}
            </pre>
          </details>
        </div>
      </div>
    </div>
  );
}
