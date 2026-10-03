"""The Streamlit pages, driven with AppTest on the in-memory store. The page calls only the service layer."""
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from p2.catalog.seed import seed_registry

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
REG = seed_registry()


@pytest.fixture(autouse=True)
def memory_platform(monkeypatch):
    monkeypatch.setenv("P2_STORE", "memory")
    st.cache_resource.clear()
    st.cache_data.clear()
    yield
    st.cache_resource.clear()
    st.cache_data.clear()


def platform():
    from common import get_platform
    return get_platform()


def page(name, actor="priya", **state):
    at = AppTest.from_file(str(APP / "views" / f"{name}.py"), default_timeout=60)
    at.session_state["actor_id"] = actor
    for k, v in state.items():
        at.session_state[k] = v
    return at.run()


def new_form(actor="priya", exp_id=None, product=None):
    """The Add Experiment page with an id typed in and the person's name picked as owner (the product follows from the owner). The next
    free number stands in for an id from the experiment tool's log."""
    at = page("design", actor)
    at.text_input(key="exp_id").set_value(exp_id or f"exp-{len(platform().list_experiments()) + 1:03d}").run()
    if any(w.key == "owner" for w in at.selectbox):
        at.selectbox(key="owner").set_value(actor).run()
        if product:
            at.selectbox(key="product").set_value(product).run()
        elif at.selectbox(key="product").value is None:
            at.selectbox(key="product").set_value("checkout").run()
    return at


def label(metric_id):
    m = REG.get(metric_id)
    return f"{m.display_name} ({m.product_id})"


def options(at, key):
    return set(at.selectbox(key=key).options) if key == "primary_metric" else set(at.multiselect(key=key).options)


def choose_primary(at, metric_id):
    box = at.selectbox(key="primary_metric")
    box.select_index(box.options.index(label(metric_id))).run()
    return at


def primary_id_for(user):
    return {"priya": "conversion_rate", "marcus": "click_rate", "sofia": "activation_rate", "admin": "conversion_rate"}[user]


def fill_primary(at, user="priya", baseline=3.5, effect=10.0):
    """Pick the user's usual primary metric and type the data scientist's own numbers (the tool supplies none)."""
    m = primary_id_for(user)
    choose_primary(at, m)
    at.number_input(key=f"primary_{m}_baseline").set_value(baseline)
    at.number_input(key=f"primary_{m}_effect_relative").set_value(effect)
    return at.run()


def texts(at):
    return " ".join(m.value for m in at.markdown)


# ---- the one-page plan form ------------------------------------------------------------------------------------------
def test_the_form_is_one_page_with_no_tabs_tags_or_review_step():
    at = new_form()
    assert not at.exception and not at.error
    assert len(at.tabs) == 0
    keys = {w.key for w in at.text_input} | {w.key for w in at.button}
    assert {"name", "exp_id", "save", "save_run"} <= keys and not keys & {"tags", "wiz_load", "draft_btn", "wiz_new"}
    assert [h.value for h in at.subheader] == ["Experiment", "Metrics", "Audience (optional)"]
    assert "Acting as" not in texts(at) and "Start from" not in texts(at)


def test_the_owner_is_picked_by_the_person_adding_the_experiment_and_there_is_no_sign_in():
    at = new_form("marcus")
    box = at.selectbox(key="owner")
    assert box.value == "marcus" and box.label == "Owner" and set(box.options) >= {"Priya (Checkout owner)", "Marcus (Email owner)", "Sofia (Onboarding owner)"}
    assert "Viewer (read only)" not in box.options                                                   # the demo read-only persona is not an owner
    assert at.selectbox(key="product").value == "email"                                              # the product follows the owner, and can be changed
    assert "Signed in" not in texts(at) and not at.sidebar.markdown


def test_the_owner_must_be_picked_before_saving():
    at = page("design")
    at.text_input(key="exp_id").set_value("exp-001").run()
    assert at.selectbox(key="owner").value is None and at.selectbox(key="owner").placeholder == "Select your name"
    at.text_input(key="name").set_value("No owner")
    at.selectbox(key="product").set_value("checkout").run()
    at.button(key="save").click().run()
    assert at.error and "owner" in at.error[0].value
    assert any("an owner" in c.value for c in at.caption) and platform().list_experiments() == []


def test_fields_carry_one_line_hover_tips():
    at = new_form()
    for w in (*at.text_input, *at.text_area, *at.date_input, *at.number_input):
        if w.key in ("name", "exp_id", "hypothesis", "launch", "runtime"):
            assert w.help and len(w.help) < 160 and "\n" not in w.help, w.key
    assert at.selectbox(key="primary_metric").help and at.multiselect(key="guardrails").help and at.multiselect(key="filters").help


def test_the_plan_form_asks_for_the_users_own_numbers_and_supplies_none():
    at = new_form()
    choose_primary(at, "conversion_rate")
    assert not at.exception and not at.error
    assert at.number_input(key="primary_conversion_rate_baseline").value is None and at.number_input(key="primary_conversion_rate_effect_relative").value is None
    assert at.number_input(key="primary_conversion_rate_alpha").value == pytest.approx(0.05)     # the only defaults: alpha and power
    assert at.number_input(key="primary_conversion_rate_power").value == pytest.approx(0.80)
    assert any("Enter the baseline, effect" in i.value for i in at.info)
    assert "sample size" not in texts(at).lower() and "users needed" not in texts(at).lower()   # no sample-size calculator in the tool


def test_every_metric_from_every_product_is_offered_in_every_role():
    for user in ("priya", "marcus", "sofia"):
        at = new_form(user)
        assert len(options(at, "primary_metric")) == len(options(at, "guardrails")) == len(options(at, "secondary")) == 15
        assert {"Conversion rate (checkout)", "Open rate (email)", "Activation rate (onboarding)"} <= options(at, "primary_metric")


def test_a_metric_picked_in_one_role_disappears_from_the_other_two():
    at = new_form()
    choose_primary(at, "conversion_rate")
    assert label("conversion_rate") not in options(at, "guardrails") | options(at, "secondary")
    at.multiselect(key="guardrails").set_value(["refund_rate"]).run()
    assert label("refund_rate") not in options(at, "primary_metric") | options(at, "secondary")
    assert label("conversion_rate") not in options(at, "guardrails") | options(at, "secondary")
    at.multiselect(key="secondary").set_value(["add_to_cart_rate"]).run()
    assert label("add_to_cart_rate") not in options(at, "primary_metric") | options(at, "guardrails")
    assert not at.exception and not at.error
    at.multiselect(key="guardrails").set_value([]).run()                                   # un-picking frees it again
    assert label("refund_rate") in options(at, "primary_metric") | options(at, "secondary")


def test_the_metric_type_decides_whether_std_dev_applies():
    at = new_form()
    choose_primary(at, "conversion_rate")
    assert at.number_input(key="primary_conversion_rate_std").disabled                      # a rate: the spread follows from the baseline
    assert any("Rate" in m.value and "z-test" in (m.help or "") for m in at.markdown)
    choose_primary(at, "revenue_per_user")
    assert not at.number_input(key="primary_revenue_per_user_std").disabled
    assert any("Continuous" in m.value for m in at.markdown)


def test_a_continuous_metric_also_needs_a_standard_deviation():
    at = new_form()
    choose_primary(at, "revenue_per_user")
    at.number_input(key="primary_revenue_per_user_baseline").set_value(3.85)
    at.number_input(key="primary_revenue_per_user_effect_relative").set_value(10.0)
    at.run()
    assert any("standard deviation" in i.value for i in at.info)
    at.number_input(key="primary_revenue_per_user_std").set_value(29.0).run()
    assert not any("standard deviation" in i.value for i in at.info) and not at.error


def test_a_guardrail_asks_for_numbers_too_and_audience_is_optional():
    at = new_form()
    choose_primary(at, "conversion_rate")
    at.multiselect(key="guardrails").set_value(["refund_rate"]).run()
    assert at.number_input(key="guardrail_refund_rate_baseline").value is None
    assert at.multiselect(key="filters").value == [] and at.multiselect(key="dimensions").value == []
    assert not at.error


def fill_everything(at):
    at.text_input(key="name").set_value("Banner test")
    at.text_area(key="hypothesis").set_value("Banner lifts conversion")
    at.date_input(key="launch").set_value(date(2026, 1, 5))
    at.number_input(key="runtime").set_value(21)
    choose_primary(at, "conversion_rate")
    at.number_input(key="primary_conversion_rate_baseline").set_value(3.74)
    at.number_input(key="primary_conversion_rate_effect_relative").set_value(12.5)
    at.number_input(key="primary_conversion_rate_alpha").set_value(0.1)
    at.multiselect(key="guardrails").set_value(["refund_rate"]).run()
    at.number_input(key="guardrail_refund_rate_baseline").set_value(0.47)
    at.number_input(key="guardrail_refund_rate_effect_relative").set_value(25.0)
    at.multiselect(key="filters").set_value(["existing_user"])
    at.multiselect(key="dimensions").set_value(["plan_tier", "customer_type"])
    return at.run()


