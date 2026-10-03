"""Per-product generative models with closed-form truth.

Each model turns a cohort (users with an anchor time and entry attributes) into raw product tables, plus
the clean user-level outcomes, and can compute the exact expected value of each candidate metric for any
cohort under any parameters. Effects are multipliers on parameters, so true lifts are exact.
All generated activity falls inside the attribution window: the anchor's calendar day plus the next six
days (whole days, as the user-day metric contract defines it). The mess that lives outside the window is
added separately (see mess.py).

Cohort columns: user_id, anchor_ts (naive UTC), plan_tier, returning (bool), tenure_days, acquisition_channel.
"""
from dataclasses import dataclass, field, replace
from importlib import resources

import numpy as np
import pandas as pd
import yaml

DAY = 86400.0
WINDOW_DAYS = 7


def window_seconds(anchor: pd.Series) -> np.ndarray:
    """Seconds from each anchor to the last second of day 6 (minus a minute of safety)."""
    tod = (anchor - anchor.dt.normalize()).dt.total_seconds().to_numpy()
    return WINDOW_DAYS * DAY - tod - 60
PLAN_ORDER_MULT = {"premium": 1.15, "standard": 1.05, "free": 1.0, "Unknown": 1.0}
PRICE = {"standard": 29.0, "premium": 99.0}


@dataclass
class Activity:
    tables: dict[str, pd.DataFrame]   # raw product tables, without ingested_at
    outcomes: pd.DataFrame            # clean user-level outcome per candidate metric (user_id + metric ids)
    plan_changes: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=["user_id", "effective_date", "plan_tier", "mrr"]))


def _at(anchor: pd.Series, idx: np.ndarray, offset_s: np.ndarray) -> pd.Series:
    return anchor.iloc[idx].reset_index(drop=True) + pd.to_timedelta(offset_s, unit="s")


def _check_probs(**probs):
    for name, p in probs.items():
        if np.any(np.asarray(p) < 0) or np.any(np.asarray(p) > 1):
            raise ValueError(f"{name} would leave [0, 1]; choose a smaller effect")


def _apply(params, multipliers):
    unknown = set(multipliers) - set(params.__dataclass_fields__)
    if unknown:
        raise ValueError(f"unknown effect parameters: {sorted(unknown)}")
    return replace(params, **{k: getattr(params, k) * v for k, v in multipliers.items()})


# ---------------------------------------------------------------- Checkout
@dataclass(frozen=True)
class CheckoutParams:
    """Order value, orders per buyer and refund rate were calibrated once from a public dataset (2025); the
    funnel rates and cancel rate are round numbers."""
    p_add_to_cart: float
    p_begin_given_add: float
    p_order_given_begin: float
    extra_orders_lambda: float
    mean_order_value: float
    order_value_sigma: float
    p_cancel: float
    p_refund: float  # per completed order
    returning_add_mult: float = 1.25

    def with_multipliers(self, m: dict[str, float]) -> "CheckoutParams":
        return _apply(self, m)


def _checkout_probs(p: CheckoutParams, c: pd.DataFrame):
    pa = p.p_add_to_cart * np.where(c["returning"].to_numpy(), p.returning_add_mult, 1.0)
    po = p.p_order_given_begin * c["plan_tier"].map(PLAN_ORDER_MULT).fillna(1.0).to_numpy()
    _check_probs(p_add_to_cart=pa, p_order_given_begin=po, p_begin_given_add=p.p_begin_given_add,
                 p_cancel=p.p_cancel, p_refund=p.p_refund)
    return pa, po


def checkout_truth(p: CheckoutParams, c: pd.DataFrame) -> dict[str, float]:
    pa, po = _checkout_probs(p, c)
    lam, cancel = p.extra_orders_lambda, p.p_cancel
    p_order = pa * p.p_begin_given_add * po
    orders = p_order * (1 + lam) * (1 - cancel)
    q = (1 - cancel) * p.p_refund
    return {
        "add_to_cart_rate": float(pa.mean()),
        "conversion_rate": float((p_order * (1 - cancel * np.exp(-lam * (1 - cancel)))).mean()),
        "orders_per_user": float(orders.mean()),
        "revenue_per_user": float((orders * p.mean_order_value).mean()),
        "refund_rate": float((p_order * (1 - (1 - q) * np.exp(-lam * q))).mean()),
    }


