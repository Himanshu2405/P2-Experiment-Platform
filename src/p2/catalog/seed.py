"""The starter catalog: 15 metrics, 5 dimensions, 4 filters, each with real SQL over the staging views.

These are seeded as Certified by the system. `tests/test_catalog_bq.py` runs the automated checks against
every one of them on real data, so the seed is verified even though startup does not re-run checks.
Queries use logical source names (`{{ checkout_orders }}`) and follow the contracts in DATA_SOURCES.md.
State-at-entry items use effective_date = the day after the state was created, so a change on the
assignment day itself can never leak into a user's entry state.
"""
from dataclasses import dataclass

from p2.registry.registry import MetricDef, Registry


@dataclass(frozen=True)
class SeedItem:
    item_id: str
    kind: str                      # metric, dimension, filter
    product_id: str                # checkout, email, onboarding, or shared (dimensions and filters only)
    display_name: str
    description: str
    sql: str
    value_type: str | None = None  # metrics: binary or continuous
    aggregation: str | None = None  # metrics: max or sum over the window
    good_direction: str | None = None
    format: str | None = None
    default_value: str | None = None  # dimensions


def _flag(table: str, where: str, ts: str, user: str = "user_id") -> str:
    return f"SELECT {user}, DATE({ts}) AS metric_date, 1 AS value FROM {{{{ {table} }}}} WHERE {where} GROUP BY {user}, metric_date"


def _metric(item_id, product, name, desc, sql, vtype, agg, direction, fmt) -> SeedItem:
    return SeedItem(item_id, "metric", product, name, desc, sql, vtype, agg, direction, fmt)


METRICS = [
    # ---- Checkout
    _metric("conversion_rate", "checkout", "Conversion rate", "Share of users with at least one completed order",
            _flag("checkout_orders", "status = 'completed'", "order_ts"), "binary", "max", "higher", "percent"),
    _metric("revenue_per_user", "checkout", "Revenue per user", "Average value of completed orders per user, including users with none",
            "SELECT user_id, DATE(order_ts) AS metric_date, SUM(order_value) AS value FROM {{ checkout_orders }} "
            "WHERE status = 'completed' GROUP BY user_id, metric_date", "continuous", "sum", "higher", "currency"),
    _metric("orders_per_user", "checkout", "Orders per user", "Average number of completed orders per user, including users with none",
            "SELECT user_id, DATE(order_ts) AS metric_date, COUNT(*) AS value FROM {{ checkout_orders }} "
            "WHERE status = 'completed' GROUP BY user_id, metric_date", "continuous", "sum", "higher", "number"),
    _metric("add_to_cart_rate", "checkout", "Add-to-cart rate", "Share of users who added at least one item to cart",
            _flag("checkout_events", "event_type = 'add_to_cart'", "event_ts"), "binary", "max", "higher", "percent"),
    _metric("refund_rate", "checkout", "Refund rate", "Share of users with a refunded order",
            _flag("checkout_orders", "status = 'completed' AND refund_ts IS NOT NULL", "refund_ts"), "binary", "max", "lower", "percent"),
    # ---- Email
    _metric("open_rate", "email", "Open rate", "Share of users who opened at least one email",
            _flag("email_events", "event_type = 'open'", "event_ts"), "binary", "max", "higher", "percent"),
    _metric("click_rate", "email", "Click rate", "Share of users who clicked at least one email",
            _flag("email_events", "event_type = 'click'", "event_ts"), "binary", "max", "higher", "percent"),
    _metric("unsubscribe_rate", "email", "Unsubscribe rate", "Share of users who unsubscribed",
            _flag("email_events", "event_type = 'unsubscribe'", "event_ts"), "binary", "max", "lower", "percent"),
    _metric("bounce_rate", "email", "Bounce rate", "Share of users with at least one bounced email",
            _flag("email_events", "event_type = 'bounce'", "event_ts"), "binary", "max", "lower", "percent"),
    _metric("emails_per_user", "email", "Emails per user", "Average number of emails sent to a user",
            "SELECT user_id, DATE(sent_ts) AS metric_date, COUNT(*) AS value FROM {{ email_sends }} GROUP BY user_id, metric_date",
            "continuous", "sum", "higher", "number"),
    # ---- Onboarding
    _metric("profile_completion_rate", "onboarding", "Profile completion rate", "Share of new users who completed their profile",
            _flag("onboarding_events", "step = 'profile_completed'", "event_ts"), "binary", "max", "higher", "percent"),
    _metric("activation_rate", "onboarding", "Activation rate", "Share of new users who reached activation",
            _flag("onboarding_events", "step = 'activated'", "event_ts"), "binary", "max", "higher", "percent"),
    _metric("invited_teammate_rate", "onboarding", "Invited-teammate rate", "Share of new users who invited a teammate",
            _flag("onboarding_events", "step = 'invited_teammate'", "event_ts"), "binary", "max", "higher", "percent"),
    _metric("paid_conversion_rate", "onboarding", "Paid conversion rate", "Share of new users who made a payment",
            "SELECT user_id, DATE(paid_ts) AS metric_date, 1 AS value FROM {{ payments }} GROUP BY user_id, metric_date",
            "binary", "max", "higher", "percent"),
    _metric("paid_revenue_per_user", "onboarding", "Paid revenue per user", "Average payment amount per new user, including users who did not pay",
            "SELECT user_id, DATE(paid_ts) AS metric_date, SUM(amount) AS value FROM {{ payments }} GROUP BY user_id, metric_date",
            "continuous", "sum", "higher", "currency"),
]

