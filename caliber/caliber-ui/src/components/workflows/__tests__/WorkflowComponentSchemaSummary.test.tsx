import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type {
  WorkflowComponent,
  WorkflowComponentField,
  WorkflowComponentSetupCheck,
} from "@/api/workflowTypes";
import { WorkflowComponentSchemaSummary } from "@/components/workflows/WorkflowComponentSchemaSummary";

function baseComponent(overrides: Partial<WorkflowComponent> = {}): WorkflowComponent {
  return {
    type: "agent",
    label: "Agent",
    category: "Orchestration",
    description: "Base agent component.",
    docs: [],
    default_inputs: {},
    default_outputs: {},
    fields: [],
    setup_checks: [],
    ...overrides,
  };
}

function field(overrides: Partial<WorkflowComponentField> = {}): WorkflowComponentField {
  return {
    key: "f1",
    label: "Field One",
    type: "string",
    required: true,
    default: "",
    description: "A field.",
    constraints: {},
    examples: [],
    ...overrides,
  };
}

describe("WorkflowComponentSchemaSummary", () => {
  it("renders header metadata, docs list, and starter ports", () => {
    render(
      <WorkflowComponentSchemaSummary
        component={baseComponent({
          category: "Inputs & Outputs",
          description: "Reads a single file.",
          docs: ["First doc line.", "Second doc line."],
          default_inputs: { path: { type: "string" } },
          default_outputs: { text: { type: "string" }, meta: { type: "structured" } },
        })}
      />,
    );

    expect(screen.getByText("Runtime schema")).toBeInTheDocument();
    expect(screen.getByText("Inputs & Outputs")).toBeInTheDocument();
    expect(screen.getByText("Reads a single file.")).toBeInTheDocument();
    expect(screen.getByText("First doc line.")).toBeInTheDocument();
    expect(screen.getByText("Second doc line.")).toBeInTheDocument();
    expect(screen.getByText("path")).toBeInTheDocument();
    expect(screen.getByText("text")).toBeInTheDocument();
    expect(screen.getByText("meta")).toBeInTheDocument();
  });

  it("omits the docs block entirely when docs is empty", () => {
    const { container } = render(
      <WorkflowComponentSchemaSummary
        component={baseComponent({ docs: [] })}
      />,
    );
    // No stray doc paragraphs beyond the description itself.
    expect(container.querySelectorAll(".text-sky-900\\/90").length).toBe(0);
  });

  it("shows every constraint token, examples, and formats varied default value types", () => {
    const circular: Record<string, unknown> = { self: null };
    circular.self = circular; // JSON.stringify throws -> exercises formatValue's catch branch

    render(
      <WorkflowComponentSchemaSummary
        component={baseComponent({
          fields: [
            field({
              key: "constrained",
              label: "Constrained Field",
              default: circular,
              constraints: {
                minimum: 1,
                maximum: 10,
                min_length: 2,
                max_length: 20,
                min_items: 1,
                max_items: 5,
                pattern: "^[a-z]+$",
                multiple_of: 2,
                nullable: true,
                options: ["a", "b", "c", "d"],
              },
              examples: ["example-str", 42, true, null],
            }),
          ],
        })}
      />,
    );

    const fieldRow = screen.getByTestId("workflow-component-field-constrained");
    expect(fieldRow).toHaveTextContent("min 1");
    expect(fieldRow).toHaveTextContent("max 10");
    expect(fieldRow).toHaveTextContent("min length 2");
    expect(fieldRow).toHaveTextContent("max length 20");
    expect(fieldRow).toHaveTextContent("min items 1");
    expect(fieldRow).toHaveTextContent("max items 5");
    expect(fieldRow).toHaveTextContent("pattern ^[a-z]+$");
    expect(fieldRow).toHaveTextContent("step 2");
    expect(fieldRow).toHaveTextContent("nullable");
    // Options preview truncates after 3 with an ellipsis.
    expect(fieldRow).toHaveTextContent("options a, b, c…");
    // Circular default falls back to String(value) since JSON.stringify throws.
    expect(fieldRow).toHaveTextContent("Default [object Object]");
    // Examples: string, number, boolean, and null/empty formatting.
    expect(fieldRow).toHaveTextContent("Example example-str");
    expect(fieldRow).toHaveTextContent("Example 42");
    expect(fieldRow).toHaveTextContent("Example true");
    expect(fieldRow).toHaveTextContent("Example —");
  });

  it("tolerates a missing constraints object and does not add an ellipsis for three or fewer options", () => {
    const fieldWithoutConstraints = field({
      key: "no-constraints",
      constraints: undefined as unknown as Record<string, unknown>,
    });
    const fieldWithFewOptions = field({
      key: "few-options",
      constraints: { options: ["x", "y"] },
    });

    render(
      <WorkflowComponentSchemaSummary
        component={baseComponent({
          fields: [fieldWithoutConstraints, fieldWithFewOptions],
        })}
      />,
    );

    // No constraints -> no constraint token badges beyond the Default badge.
    const noConstraintsRow = screen.getByTestId(
      "workflow-component-field-no-constraints",
    );
    expect(noConstraintsRow).not.toHaveTextContent("options");

    const fewOptionsRow = screen.getByTestId("workflow-component-field-few-options");
    expect(fewOptionsRow).toHaveTextContent("options x, y");
    expect(fewOptionsRow).not.toHaveTextContent("options x, y…");
  });

  it("renders required vs optional badges and skips the description when absent", () => {
    render(
      <WorkflowComponentSchemaSummary
        component={baseComponent({
          fields: [
            field({ key: "req", label: "Required Field", required: true, description: "" }),
            field({ key: "opt", label: "Optional Field", required: false, description: undefined }),
          ],
        })}
      />,
    );

    expect(screen.getByTestId("workflow-component-field-req")).toHaveTextContent(
      "Required",
    );
    expect(screen.getByTestId("workflow-component-field-opt")).toHaveTextContent(
      "Optional",
    );
  });

  it("shows a fallback message when there are no config fields", () => {
    render(<WorkflowComponentSchemaSummary component={baseComponent({ fields: [] })} />);
    expect(
      screen.getByText(
        "This component does not introduce extra node-specific configuration fields beyond its ports and common workflow wiring.",
      ),
    ).toBeInTheDocument();
  });

  it("shows a fallback message when there are no setup rules (including an undefined setup_checks)", () => {
    const { rerender } = render(
      <WorkflowComponentSchemaSummary
        component={baseComponent({ setup_checks: [] })}
      />,
    );
    expect(
      screen.getByText(
        "No component-specific setup rules are defined beyond the field defaults and constraints shown here.",
      ),
    ).toBeInTheDocument();

    const withoutSetupChecks = baseComponent();
    delete (withoutSetupChecks as { setup_checks?: WorkflowComponentSetupCheck[] }).setup_checks;
    rerender(<WorkflowComponentSchemaSummary component={withoutSetupChecks} />);
    expect(
      screen.getByText(
        "No component-specific setup rules are defined beyond the field defaults and constraints shown here.",
      ),
    ).toBeInTheDocument();
  });

  it("humanizes every known setup-check kind and falls back to the raw kind for unknown ones", () => {
    const kinds: Array<{ kind: WorkflowComponentSetupCheck["kind"]; expected: string; minimum?: number }> = [
      { kind: "non_empty_string", expected: "Non-empty text" },
      { kind: "non_empty_list", expected: "At least one item" },
      { kind: "any_non_empty", expected: "Any configured input" },
      { kind: "instructions_present", expected: "Instructions present" },
      { kind: "minimum_number", expected: "Minimum 3", minimum: 3 },
      { kind: "minimum_number", expected: "Minimum 0" },
      {
        kind: "minimum_outgoing_edges",
        expected: "At least 1 downstream edge",
        minimum: 1,
      },
      {
        kind: "minimum_outgoing_edges",
        expected: "At least 2 downstream edges",
        minimum: 2,
      },
      {
        kind: "minimum_outgoing_edges",
        expected: "At least 0 downstream edges",
      },
      {
        kind: "minimum_incoming_edges",
        expected: "At least 1 upstream edge",
        minimum: 1,
      },
      {
        kind: "minimum_incoming_edges",
        expected: "At least 3 upstream edges",
        minimum: 3,
      },
      {
        kind: "minimum_incoming_edges",
        expected: "At least 0 upstream edges",
      },
      {
        kind: "distinct_incoming_target_ports",
        expected: "Distinct incoming ports",
      },
      {
        kind: "target_node_executable_if_set",
        expected: "Executable target when set",
      },
      {
        kind: "not_current_workflow_id",
        expected: "Different workflow than current",
      },
      {
        kind: "router_branch_edges_connected",
        expected: "Branch targets connected",
      },
      { kind: "some_future_unmapped_kind", expected: "some_future_unmapped_kind" },
    ];

    kinds.forEach(({ kind, expected, minimum }, index) => {
      const { unmount } = render(
        <WorkflowComponentSchemaSummary
          component={baseComponent({
            setup_checks: [
              {
                label: `Rule ${index}`,
                help: "help text",
                kind,
                minimum,
              },
            ],
          })}
        />,
      );
      expect(screen.getByTestId("workflow-component-setup-check-0")).toHaveTextContent(
        expected,
      );
      unmount();
    });
  });

  it("resolves setup-rule scope labels from a single field, multiple fields, or neither", () => {
    const componentWithFields = baseComponent({
      fields: [
        field({ key: "path", label: "Path" }),
        field({ key: "name", label: "Name" }),
      ],
      setup_checks: [
        {
          label: "Single-field rule",
          help: "help",
          kind: "non_empty_string",
          field: "path",
        },
        {
          label: "Multi-field rule",
          help: "help",
          kind: "any_non_empty",
          fields: ["path", "name"],
        },
        {
          label: "Unscoped rule",
          help: "help",
          kind: "instructions_present",
        },
      ],
    });

    render(<WorkflowComponentSchemaSummary component={componentWithFields} />);

    expect(screen.getByTestId("workflow-component-setup-check-0")).toHaveTextContent(
      "Targets Path",
    );
    expect(screen.getByTestId("workflow-component-setup-check-1")).toHaveTextContent(
      "Targets Path, Name",
    );
    expect(
      screen.getByTestId("workflow-component-setup-check-2"),
    ).not.toHaveTextContent("Targets");
  });

  it("falls back to the raw field key in the scope label when the field isn't in the schema", () => {
    render(
      <WorkflowComponentSchemaSummary
        component={baseComponent({
          fields: [],
          setup_checks: [
            {
              label: "Orphan rule",
              help: "help",
              kind: "non_empty_string",
              field: "missing_field",
            },
          ],
        })}
      />,
    );
    expect(screen.getByTestId("workflow-component-setup-check-0")).toHaveTextContent(
      "Targets missing_field",
    );
  });

  it("explains every intentional empty-port contract by component type and side", () => {
    const cases: Array<{
      type: WorkflowComponent["type"];
      inputsMessage: string;
      outputsMessage: string;
    }> = [
      {
        type: "join",
        inputsMessage:
          "Join nodes accept one inbound edge per branch instead of named starter inputs. Wire the upstream branches directly into the join barrier.",
        outputsMessage:
          "No starter ports are defined for this component. Add explicit ports in the manifest when wiring it into a workflow.",
      },
      {
        type: "router",
        inputsMessage:
          "No starter ports are defined for this component. Add explicit ports in the manifest when wiring it into a workflow.",
        outputsMessage:
          "Router branches are modeled as outgoing edges, so there are no named starter outputs. Add branch destinations to define each control-flow path.",
      },
      {
        type: "output",
        inputsMessage:
          "No starter ports are defined for this component. Add explicit ports in the manifest when wiring it into a workflow.",
        outputsMessage:
          "Output nodes end the workflow response and do not emit downstream ports.",
      },
      {
        type: "note",
        inputsMessage:
          "Note nodes are canvas annotations only and do not participate in runtime data flow.",
        outputsMessage:
          "Note nodes are canvas annotations only and do not participate in runtime data flow.",
      },
      {
        type: "agent",
        inputsMessage:
          "No starter ports are defined for this component. Add explicit ports in the manifest when wiring it into a workflow.",
        outputsMessage:
          "No starter ports are defined for this component. Add explicit ports in the manifest when wiring it into a workflow.",
      },
    ];

    cases.forEach(({ type, inputsMessage, outputsMessage }) => {
      const { unmount } = render(
        <WorkflowComponentSchemaSummary
          component={baseComponent({ type, default_inputs: {}, default_outputs: {} })}
        />,
      );
      if (inputsMessage === outputsMessage) {
        expect(screen.getAllByText(inputsMessage)).toHaveLength(2);
      } else {
        expect(screen.getByText(inputsMessage)).toBeInTheDocument();
        expect(screen.getByText(outputsMessage)).toBeInTheDocument();
      }
      unmount();
    });
  });
});
