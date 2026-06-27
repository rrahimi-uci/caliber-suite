import { describe, expect, it } from "vitest";

import type {
  WorkflowBenchmarkReport,
  WorkflowBakeoffRubricSection,
  WorkflowBakeoffScenario,
} from "@/api/workflowTypes";
import {
  buildWorkflowBakeoffExportPayload,
  buildWorkflowBakeoffMarkdown,
  createWorkflowBakeoffWorksheet,
  filterWorkflowBenchmarkReports,
  normalizeWorkflowBakeoffWorksheet,
  parseWorkflowBakeoffEvidenceReferences,
  workflowBenchmarkReportDraftHasUnsavedChanges,
  workflowBakeoffRubricAverage,
  workflowBakeoffWorksheetHasEvidence,
  workflowBakeoffWorksheetStats,
} from "@/lib/workflowBakeoff";

const SCENARIOS: WorkflowBakeoffScenario[] = [
  {
    id: "B1",
    title: "Single-agent answer with tools",
    starter_kind: "single_agent",
    capabilities: ["agent execution"],
    evidence_to_capture: ["Final output"],
  },
  {
    id: "B9",
    title: "AGE graph retrieval",
    starter_kind: "knowledge_age",
    capabilities: ["Apache AGE retrieval mode"],
    evidence_to_capture: ["Graph evidence"],
  },
];

const RUBRIC: WorkflowBakeoffRubricSection[] = [
  {
    title: "Authoring friction",
    checks: ["Time to create the workflow from a starter."],
  },
];

function makeBenchmarkReport(
  overrides: Partial<WorkflowBenchmarkReport> = {},
): WorkflowBenchmarkReport {
  const worksheet = normalizeWorkflowBakeoffWorksheet(
    overrides.worksheet ?? {},
    SCENARIOS,
    RUBRIC,
  );
  return {
    report_id: "WFB-1",
    name: "Caliber benchmark",
    owner: "@owner",
    status: "draft",
    product_name: worksheet.product_name,
    evaluator: worksheet.evaluator,
    environment: worksheet.environment,
    summary: worksheet.summary,
    scenario_count: SCENARIOS.length,
    captured_count: Object.values(worksheet.scenarios).filter((entry) =>
      entry.status !== "not_started"
      || entry.minutes_to_first_success
      || entry.evidence_links
      || entry.notes,
    ).length,
    passed_count: Object.values(worksheet.scenarios).filter(
      (entry) => entry.status === "passed",
    ).length,
    blocked_count: Object.values(worksheet.scenarios).filter(
      (entry) => entry.status === "blocked",
    ).length,
    worksheet,
    created_at: "2026-06-15T12:00:00Z",
    updated_at: "2026-06-15T12:00:00Z",
    ...overrides,
    product_name: overrides.product_name ?? worksheet.product_name,
    evaluator: overrides.evaluator ?? worksheet.evaluator,
    environment: overrides.environment ?? worksheet.environment,
    summary: overrides.summary ?? worksheet.summary,
    worksheet,
  };
}

