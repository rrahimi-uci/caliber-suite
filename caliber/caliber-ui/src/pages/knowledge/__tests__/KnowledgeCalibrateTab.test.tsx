import { afterEach, describe, expect, it, vi } from "vitest";

import { caliberApi } from "@/api/caliberApi";
import type {
  KnowledgeBaseVersion,
  KnowledgeCalibrationRunDetail,
  KnowledgeCalibrationRunSummary,
} from "@/api/knowledgeTypes";
import { KnowledgeCalibrateTab } from "@/pages/knowledge/KnowledgeCalibrateTab";
import { render, screen, userEvent, waitFor } from "@/test/utils";

const version = (status = "completed"): KnowledgeBaseVersion => ({
  knowledge_base_version_id: "KBV-1",
  knowledge_base_id: "KB-1",
  version_number: 1,
  status,
  source_manifest: [],
  chunking_config: null,
  embedding_config: null,
  graph_config: null,
  output_bucket: null,
  output_prefix: null,
  artifact_uri: null,
  manifest_uri: null,
  logs_uri: null,
  stats_uri: null,
  sources_uri: null,
  entities_uri: null,
  relationships_uri: null,
  graph_uri: null,
  created_by: "@test",
  created_at: "2026-01-01T00:00:00Z",
  completed_at: status === "completed" ? "2026-01-01T00:01:00Z" : null,
});

const summary = (
  id: string,
  mode: string,
): KnowledgeCalibrationRunSummary => ({
  test_run_id: id,
  knowledge_base_id: "KB-1",
  knowledge_base_version_id: "KBV-1",
  eval_dataset_id: "DS-1",
  eval_dataset_version: 1,
  retrieval_mode: mode,
  top_k: 6,
  test_set_size: 2,
  metrics: {
    recall_at_k: 0.5,
    ndcg_at_k: 0.6,
    faithfulness: 0.7,
    answer_correctness: 0.8,
    passed_count: 1,
    partial_count: 0,
    failed_count: 1,
  },
  created_by: "@test",
  status: "completed",
  created_at: "2026-01-01T00:02:00Z",
  completed_at: "2026-01-01T00:03:00Z",
});

const detail = (
  id: string,
  verdict: string,
  score: number,
): KnowledgeCalibrationRunDetail => ({
  ...summary(id, id === "run-base" ? "dense" : "age_graph"),
  metrics: {
    recall_at_k: id === "run-base" ? 0.9 : 0.5,
    ndcg_at_k: id === "run-base" ? 0.9 : null,
    faithfulness: id === "run-base" ? 0.9 : 0.7,
    answer_correctness: id === "run-base" ? 0.9 : 0.8,
    passed_count: id === "run-base" ? 2 : 1,
    partial_count: 0,
    failed_count: id === "run-base" ? 0 : 1,
  },
  results: [
    {
      question: "Which policy applies?",
      verdict,
      recall_at_k: id === "run-base" ? 0.9 : 0.4,
      ndcg_at_k: id === "run-base" ? 0.9 : null,
      faithfulness: id === "run-base" ? 0.9 : 0.6,
      answer_correctness: id === "run-base" ? 0.9 : score,
      score,
      answer: id === "run-base" ? "The baseline answer" : null,
      answer_error: id === "run-base" ? null : "judge unavailable",
      gold_sources: ["policy.md"],
      retrieved_sources: id === "run-base" ? ["policy.md"] : [],
    },
    ...(id === "run-candidate"
      ? [
          {
            question: "A question only in the candidate",
            verdict: "pass",
            gold_sources: [],
            retrieved_sources: [],
          },
        ]
      : []),
  ],
});

