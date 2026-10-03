"""The catalog lifecycle on the in-memory store: propose, check, review, certify, version, retire."""
from datetime import date, datetime, timedelta, timezone

import pytest

from p2.catalog.seed import DIMENSIONS, FILTERS, METRICS, SEED_ITEMS
from p2.services import errors
from p2.services.catalog import fingerprint
from p2.services.platform import Platform
from p2.stats.plan import MetricPlan
from p2.store.memory import MemoryStore
from p2.store.schema import APP_TABLES
from p2.warehouse.sources import load_sources
from p2.testing import FakeChecker

LAUNCH = date(2026, 1, 1)
SQL_OK = "SELECT user_id, DATE(order_ts) AS metric_date, 1 AS value FROM {{ checkout_orders }} GROUP BY user_id, metric_date"


class Clock:
    def __init__(self):
        self.t = datetime(2026, 3, 1, tzinfo=timezone.utc)

    def __call__(self):
        self.t += timedelta(seconds=1)
        return self.t


@pytest.fixture
def plat():
    clock = Clock()
    p = Platform(MemoryStore(APP_TABLES, clock), load_sources(), clock, checker=FakeChecker())
    p.bootstrap()
    return p


def who(p, user):
    return p.get_actor(user)


def metric_kwargs(**over):
    base = dict(value_type="binary", aggregation="max", good_direction="higher", format="percent")
    return {**base, **over}


def propose_metric(p, user="priya", item_id="big_basket_rate", product="checkout", sql=SQL_OK, **kw):
    return p.propose_item(who(p, user), "metric", item_id, product, "Big basket rate", "Share of users with a big basket", sql,
                          **metric_kwargs(**kw))


def certify_flow(p, item_id="big_basket_rate", author="priya", reviewer="admin"):
    v = p.get_item(item_id)["version"]
    p.run_checks(who(p, author), item_id, v)
    p.submit_for_review(who(p, author), item_id, v)
    return p.certify(who(p, reviewer), item_id, v)


# ---- the seed ------------------------------------------------------------------------------------
def test_seed_is_24_certified_items_with_real_sql(plat):
    items = plat.list_items()
    assert len(items) == len(SEED_ITEMS) == 24
    assert {k: sum(i["kind"] == k for i in items) for k in ("metric", "dimension", "filter")} == {"metric": 15, "dimension": 5, "filter": 4}
    assert all(i["status"] == "Certified" and i["author"] == "system" and i["version"] == 1 for i in items)
    assert all("{{" in i["sql"] for i in items)
    assert all(i["default_value"] for i in items if i["kind"] == "dimension")


def test_seed_metrics_form_the_registry_and_scope_by_product(plat):
    reg = plat.metric_registry()
    assert len(reg.ids()) == 15
    assert {m.metric_id for m in reg.all_metrics() if m.product_id == "email"} >= {"open_rate", "click_rate"}
    assert {i["item_id"] for i in plat.certified_items("dimension", "email")} == {"plan_tier", "region", "acquisition_channel", "tenure_bucket"}
    assert "customer_type" in {i["item_id"] for i in plat.certified_items("dimension", "checkout")}
    assert {i["item_id"] for i in plat.certified_items("filter", "onboarding")} == {"paid_at_entry", "existing_user", "region_us"}


def test_seed_ids_and_contracts_are_consistent():
    ids = [s.item_id for s in SEED_ITEMS]
    assert len(set(ids)) == len(ids)
    for m in METRICS:
        assert m.product_id != "shared" and (m.value_type == "binary") == (m.aggregation == "max") or m.value_type == "continuous"
    for d in DIMENSIONS + FILTERS:
        assert "effective_date" in d.sql and "user_id" in d.sql


# ---- proposing -----------------------------------------------------------------------------------
def test_owner_proposes_a_draft_in_their_product(plat):
    row = propose_metric(plat)
    assert (row["status"], row["version"], row["author"]) == ("Draft", 1, "priya")
    assert row["qa_report"] is None and row["reviewed_by"] is None
    assert [a["action"] for a in plat.list_audit(who(plat, "admin"), "big_basket_rate")] == ["catalog.propose"]
    assert "big_basket_rate" not in plat.metric_registry().ids()  # drafts never reach the dropdowns


def test_propose_permissions(plat):
    with pytest.raises(errors.PermissionDenied, match="works in email"):
        propose_metric(plat, user="marcus", product="checkout")
    with pytest.raises(errors.PermissionDenied, match="read-only"):
        propose_metric(plat, user="viewer")
    with pytest.raises(errors.PermissionDenied, match="only an admin"):  # shared items are admin territory
        plat.propose_item(who(plat, "priya"), "filter", "vip", "shared", "VIP", "VIP users", "SELECT 1")
    plat.propose_item(who(plat, "admin"), "filter", "vip", "shared", "VIP", "VIP users", "SELECT 1")