def generate_checkout(p: CheckoutParams, c: pd.DataFrame, rng: np.random.Generator, prefix: str) -> Activity:
    c = c.reset_index(drop=True)
    n, uid, anchor = len(c), c["user_id"].to_numpy(), c["anchor_ts"]
    wmax = window_seconds(anchor)
    pa, po = _checkout_probs(p, c)
    add = rng.random(n) < pa
    begin = add & (rng.random(n) < p.p_begin_given_add)
    ordered = begin & (rng.random(n) < po)
    add_off = rng.uniform(0, 2 * DAY, n)
    begin_off = add_off + rng.uniform(0, 1, n) * DAY
    allidx = np.arange(n)
    ev = [pd.DataFrame({"user_id": uid, "event_type": "visit", "event_ts": anchor})]
    for name, mask, off in (("add_to_cart", add, add_off), ("begin_checkout", begin, begin_off)):
        i = allidx[mask]
        ev.append(pd.DataFrame({"user_id": uid[i], "event_type": name, "event_ts": _at(anchor, i, off[i])}))
    events = pd.concat(ev, ignore_index=True)
    events.insert(0, "event_id", [f"{prefix}e{i}" for i in range(len(events))])

    k = np.where(ordered, 1 + rng.poisson(p.extra_orders_lambda, n), 0)
    idx = np.repeat(allidx, k)
    m = len(idx)
    off = begin_off[idx] + rng.uniform(0, 1, m) * (wmax[idx] - begin_off[idx])
    order_ts = _at(anchor, idx, off)
    mu = np.log(p.mean_order_value) - p.order_value_sigma**2 / 2
    value = rng.lognormal(mu, p.order_value_sigma, m)
    completed = rng.random(m) >= p.p_cancel
    refunded = completed & (rng.random(m) < p.p_refund)
    refund_off = off + rng.uniform(0, 1, m) * (wmax[idx] - off)
    cancel_upd = order_ts + pd.to_timedelta(rng.uniform(600, 6 * 3600, m), unit="s")
    refund_ts = _at(anchor, idx, refund_off)
    orders = pd.DataFrame({
        "order_id": [f"{prefix}o{i}" for i in range(m)], "user_id": uid[idx], "order_ts": order_ts,
        "order_value": value, "item_count": 1 + rng.poisson(1.2, m),
        "discount_value": np.where(rng.random(m) < 0.15, value * rng.uniform(0.05, 0.2, m), 0.0),
        "status": np.where(completed, "completed", "cancelled"),
        "refund_ts": refund_ts.where(refunded), "refund_value": np.where(refunded, value, np.nan),
        "updated_at": order_ts.where(completed & ~refunded, cancel_upd).where(~refunded, refund_ts),
    })
    comp = orders[orders.status == "completed"]
    g = comp.groupby("user_id")
    outcomes = pd.DataFrame({"user_id": uid})
    n_comp = outcomes.user_id.map(g.size()).fillna(0)
    outcomes["conversion_rate"] = n_comp.clip(upper=1).astype(int)
    outcomes["orders_per_user"] = n_comp.astype(int)
    outcomes["revenue_per_user"] = outcomes.user_id.map(g.order_value.sum()).fillna(0.0)
    outcomes["add_to_cart_rate"] = add.astype(int)
    outcomes["refund_rate"] = outcomes.user_id.map(comp[comp.refund_ts.notna()].groupby("user_id").size()).fillna(0).clip(upper=1).astype(int)
    return Activity({"checkout_events": events, "checkout_orders": orders}, outcomes)


# ---------------------------------------------------------------- Email
@dataclass(frozen=True)
class EmailParams:
    extra_sends_lambda: float   # sends per user in the window = 1 + Poisson(lambda)
    p_open: float               # per delivered send
    p_click_given_open: float
    p_unsubscribe: float        # per send
    p_bounce: float             # per send
    new_user_open_mult: float = 1.3  # users in their first 30 days open more

    def with_multipliers(self, m: dict[str, float]) -> "EmailParams":
        return _apply(self, m)


