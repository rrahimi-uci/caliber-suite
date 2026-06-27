"""Human-alignment metrics for judges: agreement rate + Cohen's kappa.

A judge is only trustworthy insofar as it agrees with human reviewers. This
module is the pure, dependency-free math behind the judge "alignment" surface:
given a judge's labels and the matching human labels over the same examples, it
reports the observed agreement rate and Cohen's kappa (agreement corrected for
chance). The route runs the judge to produce its labels; this module never calls
an LLM, so it's fully unit-testable.

Labels are compared as discrete categories (typically the booleans a unit score
thresholds into, but any hashable label works), so a judge that emits a
pass/fail verdict can be aligned against a human pass/fail annotation.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Any


def observed_agreement(judge_labels: Sequence[Any], human_labels: Sequence[Any]) -> float:
    """Fraction of examples where the two raters assign the same label."""
    if len(judge_labels) != len(human_labels):
        raise ValueError("judge_labels and human_labels must be the same length")
    n = len(judge_labels)
    if n == 0:
        return 0.0
    matches = sum(1 for a, b in zip(judge_labels, human_labels, strict=True) if a == b)
    return matches / n


def cohen_kappa(judge_labels: Sequence[Any], human_labels: Sequence[Any]) -> float:
    """Cohen's kappa between two raters over the same examples.

    ``κ = (p_o - p_e) / (1 - p_e)`` where ``p_o`` is the observed agreement and
    ``p_e`` is the agreement expected by chance from each rater's label marginals.
    Generalizes to any number of discrete categories. Edge cases:

    * empty input → ``0.0`` (nothing to compare).
    * ``p_e == 1`` (both raters always pick the same single label) → ``1.0`` if
      they fully agree, else ``0.0`` — kappa is otherwise undefined (0/0).

    Range is ``[-1, 1]``: 1 = perfect agreement, 0 = chance-level, negative =
    worse than chance.
    """
    if len(judge_labels) != len(human_labels):
        raise ValueError("judge_labels and human_labels must be the same length")
    n = len(judge_labels)
    if n == 0:
        return 0.0

    p_o = observed_agreement(judge_labels, human_labels)
    judge_counts = Counter(judge_labels)
    human_counts = Counter(human_labels)
    labels = set(judge_counts) | set(human_counts)
    p_e = sum((judge_counts[label] / n) * (human_counts[label] / n) for label in labels)

    if p_e >= 1.0:
        return 1.0 if p_o >= 1.0 else 0.0
    return (p_o - p_e) / (1.0 - p_e)


def confusion_counts(
    judge_labels: Sequence[Any], human_labels: Sequence[Any]
) -> dict[str, int]:
    """Binary confusion counts (treating ``True`` as the positive class).

    Useful when the aligned labels are booleans (the common pass/fail case): the
    UI shows where the judge and human diverge — false positives (judge says
    pass, human says fail) and false negatives.
    """
    tp = fp = tn = fn = 0
    for judge, human in zip(judge_labels, human_labels, strict=True):
        if judge and human:
            tp += 1
        elif judge and not human:
            fp += 1
        elif not judge and not human:
            tn += 1
        else:
            fn += 1
    return {"true_pos": tp, "false_pos": fp, "true_neg": tn, "false_neg": fn}
