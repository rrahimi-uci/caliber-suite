/**
 * No-code editor for a guardrail node's checks (Lakeflow "every operator is a
 * clear, configurable form"). Mirrors the backend's closed check vocabulary
 * (`caliber/workflows/guardrails.py`). Each check is stored in the manifest as
 * a single-key object `{ <kind>: <params> }`.
 */

import { useState } from "react";

type ParamKind = "string" | "number" | "csv" | "entities";

interface ParamSpec {
  key: string;
  label: string;
  kind: ParamKind;
}

interface CheckSpec {
  kind: string;
  label: string;
  params: ParamSpec[];
}

/** The 8 check kinds the runtime supports, with their configurable params. */
const CHECK_CATALOG: CheckSpec[] = [
  {
    kind: "tool_required_before_claim",
    label: "Tool required before claim",
    params: [
      { key: "tool", label: "Required tool", kind: "string" },
      { key: "categories", label: "Categories (comma-separated)", kind: "csv" },
    ],
  },
  { kind: "non_empty_output", label: "Non-empty output", params: [] },
  {
    kind: "max_length",
    label: "Max length",
    params: [{ key: "max_chars", label: "Max characters", kind: "number" }],
  },
  {
    kind: "forbid_substring",
    label: "Forbid substring",
    params: [{ key: "substring", label: "Forbidden substring", kind: "string" }],
  },
  {
    kind: "pii_detection",
    label: "PII detection",
    params: [{ key: "entities", label: "Entities", kind: "entities" }],
  },
  {
    kind: "toxicity_check",
    label: "Toxicity check",
    params: [{ key: "threshold", label: "Threshold (advisory)", kind: "number" }],
  },
  {
    kind: "budget_limit",
    label: "Budget limit",
    params: [{ key: "max_usd", label: "Max USD", kind: "number" }],
  },
  {
    kind: "schema_validation",
    label: "Schema validation",
    params: [{ key: "required_fields", label: "Required fields (comma-separated)", kind: "csv" }],
  },
];

const PII_ENTITIES = ["email", "ssn", "phone", "credit_card"];

const CATALOG_BY_KIND: Record<string, CheckSpec> = Object.fromEntries(
  CHECK_CATALOG.map((c) => [c.kind, c]),
);

type Check = Record<string, unknown>;

/**
 * Read a check's kind, tolerating both manifest shapes:
 *   - single-key sugar `{ <kind>: <params> }` (the Studio/FE convention), and
 *   - canonical `{ "kind": <kind>, "params": <params> }` (what the server's
 *     `manifest.to_dict()` emits, e.g. for imported workflows).
 */
function checkKind(check: Check): string {
  if (typeof check.kind === "string") return check.kind;
  return Object.keys(check)[0] ?? "";
}
function checkParams(check: Check): Record<string, unknown> {
  const p = typeof check.kind === "string" ? check.params : check[checkKind(check)];
  return p && typeof p === "object" ? (p as Record<string, unknown>) : {};
}

interface GuardrailChecksEditorProps {
  checks: Check[];
  onChange: (checks: Check[]) => void;
}

