import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import {
  afterAll,
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";

import type {
  IssuedPersonalAccessToken,
  PersonalAccessToken,
} from "@/api/types";
import { ApiTokensTab } from "@/components/ApiTokensTab";
import { showToast } from "@/lib/toast";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const SECRET = "calpat_super_secret_value_1234567890";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function metadata(
  overrides: Partial<PersonalAccessToken> = {},
): PersonalAccessToken {
  return {
    token_id: "PAT-1",
    user_id: "@ana",
    name: "ci-automation",
    scopes: ["caliber.viewer"],
    created_at: "2026-10-01T10:00:00Z",
    created_by: "@ana",
    expires_at: null,
    last_used_at: null,
    revoked_at: null,
    revoked_reason: null,
    rotated_from: null,
    active: true,
    project_id: null,
    ...overrides,
  };
}

interface Backend {
  tokens: PersonalAccessToken[];
  created: Array<Record<string, unknown>>;
  revoked: string[];
  rotated: string[];
}

function installBackend(
  seed: PersonalAccessToken[] = [],
  scopes: string[] = ["caliber.viewer", "caliber.operator"],
): Backend {
  const backend: Backend = {
    tokens: [...seed],
    created: [],
    revoked: [],
    rotated: [],
  };
  server.use(
    http.get(`${API_BASE}/me`, () =>
      HttpResponse.json(envelope({ user_id: "@ana", scopes, is_admin: false })),
    ),
    http.get(`${API_BASE}/auth/tokens`, () =>
      HttpResponse.json(envelope({ tokens: backend.tokens })),
    ),
    http.post(`${API_BASE}/auth/tokens`, async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      backend.created.push(body);
      const row = metadata({
        token_id: `PAT-${backend.tokens.length + 1}`,
        name: String(body["name"]),
        scopes: body["scopes"] as string[],
        expires_at: (body["expires_at"] as string | undefined) ?? null,
      });
      backend.tokens.push(row);
      const issued: IssuedPersonalAccessToken = { ...row, token: SECRET };
      return HttpResponse.json(envelope(issued), { status: 201 });
    }),
    http.delete(`${API_BASE}/auth/tokens/:id`, ({ params }) => {
      const id = String(params["id"]);
      backend.revoked.push(id);
      backend.tokens = backend.tokens.map((t) =>
        t.token_id === id
          ? { ...t, active: false, revoked_at: "2026-10-02T00:00:00Z" }
          : t,
      );
      return HttpResponse.json(envelope({ token_id: id, revoked: true }));
    }),
    http.post(`${API_BASE}/auth/tokens/:id/rotate`, ({ params }) => {
      const id = String(params["id"]);
      backend.rotated.push(id);
      const old = backend.tokens.find(
        (t) => t.token_id === id,
      ) as PersonalAccessToken;
      backend.tokens = backend.tokens.map((t) =>
        t.token_id === id
          ? {
              ...t,
              active: false,
              revoked_reason: "rotated",
              revoked_at: "2026-10-02T00:00:00Z",
            }
          : t,
      );
      const fresh = metadata({
        ...old,
        token_id: "PAT-ROT",
        rotated_from: id,
        active: true,
        revoked_at: null,
        revoked_reason: null,
      });
      backend.tokens.push(fresh);
      return HttpResponse.json(
        envelope({ ...fresh, token: `${SECRET}_rotated` }),
        { status: 201 },
      );
    }),
  );
  return backend;
}

