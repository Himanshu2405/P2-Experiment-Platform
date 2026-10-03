"""Tables and charts for one experiment's numbers. Plain functions that turn service rows into frames and charts, so they can
be tested without a browser."""
import altair as alt
import pandas as pd

from ui import BAD, GOOD, GREY, NEUTRAL

ICON = {"Significant improvement": "✅", "Significant decline": "❌", "No significant difference": "➖", "Not significant": "➖",
        "Passed": "✅", "Inconclusive": "⚠️", "Failed": "❌"}
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
            word = v.split(" ", 1)[1] if v[:1] in "✅❌➖⚠" and " " in v else v
            tint = (GOOD if word in ("Significant improvement", "Passed") else BAD if word in ("Significant decline", "Failed")
                    else GREY if word in ("No significant difference", "Not significant") else NEUTRAL if word else "")
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


def diff_chart(df: pd.DataFrame, binary: bool, title: str) -> alt.LayerChart:
    """The difference line, a shaded range around it when there is one, and a grey line at no difference: where the shade
    stays on one side of the grey line, the two arms really differ."""
    zero = alt.Chart(pd.DataFrame({"zero": [0]})).mark_rule(color=ZERO_GREY, strokeDash=[4, 3]).encode(y="zero:Q")
    x = alt.X("day:T", title="Day", axis=alt.Axis(format="%b %d"))
    y = alt.Y("difference:Q", title=f"Variant minus control ({'pp' if binary else 'units'})", scale=alt.Scale(zero=True))
    tip = [alt.Tooltip("day:T", title="Day", format="%b %d"), alt.Tooltip("difference:Q", title="Difference", format=".3f"),
           alt.Tooltip("n_control:Q", title="Control users", format=","), alt.Tooltip("n_variant:Q", title="Variant users", format=",")]
    layers = [zero]
    if "low" in df:
        tip += [alt.Tooltip("low:Q", title="Range low", format=".3f"), alt.Tooltip("high:Q", title="Range high", format=".3f")]
        layers.append(alt.Chart(df).mark_area(color=BAND_BLUE, opacity=0.18).encode(x=x, y="low:Q", y2="high:Q"))
    layers.append(alt.Chart(df).mark_line(color=LINE_BLUE, point=alt.OverlayMarkDef(size=70, color=LINE_BLUE)).encode(x=x, y=y, tooltip=tip))
    return alt.layer(*layers, title=title).properties(height=260)
