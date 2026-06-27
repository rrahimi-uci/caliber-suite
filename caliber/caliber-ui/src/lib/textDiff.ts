/**
 * Tiny, dependency-free line + word diff for the prompt version-compare view.
 *
 * No diff library ships in the SPA, so this is an in-house LCS diff (the same
 * dependency-free, lossless philosophy as syntaxHighlight.ts). It is lossless:
 * for any line, concatenating its word ``value``s reproduces the line exactly,
 * so the rendered diff text equals the underlying template character-for-char.
 *
 * ``diffLines(left, right)`` returns a flat unified-diff sequence: ``equal``
 * lines once, and a changed line rendered as a ``delete`` row (old line, with
 * removed words marked) immediately followed by an ``insert`` row (new line,
 * with added words marked). Pure additions/removals carry no word spans.
 */

export type DiffOp = "equal" | "insert" | "delete";

export interface WordPart {
  op: DiffOp;
  value: string;
}

export interface DiffLine {
  op: DiffOp;
  /** 0-based index in the left text, or null for an insert. */
  left: number | null;
  /** 0-based index in the right text, or null for a delete. */
  right: number | null;
  /** The full line text (lossless). */
  text: string;
  /** Word-level parts when this line is part of a change pair; else undefined. */
  words?: WordPart[];
}

interface SeqOp<T> {
  op: DiffOp;
  a?: T;
  b?: T;
  ai?: number;
  bi?: number;
}

/** Classic LCS diff over two arrays (O(n*m) — fine for prompt-sized text). */
function lcsDiff<T>(a: T[], b: T[]): SeqOp<T>[] {
  const n = a.length;
  const m = b.length;
  // dp[i][j] = length of the LCS of a[i:] and b[j:].
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      dp[i]![j] = a[i] === b[j] ? dp[i + 1]![j + 1]! + 1 : Math.max(dp[i + 1]![j]!, dp[i]![j + 1]!);
    }
  }
  const ops: SeqOp<T>[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      ops.push({ op: "equal", a: a[i], b: b[j], ai: i, bi: j });
      i += 1;
      j += 1;
    } else if (dp[i + 1]![j]! >= dp[i]![j + 1]!) {
      ops.push({ op: "delete", a: a[i], ai: i });
      i += 1;
    } else {
      ops.push({ op: "insert", b: b[j], bi: j });
      j += 1;
    }
  }
  while (i < n) {
    ops.push({ op: "delete", a: a[i], ai: i });
    i += 1;
  }
  while (j < m) {
    ops.push({ op: "insert", b: b[j], bi: j });
    j += 1;
  }
  return ops;
}

/** Tokenize preserving whitespace so the parts rejoin to the input exactly. */
function tokenizeWords(line: string): string[] {
  return line.match(/\S+|\s+/g) ?? [];
}

/** Word parts for one side of a changed line pair (lossless for that side). */
function wordParts(leftLine: string, rightLine: string, side: "delete" | "insert"): WordPart[] {
  const ops = lcsDiff(tokenizeWords(leftLine), tokenizeWords(rightLine));
  const parts: WordPart[] = [];
  for (const op of ops) {
    if (op.op === "equal") {
      parts.push({ op: "equal", value: op.a as string });
    } else if (op.op === "delete" && side === "delete") {
      parts.push({ op: "delete", value: op.a as string });
    } else if (op.op === "insert" && side === "insert") {
      parts.push({ op: "insert", value: op.b as string });
    }
  }
  return parts;
}

/** Unified line+word diff of two texts. */
export function diffLines(left: string, right: string): DiffLine[] {
  const a = left.split("\n");
  const b = right.split("\n");
  const ops = lcsDiff(a, b);
  const out: DiffLine[] = [];
  let k = 0;
  while (k < ops.length) {
    const op = ops[k]!;
    if (op.op === "equal") {
      out.push({ op: "equal", left: op.ai!, right: op.bi!, text: op.a as string });
      k += 1;
      continue;
    }
    // Gather a run of deletes then a run of inserts; pair them as changed lines.
    const dels: SeqOp<string>[] = [];
    while (k < ops.length && ops[k]!.op === "delete") {
      dels.push(ops[k]!);
      k += 1;
    }
    const ins: SeqOp<string>[] = [];
    while (k < ops.length && ops[k]!.op === "insert") {
      ins.push(ops[k]!);
      k += 1;
    }
    const pairs = Math.min(dels.length, ins.length);
    for (let p = 0; p < pairs; p += 1) {
      const l = dels[p]!.a as string;
      const r = ins[p]!.b as string;
      out.push({ op: "delete", left: dels[p]!.ai!, right: null, text: l, words: wordParts(l, r, "delete") });
      out.push({ op: "insert", left: null, right: ins[p]!.bi!, text: r, words: wordParts(l, r, "insert") });
    }
    for (let p = pairs; p < dels.length; p += 1) {
      out.push({ op: "delete", left: dels[p]!.ai!, right: null, text: dels[p]!.a as string });
    }
    for (let p = pairs; p < ins.length; p += 1) {
      out.push({ op: "insert", left: null, right: ins[p]!.bi!, text: ins[p]!.b as string });
    }
  }
  return out;
}

/** Added / removed line counts for a compare summary. */
export function diffStats(lines: DiffLine[]): { additions: number; deletions: number } {
  let additions = 0;
  let deletions = 0;
  for (const line of lines) {
    if (line.op === "insert") additions += 1;
    else if (line.op === "delete") deletions += 1;
  }
  return { additions, deletions };
}

/** Tailwind line-background class per op (tuned for a light bg-slate-50 panel). */
export const DIFF_LINE_CLASS: Record<DiffOp, string> = {
  equal: "",
  insert: "bg-emerald-50 text-emerald-800",
  delete: "bg-red-50 text-red-800",
};

/** Tailwind class for intra-line changed words (stronger than the line bg). */
export const DIFF_WORD_CLASS: Record<DiffOp, string> = {
  equal: "",
  insert: "rounded-sm bg-emerald-200 text-emerald-900",
  delete: "rounded-sm bg-red-200 text-red-900 line-through",
};
