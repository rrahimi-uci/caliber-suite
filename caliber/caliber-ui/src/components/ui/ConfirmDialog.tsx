import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description?: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  destructive?: boolean;
  /** When true, require a non-empty reason and pass it to onConfirm (used for gate overrides). */
  requireReason?: boolean;
  reasonLabel?: string;
  busy?: boolean;
  onConfirm: (reason: string) => void;
  onCancel: () => void;
  "data-testid"?: string;
}

/**
 * A small, dependency-free confirm modal — the project has no shared dialog
 * primitive (only hand-rolled inline ones), so this is the shared extraction.
 * Used by the Version panel's rollback confirm and the promote-past-FAIL
 * acknowledgment (which requires a typed reason).
 */
export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  destructive = false,
  requireReason = false,
  reasonLabel = "Reason",
  busy = false,
  onConfirm,
  onCancel,
  "data-testid": testId = "confirm-dialog",
}: ConfirmDialogProps): JSX.Element | null {
  const [reason, setReason] = useState("");
  const cancelRef = useRef<HTMLButtonElement>(null);

  // Reset the typed reason whenever the dialog (re)opens, and move focus in.
  useEffect(() => {
    if (open) {
      setReason("");
      cancelRef.current?.focus();
    }
  }, [open]);

  if (!open) return null;

  const reasonMissing = requireReason && reason.trim().length === 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-label={title}
      data-testid={testId}
      onKeyDown={(event) => {
        if (event.key === "Escape") onCancel();
      }}
    >
      <div className="w-full max-w-md rounded-lg bg-white p-5 shadow-xl">
        <h2 className="text-base font-semibold text-gray-900">{title}</h2>
        {description ? (
          <div className="mt-2 text-sm text-gray-600">{description}</div>
        ) : null}
        {requireReason ? (
          <label className="mt-3 block text-sm">
            <span className="mb-1 block font-medium text-gray-700">{reasonLabel}</span>
            <textarea
              data-testid="confirm-dialog-reason"
              className={cn(
                "w-full rounded-md border border-surface-200 px-2 py-1.5 text-sm",
                "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-caliber-purple",
              )}
              rows={3}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
        ) : null}
        <div className="mt-5 flex justify-end gap-2">
          <Button
            ref={cancelRef}
            variant="outline"
            size="sm"
            onClick={onCancel}
            disabled={busy}
            data-testid="confirm-dialog-cancel"
          >
            {cancelLabel}
          </Button>
          <Button
            variant={destructive ? "destructive" : "default"}
            size="sm"
            onClick={() => onConfirm(reason.trim())}
            disabled={busy || reasonMissing}
            data-testid="confirm-dialog-confirm"
          >
            {confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}
