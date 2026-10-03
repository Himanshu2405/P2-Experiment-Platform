"""R1 exit checks against a throwaway BigQuery environment: the nine raw tables land, the staging views are
clean, and the planted mess is provably handled by staging views plus a whole-day window."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from p2.simulator.service import load_context, seed_universe, simulate_experiment
from p2.simulator.universe import plan_as_of

pytestmark = pytest.mark.bq


@pytest.fixture(scope="module")
def world(wh):
    seed_universe(wh, 20_000, seed=3)
    ctx = load_context(wh)
    sims = {
        "checkout": simulate_experiment(wh, "r1-chk", "checkout", 5000, 7, {"p_order_given_begin": 1.2}, seed=11, ctx=ctx),
        "email": simulate_experiment(wh, "r1-eml", "email", 5000, 7, {"p_click_given_open": 1.3}, seed=12, ctx=ctx),
        "onboarding": simulate_experiment(wh, "r1-onb", "onboarding", 3000, 7, {"p_activate_given_project": 1.15}, seed=13),
    }
    return wh, ctx, sims


def count(wh, sql, **params):
    return int(wh.query_df(sql, params).iloc[0, 0])


def test_datasets_tables_and_views_exist(world):
    wh = world[0]
    assert {t.table_id for t in wh.client.list_tables(wh.raw)} == set(wh.sources.ids())
    views = {t.table_id for t in wh.client.list_tables(wh.staging)}
    assert views == {s.staging_name for s in wh.sources}
    assert all(t.table_type == "VIEW" for t in wh.client.list_tables(wh.staging))
    for ds in (wh.analytics, wh.app):
        wh.client.get_dataset(ds)


def test_partitioning_and_clustering_follow_the_registry(world):
    wh = world[0]
    for s in wh.sources:
        t = wh.client.get_table(f"{wh.raw}.{s.source_id}")
        assert list(t.clustering_fields or []) == list(s.cluster), s.source_id
        assert bool(t.time_partitioning) == bool(s.partition), s.source_id
        names = [f.name for f in t.schema]
        assert names == s.column_names


def test_ensure_is_idempotent(world):
    world[0].ensure()


def test_raw_has_duplicates_but_staging_keys_are_unique(world):
    wh = world[0]
    for s in wh.sources:
        key = ", ".join(s.dedupe_key)
        raw_dupes = count(wh, f"SELECT COUNT(*) - COUNT(DISTINCT TO_JSON_STRING(STRUCT({key}))) FROM `{wh.raw}.{s.source_id}`")
        stg_dupes = count(wh, f"SELECT COUNT(*) - COUNT(DISTINCT TO_JSON_STRING(STRUCT({key}))) FROM `{wh.staging}.{s.staging_name}`")
        assert stg_dupes == 0, s.source_id
        if s.source_id in {"checkout_events", "checkout_orders", "email_events", "email_sends", "onboarding_events", "exp_assignments", "payments"}:
            assert raw_dupes > 0, f"{s.source_id} should contain planted duplicates"


def test_latest_order_version_wins(world):
    wh = world[0]
    superseded = count(wh, f"""SELECT COUNT(*) FROM (SELECT order_id FROM `{wh.raw}.checkout_orders`
                               GROUP BY order_id HAVING COUNT(DISTINCT status) > 1 OR COUNT(DISTINCT refund_ts IS NULL) > 1)""")
    assert superseded > 0
    stale = count(wh, f"""SELECT COUNT(*) FROM `{wh.staging}.stg_checkout_orders` s JOIN
                          (SELECT order_id, MAX(updated_at) mx FROM `{wh.raw}.checkout_orders` GROUP BY order_id) r USING (order_id)
                          WHERE s.updated_at < r.mx""")
    assert stale == 0


def test_universe_shape(world):
    wh = world[0]
    users = wh.query_df(f"SELECT * FROM `{wh.staging}.stg_users` WHERE STARTS_WITH(user_id, 'uni-')")
    assert len(users) == 20_000
    assert 0.02 < users.region.isna().mean() < 0.045 and 0.03 < users.industry.isna().mean() < 0.07
    plans = wh.query_df(f"SELECT user_id, plan_tier FROM `{wh.staging}.stg_plan_history` WHERE STARTS_WITH(user_id, 'uni-')")
    assert 0.005 < 1 - plans.user_id.nunique() / 20_000 < 0.04  # about 2% have no plan history
    assert set(plans.plan_tier) == {"free", "standard", "premium"}


def test_plan_as_of_entry_in_sql_matches_python(world):
    wh, ctx, _ = world
    sample = ctx.users.user_id.sample(3000, random_state=1).reset_index(drop=True)
    dates = pd.Series(pd.to_datetime(["2025-09-15"] * 1500 + ["2025-12-31"] * 1500))
    py = plan_as_of(ctx.plan_history, sample, dates)
    sql = f"""
        SELECT user_id, ARRAY_AGG(plan_tier ORDER BY effective_date DESC LIMIT 1)[OFFSET(0)] AS plan
        FROM `{wh.staging}.stg_plan_history` WHERE effective_date <= @d AND STARTS_WITH(user_id, 'uni-') GROUP BY user_id"""
    for d, sl in ((date(2025, 9, 15), slice(0, 1500)), (date(2025, 12, 31), slice(1500, 3000))):
        sqlres = wh.query_df(sql, {"d": d}).set_index("user_id")["plan"]
        ids = sample[sl]
        got = ids.map(sqlres).fillna("Unknown").reset_index(drop=True)
        assert (got == py[sl].reset_index(drop=True)).all()


def user_metric(wh, eid, inner, agg):
    """An author's user-day query, run through the platform's window rule on staging views."""
    sql = f"""
    WITH cohort AS (SELECT user_id, DATE(assigned_at) AS d0 FROM `{wh.staging}.stg_exp_assignments` WHERE experiment_id = @e),
    m AS ({wh.resolve(inner)})
    SELECT c.user_id, COALESCE({agg}(m.value), 0) AS value
    FROM cohort c LEFT JOIN m ON m.user_id = c.user_id AND m.metric_date BETWEEN c.d0 AND DATE_ADD(c.d0, INTERVAL 6 DAY)
    GROUP BY c.user_id"""
    return wh.query_df(sql, {"e": eid}).set_index("user_id")["value"]


ORD = "FROM {{ checkout_orders }} WHERE status = 'completed'"
CHECKOUT = {
    "conversion_rate": (f"SELECT user_id, DATE(order_ts) AS metric_date, 1 AS value {ORD} GROUP BY 1, 2", "MAX"),
    "orders_per_user": (f"SELECT user_id, DATE(order_ts) AS metric_date, COUNT(*) AS value {ORD} GROUP BY 1, 2", "SUM"),
    "revenue_per_user": (f"SELECT user_id, DATE(order_ts) AS metric_date, SUM(order_value) AS value {ORD} GROUP BY 1, 2", "SUM"),
    "refund_rate": (f"SELECT user_id, DATE(refund_ts) AS metric_date, 1 AS value {ORD} AND refund_ts IS NOT NULL GROUP BY 1, 2", "MAX"),
    "add_to_cart_rate": ("SELECT user_id, DATE(event_ts) AS metric_date, 1 AS value FROM {{ checkout_events }} WHERE event_type = 'add_to_cart' GROUP BY 1, 2", "MAX"),
}
EVT = "FROM {{ email_events }} WHERE event_type = '%s'"
EMAIL = {
    "open_rate": (f"SELECT user_id, DATE(event_ts) AS metric_date, 1 AS value {EVT % 'open'} GROUP BY 1, 2", "MAX"),
    "click_rate": (f"SELECT user_id, DATE(event_ts) AS metric_date, 1 AS value {EVT % 'click'} GROUP BY 1, 2", "MAX"),
    "unsubscribe_rate": (f"SELECT user_id, DATE(event_ts) AS metric_date, 1 AS value {EVT % 'unsubscribe'} GROUP BY 1, 2", "MAX"),
    "bounce_rate": (f"SELECT user_id, DATE(event_ts) AS metric_date, 1 AS value {EVT % 'bounce'} GROUP BY 1, 2", "MAX"),
    "emails_per_user": ("SELECT user_id, DATE(sent_ts) AS metric_date, COUNT(*) AS value FROM {{ email_sends }} GROUP BY 1, 2", "SUM"),
}
STEP = "SELECT user_id, DATE(event_ts) AS metric_date, 1 AS value FROM {{ onboarding_events }} WHERE step = '%s' GROUP BY 1, 2"
ONBOARDING = {
    "profile_completion_rate": (STEP % "profile_completed", "MAX"),
    "invited_teammate_rate": (STEP % "invited_teammate", "MAX"),
    "activation_rate": (STEP % "activated", "MAX"),
    "paid_conversion_rate": ("SELECT user_id, DATE(paid_ts) AS metric_date, 1 AS value FROM {{ payments }} GROUP BY 1, 2", "MAX"),
    "paid_revenue_per_user": ("SELECT user_id, DATE(paid_ts) AS metric_date, SUM(amount) AS value FROM {{ payments }} GROUP BY 1, 2", "SUM"),
}


@pytest.mark.parametrize("product,eid,metrics", [("checkout", "r1-chk", CHECKOUT), ("email", "r1-eml", EMAIL),
                                                 ("onboarding", "r1-onb", ONBOARDING)])
def test_staging_plus_day_window_recovers_the_clean_outcomes_despite_the_mess(world, product, eid, metrics):
    wh, _, sims = world
    sim = sims[product]
    out = sim.outcomes.set_index("user_id")
    assert len(out) == len(sim.cohort)
    for metric, (sql, agg) in metrics.items():
        got = user_metric(wh, eid, sql, agg)
        assert len(got) == len(out), metric  # every assigned user appears once (assignment duplicates are gone)
        assert np.allclose(got.reindex(out.index).astype(float), out[metric].astype(float), atol=1e-6), metric


def test_assignments_landed_once_per_user_with_planted_duplicates(world):
    wh = world[0]
    raw = count(wh, f"SELECT COUNT(*) FROM `{wh.raw}.exp_assignments` WHERE experiment_id = 'r1-chk'")
    stg = count(wh, f"SELECT COUNT(*) FROM `{wh.staging}.stg_exp_assignments` WHERE experiment_id = 'r1-chk'")
    assert raw > stg == 5000


def test_resimulating_an_experiment_replaces_its_rows(world):
    wh, ctx, _ = world
    q = f"SELECT COUNT(*) FROM `{wh.raw}.checkout_events` WHERE STARTS_WITH(event_id, 'r1-chk-')"
    before = count(wh, q)
    simulate_experiment(wh, "r1-chk", "checkout", 5000, 7, {"p_order_given_begin": 1.2}, seed=11, ctx=ctx)
    assert count(wh, q) == before


def test_truth_is_exact_and_documented(world):
    sims = world[2]
    t = sims["checkout"].truth
    assert t["add_to_cart_rate"]["relative_lift"] == pytest.approx(0)
    assert t["conversion_rate"]["relative_lift"] > 0.15 and t["refund_rate"]["relative_lift"] > 0.15
    e = sims["email"].truth
    assert e["click_rate"]["relative_lift"] > 0.2 and e["open_rate"]["relative_lift"] == pytest.approx(0)
    o = sims["onboarding"].truth
    assert o["activation_rate"]["relative_lift"] == pytest.approx(0.15) and o["profile_completion_rate"]["relative_lift"] == pytest.approx(0)


def test_onboarding_experiment_creates_new_users_with_plan_history(world):
    wh = world[0]
    n = count(wh, f"SELECT COUNT(*) FROM `{wh.staging}.stg_users` WHERE STARTS_WITH(user_id, 'r1-onb-')")
    assert n == 3000
    free = count(wh, f"SELECT COUNT(*) FROM `{wh.staging}.stg_plan_history` WHERE STARTS_WITH(user_id, 'r1-onb-') AND plan_tier = 'free'")
    assert free == 3000


def test_returning_buyers_have_prior_orders_before_the_experiment(world):
    wh, ctx, sims = world
    c = sims["checkout"].cohort
    assert 0.1 < c["returning"].mean() < 0.5
    assert set(c.plan_tier) >= {"free", "standard"}
    prior = count(wh, f"SELECT COUNT(DISTINCT user_id) FROM `{wh.staging}.stg_checkout_orders` WHERE order_ts < TIMESTAMP '2025-09-20' AND STARTS_WITH(order_id, 'uni-')")
    assert prior > 1000

