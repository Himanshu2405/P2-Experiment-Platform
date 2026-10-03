"""The statistics engine, validated against scipy and statsmodels, then against simulation with a known truth."""
import numpy as np
import pytest
from scipy import stats as sps
from statsmodels.stats.power import NormalIndPower
from statsmodels.stats.proportion import confint_proportions_2indep, proportions_ztest

from p2.catalog.seed import seed_registry
from p2.stats.plan import MetricPlan
from p2.stats.tests import Arm, achieved_power, analyze_metric, non_inferiority, two_group_test

reg = seed_registry()
CONV, REVENUE, REFUND = reg.get("conversion_rate"), reg.get("revenue_per_user"), reg.get("refund_rate")


def bin_arm(n, successes):
    p = successes / n
    return Arm(n, p, p * (1 - p) * n / (n - 1))


# ---- binary: two-proportion z-test --------------------------------------------------------------------------
@pytest.mark.parametrize("nc,sc,nv,sv", [(10_000, 350, 10_000, 400), (5_000, 100, 7_000, 175), (30_000, 900, 30_000, 1000)])
def test_z_test_matches_statsmodels(nc, sc, nv, sv):
    out = two_group_test(CONV, bin_arm(nc, sc), bin_arm(nv, sv))
    z, p = proportions_ztest([sv, sc], [nv, nc], alternative="two-sided")  # pooled, variant first
    assert out.statistic == pytest.approx(z, rel=1e-9) and out.p_value == pytest.approx(p, rel=1e-9)
    lo, hi = confint_proportions_2indep(sv, nv, sc, nc, method="wald", compare="diff", alpha=0.05)
    assert (out.ci_low, out.ci_high) == (pytest.approx(lo, rel=1e-9), pytest.approx(hi, rel=1e-9))
    assert out.difference == pytest.approx(sv / nv - sc / nc) and out.relative_lift == pytest.approx((sv / nv) / (sc / nc) - 1)


def test_verdicts_follow_significance_and_direction():
    win = two_group_test(CONV, bin_arm(50_000, 1_750), bin_arm(50_000, 2_000))
    loss = two_group_test(CONV, bin_arm(50_000, 2_000), bin_arm(50_000, 1_750))
    flat = two_group_test(CONV, bin_arm(2_000, 70), bin_arm(2_000, 72))
    assert (win.verdict, loss.verdict, flat.verdict) == ("Significant improvement", "Significant decline", "No significant difference")
    assert win.ci_low > 0 and loss.ci_high < 0 and flat.ci_low < 0 < flat.ci_high


def test_lower_is_better_flips_what_counts_as_an_improvement():
    down = two_group_test(REFUND, bin_arm(50_000, 500), bin_arm(50_000, 350))
    assert down.difference < 0 and down.verdict == "Significant improvement"
    up = two_group_test(REFUND, bin_arm(50_000, 350), bin_arm(50_000, 500))
    assert up.verdict == "Significant decline"


def test_one_sided_halves_the_p_value_in_the_good_direction_and_ignores_the_wrong_one():
    c, v = bin_arm(10_000, 350), bin_arm(10_000, 400)
    two, one = two_group_test(CONV, c, v), two_group_test(CONV, c, v, sidedness="one-sided")
    assert one.p_value == pytest.approx(two.p_value / 2) and one.ci_level == pytest.approx(0.90)
    wrong = two_group_test(CONV, v, c, sidedness="one-sided")
    assert wrong.p_value > 0.9 and wrong.verdict != "Significant improvement"


# ---- continuous: Welch ---------------------------------------------------------------------------------------
def test_welch_matches_scipy_on_raw_data():
    rng = np.random.default_rng(1)
    a, b = rng.lognormal(1.0, 1.0, 4000), rng.lognormal(1.05, 1.3, 6000)
    out = two_group_test(REVENUE, Arm(len(a), a.mean(), a.var(ddof=1)), Arm(len(b), b.mean(), b.var(ddof=1)))
    ref = sps.ttest_ind(b, a, equal_var=False)
    assert out.statistic == pytest.approx(ref.statistic, rel=1e-9) and out.p_value == pytest.approx(ref.pvalue, rel=1e-9)
    ci = ref.confidence_interval(0.95)
    assert (out.ci_low, out.ci_high) == (pytest.approx(ci.low, rel=1e-9), pytest.approx(ci.high, rel=1e-9))


def test_welch_uses_unequal_variances_not_a_pooled_one():
    c, v = Arm(1000, 5.0, 4.0), Arm(3000, 5.4, 100.0)
    welch = two_group_test(REVENUE, c, v)
    se = np.sqrt(4 / 1000 + 100 / 3000)
    pooled_var = (999 * 4 + 2999 * 100) / 3998
    pooled_se = np.sqrt(pooled_var * (1 / 1000 + 1 / 3000))
    assert welch.statistic == pytest.approx(0.4 / se) and abs(welch.statistic - 0.4 / pooled_se) > 0.5


