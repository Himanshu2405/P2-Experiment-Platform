import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from experiment_form import render

render()

import os

import streamlit as st

from common import current_actor, get_platform, refresh
from p2.services.errors import PlatformError
from p2.simulator.demo import DEMO, MONTH_END, MONTH_START

if os.environ.get("P2_DEV_TOOLS", "1") != "0":    # set P2_DEV_TOOLS=0 at deploy
    st.divider()
    with st.expander("Dev only: land the September demo data"):
        st.caption(f"In production the experiment tool already has your experiment. Here, this lands {len(DEMO)} demo experiments with users "
                   f"assigned on every day from {MONTH_START:%d %b} to {MONTH_END:%d %b %Y}, so you can pick any launch and end date in that month. "
                   "Type one of the ids below in the form above. The id says the product; the first of each pair has a planted effect, the second has none. "
                   "It takes a few minutes and is safe to repeat.")
        st.code("\n".join(f"{eid}   ({product}, {'planted effect' if lift else 'no effect'})" for eid, product, _, lift in DEMO), language=None)
        if st.button("Land September demo data", key="land_sep"):
            try:
                bar = st.progress(0.0, text="Starting...")
                get_platform().land_september_demo(current_actor(), lambda i, n, eid: bar.progress(i / n, text=f"Landed {eid} ({i} of {n})"))
            except PlatformError as e:
                st.error(str(e))
            else:
                refresh()
                st.success("September demo data landed. Type one of the ids above in the form.")
