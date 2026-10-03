"""Service-layer behaviour on the in-memory store: permissions, validation, the plan, analysis, decisions, audit."""
from datetime import date, datetime, timedelta, timezone

import pytest

from p2.catalog.seed import seed_registry
from p2.services import errors
from p2.services.platform import SEED_MEMBERS, Platform
from p2.stats.plan import MetricPlan
from p2.store.memory import MemoryStore
from p2.store.schema import APP_TABLES
from p2.testing import FakeChecker, FakeRunner
from p2.warehouse.sources import load_sources

reg = seed_registry()
LAUNCH = date(2026, 1, 1)


class Clock:
    """A clock that moves forward a second each call, so ordering is deterministic."""
    def __init__(self):
        self.t = datetime(2026, 2, 1, tzinfo=timezone.utc)

    def __call__(self):
        self.t += timedelta(seconds=1)
        return self.t


@pytest.fixture
def plat():
    clock = Clock()
    p = Platform(MemoryStore(APP_TABLES, clock), load_sources(), clock, checker=FakeChecker(), runner=FakeRunner())
    p.bootstrap()
    return p


def who(plat, user_id):
    return plat.get_actor(user_id)


def primary(metric="conversion_rate", baseline=0.0374, effect=0.10, kind="relative", std=None, sided="two-sided"):
    return MetricPlan(reg.get(metric), "primary", baseline, effect, kind, 0.05, 0.8, sided, std)


def guard(metric="refund_rate", baseline=0.0047, effect=0.25):
    return MetricPlan(reg.get(metric), "guardrail", baseline, effect, "relative", 0.05, 0.8, "one-sided")


def make(plat, eid="exp-001", user="priya", product="checkout", **kw):
    return plat.create_experiment(who(plat, user), eid, product, kw.get("name", "Banner"), kw.get("hyp", "Lifts conversion"),
                                  kw.get("launch", LAUNCH), kw.get("runtime", 14))


# ---- bootstrap -----------------------------------------------------------------------------------
def test_bootstrap_seeds_team_products_and_nine_sources(plat):
    assert [m["user_id"] for m in plat.list_team()] == sorted(u for u, *_ in SEED_MEMBERS)
    assert [p["product_id"] for p in plat.list_products()] == ["checkout", "email", "onboarding"]
    assert len(plat.list_sources()) == 9
    assert {s["source_id"] for s in plat.list_sources("email")} == {
        "exp_assignments", "users", "plan_history", "email_sends", "email_events"}
    src = {s["source_id"]: s for s in plat.list_sources()}["checkout_orders"]
    assert src["staging_view"] == "stg_checkout_orders" and src["dedupe_key"] == ["order_id"]
    assert {c["name"] for c in src["schema"]} >= {"order_id", "status", "refund_ts"}
    assert all(c["description"] for c in src["schema"])


def test_bootstrap_is_idempotent_and_reports_what_it_created(plat):
    assert plat.bootstrap() == {"team_members": 0, "products": 0, "data_sources": 0, "catalog_items": 0}
    assert len(plat.store.select("audit_log", {"action": "seed"})) == 4


def test_get_actor(plat):
    a = who(plat, "marcus")
    assert (a.role, a.product_id) == ("metric_owner", "email")
    with pytest.raises(errors.NotFound):
        plat.get_actor("ghost")
    plat.store.update("team_members", {"user_id": "viewer"}, {"active": False})
    with pytest.raises(errors.PermissionDenied, match="deactivated"):
        plat.get_actor("viewer")


# ---- creating and editing experiments ------------------------------------------------------------
def test_owner_creates_in_their_product(plat):
    row = make(plat)
    assert (row["status"], row["owner_user_id"], row["product_id"]) == ("Draft", "priya", "checkout")
    assert (row["launch_date"], row["runtime_days"]) == (LAUNCH, 14)
    audit = plat.list_audit(who(plat, "admin"), "exp-001")
    assert [a["action"] for a in audit] == ["experiment.create"] and audit[0]["actor"] == "priya"


def test_the_owner_can_be_someone_other_than_the_person_acting(plat):
    row = plat.create_experiment(who(plat, "admin"), "exp-001", "checkout", "Banner", "h", LAUNCH, 14, owner_user_id="priya")
    assert row["owner_user_id"] == "priya"
    audit = plat.list_audit(who(plat, "admin"), "exp-001")
    assert audit[0]["actor"] == "admin" and audit[0]["detail"]["owner"] == "priya"
    assert make(plat, "exp-002", user="priya")["owner_user_id"] == "priya"                       # no owner given: the actor
    with pytest.raises(errors.InvalidInput, match="unknown or inactive team member"):
        plat.create_experiment(who(plat, "admin"), "exp-003", "checkout", "n", "h", LAUNCH, 14, owner_user_id="ghost")
    plat.store.update("team_members", {"user_id": "marcus"}, {"active": False})
    with pytest.raises(errors.InvalidInput, match="unknown or inactive team member"):
        plat.create_experiment(who(plat, "admin"), "exp-004", "email", "n", "h", LAUNCH, 14, owner_user_id="marcus")


