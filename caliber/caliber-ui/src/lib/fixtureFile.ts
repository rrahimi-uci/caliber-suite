import type {
  CalibrationAssertionType,
  CalibrationCase,
} from "@/api/workflowTypes";

const ASSERTION_TYPES: CalibrationAssertionType[] = [
  "no_error",
  "output_contains",
  "equals",
];

/**
 * Parse a fixtures file into saved test cases: JSON Lines (one object per line) or one JSON array.
 *
 * A row is `{name, input, assertion: {type, value}}` (the shape the API stores), or the shorter form
 * a lab writes by hand: `{id, name, inputs, expectations}`, where the expected output is a JSON
 * object and the case asserts that the tool returns exactly it (`equals`). `input` and `inputs`
 * are the same; an `id` is put in front of the name so rows stay traceable; `tags` and other keys
 * are ignored. A row with neither an assertion nor an expected output only has to run without error.
 * Returns the cases, or the first problem with the line it is on.
 */
export function parseFixtureFile(
  text: string,
): { cases: CalibrationCase[] } | { error: string } {
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
    for (const [index, line] of trimmed.split(/\r?\n/).entries()) {
      if (!line.trim()) continue;
      try {
        records.push({ value: JSON.parse(line), label: `Line ${index + 1}` });
      } catch {
        return { error: `Line ${index + 1} is not valid JSON.` };
      }
    }
  }
  if (records.length === 0) return { error: "The file has no rows." };

  const cases: CalibrationCase[] = [];
  const seen = new Set<string>();
  for (const { value, label } of records) {
    if (typeof value !== "object" || value === null || Array.isArray(value)) {
      return { error: `${label} must be a JSON object.` };
    }
    const row = value as Record<string, unknown>;
    const rawName = typeof row["name"] === "string" ? row["name"].trim() : "";
    const id =
      typeof row["id"] === "string" || typeof row["id"] === "number"
        ? String(row["id"]).trim()
        : "";
    const name = [id, rawName].filter(Boolean).join(" ");
    if (!name) return { error: `${label} needs a "name" (or an "id").` };
    if (seen.has(name))
      return {
        error: `${label}: the case name "${name}" appears twice; names must be unique.`,
      };
    seen.add(name);

    const input = row["input"] ?? row["inputs"] ?? {};
    if (typeof input !== "object" || input === null || Array.isArray(input)) {
      return { error: `${label} ("${name}"): "input" must be a JSON object.` };
    }

    const assertion = row["assertion"];
    if (assertion !== undefined) {
      if (
        typeof assertion !== "object" ||
        assertion === null ||
        Array.isArray(assertion)
      ) {
        return {
          error: `${label} ("${name}"): "assertion" must be an object with a "type".`,
        };
      }
      const spec = assertion as Record<string, unknown>;
      const type = spec["type"] ?? "no_error";
      if (!ASSERTION_TYPES.includes(type as CalibrationAssertionType)) {
        return {
          error: `${label} ("${name}"): assertion type "${String(type)}" is not one of ${ASSERTION_TYPES.join(", ")}.`,
        };
      }
      const rawValue = spec["value"];
      const needsValue = type !== "no_error";
      const assertionValue =
        rawValue === undefined || rawValue === null
          ? ""
          : typeof rawValue === "string"
            ? rawValue
            : JSON.stringify(rawValue);
      if (needsValue && !assertionValue) {
        return {
          error: `${label} ("${name}"): assertion "${String(type)}" needs a "value".`,
        };
      }
      cases.push({
        name,
        input: input as Record<string, unknown>,
        assertion: {
          type: type as CalibrationAssertionType,
          value: needsValue ? assertionValue : null,
        },
      });
      continue;
    }

    const expected = row["expectations"] ?? row["expected"];
    if (expected === undefined) {
      cases.push({
        name,
        input: input as Record<string, unknown>,
        assertion: { type: "no_error", value: null },
      });
    } else {
      cases.push({
        name,
        input: input as Record<string, unknown>,
        assertion: { type: "equals", value: JSON.stringify(expected) },
      });
    }
  }
  return { cases };
}
