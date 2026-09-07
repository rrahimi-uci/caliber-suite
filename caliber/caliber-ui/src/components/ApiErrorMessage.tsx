/**
 * ApiErrorMessage — the one place an API failure is rendered.
 *
 * Shows the server's summary, and beneath it the per-field detail the backend
 * already returns for request-schema failures (see ``@/lib/apiErrors``). A
 * non-validation error renders as the summary alone — there is no structure to
 * show and inventing one would be worse than the plain message.
 *
 * Uses ``role="alert"`` so the failure is announced when it appears, which is
 * the behaviour a form rejection needs: the user has just submitted and their
 * focus is elsewhere.
 */

import { describeApiError } from "@/lib/apiErrors";
import { cn } from "@/lib/utils";

export interface ApiErrorMessageProps {
  /** The thrown value. `null`/`undefined` renders nothing. */
  error: unknown;
  /**
   * Per-surface overrides from a server field path to the label this form
   * actually uses, e.g. `{ artifact_ref: "Artifact ID or registry name" }`.
   */
  labels?: Readonly<Record<string, string>>;
  /** Shown instead of the server summary when the error is not an ApiError. */
  fallback?: string;
  className?: string;
  "data-testid"?: string;
}

export function ApiErrorMessage({
  error,
  labels,
  fallback,
  className,
  "data-testid": testId = "api-error",
}: ApiErrorMessageProps): JSX.Element | null {
  if (error === null || error === undefined) return null;

  const described = describeApiError(error, labels);
  const summary =
    described.status === null && fallback ? fallback : described.summary;

  return (
    <div
      role="alert"
      data-testid={testId}
      data-error-status={described.status ?? undefined}
      className={cn(
        "rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-800",
        "dark:border-red-500/40 dark:bg-red-500/10 dark:text-red-200",
        className,
      )}
    >
      <p className="font-medium">{summary}</p>
      {described.issues.length > 0 && (
        <ul
          data-testid={`${testId}-fields`}
          className="mt-2 list-disc space-y-1 pl-5 text-[13px] font-normal"
        >
          {described.issues.map((issue) => (
            <li key={`${issue.path}:${issue.message}`} data-field-path={issue.path}>
              {issue.label ? (
                <>
                  <span className="font-medium">{issue.label}</span>
                  {": "}
                </>
              ) : null}
              {issue.message}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
