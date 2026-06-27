/**
 * Client code snippets for invoking a published workflow service.
 *
 * A service exposes a run-and-poll contract: POST `{endpoint}` returns a
 * `{ run_id, status }` acknowledgement (HTTP 202), then GET
 * `{endpoint/../runs/{run_id}}` is polled until the run reaches a terminal
 * status. Every response is wrapped in a `{ "data": ... }` envelope.
 *
 * These helpers turn a service's stored input JSON-Schema + endpoint +
 * auth flag into ready-to-paste curl / Python / JavaScript, including the
 * Bearer header (when the service requires auth) and the async poll step.
 */

export type SnippetLanguage = "curl" | "python" | "javascript";

export const SNIPPET_LANGUAGES: ReadonlyArray<{ id: SnippetLanguage; label: string }> = [
  { id: "curl", label: "curl" },
  { id: "python", label: "Python" },
  { id: "javascript", label: "JavaScript" },
];

const TERMINAL_STATUSES = ["completed", "failed", "cancelled", "expired"];

/** A representative value for a JSON-Schema fragment (used to fill the sample body). */
function sampleValueForSchema(schema: Record<string, unknown> | undefined): unknown {
  const type = typeof schema?.type === "string" ? schema.type : undefined;
  if (type === "number" || type === "integer") return 0;
  if (type === "boolean") return false;
  if (type === "array") return [];
  if (type === "object" || (schema && "properties" in schema)) {
    const props = (schema?.properties ?? {}) as Record<string, Record<string, unknown>>;
    const out: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(props)) {
      out[key] = sampleValueForSchema(value ?? {});
    }
    return out;
  }
  return "...";
}

/** Derive a sample `input` object from the service's input schema. */
export function sampleInput(
  inputSchema: Record<string, unknown> | undefined,
): Record<string, unknown> {
  const sample = sampleValueForSchema(inputSchema ?? {});
  return sample && typeof sample === "object" && !Array.isArray(sample)
    ? (sample as Record<string, unknown>)
    : {};
}

/** GET base for polling a run, derived from the invoke endpoint. */
function runStatusBase(endpoint: string): string {
  return endpoint.replace(/\/invoke$/, "/runs");
}

/** Render a JSON value as a Python literal (so `false`/`null` become `False`/`None`). */
function toPythonLiteral(value: unknown, indent = 0): string {
  const pad = "    ".repeat(indent);
  const padInner = "    ".repeat(indent + 1);
  if (value === null) return "None";
  if (typeof value === "boolean") return value ? "True" : "False";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) {
    if (value.length === 0) return "[]";
    const items = value.map((v) => `${padInner}${toPythonLiteral(v, indent + 1)}`);
    return `[\n${items.join(",\n")}\n${pad}]`;
  }
  const entries = Object.entries(value as Record<string, unknown>);
  if (entries.length === 0) return "{}";
  const rendered = entries.map(
    ([k, v]) => `${padInner}${JSON.stringify(k)}: ${toPythonLiteral(v, indent + 1)}`,
  );
  return `{\n${rendered.join(",\n")}\n${pad}}`;
}

export interface ServiceSnippetOptions {
  /** Absolute invoke endpoint (origin + service.endpoint). */
  endpoint: string;
  inputSchema: Record<string, unknown> | undefined;
  authRequired: boolean;
  language: SnippetLanguage;
}

function curlSnippet(endpoint: string, body: string, authRequired: boolean): string {
  const auth = authRequired ? "  -H 'Authorization: Bearer YOUR_TOKEN' \\\n" : "";
  const pollAuth = authRequired ? " \\\n  -H 'Authorization: Bearer YOUR_TOKEN'" : "";
  return [
    "# 1. Invoke the workflow — returns a run id (HTTP 202)",
    `curl -X POST '${endpoint}' \\`,
    "  -H 'Content-Type: application/json' \\",
    `${auth}  -d '${body}'`,
    "",
    "# 2. Poll for the result (replace RUN_ID with the run_id from step 1)",
    `curl '${runStatusBase(endpoint)}/RUN_ID'${pollAuth}`,
  ].join("\n");
}

function pythonSnippet(
  endpoint: string,
  sample: Record<string, unknown>,
  authRequired: boolean,
): string {
  const headerDef = authRequired ? '\nHEADERS = {"Authorization": "Bearer YOUR_TOKEN"}' : "";
  const headerArg = authRequired ? ", headers=HEADERS" : "";
  return [
    "import time",
    "import requests",
    "",
    `BASE = "${endpoint}"${headerDef}`,
    `payload = {"input": ${toPythonLiteral(sample)}}`,
    "",
    `ack = requests.post(BASE, json=payload${headerArg}).json()["data"]`,
    'run_id = ack["run_id"]',
    "",
    `status_url = f"${runStatusBase(endpoint)}/{run_id}"`,
    `terminal = ${toPythonLiteral(TERMINAL_STATUSES)}`,
    "while True:",
    `    run = requests.get(status_url${headerArg}).json()["data"]`,
    '    if run["status"] in terminal:',
    "        break",
    "    time.sleep(1)",
    "print(run)",
  ].join("\n");
}

function javascriptSnippet(
  endpoint: string,
  sample: Record<string, unknown>,
  authRequired: boolean,
): string {
  const payload = JSON.stringify({ input: sample }, null, 2);
  const headers = authRequired
    ? '{ "Content-Type": "application/json", Authorization: "Bearer YOUR_TOKEN" }'
    : '{ "Content-Type": "application/json" }';
  const pollHeaders = authRequired
    ? ', { headers: { Authorization: "Bearer YOUR_TOKEN" } }'
    : "";
  return [
    `const BASE = "${endpoint}";`,
    `const payload = ${payload};`,
    "",
    "const ack = await fetch(BASE, {",
    '  method: "POST",',
    `  headers: ${headers},`,
    "  body: JSON.stringify(payload),",
    "}).then((r) => r.json());",
    "let run = ack.data;",
    "",
    `const statusUrl = \`${runStatusBase(endpoint)}/\${run.run_id}\`;`,
    `const terminal = ${JSON.stringify(TERMINAL_STATUSES)};`,
    "while (!terminal.includes(run.status)) {",
    "  await new Promise((r) => setTimeout(r, 1000));",
    `  run = (await fetch(statusUrl${pollHeaders}).then((r) => r.json())).data;`,
    "}",
    "console.log(run);",
  ].join("\n");
}

/** Build a client snippet in the requested language. */
export function buildServiceSnippet(options: ServiceSnippetOptions): string {
  const { endpoint, inputSchema, authRequired, language } = options;
  const sample = sampleInput(inputSchema);
  if (language === "python") return pythonSnippet(endpoint, sample, authRequired);
  if (language === "javascript") return javascriptSnippet(endpoint, sample, authRequired);
  return curlSnippet(endpoint, JSON.stringify({ input: sample }), authRequired);
}
