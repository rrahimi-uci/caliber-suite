/**
 * Workflow Version Detail (§16.1) — manifest, validation, compile, and export
 * for a single version.
 */

import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { caliberApi } from "@/api/caliberApi";
import type { CompileResult, ValidationReport } from "@/api/workflowTypes";
import { ProblemsPanel } from "@/components/workflows/ProblemsPanel";
import { useApiMutation, useApiQuery } from "@/hooks/useApiQuery";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function exportModeLabel(mode: string | null): string | null {
  if (mode === "agents_sdk_direct") return "Direct Agents SDK export";
  if (mode === "runtime_ir") return "Full runtime export";
  return null;
}

function exportModeHelp(mode: string | null): string | null {
  if (mode === "agents_sdk_direct") {
    return "This version compiles to a readable agent-only module because its executable graph stays within the direct Agents SDK export path.";
  }
  if (mode === "runtime_ir") {
    return "This version embeds the workflow IR and executes through the CALIBER runtime so templates, tools, checkpoints, knowledge queries, and other non-agent nodes keep full behavior fidelity.";
  }
  return null;
}

export function WorkflowVersionDetail(): JSX.Element {
  const { versionId } = useParams<{ versionId: string }>();
  const [report, setReport] = useState<ValidationReport | null>(null);
  const [compiled, setCompiled] = useState<CompileResult | null>(null);

  const versionQuery = useApiQuery(
    ["workflow-version", versionId],
    (s) => caliberApi.getWorkflowVersion(versionId!, s),
    { enabled: Boolean(versionId) },
  );
  const validateMut = useApiMutation(() => caliberApi.validateWorkflowVersion(versionId!), {
    onSuccess: setReport,
  });
  const compileMut = useApiMutation(() => caliberApi.compileWorkflowVersion(versionId!), {
    onSuccess: (data) => {
      setCompiled(data);
      void versionQuery.refetch();
    },
  });

  const version = versionQuery.data;
  if (versionQuery.isLoading || !version) {
    return <div className="text-sm text-gray-400">Loading version…</div>;
  }
  const compilerReport =
    (compiled?.report as Record<string, unknown> | null | undefined)
    ?? (version.compiled_bundle?.compiler_report as Record<string, unknown> | null | undefined)
    ?? null;
  const compiledCode =
    compiled?.generated_python
    ?? (typeof version.compiled_bundle?.generated_python === "string"
      ? version.compiled_bundle.generated_python
      : "");
  const compiledRequirements = compiled?.requirements ?? version.compiled_bundle?.requirements ?? [];
  const compiledArtifactUri = compiled?.compiled_artifact_uri ?? version.compiled_artifact_uri;
  const compiledCompilerVersion = compiled?.compiler_version ?? version.compiler_version;
  const compiledManifestHash = compiled?.manifest_hash ?? version.manifest_hash;
  const compileMs = typeof compiled?.compile_ms === "number" ? compiled.compile_ms : null;
  const compileCached = typeof compiled?.cached === "boolean" ? compiled.cached : null;
  const hasCompiledOutput = Boolean(
    compiledCode
    || compilerReport
    || compiledRequirements.length > 0
    || compiledArtifactUri
    || compiledCompilerVersion,
  );
  const exportMode =
    compilerReport && typeof compilerReport.export_mode === "string"
      ? compilerReport.export_mode
      : null;
  const exportModeTitle = exportModeLabel(exportMode);
  const exportModeDescription = exportModeHelp(exportMode);

  return (
    <div data-testid="version-detail">
      <Link to={`/workflows/${version.workflow_id}`} className="text-xs text-gray-400 hover:underline">
        ← Workflow
      </Link>
      <h1 className="text-xl font-semibold text-gray-900">
        Version v{version.version_number}{" "}
        <span className="text-xs text-gray-400">({version.status})</span>
      </h1>

      <div className="my-3 flex gap-2">
        <button
          type="button"
          data-testid="vd-validate"
          disabled={validateMut.isPending}
          onClick={() => validateMut.mutate(undefined)}
          className="rounded border border-gray-300 px-2 py-1 text-xs"
        >
          {validateMut.isPending ? "Validating…" : "Validate"}
        </button>
        <button
          type="button"
          data-testid="vd-compile"
          disabled={compileMut.isPending}
          onClick={() => compileMut.mutate(undefined)}
          className="rounded border border-gray-300 px-2 py-1 text-xs"
        >
          {compileMut.isPending ? "Compiling…" : "Compile"}
        </button>
        <a
          data-testid="vd-export-manifest"
          href={`${API_BASE}/workflow-versions/${version.version_id}/export/manifest`}
          className="rounded border border-gray-300 px-2 py-1 text-xs"
        >
          Export YAML
        </a>
        <a
          data-testid="vd-export-python"
          href={`${API_BASE}/workflow-versions/${version.version_id}/export/python`}
          className="rounded border border-gray-300 px-2 py-1 text-xs"
        >
          Export Python
        </a>
      </div>

      <ProblemsPanel report={report ?? version.validation_report} />

      {exportModeTitle && (
        <div
          data-testid="vd-export-mode"
          className="mt-3 rounded-xl border border-sky-200/70 bg-sky-50/80 px-3 py-3 text-xs text-sky-900"
        >
          <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-sky-700">
            Export mode
          </div>
          <div className="mt-1 text-sm font-semibold text-slate-900">{exportModeTitle}</div>
          {exportModeDescription && <div className="mt-1 leading-relaxed">{exportModeDescription}</div>}
        </div>
      )}

      {hasCompiledOutput && (
        <div className="mt-3 space-y-3">
          <div
            data-testid="vd-compile-summary"
            className="rounded-xl border border-slate-200 bg-slate-50/80 px-3 py-3 text-xs text-slate-700"
          >
            <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-500">
              Compile summary
            </div>
            <dl className="mt-2 grid gap-2 sm:grid-cols-2">
              <div>
                <dt className="font-semibold text-slate-500">Manifest hash</dt>
                <dd className="font-mono text-[11px] text-slate-800">{compiledManifestHash}</dd>
              </div>
              <div>
                <dt className="font-semibold text-slate-500">Compiler version</dt>
                <dd>{compiledCompilerVersion ?? "—"}</dd>
              </div>
              <div>
                <dt className="font-semibold text-slate-500">Artifact URI</dt>
                <dd className="break-all font-mono text-[11px] text-slate-800">
                  {compiledArtifactUri ?? "—"}
                </dd>
              </div>
              <div>
                <dt className="font-semibold text-slate-500">Compile status</dt>
                <dd>
                  {compileCached === true
                    ? "Cache hit"
                    : compileCached === false
                      ? "Fresh compile"
                      : "Persisted bundle"}
                  {compileMs !== null ? ` · ${Math.round(compileMs)} ms` : ""}
                </dd>
              </div>
            </dl>
          </div>

          {compiledRequirements.length > 0 && (
            <div>
              <div className="text-xs font-semibold text-gray-500">Requirements</div>
              <div data-testid="vd-compile-requirements" className="mt-2 flex flex-wrap gap-2">
                {compiledRequirements.map((requirement) => (
                  <span
                    key={requirement}
                    className="rounded-full border border-slate-200 bg-white px-3 py-1 text-[11px] font-mono text-slate-700"
                  >
                    {requirement}
                  </span>
                ))}
              </div>
            </div>
          )}

          {compilerReport && (
            <div>
              <div className="text-xs font-semibold text-gray-500">Compiler report</div>
              <pre
                data-testid="vd-compile-report"
                className="mt-1 max-h-48 overflow-auto rounded bg-slate-950 p-2 text-[11px] text-slate-100"
              >
                {JSON.stringify(compilerReport, null, 2)}
              </pre>
            </div>
          )}

          <div>
            <div className="text-xs font-semibold text-gray-500">Compiled code</div>
          <pre
            data-testid="vd-compiled-code"
            className="mt-1 max-h-72 overflow-auto rounded bg-gray-900 p-2 text-[11px] text-gray-100"
          >
            {compiledCode}
          </pre>
          </div>
        </div>
      )}

      <div className="mt-3">
        <div className="text-xs font-semibold text-gray-500">Manifest</div>
        <pre
          data-testid="vd-manifest"
          className="mt-1 max-h-96 overflow-auto rounded bg-gray-50 p-2 text-[11px]"
        >
          {JSON.stringify(version.manifest, null, 2)}
        </pre>
      </div>
    </div>
  );
}
