/**
 * Syntax-highlighted code surfaces for the Workflow Studio code view.
 *
 * - {@link CodeBlock}: read-only highlighted code (the compiled Python).
 * - {@link CodeEditorField}: an *editable* highlighted field — a transparent
 *   textarea layered over a colored mirror, so editing the manifest JSON shows
 *   live highlighting while remaining a real `<textarea>` (caret, selection,
 *   value semantics) for callers and tests.
 *
 * Both share one typography scale so the editor's mirror aligns to the caret.
 */

import { useRef, type JSX } from "react";

import { highlightTokens, TOKEN_CLASS, type Language } from "@/lib/syntaxHighlight";

// Identical font metrics on the mirror <pre> and the <textarea> are what keep
// the highlighted text under the caret.
const CODE_TYPO = "font-mono text-[12px] leading-relaxed";

function renderTokens(code: string, language: Language): JSX.Element[] {
  return highlightTokens(code, language).map((token, index) => {
    const cls = TOKEN_CLASS[token.type] ?? "";
    return cls ? (
      <span key={index} className={cls}>
        {token.value}
      </span>
    ) : (
      <span key={index}>{token.value}</span>
    );
  });
}

interface CodeBlockProps {
  code: string;
  language: Language;
  /** Layout classes (overflow, padding, border, background, sizing). */
  className?: string;
  testId?: string;
}

export function CodeBlock({ code, language, className, testId }: CodeBlockProps): JSX.Element {
  return (
    <pre data-testid={testId} className={`${CODE_TYPO} ${className ?? ""}`}>
      <code>{renderTokens(code, language)}</code>
    </pre>
  );
}

interface CodeEditorFieldProps {
  value: string;
  onChange: (value: string) => void;
  language: Language;
  /** Layout classes for the container (border, background, sizing, focus ring). */
  className?: string;
  testId?: string;
  ariaLabel?: string;
}

export function CodeEditorField({
  value,
  onChange,
  language,
  className,
  testId,
  ariaLabel,
}: CodeEditorFieldProps): JSX.Element {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const mirrorRef = useRef<HTMLPreElement>(null);

  function syncScroll(): void {
    const ta = textareaRef.current;
    const mirror = mirrorRef.current;
    if (ta && mirror) {
      mirror.scrollTop = ta.scrollTop;
      mirror.scrollLeft = ta.scrollLeft;
    }
  }

  // Padding/whitespace must match between the mirror and the textarea so the
  // colored text sits exactly under the typed text.
  const layer = `absolute inset-0 m-0 ${CODE_TYPO} whitespace-pre-wrap break-words p-3`;

  return (
    <div className={`relative overflow-hidden ${className ?? ""}`}>
      <pre ref={mirrorRef} aria-hidden className={`pointer-events-none overflow-auto ${layer}`}>
        {/* Trailing newline keeps the last (possibly empty) line aligned. */}
        <code>
          {renderTokens(value, language)}
          {"\n"}
        </code>
      </pre>
      <textarea
        ref={textareaRef}
        data-testid={testId}
        aria-label={ariaLabel}
        spellCheck={false}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onScroll={syncScroll}
        className={`resize-none overflow-auto bg-transparent text-transparent caret-zinc-800 outline-none ${layer}`}
      />
    </div>
  );
}
