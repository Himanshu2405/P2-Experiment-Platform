"""Tables and charts for one experiment's numbers. Plain functions that turn service rows into frames and charts, so they can
be tested without a browser."""
import altair as alt
import pandas as pd

from p2.stats import bayes
from ui import BAD, GOOD, GREY, NEUTRAL, check_card, chip

ICON = {"Significant improvement": "✅", "Significant decline": "❌", "No significant difference": "➖", "Not significant": "➖",
        "Passed": "✅", "Inconclusive": "⚠️", "Failed": "❌", "Variant is the safer choice": "✅", "Control is the safer choice": "❌",
        "No meaningful difference": "➖", "Not enough evidence yet": "⚠️"}
ARM_COLORS = alt.Scale(domain=["control", "variant"], range=["#94a3b8", "#3b82f6"])    # blue is the variant; green and red are kept for good and bad
LINE_BLUE, BAND_BLUE, ZERO_GREY = "#3b82f6", "#3b82f6", "#94a3b8"


def _binary(registry, design: list[dict], item_id: str) -> bool:
    version = next(d["item_version"] for d in design if d["item_id"] == item_id)
    return registry.get(item_id, version).type == "binary"


def fmt_value(binary: bool, x: float | None, diff: bool = False) -> str:
    if x is None or pd.isna(x):
        return ""
    if binary:
        return f"{x * 100:+.3f} pp" if diff else f"{x * 100:.3f}%"
    return f"{x:+,.3f}" if diff else f"{x:,.3f}"


def metric_table(rows: list[dict], registry, design: list[dict], final: bool, power: bool = True) -> pd.DataFrame:
    """One row per metric. Live (interim) numbers have no Verdict, p-value or CI columns at all; the final analysis adds them, with the
    Verdict next to the Lift. Segment tables omit power. The CI is a 95% interval unless the cell says otherwise."""
    out, good_up = [], []
    for r in rows:
        binary = _binary(registry, design, r["item_id"])
        good_up.append(registry.get(r["item_id"], next(d["item_version"] for d in design if d["item_id"] == r["item_id"])).good_direction == "higher")
        lift = "" if r["relative_lift"] is None else f"{r['relative_lift'] * 100:+.1f}%"
        row = {"Metric": registry.get(r["item_id"]).display_name, "Role / type": f"{r['role'].capitalize()}, {'rate' if binary else 'continuous'}",
               "Control": fmt_value(binary, r["mean_control"]), "Variant": fmt_value(binary, r["mean_variant"]),
               "Difference": fmt_value(binary, r["difference"], True), "Lift": lift}
        if final:
            row["Verdict"] = f"{ICON.get(r['verdict'], '')} {r['verdict']}"
            row["p-value"] = "" if r["p_value"] is None else ("<0.001" if r["p_value"] < 0.001 else f"{r['p_value']:.3f}")
            level = "" if round(r["ci_level"], 2) == 0.95 else f" ({r['ci_level'] * 100:.0f}%)"
            row["CI"] = f"{fmt_value(binary, r['ci_low'], True)} to {fmt_value(binary, r['ci_high'], True)}{level}"
        row["Users (control / variant)"] = f"{r['n_control']:,} / {r['n_variant']:,}"
        if final and power:
            row["Power"] = "" if r.get("achieved_power") is None else f"{r['achieved_power'] * 100:.0f}%"
        out.append(row)
    df = pd.DataFrame(out)
    df.attrs["good_up"] = good_up
    return df


NEUTRAL_BAND = 0.5   # a lift smaller than this many percent counts as no movement


def _sign(text: str) -> float | None:
    """The number in a cell such as '+28.4%' or '-0.852 pp' (None when blank)."""
    try:
        return float(text.replace("%", "").replace("pp", "").replace(",", "").strip())
    except ValueError:
        return None


