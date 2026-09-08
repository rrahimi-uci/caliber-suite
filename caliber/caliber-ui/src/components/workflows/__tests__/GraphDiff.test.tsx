import { render, screen } from "@/test/utils";
import { GraphDiff } from "@/components/workflows/GraphDiff";
import type { GraphDiff as GraphDiffData } from "@/api/workflowTypes";

function emptyDiff(overrides: Partial<GraphDiffData> = {}): GraphDiffData {
  return {
    added_nodes: [],
    removed_nodes: [],
    modified_nodes: [],
    added_edges: [],
    removed_edges: [],
    modified_edges: [],
    artifact_changes: [],
    deploy_gate_changes: [],
    empty: false,
    ...overrides,
  };
}

describe("GraphDiff", () => {
  it("renders a 'no changes' message when the diff is marked empty", () => {
    render(<GraphDiff diff={emptyDiff({ empty: true })} />);
    expect(screen.getByText("No graph changes.")).toBeInTheDocument();
    expect(screen.queryByTestId("diff-added-node")).not.toBeInTheDocument();
  });

  it("renders an added node with its type suffix, and without one when type is absent", () => {
    render(
      <GraphDiff
        diff={emptyDiff({
          added_nodes: [{ id: "n1", type: "agent" }, { id: "n2" }],
        })}
      />,
    );
    const rows = screen.getAllByTestId("diff-added-node");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("+ node n1 (agent)");
    expect(rows[1]).toHaveTextContent("+ node n2");
    expect(rows[1]).not.toHaveTextContent("(");
  });

  it("renders removed nodes", () => {
    render(
      <GraphDiff
        diff={emptyDiff({ removed_nodes: [{ id: "n1", type: "tool" }] })}
      />,
    );
    expect(screen.getByTestId("diff-removed-node")).toHaveTextContent(
      "− node n1 (tool)",
    );
  });

  it("renders modified nodes with their changed field list", () => {
    render(
      <GraphDiff
        diff={emptyDiff({
          modified_nodes: [
            {
              id: "n1",
              changes: [
                { field: "model", from: "gpt-4", to: "gpt-5" },
                { field: "temperature", from: 0.2, to: 0.7 },
              ],
            },
          ],
        })}
      />,
    );
    expect(screen.getByTestId("diff-modified-node")).toHaveTextContent(
      "~ node n1: model, temperature",
    );
  });

  it("renders added and removed edges", () => {
    render(
      <GraphDiff
        diff={emptyDiff({ added_edges: ["e1"], removed_edges: ["e2"] })}
      />,
    );
    expect(screen.getByTestId("diff-added-edge")).toHaveTextContent(
      "+ edge e1",
    );
    expect(screen.getByTestId("diff-removed-edge")).toHaveTextContent(
      "− edge e2",
    );
  });

  it("renders a modified edge with changes, and without a suffix when changes is empty", () => {
    render(
      <GraphDiff
        diff={emptyDiff({
          modified_edges: [
            { id: "e1", changes: [{ field: "condition", from: "a", to: "b" }] },
            { id: "e2", changes: [] },
          ],
        })}
      />,
    );
    const rows = screen.getAllByTestId("diff-modified-edge");
    expect(rows[0]).toHaveTextContent("~ edge e1: condition");
    expect(rows[1]).toHaveTextContent("~ edge e2");
    expect(rows[1]).not.toHaveTextContent(":");
  });

  it("renders artifact changes, preferring ref over kind", () => {
    render(
      <GraphDiff
        diff={emptyDiff({
          artifact_changes: [
            { ref: "prompts:/greeter@prod" },
            { kind: "eval_dataset" },
          ],
        })}
      />,
    );
    const rows = screen.getAllByTestId("diff-artifact-change");
    expect(rows[0]).toHaveTextContent("~ artifact prompts:/greeter@prod");
    expect(rows[1]).toHaveTextContent("~ artifact eval_dataset");
  });

  it("renders deploy gate changes by name", () => {
    render(
      <GraphDiff
        diff={emptyDiff({ deploy_gate_changes: [{ name: "prod-gate" }] })}
      />,
    );
    expect(screen.getByTestId("diff-gate-change")).toHaveTextContent(
      "~ deploy gate prod-gate",
    );
  });

  it("renders every section together for a fully mixed diff", () => {
    render(
      <GraphDiff
        diff={{
          added_nodes: [{ id: "n1" }],
          removed_nodes: [{ id: "n2" }],
          modified_nodes: [
            { id: "n3", changes: [{ field: "x", from: 1, to: 2 }] },
          ],
          added_edges: ["e1"],
          removed_edges: ["e2"],
          modified_edges: [{ id: "e3", changes: [] }],
          artifact_changes: [{ ref: "r1" }],
          deploy_gate_changes: [{ name: "g1" }],
          empty: false,
        }}
      />,
    );
    for (const testId of [
      "diff-added-node",
      "diff-removed-node",
      "diff-modified-node",
      "diff-added-edge",
      "diff-removed-edge",
      "diff-modified-edge",
      "diff-artifact-change",
      "diff-gate-change",
    ]) {
      expect(screen.getByTestId(testId)).toBeInTheDocument();
    }
  });
});
