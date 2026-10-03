"""The Bayesian engine: posterior numbers against closed forms, the decision rules, and Monte Carlo checks that the method behaves
(A/A runs rarely call a winner, 95% credible intervals cover the truth about 95% of the time)."""
from datetime import date, timedelta

import numpy as np
import pytest
from scipy import integrate, stats

from p2.catalog.seed import seed_registry
from p2.stats import bayes
from p2.stats.bayes import BayesPlan
from p2.stats.tests import Arm

REG = seed_registry()
CONV, REFUND, REVENUE = REG.get("conversion_rate"), REG.get("refund_rate"), REG.get("revenue_per_user")


def bin_arm(n, p):
    return Arm(n, p, p * (1 - p))


def test_rate_chance_to_win_matches_the_exact_beta_integral():
    c, v = bin_arm(1000, 0.10), bin_arm(1000, 0.12)
    out = bayes.analyze_bayes(CONV, c, v)
    pc, pv = stats.beta(1 + 100, 1 + 900), stats.beta(1 + 120, 1 + 880)
    exact, _ = integrate.quad(lambda x: pc.pdf(x) * pv.sf(x), 0, 1, limit=200)
    assert out.chance_to_win == pytest.approx(exact, abs=0.004)
    assert out.difference == pytest.approx(pv.mean() - pc.mean(), abs=2e-4)


def test_average_posterior_matches_the_normal_closed_form():
    c, v = Arm(4000, 10.0, 400.0), Arm(4000, 10.8, 420.0)
    out = bayes.analyze_bayes(REVENUE, c, v)
    mu, sigma = v.mean - c.mean, (c.var / c.n + v.var / v.n) ** 0.5
    z = mu / sigma
    assert out.chance_to_win == pytest.approx(stats.norm.cdf(z), abs=0.004)
    assert out.risk_variant == pytest.approx(sigma * stats.norm.pdf(z) - mu * stats.norm.cdf(-z), abs=0.003)    # E[max(-d, 0)]
    assert out.risk_control == pytest.approx(sigma * stats.norm.pdf(z) + mu * stats.norm.cdf(z), abs=0.003)     # E[max(d, 0)]
    assert (out.ci_low, out.ci_high) == pytest.approx((mu - 1.96 * sigma, mu + 1.96 * sigma), abs=0.01)
    assert out.relative_lift == pytest.approx(mu / c.mean, abs=2e-3)


def test_the_result_is_repeatable_because_the_draws_are_seeded():
    c, v = bin_arm(500, 0.10), bin_arm(500, 0.11)
    assert bayes.analyze_bayes(CONV, c, v) == bayes.analyze_bayes(CONV, c, v)


def test_a_lower_is_better_metric_flips_which_arm_is_better():
    c, v = bin_arm(5000, 0.05), bin_arm(5000, 0.04)           # refund rate down: the variant is better
    out = bayes.analyze_bayes(REFUND, c, v)
    assert out.chance_to_win > 0.99 and out.risk_variant < out.risk_control
    assert out.difference < 0                                  # the difference itself stays variant minus control


def test_risks_are_never_negative_and_a_clear_winner_has_a_tiny_risk_of_shipping():
    out = bayes.analyze_bayes(CONV, bin_arm(20000, 0.10), bin_arm(20000, 0.12))
    assert out.risk_variant == pytest.approx(0, abs=1e-6) and out.risk_control == pytest.approx(0.02, abs=2e-4)


def test_each_arm_needs_two_users():
    with pytest.raises(ValueError, match="at least 2"):
        bayes.analyze_bayes(CONV, bin_arm(1, 0.1), bin_arm(100, 0.1))


# ---- the plans --------------------------------------------------------------------------------------------------------------
def test_plan_validation():
    with pytest.raises(ValueError, match="risk threshold"):
        BayesPlan(CONV, "primary", threshold=0)
    with pytest.raises(ValueError, match="minimum days"):
        BayesPlan(CONV, "primary", threshold=0.001, min_days=0)
    with pytest.raises(ValueError, match="margin"):
        BayesPlan(REFUND, "guardrail")
    with pytest.raises(ValueError, match="harm limit"):
        BayesPlan(REFUND, "guardrail", margin=0.1, harm_limit=0.6)
    assert BayesPlan(REFUND, "guardrail", margin=0.1).harm_limit == 0.01       # strict by default


