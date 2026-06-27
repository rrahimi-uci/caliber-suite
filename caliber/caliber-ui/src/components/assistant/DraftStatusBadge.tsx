/* DraftStatusBadge — colour-coded status pill for assistant drafts. */

import { cn } from "@/lib/utils";

const STATUS_STYLES: Record<string, string> = {
  draft: "bg-slate-100 text-slate-700",
  validating: "bg-blue-100 text-blue-700 animate-pulse",
  validated: "bg-emerald-100 text-emerald-700",
  validation_failed: "bg-red-100 text-red-700",
  testing: "bg-blue-100 text-blue-700 animate-pulse",
  tested: "bg-emerald-100 text-emerald-700",
  test_failed: "bg-red-100 text-red-700",
  approved: "bg-amber-100 text-amber-700",
  publishing: "bg-blue-100 text-blue-700 animate-pulse",
  published: "bg-green-100 text-green-800",
  publish_failed: "bg-red-100 text-red-700",
};

interface Props {
  status: string;
  className?: string;
}

export function DraftStatusBadge({ status, className }: Props): JSX.Element {
  return (
    <span
      className={cn(
        "inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium capitalize",
        STATUS_STYLES[status] ?? "bg-gray-100 text-gray-600",
        className,
      )}
    >
      {status.replace(/_/g, " ")}
    </span>
  );
}