def style_table(df: pd.DataFrame):
    """The metric table with colour: lift and difference go green when the variant moved the good way, red the bad way and yellow when it
    barely moved (a lower-is-better metric flips); verdicts and small p-values are tinted; control and variant get a soft tint each."""
    good_up = df.attrs.get("good_up", [True] * len(df))
    lift_values = [_sign(x) for x in df["Lift"]]
    tones = []
    for value, up in zip(lift_values, good_up):
        if value is None:
            tones.append("")
        elif abs(value) < NEUTRAL_BAND:
            tones.append(NEUTRAL)
        else:
            tones.append(GOOD if (value > 0) == up else BAD)

    def row_tint(col):
        return [f"background-color: {t}; font-weight: 600" if t else "" for t in tones]

    def verdict_tint(col):
        out = []
        for v in col:
            word = v.split(" ", 1)[1] if v[:1] in "✅❌➖⚠⏳" and " " in v else v
            tint = (GOOD if word in ("Significant improvement", "Passed", "Variant is the safer choice")
                    else BAD if word in ("Significant decline", "Failed", "Control is the safer choice")
                    else GREY if word in ("No significant difference", "Not significant", "No meaningful difference") or word.startswith("Collecting evidence")
                    else NEUTRAL if word else "")
            out.append(f"background-color: {tint}; font-weight: 600" if tint else "")
        return out

    def p_tint(col):
        return [f"background-color: {GOOD}" if (v == "<0.001" or (_sign(v) is not None and _sign(v) < 0.05)) else "" for v in col]

    styler = df.style
    for col in ("Lift", "Difference"):
        if col in df:
            styler = styler.apply(row_tint, subset=[col])
    if "Verdict" in df:
        styler = styler.apply(verdict_tint, subset=["Verdict"])
    if "p-value" in df:
        styler = styler.apply(p_tint, subset=["p-value"])
    styler = styler.set_properties(subset=["Control"], **{"background-color": "rgba(148, 163, 184, 0.14)"})
    styler = styler.set_properties(subset=["Variant"], **{"background-color": "rgba(59, 130, 246, 0.14)"})
    return styler


# ---- over time -------------------------------------------------------------------------------------------------------------
def series_frame(series: list[dict], item_id: str, binary: bool) -> pd.DataFrame:
    df = pd.DataFrame([s for s in series if s["item_id"] == item_id])
    if df.empty:
        return df
    df["day"] = pd.to_datetime(df["day"])
    df["value"] = df["mean_value"] * (100 if binary else 1)
    return df[["day", "arm", "value", "n_users"]]


def time_chart(df: pd.DataFrame, binary: bool, title: str) -> alt.Chart:
    """Control against variant, cumulative from launch: each point is the average over everyone assigned so far."""
    return (alt.Chart(df, title=title).mark_line(point=alt.OverlayMarkDef(size=90))
            .encode(x=alt.X("day:T", title="Day", axis=alt.Axis(format="%b %d")),
                    y=alt.Y("value:Q", title=f"Cumulative average{' (%)' if binary else ''}", scale=alt.Scale(zero=False)),
                    color=alt.Color("arm:N", scale=ARM_COLORS, title="Arm"),
                    tooltip=[alt.Tooltip("day:T", title="Day", format="%b %d"), alt.Tooltip("arm:N", title="Arm"),
                             alt.Tooltip("value:Q", title="Average (%)" if binary else "Average", format=".3f"),
                             alt.Tooltip("n_users:Q", title="Users so far", format=",")])
            .properties(height=300))


# ---- difference over time with its range -----------------------------------------------------------------------------------
Z95 = 1.959964  # two-sided 95 percent


def diff_frame(series: list[dict], item_id: str, binary: bool, band: bool) -> pd.DataFrame:
    """Variant minus control by day, in percentage points for rates. With `band`, also the 95 percent range of the difference
    (unpooled normal approximation from the per-arm spread); days with too few users get no range."""
    df = pd.DataFrame([s for s in series if s["item_id"] == item_id])
    if df.empty:
        return df
    wide = df.pivot(index="day", columns="arm", values=["n_users", "mean_value", "var_value"]).dropna(subset=[("mean_value", "control"), ("mean_value", "variant")])
    k = 100 if binary else 1
    out = pd.DataFrame({"day": pd.to_datetime(wide.index), "difference": (wide[("mean_value", "variant")] - wide[("mean_value", "control")]).to_numpy() * k,
                        "n_control": wide[("n_users", "control")].to_numpy(), "n_variant": wide[("n_users", "variant")].to_numpy()})
    if band:
        n0, n1 = wide[("n_users", "control")], wide[("n_users", "variant")]
        m0, m1 = wide[("mean_value", "control")], wide[("mean_value", "variant")]
        v0, v1 = (m0 * (1 - m0), m1 * (1 - m1)) if binary else (wide[("var_value", "control")], wide[("var_value", "variant")])   # as the z-test does for rates
        se = ((v1 / n1 + v0 / n0) ** 0.5).where((n0 >= 2) & (n1 >= 2))
        out["low"] = out["difference"] - Z95 * se.to_numpy() * k
        out["high"] = out["difference"] + Z95 * se.to_numpy() * k
    return out


