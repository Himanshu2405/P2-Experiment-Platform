"""SQL for each pipeline step. Pure string building, so it is unit-tested without a warehouse.

Every step is one small query that writes one table. The big per-experiment table is generated here from many
small pieces; nobody writes or edits it by hand. Author SQL arrives already resolved to staging views.
"""
RESERVED_COLUMNS = ("user_id", "arm", "assigned_date")


def table(dataset: str, key: str, suffix: str) -> str:
    """exp_<experiment id>__<step>; hyphens in the id become underscores."""
    return f"{dataset}.exp_{key.replace('-', '_')}__{suffix}"


def ctas(tbl: str, select: str, expire_days: int | None) -> str:
    """CREATE OR REPLACE makes every step atomic and repeatable. Intermediate tables expire; the final one does not."""
    opts = f"OPTIONS (expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL {expire_days} DAY))\n" if expire_days else ""
    return f"CREATE OR REPLACE TABLE `{tbl}`\n{opts}AS\n{select}"


def cohort_experiment(staging: str) -> str:
    """Who is in the test and when each user's clock starts: users assigned during the runtime. The staging view
    keeps one assignment per user."""
    return (f"SELECT user_id, arm, DATE(assigned_at) AS assigned_date\n"
            f"FROM `{staging}.stg_exp_assignments`\n"
            f"WHERE experiment_id = @experiment_id AND DATE(assigned_at) BETWEEN @launch AND @end_date")


def attr_step(cohort_tbl: str, item_sql: str, kind: str) -> str:
    """A dimension or filter as of entry: the latest row on or before each user's assignment date, else the default."""
    default = "@default" if kind == "dimension" else "0"
    return (f"SELECT c.user_id,\n"
            f"  COALESCE(ARRAY_AGG(a.value IGNORE NULLS ORDER BY a.effective_date DESC LIMIT 1)[SAFE_OFFSET(0)], {default}) AS value\n"
            f"FROM `{cohort_tbl}` c\nLEFT JOIN (\n{item_sql.strip().rstrip(';')}\n) a\n"
            f"  ON a.user_id = c.user_id AND a.effective_date <= c.assigned_date\nGROUP BY c.user_id")


def audience_step(cohort_tbl: str, filter_tbls: list[str]) -> str:
    """Users who pass every selected filter. With no filters, everyone in the cohort."""
    joins = "".join(f"\nJOIN `{t}` f{i} ON f{i}.user_id = c.user_id AND f{i}.value = 1" for i, t in enumerate(filter_tbls))
    return f"SELECT c.user_id, c.arm, c.assigned_date\nFROM `{cohort_tbl}` c{joins}"


def metric_step(audience_tbl: str, item_sql: str, aggregation: str, value_type: str) -> str:
    """One metric per user from their assignment day through the experiment's last day (@end_date); no rows means 0."""
    agg = {"max": "MAX", "sum": "SUM"}[aggregation]
    cast = "INT64" if value_type == "binary" else "FLOAT64"
    return (f"SELECT a.user_id, CAST(COALESCE({agg}(m.value), 0) AS {cast}) AS value\n"
            f"FROM `{audience_tbl}` a\nLEFT JOIN (\n{item_sql.strip().rstrip(';')}\n) m\n"
            f"  ON m.user_id = a.user_id\n AND m.metric_date BETWEEN a.assigned_date AND @end_date\n"
            f"GROUP BY a.user_id")


def daily_step(audience_tbl: str, item_sql: str, aggregation: str) -> str:
    """Cumulative metric by day and arm: for each day, the users assigned on or before it, each measured from their own
    assignment day through that day. The last day equals the experiment-level mean. Returns rows, not a table."""
    agg = {"max": "MAX", "sum": "SUM"}[aggregation]
    return (f"WITH days AS (SELECT day FROM UNNEST(GENERATE_DATE_ARRAY(@launch, @end_date)) AS day),\n"
            f"per_user AS (\n"
            f"  SELECT d.day, a.arm, a.user_id, {agg}(m.value) AS v\n"
            f"  FROM days d JOIN `{audience_tbl}` a ON a.assigned_date <= d.day\n"
            f"  LEFT JOIN (\n{item_sql.strip().rstrip(';')}\n) m\n"
            f"    ON m.user_id = a.user_id AND m.metric_date BETWEEN a.assigned_date AND d.day\n"
            f"  GROUP BY d.day, a.arm, a.user_id)\n"
            f"SELECT day, arm, COUNT(*) AS n_users, AVG(CAST(COALESCE(v, 0) AS FLOAT64)) AS mean_value,\n"
            f"  VAR_SAMP(CAST(COALESCE(v, 0) AS FLOAT64)) AS var_value\n"
            f"FROM per_user GROUP BY day, arm ORDER BY day, arm")


def final_step(audience_tbl: str, dims: list[tuple[str, str]], metrics: list[tuple[str, str]],
               filters: list[tuple[str, str]] = ()) -> str:
    """One row per audience user; one column per dimension, per filter (0 or 1) and per metric, named by item id."""
    for item_id, _ in [*dims, *filters, *metrics]:
        if item_id in RESERVED_COLUMNS:
            raise ValueError(f"{item_id} is a reserved column name")
    cols = ["a.user_id", "a.arm", "a.assigned_date"]
    joins = []
    for prefix, group in (("d", dims), ("f", filters), ("m", metrics)):
        for i, (item_id, tbl) in enumerate(group):
            cols.append(f"{prefix}{i}.value AS `{item_id}`")
            joins.append(f"LEFT JOIN `{tbl}` {prefix}{i} ON {prefix}{i}.user_id = a.user_id")
    return "SELECT " + ",\n  ".join(cols) + f"\nFROM `{audience_tbl}` a\n" + "\n".join(joins)
