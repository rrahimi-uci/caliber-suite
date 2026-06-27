/**
 * ModeSelector — compact turn-mode control for Aria. The chosen mode is sent
 * with each message and persisted on the session server-side.
 */

import {
  ASSISTANT_MODES,
  ASSISTANT_MODE_HINTS,
  ASSISTANT_MODE_LABELS,
  ASSISTANT_MODE_CAPTIONS,
} from "@/api/assistantTypes";
import type { AssistantMode } from "@/api/assistantTypes";

import {
  AssistantControlDropdown,
  AssistantControlDropdownOption,
} from "./AssistantControlDropdown";

interface ModeSelectorProps {
  value: AssistantMode;
  onChange: (mode: AssistantMode) => void;
  disabled?: boolean;
}

export function ModeSelector({ value, onChange, disabled }: ModeSelectorProps): JSX.Element {
  return (
    <AssistantControlDropdown
      ariaLabel="Interaction mode"
      testId="assistant-mode-selector"
      disabled={disabled}
      title={ASSISTANT_MODE_HINTS[value]}
      value={ASSISTANT_MODE_LABELS[value]}
      menuClassName="min-w-[260px]"
    >
      {({ closeMenu }) => (
        <div className="space-y-1">
          {ASSISTANT_MODES.map((mode) => (
            <AssistantControlDropdownOption
              key={mode}
              label={ASSISTANT_MODE_LABELS[mode]}
              description={ASSISTANT_MODE_CAPTIONS[mode]}
              selected={mode === value}
              onClick={() => {
                onChange(mode);
                closeMenu();
              }}
            />
          ))}
        </div>
      )}
    </AssistantControlDropdown>
  );
}
