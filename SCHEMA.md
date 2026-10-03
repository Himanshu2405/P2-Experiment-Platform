# Schema and Data Dictionary

Generated file. Do not edit by hand. Change `src/p2/warehouse/sources.yaml` (raw tables) or `src/p2/store/docs.py` and `src/p2/store/schema.py` (application tables), then run `python -m p2.schema_doc`. A test fails if this file is out of date or any column lacks a description.

Related: [DATA_SOURCES.md](DATA_SOURCES.md) explains how the sources are used; [TECH_SPEC.md](TECH_SPEC.md) explains the platform.

## 1. How the data is organised

Everything lives in BigQuery, project `master-chariot-413216`, location US. One environment is four datasets, named `p2_<env>_<layer>` (for example `p2_dev_raw`). Tests create and drop `p2_test_*` environments.

| Dataset | What it holds | Written by |
|---|---|---|
| `raw` | The nine source tables exactly as the company's systems land them, including duplicates and late updates. | Simulator (plays the company's systems) |
| `staging` | One de-duplicated view per raw table. All SQL written in the catalog reads these, never raw. | Generated from `sources.yaml` |
| `analytics` | Per-experiment build tables and baseline tables created by the pipeline. | Pipeline |
| `app` | Application state: team, products, catalog, experiments, audit log. | Service layer |

Flow: raw tables, then staging views, then catalog item SQL reads the views, then the pipeline writes analytics tables, then the app reads them. All timestamps are UTC.

## 2. Raw sources (9 tables)

