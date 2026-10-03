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



def test_the_bayesian_table_shows_chances_risks_and_a_verdict_and_tells_when_a_run_is_needed():
    from p2.stats.bayes import BayesOutcome
    from results_view import bayes_table
    o = BayesOutcome(0.972, 0.0101, 0.25, 0.0002, 0.0201, 0.0001, 0.0102, None, "Variant is the safer choice")
    harm = BayesOutcome(0.1, -0.001, -0.2, -0.003, 0.001, 0.003, 0.0001, 0.034, "Inconclusive")
    rows = [row("conversion_rate", "primary", 0.04, 0.05, final=False), row("refund_rate", "guardrail", 0.005, 0.004, final=False),
            row("revenue_per_user", "secondary", 3.0, 3.3, final=False)]
    df = bayes_table(rows, [o, harm, None], REG, DESIGN)
    assert list(df.columns) == ["Metric", "Role / type", "Control", "Variant", "Lift", "Chance variant wins", "Expected difference", "95% credible interval",
                                "Risk: ship variant", "Risk: keep control", "Chance of harm", "Verdict", "Users (control / variant)"]
    a, g, s = df.iloc[0], df.iloc[1], df.iloc[2]
    assert (a["Chance variant wins"], a["Expected difference"], a["95% credible interval"]) == ("97.2%", "+1.010 pp", "+0.020 pp to +2.010 pp")
    assert (a["Risk: ship variant"], a["Risk: keep control"], a["Chance of harm"], a.Verdict) == ("0.010 pp", "1.020 pp", "", "✅ Variant is the safer choice")
    assert (g["Risk: ship variant"], g["Chance of harm"], g.Verdict) == ("", "3.4%", "⚠️ Inconclusive")       # a guardrail shows harm, not the two risks
    assert s["Chance variant wins"] == "" and s.Verdict == ""                                                 # nothing to rebuild and nothing to wait for
    waiting = bayes_table(rows[:1], [None], REG, DESIGN)
    assert waiting.iloc[0].Verdict == "Refresh to see Bayesian numbers"
    assert df.attrs["good_up"] == [True, False, True]
    style_table(df)                                                                                           # the colours apply to this table too


def test_collecting_evidence_has_an_hourglass_and_the_bayesian_verdicts_are_tinted():
    from p2.stats.bayes import BayesOutcome
    from results_view import bayes_table
    wait = BayesOutcome(0.6, 0.001, 0.1, -0.01, 0.01, 0.002, 0.001, None, "Collecting evidence (day 2 of 7)")
    df = bayes_table([row("conversion_rate", "primary", 0.04, 0.05, final=False)], [wait], REG, DESIGN)
    assert df.iloc[0].Verdict == "⏳ Collecting evidence (day 2 of 7)"
    assert GREY in style_table(df).to_html()                                                   # waiting is grey, not the yellow of "inconclusive"
    few = row("conversion_rate", "primary", 0.04, 0.05, final=False, n_control=1)
    assert bayes_table([few], [None], REG, DESIGN).iloc[0].Verdict == "Too few users yet"


