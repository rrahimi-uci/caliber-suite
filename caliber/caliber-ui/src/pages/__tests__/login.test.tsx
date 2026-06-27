import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Login } from "@/pages/Login";

afterEach(() => {
  window.localStorage.clear();
  vi.restoreAllMocks();
});

describe("Login", () => {
  it("shows an error for invalid credentials and clears it when editing fields", async () => {
    const user = userEvent.setup();
    render(<Login onLogin={vi.fn()} />);

    await user.clear(screen.getByPlaceholderText("Enter your username"));
    await user.type(screen.getByPlaceholderText("Enter your username"), "bad-user");
    await user.clear(screen.getByPlaceholderText("Enter your password"));
    await user.type(screen.getByPlaceholderText("Enter your password"), "bad-pass");
    await user.click(screen.getByRole("button", { name: /^Sign in$/ }));
    expect(screen.getByRole("alert")).toHaveTextContent("Invalid username or password.");

    await user.type(screen.getByPlaceholderText("Enter your username"), "x");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("toggles password visibility and logs in with defaults", async () => {
    const user = userEvent.setup();
    const onLogin = vi.fn();
    render(<Login onLogin={onLogin} />);

    const password = screen.getByPlaceholderText("Enter your password");
    expect(password).toHaveAttribute("type", "password");
    await user.click(screen.getByRole("button", { name: "Show password" }));
    expect(password).toHaveAttribute("type", "text");
    await user.click(screen.getByRole("button", { name: "Hide password" }));
    expect(password).toHaveAttribute("type", "password");

    await user.click(screen.getByRole("button", { name: /^Sign in$/ }));
    expect(onLogin).toHaveBeenCalledTimes(1);
    expect(window.localStorage.getItem("caliber.auth.session")).toContain("@local-admin");
  });
});

