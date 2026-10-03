# Tech Spec: P2 Experiment Platform (team version) v0.3

Supersedes v0.1 (single-user app) and v0.2. Revised 2026-09-30: the data scientist records their own statistical plan (the tool no longer computes sample sizes or baselines), and each user is measured until the experiment's last day. Approved direction 2026-09-29, see [P2_Decisions_Log.md](P2_Decisions_Log.md). Sources, schemas, and query contracts are defined in [DATA_SOURCES.md](DATA_SOURCES.md), which is authoritative for anything about data. The column-level data dictionary, generated from code, is [SCHEMA.md](SCHEMA.md).

## 1. Purpose and scope

- An internal experiment measurement platform for a team of about 15 data scientists. Each works on a different product with its own metrics. Everyone can register experiments, pick governed metrics, and track every experiment the team runs.
- Design principle: routine work happens in a UI. Adding a metric, registering an experiment, onboarding a data source, and reading results never need git or python. Code changes are for platform features only.
- All configuration lives in shared storage, not in files in the repo.
- Public synthetic data only. No employer names, code, SQL, or numbers.

### Scope rules (deliberately simple)
- Unit of randomization and analysis is the **user**. One identity key. One assignment per user per experiment (first assignment wins).
- Not supported: session, account, store, or geo randomization; clustered standard errors; interference between users.
- Two arms per experiment (control and one variant) for now.
- The data scientist does their own power and sample-size work, the traditional way, and enters the result: alpha, power, baseline, standard deviation and MDE for the primary metric and each guardrail. The tool records and follows those numbers. It does not calculate or second-guess sample sizes or baselines, because the baseline depends on who was bucketed, which differs for every experiment.
- Measurement window: each user is measured from their assignment day through the experiment's last day, which is `launch_date + runtime_days - 1` (the launch day counts as day one). Only users assigned during the runtime are included.
- Fixed-horizon analysis, run once the experiment has ended. Frequentist first. Bayesian, SRM, CUPED and multiple-comparison correction are later.

## 2. Personas, roles, permissions

| Role | Who | Can do |
|---|---|---|
| viewer | PMs, leaders | Read experiments, results, catalog |
| experimenter | data scientists | Create and edit their own experiments; propose metrics (Draft) in their product |
| metric_owner | product metric owner | Everything an experimenter can; certify and deprecate metrics in their product, when not the author |
| admin | platform team | Everything; manage users, products, data sources; certify any metric; run jobs |

- Starting users (simulated team rows in BigQuery): one owner per product for Checkout, Email, and Onboarding, plus one admin and one read-only viewer. The UI does not use them to sign anyone in: the Owner of an experiment is picked in the form.
- Separation of duties: a metric cannot be certified by its own author. With one owner per product, the admin reviews until a product has two owners.
- No sign-in and no permissions in the tool (decision 2026-10-01: trust the data scientists, keep it light). Everyone can add, edit and run any experiment. Every UI action runs as one identity (`admin`), and the Owner is just a field. The service layer still takes an `actor` and still checks roles for other callers (scripts, tests); the UI always acts as the admin. Real sign-in is optional at deploy (R7).

## 3. Architecture

```
Streamlit UI  (three pages, navigation on top)
     |
Service layer  (p2.services: catalog, experiments, pipeline, stats; checks permissions, writes audit log)
     |                                   |
Store interface                    Warehouse client
 BigQueryStore | MemoryStore        raw -> staging -> analytics
     |
BigQuery dataset: app (state)
```

- Four BigQuery datasets per environment (`dev`, `test_*`, `prod`): `p2_<env>_raw` (landed sources), `p2_<env>_staging` (generated de-duplicated views), `p2_<env>_analytics` (per-experiment build tables), `p2_<env>_app` (application state). See DATA_SOURCES.md section 2.
- No Postgres and no FastAPI for now. Streamlit calls the service layer in process. Roles and audit are enforced in the service layer, not in the UI. FastAPI is added when a second client needs it (the chatbot) or at deploy time.
- `Store` is an interface with two implementations: `BigQueryStore` (real) and `MemoryStore` (fast offline tests). Swapping in Postgres later means one new implementation.
- The simulator plays the company's systems and lands the 9 raw source tables. Everything after that is platform. For the demo it also lands one fixed September 2026 dataset (`src/p2/simulator/demo.py`: 6 `sep-*` experiments, users assigned on every day of the month, activity to 30 Sep), landed from a dev-only button on Add Experiment.
- Design rule: one small query per unit (metric, dimension, filter), each with a fixed contract. The platform generates the large SQL that joins them.