def diff_chart(df: pd.DataFrame, binary: bool, title: str, band_label: str = "Range") -> alt.LayerChart:
    """The difference line, a shaded range around it when there is one, and a grey line at no difference: where the shade
    stays on one side of the grey line, the two arms really differ."""
    zero = alt.Chart(pd.DataFrame({"zero": [0]})).mark_rule(color=ZERO_GREY, strokeDash=[4, 3]).encode(y="zero:Q")
    x = alt.X("day:T", title="Day", axis=alt.Axis(format="%b %d"))
    y = alt.Y("difference:Q", title=f"Variant minus control ({'pp' if binary else 'units'})", scale=alt.Scale(zero=True))
    tip = [alt.Tooltip("day:T", title="Day", format="%b %d"), alt.Tooltip("difference:Q", title="Difference", format=".3f"),
           alt.Tooltip("n_control:Q", title="Control users", format=","), alt.Tooltip("n_variant:Q", title="Variant users", format=",")]
    layers = [zero]
    if "low" in df:
        tip += [alt.Tooltip("low:Q", title=f"{band_label} low", format=".3f"), alt.Tooltip("high:Q", title=f"{band_label} high", format=".3f")]
        layers.append(alt.Chart(df).mark_area(color=BAND_BLUE, opacity=0.18).encode(x=x, y="low:Q", y2="high:Q"))
    layers.append(alt.Chart(df).mark_line(color=LINE_BLUE, point=alt.OverlayMarkDef(size=70, color=LINE_BLUE)).encode(x=x, y=y, tooltip=tip))
    return alt.layer(*layers, title=title).properties(height=260)


# ---- the Bayesian view -----------------------------------------------------------------------------------------------------
def _chance(p: float | None) -> str:
    return "" if p is None else f"{p * 100:.1f}%"


def _loss(binary: bool, x: float | None) -> str:
    """A risk (an average loss, never negative) in the metric's own units: percentage points for rates."""
    if x is None:
        return ""
    return f"{x * 100:.3f} pp" if binary else f"{x:,.3f}"


def bayes_table(rows: list[dict], outcomes: list, registry, design: list[dict]) -> pd.DataFrame:
    """One row per metric from the Bayesian numbers. `outcomes` lines up with `rows` (None where the numbers cannot be rebuilt, for example
    results saved before variances were stored). Primary and guardrail rows carry a verdict; secondary rows show the numbers only."""
    out, good_up = [], []
    for r, o in zip(rows, outcomes):
        binary = _binary(registry, design, r["item_id"])
        good_up.append(registry.get(r["item_id"], next(d["item_version"] for d in design if d["item_id"] == r["item_id"])).good_direction == "higher")
        lift = "" if r["relative_lift"] is None else f"{r['relative_lift'] * 100:+.1f}%"
        row = {"Metric": registry.get(r["item_id"]).display_name, "Role / type": f"{r['role'].capitalize()}, {'rate' if binary else 'continuous'}",
               "Control": fmt_value(binary, r["mean_control"]), "Variant": fmt_value(binary, r["mean_variant"]), "Lift": lift}
        if o is None:
            few = (r["n_control"] or 0) < 2 or (r["n_variant"] or 0) < 2
            row.update({"Chance variant wins": "", "Expected difference": "", "95% credible interval": "", "Risk: ship variant": "",
                        "Risk: keep control": "", "Chance of harm": "",
                        "Verdict": "" if r["role"] == "secondary" else "Too few users yet" if few else "Refresh to see Bayesian numbers"})
        else:
            guard = r["role"] == "guardrail"
            row.update({"Chance variant wins": _chance(o.chance_to_win), "Expected difference": fmt_value(binary, o.difference, True),
                        "95% credible interval": f"{fmt_value(binary, o.ci_low, True)} to {fmt_value(binary, o.ci_high, True)}",
                        "Risk: ship variant": "" if guard else _loss(binary, o.risk_variant),
                        "Risk: keep control": "" if guard else _loss(binary, o.risk_control),
                        "Chance of harm": _chance(o.chance_of_harm),
                        "Verdict": f"{ICON.get(o.verdict, '⏳' if (o.verdict or '').startswith('Collecting') else '')} {o.verdict}".strip() if o.verdict else ""})
        row["Users (control / variant)"] = f"{r['n_control']:,} / {r['n_variant']:,}"
        out.append(row)
    df = pd.DataFrame(out)
    df.attrs["good_up"] = good_up
    return df


