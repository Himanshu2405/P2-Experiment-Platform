"""Real-world mess planted in the landed tables. The staging views and the pipeline must handle all of it,
and the true effects stay exact only if they do.

Planted, per product table set:
- duplicate rows (about 2%), delivered again a few minutes later with the same key
- superseded order versions: a refunded or cancelled order first landed as a plain completed order, and was
  updated later (the latest version must win)
- activity after the attribution window closes (about 3% of users)
- activity before the user's anchor (about 5% of users; experiments only, because for a historic cohort the
  first visit defines the anchor)
"""
import numpy as np
import pandas as pd

DAY = 86400.0
TIME_COL = {
    "checkout_events": "event_ts", "checkout_orders": "updated_at", "email_sends": "sent_ts",
    "email_events": "event_ts", "onboarding_events": "event_ts", "payments": "paid_ts",
    "exp_assignments": "assigned_at",
}
DUP_FRAC = 0.02
POST_FRAC = 0.03
PRE_FRAC = 0.05


def _pick(n: int, frac: float, rng: np.random.Generator) -> np.ndarray:
    return np.flatnonzero(rng.random(n) < frac)


def _shift(anchor: pd.Series, idx: np.ndarray, lo_days: float, hi_days: float, rng) -> pd.Series:
    off = rng.uniform(lo_days * DAY, hi_days * DAY, len(idx))
    return anchor.iloc[idx].reset_index(drop=True) + pd.to_timedelta(off, unit="s")


def add_ingested_at(df: pd.DataFrame, table: str, lag_hours: float = 1.0) -> pd.DataFrame:
    out = df.copy()
    out["ingested_at"] = out[TIME_COL[table]] + pd.Timedelta(hours=lag_hours)
    return out


def add_duplicates(df: pd.DataFrame, rng: np.random.Generator, frac: float = DUP_FRAC) -> pd.DataFrame:
    """Append exact copies that landed ten minutes later."""
    copies = df.iloc[_pick(len(df), frac, rng)].copy()
    copies["ingested_at"] = copies["ingested_at"] + pd.Timedelta(minutes=10)
    return pd.concat([df, copies], ignore_index=True)


def _next_ids(prefix: str, letter: str, n: int) -> list[str]:
    return [f"{prefix}{letter}{i}" for i in range(n)]


def add_mess(tables: dict[str, pd.DataFrame], product: str, cohort: pd.DataFrame, rng: np.random.Generator,
             prefix: str, pre_anchor: bool) -> dict[str, pd.DataFrame]:
    """Return the product tables with ingested_at and the planted mess added."""
    c = cohort.reset_index(drop=True)
    uid, anchor = c["user_id"].to_numpy(), c["anchor_ts"]
    out = {k: v.copy() for k, v in tables.items()}
    post = _pick(len(c), POST_FRAC, rng)
    pre = _pick(len(c), PRE_FRAC, rng) if pre_anchor else np.array([], dtype=int)

    if product == "checkout":
        ev = []
        for idx, lo, hi, name in ((post, 8, 15, "visit"), (post, 8, 15, "add_to_cart"), (pre, -10, -1, "visit")):
            ev.append(pd.DataFrame({"user_id": uid[idx], "event_type": name, "event_ts": _shift(anchor, idx, lo, hi, rng)}))
        extra = pd.concat(ev, ignore_index=True)
        extra.insert(0, "event_id", _next_ids(prefix, "x", len(extra)))
        out["checkout_events"] = pd.concat([out["checkout_events"], extra], ignore_index=True)
        ts = _shift(anchor, post, 8, 20, rng)
        late = pd.DataFrame({"order_id": _next_ids(prefix, "ox", len(post)), "user_id": uid[post], "order_ts": ts,
                             "order_value": rng.uniform(20, 200, len(post)), "item_count": 1, "discount_value": 0.0,
                             "status": "completed", "refund_ts": pd.NaT, "refund_value": np.nan, "updated_at": ts})
        orders = pd.concat([out["checkout_orders"], late], ignore_index=True)
        orders = add_ingested_at(orders, "checkout_orders")
        changed = orders[(orders.status == "cancelled") | orders.refund_ts.notna()].copy()
        changed["status"], changed["refund_ts"], changed["refund_value"] = "completed", pd.NaT, np.nan
        changed["updated_at"] = changed["order_ts"]
        changed["ingested_at"] = changed["order_ts"] + pd.Timedelta(hours=1)
        out["checkout_orders"] = orders
        out["checkout_orders"] = pd.concat([orders, changed], ignore_index=True)
    elif product == "email":
        s_post = pd.DataFrame({"user_id": uid[post], "campaign_id": "camp-late", "campaign_type": "promo",
                               "sent_ts": _shift(anchor, post, 8, 14, rng)})
        s_pre = pd.DataFrame({"user_id": uid[pre], "campaign_id": "camp-early", "campaign_type": "newsletter",
                              "sent_ts": _shift(anchor, pre, -30, -10, rng)})
        extra_sends = pd.concat([s_post, s_pre], ignore_index=True)
        extra_sends.insert(0, "send_id", _next_ids(prefix, "sx", len(extra_sends)))
        opens = extra_sends.rename(columns={"sent_ts": "event_ts"})[["send_id", "user_id", "event_ts"]].copy()
        opens["event_ts"] = opens["event_ts"] + pd.Timedelta(minutes=30)
        opens.insert(0, "event_id", _next_ids(prefix, "ex", len(opens)))
        opens["event_type"] = "open"
        out["email_sends"] = pd.concat([out["email_sends"], extra_sends], ignore_index=True)
        out["email_events"] = pd.concat([out["email_events"], opens[["event_id", "send_id", "user_id", "event_type", "event_ts"]]], ignore_index=True)
    else:  # onboarding
        done = set(out["onboarding_events"].loc[out["onboarding_events"].step == "activated", "user_id"])
        paid = set(out["payments"]["user_id"])
        late_act = np.array([i for i in post if uid[i] not in done], dtype=int)
        late_pay = np.array([i for i in _pick(len(c), 0.02, rng) if uid[i] not in paid], dtype=int)
        extra = pd.DataFrame({"user_id": uid[late_act], "step": "activated", "event_ts": _shift(anchor, late_act, 8, 20, rng)})
        extra.insert(0, "event_id", _next_ids(prefix, "x", len(extra)))
        out["onboarding_events"] = pd.concat([out["onboarding_events"], extra], ignore_index=True)
        late = pd.DataFrame({"payment_id": _next_ids(prefix, "px", len(late_pay)), "user_id": uid[late_pay],
                             "paid_ts": _shift(anchor, late_pay, 8, 20, rng), "amount": 29.0, "plan_tier": "standard"})
        out["payments"] = pd.concat([out["payments"], late], ignore_index=True)

    for name, df in list(out.items()):
        if "ingested_at" not in df.columns:
            df = add_ingested_at(df, name)
        out[name] = add_duplicates(df, rng)
    return out
