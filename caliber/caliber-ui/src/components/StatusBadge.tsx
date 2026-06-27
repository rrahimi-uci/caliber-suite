/**
 * Pill badge for queue-item / job / approval status.
 *
 * Unifies the status vocabulary across the verification queue, jobs, and
 * approvals so the same color/tone meaning is everywhere — "pending" is
 * always amber, "completed/verified" is always green, etc.
 */

const KNOWN_STATUSES = {
  pending: { tone: "bg-amber-100 text-amber-700", label: "Pending" },
  verified: { tone: "bg-green-100 text-green-700", label: "Verified" },
  dismissed: { tone: "bg-gray-100 text-gray-600", label: "Dismissed" },
  duplicate: { tone: "bg-gray-100 text-gray-600", label: "Duplicate" },
  queued: { tone: "bg-blue-100 text-blue-700", label: "Queued" },
  running: { tone: "bg-blue-100 text-blue-700", label: "Running" },
  awaiting_approval: { tone: "bg-amber-100 text-amber-700", label: "Awaiting Approval" },
  approved: { tone: "bg-green-100 text-green-700", label: "Approved" },
  completed: { tone: "bg-green-100 text-green-700", label: "Completed" },
  rejected: { tone: "bg-red-100 text-red-700", label: "Rejected" },
  failed: { tone: "bg-red-100 text-red-700", label: "Failed" },
  request_changes: { tone: "bg-amber-100 text-amber-700", label: "Changes Requested" },
} as const;

type KnownStatus = keyof typeof KNOWN_STATUSES;

interface StatusBadgeProps {
  status: string;
}

export function StatusBadge({ status }: StatusBadgeProps): JSX.Element {
  const known = (KNOWN_STATUSES as Record<string, { tone: string; label: string }>)[status];
  const tone = known?.tone ?? "bg-gray-100 text-gray-700";
  const label = known?.label ?? status;
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${tone}`}
    >
      {label}
    </span>
  );
}

export type { KnownStatus };
