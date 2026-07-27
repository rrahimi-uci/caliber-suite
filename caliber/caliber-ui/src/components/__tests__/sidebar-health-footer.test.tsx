/**
 * Shell health has one polling owner. Sidebar and TopBar receive the same
 * narrow API/database signal instead of mounting independent query observers.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { AppShell } from "@/components/AppShell";
import { HEALTH_QUERY_KEY } from "@/components/useHealthStatus";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function renderShell(healthPollIntervalMs = 30_000): QueryClient {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <AppShell healthPollIntervalMs={healthPollIntervalMs}>
          <div>content</div>
        </AppShell>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return queryClient;
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
});
afterAll(() => server.close());

describe("shared health poll", () => {
  it("keeps one query observer across a sustained polling cadence", async () => {
    let calls = 0;
    server.use(
      http.get(`${API_BASE}/health`, () => {
        calls += 1;
        return HttpResponse.json({ data: { status: "ok", version: "test" } });
      }),
    );

    const queryClient = renderShell(50);

    await waitFor(() => {
      expect(screen.getByTestId("sidebar-health")).toHaveTextContent(
        "API + database reachable",
      );
    });
    expect(calls).toBe(1);
    const healthQuery = queryClient.getQueryCache().find({
      queryKey: HEALTH_QUERY_KEY,
      exact: true,
    });
    expect(healthQuery?.getObserversCount()).toBe(1);

    await waitFor(() => expect(calls).toBeGreaterThanOrEqual(2), { timeout: 1_000 });
    expect(healthQuery?.getObserversCount()).toBe(1);
    expect(screen.getByLabelText("API and database reachable")).toBeInTheDocument();
  });
});

describe("Sidebar health footer", () => {
  it("shows precise API/database reachability when /health responds", async () => {
    renderShell();

    await waitFor(() => {
      expect(screen.getByTestId("sidebar-health")).toHaveTextContent(
        "API + database reachable",
      );
    });
    expect(screen.getByTestId("sidebar-health").querySelector("div")).toHaveClass(
      "bg-emerald-400",
    );
  });

  it("shows precise failure state when /health fails", async () => {
    server.use(
      http.get(`${API_BASE}/health`, () => HttpResponse.json({}, { status: 503 })),
    );

    renderShell();

    await waitFor(() => {
      expect(screen.getByTestId("sidebar-health")).toHaveTextContent(
        "API or database unreachable",
      );
    });
    // The regression this pins: the dot used to stay emerald regardless.
    const dot = screen.getByTestId("sidebar-health").querySelector("div");
    expect(dot).toHaveClass("bg-red-400");
    expect(dot).not.toHaveClass("bg-emerald-400");
    expect(screen.getByLabelText("API or database unreachable")).toBeInTheDocument();
  });
});
