import { render, screen } from "@/test/utils";
import { PipelineProgress } from "@/components/PipelineProgress";

/** Reads a stage dot's derived state back off its `title` tooltip in compact mode. */
function stageTitles(): string[] {
  return screen
    .getAllByTitle(/^(Triage|Evidence|Diagnosis|Candidate|Eval):/)
    .map((el) => el.getAttribute("title") ?? "");
}

describe("PipelineProgress", () => {
  it("marks stages before the current one complete, the current one in-progress, and the rest pending", () => {
    render(
      <PipelineProgress currentStage="diagnosis" status="running" compact />,
    );
    expect(stageTitles()).toEqual([
      "Triage: complete",
      "Evidence: complete",
      "Diagnosis: current",
      "Candidate: pending",
      "Eval: pending",
    ]);
  });

  it("paints the current stage failed when the job's status is failed", () => {
    render(
      <PipelineProgress currentStage="evidence" status="failed" compact />,
    );
    expect(stageTitles()).toEqual([
      "Triage: complete",
      "Evidence: failed",
      "Diagnosis: pending",
      "Candidate: pending",
      "Eval: pending",
    ]);
  });

  it("paints the current stage failed when the job's status is rejected", () => {
    render(
      <PipelineProgress currentStage="candidate" status="rejected" compact />,
    );
    expect(stageTitles()).toEqual([
      "Triage: complete",
      "Evidence: complete",
      "Diagnosis: complete",
      "Candidate: failed",
      "Eval: pending",
    ]);
  });

  it("marks every stage complete once the job has completed, regardless of the recorded stage", () => {
    render(<PipelineProgress currentStage="eval" status="completed" compact />);
    expect(stageTitles()).toEqual([
      "Triage: complete",
      "Evidence: complete",
      "Diagnosis: complete",
      "Candidate: complete",
      "Eval: complete",
    ]);
  });

  it("marks every stage complete for the done stage even if status hasn't caught up to completed", () => {
    render(<PipelineProgress currentStage="done" status="applied" compact />);
    expect(stageTitles()).toEqual([
      "Triage: complete",
      "Evidence: complete",
      "Diagnosis: complete",
      "Candidate: complete",
      "Eval: complete",
    ]);
  });

  it("treats an unrecognized stage as fully complete rather than crashing", () => {
    render(
      <PipelineProgress
        currentStage={"some_future_stage" as never}
        status="running"
        compact
      />,
    );
    expect(stageTitles()).toEqual([
      "Triage: complete",
      "Evidence: complete",
      "Diagnosis: complete",
      "Candidate: complete",
      "Eval: complete",
    ]);
  });

  it("renders the full (non-compact) stepper with labeled stages and connecting rails", () => {
    render(<PipelineProgress currentStage="candidate" status="running" />);
    const list = screen.getByRole("list");
    expect(list).toBeInTheDocument();
    for (const label of [
      "Triage",
      "Evidence",
      "Diagnosis",
      "Candidate",
      "Eval",
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    // 4 connecting rails between 5 stages.
    expect(list.querySelectorAll("li > span.bg-surface-200")).toHaveLength(4);
  });
});
