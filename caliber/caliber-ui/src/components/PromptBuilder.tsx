import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { Copy, LayoutTemplate, PencilLine, Sparkles } from "lucide-react";

import { caliberApi } from "@/api/caliberApi";
import { LIVE_ALIAS, SINGLE_ENVIRONMENT } from "@/lib/environment";
import type {
  PromptCreateResult,
  PromptElementName,
  PromptInfo,
  PromptTemplateCatalog,
  PromptTemplateDefinition,
  PromptTemplatePreviewResult,
  PromptTemplateStarterRecipe,
  PromptVersionInfo,
} from "@/api/types";

// The five canonical prompt elements, in render order, with display labels.
// This is the backbone of the element-level editor: each element can be left
// as the template/behavior default ("base") or overridden wholesale.
const PROMPT_ELEMENTS: ReadonlyArray<{
  name: PromptElementName;
  label: string;
}> = [
  { name: "instruction", label: "Instruction" },
  { name: "context", label: "Context" },
  { name: "examples", label: "Examples" },
  { name: "input", label: "Input" },
  { name: "output_indicator", label: "Output indicator" },
];


const DEPLOYMENT_ALIASES = [
  {
    value: "staging",
    label: "@staging",
    description: "Safe default for testing and calibration before promotion.",
  },
  {
    value: "prod",
    label: "@prod",
    description: "Deploy live immediately.",
  },
  {
    value: "dev",
    label: "@dev",
    description: "Scratch space for early drafts.",
  },
] as const;

const STEPS = [
  { n: 1, label: "Start" },
  { n: 2, label: "Compose" },
  { n: 3, label: "Save" },
] as const;

type StepNumber = 1 | 2 | 3;

// Creating a prompt is several different jobs. The builder opens on an intent
// fork so each one gets the right on-ramp instead of a single template-first
// funnel: paste an existing prompt, build from a template, describe it, or
// clone an existing registry prompt as a variant. "paste", "template", and
// "clone" converge on the same create call.
type BuilderMode = "fork" | "paste" | "template" | "describe" | "clone";

const STEP_SUBTITLE: Record<StepNumber, string> = {
  1: "Pick the starting template that matches the job this prompt should do.",
  2: "Layer optional behavior, then fill in the template fields.",
  3: "Review the compiled prompt, then name it and choose where to save.",
};

const DEFAULT_PREVIEW_VARIABLES_TEXT =
  '{\n  "user_input": "Example user request"\n}';

interface PromptBuilderProps {
  prefillName?: string;
  onCancel: () => void;
  onCreated: (
    result: PromptCreateResult,
    options: { openCalibration: boolean },
  ) => void;
}

interface PreviewRequestDraft {
  baseTemplateId: string;
  modifierIds: string[];
  builderValues: Record<string, string>;
  previewVariablesText: string;
  runtimeVariablesText: string;
  templateOverride: string;
  sectionOverrides: Partial<Record<PromptElementName, string>>;
}

function buildPreviewRequest({
  baseTemplateId,
  modifierIds,
  builderValues,
  previewVariablesText,
  runtimeVariablesText,
  templateOverride,
  sectionOverrides,
}: PreviewRequestDraft) {
  if (!baseTemplateId) {
    return { ok: false as const, error: "Select a base template first." };
  }
  const parsedPreview = parsePreviewVariables(previewVariablesText);
  if (!parsedPreview.ok) {
    return { ok: false as const, error: parsedPreview.error };
  }
  return {
    ok: true as const,
    payload: {
      base_template_id: baseTemplateId,
      modifier_ids: modifierIds,
      builder_values: builderValues,
      preview_variables: parsedPreview.value,
      runtime_variables: parseRuntimeVariables(runtimeVariablesText),
      template_override: templateOverride.trim() || undefined,
      section_overrides: sectionOverrides,
    },
  };
}

