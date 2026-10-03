"""The Bayesian method end to end on the in-memory store: the service layer (plans, runs, catalog verdict), the plan form and the
results page. The fake runner gives control 10% against variant 16% with 450 users per arm."""
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest
import streamlit as st

from p2.catalog.seed import seed_registry
from p2.services import errors
from p2.services.platform import Platform
from p2.stats.bayes import BayesPlan
from p2.stats.plan import MetricPlan
from p2.store.memory import MemoryStore
from p2.store.schema import APP_TABLES
from p2.testing import FakeChecker, FakeRunner
from p2.warehouse.sources import load_sources
from test_app import charts, chart_titles, choose_primary, memory_platform, new_form, page, platform, table_for, texts  # noqa: F401

REG = seed_registry()
CONV, REFUND = REG.get("conversion_rate"), REG.get("refund_rate")
AFTER = datetime(2026, 2, 1, tzinfo=timezone.utc)


def primary_plan(threshold=0.0005, min_days=7):
    return BayesPlan(CONV, "primary", threshold=threshold, min_days=min_days)


def guard_plan(margin=0.25, harm=0.01):
    return BayesPlan(REFUND, "guardrail", margin=margin, margin_kind="relative", harm_limit=harm)


def freq_primary():
    return MetricPlan(CONV, "primary", 0.0374, 0.1, "relative", 0.05, 0.8, "two-sided")


@pytest.fixture
def plat():
    p = Platform(MemoryStore(APP_TABLES), load_sources(), checker=FakeChecker(), runner=FakeRunner())
    p.bootstrap()
    return p


def who(p):
    return p.get_actor("priya")


def bayes_design(p, eid="exp-1", launch=date(2026, 1, 1), runtime=14, guardrail=True):
    p.create_experiment(who(p), eid, "checkout", "n", "h", launch, runtime)
    p.save_design(who(p), eid, primary_plan(), [guard_plan()] if guardrail else [], ["add_to_cart_rate"])


# ---- the service layer ------------------------------------------------------------------------------------------------------
def test_a_bayesian_plan_is_stored_with_its_own_columns_and_sets_the_method(plat):
    bayes_design(plat)
    assert plat.get_experiment("exp-1")["method"] == "bayesian" and plat.get_experiment("exp-1")["status"] == "Designed"
    items = {i["item_id"]: i for i in plat.get_design("exp-1")}
    p, g = items["conversion_rate"], items["refund_rate"]
    assert (p["loss_threshold"], p["effect_kind"], p["min_days"], p["baseline"], p["alpha"], p["sidedness"]) == (0.0005, "absolute", 7, None, None, None)
    assert (g["effect"], g["effect_kind"], g["harm_limit"], g["baseline"]) == (0.25, "relative", 0.01, None)


def test_the_method_follows_the_plan_and_the_two_cannot_be_mixed(plat):
    plat.create_experiment(who(plat), "exp-1", "checkout", "n", "h", date(2026, 1, 1), 14)
    plat.save_design(who(plat), "exp-1", freq_primary(), [], [])
    assert plat.get_experiment("exp-1")["method"] == "frequentist"
    plat.save_design(who(plat), "exp-1", primary_plan(), [], [])
    assert plat.get_experiment("exp-1")["method"] == "bayesian"
    with pytest.raises(errors.InvalidInput, match="experiment's method"):
        plat.save_design(who(plat), "exp-1", primary_plan(), [MetricPlan(REFUND, "guardrail", 0.005, 0.25, "relative", 0.05, 0.8, "one-sided")], [])
    plat.save_design(who(plat), "exp-1", freq_primary(), [], [])
    assert plat.get_experiment("exp-1")["method"] == "frequentist"
    assert {i["loss_threshold"] for i in plat.get_design("exp-1")} == {None}      # the old Bayesian numbers are gone with the old plan


def test_the_final_run_of_a_bayesian_experiment_runs_no_test_and_stores_the_variances(plat):
    bayes_design(plat)
    plat.run_analysis(who(plat), "exp-1", as_of=AFTER)
    rows = {r["item_id"]: r for r in plat.latest_results("exp-1")}
    assert set(rows) == {"conversion_rate", "refund_rate", "add_to_cart_rate"}
    c = rows["conversion_rate"]
    assert c["kind"] == "final" and c["params"] == {"method": "bayesian"}
    assert (c["p_value"], c["verdict"], c["ci_low"], c["achieved_power"]) == (None, None, None, None)
    assert (c["mean_control"], c["var_control"], c["var_variant"], c["n_control"]) == (0.10, 0.09, 0.1344, 450)
    assert c["difference"] == pytest.approx(0.06) and plat.get_experiment("exp-1")["status"] == "Analyzed"


