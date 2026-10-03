"""Play the company's systems: land the shared user universe and experiment data in the raw layer.

Nothing here touches the app database. Ground truth is returned to the caller.
"""
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from p2.simulator.mess import add_duplicates, add_ingested_at, add_mess
from p2.simulator.models import PRODUCTS, load_params
from p2.simulator.universe import (RETURNING_MAX_SIGNUP, Universe, generate_universe, generate_user_attrs, plan_as_of)
from p2.warehouse.bq import Warehouse

ID_COLUMN = {"checkout_events": "event_id", "checkout_orders": "order_id", "email_sends": "send_id",
             "email_events": "event_id", "onboarding_events": "event_id", "payments": "payment_id"}
LANDED = pd.Timestamp("2026-01-01")  # when the universe "landed"; any fixed time works
EXPERIMENT_START = date(2026, 1, 1)


@dataclass
class SimResult:
    cohort: pd.DataFrame      # user_id, arm, anchor_ts, entry attributes
    outcomes: pd.DataFrame    # clean user-level outcome per candidate metric (no mess), with arm
    truth: dict[str, dict[str, float]]  # metric -> control, variant, relative_lift (exact, for this cohort)
    assignments: pd.DataFrame


# ------------------------------------------------------------------ context read back from the warehouse
@dataclass
class Context:
    users: pd.DataFrame
    plan_history: pd.DataFrame
    first_order: pd.Series  # user_id -> timestamp of the user's first completed order


def load_context(wh: Warehouse) -> Context:
    users = wh.query_df(f"SELECT * FROM `{wh.staging}.stg_users`")
    plans = wh.query_df(f"SELECT user_id, effective_date, plan_tier, mrr FROM `{wh.staging}.stg_plan_history`")
    first = wh.query_df(
        f"SELECT user_id, MIN(order_ts) AS first_order_ts FROM `{wh.staging}.stg_checkout_orders` "
        f"WHERE status = 'completed' GROUP BY user_id")
    if first.empty:
        first_order = pd.Series(dtype="datetime64[ns]", name="first_order_ts", index=pd.Index([], name="user_id"))
    else:
        first["first_order_ts"] = first["first_order_ts"].dt.tz_localize(None)
        first_order = first.set_index("user_id")["first_order_ts"]
    return Context(users, plans, first_order)


def build_cohort(ctx: Context, user_ids: pd.Series, anchors: pd.Series) -> pd.DataFrame:
    """Entry attributes as of each user's anchor: plan in force, prior orders, tenure, channel."""
    c = pd.DataFrame({"user_id": user_ids.to_numpy(), "anchor_ts": pd.to_datetime(anchors).to_numpy()})
    u = ctx.users.set_index("user_id")
    c["plan_tier"] = plan_as_of(ctx.plan_history, c["user_id"], c["anchor_ts"].dt.normalize())
    first = c["user_id"].map(ctx.first_order)
    c["returning"] = (first < c["anchor_ts"]).fillna(False).to_numpy()
    c["tenure_days"] = (c["anchor_ts"].dt.normalize() - pd.to_datetime(c["user_id"].map(u["signup_date"]))).dt.days.to_numpy()
    c["acquisition_channel"] = c["user_id"].map(u["acquisition_channel"]).to_numpy()
    return c


# ------------------------------------------------------------------ landing helpers
def _land_activity(wh: Warehouse, tables: dict[str, pd.DataFrame], prefix: str) -> None:
    for name, df in tables.items():
        col = ID_COLUMN[name]
        wh.replace_rows(name, df, f"STARTS_WITH({col}, @p)", {"p": prefix})


def cut_after(tables: dict[str, pd.DataFrame], sources, until: date | None) -> dict[str, pd.DataFrame]:
    """Drop activity stamped after the end of day `until`, so the landed data stops on that day like a log read on that day."""
    if until is None:
        return tables
    limit = pd.Timestamp(until) + pd.Timedelta(days=1)
    out = {}
    for name, df in tables.items():
        col = sources.get(name).time_column
        if col in df.columns and pd.api.types.is_datetime64_any_dtype(df[col]):
            stamps = pd.to_datetime(df[col])
            if stamps.dt.tz is not None:
                stamps = stamps.dt.tz_convert(None)
            df = df[stamps < limit]
        out[name] = df
    return out


