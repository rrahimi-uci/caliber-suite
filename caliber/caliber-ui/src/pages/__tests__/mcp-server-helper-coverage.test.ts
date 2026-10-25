import { describe, expect, it } from "vitest";

import {
  formatMcpOperatorError,
  normalizeMcpGeneratedTests,
  parseJsonArrayFromAssistant,
  parseJsonObjectFromAssistant,
  withDefaultPolicy,
} from "@/pages/McpServers";

describe("MCP server helper decisions", () => {
  it("unwraps and classifies operator-facing errors", () => {
    expect(formatMcpOperatorError(null)).toBeNull();
    expect(formatMcpOperatorError("   ")).toBeNull();
    expect(formatMcpOperatorError("MCP session failed: plain failure")).toBe("plain failure");
    expect(formatMcpOperatorError("MCP tools/call failed: MCP session failed: timeout")).toContain("timed out");
    expect(formatMcpOperatorError("unhandled errors in a TaskGroup (1 sub-exception)")).toContain("server is running");
    expect(formatMcpOperatorError("[Errno 2] No such file or directory")).toContain("installed");
    expect(formatMcpOperatorError("request timed out")).toContain("timed out");
    expect(formatMcpOperatorError("Invalid input: [{\"path\":[\"args\",\"name\"],\"message\":\"required\"},{\"path\":[\"limit\"],\"message\":\"must be positive\"},{\"path\":[],\"message\":\"bad value\"}]")).toBe(
      "Invalid input: args.name is required; limit: must be positive; bad value",
    );
    expect(formatMcpOperatorError("Invalid input: [not-json]")).toBe("Invalid input: [not-json]");
    expect(formatMcpOperatorError("Invalid input: []")).toBe("Invalid input: []");
    expect(formatMcpOperatorError("ordinary failure")).toBe("ordinary failure");
  });

  it("parses assistant JSON envelopes and rejects invalid shapes", () => {
    expect(parseJsonArrayFromAssistant("prefix [{\"input\":{}}] suffix")).toEqual([{ input: {} }]);
    expect(() => parseJsonArrayFromAssistant("no array")).toThrow("JSON array");
    expect(() => parseJsonArrayFromAssistant("{\"not\":\"array\"}")).toThrow("JSON array");
    expect(parseJsonObjectFromAssistant("prefix {\"ok\":true} suffix")).toEqual({ ok: true });
    expect(parseJsonObjectFromAssistant("no object")).toBeNull();
    expect(parseJsonObjectFromAssistant("[1,2]")).toBeNull();
    expect(() => parseJsonObjectFromAssistant("prefix {not-json} suffix")).toThrow();
  });

  it("normalizes generated cases and applies the default tool policy", () => {
    const normalized = normalizeMcpGeneratedTests([
      { input: { q: "hello" }, output: { ok: true }, expectedBehavior: "  succeeds  ", tags: [" smoke ", "", 3] },
      { input: { q: "fallback" }, expectedOutput: { result: 1 } },
    ]);
    expect(normalized).toHaveLength(2);
    expect(normalized[0]).toMatchObject({ expectedOutput: { ok: true }, expectedBehavior: "succeeds", tags: [" smoke "] });
    expect(normalized[1]).toMatchObject({ expectedOutput: { result: 1 }, tags: [] });
    expect(() => normalizeMcpGeneratedTests([null])).toThrow("must be an object");
    expect(() => normalizeMcpGeneratedTests([{ input: "bad" }])).toThrow("needs an object input");
    expect(withDefaultPolicy({ name: "lookup", description: "", input_schema: null } as never)).toMatchObject({
      name: "lookup",
      classified: false,
      policy: { allowed: false, side_effect_level: "read", requires_approval: false, rate_limit_per_minute: null },
    });
    expect(normalizeMcpGeneratedTests([{ input: {}, expectedOutput: null, tags: "bad" } as never])).toMatchObject([
      { expectedOutput: {}, expectedBehavior: "", tags: [] },
    ]);
  });
});
