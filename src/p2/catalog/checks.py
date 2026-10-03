"""Automated checks for a catalog item's SQL, run against the warehouse before anyone reviews it.

Three groups, in order; a failure in an earlier group stops the later ones:
1. Safety, from a BigQuery dry run (nothing is executed): only registered sources by logical name, a single
   SELECT, under the cost cap, and exactly the contract's columns and types.
2. Data, from one real run wrapped in a statistics query: no duplicate keys, no nulls, values in range,
   sensible cardinality, dates not in the future, and not empty.
3. A preview (rows, users, means, top values) so a reviewer sees what the query returns before certifying it.
"""
import re

from google.api_core.exceptions import GoogleAPICallError
from google.cloud import bigquery

from p2.warehouse.bq import Warehouse
from p2.warehouse.sources import SourceRegistry

COST_CAP_BYTES = 1_000_000_000  # 1 GB per check query; the simulated data is about 10 MB
MAX_DISTINCT_VALUES = 20
NUMERIC = {"INTEGER", "FLOAT", "NUMERIC", "BIGNUMERIC", "INT64", "FLOAT64"}
CONTRACTS = {  # kind -> (date column, value column types)
    "metric": ("metric_date", NUMERIC),
    "dimension": ("effective_date", {"STRING"}),
    "filter": ("effective_date", {"INTEGER", "INT64"}),
}
_DIRECT_REF = re.compile(r"p2_\w*\.|`[^`]*\.[^`]*`")


class Report:
    def __init__(self):
        self.checks: list[dict] = []
        self.stats: dict = {}

    def add(self, name: str, status: str, detail: str) -> bool:
        self.checks.append({"name": name, "status": status, "detail": detail})
        return status != "fail"

    @property
    def passed(self) -> bool:
        return all(c["status"] != "fail" for c in self.checks)

    def as_dict(self) -> dict:
        return {"passed": self.passed, "checks": self.checks, "stats": self.stats}


def _message(e: Exception) -> str:
    msg = getattr(e, "message", None) or str(e)
    return msg.strip().splitlines()[0][:240]


