SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

BUILDDIR := build
JOBNAME := caliber-arxiv
PDF := $(BUILDDIR)/$(JOBNAME).pdf

PDFLATEX := pdflatex
BIBTEX := bibtex

LATEXOPTS := -interaction=nonstopmode -halt-on-error -file-line-error \
             -output-directory=$(BUILDDIR) -jobname=$(JOBNAME)

SOURCES := main.tex article-layout.sty preamble.tex macros.tex pseudocode.sty \
           $(wildcard sections/*.tex) $(wildcard images/*.tex) \
           $(wildcard tables/*.tex) $(wildcard appendix/*.tex) \
           $(wildcard generated/*.tex) $(wildcard generated/diagrams/*.pdf) \
           refs.bib

.PHONY: all clean check package

all: $(PDF)

$(PDF): $(SOURCES) | $(BUILDDIR)
	@$(PDFLATEX) $(LATEXOPTS) main.tex >/dev/null
	@cp refs.bib $(BUILDDIR)/refs.bib
	@cd $(BUILDDIR) && $(BIBTEX) $(JOBNAME) >/dev/null
	@$(PDFLATEX) $(LATEXOPTS) main.tex >/dev/null
	@$(PDFLATEX) $(LATEXOPTS) main.tex >/dev/null
	@$(PDFLATEX) $(LATEXOPTS) main.tex >/dev/null
	@$(MAKE) --no-print-directory check

$(BUILDDIR):
	@mkdir -p $(BUILDDIR)

check:
	@log=$(BUILDDIR)/$(JOBNAME).log; \
	if grep -q 'LaTeX Warning: There were undefined references' "$$log"; then \
	  echo "undefined references"; exit 1; fi; \
	if grep -q "Citation .* undefined" "$$log"; then \
	  echo "undefined citations"; exit 1; fi; \
	if command -v pdffonts >/dev/null 2>&1; then \
	  t3=$$(pdffonts "$(PDF)" | tail -n +3 | awk '$$3 == "Type3"' | wc -l | tr -d ' '); \
	  if [ "$$t3" -gt 0 ]; then echo "Type 3 fonts present"; exit 1; fi; fi

package: all
	@pkg=$$(mktemp -d); \
	rsync -a --delete --exclude build --exclude '*.aux' --exclude '*.log' --exclude '*.out' \
	  --exclude '*.blg' --exclude '*.bbl' --exclude '*.toc' ./ "$$pkg/"; \
	(cd "$$pkg" && zip -qr caliber-arxiv-source.zip .); \
	cp "$$pkg/caliber-arxiv-source.zip" $(BUILDDIR)/

clean:
	@rm -rf $(BUILDDIR)
