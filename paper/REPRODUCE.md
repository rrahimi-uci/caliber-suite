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

## What `make repro` does not prove

**The measurement protocol has not been executed.** Table 5 contains no numbers;
every quantitative cell is marked *not measured*. Appendix C specifies the protocol
in enough detail for an independent party to run it, but there is no result set to
reproduce, and no pinned experiment environment is published yet. Section 10 states
this as the paper's primary limitation rather than a footnote.

Providing a pinned environment and a results manifest is the first thing to add once
Table 5 has numbers in it.

## Toolchain

Last run against:

| Component | Version |
|---|---|
| pdfTeX  | TeX Live 2024 (`pdflatex --version`) |
| BibTeX  | 0.99d |
| Python  | 3.11+ (only `scripts/gen_stats.py`; standard library only) |

No `latexmk`, no Node, and no network access are required for `make repro`. Four
pdflatex passes plus one bibtex pass settle floats, citations, and cross-references.

`make diagrams` is separate and *is* optional: it rebuilds the Excalidraw figures
and needs Node. The committed PDFs in `generated/diagrams/` are what an ordinary
build consumes, so the paper stays buildable with nothing but pdflatex.