describe("KnowledgeCalibrateTab", () => {
  afterEach(() => vi.restoreAllMocks());

  it("runs a calibration, expands question evidence, compares a baseline, and pins a run", async () => {
    const candidate = summary("run-candidate", "age_graph");
    const baseline = summary("run-base", "dense");
    const calibration = vi
      .spyOn(caliberApi, "calibrateKnowledgeBase")
      .mockResolvedValue(candidate);
    const listRuns = vi
      .spyOn(caliberApi, "listKnowledgeBaseTestRuns")
      .mockResolvedValue([candidate, baseline]);
    vi.spyOn(caliberApi, "listEvalDatasets").mockResolvedValue([
      {
        dataset_id: "DS-1",
        name: "Support questions",
        description: "",
        owner: "@test",
        tags: [],
        status: "active",
        version: 1,
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
    ]);
    vi.spyOn(caliberApi, "getKnowledgeBase").mockResolvedValue({
      baseline_run_id: "run-base",
    } as never);
    vi.spyOn(caliberApi, "getKnowledgeBaseTestRun").mockImplementation(
      async (id) => (id === "run-base" ? detail("run-base", "pass", 0.9) : detail("run-candidate", "fail", 0.4)),
    );
    const pin = vi
      .spyOn(caliberApi, "setKnowledgeBaseBaseline")
      .mockResolvedValue({ knowledge_base_id: "KB-1", baseline_run_id: "run-candidate" });

    const user = userEvent.setup();
    render(
      <KnowledgeCalibrateTab
        knowledgeBaseId="KB-1"
        versions={[version()]}
        activeVersionId="KBV-1"
      />,
    );

    expect(await screen.findByTestId("kb-calibrate-comparison")).toBeInTheDocument();
    expect(screen.getByText("1 regression")).toBeInTheDocument();
    expect(screen.getByText("A question only in the candidate")).toBeInTheDocument();
    expect(screen.getByText("baseline pass (90%) → candidate fail (40%)")).toBeInTheDocument();

    await user.click(screen.getAllByTestId("kb-calibrate-question-row")[0]!);
    expect(await screen.findByTestId("kb-calibrate-question-detail")).toHaveTextContent(
      "Retrieved sources: —",
    );
    expect(screen.getByText("Error: judge unavailable")).toBeInTheDocument();
    await user.click(screen.getAllByTestId("kb-calibrate-question-row")[0]!);
    expect(screen.queryByTestId("kb-calibrate-question-detail")).not.toBeInTheDocument();

    await user.selectOptions(screen.getByTestId("kb-calibrate-mode"), "hybrid");
    await user.clear(screen.getByTestId("kb-calibrate-topk"));
    await user.type(screen.getByTestId("kb-calibrate-topk"), "25");
    expect(screen.getByTestId("kb-calibrate-topk")).toHaveValue(20);
    await user.click(screen.getByTestId("kb-calibrate-run"));
    await waitFor(() => expect(calibration).toHaveBeenCalledWith("KB-1", {
      version_id: "KBV-1",
      eval_dataset_id: "DS-1",
      retrieval_mode: "hybrid",
      top_k: 20,
    }));
    expect(listRuns).toHaveBeenCalledTimes(2);

    await user.click(screen.getByTestId("kb-calibrate-set-baseline"));
    await waitFor(() => expect(pin).toHaveBeenCalledWith("KB-1", "run-candidate"));
    expect(await screen.findByTestId("kb-calibrate-baseline-marker")).toBeInTheDocument();
  });

  it("shows empty and unavailable states and surfaces run and baseline errors", async () => {
    vi.spyOn(caliberApi, "listEvalDatasets").mockRejectedValue(new Error("datasets unavailable"));
    vi.spyOn(caliberApi, "getKnowledgeBase").mockRejectedValue(new Error("kb unavailable"));
    vi.spyOn(caliberApi, "listKnowledgeBaseTestRuns").mockRejectedValue(new Error("history unavailable"));

    render(
      <KnowledgeCalibrateTab
        knowledgeBaseId="KB-1"
        versions={[]}
        activeVersionId={null}
      />,
    );
    expect(await screen.findByText(/No calibration runs yet/)).toBeInTheDocument();
    expect(screen.getByText(/No test sets yet/)).toBeInTheDocument();
    expect(screen.getByTestId("kb-calibrate-run")).toBeDisabled();
  });

  it("surfaces calibration and baseline mutation failures for a scored-empty run", async () => {
    const run = summary("run-empty", "dense");
    vi.spyOn(caliberApi, "listEvalDatasets").mockResolvedValue([
      {
        dataset_id: "DS-1",
        name: "Questions",
        description: "",
        owner: "@test",
        tags: [],
        status: "active",
        version: 1,
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
    ]);
    vi.spyOn(caliberApi, "getKnowledgeBase").mockResolvedValue({
      baseline_run_id: null,
    } as never);
    vi.spyOn(caliberApi, "listKnowledgeBaseTestRuns").mockResolvedValue([run]);
    vi.spyOn(caliberApi, "getKnowledgeBaseTestRun").mockResolvedValue({
      ...run,
      results: [],
    } as never);
    vi.spyOn(caliberApi, "calibrateKnowledgeBase").mockRejectedValue(
      new Error("calibration unavailable"),
    );
    vi.spyOn(caliberApi, "setKnowledgeBaseBaseline").mockRejectedValue(
      new Error("baseline unavailable"),
    );

    const user = userEvent.setup();
    render(
      <KnowledgeCalibrateTab
        knowledgeBaseId="KB-1"
        versions={[version()]}
        activeVersionId="KBV-1"
      />,
    );

    expect(await screen.findByText("This run has no scored questions.")).toBeInTheDocument();
    await user.click(screen.getByTestId("kb-calibrate-set-baseline"));
    expect(await screen.findByText("baseline unavailable")).toBeInTheDocument();
    await user.click(screen.getByTestId("kb-calibrate-run"));
    expect(await screen.findByText("calibration unavailable")).toBeInTheDocument();
  });
});
