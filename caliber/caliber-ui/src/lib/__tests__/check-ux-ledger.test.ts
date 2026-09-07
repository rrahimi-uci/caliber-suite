import { describe, expect, it } from "vitest";

import {
  STATUS_IN_REVIEW,
  STATUS_LANDED,
  STATUS_OPEN,
  checkLedger,
  mergedPrNumbers,
  parseLedger,
  // @ts-expect-error -- plain-JS CLI module, deliberately untyped
} from "../../../scripts/check-ux-ledger.mjs";

/**
 * This checker exists because §15.2's merge-state claims went stale five times
 * in one review cycle — each correction of one sentence leaving an adjacent one
 * contradicting it. So the thing that most needs testing is that it *fails*
 * when it should: a checker that silently passes is worse than none, because
 * it converts an unnoticed problem into an unnoticed problem with a green tick.
 */

const LEDGER = `
### 15.2 Status ledger

| ID | Outcome | Status | Wave · gate | Blast radius | PR |
| --- | --- | --- | --- | --- | --- |
| **UX-00** | UX evidence harness | **In review** ¹ | 0 · G0 | M | \`#248\` |
| **UX-01** | Read-only versions | **Landed** | 1 · G1 | S | \`#239\` |
| **UX-03a** | Name the KB | **Landed** | 1 · G1 | XS | \`#245\` |
| **UX-08** | Reviewer evidence | Open | 2 · G2 | M | — |

Some prose after the table.
`;

describe("parseLedger", () => {
  it("reads id, status, and PR from each row", () => {
    expect(parseLedger(LEDGER)).toEqual([
      { id: "UX-00", status: STATUS_IN_REVIEW, prs: [248] },
      { id: "UX-01", status: STATUS_LANDED, prs: [239] },
      { id: "UX-03a", status: STATUS_LANDED, prs: [245] },
      { id: "UX-08", status: STATUS_OPEN, prs: [] },
    ]);
  });

  it("ignores the footnote marker and the bold markup", () => {
    // `**Landed** ¹` and `Landed` have to parse the same, or adding a footnote
    // would silently drop a row from the check.
    const [uxZero] = parseLedger(LEDGER);
    expect(uxZero.status).toBe(STATUS_IN_REVIEW);
  });

  it("handles a lettered residue id", () => {
    expect(parseLedger(LEDGER).map((r) => r.id)).toContain("UX-03a");
  });

  it("stops at the end of the table rather than eating the prose", () => {
    expect(parseLedger(LEDGER)).toHaveLength(4);
  });

  it("throws, rather than truncating, when the section is absent", () => {
    // `slice(-1)` returns the document's *last character*, not nothing. The
    // earlier version relied on that yielding an unparseable string — correct
    // by luck. A missing section is a structural failure and has to say so.
    expect(() => parseLedger("# Some other document\n\nNo ledger here.")).toThrow(
      /Status ledger.*not found/,
    );
  });

  it("throws when the table header has been renamed", () => {
    // The failure this replaces was the quiet one: `indexOf("| ID |")` → -1
    // made the end-of-table search start from 0 and stop at the first blank
    // line, so a present table parsed as zero rows and the whole check went
    // unenforced with a plausible-looking message.
    const renamed = [
      "### 15.2 Status ledger",
      "",
      "Some preamble paragraph.",
      "",
      "| Package | Outcome | Status | Wave | Size | PR |",
      "| --- | --- | --- | --- | --- | --- |",
      "| **UX-01** | A | **Landed** | 1 | S | `#239` |",
    ].join("\n");

    expect(() => parseLedger(renamed)).toThrow(/first column been renamed/);
  });

  it("reads the last row when the table ends at EOF with no blank line", () => {
    // `slice(0, -1)` drops the final character when no trailing blank line
    // exists, eating the closing pipe of the last row. It survived only
    // because the cell-count guard was loose enough to tolerate it.
    const atEof = [
      "### 15.2 Status ledger",
      "",
      "| ID | Outcome | Status | Wave | Size | PR |",
      "| --- | --- | --- | --- | --- | --- |",
      "| **UX-01** | A | **Landed** | 1 | S | `#239` |",
      "| **UX-02** | B | **In review** | 1 | S | `#247` |",
    ].join("\n");

    const rows = parseLedger(atEof);
    expect(rows.map((r: { id: string }) => r.id)).toEqual(["UX-01", "UX-02"]);
    expect(rows[1].prs).toEqual([247]);
  });
});

