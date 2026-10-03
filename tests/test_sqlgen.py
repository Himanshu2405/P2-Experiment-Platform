import pytest

from p2.pipeline import sqlgen

ITEM = "SELECT user_id, effective_date, plan_tier AS value FROM `p.stg.stg_plan_history`"
METRIC = "SELECT user_id, DATE(order_ts) AS metric_date, 1 AS value FROM `p.stg.stg_checkout_orders` GROUP BY 1, 2"


def test_table_names_map_hyphens_and_suffixes():
    assert sqlgen.table("p.analytics", "exp-001", "cohort") == "p.analytics.exp_exp_001__cohort"
    assert sqlgen.table("p.analytics", "my-test", "final") != sqlgen.table("p.analytics", "my_test2", "final")


def test_intermediate_tables_expire_and_the_final_one_does_not():
    assert "INTERVAL 7 DAY" in sqlgen.ctas("p.a.t", "SELECT 1", 7)
    sql = sqlgen.ctas("p.a.t", "SELECT 1", None)
    assert "expiration" not in sql and sql.startswith("CREATE OR REPLACE TABLE `p.a.t`")


def test_experiment_cohort_reads_the_deduplicated_view_and_only_assignments_during_the_runtime():
    sql = sqlgen.cohort_experiment("p.stg")
    assert "`p.stg.stg_exp_assignments`" in sql and "@experiment_id" in sql and "DATE(assigned_at) AS assigned_date" in sql
    assert "DATE(assigned_at) BETWEEN @launch AND @end_date" in sql


def test_attribute_step_is_as_of_entry_with_a_default():
    dim = sqlgen.attr_step("p.a.cohort", ITEM, "dimension")
    assert "a.effective_date <= c.assigned_date" in dim and "ORDER BY a.effective_date DESC LIMIT 1" in dim
    assert "IGNORE NULLS" in dim and "@default" in dim and "GROUP BY c.user_id" in dim
    flt = sqlgen.attr_step("p.a.cohort", ITEM, "filter")
    assert ", 0) AS value" in flt and "@default" not in flt


def test_audience_joins_one_table_per_filter():
    assert "JOIN" not in sqlgen.audience_step("p.a.cohort", [])
    sql = sqlgen.audience_step("p.a.cohort", ["p.a.f1", "p.a.f2"])
    assert sql.count("JOIN") == 2 and "f1.value = 1" in sql and "f0.value = 1" in sql.replace("f1", "f0") or "f1.value = 1" in sql


def test_metric_step_measures_from_assignment_until_the_last_day_of_the_experiment():
    binary = sqlgen.metric_step("p.a.aud", METRIC, "max", "binary")
    assert "MAX(m.value)" in binary and "AS INT64" in binary
    assert "BETWEEN a.assigned_date AND @end_date" in binary and "INTERVAL" not in binary   # no fixed 7-day window any more
    money = sqlgen.metric_step("p.a.aud", METRIC, "sum", "continuous")
    assert "SUM(m.value)" in money and "AS FLOAT64" in money
    assert "LEFT JOIN" in binary and "COALESCE" in binary  # users with no rows still get a zero


def test_daily_step_is_cumulative_from_each_users_own_assignment_day():
    sql = sqlgen.daily_step("p.a.aud", METRIC, "max")
    assert "GENERATE_DATE_ARRAY(@launch, @end_date)" in sql and "a.assigned_date <= d.day" in sql
    assert "m.metric_date BETWEEN a.assigned_date AND d.day" in sql                    # each user from their own day, through this day
    assert "MAX(m.value)" in sql and "COALESCE(v, 0)" in sql and "GROUP BY day, arm" in sql
    assert "SUM(m.value)" in sqlgen.daily_step("p.a.aud", METRIC, "sum")


def test_the_final_table_carries_each_filter_as_a_zero_or_one_column_so_filters_can_be_views():
    sql = sqlgen.final_step("p.a.aud", [("plan_tier", "p.a.d1")], [("conversion_rate", "p.a.m1")], [("existing_user", "p.a.f1")])
    assert "f0.value AS `existing_user`" in sql and "LEFT JOIN `p.a.f1` f0 ON f0.user_id = a.user_id" in sql
    assert sql.index("`plan_tier`") < sql.index("`existing_user`") < sql.index("`conversion_rate`")
    with pytest.raises(ValueError, match="reserved"):
        sqlgen.final_step("p.a.aud", [], [], [("arm", "p.a.f1")])


def test_final_step_has_one_column_per_item_named_by_id():
    sql = sqlgen.final_step("p.a.aud", [("plan_tier", "p.a.d1")], [("conversion_rate", "p.a.m1"), ("refund_rate", "p.a.m2")])
    for col in ("a.user_id", "a.arm", "a.assigned_date", "AS `plan_tier`", "AS `conversion_rate`", "AS `refund_rate`"):
        assert col in sql
    assert sql.count("LEFT JOIN") == 3
    assert "FROM `p.a.aud` a" in sql


@pytest.mark.parametrize("reserved", ["user_id", "arm", "assigned_date"])
def test_reserved_column_names_are_rejected(reserved):
    with pytest.raises(ValueError, match="reserved"):
        sqlgen.final_step("p.a.aud", [], [(reserved, "p.a.m1")])
