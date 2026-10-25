import { http, HttpResponse } from "msw";
import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  it,
  vi,
} from "vitest";

import {
  BucketContentsField,
  BucketPrefixField,
  BucketSelect,
  BucketUploadField,
} from "@/components/workflows/BucketSelect";
import { render, screen, userEvent, waitFor } from "@/test/utils";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const OS = `${API_BASE}/object-store`;

function envelope<T>(data: T): { data: T } {
  return { data };
}

function bucketHandlers() {
  return [
    http.get(`${OS}/buckets`, () =>
      HttpResponse.json(envelope([{ name: "reports", creation_date: null }])),
    ),
    http.get(`${OS}/buckets/:bucket/objects`, ({ request, params }) => {
      const url = new URL(request.url);
      return HttpResponse.json(
        envelope({
          bucket: String(params.bucket),
          prefix: url.searchParams.get("prefix") ?? "",
          prefixes: [],
          objects: [
            { key: "service/", size: 0, last_modified: null, etag: "folder" },
            {
              key: "service/a.txt",
              size: 10,
              last_modified: "2026-06-10T12:00:00Z",
              etag: "a",
            },
            {
              key: "service/b.txt",
              size: 20,
              last_modified: "2026-06-10T12:01:00Z",
              etag: "b",
            },
          ],
          next_token: null,
          is_truncated: false,
        }),
      );
    }),
  ];
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
});
afterAll(() => server.close());