def test_threshold_and_margin_units():
    rate = BayesPlan(CONV, "primary", threshold=0.0005)
    avg = BayesPlan(REVENUE, "primary", threshold=0.005)
    assert rate.threshold_abs(0.10) == 0.0005                   # a rate's threshold is absolute (pp)
    assert avg.threshold_abs(20.0) == pytest.approx(0.1)        # an average's is a share of the control mean
    rel = BayesPlan(REFUND, "guardrail", margin=0.25, margin_kind="relative")
    ab = BayesPlan(REFUND, "guardrail", margin=0.002, margin_kind="absolute")
    assert rel.margin_abs(0.004) == pytest.approx(0.001) and ab.margin_abs(0.004) == 0.002


# ---- the decision rules -----------------------------------------------------------------------------------------------------
def out_with(rv, rc, harm=None):
    return bayes.BayesOutcome(0.5, 0.0, 0.0, -0.01, 0.01, rv, rc, harm)


@pytest.mark.parametrize("rv, rc, running, ended", [
    (0.0001, 0.01, bayes.VERDICT_VARIANT, bayes.VERDICT_VARIANT),
    (0.01, 0.0001, bayes.VERDICT_CONTROL, bayes.VERDICT_CONTROL),
    (0.0001, 0.0002, bayes.VERDICT_EITHER, bayes.VERDICT_EITHER),
    (0.01, 0.02, bayes.VERDICT_MORE, bayes.VERDICT_DONE),
])
def test_primary_verdict_compares_both_risks_with_the_threshold(rv, rc, running, ended):
    assert bayes.primary_verdict(out_with(rv, rc), 0.0005, days=10, min_days=7, ended=False) == running
    assert bayes.primary_verdict(out_with(rv, rc), 0.0005, days=14, min_days=7, ended=True) == ended


def test_no_verdict_before_the_minimum_days_but_the_end_date_always_gets_one():
    assert bayes.primary_verdict(out_with(0.0, 0.01), 0.0005, days=3, min_days=7, ended=False) == "Collecting evidence (day 3 of 7)"
    assert bayes.primary_verdict(out_with(0.0, 0.01), 0.0005, days=3, min_days=7, ended=True) == bayes.VERDICT_VARIANT
    assert bayes.primary_verdict(out_with(0.0, 0.01), 0.0005, days=7, min_days=7, ended=False) == bayes.VERDICT_VARIANT   # day 7 counts


def test_guardrail_verdict():
    g = lambda harm, **kw: bayes.guardrail_verdict(out_with(0, 0, harm), 0.01, **{"days": 10, "min_days": 7, "ended": False, **kw})
    assert g(0.004) == "Passed" and g(0.01) == "Inconclusive" and g(0.2) == "Inconclusive" and g(0.51) == "Failed"
    assert g(0.004, days=2) == "Collecting evidence (day 2 of 7)"


def test_decide_gives_a_guardrail_its_chance_of_harm_and_a_secondary_no_verdict():
    c, v = bin_arm(20000, 0.05), bin_arm(20000, 0.05)
    guard = BayesPlan(REFUND, "guardrail", margin=0.5, margin_kind="relative")        # 50% worse than 5% is 2.5 pp
    g = bayes.decide(REFUND, guard, c, v, days=10, min_days=7, ended=False)
    assert g.chance_of_harm < 0.001 and g.verdict == "Passed"
    s = bayes.decide(REFUND, None, c, v, days=10, min_days=7, ended=False)
    assert s.verdict is None and s.chance_of_harm is None


def test_a_harmful_variant_fails_the_guardrail():
    c, v = bin_arm(20000, 0.05), bin_arm(20000, 0.07)
    guard = BayesPlan(REFUND, "guardrail", margin=0.002, margin_kind="absolute")
    assert bayes.decide(REFUND, guard, c, v, days=10, min_days=7, ended=False).verdict == "Failed"


