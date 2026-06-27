/**
 * Dedicated coverage tests for AttachmentBar — Aria's "+ add files"
 * affordance. Exercises the uncovered branches: the file-picker upload path
 * (onFilePicked → ensureSession → uploadAssistantAttachment), the attachment
 * chip list + remove/delete button, the add-context menu open/close, and the
 * three context modals (Paste text, Library resource, Object store) including
 * their loading / empty / error branches.
 */

import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { AttachmentBar } from "@/components/assistant/AttachmentBar";
import type { AssistantAttachment } from "@/api/assistantTypes";
import { render, screen, waitFor, within, userEvent } from "@/test/utils";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function attachment(overrides: Partial<AssistantAttachment> = {}): AssistantAttachment {
  return {
    attachment_id: "AATT-1",
    session_id: "ASST-1",
    kind: "upload",
    ref_type: "",
    ref_id: "",
    name: "report.pdf",
    content_text: "",
    bytes_size: 1234,
    truncated: false,
    metadata_: {},
    created_by: "@test",
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("AttachmentBar — add-context menu", () => {
  it("toggles the add-context menu open and closed", async () => {
    server.use(
      http.get(`${API_BASE}/assistant/sessions/:sessionId/attachments`, () =>
        HttpResponse.json(envelope([])),
      ),
    );
    const ensureSession = vi.fn<() => Promise<string>>().mockResolvedValue("ASST-1");
    render(<AttachmentBar sessionId="ASST-1" ensureSession={ensureSession} />);

    const trigger = screen.getByTestId("assistant-add-context");
    expect(screen.queryByText("Upload file")).not.toBeInTheDocument();

    await userEvent.click(trigger);
    expect(screen.getByText("Upload file")).toBeInTheDocument();
    expect(screen.getByText("Object store")).toBeInTheDocument();
    expect(screen.getByText("Library resource")).toBeInTheDocument();
    expect(screen.getByText("Paste text")).toBeInTheDocument();

    // Clicking the trigger again closes the menu.
    await userEvent.click(trigger);
    expect(screen.queryByText("Upload file")).not.toBeInTheDocument();
  });

  it("disables the trigger when the disabled prop is set", () => {
    server.use(
      http.get(`${API_BASE}/assistant/sessions/:sessionId/attachments`, () =>
        HttpResponse.json(envelope([])),
      ),
    );
    const ensureSession = vi.fn<() => Promise<string>>().mockResolvedValue("ASST-1");
    render(<AttachmentBar sessionId="ASST-1" ensureSession={ensureSession} disabled />);

    expect(screen.getByTestId("assistant-add-context")).toBeDisabled();
  });

  it("does not query attachments when there is no session", () => {
    // No GET handler is registered; onUnhandledRequest:"error" would trip if the
    // query fired. A null session disables the query entirely.
    const ensureSession = vi.fn<() => Promise<string>>().mockResolvedValue("ASST-1");
    render(<AttachmentBar sessionId={null} ensureSession={ensureSession} />);

    expect(screen.getByTestId("assistant-add-context")).toBeInTheDocument();
    expect(screen.queryByTestId("assistant-attachment-chips")).not.toBeInTheDocument();
  });
});

describe("AttachmentBar — upload via file picker", () => {
  it("uploads a picked file through ensureSession and lists the new chip", async () => {
    let uploaded = false;
    // The list query re-fetches after a successful upload (invalidate); serve
    // the new chip only once the upload POST has fired.
    server.use(
      http.get(`${API_BASE}/assistant/sessions/:sessionId/attachments`, () =>
        HttpResponse.json(envelope(uploaded ? [attachment({ name: "report.pdf" })] : [])),
      ),
      http.post(
        `${API_BASE}/assistant/sessions/:sessionId/attachments/upload`,
        () => {
          uploaded = true;
          return HttpResponse.json(envelope(attachment({ name: "report.pdf" })), {
            status: 201,
          });
        },
      ),
    );

    const ensureSession = vi.fn<() => Promise<string>>().mockResolvedValue("ASST-1");
    render(<AttachmentBar sessionId="ASST-1" ensureSession={ensureSession} />);

    const input = screen.getByLabelText("Upload a file to attach") as HTMLInputElement;
    const file = new File(["hello world"], "report.pdf", { type: "application/pdf" });
    await userEvent.upload(input, file);

    expect(ensureSession).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(screen.getByTestId("assistant-attachment-chips")).toHaveTextContent("report.pdf"),
    );
  });

  it("opens the file dialog when the Upload file menu item is clicked", async () => {
    server.use(
      http.get(`${API_BASE}/assistant/sessions/:sessionId/attachments`, () =>
        HttpResponse.json(envelope([])),
      ),
    );
    const ensureSession = vi.fn<() => Promise<string>>().mockResolvedValue("ASST-1");
    render(<AttachmentBar sessionId="ASST-1" ensureSession={ensureSession} />);

    const input = screen.getByLabelText("Upload a file to attach") as HTMLInputElement;
    const clickSpy = vi.spyOn(input, "click");

    await userEvent.click(screen.getByTestId("assistant-add-context"));
    await userEvent.click(screen.getByText("Upload file"));

    expect(clickSpy).toHaveBeenCalledTimes(1);
    // The menu closes after selecting an item.
    expect(screen.queryByText("Upload file")).not.toBeInTheDocument();
    clickSpy.mockRestore();
  });

  it("surfaces an upload failure without adding a chip", async () => {
    server.use(
      http.get(`${API_BASE}/assistant/sessions/:sessionId/attachments`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.post(`${API_BASE}/assistant/sessions/:sessionId/attachments/upload`, () =>
        HttpResponse.json({ detail: "file too large" }, { status: 413 }),
      ),
    );
    const ensureSession = vi.fn<() => Promise<string>>().mockResolvedValue("ASST-1");
    render(<AttachmentBar sessionId="ASST-1" ensureSession={ensureSession} />);

    const input = screen.getByLabelText("Upload a file to attach") as HTMLInputElement;
    await userEvent.upload(input, new File(["x"], "big.bin"));

    await waitFor(() => expect(ensureSession).toHaveBeenCalled());
    // No chips render on a failed upload.
    expect(screen.queryByTestId("assistant-attachment-chips")).not.toBeInTheDocument();
  });
});

describe("AttachmentBar — chip list & remove", () => {
  it("renders existing attachments and removes one via the delete button", async () => {
    let deleted = false;
    let deletedId: string | null = null;
    server.use(
      http.get(`${API_BASE}/assistant/sessions/:sessionId/attachments`, () =>
        HttpResponse.json(envelope(deleted ? [] : [attachment({ name: "spec.md", truncated: true })])),
      ),
      http.delete(`${API_BASE}/assistant/attachments/:attachmentId`, ({ params }) => {
        deleted = true;
        deletedId = String(params.attachmentId);
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const ensureSession = vi.fn<() => Promise<string>>().mockResolvedValue("ASST-1");
    render(<AttachmentBar sessionId="ASST-1" ensureSession={ensureSession} />);

    const chips = await screen.findByTestId("assistant-attachment-chips");
    expect(within(chips).getByText("spec.md")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Remove spec.md" }));

    await waitFor(() => expect(deleted).toBe(true));
    expect(deletedId).toBe("AATT-1");
    await waitFor(() =>
      expect(screen.queryByTestId("assistant-attachment-chips")).not.toBeInTheDocument(),
    );
  });
});

describe("AttachmentBar — Paste text modal", () => {
  it("keeps the modal open when attaching text fails", async () => {
    server.use(
      http.get(`${API_BASE}/assistant/sessions/:sessionId/attachments`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.post(`${API_BASE}/assistant/sessions/:sessionId/attachments`, () =>
        HttpResponse.json({ detail: "snippet rejected" }, { status: 400 }),
      ),
    );
    const ensureSession = vi.fn<() => Promise<string>>().mockResolvedValue("ASST-1");
    render(<AttachmentBar sessionId="ASST-1" ensureSession={ensureSession} />);

    await userEvent.click(screen.getByTestId("assistant-add-context"));
    await userEvent.click(screen.getByText("Paste text"));

    const modal = await screen.findByTestId("assistant-text-modal");
    await userEvent.type(within(modal).getByPlaceholderText("Paste context here…"), "some context");
    await userEvent.click(within(modal).getByRole("button", { name: "Attach" }));

    // The error branch leaves the modal mounted (only success closes it).
    await waitFor(() => expect(ensureSession).toHaveBeenCalled());
    expect(screen.getByTestId("assistant-text-modal")).toBeInTheDocument();
  });

  it("closes the modal via the Close button", async () => {
    server.use(
      http.get(`${API_BASE}/assistant/sessions/:sessionId/attachments`, () =>
        HttpResponse.json(envelope([])),
      ),
    );
    const ensureSession = vi.fn<() => Promise<string>>().mockResolvedValue("ASST-1");
    render(<AttachmentBar sessionId="ASST-1" ensureSession={ensureSession} />);

    await userEvent.click(screen.getByTestId("assistant-add-context"));
    await userEvent.click(screen.getByText("Paste text"));
    await screen.findByTestId("assistant-text-modal");

    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByTestId("assistant-text-modal")).not.toBeInTheDocument();
  });
});

describe("AttachmentBar — Library resource modal", () => {
  it("lists skills, switches resource type, and attaches a resource", async () => {
    let createdBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/assistant/sessions/:sessionId/attachments`, () =>
        HttpResponse.json(envelope([])),
      ),
      // Skills (default type) — global handler returns one, but override to be explicit.
      http.get(`${API_BASE}/skills`, () =>
        HttpResponse.json(
          envelope([
            {
              skill_id: "sk-001",
              name: "reasoning-v1",
              description: "",
              summary: "",
              content: "",
              owner: "@t",
              category: "custom",
              tags: [],
              skill_metadata: {},
              allowed_tools: null,
              depends_on: [],
              status: "active",
              version: 1,
              created_at: "2025-01-01T00:00:00Z",
              updated_at: "2025-01-01T00:00:00Z",
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflows`, () =>
        HttpResponse.json(
          envelope([
            {
              workflow_id: "WF-1",
              name: "ingest-flow",
              description: "",
              owner: "@t",
              status: "draft",
              latest_version: 1,
              created_at: "2025-01-01T00:00:00Z",
              updated_at: "2025-01-01T00:00:00Z",
            },
          ]),
        ),
      ),
      http.post(`${API_BASE}/assistant/sessions/:sessionId/attachments`, async ({ request }) => {
        createdBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope(attachment({ kind: "library_resource", name: "ingest-flow" })),
          { status: 201 },
        );
      }),
    );
    const ensureSession = vi.fn<() => Promise<string>>().mockResolvedValue("ASST-1");
    render(<AttachmentBar sessionId="ASST-1" ensureSession={ensureSession} />);

    await userEvent.click(screen.getByTestId("assistant-add-context"));
    await userEvent.click(screen.getByText("Library resource"));

    const modal = await screen.findByTestId("assistant-library-modal");
    // Default type is "skill": the skill row renders.
    expect(await within(modal).findByText("reasoning-v1")).toBeInTheDocument();

    // Switch to Workflows and attach the workflow.
    await userEvent.click(within(modal).getByRole("button", { name: "Workflows" }));
    const row = await within(modal).findByText("ingest-flow");
    await userEvent.click(row);

    await waitFor(() => expect(createdBody).not.toBeNull());
    expect(createdBody).toMatchObject({
      kind: "library_resource",
      resource_type: "workflow",
      resource_id: "WF-1",
    });
    // Success closes the modal.
    await waitFor(() =>
      expect(screen.queryByTestId("assistant-library-modal")).not.toBeInTheDocument(),
    );
  });

  it("shows the empty state when a resource type has no items", async () => {
    server.use(
      http.get(`${API_BASE}/assistant/sessions/:sessionId/attachments`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
    );
    const ensureSession = vi.fn<() => Promise<string>>().mockResolvedValue("ASST-1");
    render(<AttachmentBar sessionId="ASST-1" ensureSession={ensureSession} />);

    await userEvent.click(screen.getByTestId("assistant-add-context"));
    await userEvent.click(screen.getByText("Library resource"));

    const modal = await screen.findByTestId("assistant-library-modal");
    expect(await within(modal).findByText("No skills found.")).toBeInTheDocument();
  });

  it("shows an error state when the resource list fails to load", async () => {
    server.use(
      http.get(`${API_BASE}/assistant/sessions/:sessionId/attachments`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/tools`, () =>
        HttpResponse.json({ detail: "tools service down" }, { status: 500 }),
      ),
    );
    const ensureSession = vi.fn<() => Promise<string>>().mockResolvedValue("ASST-1");
    render(<AttachmentBar sessionId="ASST-1" ensureSession={ensureSession} />);

    await userEvent.click(screen.getByTestId("assistant-add-context"));
    await userEvent.click(screen.getByText("Library resource"));

    const modal = await screen.findByTestId("assistant-library-modal");
    await userEvent.click(within(modal).getByRole("button", { name: "Tools" }));

    expect(await within(modal).findByText(/Couldn't load tools/)).toBeInTheDocument();
  });
});

describe("AttachmentBar — Object store modal", () => {
  it("lists objects for a selected bucket and attaches one", async () => {
    let createdBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/assistant/sessions/:sessionId/attachments`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/object-store/buckets`, () =>
        HttpResponse.json(
          envelope([{ name: "documents", creation_date: "2025-01-01T00:00:00Z" }]),
        ),
      ),
      http.get(`${API_BASE}/object-store/buckets/:bucket/objects`, ({ params }) =>
        HttpResponse.json(
          envelope({
            bucket: String(params.bucket),
            prefix: "",
            prefixes: [],
            objects: [
              {
                key: "policy.txt",
                size: 42,
                created_at: "2025-01-01T00:00:00Z",
                last_modified: "2025-01-01T00:00:00Z",
                etag: "e1",
              },
            ],
            next_token: null,
            is_truncated: false,
          }),
        ),
      ),
      http.post(`${API_BASE}/assistant/sessions/:sessionId/attachments`, async ({ request }) => {
        createdBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope(attachment({ kind: "object_file", name: "policy.txt" })),
          { status: 201 },
        );
      }),
    );
    const ensureSession = vi.fn<() => Promise<string>>().mockResolvedValue("ASST-1");
    render(<AttachmentBar sessionId="ASST-1" ensureSession={ensureSession} />);

    await userEvent.click(screen.getByTestId("assistant-add-context"));
    await userEvent.click(screen.getByText("Object store"));

    const modal = await screen.findByTestId("assistant-object-modal");
    // Pick the bucket — the listing is only fetched once a bucket is selected.
    await userEvent.selectOptions(within(modal).getByRole("combobox"), "documents");

    const row = await within(modal).findByText("policy.txt");
    await userEvent.click(row);

    await waitFor(() => expect(createdBody).not.toBeNull());
    expect(createdBody).toMatchObject({
      kind: "object_file",
      bucket: "documents",
      key: "policy.txt",
    });
    await waitFor(() =>
      expect(screen.queryByTestId("assistant-object-modal")).not.toBeInTheDocument(),
    );
  });

  it("shows the empty state when the selected bucket has no files", async () => {
    server.use(
      http.get(`${API_BASE}/assistant/sessions/:sessionId/attachments`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/object-store/buckets`, () =>
        HttpResponse.json(
          envelope([{ name: "documents", creation_date: "2025-01-01T00:00:00Z" }]),
        ),
      ),
      http.get(`${API_BASE}/object-store/buckets/:bucket/objects`, ({ params }) =>
        HttpResponse.json(
          envelope({
            bucket: String(params.bucket),
            prefix: "",
            prefixes: [],
            objects: [],
            next_token: null,
            is_truncated: false,
          }),
        ),
      ),
    );
    const ensureSession = vi.fn<() => Promise<string>>().mockResolvedValue("ASST-1");
    render(<AttachmentBar sessionId="ASST-1" ensureSession={ensureSession} />);

    await userEvent.click(screen.getByTestId("assistant-add-context"));
    await userEvent.click(screen.getByText("Object store"));

    const modal = await screen.findByTestId("assistant-object-modal");
    await userEvent.selectOptions(within(modal).getByRole("combobox"), "documents");

    expect(await within(modal).findByText("No files at the bucket root.")).toBeInTheDocument();
  });

  it("shows an error state when the object listing fails", async () => {
    server.use(
      http.get(`${API_BASE}/assistant/sessions/:sessionId/attachments`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/object-store/buckets`, () =>
        HttpResponse.json(
          envelope([{ name: "documents", creation_date: "2025-01-01T00:00:00Z" }]),
        ),
      ),
      http.get(`${API_BASE}/object-store/buckets/:bucket/objects`, () =>
        HttpResponse.json({ detail: "bucket unreachable" }, { status: 502 }),
      ),
    );
    const ensureSession = vi.fn<() => Promise<string>>().mockResolvedValue("ASST-1");
    render(<AttachmentBar sessionId="ASST-1" ensureSession={ensureSession} />);

    await userEvent.click(screen.getByTestId("assistant-add-context"));
    await userEvent.click(screen.getByText("Object store"));

    const modal = await screen.findByTestId("assistant-object-modal");
    await userEvent.selectOptions(within(modal).getByRole("combobox"), "documents");

    expect(await within(modal).findByText(/Couldn't load objects/)).toBeInTheDocument();
  });
});
