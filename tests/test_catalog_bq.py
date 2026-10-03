"""Catalog and pipeline checks on a throwaway BigQuery environment: every seed item passes the automated checks, bad
queries are rejected with clear reasons, the step-based pipeline builds exactly the right per-user table with filters and
dimensions despite the planted mess, and RUN recovers the planted effects with the right statistics."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from p2.catalog.checks import SqlChecker
from p2.pipeline.runner import PipelineRunner
from p2.services import errors
from p2.services.platform import Platform
from p2.stats.plan import MetricPlan
from p2.simulator.service import load_context, seed_universe, simulate_experiment
from p2.store.bigquery import BigQueryStore
from p2.warehouse.sources import load_sources

pytestmark = pytest.mark.bq


@pytest.fixture(scope="module")
def world(wh):
    seed_universe(wh, 20_000, seed=3)
    # Landed activity for the seed items' checks to run on. Dated mid-2025 so it never falls inside the January 2026 test windows.
    for eid, product, n in (("seed-chk", "checkout", 6000), ("seed-eml", "email", 6000), ("seed-onb", "onboarding", 4000)):
        simulate_experiment(wh, eid, product, n, 7, {}, seed=2, start=date(2025, 6, 1))
    ctx = load_context(wh)
    store = BigQueryStore(wh)
    store.ensure()
    plat = Platform(store, load_sources(), checker=SqlChecker(wh), runner=PipelineRunner(wh))
    plat.bootstrap()
    return wh, plat, ctx


def who(p, u):
    return p.get_actor(u)


def item(kind="metric", sql="", product="checkout", **kw):
    base = dict(item_id="probe", version=1, kind=kind, product_id=product, sql=sql, value_type="binary" if kind == "metric" else None,
                aggregation="max" if kind == "metric" else None, default_value="Unknown" if kind == "dimension" else None)
    return {**base, **kw}


def failed(report):
    return {c["name"]: c["detail"] for c in report["checks"] if c["status"] == "fail"}


# ---- the seed passes its own checks -------------------------------------------------------------------
def test_every_seed_item_passes_the_automated_checks_on_real_data(world):
    _, plat, _ = world
    rows = plat.list_items()
    assert len(rows) == 24
    with ThreadPoolExecutor(6) as pool:
        reports = list(pool.map(plat.checker.check, rows))
    bad = {r["item_id"]: failed(rep) for r, rep in zip(rows, reports) if not rep["passed"]}
    assert not bad, bad
    by = {r["item_id"]: rep for r, rep in zip(rows, reports)}
    assert by["conversion_rate"]["stats"]["n_users"] > 100 and by["conversion_rate"]["stats"]["max_value"] == 1
    assert by["plan_tier"]["stats"]["n_distinct_values"] <= 3 and "free" in by["plan_tier"]["stats"]["top_values"]
    assert set(by["tenure_bucket"]["stats"]["top_values"]) == {"0-29 days", "30-89 days", "90-179 days", "180+ days"}
    assert 0.3 < by["region_us"]["stats"]["share_passing"] < 0.6  # about 45% of users are in the US


# ---- bad queries are rejected, with the reason ---------------------------------------------------------
BAD = [
    ("unknown source", item(sql="SELECT user_id, DATE(ts) AS metric_date, 1 AS value FROM {{ nope }}"), "sources", "unknown source"),
    ("another product's source", item(sql="SELECT user_id, DATE(event_ts) AS metric_date, 1 AS value FROM {{ email_events }}"), "sources", "not available"),
    ("jinja logic", item(sql="SELECT {% if 1 %}1{% endif %}"), "sources", "placeholders"),
    ("syntax error", item(sql="SELEC user_id FROM {{ checkout_orders }}"), "valid_sql", "rejected"),
    ("unknown column", item(sql="SELECT user_id, nope AS metric_date, 1 AS value FROM {{ checkout_orders }}"), "valid_sql", "rejected"),
    ("several statements", item(sql="SELECT 1; SELECT 2"), "single_select", "SCRIPT"),
    ("wrong columns", item(sql="SELECT user_id, 1 AS value FROM {{ checkout_orders }}"), "columns", "exactly"),
    ("wrong types", item(sql="SELECT user_id, order_ts AS metric_date, 1 AS value FROM {{ checkout_orders }}"), "columns", "wrong column types"),
    ("duplicate keys", item(sql="SELECT user_id, DATE(order_ts) AS metric_date, 1 AS value FROM {{ checkout_orders }}"), "unique_keys", "duplicate"),
    ("non-binary value", item(sql="SELECT user_id, DATE(order_ts) AS metric_date, 2 AS value FROM {{ checkout_orders }} GROUP BY 1, 2"), "value_range", "0 or 1"),
    ("negative continuous", item(sql="SELECT user_id, DATE(order_ts) AS metric_date, -1.0 AS value FROM {{ checkout_orders }} GROUP BY 1, 2",
                                value_type="continuous", aggregation="sum"), "value_range", "negative"),
    ("future date", item(sql="SELECT user_id, DATE_ADD(CURRENT_DATE(), INTERVAL 2 DAY) AS metric_date, 1 AS value FROM {{ checkout_orders }} GROUP BY 1, 2"), "dates", "future"),
    ("null value", item(sql="SELECT user_id, DATE(order_ts) AS metric_date, NULLIF(1, 1) AS value FROM {{ checkout_orders }} GROUP BY 1, 2"), "no_nulls", "nulls"),
    ("empty", item(sql="SELECT user_id, DATE(order_ts) AS metric_date, 1 AS value FROM {{ checkout_orders }} WHERE FALSE"), "not_empty", "no rows"),
    ("too many dimension values", item(kind="dimension", sql="SELECT user_id, signup_date AS effective_date, user_id AS value FROM {{ users }}", product="shared"),
     "cardinality", "at most 20"),
    ("filter not 0 or 1", item(kind="filter", sql="SELECT user_id, signup_date AS effective_date, 5 AS value FROM {{ users }}", product="shared"),
     "value_range", "not 0 or 1"),
    ("filter of the wrong type", item(kind="filter", sql="SELECT user_id, signup_date AS effective_date, 'yes' AS value FROM {{ users }}", product="shared"),
     "columns", "wrong column types"),
]


@pytest.mark.parametrize("label,it,check,text", BAD, ids=[b[0] for b in BAD])
def test_bad_queries_are_rejected_with_the_reason(world, label, it, check, text):
    report = world[1].checker.check(it)
    assert not report["passed"]
    assert check in failed(report), report["checks"]
    assert text in failed(report)[check], failed(report)


def test_direct_table_references_cannot_bypass_the_staging_views(world):
    wh, plat, _ = world
    sql = f"SELECT user_id, DATE(order_ts) AS metric_date, 1 AS value FROM `{wh.raw}.checkout_orders` GROUP BY 1, 2"
    assert "direct table references" in failed(plat.checker.check(item(sql=sql)))["sources"]
    unquoted = f"SELECT user_id, DATE(order_ts) AS metric_date, 1 AS value FROM {wh.raw.split('.')[-1]}.checkout_orders GROUP BY 1, 2"
    assert "sources" in failed(plat.checker.check(item(sql=unquoted)))


def test_the_cost_cap_is_enforced(world):
    wh, plat, _ = world
    tight = SqlChecker(wh, cost_cap_bytes=1)
    ok = next(i for i in plat.list_items() if i["item_id"] == "conversion_rate")
    assert "cost_cap" in failed(tight.check(ok))


def test_a_passing_report_has_every_group_of_checks(world):
    report = world[1].checker.check(next(i for i in world[1].list_items() if i["item_id"] == "open_rate"))
    names = [c["name"] for c in report["checks"]]
    assert names == ["sources", "valid_sql", "single_select", "allowed_tables", "cost_cap", "columns", "not_empty", "no_nulls",
                     "unique_keys", "dates", "value_range"]
    assert all(c["status"] == "pass" for c in report["checks"])


# ---- a new metric through the whole lifecycle, on real data --------------------------------------------
def test_propose_check_certify_and_use_a_new_metric_end_to_end(world):
    _, plat, _ = world
    sql = ("SELECT user_id, DATE(order_ts) AS metric_date, 1 AS value FROM {{ checkout_orders }} "
           "WHERE status = 'completed' AND order_value > 150 GROUP BY user_id, metric_date")
    plat.propose_item(who(plat, "priya"), "metric", "big_order_rate", "checkout", "Big order rate", "Share of users with an order over 150",
                      sql, value_type="binary", aggregation="max", good_direction="higher", format="percent")
    report = plat.run_checks(who(plat, "priya"), "big_order_rate", 1)
    assert report["passed"], failed(report)
    plat.submit_for_review(who(plat, "priya"), "big_order_rate", 1)
    with pytest.raises(errors.PermissionDenied):
        plat.certify(who(plat, "priya"), "big_order_rate", 1)
    plat.certify(who(plat, "admin"), "big_order_rate", 1)
    assert "big_order_rate" in plat.metric_registry().ids()


# ---- the pipeline ---------------------------------------------------------------------------------------
LAUNCH = date(2026, 1, 1)
ASSIGN_DAYS = 2
RUNTIME = ASSIGN_DAYS + 6          # assignments on 2 days plus 6 days of activity for the last assigned user


def plan(reg, metric, role="primary", baseline=0.0374, effect=0.2, kind="relative", sided="two-sided", std=None):
    return MetricPlan(reg.get(metric), role, baseline, effect, kind, 0.05, 0.8, "one-sided" if role == "guardrail" else sided, std)


def bucket(days: int) -> str:
    return "0-29 days" if days < 30 else "30-89 days" if days < 90 else "90-179 days" if days < 180 else "180+ days"


@pytest.fixture(scope="module")
def built(world):
    wh, plat, ctx = world
    priya = who(plat, "priya")
    reg = plat.metric_registry()
    plat.create_experiment(priya, "r3-chk", "checkout", "Banner", "Lifts conversion", LAUNCH, RUNTIME)
    plat.save_design(priya, "r3-chk", plan(reg, "conversion_rate", effect=0.5), [plan(reg, "refund_rate", "guardrail", 0.0047, 2.0)],
                     ["add_to_cart_rate", "orders_per_user"], dimension_ids=["plan_tier", "customer_type", "tenure_bucket", "region"],
                     filter_ids=["region_us"])
    sim = simulate_experiment(wh, "r3-chk", "checkout", 16000, ASSIGN_DAYS, {"p_order_given_begin": 1.5}, seed=21, start=LAUNCH, ctx=ctx)
    plat.save_ground_truth(priya, "r3-chk", sim.truth)
    result = plat.run_analysis(priya, "r3-chk")
    return plat, sim, result, plat.experiment_frame("r3-chk").set_index("user_id"), ctx


def test_the_audience_is_everyone_and_a_filter_is_a_view_with_the_right_users(built):
    plat, sim, result, frame, ctx = built
    us = set(ctx.users.loc[ctx.users.country == "US", "user_id"])
    cohort = set(sim.cohort.user_id)
    assert set(frame.index) == cohort and result.audience_users == result.cohort_users == len(frame) == 16000    # the filter does not cut anyone
    assert set(frame.index[frame.region_us == 1]) == cohort & us and set(frame.region_us) == {0, 1}                # it only marks who passes
    assert result.n_control + result.n_variant == 16000
    slice_rows = [r for r in plat.segment_results("r3-chk") if (r["dimension_id"], r["filter_id"], r["item_id"]) == ("", "region_us", "conversion_rate")]
    assert len(slice_rows) == 1 and slice_rows[0]["n_control"] + slice_rows[0]["n_variant"] == len(cohort & us)   # the filter view counts exactly those users
    assert 0.35 < len(cohort & us) / 16000 < 0.55
    both = [r for r in plat.segment_results("r3-chk") if (r["filter_id"], r["item_id"]) == ("region_us", "conversion_rate") and r["dimension_id"] == "plan_tier"]
    assert sum(r["n_control"] + r["n_variant"] for r in both) == len(cohort & us)                                 # a segment inside the filter splits the same users
    everyone = plat.latest_results("r3-chk")[0]
    assert everyone["n_control"] + everyone["n_variant"] == 16000                                                 # the main result still covers everyone


def test_metrics_equal_the_clean_outcomes_despite_the_mess(built):
    _, sim, _, frame, _ = built
    out = sim.outcomes.set_index("user_id").reindex(frame.index)
    for metric in ("conversion_rate", "refund_rate", "add_to_cart_rate", "orders_per_user"):
        assert np.allclose(frame[metric].astype(float), out[metric].astype(float)), metric
    assert (frame.arm == sim.cohort.set_index("user_id").arm.reindex(frame.index)).all()


def test_dimensions_are_the_state_at_entry(built):
    _, sim, _, frame, ctx = built
    c = sim.cohort.set_index("user_id").reindex(frame.index)
    assert (frame.plan_tier == c.plan_tier).all()
    assert (frame.customer_type == np.where(c.returning, "returning", "new")).all()
    assert (frame.tenure_bucket == c.tenure_days.map(bucket)).all()
    region = ctx.users.set_index("user_id").region.reindex(frame.index).fillna("Unknown")
    assert (frame.region == region).all()
    assert set(frame.customer_type) == {"new", "returning"} and len(set(frame.plan_tier)) >= 2


def test_steps_are_small_logged_and_tables_expire_correctly(built, world):
    wh, plat, *_ = world
    plat, *_ = built
    job = plat.list_jobs("r3-chk")[0]
    steps = {s["step"]: s for s in plat.job_steps(job["job_id"])}
    assert {"gates_before", "cohort", "attr_region_us", "attr_plan_tier", "attr_customer_type", "attr_tenure_bucket", "attr_region",
            "audience", "m_conversion_rate", "m_refund_rate", "m_add_to_cart_rate", "m_orders_per_user", "final", "gates_after",
            "stats", "daily", "segments"} == set(steps)
    assert steps["cohort"]["row_count"] == 16000 and steps["audience"]["row_count"] == steps["final"]["row_count"]
    assert all(s["status"] == "ok" for s in steps.values())
    from p2.pipeline import sqlgen
    cohort = wh.client.get_table(sqlgen.table(wh.analytics, "r3-chk", "cohort"))
    final = wh.client.get_table(sqlgen.table(wh.analytics, "r3-chk", "final"))
    assert cohort.expires is not None and final.expires is None


# ---- RUN: the statistics, computed in the warehouse --------------------------------------------------------
def test_warehouse_summary_statistics_equal_a_pandas_calculation(built, world):
    plat, _, result, frame, _ = built
    summary = plat.runner.summary_stats(result.final_table, ["conversion_rate", "orders_per_user"])
    for metric in ("conversion_rate", "orders_per_user"):
        for arm in ("control", "variant"):
            values = frame.loc[frame.arm == arm, metric].astype(float)
            got = summary[metric][arm]
            assert got.n == len(values) and got.mean == pytest.approx(values.mean()) and got.var == pytest.approx(values.var(ddof=1))


def test_results_recover_the_planted_effect_with_the_right_verdicts(built):
    plat, sim, _, frame, _ = built
    assert plat.get_experiment("r3-chk")["status"] == "Analyzed"
    res = {r["item_id"]: r for r in plat.latest_results("r3-chk")}
    assert list(res) == ["conversion_rate", "refund_rate", "add_to_cart_rate", "orders_per_user"]
    truth = plat.load_ground_truth("r3-chk")
    conv = res["conversion_rate"]
    true_diff = truth["conversion_rate"]["variant"] - truth["conversion_rate"]["control"]
    assert conv["verdict"] == "Significant improvement" and conv["p_value"] < 0.01
    assert conv["ci_low"] < true_diff < conv["ci_high"]                                   # the interval covers the exact planted effect
    assert conv["relative_lift"] == pytest.approx(conv["difference"] / conv["mean_control"])
    assert conv["n_control"] + conv["n_variant"] == len(frame) and conv["achieved_power"] > 0.9
    assert res["refund_rate"]["verdict"] == "Passed" and res["refund_rate"]["params"]["effect_abs"] == pytest.approx(0.0094)
    assert res["add_to_cart_rate"]["role"] == "secondary" and res["add_to_cart_rate"]["achieved_power"] is None
    a2c = res["add_to_cart_rate"]
    assert a2c["ci_low"] < 0 < a2c["ci_high"]                                              # no true effect on add to cart: the interval covers zero


def test_rerunning_is_stable_and_keeps_a_history(built):
    plat, _, _, frame, _ = built
    first = {r["item_id"]: r["difference"] for r in plat.latest_results("r3-chk")}
    plat.run_analysis(who(plat, "priya"), "r3-chk")
    again = {r["item_id"]: r["difference"] for r in plat.latest_results("r3-chk")}
    assert again == pytest.approx(first)
    assert len({r["run_at"] for r in plat.store.select("results", {"experiment_id": "r3-chk"})}) == 2
    pd.testing.assert_frame_equal(frame.drop(columns="assigned_date").sort_index(),
                                  plat.experiment_frame("r3-chk").set_index("user_id").drop(columns="assigned_date").reindex(frame.index).sort_index())


def test_a_decision_can_be_recorded_on_bigquery(built):
    plat, *_ = built
    row = plat.record_decision(who(plat, "priya"), "r3-chk", "ship", "Conversion up, refunds within the margin")
    assert row["status"] == "Decided" and row["decision"] == "ship" and row["decided_at"] is not None


def test_email_and_onboarding_experiments_run_too(world):
    wh, plat, ctx = world
    marcus, sofia = who(plat, "marcus"), who(plat, "sofia")
    reg = plat.metric_registry()
    plat.create_experiment(marcus, "r3-eml", "email", "Subject", "Lifts clicks", LAUNCH, RUNTIME)
    plat.save_design(marcus, "r3-eml", plan(reg, "click_rate", baseline=0.05, effect=0.5),
                     [plan(reg, "unsubscribe_rate", "guardrail", 0.006, 1.0)], ["open_rate", "bounce_rate", "emails_per_user"],
                     dimension_ids=["tenure_bucket"], filter_ids=["existing_user"])
    sim = simulate_experiment(wh, "r3-eml", "email", 6000, ASSIGN_DAYS, {"p_click_given_open": 1.5}, seed=22, start=LAUNCH, ctx=ctx)
    plat.run_analysis(marcus, "r3-eml")
    f = plat.experiment_frame("r3-eml").set_index("user_id")
    out = sim.outcomes.set_index("user_id").reindex(f.index)
    assert len(f) == 6000
    for m in ("click_rate", "open_rate", "unsubscribe_rate", "bounce_rate", "emails_per_user"):
        assert np.allclose(f[m].astype(float), out[m].astype(float)), m
    res = {r["item_id"]: r for r in plat.latest_results("r3-eml")}
    assert res["click_rate"]["verdict"] == "Significant improvement" and res["unsubscribe_rate"]["verdict"] == "Passed"

    plat.create_experiment(sofia, "r3-onb", "onboarding", "Checklist", "Lifts activation", LAUNCH, RUNTIME)
    plat.save_design(sofia, "r3-onb", plan(reg, "activation_rate", baseline=0.25, effect=0.3), [],
                     ["profile_completion_rate", "paid_conversion_rate", "paid_revenue_per_user"],
                     dimension_ids=["acquisition_channel", "plan_tier"], filter_ids=["paid_at_entry"])
    sim2 = simulate_experiment(wh, "r3-onb", "onboarding", 3000, ASSIGN_DAYS, {"p_activate_given_project": 1.4}, seed=23, start=LAUNCH)
    plat.run_analysis(sofia, "r3-onb")                      # new signups all start on the free plan, so nobody passes paid_at_entry
    assert not [r for r in plat.segment_results("r3-onb") if r["filter_id"] == "paid_at_entry"]      # that view has no users, so no rows
    assert len(plat.experiment_frame("r3-onb")) == 3000       # but the experiment still covers everyone
    g = plat.experiment_frame("r3-onb").set_index("user_id")
    o = sim2.outcomes.set_index("user_id").reindex(g.index)
    assert len(g) == 3000 and set(g.plan_tier) == {"free"}
    for m in ("activation_rate", "profile_completion_rate", "paid_conversion_rate", "paid_revenue_per_user"):
        assert np.allclose(g[m].astype(float), o[m].astype(float)), m


def test_a_no_effect_experiment_is_not_called_a_winner(world):
    """An A/A: the same model in both arms. The statistics must not see an effect (fixed seed, so this is deterministic)."""
    wh, plat, ctx = world
    priya = who(plat, "priya")
    reg = plat.metric_registry()
    plat.create_experiment(priya, "r3-aa", "checkout", "A/A", "No change", LAUNCH, RUNTIME)
    plat.save_design(priya, "r3-aa", plan(reg, "conversion_rate"), [], ["add_to_cart_rate"])
    sim = simulate_experiment(wh, "r3-aa", "checkout", 16000, ASSIGN_DAYS, {}, seed=31, start=LAUNCH, ctx=ctx)
    assert all(abs(t["relative_lift"]) < 1e-12 for t in sim.truth.values())
    plat.run_analysis(priya, "r3-aa")
    res = {r["item_id"]: r for r in plat.latest_results("r3-aa")}
    assert res["conversion_rate"]["verdict"] == "No significant difference" and res["conversion_rate"]["ci_low"] < 0 < res["conversion_rate"]["ci_high"]


# ---- gates -----------------------------------------------------------------------------------------------
def design_min(plat, eid, user="priya", product="checkout", metric="conversion_rate", launch=LAUNCH, runtime=14):
    p = plat.get_actor(user)
    plat.create_experiment(p, eid, product, "n", "h", launch, runtime)
    plat.save_design(p, eid, plan(plat.metric_registry(), metric, baseline=0.0374 if product == "checkout" else 0.05), [], [])


def land_assignments(wh, eid, rows):
    df = pd.DataFrame([(eid, u, arm, pd.Timestamp(t, tz="UTC"), pd.Timestamp(t, tz="UTC") + pd.Timedelta(hours=1)) for u, arm, t in rows],
                      columns=["experiment_id", "user_id", "arm", "assigned_at", "ingested_at"])
    wh.replace_rows("exp_assignments", df, "experiment_id = @e", {"e": eid})


def test_gate_no_assignments(world):
    _, plat, _ = world
    design_min(plat, "g-none")
    with pytest.raises(errors.InvalidInput, match="no assignments landed"):
        plat.run_analysis(who(plat, "priya"), "g-none")
    job = plat.list_jobs("g-none")[0]
    assert job["status"] == "failed" and {s["step"]: s["status"] for s in plat.job_steps(job["job_id"])} == {"gates_before": "failed"}


def test_gate_assignments_exist_but_outside_the_runtime_names_both_date_ranges(world):
    wh, plat, _ = world
    design_min(plat, "g-range", launch=date(2026, 3, 1), runtime=14)
    land_assignments(wh, "g-range", [("uni-u1", "control", "2026-01-01 10:00:00"), ("uni-u2", "variant", "2026-01-02 11:00:00")])
    with pytest.raises(errors.InvalidInput, match="no assignments between 2026-03-01 and 2026-03-14, but the log has 2 between 2026-01-01"):
        plat.run_analysis(who(plat, "priya"), "g-range")


def test_gates_one_arm_two_arms_for_one_user_still_running_and_not_launched(world):
    wh, plat, _ = world
    design_min(plat, "g-arm")
    land_assignments(wh, "g-arm", [("uni-u1", "control", "2026-01-01 10:00:00"), ("uni-u2", "control", "2026-01-01 11:00:00")])
    with pytest.raises(errors.InvalidInput, match="only one arm"):
        plat.run_analysis(who(plat, "priya"), "g-arm")
    design_min(plat, "g-two")
    land_assignments(wh, "g-two", [("uni-u1", "control", "2026-01-01 10:00:00"), ("uni-u1", "variant", "2026-01-01 10:00:00"),
                                    ("uni-u2", "variant", "2026-01-01 11:00:00")])
    with pytest.raises(errors.InvalidInput, match="more than one arm"):
        plat.run_analysis(who(plat, "priya"), "g-two")
    design_min(plat, "g-open")
    land_assignments(wh, "g-open", [("uni-u1", "control", "2026-01-01 10:00:00"), ("uni-u2", "variant", "2026-01-01 11:00:00")])
    with pytest.raises(errors.InvalidInput, match="still running until 2026-01-14"):
        plat.run_analysis(who(plat, "priya"), "g-open", as_of=datetime(2026, 1, 14, tzinfo=timezone.utc))   # the last day is not over yet
    assert plat.get_experiment("g-open")["status"] == "Designed"
    plat.refresh_monitor(who(plat, "priya"), "g-open", as_of=datetime(2026, 1, 15, tzinfo=timezone.utc))  # 2 users: enough to monitor, too few to test
    assert plat.get_experiment("g-open")["status"] == "Running"
    assert len(plat.experiment_frame("g-open")) == 2
    with pytest.raises(errors.InvalidInput, match="at least 2 users"):
        plat.run_analysis(who(plat, "priya"), "g-open", as_of=datetime(2026, 1, 15, tzinfo=timezone.utc))
    design_min(plat, "g-future", launch=date(2026, 6, 1))
    with pytest.raises(errors.InvalidInput, match="has not launched yet"):
        plat.run_analysis(who(plat, "priya"), "g-future", as_of=datetime(2026, 5, 1, tzinfo=timezone.utc))


def test_monitoring_counts_only_complete_days_and_reports_numbers_without_a_verdict(world):
    """Orders after yesterday are not in the live numbers; after a later refresh they are. No test statistics at any point."""
    wh, plat, _ = world
    design_min(plat, "g-mon")
    land_assignments(wh, "g-mon", [("g-mon-u1", "control", "2026-01-01 10:00:00"), ("g-mon-u2", "variant", "2026-01-01 10:00:00"),
                                   ("g-mon-u3", "control", "2026-01-01 10:00:00"), ("g-mon-u4", "variant", "2026-01-01 10:00:00")])
    orders = pd.DataFrame([("g-mon-o1", "g-mon-u1", "2026-01-03 09:00:00"), ("g-mon-o2", "g-mon-u2", "2026-01-10 09:00:00")],
                          columns=["order_id", "user_id", "order_ts"])
    orders["order_ts"] = pd.to_datetime(orders.order_ts, utc=True)
    orders = orders.assign(order_value=10.0, item_count=1, discount_value=0.0, status="completed", refund_ts=pd.NaT,
                           refund_value=np.nan, updated_at=orders.order_ts, ingested_at=orders.order_ts + pd.Timedelta(hours=1))
    wh.replace_rows("checkout_orders", orders, "STARTS_WITH(order_id, 'g-mon-')")
    priya = who(plat, "priya")
    with pytest.raises(errors.InvalidInput, match="no complete day of data yet"):
        plat.refresh_monitor(priya, "g-mon", as_of=datetime(2026, 1, 1, 18, tzinfo=timezone.utc))      # launch day is not over
    plat.refresh_monitor(priya, "g-mon", as_of=datetime(2026, 1, 6, 9, tzinfo=timezone.utc))            # data through Jan 5
    live = plat.latest_results("g-mon", "interim")[0]
    assert live["through_date"] == date(2026, 1, 5) and (live["n_control"], live["n_variant"]) == (2, 2)
    assert (live["mean_control"], live["mean_variant"]) == (0.5, 0.0)                                 # only the Jan 3 order so far
    assert live["p_value"] is None and live["verdict"] is None and live["ci_low"] is None
    plat.refresh_monitor(priya, "g-mon", as_of=datetime(2026, 1, 12, 9, tzinfo=timezone.utc))           # data through Jan 11
    live = plat.latest_results("g-mon", "interim")[0]
    assert live["through_date"] == date(2026, 1, 11) and (live["mean_control"], live["mean_variant"]) == (0.5, 0.5)
    daily = {(r["day"], r["arm"]): r for r in plat.daily_series("g-mon") if r["item_id"] == "conversion_rate"}
    assert len(daily) == 11 * 2 and max(d for d, _ in daily) == date(2026, 1, 11)                  # every day from launch through Jan 11, both arms
    assert [daily[(date(2026, 1, d), "control")]["mean_value"] for d in (2, 3, 4)] == [0.0, 0.5, 0.5]    # cumulative: the Jan 3 order stays in
    assert [daily[(date(2026, 1, d), "variant")]["mean_value"] for d in (9, 10, 11)] == [0.0, 0.5, 0.5]  # the Jan 10 order enters on its day
    assert all(r["n_users"] == 2 for r in daily.values()) and daily[(date(2026, 1, 11), "variant")]["mean_value"] == live["mean_variant"]
    assert plat.latest_results("g-mon") == [] and plat.get_experiment("g-mon")["status"] == "Running"
    with pytest.raises(errors.InvalidInput, match="still running until 2026-01-14"):                   # no verdict before the last day
        plat.run_analysis(priya, "g-mon", as_of=datetime(2026, 1, 12, tzinfo=timezone.utc))
    plat.run_analysis(priya, "g-mon", as_of=datetime(2026, 1, 15, tzinfo=timezone.utc))
    final = plat.latest_results("g-mon")[0]
    assert final["kind"] == "final" and final["through_date"] == date(2026, 1, 14) and final["verdict"]
    assert plat.get_experiment("g-mon")["status"] == "Analyzed"
    last = {r["arm"]: r["mean_value"] for r in plat.daily_series("g-mon") if r["item_id"] == "conversion_rate" and r["day"] == date(2026, 1, 14)}
    assert last == {"control": final["mean_control"], "variant": final["mean_variant"]}          # the series ends exactly at the final means
    c, v = (next(r for r in plat.daily_series("g-mon") if r["item_id"] == "conversion_rate" and r["day"] == date(2026, 1, 14) and r["arm"] == arm)
            for arm in ("control", "variant"))
    assert c["var_value"] is not None and c["var_value"] >= 0
    half = 1.959964 * (c["mean_value"] * (1 - c["mean_value"]) / c["n_users"] + v["mean_value"] * (1 - v["mean_value"]) / v["n_users"]) ** 0.5
    assert (v["mean_value"] - c["mean_value"]) - half == pytest.approx(final["ci_low"])           # the last day's range is the final confidence interval
    assert (v["mean_value"] - c["mean_value"]) + half == pytest.approx(final["ci_high"])


def test_an_experiment_mixing_products_gets_activity_for_every_product_and_segments_partition_the_audience(world):
    """Metrics from email and onboarding in a checkout experiment are simulated too (no planted effect there), so they are not all zero."""
    _, plat, _ = world
    priya = who(plat, "priya")
    reg = plat.metric_registry()
    plat.create_experiment(priya, "s-mix", "checkout", "Mixed", "h", LAUNCH, 10)
    plat.save_design(priya, "s-mix", plan(reg, "conversion_rate", effect=0.5),
                     [plan(reg, "open_rate", "guardrail", 0.37, 0.2)], ["activation_rate", "add_to_cart_rate"], dimension_ids=["plan_tier"])
    plat.simulate_data(priya, "s-mix", {"p_order_given_begin": 1.5}, n_users=8000, seed=9)
    assert plat.list_audit(who(plat, "admin"), "s-mix")[0]["detail"]["also_simulated"] == ["email", "onboarding"]
    plat.run_analysis(priya, "s-mix")
    res = {r["item_id"]: r for r in plat.latest_results("s-mix")}
    assert res["open_rate"]["mean_control"] > 0.05 and res["activation_rate"]["mean_control"] > 0.01      # real activity, not zeros
    assert res["conversion_rate"]["verdict"] == "Significant improvement"                                    # the planted effect is still found
    assert res["open_rate"]["verdict"] == "Passed" and res["activation_rate"]["verdict"] == "No significant difference"   # and nothing else moved
    seg = plat.segment_results("s-mix")
    assert {r["dimension_id"] for r in seg} == {"plan_tier"} and len({r["segment"] for r in seg}) >= 2
    for metric, overall in res.items():                                                                      # segments split the same users the totals count
        parts = [r for r in seg if r["item_id"] == metric]
        assert sum(r["n_control"] for r in parts) == overall["n_control"] and sum(r["n_variant"] for r in parts) == overall["n_variant"]
        weighted = sum(r["mean_control"] * r["n_control"] for r in parts) / overall["n_control"]
        assert weighted == pytest.approx(overall["mean_control"])


def test_each_user_is_measured_from_their_assignment_day_to_the_last_day_not_for_a_fixed_week(world):
    """Synthetic users, so nothing else lands on them. Orders on day 12 count for a user assigned on day 1 of a 14-day experiment, but not for a user assigned on day 13."""
    wh, plat, _ = world
    design_min(plat, "g-win")
    land_assignments(wh, "g-win", [("g-win-u1", "control", "2026-01-01 10:00:00"), ("g-win-u2", "variant", "2026-01-13 10:00:00"),
                                   ("g-win-u3", "control", "2026-01-01 10:00:00"), ("g-win-u4", "variant", "2026-01-01 10:00:00")])
    orders = pd.DataFrame([
        ("g-win-o1", "g-win-u1", "2026-01-12 09:00:00"),    # day 12, user assigned Jan 1: counts (the old fixed 7-day window would drop it)
        ("g-win-o2", "g-win-u2", "2026-01-12 09:00:00"),    # before this user's assignment on Jan 13: does not count
        ("g-win-o3", "g-win-u3", "2026-01-15 09:00:00"),    # after the last day (Jan 14): does not count
        ("g-win-o4", "g-win-u4", "2026-01-14 23:00:00"),    # on the last day: counts
    ], columns=["order_id", "user_id", "order_ts"])
    orders["order_ts"] = pd.to_datetime(orders.order_ts, utc=True)
    orders = orders.assign(order_value=10.0, item_count=1, discount_value=0.0, status="completed", refund_ts=pd.NaT,
                           refund_value=np.nan, updated_at=orders.order_ts, ingested_at=orders.order_ts + pd.Timedelta(hours=1))
    wh.replace_rows("checkout_orders", orders, "STARTS_WITH(order_id, 'g-win-')")
    plat.run_analysis(who(plat, "priya"), "g-win")
    f = plat.experiment_frame("g-win").set_index("user_id")
    assert {u: int(f.loc[u, "conversion_rate"]) for u in f.index} == {"g-win-u1": 1, "g-win-u2": 0, "g-win-u3": 0, "g-win-u4": 1}


# ---- simulation through the service --------------------------------------------------------------------------
def test_simulate_data_through_the_service_stores_ground_truth_and_respects_the_runtime(world):
    _, plat, _ = world
    design_min(plat, "s-onb", "sofia", "onboarding", "activation_rate", runtime=10)
    with pytest.raises(errors.InvalidInput, match="between 100 and 100,000"):
        plat.simulate_data(who(plat, "sofia"), "s-onb", {}, n_users=10)
    out = plat.simulate_data(who(plat, "sofia"), "s-onb", {"p_activate_given_project": 1.3}, n_users=1500, seed=5)
    assert out["n_users"] == 1500
    truth = plat.load_ground_truth("s-onb")
    assert truth["activation_rate"]["relative_lift"] == pytest.approx(0.3)
    assert truth["profile_completion_rate"]["relative_lift"] == pytest.approx(0)
    plat.run_analysis(who(plat, "sofia"), "s-onb")
    assert len(plat.experiment_frame("s-onb")) == 1500
    design_min(plat, "s-short", "sofia", "onboarding", "activation_rate", runtime=5)
    with pytest.raises(errors.InvalidInput, match="at least 7 days"):
        plat.simulate_data(who(plat, "sofia"), "s-short", {}, n_users=500)
