import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ArtifactVersion } from "@/api/versioning";
import { VersionPanel, type VersionAdapter } from "@/components/versioning/VersionPanel";

function v(overrides: Partial<ArtifactVersion>): ArtifactVersion {
  return {
    artifactType: "prompt",
    artifactId: "p",
    artifactName: "p",
    versionKey: "1",
    versionLabel: "v1",
    ordinal: 1,
    status: "published",
    isLive: false,
    liveAliases: [],
    capabilities: {
      hasHistory: true,
      canPromote: true,
      canRollback: true,
      canDiff: true,
      canEditDraft: false,
      canDelete: false,
      gating: "advisory",
    },
    raw: null,
    ...overrides,
  };
}

function makeAdapter(versions: ArtifactVersion[]): VersionAdapter {
  return {
    loadVersions: vi.fn().mockResolvedValue(versions),
    promote: vi.fn().mockResolvedValue(undefined),
    rollback: vi.fn().mockResolvedValue(undefined),
  };
}

const LIVE = v({ versionKey: "2", versionLabel: "v2", ordinal: 2, status: "active", isLive: true, liveAliases: ["prod"] });
const PRIOR = v({ versionKey: "1", versionLabel: "v1" });

describe("VersionPanel", () => {
  it("renders the list with a LIVE badge and the right per-row controls", async () => {
    render(<VersionPanel adapter={makeAdapter([LIVE, PRIOR])} />);
    await screen.findByTestId("version-panel");

    expect(screen.getByTestId("version-live-badge")).toBeInTheDocument();
    // Promote on the non-live row, rollback on the live row.
    expect(screen.getByTestId("version-promote-1")).toBeInTheDocument();
    expect(screen.getByTestId("version-rollback")).toBeInTheDocument();
    // No promote button on the live version.
    expect(screen.queryByTestId("version-promote-2")).not.toBeInTheDocument();
  });

  it("promotes a non-live version after confirmation (no override needed)", async () => {
    const adapter = makeAdapter([LIVE, PRIOR]);
    render(<VersionPanel adapter={adapter} />);
    await screen.findByTestId("version-panel");

    await userEvent.click(screen.getByTestId("version-promote-1"));
    await userEvent.click(screen.getByTestId("confirm-dialog-confirm"));

    await waitFor(() =>
      expect(adapter.promote).toHaveBeenCalledWith(
        expect.objectContaining({ versionKey: "1" }),
        { overridden: false, reason: "" },
      ),
    );
    // reload after the action (mount + post-action).
    expect(adapter.loadVersions).toHaveBeenCalledTimes(2);
  });

  it("requires an override reason to promote a gate-FAIL version", async () => {
    const failing = v({ versionKey: "1", versionLabel: "v1", gate: { state: "fail", score: 0.7 } });
    const adapter = makeAdapter([LIVE, failing]);
    render(<VersionPanel adapter={adapter} />);
    await screen.findByTestId("version-panel");

    await userEvent.click(screen.getByTestId("version-promote-1"));
    const confirm = screen.getByTestId("confirm-dialog-confirm");
    expect(confirm).toBeDisabled(); // can't promote past FAIL without a reason

    await userEvent.type(screen.getByTestId("confirm-dialog-reason"), "urgent hotfix");
    await userEvent.click(confirm);

    await waitFor(() =>
      expect(adapter.promote).toHaveBeenCalledWith(
        expect.objectContaining({ versionKey: "1" }),
        { overridden: true, reason: "urgent hotfix" },
      ),
    );
  });

  it("rolls back the live version after confirmation", async () => {
    const adapter = makeAdapter([LIVE, PRIOR]);
    render(<VersionPanel adapter={adapter} />);
    await screen.findByTestId("version-panel");

    await userEvent.click(screen.getByTestId("version-rollback"));
    await userEvent.click(screen.getByTestId("confirm-dialog-confirm"));

    await waitFor(() => expect(adapter.rollback).toHaveBeenCalledTimes(1));
  });

  it("shows the empty state when there are no versions", async () => {
    render(<VersionPanel adapter={makeAdapter([])} />);
    expect(await screen.findByTestId("version-panel-empty")).toBeInTheDocument();
  });

  it("shows an error state when loading fails", async () => {
    const adapter: VersionAdapter = {
      loadVersions: vi.fn().mockRejectedValue(new Error("boom")),
      promote: vi.fn(),
      rollback: vi.fn(),
    };
    render(<VersionPanel adapter={adapter} />);
    expect(await screen.findByTestId("version-panel-error")).toHaveTextContent("boom");
  });

  it("reloads when refreshKey changes", async () => {
    const adapter = makeAdapter([LIVE, PRIOR]);
    const { rerender } = render(<VersionPanel adapter={adapter} refreshKey={0} />);
    await screen.findByTestId("version-panel");
    expect(adapter.loadVersions).toHaveBeenCalledTimes(1);

    // A bump from the parent (e.g. after saving a new version) forces a refetch.
    rerender(<VersionPanel adapter={adapter} refreshKey={1} />);
    await waitFor(() => expect(adapter.loadVersions).toHaveBeenCalledTimes(2));
  });

  it("keeps the list intact and shows an inline error when an action fails", async () => {
    const adapter = makeAdapter([LIVE, PRIOR]);
    adapter.promote = vi.fn().mockRejectedValue(new Error("alias moved; reload and retry"));
    render(<VersionPanel adapter={adapter} />);
    await screen.findByTestId("version-panel");

    await userEvent.click(screen.getByTestId("version-promote-1"));
    await userEvent.click(screen.getByTestId("confirm-dialog-confirm"));

    // The failure surfaces inline...
    expect(await screen.findByTestId("version-panel-action-error")).toHaveTextContent(
      "alias moved; reload and retry",
    );
    // ...without tearing down the panel: the list and its controls are still
    // there so the operator can retry.
    expect(screen.getByTestId("version-panel")).toBeInTheDocument();
    expect(screen.getByTestId("version-list")).toBeInTheDocument();
    expect(screen.getByTestId("version-promote-1")).toBeInTheDocument();
    expect(screen.queryByTestId("version-panel-error")).not.toBeInTheDocument();
  });

  it("shows the gate score only for settled verdicts, not pending/stale", async () => {
    const pending = v({
      versionKey: "1",
      versionLabel: "v1",
      gate: { state: "pending", score: 0.83 },
    });
    const { unmount } = render(<VersionPanel adapter={makeAdapter([pending])} />);
    await screen.findByTestId("version-panel");
    const pendingGate = screen.getByTestId("version-gate");
    expect(pendingGate).toHaveTextContent("eval in progress");
    expect(pendingGate).not.toHaveTextContent("0.83"); // leftover score is hidden
    unmount();

    const passing = v({
      versionKey: "1",
      versionLabel: "v1",
      gate: { state: "pass", score: 0.92 },
    });
    render(<VersionPanel adapter={makeAdapter([passing])} />);
    await screen.findByTestId("version-panel");
    expect(screen.getByTestId("version-gate")).toHaveTextContent("0.92");
  });
});
