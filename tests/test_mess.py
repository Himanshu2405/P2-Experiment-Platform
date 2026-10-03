import numpy as np
import pandas as pd
import pytest

from p2.simulator.mess import add_mess
from p2.simulator.models import PRODUCTS, load_params
from p2.simulator.universe import generate_universe, plan_as_of

W = pd.Timedelta(days=7)


@pytest.fixture(scope="module")
def cohort():
    u = generate_universe(60_000, seed=5)
    rng = np.random.default_rng(2)
    anchor = pd.Timestamp("2026-02-01") + pd.to_timedelta(rng.integers(0, 10 * 86400, len(u.users)), unit="s")
    c = pd.DataFrame({"user_id": u.users.user_id, "anchor_ts": anchor})
    c["plan_tier"] = plan_as_of(u.plan_history, u.users.user_id, pd.Series([pd.Timestamp("2026-02-01")] * len(c)))
    c["returning"] = rng.random(len(c)) < 0.29
    c["tenure_days"] = 100
    c["acquisition_channel"] = u.users.acquisition_channel.to_numpy()
    return c


def dedupe(df, key, order_col, ascending):
    return df.sort_values([order_col, "ingested_at"], ascending=[ascending, ascending]).drop_duplicates(key, keep="first")


def windowed(df, ts_col, anchors):
    """Whole-day window: the anchor's date through six days later."""
    a = df.user_id.map(anchors).dt.normalize()
    return df[(df[ts_col].dt.normalize() >= a) & (df[ts_col].dt.normalize() <= a + pd.Timedelta(days=6))]


def any_flag(df, users, mask=None):
    d = df if mask is None else df[mask]
    return pd.Series(users).isin(d.user_id).astype(int).to_numpy()


@pytest.mark.parametrize("pre", [True, False])
def test_checkout_mess_is_handled_by_dedupe_and_window(cohort, pre):
    act = PRODUCTS["checkout"][2](load_params("checkout"), cohort, np.random.default_rng(1), "t-")
    t = add_mess(act.tables, "checkout", cohort, np.random.default_rng(2), "t-", pre_anchor=pre)
    anchors = cohort.set_index("user_id").anchor_ts
    ev = windowed(dedupe(t["checkout_events"], "event_id", "event_ts", True), "event_ts", anchors)
    orders = windowed(dedupe(t["checkout_orders"], "order_id", "updated_at", False), "order_ts", anchors)
    done = orders[orders.status == "completed"]
    out = act.outcomes
    users = out.user_id
    assert (any_flag(ev, users, ev.event_type == "add_to_cart") == out.add_to_cart_rate).all()
    assert (any_flag(done, users) == out.conversion_rate).all()
    assert (any_flag(done, users, done.refund_ts.notna()) == out.refund_rate).all()
    assert np.allclose(users.map(done.groupby("user_id").order_value.sum()).fillna(0), out.revenue_per_user)
    assert (users.map(done.groupby("user_id").size()).fillna(0) == out.orders_per_user).all()


def test_the_mess_is_really_there(cohort):
    act = PRODUCTS["checkout"][2](load_params("checkout"), cohort, np.random.default_rng(1), "t-")
    t = add_mess(act.tables, "checkout", cohort, np.random.default_rng(2), "t-", pre_anchor=True)
    ev, orders = t["checkout_events"], t["checkout_orders"]
    assert ev.event_id.duplicated().sum() > 0
    assert orders.order_id.duplicated().sum() > 0
    # naive handling (no dedupe, no window) gives different answers
    naive = orders[orders.status == "completed"].user_id.nunique()
    assert naive != act.outcomes.conversion_rate.sum()
    anchors = cohort.set_index("user_id").anchor_ts
    a = ev.user_id.map(anchors).dt.normalize()
    assert (ev.event_ts.dt.normalize() < a).sum() > 0 and (ev.event_ts.dt.normalize() > a + pd.Timedelta(days=6)).sum() > 0
    assert set(t["checkout_orders"].columns) >= {"ingested_at"}


def test_latest_order_version_wins(cohort):
    act = PRODUCTS["checkout"][2](load_params("checkout"), cohort, np.random.default_rng(1), "t-")
    t = add_mess(act.tables, "checkout", cohort, np.random.default_rng(2), "t-", pre_anchor=False)
    orders = t["checkout_orders"]
    refunded_ids = act.tables["checkout_orders"].query("refund_ts == refund_ts").order_id
    assert len(refunded_ids) > 0
    latest = dedupe(orders, "order_id", "updated_at", False).set_index("order_id")
    assert latest.loc[refunded_ids, "refund_ts"].notna().all()
    earlier = orders[orders.order_id.isin(refunded_ids) & orders.refund_ts.isna()]
    assert len(earlier) > 0  # the superseded version exists in the raw table


def test_email_mess_is_handled(cohort):
    act = PRODUCTS["email"][2](load_params("email"), cohort, np.random.default_rng(1), "t-")
    t = add_mess(act.tables, "email", cohort, np.random.default_rng(2), "t-", pre_anchor=True)
    anchors = cohort.set_index("user_id").anchor_ts
    ev = windowed(dedupe(t["email_events"], "event_id", "event_ts", True), "event_ts", anchors)
    sends = windowed(dedupe(t["email_sends"], "send_id", "sent_ts", True), "sent_ts", anchors)
    out, users = act.outcomes, act.outcomes.user_id
    for metric, et in (("open_rate", "open"), ("click_rate", "click"), ("unsubscribe_rate", "unsubscribe"), ("bounce_rate", "bounce")):
        assert (any_flag(ev, users, ev.event_type == et) == out[metric]).all(), metric
    assert (users.map(sends.groupby("user_id").size()).fillna(0) == out.emails_per_user).all()


def test_onboarding_mess_is_handled(cohort):
    act = PRODUCTS["onboarding"][2](load_params("onboarding"), cohort, np.random.default_rng(1), "t-")
    t = add_mess(act.tables, "onboarding", cohort, np.random.default_rng(2), "t-", pre_anchor=False)
    anchors = cohort.set_index("user_id").anchor_ts
    ev = windowed(dedupe(t["onboarding_events"], "event_id", "event_ts", True), "event_ts", anchors)
    pay = windowed(dedupe(t["payments"], "payment_id", "paid_ts", True), "paid_ts", anchors)
    out, users = act.outcomes, act.outcomes.user_id
    assert (any_flag(ev, users, ev.step == "activated") == out.activation_rate).all()
    assert (any_flag(ev, users, ev.step == "profile_completed") == out.profile_completion_rate).all()
    assert (any_flag(pay, users) == out.paid_conversion_rate).all()
    assert np.allclose(users.map(pay.groupby("user_id").amount.sum()).fillna(0), out.paid_revenue_per_user)
    assert t["payments"].payment_id.duplicated().sum() >= 0
