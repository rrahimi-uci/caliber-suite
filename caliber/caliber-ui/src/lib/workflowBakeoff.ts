import type {
  WorkflowBenchmarkReport,
  WorkflowBenchmarkReportStatus,
  WorkflowBakeoffRubricSection,
  WorkflowBakeoffRubricScore,
  WorkflowBakeoffRubricWorksheetEntry,
  WorkflowBakeoffScenario,
  WorkflowBakeoffScenarioStatus,
  WorkflowBakeoffScenarioWorksheetEntry,
  WorkflowBakeoffWorksheet,
} from "@/api/workflowTypes";
import { workflowRunPath } from "@/lib/workflowRunLinks";

export type {
  WorkflowBenchmarkReportStatus,
  WorkflowBakeoffRubricScore,
  WorkflowBakeoffRubricWorksheetEntry,
  WorkflowBakeoffScenarioStatus,
  WorkflowBakeoffScenarioWorksheetEntry,
  WorkflowBakeoffWorksheet,
} from "@/api/workflowTypes";

export type WorkflowBenchmarkReportLibraryStatusFilter =
  | WorkflowBenchmarkReportStatus
  | "all";

export type WorkflowBenchmarkReportSort =
  | "updated_desc"
  | "name_asc"
  | "product_asc"
  | "captured_desc"
  | "passed_desc";

export interface WorkflowBakeoffEvidenceReference {
  key: string;
  type: "workflow" | "run";
  id: string;
  href: string;
  label: string;
}

export interface WorkflowBakeoffScenarioExport
  extends WorkflowBakeoffScenario {
  worksheet: WorkflowBakeoffScenarioWorksheetEntry;
}

export interface WorkflowBakeoffRubricSectionExport
  extends WorkflowBakeoffRubricSection {
  worksheet: WorkflowBakeoffRubricWorksheetEntry;
}

export interface WorkflowBakeoffExportPayload {
  schema_version: number;
  generated_at: string;
  worksheet: WorkflowBakeoffWorksheet;
  scenarios: WorkflowBakeoffScenarioExport[];
  operator_rubric: WorkflowBakeoffRubricSectionExport[];
}

export interface WorkflowBakeoffWorksheetStats {
  scenario_count: number;
  captured_count: number;
  passed_count: number;
  blocked_count: number;
}

interface WorkflowBenchmarkReportDraftState {
  selectedReport: WorkflowBenchmarkReport | null;
  worksheet: WorkflowBakeoffWorksheet;
  reportName: string;
  reportStatus: WorkflowBenchmarkReportStatus;
  scenarios: WorkflowBakeoffScenario[];
  rubric: WorkflowBakeoffRubricSection[];
}

const WORKFLOW_BAKEOFF_SCENARIO_STATUSES = new Set<WorkflowBakeoffScenarioStatus>([
  "not_started",
  "in_progress",
  "passed",
  "blocked",
]);