# ---- guardrail: non-inferiority ------------------------------------------------------------------------------
def test_non_inferiority_formulas():
    c, v = Arm(20_000, 0.005, 0), Arm(20_000, 0.0055, 0)
    out = non_inferiority(REFUND, c, v, margin=0.0025, alpha=0.05)
    se = np.sqrt(0.005 * 0.995 / 20_000 + 0.0055 * 0.9945 / 20_000)
    assert out.statistic == pytest.approx((0.0005 - 0.0025) / se) and out.p_value == pytest.approx(sps.norm.cdf((0.0005 - 0.0025) / se))
    assert out.ci_high == pytest.approx(0.0005 + 1.6448536 * se, rel=1e-6) and out.ci_low == pytest.approx(0.0005 - 1.6448536 * se, rel=1e-6)
    assert out.verdict == "Passed" and out.ci_level == pytest.approx(0.90)


def test_non_inferiority_three_verdicts():
    base = Arm(20_000, 0.005, 0)
    assert non_inferiority(REFUND, base, Arm(20_000, 0.0050, 0), 0.0025).verdict == "Passed"
    assert non_inferiority(REFUND, base, Arm(20_000, 0.0072, 0), 0.0025).verdict == "Inconclusive"
    assert non_inferiority(REFUND, base, Arm(20_000, 0.0150, 0), 0.0025).verdict == "Failed"
    better = non_inferiority(REFUND, base, Arm(20_000, 0.0030, 0), 0.0025)        # fewer refunds is not harm
    assert better.verdict == "Passed" and better.ci_high < 0.0025


def test_non_inferiority_direction_for_higher_is_better_metrics():
    guard = reg.get("add_to_cart_rate")
    c = Arm(20_000, 0.10, 0)
    assert non_inferiority(guard, c, Arm(20_000, 0.095, 0), 0.02).verdict == "Passed"         # dropped 0.5 points, margin 2
    assert non_inferiority(guard, c, Arm(20_000, 0.070, 0), 0.02).verdict == "Failed"         # dropped 3 points
    assert non_inferiority(guard, c, Arm(20_000, 0.110, 0), 0.02).verdict == "Passed"


# ---- achieved power -------------------------------------------------------------------------------------------
def test_achieved_power_matches_statsmodels_for_equal_arms():
    sd, n, delta = 10.0, 3000, 0.8
    arm = Arm(n, 5.0, sd**2)
    got = achieved_power(REVENUE, arm, arm, delta, 0.05, "two-sided")
    assert got == pytest.approx(NormalIndPower().power(effect_size=delta / sd, nobs1=n, alpha=0.05, ratio=1.0, alternative="two-sided"))
    one = achieved_power(REVENUE, arm, arm, delta, 0.05, "one-sided")
    assert one == pytest.approx(NormalIndPower().power(effect_size=delta / sd, nobs1=n, alpha=0.05, ratio=1.0, alternative="larger"))


def test_power_rises_with_users_and_with_effect():
    small, big = Arm(2_000, 0.035, 0), Arm(20_000, 0.035, 0)
    assert achieved_power(CONV, big, big, 0.005, 0.05, "two-sided") > achieved_power(CONV, small, small, 0.005, 0.05, "two-sided")
    assert achieved_power(CONV, big, big, 0.01, 0.05, "two-sided") > achieved_power(CONV, big, big, 0.005, 0.05, "two-sided")


# ---- simulation with a known truth ----------------------------------------------------------------------------
def draw_bin(rng, n, p):
    return bin_arm(n, int(rng.binomial(n, p)))


def test_a_a_tests_reject_about_five_percent_of_the_time():
    rng = np.random.default_rng(7)
    sims = 4000
    rejected = sum(two_group_test(CONV, draw_bin(rng, 5000, 0.035), draw_bin(rng, 5000, 0.035)).p_value < 0.05 for _ in range(sims))
    assert rejected / sims == pytest.approx(0.05, abs=0.012)


def test_continuous_a_a_tests_also_reject_about_five_percent_of_the_time():
    rng = np.random.default_rng(8)
    n, mu, sd, sims = 3000, 5.0, 12.0, 4000
    def arm():
        return Arm(n, rng.normal(mu, sd / np.sqrt(n)), sd**2 * rng.chisquare(n - 1) / (n - 1))
    rejected = sum(two_group_test(REVENUE, arm(), arm()).p_value < 0.05 for _ in range(sims))
    assert rejected / sims == pytest.approx(0.05, abs=0.012)


def test_confidence_intervals_cover_the_true_difference_about_95_percent_of_the_time():
    rng = np.random.default_rng(9)
    p0, p1, sims = 0.035, 0.042, 4000
    covered = 0
    for _ in range(sims):
        o = two_group_test(CONV, draw_bin(rng, 8000, p0), draw_bin(rng, 8000, p1))
        covered += o.ci_low <= (p1 - p0) <= o.ci_high
    assert covered / sims == pytest.approx(0.95, abs=0.012)


