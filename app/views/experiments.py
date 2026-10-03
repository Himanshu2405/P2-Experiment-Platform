import html
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import streamlit as st

from common import current_actor, get_platform, get_registry, read, refresh
from p2.pipeline.runner import end_of
from p2.services.errors import PlatformError
from p2.stats import bayes
from p2.stats.srm import srm_check
import ui
from results_view import (balance_status_card, bayes_reason, bayes_table, bayes_time_frame, chance_chart, diff_chart, diff_frame, frequentist_reason, harm_sentence,
                          metric_table, placebo_status_card, risk_chart, risk_frame, risk_sentence, risk_time_chart, series_frame, style_table, time_chart)

ss = st.session_state
actor = current_actor()
plat = get_platform()
st.title("Experiment Results")
if flash := ss.pop("flash", None):
    st.success(flash)

# ------------------------------------------------------------------ search
experiments = read("list_experiments", None, None, None)
if target := ss.pop("open_target", None):     # set by Save and run on the New experiment page
    ss["search_id"] = ss["search_pick"] = target
ids = sorted(e["experiment_id"] for e in experiments)
if "search_pick" not in ss or ss["search_pick"] not in ids:      # first visit, or the saved pick no longer exists
    ss["search_pick"] = ss.get("search_id") if ss.get("search_id") in ids else None
c = st.columns([3, 7])
wanted = ss["search_id"] = c[0].selectbox("Experiment ID", ids, index=None, key="search_pick", placeholder="Type to search, for example sep",
                                          help="Start typing: the experiments already in the app that match are listed. Pick one to see its results.") or ""
if not wanted:
    st.info("Search for an experiment by its ID to see its results.")
    st.stop()

exp = next(e for e in experiments if e["experiment_id"] == wanted)

chosen = exp["experiment_id"]
owners = {m["user_id"]: m["name"] for m in read("list_team")}
registry = get_registry()
end = end_of(exp)
today = date.today()
running = today <= end
status = exp["status"]
design = read("get_design", chosen)
can_edit = True      # no permissions: everyone can edit and run (trust the data scientists)

final = read("latest_results", chosen, "final")
interim = read("latest_results", chosen, "interim")
is_final = bool(final) and (not interim or final[0]["run_at"] >= interim[0]["run_at"])    # the latest run wins, so extending an experiment shows live numbers again
rows = final if is_final else interim
bayesian = exp.get("method") == "bayesian"
other_method = bool(rows) and not bayesian and (rows[0].get("params") or {}).get("method") == "bayesian"     # saved before the method was switched
if other_method:
    rows, is_final = [], False
jobs = read("list_jobs", chosen)


@st.cache_data(show_spinner=False)
def bayes_numbers(experiment_id: str, key: tuple, _rows: list[dict]) -> list:
    """The Bayesian numbers for some rows, computed from the saved counts, means and variances. Cached per run and plan (`key`)."""
    return get_platform().bayes_outcomes(experiment_id, _rows)


RUN_DONE = {"monitor": "Monitoring refreshed.", "final": "Final analysis finished."}


def queue_run(label: str, key: str, **kw) -> None:
    """A Run button: it puts the Run in the queue and returns at once. Refused mistakes (no plan, final too early) show immediately."""
    if st.button(label, key=key, **kw):
        try:
            ticket = plat.start_run(actor, chosen)
        except PlatformError as e:
            st.error(str(e))
            return
        refresh()
        if ticket["state"] == "succeeded":        # ran straight away (a server set up to run in the caller's thread)
            ss["flash"] = RUN_DONE[ticket["kind"]]
        st.rerun()


run = plat.run_status(chosen)
busy = bool(run) and run["state"] in ("queued", "running")


