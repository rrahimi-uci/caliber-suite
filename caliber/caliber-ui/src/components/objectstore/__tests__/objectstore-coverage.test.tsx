import { http, HttpResponse } from "msw";
import { useState } from "react";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import {
  DataTable,
  MarkdownView,
  parseDelimited,
} from "@/components/objectstore/previewRenderers";
import { BucketTree } from "@/components/knowledge/BucketTree";
import type { KnowledgeSourceSelection } from "@/api/knowledgeTypes";
import type {
  ObjectStoreListing,
  ObjectStoreSheet,
} from "@/api/workflowTypes";
import { render, screen, userEvent, waitFor, within } from "@/test/utils";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const OS = `${API_BASE}/object-store`;

function envelope<T>(data: T): { data: T } {
  return { data };
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
});
afterAll(() => server.close());

// ── previewRenderers: MarkdownView ───────────────────────────────────────────

describe("MarkdownView", () => {
  it("renders an empty-file fallback when there is no content", () => {
    render(<MarkdownView source="" />);
    expect(screen.getByText("(empty file)")).toBeInTheDocument();
  });

  it("renders headings, emphasis, inline code, and safe links", () => {
    const source = [
      "# Title",
      "",
      "Some **bold** and *italic* and `code` text.",
      "",
      "[docs](https://example.com) and [bad](javascript:alert(1)) links.",
    ].join("\n");
    render(<MarkdownView source={source} />);

    expect(
      screen.getByRole("heading", { level: 1, name: "Title" }),
    ).toBeInTheDocument();
    expect(screen.getByText("bold").tagName).toBe("STRONG");
    expect(screen.getByText("italic").tagName).toBe("EM");
    expect(screen.getByText("code").tagName).toBe("CODE");

    // Safe href becomes an anchor; the javascript: scheme is rejected to text.
    const link = screen.getByRole("link", { name: "docs" });
    expect(link).toHaveAttribute("href", "https://example.com");
    expect(screen.queryByRole("link", { name: "bad" })).not.toBeInTheDocument();
    expect(screen.getByText("bad")).toBeInTheDocument();
  });

  it("renders fenced code, lists, blockquotes, horizontal rules, and tables", () => {
    const source = [
      "```",
      "const x = 1;",
      "```",
      "",
      "- first",
      "- second",
      "",
      "1. one",
      "2. two",
      "",
      "> a quoted line",
      "",
      "---",
      "",
      "| Name | Age |",
      "| --- | --- |",
      "| Ada | 36 |",
    ].join("\n");
    const { container } = render(<MarkdownView source={source} />);

    expect(screen.getByText("const x = 1;").closest("pre")).not.toBeNull();
    expect(screen.getByText("first")).toBeInTheDocument();
    expect(screen.getByText("one").closest("ol")).not.toBeNull();
    expect(screen.getByText("a quoted line").closest("blockquote")).not.toBeNull();
    expect(container.querySelector("hr")).not.toBeNull();

    const table = screen.getByRole("table");
    expect(within(table).getByText("Name")).toBeInTheDocument();
    expect(within(table).getByText("Ada")).toBeInTheDocument();
    expect(within(table).getByText("36")).toBeInTheDocument();
  });

  it("renders markdown images with a safe src", () => {
    render(<MarkdownView source="![alt text](https://example.com/x.png)" />);
    const img = screen.getByRole("img", { name: "alt text" });
    expect(img).toHaveAttribute("src", "https://example.com/x.png");
  });
});

// ── previewRenderers: parseDelimited ─────────────────────────────────────────

describe("parseDelimited", () => {
  it("parses quoted fields with embedded delimiters and newlines", () => {
    const csv = 'a,"b,c",d\n"line\nbreak",e,f\n';
    expect(parseDelimited(csv, ",")).toEqual([
      ["a", "b,c", "d"],
      ["line\nbreak", "e", "f"],
    ]);
  });

  it("unescapes doubled quotes and drops fully-empty rows", () => {
    const csv = '"say ""hi""",x\n,,\n\n';
    expect(parseDelimited(csv, ",")).toEqual([['say "hi"', "x"]]);
  });
});

// ── previewRenderers: DataTable ──────────────────────────────────────────────

