"""Turn what a person types in the plan form into a validated MetricPlan, and describe it in words.

The UI shows binary rates in percent and effects in percentage points; the plan works in fractions.
"""
from p2.registry.registry import MetricDef
from p2.stats.bayes import BayesPlan
from p2.stats.plan import MetricPlan


def to_plan(metric: MetricDef, role: str, baseline: float, std: float | None, effect: float, kind: str,
            alpha: float, power: float, sidedness: str) -> MetricPlan:
    """UI units in (percent for binary baselines and effects, raw units for continuous), fractions out."""
    binary = metric.type == "binary"
    return MetricPlan(
        metric=metric, role=role,
        baseline=baseline / 100 if binary else baseline,
        std=None if binary else std,
        effect=effect / 100 if (binary or kind == "relative") else effect,
        effect_kind=kind, alpha=alpha, power=power, sidedness=sidedness,
    )


def _fmt(metric: MetricDef, value: float) -> str:
    return f"{value * 100:.3f}%" if metric.type == "binary" else f"{value:,.2f}"


def describe(plan: MetricPlan) -> str:
    """One plain sentence on what the entered effect means for this metric."""
    m = plan.metric
    up = m.good_direction == "higher"
    if plan.role == "primary":
        target = plan.baseline + plan.effect_abs if up else plan.baseline - plan.effect_abs
        return f"Looking for an improvement from {_fmt(m, plan.baseline)} to {_fmt(m, target)}."
    worse = plan.baseline + plan.effect_abs if not up else plan.baseline - plan.effect_abs
    return f"Guardrail fails if it worsens past {_fmt(m, worse)} (from {_fmt(m, plan.baseline)})."


def to_ui(metric: MetricDef, row: dict) -> dict:
    """The form values (UI units) that reproduce a saved experiment_items row; the inverse of to_plan."""
    binary = metric.type == "binary"
    return {
        "baseline": row["baseline"] * 100 if binary else row["baseline"],
        "std": row["std"],
        "kind": row["effect_kind"],
        "effect": row["effect"] * 100 if (binary or row["effect_kind"] == "relative") else row["effect"],
        "alpha": row["alpha"], "power": row["power"], "sidedness": row["sidedness"],
    }


# ---- the Bayesian method ----------------------------------------------------------------------------------------------------
def to_bayes_plan(metric: MetricDef, role: str, threshold: float | None, min_days: int | None, margin: float | None, kind: str,
                  harm_limit: float | None) -> BayesPlan:
    """UI units in, fractions out. A rate's threshold is in percentage points and an average's in percent of the control average;
    a guardrail's margin follows the margin type (percent of the control average, or the metric's own units, percentage points for rates);
    the harm limit is in percent."""
    binary = metric.type == "binary"
    if role == "primary":
        return BayesPlan(metric, "primary", threshold=None if threshold is None else threshold / 100, min_days=min_days or 0)
    return BayesPlan(metric, "guardrail", margin=None if margin is None else margin / 100 if (binary or kind == "relative") else margin,
                     margin_kind=kind, harm_limit=0.0 if harm_limit is None else harm_limit / 100)


def describe_bayes(plan: BayesPlan) -> str:
    """One plain sentence on what the entered numbers mean."""
    m = plan.metric
    if plan.role == "primary":
        unit = f"{plan.threshold * 100:.3g} percentage points" if m.type == "binary" else f"{plan.threshold * 100:.3g}% of the control average"
        return (f"Verdict after {plan.min_days} days: the variant is the safer choice when shipping it risks losing less than {unit} "
                f"and keeping control risks more.")
    margin = (f"{plan.margin * 100:.3g} percentage points" if m.type == "binary" and plan.margin_kind == "absolute"
              else f"{plan.margin * 100:.3g}%" if plan.margin_kind == "relative" else f"{plan.margin:,.3g}")
    return f"Guardrail passes when the chance it got worse by more than {margin} is below {plan.harm_limit * 100:.3g}%."


def to_bayes_ui(metric: MetricDef, row: dict) -> dict:
    """The form values (UI units) that reproduce a saved Bayesian experiment_items row; the inverse of to_bayes_plan."""
    binary = metric.type == "binary"
    if row["role"] == "primary":
        return {"threshold": row["loss_threshold"] * 100, "min_days": row["min_days"]}
    kind = row["effect_kind"]
    return {"kind": kind, "margin": row["effect"] * 100 if (binary or kind == "relative") else row["effect"],
            "harm_limit": row["harm_limit"] * 100}