def risk_sentence(outcome, binary: bool, threshold: float | None) -> str:
    """The risk plot in one or two plain sentences, for people who do not read statistics."""
    loss = lambda x: _loss(binary, x)
    text = (f"If we ship the variant and it is actually worse, we lose **{loss(outcome.risk_variant)}** on average. "
            f"If we keep control and the variant is actually better, we miss out on **{loss(outcome.risk_control)}** on average.")
    if threshold is None:
        return text
    ship_ok, keep_ok = outcome.risk_variant <= threshold, outcome.risk_control <= threshold
    end = ("only shipping the variant is under it, so **the variant is the safer choice**" if ship_ok and not keep_ok else
           "only keeping control is under it, so **control is the safer choice**" if keep_ok and not ship_ok else
           "both are under it, so **either choice is fine**" if ship_ok else
           "neither is under it yet, so **there is not enough evidence**")
    return f"{text} The most we accept to lose is **{loss(threshold)}**: {end}."


def harm_sentence(outcome, binary: bool, margin: float | None, harm_limit: float) -> str:
    """A guardrail in one plain sentence: the chance it got worse by more than the margin, against the limit."""
    worse = "" if margin is None else f" by more than {_loss(binary, margin)}"
    return (f"There is a **{_chance(outcome.chance_of_harm)}** chance this guardrail got worse{worse}. "
            f"It passes when that chance is below **{harm_limit * 100:.3g}%**.")


def risk_frame(outcome, binary: bool, threshold: float | None) -> pd.DataFrame:
    k = 100 if binary else 1
    risks = [outcome.risk_variant, outcome.risk_control]
    return pd.DataFrame({"choice": ["Ship variant", "Keep control"], "risk": [r * k for r in risks],
                         "label": [_loss(binary, r) for r in risks],
                         "safe": ["Under the threshold" if threshold is not None and r <= threshold else "Over the threshold" if threshold is not None
                                  else "No threshold" for r in risks],
                         "threshold": [None if threshold is None else threshold * k] * 2})


RISK_TONES = alt.Scale(domain=["Under the threshold", "Over the threshold", "No threshold"], range=["#22c55e", "#94a3b8", LINE_BLUE])


def risk_chart(df: pd.DataFrame, binary: bool, title: str) -> alt.LayerChart:
    """The risk plot: what each choice could cost if it turns out wrong. A bar is green when it is under the threshold (the red dashed
    line), grey when it is over; the value is written above the bar."""
    unit = "pp" if binary else "units"
    x = alt.X("choice:N", title=None, sort=["Ship variant", "Keep control"], axis=alt.Axis(labelAngle=0, labelFontSize=13, labelFontWeight="bold"))
    top = max(df["risk"].max(), df["threshold"].max() if df["threshold"].notna().any() else 0) * 1.25 or 1   # room for the value above the bar
    y = alt.Y("risk:Q", title=f"Average loss if wrong ({unit})", scale=alt.Scale(domain=[0, top]))
    bars = (alt.Chart(df).mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4, size=90)
            .encode(x=x, y=y, color=alt.Color("safe:N", scale=RISK_TONES, legend=None),
                    tooltip=[alt.Tooltip("choice:N", title="Choice"), alt.Tooltip("label:N", title="Average loss if wrong")]))
    labels = alt.Chart(df).mark_text(dy=-8, fontWeight="bold", fontSize=13, color="#64748b").encode(x=x, y=y, text="label:N")   # reads on both themes
    layers = [bars, labels]
    if df["threshold"].notna().any():
        line = alt.Chart(df.head(1))
        layers.append(line.mark_rule(color="#ef4444", strokeDash=[5, 3], size=2).encode(y="threshold:Q"))
        layers.append(line.mark_text(color="#ef4444", align="left", dx=4, dy=-7, fontWeight="bold")
                      .encode(y="threshold:Q", x=alt.value(0), text=alt.value("Most we accept to lose")))
    return alt.layer(*layers, title=title).properties(height=260)


