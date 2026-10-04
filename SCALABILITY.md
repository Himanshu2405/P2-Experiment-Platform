# P2 scalability and failure modes

## Status (2026-10-01)

- **Step 3, Runs in the background: done.** Clicking Run or Refresh (or Save and run) puts the Run in a queue and returns at once. Four worker threads (`P2_MAX_RUNS`) run them; at most one Run per experiment is queued or running, so two clicks or two people never start two builds of the same tables; the rest wait in order (up to 50 waiting). The results page shows "Waiting in line, position N" or "Running for N seconds", redraws itself every 3 seconds, and reloads with the new numbers when the Run ends; the person can leave the page and the Experiment Catalog marks the experiment "Run in progress". A temporary BigQuery problem (rate limit, busy, brief outage) is retried after 5, 15 and 45 seconds before giving up with a plain message; a data problem fails at once with its reason. Every Run now ends cleanly: an unexpected crash closes the job as failed instead of leaving it "running", and jobs left running by a stopped server are marked failed at the next start. Checked live on dev: the button turned into a disabled "Running", the status line counted up, the catalog showed "Run in progress" from another page, and "Final analysis finished." appeared by itself. The queue lives in one server's memory, so it assumes one app server (the deploy shape in item 6).

- **Step 2, fewer and lighter writes: done.** Writes are saved to memory at once and sent to BigQuery in the background as one combined statement per table (the net effect of everything since the last flush, about every 2 seconds). Measured with the same Save and Run on a throwaway BigQuery environment: before, the person waited 28.6 seconds for Save and 41.9 seconds for the write phase of a Run, with 41 BigQuery statements; after, they wait 0 seconds for both and the same work is 9 write statements sent in the background (about 9 seconds). Only one flush runs at a time per server, so 20 people cannot pile up more writes than BigQuery allows. If BigQuery is unreachable the changes stay queued, are retried with growing pauses and are flushed on exit; a warning appears only if a change has waited over 30 seconds. A single BigQuery write takes 2 to 3 seconds whatever its size or grouping (a script of 8 writes took as long as 8 separate ones), which is why fewer statements and not waiting for them were the fixes.

- **Step 1, instant pages: done.** The small application tables are kept in memory (`src/p2/store/snapshot.py`) and written through to BigQuery. Measured on the dev data after the first load: every page opens in 0.04 to 0.15 seconds with zero BigQuery queries, down from 10 to 15 seconds cold. The first page after a server start takes about 17 seconds once (about 10 of those are the usual start-up table check and seeding) and then it is instant for everyone.
- **How fresh is it?** Your own saves show at once. Other people's changes show within about 60 seconds (`P2_REFRESH_SECONDS`).
- **What it costs in BigQuery** (on-demand price about $6.25 per TiB, the first 1 TiB each month is free, and BigQuery bills at least 10 MB for each table a query reads): the refresh first asks BigQuery when each table last changed (a free metadata call) and only reads a table that did change; it also only runs while someone is using the app. Measured: 40 seconds of use with nothing changing sent 0 queries; one changed table sent exactly 1 query and billed 10.5 MB (about $0.00007). The first load per server start is 14 queries and about 136 MB (about $0.001).
- **Worst case:** 20 people busy all working month with every table changing on every 60-second cycle is about 1.2 TB a month, roughly $1 after the free tier (about $7 if the free tier is already used up elsewhere). Realistically it is cents.
- **A safety limit you can set yourself:** in Google Cloud, set a custom BigQuery quota on "Query usage per day" for the project (for example 100 GB). The app's own reads use well under 1 GB a day, so the limit would only ever stop something else. The real spend risk is Runs scanning big event tables (item 4 below), not these reads.


Measured on 2026-10-01 against a throwaway BigQuery environment, then dropped. Question asked: can the tool handle 50 to 100 experiments, how much slower does each experiment make it, and what breaks.

## What was measured

The probe filled the application tables with N synthetic experiments (each with 7 plan items, 2 result sets of 5 metrics, 140 daily rows, about 40 segment rows per metric, 3 job runs and 36 job steps; at 200 experiments that is about 28,000 daily rows and 8,000 segment rows) and timed what the pages read. Times are seconds from a laptop to BigQuery in the US.

| Experiments | Catalog list read | Results page, cold cache | Catalog page, cold cache incl. server start | Any page, warm cache |
|---|---|---|---|---|
| 2 | 2.4 | 10.3 | 20.7 | 0.07 |
| 25 | 2.9 | 10.6 | 19.7 | 0.08 |
| 50 | 2.6 | 10.0 | 18.3 | 0.09 |
| 100 | 5.0 | 13.2 | 25.3 | 0.09 |
| 200 | 5.1 | 14.9 | 21.0 | 0.09 |

## Findings in easy words

