import { describe, expect, it } from "vitest";

import { diffLines, diffStats, type DiffOp } from "@/lib/textDiff";

/** Reconstruct one side ("left"|"right") from the diff and assert it is lossless. */
function side(left: string, right: string, which: "left" | "right"): string {
  const keep: DiffOp = which === "left" ? "delete" : "insert";
  return diffLines(left, right)
    .filter((l) => l.op === "equal" || l.op === keep)
    .map((l) => l.text)
    .join("\n");
}

describe("diffLines", () => {
  it("marks all lines equal for identical text", () => {
    const text = "line a\nline b\nline c";
    const lines = diffLines(text, text);
    expect(lines.every((l) => l.op === "equal")).toBe(true);
    expect(diffStats(lines)).toEqual({ additions: 0, deletions: 0 });
  });

  it("is lossless — equal+delete lines rejoin to the left, equal+insert to the right", () => {
    const left = "alpha\nbeta\ngamma\ndelta";
    const right = "alpha\nbeta CHANGED\ngamma\nepsilon";
    expect(side(left, right, "left")).toBe(left);
    expect(side(left, right, "right")).toBe(right);
  });

  it("emits a changed line as a delete row followed by an insert row with word parts", () => {
    const lines = diffLines("the quick brown fox", "the slow brown fox");
    expect(lines.map((l) => l.op)).toEqual(["delete", "insert"]);
    const del = lines[0]!;
    const ins = lines[1]!;
    // Word parts are lossless per side and isolate only the changed word.
    expect(del.words?.map((w) => w.value).join("")).toBe("the quick brown fox");
    expect(ins.words?.map((w) => w.value).join("")).toBe("the slow brown fox");
    expect(del.words?.find((w) => w.op === "delete")?.value).toBe("quick");
    expect(ins.words?.find((w) => w.op === "insert")?.value).toBe("slow");
  });

  it("counts pure additions and deletions", () => {
    const lines = diffLines("keep\nremove me", "keep\nadd one\nadd two");
    const stats = diffStats(lines);
    expect(stats.additions).toBeGreaterThanOrEqual(2);
    expect(stats.deletions).toBeGreaterThanOrEqual(1);
  });

  it("handles empty inputs", () => {
    expect(diffStats(diffLines("", ""))).toEqual({ additions: 0, deletions: 0 });
    const added = diffLines("", "hello");
    expect(added.some((l) => l.op === "insert")).toBe(true);
  });
});