export function PromptBuilder({
  prefillName = "",
  onCancel,
  onCreated,
}: PromptBuilderProps): JSX.Element {
  const [mode, setMode] = useState<BuilderMode>("fork");
  const [pastedTemplate, setPastedTemplate] = useState("");
  // "Describe it": the assistant drafts a starting prompt, then hands off to the
  // manual builder so the user runs the same compose/validate/save/calibrate.
  const [describeText, setDescribeText] = useState("");
  const [drafting, setDrafting] = useState(false);
  const [draftError, setDraftError] = useState<string | null>(null);
  const [draftedFromAssistant, setDraftedFromAssistant] = useState(false);
  // "Start from existing": clone a deployed registry prompt into a NEW name as a
  // variant/branch. The fetched template lands in the shared paste editing
  // surface so the user can tweak it before saving via the same create call.
  const [cloneSources, setCloneSources] = useState<PromptInfo[]>([]);
  const [cloneSourcesLoading, setCloneSourcesLoading] = useState(false);
  const [cloneSourceName, setCloneSourceName] = useState("");
  const [cloneVersions, setCloneVersions] = useState<PromptVersionInfo[]>([]);
  const [cloneVersion, setCloneVersion] = useState<number | null>(null);
  const [cloneLoading, setCloneLoading] = useState(false);
  const [cloneError, setCloneError] = useState<string | null>(null);
  // Provenance of the loaded clone, surfaced in the UI and stamped into tags +
  // the default commit message: "Forked from {source} v{n}".
  const [cloneProvenance, setCloneProvenance] = useState<{
    source: string;
    version: number;
  } | null>(null);
  const [step, setStep] = useState<StepNumber>(1);
  const [catalog, setCatalog] = useState<PromptTemplateCatalog | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [templateSearch, setTemplateSearch] = useState("");
  // Goal-first gallery filters: pick the job (domain) and method(s) before
  // browsing, so the catalog narrows to relevant starting points. Method is
  // multi-select — narrowing by any combination of techniques, not just one.
  const [goalFilter, setGoalFilter] = useState("");
  const [methodFilters, setMethodFilters] = useState<string[]>([]);
  // The prompt's own classification (Save step). These default to the base
  // template's domain/technique but are fully user-owned: the goal can be any
  // custom text, and methods can be any set of techniques — not only the base's
  // or the "compatible" ones.
  const [promptGoal, setPromptGoal] = useState("");
  const [promptMethods, setPromptMethods] = useState<string[]>([]);
  const [customMethodText, setCustomMethodText] = useState("");
  const [selectedStarterRecipeId, setSelectedStarterRecipeId] = useState("");
  const [baseTemplateId, setBaseTemplateId] = useState("");
  const [modifierIds, setModifierIds] = useState<string[]>([]);
  // Per-element overrides (instruction/context/examples/input/output_indicator).
  // A key is present only once the user has edited that element; "Reset"
  // removes the key and the element falls back to the composed default.
  const [sectionOverrides, setSectionOverrides] = useState<
    Partial<Record<PromptElementName, string>>
  >({});
  const [builderValues, setBuilderValues] = useState<Record<string, string>>(
    {},
  );
  const [runtimeVariablesText, setRuntimeVariablesText] = useState("");
  const [previewVariablesText, setPreviewVariablesText] = useState(
    DEFAULT_PREVIEW_VARIABLES_TEXT,
  );
  const [templateOverride, setTemplateOverride] = useState("");
  const [preview, setPreview] = useState<PromptTemplatePreviewResult | null>(
    null,
  );
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [promptName, setPromptName] = useState(prefillName);
  const [commitMessage, setCommitMessage] = useState("");
  // Single-environment mode: every prompt deploys to the one live alias and the
  // selector below is hidden. Multi-env restores the staging-first default.
  const [targetAlias, setTargetAlias] = useState(SINGLE_ENVIRONMENT ? LIVE_ALIAS : "staging");
  const [openCalibrationAfterCreate, setOpenCalibrationAfterCreate] =
    useState(true);
  const [createError, setCreateError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const previewRequestId = useRef(0);

  const baseTemplates = useMemo(() => catalog?.base_templates ?? [], [catalog]);
  const modifiers = useMemo(() => catalog?.modifiers ?? [], [catalog]);
  const starterRecipes = useMemo(
    () => catalog?.starter_recipes ?? [],
    [catalog],
  );
  const selectedBase =
    baseTemplates.find((template) => template.id === baseTemplateId) ?? null;
  const selectedStarterRecipe =
    starterRecipes.find((recipe) => recipe.id === selectedStarterRecipeId) ??
    null;
  const modifierTitles = useMemo(
    () => new Map(modifiers.map((modifier) => [modifier.id, modifier.title])),
    [modifiers],
  );

  // Goal (domain) and Method (technique) facets, derived from everything the
  // catalog actually offers, so the filter chips never show a dead option.
  const availableGoals = useMemo(() => {
    const goals = new Set<string>();
    for (const template of baseTemplates) {
      if (template.domain) goals.add(template.domain);
    }
    for (const recipe of starterRecipes) {
      if (recipe.domain) goals.add(recipe.domain);
    }
    return [...goals].sort();
  }, [baseTemplates, starterRecipes]);
  const availableMethods = useMemo(() => {
    const methods = new Set<string>();
    for (const template of baseTemplates) {
      if (template.technique) methods.add(template.technique);
    }
    for (const recipe of starterRecipes) {
      if (recipe.technique) methods.add(recipe.technique);
    }
    return [...methods].sort();
  }, [baseTemplates, starterRecipes]);

  const filteredBaseTemplates = useMemo(() => {
    const query = templateSearch.trim().toLowerCase();
    return baseTemplates.filter((template) => {
      if (goalFilter && template.domain !== goalFilter) return false;
      // Multi-select: a template matches if its technique is any selected method.
      if (methodFilters.length > 0 && !methodFilters.includes(template.technique))
        return false;
      if (!query) return true;
      return [
        template.title,
        template.summary,
        template.technique,
        template.domain,
      ]
        .filter(Boolean)
        .some((field) => String(field).toLowerCase().includes(query));
    });
  }, [baseTemplates, templateSearch, goalFilter, methodFilters]);
  const filteredStarterRecipes = useMemo(() => {
    const query = templateSearch.trim().toLowerCase();
    return starterRecipes.filter((recipe) => {
      if (goalFilter && recipe.domain !== goalFilter) return false;
      if (methodFilters.length > 0 && !methodFilters.includes(recipe.technique))
        return false;
      if (!query) return true;
      return [
        recipe.title,
        recipe.summary,
        recipe.technique,
        recipe.domain,
        recipe.support_reason,
        ...(recipe.composable_with ?? []),
      ]
        .filter(Boolean)
        .some((field) => String(field).toLowerCase().includes(query));
    });
  }, [starterRecipes, templateSearch, goalFilter, methodFilters]);

  // Seed the prompt's own classification from the chosen base. The user can
  // then override the goal with any custom text and pick any set of methods on
  // the Save step — independent of the base's defaults.
  useEffect(() => {
    if (!baseTemplateId) return;
    const base = baseTemplates.find((item) => item.id === baseTemplateId);
    if (!base) return;
    setPromptGoal(base.domain ?? "");
    setPromptMethods(base.technique ? [base.technique] : []);
  }, [baseTemplateId, baseTemplates]);

  const builderStarterRecipes = useMemo(
    () =>
      filteredStarterRecipes.filter(
        (recipe) => recipe.support_level === "builder",
      ),
    [filteredStarterRecipes],
  );
  const workflowOnlyRecipes = useMemo(
    () =>
      filteredStarterRecipes.filter(
        (recipe) => recipe.support_level === "workflow_only",
      ),
    [filteredStarterRecipes],
  );
  const filteredCoreTemplates = useMemo(
    () =>
      filteredBaseTemplates.filter(
        (template) => template.source_kind !== "library",
      ),
    [filteredBaseTemplates],
  );
  const recommendedModifierIds = useMemo(() => {
    if (selectedStarterRecipe?.suggested_modifier_ids.length) {
      return selectedStarterRecipe.suggested_modifier_ids;
    }
    return selectedBase?.recommended_modifiers ?? [];
  }, [selectedBase, selectedStarterRecipe]);

  // Step 2/3 are only reachable once a base template is chosen.
  const maxReachableStep: StepNumber = baseTemplateId ? 3 : 1;
  const previewReady = preview?.validation_report.valid ?? false;

  // The backend reports unfilled required fields as validation errors
  // ("Builder field 'x' is required."). Those aren't failures — they're just
  // not done yet — so split them out and show them as a friendly to-do list
  // (using the field's label), keeping the red "Errors" block for real problems.
  const { fieldsToFill, blockingErrors } = useMemo(() => {
    const errors = preview?.validation_report.errors ?? [];
    const labelByName = new Map(
      (preview?.builder_variables ?? []).map((variable) => [
        variable.name,
        variable.label,
      ]),
    );
    const toFill: string[] = [];
    const blocking: string[] = [];
    for (const message of errors) {
      const match = /^Builder field '(.+)' is required\.$/.exec(message);
      if (match) {
        toFill.push(labelByName.get(match[1]!) ?? match[1]!);
      } else {
        blocking.push(message);
      }
    }
    return { fieldsToFill: toFill, blockingErrors: blocking };
  }, [preview]);

  const previewStatus = previewLoading
    ? "Updating…"
    : !preview
      ? "Waiting"
      : previewReady
        ? "Ready"
        : blockingErrors.length > 0
          ? "Needs review"
          : "Incomplete";

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      setCatalogLoading(true);
      setCatalogError(null);
      try {
        const loaded = await caliberApi.getPromptTemplateLibrary();
        if (cancelled) return;
        setCatalog(loaded);
        const firstRecipe = loaded.starter_recipes[0] ?? null;
        if (firstRecipe?.base_template_id) {
          setSelectedStarterRecipeId((current) => current || firstRecipe.id);
          setBaseTemplateId(
            (current) => current || firstRecipe.base_template_id || "",
          );
          setModifierIds((current) =>
            current.length > 0 ? current : [...firstRecipe.modifier_ids],
          );
          setBuilderValues((current) =>
            Object.keys(current).length > 0
              ? current
              : { ...firstRecipe.builder_values },
          );
          setRuntimeVariablesText((current) =>
            current || firstRecipe.runtime_variables.join("\n"),
          );
          setPreviewVariablesText((current) =>
            current !== DEFAULT_PREVIEW_VARIABLES_TEXT
              ? current
              : Object.keys(firstRecipe.preview_variables).length > 0
                ? JSON.stringify(firstRecipe.preview_variables, null, 2)
                : DEFAULT_PREVIEW_VARIABLES_TEXT,
          );
          setTemplateOverride((current) =>
            current || firstRecipe.template_override?.trim() || "",
          );
        } else {
          setBaseTemplateId(
            (current) => current || loaded.base_templates[0]?.id || "",
          );
        }
      } catch (err) {
        if (!cancelled) {
          setCatalogError(
            err instanceof Error
              ? err.message
              : "Failed to load prompt template catalog",
          );
        }
      } finally {
        if (!cancelled) {
          setCatalogLoading(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!catalog || !baseTemplateId) return;
    setModifierIds((current) =>
      current.filter((modifierId) => {
        const modifier = catalog.modifiers.find(
          (item) => item.id === modifierId,
        );
        if (!modifier) return false;
        if (
          modifier.compatible_base_ids.length > 0 &&
          !modifier.compatible_base_ids.includes(baseTemplateId)
        ) {
          return false;
        }
        return true;
      }),
    );
  }, [catalog, baseTemplateId]);

  useEffect(() => {
    if (!baseTemplateId) {
      setPreview(null);
      setPreviewError(null);
      setPreviewLoading(false);
      return;
    }

    const nextRequest = buildPreviewRequest({
      baseTemplateId,
      modifierIds,
      builderValues,
      previewVariablesText,
      runtimeVariablesText,
      templateOverride,
      sectionOverrides,
    });
    if (!nextRequest.ok) {
      setPreviewError(nextRequest.error);
      setPreviewLoading(false);
      return;
    }

    const timeoutId = window.setTimeout(() => {
      const requestId = previewRequestId.current + 1;
      previewRequestId.current = requestId;
      setPreviewLoading(true);
      setPreviewError(null);

      void caliberApi
        .previewPromptTemplate(nextRequest.payload)
        .then((result) => {
          if (previewRequestId.current !== requestId) return;
          setPreview(result);
        })
        .catch((err) => {
          if (previewRequestId.current !== requestId) return;
          setPreviewError(
            err instanceof Error
              ? err.message
              : "Failed to compile prompt preview",
          );
        })
        .finally(() => {
          if (previewRequestId.current === requestId) {
            setPreviewLoading(false);
          }
        });
    }, 250);

    return () => {
      window.clearTimeout(timeoutId);
    };
  }, [
    baseTemplateId,
    builderValues,
    modifierIds,
    previewVariablesText,
    runtimeVariablesText,
    templateOverride,
    sectionOverrides,
  ]);

  const handleModifierToggle = (modifierId: string) => {
    const modifier = modifiers.find((item) => item.id === modifierId);
    if (!modifier) return;

    if (modifierIds.includes(modifierId)) {
      setModifierIds((current) =>
        current.filter((item) => item !== modifierId),
      );
      return;
    }

    if (
      modifier.compatible_base_ids.length > 0 &&
      !modifier.compatible_base_ids.includes(baseTemplateId)
    ) {
      return;
    }

    const selectedModifierSet = new Set(modifierIds);
    if (
      modifier.incompatible_modifier_ids.some((item) =>
        selectedModifierSet.has(item),
      )
    ) {
      return;
    }

    const blockedBySelected = modifiers.some(
      (item) =>
        selectedModifierSet.has(item.id) &&
        item.incompatible_modifier_ids.includes(modifierId),
    );
    if (blockedBySelected) {
      return;
    }

    setModifierIds((current) => [...current, modifierId]);
  };

  const goToStep = (next: StepNumber) => {
    if (next > maxReachableStep) return;
    setCreateError(null);
    setStep(next);
  };

  const handleBaseTemplateSelect = (templateId: string) => {
    setSelectedStarterRecipeId("");
    setBaseTemplateId(templateId);
    setModifierIds([]);
    setBuilderValues({});
    setRuntimeVariablesText("");
    setPreviewVariablesText(DEFAULT_PREVIEW_VARIABLES_TEXT);
    setTemplateOverride("");
    setSectionOverrides({});
    setDraftedFromAssistant(false);
  };

  function applyStarterRecipe(recipe: PromptTemplateStarterRecipe) {
    if (recipe.support_level !== "builder" || !recipe.base_template_id) return;
    setSelectedStarterRecipeId(recipe.id);
    setBaseTemplateId(recipe.base_template_id);
    setModifierIds([...recipe.modifier_ids]);
    setBuilderValues({ ...recipe.builder_values });
    setRuntimeVariablesText(recipe.runtime_variables.join("\n"));
    setPreviewVariablesText(
      Object.keys(recipe.preview_variables).length > 0
        ? JSON.stringify(recipe.preview_variables, null, 2)
        : DEFAULT_PREVIEW_VARIABLES_TEXT,
    );
    setTemplateOverride(recipe.template_override?.trim() ?? "");
    setSectionOverrides({});
    setDraftedFromAssistant(false);
  }

  // ---- element-level override helpers (Pillar B) ------------------------
  const composedSections: Record<string, string> =
    preview?.composed_sections ?? {};
  const overriddenElements = useMemo(
    () => PROMPT_ELEMENTS.filter((el) => el.name in sectionOverrides),
    [sectionOverrides],
  );

  const setElementOverride = (name: PromptElementName, value: string) => {
    setSectionOverrides((current) => ({ ...current, [name]: value }));
  };

  const resetElementOverride = (name: PromptElementName) => {
    setSectionOverrides((current) => {
      const next = { ...current };
      delete next[name];
      return next;
    });
  };

  const moveModifier = (index: number, direction: -1 | 1) => {
    setModifierIds((current) => {
      const target = index + direction;
      if (target < 0 || target >= current.length) return current;
      const next = [...current];
      const [moved] = next.splice(index, 1);
      next.splice(target, 0, moved!);
      return next;
    });
  };

  // Human-readable changeset surfaced on Save — the diff from the base
  // template (Pillar D). Behaviors layered, elements overridden, manual pass.
  const changeset = useMemo(() => {
    const entries: string[] = [];
    for (const id of modifierIds) {
      const modifier = modifiers.find((item) => item.id === id);
      entries.push(`Layered behavior: ${modifier?.title ?? id}`);
    }
    for (const el of overriddenElements) {
      entries.push(`${el.label} overridden`);
    }
    const baseDomain = selectedBase?.domain ?? "";
    const baseTechnique = selectedBase?.technique ?? "";
    const goal = promptGoal.trim();
    if (goal && goal !== baseDomain) {
      entries.push(`Goal: ${goal}`);
    }
    const methodsChanged =
      promptMethods.length !== 1 || promptMethods[0] !== baseTechnique;
    if (promptMethods.length > 0 && methodsChanged) {
      entries.push(`Methods: ${promptMethods.join(", ")}`);
    }
    if (templateOverride.trim()) {
      entries.push("Manual full-template override");
    }
    return entries;
  }, [
    modifierIds,
    modifiers,
    overriddenElements,
    templateOverride,
    promptGoal,
    promptMethods,
    selectedBase?.domain,
    selectedBase?.technique,
  ]);

  const togglePromptMethod = (value: string): void => {
    setPromptMethods((current) =>
      current.includes(value)
        ? current.filter((item) => item !== value)
        : [...current, value],
    );
  };

  const addCustomMethod = (): void => {
    const value = customMethodText.trim();
    if (!value) return;
    setPromptMethods((current) =>
      current.includes(value) ? current : [...current, value],
    );
    setCustomMethodText("");
  };

  const submit = async () => {
    if (!promptName.trim()) {
      setCreateError("Prompt name is required.");
      return;
    }

    setCreating(true);
    setCreateError(null);
    try {
      const nextRequest = buildPreviewRequest({
        baseTemplateId,
        modifierIds,
        builderValues,
        previewVariablesText,
        runtimeVariablesText,
        templateOverride,
        sectionOverrides,
      });
      if (!nextRequest.ok) {
        setCreateError(nextRequest.error);
        return;
      }
      const latestPreview = await caliberApi.previewPromptTemplate(
        nextRequest.payload,
      );
      setPreview(latestPreview);
      if (!latestPreview.validation_report.valid) {
        setCreateError("Fix the validation errors before creating the prompt.");
        return;
      }

      const tags: Record<string, string> = {
        "caliber.builder.catalog_version":
          catalog?.catalog_version ?? "unknown",
        "caliber.builder.base_template": baseTemplateId,
      };
      if (promptGoal.trim()) {
        tags["caliber.builder.goal"] = promptGoal.trim();
      }
      if (promptMethods.length > 0) {
        tags["caliber.builder.methods"] = promptMethods.join(",");
      }
      if (modifierIds.length > 0) {
        tags["caliber.builder.modifiers"] = modifierIds.join(",");
      }
      if (overriddenElements.length > 0) {
        tags["caliber.builder.section_overrides"] = overriddenElements
          .map((el) => el.name)
          .join(",");
      }
      if (templateOverride.trim()) {
        tags["caliber.builder.override"] = "true";
      }

      const created = await caliberApi.createPrompt({
        name: promptName.trim(),
        template: latestPreview.compiled_template,
        commit_message: commitMessage.trim() || undefined,
        tags,
      });
      await caliberApi.promotePrompt(created.name, created.version, {
        alias: targetAlias,
        gate_state: "none",
        overridden: true,
        override_reason: "initial prompt activation from Prompt Builder",
      });
      onCreated(
        { ...created, alias_changed: true, active_alias: targetAlias },
        { openCalibration: openCalibrationAfterCreate },
      );
    } catch (err) {
      setCreateError(
        err instanceof Error ? err.message : "Failed to create prompt",
      );
    } finally {
      setCreating(false);
    }
  };

  // Runtime placeholders detected in a pasted prompt, surfaced as a hint so the
  // user can see what will be filled at call time.
  const detectedPasteVars = useMemo(() => {
    const found = new Set<string>();
    for (const match of pastedTemplate.matchAll(
      /\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g,
    )) {
      found.add(match[1]!);
    }
    return [...found];
  }, [pastedTemplate]);

  // The fast path: register a prompt the user already wrote, no template
  // machinery — just name it, pick where it lands, and ship.
  const submitPaste = async () => {
    if (!promptName.trim()) {
      setCreateError("Prompt name is required.");
      return;
    }
    if (!pastedTemplate.trim()) {
      setCreateError("Prompt text is required.");
      return;
    }
    setCreating(true);
    setCreateError(null);
    try {
      const created = await caliberApi.createPrompt({
        name: promptName.trim(),
        template: pastedTemplate,
        commit_message: commitMessage.trim() || undefined,
        tags: { "caliber.builder.source": "paste" },
      });
      await caliberApi.promotePrompt(created.name, created.version, {
        alias: targetAlias,
        gate_state: "none",
        overridden: true,
        override_reason: "initial pasted-prompt activation",
      });
      onCreated(
        { ...created, alias_changed: true, active_alias: targetAlias },
        { openCalibration: openCalibrationAfterCreate },
      );
    } catch (err) {
      setCreateError(
        err instanceof Error ? err.message : "Failed to create prompt",
      );
    } finally {
      setCreating(false);
    }
  };

  // "Start from existing": enter clone mode and load the deployed-prompt picker.
  // Only ``has_prompt`` rows are clonable — promptless backlog rows have no
  // registry content to fork.
  const enterCloneMode = () => {
    setMode("clone");
    setCreateError(null);
    setCloneError(null);
    setCloneSourcesLoading(true);
    void caliberApi
      .listPrompts()
      .then((rows) => {
        setCloneSources(rows.filter((row) => row.has_prompt));
      })
      .catch((err) => {
        setCloneError(
          err instanceof Error ? err.message : "Failed to load existing prompts.",
        );
      })
      .finally(() => {
        setCloneSourcesLoading(false);
      });
  };

  // Fetch a source prompt's version's full template text and prefill it into the
  // shared paste editing surface, suggesting a unique ``{source}-variant`` name.
  const loadCloneTemplate = async (sourceName: string, version: number) => {
    setCloneLoading(true);
    setCloneError(null);
    try {
      const detail = await caliberApi.getPromptVersion(sourceName, version);
      setPastedTemplate(detail.template);
      setCloneProvenance({ source: sourceName, version });
      setCommitMessage((current) =>
        current.trim() ? current : `Forked from ${sourceName} v${version}`,
      );
      setPromptName((current) =>
        current.trim() ? current : `${sourceName}-variant`,
      );
    } catch (err) {
      setCloneError(
        err instanceof Error ? err.message : "Failed to load the source prompt.",
      );
    } finally {
      setCloneLoading(false);
    }
  };

  // Source selection → list its versions, default to the latest, and load it.
  const selectCloneSource = async (sourceName: string) => {
    setCloneSourceName(sourceName);
    setCloneVersions([]);
    setCloneVersion(null);
    setCloneProvenance(null);
    if (!sourceName) return;
    setCloneLoading(true);
    setCloneError(null);
    try {
      const versions = await caliberApi.listPromptVersions(sourceName);
      setCloneVersions(versions);
      const latest = versions[0]?.version ?? null;
      if (latest != null) {
        setCloneVersion(latest);
        await loadCloneTemplate(sourceName, latest);
      } else {
        setCloneError("That prompt has no versions to clone.");
      }
    } catch (err) {
      setCloneError(
        err instanceof Error ? err.message : "Failed to load prompt versions.",
      );
    } finally {
      setCloneLoading(false);
    }
  };

  const selectCloneVersion = async (version: number) => {
    setCloneVersion(version);
    if (cloneSourceName) {
      await loadCloneTemplate(cloneSourceName, version);
    }
  };

  // Clone into a NEW prompt name (a variant/branch), never a new version of the
  // source. Reuses the paste create call; stamps clone provenance into tags.
  const submitClone = async () => {
    if (!promptName.trim()) {
      setCreateError("Prompt name is required.");
      return;
    }
    if (cloneSourceName && promptName.trim() === cloneSourceName) {
      setCreateError(
        "Choose a new name — a clone is a variant, not a new version of the source.",
      );
      return;
    }
    if (!pastedTemplate.trim()) {
      setCreateError("Prompt text is required.");
      return;
    }
    setCreating(true);
    setCreateError(null);
    try {
      const tags: Record<string, string> = { "caliber.builder.source": "clone" };
      if (cloneProvenance) {
        tags["caliber.builder.forked_from"] = cloneProvenance.source;
        tags["caliber.builder.forked_from_version"] = String(
          cloneProvenance.version,
        );
      }
      const created = await caliberApi.createPrompt({
        name: promptName.trim(),
        template: pastedTemplate,
        commit_message: commitMessage.trim() || undefined,
        tags,
      });
      await caliberApi.promotePrompt(created.name, created.version, {
        alias: targetAlias,
        gate_state: "none",
        overridden: true,
        override_reason: "initial cloned-prompt activation",
      });
      onCreated(
        { ...created, alias_changed: true, active_alias: targetAlias },
        { openCalibration: openCalibrationAfterCreate },
      );
    } catch (err) {
      setCreateError(
        err instanceof Error ? err.message : "Failed to create prompt",
      );
    } finally {
      setCreating(false);
    }
  };

  // "Describe it": ask the CALIBER assistant to draft a prompt, then seed the
  // manual builder with it. From there the flow is identical to a hand-authored
  // prompt — compose/elements, the same validation preview, save, and the same
  // "Open Prompt Calibration" handoff.
  const submitDescribe = async () => {
    if (!describeText.trim()) {
      setDraftError("Describe the task first.");
      return;
    }
    setDrafting(true);
    setDraftError(null);
    try {
      const draft = await caliberApi.draftPromptFromDescription({
        description: describeText.trim(),
      });
      const template = draft.template.trim() || describeText.trim();
      // Seed the custom-prompt base so the drafted text lands in the manual
      // builder as an editable freeform prompt.
      setSelectedStarterRecipeId("");
      setBaseTemplateId("custom-prompt");
      setModifierIds([]);
      setSectionOverrides({});
      setBuilderValues({ custom_prompt: template });
      setRuntimeVariablesText(draft.variables.join("\n"));
      setPreviewVariablesText(DEFAULT_PREVIEW_VARIABLES_TEXT);
      setTemplateOverride("");
      if (draft.name && !promptName.trim()) {
        setPromptName(draft.name);
      }
      setDraftedFromAssistant(true);
      setMode("template");
      setStep(2);
    } catch (err) {
      setDraftError(
        err instanceof Error ? err.message : "Failed to draft the prompt.",
      );
    } finally {
      setDrafting(false);
    }
  };

  const headerSubtitle =
    mode === "fork"
      ? "How do you want to start?"
      : mode === "paste"
        ? "Paste a prompt you already have, name it, and choose where it lands."
        : mode === "clone"
          ? "Fork a deployed prompt into a new variant, tweak it, and save it under a new name."
          : mode === "describe"
            ? "Describe the task; CALIBER drafts a prompt you then refine, validate, and calibrate."
            : STEP_SUBTITLE[step];

  return (
    <div className="rounded-2xl border border-slate-200/70 bg-white shadow-card">
      {/* ── Header + stepper ─────────────────────────────────────── */}
      <div className="border-b border-slate-100 px-5 py-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-sm font-semibold text-slate-900">
              Create Prompt
            </h2>
            <p className="mt-1 text-xs text-slate-500">{headerSubtitle}</p>
          </div>
          <div className="flex items-center gap-3">
            {mode !== "fork" && (
              <button
                type="button"
                className="text-xs font-medium text-caliber-700 hover:underline"
                onClick={() => {
                  setMode("fork");
                  setCreateError(null);
                  setDraftError(null);
                }}
              >
                ← Change start
              </button>
            )}
            <button
              type="button"
              className="text-xs font-medium text-slate-500 hover:text-slate-700"
              onClick={onCancel}
            >
              Close
            </button>
          </div>
        </div>

        {mode === "template" && (
        <nav
          className="mt-4 flex items-center gap-1.5"
          aria-label="Create prompt steps"
        >
          {STEPS.map((s, index) => {
            const reachable = s.n <= maxReachableStep;
            const active = s.n === step;
            const done = s.n < step;
            return (
              <Fragment key={s.n}>
                <button
                  type="button"
                  disabled={!reachable}
                  aria-current={active ? "step" : undefined}
                  onClick={() => goToStep(s.n)}
                  className={`inline-flex items-center gap-2 rounded-full px-3 py-1.5 text-xs font-medium transition-colors disabled:cursor-not-allowed ${
                    active
                      ? "bg-caliber-600 text-white"
                      : done
                        ? "bg-caliber-50 text-caliber-700 hover:bg-caliber-100"
                        : reachable
                          ? "bg-slate-100 text-slate-600 hover:bg-slate-200"
                          : "bg-slate-50 text-slate-300"
                  }`}
                >
                  <span
                    className={`grid h-4 w-4 place-items-center rounded-full text-[10px] font-semibold ${
                      active
                        ? "bg-white/20 text-white"
                        : "bg-white text-slate-500 ring-1 ring-slate-200"
                    }`}
                  >
                    {s.n}
                  </span>
                  {s.label}
                </button>
                {index < STEPS.length - 1 && (
                  <span className="h-px w-5 bg-slate-200" />
                )}
              </Fragment>
            );
          })}
        </nav>
        )}
      </div>

      {/* ── Intent fork: pick an on-ramp ──────────────────────────── */}
      {mode === "fork" && (
        <div className="p-5">
          <div className="grid gap-3 sm:grid-cols-2">
            <button
              type="button"
              onClick={() => {
                setMode("paste");
                setCreateError(null);
              }}
              className="group flex items-start gap-3 rounded-2xl border border-slate-200 bg-white p-4 text-left transition-colors hover:border-caliber-400 hover:bg-caliber-50/40"
            >
              <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-caliber-50 text-caliber-700">
                <PencilLine className="h-5 w-5" strokeWidth={1.85} />
              </span>
              <span className="min-w-0">
                <span className="block text-sm font-semibold text-slate-900">
                  Write / paste
                </span>
                <span className="mt-1 block text-xs leading-relaxed text-slate-500">
                  I already have it — paste, name, and ship. The fastest path.
                </span>
              </span>
            </button>

            <button
              type="button"
              onClick={() => {
                setMode("template");
                setCreateError(null);
              }}
              className="group flex items-start gap-3 rounded-2xl border border-slate-200 bg-white p-4 text-left transition-colors hover:border-caliber-400 hover:bg-caliber-50/40"
            >
              <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-blue-50 text-blue-600">
                <LayoutTemplate className="h-5 w-5" strokeWidth={1.85} />
              </span>
              <span className="min-w-0">
                <span className="block text-sm font-semibold text-slate-900">
                  Build from template
                </span>
                <span className="mt-1 block text-xs leading-relaxed text-slate-500">
                  Guided: pick a base, layer behavior, then fill the fields.
                </span>
              </span>
            </button>

            <button
              type="button"
              onClick={() => {
                setMode("describe");
                setCreateError(null);
                setDraftError(null);
              }}
              className="group flex items-start gap-3 rounded-2xl border border-slate-200 bg-white p-4 text-left transition-colors hover:border-caliber-400 hover:bg-caliber-50/40"
            >
              <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-violet-50 text-caliber-purple">
                <Sparkles className="h-5 w-5" strokeWidth={1.85} />
              </span>
              <span className="min-w-0">
                <span className="block text-sm font-semibold text-slate-900">
                  Describe it
                </span>
                <span className="mt-1 block text-xs leading-relaxed text-slate-500">
                  Tell CALIBER the task; it drafts the prompt, then you refine,
                  validate, and calibrate it the same way.
                </span>
              </span>
            </button>

            <button
              type="button"
              onClick={enterCloneMode}
              className="group flex items-start gap-3 rounded-2xl border border-slate-200 bg-white p-4 text-left transition-colors hover:border-caliber-400 hover:bg-caliber-50/40"
            >
              <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-amber-50 text-amber-600">
                <Copy className="h-5 w-5" strokeWidth={1.85} />
              </span>
              <span className="min-w-0">
                <span className="block text-sm font-semibold text-slate-900">
                  Start from existing
                </span>
                <span className="mt-1 block text-xs leading-relaxed text-slate-500">
                  Clone a deployed registry prompt as a new variant, then tweak it.
                </span>
              </span>
            </button>
          </div>
        </div>
      )}

      {/* ── Fast path: paste an existing prompt ───────────────────── */}
      {mode === "paste" && (
        <div className="space-y-4 p-5">
          <section className="rounded-2xl border border-slate-200/70 bg-white p-4">
            <h3 className="text-sm font-semibold text-slate-900">Your prompt</h3>
            <p className="mt-1 text-xs text-slate-500">
              Paste the prompt text. Use{" "}
              <span className="font-mono">{"{curly}"}</span> placeholders for
              values resolved at runtime.
            </p>
            <textarea
              aria-label="Prompt text"
              value={pastedTemplate}
              onChange={(event) => setPastedTemplate(event.target.value)}
              rows={14}
              placeholder={
                "You are a helpful assistant. Answer the user's question:\n\n{user_input}"
              }
              className="mt-3 w-full rounded-md border border-slate-300 bg-white px-3 py-2 font-mono text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500"
            />
            <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
              <span>{pastedTemplate.length.toLocaleString()} chars</span>
              {detectedPasteVars.length > 0 && (
                <>
                  <span className="text-slate-300">·</span>
                  <span>Runtime variables:</span>
                  {detectedPasteVars.map((variable) => (
                    <span
                      key={variable}
                      className="rounded-full bg-slate-100 px-2 py-0.5 font-mono text-[10px] text-slate-600"
                    >
                      {`{${variable}}`}
                    </span>
                  ))}
                </>
              )}
            </div>
          </section>

          <section className="rounded-2xl border border-slate-200/70 bg-slate-50/60 p-4">
            <h3 className="text-sm font-semibold text-slate-900">Save</h3>
            <div className="mt-3 grid gap-4 md:grid-cols-2">
              <div>
                <label className="mb-1 block text-xs font-medium text-slate-700">
                  Prompt name *
                </label>
                <input
                  aria-label="Prompt name"
                  value={promptName}
                  onChange={(event) => setPromptName(event.target.value)}
                  placeholder="support-agent-staging"
                  className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-slate-700">
                  Commit message
                </label>
                <input
                  aria-label="Commit message"
                  value={commitMessage}
                  onChange={(event) => setCommitMessage(event.target.value)}
                  placeholder="Imported existing prompt"
                  className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500"
                />
              </div>
            </div>

            <div className="mt-4">
              {!SINGLE_ENVIRONMENT && (
                <>
                  <label className="mb-2 block text-xs font-medium text-slate-700">
                    Deployment alias
                  </label>
                  <div className="grid gap-2 md:grid-cols-3">
                    {DEPLOYMENT_ALIASES.map((option) => (
                      <button
                        key={option.value}
                        type="button"
                        onClick={() => setTargetAlias(option.value)}
                        className={`rounded-xl border px-3 py-3 text-left transition-colors ${
                          targetAlias === option.value
                            ? "border-caliber-500 bg-caliber-50 text-caliber-900"
                            : "border-slate-200 bg-white text-slate-700 hover:border-slate-300"
                        }`}
                      >
                        <div className="text-sm font-semibold">{option.label}</div>
                        <div className="mt-1 text-[11px] leading-relaxed text-slate-500">
                          {option.description}
                        </div>
                      </button>
                    ))}
                  </div>
                </>
              )}
              <label className="mt-3 flex items-center gap-2 text-xs text-slate-600">
                <input
                  type="checkbox"
                  checked={openCalibrationAfterCreate}
                  onChange={(event) =>
                    setOpenCalibrationAfterCreate(event.target.checked)
                  }
                  className="h-4 w-4 rounded border-slate-300 text-caliber-600 focus:ring-caliber-500"
                />
                Open Prompt Calibration after save
              </label>
            </div>

            {createError && (
              <div className="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                {createError}
              </div>
            )}
          </section>

          <div className="flex items-center justify-end gap-3 border-t border-slate-100 pt-4">
            <button
              type="button"
              onClick={onCancel}
              className="rounded-md border border-slate-200 px-3 py-2 text-xs font-medium text-slate-600 hover:bg-slate-50"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void submitPaste()}
              disabled={creating}
              className="rounded-md bg-caliber-600 px-3 py-2 text-xs font-semibold text-white hover:bg-caliber-700 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {creating
                ? "Creating…"
                : openCalibrationAfterCreate
                  ? (SINGLE_ENVIRONMENT ? "Create and Open" : `Create and Open ${targetAlias}`)
                  : (SINGLE_ENVIRONMENT ? "Create" : `Create in ${targetAlias}`)}
            </button>
          </div>
        </div>
      )}

      {/* ── Start from existing: fork a deployed prompt into a new variant ── */}
      {mode === "clone" && (
        <div className="space-y-4 p-5">
          <section className="rounded-2xl border border-slate-200/70 bg-white p-4">
            <h3 className="text-sm font-semibold text-slate-900">
              Choose a prompt to fork
            </h3>
            <p className="mt-1 text-xs text-slate-500">
              Pick a deployed prompt and a version. Its template loads below so you
              can tweak it before saving under a new name.
            </p>
            <div className="mt-3 grid gap-4 md:grid-cols-2">
              <div>
                <label className="mb-1 block text-xs font-medium text-slate-700">
                  Source prompt *
                </label>
                <select
                  aria-label="Source prompt"
                  value={cloneSourceName}
                  onChange={(event) => void selectCloneSource(event.target.value)}
                  disabled={cloneSourcesLoading || creating}
                  className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500 disabled:bg-slate-100"
                >
                  <option value="">
                    {cloneSourcesLoading ? "Loading prompts…" : "Select a prompt…"}
                  </option>
                  {cloneSources.map((source) => (
                    <option
                      key={source.agent_id}
                      value={source.prompt_name ?? source.agent_id}
                    >
                      {source.agent_name} ({source.prompt_name ?? source.agent_id})
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-slate-700">
                  Version
                </label>
                <select
                  aria-label="Source version"
                  value={cloneVersion ?? ""}
                  onChange={(event) =>
                    void selectCloneVersion(Number(event.target.value))
                  }
                  disabled={
                    !cloneSourceName || cloneVersions.length === 0 || creating
                  }
                  className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500 disabled:bg-slate-100"
                >
                  {cloneVersions.map((v, index) => (
                    <option key={v.version} value={v.version}>
                      v{v.version}
                      {index === 0 ? " (latest)" : ""}
                      {v.aliases.includes(LIVE_ALIAS)
                        ? SINGLE_ENVIRONMENT
                          ? " · live"
                          : " · @prod"
                        : ""}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            {cloneProvenance && (
              <div
                data-testid="clone-provenance"
                className="mt-3 flex items-center gap-2 rounded-xl border border-amber-200 bg-amber-50/60 px-3 py-2 text-[11px] font-medium text-amber-800"
              >
                <Copy className="h-3.5 w-3.5" strokeWidth={1.85} />
                Forked from{" "}
                <span className="font-mono">{cloneProvenance.source}</span> v
                {cloneProvenance.version}
              </div>
            )}
            {cloneError && (
              <div className="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                {cloneError}
              </div>
            )}
          </section>

          <section className="rounded-2xl border border-slate-200/70 bg-white p-4">
            <h3 className="text-sm font-semibold text-slate-900">
              Variant prompt
            </h3>
            <p className="mt-1 text-xs text-slate-500">
              Edit the forked template. Use{" "}
              <span className="font-mono">{"{curly}"}</span> placeholders for
              values resolved at runtime.
            </p>
            <textarea
              aria-label="Prompt text"
              value={pastedTemplate}
              onChange={(event) => setPastedTemplate(event.target.value)}
              rows={14}
              placeholder={
                cloneLoading
                  ? "Loading source template…"
                  : "Select a source prompt above to load its template."
              }
              disabled={cloneLoading}
              className="mt-3 w-full rounded-md border border-slate-300 bg-white px-3 py-2 font-mono text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500 disabled:bg-slate-100"
            />
            <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
              <span>{pastedTemplate.length.toLocaleString()} chars</span>
              {detectedPasteVars.length > 0 && (
                <>
                  <span className="text-slate-300">·</span>
                  <span>Runtime variables:</span>
                  {detectedPasteVars.map((variable) => (
                    <span
                      key={variable}
                      className="rounded-full bg-slate-100 px-2 py-0.5 font-mono text-[10px] text-slate-600"
                    >
                      {`{${variable}}`}
                    </span>
                  ))}
                </>
              )}
            </div>
          </section>

          <section className="rounded-2xl border border-slate-200/70 bg-slate-50/60 p-4">
            <h3 className="text-sm font-semibold text-slate-900">Save variant</h3>
            <div className="mt-3 grid gap-4 md:grid-cols-2">
              <div>
                <label className="mb-1 block text-xs font-medium text-slate-700">
                  New prompt name *
                </label>
                <input
                  aria-label="Prompt name"
                  value={promptName}
                  onChange={(event) => setPromptName(event.target.value)}
                  placeholder="support-agent-variant"
                  className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500"
                />
                <p className="mt-1 text-[11px] text-slate-500">
                  Clones into a new prompt — not a new version of the source.
                </p>
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-slate-700">
                  Commit message
                </label>
                <input
                  aria-label="Commit message"
                  value={commitMessage}
                  onChange={(event) => setCommitMessage(event.target.value)}
                  placeholder="Forked variant"
                  className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500"
                />
              </div>
            </div>

            <div className="mt-4">
              {!SINGLE_ENVIRONMENT && (
                <>
                  <label className="mb-2 block text-xs font-medium text-slate-700">
                    Deployment alias
                  </label>
                  <div className="grid gap-2 md:grid-cols-3">
                    {DEPLOYMENT_ALIASES.map((option) => (
                      <button
                        key={option.value}
                        type="button"
                        onClick={() => setTargetAlias(option.value)}
                        className={`rounded-xl border px-3 py-3 text-left transition-colors ${
                          targetAlias === option.value
                            ? "border-caliber-500 bg-caliber-50 text-caliber-900"
                            : "border-slate-200 bg-white text-slate-700 hover:border-slate-300"
                        }`}
                      >
                        <div className="text-sm font-semibold">{option.label}</div>
                        <div className="mt-1 text-[11px] leading-relaxed text-slate-500">
                          {option.description}
                        </div>
                      </button>
                    ))}
                  </div>
                </>
              )}
              <label className="mt-3 flex items-center gap-2 text-xs text-slate-600">
                <input
                  type="checkbox"
                  checked={openCalibrationAfterCreate}
                  onChange={(event) =>
                    setOpenCalibrationAfterCreate(event.target.checked)
                  }
                  className="h-4 w-4 rounded border-slate-300 text-caliber-600 focus:ring-caliber-500"
                />
                Open Prompt Calibration after save
              </label>
            </div>

            {createError && (
              <div className="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                {createError}
              </div>
            )}
          </section>

          <div className="flex items-center justify-end gap-3 border-t border-slate-100 pt-4">
            <button
              type="button"
              onClick={onCancel}
              className="rounded-md border border-slate-200 px-3 py-2 text-xs font-medium text-slate-600 hover:bg-slate-50"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void submitClone()}
              disabled={creating || cloneLoading}
              className="rounded-md bg-caliber-600 px-3 py-2 text-xs font-semibold text-white hover:bg-caliber-700 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {creating
                ? "Creating…"
                : openCalibrationAfterCreate
                  ? (SINGLE_ENVIRONMENT ? "Create and Open" : `Create and Open ${targetAlias}`)
                  : (SINGLE_ENVIRONMENT ? "Create" : `Create in ${targetAlias}`)}
            </button>
          </div>
        </div>
      )}

      {/* ── Describe it: assistant drafts, then hands off to the builder ── */}
      {mode === "describe" && (
        <div className="space-y-4 p-5">
          <section className="rounded-2xl border border-slate-200/70 bg-white p-4">
            <h3 className="text-sm font-semibold text-slate-900">
              Describe the task
            </h3>
            <p className="mt-1 text-xs text-slate-500">
              Tell CALIBER what this prompt should do. It drafts a starting
              prompt; you then refine the elements, validate, save, and calibrate
              it exactly like a hand-built prompt.
            </p>
            <textarea
              aria-label="Task description"
              value={describeText}
              onChange={(event) => setDescribeText(event.target.value)}
              rows={6}
              placeholder="e.g. Classify inbound support tickets as billing, technical, or account-access, and ask a clarifying question only when the category is ambiguous."
              className="mt-3 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500"
            />
            {draftError && (
              <div className="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                {draftError}
              </div>
            )}
            <div className="mt-3 flex items-center justify-between gap-3">
              <span className="text-[11px] text-slate-400">
                The draft lands in the builder — nothing is saved until you
                review and create it.
              </span>
              <button
                type="button"
                onClick={() => void submitDescribe()}
                disabled={drafting || !describeText.trim()}
                className="inline-flex items-center gap-1.5 rounded-md bg-caliber-600 px-3 py-2 text-xs font-semibold text-white hover:bg-caliber-700 disabled:cursor-not-allowed disabled:opacity-60"
              >
                <Sparkles className="h-4 w-4" strokeWidth={1.85} />
                {drafting ? "Drafting…" : "Draft with CALIBER"}
              </button>
            </div>
          </section>
        </div>
      )}

      {mode === "template" && (
      <div className="grid gap-6 p-5 xl:grid-cols-[minmax(0,1fr)_minmax(340px,0.8fr)]">
        {/* ── Left: step content + nav ──────────────────────────── */}
        <div className="space-y-5">
          {step === 1 && (
            <section className="rounded-2xl border border-slate-200/70 bg-white p-4">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <h3 className="text-sm font-semibold text-slate-900">
                    Choose a starting template
                  </h3>
                  <p className="mt-1 text-xs text-slate-500">
                    Start from the imported library as-is, or use a CALIBER
                    builder archetype when you want to shape the prompt from
                    lower-level pieces.
                  </p>
                </div>
                {catalogLoading && (
                  <span className="text-xs text-slate-400">Loading…</span>
                )}
              </div>
              {catalogError && (
                <div className="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                  {catalogError}
                </div>
              )}
              <input
                aria-label="Search templates"
                value={templateSearch}
                onChange={(event) => setTemplateSearch(event.target.value)}
                placeholder="Search recipes, templates, techniques, or domains…"
                className="mt-4 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500"
              />

              {/* Goal-first facets: narrow by the job and the method(s). */}
              <div className="mt-4 space-y-2.5">
                <FacetRow
                  label="Goal"
                  ariaLabel="Filter by goal"
                  options={availableGoals}
                  selected={goalFilter ? [goalFilter] : []}
                  onToggle={(value) =>
                    setGoalFilter((current) => (current === value ? "" : value))
                  }
                  onClear={() => setGoalFilter("")}
                />
                <FacetRow
                  label="Method"
                  ariaLabel="Filter by method"
                  options={availableMethods}
                  selected={methodFilters}
                  onToggle={(value) =>
                    setMethodFilters((current) =>
                      current.includes(value)
                        ? current.filter((item) => item !== value)
                        : [...current, value],
                    )
                  }
                  onClear={() => setMethodFilters([])}
                />
              </div>
              {builderStarterRecipes.length > 0 && (
                <div className="mt-4">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <div className="text-[11px] font-medium uppercase tracking-wide text-slate-400">
                        Library Quick Starts
                      </div>
                      <p className="mt-1 text-xs text-slate-500">
                        These are the real templates imported from the library.
                        Picking one preserves its original
                        instruction/context/examples/input/output shape instead
                        of translating it into a generic prompt shell.
                      </p>
                    </div>
                    <span className="text-[11px] text-slate-400">
                      {builderStarterRecipes.length} templates
                    </span>
                  </div>
                  <div className="mt-3 grid max-h-[340px] gap-3 overflow-auto pr-1 md:grid-cols-2">
                    {builderStarterRecipes.map((recipe) => {
                      const suggestedModifierTitles =
                        recipe.suggested_modifier_ids
                          .map(
                            (modifierId) =>
                              modifierTitles.get(modifierId) ?? modifierId,
                          )
                          .join(", ");
                      const compositionHooks = (
                        recipe.composable_with ?? []
                      ).join(", ");
                      return (
                        <button
                          key={recipe.id}
                          type="button"
                          aria-label={recipe.title}
                          aria-pressed={selectedStarterRecipeId === recipe.id}
                          onClick={() => applyStarterRecipe(recipe)}
                          className={`rounded-2xl border px-4 py-3 text-left transition-colors ${
                            selectedStarterRecipeId === recipe.id
                              ? "border-caliber-500 bg-caliber-50"
                              : "border-slate-200 bg-slate-50/50 hover:border-slate-300"
                          }`}
                        >
                          <div className="flex items-center justify-between gap-2">
                            <span className="text-sm font-semibold text-slate-900">
                              {recipe.title}
                            </span>
                            <span className="rounded-full bg-white px-2 py-0.5 text-[10px] font-medium text-slate-500 ring-1 ring-slate-200">
                              {recipe.technique}
                            </span>
                          </div>
                          <p className="mt-2 text-xs leading-relaxed text-slate-500">
                            {recipe.summary}
                          </p>
                          {compositionHooks && (
                            <div className="mt-3 text-[11px] text-slate-400">
                              Composes with: {compositionHooks}
                            </div>
                          )}
                          {suggestedModifierTitles && (
                            <div className="mt-1 text-[11px] text-slate-400">
                              Suggested fusion: {suggestedModifierTitles}
                            </div>
                          )}
                          {recipe.execution_note && (
                            <div className="mt-2 rounded-xl border border-amber-200 bg-amber-50 px-2.5 py-2 text-[11px] leading-relaxed text-amber-800">
                              {recipe.execution_note}
                            </div>
                          )}
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}

              {workflowOnlyRecipes.length > 0 && (
                <details className="mt-4 rounded-xl border border-slate-200 bg-slate-50/50 px-3 py-2">
                  <summary className="cursor-pointer text-xs font-medium text-slate-700">
                    Workflow-only patterns ({workflowOnlyRecipes.length})
                  </summary>
                  <p className="mt-2 text-xs leading-relaxed text-slate-500">
                    These library patterns need multi-pass orchestration or live
                    tool loops, so they stay on the workflow side instead of
                    pretending the prompt builder can run them by itself.
                  </p>
                  <div className="mt-3 grid gap-2 md:grid-cols-2">
                    {workflowOnlyRecipes.map((recipe) => (
                      <div
                        key={recipe.id}
                        className="rounded-xl border border-slate-200 bg-white px-3 py-2"
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="text-xs font-semibold text-slate-900">
                            {recipe.title}
                          </span>
                          <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-slate-500">
                            Workflow only
                          </span>
                        </div>
                        <p className="mt-1.5 text-[11px] leading-relaxed text-slate-500">
                          {recipe.support_reason}
                        </p>
                      </div>
                    ))}
                  </div>
                </details>
              )}

              <div className="mt-5 border-t border-slate-100 pt-4">
                <div className="mb-3">
                  <div className="text-[11px] font-medium uppercase tracking-wide text-slate-400">
                    System And Core Templates
                  </div>
                  <p className="mt-1 text-xs text-slate-500">
                    Use these when you want a freeform custom prompt or a
                    lower-level CALIBER builder archetype.
                  </p>
                </div>
                <div className="grid max-h-[440px] gap-3 overflow-auto pr-1 md:grid-cols-2">
                  {filteredCoreTemplates.map((template) => (
                    <button
                      key={template.id}
                      type="button"
                      aria-label={template.title}
                      aria-pressed={
                        !selectedStarterRecipeId && baseTemplateId === template.id
                      }
                      onClick={() => handleBaseTemplateSelect(template.id)}
                      className={`rounded-2xl border px-4 py-3 text-left transition-colors ${
                        !selectedStarterRecipeId &&
                        baseTemplateId === template.id
                          ? "border-caliber-500 bg-caliber-50"
                          : "border-slate-200 bg-slate-50/50 hover:border-slate-300"
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-sm font-semibold text-slate-900">
                          {template.title}
                        </span>
                        <span className="rounded-full bg-white px-2 py-0.5 text-[10px] font-medium text-slate-500 ring-1 ring-slate-200">
                          {template.technique}
                        </span>
                      </div>
                      <p className="mt-2 text-xs leading-relaxed text-slate-500">
                        {template.summary}
                      </p>
                      <div className="mt-3 text-[11px] text-slate-400">
                        {template.domain}
                      </div>
                    </button>
                  ))}
                </div>
              </div>

              {!catalogLoading &&
                builderStarterRecipes.length === 0 &&
                workflowOnlyRecipes.length === 0 &&
                filteredCoreTemplates.length === 0 && (
                  <div className="mt-3 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-500">
                    {templateSearch.trim()
                      ? `No templates or recipes match “${templateSearch.trim()}”.`
                      : "No templates match the selected goal/method filters."}
                    {(goalFilter || methodFilters.length > 0) && (
                      <button
                        type="button"
                        onClick={() => {
                          setGoalFilter("");
                          setMethodFilters([]);
                        }}
                        className="ml-1 font-medium text-caliber-700 hover:underline"
                      >
                        Clear filters
                      </button>
                    )}
                  </div>
                )}
            </section>
          )}

          {step === 2 && (
            <>
              {draftedFromAssistant && (
                <div
                  data-testid="assistant-draft-banner"
                  className="flex items-start gap-2 rounded-2xl border border-violet-200 bg-violet-50/60 px-4 py-3 text-xs leading-relaxed text-caliber-purple"
                >
                  <Sparkles className="mt-0.5 h-4 w-4 shrink-0" strokeWidth={1.85} />
                  <span>
                    Drafted by CALIBER from your description. Review and edit the
                    elements below, then validate, save, and calibrate it the same
                    way as any prompt.
                  </span>
                </div>
              )}
              <section className="rounded-2xl border border-slate-200/70 bg-slate-50/60 p-4">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-slate-400">
                      <span aria-hidden="true">⟲</span> Extends
                    </div>
                    <div className="mt-0.5 flex flex-wrap items-center gap-2">
                      <span className="truncate text-sm font-semibold text-slate-900">
                        {selectedBase?.title ?? "—"}
                      </span>
                      {selectedBase?.version && (
                        <span className="rounded-full bg-white px-2 py-0.5 font-mono text-[10px] font-medium text-slate-500 ring-1 ring-slate-200">
                          v{selectedBase.version}
                        </span>
                      )}
                      {selectedBase && (
                        <span className="rounded-full bg-white px-2 py-0.5 text-[10px] font-medium text-slate-500 ring-1 ring-slate-200">
                          {selectedBase.technique}
                        </span>
                      )}
                    </div>
                    {selectedStarterRecipe && (
                      <div className="mt-1 text-xs text-slate-500">
                        Loaded from library template{" "}
                        <span className="font-medium text-slate-700">
                          {selectedStarterRecipe.title}
                        </span>
                        .
                      </div>
                    )}
                  </div>
                  <button
                    type="button"
                    onClick={() => goToStep(1)}
                    className="shrink-0 text-xs font-medium text-caliber-700 hover:underline"
                  >
                    Change
                  </button>
                </div>
              </section>

              <section className="rounded-2xl border border-slate-200/70 bg-white p-4">
                <div>
                  <h3 className="text-sm font-semibold text-slate-900">
                    Add behavior
                  </h3>
                  <p className="mt-1 text-xs text-slate-500">
                    Optional: layer examples, formatting, retrieval, or
                    guardrails.
                  </p>
                </div>

                <div className="mt-3">
                  {modifierIds.length === 0 ? (
                    <span className="text-xs text-slate-400">
                      No behavior added yet.
                    </span>
                  ) : (
                    <>
                      <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-slate-400">
                        Behavior layers (applied in order)
                      </div>
                      <ol aria-label="Behavior layers" className="space-y-1.5">
                        {modifierIds.map((id, index) => {
                          const modifier = modifiers.find(
                            (item) => item.id === id,
                          );
                          if (!modifier) return null;
                          return (
                            <li
                              key={id}
                              className="flex items-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50/70 px-3 py-2"
                            >
                              <span className="grid h-5 w-5 shrink-0 place-items-center rounded-full bg-white text-[10px] font-semibold text-emerald-700 ring-1 ring-emerald-200">
                                {index + 1}
                              </span>
                              <div className="min-w-0 flex-1">
                                <div className="truncate text-xs font-semibold text-emerald-900">
                                  {modifier.title}
                                </div>
                                <div className="text-[10px] text-emerald-700/80">
                                  {modifier.technique}
                                </div>
                              </div>
                              <div className="flex items-center gap-0.5">
                                <button
                                  type="button"
                                  aria-label={`Move ${modifier.title} earlier`}
                                  disabled={index === 0}
                                  onClick={() => moveModifier(index, -1)}
                                  className="grid h-6 w-6 place-items-center rounded-md text-emerald-700 hover:bg-emerald-100 disabled:cursor-not-allowed disabled:opacity-30"
                                >
                                  <span aria-hidden="true">↑</span>
                                </button>
                                <button
                                  type="button"
                                  aria-label={`Move ${modifier.title} later`}
                                  disabled={index === modifierIds.length - 1}
                                  onClick={() => moveModifier(index, 1)}
                                  className="grid h-6 w-6 place-items-center rounded-md text-emerald-700 hover:bg-emerald-100 disabled:cursor-not-allowed disabled:opacity-30"
                                >
                                  <span aria-hidden="true">↓</span>
                                </button>
                                <button
                                  type="button"
                                  aria-label={`Remove ${modifier.title}`}
                                  onClick={() => handleModifierToggle(id)}
                                  className="grid h-6 w-6 place-items-center rounded-md text-emerald-700 hover:bg-emerald-100"
                                >
                                  <span aria-hidden="true">✕</span>
                                </button>
                              </div>
                            </li>
                          );
                        })}
                      </ol>
                    </>
                  )}
                </div>

                {selectedBase && recommendedModifierIds.length > 0 && (
                  <div className="mt-3">
                    <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-slate-400">
                      {selectedStarterRecipe?.suggested_modifier_ids.length
                        ? "Suggested Fusions"
                        : "Recommended"}
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {recommendedModifierIds.map((modifierId) => {
                        const modifier = modifiers.find(
                          (item) => item.id === modifierId,
                        );
                        if (!modifier) return null;
                        const included = modifierIds.includes(modifier.id);
                        return (
                          <button
                            key={modifier.id}
                            type="button"
                            onClick={() => handleModifierToggle(modifier.id)}
                            className={`rounded-full px-2.5 py-1 text-[11px] font-medium ring-1 transition-colors ${
                              included
                                ? "bg-emerald-50 text-emerald-700 ring-emerald-200"
                                : "bg-caliber-50 text-caliber-700 ring-caliber-200 hover:bg-caliber-100"
                            }`}
                          >
                            {included ? "Included" : "Add"} {modifier.title}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                )}

                {selectedStarterRecipe?.composable_with?.length ? (
                  <div className="mt-3">
                    <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-slate-400">
                      Library Composition Hooks
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {selectedStarterRecipe.composable_with.map((hook) => (
                        <span
                          key={hook}
                          className="rounded-full bg-slate-100 px-2.5 py-1 text-[11px] font-medium text-slate-600"
                        >
                          {hook}
                        </span>
                      ))}
                    </div>
                  </div>
                ) : null}

                <details className="mt-3 rounded-xl border border-slate-200 bg-slate-50/50 px-3 py-2">
                  <summary className="cursor-pointer text-xs font-medium text-slate-700">
                    Browse all modifiers ({modifiers.length})
                  </summary>
                  <div className="mt-3 grid gap-2 md:grid-cols-2">
                    {modifiers.map((modifier) => {
                      const selected = modifierIds.includes(modifier.id);
                      const disabledReason = getModifierDisabledReason(
                        modifier,
                        baseTemplateId,
                        modifierIds,
                        modifiers,
                      );
                      return (
                        <button
                          key={modifier.id}
                          type="button"
                          onClick={() => handleModifierToggle(modifier.id)}
                          disabled={Boolean(disabledReason) && !selected}
                          title={disabledReason ?? undefined}
                          className={`rounded-xl border px-3 py-2 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${
                            selected
                              ? "border-emerald-500 bg-emerald-50"
                              : "border-slate-200 bg-white hover:border-slate-300"
                          }`}
                        >
                          <div className="flex items-center justify-between gap-2">
                            <span className="text-xs font-semibold text-slate-900">
                              {modifier.title}
                            </span>
                            <span
                              className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${
                                selected
                                  ? "bg-emerald-100 text-emerald-700"
                                  : "bg-slate-100 text-slate-500"
                              }`}
                            >
                              {selected ? "Selected" : modifier.technique}
                            </span>
                          </div>
                          <p className="mt-1.5 text-[11px] leading-relaxed text-slate-500">
                            {modifier.summary}
                          </p>
                          {disabledReason && !selected && (
                            <div className="mt-2 text-[11px] text-amber-700">
                              {disabledReason}
                            </div>
                          )}
                        </button>
                      );
                    })}
                  </div>
                </details>
              </section>

              <section className="rounded-2xl border border-slate-200/70 bg-white p-4">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <h3 className="text-sm font-semibold text-slate-900">
                      Prompt elements
                    </h3>
                    <p className="mt-1 text-xs text-slate-500">
                      Edit any element to override just that part. Everything
                      else stays tied to the base template and the behaviors
                      above.
                    </p>
                  </div>
                  {overriddenElements.length > 0 && (
                    <button
                      type="button"
                      onClick={() => setSectionOverrides({})}
                      className="shrink-0 text-xs font-medium text-caliber-700 hover:underline"
                    >
                      Reset all
                    </button>
                  )}
                </div>
                {!preview ? (
                  <div className="mt-4 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-500">
                    Compiling template…
                  </div>
                ) : (
                  <>
                    <div className="mt-4 space-y-4">
                      {PROMPT_ELEMENTS.map(({ name, label }) => {
                        const overridden = name in sectionOverrides;
                        const composed = composedSections[name] ?? "";
                        const value = overridden
                          ? (sectionOverrides[name] ?? "")
                          : composed;
                        const empty = !overridden && !composed.trim();
                        return (
                          <div key={name}>
                            <div className="mb-1 flex items-center justify-between gap-2">
                              <span className="flex items-center gap-2 text-xs font-medium text-slate-700">
                                {label}
                                <span
                                  className={`rounded-full px-1.5 py-0.5 text-[10px] font-medium ${
                                    overridden
                                      ? "bg-amber-100 text-amber-700"
                                      : "bg-slate-100 text-slate-400"
                                  }`}
                                >
                                  {overridden
                                    ? "overridden"
                                    : empty
                                      ? "empty"
                                      : "base"}
                                </span>
                              </span>
                              {overridden && (
                                <button
                                  type="button"
                                  aria-label={`Reset ${label} element`}
                                  onClick={() => resetElementOverride(name)}
                                  className="text-[11px] font-medium text-caliber-700 hover:underline"
                                >
                                  Reset
                                </button>
                              )}
                            </div>
                            <textarea
                              aria-label={`${label} element`}
                              value={value}
                              onChange={(event) =>
                                setElementOverride(name, event.target.value)
                              }
                              rows={Math.max(2, estimateRows(value))}
                              placeholder={
                                empty
                                  ? "Empty — add text to include this element."
                                  : undefined
                              }
                              className={`w-full rounded-md border bg-white px-3 py-2 text-sm font-mono outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500 ${
                                overridden
                                  ? "border-amber-300"
                                  : "border-slate-300"
                              }`}
                            />
                          </div>
                        );
                      })}
                    </div>
                    <div className="mt-2 text-[11px] text-slate-500">
                      Use{" "}
                      <span className="font-mono">{"{{variable}}"}</span>{" "}
                      placeholders to keep fields dynamic; fill their values
                      below.
                    </div>
                  </>
                )}
              </section>

              <section className="rounded-2xl border border-slate-200/70 bg-white p-4">
                <div>
                  <h3 className="text-sm font-semibold text-slate-900">
                    Fill in the prompt
                  </h3>
                  <p className="mt-1 text-xs text-slate-500">
                    These fields come from the template and any behavior you
                    added.
                  </p>
                </div>
                <div className="mt-4 space-y-4">
                  {(preview?.builder_variables ?? []).map((variable) => {
                    const currentValue =
                      builderValues[variable.name] ?? variable.value ?? "";
                    const showStarterHint =
                      variable.required === true &&
                      typeof variable.default === "string" &&
                      currentValue === variable.default;

                    return (
                      <div key={variable.name}>
                        <label className="mb-1 block text-xs font-medium text-slate-700">
                          {variable.label}
                          {variable.required ? " *" : ""}
                        </label>
                        <textarea
                          aria-label={variable.label}
                          value={currentValue}
                          onChange={(event) =>
                            setBuilderValues((current) => ({
                              ...current,
                              [variable.name]: event.target.value,
                            }))
                          }
                          placeholder={variable.default ?? variable.description}
                          rows={Math.max(
                            3,
                            estimateRows(
                              currentValue || variable.default || "",
                            ),
                          )}
                          className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500"
                        />
                        <div className="mt-1 text-[11px] text-slate-500">
                          {variable.description}
                        </div>
                        {showStarterHint ? (
                          <div className="mt-1 text-[11px] font-medium text-amber-700">
                            Starter example loaded. Replace it with your use
                            case before saving.
                          </div>
                        ) : null}
                      </div>
                    );
                  })}
                  {preview && preview.builder_variables.length === 0 && (
                    <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-500">
                      This template does not need any extra design-time fields.
                    </div>
                  )}
                  {!preview && previewLoading && (
                    <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-500">
                      Compiling template…
                    </div>
                  )}
                </div>
              </section>

              <details className="rounded-2xl border border-slate-200/70 bg-white p-4">
                <summary className="cursor-pointer text-sm font-semibold text-slate-900">
                  Advanced: runtime variables, preview values, manual override
                </summary>
                <div className="mt-4 space-y-4">
                  <div>
                    <label className="mb-1 block text-xs font-medium text-slate-700">
                      Runtime variables
                    </label>
                    <textarea
                      aria-label="Runtime variables"
                      value={runtimeVariablesText}
                      onChange={(event) =>
                        setRuntimeVariablesText(event.target.value)
                      }
                      placeholder={"retrieved_docs\ncustomer_profile"}
                      rows={3}
                      className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-mono outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500"
                    />
                    <div className="mt-1 text-[11px] text-slate-500">
                      One placeholder per line or comma-separated. These remain
                      unresolved on purpose at runtime.
                    </div>
                  </div>

                  <div>
                    <label className="mb-1 block text-xs font-medium text-slate-700">
                      Preview variables (JSON object)
                    </label>
                    <textarea
                      aria-label="Preview variables"
                      value={previewVariablesText}
                      onChange={(event) =>
                        setPreviewVariablesText(event.target.value)
                      }
                      rows={6}
                      className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-mono outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500"
                    />
                    <div className="mt-1 text-[11px] text-slate-500">
                      Applied only to the preview so you can see the prompt with
                      realistic values.
                    </div>
                  </div>

                  <div>
                    <label className="mb-1 block text-xs font-medium text-slate-700">
                      Template override
                    </label>
                    <textarea
                      aria-label="Template override"
                      value={templateOverride}
                      onChange={(event) =>
                        setTemplateOverride(event.target.value)
                      }
                      placeholder="Optional: replace the compiled prompt with a manually edited version."
                      rows={6}
                      className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-mono outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500"
                    />
                    <div className="mt-1 text-[11px] text-slate-500">
                      Useful when the builder gets you 90% of the way and you
                      want a final manual pass, or to paste a prompt you already
                      have.
                    </div>
                  </div>
                </div>
              </details>
            </>
          )}

          {step === 3 && (
            <>
              <section className="rounded-2xl border border-slate-200/70 bg-white p-4">
                <div className="flex items-center justify-between gap-3">
                  <h3 className="text-sm font-semibold text-slate-900">
                    Review
                  </h3>
                  <span
                    className={`rounded-full px-2.5 py-1 text-[11px] font-medium ${
                      previewReady
                        ? "bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200"
                        : "bg-amber-50 text-amber-700 ring-1 ring-amber-200"
                    }`}
                  >
                    {previewStatus}
                  </span>
                </div>
                {fieldsToFill.length ? (
                  <div className="mt-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2">
                    <div className="text-xs font-semibold uppercase tracking-wide text-amber-700">
                      Fields still to fill
                    </div>
                    <ul className="mt-1 space-y-1 text-xs text-amber-800">
                      {fieldsToFill.map((label) => (
                        <li key={label}>• {label}</li>
                      ))}
                    </ul>
                    <button
                      type="button"
                      onClick={() => goToStep(2)}
                      className="mt-2 text-[11px] font-medium text-amber-800 hover:underline"
                    >
                      ← Fill on the Compose step
                    </button>
                  </div>
                ) : null}
                {blockingErrors.length ? (
                  <ul className="mt-3 space-y-1 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                    {blockingErrors.map((message) => (
                      <li key={message}>• {message}</li>
                    ))}
                  </ul>
                ) : null}
                <div className="mt-3">
                  <label className="mb-1 block text-xs font-medium text-slate-700">
                    Final prompt
                  </label>
                  <pre className="max-h-64 overflow-auto rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-[12px] leading-relaxed text-slate-700 whitespace-pre-wrap">
                    {preview?.rendered_preview ?? "—"}
                  </pre>
                </div>
              </section>

              <section
                aria-label="Goal and methods"
                className="rounded-2xl border border-slate-200/70 bg-white p-4"
              >
                <h3 className="text-sm font-semibold text-slate-900">
                  Goal &amp; methods
                </h3>
                <p className="mt-1 text-xs text-slate-500">
                  Classify this prompt for the library. Defaults come from the
                  base template, but the goal can be anything and you can tag any
                  combination of methods — not just the base&apos;s technique.
                </p>

                <div className="mt-4">
                  <label className="mb-1 block text-xs font-medium text-slate-700">
                    Goal
                  </label>
                  <input
                    aria-label="Prompt goal"
                    list="prompt-goal-options"
                    value={promptGoal}
                    onChange={(event) => setPromptGoal(event.target.value)}
                    placeholder="e.g. question-answering, or a custom goal of your own"
                    className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500"
                  />
                  <datalist id="prompt-goal-options">
                    {availableGoals.map((goal) => (
                      <option key={goal} value={goal} />
                    ))}
                  </datalist>
                  <div className="mt-1 text-[11px] text-slate-500">
                    Pick a known goal or type your own.
                  </div>
                </div>

                <div className="mt-4">
                  <label className="mb-1.5 block text-xs font-medium text-slate-700">
                    Methods
                  </label>
                  <div
                    className="flex flex-wrap gap-1.5"
                    aria-label="Prompt methods"
                  >
                    {Array.from(
                      new Set([...availableMethods, ...promptMethods]),
                    )
                      .sort()
                      .map((method) => {
                        const selected = promptMethods.includes(method);
                        return (
                          <button
                            key={method}
                            type="button"
                            aria-pressed={selected}
                            onClick={() => togglePromptMethod(method)}
                            className={`rounded-full px-2.5 py-1 text-[11px] font-medium transition-colors ${
                              selected
                                ? "bg-caliber-600 text-white"
                                : "bg-slate-100 text-slate-600 hover:bg-slate-200"
                            }`}
                          >
                            {humanizeFacet(method)}
                          </button>
                        );
                      })}
                    {promptMethods.length === 0 && (
                      <span className="text-[11px] text-slate-400">
                        No methods tagged yet.
                      </span>
                    )}
                  </div>
                  <div className="mt-2 flex items-center gap-2">
                    <input
                      aria-label="Add custom method"
                      value={customMethodText}
                      onChange={(event) =>
                        setCustomMethodText(event.target.value)
                      }
                      onKeyDown={(event) => {
                        if (event.key === "Enter") {
                          event.preventDefault();
                          addCustomMethod();
                        }
                      }}
                      placeholder="Add a custom method…"
                      className="w-56 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500"
                    />
                    <button
                      type="button"
                      onClick={addCustomMethod}
                      disabled={!customMethodText.trim()}
                      className="rounded-md border border-slate-200 px-2.5 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      Add
                    </button>
                  </div>
                </div>
              </section>

              <section
                aria-label="Lineage"
                className="rounded-2xl border border-slate-200/70 bg-white p-4"
              >
                <h3 className="text-sm font-semibold text-slate-900">Lineage</h3>
                <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-600">
                  <span aria-hidden="true">⟲</span>
                  Derived from
                  <span className="font-medium text-slate-800">
                    {selectedBase?.title ?? "—"}
                  </span>
                  {selectedBase?.version && (
                    <span className="rounded-full bg-slate-100 px-2 py-0.5 font-mono text-[10px] font-medium text-slate-600">
                      v{selectedBase.version}
                    </span>
                  )}
                </div>
                <div className="mt-3">
                  <div className="text-[11px] font-medium uppercase tracking-wide text-slate-400">
                    Changeset
                  </div>
                  {changeset.length > 0 ? (
                    <ul className="mt-1.5 space-y-1 text-xs text-slate-700">
                      {changeset.map((entry) => (
                        <li key={entry} className="flex items-start gap-1.5">
                          <span className="text-caliber-600" aria-hidden="true">
                            •
                          </span>
                          {entry}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <div className="mt-1.5 text-xs text-slate-400">
                      No changes from the base template yet.
                    </div>
                  )}
                </div>
              </section>

              <section className="rounded-2xl border border-slate-200/70 bg-slate-50/60 p-4">
                <h3 className="text-sm font-semibold text-slate-900">Save</h3>
                <div className="mt-3 grid gap-4 md:grid-cols-2">
                  <div>
                    <label className="mb-1 block text-xs font-medium text-slate-700">
                      Prompt name *
                    </label>
                    <input
                      aria-label="Prompt name"
                      value={promptName}
                      onChange={(event) => setPromptName(event.target.value)}
                      placeholder="support-agent-staging"
                      className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500"
                    />
                  </div>
                  <div>
                    <label className="mb-1 block text-xs font-medium text-slate-700">
                      Commit message
                    </label>
                    <input
                      aria-label="Commit message"
                      value={commitMessage}
                      onChange={(event) => setCommitMessage(event.target.value)}
                      placeholder="Initial prompt builder draft"
                      className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-caliber-500 focus:ring-1 focus:ring-caliber-500"
                    />
                  </div>
                </div>

                <div className="mt-4">
                  {!SINGLE_ENVIRONMENT && (
                    <>
                      <label className="mb-2 block text-xs font-medium text-slate-700">
                        Deployment alias
                      </label>
                      <div className="grid gap-2 md:grid-cols-3">
                        {DEPLOYMENT_ALIASES.map((option) => (
                          <button
                            key={option.value}
                            type="button"
                            onClick={() => setTargetAlias(option.value)}
                            className={`rounded-xl border px-3 py-3 text-left transition-colors ${
                              targetAlias === option.value
                                ? "border-caliber-500 bg-caliber-50 text-caliber-900"
                                : "border-slate-200 bg-white text-slate-700 hover:border-slate-300"
                            }`}
                          >
                            <div className="text-sm font-semibold">
                              {option.label}
                            </div>
                            <div className="mt-1 text-[11px] leading-relaxed text-slate-500">
                              {option.description}
                            </div>
                          </button>
                        ))}
                      </div>
                    </>
                  )}
                  <label className="mt-3 flex items-center gap-2 text-xs text-slate-600">
                    <input
                      type="checkbox"
                      checked={openCalibrationAfterCreate}
                      onChange={(event) =>
                        setOpenCalibrationAfterCreate(event.target.checked)
                      }
                      className="h-4 w-4 rounded border-slate-300 text-caliber-600 focus:ring-caliber-500"
                    />
                    Open Prompt Calibration after save
                  </label>
                </div>

                {createError && (
                  <div className="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                    {createError}
                  </div>
                )}
              </section>
            </>
          )}

          {/* footer nav */}
          <div className="flex items-center justify-between gap-3 border-t border-slate-100 pt-4">
            <button
              type="button"
              onClick={() =>
                step === 1 ? onCancel() : goToStep((step - 1) as StepNumber)
              }
              className="rounded-md border border-slate-200 px-3 py-2 text-xs font-medium text-slate-600 hover:bg-slate-50"
            >
              {step === 1 ? "Cancel" : "← Back"}
            </button>
            {step < 3 ? (
              <button
                type="button"
                onClick={() => goToStep((step + 1) as StepNumber)}
                disabled={!baseTemplateId}
                className="rounded-md bg-caliber-600 px-3 py-2 text-xs font-semibold text-white hover:bg-caliber-700 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {step === 1 ? "Next: Compose →" : "Next: Review →"}
              </button>
            ) : (
              <button
                type="button"
                onClick={() => void submit()}
                disabled={creating || previewLoading || !preview}
                className="rounded-md bg-caliber-600 px-3 py-2 text-xs font-semibold text-white hover:bg-caliber-700 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {creating
                  ? "Creating…"
                  : openCalibrationAfterCreate
                    ? (SINGLE_ENVIRONMENT ? "Create and Open" : `Create and Open ${targetAlias}`)
                    : (SINGLE_ENVIRONMENT ? "Create" : `Create in ${targetAlias}`)}
              </button>
            )}
          </div>
        </div>

        {/* ── Right: persistent live-preview rail ───────────────── */}
        <div>
          <section className="sticky top-4 rounded-2xl border border-slate-200/70 bg-white shadow-card">
            <div className="border-b border-slate-100 px-5 py-4">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <h3 className="text-sm font-semibold text-slate-900">
                    Live preview
                  </h3>
                  <p className="mt-1 text-xs text-slate-500">
                    Refreshes automatically as you edit.
                  </p>
                </div>
                <span
                  className={`rounded-full px-2.5 py-1 text-[11px] font-medium ${
                    previewReady
                      ? "bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200"
                      : "bg-amber-50 text-amber-700 ring-1 ring-amber-200"
                  }`}
                >
                  {previewStatus}
                </span>
              </div>
            </div>

            <div className="space-y-4 px-5 py-4">
              {!baseTemplateId && (
                <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-500">
                  Pick a template to see the compiled prompt.
                </div>
              )}

              {previewError && (
                <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                  {previewError}
                </div>
              )}

              {fieldsToFill.length > 0 ? (
                <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2">
                  <div className="text-xs font-semibold uppercase tracking-wide text-amber-700">
                    Fields still to fill
                  </div>
                  <ul className="mt-2 space-y-1 text-xs text-amber-800">
                    {fieldsToFill.map((label) => (
                      <li key={label}>• {label}</li>
                    ))}
                  </ul>
                  {step !== 2 && (
                    <button
                      type="button"
                      onClick={() => goToStep(2)}
                      className="mt-2 text-[11px] font-medium text-amber-800 hover:underline"
                    >
                      ← Fill on the Compose step
                    </button>
                  )}
                </div>
              ) : null}

              {blockingErrors.length ? (
                <div>
                  <div className="text-xs font-semibold uppercase tracking-wide text-red-600">
                    Errors
                  </div>
                  <ul className="mt-2 space-y-1 text-xs text-red-700">
                    {blockingErrors.map((message) => (
                      <li key={message}>• {message}</li>
                    ))}
                  </ul>
                </div>
              ) : null}

              {preview?.validation_report.warnings.length ? (
                <div>
                  <div className="text-xs font-semibold uppercase tracking-wide text-amber-600">
                    Warnings
                  </div>
                  <ul className="mt-2 space-y-1 text-xs text-amber-700">
                    {preview.validation_report.warnings.map((message) => (
                      <li key={message}>• {message}</li>
                    ))}
                  </ul>
                </div>
              ) : null}

              {preview && (
                <>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <MetricCard
                      label="Characters"
                      value={preview.char_count.toLocaleString()}
                    />
                    <MetricCard
                      label="Words"
                      value={preview.word_count.toLocaleString()}
                    />
                  </div>

                  <div className="grid gap-3 sm:grid-cols-2">
                    <MetaList
                      title="Detected variables"
                      items={preview.detected_variables}
                      emptyLabel="None"
                    />
                    <MetaList
                      title="Runtime placeholders still open"
                      items={preview.unresolved_variables}
                      emptyLabel="All preview variables resolved"
                    />
                  </div>

                  <MetaList
                    title="Recommended scorers for calibration"
                    items={preview.recommended_scorers}
                    emptyLabel="No scorer hints"
                  />

                  <div>
                    <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
                      Compiled prompt
                    </div>
                    <pre className="max-h-[420px] overflow-auto rounded-xl border border-slate-200 bg-slate-50 px-3 py-3 text-[12px] leading-relaxed text-slate-700 whitespace-pre-wrap">
                      {preview.rendered_preview}
                    </pre>
                  </div>
                </>
              )}
            </div>
          </section>
        </div>
      </div>
      )}
    </div>
  );
}

function MetricCard({
  label,
  value,
}: {
  label: string;
  value: string;
}): JSX.Element {
  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2">
      <div className="text-[11px] uppercase tracking-wide text-slate-400">
        {label}
      </div>
      <div className="mt-1 text-sm font-semibold text-slate-900">{value}</div>
    </div>
  );
}

function MetaList({
  title,
  items,
  emptyLabel,
}: {
  title: string;
  items: string[];
  emptyLabel: string;
}): JSX.Element {
  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2">
      <div className="text-[11px] uppercase tracking-wide text-slate-400">
        {title}
      </div>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {items.length > 0 ? (
          items.map((item) => (
            <span
              key={item}
              className="rounded-full bg-white px-2 py-0.5 text-[11px] text-slate-600 ring-1 ring-slate-200"
            >
              {item}
            </span>
          ))
        ) : (
          <span className="text-xs text-slate-400">{emptyLabel}</span>
        )}
      </div>
    </div>
  );
}

function humanizeFacet(value: string): string {
  return value
    .split(/[-_]/g)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

// A facet row. Multi-capable: `selected` is the set of active values (empty =
// "All"). Single-select callers just pass a 0-or-1-length array.
function FacetRow({
  label,
  ariaLabel,
  options,
  selected,
  onToggle,
  onClear,
}: {
  label: string;
  ariaLabel: string;
  options: string[];
  selected: string[];
  onToggle: (value: string) => void;
  onClear: () => void;
}): JSX.Element {
  return (
    <div className="flex flex-wrap items-center gap-1.5" aria-label={ariaLabel}>
      <span className="mr-0.5 text-[11px] font-medium uppercase tracking-wide text-slate-400">
        {label}
      </span>
      <button
        type="button"
        aria-pressed={selected.length === 0}
        onClick={onClear}
        className={`rounded-full px-2.5 py-1 text-[11px] font-medium transition-colors ${
          selected.length === 0
            ? "bg-caliber-600 text-white"
            : "bg-slate-100 text-slate-600 hover:bg-slate-200"
        }`}
      >
        All
      </button>
      {options.map((option) => {
        const isActive = selected.includes(option);
        return (
          <button
            key={option}
            type="button"
            aria-pressed={isActive}
            onClick={() => onToggle(option)}
            className={`rounded-full px-2.5 py-1 text-[11px] font-medium transition-colors ${
              isActive
                ? "bg-caliber-600 text-white"
                : "bg-slate-100 text-slate-600 hover:bg-slate-200"
            }`}
          >
            {humanizeFacet(option)}
          </button>
        );
      })}
    </div>
  );
}

function getModifierDisabledReason(
  modifier: PromptTemplateDefinition,
  baseTemplateId: string,
  selectedModifierIds: string[],
  modifiers: PromptTemplateDefinition[],
): string | null {
  if (
    modifier.compatible_base_ids.length > 0 &&
    !modifier.compatible_base_ids.includes(baseTemplateId)
  ) {
    return "Not compatible with the selected base template.";
  }

  if (
    modifier.incompatible_modifier_ids.some((id) =>
      selectedModifierIds.includes(id),
    )
  ) {
    return "Conflicts with another selected modifier.";
  }

  const selectedModifierSet = new Set(selectedModifierIds);
  const blockedBySelected = modifiers.some(
    (item) =>
      selectedModifierSet.has(item.id) &&
      item.incompatible_modifier_ids.includes(modifier.id),
  );
  if (blockedBySelected) {
    return "Conflicts with another selected modifier.";
  }

  return null;
}

function parseRuntimeVariables(text: string): string[] {
  return text
    .split(/[\n,]/g)
    .map((value) => value.trim())
    .filter(Boolean);
}

function parsePreviewVariables(
  text: string,
): { ok: true; value: Record<string, string> } | { ok: false; error: string } {
  const trimmed = text.trim();
  if (!trimmed) {
    return { ok: true, value: {} };
  }

  try {
    const parsed = JSON.parse(trimmed) as unknown;
    if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
      return { ok: false, error: "Preview variables must be a JSON object." };
    }
    const normalized: Record<string, string> = {};
    for (const [key, value] of Object.entries(parsed)) {
      normalized[String(key)] = String(value);
    }
    return { ok: true, value: normalized };
  } catch {
    return { ok: false, error: "Preview variables must be valid JSON." };
  }
}

function estimateRows(value: string): number {
  return Math.min(10, Math.max(3, value.split("\n").length));
}
