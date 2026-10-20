import type { EvalExampleCreatePayload } from "@/api/types";

/**
 * Parse a test-set file into example payloads: JSON Lines (one object per line) or one JSON array.
 * Every row needs an `input` object; `expected` (object), `tags` (strings) and `weight` (number >= 0)
 * are optional, and any other key (for example an `id` kept for the reader's benefit) is ignored.
 * Returns the rows, or the first problem with the line it is on.
 */
export function parseExampleFile(
  text: string,
): { rows: EvalExampleCreatePayload[] } | { error: string } {
  const trimmed = text.trim();
  if (!trimmed) return { error: "The file is empty." };
  let records: { value: unknown; label: string }[];
  if (trimmed.startsWith("[")) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(trimmed);
    } catch {
      return { error: "The file starts with [ but is not a valid JSON array." };
    }
    records = (parsed as unknown[]).map((value, index) => ({
      value,
      label: `Row ${index + 1}`,
    }));
  } else {
    records = [];
    const lines = trimmed.split(/\r?\n/);
    for (const [index, line] of lines.entries()) {
      if (!line.trim()) continue;
      try {
        records.push({ value: JSON.parse(line), label: `Line ${index + 1}` });
      } catch {
        return { error: `Line ${index + 1} is not valid JSON.` };
      }
    }
  }
  if (records.length === 0) return { error: "The file has no rows." };
  const rows: EvalExampleCreatePayload[] = [];
  for (const { value, label } of records) {
    if (typeof value !== "object" || value === null || Array.isArray(value)) {
      return { error: `${label} must be a JSON object.` };
    }
    const record = value as Record<string, unknown>;
    const input = record["input"];
    if (typeof input !== "object" || input === null || Array.isArray(input)) {
      return { error: `${label} needs an "input" object.` };
    }
    const expected = record["expected"] ?? {};
    if (
      typeof expected !== "object" ||
      expected === null ||
      Array.isArray(expected)
    ) {
      return { error: `${label}: "expected" must be an object.` };
    }
    const tags = record["tags"] ?? [];
    if (!Array.isArray(tags) || tags.some((tag) => typeof tag !== "string")) {
      return { error: `${label}: "tags" must be a list of strings.` };
    }
    const weight = record["weight"] ?? 1;
    if (typeof weight !== "number" || !Number.isFinite(weight) || weight < 0) {
      return { error: `${label}: "weight" must be a number >= 0.` };
    }
    rows.push({
      input: input as Record<string, unknown>,
      expected: expected as Record<string, unknown>,
      tags: tags as string[],
      weight,
    });
  }
  return { rows };
}
