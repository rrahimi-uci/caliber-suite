import type { SkillRuntimeMode } from "@/api/assistantTypes";

const DEFAULT_SKILL_MODE_KEY = "caliber.assistant.defaults.skillMode";

export function readDefaultAssistantSkillMode(): SkillRuntimeMode {
  if (typeof window === "undefined") return "auto";
  const raw = window.localStorage.getItem(DEFAULT_SKILL_MODE_KEY);
  return raw === "manual" || raw === "off" ? raw : "auto";
}

export function writeDefaultAssistantSkillMode(mode: SkillRuntimeMode): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(DEFAULT_SKILL_MODE_KEY, mode);
}

