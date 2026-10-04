"""The charts of a results page: the same charts the tool draws, as Vega-Lite specs the page renders in the browser.

The chart builders live with the app (app/results_view.py, plain functions with no Streamlit calls), so this module borrows them rather than
drawing the charts a second time: a chart on a page and the chart in the tool can never disagree."""
import sys
from pathlib import Path

_APP = Path(__file__).resolve().parents[3] / "app"
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

import results_view as rv  # noqa: E402  (the app's table and chart builders)


def results_view_module():
    return rv


def _spec(chart) -> dict:
    return chart.properties(width="container").to_dict()


def build_charts(series: list[dict], metric, bayesian: bool, is_final: bool, outcome=None, threshold: float | None = None) -> list[dict]:
    """[{key, title, spec}] for the primary metric. `series` are the experiment's daily_stats rows; `outcome` and `threshold` (in the metric's
    units) are the primary metric's Bayesian numbers, needed for the risk charts."""
    binary = metric.type == "binary"
    item = metric.metric_id
    charts = [{"key": "time", "title": "Control and variant over time",
               "spec": _spec(rv.time_chart(rv.series_frame(series, item, binary), binary, ""))}]
    if bayesian:
        frame = rv.bayes_time_frame(series, metric, threshold)
        if outcome is not None:
            charts.append({"key": "risk", "title": "Risk of each choice, on all the data",
                           "spec": _spec(rv.risk_chart(rv.risk_frame(outcome, binary, threshold), binary, ""))})
        if not frame.empty:
            charts.append({"key": "risk_time", "title": "Risk of each choice, by day",
                           "spec": _spec(rv.risk_time_chart(frame, binary, ""))})
            charts.append({"key": "difference", "title": "Difference with its 95% credible interval, by day",
                           "spec": _spec(rv.diff_chart(frame, binary, "", band_label="95% credible"))})
            charts.append({"key": "chance", "title": "Chance the variant wins, by day", "spec": _spec(rv.chance_chart(frame, ""))})
    else:
        charts.append({"key": "difference", "title": "Difference over time" + (", with its 95% range" if is_final else ""),
                       "spec": _spec(rv.diff_chart(rv.diff_frame(series, item, binary, band=is_final), binary, ""))})
    return charts