def test_owner_and_product_can_be_changed_by_editing(plat):
    plat.create_experiment(who(plat, "admin"), "exp-001", "checkout", "Banner", "h", LAUNCH, 14, owner_user_id="priya")
    row = plat.update_experiment(who(plat, "admin"), "exp-001", "Banner", "h", LAUNCH, 14, owner_user_id="sofia", product_id="onboarding")
    assert (row["owner_user_id"], row["product_id"]) == ("sofia", "onboarding")
    with pytest.raises(errors.InvalidInput, match="unknown product"):
        plat.update_experiment(who(plat, "admin"), "exp-001", "Banner", "h", LAUNCH, 14, product_id="billing")
    with pytest.raises(errors.InvalidInput, match="unknown or inactive"):
        plat.update_experiment(who(plat, "admin"), "exp-001", "Banner", "h", LAUNCH, 14, owner_user_id="ghost")
    assert plat.get_experiment("exp-001")["owner_user_id"] == "sofia"


def test_the_hypothesis_is_optional(plat):
    assert make(plat, hyp="")["hypothesis"] == ""
    assert make(plat, "exp-002", hyp=None)["hypothesis"] == ""


def test_create_permissions(plat):
    with pytest.raises(errors.PermissionDenied, match="works in email"):
        make(plat, user="marcus", product="checkout")
    with pytest.raises(errors.PermissionDenied, match="read-only"):
        make(plat, user="viewer", product="checkout")
    assert make(plat, eid="exp-002", user="admin", product="onboarding")["owner_user_id"] == "admin"


@pytest.mark.parametrize("kw,msg", [
    (dict(name=" "), "name is required"), (dict(runtime=0), "runtime"), (dict(runtime=366), "runtime"),
    (dict(runtime=7.5), "runtime"), (dict(runtime=True), "runtime"), (dict(launch="2026-01-01"), "launch date"),
    (dict(launch=datetime(2026, 1, 1)), "launch date"),
])
def test_create_validation(plat, kw, msg):
    with pytest.raises(errors.InvalidInput, match=msg):
        make(plat, **kw)


@pytest.mark.parametrize("eid", ["Has Caps", "under_score", "", "-lead", "x" * 42])
def test_bad_ids_rejected(plat, eid):
    with pytest.raises(errors.InvalidInput, match="experiment id"):
        make(plat, eid=eid)


def test_duplicate_and_unknown_product(plat):
    make(plat)
    with pytest.raises(errors.InvalidInput, match="already exists"):
        make(plat)
    with pytest.raises(errors.InvalidInput, match="unknown product"):
        plat.create_experiment(who(plat, "admin"), "exp-009", "billing", "n", "h", LAUNCH, 14)


def test_update_by_owner_only(plat):
    make(plat)
    row = plat.update_experiment(who(plat, "priya"), "exp-001", "New name", "New hyp", date(2026, 2, 1), 21)
    assert (row["name"], row["launch_date"], row["runtime_days"]) == ("New name", date(2026, 2, 1), 21)
    plat.update_experiment(who(plat, "admin"), "exp-001", "Admin edit", "h", LAUNCH, 14)
    with pytest.raises(errors.PermissionDenied, match="only the owner"):
        plat.update_experiment(who(plat, "marcus"), "exp-001", "x", "y", LAUNCH, 14)
    with pytest.raises(errors.PermissionDenied):
        plat.update_experiment(who(plat, "viewer"), "exp-001", "x", "y", LAUNCH, 14)


def test_stale_edit_is_a_conflict(plat):
    make(plat)
    first = plat.get_experiment("exp-001")["updated_at"]
    plat.update_experiment(who(plat, "priya"), "exp-001", "Edit 1", "h", LAUNCH, 14, expected_updated_at=first)
    with pytest.raises(errors.Conflict):
        plat.update_experiment(who(plat, "priya"), "exp-001", "Edit 2", "h", LAUNCH, 14, expected_updated_at=first)
    assert plat.get_experiment("exp-001")["name"] == "Edit 1"


