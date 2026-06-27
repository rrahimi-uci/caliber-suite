/**
 * Dedicated coverage for CalibrationPanel — the props-driven Workflows ›
 * Calibrate panel. This component takes `onSave`/`onCalibrate` callbacks as
 * props (no caliberApi / MSW), so we drive it with vi.fn() callbacks and the
 * shared `render` helper. These tests deliberately target the branches the
 * sibling `calibration-panel.test.tsx` leaves uncovered: the assertion-type
 * select + conditional value input, the needs-value validation guard, the
 * remove-case button (and its disabled-when-single branch), the seed-import
 * button, the per-case result `<pre>` body, and the explicit Save flow
 * (saved badge + save-failure error path).
 */

import { describe, expect, it, vi } from "vitest";

import type { CalibrationCase, CalibrationResult } from "@/api/workflowTypes";
import { CalibrationPanel } from "@/components/CalibrationPanel";
import { render, screen, userEvent, waitFor, within } from "@/test/utils";

function makeResult(): CalibrationResult {
  return {
    pass_rate: 1,
    total: 1,
    passed: 1,
    ran_at: "2026-06-25T12:00:00Z",
    cases: [
      {
        name: "alpha",
        passed: true,
        output: { policy: "ok" },
        error: null,
        duration_ms: 7,
      },
    ],
  };
}

