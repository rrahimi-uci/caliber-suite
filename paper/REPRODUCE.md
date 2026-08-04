# Reproducing this paper

    make repro

That is the whole entrypoint. It is deliberately narrow, and it is worth being
precise about what it does and does not establish.

## What `make repro` proves

Every quantitative fact in the paper — line counts, family counts, layer counts,
node-type counts, the tree revision — is emitted by `scripts/gen_stats.py`, which
walks the CALIBER source tree and writes `generated/stats.tex`. Nothing is typed
into the prose by hand. `make repro` regenerates those files, **fails if the
regenerated output differs from the committed output**, and then rebuilds the PDF
under `make check`.

A pass therefore means: the numbers in the text describe the tree you have, and no
figure was transcribed. A failure means the committed `generated/` files describe a
different revision than your checkout — which is exactly the drift the check exists
to catch.

`make check` additionally fails on undefined references, undefined citations,
floats taller than the page, and an abstract over 250 words.

`make repro` also executes `benchmarks/run_structural.py --verify`. That suite
replays eight deterministic ownership, operator-fencing, release-fault, resolver-outage, and
publication-gating checks. `make evidence` writes the environment and outcomes to
`benchmarks/results/structural.json`; ordinary reproduction verifies the checks
without rewriting the recorded timestamp. The manifest records whether the worktree
was dirty. If it was, its base Git revision does not identify the uncommitted patch;
the dirty flag discloses that limit rather than making the run independently exact.

## What `make repro` does not prove

**The performance, scale, baseline, and human-study protocol has not been
executed.** Table 5 contains no quantitative numbers; every such cell is marked
*not measured*. The structural manifest explicitly excludes production latency,
throughput, replica-scale stress, and human agreement. Appendix C specifies those
protocols, but no pinned experiment environment or result set is published yet.

Providing a pinned environment and a results manifest is the first thing to add once
Table 5 has numbers in it.

## Toolchain

Last run against:

| Component | Version |
|---|---|
| pdfTeX  | TeX Live 2024 (`pdflatex --version`) |
| BibTeX  | 0.99d |
| Python  | 3.10–3.12 for CALIBER checks; 3.11+ for paper-only scripts |

No `latexmk`, no Node, and no network access are required for `make repro`. The
repository's `caliber/.venv` must contain the development test dependencies. Four
pdflatex passes plus one bibtex pass settle floats, citations, and cross-references.

`make diagrams` is separate and *is* optional: it rebuilds the Excalidraw figures
and needs Node. The committed PDFs in `generated/diagrams/` are what an ordinary
build consumes, so the paper stays buildable with nothing but pdflatex.