describe("workflowBakeoff", () => {
  it("normalizes partial worksheet data and backfills missing entries", () => {
    const worksheet = normalizeWorkflowBakeoffWorksheet(
      {
        product_name: "n8n",
        scenarios: {
          B9: {
            status: "passed",
            minutes_to_first_success: "6",
            notes: "Strong graph evidence.",
          },
        },
      },
      SCENARIOS,
      RUBRIC,
    );

    expect(worksheet.product_name).toBe("n8n");
    expect(worksheet.scenarios.B1.status).toBe("not_started");
    expect(worksheet.scenarios.B9.status).toBe("passed");
    expect(worksheet.scenarios.B9.minutes_to_first_success).toBe("6");
    expect(worksheet.scenarios.B9.evidence_links).toBe("");
    expect(worksheet.rubric["Authoring friction"].score).toBe("");
  });

  it("builds export payloads and markdown from worksheet evidence", () => {
    const worksheet = createWorkflowBakeoffWorksheet(SCENARIOS, RUBRIC);
    worksheet.evaluator = "Ops reviewer";
    worksheet.environment = "staging";
    worksheet.summary = "Caliber stayed stable during graph retrieval.";
    worksheet.scenarios.B9.status = "passed";
    worksheet.scenarios.B9.evidence_links = "WR-AGE-1";
    worksheet.scenarios.B9.notes = "AGE returned neighbors and citations.";
    worksheet.rubric["Authoring friction"].score = "4";
    worksheet.rubric["Authoring friction"].notes =
      "Starter required minimal edits.";

    const payload = buildWorkflowBakeoffExportPayload({
      worksheet,
      scenarios: SCENARIOS,
      rubric: RUBRIC,
      generatedAt: "2026-06-15T12:00:00Z",
    });
    const markdown = buildWorkflowBakeoffMarkdown({
      worksheet,
      scenarios: SCENARIOS,
      rubric: RUBRIC,
      generatedAt: "2026-06-15T12:00:00Z",
    });

    expect(payload.schema_version).toBe(1);
    expect(payload.scenarios[1]?.worksheet.status).toBe("passed");
    expect(payload.operator_rubric[0]?.worksheet.score).toBe("4");
    expect(markdown).toContain("# Workflow benchmark scorecard");
    expect(markdown).toContain("Generated: 2026-06-15T12:00:00Z");
    expect(markdown).toContain("### B9 - AGE graph retrieval");
    expect(markdown).toContain("Evidence links / run ids: WR-AGE-1");
    expect(markdown).toContain("Score: 4");
  });

  it("computes worksheet stats and rubric averages", () => {
    const worksheet = createWorkflowBakeoffWorksheet(SCENARIOS, RUBRIC);
    worksheet.scenarios.B1.status = "blocked";
    worksheet.scenarios.B9.status = "passed";
    worksheet.scenarios.B9.minutes_to_first_success = "6";
    worksheet.rubric["Authoring friction"].score = "4";

    expect(workflowBakeoffWorksheetStats(worksheet, SCENARIOS)).toEqual({
      scenario_count: 2,
      captured_count: 2,
      passed_count: 1,
      blocked_count: 1,
    });
    expect(workflowBakeoffRubricAverage(worksheet, RUBRIC)).toBe(4);
  });

  it("treats rubric-only edits as worksheet evidence", () => {
    const worksheet = createWorkflowBakeoffWorksheet(SCENARIOS, RUBRIC);

    expect(workflowBakeoffWorksheetHasEvidence(worksheet)).toBe(false);

    worksheet.rubric["Authoring friction"].score = "5";

    expect(workflowBakeoffWorksheetHasEvidence(worksheet)).toBe(true);
  });

  it("filters, sorts, and detects dirty benchmark report drafts", () => {
    const current = makeBenchmarkReport({
      report_id: "WFB-1",
      name: "Caliber baseline",
      status: "completed",
      worksheet: {
        product_name: "Caliber",
        evaluator: "Ops reviewer",
        scenarios: {
          B9: {
            status: "passed",
            minutes_to_first_success: "5",
            evidence_links: "WR-CAL-1",
            notes: "Clean first run.",
          },
        },
        rubric: {
          "Authoring friction": {
            score: "4",
            notes: "Fast setup.",
          },
        },
      },
      updated_at: "2026-06-15T12:20:00Z",
    });
    const compare = makeBenchmarkReport({
      report_id: "WFB-2",
      name: "n8n comparison",
      status: "completed",
      worksheet: {
        product_name: "n8n",
        evaluator: "Ops reviewer",
        scenarios: {
          B1: {
            status: "blocked",
            minutes_to_first_success: "",
            evidence_links: "RUN-2",
            notes: "Needed manual setup.",
          },
        },
      },
      updated_at: "2026-06-15T12:10:00Z",
    });
    const archived = makeBenchmarkReport({
      report_id: "WFB-3",
      name: "Archived AGE run",
      status: "archived",
      worksheet: {
        product_name: "LangFlow",
        evaluator: "Analyst",
        scenarios: {
          B1: {
            status: "passed",
            minutes_to_first_success: "9",
            evidence_links: "RUN-3",
            notes: "Archived sample.",
          },
          B9: {
            status: "passed",
            minutes_to_first_success: "11",
            evidence_links: "RUN-4",
            notes: "Archived graph run.",
          },
        },
      },
      updated_at: "2026-06-15T12:30:00Z",
    });

    expect(
      filterWorkflowBenchmarkReports(
        [current, compare, archived],
        { search: "comparison", status: "completed", sort: "passed_desc" },
      ).map((report) => report.report_id),
    ).toEqual(["WFB-2"]);

    expect(
      filterWorkflowBenchmarkReports(
        [current, compare, archived],
        { sort: "captured_desc" },
      ).map((report) => report.report_id),
    ).toEqual(["WFB-3", "WFB-1", "WFB-2"]);

    expect(
      workflowBenchmarkReportDraftHasUnsavedChanges({
        selectedReport: current,
        worksheet: current.worksheet,
        reportName: current.name,
        reportStatus: current.status,
        scenarios: SCENARIOS,
        rubric: RUBRIC,
      }),
    ).toBe(false);

    const dirtyWorksheet = normalizeWorkflowBakeoffWorksheet(
      current.worksheet,
      SCENARIOS,
      RUBRIC,
    );
    dirtyWorksheet.summary = "Need one more retest.";

    expect(
      workflowBenchmarkReportDraftHasUnsavedChanges({
        selectedReport: current,
        worksheet: dirtyWorksheet,
        reportName: current.name,
        reportStatus: current.status,
        scenarios: SCENARIOS,
        rubric: RUBRIC,
      }),
    ).toBe(true);
  });

  it("parses workflow and run references from benchmark evidence text", () => {
    expect(
      parseWorkflowBakeoffEvidenceReferences(
        "Primary retest WF-123 reopened WR-456, then archived WR-456 after comparing WF-789.",
      ),
    ).toEqual([
      {
        key: "workflow:WF-123",
        type: "workflow",
        id: "WF-123",
        href: "/workflows/WF-123",
        label: "Workflow WF-123",
      },
      {
        key: "run:WR-456",
        type: "run",
        id: "WR-456",
        href: "/workflow-runs/WR-456",
        label: "Run WR-456",
      },
      {
        key: "workflow:WF-789",
        type: "workflow",
        id: "WF-789",
        href: "/workflows/WF-789",
        label: "Workflow WF-789",
      },
    ]);
  });
});