@st.fragment(run_every=3 if busy else None)
def run_panel() -> None:
    """Shows where the Run is. While it is queued or running this redraws itself every 3 seconds; when it ends the whole page reloads."""
    now = plat.run_status(chosen)
    if not now:
        return
    if now["state"] == "queued":
        st.info(f"Waiting in line, position {now['position']}. Runs start as soon as one of the {plat.runs.max_concurrent} slots is free.")
    elif now["state"] == "running":
        retry = f" (try {now['attempt']}, BigQuery was busy)" if now["attempt"] > 1 else ""
        st.info(f"{'Final analysis' if now['kind'] == 'final' else 'Refresh'} running for {int(now['running_for'] or 0)} seconds{retry}. "
                "You can leave this page; the numbers appear here when it finishes.")
    elif now["state"] == "succeeded":
        plat.runs.acknowledge(chosen)
        refresh()
        ss["flash"] = RUN_DONE[now["kind"]]
        st.rerun()
    elif now["state"] == "failed":
        st.error(f"The run failed: {now['error']}")
        if "no assignments between" in (now["error"] or ""):
            st.caption("The experiment tool has logged no users in this window. Check the launch and end dates against the log "
                       "(the September demo data covers 1 to 30 Sep 2026).")
        if st.button("Dismiss", key="run_dismiss"):
            plat.runs.acknowledge(chosen)
            st.rerun()


days = (end - exp["launch_date"]).days + 1
if today < exp["launch_date"]:
    progress, tone = f"Starts in {(exp['launch_date'] - today).days} days", "yellow"
elif today <= end:
    progress, tone = f"Day {(today - exp['launch_date']).days + 1} of {days}", "blue"
else:
    progress, tone = f"Ended {end}", "grey"

# ------------------------------------------------------------------ what the three cards on the right of the details will say
no_numbers = ("No numbers yet. " + ("Refresh monitoring to see the live numbers." if running else "Run the final analysis to see the results.")
              if can_edit else "No numbers yet.")
metric_ids = [d["item_id"] for d in design if d["kind"] == "metric"]
role_of = {d["item_id"]: d["role"] for d in design if d["kind"] == "metric"}
names = {m: f"{registry.get(m).display_name} ({role_of[m]})" for m in metric_ids}
version = {d["item_id"]: d["item_version"] for d in design}
primary_id = next((d["item_id"] for d in design if d["role"] == "primary"), None)
headline = next((r for r in rows if r["role"] == "primary"), None) if rows and design and not busy else None   # a Run in progress hides the old numbers
balance_card = ui.check_card("Balance check", ui.chip("Waiting for a Run", "grey"))
placebo_card = ui.check_card("Placebo A/A check", ui.chip("Waiting for a Run", "grey"))
verdict_title, verdict_html = "Verdict", ui.chip("No verdict yet", "grey")
reason = ("Save a complete plan to see a verdict." if not design else "A Run is rebuilding the numbers." if busy
          else "The numbers were calculated with the other method. Press Refresh to calculate them again." if other_method else no_numbers)
if headline:
    pm = registry.get(headline["item_id"], version[headline["item_id"]])
    is_rate = pm.type == "binary"
    srm = srm_check(headline["n_control"], headline["n_variant"]) if headline["n_control"] and headline["n_variant"] else None
    if srm:
        balance_card = balance_status_card(srm)
    check = (headline.get("params") or {}).get("placebo")
    placebo_card = placebo_status_card(check)
    verdict_title = f"Verdict on {pm.display_name}"
    if bayesian:
        head_out = bayes_numbers(chosen, (headline["run_at"], "headline", str(exp["launch_date"]), str(end), design), [headline])[0]
        verdict_html = ui.verdict_chip(head_out.verdict) if head_out and head_out.verdict else ui.chip("Refresh needed", "grey")
        head_plan = bayes.plan_from_row(pm, next(d for d in design if d["item_id"] == headline["item_id"]))
        head_min_days = min(head_plan.min_days if head_plan and head_plan.role == "primary" else bayes.DEFAULT_MIN_DAYS, days)
        threshold = head_plan.threshold_abs(headline["mean_control"]) if head_plan and head_plan.role == "primary" else None
        reason = bayes_reason(head_out, is_rate, threshold, head_min_days)
    else:
        verdict_html = (ui.verdict_chip(headline["verdict"]) if is_final and headline["verdict"] else ui.chip("In progress", "grey"))
        reason = frequentist_reason(headline, is_final, end)

