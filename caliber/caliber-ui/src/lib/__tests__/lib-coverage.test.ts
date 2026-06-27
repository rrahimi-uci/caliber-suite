import { afterEach, describe, expect, it } from "vitest";

import type { KnowledgeSourceSelection } from "@/api/knowledgeTypes";
import type { RefinementJob } from "@/api/types";
import {
  appendThemeHintToUrl,
  buildCaliberHref,
  buildMlflowHref,
} from "@/lib/externalLinks";
import {
  buildKnowledgeBuildLaunchPath,
  parseKnowledgeBuildLaunchParams,
  stripKnowledgeBuildLaunchParams,
  type KnowledgeBuildLaunchPayload,
} from "@/lib/knowledgeBuildLaunch";
import {
  formatWorkflowCalibrationDelta,
  formatWorkflowCalibrationScore,
  humanizeWorkflowCalibrationLabel,
  workflowCalibrationView,
} from "@/lib/workflowCalibration";

/**
 * Coverage-focused unit tests for three pure-logic libs:
 *   - knowledgeBuildLaunch.ts  (build/parse/strip launch URL params)
 *   - workflowCalibration.ts   (formatters + payload → view projection)
 *   - externalLinks.ts         (port-5001 unified-origin + hash + theme branches)
 *
 * These are pure functions, so each test is input → expected output. Where a
 * branch depends on `window.location.port` (the local-suite :5001 → :5050
 * gateway hop), we temporarily override `window.location` and restore it in
 * `afterEach` so tests stay isolated.
 */

const REAL_LOCATION = window.location;

function setPort(port: string): void {
  Object.defineProperty(window, "location", {
    configurable: true,
    writable: true,
    value: {
      ...REAL_LOCATION,
      protocol: "http:",
      hostname: "localhost",
      port,
      origin: `http://localhost${port ? `:${port}` : ""}`,
    },
  });
}

afterEach(() => {
  // Restore the real jsdom Location and clear any leaked state.
  Object.defineProperty(window, "location", {
    configurable: true,
    writable: true,
    value: REAL_LOCATION,
  });
  window.localStorage.clear();
  window.__CALIBER_STATIC_PREFIX__ = undefined;
});

