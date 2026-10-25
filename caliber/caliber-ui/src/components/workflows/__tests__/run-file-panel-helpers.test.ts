import { describe, expect, it } from "vitest";

import {
  formatBytes,
  formatDate,
  isActiveRunStatus,
  isStoppedRunStatus,
  kindTone,
  persistenceLabel,
  persistenceTone,
  producerLabel,
  sortFiles,
  statusTone,
} from "@/components/workflows/RunFilePanel";

describe("RunFilePanel presentation helpers", () => {
  it("formats sizes and dates at every boundary", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(1023)).toBe("1023 B");
    expect(formatBytes(1024)).toBe("1.0 KB");
    expect(formatBytes(1024 * 1024)).toBe("1.0 MB");
    expect(formatDate(null)).toBe("—");
    expect(formatDate("invalid-date")).toBe("invalid-date");
    expect(formatDate("2026-01-01T00:00:00Z")).not.toBe("—");
  });

  it("maps file, persistence, and run statuses to stable tones", () => {
    for (const kind of ["input", "work", "artifact", "log", "unknown"]) {
      expect(kindTone(kind)).toContain("border-");
    }
    for (const status of [
      "artifact",
      "attached",
      "uploaded",
      "completed",
      "pending",
      "pending_upload",
      "failed",
      "rejected",
      "unknown",
    ]) {
      expect(statusTone(status)).toContain("border-");
    }
    expect(persistenceTone("persisted")).toContain("emerald");
    expect(persistenceTone("failed")).toContain("red");
    expect(persistenceTone("pending")).toContain("slate");
    expect(persistenceLabel("persisted")).toBe("Persisted");
    expect(persistenceLabel("failed")).toBe("Failed");
    expect(persistenceLabel("other")).toBe("other");
  });

  it("classifies active and stopped lifecycle statuses", () => {
    for (const status of [
      "queued",
      "running",
      "resuming",
      "cancel_requested",
      "waiting_approval",
      "waiting_event",
    ]) {
      expect(isActiveRunStatus(status)).toBe(true);
    }
    expect(isActiveRunStatus(null)).toBe(false);
    expect(isActiveRunStatus("completed")).toBe(false);
    for (const status of ["failed", "cancelled", "rejected", "expired", "blocked"]) {
      expect(isStoppedRunStatus(status)).toBe(true);
    }
    expect(isStoppedRunStatus(null)).toBe(false);
    expect(isStoppedRunStatus("running")).toBe(false);
  });

  it("labels producers and sorts newest files before path ties", () => {
    const base = {
      file_id: "file",
      file_ref: "ref",
      name: "file",
      kind: "artifact",
      relative_path: "z.txt",
      media_type: "text/plain",
      size_bytes: 1,
      sha256: null,
      status: "attached",
      producer_node_id: null,
      created_at: "2026-01-01T00:00:00Z",
    } as never;
    expect(producerLabel({ ...base, producer_node_id: "node-1" })).toBe("node-1");
    expect(producerLabel({ ...base, kind: "input" })).toBe("manual input");
    expect(producerLabel({ ...base, kind: "log" })).toBe("runtime log");
    expect(producerLabel(base)).toBe("unassigned");

    const files = [
      { ...base, file_id: "late", relative_path: "z.txt", created_at: "2026-01-02T00:00:00Z" },
      { ...base, file_id: "same-z", relative_path: "z.txt", created_at: null },
      { ...base, file_id: "same-a", relative_path: "a.txt", created_at: null },
    ];
    expect(sortFiles(files as never).map((file) => file.file_id)).toEqual([
      "late",
      "same-a",
      "same-z",
    ]);
  });
});
