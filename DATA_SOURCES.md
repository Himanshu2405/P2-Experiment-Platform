# Data Sources and Schemas: P2 Experiment Platform v1.0

Status: approved 2026-09-29. This is the foundation document: sources, schemas, contracts, and how every piece is used. Change it deliberately, because everything downstream depends on it.

Related: [PLAN.md](PLAN.md), [TECH_SPEC.md](TECH_SPEC.md), [P2_Decisions_Log.md](P2_Decisions_Log.md). The generated column-level data dictionary for every table is [SCHEMA.md](SCHEMA.md).

## 1. Principles (learned from a real experimentation platform, applied here)

- **Layers, each with one job.** Raw (landed) then Staging (cleaned views) then Analytics (per-experiment build) then App (state). Never mix them.
- **One small query per unit.** A metric, a dimension, and a filter are each their own short query with a fixed contract. The platform joins them. Nobody edits a shared file, and no query grows into a monster.
- **The platform generates the big SQL, people write the small SQL.** The wide per-experiment table is assembled by code from many small pieces.
- **State at entry, not today.** Dimensions and filters describe the user as they were when assigned, never after the experiment could have changed them.
- **Adding something never needs a schema migration.** Experiment tables are built only from the items the experiment selected.
- **Real-world mess is planted on purpose** in the simulated sources, so the pipeline and checks are tested against it.
- **Public synthetic data only.** No employer names, tables, SQL, or numbers.

## 2. Layers and environments

One environment = four BigQuery datasets in project `master-chariot-413216`, location US. Environments: `dev`, `test_<id>` (created and dropped by tests), `prod` later.

| Dataset | Holds | Written by |
|---|---|---|
| `p2_<env>_raw` | The 9 landed source tables below | The simulator, playing the company's systems |
| `p2_<env>_staging` | One cleaned view per raw table, `stg_<source_id>` | The platform, generated from the source registry |
| `p2_<env>_analytics` | Per-experiment build tables (cohort, attributes, metrics, final) | The pipeline |
| `p2_<env>_app` | Application state (catalog, experiments, results, audit) | The service layer |

- Authors never write SQL against raw tables. They write against logical names like `{{ checkout_events }}`, which resolve to the staging view for the current environment. Dev and test runs therefore never touch other environments.

## 3. The 9 raw source tables

All columns are NOT NULL unless marked nullable. Timestamps are UTC `TIMESTAMP`. Every table also carries `ingested_at TIMESTAMP` (when the row landed), used to break ties when de-duplicating.

### 3.1 Shared sources (3)

