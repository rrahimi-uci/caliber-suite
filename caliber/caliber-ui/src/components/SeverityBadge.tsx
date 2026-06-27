/**
 * Pill badge for severity. Tone follows the mockup palette: red for
 * critical (action-required), gray for standard.
 */

import type { Severity } from "@/api/types";

interface SeverityBadgeProps {
  severity: Severity;
}

const TONE: Record<Severity, string> = {
  critical: "bg-red-100 text-red-700",
  standard: "bg-gray-100 text-gray-700",
};

const LABEL: Record<Severity, string> = {
  critical: "Critical",
  standard: "Standard",
};

export function SeverityBadge({ severity }: SeverityBadgeProps): JSX.Element {
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${TONE[severity]}`}
      aria-label={`${LABEL[severity]} severity`}
    >
      {LABEL[severity]}
    </span>
  );
}