def test_save_records_the_entered_plan_exactly_with_the_actor_as_owner():
    at = fill_everything(new_form())
    assert not at.exception and not at.error
    at.button(key="save").click().run()
    assert at.success and not at.error, [e.value for e in at.error]
    p = platform()
    exp = p.get_experiment("exp-001")
    assert (exp["owner_user_id"], exp["product_id"], exp["status"], exp["launch_date"], exp["runtime_days"]) == (
        "priya", "checkout", "Designed", date(2026, 1, 5), 21)
    items = {i["item_id"]: i for i in p.get_design("exp-001")}
    assert set(items) == {"conversion_rate", "refund_rate", "existing_user", "plan_tier", "customer_type"}
    c = items["conversion_rate"]
    assert (c["baseline"], c["effect"], c["effect_kind"], c["alpha"], c["power"], c["sidedness"]) == (0.0374, 0.125, "relative", 0.1, 0.8, "two-sided")
    g = items["refund_rate"]
    assert g["baseline"] == pytest.approx(0.0047) and g["effect"] == pytest.approx(0.25) and g["sidedness"] == "one-sided"
    assert exp["owner_user_id"] == "priya"                                                             # the owner is the name picked in the form
    assert any(a["action"] == "experiment.design" for a in p.list_audit(p.get_actor("admin")))
    assert not p.latest_results("exp-001") and not p.list_jobs("exp-001")                   # Save never runs anything


def test_a_metric_from_another_product_can_be_the_primary():
    at = new_form()
    at.text_input(key="name").set_value("Cross product")
    choose_primary(at, "open_rate")
    at.number_input(key="primary_open_rate_baseline").set_value(37.0)
    at.number_input(key="primary_open_rate_effect_relative").set_value(5.0)
    at.run()
    at.button(key="save").click().run()
    assert at.success and not at.error
    assert platform().get_design("exp-001")[0]["item_id"] == "open_rate"


def test_save_with_an_incomplete_plan_keeps_a_draft_and_says_what_is_missing():
    at = new_form()
    at.text_input(key="name").set_value("Half done")
    at.run()
    assert any("primary metric" in c.value for c in at.caption)
    at.button(key="save").click().run()
    assert any("Saved exp-001 as a draft" in s.value for s in at.success)
    p = platform()
    assert p.get_experiment("exp-001")["status"] == "Draft" and p.get_design("exp-001") == []
    view = page("experiments", "viewer", search_id="exp-001")
    assert any("No complete plan" in c.value for c in view.caption)


def test_saving_again_from_the_catalog_updates_the_experiment_and_keeps_the_values():
    at = new_form()
    at.text_input(key="name").set_value("First")
    at.button(key="save").click().run()
    edit = page("catalog", "priya", catalog_edit="exp-001")
    assert edit.text_input(key="name").value == "First"
    edit.text_input(key="name").set_value("Second")
    fill_primary(edit)
    edit.button(key="save").click().run()
    assert edit.success and not edit.error
    p = platform()
    assert len(p.list_experiments()) == 1 and p.get_experiment("exp-001")["name"] == "Second" and p.get_experiment("exp-001")["status"] == "Designed"
    assert edit.text_input(key="name").value == "Second"                                          # the edit form stays open with its values


def test_blank_name_blocks_saving():
    at = new_form()
    fill_primary(at)
    at.button(key="save").click().run()
    assert at.error and "name" in at.error[0].value
    assert platform().list_experiments() == []


def test_nobody_is_read_only_everyone_can_add_an_experiment():
    for who_ in ("viewer", "priya", "marcus"):
        at = new_form(who_ if who_ != "viewer" else "priya", "exp-0%d" % (len(platform().list_experiments()) + 1))
        assert not at.exception and [w for w in at.text_input if w.key == "name"] and any(b.key == "save" for b in at.button)


def test_admin_chooses_the_product_and_sees_its_audience_options():
    at = new_form("admin")
    assert at.selectbox(key="product").options == ["checkout", "email", "onboarding"]
    assert "Customer type at entry" in options(at, "dimensions")                                  # a checkout-only dimension
    at.selectbox(key="product").select("email").run()
    assert "Customer type at entry" not in options(at, "dimensions") and "Plan tier at entry" in options(at, "dimensions")
    assert len(options(at, "primary_metric")) == 15                                      # metrics are not scoped to the product


def test_the_owner_can_be_changed_to_someone_else_when_editing():
    planned_experiment()
    st.cache_data.clear()
    at = page("catalog", "marcus", catalog_edit="exp-001")                                          # anyone can open anyone's experiment
    assert at.selectbox(key="owner").value == "priya"
    at.selectbox(key="owner").set_value("sofia").run()
    at.button(key="save").click().run()
    assert at.success and not at.error and platform().get_experiment("exp-001")["owner_user_id"] == "sofia"


def test_an_id_that_already_exists_says_so_and_hides_the_form():
    p = platform()
    p.create_experiment(p.get_actor("priya"), "exp-001", "checkout", "Mine", "h", date(2026, 1, 1), 14)
    for actor in ("priya", "marcus"):
        at = new_form(actor, "exp-001")
        assert not at.exception and any("already exists" in i.value and "Experiment Catalog" in i.value for i in at.info)
        assert not [w for w in at.text_input if w.key == "name"] and not [b for b in at.button if b.key == "save"]    # nothing else is shown
    assert p.get_experiment("exp-001")["name"] == "Mine"


def test_an_id_missing_from_the_assignment_log_stops_the_page():
    at = new_form(exp_id="nolog-1")
    assert not at.exception and any("does not exist in the experiment tool's assignment log" in e.value for e in at.error)
    assert not [w for w in at.text_input if w.key == "name"] and not [b for b in at.button if b.key == "save"]
    assert platform().list_experiments() == []


def test_the_page_waits_for_an_id_before_showing_anything():
    at = page("design")
    assert any("Enter the experiment ID" in i.value for i in at.info) and [b.key for b in at.button] == ["land_sep"]    # only the dev button
    assert not [w for w in at.text_input if w.key == "name"]


def test_the_launch_date_starts_from_the_first_assignment_day_and_can_be_changed():
    at = new_form(exp_id="exp-001")                                                               # the fake log starts on 1 January
    assert at.date_input(key="launch").value == date(2026, 1, 1)
    assert any("Found exp-001 in the assignment log: 1,000 users so far, first assigned 2026-01-01" in s.value for s in at.success)
    at.date_input(key="launch").set_value(date(2026, 1, 3)).run()                                  # for example to skip a testing day
    assert at.date_input(key="launch").value == date(2026, 1, 3)
    at.text_input(key="name").set_value("Skip day one")
    fill_primary(at)
    at.button(key="save").click().run()
    assert platform().get_experiment("exp-001")["launch_date"] == date(2026, 1, 3)


def test_after_saving_a_new_experiment_the_form_starts_blank_and_points_to_the_catalog():
    at = new_form(exp_id="exp-001")
    at.text_input(key="name").set_value("First")
    fill_primary(at)
    at.button(key="save").click().run()
    assert at.success and not at.error and any("Experiment catalog" in x.value for x in at.success)
    assert at.title[0].value == "Add Experiment" and any("Enter the experiment ID" in i.value for i in at.info)    # blank, ready for the next one
    assert platform().get_experiment("exp-001")["status"] == "Designed"
    again = new_form(exp_id="exp-001")
    assert any("already exists" in i.value and "Experiment Catalog" in i.value for i in again.info)                # and it cannot be created twice


def test_save_and_run_on_a_running_experiment_starts_monitoring():
    at = new_form()
    at.text_input(key="name").set_value("Live one")
    at.date_input(key="launch").set_value(date.today() - timedelta(days=3))
    fill_primary(at)
    at.button(key="save_run").click().run()
    assert not at.exception and not at.error, [e.value for e in at.error]
    p = platform()
    assert p.get_experiment("exp-001")["status"] == "Running"
    assert p.latest_results("exp-001", "interim") and p.latest_results("exp-001") == []        # numbers, but no verdict yet


def test_save_and_run_after_the_last_day_gives_the_final_analysis():
    at = new_form()
    at.text_input(key="name").set_value("Done one")
    at.date_input(key="launch").set_value(date.today() - timedelta(days=30))
    at.number_input(key="runtime").set_value(14)
    fill_primary(at)
    at.button(key="save_run").click().run()
    assert not at.exception and not at.error, [e.value for e in at.error]
    p = platform()
    assert p.get_experiment("exp-001")["status"] == "Analyzed" and p.latest_results("exp-001")[0]["verdict"]


def test_save_and_run_with_a_bad_plan_runs_nothing():
    at = new_form()
    at.text_input(key="name").set_value("No metric")
    at.button(key="save_run").click().run()
    assert platform().list_jobs("exp-001") == [] and platform().get_experiment("exp-001")["status"] == "Draft"