export function GuardrailChecksEditor({
  checks,
  onChange,
}: GuardrailChecksEditorProps): JSX.Element {
  const [addKind, setAddKind] = useState<string>(CHECK_CATALOG[0]!.kind);

  function setParam(index: number, key: string, value: unknown): void {
    onChange(
      checks.map((c, i) => {
        if (i !== index) return c;
        const kind = checkKind(c);
        return { [kind]: { ...checkParams(c), [key]: value } };
      }),
    );
  }

  function addCheck(): void {
    onChange([...checks, { [addKind]: {} }]);
  }

  function removeCheck(index: number): void {
    onChange(checks.filter((_, i) => i !== index));
  }

  return (
    <div data-testid="guardrail-checks" className="space-y-2">
      {checks.length === 0 && (
        <div className="rounded-lg border border-dashed border-zinc-200 px-3 py-2 text-xs text-zinc-400">
          No checks yet — add one below.
        </div>
      )}

      {checks.map((check, index) => {
        const kind = checkKind(check);
        const spec = CATALOG_BY_KIND[kind];
        const params = checkParams(check);
        return (
          <div
            key={index}
            data-testid={`check-row-${index}`}
            className="rounded-lg border border-zinc-200 bg-white p-2.5"
          >
            <div className="mb-1.5 flex items-center justify-between gap-2">
              <span className="text-xs font-semibold text-zinc-800">{spec?.label ?? kind}</span>
              <button
                type="button"
                data-testid={`check-remove-${index}`}
                onClick={() => removeCheck(index)}
                className="text-[11px] text-zinc-400 transition-colors hover:text-red-600"
                title="Remove check"
              >
                ✕
              </button>
            </div>

            {spec && spec.params.length === 0 && (
              <div className="text-[11px] text-zinc-400">No configuration.</div>
            )}

            {spec?.params.map((p) => (
              <div key={p.key} className="mb-1.5 last:mb-0">
                <label className="mb-0.5 block text-[11px] font-medium text-zinc-500">{p.label}</label>
                {p.kind === "entities" ? (
                  <div className="flex flex-wrap gap-2">
                    {PII_ENTITIES.map((entity) => {
                      const current = Array.isArray(params[p.key]) ? (params[p.key] as string[]) : [];
                      const on = current.includes(entity);
                      return (
                        <label key={entity} className="flex items-center gap-1 text-[11px] text-zinc-600">
                          <input
                            type="checkbox"
                            data-testid={`check-${index}-entity-${entity}`}
                            checked={on}
                            onChange={() =>
                              setParam(
                                index,
                                p.key,
                                on ? current.filter((x) => x !== entity) : [...current, entity],
                              )
                            }
                          />
                          {entity}
                        </label>
                      );
                    })}
                  </div>
                ) : (
                  <input
                    data-testid={`check-${index}-param-${p.key}`}
                    aria-label={p.label}
                    type={p.kind === "number" ? "number" : "text"}
                    value={
                      p.kind === "csv"
                        ? Array.isArray(params[p.key])
                          ? (params[p.key] as string[]).join(", ")
                          : ""
                        : p.kind === "number"
                          ? typeof params[p.key] === "number"
                            ? (params[p.key] as number)
                            : ""
                          : typeof params[p.key] === "string"
                            ? (params[p.key] as string)
                            : ""
                    }
                    onChange={(e) => {
                      const raw = e.target.value;
                      if (p.kind === "number") {
                        setParam(index, p.key, raw === "" ? "" : Number(raw));
                      } else if (p.kind === "csv") {
                        setParam(
                          index,
                          p.key,
                          raw
                            .split(",")
                            .map((s) => s.trim())
                            .filter(Boolean),
                        );
                      } else {
                        setParam(index, p.key, raw);
                      }
                    }}
                    className="w-full rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs text-zinc-800 focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900"
                  />
                )}
              </div>
            ))}
          </div>
        );
      })}

      <div className="flex items-center gap-2">
        <select
          data-testid="check-add-kind"
          aria-label="Check kind to add"
          value={addKind}
          onChange={(e) => setAddKind(e.target.value)}
          className="flex-1 rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs text-zinc-700 focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900"
        >
          {CHECK_CATALOG.map((c) => (
            <option key={c.kind} value={c.kind}>
              {c.label}
            </option>
          ))}
        </select>
        <button
          type="button"
          data-testid="check-add"
          onClick={addCheck}
          className="rounded-md border border-zinc-200 px-2.5 py-1 text-xs font-medium text-zinc-700 transition-colors hover:bg-zinc-50 active:scale-[0.97]"
        >
          + Add check
        </button>
      </div>
    </div>
  );
}
