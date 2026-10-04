# Plan: P2 Experiment Platform (team version)

| Field | Value |
|---|---|
| Updated | 2026-09-30 |
| Version | v0.3, team platform (re-planned from the single-user app) |
| Learning plan | [AI_Automation_Engineering_Plan.md](../../AI_Automation_Engineering_Plan.md) |
| Master plan | [Portfolio_Master_Plan.md](../Portfolio_Master_Plan.md) |
| Tech spec | [TECH_SPEC.md](TECH_SPEC.md) |
| Data sources | [DATA_SOURCES.md](DATA_SOURCES.md) |
| Decisions log | [P2_Decisions_Log.md](P2_Decisions_Log.md) |

This document covers WHEN and WHAT TO LEARN FIRST. How the system works lives in the tech spec.

Legend: DONE = covered by a completed module. PREREQ = must learn first. GAP = small skill not in the plan, learn just-in-time. OPTIONAL = only if wanted.

## Goal

- An internal platform where a team of about 15 data scientists, each on a different product with its own metrics, register experiments, pick governed metrics, and track every experiment the team runs.
- Everything routine happens in a UI. No git or python needed to add a metric or register an experiment.
- Working agreement stays: you pick metrics and cuts before code; propose, align, then change; public synthetic data only; stage only, no commits until asked.

## Decisions locked (2026-09-29)

- Team-scale design with roles: viewer, experimenter, metric owner, admin. Simulated users; a "Demo: switch user" box in the sidebar stands in for sign-in. Real sign-in at deploy.
- Three simulated products (Checkout, Email, Onboarding), one owner each to start, plus an admin. Later about three data scientists per product.
- App state in BigQuery (dataset `app`), no Postgres. Streamlit calls a service layer directly. FastAPI deferred until the chatbot or deploy needs it.
- Metrics are custom SQL in the **user-day contract**: one row per user per day (`user_id`, `metric_date`, `value`). No no-code builder.
- Unit of randomization and analysis is the user. Two arms. Each user is measured from their assignment day through the experiment's last day (launch date plus runtime minus one day).
- Catalog items (metrics, dimensions, filters) share one lifecycle: Draft, In review, Certified, Deprecated. Only Certified items reach experiment dropdowns. Author cannot certify their own item.
- Data foundation first (approved in DATA_SOURCES.md): 9 raw source tables over a shared user universe, platform-generated staging views with de-duplication, dimensions and filters as first-class catalog items resolved as of assignment date, a pipeline built from small one-table steps so no query gets large.
- Frequentist analysis first. A Bayesian method, chosen per experiment, was added on 2026-10-03. CUPED and multiple-comparison correction later.

## Direction change (2026-09-30)

- **The data scientist owns power and sample size.** Baselines depend on who was bucketed, which differs for every experiment and every one of 15 to 20 data scientists, so a tool-computed baseline can be silently wrong. The tool no longer calculates sample sizes, durations or baselines. The data scientist enters alpha, power, baseline, standard deviation and MDE for the primary metric and each guardrail, plus experiment id, product, launch date and runtime. The tool records those numbers and follows them when it analyses the results. No planned sample size is asked for and no sanity checker is kept.
- **Measurement runs until the end of the runtime**, not for a fixed week.
- **Statistics are computed in BigQuery from summary numbers** (count, mean, variance per arm) so the tool works for any experiment size; Python runs the tests on the summaries.
- Removed because of this: the sample-size code, the warehouse baselines (and the seeded baseline-period history), the product population queries, sample-size fields in the wizard and database.

## Foundation already built (Phases 0 to 4b, DONE)

- Kept and still in use: the single metric calculation function (`compute_metric`) and the Streamlit pages (reshaped in R2 to R5). The provisional metric list became the seeded catalog in R3. The power calculation built in Phase 2 was removed in R5.
- Retired during R1: the event-level SQL pipeline and its templates, the Checkout-only generator, the single raw events table.
- Replaced in R2: the SQLite app database (deleted).
- What carries over and what is reshaped is listed in section 15 of the tech spec. Test counts live in the decisions log.

