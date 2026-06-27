/**
 * Audit Log — read-only explorer over the append-only audit trail.
 *
 * Every state change CALIBER makes is written to `caliber_audit_log`; this page
 * is the read surface. Filter by actor / action / entity / date window, page
 * through matches, and export the filtered set as CSV or JSON. Admin-only — the
 * backend returns 403 for non-admins, which we surface inline.
 */

import { useCallback, useMemo, useState } from "react";
import { Download, ScrollText } from "lucide-react";

import { caliberApi } from "@/api/caliberApi";
import type { AuditLogFilters, AuditLogPage } from "@/api/types";
import { CopyButton } from "@/components/CopyButton";
import { PageHeader } from "@/components/PageHeader";
import { useApiQuery } from "@/hooks/useApiQuery";

const PAGE_SIZE = 50;

interface DraftFilters {
  actor: string;
  action: string;
  entity_type: string;
  entity_id: string;
  since: string;
  until: string;
}

const EMPTY_FILTERS: DraftFilters = {
  actor: "",
  action: "",
  entity_type: "",
  entity_id: "",
  since: "",
  until: "",
};

/** Turn the draft inputs into API filters, dropping blanks. */
function toApiFilters(draft: DraftFilters): AuditLogFilters {
  const out: AuditLogFilters = {};
  if (draft.actor.trim()) out.actor = draft.actor.trim();
  if (draft.action.trim()) out.action = draft.action.trim();
  if (draft.entity_type.trim()) out.entity_type = draft.entity_type.trim();
  if (draft.entity_id.trim()) out.entity_id = draft.entity_id.trim();
  if (draft.since.trim()) out.since = draft.since.trim();
  if (draft.until.trim()) out.until = draft.until.trim();
  return out;
}