def test_a_real_effect_is_found_about_as_often_as_the_power_says():
    rng = np.random.default_rng(10)
    n, p0, p1, sims = 12_000, 0.035, 0.042, 3000
    hit = sum(two_group_test(CONV, draw_bin(rng, n, p0), draw_bin(rng, n, p1)).p_value < 0.05 for _ in range(sims))
    expected = achieved_power(CONV, Arm(n, p0, 0), Arm(n, p1, 0), p1 - p0, 0.05, "two-sided")
    assert hit / sims == pytest.approx(expected, abs=0.03)


def test_non_inferiority_has_the_right_error_rate_at_the_margin_and_the_right_power_at_zero():
    rng = np.random.default_rng(11)
    n, p0, margin, sims = 40_000, 0.005, 0.002, 3000
    at_margin = sum(non_inferiority(REFUND, draw_bin(rng, n, p0), draw_bin(rng, n, p0 + margin), margin).verdict == "Passed" for _ in range(sims))
    assert at_margin / sims == pytest.approx(0.05, abs=0.015)            # wrongly passing when harm equals the margin
    at_zero = sum(non_inferiority(REFUND, draw_bin(rng, n, p0), draw_bin(rng, n, p0), margin).verdict == "Passed" for _ in range(sims))
    expected = achieved_power(REFUND, Arm(n, p0, 0), Arm(n, p0, 0), margin, 0.05, "one-sided", non_inferiority=True)
    assert at_zero / sims == pytest.approx(expected, abs=0.03)


# ---- edges ---------------------------------------------------------------------------------------------------
def test_edges():
    with pytest.raises(ValueError, match="at least 2"):
        two_group_test(CONV, Arm(1, 0.0, 0), Arm(100, 0.1, 0))
    flat = two_group_test(CONV, Arm(100, 0.0, 0), Arm(100, 0.0, 0))
    assert flat.p_value == 1.0 and flat.verdict == "No significant difference" and flat.relative_lift is None
    assert achieved_power(CONV, Arm(100, 0.0, 0), Arm(100, 0.0, 0), 0.01, 0.05, "two-sided") is None
    same = non_inferiority(REFUND, Arm(100, 0.0, 0), Arm(100, 0.0, 0), 0.01)
    assert same.verdict == "Passed"


# ---- analyze_metric and MetricPlan ----------------------------------------------------------------------------
def plan(metric=CONV, role="primary", **over):
    base = dict(metric=metric, role=role, baseline=0.035, effect=0.10, effect_kind="relative", alpha=0.05, power=0.8,
                sidedness="two-sided" if role == "primary" else "one-sided")
    if metric.type == "continuous":
        base.update(baseline=3.5, std=27.0)
    return MetricPlan(**{**base, **over})


def test_analyze_metric_by_role():
    c, v = bin_arm(30_000, 1_050), bin_arm(30_000, 1_200)
    primary = analyze_metric(CONV, "primary", c, v, plan())
    assert primary.verdict == "Significant improvement" and 0 < primary.achieved_power < 1
    secondary = analyze_metric(CONV, "secondary", c, v, None)
    assert secondary.verdict == primary.verdict and secondary.achieved_power is None
    guardrail = analyze_metric(REFUND, "guardrail", bin_arm(30_000, 150), bin_arm(30_000, 160), plan(REFUND, "guardrail", baseline=0.005, effect=0.5))
    assert guardrail.verdict == "Passed" and guardrail.achieved_power > 0.5


def test_effect_abs_handles_relative_and_absolute():
    assert plan(effect=0.10).effect_abs == pytest.approx(0.0035)
    assert plan(effect=0.004, effect_kind="absolute").effect_abs == pytest.approx(0.004)


@pytest.mark.parametrize("bad,msg", [
    (dict(alpha=0.0), "alpha"), (dict(alpha=0.6), "alpha"), (dict(power=0.4), "power"), (dict(power=1.0), "power"),
    (dict(effect=0.0), "positive"), (dict(baseline=0.0), "between 0 and 1"), (dict(baseline=3.5), "between 0 and 1"),
])
def test_plan_validation_for_binary(bad, msg):
    with pytest.raises(ValueError, match=msg):
        plan(**bad)


def test_plan_validation_for_continuous_guardrail_and_any_role():
    with pytest.raises(ValueError, match="standard deviation"):
        plan(REVENUE, std=None)
    with pytest.raises(ValueError, match="standard deviation"):
        plan(REVENUE, std=0.0)
    with pytest.raises(ValueError, match="one-sided"):
        MetricPlan(REFUND, "guardrail", 0.005, 0.5, "relative", 0.05, 0.8, "two-sided")
    assert plan(REFUND, "primary").role == "primary"  # roles are not tied to metrics: any metric can be primary