### Known trade-offs of BigQuery for app state
- Each small read or write takes about 1 to 2 seconds. Mitigation: cached reads for lists, few writes per screen.
- No enforced unique or foreign keys. Mitigation: inserts are a `MERGE ... WHEN NOT MATCHED`, bulk seeding is one `MERGE` over an array of structs (`insert_missing`), so two processes starting at once cannot both insert. Learned the hard way: the first live run, with two server processes bootstrapping together and a load job after a pre-check, inserted every source twice.
- Concurrent edits: optimistic check on `updated_at`; a stale edit is rejected with a clear message. Fine at 15 users.
- Cost stays inside the free tier. The $5 budget alert still applies.

## 4. App state (BigQuery dataset `app`)

| Table | Key fields |
|---|---|
| `team_members` | user_id, name, role, product_id (nullable for admin), active. The people who use the platform; not the company `users` source table |
| `products` | product_id (checkout, email, onboarding), name, owner_user_id |
| `data_sources` | source_id, product_id (or shared), raw_table, staging_view, dedupe_key, dedupe_order, time_column, user_column, description, owner, status, schema (JSON) |
| `catalog_items` | item_id, version, kind (metric, dimension, filter), product_id, owner, author, category, display_name, description, sql, status, qa_report (JSON), reviewed_by, created_at, updated_at; metric: value_type, aggregation, good_direction, format; dimension: default_value; filter: none extra |
| `experiments` | experiment_id, product_id, owner, name, hypothesis (optional), status, launch_date, runtime_days, end_date (optional), created_at, updated_at, decision, decision_notes, decided_by, decided_at |
| `experiment_items` | experiment_id, item_id, item_version (pinned), kind, role (metrics: primary, guardrail, secondary, chosen per experiment, any metric in any role; plus filter and dimension), and for primary and guardrail the plan as entered: baseline, std, effect, effect kind, alpha, power, sidedness |
| `results` | experiment_id, metric item_id, run_at, role, mean per arm, difference, relative lift, confidence interval and its level, p-value, verdict, achieved power, users per arm, plan parameters used, kind (interim or final) and through_date. Interim rows (live monitoring) carry only means, difference, lift and users; no p-value, interval or verdict. One set of rows per run; the latest run of each kind is current |
| `segment_results` | experiment_id, metric item_id, dimension_id, segment, run_at, kind (interim or final), role, mean per arm, difference, relative lift, interval, p-value, verdict, users per arm, through_date. Latest run only; exploratory |
| `daily_stats` | experiment_id, item_id, day, arm, users so far, cumulative mean, run_at. Replaced on every monitoring refresh and final analysis; feeds the over-time chart |
| `job_runs` | job_id, experiment_id, type, status, started, finished, error; `job_steps`: job_id, step, table, status, row counts, duration |
| `audit_log` | ts, actor, action, entity_type, entity_id, detail (JSON) |
| `sim_ground_truth` | experiment_id, metric item_id, true control, true variant, true lift. Validation only, never read by the analysis path |

- `experiment_id` uses lowercase letters, digits, and hyphens; `item_id` uses lowercase letters, digits, and underscores. Both map one-to-one onto warehouse table names.
- Experiment statuses: Draft (saved, plan incomplete), Designed (plan saved), Running (the latest run was a live refresh), Analyzed (the latest run was the final analysis), Decided (a decision is recorded and stays). The status follows the latest run. Nothing is locked: everything except the experiment id can be edited in any status.
- Catalog statuses: Draft, In review, Certified, Deprecated.

## 5. Data sources and products

- The 9 raw source tables (3 shared, 2 each for Checkout, Email, Onboarding), their columns, partitioning, de-duplication keys, and the source registry are specified in DATA_SOURCES.md sections 3 and 4.
- Authors refer to sources by logical name (`{{ checkout_events }}`); the platform substitutes the staging view for the environment.
- Starter dimensions, filters, and candidate metrics: DATA_SOURCES.md sections 8 and 9. You pick the final metric list per product in the catalog phase.

## 6. Catalog: metrics, dimensions, filters

**How metrics are added (current decision, 2026-10-01).** Metrics, segments and filters are added in the data source, in BigQuery, not in the tool. The tool only reads the rows whose status is Certified (the Metric catalog page lists them and the experiment form offers them). There is no add, review or version screen, to keep the tool light. To add a metric, insert one row into `p2_<env>_app.catalog_items`:

