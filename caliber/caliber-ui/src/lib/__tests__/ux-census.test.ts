import { describe, expect, it } from "vitest";

import {
  countLines,
  countMutatingCalls,
  diffCensus,
  duplicatedHelpers,
  findUnroutedPages,
  parseRoutes,
  passesCrumbs,
  toMarkdown,
  topLevelFunctionNames,
  usesPageHeader,
  // @ts-expect-error -- plain-JS CLI module, deliberately untyped
} from "../../../scripts/ux-census.mjs";

/**
 * The census is only useful if its numbers can be trusted, and the way a
 * source-scraping script goes wrong is quietly: a regex that stops matching
 * after a refactor reports "0 duplicated helpers" and reads as progress.
 *
 * So these tests exercise the parsing rules against fixtures rather than
 * against the repository. `collectCensus` is the only filesystem-touching
 * function and is deliberately not unit-tested here — its behaviour is the
 * composition of these, and asserting against live source would make the test
 * fail every time a page is added.
 */

describe("parseRoutes", () => {
  const APP = `
    <Route path="/login" element={<Login />} />
    <Route path="*" element={<Navigate to="/login" replace />} />
    <Route path="/" element={<Dashboard />} />
    <Route path="/login" element={<Navigate to="/" replace />} />
    <Route path="/tools" element={<ToolRegistry />} />
    <Route path="/tools/:toolId" element={<ToolDetail />} />
    <Route path="*" element={<NotFound />} />
  `;

  it("counts every registration, separating wildcards", () => {
    const r = parseRoutes(APP);
    expect(r.total).toBe(7);
    expect(r.wildcard).toBe(2);
    expect(r.addressable).toBe(5);
  });

  it("reports registrations and distinct paths separately", () => {
    // `/login` is registered twice — once unauthenticated, once as a redirect.
    // "How many routes" has two defensible answers, and reporting one silently
    // is how a census ends up disagreeing with the document it measures.
    const r = parseRoutes(APP);
    expect(r.addressable).toBe(5);
    expect(r.distinct).toBe(4);
  });

  it("identifies parameterized detail routes", () => {
    const r = parseRoutes(APP);
    expect(r.parameterized).toBe(1);
    expect(r.detailRoutes).toEqual(["/tools/:toolId"]);
  });

  it("de-duplicates the path list it returns", () => {
    expect(parseRoutes(APP).paths).toEqual(["/", "/login", "/tools", "/tools/:toolId"]);
  });

  it("returns zeroes rather than throwing on a file with no routes", () => {
    const r = parseRoutes("export const nothing = 1;");
    expect(r).toMatchObject({ total: 0, addressable: 0, distinct: 0, parameterized: 0 });
  });
});

describe("findUnroutedPages", () => {
  it("finds a page component App.tsx never mounts", () => {
    expect(
      findUnroutedPages('<Prompts />\n<ToolRegistry />', ["Prompts", "ToolRegistry", "ToolWizard"]),
    ).toEqual(["ToolWizard"]);
  });

  it("reports an aliased export as unrouted", () => {
    // Overview.tsx exports `Dashboard`, so it reads as unrouted even though it
    // is mounted. That mismatch is the finding, not a bug in the census — a
    // page whose file name does not match its export is harder to navigate to
    // in the codebase, which is the point.
    expect(findUnroutedPages("<Dashboard />", ["Overview"])).toEqual(["Overview"]);
  });

  it("is empty when everything is routed", () => {
    expect(findUnroutedPages("<Prompts />", ["Prompts"])).toEqual([]);
  });
});

describe("passesCrumbs / usesPageHeader", () => {
  it("counts a real crumbs prop, not the declaration or a mention", () => {
    expect(passesCrumbs("<PageHeader crumbs={[{ label: 'x' }]} />")).toBe(true);
    // The prop's own declaration in PageHeader.tsx must not count as adoption,
    // which is what made "breadcrumbs exist" and "breadcrumbs are used" look
    // like the same fact.
    expect(passesCrumbs("crumbs?: { label: string }[];")).toBe(false);
    expect(passesCrumbs("// TODO: pass crumbs here")).toBe(false);
  });

  it("detects PageHeader usage", () => {
    expect(usesPageHeader('import { PageHeader } from "@/components/PageHeader";')).toBe(true);
    expect(usesPageHeader("<div>no chrome</div>")).toBe(false);
  });
});

