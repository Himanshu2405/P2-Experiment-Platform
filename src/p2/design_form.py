"""Turn what a person types in the plan form into a validated MetricPlan, and describe it in words.

The UI shows binary rates in percent and effects in percentage points; the plan works in fractions.
"""
from p2.registry.registry import MetricDef
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
