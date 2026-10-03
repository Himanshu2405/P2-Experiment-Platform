"""Bayesian analysis from summary statistics (count, mean, variance per arm), the same inputs the frequentist tests use.

Rates use a Beta-Binomial model with a flat Beta(1, 1) prior. Averages use a Normal posterior for each arm's mean (flat prior),
which is accurate with thousands of users. The posterior of the difference is simulated with a fixed seed, so the same numbers
always give the same result. The decision rule compares the risk of shipping the variant and the risk of keeping control with a
threshold the data scientist sets.
"""
from dataclasses import dataclass, replace
from typing import Literal

import numpy as np

from p2.registry.registry import MetricDef
from p2.stats.tests import Arm

DRAWS = 100_000          # posterior draws for the numbers in the table
DAILY_DRAWS = 20_000     # fewer draws per day for the over-time chart
SEED = 20260401
CRED = 0.95
VERDICT_WAIT = "Collecting evidence"
VERDICT_VARIANT = "Variant is the safer choice"
VERDICT_CONTROL = "Control is the safer choice"
VERDICT_EITHER = "No meaningful difference"
VERDICT_MORE = "Not enough evidence yet"
VERDICT_DONE = "Inconclusive"
FAIL_ABOVE = 0.5         # a guardrail fails when the chance of harm is above this

# Defaults the form starts from: a rate threshold is an absolute number (0.05 percentage points), an average's is a share of control.
DEFAULT_RATE_THRESHOLD = 0.0005
DEFAULT_MEAN_THRESHOLD = 0.005
DEFAULT_MIN_DAYS = 7
DEFAULT_HARM_LIMIT = 0.01


@dataclass(frozen=True)
class BayesPlan:
    """What a data scientist entered for a primary or guardrail metric when choosing the Bayesian method."""
    metric: MetricDef
    role: Literal["primary", "guardrail"]
    threshold: float | None = None        # primary: the most loss accepted (absolute for a rate, a share of the control mean for an average)
    min_days: int = DEFAULT_MIN_DAYS      # primary: no verdict before this many days of data
    margin: float | None = None           # guardrail: how much worse counts as harm
    margin_kind: Literal["relative", "absolute"] = "relative"
    harm_limit: float = DEFAULT_HARM_LIMIT  # guardrail: passes when the chance of harm is below this

    def __post_init__(self):
        if self.role == "primary":
            if self.threshold is None or self.threshold <= 0:
                raise ValueError("the risk threshold must be positive")
            if not isinstance(self.min_days, int) or isinstance(self.min_days, bool) or self.min_days < 1:
                raise ValueError("the minimum days must be a whole number of at least 1")
        else:
            if self.margin is None or self.margin <= 0:
                raise ValueError("the margin (how much worse counts as harm) must be positive")
            if not 0 < self.harm_limit < FAIL_ABOVE:
                raise ValueError(f"the harm limit must be between 0 and {FAIL_ABOVE:.0%}")

    @property
    def threshold_kind(self) -> str:
        return "absolute" if self.metric.type == "binary" else "relative"

    def threshold_abs(self, control_mean: float) -> float | None:
        """The risk threshold in the metric's own units."""
        if self.threshold is None:
            return None
        return self.threshold if self.threshold_kind == "absolute" else self.threshold * abs(control_mean)

    def margin_abs(self, control_mean: float) -> float | None:
        if self.margin is None:
            return None
        return self.margin if self.margin_kind == "absolute" else self.margin * abs(control_mean)


@dataclass(frozen=True)
class BayesOutcome:
    chance_to_win: float            # chance the variant is better, in the metric's good direction
    difference: float               # posterior mean of variant minus control
    relative_lift: float | None
    ci_low: float                   # 95% credible interval of variant minus control
    ci_high: float
    risk_variant: float             # expected loss if the variant is shipped (average shortfall when it is actually worse)
    risk_control: float             # expected loss if control is kept (average gain given up when the variant is actually better)
    chance_of_harm: float | None    # guardrail only: chance the variant is worse by more than the margin
    verdict: str | None = None


