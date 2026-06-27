/* QuestionList — renders clarifying questions from the assistant. */

import type { ClarifyingQuestion } from "@/api/assistantTypes";

import { AriaLogo } from "./AriaLogo";

interface Props {
  questions: ClarifyingQuestion[];
  onAnswer: (answer: string) => void;
}

export function QuestionList({ questions, onAnswer }: Props): JSX.Element | null {
  if (questions.length === 0) return null;

  return (
    <div className="mr-auto max-w-[92%] space-y-3 rounded-2xl border border-slate-200/80 bg-white px-4 py-3 shadow-sm dark:border-slate-700 dark:bg-slate-900">
      <div className="flex items-center gap-2.5">
        <AriaLogo className="h-8 w-8 ring-1 ring-slate-200 dark:ring-slate-700" alt="" />
        <div className="min-w-0">
          <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-slate-500 dark:text-slate-400">
            Aria
          </p>
          <p className="text-xs font-semibold text-slate-800 dark:text-slate-100">
            Clarifying questions
          </p>
        </div>
      </div>
      {questions.map((q, i) => (
        <div key={i} className="space-y-1.5">
          <p className="text-sm text-slate-700 dark:text-slate-200">{q.question}</p>
          {q.options.length > 0 ? (
            <div className="flex flex-wrap gap-1.5">
              {q.options.map((opt) => (
                <button
                  key={opt}
                  type="button"
                  onClick={() => onAnswer(opt)}
                  className="rounded-full border border-caliber-200 bg-caliber-50 px-3 py-1.5 text-xs font-medium text-caliber-700 transition-colors hover:bg-caliber-100 dark:border-caliber-400/30 dark:bg-caliber-500/15 dark:text-caliber-100 dark:hover:bg-caliber-500/25"
                >
                  {opt}
                </button>
              ))}
            </div>
          ) : null}
        </div>
      ))}
    </div>
  );
}