describe("DataTable", () => {
  it("shows a no-rows message when there are no sheets", () => {
    render(<DataTable sheets={[]} />);
    expect(screen.getByText("No rows to display.")).toBeInTheDocument();
    expect(screen.queryByTestId("object-preview-table")).not.toBeInTheDocument();
  });

  it("renders a single sheet without a switcher and fills ragged rows", () => {
    const sheets: ObjectStoreSheet[] = [
      { name: "Sheet1", rows: [["Name", "Age"], ["Ada", "36"], ["Solo"]] },
    ];
    render(<DataTable sheets={sheets} />);

    const table = screen.getByTestId("object-preview-table");
    expect(within(table).getByText("Name")).toBeInTheDocument();
    expect(within(table).getByText("Ada")).toBeInTheDocument();
    // No sheet-switcher buttons for a single sheet.
    expect(screen.queryByRole("button", { name: "Sheet1" })).not.toBeInTheDocument();
  });

  it("switches between sheets via the tab buttons", async () => {
    const sheets: ObjectStoreSheet[] = [
      { name: "Alpha", rows: [["A1"], ["a-body"]] },
      { name: "Beta", rows: [["B1"], ["b-body"]] },
    ];
    const user = userEvent.setup();
    render(<DataTable sheets={sheets} truncated />);

    // Starts on the first sheet.
    expect(screen.getByText("a-body")).toBeInTheDocument();
    expect(screen.queryByText("b-body")).not.toBeInTheDocument();
    expect(screen.getByText(/truncated view/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Beta" }));

    expect(screen.getByText("b-body")).toBeInTheDocument();
    expect(screen.queryByText("a-body")).not.toBeInTheDocument();
  });

  it("renders body rows even when a sheet has no header row", () => {
    const sheets: ObjectStoreSheet[] = [{ name: "Empty", rows: [] }];
    render(<DataTable sheets={sheets} />);
    expect(screen.getByTestId("object-preview-table")).toBeInTheDocument();
  });
});

// ── BucketTree ───────────────────────────────────────────────────────────────

function listing(
  partial: Partial<ObjectStoreListing> & { bucket?: string; prefix?: string },
): ObjectStoreListing {
  return {
    bucket: partial.bucket ?? "docs",
    prefix: partial.prefix ?? "",
    prefixes: partial.prefixes ?? [],
    objects: partial.objects ?? [],
    next_token: partial.next_token ?? null,
    is_truncated: partial.is_truncated ?? false,
  };
}

/** Stateful selection harness so toggle/bulk actually flip checkbox state. */
function Harness(props: { bucket: string; filter?: string }): JSX.Element {
  const [selected, setSelected] = useState<KnowledgeSourceSelection[]>([]);
  const key = (s: KnowledgeSourceSelection): string => `${s.kind}:${s.path}`;
  return (
    <div>
      <span data-testid="selected-count">{selected.length}</span>
      <BucketTree
        bucket={props.bucket}
        filter={props.filter}
        isSelected={(s) => selected.some((x) => key(x) === key(s))}
        onToggle={(s) =>
          setSelected((cur) =>
            cur.some((x) => key(x) === key(s))
              ? cur.filter((x) => key(x) !== key(s))
              : [...cur, s],
          )
        }
        onBulk={(sources, select) =>
          setSelected(() => (select ? [...sources] : []))
        }
      />
    </div>
  );
}

describe("BucketTree", () => {
  it("prompts to pick a bucket when none is provided", () => {
    render(<Harness bucket="" />);
    expect(
      screen.getByText(/Select a bucket to browse/i),
    ).toBeInTheDocument();
  });

  it("shows a loading state while the listing resolves", async () => {
    server.use(
      http.get(`${OS}/buckets/:bucket/objects`, async () => {
        await new Promise((r) => setTimeout(r, 30));
        return HttpResponse.json(envelope(listing({})));
      }),
    );
    render(<Harness bucket="docs" />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByText(/No folders or files/i)).toBeInTheDocument(),
    );
  });

  it("shows the empty-bucket message when there is nothing to browse", async () => {
    server.use(
      http.get(`${OS}/buckets/:bucket/objects`, () =>
        HttpResponse.json(envelope(listing({}))),
      ),
    );
    render(<Harness bucket="docs" />);
    expect(
      await screen.findByText(/No folders or files in this bucket/i),
    ).toBeInTheDocument();
    // Select-all checkbox is disabled when there is nothing to select.
    expect(screen.getByTestId("kb-tree-select-all")).toBeDisabled();
  });

  it("lists top-level folders and files with a size label and count", async () => {
    server.use(
      http.get(`${OS}/buckets/:bucket/objects`, () =>
        HttpResponse.json(
          envelope(
            listing({
              prefixes: ["reports/"],
              objects: [
                { key: "readme.md", size: 2048, last_modified: null, etag: "r" },
              ],
            }),
          ),
        ),
      ),
    );
    render(<Harness bucket="docs" />);

    expect(await screen.findByText("reports")).toBeInTheDocument();
    expect(screen.getByText("readme.md")).toBeInTheDocument();
    expect(screen.getByText("2.0 KB")).toBeInTheDocument();
    expect(screen.getByText(/1 folder · 1 file/)).toBeInTheDocument();
  });

  it("filters the top-level view by label (case-insensitive)", async () => {
    server.use(
      http.get(`${OS}/buckets/:bucket/objects`, () =>
        HttpResponse.json(
          envelope(
            listing({
              prefixes: ["Reports/", "logs/"],
              objects: [
                { key: "notes.txt", size: 10, last_modified: null, etag: "n" },
              ],
            }),
          ),
        ),
      ),
    );
    render(<Harness bucket="docs" filter="report" />);

    expect(await screen.findByText("Reports")).toBeInTheDocument();
    expect(screen.queryByText("logs")).not.toBeInTheDocument();
    expect(screen.queryByText("notes.txt")).not.toBeInTheDocument();
  });

  it("toggles file selection and bulk-selects the whole view", async () => {
    server.use(
      http.get(`${OS}/buckets/:bucket/objects`, () =>
        HttpResponse.json(
          envelope(
            listing({
              prefixes: ["reports/"],
              objects: [
                { key: "a.txt", size: 5, last_modified: null, etag: "a" },
              ],
            }),
          ),
        ),
      ),
    );
    const user = userEvent.setup();
    render(<Harness bucket="docs" />);

    const fileBox = await screen.findByLabelText("Select file a.txt");
    expect(fileBox).not.toBeChecked();
    await user.click(fileBox);
    expect(fileBox).toBeChecked();
    expect(screen.getByTestId("selected-count")).toHaveTextContent("1");

    // Select all in view replaces the selection with every entry (folder + file).
    await user.click(screen.getByTestId("kb-tree-select-all"));
    expect(screen.getByTestId("selected-count")).toHaveTextContent("2");
    expect(screen.getByTestId("kb-tree-select-all")).toBeChecked();

    // Toggling it again clears the selection.
    await user.click(screen.getByTestId("kb-tree-select-all"));
    expect(screen.getByTestId("selected-count")).toHaveTextContent("0");
  });

  it("toggles folder selection from the folder checkbox", async () => {
    server.use(
      http.get(`${OS}/buckets/:bucket/objects`, () =>
        HttpResponse.json(
          envelope(listing({ prefixes: ["reports/"] })),
        ),
      ),
    );
    const user = userEvent.setup();
    render(<Harness bucket="docs" />);

    const folderBox = await screen.findByLabelText("Select folder reports");
    await user.click(folderBox);
    expect(folderBox).toBeChecked();
    expect(screen.getByTestId("selected-count")).toHaveTextContent("1");
  });

  it("lazily loads a folder's children on first expand and collapses again", async () => {
    let subCalls = 0;
    server.use(
      http.get(`${OS}/buckets/:bucket/objects`, ({ request }) => {
        const prefix = new URL(request.url).searchParams.get("prefix") ?? "";
        if (prefix === "") {
          return HttpResponse.json(
            envelope(listing({ prefixes: ["reports/"] })),
          );
        }
        subCalls += 1;
        return HttpResponse.json(
          envelope(
            listing({
              prefix: "reports/",
              objects: [
                {
                  key: "reports/q1.csv",
                  size: 12,
                  last_modified: null,
                  etag: "q",
                },
              ],
            }),
          ),
        );
      }),
    );
    const user = userEvent.setup();
    render(<Harness bucket="docs" />);

    const expandBtn = await screen.findByRole("button", {
      name: "Expand folder",
    });
    await user.click(expandBtn);

    expect(await screen.findByText("q1.csv")).toBeInTheDocument();
    expect(subCalls).toBe(1);

    // Collapsing hides the child again (button label flips back).
    await user.click(screen.getByRole("button", { name: "Collapse folder" }));
    await waitFor(() =>
      expect(screen.queryByText("q1.csv")).not.toBeInTheDocument(),
    );
  });

  it("shows an empty-folder note when an expanded folder has no children", async () => {
    server.use(
      http.get(`${OS}/buckets/:bucket/objects`, ({ request }) => {
        const prefix = new URL(request.url).searchParams.get("prefix") ?? "";
        if (prefix === "") {
          return HttpResponse.json(
            envelope(listing({ prefixes: ["empty/"] })),
          );
        }
        return HttpResponse.json(
          envelope(listing({ prefix: "empty/" })),
        );
      }),
    );
    const user = userEvent.setup();
    render(<Harness bucket="docs" />);

    await user.click(
      await screen.findByRole("button", { name: "Expand folder" }),
    );
    expect(await screen.findByText("Empty folder.")).toBeInTheDocument();
  });
});
