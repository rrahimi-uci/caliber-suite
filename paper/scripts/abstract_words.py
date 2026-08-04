#!/usr/bin/env python3
"""Count the abstract's words, so the 250-word limit is enforced rather than
remembered. A macro counts as one word, which is the reading a submission form
would give it: \\statPyLoc{} renders as a number, not as nothing."""
import pathlib, re, sys

src = pathlib.Path(__file__).resolve().parent.parent / "sections" / "00-abstract.tex"
text = re.sub(r"(?m)^\s*%.*$", "", src.read_text())
try:
    body = text.split(r"\begin{abstract}")[1].split(r"\end{abstract}")[0]
except IndexError:
    sys.exit("abstract environment not found")
body = re.sub(r"\\[a-zA-Z]+\s*\{?", " WORD ", body)   # macro -> one word
body = re.sub(r"[{}\\~]|---|--", " ", body)
print(len([w for w in body.split() if re.search(r"[A-Za-z0-9]", w)]))
