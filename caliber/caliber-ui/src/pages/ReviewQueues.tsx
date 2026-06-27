/**
 * Review Queues — structured human review of traces (CALIBER-native).
 *
 * A queue defines a label schema of review questions; reviewers answer them per
 * trace and the answers are written back onto the trace as MLflow assessments
 * (feedback) or expectations (ground truth) via the OSS primitives. This is the
 * open-source analogue of MLflow's Databricks-only Review Queues.
 */

import { useCallback, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, caliberApi } from "@/api/caliberApi";
import type {
  ReviewItem,
  ReviewQuestion,
  ReviewQuestionTarget,
  ReviewQuestionType,
  ReviewQueue,
  ReviewQueueCreatePayload,
} from "@/api/types";
import { useApi } from "@/hooks/useApi";
import { relativeTime } from "@/lib/time";

export function ReviewQueues(): JSX.Element {
  const [openQueueId, setOpenQueueId] = useState<string | null>(null);

  if (openQueueId) {
    return (
      <QueueDetail queueId={openQueueId} onBack={() => setOpenQueueId(null)} />
    );
  }
  return <QueueList onOpen={setOpenQueueId} />;
}

/* -------------------------------------------------------------------------- */
/* List + create                                                               */
/* -------------------------------------------------------------------------- */

