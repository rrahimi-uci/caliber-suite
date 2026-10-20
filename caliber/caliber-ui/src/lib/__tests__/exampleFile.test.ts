import { describe, expect, it } from "vitest";

import { parseExampleFile } from "@/lib/exampleFile";

describe("parseExampleFile", () => {
  it("reads JSON Lines, keeps input/expected/tags/weight and ignores other keys", () => {
    const text = [
      '{"id": "TK01", "tags": ["how_to"], "input": {"ticket_text": "How do I enable SSO?"}, "expected": {"decision": "reply"}}',
      "",
      '{"input": {"q": "b"}, "weight": 2.5}',
    ].join("\n");
    expect(parseExampleFile(text)).toEqual({
      rows: [
        {
          input: { ticket_text: "How do I enable SSO?" },
          expected: { decision: "reply" },
          tags: ["how_to"],
          weight: 1,
        },
        { input: { q: "b" }, expected: {}, tags: [], weight: 2.5 },
      ],
    });
  });

  it("reads a JSON array, with CRLF-free and CRLF files alike", () => {
    expect(parseExampleFile('[{"input": {"q": "a"}}]')).toEqual({
      rows: [{ input: { q: "a" }, expected: {}, tags: [], weight: 1 }],
    });
    const crlf = '{"input": {"q": "a"}}\r\n{"input": {"q": "b"}}\r\n';
    const parsed = parseExampleFile(crlf);
    expect("rows" in parsed && parsed.rows).toHaveLength(2);
  });

  it.each([
    ["", "The file is empty."],
    ["[not json", "The file starts with [ but is not a valid JSON array."],
    ['{"input": {}}\nnot json', "Line 2 is not valid JSON."],
    ["[1]", "Row 1 must be a JSON object."],
    ['{"expected": {}}', 'Line 1 needs an "input" object.'],
    ['{"input": []}', 'Line 1 needs an "input" object.'],
    ['{"input": {}, "expected": "x"}', 'Line 1: "expected" must be an object.'],
    ['{"input": {}, "tags": [1]}', 'Line 1: "tags" must be a list of strings.'],
    ['{"input": {}, "weight": -1}', 'Line 1: "weight" must be a number >= 0.'],
    ["[]", "The file has no rows."],
  ])("rejects %j with a readable message", (text, error) => {
    expect(parseExampleFile(text)).toEqual({ error });
  });
});
