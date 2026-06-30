/**
 * Eval Datasets — list of versioned evaluation datasets.
 *
 * Reuses the same chrome conventions as Skills. A future detail page
 * will show the example list + supersede actions; this slice ships
 * the list + create.
 */

import { useCallback, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, caliberApi } from "@/api/caliberApi";
import { ClearFiltersButton } from "@/components/ClearFiltersButton";
import { FilterBar } from "@/components/FilterBar";
import { FilterSelect } from "@/components/FilterSelect";
import { SearchInput } from "@/components/SearchInput";
import type {
  EvalDataset,
  EvalDatasetCreatePayload,
  ResourceStatus,
} from "@/api/types";
import { useApi } from "@/hooks/useApi";
import { relativeTime } from "@/lib/time";

type StatusFilter = "active" | "archived" | "all";

export function EvalDatasets(): JSX.Element {
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("active");
  const fetcher = useCallback(
    (signal: AbortSignal) =>
      caliberApi.listEvalDatasets({ status: statusFilter }, signal),
    [statusFilter],
  );
  const { data, error, loading, refresh } = useApi(fetcher, [statusFilter]);
  const [showCreate, setShowCreate] = useState(false);
  const [pending, setPending] = useState<string | null>(null);
  const [syncing, setSyncing] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  // Client-side text filter over the rows already fetched for the active status;
  // the backend ``?status=`` fetch is untouched. Empty query = show everything.
  const [search, setSearch] = useState("");
  const [ownerFilter, setOwnerFilter] = useState("");
  const [tagFilter, setTagFilter] = useState("");
  const query = search.trim().toLowerCase();
  const ownerOptions = Array.from(new Set((data ?? []).map((dataset) => dataset.owner)))
    .filter(Boolean)
    .sort()
    .map((owner) => ({ value: owner, label: owner }));
  const tagOptions = Array.from(new Set((data ?? []).flatMap((dataset) => dataset.tags)))
    .filter(Boolean)
    .sort()
    .map((tag) => ({ value: tag, label: tag }));
  const visibleDatasets = (data ?? []).filter((dataset) => {
    if (ownerFilter && dataset.owner !== ownerFilter) return false;
    if (tagFilter && !dataset.tags.includes(tagFilter)) return false;
    if (!query) return true;
    return [dataset.name, dataset.description, dataset.owner, ...dataset.tags]
      .filter(Boolean)
      .some((field) => String(field).toLowerCase().includes(query));
  });
  const hasDatasetFilters = Boolean(
    search || ownerFilter || tagFilter || statusFilter !== "active",
  );

  const setStatus = async (
    dataset: EvalDataset,
    status: ResourceStatus,
  ): Promise<void> => {
    setPending(dataset.dataset_id);
    setActionError(null);
    try {
      await caliberApi.updateEvalDataset(dataset.dataset_id, { status });
      refresh();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "update failed");
    } finally {
      setPending(null);
    }
  };

  const runSync = async (dataset: EvalDataset): Promise<void> => {
    setSyncing(dataset.dataset_id);
    setActionError(null);
    try {
      await caliberApi.syncEvalDataset(dataset.dataset_id);
      refresh();
    } catch (err) {
      setActionError(
        err instanceof ApiError ? err.message : "sync to MLflow failed",
      );
    } finally {
      setSyncing(null);
    }
  };

  return (
    <>
      <div className="flex items-center gap-2 text-sm text-gray-500 mb-4">
        <Link to="/" className="hover:text-gray-700">
          Dashboard
        </Link>
        <Chevron />
        <span className="text-gray-900 font-medium">Test Sets</span>
      </div>

      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-semibold text-gray-900">Test Sets</h1>
          <p className="text-sm text-gray-500 mt-0.5">
            Versioned input/expected sets the refinement pipeline scores
            candidates against.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setShowCreate((v) => !v)}
          className="text-sm font-medium text-white bg-caliber-purple px-3 py-1.5 rounded-md hover:bg-caliber-purple-dark"
        >
          {showCreate ? "Cancel" : "+ New Test Set"}
        </button>
      </div>

      {error && (
        <div className="mb-4 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          <div className="font-medium">Failed to load test sets</div>
          <div className="text-xs mt-0.5">{error.message}</div>
        </div>
      )}

      {actionError && (
        <div className="mb-4 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {actionError}
        </div>
      )}

      {showCreate && (
        <CreateDatasetPanel
          onCancel={() => setShowCreate(false)}
          onSuccess={() => {
            setShowCreate(false);
            refresh();
          }}
        />
      )}

      <div className="mb-4 flex flex-col gap-3">
        <FilterTabs current={statusFilter} onChange={setStatusFilter} />
        <FilterBar
          search={
            <SearchInput
              value={search}
              onChange={setSearch}
              ariaLabel="Search test sets"
              placeholder="Search by name, owner, tag…"
              className="w-full"
            />
          }
          filters={
            <>
              <FilterSelect
                label="Owner"
                allLabel="All owners"
                value={ownerFilter}
                onChange={setOwnerFilter}
                options={ownerOptions}
                className="w-full sm:w-44"
              />
              <FilterSelect
                label="Tag"
                allLabel="All tags"
                value={tagFilter}
                onChange={setTagFilter}
                options={tagOptions}
                className="w-full sm:w-44"
              />
            </>
          }
          actions={
            <ClearFiltersButton
              visible={hasDatasetFilters}
              onClear={() => {
                setStatusFilter("active");
                setSearch("");
                setOwnerFilter("");
                setTagFilter("");
              }}
            />
          }
        />
      </div>

      <div className="bg-white rounded-lg border border-surface-200 overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-surface-200 bg-surface-50">
              <th className="text-left font-medium px-4 py-3">Name</th>
              <th className="text-left font-medium px-4 py-3">Owner</th>
              <th className="text-left font-medium px-4 py-3">Tags</th>
              <th className="text-left font-medium px-4 py-3">Version</th>
              <th className="text-left font-medium px-4 py-3">Status</th>
              <th className="text-left font-medium px-4 py-3">MLflow</th>
              <th className="text-left font-medium px-4 py-3">Updated</th>
              <th className="text-right font-medium px-4 py-3">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-surface-100">
            {loading && !data && (
              <tr>
                <td colSpan={8} className="px-4 py-10 text-center text-sm text-gray-500">
                  Loading…
                </td>
              </tr>
            )}
            {data && visibleDatasets.length === 0 && (
              <tr>
                <td colSpan={8} className="px-4 py-10 text-center text-sm text-gray-500">
                  {query
                    ? `No test sets match “${search.trim()}”.`
                    : "No test sets in this view."}
                </td>
              </tr>
            )}
            {visibleDatasets.map((dataset) => (
              <tr key={dataset.dataset_id} className="hover:bg-surface-50">
                <td className="px-4 py-3">
                  <Link
                    to={`/eval-datasets/${dataset.dataset_id}`}
                    className="font-medium text-gray-900 hover:text-caliber-purple hover:underline"
                  >
                    {dataset.name}
                  </Link>
                  <div className="text-xs text-gray-500 mt-0.5 truncate max-w-md">
                    {dataset.description || "—"}
                  </div>
                </td>
                <td className="px-4 py-3 text-gray-600">{dataset.owner}</td>
                <td className="px-4 py-3">
                  <div className="flex gap-1 flex-wrap">
                    {dataset.tags.map((tag) => (
                      <span
                        key={tag}
                        className="text-xs bg-surface-100 text-gray-600 px-1.5 py-0.5 rounded"
                      >
                        {tag}
                      </span>
                    ))}
                  </div>
                </td>
                <td className="px-4 py-3 text-gray-600 font-mono text-xs">
                  v{dataset.version}
                </td>
                <td className="px-4 py-3">
                  <StatusPill status={dataset.status} />
                </td>
                <td className="px-4 py-3">
                  <MLflowSyncBadge dataset={dataset} />
                </td>
                <td className="px-4 py-3 text-xs text-gray-500">
                  {relativeTime(dataset.updated_at)}
                </td>
                <td className="px-4 py-3 text-right">
                  <div className="flex items-center justify-end gap-3">
                    <button
                      type="button"
                      disabled={syncing === dataset.dataset_id}
                      onClick={() => void runSync(dataset)}
                      title="Push the current example set to MLflow's GenAI dataset registry"
                      className="text-xs font-medium text-mlflow-blue hover:underline disabled:opacity-50"
                    >
                      {syncing === dataset.dataset_id
                        ? "Syncing…"
                        : isStale(dataset)
                          ? "Re-sync"
                          : "Sync to MLflow"}
                    </button>
                    <button
                      type="button"
                      disabled={pending === dataset.dataset_id}
                      onClick={() =>
                        void setStatus(
                          dataset,
                          dataset.status === "active" ? "archived" : "active",
                        )
                      }
                      className="text-xs font-medium text-caliber-purple hover:underline disabled:opacity-50"
                    >
                      {dataset.status === "active" ? "Archive" : "Restore"}
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function FilterTabs({
  current,
  onChange,
}: {
  current: StatusFilter;
  onChange: (value: StatusFilter) => void;
}): JSX.Element {
  const tabs: StatusFilter[] = ["active", "archived", "all"];
  return (
    <div className="flex gap-2 text-sm">
      {tabs.map((t) => (
        <button
          key={t}
          type="button"
          onClick={() => onChange(t)}
          className={`px-3 py-1 rounded-md ${
            current === t
              ? "bg-caliber-purple text-white"
              : "bg-surface-100 text-gray-600 hover:bg-surface-200"
          }`}
        >
          {t.charAt(0).toUpperCase() + t.slice(1)}
        </button>
      ))}
    </div>
  );
}

function StatusPill({ status }: { status: ResourceStatus }): JSX.Element {
  const cls =
    status === "active"
      ? "bg-emerald-100 text-emerald-700"
      : "bg-gray-200 text-gray-600";
  return (
    <span className={`text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded ${cls}`}>
      {status}
    </span>
  );
}

/** A dataset is "stale" when its examples changed after the last MLflow sync. */
function isStale(dataset: EvalDataset): boolean {
  return (
    dataset.mlflow_dataset_id != null &&
    dataset.mlflow_synced_version != null &&
    dataset.mlflow_synced_version < dataset.version
  );
}

/**
 * MLflow GenAI dataset sync state, shown as a small pill. This is registry
 * PARITY, not which dataset version is active — labelled accordingly so it is
 * not mistaken for version liveness (Q2):
 *  • never synced  → neutral "Not synced"
 *  • synced + current → "MLflow: up to date · vN"
 *  • synced but examples changed since → amber "MLflow: behind · vN"
 */
function MLflowSyncBadge({ dataset }: { dataset: EvalDataset }): JSX.Element {
  if (dataset.mlflow_dataset_id == null) {
    return (
      <span className="inline-flex items-center gap-1 text-[10px] font-medium text-gray-400">
        <Dot className="bg-gray-300" />
        Not synced
      </span>
    );
  }
  const stale = isStale(dataset);
  const recordHint =
    dataset.mlflow_record_count != null
      ? `${dataset.mlflow_record_count} record${dataset.mlflow_record_count === 1 ? "" : "s"} · `
      : "";
  return (
    <span
      title={`${recordHint}MLflow dataset ${dataset.mlflow_dataset_id}${
        dataset.mlflow_synced_at ? ` · synced ${relativeTime(dataset.mlflow_synced_at)}` : ""
      }`}
      className={`inline-flex items-center gap-1 text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded ${
        stale ? "bg-amber-100 text-amber-700" : "bg-mlflow-blue/10 text-mlflow-blue"
      }`}
    >
      <Dot className={stale ? "bg-amber-500" : "bg-mlflow-blue"} />
      {/* "MLflow sync" parity, NOT version liveness — relabeled so it isn't read
          as which dataset version is active (Q2). */}
      {stale ? "MLflow: behind" : "MLflow: up to date"}
      {dataset.mlflow_synced_version != null && (
        <span className="font-mono normal-case opacity-80">
          v{dataset.mlflow_synced_version}
        </span>
      )}
    </span>
  );
}

function Dot({ className }: { className: string }): JSX.Element {
  return <span className={`h-1.5 w-1.5 rounded-full ${className}`} aria-hidden />;
}

interface CreatePanelProps {
  onCancel: () => void;
  onSuccess: () => void;
}

function CreateDatasetPanel({ onCancel, onSuccess }: CreatePanelProps): JSX.Element {
  const [form, setForm] = useState<EvalDatasetCreatePayload>({
    name: "",
    description: "",
    owner: "",
    tags: [],
  });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (): Promise<void> => {
    setSubmitting(true);
    setError(null);
    try {
      await caliberApi.createEvalDataset(form);
      onSuccess();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "create failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="mb-6 bg-white rounded-lg border border-surface-200 p-4">
      <h2 className="text-sm font-semibold text-gray-900 mb-3">New test set</h2>
      <div className="grid grid-cols-2 gap-3">
        <Field
          label="Name"
          value={form.name}
          onChange={(v) => setForm({ ...form, name: v })}
          placeholder="factual-checks"
        />
        <Field
          label="Owner"
          value={form.owner}
          onChange={(v) => setForm({ ...form, owner: v })}
          placeholder="@sarah"
        />
        <div className="col-span-2">
          <Field
            label="Description"
            value={form.description ?? ""}
            onChange={(v) => setForm({ ...form, description: v })}
            placeholder="Curated Q&A pairs that test factual recall."
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
          disabled={submitting || !form.name || !form.owner}
          onClick={() => void submit()}
          className="text-sm font-medium text-white bg-caliber-purple px-3 py-1.5 rounded-md hover:bg-caliber-purple-dark disabled:opacity-50"
        >
          {submitting ? "Creating…" : "Create"}
        </button>
      </div>
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
}): JSX.Element {
  return (
    <div>
      <label className="text-xs text-gray-500 block mb-1">{label}</label>
      <input
        className="w-full border border-surface-200 rounded-md px-3 py-1.5 text-sm"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
      />
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
