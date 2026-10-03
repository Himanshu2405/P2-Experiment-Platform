"""Sample ratio mismatch (SRM): did the users land in the two arms in the planned proportion? A chi-square test on the arm counts.
A very small p-value means the split is broken (bad bucketing, lost data) and the results should not be trusted."""
from dataclasses import dataclass

from scipy.stats import chi2

SRM_ALPHA = 0.001      # the usual strict threshold: a healthy test fails it once in a thousand


@dataclass(frozen=True)
class SrmResult:
    balanced: bool
    p_value: float
    n_control: int
    n_variant: int
    expected_control: float


def srm_check(n_control: int, n_variant: int, expected_control: float = 0.5, alpha: float = SRM_ALPHA) -> SrmResult:
    """Chi-square goodness of fit of the arm counts against the planned split (50/50 unless said otherwise)."""
    if not 0 < expected_control < 1:
        raise ValueError("expected_control must be between 0 and 1")
    n = n_control + n_variant
    if n <= 0:
        raise ValueError("no users to check")
    e_c, e_v = n * expected_control, n * (1 - expected_control)
    stat = (n_control - e_c) ** 2 / e_c + (n_variant - e_v) ** 2 / e_v
    p = float(chi2.sf(stat, 1))
    return SrmResult(p >= alpha, p, int(n_control), int(n_variant), expected_control)