describe("knowledgeBuildLaunch", () => {
  const SOURCES: KnowledgeSourceSelection[] = [
    { kind: "file", path: "docs/intro.pdf" },
    { kind: "folder", path: "guides/" },
  ];

  it("builds a launch path with mode, bucket, preset, and every source", () => {
    const payload: KnowledgeBuildLaunchPayload = {
      bucket: "kb-alpha",
      sources: SOURCES,
      buildMode: "existing",
      graphPreset: "age_native",
    };
    const path = buildKnowledgeBuildLaunchPath(payload);
    const [base, query] = path.split("?");
    expect(base).toBe("/knowledge-bases");
    const params = new URLSearchParams(query);
    expect(params.get("tab")).toBe("build");
    expect(params.get("build_mode")).toBe("existing");
    expect(params.get("bucket")).toBe("kb-alpha");
    expect(params.get("graph_preset")).toBe("age_native");
    expect(params.getAll("source")).toEqual([
      "file:docs/intro.pdf",
      "folder:guides/",
    ]);
  });

  it("omits graph_preset when no preset is selected", () => {
    const path = buildKnowledgeBuildLaunchPath({
      bucket: "kb-beta",
      sources: [{ kind: "file", path: "a.txt" }],
      buildMode: "new",
      graphPreset: null,
    });
    const params = new URLSearchParams(path.split("?")[1]);
    expect(params.has("graph_preset")).toBe(false);
    expect(params.get("build_mode")).toBe("new");
  });

  it("round-trips build → parse for a full payload", () => {
    const payload: KnowledgeBuildLaunchPayload = {
      bucket: "kb-alpha",
      sources: SOURCES,
      buildMode: "existing",
      graphPreset: "age_strict",
    };
    const path = buildKnowledgeBuildLaunchPath(payload);
    const parsed = parseKnowledgeBuildLaunchParams(
      new URLSearchParams(path.split("?")[1]),
    );
    expect(parsed).toEqual(payload);
  });

  it("defaults build_mode to 'new' and drops an unknown graph_preset", () => {
    const parsed = parseKnowledgeBuildLaunchParams(
      new URLSearchParams(
        "tab=build&bucket=kb&source=file:x.txt&build_mode=bogus&graph_preset=mystery",
      ),
    );
    expect(parsed).not.toBeNull();
    expect(parsed?.buildMode).toBe("new");
    expect(parsed?.graphPreset).toBeNull();
  });

  it("returns null when tab is not 'build'", () => {
    expect(
      parseKnowledgeBuildLaunchParams(
        new URLSearchParams("tab=search&bucket=kb&source=file:x.txt"),
      ),
    ).toBeNull();
  });

  it("returns null when bucket is missing or whitespace-only", () => {
    expect(
      parseKnowledgeBuildLaunchParams(
        new URLSearchParams("tab=build&source=file:x.txt"),
      ),
    ).toBeNull();
    expect(
      parseKnowledgeBuildLaunchParams(
        new URLSearchParams("tab=build&bucket=%20%20&source=file:x.txt"),
      ),
    ).toBeNull();
  });

  it("returns null when there are no source params", () => {
    expect(
      parseKnowledgeBuildLaunchParams(
        new URLSearchParams("tab=build&bucket=kb"),
      ),
    ).toBeNull();
  });

  it("returns null when every source is malformed (decode → empty)", () => {
    // No separator, leading separator, unknown kind, and empty path all decode
    // to null, leaving zero valid sources.
    const parsed = parseKnowledgeBuildLaunchParams(
      new URLSearchParams(
        "tab=build&bucket=kb&source=nopath&source=:onlysep&source=db:tbl&source=file:",
      ),
    );
    expect(parsed).toBeNull();
  });

  it("keeps only well-formed sources and trims the bucket", () => {
    const parsed = parseKnowledgeBuildLaunchParams(
      new URLSearchParams(
        "tab=build&bucket=%20kb-trim%20&source=file:good.txt&source=garbage&source=folder:dir/",
      ),
    );
    expect(parsed?.bucket).toBe("kb-trim");
    expect(parsed?.sources).toEqual([
      { kind: "file", path: "good.txt" },
      { kind: "folder", path: "dir/" },
    ]);
  });

  it("preserves a colon inside the source path (splits on first ':' only)", () => {
    const parsed = parseKnowledgeBuildLaunchParams(
      new URLSearchParams("tab=build&bucket=kb&source=file:a:b:c.txt"),
    );
    expect(parsed?.sources).toEqual([{ kind: "file", path: "a:b:c.txt" }]);
  });

  it("strips only the launch keys and keeps unrelated params intact", () => {
    const stripped = stripKnowledgeBuildLaunchParams(
      new URLSearchParams(
        "tab=build&build_mode=new&bucket=kb&graph_preset=portable&source=file:a&source=file:b&keep=yes&page=2",
      ),
    );
    expect(stripped.get("keep")).toBe("yes");
    expect(stripped.get("page")).toBe("2");
    for (const key of ["tab", "build_mode", "bucket", "graph_preset", "source"]) {
      expect(stripped.has(key)).toBe(false);
    }
  });
});

describe("workflowCalibration formatters", () => {
  it("humanizes labels by replacing underscores, with em-dash fallback", () => {
    expect(humanizeWorkflowCalibrationLabel("age_native_strict")).toBe(
      "age native strict",
    );
    expect(humanizeWorkflowCalibrationLabel("plain")).toBe("plain");
    expect(humanizeWorkflowCalibrationLabel(null)).toBe("—");
    expect(humanizeWorkflowCalibrationLabel("")).toBe("—");
  });

  it("formats finite scores to 3dp and non-numbers to em-dash", () => {
    expect(formatWorkflowCalibrationScore(0.5)).toBe("0.500");
    expect(formatWorkflowCalibrationScore(1)).toBe("1.000");
    expect(formatWorkflowCalibrationScore(null)).toBe("—");
    expect(formatWorkflowCalibrationScore(undefined)).toBe("—");
    expect(formatWorkflowCalibrationScore(Number.NaN)).toBe("—");
    expect(formatWorkflowCalibrationScore(Number.POSITIVE_INFINITY)).toBe("—");
  });

  it("formats deltas as signed percentage-points", () => {
    expect(formatWorkflowCalibrationDelta(0.123)).toBe("+12.3pp");
    expect(formatWorkflowCalibrationDelta(-0.05)).toBe("-5.0pp");
    expect(formatWorkflowCalibrationDelta(0)).toBe("0.0pp");
    expect(formatWorkflowCalibrationDelta(null)).toBe("—");
    expect(formatWorkflowCalibrationDelta(undefined)).toBe("—");
    expect(formatWorkflowCalibrationDelta(Number.NaN)).toBe("—");
  });
});

