/**
 * Administration page — accounts and secrets.
 *
 * The identity and secret stores shipped API-only, which is why the report kept
 * Platform UX and the end-to-end lifecycle scored down: a product whose answer to
 * "add a second user" is `curl` has a real hole in its low-code claim.
 *
 * The assertions worth having here are the *negative* ones. Rendering a table is
 * unlikely to regress silently; leaking a credential into the DOM is exactly the kind
 * of thing that regresses silently, so this file pins:
 *
 * - a secret value is never displayed (the API returns metadata only, and the page has
 *   no field to render one into);
 * - a password is cleared from its input on success rather than left in the DOM; and
 * - a disabled store and an errored request are distinguishable from "nothing here",
 *   because an empty table reads as "no secrets" when it may mean "you may not look".
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import {
  clearLocalAuthSession,
  createLocalAuthSession,
  getStoredAuthSession,
  saveLocalAuthSession,
} from "@/auth/localAuth";
import { Administration } from "@/pages/Administration";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function renderPage(): void {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        initialEntries={["/administration"]}
      >
        <Routes>
          <Route path="/administration" element={<Administration />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function stubStores(
  options: {
    accounts?: unknown[];
    secrets?: unknown[];
    secretsEnabled?: boolean;
    projects?: unknown[];
  } = {},
): void {
  const accounts = options.accounts ?? [];
  const secrets = options.secrets ?? [];
  const projects = options.projects ?? [];
  server.use(
    http.get(`${API_BASE}/auth/accounts`, () =>
      HttpResponse.json(envelope({ accounts, total: accounts.length })),
    ),
    http.get(`${API_BASE}/secrets`, () =>
      HttpResponse.json(
        envelope({
          secrets,
          total: secrets.length,
          enabled: options.secretsEnabled ?? true,
          reference_scheme: "secret://",
        }),
      ),
    ),
    http.get(`${API_BASE}/projects`, () =>
      HttpResponse.json(envelope(projects)),
    ),
    http.get(`${API_BASE}/projects/:projectId/members`, () =>
      HttpResponse.json(envelope({ members: [] })),
    ),
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  clearLocalAuthSession();
});
afterAll(() => server.close());

describe("Administration", () => {
  it("shows project roles and member management in the administration surface", async () => {
    stubStores({
      projects: [
        {
          project_id: "PRJ-1",
          name: "Support",
          description: "",
          owner: "@admin",
          status: "active",
          permissions: ["read", "project.manage_members"],
          access_role: "owner",
        },
      ],
    });
    server.use(
      http.get(`${API_BASE}/projects/PRJ-1/members`, () =>
        HttpResponse.json(
          envelope({
            members: [
              {
                member_id: "PRJM-1",
                project_id: "PRJ-1",
                user_id: "@admin",
                role: "owner",
                status: "active",
                created_by: "@admin",
                created_at: null,
                updated_at: null,
              },
            ],
          }),
        ),
      ),
    );
    renderPage();

    expect(await screen.findByText("Project access")).toBeInTheDocument();
    expect(await screen.findByText("@admin")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add member" })).toBeInTheDocument();
    expect(screen.getByText("Manage who can read, edit, review, and publish resources in each project.")).toBeInTheDocument();
  });

  it("lets the primary owner manage a secondary Admin and transfer ownership, but not their own row", async () => {
    // `P1-C`: multiple active `owner`-role (Admin) memberships are allowed
    // alongside `project.owner`'s one primary-owner pointer -- the old
    // `member.role !== "owner"` gate would have hidden every control for
    // *both* rows here, since both hold the `owner` role.
    saveLocalAuthSession(createLocalAuthSession("admin"));
    stubStores({
      projects: [
        {
          project_id: "PRJ-1",
          name: "Support",
          description: "",
          owner: "@admin",
          status: "active",
          permissions: ["read", "project.manage_members"],
          access_role: "owner",
        },
      ],
    });
    server.use(
      http.get(`${API_BASE}/projects/PRJ-1/members`, () =>
        HttpResponse.json(
          envelope({
            members: [
              {
                member_id: "PRJM-1",
                project_id: "PRJ-1",
                user_id: "@admin",
                role: "owner",
                status: "active",
                created_by: "@admin",
                created_at: null,
                updated_at: null,
              },
              {
                member_id: "PRJM-2",
                project_id: "PRJ-1",
                user_id: "@second-admin",
                role: "owner",
                status: "active",
                created_by: "@admin",
                created_at: null,
                updated_at: null,
              },
            ],
          }),
        ),
      ),
    );
    let transferBody: unknown;
    server.use(
      http.post(
        `${API_BASE}/projects/PRJ-1/transfer-ownership`,
        async ({ request }) => {
          transferBody = await request.json();
          return HttpResponse.json(
            envelope({ project_id: "PRJ-1", owner: "@second-admin" }),
          );
        },
      ),
    );
    renderPage();

    const primaryRow = (await screen.findByText("@admin")).closest("tr");
    const secondaryRow = (await screen.findByText("@second-admin")).closest(
      "tr",
    );
    expect(primaryRow).not.toBeNull();
    expect(secondaryRow).not.toBeNull();

    // The primary owner's own row has no role select or Remove button.
    expect(primaryRow).toHaveTextContent("(primary owner)");
    expect(
      within(primaryRow as HTMLElement).queryByRole("combobox"),
    ).not.toBeInTheDocument();
    expect(
      within(primaryRow as HTMLElement).queryByRole("button", {
        name: "Remove",
      }),
    ).not.toBeInTheDocument();

    // The secondary Admin's row is fully manageable: a role select
    // (offering "Owner (Admin)"), a Remove button, and -- because the
    // viewer *is* the current primary owner -- a transfer action.
    const secondarySelect = within(secondaryRow as HTMLElement).getByRole(
      "combobox",
    );
    expect(
      within(secondarySelect as HTMLElement).getByRole("option", {
        name: "Owner (Admin)",
      }),
    ).toBeInTheDocument();
    expect(
      within(secondaryRow as HTMLElement).getByRole("button", { name: "Remove" }),
    ).toBeInTheDocument();
    const transferButton = within(secondaryRow as HTMLElement).getByRole(
      "button",
      { name: "Make primary owner" },
    );

    fireEvent.click(transferButton);

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "Transferred primary ownership to @second-admin",
      ),
    );
    expect(transferBody).toEqual({ new_owner_user_id: "@second-admin" });
  });

  it("lists accounts with their status and last login", async () => {
    stubStores({
      accounts: [
        {
          user_id: "@alice",
          disabled: false,
          created_at: "2026-01-01T00:00:00",
          password_updated_at: "2026-02-01T00:00:00",
          last_login_at: "2026-07-01T09:00:00",
        },
        {
          user_id: "@bob",
          disabled: true,
          created_at: "2026-01-02T00:00:00",
          password_updated_at: null,
          last_login_at: null,
        },
      ],
    });
    renderPage();

    expect(await screen.findByText("@alice")).toBeInTheDocument();
    expect(screen.getByText("@bob")).toBeInTheDocument();
    expect(screen.getByText("Disabled")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
  });

  it("never renders a secret value, because the API never returns one", async () => {
    stubStores({
      secrets: [
        {
          name: "stripe-key",
          current_version: 3,
          versions: 3,
          revoked: false,
          updated_at: "2026-07-01T09:00:00",
          updated_by: "@alice",
        },
      ],
    });
    renderPage();

    const name = await screen.findByText("stripe-key");
    // Metadata is shown. Scoped to the row rather than the document, because the
    // version and the version *count* both render "3" and a bare getByText matches
    // whichever it finds — a passing assertion that proves nothing specific.
    const row = name.closest("tr");
    expect(row).not.toBeNull();
    expect(row).toHaveTextContent("Active");
    // ...and the value input is write-only, never prefilled from the inventory.
    const value = screen.getByLabelText("Value") as HTMLInputElement;
    expect(value.value).toBe("");
    expect(value.type).toBe("password");
  });

  it("clears the password field after creating an account", async () => {
    stubStores();
    server.use(
      http.post(`${API_BASE}/auth/accounts`, () =>
        HttpResponse.json(envelope({ user_id: "@carol" })),
      ),
    );
    renderPage();

    const user = await screen.findByLabelText("User ID");
    const password = screen.getByLabelText("Password") as HTMLInputElement;
    fireEvent.change(user, { target: { value: "@carol" } });
    fireEvent.change(password, { target: { value: "correct-horse-battery" } });
    fireEvent.click(screen.getByRole("button", { name: /Create account/ }));

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("Created @carol"),
    );
    // The credential must not linger in a DOM node after the request succeeds.
    expect(password.value).toBe("");
    expect((user as HTMLInputElement).value).toBe("");
  });

  it("rejects a short password before making a request", async () => {
    stubStores();
    renderPage();

    fireEvent.change(await screen.findByLabelText("User ID"), {
      target: { value: "@dave" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "short" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Create account/ }));

    // No POST handler is registered, so reaching the network would fail the test via
    // `onUnhandledRequest: "error"` — the local guard has to catch this first.
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "at least 12 characters",
    );
  });

  it("resets a password through the write-only account control and clears it", async () => {
    const account = {
      user_id: "admin",
      disabled: false,
      created_at: "2026-01-01T00:00:00",
      password_updated_at: "2026-01-01T00:00:00",
      last_login_at: null,
    };
    stubStores({ accounts: [account] });
    let requestBody: unknown;
    let accountReads = 0;
    server.use(
      http.get(`${API_BASE}/auth/accounts`, () => {
        accountReads += 1;
        return HttpResponse.json(envelope({ accounts: [account], total: 1 }));
      }),
      http.patch(`${API_BASE}/auth/accounts/admin`, async ({ request }) => {
        requestBody = await request.json();
        return HttpResponse.json(
          envelope({ user_id: "admin", changed: ["password"] }),
        );
      }),
    );
    renderPage();

    const password = (await screen.findByLabelText(
      "New password for admin",
    )) as HTMLInputElement;
    const actionCell = password.closest("td");
    expect(actionCell).not.toHaveClass("flex");
    expect(actionCell?.firstElementChild).toHaveClass("flex");
    fireEvent.change(password, { target: { value: "correct-horse-battery" } });
    fireEvent.click(
      screen.getByRole("button", { name: "Reset password for admin" }),
    );

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "Changed the password for admin and revoked all of that account's sessions",
      ),
    );
    expect(requestBody).toEqual({ password: "correct-horse-battery" });
    expect(password.value).toBe("");
    expect(accountReads).toBeGreaterThanOrEqual(2);
  });

  it("clears the current browser session after resetting its own password", async () => {
    stubStores({
      accounts: [
        {
          user_id: "admin",
          disabled: false,
          created_at: "2026-01-01T00:00:00",
          password_updated_at: "2026-01-01T00:00:00",
          last_login_at: null,
        },
      ],
    });
    server.use(
      http.patch(`${API_BASE}/auth/accounts/admin`, () =>
        HttpResponse.json(
          envelope({ user_id: "admin", changed: ["password"] }),
        ),
      ),
    );
    saveLocalAuthSession(createLocalAuthSession("admin"));
    renderPage();

    fireEvent.change(await screen.findByLabelText("New password for admin"), {
      target: { value: "correct-horse-battery" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Reset password for admin" }),
    );

    await waitFor(() => expect(getStoredAuthSession()).toBeNull());
    expect(screen.getByRole("status")).toHaveTextContent(
      "Sign in again with the new password",
    );
  });

  it("does not clear admin when resetting the distinct @admin account", async () => {
    stubStores({
      accounts: [
        {
          user_id: "@admin",
          disabled: false,
          created_at: "2026-01-01T00:00:00",
          password_updated_at: "2026-01-01T00:00:00",
          last_login_at: null,
        },
      ],
    });
    server.use(
      http.patch(
        `${API_BASE}/auth/accounts/${encodeURIComponent("@admin")}`,
        () =>
          HttpResponse.json(
            envelope({ user_id: "@admin", changed: ["password"] }),
          ),
      ),
    );
    saveLocalAuthSession(createLocalAuthSession("admin"));
    renderPage();

    fireEvent.change(await screen.findByLabelText("New password for @admin"), {
      target: { value: "correct-horse-battery" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Reset password for @admin" }),
    );

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "Changed the password for @admin and revoked all of that account's sessions",
      ),
    );
    expect(getStoredAuthSession()?.username).toBe("admin");
  });

  it("says the store is disabled rather than showing an empty inventory", async () => {
    stubStores({ secretsEnabled: false });
    renderPage();

    expect(await screen.findByRole("status")).toHaveTextContent(
      /encrypted store is disabled/i,
    );
  });

  it("shows empty states for both accounts and secrets when the deployment has neither", async () => {
    stubStores({ accounts: [], secrets: [] });
    renderPage();

    expect(await screen.findByText("No accounts yet.")).toBeInTheDocument();
    expect(screen.getByText("No secrets stored yet.")).toBeInTheDocument();
  });

  it("shows an account-mutation error when creating an account is rejected", async () => {
    stubStores();
    server.use(
      http.post(`${API_BASE}/auth/accounts`, () =>
        HttpResponse.json({ detail: "user_id already exists" }, { status: 409 }),
      ),
    );
    renderPage();

    fireEvent.change(await screen.findByLabelText("User ID"), {
      target: { value: "@dup" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "correct-horse-battery" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Create account/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent("user_id already exists");
    // The credential-clearing behavior is only for success — a failed create keeps
    // what the operator typed so they can fix it and resubmit.
    expect((screen.getByLabelText("User ID") as HTMLInputElement).value).toBe("@dup");
  });

  it("rejects a short reset password before making a request", async () => {
    const account = {
      user_id: "@alice",
      disabled: false,
      created_at: "2026-01-01T00:00:00",
      password_updated_at: null,
      last_login_at: null,
    };
    stubStores({ accounts: [account] });
    renderPage();

    fireEvent.change(await screen.findByLabelText("New password for @alice"), {
      target: { value: "short" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Reset password for @alice" }));

    // No PATCH handler is registered, so reaching the network would fail the test.
    expect(await screen.findByRole("alert")).toHaveTextContent("at least 12 characters");
  });

  it("shows an error when resetting a password is rejected by the API", async () => {
    const account = {
      user_id: "@alice",
      disabled: false,
      created_at: "2026-01-01T00:00:00",
      password_updated_at: null,
      last_login_at: null,
    };
    stubStores({ accounts: [account] });
    server.use(
      http.patch(`${API_BASE}/auth/accounts/${encodeURIComponent("@alice")}`, () =>
        HttpResponse.json({ detail: "password reuse is not allowed" }, { status: 422 }),
      ),
    );
    renderPage();

    fireEvent.change(await screen.findByLabelText("New password for @alice"), {
      target: { value: "correct-horse-battery" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Reset password for @alice" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("password reuse is not allowed");
  });

  it("enables and disables an account, and surfaces an error when the toggle is rejected", async () => {
    const account = {
      user_id: "@alice",
      disabled: false,
      created_at: "2026-01-01T00:00:00",
      password_updated_at: null,
      last_login_at: null,
    };
    stubStores({ accounts: [account] });
    let lastBody: unknown;
    server.use(
      http.patch(`${API_BASE}/auth/accounts/${encodeURIComponent("@alice")}`, async ({ request }) => {
        lastBody = await request.json();
        return HttpResponse.json(envelope({ user_id: "@alice", changed: ["disabled"] }));
      }),
    );
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Disable" }));
    await waitFor(() => expect(lastBody).toEqual({ disabled: true }));

    // The toggle failing surfaces the account error banner.
    server.use(
      http.patch(`${API_BASE}/auth/accounts/${encodeURIComponent("@alice")}`, () =>
        HttpResponse.json({ detail: "cannot disable the last admin" }, { status: 409 }),
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Disable" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "cannot disable the last admin",
    );
  });

  it("revokes an account's sessions and surfaces an error when revocation is rejected", async () => {
    const account = {
      user_id: "@alice",
      disabled: false,
      created_at: "2026-01-01T00:00:00",
      password_updated_at: null,
      last_login_at: null,
    };
    stubStores({ accounts: [account] });
    server.use(
      http.delete(`${API_BASE}/auth/accounts/${encodeURIComponent("@alice")}/sessions`, () =>
        HttpResponse.json(envelope({ revoked: 3 })),
      ),
    );
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Revoke sessions" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Revoked 3 session(s) for @alice.",
    );

    server.use(
      http.delete(`${API_BASE}/auth/accounts/${encodeURIComponent("@alice")}/sessions`, () =>
        HttpResponse.json({ detail: "account not found" }, { status: 404 }),
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Revoke sessions" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("account not found");
  });

  it("stores a secret through the write-only form and surfaces the reference scheme", async () => {
    stubStores();
    server.use(
      http.post(`${API_BASE}/secrets`, () =>
        HttpResponse.json(envelope({ name: "stripe-key", version: 1 })),
      ),
    );
    renderPage();

    fireEvent.change(await screen.findByLabelText("Name"), {
      target: { value: "stripe-key" },
    });
    fireEvent.change(screen.getByLabelText("Value"), {
      target: { value: "sk_live_abc" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Store / rotate" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Stored stripe-key as version 1. Reference it as secret://stripe-key.",
    );
    // The value never lingers in the form after a successful store.
    expect((screen.getByLabelText("Value") as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("");
  });

  it("shows an error when storing a secret is rejected", async () => {
    stubStores();
    server.use(
      http.post(`${API_BASE}/secrets`, () =>
        HttpResponse.json({ detail: "encryption key source is not configured" }, { status: 503 }),
      ),
    );
    renderPage();

    fireEvent.change(await screen.findByLabelText("Name"), {
      target: { value: "stripe-key" },
    });
    fireEvent.change(screen.getByLabelText("Value"), {
      target: { value: "sk_live_abc" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Store / rotate" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "encryption key source is not configured",
    );
  });

  it("revokes a secret and surfaces an error when revocation is rejected", async () => {
    // A mutable row, not a fresh literal per `stubStores` call: `revokeSecret`
    // refetches `GET /secrets` after the POST succeeds, so the mock must
    // reflect the mutation for that refetch to show "Revoked" — a static
    // per-call array would refetch the exact same unrevoked row forever.
    const stripeKeyRow = {
      name: "stripe-key",
      current_version: 2,
      versions: 2,
      revoked: false,
      updated_at: "2026-07-01T09:00:00",
    };
    stubStores({ secrets: [stripeKeyRow] });
    server.use(
      http.post(`${API_BASE}/secrets/stripe-key/revoke`, () => {
        stripeKeyRow.revoked = true;
        return HttpResponse.json(envelope({ name: "stripe-key", revoked: true }));
      }),
    );
    renderPage();

    const row = (await screen.findByText("stripe-key")).closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(within(row).getByText("Revoked")).toBeInTheDocument());
    // A revoked secret has no further Revoke action.
    expect(within(row).queryByRole("button", { name: "Revoke" })).not.toBeInTheDocument();

    stubStores({
      secrets: [
        {
          name: "other-key",
          current_version: 1,
          versions: 1,
          revoked: false,
          updated_at: "2026-07-01T09:00:00",
        },
      ],
    });
    server.use(
      http.post(`${API_BASE}/secrets/other-key/revoke`, () =>
        HttpResponse.json({ detail: "secret already revoked" }, { status: 409 }),
      ),
    );
    renderPage();
    const otherRow = (await screen.findAllByText("other-key"))[0]!.closest(
      "tr",
    ) as HTMLElement;
    fireEvent.click(within(otherRow).getByRole("button", { name: "Revoke" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("secret already revoked");
  });

  it("shows a load error for secrets", async () => {
    server.use(
      http.get(`${API_BASE}/auth/accounts`, () =>
        HttpResponse.json(envelope({ accounts: [], total: 0 })),
      ),
      http.get(`${API_BASE}/secrets`, () =>
        HttpResponse.json({ detail: "secret store unreachable" }, { status: 500 }),
      ),
      http.get(`${API_BASE}/projects`, () => HttpResponse.json(envelope([]))),
    );
    renderPage();

    expect(await screen.findByText("secret store unreachable")).toBeInTheDocument();
  });

  it("says there are no active projects rather than showing an empty member table", async () => {
    stubStores({ projects: [] });
    renderPage();

    expect(
      await screen.findByText("No active projects are available."),
    ).toBeInTheDocument();
  });

  it("hides member management for a viewer without project.manage_members", async () => {
    stubStores({
      projects: [
        {
          project_id: "PRJ-1",
          name: "Support",
          description: "",
          owner: "@admin",
          status: "active",
          permissions: ["read"],
          access_role: "viewer",
        },
      ],
    });
    server.use(
      http.get(`${API_BASE}/projects/PRJ-1/members`, () =>
        HttpResponse.json(
          envelope({
            members: [
              {
                member_id: "PRJM-1",
                project_id: "PRJ-1",
                user_id: "@admin",
                role: "owner",
                status: "active",
                created_by: "@admin",
                created_at: null,
                updated_at: null,
              },
            ],
          }),
        ),
      ),
    );
    renderPage();

    expect(
      await screen.findByText("Only a project Admin can manage members."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add member" })).not.toBeInTheDocument();
    // The member row itself loads via a separate query from the "Only a
    // project Admin..." text above (which only depends on the already-loaded
    // project permissions) -- wait for it explicitly rather than assuming
    // it resolved by now.
    expect(await screen.findByText("owner")).toBeInTheDocument();
    // The role column renders as plain text, not an editable select, for a
    // non-managing viewer.
    expect(screen.queryByRole("combobox", { name: /Role for/ })).not.toBeInTheDocument();
  });

  it("surfaces a load error for project members", async () => {
    stubStores({
      projects: [
        {
          project_id: "PRJ-1",
          name: "Support",
          description: "",
          owner: "@admin",
          status: "active",
          permissions: ["read", "project.manage_members"],
          access_role: "owner",
        },
      ],
    });
    server.use(
      http.get(`${API_BASE}/projects/PRJ-1/members`, () =>
        HttpResponse.json({ detail: "member store unreachable" }, { status: 500 }),
      ),
    );
    renderPage();

    expect(await screen.findByText("member store unreachable")).toBeInTheDocument();
  });

  it("adds, edits, and removes a project member, surfacing errors from each action", async () => {
    const project = {
      project_id: "PRJ-1",
      name: "Support",
      description: "",
      owner: "@admin",
      status: "active",
      permissions: ["read", "project.manage_members"],
      access_role: "owner",
    };
    stubStores({ projects: [project] });
    let membersState = [
      {
        member_id: "PRJM-1",
        project_id: "PRJ-1",
        user_id: "@admin",
        role: "owner",
        status: "active",
        created_by: "@admin",
        created_at: null,
        updated_at: null,
      },
    ];
    server.use(
      http.get(`${API_BASE}/projects/PRJ-1/members`, () =>
        HttpResponse.json(envelope({ members: membersState })),
      ),
      http.post(`${API_BASE}/projects/PRJ-1/members`, async ({ request }) => {
        const body = (await request.json()) as { user_id: string; role: string };
        const created = {
          member_id: "PRJM-2",
          project_id: "PRJ-1",
          user_id: body.user_id,
          role: body.role,
          status: "active",
          created_by: "@admin",
          created_at: null,
          updated_at: null,
        };
        membersState = [...membersState, created];
        return HttpResponse.json(envelope(created));
      }),
      http.patch(`${API_BASE}/projects/PRJ-1/members/${encodeURIComponent("@dev")}`, async ({ request }) => {
        const body = (await request.json()) as { role: string };
        membersState = membersState.map((m) =>
          m.user_id === "@dev" ? { ...m, role: body.role } : m,
        );
        return HttpResponse.json(envelope(membersState.find((m) => m.user_id === "@dev")));
      }),
      http.delete(`${API_BASE}/projects/PRJ-1/members/${encodeURIComponent("@dev")}`, () => {
        membersState = membersState.filter((m) => m.user_id !== "@dev");
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderPage();

    await screen.findByText("@admin");
    const accessSection = screen.getByRole("region", { name: "Project access" });
    fireEvent.change(within(accessSection).getByLabelText("User ID"), {
      target: { value: "@dev" },
    });
    fireEvent.click(within(accessSection).getByRole("button", { name: "Add member" }));

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("Added @dev to Support."),
    );
    const devRow = (await screen.findByText("@dev")).closest("tr") as HTMLElement;
    fireEvent.change(within(devRow).getByRole("combobox"), {
      target: { value: "editor" },
    });
    await waitFor(() =>
      expect(within(devRow).getByRole("combobox")).toHaveValue("editor"),
    );

    fireEvent.click(within(devRow).getByRole("button", { name: "Remove" }));
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("Removed @dev from the project."),
    );
    // The status message and the member-list refetch are two independent
    // async updates -- wait for the row to actually disappear rather than
    // assuming it already has by the time the status text appears.
    await waitFor(() => expect(screen.queryByText("@dev")).not.toBeInTheDocument());
  });

  it("surfaces errors from add-member, change-role, and remove-member", async () => {
    const project = {
      project_id: "PRJ-1",
      name: "Support",
      description: "",
      owner: "@admin",
      status: "active",
      permissions: ["read", "project.manage_members"],
      access_role: "owner",
    };
    stubStores({ projects: [project] });
    server.use(
      http.get(`${API_BASE}/projects/PRJ-1/members`, () =>
        HttpResponse.json(
          envelope({
            members: [
              {
                member_id: "PRJM-1",
                project_id: "PRJ-1",
                user_id: "@admin",
                role: "owner",
                status: "active",
                created_by: "@admin",
                created_at: null,
                updated_at: null,
              },
              {
                member_id: "PRJM-2",
                project_id: "PRJ-1",
                user_id: "@dev",
                role: "viewer",
                status: "active",
                created_by: "@admin",
                created_at: null,
                updated_at: null,
              },
            ],
          }),
        ),
      ),
      http.post(`${API_BASE}/projects/PRJ-1/members`, () =>
        HttpResponse.json({ detail: "user is already a member" }, { status: 409 }),
      ),
      http.patch(`${API_BASE}/projects/PRJ-1/members/${encodeURIComponent("@dev")}`, () =>
        HttpResponse.json({ detail: "cannot promote to owner here" }, { status: 422 }),
      ),
      http.delete(`${API_BASE}/projects/PRJ-1/members/${encodeURIComponent("@dev")}`, () =>
        HttpResponse.json({ detail: "cannot remove the last editor" }, { status: 409 }),
      ),
    );
    renderPage();

    await screen.findByText("@dev");
    const accessSection = screen.getByRole("region", { name: "Project access" });
    fireEvent.change(within(accessSection).getByLabelText("User ID"), {
      target: { value: "@dev" },
    });
    fireEvent.click(within(accessSection).getByRole("button", { name: "Add member" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("user is already a member");

    const devRow = screen.getByText("@dev").closest("tr") as HTMLElement;
    fireEvent.change(within(devRow).getByRole("combobox"), {
      target: { value: "owner" },
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("cannot promote to owner here");

    fireEvent.click(within(devRow).getByRole("button", { name: "Remove" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("cannot remove the last editor");
  });

  it("surfaces an error when transferring ownership is rejected", async () => {
    saveLocalAuthSession(createLocalAuthSession("admin"));
    stubStores({
      projects: [
        {
          project_id: "PRJ-1",
          name: "Support",
          description: "",
          owner: "@admin",
          status: "active",
          permissions: ["read", "project.manage_members"],
          access_role: "owner",
        },
      ],
    });
    server.use(
      http.get(`${API_BASE}/projects/PRJ-1/members`, () =>
        HttpResponse.json(
          envelope({
            members: [
              {
                member_id: "PRJM-1",
                project_id: "PRJ-1",
                user_id: "@admin",
                role: "owner",
                status: "active",
                created_by: "@admin",
                created_at: null,
                updated_at: null,
              },
              {
                member_id: "PRJM-2",
                project_id: "PRJ-1",
                user_id: "@second-admin",
                role: "owner",
                status: "active",
                created_by: "@admin",
                created_at: null,
                updated_at: null,
              },
            ],
          }),
        ),
      ),
      http.post(`${API_BASE}/projects/PRJ-1/transfer-ownership`, () =>
        HttpResponse.json({ detail: "target is not an active owner" }, { status: 422 }),
      ),
    );
    renderPage();

    const secondaryRow = (await screen.findByText("@second-admin")).closest(
      "tr",
    ) as HTMLElement;
    fireEvent.click(
      within(secondaryRow).getByRole("button", { name: "Make primary owner" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "target is not an active owner",
    );
  });

  it("surfaces a forbidden account list instead of rendering it as empty", async () => {
    server.use(
      http.get(`${API_BASE}/auth/accounts`, () =>
        HttpResponse.json({ detail: "admin scope required" }, { status: 403 }),
      ),
      http.get(`${API_BASE}/secrets`, () =>
        HttpResponse.json(
          envelope({
            secrets: [],
            total: 0,
            enabled: true,
            reference_scheme: "secret://",
          }),
        ),
      ),
      http.get(`${API_BASE}/projects/:projectId/members`, () =>
        HttpResponse.json(envelope({ members: [] })),
      ),
    );
    renderPage();

    // "No accounts" and "you may not list accounts" are different facts.
    const alerts = await screen.findAllByRole("alert");
    expect(alerts.length).toBeGreaterThan(0);
    expect(screen.queryByText("No accounts yet.")).not.toBeInTheDocument();
  });
});
