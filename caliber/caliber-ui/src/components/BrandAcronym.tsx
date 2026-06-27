/**
 * The CALIBER backronym — "Contextual Adaptive Lifecycle for Intelligent Build,
 * Evaluation, and Refinement" — rendered with each acronym initial emphasized
 * (bold). Color and base font weight are inherited from the surrounding context
 * so it reads correctly on the dark login hero and the (light or dark) in-app
 * top bar alike.
 */

const PARTS: ReadonlyArray<readonly [initial: string, rest: string]> = [
  ["C", "ontextual"],
  ["A", "daptive"],
  ["L", "ifecycle for"],
  ["I", "ntelligent"],
  ["B", "uild,"],
  ["E", "valuation, and"],
  ["R", "efinement"],
];

/** The full backronym text, e.g. for `title`/`aria-label` attributes. */
export const BRAND_ACRONYM_TEXT =
  "Contextual Adaptive Lifecycle for Intelligent Build, Evaluation, and Refinement";

export function BrandAcronym({ className }: { className?: string }): JSX.Element {
  return (
    <span className={className}>
      {PARTS.map(([initial, rest], i) => (
        <span key={initial}>
          {i > 0 ? " " : ""}
          <strong className="font-bold">{initial}</strong>
          {rest}
        </span>
      ))}
    </span>
  );
}