describe("workflowCalibrationView", () => {
  function makeJob(overrides: Partial<RefinementJob>): RefinementJob {
    return {
      job_id: "JOB-1",
      agent_id: "AG-1",
      workflow_id: "WF-1",
      primary_item_id: "ITEM-1",
      mlflow_run_id: null,
      artifact_type: "workflow_manifest",
      optimizer_type: null,
      status: "completed",
      current_stage: "done",
      attempt_count: 1,
      error_message: null,
      total_tokens: 0,
      cost_usd: 0,
      bundle_targets: [],
      bundle_expansion_count: 0,
      diagnosis: null,
      candidate: null,
      eval_results: null,
      calibration_spec: null,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      ...overrides,
    };
  }

  it("returns null when artifact_type is not 'workflow_manifest'", () => {
    expect(
      workflowCalibrationView(
        makeJob({ artifact_type: "skill", calibration_spec: { objective: {} } }),
      ),
    ).toBeNull();
  });

  it("returns null when workflow_id is missing", () => {
    expect(
      workflowCalibrationView(
        makeJob({ workflow_id: null, calibration_spec: { objective: {} } }),
      ),
    ).toBeNull();
  });

  it("returns null when calibration_spec is not a record", () => {
    expect(
      workflowCalibrationView(makeJob({ calibration_spec: null })),
    ).toBeNull();
  });

  it("projects a full calibration spec with winner-resolved candidate", () => {
    const job = makeJob({
      calibration_spec: {
        objective: { maximize: "accuracy", epsilon: 0.01 },
        budget: { max_candidates: 4 },
        judge: { enabled: true },
        dataset_summary: { dataset_name: "golden", example_count: 12 },
        workflow_version_id: "WV-base",
      },
      candidate: {
        calibration_winner_id: "C2",
        calibration_patch_id: "PATCH-9",
        calibration_low_confidence: true,
        calibration_n_examples: 8,
        prompt_suggestion: "Tighten the system prompt.",
        content: "manifest yaml ...",
        // semantic_ops here is empty so the view falls back to the winner's ops.
        semantic_ops: [],
        calibration_candidates: [
          {
            candidate_id: "C1",
            summary: "first",
            accepted: false,
            rejected_reason: "regressed",
            patch_kind: "prompt",
            scores: { accuracy: 0.7, junk: "no" },
            deltas: { accuracy: 0.0 },
            semantic_ops: ["rewrite_prompt"],
            gate: { reasons: ["below_epsilon"] },
          },
          {
            candidate_id: "C2",
            summary: "winner",
            accepted: true,
            rejected_reason: null,
            patch_kind: "prompt_v2",
            scores: { accuracy: 0.92 },
            deltas: { accuracy: 0.22 },
            // mix of string + object semantic ops, plus an unlabelable entry.
            semantic_ops: [
              "merge_nodes",
              { op: "add_tool" },
              { kind: "drop_edge" },
              { nothing: true },
              42,
            ],
            gate: { reasons: ["passed_epsilon"] },
          },
        ],
      },
      eval_results: {
        gate: { passed: true, reasons: ["winner_beats_baseline"] },
      },
    });

    const view = workflowCalibrationView(job);
    expect(view).not.toBeNull();
    if (!view) return;

    expect(view.workflowId).toBe("WF-1");
    expect(view.baselineVersionId).toBe("WV-base");
    expect(view.objective).toBe("accuracy");
    expect(view.epsilon).toBe(0.01);
    expect(view.maxCandidates).toBe(4);
    expect(view.datasetName).toBe("golden");
    expect(view.datasetExampleCount).toBe(12);
    expect(view.judgeEnabled).toBe(true);
    expect(view.patchId).toBe("PATCH-9");
    expect(view.lowConfidence).toBe(true);
    expect(view.nExamples).toBe(8);
    expect(view.winnerId).toBe("C2");
    expect(view.promptSuggestion).toBe("Tighten the system prompt.");
    expect(view.candidateManifestText).toBe("manifest yaml ...");

    // Winner-derived fields (candidate has no patch_kind/summary of its own).
    expect(view.patchKind).toBe("prompt_v2");
    expect(view.summary).toBe("winner");
    // Candidate semantic_ops was [] → falls back to winner's, humanized,
    // dropping the non-labelable (42 / {nothing:true}) entries.
    expect(view.semanticOps).toEqual(["merge nodes", "add tool", "drop edge"]);

    // Gate comes from eval_results.gate.
    expect(view.gatePassed).toBe(true);
    expect(view.gateReasons).toEqual(["winner_beats_baseline"]);

    // Candidates parsed; non-numeric score values filtered out.
    expect(view.candidates).toHaveLength(2);
    expect(view.candidates[0]?.scores).toEqual({ accuracy: 0.7 });
    expect(view.candidates[0]?.gateReasons).toEqual(["below_epsilon"]);
  });

  it("falls back to candidate.calibration_gate and eval_results candidates", () => {
    const job = makeJob({
      calibration_spec: { objective: { maximize: "f1" } },
      candidate: {
        calibration_gate: { passed: false, reasons: ["candidate_gate"] },
      },
      eval_results: {
        calibration_winner_id: "W1",
        calibration_patch_id: "P-evt",
        n_examples: 3,
        calibration_candidates: [
          { candidate_id: "W1", scores: {}, deltas: {} },
          { not_a_candidate: true },
        ],
      },
    });

    const view = workflowCalibrationView(job);
    expect(view?.winnerId).toBe("W1");
    expect(view?.patchId).toBe("P-evt");
    expect(view?.nExamples).toBe(3);
    expect(view?.gatePassed).toBe(false);
    expect(view?.gateReasons).toEqual(["candidate_gate"]);
    // The malformed candidate (no candidate_id) is dropped.
    expect(view?.candidates).toHaveLength(1);
    expect(view?.candidates[0]?.candidateId).toBe("W1");
  });

  it("defaults lowConfidence to false and tolerates empty/absent collections", () => {
    const view = workflowCalibrationView(
      makeJob({ calibration_spec: { objective: {} } }),
    );
    expect(view).not.toBeNull();
    expect(view?.lowConfidence).toBe(false);
    expect(view?.candidates).toEqual([]);
    expect(view?.semanticOps).toEqual([]);
    expect(view?.gateReasons).toEqual([]);
    expect(view?.winnerId).toBeNull();
    expect(view?.objective).toBeNull();
    expect(view?.gatePassed).toBeNull();
  });
});

