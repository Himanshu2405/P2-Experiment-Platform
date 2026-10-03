"""Runs the pipeline: quality gates, then small steps that each write one table, then the generated final table,
then one scan for each arm's count, mean and variance.

Step order for an experiment: gates, cohort, one step per filter and dimension, audience, one step per metric,
final table, gates. Independent steps run in parallel. Every step reports its table, row count, and time.
Each user is measured from their assignment day through the last day of data: the experiment's last day for the final
analysis, or yesterday while monitoring.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from p2.pipeline import sqlgen
from p2.stats.tests import Arm
from p2.warehouse.bq import Warehouse

INTERMEDIATE_EXPIRY_DAYS = 7


class DataQualityError(Exception):
    """A gate failed; the build stops instead of producing a misleading table."""


@dataclass
class StepResult:
    step: str
    table: str | None
    status: str  # ok or failed
    rows: int | None = None
    seconds: float = 0.0
    error: str | None = None


@dataclass
class BuildResult:
    final_table: str
    steps: list[StepResult] = field(default_factory=list)
    cohort_users: int = 0
    audience_users: int = 0
    n_control: int = 0
    n_variant: int = 0


def last_day(launch_date: date, runtime_days: int) -> date:
    """The planned last day: the launch day counts as day one."""
    return launch_date + timedelta(days=runtime_days - 1)


def end_of(exp: dict) -> date:
    """The experiment's current last day: an explicit end date (an extended or shortened run) wins over the planned runtime."""
    return exp.get("end_date") or last_day(exp["launch_date"], exp["runtime_days"])


