# CALIBER arXiv package

This directory is the self-contained arXiv source package for the comprehensive
CALIBER technical manuscript.

Important note: arXiv does not provide a branded conference-style class for
general CS preprints. Its official guidance is to upload a clean TeX/LaTeX source
package, include every figure and bibliography dependency, avoid extraneous files,
and ensure the submission compiles from the package root. This directory follows
that model.

## Build

```bash
cd paper/arxiv
make
```

Output:

- `build/caliber-arxiv.pdf`
- `build/caliber-arxiv-source.zip` via `make package`

## Package structure

- `main.tex` — arXiv root file
- `sections/`, `images/`, `tables/`, `appendix/` — manuscript content
- `generated/` — checked-in source-derived counts and diagram PDFs required to compile
- `refs.bib` — bibliography
- `article-layout.sty`, `preamble.tex`, `macros.tex`, `pseudocode.sty` — local style and macros bundled with the submission

## arXiv hygiene

This package intentionally excludes:

- build outputs committed from the main paper build,
- slide assets,
- node-based figure generators,
- repository reports unrelated to this manuscript,
- unused source artifacts.

That keeps the upload aligned with arXiv's source-package guidance.