# ------------------------------------------------------------------ Experiment Details: the experiment on the left, the checks and the verdict on the right
with st.expander("Experiment Details", expanded=True):
    left, right = st.columns(2, gap="large")
    with left:
        st.subheader(exp["name"])
        st.markdown(ui.status_chip(status) + " " + ui.chip(progress, tone) + " " + ui.chip(exp["product_id"], "grey"), unsafe_allow_html=True)
        runs = f'{exp["launch_date"]} to {end} ({days} days' + (f', planned {exp["runtime_days"]}' if days != exp["runtime_days"] else "") + ")"
        st.markdown('<div class="fields">' + ui.field("Owner", html.escape(ui.short_name(owners.get(exp["owner_user_id"], exp["owner_user_id"]))))
                    + ui.field("Runs", html.escape(runs)) + "</div>"
                    + (ui.field("Hypothesis", html.escape(exp["hypothesis"])) if exp["hypothesis"] else ""), unsafe_allow_html=True)
        st.markdown('<div class="detail-spacer"></div>', unsafe_allow_html=True)
        buttons = st.columns(2)
        with buttons[0]:
            if st.button("Edit", key="edit_plan", width="stretch", help="Open this experiment in the Experiment Catalog to change it. Everything except the ID can be changed."):
                ss["catalog_edit"] = chosen
                st.switch_page("views/catalog.py")
        with buttons[1]:
            if design and status in ("Designed", "Running", "Analyzed") and busy:
                st.button("Queued" if run["state"] == "queued" else "Running", key="run_busy", disabled=True, width="stretch",
                          help="A Run for this experiment is already in progress.")
            elif design and status in ("Designed", "Running", "Analyzed"):
                if running:
                    queue_run("Refresh", "monitor_go", type="primary", width="stretch",
                              help=f"Refresh monitoring: builds the data through yesterday and updates the live numbers. The final analysis unlocks after {end}.")
                elif status != "Analyzed":
                    queue_run("Run final analysis", "run_go", type="primary", width="stretch", help="Runs the full tests on the whole runtime and gives the verdict.")
                else:
                    queue_run("Refresh", "run_go", width="stretch", help="Refresh: runs the final analysis again on the data through the end date.")
    with right:
        verdict_card = (f'<div class="verdict-card"><div class="label">{html.escape(verdict_title)}</div>{verdict_html}'
                        f'<div class="reason">{html.escape(reason)}</div></div>')
        st.markdown(ui.details_panel(balance_card, placebo_card, verdict_card), unsafe_allow_html=True)
if not design:
    st.caption("No complete plan saved yet.")
run_panel()
if not run and jobs and jobs[0]["error"]:
    st.error(f"The last run failed: {jobs[0]['error']}")
if not design:
    st.stop()
if other_method:
    st.info("The saved numbers were calculated with the Bayesian method, and this experiment is now frequentist. Press Refresh to calculate them again.")
if busy:        # a Run is rebuilding the numbers: show nothing old, so nobody mistakes last run's numbers for the new ones
    st.caption("The numbers are hidden while this run rebuilds them, so old numbers are not mistaken for new ones. They appear here when it finishes.")
    st.stop()
changes = plat.plan_changes_since_run(chosen) if rows else []
failed_after = bool(rows and jobs and jobs[0]["status"] == "failed" and jobs[0]["started_at"] > rows[0]["run_at"])
if changes:        # one message, not two: what changed, and (if it applies) that the latest run failed
    st.warning("These numbers are out of date: " + "; ".join(changes) + " since they were calculated."
               + (f" The latest run failed, so they are from the last successful run ({rows[0]['run_at']:%Y-%m-%d %H:%M})." if failed_after else "")
               + " Press Refresh to update them.")
elif failed_after:
    st.warning(f"The latest run failed, so the numbers below are from the last successful run ({rows[0]['run_at']:%Y-%m-%d %H:%M}).")

# ------------------------------------------------------------------ numbers: two tabs, Tables and Charts
tab_tables, tab_charts = st.tabs(["Tables", "Charts"])

