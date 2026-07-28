import { useEffect, useMemo, useState } from "react";

import { caliberApi } from "@/api/caliberApi";
import type {
  Workflow,
  WorkflowImportPreview,
  WorkflowManifest,
  WorkflowVersion,
} from "@/api/workflowTypes";

type ImportResult = Awaited<ReturnType<typeof caliberApi.importWorkflow>>;

interface WorkflowImportDialogProps {
  mode: "import" | "clone";
  sourceWorkflow?: Workflow;
  onClose: () => void;
  onImported: (result: ImportResult) => void;
}

function newestFirst(a: WorkflowVersion, b: WorkflowVersion): number {
  return b.version_number - a.version_number;
}

export function WorkflowImportDialog({
  mode,
  sourceWorkflow,
  onClose,
  onImported,
}: WorkflowImportDialogProps): JSX.Element {
  const [name, setName] = useState(
    mode === "clone" && sourceWorkflow ? `${sourceWorkflow.name} Copy` : "",
  );
  const [rawManifest, setRawManifest] = useState("");
  const [versions, setVersions] = useState<WorkflowVersion[]>([]);
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const [loadingVersions, setLoadingVersions] = useState(mode === "clone");
  const [preview, setPreview] = useState<WorkflowImportPreview | null>(null);
  const [error, setError] = useState("");
  const [validating, setValidating] = useState(false);
  const [importing, setImporting] = useState(false);

  useEffect(() => {
    if (mode !== "clone" || !sourceWorkflow) return;
    let cancelled = false;
    setLoadingVersions(true);
    caliberApi
      .listWorkflowVersions(sourceWorkflow.workflow_id)
      .then((items) => {
        if (cancelled) return;
        const sorted = [...items].sort(newestFirst);
        setVersions(sorted);
        setSelectedVersionId(sorted[0]?.version_id ?? "");
      })
      .catch((err: Error) => {
        if (!cancelled)
          setError(`Could not load source versions: ${err.message}`);
      })
      .finally(() => {
        if (!cancelled) setLoadingVersions(false);
      });
    return () => {
      cancelled = true;
    };
  }, [mode, sourceWorkflow]);

  const selectedVersion = useMemo(
    () => versions.find((version) => version.version_id === selectedVersionId),
    [selectedVersionId, versions],
  );

  const payload = (): {
    manifest?: WorkflowManifest;
    manifest_yaml?: string;
    name?: string;
  } => {
    const override = name.trim() || undefined;
    if (mode === "clone") {
      return { manifest: selectedVersion?.manifest, name: override };
    }
    return { manifest_yaml: rawManifest, name: override };
  };

  const resetPreview = (): void => {
    setPreview(null);
    setError("");
  };

  const canValidate =
    mode === "clone"
      ? Boolean(selectedVersion?.manifest && name.trim())
      : Boolean(rawManifest.trim());

  const validate = async (): Promise<void> => {
    setValidating(true);
    setError("");
    try {
      setPreview(await caliberApi.previewWorkflowImport(payload()));
    } catch (err) {
      setPreview(null);
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setValidating(false);
    }
  };

  const persist = async (): Promise<void> => {
    if (!preview?.ready_to_import) return;
    setImporting(true);
    setError("");
    try {
      onImported(await caliberApi.importWorkflow(payload()));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setImporting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-sm"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !importing) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="workflow-import-title"
        className="max-h-[90vh] w-full max-w-3xl overflow-y-auto rounded-2xl border border-slate-200 bg-white p-6 shadow-xl"
      >
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2
              id="workflow-import-title"
              className="text-lg font-bold text-slate-900"
            >
              {mode === "clone"
                ? "Clone workflow as new"
                : "Import workflow manifest"}
            </h2>
            <p className="mt-1 text-xs leading-relaxed text-slate-500">
              A new workflow ID and draft version are created. Referenced
              prompts, skills, tools, datasets, MCP tools, managed files, and
              child workflows stay linked; they are not copied.
            </p>
          </div>
          <button
            type="button"
            aria-label="Close import dialog"
            className="rounded-lg p-2 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
            onClick={onClose}
            disabled={importing}
          >
            ✕
          </button>
        </div>

        <div className="mt-5 grid gap-4 sm:grid-cols-2">
          <label className="text-xs font-semibold text-slate-700">
            New workflow name {mode === "clone" ? "" : "(optional)"}
            <input
              data-testid="workflow-import-name"
              className="form-input mt-1.5 w-full"
              value={name}
              onChange={(event) => {
                setName(event.target.value);
                resetPreview();
              }}
              placeholder="Use the manifest name"
            />
          </label>

          {mode === "clone" && (
            <label className="text-xs font-semibold text-slate-700">
              Source version
              <select
                data-testid="workflow-clone-version"
                className="form-input mt-1.5 w-full"
                value={selectedVersionId}
                onChange={(event) => {
                  setSelectedVersionId(event.target.value);
                  resetPreview();
                }}
                disabled={loadingVersions}
              >
                {versions.map((version) => (
                  <option key={version.version_id} value={version.version_id}>
                    v{version.version_number} · {version.status}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>

        {mode === "import" && (
          <div className="mt-4">
            <div className="flex items-center justify-between gap-3">
              <label
                htmlFor="workflow-manifest"
                className="text-xs font-semibold text-slate-700"
              >
                YAML or JSON manifest
              </label>
              <label className="cursor-pointer rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-semibold text-slate-600 hover:bg-slate-50">
                Choose file
                <input
                  className="sr-only"
                  type="file"
                  accept=".yaml,.yml,.json,application/json,text/yaml"
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    if (!file) return;
                    void file.text().then((text) => {
                      setRawManifest(text);
                      resetPreview();
                    });
                  }}
                />
              </label>
            </div>
            <textarea
              id="workflow-manifest"
              data-testid="workflow-import-manifest"
              className="form-input mt-1.5 min-h-56 w-full font-mono text-xs"
              value={rawManifest}
              onChange={(event) => {
                setRawManifest(event.target.value);
                resetPreview();
              }}
              placeholder="schema_version: 1\nworkflow_id: source-id\nname: My workflow\n…"
            />
          </div>
        )}

        {mode === "clone" && !loadingVersions && versions.length === 0 && (
          <p className="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
            This workflow has no saved version to clone.
          </p>
        )}
        {error && (
          <p
            role="alert"
            className="mt-4 rounded-xl border border-red-200 bg-red-50 p-3 text-xs text-red-700"
          >
            {error}
          </p>
        )}

        {preview && (
          <div data-testid="workflow-import-preview" className="mt-5 space-y-4">
            <div
              className={`rounded-xl border p-4 ${
                preview.ready_to_import
                  ? "border-emerald-200 bg-emerald-50"
                  : "border-amber-200 bg-amber-50"
              }`}
            >
              <p className="text-sm font-bold text-slate-900">
                {preview.ready_to_import
                  ? "Preflight passed"
                  : "Preflight needs attention"}
              </p>
              <p className="mt-1 text-xs text-slate-600">
                {preview.node_count} nodes · {preview.edge_count} edges · source
                ID <code>{preview.source_workflow_id}</code>
              </p>
            </div>

            {preview.validation.errors.length > 0 && (
              <section>
                <h3 className="text-xs font-bold uppercase tracking-wide text-red-700">
                  Blocking errors
                </h3>
                <ul className="mt-2 space-y-1.5 text-xs text-red-700">
                  {preview.validation.errors.map((issue, index) => (
                    <li key={`${issue.code}-${issue.path}-${index}`}>
                      <code>{issue.path || "manifest"}</code>: {issue.message}
                    </li>
                  ))}
                </ul>
              </section>
            )}

            <section>
              <h3 className="text-xs font-bold uppercase tracking-wide text-slate-600">
                Dependency mapping
              </h3>
              {preview.dependencies.length === 0 ? (
                <p className="mt-2 text-xs text-slate-500">
                  No external dependencies declared.
                </p>
              ) : (
                <div className="mt-2 overflow-hidden rounded-xl border border-slate-200">
                  {preview.dependencies.map((dependency, index) => (
                    <div
                      key={`${dependency.kind}-${dependency.path}-${index}`}
                      className="grid gap-1 border-b border-slate-100 px-3 py-2 text-xs last:border-0 sm:grid-cols-[8rem_1fr_7rem]"
                    >
                      <span className="font-semibold text-slate-600">
                        {dependency.kind}
                      </span>
                      <span className="min-w-0">
                        <span className="block truncate font-mono text-slate-800">
                          {dependency.reference}
                          {dependency.version ? ` @ ${dependency.version}` : ""}
                        </span>
                        <span className="block text-slate-500">
                          {dependency.detail}
                        </span>
                      </span>
                      <span
                        className={`h-fit justify-self-start rounded-full px-2 py-0.5 font-semibold sm:justify-self-end ${
                          dependency.status === "resolved"
                            ? "bg-emerald-100 text-emerald-700"
                            : dependency.status === "unresolved"
                              ? "bg-red-100 text-red-700"
                              : "bg-amber-100 text-amber-700"
                        }`}
                      >
                        {dependency.status}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </section>
          </div>
        )}

        <div className="mt-6 flex justify-end gap-3 border-t border-slate-100 pt-4">
          <button
            type="button"
            className="btn-secondary"
            onClick={onClose}
            disabled={importing}
          >
            Cancel
          </button>
          <button
            type="button"
            data-testid="workflow-import-validate"
            className="btn-secondary"
            disabled={!canValidate || validating || importing}
            onClick={() => void validate()}
          >
            {validating ? "Validating…" : "Validate dependencies"}
          </button>
          <button
            type="button"
            data-testid="workflow-import-submit"
            className="btn-primary"
            disabled={!preview?.ready_to_import || importing || validating}
            onClick={() => void persist()}
          >
            {importing
              ? "Creating…"
              : mode === "clone"
                ? "Clone as new"
                : "Import as new"}
          </button>
        </div>
      </div>
    </div>
  );
}