## Roadmap

### R1: Data sources and simulator (DONE)
- Spec: [DATA_SOURCES.md](DATA_SOURCES.md) (approved). You pick the final metric list per product when we reach R3.
- Needs: Modules 1, 2, 2.5. DONE. GAP: none (BigQuery SQL you know from work).
- Tasks:
  - Create the four datasets per environment and the 9 raw tables with partitioning and clustering.
  - Source registry (code-first seed for now, moves to the app in R2) and the generated staging views with de-duplication.
  - Simulator: shared user universe (users, plan history) and per-product activity with closed-form truth; land all 9 tables, including the planted mess.
  - Checks: row counts, key uniqueness after staging, planted duplicates removed, as-of lookups return the right plan.
- Exit: 9 tables landed in dev; staging views return clean data; the planted mess is provably handled; truth values documented.

### R2: Shared foundation (state, users, audit) (DONE)
- Needs: Modules 1, 2. DONE. GAP: BigQuery DML patterns (`MERGE`, insert-if-absent, optimistic update).
- Tasks:
  - `Store` interface with `BigQueryStore` and `MemoryStore`, plus shared contract tests.
  - `app` dataset and tables from the tech spec; service layer skeleton with `actor`, permission checks, audit entries.
  - Move experiments and designs out of SQLite, and the source registry into the app. Remove the SQLite dependency.
  - Sidebar "Signed in as" label with a collapsed demo switch over the seeded users (3 owners and 1 admin).
- Exit: existing Design flow works for a chosen user; permissions tests pass offline; audit log records every write.

### R3: Catalog (metrics, dimensions, filters) (DONE)
- Needs: R1, R2.
- Tasks:
  - `catalog_items` table with versions and lifecycle for all three kinds.
  - Contracts (metric user-day, dimension and filter with as-of-entry), SQL safety checks (dry run: SELECT only, registered sources only, cost cap), automated data checks, sample preview.
  - Propose-an-item form, review queue, certify and deprecate with separation of duties.
  - Catalog browse and search with usage by experiments.
  - Seeded as Certified starters (assumed accepted when you said go): the 15 provisional metrics, the 5 agreed dimensions and the 4 agreed filters, each with real SQL. Change them through the catalog.
  - The step-based pipeline: cohort, attribute steps, audience, metric steps, generated final table. Retire the YAML registry.
- Exit: an owner proposes a metric, a dimension, and a filter in the UI, checks run, an admin certifies them, they appear in the wizard; a bad query is rejected with a clear reason; an experiment builds with filters and dimensions.

### R4: Experiment wizard and registry (DONE, revised in R5)
- Built: a wizard with editing of saved experiments; a portfolio with status counts, filters and per-experiment history. Simplified in R5 to record the data scientist's own plan (see the direction change above), then rebuilt as one page in the R5.1 feedback round (below).
- Exit met: three owners each create experiments in their product and everyone sees all of them in the portfolio.

### R5: The plan form, RUN and the statistics (DONE)
- Needs: R4. No new modules.
- Tasks done:
  - Plan form: experiment id (checked against the assignment log), product, launch date, runtime, owner from the signed-in person, primary and guardrail statistical inputs, secondary metrics, optional filters and dimensions. No sample-size or baseline calculation.
  - Pipeline change: users assigned during the runtime, measured from their assignment day to the last day; gates for not launched, still running, and assignments outside the runtime.
  - Statistics engine (`src/p2/stats/`): two-proportion z-test, Welch t-test, non-inferiority for guardrails, confidence intervals, achieved power, verdicts; computed from warehouse summaries. Validated against scipy and statsmodels and by simulation (A/A error rate, interval coverage, power, guardrail error rate).
  - RUN: one button builds the data, computes the statistics, saves results (history kept), moves the experiment to Analyzed. Results table and decision log (ship, no ship, iterate, inconclusive with a reason).
  - Simulated data for a planted effect from the UI (dev only).
