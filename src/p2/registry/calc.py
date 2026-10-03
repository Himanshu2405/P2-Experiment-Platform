"""Metric calculation. The only place a metric value is computed, at design time and analysis time."""
from dataclasses import dataclass
from math import sqrt

import pandas as pd

from p2.registry.registry import MetricDef


@dataclass(frozen=True)
class MetricSummary:
    """Sufficient statistics for one group of users on one metric."""
    n: int
    mean: float
    std: float  # sample std, ddof=1

    @property
    def se(self) -> float:
        return self.std / sqrt(self.n)


def metric_values(metric: MetricDef, df: pd.DataFrame) -> pd.Series:
    """Per-user values of a metric, validated against its definition."""
    if metric.metric_id not in df.columns:
        raise KeyError(f"column '{metric.metric_id}' missing for metric {metric.metric_id}")
    values = df[metric.metric_id]  # the pipeline's final table has one column per metric, named by metric_id
    if values.isna().any():
        raise ValueError(f"{metric.metric_id}: {int(values.isna().sum())} null values")
    if metric.type == "binary" and not values.isin([0, 1]).all():
        raise ValueError(f"{metric.metric_id}: binary metric must contain only 0 or 1")
    if metric.type == "continuous" and (values < 0).any():
        raise ValueError(f"{metric.metric_id}: negative values not allowed")
    return values.astype(float)


def compute_metric(metric: MetricDef, df: pd.DataFrame) -> MetricSummary:
    values = metric_values(metric, df)
    if len(values) < 2:
        raise ValueError(f"{metric.metric_id}: need at least 2 users, got {len(values)}")
    return MetricSummary(n=len(values), mean=float(values.mean()), std=float(values.std(ddof=1)))
