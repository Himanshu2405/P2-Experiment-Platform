"""The September 2026 demo data: a fixed set of experiments whose users are assigned across the whole month.

Every experiment id below has users assigned on every day from 1 to 30 September and activity up to the end of 30 September.
So a demo experiment can be launched on any day of the month and ended on any later day (or run on to the end of the month),
and the dates can be changed inside the month and still find data. The id says the product; the first of each pair has a planted
effect, the second has none. All three products' activity is landed for every id, so any metric can be picked
(effect on the id's own product only; the others read as no difference).
"""
from datetime import date

from p2.simulator.service import load_context, simulate_experiment
from p2.warehouse.bq import Warehouse

MONTH_START, MONTH_END = date(2026, 9, 1), date(2026, 9, 30)
USERS = 6000
# id, product, parameter with the planted effect, lift (0 means no effect)
DEMO = [("sep-checkout-1", "checkout", "p_order_given_begin", 0.20), ("sep-checkout-2", "checkout", "p_order_given_begin", 0.0),
        ("sep-email-1", "email", "p_click_given_open", 0.15), ("sep-email-2", "email", "p_click_given_open", 0.0),
        ("sep-onboarding-1", "onboarding", "p_activate_given_project", 0.20), ("sep-onboarding-2", "onboarding", "p_activate_given_project", 0.0)]


def land_september(wh: Warehouse, progress=None) -> list[str]:
    """Land the demo experiments (assignments and activity). Safe to run again: each id is replaced. Returns the ids."""
    ctx = load_context(wh)
    for i, (eid, product, param, lift) in enumerate(DEMO):
        others = tuple(p for p in ("checkout", "email", "onboarding") if p != product)
        simulate_experiment(wh, eid, product, USERS, (MONTH_END - MONTH_START).days + 1, {param: 1 + lift}, 0.5, seed=100 + i,
                            start=MONTH_START, ctx=ctx, extra_products=others, until=MONTH_END)
        if progress:
            progress(i + 1, len(DEMO), eid)
    return [d[0] for d in DEMO]