def _anchor_times(n: int, first_day: date, days: int, rng: np.random.Generator) -> pd.Series:
    d = pd.Timestamp(first_day) + pd.to_timedelta(rng.integers(0, days, n), unit="D")
    return d + pd.to_timedelta(rng.integers(0, 86400, n), unit="s")


def _with_ingested(df: pd.DataFrame, at: pd.Timestamp = LANDED) -> pd.DataFrame:
    out = df.copy()
    out["ingested_at"] = at
    return out


# ------------------------------------------------------------------ universe
def _prior_orders(users: pd.DataFrame, rng: np.random.Generator, prefix: str) -> pd.DataFrame:
    """Earlier orders for about half of the users who signed up early: they are returning buyers later."""
    p = load_params("checkout")
    early = users[pd.to_datetime(users.signup_date) <= pd.Timestamp(RETURNING_MAX_SIGNUP)]
    early = early[rng.random(len(early)) < 0.5]
    k = 1 + (rng.random(len(early)) < 0.3)
    idx = np.repeat(np.arange(len(early)), k)
    e = early.iloc[idx].reset_index(drop=True)
    ts = pd.to_datetime(e.signup_date) + pd.to_timedelta(rng.uniform(86400, 45 * 86400, len(e)), unit="s")
    mu = np.log(p.mean_order_value) - p.order_value_sigma**2 / 2
    return pd.DataFrame({"order_id": [f"{prefix}o{i}" for i in range(len(e))], "user_id": e.user_id, "order_ts": ts,
                         "order_value": rng.lognormal(mu, p.order_value_sigma, len(e)), "item_count": 1, "discount_value": 0.0,
                         "status": "completed", "refund_ts": pd.NaT, "refund_value": np.nan, "updated_at": ts})


def seed_universe(wh: Warehouse, n_users: int = 400_000, seed: int = 0) -> Universe:
    """Land users, plan history, legacy payments, and the prior orders that make some users returning buyers."""
    prefix = "uni-"
    uni = generate_universe(n_users, seed, prefix)
    rng = np.random.default_rng(seed + 1)
    wh.replace_rows("users", _with_ingested(uni.users), "STARTS_WITH(user_id, @p)", {"p": prefix})
    wh.replace_rows("plan_history", _with_ingested(uni.plan_history), "STARTS_WITH(user_id, @p)", {"p": prefix})
    wh.replace_rows("payments", add_duplicates(add_ingested_at(uni.payments, "payments"), rng),
                    "STARTS_WITH(payment_id, @p)", {"p": prefix})
    prior = add_duplicates(add_ingested_at(_prior_orders(uni.users, rng, prefix), "checkout_orders"), rng)
    wh.replace_rows("checkout_orders", prior, "STARTS_WITH(order_id, @p)", {"p": prefix})
    return uni


