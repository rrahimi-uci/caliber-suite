"""Unit tests for judge human-alignment math (``caliber.eval.alignment``)."""

from __future__ import annotations

import pytest

from caliber.eval.alignment import cohen_kappa, confusion_counts, observed_agreement


def test_observed_agreement() -> None:
    assert observed_agreement([True, True, False], [True, False, False]) == pytest.approx(2 / 3)
    assert observed_agreement([], []) == 0.0


def test_cohen_kappa_perfect() -> None:
    labels = [True, False, True, False]
    assert cohen_kappa(labels, labels) == pytest.approx(1.0)


def test_cohen_kappa_chance_level_is_zero() -> None:
    # Judge and human are independent with balanced marginals: observed agreement
    # equals chance agreement → kappa ≈ 0.
    judge = [True, True, False, False]
    human = [True, False, True, False]
    assert cohen_kappa(judge, human) == pytest.approx(0.0, abs=1e-9)


def test_cohen_kappa_worse_than_chance_is_negative() -> None:
    judge = [True, True, False, False]
    human = [False, False, True, True]
    assert cohen_kappa(judge, human) < 0


def test_cohen_kappa_single_constant_label() -> None:
    # Both raters always say True → p_e == 1; full agreement → 1.0.
    assert cohen_kappa([True, True], [True, True]) == 1.0
    # One disagreement under a near-constant marginal → not perfect.
    assert cohen_kappa([True, True, False], [True, True, True]) < 1.0


def test_cohen_kappa_partial() -> None:
    # 9/10 agree, balanced-ish marginals → high but < 1 kappa.
    judge = [True] * 5 + [False] * 5
    human = [True] * 5 + [False] * 4 + [True]
    k = cohen_kappa(judge, human)
    assert 0.0 < k < 1.0


def test_confusion_counts() -> None:
    judge = [True, True, False, False]
    human = [True, False, False, True]
    assert confusion_counts(judge, human) == {
        "true_pos": 1,
        "false_pos": 1,
        "true_neg": 1,
        "false_neg": 1,
    }


def test_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        cohen_kappa([True], [True, False])
    with pytest.raises(ValueError):
        observed_agreement([True], [True, False])