DIMENSIONS = [
    SeedItem("plan_tier", "dimension", "shared", "Plan tier at entry", "Plan the user was on when assigned",
             "SELECT user_id, effective_date, plan_tier AS value FROM {{ plan_history }}", default_value="Unknown"),
    SeedItem("region", "dimension", "shared", "Region", "Sales region of the user's country",
             "SELECT user_id, signup_date AS effective_date, region AS value FROM {{ users }} WHERE region IS NOT NULL",
             default_value="Unknown"),
    SeedItem("acquisition_channel", "dimension", "shared", "Acquisition channel", "How the user first arrived",
             "SELECT user_id, signup_date AS effective_date, acquisition_channel AS value FROM {{ users }}", default_value="Unknown"),
    SeedItem("tenure_bucket", "dimension", "shared", "Tenure at entry", "Days since signup when assigned, in buckets",
             "SELECT user_id, DATE_ADD(signup_date, INTERVAL b.d DAY) AS effective_date, b.label AS value FROM {{ users }}, "
             "UNNEST([STRUCT(0 AS d, '0-29 days' AS label), STRUCT(30, '30-89 days'), STRUCT(90, '90-179 days'), "
             "STRUCT(180, '180+ days')]) AS b", default_value="Unknown"),
    SeedItem("customer_type", "dimension", "checkout", "Customer type at entry", "New or returning buyer when assigned",
             "SELECT user_id, DATE_ADD(DATE(MIN(order_ts)), INTERVAL 1 DAY) AS effective_date, 'returning' AS value "
             "FROM {{ checkout_orders }} WHERE status = 'completed' GROUP BY user_id", default_value="new"),
]

FILTERS = [
    SeedItem("paid_at_entry", "filter", "shared", "Paid at entry", "Only users on a paid plan when assigned",
             "SELECT user_id, effective_date, IF(plan_tier != 'free', 1, 0) AS value FROM {{ plan_history }}"),
    SeedItem("existing_user", "filter", "shared", "Existing user", "Only users who signed up before the assignment day",
             "SELECT user_id, DATE_ADD(signup_date, INTERVAL 1 DAY) AS effective_date, 1 AS value FROM {{ users }}"),
    SeedItem("has_prior_order", "filter", "checkout", "Has a prior order", "Only users with a completed order before the assignment day",
             "SELECT user_id, DATE_ADD(DATE(MIN(order_ts)), INTERVAL 1 DAY) AS effective_date, 1 AS value "
             "FROM {{ checkout_orders }} WHERE status = 'completed' GROUP BY user_id"),
    SeedItem("region_us", "filter", "shared", "Region is US", "Only users whose country is the US",
             "SELECT user_id, signup_date AS effective_date, IF(country = 'US', 1, 0) AS value FROM {{ users }}"),
]

SEED_ITEMS: list[SeedItem] = METRICS + DIMENSIONS + FILTERS


def seed_registry() -> Registry:
    """The seed metrics as a Registry (handy for tests and offline tools; the app builds it from the catalog)."""
    return Registry([
        MetricDef(metric_id=m.item_id, version=1, product_id=m.product_id, display_name=m.display_name,
                  description=m.description, type=m.value_type, window_aggregation=m.aggregation,
                  good_direction=m.good_direction)
        for m in METRICS])
