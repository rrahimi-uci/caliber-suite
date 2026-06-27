import type { PortSpec, WorkflowComponent, WorkflowComponentField } from "@/api/workflowTypes";
import { portColor } from "@/lib/workflowGraph";

function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function constraintTokens(field: WorkflowComponentField): string[] {
  const constraints = field.constraints ?? {};
  const tokens: string[] = [];
  if (typeof constraints.minimum === "number") tokens.push(`min ${constraints.minimum}`);
  if (typeof constraints.maximum === "number") tokens.push(`max ${constraints.maximum}`);
  if (typeof constraints.min_length === "number") tokens.push(`min length ${constraints.min_length}`);
  if (typeof constraints.max_length === "number") tokens.push(`max length ${constraints.max_length}`);
  if (typeof constraints.min_items === "number") tokens.push(`min items ${constraints.min_items}`);
  if (typeof constraints.max_items === "number") tokens.push(`max items ${constraints.max_items}`);
  if (typeof constraints.pattern === "string" && constraints.pattern) tokens.push(`pattern ${constraints.pattern}`);
  if (typeof constraints.multiple_of === "number") tokens.push(`step ${constraints.multiple_of}`);
  if (constraints.nullable === true) tokens.push("nullable");
  if (Array.isArray(constraints.options) && constraints.options.length > 0) {
    const preview = constraints.options.slice(0, 3).map((item) => String(item)).join(", ");
    tokens.push(`options ${preview}${constraints.options.length > 3 ? "…" : ""}`);
  }
  return tokens;
}

function setupRuleKindLabel(rule: NonNullable<WorkflowComponent["setup_checks"]>[number]): string {
  switch (rule.kind) {
    case "non_empty_string":
      return "Non-empty text";
    case "non_empty_list":
      return "At least one item";
    case "any_non_empty":
      return "Any configured input";
    case "instructions_present":
      return "Instructions present";
    case "minimum_number":
      return `Minimum ${typeof rule.minimum === "number" ? rule.minimum : 0}`;
    case "minimum_outgoing_edges":
      return `At least ${typeof rule.minimum === "number" ? rule.minimum : 0} downstream edge${rule.minimum === 1 ? "" : "s"}`;
    case "minimum_incoming_edges":
      return `At least ${typeof rule.minimum === "number" ? rule.minimum : 0} upstream edge${rule.minimum === 1 ? "" : "s"}`;
    case "distinct_incoming_target_ports":
      return "Distinct incoming ports";
    case "target_node_executable_if_set":
      return "Executable target when set";
    case "not_current_workflow_id":
      return "Different workflow than current";
    case "router_branch_edges_connected":
      return "Branch targets connected";
    default:
      return rule.kind;
  }
}

function setupRuleScopeLabel(
  rule: NonNullable<WorkflowComponent["setup_checks"]>[number],
  fieldsByKey: ReadonlyMap<string, WorkflowComponentField>,
): string | null {
  const fieldKeys = [
    ...(rule.field ? [rule.field] : []),
    ...(rule.fields ?? []),
  ].filter(Boolean);
  if (fieldKeys.length === 0) return null;
  const labels = fieldKeys.map((fieldKey) => fieldsByKey.get(fieldKey)?.label ?? fieldKey);
  return labels.join(", ");
}

function emptyPortMessage(
  componentType: WorkflowComponent["type"],
  side: "inputs" | "outputs",
): string {
  if (componentType === "join" && side === "inputs") {
    return "Join nodes accept one inbound edge per branch instead of named starter inputs. Wire the upstream branches directly into the join barrier.";
  }
  if (componentType === "router" && side === "outputs") {
    return "Router branches are modeled as outgoing edges, so there are no named starter outputs. Add branch destinations to define each control-flow path.";
  }
  if (componentType === "output" && side === "outputs") {
    return "Output nodes end the workflow response and do not emit downstream ports.";
  }
  if (componentType === "note") {
    return "Note nodes are canvas annotations only and do not participate in runtime data flow.";
  }
  return "No starter ports are defined for this component. Add explicit ports in the manifest when wiring it into a workflow.";
}

function PortList({
  title,
  ports,
  emptyState,
}: {
  title: string;
  ports: Record<string, PortSpec>;
  emptyState: string;
}): JSX.Element {
  const entries = Object.entries(ports);
  return (
    <div className="rounded-xl border border-slate-200 bg-white">
      <div className="border-b border-slate-100 px-3 py-2 text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-500">
        {title}
      </div>
      {entries.length > 0 ? (
        <div className="space-y-2 px-3 py-3">
          {entries.map(([name, spec]) => (
            <div key={`${title}-${name}`} className="flex items-center gap-2 text-xs text-slate-600">
              <span
                className="inline-flex h-2.5 w-2.5 shrink-0 rounded-full"
                style={{ backgroundColor: portColor(spec.type) }}
              />
              <span className="font-mono font-medium text-slate-800">{name}</span>
              <span className="ml-auto rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold text-slate-500">
                {spec.type}
              </span>
            </div>
          ))}
        </div>
      ) : (
        <div className="px-3 py-3 text-xs text-slate-500">
          {emptyState}
        </div>
      )}
    </div>
  );
}

