/**
 * CalibrationPanel — manage a component's saved calibration test cases, persist
 * them, run a scored calibration, and render the per-case pass/fail + pass-rate.
 *
 * Reused by both the tool registry (ToolDetail) and MCP tools (McpServers
 * playground). The page supplies `save`/`calibrate` callbacks that hit the
 * matching endpoint; this component owns only the editable case state + result.
 */

import { useState } from "react";

import type {
  CalibrationAssertionType,
  CalibrationCase,
  CalibrationResult,
} from "@/api/workflowTypes";

/** Editable case where `input` is a raw JSON string (parsed on save/run). */
interface DraftCase {
  name: string;
  inputText: string;
  assertionType: CalibrationAssertionType;
  assertionValue: string;
}

function emptyDraft(): DraftCase {
  return { name: "", inputText: "{}", assertionType: "no_error", assertionValue: "" };
}

function draftFromCase(testCase: CalibrationCase): DraftCase {
  return {
    name: testCase.name,
    inputText: JSON.stringify(testCase.input ?? {}, null, 2),
    assertionType: testCase.assertion?.type ?? "no_error",
    assertionValue: testCase.assertion?.value ?? "",
  };
}

/** Parse a draft into the wire shape; throws on bad JSON / missing fields. */
function caseFromDraft(draft: DraftCase): CalibrationCase {
  const name = draft.name.trim();
  if (!name) throw new Error("Each test case needs a name.");
  let input: Record<string, unknown>;
  try {
    const parsed = JSON.parse(draft.inputText || "{}") as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("Input must be a JSON object.");
    }
    input = parsed as Record<string, unknown>;
  } catch (err) {
    throw new Error(`"${name}" input is not valid JSON: ${err instanceof Error ? err.message : String(err)}`);
  }
  const needsValue = draft.assertionType === "output_contains" || draft.assertionType === "equals";
  if (needsValue && !draft.assertionValue.trim()) {
    throw new Error(`"${name}" assertion "${draft.assertionType}" needs a value.`);
  }
  return {
    name,
    input,
    assertion: {
      type: draft.assertionType,
      value: needsValue ? draft.assertionValue : null,
    },
  };
}

export interface CalibrationPanelProps {
  /** Saved cases loaded from the backend (seed the editor). */
  initialCases: CalibrationCase[];
  /** Last persisted calibration result, if any. */
  lastResult: CalibrationResult | null;
  /** Persist the edited cases. */
  onSave: (cases: CalibrationCase[]) => Promise<unknown>;
  /** Run a scored calibration against the saved cases. */
  onCalibrate: () => Promise<CalibrationResult>;
  /** test-id prefix so tools / MCP can have distinct hooks. */
  idPrefix: string;
  /** Calibrate button test-id (e.g. tool-calibrate-btn / mcp-calibrate-btn). */
  calibrateTestId: string;
  /** Optional seed cases (e.g. from generated tests) the user can import. */
  seedCases?: CalibrationCase[];
}