describe("externalLinks port-5001 + hash branches", () => {
  it("normalizes a '/#'-prefixed hash by stripping the leading slash", () => {
    expect(buildMlflowHref({ hash: "/#/experiments/7" })).toBe(
      "/?ui=mlflow#/experiments/7",
    );
  });

  it("normalizes a bare hash (no leading '#', strips a leading slash)", () => {
    expect(buildMlflowHref({ hash: "metrics" })).toBe(
      "/?ui=mlflow#metrics",
    );
    expect(buildMlflowHref({ hash: "/metrics" })).toBe(
      "/?ui=mlflow#metrics",
    );
  });

  it("treats an empty hash as no hash", () => {
    expect(buildMlflowHref({ hash: "" })).toBe("/?ui=mlflow");
  });

  it("routes the CALIBER link through the :5050 gateway on port 5001", () => {
    setPort("5001");
    expect(buildCaliberHref()).toBe("http://localhost:5050/caliber/");
  });

  it("routes the MLflow link through the :5050 gateway on port 5001", () => {
    setPort("5001");
    expect(buildMlflowHref({ hash: "#/experiments/1" })).toBe(
      "http://localhost:5050/?ui=mlflow#/experiments/1",
    );
  });

  it("carries the theme hint across the :5001 → :5050 gateway hop", () => {
    setPort("5001");
    expect(buildCaliberHref({ theme: "dark" })).toBe(
      "http://localhost:5050/caliber/?theme=dark",
    );
    expect(buildMlflowHref({ theme: "light" })).toBe(
      "http://localhost:5050/?ui=mlflow&theme=light",
    );
  });

  it("static prefix wins over the gateway hop even on port 5001", () => {
    setPort("5001");
    window.__CALIBER_STATIC_PREFIX__ = "/proxy";
    expect(buildCaliberHref()).toBe("/proxy/caliber/");
    expect(buildMlflowHref()).toBe("/proxy/");
  });

  it("appendThemeHintToUrl returns href unchanged when theme is null or href empty", () => {
    expect(appendThemeHintToUrl("/foo", null)).toBe("/foo");
    expect(appendThemeHintToUrl("", "dark")).toBe("");
  });

  it("appendThemeHintToUrl keeps relative URLs relative (path+search+hash)", () => {
    expect(appendThemeHintToUrl("/dash?a=1#sec", "dark")).toBe(
      "/dash?a=1&theme=dark#sec",
    );
  });
});