@pytest.mark.parametrize("over,msg", [
    (dict(item_id="Bad-Id"), "item id"), (dict(item_id="x"), "item id"), (dict(item_id="user_id"), "item id"),
    (dict(item_id="conversion_rate"), "already exists"),
    (dict(value_type="ratio"), "binary or continuous"), (dict(aggregation="mean"), "max or sum"),
    (dict(good_direction="up"), "higher or lower"), (dict(format="pct"), "format"),
    (dict(value_type="binary", aggregation="sum"), "binary metric must use"), (dict(sql=" "), "sql is required"),
    (dict(sql="x" * 10_001), "longer than"), (dict(format=None), "needs"),
])
def test_metric_validation(plat, over, msg):
    kw = dict(user="priya", item_id="big_basket_rate")
    kw.update(over)
    with pytest.raises(errors.InvalidInput, match=msg):
        propose_metric(plat, **kw)


def test_kind_and_product_validation(plat):
    a = who(plat, "admin")
    with pytest.raises(errors.InvalidInput, match="kind"):
        plat.propose_item(a, "segment", "abc", "checkout", "n", "d", "s")
    with pytest.raises(errors.InvalidInput, match="one product"):
        plat.propose_item(a, "metric", "abc", "shared", "n", "d", "s", **metric_kwargs())
    with pytest.raises(errors.InvalidInput, match="default value"):
        plat.propose_item(a, "dimension", "dim_a", "shared", "n", "d", "SELECT 1")
    plat.propose_item(a, "dimension", "dim_a", "shared", "n", "d", "SELECT 1", default_value="Unknown")


# ---- editing drafts ------------------------------------------------------------------------------
def test_author_edits_a_draft_and_checks_go_stale(plat):
    propose_metric(plat)
    a = who(plat, "priya")
    plat.run_checks(a, "big_basket_rate", 1)
    plat.update_draft(a, "big_basket_rate", 1, description="Better words")          # does not affect the checks
    plat.submit_for_review(a, "big_basket_rate", 1)
    plat2 = plat  # a fresh draft to show the stale case
    propose_metric(plat2, item_id="other_rate")
    plat2.run_checks(a, "other_rate", 1)
    plat2.update_draft(a, "other_rate", 1, sql=SQL_OK + " -- changed")
    with pytest.raises(errors.InvalidInput, match="run them again"):
        plat2.submit_for_review(a, "other_rate", 1)


def test_edit_rules(plat):
    propose_metric(plat)
    with pytest.raises(errors.PermissionDenied, match="only the author"):
        plat.update_draft(who(plat, "marcus"), "big_basket_rate", 1, description="x")
    with pytest.raises(errors.InvalidInput, match="cannot change"):
        plat.update_draft(who(plat, "priya"), "big_basket_rate", 1, status="Certified")
    with pytest.raises(errors.InvalidInput, match="no changes"):
        plat.update_draft(who(plat, "priya"), "big_basket_rate", 1)
    with pytest.raises(errors.InvalidInput, match="binary metric must use"):
        plat.update_draft(who(plat, "priya"), "big_basket_rate", 1, aggregation="sum")
    plat.update_draft(who(plat, "admin"), "big_basket_rate", 1, display_name="Renamed")
    with pytest.raises(errors.InvalidInput, match="only drafts"):
        plat.update_draft(who(plat, "admin"), "conversion_rate", 1, description="x")


# ---- checks and review ---------------------------------------------------------------------------
def test_checks_store_a_fingerprinted_report(plat):
    propose_metric(plat)
    report = plat.run_checks(who(plat, "priya"), "big_basket_rate", 1)
    assert report["passed"] and report["fingerprint"] == fingerprint(plat.get_item("big_basket_rate"))
    assert plat.get_item("big_basket_rate")["qa_report"]["passed"] is True
    assert plat.checker.calls == [("big_basket_rate", 1)]
    with pytest.raises(errors.PermissionDenied):
        plat.run_checks(who(plat, "viewer"), "big_basket_rate", 1)
    plat.checker = None
    with pytest.raises(errors.InvalidInput, match="no warehouse"):
        plat.run_checks(who(plat, "priya"), "big_basket_rate", 1)


