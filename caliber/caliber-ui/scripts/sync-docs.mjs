/**
 * Sync the documentation site into the SPA's public/ dir so the sidebar "Docs"
 * link serves an up-to-date copy at /caliber/docs/.
 *
 * The site is a multi-page set: the hand-authored landing page (index.html), the
 * shared design system (docs.css + docs.js), the generated sidebar nav data
 * (docs-nav.js), and one generated page per published module (m-*.html). The
 * The layered overview is produced from root ARCHITECTURE.md; the architecture,
 * workflow-reference, and strategy modules come from docs/**.md. The shared
 * docs-site/build-docs.mjs builder emits those pages plus docs-nav.js, and this
 * hook runs it first so a markdown edit flows
 * through to the served docs on the next build.
 *
 * The source of truth is the suite-level docs-site/. We rewrite image references
 * in the HTML to reuse the SPA's public-root assets (../caliber.png,
 * ../caliber-icon.png) so no large binaries are duplicated.
 *
 * Runs as a `prebuild`/`predev` hook. When docs-site/ isn't in the build context
 * — e.g. the Docker UI stage only copies caliber-ui/ — it skips gracefully and
 * the committed public/docs/ files are used as-is.
 */

import {
  existsSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
  readdirSync,
  renameSync,
  rmSync,
} from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const DOCS_SITE = resolve(here, "../../../docs-site"); // caliber-suite/docs-site
const SERVED_DEST_DIR = resolve(here, "../public/docs"); // caliber-ui/public/docs
const PACKAGED_UI_DIR = resolve(here, "../../src/caliber/ui");
const PACKAGED_DEST_DIR = resolve(PACKAGED_UI_DIR, "docs"); // caliber/src/caliber/ui/docs
const STRICT = process.env.CALIBER_DOCS_STRICT === "1";

function writeTextAtomic(dest, contents) {
  const tmp = `${dest}.tmp-${process.pid}-${Date.now()}`;
  writeFileSync(tmp, contents, "utf8");
  try {
    renameSync(tmp, dest);
  } catch (err) {
    try {
      rmSync(tmp, { force: true });
    } catch {}
    throw err;
  }
}

if (!existsSync(DOCS_SITE) || !existsSync(resolve(DOCS_SITE, "index.html"))) {
  if (STRICT)
    throw new Error(
      `[sync-docs] required documentation site ${DOCS_SITE} not found`,
    );
  console.log(
    `[sync-docs] ${DOCS_SITE} not found — keeping committed docs copies.`,
  );
  process.exit(0);
}

// 1. Regenerate the module pages + nav from the markdown sources (best-effort).
const builder = resolve(DOCS_SITE, "build-docs.mjs");
if (existsSync(builder)) {
  try {
    await import(pathToFileURL(builder).href); // runs main() on import
  } catch (err) {
    if (STRICT) throw err;
    console.warn(
      `[sync-docs] build-docs.mjs failed (${err.message}) — copying existing docs-site files.`,
    );
  }
}

// 2. Decide which files make up the served docs site.
const SITE_FILES = readdirSync(DOCS_SITE).filter(
  (f) =>
    f === "index.html" ||
    f === "docs.css" ||
    f === "docs.js" ||
    f === "docs-nav.js" ||
    f === "llms.txt" ||
    f === "presentation.html" ||
    f === "presentation_timed.html" ||
    f === "walkthrough.html" ||
    /^m-.*\.html$/.test(f) ||
    /^m-.*\.md$/.test(f),
);

// HTML pages reference the shared UI-root images one level up.
function rewriteHtml(html) {
  return html
    .replaceAll('"caliber-icon.png"', '"../caliber-icon.png"')
    .replaceAll('"caliber.png"', '"../caliber.png"');
}

const DESTINATIONS = [
  { label: "public/docs", dir: SERVED_DEST_DIR },
  ...(existsSync(PACKAGED_UI_DIR)
    ? [{ label: "src/caliber/ui/docs", dir: PACKAGED_DEST_DIR }]
    : []),
];

let written = 0;
for (const target of DESTINATIONS) {
  mkdirSync(target.dir, { recursive: true });
  for (const file of SITE_FILES) {
    const src = readFileSync(resolve(DOCS_SITE, file), "utf8");
    const out = file.endsWith(".html") ? rewriteHtml(src) : src;
    if (!out.trim()) {
      if (STRICT)
        throw new Error(
          `[sync-docs] empty output for ${file} (${target.label})`,
        );
      console.warn(
        `[sync-docs] skipping empty output for ${file} (${target.label}) — keeping existing file.`,
      );
      continue;
    }
    if (file.endsWith(".html") && !out.includes("<!DOCTYPE html>")) {
      if (STRICT)
        throw new Error(
          `[sync-docs] invalid HTML output for ${file} (${target.label})`,
        );
      console.warn(
        `[sync-docs] skipping invalid HTML output for ${file} (${target.label}) — keeping existing file.`,
      );
      continue;
    }
    const dest = resolve(target.dir, file);
    const current = existsSync(dest) ? readFileSync(dest, "utf8") : null;
    if (current !== out) {
      writeTextAtomic(dest, out);
      written++;
    }
  }
}

if (written === 0) {
  console.log(
    `[sync-docs] docs copies already up to date (${DESTINATIONS.length} target(s), ${SITE_FILES.length} files each).`,
  );
} else {
  console.log(
    `[sync-docs] refreshed ${written} file write(s) across ${DESTINATIONS.length} target(s) (${SITE_FILES.length} files each).`,
  );
}