def test_every_run_keeps_the_variances_frequentist_final_and_live_alike(plat):
    plat.create_experiment(who(plat), "exp-1", "checkout", "n", "h", date(2026, 1, 1), 14)
    plat.save_design(who(plat), "exp-1", freq_primary(), [], [], ["plan_tier"])
    plat.run_analysis(who(plat), "exp-1", as_of=AFTER)
    final = plat.latest_results("exp-1")[0]
    assert final["p_value"] is not None and final["var_control"] == 0.09 and final["var_variant"] == 0.1344
    plat.refresh_monitor(who(plat), "exp-1", as_of=datetime(2026, 1, 6, tzinfo=timezone.utc))
    live = plat.latest_results("exp-1", "interim")[0]
    assert live["var_control"] == 0.09 and live["var_variant"] == 0.1344
    assert all(r["var_control"] is not None for r in plat.segment_results("exp-1"))


def test_a_bayesian_experiments_slices_have_no_test_but_carry_the_variances(plat):
    bayes_design(plat)
    plat.save_design(who(plat), "exp-1", primary_plan(), [], [], ["plan_tier"])
    plat.run_analysis(who(plat), "exp-1", as_of=AFTER)
    rows = plat.segment_results("exp-1")
    assert rows and all(r["p_value"] is None and r["verdict"] is None and r["var_variant"] is not None for r in rows)
    outs = plat.bayes_outcomes("exp-1", [r for r in rows if r["item_id"] == "conversion_rate"])
    assert all(o is not None and o.chance_to_win > 0.5 for o in outs)


def test_the_catalog_verdict_is_live_for_a_bayesian_experiment(plat):
    bayes_design(plat)
    assert plat.experiment_catalog()[0]["verdict"] is None                                     # not run yet
    plat.refresh_monitor(who(plat), "exp-1", as_of=datetime(2026, 1, 5, tzinfo=timezone.utc))   # data through 4 Jan: day 4
    assert plat.experiment_catalog()[0]["verdict"] == "Collecting evidence (day 4 of 7)"
    plat.refresh_monitor(who(plat), "exp-1", as_of=datetime(2026, 1, 9, tzinfo=timezone.utc))   # through 8 Jan: day 8
    assert plat.experiment_catalog()[0]["verdict"] == "Variant is the safer choice"            # live, no waiting for the last day
    plat.run_analysis(who(plat), "exp-1", as_of=AFTER)
    assert plat.experiment_catalog()[0]["verdict"] == "Variant is the safer choice"


def test_the_minimum_days_is_capped_at_the_runtime(plat):
    plat.create_experiment(who(plat), "exp-1", "checkout", "n", "h", date(2026, 1, 1), 3)
    plat.save_design(who(plat), "exp-1", primary_plan(min_days=7), [], [])
    plat.run_analysis(who(plat), "exp-1", as_of=AFTER)
    assert plat.experiment_catalog()[0]["verdict"] == "Variant is the safer choice"            # a 3-day test cannot wait for day 7


def test_the_guardrail_gets_a_chance_of_harm_and_its_own_verdict(plat):
    bayes_design(plat)
    plat.run_analysis(who(plat), "exp-1", as_of=AFTER)
    rows = plat.latest_results("exp-1")
    outs = dict(zip([r["item_id"] for r in rows], plat.bayes_outcomes("exp-1", rows)))
    assert outs["refund_rate"].chance_of_harm > 0.9 and outs["refund_rate"].verdict == "Failed"     # refunds went from 10% to 16%
    assert outs["conversion_rate"].chance_of_harm is None and outs["add_to_cart_rate"].verdict is None    # secondary: numbers only


def test_the_plan_fingerprint_of_a_frequentist_experiment_did_not_change(plat):
    """Experiments run before the Bayesian method existed must not suddenly look out of date."""
    plat.create_experiment(who(plat), "exp-1", "checkout", "n", "h", date(2026, 1, 1), 14)
    plat.save_design(who(plat), "exp-1", freq_primary(), [], ["add_to_cart_rate"])
    exp, design = plat.get_experiment("exp-1"), plat.get_design("exp-1")
    import hashlib
    from p2.pipeline.runner import end_of
    keys = ("kind", "item_id", "item_version", "role", "baseline", "std", "effect", "effect_kind", "alpha", "power", "sidedness")
    old = hashlib.sha1("\n".join([str(exp["launch_date"]), str(end_of(exp))] + sorted("|".join(str(r.get(k)) for k in keys) for r in design))
                       .encode()).hexdigest()[:12]
    assert plat._plan_stamp(exp, design) == old
    plat.save_design(who(plat), "exp-1", primary_plan(), [], [])
    before = plat._plan_stamp(plat.get_experiment("exp-1"), plat.get_design("exp-1"))
    plat.save_design(who(plat), "exp-1", primary_plan(threshold=0.001), [], [])
    assert plat._plan_stamp(plat.get_experiment("exp-1"), plat.get_design("exp-1")) != before   # a changed threshold marks the numbers out of date