def test_cannot_submit_without_passing_fresh_checks(plat):
    propose_metric(plat)
    a = who(plat, "priya")
    with pytest.raises(errors.InvalidInput, match="run the automated checks"):
        plat.submit_for_review(a, "big_basket_rate", 1)
    plat.update_draft(a, "big_basket_rate", 1, sql="SELECT FAIL")
    assert not plat.run_checks(a, "big_basket_rate", 1)["passed"]
    with pytest.raises(errors.InvalidInput, match="checks failed"):
        plat.submit_for_review(a, "big_basket_rate", 1)
    with pytest.raises(errors.PermissionDenied):
        plat.submit_for_review(who(plat, "marcus"), "big_basket_rate", 1)


def test_full_lifecycle_to_the_dropdown_and_a_design(plat):
    propose_metric(plat)
    row = certify_flow(plat)
    assert (row["status"], row["reviewed_by"]) == ("Certified", "admin")
    assert "big_basket_rate" in plat.metric_registry().ids()
    plat.create_experiment(who(plat, "priya"), "exp-1", "checkout", "Basket", "Bigger baskets", LAUNCH, 14)
    m = plat.metric_registry().get("big_basket_rate")
    plat.save_design(who(plat, "priya"), "exp-1", MetricPlan(m, "primary", 0.05, 0.1, "relative", 0.05, 0.8, "two-sided"), [], [])
    assert plat.get_design("exp-1")[0]["item_id"] == "big_basket_rate"
    actions = [a["action"] for a in plat.list_audit(who(plat, "admin"), "big_basket_rate")]
    assert sorted(actions) == ["catalog.certify", "catalog.checks", "catalog.propose", "catalog.submit"]


def test_separation_of_duties_and_scope(plat):
    propose_metric(plat)
    a = who(plat, "priya")
    plat.run_checks(a, "big_basket_rate", 1)
    plat.submit_for_review(a, "big_basket_rate", 1)
    with pytest.raises(errors.PermissionDenied, match="separation of duties"):
        plat.certify(a, "big_basket_rate", 1)                              # the author
    with pytest.raises(errors.PermissionDenied, match="owns email"):
        plat.certify(who(plat, "marcus"), "big_basket_rate", 1)            # another product's owner
    with pytest.raises(errors.PermissionDenied, match="cannot review"):
        plat.certify(who(plat, "viewer"), "big_basket_rate", 1)
    plat.certify(who(plat, "admin"), "big_basket_rate", 1)


def test_an_admin_author_cannot_certify_their_own_item(plat):
    a = who(plat, "admin")
    plat.propose_item(a, "filter", "vip", "shared", "VIP", "VIP users", "SELECT 1")
    plat.run_checks(a, "vip", 1)
    plat.submit_for_review(a, "vip", 1)
    with pytest.raises(errors.PermissionDenied, match="separation of duties"):
        plat.certify(a, "vip", 1)


def test_reject_needs_a_reason_and_returns_the_draft(plat):
    propose_metric(plat)
    a = who(plat, "priya")
    plat.run_checks(a, "big_basket_rate", 1)
    plat.submit_for_review(a, "big_basket_rate", 1)
    with pytest.raises(errors.InvalidInput, match="why"):
        plat.reject(who(plat, "admin"), "big_basket_rate", 1, " ")
    row = plat.reject(who(plat, "admin"), "big_basket_rate", 1, "Definition is unclear")
    assert row["status"] == "Draft" and row["review_note"] == "Definition is unclear"
    with pytest.raises(errors.InvalidInput, match="not In review"):
        plat.reject(who(plat, "admin"), "big_basket_rate", 1, "again")
    with pytest.raises(errors.InvalidInput, match="only items In review"):
        plat.certify(who(plat, "admin"), "big_basket_rate", 1)


def test_new_version_supersedes_and_old_versions_stay_pinnable(plat):
    plat.create_experiment(who(plat, "priya"), "exp-1", "checkout", "Old", "h", LAUNCH, 14)
    m1 = plat.metric_registry().get("conversion_rate")
    plat.save_design(who(plat, "priya"), "exp-1", MetricPlan(m1, "primary", 0.0374, 0.1, "relative", 0.05, 0.8, "two-sided"), [], [])
    v2 = plat.new_version(who(plat, "priya"), "conversion_rate")
    assert (v2["version"], v2["status"], v2["author"], v2["qa_report"]) == (2, "Draft", "priya", None)
    with pytest.raises(errors.InvalidInput, match="still Draft"):
        plat.new_version(who(plat, "priya"), "conversion_rate")
    plat.update_draft(who(plat, "priya"), "conversion_rate", 2, sql=SQL_OK + " -- stricter")
    certify_flow(plat, "conversion_rate")
    assert plat.get_item("conversion_rate", 1)["status"] == "Deprecated"
    assert plat.get_item("conversion_rate")["version"] == 2
    reg = plat.metric_registry()
    assert reg.get("conversion_rate").version == 2 and reg.get("conversion_rate", 1).status == "Deprecated"
    assert plat.get_design("exp-1")[0]["item_version"] == 1       # the experiment keeps what it pinned
    assert [u["item_version"] for u in plat.item_usage("conversion_rate")] == [1]