export function CalibrationPanel({
  initialCases,
  lastResult,
  onSave,
  onCalibrate,
  idPrefix,
  calibrateTestId,
  seedCases,
}: CalibrationPanelProps): JSX.Element {
  const [drafts, setDrafts] = useState<DraftCase[]>(
    initialCases.length > 0 ? initialCases.map(draftFromCase) : [emptyDraft()],
  );
  const [result, setResult] = useState<CalibrationResult | null>(lastResult);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  const update = (index: number, patch: Partial<DraftCase>): void => {
    setDrafts((current) => current.map((d, i) => (i === index ? { ...d, ...patch } : d)));
  };
  const addCase = (): void => setDrafts((current) => [...current, emptyDraft()]);
  const removeCase = (index: number): void =>
    setDrafts((current) => (current.length <= 1 ? current : current.filter((_, i) => i !== index)));

  const importSeed = (): void => {
    if (seedCases && seedCases.length > 0) {
      setDrafts(seedCases.map(draftFromCase));
    }
  };

  const buildCases = (): CalibrationCase[] => drafts.map(caseFromDraft);

  const save = async (): Promise<void> => {
    setError(null);
    let cases: CalibrationCase[];
    try {
      cases = buildCases();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      return;
    }
    setSaving(true);
    try {
      await onSave(cases);
      setSavedAt(Date.now());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save test cases.");
    } finally {
      setSaving(false);
    }
  };

  const run = async (): Promise<void> => {
    setError(null);
    let cases: CalibrationCase[];
    try {
      cases = buildCases();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      return;
    }
    setRunning(true);
    try {
      // Persist before scoring so calibration runs the freshest cases.
      await onSave(cases);
      setSavedAt(Date.now());
      const scored = await onCalibrate();
      setResult(scored);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Calibration failed.");
    } finally {
      setRunning(false);
    }
  };

  const resultByName = new Map(result?.cases.map((c) => [c.name, c]) ?? []);

  return (
    <div data-testid={`${idPrefix}-calibration`} className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-gray-900">Calibration</h3>
          <p className="text-xs text-gray-500">
            Save test cases and score them against this component to track a pass-rate.
          </p>
        </div>
        {result && (
          <span
            data-testid={`${idPrefix}-calibration-passrate`}
            className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-semibold text-gray-700"
          >
            {(result.pass_rate * 100).toFixed(0)}% pass ({result.passed}/{result.total})
          </span>
        )}
      </div>

      <div className="space-y-2">
        {drafts.map((draft, index) => {
          const caseResult = resultByName.get(draft.name.trim());
          return (
            <div
              key={index}
              data-testid={`${idPrefix}-calibration-case`}
              className="rounded border border-gray-200 bg-gray-50 p-2 text-xs"
            >
              <div className="flex items-center gap-2">
                <input
                  aria-label="Test case name"
                  data-testid={`${idPrefix}-calibration-case-name`}
                  value={draft.name}
                  placeholder="case name"
                  onChange={(e) => update(index, { name: e.target.value })}
                  className="flex-1 rounded border border-gray-300 px-2 py-1"
                />
                {caseResult && (
                  <span
                    data-testid={`${idPrefix}-calibration-case-verdict`}
                    className={`rounded-full px-1.5 py-0.5 text-[10px] font-semibold ${
                      caseResult.passed
                        ? "bg-emerald-50 text-emerald-700"
                        : "bg-red-50 text-red-700"
                    }`}
                  >
                    {caseResult.passed ? "pass" : "fail"}
                  </span>
                )}
                <button
                  type="button"
                  aria-label="Remove test case"
                  onClick={() => removeCase(index)}
                  disabled={drafts.length <= 1}
                  className="rounded border border-gray-300 px-2 py-1 text-gray-500 disabled:opacity-40"
                >
                  ✕
                </button>
              </div>
              <label className="mt-2 block">
                <span className="font-semibold text-gray-500">Input (JSON)</span>
                <textarea
                  aria-label="Test case input JSON"
                  data-testid={`${idPrefix}-calibration-case-input`}
                  value={draft.inputText}
                  onChange={(e) => update(index, { inputText: e.target.value })}
                  className="mt-1 h-20 w-full rounded border border-gray-300 px-2 py-1 font-mono"
                />
              </label>
              <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
                <label className="block">
                  <span className="font-semibold text-gray-500">Assertion</span>
                  <select
                    aria-label="Assertion type"
                    data-testid={`${idPrefix}-calibration-case-assertion`}
                    value={draft.assertionType}
                    onChange={(e) =>
                      update(index, {
                        assertionType: e.target.value as CalibrationAssertionType,
                      })
                    }
                    className="mt-1 w-full rounded border border-gray-300 px-2 py-1"
                  >
                    <option value="no_error">no_error</option>
                    <option value="output_contains">output_contains</option>
                    <option value="equals">equals</option>
                  </select>
                </label>
                {draft.assertionType !== "no_error" && (
                  <label className="block">
                    <span className="font-semibold text-gray-500">Value</span>
                    <input
                      aria-label="Assertion value"
                      data-testid={`${idPrefix}-calibration-case-value`}
                      value={draft.assertionValue}
                      onChange={(e) => update(index, { assertionValue: e.target.value })}
                      className="mt-1 w-full rounded border border-gray-300 px-2 py-1 font-mono"
                    />
                  </label>
                )}
              </div>
              {caseResult && (
                <pre className="mt-2 max-h-32 overflow-auto rounded bg-white p-2 text-[10px] text-gray-700">
                  {JSON.stringify(
                    { output: caseResult.output, error: caseResult.error, duration_ms: caseResult.duration_ms },
                    null,
                    2,
                  )}
                </pre>
              )}
            </div>
          );
        })}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          data-testid={`${idPrefix}-calibration-add`}
          onClick={addCase}
          className="rounded border border-gray-300 px-2 py-1 text-xs"
        >
          Add case
        </button>
        {seedCases && seedCases.length > 0 && (
          <button
            type="button"
            data-testid={`${idPrefix}-calibration-import`}
            onClick={importSeed}
            className="rounded border border-gray-300 px-2 py-1 text-xs"
          >
            Seed from generated tests
          </button>
        )}
        <button
          type="button"
          data-testid={`${idPrefix}-calibration-save`}
          onClick={() => void save()}
          disabled={saving || running}
          className="rounded border border-gray-300 px-3 py-1 text-xs disabled:opacity-50"
        >
          {saving ? "Saving…" : "Save cases"}
        </button>
        <button
          type="button"
          data-testid={calibrateTestId}
          onClick={() => void run()}
          disabled={saving || running}
          className="rounded bg-emerald-600 px-3 py-1 text-xs font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
        >
          {running ? "Running…" : "Run calibration"}
        </button>
        {savedAt !== null && !error && (
          <span data-testid={`${idPrefix}-calibration-saved`} className="text-xs text-gray-500">
            Saved
          </span>
        )}
      </div>

      {error && (
        <div data-testid={`${idPrefix}-calibration-error`} className="rounded border border-red-200 bg-red-50 px-2 py-1 text-xs text-red-700">
          {error}
        </div>
      )}

      {result ? (
        <div
          data-testid={`${idPrefix}-calibration-result`}
          className="rounded border border-gray-200 bg-white p-2 text-xs text-gray-700"
        >
          Pass rate {(result.pass_rate * 100).toFixed(0)}% — {result.passed}/{result.total} passed
          {result.ran_at ? ` · ${result.ran_at}` : ""}
        </div>
      ) : (
        <div
          data-testid={`${idPrefix}-calibration-empty`}
          className="rounded border border-dashed border-gray-300 bg-gray-50 p-2 text-xs text-gray-400"
        >
          No calibration run yet.
        </div>
      )}
    </div>
  );
}
