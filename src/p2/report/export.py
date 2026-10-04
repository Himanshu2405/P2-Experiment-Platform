"""Export one experiment's saved results from the tool into a plain JSON file (results.json) for its results page.

Read-only: it only reads from the tool's store and never writes to it. The page then needs no database. Everything the page shows that the tool
does not store itself (a Win, Neutral or Loss label for secondary metrics, the balance p-value, the chart specs) is worked out here from the tool's own
functions, so the tool is not changed.
"""
import json
import sys
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from p2.pipeline.runner import end_of
from p2.report import charts as chart_module
from p2.stats import bayes
from p2.stats.srm import srm_check

WIN_AT, LOSS_AT = 0.90, 0.10     # a secondary metric is a Win when the chance the variant is better is at least WIN_AT, a Loss at or below LOSS_AT
LABEL_RULE = ("Secondary metrics are labelled Win when the chance the variant is better is {win:.0%} or more, Loss when it is {loss:.0%} or less, "
              "and Neutral in between.").format(win=WIN_AT, loss=LOSS_AT)
FREQUENTIST_LABEL = {"Significant improvement": "Win", "Significant decline": "Loss"}


def _json_default(x):
    if isinstance(x, (date, datetime)):
        return x.isoformat()
    raise TypeError(f"cannot serialise {type(x).__name__}")


def _label(role: str, bayesian: bool, row: dict, outcome) -> str:
    """The label shown in the Verdict column of the results table."""
    if bayesian:
        if role in ("primary", "guardrail"):
            return outcome.verdict if outcome and outcome.verdict else "Refresh needed"
        if outcome is None:
            return "Refresh needed"
        if outcome.chance_to_win >= WIN_AT:
            return "Win"
        return "Loss" if outcome.chance_to_win <= LOSS_AT else "Neutral"
    verdict = row.get("verdict") or ""
    if role == "secondary":
        return FREQUENTIST_LABEL.get(verdict, "Neutral")
    return verdict


