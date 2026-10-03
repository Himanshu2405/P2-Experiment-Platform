import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import streamlit as st

import experiment_catalog
from common import read

st.title("Experiment Catalog")
if flash := st.session_state.pop("flash", None):
    st.success(flash)


experiments_tab, metrics_tab = st.tabs(["Experiments", "Metrics"])
with experiments_tab:
    experiment_catalog.render()

with metrics_tab:
    team = {m["user_id"]: m["name"] for m in read("list_team")}
    metrics = read("certified_items", "metric", None)
    pick_metric = st.selectbox("Select metric", [""] + [m["item_id"] for m in metrics], key="m_pick",
                               format_func=lambda i: "All metrics" if not i else next(m["display_name"] for m in metrics if m["item_id"] == i),
                               help="Type to search. Pick a metric to see only that one.")
    shown = [m for m in metrics if not pick_metric or m["item_id"] == pick_metric]
    st.dataframe(pd.DataFrame([{"Metric": m["display_name"], "Product": m["product_id"],
                                "Added by": "System (seeded)" if m["author"] == "system" else team.get(m["author"], m["author"]),
                                "Definition": m["description"]} for m in shown]),
                 hide_index=True, width="stretch", column_config={"Definition": st.column_config.TextColumn(width="large")})
