import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CalibrationStep, StepConnector } from "@/components/CalibrationStep";

describe("CalibrationStep", () => {
  it("renders the numbered badge, title, description, and children", () => {
    render(
      <CalibrationStep
        index={1}
        title="Build a test set"
        description="Curate representative examples to calibrate against."
      >
        <div data-testid="step-body">step content</div>
      </CalibrationStep>,
    );

    // The numbered badge shows the step index.
    expect(screen.getByText("1")).toBeInTheDocument();
    // Title and description render their text.
    expect(screen.getByText("Build a test set")).toBeInTheDocument();
    expect(
      screen.getByText("Curate representative examples to calibrate against."),
    ).toBeInTheDocument();
    // The children slot is rendered.
    expect(screen.getByTestId("step-body")).toHaveTextContent("step content");
  });

  it("renders a distinct index for a second step instance", () => {
    render(
      <CalibrationStep index={2} title="Run the optimizer" description="Kick off the run.">
        <span>body</span>
      </CalibrationStep>,
    );

    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("Run the optimizer")).toBeInTheDocument();
  });

  it("uses a section element as the step container", () => {
    const { container } = render(
      <CalibrationStep index={3} title="Heading" description="Desc">
        <p>inner</p>
      </CalibrationStep>,
    );

    const section = container.querySelector("section");
    expect(section).not.toBeNull();
    expect(section).toHaveTextContent("inner");
  });
});

describe("StepConnector", () => {
  it("renders a decorative, aria-hidden divider", () => {
    const { container } = render(<StepConnector />);

    const divider = container.firstElementChild;
    expect(divider).not.toBeNull();
    expect(divider).toHaveAttribute("aria-hidden", "true");
    // The downward chevron SVG is present inside the divider.
    expect(container.querySelector("svg")).not.toBeNull();
  });
});
