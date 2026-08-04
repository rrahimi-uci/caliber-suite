/**
 * Render a generated Excalidraw scene to SVG using roughjs -- the same stroke
 * library Excalidraw itself draws with, so the output is authentically Excalidraw
 * rather than an imitation of it.
 *
 * Usage:  node render.mjs scene.json out.svg
 *
 * Two properties matter here and are worth stating because they are why this is a
 * script rather than a browser round-trip:
 *
 *   * Determinism. Every element carries a seed derived from its index in the
 *     scene, so the SVG is byte-identical across machines and runs. rough's own
 *     default is a random seed per call, which would make the PDF change on every
 *     build and make review impossible.
 *
 *   * No DOM. rough's `generator()` API returns path data as strings and touches
 *     no canvas, so this runs headless with no jsdom and no browser.
 *
 * Text is emitted as filled outlines, not as SVG <text>. Three reasons, and the
 * first is not optional:
 *
 *   * No Type 3 fonts. librsvg's PDF backend emits a companion Type 3 font for
 *     every text run -- even for pure ASCII, so it is not a glyph-coverage problem
 *     that rewording can fix. A Type 3 font is a routine camera-ready rejection.
 *     Outlines mean the figure PDF contains no fonts at all, so the failure mode
 *     cannot occur.
 *   * Byte-identical output everywhere. SVG <text> resolves against whatever font
 *     the converting machine happens to have installed. Outlines do not.
 *   * Exact centring. opentype.js gives real advance widths, so a label sits where
 *     the generator says it does rather than where a font substitution puts it.
 *
 * The typeface is Latin Modern Sans, which is the paper's own \sfdefault: the
 * figures and the body text therefore share a family rather than merely coexisting.
 * It ships with TeX Live, so it is present wherever the paper can be built.
 */

import { readFileSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import rough from "roughjs";
import opentype from "opentype.js";

const [, , scenePath, outPath] = process.argv;
if (!scenePath || !outPath) {
  console.error("usage: node render.mjs <scene.json> <out.svg>");
  process.exit(2);
}

const scene = JSON.parse(readFileSync(scenePath, "utf8"));
const gen = rough.generator();
const PAD = 8; // breathing room so a rough stroke never clips at the edge

// ------------------------------------------------------------------- fonts ---
/** Locate a TeX Live font by name, so this is not tied to one install path. */
function findFont(basename) {
  try {
    const p = execFileSync("kpsewhich", [basename], { encoding: "utf8" }).trim();
    if (p) return p;
  } catch {
    /* fall through to the error below */
  }
  throw new Error(
    `cannot locate ${basename}. It ships with TeX Live; check that kpsewhich is ` +
      `on PATH.`,
  );
}

const FONTS = {
  regular: opentype.parse(readFileSync(findFont("lmsans10-regular.otf")).buffer),
  bold: opentype.parse(readFileSync(findFont("lmsans10-bold.otf")).buffer),
  italic: opentype.parse(readFileSync(findFont("lmsans10-oblique.otf")).buffer),
};

function pickFont(el) {
  if (el._bold) return FONTS.bold;
  if (el._italic) return FONTS.italic;
  return FONTS.regular;
}

/**
 * Serialise an opentype.js Path to SVG path data.
 *
 * Deliberately not `path.toPathData()`: that helper rounds via string
 * concatenation (`float + "e+" + places`), so any coordinate JavaScript
 * stringifies in exponential form -- which happens for values very close to zero,
 * and therefore depends on where the label was positioned -- concatenates to
 * "1.2e-7e+2" and parses as NaN. A NaN in path data makes renderers abandon the
 * rest of the subpath, which shows up as a label whose last few glyphs collapse
 * into a blob. `toFixed` cannot produce that.
 */
function pathToD(path, dp = 2) {
  const n = (v) => {
    if (!Number.isFinite(v)) {
      throw new Error(`non-finite glyph coordinate: ${v}`);
    }
    let out = v.toFixed(dp);
    if (out.includes(".")) out = out.replace(/0+$/, "").replace(/\.$/, "");
    return out === "-0" ? "0" : out;
  };
  const parts = [];
  for (const c of path.commands) {
    switch (c.type) {
      case "M":
        parts.push(`M${n(c.x)} ${n(c.y)}`);
        break;
      case "L":
        parts.push(`L${n(c.x)} ${n(c.y)}`);
        break;
      case "C":
        parts.push(
          `C${n(c.x1)} ${n(c.y1)} ${n(c.x2)} ${n(c.y2)} ${n(c.x)} ${n(c.y)}`,
        );
        break;
      case "Q":
        parts.push(`Q${n(c.x1)} ${n(c.y1)} ${n(c.x)} ${n(c.y)}`);
        break;
      case "Z":
        parts.push("Z");
        break;
      default:
        throw new Error(`unhandled glyph command ${c.type}`);
    }
  }
  return parts.join("");
}

/** Escape the five characters that are not legal as XML character data. */
const esc = (s) =>
  String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&apos;");

/** roughjs options shared by every shape, derived from the element. */
function roughOpts(el, { fill = false } = {}) {
  const o = {
    seed: el.seed || 1,
    roughness: el.roughness ?? 0.55,
    bowing: 0.55,
    stroke: el.strokeColor,
    strokeWidth: el.strokeWidth ?? 1.4,
    preserveVertices: true, // keeps corners where the author put them
    disableMultiStroke: false,
  };
  if (el.strokeStyle === "dashed") {
    o.strokeLineDash = [7, 4];
    o.disableMultiStroke = true; // a double-drawn dash reads as noise
  } else if (el.strokeStyle === "dotted") {
    o.strokeLineDash = [1.6, 3.2];
    o.disableMultiStroke = true;
  }
  if (fill && el.backgroundColor && el.backgroundColor !== "transparent") {
    o.fill = el.backgroundColor;
    o.fillStyle = "solid";
  }
  return o;
}

/** Turn a rough drawable into SVG <path> elements. */
function drawableToSvg(drawable) {
  const out = [];
  for (const set of drawable.sets) {
    const d = gen.opsToPath(set);
    if (!d) continue;
    if (set.type === "fillPath" || set.type === "fillSketch") {
      const fill = drawable.options.fill ?? "none";
      const isSketch = set.type === "fillSketch";
      out.push(
        `<path d="${d}" fill="${isSketch ? "none" : fill}" ` +
          `stroke="${isSketch ? fill : "none"}" ` +
          `stroke-width="${isSketch ? drawable.options.fillWeight ?? 1 : 0}"/>`,
      );
    } else {
      const o = drawable.options;
      const dash = o.strokeLineDash
        ? ` stroke-dasharray="${o.strokeLineDash.join(" ")}"`
        : "";
      out.push(
        `<path d="${d}" fill="none" stroke="${o.stroke}" ` +
          `stroke-width="${o.strokeWidth}" stroke-linecap="round" ` +
          `stroke-linejoin="round"${dash}/>`,
      );
    }
  }
  return out;
}

/**
 * A rounded rectangle. rough has no native rounded rect, so the shape is built as
 * a path with arc corners and handed to rough as a path -- which is also how
 * Excalidraw does it.
 */
function roundedRectPath(x, y, w, h, r) {
  const rr = Math.min(r, w / 2, h / 2);
  return [
    `M ${x + rr} ${y}`,
    `L ${x + w - rr} ${y}`,
    `Q ${x + w} ${y} ${x + w} ${y + rr}`,
    `L ${x + w} ${y + h - rr}`,
    `Q ${x + w} ${y + h} ${x + w - rr} ${y + h}`,
    `L ${x + rr} ${y + h}`,
    `Q ${x} ${y + h} ${x} ${y + h - rr}`,
    `L ${x} ${y + rr}`,
    `Q ${x} ${y} ${x + rr} ${y}`,
    "Z",
  ].join(" ");
}

/** Excalidraw's adaptive corner radius: proportional, capped. */
function adaptiveRadius(w, h) {
  const m = Math.min(w, h);
  return Math.min(m * 0.25, 16);
}

/** An arrowhead as two short strokes at the end of a segment. */
function arrowHead(px, py, qx, qy, el) {
  const angle = Math.atan2(qy - py, qx - px);
  const len = 9 + (el.strokeWidth ?? 1.4) * 1.6;
  const spread = 0.42;
  const parts = [];
  for (const s of [-1, 1]) {
    const a = angle + Math.PI + s * spread;
    const d = gen.line(qx, qy, qx + Math.cos(a) * len, qy + Math.sin(a) * len, {
      ...roughOpts(el),
      strokeLineDash: undefined, // an arrowhead is never dashed
      disableMultiStroke: true,
      roughness: (el.roughness ?? 0.55) * 0.5,
    });
    parts.push(...drawableToSvg(d));
  }
  return parts;
}

const body = [];

for (const el of scene.elements) {
  if (el.isDeleted) continue;

  switch (el.type) {
    case "rectangle": {
      const r = el.roundness ? adaptiveRadius(el.width, el.height) : 0;
      const opts = roughOpts(el, { fill: true });
      const d =
        r > 0
          ? gen.path(roundedRectPath(el.x, el.y, el.width, el.height, r), opts)
          : gen.rectangle(el.x, el.y, el.width, el.height, opts);
      body.push(...drawableToSvg(d));
      break;
    }

    case "ellipse": {
      body.push(
        ...drawableToSvg(
          gen.ellipse(
            el.x + el.width / 2,
            el.y + el.height / 2,
            el.width,
            el.height,
            roughOpts(el, { fill: true }),
          ),
        ),
      );
      break;
    }

    case "line":
    case "arrow": {
      const pts = el.points.map(([dx, dy]) => [el.x + dx, el.y + dy]);
      for (let i = 0; i < pts.length - 1; i++) {
        const [ax, ay] = pts[i];
        const [bx, by] = pts[i + 1];
        body.push(...drawableToSvg(gen.line(ax, ay, bx, by, roughOpts(el))));
      }
      if (el.endArrowhead === "arrow" && pts.length >= 2) {
        const [px, py] = pts[pts.length - 2];
        const [qx, qy] = pts[pts.length - 1];
        body.push(...arrowHead(px, py, qx, qy, el));
      }
      if (el.startArrowhead === "arrow" && pts.length >= 2) {
        const [px, py] = pts[1];
        const [qx, qy] = pts[0];
        body.push(...arrowHead(px, py, qx, qy, el));
      }
      break;
    }

    case "text": {
      const font = pickFont(el);
      const size = el.fontSize;
      const ax = el._anchorX ?? el.x;
      const lines = String(el.text).split("\n");
      const lh = (el.lineHeight ?? 1.25) * size;
      // Cap height rather than the full ascent: it centres visually in a box,
      // which is what the generator's label_in() assumes.
      const capTop = el.y + size * 0.78;
      const paths = [];
      lines.forEach((line, i) => {
        if (!line) return;
        const advance = font.getAdvanceWidth(line, size);
        let x = ax;
        if (el.textAlign === "center") x = ax - advance / 2;
        else if (el.textAlign === "right") x = ax - advance;
        const d = pathToD(font.getPath(line, x, capTop + i * lh, size));
        if (d) paths.push(d);
      });
      if (paths.length) {
        body.push(
          `<path d="${paths.join(" ")}" fill="${el.strokeColor}" stroke="none"/>`,
        );
      }
      break;
    }

    default:
      console.error(`render.mjs: skipping unsupported element type ${el.type}`);
  }
}

const w = scene.width + PAD * 2;
const h = scene.height + PAD * 2;
// The width and height carry explicit "pt" units. Without them a converter reads
// the numbers as CSS pixels (1/96in) and the figure lands at 72/96 of its intended
// size -- which silently shrinks the type below the floor the generator just
// checked. The viewBox stays unitless, so one user unit is one point.
const svg = [
  `<?xml version="1.0" encoding="UTF-8"?>`,
  `<svg xmlns="http://www.w3.org/2000/svg" width="${w}pt" height="${h}pt" ` +
    `viewBox="${-PAD} ${-PAD} ${w} ${h}">`,
  `<!-- Generated from paper/diagrams/${scene.name}.py. Do not edit. -->`,
  `<g stroke-linecap="round">`,
  ...body,
  `</g>`,
  `</svg>`,
].join("\n");

for (const token of ["NaN", "Infinity", "undefined", "null"]) {
  if (svg.includes(token)) {
    console.error(
      `render.mjs: refusing to write ${outPath} -- it contains "${token}". ` +
        `Renderers silently abandon a subpath at a malformed number, which shows ` +
        `up as truncated text rather than as an error.`,
    );
    process.exit(1);
  }
}

writeFileSync(outPath, svg + "\n");
console.log(`  rendered ${outPath} (${scene.elements.length} elements)`);
