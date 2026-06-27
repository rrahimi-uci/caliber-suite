import { describe, expect, it } from "vitest";

import {
  clampTestCaseCount,
  formatOverallScore,
  normalizeUploadedExample,
  parseUploadedDataset,
  readString,
  readStringArray,
  scorerCategoryLabel,
  statusTone,
} from "@/pages/Prompts";

describe("Prompts utility helpers", () => {
  it("clamps test case counts into the allowed range", () => {
    expect(clampTestCaseCount(Number.NaN)).toBe(1);
    expect(clampTestCaseCount(0)).toBe(1);
    expect(clampTestCaseCount(1.8)).toBe(2);
    expect(clampTestCaseCount(99)).toBe(50);
  });

  it("maps status tones for run table badges", () => {
    expect(statusTone("completed")).toBe("text-emerald-700");
    expect(statusTone("queued")).toBe("text-blue-700");
    expect(statusTone("blocked")).toBe("text-amber-700");
    expect(statusTone("failed")).toBe("text-red-700");
    expect(statusTone("unknown")).toBe("text-zinc-700");
  });

  it("reads scalar/string array helper values", () => {
    expect(readString("hello")).toBe("hello");
    expect(readString("   ")).toBeNull();
    expect(readString(7)).toBeNull();
    expect(readStringArray(["a", "", 7, "b"])).toEqual(["a", "b"]);
    expect(readStringArray("nope")).toEqual([]);
  });

  it("formats scorer categories and overall score safely", () => {
    expect(scorerCategoryLabel("deepeval_beta")).toBe("DeepEval (Beta)");
    expect(scorerCategoryLabel("core")).toBe("Core MLflow");
    expect(scorerCategoryLabel("custom")).toBe("custom");

    expect(formatOverallScore({ candidate: { overall: 0.9132 } })).toBe("91.3%");
    expect(formatOverallScore({ candidate: {} })).toBe("—");
    expect(formatOverallScore({})).toBe("—");
  });

  it("normalizes uploaded examples from mixed shapes", () => {
    expect(
      normalizeUploadedExample(
        {
          input: "refund request",
          expected: "route to refunds",
          tags: ["refund", 3],
          weight: 2,
        },
        0,
      ),
    ).toEqual({
      input: { user_message: "refund request" },
      expected: { behavior: "route to refunds" },
      tags: ["refund"],
      weight: 2,
    });

    expect(
      normalizeUploadedExample(
        {
          user_message: "hello",
          reference_answer: "assist politely",
        },
        1,
      ),
    ).toEqual({
      input: { user_message: "hello" },
      expected: { behavior: "assist politely" },
      tags: [],
      weight: undefined,
    });
  });

  it("fills safe defaults for uploaded examples with missing input or expected fields", () => {
    expect(normalizeUploadedExample({ tags: ["fallback"], weight: "heavy" }, 4)).toEqual({
      input: { user_message: "Example 5" },
      expected: { behavior: "" },
      tags: ["fallback"],
      weight: undefined,
    });

    expect(
      normalizeUploadedExample(
        {
          input: { ticket: "T-1" },
          expected: { route: "billing" },
          tags: "not-an-array",
        },
        0,
      ),
    ).toEqual({
      input: { ticket: "T-1" },
      expected: { route: "billing" },
      tags: [],
      weight: undefined,
    });
  });

  it("throws when normalizeUploadedExample receives invalid rows", () => {
    expect(() => normalizeUploadedExample(null, 0)).toThrow("Invalid example at row 1.");
  });

  it("parses uploaded JSON and JSONL datasets", () => {
    const fromJson = parseUploadedDataset(
      JSON.stringify({
        examples: [
          {
            input: { user_message: "Need refund" },
            expected: { route: "refund_agent" },
            tags: ["refund"],
          },
        ],
      }),
      "dataset.json",
    );
    expect(fromJson).toHaveLength(1);
    expect(fromJson[0]?.input).toEqual({ user_message: "Need refund" });

    const fromJsonl = parseUploadedDataset(
      '{"input":"Hi","expected":"Greet"}\n{"user_message":"Need help","reference_answer":"Assist"}\n',
      "dataset.jsonl",
    );
    expect(fromJsonl).toHaveLength(2);
    expect(fromJsonl[1]?.expected).toEqual({ behavior: "Assist" });
  });

  it("parses JSON arrays directly", () => {
    const rows = parseUploadedDataset(
      JSON.stringify([{ input: "A", expected: "B" }]),
      "dataset.json",
    );
    expect(rows).toEqual([
      {
        input: { user_message: "A" },
        expected: { behavior: "B" },
        tags: [],
        weight: undefined,
      },
    ]);
  });

  it("rejects unsupported JSON payload shapes", () => {
    expect(() => parseUploadedDataset('{"foo":"bar"}', "dataset.json")).toThrow(
      "JSON upload must be an array or an object with an examples array.",
    );
  });
});
