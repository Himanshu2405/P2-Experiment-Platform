"""Test doubles for the warehouse-backed parts of the platform. The app uses them when P2_STORE=memory."""
from datetime import date, timedelta

from p2.pipeline.runner import BuildResult, DataQualityError, StepResult
from p2.stats.tests import Arm


class FakeChecker:
    """Passes everything except SQL containing the word FAIL. Records what it was asked to check."""

    def __init__(self):
        self.calls = []

    def check(self, item: dict) -> dict:
        self.calls.append((item["item_id"], item["version"]))
        bad = "FAIL" in item["sql"]
        return {"passed": not bad, "checks": [{"name": "fake", "status": "fail" if bad else "pass",
                                               "detail": "contains FAIL" if bad else "ok"}], "stats": {"n_rows": 10}}


class FakeRunner:
    """Pretends to build experiment tables. An experiment id containing 'bad' fails a data-quality gate."""

    def __init__(self):
        self.builds = []
        self.last_steps = []
        self.columns = {}  # experiment id -> (dimensions, metrics) of its latest build

    def build(self, experiment_id, product_id, metrics, dimensions, filters, launch_date, end_date, as_of=None, final=True):
        self.builds.append({"experiment_id": experiment_id, "metrics": [m["item_id"] for m in metrics],
                            "dimensions": [d["item_id"] for d in dimensions], "filters": [f["item_id"] for f in filters],
                            "launch_date": launch_date, "end_date": end_date, "final": final})
        steps = [StepResult("gates_before", None, "ok", None, 0.1), StepResult("cohort", "t.cohort", "ok", 1000, 0.2)]
        self.last_steps = steps
        self.columns[experiment_id] = ([d["item_id"] for d in dimensions], [m["item_id"] for m in metrics])
        if "bad" in experiment_id:
            steps.append(StepResult("gates_after", None, "failed", None, 0.1, "final table has 1 rows but the audience has 2 users"))
            raise DataQualityError("final table has 1 rows but the audience has 2 users")
        steps += [StepResult(f"m_{m['item_id']}", f"t.m_{m['item_id']}", "ok", 900, 0.3) for m in metrics]
        return BuildResult(f"t.{experiment_id}__final", steps, 1000, 900, 450, 450)

    def summary_stats(self, final_table, metric_ids):
        """Control 10% (mean 5 for money-like metrics), variant a clear, significant lift; 450 users per arm."""
        return {m: {"control": Arm(450, 0.10, 0.09), "variant": Arm(450, 0.16, 0.1344)} for m in metric_ids}

    def segment_stats(self, final_table, dimension_ids, filter_ids, metric_ids):
        """Every slice has two segments for a dimension ('a' repeats the overall numbers, 'b' has a smaller, weaker effect) and one
        unnamed segment for a filter alone (a quarter smaller than everyone, with the same effect)."""
        out = {}
        for d in ["", *dimension_ids]:
            for f in ["", *filter_ids]:
                if not d and not f:
                    continue
                shrink = 0.75 if f else 1.0
                segs = {"a": (300, 0.16), "b": (150, 0.11)} if d else {"": (450, 0.16)}
                out[(d, f)] = {seg: {m: {"control": Arm(round(n * shrink), 0.10, 0.09), "variant": Arm(round(n * shrink), v, v * (1 - v))}
                                     for m in metric_ids} for seg, (n, v) in segs.items()}
        return out

    def daily_series(self, experiment_id, metrics, launch_date, end_date):
        """One row per metric, day and arm: users grow and the means drift up to the final summary values."""
        rows, days = [], (end_date - launch_date).days + 1
        for m in metrics:
            for i in range(days):
                share = (i + 1) / days
                for arm, final_mean, n, var in (("control", 0.10, 450, 0.09), ("variant", 0.16, 450, 0.1344)):
                    rows.append({"item_id": m["item_id"], "day": launch_date + timedelta(days=i), "arm": arm,
                                 "n_users": round(n * share), "mean_value": final_mean * (0.5 + 0.5 * share), "var_value": var})
        return rows

    def assignment_summary(self, experiment_id):
        if experiment_id.startswith("nolog"):      # an id the experiment tool never logged
            return {"n_users": 0, "arms": 0, "first_day": None, "last_day": None}
        return {"n_users": 1000, "arms": 2, "first_day": date(2026, 1, 1), "last_day": date(2026, 1, 14)}

    def load_final(self, experiment_id):
        import pandas as pd
        dims, metrics = self.columns.get(experiment_id, ([], []))
        frame = {"user_id": ["u1", "u2", "u3", "u4"], "arm": ["control", "variant", "control", "variant"], "assigned_date": [date(2026, 1, 1)] * 4}
        frame.update({d: ["a", "b", "a", "b"] for d in dims})
        frame.update({m: [0, 1, 1, 1] for m in metrics})
        return pd.DataFrame(frame)