function QueueList({ onOpen }: { onOpen: (id: string) => void }): JSX.Element {
  const fetcher = useCallback(
    (signal: AbortSignal) => caliberApi.listReviewQueues({ status: "active" }, signal),
    [],
  );
  const { data, error, loading, refresh } = useApi(fetcher, []);
  const [showCreate, setShowCreate] = useState(false);

  return (
    <>
      <div className="flex items-center gap-2 text-sm text-gray-500 mb-4">
        <Link to="/" className="hover:text-gray-700">
          Dashboard
        </Link>
        <Chevron />
        <span className="text-gray-900 font-medium">Review Queues</span>
      </div>

      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-semibold text-gray-900">Review Queues</h1>
          <p className="text-sm text-gray-500 mt-0.5">
            Structured human review of traces. Answers are written back onto each
            trace as MLflow assessments &amp; expectations.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setShowCreate((v) => !v)}
          className="text-sm font-medium text-white bg-caliber-purple px-3 py-1.5 rounded-md hover:bg-caliber-purple-dark"
        >
          {showCreate ? "Cancel" : "+ New Queue"}
        </button>
      </div>

      {error && (
        <div className="mb-4 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          <div className="font-medium">Failed to load review queues</div>
          <div className="text-xs mt-0.5">{error.message}</div>
        </div>
      )}

      {showCreate && (
        <CreateQueuePanel
          onCancel={() => setShowCreate(false)}
          onSuccess={() => {
            setShowCreate(false);
            refresh();
          }}
        />
      )}

      <div className="bg-white rounded-lg border border-surface-200 overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-surface-200 bg-surface-50">
              <th className="text-left font-medium px-4 py-3">Name</th>
              <th className="text-left font-medium px-4 py-3">Questions</th>
              <th className="text-left font-medium px-4 py-3">Reviewers</th>
              <th className="text-left font-medium px-4 py-3">Progress</th>
              <th className="text-left font-medium px-4 py-3">Updated</th>
              <th className="text-right font-medium px-4 py-3">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-surface-100">
            {loading && !data && (
              <tr>
                <td colSpan={6} className="px-4 py-10 text-center text-sm text-gray-500">
                  Loading…
                </td>
              </tr>
            )}
            {data && data.length === 0 && (
              <tr>
                <td colSpan={6} className="px-4 py-10 text-center text-sm text-gray-500">
                  No review queues yet.
                </td>
              </tr>
            )}
            {(data ?? []).map((queue) => (
              <tr key={queue.queue_id} className="hover:bg-surface-50">
                <td className="px-4 py-3">
                  <button
                    type="button"
                    onClick={() => onOpen(queue.queue_id)}
                    className="font-medium text-gray-900 hover:text-caliber-purple hover:underline"
                  >
                    {queue.name}
                  </button>
                  <div className="text-xs text-gray-500 mt-0.5 max-w-md truncate">
                    {queue.description || "—"}
                  </div>
                </td>
                <td className="px-4 py-3 text-gray-600">{queue.questions.length}</td>
                <td className="px-4 py-3 text-gray-600 text-xs">
                  {queue.reviewers.length ? queue.reviewers.join(", ") : "—"}
                </td>
                <td className="px-4 py-3">
                  <ProgressPill queue={queue} />
                </td>
                <td className="px-4 py-3 text-xs text-gray-500">
                  {relativeTime(queue.updated_at)}
                </td>
                <td className="px-4 py-3 text-right">
                  <button
                    type="button"
                    onClick={() => onOpen(queue.queue_id)}
                    className="text-xs font-medium text-caliber-purple hover:underline"
                  >
                    Open
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function ProgressPill({ queue }: { queue: ReviewQueue }): JSX.Element {
  const total = queue.item_count ?? 0;
  const pending = queue.pending_count ?? 0;
  const done = total - pending;
  if (total === 0) {
    return <span className="text-xs text-gray-400">no items</span>;
  }
  const complete = pending === 0;
  return (
    <span
      className={`text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded ${
        complete ? "bg-emerald-100 text-emerald-700" : "bg-amber-100 text-amber-700"
      }`}
    >
      {done}/{total} reviewed
    </span>
  );
}

const QUESTION_TYPES: ReviewQuestionType[] = [
  "pass_fail",
  "categorical",
  "numeric",
  "text",
];

function emptyQuestion(): ReviewQuestion {
  return {
    key: "",
    title: "",
    type: "pass_fail",
    options: [],
    required: true,
    target: "feedback",
  };
}

function CreateQueuePanel({
  onCancel,
  onSuccess,
}: {
  onCancel: () => void;
  onSuccess: () => void;
}): JSX.Element {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [reviewers, setReviewers] = useState("");
  const [questions, setQuestions] = useState<ReviewQuestion[]>([emptyQuestion()]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const updateQuestion = (i: number, patch: Partial<ReviewQuestion>): void =>
    setQuestions((qs) => qs.map((q, idx) => (idx === i ? { ...q, ...patch } : q)));

  const validQuestions = questions.every((q) => q.key.trim() && q.title.trim());
  const canSubmit = Boolean(name.trim()) && validQuestions && !submitting;

  const submit = async (): Promise<void> => {
    setSubmitting(true);
    setError(null);
    const payload: ReviewQueueCreatePayload = {
      name: name.trim(),
      description: description.trim(),
      reviewers: reviewers
        .split(",")
        .map((r) => r.trim())
        .filter(Boolean),
      questions: questions.map((q) => ({
        ...q,
        key: q.key.trim(),
        title: q.title.trim(),
        options:
          q.type === "categorical"
            ? q.options.map((o) => o.trim()).filter(Boolean)
            : [],
      })),
    };
    try {
      await caliberApi.createReviewQueue(payload);
      onSuccess();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "create failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="mb-6 bg-white rounded-lg border border-surface-200 p-4">
      <h2 className="text-sm font-semibold text-gray-900 mb-3">New review queue</h2>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Name" value={name} onChange={setName} placeholder="answer-quality" />
        <Field
          label="Reviewers (comma-separated)"
          value={reviewers}
          onChange={setReviewers}
          placeholder="@sarah, @alex"
        />
        <div className="col-span-2">
          <Field
            label="Description"
            value={description}
            onChange={setDescription}
            placeholder="Human review of answer correctness and tone."
          />
        </div>
      </div>

      <div className="mt-4">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-semibold text-gray-700 uppercase tracking-wide">
            Questions
          </span>
          <button
            type="button"
            onClick={() => setQuestions((qs) => [...qs, emptyQuestion()])}
            className="text-xs font-medium text-caliber-purple hover:underline"
          >
            + Add question
          </button>
        </div>
        <div className="space-y-2">
          {questions.map((q, i) => (
            <div
              key={i}
              className="rounded-md border border-surface-200 p-3 grid grid-cols-12 gap-2 items-start"
            >
              <input
                aria-label={`Question ${i + 1} key`}
                className="col-span-2 border border-surface-200 rounded px-2 py-1 text-xs font-mono"
                placeholder="key"
                value={q.key}
                onChange={(e) => updateQuestion(i, { key: e.target.value })}
              />
              <input
                aria-label={`Question ${i + 1} title`}
                className="col-span-4 border border-surface-200 rounded px-2 py-1 text-sm"
                placeholder="Question shown to the reviewer"
                value={q.title}
                onChange={(e) => updateQuestion(i, { title: e.target.value })}
              />
              <select
                aria-label={`Question ${i + 1} type`}
                className="col-span-2 border border-surface-200 rounded px-2 py-1 text-xs bg-white"
                value={q.type}
                onChange={(e) =>
                  updateQuestion(i, { type: e.target.value as ReviewQuestionType })
                }
              >
                {QUESTION_TYPES.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
              <select
                aria-label={`Question ${i + 1} target`}
                className="col-span-2 border border-surface-200 rounded px-2 py-1 text-xs bg-white"
                value={q.target}
                onChange={(e) =>
                  updateQuestion(i, { target: e.target.value as ReviewQuestionTarget })
                }
              >
                <option value="feedback">feedback</option>
                <option value="expectation">expectation</option>
              </select>
              <button
                type="button"
                aria-label={`Remove question ${i + 1}`}
                disabled={questions.length === 1}
                onClick={() =>
                  setQuestions((qs) => qs.filter((_, idx) => idx !== i))
                }
                className="col-span-2 text-xs text-gray-400 hover:text-red-600 disabled:opacity-30 text-right"
              >
                Remove
              </button>
              {q.type === "categorical" && (
                <input
                  aria-label={`Question ${i + 1} options`}
                  className="col-span-12 border border-surface-200 rounded px-2 py-1 text-xs"
                  placeholder="options, comma-separated (e.g. none, hallucination, refusal)"
                  value={q.options.join(", ")}
                  onChange={(e) =>
                    updateQuestion(i, { options: e.target.value.split(",") })
                  }
                />
              )}
            </div>
          ))}
        </div>
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
          {submitting ? "Creating…" : "Create queue"}
        </button>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Detail + review                                                             */
/* -------------------------------------------------------------------------- */

function QueueDetail({
  queueId,
  onBack,
}: {
  queueId: string;
  onBack: () => void;
}): JSX.Element {
  const fetcher = useCallback(
    (signal: AbortSignal) => caliberApi.getReviewQueue(queueId, signal),
    [queueId],
  );
  const { data, error, loading, refresh } = useApi(fetcher, [queueId]);
  const [selectedItemId, setSelectedItemId] = useState<string | null>(null);
  const [addTraces, setAddTraces] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);

  const queue = data?.queue;
  const items = data?.items ?? [];
  const selected =
    items.find((it) => it.item_id === selectedItemId) ??
    items.find((it) => it.status === "pending") ??
    null;

  const enqueue = async (): Promise<void> => {
    const traceIds = addTraces
      .split(/[\s,]+/)
      .map((t) => t.trim())
      .filter(Boolean);
    if (!traceIds.length) return;
    setActionError(null);
    try {
      await caliberApi.addReviewItems(queueId, { trace_ids: traceIds });
      setAddTraces("");
      refresh();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "failed to add traces");
    }
  };

  return (
    <>
      <div className="flex items-center gap-2 text-sm text-gray-500 mb-4">
        <button type="button" onClick={onBack} className="hover:text-gray-700">
          Review Queues
        </button>
        <Chevron />
        <span className="text-gray-900 font-medium">{queue?.name ?? queueId}</span>
      </div>

      {error && (
        <div className="mb-4 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error.message}
        </div>
      )}
      {loading && !data && <div className="text-sm text-gray-500">Loading…</div>}

      {queue && (
        <>
          <div className="mb-5">
            <h1 className="text-xl font-semibold text-gray-900">{queue.name}</h1>
            <p className="text-sm text-gray-500 mt-0.5">{queue.description || "—"}</p>
          </div>

          <div className="mb-4 bg-white rounded-lg border border-surface-200 p-4">
            <label className="text-xs text-gray-500 block mb-1">
              Add traces to review (ids, comma or space separated)
            </label>
            <div className="flex gap-2">
              <input
                aria-label="Trace ids"
                className="flex-1 border border-surface-200 rounded-md px-3 py-1.5 text-sm font-mono"
                value={addTraces}
                onChange={(e) => setAddTraces(e.target.value)}
                placeholder="tr-abc123 tr-def456"
              />
              <button
                type="button"
                onClick={() => void enqueue()}
                disabled={!addTraces.trim()}
                className="text-sm font-medium text-white bg-caliber-purple px-3 py-1.5 rounded-md hover:bg-caliber-purple-dark disabled:opacity-50"
              >
                Enqueue
              </button>
            </div>
            {actionError && (
              <div className="mt-2 text-sm text-red-600">{actionError}</div>
            )}
          </div>

          <div className="grid grid-cols-3 gap-4">
            <div className="col-span-1 bg-white rounded-lg border border-surface-200 overflow-hidden">
              <div className="px-3 py-2 text-xs font-semibold uppercase tracking-wide text-gray-500 border-b border-surface-200 bg-surface-50">
                Items ({items.length})
              </div>
              <div className="divide-y divide-surface-100 max-h-[60vh] overflow-y-auto">
                {items.length === 0 && (
                  <div className="px-3 py-6 text-center text-sm text-gray-400">
                    No traces queued yet.
                  </div>
                )}
                {items.map((item) => (
                  <button
                    key={item.item_id}
                    type="button"
                    onClick={() => setSelectedItemId(item.item_id)}
                    className={`w-full text-left px-3 py-2 hover:bg-surface-50 ${
                      selected?.item_id === item.item_id ? "bg-violet-50" : ""
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-mono text-xs text-gray-700 truncate">
                        {item.trace_id}
                      </span>
                      <ItemStatus status={item.status} />
                    </div>
                  </button>
                ))}
              </div>
            </div>

            <div className="col-span-2">
              {selected ? (
                <ReviewForm
                  key={selected.item_id}
                  queueId={queueId}
                  item={selected}
                  questions={queue.questions}
                  onSubmitted={() => {
                    setSelectedItemId(null);
                    refresh();
                  }}
                />
              ) : (
                <div className="bg-white rounded-lg border border-surface-200 p-8 text-center text-sm text-gray-400">
                  {items.length
                    ? "All items reviewed. 🎉"
                    : "Enqueue traces, then review them here."}
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </>
  );
}

function ItemStatus({ status }: { status: ReviewItem["status"] }): JSX.Element {
  const cls =
    status === "completed"
      ? "bg-emerald-100 text-emerald-700"
      : status === "skipped"
        ? "bg-gray-200 text-gray-600"
        : "bg-amber-100 text-amber-700";
  return (
    <span className={`text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded ${cls}`}>
      {status}
    </span>
  );
}

function ReviewForm({
  queueId,
  item,
  questions,
  onSubmitted,
}: {
  queueId: string;
  item: ReviewItem;
  questions: ReviewQuestion[];
  onSubmitted: () => void;
}): JSX.Element {
  const [answers, setAnswers] = useState<Record<string, unknown>>(
    () => ({ ...item.answers }),
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const readOnly = item.status === "completed";

  const setAnswer = (key: string, value: unknown): void =>
    setAnswers((a) => ({ ...a, [key]: value }));

  const submit = async (): Promise<void> => {
    setSubmitting(true);
    setError(null);
    try {
      await caliberApi.submitReviewItem(queueId, item.item_id, answers);
      onSubmitted();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "submit failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="bg-white rounded-lg border border-surface-200 p-5">
      <div className="flex items-center justify-between mb-4">
        <div>
          <div className="text-xs text-gray-500">Reviewing trace</div>
          <div className="font-mono text-sm text-gray-900">{item.trace_id}</div>
        </div>
        <ItemStatus status={item.status} />
      </div>

      <div className="space-y-4">
        {questions.map((q) => (
          <div key={q.key}>
            <label className="text-sm font-medium text-gray-800 block mb-1">
              {q.title}
              {q.required && <span className="text-red-500 ml-0.5">*</span>}
              <span className="ml-2 text-[10px] font-normal uppercase text-gray-400">
                {q.target}
              </span>
            </label>
            <QuestionInput
              question={q}
              value={answers[q.key]}
              disabled={readOnly}
              onChange={(v) => setAnswer(q.key, v)}
            />
          </div>
        ))}
      </div>

      {error && <div className="mt-3 text-sm text-red-600">{error}</div>}

      {!readOnly && (
        <div className="mt-5 flex justify-end">
          <button
            type="button"
            disabled={submitting}
            onClick={() => void submit()}
            className="text-sm font-medium text-white bg-caliber-purple px-4 py-1.5 rounded-md hover:bg-caliber-purple-dark disabled:opacity-50"
          >
            {submitting ? "Submitting…" : "Submit review"}
          </button>
        </div>
      )}
      {readOnly && (
        <div className="mt-4 text-xs text-gray-400">
          Reviewed by {item.completed_by} · wrote {item.assessment_ids.length}{" "}
          assessment(s) back to the trace.
        </div>
      )}
    </div>
  );
}

function QuestionInput({
  question,
  value,
  disabled,
  onChange,
}: {
  question: ReviewQuestion;
  value: unknown;
  disabled: boolean;
  onChange: (value: unknown) => void;
}): JSX.Element {
  if (question.type === "pass_fail") {
    return (
      <div className="flex gap-2">
        {[
          { label: "Pass", val: true },
          { label: "Fail", val: false },
        ].map((opt) => (
          <button
            key={opt.label}
            type="button"
            disabled={disabled}
            onClick={() => onChange(opt.val)}
            className={`px-3 py-1.5 rounded-md text-sm font-medium border disabled:opacity-60 ${
              value === opt.val
                ? opt.val
                  ? "bg-emerald-600 text-white border-emerald-600"
                  : "bg-red-600 text-white border-red-600"
                : "bg-white text-gray-600 border-surface-200 hover:bg-surface-50"
            }`}
          >
            {opt.label}
          </button>
        ))}
      </div>
    );
  }
  if (question.type === "categorical") {
    return (
      <select
        aria-label={question.title}
        disabled={disabled}
        className="w-full border border-surface-200 rounded-md px-3 py-1.5 text-sm bg-white disabled:opacity-60"
        value={typeof value === "string" ? value : ""}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">Select…</option>
        {question.options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    );
  }
  if (question.type === "numeric") {
    return (
      <input
        aria-label={question.title}
        type="number"
        disabled={disabled}
        className="w-full border border-surface-200 rounded-md px-3 py-1.5 text-sm disabled:opacity-60"
        value={typeof value === "number" ? value : ""}
        onChange={(e) =>
          onChange(e.target.value === "" ? null : Number(e.target.value))
        }
      />
    );
  }
  return (
    <textarea
      aria-label={question.title}
      disabled={disabled}
      className="w-full border border-surface-200 rounded-md px-3 py-2 text-sm h-20 disabled:opacity-60"
      value={typeof value === "string" ? value : ""}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
}): JSX.Element {
  return (
    <div>
      <label className="text-xs text-gray-500 block mb-1">{label}</label>
      <input
        className="w-full border border-surface-200 rounded-md px-3 py-1.5 text-sm"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
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
