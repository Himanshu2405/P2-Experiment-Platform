"""The placebo check: an A/A test on the control users. A healthy test finds a difference only as often as it promises."""
import numpy as np
import pytest

from p2.catalog.seed import seed_registry
from p2.stats import placebo
from p2.stats.tests import Arm

REG = seed_registry()
CONV, REVENUE = REG.get("conversion_rate"), REG.get("revenue_per_user")


def rate_arm(n=3000, p=0.05):
    return Arm(n, p, p * (1 - p))


def test_a_healthy_frequentist_test_finds_differences_about_as_often_as_alpha_promises():
    for seed in range(15):
        r = placebo.run_placebo(CONV, rate_arm(), None, "frequentist", alpha=0.05, seed=seed)
        assert (r.reps, r.n, r.expected) == (200, 3000, 0.05)
        assert 0.0 <= r.low < 0.05 < r.high <= 0.2
    healthy = [placebo.run_placebo(CONV, rate_arm(), None, "frequentist", alpha=0.05, seed=s).ok for s in range(40)]
    assert sum(healthy) >= 38                         # a 99% range: about one healthy check in a hundred trips by chance


def test_a_skewed_continuous_metric_is_checked_on_its_own_values():
    rng = np.random.default_rng(1)
    values = rng.gamma(0.3, 30, 3000) * (rng.random(3000) < 0.1)       # mostly zero, a few large: like revenue per user
    arm = Arm(len(values), values.mean(), values.var(ddof=1))
    rates = [placebo.run_placebo(REVENUE, arm, values, "frequentist", seed=s).rate for s in range(10)]
    assert 0.02 < np.mean(rates) < 0.09


def test_the_check_catches_an_analysis_that_is_too_confident(monkeypatch):
    """Sabotage the analysis the way real bugs do (pretend there are four times the users, so every spread looks too small): the check
    must fail, because it is the only thing that would notice."""
    real = placebo.two_group_test
    monkeypatch.setattr(placebo, "two_group_test",
                        lambda m, a, b, alpha, sidedness: real(m, Arm(a.n * 4, a.mean, a.var), Arm(b.n * 4, b.mean, b.var), alpha, sidedness))
    broken = placebo.run_placebo(CONV, rate_arm(), None, "frequentist", alpha=0.05)
    assert broken.rate > 0.2 and not broken.ok and broken.rate > broken.high
    monkeypatch.undo()
    assert placebo.run_placebo(CONV, rate_arm(), None, "frequentist", alpha=0.05).ok


def test_one_sided_tests_only_count_improvements():
    two = placebo.run_placebo(CONV, rate_arm(), None, "frequentist", alpha=0.05, sidedness="two-sided", seed=3)
    one = placebo.run_placebo(CONV, rate_arm(), None, "frequentist", alpha=0.05, sidedness="one-sided", seed=3)
    assert one.ok and two.ok


def test_the_bayesian_rule_is_honest_about_a_loose_threshold():
    loose = placebo.run_placebo(CONV, rate_arm(), None, "bayesian", threshold_abs=0.0005)
    tight = placebo.run_placebo(CONV, rate_arm(), None, "bayesian", threshold_abs=0.00003)
    assert loose.rate > 0.2 and not loose.ok           # a 0.05 pp threshold is close to the noise of 3,000 users
    assert tight.rate <= placebo.BAYES_OK and tight.ok
    assert (loose.expected, loose.low, loose.high) == (None, None, None)


def test_the_result_is_repeatable_and_too_few_users_is_refused():
    a = placebo.run_placebo(CONV, rate_arm(), None, "frequentist", seed=5)
    assert a == placebo.run_placebo(CONV, rate_arm(), None, "frequentist", seed=5)
    with pytest.raises(ValueError, match="too few"):
        placebo.run_placebo(CONV, rate_arm(n=10), None, "frequentist")
    with pytest.raises(ValueError, match="too few"):
        placebo.run_placebo(REVENUE, Arm(10, 1.0, 1.0), np.ones(10), "frequentist")
    with pytest.raises(ValueError, match="too few"):
        placebo.run_placebo(REVENUE, Arm(100, 1.0, 1.0), None, "frequentist")           # a continuous metric needs its values
