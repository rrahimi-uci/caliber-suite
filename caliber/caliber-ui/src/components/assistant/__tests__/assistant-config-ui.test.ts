import { describe, expect, it } from "vitest";

import {
  ASSISTANT_REASONING_OPTIONS,
  assistantCompactModelLabel,
  assistantModelDisplayName,
  assistantProviderLabel,
  assistantReasoningDescription,
  assistantReasoningLabel,
  normalizeAssistantReasoningValue,
} from "@/components/assistant/assistantConfigUi";

describe("assistantConfigUi", () => {
  it("publishes the canonical reasoning-effort option set", () => {
    expect(ASSISTANT_REASONING_OPTIONS.map((option) => option.value)).toEqual([
      "",
      "low",
      "medium",
      "high",
    ]);
  });

  it.each([
    [undefined, ""],
    [null, ""],
    ["", ""],
    ["default", ""],
    ["none", ""],
    ["low", "low"],
    ["LOW", "low"],
    [" medium ", "medium"],
    ["high", "high"],
    ["minimal", "low"],
    ["xhigh", "high"],
    ["unknown", ""],
  ])("normalizes reasoning value %p -> %p", (raw, expected) => {
    expect(normalizeAssistantReasoningValue(raw)).toBe(expected);
  });

  it.each([
    [undefined, "Default"],
    [null, "Default"],
    ["", "Default"],
    ["default", "Default"],
    ["low", "Low"],
    ["minimal", "Low"],
    ["medium", "Medium"],
    ["high", "High"],
    ["xhigh", "High"],
    ["unknown", "Default"],
  ])("maps reasoning label for %p", (raw, expected) => {
    expect(assistantReasoningLabel(raw)).toBe(expected);
  });

  it.each([
    [undefined, "Use the model's built-in reasoning effort."],
    [null, "Use the model's built-in reasoning effort."],
    ["", "Use the model's built-in reasoning effort."],
    ["none", "Use the model's built-in reasoning effort."],
    ["low", "Fast response with light reasoning effort."],
    ["minimal", "Fast response with light reasoning effort."],
    ["medium", "Balanced reasoning effort for most work."],
    ["high", "More deliberate reasoning for complex changes."],
    ["xhigh", "More deliberate reasoning for complex changes."],
    ["unknown", "Use the model's built-in reasoning effort."],
  ])("maps reasoning description for %p", (raw, expected) => {
    expect(assistantReasoningDescription(raw)).toBe(expected);
  });

  it.each([
    ["openai", "OpenAI"],
    ["anthropic", "Anthropic"],
    ["ollama", "Ollama"],
    ["custom", "custom"],
    ["  gateway  ", "gateway"],
    [undefined, "Unknown"],
    [null, "Unknown"],
    ["", "Unknown"],
  ])("maps provider label for %p", (raw, expected) => {
    expect(assistantProviderLabel(raw)).toBe(expected);
  });

  it.each([
    [undefined, "Model"],
    [null, "Model"],
    [{ id: "gpt-5.4", name: "GPT-5.4", provider: "openai" as const }, "GPT-5.4"],
    [{ id: "custom-model", name: "", provider: "ollama" as const }, "custom-model"],
    [{ id: "custom-model", name: "Custom Model", provider: "ollama" as const }, "Custom Model"],
  ])("uses the right display name for %p", (option, expected) => {
    expect(assistantModelDisplayName(option)).toBe(expected);
  });

  it.each([
    [undefined, "Model"],
    [null, "Model"],
    [{ id: "gpt-5.4", name: "GPT-5.4", provider: "openai" as const }, "5.4"],
    [{ id: "gpt-4o-mini", name: "GPT-4o Mini", provider: "openai" as const }, "4o mini"],
    [{ id: "o3", name: "o3", provider: "openai" as const }, "o3"],
    [{ id: "o3-pro", name: "o3-pro", provider: "openai" as const }, "o3-pro"],
    [{ id: "o4-mini", name: "o4-mini", provider: "openai" as const }, "o4-mini"],
    [
      { id: "claude-sonnet-4-20250514", name: "Claude Sonnet 4", provider: "anthropic" as const },
      "Sonnet 4",
    ],
    [
      { id: "claude-opus-4-20250514", name: "Claude Opus 4", provider: "anthropic" as const },
      "Opus 4",
    ],
    [
      { id: "claude-3-5-sonnet-20241022", name: "Claude 3.5 Sonnet", provider: "anthropic" as const },
      "3.5 Sonnet",
    ],
    [
      { id: "claude-3-5-haiku-20241022", name: "Claude 3.5 Haiku", provider: "anthropic" as const },
      "3.5 Haiku",
    ],
    [
      { id: "custom", name: "Claude Research", provider: "anthropic" as const },
      "Research",
    ],
    [
      { id: "custom", name: "GPT-Preview", provider: "openai" as const },
      "Preview",
    ],
    [
      { id: "llama3.1", name: "Llama 3.1 70B", provider: "ollama" as const },
      "Llama 3.1 70B",
    ],
  ])("computes compact model label for %p", (option, expected) => {
    expect(assistantCompactModelLabel(option)).toBe(expected);
  });
});
