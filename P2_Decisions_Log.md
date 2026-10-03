# P2 Decisions Log

Entries are chronological and later entries supersede earlier ones. Superseded by the team-platform re-plan (2026-09-29): the SQLite storage, YAML-registry-as-source-of-truth, event-level pipeline, FastAPI and Postgres assumptions in the earlier entries. Current truth: [PLAN.md](PLAN.md), [TECH_SPEC.md](TECH_SPEC.md), [DATA_SOURCES.md](DATA_SOURCES.md).

## 2026-09-29: Basic platform first

- Build a basic single-user Streamlit platform with frequentist stats only. Bayesian, SRM, and the rest come after.
- Metric registry: one versioned YAML file defines every metric. Dropdowns per role read from it. Experiments pin metric version.
- Roles: one primary, zero or more guardrails, optional secondary. Power inputs only for primary and guardrails.
- Guardrails use non-inferiority against a max acceptable degradation margin.
- Sample size is the max across primary and guardrails, bottleneck named in the UI.
- Fixed-horizon analysis only (no peeking) in v1.
- Storage: SQLite behind an interface, BigQuery at deploy time.
- Logic in a plain Python package, Streamlit is a thin UI.
- Starter metrics: conversion_rate, revenue_per_user, refund_rate, unsubscribe_rate, add_to_cart_rate, orders_per_user.

### Assumptions taken from "approved" (confirm or change)

- SQLite for v1 (not BigQuery from day one).
- MDE selectable as relative or absolute.
- Primary test two-sided by default with a one-sided option. Guardrails one-sided by nature of non-inferiority.
- Exactly one primary metric.

## 2026-09-29: Phase 0 done

- Layout mirrors P1: `src/p2/{registry,power,stats,storage,simulator}`, `app/` for Streamlit, `tests/`, `data/` (gitignored, holds `p2.db`).
- Python 3.14 venv, editable install, `requirements.txt` is a full pin of the installed set (numpy, pandas, scipy, statsmodels, streamlit, PyYAML, pydantic, pytest).
- SQLite schema in `src/p2/storage/schema.sql` (5 tables from the tech spec) with CHECK and foreign key constraints; `db.py` exposes `connect` and `init_db` (idempotent).
- No git repo yet, per the user. A `.gitignore` file was created in the folder (data, db files, venv, env files) so it is ready when the repo is made.
- BigQuery datasets from the master plan are dropped for v1 since storage is SQLite.

## 2026-09-29: Phase 1 done

- Registry lives in `src/p2/registry/metrics.yaml`, validated by pydantic (`MetricDef`). Multiple versions of one metric_id can coexist; `get(id)` returns the latest, `get(id, version)` returns a pinned one.
- `for_role(role)` feeds the UI dropdowns. `validate_selection` enforces allowed roles and no metric in two roles.
- `compute_metric` is the only metric calculation: returns n, mean, sample std (ddof=1) from user-level rows. It rejects nulls, non-0/1 binary values, negative continuous values, and groups under 2 users.
- Dropped `filters` from the metric definition for v1 (nothing needs them yet). Add as a new field with a version bump if needed.
- Aggregation is `mean` only. Ratio metrics remain deferred.

## 2026-09-29: Phase 2 done

- Sample size is closed-form normal approximation in `src/p2/power/sample_size.py`, validated to match statsmodels `NormalIndPower` exactly (ceil) for binary, continuous, one-sided and non-inferiority cases.
- Primary MDE is the improvement in the metric's good direction (binary variant rate = baseline plus or minus MDE by `good_direction`).
- Guardrail: one-sided non-inferiority, true difference assumed zero, so variance is baseline variance in both arms. Two-sided guardrails are rejected.
- Uneven traffic split supported. Schema column renamed `required_n_per_arm` to `required_n_total` because arms can differ in size.
- `design_experiment` requires exactly one primary, no duplicate metrics; experiment size = largest total across metrics, bottleneck named, days = ceil(total / daily traffic).
- `achieved_power` is built now for reuse in the Phase 5 analysis.
- OPEN for Phase 3: `historic_metrics` stores daily aggregates (n, mean, std), but baselines must come from the same `compute_metric` as analysis. Decide whether to store user-level pre-period rows instead, or pool the daily aggregates with an exact pooled-std formula.

## 2026-09-29: Phase 3 done

- Decisions taken on the user's behalf (they said go without answering; both were my recommendations): historic data is user-level, and baselines are calibrated once from thelook.
- Calibration (one read-only BigQuery query, 0.05 GB, calendar year 2025): mean order value 86.33, sd 94.18, 1.171 orders per buyer, 11.7% of buyers had a return. thelook cannot calibrate the funnel (about 99.9% of event users purchase), so p(add to cart) = 10%, p(convert | cart) = 35% (conversion 3.5%), and unsubscribe 0.5% are round numbers. Results live in `src/p2/simulator/baselines.yaml`.
- `historic_metrics` replaced by `historic_users` (user-level), so design-time baselines and analysis share `compute_metric`. New `ground_truth` table.
- Simulator effects are multipliers on generative parameters, so metric-level truth is exact and can be compound (raising conversion also raises revenue per user).
- Repository layer `storage/repo.py` owns all SQL: create/list/get experiments, `save_design` (validates selection, sizes the experiment, pins versions, sets Designed), data load (once, after design), ground truth, historic baseline. Design locks once data is loaded; status only moves forward.
- `python -m p2.simulator` seeds 200k historic users into `data/p2.db` (gitignored). Not built: SRM, novelty, peeking scenarios (deferred with those features).

## 2026-09-29: Phase 4 done

- Streamlit app runs with `.venv/bin/streamlit run app/streamlit_app.py`. Pages: Design (4 sections, live result, Save) and Experiments (list and saved design). Data loading and analysis pages come in Phase 5.
- Connection pattern: UI calls `p2.storage.repo` only; fresh SQLite connection per rerun (threads), `P2_DB_PATH` env var for the location, cached registry and baselines.
- Form logic (percent to fraction, plain-language effect description) lives in `p2/design_form.py` so it is unit-tested without Streamlit. UI tested with Streamlit `AppTest` (renders, dropdown contents, save writes rows, blank hypothesis blocked).
- Re-saving an existing experiment ID in Draft or Designed updates it; once data is loaded the design is locked.
- Absolute effect defaults to 10% of the baseline; relative primary default 10%, guardrail margin default 25%.
- Manually checked in the browser against the seeded database: baseline prefills 3.53%, 90,024 users and 19 days for the default primary.

## 2026-09-29: Phase 4b, warehouse and SQL pipeline

- User direction: mimic the real world across the whole project, not build for its own sake. Consequence: analytics data moves out of SQLite into BigQuery, and metrics are derived by SQL from raw events.
- Two stores: BigQuery for raw and analytics data (datasets `p2_<env>_raw`, `p2_<env>_analytics`, project `master-chariot-413216`, US), SQLite for app state only. This reverses the earlier "SQLite for v1" storage choice for data; SQLite stays for config and results.
- Removed from SQLite: `historic_users`, `experiment_data`. The repo functions for them are gone. `ground_truth` stays in SQLite because real life has no ground truth; it is a validation artifact.
- Registry gets `sql_expression` per metric (validated: no `;`, no `--`, must use event alias `e`). `source_column` is now the output column name of the SQL. Metric versions stayed at 1 because nothing was pinned in real use yet.
- One SQL template serves experiments and baselines; only the cohort differs. Experiment anchor = assigned_at; baseline anchor = first visit in the cohort window, restricted so the 7-day window has closed by the as-of date. Attribution window is 7 days.
- Idempotency via `CREATE OR REPLACE TABLE` per experiment (`exp_<id>_user_metrics`). Experiment IDs are now lowercase letters, digits and hyphens so they map one-to-one onto table names.
- Simulator now plays logging systems: expands user-level truth into raw events. Planted mess: 2% duplicate events, 5% of users with a purchase before the anchor, 3% with a purchase after the window. Revenue is split across a user's orders; a refund event follows the first order. Truth stays exact only if the SQL handles all of it, and the equivalence test proves it does.
- Data-quality gates (stop the build): no assignments, one arm only, a user in two arms, attribution window still open, output row count differs from assigned users. SRM stays deferred.
- Bug found by testing: pandas 3 defaults datetimes to microseconds, so integer nanosecond math put events in 1970. Fixed by converting to nanoseconds explicitly.
- Test strategy: SQL is tested against a throwaway BigQuery dataset created and dropped per session (`pytest -m bq`, about 3 minutes, needs credentials, skipped without them); everything else is offline (`-m "not bq"`, seconds).
- Dev warehouse seeded: 200k historic users, about 275k events in `p2_dev_raw.events`. Load and query cost is far inside the free tier; the $5 budget alert still applies.
- Not yet done (real-world items for later): scheduled daily pipeline runs, dev/prod promotion, a metrics-per-day incremental table, CI running the BigQuery tests.

