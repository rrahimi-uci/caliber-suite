import { describe, expect, it } from "vitest";

import type {
  KnowledgeCalibrationMetrics,
  KnowledgeCalibrationQuestionResult,
} from "@/api/knowledgeTypes";
import {
  count,
  formatDate,
  headlineScore,
  metricValue,
  pct,
  questionMetric,
  retrievalModeLabel,
  signedPct,
  verdictTone,
} from "@/pages/knowledge/KnowledgeCalibrateTab";

const metrics = (overrides: Partial<KnowledgeCalibrationMetrics> = {}) => ({
  recall_at_k: 0.4,
  ndcg_at_k: 0.5,
  faithfulness: 0.6,
  answer_correctness: 0.7,
  ...overrides,
}) as KnowledgeCalibrationMetrics;

const result = (overrides: Partial<KnowledgeCalibrationQuestionResult> = {}) => ({
  question: "Which policy applies?",
  verdict: "pass",
  recall_at_k: 0.4,
  ndcg_at_k: 0.5,
  faithfulness: 0.6,
  answer_correctness: 0.7,
  ...overrides,
}) as KnowledgeCalibrationQuestionResult;

describe("knowledge calibration display helpers", () => {
  it("formats retrieval modes, dates, metric values, and percentages", () => {
    expect(retrievalModeLabel("dense")).toBe("Dense");
    expect(retrievalModeLabel("hybrid")).toBe("Hybrid");
    expect(retrievalModeLabel("graph_hybrid")).toBe("GraphRAG hybrid");
    expect(retrievalModeLabel("age_graph")).toBe("Apache AGE");
    expect(retrievalModeLabel("future")).toBe("future");
    expect(formatDate(null)).toBe("—");
    expect(formatDate("not-a-date")).toBe("—");
    expect(formatDate("2026-01-01T00:00:00Z")).not.toBe("—");
    expect(metricValue(null, "recall_at_k")).toBeNull();
    expect(metricValue(metrics({ recall_at_k: Number.NaN }), "recall_at_k")).toBeNull();
    expect(metricValue(metrics(), "answer_correctness")).toBe(0.7);
    expect(questionMetric(result({ faithfulness: Number.POSITIVE_INFINITY }), "faithfulness")).toBeNull();
    expect(questionMetric(result(), "ndcg_at_k")).toBe(0.5);
    expect(pct(null)).toBe("—");
    expect(pct(0.712)).toBe("71%");
    expect(signedPct(0.1)).toBe("+10%");
    expect(signedPct(0)).toBe("+0%");
    expect(signedPct(-0.1)).toBe("-10%");
  });

  it("selects headline fallbacks, counts values, and labels verdicts", () => {
    expect(headlineScore(metrics())).toBe(0.7);
    expect(headlineScore(metrics({ answer_correctness: undefined, faithfulness: 0.61 }))).toBe(0.61);
    expect(headlineScore(metrics({ answer_correctness: undefined, faithfulness: undefined, ndcg_at_k: 0.51 }))).toBe(0.51);
    expect(headlineScore(metrics({ answer_correctness: undefined, faithfulness: undefined, ndcg_at_k: undefined, recall_at_k: 0.41 }))).toBe(0.41);
    expect(headlineScore(metrics({ answer_correctness: undefined, faithfulness: undefined, ndcg_at_k: undefined, recall_at_k: undefined }))).toBeNull();
    expect(count(metrics(), "recall_at_k")).toBe(0.4);
    expect(count(metrics({ recall_at_k: Number.NaN }), "recall_at_k")).toBe(0);
    expect(count(metrics(), "missing")).toBe(0);
    expect(verdictTone("pass")).toContain("emerald");
    expect(verdictTone("passed")).toContain("emerald");
    expect(verdictTone("partial")).toContain("amber");
    expect(verdictTone("fail")).toContain("red");
    expect(verdictTone("unknown")).toContain("red");
    expect(count(metrics({ faithfulness: null as never }), "faithfulness")).toBe(0);
  });
});