const WORKFLOW_BAKEOFF_RUBRIC_SCORES = new Set<WorkflowBakeoffRubricScore>([
  "",
  "1",
  "2",
  "3",
  "4",
  "5",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function readString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function normalizeScenarioStatus(value: unknown): WorkflowBakeoffScenarioStatus {
  return typeof value === "string"
    && WORKFLOW_BAKEOFF_SCENARIO_STATUSES.has(value as WorkflowBakeoffScenarioStatus)
    ? (value as WorkflowBakeoffScenarioStatus)
    : "not_started";
}

function normalizeRubricScore(value: unknown): WorkflowBakeoffRubricScore {
  return typeof value === "string"
    && WORKFLOW_BAKEOFF_RUBRIC_SCORES.has(value as WorkflowBakeoffRubricScore)
    ? (value as WorkflowBakeoffRubricScore)
    : "";
}

export function createWorkflowBakeoffScenarioWorksheetEntry(): WorkflowBakeoffScenarioWorksheetEntry {
  return {
    status: "not_started",
    minutes_to_first_success: "",
    evidence_links: "",
    notes: "",
  };
}

export function createWorkflowBakeoffRubricWorksheetEntry(): WorkflowBakeoffRubricWorksheetEntry {
  return {
    score: "",
    notes: "",
  };
}

export function createWorkflowBakeoffWorksheet(
  scenarios: WorkflowBakeoffScenario[],
  rubric: WorkflowBakeoffRubricSection[],
): WorkflowBakeoffWorksheet {
  return {
    product_name: "Caliber",
    evaluator: "",
    environment: "",
    summary: "",
    updated_at: null,
    scenarios: Object.fromEntries(
      scenarios.map((scenario) => [scenario.id, createWorkflowBakeoffScenarioWorksheetEntry()]),
    ),
    rubric: Object.fromEntries(
      rubric.map((section) => [section.title, createWorkflowBakeoffRubricWorksheetEntry()]),
    ),
  };
}

export function normalizeWorkflowBakeoffWorksheet(
  value: unknown,
  scenarios: WorkflowBakeoffScenario[],
  rubric: WorkflowBakeoffRubricSection[],
): WorkflowBakeoffWorksheet {
  const fallback = createWorkflowBakeoffWorksheet(scenarios, rubric);
  if (!isRecord(value)) return fallback;
  const rawScenarios = isRecord(value.scenarios) ? value.scenarios : {};
  const rawRubric = isRecord(value.rubric) ? value.rubric : {};
  return {
    product_name: readString(value.product_name) || fallback.product_name,
    evaluator: readString(value.evaluator),
    environment: readString(value.environment),
    summary: readString(value.summary),
    updated_at:
      typeof value.updated_at === "string" && value.updated_at.trim()
        ? value.updated_at
        : null,
    scenarios: Object.fromEntries(
      scenarios.map((scenario) => {
        const raw = rawScenarios[scenario.id];
        return [
          scenario.id,
          isRecord(raw)
            ? {
                status: normalizeScenarioStatus(raw.status),
                minutes_to_first_success: readString(raw.minutes_to_first_success),
                evidence_links: readString(raw.evidence_links),
                notes: readString(raw.notes),
              }
            : createWorkflowBakeoffScenarioWorksheetEntry(),
        ];
      }),
    ),
    rubric: Object.fromEntries(
      rubric.map((section) => {
        const raw = rawRubric[section.title];
        return [
          section.title,
          isRecord(raw)
            ? {
                score: normalizeRubricScore(raw.score),
                notes: readString(raw.notes),
              }
            : createWorkflowBakeoffRubricWorksheetEntry(),
        ];
      }),
    ),
  };
}

export function workflowBakeoffScenarioStatusLabel(
  status: WorkflowBakeoffScenarioStatus,
): string {
  switch (status) {
    case "in_progress":
      return "In progress";
    case "passed":
      return "Passed";
    case "blocked":
      return "Blocked";
    default:
      return "Not started";
  }
}

export function workflowBakeoffScenarioEntryHasEvidence(
  entry: WorkflowBakeoffScenarioWorksheetEntry | null | undefined,
): boolean {
  if (!entry) return false;
  return (
    entry.status !== "not_started"
    || entry.minutes_to_first_success.trim().length > 0
    || entry.evidence_links.trim().length > 0
    || entry.notes.trim().length > 0
  );
}

export function workflowBakeoffRubricEntryHasEvidence(
  entry: WorkflowBakeoffRubricWorksheetEntry | null | undefined,
): boolean {
  if (!entry) return false;
  return entry.score !== "" || entry.notes.trim().length > 0;
}

export function workflowBakeoffWorksheetHasEvidence(
  worksheet: WorkflowBakeoffWorksheet | null | undefined,
): boolean {
  if (!worksheet) return false;
  if (
    worksheet.evaluator.trim().length > 0
    || worksheet.environment.trim().length > 0
    || worksheet.summary.trim().length > 0
  ) {
    return true;
  }
  return (
    Object.values(worksheet.scenarios).some((entry) =>
      workflowBakeoffScenarioEntryHasEvidence(entry),
    )
    || Object.values(worksheet.rubric).some((entry) =>
      workflowBakeoffRubricEntryHasEvidence(entry),
    )
  );
}

export function workflowBakeoffWorksheetStats(
  worksheet: WorkflowBakeoffWorksheet,
  scenarios: WorkflowBakeoffScenario[],
): WorkflowBakeoffWorksheetStats {
  const entries = scenarios.map(
    (scenario) =>
      worksheet.scenarios[scenario.id]
      ?? createWorkflowBakeoffScenarioWorksheetEntry(),
  );
  return {
    scenario_count: scenarios.length,
    captured_count: entries.filter((entry) =>
      workflowBakeoffScenarioEntryHasEvidence(entry),
    ).length,
    passed_count: entries.filter((entry) => entry.status === "passed").length,
    blocked_count: entries.filter((entry) => entry.status === "blocked").length,
  };
}

export function workflowBakeoffRubricAverage(
  worksheet: WorkflowBakeoffWorksheet,
  rubric: WorkflowBakeoffRubricSection[],
): number | null {
  const scores = rubric
    .map((section) => worksheet.rubric[section.title]?.score ?? "")
    .map((value) => (value ? Number(value) : Number.NaN))
    .filter((value) => Number.isFinite(value));
  if (scores.length === 0) return null;
  return scores.reduce((sum, value) => sum + value, 0) / scores.length;
}

function benchmarkReportTimestamp(value: string): number {
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function benchmarkReportStableCompare(
  left: WorkflowBenchmarkReport,
  right: WorkflowBenchmarkReport,
): number {
  return (
    benchmarkReportTimestamp(right.updated_at)
    - benchmarkReportTimestamp(left.updated_at)
    || benchmarkReportTimestamp(right.created_at)
      - benchmarkReportTimestamp(left.created_at)
    || left.name.localeCompare(right.name)
  );
}

const WORKFLOW_BAKEOFF_EVIDENCE_ID_PATTERN = /\b(?:WR|WF)-[A-Za-z0-9-]+\b/g;

export function parseWorkflowBakeoffEvidenceReferences(
  value: string,
): WorkflowBakeoffEvidenceReference[] {
  const seen = new Set<string>();
  const refs: WorkflowBakeoffEvidenceReference[] = [];
  for (const match of value.matchAll(WORKFLOW_BAKEOFF_EVIDENCE_ID_PATTERN)) {
    const token = match[0]?.trim();
    if (!token || seen.has(token)) continue;
    seen.add(token);
    if (token.startsWith("WR-")) {
      refs.push({
        key: `run:${token}`,
        type: "run",
        id: token,
        href: workflowRunPath(token),
        label: `Run ${token}`,
      });
      continue;
    }
    refs.push({
      key: `workflow:${token}`,
      type: "workflow",
      id: token,
      href: `/workflows/${encodeURIComponent(token)}`,
      label: `Workflow ${token}`,
    });
  }
  return refs;
}

export function workflowBenchmarkReportMatchesSearch(
  report: WorkflowBenchmarkReport,
  search: string,
): boolean {
  const needle = search.trim().toLocaleLowerCase();
  if (!needle) return true;
  const haystacks = [
    report.report_id,
    report.name,
    report.owner,
    report.status,
    report.product_name,
    report.evaluator,
    report.environment,
    report.summary,
  ];
  return haystacks.some((value) =>
    value.toLocaleLowerCase().includes(needle),
  );
}

export function sortWorkflowBenchmarkReports(
  reports: WorkflowBenchmarkReport[],
  sort: WorkflowBenchmarkReportSort,
): WorkflowBenchmarkReport[] {
  const sorted = [...reports];
  sorted.sort((left, right) => {
    switch (sort) {
      case "name_asc":
        return (
          left.name.localeCompare(right.name)
          || left.product_name.localeCompare(right.product_name)
          || benchmarkReportStableCompare(left, right)
        );
      case "product_asc":
        return (
          left.product_name.localeCompare(right.product_name)
          || left.name.localeCompare(right.name)
          || benchmarkReportStableCompare(left, right)
        );
      case "captured_desc":
        return (
          right.captured_count - left.captured_count
          || right.passed_count - left.passed_count
          || benchmarkReportStableCompare(left, right)
        );
      case "passed_desc":
        return (
          right.passed_count - left.passed_count
          || right.captured_count - left.captured_count
          || benchmarkReportStableCompare(left, right)
        );
      case "updated_desc":
      default:
        return benchmarkReportStableCompare(left, right);
    }
  });
  return sorted;
}

export function filterWorkflowBenchmarkReports(
  reports: WorkflowBenchmarkReport[],
  options: {
    search?: string;
    status?: WorkflowBenchmarkReportLibraryStatusFilter;
    sort?: WorkflowBenchmarkReportSort;
  } = {},
): WorkflowBenchmarkReport[] {
  const {
    search = "",
    status = "all",
    sort = "updated_desc",
  } = options;
  return sortWorkflowBenchmarkReports(
    reports.filter(
      (report) =>
        (status === "all" || report.status === status)
        && workflowBenchmarkReportMatchesSearch(report, search),
    ),
    sort,
  );
}

export function workflowBenchmarkReportDraftHasUnsavedChanges({
  selectedReport,
  worksheet,
  reportName,
  reportStatus,
  scenarios,
  rubric,
}: WorkflowBenchmarkReportDraftState): boolean {
  const normalizedCurrent = normalizeWorkflowBakeoffWorksheet(
    worksheet,
    scenarios,
    rubric,
  );
  if (!selectedReport) {
    return (
      reportName.trim().length > 0
      || reportStatus !== "draft"
      || workflowBakeoffWorksheetHasEvidence(normalizedCurrent)
    );
  }
  const normalizedSaved = normalizeWorkflowBakeoffWorksheet(
    selectedReport.worksheet,
    scenarios,
    rubric,
  );
  return (
    reportName.trim() !== selectedReport.name
    || reportStatus !== selectedReport.status
    || JSON.stringify(normalizedCurrent) !== JSON.stringify(normalizedSaved)
  );
}

export function buildWorkflowBakeoffExportPayload({
  worksheet,
  scenarios,
  rubric,
  generatedAt = new Date().toISOString(),
}: {
  worksheet: WorkflowBakeoffWorksheet;
  scenarios: WorkflowBakeoffScenario[];
  rubric: WorkflowBakeoffRubricSection[];
  generatedAt?: string;
}): WorkflowBakeoffExportPayload {
  return {
    schema_version: 1,
    generated_at: generatedAt,
    worksheet,
    scenarios: scenarios.map((scenario) => ({
      ...scenario,
      worksheet:
        worksheet.scenarios[scenario.id]
        ?? createWorkflowBakeoffScenarioWorksheetEntry(),
    })),
    operator_rubric: rubric.map((section) => ({
      ...section,
      worksheet:
        worksheet.rubric[section.title]
        ?? createWorkflowBakeoffRubricWorksheetEntry(),
    })),
  };
}

export function buildWorkflowBakeoffMarkdown({
  worksheet,
  scenarios,
  rubric,
  generatedAt = new Date().toISOString(),
}: {
  worksheet: WorkflowBakeoffWorksheet;
  scenarios: WorkflowBakeoffScenario[];
  rubric: WorkflowBakeoffRubricSection[];
  generatedAt?: string;
}): string {
  const lines: string[] = [
    "# Workflow benchmark scorecard",
    "",
    `Generated: ${generatedAt}`,
    `Product: ${worksheet.product_name || "Caliber"}`,
    `Evaluator: ${worksheet.evaluator || "TBD"}`,
    `Environment: ${worksheet.environment || "TBD"}`,
    "",
    "## Session summary",
    worksheet.summary.trim() || "No summary recorded yet.",
    "",
    "## Scenario worksheet",
  ];
  for (const scenario of scenarios) {
    const entry =
      worksheet.scenarios[scenario.id]
      ?? createWorkflowBakeoffScenarioWorksheetEntry();
    lines.push(`### ${scenario.id} - ${scenario.title}`);
    lines.push(`Starter: ${scenario.starter_kind}`);
    lines.push(
      `Status: ${workflowBakeoffScenarioStatusLabel(entry.status)}`,
    );
    lines.push(
      `Minutes to first clean run: ${
        entry.minutes_to_first_success.trim() || "Not recorded"
      }`,
    );
    lines.push(
      `Evidence links / run ids: ${entry.evidence_links.trim() || "Not recorded"}`,
    );
    lines.push("Capabilities:");
    for (const capability of scenario.capabilities) {
      lines.push(`- ${capability}`);
    }
    lines.push("Capture:");
    for (const checkpoint of scenario.evidence_to_capture) {
      lines.push(`- ${checkpoint}`);
    }
    lines.push("Notes:");
    lines.push(entry.notes.trim() || "No notes recorded.");
    lines.push("");
  }
  lines.push("## Operator rubric");
  for (const section of rubric) {
    const entry =
      worksheet.rubric[section.title]
      ?? createWorkflowBakeoffRubricWorksheetEntry();
    lines.push(`### ${section.title}`);
    lines.push(`Score: ${entry.score || "Not scored"}`);
    lines.push("Checks:");
    for (const check of section.checks) {
      lines.push(`- ${check}`);
    }
    lines.push("Findings:");
    lines.push(entry.notes.trim() || "No findings recorded.");
    lines.push("");
  }
  return lines.join("\n").trim();
}
