"""The sample ratio mismatch check: the maths offline."""
import pytest
from scipy.stats import chisquare

from p2.stats.srm import srm_check


def test_an_even_split_is_balanced_and_matches_scipy():
    r = srm_check(5000, 5030)
    assert r.balanced and r.p_value == pytest.approx(chisquare([5000, 5030]).pvalue)


def test_a_clearly_broken_split_is_not_balanced():
    r = srm_check(5000, 4500)
    assert not r.balanced and r.p_value < 0.001


def test_a_normal_wobble_is_not_flagged_at_the_strict_threshold():
    assert srm_check(5100, 4900).balanced            # p is about 0.005, above 0.001


def test_an_uneven_planned_split_is_respected():
    assert srm_check(7000, 3000, expected_control=0.7).balanced
    assert not srm_check(7000, 3000).balanced


def test_bad_input_is_refused():
    with pytest.raises(ValueError):
        srm_check(0, 0)
    with pytest.raises(ValueError):
        srm_check(10, 10, expected_control=1.0)
