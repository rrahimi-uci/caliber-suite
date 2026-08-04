/**
 * Judges — custom LLM judges (MLflow 3.14 `make_judge`).
 *
 * Operators author a reusable judge (name + NL instructions referencing the
 * evaluation variables + a model) and then select it as a scorer in eval runs.
 * Mirrors the Test Sets / Skills chrome.
 */

import { useCallback, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, caliberApi } from "@/api/caliberApi";
import { ClearFiltersButton } from "@/components/ClearFiltersButton";
import { FilterBar } from "@/components/FilterBar";
import { FilterSelect } from "@/components/FilterSelect";
import { SearchInput } from "@/components/SearchInput";
import type {
  Judge,
  JudgeAlignmentExampleInput,
  JudgeAlignmentResult,
  JudgeCreatePayload,
  JudgeTestRunResult,
  JudgeValueType,
  ResourceStatus,
} from "@/api/types";
import { useApi } from "@/hooks/useApi";
import { relativeTime } from "@/lib/time";

type StatusFilter = "active" | "archived" | "all";

const TEMPLATE_VARS = ["inputs", "outputs", "expectations"] as const;
const VALUE_TYPES: JudgeValueType[] = ["bool", "int", "float", "str"];

/** Instructions must reference at least one `{{ var }}` — mirrors the backend. */
function referencesVar(instructions: string): boolean {
  const normalized = instructions.replace(/\s+/g, "");
  return TEMPLATE_VARS.some((v) => normalized.includes(`{{${v}}}`));
}

