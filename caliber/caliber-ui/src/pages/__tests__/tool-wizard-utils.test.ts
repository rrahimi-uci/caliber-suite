import { describe, expect, it } from "vitest";

import {
  buildInputSchema,
  buildOutputSchema,
  propertiesToSchema,
  schemaToProperties,
  toSnake,
} from "@/pages/ToolWizard";

describe("ToolWizard helpers", () => {
  it("normalizes names to snake_case", () => {
    expect(toSnake("LookupOrder")).toBe("lookup_order");
    expect(toSnake("lookup order!!")).toBe("lookup_order");
    expect(toSnake("__Already__snake__")).toBe("already_snake");
  });

  it("converts properties to JSON schema with required fields", () => {
    const schema = propertiesToSchema([
      { id: "a", name: "query", type: "string", description: "Search text", required: true },
      { id: "b", name: "limit", type: "integer", description: "", required: false },
    ]);
    expect(schema).toEqual({
      type: "object",
      properties: {
        query: { type: "string", description: "Search text" },
        limit: { type: "integer", description: undefined },
      },
      required: ["query"],
    });
    expect(propertiesToSchema([])).toBeNull();
  });

  it("converts schema to editable property rows", () => {
    const props = schemaToProperties({
      type: "object",
      required: ["query"],
      properties: {
        query: { type: "string", description: "Search text" },
        limit: { type: "integer" },
      },
    });
    expect(props).toHaveLength(2);
    expect(props[0]).toMatchObject({ name: "query", type: "string", required: true });
    expect(schemaToProperties(null)).toEqual([]);
    expect(schemaToProperties({})).toEqual([]);
  });

  it("builds input/output schema from raw json and falls back on parse errors", () => {
    const withRaw = {
      useRawInput: true,
      inputSchemaRaw: '{"type":"object","properties":{"q":{"type":"string"}}}',
      inputProperties: [],
      useRawOutput: true,
      outputSchemaRaw: '{"type":"object","properties":{"ok":{"type":"boolean"}}}',
      outputProperties: [],
    } as never;
    expect(buildInputSchema(withRaw)).toEqual({
      type: "object",
      properties: { q: { type: "string" } },
    });
    expect(buildOutputSchema(withRaw)).toEqual({
      type: "object",
      properties: { ok: { type: "boolean" } },
    });

    const invalidRaw = {
      ...withRaw,
      inputSchemaRaw: "{bad",
      outputSchemaRaw: "{}",
    } as never;
    expect(buildInputSchema(invalidRaw)).toBeNull();
    expect(buildOutputSchema(invalidRaw)).toBeNull();
  });

  it("builds schema from visual properties when raw mode is disabled", () => {
    const form = {
      useRawInput: false,
      inputSchemaRaw: "{}",
      inputProperties: [{ id: "1", name: "id", type: "string", description: "", required: true }],
      useRawOutput: false,
      outputSchemaRaw: "{}",
      outputProperties: [{ id: "2", name: "count", type: "integer", description: "", required: false }],
    } as never;
    expect(buildInputSchema(form)).toEqual({
      type: "object",
      properties: { id: { type: "string", description: undefined } },
      required: ["id"],
    });
    expect(buildOutputSchema(form)).toEqual({
      type: "object",
      properties: { count: { type: "integer", description: undefined } },
    });
  });
});

