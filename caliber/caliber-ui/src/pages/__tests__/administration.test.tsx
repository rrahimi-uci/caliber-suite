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
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  } = {},
): void {
  const accounts = options.accounts ?? [];
  const secrets = options.secrets ?? [];
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
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  clearLocalAuthSession();
});
afterAll(() => server.close());

describe("Administration", () => {
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
    );
    renderPage();

    // "No accounts" and "you may not list accounts" are different facts.
    const alerts = await screen.findAllByRole("alert");
    expect(alerts.length).toBeGreaterThan(0);
    expect(screen.queryByText("No accounts yet.")).not.toBeInTheDocument();
  });
});