def test_the_edit_button_loads_a_saved_plan_and_saving_changes_it():
    at = fill_everything(new_form())
    at.button(key="save").click().run()
    again = page("catalog", "priya", catalog_edit="exp-001")
    assert not again.exception and not again.error
    assert again.text_input(key="name").value == "Banner test" and again.text_area(key="hypothesis").value == "Banner lifts conversion"
    assert again.date_input(key="launch").value == date(2026, 1, 5) and again.number_input(key="runtime").value == 21
    assert again.text_input(key="exp_id").disabled and again.text_input(key="exp_id").value == "exp-001"
    assert again.selectbox(key="primary_metric").value == "conversion_rate"
    assert again.number_input(key="primary_conversion_rate_baseline").value == pytest.approx(3.74)
    assert again.number_input(key="primary_conversion_rate_effect_relative").value == pytest.approx(12.5)
    assert again.number_input(key="primary_conversion_rate_alpha").value == pytest.approx(0.1)
    assert again.multiselect(key="guardrails").value == ["refund_rate"]
    assert again.number_input(key="guardrail_refund_rate_effect_relative").value == pytest.approx(25.0)
    assert again.multiselect(key="filters").value == ["existing_user"]
    assert set(again.multiselect(key="dimensions").value) == {"plan_tier", "customer_type"}

    again.text_input(key="name").set_value("Renamed")
    again.number_input(key="primary_conversion_rate_effect_relative").set_value(15.0)
    again.number_input(key="runtime").set_value(28)
    again.run()
    again.button(key="save").click().run()
    assert again.success and not again.error
    p = platform()
    e = p.get_experiment("exp-001")
    assert (e["name"], e["status"], e["runtime_days"]) == ("Renamed", "Designed", 28)
    primary = next(i for i in p.get_design("exp-001") if i["role"] == "primary")
    assert primary["effect"] == pytest.approx(0.15) and primary["baseline"] == pytest.approx(0.0374)
    assert len([x for x in p.experiment_history("exp-001") if x["action"] == "experiment.design"]) == 2


def test_back_to_list_leaves_the_edit_form():
    at = fill_everything(new_form())
    at.button(key="save").click().run()
    edit = page("catalog", "priya", catalog_edit="exp-001")
    assert any(h.value == "Edit exp-001" for h in edit.subheader) and edit.text_input(key="name").value == "Banner test"
    edit.button(key="cat_back").click().run()
    assert not any(h.value == "Edit exp-001" for h in edit.subheader) and edit.text_input(key="cat_search") is not None   # back on the list


def test_the_new_form_and_the_edit_form_do_not_leak_into_each_other():
    at = new_form(exp_id="exp-007")
    at.text_input(key="name").set_value("A half typed new experiment")
    planned_experiment()
    st.cache_data.clear()
    edit = page("catalog", "priya", catalog_edit="exp-001")
    assert edit.text_input(key="name").value == "Banner"                                          # the edit shows the saved experiment, not the draft
    blank = new_form(exp_id="exp-008")
    assert blank.text_input(key="name").value == ""


def test_editing_works_in_every_status_and_changes_everything_but_the_id():
    planned_experiment()
    p = platform()
    p.run_analysis(p.get_actor("priya"), "exp-001", as_of=pd.Timestamp("2099-01-01", tz="UTC").to_pydatetime())
    p.record_decision(p.get_actor("priya"), "exp-001", "ship", "ok")
    st.cache_data.clear()
    at = page("catalog", "priya", catalog_edit="exp-001")                                              # Analyzed and then Decided: still editable
    assert not at.exception and not at.error and at.text_input(key="exp_id").disabled
    assert at.selectbox(key="primary_metric").value == "conversion_rate" and not at.number_input(key="primary_conversion_rate_baseline").disabled
    at.text_input(key="name").set_value("Changed after the decision")
    at.multiselect(key="dimensions").set_value(["plan_tier", "customer_type"])                    # a segment the PM asked for
    at.multiselect(key="filters").set_value(["region_us"])
    at.run()
    at.button(key="save").click().run()
    assert at.success and not at.error, [e.value for e in at.error]
    e = platform().get_experiment("exp-001")
    assert (e["name"], e["status"], e["decision"]) == ("Changed after the decision", "Decided", "ship")
    assert {i["item_id"] for i in platform().get_design("exp-001")} >= {"plan_tier", "customer_type", "region_us"}


def test_extending_an_experiment_that_has_results_shows_a_warning_but_does_not_block():
    running_experiment()
    p = platform()
    p.refresh_monitor(p.get_actor("priya"), "exp-001")
    st.cache_data.clear()
    at = page("catalog", "priya", catalog_edit="exp-001")
    assert not any("extending" in w.value for w in at.warning)
    at.date_input(key="end").set_value(date.today() + timedelta(days=30)).run()
    assert any("extending an experiment that already has results" in w.value for w in at.warning)
    at.button(key="save").click().run()
    assert at.success and not at.error and platform().get_experiment("exp-001")["end_date"] == date.today() + timedelta(days=30)
    early = page("catalog", "priya", catalog_edit="exp-001")                                           # shortening is fine and silent
    early.date_input(key="end").set_value(date.today() + timedelta(days=5)).run()
    assert not any("extending" in w.value for w in early.warning)


def test_after_extending_a_finished_experiment_the_page_shows_live_numbers_again():
    planned_experiment()
    p = platform()
    a = p.get_actor("priya")
    p.run_analysis(a, "exp-001", as_of=pd.Timestamp("2099-01-01", tz="UTC").to_pydatetime())
    p.update_experiment(a, "exp-001", "Banner", "h", date(2026, 1, 1), 14, end_date=date.today() + timedelta(days=10))
    st.cache_data.clear()
    at = page("experiments", search_id="exp-001")
    assert at.button(key="monitor_go") is not None
    at.button(key="monitor_go").click().run()
    assert platform().get_experiment("exp-001")["status"] == "Running"
    assert any("Interim view" in w.value for w in at.warning) and not any("Final analysis on the full runtime" in c.value for c in at.caption)   # the latest run wins


def search(at, text):
    at.text_input(key="search_text").set_value(text)
    return at.button(key="search_go").click().run()


def test_the_experiments_page_is_a_search_with_no_portfolio_overview():
    p = platform()
    p.create_experiment(p.get_actor("priya"), "exp-001", "checkout", "Banner", "Lifts conversion", date(2026, 1, 1), 14)
    at = page("experiments", "viewer")
    assert not at.exception and at.info and "Search for an experiment by its ID" in at.info[0].value
    assert len(at.dataframe) == 0 and len(at.metric) == 0 and len(at.selectbox) == 0      # no filters, status counts or table of everything
    assert at.text_input(key="search_text").label == "Experiment ID"


def test_searching_by_id_opens_that_experiment_ignoring_case_and_spaces():
    p = platform()
    p.create_experiment(p.get_actor("priya"), "exp-001", "checkout", "Banner", "Lifts conversion", date(2026, 1, 1), 14)
    p.create_experiment(p.get_actor("marcus"), "exp-002", "email", "Subject", "Lifts opens", date(2026, 1, 1), 14)
    at = search(page("experiments", "viewer"), "  EXP-002 ")
    assert not at.exception and not at.error
    assert any(s.value == "Subject" for s in at.subheader) and any(">email<" in m.value for m in at.markdown)
    at = search(at, "exp-001")
    assert any(s.value == "Banner" for s in at.subheader)


def test_an_unknown_id_says_so_and_suggests_similar_ones():
    p = platform()
    p.create_experiment(p.get_actor("priya"), "exp-001", "checkout", "Banner", "h", date(2026, 1, 1), 14)
    at = search(page("experiments"), "exp-0")
    assert at.error and "No experiment with the id" in at.error[0].value
    assert any("Similar ids: exp-001" in c.value for c in at.caption)
    assert not at.exception


def test_there_is_no_admin_page():
    from pathlib import Path
    assert not (APP / "views" / "admin.py").exists()
    assert "admin.py" not in (APP / "streamlit_app.py").read_text()                                  # and no sidebar entry for it
    assert [p.stem for p in sorted((APP / "views").glob("*.py"))] == ["catalog", "design", "experiments"]


# ---- the catalog page ----------------------------------------------------------------------------------------
def test_the_metric_catalog_is_one_simple_table_of_metrics_only():
    at = page("catalog", "viewer")
    assert not at.exception and not at.error
    table = at.dataframe[0].value
    assert list(table.columns) == ["Metric", "Product", "Added by", "Definition"]              # nothing else
    assert len(table) == 15 and set(table.Product) == {"checkout", "email", "onboarding"}      # metrics only: no segments or filters
    assert "Customer type at entry" not in set(table.Metric) and "Existing user" not in set(table.Metric)
    row = table[table.Metric == "Conversion rate"].iloc[0]
    assert row["Added by"] == "System (seeded)" and row.Definition
    assert at.selectbox(key="m_pick").label == "Select metric" and at.selectbox(key="m_pick").options[0] == "All metrics"


