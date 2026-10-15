/**
 * Route guard and inline notice for the alpha's feature-flagged areas (see
 * ``hooks/useFeature.ts``).
 *
 * ``FeatureGate`` wraps a whole page. A disabled area gets a panel that says so, names the
 * area and the server variable that turns it on, and links back to the dashboard: not a blank
 * page, and not a redirect, so a bookmark to a disabled area explains itself instead of
 * bouncing the user somewhere unrelated. While capabilities load it renders a neutral
 * placeholder, never the page, so a default install cannot flash an unsupported screen.
 *
 * ``FeatureDisabledNotice`` is the small version for a control inside an otherwise-supported
 * page that depends on a disabled area (for example, generating test cases uses Aria).
 */

import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import type { FeatureId, FeatureInfo } from "@/api/workflowTypes";
import { useFeature } from "@/hooks/useFeature";

export function FeatureGate({
  feature,
  children,
}: {
  feature: FeatureId;
  children: ReactNode;
}): JSX.Element {
  const { enabled, loading, info } = useFeature(feature);
  if (loading) {
    return (
      <div
        role="status"
        data-testid="feature-gate-loading"
        className="flex min-h-[40vh] items-center justify-center"
      >
        <div className="animate-pulse text-sm text-gray-400">Loading…</div>
      </div>
    );
  }
  if (!enabled) return <FeatureDisabledPanel info={info} />;
  return <>{children}</>;
}

export function FeatureDisabledPanel({
  info,
}: {
  info: FeatureInfo;
}): JSX.Element {
  return (
    <div
      data-testid="feature-disabled-panel"
      className="mx-auto max-w-xl rounded-2xl border border-slate-200/70 bg-white p-8 shadow-card dark:border-white/10 dark:bg-slate-900"
    >
      <h1 className="text-xl font-semibold text-gray-900 dark:text-slate-100">
        This isn&apos;t available on this deployment
      </h1>
      <p className="mt-1 text-sm font-medium text-gray-700 dark:text-slate-200">
        {info.label}
      </p>
      <p className="mt-3 text-sm text-gray-500 dark:text-slate-400">
        An administrator can turn it on by setting{" "}
        <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-xs text-slate-800 dark:bg-white/10 dark:text-slate-200">
          {info.env_var}=true
        </code>{" "}
        on the CALIBER server and restart it.
      </p>
      <Link
        to="/"
        className="mt-5 inline-block text-sm font-medium text-caliber-600 hover:underline dark:text-caliber-400"
      >
        Back to the dashboard
      </Link>
    </div>
  );
}

/**
 * An inline explanation for something that needs a disabled area. ``children`` is for a link
 * to what still works (for example, the Test Sets page).
 */
export function FeatureDisabledNotice({
  feature,
  what,
  children,
}: {
  feature: FeatureId;
  /** The thing that is unavailable, as a noun phrase: "Generating test cases". */
  what: string;
  children?: ReactNode;
}): JSX.Element | null {
  const { info, loading } = useFeature(feature);
  // Say nothing until the server has answered: "disabled" shown for a beat on a deployment that
  // enabled the area would be wrong, and a default install shows nothing either way.
  if (loading) return null;
  return (
    <div
      role="note"
      data-testid="feature-disabled-notice"
      className="rounded-lg border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-700 dark:border-white/10 dark:bg-white/5 dark:text-slate-300"
    >
      {what} needs {info.label}, which isn&apos;t enabled on this deployment. An
      administrator can enable it by setting{" "}
      <code className="font-mono text-xs">{info.env_var}=true</code> on the
      server.
      {children ? <> {children}</> : null}
    </div>
  );
}
