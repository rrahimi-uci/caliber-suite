import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { fireEvent, render as rtlRender } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import { ObjectStore } from "@/pages/ObjectStore";
import { render, screen, userEvent, waitFor, within } from "@/test/utils";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const OS = `${API_BASE}/object-store`;
const NOW = "2026-06-09T12:00:00Z";

type MockObjectStoreObject = {
  key: string;
  size: number;
  last_modified: string | null;
  created_at?: string;
  etag: string;
};

function envelope<T>(data: T): { data: T } {
  return { data };
}

const STATUS = { connected: true, endpoint: "http://localhost:9000", bucket_count: 1 };
const BUCKETS = [{ name: "reports", creation_date: null }];
const FILES: MockObjectStoreObject[] = [
  { key: "alpha.txt", size: 10, created_at: NOW, last_modified: NOW, etag: "a" },
  { key: "beta.log", size: 2048, created_at: NOW, last_modified: NOW, etag: "b" },
];

function listing(objects: MockObjectStoreObject[], prefixes: string[] = []) {
  return { bucket: "reports", prefix: "", prefixes, objects, next_token: null, is_truncated: false };
}

/** GET handlers the page hits on mount + after selecting the bucket. */
function reads(objectsData: ReturnType<typeof listing>) {
  return [
    http.get(`${OS}/status`, () => HttpResponse.json(envelope(STATUS))),
    http.get(`${OS}/buckets`, () => HttpResponse.json(envelope(BUCKETS))),
    http.get(`${OS}/buckets/reports/objects`, () => HttpResponse.json(envelope(objectsData))),
  ];
}

function LocationProbe(): JSX.Element {
  const location = useLocation();
  return <div data-testid="location-search">{location.search}</div>;
}

function renderObjectStoreAt(initialPath: string): void {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });
  rtlRender(
    <QueryClientProvider client={qc}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }} initialEntries={[initialPath]}>
        <Routes>
          <Route
            path="/object-store"
            element={(
              <>
                <ObjectStore />
                <LocationProbe />
              </>
            )}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
afterAll(() => server.close());

