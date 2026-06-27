#!/usr/bin/env node

import { existsSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const STATUS_ORDER = ["failed", "broken", "skipped", "passed", "unknown"];
const STATUS_COLORS = {
  failed: "#dc2626",
  broken: "#f97316",
  skipped: "#64748b",
  passed: "#16a34a",
  unknown: "#a855f7",
};
const SERIES_COLORS = [
  "#0f766e",
  "#2563eb",
  "#9333ea",
  "#ea580c",
  "#0891b2",
  "#be185d",
  "#4f46e5",
  "#15803d",
  "#b45309",
  "#475569",
];

function readJson(path, fallback) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return fallback;
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function formatNumber(value) {
  return new Intl.NumberFormat("en-US").format(Number(value) || 0);
}

function formatPercent(value) {
  return `${(Number(value) || 0).toFixed(1)}%`;
}

function formatDuration(ms) {
  const value = Math.max(0, Number(ms) || 0);
  if (value < 1000) return `${Math.round(value)} ms`;
  if (value < 60_000) return `${(value / 1000).toFixed(1)} s`;
  const totalSeconds = Math.round(value / 1000);
  const seconds = totalSeconds % 60;
  const totalMinutes = Math.floor(totalSeconds / 60);
  const minutes = totalMinutes % 60;
  const hours = Math.floor(totalMinutes / 60);
  if (hours > 0) return `${hours}h ${minutes}m ${seconds}s`;
  return `${minutes}m ${seconds}s`;
}

function toStatusCounts(source = {}) {
  const counts = Object.fromEntries(STATUS_ORDER.map((status) => [status, 0]));
  for (const status of STATUS_ORDER) {
    counts[status] = Number(source?.[status] || 0);
  }
  counts.total = STATUS_ORDER.reduce((sum, status) => sum + counts[status], 0);
  return counts;
}

function statusLegend(counts) {
  return STATUS_ORDER.filter((status) => counts[status] > 0)
    .map(
      (status) => `
        <span class="legend-item">
          <span class="legend-swatch" style="background:${STATUS_COLORS[status]}"></span>
          <span>${escapeHtml(status)}: ${formatNumber(counts[status])}</span>
        </span>
      `,
    )
    .join("");
}

function renderStatusBar(counts) {
  const total = counts.total || 1;
  const segments = STATUS_ORDER.filter((status) => counts[status] > 0)
    .map((status) => {
      const pct = (counts[status] / total) * 100;
      return `<span class="stack-segment stack-segment_${status}" style="width:${pct.toFixed(4)}%;background:${STATUS_COLORS[status]}"></span>`;
    })
    .join("");
  return `<div class="stack-bar">${segments}</div>`;
}

function groupBy(rows, keyFn) {
  const groups = new Map();
  for (const row of rows) {
    const rawName = keyFn(row);
    const name = String(rawName || "Unlabeled").trim() || "Unlabeled";
    const group = groups.get(name) || {
      name,
      total: 0,
      durationMs: 0,
      retries: 0,
      flaky: 0,
      counts: toStatusCounts(),
    };
    group.total += 1;
    group.durationMs += row.durationMs;
    group.retries += row.retriesCount;
    if (row.flaky) group.flaky += 1;
    if (STATUS_ORDER.includes(row.status)) {
      group.counts[row.status] += 1;
      group.counts.total += 1;
    }
    groups.set(name, group);
  }
  return [...groups.values()];
}

function sortGroups(groups, key) {
  return [...groups].sort((a, b) => {
    const av = Number(a[key] || 0);
    const bv = Number(b[key] || 0);
    if (bv !== av) return bv - av;
    return a.name.localeCompare(b.name);
  });
}

function renderRankList(items, { titleKey = "name", valueKey, valueFormatter, caption }) {
  if (!items.length) {
    return `<p class="empty">${escapeHtml(caption || "No data available.")}</p>`;
  }
  const max = Math.max(...items.map((item) => Number(item[valueKey] || 0)), 1);
  const rows = items
    .map((item, index) => {
      const value = Number(item[valueKey] || 0);
      const width = Math.max(3, (value / max) * 100);
      const passRate = item.counts?.total
        ? formatPercent((100 * (item.counts.passed || 0)) / item.counts.total)
        : null;
      return `
        <div class="rank-row">
          <div class="rank-head">
            <span class="rank-index">${index + 1}</span>
            <span class="rank-name">${escapeHtml(item[titleKey])}</span>
            <span class="rank-value">${escapeHtml(valueFormatter(value))}</span>
          </div>
          <div class="rank-bar">
            <span class="rank-bar-fill" style="width:${width.toFixed(4)}%; background:${SERIES_COLORS[index % SERIES_COLORS.length]}"></span>
          </div>
          ${
            passRate
              ? `<div class="rank-meta">${renderInlineStatus(item.counts)} <span class="pill">pass ${passRate}</span></div>`
              : ""
          }
        </div>
      `;
    })
    .join("");
  return rows;
}

function renderInlineStatus(counts) {
  return STATUS_ORDER.filter((status) => counts?.[status] > 0)
    .map(
      (status) =>
        `<span class="inline-status"><span class="inline-status-dot" style="background:${STATUS_COLORS[status]}"></span>${escapeHtml(status)} ${formatNumber(counts[status])}</span>`,
    )
    .join("");
}

function renderTrendRows(items, { rowLabel, segments, summary }) {
  if (!items.length) {
    return `<p class="empty">No history has been generated yet.</p>`;
  }
  return items
    .map((item, index) => {
      const label = rowLabel(item, index);
      const parts = segments(item);
      const total = parts.reduce((sum, part) => sum + part.value, 0) || 1;
      const bar = parts
        .filter((part) => part.value > 0)
        .map((part) => {
          const width = (part.value / total) * 100;
          return `<span class="stack-segment" style="width:${width.toFixed(4)}%;background:${part.color}" title="${escapeHtml(`${part.label}: ${formatNumber(part.value)}`)}"></span>`;
        })
        .join("");
      return `
        <div class="trend-row">
          <div class="trend-label">${escapeHtml(label)}</div>
          <div class="stack-bar stack-bar_tall">${bar}</div>
          <div class="trend-summary">${summary(item)}</div>
        </div>
      `;
    })
    .join("");
}

function renderSingleSeriesRows(items, { rowLabel, value, color = "#2563eb", formatter }) {
  if (!items.length) {
    return `<p class="empty">No history has been generated yet.</p>`;
  }
  const max = Math.max(...items.map((item) => Number(value(item) || 0)), 1);
  return items
    .map((item, index) => {
      const raw = Number(value(item) || 0);
      const width = Math.max(raw > 0 ? 3 : 0, (raw / max) * 100);
      return `
        <div class="trend-row">
          <div class="trend-label">${escapeHtml(rowLabel(item, index))}</div>
          <div class="rank-bar rank-bar_tall">
            <span class="rank-bar-fill" style="width:${width.toFixed(4)}%; background:${color}"></span>
          </div>
          <div class="trend-summary">${escapeHtml(formatter(raw))}</div>
        </div>
      `;
    })
    .join("");
}

function renderSlowTable(rows) {
  if (!rows.length) {
    return `<p class="empty">No timing data was found in the report.</p>`;
  }
  const body = rows
    .map(
      (row) => `
        <tr>
          <td>${escapeHtml(row.name)}</td>
          <td>${escapeHtml(row.framework || "unknown")}</td>
          <td>${escapeHtml(row.feature || row.epic || "Unlabeled")}</td>
          <td class="num">${escapeHtml(formatDuration(row.durationMs))}</td>
          <td>${escapeHtml(row.status)}</td>
        </tr>
      `,
    )
    .join("");
  return `
    <table class="data-table">
      <thead>
        <tr>
          <th>Test</th>
          <th>Framework</th>
          <th>Feature</th>
          <th class="num">Duration</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>${body}</tbody>
    </table>
  `;
}

function renderSignalTable(rows) {
  if (!rows.length) {
    return `<p class="empty">No flaky, retried, failed, or broken tests were recorded in this launch.</p>`;
  }
  const body = rows
    .map(
      (row) => `
        <tr>
          <td>${escapeHtml(row.name)}</td>
          <td>${escapeHtml(row.framework || "unknown")}</td>
          <td>${escapeHtml(row.status)}</td>
          <td class="num">${escapeHtml(formatNumber(row.retriesCount))}</td>
          <td>${row.flaky ? "yes" : "no"}</td>
          <td>${escapeHtml(row.feature || row.epic || "Unlabeled")}</td>
        </tr>
      `,
    )
    .join("");
  return `
    <table class="data-table">
      <thead>
        <tr>
          <th>Test</th>
          <th>Framework</th>
          <th>Status</th>
          <th class="num">Retries</th>
          <th>Flaky</th>
          <th>Feature</th>
        </tr>
      </thead>
      <tbody>${body}</tbody>
    </table>
  `;
}

function loadTestCases(reportDir) {
  const dir = join(reportDir, "data", "test-cases");
  if (!existsSync(dir)) return [];
  const files = readdirSync(dir).filter((name) => name.endsWith(".json"));
  return files
    .map((name) => readJson(join(dir, name), null))
    .filter(Boolean)
    .map((item) => {
      const labels = new Map((item.labels || []).map((entry) => [entry.name, entry.value]));
      return {
        name: String(item.name || item.fullName || item.uid || "test"),
        fullName: String(item.fullName || item.name || item.uid || ""),
        status: String(item.status || "unknown"),
        durationMs: Number(item?.time?.duration || 0),
        retriesCount: Number(item.retriesCount || 0),
        flaky: Boolean(item.flaky),
        newFailed: Boolean(item.newFailed),
        newBroken: Boolean(item.newBroken),
        newPassed: Boolean(item.newPassed),
        framework: labels.get("framework") || "",
        epic: labels.get("epic") || "",
        feature: labels.get("feature") || "",
        suite: labels.get("suite") || labels.get("parentSuite") || "",
        packageName: labels.get("package") || "",
      };
    });
}

function buildInsights(reportDir) {
  const summary = readJson(join(reportDir, "widgets", "summary.json"), {});
  const historyTrend = readJson(join(reportDir, "history", "history-trend.json"), []).map((item) =>
    toStatusCounts(item?.data || {}),
  );
  const durationTrend = readJson(join(reportDir, "history", "duration-trend.json"), []).map((item) =>
    Number(item?.data?.duration || 0),
  );
  const retryTrend = readJson(join(reportDir, "history", "retry-trend.json"), []).map((item) => ({
    run: Number(item?.data?.run || 0),
    retry: Number(item?.data?.retry || 0),
  }));
  const categoriesTrend = readJson(join(reportDir, "history", "categories-trend.json"), []).map(
    (item) => item?.data || {},
  );
  const tests = loadTestCases(reportDir);
  const summaryCounts = toStatusCounts(summary?.statistic || {});
  const passRate =
    summaryCounts.total > 0 ? (100 * (summaryCounts.passed || 0)) / summaryCounts.total : 0;

  const frameworkGroups = sortGroups(groupBy(tests, (row) => row.framework || "unknown"), "total");
  const suiteGroups = sortGroups(groupBy(tests, (row) => row.suite || row.packageName || "suite"), "total");
  const featureGroups = sortGroups(
    groupBy(tests, (row) => row.feature || row.epic || "Unlabeled"),
    "durationMs",
  );
  const epicGroups = sortGroups(groupBy(tests, (row) => row.epic || "Unlabeled"), "total");

  const slowestTests = [...tests]
    .sort((a, b) => b.durationMs - a.durationMs || a.name.localeCompare(b.name))
    .slice(0, 15);
  const signalRows = [...tests]
    .filter(
      (row) =>
        row.flaky ||
        row.retriesCount > 0 ||
        row.status === "failed" ||
        row.status === "broken" ||
        row.newBroken ||
        row.newFailed,
    )
    .sort(
      (a, b) =>
        Number(b.flaky) - Number(a.flaky) ||
        b.retriesCount - a.retriesCount ||
        b.durationMs - a.durationMs,
    )
    .slice(0, 15);

  const signals = {
    flaky: tests.filter((row) => row.flaky).length,
    retried: tests.filter((row) => row.retriesCount > 0).length,
    newFailed: tests.filter((row) => row.newFailed).length,
    newBroken: tests.filter((row) => row.newBroken).length,
    skipped: summaryCounts.skipped || 0,
  };

  const cards = [
    { label: "Total tests", value: formatNumber(summaryCounts.total) },
    { label: "Pass rate", value: formatPercent(passRate) },
    {
      label: "Wall time",
      value: formatDuration(summary?.time?.duration || 0),
      note: `sum ${formatDuration(summary?.time?.sumDuration || 0)}`,
    },
    {
      label: "Max test runtime",
      value: formatDuration(summary?.time?.maxDuration || 0),
      note: `min ${formatDuration(summary?.time?.minDuration || 0)}`,
    },
    { label: "Flaky tests", value: formatNumber(signals.flaky) },
    { label: "Retried tests", value: formatNumber(signals.retried) },
  ];

  const suitesTop = suiteGroups.slice(0, 12);
  const featuresTop = featureGroups.slice(0, 12);
  const epicsTop = epicGroups.slice(0, 8);
  const frameworksTop = frameworkGroups.slice(0, 8);

  return {
    cards,
    summaryCounts,
    historyTrend,
    durationTrend,
    retryTrend,
    categoriesTrend,
    suitesTop,
    featuresTop,
    epicsTop,
    frameworksTop,
    slowestTests,
    signalRows,
    signals,
  };
}

function renderInsightsPage(data) {
  const historyLaunches = data.historyTrend.map((counts, index) => ({
    label: `Launch ${index + 1}`,
    counts,
  }));
  const durationLaunches = data.durationTrend.map((value, index) => ({
    label: `Launch ${index + 1}`,
    value,
  }));
  const retryLaunches = data.retryTrend.map((value, index) => ({
    label: `Launch ${index + 1}`,
    ...value,
  }));
  const categoryLaunches = data.categoriesTrend.map((value, index) => ({
    label: `Launch ${index + 1}`,
    categories: value,
  }));

  const categoryNames = [
    ...new Set(categoryLaunches.flatMap((row) => Object.keys(row.categories || {}))),
  ];
  const categoryColors = Object.fromEntries(
    categoryNames.map((name, index) => [name, SERIES_COLORS[index % SERIES_COLORS.length]]),
  );

  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CALIBER Quality Insights</title>
  <style>
    :root {
      --bg: #f8fafc;
      --panel: #ffffff;
      --line: #dbe4ee;
      --text: #0f172a;
      --muted: #475569;
      --subtle: #64748b;
      --accent: #0f766e;
      --shadow: 0 18px 38px rgba(15, 23, 42, 0.06);
      --radius: 18px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: linear-gradient(180deg, #f8fafc 0%, #eef6ff 100%);
      color: var(--text);
    }
    a { color: inherit; }
    .page {
      width: min(1400px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 28px 0 48px;
    }
    .hero {
      display: flex;
      justify-content: space-between;
      gap: 20px;
      align-items: flex-start;
      margin-bottom: 22px;
    }
    .hero-copy h1 {
      margin: 0;
      font-size: clamp(2rem, 3vw, 3rem);
      line-height: 1.02;
      letter-spacing: -0.04em;
    }
    .hero-copy p {
      margin: 12px 0 0;
      max-width: 76ch;
      color: var(--muted);
      line-height: 1.6;
    }
    .hero-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      justify-content: flex-end;
    }
    .button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      text-decoration: none;
      padding: 12px 16px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.92);
      box-shadow: var(--shadow);
      font-weight: 700;
    }
    .button-primary {
      background: linear-gradient(135deg, #0f766e 0%, #0f9f8c 100%);
      color: white;
      border-color: transparent;
    }
    .grid {
      display: grid;
      gap: 18px;
    }
    .grid-cards {
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      margin-bottom: 18px;
    }
    .grid-two {
      grid-template-columns: repeat(2, minmax(0, 1fr));
      margin-bottom: 18px;
    }
    .panel, .metric {
      background: rgba(255,255,255,0.92);
      border: 1px solid rgba(219, 228, 238, 0.9);
      box-shadow: var(--shadow);
      border-radius: var(--radius);
    }
    .metric {
      padding: 16px 18px;
    }
    .metric-label {
      color: var(--subtle);
      font-size: 0.82rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    .metric-value {
      margin-top: 8px;
      font-size: 1.8rem;
      font-weight: 800;
      letter-spacing: -0.04em;
    }
    .metric-note {
      margin-top: 6px;
      color: var(--subtle);
      font-size: 0.85rem;
    }
    .panel {
      padding: 20px;
    }
    .panel h2 {
      margin: 0;
      font-size: 1.15rem;
      letter-spacing: -0.03em;
    }
    .panel-copy {
      margin: 8px 0 16px;
      color: var(--muted);
      line-height: 1.55;
      font-size: 0.95rem;
    }
    .legend, .rank-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 8px 12px;
      margin-top: 12px;
      color: var(--muted);
      font-size: 0.84rem;
    }
    .legend-item, .inline-status, .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 9px;
      border-radius: 999px;
      background: #f8fafc;
      border: 1px solid var(--line);
    }
    .legend-swatch, .inline-status-dot {
      width: 10px;
      height: 10px;
      border-radius: 999px;
      display: inline-block;
    }
    .stack-bar, .rank-bar {
      width: 100%;
      display: flex;
      min-height: 14px;
      overflow: hidden;
      border-radius: 999px;
      background: #e2e8f0;
      border: 1px solid #d7e0ea;
    }
    .stack-bar_tall, .rank-bar_tall {
      min-height: 18px;
    }
    .stack-segment, .rank-bar-fill {
      display: block;
      min-width: 0;
    }
    .rank-row, .trend-row {
      padding: 12px 0;
      border-top: 1px solid rgba(219, 228, 238, 0.7);
    }
    .rank-row:first-child, .trend-row:first-child {
      border-top: 0;
      padding-top: 0;
    }
    .rank-head, .trend-row {
      display: grid;
      gap: 10px;
      align-items: center;
    }
    .rank-head {
      grid-template-columns: auto minmax(0, 1fr) auto;
      margin-bottom: 10px;
    }
    .trend-row {
      grid-template-columns: 100px minmax(0, 1fr) minmax(130px, max-content);
    }
    .rank-index {
      width: 26px;
      height: 26px;
      display: inline-grid;
      place-items: center;
      border-radius: 999px;
      background: #ecfeff;
      color: var(--accent);
      font-size: 0.8rem;
      font-weight: 800;
    }
    .rank-name {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-weight: 700;
    }
    .rank-value, .trend-summary, .num {
      text-align: right;
      font-variant-numeric: tabular-nums;
      color: var(--muted);
      font-weight: 700;
      white-space: nowrap;
    }
    .trend-label {
      font-size: 0.88rem;
      color: var(--muted);
      font-weight: 700;
      white-space: nowrap;
    }
    .data-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.93rem;
    }
    .data-table th,
    .data-table td {
      padding: 10px 0;
      border-top: 1px solid rgba(219, 228, 238, 0.75);
      text-align: left;
      vertical-align: top;
    }
    .data-table tr:first-child th,
    .data-table tbody tr:first-child td {
      border-top: 0;
    }
    .data-table th {
      color: var(--subtle);
      font-size: 0.76rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    .empty {
      margin: 0;
      color: var(--muted);
      line-height: 1.6;
    }
    .section-label {
      margin: 10px 0 14px;
      color: var(--subtle);
      font-size: 0.82rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    @media (max-width: 1080px) {
      .grid-two { grid-template-columns: 1fr; }
      .hero { flex-direction: column; }
      .hero-actions { justify-content: flex-start; }
    }
    @media (max-width: 720px) {
      .page { width: min(100vw - 20px, 1400px); padding-top: 20px; }
      .trend-row { grid-template-columns: 1fr; }
      .trend-summary, .num { text-align: left; }
      .rank-head { grid-template-columns: auto minmax(0, 1fr); }
      .rank-value { grid-column: 1 / -1; text-align: left; }
      .data-table { display: block; overflow-x: auto; }
    }
  </style>
</head>
<body>
  <main class="page">
    <section class="hero">
      <div class="hero-copy">
        <div class="section-label">Quality telemetry</div>
        <h1>CALIBER insights for the current Allure report</h1>
        <p>
          The stock Allure 2 Graphs tab is intentionally limited. This page derives extra insight from the same generated
          report artifacts: current launch health, recent launch history, framework and feature concentration, execution hotspots,
          and flake / retry signals. It is generated alongside the report and served by the same CALIBER route.
        </p>
      </div>
      <div class="hero-actions">
        <a class="button button-primary" href="./">Open Allure report</a>
        <a class="button" href="./#graph">Open Allure graphs</a>
      </div>
    </section>

    <section class="grid grid-cards">
      ${data.cards
        .map(
          (card) => `
            <article class="metric">
              <div class="metric-label">${escapeHtml(card.label)}</div>
              <div class="metric-value">${escapeHtml(card.value)}</div>
              ${card.note ? `<div class="metric-note">${escapeHtml(card.note)}</div>` : ""}
            </article>
          `,
        )
        .join("")}
    </section>

    <section class="grid grid-two">
      <article class="panel">
        <h2>Current launch status mix</h2>
        <p class="panel-copy">A compact view of pass, fail, broken, skipped, and unknown counts in the generated report.</p>
        ${renderStatusBar(data.summaryCounts)}
        <div class="legend">${statusLegend(data.summaryCounts)}</div>
      </article>
      <article class="panel">
        <h2>Current framework mix</h2>
        <p class="panel-copy">How much of the launch came from pytest, Vitest, Playwright, and any other adapters that emitted results.</p>
        ${renderRankList(data.frameworksTop, {
          valueKey: "total",
          valueFormatter: formatNumber,
          caption: "No framework labels were found in the report.",
        })}
      </article>
    </section>

    <section class="grid grid-two">
      <article class="panel">
        <h2>Recent launch status trend</h2>
        <p class="panel-copy">Each row is one historical launch preserved by Allure history. This becomes more valuable after repeated report generations.</p>
        ${renderTrendRows(historyLaunches, {
          rowLabel: (item) => item.label,
          segments: (item) =>
            STATUS_ORDER.map((status) => ({
              label: status,
              value: item.counts[status] || 0,
              color: STATUS_COLORS[status],
            })),
          summary: (item) =>
            `${formatNumber(item.counts.total)} total · ${formatPercent(
              item.counts.total ? (100 * item.counts.passed) / item.counts.total : 0,
            )} passed`,
        })}
      </article>
      <article class="panel">
        <h2>Recent launch wall-time trend</h2>
        <p class="panel-copy">Wall-clock duration per preserved launch, using the same history that feeds Allure trend widgets.</p>
        ${renderSingleSeriesRows(durationLaunches, {
          rowLabel: (item) => item.label,
          value: (item) => item.value,
          color: "#0f766e",
          formatter: formatDuration,
        })}
      </article>
    </section>

    <section class="grid grid-two">
      <article class="panel">
        <h2>Recent retry trend</h2>
        <p class="panel-copy">Automatic or repeated reruns carried in Allure history. Useful when failures are hidden by retries.</p>
        ${renderTrendRows(retryLaunches, {
          rowLabel: (item) => item.label,
          segments: (item) => [
            { label: "retry", value: item.retry, color: "#ea580c" },
            { label: "non-retry", value: Math.max(0, item.run - item.retry), color: "#cbd5e1" },
          ],
          summary: (item) =>
            `${formatNumber(item.retry)} retries / ${formatNumber(item.run)} results`,
        })}
      </article>
      <article class="panel">
        <h2>Recent category trend</h2>
        <p class="panel-copy">Known-issue or custom category counts recorded in recent launches.</p>
        ${
          categoryNames.length
            ? renderTrendRows(categoryLaunches, {
                rowLabel: (item) => item.label,
                segments: (item) =>
                  categoryNames.map((name) => ({
                    label: name,
                    value: Number(item.categories?.[name] || 0),
                    color: categoryColors[name],
                  })),
                summary: (item) =>
                  `${formatNumber(
                    categoryNames.reduce(
                      (sum, name) => sum + Number(item.categories?.[name] || 0),
                      0,
                    ),
                  )} categorized`,
              })
            : `<p class="empty">No category history was found in this report.</p>`
        }
        ${
          categoryNames.length
            ? `<div class="legend">${categoryNames
                .map(
                  (name) =>
                    `<span class="legend-item"><span class="legend-swatch" style="background:${categoryColors[name]}"></span>${escapeHtml(name)}</span>`,
                )
                .join("")}</div>`
            : ""
        }
      </article>
    </section>

    <section class="grid grid-two">
      <article class="panel">
        <h2>Top suites by volume</h2>
        <p class="panel-copy">The suites that contributed the largest number of tests to the launch.</p>
        ${renderRankList(data.suitesTop, {
          valueKey: "total",
          valueFormatter: formatNumber,
          caption: "No suite labels were found in the report.",
        })}
      </article>
      <article class="panel">
        <h2>Top features by cumulative test time</h2>
        <p class="panel-copy">Where execution time is concentrated. This is useful for spotting expensive domains before looking at individual tests.</p>
        ${renderRankList(data.featuresTop, {
          valueKey: "durationMs",
          valueFormatter: formatDuration,
          caption: "No feature labels were found in the report.",
        })}
      </article>
    </section>

    <section class="grid grid-two">
      <article class="panel">
        <h2>Top epics by coverage</h2>
        <p class="panel-copy">A higher-level product-area split built from Allure epic labels.</p>
        ${renderRankList(data.epicsTop, {
          valueKey: "total",
          valueFormatter: formatNumber,
          caption: "No epic labels were found in the report.",
        })}
      </article>
      <article class="panel">
        <h2>Signal summary</h2>
        <p class="panel-copy">Quick counts of instability indicators in the current launch.</p>
        <div class="legend">
          <span class="pill">flaky ${formatNumber(data.signals.flaky)}</span>
          <span class="pill">retried ${formatNumber(data.signals.retried)}</span>
          <span class="pill">new failed ${formatNumber(data.signals.newFailed)}</span>
          <span class="pill">new broken ${formatNumber(data.signals.newBroken)}</span>
          <span class="pill">skipped ${formatNumber(data.signals.skipped)}</span>
        </div>
      </article>
    </section>

    <section class="grid grid-two">
      <article class="panel">
        <h2>Slowest tests in the launch</h2>
        <p class="panel-copy">The largest single-test contributors to runtime.</p>
        ${renderSlowTable(data.slowestTests)}
      </article>
      <article class="panel">
        <h2>Flake and retry hotspots</h2>
        <p class="panel-copy">Tests that are already signaling instability through flake flags, retries, or non-passing states.</p>
        ${renderSignalTable(data.signalRows)}
      </article>
    </section>
  </main>
</body>
</html>`;
}

export function generateAllureInsights(reportDir = resolve("allure-report")) {
  const data = buildInsights(reportDir);
  const html = renderInsightsPage(data);
  const target = join(reportDir, "caliber-insights.html");
  writeFileSync(target, html, "utf8");
  return target;
}

export function injectInsightsEntry(reportDir = resolve("allure-report")) {
  const indexPath = join(reportDir, "index.html");
  if (!existsSync(indexPath)) return false;
  const html = readFileSync(indexPath, "utf8");
  if (html.includes("caliber-insights-entry")) return false;
  const snippet = `
    <style id="caliber-insights-entry-style">
      .caliber-insights-entry{
        position:fixed;
        right:18px;
        bottom:18px;
        z-index:9999;
        display:inline-flex;
        align-items:center;
        justify-content:center;
        gap:8px;
        padding:12px 16px;
        border-radius:999px;
        border:1px solid rgba(15,118,110,.22);
        background:linear-gradient(135deg,#0f766e 0%,#0f9f8c 100%);
        color:#fff;
        font:700 13px/1 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
        text-decoration:none;
        box-shadow:0 16px 32px rgba(15,23,42,.18);
      }
      .caliber-insights-entry:hover{filter:brightness(1.03)}
      .caliber-insights-entry__sub{opacity:.82;font-weight:600}
    </style>
    <a id="caliber-insights-entry" class="caliber-insights-entry" href="./caliber-insights.html">
      <span>CALIBER insights</span>
      <span class="caliber-insights-entry__sub">extra charts</span>
    </a>
  `;
  writeFileSync(indexPath, html.replace("</body>", `${snippet}\n</body>`), "utf8");
  return true;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const reportDir = resolve(process.argv[2] || "allure-report");
  const target = generateAllureInsights(reportDir);
  injectInsightsEntry(reportDir);
  console.log(`CALIBER insights written to ${target}`);
}
