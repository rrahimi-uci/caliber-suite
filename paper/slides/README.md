# CALIBER seminar deck

`caliber-layered-control-plane.pptx` — a 25-slide technical seminar deck for
*CALIBER: A Layered Control Plane for Per-Family Governance of AI-Agent
Resources*. It is **generated**, not drawn, from two files in this directory:

| File | What it holds |
| --- | --- |
| `deck_kit.py` | The visual system: the 960 × 540 pt grid, the role palette, and the drawing primitives (card, table, badge, rule, ornament). |
| `generate_slides.py` | The deck itself — one function per slide, plus the counts it reads back out of the manuscript. |
| `preview.py` | Renders the finished `.pptx` to SVG/PNG proof sheets in `preview/`. |

## Generate

```bash
python3 -m venv .venv && .venv/bin/pip install python-pptx
.venv/bin/python paper/slides/generate_slides.py     # writes the .pptx
.venv/bin/python paper/slides/preview.py --png       # writes preview/slide-NN.png
```

`python-pptx` is the only dependency. `preview.py --png` additionally wants
`rsvg-convert` (`brew install librsvg`); without it the SVGs are still written.

## Why it is generated

Three properties are worth the generator, and all three are the same ones
`paper/README.md` claims for the Excalidraw figures:

1. **Numbers stay single-sourced.** `stats()` parses `\newcommand{\stat...}` out of
   `tex/macros.tex` and `generated/stats.tex` — so `nine families`, `487 route
   declarations` and `126k lines of Python` come from the same place the
   manuscript's prose does. Re-run `scripts/gen_stats.py` and the deck moves with
   the paper. A count the generator cannot find is a build failure, not a slide
   someone has to notice.

2. **Text that does not fit is a build failure.** Every text box is measured
   against its frame before it is written, and `Deck.save` *refuses to write the
   file* if anything overflows or runs below the footer line. This matters more
   than it sounds: PowerPoint silently spills text past a shape's bounds, so an
   unmeasured deck looks correct in the XML and broken on a projector. The first
   draft of this deck had 46 overflows and every one of them was caught here
   rather than in the room.

3. **Reviewable diffs.** A `.pptx` is a zip of XML with generated element IDs; a
   one-word change is an unreadable diff. The Python diffs by line.

## The visual system

16:9, authored on a 960 × 540 pt grid. Two grounds: **dark** (`#0E1E3A`) for the
title, the three section dividers, and the conclusion; **light** (white) for every
content slide. Content sits on a 859 pt measure between a 50 pt left margin and a
51 pt right margin, with the kicker at *y* = 30, the title at 51, and nothing but
the footer below 486.

Colour is assigned by **role**, never by decoration, so the key learned on slide 2
reads on slide 20:

| Role | Colour | Means |
| --- | --- | --- |
| governed / enforced | teal `#1F8A99` | the governed path; an unbypassable gate |
| advisory | amber `#B5821F` | filed as evidence; blocks nothing |
| limit / refusal / cost | warm red `#A93B2B` | an unmet obligation, a price paid, a facet a family does not implement |
| neutral | slate tints | structure and supporting copy |

That mapping is deliberately the paper's own chip vocabulary from `tab-families`,
which is why the guarantee-surface slide can be read without a legend lookup — the
legend under it restates the chips anyway, because an adopter reading a
screenshot has no table caption.

## Structure

Three movements, each opened by a dark divider:

| Slides | |
| --- | --- |
| 1–4 | Title, the nine-step remediation gap, why the obvious answers do not close it, and the positioning table |
| 5–9 | **I — The abstraction.** The governed asset, six lifecycle modes, the governance chain and its durable residue, the six-layer factoring |
| 10–17 | **II — Per-family governance.** The central claim, the nine guarantee surfaces, the third design not taken, the gate taxonomy, late binding, intent-first release, the nine loops |
| 18–25 | **III — Evidence and limits.** The unrun evaluation, what survives the comparison, the decisions and their prices, the limitations, the claims register, future work, conclusion |

Every slide carries speaker notes. They are not a transcript — each one says what
the slide is *for* and names the misreading it exists to prevent.

## What the deck states about its own standing

The manuscript's primary limitation is that its quantitative evaluation is
specified and has not been executed. Slide 19 leads with that rather than burying
it, slide 23 gives the full claims register with a `established` / `argued` /
`unmet` standing per row, and slide 25 restates it in the conclusion. A deck that
presented the architecture without that column would be making a stronger claim
than the paper does.

Two numbers on slide 19 are worth checking before presenting: the eight
deterministic structural checks are the ones `make evidence` records, and they are
*structural* evidence — not latency, throughput, replica-scale, or human-agreement
evidence. If `benchmarks/` gains or loses a check, update the chips.

## If you edit a slide

- Keep titles to roughly 63 characters. The title slot is one line at 26 pt; a
  longer title silently becomes two and the fit checker will fail the build.
- Do not hand-set a card height. Pass copy to `card_grid` and let it measure;
  pass `bottom=` if a row should reach toward a baseline. The stretch is capped,
  because a card at twice its content's height reads as unfinished, not airy.
- Use `\n` freely inside a run — `deck_kit` converts it to a real DrawingML line
  break. A raw newline left in a run is collapsed by PowerPoint, which is a bug
  that only shows up after the file is opened.
- `ruff` reports `ISC004` on the slide copy — implicit string concatenation inside
  a list literal. That is the pattern that keeps the copy readable, CI's lint job
  runs inside `caliber/` and does not reach this directory, and the finding is
  deliberate rather than pending.
- Re-run `preview.py --png` and look at the slide. The fit checker proves nothing
  overflows its own box; it cannot prove two boxes do not overlap, and the proof
  sheet is built from the shipped `.pptx` rather than from the generator's
  intentions.
