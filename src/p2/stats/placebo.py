"""Placebo check: an A/A test on the experiment's own control users.

The control users are split at random into two groups that got exactly the same experience, so there is no real difference to
find. The experiment's own analysis (the same test, alpha and sidedness, or the same Bayesian rule and threshold) runs on the two
groups, many times. A healthy method finds a "difference" only as often as it promises: about alpha for the frequentist test.

Each try draws two groups of the control arm's size from the control users' own values (a bootstrap, so the real shape of the
metric, such as skewed revenue, is kept). A rate needs no user values: each group's count is a binomial draw.
"""
from dataclasses import dataclass

import numpy as np
from scipy.stats import binom

from p2.registry.registry import MetricDef
from p2.stats import bayes
from p2.stats.tests import Arm, two_group_test

REPS = 200
SEED = 20261003
RANGE = 0.99                 # the normal range holds the frequentist rate 99% of the time, so a healthy test rarely trips the check
BAYES_OK = 0.10              # the Bayesian rule bounds expected loss, not the false-call rate; up to 10% on identical groups is fine
MIN_USERS = 30
BAYES_DRAWS = 2000           # posterior draws per try (the table uses 100,000; this is only to count verdicts)


@dataclass(frozen=True)
class Placebo:
    method: str              # frequentist or bayesian
    reps: int                # random splits tried
    n: int                   # users in each of the two groups
    rate: float              # share of tries that found a difference (frequentist) or named a safer arm (Bayesian)
    expected: float | None   # frequentist: the alpha the test promises
    low: float | None        # frequentist: the normal range of the rate
    high: float | None
    ok: bool


def _group(metric: MetricDef, n: int, p: float, values: np.ndarray | None, rng: np.random.Generator) -> Arm:
    """One fake group of n users from the control users' distribution."""
    if metric.type == "binary":
        mean = rng.binomial(n, p) / n
        return Arm(n, float(mean), float(mean * (1 - mean)))
    sample = rng.choice(values, size=n)
    return Arm(n, float(sample.mean()), float(sample.var(ddof=1)))


def run_placebo(metric: MetricDef, control: Arm, values: np.ndarray | None, method: str, *, alpha: float = 0.05,
                sidedness: str = "two-sided", threshold_abs: float | None = None, min_days: int = bayes.DEFAULT_MIN_DAYS,
                reps: int = REPS, seed: int = SEED) -> Placebo:
    """Run the check. `control` is the control arm's summary; `values` the control users' metric values (needed for a continuous
    metric, ignored for a rate). Raises ValueError when there are too few users to split."""
    if metric.type == "binary":
        n, p, pool = control.n, min(max(control.mean, 0.0), 1.0), None
    else:
        if values is None or len(values) < MIN_USERS:
            raise ValueError("too few control users for a placebo check")
        n, p, pool = len(values), 0.0, np.asarray(values, dtype=float)
    if n < MIN_USERS:
        raise ValueError("too few control users for a placebo check")
    rng = np.random.default_rng(seed)
    hits = 0
    for _ in range(reps):
        a, b = _group(metric, n, p, pool, rng), _group(metric, n, p, pool, rng)
        if method == "bayesian":
            out = bayes.analyze_bayes(metric, a, b, draws=BAYES_DRAWS, seed=int(rng.integers(2**31)))
            verdict = bayes.primary_verdict(out, threshold_abs, days=min_days, min_days=min_days, ended=False)
            hits += verdict in (bayes.VERDICT_VARIANT, bayes.VERDICT_CONTROL)
        else:
            hits += two_group_test(metric, a, b, alpha, sidedness).verdict in ("Significant improvement", "Significant decline")
    rate = hits / reps
    if method == "bayesian":
        return Placebo(method, reps, n, rate, None, None, None, rate <= BAYES_OK)
    tail = (1 - RANGE) / 2
    low, high = binom.ppf(tail, reps, alpha) / reps, binom.ppf(1 - tail, reps, alpha) / reps
    return Placebo(method, reps, n, rate, alpha, float(low), float(high), bool(low <= rate <= high))