def _check(c: Arm, v: Arm) -> None:
    if c.n < 2 or v.n < 2:
        raise ValueError("each arm needs at least 2 users")


def _draws(m: MetricDef, a: Arm, rng: np.random.Generator, size: int) -> np.ndarray:
    """Draws of one arm's true mean from its posterior."""
    if m.type == "binary":
        successes = min(max(round(a.n * a.mean), 0), a.n)
        return rng.beta(1 + successes, 1 + a.n - successes, size)
    return rng.normal(a.mean, np.sqrt(max(a.var, 0.0) / a.n), size)


def posterior_difference(m: MetricDef, c: Arm, v: Arm, draws: int = DRAWS, seed: int = SEED) -> np.ndarray:
    """Draws of the true difference (variant minus control)."""
    _check(c, v)
    rng = np.random.default_rng(seed)
    return _draws(m, v, rng, draws) - _draws(m, c, rng, draws)


def analyze_bayes(m: MetricDef, c: Arm, v: Arm, margin_abs: float | None = None, draws: int = DRAWS, seed: int = SEED) -> BayesOutcome:
    """The numbers of the Bayesian view for one metric. `margin_abs` (in the metric's units) adds the chance of harm for a guardrail."""
    d = posterior_difference(m, c, v, draws, seed)
    good = 1.0 if m.good_direction == "higher" else -1.0
    gd = good * d                                   # positive means the variant is better
    harm = None if margin_abs is None else float(np.mean(-gd > margin_abs))
    low, high = np.quantile(d, [(1 - CRED) / 2, 1 - (1 - CRED) / 2])
    mean_diff = float(d.mean())
    return BayesOutcome(chance_to_win=float(np.mean(gd > 0)), difference=mean_diff,
                        relative_lift=mean_diff / c.mean if c.mean else None, ci_low=float(low), ci_high=float(high),
                        risk_variant=float(np.mean(np.maximum(-gd, 0.0))), risk_control=float(np.mean(np.maximum(gd, 0.0))),
                        chance_of_harm=harm)


def primary_verdict(out: BayesOutcome, threshold_abs: float, days: int, min_days: int, ended: bool) -> str:
    """Compare the two risks with the threshold. Before `min_days` of data there is no verdict yet. After the last day, an unclear
    result is Inconclusive, because no more data is coming."""
    if days < min_days and not ended:
        return f"{VERDICT_WAIT} (day {days} of {min_days})"
    variant_ok, control_ok = out.risk_variant <= threshold_abs, out.risk_control <= threshold_abs
    if variant_ok and control_ok:
        return VERDICT_EITHER
    if variant_ok:
        return VERDICT_VARIANT
    if control_ok:
        return VERDICT_CONTROL
    return VERDICT_DONE if ended else VERDICT_MORE


def guardrail_verdict(out: BayesOutcome, harm_limit: float, days: int, min_days: int, ended: bool) -> str:
    """Passed when the chance of harm is below the limit, Failed when harm is more likely than not, otherwise Inconclusive."""
    if days < min_days and not ended:
        return f"{VERDICT_WAIT} (day {days} of {min_days})"
    if out.chance_of_harm < harm_limit:
        return "Passed"
    return "Failed" if out.chance_of_harm > FAIL_ABOVE else "Inconclusive"


def decide(m: MetricDef, plan: BayesPlan | None, c: Arm, v: Arm, days: int, min_days: int, ended: bool) -> BayesOutcome:
    """The Bayesian numbers and, for a primary or guardrail metric with a plan, the verdict."""
    if plan is not None and plan.role == "guardrail":
        out = analyze_bayes(m, c, v, margin_abs=plan.margin_abs(c.mean))
        verdict = guardrail_verdict(out, plan.harm_limit, days, min_days, ended)
    else:
        out = analyze_bayes(m, c, v)
        verdict = None if plan is None else primary_verdict(out, plan.threshold_abs(c.mean), days, min_days, ended)
    return replace(out, verdict=verdict)