def _email_open_prob(p: EmailParams, c: pd.DataFrame) -> np.ndarray:
    """Per-send probability that a send is delivered and opened."""
    p_open = p.p_open * np.where(c["tenure_days"].to_numpy() < 30, p.new_user_open_mult, 1.0)
    _check_probs(p_open=p_open, p_click_given_open=p.p_click_given_open, p_unsubscribe=p.p_unsubscribe, p_bounce=p.p_bounce)
    return (1 - p.p_bounce) * p_open


def _any(q, lam):
    """P(at least one success) over 1 + Poisson(lam) independent sends with per-send probability q."""
    return 1 - (1 - q) * np.exp(-lam * q)


def email_truth(p: EmailParams, c: pd.DataFrame) -> dict[str, float]:
    qo = _email_open_prob(p, c)
    lam = p.extra_sends_lambda
    return {
        "open_rate": float(_any(qo, lam).mean()),
        "click_rate": float(_any(qo * p.p_click_given_open, lam).mean()),
        "unsubscribe_rate": float(_any(p.p_unsubscribe, lam)),
        "bounce_rate": float(_any(p.p_bounce, lam)),
        "emails_per_user": 1 + lam,
    }


def generate_email(p: EmailParams, c: pd.DataFrame, rng: np.random.Generator, prefix: str) -> Activity:
    c = c.reset_index(drop=True)
    n, uid, anchor = len(c), c["user_id"].to_numpy(), c["anchor_ts"]
    wmax = window_seconds(anchor)
    qo = _email_open_prob(p, c)
    k = 1 + rng.poisson(p.extra_sends_lambda, n)
    idx = np.repeat(np.arange(n), k)
    m = len(idx)
    first = np.r_[True, idx[1:] != idx[:-1]]
    send_off = np.where(first, 0.0, rng.uniform(0, 1, m) * 0.6 * wmax[idx])
    camp = rng.integers(1, 21, m)
    ctype = np.array(["promo", "newsletter", "lifecycle"], dtype=object)[camp % 3]
    sends = pd.DataFrame({"send_id": [f"{prefix}s{i}" for i in range(m)], "user_id": uid[idx],
                          "campaign_id": [f"camp-{x}" for x in camp], "campaign_type": ctype,
                          "sent_ts": _at(anchor, idx, send_off)})
    bounce = rng.random(m) < p.p_bounce
    opened = ~bounce & (rng.random(m) < (qo[idx] / (1 - p.p_bounce)))
    clicked = opened & (rng.random(m) < p.p_click_given_open)
    unsub = rng.random(m) < p.p_unsubscribe
    rows = []
    for name, mask in (("bounce", bounce), ("open", opened), ("click", clicked), ("unsubscribe", unsub)):
        i = np.flatnonzero(mask)
        room = (wmax[idx[i]] - send_off[i] - 60).clip(0)
        rows.append(pd.DataFrame({"send_id": sends.send_id.to_numpy()[i], "user_id": uid[idx][i], "event_type": name,
                                  "event_ts": _at(anchor, idx[i], send_off[i] + 60 + rng.uniform(0, 1, len(i)) * room)}))
    events = pd.concat(rows, ignore_index=True)
    events.insert(0, "event_id", [f"{prefix}e{i}" for i in range(len(events))])
    users = pd.Series(uid)
    outcomes = pd.DataFrame({"user_id": uid, "emails_per_user": k})
    for metric, et in (("open_rate", "open"), ("click_rate", "click"), ("unsubscribe_rate", "unsubscribe"), ("bounce_rate", "bounce")):
        outcomes[metric] = users.isin(events.loc[events.event_type == et, "user_id"]).astype(int).to_numpy()
    return Activity({"email_sends": sends, "email_events": events}, outcomes)


# ---------------------------------------------------------------- Onboarding
@dataclass(frozen=True)
class OnboardingParams:
    p_profile: float
    p_first_project_given_profile: float
    p_invite_given_project: float
    p_activate_given_project: float
    p_paid_given_activated: float
    p_standard_given_paid: float = 0.75

    def with_multipliers(self, m: dict[str, float]) -> "OnboardingParams":
        return _apply(self, m)


