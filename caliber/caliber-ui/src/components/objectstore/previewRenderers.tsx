/**
 * Inline content renderers for the Object Store file preview.
 *
 * - `MarkdownView` renders a safe subset of Markdown to React elements (never
 *   `dangerouslySetInnerHTML`, so file content can't inject markup/scripts).
 * - `DataTable` renders extracted spreadsheet/CSV rows, with a sheet switcher
 *   when a workbook has multiple sheets.
 *
 * These keep the viewer dependency-free; Office documents are extracted
 * server-side (see routes/object_store.py `extract_object`).
 */
import { useMemo, useState, type ReactNode } from "react";

import type { ObjectStoreSheet } from "@/api/workflowTypes";

// ── Markdown ────────────────────────────────────────────────────────────────

/** Allow only hrefs that can't execute script. Returns null to render as text. */
function safeHref(raw: string): string | null {
  const url = raw.trim();
  if (/^(https?:|mailto:)/i.test(url)) return url;
  if (/^[/#.]/.test(url)) return url; // root / anchor / relative path
  if (!/^[a-z][a-z0-9+.-]*:/i.test(url)) return url; // no scheme → relative
  return null; // javascript:, data:, etc.
}

/** Regex capture group as a string ("" when the optional group didn't match). */
function grp(match: RegExpExecArray, index: number): string {
  return match[index] ?? "";
}

interface InlinePattern {
  re: RegExp;
  render: (match: RegExpExecArray, key: string) => ReactNode;
}

const INLINE_PATTERNS: InlinePattern[] = [
  // Inline code first so emphasis inside it stays literal.
  {
    re: /`([^`]+)`/,
    render: (m, key) => (
      <code
        key={key}
        className="rounded bg-slate-100 px-1 py-0.5 font-mono text-[0.85em] text-caliber-700 dark:bg-slate-800 dark:text-violet-200"
      >
        {grp(m, 1)}
      </code>
    ),
  },
  {
    re: /!\[([^\]]*)\]\(([^)\s]+)[^)]*\)/,
    render: (m, key) => {
      const href = safeHref(grp(m, 2));
      return href ? (
        <img key={key} src={href} alt={grp(m, 1)} className="my-2 max-h-80 rounded-lg" />
      ) : (
        <span key={key}>{grp(m, 0)}</span>
      );
    },
  },
  {
    re: /\[([^\]]+)\]\(([^)\s]+)[^)]*\)/,
    render: (m, key) => {
      const href = safeHref(grp(m, 2));
      return href ? (
        <a
          key={key}
          href={href}
          target="_blank"
          rel="noreferrer"
          className="font-medium text-caliber-purple underline decoration-caliber-300 underline-offset-2"
        >
          {renderInline(grp(m, 1), key)}
        </a>
      ) : (
        <span key={key}>{grp(m, 1)}</span>
      );
    },
  },
  {
    re: /\*\*([^*]+)\*\*|__([^_]+)__/,
    render: (m, key) => (
      <strong key={key} className="font-semibold text-slate-900 dark:text-slate-100">
        {renderInline(grp(m, 1) || grp(m, 2), key)}
      </strong>
    ),
  },
  {
    re: /\*([^*]+)\*|_([^_]+)_/,
    render: (m, key) => <em key={key}>{renderInline(grp(m, 1) || grp(m, 2), key)}</em>,
  },
];

/** Tokenize a line of inline markdown into React nodes (links, emphasis, code). */
function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let rest = text;
  let i = 0;
  while (rest) {
    let best: { p: InlinePattern; m: RegExpExecArray } | null = null;
    for (const p of INLINE_PATTERNS) {
      const m = new RegExp(p.re).exec(rest);
      if (m && (best === null || m.index < best.m.index)) best = { p, m };
    }
    if (!best) {
      nodes.push(rest);
      break;
    }
    if (best.m.index > 0) nodes.push(rest.slice(0, best.m.index));
    nodes.push(best.p.render(best.m, `${keyPrefix}-${i}`));
    rest = rest.slice(best.m.index + best.m[0].length);
    i += 1;
  }
  return nodes;
}

const HEADING_CLASSES = [
  "mt-4 mb-2 text-2xl font-bold text-slate-900 dark:text-slate-100",
  "mt-4 mb-2 text-xl font-bold text-slate-900 dark:text-slate-100",
  "mt-3 mb-1.5 text-lg font-semibold text-slate-900 dark:text-slate-100",
  "mt-3 mb-1.5 text-base font-semibold text-slate-800 dark:text-slate-200",
  "mt-2 mb-1 text-sm font-semibold text-slate-800 dark:text-slate-200",
  "mt-2 mb-1 text-sm font-semibold uppercase tracking-wide text-slate-600 dark:text-slate-300",
];

/** Block-level Markdown → React (headings, lists, quotes, code, tables, rules). */
function renderBlocks(source: string): ReactNode[] {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const at = (i: number): string => lines[i] ?? "";
  const blocks: ReactNode[] = [];
  let key = 0;
  const nextKey = (): string => `b${key++}`;

  for (let idx = 0; idx < lines.length; ) {
    const line = at(idx);

    // Fenced code block
    if (/^```/.test(line.trim())) {
      const body: string[] = [];
      idx += 1;
      while (idx < lines.length && !/^```/.test(at(idx).trim())) {
        body.push(at(idx));
        idx += 1;
      }
      idx += 1; // closing fence
      blocks.push(
        <pre
          key={nextKey()}
          className="my-3 overflow-auto rounded-xl border border-slate-200/70 bg-slate-950 px-4 py-3 text-xs leading-relaxed text-slate-100"
        >
          <code>{body.join("\n")}</code>
        </pre>,
      );
      continue;
    }

    // Blank line
    if (!line.trim()) {
      idx += 1;
      continue;
    }

    // Heading
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      const level = grp(heading, 1).length;
      const Tag = `h${level}` as keyof JSX.IntrinsicElements;
      blocks.push(
        <Tag key={nextKey()} className={HEADING_CLASSES[level - 1]}>
          {renderInline(grp(heading, 2), nextKey())}
        </Tag>,
      );
      idx += 1;
      continue;
    }

    // Horizontal rule
    if (/^(\*\*\*|---|___)\s*$/.test(line.trim())) {
      blocks.push(
        <hr key={nextKey()} className="my-4 border-slate-200 dark:border-slate-700" />,
      );
      idx += 1;
      continue;
    }

    // GFM table: header row of pipes followed by a separator row
    const sepLine = at(idx + 1);
    if (
      line.includes("|") &&
      idx + 1 < lines.length &&
      /^\s*\|?[\s:|-]+\|?\s*$/.test(sepLine) &&
      sepLine.includes("-")
    ) {
      const splitRow = (row: string): string[] =>
        row
          .trim()
          .replace(/^\||\|$/g, "")
          .split("|")
          .map((c) => c.trim());
      const headers = splitRow(line);
      idx += 2;
      const rows: string[][] = [];
      while (idx < lines.length && at(idx).includes("|") && at(idx).trim()) {
        rows.push(splitRow(at(idx)));
        idx += 1;
      }
      blocks.push(
        <div key={nextKey()} className="my-3 overflow-auto">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr>
                {headers.map((h, i) => (
                  <th
                    key={i}
                    className="border border-slate-200 bg-slate-50 px-3 py-1.5 text-left font-semibold text-slate-700 dark:border-slate-700 dark:bg-slate-800"
                  >
                    {renderInline(h, `${nextKey()}h${i}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, ri) => (
                <tr key={ri}>
                  {headers.map((_, ci) => (
                    <td
                      key={ci}
                      className="border border-slate-200 px-3 py-1.5 text-slate-700 dark:border-slate-700 dark:text-slate-200"
                    >
                      {renderInline(r[ci] ?? "", `${nextKey()}c${ri}-${ci}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }

    // Lists (flat)
    const ordered = /^\s*\d+\.\s+/.test(line);
    const unordered = /^\s*[-*+]\s+/.test(line);
    if (ordered || unordered) {
      const items: ReactNode[] = [];
      const itemRe = ordered ? /^\s*\d+\.\s+(.*)$/ : /^\s*[-*+]\s+(.*)$/;
      while (idx < lines.length && itemRe.test(at(idx))) {
        const m = itemRe.exec(at(idx));
        items.push(
          <li key={items.length}>{renderInline(m ? grp(m, 1) : "", nextKey())}</li>,
        );
        idx += 1;
      }
      const ListTag = ordered ? "ol" : "ul";
      blocks.push(
        <ListTag
          key={nextKey()}
          className={`my-2 ${ordered ? "list-decimal" : "list-disc"} space-y-1 pl-6 text-sm text-slate-700 dark:text-slate-200`}
        >
          {items}
        </ListTag>,
      );
      continue;
    }

    // Blockquote
    if (/^\s*>\s?/.test(line)) {
      const quoted: string[] = [];
      while (idx < lines.length && /^\s*>\s?/.test(at(idx))) {
        quoted.push(at(idx).replace(/^\s*>\s?/, ""));
        idx += 1;
      }
      blocks.push(
        <blockquote
          key={nextKey()}
          className="my-3 border-l-4 border-caliber-200 bg-slate-50/70 px-4 py-2 text-sm italic text-slate-600 dark:border-slate-700 dark:bg-slate-900/60 dark:text-slate-300"
        >
          {renderInline(quoted.join(" "), nextKey())}
        </blockquote>,
      );
      continue;
    }

    // Paragraph (gather consecutive plain lines)
    const para: string[] = [];
    while (
      idx < lines.length &&
      at(idx).trim() &&
      !/^(#{1,6})\s+/.test(at(idx)) &&
      !/^```/.test(at(idx).trim()) &&
      !/^\s*([-*+]|\d+\.)\s+/.test(at(idx)) &&
      !/^\s*>\s?/.test(at(idx))
    ) {
      para.push(at(idx));
      idx += 1;
    }
    blocks.push(
      <p
        key={nextKey()}
        className="my-2 text-sm leading-relaxed text-slate-700 dark:text-slate-200"
      >
        {renderInline(para.join(" "), nextKey())}
      </p>,
    );
  }
  return blocks;
}

export function MarkdownView({ source }: { source: string }): JSX.Element {
  const blocks = useMemo(() => renderBlocks(source || ""), [source]);
  return (
    <div
      data-testid="object-preview-markdown"
      className="max-h-[60vh] overflow-auto rounded-2xl border border-slate-200/70 bg-white px-6 py-4 dark:border-slate-700/70 dark:bg-slate-950"
    >
      {blocks.length ? blocks : <p className="text-sm text-slate-400">(empty file)</p>}
    </div>
  );
}

// ── Spreadsheet / CSV ─────────────────────────────────────────────────────────

/** Parse CSV/TSV text into rows (handles quoted fields + embedded delimiters). */
export function parseDelimited(text: string, delimiter: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i += 1;
        } else quoted = false;
      } else field += ch;
      continue;
    }
    if (ch === '"') quoted = true;
    else if (ch === delimiter) {
      row.push(field);
      field = "";
    } else if (ch === "\n") {
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
    } else if (ch !== "\r") field += ch;
  }
  if (field.length || row.length) {
    row.push(field);
    rows.push(row);
  }
  return rows.filter((r) => r.some((c) => c !== ""));
}

export function DataTable({
  sheets,
  truncated,
}: {
  sheets: ObjectStoreSheet[];
  truncated?: boolean;
}): JSX.Element {
  const [active, setActive] = useState(0);
  if (sheets.length === 0) {
    return <p className="text-sm text-slate-400">No rows to display.</p>;
  }
  const sheet = sheets[Math.min(active, sheets.length - 1)] ?? { name: "", rows: [] };
  const header = sheet.rows[0];
  const body = sheet.rows.slice(1);
  return (
    <div data-testid="object-preview-table" className="space-y-3">
      {sheets.length > 1 && (
        <div className="flex flex-wrap gap-1.5">
          {sheets.map((s, i) => (
            <button
              key={s.name + i}
              type="button"
              onClick={() => setActive(i)}
              className={`rounded-lg px-3 py-1 text-xs font-semibold transition ${
                i === active
                  ? "bg-caliber-purple text-white"
                  : "bg-slate-100 text-slate-600 hover:bg-slate-200"
              }`}
            >
              {s.name}
            </button>
          ))}
        </div>
      )}
      <div className="max-h-[58vh] overflow-auto rounded-2xl border border-slate-200/70">
        <table className="w-full border-collapse text-sm">
          {header && (
            <thead className="sticky top-0">
              <tr>
                {header.map((cell, ci) => (
                  <th
                    key={ci}
                    className="border-b border-slate-200 bg-slate-50 px-3 py-2 text-left font-semibold text-slate-700 dark:border-slate-700 dark:bg-slate-800"
                  >
                    {cell}
                  </th>
                ))}
              </tr>
            </thead>
          )}
          <tbody>
            {body.map((r, ri) => (
              <tr key={ri} className="odd:bg-white even:bg-slate-50/50">
                {(header ?? r).map((_, ci) => (
                  <td
                    key={ci}
                    className="border-b border-slate-100 px-3 py-1.5 text-slate-700 dark:border-slate-800 dark:text-slate-200"
                  >
                    {r[ci] ?? ""}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {truncated && (
        <p className="text-xs text-slate-400">
          Showing a truncated view of large content — download the file for the
          full data.
        </p>
      )}
    </div>
  );
}
