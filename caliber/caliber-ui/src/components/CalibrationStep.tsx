/**
 * CalibrationStep — a numbered step section for the two-step Calibration flow
 * shared by Prompts and Skills: ① build a test set, ② run the optimizer/run.
 * Keeping this in one place means both pages frame the flow identically.
 */

import type { ReactNode } from "react";

export interface CalibrationStepProps {
  index: number;
  title: string;
  description: string;
  children: ReactNode;
}

export function CalibrationStep({
  index,
  title,
  description,
  children,
}: CalibrationStepProps): JSX.Element {
  return (
    <section className="space-y-4">
      <div className="flex items-start gap-3">
        <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-caliber-600 text-sm font-semibold text-white shadow-sm">
          {index}
        </span>
        <div className="pt-0.5">
          <h3 className="text-sm font-semibold text-slate-900">{title}</h3>
          <p className="text-xs text-slate-500 leading-relaxed">{description}</p>
        </div>
      </div>
      <div className="md:pl-10">{children}</div>
    </section>
  );
}

/** The downward "flows into" divider rendered between step ① and step ②. */
export function StepConnector(): JSX.Element {
  return (
    <div className="flex items-center justify-center gap-2 text-slate-300" aria-hidden="true">
      <span className="h-px w-16 bg-slate-200" />
      <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <path d="M12 5v14M5 12l7 7 7-7" />
      </svg>
      <span className="h-px w-16 bg-slate-200" />
    </div>
  );
}