| Table | Product | What it includes |
|---|---|---|
| [`exp_assignments`](#exp_assignments) | shared | The experiment tool's log of which user was bucketed into which arm, and when. |
| [`users`](#users) | shared | Company user master with one row per user: signup date, geography, acquisition channel and industry. |
| [`plan_history`](#plan_history) | shared | Slowly changing plan history, with one row each time a user's plan changes. |
| [`checkout_events`](#checkout_events) | checkout | Storefront funnel events (visit, add_to_cart, begin_checkout), one row per event. |
| [`checkout_orders`](#checkout_orders) | checkout | Current state of each order; a row is updated when its status or refund changes. |
| [`email_sends`](#email_sends) | email | Every email sent to a user, one row per send. |
| [`email_events`](#email_events) | email | Recipient reactions to sent emails (open, click, unsubscribe, bounce), one row per reaction. |
| [`onboarding_events`](#onboarding_events) | onboarding | Onboarding milestones reached by each new user (signup through activation), one row per milestone. |
| [`payments`](#payments) | onboarding | Payments users make when they buy or upgrade a plan, one row per payment. |

### `exp_assignments`

The experiment tool's log of which user was bucketed into which arm, and when.

- Product: shared
- Time column: `assigned_at`
- Partitioned by: `DATE(assigned_at)`
- Clustered by: `experiment_id`, `user_id`
- One logical row per: `experiment_id`, `user_id`; the winner is the first row by `assigned_at ASC, ingested_at ASC`
- Staging view: `stg_exp_assignments` (what `{{ exp_assignments }}` resolves to in SQL)

| Column | Type | Null | Description |
|---|---|---|---|
| `experiment_id` | STRING | no | Experiment the user was assigned to; lowercase letters, digits and hyphens. |
| `user_id` | STRING | no | The assigned user; matches users.user_id. |
| `arm` | STRING | no | Which group the user was bucketed into: control or variant. |
| `assigned_at` | TIMESTAMP | no | When the user was bucketed (UTC). Starts the user's 7-day measurement window. |
| `ingested_at` | TIMESTAMP | no | When this row landed in the warehouse (UTC); breaks ties when de-duplicating. |

### `users`

Company user master with one row per user: signup date, geography, acquisition channel and industry.

- Product: shared
- Time column: `signup_date`
- Partitioned by: not partitioned (small table)
- Clustered by: `user_id`
- One logical row per: `user_id`; the winner is the first row by `ingested_at DESC`
- Staging view: `stg_users` (what `{{ users }}` resolves to in SQL)

| Column | Type | Null | Description |
|---|---|---|---|
| `user_id` | STRING | no | Unique user identifier used by every other table. |
| `signup_date` | DATE | no | Day the user signed up. |
| `country` | STRING | yes | Two-letter country code; missing for about 3 percent of users. |
| `region` | STRING | yes | Sales region of the country (NA, EMEA, APAC, LATAM); missing when country is missing. |
| `acquisition_channel` | STRING | no | How the user first arrived (organic, paid_search, partner, sales, referral). |
| `industry` | STRING | yes | Industry of the user's business (retail, media, services, health, other); missing for about 5 percent. |
| `ingested_at` | TIMESTAMP | no | When this row landed in the warehouse (UTC); breaks ties when de-duplicating. |

### `plan_history`

Slowly changing plan history, with one row each time a user's plan changes.

- Product: shared
- Time column: `effective_date`
- Partitioned by: `effective_date`
- Clustered by: `user_id`
- One logical row per: `user_id`, `effective_date`; the winner is the first row by `ingested_at DESC`
- Staging view: `stg_plan_history` (what `{{ plan_history }}` resolves to in SQL)

| Column | Type | Null | Description |
|---|---|---|---|
| `user_id` | STRING | no | The user whose plan changed. |
| `effective_date` | DATE | no | Day the new plan took effect. |
| `plan_tier` | STRING | no | Plan in force from that day (free, standard, premium). |
| `mrr` | FLOAT64 | no | Monthly recurring revenue on that plan; 0 for free. |
| `ingested_at` | TIMESTAMP | no | When this row landed in the warehouse (UTC); breaks ties when de-duplicating. |

### `checkout_events`

Storefront funnel events (visit, add_to_cart, begin_checkout), one row per event.

- Product: checkout
- Time column: `event_ts`
- Partitioned by: `DATE(event_ts)`
- Clustered by: `event_type`, `user_id`
- One logical row per: `event_id`; the winner is the first row by `event_ts ASC, ingested_at ASC`
- Staging view: `stg_checkout_events` (what `{{ checkout_events }}` resolves to in SQL)

| Column | Type | Null | Description |
|---|---|---|---|
| `event_id` | STRING | no | Unique event identifier; duplicates of the same id are retries. |
| `user_id` | STRING | no | The user who did it. |
| `event_type` | STRING | no | What happened (visit, add_to_cart, begin_checkout). |
| `event_ts` | TIMESTAMP | no | When it happened (UTC). |
| `ingested_at` | TIMESTAMP | no | When this row landed in the warehouse (UTC); breaks ties when de-duplicating. |

### `checkout_orders`

Current state of each order; a row is updated when its status or refund changes.

- Product: checkout
- Time column: `order_ts`
- Partitioned by: `DATE(order_ts)`
- Clustered by: `user_id`
- One logical row per: `order_id`; the winner is the first row by `updated_at DESC, ingested_at DESC`
- Staging view: `stg_checkout_orders` (what `{{ checkout_orders }}` resolves to in SQL)

| Column | Type | Null | Description |
|---|---|---|---|
| `order_id` | STRING | no | Unique order identifier. |
| `user_id` | STRING | no | The buyer. |
| `order_ts` | TIMESTAMP | no | When the order was placed (UTC). |
| `order_value` | FLOAT64 | no | Gross order value before discounts and refunds. |
| `item_count` | INT64 | no | Number of items in the order. |
| `discount_value` | FLOAT64 | no | Discount applied to the order; 0 if none. |
| `status` | STRING | no | completed or cancelled. Only completed orders count as purchases. |
| `refund_ts` | TIMESTAMP | yes | When the order was refunded; null if it was not. |
| `refund_value` | FLOAT64 | yes | Amount refunded; null if not refunded. |
| `updated_at` | TIMESTAMP | no | When this version of the order row was last changed; the latest version wins. |
| `ingested_at` | TIMESTAMP | no | When this row landed in the warehouse (UTC); breaks ties when de-duplicating. |

### `email_sends`

Every email sent to a user, one row per send.

- Product: email
- Time column: `sent_ts`
- Partitioned by: `DATE(sent_ts)`
- Clustered by: `user_id`
- One logical row per: `send_id`; the winner is the first row by `sent_ts ASC, ingested_at ASC`
- Staging view: `stg_email_sends` (what `{{ email_sends }}` resolves to in SQL)

| Column | Type | Null | Description |
|---|---|---|---|
| `send_id` | STRING | no | Unique send identifier; email_events.send_id refers to it. |
| `user_id` | STRING | no | The recipient. |
| `campaign_id` | STRING | no | Campaign the email belongs to. |
| `campaign_type` | STRING | no | Kind of campaign (promo, newsletter, lifecycle). |
| `sent_ts` | TIMESTAMP | no | When the email was sent (UTC). |
| `ingested_at` | TIMESTAMP | no | When this row landed in the warehouse (UTC); breaks ties when de-duplicating. |

### `email_events`

Recipient reactions to sent emails (open, click, unsubscribe, bounce), one row per reaction.

- Product: email
- Time column: `event_ts`
- Partitioned by: `DATE(event_ts)`
- Clustered by: `event_type`, `user_id`
- One logical row per: `event_id`; the winner is the first row by `event_ts ASC, ingested_at ASC`
- Staging view: `stg_email_events` (what `{{ email_events }}` resolves to in SQL)

| Column | Type | Null | Description |
|---|---|---|---|
| `event_id` | STRING | no | Unique event identifier; duplicates of the same id are retries. |
| `send_id` | STRING | no | The send this reaction belongs to (email_sends.send_id). |
| `user_id` | STRING | no | The recipient who reacted. |
| `event_type` | STRING | no | What happened (open, click, unsubscribe, bounce). |
| `event_ts` | TIMESTAMP | no | When it happened (UTC). |
| `ingested_at` | TIMESTAMP | no | When this row landed in the warehouse (UTC); breaks ties when de-duplicating. |

### `onboarding_events`

Onboarding milestones reached by each new user (signup through activation), one row per milestone.

- Product: onboarding
- Time column: `event_ts`
- Partitioned by: `DATE(event_ts)`
- Clustered by: `step`, `user_id`
- One logical row per: `event_id`; the winner is the first row by `event_ts ASC, ingested_at ASC`
- Staging view: `stg_onboarding_events` (what `{{ onboarding_events }}` resolves to in SQL)

| Column | Type | Null | Description |
|---|---|---|---|
| `event_id` | STRING | no | Unique event identifier; duplicates of the same id are retries. |
| `user_id` | STRING | no | The user who reached the milestone. |
| `step` | STRING | no | Milestone (signup, profile_completed, first_project, invited_teammate, activated). |
| `event_ts` | TIMESTAMP | no | When the milestone was reached (UTC). |
| `ingested_at` | TIMESTAMP | no | When this row landed in the warehouse (UTC); breaks ties when de-duplicating. |

### `payments`

Payments users make when they buy or upgrade a plan, one row per payment.

- Product: onboarding
- Time column: `paid_ts`
- Partitioned by: `DATE(paid_ts)`
- Clustered by: `user_id`
- One logical row per: `payment_id`; the winner is the first row by `paid_ts ASC, ingested_at ASC`
- Staging view: `stg_payments` (what `{{ payments }}` resolves to in SQL)

| Column | Type | Null | Description |
|---|---|---|---|
| `payment_id` | STRING | no | Unique payment identifier. |
| `user_id` | STRING | no | The paying user. |
| `paid_ts` | TIMESTAMP | no | When the payment was made (UTC). |
| `amount` | FLOAT64 | no | Amount paid. |
| `plan_tier` | STRING | no | Plan purchased (standard, premium). |
| `ingested_at` | TIMESTAMP | no | When this row landed in the warehouse (UTC); breaks ties when de-duplicating. |

## 3. Staging views (9 views)

Each view keeps one row per logical key. The winner is decided by the table's de-duplication order, so retries and superseded versions disappear before any metric sees them.

| View | Built from | One row per | Winner is the first by |
|---|---|---|---|
| `stg_exp_assignments` | `exp_assignments` | `experiment_id`, `user_id` | `assigned_at ASC, ingested_at ASC` |
| `stg_users` | `users` | `user_id` | `ingested_at DESC` |
| `stg_plan_history` | `plan_history` | `user_id`, `effective_date` | `ingested_at DESC` |
| `stg_checkout_events` | `checkout_events` | `event_id` | `event_ts ASC, ingested_at ASC` |
| `stg_checkout_orders` | `checkout_orders` | `order_id` | `updated_at DESC, ingested_at DESC` |
| `stg_email_sends` | `email_sends` | `send_id` | `sent_ts ASC, ingested_at ASC` |
| `stg_email_events` | `email_events` | `event_id` | `event_ts ASC, ingested_at ASC` |
| `stg_onboarding_events` | `onboarding_events` | `event_id` | `event_ts ASC, ingested_at ASC` |
| `stg_payments` | `payments` | `payment_id` | `paid_ts ASC, ingested_at ASC` |

## 4. Analytics tables (generated per experiment)

Created by the pipeline in the `analytics` dataset. Names use the experiment id with hyphens turned into underscores. Intermediate tables expire after 7 days; the final table does not. There is no fixed schema for the final table: it is generated from the items an experiment selected.

| Table | Columns | What it is |
|---|---|---|
| `exp_<id>__cohort` | `user_id`, `arm`, `assigned_date` | Everyone assigned to the experiment, one row per user (first assignment wins). |
| `exp_<id>__attr_<item>` | `user_id`, `value` | One filter or dimension resolved as of each user's assignment date, with its default when no row applies. |
| `exp_<id>__audience` | `user_id`, `arm`, `assigned_date` | Cohort users who pass every selected filter. |
| `exp_<id>__m_<item>` | `user_id`, `value` | One metric per user over assigned date through six days later; 0 when there is no activity. |
| `exp_<id>__final` | `user_id`, `arm`, `assigned_date`, one column per dimension, one column per metric | What analysis reads. Columns are named by catalog item id; dimensions are STRING, binary metrics INT64, continuous metrics FLOAT64. |
| `base_<product>__cohort`, `__m_<item>`, `__final` | same shapes, arm is `baseline` | Historic cohort used to compute baselines; expires after 1 day. |

## 5. Query contracts for catalog items

Every metric, dimension and filter is one SQL query with a fixed output shape.

| Kind | Columns returned | Meaning |
|---|---|---|
| metric | `user_id` STRING, `metric_date` DATE, `value` number | One row per user per day; days with no row count as 0. Binary metrics return 1 and aggregate with max; continuous are non-negative and aggregate with sum. |
| dimension | `user_id` STRING, `effective_date` DATE, `value` STRING | History rows; the latest on or before the assignment date applies, else the item's default. At most 20 distinct values. |
| filter | `user_id` STRING, `effective_date` DATE, `value` INT64 (0 or 1) | Same as a dimension; users with no row at entry count as 0 and are excluded. |

## 6. Application tables (13 tables)

| Table | What it includes |
|---|---|
| [`team_members`](#team_members) | The people who use the platform (a simulated team for now), each with a role and a product. |
| [`products`](#products) | The three products (Checkout, Email, Onboarding), each with an owner. |
| [`data_sources`](#data_sources) | Registry of the warehouse source tables authors may query, with their de-duplication rule and column schema. |
| [`catalog_items`](#catalog_items) | Governed metrics, dimensions and filters, one row per version, with their SQL, lifecycle status and automated-check report. |
| [`experiments`](#experiments) | One row per experiment: who owns it, what it tests, when it ran, where it stands and the final decision. |
| [`experiment_items`](#experiment_items) | The metrics, filters and dimensions chosen for each experiment, pinned to a catalog version, with the statistical plan the data scientist entered for primary and guardrail metrics. |
| [`results`](#results) | Results per experiment and metric, one set of rows per run. Interim rows (while running) hold only counts, means and the difference; final rows add the tests. The latest run of each kind is current. |
| [`daily_stats`](#daily_stats) | Cumulative metric by day and arm, for the over-time charts. Replaced on every monitoring refresh and final analysis, so it always matches the latest run. |
| [`segment_results`](#segment_results) | Results per metric for every slice the Tables tab offers, for the latest run only: a segment of a dimension chosen in the plan (for example paid users), the users who pass a filter chosen in the plan, or both together. Everyone with no filter is the main result in results, not here. Exploratory: every metric is tested two-sided at 0.05 with no correction for the number of segments. Interim runs hold only counts, means and the difference. |
| [`job_runs`](#job_runs) | One row per pipeline run, with its outcome. |
| [`job_steps`](#job_steps) | One row per step of a pipeline run: gates, cohort, filter, dimension and metric steps, final table. |
| [`audit_log`](#audit_log) | Append-only record of who did what and when; every write through the service layer adds a row. |
| [`sim_ground_truth`](#sim_ground_truth) | The simulator's exact true effect per experiment and metric, used only to validate the analysis, never by it. |

### `team_members`

The people who use the platform (a simulated team for now), each with a role and a product.

- Key: `user_id`

| Column | Type | Null | Description |
|---|---|---|---|
| `user_id` | STRING | no | Unique id of the team member, for example priya. |
| `name` | STRING | no | Display name shown in the Acting as switch. |
| `role` | STRING | no | What they may do: viewer, experimenter, metric_owner or admin. |
| `product_id` | STRING | yes | The product they work in; empty for admins and viewers. |
| `active` | BOOL | no | False once the member is deactivated; deactivated members cannot act. |
| `created_at` | TIMESTAMP | no | When the row was created (UTC). |
| `updated_at` | TIMESTAMP | no | When the row last changed (UTC). |

### `products`

The three products (Checkout, Email, Onboarding), each with an owner.

- Key: `product_id`

| Column | Type | Null | Description |
|---|---|---|---|
| `product_id` | STRING | no | Short id: checkout, email or onboarding. |
| `name` | STRING | no | Display name. |
| `owner_user_id` | STRING | yes | The team member who owns the product's metrics. |
| `created_at` | TIMESTAMP | no | When the row was created (UTC). |
| `updated_at` | TIMESTAMP | no | When the row last changed (UTC). |

### `data_sources`

Registry of the warehouse source tables authors may query, with their de-duplication rule and column schema.

- Key: `source_id`

| Column | Type | Null | Description |
|---|---|---|---|
| `source_id` | STRING | no | Logical name used in SQL as {{ source_id }}; equals the raw table name. |
| `product_id` | STRING | no | Which product may query it: shared, checkout, email or onboarding. |
| `raw_table` | STRING | no | Name of the landed table in the raw dataset. |
| `staging_view` | STRING | no | Name of the de-duplicated view in the staging dataset that {{ source_id }} resolves to. |
| `dedupe_key` | JSON | no | JSON list of columns that identify one logical row. |
| `dedupe_order` | STRING | no | Ordering that picks the winning row per key; the first row wins. |
| `time_column` | STRING | no | Main timestamp or date column of the table. |
| `user_column` | STRING | no | Column holding the user id. |
| `description` | STRING | no | One-line context for the source. |
| `owner` | STRING | yes | Who maintains the source. |
| `status` | STRING | no | active or retired. |
| `schema` | JSON | no | JSON list of the source's columns with type, nullable and description. |
| `created_at` | TIMESTAMP | no | When the row was created (UTC). |
| `updated_at` | TIMESTAMP | no | When the row last changed (UTC). |

### `catalog_items`

Governed metrics, dimensions and filters, one row per version, with their SQL, lifecycle status and automated-check report.

- Key: `item_id`, `version`

| Column | Type | Null | Description |
|---|---|---|---|
| `item_id` | STRING | no | Globally unique id; becomes the column name in experiment tables. |
| `version` | INT64 | no | Version number; a certified version never changes, a change is a new version. |
| `kind` | STRING | no | metric, dimension or filter. |
| `product_id` | STRING | no | Product the item belongs to, or shared for dimensions and filters usable everywhere. |
| `display_name` | STRING | no | Name shown in dropdowns. |
| `description` | STRING | no | Plain-language definition. |
| `category` | STRING | yes | Optional grouping label. |
| `sql` | STRING | no | The item's query, using {{ source }} placeholders, following the contract for its kind. |
| `status` | STRING | no | Draft, In review, Certified or Deprecated. |
| `author` | STRING | no | Team member who wrote this version (system for the seeded items). |
| `reviewed_by` | STRING | yes | Who certified or sent it back. |
| `reviewed_at` | TIMESTAMP | yes | When that review happened (UTC). |
| `review_note` | STRING | yes | The reviewer's note; required when sending an item back. |
| `value_type` | STRING | yes | Metrics only: binary or continuous. |
| `aggregation` | STRING | yes | Metrics only: how a user's daily values combine over the window, max or sum. |
| `good_direction` | STRING | yes | Metrics only: higher or lower is better. |
| `format` | STRING | yes | Metrics only: percent, number or currency. |
| `default_value` | STRING | yes | Dimensions only: value used when a user has no row as of entry, for example Unknown. |
| `qa_report` | JSON | yes | JSON report of the automated checks with a fingerprint of what they checked. |
| `created_at` | TIMESTAMP | no | When the version was created (UTC). |
| `updated_at` | TIMESTAMP | no | When the row last changed (UTC). |

### `experiments`

One row per experiment: who owns it, what it tests, when it ran, where it stands and the final decision.

- Key: `experiment_id`

| Column | Type | Null | Description |
|---|---|---|---|
| `experiment_id` | STRING | no | Unique id: lowercase letters, digits and hyphens. |
| `product_id` | STRING | no | Product the experiment runs in. |
| `owner_user_id` | STRING | no | Team member who owns it; only they or an admin may change it. |
| `name` | STRING | no | Short name. |
| `hypothesis` | STRING | no | What the team expects to happen and why; may be empty. |
| `status` | STRING | no | Draft (plan incomplete), Designed (plan saved), Running (the latest run was a live refresh), Analyzed (the latest run was the final analysis) or Decided (a decision is recorded and stays). Plans can be edited in any status. |
| `launch_date` | DATE | no | First day of the experiment (the day it launched). |
| `runtime_days` | INT64 | no | How many days it runs, counting the launch day; the last day is launch_date plus runtime_days minus 1. Users are measured from their assignment day to the last day. |
| `end_date` | DATE | yes | Last day of the experiment when it differs from the planned runtime (for example it was extended); empty means launch_date plus runtime_days minus 1. Can be changed at any time. |
| `created_at` | TIMESTAMP | no | When the experiment was created (UTC). |
| `updated_at` | TIMESTAMP | no | When the row last changed (UTC); used to detect conflicting edits. |
| `decision` | STRING | yes | Final call once decided: ship, no ship, iterate or inconclusive. |
| `decision_notes` | STRING | yes | Reasoning behind the decision. |
| `decided_by` | STRING | yes | Who recorded the decision. |
| `decided_at` | TIMESTAMP | yes | When it was recorded (UTC). |

### `experiment_items`

The metrics, filters and dimensions chosen for each experiment, pinned to a catalog version, with the statistical plan the data scientist entered for primary and guardrail metrics.

- Key: `experiment_id`, `item_id`

| Column | Type | Null | Description |
|---|---|---|---|
| `experiment_id` | STRING | no | The experiment. |
| `item_id` | STRING | no | The catalog item chosen. |
| `item_version` | INT64 | no | The catalog version pinned at design time; later certifications do not change it. |
| `kind` | STRING | no | metric, filter or dimension. |
| `role` | STRING | no | primary, guardrail, secondary, filter or dimension. |
| `baseline` | FLOAT64 | yes | Expected value of the metric in control, as entered by the data scientist (primary and guardrail only). |
| `std` | FLOAT64 | yes | Standard deviation as entered, for continuous metrics only. |
| `effect` | FLOAT64 | yes | Minimum detectable effect, or maximum acceptable degradation (the margin) for a guardrail. |
| `effect_kind` | STRING | yes | relative or absolute. |
| `alpha` | FLOAT64 | yes | Significance level used in the analysis. |
| `power` | FLOAT64 | yes | Target power the data scientist planned for; recorded for reference. |
| `sidedness` | STRING | yes | one-sided or two-sided; guardrails are always one-sided. |

### `results`

Results per experiment and metric, one set of rows per run. Interim rows (while running) hold only counts, means and the difference; final rows add the tests. The latest run of each kind is current.

- Key: `experiment_id`, `item_id`, `run_at`

| Column | Type | Null | Description |
|---|---|---|---|
| `experiment_id` | STRING | no | The experiment. |
| `item_id` | STRING | no | The metric analysed. |
| `run_at` | TIMESTAMP | no | When the analysis ran (UTC). |
| `role` | STRING | no | The metric's role in the experiment. |
| `mean_control` | FLOAT64 | yes | Average in control. |
| `mean_variant` | FLOAT64 | yes | Average in variant. |
| `difference` | FLOAT64 | yes | Variant minus control. |
| `ci_low` | FLOAT64 | yes | Lower bound of the confidence interval for the difference. |
| `ci_high` | FLOAT64 | yes | Upper bound of the confidence interval for the difference. |
| `p_value` | FLOAT64 | yes | P-value of the test; for a guardrail, of the non-inferiority test. |
| `verdict` | STRING | yes | Plain-language result: Significant improvement or decline, No significant difference, or for guardrails Passed, Inconclusive or Failed. |
| `achieved_power` | FLOAT64 | yes | Power to detect the planned effect (or to show non-inferiority) given the users actually observed; empty for secondary metrics. |
| `n_control` | INT64 | yes | Users in control. |
| `n_variant` | INT64 | yes | Users in variant. |
| `relative_lift` | FLOAT64 | yes | Difference divided by the control mean; empty when the control mean is zero. |
| `ci_level` | FLOAT64 | yes | Confidence level the interval was built at (for a one-sided test it is 1 minus twice alpha). |
| `kind` | STRING | yes | final (full tests, after the last day) or interim (descriptive numbers only, while the experiment runs). |
| `through_date` | DATE | yes | Last day of data the numbers include; for a final result it is the experiment's last day. |
| `params` | JSON | yes | JSON of the plan the analysis followed: alpha, sidedness, effect and its kind, baseline, planned power, and the margin in metric units. |

### `daily_stats`

Cumulative metric by day and arm, for the over-time charts. Replaced on every monitoring refresh and final analysis, so it always matches the latest run.

- Key: `experiment_id`, `item_id`, `day`, `arm`

| Column | Type | Null | Description |
|---|---|---|---|
| `experiment_id` | STRING | no | The experiment. |
| `item_id` | STRING | no | The metric. |
| `day` | DATE | no | The day; users assigned on or before it are included, each measured from their own assignment day through this day. |
| `arm` | STRING | no | control or variant. |
| `n_users` | INT64 | no | Users assigned on or before this day. |
| `mean_value` | FLOAT64 | no | Average metric value per user, cumulative through this day. |
| `var_value` | FLOAT64 | yes | Sample variance of the per-user value, cumulative through this day; used for the range band of the difference chart. |
| `run_at` | TIMESTAMP | no | When the run that produced it happened (UTC). |

### `segment_results`

Results per metric for every slice the Tables tab offers, for the latest run only: a segment of a dimension chosen in the plan (for example paid users), the users who pass a filter chosen in the plan, or both together. Everyone with no filter is the main result in results, not here. Exploratory: every metric is tested two-sided at 0.05 with no correction for the number of segments. Interim runs hold only counts, means and the difference.

- Key: `experiment_id`, `item_id`, `dimension_id`, `segment`, `filter_id`

| Column | Type | Null | Description |
|---|---|---|---|
| `experiment_id` | STRING | no | The experiment. |
| `item_id` | STRING | no | The metric. |
| `dimension_id` | STRING | no | The dimension that defines the segments; empty for a filter-only slice. |
| `segment` | STRING | no | The dimension value, as of each user's assignment day; empty for a filter-only slice. |
| `filter_id` | STRING | no | The filter whose passing users are analysed; empty when no filter is applied. |
| `run_at` | TIMESTAMP | no | When the run happened (UTC). |
| `kind` | STRING | no | interim (live monitoring) or final. |
| `role` | STRING | no | The metric's role in the experiment's plan. |
| `mean_control` | FLOAT64 | yes | Average in control within the segment. |
| `mean_variant` | FLOAT64 | yes | Average in variant within the segment. |
| `difference` | FLOAT64 | yes | Variant minus control. |
| `relative_lift` | FLOAT64 | yes | Difference divided by the control average; empty when that is zero. |
| `ci_low` | FLOAT64 | yes | Lower end of the confidence interval of the difference (final only). |
| `ci_high` | FLOAT64 | yes | Upper end of the confidence interval of the difference (final only). |
| `ci_level` | FLOAT64 | yes | Confidence level of the interval (final only). |
| `p_value` | FLOAT64 | yes | Two-sided p-value (final only). |
| `verdict` | STRING | yes | Significant improvement, Significant decline or No significant difference (final only). |
| `n_control` | INT64 | no | Users in control within the segment. |
| `n_variant` | INT64 | no | Users in variant within the segment. |
| `through_date` | DATE | no | Last day of data included. |

### `job_runs`

One row per pipeline run, with its outcome.

- Key: `job_id`

| Column | Type | Null | Description |
|---|---|---|---|
| `job_id` | STRING | no | Unique id of the run. |
| `experiment_id` | STRING | no | The experiment the run built data for. |
| `type` | STRING | no | Kind of job; pipeline for now. |
| `status` | STRING | no | running, succeeded or failed. |
| `started_at` | TIMESTAMP | no | When it started (UTC). |
| `finished_at` | TIMESTAMP | yes | When it ended (UTC). |
| `error` | STRING | yes | The data-quality gate or step error if it failed. |
| `detail` | JSON | yes | JSON with cohort_users, audience_users, n_control and n_variant on success. |

### `job_steps`

One row per step of a pipeline run: gates, cohort, filter, dimension and metric steps, final table.

- Key: `job_id`, `step`

| Column | Type | Null | Description |
|---|---|---|---|
| `job_id` | STRING | no | The run. |
| `step` | STRING | no | Step name, for example cohort, attr_region_us, m_conversion_rate, final. |
| `table_name` | STRING | yes | Table the step wrote; empty for gates. |
| `status` | STRING | no | ok or failed. |
| `row_count` | INT64 | yes | Rows the step produced. |
| `duration_s` | FLOAT64 | yes | Seconds the step took. |

### `audit_log`

Append-only record of who did what and when; every write through the service layer adds a row.

- Key: `audit_id`

| Column | Type | Null | Description |
|---|---|---|---|
| `audit_id` | STRING | no | Unique id of the entry. |
| `ts` | TIMESTAMP | no | When it happened (UTC). |
| `actor` | STRING | no | Team member who acted, or system for seeding. |
| `action` | STRING | no | What was done, for example experiment.design or catalog.certify. |
| `entity_type` | STRING | no | Kind of thing affected: experiment, catalog_item, data_source or a table name. |
| `entity_id` | STRING | no | Id of the thing affected. |
| `detail` | JSON | yes | JSON with specifics of the action. |

### `sim_ground_truth`

The simulator's exact true effect per experiment and metric, used only to validate the analysis, never by it.

- Key: `experiment_id`, `metric_id`

| Column | Type | Null | Description |
|---|---|---|---|
| `experiment_id` | STRING | no | The simulated experiment. |
| `metric_id` | STRING | no | The metric. |
| `true_control` | FLOAT64 | no | True expected value in control for the simulated cohort. |
| `true_variant` | FLOAT64 | no | True expected value in variant. |
| `true_lift` | FLOAT64 | no | True relative lift, variant over control minus 1. |

## 7. Key relationships

There are no enforced foreign keys in BigQuery; the service layer checks these.

- `users.user_id` is the identity used by every source table and every pipeline table.
- `email_events.send_id` refers to `email_sends.send_id`.
- `exp_assignments.experiment_id` equals `experiments.experiment_id` in the app.
- `experiment_items (item_id, item_version)` refers to `catalog_items (item_id, version)`.
- `catalog_items.product_id` and `experiments.product_id` refer to `products.product_id`; `products.owner_user_id` and `experiments.owner_user_id` refer to `team_members.user_id`.
- `job_runs.experiment_id` refers to `experiments`; `job_steps.job_id` refers to `job_runs`.
- `results`, `daily_stats`, `segment_results` and `sim_ground_truth` are keyed by experiment and metric.

## 8. Conventions and simulated data

- Ids: experiment ids use lowercase letters, digits and hyphens; catalog item ids use lowercase letters, digits and underscores and become column names, so `user_id`, `arm`, `assigned_date`, `value`, `metric_date` and `effective_date` are reserved.
- The data is synthetic. Row ids carry a prefix so a simulation can be replaced exactly: `uni-` for the shared universe, `hist-<product>-` for baseline-period activity, `<experiment id>-` for an experiment's activity.
- Planted mess in raw tables, all removed by the staging views or the whole-day window: duplicate rows, orders first landed as completed then updated to cancelled or refunded, activity before assignment or after the 7-day window, duplicated assignments, and missing attributes (country, region, industry, plan history).
- The measurement window is whole days: the assignment date and the six days after it.
