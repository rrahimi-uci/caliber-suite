/**
 * Per-adapter support labels, rendered from ``GET /capabilities`` ``adapters`` (B15-Support.3, #771).
 *
 * A feature flag says an area is *on*; it never says an area is *supported*. This is the one place
 * the UI turns the server's per-adapter report (``caliber/features.py``, #755) into words, and it
 * has one rule: **an adapter reads as supported only when the server said so, completely.**
 *
 *   * An adapter that can never be supported (MCP servers, OpenAPI integrations) is capped at
 *     ``preview`` even if the server says ``supported``: the UI's own allowlist, the same two
 *     adapters ``caliber.features.SUPPORT_EVIDENCE`` can promote.
 *   * Loading, a failed request, a server that predates ``adapters``, an entry for an adapter the
 *     UI does not know, a status the UI does not know (nothing here reads or invents a "GA" or
 *     any other tier) and a ``supported`` entry that is missing its text, carries a reason, or
 *     cites no evidence all read as ``preview``, never as ``supported``.
 *   * The reason and recovery text is the server's ``message`` word for word, the same text the
 *     disabled route answers with. The UI writes no support claim of its own: the only text it adds
 *     is the notice that the server did not say, and the notice that it capped an adapter.
 *   * A status is a word plus an icon, never a colour alone.
 *
 * Reading this grants no backend authority. A ``supported`` adapter is still subject to the
 * authorization, approval and admission checks of its own routes.
 */

import { Ban, CircleCheck, CircleDashed } from "lucide-react";

import type { AdapterId, AdapterSupportStatus } from "@/api/workflowTypes";
import { useAdapterSupport } from "@/hooks/useFeature";

/** The adapters ``/capabilities`` can report, in the order it lists them. */
export const ADAPTER_IDS: readonly AdapterId[] = [
  "document_request_connector",
  "release_evaluation",
  "mcp_servers",
  "openapi_integrations",
];

/** The status words shown to a person. Exactly the server's three statuses, nothing else. */
export const ADAPTER_STATUS_LABEL: Readonly<Record<AdapterSupportStatus, string>> = {
  supported: "Supported",
  preview: "Preview",
  unavailable: "Unavailable",
};

/** Names shown for an adapter the server sent no (usable) entry for. They state no support. */
const FALLBACK_LABEL: Readonly<Record<AdapterId, string>> = {
  document_request_connector: "OpenAPI document-request connector",
  release_evaluation: "Workspace release evaluation",
  mcp_servers: "MCP servers",
  openapi_integrations: "OpenAPI integrations",
};

/** The only support text the UI writes itself: that the server did not say, so nothing is promoted. */
export const NOT_REPORTED_MESSAGE =
  "The server did not report why. Nothing is shown as supported unless the server says so.";

const STATUS_ICON: Record<AdapterSupportStatus, typeof Ban> = {
  supported: CircleCheck,
  preview: CircleDashed,
  unavailable: Ban,
};

// The colours are in ``styles/index.css`` (``.adapter-chip-*``), set per theme in one place, so a
// chip is readable in light and dark. The browser spec measures the contrast in both.
const STATUS_CLASS: Record<AdapterSupportStatus, string> = {
  supported: "adapter-chip-supported",
  preview: "adapter-chip-preview",
  unavailable: "adapter-chip-unavailable",
};

/**
 * The adapters that may ever read ``supported``: the reference connector and release evaluation,
 * the same two ``caliber.features.SUPPORT_EVIDENCE`` can promote. MCP servers and OpenAPI
 * integrations are capped at ``preview`` here whatever the server sends, so a server regression
 * cannot promote them. Everything else stays server-driven.
 */
const MAY_BE_SUPPORTED: ReadonlySet<AdapterId> = new Set<AdapterId>([
  "document_request_connector",
  "release_evaluation",
]);

/** Shown when the server says ``supported`` for an adapter that can never be: the UI's own downgrade. */
export const NEVER_SUPPORTED_MESSAGE =
  "This app shows only the reference connector and release evaluation as supported, so this adapter is shown as preview.";