def test_list_filters_and_order(plat):
    make(plat, "exp-001")
    make(plat, "exp-002", user="marcus", product="email")
    make(plat, "exp-003", user="priya")
    assert [e["experiment_id"] for e in plat.list_experiments()] == ["exp-003", "exp-002", "exp-001"]  # newest first
    assert [e["experiment_id"] for e in plat.list_experiments(product_id="checkout")] == ["exp-003", "exp-001"]
    assert [e["experiment_id"] for e in plat.list_experiments(owner_user_id="marcus")] == ["exp-002"]
    plat.save_design(who(plat, "priya"), "exp-001", primary(), [], [])
    assert [e["experiment_id"] for e in plat.list_experiments(status="Designed")] == ["exp-001"]
    with pytest.raises(errors.NotFound):
        plat.get_experiment("nope")


# ---- the plan ------------------------------------------------------------------------------------
def test_save_design_stores_the_entered_numbers_pinned(plat):
    make(plat)
    plat.save_design(who(plat, "priya"), "exp-001", primary(), [guard()], ["add_to_cart_rate", "orders_per_user"])
    items = plat.get_design("exp-001")
    assert [(i["item_id"], i["role"]) for i in items] == [
        ("conversion_rate", "primary"), ("refund_rate", "guardrail"), ("add_to_cart_rate", "secondary"), ("orders_per_user", "secondary")]
    assert all(i["item_version"] == 1 and i["kind"] == "metric" for i in items)
    p = items[0]
    assert (p["baseline"], p["effect"], p["effect_kind"], p["alpha"], p["power"], p["sidedness"]) == (0.0374, 0.10, "relative", 0.05, 0.8, "two-sided")
    assert items[2]["alpha"] is None and items[2]["baseline"] is None
    assert plat.get_experiment("exp-001")["status"] == "Designed"
    detail = [a for a in plat.list_audit(who(plat, "admin"), "exp-001") if a["action"] == "experiment.design"][0]["detail"]
    assert detail["primary"] == "conversion_rate" and detail["guardrails"] == ["refund_rate"]


def test_the_tool_does_not_compute_or_second_guess_sample_sizes(plat):
    make(plat)
    plat.save_design(who(plat, "priya"), "exp-001", primary(effect=0.9), [], [])     # an absurd but valid plan is accepted as entered
    row = plat.get_design("exp-001")[0]
    assert row["effect"] == 0.9 and "required_n_total" not in row


def test_redesign_replaces_items(plat):
    make(plat)
    a = who(plat, "priya")
    plat.save_design(a, "exp-001", primary(), [guard()], [])
    plat.save_design(a, "exp-001", primary(), [], ["orders_per_user"])
    assert [i["item_id"] for i in plat.get_design("exp-001")] == ["conversion_rate", "orders_per_user"]


def test_design_permissions(plat):
    make(plat)
    with pytest.raises(errors.PermissionDenied):
        plat.save_design(who(plat, "marcus"), "exp-001", primary(), [], [])
    with pytest.raises(errors.PermissionDenied):
        plat.save_design(who(plat, "viewer"), "exp-001", primary(), [], [])
    plat.save_design(who(plat, "admin"), "exp-001", primary(), [], [])


def test_any_metric_from_any_product_can_be_used_in_any_role(plat):
    make(plat)  # a checkout experiment may use email metrics: the catalog is shared, roles are chosen per experiment
    plat.save_design(who(plat, "priya"), "exp-001", primary("open_rate", baseline=0.37), [guard()], ["unsubscribe_rate"])
    assert [(i["item_id"], i["role"]) for i in plat.get_design("exp-001")] == [
        ("open_rate", "primary"), ("refund_rate", "guardrail"), ("unsubscribe_rate", "secondary")]
    plat.save_design(who(plat, "priya"), "exp-001", primary("refund_rate", baseline=0.005), [], [])  # a former guardrail as primary


def test_a_metric_cannot_hold_two_roles(plat):
    make(plat)
    with pytest.raises(errors.InvalidInput, match="more than once"):
        plat.save_design(who(plat, "priya"), "exp-001", primary(), [guard("conversion_rate", 0.0374, 0.1)], [])
    assert plat.get_design("exp-001") == []


def test_invalid_designs_leave_the_previous_plan_untouched(plat):
    make(plat)
    a = who(plat, "priya")
    plat.save_design(a, "exp-001", primary(), [guard()], [])
    with pytest.raises(errors.InvalidInput, match="more than once"):
        plat.save_design(a, "exp-001", primary(), [], ["conversion_rate"])
    assert [i["item_id"] for i in plat.get_design("exp-001")] == ["conversion_rate", "refund_rate"]