def test_selecting_a_metric_shows_only_that_metric():
    at = page("catalog", "viewer")
    at.selectbox(key="m_pick").set_value("refund_rate").run()
    table = at.dataframe[0].value
    assert list(table.Metric) == ["Refund rate"] and table.iloc[0].Product == "checkout"
    at.selectbox(key="m_pick").set_value("").run()
    assert len(at.dataframe[0].value) == 15


def add_metric_like_a_bigquery_insert(item_id="big_basket_rate", author="priya", status="Certified", product="checkout"):
    """What a data scientist does in BigQuery: insert one row into the catalog table. The tool has no way to add metrics."""
    from datetime import datetime, timezone
    p = platform()
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    p.store.insert("catalog_items", {
        "item_id": item_id, "version": 1, "kind": "metric", "product_id": product, "display_name": "Big basket rate",
        "description": "Share of users with an order of five or more items", "sql": "SELECT 1", "status": status, "author": author,
        "value_type": "binary", "aggregation": "max", "good_direction": "higher", "format": "percent", "created_at": now, "updated_at": now})
    st.cache_data.clear()


def test_a_metric_added_in_bigquery_shows_up_with_who_added_it_and_in_the_experiment_form():
    add_metric_like_a_bigquery_insert()
    table = page("catalog", "viewer").dataframe[0].value
    row = table[table.Metric == "Big basket rate"].iloc[0]
    assert row["Added by"] == "Priya (Checkout owner)" and row.Product == "checkout" and "five or more items" in row.Definition
    assert len(table) == 16
    form = new_form(exp_id="exp-001")
    assert "Big basket rate (checkout)" in options(form, "primary_metric")                          # and it can be chosen in an experiment


def test_a_metric_that_is_not_certified_is_not_listed_or_offered():
    add_metric_like_a_bigquery_insert(status="Draft")
    assert "Big basket rate" not in set(page("catalog", "viewer").dataframe[0].value.Metric)
    assert "Big basket rate (checkout)" not in options(new_form(exp_id="exp-001"), "primary_metric")


def test_the_catalog_page_has_no_tools_to_add_review_or_version_metrics_for_anyone():
    for user in ("viewer", "priya", "marcus", "admin"):
        at = page("catalog", user)
        assert not at.exception and not [e for e in at.expander if "Manage" in e.label]
        keys = {b.key for b in at.button} | {w.key for w in at.text_input} | {w.key for w in at.text_area}
        assert not any(k and k.split("_")[0] in ("p", "draft", "review", "b") for k in keys), (user, keys)
        assert len(at.dataframe) == 1 and [t.label for t in at.tabs] == ["Experiments", "Metrics"]


# ---- monitoring, the final analysis and decisions ----------------------------------------------------------------------
def planned_experiment(user="priya", eid="exp-001", launch=date(2026, 1, 1), runtime=14, dimensions=(), end_date=None, filters=()):
    p = platform()
    from p2.stats.plan import MetricPlan
    product = {"priya": "checkout", "marcus": "email"}[user]
    p.create_experiment(p.get_actor(user), eid, product, "Banner", "Lifts conversion", launch, runtime, end_date)
    reg = p.metric_registry()
    metric, guardrail = {"checkout": ("conversion_rate", "refund_rate"), "email": ("click_rate", "unsubscribe_rate")}[product]
    p.save_design(p.get_actor(user), eid, MetricPlan(reg.get(metric), "primary", 0.0374, 0.1, "relative", 0.05, 0.8, "two-sided"),
                  [MetricPlan(reg.get(guardrail), "guardrail", 0.0047, 0.25, "relative", 0.05, 0.8, "one-sided")], [], dimensions, filters)


def running_experiment(**kw):
    planned_experiment(launch=date.today() - timedelta(days=3), runtime=14, **kw)


def charts(at):
    return at.get("vega_lite_chart")


def test_a_running_experiment_offers_live_monitoring_but_no_final_analysis_or_verdict():
    running_experiment()
    at = page("experiments", search_id="exp-001")
    assert not at.exception and at.button(key="monitor_go") is not None
    assert len([b for b in at.button if b.key == "run_go"]) == 0 and len(charts(at)) == 0 and any("No numbers yet" in i.value for i in at.info)
    at.button(key="monitor_go").click().run()
    assert not at.exception and at.success and "Monitoring refreshed" in at.success[0].value
    p = platform()
    assert p.get_experiment("exp-001")["status"] == "Running" and p.list_jobs("exp-001")[0]["type"] == "monitor"
    assert any("Interim view" in w.value and "No conclusions until" in w.value for w in at.warning)
    assert not any("Final analysis on the full runtime" in c.value for c in at.caption)
    live = next(d.value for d in at.dataframe if "Lift" in d.value.columns)
    assert list(live.Metric) == ["Conversion rate", "Refund rate"] and list(live["Role / type"]) == ["Primary, rate", "Guardrail, rate"]
    assert live.set_index("Metric").loc["Conversion rate", "Difference"] == "+6.000 pp"
    assert not {"Verdict", "p-value", "CI"} & set(live.columns)                         # nothing that could be read as a conclusion
    assert len(charts(at)) == 2                                                       # control against variant, and the difference: a line only, no range yet
    assert '"area"' not in chart_titles(at) and any("range around the line appears after the final analysis" in c.value for c in at.caption)
    assert not [s for s in at.selectbox if s.key == "dec_choice"]                      # and no decision form yet


def test_the_owner_runs_the_final_analysis_after_the_last_day_and_sees_every_verdict_and_both_charts():
    planned_experiment()
    at = page("experiments", search_id="exp-001")
    assert not at.exception and at.button(key="run_go") is not None
    assert len([b for b in at.button if b.key == "monitor_go"]) == 0
    at.button(key="run_go").click().run()
    assert not at.exception and at.success and "Final analysis finished" in at.success[0].value
    p = platform()
    assert p.get_experiment("exp-001")["status"] == "Analyzed" and p.list_jobs("exp-001")[0]["type"] == "analysis"
    results = next(d.value for d in at.dataframe if "Verdict" in d.value.columns and (d.value["Verdict"] != "").any())
    assert list(results.columns) == ["Metric", "Role / type", "Control", "Variant", "Difference", "Lift", "Verdict", "p-value", "CI",
                                     "Users (control / variant)", "Power"]
    by_metric = results.set_index("Metric")
    assert list(by_metric.index) == ["Conversion rate", "Refund rate"]
    assert "Significant improvement" in by_metric.loc["Conversion rate", "Verdict"] and "Failed" in by_metric.loc["Refund rate", "Verdict"]
    assert by_metric.loc["Conversion rate", "Control"] == "10.000%" and by_metric.loc["Conversion rate", "Difference"] == "+6.000 pp"
    assert by_metric.loc["Conversion rate", "Lift"] == "+60.0%" and "(" not in by_metric.loc["Conversion rate", "CI"]
    assert by_metric.loc["Conversion rate", "p-value"] != "" and by_metric.loc["Conversion rate", "Power"].endswith("%")
    assert any("Final analysis on the full runtime" in c.value for c in at.caption)
    assert [t.label for t in at.tabs] == ["Tables", "Charts"]
    assert len(charts(at)) == 2                                                                      # primary only by default: the two arms, and the difference
    assert '"area"' in chart_titles(at) and any("95% range" in c.value for c in at.caption)         # after the final analysis the difference chart has its shaded range
    assert at.multiselect(key="chart_metrics_exp-001").value == ["conversion_rate"]


def chart_titles(at):
    return " ".join(str(c.proto.spec) for c in charts(at))


def test_the_charts_show_only_the_metrics_picked_in_the_filter():
    planned_experiment()
    at = page("experiments", search_id="exp-001")
    at.button(key="run_go").click().run()
    box = at.multiselect(key="chart_metrics_exp-001")
    assert set(box.options) == {"Conversion rate (primary)", "Refund rate (guardrail)"}              # the role is part of the name
    assert "Conversion rate" in chart_titles(at) and "Refund rate" not in chart_titles(at)         # neither chart has the unpicked metric
    box.set_value(["conversion_rate", "refund_rate"]).run()
    assert not at.exception and len(charts(at)) == 4                                               # per metric: the two arms over time and the difference
    assert "Refund rate" in chart_titles(at)
    at.multiselect(key="chart_metrics_exp-001").set_value(["refund_rate"]).run()
    assert "Refund rate" in chart_titles(at) and "Conversion rate" not in chart_titles(at)
    at.multiselect(key="chart_metrics_exp-001").set_value([]).run()
    assert len(charts(at)) == 0 and any("Pick at least one metric" in i.value for i in at.info)


def test_monitoring_history_stays_visible_next_to_the_final_results():
    running_experiment()
    at = page("experiments", search_id="exp-001")
    at.button(key="monitor_go").click().run()
    p = platform()
    p.run_analysis(p.get_actor("priya"), "exp-001", as_of=pd.Timestamp("2099-01-01", tz="UTC").to_pydatetime())
    st.cache_data.clear()
    again = page("experiments", search_id="exp-001")
    assert any("Final analysis on the full runtime" in c.value for c in again.caption)
    assert any("Verdict" in d.value.columns and (d.value["Verdict"] != "").any() for d in again.dataframe)       # the final table wins over the live one