# ---- the plan form ----------------------------------------------------------------------------------------------------------
def test_the_form_starts_frequentist_and_the_bayesian_choice_swaps_the_inputs():
    at = new_form()
    assert at.radio(key="method").value == "Frequentist" and at.radio(key="method").options == ["Frequentist", "Bayesian"]
    choose_primary(at, "conversion_rate")
    assert at.number_input(key="primary_conversion_rate_baseline") is not None
    at.radio(key="method").set_value("Bayesian").run()
    assert not at.exception and not at.error
    keys = {w.key for w in at.number_input}
    assert {"primary_conversion_rate_bthreshold", "primary_conversion_rate_bmindays"} <= keys
    assert not [k for k in keys if k.endswith(("_baseline", "_alpha", "_power", "_std")) or "_effect_" in k]     # no frequentist inputs
    assert at.number_input(key="primary_conversion_rate_bthreshold").value == pytest.approx(0.05)
    assert at.number_input(key="primary_conversion_rate_bmindays").value == 7
    assert not any("Enter the baseline" in i.value for i in at.info)                                              # defaults: nothing to fill in


def test_a_continuous_metric_asks_for_its_threshold_as_a_share_of_the_control_average():
    at = new_form()
    at.radio(key="method").set_value("Bayesian").run()
    choose_primary(at, "revenue_per_user")
    box = at.number_input(key="primary_revenue_per_user_bthreshold")
    assert box.value == pytest.approx(0.5) and "% of control average" in box.label


def test_a_bayesian_guardrail_asks_for_a_margin_and_a_harm_limit_that_defaults_to_one_percent():
    at = new_form()
    at.radio(key="method").set_value("Bayesian").run()
    choose_primary(at, "conversion_rate")
    at.multiselect(key="guardrails").set_value(["refund_rate"]).run()
    assert at.number_input(key="guardrail_refund_rate_bharm").value == pytest.approx(1.0)
    assert at.number_input(key="guardrail_refund_rate_bmargin_relative").value is None
    assert any("Enter the margin" in i.value for i in at.info)
    assert any(k.endswith("_bmargin_relative") for k in {w.key for w in at.number_input}) and not any("_baseline" in w.key for w in at.number_input)


def fill_bayes(at):
    at.radio(key="method").set_value("Bayesian").run()
    at.text_input(key="name").set_value("Bayes banner")
    at.date_input(key="launch").set_value(date(2026, 1, 5))
    choose_primary(at, "conversion_rate")
    at.number_input(key="primary_conversion_rate_bthreshold").set_value(0.1)
    at.number_input(key="primary_conversion_rate_bmindays").set_value(10)
    at.multiselect(key="guardrails").set_value(["refund_rate"]).run()
    at.number_input(key="guardrail_refund_rate_bmargin_relative").set_value(20.0)
    at.number_input(key="guardrail_refund_rate_bharm").set_value(2.0)
    return at.run()


def test_saving_a_bayesian_plan_records_the_entered_numbers_and_the_edit_form_loads_them_back():
    at = fill_bayes(new_form())
    assert not at.exception and not at.error
    at.button(key="save").click().run()
    assert at.success and not at.error, [e.value for e in at.error]
    p = platform()
    assert p.get_experiment("exp-001")["method"] == "bayesian"
    items = {i["item_id"]: i for i in p.get_design("exp-001")}
    c, g = items["conversion_rate"], items["refund_rate"]
    assert (c["loss_threshold"], c["min_days"], c["effect_kind"]) == (pytest.approx(0.001), 10, "absolute")
    assert (g["effect"], g["effect_kind"], g["harm_limit"]) == (pytest.approx(0.20), "relative", pytest.approx(0.02))
    again = page("catalog", "priya", catalog_edit="exp-001")
    assert not again.exception and not again.error
    assert again.radio(key="method").value == "Bayesian"
    assert again.number_input(key="primary_conversion_rate_bthreshold").value == pytest.approx(0.1)
    assert again.number_input(key="primary_conversion_rate_bmindays").value == 10
    assert again.number_input(key="guardrail_refund_rate_bmargin_relative").value == pytest.approx(20.0)
    assert again.number_input(key="guardrail_refund_rate_bharm").value == pytest.approx(2.0)