## 2026-09-29: Re-plan as a team platform (v0.2)

- Trigger: the user said the single-user Streamlit app is not scalable and hides everything in python and git. The target is an internal tool for about 15 data scientists, each on their own product with their own metrics, usable without git or python. Reference for the pain to remove: a guided engineering workflow for adding metrics (edit SQL files in a warehouse project, run schema migrations, open a PR, maintain a separate spreadsheet catalog). Concepts only were taken; nothing from that employer material is copied.
- What is not scalable in v0.1: single-file SQLite, YAML registry in git, no users or ownership, pipeline inside a button click, one events table, no portfolio view.
- What was kept as a strength: experiment tables are built from only the selected metrics, so adding a metric never needs a schema migration.
- Decisions (all confirmed by the user in chat):
  - State in BigQuery only. No Postgres (free tier, one system, mirrors how the team already works). Repository interface kept so Postgres can replace it. FastAPI deferred.
  - Simulated users with an "acting as" switch; real sign-in at deploy.
  - Three products (Checkout, Email, Onboarding), one owner each to start plus an admin.
  - Custom SQL only, no no-code builder, because the team is comfortable with SQL.
  - User-level randomization and analysis only, to keep it simple.
  - Metric contract is **user-day** (one row per user per day) rather than event-level. Why: it matches the team's existing habit and makes daily result tracking natural. Cost: authors own de-duplication and late data, and the window is whole days. Mitigation: automated checks and a sample preview before certification.
  - Roadmap order R1 to R7 chosen by the assistant and accepted: foundation, data sources and products, metric catalog, experiment wizard, jobs and analysis, portfolio dashboard, deploy.
- Consequence for earlier work: event-level SQL templates, SQLite storage, and the YAML registry will be replaced. Power calc, stats plans, metric calculation, simulator core, and most tests carry over.
- Consequence for other docs: the master plan's FastAPI service and chatbot sequencing is superseded in part; the chatbot phase moves after R7.

## 2026-09-29: Data sources finalized (DATA_SOURCES.md v1.0)

- User direction: finalize all data sources and schemas first, to avoid rework, mimicking a real experimentation platform. The user asked that dimensions and filters be part of the platform and that no single query become complex or messy.
- What was learned from the reference platform (concepts only): it keeps assignments, experiment config, upstream business facts, a reduced audience with filter flags, customer dimensions at entry, per-area metric tables, a final joined table, a spreadsheet registry, and an orchestrated pipeline with dev and prod schemas. Its weak point is large shared files (one wide SQL file per metric area, one audience file with dynamic SQL, one dimension file) where every addition lands in the same place.
- Confirmed by the user (all five proposals):
  1. One shared user universe, with `users` and a slowly changing `plan_history`.
  2. Nine raw tables: shared `exp_assignments`, `users`, `plan_history`; Checkout `checkout_events`, `checkout_orders`; Email `email_sends`, `email_events`; Onboarding `onboarding_events`, `payments`.
  3. Metrics, dimensions, and filters are all catalog items (one lifecycle, one set of checks) with three contracts: metric `(user_id, metric_date, value)`, dimension `(user_id, effective_date, value)`, filter `(user_id, effective_date, 0 or 1)`. Dimensions and filters resolve as of the assignment date so they describe the user at entry.
  4. The platform serves generated, de-duplicated staging views; authors no longer repeat de-duplication. This refines the earlier rule that authors own de-duplication; the automated query checks stay.
  5. Starter dimensions: plan tier at entry, region, acquisition channel, tenure bucket, customer type. Starter filters: paid at entry, existing user, has a prior order, region is US.
- Scalability rule adopted: one small query per unit, one table per pipeline step, and the wide per-experiment table is generated by code. Limits per experiment: 30 metrics, 10 dimensions, 5 filters.
- Datasets per environment grow from three to four: raw, staging, analytics, app.
- Known limit recorded: whole-day granularity on entry state can leak a same-day change into entry state; revisit with timestamps if needed.
- Roadmap reordered: data sources and simulator become R1 (they do not depend on app state); shared foundation becomes R2; R3 is now the catalog for all three kinds. Tech spec moved to v0.3 and points to DATA_SOURCES.md for anything about data.
- Existing single events table, event-level templates, and Checkout-only simulator will be rebuilt in R1; the Checkout calibration numbers are reused.

## 2026-09-29: R1 done, data sources and simulator

- Built: `sources.yaml` source registry (one place per table: columns, partition, cluster, dedupe key and order) that generates both the raw DDL and the staging views; four datasets per environment; shared user universe; per-product generative models (Checkout, Email, Onboarding) with closed-form truth; planted mess; landing with replace-by-prefix so re-running a simulation is idempotent.
- Whole-day window alignment (bug found by design review, then enforced by tests): the user-day contract counts the anchor's date through six days later, but the first simulator cut generated activity up to 7 x 24 hours after the anchor, which can fall on day 7 for a late-day anchor. Models now cap each user's offsets at the end of day 6. A test puts 2,000 anchors at 23:50 to prove it.
- Bug found by the BigQuery equivalence test: historic (baseline-period) activity for users near the end of the period included planted post-window activity that fell inside the experiment window, changing those users' experiment outcomes. Rule adopted: all historic activity must finish before experiments start (anchors 2025-09-01 to 2025-12-09; baselines use as-of 2025-12-16).
- Bug found by the first landing run: pandas 3 nanosecond timestamps cannot be cast to BigQuery microseconds without loss; landing now floors to microseconds.
- Plan mix changed from "70/20/10 at signup" to: everyone starts free, upgrades happen through payments (so plan history, payments, and onboarding stay consistent). Result at year end: about 77% free, 16% standard, 5% premium, 2% unknown.
- Onboarding experiments create new users (new signups) instead of sampling existing ones.
- Proof of the exit criterion: for one simulated experiment per product, author-style user-day queries over the staging views with the whole-day window reproduce the clean per-user outcomes exactly (conversion, revenue, orders, refund, add to cart; open, click, unsubscribe, bounce, emails; profile, invite, activation, paid, revenue), despite duplicates, superseded order versions, and out-of-window activity. Raw tables provably contain duplicates; staging keys are unique.
- Retired in R1 (rebuilt later): event-level pipeline and SQL templates, the Checkout-only generator, the single events table, and the old pipeline and app tests. The Design page now uses simulator-default baselines until the warehouse pipeline returns in R3; the Experiments page lists experiments only. The earlier dev warehouse datasets were dropped and reseeded.
- Test suite at the end of R1: 103 offline tests plus 15 BigQuery tests (`pytest -m bq`, about 6 minutes against throwaway datasets).

## 2026-09-30: Repository alignment audit (before R2)