**`exp_assignments`** (the experiment tool's log; one source for all products)
| Column | Type | Notes |
|---|---|---|
| experiment_id | STRING | lowercase letters, digits, hyphens |
| user_id | STRING | |
| arm | STRING | `control` or `variant` |
| assigned_at | TIMESTAMP | when the user was bucketed |
- Partition `DATE(assigned_at)`, cluster `experiment_id, user_id`.
- De-duplication key `(experiment_id, user_id)`, keep the earliest `assigned_at`.

**`users`** (company user master, one row per user)
| Column | Type | Notes |
|---|---|---|
| user_id | STRING | |
| signup_date | DATE | |
| country | STRING | nullable |
| region | STRING | `NA`, `EMEA`, `APAC`, `LATAM`; nullable |
| acquisition_channel | STRING | `organic`, `paid_search`, `partner`, `sales`, `referral` |
| industry | STRING | `retail`, `media`, `services`, `health`, `other`; nullable |
- Cluster `user_id`. De-duplication key `user_id`, keep latest `ingested_at`.

**`plan_history`** (slowly changing: one row each time a user's plan changes)
| Column | Type | Notes |
|---|---|---|
| user_id | STRING | |
| effective_date | DATE | the day the plan took effect |
| plan_tier | STRING | `free`, `standard`, `premium` |
| mrr | FLOAT64 | monthly recurring revenue, 0 for free |
- Partition `effective_date`, cluster `user_id`. De-duplication key `(user_id, effective_date)`, keep latest `ingested_at`.

### 3.2 Checkout (2)

**`checkout_events`**
| Column | Type | Notes |
|---|---|---|
| event_id | STRING | |
| user_id | STRING | |
| event_type | STRING | `visit`, `add_to_cart`, `begin_checkout` |
| event_ts | TIMESTAMP | |
- Partition `DATE(event_ts)`, cluster `event_type, user_id`. De-duplication key `event_id`, keep earliest `event_ts`.

**`checkout_orders`** (current state of each order; rows are updated when status or refund changes)
| Column | Type | Notes |
|---|---|---|
| order_id | STRING | |
| user_id | STRING | |
| order_ts | TIMESTAMP | |
| order_value | FLOAT64 | gross |
| item_count | INT64 | |
| discount_value | FLOAT64 | 0 if none |
| status | STRING | `completed` or `cancelled` |
| refund_ts | TIMESTAMP | nullable |
| refund_value | FLOAT64 | nullable |
| updated_at | TIMESTAMP | last change |
- Partition `DATE(order_ts)`, cluster `user_id`. De-duplication key `order_id`, keep latest `updated_at`.

### 3.3 Email (2)

**`email_sends`**
| Column | Type | Notes |
|---|---|---|
| send_id | STRING | |
| user_id | STRING | recipient |
| campaign_id | STRING | |
| campaign_type | STRING | `promo`, `newsletter`, `lifecycle` |
| sent_ts | TIMESTAMP | |
- Partition `DATE(sent_ts)`, cluster `user_id`. De-duplication key `send_id`.

**`email_events`**
| Column | Type | Notes |
|---|---|---|
| event_id | STRING | |
| send_id | STRING | links to `email_sends` |
| user_id | STRING | |
| event_type | STRING | `open`, `click`, `unsubscribe`, `bounce` |
| event_ts | TIMESTAMP | |
- Partition `DATE(event_ts)`, cluster `event_type, user_id`. De-duplication key `event_id`.

### 3.4 Onboarding (2)

**`onboarding_events`**
| Column | Type | Notes |
|---|---|---|
| event_id | STRING | |
| user_id | STRING | |
| step | STRING | `signup`, `profile_completed`, `first_project`, `invited_teammate`, `activated` |
| event_ts | TIMESTAMP | |
- Partition `DATE(event_ts)`, cluster `step, user_id`. De-duplication key `event_id`.

**`payments`**
| Column | Type | Notes |
|---|---|---|
| payment_id | STRING | |
| user_id | STRING | |
| paid_ts | TIMESTAMP | |
| amount | FLOAT64 | |
| plan_tier | STRING | plan purchased |
- Partition `DATE(paid_ts)`, cluster `user_id`. De-duplication key `payment_id`.

### 3.5 User universe
- One shared set of users across products (400,000 in dev, signing up 2025-01-01 to 2025-12-31). A user may be active in several products. Experiments draw their users from these, or (for Onboarding) create new signups.
- The source registry lives in `src/p2/warehouse/sources.yaml`. The raw DDL and the staging views are both generated from it, so a table is defined in one place. Until the app owns the registry (R2), the YAML is the seed.

## 4. Source registry (app state table `data_sources`)

An admin registers each source; the platform generates its staging view from these fields.

| Field | Meaning |
|---|---|
| source_id | logical name used in SQL, e.g. `checkout_events` |
| product_id | `shared`, `checkout`, `email`, `onboarding` |
| raw_table | physical table per environment |
| staging_view | `stg_<source_id>`, generated |
| dedupe_key | columns that identify one logical row |
| dedupe_order | column and direction that picks the winner |
| time_column | main timestamp or date, used for partition pruning |
| user_column | the user identity column |
| description, owner, status | documentation and lifecycle |
| schema | JSON of columns and types, used to validate metric queries |

Staging view shape (generated, not hand-written):
```sql
SELECT * EXCEPT (rn) FROM (
  SELECT t.*, ROW_NUMBER() OVER (PARTITION BY <dedupe_key> ORDER BY <dedupe_order>) AS rn
  FROM `<raw_table>` t
) WHERE rn = 1
```
Authors query staging views and so never repeat de-duplication.

## 5. Three query contracts, one catalog

A single catalog table `catalog_items` holds all three kinds, with the same lifecycle (Draft, In review, Certified, Deprecated), owner, product, versioning, and automated checks.

| Kind | Query returns | One row per | Used for |
|---|---|---|---|
| metric | `user_id STRING, metric_date DATE, value numeric` | user and day | Measuring outcomes |
| dimension | `user_id STRING, effective_date DATE, value STRING` | user and change | Slicing results by segment |
| filter | `user_id STRING, effective_date DATE, value INT64 (0 or 1)` | user and change | Deciding who is in the analysis |

Shared rules:
- Queries are a single `SELECT`, reference only registered sources by logical name, and stay under a cost cap.
- Results contain no duplicate keys and no nulls in key columns. Dimension values are checked for sensible cardinality (at most 20 distinct values).

Kind-specific fields:
- **Metric:** value type (binary or continuous), aggregation over the window (`sum`, or `max` for flags), good direction, format, allowed roles (primary, secondary, guardrail). Binary metrics use `max` and values 0 or 1. Continuous metrics use `sum` and non-negative values.
- **Dimension:** `default_value` (used when a user has no row as of entry, e.g. `Unknown`), display order of values.
- **Filter:** a user with no row as of entry counts as 0 (fails the filter).

### As-of-entry rule (dimensions and filters)
For each cohort user the platform takes the row with the latest `effective_date` on or before the user's assignment date. No row means the default. Authors must set `effective_date` to the date the state became true.
- Known limit: dates are whole days, so a change made on the assignment day itself can leak into that user's entry state. For states derived from events (a first order, a signup), the seed items use the day after the event as `effective_date`, so an order placed after assignment on the same day can never make a user look like a returning buyer. Plan history uses the change date as recorded. Revisit with timestamps if it becomes a problem.

### Illustrative queries
```sql
-- dimension: plan_tier (history table already has effective dates)
SELECT user_id, effective_date, plan_tier AS value FROM {{ plan_history }}

-- dimension: customer_type ('returning' from the date of the first completed order onward; default 'new')
SELECT user_id, DATE(MIN(order_ts)) AS effective_date, 'returning' AS value
FROM {{ checkout_orders }} WHERE status = 'completed' GROUP BY user_id

-- filter: signed_up_before_experiment style 'existing_user' (1 from the signup date onward)
SELECT user_id, signup_date AS effective_date, 1 AS value FROM {{ users }}

-- metric: conversion_rate (binary, aggregation max)
SELECT user_id, DATE(order_ts) AS metric_date, 1 AS value
FROM {{ checkout_orders }} WHERE status = 'completed' GROUP BY user_id, metric_date
```

## 6. How an experiment build works (small steps, one table each)

Intermediate tables live in `p2_<env>_analytics` (`exp_<experiment id>__<step>`, for example `exp_exp_001__cohort`) with a 7-day expiration; the final table `__final` does not expire. Every step records its status, row count, and time in the job log, including failed runs.

**The measurement window.** An experiment has a launch date and a runtime in days. Its last day is `launch_date + runtime_days - 1` (the launch day counts as day one). The cohort is the users assigned during that period. Each user is measured from their own assignment day through the last day, so a user assigned on day 1 of a 14-day experiment contributes 14 days of activity and a user assigned on day 13 contributes 2. There is no fixed per-user window.

1. **Gates (before):** the experiment has launched and its last day is over; no user in two arms (checked on the raw log); assignments exist between launch and last day; both arms present.
2. **Cohort:** `exp_<id>__cohort`, the first assignment per user with `assigned_date`, from `stg_exp_assignments`, limited to assignments between launch and last day.
3. **Attribute steps:** one small query per selected filter and per selected dimension, each resolved as of entry: `exp_<id>__attr_<item_id>` with `(user_id, value)`.
4. **Audience:** users who pass all selected filters (AND). Users before and after are recorded as a health signal.
5. **Metric steps:** one small query per selected metric, joined to the audience, rows kept from `assigned_date` through the last day, aggregated per user with the declared aggregation; no rows means 0. Table `exp_<id>__m_<item_id>`.
6. **Assemble:** `exp_<id>__final`, generated SQL joining everything: user_id, arm, assigned_date, one column per dimension, one column per metric.
7. **Gates (after):** final row count equals audience size; no nulls in metric columns.
8. **Statistics (RUN only):** one scan of the final table gives each arm's count, mean and variance per metric; the tests run on those summaries (see TECH_SPEC section 9).

Limits to keep experiments manageable: at most 30 metrics, 10 dimensions, 5 filters per experiment.

## 7. How each source is used

| Source | Cohort | Filters | Dimensions | Metrics |
|---|---|---|---|---|
| exp_assignments | yes | | | |
| users | | existing user, region | region, channel, industry, tenure bucket | |
| plan_history | | paid at entry | plan tier at entry | |
| checkout_events | | | | add to cart, begin checkout, visits |
| checkout_orders | | has prior order | customer type | conversion, revenue per user, orders per user, refund rate |
| email_sends | | | | emails received |
| email_events | | | | open, click, unsubscribe, bounce |
| onboarding_events | | | | profile completed, first project, invited teammate, activated |
| payments | | | | paid conversion, revenue |

## 8. Starter dimensions and filters (agreed)

- **Dimensions:** plan tier at entry, region, acquisition channel, tenure bucket (days since signup at entry), customer type (new or returning buyer at entry).
- **Filters:** paid at entry, existing users only (signed up before the experiment), has a prior order, region is US.
- All are built from the shared tables plus prior activity in product tables. No extra sources are needed.

## 9. Starter metrics (candidates; you pick in the catalog phase)

- Metric ids are globally unique across products (for example Onboarding's revenue metric is `paid_revenue_per_user`, not `revenue_per_user`). A provisional list of these candidates lives in `src/p2/registry/metrics.yaml` until the catalog replaces it.

- **Checkout:** conversion rate, revenue per user, orders per user, add-to-cart rate, refund rate (guardrail).
- **Email:** open rate, click rate, unsubscribe rate (guardrail), bounce rate (guardrail).
- **Onboarding:** profile completion rate, activation rate, invited-teammate rate, paid conversion rate.

## 10. Simulator (built in R1)

- Shared user universe: attributes with about 3% missing country and region and 5% missing industry. Everyone starts on the `free` plan at signup. About 30% of users who signed up before 2025-10-01 upgrade later (75% standard, 25% premium) through a legacy payment, which also writes the plan history row; about 3% of upgraders later downgrade; 2% of users have no plan history at all. Later signups get a paid plan only through onboarding payments.
- About half of users who signed up by 2025-08-01 have one or two earlier orders (returning buyers at entry).
- Each product has a small generative model with closed-form truth for its candidate metrics, including simple entry-attribute effects (returning buyers add to cart more, premium plans order more, new users open email more, referral signups complete profiles more). Effects are multipliers on parameters, so the true lift of any metric is exact for the cohort.
- All simulated activity falls on the anchor's date through six days later. For the planted effect to be exact, an experiment's runtime must cover that (assignments are spread over the first runtime minus 6 days), so the simulator needs a runtime of at least 7 days. This is a property of the simulated data, not a rule of the platform.
- Planted mess, all handled by the staging views and the whole-day window:
  - duplicate rows (about 2% of rows, delivered ten minutes later)
  - orders landed first as completed and updated later (cancelled or refunded): the latest version must win
  - activity after the seven simulated days (about 3% of users) and, for experiments, before the anchor (about 5% of users)
  - duplicated assignment rows (about 1%)
  - missing attributes and missing plan history
- Populations: Checkout and Email experiments sample users from the universe. Onboarding experiments create new signups, because that is who onboarding tests target; their users, plan history, and activity are landed together.
- Everything landed for one experiment or seed carries an id prefix (`<experiment_id>-`, `uni-`), so re-running a simulation replaces exactly its own rows.
- Volumes stay small: hundreds of thousands of users and about a million rows per table at most, far inside the free tier. Loads are batch jobs.
- Optional later: effects that differ by segment, to validate segment-level results.

## 11. Ownership

| Thing | Owner |
|---|---|
| Shared sources and the source registry | Admin |
| A product's tables | That product's owner, registered by the admin |
| Catalog items (metrics, dimensions, filters) | The author, certified by an owner or admin who is not the author |
| Staging views and pipeline SQL generation | Platform code |

## 12. Decisions locked (2026-09-29)

1. One shared user universe, with `users` and a slowly changing `plan_history`.
2. The 9 source tables and columns above.
3. Metrics, dimensions, and filters are all catalog items with their own contracts and the as-of-entry rule.
4. The platform serves de-duplicated staging views; authors do not repeat de-duplication.
5. The starter dimension and filter lists in section 8.

## 13. Open items (decide when reached)

- The exact metric list per product (you pick in the catalog phase).
- Whether numeric dimensions (not just categorical) are needed.
- Whether views need to become materialized tables for speed at larger scale.
- Timestamp-level entry state if day granularity proves too coarse.
- Late-arriving data and out-of-order updates beyond what is planted today.