def test_the_bayesian_charts_build_from_the_numbers():
    from p2.stats.bayes import BayesOutcome
    from results_view import bayes_time_frame, chance_chart, risk_chart, risk_frame, risk_time_chart
    conv = REG.get("conversion_rate")
    out = BayesOutcome(0.9, 0.01, 0.1, 0.0, 0.02, 0.0003, 0.01, None)
    rf = risk_frame(out, True, 0.0005)
    assert list(rf.choice) == ["Ship variant", "Keep control"] and list(rf.risk) == pytest.approx([0.03, 1.0]) and list(rf.threshold) == pytest.approx([0.05, 0.05])
    assert list(rf.safe) == ["Under the threshold", "Over the threshold"] and list(rf.label) == ["0.030 pp", "1.000 pp"]
    assert len(risk_chart(rf, True, "t").to_dict()["layer"]) == 4                                     # bars, values, threshold line and its label
    no_threshold = risk_frame(out, True, None)
    assert set(no_threshold.safe) == {"No threshold"} and len(risk_chart(no_threshold, True, "t").to_dict()["layer"]) == 2
    series = [{"item_id": "conversion_rate", "day": date(2026, 1, d), "arm": arm, "n_users": 200 * d, "mean_value": m, "var_value": m * (1 - m)}
              for d in range(1, 6) for arm, m in (("control", 0.10), ("variant", 0.12))]
    tf = bayes_time_frame(series, conv, 0.0005)
    assert len(tf) == 5 and tf.chance_to_win.is_monotonic_increasing and tf.chance_to_win.between(0, 100).all()
    assert (tf.low < tf.difference).all() and (tf.difference < tf.high).all() and tf.difference.iloc[-1] == pytest.approx(2.0, abs=0.1)   # pp
    assert (tf.high - tf.low).is_monotonic_decreasing                                                # more users, a narrower interval
    assert tf.risk_control.iloc[-1] > 1 and tf.threshold.iloc[0] == pytest.approx(0.05)              # in pp: a 2 pp gap risks about 2 pp, not 0.02
    assert tf.risk_control.iloc[-1] > tf.risk_variant.iloc[-1]                                       # the variant is ahead, so keeping control risks more
    assert len(risk_time_chart(tf, True, "t").to_dict()["layer"]) == 3                               # two lines, the threshold and its label
    assert len(risk_time_chart(bayes_time_frame(series, conv, None), True, "t").to_dict()["layer"]) == 1
    spec = str(diff_chart(tf, True, "t", band_label="95% credible").to_dict())
    assert "95% credible low" in spec and chance_chart(tf, "t").to_dict()["layer"]
    assert bayes_time_frame(series, REG.get("refund_rate"), None).empty


def test_the_risk_and_harm_sentences_say_the_decision_in_plain_words():
    from p2.stats.bayes import BayesOutcome
    from results_view import harm_sentence, risk_sentence
    o = lambda rv, rc: BayesOutcome(0.5, 0.0, 0.0, -0.01, 0.01, rv, rc, None)
    assert risk_sentence(o(0.0001, 0.004), True, 0.0005).endswith("so **the variant is the safer choice**.")
    assert "we lose **0.010 pp** on average" in risk_sentence(o(0.0001, 0.004), True, 0.0005)
    assert risk_sentence(o(0.004, 0.0001), True, 0.0005).endswith("so **control is the safer choice**.")
    assert risk_sentence(o(0.0001, 0.0002), True, 0.0005).endswith("so **either choice is fine**.")
    assert risk_sentence(o(0.001, 0.0044), True, 0.0005).endswith("so **there is not enough evidence**.")
    assert "most we accept" not in risk_sentence(o(0.001, 0.004), True, None)                      # a secondary metric has no threshold
    h = BayesOutcome(0.1, 0.0, 0.0, -0.01, 0.01, 0.0, 0.0, 0.034)
    assert harm_sentence(h, True, 0.001, 0.01) == ("There is a **3.4%** chance this guardrail got worse by more than 0.100 pp. "
                                                  "It passes when that chance is below **1%**.")