def _onb_profile(p: OnboardingParams, c: pd.DataFrame) -> np.ndarray:
    mult = c["acquisition_channel"].map({"referral": 1.15, "paid_search": 0.92}).fillna(1.0).to_numpy()
    pp = p.p_profile * mult
    _check_probs(p_profile=pp, p_first_project_given_profile=p.p_first_project_given_profile,
                 p_invite_given_project=p.p_invite_given_project, p_activate_given_project=p.p_activate_given_project,
                 p_paid_given_activated=p.p_paid_given_activated, p_standard_given_paid=p.p_standard_given_paid)
    return pp


def onboarding_truth(p: OnboardingParams, c: pd.DataFrame) -> dict[str, float]:
    pp = _onb_profile(p, c)
    p_proj = pp * p.p_first_project_given_profile
    p_act = p_proj * p.p_activate_given_project
    p_paid = p_act * p.p_paid_given_activated
    avg_price = p.p_standard_given_paid * PRICE["standard"] + (1 - p.p_standard_given_paid) * PRICE["premium"]
    return {
        "profile_completion_rate": float(pp.mean()),
        "invited_teammate_rate": float((p_proj * p.p_invite_given_project).mean()),
        "activation_rate": float(p_act.mean()),
        "paid_conversion_rate": float(p_paid.mean()),
        "paid_revenue_per_user": float(p_paid.mean() * avg_price),
    }


def generate_onboarding(p: OnboardingParams, c: pd.DataFrame, rng: np.random.Generator, prefix: str) -> Activity:
    c = c.reset_index(drop=True)
    n, uid, anchor = len(c), c["user_id"].to_numpy(), c["anchor_ts"]
    profile = rng.random(n) < _onb_profile(p, c)
    project = profile & (rng.random(n) < p.p_first_project_given_profile)
    invite = project & (rng.random(n) < p.p_invite_given_project)
    activated = project & (rng.random(n) < p.p_activate_given_project)
    paid = activated & (rng.random(n) < p.p_paid_given_activated)
    t_profile = rng.uniform(60, 1 * DAY, n)
    t_project = t_profile + rng.uniform(60, 1 * DAY, n)
    t_invite = t_project + rng.uniform(60, 1 * DAY, n)
    t_act = t_project + rng.uniform(60, 1.5 * DAY, n)
    t_pay = t_act + rng.uniform(60, 1.5 * DAY, n)  # at most 5 days after the anchor, well inside the window
    allidx = np.arange(n)
    parts = [pd.DataFrame({"user_id": uid, "step": "signup", "event_ts": anchor})]
    for step, mask, off in (("profile_completed", profile, t_profile), ("first_project", project, t_project),
                            ("invited_teammate", invite, t_invite), ("activated", activated, t_act)):
        i = allidx[mask]
        parts.append(pd.DataFrame({"user_id": uid[i], "step": step, "event_ts": _at(anchor, i, off[i])}))
    events = pd.concat(parts, ignore_index=True)
    events.insert(0, "event_id", [f"{prefix}e{i}" for i in range(len(events))])
    i = allidx[paid]
    tier = np.where(rng.random(len(i)) < p.p_standard_given_paid, "standard", "premium")
    price = np.array([PRICE[t] for t in tier], dtype=float)
    pay_ts = _at(anchor, i, t_pay[i])
    payments = pd.DataFrame({"payment_id": [f"{prefix}pay{j}" for j in range(len(i))], "user_id": uid[i], "paid_ts": pay_ts,
                             "amount": price, "plan_tier": tier})
    changes = pd.DataFrame({"user_id": uid[i], "effective_date": pay_ts.dt.date.to_numpy(), "plan_tier": tier, "mrr": price})
    outcomes = pd.DataFrame({"user_id": uid, "profile_completion_rate": profile.astype(int),
                             "invited_teammate_rate": invite.astype(int), "activation_rate": activated.astype(int),
                             "paid_conversion_rate": paid.astype(int), "paid_revenue_per_user": 0.0})
    outcomes.loc[paid, "paid_revenue_per_user"] = price
    return Activity({"onboarding_events": events, "payments": payments}, outcomes, changes)


PRODUCTS = {
    "checkout": (CheckoutParams, checkout_truth, generate_checkout),
    "email": (EmailParams, email_truth, generate_email),
    "onboarding": (OnboardingParams, onboarding_truth, generate_onboarding),
}


def load_params(product: str):
    raw = yaml.safe_load(resources.files("p2.simulator").joinpath("baselines.yaml").read_text())[product]
    return PRODUCTS[product][0](**raw)
