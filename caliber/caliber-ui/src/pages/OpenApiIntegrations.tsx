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
    <>
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
    </>
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
    <>
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
    </>
  );
}

/* -------------------------------------------------------------------------- */
/* Import tab                                                                  */
/* -------------------------------------------------------------------------- */

type SourceKind = "inline_text" | "upload" | "url";

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
        const base64 = btoa(String.fromCharCode(...new Uint8Array(buffer)));
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

function DependenciesTab({ integrationId }: { integrationId: string }): JSX.Element {
  const fetcher = useCallback(
    (signal: AbortSignal) => caliberApi.listOpenApiDependencies(integrationId, undefined, signal),
    [integrationId],
  );
  const { data, error, loading, refresh } = useApi(fetcher, [integrationId]);

  const review = async (dependencyId: string, status: "confirmed" | "rejected"): Promise<void> => {
    await caliberApi.reviewOpenApiDependency(integrationId, dependencyId, { status });
    refresh();
  };

  if (loading && !data) return <div className="text-sm text-gray-500">Loading…</div>;
  if (error) return <div className="text-sm text-red-600">{error.message}</div>;

  return (
    <div className="bg-white rounded-lg border border-surface-200 overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-surface-200 bg-surface-50">
            <th className="text-left font-medium px-4 py-3">Type</th>
            <th className="text-left font-medium px-4 py-3">Confidence</th>
            <th className="text-left font-medium px-4 py-3">Source</th>
            <th className="text-left font-medium px-4 py-3">Status</th>
            <th className="text-left font-medium px-4 py-3">Notes</th>
            <th className="text-right font-medium px-4 py-3">Actions</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-surface-100">
          {(data ?? []).map((dep: OpenApiOperationDependency) => (
            <tr key={dep.dependency_id} className="hover:bg-surface-50">
              <td className="px-4 py-3 font-mono text-xs text-gray-900">{dep.dependency_type}</td>
              <td className="px-4 py-3">
                <span
                  className={`text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded ${
                    CONFIDENCE_COLOR[dep.confidence] ?? "bg-slate-50 text-slate-600"
                  }`}
                >
                  {dep.confidence}
                </span>
              </td>
              <td className="px-4 py-3 text-gray-500 text-xs">{dep.source}</td>
              <td className="px-4 py-3 text-gray-600 text-xs">{dep.status}</td>
              <td className="px-4 py-3 text-gray-400 text-xs max-w-xs truncate">{dep.notes || "—"}</td>
              <td className="px-4 py-3 text-right">
                {(dep.status === "suggested" || dep.status === "advisory") && (
                  <div className="flex justify-end gap-2">
                    <button
                      type="button"
                      onClick={() => review(dep.dependency_id, "confirmed")}
                      className="text-xs font-medium text-emerald-600 hover:underline"
                    >
                      Confirm
                    </button>
                    <button
                      type="button"
                      onClick={() => review(dep.dependency_id, "rejected")}
                      className="text-xs font-medium text-red-600 hover:underline"
                    >
                      Reject
                    </button>
                  </div>
                )}
              </td>
            </tr>
          ))}
          {data && data.length === 0 && (
            <tr>
              <td colSpan={6} className="px-4 py-10 text-center text-sm text-gray-500">
                No dependencies detected yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
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
      setMessage(`Published as tool ${result.tool.tool_id}.`);
      onChanged();
    } catch (err) {
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

          {message && <div className="text-xs text-gray-600">{message}</div>}

          <div className="flex justify-end">
            {draft.published_tool_id ? (
              <span className="text-xs text-emerald-700">
                Published as tool {draft.published_tool_id}
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

function GraphTab({ integrationId }: { integrationId: string }): JSX.Element {
  const fetcher = useCallback(
    (signal: AbortSignal) => caliberApi.getOpenApiGraph(integrationId, undefined, signal),
    [integrationId],
  );
  const { data, error, loading } = useApi(fetcher, [integrationId]);
  const [showRaw, setShowRaw] = useState(false);

  const byType = useMemo(() => {
    if (!data) return new Map<string, number>();
    const counts = new Map<string, number>();
    for (const node of data.nodes) {
      counts.set(node.type, (counts.get(node.type) ?? 0) + 1);
    }
    return counts;
  }, [data]);

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

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-4">
        {Array.from(byType.entries()).map(([type, count]) => (
          <div key={type} className="rounded-lg border border-surface-200 bg-white p-3 text-center">
            <div className="text-lg font-semibold text-gray-900">{count}</div>
            <div className="text-[10px] uppercase text-gray-500 tracking-wide">{type}</div>
          </div>
        ))}
      </div>

      <button
        type="button"
        onClick={() => setShowRaw((v) => !v)}
        className="text-xs font-medium text-caliber-purple hover:underline mb-2"
      >
        {showRaw ? "Hide raw graph JSON" : "Show raw graph JSON"}
      </button>
      {showRaw && (
        <pre className="rounded-md bg-surface-50 border border-surface-100 p-3 text-[11px] overflow-x-auto max-h-96">
          {JSON.stringify(data, null, 2)}
        </pre>
      )}
    </div>
  );
}
