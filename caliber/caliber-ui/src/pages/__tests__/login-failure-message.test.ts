import { describe, expect, it } from "vitest";

import { ApiError } from "@/api/caliberApi";
import { loginFailureMessage } from "@/pages/Login";

describe("loginFailureMessage", () => {
  it("keeps a generic message for a rejected credential so accounts cannot be enumerated", () => {
    expect(
      loginFailureMessage(new ApiError(401, "invalid credentials", null)),
    ).toBe("Invalid username or password.");
    expect(loginFailureMessage(new ApiError(400, "bad request", null))).toBe(
      "Invalid username or password.",
    );
  });

  it("reports throttling by status or by the server's wording", () => {
    expect(loginFailureMessage(new ApiError(429, "slow down", null))).toMatch(
      /Too many failed sign-in/,
    );
    expect(loginFailureMessage(new Error("too many failed attempts"))).toMatch(
      /Too many failed sign-in/,
    );
  });

  it("reports an unreachable server for status 0", () => {
    expect(
      loginFailureMessage(new ApiError(0, "network error: fetch failed", null)),
    ).toMatch(/Can't reach CALIBER/);
  });

  it("reports every 5xx with its status", () => {
    for (const status of [500, 502, 503, 504]) {
      expect(loginFailureMessage(new ApiError(status, "boom", null))).toContain(
        `server error ${status}`,
      );
    }
  });

  it("falls back to the generic message for an unknown failure", () => {
    expect(loginFailureMessage(new Error("something else"))).toBe(
      "Invalid username or password.",
    );
    expect(loginFailureMessage("not an error")).toBe(
      "Invalid username or password.",
    );
  });
});