export function Judges(): JSX.Element {
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("active");
  const fetcher = useCallback(
    (signal: AbortSignal) => caliberApi.listJudges({ status: statusFilter }, signal),
    [statusFilter],
  );
  const { data, error, loading, refresh } = useApi(fetcher, [statusFilter]);
  // Datalist suggestions for the free-text judge-model field: every model
  // already in use (guaranteed routable by the gateway) plus the app's own
  // default. No speculative names — the field stays free-text regardless.
  const modelSuggestions = useMemo(
    () =>
      Array.from(
        new Set([
          "openai:/gpt-4o-mini",
          ...(data ?? []).map((j) => j.model).filter((m): m is string => Boolean(m)),
        ]),
      ),
    [data],
  );
  const [showCreate, setShowCreate] = useState(false);
  const [trying, setTrying] = useState<Judge | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [modelFilter, setModelFilter] = useState("");
  const query = search.trim().toLowerCase();

  const modelOptions = Array.from(
    new Set((data ?? []).map((j) => j.model).filter((m): m is string => Boolean(m))),
  )
    .sort()
    .map((m) => ({ value: m, label: m }));

  const visible = (data ?? []).filter((judge) => {
    if (modelFilter && judge.model !== modelFilter) return false;
    if (!query) return true;
    return [judge.name, judge.description, judge.owner, judge.instructions]
      .filter(Boolean)
      .some((field) => String(field).toLowerCase().includes(query));
  });
  const hasFilters = Boolean(search || modelFilter || statusFilter !== "active");

  const setStatus = async (judge: Judge, status: ResourceStatus): Promise<void> => {
    setPending(judge.judge_id);
    setActionError(null);
    try {
      await caliberApi.updateJudge(judge.judge_id, { status });
      refresh();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "update failed");
    } finally {
      setPending(null);
    }
  };

  return (
    <>
      <div className="flex items-center gap-2 text-sm text-gray-500 mb-4">
        <Link to="/" className="hover:text-gray-700">
          Dashboard
        </Link>
        <Chevron />
        <span className="text-gray-900 font-medium">Judges</span>
      </div>

      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-semibold text-gray-900">Judges</h1>
          <p className="text-sm text-gray-500 mt-0.5">
            Custom LLM judges scored against eval runs — authored once, reused as
            scorers. Backed by MLflow&nbsp;3.14 <code className="text-xs">make_judge</code>.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setShowCreate((v) => !v)}
          className="text-sm font-medium text-white bg-caliber-purple px-3 py-1.5 rounded-md hover:bg-caliber-purple-dark"
        >
          {showCreate ? "Cancel" : "+ New Judge"}
        </button>
      </div>

      {error && (
        <div className="mb-4 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          <div className="font-medium">Failed to load judges</div>
          <div className="text-xs mt-0.5">{error.message}</div>
        </div>
      )}
      {actionError && (
        <div className="mb-4 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {actionError}
        </div>
      )}

      {showCreate && (
        <CreateJudgePanel
          modelSuggestions={modelSuggestions}
          onCancel={() => setShowCreate(false)}
          onSuccess={() => {
            setShowCreate(false);
            refresh();
          }}
        />
      )}

      {trying && (
        <JudgePlayground judge={trying} onClose={() => setTrying(null)} />
      )}

      <div className="mb-4 flex flex-col gap-3">
        <FilterTabs current={statusFilter} onChange={setStatusFilter} />
        <FilterBar
          search={
            <SearchInput
              value={search}
              onChange={setSearch}
              ariaLabel="Search judges"
              placeholder="Search by name, model, instructions…"
              className="w-full"
            />
          }
          filters={
            <FilterSelect
              label="Model"
              allLabel="All models"
              value={modelFilter}
              onChange={setModelFilter}
              options={modelOptions}
              className="w-full sm:w-56"
            />
          }
          actions={
            <ClearFiltersButton
              visible={hasFilters}
              onClear={() => {
                setStatusFilter("active");
                setSearch("");
                setModelFilter("");
              }}
            />
          }
        />
      </div>

      <div className="bg-white rounded-lg border border-surface-200 overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-surface-200 bg-surface-50">
              <th className="text-left font-medium px-4 py-3">Name</th>
              <th className="text-left font-medium px-4 py-3">Model</th>
              <th className="text-left font-medium px-4 py-3">Returns</th>
              <th className="text-left font-medium px-4 py-3">Owner</th>
              <th className="text-left font-medium px-4 py-3">Status</th>
              <th className="text-left font-medium px-4 py-3">Updated</th>
              <th className="text-right font-medium px-4 py-3">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-surface-100">
            {loading && !data && (
              <tr>
                <td colSpan={7} className="px-4 py-10 text-center text-sm text-gray-500">
                  Loading…
                </td>
              </tr>
            )}
            {data && visible.length === 0 && (
              <tr>
                <td colSpan={7} className="px-4 py-10 text-center text-sm text-gray-500">
                  {query ? `No judges match “${search.trim()}”.` : "No judges yet."}
                </td>
              </tr>
            )}
            {visible.map((judge) => (
              <tr key={judge.judge_id} className="hover:bg-surface-50 align-top">
                <td className="px-4 py-3">
                  <div className="font-medium text-gray-900">{judge.name}</div>
                  <div className="text-xs text-gray-500 mt-0.5 max-w-md line-clamp-2">
                    {judge.description || judge.instructions}
                  </div>
                </td>
                <td className="px-4 py-3 text-gray-600 font-mono text-xs">
                  {judge.model || <span className="text-gray-400">default</span>}
                </td>
                <td className="px-4 py-3">
                  <span className="text-[10px] font-mono uppercase bg-violet-50 text-caliber-purple px-1.5 py-0.5 rounded">
                    {judge.feedback_value_type ?? "auto"}
                  </span>
                </td>
                <td className="px-4 py-3 text-gray-600">{judge.owner}</td>
                <td className="px-4 py-3">
                  <StatusPill status={judge.status} />
                </td>
                <td className="px-4 py-3 text-xs text-gray-500">
                  {relativeTime(judge.updated_at)}
                </td>
                <td className="px-4 py-3 text-right">
                  <div className="flex items-center justify-end gap-3">
                    <button
                      type="button"
                      data-testid={`judge-try-${judge.judge_id}`}
                      onClick={() => setTrying(judge)}
                      className="text-xs font-medium text-caliber-purple hover:underline"
                    >
                      Try it
                    </button>
                    <button
                      type="button"
                      disabled={pending === judge.judge_id}
                      onClick={() =>
                        void setStatus(
                          judge,
                          judge.status === "active" ? "archived" : "active",
                        )
                      }
                      className="text-xs font-medium text-gray-600 hover:underline disabled:opacity-50"
                    >
                      {judge.status === "active" ? "Archive" : "Restore"}
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function FilterTabs({
  current,
  onChange,
}: {
  current: StatusFilter;
  onChange: (value: StatusFilter) => void;
}): JSX.Element {
  const tabs: StatusFilter[] = ["active", "archived", "all"];
  return (
    <div className="flex gap-2 text-sm">
      {tabs.map((t) => (
        <button
          key={t}
          type="button"
          onClick={() => onChange(t)}
          className={`px-3 py-1 rounded-md ${
            current === t
              ? "bg-caliber-purple text-white"
              : "bg-surface-100 text-gray-600 hover:bg-surface-200"
          }`}
        >
          {t.charAt(0).toUpperCase() + t.slice(1)}
        </button>
      ))}
    </div>
  );
}

function StatusPill({ status }: { status: ResourceStatus }): JSX.Element {
  const cls =
    status === "active"
      ? "bg-emerald-100 text-emerald-700"
      : "bg-gray-200 text-gray-600";
  return (
    <span className={`text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded ${cls}`}>
      {status}
    </span>
  );
}

function JudgePlayground({
  judge,
  onClose,
}: {
  judge: Judge;
  onClose: () => void;
}): JSX.Element {
  const [outputs, setOutputs] = useState("");
  const [inputs, setInputs] = useState("");
  const [expectations, setExpectations] = useState("");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<JudgeTestRunResult | null>(null);
  const [mode, setMode] = useState<"try" | "align">("try");

  const parseOptionalJson = (
    text: string,
    label: string,
  ): Record<string, unknown> | "error" | undefined => {
    if (!text.trim()) return undefined;
    try {
      const parsed = JSON.parse(text);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        setError(`${label} must be a JSON object.`);
        return "error";
      }
      return parsed as Record<string, unknown>;
    } catch {
      setError(`${label} is not valid JSON.`);
      return "error";
    }
  };

  const run = async (): Promise<void> => {
    setError(null);
    setResult(null);
    const parsedInputs = parseOptionalJson(inputs, "Inputs");
    if (parsedInputs === "error") return;
    const parsedExpectations = parseOptionalJson(expectations, "Expectations");
    if (parsedExpectations === "error") return;
    setRunning(true);
    try {
      const res = await caliberApi.testRunJudge(judge.judge_id, {
        outputs,
        inputs: parsedInputs,
        expectations: parsedExpectations,
      });
      setResult(res);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "judge run failed");
    } finally {
      setRunning(false);
    }
  };

  return (
    <div
      data-testid="judge-playground"
      className="mb-6 bg-white rounded-lg border border-surface-200 p-4"
    >
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-semibold text-gray-900">
          Try “{judge.name}”
        </h2>
        <button
          type="button"
          onClick={onClose}
          className="text-xs text-gray-500 hover:text-gray-800"
        >
          Close
        </button>
      </div>

      <div className="mb-3 flex gap-2 text-xs">
        <button
          type="button"
          data-testid="judge-mode-try"
          onClick={() => setMode("try")}
          className={`px-2.5 py-1 rounded-md ${
            mode === "try"
              ? "bg-caliber-purple text-white"
              : "bg-surface-100 text-gray-600 hover:bg-surface-200"
          }`}
        >
          Try once
        </button>
        <button
          type="button"
          data-testid="judge-mode-align"
          onClick={() => setMode("align")}
          className={`px-2.5 py-1 rounded-md ${
            mode === "align"
              ? "bg-caliber-purple text-white"
              : "bg-surface-100 text-gray-600 hover:bg-surface-200"
          }`}
        >
          Human alignment
        </button>
      </div>

      {mode === "align" ? (
        <JudgeAlignmentSection judge={judge} />
      ) : (
        <>
      <p className="text-xs text-gray-500 mb-3">
        Run this judge once on a sample to see its verdict + rationale — nothing is saved.
      </p>
      <div>
        <label className="text-xs text-gray-500 block mb-1">Output to judge</label>
        <textarea
          data-testid="judge-try-outputs"
          aria-label="Output to judge"
          rows={3}
          className="w-full border border-surface-200 rounded-md px-3 py-1.5 text-sm"
          value={outputs}
          onChange={(e) => setOutputs(e.target.value)}
          placeholder="The model output the judge should grade…"
        />
      </div>
      <div className="mt-3 grid grid-cols-2 gap-3">
        <div>
          <label className="text-xs text-gray-500 block mb-1">Inputs (JSON, optional)</label>
          <textarea
            data-testid="judge-try-inputs"
            aria-label="Inputs JSON"
            rows={2}
            className="w-full border border-surface-200 rounded-md px-3 py-1.5 text-sm font-mono"
            value={inputs}
            onChange={(e) => setInputs(e.target.value)}
            placeholder='{"question": "…"}'
          />
        </div>
        <div>
          <label className="text-xs text-gray-500 block mb-1">Expectations (JSON, optional)</label>
          <textarea
            data-testid="judge-try-expectations"
            aria-label="Expectations JSON"
            rows={2}
            className="w-full border border-surface-200 rounded-md px-3 py-1.5 text-sm font-mono"
            value={expectations}
            onChange={(e) => setExpectations(e.target.value)}
            placeholder='{"expected": "…"}'
          />
        </div>
      </div>
      {error && <div className="mt-3 text-sm text-red-600">{error}</div>}
      {result && (
        <div
          data-testid="judge-try-result"
          className="mt-3 rounded-md border border-surface-200 bg-surface-50 px-3 py-2 text-sm"
        >
          <div className="flex items-center gap-2">
            <span className="text-xs text-gray-500">Score</span>
            <span className="font-semibold text-gray-900">
              {Math.round(result.score * 100)}%
            </span>
            <span className="text-xs text-gray-400">
              (verdict: {String(result.value)})
            </span>
          </div>
          {result.rationale && (
            <p className="mt-1 text-xs text-gray-600">{result.rationale}</p>
          )}
        </div>
      )}
      <div className="mt-4 flex justify-end">
        <button
          type="button"
          data-testid="judge-try-run"
          disabled={running || !outputs.trim()}
          onClick={() => void run()}
          className="text-sm font-medium text-white bg-caliber-purple px-3 py-1.5 rounded-md hover:bg-caliber-purple-dark disabled:opacity-50"
        >
          {running ? "Running…" : "Run judge"}
        </button>
      </div>
        </>
      )}
    </div>
  );
}

interface AlignmentRow {
  outputs: string;
  label: boolean;
  inputs?: Record<string, unknown>;
  expectations?: Record<string, unknown>;
}

function JudgeAlignmentSection({ judge }: { judge: Judge }): JSX.Element {
  const [rows, setRows] = useState<AlignmentRow[]>([
    { outputs: "", label: true },
    { outputs: "", label: false },
  ]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<JudgeAlignmentResult | null>(null);
  const [queueId, setQueueId] = useState("");
  const [questionKey, setQuestionKey] = useState("");
  const [importing, setImporting] = useState(false);
  const [importMessage, setImportMessage] = useState<string | null>(null);
  const queueFetcher = useCallback(
    (signal: AbortSignal) => caliberApi.listReviewQueues({ status: "active" }, signal),
    [],
  );
  const { data: reviewQueues } = useApi(queueFetcher, []);
  const selectedQueue = (reviewQueues ?? []).find((queue) => queue.queue_id === queueId);
  const passFailQuestions = selectedQueue?.questions.filter(
    (question) => question.type === "pass_fail",
  ) ?? [];

  const setRow = (index: number, patch: Partial<AlignmentRow>): void =>
    setRows((prev) => prev.map((r, i) => (i === index ? { ...r, ...patch } : r)));

  const run = async (): Promise<void> => {
    setError(null);
    setResult(null);
    const examples: JudgeAlignmentExampleInput[] = rows
      .filter((r) => r.outputs.trim())
      .map((r) => ({
        outputs: r.outputs,
        label: r.label,
        inputs: r.inputs,
        expectations: r.expectations,
      }));
    if (examples.length === 0) {
      setError("Add at least one labeled example.");
      return;
    }
    setRunning(true);
    try {
      setResult(await caliberApi.alignJudge(judge.judge_id, { examples }));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "alignment run failed");
    } finally {
      setRunning(false);
    }
  };

  const importFromQueue = async (): Promise<void> => {
    if (!queueId || !questionKey) return;
    setError(null);
    setImportMessage(null);
    setImporting(true);
    try {
      const imported = await caliberApi.importReviewAlignmentExamples(
        queueId,
        questionKey,
      );
      setRows(
        imported.examples.map((example) => ({
          outputs: example.outputs,
          label: example.label,
          inputs: example.inputs,
          expectations: example.expectations,
        })),
      );
      setImportMessage(
        `Imported ${imported.examples.length} completed label(s); skipped ${imported.skipped.length}.`,
      );
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "review label import failed");
    } finally {
      setImporting(false);
    }
  };

  return (
    <div>
      <p className="text-xs text-gray-500 mb-3">
        Label a few outputs yourself, then run the judge to see how often it agrees
        with you — the agreement rate and Cohen&apos;s kappa (chance-corrected).
      </p>
      <div className="mb-3 grid gap-2 rounded-md border border-surface-200 bg-surface-50 p-3 sm:grid-cols-[1fr_1fr_auto]">
        <select
          data-testid="align-review-queue"
          aria-label="Alignment review queue"
          className="rounded-md border border-surface-200 bg-white px-2 py-1.5 text-sm"
          value={queueId}
          onChange={(event) => {
            setQueueId(event.target.value);
            setQuestionKey("");
          }}
        >
          <option value="">Import from Review Queue…</option>
          {(reviewQueues ?? []).map((queue) => (
            <option key={queue.queue_id} value={queue.queue_id}>{queue.name}</option>
          ))}
        </select>
        <select
          data-testid="align-review-question"
          aria-label="Alignment pass fail question"
          className="rounded-md border border-surface-200 bg-white px-2 py-1.5 text-sm"
          value={questionKey}
          disabled={!queueId}
          onChange={(event) => setQuestionKey(event.target.value)}
        >
          <option value="">Pass/fail label…</option>
          {passFailQuestions.map((question) => (
            <option key={question.key} value={question.key}>{question.title}</option>
          ))}
        </select>
        <button
          type="button"
          data-testid="align-import-review-labels"
          disabled={!queueId || !questionKey || importing}
          onClick={() => void importFromQueue()}
          className="rounded-md bg-white px-3 py-1.5 text-xs font-medium text-caliber-purple ring-1 ring-surface-200 disabled:opacity-50"
        >
          {importing ? "Importing…" : "Import labels"}
        </button>
      </div>
      {importMessage && <p className="mb-3 text-xs text-emerald-700">{importMessage}</p>}
      <div className="space-y-2">
        {rows.map((row, i) => (
          <div key={i} className="flex items-center gap-2">
            <input
              data-testid={`align-output-${i}`}
              aria-label={`Example ${i + 1} output`}
              className="flex-1 border border-surface-200 rounded-md px-3 py-1.5 text-sm"
              value={row.outputs}
              onChange={(e) => setRow(i, { outputs: e.target.value })}
              placeholder="Output to judge…"
            />
            <select
              data-testid={`align-label-${i}`}
              aria-label={`Example ${i + 1} human label`}
              className="border border-surface-200 rounded-md px-2 py-1.5 text-sm bg-white"
              value={row.label ? "pass" : "fail"}
              onChange={(e) => setRow(i, { label: e.target.value === "pass" })}
            >
              <option value="pass">Human: pass</option>
              <option value="fail">Human: fail</option>
            </select>
            <button
              type="button"
              aria-label={`Remove example ${i + 1}`}
              onClick={() => setRows((prev) => prev.filter((_, j) => j !== i))}
              className="text-xs text-gray-400 hover:text-red-600"
            >
              ✕
            </button>
          </div>
        ))}
      </div>
      <button
        type="button"
        data-testid="align-add-row"
        onClick={() => setRows((prev) => [...prev, { outputs: "", label: true }])}
        className="mt-2 text-xs font-medium text-caliber-purple hover:underline"
      >
        + Add example
      </button>

      {error && <div className="mt-3 text-sm text-red-600">{error}</div>}
      {result && (
        <div
          data-testid="align-result"
          className="mt-3 rounded-md border border-surface-200 bg-surface-50 px-3 py-2 text-sm"
        >
          <div className="flex flex-wrap items-center gap-4">
            <span>
              <span className="text-xs text-gray-500">Agreement </span>
              <span className="font-semibold text-gray-900">
                {Math.round(result.agreement_rate * 100)}%
              </span>
            </span>
            <span>
              <span className="text-xs text-gray-500">Cohen&apos;s κ </span>
              <span className="font-semibold text-gray-900">
                {result.cohen_kappa.toFixed(2)}
              </span>
            </span>
            <span className="text-xs text-gray-400">
              over {result.scored}/{result.n} examples
            </span>
          </div>
          <div className="mt-1 text-xs text-gray-500">
            FP {result.confusion.false_pos} · FN {result.confusion.false_neg}
          </div>
        </div>
      )}
      <div className="mt-4 flex justify-end">
        <button
          type="button"
          data-testid="align-run"
          disabled={running}
          onClick={() => void run()}
          className="text-sm font-medium text-white bg-caliber-purple px-3 py-1.5 rounded-md hover:bg-caliber-purple-dark disabled:opacity-50"
        >
          {running ? "Checking…" : "Check alignment"}
        </button>
      </div>
    </div>
  );
}

