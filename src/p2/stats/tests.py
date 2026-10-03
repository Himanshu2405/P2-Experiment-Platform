"""Frequentist tests from summary statistics (count, mean, variance per arm).

The warehouse computes the summaries with one scan, so this works for any experiment size. Binary metrics use a
two-proportion z-test; continuous metrics use Welch's t-test; guardrails use a one-sided non-inferiority test.
"""
from dataclasses import dataclass
from math import sqrt

from scipy.stats import norm, t

from p2.registry.registry import MetricDef
from p2.stats.plan import MetricPlan


@dataclass(frozen=True)
class Arm:
    n: int
    mean: float
    var: float  # sample variance; binary metrics use mean * (1 - mean) instead


@dataclass(frozen=True)
class Outcome:
    difference: float           # variant minus control
    relative_lift: float | None
    ci_low: float
    ci_high: float
    ci_level: float             # the confidence level the interval was built at
    statistic: float
    p_value: float              # for a guardrail: the non-inferiority p-value
    verdict: str
    achieved_power: float | None


def _check(c: Arm, v: Arm) -> None:
    if c.n < 2 or v.n < 2:
        raise ValueError("each arm needs at least 2 users")


def _var(m: MetricDef, a: Arm) -> float:
    return a.mean * (1 - a.mean) if m.type == "binary" else a.var


def _crit(m: MetricDef, c: Arm, v: Arm, prob: float) -> float:
    """Critical value at cumulative probability `prob`: normal for proportions, Welch t for continuous."""
    if m.type == "binary":
        return float(norm.ppf(prob))
    return float(t.ppf(prob, _welch_df(c, v)))


def _cdf(m: MetricDef, c: Arm, v: Arm, x: float) -> float:
    return float(norm.cdf(x)) if m.type == "binary" else float(t.cdf(x, _welch_df(c, v)))


def _welch_df(c: Arm, v: Arm) -> float:
    a, b = c.var / c.n, v.var / v.n
    if a + b == 0:
        return float(c.n + v.n - 2)
    return (a + b) ** 2 / (a**2 / (c.n - 1) + b**2 / (v.n - 1))


def _se_unpooled(m: MetricDef, c: Arm, v: Arm) -> float:
    return sqrt(_var(m, c) / c.n + _var(m, v) / v.n)


def two_group_test(m: MetricDef, c: Arm, v: Arm, alpha: float = 0.05, sidedness: str = "two-sided") -> Outcome:
    """Difference (variant minus control) with a confidence interval and p-value; no power information."""
    _check(c, v)
    diff = v.mean - c.mean
    if m.type == "binary":
        pooled = (c.n * c.mean + v.n * v.mean) / (c.n + v.n)
        se_test = sqrt(pooled * (1 - pooled) * (1 / c.n + 1 / v.n))
    else:
        se_test = _se_unpooled(m, c, v)
    se_ci = _se_unpooled(m, c, v)
    stat = diff / se_test if se_test > 0 else 0.0
    good = 1.0 if m.good_direction == "higher" else -1.0
    if sidedness == "two-sided":
        p = 2 * (1 - _cdf(m, c, v, abs(stat))) if se_test > 0 else (1.0 if diff == 0 else 0.0)
        ci_level = 1 - alpha
        half = _crit(m, c, v, 1 - alpha / 2) * se_ci
    else:  # one-sided, in the direction that counts as an improvement
        p = 1 - _cdf(m, c, v, good * stat) if se_test > 0 else (1.0 if diff == 0 else 0.0 if good * diff > 0 else 1.0)
        ci_level = 1 - 2 * alpha  # the two-sided interval that matches a one-sided test at alpha
        half = _crit(m, c, v, 1 - alpha) * se_ci
    improving = good * diff > 0
    if p < alpha:
        verdict = "Significant improvement" if improving else "Significant decline"
    else:
        verdict = "No significant difference" if sidedness == "two-sided" else "Not significant"
    if sidedness == "one-sided" and p < alpha and not improving:
        verdict = "No significant difference"
    return Outcome(diff, diff / c.mean if c.mean else None, diff - half, diff + half, ci_level, stat, p, verdict, None)


def non_inferiority(m: MetricDef, c: Arm, v: Arm, margin: float, alpha: float = 0.05) -> Outcome:
    """Guardrail: is the variant's harm smaller than `margin` (in the metric's units)? H0: harm >= margin.

    Harm is how much worse the variant is, in the metric's bad direction. Passed means the one-sided upper
    bound of harm is below the margin; Failed means even the lower bound is beyond it.
    """
    _check(c, v)
    diff = v.mean - c.mean
    harm = -diff if m.good_direction == "higher" else diff
    se = _se_unpooled(m, c, v)
    if se == 0:
        passed = harm < margin
        z, p, upper, lower = 0.0, 0.0 if passed else 1.0, harm, harm
    else:
        z = (harm - margin) / se
        p = _cdf(m, c, v, z)
        k = _crit(m, c, v, 1 - alpha)
        upper, lower = harm + k * se, harm - k * se
    if upper < margin:
        verdict = "Passed"
    elif lower > margin:
        verdict = "Failed"
    else:
        verdict = "Inconclusive"
    # express the harm bounds as a difference (variant minus control) so every metric reads the same way
    lo, hi = (-upper, -lower) if m.good_direction == "higher" else (lower, upper)
    power = achieved_power(m, c, v, margin, alpha, "one-sided", non_inferiority=True)
    return Outcome(diff, diff / c.mean if c.mean else None, lo, hi, 1 - 2 * alpha, z, p, verdict, power)


def achieved_power(m: MetricDef, c: Arm, v: Arm, effect_abs: float, alpha: float, sidedness: str,
                   non_inferiority: bool = False) -> float | None:
    """Power to detect `effect_abs` (or to show non-inferiority at zero true difference) with the users observed."""
    se = _se_unpooled(m, c, v)
    if se == 0:
        return None
    if sidedness == "one-sided" or non_inferiority:
        return float(norm.cdf(effect_abs / se - norm.ppf(1 - alpha)))
    z = norm.ppf(1 - alpha / 2)
    return float(norm.cdf(effect_abs / se - z) + norm.cdf(-effect_abs / se - z))


def analyze_metric(metric: MetricDef, role: str, control: Arm, variant: Arm, plan: MetricPlan | None) -> Outcome:
    """Run the right test for a metric in its role. A secondary metric has no plan: two-sided at alpha 0.05, exploratory."""
    if role == "guardrail":
        return non_inferiority(metric, control, variant, plan.effect_abs, plan.alpha)
    alpha, sided = (plan.alpha, plan.sidedness) if plan else (0.05, "two-sided")
    out = two_group_test(metric, control, variant, alpha, sided)
    if plan is None:
        return out
    power = achieved_power(metric, control, variant, plan.effect_abs, alpha, sided)
    return Outcome(out.difference, out.relative_lift, out.ci_low, out.ci_high, out.ci_level, out.statistic,
                   out.p_value, out.verdict, power)