def test_everyone_sees_the_run_and_edit_buttons_there_are_no_permissions():
    planned_experiment()
    for user in ("marcus", "viewer", "priya"):
        at = page("experiments", user, search_id="exp-001")
        assert at.button(key="run_go") is not None and any(b.key == "edit_plan" for b in at.button), user
        assert not any("Only the owner" in c.value for c in at.caption)


def test_a_failed_gate_is_shown_and_no_results_appear():
    p = platform()
    planned_experiment(eid="exp-bad-1")
    at = page("experiments", search_id="exp-bad-1")
    at.button(key="run_go").click().run()
    assert at.error and "Data-quality gate failed" in at.error[0].value
    assert p.latest_results("exp-bad-1") == [] and p.get_experiment("exp-bad-1")["status"] == "Designed"
    assert len(charts(at)) == 0


def test_the_edit_button_is_there_in_every_status():
    planned_experiment()
    assert any(b.key == "edit_plan" for b in page("experiments", search_id="exp-001").button)
    p = platform()
    p.run_analysis(p.get_actor("priya"), "exp-001", as_of=pd.Timestamp("2099-01-01", tz="UTC").to_pydatetime())
    st.cache_data.clear()
    at = page("experiments", search_id="exp-001")
    assert any(b.key == "edit_plan" for b in at.button)
    assert not any("Edit details" in e.label for e in at.expander) and not any(b.key in ("copy_btn", "details_go") for b in at.button)


def test_there_is_no_decision_form_for_now_and_the_run_button_is_called_run():
    planned_experiment()
    at = page("experiments", search_id="exp-001")
    at.button(key="run_go").click().run()
    assert not [w for w in at.selectbox if w.key == "dec_choice"] and not any(b.key == "dec_go" for b in at.button)
    assert not any(h.value == "Decision" for h in at.subheader)
    assert at.button(key="run_go").label == "Run"                                            # when the final analysis is already there
    p = platform()
    assert p.record_decision(p.get_actor("priya"), "exp-001", "ship", "ok")["status"] == "Decided"   # the backend still supports it for later
    st.cache_data.clear()
    assert not any("Decision:" in x.value for x in page("experiments", search_id="exp-001").success)


# ---- the portfolio ----------------------------------------------------------------------------------------------------
def create_in_ui(user, name, hypothesis="It will help", save=True, baseline=3.5, effect=10.0):
    at = new_form(user)
    at.text_input(key="name").set_value(name)
    at.text_area(key="hypothesis").set_value(hypothesis)
    fill_primary(at, user, baseline, effect)
    assert not at.exception and not at.error
    if save:
        at.button(key="save").click().run()
        assert at.success and not at.error, [e.value for e in at.error]
    return at


def test_each_owner_creates_an_experiment_in_their_product_and_everyone_can_search_for_all_three():
    create_in_ui("priya", "Free shipping banner")
    create_in_ui("marcus", "Subject line test", baseline=5.0)
    create_in_ui("sofia", "Checklist onboarding", baseline=25.0)
    p = platform()
    assert {(e["experiment_id"], e["product_id"], e["owner_user_id"]) for e in p.list_experiments()} == {
        ("exp-001", "checkout", "priya"), ("exp-002", "email", "marcus"), ("exp-003", "onboarding", "sofia")}
    assert all(e["status"] == "Designed" for e in p.list_experiments())
    view = page("experiments", "viewer")
    for eid, name, product in (("exp-001", "Free shipping banner", "checkout"), ("exp-002", "Subject line test", "email"),
                               ("exp-003", "Checklist onboarding", "onboarding")):
        view = search(view, eid)
        assert not view.exception and any(s.value == name for s in view.subheader) and any(f">{product}<" in m.value for m in view.markdown)
    assert all(any(b.key == "edit_plan" for b in view.button) for _ in [0])                      # everyone can edit and run


def test_the_open_target_set_by_save_and_run_opens_that_experiment():
    create_in_ui("priya", "First")
    create_in_ui("marcus", "Second", baseline=5.0)
    at = page("experiments", "viewer", open_target="exp-001")
    assert any(s.value == "First" for s in at.subheader) and at.text_input(key="search_text").value == "exp-001"


def test_the_run_log_and_history_are_kept_in_the_backend_not_shown_on_the_page():
    planned_experiment()
    at = page("experiments", search_id="exp-001")
    at.button(key="run_go").click().run()
    labels = [e.label for e in at.expander]
    assert not any("History" in label or "Last run" in label or "steps" in label for label in labels)
    assert not any("what" in d.value.columns or "step" in d.value.columns for d in at.dataframe)
    p = platform()
    assert p.list_jobs("exp-001") and p.job_steps(p.list_jobs("exp-001")[0]["job_id"]) and p.experiment_history("exp-001")   # but they are stored


# ---- end date, editing, copying and segments ------------------------------------------------------------------------------
def test_the_form_has_an_optional_end_date_that_extends_the_experiment():
    at = new_form()
    at.text_input(key="name").set_value("Longer run")
    at.date_input(key="launch").set_value(date(2026, 1, 5))
    at.number_input(key="runtime").set_value(14)
    fill_primary(at)
    assert at.date_input(key="end").value is None and any("Planned last day: 2026-01-18" in c.value for c in at.caption)
    at.date_input(key="end").set_value(date(2026, 2, 15)).run()
    assert any("Ends 2026-02-15 (42 days in total)" in c.value for c in at.caption)
    at.button(key="save").click().run()
    assert at.success and not at.error, [e.value for e in at.error]
    e = platform().get_experiment("exp-001")
    assert (e["runtime_days"], e["end_date"]) == (14, date(2026, 2, 15))                            # the planned runtime is kept; the end is later


def test_a_bad_end_date_is_refused_with_the_reason():
    at = new_form()
    at.text_input(key="name").set_value("Backwards")
    at.date_input(key="launch").set_value(date(2026, 1, 5))
    fill_primary(at)
    at.date_input(key="end").set_value(date(2026, 1, 1)).run()
    at.button(key="save").click().run()
    assert at.error and "before the launch" in at.error[0].value


def test_save_and_run_follows_the_end_date_not_the_planned_runtime():
    at = new_form()
    at.text_input(key="name").set_value("Still going")
    at.date_input(key="launch").set_value(date.today() - timedelta(days=30))
    at.number_input(key="runtime").set_value(14)                                                    # planned to finish 17 days ago
    at.date_input(key="end").set_value(date.today() + timedelta(days=5))                            # but it is being kept running
    fill_primary(at)
    at.button(key="save_run").click().run()
    assert not at.exception and not at.error, [e.value for e in at.error]
    p = platform()
    assert p.get_experiment("exp-001")["status"] == "Running" and p.latest_results("exp-001") == [] and p.latest_results("exp-001", "interim")


def test_edit_plan_loads_the_end_date():
    planned_experiment(end_date=date(2026, 3, 1))
    at = page("catalog", "priya", catalog_edit="exp-001")
    assert at.date_input(key="end").value == date(2026, 3, 1) and not at.error


def table_for(at, columns=("Verdict",)):
    """The metric table currently shown on the Tables tab: the first dataframe that has the result columns."""
    return next(d.value for d in at.dataframe if "Lift" in d.value.columns)


def tables_with_users(at):
    """(title, table) pairs for the segment or filter views: the markdown title line is followed by its dataframe."""
    return [m.value for m in at.markdown if m.value.startswith("**") and "users (" in m.value and " control," in m.value]


def test_the_tables_tab_shows_everyone_by_default_and_the_dropdowns_list_only_the_plan():
    planned_experiment(dimensions=["plan_tier", "customer_type"], filters=["existing_user"])
    at = page("experiments", search_id="exp-001")
    at.button(key="run_go").click().run()
    assert [t.label for t in at.tabs] == ["Tables", "Charts"]
    seg, flt = at.selectbox(key="seg_pick_exp-001"), at.selectbox(key="flt_pick_exp-001")
    assert seg.value == "" and flt.value == ""                                                      # Everyone, No filter
    assert seg.options == ["Everyone", "Customer type at entry", "Plan tier at entry"] and flt.options == ["No filter", "Existing user"]
    main = table_for(at)
    assert list(main.Metric) == ["Conversion rate", "Refund rate"] and main.iloc[0]["Users (control / variant)"] == "450 / 450" and "Power" in main.columns
    assert not any("Exploratory" in c.value for c in at.caption) and tables_with_users(at) == []   # the main table is the real result, a single table


