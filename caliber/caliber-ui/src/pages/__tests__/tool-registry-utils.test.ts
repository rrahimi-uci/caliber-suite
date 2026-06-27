import { describe, expect, it } from "vitest";

import {
  clampToolTestCount,
  extractJsonArray,
  extractJsonObject,
  normalizeToolTestCases,
} from "@/pages/ToolRegistry";

describe("ToolRegistry helpers", () => {
  it("clamps requested test counts to valid bounds", () => {
    expect(clampToolTestCount(Number.NaN)).toBe(1);
    expect(clampToolTestCount(0)).toBe(1);
    expect(clampToolTestCount(3.4)).toBe(3);
    expect(clampToolTestCount(999)).toBe(50);
  });

  it("extracts json arrays and objects from assistant text", () => {
    expect(extractJsonArray("prefix [1,2,3] suffix")).toEqual([1, 2, 3]);
    expect(() => extractJsonArray("no array here")).toThrow("JSON array");
    expect(() => extractJsonArray("{\"ok\":true}")).toThrow("JSON array");

    expect(extractJsonObject("noise {\"verdict\":\"pass\"} tail")).toEqual({ verdict: "pass" });
    expect(extractJsonObject("[]")).toBeNull();
    expect(extractJsonObject("plain text")).toBeNull();
  });

  it("normalizes generated test cases and validates shape", () => {
    const normalized = normalizeToolTestCases([
      {
        input: { q: "status" },
        expectedOutput: { ok: true },
        expectedBehavior: "returns status",
        tags: ["happy"],
      },
      {
        input: { q: "ping" },
        output: { pong: true },
        tags: ["fallback"],
      },
    ]);
    expect(normalized).toHaveLength(2);
    expect(normalized[0]?.input).toEqual({ q: "status" });
    expect(normalized[1]?.expectedOutput).toEqual({ pong: true });

    expect(() => normalizeToolTestCases([{}])).toThrow("needs an object input");
    expect(() => normalizeToolTestCases([{ input: [] }])).toThrow("needs an object input");
  });
});