def bayes_time_frame(series: list[dict], metric, threshold: float | None) -> pd.DataFrame:
    """By day (cumulative from launch): chance to win (percent), the expected difference and its 95% credible interval (named
    difference, low and high, so the difference chart can draw it), and both risks with the threshold, in percentage points for rates."""
    df = pd.DataFrame(bayes.daily_posterior(metric, [s for s in series if s["item_id"] == metric.metric_id], threshold))
    if df.empty:
        return df
    k = 100 if metric.type == "binary" else 1
    df["day"] = pd.to_datetime(df["day"])
    df["chance_to_win"] *= 100
    df["difference"], df["low"], df["high"] = df["difference"] * k, df["ci_low"] * k, df["ci_high"] * k
    for col in ("risk_variant", "risk_control", "threshold"):
        df[col] = df[col] * k
    return df


def risk_time_chart(df: pd.DataFrame, binary: bool, title: str) -> alt.LayerChart:
    """The risk of each choice by day against the threshold (red dashed line): a choice is safe from the day its line stays under it."""
    long = df.melt(id_vars=["day"], value_vars=["risk_variant", "risk_control"], var_name="choice", value_name="risk")
    long["choice"] = long["choice"].map({"risk_variant": "Ship variant", "risk_control": "Keep control"})
    lines = (alt.Chart(long).mark_line(point=alt.OverlayMarkDef(size=50))
             .encode(x=alt.X("day:T", title="Day", axis=alt.Axis(format="%b %d")),
                     y=alt.Y("risk:Q", title=f"Average loss if wrong ({'pp' if binary else 'units'})", scale=alt.Scale(zero=True)),
                     color=alt.Color("choice:N", scale=alt.Scale(domain=["Ship variant", "Keep control"], range=[LINE_BLUE, "#94a3b8"]),
                                     title=None, legend=alt.Legend(orient="top")),
                     tooltip=[alt.Tooltip("day:T", title="Day", format="%b %d"), alt.Tooltip("choice:N", title="Choice"),
                              alt.Tooltip("risk:Q", title="Average loss if wrong", format=".3f")]))
    layers = [lines]
    if df["threshold"].notna().any():
        line = alt.Chart(df.head(1))
        layers.append(line.mark_rule(color="#ef4444", strokeDash=[5, 3], size=2).encode(y="threshold:Q"))
        layers.append(line.mark_text(color="#ef4444", align="left", dx=4, dy=-7, fontWeight="bold")
                      .encode(y="threshold:Q", x=alt.value(0), text=alt.value("Most we accept to lose")))
    return alt.layer(*layers, title=title).properties(height=260)


def chance_chart(df: pd.DataFrame, title: str) -> alt.LayerChart:
    """Chance the variant beats control by day, with a grey line at 50% (a coin flip)."""
    half = alt.Chart(pd.DataFrame({"half": [50]})).mark_rule(color=ZERO_GREY, strokeDash=[4, 3]).encode(y="half:Q")
    line = (alt.Chart(df).mark_line(color=LINE_BLUE, point=alt.OverlayMarkDef(size=70, color=LINE_BLUE))
            .encode(x=alt.X("day:T", title="Day", axis=alt.Axis(format="%b %d")),
                    y=alt.Y("chance_to_win:Q", title="Chance variant wins (%)", scale=alt.Scale(domain=[0, 100])),
                    tooltip=[alt.Tooltip("day:T", title="Day", format="%b %d"), alt.Tooltip("chance_to_win:Q", title="Chance (%)", format=".1f")]))
    return alt.layer(half, line, title=title).properties(height=260)


# ---- the reason under the verdict ------------------------------------------------------------------------------------------
def _p(p: float) -> str:
    return "p < 0.001" if p < 0.001 else f"p = {p:.3f}"


def frequentist_reason(row: dict, final: bool, end) -> str:
    """Why the primary metric got its frequentist verdict, in one plain line."""
    if not final:
        return f"The verdict comes after the last day ({end}); until then the numbers can still change by chance."
    p, alpha = row.get("p_value"), (row.get("params") or {}).get("alpha", 0.05)
    if p is None:
        return ""
    verdict = row.get("verdict")
    if verdict == "Significant improvement":
        return f"The lift is real, not chance ({_p(p)}, below your alpha of {alpha:g})."
    if verdict == "Significant decline":
        return f"The variant is really worse, not by chance ({_p(p)}, below your alpha of {alpha:g})."
    if p < alpha:   # a one-sided test that saw a clear move in the wrong direction
        return "The variant moved the wrong way, and this one-sided test only counts improvements."
    return f"The difference could be chance ({_p(p)}, above your alpha of {alpha:g})."


