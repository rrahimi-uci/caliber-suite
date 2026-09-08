import { render, screen } from "@/test/utils";
import { EvalComparison } from "@/components/EvalComparison";

describe("EvalComparison", () => {
  it("renders a placeholder when no results are recorded", () => {
    render(<EvalComparison results={null} />);
    expect(screen.getByText("No eval results recorded.")).toBeInTheDocument();
  });

  it("renders the same placeholder when results is undefined", () => {
    render(<EvalComparison results={undefined} />);
    expect(screen.getByText("No eval results recorded.")).toBeInTheDocument();
  });

  it("shows a cold-start message instead of a baseline block when there is no baseline", () => {
    render(
      <EvalComparison
        results={{
          candidate: {
            overall: 0.842,
            dimensions: { correctness: 0.9, tone: 0.784 },
          },
        }}
      />,
    );
    expect(screen.getByText("Candidate")).toBeInTheDocument();
    expect(screen.getByText("0.842")).toBeInTheDocument();
    expect(screen.getByText("correctness")).toBeInTheDocument();
    expect(screen.getByText("0.900")).toBeInTheDocument();
    expect(
      screen.getByText("Cold start — no baseline to compare against."),
    ).toBeInTheDocument();
  });

  it("renders candidate and baseline scores, delta pills, a passing gate, N=, and the dataset id", () => {
    render(
      <EvalComparison
        results={{
          candidate: { overall: 0.9, dimensions: { correctness: 0.95 } },
          baseline: { overall: 0.8, dimensions: { correctness: 0.85 } },
          deltas: { correctness: 0.1, tone: -0.05 },
          gate: { passed: true },
          n_examples: 42,
          eval_dataset_id: "DS-abc123",
        }}
      />,
    );
    expect(screen.getByText("Candidate")).toBeInTheDocument();
    expect(screen.getByText("Baseline")).toBeInTheDocument();
    expect(screen.getByText("0.900")).toBeInTheDocument();
    expect(screen.getByText("0.800")).toBeInTheDocument();

    // Positive delta renders with a leading "+" and pp suffix.
    expect(screen.getByText(/\+10\.0pp/)).toBeInTheDocument();
    // Negative delta has no leading "+".
    expect(screen.getByText(/^-5\.0pp/)).toBeInTheDocument();

    expect(screen.getByText("Gate passed")).toBeInTheDocument();
    expect(screen.getByText(/eligible for approval/)).toBeInTheDocument();

    expect(screen.getByText("42 examples")).toBeInTheDocument();
    expect(screen.getByText("DS-abc123")).toBeInTheDocument();
  });

  it("renders a failed gate with its reasons listed", () => {
    render(
      <EvalComparison
        results={{
          candidate: { overall: 0.5 },
          gate: {
            passed: false,
            reasons: ["correctness below threshold", "tone regressed"],
          },
        }}
      />,
    );
    expect(screen.getByText("Gate failed")).toBeInTheDocument();
    expect(screen.getByText("correctness below threshold")).toBeInTheDocument();
    expect(screen.getByText("tone regressed")).toBeInTheDocument();
  });

  it("renders a pending-gate banner when the gate verdict is absent", () => {
    render(<EvalComparison results={{ candidate: { overall: 0.5 } }} />);
    expect(screen.getByText("Gate decision pending.")).toBeInTheDocument();
  });

  it("renders caliber_tags as a key/value grid", () => {
    render(
      <EvalComparison
        results={{
          candidate: { overall: 0.5 },
          caliber_tags: { environment: "staging", model: "gpt-5" },
        }}
      />,
    );
    expect(screen.getByText("environment")).toBeInTheDocument();
    expect(screen.getByText("staging")).toBeInTheDocument();
    expect(screen.getByText("model")).toBeInTheDocument();
    expect(screen.getByText("gpt-5")).toBeInTheDocument();
  });

  it("renders '—' for a missing overall score rather than crashing on undefined.toFixed", () => {
    render(<EvalComparison results={{ candidate: {} }} />);
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });
});
