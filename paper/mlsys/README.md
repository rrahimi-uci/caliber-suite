# CALIBER MLSys Industry Track package

This directory contains the MLSys Industry Track adaptation of the CALIBER paper.

It uses the official MLSys 2026 Industry Track LaTeX style files and keeps the
manuscript in the required two-column format. The paper is written as a
submission-style version focused on the industrial problem, system architecture,
deployment model, engineering tradeoffs, operational interfaces, current evidence,
and lessons learned.

## Build

```bash
cd paper/mlsys
make
```

Outputs:

- `build/caliber-mlsys-industry.pdf`
- `build/caliber-mlsys-industry-source.zip` via `make package`

## What is included

- `main.tex` — anonymized submission-style manuscript
- `mlsys2026.sty`, `mlsys2026.bst`, and companion style files — official MLSys template assets
- `generated/stats.tex` — source-derived implementation counts
- `figures/*.pdf` — prebuilt vector figures used by the paper
- `refs.bib` — bibliography

The original downloaded MLSys template zip is retained in this directory as
provenance, but the actual submission package is the clean root-level source set
plus the packaged zip created by `make package`.