describe("topLevelFunctionNames / duplicatedHelpers", () => {
  const A = [
    "function readString(v) { return v; }",
    "function onlyInA() {}",
    "  function nestedInA() {}", // indented — not top level
  ].join("\n");
  const B = ["function readString(v) { return v; }", "function onlyInB() {}"].join("\n");

  it("collects only top-level declarations", () => {
    expect(topLevelFunctionNames(A)).toEqual(["readString", "onlyInA"]);
  });

  it("ignores arrow consts, which are not the duplication being measured", () => {
    expect(topLevelFunctionNames("const readString = (v) => v;")).toEqual([]);
  });

  it("reports a name declared in more than one file, with its files", () => {
    const dup = duplicatedHelpers({ "a.tsx": A, "b.tsx": B });
    expect(dup).toEqual([{ name: "readString", files: ["a.tsx", "b.tsx"] }]);
  });

  it("does not report a name declared twice in the same file", () => {
    // Same-file shadowing is a different (impossible) problem; counting it
    // would inflate the duplication figure.
    expect(duplicatedHelpers({ "a.tsx": "function x() {}\nfunction x() {}" })).toEqual([]);
  });

  it("is empty for disjoint files", () => {
    expect(duplicatedHelpers({ "a.tsx": "function p() {}", "b.tsx": "function q() {}" })).toEqual(
      [],
    );
  });
});

describe("countMutatingCalls", () => {
  it("counts writes and ignores reads", () => {
    const src = `
      caliberApi.createPrompt({});
      caliberApi.updateTool(id, {});
      caliberApi.promotePrompt(n, v, {});
      caliberApi.listPrompts();
      caliberApi.getMe(signal);
      caliberApi.getTool(id);
    `;
    expect(countMutatingCalls(src)).toBe(3);
  });

  it("does not treat a bare prefix as a mutation", () => {
    // A method named exactly `set` or `create` would be a read of something
    // else, and matching it would inflate the count. The prefix has to be
    // followed by an object name.
    expect(countMutatingCalls("caliberApi.set();")).toBe(0);
    expect(countMutatingCalls("caliberApi.setPromptAlias(a, b, c);")).toBe(1);
  });

  it("ignores a similarly-named call on another object", () => {
    expect(countMutatingCalls("otherClient.createThing();")).toBe(0);
  });
});

describe("countLines", () => {
  it("matches wc -l semantics", () => {
    // wc -l counts newlines. `split("\\n").length` would say 3 here, which is
    // why the census disagreed with the report by one line on every file
    // before this helper existed.
    expect(countLines("a\nb\n")).toBe(2);
    expect(countLines("a\nb")).toBe(1);
    expect(countLines("")).toBe(0);
  });
});

describe("toMarkdown", () => {
  const CENSUS = {
    routes: { total: 35, addressable: 33, distinct: 32, wildcard: 2, parameterized: 9 },
    unroutedPages: ["Overview"],
    pages: {
      total: 34,
      withPageHeader: 18,
      passingCrumbs: 1,
      totalLines: 56624,
      heaviest: [{ file: "KnowledgeBases.tsx", lines: 9021 }],
    },
    duplicatedHelpers: { count: 22, names: [] },
    mutatingCallSites: 132,
  };

  it("renders every headline measure", () => {
    const md = toMarkdown(CENSUS);
    expect(md).toContain("| Addressable routes (registrations) | 33 |");
    expect(md).toContain("| Addressable routes (distinct paths) | 32 |");
    expect(md).toContain("| — using PageHeader | 18 of 34 |");
    expect(md).toContain("| — passing real crumbs | 1 of 34 |");
    expect(md).toContain("| Duplicated workflow helpers | 22 |");
    expect(md).toContain("9,021 lines");
  });

  it("names the unrouted pages rather than only counting them", () => {
    expect(toMarkdown(CENSUS)).toContain("1 (Overview)");
  });

  it("does not render a parenthetical when nothing is unrouted", () => {
    const md = toMarkdown({ ...CENSUS, unroutedPages: [] });
    expect(md).toContain("| Unrouted page components | 0 |");
  });
});

describe("diffCensus", () => {
  const base = {
    routes: { addressable: 33, distinct: 32, parameterized: 9 },
    pages: { total: 34, withPageHeader: 18, passingCrumbs: 1, totalLines: 56624 },
    duplicatedHelpers: { count: 22 },
    mutatingCallSites: 132,
  };

  it("reports only what moved, with a signed delta", () => {
    const after = structuredClone(base);
    after.pages.passingCrumbs = 10;
    after.duplicatedHelpers.count = 0;

    expect(diffCensus(base, after)).toEqual([
      { measure: "Pages passing crumbs", before: 1, after: 10, delta: 9 },
      { measure: "Duplicated helpers", before: 22, after: 0, delta: -22 },
    ]);
  });

  it("is empty when nothing changed", () => {
    expect(diffCensus(base, structuredClone(base))).toEqual([]);
  });
});