def daily_posterior(m: MetricDef, rows: list[dict], threshold_abs: float | None = None) -> list[dict]:
    """Chance to win, both risks and the 95% credible interval of the difference for every day of a metric's daily series (cumulative
    from launch). `rows` are the `daily_stats` rows of one metric. Days with fewer than 2 users in an arm are skipped."""
    by_day: dict = {}
    for r in rows:
        by_day.setdefault(r["day"], {})[r["arm"]] = Arm(int(r["n_users"]), r["mean_value"], r["var_value"] or 0.0)
    out = []
    for day in sorted(by_day):
        arms = by_day[day]
        if "control" not in arms or "variant" not in arms or arms["control"].n < 2 or arms["variant"].n < 2:
            continue
        o = analyze_bayes(m, arms["control"], arms["variant"], draws=DAILY_DRAWS)
        out.append({"day": day, "chance_to_win": o.chance_to_win, "risk_variant": o.risk_variant, "risk_control": o.risk_control,
                    "difference": o.difference, "ci_low": o.ci_low, "ci_high": o.ci_high,
                    "n_control": arms["control"].n, "n_variant": arms["variant"].n,
                    "threshold": float("nan") if threshold_abs is None else threshold_abs})
    return out


# ---- from saved rows (the results page and the catalog) --------------------------------------------------------------------
def plan_from_row(metric: MetricDef, row: dict) -> BayesPlan | None:
    """The BayesPlan an `experiment_items` row holds (None for a secondary metric)."""
    if row["role"] == "primary":
        return BayesPlan(metric, "primary", threshold=row["loss_threshold"], min_days=row.get("min_days") or DEFAULT_MIN_DAYS)
    if row["role"] == "guardrail":
        return BayesPlan(metric, "guardrail", margin=row["effect"], margin_kind=row["effect_kind"] or "relative",
                         harm_limit=row.get("harm_limit") or DEFAULT_HARM_LIMIT)
    return None


def arms_of(metric: MetricDef, row: dict) -> tuple[Arm, Arm] | None:
    """The two arms of a results or segment row, or None when they cannot be rebuilt (a continuous metric whose variance was not stored,
    which is a row from before the Bayesian method existed, or an arm with fewer than 2 users)."""
    if (row["n_control"] or 0) < 2 or (row["n_variant"] or 0) < 2:
        return None
    arms = []
    for side in ("control", "variant"):
        mean, var = row[f"mean_{side}"], row.get(f"var_{side}")
        if metric.type == "binary":
            var = mean * (1 - mean)
        elif var is None:
            return None
        arms.append(Arm(int(row[f"n_{side}"]), mean, var))
    return arms[0], arms[1]


def analyze_rows(rows: list[dict], design: list[dict], registry, launch, end) -> list[BayesOutcome | None]:
    """One Bayesian outcome per results (or segment) row, in the same order; None where the arms cannot be rebuilt. `launch` and `end` are
    the experiment's first and last day. The days of data come from the row's `through_date`; a row through the last day has ended."""
    plans = {d["item_id"]: d for d in design if d["kind"] == "metric"}
    primary = next((d for d in design if d["role"] == "primary"), None)
    total_days = (end - launch).days + 1
    min_days = min((primary and primary.get("min_days")) or DEFAULT_MIN_DAYS, total_days)
    out = []
    for r in rows:
        d = plans[r["item_id"]]
        metric = registry.get(r["item_id"], d["item_version"])
        arms = arms_of(metric, r)
        if arms is None:
            out.append(None)
            continue
        through = r.get("through_date") or end          # rows from before through_date was stored are final, through the last day
        days = (through - launch).days + 1
        out.append(decide(metric, plan_from_row(metric, d), *arms, days, min_days, through >= end))
    return out