def test_the_verdict_reason_is_one_plain_line_for_each_method():
    from p2.stats import bayes
    from p2.stats.bayes import BayesOutcome
    from results_view import bayes_reason, frequentist_reason
    win = {"p_value": 0.003, "verdict": "Significant improvement", "params": {"alpha": 0.05}}
    assert frequentist_reason(win, True, date(2026, 1, 14)) == "The lift is real, not chance (p = 0.003, below your alpha of 0.05)."
    flat = {"p_value": 0.21, "verdict": "No significant difference", "params": {"alpha": 0.1}}
    assert frequentist_reason(flat, True, date(2026, 1, 14)) == "The difference could be chance (p = 0.210, above your alpha of 0.1)."
    wrong_way = {"p_value": 0.01, "verdict": "No significant difference", "params": {"alpha": 0.05}}
    assert "only counts improvements" in frequentist_reason(wrong_way, True, date(2026, 1, 14))
    assert frequentist_reason({"p_value": 0.0001, "verdict": "Significant decline", "params": {}}, True, None) == (
        "The variant is really worse, not by chance (p < 0.001, below your alpha of 0.05).")
    assert frequentist_reason(win, False, date(2026, 1, 14)).startswith("The verdict comes after the last day (2026-01-14)")
    out = lambda rv, rc, v: BayesOutcome(0.5, 0.0, 0.0, -0.01, 0.01, rv, rc, None, v)
    assert bayes_reason(out(0.0001, 0.004, bayes.VERDICT_VARIANT), True, 0.0005, 7) == (
        "Shipping the variant risks only 0.010 pp, under your 0.050 pp limit, while keeping control risks 0.400 pp.")
    assert bayes_reason(out(0.00097, 0.00435, bayes.VERDICT_MORE), True, 0.0005, 7) == (
        "Both choices still risk more than your 0.050 pp limit (ship 0.097 pp, keep 0.435 pp); more data may settle it.")
    assert bayes_reason(out(0.00097, 0.00435, bayes.VERDICT_DONE), True, 0.0005, 7).startswith("The test has ended and both choices still risk more")
    assert bayes_reason(out(0.0001, 0.0002, bayes.VERDICT_EITHER), True, 0.0005, 7).endswith("so either is fine.")
    assert bayes_reason(out(0.004, 0.0001, bayes.VERDICT_CONTROL), True, 0.0005, 7).startswith("Keeping control risks only 0.010 pp")
    assert bayes_reason(out(0.0, 0.0, "Collecting evidence (day 3 of 7)"), True, 0.0005, 7) == "Too early: the verdict comes after 7 days of data."
    assert bayes_reason(None, True, 0.0005, 7) == "Refresh to see the Bayesian numbers."


def test_the_placebo_card_says_in_plain_words_whether_the_analysis_can_be_trusted():
    from results_view import placebo_summary
    freq = {"method": "frequentist", "reps": 200, "n": 4500, "rate": 0.045, "expected": 0.05, "low": 0.015, "high": 0.095, "ok": True}
    chip, tone, text = placebo_summary(freq)
    assert (chip, tone) == ("Placebo passed", "green")
    assert text == ("**The test behaves correctly on your data.** In 200 random splits of your control users into two identical groups of 4,500, "
                    "the test found a difference 4.5% of the time (expected about 5%, normal range 1.5% to 9.5%).")
    chip, tone, text = placebo_summary({**freq, "rate": 0.2, "ok": False})
    assert (chip, tone) == ("Placebo failed", "red") and "finds differences that are not there" in text and "Do not trust the p-values" in text
    bayes_ok = {"method": "bayesian", "reps": 200, "n": 450, "rate": 0.03, "expected": None, "low": None, "high": None, "ok": True}
    chip, tone, text = placebo_summary(bayes_ok)
    assert (chip, tone) == ("Placebo passed", "green") and "named a safer arm 3.0% of the time" in text
    chip, tone, text = placebo_summary({**bayes_ok, "rate": 0.355, "ok": False})
    assert (chip, tone) == ("High false calls", "yellow") and "35.5%" in text and "a smaller threshold makes the rule more cautious" in text


def test_the_placebo_status_card_is_just_passed_or_not_passed():
    from results_view import placebo_status_card
    freq = {"method": "frequentist", "reps": 200, "n": 4500, "rate": 0.045, "expected": 0.05, "low": 0.015, "high": 0.095, "ok": True}
    assert ">Passed<" in placebo_status_card(freq) and "#16a34a" in placebo_status_card(freq)
    assert ">Not passed<" in placebo_status_card({**freq, "rate": 0.2, "ok": False}) and "#dc2626" in placebo_status_card({**freq, "rate": 0.2, "ok": False})
    bayes_high = {"method": "bayesian", "reps": 200, "n": 450, "rate": 0.355, "expected": None, "low": None, "high": None, "ok": False}
    assert ">Not passed<" in placebo_status_card(bayes_high) and "#b7791f" in placebo_status_card(bayes_high)        # a caution, so yellow, not red
    assert "35.5%" in placebo_status_card(bayes_high) and "Do not trust" not in placebo_status_card(bayes_high)
    assert ">Refresh needed<" in placebo_status_card(None)