# ------------------------------------------------------------------ experiments
def simulate_experiment(wh: Warehouse, experiment_id: str, product: str, n_users: int, days: int,
                        multipliers: dict[str, float], split_control: float = 0.5, seed: int = 0,
                        start: date = EXPERIMENT_START, ctx: Context | None = None,
                        extra_products: tuple[str, ...] = (), until: date | None = None) -> SimResult:
    """Land assignments and product activity for one experiment, with an exact planted effect.

    `extra_products` also lands activity for the other products whose metrics the experiment uses, for the same users and arms,
    with no planted effect there (so those metrics read as an A/A comparison, like guardrails that nothing should move).
    `until` stops the landed activity at the end of that day (the way a log read on that day would end).

    Checkout and Email draw users from the universe. Onboarding experiments create new signups, because
    that is who onboarding tests target.
    """
    if product not in PRODUCTS:
        raise ValueError(f"unknown product: {product}")
    if n_users < 2 or days < 1 or not 0 < split_control < 1:
        raise ValueError("need n_users >= 2, days >= 1, split_control in (0, 1)")
    rng = np.random.default_rng(seed)
    base = load_params(product)
    variant = base.with_multipliers(multipliers)
    prefix = f"{experiment_id}-"

    if product == "onboarding":
        dates = pd.Timestamp(start) + pd.to_timedelta(rng.integers(0, days, n_users), unit="D")
        ids = [f"{prefix}u{i}" for i in range(n_users)]
        new_users = generate_user_attrs(n_users, dates.to_numpy(), rng, ids)
        anchors = dates + pd.to_timedelta(rng.integers(0, 86400, n_users), unit="s")
        cohort = pd.DataFrame({"user_id": ids, "anchor_ts": anchors.to_numpy(), "plan_tier": "free", "returning": False,
                               "tenure_days": 0, "acquisition_channel": new_users["acquisition_channel"].to_numpy()})
    else:
        ctx = ctx or load_context(wh)
        pool = ctx.users[pd.to_datetime(ctx.users.signup_date) < pd.Timestamp(start)].user_id
        if n_users > len(pool):
            raise ValueError(f"only {len(pool):,} eligible users exist; asked for {n_users:,}")
        ids = pool.sample(n_users, random_state=seed).reset_index(drop=True)
        cohort = build_cohort(ctx, ids, _anchor_times(n_users, start, days, rng))

    control_mask = rng.random(n_users) < split_control
    if control_mask.all() or not control_mask.any():
        raise ValueError("assignment left an empty arm; increase n_users")
    cohort["arm"] = np.where(control_mask, "control", "variant")
    gen = PRODUCTS[product][2]
    parts, outcomes, changes = [], [], []
    for arm, params in (("control", base), ("variant", variant)):
        sub = cohort[cohort.arm == arm].reset_index(drop=True)
        pfx = f"{prefix}{arm[0]}-"
        act = gen(params, sub, rng, pfx)
        parts.append(add_mess(act.tables, product, sub, rng, pfx, pre_anchor=True))
        outcomes.append(act.outcomes.assign(arm=arm))
        changes.append(act.plan_changes)
    tables = {name: pd.concat([p[name] for p in parts], ignore_index=True) for name in parts[0]}
    tables = cut_after(tables, wh.sources, until)

    assign = pd.DataFrame({"experiment_id": experiment_id, "user_id": cohort.user_id, "arm": cohort.arm,
                           "assigned_at": cohort.anchor_ts})
    assign = add_duplicates(add_ingested_at(assign, "exp_assignments"), rng, frac=0.01)
    wh.replace_rows("exp_assignments", assign, "experiment_id = @e", {"e": experiment_id})
    if product == "onboarding":
        wh.execute(f"DELETE FROM `{wh.raw}.plan_history` WHERE STARTS_WITH(user_id, @p)", {"p": prefix})
        wh.replace_rows("users", _with_ingested(new_users, pd.Timestamp(start)), "STARTS_WITH(user_id, @p)", {"p": prefix})
        free = pd.DataFrame({"user_id": new_users.user_id, "effective_date": new_users.signup_date, "plan_tier": "free", "mrr": 0.0})
        changed = pd.concat(changes, ignore_index=True)
        history = pd.concat([free, changed], ignore_index=True)
        history["ingested_at"] = pd.to_datetime(history["effective_date"]) + pd.Timedelta(hours=1)
        wh.replace_rows("plan_history", history, "FALSE")
    _land_activity(wh, tables, prefix)

    truth = {}
    tc = PRODUCTS[product][1](base, cohort)
    tv = PRODUCTS[product][1](variant, cohort)
    for m in tc:
        truth[m] = {"control": tc[m], "variant": tv[m], "relative_lift": tv[m] / tc[m] - 1}
    for other in dict.fromkeys(p for p in extra_products if p != product):
        if other not in PRODUCTS:
            raise ValueError(f"unknown product: {other}")
        oparams = load_params(other)
        oparts = []
        for arm in ("control", "variant"):
            sub = cohort[cohort.arm == arm].reset_index(drop=True)
            opfx = f"{prefix}{arm[0]}-"
            act = PRODUCTS[other][2](oparams, sub, rng, opfx)
            oparts.append(add_mess(act.tables, other, sub, rng, opfx, pre_anchor=True))
        _land_activity(wh, cut_after({name: pd.concat([p[name] for p in oparts], ignore_index=True) for name in oparts[0]}, wh.sources, until), prefix)
        for m, value in PRODUCTS[other][1](oparams, cohort).items():
            truth[m] = {"control": value, "variant": value, "relative_lift": 0.0}
    return SimResult(cohort, pd.concat(outcomes, ignore_index=True), truth, assign)
