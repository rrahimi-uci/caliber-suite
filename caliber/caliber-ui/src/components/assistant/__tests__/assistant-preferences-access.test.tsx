import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";

import { AccessBadge } from "@/components/assistant/AccessBadge";
import {
  readDefaultAssistantSkillMode,
  writeDefaultAssistantSkillMode,
} from "@/lib/assistantPreferences";
import { render, screen } from "@/test/utils";
import { server } from "@/test/server";


const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const DEFAULT_SKILL_MODE_KEY = "caliber.assistant.defaults.skillMode";


beforeAll(() => {
  server.listen({ onUnhandledRequest: "error" });
});

afterAll(() => {
  server.close();
});

afterEach(() => {
  server.resetHandlers();
});


describe("assistantPreferences", () => {
  it.each([
    [null, "auto"],
    ["manual", "manual"],
    ["off", "off"],
    ["auto", "auto"],
    ["invalid", "auto"],
  ])("reads the default skill mode from localStorage value %p", (stored, expected) => {
    if (stored === null) {
      window.localStorage.removeItem(DEFAULT_SKILL_MODE_KEY);
    } else {
      window.localStorage.setItem(DEFAULT_SKILL_MODE_KEY, stored);
    }
    expect(readDefaultAssistantSkillMode()).toBe(expected);
  });

  it.each(["auto", "manual", "off"])("writes default skill mode %s", (mode) => {
    writeDefaultAssistantSkillMode(mode as "auto" | "manual" | "off");
    expect(window.localStorage.getItem(DEFAULT_SKILL_MODE_KEY)).toBe(mode);
  });
});


describe("AccessBadge", () => {
  it("renders nothing before the current-user query resolves", () => {
    render(<AccessBadge />);
    expect(screen.queryByTestId("assistant-access-badge")).not.toBeInTheDocument();
  });

  /**
   * The payloads below are the ones ``GET /me`` actually returns.
   * ``scopes_for_user`` (caliber/src/caliber/auth.py) issues the prefixed
   * ``caliber.*`` names and expands the hierarchy before responding, so an
   * admin's payload carries all four scopes.
   *
   * The previous version of this suite asserted against bare names
   * (``["admin"]``, ``["operator"]``) that the server never issues. It passed
   * against fixtures that could not occur, which is why the badge shipped
   * labelling every admin "Viewer" with green tests.
   */
  it.each([
    [
      { user_id: "@anonymous", scopes: [], is_admin: false },
      "No access",
      "Scopes: none",
      "bg-slate-100",
    ],
    [
      { user_id: "@viewer", scopes: ["caliber.viewer"], is_admin: false },
      "Viewer",
      "Scopes: caliber.viewer",
      "bg-slate-100",
    ],
    [
      {
        user_id: "@operator",
        scopes: ["caliber.operator", "caliber.viewer"],
        is_admin: false,
      },
      "Operator",
      "Scopes: caliber.operator, caliber.viewer",
      "bg-caliber-50",
    ],
    [
      {
        user_id: "@approver",
        scopes: ["caliber.approver", "caliber.viewer"],
        is_admin: false,
      },
      "Approver",
      "Scopes: caliber.approver, caliber.viewer",
      "bg-amber-50",
    ],
    [
      {
        user_id: "@admin",
        scopes: [
          "caliber.admin",
          "caliber.approver",
          "caliber.operator",
          "caliber.viewer",
        ],
        is_admin: true,
      },
      "Admin",
      "Scopes: caliber.admin",
      "bg-purple-50",
    ],
  ])(
    "renders the right access level for %p",
    async (payload, expectedLabel, titleFragment, classFragment) => {
      server.use(
        http.get(`${API_BASE}/me`, () => HttpResponse.json({ data: payload })),
      );

      render(<AccessBadge />);

      const badge = await screen.findByTestId("assistant-access-badge");
      expect(badge).toHaveTextContent(expectedLabel);
      expect(badge).toHaveAttribute("title", expect.stringContaining(titleFragment));
      expect(badge.className).toContain(classFragment);
    },
  );

  it("labels a real admin payload Admin, not Viewer", async () => {
    // The reported regression, stated directly: every admin was labelled
    // "Viewer" because the badge compared against "admin" rather than
    // "caliber.admin".
    server.use(
      http.get(`${API_BASE}/me`, () =>
        HttpResponse.json({
          data: {
            user_id: "@admin",
            scopes: [
              "caliber.admin",
              "caliber.approver",
              "caliber.operator",
              "caliber.viewer",
            ],
            is_admin: true,
          },
        }),
      ),
    );

    render(<AccessBadge />);

    const badge = await screen.findByTestId("assistant-access-badge");
    expect(badge).toHaveTextContent("Admin");
    expect(badge).not.toHaveTextContent("Viewer");
  });

  it("does not accept the bare scope names the server never issues", async () => {
    // Guards the fixture mistake that hid the bug: if someone reintroduces
    // bare-name matching, this payload would read as "Admin".
    server.use(
      http.get(`${API_BASE}/me`, () =>
        HttpResponse.json({
          data: { user_id: "@spoof", scopes: ["admin", "operator"], is_admin: false },
        }),
      ),
    );

    render(<AccessBadge />);

    const badge = await screen.findByTestId("assistant-access-badge");
    expect(badge).toHaveTextContent("No access");
  });

  it("reports the highest scope held regardless of payload order", async () => {
    server.use(
      http.get(`${API_BASE}/me`, () =>
        HttpResponse.json({
          data: {
            user_id: "@admin",
            scopes: ["caliber.viewer", "caliber.operator", "caliber.admin"],
            is_admin: true,
          },
        }),
      ),
    );

    render(<AccessBadge />);

    expect(await screen.findByTestId("assistant-access-badge")).toHaveTextContent(
      "Admin",
    );
  });
});