describe("Object Store file manager", () => {
  it("lists folders + files and filters with the search box", async () => {
    server.use(...reads(listing(FILES, ["2026/"])));
    const user = userEvent.setup();
    render(<ObjectStore />);

    await user.click(await screen.findByTestId("bucket-reports"));
    expect(await screen.findByText("alpha.txt")).toBeInTheDocument();
    expect(screen.getByText("beta.log")).toBeInTheDocument();
    expect(screen.getByText("2026/")).toBeInTheDocument();

    await user.type(screen.getByTestId("object-search"), "alpha");
    expect(screen.getByText("alpha.txt")).toBeInTheDocument();
    expect(screen.queryByText("beta.log")).not.toBeInTheDocument();
    expect(screen.queryByText("2026/")).not.toBeInTheDocument();
  });

  it("filters rows by the Type dropdown (folder vs file category)", async () => {
    const mixed: MockObjectStoreObject[] = [
      { key: "notes.txt", size: 10, created_at: NOW, last_modified: NOW, etag: "t" },
      { key: "diagram.png", size: 20, created_at: NOW, last_modified: NOW, etag: "i" },
      { key: "script.py", size: 30, created_at: NOW, last_modified: NOW, etag: "c" },
    ];
    server.use(...reads(listing(mixed, ["2026/"])));
    const user = userEvent.setup();
    render(<ObjectStore />);

    await user.click(await screen.findByTestId("bucket-reports"));
    expect(await screen.findByText("notes.txt")).toBeInTheDocument();
    expect(screen.getByText("diagram.png")).toBeInTheDocument();
    expect(screen.getByText("2026/")).toBeInTheDocument();

    const typeSelect = screen.getByRole("combobox", { name: "Filter by type" });

    // Images only: file rows narrow, folders hidden.
    await user.selectOptions(typeSelect, "image");
    expect(screen.getByText("diagram.png")).toBeInTheDocument();
    expect(screen.queryByText("notes.txt")).not.toBeInTheDocument();
    expect(screen.queryByText("script.py")).not.toBeInTheDocument();
    expect(screen.queryByText("2026/")).not.toBeInTheDocument();

    // Folders only: every file row hidden, folder shown.
    await user.selectOptions(typeSelect, "folder");
    expect(screen.getByText("2026/")).toBeInTheDocument();
    expect(screen.queryByText("diagram.png")).not.toBeInTheDocument();
    expect(screen.queryByText("notes.txt")).not.toBeInTheDocument();

    // Reset shows everything again.
    await user.selectOptions(typeSelect, "");
    expect(screen.getByText("notes.txt")).toBeInTheDocument();
    expect(screen.getByText("diagram.png")).toBeInTheDocument();
    expect(screen.getByText("script.py")).toBeInTheDocument();
    expect(screen.getByText("2026/")).toBeInTheDocument();
  });

  it("selects all and bulk-deletes via the batch endpoint", async () => {
    let body: { keys?: string[] } | null = null;
    server.use(
      ...reads(listing(FILES)),
      http.post(`${OS}/buckets/reports/objects/delete`, async ({ request }) => {
        body = (await request.json()) as { keys?: string[] };
        return HttpResponse.json(envelope({ deleted: 2, errors: [] }));
      }),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<ObjectStore />);

    await user.click(await screen.findByTestId("bucket-reports"));
    await screen.findByText("alpha.txt");
    await user.click(screen.getByTestId("select-all"));
    await user.click(await screen.findByTestId("bulk-delete"));

    await waitFor(() => expect(body).toMatchObject({ keys: expect.arrayContaining(["alpha.txt", "beta.log"]) }));
  });

  it("creates a folder via the folders endpoint", async () => {
    let body: { prefix?: string; name?: string } | null = null;
    server.use(
      ...reads(listing(FILES)),
      http.post(`${OS}/buckets/reports/folders`, async ({ request }) => {
        body = (await request.json()) as { prefix?: string; name?: string };
        return HttpResponse.json(envelope({ prefix: "newdir/" }), { status: 201 });
      }),
    );
    const user = userEvent.setup();
    render(<ObjectStore />);

    await user.click(await screen.findByTestId("bucket-reports"));
    await screen.findByText("alpha.txt");
    await user.click(screen.getByRole("button", { name: /New folder/i }));
    await user.type(screen.getByTestId("new-folder-input"), "newdir");
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(body).toMatchObject({ prefix: "", name: "newdir" }));
  });

  it("opens an object in a new tab and marks its row as selected", async () => {
    server.use(...reads(listing(FILES)));
    const user = userEvent.setup();
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
    render(<ObjectStore />);

    await user.click(await screen.findByTestId("bucket-reports"));
    await user.click(await screen.findByText("alpha.txt"));

    await waitFor(() =>
      expect(openSpy).toHaveBeenCalledWith(
        `${OS}/buckets/reports/object?key=alpha.txt&disposition=inline`,
        "_blank",
        "noopener,noreferrer",
      ),
    );
    expect(screen.getByTestId("object-alpha.txt")).toHaveClass("object-store-open-row");
  });

  it("imports an immutable project file and keeps clipboard denial non-fatal", async () => {
    let body: { key?: string; expected_etag?: string } | null = null;
    server.use(
      ...reads(listing(FILES)),
      http.post(`${OS}/buckets/reports/object/import`, async ({ request }) => {
        body = (await request.json()) as { key?: string; expected_etag?: string };
        return HttpResponse.json(
          envelope({
            file_id: "FILE-1",
            file_ref: "caliber://projects/PRJ-1/input/alpha.txt",
            name: "alpha.txt",
            kind: "input",
            relative_path: "alpha.txt",
            media_type: "text/plain",
            size_bytes: 10,
            sha256: "a".repeat(64),
            status: "attached",
            producer_node_id: null,
            created_at: NOW,
            immutable_ref: {
              file_id: "FILE-1",
              file_ref: "caliber://projects/PRJ-1/input/alpha.txt",
              sha256: "a".repeat(64),
              name: "alpha.txt",
              size_bytes: 10,
              media_type: "text/plain",
              object_version_id: null,
            },
          }),
          { status: 201 },
        );
      }),
    );
    const user = userEvent.setup();
    const writeText = vi
      .spyOn(navigator.clipboard, "writeText")
      .mockRejectedValue(new Error("clipboard denied"));
    render(<ObjectStore />);

    await user.click(await screen.findByTestId("bucket-reports"));
    await screen.findByText("alpha.txt");
    await user.click(
      screen.getAllByTitle("Add immutable copy to active project files")[0],
    );

    await waitFor(() =>
      expect(body).toEqual({ key: "alpha.txt", expected_etag: "a" }),
    );
    expect(
      await screen.findByText("alpha.txt", { selector: "strong" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("caliber://projects/PRJ-1/input/alpha.txt", {
        selector: "code",
      }),
    ).toBeInTheDocument();
    expect(screen.queryByText("clipboard denied")).not.toBeInTheDocument();
    expect(writeText).toHaveBeenCalledWith(
      "caliber://projects/PRJ-1/input/alpha.txt",
    );
  });

  it("uploads selected files into the current folder", async () => {
    let uploaded: { hasUpload: boolean; prefix: FormDataEntryValue | null } | null = null;
    server.use(
      ...reads(listing(FILES, ["service/"])),
      http.post(`${OS}/buckets/reports/objects`, async ({ request }) => {
        const form = await request.formData();
        uploaded = {
          hasUpload: form.has("file"),
          prefix: form.get("prefix"),
        };
        return HttpResponse.json(envelope({ bucket: "reports", key: "service/uploaded.txt", size: 4 }));
      }),
      http.get(`${OS}/buckets/reports/object/preview`, () =>
        HttpResponse.json(
          envelope({
            bucket: "reports",
            key: "service/uploaded.txt",
            size: 4,
            last_modified: NOW,
            etag: "u",
            content_type: "text/plain",
            preview_bytes: 4,
            truncated: false,
            is_text: true,
            text: "done",
          }),
        ),
      ),
    );
    const user = userEvent.setup();
    render(<ObjectStore />);

    await user.click(await screen.findByTestId("bucket-reports"));
    await user.click(await screen.findByText("service/"));
    await user.upload(screen.getByTestId("upload-input"), new File(["data"], "uploaded.txt"));

    await waitFor(() =>
      expect(uploaded).toEqual({ hasUpload: true, prefix: "service/" }),
    );
  });

  it("searches recursively across all folders", async () => {
    const queries: Array<{ recursive: string | null; token: string | null }> = [];
    server.use(
      http.get(`${OS}/status`, () => HttpResponse.json(envelope(STATUS))),
      http.get(`${OS}/buckets`, () => HttpResponse.json(envelope(BUCKETS))),
      http.get(`${OS}/buckets/reports/objects`, ({ request }) => {
        const url = new URL(request.url);
        queries.push({
          recursive: url.searchParams.get("recursive"),
          token: url.searchParams.get("token"),
        });
        if (url.searchParams.get("recursive") === "true") {
          return HttpResponse.json(
            envelope(
              listing([
                { key: "service/2026/gamma.jsonl", size: 3, last_modified: NOW, etag: "g" },
              ]),
            ),
          );
        }
        return HttpResponse.json(envelope(listing([], ["service/"])));
      }),
    );
    const user = userEvent.setup();
    render(<ObjectStore />);

    await user.click(await screen.findByTestId("bucket-reports"));
    await user.click(screen.getByLabelText("Search all folders"));

    expect(await screen.findByText("service/2026/gamma.jsonl")).toBeInTheDocument();
    expect(queries).toEqual(
      expect.arrayContaining([expect.objectContaining({ recursive: "true", token: null })]),
    );
  });

  it("deletes a folder through the recursive delete endpoint", async () => {
    let body: { prefix?: string } | null = null;
    server.use(
      ...reads(listing([], ["service/"])),
      http.post(`${OS}/buckets/reports/objects/delete`, async ({ request }) => {
        body = (await request.json()) as { prefix?: string };
        return HttpResponse.json(envelope({ deleted: 3, errors: [] }));
      }),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<ObjectStore />);

    await user.click(await screen.findByTestId("bucket-reports"));
    await screen.findByText("service/");
    await user.click(screen.getByTitle("Delete folder and contents"));

    await waitFor(() => expect(body).toEqual({ prefix: "service/" }));
  });

  it("shows the offline banner when the object store status is disconnected", async () => {
    server.use(
      http.get(`${OS}/status`, () =>
        HttpResponse.json(
          envelope({
            connected: false,
            endpoint: "http://localhost:9000",
            bucket_count: 0,
            error: "connection refused",
          }),
        ),
      ),
      http.get(`${OS}/buckets`, () => HttpResponse.json(envelope([]))),
    );

    render(<ObjectStore />);

    expect(await screen.findByTestId("object-store-offline")).toHaveTextContent(
      "connection refused",
    );
    expect(screen.getByText("No buckets yet.")).toBeInTheDocument();
  });

  it("creates and deletes buckets from the bucket panel", async () => {
    let created: { name?: string } | null = null;
    let deletedBucket: string | null = null;
    server.use(
      ...reads(listing(FILES)),
      http.post(`${OS}/buckets`, async ({ request }) => {
        created = (await request.json()) as { name?: string };
        return HttpResponse.json(envelope({ name: created.name }), { status: 201 });
      }),
      http.delete(`${OS}/buckets/:bucket`, ({ params }) => {
        deletedBucket = String(params.bucket);
        return new HttpResponse(null, { status: 204 });
      }),
      http.get(`${OS}/buckets/new-logs/objects`, () =>
        HttpResponse.json(envelope({ bucket: "new-logs", prefix: "", prefixes: [], objects: [], next_token: null, is_truncated: false })),
      ),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<ObjectStore />);

    await user.type(await screen.findByTestId("new-bucket-input"), "new-logs");
    await user.click(screen.getByTitle("Create bucket"));
    await waitFor(() => expect(created).toEqual({ name: "new-logs" }));

    await user.click(await screen.findByTestId("bucket-reports"));
    await user.click(screen.getByTitle("Delete bucket"));
    await waitFor(() => expect(deletedBucket).toBe("reports"));
  });

  it("deletes a single object and clears the selected row state", async () => {
    let body: { keys?: string[] } | null = null;
    server.use(
      ...reads(listing(FILES)),
      http.post(`${OS}/buckets/reports/objects/delete`, async ({ request }) => {
        body = (await request.json()) as { keys?: string[] };
        return HttpResponse.json(envelope({ deleted: 1, errors: [] }));
      }),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.spyOn(window, "open").mockImplementation(() => null);
    const user = userEvent.setup();
    render(<ObjectStore />);

    await user.click(await screen.findByTestId("bucket-reports"));
    await user.click(await screen.findByText("alpha.txt"));
    expect(screen.getByTestId("object-alpha.txt")).toHaveClass("object-store-open-row");
    await user.click(within(screen.getByTestId("object-alpha.txt")).getByTitle("Delete"));

    await waitFor(() => expect(body).toEqual({ keys: ["alpha.txt"] }));
    expect(screen.getByTestId("object-alpha.txt")).not.toHaveClass("object-store-open-row");
  });

  it("opens binary objects with the inline object URL and preserves row formatting", async () => {
    server.use(...reads(listing([{ key: "image.png", size: 4096, last_modified: NOW, etag: "img" }])));
    const user = userEvent.setup();
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
    render(<ObjectStore />);

    await user.click(await screen.findByTestId("bucket-reports"));
    await user.click(await screen.findByText("image.png"));

    expect(await screen.findByText("4.0 KB")).toBeInTheDocument();
    await waitFor(() =>
      expect(openSpy).toHaveBeenCalledWith(
        `${OS}/buckets/reports/object?key=image.png&disposition=inline`,
        "_blank",
        "noopener,noreferrer",
      ),
    );
    expect(screen.getByTestId("object-image.png")).toHaveClass("object-store-open-row");
  });

  it("surfaces upload failures and lets the user dismiss the error", async () => {
    server.use(
      ...reads(listing(FILES)),
      http.post(`${OS}/buckets/reports/objects`, () =>
        HttpResponse.json({ detail: "disk full", status_code: 500 }, { status: 500 }),
      ),
    );
    const user = userEvent.setup();
    render(<ObjectStore />);

    await user.click(await screen.findByTestId("bucket-reports"));
    await user.upload(screen.getByTestId("upload-input"), new File(["bad"], "bad.txt"));

    expect(await screen.findByText(/upload\(s\) failed/)).toBeInTheDocument();
    await user.click(screen.getByLabelText("Dismiss error"));
    expect(screen.queryByText(/upload\(s\) failed/)).not.toBeInTheDocument();
  });

  it("sorts object rows by size", async () => {
    server.use(...reads(listing(FILES)));
    const user = userEvent.setup();
    render(<ObjectStore />);

    await user.click(await screen.findByTestId("bucket-reports"));
    await screen.findByText("alpha.txt");
    await user.click(screen.getByRole("button", { name: /^Size$/i }));

    const rows = screen
      .getAllByTestId(/^object-/)
      .map((row) => row.textContent ?? "")
      .filter((text) => text.includes("alpha.txt") || text.includes("beta.log"));
    expect(rows.join(" ")).toContain("alpha.txt");
    expect(rows.join(" ")).toContain("beta.log");
  });

  it("paginates recursive search results and shows the truncated warning", async () => {
    const queries: Array<{ recursive: string | null; token: string | null }> = [];
    server.use(
      http.get(`${OS}/status`, () => HttpResponse.json(envelope(STATUS))),
      http.get(`${OS}/buckets`, () => HttpResponse.json(envelope(BUCKETS))),
      http.get(`${OS}/buckets/reports/objects`, ({ request }) => {
        const url = new URL(request.url);
        const token = url.searchParams.get("token");
        queries.push({ recursive: url.searchParams.get("recursive"), token });
        if (url.searchParams.get("recursive") === "true") {
          const page = token ? Number(token.replace("page-", "")) : 0;
          return HttpResponse.json(
            envelope({
              bucket: "reports",
              prefix: "",
              prefixes: [],
              objects: [
                {
                  key: `service/page-${page}.jsonl`,
                  size: page + 1,
                  last_modified: NOW,
                  etag: `etag-${page}`,
                },
              ],
              next_token: `page-${page + 1}`,
              is_truncated: true,
            }),
          );
        }
        return HttpResponse.json(envelope(listing([], ["service/"])));
      }),
    );

    const user = userEvent.setup();
    render(<ObjectStore />);

    await user.click(await screen.findByTestId("bucket-reports"));
    await user.click(screen.getByLabelText("Search all folders"));

    expect(await screen.findByText("service/page-0.jsonl")).toBeInTheDocument();
    expect(await screen.findByText("service/page-19.jsonl")).toBeInTheDocument();
    expect(queries.filter((query) => query.recursive === "true")).toHaveLength(20);
    expect(screen.getByText(/Showing the first 20 results \(truncated\)/)).toBeInTheDocument();
  });

  it("copies object keys and downloads selected objects", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    server.use(...reads(listing(FILES)));
    const user = userEvent.setup();
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    render(<ObjectStore />);

    await user.click(await screen.findByTestId("bucket-reports"));
    await screen.findByText("alpha.txt");
    await user.click(within(screen.getByTestId("object-alpha.txt")).getByTitle("Copy key"));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("alpha.txt"));

    await user.click(screen.getByLabelText("Select alpha.txt"));
    await user.click(within(screen.getByTestId("bulk-bar")).getByRole("button", { name: /Download/i }));
    expect(clickSpy).toHaveBeenCalledTimes(1);
  });

  it("renders bucket count without the status banner when connected", async () => {
    server.use(...reads(listing(FILES)));
    render(<ObjectStore />);

    // The dedicated status banner was removed from the page.
    expect(screen.queryByText("MinIO connected")).not.toBeInTheDocument();
    expect(screen.queryByText("healthy")).not.toBeInTheDocument();

    await screen.findByTestId("bucket-reports");

    // Bucket panel count still reflects real bucket count (BUCKETS has one).
    const bucketsLabel = screen.getByText("Workspaces", {
      selector: "span",
    });
    expect(bucketsLabel.nextElementSibling).toHaveTextContent("1");

    // Never shows the offline warning when connected.
    expect(screen.queryByTestId("object-store-offline")).not.toBeInTheDocument();
  });

  it("shows the created column and opens a preview modal from the row action", async () => {
    const createdAt = "2026-06-01T10:15:00Z";
    server.use(
      ...reads(
        listing([
          { key: "notes.md", size: 20, created_at: createdAt, last_modified: NOW, etag: "m" },
        ]),
      ),
      http.get(`${OS}/buckets/reports/object/preview`, ({ request }) => {
        const url = new URL(request.url);
        if (url.searchParams.get("key") !== "notes.md") {
          return HttpResponse.json({ detail: "not found" }, { status: 404 });
        }
        return HttpResponse.json(
          envelope({
            bucket: "reports",
            key: "notes.md",
            size: 20,
            created_at: createdAt,
            last_modified: NOW,
            etag: "m",
            content_type: "text/markdown",
            preview_bytes: 19,
            truncated: false,
            is_text: true,
            text: "hello from preview",
          }),
        );
      }),
    );
    const user = userEvent.setup();
    render(<ObjectStore />);

    await user.click(await screen.findByTestId("bucket-reports"));
    await screen.findByText("notes.md");

    expect(screen.getByRole("button", { name: /^Created$/i })).toBeInTheDocument();
    expect(screen.queryByText("JSON / JSONL")).not.toBeInTheDocument();
    // "Images" now exists as a Type-filter dropdown option, so assert it is not
    // rendered as a standalone column/label (i.e. outside the filter select).
    expect(
      screen.queryByText("Images", { selector: ":not(option)" }),
    ).not.toBeInTheDocument();

    const row = screen.getByTestId("object-notes.md");
    await user.click(within(row).getByTitle("View preview"));

    const modal = await screen.findByTestId("object-preview-modal");
    expect(within(modal).getByText("Object preview")).toBeInTheDocument();
    expect(within(modal).getByText("Created")).toBeInTheDocument();
    expect(within(modal).getByText("hello from preview")).toBeInTheDocument();

    await user.click(within(modal).getByLabelText("Close file preview"));
    await waitFor(() =>
      expect(screen.queryByTestId("object-preview-modal")).not.toBeInTheDocument(),
    );
  });

  // Build a preview-endpoint handler that responds for a single key.
  function previewHandler(
    key: string,
    body: Partial<{
      content_type: string;
      is_text: boolean;
      text: string | null;
      size: number;
    }>,
  ) {
    return http.get(`${OS}/buckets/reports/object/preview`, ({ request }) => {
      if (new URL(request.url).searchParams.get("key") !== key) {
        return HttpResponse.json({ detail: "not found" }, { status: 404 });
      }
      return HttpResponse.json(
        envelope({
          bucket: "reports",
          key,
          size: body.size ?? 10,
          created_at: NOW,
          last_modified: NOW,
          etag: "e",
          content_type: body.content_type ?? "application/octet-stream",
          preview_bytes: body.text ? body.text.length : 0,
          truncated: false,
          is_text: body.is_text ?? false,
          text: body.text ?? null,
        }),
      );
    });
  }

  async function openPreviewFor(key: string): Promise<HTMLElement> {
    const user = userEvent.setup();
    render(<ObjectStore />);
    await user.click(await screen.findByTestId("bucket-reports"));
    await screen.findByText(key);
    await user.click(within(screen.getByTestId(`object-${key}`)).getByTitle("View preview"));
    return screen.findByTestId("object-preview-modal");
  }

  it("renders markdown files as formatted content", async () => {
    server.use(
      ...reads(listing([{ key: "doc.md", size: 30, created_at: NOW, last_modified: NOW, etag: "m" }])),
      previewHandler("doc.md", {
        content_type: "text/markdown",
        is_text: true,
        text: "# Title\n\nSome **bold** text.",
      }),
    );
    const modal = await openPreviewFor("doc.md");
    const md = within(modal).getByTestId("object-preview-markdown");
    expect(within(md).getByRole("heading", { name: "Title" })).toBeInTheDocument();
    expect(md.querySelector("strong")?.textContent).toBe("bold");
  });

  it("renders CSV files as a table", async () => {
    server.use(
      ...reads(listing([{ key: "rows.csv", size: 20, created_at: NOW, last_modified: NOW, etag: "c" }])),
      previewHandler("rows.csv", {
        content_type: "text/csv",
        is_text: true,
        text: "name,score\nAda,99",
      }),
    );
    const modal = await openPreviewFor("rows.csv");
    const table = within(modal).getByTestId("object-preview-table");
    expect(within(table).getByText("name")).toBeInTheDocument();
    expect(within(table).getByText("Ada")).toBeInTheDocument();
    expect(within(table).getByText("99")).toBeInTheDocument();
  });

  it("renders a video player for media files", async () => {
    server.use(
      ...reads(listing([{ key: "clip.mp4", size: 999, created_at: NOW, last_modified: NOW, etag: "v" }])),
      previewHandler("clip.mp4", { content_type: "video/mp4", is_text: false }),
    );
    const modal = await openPreviewFor("clip.mp4");
    const video = within(modal).getByTestId("object-preview-video");
    expect(video).toHaveAttribute("controls");
    expect(video.getAttribute("src")).toContain("disposition=inline");
  });

  it("extracts Word documents to inline text", async () => {
    server.use(
      ...reads(listing([{ key: "memo.docx", size: 4096, created_at: NOW, last_modified: NOW, etag: "d" }])),
      previewHandler("memo.docx", {
        content_type:
          "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        is_text: false,
      }),
      http.get(`${OS}/buckets/reports/object/extract`, ({ request }) => {
        if (new URL(request.url).searchParams.get("key") !== "memo.docx") {
          return HttpResponse.json({ detail: "not found" }, { status: 404 });
        }
        return HttpResponse.json(
          envelope({
            bucket: "reports",
            key: "memo.docx",
            format: "docx",
            size: 4096,
            kind: "document",
            text: "Quarterly memo body.",
            truncated: false,
            error: null,
          }),
        );
      }),
    );
    const modal = await openPreviewFor("memo.docx");
    expect(
      await within(modal).findByTestId("object-preview-document"),
    ).toHaveTextContent("Quarterly memo body.");
  });

  it("extracts Excel workbooks to a table", async () => {
    server.use(
      ...reads(listing([{ key: "data.xlsx", size: 8192, created_at: NOW, last_modified: NOW, etag: "x" }])),
      previewHandler("data.xlsx", {
        content_type:
          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        is_text: false,
      }),
      http.get(`${OS}/buckets/reports/object/extract`, () =>
        HttpResponse.json(
          envelope({
            bucket: "reports",
            key: "data.xlsx",
            format: "xlsx",
            size: 8192,
            kind: "sheet",
            sheets: [{ name: "Q1", rows: [["Region", "Total"], ["EMEA", "42"]] }],
            truncated: false,
            error: null,
          }),
        ),
      ),
    );
    const modal = await openPreviewFor("data.xlsx");
    const table = await within(modal).findByTestId("object-preview-table");
    expect(within(table).getByText("Region")).toBeInTheDocument();
    expect(within(table).getByText("EMEA")).toBeInTheDocument();
  });

  it("copies the selected keys joined by newlines via the bulk bar", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    server.use(...reads(listing(FILES)));
    const user = userEvent.setup();
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    render(<ObjectStore />);

    await user.click(await screen.findByTestId("bucket-reports"));
    await screen.findByText("alpha.txt");
    await user.click(screen.getByTestId("select-all"));
    await user.click(
      within(screen.getByTestId("bulk-bar")).getByRole("button", { name: /Copy keys/i }),
    );

    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith("alpha.txt\nbeta.log"),
    );
  });

  it("keeps the selected object in the router search params", async () => {
    server.use(...reads(listing(FILES)));
    const user = userEvent.setup();
    vi.spyOn(window, "open").mockImplementation(() => null);
    renderObjectStoreAt("/object-store");

    await user.click(await screen.findByTestId("bucket-reports"));
    await user.click(await screen.findByText("alpha.txt"));
    expect(screen.getByTestId("location-search")).toHaveTextContent("?bucket=reports&key=alpha.txt");

    await user.click(await screen.findByText("beta.log"));
    expect(screen.getByTestId("location-search")).toHaveTextContent("?bucket=reports&key=beta.log");
  });

  it("stores resized bucket pane widths and resets them on double click", async () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function rectForElement() {
      const className = (this as HTMLElement).getAttribute("class") ?? "";
      if (className.includes("object-store-resizable-shell")) {
        return {
          x: 100,
          y: 0,
          left: 100,
          top: 0,
          right: 1300,
          bottom: 800,
          width: 1200,
          height: 800,
          toJSON: () => ({}),
        } as DOMRect;
      }
      if (className.includes("object-store-browser-grid")) {
        return {
          x: 0,
          y: 0,
          left: 0,
          top: 0,
          right: 1200,
          bottom: 800,
          width: 1200,
          height: 800,
          toJSON: () => ({}),
        } as DOMRect;
      }
      return {
        x: 0,
        y: 0,
        left: 0,
        top: 0,
        right: 1200,
        bottom: 800,
        width: 1200,
        height: 800,
        toJSON: () => ({}),
      } as DOMRect;
    });

    server.use(...reads(listing(FILES)));
    render(<ObjectStore />);
    await screen.findByTestId("bucket-reports");

    const bucketResize = screen.getByLabelText("Resize bucket panel");
    fireEvent.pointerDown(bucketResize, { clientX: 420 });
    fireEvent.pointerMove(window, { clientX: 470 });
    fireEvent.pointerUp(window);
    await waitFor(() =>
      expect(window.localStorage.getItem("caliber.objectStore.bucketPaneWidth")).toBe("370"),
    );

    fireEvent.doubleClick(bucketResize);
    await waitFor(() =>
      expect(window.localStorage.getItem("caliber.objectStore.bucketPaneWidth")).toBe("300"),
    );
  });

  it("covers row controls, breadcrumbs, formatting variants, and drag upload", async () => {
    let uploadedPrefix: FormDataEntryValue | null = null;
    server.use(
      http.get(`${OS}/status`, () => HttpResponse.json(envelope(STATUS))),
      http.get(`${OS}/buckets`, () => HttpResponse.json(envelope(BUCKETS))),
      http.get(`${OS}/buckets/reports/objects`, ({ request }) => {
        const url = new URL(request.url);
        const prefix = url.searchParams.get("prefix") ?? "";
        if (prefix === "logs/") {
          return HttpResponse.json(
            envelope({
              bucket: "reports",
              prefix,
              prefixes: [],
              objects: [
                { key: "logs/app.py", size: 5 * 1024 * 1024 * 1024, last_modified: null, etag: "py" },
                { key: "logs/archive.zip", size: 2 * 1024 * 1024, last_modified: "not-a-date", etag: "zip" },
              ],
              next_token: null,
              is_truncated: false,
            }),
          );
        }
        return HttpResponse.json(envelope(listing([], ["logs/"])));
      }),
      http.post(`${OS}/buckets/reports/objects`, async ({ request }) => {
        const form = await request.formData();
        uploadedPrefix = form.get("prefix");
        return HttpResponse.json(envelope({ bucket: "reports", key: "logs/drop.txt", size: 4 }));
      }),
    );
    vi.stubGlobal("navigator", { ...navigator, clipboard: undefined });
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
    const user = userEvent.setup();
    render(<ObjectStore />);

    await user.click(await screen.findByTestId("bucket-reports"));
    await user.click(await screen.findByText("logs/"));
    expect(await screen.findByText("5.0 GB")).toBeInTheDocument();
    expect(screen.getByText("2.0 MB")).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2);

    await user.click(screen.getByRole("button", { name: /^Name$/i }));
    await user.click(screen.getByRole("button", { name: /^Name$/i }));
    await user.click(screen.getByLabelText("Select app.py"));
    await user.click(screen.getByLabelText("Select app.py"));
    await user.click(screen.getByTestId("select-all"));
    await user.click(screen.getByTestId("select-all"));

    await user.click(screen.getAllByRole("button", { name: "reports" })[1]!);
    await user.click(await screen.findByText("logs/"));
    await user.click(screen.getByRole("button", { name: "logs" }));

    const appRow = await screen.findByTestId("object-logs/app.py");
    await user.click(within(appRow).getByTitle("Open in new tab"));
    await waitFor(() =>
      expect(openSpy).toHaveBeenCalledWith(
        `${OS}/buckets/reports/object?key=logs%2Fapp.py&disposition=inline`,
        "_blank",
        "noopener,noreferrer",
      ),
    );
    await user.click(within(appRow).getByTitle("Copy key"));

    const dropZone = screen.getByTestId("object-drop-zone");
    fireEvent.dragOver(dropZone!, { dataTransfer: { files: [] } });
    expect(await screen.findByText(new RegExp("Drop files to upload to logs/"))).toBeInTheDocument();
    fireEvent.dragLeave(dropZone!, { dataTransfer: { files: [] } });
    expect(screen.queryByText(/Drop files to upload/)).not.toBeInTheDocument();

    fireEvent.dragOver(dropZone!, { dataTransfer: { files: [] } });
    fireEvent.drop(dropZone!, { dataTransfer: { files: [new File(["drop"], "drop.txt")] } });
    await waitFor(() => expect(uploadedPrefix).toBe("logs/"));
  });

  it("guards empty and cancelled destructive actions", async () => {
    let deleteCalls = 0;
    let folderCreates = 0;
    server.use(
      ...reads(listing(FILES, ["service/"])),
      http.post(`${OS}/buckets/reports/folders`, async ({ request }) => {
        folderCreates += 1;
        const body = (await request.json()) as { name?: string };
        return HttpResponse.json(envelope({ prefix: `${body.name}/` }), { status: 201 });
      }),
      http.post(`${OS}/buckets/reports/objects/delete`, () => {
        deleteCalls += 1;
        return HttpResponse.json(envelope({ deleted: 1, errors: [] }));
      }),
      http.delete(`${OS}/buckets/reports`, () => {
        deleteCalls += 1;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const user = userEvent.setup();
    render(<ObjectStore />);

    await user.click(await screen.findByTestId("bucket-reports"));
    await screen.findByText("alpha.txt");
    await user.click(screen.getByTitle("Create bucket"));
    await user.click(screen.getByTitle("Delete bucket"));
    await user.click(within(screen.getByTestId("object-alpha.txt")).getByTitle("Delete"));
    await user.click(screen.getByTestId("select-all"));
    await user.click(await screen.findByTestId("bulk-delete"));
    await user.click(screen.getByTitle("Delete folder and contents"));
    expect(deleteCalls).toBe(0);

    await user.click(screen.getByRole("button", { name: /New folder/i }));
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(screen.queryByTestId("new-folder-input")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /New folder/i }));
    await user.type(screen.getByTestId("new-folder-input"), "from-enter");
    fireEvent.keyDown(screen.getByTestId("new-folder-input"), { key: "Enter" });
    await waitFor(() => expect(folderCreates).toBe(1));

    await user.click(screen.getByRole("button", { name: /New folder/i }));
    fireEvent.keyDown(screen.getByTestId("new-folder-input"), { key: "Escape" });
    expect(screen.queryByTestId("new-folder-input")).not.toBeInTheDocument();
  });

  it("surfaces run, bulk delete, and folder delete errors", async () => {
    server.use(
      ...reads(listing(FILES, ["service/"])),
      http.post(`${OS}/buckets/reports/folders`, () =>
        HttpResponse.json({ detail: "folder denied" }, { status: 500 }),
      ),
      http.post(`${OS}/buckets/reports/objects/delete`, async ({ request }) => {
        const body = (await request.json()) as { keys?: string[]; prefix?: string };
        if (body.prefix) {
          return HttpResponse.json(envelope({ deleted: 2, errors: ["nested failed"] }));
        }
        return HttpResponse.json(envelope({ deleted: 1, errors: ["beta failed"] }));
      }),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<ObjectStore />);

    await user.click(await screen.findByTestId("bucket-reports"));
    await screen.findByText("alpha.txt");
    await user.click(screen.getByRole("button", { name: /New folder/i }));
    await user.type(screen.getByTestId("new-folder-input"), "blocked");
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByText("folder denied")).toBeInTheDocument();
    await user.click(screen.getByLabelText("Dismiss error"));

    await user.click(screen.getByTestId("select-all"));
    await user.click(await screen.findByTestId("bulk-delete"));
    expect(await screen.findByText("Deleted 1; 1 failed.")).toBeInTheDocument();
    await user.click(screen.getByLabelText("Dismiss error"));

    await user.click(screen.getByTitle("Delete folder and contents"));
    expect(await screen.findByText("Deleted 2; 1 failed.")).toBeInTheDocument();
  });

  it("covers refresh, upload proxy, folder search, cancel, and clipboard-missing branches", async () => {
    const inputClick = vi.spyOn(HTMLInputElement.prototype, "click").mockImplementation(() => {});
    Object.defineProperty(navigator, "clipboard", {
      value: undefined,
      configurable: true,
    });
    server.use(...reads(listing(FILES, ["logs/"])));
    const user = userEvent.setup();
    render(<ObjectStore />);

    await user.click(await screen.findByTestId("bucket-reports"));
    await screen.findByText("alpha.txt");
    await user.click(screen.getByTitle("Create bucket"));
    await user.click(screen.getByTitle("Refresh"));

    await user.type(screen.getByTestId("object-search"), "log");
    expect(await screen.findByText("logs/")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Upload/i }));
    expect(inputClick).toHaveBeenCalled();
    fireEvent.change(screen.getByTestId("upload-input"), { target: { files: [] } });

    await user.click(screen.getByRole("button", { name: /New folder/i }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByTestId("new-folder-input")).not.toBeInTheDocument();

    await user.clear(screen.getByTestId("object-search"));
    const row = await screen.findByTestId("object-alpha.txt");
    await user.click(within(row).getByTitle("Copy key"));
  });

  it("clears search and navigates back to the parent folder", async () => {
    server.use(
      http.get(`${OS}/status`, () => HttpResponse.json(envelope(STATUS))),
      http.get(`${OS}/buckets`, () => HttpResponse.json(envelope(BUCKETS))),
      http.get(`${OS}/buckets/reports/objects`, ({ request }) => {
        const url = new URL(request.url);
        const prefix = url.searchParams.get("prefix") ?? "";
        if (prefix === "service/") {
          return HttpResponse.json(
            envelope({
              bucket: "reports",
              prefix,
              prefixes: [],
              objects: [
                { key: "service/gamma.jsonl", size: 12, last_modified: NOW, etag: "g" },
              ],
              next_token: null,
              is_truncated: false,
            }),
          );
        }
        return HttpResponse.json(envelope(listing([], ["service/"])));
      }),
    );
    const user = userEvent.setup();
    render(<ObjectStore />);

    await user.click(await screen.findByTestId("bucket-reports"));
    await user.click(await screen.findByText("service/"));
    expect(await screen.findByText("gamma.jsonl")).toBeInTheDocument();

    await user.type(screen.getByTestId("object-search"), "missing");
    expect(await screen.findByText('No matches for "missing".')).toBeInTheDocument();
    await user.click(screen.getByLabelText("Clear search"));
    expect(screen.queryByText('No matches for "missing".')).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /\.\./ }));
    expect(await screen.findByText("service/")).toBeInTheDocument();
  });
});