def test_changing_the_method_of_an_experiment_with_results_warns_and_switches_it():
    planned("exp-001", date(2026, 1, 1))
    p = platform()
    p.run_analysis(p.get_actor("priya"), "exp-001", as_of=AFTER)
    at = page("catalog", "priya", catalog_edit="exp-001")
    assert at.radio(key="method").value == "Bayesian" and not at.warning
    at.radio(key="method").set_value("Frequentist").run()
    assert any("changing the method" in w.value for w in at.warning)


# ---- the results page -------------------------------------------------------------------------------------------------------
def planned(eid, launch, runtime=14, dimensions=()):
    p = platform()
    p.create_experiment(p.get_actor("priya"), eid, "checkout", "Banner", "Lifts conversion", launch, runtime)
    p.save_design(p.get_actor("priya"), eid, primary_plan(), [guard_plan()], ["add_to_cart_rate"], dimensions)


BAYES_COLUMNS = ["Metric", "Role / type", "Control", "Variant", "Lift", "Chance variant wins", "Expected difference", "95% credible interval",
                 "Risk: ship variant", "Risk: keep control", "Chance of harm", "Verdict", "Users (control / variant)"]


def test_live_bayesian_numbers_show_a_verdict_once_the_minimum_days_have_passed():
    planned("exp-001", date.today() - timedelta(days=10))
    at = page("experiments", search_id="exp-001")
    at.button(key="monitor_go").click().run()
    assert not at.exception and at.success and not at.error
    assert not any("No conclusions until" in w.value for w in at.warning)                    # no no-peeking rule for Bayesian
    assert any("Live Bayesian numbers" in i.value for i in at.info)
    t = table_for(at).set_index("Metric")
    assert list(table_for(at).columns) == BAYES_COLUMNS and list(t.index) == ["Conversion rate", "Refund rate", "Add-to-cart rate"]
    assert "Variant is the safer choice" in t.loc["Conversion rate", "Verdict"] and "Failed" in t.loc["Refund rate", "Verdict"]
    assert t.loc["Add-to-cart rate", "Verdict"] == ""                                      # secondary: numbers only
    assert t.loc["Conversion rate", "Chance variant wins"] == "99.6%" and t.loc["Conversion rate", "Expected difference"].startswith("+5.9")
    assert t.loc["Conversion rate", "Risk: ship variant"].startswith("0.00") and t.loc["Conversion rate", "Risk: keep control"].endswith(" pp")
    assert t.loc["Refund rate", "Risk: ship variant"] == "" and t.loc["Refund rate", "Chance of harm"].endswith("%")     # guardrails show harm, not risks
    verdict = [m.value for m in at.markdown if 'class="verdict-card"' in m.value][0]
    assert "Variant is the safer choice" in verdict and "Shipping the variant risks only" in verdict and "under your 0.050 pp limit" in verdict
    assert "users" not in verdict and "chance the variant wins" not in verdict                                       # one line, the rest is in the table


def test_before_the_minimum_days_the_numbers_show_but_the_verdict_waits():
    planned("exp-001", date.today() - timedelta(days=3))
    at = page("experiments", search_id="exp-001")
    at.button(key="monitor_go").click().run()
    assert not at.exception
    t = table_for(at).set_index("Metric")
    assert t.loc["Conversion rate", "Verdict"] == "⏳ Collecting evidence (day 3 of 7)" and t.loc["Refund rate", "Verdict"] == "⏳ Collecting evidence (day 3 of 7)"
    assert t.loc["Conversion rate", "Chance variant wins"] != ""
    assert "Collecting evidence (day 3 of 7)" in texts(at) and "Too early: the verdict comes after 7 days of data." in texts(at)