export function WorkflowComponentSchemaSummary({
  component,
}: {
  component: WorkflowComponent;
}): JSX.Element {
  const fieldsByKey = new Map(component.fields.map((field) => [field.key, field] as const));
  const setupChecks = component.setup_checks ?? [];

  return (
    <div data-testid="workflow-component-schema-summary" className="space-y-3">
      <div className="rounded-xl border border-sky-200 bg-sky-50 px-3 py-3 text-xs leading-relaxed text-sky-800">
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded-full bg-white px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-sky-700">
            Runtime schema
          </span>
          <span className="rounded-full bg-white px-2 py-0.5 text-[10px] font-semibold text-sky-700">
            {component.category}
          </span>
        </div>
        <div className="mt-2 text-sm font-semibold text-slate-900">{component.description}</div>
        {component.docs.length > 0 && (
          <div className="mt-2 space-y-1 text-[11px] text-sky-900/90">
            {component.docs.map((doc) => (
              <div key={doc}>{doc}</div>
            ))}
          </div>
        )}
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        <PortList
          title="Starter inputs"
          ports={component.default_inputs}
          emptyState={emptyPortMessage(component.type, "inputs")}
        />
        <PortList
          title="Starter outputs"
          ports={component.default_outputs}
          emptyState={emptyPortMessage(component.type, "outputs")}
        />
      </div>

      <div className="rounded-xl border border-slate-200 bg-white">
        <div className="border-b border-slate-100 px-3 py-2 text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-500">
          Validation & setup rules
        </div>
        {setupChecks.length > 0 ? (
          <div className="divide-y divide-slate-100">
            {setupChecks.map((rule, index) => {
              const scope = setupRuleScopeLabel(rule, fieldsByKey);
              return (
                <div
                  key={`${component.type}-setup-${rule.label}-${index}`}
                  data-testid={`workflow-component-setup-check-${index}`}
                  className="px-3 py-3"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-semibold text-slate-900">{rule.label}</span>
                    <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-semibold text-amber-700">
                      {setupRuleKindLabel(rule)}
                    </span>
                    {scope && (
                      <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold text-slate-600">
                        Targets {scope}
                      </span>
                    )}
                  </div>
                  <div className="mt-1 text-xs leading-relaxed text-slate-600">{rule.help}</div>
                </div>
              );
            })}
          </div>
        ) : (
          <div className="px-3 py-3 text-xs text-slate-500">
            No component-specific setup rules are defined beyond the field defaults and constraints shown here.
          </div>
        )}
      </div>

      <div className="rounded-xl border border-slate-200 bg-white">
        <div className="border-b border-slate-100 px-3 py-2 text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-500">
          Config fields
        </div>
        {component.fields.length > 0 ? (
          <div className="divide-y divide-slate-100">
            {component.fields.map((field) => {
              const tokens = constraintTokens(field);
              return (
                <div
                  key={`${component.type}-${field.key}`}
                  data-testid={`workflow-component-field-${field.key}`}
                  className="px-3 py-3"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-semibold text-slate-900">{field.label}</span>
                    <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold text-slate-500">
                      {field.type}
                    </span>
                    <span
                      className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${
                        field.required
                          ? "bg-amber-50 text-amber-700"
                          : "bg-emerald-50 text-emerald-700"
                      }`}
                    >
                      {field.required ? "Required" : "Optional"}
                    </span>
                  </div>
                  {field.description && (
                    <div className="mt-1 text-xs leading-relaxed text-slate-600">{field.description}</div>
                  )}
                  <div className="mt-2 flex flex-wrap gap-2 text-[11px]">
                    <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 font-medium text-slate-600">
                      Default {formatValue(field.default)}
                    </span>
                    {tokens.map((token) => (
                      <span
                        key={`${field.key}-${token}`}
                        className="rounded-full border border-slate-200 bg-white px-2.5 py-1 font-medium text-slate-500"
                      >
                        {token}
                      </span>
                    ))}
                    {field.examples.map((example, index) => (
                      <span
                        key={`${field.key}-example-${index}`}
                        className="rounded-full border border-blue-200 bg-blue-50 px-2.5 py-1 font-medium text-blue-700"
                      >
                        Example {formatValue(example)}
                      </span>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          <div className="px-3 py-3 text-xs text-slate-500">
            This component does not introduce extra node-specific configuration fields beyond its ports and common workflow wiring.
          </div>
        )}
      </div>
    </div>
  );
}
