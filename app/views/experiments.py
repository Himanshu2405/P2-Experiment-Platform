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
from p2.stats.srm import srm_check
import ui
from results_view import NEUTRAL_BAND, diff_chart, diff_frame, fmt_value, metric_table, series_frame, style_table, time_chart

ss = st.session_state
actor = current_actor()
plat = get_platform()
st.title("Experiment Results")
if flash := ss.pop("flash", None):
    st.success(flash)

# ------------------------------------------------------------------ search
if target := ss.pop("open_target", None):     # set by Save and run on the New experiment page
    ss["search_id"] = ss["search_text"] = target
ss.setdefault("search_text", ss.get("search_id", ""))
with st.form("search_form", border=False):
    c = st.columns([3, 1, 6], vertical_alignment="bottom")
    typed = c[0].text_input("Experiment ID", key="search_text", placeholder="for example exp-002",
                            help="Type the experiment id and press Enter or Search.")
    submitted = c[1].form_submit_button("Search", type="primary", width="stretch", key="search_go")
if submitted:
    ss["search_id"] = typed.strip().lower()
wanted = ss.get("search_id", "")
if not wanted:
    st.info("Search for an experiment by its ID to see its results.")
    st.stop()

experiments = read("list_experiments", None, None, None)
exp = next((e for e in experiments if e["experiment_id"] == wanted), None)
if exp is None:
    st.error(f"No experiment with the id \"{wanted}\".")
    close = [e["experiment_id"] for e in experiments if wanted in e["experiment_id"] or e["experiment_id"] in wanted][:8]
    if close:
        st.caption("Similar ids: " + ", ".join(close))
    st.stop()

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
jobs = read("list_jobs", chosen)


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
with st.container(border=True):
    left, right = st.columns([5, 2], vertical_alignment="center")
    with left:
        st.subheader(exp["name"])
        st.markdown(ui.status_chip(status) + " " + ui.chip(progress, tone) + " " + ui.chip(exp["product_id"], "grey"), unsafe_allow_html=True)
        st.markdown(f'<div class="meta"><b>Owner</b> {ui.short_name(owners.get(exp["owner_user_id"], exp["owner_user_id"]))} &nbsp;|&nbsp; <b>Runs</b> {exp["launch_date"]} to {end} '
                    f'({days} days' + (f', planned {exp["runtime_days"]}' if days != exp["runtime_days"] else "") + ")</div>", unsafe_allow_html=True)
        if exp["hypothesis"]:
            st.markdown(f'<div class="meta"><b>Hypothesis</b> {html.escape(exp["hypothesis"])}</div>', unsafe_allow_html=True)
    with right:
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
                    queue_run("Run", "run_go", width="stretch", help="Runs the final analysis again on the data through the end date.")
if not design:
    st.caption("No complete plan saved yet.")
run_panel()
if not run and jobs and jobs[0]["error"]:
    st.error(f"The last run failed: {jobs[0]['error']}")
if not design:
    st.stop()
if busy:        # a Run is rebuilding the numbers: show nothing old, so nobody mistakes last run's numbers for the new ones
    st.caption("The numbers are hidden while this run rebuilds them, so old numbers are not mistaken for new ones. They appear here when it finishes.")
    st.stop()
changes = plat.plan_changes_since_run(chosen) if rows else []
failed_after = bool(rows and jobs and jobs[0]["status"] == "failed" and jobs[0]["started_at"] > rows[0]["run_at"])
if changes:        # one message, not two: what changed, and (if it applies) that the latest run failed
    st.warning("These numbers are out of date: " + "; ".join(changes) + " since they were calculated."
               + (f" The latest run failed, so they are from the last successful run ({rows[0]['run_at']:%Y-%m-%d %H:%M})." if failed_after else "")
               + " Press Run to update them.")
elif failed_after:
    st.warning(f"The latest run failed, so the numbers below are from the last successful run ({rows[0]['run_at']:%Y-%m-%d %H:%M}).")

# ------------------------------------------------------------------ numbers: two tabs, Tables and Charts
no_numbers = ("No numbers yet. " + ("Refresh monitoring to see the live numbers." if running else "Run the final analysis to see the results.")
              if can_edit else "No numbers yet.")
