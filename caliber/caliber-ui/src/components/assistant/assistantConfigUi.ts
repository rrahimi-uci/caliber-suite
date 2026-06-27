import type { AssistantModelOption } from "@/api/assistantTypes";

export interface AssistantReasoningOption {
  value: string;
  label: string;
  description: string;
}

const ASSISTANT_REASONING_ALIASES: Record<string, string> = {
  default: "",
  none: "",
  minimal: "low",
  xhigh: "high",
};

export const ASSISTANT_REASONING_OPTIONS: AssistantReasoningOption[] = [
  {
    value: "",
    label: "Default",
    description: "Use the model's built-in reasoning effort.",
  },
  {
    value: "low",
    label: "Low",
    description: "Fast response with light reasoning effort.",
  },
  {
    value: "medium",
    label: "Medium",
    description: "Balanced reasoning effort for most work.",
  },
  {
    value: "high",
    label: "High",
    description: "More deliberate reasoning for complex changes.",
  },
];

export function normalizeAssistantReasoningValue(
  value: string | null | undefined,
): string {
  const normalized = (value ?? "").trim().toLowerCase();
  if (!normalized) return "";
  const alias = ASSISTANT_REASONING_ALIASES[normalized];
  if (alias !== undefined) return alias;
  return ASSISTANT_REASONING_OPTIONS.some((option) => option.value === normalized)
    ? normalized
    : "";
}

export function assistantReasoningLabel(value: string | null | undefined): string {
  const match = ASSISTANT_REASONING_OPTIONS.find(
    (option) => option.value === normalizeAssistantReasoningValue(value),
  );
  return match?.label ?? "Default";
}

export function assistantReasoningDescription(value: string | null | undefined): string {
  const match = ASSISTANT_REASONING_OPTIONS.find(
    (option) => option.value === normalizeAssistantReasoningValue(value),
  );
  return match?.description ?? "Use the model's built-in reasoning effort.";
}

export function assistantProviderLabel(provider: string | null | undefined): string {
  if (provider === "openai") return "OpenAI";
  if (provider === "anthropic") return "Anthropic";
  if (provider === "ollama") return "Ollama";
  return provider?.trim() || "Unknown";
}

export function assistantModelDisplayName(option: AssistantModelOption | null | undefined): string {
  return option?.name?.trim() || option?.id || "Model";
}

export function assistantCompactModelLabel(option: AssistantModelOption | null | undefined): string {
  if (!option) return "Model";

  const modelId = option.id.trim();
  const fallbackName = option.name?.trim() || modelId;
  const normalized = modelId.toLowerCase();

  if (normalized.startsWith("gpt-")) {
    return modelId.slice(4).replace(/-/g, " ");
  }
  if (normalized === "o3" || normalized === "o3-pro" || normalized === "o4-mini") {
    return modelId;
  }
  if (normalized.startsWith("claude-sonnet-4")) return "Sonnet 4";
  if (normalized.startsWith("claude-opus-4")) return "Opus 4";
  if (normalized.startsWith("claude-3-5-sonnet")) return "3.5 Sonnet";
  if (normalized.startsWith("claude-3-5-haiku")) return "3.5 Haiku";
  if (/^claude\s+/i.test(fallbackName)) {
    return fallbackName.replace(/^claude\s+/i, "");
  }
  if (/^gpt-/i.test(fallbackName)) {
    return fallbackName.replace(/^gpt-/i, "");
  }
  return fallbackName;
}
