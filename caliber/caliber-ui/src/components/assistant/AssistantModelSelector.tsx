import type { AssistantConfig } from "@/api/assistantTypes";

import {
  AssistantControlDropdown,
  AssistantControlDropdownOption,
  AssistantControlDropdownSection,
} from "./AssistantControlDropdown";
import {
  ASSISTANT_REASONING_OPTIONS,
  assistantCompactModelLabel,
  assistantModelDisplayName,
  assistantProviderLabel,
  normalizeAssistantReasoningValue,
} from "./assistantConfigUi";

interface AssistantModelSelectorProps {
  config: AssistantConfig | null;
  disabled?: boolean;
  isLoading?: boolean;
  isSaving?: boolean;
  onModelChange: (model: string) => void;
  onReasoningChange: (reasoning: string) => void;
}

export function AssistantModelSelector({
  config,
  disabled,
  isLoading,
  isSaving,
  onModelChange,
  onReasoningChange,
}: AssistantModelSelectorProps): JSX.Element {
  const selectedModel =
    config
      ? config.available_models.find((option) => option.id === config.model)
        ?? {
          id: config.model,
          name: config.model,
          provider: config.provider,
        }
      : null;
  const triggerLabel = isLoading ? "Loading…" : assistantCompactModelLabel(selectedModel);

  return (
    <AssistantControlDropdown
      align="right"
      ariaLabel="Aria model and reasoning effort"
      testId="assistant-model-selector"
      disabled={disabled || isLoading || !config}
      title={
        config
          ? `${assistantModelDisplayName(selectedModel)} · ${assistantProviderLabel(config.provider)}`
          : "Loading Aria model configuration"
      }
      value={triggerLabel}
      menuClassName="min-w-[320px] max-w-[340px]"
      icon={
        isSaving ? (
          <svg className="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.25" strokeWidth="2" />
            <path d="M21 12a9 9 0 00-9-9" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          </svg>
        ) : (
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4 w-4">
            <circle cx="12" cy="12" r="8" />
            <path d="M12 7v5l3 3" />
          </svg>
        )
      }
    >
      {({ closeMenu }) => (
        <div className="space-y-2">
          <div className="space-y-1">
            <AssistantControlDropdownSection>Reasoning effort</AssistantControlDropdownSection>
            {ASSISTANT_REASONING_OPTIONS.map((option) => (
              <AssistantControlDropdownOption
                key={option.value || "default"}
                label={option.label}
                description={option.description}
                selected={normalizeAssistantReasoningValue(config?.reasoning) === option.value}
                onClick={() => {
                  onReasoningChange(option.value);
                  closeMenu();
                }}
              />
            ))}
          </div>

          <div className="space-y-1">
            <AssistantControlDropdownSection>Model</AssistantControlDropdownSection>
            <div className="max-h-64 space-y-1 overflow-y-auto pr-1">
              {(config?.available_models ?? []).map((model) => (
                <AssistantControlDropdownOption
                  key={model.id}
                  label={assistantModelDisplayName(model)}
                  description={model.id}
                  secondaryLabel={assistantProviderLabel(model.provider)}
                  selected={config?.model === model.id}
                  onClick={() => {
                    onModelChange(model.id);
                    closeMenu();
                  }}
                />
              ))}
            </div>
          </div>
        </div>
      )}
    </AssistantControlDropdown>
  );
}