metric_ids = [d["item_id"] for d in design if d["kind"] == "metric"]
role_of = {d["item_id"]: d["role"] for d in design if d["kind"] == "metric"}
names = {m: f"{registry.get(m).display_name} ({role_of[m]})" for m in metric_ids}
version = {d["item_id"]: d["item_version"] for d in design}
primary_id = next((d["item_id"] for d in design if d["role"] == "primary"), None)
# ------------------------------------------------------------------ the headline numbers of the primary metric
headline = next((r for r in rows if r["role"] == "primary"), None) if rows else None
if headline:
    pm = registry.get(headline["item_id"], version[headline["item_id"]])
    is_rate, higher_is_better = pm.type == "binary", pm.good_direction == "higher"
    lift = headline["relative_lift"]
    srm = srm_check(headline["n_control"], headline["n_variant"]) if headline["n_control"] and headline["n_variant"] else None
    if srm:
        share = srm.n_control / (srm.n_control + srm.n_variant)
        with st.container(border=True, key="srm_card"):
            if srm.balanced:
                st.markdown(ui.chip("Balanced", "green") + " &nbsp; **The test is balanced.** Users are split evenly between control and variant.", unsafe_allow_html=True)
            else:
                st.markdown(ui.chip("Not balanced", "red") + f" &nbsp; **The split looks wrong.** Expected 50 / 50, saw {share * 100:.1f} / {100 - share * 100:.1f} "
                            f"({srm.n_control:,} control, {srm.n_variant:,} variant). Check how users were assigned before trusting these numbers.", unsafe_allow_html=True)
    flat = lift is None or abs(lift * 100) < NEUTRAL_BAND
    cards = st.columns(4)
    cards[0].metric(f"{pm.display_name} · control", fmt_value(is_rate, headline["mean_control"]))
    cards[1].metric(f"{pm.display_name} · variant", fmt_value(is_rate, headline["mean_variant"]))
    cards[2].metric("Lift (variant vs control)", "n/a" if lift is None else f"{lift * 100:+.1f}%",
                    delta=fmt_value(is_rate, headline["difference"], True),
                    delta_color="off" if flat else ("normal" if higher_is_better else "inverse"))
    verdict_html = (ui.verdict_chip(headline["verdict"]) if is_final and headline["verdict"] else ui.chip("In progress", "grey"))
    users = f"{headline['n_control'] + headline['n_variant']:,} users ({headline['n_control']:,} control, {headline['n_variant']:,} variant)"
    cards[3].markdown(f'<div class="verdict-card"><div class="label">Verdict on {html.escape(pm.display_name)}</div>{verdict_html}'
                      f'<div class="meta">{"Final analysis" if is_final else f"Final verdict after {end}"}<br>{users}</div></div>', unsafe_allow_html=True)

tab_tables, tab_charts = st.tabs(["Tables", "Charts"])

with tab_tables:
    if not rows:
        st.info(no_numbers)
    else:
        if is_final:
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
        if not view:
            st.dataframe(style_table(metric_table(rows, registry, design, is_final)), hide_index=True, width="stretch")
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
                st.dataframe(style_table(metric_table(shown, registry, design, is_final, power=False)), hide_index=True, width="stretch")
            st.caption("Exploratory. Every metric in a view gets the plain two-sided test at 0.05 with no correction for how many views you look "
                       "at, so a few will look significant by chance. Treat them as leads for a follow-up, not as the verdict."
                       + ("" if is_final else " Live numbers only until the final analysis."))
        if is_final and not view:
            st.caption("Rates use a two-proportion z-test and averages a Welch t-test, at the alpha in your plan (secondary metrics at 0.05, "
                       "exploratory). Guardrails use a one-sided non-inferiority test against your margin. Difference is variant minus control, "
                       "the average treatment effect of assigning a user to the variant.")

with tab_charts:
    series = read("daily_series", chosen)
    if not series:
        st.info(no_numbers)
    else:
        picked = st.multiselect("Metrics in the charts", metric_ids, default=[primary_id] if primary_id else [], format_func=names.get,
                                key=f"chart_metrics_{chosen}", help="Pick the metrics to chart. The primary metric is shown by default.")
        if not picked:
            st.info("Pick at least one metric to see the charts.")
        else:
            st.markdown("**Control and variant over time**")
            for m in picked:
                binary = registry.get(m, version[m]).type == "binary"
                st.altair_chart(time_chart(series_frame(series, m, binary), binary, names[m]), width="stretch")
            st.caption("Cumulative average from launch: each point averages every user assigned so far, each from their own assignment day. The lines "
                       "rise because every user's measurement window grows day by day, and early days include few users. Compare the two lines at the same day.")
            st.markdown("**Difference over time**")
            for m in picked:
                binary = registry.get(m, version[m]).type == "binary"
                st.altair_chart(diff_chart(diff_frame(series, m, binary, band=is_final), binary, names[m]), width="stretch")
            st.caption("Variant minus control, cumulative from launch. " + (
                "The shaded area is the 95% range of the difference. Where it stays on one side of the grey zero line, the arms really differ. "
                "Early days have few users, so the range is wide." if is_final else
                "The range around the line appears after the final analysis, so nobody reads a verdict before the experiment ends."))
