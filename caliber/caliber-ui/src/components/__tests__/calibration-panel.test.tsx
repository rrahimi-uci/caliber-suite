import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CalibrationCase, CalibrationResult } from "@/api/workflowTypes";
import { CalibrationPanel } from "@/components/CalibrationPanel";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("CalibrationPanel", () => {
  it("enters a case, runs calibration, and shows pass/fail per case", async () => {
    const onSave = vi.fn(async (_cases: CalibrationCase[]) => ({}));
    const result: CalibrationResult = {
      pass_rate: 0.5,
      total: 2,
      passed: 1,
      ran_at: "2026-06-17T00:00:00Z",
      cases: [
        { name: "ok", passed: true, output: { policy: "x" }, error: null, duration_ms: 1 },
        { name: "bad", passed: false, output: null, error: "boom", duration_ms: 2 },
      ],
    };
    const onCalibrate = vi.fn(async (): Promise<CalibrationResult> => result);

    render(
      <CalibrationPanel
        idPrefix="tool"
        calibrateTestId="tool-calibrate-btn"
        initialCases={[
          { name: "ok", input: { query: "refund" }, assertion: { type: "no_error" } },
          {
            name: "bad",
            input: { query: "refund" },
            assertion: { type: "output_contains", value: "missing" },
          },
        ]}
        lastResult={null}
        onSave={onSave}
        onCalibrate={onCalibrate}
      />,
    );

    // Seeded with the two initial cases.
    expect(screen.getAllByTestId("tool-calibration-case")).toHaveLength(2);
    expect(screen.getByTestId("tool-calibration-empty")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("tool-calibrate-btn"));

    await waitFor(() => {
      expect(screen.getByTestId("tool-calibration-result")).toBeInTheDocument();
    });
    // Run persists the cases then scores them.
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onCalibrate).toHaveBeenCalledTimes(1);

    const verdicts = screen.getAllByTestId("tool-calibration-case-verdict");
    expect(verdicts.map((v) => v.textContent)).toEqual(["pass", "fail"]);
    expect(screen.getByTestId("tool-calibration-result").textContent).toContain("50%");
    expect(screen.getByTestId("tool-calibration-passrate").textContent).toContain("1/2");
  });

  it("adds a new case and surfaces invalid JSON without calibrating", async () => {
    const onSave = vi.fn(async (_cases: CalibrationCase[]) => ({}));
    const onCalibrate = vi.fn(async (): Promise<CalibrationResult> => {
      throw new Error("should not be called");
    });

    render(
      <CalibrationPanel
        idPrefix="tool"
        calibrateTestId="tool-calibrate-btn"
        initialCases={[]}
        lastResult={null}
        onSave={onSave}
        onCalibrate={onCalibrate}
      />,
    );

    // Starts with one empty draft; add a second.
    fireEvent.click(screen.getByTestId("tool-calibration-add"));
    const names = screen.getAllByTestId("tool-calibration-case-name");
    fireEvent.change(names[0]!, { target: { value: "good" } });
    fireEvent.change(names[1]!, { target: { value: "broken" } });

    const inputs = screen.getAllByTestId("tool-calibration-case-input");
    fireEvent.change(inputs[1]!, { target: { value: "{not json" } });

    fireEvent.click(screen.getByTestId("tool-calibrate-btn"));

    await waitFor(() => {
      expect(screen.getByTestId("tool-calibration-error")).toBeInTheDocument();
    });
    expect(screen.getByTestId("tool-calibration-error").textContent).toContain("broken");
    expect(onCalibrate).not.toHaveBeenCalled();
  });
});
