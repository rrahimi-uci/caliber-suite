/**
 * Preview labels: a pill for navigation and page headers, and a banner for the top of a
 * page. Both say the same thing everywhere, so "Preview" means one thing (alpha gap P1-10):
 * the feature works, but it is outside the supported alpha journey and may change.
 */

import type { ReactNode } from "react";

export function PreviewBadge({ className = "" }: { className?: string }): JSX.Element {
  return (
    <span
      data-testid="preview-badge"
      title="Preview: works, but outside the supported alpha journey and may change"
      className={`inline-flex items-center rounded-full bg-violet-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-violet-700 ${className}`}
    >
      Preview
    </span>
  );
}

export function PreviewBanner({ children }: { children?: ReactNode }): JSX.Element {
  return (
    <div
      role="note"
      data-testid="preview-banner"
      className="mb-4 rounded-lg border border-violet-200 bg-violet-50 px-4 py-2.5 text-sm text-violet-900"
    >
      <strong className="font-semibold">Preview.</strong> This area works, but it is outside the
      supported alpha journey and may change.
      {children ? <> {children}</> : null}
    </div>
  );
}