/** Trigger a browser download of a fetched Blob (no-op in non-DOM test envs). */
function triggerDownload(blob: Blob, filename: string): void {
  if (typeof URL === "undefined" || typeof URL.createObjectURL !== "function") {
    return;
  }
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function formatTimestamp(iso: string): string {
  // Backend timestamps are ISO-8601; render compactly without locale surprises.
  return iso.replace("T", " ").replace(/\.\d+/, "").replace(/(\+00:00|Z)$/, " UTC");
}

export function AuditLog(): JSX.Element {
  const [filters, setFilters] = useState<DraftFilters>(EMPTY_FILTERS);
  const [offset, setOffset] = useState(0);
  const [exporting, setExporting] = useState<null | "csv" | "json">(null);
  const [exportError, setExportError] = useState<string | null>(null);

  const apiFilters = useMemo(() => toApiFilters(filters), [filters]);

  const query = useApiQuery<AuditLogPage>(
    ["audit-log", apiFilters, offset],
    (signal) =>
      caliberApi.listAuditLog({ ...apiFilters, limit: PAGE_SIZE, offset }, signal),
  );

  const setField = useCallback((key: keyof DraftFilters, value: string) => {
    setFilters((prev) => ({ ...prev, [key]: value }));
    setOffset(0); // a new filter invalidates the current page window
  }, []);

  const handleExport = useCallback(
    async (format: "csv" | "json") => {
      setExporting(format);
      setExportError(null);
      try {
        const blob = await caliberApi.exportAuditLog(apiFilters, format);
        triggerDownload(blob, `caliber-audit-log.${format}`);
      } catch (err) {
        setExportError(
          err instanceof Error ? err.message : "Export failed. Please try again.",
        );
      } finally {
        setExporting(null);
      }
    },
    [apiFilters],
  );

  const isForbidden = query.error?.status === 403;
  const page = query.data;
  const entries = page?.entries ?? [];
  const total = page?.total ?? 0;
  const showingFrom = total === 0 ? 0 : offset + 1;
  const showingTo = Math.min(offset + entries.length, total);
  const hasFilters = Object.keys(apiFilters).length > 0;

  return (
    <div className="space-y-5" data-testid="audit-log-page">
      <PageHeader
        title="Audit Log"
        subtitle="Every governance action CALIBER records — who did what, to which artifact, and when. Append-only and admin-only."
        actions={
          <div className="flex items-center gap-2">
            <button
              type="button"
              data-testid="audit-export-csv"
              disabled={isForbidden || exporting !== null}
              onClick={() => void handleExport("csv")}
              className="inline-flex items-center gap-1.5 rounded-md border border-surface-200 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            >
              <Download className="h-3.5 w-3.5" />
              {exporting === "csv" ? "Exporting…" : "Export CSV"}
            </button>
            <button
              type="button"
              data-testid="audit-export-json"
              disabled={isForbidden || exporting !== null}
              onClick={() => void handleExport("json")}
              className="inline-flex items-center gap-1.5 rounded-md border border-surface-200 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            >
              <Download className="h-3.5 w-3.5" />
              {exporting === "json" ? "Exporting…" : "Export JSON"}
            </button>
          </div>
        }
      />

      {exportError && (
        <div
          data-testid="audit-export-error"
          className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700"
        >
          {exportError}
        </div>
      )}

      {/* Filter bar */}
      <div className="grid grid-cols-2 gap-3 rounded-xl border border-surface-200 bg-white p-4 sm:grid-cols-3 lg:grid-cols-6">
        <FilterInput
          label="Actor"
          testId="audit-filter-actor"
          value={filters.actor}
          placeholder="@alice"
          onChange={(v) => setField("actor", v)}
        />
        <FilterInput
          label="Action"
          testId="audit-filter-action"
          value={filters.action}
          placeholder="approve"
          onChange={(v) => setField("action", v)}
        />
        <FilterInput
          label="Entity type"
          testId="audit-filter-entity-type"
          value={filters.entity_type}
          placeholder="workflow"
          onChange={(v) => setField("entity_type", v)}
        />
        <FilterInput
          label="Entity ID"
          testId="audit-filter-entity-id"
          value={filters.entity_id}
          placeholder="WF-1"
          onChange={(v) => setField("entity_id", v)}
        />
        <FilterInput
          label="Since"
          testId="audit-filter-since"
          type="datetime-local"
          value={filters.since}
          onChange={(v) => setField("since", v)}
        />
        <FilterInput
          label="Until"
          testId="audit-filter-until"
          type="datetime-local"
          value={filters.until}
          onChange={(v) => setField("until", v)}
        />
      </div>

      {isForbidden ? (
        <div
          data-testid="audit-forbidden"
          className="rounded-xl border border-amber-200/70 bg-amber-50 px-4 py-4 text-sm text-amber-800"
        >
          <div className="font-semibold">Admin access required</div>
          <p className="mt-1 text-xs">
            The audit trail spans every user and project, so reading it requires the{" "}
            <code className="font-mono">admin</code> scope.
          </p>
        </div>
      ) : query.error ? (
        <div
          data-testid="audit-error"
          className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700"
        >
          <div className="font-medium">Failed to load the audit log</div>
          <div className="mt-0.5 text-xs">{query.error.message}</div>
        </div>
      ) : (
        <div className="rounded-xl border border-surface-200 bg-white">
          <div className="flex items-center justify-between border-b border-surface-200 px-4 py-2.5 text-xs text-slate-500">
            <span data-testid="audit-total">
              {query.isLoading
                ? "Loading…"
                : total === 0
                  ? "No matching entries"
                  : `Showing ${showingFrom}–${showingTo} of ${total.toLocaleString()}`}
            </span>
            <div className="flex items-center gap-2">
              <button
                type="button"
                data-testid="audit-prev"
                disabled={offset === 0}
                onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}
                className="rounded border border-surface-200 px-2 py-1 disabled:opacity-40"
              >
                Prev
              </button>
              <button
                type="button"
                data-testid="audit-next"
                disabled={showingTo >= total}
                onClick={() => setOffset((o) => o + PAGE_SIZE)}
                className="rounded border border-surface-200 px-2 py-1 disabled:opacity-40"
              >
                Next
              </button>
            </div>
          </div>

          {!query.isLoading && entries.length === 0 ? (
            <div data-testid="audit-empty" className="px-4 py-12 text-center text-sm text-slate-400">
              {hasFilters
                ? "No audit entries match these filters."
                : "No audit entries recorded yet."}
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-[11px] uppercase tracking-wider text-slate-400">
                  <tr className="border-b border-surface-200">
                    <th className="px-4 py-2 font-semibold">Time</th>
                    <th className="px-4 py-2 font-semibold">Actor</th>
                    <th className="px-4 py-2 font-semibold">Action</th>
                    <th className="px-4 py-2 font-semibold">Entity</th>
                    <th className="px-4 py-2 font-semibold">Details</th>
                  </tr>
                </thead>
                <tbody>
                  {entries.map((entry) => (
                    <tr
                      key={entry.log_id}
                      data-testid="audit-row"
                      className="border-b border-surface-100 last:border-0 align-top"
                    >
                      <td className="whitespace-nowrap px-4 py-2 font-mono text-xs text-slate-500">
                        {formatTimestamp(entry.timestamp)}
                      </td>
                      <td className="whitespace-nowrap px-4 py-2 text-slate-700">{entry.actor}</td>
                      <td className="whitespace-nowrap px-4 py-2">
                        <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-700">
                          {entry.action}
                        </span>
                      </td>
                      <td className="group whitespace-nowrap px-4 py-2 text-xs text-slate-600">
                        <span className="text-slate-400">{entry.entity_type}</span>
                        {" · "}
                        <span className="inline-flex items-center gap-1">
                          <span className="font-mono">{entry.entity_id}</span>
                          <CopyButton
                            value={entry.entity_id}
                            label="Copy entity ID"
                            className="opacity-0 group-hover:opacity-100"
                          />
                        </span>
                      </td>
                      <td className="px-4 py-2 text-xs text-slate-500">
                        {entry.details && Object.keys(entry.details).length > 0 ? (
                          <code className="font-mono break-all">
                            {JSON.stringify(entry.details)}
                          </code>
                        ) : (
                          <span className="text-slate-300">—</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      <p className="flex items-center gap-1.5 text-[11px] text-slate-400">
        <ScrollText className="h-3 w-3" />
        Exports include every entry matching the active filters (up to 10,000 rows).
      </p>
    </div>
  );
}

function FilterInput({
  label,
  testId,
  value,
  onChange,
  placeholder,
  type = "text",
}: {
  label: string;
  testId: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  type?: string;
}): JSX.Element {
  return (
    <div>
      <label className="mb-1 block text-xs text-slate-500">{label}</label>
      <input
        data-testid={testId}
        aria-label={label}
        type={type}
        className="w-full rounded-md border border-surface-200 px-3 py-1.5 text-sm"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}
