import { describe, expect, it } from "vitest";

import {
  clampNumber,
  compareIsoDates,
  extOf,
  fileIconFor,
  fileTypeOf,
  fmtDate,
  humanSize,
  previewKindFor,
  readPaneWidth,
} from "@/pages/ObjectStore";

describe("ObjectStore helper decisions", () => {
  it("normalizes extensions, sizes, dates, and pane widths", () => {
    expect(extOf("")).toBe("");
    expect(extOf("README.MD")).toBe("md");
    expect(extOf("no-extension")).toBe("no-extension");
    expect(clampNumber(10, 0, 20)).toBe(10);
    expect(clampNumber(-1, 0, 20)).toBe(0);
    expect(clampNumber(30, 0, 20)).toBe(20);
    expect(clampNumber(5, 20, 10)).toBe(20);
    expect(humanSize(12)).toBe("12 B");
    expect(humanSize(1024)).toBe("1.0 KB");
    expect(humanSize(1024 * 1024 * 12)).toBe("12 MB");
    expect(humanSize(1024 ** 3 * 2)).toBe("2.0 GB");
    expect(humanSize(1024 ** 4)).toBe("1.0 TB");
    expect(fmtDate(null)).toBe("—");
    expect(fmtDate("invalid")).toBe("—");
    expect(fmtDate("2026-01-02T03:04:00Z")).toContain("2026");

    window.localStorage.removeItem("coverage-pane");
    expect(readPaneWidth("coverage-pane", 300)).toBe(300);
    window.localStorage.setItem("coverage-pane", "512");
    expect(readPaneWidth("coverage-pane", 300)).toBe(512);
    window.localStorage.setItem("coverage-pane", "0");
    expect(readPaneWidth("coverage-pane", 300)).toBe(300);
    window.localStorage.setItem("coverage-pane", "not-a-number");
    expect(readPaneWidth("coverage-pane", 300)).toBe(300);
  });

  it("classifies every supported file family and preview strategy", () => {
    const families: Array<[string, string]> = [
      ["photo.png", "image"],
      ["sound.mp3", "audio"],
      ["movie.webm", "video"],
      ["report.docx", "document"],
      ["script.ts", "code"],
      ["notes.md", "text"],
      ["archive.zip", "archive"],
      ["unknown.bin", "other"],
    ];
    for (const [name, type] of families) expect(fileTypeOf(name)).toBe(type);
    for (const name of [
      "photo.png",
      "sound.mp3",
      "movie.webm",
      "sheet.xlsx",
      "report.docx",
      "script.ts",
      "notes.md",
      "archive.zip",
      "unknown.bin",
    ]) {
      expect(fileIconFor(name)).toBeDefined();
    }

    expect(previewKindFor("notes.md", "text/plain")).toBe("markdown");
    expect(previewKindFor("data.csv", "text/csv")).toBe("csv");
    expect(previewKindFor("photo.bin", "image/png")).toBe("image");
    expect(previewKindFor("report.pdf", "application/octet-stream")).toBe("pdf");
    expect(previewKindFor("sound.bin", "audio/mpeg")).toBe("audio");
    expect(previewKindFor("movie.bin", "video/mp4")).toBe("video");
    expect(previewKindFor("report.docx", "application/octet-stream")).toBe("office");
    expect(previewKindFor("unknown.bin", "application/octet-stream")).toBe("none");
  });

  it("sorts ISO dates with null and invalid values deterministically", () => {
    expect(compareIsoDates(null, null)).toBe(0);
    expect(compareIsoDates(null, "2026-01-01T00:00:00Z")).toBe(-1);
    expect(compareIsoDates("2026-01-02T00:00:00Z", null)).toBe(1);
    expect(compareIsoDates("invalid", "also-invalid")).toBe(0);
    expect(compareIsoDates("invalid", "2026-01-01T00:00:00Z")).toBe(-1);
    expect(compareIsoDates("2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z")).toBeGreaterThan(0);
  });
});