- Exit met: full loop from plan to recorded decision in the UI; on BigQuery RUN recovers the exact planted effects and an A/A experiment is not called a winner.

### R6: Portfolio dashboard (DONE, covered by the Experiment Catalog)
- Decision 2026-10-01: the Experiment Catalog, the default page, is the portfolio view (counts of total, live, not started and ended; every experiment with status, progress and verdict). Win rate, time to decision and a decision history are not built; skipped for the demo.
- Needs: R5.
- Tasks:
  - Portfolio metrics: running, needs decision (the count already shows), health issues, win rate, time to decision, by product.
  - Daily per-arm stats table for trend charts.
  - Decision history view.
- Exit: someone opening the app cold can see how every experiment is doing.

### Demo experience (built 2026-10-04)

- Built: the GIF (`docs/lifecycle-demo.gif`, the whole lifecycle: EDD, tool, results page) and the read-only demo (`app/demo_app.py` on `demo/app_tables.json.gz`). Differences from the plan below: the GIF follows the lifecycle instead of the tool alone; the demo hides Add Experiment, Edit and Run instead of showing a disabled form, and it has no BigQuery runner, so nothing can be run even by accident. Hosted by the owner on Streamlit Community Cloud: https://p2-experiment-platform.streamlit.app/ (public, redeploys on every push to main).

The original plan:
- Why: a public repo is read, not felt. Visitors should be able to see and click the app without BigQuery or credentials, and without any cost or risk to the owner.
- 1. A 15 to 20 second GIF or video at the top of the README (Catalog, open results, balance card, Charts tab, Add Experiment form). Recorded from the running app with headless Chrome, as the screenshots were.
- 2. A live read-only demo mode of the same app: sample data exported from the dev store into a small file in the repo (synthetic), loaded in memory; every write blocked at the store; Run and Save disabled with a banner "Demo mode: read-only, sample data". Host on Streamlit Community Cloud from the public repo (free; sleeps when idle, so the first load is slow). Connecting it needs the owner's GitHub login, so the owner approves that step.
- Experiments to show (suggested): `demo-banner`, `exp-002`, plus one live, one with no effect and one with an unbalanced split (so the SRM warning shows).
- Add Experiment in demo mode: the form opens for demo IDs and shows everything; Save is disabled with a note.
- Decided 2026-10-03: keep this plan; build after a few more additions are final.

### Experiment pages (next, not built yet)
- Scope, decided 2026-10-04: **the tool is finished and does not change.** This is a separate piece that turns an experiment's results into a results page (a static git page) for product managers and stakeholders.
- The lifecycle it completes, and who owns each step:
  - 1. **PM brings the experiment design doc (EDD).** A separate document kept by the PM and DS outside the tool: what we test and why, hypothesis, metrics with baseline and minimum detectable effect, guardrails, decision criteria, visible changes.
  - 2. **DS sets up the experiment in the tool.** Done: the Add Experiment form.
  - 3. **DS reaches a conclusion from the tool's data.** Done in the tool: the balance and placebo checks, the verdict with its reason, the tables and charts. The conclusion itself (decision, summary, recommendation) is written by the DS in the page's own conclusion file, never in the tool.
  - 4. **DS builds the results page.** This is the new work.
- A page comes from three inputs and one fixed template:
  - **The EDD** of that experiment (background, hypothesis, metrics, audience, decision criteria). It needs a fixed set of headings so the template can read it.
  - **The tool's results** for that experiment, exported read-only from the app's store: results rows, segment rows, daily stats, the balance and placebo checks, the verdict and its one-line reason.
  - **The DS's conclusion**, a small separate file with fixed fields (decision, executive summary, recommendation, notes on anything odd).
  - **The template** puts them together: header (experiment, DS, PM, period, sample, verdict), executive summary, links, background, results table, notes on odd results, balance check, charts, conclusion and recommendation.