def test_the_final_bayesian_analysis_and_the_charts_in_the_order_pms_read_them():
    planned("exp-001", date(2026, 1, 1))
    at = page("experiments", search_id="exp-001")
    at.button(key="run_go").click().run()
    assert not at.exception and at.success and "Final analysis finished" in at.success[0].value
    assert any("Bayesian analysis on the full runtime" in c.value for c in at.caption)
    assert list(table_for(at).columns) == BAYES_COLUMNS and "p-value" not in table_for(at).columns
    assert [t.label for t in at.tabs] == ["Tables", "Charts"]
    specs = [str(c.proto.spec) for c in charts(at)]
    assert len(specs) == 5      # arms over time, the risk bars and the risk by day side by side, the credible interval and chance to win by day
    assert '"arm"' in specs[0] and "Most we accept to lose" in specs[1] and "On all the data so far" in specs[1]
    assert "By day" in specs[2] and "Most we accept to lose" in specs[2] and "Keep control" in specs[2]
    assert "Difference, 95% credible interval" in specs[3] and '"area"' in specs[3] and "95% credible low" in specs[3]
    assert "Chance the variant wins" in specs[4]
    assert "Likelihood" not in " ".join(specs) and "Range low" not in " ".join(specs)       # no posterior shape, no frequentist band
    heads = [m.value for m in at.markdown if m.value in ("**Control and variant over time**", "**Risk of each choice**",
                                                          "**95% credible interval and chance the variant wins, by day**")]
    assert heads == ["**Control and variant over time**", "**Risk of each choice**", "**95% credible interval and chance the variant wins, by day**"]
    assert any("If we ship the variant and it is actually worse" in m.value and "the variant is the safer choice" in m.value for m in at.markdown)


def test_a_guardrail_gets_one_plain_sentence_instead_of_a_risk_plot():
    planned("exp-001", date(2026, 1, 1))
    at = page("experiments", search_id="exp-001")
    at.button(key="run_go").click().run()
    at.multiselect(key="chart_metrics_exp-001").set_value(["refund_rate"])
    at.button(key="chart_apply").click().run()
    assert not at.exception
    assert any("chance this guardrail got worse by more than" in m.value and "below **1%**" in m.value for m in at.markdown)
    assert not any("Most we accept to lose" in str(c.proto.spec) for c in charts(at))


def test_slices_get_their_own_bayesian_numbers():
    planned("exp-001", date(2026, 1, 1), dimensions=["plan_tier"])
    at = page("experiments", search_id="exp-001")
    at.button(key="run_go").click().run()
    at.selectbox(key="seg_pick_exp-001").set_value("plan_tier").run()
    assert not at.exception
    tables = [d.value for d in at.dataframe if "Lift" in d.value.columns]
    assert len(tables) == 2 and all("Chance variant wins" in t.columns for t in tables)
    assert any("Exploratory" in c.value and "Bayesian numbers" in c.value for c in at.caption)


def test_the_catalog_shows_the_live_bayesian_verdict():
    planned("exp-001", date.today() - timedelta(days=10))
    p = platform()
    p.refresh_monitor(p.get_actor("priya"), "exp-001")
    st.cache_data.clear()
    at = page("catalog", "viewer")
    assert not at.exception
    assert any("Variant is the safer choice" in m.value for m in at.markdown)


def test_a_frequentist_page_ignores_leftover_bayesian_numbers_and_says_so():
    planned("exp-001", date(2026, 1, 1))
    p = platform()
    p.run_analysis(p.get_actor("priya"), "exp-001", as_of=AFTER)
    p.save_design(p.get_actor("priya"), "exp-001", freq_primary(), [], [])
    st.cache_data.clear()
    at = page("experiments", search_id="exp-001")
    assert not at.exception and not at.error
    assert any("calculated with the Bayesian method" in i.value for i in at.info)
    assert not [d for d in at.dataframe if "Lift" in d.value.columns]


def test_old_results_without_variances_ask_for_a_new_run_for_averages_but_not_for_rates():
    p = platform()
    p.create_experiment(p.get_actor("priya"), "exp-001", "checkout", "n", "h", date(2026, 1, 1), 14)
    p.save_design(p.get_actor("priya"), "exp-001", freq_primary(), [], ["revenue_per_user"])
    p.run_analysis(p.get_actor("priya"), "exp-001", as_of=AFTER)
    p.save_design(p.get_actor("priya"), "exp-001", primary_plan(), [], ["revenue_per_user"])
    for r in p.store.select("results", {"experiment_id": "exp-001"}):                 # pretend the rows predate the stored variances
        p.store.update("results", {"experiment_id": "exp-001", "item_id": r["item_id"], "run_at": r["run_at"]}, {"var_control": None, "var_variant": None})
    st.cache_data.clear()
    at = page("experiments", search_id="exp-001")
    t = table_for(at).set_index("Metric")
    assert t.loc["Conversion rate", "Chance variant wins"] != "" and t.loc["Revenue per user", "Chance variant wins"] == ""
    assert t.loc["Revenue per user", "Verdict"] == ""                                    # a secondary metric has no verdict to wait for
    assert any("out of date" in w.value for w in at.warning)                             # the plan changed since the run
