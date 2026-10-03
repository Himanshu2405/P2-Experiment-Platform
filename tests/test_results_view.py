"""Tables and chart data for the Experiments page, built from service rows."""
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from p2.catalog.seed import seed_registry  # noqa: E402
from results_view import diff_chart, diff_frame, metric_table, series_frame, style_table, time_chart  # noqa: E402
from ui import BAD, GOOD, GREY, NEUTRAL  # noqa: E402

REG = seed_registry()
DESIGN = [
    {"item_id": "conversion_rate", "item_version": 1, "kind": "metric", "role": "primary", "baseline": 0.04, "effect": 0.10, "effect_kind": "relative"},
    {"item_id": "refund_rate", "item_version": 1, "kind": "metric", "role": "guardrail", "baseline": 0.005, "effect": 0.002, "effect_kind": "absolute"},
    {"item_id": "revenue_per_user", "item_version": 1, "kind": "metric", "role": "secondary", "baseline": None, "effect": None, "effect_kind": None},
]


def row(item_id, role, c, v, final=True, **kw):
    base = {"item_id": item_id, "role": role, "mean_control": c, "mean_variant": v, "difference": v - c, "relative_lift": (v - c) / c,
            "n_control": 500, "n_variant": 510, "p_value": 0.01 if final else None, "ci_low": (v - c) - 0.002 if final else None,
            "ci_high": (v - c) + 0.002 if final else None, "ci_level": 0.95, "verdict": "Significant improvement" if final else None,
            "achieved_power": 0.8 if final else None, "through_date": date(2026, 1, 14)}
    return {**base, **kw}


def test_the_metric_table_puts_the_verdict_next_to_the_lift_and_keeps_cells_short():
    df = metric_table([row("conversion_rate", "primary", 0.04, 0.05), row("revenue_per_user", "secondary", 3.0, 3.3)], REG, DESIGN, True)
    assert list(df.columns) == ["Metric", "Role / type", "Control", "Variant", "Difference", "Lift", "Verdict", "p-value", "CI",
                                "Users (control / variant)", "Power"]
    a, b = df.iloc[0], df.iloc[1]
    assert (a["Role / type"], a.Control, a.Variant, a.Difference, a.Lift) == ("Primary, rate", "4.000%", "5.000%", "+1.000 pp", "+25.0%")
    assert (b["Role / type"], b.Control, b.Difference) == ("Secondary, continuous", "3.000", "+0.300")
    assert a["p-value"] == "0.010" and a.CI == "+0.800 pp to +1.200 pp"                        # a 95% interval needs no label
    assert a.Verdict == "✅ Significant improvement" and a["Users (control / variant)"] == "500 / 510"


def test_a_small_p_value_reads_less_than_and_a_different_interval_level_is_labelled():
    df = metric_table([row("conversion_rate", "primary", 0.04, 0.05, p_value=0.0004, ci_level=0.90)], REG, DESIGN, True)
    assert df.iloc[0]["p-value"] == "<0.001" and df.iloc[0].CI.endswith("(90%)")


def test_live_numbers_have_no_verdict_p_value_or_ci_columns_at_all():
    df = metric_table([row("conversion_rate", "primary", 0.04, 0.05, final=False)], REG, DESIGN, False)
    assert list(df.columns) == ["Metric", "Role / type", "Control", "Variant", "Difference", "Lift", "Users (control / variant)"]
    assert df.iloc[0]["Difference"] == "+1.000 pp" and df.iloc[0]["Lift"] == "+25.0%"


def test_the_colours_follow_the_good_direction_and_no_difference_is_grey():
    rows = [row("conversion_rate", "primary", 0.04, 0.05), row("refund_rate", "guardrail", 0.005, 0.006),
            row("conversion_rate", "primary", 0.04, 0.0401, verdict="No significant difference")]
    df = metric_table(rows, REG, DESIGN, True)
    ctx = style_table(df)._compute().ctx

    def tint(i, col):
        return next((v for k, v in ctx.get((i, list(df.columns).index(col)), []) if k == "background-color"), None)

    assert tint(0, "Lift") == GOOD                                                           # conversion went up: good
    assert tint(1, "Lift") == BAD                                                            # refund rate went up and lower is better: bad
    assert tint(2, "Lift") == NEUTRAL                                                        # a 0.25% move is flat: yellow
    assert tint(2, "Verdict") == GREY and tint(0, "Verdict") == GOOD                         # no clear difference is grey, not yellow


