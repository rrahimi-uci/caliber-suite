# Reproducing the CALIBER manuscript

Last verified: 2026-08-03.

Requirements:

- Python 3.10–3.12 for the repository-supported statistics generator.
- `pdflatex` and `bibtex` from TeX Live.
- Optional: Node.js only when regenerating Excalidraw diagrams.

From the repository root:

```bash
make -C paper repro
```

This regenerates implementation counts, forces all four LaTeX passes and
BibTeX, and runs the document checks for undefined references, citations,
oversized floats, bad boxes, and abstract length. The PDF is written to
`paper/build/caliber-paper.pdf`.

The quantitative evaluation described in the paper has not been executed.
`make repro` verifies the manuscript and source-derived implementation counts;
it does not reproduce unreported empirical results.
