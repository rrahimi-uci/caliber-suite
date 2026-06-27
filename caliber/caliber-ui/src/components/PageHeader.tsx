/**
 * PageHeader — the shared page chrome: breadcrumb, title, subtitle, and an
 * optional right-aligned actions slot. Used by every top-level page so the
 * header looks identical everywhere (consistent spacing, type scale, colors).
 */

import type { ReactNode } from "react";
import { Link } from "react-router-dom";

export interface PageHeaderProps {
  title: string;
  subtitle?: ReactNode;
  /** Right-aligned actions (buttons, etc.). */
  actions?: ReactNode;
  /** Breadcrumb trail after "Dashboard". Defaults to just the page title. */
  crumbs?: { label: string; to?: string }[];
  /** Hide the "Dashboard ›" breadcrumb (e.g. on the Dashboard page itself). */
  hideBreadcrumb?: boolean;
}

function Chevron(): JSX.Element {
  return (
    <svg className="h-3.5 w-3.5 text-slate-300" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M9 18l6-6-6-6" />
    </svg>
  );
}

export function PageHeader({
  title,
  subtitle,
  actions,
  crumbs,
  hideBreadcrumb = false,
}: PageHeaderProps): JSX.Element {
  const trail = crumbs ?? [{ label: title }];
  return (
    <div className="mb-6">
      {!hideBreadcrumb && (
        <nav className="mb-3 flex items-center gap-1.5 text-sm text-slate-400" aria-label="Breadcrumb">
          <Link to="/" className="transition-colors hover:text-slate-600">
            Dashboard
          </Link>
          {trail.map((c, i) => (
            <span key={`${c.label}-${i}`} className="flex items-center gap-1.5">
              <Chevron />
              {c.to ? (
                <Link to={c.to} className="transition-colors hover:text-slate-600">
                  {c.label}
                </Link>
              ) : (
                <span className="font-medium text-slate-900">{c.label}</span>
              )}
            </span>
          ))}
        </nav>
      )}
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <h1 className="text-2xl font-bold tracking-tight text-slate-900">{title}</h1>
          {subtitle && (
            <p className="mt-1 w-full break-words text-sm leading-relaxed text-slate-500">{subtitle}</p>
          )}
        </div>
        {actions && <div className="flex flex-shrink-0 items-center gap-2">{actions}</div>}
      </div>
    </div>
  );
}