def test_picking_a_segment_shows_one_titled_table_for_each_of_its_values():
    planned_experiment(dimensions=["plan_tier"])
    at = page("experiments", search_id="exp-001")
    at.button(key="run_go").click().run()
    at.selectbox(key="seg_pick_exp-001").set_value("plan_tier").run()
    titles = tables_with_users(at)
    assert len(titles) == 2 and "Plan tier at entry = a" in titles[0] and "600 users (300 control, 300 variant)" in titles[0]
    assert "Plan tier at entry = b" in titles[1] and "300 users (150 control, 150 variant)" in titles[1]
    tables = [d.value for d in at.dataframe if "Lift" in d.value.columns]
    assert len(tables) == 2 and all("Power" not in t.columns for t in tables)                       # one table per value, nothing for everyone
    assert "Significant improvement" in tables[0].iloc[0].Verdict and "No significant difference" in tables[1].iloc[0].Verdict
    assert any("Exploratory" in c.value for c in at.caption)
    at.selectbox(key="seg_pick_exp-001").set_value("").run()
    assert tables_with_users(at) == [] and table_for(at).iloc[0]["Users (control / variant)"] == "450 / 450"        # back to everyone
    assert len(charts(at)) == 2                                                                      # nothing per segment: only the two metric charts


def test_picking_a_filter_shows_only_the_users_who_pass_it_and_combines_with_a_segment():
    planned_experiment(dimensions=["plan_tier"], filters=["existing_user"])
    at = page("experiments", search_id="exp-001")
    at.button(key="run_go").click().run()
    at.selectbox(key="flt_pick_exp-001").set_value("existing_user").run()
    titles = tables_with_users(at)
    assert len(titles) == 1 and "Users passing Existing user" in titles[0]
    assert table_for(at).iloc[0]["Users (control / variant)"] == "338 / 338"                        # fewer users than everyone
    at.selectbox(key="seg_pick_exp-001").set_value("plan_tier").run()
    titles = tables_with_users(at)
    assert len(titles) == 2 and all("Plan tier at entry = " in t and "Users passing Existing user" in t for t in titles)                    # one table per segment value, inside the filter
    assert "450 users" in titles[0] and "225 control" in titles[0]


def test_segment_and_filter_views_while_running_are_live_numbers_without_verdicts():
    planned_experiment(eid="exp-seg", launch=date.today() - timedelta(days=3), dimensions=["plan_tier"], filters=["existing_user"])
    at = page("experiments", search_id="exp-seg")
    at.button(key="monitor_go").click().run()
    at.selectbox(key="seg_pick_exp-seg").set_value("plan_tier").run()
    at.selectbox(key="flt_pick_exp-seg").set_value("existing_user").run()
    tables = [d.value for d in at.dataframe if "Lift" in d.value.columns]
    assert len(tables) == 2 and all(not {"Verdict", "p-value", "CI"} & set(t.columns) and t.iloc[0].Difference != "" for t in tables)
    assert any("Live numbers only" in c.value for c in at.caption)


def test_the_dropdowns_are_disabled_when_the_plan_has_no_segments_or_filters():
    planned_experiment()
    at = page("experiments", search_id="exp-001")
    at.button(key="run_go").click().run()
    assert at.selectbox(key="seg_pick_exp-001").disabled and at.selectbox(key="flt_pick_exp-001").disabled


def test_the_header_shows_progress_through_the_experiment():
    def header(eid):
        return " ".join(m.value for m in page("experiments", search_id=eid).markdown)
    running_experiment()                                                              # launched three days ago, 14 days planned
    assert "Day 4 of 14" in header("exp-001")
    st.cache_data.clear()                                                             # the test writes through the service, not the app
    planned_experiment(eid="exp-old")                                                 # ran in January
    assert "Ended 2026-01-14" in header("exp-old")
    st.cache_data.clear()
    planned_experiment(eid="exp-new", launch=date.today() + timedelta(days=3))
    assert "Starts in 3 days" in header("exp-new")
    st.cache_data.clear()
    planned_experiment(eid="exp-long", launch=date.today() - timedelta(days=19), end_date=date.today() + timedelta(days=10))
    text = header("exp-long")
    assert "Day 20 of 30" in text and "planned 14" in text                           # extended past the planned runtime


def test_the_page_has_no_plan_box_and_no_dev_simulator():
    running_experiment()
    at = page("experiments", search_id="exp-001")
    assert not any("Plan and audience" in e.label for e in at.expander)
    assert not any("Dev only" in e.label for e in at.expander)


# ---- the Experiment catalog ------------------------------------------------------------------------------------------
def test_the_catalog_page_has_the_two_sub_tabs():
    at = page("catalog")
    assert [t.label for t in at.tabs][:2] == ["Experiments", "Metrics"]
    assert not at.exception and any("No experiments yet" in i.value for i in at.info)


def row_texts(at):
    """Every markdown and text cell of the catalog list, in order."""
    return [m.value for m in at.markdown]


def test_the_experiment_catalog_lists_live_experiments_first_with_progress_and_verdict():
    planned_experiment()                                                                           # priya: ran in January, then analysed
    p = platform()
    p.run_analysis(p.get_actor("priya"), "exp-001", as_of=pd.Timestamp("2099-01-01", tz="UTC").to_pydatetime())
    planned_experiment("marcus", "exp-002", launch=date.today() - timedelta(days=3))              # marcus: live, day 4 of 14
    planned_experiment("priya", "exp-003", launch=date.today() + timedelta(days=5))              # not started
    st.cache_data.clear()
    at = page("catalog", "viewer")
    assert not at.exception
    cells = row_texts(at)
    assert [c for c in cells if c.startswith("**exp-")] == ["**exp-002**", "**exp-003**", "**exp-001**"]   # live, then not started, then ended
    assert [h for h in cells if h in ("**Experiment**", "**Verdict**", "**Progress**", "**Launch to end**")] == ["**Experiment**", "**Launch to end**", "**Verdict**", "**Progress**"]
    joined = " ".join(cells)
    assert "3 experiments" in joined and "1 live" in joined and "1 not started" in joined and "1 ended" in joined             # the summary line
    bars = [c for c in cells if 'class="pbar"' in c]
    assert [("Live, day 4 of 14" in b) for b in bars] == [True, False, False] and "Starts in 5 days" in bars[1] and "Ended, 14 days" in bars[2]
    assert "#3b82f6" in bars[0] and "#eab308" in bars[1] and "#22c55e" in bars[2]                        # blue while live, yellow before, green after
    assert joined.count("Significant improvement") == 1 and joined.count("Not run yet") == 2 and "Conversion rate" in cells and "Click rate" in cells
    assert "rgba(34,197,94" in [c for c in cells if "Significant improvement" in c][0]                       # a green chip for the good verdict
    captions = [c.value for c in at.caption]
    assert "Priya" in captions and "Marcus" in captions and "Banner" in captions                            # short names under the product; the name under the id
    assert not any("(Checkout owner)" in c for c in captions) and f"to {date.today() + timedelta(days=-3 + 13)}" in captions
    assert len([b for b in at.button if b.key.startswith("cat_open_")]) == 3 and len([b for b in at.button if b.key.startswith("cat_edit_")]) == 3
    assert at.text_input(key="cat_search") is not None


def test_the_catalog_header_has_no_empty_labels():
    planned_experiment()
    st.cache_data.clear()
    cells = row_texts(page("catalog"))
    assert "****" not in cells and "** **" not in cells and all(c.strip() not in ("**", "") for c in cells)


def test_the_verdict_column_shows_the_final_verdict_only_after_the_final_analysis():
    planned_experiment()
    p = platform()
    assert p.experiment_catalog()[0]["verdict"] is None                                         # not run yet
    p.refresh_monitor(p.get_actor("priya"), "exp-001", as_of=pd.Timestamp("2026-01-05", tz="UTC").to_pydatetime())
    assert p.experiment_catalog()[0]["verdict"] is None                                         # live numbers are not a verdict
    p.run_analysis(p.get_actor("priya"), "exp-001", as_of=pd.Timestamp("2026-02-01", tz="UTC").to_pydatetime())
    assert p.experiment_catalog()[0]["verdict"] == "Significant improvement"
    p.update_experiment(p.get_actor("priya"), "exp-001", "Banner", "h", date(2026, 1, 1), 14, end_date=date.today() + timedelta(days=9))
    p.refresh_monitor(p.get_actor("priya"), "exp-001", as_of=pd.Timestamp("2026-02-10", tz="UTC").to_pydatetime())
    assert p.experiment_catalog()[0]["verdict"] is None                                         # extended and live again: no verdict yet
    assert {"primary_metric", "end_date", "status"} <= set(p.experiment_catalog()[0])


def test_the_experiment_catalog_filters_only_by_experiment_id():
    planned_experiment()
    planned_experiment("marcus", "exp-002")
    st.cache_data.clear()
    at = page("catalog")
    assert at.text_input(key="cat_search").label == "Experiment ID" and not [s for s in at.selectbox if s.key.startswith("cat_")]
    at.text_input(key="cat_search").set_value("EXP-002").run()
    assert [c for c in row_texts(at) if c.startswith("**exp-")] == ["**exp-002**"]
    at.text_input(key="cat_search").set_value("zzz").run()
    assert any("No experiments match" in i.value for i in at.info)


