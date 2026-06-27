/**
 * Tiny, dependency-free syntax tokenizer for JSON and Python.
 *
 * Used by the Workflow Studio code view to colorize the manifest (JSON) and the
 * compiled Agents-SDK module (Python). It is intentionally lossless: the
 * concatenation of every token's ``value`` equals the input exactly, so the
 * same tokens can back a read-only highlighted block *and* the mirror layer of
 * an editable highlighted textarea (where misalignment would be visible).
 */

export type Language = "json" | "python";

export interface CodeToken {
  value: string;
  /** key | string | number | keyword | boolean | comment | decorator | punct | plain */
  type: string;
}

interface Rule {
  type: string;
  /** Sticky (``y``) regex, matched only at the current scan position. */
  re: RegExp;
}

const JSON_RULES: Rule[] = [
  // A quoted string immediately followed by ':' is a property key.
  { type: "key", re: /"(?:\\.|[^"\\])*"(?=\s*:)/y },
  { type: "string", re: /"(?:\\.|[^"\\])*"/y },
  { type: "boolean", re: /\b(?:true|false|null)\b/y },
  { type: "number", re: /-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/y },
  { type: "punct", re: /[{}[\],:]/y },
];

const PY_KEYWORDS = [
  "False", "None", "True", "and", "as", "assert", "async", "await", "break",
  "case", "class", "continue", "def", "del", "elif", "else", "except", "finally",
  "for", "from", "global", "if", "import", "in", "is", "lambda", "match",
  "nonlocal", "not", "or", "pass", "raise", "return", "try", "while", "with", "yield",
];

const PY_RULES: Rule[] = [
  { type: "comment", re: /#[^\n]*/y },
  // Triple-quoted strings first (with optional r/b/f/u prefixes), then single-line.
  { type: "string", re: /(?:[rRbBfFuU]{0,2})(?:"""[\s\S]*?"""|'''[\s\S]*?''')/y },
  { type: "string", re: /(?:[rRbBfFuU]{0,2})(?:"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')/y },
  { type: "decorator", re: /@[A-Za-z_][\w.]*/y },
  { type: "keyword", re: new RegExp(`\\b(?:${PY_KEYWORDS.join("|")})\\b`, "y") },
  { type: "number", re: /\b\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?[jJ]?\b/y },
];

function tokenize(code: string, rules: Rule[]): CodeToken[] {
  const tokens: CodeToken[] = [];
  let plain = "";
  const flush = (): void => {
    if (plain) {
      tokens.push({ value: plain, type: "plain" });
      plain = "";
    }
  };
  let i = 0;
  while (i < code.length) {
    let matched = false;
    for (const rule of rules) {
      rule.re.lastIndex = i;
      const m = rule.re.exec(code);
      if (m && m.index === i && m[0].length > 0) {
        flush();
        tokens.push({ value: m[0], type: rule.type });
        i += m[0].length;
        matched = true;
        break;
      }
    }
    if (!matched) {
      plain += code[i];
      i += 1;
    }
  }
  flush();
  return tokens;
}

/** Tokenize ``code`` for the given language. Lossless: tokens rejoin to the input. */
export function highlightTokens(code: string, language: Language): CodeToken[] {
  return tokenize(code, language === "json" ? JSON_RULES : PY_RULES);
}

/** Tailwind text class per token type (tuned for a light ``bg-zinc-50`` panel). */
export const TOKEN_CLASS: Record<string, string> = {
  key: "text-sky-700",
  string: "text-emerald-700",
  number: "text-amber-600",
  keyword: "text-violet-700",
  boolean: "text-violet-700",
  decorator: "text-amber-600",
  comment: "italic text-zinc-400",
  punct: "text-zinc-400",
  plain: "",
};