function renderTab(): ReturnType<typeof render> {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ApiTokensTab />
    </QueryClientProvider>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
beforeEach(() => {
  vi.stubGlobal("navigator", {
    ...navigator,
    clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
  });
});
afterEach(() => {
  cleanup();
  server.resetHandlers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
afterAll(() => server.close());

describe("ApiTokensTab", () => {
  it("offers only the scopes the user holds, with viewer preselected", async () => {
    installBackend();
    renderTab();

    expect(
      await screen.findByRole("checkbox", { name: "caliber.viewer" }),
    ).toBeChecked();
    expect(
      screen.getByRole("checkbox", { name: "caliber.operator" }),
    ).not.toBeChecked();
    expect(
      screen.queryByRole("checkbox", { name: "caliber.admin" }),
    ).not.toBeInTheDocument();
  });

  it("shows the secret once after create, and never again once dismissed", async () => {
    const backend = installBackend();
    const user = userEvent.setup();
    renderTab();

    await user.type(
      await screen.findByLabelText("Token name"),
      "  deploy-bot  ",
    );
    await user.click(
      screen.getByRole("checkbox", { name: "caliber.operator" }),
    );
    await user.click(screen.getByRole("button", { name: "Create token" }));

    const secret = await screen.findByTestId("api-token-secret-value");
    expect(secret).toHaveTextContent(SECRET);
    expect(screen.getByText(/will not be shown again/i)).toBeInTheDocument();
    expect(backend.created).toHaveLength(1);
    expect(backend.created[0]).toMatchObject({
      name: "deploy-bot",
      scopes: ["caliber.operator", "caliber.viewer"],
    });
    expect(typeof backend.created[0]?.["expires_at"]).toBe("string");

    // The listing never carries it, and dismissing removes it from the page entirely.
    const row = await screen.findByTestId("api-token-row-PAT-1");
    expect(row).toHaveTextContent("deploy-bot");
    expect(row).not.toHaveTextContent(SECRET);
    await user.click(screen.getByRole("button", { name: "I have copied it" }));
    expect(screen.queryByTestId("api-token-secret")).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(SECRET);
  });

  it("copies the secret to the clipboard", async () => {
    installBackend();
    const user = userEvent.setup();
    // ``userEvent.setup()`` installs its own clipboard stub, so spy on that one.
    const writeText = vi
      .spyOn(navigator.clipboard, "writeText")
      .mockResolvedValue(undefined);
    renderTab();

    await user.type(await screen.findByLabelText("Token name"), "ci");
    await user.click(screen.getByRole("button", { name: "Create token" }));
    await user.click(await screen.findByRole("button", { name: "Copy" }));

    expect(writeText).toHaveBeenCalledWith(SECRET);
    expect(
      await screen.findByRole("button", { name: "Copied" }),
    ).toBeInTheDocument();
  });

  it("says so when the clipboard is unavailable", async () => {
    installBackend();
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(
      new Error("denied"),
    );
    const toast = vi
      .spyOn(showToast, "error")
      .mockImplementation(() => undefined);
    renderTab();

    await user.type(await screen.findByLabelText("Token name"), "ci");
    await user.click(screen.getByRole("button", { name: "Create token" }));
    await user.click(await screen.findByRole("button", { name: "Copy" }));

    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(
        expect.stringMatching(/copy it by hand/),
      ),
    );
    // The secret stays on screen so the user can still select it.
    expect(screen.getByTestId("api-token-secret-value")).toHaveTextContent(
      SECRET,
    );
  });

  it("omits expires_at for a token that never expires", async () => {
    const backend = installBackend();
    const user = userEvent.setup();
    renderTab();

    await user.type(await screen.findByLabelText("Token name"), "forever");
    await user.selectOptions(screen.getByLabelText("Token expiry"), "never");
    await user.click(screen.getByRole("button", { name: "Create token" }));

    await screen.findByTestId("api-token-secret");
    expect(backend.created[0]).not.toHaveProperty("expires_at");
  });

  it("requires a name and at least one scope before calling the server", async () => {
    const backend = installBackend();
    const user = userEvent.setup();
    renderTab();

    await user.click(
      await screen.findByRole("button", { name: "Create token" }),
    );
    expect(await screen.findByTestId("api-token-error")).toHaveTextContent(
      /name/i,
    );

    await user.type(screen.getByLabelText("Token name"), "x");
    await user.click(screen.getByRole("checkbox", { name: "caliber.viewer" }));
    await user.click(screen.getByRole("button", { name: "Create token" }));
    expect(await screen.findByTestId("api-token-error")).toHaveTextContent(
      /at least one scope/i,
    );
    expect(backend.created).toHaveLength(0);
  });

  it("surfaces the server's refusal for an over-scoped request", async () => {
    installBackend();
    server.use(
      http.post(`${API_BASE}/auth/tokens`, () =>
        HttpResponse.json(
          { detail: "cannot grant scopes you do not hold: ['caliber.admin']" },
          { status: 403 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderTab();

    await user.type(await screen.findByLabelText("Token name"), "too-much");
    await user.click(screen.getByRole("button", { name: "Create token" }));

    expect(await screen.findByTestId("api-token-error")).toHaveTextContent(
      "cannot grant scopes you do not hold: ['caliber.admin']",
    );
    expect(screen.queryByTestId("api-token-secret")).not.toBeInTheDocument();
  });

  it("revokes a token only after confirmation and then shows it as revoked", async () => {
    const backend = installBackend([metadata()]);
    const user = userEvent.setup();
    renderTab();

    await user.click(
      await screen.findByRole("button", { name: "Revoke ci-automation" }),
    );
    const dialog = await screen.findByTestId("confirm-dialog");
    expect(dialog).toHaveTextContent("stops working immediately");
    // Cancelling changes nothing.
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(backend.revoked).toEqual([]);

    await user.click(
      screen.getByRole("button", { name: "Revoke ci-automation" }),
    );
    await user.click(
      within(await screen.findByTestId("confirm-dialog")).getByRole("button", {
        name: "Revoke token",
      }),
    );

    await waitFor(() => expect(backend.revoked).toEqual(["PAT-1"]));
    const row = await screen.findByTestId("api-token-row-PAT-1");
    await waitFor(() => expect(row).toHaveTextContent("Revoked"));
    expect(
      screen.queryByRole("button", { name: "Revoke ci-automation" }),
    ).not.toBeInTheDocument();
  });

  it("rotates a token and shows the replacement secret once", async () => {
    const backend = installBackend([metadata()]);
    const user = userEvent.setup();
    renderTab();

    await user.click(
      await screen.findByRole("button", { name: "Rotate ci-automation" }),
    );

    expect(
      await screen.findByTestId("api-token-secret-value"),
    ).toHaveTextContent(`${SECRET}_rotated`);
    expect(backend.rotated).toEqual(["PAT-1"]);
    await waitFor(() =>
      expect(screen.getByTestId("api-token-row-PAT-1")).toHaveTextContent(
        "Rotated",
      ),
    );
    expect(screen.getByTestId("api-token-row-PAT-ROT")).toHaveTextContent(
      "Active",
    );
  });

  it("labels expired tokens and an empty list", async () => {
    installBackend([metadata({ expires_at: "2020-01-01T00:00:00Z" })]);
    renderTab();
    expect(await screen.findByTestId("api-token-row-PAT-1")).toHaveTextContent(
      "Expired",
    );
    cleanup();

    installBackend([]);
    renderTab();
    expect(
      await screen.findByText("You have not created any tokens yet."),
    ).toBeInTheDocument();
  });

  it("explains when the list cannot be loaded", async () => {
    installBackend();
    server.use(
      http.get(`${API_BASE}/auth/tokens`, () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    renderTab();
    expect(
      await screen.findByText(/Could not load your tokens/),
    ).toBeInTheDocument();
  });

  it("toasts when a revoke or rotate fails", async () => {
    installBackend([metadata()]);
    const toast = vi
      .spyOn(showToast, "error")
      .mockImplementation(() => undefined);
    server.use(
      http.delete(`${API_BASE}/auth/tokens/:id`, () =>
        HttpResponse.json({ detail: "nope" }, { status: 500 }),
      ),
      http.post(`${API_BASE}/auth/tokens/:id/rotate`, () =>
        HttpResponse.json({ detail: "nope" }, { status: 409 }),
      ),
    );
    const user = userEvent.setup();
    renderTab();

    await user.click(
      await screen.findByRole("button", { name: "Rotate ci-automation" }),
    );
    await user.click(
      await screen.findByRole("button", { name: "Revoke ci-automation" }),
    );
    await user.click(
      within(await screen.findByTestId("confirm-dialog")).getByRole("button", {
        name: "Revoke token",
      }),
    );

    // Both failures leave the token active and the page usable, and each says why.
    await waitFor(() =>
      expect(screen.queryByTestId("confirm-dialog")).not.toBeInTheDocument(),
    );
    expect(screen.getByTestId("api-token-row-PAT-1")).toHaveTextContent(
      "Active",
    );
    expect(toast).toHaveBeenCalledTimes(2);
    expect(toast).toHaveBeenCalledWith("nope");
  });
});
