"""Small-sample statistics for evaluation reporting.

Standard library only. Every function here exists because the evaluation sets in
this project are small (6 to 26 meetings, tens to low hundreds of items) and the
proportions are frequently near 0 or 1. Normal-approximation intervals and the
bootstrap are known to behave badly in exactly that regime
(Bowyer, Aitchison & Ivanova, ICML 2025 Spotlight, arXiv:2503.01747), so exact
frequentist intervals are the primary method and the bootstrap is offered only
as a secondary check on paired means.
"""

from __future__ import annotations

import math
import random
from typing import Sequence

Z_95 = 1.959963984540054


def _log_binom_pmf(k: int, n: int, p: float) -> float:
    """log P(X = k) for X ~ Binomial(n, p), without overflow."""
    if p <= 0.0:
        return 0.0 if k == 0 else -math.inf
    if p >= 1.0:
        return 0.0 if k == n else -math.inf
    return (
        math.lgamma(n + 1)
        - math.lgamma(k + 1)
        - math.lgamma(n - k + 1)
        + k * math.log(p)
        + (n - k) * math.log1p(-p)
    )


def binom_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p)."""
    if n < 0:
        raise ValueError("n must be non-negative")
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    terms = [_log_binom_pmf(i, n, p) for i in range(k + 1)]
    peak = max(terms)
    return min(1.0, math.exp(peak) * sum(math.exp(t - peak) for t in terms))


def _bisect_p(predicate, low: float = 0.0, high: float = 1.0, iterations: int = 200) -> float:
    """Locate the boundary where a monotonically increasing predicate turns true.

    Callers must pass a predicate that is False for small p and True for large p.
    """
    for _ in range(iterations):
        mid = (low + high) / 2.0
        if predicate(mid):
            high = mid
        else:
            low = mid
    return (low + high) / 2.0


def wilson_interval(successes: int, total: int, z: float = Z_95) -> tuple[float, float] | None:
    """Wilson score interval. Closed form; preferred when n is moderate."""
    if total <= 0:
        return None
    if not 0 <= successes <= total:
        raise ValueError("successes must satisfy 0 <= successes <= total")
    p = successes / total
    denominator = 1.0 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = (z / denominator) * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return (max(0.0, centre - half), min(1.0, centre + half))


def clopper_pearson_interval(
    successes: int, total: int, alpha: float = 0.05
) -> tuple[float, float] | None:
    """Exact binomial interval. Primary method for the small-N proportions here.

    ``P(X <= k)`` is decreasing in ``p`` while ``P(X >= k)`` is increasing, so
    both boundaries are stated as increasing predicates:
    the lower bound solves ``P(X >= k) = alpha/2`` and the upper bound solves
    ``P(X <= k) = alpha/2``.
    """
    if total <= 0:
        return None
    if not 0 <= successes <= total:
        raise ValueError("successes must satisfy 0 <= successes <= total")
    lower = 0.0
    if successes > 0:
        lower = _bisect_p(lambda p: 1.0 - binom_cdf(successes - 1, total, p) > alpha / 2)
    upper = 1.0
    if successes < total:
        upper = _bisect_p(lambda p: binom_cdf(successes, total, p) < alpha / 2)
    return (lower, upper)


def proportion_report(successes: int, total: int) -> dict[str, object]:
    """Point estimate with both intervals, so a reader can see the disagreement."""
    if total <= 0:
        return {
            "successes": successes,
            "total": total,
            "rate": None,
            "wilson_95": None,
            "clopper_pearson_95": None,
            "note": "empty denominator; no estimate",
        }
    return {
        "successes": successes,
        "total": total,
        "rate": successes / total,
        "wilson_95": list(wilson_interval(successes, total) or ()),
        "clopper_pearson_95": list(clopper_pearson_interval(successes, total) or ()),
        "note": "exact interval is primary; Wilson shown for comparison",
    }


def holm_adjust(p_values: Sequence[float | None]) -> list[float | None]:
    """Holm-Bonferroni step-down adjusted p-values, slot-preserving.

    Added 2026-09-26 after the audit found the project reports families of
    3-12 comparisons (three arms x several endpoints) with no multiplicity
    control, while its two headline "positive" results sat at p=0.016-0.028.
    Holm is uniformly less conservative than Bonferroni and needs no
    independence assumption; ``None`` p-values (no-discordant-pair cases)
    pass through unchanged and are excluded from the family size.
    """
    indexed = [(i, p) for i, p in enumerate(p_values) if p is not None]
    adjusted: list[float | None] = [None] * len(p_values)
    m = len(indexed)
    if m == 0:
        return adjusted
    running_max = 0.0
    for order, (i, p) in enumerate(sorted(indexed, key=lambda ip: ip[1])):
        running_max = max(running_max, (m - order) * p)
        adjusted[i] = min(1.0, running_max)
    return adjusted


def family_report(comparisons: Sequence[tuple[str, float | None]]) -> dict[str, object]:
    """Named family of comparisons with raw and Holm-adjusted p-values.

    ``comparisons`` is a sequence of (label, p_value); a ``None`` p means the
    test was not evaluable (e.g. no discordant pairs) and is reported as such
    without inflating the family size.
    """
    labels = [label for label, _ in comparisons]
    adjusted = holm_adjust([p for _, p in comparisons])
    rows = [
        {"label": label, "p_raw": p, "p_holm": adj,
         "reject_at_0_05_holm": (adj is not None and adj < 0.05)}
        for label, (_, p), adj in zip(labels, comparisons, adjusted)
    ]
    return {
        "n_tests": sum(1 for _, p in comparisons if p is not None),
        "method": "Holm-Bonferroni step-down",
        "comparisons": rows,
        "note": "family size counts evaluable tests only; report this whenever "
                "more than one comparison is quoted from the same experiment",
    }


def mcnemar_exact(discordant_before_only: int, discordant_after_only: int) -> dict[str, object]:
    """Exact two-sided McNemar test on the discordant pair counts.

    ``discordant_before_only`` counts pairs correct before and wrong after
    (the harm direction); ``discordant_after_only`` counts wrong before and
    correct after (the fix direction). No continuity correction is applied
    because the exact conditional test does not need one.
    """
    for value in (discordant_before_only, discordant_after_only):
        if value < 0:
            raise ValueError("discordant counts must be non-negative")
    n = discordant_before_only + discordant_after_only
    if n == 0:
        return {
            "n_discordant": 0,
            "p_value_two_sided": None,
            "note": "no discordant pairs; the test has no power here",
        }
    k = min(discordant_before_only, discordant_after_only)
    tail = sum(math.comb(n, i) for i in range(k + 1)) * (0.5 ** n)
    return {
        "n_discordant": n,
        "p_value_two_sided": min(1.0, 2.0 * tail),
        "exact_method": "conditional binomial, p=0.5, two-sided",
    }


def paired_bootstrap_mean_ci(
    differences: Sequence[float], *, samples: int = 5000, seed: int = 20260925
) -> dict[str, object] | None:
    """Percentile bootstrap CI for the mean of paired differences.

    Secondary only. Under-covers when n is small and the mean sits near a
    boundary; report the exact intervals above as the primary evidence.
    """
    values = [float(v) for v in differences]
    if not values:
        return None
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(samples):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    # Nearest-rank on both tails (the earlier version was off by one slot on
    # one side only; numerical impact was negligible but the asymmetry was not).
    lo = means[max(0, math.ceil(0.025 * samples) - 1)]
    hi = means[min(samples - 1, math.ceil(0.975 * samples) - 1)]
    return {
        "n_pairs": n,
        "mean_difference": sum(values) / n,
        "percentile_95": [lo, hi],
        "bootstrap_samples": samples,
        "note": "percentile bootstrap on paired differences; secondary to exact intervals",
    }


def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = average
        i = j + 1
    return ranks


def spearman_rho(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Spearman rank correlation with average ranks for ties."""
    if len(x) != len(y):
        raise ValueError("x and y must have equal length")
    if len(x) < 3:
        return None
    rx, ry = _ranks(x), _ranks(y)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    numerator = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if dx == 0 or dy == 0:
        return None
    return numerator / (dx * dy)


