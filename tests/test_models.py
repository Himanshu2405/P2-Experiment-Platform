import numpy as np
import pandas as pd
import pytest

from p2.simulator.models import PRODUCTS, load_params
from p2.simulator.universe import generate_universe, plan_as_of


@pytest.fixture(scope="module")
def cohort():
    u = generate_universe(200_000, seed=3)
    rng = np.random.default_rng(1)
    anchor = pd.Timestamp("2026-01-15") + pd.to_timedelta(rng.integers(0, 86400, len(u.users)), unit="s")
    c = pd.DataFrame({"user_id": u.users.user_id, "anchor_ts": anchor})
    c["plan_tier"] = plan_as_of(u.plan_history, u.users.user_id, pd.Series([pd.Timestamp("2026-01-15")] * len(c)))
    c["returning"] = rng.random(len(c)) < 0.29
    c["tenure_days"] = (pd.Timestamp("2026-01-15") - pd.to_datetime(u.users.signup_date)).dt.days.to_numpy()
    c["acquisition_channel"] = u.users.acquisition_channel.to_numpy()
    return c


@pytest.mark.parametrize("product", ["checkout", "email", "onboarding"])
def test_sampled_outcomes_match_closed_form_truth(cohort, product):
    cls, truth_fn, gen_fn = PRODUCTS[product]
    p = load_params(product)
    act = gen_fn(p, cohort, np.random.default_rng(7), "t-")
    truth = truth_fn(p, cohort)
    for metric, expected in truth.items():
        col = act.outcomes[metric]
        se = col.std(ddof=1) / np.sqrt(len(col))
        assert abs(col.mean() - expected) < 4.5 * se, f"{product}.{metric}: {col.mean():.5f} vs {expected:.5f}"


@pytest.mark.parametrize("product", ["checkout", "email", "onboarding"])
def test_planted_lift_is_exact_in_truth(cohort, product):
    cls, truth_fn, _ = PRODUCTS[product]
    p = load_params(product)
    key = {"checkout": "p_order_given_begin", "email": "p_click_given_open", "onboarding": "p_activate_given_project"}[product]
    base, var = truth_fn(p, cohort), truth_fn(p.with_multipliers({key: 1.1}), cohort)
    changed = {m for m in base if abs(var[m] / base[m] - 1) > 1e-9}
    assert changed and all(var[m] > base[m] for m in changed)


def test_truth_identities(cohort):
    p = load_params("checkout")
    t = PRODUCTS["checkout"][1](p, cohort)
    assert t["revenue_per_user"] == pytest.approx(t["orders_per_user"] * p.mean_order_value)
    assert t["conversion_rate"] < t["add_to_cart_rate"]
    assert t["refund_rate"] < t["conversion_rate"]


def test_all_generated_activity_lies_inside_the_day_window(cohort):
    """Activity falls on the anchor's date through six days later, as the user-day contract counts."""
    sub = cohort.sample(20_000, random_state=1)
    sub.loc[sub.index[:2000], "anchor_ts"] = sub.anchor_ts.dt.normalize() + pd.Timedelta(hours=23, minutes=50)  # late-day anchors
    anchors = sub.set_index("user_id").anchor_ts
    for product, cols in {"checkout": {"checkout_events": "event_ts", "checkout_orders": "order_ts"},
                          "email": {"email_sends": "sent_ts", "email_events": "event_ts"},
                          "onboarding": {"onboarding_events": "event_ts", "payments": "paid_ts"}}.items():
        act = PRODUCTS[product][2](load_params(product), sub, np.random.default_rng(2), "t-")
        for table, col in cols.items():
            df = act.tables[table]
            day = (df[col].dt.normalize() - df.user_id.map(anchors).dt.normalize()).dt.days
            assert day.min() >= 0 and day.max() <= 6, f"{table}: days {day.min()} to {day.max()}"
            assert (df[col] >= df.user_id.map(anchors)).all()


def test_checkout_logic_constraints(cohort):
    act = PRODUCTS["checkout"][2](load_params("checkout"), cohort.head(100_000), np.random.default_rng(3), "t-")
    ev, orders, out = act.tables["checkout_events"], act.tables["checkout_orders"], act.outcomes.set_index("user_id")
    assert ev.event_id.is_unique and orders.order_id.is_unique
    buyers = set(orders.user_id)
    carts = set(ev.loc[ev.event_type == "add_to_cart", "user_id"])
    begins = set(ev.loc[ev.event_type == "begin_checkout", "user_id"])
    assert buyers <= begins <= carts
    done = orders[orders.status == "completed"]
    assert orders.loc[orders.refund_ts.notna(), "status"].eq("completed").all()
    assert (orders.refund_ts.dropna() > orders.loc[orders.refund_ts.notna(), "order_ts"]).all()
    assert out.conversion_rate.sum() == done.user_id.nunique()


def test_effects_that_break_probabilities_are_rejected(cohort):
    with pytest.raises(ValueError, match="outside|leave"):
        PRODUCTS["checkout"][1](load_params("checkout").with_multipliers({"p_begin_given_add": 2.0}), cohort)
    with pytest.raises(ValueError, match="unknown"):
        load_params("email").with_multipliers({"nope": 1.1})


def test_onboarding_payments_create_plan_changes(cohort):
    act = PRODUCTS["onboarding"][2](load_params("onboarding"), cohort.head(50_000), np.random.default_rng(4), "t-")
    assert len(act.plan_changes) == len(act.tables["payments"]) > 0
    assert set(act.plan_changes.plan_tier) <= {"standard", "premium"}
