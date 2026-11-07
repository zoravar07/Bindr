"""Unit tests for the deterministic statistics. No network."""

import pytest

from eval.stats import (
    GroupComparison,
    auroc,
    average_ranks,
    expected_recall_random,
    expected_recall_random_subset,
    jaccard,
    mann_whitney,
    median,
    precision_at_k,
    recall_at_k,
)


def test_average_ranks_handles_ties():
    assert average_ranks([10, 20, 20, 30]) == [1.0, 2.5, 2.5, 4.0]
    assert average_ranks([5, 5, 5]) == [2.0, 2.0, 2.0]


def test_perfect_and_inverted_separation():
    perfect = mann_whitney([0.9, 0.8, 0.7], [0.3, 0.2, 0.1])
    assert perfect.auroc == 1.0
    inverted = mann_whitney([0.1, 0.2, 0.3], [0.7, 0.8, 0.9])
    assert inverted.auroc == 0.0


def test_constant_predictor_scores_half():
    """The baseline-to-beat property: a constant cannot rank."""
    mw = mann_whitney([0.711] * 10, [0.711] * 10)
    assert mw.auroc == 0.5
    assert mw.p_value == pytest.approx(1.0)
    assert auroc([0.5] * 6, [1, 1, 1, 0, 0, 0]) == 0.5


def test_auroc_matches_hand_computed_case():
    # positives {3,1}, negatives {2,0}: pairs (3>2),(3>0),(1<2),(1>0) -> 3/4
    assert auroc([3, 1, 2, 0], [1, 1, 0, 0]) == 0.75


def test_mann_whitney_known_u_statistic():
    mw = mann_whitney([1, 2, 3, 4], [5, 6, 7, 8])
    assert mw.u_statistic == 0.0 and mw.auroc == 0.0
    mw = mann_whitney([5, 6, 7, 8], [1, 2, 3, 4])
    assert mw.u_statistic == 16.0 and mw.auroc == 1.0
    assert mw.p_value < 0.05


def test_precision_and_recall_at_k_are_deterministic_under_ties():
    scores = [0.5, 0.5, 0.5, 0.1]
    labels = [1, 0, 1, 0]
    ids = ["c", "a", "b", "d"]
    # ties broken by id: a(0), b(1), c(1) -> top 2 = a,b -> precision 0.5
    assert precision_at_k(scores, labels, 2, tiebreak=ids) == 0.5
    assert precision_at_k(scores, labels, 2, tiebreak=ids) == precision_at_k(scores, labels, 2, tiebreak=ids)
    assert recall_at_k([0.9, 0.8, 0.1], [1, 1, 0], 2) == 1.0
    assert recall_at_k([0.9, 0.8, 0.1], [0, 0, 0], 2) is None
    assert precision_at_k([], [], 5) is None


def test_random_baseline_expectations():
    assert expected_recall_random(300, 10) == pytest.approx(10 / 300)
    assert expected_recall_random(5, 10) == 1.0
    # 2 of 3 known escapes sit in the pocket; drawing 10 of 20 pocket residues
    assert expected_recall_random_subset(20, 10, 2, 3) == pytest.approx((2 / 3) * 0.5)
    assert expected_recall_random_subset(20, 10, 0, 3) == 0.0
    assert expected_recall_random_subset(0, 10, 1, 1) is None


def test_group_comparison_reports_separation():
    d = GroupComparison("failed", "approved", [0.8, 0.7, 0.9], [0.2, 0.3, 0.1]).to_dict()
    assert d["separation_of_means"] == pytest.approx(0.6)
    assert d["auroc"] == 1.0 and d["n_positive"] == 3
    assert d["median_positive"] == 0.8


def test_median_and_jaccard():
    assert median([3, 1, 2]) == 2
    assert median([4, 1, 2, 3]) == 2.5
    assert median([]) is None
    assert jaccard(["a", "b"], ["b", "c"]) == pytest.approx(1 / 3)
    assert jaccard([], []) == 0.0
