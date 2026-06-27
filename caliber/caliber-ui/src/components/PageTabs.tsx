/**
 * PageTabs — the shared underline tab bar used across Prompts, Skills,
 * Workflows, MCP, etc. Renders plain ``<button>`` elements (so existing
 * ``getByRole("button", { name })`` tests keep working) with one consistent
 * active style (brand caliber underline). An optional right-aligned ``actions``
 * slot sits on the same row as the tabs.
 */

import type { ReactNode } from "react";

export interface PageTab {
  key: string;
  label: string;
  icon?: ReactNode;
}

export interface PageTabsProps {
  tabs: PageTab[];
  active: string;
  onChange: (key: string) => void;
  actions?: ReactNode;
}

export function PageTabs({ tabs, active, onChange, actions }: PageTabsProps): JSX.Element {
  return (
    <div className="mb-6 flex items-center justify-between border-b border-slate-200">
      <div className="flex items-center gap-1" role="tablist">
        {tabs.map((tab) => {
          const isActive = tab.key === active;
          return (
            <button
              key={tab.key}
              type="button"
              aria-selected={isActive}
              onClick={() => onChange(tab.key)}
              className={`-mb-px flex items-center gap-1.5 border-b-2 px-4 py-2.5 text-sm font-medium transition-colors ${
                isActive
                  ? "border-caliber-600 text-caliber-700"
                  : "border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-700"
              }`}
            >
              {tab.icon && <span className="h-4 w-4">{tab.icon}</span>}
              {tab.label}
            </button>
          );
        })}
      </div>
      {actions && <div className="flex flex-shrink-0 items-center gap-2 pb-2">{actions}</div>}
    </div>
  );
}