- Nothing is typed twice: numbers and charts come from the tool, the story comes from the EDD and the conclusion file. Anything the page needs that the tool does not store, such as a Win, Neutral or Loss label for each secondary metric or the balance p-value, is worked out in the export step from the tool's own functions, so the app is untouched.
- Product context file, one per product (Checkout, Email, Onboarding), in the style of the existing product context file. It is a helper for writing the EDDs and pages with the same facts, not an input the template needs.
- Suggested layout: `experiments/<experiment-id>/` with `design.md` (the EDD), `conclusion.md`, `results.json` (a snapshot exported from the tool) and the page; `products/<product>.md`. Published with GitHub Pages. The snapshot is committed, so the public pages need no BigQuery.
- Rules: the repo is public and synthetic only (see the scope rules in TECH_SPEC). The reference documents from work are format guides only, so no employer names, numbers, text, links or screenshots go in. The EDDs and conclusions are written fresh for the synthetic September experiments.
- Steps, in order:
  - 1. DONE (draft, 2026-10-04): the EDD format is Markdown with fixed headings (`experiments/README.md` and `experiments/_template/design.md`), and the Checkout product context is in `products/checkout.md`.
  - 2. DONE (draft, for the owner to edit): a synthetic EDD for each of the three experiments that already have results in the tool: `demo-banner` (frequentist, a win), `sep-checkout-1` (Bayesian, inconclusive) and `exp-002` (frequentist, inconclusive because it ran with far fewer users than the plan needed). A Bayesian win can be added later.
  - 3. DONE (2026-10-04): the results export and the page template (`src/p2/report/`, `python -m p2.report.export` and `python -m p2.report.build`), with tests.
  - 4. DONE as drafts (2026-10-04): a conclusion file for each example, and the three pages and a list page generated. Still to do: publish them with GitHub Pages, which needs the owner's approval.
  - 5. Link the pages from the README and, in the demo, walk through the lifecycle above.
- Decided: the EDD format is Markdown. The repo is already public. Still open: the conclusion file fields; whether the page draws its own charts or reuses images from the app; and switching on GitHub Pages, which is the owner's setting to approve when we get there.
- Decided 2026-10-04: nothing is added to the tool, the EDD is a separate file kept outside the tool, and the conclusion lives in the page's conclusion file. Build after the final app work, before or with the demo experience.

### R7: Ship and operate
- PREREQ: Module 7.2 (Docker), 7.5 (Cloud Run), 7.4 (observability), 7.3 (CI/CD) is DONE. Module 7.1 (FastAPI) becomes PREREQ only if a second client is added.
- Tasks: Docker, Cloud Run, sign-in behind Google identity, Cloud Scheduler for daily runs, notifications, CI running the BigQuery tests, cost and job monitoring.
- Exit: live URL, scheduled job green, budget alert intact.

### Later
- CUPED, ratio metrics, multiple-comparison correction. Bayesian extras: informative or historical priors, ROPE band, partial pooling for segments, monetary value of lift, prior sensitivity check, heavy-tail model for revenue.
- Chatbot over results (PREREQ: Module 5 LangSmith videos, FastAPI 7.1), optional RAG over methodology docs (PREREQ: Module 6).

## Risks and mitigations

- BigQuery is slow for small writes: cache list reads, keep writes per screen few, `MemoryStore` for fast tests.
- Authors own de-duplication in user-day SQL: automated checks (duplicates, nulls, ranges) and a sample preview.
- Custom SQL safety: dry run enforces SELECT only, registered tables only, and a cost cap.
- Simulator effort for three products: keep each model small, closed-form truth, minimal metrics.
- One owner per product means no independent review: the admin certifies until a second owner exists.
- Scope creep: each roadmap step has an exit check; nothing new is added without alignment.

## Learning track in parallel

- BigQuery DML and `MERGE` patterns during R1.
- Module 7.2 Docker and 7.5 Cloud Run before R7.
- Module 5 LangSmith videos before the chatbot phase, not before R1 to R7.

## Deliverables

