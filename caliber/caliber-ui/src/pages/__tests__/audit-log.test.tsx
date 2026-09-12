import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { caliberApi } from "@/api/caliberApi";
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
afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
});
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
    expect(within(rows[0]!).getByText("dismiss")).toBeInTheDocument();
    expect(within(rows[0]!).getByText(/"reason":"duplicate"/)).toBeInTheDocument();
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

  it("requests a JSON export", async () => {
    let exportUrl: string | null = null;
    server.use(
      http.get(`${API_BASE}/audit-log`, () =>
        HttpResponse.json(envelope(page([entry()], 1))),
      ),
      http.get(`${API_BASE}/audit-log/export`, ({ request }) => {
        exportUrl = request.url;
        return new HttpResponse('[{"log_id":1}]', {
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
    renderPage();
    await screen.findAllByTestId("audit-row");
    fireEvent.click(screen.getByTestId("audit-export-json"));
    await waitFor(() => expect(exportUrl).not.toBeNull());
    const url = new URL(exportUrl as unknown as string);
    expect(url.searchParams.get("format")).toBe("json");
  });

  it("surfaces a non-403 load error distinctly from the forbidden state", async () => {
    server.use(
      http.get(`${API_BASE}/audit-log`, () =>
        HttpResponse.json({ detail: "audit store unavailable" }, { status: 500 }),
      ),
    );
    renderPage();

    expect(await screen.findByTestId("audit-error")).toBeInTheDocument();
    expect(screen.getByText("Failed to load the audit log")).toBeInTheDocument();
    expect(screen.getByText(/audit store unavailable/i)).toBeInTheDocument();
    expect(screen.queryByTestId("audit-forbidden")).not.toBeInTheDocument();
    // A load error still permits export attempts (only the forbidden state disables it).
    expect(screen.getByTestId("audit-export-csv")).not.toBeDisabled();
  });

  it("shows an unfiltered empty state, then a filtered one once a filter is active", async () => {
    server.use(
      http.get(`${API_BASE}/audit-log`, () =>
        HttpResponse.json(envelope(page([], 0))),
      ),
    );
    renderPage();

    expect(await screen.findByTestId("audit-empty")).toHaveTextContent(
      "No audit entries recorded yet.",
    );
    expect(screen.getByTestId("audit-total")).toHaveTextContent("No matching entries");

    fireEvent.change(screen.getByTestId("audit-filter-entity-type"), {
      target: { value: "workflow" },
    });

    await waitFor(() =>
      expect(screen.getByTestId("audit-empty")).toHaveTextContent(
        "No audit entries match these filters.",
      ),
    );
  });

  it("renders a dash when an entry carries no details", async () => {
    server.use(
      http.get(`${API_BASE}/audit-log`, () =>
        HttpResponse.json(envelope(page([entry({ details: null })], 1))),
      ),
    );
    renderPage();

    const row = await screen.findByTestId("audit-row");
    expect(within(row).getByText("—")).toBeInTheDocument();
  });

  it("passes every filter field to the API and resets pagination on change", async () => {
    const seen: Record<string, string | null> = {};
    server.use(
      http.get(`${API_BASE}/audit-log`, ({ request }) => {
        const params = new URL(request.url).searchParams;
        seen.entity_type = params.get("entity_type");
        seen.entity_id = params.get("entity_id");
        seen.since = params.get("since");
        seen.until = params.get("until");
        seen.offset = params.get("offset");
        // 60 entries so pagination controls are meaningfully exercised elsewhere.
        return HttpResponse.json(envelope(page([entry()], 1)));
      }),
    );
    renderPage();
    await screen.findAllByTestId("audit-row");

    fireEvent.change(screen.getByTestId("audit-filter-entity-type"), {
      target: { value: "workflow" },
    });
    await waitFor(() => expect(seen.entity_type).toBe("workflow"));

    fireEvent.change(screen.getByTestId("audit-filter-entity-id"), {
      target: { value: "WF-9" },
    });
    await waitFor(() => expect(seen.entity_id).toBe("WF-9"));

    fireEvent.change(screen.getByTestId("audit-filter-since"), {
      target: { value: "2026-01-01T00:00" },
    });
    await waitFor(() => expect(seen.since).toBe("2026-01-01T00:00"));

    fireEvent.change(screen.getByTestId("audit-filter-until"), {
      target: { value: "2026-02-01T00:00" },
    });
    await waitFor(() => expect(seen.until).toBe("2026-02-01T00:00"));
    // Every filter edit resets the offset back to the first page.
    expect(seen.offset).toBe("0");
  });

  it("pages forward and back through results, disabling Prev/Next at the edges", async () => {
    server.use(
      http.get(`${API_BASE}/audit-log`, ({ request }) => {
        const offset = Number(new URL(request.url).searchParams.get("offset") ?? "0");
        const entries = offset === 0 ? [entry({ log_id: 1 })] : [entry({ log_id: 2 })];
        return HttpResponse.json({ data: { entries, total: 60, limit: 50, offset } });
      }),
    );
    renderPage();

    await screen.findAllByTestId("audit-row");
    expect(screen.getByTestId("audit-total")).toHaveTextContent("Showing 1–1 of 60");
    expect(screen.getByTestId("audit-prev")).toBeDisabled();
    expect(screen.getByTestId("audit-next")).not.toBeDisabled();

    fireEvent.click(screen.getByTestId("audit-next"));
    await waitFor(() =>
      expect(screen.getByTestId("audit-total")).toHaveTextContent("Showing 51–51 of 60"),
    );
    expect(screen.getByTestId("audit-prev")).not.toBeDisabled();

    fireEvent.click(screen.getByTestId("audit-prev"));
    await waitFor(() =>
      expect(screen.getByTestId("audit-total")).toHaveTextContent("Showing 1–1 of 60"),
    );
    expect(screen.getByTestId("audit-prev")).toBeDisabled();
  });

  it("shows an inline export error and recovers when the export call fails", async () => {
    server.use(
      http.get(`${API_BASE}/audit-log`, () =>
        HttpResponse.json(envelope(page([entry()], 1))),
      ),
    );
    vi.spyOn(caliberApi, "exportAuditLog").mockRejectedValueOnce(new Error("export exploded"));
    renderPage();
    await screen.findAllByTestId("audit-row");

    fireEvent.click(screen.getByTestId("audit-export-csv"));

    expect(await screen.findByTestId("audit-export-error")).toHaveTextContent(
      "export exploded",
    );
  });

  it("falls back to a generic export-error message when the rejection is not an Error", async () => {
    server.use(
      http.get(`${API_BASE}/audit-log`, () =>
        HttpResponse.json(envelope(page([entry()], 1))),
      ),
    );
    vi.spyOn(caliberApi, "exportAuditLog").mockRejectedValueOnce("boom");
    renderPage();
    await screen.findAllByTestId("audit-row");

    fireEvent.click(screen.getByTestId("audit-export-csv"));

    expect(await screen.findByTestId("audit-export-error")).toHaveTextContent(
      "Export failed. Please try again.",
    );
  });

  it("does not throw when triggering a download in an environment without URL.createObjectURL", async () => {
    server.use(
      http.get(`${API_BASE}/audit-log`, () =>
        HttpResponse.json(envelope(page([entry()], 1))),
      ),
      http.get(`${API_BASE}/audit-log/export`, () =>
        new HttpResponse("log_id,timestamp\n1,2026-06-03T14:15:00", {
          headers: { "Content-Type": "text/csv" },
        }),
      ),
    );
    const originalCreateObjectURL = URL.createObjectURL;
    // @ts-expect-error -- simulating a non-DOM environment where the API is absent.
    delete URL.createObjectURL;
    try {
      renderPage();
      await screen.findAllByTestId("audit-row");
      fireEvent.click(screen.getByTestId("audit-export-csv"));

      // No crash, and no export error either — the download is just a no-op.
      await waitFor(() =>
        expect(screen.getByTestId("audit-export-csv")).toHaveTextContent("Export CSV"),
      );
      expect(screen.queryByTestId("audit-export-error")).not.toBeInTheDocument();
    } finally {
      URL.createObjectURL = originalCreateObjectURL;
    }
  });
});