class PipelineRunner:
    def __init__(self, wh: Warehouse, workers: int = 4):
        self.wh = wh
        self.workers = workers
        self.last_steps: list[StepResult] = []  # steps of the most recent build, kept even when it failed

    # ---- helpers -----------------------------------------------------------------------------
    def _allowed(self, product_id: str) -> set[str]:
        return {s.source_id for s in self.wh.sources if s.product_id in ("shared", product_id)}

    def _sql(self, item: dict) -> str:
        return self.wh.resolve(item["sql"], self._allowed(item["product_id"]))

    def _step(self, name: str, tbl: str, select: str, params: dict | None, expire: int | None = INTERMEDIATE_EXPIRY_DAYS) -> StepResult:
        start = time.perf_counter()
        try:
            self.wh.execute(sqlgen.ctas(tbl, select, expire), params)
            rows = self.wh.client.get_table(tbl).num_rows
            return StepResult(name, tbl, "ok", int(rows), round(time.perf_counter() - start, 2))
        except Exception as e:
            return StepResult(name, tbl, "failed", None, round(time.perf_counter() - start, 2), str(e).splitlines()[0][:300])

    def _parallel(self, jobs: list[tuple], steps: list[StepResult]) -> None:
        with ThreadPoolExecutor(self.workers) as pool:
            results = list(pool.map(lambda j: self._step(*j), jobs))
        steps.extend(results)
        failed = [r for r in results if r.status == "failed"]
        if failed:
            raise DataQualityError(f"step {failed[0].step} failed: {failed[0].error}")

    # ---- what the assignment log says about an experiment ---------------------------------------
    def assignment_summary(self, experiment_id: str) -> dict:
        """Users, arms and date range in the experiment tool's log (before any runtime filter)."""
        r = self.wh.query_df(
            f"SELECT COUNT(*) AS n, COUNT(DISTINCT arm) AS arms, MIN(DATE(assigned_at)) AS first_day, "
            f"MAX(DATE(assigned_at)) AS last_day FROM `{self.wh.staging}.stg_exp_assignments` WHERE experiment_id = @e",
            {"e": experiment_id}).iloc[0]
        return {"n_users": int(r.n), "arms": int(r.arms), "first_day": None if not r.n else r.first_day,
                "last_day": None if not r.n else r.last_day}

    # ---- gates -------------------------------------------------------------------------------
    def _gates_before(self, experiment_id: str, launch: date, end: date, as_of: datetime, final: bool) -> None:
        """`end` is the last day of data to include: the experiment's last day for the final analysis, yesterday (or the
        last day, if earlier) for monitoring."""
        today = as_of.date()
        if launch > today:
            raise DataQualityError(f"{experiment_id}: the experiment has not launched yet (launch date {launch})")
        if final and end >= today:
            raise DataQualityError(f"{experiment_id}: the experiment is still running until {end}; the final analysis can run after that day")
        if end < launch:
            raise DataQualityError(f"{experiment_id}: no complete day of data yet (launched {launch}); monitoring starts the day after launch")
        params = {"e": experiment_id, "launch": launch, "end": end}
        # Data integrity first, on the raw log: a user in two arms makes the de-duplicated view pick one arbitrarily.
        both = int(self.wh.query_df(
            f"SELECT COUNT(*) AS n FROM (SELECT user_id FROM `{self.wh.raw}.exp_assignments` WHERE experiment_id = @e "
            f"AND DATE(assigned_at) BETWEEN @launch AND @end GROUP BY user_id HAVING COUNT(DISTINCT arm) > 1)", params).iloc[0].n)
        if both:
            raise DataQualityError(f"{experiment_id}: {both} users are assigned to more than one arm")
        r = self.wh.query_df(
            f"SELECT COUNT(*) AS n, COUNT(DISTINCT arm) AS arms FROM `{self.wh.staging}.stg_exp_assignments` "
            f"WHERE experiment_id = @e AND DATE(assigned_at) BETWEEN @launch AND @end", params).iloc[0]
        if r.n == 0:
            seen = self.assignment_summary(experiment_id)
            if seen["n_users"]:
                raise DataQualityError(f"{experiment_id}: no assignments between {launch} and {end}, but the log has "
                                       f"{seen['n_users']:,} between {seen['first_day']} and {seen['last_day']}; check the launch date and runtime")
            raise DataQualityError(f"no assignments landed for {experiment_id}; the experiment tool has not logged any users for this id yet "
                                   f"(in this demo, simulate them with 'Dev only: simulate the raw data' on the Experiments page)")
        if r.arms < 2:
            raise DataQualityError(f"{experiment_id}: only one arm has assignments")

    def _gates_after(self, final_tbl: str, audience_users: int, metric_ids: list[str]) -> None:
        nulls = " + ".join(f"COUNTIF(`{m}` IS NULL)" for m in metric_ids) or "0"
        r = self.wh.query_df(f"SELECT COUNT(*) AS n, {nulls} AS n_null FROM `{final_tbl}`").iloc[0]
        if r.n != audience_users:
            raise DataQualityError(f"final table has {int(r.n)} rows but the audience has {audience_users} users")
        if r.n_null:
            raise DataQualityError(f"{int(r.n_null)} null metric values in the final table")

    # ---- experiment build --------------------------------------------------------------------
    def build(self, experiment_id: str, product_id: str, metrics: list[dict], dimensions: list[dict], filters: list[dict],
              launch_date: date, end_date: date, as_of: datetime | None = None, final: bool = True) -> BuildResult:
        ds, key = self.wh.analytics, experiment_id
        steps: list[StepResult] = []
        res = BuildResult(final_table=sqlgen.table(ds, key, "final"), steps=steps)
        self.last_steps = steps
        t0 = time.perf_counter()
        try:
            self._gates_before(experiment_id, launch_date, end_date, as_of or datetime.now(timezone.utc), final)
        except DataQualityError as e:
            steps.append(StepResult("gates_before", None, "failed", None, round(time.perf_counter() - t0, 2), str(e)))
            raise
        steps.append(StepResult("gates_before", None, "ok", None, round(time.perf_counter() - t0, 2)))

        cohort = sqlgen.table(ds, key, "cohort")
        first = self._step("cohort", cohort, sqlgen.cohort_experiment(self.wh.staging),
                           {"experiment_id": experiment_id, "launch": launch_date, "end_date": end_date})
        steps.append(first)
        if first.status == "failed":
            raise DataQualityError(f"cohort step failed: {first.error}")
        res.cohort_users = first.rows or 0

        attr_jobs, attr_tables = [], {}
        for it in (*filters, *dimensions):
            tbl = sqlgen.table(ds, key, f"attr_{it['item_id']}")
            attr_tables[it["item_id"]] = tbl
            attr_jobs.append((f"attr_{it['item_id']}", tbl, sqlgen.attr_step(cohort, self._sql(it), it["kind"]),
                              {"default": it["default_value"]} if it["kind"] == "dimension" else None))
        self._parallel(attr_jobs, steps)

        audience = sqlgen.table(ds, key, "audience")
        aud = self._step("audience", audience, sqlgen.audience_step(cohort, []), None)   # everyone assigned; filters are views, not cuts
        steps.append(aud)
        if aud.status == "failed":
            raise DataQualityError(f"audience step failed: {aud.error}")
        res.audience_users = aud.rows or 0
        if res.audience_users == 0:
            raise DataQualityError("no users were assigned during the runtime")

        metric_jobs, metric_tables = [], {}
        for m in metrics:
            tbl = sqlgen.table(ds, key, f"m_{m['item_id']}")
            metric_tables[m["item_id"]] = tbl
            metric_jobs.append((f"m_{m['item_id']}", tbl, sqlgen.metric_step(audience, self._sql(m), m["aggregation"], m["value_type"]),
                                {"end_date": end_date}))
        self._parallel(metric_jobs, steps)

        final = self._step("final", res.final_table, sqlgen.final_step(
            audience, [(d["item_id"], attr_tables[d["item_id"]]) for d in dimensions],
            [(m["item_id"], metric_tables[m["item_id"]]) for m in metrics],
            [(f["item_id"], attr_tables[f["item_id"]]) for f in filters]), None, expire=None)
        steps.append(final)
        if final.status == "failed":
            raise DataQualityError(f"final step failed: {final.error}")
        t1 = time.perf_counter()
        try:
            self._gates_after(res.final_table, res.audience_users, [m["item_id"] for m in metrics])
        except DataQualityError as e:
            steps.append(StepResult("gates_after", None, "failed", None, round(time.perf_counter() - t1, 2), str(e)))
            raise
        steps.append(StepResult("gates_after", None, "ok", None, round(time.perf_counter() - t1, 2)))
        arms = self.wh.query_df(f"SELECT arm, COUNT(*) AS n FROM `{res.final_table}` GROUP BY arm").set_index("arm")["n"]
        res.n_control, res.n_variant = int(arms.get("control", 0)), int(arms.get("variant", 0))
        return res

    def daily_series(self, experiment_id: str, metrics: list[dict], launch_date: date, end_date: date) -> list[dict]:
        """Per metric, day and arm: users so far and the cumulative mean. Run right after build(), on the same audience.
        One query per metric, in parallel."""
        audience = sqlgen.table(self.wh.analytics, experiment_id, "audience")

        def one(m: dict) -> list[dict]:
            df = self.wh.query_df(sqlgen.daily_step(audience, self._sql(m), m["aggregation"]), {"launch": launch_date, "end_date": end_date})
            return [{"item_id": m["item_id"], "day": r.day, "arm": r.arm, "n_users": int(r.n_users), "mean_value": float(r.mean_value),
                     "var_value": None if pd.isna(r.var_value) else float(r.var_value)}
                    for r in df.itertuples()]

        with ThreadPoolExecutor(self.workers) as pool:
            return [row for rows in pool.map(one, metrics) for row in rows]

    def segment_stats(self, final_table: str, dimension_ids: list[str], filter_ids: list[str], metric_ids: list[str]
                      ) -> dict[tuple[str, str], dict[str, dict[str, dict[str, Arm]]]]:
        """Count, mean and variance per arm for each metric in every slice the page offers, computed in the warehouse:
        {(dimension, filter): {segment: {metric: {arm: Arm}}}}. A slice is a dimension value, a filter (users who pass it), or both
        together; "" stands for none. The everyone-and-no-filter slice is the main result, so it is not repeated here."""
        cols = ", ".join(f"AVG(CAST(`{m}` AS FLOAT64)) AS `{m}__mean`, VAR_SAMP(CAST(`{m}` AS FLOAT64)) AS `{m}__var`" for m in metric_ids)

        def one(key: tuple[str, str]) -> tuple[tuple[str, str], dict]:
            dim, flt = key
            seg = f"CAST(`{dim}` AS STRING)" if dim else "''"
            where = f"WHERE `{flt}` = 1" if flt else ""
            df = self.wh.query_df(f"SELECT {seg} AS segment, arm, COUNT(*) AS n, {cols} FROM `{final_table}` {where} GROUP BY segment, arm")
            out: dict = {}
            for r in df.itertuples(index=False):
                r = r._asdict()
                for m in metric_ids:
                    var = r[f"{m}__var"]
                    out.setdefault(r["segment"], {}).setdefault(m, {})[r["arm"]] = Arm(
                        int(r["n"]), float(r[f"{m}__mean"]), 0.0 if pd.isna(var) else float(var))
            return key, out

        keys = [(d, f) for d in ["", *dimension_ids] for f in ["", *filter_ids] if d or f]
        with ThreadPoolExecutor(self.workers) as pool:
            return dict(pool.map(one, keys))

    def load_final(self, experiment_id: str) -> pd.DataFrame:
        return self.wh.query_df(f"SELECT * FROM `{sqlgen.table(self.wh.analytics, experiment_id, 'final')}`")

    # ---- summary statistics (one scan, any experiment size) -----------------------------------
    def summary_stats(self, final_table: str, metric_ids: list[str]) -> dict[str, dict[str, Arm]]:
        """Count, mean and sample variance per arm for each metric, computed in the warehouse. Python never sees user rows."""
        cols = ", ".join(f"AVG(CAST(`{m}` AS FLOAT64)) AS `{m}__mean`, VAR_SAMP(CAST(`{m}` AS FLOAT64)) AS `{m}__var`" for m in metric_ids)
        df = self.wh.query_df(f"SELECT arm, COUNT(*) AS n, {cols} FROM `{final_table}` GROUP BY arm").set_index("arm")
        out: dict[str, dict[str, Arm]] = {}
        for m in metric_ids:
            out[m] = {arm: Arm(int(r.n), float(r[f"{m}__mean"]), 0.0 if pd.isna(r[f"{m}__var"]) else float(r[f"{m}__var"]))
                      for arm, r in df.iterrows()}
        return out
