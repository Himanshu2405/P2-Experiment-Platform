"""Metric definitions as the power calculation and design checks see them.

The catalog (app state) is the source of truth; `Registry` is the in-memory view built from its Certified and
Deprecated metric versions. A metric is one value per user over the attribution window; the pipeline
computes it from the metric's user-day SQL.
"""
from typing import Literal

from pydantic import BaseModel, Field

class MetricDef(BaseModel, frozen=True):
    metric_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    version: int = Field(ge=1)
    product_id: Literal["checkout", "email", "onboarding"]
    display_name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    type: Literal["binary", "continuous"]
    window_aggregation: Literal["sum", "max"]  # how a user's daily values combine over the window
    good_direction: Literal["higher", "lower"]
    status: Literal["Certified", "Deprecated"] = "Certified"


class Registry:
    def __init__(self, metrics: list[MetricDef]):
        seen = set()
        for m in metrics:
            key = (m.metric_id, m.version)
            if key in seen:
                raise ValueError(f"duplicate metric {m.metric_id} version {m.version}")
            seen.add(key)
        self._metrics = {(m.metric_id, m.version): m for m in metrics}

    def ids(self) -> list[str]:
        """Metrics that have at least one Certified version (the ones new experiments may pick)."""
        return sorted({m.metric_id for m in self._metrics.values() if m.status == "Certified"})

    def get(self, metric_id: str, version: int | None = None) -> MetricDef:
        """A pinned version (any status), or the latest Certified version when version is None."""
        versions = {v: m for (mid, v), m in self._metrics.items() if mid == metric_id}
        if not versions:
            raise KeyError(f"unknown metric: {metric_id}")
        if version is not None:
            try:
                return versions[version]
            except KeyError:
                raise KeyError(f"metric {metric_id} has no version {version}") from None
        certified = [v for v, m in versions.items() if m.status == "Certified"]
        if not certified:
            raise KeyError(f"metric {metric_id} has no certified version")
        return versions[max(certified)]

    def all_metrics(self) -> list["MetricDef"]:
        """Latest Certified version of every metric, across all products; feeds the pickers in the plan form."""
        return [self.get(i) for i in self.ids()]

    def validate_selection(self, primary: str, guardrails: list[str], secondary: list[str]) -> None:
        """Check an experiment's metric picks: every metric exists and none is picked in two roles."""
        ids = [primary, *guardrails, *secondary]
        if len(set(ids)) != len(ids):
            raise ValueError("a metric cannot appear more than once in an experiment")
        for mid in ids:
            self.get(mid)
