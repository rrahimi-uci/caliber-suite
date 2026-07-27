/**
 * The sidebar footer status must reflect the real backend, not a constant.
 *
 * From the repository review (``ui-complete-report.md`` §6): the footer
 * rendered a hard-coded pulsing green dot labelled "System Online" that stayed
 * green with the backend down, while the TopBar indicator next to it polled
 * ``/health`` for real. Two indicators, one honest — a small but corrosive
 * trust bug.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { AssistantPanelProvider } from "@/components/assistant/AssistantPanelContext";
import { Sidebar } from "@/components/Sidebar";
import { TopBar } from "@/components/TopBar";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function renderSidebar(): void {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <Sidebar />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
});
afterAll(() => server.close());

describe("shared health poll", () => {
  it("issues one request for both shell indicators, not one each", async () => {
    // A follow-up review noted the Sidebar and TopBar shared hook *code* but
    // each ran its own poll cycle. The hook now goes through useApiQuery, so
    // the two mounted indicators dedupe onto a single in-flight request.
    let calls = 0;
    server.use(
      http.get(`${API_BASE}/health`, () => {
        calls += 1;
        return HttpResponse.json({ data: { status: "ok", version: "test" } });
      }),
    );

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
          <Sidebar />
          <AssistantPanelProvider>
            <TopBar />
          </AssistantPanelProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("sidebar-health")).toHaveTextContent("System Online");
    });
    expect(calls).toBe(1);
  });
});

describe("Sidebar health footer", () => {
  it("shows System Online when /health responds", async () => {
    renderSidebar();

    await waitFor(() => {
      expect(screen.getByTestId("sidebar-health")).toHaveTextContent("System Online");
    });
    expect(screen.getByTestId("sidebar-health").querySelector("div")).toHaveClass(
      "bg-emerald-400",
    );
  });

  it("shows System Unreachable when /health fails", async () => {
    server.use(
      http.get(`${API_BASE}/health`, () => HttpResponse.json({}, { status: 503 })),
    );

    renderSidebar();

    await waitFor(() => {
      expect(screen.getByTestId("sidebar-health")).toHaveTextContent(
        "System Unreachable",
      );
    });
    // The regression this pins: the dot used to stay emerald regardless.
    const dot = screen.getByTestId("sidebar-health").querySelector("div");
    expect(dot).toHaveClass("bg-red-400");
    expect(dot).not.toHaveClass("bg-emerald-400");
  });
});
