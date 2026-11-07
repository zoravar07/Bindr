"""Deterministic statistics for the backtests. No model writes a number here.

Hand-rolled so the eval package has no scipy dependency. Every function is
pure and exact apart from the normal approximation in `mann_whitney`, which
is labelled as such in its return value.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence


def mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def median(xs: Sequence[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def stdev(xs: Sequence[float]) -> float | None:
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def average_ranks(values: Sequence[float]) -> list[float]:
    """Ranks starting at 1, ties sharing their average rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


@dataclass(frozen=True)
class MannWhitney:
    u_statistic: float
    auroc: float
    p_value: float
    n_positive: int
    n_negative: int
    method: str = "two-sided normal approximation with tie correction"


def mann_whitney(positive: Sequence[float], negative: Sequence[float]) -> MannWhitney | None:
    """Test whether `positive` scores higher than `negative`.

    AUROC = P(positive > negative) + 0.5 * P(tie), which equals U / (n1 * n2).
    """
    n1, n2 = len(positive), len(negative)
    if n1 == 0 or n2 == 0:
        return None
    combined = list(positive) + list(negative)
    ranks = average_ranks(combined)
    r1 = sum(ranks[:n1])
    u1 = r1 - n1 * (n1 + 1) / 2
    auc = u1 / (n1 * n2)

    mu = n1 * n2 / 2
    counts: dict[float, int] = {}
    for v in combined:
        counts[v] = counts.get(v, 0) + 1
    n = n1 + n2
    tie_term = sum(c ** 3 - c for c in counts.values())
    var = (n1 * n2 / 12) * ((n + 1) - tie_term / (n * (n - 1))) if n > 1 else 0.0
    if var <= 0:
        p = 1.0
    else:
        z = (abs(u1 - mu) - 0.5) / math.sqrt(var)  # continuity-corrected
        p = min(1.0, 2 * (1 - _normal_cdf(max(z, 0.0))))
    return MannWhitney(u_statistic=u1, auroc=auc, p_value=p, n_positive=n1, n_negative=n2)


def _normal_cdf(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def auroc(scores: Sequence[float], labels: Sequence[int]) -> float | None:
    """AUROC for label 1 = positive. Ties count half."""
    pos = [s for s, l in zip(scores, labels) if l == 1]
    neg = [s for s, l in zip(scores, labels) if l != 1]
    result = mann_whitney(pos, neg)
    return result.auroc if result else None


def _ranked_labels(scores: Sequence[float], labels: Sequence[int], tiebreak: Sequence | None = None) -> list[int]:
    """Labels ordered by score descending. Ties broken by `tiebreak` for determinism."""
    keys = tiebreak if tiebreak is not None else list(range(len(scores)))
    order = sorted(range(len(scores)), key=lambda i: (-scores[i], str(keys[i])))
    return [labels[i] for i in order]


def precision_at_k(scores: Sequence[float], labels: Sequence[int], k: int,
                   tiebreak: Sequence | None = None) -> float | None:
    if k <= 0 or not scores:
        return None
    ranked = _ranked_labels(scores, labels, tiebreak)[:k]
    return sum(1 for l in ranked if l == 1) / len(ranked)


def recall_at_k(scores: Sequence[float], labels: Sequence[int], k: int,
                tiebreak: Sequence | None = None) -> float | None:
    total = sum(1 for l in labels if l == 1)
    if total == 0 or k <= 0:
        return None
    ranked = _ranked_labels(scores, labels, tiebreak)[:k]
    return sum(1 for l in ranked if l == 1) / total


def expected_recall_random(n_candidates: int, k: int) -> float | None:
    """Expected recall when drawing k candidates uniformly at random.

    Each positive is drawn with probability k/N, so E[recall] = min(1, k/N),
    independent of how many positives there are.
    """
    if n_candidates <= 0 or k <= 0:
        return None
    return min(1.0, k / n_candidates)


def expected_recall_random_subset(n_subset: int, k: int, positives_in_subset: int,
                                  positives_total: int) -> float | None:
    """Expected recall when drawing k at random from a subset (e.g. the binding pocket).

    Positives outside the subset can never be found, so the ceiling is
    positives_in_subset / positives_total.
    """
    if positives_total <= 0 or n_subset <= 0 or k <= 0:
        return None
    hit_rate = min(1.0, k / n_subset)
    return (positives_in_subset / positives_total) * hit_rate


@dataclass
class GroupComparison:
    """Two-group separation summary. `positive` is the group expected to score higher."""

    positive_label: str
    negative_label: str
    positive_scores: list[float] = field(default_factory=list)
    negative_scores: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        mw = mann_whitney(self.positive_scores, self.negative_scores)
        pm, nm = mean(self.positive_scores), mean(self.negative_scores)
        return {
            "positive_label": self.positive_label,
            "negative_label": self.negative_label,
            "n_positive": len(self.positive_scores),
            "n_negative": len(self.negative_scores),
            "mean_positive": _r(pm),
            "mean_negative": _r(nm),
            "median_positive": _r(median(self.positive_scores)),
            "median_negative": _r(median(self.negative_scores)),
            "stdev_positive": _r(stdev(self.positive_scores)),
            "stdev_negative": _r(stdev(self.negative_scores)),
            "separation_of_means": _r(pm - nm) if pm is not None and nm is not None else None,
            "auroc": _r(mw.auroc) if mw else None,
            "mann_whitney_u": mw.u_statistic if mw else None,
            "mann_whitney_p": _r(mw.p_value, 5) if mw else None,
            "mann_whitney_method": mw.method if mw else None,
        }


def _r(x: float | None, places: int = 4) -> float | None:
    return None if x is None else round(x, places)


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)