def nearest_rank_percentile(values: Sequence[float], percentile: float) -> float | None:
    """Nearest-rank percentile: returns an actually observed value.

    The project already reports nearest-rank P95; this keeps that convention
    explicit rather than silently switching to interpolation.
    """
    if not values:
        return None
    if not 0 < percentile <= 100:
        raise ValueError("percentile must be in (0, 100]")
    ordered = sorted(float(v) for v in values)
    index = math.ceil(percentile / 100.0 * len(ordered)) - 1
    return ordered[max(0, min(len(ordered) - 1, index))]


def auc(positive_scores: Sequence[float], negative_scores: Sequence[float]) -> float | None:
    """Probability that a random positive scores above a random negative.

    Ties count as half. Returns None when either side is empty, because the
    quantity is then undefined rather than 0.5 -- the distinction matters when
    reporting that a proxy could not be evaluated.
    """
    positives = [float(v) for v in positive_scores]
    negatives = [float(v) for v in negative_scores]
    if not positives or not negatives:
        return None
    wins = 0.0
    for p in positives:
        for n in negatives:
            if p > n:
                wins += 1.0
            elif p == n:
                wins += 0.5
    return wins / (len(positives) * len(negatives))


def wilcoxon_signed_rank(
    differences: Sequence[float], *, exact_max_n: int = 25
) -> dict[str, object]:
    """Two-sided Wilcoxon signed-rank test on paired differences.

    Preferred over a t-test here because per-meeting unsupported counts are
    small, non-negative and heavily skewed. ``wilcox`` zero handling (drop
    before ranking) is the primary convention; since 2026-09-26 the ``pratt``
    convention (rank with zeros, then drop) is reported alongside whenever
    zeros exist, because the project's own GAP remediation doc showed the
    headline p-value is partly a product of the zero-handling convention.
    """
    values_all = [float(d) for d in differences]
    nonzero = [d for d in values_all if d != 0]
    zeros = len(values_all) - len(nonzero)
    base = _wilcoxon_core(nonzero, zeros, len(values_all), exact_max_n)
    result: dict[str, object] = {
        "n_pairs": len(values_all),
        "n_nonzero": len(nonzero),
        "n_zero": zeros,
        "w_plus": base["w_plus"],
        "w_minus": base["w_minus"],
        "statistic": base["statistic"],
        "p_value_two_sided": base["p_value"],
        "method": base["method"],
    }
    if not nonzero:
        result["note"] = "all paired differences are zero; the test has no power"
        result["statistic"] = None
        result["p_value_two_sided"] = None
    if zeros:
        result["pratt"] = _wilcoxon_pratt(values_all, exact_max_n)
        result["zero_convention_note"] = (
            "primary p uses the wilcox convention (zeros dropped before ranking); "
            "pratt ranks zeros together with nonzeros then drops them -- quote both"
        )
    return result