def test_edit_and_open_results_are_offered_to_everyone():
    planned_experiment()
    st.cache_data.clear()
    for user in ("priya", "admin", "marcus", "viewer"):
        at = page("catalog", user)
        assert any(b.key == "cat_edit_exp-001" for b in at.button) and any(b.key == "cat_open_exp-001" for b in at.button), user


def test_edit_opens_the_form_in_place_and_open_results_goes_to_the_experiment():
    planned_experiment()
    st.cache_data.clear()
    at = page("catalog", "priya")
    at.button(key="cat_edit_exp-001").click().run()
    assert any(h.value == "Edit exp-001" for h in at.subheader) and at.text_input(key="name").value == "Banner"
    assert at.text_input(key="exp_id").disabled
    other = page("catalog", "priya")
    other.button(key="cat_open_exp-001").click().run()
    assert other.session_state["open_target"] == "exp-001"


def test_someone_who_is_not_the_owner_can_open_the_edit_form_too():
    planned_experiment()
    st.cache_data.clear()
    at = page("catalog", "marcus", catalog_edit="exp-001")
    assert not at.exception and at.text_input(key="name").value == "Banner" and any(b.key == "save" for b in at.button)


def test_the_edit_button_on_the_experiments_page_leads_to_the_catalog_edit():
    planned_experiment()
    at = page("experiments", search_id="exp-001")
    at.button(key="edit_plan").click().run()
    assert at.session_state["catalog_edit"] == "exp-001"


def test_progress_says_whether_the_test_is_live_and_how_far_it_is():
    import experiment_catalog as ec
    d = date(2026, 6, 10)
    assert ec.progress_of(date(2026, 6, 1), date(2026, 6, 14), d) == (10 / 14, "Live, day 10 of 14")
    assert ec.progress_of(date(2026, 6, 10), date(2026, 6, 23), d) == (1 / 14, "Live, day 1 of 14")
    assert ec.progress_of(date(2026, 6, 1), date(2026, 6, 14), date(2026, 6, 15)) == (1.0, "Ended, 14 days")
    assert ec.progress_of(date(2026, 6, 14), date(2026, 6, 27), d) == (0.0, "Starts in 4 days")
    assert ec.progress_of(date(2026, 6, 1), date(2026, 6, 30), d) == (10 / 30, "Live, day 10 of 30")    # extended past the planned runtime


def test_the_app_shell_has_three_pages_on_top_and_no_sidebar_or_sign_in():
    shell = (APP / "streamlit_app.py").read_text()
    assert 'position="top"' in shell and "sidebar" not in shell and "Signed in" not in shell and "actor_id" not in shell
    titles = [line.split('title="')[1].split('"')[0] for line in shell.splitlines() if "st.Page(" in line]
    assert titles == ["Experiment Catalog", "Experiment Results", "Add Experiment"]
    assert 'default=True' in [line for line in shell.splitlines() if "Experiment Catalog" in line][0]          # the catalog opens first
    for page_name, heading in (("catalog", "Experiment Catalog"), ("experiments", "Experiment Results"), ("design", "Add Experiment")):
        assert page(page_name).title[0].value == heading


def test_the_headline_cards_read_control_then_variant_then_lift_then_verdict():
    planned_experiment()
    at = page("experiments", search_id="exp-001")
    assert [m.label for m in at.metric] == []                                                      # no numbers yet, so no cards
    at.button(key="run_go").click().run()
    assert [m.label for m in at.metric] == ["Conversion rate · control", "Conversion rate · variant", "Lift (variant vs control)"]
    control, variant, lift = at.metric
    assert (control.value, variant.value, lift.value, lift.delta) == ("10.000%", "16.000%", "+60.0%", "+6.000 pp")
    verdict = [m.value for m in at.markdown if 'class="verdict-card"' in m.value][0]
    assert "Significant improvement" in verdict and "900 users (450 control, 450 variant)" in verdict


def test_the_results_page_explains_the_colours_and_shows_a_short_owner_name():
    planned_experiment()
    at = page("experiments", search_id="exp-001")
    at.button(key="run_go").click().run()
    assert any("Colour key" in c.value and "lower is better" in c.value for c in at.caption)
    header = " ".join(m.value for m in at.markdown)
    assert "<b>Owner</b> Priya &nbsp;" in header and "(Checkout owner)" not in header


def test_the_search_and_dropdowns_are_compact_not_full_width():
    shell = (APP / "views" / "experiments.py").read_text()
    assert "st.columns([3, 1, 6]" in shell and "st.columns([3, 3, 4])" in shell
    assert "st.columns([3, 7]" in (APP / "experiment_catalog.py").read_text()


# ---- Runs in the background ----------------------------------------------------------------------------------------------
def background_runs():
    """Switch the app's platform from running in the caller's thread to the real background queue, with a runner that waits for a gate."""
    import threading
    p = platform()
    p.runs.inline = False
    gate = threading.Event()
    original = p.runner.build

    def slow(*a, **k):
        gate.wait(5)
        return original(*a, **k)

    p.runner.build = slow
    return p, gate


def test_clicking_run_queues_it_and_shows_the_status_instead_of_making_you_wait():
    planned_experiment()
    p, gate = background_runs()
    at = page("experiments", search_id="exp-001")
    at.button(key="run_go").click().run()
    assert not at.exception and not at.error
    assert any(("running for" in i.value) or ("Waiting in line" in i.value) for i in at.info)                 # the status panel
    busy = [b for b in at.button if b.key == "run_busy"]
    assert busy and busy[0].disabled and not [b for b in at.button if b.key in ("run_go", "monitor_go")]       # no second Run while one is in progress
    assert p.get_experiment("exp-001")["status"] == "Designed" and p.latest_results("exp-001") == []           # the numbers are not there yet
    assert not at.tabs and not at.metric and not at.dataframe                                                  # and no old numbers are shown while it runs
    assert any("hidden while this run rebuilds" in c.value for c in at.caption)
    gate.set()
    p.runs.wait_all(5)
    st.cache_data.clear()
    again = page("experiments", search_id="exp-001")
    assert not again.exception and any("Final analysis finished" in x.value for x in again.success)             # finished: the page announces it and shows the numbers
    assert p.get_experiment("exp-001")["status"] == "Analyzed" and any("Verdict" in d.value.columns for d in again.dataframe)


def test_someone_else_opening_the_experiment_sees_the_run_in_progress_too():
    planned_experiment()
    p, gate = background_runs()
    page("experiments", search_id="exp-001").button(key="run_go").click().run()
    other = page("experiments", "marcus", search_id="exp-001")
    assert any("running for" in i.value or "Waiting in line" in i.value for i in other.info)
    assert any(b.key == "run_busy" and b.disabled for b in other.button)
    gate.set()
    p.runs.wait_all(5)


def test_a_failed_background_run_shows_why_and_can_be_dismissed():
    planned_experiment(eid="exp-bad-1")
    p, gate = background_runs()
    gate.set()
    at = page("experiments", search_id="exp-bad-1")
    at.button(key="run_go").click().run()
    p.runs.wait_all(5)
    again = page("experiments", search_id="exp-bad-1")
    assert any("The run failed" in e.value and "Data-quality gate failed" in e.value for e in again.error)
    again.button(key="run_dismiss").click().run()
    assert not any("The run failed" in e.value for e in again.error) and any(b.key == "run_go" for b in again.button)      # and Run is available again


def test_the_catalog_marks_experiments_with_a_run_in_progress():
    planned_experiment()
    p, gate = background_runs()
    page("experiments", search_id="exp-001").button(key="run_go").click().run()
    cells = " ".join(m.value for m in page("catalog").markdown)
    assert "Run in progress" in cells
    gate.set()
    p.runs.wait_all(5)
    assert "Run in progress" not in " ".join(m.value for m in page("catalog").markdown)


def test_save_and_run_queues_the_run_and_sends_you_to_the_results():
    p, gate = background_runs()
    at = new_form(exp_id="exp-001")
    at.text_input(key="name").set_value("Background run")
    at.date_input(key="launch").set_value(date.today() - timedelta(days=3))
    fill_primary(at)
    at.button(key="save_run").click().run()
    assert not at.exception and not at.error, [e.value for e in at.error]
    assert p.get_experiment("exp-001")["status"] == "Designed" and p.run_status("exp-001")["state"] in ("queued", "running")   # saved at once, still running
    assert at.session_state["open_target"] == "exp-001" and any("in the background" in x.value for x in at.success)
    gate.set()
    p.runs.wait_all(5)
    assert p.get_experiment("exp-001")["status"] == "Running"