def test_email_and_onboarding_owners_can_plan_their_own(plat):
    plat.create_experiment(who(plat, "marcus"), "exp-e1", "email", "Subject line", "Lifts opens", LAUNCH, 14)
    plat.save_design(who(plat, "marcus"), "exp-e1", primary("open_rate", 0.374), [guard("unsubscribe_rate", 0.006, 0.5)], ["bounce_rate"])
    plat.create_experiment(who(plat, "sofia"), "exp-o1", "onboarding", "Checklist", "Lifts activation", LAUNCH, 14)
    plat.save_design(who(plat, "sofia"), "exp-o1", primary("paid_revenue_per_user", 1.41, std=9.55), [], ["activation_rate"])


def test_status_set_by_hand_moves_forward_only_but_the_plan_is_never_locked(plat):
    make(plat)
    a = who(plat, "priya")
    plat.save_design(a, "exp-001", primary(), [], [])
    plat.set_status(a, "exp-001", "Running")
    with pytest.raises(errors.InvalidInput, match="back"):
        plat.set_status(a, "exp-001", "Designed")
    plat.save_design(a, "exp-001", primary("refund_rate", baseline=0.005), [guard("conversion_rate", 0.0374, 0.1)], ["add_to_cart_rate"])   # any change, any time
    plat.update_experiment(a, "exp-001", "x", "y", LAUNCH, 14, end_date=date(2026, 3, 1))
    assert plat.get_experiment("exp-001")["status"] == "Running"                     # editing never changes the status
    assert [(i["item_id"], i["role"]) for i in plat.get_design("exp-001")][0] == ("refund_rate", "primary")
    with pytest.raises(errors.InvalidInput, match="unknown status"):
        plat.set_status(a, "exp-001", "Shipped")
    with pytest.raises(errors.PermissionDenied):
        plat.set_status(who(plat, "marcus"), "exp-001", "Analyzed")


def test_everything_but_the_id_can_be_changed_in_every_status_even_after_the_decision(plat):
    planned(plat)
    a = who(plat, "priya")
    plat.run_analysis(a, "exp-001", as_of=datetime(2026, 2, 1, tzinfo=timezone.utc))
    plat.record_decision(a, "exp-001", "ship", "looks good")
    plat.update_experiment(a, "exp-001", "Final name", "New hypothesis", date(2026, 1, 2), 30, end_date=date(2026, 2, 20))
    plat.save_design(a, "exp-001", primary(), [], ["orders_per_user"], dimension_ids=["customer_type"], filter_ids=["region_us"])
    e = plat.get_experiment("exp-001")
    assert (e["name"], e["launch_date"], e["runtime_days"], e["end_date"]) == ("Final name", date(2026, 1, 2), 30, date(2026, 2, 20))
    assert (e["status"], e["decision"]) == ("Decided", "ship")                        # the recorded decision stays
    assert {i["item_id"] for i in plat.get_design("exp-001")} == {"conversion_rate", "orders_per_user", "customer_type", "region_us"}
    for user in ("marcus", "viewer"):
        with pytest.raises(errors.PermissionDenied):
            plat.update_experiment(who(plat, user), "exp-001", "x", "y", LAUNCH, 14)


