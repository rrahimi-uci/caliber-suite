type JsonSchema = Record<string, unknown>;

interface ToolSignatureProps {
  title: string;
  schema?: JsonSchema | null;
  emptyLabel?: string;
  testId?: string;
}

interface PropertyRow {
  name: string;
  typeLabel: string;
  required: boolean;
  description: string | null;
}

export function ToolSignature({
  title,
  schema,
  emptyLabel = "No schema declared.",
  testId,
}: ToolSignatureProps): JSX.Element {
  const rows = propertyRows(schema);
  const rootType = typeLabel(schema);

  return (
    <div data-testid={testId} className="rounded-md border border-zinc-200 bg-zinc-50 p-3">
      <div className="mb-2 flex items-center justify-between gap-3">
        <h4 className="text-[11px] font-semibold uppercase tracking-wide text-zinc-600">
          {title}
        </h4>
        <span className="rounded bg-white px-1.5 py-0.5 font-mono text-[10px] text-zinc-500 ring-1 ring-zinc-200">
          {rootType}
        </span>
      </div>

      {!schema ? (
        <p className="text-xs text-zinc-500">{emptyLabel}</p>
      ) : rows.length === 0 ? (
        <p className="text-xs text-zinc-500">
          {rootType === "object" ? "No named fields." : `Root value: ${rootType}.`}
        </p>
      ) : (
        <div className="space-y-2">
          {rows.map((row) => (
            <div key={row.name} className="rounded border border-zinc-200 bg-white px-2.5 py-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-xs font-semibold text-zinc-900">
                  {row.name}
                </span>
                <span className="rounded bg-zinc-100 px-1.5 py-0.5 font-mono text-[10px] text-zinc-600">
                  {row.typeLabel}
                </span>
                {row.required && (
                  <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 ring-1 ring-amber-200">
                    required
                  </span>
                )}
              </div>
              {row.description && (
                <p className="mt-1 text-[11px] leading-relaxed text-zinc-500">
                  {row.description}
                </p>
              )}
            </div>
          ))}
        </div>
      )}

      {schema && (
        <details className="mt-2">
          <summary className="cursor-pointer select-none text-[10px] font-medium text-zinc-500 hover:text-zinc-700">
            Raw schema
          </summary>
          <pre className="mt-2 max-h-44 overflow-auto rounded border border-zinc-200 bg-white p-2 font-mono text-[10px] leading-relaxed text-zinc-600">
            {JSON.stringify(schema, null, 2)}
          </pre>
        </details>
      )}
    </div>
  );
}

function propertyRows(schema: JsonSchema | null | undefined): PropertyRow[] {
  const properties = schemaObject(schema?.properties);
  if (!properties) return [];

  const required = new Set(arrayOfStrings(schema?.required));
  return Object.entries(properties).map(([name, raw]) => {
    const prop = schemaObject(raw) ?? {};
    return {
      name,
      typeLabel: typeLabel(prop),
      required: required.has(name),
      description: typeof prop.description === "string" ? prop.description : null,
    };
  });
}

function typeLabel(schema: JsonSchema | null | undefined): string {
  if (!schema) return "unknown";
  const rawType = schema.type;
  if (typeof rawType === "string") return rawType;
  if (Array.isArray(rawType)) {
    const values = rawType.filter((value): value is string => typeof value === "string");
    if (values.length > 0) return values.join(" | ");
  }
  if (schema.enum && Array.isArray(schema.enum)) return "enum";
  if (schema.anyOf) return "anyOf";
  if (schema.oneOf) return "oneOf";
  if (schema.allOf) return "allOf";
  return "unknown";
}

function schemaObject(value: unknown): JsonSchema | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as JsonSchema;
}

function arrayOfStrings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string");
}