```sql
INSERT INTO `master-chariot-413216.p2_dev_app.catalog_items`
  (item_id, version, kind, product_id, display_name, description, sql, status, author,
   value_type, aggregation, good_direction, format, created_at, updated_at)
VALUES ('big_basket_rate', 1, 'metric', 'checkout', 'Big basket rate', 'Share of users with an order of five or more items',
  """SELECT user_id, DATE(order_ts) AS metric_date, 1 AS value FROM {{ checkout_orders }}
     WHERE status = 'completed' AND item_count >= 5 GROUP BY user_id, metric_date""",
  'Certified', 'priya', 'binary', 'max', 'higher', 'percent', CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP());
```

`item_id` is lowercase letters, digits and underscores and is unique; `author` is a team member id (shown as "Added by"); the SQL uses `{{ source }}` names and returns the contract below; a binary metric uses `max` and returns 1, a continuous metric uses `sum` and returns non-negative values. Segments and filters follow the same pattern with `kind` set to `dimension` (add `default_value`) or `filter`. A BigQuery test inserts a metric this way and checks the tool picks it up. The sections below describe the service-layer lifecycle and automated checks, which are still in the code and tested but are not exposed in the UI.

All three are catalog items. In the service layer they have one lifecycle, one set of automated checks, and one review flow. Contracts (DATA_SOURCES.md section 5):

| Kind | Query returns | Used for |
|---|---|---|
| metric | `user_id, metric_date, value` (user-day) | Outcomes, aggregated over each user's window, from their assignment day to the experiment's last day |
| dimension | `user_id, effective_date, value` (string) | Segments, resolved as of the assignment date |
| filter | `user_id, effective_date, value` (0 or 1) | A view of the results for the users who pass it, resolved as of the assignment date; it does not cut the experiment |

