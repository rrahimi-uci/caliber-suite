import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

import { Login } from "@/pages/Login";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  window.localStorage.clear();
  vi.restoreAllMocks();
});
afterAll(() => server.close());

async function fillCredentials(
  user: ReturnType<typeof userEvent.setup>,
  username: string,
  password: string,
): Promise<void> {
  await user.type(screen.getByPlaceholderText("Enter your username"), username);
  await user.type(screen.getByPlaceholderText("Enter your password"), password);
}

describe("Login", () => {
  it("does not prefill a default credential", () => {
    // Regression (C1): the form arrived with admin/admin already typed, and the
    // browser validated it. Both are gone — credentials are verified server-side.
    render(<Login onLogin={vi.fn()} />);
    expect(screen.getByPlaceholderText("Enter your username")).toHaveValue("");
    expect(screen.getByPlaceholderText("Enter your password")).toHaveValue("");
    expect(screen.queryByText(/Demo credentials/i)).not.toBeInTheDocument();
    expect(
      screen.getByText(
        /Credentials are managed by your CALIBER administrator/i,
      ),
    ).toBeInTheDocument();
  });

  it("shows an authentication transition notice", () => {
    render(
      <Login
        notice="Your password changed. Sign in again."
        onLogin={vi.fn()}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "Your password changed. Sign in again.",
    );
  });

  it("signs in through the server and reports the session, not a synthesised identity", async () => {
    const user = userEvent.setup();
    const onLogin = vi.fn();
    let received: Record<string, unknown> | null = null;
    server.use(
      http.post(`${API_BASE}/auth/login`, async ({ request }) => {
        received = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          data: { user_id: "@owner", expires_at: "2026-01-01T00:00:00Z" },
        });
      }),
    );

    render(<Login onLogin={onLogin} />);
    await fillCredentials(user, "@owner", "correct-horse-battery");
    await user.click(screen.getByRole("button", { name: /^Sign in$/ }));

    await waitFor(() => expect(onLogin).toHaveBeenCalledTimes(1));
    // The password left the browser to be checked, rather than being compared here.
    expect(received).toEqual({
      user_id: "@owner",
      password: "correct-horse-battery",
    });
    // Only display state is persisted; the session itself is an HttpOnly cookie the
    // browser cannot read.
    expect(window.localStorage.getItem("caliber.auth.session")).toContain(
      "@owner",
    );
  });

  it("shows a generic error when the server rejects the credentials", async () => {
    const user = userEvent.setup();
    const onLogin = vi.fn();
    server.use(
      http.post(`${API_BASE}/auth/login`, () =>
        HttpResponse.json({ detail: "invalid credentials" }, { status: 401 }),
      ),
    );

    render(<Login onLogin={onLogin} />);
    await fillCredentials(user, "@ghost", "whatever-password");
    await user.click(screen.getByRole("button", { name: /^Sign in$/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Invalid username or password.",
    );
    expect(onLogin).not.toHaveBeenCalled();
    expect(window.localStorage.getItem("caliber.auth.session")).toBeNull();
  });

  it("distinguishes a throttled sign-in from a wrong password", async () => {
    // The server throttles repeated failures; telling the user that is different from
    // telling them the password is wrong, and only one of the two is actionable.
    const user = userEvent.setup();
    server.use(
      http.post(`${API_BASE}/auth/login`, () =>
        HttpResponse.json(
          { detail: "too many failed sign-in attempts" },
          { status: 429 },
        ),
      ),
    );

    render(<Login onLogin={vi.fn()} />);
    await fillCredentials(user, "@owner", "wrong-password-here");
    await user.click(screen.getByRole("button", { name: /^Sign in$/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Too many failed sign-in/,
    );
  });

  it("says the server is unreachable instead of blaming the password", async () => {
    // A network failure carries no verdict on the credentials. Telling a user with
    // a correct password that it is "invalid" sends them hunting for a typo.
    const user = userEvent.setup();
    server.use(http.post(`${API_BASE}/auth/login`, () => HttpResponse.error()));

    render(<Login onLogin={vi.fn()} />);
    await fillCredentials(user, "@owner", "correct-password-here");
    await user.click(screen.getByRole("button", { name: /^Sign in$/ }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Can't reach CALIBER/);
    expect(alert).not.toHaveTextContent(/Invalid username or password/);
  });

  it("reports a server error with its status, not a wrong password", async () => {
    const user = userEvent.setup();
    server.use(
      http.post(`${API_BASE}/auth/login`, () =>
        HttpResponse.json({ detail: "database unavailable" }, { status: 503 }),
      ),
    );

    render(<Login onLogin={vi.fn()} />);
    await fillCredentials(user, "@owner", "correct-password-here");
    await user.click(screen.getByRole("button", { name: /^Sign in$/ }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      /CALIBER is unavailable \(server error 503\)/,
    );
    expect(alert).not.toHaveTextContent(/Invalid username or password/);
  });

  it("clears the error when a field is edited", async () => {
    const user = userEvent.setup();
    server.use(
      http.post(`${API_BASE}/auth/login`, () =>
        HttpResponse.json({ detail: "invalid credentials" }, { status: 401 }),
      ),
    );

    render(<Login onLogin={vi.fn()} />);
    await fillCredentials(user, "bad-user", "bad-password-x");
    await user.click(screen.getByRole("button", { name: /^Sign in$/ }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();

    await user.type(screen.getByPlaceholderText("Enter your username"), "x");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("toggles password visibility", async () => {
    const user = userEvent.setup();
    render(<Login onLogin={vi.fn()} />);

    const password = screen.getByPlaceholderText("Enter your password");
    expect(password).toHaveAttribute("type", "password");
    await user.click(screen.getByRole("button", { name: "Show password" }));
    expect(password).toHaveAttribute("type", "text");
    await user.click(screen.getByRole("button", { name: "Hide password" }));
    expect(password).toHaveAttribute("type", "password");
  });
});
