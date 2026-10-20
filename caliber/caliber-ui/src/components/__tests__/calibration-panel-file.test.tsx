/**
 * CalibrationPanel "Load cases from file": a first user loads a lab's fixture file (49 cases) in one
 * step instead of typing them one at a time (cookbook 03, Fixtures stage).
 */

import { fireEvent } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { CalibrationCase, CalibrationResult } from "@/api/workflowTypes";
import { CalibrationPanel } from "@/components/CalibrationPanel";
import { render, screen, waitFor } from "@/test/utils";

const rows = [
  {
    id: "R01",
    name: "small order",
    inputs: { amount: 49 },
    expectations: { decision: "approve" },
  },
  {
    id: "R02",
    name: "big order",
    inputs: { amount: 900 },
    expectations: { decision: "manual_review" },
  },
];

function renderPanel(
  onSave = vi.fn(async (_cases: CalibrationCase[]) => ({})),
) {
  const onCalibrate = vi.fn(async (): Promise<CalibrationResult> => {
    throw new Error("not used");
  });
  render(
    <CalibrationPanel
      idPrefix="tool-fixtures"
      calibrateTestId="tool-fixtures-calibrate-btn"
      initialCases={[]}
      lastResult={null}
      onSave={onSave}
      onCalibrate={onCalibrate}
    />,
  );
  return { onSave };
}

function pick(text: string, name = "fixtures.jsonl"): void {
  const input = screen.getByTestId(
    "tool-fixtures-calibration-file",
  ) as HTMLInputElement;
  const file = new File([text], name, { type: "application/jsonl" });
  fireEvent.change(input, { target: { files: [file] } });
}

describe("CalibrationPanel file load", () => {
  it("fills the editor from a JSONL file, then Save cases persists exactly those cases", async () => {
    const { onSave } = renderPanel();
    pick(rows.map((r) => JSON.stringify(r)).join("\n"));

    await waitFor(() =>
      expect(
        screen.getByTestId("tool-fixtures-calibration-loaded").textContent,
      ).toContain(
        "Loaded 2 cases from fixtures.jsonl. Save cases to keep them.",
      ),
    );
    expect(
      screen.getAllByTestId("tool-fixtures-calibration-case"),
    ).toHaveLength(2);
    expect(
      (
        screen.getAllByTestId(
          "tool-fixtures-calibration-case-name",
        )[1] as HTMLInputElement
      ).value,
    ).toBe("R02 big order");
    // nothing is persisted by loading
    expect(onSave).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("tool-fixtures-calibration-save"));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave.mock.calls[0]![0]).toEqual([
      {
        name: "R01 small order",
        input: { amount: 49 },
        assertion: { type: "equals", value: '{"decision":"approve"}' },
      },
      {
        name: "R02 big order",
        input: { amount: 900 },
        assertion: { type: "equals", value: '{"decision":"manual_review"}' },
      },
    ]);
    await waitFor(() =>
      expect(
        screen.getByTestId("tool-fixtures-calibration-saved"),
      ).toBeInTheDocument(),
    );
    expect(
      screen.queryByTestId("tool-fixtures-calibration-loaded"),
    ).not.toBeInTheDocument();
  });

  it("says what is wrong with a bad file and leaves the editor alone", async () => {
    renderPanel();
    pick('{"name": "ok"}\nnot json', "broken.jsonl");
    await waitFor(() =>
      expect(
        screen.getByTestId("tool-fixtures-calibration-error").textContent,
      ).toBe("broken.jsonl: Line 2 is not valid JSON."),
    );
    expect(
      screen.getAllByTestId("tool-fixtures-calibration-case"),
    ).toHaveLength(1);
    expect(
      screen.queryByTestId("tool-fixtures-calibration-loaded"),
    ).not.toBeInTheDocument();
  });

  it("ignores a cancelled file chooser and uses the singular for one case", async () => {
    renderPanel();
    const input = screen.getByTestId(
      "tool-fixtures-calibration-file",
    ) as HTMLInputElement;
    fireEvent.change(input, { target: { files: [] } });
    expect(
      screen.queryByTestId("tool-fixtures-calibration-loaded"),
    ).not.toBeInTheDocument();
    pick(JSON.stringify(rows[0]), "one.json");
    await waitFor(() =>
      expect(
        screen.getByTestId("tool-fixtures-calibration-loaded").textContent,
      ).toContain("Loaded 1 case from one.json"),
    );
  });
});
