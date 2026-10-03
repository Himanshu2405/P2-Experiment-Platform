import numpy as np
import pandas as pd
import pytest

from p2.registry.calc import compute_metric, metric_values
from p2.catalog.seed import seed_registry as load_registry

reg = load_registry()


def test_binary_mean_and_std():
    df = pd.DataFrame({"conversion_rate": [1, 0, 0, 1, 0]})
    s = compute_metric(reg.get("conversion_rate"), df)
    assert s.n == 5 and s.mean == pytest.approx(0.4)
    assert s.std == pytest.approx(np.std([1, 0, 0, 1, 0], ddof=1))
    assert s.se == pytest.approx(s.std / np.sqrt(5))


def test_continuous_includes_zeros():
    df = pd.DataFrame({"revenue_per_user": [0.0, 0.0, 10.0, 20.0]})
    s = compute_metric(reg.get("revenue_per_user"), df)
    assert s.mean == pytest.approx(7.5)


def test_same_function_gives_same_answer_on_repeat():
    df = pd.DataFrame({"refund_rate": [0, 1, 0, 0]})
    m = reg.get("refund_rate")
    assert compute_metric(m, df) == compute_metric(m, df)


def test_missing_column():
    with pytest.raises(KeyError):
        compute_metric(reg.get("conversion_rate"), pd.DataFrame({"x": [1, 2]}))


def test_nulls_rejected():
    with pytest.raises(ValueError, match="null"):
        metric_values(reg.get("conversion_rate"), pd.DataFrame({"conversion_rate": [1, None, 0]}))


def test_binary_must_be_zero_or_one():
    with pytest.raises(ValueError, match="only 0 or 1"):
        metric_values(reg.get("conversion_rate"), pd.DataFrame({"conversion_rate": [0, 2]}))


def test_negative_continuous_rejected():
    with pytest.raises(ValueError, match="negative"):
        metric_values(reg.get("revenue_per_user"), pd.DataFrame({"revenue_per_user": [1.0, -1.0]}))


def test_needs_two_users():
    with pytest.raises(ValueError, match="at least 2"):
        compute_metric(reg.get("conversion_rate"), pd.DataFrame({"conversion_rate": [1]}))
