import { useCallback, useEffect, useState } from "react";

import type { ArtifactVersion, GateVerdict } from "@/api/versioning";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { LiveBadge, VersionStatusBadge } from "@/components/versioning/VersionStatusBadge";

/**
 * Per-artifact version operations the panel needs. Each artifact provides one,
 * wiring these to the relevant `caliberApi` calls — the panel never branches on
 * artifact type.
 */
export interface VersionAdapter {
  loadVersions: () => Promise<ArtifactVersion[]>;
  promote: (
    version: ArtifactVersion,
    opts: { overridden: boolean; reason: string },
  ) => Promise<void>;
  rollback: () => Promise<void>;
}

export interface VersionPanelProps {
  adapter: VersionAdapter;
  /**
   * Bump this (e.g. after saving a new version elsewhere on the page) to force
   * the panel to reload. Without it the list goes stale after an out-of-band
   * create until the component remounts.
   */
  refreshKey?: number;
}

function GateLine({ gate }: { gate: GateVerdict }): JSX.Element | null {
  if (gate.state === "none") return null;
  const tone =
    gate.state === "pass"
      ? "text-green-700"
      : gate.state === "fail"
        ? "text-amber-700"
        : "text-gray-500";
  const label =
    gate.state === "pass"
      ? "PASS"
      : gate.state === "fail"
        ? "FAIL"
        : gate.state === "pending"
          ? "eval in progress"
          : "stale";
  // A score only means something for a settled verdict. For pending/stale the
  // number is a leftover from a prior run — showing it reads as a fresh result.
  const showScore =
    (gate.state === "pass" || gate.state === "fail") && typeof gate.score === "number";
  return (
    <span className={`text-xs ${tone}`} data-testid="version-gate">
      gate: {label}
      {showScore ? ` · ${gate.score!.toFixed(2)}` : ""}
    </span>
  );
}

type DialogState =
  | { kind: "promote"; version: ArtifactVersion }
  | { kind: "rollback" }
  | null;

export function VersionPanel({ adapter, refreshKey }: VersionPanelProps): JSX.Element {
  const [versions, setVersions] = useState<ArtifactVersion[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // A failed promote/rollback is kept separate from the load error: it must NOT
  // tear down the whole panel (which would hide the very list the operator needs
  // to retry). It renders as an inline banner while the list stays put.
  const [actionError, setActionError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<DialogState>(null);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setVersions(await adapter.loadVersions());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load versions");
    } finally {
      setLoading(false);
    }
  }, [adapter]);

  useEffect(() => {
    void reload();
    // refreshKey is an intentional reload trigger: when the parent bumps it
    // (e.g. after saving a new version), re-fetch so the list isn't stale.
  }, [reload, refreshKey]);

  const live = versions.find((v) => v.isLive);

  const openDialog = useCallback((next: DialogState) => {
    setActionError(null);
    setDialog(next);
  }, []);

  const runAction = useCallback(
    async (action: () => Promise<void>) => {
      setBusy(true);
      setActionError(null);
      try {
        await action();
        setDialog(null);
        await reload();
      } catch (err) {
        // Keep the list intact; surface the failure inline and close the dialog
        // so the operator can read it and retry from the still-visible list.
        setActionError(err instanceof Error ? err.message : "Action failed");
        setDialog(null);
      } finally {
        setBusy(false);
      }
    },
    [reload],
  );

  if (loading) {
    return (
      <div data-testid="version-panel-loading" className="space-y-2">
        {[0, 1, 2].map((i) => (
          <div key={i} className="h-8 animate-pulse rounded bg-surface-100" />
        ))}
      </div>
    );
  }

  if (error) {
    return (
      <div data-testid="version-panel-error" className="text-sm text-red-700">
        {error}
        <Button variant="link" size="sm" onClick={() => void reload()}>
          Retry
        </Button>
      </div>
    );
  }

  if (versions.length === 0) {
    return (
      <div data-testid="version-panel-empty" className="text-sm text-gray-500">
        No versions yet.
      </div>
    );
  }

  const promoteFailing =
    dialog?.kind === "promote" && dialog.version.gate?.state === "fail";

  return (
    <div data-testid="version-panel">
      {actionError ? (
        <div
          data-testid="version-panel-action-error"
          className="mb-2 rounded border border-red-200 bg-red-50 px-2 py-1 text-sm text-red-700"
        >
          {actionError}
        </div>
      ) : null}
      <ul data-testid="version-list" className="divide-y divide-surface-100">
        {versions.map((version) => (
          <li
            key={version.versionKey}
            data-testid={`version-row-${version.versionKey}`}
            className="flex items-center gap-3 py-2"
          >
            <span className="font-medium text-gray-900">{version.versionLabel}</span>
            {version.isLive ? <LiveBadge alias={version.liveAliases[0]} /> : null}
            <VersionStatusBadge status={version.status} />
            {version.gate ? <GateLine gate={version.gate} /> : null}
            {version.author ? (
              <span className="text-xs text-gray-500">{version.author}</span>
            ) : null}
            {version.label ? (
              <span className="truncate text-xs text-gray-500">{version.label}</span>
            ) : null}
            <span className="ml-auto flex gap-2">
              {!version.isLive && version.capabilities.canPromote ? (
                <Button
                  variant="outline"
                  size="sm"
                  data-testid={`version-promote-${version.versionKey}`}
                  onClick={() => openDialog({ kind: "promote", version })}
                >
                  Promote
                </Button>
              ) : null}
              {version.isLive && version.capabilities.canRollback ? (
                <Button
                  variant="outline"
                  size="sm"
                  data-testid="version-rollback"
                  onClick={() => openDialog({ kind: "rollback" })}
                >
                  Roll back
                </Button>
              ) : null}
            </span>
          </li>
        ))}
      </ul>

      <ConfirmDialog
        open={dialog?.kind === "promote"}
        title={
          dialog?.kind === "promote"
            ? `Promote ${dialog.version.versionLabel} to LIVE`
            : ""
        }
        description={
          promoteFailing
            ? "This version failed the eval gate. Promoting anyway requires a reason."
            : "This will make the selected version live for all consumers."
        }
        confirmLabel={promoteFailing ? "Promote anyway" : "Promote"}
        requireReason={promoteFailing}
        reasonLabel="Override reason"
        busy={busy}
        onCancel={() => setDialog(null)}
        onConfirm={(reason) => {
          if (dialog?.kind !== "promote") return;
          void runAction(() =>
            adapter.promote(dialog.version, { overridden: Boolean(promoteFailing), reason }),
          );
        }}
      />

      <ConfirmDialog
        open={dialog?.kind === "rollback"}
        title="Roll back the live version"
        description={
          live
            ? `Roll back from ${live.versionLabel} to the previously-live version.`
            : ""
        }
        confirmLabel="Roll back"
        destructive
        busy={busy}
        onCancel={() => setDialog(null)}
        onConfirm={() => void runAction(() => adapter.rollback())}
      />
    </div>
  );
}