def test_old_numbers_are_hidden_while_a_new_run_is_in_progress_even_when_there_were_numbers_before():
    planned_experiment()
    p = platform()
    p.run_analysis(p.get_actor("priya"), "exp-001", as_of=pd.Timestamp("2099-01-01", tz="UTC").to_pydatetime())
    st.cache_data.clear()
    assert page("experiments", search_id="exp-001").dataframe                                                  # numbers are shown normally
    p, gate = background_runs()
    at = page("experiments", search_id="exp-001")
    at.button(key="run_go").click().run()
    assert any("running for" in i.value or "Waiting in line" in i.value for i in at.info)
    assert not at.tabs and not at.metric and not at.dataframe                                                  # the earlier results are not shown during the run
    gate.set()
    p.runs.wait_all(5)
    st.cache_data.clear()
    again = page("experiments", search_id="exp-001")
    assert again.tabs and again.dataframe                                                                      # and they are back, fresh, afterwards


def test_after_a_failed_run_the_old_numbers_are_labelled_as_from_the_last_successful_run():
    planned_experiment()
    p = platform()
    p.run_analysis(p.get_actor("priya"), "exp-001", as_of=pd.Timestamp("2099-01-01", tz="UTC").to_pydatetime())
    from p2.pipeline.runner import DataQualityError
    p.runner.build = lambda *a, **k: (_ for _ in ()).throw(DataQualityError("exp-001: no assignments between 2026-09-22 and 2026-09-30"))
    st.cache_data.clear()
    at = page("experiments", search_id="exp-001")
    at.button(key="run_go").click().run()
    again = page("experiments", search_id="exp-001")
    assert any("The run failed" in e.value or "last run failed" in e.value for e in again.error) or at.error
    shown = list(at.warning) + list(again.warning)
    assert any("latest run failed" in w.value and "last successful run" in w.value for w in shown)
    assert len([w for w in again.warning if "numbers" in w.value]) == 1                                         # one message, not several
    assert again.dataframe                                                                                    # the earlier numbers are still there, but labelled


def test_changing_the_launch_date_after_a_run_flags_the_numbers_as_out_of_date_until_the_next_run():
    planned_experiment()
    p = platform()
    a = p.get_actor("priya")
    p.run_analysis(a, "exp-001", as_of=pd.Timestamp("2099-01-01", tz="UTC").to_pydatetime())
    st.cache_data.clear()
    fresh = page("experiments", search_id="exp-001")
    assert not any("out of date" in w.value for w in fresh.warning)                                          # nothing changed: no warning
    p.update_experiment(a, "exp-001", "Banner", "Lifts conversion", date(2026, 1, 5), 14)                    # the launch date moves from 1 to 5 January
    st.cache_data.clear()
    stale = page("experiments", search_id="exp-001")
    warn = [w.value for w in stale.warning if "out of date" in w.value]
    assert warn and "launch date is now 2026-01-05 (it was 2026-01-01)" in warn[0] and "Press Run" in warn[0]
    assert stale.dataframe                                                                                    # the old numbers are still there, but clearly marked
    stale.button(key="run_go").click().run()
    st.cache_data.clear()
    assert not any("out of date" in w.value for w in page("experiments", search_id="exp-001").warning)      # a new Run clears it


def test_changing_the_end_date_or_the_plan_numbers_is_flagged_too_but_a_name_change_is_not():
    planned_experiment()
    p = platform()
    a = p.get_actor("priya")
    p.run_analysis(a, "exp-001", as_of=pd.Timestamp("2099-01-01", tz="UTC").to_pydatetime())
    assert p.plan_changes_since_run("exp-001") == []
    p.update_experiment(a, "exp-001", "A new name", "A new hypothesis", date(2026, 1, 1), 14)
    assert p.plan_changes_since_run("exp-001") == []                                                           # labels do not change the numbers
    p.update_experiment(a, "exp-001", "A new name", "h", date(2026, 1, 1), 14, end_date=date(2026, 2, 1))
    assert p.plan_changes_since_run("exp-001") == ["the end date is now 2026-02-01 (it was 2026-01-14)"]
    p.update_experiment(a, "exp-001", "A new name", "h", date(2026, 1, 1), 14)                                  # back to the original dates
    reg = p.metric_registry()
    from p2.stats.plan import MetricPlan
    p.save_design(a, "exp-001", MetricPlan(reg.get("conversion_rate"), "primary", 0.0374, 0.2, "relative", 0.05, 0.8, "two-sided"),
                  [MetricPlan(reg.get("refund_rate"), "guardrail", 0.0047, 0.25, "relative", 0.05, 0.8, "one-sided")], [])     # a different effect size
    assert p.plan_changes_since_run("exp-001") == ["the metrics, segments, filters or statistical numbers in the plan were changed"]
    p.run_analysis(a, "exp-001", as_of=pd.Timestamp("2099-01-01", tz="UTC").to_pydatetime())
    assert p.plan_changes_since_run("exp-001") == []


def test_an_experiment_that_has_never_run_has_no_out_of_date_warning():
    planned_experiment()
    assert platform().plan_changes_since_run("exp-001") == []


def test_a_run_from_before_fingerprints_is_still_flagged_when_its_last_day_no_longer_matches():
    planned_experiment()
    p = platform()
    a = p.get_actor("priya")
    p.run_analysis(a, "exp-001", as_of=pd.Timestamp("2099-01-01", tz="UTC").to_pydatetime())
    job = p.list_jobs("exp-001")[0]
    old_detail = {k: v for k, v in job["detail"].items() if k not in ("plan", "launch", "end")}
    p.store.update("job_runs", {"job_id": job["job_id"]}, {"detail": old_detail})                               # as an older Run recorded it
    assert p.plan_changes_since_run("exp-001") == []
    p.update_experiment(a, "exp-001", "Banner", "h", date(2026, 1, 5), 14)                                       # the last day moves from 14 to 18 January
    assert p.plan_changes_since_run("exp-001") == ["the experiment now ends 2026-01-18 (these numbers run through 2026-01-14)"]


def test_a_failed_run_after_a_plan_change_gives_one_combined_message():
    planned_experiment()
    p = platform()
    a = p.get_actor("priya")
    p.run_analysis(a, "exp-001", as_of=pd.Timestamp("2099-01-01", tz="UTC").to_pydatetime())
    from p2.pipeline.runner import DataQualityError
    p.update_experiment(a, "exp-001", "Banner", "h", date(2026, 1, 5), 14)
    p.runner.build = lambda *a, **k: (_ for _ in ()).throw(DataQualityError("exp-001: no assignments between 2026-01-05 and 2026-01-18"))
    st.cache_data.clear()
    at = page("experiments", search_id="exp-001")
    at.button(key="run_go").click().run()
    again = page("experiments", search_id="exp-001")
    numbers = [w.value for w in again.warning if "numbers" in w.value]
    assert len(numbers) == 1 and "out of date" in numbers[0] and "launch date is now 2026-01-05" in numbers[0] and "latest run failed" in numbers[0]


def test_a_run_refused_for_having_no_users_in_the_window_says_what_to_check():
    planned_experiment(eid="exp-bad-1")
    p, gate = background_runs()
    gate.set()
    p.runner.build = lambda *a, **k: (_ for _ in ()).throw(__import__("p2.pipeline.runner", fromlist=["DataQualityError"]).DataQualityError(
        "exp-bad-1: no assignments between 2026-09-22 and 2026-09-30, but the log has 5,000 between 2026-09-01 and 2026-09-08; check the launch date and runtime"))
    at = page("experiments", search_id="exp-bad-1")
    at.button(key="run_go").click().run()
    p.runs.wait_all(5)
    again = page("experiments", search_id="exp-bad-1")
    assert any("no assignments between 2026-09-22" in e.value for e in again.error)
    assert any("Check the launch and end dates" in c.value and "September" in c.value for c in again.caption)



# ---- the SRM card ----------------------------------------------------------------------------------------------------
def srm_card_text(at):
    return " ".join(m.value for m in at.markdown if "balanced" in m.value.lower() or "split looks wrong" in m.value.lower())


def refreshed_experiment():
    running_experiment()
    p = platform()
    p.refresh_monitor(p.get_actor("priya"), "exp-001", as_of=pd.Timestamp("2099-01-01", tz="UTC").to_pydatetime())
    st.cache_data.clear()
    return p


def test_the_results_page_says_the_test_is_balanced_when_the_split_is_even():
    refreshed_experiment()
    assert "The test is balanced" in srm_card_text(page("experiments", search_id="exp-001"))


def test_the_results_page_warns_when_the_split_is_off():
    from p2.stats.tests import Arm
    p = platform()
    running_experiment()
    p.runner.summary_stats = lambda table, ids: {m: {"control": Arm(450, 0.10, 0.09), "variant": Arm(300, 0.16, 0.1344)} for m in ids}
    p.refresh_monitor(p.get_actor("priya"), "exp-001", as_of=pd.Timestamp("2099-01-01", tz="UTC").to_pydatetime())
    st.cache_data.clear()
    text = srm_card_text(page("experiments", search_id="exp-001"))
    assert "The split looks wrong" in text and "60.0 / 40.0" in text


def test_no_srm_card_before_there_are_numbers():
    running_experiment()
    assert srm_card_text(page("experiments", search_id="exp-001")) == ""