describe("CalibrationPanel (branch coverage)", () => {
  it("switches the assertion type, reveals the value input, and validates needs-value", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn(async (_cases: CalibrationCase[]) => ({}));
    const onCalibrate = vi.fn(async (): Promise<CalibrationResult> => makeResult());

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

    // A no_error draft has no value input.
    expect(screen.queryByTestId("tool-calibration-case-value")).not.toBeInTheDocument();

    // Switch the assertion type -> exercises the select onChange (lines 239-242)
    // and reveals the conditional value input (lines 250-261).
    const select = screen.getByTestId("tool-calibration-case-assertion");
    await user.selectOptions(select, "output_contains");
    expect((select as HTMLSelectElement).value).toBe("output_contains");

    const valueInput = await screen.findByTestId("tool-calibration-case-value");
    expect(valueInput).toBeInTheDocument();

    // Name the case but leave the assertion value blank -> needs-value guard
    // (line 54) surfaces an error and does NOT calibrate.
    await user.type(screen.getByTestId("tool-calibration-case-name"), "needs-value");
    await user.click(screen.getByTestId("tool-calibrate-btn"));

    await waitFor(() => {
      expect(screen.getByTestId("tool-calibration-error")).toBeInTheDocument();
    });
    expect(screen.getByTestId("tool-calibration-error").textContent).toContain(
      'assertion "output_contains" needs a value',
    );
    expect(onCalibrate).not.toHaveBeenCalled();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("adds and removes cases, with the remove button disabled at a single case", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn(async (_cases: CalibrationCase[]) => ({}));
    const onCalibrate = vi.fn(async (): Promise<CalibrationResult> => makeResult());

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

    // One starting draft -> its remove button is disabled (drafts.length <= 1).
    let cases = screen.getAllByTestId("tool-calibration-case");
    expect(cases).toHaveLength(1);
    const firstRemove = within(cases[0]!).getByRole("button", { name: "Remove test case" });
    expect(firstRemove).toBeDisabled();

    // Add a second case (line 281) -> now both remove buttons are enabled.
    await user.click(screen.getByTestId("tool-calibration-add"));
    cases = screen.getAllByTestId("tool-calibration-case");
    expect(cases).toHaveLength(2);
    const removeButtons = screen.getAllByRole("button", { name: "Remove test case" });
    removeButtons.forEach((btn) => expect(btn).toBeEnabled());

    // Remove the second case (line 214 onClick -> removeCase) -> back to one.
    await user.click(removeButtons[1]!);
    expect(screen.getAllByTestId("tool-calibration-case")).toHaveLength(1);

    // The lone remaining case cannot be removed (guard keeps at least one).
    const loneRemove = screen.getByRole("button", { name: "Remove test case" });
    expect(loneRemove).toBeDisabled();
    await user.click(loneRemove);
    expect(screen.getAllByTestId("tool-calibration-case")).toHaveLength(1);
  });

  it("imports seed cases and renders the per-case result body after a run", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn(async (_cases: CalibrationCase[]) => ({}));
    const onCalibrate = vi.fn(async (): Promise<CalibrationResult> => makeResult());
    const seedCases: CalibrationCase[] = [
      { name: "alpha", input: { q: "refund" }, assertion: { type: "no_error" } },
    ];

    render(
      <CalibrationPanel
        idPrefix="tool"
        calibrateTestId="tool-calibrate-btn"
        initialCases={[]}
        lastResult={null}
        onSave={onSave}
        onCalibrate={onCalibrate}
        seedCases={seedCases}
      />,
    );

    // Import the seed (lines 286-294 button -> importSeed handler, 109-113).
    await user.click(screen.getByTestId("tool-calibration-import"));
    await waitFor(() => {
      expect((screen.getByTestId("tool-calibration-case-name") as HTMLInputElement).value).toBe(
        "alpha",
      );
    });

    // Run calibration -> result.cases keyed by name renders the per-case <pre>
    // body (lines 263-271) with output / error / duration_ms.
    await user.click(screen.getByTestId("tool-calibrate-btn"));
    await waitFor(() => {
      expect(screen.getByTestId("tool-calibration-result")).toBeInTheDocument();
    });
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onCalibrate).toHaveBeenCalledTimes(1);

    const verdict = screen.getByTestId("tool-calibration-case-verdict");
    expect(verdict.textContent).toBe("pass");

    const resultBody = screen.getByTestId("tool-calibration-case").querySelector("pre");
    expect(resultBody).not.toBeNull();
    expect(resultBody?.textContent).toContain('"duration_ms": 7');
    expect(resultBody?.textContent).toContain('"policy": "ok"');
  });

  it("saves cases explicitly and shows the Saved badge", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn(async (_cases: CalibrationCase[]) => ({}));
    const onCalibrate = vi.fn(async (): Promise<CalibrationResult> => makeResult());

    render(
      <CalibrationPanel
        idPrefix="tool"
        calibrateTestId="tool-calibrate-btn"
        initialCases={[
          { name: "alpha", input: { q: 1 }, assertion: { type: "no_error" } },
        ]}
        lastResult={null}
        onSave={onSave}
        onCalibrate={onCalibrate}
      />,
    );

    // No Saved badge before saving.
    expect(screen.queryByTestId("tool-calibration-saved")).not.toBeInTheDocument();

    // Explicit Save (lines 298-303 button -> save()) persists without calibrating.
    await user.click(screen.getByTestId("tool-calibration-save"));
    await waitFor(() => {
      expect(screen.getByTestId("tool-calibration-saved")).toBeInTheDocument();
    });
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onCalibrate).not.toHaveBeenCalled();
    expect(screen.queryByTestId("tool-calibration-error")).not.toBeInTheDocument();
  });

  it("surfaces a save failure and suppresses the Saved badge", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn(async (_cases: CalibrationCase[]) => {
      throw new Error("backend down");
    });
    const onCalibrate = vi.fn(async (): Promise<CalibrationResult> => makeResult());

    render(
      <CalibrationPanel
        idPrefix="tool"
        calibrateTestId="tool-calibrate-btn"
        initialCases={[
          { name: "alpha", input: { q: 1 }, assertion: { type: "no_error" } },
        ]}
        lastResult={null}
        onSave={onSave}
        onCalibrate={onCalibrate}
      />,
    );

    await user.click(screen.getByTestId("tool-calibration-save"));
    await waitFor(() => {
      expect(screen.getByTestId("tool-calibration-error")).toBeInTheDocument();
    });
    expect(screen.getByTestId("tool-calibration-error").textContent).toContain("backend down");
    // Save failed -> the Saved badge is suppressed.
    expect(screen.queryByTestId("tool-calibration-saved")).not.toBeInTheDocument();
  });

  it("renders a persisted lastResult on mount (pass-rate badge + empty absent)", () => {
    const onSave = vi.fn(async (_cases: CalibrationCase[]) => ({}));
    const onCalibrate = vi.fn(async (): Promise<CalibrationResult> => makeResult());

    render(
      <CalibrationPanel
        idPrefix="mcp"
        calibrateTestId="mcp-calibrate-btn"
        initialCases={[
          { name: "alpha", input: { q: 1 }, assertion: { type: "no_error" } },
        ]}
        lastResult={makeResult()}
        onSave={onSave}
        onCalibrate={onCalibrate}
      />,
    );

    // lastResult seeds state -> result block (not the empty placeholder).
    expect(screen.queryByTestId("mcp-calibration-empty")).not.toBeInTheDocument();
    expect(screen.getByTestId("mcp-calibration-result").textContent).toContain("100%");
    expect(screen.getByTestId("mcp-calibration-passrate").textContent).toContain("1/1");
    // The idPrefix flows through to every hook.
    expect(screen.getByTestId("mcp-calibration")).toBeInTheDocument();
  });
});
