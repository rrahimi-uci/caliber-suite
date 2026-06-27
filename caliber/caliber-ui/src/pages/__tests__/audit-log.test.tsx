import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { AuditLog } from "@/pages/AuditLog";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function entry(overrides: Record<string, unknown> = {}) {
  return {
    log_id: 1,
    timestamp: "2026-06-03T14:15:00",
    actor: "@alice",
    action: "approve",
    entity_type: "workflow",
    entity_id: "WF-1",
    details: { alias: "prod" },
    ...overrides,
  };
}

function page(entries: ReturnType<typeof entry>[], total?: number) {
  return { entries, total: total ?? entries.length, limit: 50, offset: 0 };
}

function renderPage(): void {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        initialEntries={["/audit-log"]}
      >
        <Routes>
          <Route path="/audit-log" element={<AuditLog />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("AuditLog", () => {
  it("renders audit entries newest-first with a total summary", async () => {
    server.use(
      http.get(`${API_BASE}/audit-log`, () =>
        HttpResponse.json(
          envelope(
            page(
              [
                entry({
                  log_id: 3,
                  action: "dismiss",
                  entity_type: "verification_item",
                  entity_id: "VI-9",
                  details: { reason: "duplicate" },
                }),
                entry({ log_id: 1, action: "approve" }),
              ],
              2,
            ),
          ),
        ),
      ),
    );
    renderPage();
    const rows = await screen.findAllByTestId("audit-row");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText("dismiss")).toBeInTheDocument();
    expect(within(rows[0]).getByText(/"reason":"duplicate"/)).toBeInTheDocument();
    expect(screen.getByTestId("audit-total")).toHaveTextContent("Showing 1–2 of 2");
  });

  it("passes the actor filter to the API", async () => {
    let lastActor: string | null = "unset";
    server.use(
      http.get(`${API_BASE}/audit-log`, ({ request }) => {
        lastActor = new URL(request.url).searchParams.get("actor");
        return HttpResponse.json(envelope(page([entry()], 1)));
      }),
    );
    renderPage();
    await screen.findAllByTestId("audit-row");
    fireEvent.change(screen.getByTestId("audit-filter-actor"), {
      target: { value: "@bob" },
    });
    await waitFor(() => expect(lastActor).toBe("@bob"));
  });

  it("shows an admin-required notice and disables export on 403", async () => {
    server.use(
      http.get(`${API_BASE}/audit-log`, () =>
        HttpResponse.json({ detail: "forbidden" }, { status: 403 }),
      ),
    );
    renderPage();
    expect(await screen.findByTestId("audit-forbidden")).toBeInTheDocument();
    expect(screen.getByTestId("audit-export-csv")).toBeDisabled();
  });

  it("requests a CSV export carrying the active filters", async () => {
    let exportUrl: string | null = null;
    server.use(
      http.get(`${API_BASE}/audit-log`, () =>
        HttpResponse.json(envelope(page([entry()], 1))),
      ),
      http.get(`${API_BASE}/audit-log/export`, ({ request }) => {
        exportUrl = request.url;
        return new HttpResponse("log_id,timestamp\n1,2026-06-03T14:15:00", {
          headers: { "Content-Type": "text/csv" },
        });
      }),
    );
    renderPage();
    await screen.findAllByTestId("audit-row");
    fireEvent.change(screen.getByTestId("audit-filter-action"), {
      target: { value: "approve" },
    });
    await screen.findAllByTestId("audit-row");
    fireEvent.click(screen.getByTestId("audit-export-csv"));
    await waitFor(() => expect(exportUrl).not.toBeNull());
    const url = new URL(exportUrl as unknown as string);
    expect(url.searchParams.get("format")).toBe("csv");
    expect(url.searchParams.get("action")).toBe("approve");
  });
});
