/**
 * Eval Dataset detail — the curation surface for one Test Set.
 *
 * Lists the dataset's examples and lets an operator hand-build a golden set:
 * add a row, edit a row (append-only → supersede + replace atomically via the
 * /revise endpoint), retire a row, capture a row from an observed trace, filter
 * to a historical version, reveal retired rows, and push the set to MLflow.
 *
 * Reuses the list page's chrome conventions (caliber-purple / surface tokens).
 */

import { useCallback, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiError, caliberApi } from "@/api/caliberApi";
import type {
  EvalDataset,
  EvalExample,
  EvalExampleCreatePayload,
} from "@/api/types";
import { useApi } from "@/hooks/useApi";
import { relativeTime } from "@/lib/time";

/** Parse a JSON object from a textarea, returning the dict or an error message. */
function parseJsonObject(text: string): { value: Record<string, unknown> } | { error: string } {
  const trimmed = text.trim();
  if (!trimmed) return { value: {} };
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return { error: "Not valid JSON." };
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return { error: "Must be a JSON object, e.g. { \"input\": \"…\" }." };
  }
  return { value: parsed as Record<string, unknown> };
}

function prettyJson(value: Record<string, unknown>): string {
  return JSON.stringify(value, null, 2);
}

export function EvalDatasetDetail(): JSX.Element {
  const { datasetId = "" } = useParams<{ datasetId: string }>();

  const datasetFetcher = useCallback(
    (signal: AbortSignal) => caliberApi.getEvalDataset(datasetId, signal),
    [datasetId],
  );
  const {
    data: dataset,
    error: datasetError,
    loading: datasetLoading,
    refresh: refreshDataset,
  } = useApi(datasetFetcher, [datasetId]);

  const [versionFilter, setVersionFilter] = useState<number | "">("");
  const [showRetired, setShowRetired] = useState(false);

  const examplesFetcher = useCallback(
    (signal: AbortSignal) =>
      caliberApi.listEvalExamples(
        datasetId,
        {
          version: versionFilter === "" ? undefined : versionFilter,
          includeSuperseded: showRetired,
        },
        signal,
      ),
    [datasetId, versionFilter, showRetired],
  );
  const {
    data: examples,
    error: examplesError,
    loading: examplesLoading,
    refresh: refreshExamples,
  } = useApi(examplesFetcher, [datasetId, versionFilter, showRetired]);

  const [mode, setMode] = useState<"none" | "add" | "trace">("none");
  const [editing, setEditing] = useState<EvalExample | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);

  const refreshAll = useCallback(() => {
    refreshDataset();
    refreshExamples();
  }, [refreshDataset, refreshExamples]);

  const closeEditors = useCallback(() => {
    setMode("none");
    setEditing(null);
  }, []);

  const retire = async (example: EvalExample): Promise<void> => {
    setPending(example.example_id);
    setActionError(null);
    try {
      await caliberApi.supersedeEvalExample(datasetId, example.example_id);
      refreshAll();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "retire failed");
    } finally {
      setPending(null);
    }
  };

  const runSync = async (): Promise<void> => {
    setSyncing(true);
    setActionError(null);
    try {
      await caliberApi.syncEvalDataset(datasetId);
      refreshDataset();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "sync to MLflow failed");
    } finally {
      setSyncing(false);
    }
  };

  const rows = useMemo(() => examples ?? [], [examples]);
  const liveCount = useMemo(
    () => rows.filter((row) => row.superseded_at === null).length,
    [rows],
  );

  return (
    <div data-testid="eval-dataset-detail">
      <div className="flex items-center gap-2 text-sm text-gray-500 mb-4">
        <Link to="/" className="hover:text-gray-700">
          Dashboard
        </Link>
        <Chevron />
        <Link to="/eval-datasets" className="hover:text-gray-700">
          Test Sets
        </Link>
        <Chevron />
        <span className="text-gray-900 font-medium" data-testid="eval-dataset-name">
          {dataset?.name ?? datasetId}
        </span>
      </div>

      {datasetError && (
        <div className="mb-4 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          <div className="font-medium">Failed to load test set</div>
          <div className="text-xs mt-0.5">{datasetError.message}</div>
        </div>
      )}

      {dataset && <DatasetHeader dataset={dataset} liveCount={liveCount} syncing={syncing} onSync={() => void runSync()} />}

      {actionError && (
        <div className="mb-4 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {actionError}
        </div>
      )}

      {/* Toolbar */}
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <button
            type="button"
            data-testid="example-add-toggle"
            onClick={() => {
              setEditing(null);
              setMode((m) => (m === "add" ? "none" : "add"));
            }}
            className="text-sm font-medium text-white bg-caliber-purple px-3 py-1.5 rounded-md hover:bg-caliber-purple-dark"
          >
            {mode === "add" ? "Cancel" : "+ Add example"}
          </button>
          <button
            type="button"
            data-testid="from-trace-toggle"
            onClick={() => {
              setEditing(null);
              setMode((m) => (m === "trace" ? "none" : "trace"));
            }}
            className="text-sm font-medium text-gray-600 border border-surface-200 px-3 py-1.5 rounded-md hover:bg-surface-100"
          >
            {mode === "trace" ? "Cancel" : "Capture from trace"}
          </button>
        </div>
        <div className="flex items-center gap-3 text-sm">
          <label className="flex items-center gap-1.5 text-gray-600">
            Version
            <select
              data-testid="version-filter"
              aria-label="Filter by version"
              className="border border-surface-200 rounded-md px-2 py-1 text-sm"
              value={versionFilter}
              onChange={(e) =>
                setVersionFilter(e.target.value === "" ? "" : Number(e.target.value))
              }
            >
              <option value="">All</option>
              {dataset &&
                Array.from({ length: dataset.version }, (_, i) => dataset.version - i).map(
                  (v) => (
                    <option key={v} value={v}>
                      v{v}
                    </option>
                  ),
                )}
            </select>
          </label>
          <label className="flex items-center gap-1.5 text-gray-600">
            <input
              type="checkbox"
              data-testid="show-retired-toggle"
              checked={showRetired}
              onChange={(e) => setShowRetired(e.target.checked)}
            />
            Show retired
          </label>
        </div>
      </div>

      {mode === "add" && !editing && (
        <ExampleEditor
          title="Add example"
          onCancel={closeEditors}
          onSubmit={async (payload) => {
            await caliberApi.appendEvalExample(datasetId, payload);
            closeEditors();
            refreshAll();
          }}
        />
      )}

      {mode === "trace" && (
        <FromTraceEditor
          onCancel={closeEditors}
          onSubmit={async (traceId) => {
            await caliberApi.addEvalExampleFromTrace(datasetId, { trace_id: traceId });
            closeEditors();
            refreshAll();
          }}
        />
      )}

      {editing && (
        <ExampleEditor
          title={`Edit example ${editing.example_id}`}
          initial={editing}
          onCancel={closeEditors}
          onSubmit={async (payload) => {
            await caliberApi.reviseEvalExample(datasetId, editing.example_id, payload);
            closeEditors();
            refreshAll();
          }}
        />
      )}

      {examplesError && (
        <div className="mb-4 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {examplesError.message}
        </div>
      )}

      <div className="bg-white rounded-lg border border-surface-200 overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-surface-200 bg-surface-50">
              <th className="text-left font-medium px-4 py-3">Input</th>
              <th className="text-left font-medium px-4 py-3">Expected</th>
              <th className="text-left font-medium px-4 py-3">Weight</th>
              <th className="text-left font-medium px-4 py-3">Tags</th>
              <th className="text-left font-medium px-4 py-3">Added</th>
              <th className="text-right font-medium px-4 py-3">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-surface-100">
            {examplesLoading && !examples && (
              <tr>
                <td colSpan={6} className="px-4 py-10 text-center text-sm text-gray-500">
                  Loading…
                </td>
              </tr>
            )}
            {examples && rows.length === 0 && (
              <tr>
                <td
                  colSpan={6}
                  data-testid="examples-empty"
                  className="px-4 py-10 text-center text-sm text-gray-500"
                >
                  No examples yet. Add one by hand or capture one from a trace.
                </td>
              </tr>
            )}
            {rows.map((row) => (
              <tr
                key={row.example_id}
                data-testid="example-row"
                className={`hover:bg-surface-50 align-top ${
                  row.superseded_at ? "opacity-55" : ""
                }`}
              >
                <td className="px-4 py-3">
                  <pre className="font-mono text-xs text-gray-700 whitespace-pre-wrap break-words max-w-xs">
                    {prettyJson(row.input)}
                  </pre>
                </td>
                <td className="px-4 py-3">
                  <pre className="font-mono text-xs text-gray-700 whitespace-pre-wrap break-words max-w-xs">
                    {prettyJson(row.expected)}
                  </pre>
                </td>
                <td className="px-4 py-3 text-gray-600 font-mono text-xs">{row.weight}</td>
                <td className="px-4 py-3">
                  <div className="flex gap-1 flex-wrap">
                    {row.tags.map((tag) => (
                      <span
                        key={tag}
                        className="text-xs bg-surface-100 text-gray-600 px-1.5 py-0.5 rounded"
                      >
                        {tag}
                      </span>
                    ))}
                    {row.superseded_at && (
                      <span className="text-[10px] font-semibold uppercase bg-gray-200 text-gray-500 px-1.5 py-0.5 rounded">
                        retired v{row.superseded_version ?? "?"}
                      </span>
                    )}
                  </div>
                </td>
                <td className="px-4 py-3 text-xs text-gray-500">
                  v{row.dataset_version} · {relativeTime(row.created_at)}
                </td>
                <td className="px-4 py-3 text-right">
                  {row.superseded_at ? (
                    <span className="text-xs text-gray-400">—</span>
                  ) : (
                    <div className="flex items-center justify-end gap-3">
                      <button
                        type="button"
                        data-testid="example-edit"
                        onClick={() => {
                          setMode("none");
                          setEditing(row);
                        }}
                        className="text-xs font-medium text-caliber-purple hover:underline"
                      >
                        Edit
                      </button>
                      <button
                        type="button"
                        data-testid="example-retire"
                        disabled={pending === row.example_id}
                        onClick={() => void retire(row)}
                        className="text-xs font-medium text-red-600 hover:underline disabled:opacity-50"
                      >
                        {pending === row.example_id ? "Retiring…" : "Retire"}
                      </button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {datasetLoading && !dataset && (
        <div className="mt-4 text-center text-sm text-gray-400">Loading test set…</div>
      )}
    </div>
  );
}

function DatasetHeader({
  dataset,
  liveCount,
  syncing,
  onSync,
}: {
  dataset: EvalDataset;
  liveCount: number;
  syncing: boolean;
  onSync: () => void;
}): JSX.Element {
  return (
    <div className="mb-6 flex items-start justify-between gap-4">
      <div>
        <div className="flex items-center gap-2">
          <h1 className="text-xl font-semibold text-gray-900">{dataset.name}</h1>
          <span
            data-testid="eval-dataset-version"
            className="text-xs font-mono text-gray-500 bg-surface-100 px-1.5 py-0.5 rounded"
          >
            v{dataset.version}
          </span>
          <span
            className={`text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded ${
              dataset.status === "active"
                ? "bg-emerald-100 text-emerald-700"
                : "bg-gray-200 text-gray-600"
            }`}
          >
            {dataset.status}
          </span>
        </div>
        <p className="text-sm text-gray-500 mt-0.5">{dataset.description || "No description."}</p>
        <p className="text-xs text-gray-400 mt-1">
          {liveCount} active example{liveCount === 1 ? "" : "s"} · owner {dataset.owner}
        </p>
      </div>
      <button
        type="button"
        data-testid="dataset-sync"
        disabled={syncing}
        onClick={onSync}
        title="Push the current example set to MLflow's GenAI dataset registry"
        className="text-sm font-medium text-mlflow-blue border border-surface-200 px-3 py-1.5 rounded-md hover:bg-surface-100 disabled:opacity-50"
      >
        {syncing ? "Syncing…" : "Sync to MLflow"}
      </button>
    </div>
  );
}

interface EditorProps {
  title: string;
  initial?: EvalExample;
  onCancel: () => void;
  onSubmit: (payload: EvalExampleCreatePayload) => Promise<void>;
}

function ExampleEditor({ title, initial, onCancel, onSubmit }: EditorProps): JSX.Element {
  const [inputText, setInputText] = useState(
    initial ? prettyJson(initial.input) : '{\n  "input": ""\n}',
  );
  const [expectedText, setExpectedText] = useState(
    initial ? prettyJson(initial.expected) : '{\n  "expected": ""\n}',
  );
  const [weight, setWeight] = useState(initial ? String(initial.weight) : "1");
  const [tags, setTags] = useState(initial ? initial.tags.join(", ") : "");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const submit = async (): Promise<void> => {
    setError(null);
    const inputParsed = parseJsonObject(inputText);
    if ("error" in inputParsed) {
      setError(`Input: ${inputParsed.error}`);
      return;
    }
    const expectedParsed = parseJsonObject(expectedText);
    if ("error" in expectedParsed) {
      setError(`Expected: ${expectedParsed.error}`);
      return;
    }
    const weightNum = Number(weight);
    if (!Number.isFinite(weightNum) || weightNum < 0) {
      setError("Weight must be a number ≥ 0.");
      return;
    }
    setSubmitting(true);
    try {
      await onSubmit({
        input: inputParsed.value,
        expected: expectedParsed.value,
        weight: weightNum,
        tags: tags
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
      });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "save failed");
      setSubmitting(false);
    }
  };

  return (
    <div
      data-testid="example-editor"
      className="mb-6 bg-white rounded-lg border border-surface-200 p-4"
    >
      <h2 className="text-sm font-semibold text-gray-900 mb-3">{title}</h2>
      <div className="grid grid-cols-2 gap-3">
        <JsonField label="Input (JSON object)" testId="example-input" value={inputText} onChange={setInputText} />
        <JsonField
          label="Expected (JSON object)"
          testId="example-expected"
          value={expectedText}
          onChange={setExpectedText}
        />
      </div>
      <div className="mt-3 grid grid-cols-2 gap-3">
        <div>
          <label className="text-xs text-gray-500 block mb-1">Weight</label>
          <input
            data-testid="example-weight"
            aria-label="Weight"
            type="number"
            min="0"
            step="0.1"
            className="w-full border border-surface-200 rounded-md px-3 py-1.5 text-sm"
            value={weight}
            onChange={(e) => setWeight(e.target.value)}
          />
        </div>
        <div>
          <label className="text-xs text-gray-500 block mb-1">Tags (comma-separated)</label>
          <input
            data-testid="example-tags"
            aria-label="Tags"
            className="w-full border border-surface-200 rounded-md px-3 py-1.5 text-sm"
            value={tags}
            onChange={(e) => setTags(e.target.value)}
            placeholder="golden, edge-case"
          />
        </div>
      </div>
      {error && <div className="mt-3 text-sm text-red-600">{error}</div>}
      <div className="mt-4 flex gap-2 justify-end">
        <button
          type="button"
          onClick={onCancel}
          className="text-sm px-3 py-1.5 rounded-md text-gray-600 hover:bg-surface-100"
        >
          Cancel
        </button>
        <button
          type="button"
          data-testid="example-save"
          disabled={submitting}
          onClick={() => void submit()}
          className="text-sm font-medium text-white bg-caliber-purple px-3 py-1.5 rounded-md hover:bg-caliber-purple-dark disabled:opacity-50"
        >
          {submitting ? "Saving…" : "Save"}
        </button>
      </div>
    </div>
  );
}

function JsonField({
  label,
  testId,
  value,
  onChange,
}: {
  label: string;
  testId: string;
  value: string;
  onChange: (value: string) => void;
}): JSX.Element {
  return (
    <div>
      <label className="text-xs text-gray-500 block mb-1">{label}</label>
      <textarea
        data-testid={testId}
        aria-label={label}
        rows={5}
        className="w-full border border-surface-200 rounded-md px-3 py-1.5 text-sm font-mono"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}

function FromTraceEditor({
  onCancel,
  onSubmit,
}: {
  onCancel: () => void;
  onSubmit: (traceId: string) => Promise<void>;
}): JSX.Element {
  const [traceId, setTraceId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const submit = async (): Promise<void> => {
    setError(null);
    setSubmitting(true);
    try {
      await onSubmit(traceId.trim());
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "capture failed");
      setSubmitting(false);
    }
  };

  return (
    <div className="mb-6 bg-white rounded-lg border border-surface-200 p-4">
      <h2 className="text-sm font-semibold text-gray-900 mb-1">Capture from trace</h2>
      <p className="text-xs text-gray-500 mb-3">
        The trace's request becomes the example input and its response the expected answer.
      </p>
      <input
        data-testid="from-trace-id"
        className="w-full border border-surface-200 rounded-md px-3 py-1.5 text-sm font-mono"
        value={traceId}
        onChange={(e) => setTraceId(e.target.value)}
        placeholder="trace id, e.g. tr-abc123"
      />
      {error && <div className="mt-3 text-sm text-red-600">{error}</div>}
      <div className="mt-4 flex gap-2 justify-end">
        <button
          type="button"
          onClick={onCancel}
          className="text-sm px-3 py-1.5 rounded-md text-gray-600 hover:bg-surface-100"
        >
          Cancel
        </button>
        <button
          type="button"
          data-testid="from-trace-save"
          disabled={submitting || !traceId.trim()}
          onClick={() => void submit()}
          className="text-sm font-medium text-white bg-caliber-purple px-3 py-1.5 rounded-md hover:bg-caliber-purple-dark disabled:opacity-50"
        >
          {submitting ? "Capturing…" : "Capture"}
        </button>
      </div>
    </div>
  );
}

function Chevron(): JSX.Element {
  return (
    <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M9 18l6-6-6-6" />
    </svg>
  );
}