interface CreatePanelProps {
  onCancel: () => void;
  onSuccess: () => void;
  /** Model identifiers offered as <datalist> suggestions for the model field. */
  modelSuggestions?: string[];
}

function CreateJudgePanel({
  onCancel,
  onSuccess,
  modelSuggestions = [],
}: CreatePanelProps): JSX.Element {
  const [form, setForm] = useState<JudgeCreatePayload>({
    name: "",
    description: "",
    instructions: "",
    model: "openai:/gpt-4o-mini",
    feedback_value_type: "bool",
    tags: [],
  });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const instructionsValid = referencesVar(form.instructions);
  const canSubmit = Boolean(form.name) && instructionsValid && !submitting;

  const insertVar = (v: string): void =>
    setForm((f) => ({
      ...f,
      instructions: `${f.instructions}${f.instructions && !f.instructions.endsWith(" ") ? " " : ""}{{ ${v} }}`,
    }));

  const submit = async (): Promise<void> => {
    setSubmitting(true);
    setError(null);
    try {
      await caliberApi.createJudge(form);
      onSuccess();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "create failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="mb-6 bg-white rounded-lg border border-surface-200 p-4">
      <h2 className="text-sm font-semibold text-gray-900 mb-3">New judge</h2>
      <div className="grid grid-cols-2 gap-3">
        <Field
          label="Name"
          value={form.name}
          onChange={(v) => setForm({ ...form, name: v })}
          placeholder="answer-faithfulness"
        />
        <Field
          label="Model"
          value={form.model ?? ""}
          onChange={(v) => setForm({ ...form, model: v })}
          placeholder="openai:/gpt-4o-mini"
          list={modelSuggestions.length > 0 ? "judge-model-options" : undefined}
        />
        <datalist id="judge-model-options">
          {modelSuggestions.map((m) => (
            <option key={m} value={m} />
          ))}
        </datalist>
        <div className="col-span-2">
          <Field
            label="Description"
            value={form.description ?? ""}
            onChange={(v) => setForm({ ...form, description: v })}
            placeholder="Judges whether the answer is faithful to the expected answer."
          />
        </div>
        <div>
          <label className="text-xs text-gray-500 block mb-1">Returns</label>
          <select
            aria-label="Return type"
            className="w-full border border-surface-200 rounded-md px-3 py-1.5 text-sm bg-white"
            value={form.feedback_value_type ?? "bool"}
            onChange={(e) =>
              setForm({ ...form, feedback_value_type: e.target.value as JudgeValueType })
            }
          >
            {VALUE_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="mt-3">
        <div className="flex items-center justify-between mb-1">
          <label className="text-xs text-gray-500">Instructions</label>
          <div className="flex items-center gap-1.5">
            <span className="text-[10px] text-gray-400">insert:</span>
            {TEMPLATE_VARS.map((v) => (
              <button
                key={v}
                type="button"
                onClick={() => insertVar(v)}
                className="text-[10px] font-mono bg-surface-100 hover:bg-surface-200 text-gray-600 px-1.5 py-0.5 rounded"
              >
                {`{{ ${v} }}`}
              </button>
            ))}
          </div>
        </div>
        <textarea
          className="w-full border border-surface-200 rounded-md px-3 py-2 text-sm font-mono h-28 focus:ring-1 focus:ring-caliber-purple focus:border-caliber-purple"
          value={form.instructions}
          onChange={(e) => setForm({ ...form, instructions: e.target.value })}
          placeholder="Does {{ outputs }} faithfully answer {{ inputs }} given {{ expectations }}? Answer true or false."
        />
        {!instructionsValid && form.instructions.length > 0 && (
          <div className="mt-1 text-xs text-amber-600">
            Instructions must reference at least one variable:{" "}
            {TEMPLATE_VARS.map((v) => `{{ ${v} }}`).join(", ")}.
          </div>
        )}
      </div>

      {error && <div className="mt-3 text-sm text-red-600">{error}</div>}
      <div className="mt-4 flex gap-2 justify-end">
        <button
          type="button"
          onClick={onCancel}
          className="text-sm px-3 py-1.5 rounded-md text-gray-600 hover:bg-surface-100"
        >
          Cancel
        </button>
        <button
          type="button"
          disabled={!canSubmit}
          onClick={() => void submit()}
          className="text-sm font-medium text-white bg-caliber-purple px-3 py-1.5 rounded-md hover:bg-caliber-purple-dark disabled:opacity-50"
        >
          {submitting ? "Creating…" : "Create judge"}
        </button>
      </div>
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
  list,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  /** Optional <datalist> id for free-text suggestions. */
  list?: string;
}): JSX.Element {
  return (
    <div>
      <label className="text-xs text-gray-500 block mb-1">{label}</label>
      <input
        className="w-full border border-surface-200 rounded-md px-3 py-1.5 text-sm"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        list={list}
      />
    </div>
  );
}

function Chevron(): JSX.Element {
  return (
    <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M9 18l6-6-6-6" />
    </svg>
  );
}
