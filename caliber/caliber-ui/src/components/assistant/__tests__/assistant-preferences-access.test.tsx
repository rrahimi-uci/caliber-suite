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

  it.each([
    [{ user_id: "@viewer", scopes: [], is_admin: false }, "Viewer", "scopes: none", "bg-slate-100"],
    [
      { user_id: "@operator", scopes: ["operator"], is_admin: false },
      "Operator",
      "scopes: operator",
      "bg-caliber-50",
    ],
    [
      { user_id: "@admin", scopes: ["operator", "admin"], is_admin: true },
      "Admin",
      "scopes: operator, admin",
      "bg-purple-50",
    ],
    [
      { user_id: "@alt-admin", scopes: ["admin"], is_admin: false },
      "Admin",
      "scopes: admin",
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
});