# ---- from saved rows --------------------------------------------------------------------------------------------------------
LAUNCH, END = date(2026, 9, 1), date(2026, 9, 14)


def design(min_days=7):
    return [{"item_id": "conversion_rate", "item_version": 1, "kind": "metric", "role": "primary", "loss_threshold": 0.0005, "min_days": min_days,
             "effect": None, "effect_kind": "absolute", "harm_limit": None},
            {"item_id": "revenue_per_user", "item_version": 1, "kind": "metric", "role": "secondary", "loss_threshold": None, "min_days": None,
             "effect": None, "effect_kind": None, "harm_limit": None}]


def row(item, c, v, through, var_c=None, var_v=None, n=20000):
    return {"item_id": item, "mean_control": c, "mean_variant": v, "n_control": n, "n_variant": n, "var_control": var_c, "var_variant": var_v,
            "through_date": through}


def test_analyze_rows_counts_days_from_launch_and_knows_when_the_experiment_has_ended():
    rows = [row("conversion_rate", 0.10, 0.12, date(2026, 9, 3)), row("conversion_rate", 0.10, 0.12, date(2026, 9, 7)),
            row("conversion_rate", 0.10, 0.10, END)]
    a, b, c = bayes.analyze_rows(rows, design(), REG, LAUNCH, END)
    assert a.verdict == "Collecting evidence (day 3 of 7)" and b.verdict == bayes.VERDICT_VARIANT
    assert c.verdict in (bayes.VERDICT_EITHER, bayes.VERDICT_DONE, bayes.VERDICT_VARIANT, bayes.VERDICT_CONTROL)   # a verdict, never "collecting"


def test_the_minimum_days_cannot_exceed_the_runtime():
    short_end = date(2026, 9, 3)                                       # a 3-day experiment with min_days 7
    out = bayes.analyze_rows([row("conversion_rate", 0.10, 0.12, short_end)], design(), REG, LAUNCH, short_end)[0]
    assert not out.verdict.startswith("Collecting")


def test_a_continuous_row_without_variances_cannot_be_rebuilt_but_a_rate_can():
    rows = [row("revenue_per_user", 10.0, 11.0, END), row("revenue_per_user", 10.0, 11.0, END, 400.0, 410.0),
            row("conversion_rate", 0.1, 0.12, END), row("conversion_rate", 0.1, 0.12, END, n=1)]
    out = bayes.analyze_rows(rows, design(), REG, LAUNCH, END)
    assert out[0] is None and out[1] is not None and out[2] is not None and out[3] is None


def test_the_plan_from_a_row():
    p = bayes.plan_from_row(CONV, design()[0])
    assert (p.role, p.threshold, p.min_days) == ("primary", 0.0005, 7)
    g = bayes.plan_from_row(REFUND, {"role": "guardrail", "effect": 0.2, "effect_kind": "relative", "harm_limit": None})
    assert (g.margin, g.harm_limit) == (0.2, 0.01)
    assert bayes.plan_from_row(REVENUE, design()[1]) is None


def test_daily_posterior_skips_days_with_too_few_users_and_follows_the_evidence():
    rows = []
    for i, n in enumerate([1, 200, 2000, 20000]):
        day = LAUNCH + timedelta(days=i)
        rows += [{"day": day, "arm": "control", "n_users": n, "mean_value": 0.10, "var_value": 0.09},
                 {"day": day, "arm": "variant", "n_users": n, "mean_value": 0.12, "var_value": 0.1056}]
    daily = bayes.daily_posterior(CONV, rows, 0.0005)
    assert [d["day"] for d in daily] == [LAUNCH + timedelta(days=i) for i in (1, 2, 3)]
    chance = [d["chance_to_win"] for d in daily]
    assert chance == sorted(chance) and chance[-1] > 0.999            # more users, the same gap: more certainty
    assert all(d["threshold"] == 0.0005 for d in daily)
    assert np.isnan(bayes.daily_posterior(CONV, rows)[0]["threshold"])