def test_the_status_follows_the_latest_run(plat):
    planned(plat)
    a = who(plat, "priya")
    plat.run_analysis(a, "exp-001", as_of=datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert plat.get_experiment("exp-001")["status"] == "Analyzed"
    plat.update_experiment(a, "exp-001", "n", "h", LAUNCH, 14, end_date=date(2026, 3, 1))      # the run is extended after the verdict
    plat.refresh_monitor(a, "exp-001", as_of=datetime(2026, 2, 10, tzinfo=timezone.utc))
    assert plat.get_experiment("exp-001")["status"] == "Running"                            # live numbers are the latest again
    assert plat.latest_results("exp-001", "interim")[0]["run_at"] > plat.latest_results("exp-001")[0]["run_at"]
    plat.run_analysis(a, "exp-001", as_of=datetime(2026, 3, 2, tzinfo=timezone.utc))
    assert plat.get_experiment("exp-001")["status"] == "Analyzed"


# ---- ground truth, sources, audit ----------------------------------------------------------------
def test_ground_truth_round_trip_replaces(plat):
    make(plat)
    a = who(plat, "priya")
    plat.save_ground_truth(a, "exp-001", {"conversion_rate": {"control": 0.035, "variant": 0.0385, "relative_lift": 0.1}})
    plat.save_ground_truth(a, "exp-001", {"conversion_rate": {"control": 0.04, "variant": 0.044, "relative_lift": 0.1},
                                          "refund_rate": {"control": 0.005, "variant": 0.005, "relative_lift": 0.0}})
    assert plat.load_ground_truth("exp-001")["conversion_rate"]["control"] == 0.04
    assert len(plat.load_ground_truth("exp-001")) == 2
    with pytest.raises(errors.PermissionDenied):
        plat.save_ground_truth(who(plat, "marcus"), "exp-001", {})


def test_register_source_admin_only_and_unique(plat):
    src = load_sources().get("users").model_copy(update={"source_id": "extra_users"})
    with pytest.raises(errors.PermissionDenied):
        plat.register_source(who(plat, "priya"), src)
    plat.register_source(who(plat, "admin"), src)
    assert len(plat.list_sources()) == 10
    with pytest.raises(errors.InvalidInput, match="already registered"):
        plat.register_source(who(plat, "admin"), src)


def test_audit_is_admin_only_newest_first_and_limited(plat):
    make(plat)
    plat.update_experiment(who(plat, "priya"), "exp-001", "n2", "h", LAUNCH, 14)
    log = plat.list_audit(who(plat, "admin"), "exp-001")
    assert [a["action"] for a in log] == ["experiment.update", "experiment.create"]
    assert len(plat.list_audit(who(plat, "admin"), limit=2)) == 2
    for user in ("priya", "viewer"):
        with pytest.raises(errors.PermissionDenied, match="admin role"):
            plat.list_audit(who(plat, user))


def test_every_write_is_audited(plat):
    make(plat)
    a = who(plat, "priya")
    plat.save_design(a, "exp-001", primary(), [], [])
    plat.set_status(a, "exp-001", "Running")
    actions = [x["action"] for x in plat.list_audit(who(plat, "admin"), "exp-001")]
    assert sorted(actions) == ["experiment.create", "experiment.design", "experiment.status"]


# ---- portfolio, history --------------------------------------------------------------------------
def test_portfolio_summarises_each_plan_and_filters(plat):
    make(plat, "exp-001", launch=date(2026, 1, 1), runtime=14)
    plat.save_design(who(plat, "priya"), "exp-001", primary(), [guard()], ["add_to_cart_rate"])
    make(plat, "exp-002", user="marcus", product="email", name="Subject", hyp="Lifts opens")
    make(plat, "exp-003", user="sofia", product="onboarding", name="Checklist", hyp="Lifts activation")
    rows = {r["experiment_id"]: r for r in plat.list_portfolio()}
    assert set(rows) == {"exp-001", "exp-002", "exp-003"}
    a = rows["exp-001"]
    assert (a["primary_metric"], a["n_items"], a["end_date"]) == ("conversion_rate", 3, date(2026, 1, 14))
    assert (rows["exp-002"]["primary_metric"], rows["exp-002"]["n_items"]) == (None, 0)
    assert [r["experiment_id"] for r in plat.list_portfolio(product_id="email")] == ["exp-002"]
    assert [r["experiment_id"] for r in plat.list_portfolio(owner_user_id="sofia")] == ["exp-003"]
    assert [r["experiment_id"] for r in plat.list_portfolio(status="Designed")] == ["exp-001"]
    assert [r["experiment_id"] for r in plat.list_portfolio(search="OPENS")] == ["exp-002"]
    assert plat.list_portfolio(search="checklist", product_id="email") == []
    assert plat.experiment_summary("exp-001")["end_date"] == date(2026, 1, 14)


def test_everyone_sees_the_history_of_an_experiment_but_not_the_full_audit_log(plat):
    make(plat)
    plat.save_design(who(plat, "priya"), "exp-001", primary(), [], [])
    plat.set_status(who(plat, "priya"), "exp-001", "Running")
    h = plat.experiment_history("exp-001")
    assert [x["action"] for x in h] == ["experiment.status", "experiment.design", "experiment.create"]
    with pytest.raises(errors.PermissionDenied):
        plat.list_audit(who(plat, "viewer"))
    with pytest.raises(errors.NotFound):
        plat.experiment_history("nope")


# ---- RUN: analysis, results, decisions ------------------------------------------------------------
def planned(plat, eid="exp-001", user="priya"):
    make(plat, eid, user=user)
    plat.save_design(who(plat, user), eid, primary(), [guard()], ["add_to_cart_rate"], dimension_ids=["plan_tier"], filter_ids=["existing_user"])


def test_run_analysis_builds_tests_saves_results_and_moves_to_analyzed(plat):
    planned(plat)
    res = plat.run_analysis(who(plat, "priya"), "exp-001", as_of=datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert res.audience_users == 900 and plat.get_experiment("exp-001")["status"] == "Analyzed"
    build = plat.runner.builds[0]
    assert (build["launch_date"], build["end_date"]) == (date(2026, 1, 1), date(2026, 1, 14))   # 14 days, launch day counts as day one
    rows = {r["item_id"]: r for r in plat.latest_results("exp-001")}
    assert [r["item_id"] for r in plat.latest_results("exp-001")] == ["conversion_rate", "refund_rate", "add_to_cart_rate"]
    p = rows["conversion_rate"]
    assert (p["n_control"], p["n_variant"], p["verdict"]) == (450, 450, "Significant improvement")
    assert p["difference"] == pytest.approx(0.06) and p["relative_lift"] == pytest.approx(0.6) and 0 < p["p_value"] < 0.05
    assert p["params"]["alpha"] == 0.05 and p["params"]["effect_abs"] == pytest.approx(0.00374) and p["ci_level"] == pytest.approx(0.95)
    assert p["achieved_power"] is not None and p["ci_low"] < p["difference"] < p["ci_high"]
    g = rows["refund_rate"]                      # refunds went UP 10% to 16% in the fake data: clearly harmful
    assert g["verdict"] == "Failed" and g["params"]["effect_abs"] == pytest.approx(0.0047 * 0.25)
    s = rows["add_to_cart_rate"]
    assert s["achieved_power"] is None and s["params"]["exploratory"] is True and s["role"] == "secondary"


# ---- end date, editing details, segments ----------------------------------------------------------------------------
def test_the_end_date_defaults_to_the_planned_runtime_and_can_be_set_to_extend_it(plat):
    make(plat)
    assert plat.get_experiment("exp-001")["end_date"] is None and plat.experiment_summary("exp-001")["end_date"] == date(2026, 1, 14)
    plat.create_experiment(who(plat, "priya"), "exp-002", "checkout", "Longer", "h", LAUNCH, 14, date(2026, 2, 28))
    assert plat.get_experiment("exp-002")["end_date"] == date(2026, 2, 28) and plat.experiment_summary("exp-002")["end_date"] == date(2026, 2, 28)
    for bad, msg in ((date(2025, 12, 31), "before the launch"), (date(2028, 1, 1), "more than"), ("2026-02-01", "must be a date"),
                     (datetime(2026, 2, 1), "must be a date")):
        with pytest.raises(errors.InvalidInput, match=msg):
            plat.create_experiment(who(plat, "priya"), "exp-003", "checkout", "n", "h", LAUNCH, 14, bad)


def test_analysis_runs_through_the_extended_end_date(plat):
    planned(plat)
    plat.update_experiment(who(plat, "priya"), "exp-001", "n", "h", LAUNCH, 14, end_date=date(2026, 1, 28))
    plat.refresh_monitor(who(plat, "priya"), "exp-001", as_of=datetime(2026, 1, 20, tzinfo=timezone.utc))
    assert plat.runner.builds[-1]["end_date"] == date(2026, 1, 19)                       # yesterday, well past the planned last day
    plat.run_analysis(who(plat, "priya"), "exp-001", as_of=datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert plat.runner.builds[-1]["end_date"] == date(2026, 1, 28) and plat.latest_results("exp-001")[0]["through_date"] == date(2026, 1, 28)


def test_slices_cover_each_segment_each_filter_and_each_pair_and_are_replaced_each_run(plat):
    planned(plat)                                                                       # one dimension (plan_tier) and one filter (existing_user)
    a = who(plat, "priya")
    plat.refresh_monitor(a, "exp-001", as_of=datetime(2026, 1, 5, tzinfo=timezone.utc))
    live = plat.segment_results("exp-001")
    slices = {(r["dimension_id"], r["segment"], r["filter_id"]) for r in live}
    assert slices == {("", "", "existing_user"), ("plan_tier", "a", ""), ("plan_tier", "b", ""),
                      ("plan_tier", "a", "existing_user"), ("plan_tier", "b", "existing_user")}    # everyone with no filter is the main result, not repeated
    assert len(live) == 5 * 3
    assert all(r["kind"] == "interim" and r["p_value"] is None and r["verdict"] is None and r["ci_low"] is None for r in live)
    plat.run_analysis(a, "exp-001", as_of=datetime(2026, 2, 1, tzinfo=timezone.utc))
    final = plat.segment_results("exp-001")
    assert len(final) == 5 * 3 and all(r["kind"] == "final" for r in final)         # replaced, not appended
    only_a = [r for r in final if (r["dimension_id"], r["segment"], r["filter_id"]) == ("plan_tier", "a", "")]
    assert [(r["item_id"], r["role"]) for r in only_a] == [("conversion_rate", "primary"), ("refund_rate", "guardrail"), ("add_to_cart_rate", "secondary")]
    big = only_a[0]
    assert (big["n_control"], big["n_variant"]) == (300, 300) and big["verdict"] == "Significant improvement" and 0 < big["p_value"] < 0.05
    assert big["difference"] == pytest.approx(0.06) and big["ci_level"] == pytest.approx(0.95)
    small = next(r for r in final if (r["dimension_id"], r["segment"], r["filter_id"], r["item_id"]) == ("plan_tier", "b", "", "conversion_rate"))
    assert small["verdict"] == "No significant difference" and small["ci_low"] < 0 < small["ci_high"]
    filt = next(r for r in final if (r["dimension_id"], r["filter_id"], r["item_id"]) == ("", "existing_user", "conversion_rate"))
    assert (filt["n_control"], filt["n_variant"]) == (338, 338)                        # the users who pass the filter, fewer than everyone


def test_no_dimensions_means_no_segment_results(plat):
    make(plat)
    plat.save_design(who(plat, "priya"), "exp-001", primary(), [], [])
    plat.refresh_monitor(who(plat, "priya"), "exp-001", as_of=datetime(2026, 1, 5, tzinfo=timezone.utc))
    assert plat.segment_results("exp-001") == []


# ---- live monitoring: descriptive numbers only, until the runtime is over ----------------------------------------
def test_monitoring_saves_descriptive_numbers_without_tests_or_verdicts(plat):
    planned(plat)
    a = who(plat, "priya")
    plat.refresh_monitor(a, "exp-001", as_of=datetime(2026, 1, 5, 9, tzinfo=timezone.utc))
    assert plat.get_experiment("exp-001")["status"] == "Running"
    build = plat.runner.builds[0]
    assert (build["end_date"], build["final"]) == (date(2026, 1, 4), False)          # yesterday: only complete days
    rows = plat.latest_results("exp-001", "interim")
    assert [r["item_id"] for r in rows] == ["conversion_rate", "refund_rate", "add_to_cart_rate"]
    r = rows[0]
    assert r["kind"] == "interim" and r["through_date"] == date(2026, 1, 4)
    assert r["difference"] == pytest.approx(0.06) and r["relative_lift"] == pytest.approx(0.6) and (r["n_control"], r["n_variant"]) == (450, 450)
    for row in rows:                                   # no peeking: nothing that could be read as a conclusion
        assert all(row.get(k) is None for k in ("p_value", "verdict", "ci_low", "ci_high", "achieved_power", "ci_level"))
    assert plat.latest_results("exp-001") == []        # there is no final result yet
    job = plat.list_jobs("exp-001")[0]
    assert job["type"] == "monitor" and job["detail"]["final"] is False and job["detail"]["through_date"] == "2026-01-04"
    assert plat.list_audit(who(plat, "admin"), "exp-001")[0]["action"] == "monitor.refresh"


def test_every_run_saves_the_daily_series_and_replaces_the_old_one(plat):
    planned(plat)
    a = who(plat, "priya")
    plat.refresh_monitor(a, "exp-001", as_of=datetime(2026, 1, 5, 9, tzinfo=timezone.utc))        # data through Jan 4: four days
    series = plat.daily_series("exp-001")
    assert {r["day"] for r in series} == {date(2026, 1, d) for d in range(1, 5)} and {r["arm"] for r in series} == {"control", "variant"}
    assert {r["item_id"] for r in series} == {"conversion_rate", "refund_rate", "add_to_cart_rate"} and len(series) == 3 * 4 * 2
    plat.run_analysis(a, "exp-001", as_of=datetime(2026, 2, 1, tzinfo=timezone.utc))              # the final run replaces it: fourteen days
    series = plat.daily_series("exp-001")
    assert len(series) == 3 * 14 * 2 and max(r["day"] for r in series) == date(2026, 1, 14)
    last = [r for r in series if r["item_id"] == "conversion_rate" and r["day"] == date(2026, 1, 14)]
    assert {r["arm"]: r["mean_value"] for r in last} == {"control": pytest.approx(0.10), "variant": pytest.approx(0.16)}   # ends at the summary
    assert plat.daily_series("nope") == []


def test_monitoring_can_be_refreshed_and_never_goes_past_the_last_day(plat):
    planned(plat)
    a = who(plat, "priya")
    plat.refresh_monitor(a, "exp-001", as_of=datetime(2026, 1, 3, tzinfo=timezone.utc))
    plat.refresh_monitor(a, "exp-001", as_of=datetime(2026, 3, 1, tzinfo=timezone.utc))
    assert plat.runner.builds[1]["end_date"] == date(2026, 1, 14)                      # the last day, not yesterday
    assert plat.latest_results("exp-001", "interim")[0]["through_date"] == date(2026, 1, 14)
    assert len(plat.store.select("results", {"experiment_id": "exp-001"})) == 6          # both refreshes kept


def test_the_final_analysis_follows_monitoring_and_closes_it(plat):
    planned(plat)
    a = who(plat, "priya")
    plat.refresh_monitor(a, "exp-001", as_of=datetime(2026, 1, 5, tzinfo=timezone.utc))
    plat.run_analysis(a, "exp-001", as_of=datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert plat.get_experiment("exp-001")["status"] == "Analyzed"
    assert plat.latest_results("exp-001")[0]["verdict"] == "Significant improvement"
    assert plat.latest_results("exp-001", "interim")[0]["verdict"] is None          # the two kinds never mix
    plat.refresh_monitor(a, "exp-001", as_of=datetime(2026, 2, 2, tzinfo=timezone.utc))     # allowed again: the run may have been extended


def test_monitoring_needs_a_saved_plan_and_the_right_person(plat):
    make(plat)
    with pytest.raises(errors.InvalidInput, match="save its plan before running it"):
        plat.refresh_monitor(who(plat, "priya"), "exp-001")
    plat.save_design(who(plat, "priya"), "exp-001", primary(), [], [])
    for user in ("marcus", "viewer"):
        with pytest.raises(errors.PermissionDenied):
            plat.refresh_monitor(who(plat, user), "exp-001")


def test_the_job_and_its_stats_step_are_logged(plat):
    planned(plat)
    plat.run_analysis(who(plat, "priya"), "exp-001", as_of=datetime(2026, 2, 1, tzinfo=timezone.utc))
    job = plat.list_jobs("exp-001")[0]
    assert job["type"] == "analysis" and job["status"] == "succeeded"
    steps = {s["step"]: s for s in plat.job_steps(job["job_id"])}
    assert "stats" in steps and steps["stats"]["row_count"] == 3 and steps["stats"]["status"] == "ok"
    assert plat.list_audit(who(plat, "admin"), "exp-001")[0]["action"] == "analysis.run"


def test_rerunning_keeps_history_and_shows_the_latest(plat):
    planned(plat)
    a = who(plat, "priya")
    plat.run_analysis(a, "exp-001", as_of=datetime(2026, 2, 1, tzinfo=timezone.utc))
    first = plat.latest_results("exp-001")[0]["run_at"]
    plat.run_analysis(a, "exp-001", as_of=datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert plat.latest_results("exp-001")[0]["run_at"] > first
    assert len(plat.store.select("results", {"experiment_id": "exp-001"})) == 6   # two runs of three metrics kept
    assert len(plat.list_jobs("exp-001")) == 2


def test_a_failed_gate_saves_no_results_and_keeps_the_status(plat):
    planned(plat, "exp-bad-1")
    with pytest.raises(errors.InvalidInput, match="Data-quality gate failed"):
        plat.run_analysis(who(plat, "priya"), "exp-bad-1")
    assert plat.get_experiment("exp-bad-1")["status"] == "Designed" and plat.latest_results("exp-bad-1") == []
    assert plat.list_jobs("exp-bad-1")[0]["status"] == "failed"


def test_run_permissions_and_state(plat):
    planned(plat)
    for user in ("marcus", "viewer"):
        with pytest.raises(errors.PermissionDenied):
            plat.run_analysis(who(plat, user), "exp-001")
    make(plat, "exp-002")
    with pytest.raises(errors.InvalidInput, match="save its plan before running it"):
        plat.run_analysis(who(plat, "priya"), "exp-002")          # no plan saved yet
    plat.runner = None
    with pytest.raises(errors.InvalidInput, match="no warehouse"):
        plat.run_analysis(who(plat, "priya"), "exp-001")


def test_record_a_decision_once_analyzed(plat):
    planned(plat)
    a = who(plat, "priya")
    with pytest.raises(errors.InvalidInput, match="once it is Analyzed"):
        plat.record_decision(a, "exp-001", "ship", "because")
    plat.run_analysis(a, "exp-001", as_of=datetime(2026, 2, 1, tzinfo=timezone.utc))
    with pytest.raises(errors.InvalidInput, match="decision must be"):
        plat.record_decision(a, "exp-001", "maybe", "because")
    with pytest.raises(errors.InvalidInput, match="say why"):
        plat.record_decision(a, "exp-001", "iterate", "  ")
    with pytest.raises(errors.PermissionDenied):
        plat.record_decision(who(plat, "marcus"), "exp-001", "ship", "because")
    row = plat.record_decision(a, "exp-001", "iterate", "Guardrail failed; fix refunds and rerun")
    assert (row["status"], row["decision"], row["decided_by"]) == ("Decided", "iterate", "priya") and row["decided_at"]
    assert plat.list_audit(who(plat, "admin"), "exp-001")[0]["action"] == "experiment.decision"
    with pytest.raises(errors.InvalidInput, match="once it is Analyzed"):
        plat.record_decision(a, "exp-001", "ship", "changed my mind")


def test_no_warehouse_helpers(plat):
    assert plat.assignment_check("exp-001")["n_users"] == 1000
    plat.runner = None
    assert plat.assignment_check("exp-001") == {}
