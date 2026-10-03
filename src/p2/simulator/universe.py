"""The shared user universe: users, slowly changing plan history, and legacy payments.

Every product draws its population from these users, like a company's central user tables.
"""
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

COUNTRIES = [("US", "NA", 0.45), ("CA", "NA", 0.05), ("GB", "EMEA", 0.08), ("DE", "EMEA", 0.07), ("FR", "EMEA", 0.04),
             ("NL", "EMEA", 0.09), ("IN", "APAC", 0.08), ("AU", "APAC", 0.04), ("BR", "LATAM", 0.06), ("MX", "LATAM", 0.04)]
CHANNELS = [("organic", 0.35), ("paid_search", 0.25), ("partner", 0.10), ("sales", 0.10), ("referral", 0.20)]
INDUSTRIES = [("retail", 0.30), ("media", 0.15), ("services", 0.30), ("health", 0.10), ("other", 0.15)]
PRICE = {"standard": 29.0, "premium": 99.0}
UNIVERSE_START, UNIVERSE_END = date(2025, 1, 1), date(2025, 12, 31)
LEGACY_CUTOFF = date(2025, 10, 1)  # later signups get their plan only through onboarding payments
RETURNING_MAX_SIGNUP = date(2025, 8, 1)  # returning buyers signed up early enough to have prior orders


@dataclass
class Universe:
    users: pd.DataFrame
    plan_history: pd.DataFrame
    payments: pd.DataFrame


def _choice(rng, options, n):
    names = [o[0] for o in options]
    p = np.array([o[-1] for o in options], dtype=float)
    return rng.choice(len(names), n, p=p / p.sum()), names


def generate_user_attrs(n: int, signup_dates: np.ndarray, rng: np.random.Generator, ids: list[str]) -> pd.DataFrame:
    """Attributes for n users with given signup dates. About 3% have no country or region, 5% no industry."""
    ci, _ = _choice(rng, COUNTRIES, n)
    country = np.array([c[0] for c in COUNTRIES], dtype=object)[ci]
    region = np.array([c[1] for c in COUNTRIES], dtype=object)[ci]
    chi, chn = _choice(rng, CHANNELS, n)
    ii, inn = _choice(rng, INDUSTRIES, n)
    industry = np.array(inn, dtype=object)[ii]
    miss = rng.random(n) < 0.03
    country[miss], region[miss] = None, None
    industry[rng.random(n) < 0.05] = None
    return pd.DataFrame({
        "user_id": ids, "signup_date": pd.to_datetime(signup_dates).date, "country": country, "region": region,
        "acquisition_channel": np.array(chn, dtype=object)[chi], "industry": industry,
    })


def generate_universe(n_users: int, seed: int = 0, prefix: str = "uni-") -> Universe:
    rng = np.random.default_rng(seed)
    days = (UNIVERSE_END - UNIVERSE_START).days + 1
    signup = np.datetime64(UNIVERSE_START) + rng.integers(0, days, n_users).astype("timedelta64[D]")
    users = generate_user_attrs(n_users, signup, rng, [f"{prefix}u{i}" for i in range(n_users)])

    has_plan = rng.random(n_users) >= 0.02  # 2% of users have no plan history at all (missing data)
    base = pd.DataFrame({"user_id": users.user_id[has_plan], "effective_date": users.signup_date[has_plan],
                         "plan_tier": "free", "mrr": 0.0})
    # legacy upgrades for users who signed up before the cutoff
    early = has_plan & (pd.to_datetime(users.signup_date) < pd.Timestamp(LEGACY_CUTOFF)).to_numpy()
    up = early & (rng.random(n_users) < 0.30)
    u = users[up].reset_index(drop=True)
    room = (pd.Timestamp(UNIVERSE_END) - pd.to_datetime(u.signup_date)).dt.days.to_numpy() - 7
    keep = room > 0
    u, room = u[keep].reset_index(drop=True), room[keep]
    up_date = pd.to_datetime(u.signup_date) + pd.to_timedelta(7 + (rng.random(len(u)) * room).astype(int), unit="D")
    tier = np.where(rng.random(len(u)) < 0.75, "standard", "premium")
    amount = np.vectorize(PRICE.get)(tier)
    pay_ts = up_date + pd.to_timedelta(rng.integers(0, 86400, len(u)), unit="s")
    payments = pd.DataFrame({"payment_id": [f"{prefix}pay{i}" for i in range(len(u))], "user_id": u.user_id,
                             "paid_ts": pay_ts, "amount": amount.astype(float), "plan_tier": tier})
    upgrade = pd.DataFrame({"user_id": u.user_id, "effective_date": up_date.dt.date, "plan_tier": tier, "mrr": amount.astype(float)})
    # 3% of upgraders later downgrade to free
    d = upgrade[rng.random(len(upgrade)) < 0.03].copy()
    d = d[pd.to_datetime(d.effective_date) < pd.Timestamp(UNIVERSE_END)]
    gap = (pd.Timestamp(UNIVERSE_END) - pd.to_datetime(d.effective_date)).dt.days.to_numpy()
    d["effective_date"] = (pd.to_datetime(d.effective_date) + pd.to_timedelta(1 + (rng.random(len(d)) * gap).astype(int).clip(0, None), unit="D")).dt.date
    d["plan_tier"], d["mrr"] = "free", 0.0
    history = pd.concat([base, upgrade, d], ignore_index=True)
    return Universe(users, history, payments)


def plan_as_of(plan_history: pd.DataFrame, user_ids: pd.Series, dates: pd.Series) -> pd.Series:
    """Plan tier in force on each date: latest row with effective_date on or before it, else 'Unknown'."""
    left = pd.DataFrame({"user_id": user_ids.to_numpy(), "d": pd.to_datetime(dates).astype("datetime64[ns]").to_numpy(), "pos": np.arange(len(user_ids))})
    right = plan_history.assign(eff=pd.to_datetime(plan_history.effective_date).astype("datetime64[ns]"))[["user_id", "eff", "plan_tier"]]
    m = pd.merge_asof(left.sort_values("d"), right.sort_values("eff"), left_on="d", right_on="eff", by="user_id", direction="backward")
    out = m.sort_values("pos")["plan_tier"].fillna("Unknown")
    return out.reset_index(drop=True)