class SqlChecker:
    def __init__(self, wh: Warehouse, sources: SourceRegistry | None = None, cost_cap_bytes: int = COST_CAP_BYTES):
        self.wh = wh
        self.sources = sources or wh.sources
        self.cost_cap = cost_cap_bytes

    def allowed_sources(self, product_id: str) -> set[str]:
        return {s.source_id for s in self.sources if s.product_id in ("shared", product_id)}

    # ---- group 1: safety ---------------------------------------------------------------------
    def _safety(self, item: dict, r: Report) -> str | None:
        """Returns the resolved SQL when every safety check passes, else None."""
        sql = item["sql"]
        allowed = self.allowed_sources(item["product_id"])
        try:
            resolved = self.wh.resolve(sql, allowed)
        except ValueError as e:
            r.add("sources", "fail", f"{e}. Use only registered sources, written as {{{{ source_name }}}}.")
            return None
        stripped = re.sub(r"\{\{\s*\w+\s*\}\}", "", sql)
        if _DIRECT_REF.search(stripped):
            r.add("sources", "fail", "direct table references are not allowed; refer to sources only by {{ name }} "
                                     "so de-duplicated staging views are used")
            return None
        r.add("sources", "pass", "uses only registered sources: " + ", ".join(sorted(set(re.findall(r"\{\{\s*(\w+)\s*\}\}", sql)))))
        try:
            job = self.wh.client.query(resolved, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False))
        except GoogleAPICallError as e:
            r.add("valid_sql", "fail", f"BigQuery rejected the query: {_message(e)}")
            return None
        r.add("valid_sql", "pass", "the query compiles")
        if job.statement_type != "SELECT":
            r.add("single_select", "fail", f"must be a single SELECT, found {job.statement_type}")
            return None
        r.add("single_select", "pass", "a single SELECT; no DDL or DML")
        staged = {f"stg_{i}" for i in allowed}
        outside = sorted(f"{t.dataset_id}.{t.table_id}" for t in job.referenced_tables
                         if not ((t.dataset_id == self.wh.raw.split(".")[-1] and t.table_id in allowed)
                                 or (t.dataset_id == self.wh.staging.split(".")[-1] and t.table_id in staged)))
        if outside:
            r.add("allowed_tables", "fail", f"reads tables outside this product's registered sources: {outside}")
            return None
        r.add("allowed_tables", "pass", "reads only allowed sources")
        if (job.total_bytes_processed or 0) > self.cost_cap:
            r.add("cost_cap", "fail", f"would scan {job.total_bytes_processed / 1e9:.2f} GB, over the {self.cost_cap / 1e9:.1f} GB cap")
            return None
        r.add("cost_cap", "pass", f"scans about {(job.total_bytes_processed or 0) / 1e6:.1f} MB")
        date_col, value_types = CONTRACTS[item["kind"]]
        cols = {f.name: f.field_type for f in job.schema}
        expected = {"user_id", date_col, "value"}
        if set(cols) != expected:
            r.add("columns", "fail", f"must return exactly {sorted(expected)}, returns {sorted(cols)}")
            return None
        wrong = [f"{n} is {cols[n]}" for n, ok in (("user_id", {"STRING"}), (date_col, {"DATE"}), ("value", value_types)) if cols[n] not in ok]
        if wrong:
            r.add("columns", "fail", "wrong column types: " + "; ".join(wrong))
            return None
        r.add("columns", "pass", f"user_id STRING, {date_col} DATE, value {cols['value']}")
        return resolved

    # ---- groups 2 and 3: data and preview -----------------------------------------------------
    def _data(self, item: dict, resolved: str, r: Report) -> None:
        kind, date_col = item["kind"], CONTRACTS[item["kind"]][0]
        body = "(\n" + resolved.strip().rstrip(";") + "\n)"
        if kind == "metric":
            bad = "COUNTIF(value NOT IN (0, 1))" if item["value_type"] == "binary" else "COUNTIF(value < 0)"
        elif kind == "filter":
            bad = "COUNTIF(value NOT IN (0, 1))"
        else:
            bad = "0"
        mean_expr, max_expr = (("CAST(NULL AS FLOAT64)",) * 2 if kind == "dimension"
                               else ("AVG(CAST(value AS FLOAT64))", "MAX(CAST(value AS FLOAT64))"))
        sql = f"""SELECT COUNT(*) AS n_rows, COUNT(DISTINCT user_id) AS n_users,
            COUNTIF(user_id IS NULL OR {date_col} IS NULL OR value IS NULL) AS n_nulls,
            COUNT(*) - COUNT(DISTINCT TO_JSON_STRING(STRUCT(user_id, {date_col}))) AS n_dup,
            COUNTIF({date_col} > CURRENT_DATE()) AS n_future, {bad} AS n_bad,
            COUNT(DISTINCT value) AS n_distinct, {mean_expr} AS mean_value,
            {max_expr} AS max_value, MIN({date_col}) AS min_date, MAX({date_col}) AS max_date
            FROM {body}"""
        try:
            s = self.wh.query_df(sql).iloc[0]
        except GoogleAPICallError as e:
            r.add("runs", "fail", f"the query failed to run: {_message(e)}")
            return
        r.stats.update({"n_rows": int(s.n_rows), "n_users": int(s.n_users), "n_distinct_values": int(s.n_distinct),
                        "min_date": str(s.min_date), "max_date": str(s.max_date)})
        if s.n_rows == 0:
            r.add("not_empty", "fail", "the query returns no rows")
            return
        r.add("not_empty", "pass", f"{int(s.n_rows):,} rows for {int(s.n_users):,} users")
        r.add("no_nulls", "pass" if s.n_nulls == 0 else "fail",
              "no nulls in the key, date, or value columns" if s.n_nulls == 0 else f"{int(s.n_nulls):,} rows have nulls")
        r.add("unique_keys", "pass" if s.n_dup == 0 else "fail",
              f"one row per user and {date_col}" if s.n_dup == 0 else
              f"{int(s.n_dup):,} duplicate (user_id, {date_col}) rows; aggregate to one row per user and day")
        r.add("dates", "pass" if s.n_future == 0 else "fail",
              "no dates in the future" if s.n_future == 0 else f"{int(s.n_future):,} rows are dated in the future")
        if kind == "metric":
            ok = s.n_bad == 0
            r.add("value_range", "pass" if ok else "fail",
                  ("values are only 0 or 1" if item["value_type"] == "binary" else "values are non-negative") if ok
                  else f"{int(s.n_bad):,} values out of range ({'must be 0 or 1' if item['value_type'] == 'binary' else 'must not be negative'})")
            r.stats["mean_value"] = float(s.mean_value)  # mean over the rows returned (user-days with activity), not over all users
            r.stats["max_value"] = float(s.max_value)
        elif kind == "filter":
            r.add("value_range", "pass" if s.n_bad == 0 else "fail",
                  "values are only 0 or 1" if s.n_bad == 0 else f"{int(s.n_bad):,} values are not 0 or 1")
            r.stats["share_passing"] = float(s.mean_value)
        else:
            ok = s.n_distinct <= MAX_DISTINCT_VALUES
            r.add("cardinality", "pass" if ok else "fail",
                  f"{int(s.n_distinct)} distinct values" if ok else
                  f"{int(s.n_distinct)} distinct values; a dimension may have at most {MAX_DISTINCT_VALUES}, bucket it")
            top = self.wh.query_df(f"SELECT value, COUNT(*) AS n FROM {body} GROUP BY value ORDER BY n DESC LIMIT 8")
            r.stats["top_values"] = {str(v): int(n) for v, n in zip(top.value, top.n)}

    def check(self, item: dict) -> dict:
        r = Report()
        resolved = self._safety(item, r)
        if resolved is not None:
            self._data(item, resolved, r)
        return r.as_dict()
