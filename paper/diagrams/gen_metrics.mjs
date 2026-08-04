/**
 * Emit advance-width metrics for the diagram typeface as JSON, so the Python
 * scene builders can measure text exactly.
 *
 *     node gen_metrics.mjs > metrics.json
 *
 * Without this, a scene builder has to guess how wide a label is, and the only
 * place a wrong guess shows up is as text overflowing a border in the rendered
 * output -- which is precisely the class of defect that generating the diagrams
 * was supposed to eliminate. With it, `labelled_box` can size a box to its
 * content in both dimensions and fail the build when a label genuinely does not
 * fit.
 *
 * Widths are in em units (advance / unitsPerEm), so they scale to any font size.
 */

import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import opentype from "opentype.js";

const STYLES = {
  regular: "lmsans10-regular.otf",
  bold: "lmsans10-bold.otf",
  italic: "lmsans10-oblique.otf",
};

// Latin-1 is the whole permitted range: anything above U+00FF is rejected by the
// build, because librsvg would emit it as a Type 3 font.
const CODEPOINTS = [];
for (let c = 0x20; c <= 0xff; c++) CODEPOINTS.push(c);

const out = { unitsPerEm: null, styles: {} };

for (const [style, file] of Object.entries(STYLES)) {
  const path = execFileSync("kpsewhich", [file], { encoding: "utf8" }).trim();
  const font = opentype.parse(readFileSync(path).buffer);
  out.unitsPerEm = font.unitsPerEm;
  const widths = {};
  for (const c of CODEPOINTS) {
    const ch = String.fromCharCode(c);
    const glyph = font.charToGlyph(ch);
    // A missing glyph maps to .notdef; record it as null so Python can complain
    // about the character rather than silently measure a box.
    widths[c] = glyph && glyph.index !== 0 ? glyph.advanceWidth / font.unitsPerEm : null;
  }
  out.styles[style] = widths;
}

process.stdout.write(JSON.stringify(out) + "\n");
