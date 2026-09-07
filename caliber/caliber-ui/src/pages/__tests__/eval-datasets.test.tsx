import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { EvalDatasets } from "@/pages/EvalDatasets";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const NOW = "2026-06-07T18:00:00Z";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function makeDataset(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    dataset_id: "DS-1",
    name: "factual-checks",
    description: "Curated Q&A pairs that test factual recall.",
    owner: "@sarah",
    tags: ["qa", "facts"],
    status: "active" as const,
    version: 1,
    created_at: NOW,
    updated_at: NOW,
    mlflow_dataset_id: null,
    mlflow_synced_at: null,
    mlflow_synced_version: null,
    mlflow_record_count: null,
    mlflow_digest: null,
    ...overrides,
  };
}

function renderPage(): void {
  render(
    <MemoryRouter
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      initialEntries={["/eval-datasets"]}
    >
      <Routes>
        <Route path="/eval-datasets" element={<EvalDatasets />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("EvalDatasets", () => {
  it("narrows the list with the search box (name / owner / tag)", async () => {
    server.use(
      http.get(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json(
          envelope([
            makeDataset(),
            makeDataset({
              dataset_id: "DS-2",
              name: "tone-grading",
              description: "Style and tone rubric set.",
              owner: "@dana",
              tags: ["style"],
            }),
          ]),
        ),
      ),
    );

    const user = userEvent.setup();
    renderPage();

    // Both rows visible by default.
    expect(await screen.findByText("factual-checks")).toBeInTheDocument();
    expect(screen.getByText("tone-grading")).toBeInTheDocument();

    // Search by name narrows the table.
    const box = screen.getByRole("searchbox", { name: "Search test sets" });
    await user.type(box, "tone");
    expect(screen.queryByText("factual-checks")).not.toBeInTheDocument();
    expect(screen.getByText("tone-grading")).toBeInTheDocument();

    // Search by owner of the other row.
    await user.clear(box);
    await user.type(box, "@sarah");
    expect(screen.getByText("factual-checks")).toBeInTheDocument();
    expect(screen.queryByText("tone-grading")).not.toBeInTheDocument();

    // A miss shows the no-match empty state.
    await user.clear(box);
    await user.type(box, "zzz-no-match");
    expect(
      screen.getByText(/No test sets match/),
    ).toBeInTheDocument();

    // Clearing restores everything.
    await user.click(screen.getByRole("button", { name: "Clear search" }));
    expect(screen.getByText("factual-checks")).toBeInTheDocument();
    expect(screen.getByText("tone-grading")).toBeInTheDocument();
  });

  it("keeps the status FilterTabs working alongside search", async () => {
    let lastStatus: string | null = null;
    server.use(
      http.get(`${API_BASE}/eval-datasets`, ({ request }) => {
        lastStatus = new URL(request.url).searchParams.get("status");
        return HttpResponse.json(envelope([makeDataset()]));
      }),
    );

    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByText("factual-checks")).toBeInTheDocument();

    // The status tabs still drive the backend fetch (unchanged behavior).
    await user.click(within(document.body).getByRole("button", { name: "Archived" }));
    expect(lastStatus).toBe("archived");
  });

  it("adds owner and tag filters with a clear-filters reset", async () => {
    server.use(
      http.get(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json(
          envelope([
            makeDataset(),
            makeDataset({
              dataset_id: "DS-2",
              name: "tone-grading",
              owner: "@dana",
              tags: ["style"],
            }),
          ]),
        ),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByText("factual-checks")).toBeInTheDocument();
    expect(screen.getByText("tone-grading")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Filter by owner"), "@dana");
    expect(screen.getByText("tone-grading")).toBeInTheDocument();
    expect(screen.queryByText("factual-checks")).not.toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Filter by tag"), "style");
    expect(screen.getByText("tone-grading")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(screen.getByText("factual-checks")).toBeInTheDocument();
    expect(screen.getByText("tone-grading")).toBeInTheDocument();
  });

  it("syncs a test set to MLflow and reflects the synced badge", async () => {
    let synced = false;
    server.use(
      http.get(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json(
          envelope([
            synced
              ? makeDataset({
                  mlflow_dataset_id: "d-123",
                  mlflow_synced_at: NOW,
                  mlflow_synced_version: 1,
                  mlflow_record_count: 4,
                  mlflow_digest: "abc",
                })
              : makeDataset(),
          ]),
        ),
      ),
      http.post(`${API_BASE}/eval-datasets/DS-1/sync`, () => {
        synced = true;
        return HttpResponse.json(
          envelope(
            makeDataset({
              mlflow_dataset_id: "d-123",
              mlflow_synced_at: NOW,
              mlflow_synced_version: 1,
              mlflow_record_count: 4,
            }),
          ),
        );
      }),
    );

    const user = userEvent.setup();
    renderPage();

    // Starts "Not synced" with a "Sync to MLflow" action.
    expect(await screen.findByText("factual-checks")).toBeInTheDocument();
    expect(screen.getByText("Not synced")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Sync to MLflow" }));

    // After the sync + refetch, the badge flips to "MLflow: up to date".
    expect(await screen.findByText("MLflow: up to date")).toBeInTheDocument();
    expect(screen.queryByText("Not synced")).not.toBeInTheDocument();
  });

  it("flags a synced-but-changed test set as stale with a re-sync action", async () => {
    server.use(
      http.get(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json(
          envelope([
            makeDataset({
              version: 3,
              mlflow_dataset_id: "d-123",
              mlflow_synced_at: NOW,
              mlflow_synced_version: 1,
              mlflow_record_count: 2,
            }),
          ]),
        ),
      ),
    );

    renderPage();
    expect(await screen.findByText("factual-checks")).toBeInTheDocument();
    // synced_version (1) < version (3) → behind, and the action offers a re-sync.
    expect(screen.getByText("MLflow: behind")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Re-sync" })).toBeInTheDocument();
  });

  it("shows which fields a rejected create actually failed on", async () => {
    // The UX-05 gap end to end: the backend's validation handler returns
    // per-field detail, and before this change every surface rendered only
    // the generic ``detail`` string — "request body validation failed" —
    // leaving the user to guess which field was wrong.
    server.use(
      http.get(`${API_BASE}/eval-datasets`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json(
          {
            detail: "request body validation failed",
            status_code: 400,
            errors: [
              { loc: ["name"], msg: "string too short", type: "string_too_short" },
              { loc: ["owner"], msg: "value is not a valid user handle", type: "value_error" },
            ],
          },
          { status: 400 },
        ),
      ),
    );

    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "+ New Test Set" }));
    await user.type(screen.getByPlaceholderText("factual-checks"), "x");
    await user.type(screen.getByPlaceholderText("@sarah"), "nope");
    await user.click(screen.getByRole("button", { name: "Create" }));

    const message = await screen.findByText(/request body validation failed/);
    expect(message).toHaveTextContent("Name: String too short");
    expect(message).toHaveTextContent("Owner: Value is not a valid user handle");
  });

  it("leaves a non-validation rejection as its own message", async () => {
    // A 409 already says everything it has to say; the shared path must not
    // decorate it with structure that does not exist.
    server.use(
      http.get(`${API_BASE}/eval-datasets`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json(
          { detail: "eval dataset 'dupe' already exists", status_code: 409 },
          { status: 409 },
        ),
      ),
    );

    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "+ New Test Set" }));
    await user.type(screen.getByPlaceholderText("factual-checks"), "dupe");
    await user.type(screen.getByPlaceholderText("@sarah"), "@sarah");
    await user.click(screen.getByRole("button", { name: "Create" }));

    const message = await screen.findByText("eval dataset 'dupe' already exists");
    expect(message).toBeInTheDocument();
    expect(message.textContent).not.toContain("—");
  });
});