with tab_tables:
    if not rows:
        st.info(no_numbers)
    else:
        if bayesian:
            min_days = next((d["min_days"] for d in design if d["role"] == "primary" and d.get("min_days")), bayes.DEFAULT_MIN_DAYS)
            if (rows[0].get("through_date") or end) >= end:
                st.caption(f"Bayesian analysis on the full runtime ({exp['launch_date']} to {end}).")
            else:
                st.info(f"Live Bayesian numbers, data through {rows[0]['through_date']}. The verdict appears after {min(min_days, days)} days of data "
                        f"and keeps updating until {end}.")
        elif is_final:
            st.caption(f"Final analysis on the full runtime ({exp['launch_date']} to {end}).")
        else:
            st.warning(f"Interim view, data through {rows[0]['through_date']}. No conclusions until {end}: the p-value, interval and verdict "
                       f"appear after the final analysis.")
        slices = read("segment_results", chosen)
        label_of = {d["item_id"]: d["display_name"] for kind in ("dimension", "filter") for d in read("certified_items", kind, exp["product_id"])}
        dim_ids = [d["item_id"] for d in design if d["kind"] == "dimension"]
        flt_ids = [d["item_id"] for d in design if d["kind"] == "filter"]
        c = st.columns([3, 3, 4])
        seg_pick = c[0].selectbox("Segment", [""] + dim_ids, key=f"seg_pick_{chosen}", disabled=not dim_ids,
                                  format_func=lambda o: "Everyone" if not o else label_of.get(o, o),
                                  help="Split the results by a segment chosen in the experiment's plan. Every value of the segment gets its own table. Everyone is the default.")
        flt_pick = c[1].selectbox("Filter", [""] + flt_ids, key=f"flt_pick_{chosen}", disabled=not flt_ids,
                                  format_func=lambda o: "No filter" if not o else label_of.get(o, o),
                                  help="Show only the users who pass a filter chosen in the experiment's plan. It can be combined with a segment.")
        view = bool(seg_pick or flt_pick)
        st.caption("Colour key: green = moved the good way, red = moved the bad way, yellow = barely moved (under 0.5%), grey = no clear difference. "
                   "Where lower is better (for example refund rate), a rise is red.")
        flt_text = f"Users passing {label_of.get(flt_pick, flt_pick)}" if flt_pick else ""
        def table_for(shown: list[dict], scope: str, power: bool = True):
            if bayesian:
                numbers = bayes_numbers(chosen, (shown[0]["run_at"], scope, str(exp["launch_date"]), str(end), design), shown)
                return style_table(bayes_table(shown, numbers, registry, design))
            return style_table(metric_table(shown, registry, design, is_final, power=power))

        if not view:
            st.dataframe(table_for(rows, "main"), hide_index=True, width="stretch")
        else:
            mine = [r for r in slices if (r["dimension_id"] or "") == seg_pick and (r["filter_id"] or "") == flt_pick]
            values = sorted({r["segment"] for r in mine})
            if not values:
                st.info("Not enough users in this view yet to compare the two arms.")
            for value in values:
                shown = [r for r in mine if r["segment"] == value]
                title = " | ".join(x for x in (f"{label_of.get(seg_pick, seg_pick)} = {value}" if seg_pick else "", flt_text) if x)
                st.markdown(f"**{title}**: {shown[0]['n_control'] + shown[0]['n_variant']:,} users "
                            f"({shown[0]['n_control']:,} control, {shown[0]['n_variant']:,} variant)")
                st.dataframe(table_for(shown, f"{seg_pick}|{flt_pick}|{value}", False), hide_index=True, width="stretch")
            if bayesian:
                st.caption("Exploratory. Each view has its own Bayesian numbers with no adjustment for how many views you look at, so one will look "
                           "good by chance. Treat them as leads for a follow-up, not as the verdict.")
            else:
                st.caption("Exploratory. Every metric in a view gets the plain two-sided test at 0.05 with no correction for how many views you look "
                           "at, so a few will look significant by chance. Treat them as leads for a follow-up, not as the verdict."
                           + ("" if is_final else " Live numbers only until the final analysis."))
        if bayesian and not view:
            st.caption("Bayesian, with a flat prior (the data decides). Chance variant wins is the probability the variant is better in the metric's good "
                       "direction. Risk is the average loss if you pick that arm and it turns out to be the worse one: ship the variant and it is worse, "
                       "or keep control and the variant was better. The verdict compares both risks with your threshold. Guardrails show the chance the "
                       "metric got worse by more than your margin. Difference is variant minus control.")
        if is_final and not view and not bayesian:
            st.caption("Rates use a two-proportion z-test and averages a Welch t-test, at the alpha in your plan (secondary metrics at 0.05, "
                       "exploratory). Guardrails use a one-sided non-inferiority test against your margin. Difference is variant minus control, "
                       "the average treatment effect of assigning a user to the variant.")

