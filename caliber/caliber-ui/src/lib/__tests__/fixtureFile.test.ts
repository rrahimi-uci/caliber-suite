import { describe, expect, it } from "vitest";

import { parseFixtureFile } from "@/lib/fixtureFile";

const jsonl = (rows: unknown[]): string =>
  rows.map((row) => JSON.stringify(row)).join("\n");

describe("parseFixtureFile", () => {
  it("reads the lab's short form: id + name, inputs, expectations become an equals case", () => {
    const result = parseFixtureFile(
      jsonl([
        {
          id: "R01",
          name: "small delivered order",
          tags: ["golden"],
          inputs: { amount: 49.0 },
          expectations: { decision: "approve", requires_approval: false },
        },
      ]),
    );
    expect(result).toEqual({
      cases: [
        {
          name: "R01 small delivered order",
          input: { amount: 49 },
          assertion: {
            type: "equals",
            value: '{"decision":"approve","requires_approval":false}',
          },
        },
      ],
    });
  });

  it("reads the stored shape, a JSON array, and a value that is an object", () => {
    const result = parseFixtureFile(
      JSON.stringify([
        {
          name: "a",
          input: { x: 1 },
          assertion: { type: "output_contains", value: "ok" },
        },
        {
          name: "b",
          input: {},
          assertion: { type: "equals", value: { k: 1 } },
        },
        {
          name: "c",
          input: {},
          assertion: { type: "no_error", value: "ignored" },
        },
        { name: "d", input: {}, assertion: {} },
      ]),
    );
    expect(result).toEqual({
      cases: [
        {
          name: "a",
          input: { x: 1 },
          assertion: { type: "output_contains", value: "ok" },
        },
        {
          name: "b",
          input: {},
          assertion: { type: "equals", value: '{"k":1}' },
        },
        { name: "c", input: {}, assertion: { type: "no_error", value: null } },
        { name: "d", input: {}, assertion: { type: "no_error", value: null } },
      ],
    });
  });

  it("a row with no assertion and no expected output only has to run without error", () => {
    expect(
      parseFixtureFile(jsonl([{ name: "a" }, { id: 7, inputs: { y: 2 } }])),
    ).toEqual({
      cases: [
        { name: "a", input: {}, assertion: { type: "no_error", value: null } },
        {
          name: "7",
          input: { y: 2 },
          assertion: { type: "no_error", value: null },
        },
      ],
    });
  });

  it("skips blank lines and CRLF line endings", () => {
    const text = `${JSON.stringify({ name: "a" })}\r\n\r\n${JSON.stringify({ name: "b" })}\r\n`;
    const result = parseFixtureFile(text);
    expect("cases" in result && result.cases.map((c) => c.name)).toEqual([
      "a",
      "b",
    ]);
  });

  it.each([
    ["", "The file is empty."],
    ["[1, 2", "The file starts with [ but is not a valid JSON array."],
    ["[]", "The file has no rows."],
    ['{"name": "a"}\nnot json', "Line 2 is not valid JSON."],
    ["[1]", "Row 1 must be a JSON object."],
    ['{"input": {}}', 'Line 1 needs a "name" (or an "id").'],
    [
      '{"name":"a"}\n{"name":"a"}',
      'Line 2: the case name "a" appears twice; names must be unique.',
    ],
    [
      '{"name":"a","input":[1]}',
      'Line 1 ("a"): "input" must be a JSON object.',
    ],
    [
      '{"name":"a","assertion":"equals"}',
      'Line 1 ("a"): "assertion" must be an object with a "type".',
    ],
    [
      '{"name":"a","assertion":{"type":"regex","value":"x"}}',
      'Line 1 ("a"): assertion type "regex" is not one of no_error, output_contains, equals.',
    ],
    [
      '{"name":"a","assertion":{"type":"equals"}}',
      'Line 1 ("a"): assertion "equals" needs a "value".',
    ],
  ])("reports the first problem: %j", (text, error) => {
    expect(parseFixtureFile(text)).toEqual({ error });
  });
});