def _wilcoxon_core(
    nonzero: list[float], zeros: int, n_total: int, exact_max_n: int
) -> dict[str, object]:
    magnitudes = [abs(d) for d in nonzero]
    ranks = _ranks(magnitudes)
    w_plus = sum(rank for rank, d in zip(ranks, nonzero) if d > 0)
    w_minus = sum(rank for rank, d in zip(ranks, nonzero) if d < 0)
    statistic = min(w_plus, w_minus)
    n = len(nonzero)
    if n <= exact_max_n:
        from itertools import product

        counts: dict[float, int] = {}
        for signs in product((1, -1), repeat=n):
            value = sum(rank for rank, sign in zip(ranks, signs) if sign > 0)
            low = min(value, sum(ranks) - value)
            counts[low] = counts.get(low, 0) + 1
        hits = sum(count for value, count in counts.items() if value <= statistic)
        p_value = min(1.0, hits / (2 ** n))
        method = "exact enumeration of sign assignments"
    else:
        p_value, method = _wilcoxon_normal_approx(nonzero, ranks, statistic)
    return {
        "w_plus": w_plus,
        "w_minus": w_minus,
        "statistic": statistic,
        "p_value": p_value,
        "method": method,
    }


def _wilcoxon_normal_approx(
    nonzero: list[float], ranks: list[float], statistic: float
) -> tuple[float, str]:
    """Normal approximation with tie correction and continuity correction.

    The pre-2026-09-26 branch used the plain variance n(n+1)(2n+1)/24 with no
    continuity correction; on data dominated by ties it disagreed with scipy's
    tie-corrected approximation (observed p=0.339 vs 0.273 on an all-tie
    example), so both corrections are applied now.
    """
    n = len(nonzero)
    mean = n * (n + 1) / 4
    tie_term = 0.0
    ordered = sorted(ranks)
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1] == ordered[i]:
            j += 1
        t = j - i + 1
        tie_term += t ** 3 - t
        i = j + 1
    variance = n * (n + 1) * (2 * n + 1) / 24 - tie_term / 48
    if variance <= 0:
        return 1.0, "normal approximation degenerate (all |d| tied); exact branch required"
    z = (abs(statistic - mean) - 0.5) / math.sqrt(variance)
    p_value = min(1.0, math.erfc(abs(z) / math.sqrt(2)))
    return p_value, "normal approximation, tie-corrected variance, continuity-corrected"