def bayes_reason(outcome, binary: bool, threshold: float | None, min_days: int) -> str:
    """Why the primary metric got its Bayesian verdict, in one plain line."""
    if outcome is None:
        return "Refresh to see the Bayesian numbers."
    v = outcome.verdict or ""
    if v.startswith(bayes.VERDICT_WAIT):
        return f"Too early: the verdict comes after {min_days} days of data."
    if threshold is None:
        return ""
    rv, rc, t = _loss(binary, outcome.risk_variant), _loss(binary, outcome.risk_control), _loss(binary, threshold)
    return {bayes.VERDICT_VARIANT: f"Shipping the variant risks only {rv}, under your {t} limit, while keeping control risks {rc}.",
            bayes.VERDICT_CONTROL: f"Keeping control risks only {rc}, under your {t} limit, while shipping the variant risks {rv}.",
            bayes.VERDICT_EITHER: f"Both choices risk less than your {t} limit ({rv} and {rc}), so either is fine.",
            bayes.VERDICT_MORE: f"Both choices still risk more than your {t} limit (ship {rv}, keep {rc}); more data may settle it.",
            bayes.VERDICT_DONE: f"The test has ended and both choices still risk more than your {t} limit (ship {rv}, keep {rc})."}.get(v, "")


# ---- the placebo check card -------------------------------------------------------------------------------------------------
def placebo_summary(p: dict) -> tuple[str, str, str]:
    """(chip text, chip tone, text) for the placebo card, from the dict saved with the primary metric's result row."""
    reps, n, rate = p["reps"], p["n"], p["rate"]
    how = f"In {reps} random splits of your control users into two identical groups of {n:,}"
    if p["method"] == "bayesian":
        if p["ok"]:
            return ("Placebo passed", "green",
                    f"**On identical groups, the rule rarely picks a winner.** {how}, it named a safer arm {rate:.1%} of the time.")
        return ("High false calls", "yellow",
                f"**The rule often picks a winner when nothing differs.** {how}, it named a safer arm {rate:.1%} of the time. Your risk threshold is "
                f"loose compared with the noise in this metric; a smaller threshold makes the rule more cautious.")
    seen = f"{how}, the test found a difference {rate:.1%} of the time (expected about {p['expected']:.0%}, normal range {p['low']:.1%} to {p['high']:.1%})."
    if p["ok"]:
        return "Placebo passed", "green", f"**The test behaves correctly on your data.** {seen}"
    return ("Placebo failed", "red",
            f"**The test finds differences that are not there.** {seen} Do not trust the p-values until this is explained, for example "
            f"duplicated users or a very skewed metric.")


def balance_status_card(srm) -> str:
    """The balance check as a quiet card: Balanced or Not balanced. What the split was is in the hover tip."""
    n0, n1 = srm.n_control, srm.n_variant
    if srm.balanced:
        return check_card("Balance check", chip("Balanced", "green"), "The test is balanced. Users are split evenly between control and variant.")
    share = n0 / (n0 + n1)
    return check_card("Balance check", chip("Not balanced", "red"),
                      f"The split looks wrong. Expected 50 / 50, saw {share * 100:.1f} / {100 - share * 100:.1f} ({n0:,} control, {n1:,} variant). "
                      "Check how users were assigned before trusting these numbers.")


def placebo_status_card(check: dict | None) -> str:
    """The placebo A/A check as a quiet card: Passed or Not passed (a Bayesian rule that picks winners too often is a caution, so yellow).
    The numbers behind it are in the hover tip. Results saved before the check existed ask for a refresh."""
    if not check:
        return check_card("Placebo A/A check", chip("Refresh needed", "grey"), "The placebo check appears after the next Refresh.")
    _, tone, text = placebo_summary(check)
    if check["ok"]:
        return check_card("Placebo A/A check", chip("Passed", "green"), text.replace("**", ""))
    return check_card("Placebo A/A check", chip("Not passed", "yellow" if check["method"] == "bayesian" else "red"), text.replace("**", ""))
