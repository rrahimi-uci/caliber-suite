/**
 * ApprovalModeSelector — compact control for how far Aria can auto-advance a
 * draft through the validate -> test -> approve -> publish gates.
 */

import {
  ASSISTANT_APPROVAL_MODES,
  ASSISTANT_APPROVAL_MODE_HINTS,
  ASSISTANT_APPROVAL_MODE_LABELS,
} from "@/api/assistantTypes";
import type { AssistantApprovalMode } from "@/api/assistantTypes";
import type { AssistantConfig } from "@/api/assistantTypes";

import { AssistantControlDropdown, AssistantControlDropdownOption } from "./AssistantControlDropdown";

interface ApprovalModeSelectorProps {
  value: AssistantApprovalMode;
  onChange: (mode: AssistantApprovalMode) => void;
  disabled?: boolean;
  autonomy?: AssistantConfig["autonomy"];
}

export function ApprovalModeSelector({ value, onChange, disabled, autonomy }: ApprovalModeSelectorProps): JSX.Element {
  return (
    <AssistantControlDropdown
      ariaLabel="How should Aria's actions be approved?"
      testId="assistant-approval-selector"
      disabled={disabled}
      title={ASSISTANT_APPROVAL_MODE_HINTS[value]}
      tone="warning"
      value={ASSISTANT_APPROVAL_MODE_LABELS[value]}
      icon={
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4 w-4">
          <path d="M12 3l7 3v5c0 5-3.5 8.5-7 10-3.5-1.5-7-5-7-10V6l7-3z" />
          <path d="M12 8v4" />
          <circle cx="12" cy="15.5" r="0.8" fill="currentColor" stroke="none" />
        </svg>
      }
      menuClassName="min-w-[300px]"
    >
      {({ closeMenu }) => (
        <div className="space-y-1">
          {ASSISTANT_APPROVAL_MODES.map((mode) => {
            const unavailable =
              (mode === "agent_review" && autonomy?.agent_review_ready === false) ||
              (mode === "full_autonomy" && autonomy?.full_autonomy_ready === false);
            return (
              <AssistantControlDropdownOption
                key={mode}
                label={ASSISTANT_APPROVAL_MODE_LABELS[mode]}
                description={ASSISTANT_APPROVAL_MODE_HINTS[mode]}
                disabled={unavailable}
                secondaryLabel={unavailable ? "Not configured" : undefined}
                selected={mode === value}
                onClick={() => {
                  onChange(mode);
                  closeMenu();
                }}
              />
            );
          })}
        </div>
      )}
    </AssistantControlDropdown>
  );
}
