import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import streamlit as st


st.set_page_config(page_title="Experiment Platform", layout="wide")
import common
import ui

ui.inject()

pages = [
    st.Page("views/catalog.py", title="Experiment Catalog", icon=":material/library_books:", default=True),
    st.Page("views/experiments.py", title="Experiment Results", icon=":material/insights:"),
]
if not common.read_only():
    pages.append(st.Page("views/design.py", title="Add Experiment", icon=":material/add_circle:"))
page = st.navigation(pages, position="top")
if common.read_only():
    st.info("Read-only demo on synthetic data: browse the experiments and their results. Adding, editing and running experiments is "
            "turned off here; they work in the full app on BigQuery.", icon=":material/visibility:")
waiting, age = common.save_backlog()
if waiting and age > 30:     # normally changes are written within a couple of seconds; this only shows when BigQuery is slow or unreachable
    st.warning(f"{waiting} change{'s are' if waiting != 1 else ' is'} still being saved to BigQuery (waiting {age:.0f} seconds). "
               "They are safe in the app and will be retried automatically.")
page.run()