with tab_charts:
    series = read("daily_series", chosen)
    if not series:
        st.info(no_numbers)
    else:
        with st.form(f"chart_form_{chosen}", border=False):       # in a form the charts change only when Apply is pressed
            c = st.columns([8, 1], vertical_alignment="bottom")
            picked = c[0].multiselect("Metrics in the charts", metric_ids, default=[primary_id] if primary_id else [], format_func=names.get,
                                      key=f"chart_metrics_{chosen}",
                                      help="Pick the metrics to chart, then press Apply. The primary metric is shown by default.")
            c[1].form_submit_button("Apply", type="primary", width="stretch", key="chart_apply")
        if not picked:
            st.info("Pick at least one metric to see the charts.")
        else:
            st.markdown("**Control and variant over time**")
            for m in picked:
                binary = registry.get(m, version[m]).type == "binary"
                st.altair_chart(time_chart(series_frame(series, m, binary), binary, names[m]), width="stretch")
            st.caption("Cumulative average from launch: each point averages every user assigned so far, each from their own assignment day. The lines "
                       "rise because every user's measurement window grows day by day, and early days include few users. Compare the two lines at the same day.")
            if bayesian:
                by_item = {r["item_id"]: r for r in rows}
                design_of = {d["item_id"]: d for d in design if d["kind"] == "metric"}
                ready = {}                                   # metric -> (metric definition, plan, its Bayesian numbers, threshold in units)
                for m in picked:
                    metric, r = registry.get(m, version[m]), by_item.get(m)
                    arms = bayes.arms_of(metric, r) if r else None
                    if arms is None:
                        st.info(f"{names[m]}: refresh to see the Bayesian charts.")
                        continue
                    plan = bayes.plan_from_row(metric, design_of[m])
                    out = bayes_numbers(chosen, (r["run_at"], f"chart-{m}", str(exp["launch_date"]), str(end), design), [r])[0]
                    threshold = plan.threshold_abs(arms[0].mean) if plan and plan.role == "primary" else None
                    ready[m] = (metric, plan, out, threshold, arms[0].mean)
                if ready:
                    st.markdown("**Risk of each choice**")
                    for m, (metric, plan, out, threshold, control_mean) in ready.items():
                        binary = metric.type == "binary"
                        with st.container(border=True):
                            st.markdown(f"**{names[m]}**")
                            if plan is not None and plan.role == "guardrail":
                                st.markdown(harm_sentence(out, binary, plan.margin_abs(control_mean), plan.harm_limit))
                                continue
                            st.markdown(risk_sentence(out, binary, threshold))
                            left, right = st.columns(2)
                            left.altair_chart(risk_chart(risk_frame(out, binary, threshold), binary, "On all the data so far"), width="stretch")
                            frame = bayes_time_frame(series, metric, threshold)
                            if not frame.empty:
                                right.altair_chart(risk_time_chart(frame, binary, "By day"), width="stretch")
                    st.caption("Each bar is what that choice would cost on average if it turns out to be the wrong one. The shorter bar is the safer "
                               "choice. Green means it is under the most you accept to lose (the red dashed line). The lines on the right show the "
                               "same two risks day by day: a choice is safe once its line stays under the red line.")
                    st.markdown("**95% credible interval and chance the variant wins, by day**")
                    for m, (metric, plan, out, threshold, _) in ready.items():
                        binary = metric.type == "binary"
                        frame = bayes_time_frame(series, metric, threshold)
                        if frame.empty:
                            continue
                        st.markdown(f"*{names[m]}*")
                        left, right = st.columns(2)
                        left.altair_chart(diff_chart(frame, binary, "Difference, 95% credible interval", band_label="95% credible"), width="stretch")
                        right.altair_chart(chance_chart(frame, "Chance the variant wins"), width="stretch")
                    st.caption("Left: the line is the best estimate of variant minus control, and the shaded band is where the true difference is with "
                               "95% probability. When the whole band is on one side of the grey zero line, the two arms really differ. Right: the chance "
                               "the variant is better; 50% is a coin flip. Both build up from launch, so the first days have few users and move a lot.")
            else:
                st.markdown("**Difference over time**")
                for m in picked:
                    binary = registry.get(m, version[m]).type == "binary"
                    st.altair_chart(diff_chart(diff_frame(series, m, binary, band=is_final), binary, names[m]), width="stretch")
                st.caption("Variant minus control, cumulative from launch. " + (
                    "The shaded area is the 95% range of the difference. Where it stays on one side of the grey zero line, the arms really differ. "
                    "Early days have few users, so the range is wide." if is_final else
                    "The range around the line appears after the final analysis, so nobody reads a verdict before the experiment ends."))