describe("BucketSelect workflow helpers", () => {
  it("creates a new bucket from the inline picker", async () => {
    let body: { name?: string } | null = null;
    const onChange = vi.fn();
    server.use(
      ...bucketHandlers(),
      http.post(`${OS}/buckets`, async ({ request }) => {
        body = (await request.json()) as { name?: string };
        return HttpResponse.json(envelope({ name: body.name }), {
          status: 201,
        });
      }),
    );
    const user = userEvent.setup();

    render(<BucketSelect value="" onChange={onChange} testId="bucket" />);

    await user.selectOptions(await screen.findByTestId("bucket"), "__create__");
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(screen.getByText("Enter a bucket name")).toBeInTheDocument();

    await user.type(screen.getByTestId("bucket-new"), "agent-artifacts");
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(body).toEqual({ name: "agent-artifacts" }));
    expect(onChange).toHaveBeenCalledWith("agent-artifacts");
  });

  it("surfaces a missing bucket and treats 409 create as adoption", async () => {
    const onChange = vi.fn();
    server.use(
      ...bucketHandlers(),
      http.post(`${OS}/buckets`, () =>
        HttpResponse.json(
          { detail: "already exists", status_code: 409 },
          { status: 409 },
        ),
      ),
    );
    const user = userEvent.setup();

    render(
      <BucketSelect
        value="existing-outside-cache"
        onChange={onChange}
        testId="bucket"
      />,
    );

    expect(await screen.findByText(/doesn't exist yet/i)).toBeInTheDocument();
    // findBy (not getBy): the "Create it" button can render a tick after the
    // "doesn't exist yet" hint, which races a synchronous query under CI load.
    await user.click(await screen.findByRole("button", { name: "Create it" }));

    await waitFor(() => expect(onChange).not.toHaveBeenCalled());
    expect(
      screen.queryByText(/Failed to create bucket/i),
    ).not.toBeInTheDocument();
  });

  it("creates nested prefixes one segment at a time", async () => {
    const calls: Array<{ prefix?: string; name?: string }> = [];
    server.use(
      http.post(`${OS}/buckets/reports/folders`, async ({ request }) => {
        calls.push(
          (await request.json()) as { prefix?: string; name?: string },
        );
        return HttpResponse.json(envelope({ prefix: "service/2026/" }), {
          status: 201,
        });
      }),
    );
    const user = userEvent.setup();

    render(
      <BucketPrefixField
        bucket="reports"
        value="service/2026"
        onChange={vi.fn()}
        testId="prefix"
      />,
    );

    await user.click(await screen.findByTestId("prefix-create"));

    await waitFor(() =>
      expect(calls).toEqual([
        { prefix: "", name: "service" },
        { prefix: "service/", name: "2026" },
      ]),
    );
    expect(screen.getByText("Folder service/2026/ ready")).toBeInTheDocument();
  });

  it("previews object contents and uploads into the selected prefix", async () => {
    let uploadedPrefix: FormDataEntryValue | null = null;
    server.use(
      ...bucketHandlers(),
      http.post(`${OS}/buckets/reports/objects`, async ({ request }) => {
        const form = await request.formData();
        uploadedPrefix = form.get("prefix");
        return HttpResponse.json(
          envelope({ bucket: "reports", key: "service/new.txt", size: 3 }),
        );
      }),
    );
    const user = userEvent.setup();

    render(
      <BucketContentsField
        bucket="reports"
        prefix="service/"
        previewLimit={1}
        testId="contents"
      />,
    );

    expect(await screen.findByText(/2 objects/i)).toBeInTheDocument();
    expect(screen.getByText(/a.txt/)).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /Open in Object Store/i }),
    ).toHaveAttribute(
      "href",
      "/caliber/object-store?bucket=reports&prefix=service%2F",
    );

    // findBy, not getBy: the upload control is gated on ``/me`` resolving
    // ``is_admin``, which is a different request from the object listing awaited
    // above. Querying it synchronously races those two, and loses under CI load.
    await user.upload(
      await screen.findByTestId("contents-upload-input"),
      new File(["hey"], "new.txt", { type: "text/plain" }),
    );

    await waitFor(() => expect(uploadedPrefix).toBe("service/"));
    expect(await screen.findByText("Uploaded 1 object")).toBeInTheDocument();
  });

  it("hides upload controls for non-admin users", async () => {
    server.use(
      ...bucketHandlers(),
      http.get(`${API_BASE}/me`, () =>
        HttpResponse.json(
          envelope({
            user_id: "@viewer",
            scopes: ["caliber.viewer"],
            is_admin: false,
          }),
        ),
      ),
    );

    render(<BucketUploadField bucket="reports" prefix="" testId="upload" />);

    await waitFor(() => {
      expect(screen.queryByTestId("upload")).not.toBeInTheDocument();
    });
  });

  it("covers bucket loading, create, and missing-bucket failures", async () => {
    server.use(
      http.get(`${OS}/buckets`, () =>
        HttpResponse.json({ detail: "bucket service unavailable" }, { status: 503 }),
      ),
      http.post(`${OS}/buckets`, () =>
        HttpResponse.json({ detail: "create denied" }, { status: 403 }),
      ),
    );
    const user = userEvent.setup();

    render(<BucketSelect value="missing" onChange={vi.fn()} testId="bucket" />);
    expect(
      await screen.findByText("Failed to load buckets: bucket service unavailable"),
    ).toBeInTheDocument();

    await user.selectOptions(await screen.findByTestId("bucket"), "__create__");
    await user.type(screen.getByTestId("bucket-new"), "new-bucket");
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByText("create denied")).toBeInTheDocument();
  });

  it("reports missing-bucket creation errors and hides write controls for viewers", async () => {
    server.use(
      http.get(`${OS}/buckets`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/me`, () =>
        HttpResponse.json(
          envelope({ user_id: "@viewer", scopes: ["caliber.viewer"], is_admin: false }),
        ),
      ),
    );
    render(<BucketSelect value="missing" onChange={vi.fn()} testId="bucket" />);

    expect(await screen.findByText(/doesn't exist yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create it" })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /Create new bucket/ })).not.toBeInTheDocument();

  });

  it("covers folder conflict recovery, folder failures, and sparse contents states", async () => {
    let folderCalls = 0;
    server.use(
      http.post(`${OS}/buckets/reports/folders`, () => {
        folderCalls += 1;
        return HttpResponse.json(
          envelope({ prefix: folderCalls === 2 ? "service/" : "service/2026/" }),
          { status: folderCalls === 2 ? 409 : 201 },
        );
      }),
      http.get(`${OS}/buckets/reports/objects`, () =>
        HttpResponse.json(
          envelope({
            bucket: "reports",
            prefix: "",
            prefixes: [],
            objects: [
              { key: "one.txt", size: 1, last_modified: null, etag: "one" },
              { key: "two.txt", size: 2, last_modified: null, etag: "two" },
            ],
            next_token: "next",
            is_truncated: true,
          }),
        ),
      ),
    );
    const user = userEvent.setup();

    render(
      <BucketPrefixField
        bucket="reports"
        value="service/2026"
        onChange={vi.fn()}
        testId="prefix"
      />,
    );
    await user.click(await screen.findByTestId("prefix-create"));
    await waitFor(() => expect(folderCalls).toBe(2));
    expect(await screen.findByText("Folder service/2026/ ready")).toBeInTheDocument();

    render(<BucketContentsField bucket="reports" prefix="" previewLimit={1} testId="contents" />);
    expect(await screen.findByText(/2\+ objects/)).toBeInTheDocument();
    expect(screen.getByText(/one\.txt.*…/)).toBeInTheDocument();
  });

  it("covers empty, failed, and viewer-only contents states", async () => {
    server.use(
      http.get(`${OS}/buckets/empty/objects`, () =>
        HttpResponse.json({ detail: "object listing unavailable" }, { status: 503 }),
      ),
      http.get(`${API_BASE}/me`, () =>
        HttpResponse.json(envelope({ user_id: "@viewer", is_admin: false })),
      ),
    );

    const empty = render(<BucketContentsField bucket="" prefix="" testId="empty" />);
    expect(screen.getByText("Select a bucket to see its contents.")).toBeInTheDocument();
    expect(screen.queryByTestId("empty-upload")).not.toBeInTheDocument();
    empty.unmount();

    render(<BucketContentsField bucket="empty" prefix="" testId="failed" />);
    expect(await screen.findByText("object listing unavailable")).toBeInTheDocument();
    expect(screen.queryByTestId("failed-upload")).not.toBeInTheDocument();
  });

  it("covers empty object messaging and multi-file upload failures", async () => {
    const uploaded: string[] = [];
    server.use(
      http.get(`${OS}/buckets/empty/objects`, () =>
        HttpResponse.json(
          envelope({
            bucket: "empty",
            prefix: "inbox/",
            prefixes: [],
            objects: [],
            next_token: null,
            is_truncated: false,
          }),
        ),
      ),
      http.post(`${OS}/buckets/empty/objects`, async ({ request }) => {
        const form = await request.formData();
        uploaded.push(String(form.get("file")));
        return HttpResponse.json(envelope({ ok: true }));
      }),
    );
    const user = userEvent.setup();
    const onUploaded = vi.fn();
    render(<BucketContentsField bucket="empty" prefix="inbox/" testId="empty" />);
    expect(await screen.findByText(/No objects under this prefix yet — upload one below/)).toBeInTheDocument();
    await user.upload(
      await screen.findByTestId("empty-upload-input"),
      [new File(["a"], "a.txt", { type: "text/plain" }), new File(["b"], "b.txt", { type: "text/plain" })],
    );
    await waitFor(() => expect(uploaded).toHaveLength(2));
    expect(await screen.findByText("Uploaded 2 objects")).toBeInTheDocument();
    expect(onUploaded).not.toHaveBeenCalled();

    server.use(
      http.post(`${OS}/buckets/empty/objects`, () =>
        HttpResponse.json({ detail: "upload rejected" }, { status: 422 }),
      ),
    );
    await user.upload(
      await screen.findByTestId("empty-upload-input"),
      new File(["c"], "c.txt", { type: "text/plain" }),
    );
    expect(await screen.findByText("upload rejected")).toBeInTheDocument();
  });

  it("covers prefix guards and generic folder failures", async () => {
    const user = userEvent.setup();
    const { rerender } = render(
      <BucketPrefixField bucket="" value="" onChange={vi.fn()} testId="prefix" />,
    );
    await waitFor(() => expect(screen.getByTestId("prefix-create")).toBeDisabled());
    rerender(<BucketPrefixField bucket="reports" value="inbox" onChange={vi.fn()} testId="prefix" />);
    server.use(
      http.post(`${OS}/buckets/reports/folders`, () =>
        HttpResponse.json({ detail: "folder creation denied" }, { status: 403 }),
      ),
    );
    await user.click(screen.getByTestId("prefix-create"));
    expect(await screen.findByText("folder creation denied")).toBeInTheDocument();
  });
});
