"""The service layer on the real BigQuery store: bootstrap, an experiment from creation to plan, audit, and migration."""
from datetime import date

import pytest
from google.cloud import bigquery

from p2.catalog.seed import seed_registry
from p2.services import errors
from p2.services.platform import Platform
from p2.stats.plan import MetricPlan
from p2.store.base import Col, TableDef
from p2.store.bigquery import BigQueryStore
from p2.warehouse.sources import load_sources

pytestmark = pytest.mark.bq
reg = seed_registry()
LAUNCH = date(2026, 1, 1)


@pytest.fixture(scope="module")
def plat(wh):
    store = BigQueryStore(wh)
    store.ensure()
    p = Platform(store, load_sources())
    assert p.bootstrap() == {"team_members": 5, "products": 3, "data_sources": 9, "catalog_items": 24}
    return p


def test_all_application_tables_exist(plat, wh):
    assert set(plat.store.tables) <= {t.table_id for t in wh.client.list_tables(wh.app)}  # other tests may add their own


def test_bootstrap_is_idempotent_on_bigquery(plat):
    assert plat.bootstrap() == {"team_members": 0, "products": 0, "data_sources": 0, "catalog_items": 0}
    assert len(plat.list_team()) == 5 and len(plat.list_sources()) == 9


def test_experiment_from_creation_to_plan_with_audit(plat):
    priya, admin = plat.get_actor("priya"), plat.get_actor("admin")
    row = plat.create_experiment(priya, "bq-1", "checkout", "Banner", "Lifts conversion", LAUNCH, 14)
    assert (row["launch_date"], row["runtime_days"]) == (LAUNCH, 14)
    with pytest.raises(errors.InvalidInput, match="already exists"):
        plat.create_experiment(priya, "bq-1", "checkout", "Again", "h", LAUNCH, 14)
    with pytest.raises(errors.PermissionDenied):
        plat.create_experiment(plat.get_actor("marcus"), "bq-2", "checkout", "x", "y", LAUNCH, 14)
    plat.save_design(
        priya, "bq-1", MetricPlan(reg.get("conversion_rate"), "primary", 0.0374, 0.1, "relative", 0.05, 0.8, "two-sided"),
        [MetricPlan(reg.get("refund_rate"), "guardrail", 0.0047, 0.25, "relative", 0.05, 0.8, "one-sided")], ["add_to_cart_rate"])
    exp = plat.get_experiment("bq-1")
    assert exp["status"] == "Designed" and exp["created_at"] == row["created_at"] and exp["updated_at"] > row["updated_at"]
    items = plat.get_design("bq-1")
    assert [(i["item_id"], i["role"]) for i in items] == [("conversion_rate", "primary"), ("refund_rate", "guardrail"), ("add_to_cart_rate", "secondary")]
    assert (items[0]["baseline"], items[0]["alpha"]) == (0.0374, 0.05)
    assert [a["action"] for a in plat.list_audit(admin, "bq-1")] == ["experiment.design", "experiment.create"]
    with pytest.raises(errors.Conflict):
        plat.update_experiment(priya, "bq-1", "Edit", "h", LAUNCH, 14, expected_updated_at=row["updated_at"])
    plat.save_ground_truth(priya, "bq-1", {"conversion_rate": {"control": 0.037, "variant": 0.0407, "relative_lift": 0.1}})
    assert plat.load_ground_truth("bq-1")["conversion_rate"]["relative_lift"] == pytest.approx(0.1)


def test_ensure_migrates_an_older_table_to_the_current_schema(wh):
    """An old version had a REQUIRED column the code no longer writes and lacked a newer one. ensure() repairs it."""
    old = bigquery.Table(f"{wh.app}.migrating", schema=[
        bigquery.SchemaField("k", "STRING", mode="REQUIRED"), bigquery.SchemaField("legacy", "FLOAT64", mode="REQUIRED")])
    wh.client.create_table(old)
    new = TableDef("migrating", (Col("k", "STRING"), Col("added", "DATE", True)), ("k",))
    store = BigQueryStore(wh, {"migrating": new})
    store.ensure()
    store.ensure()                                                        # repeatable
    names = {f.name: f.mode for f in wh.client.get_table(f"{wh.app}.migrating").schema}
    assert names == {"k": "REQUIRED", "legacy": "NULLABLE", "added": "NULLABLE"}
    store.insert("migrating", {"k": "a", "added": date(2026, 1, 1)})      # inserts work without the legacy column
    assert store.get("migrating", {"k": "a"})["added"] == date(2026, 1, 1)


def test_a_metric_added_in_bigquery_with_a_plain_insert_is_picked_up_by_the_tool(plat, wh):
    """The documented way to add a metric (TECH_SPEC section 6): one INSERT into catalog_items, no tool involved."""
    sql = """INSERT INTO `{app}.catalog_items`
  (item_id, version, kind, product_id, display_name, description, sql, status, author,
   value_type, aggregation, good_direction, format, created_at, updated_at)
VALUES ('big_basket_rate', 1, 'metric', 'checkout', 'Big basket rate', 'Share of users with an order of five or more items',
  \"\"\"SELECT user_id, DATE(order_ts) AS metric_date, 1 AS value FROM {{{{ checkout_orders }}}}
     WHERE status = 'completed' AND item_count >= 5 GROUP BY user_id, metric_date\"\"\",
  'Certified', 'priya', 'binary', 'max', 'higher', 'percent', CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP())""".format(app=wh.app)
    wh.client.query(sql).result()
    metric = plat.metric_registry().get("big_basket_rate")
    assert (metric.product_id, metric.type, metric.window_aggregation, metric.good_direction) == ("checkout", "binary", "max", "higher")
    item = next(i for i in plat.certified_items("metric") if i["item_id"] == "big_basket_rate")
    assert item["author"] == "priya" and "{{ checkout_orders }}" in item["sql"] and "item_count >= 5" in item["sql"]

