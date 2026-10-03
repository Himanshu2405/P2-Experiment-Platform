"""What a data scientist entered for a primary or guardrail metric. The tool records and follows these numbers;
it does not compute or second-guess sample sizes or baselines."""
from dataclasses import dataclass
from typing import Literal

from p2.registry.registry import MetricDef

EffectKind = Literal["relative", "absolute"]
Sidedness = Literal["one-sided", "two-sided"]


@dataclass(frozen=True)
class MetricPlan:
    metric: MetricDef
    role: Literal["primary", "guardrail"]
    baseline: float          # expected control value, in the metric's own units (a fraction for binary metrics)
    effect: float            # MDE for a primary metric, maximum acceptable degradation for a guardrail
    effect_kind: EffectKind
    alpha: float
    power: float             # recorded for reference; analysis reports the power actually achieved
    sidedness: Sidedness
    std: float | None = None  # continuous metrics only

    def __post_init__(self):
        m = self.metric
        if not 0 < self.alpha < 0.5:
            raise ValueError("alpha must be between 0 and 0.5")
        if not 0.5 <= self.power < 1:
            raise ValueError("power must be at least 0.5 and below 1")
        if self.effect <= 0:
            raise ValueError("the effect (MDE or margin) must be positive")
        if self.role == "guardrail" and self.sidedness != "one-sided":
            raise ValueError("guardrails use a one-sided non-inferiority test")
        if m.type == "binary":
            if not 0 < self.baseline < 1:
                raise ValueError("a binary baseline must be between 0 and 1 (for example 3.5% is 0.035)")
        else:
            if self.baseline <= 0:
                raise ValueError("the baseline must be positive")
            if self.std is None or self.std <= 0:
                raise ValueError("a continuous metric needs a positive standard deviation")

    @property
    def effect_abs(self) -> float:
        """The effect in the metric's own units."""
        return self.baseline * self.effect if self.effect_kind == "relative" else self.effect