- Repo README with architecture, roles, catalog lifecycle, pipeline, validation results, cost table, lessons.
- Demo script with three personas: an owner proposing a metric, an admin certifying it, a data scientist running an experiment through to a decision.
- Slide deck in the same style as P1.

## Checklist

- [x] Phase 0 to 4b foundation (setup, registry, storage, simulator, Design page, BigQuery SQL pipeline; the Phase 2 power calculation was later removed)
- [x] Team-scale plan and tech spec v0.2 approved (2026-09-29)
- [x] DATA_SOURCES.md v1.0 approved (2026-09-29)
- [x] R1 Data sources and simulator (2026-09-29): source registry generates 9 raw tables and de-duplicating staging views; shared universe and three product models with closed-form truth; planted mess; 15 BigQuery tests prove staging plus a whole-day window recover the clean outcomes for all three products
- [x] R2 Shared foundation (2026-09-30): Store interface with memory and BigQuery implementations sharing one contract test suite, 10 application tables, service layer with permissions and audit, simulated team with an acting-as switch, Design, Experiments, and Admin pages on the service layer, SQLite deleted
- [x] R3 Catalog (2026-09-30): catalog_items with versions and lifecycle for metrics, dimensions and filters (24 seeded with real SQL); automated checks (dry-run safety, data rules, preview) on BigQuery; Propose, Review queue and Browse in the UI; separation of duties; the step-based pipeline (cohort, filters, dimensions, audience, metrics, generated final table, gates, job log); baselines from the same steps; Design page with filters and dimensions; Experiments page can simulate data and run the pipeline; YAML registry retired
- [x] R4 Experiment wizard and registry (2026-09-30): four-step wizard (Basics, Audience, Metrics, Review and save) with register-as-draft, load and edit an existing experiment, tags; portfolio with status counts, filters by product, owner, status, tag and search, design summary per experiment (primary metric, users needed, days, bottleneck), and per-experiment history
- [x] R5 Plan form, RUN and statistics (2026-09-30): the data scientist's own plan is recorded and followed (no sample-size or baseline code); runtime-based measurement window; statistics engine from warehouse summaries validated against scipy, statsmodels and simulation; results with history; decision log
- [x] R5.1 Feedback round after trying the app (2026-09-30): one-page plan form with Save and Save and run (no tabs, no Start from, no tags, no review step); the owner is the signed-in user (the visible Acting as switch is now a collapsed demo box); one-line hover tips on fields; every metric from every product in every role, a metric picked in one role leaves the other two; metric type from the catalog decides the test and the Std dev field (disabled for rates); filters and dimensions optional; live monitoring from the first complete day (counts, means, difference, lift only) and the verdict only after the last day; Edit plan on the Experiments page; what was typed survives visiting other pages
- [x] R5.2 Experiments page as a results view (2026-09-30): search by Experiment ID replaces the portfolio overview; metric table first, then control and variant over time (new `daily_stats` table written by every run) and a confidence-interval chart with the planned effect and guardrail margin; loading spinners and a visible running indicator
- [x] R5.3 Feedback round on the results view (2026-10-01): optional end date so an experiment can run past its planned runtime; Edit details (name, hypothesis, end date) for locked experiments and Copy as new experiment; chart metric filter (primary by default); segment tables per dimension value (no charts); run log and history hidden from the page; simulated data now also lands activity for other products' metrics so they are not zero
- [x] R5.4 New experiment page finalized (2026-10-01): create only, starts from the Experiment ID; existing id says "already exists"; an id missing from the assignment log stops the page; launch date filled from the first assignment and editable; everything except the id editable in any status (no locks), a warning when extending an experiment that has results; the status follows the latest run
- [x] September demo data (2026-10-01): one fixed dataset, 6 demo experiments (`sep-*`) with users assigned on every day of 1 to 30 Sep 2026, landed by a dev-only button on Add Experiment; any launch and end date inside September finds data
- [x] R5.5 Experiments page finalized (2026-10-01): two tabs (Tables, Charts); Segment and Filter dropdowns on the Tables tab (plan items only, combinable, default Everyone); filters became views instead of cuts; difference-over-time chart with a shaded 95% range after the final analysis; Day X of N header; Run button; decision form, plan box, run log and history removed from the page; dev simulator switchable off
- [x] R5.6 Experiment catalog (2026-10-01): the Catalog page has two sub-tabs; the Experiment catalog lists every experiment with progress and verdict, an ID filter, Edit and Open results; the experiment form is shared so editing happens in the catalog and New experiment is create only. The Metric catalog tab is the old Catalog, still to be reviewed
- [x] R5.7 Metric catalog simplified (2026-10-01): one table of available metrics (metric, product, added by, definition) with a Select metric search box; then the add, review and version tools were removed from the UI: metrics are added in BigQuery
- [x] R5.9 Admin page removed (2026-10-01): it only displayed team, products, data sources and the audit log, which stay in BigQuery tables. The tool has three pages: New experiment, Experiments, Catalog
- [x] R5.10 Navigation and identity (2026-10-01): the page bar moved to the top for wider charts; pages renamed Experiment Catalog (default), Experiment Results, Add Experiment; the sidebar, the "Signed in as" label and the demo user switch are gone; no permissions in the tool; the Owner is picked in the form
- [x] R5.11 Visual refresh (2026-10-01): one stylesheet and a theme file; tighter spacing under the top bar; bordered cards for the experiment header, the form sections and the catalog rows; coloured chips for status, progress and verdict; headline cards for the primary metric; tables with light borders and green, red and yellow tints for good, bad and flat lift and for verdicts; blue for the variant in charts
- [x] R5.12 Polish round on the first look (2026-10-01): equal-height cards, control then variant then lift then verdict, a colour key, shorter table cells with the verdict next to the lift and no blank columns while live, grey for "no significant difference", compact search and dropdowns, a catalog summary line with live experiments first, hand-drawn progress bars (blue live, green ended, yellow not started), short owner names, renamed inner tabs
- [x] Scalability probe (2026-10-01): measured read times from 2 to 200 experiments, listed failure modes and a work order in SCALABILITY.md. Nothing built yet
- [x] Scalability step 1, instant pages (2026-10-01): the app tables are kept in memory and written through to BigQuery; pages open in 0.04 to 0.15 seconds with no queries after the first load; the background refresh asks BigQuery which tables changed (free) and re-reads only those, only while someone is using the app
- [x] Scalability step 2, lighter writes (2026-10-01): changes are saved to memory at once and written to BigQuery in the background as one statement per table; Save and a Run's write phase no longer make the person wait (28.6 s and 41.9 s before, 0 s after) and use 9 write statements instead of 41 statements
- [x] Scalability step 3, Runs in the background (2026-10-01): Run, Refresh and Save and run queue the work and return at once; 4 at a time, one per experiment; retries for temporary BigQuery problems; a live status line on the results page and a "Run in progress" marker in the catalog; every Run ends cleanly and stale "running" jobs are recovered at start
- [x] R6 Portfolio dashboard (2026-10-01): covered by the Experiment Catalog (default page); win rate and decision history skipped for the demo
- [x] SRM check (2026-10-01): a card at the top of Experiment Results says the test is balanced, or warns when the split is off
- [x] Placebo check (2026-10-03): a card on Experiment Results with an A/A test on the experiment's own control users, saved by every Run for both methods; see P2_Decisions_Log.md
- [x] Bayesian method (2026-10-03): chosen per experiment on Add Experiment; Beta-Binomial and Normal posteriors with a flat prior, chance to win, 95% credible interval, risk of each choice and a live verdict after a minimum number of days; Bayesian charts for PMs; wide verdict card with a one-line reason for both methods; search dropdown, chart Apply button and a catalog table with a Test column; screenshots retaken. See P2_Decisions_Log.md
- [ ] Experiment results pages (planned, not built; the tool is not changed): EDD, conclusion file and exported tool results combined by one fixed template into a static page per experiment
- [ ] R7 Ship and operate (skipped for the demo)