export interface AdapterSupportView {
  /** True until capabilities have either loaded or failed. Nothing is labelled meanwhile. */
  loading: boolean;
  /** ``supported`` only when the server reported a complete, consistent ``supported`` entry. */
  status: AdapterSupportStatus;
  /** The server's label for the adapter, or a plain name when it sent none. */
  label: string;
  /** The server's exact reason and recovery text, or ``NOT_REPORTED_MESSAGE``. */
  message: string;
  /** True when the server sent a usable entry for this adapter. */
  reported: boolean;
  /** How many scenario tests the server cites; non-zero only for a ``supported`` adapter. */
  evidenceCount: number;
}

function nonEmpty(value: unknown): value is string {
  return typeof value === "string" && value.trim() !== "";
}

/** One adapter's support, read fail-closed. See the file comment for the rule. */
export function useAdapterSupportView(id: AdapterId): AdapterSupportView {
  const { loading, status, info } = useAdapterSupport(id);
  const label = nonEmpty(info?.label) ? info.label : FALLBACK_LABEL[id];
  const message = nonEmpty(info?.message) ? info.message : NOT_REPORTED_MESSAGE;
  const evidence = Array.isArray(info?.evidence)
    ? info.evidence.filter(nonEmpty)
    : [];
  // ``supported`` must be a whole claim: it says nothing is wrong (no reason code), says why it
  // holds (a message) and cites the scenarios behind it. Anything less is not promoted.
  const completeSupport =
    nonEmpty(info?.message) && info?.reason_code == null && evidence.length > 0;
  if (status === "supported" && !MAY_BE_SUPPORTED.has(id)) {
    return {
      loading,
      status: "preview",
      label,
      message: NEVER_SUPPORTED_MESSAGE,
      reported: false,
      evidenceCount: 0,
    };
  }
  if (info === null || (status === "supported" && !completeSupport)) {
    return {
      loading,
      status: "preview",
      label,
      message,
      reported: false,
      evidenceCount: 0,
    };
  }
  return {
    loading,
    status,
    label,
    message,
    reported: true,
    evidenceCount: status === "supported" ? evidence.length : 0,
  };
}

/** The status as a word and an icon on a tinted chip. Decorative to a screen reader: see ``describe``. */
export function AdapterStatusChip({
  status,
  className = "",
}: {
  status: AdapterSupportStatus;
  className?: string;
}): JSX.Element {
  const Icon = STATUS_ICON[status];
  return (
    <span
      data-testid="adapter-support-chip"
      data-status={status}
      className={`inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] font-bold ${STATUS_CLASS[status]} ${className}`}
    >
      <Icon className="h-3 w-3 flex-shrink-0" strokeWidth={2.25} aria-hidden="true" />
      {ADAPTER_STATUS_LABEL[status]}
    </span>
  );
}

/** What a screen reader hears for an adapter: its status word, then the server's text. */
export function describeAdapterSupport(view: AdapterSupportView): string {
  return `${ADAPTER_STATUS_LABEL[view.status]}. ${view.message}`;
}

function AdapterSupportRow({ id }: { id: AdapterId }): JSX.Element {
  const view = useAdapterSupportView(id);
  return (
    <li
      data-testid={`adapter-support-${id}`}
      data-status={view.loading ? "loading" : view.status}
      className="rounded-lg border border-slate-200/70 bg-white/70 px-2.5 py-2"
    >
      <div className="flex items-start justify-between gap-2">
        <span className="text-xs font-semibold text-slate-700">{view.label}</span>
        {!view.loading && <AdapterStatusChip status={view.status} />}
      </div>
      <p className="mt-1 text-[11px] leading-snug text-slate-600">
        {view.loading ? "Checking what this server supports…" : view.message}
      </p>
      {view.evidenceCount > 0 && (
        <p className="mt-1 text-[11px] text-slate-500">
          Backed by {view.evidenceCount} scenario{" "}
          {view.evidenceCount === 1 ? "test" : "tests"}.
        </p>
      )}
    </li>
  );
}

/** Every adapter's status with the server's exact reason and recovery text. */
export function AdapterSupportList(): JSX.Element {
  return (
    <ul
      aria-label="Support status by adapter"
      data-testid="adapter-support-list"
      className="mx-2 space-y-1.5"
    >
      {ADAPTER_IDS.map((id) => (
        <AdapterSupportRow key={id} id={id} />
      ))}
    </ul>
  );
}
