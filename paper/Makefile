# CALIBER technical paper -- build orchestration.
#
#   make          build the paper into build/caliber-paper.pdf
#   make stats    regenerate the implementation figures from the source tree
#   make diagrams rebuild the Excalidraw architecture figures (needs node)
#   make watch    rebuild on change (needs fswatch or inotifywait)
#   make check    lint the LaTeX log for the things reviewers notice
#   make clean    remove build artifacts
#   make distclean also remove generated/
#
# latexmk is not assumed: the toolchain here is plain pdflatex + bibtex, which
# is what a TeX Live "basic" install ships. Four passes settle the two-column
# float placement, the bibliography, and the cross-references.

SHELL      := /bin/bash
.SHELLFLAGS:= -eu -o pipefail -c

TEXDIR     := tex
BUILDDIR   := build
MAIN       := main
JOBNAME    := caliber-paper
PDF        := $(BUILDDIR)/$(JOBNAME).pdf

PDFLATEX   := pdflatex
BIBTEX     := bibtex
PYTHON     := python3

# -halt-on-error so a real LaTeX error fails the build instead of scrolling by.
LATEXOPTS  := -interaction=nonstopmode -halt-on-error -file-line-error \
              -output-directory=../$(BUILDDIR) -jobname=$(JOBNAME)

SOURCES    := $(wildcard $(TEXDIR)/*.tex) $(wildcard $(TEXDIR)/*.sty) \
              $(wildcard sections/*.tex) $(wildcard images/*.tex) \
              $(wildcard tables/*.tex) $(wildcard appendix/*.tex) \
              $(wildcard generated/*.tex) references/refs.bib \
              $(wildcard generated/diagrams/*.pdf)

.PHONY: all stats diagrams repro watch check clean distclean open help

all: $(PDF)

$(PDF): $(SOURCES) | $(BUILDDIR)
	@echo "==> pass 1/4 (structure)"
	@cd $(TEXDIR) && $(PDFLATEX) $(LATEXOPTS) $(MAIN).tex >/dev/null
	@echo "==> bibtex"
	@cd $(BUILDDIR) && $(BIBTEX) $(JOBNAME) >/dev/null || \
	    { echo "    bibtex reported problems:"; sed -n '1,40p' $(JOBNAME).blg; false; }
	@echo "==> pass 2/4 (citations)"
	@cd $(TEXDIR) && $(PDFLATEX) $(LATEXOPTS) $(MAIN).tex >/dev/null
	@echo "==> pass 3/4 (cross-references)"
	@cd $(TEXDIR) && $(PDFLATEX) $(LATEXOPTS) $(MAIN).tex >/dev/null
	@echo "==> pass 4/4 (float placement)"
	@cd $(TEXDIR) && $(PDFLATEX) $(LATEXOPTS) $(MAIN).tex >/dev/null
	@echo
	@$(MAKE) --no-print-directory check
	@pages=$$(grep -oE '\(([0-9]+) pages?,' $(BUILDDIR)/$(JOBNAME).log \
	          | tail -1 | grep -oE '[0-9]+' || echo '?'); \
	 printf '==> %s (%s pages)\n' "$(PDF)" "$$pages"

$(BUILDDIR):
	@mkdir -p $(BUILDDIR)

stats:
	@$(PYTHON) scripts/gen_stats.py

# The four architecture figures are generated Excalidraw scenes. This is a separate
# target rather than a prerequisite of the PDF on purpose: it needs Node, and the
# paper must stay buildable with nothing but pdflatex. The committed artifacts in
# generated/diagrams/ are what an ordinary `make` consumes; set \cbexcalidrawfalse
# in tex/macros.tex to fall back to the TikZ originals instead.
diagrams: stats
	@$(PYTHON) diagrams/build.py

repro: stats
	@$(MAKE) --no-print-directory -B all

# The log lint. These are exactly the classes of defect a program committee
# member sees before they read a word: broken references, missing citations,
# and text spilling out of the column.
check:
	@log=$(BUILDDIR)/$(JOBNAME).log; \
	if [ ! -f "$$log" ]; then echo "no log yet -- run make first"; exit 1; fi; \
	fail=0; \
	if grep -q 'LaTeX Warning: There were undefined references' "$$log"; then \
	  echo "FAIL  undefined references:"; \
	  grep -oE "Reference \`[^']+' on page [0-9]+ undefined" "$$log" | sort -u | sed 's/^/      /'; \
	  fail=1; fi; \
	if grep -q "Citation .* undefined" "$$log"; then \
	  echo "FAIL  undefined citations:"; \
	  grep -oE "Citation \`[^']+' on page [0-9]+ undefined" "$$log" | sort -u | sed 's/^/      /'; \
	  fail=1; fi; \
	if grep -q 'Label(s) may have changed' "$$log"; then \
	  echo "WARN  labels still settling -- run make again"; fi; \
	if grep -q 'Float too large' "$$log"; then \
	  echo "FAIL  float taller than the page (it will print over the footer):"; \
	  grep -oE 'Float too large for page by [0-9.]+pt' "$$log" | sort -u | sed 's/^/      /'; \
	  fail=1; fi; \
	ov=$$(grep -c 'Overfull \\hbox' "$$log" || true); \
	uv=$$(grep -c 'Underfull \\hbox' "$$log" || true); \
	bad=$$(grep -oE 'Overfull \\hbox \(([0-9.]+)pt' "$$log" \
	       | grep -oE '[0-9.]+' | awk '$$1 > 3 {n++} END {print n+0}'); \
	printf 'boxes  %s overfull (%s worse than 3pt), %s underfull\n' "$$ov" "$$bad" "$$uv"; \
	if [ "$$bad" -gt 0 ]; then \
	  echo "      worst offenders:"; \
	  grep -A1 'Overfull \\hbox' "$$log" | grep -E 'Overfull|^\.\.\.' \
	    | grep -oE 'Overfull \\hbox \([0-9.]+pt too wide\).*' | sort -t'(' -k2 -rn \
	    | head -5 | sed 's/^/      /'; fi; \
	aw=$$(python3 scripts/abstract_words.py 2>/dev/null || echo 0); \
	if [ "$$aw" -gt 250 ]; then \
	  echo "FAIL  abstract is $$aw words (limit 250)"; fail=1; \
	else echo "check  abstract $$aw words (limit 250)"; fi; \
	if [ "$$fail" -eq 0 ]; then echo "check  no undefined references or citations"; fi; \
	exit $$fail

watch:
	@command -v fswatch >/dev/null 2>&1 || { \
	  echo "install fswatch (brew install fswatch) or use: while :; do make; sleep 5; done"; \
	  exit 1; }
	@$(MAKE) --no-print-directory all || true
	@echo "==> watching for changes; Ctrl-C to stop"
	@fswatch -o $(TEXDIR) sections images tables appendix generated references \
	  | while read -r _; do $(MAKE) --no-print-directory all || true; done

open: all
	@open $(PDF) 2>/dev/null || xdg-open $(PDF) 2>/dev/null || echo "$(PDF)"

clean:
	@rm -rf $(BUILDDIR)
	@echo "removed $(BUILDDIR)/"

distclean: clean
	@rm -f generated/*.tex
	@echo "removed generated/*.tex (run 'make stats' to rebuild)"

help:
	@sed -n '1,20p' Makefile | sed 's/^# \{0,1\}//'
