"""The Experiment Catalog: every experiment in one list, with a progress bar that shows how far a live test has run, and an
Edit and an Open results button on each row. Editing opens the shared experiment form in place."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import streamlit as st

import experiment_form
import ui
from common import get_platform, get_registry, read
from p2.pipeline.runner import end_of

ss = st.session_state
MAX_ROWS = 50
WIDTHS = [2.0, 1.4, 1.1, 1.6, 1.4, 2.2, 2.2, 0.9, 1.5]


def progress_of(launch: date, end: date, today: date) -> tuple[float, str]:
    """(share of the test that has run, a short sentence). Live tests say which day they are on out of the total days to the end date."""
    total = (end - launch).days + 1
    if today < launch:
        return 0.0, f"Starts in {(launch - today).days} days"
    if today > end:
        return 1.0, f"Ended, {total} days"
    day = (today - launch).days + 1
    return day / total, f"Live, day {day} of {total}"


PHASE_TONE = {"live": "blue", "upcoming": "yellow", "ended": "green"}


def phase_of(launch: date, end: date, today: date) -> str:
    """live, upcoming or ended, from the dates alone."""
    return "upcoming" if today < launch else "ended" if today > end else "live"


def verdict_chip(row: dict) -> str:
    """The verdict on the primary metric as a coloured chip; grey while there is none."""
    if row["verdict"]:
        return ui.verdict_chip(row["verdict"])
    return ui.chip("Not run yet" if row["status"] in ("Draft", "Designed") else "In progress", "grey")


def render() -> None:
    edit_id = ss.get("catalog_edit")
    if edit_id:
        c = st.columns([1, 6])
        c[0].button("Back to list", key="cat_back", on_click=lambda: ss.pop("catalog_edit", None))
        if not any(e["experiment_id"] == edit_id for e in read("list_experiments", None, None, None)):
            st.error(f"No experiment with the id \"{edit_id}\".")
        else:
            st.subheader(f"Edit {edit_id}")
            experiment_form.render(edit_id, title=False)
        return

    registry = get_registry()
    owners = {m["user_id"]: ui.short_name(m["name"]) for m in read("list_team")}
    rows = read("experiment_catalog")
    today = date.today()
    phase = {r["experiment_id"]: phase_of(r["launch_date"], end_of(r), today) for r in rows}
    in_progress = {r["experiment_id"] for r in get_platform().runs.active()}
    counts = {k: sum(1 for p in phase.values() if p == k) for k in ("live", "upcoming", "ended")}
    top = st.columns([3, 7], vertical_alignment="bottom")
    query = top[0].text_input("Experiment ID", key="cat_search", placeholder="Search by experiment ID").strip().lower()
    top[1].markdown(ui.chip(f"{len(rows)} experiments", "grey") + " " + ui.chip(f"{counts['live']} live", "blue") + " "
                    + ui.chip(f"{counts['upcoming']} not started", "yellow") + " " + ui.chip(f"{counts['ended']} ended", "green"), unsafe_allow_html=True)
    if query:
        rows = [r for r in rows if query in r["experiment_id"].lower()]
    rows = sorted(rows, key=lambda r: ("live", "upcoming", "ended").index(phase[r["experiment_id"]]))     # live first; newest first within each group
    if not rows:
        st.info("No experiments match." if query else "No experiments yet. Add one on the Add Experiment page.")
        return
    if len(rows) > MAX_ROWS:
        st.caption(f"Showing the first {MAX_ROWS} of {len(rows)}. Search by ID to find the others.")
    with st.container(key="cat_header"):
        head = st.columns(WIDTHS)
        for col, label in zip(head, ["Experiment", "Product / owner", "Status", "Primary metric", "Launch to end", "Verdict", "Progress", "", ""]):
            if label:
                col.markdown(f"**{label}**")
    for r in rows[:MAX_ROWS]:
        eid = r["experiment_id"]
        end = end_of(r)
        with st.container(key=f"cat_row_{eid}"):
            c = st.columns(WIDTHS, vertical_alignment="center")
            c[0].markdown(f"**{eid}**")
            c[0].caption(r["name"])
            c[1].markdown(ui.chip(r["product_id"], "grey"), unsafe_allow_html=True)
            c[1].caption(owners.get(r["owner_user_id"], r["owner_user_id"]))
            c[2].markdown(ui.status_chip(r["status"]) + ("<br>" + ui.chip("Run in progress", "blue") if eid in in_progress else ""), unsafe_allow_html=True)
            c[3].write(registry.get(r["primary_metric"]).display_name if r["primary_metric"] else "-")
            c[4].write(str(r["launch_date"]))
            c[4].caption(f"to {end}")
            c[5].markdown(verdict_chip(r), unsafe_allow_html=True)
            frac, text = progress_of(r["launch_date"], end, today)
            c[6].markdown(ui.progress_bar(frac, text, PHASE_TONE[phase[eid]]), unsafe_allow_html=True)
            if c[7].button("Edit", key=f"cat_edit_{eid}", type="tertiary", icon=":material/edit:"):
                ss["catalog_edit"] = eid
                st.rerun()
            if c[8].button("Open results", key=f"cat_open_{eid}", type="primary"):
                ss["open_target"] = eid
                st.switch_page("views/experiments.py")