def export_results(plat, experiment_id: str, exported_on: date | None = None) -> dict:
    """Everything a results page needs about one experiment, as a JSON-ready dict. `plat` is a Platform (BigQuery or memory store)."""
    exp = plat.get_experiment(experiment_id)
    registry, design = plat.metric_registry(), [d for d in plat.get_design(experiment_id) if d["kind"] == "metric"]
    final, interim = plat.latest_results(experiment_id, "final"), plat.latest_results(experiment_id, "interim")
    is_final = bool(final) and (not interim or final[0]["run_at"] >= interim[0]["run_at"])
    rows = final if is_final else interim
    if not rows:
        raise ValueError(f"{experiment_id} has no results yet; run it in the tool first")
    bayesian = exp.get("method") == "bayesian"
    end = end_of(exp)
    design_of = {d["item_id"]: d for d in design}
    head = next(r for r in rows if r["role"] == "primary")
    pm = registry.get(head["item_id"], design_of[head["item_id"]]["item_version"])
    outcomes = plat.bayes_outcomes(experiment_id, rows) if bayesian else [None] * len(rows)

    metrics = []
    for r, out in zip(rows, outcomes):
        metric = registry.get(r["item_id"], design_of[r["item_id"]]["item_version"])
        m = {"item_id": r["item_id"], "name": metric.display_name, "role": r["role"], "type": "rate" if metric.type == "binary" else "continuous",
             "good_direction": metric.good_direction, "description": getattr(metric, "description", "") or "",
             "control": r["mean_control"], "variant": r["mean_variant"], "difference": r["difference"], "lift": r["relative_lift"],
             "n_control": r["n_control"], "n_variant": r["n_variant"], "label": _label(r["role"], bayesian, r, out)}
        if bayesian and out is not None:
            m.update({"chance_to_win": out.chance_to_win, "expected_difference": out.difference, "ci_low": out.ci_low, "ci_high": out.ci_high,
                      "risk_variant": out.risk_variant, "risk_control": out.risk_control, "chance_of_harm": out.chance_of_harm})
        elif not bayesian:
            m.update({"p_value": r.get("p_value"), "ci_low": r.get("ci_low"), "ci_high": r.get("ci_high"), "ci_level": r.get("ci_level"),
                      "achieved_power": r.get("achieved_power")})
        metrics.append(m)

    # the primary metric's verdict, its one-line reason, and the numbers the risk charts need
    rv = chart_module.results_view_module()
    is_rate = pm.type == "binary"
    head_out, threshold = (outcomes[rows.index(head)] if bayesian else None), None
    if bayesian:
        plan = bayes.plan_from_row(pm, design_of[head["item_id"]])
        days = (end - exp["launch_date"]).days + 1
        threshold = plan.threshold_abs(head["mean_control"]) if plan and plan.role == "primary" else None
        min_days = min(plan.min_days if plan and plan.role == "primary" else bayes.DEFAULT_MIN_DAYS, days)
        verdict = {"text": head_out.verdict if head_out and head_out.verdict else "Refresh needed",
                   "reason": rv.bayes_reason(head_out, is_rate, threshold, min_days)}
        plan_summary = {"risk_threshold": threshold, "min_days": plan.min_days, "harm_limit": next(
            (d["harm_limit"] for d in design if d["role"] == "guardrail" and d.get("harm_limit")), None)}
    else:
        verdict = {"text": head["verdict"] if is_final and head["verdict"] else "In progress", "reason": rv.frequentist_reason(head, is_final, end)}
        d = design_of[head["item_id"]]
        plan_summary = {"alpha": d["alpha"], "power": d["power"], "sidedness": d["sidedness"]}

    srm = srm_check(head["n_control"], head["n_variant"])
    check = (head.get("params") or {}).get("placebo")
    placebo = None
    if check:
        chip, tone, text = rv.placebo_summary(check)
        placebo = {**check, "passed": bool(check["ok"]), "text": text.replace("**", "")}
    series = plat.daily_series(experiment_id)
    return {
        "experiment": {"id": exp["experiment_id"], "name": exp["name"], "method": "bayesian" if bayesian else "frequentist", "product": exp["product_id"],
                       "owner": exp["owner_user_id"], "hypothesis": exp["hypothesis"], "launch_date": exp["launch_date"], "end_date": end,
                       "runtime_days": exp["runtime_days"], "status": exp["status"]},
        "is_final": is_final, "through_date": head.get("through_date") or end, "run_at": head["run_at"],
        "sample": {"n_control": head["n_control"], "n_variant": head["n_variant"]},
        "primary": {"item_id": head["item_id"], "name": pm.display_name},
        "plan": plan_summary, "verdict": verdict, "metrics": metrics,
        "checks": {"balance": {"balanced": srm.balanced, "p_value": srm.p_value, "n_control": srm.n_control, "n_variant": srm.n_variant},
                   "placebo": placebo},
        "label_rule": LABEL_RULE if bayesian else "",
        "charts": chart_module.build_charts(series, pm, bayesian, is_final, head_out, threshold),
        "exported_on": exported_on or date.today(),
    }


def write_results(plat, experiment_id: str, folder: Path, exported_on: date | None = None) -> Path:
    data = export_results(plat, experiment_id, exported_on)
    path = Path(folder) / "results.json"
    path.write_text(json.dumps(data, default=_json_default, indent=1) + "\n")
    return path


def _bigquery_platform():
    """The same store the app uses, read-only here (this module never calls a write method)."""
    import os
    from p2.services.platform import Platform
    from p2.store.bigquery import BigQueryStore
    from p2.warehouse.bq import Warehouse
    from p2.warehouse.sources import load_sources
    return Platform(BigQueryStore(Warehouse(env=os.environ.get("P2_ENV", "dev"))), load_sources())


def main(argv: list[str]) -> None:
    """python -m p2.report.export <experiment-id> [...]: write experiments/<id>/results.json from the tool's saved results."""
    root = Path(__file__).resolve().parents[3] / "experiments"
    if not argv:
        sys.exit("usage: python -m p2.report.export <experiment-id> [<experiment-id> ...]")
    plat = _bigquery_platform()
    for eid in argv:
        folder = root / eid
        if not folder.is_dir():
            sys.exit(f"no folder {folder}: write the design doc first")
        print("wrote", write_results(plat, eid, folder))


if __name__ == "__main__":
    main(sys.argv[1:])