def test_the_series_frame_is_in_percent_for_rates_and_ordered_for_plotting():
    series = [{"item_id": "conversion_rate", "day": date(2026, 1, d), "arm": a, "n_users": 10 * d, "mean_value": 0.04 + 0.001 * d}
              for d in (1, 2) for a in ("control", "variant")] + [{"item_id": "other", "day": date(2026, 1, 1), "arm": "control", "n_users": 1, "mean_value": 9}]
    df = series_frame(series, "conversion_rate", True)
    assert len(df) == 4 and df.value.iloc[0] == pytest.approx(4.1) and set(df.arm) == {"control", "variant"}
    assert series_frame(series, "conversion_rate", False).value.iloc[0] == pytest.approx(0.041)
    assert series_frame(series, "missing", True).empty
    assert time_chart(df, True, "Conversion rate").to_dict()["mark"]["type"] == "line"


def _series(days=3, n=100, var=0.09, c0=0.10, step=0.01):
    rows = []
    for d in range(days):
        for arm, base in (("control", c0), ("variant", c0 + 0.02)):
            rows.append({"item_id": "conversion_rate", "day": date(2026, 1, 1 + d), "arm": arm, "n_users": n * (d + 1),
                         "mean_value": base + step * d if arm == "variant" else base, "var_value": var})
    return rows


def test_the_difference_frame_is_variant_minus_control_with_a_range_that_narrows():
    df = diff_frame(_series(), "conversion_rate", True, band=True)
    assert list(df.columns) == ["day", "difference", "n_control", "n_variant", "low", "high"]
    assert df.difference.tolist() == pytest.approx([2.0, 3.0, 4.0])                       # percentage points: variant minus control
    width = (df.high - df.low).tolist()
    assert width[0] > width[1] > width[2] > 0                                              # more users, narrower range
    se = (0.10 * 0.90 / 100 + 0.12 * 0.88 / 100) ** 0.5                                    # day one: as the z-test does for rates
    assert df.low[0] == pytest.approx(2.0 - 1.959964 * se * 100)
    assert (df.low < df.difference).all() and (df.difference < df.high).all()


def test_without_the_band_there_is_only_the_line_so_nobody_reads_a_verdict_early():
    df = diff_frame(_series(), "conversion_rate", True, band=False)
    assert list(df.columns) == ["day", "difference", "n_control", "n_variant"]
    spec = diff_chart(df, True, "Conversion rate (primary)").to_dict()
    assert [layer["mark"]["type"] if isinstance(layer["mark"], dict) else layer["mark"] for layer in spec["layer"]] == ["rule", "line"]


def test_with_the_band_the_chart_adds_a_shaded_area_and_units_follow_the_metric_type():
    df = diff_frame(_series(), "conversion_rate", True, band=True)
    spec = diff_chart(df, True, "Conversion rate (primary)").to_dict()
    assert [layer["mark"]["type"] for layer in spec["layer"]] == ["rule", "area", "line"]
    assert "(pp)" in str(spec["layer"][2]["encoding"]["y"]["title"])                               # rates are shown in percentage points
    money = [{**r, "item_id": "revenue_per_user", "mean_value": r["mean_value"] * 50, "var_value": 400.0} for r in _series()]
    assert diff_frame(money, "revenue_per_user", False, band=True).difference.iloc[0] == pytest.approx(1.0)       # not multiplied by 100


def test_days_with_too_few_users_get_no_range_and_empty_series_gives_an_empty_frame():
    rows = _series(days=2, n=1)                                                             # one user per arm on day one
    df = diff_frame(rows, "conversion_rate", True, band=True)
    assert pd.isna(df.low[0]) and not pd.isna(df.low[1])
    assert diff_frame([], "conversion_rate", True, band=True).empty


def test_segment_tables_have_no_power_column():
    seg = row("conversion_rate", "primary", 0.04, 0.05)
    seg.pop("achieved_power")
    df = metric_table([seg], REG, DESIGN, True, power=False)
    assert "Power" not in df.columns and df.iloc[0].Verdict == "✅ Significant improvement"