### Automated checks (must pass before Certified; built in R3, `src/p2/catalog/checks.py`)
1. Safety, from a BigQuery dry run (nothing executes). Stops at the first failure:
   - only registered sources, written as `{{ name }}`; no other Jinja; sources must belong to the item's product or be shared;
   - no direct table references (a backticked dotted name or a `p2_...` dataset), so the de-duplicated staging views cannot be bypassed;
   - the query compiles, and is a single `SELECT` (the dry run's statement type; scripts, DDL and DML fail);
   - every table the dry run reports is an allowed source (the dry run reports the raw tables a view reads, so this also blocks other products' sources);
   - estimated scan under the cost cap (1 GB);
   - exactly the contract columns with the right types.
2. Data, from one real run wrapped in a statistics query: not empty; no nulls; no duplicate keys; dates not in the future; metrics binary only 0 or 1 or continuous non-negative; filters only 0 or 1; dimensions at most 20 distinct values.
3. Preview in the report: rows, users, mean and max, top dimension values, share passing a filter; a warning for a binary metric that never varies.
- The report is stored as `qa_report` with a fingerprint of everything the checks depend on. Changing the SQL or key fields after a run makes the report stale, and submission requires a fresh, passing report.
- The seed items are certified by the system without running checks at startup; the BigQuery test suite runs the checks on all 24 of them against real data.

### Lifecycle and versioning
- Draft (author edits) then In review (locked) then Certified (visible in dropdowns) then Deprecated (kept for pinned experiments, hidden from new ones).
- Certified versions are immutable; a change is a new version. Experiments pin `(item_id, version)`. Nothing used by an experiment is ever deleted.
- Only Certified items appear in experiment dropdowns. Authors also see their own Drafts. A certifier must be the product's metric owner or an admin and must not be the author; certifying a new version deprecates the older certified ones. Shared items are admin territory.

## 7. Pipeline (built in R3, revised in R5, `src/p2/pipeline/`)

For an experiment, the platform runs small steps, each writing one table in `analytics` (naming `exp_<experiment id>__<step>`, hyphens become underscores). SQL for each step is generated by `sqlgen.py` (pure functions, unit-tested); `runner.py` runs them, in parallel where steps are independent. Full description: DATA_SOURCES.md section 6.

1. `gates_before`, in this order: the experiment has launched; its last day is over; no user in more than one arm (checked on the raw log, because the de-duplicated view silently picks one arm for such a user); assignments exist between launch and last day (if none but others exist outside, the message names both date ranges); both arms present.
2. `cohort`: first assignment per user, from the staging view, for users assigned between the launch date and the last day.
3. `attr_<item>`: one step per selected filter and dimension, resolved as of entry (the latest row with `effective_date` on or before the assignment date, else the default).
4. `audience`: everyone assigned during the runtime (filters are views, not cuts). Zero users is a gate failure.
5. `m_<item>`: one step per selected metric over days `assigned_date` through the experiment's last day with the metric's window aggregation; no rows means 0. Binary metrics are INT64, continuous FLOAT64.
6. `final`: generated SQL joining everything: user, arm, assigned date, one column per dimension, one column per metric, named by item id. Built with `CREATE OR REPLACE TABLE`, so a rebuild is atomic and identical.
7. `gates_after`: final row count equals the audience and no metric column has nulls.
8. `stats` (RUN only): one scan of the final table returns each arm's count, mean and sample variance per metric (`summary_stats`). Python receives a few numbers per metric, never user rows, so memory is constant for any experiment size. The tests run on those summaries.

- Intermediate tables expire after 7 days; the final table does not.
- Every run is a row in `job_runs` and every step a row in `job_steps` (status, table, row count, seconds), including failed runs. A failed gate leaves the experiment's status unchanged and is shown on the Experiments page.
- The build uses the item versions pinned in the experiment's plan, so later certifications never change a running experiment.
- The experiment tool's assignment log is checked when the plan is written (how many users, which dates, a warning if the first assignment is far from the launch date) and again, as a hard gate, at RUN.
- Each run also saves a daily per-arm series (`daily_stats`): for every day from launch, the users assigned so far, each measured from their own assignment day through that day, as a cumulative mean. One extra query per metric. Its last day equals the experiment-level mean (tested on BigQuery). Slices: every run also computes the per-metric numbers for each segment value, each filter and each pair (`segment_results`; one query per combination of dimension and filter, run in parallel). They are exploratory: a plain two-sided test at 0.05 per metric with no correction for how many views are looked at; live runs hold only counts, means and the difference. `daily_stats` also holds the per-arm variance so the difference chart can draw its range.

## 8. The experiment plan (built; R4 wizard, revised in R5)

What the data scientist enters:
- Experiment id (as in the experiment tool's assignment log), product, launch date, runtime in days; the owner is picked from a dropdown of team members ("Select your name"; the product follows the owner and can be changed) and is separate from the experiment name; name; optional hypothesis.
- **Primary metric:** baseline, standard deviation (continuous metrics only), minimum detectable effect (relative or absolute), alpha, power, one- or two-sided. **Guardrails:** the same, with the effect read as the maximum acceptable degradation (the non-inferiority margin); always one-sided. **Secondary metrics:** just picked from the catalog, analysed at alpha 0.05 and labelled exploratory.
- Optional segments (dimensions) and filters from the catalog. Everyone assigned during the runtime is always analysed; segments and filters add views on the results (the Tables tab dropdowns list exactly those in the plan). The final table carries each dimension as a column and each filter as a 0 or 1 column. Each run also computes, in the warehouse, the same per-metric numbers for every segment value, every filter (users who pass it) and every segment-and-filter pair (`segment_results`). They are exploratory: plain two-sided test at 0.05, no correction for the number of views.
- Nothing is pre-filled except alpha (0.05) and power (0.80). Baselines and effects must be typed, in the metric's own units (percent for rates in the UI; stored as fractions).
- The tool validates the entries (for example a binary baseline must be between 0 and 1, a continuous metric needs a standard deviation) and stores them exactly as entered. It does not compute sample sizes, baselines or durations.
- Dates: launch date (filled from the first assignment day in the log, editable), planned runtime in days (from the power calculation) and an optional end date. With no end date the last day is launch plus runtime minus one; with one, monitoring and the final analysis load data only through it, so an experiment can run past its planned runtime. All three can be changed at any time. Extending an experiment that already has results shows a warning (reading results and then running longer raises the false-positive risk) but never blocks.
- Metric roles: exactly one primary, zero or more guardrails, optional secondary. Every metric from every product is offered in every role; a metric picked in one role disappears from the other two, so it holds one role per experiment. Items are pinned to their catalog version. Saving re-pins to the latest certified versions.
- The metric type (rate or continuous) comes from the catalog, is shown read-only, and decides the test (z-test or Welch t-test). The standard deviation field is always visible but disabled for rates.
- The New experiment page only creates. It starts with the Experiment ID alone. If the id already exists it says so and points to the Experiment Catalog, where editing happens (same form). If the id is not in the experiment tool's assignment log it stops with "does not exist", so a plan is entered once the experiment is live. After the first save the form turns into the editor for that experiment. The form is one page: Experiment, Metrics, Audience (optional), then Save and Save and run. Save keeps whatever is complete (a plan without a primary metric is kept as a Draft). Save and run also builds the numbers: live monitoring while the experiment is running, the final analysis once it has ended. What was typed survives visiting other pages.

## 9. Monitoring and analysis (built in R5, `src/p2/stats/`)

**Live monitoring (from the first complete day).** "Refresh monitoring" (or Save and run on a running experiment) builds the per-user table through yesterday and saves descriptive numbers only: users, mean per arm, difference and relative lift. There are no p-values, intervals or verdicts, because a fixed-horizon test must not be read before the runtime ends (no peeking); the page says "Interim view. No conclusions until the last day". Status becomes Running. Refresh is manual for now; a schedule comes with the deploy. The over-time chart is built from `daily_stats` (see section 7).

**Final analysis (after the last day).** The gate refuses it while the experiment is still running. It builds the table through the last day, takes the summary statistics from the warehouse, and computes for every metric in its role:
- **Binary metrics:** two-proportion z-test (pooled variance for the p-value, unpooled for the interval).
- **Continuous metrics:** Welch t-test with Welch-Satterthwaite degrees of freedom.
- **Primary and secondary:** difference (variant minus control), relative lift, confidence interval, p-value, and a verdict (Significant improvement, Significant decline, No significant difference; one-sided tests report Not significant). The direction that counts as an improvement follows the metric's good direction.
- **Guardrails:** one-sided non-inferiority test. Harm is how much worse the variant is in the metric's bad direction; the null is that harm is at least the margin. Verdicts: Passed (the upper bound of harm is below the margin), Failed (even the lower bound is beyond it), Inconclusive.
- **Achieved power:** the chance of detecting the planned effect (or showing non-inferiority at zero true difference) with the users actually observed, from the observed variances. Not shown for secondary metrics.
- Confidence level follows the plan: 1 minus alpha for two-sided tests, 1 minus twice alpha for one-sided tests (the interval that matches the test); stored on each result.
- Final results are saved to `results` (kind final), one set of rows per run; re-running keeps the history and the latest run is current. The experiment moves to Analyzed. Live monitoring rows stay (kind interim) and are shown next to the final results.
- **Decision log:** once Analyzed, the owner or an admin records ship, no ship, iterate or inconclusive with a reason; the experiment becomes Decided and the decision shows on the portfolio.
- **Validation:** the tests match scipy and statsmodels; simulated A/A experiments reject about 5% of the time; confidence intervals cover the true difference about 95% of the time; power matches the simulated hit rate; the non-inferiority test has the right error rate at the margin. On BigQuery, RUN recovers the simulator's exact planted effects and does not call an A/A experiment a winner.
- SRM check built (`src/p2/stats/srm.py`): chi-square on the control and variant user counts against 50/50, strict threshold p < 0.001; the results page shows a card at the top ("The test is balanced" or a warning with the seen split). Not built yet: CUPED, Bayesian, multiple-comparison correction, per-segment results.

## 10. Simulator (built in R1)

- Plays the company's systems: generates the shared user universe and each product's activity, and lands the 9 raw source tables. Details and planted mess: DATA_SOURCES.md section 10.
- Per product, a small generative model with closed-form true metric means, so the true lift of any metric is exact. Effects are multipliers on model parameters.
- Metric, dimension, and filter queries written by owners run against the staging views; the equivalence tests prove the pipeline recovers the exact truth despite the planted mess.
- The earlier Checkout model (calibrated once from a public dataset for order values, orders per buyer, and refund rate) is reused and reshaped into `checkout_orders` and `checkout_events`. Email and Onboarding models use round numbers.

## 11. UI

| Page | Purpose | Roles |
|---|---|---|
| Experiment Results | A search box for the Experiment ID. Above two tabs: name, hypothesis, product, owner, status, "Day 12 of 14", the Edit button (opens the experiment in the Experiment Catalog) and Refresh monitoring or Run final analysis or Run. Tab Tables: Segment (Everyone or a segment such as Acquisition channel) and Filter (No filter or a filter) dropdowns, which list only what is in the plan and can be combined. Everyone with no filter shows the main table (metric, role, type, control, variant, difference, lift, users; after the final analysis also p-value, interval, verdict, power). Picking a segment shows one titled table for each of its values (four values, four tables); picking a filter shows the users who pass it. Tab Charts: a metric picker (primary by default, names show the role) above control and variant over time and difference over time (a line, plus a shaded 95% range after the final analysis). There is no simulator on this page; the demo data comes from the dev-only September button on Add Experiment. The run log, history and decision form are not shown | everyone |
| Add Experiment | Create only. It starts with the Experiment ID alone: an existing id says "already exists, open it in the Experiment Catalog" and hides the form; an id missing from the assignment log stops with "does not exist". Then one page without tabs: Experiment (product, owner shown read-only, name, launch date filled from the first assignment and editable, runtime, optional end date, optional hypothesis), Metrics (primary, guardrails, secondary, with the statistical inputs and a one-line hover tip on every field), Audience (optional segments and filters), then Save and Save and run. After Save the form starts blank for the next experiment | everyone |
| Experiment Catalog (the default page) | Two sub-tabs. **Experiment catalog:** every experiment, newest first, one row each with ID and name, product and owner, status, primary metric, launch and end date, verdict (the final verdict on the primary metric; blank while live) and a progress bar with a sentence ("Live, day 12 of 14", "Ended, 14 days", "Starts in 3 days"), plus Edit and Open results (jumps to the Experiments page). One filter: Experiment ID. Edit opens the shared experiment form in place, with everything but the ID editable and a Back to list button. **Metric catalog:** one simple table of the available (Certified) metrics only, with the columns Metric, Product, Added by and Definition (the description), and a Select metric search box above it. Segments and filters are not listed. There is nothing to add, review or version in the tool; metrics are added in BigQuery | everyone |

- Navigation is a bar at the top (Experiment Catalog, Experiment Results, Add Experiment) so charts and tables get the full width; there is no sidebar and no "signed in" label. Everything else is forms, tables, and buttons.

## 12. Service layer (built in R2)

- Functions take `actor` first and check permission before acting: `catalog.propose_metric`, `catalog.submit_for_review`, `catalog.certify`, `experiments.create`, `experiments.save_design`, `run_analysis`, `record_decision`, and so on.
- Each write appends to `audit_log`. Reads used for lists are cached in the UI.

## 13. Testing

- Offline (seconds): the statistics engine (against scipy and statsmodels, plus simulation of error rates and coverage), plan validation, registry validation, permissions, service logic with `MemoryStore`, design form, event and simulator generators.
- BigQuery (`-m bq`, minutes, throwaway datasets): pipeline equivalence with the simulator truth, hand-built edge cases (day 0, day 6, day 7, duplicates, no rows), quality gates, idempotent rebuild, SQL safety checks (DDL rejected, unregistered table rejected, cost cap), `BigQueryStore` contract tests shared with `MemoryStore`.
- App tests use Streamlit `AppTest`; the Owner is picked in the form.
- Validation (offline and on BigQuery): A/A false positive rate near 5%, interval coverage near 95%, planted lifts recovered, guardrail verdicts match the truth.

## 14. Out of scope for now

- Real authentication, notifications, scheduled jobs (deploy phase), Bayesian analysis, SRM, CUPED, multiple-comparison correction, ratio metrics, per-segment statistical results, sequential testing, multi-variant tests, numeric dimensions, sample-size and power planning inside the tool, FastAPI, the chatbot.
- Dimensions and filters, and the statistics per segment and filter view, are built.

## 15. What exists today (as built, 2026-09-30) and what changes

| Built | Status |
|---|---|
| Power calculation (sample size) | Removed in R5: the data scientist does this outside the tool. The baseline-from-warehouse code is removed with it |
| `compute_metric` and the `Registry` / `MetricDef` classes | Kept; the registry is now built from the catalog's Certified and Deprecated metric versions. The YAML file is gone; its 15 metrics are seeded into the catalog with real SQL |
| SQLite app database and repo | Replaced in R2: `Store` interface, `MemoryStore`, `BigQueryStore`, service layer with permissions and audit. SQLite code and the local database file are deleted |
| Single raw events table, event-level SQL templates, baselines from first visit | Retired. The 9 raw tables and staging views are built (R1); the step-based pipeline comes in R3 |
| Simulator (Checkout only, one events table) | Rebuilt in R1: shared universe, three products, planted mess, exact truth |
| Streamlit pages | Built, with the navigation on top: Experiment Catalog (default; with edit and a Metric catalog sub-tab), Experiment Results (search, Tables and Charts tabs), Add Experiment (create). There is no Admin page. The Experiment Catalog is the portfolio view (R6) |
| Tests | Kept and extended; see the decisions log for counts |