# ---- does the method behave? (Monte Carlo, seeded) ---------------------------------------------------------------------------
def draw_bin(rng, n, p):
    k = rng.binomial(n, p)
    return Arm(n, k / n, k / n * (1 - k / n))


def aa_one_sided_rate(threshold, trials=400, seed=1):
    """Share of A/A runs (no true difference, 20,000 users per arm, 10% rate) that name one arm the safer choice."""
    rng = np.random.default_rng(seed)
    hits = 0
    for _ in range(trials):
        out = bayes.analyze_bayes(CONV, draw_bin(rng, 20000, 0.10), draw_bin(rng, 20000, 0.10), draws=4000)
        hits += bayes.primary_verdict(out, threshold, days=14, min_days=7, ended=False) in (bayes.VERDICT_VARIANT, bayes.VERDICT_CONTROL)
    return hits / trials


def test_the_threshold_must_be_small_next_to_the_noise_for_a_no_effect_test_to_stay_unclear():
    """The rule bounds expected loss, not the false-win rate: with a threshold near the noise (one standard error of the difference is 0.3 pp
    here) a slightly positive A/A gap already counts as safe to ship, because shipping a no-effect variant costs almost nothing."""
    loose, tight, tighter = aa_one_sided_rate(0.0005), aa_one_sided_rate(0.0001), aa_one_sided_rate(0.00003)
    assert loose > 0.3 > tight > tighter
    assert tighter < 0.1


def test_when_the_rule_says_ship_the_variant_the_true_loss_is_far_below_the_threshold():
    """Calibration: true differences drawn from a wide spread (so the flat prior is fair); among runs that say 'variant is the safer choice'
    the average true loss from shipping stays under the threshold."""
    rng, threshold, losses = np.random.default_rng(5), 0.0005, []
    for _ in range(1500):
        true_v = min(max(0.10 + rng.normal(0, 0.02), 0.001), 0.999)
        out = bayes.analyze_bayes(CONV, draw_bin(rng, 20000, 0.10), draw_bin(rng, 20000, true_v), draws=4000)
        if bayes.primary_verdict(out, threshold, days=14, min_days=7, ended=False) == bayes.VERDICT_VARIANT:
            losses.append(max(0.10 - true_v, 0.0))
    assert len(losses) > 200 and np.mean(losses) < threshold


def test_a_real_effect_is_almost_always_found():
    rng, wins = np.random.default_rng(6), 0
    for _ in range(100):
        out = bayes.analyze_bayes(CONV, draw_bin(rng, 20000, 0.10), draw_bin(rng, 20000, 0.11), draws=4000)
        wins += bayes.primary_verdict(out, 0.0005, days=14, min_days=7, ended=False) == bayes.VERDICT_VARIANT
    assert wins >= 95


def test_the_95_percent_credible_interval_covers_the_planted_truth_about_95_percent_of_the_time():
    rng = np.random.default_rng(2)
    covered, trials = 0, 400
    for _ in range(trials):
        out = bayes.analyze_bayes(CONV, draw_bin(rng, 5000, 0.10), draw_bin(rng, 5000, 0.11), draws=4000)
        covered += out.ci_low <= 0.01 <= out.ci_high
    assert covered / trials == pytest.approx(0.95, abs=0.035)


def test_the_credible_interval_for_an_average_also_covers():
    rng = np.random.default_rng(3)
    covered, trials = 0, 400

    def arm(mu):
        x = rng.gamma(2.0, mu / 2.0, 3000)                            # skewed, non-negative, like revenue
        return Arm(len(x), float(x.mean()), float(x.var(ddof=1)))

    for _ in range(trials):
        out = bayes.analyze_bayes(REVENUE, arm(10.0), arm(10.5), draws=4000)
        covered += out.ci_low <= 0.5 <= out.ci_high
    assert covered / trials == pytest.approx(0.95, abs=0.035)


def test_rows_from_before_through_date_was_stored_count_as_final():
    old = {k: v for k, v in row("conversion_rate", 0.10, 0.12, END).items() if k != "through_date"}
    assert bayes.analyze_rows([old], design(), REG, LAUNCH, END)[0].verdict == bayes.VERDICT_VARIANT
