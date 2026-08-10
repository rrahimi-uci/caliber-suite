LinkedIn Article Package — CALIBER

This package includes:
- LinkedIn_Article.md            : Markdown source (the authoritative text)
- LinkedIn_Article_CALIBER.docx  : polished article with embedded visuals
- LinkedIn_Article_CALIBER.pdf   : print/share version of the same article
- images/                        : hero, infographic and support visuals (1600x900 at 2x)
- make_images.py                 : regenerates every image in images/ from source
- head.tex                       : LaTeX preamble used by the PDF build below

The article is based on the paper *CALIBER: A Layered Control Plane for Per-Family
Governance of AI-Agent Resources* (see ../paper) and is written for a broad LinkedIn
audience: engineers, platform leads, architects, and researchers working on
production agent systems.

Suggested use:
1. Open the DOCX or PDF for the most polished reading experience.
2. Paste or adapt the Markdown if you want direct article text.
3. Reuse images/ for LinkedIn post and cover assets.
   - hero_banner.png       1600x900   — article cover / post image
   - hero_infographic.png  900x2940   — the standalone one-pager
   - the remaining six are in-article figures, in the order the article uses them
   All are rendered at 2x (3200x1800; the one-pager 1800x5880), which is above
   LinkedIn's downscale threshold on every surface.

Image inventory:
  hero_banner.png        cover: title, subtitle, three architecture counts
  hero_infographic.png   the whole argument as a single scrollable one-pager
  comparison.png         Figure 1 — a prompt in a file vs. a governed asset
  governance_chain.png   Figure 2 — the seven chain terms and their durable residue
  governed_asset.png     Figure 3 — four universal facets, eight family-specific ones
  families_matrix.png    Figure 4 — the per-family guarantee surface
  gate_flow.png          Figure 5 — where the enforced gate sits
  evidence_standing.png  Figure 6 — what the paper establishes, and what it does not

Regenerating the images:
  python3 make_images.py images
Requires rsvg-convert (brew install librsvg). The script writes SVG and rasterises
it at 2x; no Python packages are needed.

The first half of make_images.py is a small drawing kit — one palette, one shadow,
one type scale, one 24px line-icon set, and a text fitter that wraps copy to a pixel
width rather than a character count. That shared kit is what makes the eight images
read as one system; edit it rather than a single figure if you want to restyle the
whole set. Colours: ink #0A1628, teal #14B8A6 (primary), amber #F59E0B (the human
decision and every caveat), violet / sky / emerald for the remaining categories,
rose #F43F5E reserved for refusal.

Rebuilding the DOCX and PDF from the Markdown:
  pandoc LinkedIn_Article.md -f markdown-implicit_figures \
    -o LinkedIn_Article_CALIBER.docx
  pandoc LinkedIn_Article.md -f markdown-implicit_figures \
    -o LinkedIn_Article_CALIBER.pdf --pdf-engine=xelatex -H head.tex \
    -V geometry:margin=2.0cm -V fontsize=11pt -V mainfont="Helvetica Neue" \
    -V monofont="Menlo" -V colorlinks=true -V linkcolor=teal

A note on the numbers: the paper's quantitative evaluation is specified but has not
been run, so this package contains no performance claims. The counts that do appear
(nine asset families, six layers, six lifecycle modes, seven chain terms) are
architectural constants taken from paper/tex/macros.tex.
