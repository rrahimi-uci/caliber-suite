import { describe, expect, it } from "vitest";

import { highlightTokens, type Language } from "@/lib/syntaxHighlight";

function rejoin(code: string, language: Language): string {
  return highlightTokens(code, language)
    .map((t) => t.value)
    .join("");
}
function values(code: string, language: Language, type: string): string[] {
  return highlightTokens(code, language)
    .filter((t) => t.type === type)
    .map((t) => t.value);
}

describe("highlightTokens — lossless", () => {
  it("rejoins to the exact JSON input", () => {
    const code = '{\n  "name": "wf",\n  "n": 12.5,\n  "ok": true,\n  "x": null\n}\n';
    expect(rejoin(code, "json")).toBe(code);
  });

  it("rejoins to the exact Python input", () => {
    const code = '# header\n@deco\ndef run(x: str) -> str:\n    return f"{x}" + str(3)\n';
    expect(rejoin(code, "python")).toBe(code);
  });

  it("handles empty input", () => {
    expect(highlightTokens("", "json")).toEqual([]);
    expect(highlightTokens("", "python")).toEqual([]);
  });
});

describe("highlightTokens — JSON classification", () => {
  it("separates keys, strings, numbers, and booleans", () => {
    const code = '{"a": "b", "n": 1, "ok": true, "z": null}';
    expect(values(code, "json", "key")).toEqual(['"a"', '"n"', '"ok"', '"z"']);
    expect(values(code, "json", "string")).toContain('"b"');
    expect(values(code, "json", "number")).toContain("1");
    expect(values(code, "json", "boolean")).toEqual(expect.arrayContaining(["true", "null"]));
  });

  it("does not treat a value string as a key", () => {
    // "b" is a value (no following colon) → string, not key.
    expect(values('{"a": "b"}', "json", "key")).toEqual(['"a"']);
  });
});

describe("highlightTokens — Python classification", () => {
  it("classifies keywords, comments, strings, and decorators", () => {
    const code = '@deco\ndef f():\n    # note\n    return "x"';
    expect(values(code, "python", "decorator")).toContain("@deco");
    expect(values(code, "python", "keyword")).toEqual(expect.arrayContaining(["def", "return"]));
    expect(values(code, "python", "comment")).toContain("# note");
    expect(values(code, "python", "string")).toContain('"x"');
  });

  it("does not color keywords that appear inside a string", () => {
    const toks = highlightTokens('"def return class"', "python");
    expect(toks.every((t) => t.type !== "keyword")).toBe(true);
    expect(values('"def return class"', "python", "string")).toEqual(['"def return class"']);
  });

  it("does not color digits embedded in an identifier", () => {
    expect(values("abc123 = 5", "python", "number")).toEqual(["5"]);
  });
});