describe("mergedPrNumbers", () => {
  it("extracts the squash-merge PR suffix", () => {
    expect(
      mergedPrNumbers([
        "fix(dashboard): stop reading an empty install as a failure (#246)",
        "docs: correct the documented MLflow floor to >=3.15 (#240)",
      ]),
    ).toEqual(new Set([246, 240]));
  });

  it("ignores a bare issue reference in prose", () => {
    // Only the "(#NNN)" suffix means merged. A commit body mentioning #999
    // must not mark #999 as landed.
    expect(mergedPrNumbers(["fix: follow-up to #999"])).toEqual(new Set());
  });

  it("is empty for an empty log", () => {
    expect(mergedPrNumbers([])).toEqual(new Set());
  });
});

describe("checkLedger", () => {
  const rows = [
    { id: "UX-01", status: STATUS_LANDED, prs: [239] },
    { id: "UX-02", status: STATUS_IN_REVIEW, prs: [247] },
    { id: "UX-08", status: STATUS_OPEN, prs: [] },
  ];

  it("passes when every claim matches the log", () => {
    expect(checkLedger(rows, new Set([239]))).toEqual([]);
  });

  it("catches a row claiming Landed for an unmerged PR", () => {
    // The exact drift that reached review: an open PR listed as landed.
    const problems = checkLedger(
      [{ id: "UX-02", status: STATUS_LANDED, prs: [247] }],
      new Set([239]),
    );
    expect(problems).toEqual(["UX-02: marked Landed but #247 is not in the git log"]);
  });

  it("catches a row still claiming In review after its PR merged", () => {
    // The same drift in the other direction, which is the one that accumulates
    // silently: nobody notices a ledger under-claiming.
    const problems = checkLedger(
      [{ id: "UX-15", status: STATUS_IN_REVIEW, prs: [246] }],
      new Set([246]),
    );
    expect(problems).toEqual(["UX-15: marked In review but #246 has merged"]);
  });

  it("catches an Open row that cites a PR", () => {
    const problems = checkLedger([{ id: "UX-09", status: STATUS_OPEN, prs: [250] }], new Set());
    expect(problems).toEqual(["UX-09: marked Open but cites #250"]);
  });

  it("catches a Landed row citing no PR", () => {
    const problems = checkLedger([{ id: "UX-09", status: STATUS_LANDED, prs: [] }], new Set());
    expect(problems).toEqual(["UX-09: marked Landed but cites no PR"]);
  });

  it("catches an unrecognised status word", () => {
    // A typo, or a fifth state invented in passing, must not read as "fine".
    const problems = checkLedger([{ id: "UX-09", status: undefined, prs: [1] }], new Set());
    expect(problems).toEqual(["UX-09: Status cell does not start with a known state"]);
  });

  it("checks every PR on a row that cites more than one", () => {
    const problems = checkLedger(
      [{ id: "UX-01a", status: STATUS_LANDED, prs: [245, 999] }],
      new Set([245]),
    );
    expect(problems).toEqual(["UX-01a: marked Landed but #999 is not in the git log"]);
  });

  it("reports every problem, not just the first", () => {
    const problems = checkLedger(
      [
        { id: "UX-02", status: STATUS_LANDED, prs: [247] },
        { id: "UX-15", status: STATUS_IN_REVIEW, prs: [246] },
      ],
      new Set([246]),
    );
    expect(problems).toHaveLength(2);
  });

  it("does not detect omission, and that limit is deliberate", () => {
    // A package that shipped under a PR the ledger never mentions is
    // internally consistent: Open + no PR contradicts nothing. Nothing here
    // links a work-package id to a commit, because no enforced convention
    // carries one.
    //
    // This test exists so the limit is known rather than discovered: a green
    // result means "no row lies", not "the ledger is current". If it ever
    // needs to mean the latter, the id→PR link has to become something the
    // repository enforces, not something commit bodies happen to do.
    const stale = [{ id: "UX-05", status: STATUS_OPEN, prs: [] }];
    expect(checkLedger(stale, new Set([243]))).toEqual([]);
  });
});