- Trigger: the user asked to check every file against the recent changes and delete or modify anything misaligned.
- Method: listed every file, ran pyflakes over source, app and tests, grepped the docs for retired terms, and checked BigQuery for leftover datasets.
- Fixed:
  - `tests/conftest.py` still had a `seeded_wh` fixture calling a function signature that R1 removed; deleted the fixture.
  - The metric registry still carried event-level fields (`source_column`, `sql_expression`) from the retired pipeline, plus an unused import. Removed them, added `product_id`, scoped dropdowns and selection checks by product, and made `compute_metric` read the column named by `metric_id` (the final table's shape per DATA_SOURCES.md).
  - The provisional metric list grew from 6 Checkout-flavoured metrics to the 15 candidates in DATA_SOURCES.md section 9 (5 per product), so every product's owner can design an experiment. Metric ids must be globally unique, which exposed a collision: Checkout and Onboarding both produced `revenue_per_user`. Onboarding's is now `paid_revenue_per_user` (simulator models and tests updated).
  - Design page baselines now cover all 15 metrics (values from the simulator models); the page is fixed to Checkout until R2 adds product selection.
  - The app's users table is renamed `team_members` in the spec so it is not confused with the company `users` source table.
  - Stale wording fixed in PLAN.md, the decisions log header, the master plan (superseded sections marked), the Projects README (P2 plan version), and `pyproject.toml` (description).
  - Unused imports removed from `registry.py` and `test_landing.py`.
- Checked and already aligned: DATA_SOURCES.md, `sources.yaml`, the simulator modules, the warehouse wrapper, power calculation, design form logic. BigQuery holds only the four `p2_dev_*` datasets; no leftover test or scratch datasets.
- Kept on purpose until R2 replaces them: `src/p2/storage/` (SQLite), `tests/test_storage.py`, `tests/test_repo.py`, and the local `data/p2.db` (its experiment row points at warehouse tables that no longer exist). `src/p2/stats/` is an empty package reserved for R5.
- After the audit: 104 offline tests pass, pyflakes is clean.

## 2026-09-30: R2 done, shared foundation

- Built: a `Store` interface (insert, insert_missing, insert_many, get, select by equality, update with optimistic check, delete with a required filter) with two implementations, `MemoryStore` and `BigQueryStore`, that pass one shared contract test suite (24 tests, each run against both). Ten application tables in the `app` dataset (`team_members`, `products`, `data_sources`, `experiments`, `experiment_items`, `results`, `job_runs`, `job_steps`, `audit_log`, `sim_ground_truth`); the catalog table waits for R3.
- Service layer (`p2.services.Platform`): every call takes an Actor, checks permissions, validates, writes, and appends to the audit log. Roles: viewer (read only), experimenter and metric owner (create in their own product, change their own experiments), admin (everything, including Admin page, sources, audit). Errors are PlatformError subclasses the pages show to people.
- Simulated team seeded idempotently: Priya (Checkout owner), Marcus (Email owner), Sofia (Onboarding owner), a platform admin, and a read-only viewer. The viewer is an addition to the agreed four so the read-only experience can be shown. Names are placeholders. The nine sources from `sources.yaml` are registered in the app, and each product carries a population query for baselines (used in R3).
- UI: sidebar "Acting as" switch; Design page fixed to the actor's product (admin picks), metric dropdowns scoped to the product, read-only message for the viewer, save goes through the service layer; Experiments page with product, owner, and status filters; Admin page (team, products with population queries, sources with schemas, audit log), admin only. Read lists are cached for 30 seconds and cleared after writes.
- SQLite removed: `src/p2/storage/`, its two test files, the `P2_DB_PATH` setting, and the local database file (deleted at the user's request after a scratch backup). The demo experiment in it is gone.
- Lesson from the first live run: BigQuery does not enforce unique keys, so two server processes bootstrapping together, using a pre-check followed by a load job, inserted all nine sources twice. Fixed by making every insert a MERGE, and bulk seeding one MERGE over an array of structs (`insert_missing`), which is atomic. Verified on BigQuery: seeding twice inserts nothing the second time. Duplicated dev rows were cleared and reseeded.
- Another small lesson: NULL ordering differs by engine. The contract now says NULLs sort first ascending and last descending, as BigQuery does, and the memory store matches.
- Startup cost: the app used to create all warehouse tables and views on start (about 80 seconds the first time). It now creates only its own dataset and any missing tables, and seeding checks once.
- Not done in R2 (by design): real sign-in, catalog tables, the experiment wizard, results. The Experiments page still says data loading and results are being rebuilt.
- Tests at the end of R2: 151 offline and 42 against BigQuery. Offline runs in about 4 seconds; the BigQuery tests take roughly 8 minutes in total against throwaway datasets.

## 2026-09-30: R3 done, catalog and step-based pipeline

- Scope as planned: metrics, dimensions and filters are all catalog items (`catalog_items`, one lifecycle Draft, In review, Certified, Deprecated), governed in the UI; the pipeline is a chain of small one-table steps; the wide table is generated.
- Final metric list: the user said go without picking, so the 15 provisional metrics (DATA_SOURCES.md section 9) were seeded as Certified, together with the agreed 5 dimensions and 4 filters. All 24 have real SQL over the staging views. Change them through the catalog; the YAML registry file is deleted.
- Seed SQL design notes: metrics return one row per user per day and use the max or sum aggregation; dimensions and filters return history rows. `tenure_bucket` shows the pattern for a state that changes with time (one row per bucket boundary). State derived from events (customer type, has a prior order, existing user) uses the day after the event as `effective_date`, so same-day activity after assignment can never leak into entry state.
- Automated checks run against the warehouse: a dry run (statement type, referenced tables, cost, columns and types), then one real run wrapped in a statistics query. Two findings from building them: (1) the dry run reports the raw tables a view reads, not the view, so the allowlist compares raw table names; it cannot tell a staging view from its raw table, so direct table references are rejected by pattern, which is what keeps de-duplication from being bypassed; (2) the mean of a user-day flag is always 1, so the planned "binary metric that never varies" warning was meaningless and was dropped.
- Submission needs a passing report whose fingerprint matches the current item. Certifier must be the product's metric owner or an admin and never the author (an admin-authored shared item cannot be certified by that admin); certifying a version deprecates the older certified ones; experiments keep the versions they pinned.
- `Registry` and `MetricDef` remain for the power calculation, but are now built from the catalog. `MetricDef.aggregation` ("mean") was replaced by `window_aggregation` (max or sum), which is a different concept.
- Pipeline findings while testing on BigQuery: `nulls` is a reserved word (alias renamed); the dual-arm gate must run on the raw log before the arm-count gate, because the de-duplicated view silently picks one arm for a user logged in both; the baselines cohort and metric steps reuse the experiment code with a different cohort source.
- New metric id collisions are prevented across kinds and products because ids become column names; reserved column names are rejected.
- Service additions: propose, update draft, run checks, submit, certify, reject, deprecate, new version, list and search, item usage, `run_pipeline` (with job and step logging, including failed runs), `simulate_data`, `baselines`. `Platform` now takes `checker` and `runner`; both are optional so the service layer is fully testable offline with `p2.testing` fakes, which the app also uses when `P2_STORE=memory`.
- UI: new Catalog page (Browse, Propose with My drafts, Review queue); Design page gained Filters and Dimensions and warehouse baselines (falling back to simulator defaults with the reason shown); Experiments page gained simulate, run pipeline, step table, cohort and audience counts, per-arm means, and users by dimension.
- Not done (by design): statistics (R5), the wizard polish and portfolio (R4 and R6), scheduled runs and notifications (R7), numeric dimensions.
- Live check on the dev warehouse: the app seeds the 24 catalog items on first start (15 metrics, 5 dimensions, 4 filters, all Certified), and the Design page prefilled the Checkout conversion baseline at 3.693% from the warehouse pipeline, against a model expectation of about 3.74%.
- Tests at the end of R3: 215 offline and 74 against BigQuery (landing 15, store 24, service on BigQuery 3, catalog and pipeline 32). The catalog and pipeline file alone takes about 16 minutes because it builds a full test warehouse first.
- BigQuery test highlights: every one of the 24 seed items passes the automated checks on real data; 17 kinds of bad query are rejected with the specific reason; experiments for all three products build a final table whose metrics equal the simulator's clean outcomes despite the planted mess, whose dimensions equal the state at entry (plan, customer type, tenure bucket, region), whose audience equals exactly the users passing the filter, and whose rebuild is identical; gates fire for no assignments, one arm, a user in two arms, and an open window; the baselines land near the model values.

## 2026-09-30: SCHEMA.md, a generated data dictionary

- The user asked where the data sources and schemas live and for a proper schema file with a data dictionary and a one-line context per source.
- `SCHEMA.md` is generated by `python -m p2.schema_doc` from three places: `sources.yaml` (raw tables, now with a description on every column), `store/schema.py` plus the new `store/docs.py` (application tables, a one-line summary per table and a description per column), and the query contracts. It covers the four datasets, 9 raw tables, 9 staging views, the generated analytics tables, the catalog contracts, 11 application tables, key relationships, and conventions including the planted mess.
- Tests (`tests/test_schema_doc.py`) fail if the file is stale, any table or column lacks a description, or documented columns differ from the real schema, so the dictionary cannot drift from the code. No em dashes in generated text.
- `Column` in the source registry gained an optional `description`; the registered sources in the app now carry it in their schema JSON (existing seeded rows keep the older JSON until re-registered).

## 2026-09-30: R4 done, wizard and registry

- Wizard: the long Design page became a four-step wizard (Basics, Audience, Metrics, Review and save) built from tabs. Tabs were chosen over Back and Next buttons because Streamlit drops the state of widgets that are not drawn, which would lose the power inputs on other steps; tabs keep every step's values alive. A readiness checklist on the last step says what is missing, and Save is disabled until the name, hypothesis, and inputs are valid. Strict step-by-step gating can come after real user feedback.
- Registry: an experiment can be registered as a draft from step 1 so it is in the portfolio immediately. A Start from box loads any of your experiments that has no data (Draft or Designed) back into the form, including pinned metrics, power inputs, filters, dimensions and tags; admins can load anyone's. Saving re-pins every item to its latest certified version. Once data is loaded the design is locked and the experiment no longer appears in Start from. `to_ui` in `design_form.py` is the tested inverse of `to_power_input` used to refill the form.
- Tags: lowercase, trimmed, de-duplicated, at most 10 of up to 30 characters; editable from the wizard; usable as a portfolio filter.
- Portfolio (the Experiments page): status counts, filters by product, owner, status, tag and search, and one row per experiment with primary metric, users needed, estimated days and bottleneck. Built from two store reads (`list_portfolio`) however many experiments exist, because each BigQuery read costs about a second. Per-experiment history (`experiment_history`) is open to every team member; the full audit log stays admin only.
- Test-harness note: the app caches read lists for 30 seconds and clears the cache after its own writes; a test that writes through the service directly must clear the cache itself, which surfaced once.
- Exit check met in tests: each of the three product owners creates an experiment in their own product through the UI, and a read-only viewer sees all three in the portfolio with the right status counts, then filters them by product, owner, tag and search.
- Tests at the end of R4: 232 offline and 74 against BigQuery (the service-layer BigQuery tests were re-run after the changes).
- Not done (by design): the decision log and "needs a decision" view (R5), results and analysis (R5), portfolio dashboard metrics such as win rate and time to decision (R6).

## 2026-09-30: Direction change and R5, the plan form, RUN and the statistics

- **Trigger.** The user pointed out that sample size and power need a baseline, the baseline depends on where the users were bucketed, and that changes with every experiment, so a shared tool would be fragile for 15 to 20 data scientists. Proposal accepted: the data scientist calculates power and sample size the traditional way and types the values in; the tool records them and, on RUN, analyses the experiment.
- **Answers given to my five questions:** (1) measure each user until the end of the runtime; (2) no planned sample size field, follow what the data scientist entered; (3) drop the sample-size checker completely; (4) remove warehouse baselines from the wizard entirely; (5) my call on how to compute the statistics.
- **Input form (as requested):** experiment id, owner (from the signed-in person, not typed), product (dropdown), launch date, runtime in days, then for the primary metric and each guardrail alpha, power, baseline, standard deviation and MDE (the margin for a guardrail); secondary metrics picked only. Nothing is pre-filled except alpha 0.05 and power 0.80. The hypothesis became optional and tags stay optional. The experiment id is checked against the assignment log as the plan is written (soft: a plan may precede the data) and hard at RUN.
- **Removed:** the closed-form sample-size code and its tests, the experiment-size and bottleneck display, `daily_traffic` and `split_control`, `required_n_total`, the warehouse baselines (runner, service, cached UI call, default baseline table), the products' population queries, and the seeded baseline-period history in the simulator (`seed_history`; `python -m p2.simulator` now seeds only the universe). Old history rows still sit in the dev warehouse and are harmless.
- **Window.** Each user is measured from their assignment day through the experiment's last day (`launch_date + runtime_days - 1`, launch day counts as day one); only users assigned during the runtime are in the cohort. The SQL uses a query parameter for the last day instead of a fixed 7-day interval. New gates: not launched yet, still running (the last day must be over), and a message that names both date ranges when the log has assignments but none inside the runtime. The dual-arm check runs before the arm-count check and only over the runtime.
- **Statistics approach (my call).** Computed in BigQuery: one scan of the final table returns each arm's count, mean and sample variance per metric, and Python runs the tests on those summaries. Reason: pulling every user into pandas works for 10,000 users and breaks at 10 million. Tests: two-proportion z-test (pooled for the p-value, unpooled for the interval) for rates, Welch t-test for averages, one-sided non-inferiority for guardrails (Passed, Inconclusive, Failed), achieved power at the planned effect. Secondary metrics use alpha 0.05 and are labelled exploratory. Confidence level is 1 minus alpha for two-sided and 1 minus twice alpha for one-sided tests, stored on each result.
- **Results and decisions.** `results` gained `relative_lift` and `ci_level`; one set of rows per run keeps history, the latest run is current. `record_decision` (ship, no ship, iterate, inconclusive, with a required reason) needs status Analyzed and moves the experiment to Decided. The portfolio shows the decision and warns when experiments wait for one. Re-running RUN is allowed until a decision is recorded, and the plan stays locked once data exists (the guard against changing the plan after seeing results).
- **Schema migration.** `BigQueryStore.ensure` now migrates existing tables: it adds missing columns (as nullable, the only mode BigQuery allows) and drops NOT NULL on columns the code no longer writes. This was needed because removing `split_control` (a required column) from the code would otherwise break every insert into the existing dev table. A test creates an old-shaped table and proves the repair.
- **Validation.** 29 statistics tests: results equal statsmodels `proportions_ztest`, the Wald interval and scipy's Welch t-test with its interval; achieved power equals statsmodels power; simulated A/A experiments reject about 5% of the time (binary and continuous); intervals cover the true difference about 95% of the time; the hit rate matches the computed power; the non-inferiority test passes about 5% of the time at the margin and at the computed power at zero difference. On BigQuery, RUN recovers the simulator's exact planted effects for all three products, the warehouse summaries equal a pandas calculation, an A/A experiment comes out as No significant difference, and a hand-built case proves the window (an order on day 12 counts for a user assigned on day 1 but not for one assigned on day 13, nor after the last day).
- **Mistakes caught.** Importing the simulator's `__main__` module in a scratch check ran the universe seed against the dev warehouse; it is deterministic so the data is identical, but the check should have read the file instead. The BigQuery test modules had been sharing one environment; experiments that overlap on the same users contaminate each other (exactly as in real life), so each test module now gets its own environment.
- **Not built yet:** SRM check (first in line, it is cheap and catches broken bucketing), CUPED, Bayesian, multiple-comparison correction, per-segment results, the dashboard (R6), deploy (R7).
- **Tests at the end of R5:** 250 offline (about 8 seconds) and 80 against BigQuery (about 27 minutes in total, because every test module builds its own throwaway warehouse). The last two failures were test mistakes, not product bugs: a p-value threshold that assumed more users, and a hand-built window test that reused universe users who also had activity from other experiments in the same environment (it now uses synthetic user ids).
- **Live check on the dev warehouse.** `ensure()` migrated the existing dev tables in place. A demo experiment (`demo-banner`, Checkout, 20,000 simulated users, planted +40% lift on conversion) went from plan to RUN in one call: conversion 4.30% to 5.60% (+30%, interval +0.70 to +1.90 points, covering the planted effect), refund-rate guardrail Passed, add-to-cart correctly not significant, orders per user significant. It is left in dev, in status Analyzed, so the decision form can be tried.

## 2026-09-30: R5.1, the feedback round after trying the app

- **Trigger.** The user tried one experiment end to end and sent 13 points, then answered five questions (A to E). Everything below follows from those.
- **One page.** The four-tab wizard became one page with three sections (Experiment, Metrics, Audience) and two buttons at the bottom: Save and Save and run. Removed: the Start from box and its explanation, the tabs, Tags, Register as draft, and the Review and save step. Save keeps whatever is complete: a plan without a primary metric is stored as a Draft and the page lists what is missing. Editing now happens from an Edit plan button on the Experiments page.
- **Owner versus Acting as (decision A).** The user did not see why "Acting as" is needed. Explanation given: it is the stand-in for sign-in, so the app knows who is adding an experiment (the Owner) and who may edit it. It stays, but is hidden in a collapsed "Demo: switch user" box. The sidebar now says "Signed in as ...", and the form shows the Owner read-only, separate from the Experiment name. At deploy it is replaced by real sign-in. Offered to fix a single user if they prefer.
- **Live monitoring, no peeking (decision B).** From the first complete day, "Refresh monitoring" builds data through yesterday and shows counts, means, the difference and relative lift only. There are no p-values, intervals or verdicts until the runtime is over, because reading a fixed-horizon test early inflates false positives. After the last day, "Run final analysis" gives the verdict and unlocks the decision form. New status Running (Data loaded is gone). Refresh is manual for now; a schedule belongs to the deploy. A daily trend chart was asked about and not answered, so it is deferred.
- **Metric type drives the test (decision C).** The type (rate or continuous) is read from the catalog and shown read-only, not chosen again in the form, so a person cannot pick the wrong test. A rate uses the z-test and a continuous metric the Welch t-test. The Std dev field is always visible (it was hidden before, which confused the user), but disabled for rates with a hover tip.
- **All metrics everywhere (decision D).** Roles were removed from the catalog (`allowed_roles` is gone, with the product check). Every metric from every product is offered in primary, guardrail and secondary. A metric picked in one role is not offered in the other two. Reason: what is secondary in one test can be primary in another.
- **Tags removed (decision E).** Dropped from the form, the table, the portfolio filter and the code.
- **Hover tips.** Every field has a one-line `?` tip; long captions were removed.
- **Values survive page changes.** Streamlit drops a widget's value when you visit another page. The form keeps a plain snapshot of its values at the end of every run and restores it at the top. Checked in a real browser: a typed name was still there after visiting the Experiments page and coming back. Saving and running clears the snapshot so the next visit is blank.
- **Schema.** `experiments.tags` and `catalog_items.allowed_roles` are gone from the code (existing BigQuery columns are left in place and ignored). `results` gained `kind` (interim or final) and `through_date`.
- **Tests.** 262 offline. The BigQuery suite gained a monitoring test on real data (only complete days count, no test statistics, the final analysis is refused until the last day has passed) and was re-run in full.
- **Not done.** Daily trend chart, SRM check, CUPED, Bayesian analysis, multiple-comparison correction, the dashboard (R6) and the deploy (R7). The user wants to keep testing the app first.



## 2026-09-30: R5.2, the Experiments page becomes a results view

- **Trigger.** After trying the app the user said the portfolio overview (filters, status counts, table of all experiments) is not needed on the Experiments tab, and asked for a search by Experiment ID that shows the metric table first and then charts.
- **Search.** A text box with a Search button (Enter works too). Case and spaces are ignored. An unknown id says so and lists similar ids. Save and run opens the experiment it just ran. The overview is removed; a team-level view belongs to the R6 dashboard.
- **Metric table.** Metric, role, type (rate or continuous), control, variant, difference, lift and users. With the final analysis it adds p-value, confidence interval, verdict and power. While the experiment is running only the descriptive columns are filled and the page says no conclusions until the last day.
- **Chart 1, control and variant over time.** Needs a daily series, which did not exist, so every run now saves one (`daily_stats`, replaced on each run). For each day it is the average over users assigned so far, each measured from their own assignment day through that day, so the last day equals the final mean (checked on BigQuery, and by a test with hand-placed orders). The lines rise because each user's window grows. Cost: one extra query per metric per run (about 5 to 15 seconds each on the dev warehouse).
- **Chart 2, confidence intervals.** Variant minus control with its interval, one panel per metric because units differ, in table order. A dashed line shows the planned effect for the primary metric and the non-inferiority margin for guardrails. Only available after the final analysis, because that is when intervals exist.
- **ATE and ATT (answer, no chart).** The difference column is the average treatment effect (ATE): the effect of being assigned to the variant. ATT is the effect on users who actually received the treatment and differs only when some assigned users never saw it. We have no exposure data, so ATT would equal ATE and a second chart would add nothing. It becomes useful if an exposure log is ever added.
- **Loading feedback.** Reads from BigQuery now show a spinner with a message, saving and running show one, and the Streamlit running indicator is red and easy to see.
- **Operations note.** Streamlit caches imported helper modules, so changes to them need a server restart, not only a browser refresh.
- **Tests.** 269 offline. A BigQuery test covers the daily series on hand-placed orders.

## 2026-10-01: R5.3, feedback on the results view

- **Editing an existing experiment.** The Edit plan button only appears until an experiment is Running, because changing metrics or numbers after seeing data would let results shape the plan. The user was looking at an Analyzed experiment, where nothing was editable. Now: Edit plan (full form) while Draft or Designed; Edit details (name, hypothesis, end date) once it is locked; Copy as new experiment at any time, which starts a new experiment from the same plan with a new id and today as the launch date.
- **End date.** `experiments.end_date` is optional. The planned runtime stays as the data scientist's power calculation; the end date is when the experiment really stops. With none, the last day is launch plus runtime minus one. Monitoring (through yesterday) and the final analysis (through the end date) both use it, so an experiment can run past its planned runtime. It can be moved until the final analysis is run, then it is locked, because extending after the verdict would be a second look at the same test.
- **Why metrics other than the primary read 0.** The experiment (exp-002) mixed Checkout, Onboarding and Email metrics, but the simulator only generated Checkout activity, so the others had no events. Fixed in the simulator: it now also lands activity for the other products in the plan, for the same users and arms, with no planted effect, so those metrics read as A/A comparisons (non-zero, no difference). This only affects simulated data; real systems log every product.
- **Chart filter.** Charts are drawn only for the metrics picked in a filter above them, the primary metric by default. Both the over-time and interval charts follow it.
- **Segments.** A separate section with one table per dimension value (for example free and paid), for the dimension chosen in a selector; no charts at segment level. Each run groups the final table by dimension value in the warehouse, and Python runs the same tests per segment. They are exploratory: plain two-sided test at 0.05 per metric, no correction for how many segments exist, labelled as such on the page.
- **Hidden from the page.** The last run, run steps and history are still stored (job_runs, job_steps, audit_log) but no longer shown on the Experiments page. A failed run still shows its error.
- **Open point.** The user's feedback list ended at item 7 with no text; nothing was built for it.
- **Tests.** 286 offline; BigQuery tests cover a mixed-product experiment (the other products' metrics are non-zero and show no difference, the planted effect is found, segments partition the audience and their weighted means equal the totals).

## 2026-10-01: R5.4, the New experiment page finalized (page by page review)

- **Process.** The user asked to finalize one page at a time: describe the page, list questions with a recommendation, agree, then build. Short replies in easy English. Saved as a working rule.
- **Trust the data scientists, keep it simple.** The earlier idea to lock the primary metric, flag "plan changed after analysis" and mark results stale was dropped. About 20 people will use the tool, extra checks cost load time and feel like red tape. Everything except the Experiment ID can be edited in any status. The backend history already records changes.
- **Create only.** The page starts with just the Experiment ID. An existing id (any status) says "already exists, open it in the Experiment catalog" and hides the form. Editing moves to the Experiment catalog (page 3, not built yet; for now the Edit button on the Experiments page opens the form).
- **ID must be live.** If the id is not in the experiment tool's assignment log, the page stops with "does not exist". The log has no rows until traffic starts, so plans are entered once the experiment is live. No planning before launch.
- **Launch date** is filled from the first assignment day and stays editable, for example to skip testing days after launch.
- **Runtime and end date.** Both stay. Runtime is the planned days from the power calculation. The end date stops data loading: users assigned after it and activity after it are ignored, so the table and charts stop on that day even if it is later than the runtime. The tool cannot stop the split itself, because the experiment tool owns it. Extending an experiment that already has results shows a warning but never blocks.
- **Status follows the latest run.** Running means the latest run was a live refresh, Analyzed that it was the final analysis, Decided that a decision is recorded (it stays when re-running). The Experiments page shows whichever run is newest, so extending a finished experiment shows live numbers again.
- **Removed.** Copy as new experiment and Edit details (the new rules make them unnecessary).
- **Tests.** 289 offline.

## 2026-10-01: R5.5, the Experiments page finalized

- **Answers that settled it.** Search by exact ID, a "Day 12 of 14" header, manual Refresh and Run, no Planned effect column, remove the Plan and audience box, keep the dev simulator for the demo only. The decision form is removed for now (the backend keeps it). The button that re-ran the final analysis is just called Run.
- **Two tabs.** Putting the main table, segments, filters and charts on one page was cluttered, so the page has Tables and Charts tabs under a short header.
- **Tables tab.** A Segment dropdown (Everyone, or a segment such as Acquisition channel) and a Filter dropdown (No filter, or a filter). Both default to Everyone and No filter, which shows the main table. Picking a segment shows one titled table for each of its values, so a segment with four values shows four tables (first built as one dropdown entry per value, which the user corrected). They list only the segments and filters in the experiment's plan (instant, nothing computed on click) and can be combined. To add a new one, edit the experiment and Run.
- **Filters are views, not cuts.** Before, a filter removed users from the whole experiment. Now the main table always covers everyone and a filter shows the users who pass it. The final table carries each filter as a 0 or 1 column. This makes filters and segments work the same way and lets "paid users" sit next to "everyone".
- **Slices are stored per run.** Each run computes every segment value, filter and segment-and-filter pair in the warehouse (one query per combination, in parallel) and stores them (`segment_results`), so picking a dropdown is a lookup. Exploratory, plain two-sided test at 0.05, no correction for how many views exist; labelled as such. A view with fewer than two users in an arm shows "not enough users".
- **Charts tab.** A metric picker (primary by default, names include the role) and two charts per metric: control and variant over time, and difference over time. The difference chart is a line with a shaded 95% range, like the example the user shared. The range appears only after the final analysis, so nobody reads a verdict while the experiment runs; its last day equals the final confidence interval (tested on BigQuery). No charts per segment: that would multiply the daily calculation for every slice.
- **Interval chart removed.** The one-panel-per-metric chart was hard to read; the difference-over-time chart replaces it.
- **Read cache** raised from 30 to 120 seconds so changing a dropdown does not wait on BigQuery every time; writes still clear it.
- **Tests.** 296 offline, and the full BigQuery suite was re-run.

## 2026-10-01: R5.6, the Experiment catalog (page 3, first half)

- **Scope.** The Catalog page now has two sub-tabs. This round covers only the Experiment catalog; the Metric catalog (the old Catalog with Browse, Propose and Review queue) is kept as it was and reviewed next. Its name may change later.
- **List.** One row per experiment, newest first: ID and name, product and owner, status, primary metric, launch and end date, verdict, and a progress bar. The user suggested showing days run out of runtime; the bar text says whether the test is live: "Live, day 12 of 14", "Ended, 14 days", "Starts in 3 days". The total counts to the end date, so an extended test shows "day 20 of 30". Related columns share a cell (ID over name, product over owner, launch over end) so the list stays readable on narrower screens.
- **Verdict** is the final verdict on the primary metric, shown only when the latest run was the final analysis. While live, or after an extension, it says "In progress" (or "Not run yet" before any run).
- **One filter:** Experiment ID. Only the newest 50 are drawn, with a note to search for older ones.
- **Per row:** Edit (owner or admin only) and Open results (everyone).
- **Editing happens here.** The experiment form was moved into a shared module used by both New experiment (create) and the Experiment catalog (edit), so there is one form to maintain. Edit opens it in place with a Back to list button. The Edit button on the Experiments page now leads here. Opening a different experiment, or switching between new and edit, starts the form from that experiment's own values, so a half-typed new experiment is not mixed into an edit.
- **After saving a new experiment** the form clears, so the next one starts blank, and the message points to the catalog for later edits.
- **Backend.** `experiment_catalog` returns the portfolio rows plus the verdict from one read of the results table (primary rows only).
- **Tests.** 306 offline.

## 2026-10-01: R5.7, the Metric catalog simplified (page 3, second half)

- **What the user wanted.** Only the metrics that are available or added, no segments or filters, one table with the product, who added it and the definition, and a Select metric search box. Nothing else. The user did not know what the rest of the old page held (versions, SQL, check reports, propose and review tabs).
- **Built.** One table of Certified metrics (Metric, Product, Added by, Definition) and a Select metric dropdown with type-to-search; picking a metric shows only that row. Seeded metrics say "System (seeded)". A metric appears only once it is certified, so it is "available".
- **Decision made without asking, flagged to the user.** Adding and reviewing metrics still needs a home, because owners propose metrics with SQL and checks, and someone else certifies them. I did not delete that. It sits in one collapsed box, Manage metrics, shown only to people who can add metrics (viewers do not see it), keeping all previous behaviour. Moving it to the Admin page was rejected for now: the Admin page is admin only, and an author cannot certify their own item, so owners could not add metrics at all.
- **Open.** Whether "Definition" should be the plain-language description (built) or the SQL, and where Manage metrics should finally live (decided together with the Admin page review).
- **Tests.** 309 offline.

## 2026-10-01: R5.8, metrics are added in BigQuery, not in the tool

- **Decision.** The user asked why the Manage metrics box existed and answered that metric addition belongs in the data source: metrics are entered by working through BigQuery, the tool should not work backwards, and extra options make the tool heavier. The box is removed. The Catalog page now has only the Experiment catalog and the one-table Metric catalog.
- **How a metric is added now.** One INSERT into `p2_<env>_app.catalog_items` with status Certified; the template is in TECH_SPEC section 6. The tool reads Certified rows, so a new metric appears in the Metric catalog (with who added it) and in the experiment form's dropdowns. A BigQuery test inserts a metric exactly this way and checks the tool picks it up.
- **What this gives up.** The automated checks (safe SQL, no duplicate keys, sensible values) no longer run when a metric is added, and nobody else approves it. This follows the "trust the data scientists, keep it simple" rule; a bad query would show up as an error when an experiment runs. A small command that runs the same checks on a row already in BigQuery could be offered later without adding anything to the UI.
- **What stays.** The service-layer lifecycle (propose, check, review, certify, version) and its tests remain in the code but nothing in the UI uses them. They can be deleted later if they are never wanted.
- **Tests.** 305 offline.

## 2026-10-01: R5.9, the Admin page removed

- **Why.** The Admin page was read-only: it listed the team, products, registered data sources and the audit log. It had no controls, and neither data scientists nor PMs need it to add an experiment or track results. The user asked to keep only what those two groups use.
- **What stays.** The team, products, data sources and audit log remain in BigQuery tables (`team_members`, `products`, `data_sources`, `audit_log`); the data sources are also described in SCHEMA.md. Anyone who needs to look can query BigQuery. The audit log is still written by every change.
- **Result.** The sidebar has New experiment, Experiments and Catalog. All four pages of the page-by-page review are finished: New experiment, Experiments, Catalog (two sub-tabs) and Admin (removed).
- **Tests.** 305 offline; a test checks there is no Admin file or sidebar entry.

## 2026-10-01: R5.10, navigation on top, new page names, no sign-in

- **Navigation on top.** The page bar moved from the sidebar to the top (`st.navigation(position="top")`) so charts and tables get the full width. There is no sidebar any more.
- **Page names and order.** Experiment Catalog (the default page), Experiment Results, Add Experiment. The Catalog page keeps its two inner tabs (Experiment catalog, Metric catalog); the doubled wording is temporary until the Metric catalog's place is decided.
- **No "Signed in as", no demo user switch.** The user said again: trust the data scientists, do not make it red tape, and people will pick their own name when adding an experiment. So the tool has no identity and no permissions. Everyone can add, edit and run any experiment.
- **Owner is a field.** Add Experiment has an Owner dropdown (placeholder "Select your name") listing active team members except the read-only demo persona. Choosing an owner fills the Product (changeable). Both are required to save. Editing can change the owner and the product. The service gained an `owner_user_id` argument on create and edit and checks the owner is an active team member.
- **One acting identity.** Every UI action runs as the `admin` team member, so the service-layer permission checks pass. The checks stay in the service for scripts and tests, and the audit log records `admin` as the actor; the owner shown in the catalog is the field the person picked.
- **Removed behaviour.** "Only the owner or an admin can edit or run", the read-only role in the UI, and the per-row Edit visibility rules.
- **Tests.** 309 offline. The shell test checks the page names and order, that navigation is on top, and that no sidebar or sign-in text exists.

## 2026-10-01: R5.11, visual refresh, and the scalability probe

- **Look.** The user asked for organised, structured, colourful, professional pages with light borders on tables and cells, light green, red and yellow for positive, negative and flat lift, and less space under the top bar. Done with one stylesheet (`app/ui.py`) and a theme file (`.streamlit/config.toml`, so the app must be started from this folder). Colours are translucent so they work on the light and dark themes (both checked in the browser).
- **Spacing.** The top padding went from about 6rem to 3.6rem and headings are smaller, so each page starts right under the top bar.
- **Cards and chips.** The experiment header is a bordered card with coloured chips for status, progress (blue while live, yellow before launch, grey after) and product. The form sections and each catalog row are cards. Status and verdict are chips (Draft grey, Designed blue, Running yellow, Analyzed green, Decided teal; improvement and Passed green, decline and Failed red, no difference and inconclusive yellow).
- **Headline cards.** Under the header, four cards show the primary metric: variant value with its lift, control value, users, and the verdict.
- **Tables.** Lift and Difference are green when the variant moved the good way, red the bad way and yellow when it moved less than 0.5 percent; a lower-is-better metric flips (a refund rate that rose is red). Verdicts and p-values below 0.05 are tinted; control and variant columns have a soft grey and blue tint. Borders and header colour come from the theme. Live (interim) tables use the same colours for direction but carry no verdict.
- **Charts.** Blue is the variant, grey the control, so green and red keep their meaning for good and bad.
- **Scalability.** The user asked whether 50 to 100 experiments are fine, how much load time grows per experiment, and what the failure modes are. Measured on a throwaway BigQuery environment from 2 to 200 experiments: each experiment adds about 0.02 seconds to a cold page; the real cost is a fixed 10 to 15 seconds of sequential BigQuery reads when the cache is cold, and a Run takes 65 to 110 seconds. Findings, failure modes (write limits, many Runs at once, blocking Runs, cost, no locks, stale caches, no sign-in) and a proposed work order are in SCALABILITY.md. Nothing from that list is built yet.
- **Tests.** 309 offline.

## 2026-10-01: R5.12, polish round after the user's review of two screenshots

- **Asked.** The user shared the Experiment Results and Experiment Catalog pages and asked what could be better; after my list they said to do all of it and to make "no significant difference" grey.
- **Results page.** Cards are equal height and read control, variant, lift (with the difference), then the verdict with the user counts. A colour key sits above the table (green good, red bad, yellow flat under 0.5 percent, grey no clear difference; lower-is-better metrics flip). The table has Role and Type merged into one "Role / type" cell, the Verdict next to the Lift, p-values below 0.001 shown as "<0.001", and the CI without a label when it is a 95 percent interval (other levels are shown). While an experiment is live the Verdict, p-value and CI columns are left out instead of shown blank. Search and the Segment and Filter dropdowns are about a third of the width. The owner shows as a short name.
- **Colours.** No clear difference is grey (a clear answer); yellow now means a flat lift or an inconclusive guardrail.
- **Catalog.** A summary line (total, live, not started, ended) beside a narrower search; live experiments first, then not started, then ended, newest first inside each group. Progress bars are drawn by hand so each row has its own colour. The stray lines in the header are gone, owner names are short, Open results is the main button and Edit is a small pencil. The inner tabs are now Experiments and Metrics.
- **Tabs.** More space between tab labels and a bolder selected tab (Streamlit 1.64 renders tabs as `div[data-testid=stTab]`, not buttons, which is what the first stylesheet got wrong).
- **Tests.** 315 offline. One edit in this round accidentally cut the chart-helper test file short; it was restored from the earlier version and its tests were re-added and extended.

## 2026-10-01: scalability step 1, instant pages

- **Decision.** Keep the small application tables in memory (`SnapshotStore`, a wrapper around the BigQuery store) so a page is a memory read. The audit log, job steps and the simulator's ground truth are not copied (pages do not read them) and pass straight through.
- **Writes** go to BigQuery first and are then applied to the copy, so the person who saved sees it at once and BigQuery stays the source of truth. A reload that raced a write is thrown away so it cannot wipe the newer write. Writes are serialised so the copy sees them in BigQuery's order.
- **Refresh.** A background thread runs every 60 seconds (`P2_REFRESH_SECONDS`) but only if someone has read data since the last cycle. Before reading a table it asks BigQuery when the table last changed (table metadata, free) and skips tables that did not. Other people's changes therefore show within about a minute.
- **Cost the user asked about.** BigQuery bills at least 10 MB per table per query. Measured: the first load per server start is 14 queries, about 136 MB (about $0.001); 40 seconds of use with no changes sent 0 queries; one changed table sent 1 query, 10.5 MB (about $0.00007). Worst case about 1.2 TB a month if all tables changed every cycle all month (about $1 after the free 1 TiB, about $7 without it). Setting a BigQuery daily query quota on the project is the way to put a hard ceiling on all spend; the real spend risk remains Runs that scan large event tables (a later step).
- **Warehouse reads stay cached.** `assignment_check` scans the raw assignment log (billed by size), so it keeps its 2-minute cache.
- **Bug caught by running it for real.** The first version asked for the last-modified time with a table name that still had backticks, which BigQuery rejected; the offline tests could not see it because they use an in-memory stand-in. Found by running the pages against dev. The check is now also wrapped so a failure falls back to reading the table.
- **Tests.** 349 offline, including the whole store contract run against the snapshot store, plus BigQuery tests for the contract and for the last-modified check.

## 2026-10-01: scalability step 2, writes saved in memory first, sent to BigQuery in the background

- **What was measured first.** A single BigQuery write takes 2 to 3 seconds whatever it holds, and a script of eight writes took as long as eight separate writes, so batching statements into scripts would not have helped. Before the change a Save made the person wait 28.6 seconds and the write phase of a Run 41.9 seconds, using 41 BigQuery statements.
- **Decision.** Because the pages already keep the tables in memory, a write is applied to memory at once and the net effect is sent to BigQuery in the background (every 2 seconds, `P2_FLUSH_SECONDS`) as at most one delete and one merge per table (`write_batch`). Fifty updates to one row send one row. Rows created and deleted before a flush send nothing. Append-only tables the pages do not read (audit log, job steps) are buffered and sent as one statement; reading one of them flushes its buffer first.
- **Result.** The same Save and Run: 0 seconds of waiting, 9 write statements in the background (about 9 seconds), instead of 41 statements and about 70 seconds of waiting. Only one flush runs at a time per server, so a crowd cannot exceed BigQuery's limit on simultaneous writes.
- **What it costs.** BigQuery is no longer written to the instant a button is clicked: another server sees a change after the flush (about 2 seconds) plus its next refresh (up to a minute). If the app is killed in the middle of the 2-second window, that last change is lost; the app flushes on a normal exit. If BigQuery is unreachable, changes stay queued and are retried with growing pauses, and a warning shows once one has waited over 30 seconds.
- **Safe rules kept.** Duplicate ids, edit conflicts and missing rows are still checked immediately against the in-memory copy, so the person gets the same errors as before. A background refresh never overwrites changes that have not been sent yet. A failed flush never overwrites a newer change when it is retried.
- **Not changed.** Writes to the warehouse by Runs (the metric calculation) and the audit identity (still `admin`).
- **Tests.** 359 offline, including the contract against the snapshot store, tests for coalescing, retries, ordering and exit flushing, and BigQuery tests that count the statements sent.

## 2026-10-01: scalability step 3, Runs in the background

- **Decision.** A Run takes one to two minutes and used to hold the person's page. Run, Refresh and Save and run now queue the work (`RunManager`, `src/p2/services/runs.py`) and return at once; the Platform exposes `start_run`, `run_status` and `recover_interrupted_runs`. The older `refresh_monitor` and `run_analysis` calls still exist and run in the caller's thread (scripts and tests use them).
- **Rules, kept small.** At most 4 Runs at the same time (`P2_MAX_RUNS`); the rest wait first come first served, with room for 50. At most one Run per experiment queued or running: asking again returns the Run already there, which also covers two people clicking. Obvious mistakes (no plan, not launched yet, final analysis before the last day) are refused immediately instead of after waiting in line.
- **Retries.** A temporary BigQuery problem (rate limit, too many concurrent queries, busy or unavailable, a connection reset) is retried after 5, 15 and 45 seconds; after that the Run fails with "BigQuery stayed busy after N tries". Anything else (a data-quality gate, a bad plan) fails at once with its reason and is not retried. Each try is recorded as its own job so the history shows what happened.
- **Clean endings.** Before, an unexpected error in the middle of a Run left its job marked "running" for ever. Now any failure closes the job as failed, and at server start jobs still "running" after 10 minutes are marked failed ("interrupted when the server stopped").
- **What the person sees.** The results page shows the queue position or "running for N seconds" and refreshes that line every 3 seconds without reloading the page; when the Run finishes the page reloads with the new numbers and a "finished" message. While a Run is in progress the Run button is disabled (so a second click cannot start another). A failed Run shows its reason with a Dismiss button. The Experiment Catalog marks experiments with a Run in progress. Save and run saves at once, queues the Run and opens the results page, where the status is shown.
- **Limit to remember.** The queue lives in one server's memory. If the server restarts, waiting Runs are gone and people press Run again. Two servers would each have their own queue and could run the same experiment twice, so deployment should use one app server (it is also what keeps the in-memory data consistent).
- **Tests.** 380 offline, including queue ordering and the cap on concurrent Runs, duplicate requests, retries and give-up, no retry for real failures, and page tests for queued, running, finished and failed Runs. The BigQuery pipeline and platform tests were started and the first 30 passed, then the run was stopped at the user's request as too slow for a small change, so they were not completed; the Run path was checked live on the dev app instead.

## 2026-10-01: old numbers while a Run is working, and numbers that no longer match the plan

- **Trigger.** The user changed exp-002's launch date to test the behaviour, pressed Refresh, got the refusal "no assignments between 2026-09-22 and 2026-09-30, but the log has 5,000 between 2026-09-01 and 2026-09-08" (correct: the simulated users were only logged on 1 to 8 September), and the page kept showing the old numbers. They asked that numbers not be shown while a fresh Run is working, and said the numbers did not update with the date.
- **Hidden while running.** While a Run is queued or running the results page shows only the header and the status line; the cards, tables and charts from the previous Run are hidden, with a note that they return when the Run finishes.
- **Numbers that no longer match the plan.** Each successful Run now records the launch date, the end date and a short fingerprint of the whole plan. If the plan is edited afterwards, the results page says so in one yellow message: for example "the launch date is now 2026-09-22 (it was 2026-09-01) since they were calculated. Press Run to update them." It also says so for the end date and for changes to metrics, segments, filters or statistical numbers; a name or hypothesis change is not flagged. If the latest Run failed, that is added to the same message so there is one message, not several. Runs from before this change are still flagged when their last day no longer matches.
- **What was not changed.** The numbers still do not recalculate by themselves when the plan is saved; that needs Run (or Save and run), because a Run scans BigQuery for one to two minutes. The old numbers stay visible, marked, when a Run fails, so a failed Run does not leave an empty page.
- **Tests.** 387 offline.

## 2026-10-01: one fixed September 2026 dummy dataset for the demo

- **Trigger.** Making dummy data per experiment and per launch date kept failing the data gate (users only sat on the days the simulator had been asked for). A daily top-up simulator was considered and dropped as too much for a demo.
- **Decision.** One fixed dataset: 6 demo experiments (`sep-checkout-1/2`, `sep-email-1/2`, `sep-onboarding-1/2`), 6,000 users each, assigned evenly on every day from 1 to 30 September 2026, activity up to the end of 30 September (`src/p2/simulator/demo.py`). The first of each pair has a planted effect, the second none. All three products' activity is landed for every id, so any metric can be picked (the effect is on the id's own product only).
- **What it gives.** A demo experiment can be added with any launch date and any end date inside September, and the dates can be edited inside the month and Run again. Everything in September is in the past, so a final analysis works for any experiment ending by 30 September.
- **How it is landed.** A dev-only expander at the bottom of Add Experiment ("Land September demo data"; hidden with `P2_DEV_TOOLS=0`). It takes a few minutes and is safe to repeat (each id is replaced). New engine option `until` in `simulate_experiment` cuts activity at the end of a day.
- **Limits.** Users assigned late in September have less than 7 days of activity before the cut, so late launches show smaller counts for slow metrics. No ground truth is saved for these ids (it needs an existing experiment). The older per-experiment simulator on the results page still works for other ids.

## 2026-10-01: scalability steps 4 to 7 skipped

- **Decision.** P2 is a demo, not going to 20 people. Steps 1 to 3 stay; cost guards, pruning, deploy shape and safety nets are dropped (see `SCALABILITY.md`).
- **Metric catalog.** Stays as the Metrics tab in Experiment Catalog; no separate page.

## 2026-10-01: R6 portfolio dashboard is the Experiment Catalog

- **Decision.** The default Experiment Catalog page already shows how every experiment is doing (counts, status, progress, verdict), so no separate dashboard is built. Win rate, time to decision and decision history are skipped for the demo.

## 2026-10-01: dev simulator removed from the results page

- **Decision.** The per-experiment simulator expander on Experiment Results is gone; the fixed September data replaces it. The "no assignments between ..." error now says to check the dates against the log and mentions the September demo data. The service method `simulate_data` stays (used by BigQuery tests). The only dev tool left in the UI is "Land September demo data" on Add Experiment (`P2_DEV_TOOLS=0` hides it).

## 2026-10-01: SRM check as a card

- **Decision.** A card at the top of Experiment Results (above the headline numbers) says "The test is balanced" or, when the arm counts do not match the planned split, "The split looks wrong" with the split seen. Nothing else: no gate, no block, no extra setting.
- **How.** Chi-square test of the control and variant user counts of the primary metric against 50 / 50 (`src/p2/stats/srm.py`), flagged when p < 0.001 (strict, so a healthy test is almost never flagged). It uses the counts already saved by the Run, so it adds no BigQuery work and shows for live and final numbers.
- **Not done.** The planned split is assumed to be 50 / 50 because the split setting was removed from the plan earlier; an uneven split would show as not balanced.
- **Tests.** 399 offline.

## 2026-10-03: demo experience planned, not built yet

- **Decision.** For people who find the public repo: a short GIF in the README, then a live read-only demo mode (sample data in memory, all writes blocked, Run and Save disabled, banner) hosted free on Streamlit Community Cloud. Not deployed with the real BigQuery store because the app has no sign-in and every Run costs money. Details in `PLAN.md` ("Demo experience").
- **Order.** A few more additions to the app first; the demo is built once they are final.

## 2026-10-03: Bayesian method added

- **Decision.** The data scientist picks Bayesian or Frequentist per experiment on Add Experiment. The page shows only the inputs and outputs of the chosen method. The method is stored on the experiment and follows the plan: saving a Bayesian plan makes the experiment Bayesian, saving a frequentist plan makes it frequentist (switching is allowed; old numbers then ask for a new Run).
- **Live results.** Bayesian has no no-peeking rule. The verdict waits for a minimum number of days (default 7, capped at the runtime), then compares the risk of shipping the variant and the risk of keeping control with the data scientist's threshold, as in the user's last job. After the last day an unclear result reads Inconclusive, because no more data is coming.
- **Guardrails.** Chance of harm against a margin, strict by default (pass below 1%, fail above 50%), because we do not want to hurt a guardrail.
- **Compute on view.** Every Run now stores the per-arm variances in `results` and `segment_results` (new nullable columns). The posterior is computed when the page opens from counts, means and variances, so a changed threshold needs no new Run and there is no extra BigQuery scan. A Bayesian experiment's Run skips the frequentist tests.
- **Models.** Beta-Binomial for rates, Normal posterior for averages, flat prior. Not built: informative priors, ROPE band, partial pooling, monetary value, prior sensitivity check, heavy-tail model for revenue.
- **Finding from validation.** The loss rule bounds expected loss, not the false-win rate. At 20,000 users per arm and a 10% rate, a threshold of 0.05 pp names a safer arm in about 57% of A/A runs (one standard error of the difference is 0.3 pp), 0.01 pp in about 15%, and 0.003 pp in about 4%. The defaults are starting points; the threshold should be well below the noise of the experiment. Requiring a 95% chance to win on top (5% for control) would cut A/A calls to about 11%, about 5% each way (checked on 1,000 simulated A/A runs), if wanted.
- **Defaults not confirmed by the user.** Risk threshold 0.05 pp for rates and 0.5% of the control average for averages, 50% as the guardrail fail cut.
- **Tests.** 458 offline (399 before; 59 new for the Bayesian engine, plan form, service layer and pages).
- **Charts for PMs (same day, after review).** Bayesian Charts tab order: control and variant over time, then the risk of each choice (moved up: one plain sentence that says the decision, then side by side the risk bars on all the data, green when under the threshold, and the two risks by day against the threshold), then the 95% credible interval and the chance the variant wins by day, side by side. The posterior shape was removed: it added confusion for PMs.
- **Verdict card (both methods).** The headline is now the lift card and a wide verdict card with a one-line reason: frequentist gives the p-value against alpha (or when the verdict comes, while live); Bayesian gives both risks against the threshold (or "too early" before the minimum days). The control and variant cards, the user counts and the other details were removed from the headline: they are in the table, and PMs read the verdict first.
- **Search, chart filter and catalog table (same day).**
  - **Search:** the Experiment Results search is now a dropdown of the experiments already in the app: typing "sep" lists the matching ids and picking one opens it (no Search button, and an id that is not in the app cannot be entered).
  - **Chart filter:** the metrics picked on the Charts tab take effect only when Apply is pressed, so the charts do not jump while picking.
  - **Catalog table:** every cell is two lines (a main line and a quieter one) so the columns line up, and a new Test column shows Bayesian or Frequentist. The primary metric shows its type (rate or continuous) underneath.
- **Docs and screenshots refreshed (same day).** The README screenshots were retaken with headless Chrome (light theme, 1400 px at 2x) from the running app: catalog (with the Test column), frequentist results and charts, and new Bayesian results and charts, and the Add Experiment form with the Bayesian inputs. The README, TECH_SPEC and PLAN.md describe the dropdown search, the Apply button, the wide verdict card and the two-line catalog cells. The catalog column widths were widened so verdict chips and the Edit button no longer wrap or get cut off.