- **Each extra experiment adds very little.** About 0.02 seconds to a cold page, so 100 experiments cost about 2 seconds more than 2, and 200 about 5 seconds more.
- **The big cost is fixed, not per experiment.** A cold results page makes about a dozen separate BigQuery queries one after another, about 0.8 seconds each, which is about 10 seconds. The catalog page, when the server has just started, adds about 10 seconds of one-time setup (creating or checking tables, seeding).
- **Warm pages are instant** (under 0.1 seconds) because reads are cached for 120 seconds. The cache is shared by everyone on the same server, so with 20 people about one person in each 2-minute window pays the 10 to 15 seconds.
- **Writes are slower than reads.** Every write is one BigQuery statement (about 1 to 3 seconds each). A Run makes about 15 to 20 of them on top of the calculation, so a Run takes 65 to 110 seconds on the dev data.

## Failure modes (what breaks first, most likely first)

1. **Write limits per table.** BigQuery runs only a few data-changing statements at once on one table and queues about 20 more; beyond that statements fail. 20 people saving or running at the same moment can hit this on `results`, `job_runs` and `audit_log`.
2. **Many Runs at once.** One Run uses 4 parallel workers and each metric adds more queries. A handful of simultaneous Runs can reach BigQuery's concurrent query limit (about 100) or wait for slots, so Runs slow down or time out.
3. **A Run blocks the page for 1 to 2 minutes.** Many proxies and load balancers cut a silent connection after about 60 seconds. The Run may finish while the person sees a dropped page.
4. **Cost.** Metrics are not checked when inserted in BigQuery, and the cost cap is not applied at run time. A metric that scans a large events table without a date filter is paid for again on every Refresh, for every experiment.
5. **Two Runs on the same experiment** (a double click, or two people) both rebuild the same tables; there is no lock, so numbers can mix.
6. **Edits overwrite each other.** Two people editing the same experiment: the last save wins and nothing warns.
7. **Stale views.** Caches are per server process. With more than one server, people can see data up to 2 minutes old and different from each other.
8. **Growing history.** Every refresh keeps its result rows, and job runs, job steps and the audit log are never pruned. This is small today but makes the catalog read slower over months.
9. **No sign-in.** Anyone who can open the URL can change or run anything. Deployment must sit behind the company login (for example Google IAP), and the audit log only records `admin`.

## Proposed work (not started), in the order I would do it

1. **Instant pages. DONE.** Load the small application tables into memory in one parallel read, refresh in the background only when someone is using the app and only the tables BigQuery says changed, and update the copy on every write. A page costs about 0.1 seconds instead of 10 to 15.
2. **Fewer, bigger writes. DONE.** The net effect per table in one statement, sent in the background, so a person never waits for BigQuery to save.
3. **Runs in the background with a queue. DONE.** Start a Run and let the person keep working; show progress; at most about 4 Runs at once, one per experiment; retry on quota errors.
4. **Cost guards.** A dry-run size check before each Run with a cap, and a daily scheduled refresh instead of manual Refresh clicks.
5. **Keep history small.** Keep only the latest live-monitoring result per experiment; prune old job steps.
6. **Deployment shape.** One always-on instance (so the cache is consistent), behind company login, with a request timeout long enough for a Run, and a simple log of Run durations and failures.
7. **Small safety nets that are not red tape:** a "someone changed this while you were editing" warning, and a lock that stops a second Run on the same experiment.

## Streamlit at company scale (notes, 2026-10-04)

Not built; written down so the trade-offs are clear.

- **How it runs:** one long-lived Python server. Each browser tab holds a session in that server's memory, and every click reruns the page script for that session. The app needs a server that stays up, like any web app; in a company that is a container the platform keeps running and restarts.
- **Why this app scales well enough for a team:** its state is in BigQuery, not in the app; heavy work runs in BigQuery and in a background queue; pages read a small in-memory copy. A restart loses nothing.
- **Limits to plan for:**
  - Memory per open tab, so large DataFrames for many users need care.
  - Every click reruns the script, so slow code needs caching or partial reruns (`st.fragment`).
  - Sessions live in one server, so several servers need sticky sessions, and this app's in-memory copy and Run queue assume one server (step 6).
  - Sign-in comes from the company's identity proxy in front of the app (or `st.login`).
  - Long analyses belong in background jobs.
- **Where it fits:** internal tools for tens to a few hundred users, built and kept by data scientists. Not a public, high-traffic product.
- **If it is outgrown:** the statistics, store and service layers (`src/p2/`) do not depend on Streamlit, so only `app/` would be replaced (for example by a web front end on an API).
- **What was not done:** no load test, no sign-in, no multi-server setup. The claim is "the design allows it", not "it was proven at scale".

## Decision (2026-10-01): steps 4 to 7 skipped

P2 is a demo for learning and showing, not a tool for 20 people. Steps 1 to 3 stay as built. Cost guards, pruning history, the deploy shape and the small safety nets (steps 4 to 7) will not be built unless this changes.
