# Excalidraw diagram sources

The paper's architecture figures are authored here as **code that emits genuine
Excalidraw scenes**, rather than drawn by hand in the browser. The build is:

```
diagrams/*.py                 scene described in Python
      |  build.py
      v
generated/diagrams/*.excalidraw    a real Excalidraw file -- open it at
      |                            excalidraw.com, edit it, re-export
      |  render.mjs  (Node + roughjs, the library Excalidraw itself draws with)
      v
generated/diagrams/*.svg
      |  rsvg-convert
      v
generated/diagrams/*.pdf      \includegraphics in the paper
```

Run it with `make diagrams` from `paper/`.

## Why code and not the browser

Drawing these by hand in Excalidraw would have cost four things the paper relies
on. Generating the scenes keeps all four:

1. **Numbers stay single-sourced.** `454 routes`, `71 tables`, `9 families` come
   from `generated/stats.tex`, which `gen_stats.py` derives from the CALIBER tree.
   A scene generated in Python reads the same values. Hand-drawn text in a JSON
   blob would have to be retyped every time the code changes.
2. **One palette, enforced.** `palette.py` mirrors `tex/preamble.tex`, so an
   Excalidraw box and a TikZ box for the same role are the same colour by
   construction rather than by eye.
3. **Reviewable diffs.** A `.excalidraw` file is JSON with random element IDs; a
   one-word label change produces an unreadable diff. The Python scene diffs by
   line, like the TikZ it replaces.
4. **Reproducible builds.** Every rough stroke is seeded from its element index, so
   the SVG is byte-stable across machines and runs. No browser round-trip sits
   between the source and the PDF.

The `.excalidraw` files are still real, editable Excalidraw documents --- that is
the point of emitting them. Open one, move things around, and either re-export by
hand or fold the change back into the Python. The generator is the source of truth;
the scene file is a first-class build artifact.

## Style, and why it is set the way it is

The paper targets a systems conference, where Excalidraw's default hand-drawn look
reads as informal and works against an argument about precision. So the scenes are
authored at Excalidraw's **architect** sloppiness --- its crispest setting --- with
solid fills and the *Normal* (not hand-drawn) font. The result is recognisably
Excalidraw in feel while staying appropriate for review. `STYLE` in `scene.py`
holds those knobs in one place; raise `roughness` toward 1.5 for a blog or a deck.

## Files

| File | Role |
| --- | --- |
| `palette.py` | The role-based colour system, mirroring `tex/preamble.tex`. |
| `scene.py` | The scene DSL, element builders, and the `.excalidraw` emitter. |
| `render.mjs` | Scene JSON to SVG, using roughjs with per-element seeds. |
| `build.py` | Builds every scene, then renders and converts each one. |
| `fig_*.py` | One figure per file, same names as the TikZ originals. |
| `package.json` | Pins roughjs. `make diagrams` installs it on first run. |