def _wilcoxon_pratt(values_all: list[float], exact_max_n: int) -> dict[str, object]:
    """Pratt (1959) convention: zeros are ranked together with nonzeros, then dropped.

    The test statistic uses only the nonzero differences but keeps the ranks
    assigned in the full sample (so zeros displace the ranks of small
    nonzeros). The exact null enumerates sign assignments over the nonzero
    entries with those full-sample ranks.
    """
    magnitudes_all = [abs(d) for d in values_all]
    ranks_all = _ranks(magnitudes_all)
    pairs = [(rank, d) for rank, d in zip(ranks_all, values_all) if d != 0]
    ranks = [rank for rank, _ in pairs]
    signs = [1 if d > 0 else -1 for _, d in pairs]
    w_plus = sum(rank for rank, s in zip(ranks, signs) if s > 0)
    w_minus = sum(rank for rank, s in zip(ranks, signs) if s < 0)
    statistic = min(w_plus, w_minus)
    n = len(ranks)
    if n == 0:
        return {"p_value_two_sided": None, "note": "no nonzero differences"}
    if n <= exact_max_n:
        from itertools import product

        total = sum(ranks)
        hits = 0
        for combo in product((1, -1), repeat=n):
            value = sum(rank for rank, s in zip(ranks, combo) if s > 0)
            if min(value, total - value) <= statistic:
                hits += 1
        p_value = min(1.0, hits / (2 ** n))
        method = "exact enumeration (pratt ranking)"
    else:
        p_value, method = _wilcoxon_normal_approx([1.0] * n, ranks, statistic)
    return {
        "w_plus": w_plus,
        "w_minus": w_minus,
        "statistic": statistic,
        "p_value_two_sided": p_value,
        "method": method,
    }


def describe(values: Sequence[float]) -> dict[str, object]:
    """Distribution summary; percentiles are nearest-rank and N is reported."""
    if not values:
        return {"n": 0}
    ordered = sorted(float(v) for v in values)
    n = len(ordered)
    middle = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2
    return {
        "n": n,
        "mean": sum(ordered) / n,
        "median": middle,
        "min": ordered[0],
        "max": ordered[-1],
        "p95_nearest_rank": nearest_rank_percentile(ordered, 95),
        "percentile_method": "nearest-rank",
    }
