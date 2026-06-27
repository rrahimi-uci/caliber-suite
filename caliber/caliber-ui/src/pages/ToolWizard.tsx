/**
 * Tool Creation Wizard — 5-step guided flow for registering tools.
 *
 * Steps:
 *   1. Identity   — name, version, description, owner
 *   2. Implementation — module_path, callable_name
 *   3. Schema     — input_schema / output_schema (visual + raw JSON)
 *   4. Playground — sandbox test-run with auto-generated input form
 *   5. Safety & Review — side_effect_level, governance toggles, summary
 *
 * Uses the existing sandbox infrastructure for Step 4 (preview-run).
 */

import { useCallback, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { caliberApi } from "@/api/caliberApi";
import type { ToolDefinition, ToolTestRunResult } from "@/api/workflowTypes";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { useApiMutation, useInvalidate } from "@/hooks/useApiQuery";

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

interface SchemaProperty {
  id: string;
  name: string;
  type: "string" | "number" | "integer" | "boolean" | "array" | "object";
  description: string;
  required: boolean;
}

interface WizardFormData {
  // Step 1
  name: string;
  version: string;
  description: string;
  owner: string;
  // Step 2
  module_path: string;
  callable_name: string;
  // Step 3
  inputProperties: SchemaProperty[];
  outputProperties: SchemaProperty[];
  inputSchemaRaw: string;
  outputSchemaRaw: string;
  useRawInput: boolean;
  useRawOutput: boolean;
  // Step 5
  side_effect_level: "read" | "write" | "external_action";
  requires_approval: boolean;
  allow_in_preview: boolean;
  secret_refs: string[];
}

const INITIAL_FORM: WizardFormData = {
  name: "",
  version: "1.0",
  description: "",
  owner: "",
  module_path: "caliber.workflows.demo_tools",
  callable_name: "",
  inputProperties: [],
  outputProperties: [],
  inputSchemaRaw: "{}",
  outputSchemaRaw: "{}",
  useRawInput: false,
  useRawOutput: false,
  side_effect_level: "read",
  requires_approval: false,
  allow_in_preview: false,
  secret_refs: [],
};

const STEPS = [
  { label: "Identity", icon: "1" },
  { label: "Implementation", icon: "2" },
  { label: "Schema", icon: "3" },
  { label: "Playground", icon: "4" },
  { label: "Safety & Review", icon: "5" },
] as const;

function generateId(): string {
  return Math.random().toString(36).slice(2, 9);
}

/* ------------------------------------------------------------------ */
/*  Schema helper: properties ↔ JSON Schema                           */
/* ------------------------------------------------------------------ */

export function propertiesToSchema(props: SchemaProperty[]): Record<string, unknown> | null {
  if (props.length === 0) return null;
  const properties: Record<string, unknown> = {};
  const required: string[] = [];
  for (const p of props) {
    properties[p.name] = { type: p.type, description: p.description || undefined };
    if (p.required) required.push(p.name);
  }
  return { type: "object", properties, ...(required.length ? { required } : {}) };
}

export function schemaToProperties(schema: Record<string, unknown> | null): SchemaProperty[] {
  if (!schema || typeof schema !== "object") return [];
  const props = (schema as Record<string, unknown>).properties;
  const req = ((schema as Record<string, unknown>).required as string[]) ?? [];
  if (!props || typeof props !== "object") return [];
  return Object.entries(props as Record<string, Record<string, unknown>>).map(([name, def]) => ({
    id: generateId(),
    name,
    type: (def.type as SchemaProperty["type"]) ?? "string",
    description: (def.description as string) ?? "",
    required: req.includes(name),
  }));
}

/* ------------------------------------------------------------------ */
/*  Step indicator                                                     */
/* ------------------------------------------------------------------ */

function StepIndicator({ current, onGoTo }: { current: number; onGoTo: (i: number) => void }): JSX.Element {
  return (
    <nav data-testid="wizard-steps" className="mb-8 flex items-center justify-between">
      {STEPS.map((step, i) => {
        const done = i < current;
        const active = i === current;
        return (
          <button
            key={step.label}
            type="button"
            data-testid={`wizard-step-${i}`}
            onClick={() => i < current && onGoTo(i)}
            disabled={i > current}
            className="group flex flex-1 flex-col items-center gap-1.5"
          >
            <div className="flex w-full items-center">
              {i > 0 && (
                <div className={`h-0.5 flex-1 transition-colors ${done ? "bg-caliber-purple" : "bg-surface-200"}`} />
              )}
              <div
                className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-xs font-semibold transition-all
                  ${active ? "bg-caliber-purple text-white ring-2 ring-caliber-purple/30" : ""}
                  ${done ? "bg-caliber-purple text-white" : ""}
                  ${!active && !done ? "bg-surface-100 text-gray-400" : ""}
                `}
              >
                {done ? (
                  <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3">
                    <polyline points="20 6 9 17 4 12" />
                  </svg>
                ) : (
                  step.icon
                )}
              </div>
              {i < STEPS.length - 1 && (
                <div className={`h-0.5 flex-1 transition-colors ${done ? "bg-caliber-purple" : "bg-surface-200"}`} />
              )}
            </div>
            <span className={`text-xs font-medium ${active ? "text-caliber-purple" : done ? "text-gray-600" : "text-gray-400"}`}>
              {step.label}
            </span>
          </button>
        );
      })}
    </nav>
  );
}

/* ------------------------------------------------------------------ */
/*  Step 1: Identity                                                   */
/* ------------------------------------------------------------------ */

function IdentityStep({
  form,
  onChange,
}: {
  form: WizardFormData;
  onChange: (patch: Partial<WizardFormData>) => void;
}): JSX.Element {
  return (
    <div data-testid="step-identity" className="space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-gray-900">What is this tool?</h2>
        <p className="text-sm text-gray-500">Give your tool a clear name and description so agents know when to use it.</p>
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label htmlFor="wiz-name">Name *</Label>
          <Input
            id="wiz-name"
            data-testid="wiz-name"
            placeholder="e.g. lookup_order"
            value={form.name}
            onChange={(e) =>
              onChange({
                name: e.target.value,
                callable_name: form.callable_name === "" || form.callable_name === toSnake(form.name)
                  ? toSnake(e.target.value)
                  : form.callable_name,
              })
            }
          />
          <p className="text-xs text-gray-400">Unique identifier. Will be used as the function reference.</p>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="wiz-version">Version</Label>
          <Input
            id="wiz-version"
            data-testid="wiz-version"
            placeholder="1.0"
            value={form.version}
            onChange={(e) => onChange({ version: e.target.value })}
          />
        </div>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="wiz-description">Description</Label>
        <Textarea
          id="wiz-description"
          data-testid="wiz-description"
          placeholder="Describe what this tool does, when it should be called, and what it returns…"
          rows={3}
          value={form.description}
          onChange={(e) => onChange({ description: e.target.value })}
        />
        <p className="text-xs text-gray-400">A clear description helps the LLM decide when to invoke this tool.</p>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="wiz-owner">Owner</Label>
        <Input
          id="wiz-owner"
          data-testid="wiz-owner"
          placeholder="@team-name or user"
          value={form.owner}
          onChange={(e) => onChange({ owner: e.target.value })}
        />
      </div>
    </div>
  );
}

export function toSnake(s: string): string {
  return s.replace(/[^a-zA-Z0-9]+/g, "_").replace(/([A-Z])/g, "_$1").toLowerCase().replace(/^_+|_+$/g, "").replace(/_+/g, "_");
}

/* ------------------------------------------------------------------ */
/*  Step 2: Implementation                                             */
/* ------------------------------------------------------------------ */

function ImplementationStep({
  form,
  onChange,
}: {
  form: WizardFormData;
  onChange: (patch: Partial<WizardFormData>) => void;
}): JSX.Element {
  return (
    <div data-testid="step-implementation" className="space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-gray-900">Where does it live?</h2>
        <p className="text-sm text-gray-500">Point to the Python module and callable that implements this tool.</p>
      </div>
      <div className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="wiz-module">Module Path *</Label>
          <Input
            id="wiz-module"
            data-testid="wiz-module"
            placeholder="caliber.workflows.demo_tools"
            value={form.module_path}
            onChange={(e) => onChange({ module_path: e.target.value })}
          />
          <p className="text-xs text-gray-400">Python dotted import path (e.g. <code className="text-gray-500">myapp.tools.orders</code>).</p>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="wiz-callable">Callable Name *</Label>
          <Input
            id="wiz-callable"
            data-testid="wiz-callable"
            placeholder="lookup_order"
            value={form.callable_name}
            onChange={(e) => onChange({ callable_name: e.target.value })}
          />
          <p className="text-xs text-gray-400">The function or class name exported from the module.</p>
        </div>
      </div>
      <div className="rounded-lg border border-blue-200 bg-blue-50 p-3">
        <p className="text-xs text-blue-700">
          <strong>Tip:</strong> The callable will be validated when you test it in the Playground step.
          Make sure the module is importable from the Caliber runtime environment.
        </p>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Step 3: Schema                                                     */
/* ------------------------------------------------------------------ */

const SCHEMA_TYPES: SchemaProperty["type"][] = ["string", "number", "integer", "boolean", "array", "object"];

function SchemaBuilder({
  label,
  testIdPrefix,
  properties,
  rawJson,
  useRaw,
  onChangeProperties,
  onChangeRaw,
  onToggleRaw,
}: {
  label: string;
  testIdPrefix: string;
  properties: SchemaProperty[];
  rawJson: string;
  useRaw: boolean;
  onChangeProperties: (props: SchemaProperty[]) => void;
  onChangeRaw: (raw: string) => void;
  onToggleRaw: () => void;
}): JSX.Element {
  const addProperty = () => {
    onChangeProperties([...properties, { id: generateId(), name: "", type: "string", description: "", required: false }]);
  };
  const removeProperty = (id: string) => {
    onChangeProperties(properties.filter((p) => p.id !== id));
  };
  const updateProperty = (id: string, patch: Partial<SchemaProperty>) => {
    onChangeProperties(properties.map((p) => (p.id === id ? { ...p, ...patch } : p)));
  };

  const rawError = useMemo(() => {
    if (!useRaw) return null;
    try {
      JSON.parse(rawJson);
      return null;
    } catch {
      return "Invalid JSON";
    }
  }, [useRaw, rawJson]);

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <Label>{label}</Label>
        <button
          type="button"
          data-testid={`${testIdPrefix}-toggle-raw`}
          className="text-xs text-caliber-purple hover:underline"
          onClick={onToggleRaw}
        >
          {useRaw ? "Visual editor" : "Raw JSON"}
        </button>
      </div>

      {useRaw ? (
        <div className="space-y-1">
          <Textarea
            data-testid={`${testIdPrefix}-raw`}
            className="font-mono text-xs"
            rows={6}
            value={rawJson}
            onChange={(e) => onChangeRaw(e.target.value)}
          />
          {rawError && <p className="text-xs text-red-500">{rawError}</p>}
        </div>
      ) : (
        <div className="space-y-2">
          {properties.map((p) => (
            <div key={p.id} data-testid={`${testIdPrefix}-prop-${p.id}`} className="flex items-start gap-2 rounded-lg border border-surface-200 bg-surface-50 p-2">
              <Input
                data-testid={`${testIdPrefix}-prop-name-${p.id}`}
                className="w-32 text-xs"
                placeholder="name"
                value={p.name}
                onChange={(e) => updateProperty(p.id, { name: e.target.value })}
              />
              <select
                data-testid={`${testIdPrefix}-prop-type-${p.id}`}
                aria-label={`Type for ${p.name || "property"}`}
                className="h-9 rounded-md border border-surface-200 bg-white px-2 text-xs"
                value={p.type}
                onChange={(e) => updateProperty(p.id, { type: e.target.value as SchemaProperty["type"] })}
              >
                {SCHEMA_TYPES.map((t) => (
                  <option key={t} value={t}>{t}</option>
                ))}
              </select>
              <Input
                className="flex-1 text-xs"
                placeholder="description"
                value={p.description}
                onChange={(e) => updateProperty(p.id, { description: e.target.value })}
              />
              <label className="flex items-center gap-1 text-xs text-gray-500 whitespace-nowrap">
                <input
                  type="checkbox"
                  data-testid={`${testIdPrefix}-prop-req-${p.id}`}
                  checked={p.required}
                  onChange={(e) => updateProperty(p.id, { required: e.target.checked })}
                  className="h-3.5 w-3.5 rounded border-gray-300"
                />
                req
              </label>
              <button
                type="button"
                onClick={() => removeProperty(p.id)}
                className="mt-1.5 text-gray-400 hover:text-red-500"
                aria-label="Remove property"
              >
                <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
            </div>
          ))}
          <button
            type="button"
            data-testid={`${testIdPrefix}-add-prop`}
            onClick={addProperty}
            className="flex items-center gap-1 text-xs font-medium text-caliber-purple hover:underline"
          >
            <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
            </svg>
            Add property
          </button>
        </div>
      )}
    </div>
  );
}

function SchemaStep({
  form,
  onChange,
}: {
  form: WizardFormData;
  onChange: (patch: Partial<WizardFormData>) => void;
}): JSX.Element {
  return (
    <div data-testid="step-schema" className="space-y-6">
      <div>
        <h2 className="text-lg font-semibold text-gray-900">Define the schema</h2>
        <p className="text-sm text-gray-500">
          Describe what the tool accepts and returns. This helps agents construct correct calls.
        </p>
      </div>
      <SchemaBuilder
        label="Input Schema"
        testIdPrefix="input-schema"
        properties={form.inputProperties}
        rawJson={form.inputSchemaRaw}
        useRaw={form.useRawInput}
        onChangeProperties={(props) => onChange({ inputProperties: props })}
        onChangeRaw={(raw) => onChange({ inputSchemaRaw: raw })}
        onToggleRaw={() => {
          if (form.useRawInput) {
            // Switching back to visual — parse raw JSON
            try {
              const parsed = JSON.parse(form.inputSchemaRaw);
              onChange({ useRawInput: false, inputProperties: schemaToProperties(parsed) });
            } catch {
              onChange({ useRawInput: false });
            }
          } else {
            // Switching to raw — serialize current props
            const schema = propertiesToSchema(form.inputProperties);
            onChange({ useRawInput: true, inputSchemaRaw: JSON.stringify(schema ?? {}, null, 2) });
          }
        }}
      />
      <div className="border-t border-surface-200" />
      <SchemaBuilder
        label="Output Schema"
        testIdPrefix="output-schema"
        properties={form.outputProperties}
        rawJson={form.outputSchemaRaw}
        useRaw={form.useRawOutput}
        onChangeProperties={(props) => onChange({ outputProperties: props })}
        onChangeRaw={(raw) => onChange({ outputSchemaRaw: raw })}
        onToggleRaw={() => {
          if (form.useRawOutput) {
            try {
              const parsed = JSON.parse(form.outputSchemaRaw);
              onChange({ useRawOutput: false, outputProperties: schemaToProperties(parsed) });
            } catch {
              onChange({ useRawOutput: false });
            }
          } else {
            const schema = propertiesToSchema(form.outputProperties);
            onChange({ useRawOutput: true, outputSchemaRaw: JSON.stringify(schema ?? {}, null, 2) });
          }
        }}
      />
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Step 4: Playground                                                 */
/* ------------------------------------------------------------------ */

function PlaygroundStep({
  form,
  registeredToolId,
}: {
  form: WizardFormData;
  registeredToolId: string | null;
}): JSX.Element {
  const inputSchema = buildInputSchema(form);
  const fields = inputSchema ? schemaToProperties(inputSchema) : [];
  const [testInput, setTestInput] = useState<Record<string, string>>({});
  const [result, setResult] = useState<ToolTestRunResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  const runTest = useCallback(async () => {
    if (!registeredToolId) {
      setError("Tool must be registered first. Complete and submit the wizard to enable playground testing.");
      return;
    }
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const input: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(testInput)) {
        input[k] = v;
      }
      const res = await caliberApi.testRunTool(registeredToolId, input);
      setResult(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRunning(false);
    }
  }, [registeredToolId, testInput]);

  return (
    <div data-testid="step-playground" className="space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-gray-900">Test your tool</h2>
        <p className="text-sm text-gray-500">
          Run a sandbox-isolated test. Write and external-action tools are automatically mocked.
        </p>
      </div>

      {!registeredToolId && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-4">
          <p className="text-sm text-amber-700">
            <strong>Playground available after registration.</strong> Complete the wizard to register the tool,
            then test it from the Tool Detail page.
          </p>
        </div>
      )}

      {registeredToolId && (
        <>
          {/* Input form */}
          <div className="rounded-lg border border-surface-200 p-4">
            <Label className="mb-3 block text-sm font-medium text-gray-700">Test Input</Label>
            {fields.length === 0 ? (
              <p className="text-xs text-gray-400">No input schema defined. The tool will be called with no arguments.</p>
            ) : (
              <div className="space-y-3">
                {fields.map((f) => (
                  <div key={f.id} className="space-y-1">
                    <Label className="text-xs">
                      {f.name}
                      {f.required && <span className="ml-1 text-red-400">*</span>}
                      <span className="ml-2 text-gray-400">{f.type}</span>
                    </Label>
                    <Input
                      data-testid={`playground-input-${f.name}`}
                      placeholder={f.description || f.name}
                      value={testInput[f.name] ?? ""}
                      onChange={(e) => setTestInput((prev) => ({ ...prev, [f.name]: e.target.value }))}
                    />
                  </div>
                ))}
              </div>
            )}
            <Button
              data-testid="playground-run"
              className="mt-4"
              size="sm"
              disabled={running}
              onClick={runTest}
            >
              {running ? (
                <>
                  <svg className="mr-1.5 h-3.5 w-3.5 animate-spin" viewBox="0 0 24 24" fill="none">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                  </svg>
                  Running…
                </>
              ) : (
                "Run Test"
              )}
            </Button>
          </div>

          {/* Results */}
          {error && (
            <div data-testid="playground-error" className="rounded-lg border border-red-200 bg-red-50 p-3">
              <p className="text-sm font-medium text-red-700">Error</p>
              <p className="text-xs text-red-600">{error}</p>
            </div>
          )}

          {result && (
            <div data-testid="playground-result" className="space-y-3">
              <div className="flex items-center gap-2">
                <Badge variant={result.error ? "destructive" : "success"}>
                  {result.error ? "Failed" : "Success"}
                </Badge>
                <Badge variant={result.mocked ? "warning" : "default"}>
                  {result.mocked ? "Sandboxed (mocked)" : "Live execution"}
                </Badge>
                <span className="text-xs text-gray-400">{result.duration_ms}ms</span>
              </div>

              {result.error && (
                <div className="rounded-lg border border-red-200 bg-red-50 p-3">
                  <p className="text-xs font-mono text-red-600 whitespace-pre-wrap">{result.error}</p>
                </div>
              )}

              <div>
                <Label className="mb-1 block text-xs font-medium text-gray-500">Output</Label>
                <pre
                  data-testid="playground-output"
                  className="max-h-64 overflow-auto rounded-lg border border-surface-200 bg-surface-50 p-3 text-xs font-mono"
                >
                  {JSON.stringify(result.output, null, 2)}
                </pre>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Step 5: Safety & Review                                            */
/* ------------------------------------------------------------------ */

const SIDE_EFFECT_OPTIONS = [
  { value: "read", label: "Read", icon: "🟢", desc: "No side effects — only reads data" },
  { value: "write", label: "Write", icon: "🟡", desc: "Modifies data in internal systems" },
  { value: "external_action", label: "External Action", icon: "🔴", desc: "Sends data to external services" },
] as const;

function SafetyReviewStep({
  form,
  onChange,
}: {
  form: WizardFormData;
  onChange: (patch: Partial<WizardFormData>) => void;
}): JSX.Element {
  const [secretInput, setSecretInput] = useState("");

  const addSecret = () => {
    const trimmed = secretInput.trim();
    if (trimmed && !form.secret_refs.includes(trimmed)) {
      onChange({ secret_refs: [...form.secret_refs, trimmed] });
      setSecretInput("");
    }
  };

  const removeSecret = (s: string) => {
    onChange({ secret_refs: form.secret_refs.filter((r) => r !== s) });
  };

  return (
    <div data-testid="step-safety" className="space-y-6">
      <div>
        <h2 className="text-lg font-semibold text-gray-900">Safety & Review</h2>
        <p className="text-sm text-gray-500">Configure side effects, approval requirements, and review your tool before registering.</p>
      </div>

      {/* Side-effect level */}
      <div className="space-y-2">
        <Label>Side Effect Level</Label>
        <div className="grid gap-2 sm:grid-cols-3">
          {SIDE_EFFECT_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              type="button"
              data-testid={`wiz-side-effect-${opt.value}`}
              onClick={() => onChange({ side_effect_level: opt.value })}
              className={`rounded-lg border p-3 text-left transition-all ${
                form.side_effect_level === opt.value
                  ? "border-caliber-purple bg-caliber-purple/5 ring-1 ring-caliber-purple/30"
                  : "border-surface-200 hover:border-gray-300"
              }`}
            >
              <div className="flex items-center gap-2">
                <span className="text-lg">{opt.icon}</span>
                <span className="text-sm font-medium text-gray-900">{opt.label}</span>
              </div>
              <p className="mt-1 text-xs text-gray-500">{opt.desc}</p>
            </button>
          ))}
        </div>
      </div>

      {/* Toggles */}
      <div className="space-y-3">
        <label className="flex cursor-pointer items-center justify-between rounded-lg border border-surface-200 p-3">
          <div>
            <p className="text-sm font-medium text-gray-900">Requires Approval</p>
            <p className="text-xs text-gray-500">Pause for human approval before each execution</p>
          </div>
          <input
            type="checkbox"
            data-testid="wiz-requires-approval"
            checked={form.requires_approval}
            onChange={(e) => onChange({ requires_approval: e.target.checked })}
            className="h-4 w-4 rounded border-gray-300 text-caliber-purple focus:ring-caliber-purple/40"
          />
        </label>
        <label className="flex cursor-pointer items-center justify-between rounded-lg border border-surface-200 p-3">
          <div>
            <p className="text-sm font-medium text-gray-900">Allow in Preview</p>
            <p className="text-xs text-gray-500">Run the real callable in sandbox/preview mode (read-only tools)</p>
          </div>
          <input
            type="checkbox"
            data-testid="wiz-allow-preview"
            checked={form.allow_in_preview}
            onChange={(e) => onChange({ allow_in_preview: e.target.checked })}
            className="h-4 w-4 rounded border-gray-300 text-caliber-purple focus:ring-caliber-purple/40"
          />
        </label>
      </div>

      {/* Secret refs */}
      <div className="space-y-2">
        <Label>Secret References</Label>
        <div className="flex gap-2">
          <Input
            data-testid="wiz-secret-input"
            className="flex-1"
            placeholder="e.g. STRIPE_API_KEY"
            value={secretInput}
            onKeyDown={(e) => e.key === "Enter" && (e.preventDefault(), addSecret())}
            onChange={(e) => setSecretInput(e.target.value)}
          />
          <Button type="button" variant="outline" size="sm" data-testid="wiz-add-secret" onClick={addSecret}>
            Add
          </Button>
        </div>
        {form.secret_refs.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {form.secret_refs.map((s) => (
              <Badge key={s} variant="secondary" className="gap-1">
                {s}
                <button type="button" onClick={() => removeSecret(s)} className="text-gray-400 hover:text-red-500" aria-label={`Remove ${s}`}>
                  ×
                </button>
              </Badge>
            ))}
          </div>
        )}
      </div>

      {/* Review summary */}
      <div className="rounded-lg border border-surface-200 bg-surface-50 p-4">
        <h3 className="mb-3 text-sm font-semibold text-gray-700">Review Summary</h3>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs" data-testid="review-summary">
          <dt className="text-gray-500">Name</dt>
          <dd className="font-medium text-gray-900">{form.name || "—"}</dd>
          <dt className="text-gray-500">Version</dt>
          <dd className="font-medium text-gray-900">{form.version}</dd>
          <dt className="text-gray-500">Owner</dt>
          <dd className="font-medium text-gray-900">{form.owner || "—"}</dd>
          <dt className="text-gray-500">Module</dt>
          <dd className="font-mono font-medium text-gray-900">{form.module_path}</dd>
          <dt className="text-gray-500">Callable</dt>
          <dd className="font-mono font-medium text-gray-900">{form.callable_name || "—"}</dd>
          <dt className="text-gray-500">Side Effects</dt>
          <dd className="font-medium text-gray-900">
            {SIDE_EFFECT_OPTIONS.find((o) => o.value === form.side_effect_level)?.icon}{" "}
            {form.side_effect_level}
          </dd>
          <dt className="text-gray-500">Approval</dt>
          <dd className="font-medium text-gray-900">{form.requires_approval ? "Required" : "Not required"}</dd>
          <dt className="text-gray-500">Preview</dt>
          <dd className="font-medium text-gray-900">{form.allow_in_preview ? "Allowed" : "Mocked"}</dd>
          {form.secret_refs.length > 0 && (
            <>
              <dt className="text-gray-500">Secrets</dt>
              <dd className="font-medium text-gray-900">{form.secret_refs.join(", ")}</dd>
            </>
          )}
        </dl>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

export function buildInputSchema(form: WizardFormData): Record<string, unknown> | null {
  if (form.useRawInput) {
    try {
      const parsed = JSON.parse(form.inputSchemaRaw);
      return Object.keys(parsed).length > 0 ? parsed : null;
    } catch {
      return null;
    }
  }
  return propertiesToSchema(form.inputProperties);
}

export function buildOutputSchema(form: WizardFormData): Record<string, unknown> | null {
  if (form.useRawOutput) {
    try {
      const parsed = JSON.parse(form.outputSchemaRaw);
      return Object.keys(parsed).length > 0 ? parsed : null;
    } catch {
      return null;
    }
  }
  return propertiesToSchema(form.outputProperties);
}

/* ------------------------------------------------------------------ */
/*  Main Wizard                                                        */
/* ------------------------------------------------------------------ */

export function ToolWizard({ onClose }: { onClose: () => void }): JSX.Element {
  const [step, setStep] = useState(0);
  const [form, setForm] = useState<WizardFormData>(INITIAL_FORM);
  const [registeredTool, setRegisteredTool] = useState<ToolDefinition | null>(null);
  const navigate = useNavigate();
  const invalidate = useInvalidate();

  const onChange = useCallback(
    (patch: Partial<WizardFormData>) => setForm((prev) => ({ ...prev, ...patch })),
    [],
  );

  const isStepValid = useCallback(
    (s: number): boolean => {
      switch (s) {
        case 0:
          return form.name.trim().length > 0;
        case 1:
          return form.module_path.trim().length > 0 && form.callable_name.trim().length > 0;
        case 2:
          return true; // schema is optional
        case 3:
          return true; // playground is optional
        case 4:
          return true; // review
        default:
          return false;
      }
    },
    [form],
  );

  const registerMut = useApiMutation(
    () =>
      caliberApi.registerTool({
        name: form.name.trim(),
        version: form.version.trim() || "1.0",
        description: form.description.trim(),
        module_path: form.module_path.trim(),
        callable_name: form.callable_name.trim(),
        input_schema: buildInputSchema(form),
        output_schema: buildOutputSchema(form),
        side_effect_level: form.side_effect_level,
        requires_approval: form.requires_approval,
        allow_in_preview: form.allow_in_preview,
        secret_refs: form.secret_refs,
        owner: form.owner.trim(),
      }),
    {
      onSuccess: (tool) => {
        setRegisteredTool(tool);
        invalidate(["tools", "all"]);
        navigate(`/tools/${tool.tool_id}`);
      },
    },
  );

  const goNext = () => {
    if (step < STEPS.length - 1) setStep(step + 1);
  };
  const goBack = () => {
    if (step > 0) setStep(step - 1);
  };

  return (
    <div data-testid="tool-wizard" className="mx-auto max-w-2xl">
      {/* Header */}
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-xl font-semibold text-gray-900">Register New Tool</h1>
        <button
          type="button"
          data-testid="wizard-close"
          onClick={onClose}
          className="text-gray-400 hover:text-gray-600"
          aria-label="Close wizard"
        >
          <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
      </div>

      <StepIndicator current={step} onGoTo={setStep} />

      {/* Step content */}
      <div className="min-h-[340px]">
        {step === 0 && <IdentityStep form={form} onChange={onChange} />}
        {step === 1 && <ImplementationStep form={form} onChange={onChange} />}
        {step === 2 && <SchemaStep form={form} onChange={onChange} />}
        {step === 3 && <PlaygroundStep form={form} registeredToolId={registeredTool?.tool_id ?? null} />}
        {step === 4 && <SafetyReviewStep form={form} onChange={onChange} />}
      </div>

      {/* Navigation */}
      <div className="mt-8 flex items-center justify-between border-t border-surface-200 pt-4">
        <Button
          type="button"
          variant="outline"
          data-testid="wizard-back"
          onClick={step === 0 ? onClose : goBack}
        >
          {step === 0 ? "Cancel" : "Back"}
        </Button>
        <div className="flex gap-2">
          {step === STEPS.length - 1 ? (
            <Button
              type="button"
              data-testid="wizard-submit"
              disabled={!isStepValid(0) || !isStepValid(1) || registerMut.isPending}
              onClick={() => registerMut.mutate(undefined)}
            >
              {registerMut.isPending ? "Registering…" : "Register Tool"}
            </Button>
          ) : (
            <Button
              type="button"
              data-testid="wizard-next"
              disabled={!isStepValid(step)}
              onClick={goNext}
            >
              Next
            </Button>
          )}
        </div>
      </div>

      {registerMut.error && (
        <div data-testid="wizard-error" className="mt-3 rounded-lg border border-red-200 bg-red-50 p-3">
          <p className="text-sm text-red-600">{registerMut.error.message}</p>
        </div>
      )}
    </div>
  );
}
