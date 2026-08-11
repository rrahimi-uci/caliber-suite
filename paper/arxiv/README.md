# CALIBER arXiv package

This directory is the self-contained arXiv source package for the comprehensive
CALIBER technical manuscript.

The package uses the PRIME AI / arXiv article template family as the root
presentation layer while remaining a clean self-contained source upload.

## Build

```bash
cd paper/arxiv
make
```

Output:

- `build/caliber-arxiv.pdf`
- `build/caliber-arxiv-source.zip` via `make package`

## Package structure

- `main.tex` — arXiv root file using the PRIME AI / arXiv article template
- `sections/`, `images/`, `tables/`, `appendix/` — manuscript content
- `generated/` — checked-in source-derived counts and diagram PDFs required to compile
- `refs.bib` — bibliography
- `PRIMEarxiv.sty` — PRIME AI / arXiv template style file
- `preamble.tex`, `macros.tex`, `pseudocode.sty` — local manuscript support files bundled with the submission

## arXiv hygiene

This package intentionally excludes:

- build outputs committed from the main paper build,
- slide assets,
- node-based figure generators,
- repository reports unrelated to this manuscript,
- unused source artifacts.

That keeps the upload aligned with arXiv's source-package guidance.
