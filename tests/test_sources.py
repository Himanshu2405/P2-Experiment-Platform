import pytest
from pydantic import ValidationError

from p2.warehouse.sources import (SourceDef, SourceRegistry, create_table_sql, load_sources, resolve_logical_names,
                                  staging_view_sql)

reg = load_sources()
STG = "proj.p2_dev_staging"


def src(**over):
    base = dict(source_id="s1", product_id="shared", description="d", user_column="user_id", time_column="ts",
                dedupe_key=["id"], dedupe_order="ts ASC", columns=[{"name": "id", "type": "STRING"},
                {"name": "user_id", "type": "STRING"}, {"name": "ts", "type": "TIMESTAMP"}])
    return SourceDef(**{**base, **over})


def test_nine_sources_three_shared_two_per_product():
    assert len(reg.ids()) == 9
    by = {}
    for s in reg:
        by.setdefault(s.product_id, []).append(s.source_id)
    assert sorted(by["shared"]) == ["exp_assignments", "plan_history", "users"]
    assert sorted(by["checkout"]) == ["checkout_events", "checkout_orders"]
    assert sorted(by["email"]) == ["email_events", "email_sends"]
    assert sorted(by["onboarding"]) == ["onboarding_events", "payments"]


def test_authors_see_their_product_plus_shared_sources():
    assert {s.source_id for s in reg.for_product("email")} == {
        "exp_assignments", "users", "plan_history", "email_sends", "email_events"}


def test_ddl_has_partition_cluster_and_ingested_at():
    sql = create_table_sql(reg.get("checkout_orders"), "p.raw")
    assert "CREATE TABLE IF NOT EXISTS `p.raw.checkout_orders`" in sql
    assert "PARTITION BY DATE(order_ts)" in sql and "CLUSTER BY user_id" in sql
    assert "ingested_at TIMESTAMP NOT NULL" in sql
    assert "refund_ts TIMESTAMP," in sql and "order_id STRING NOT NULL" in sql
    assert "PARTITION BY" not in create_table_sql(reg.get("users"), "p.raw")


def test_every_source_has_a_partition_or_is_the_small_user_table():
    for s in reg:
        assert s.partition or s.source_id == "users"


def test_staging_view_keeps_one_row_per_key_with_documented_winner():
    sql = staging_view_sql(reg.get("checkout_orders"), "p.raw", "p.stg")
    assert "PARTITION BY order_id ORDER BY updated_at DESC, ingested_at DESC" in sql
    assert "CREATE OR REPLACE VIEW `p.stg.stg_checkout_orders`" in sql and "WHERE rn = 1" in sql


def test_validation_rejects_bad_definitions():
    with pytest.raises(ValidationError, match="unknown columns"):
        src(dedupe_key=["nope"])
    with pytest.raises(ValidationError, match="ingested_at is added"):
        src(columns=[{"name": "ingested_at", "type": "TIMESTAMP"}, {"name": "id", "type": "STRING"},
                     {"name": "user_id", "type": "STRING"}, {"name": "ts", "type": "TIMESTAMP"}])
    with pytest.raises(ValidationError, match="duplicate column"):
        src(columns=[{"name": "id", "type": "STRING"}, {"name": "id", "type": "STRING"},
                     {"name": "user_id", "type": "STRING"}, {"name": "ts", "type": "TIMESTAMP"}])
    with pytest.raises(ValidationError):
        src(source_id="Bad Name")
    with pytest.raises(ValueError, match="duplicate source_id"):
        SourceRegistry([src(), src()])


def test_logical_names_resolve_to_staging_views():
    out = resolve_logical_names("SELECT * FROM {{ checkout_events }} e JOIN {{users}} u USING (user_id)", reg, STG)
    assert "`proj.p2_dev_staging.stg_checkout_events`" in out and "`proj.p2_dev_staging.stg_users`" in out
    assert "{{" not in out


@pytest.mark.parametrize("sql,msg", [
    ("SELECT * FROM {{ nope }}", "unknown source"),
    ("SELECT * FROM {% if 1 %}x{% endif %}", "placeholders"),
    ("SELECT {{ 1 + 1 }}", "invalid placeholder"),
    ("SELECT {{ checkout_events.x }}", "invalid placeholder"),
    ("SELECT * FROM {{ email_events }}", "not available"),
])
def test_bad_placeholders_are_rejected(sql, msg):
    allowed = {s.source_id for s in reg.for_product("checkout")}
    with pytest.raises(ValueError, match=msg):
        resolve_logical_names(sql, reg, STG, allowed)


def test_bad_file(tmp_path):
    p = tmp_path / "x.yaml"
    p.write_text("a: 1")
    with pytest.raises(ValueError, match="top-level"):
        load_sources(p)
