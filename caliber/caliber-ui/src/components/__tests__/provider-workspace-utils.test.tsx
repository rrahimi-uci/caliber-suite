import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ProviderBanner } from "@/components/ProviderBanner";
import { WorkspaceSelector } from "@/components/WorkspaceSelector";
import { relativeTime } from "@/lib/time";
import { showToast } from "@/lib/toast";
import { getActiveProjectId } from "@/workspace/activeWorkspace";
import { caliberApi } from "@/api/caliberApi";
import { toast } from "sonner";

vi.mock("@/api/caliberApi", () => ({
  caliberApi: {
    getProviderReadiness: vi.fn(),
    listProjects: vi.fn(),
  },
}));

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
  },
}));

function renderWithQuery(ui: ReactElement): ReturnType<typeof render> {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

afterEach(() => {
  vi.clearAllMocks();
  vi.useRealTimers();
  window.localStorage.clear();
  window.sessionStorage.clear();
});

describe("ProviderBanner", () => {
  it("shows simulated providers and persists dismissal for the session", async () => {
    vi.mocked(caliberApi.getProviderReadiness).mockResolvedValue({
      mode: "mixed",
      real: ["workflow_storage"],
      simulated: ["artifact_store", "llm"],
      providers: {},
      warnings: [],
    });
    const user = userEvent.setup();

    render(<ProviderBanner />);

    expect(await screen.findByTestId("provider-banner")).toHaveTextContent(
      "artifact_store, llm",
    );
    await user.click(screen.getByRole("button", { name: "Dismiss" }));

    expect(screen.queryByTestId("provider-banner")).not.toBeInTheDocument();
    expect(window.sessionStorage.getItem("caliber.provider_banner_dismissed")).toBe("1");
  });

  it("renders nothing when all providers are real or readiness fails", async () => {
    vi.mocked(caliberApi.getProviderReadiness).mockResolvedValueOnce({
      mode: "real",
      real: ["artifact_store"],
      simulated: [],
      providers: {},
      warnings: [],
    });
    const { rerender } = render(<ProviderBanner />);

    await waitFor(() => expect(screen.queryByTestId("provider-banner")).not.toBeInTheDocument());

    vi.mocked(caliberApi.getProviderReadiness).mockRejectedValueOnce(new Error("offline"));
    rerender(<ProviderBanner />);
    await waitFor(() => expect(screen.queryByTestId("provider-banner")).not.toBeInTheDocument());
  });
});

describe("WorkspaceSelector", () => {
  it("loads projects, changes the active workspace, and can return to all workspaces", async () => {
    vi.mocked(caliberApi.listProjects).mockResolvedValue([
      {
        project_id: "proj-1",
        name: "Caliber Logs",
        description: "",
        owner: "@ops",
        status: "active",
        created_at: null,
        updated_at: null,
      },
    ]);
    const changed = vi.fn();
    window.addEventListener("caliber-workspace-changed", changed);
    const user = userEvent.setup();

    renderWithQuery(<WorkspaceSelector />);

    await screen.findByRole("option", { name: "Caliber Logs" });
    await user.selectOptions(screen.getByLabelText("Active workspace"), "proj-1");
    expect(getActiveProjectId()).toBe("proj-1");

    await user.selectOptions(screen.getByLabelText("Active workspace"), "");
    expect(getActiveProjectId()).toBeNull();
    expect(changed).toHaveBeenCalledTimes(2);
    window.removeEventListener("caliber-workspace-changed", changed);
  });
});

describe("utility helpers", () => {
  it("formats relative time across threshold boundaries", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-06-10T12:00:00Z"));

    expect(relativeTime("2026-06-10T11:59:58Z")).toBe("just now");
    expect(relativeTime("2026-06-10T11:59:30Z")).toBe("30s ago");
    expect(relativeTime("2026-06-10T11:30:00Z")).toBe("30m ago");
    expect(relativeTime("2026-06-10T09:00:00Z")).toBe("3h ago");
    expect(relativeTime("2026-06-08T12:00:00Z")).toBe("2d ago");
  });

  it("delegates toast helpers to sonner", () => {
    showToast.success("saved");
    showToast.error("failed");
    showToast.info("heads up");
    showToast.warning("careful");

    expect(toast.success).toHaveBeenCalledWith("saved");
    expect(toast.error).toHaveBeenCalledWith("failed");
    expect(toast.info).toHaveBeenCalledWith("heads up");
    expect(toast.warning).toHaveBeenCalledWith("careful");
  });
});