def test_deprecate_hides_from_new_experiments_only(plat):
    with pytest.raises(errors.PermissionDenied):
        plat.deprecate(who(plat, "marcus"), "refund_rate", 1)
    plat.deprecate(who(plat, "priya"), "refund_rate", 1)
    assert "refund_rate" not in plat.metric_registry().ids()
    assert plat.metric_registry().get("refund_rate", 1).status == "Deprecated"
    with pytest.raises(errors.InvalidInput, match="only Certified"):
        plat.deprecate(who(plat, "priya"), "refund_rate", 1)


def test_list_search_and_versions(plat):
    assert {i["item_id"] for i in plat.list_items(kind="filter")} == {"paid_at_entry", "existing_user", "has_prior_order", "region_us"}
    assert {i["item_id"] for i in plat.list_items(product_id="email")} >= {"open_rate", "click_rate"}
    assert [i["item_id"] for i in plat.list_items(search="refund")] == ["refund_rate"]
    propose_metric(plat)
    assert [i["item_id"] for i in plat.list_items(status="Draft")] == ["big_basket_rate"]
    with pytest.raises(errors.NotFound):
        plat.get_item("nope")


# ---- dimensions and filters in a design ----------------------------------------------------------
def design(p, eid="exp-1", user="priya", **kw):
    m = p.metric_registry().get(kw.pop("metric", "conversion_rate"))
    return p.save_design(who(p, user), eid, MetricPlan(m, "primary", 0.0374, 0.1, "relative", 0.05, 0.8, "two-sided"), [], [], **kw)


def test_design_with_dimensions_and_filters(plat):
    plat.create_experiment(who(plat, "priya"), "exp-1", "checkout", "n", "h", LAUNCH, 14)
    design(plat, dimension_ids=["plan_tier", "customer_type"], filter_ids=["existing_user"])
    items = plat.get_design("exp-1")
    assert [(i["item_id"], i["role"]) for i in items] == [("conversion_rate", "primary"), ("existing_user", "filter"),
                                                          ("customer_type", "dimension"), ("plan_tier", "dimension")]
    assert all(i["item_version"] == 1 for i in items)
    assert items[1]["alpha"] is None and items[1]["kind"] == "filter"
    detail = [a for a in plat.list_audit(who(plat, "admin"), "exp-1") if a["action"] == "experiment.design"][0]["detail"]
    assert detail["filters"] == ["existing_user"] and detail["dimensions"] == ["plan_tier", "customer_type"]
    assert [u["role"] for u in plat.item_usage("plan_tier")] == ["dimension"]


def test_dimension_and_filter_rules(plat):
    plat.create_experiment(who(plat, "marcus"), "exp-e", "email", "n", "h", LAUNCH, 14)
    with pytest.raises(errors.InvalidInput, match="not a certified dimension available to email"):
        design(plat, "exp-e", "marcus", metric="open_rate", dimension_ids=["customer_type"])      # checkout-only
    with pytest.raises(errors.InvalidInput, match="not a certified filter"):
        design(plat, "exp-e", "marcus", metric="open_rate", filter_ids=["nope"])
    with pytest.raises(errors.InvalidInput, match="only be picked once"):
        design(plat, "exp-e", "marcus", metric="open_rate", filter_ids=["region_us", "region_us"])
    with pytest.raises(errors.InvalidInput, match="at most 5 filters"):
        design(plat, "exp-e", "marcus", metric="open_rate", filter_ids=["a", "b", "c", "d", "e", "f"])
    propose = plat.propose_item(who(plat, "admin"), "dimension", "draft_dim", "shared", "n", "d", "SELECT 1", default_value="Unknown")
    with pytest.raises(errors.InvalidInput, match="not a certified dimension"):                      # drafts cannot be used
        design(plat, "exp-e", "marcus", metric="open_rate", dimension_ids=[propose["item_id"]])
    assert plat.get_design("exp-e") == []
    design(plat, "exp-e", "marcus", metric="open_rate", dimension_ids=["region"], filter_ids=["region_us", "paid_at_entry"])


def test_a_deprecated_dimension_cannot_be_picked_for_new_designs(plat):
    plat.create_experiment(who(plat, "priya"), "exp-1", "checkout", "n", "h", LAUNCH, 14)
    plat.deprecate(who(plat, "priya"), "customer_type", 1)
    with pytest.raises(errors.InvalidInput, match="not a certified dimension"):
        design(plat, dimension_ids=["customer_type"])
